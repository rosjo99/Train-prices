"""Orchestrates a full daily price-check run: enumerate candidate travel
dates, scrape and parse each one (several at once), decide whether any
non-booked fare beats the alert threshold, and send a real email if so
(or, in TEST_RUN, always send something real so a manual run exercises
the full pipeline).

See docs/plans/001-train-price-alert.md Task 6 for the full spec this
implements, and Task 7 for TEST_RUN.
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import booked_dates, config, notifier, parser, price_log, scraper, term_dates
from src.models import AlertMatch, TrainOption

logger = logging.getLogger(__name__)

# Where scraper debug artifacts (screenshot/HTML/captured response) land
# on a failed date — matches .gitignore's `artifacts/` entry and Task 7's
# workflow, which uploads this directory only if the job fails.
ARTIFACTS_DIR = Path("artifacts")

# How many travel dates are scraped concurrently, each getting its own
# browser. NRE has no bot protection to trip (see CLAUDE.md), so this is
# purely a wall-clock lever, not politeness pacing — picked to comfortably
# fit a GitHub-hosted runner's CPU/memory running that many headless
# Chromium instances at once.
PARALLEL_DATES = 5

# National Rail Enquiries only releases fares up to ~12 weeks ahead —
# candidate dates beyond that horizon fail every time (scrape or parse
# error), not just occasionally, since there's simply no fare data yet.
# Once this many dates in a row fail, stop scheduling any further batches
# rather than spending the full per-date retry budget on every remaining
# date of the school year. Dates are grouped into concurrent batches of
# PARALLEL_DATES (see main()), so this is now checked once per batch in
# original-date order rather than after every single date — a batch that
# straddles the threshold can attempt up to PARALLEL_DATES-1 dates beyond
# it before the run notices, a small, accepted cost of running dates in
# parallel. A run that succeeds on some dates and then hits this cutoff
# still alerts on whatever it found.
MAX_CONSECUTIVE_FAILURES = 5


def _ensure_logging_configured() -> None:
    """Configure a stdout, timestamped logger if nothing has already.

    Idempotent, same pattern as src.scraper's own copy — this is the
    real entrypoint, so it runs first, but each module stays independent
    (e.g. a test importing src.scraper alone still gets logging set up).
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


def _fetch_and_parse_one(
    travel_date: date,
) -> dict[str, TrainOption | None] | Exception:
    """Runs in a worker thread: one full scrape + parse for a single
    travel date. Returns the outcome instead of raising/returning, so a
    whole concurrent batch's results can be gathered and then processed,
    in original date order, after every worker in it has finished.
    """
    try:
        raw = scraper.fetch_journey_search(travel_date, artifacts_dir=ARTIFACTS_DIR)
    except scraper.ScraperError as exc:
        return exc
    try:
        options = parser.parse_journeys(raw, travel_date)
    except parser.ParseError as exc:
        return exc
    return parser.select_target_trains(options, config.TARGET_DEPARTURES)


def evaluate(
    targets_by_date: dict[date, dict[str, TrainOption | None]],
) -> list[AlertMatch]:
    """Pure decision function: which TrainOptions beat the price
    threshold.

    A match requires price is not None, currency == "GBP", and
    price < config.PRICE_THRESHOLD (strictly less-than — exactly the
    threshold does not alert). Whether the 16-25 railcard discount was
    positively confirmed (option.railcard_applied) is informational only
    — carried through to the CSV log and the email — and no longer gates
    whether a price counts, per explicit decision: alert on any
    unbooked fare under threshold, confirmed discount or not (see
    src/parser.py's module docstring). Booked dates are filtered out by
    the caller before this ever sees them — see main()'s
    `alertable_results`.
    """
    matches: list[AlertMatch] = []
    seen: set[tuple[date, str]] = set()

    for travel_date, targets in targets_by_date.items():
        for option in targets.values():
            if option is None:
                continue
            if not (
                option.price is not None
                and option.currency == "GBP"
                and option.price < config.PRICE_THRESHOLD
            ):
                continue
            key = (travel_date, option.departure_time)
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                AlertMatch(travel_date=travel_date, option=option, threshold=config.PRICE_THRESHOLD)
            )

    return matches


def _best_effort_matches_for_test(
    targets_by_date: dict[date, dict[str, TrainOption | None]],
) -> list[AlertMatch]:
    """TEST_RUN fallback for when nothing found is genuinely below
    threshold: pick the single cheapest real fare across the whole run
    (regardless of the threshold) so a manual test run still sends one
    genuine email end to end — using only real scraped data, never a
    fabricated price. Returns [] if literally no priced fare was found
    at all (e.g. every target sold out) — there's then nothing real to
    report.
    """
    best: AlertMatch | None = None
    for travel_date, targets in targets_by_date.items():
        for option in targets.values():
            if option is None or option.price is None:
                continue
            if best is None or option.price < best.option.price:
                best = AlertMatch(
                    travel_date=travel_date, option=option, threshold=config.PRICE_THRESHOLD
                )
    return [best] if best is not None else []


