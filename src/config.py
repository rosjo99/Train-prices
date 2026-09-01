"""Module-level configuration constants and environment-var reading.

Import-time behaviour is intentionally cheap and side-effect-free: nothing
in this module reads secrets or hits the network at import. Secrets are
only read (and only raise) when `get_secrets()` is called explicitly, so
that `import src.config` never fails in a context with no env vars set
(e.g. a plain `pytest` collection run).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    """Raised for missing or malformed configuration."""


# --- Route -------------------------------------------------------------

ORIGIN_NAME = "Oxford"
DESTINATION_NAME = "London Paddington"

# National Rail Enquiries CRS (station) codes. Confirmed live on
# 2026-08-31 via a one-off probe script (since deleted as dead research
# code once its findings were implemented here — see git history if
# needed): navigating to
# JOURNEY_PLANNER_URL_TEMPLATE below with these two codes returned a
# results page showing "Oxford to London Paddington" and real journeys
# (see JOURNEY_PLANNER_API_HOST's comment for the fare evidence).
ORIGIN_CRS = "OXF"
DESTINATION_CRS = "PAD"

TARGET_DEPARTURES: tuple[str, ...] = ("07:25", "07:30")

# --- Alerting ------------------------------------------------------------

PRICE_THRESHOLD = Decimal("10.00")

# Confirmed live on 2026-08-31 via a one-off probe script (since deleted
# as dead research code once its findings were implemented here — see
# git history if needed): passing railcards=YNG|1 in the deep-link URL
# produced a real fare response whose
# jpservices.nationalrail.co.uk/journey-planner JSON contained, per fare,
# a "railcardFares" array entry with "code": "YNG" and a discounted
# "prices.adult" distinct from that fare's own "undiscountedPrices" — e.g.
# one Advance Single fare had undiscountedPrices.adult=4650 (pence) and a
# railcardFares entry {"code": "YNG", "count": 1, "prices": {"adult": 3060}}.
# This is the positive, structured confirmation CLAUDE.md requires before
# any alert can be sent (see src/parser.py, not yet written, for where
# this gets checked per-fare). "YNG" was the original hypothesis carried
# over from the abandoned Trainline attempt and turned out to be correct
# for NRE too — a coincidence, not carried-over evidence.
RAILCARD_CODE = "YNG"

# Confirmed live on 2026-08-31 via a one-off probe script (since deleted
# as dead research code once its findings were implemented here — see
# git history if needed): this exact query-string shape, with the CRS
# codes/railcard code above
# substituted in, loaded straight into a real results page — no click
# needed — showing "07:25 journey from Oxford to London Paddington" and
# "07:30 journey from Oxford to London Paddington" each priced at
# "Single from £30.60", and made a same-origin XHR to
# JOURNEY_PLANNER_API_HOST carrying the full fare JSON (see
# RAILCARD_CODE's comment above for the discount evidence in that JSON).
# No DOB or passenger name is needed, unlike Trainline's abandoned
# RESULTS_URL_TEMPLATE — "adults=1" is sufficient. `leaving_date` is
# DDMMYY (NRE's own format, confirmed from a real example URL), not ISO.
JOURNEY_PLANNER_URL_TEMPLATE = (
    "https://www.nationalrail.co.uk/journey-planner/"
    "?type=single&origin={origin_crs}&destination={destination_crs}"
    "&leavingType=departing&leavingDate={leaving_date}"
    "&leavingHour={leaving_hour}&leavingMin={leaving_minute}"
    "&adults=1&railcards={railcard_code}%7C1&extraTime=0#O"
)


def build_journey_planner_url(travel_date: date, hour: str, minute: str) -> str:
    """Build a deep-link journey-planner URL for a specific date/time.

    Single source of truth for JOURNEY_PLANNER_URL_TEMPLATE's formatting,
    shared by src.scraper (anchored at the earliest of TARGET_DEPARTURES,
    so one fetch's results cover every target train) and src.notifier
    (anchored at each alerted train's own departure time, for the
    email's per-fare link).
    """
    return JOURNEY_PLANNER_URL_TEMPLATE.format(
        origin_crs=ORIGIN_CRS,
        destination_crs=DESTINATION_CRS,
        leaving_date=travel_date.strftime("%d%m%y"),
        leaving_hour=hour,
        leaving_minute=minute,
        railcard_code=RAILCARD_CODE,
    )

# The same-origin API NRE's own journey-planner page calls to fetch fares
# (confirmed live on 2026-08-31): a request to this host, made by the page
# itself after JOURNEY_PLANNER_URL_TEMPLATE loads, returns the structured
# JSON src.scraper captures — no DataDome/CAPTCHA gate, unlike Trainline
# (see CLAUDE.md's Tech decisions for the full comparison).
JOURNEY_PLANNER_API_HOST = "jpservices.nationalrail.co.uk"

# The specific endpoint on JOURNEY_PLANNER_API_HOST that returns the
# outwardJourneys/fares payload this scraper needs. The same host also
# serves sibling endpoints (observed live: "/fare-info") for other data
# the page loads — src.scraper's response handler must match on this path
# too, not just the host, or it can capture the wrong endpoint's response
# whenever that sibling happens to reply after the real one.
JOURNEY_PLANNER_API_PATH = "/journey-planner"

# Every page on nationalrail.co.uk loads third-party ad/tracking scripts,
# one of which was observed (during interactive UI-driven probing, not the
# deep-link approach this scraper actually uses) redirecting the whole tab
# to a Booking.com hotel search. The deep-link approach never triggered
# this in any probe run, but src.scraper still guards against it — see
# its NRE_HOST_SUFFIX usage — as cheap defense in depth for an unattended
# daily job, since the underlying ad ecosystem is outside our control.
NRE_HOST_SUFFIX = "nationalrail.co.uk"

# --- Time ------------------------------------------------------------

LONDON = ZoneInfo("Europe/London")

# --- Booked dates ------------------------------------------------------

# Committed file at a fixed repo-relative path, read by a later task's
# booked_dates.load_booked_dates(). Not env-configurable. Can also be
# edited via the GitHub Pages site in site/ — see README.md.
BOOKED_DATES_PATH = Path("booked-dates.txt")

# --- Price history log ---------------------------------------------------

# Committed, append-only CSV of every price ever checked (see
# src/price_log.py) — never overwritten, so this is a running history,
# not a snapshot. The workflow commits this file back to the repo after
# each run (see .github/workflows/price-check.yml).
PRICE_LOG_PATH = Path("price-history.csv")


def _read_max_dates() -> int | None:
    # Empty/absent means uncapped (e.g. a local shell run with MAX_DATES
    # unset). "all" is a second, explicit way to say the same thing,
    # needed because GitHub's own "Run workflow" web UI re-substitutes a
    # workflow_dispatch input's default value whenever the field is
    # cleared to blank before submitting — there is no way to actually
    # submit an empty override from that UI, only from the API or CLI.
    # See .github/workflows/price-check.yml's max_dates input.
    raw = os.environ.get("MAX_DATES", "").strip()
    if not raw or raw.lower() == "all":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"MAX_DATES must be a positive integer, got {raw!r}"
        ) from None
    if value <= 0:
        raise ConfigError(
            f"MAX_DATES must be a positive integer, got {raw!r}"
        )
    return value


def _read_bool_env(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in ("1", "true", "yes")


# Used only to cap the candidate list for manual/debug runs; never set by
# the scheduled cron run.
MAX_DATES: int | None = _read_max_dates()

# Set automatically by price-check.yml purely from
# `github.event_name == 'workflow_dispatch'` — there's no separate user
# toggle, deliberately, per explicit request for one single "run it
# manually" test rather than a checkbox to pick a combination from.
#
# TEST_RUN makes main() always send a genuine email through the real
# notifier using REAL scraped data — if nothing is actually below
# threshold, it reports the cheapest real fare found instead of staying
# silent, so a manual run always exercises scraping, the CSV log, and
# Resend delivery end to end. See src/main.py's
# `_best_effort_matches_for_test`.
TEST_RUN: bool = _read_bool_env("TEST_RUN")


@dataclass(frozen=True)
class Secrets:
    # repr=False so the API key never appears in a repr()/log line by
    # accident (e.g. an uncaught exception printing a Secrets instance).
    resend_api_key: str = field(repr=False)
    # One address, or several comma-separated (e.g. "a@x.com, b@y.com") —
    # split into a list at send time by src.notifier._parse_recipients.
    email_to: str
    email_from: str


def get_secrets() -> Secrets:
    """Read required secrets from the environment.

    Raises ConfigError listing every missing variable at once. Never
    include a secret value in the exception message.
    """
    resend_api_key = os.environ.get("RESEND_API_KEY", "").strip()
    email_to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    email_from = os.environ.get("ALERT_EMAIL_FROM", "").strip() or (
        "Train Alerts <onboarding@resend.dev>"
    )

    missing = []
    if not resend_api_key:
        missing.append("RESEND_API_KEY")
    if not email_to:
        missing.append("ALERT_EMAIL_TO")

    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    return Secrets(
        resend_api_key=resend_api_key,
        email_to=email_to,
        email_from=email_from,
    )
