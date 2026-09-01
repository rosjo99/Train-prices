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
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import booked_dates, config, notifier, parser, price_log, scraper, term_dates
from src.models import AlertMatch, DateRow, TrainOption

logger = logging.getLogger(__name__)

# Where scraper debug artifacts (screenshot/HTML/captured response) land
# on a failed date — matches .gitignore's `artifacts/` entry and Task 7's
# workflow, which uploads this directory only if the job fails.
ARTIFACTS_DIR = Path("artifacts")

# How many travel dates are scraped concurrently, each getting its own
# browser. TPE has shown no bot protection to trip (see CLAUDE.md), so
# this is purely a wall-clock lever, not politeness pacing — picked to
# comfortably fit a GitHub-hosted runner's CPU/memory running that many
# browser instances at once. Chosen against Playwright/Chromium; now that
# src.scraper drives Camoufox/Firefox instead (docs/plans/005-migrate-to-
# tpe.md), eight concurrent Firefox instances is a genuinely different
# memory/CPU proposition and this number has not been re-validated — see
# that plan's §7.2. Left unchanged here deliberately, so the first live
# run under the new retailer/browser isn't also confounded by a
# concurrency change at the same time; it's the first thing to revisit if
# that run is slow or shows a spike of otherwise-inexplicable timeouts.
PARALLEL_DATES = 8

# How many days out a travel date can be and still get the full 3-attempt
# retry budget (see SPECULATIVE_ATTEMPTS below) rather than being demoted
# to a single attempt.
#
# The 94-day figure this constant used to be based on (measured three
# times, zero drift — docs/plans/003-scheduler-and-retry-horizon.md §1.1)
# was National Rail Enquiries' fare-release horizon specifically. It said
# nothing about TransPennine Express, the retailer this repo now scrapes
# (docs/plans/005-migrate-to-tpe.md), and a 400-day placeholder was used
# in its place while that was unknown (see that plan's §7.1 for the
# original reasoning, kept as a historical record).
#
# That placeholder has since been replaced with a real, if still
# un-measured-by-this-codebase, number: Great Western Railway (GWR) is
# the train operating company that actually sets fares on this Oxford→
# Paddington route — every fare object in the captured TPE fixture has
# its "setter" field pointing at /data/tocs/GW, not TPE itself. Per the
# user's own domain knowledge of GWR (stated 2026-09-01, day of merge —
# see docs/plans/005-migrate-to-tpe.md §7.1's addendum), GWR releases
# weekday advance tickets up to 24 weeks (168 days) ahead. That's a
# person's stated knowledge of the operator, not a live measurement this
# repo has made, and is treated accordingly — a considered estimate, not
# a finding — but it is far more precise than the "many months further
# out" guess 400 was based on, so 168 replaces it here.
#
# Unlike the old placeholder, 168 days is comfortably *inside* the
# candidate range a run this school year can produce (term_dates.
# LAST_KNOWN_DATE is currently Thu 8 Jul 2027, ~310 days from
# 2026-09-01), so this reactivates the machinery the 400-day value had
# put to sleep: dates beyond 168 days out now get demoted to
# SPECULATIVE_ATTEMPTS (a single attempt) instead of the full 3, and the
# boundary-priority dispatch (_dispatch_order / BOUNDARY_PRIORITY_ZONE_
# DAYS) now has a real boundary partway through the candidate range to
# prioritise, instead of being a no-op past the end of every run's
# candidate list.
#
# If 168 turns out wrong (too close, i.e. GWR's real horizon is nearer
# than believed, or TPE just has a bad day), MAX_CONSECUTIVE_FAILURES
# below remains the reactive backstop, unchanged — a run failing on a
# stretch of far-out dates will still stop early rather than burning
# retry budget on every remaining candidate date. This constant affects
# attempt count only — it is not a cap on which dates get checked;
# MAX_CONSECUTIVE_FAILURES and term_dates.LAST_KNOWN_DATE are what bound
# that.
FULL_RETRY_HORIZON_DAYS = 168

# How many attempts a date beyond FULL_RETRY_HORIZON_DAYS gets. It's still
# fetched, parsed, logged, and eligible to alert — just with one attempt
# instead of three, since a timeout there is expected, not a transient
# fault. It regains the full retry budget once it comes inside the
# horizon on some later run.
SPECULATIVE_ATTEMPTS = 1