def _log_stopped_early(last_failed_date: date, remaining_count: int) -> None:
    logger.warning(
        "[%s] %d consecutive dates failed — stopping early, assuming fares "
        "aren't released yet this far out (NRE releases fares roughly 12 "
        "weeks ahead); %d further candidate date(s) were not attempted",
        last_failed_date.isoformat(),
        MAX_CONSECUTIVE_FAILURES,
        remaining_count,
    )


def _log_target_summary(travel_date: date, targets: dict[str, TrainOption | None]) -> None:
    for departure_time, option in targets.items():
        if option is None:
            logger.info("[%s] %s: not found", travel_date.isoformat(), departure_time)
        elif option.sold_out:
            logger.info("[%s] %s: sold out", travel_date.isoformat(), departure_time)
        else:
            logger.info("[%s] %s: %s", travel_date.isoformat(), departure_time, option.price)


def main(today: date | None = None) -> int:
    _ensure_logging_configured()

    if today is None:
        today = datetime.now(config.LONDON).date()

    candidates = term_dates.checkable_dates(today + timedelta(days=1), term_dates.LAST_KNOWN_DATE)
    booked = booked_dates.load_booked_dates(config.BOOKED_DATES_PATH)

    if config.MAX_DATES is not None:
        candidates = candidates[: config.MAX_DATES]

    if not candidates:
        logger.info("No checkable travel dates remaining this school year — nothing to do.")
        return 0

    booked_candidates = [d for d in candidates if d in booked]
    if booked_candidates:
        logger.info(
            "%d of %d candidate date(s) are already booked — still checking and "
            "logging their prices (for the website), but excluding them from "
            "alerting: %s",
            len(booked_candidates),
            len(candidates),
            ", ".join(d.isoformat() for d in booked_candidates),
        )

    secrets = config.get_secrets()

    results: dict[date, dict[str, TrainOption | None]] = {}
    failures: list[tuple[date, str]] = []
    consecutive_failures = 0

    with ThreadPoolExecutor(max_workers=PARALLEL_DATES) as executor:
        for batch_start in range(0, len(candidates), PARALLEL_DATES):
            batch = candidates[batch_start : batch_start + PARALLEL_DATES]
            outcomes = list(zip(batch, executor.map(_fetch_and_parse_one, batch)))

            stop_early = False
            for offset, (travel_date, outcome) in enumerate(outcomes):
                if isinstance(outcome, (scraper.BlockedError, scraper.HijackedError)):
                    logger.error(
                        "[%s] %s — aborting the whole run, not scheduling further "
                        "dates: %s",
                        travel_date.isoformat(),
                        type(outcome).__name__,
                        outcome,
                    )
                    return 1

                if isinstance(outcome, Exception):
                    logger.warning(
                        "[%s] scrape/parse failed, skipping this date: %s",
                        travel_date.isoformat(),
                        outcome,
                    )
                    failures.append((travel_date, str(outcome)))
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        remaining = len(candidates) - (batch_start + offset) - 1
                        _log_stopped_early(travel_date, remaining)
                        stop_early = True
                        break
                    continue

                consecutive_failures = 0
                targets = outcome
                _log_target_summary(travel_date, targets)
                results[travel_date] = targets

                price_log.append_price_log(
                    config.PRICE_LOG_PATH,
                    datetime.now(timezone.utc),
                    [(travel_date, departure_time, option) for departure_time, option in targets.items()],
                )

            if stop_early:
                break

    if not results:
        logger.error("all %d attempted candidate date(s) failed — see failures logged above", len(failures))
        return 1
    if failures:
        logger.warning(
            "%d of %d attempted candidate date(s) failed; continuing with the %d that succeeded",
            len(failures),
            len(results) + len(failures),
            len(results),
        )

    alertable_results = {d: targets for d, targets in results.items() if d not in booked}

    matches = evaluate(alertable_results)

    is_test_summary = False
    if not matches and config.TEST_RUN:
        matches = _best_effort_matches_for_test(alertable_results)
        is_test_summary = bool(matches)
        if is_test_summary:
            logger.info(
                "TEST_RUN: nothing below threshold, sending the cheapest real fare "
                "found instead (£%s) so this test exercises Resend delivery too",
                matches[0].option.price,
            )

    if not matches:
        logger.info("no fares below threshold — nothing to alert on")
        return 0

    try:
        notifier.send_alert(matches, secrets, dry_run=False)
    except notifier.NotifierError as exc:
        logger.error("failed to send alert email: %s", exc)
        return 1

    if is_test_summary:
        logger.info("sent test summary email (real scraped data, not a genuine alert)")
    else:
        logger.info("sent alert for %d match(es)", len(matches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
