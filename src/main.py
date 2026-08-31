"""Orchestrates a full daily price-check run: enumerate candidate travel
dates, scrape and parse each one, decide whether any fare beats the
alert threshold, and send (or dry-run print) an email if so.

See docs/plans/001-train-price-alert.md Task 6 for the full spec this
implements.
"""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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


def _load_secrets_for_run() -> config.Secrets:
    """Load real secrets — except in DRY_RUN, where a missing/incomplete
    environment must not stop the run (the whole point of dry-run is to
    exercise the pipeline without needing them). If real secrets happen
    to be set anyway, they're used (so DRY_RUN's printed email reflects
    the real recipient); otherwise a clearly-labelled placeholder stands
    in, since notifier.send_alert's dry-run path still reads
    secrets.email_to/email_from to print them.
    """
    if not config.DRY_RUN:
        return config.get_secrets()
    try:
        return config.get_secrets()
    except config.ConfigError:
        return config.Secrets(
            resend_api_key="(dry-run: RESEND_API_KEY not set)",
            email_to="(dry-run: ALERT_EMAIL_TO not set)",
            email_from="Train Alerts <onboarding@resend.dev>",
        )


def evaluate(
    targets_by_date: dict[date, dict[str, TrainOption | None]],
) -> tuple[list[AlertMatch], bool]:
    """Pure decision function: which TrainOptions beat the price
    threshold, and whether any priced option's railcard discount
    couldn't be confirmed.

    Returns (matches, railcard_unconfirmed). A single list[AlertMatch]
    return (as docs/plans/001-train-price-alert.md Task 6 shows in its
    signature sketch) can't also carry the railcard_unconfirmed signal
    that step 7 of the same task needs — this returns both explicitly
    rather than smuggling a flag onto the list.

    A match requires price is not None, currency == "GBP",
    railcard_applied, and price < config.PRICE_THRESHOLD (strictly
    less-than — exactly the threshold does not alert). Any option with a
    price but railcard_applied is False sets railcard_unconfirmed, which
    the caller treats as reason to send no email at all, even if other,
    genuinely-confirmed matches exist elsewhere in the same run —
    CLAUDE.md: a wrong price in an alert is worse than a missed alert.
    """
    matches: list[AlertMatch] = []
    railcard_unconfirmed = False
    seen: set[tuple[date, str]] = set()

    for travel_date, targets in targets_by_date.items():
        for option in targets.values():
            if option is None:
                continue
            if option.price is not None and not option.railcard_applied:
                railcard_unconfirmed = True
                continue
            if not (
                option.price is not None
                and option.currency == "GBP"
                and option.railcard_applied
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

    return matches, railcard_unconfirmed


def _send_test_email() -> int:
    """SEND_TEST_EMAIL path: send one synthetic alert through the real
    notifier, no scraping at all, to positively confirm email delivery
    works. Always a real send (never dry-run) regardless of config.DRY_RUN
    — the whole point is confirming Resend actually delivers.
    """
    logger.info("SEND_TEST_EMAIL set — sending a single synthetic test alert, no scraping")
    secrets = config.get_secrets()
    travel_date = datetime.now(config.LONDON).date() + timedelta(days=1)
    test_option = TrainOption(
        travel_date=travel_date,
        departure_time="07:25",
        arrival_time="08:26",
        price=Decimal("7.77"),
        currency="GBP",
        railcard_applied=True,
        is_direct=True,
        sold_out=False,
        fare_name="TEST EMAIL — not a real fare, please ignore",
    )
    match = AlertMatch(travel_date=travel_date, option=test_option, threshold=config.PRICE_THRESHOLD)
    try:
        notifier.send_alert([match], secrets, dry_run=False)
    except notifier.NotifierError as exc:
        logger.error("test email failed to send: %s", exc)
        return 1
    logger.info("test email sent to %s — check the inbox", secrets.email_to)
    return 0


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

    if config.SEND_TEST_EMAIL:
        return _send_test_email()

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

    secrets = _load_secrets_for_run()

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

    matches, railcard_unconfirmed = evaluate(results)

    if railcard_unconfirmed:
        logger.error(
            "at least one priced fare could not have its railcard discount "
            "positively confirmed — sending no email (a wrong price in an "
            "alert is worse than a missed alert)"
        )
        return 1

    if not matches:
        logger.info("no fares below threshold — nothing to alert on")
        return 0

    try:
        notifier.send_alert(matches, secrets, dry_run=config.DRY_RUN)
    except notifier.NotifierError as exc:
        logger.error("failed to send alert email: %s", exc)
        return 1

    logger.info("sent alert for %d match(es)", len(matches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