# Reactive backstop for when the static FULL_RETRY_HORIZON_DAYS assumption
# turns out wrong for a given run (TPE/GWR having a bad day, or the real
# fare-release horizon being closer than the 168-day estimate above
# assumes). Once
# this many dates in a row fail, stop submitting any further dates rather
# than spending retry budget on every remaining date of the school year.
# Counted strictly in ascending travel-date order on the main thread as
# results are finalized (see main()'s continuous scheduler), regardless
# of the order dates actually complete in, so this behaves exactly like a
# serial run over `candidates` would. Work already in flight when the
# threshold is hit is still allowed to finish and is still finalized —
# logged, added to results, eligible to alert — nothing already scraped
# is thrown away (docs/plans/003-scheduler-and-retry-horizon.md §4.3). A
# run that succeeds on some dates and then hits this cutoff still alerts
# on whatever it found, and nothing is lost long-term: the next run
# re-derives the candidate list from scratch, still bounded only by
# term_dates.LAST_KNOWN_DATE.
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
    travel_date: date, attempts: int = 3
) -> dict[str, TrainOption | None] | Exception:
    """Runs in a worker thread: one full scrape + parse for a single
    travel date. Returns the outcome instead of raising/returning, so
    main()'s scheduler can harvest it whenever it completes and finalize
    it later, strictly in ascending travel-date order, regardless of
    completion order.
    """
    try:
        raw = scraper.fetch_journey_search(
            travel_date, artifacts_dir=ARTIFACTS_DIR, attempts=attempts
        )
    except scraper.ScraperError as exc:
        return exc
    try:
        options = parser.parse_journeys(raw, travel_date)
    except parser.ParseError as exc:
        return exc
    return parser.select_target_trains(options, config.TARGET_DEPARTURES)


def _date_rows(
    travel_dates: Iterable[date], results: dict[date, dict[str, TrainOption | None]]
) -> list[DateRow]:
    """DateRows for `travel_dates`, always in ascending date order (the
    order both email tables are rendered in — see src/notifier.py)."""
    return [DateRow(travel_date=d, options=results[d]) for d in sorted(travel_dates)]


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


def _log_stopped_early(
    last_failed_date: date, remaining_count: int, in_flight_count: int
) -> None:
    logger.warning(
        "[%s] %d consecutive dates failed — stopping early; this suggests "
        "either TPE's fare-release window has moved closer than "
        "FULL_RETRY_HORIZON_DAYS (%d days) assumes, or TPE is unavailable; "
        "%d further candidate date(s) were not attempted; %d already in "
        "flight will be allowed to finish and are still logged",
        last_failed_date.isoformat(),
        MAX_CONSECUTIVE_FAILURES,
        FULL_RETRY_HORIZON_DAYS,
        remaining_count,
        in_flight_count,
    )


def _log_target_summary(travel_date: date, targets: dict[str, TrainOption | None]) -> None:
    for departure_time, option in targets.items():
        if option is None:
            logger.info("[%s] %s: not found", travel_date.isoformat(), departure_time)
        elif option.sold_out:
            logger.info("[%s] %s: sold out", travel_date.isoformat(), departure_time)
        else:
            logger.info("[%s] %s: %s", travel_date.isoformat(), departure_time, option.price)


# The candidate most likely to spend the full 3-attempt budget and still
# fail is the latest one still inside FULL_RETRY_HORIZON_DAYS: nearer
# dates reliably succeed on attempt 1, and dates past the horizon only
# ever get SPECULATIVE_ATTEMPTS regardless. Dispatching it first overlaps
# its ~51s worst case with the bulk of the run instead of appending it to
# the tail. Zone-gated so that short candidate lists (a manual max_dates
# run, or the last weeks of the school year) are left in plain ascending
# order — reordering only kicks in when there really is a boundary date.
# The moved date is always attempted, even if the early stop would
# otherwise have prevented it — one extra doomed date's worth of work,
# running concurrently with useful work, so ~0 wall clock.
# Set to 0 to disable the reordering entirely.
BOUNDARY_PRIORITY_ZONE_DAYS = 7


