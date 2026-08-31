"""Orchestrates a full daily price-check run: enumerate candidate travel
dates, scrape and parse each one, decide whether any fare beats the
alert threshold, and send a real email if so (or, in TEST_RUN, always
send something real so a manual run exercises the full pipeline).

See docs/plans/001-train-price-alert.md Task 6 for the full spec this
implements, and Task 7 for TEST_RUN and the RUN_HOUR_LONDON time gate.
"""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import booked_dates, config, notifier, parser, price_log, scraper, term_dates
from src.models import AlertMatch, TrainOption

logger = logging.getLogger(__name__)

# Where scraper debug artifacts (screenshot/HTML/captured response) land
# on a failed date — matches .gitignore's `artifacts/` entry and Task 7's
# workflow, which uploads this directory only if the job fails.
ARTIFACTS_DIR = Path("artifacts")

# Randomised pause between consecutive date checks, in seconds — mild
# pacing against a site with no bot protection to trip (see CLAUDE.md),
# not an evasion measure.
PAUSE_BETWEEN_DATES_SECONDS = (5, 15)


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
    src/parser.py's module docstring).
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


def _log_target_summary(travel_date: date, targets: dict[str, TrainOption | None]) -> None:
    for departure_time, option in targets.items():
        if option is None:
            logger.info("[%s] %s: not found", travel_date.isoformat(), departure_time)
        elif option.sold_out:
            logger.info("[%s] %s: sold out", travel_date.isoformat(), departure_time)
        else:
            logger.info("[%s] %s: %s", travel_date.isoformat(), departure_time, option.price)


def main(today: date | None = None, now: datetime | None = None) -> int:
    """`now` is the current Europe/London-aware instant, used only for
    RUN_HOUR_LONDON's time-of-day gate — kept separate from `today` (a
    plain date) so tests can freeze one without the other. Both default
    to the real clock.
    """
    _ensure_logging_configured()

    if now is None:
        now = datetime.now(config.LONDON)
    if today is None:
        today = now.date()

    if not config.SKIP_TIME_GATE and now.astimezone(config.LONDON).hour != config.RUN_HOUR_LONDON:
        logger.info(
            "current Europe/London time is %s, not %02d:xx — this cron "
            "slot isn't real 8pm London today (see the dual-cron-line "
            "design in docs/plans/001-train-price-alert.md Task 7), "
            "no-op",
            now.astimezone(config.LONDON).strftime("%H:%M"),
            config.RUN_HOUR_LONDON,
        )
        return 0

    all_candidates = term_dates.checkable_dates(today + timedelta(days=1), term_dates.LAST_KNOWN_DATE)
    booked = booked_dates.load_booked_dates(config.BOOKED_DATES_PATH)
    candidates = [d for d in all_candidates if d not in booked]

    skipped = [d for d in all_candidates if d in booked]
    if skipped:
        logger.info(
            "skipping %d already-booked date(s): %s",
            len(skipped),
            ", ".join(d.isoformat() for d in skipped),
        )

    if config.MAX_DATES is not None:
        candidates = candidates[: config.MAX_DATES]

    if not candidates:
        if all_candidates and len(skipped) == len(all_candidates):
            logger.info("All remaining dates are already booked — nothing to do.")
        else:
            logger.info("No checkable travel dates remaining this school year — nothing to do.")
        return 0

    secrets = config.get_secrets()

    results: dict[date, dict[str, TrainOption | None]] = {}
    failures: list[tuple[date, str]] = []

    for index, travel_date in enumerate(candidates):
        if index > 0:
            time.sleep(random.uniform(*PAUSE_BETWEEN_DATES_SECONDS))

        try:
            raw = scraper.fetch_journey_search(travel_date, artifacts_dir=ARTIFACTS_DIR)
        except (scraper.BlockedError, scraper.HijackedError) as exc:
            logger.error(
                "[%s] %s — aborting the whole run, not attempting further dates: %s",
                travel_date.isoformat(),
                type(exc).__name__,
                exc,
            )
            return 1
        except scraper.ScraperError as exc:
            logger.warning("[%s] scrape failed, skipping this date: %s", travel_date.isoformat(), exc)
            failures.append((travel_date, str(exc)))
            continue

        try:
            options = parser.parse_journeys(raw, travel_date)
        except parser.ParseError as exc:
            logger.warning("[%s] parse failed, skipping this date: %s", travel_date.isoformat(), exc)
            failures.append((travel_date, str(exc)))
            continue

        targets = parser.select_target_trains(options, config.TARGET_DEPARTURES)
        _log_target_summary(travel_date, targets)
        results[travel_date] = targets

        price_log.append_price_log(
            config.PRICE_LOG_PATH,
            datetime.now(timezone.utc),
            [(travel_date, departure_time, option) for departure_time, option in targets.items()],
        )

    if not results:
        logger.error("all %d candidate date(s) failed — see failures logged above", len(candidates))
        return 1
    if failures:
        logger.warning(
            "%d of %d candidate date(s) failed; continuing with the %d that succeeded",
            len(failures),
            len(candidates),
            len(results),
        )

    matches = evaluate(results)

    is_test_summary = False
    if not matches and config.TEST_RUN:
        matches = _best_effort_matches_for_test(results)
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