def _dispatch_order(candidates: list[date], full_retry_until: date) -> list[int]:
    """Indices into `candidates`, in the order they should be submitted.

    A permutation of range(len(candidates)) — dispatch order only; every
    result is still finalized in ascending date order (see main()).
    """
    zone_start = full_retry_until - timedelta(days=BOUNDARY_PRIORITY_ZONE_DAYS)
    boundary = [i for i, d in enumerate(candidates) if zone_start < d <= full_retry_until]
    first = boundary[-1:]  # at most one; [] when the zone is empty
    return first + [i for i in range(len(candidates)) if i not in set(first)]


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

    # Dates this far out or closer get the full retry budget; dates beyond
    # it get SPECULATIVE_ATTEMPTS (see FULL_RETRY_HORIZON_DAYS above).
    full_retry_until = today + timedelta(days=FULL_RETRY_HORIZON_DAYS)

    # Continuous queue scheduler (docs/plans/003-scheduler-and-retry-
    # horizon.md §4.2): a rolling window of up to PARALLEL_DATES in-flight
    # scrapes, refilled the instant any one finishes, rather than fixed
    # batches that leave idle workers behind a single straggler. Dispatch
    # order (`order`) and finalization order are deliberately decoupled —
    # dispatch is free to reorder (see _dispatch_order below), but
    # finalization — failure counting, price-log writes, alert
    # eligibility — always happens strictly in ascending candidate-index
    # (i.e. travel-date) order, via the `completed` reorder buffer, so
    # MAX_CONSECUTIVE_FAILURES behaves exactly like a serial run would.
    order = _dispatch_order(candidates, full_retry_until)
    cursor = 0
    submitted: set[int] = set()
    in_flight: dict[Future, int] = {}
    completed: dict[int, dict[str, TrainOption | None] | Exception] = {}
    next_to_finalize = 0
    stop_submitting = False

    with ThreadPoolExecutor(max_workers=PARALLEL_DATES) as executor:
        while True:
            # 1. REFILL — submit until the window is full or nothing's left.
            while (
                not stop_submitting
                and len(in_flight) < PARALLEL_DATES
                and cursor < len(order)
            ):
                idx = order[cursor]
                cursor += 1
                travel_date = candidates[idx]
                attempts = 3 if travel_date <= full_retry_until else SPECULATIVE_ATTEMPTS
                in_flight[executor.submit(_fetch_and_parse_one, travel_date, attempts)] = idx
                submitted.add(idx)

            # 2. DONE?
            if not in_flight:
                break

            # 3. HARVEST — block until at least one completes, take all
            # that have (wait() returns every already-done future, not
            # just one; snapshot the keys since harvest mutates in_flight).
            done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                completed[in_flight.pop(future)] = future.result()

            # 4. FINALIZE — strictly in ascending candidate-index order.
            while True:
                if next_to_finalize in completed:
                    idx = next_to_finalize
                    next_to_finalize += 1
                    travel_date = candidates[idx]
                    outcome = completed.pop(idx)

                    if isinstance(outcome, (scraper.BlockedError, scraper.HijackedError)):
                        logger.error(
                            "[%s] %s — aborting the whole run, not scheduling "
                            "further dates: %s",
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
                            if not stop_submitting:
                                remaining = len(order) - cursor
                                _log_stopped_early(travel_date, remaining, len(in_flight))
                            stop_submitting = True
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
                elif (
                    stop_submitting
                    and next_to_finalize not in submitted
                    and next_to_finalize < len(candidates)
                ):
                    # Never submitted and never will be — skip over it so
                    # later already-completed results (e.g. a priority-
                    # dispatched date, see _dispatch_order) can still be
                    # finalized instead of being stranded behind this gap.
                    next_to_finalize += 1
                else:
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

    # The cheap table is derived from `matches` themselves, not
    # re-computed from a threshold comparison, so the table can never
    # disagree with the gate that decided to send this email at all —
    # including in the TEST_RUN fallback case, where the "matches" are
    # deliberately not below threshold (see _best_effort_matches_for_test).
    # Every match came from `alertable_results`, so its date is unbooked
    # by construction.
    cheap_rows = _date_rows({m.travel_date for m in matches}, results)
    # Not threshold-gated and not derived from `matches`: every booked
    # date that was actually scraped this run, purely for information.
    booked_rows = _date_rows([d for d in results if d in booked], results)

    try:
        notifier.send_alert(
            cheap_rows,
            secrets,
            booked_rows=booked_rows,
            test_summary=is_test_summary,
            dry_run=False,
        )
    except notifier.NotifierError as exc:
        logger.error("failed to send alert email: %s", exc)
        return 1

    if is_test_summary:
        logger.info(
            "sent test summary email (real scraped data, not a genuine alert); "
            "%d booked date(s) also shown", len(booked_rows)
        )
    else:
        logger.info(
            "sent alert for %d fare(s) across %d date(s); %d booked date(s) also shown",
            len(matches), len(cheap_rows), len(booked_rows),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
