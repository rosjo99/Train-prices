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

# TransPennine Express (TPE) CRS (station) codes — TPE's own booking
# engine accepts standard National Rail CRS codes directly in its
# deep-link URL and resolves them to its own internal station IDs itself
# (confirmed live 2026-09-01: navigating to JOURNEY_PLANNER_URL_TEMPLATE
# below triggered the page's own GET /config/stations?search=OXF/PAD
# calls before its journey-plan POST — see src/scraper.py's docstring for
# why TPE, not NRE).
ORIGIN_CRS = "OXF"
DESTINATION_CRS = "PAD"

TARGET_DEPARTURES: tuple[str, ...] = ("07:25", "07:30")

# --- Alerting ------------------------------------------------------------

PRICE_THRESHOLD = Decimal("10.00")

# Confirmed live 2026-09-01 (GitHub Actions run 33527007099, capturing
# the real /jp/journey-plan response via scripts/capture_fixture_tpe.py —
# see tests/fixtures/journey_plan_sample.json): with "YNG" requested as
# the railcard, every priced fare's own "totalPrice" already has the
# discount applied (TPE returns one price per fare, not NRE's separate
# undiscounted/railcard-discounted pair) — e.g. one Advance Single fare
# had originalTotalPrice=1400 (pence) and totalPrice=930, with its
# tickets[0].railcard pointing at "/data/railcards/YNG" and
# railcardDiscount=470. "YNG" is unchanged from the NRE-era value — a
# coincidence confirmed independently for TPE, not carried-over evidence.
RAILCARD_CODE = "YNG"

# TPE's grid frontend only returns a small, fixed number of the next
# journeys after the anchor time (confirmed live 2026-09-01: its own
# POST body set "numJourneys": 3, not configurable via this deep-link's
# query string). Anchoring exactly at the earliest target departure risks
# an earlier direct service using up a "slot" before it — confirmed live:
# anchoring at 07:00 returned 07:02, 07:16, 07:25, pushing the 07:30
# departure out of the window entirely. Anchoring a few minutes before
# the earliest target keeps that gap small enough to avoid it — anchoring
# at 07:20 returned 07:25, 07:30, 07:53, comfortably covering both target
# departures in one fetch. Not foolproof (a same-route service in that
# 5-minute gap on some other day could still push a target out), but no
# worse than the single-anchor approach it replaces, and this is the only
# lever this deep-link exposes.
ANCHOR_OFFSET_MINUTES = 5

# Confirmed live 2026-09-01 (same run as RAILCARD_CODE's comment above):
# this exact URL shape — with the CRS codes above, an ISO "YYYY-MM-DD"
# date (not NRE's DDMMYY), and "{railcard_code}x1" — loaded straight into
# a real results grid, no click needed, and made same-origin fetch calls
# including a POST to JOURNEY_PLANNER_API_HOST carrying the full fare
# JSON src.scraper captures (see JOURNEY_PLANNER_API_HOST's comment).
# "adults=1" is expressed as the "1" path segment; TPE's frontend
# resolves the CRS codes to its own internal station IDs itself, so
# nothing here needs to know those IDs.
JOURNEY_PLANNER_URL_TEMPLATE = (
    "https://ticket.tpexpress.co.uk/journeys-grid/"
    "{origin_crs}/{destination_crs}/{leaving_date}T{leaving_hour}:{leaving_minute}"
    "//1//{railcard_code}x1"
    "?departNow=no&realTime=no&searchPreferences=%2C%2C%2C%2Ctrue"
    "&showAdditionalRoutes=no&showCheapest=no&tocSpecific=no"
)


def build_journey_planner_url(travel_date: date, hour: str, minute: str) -> str:
    """Build a deep-link journeys-grid URL for a specific date/time.

    Single source of truth for JOURNEY_PLANNER_URL_TEMPLATE's formatting,
    shared by src.scraper (anchored a few minutes before the earliest of
    TARGET_DEPARTURES — see ANCHOR_OFFSET_MINUTES — so one fetch's
    results cover every target train) and src.notifier (anchored at each
    alerted train's own departure time, for the email's per-fare link).
    """
    return JOURNEY_PLANNER_URL_TEMPLATE.format(
        origin_crs=ORIGIN_CRS,
        destination_crs=DESTINATION_CRS,
        leaving_date=travel_date.isoformat(),
        leaving_hour=hour,
        leaving_minute=minute,
        railcard_code=RAILCARD_CODE,
    )

# The same-origin API TPE's own journeys-grid page calls to fetch fares
# (confirmed live 2026-09-01): a POST to this host, made by the page
# itself after JOURNEY_PLANNER_URL_TEMPLATE loads, returns the structured
# JSON src.scraper captures — no bot-protection gate observed across
# either the diagnostic Camoufox probe or the real fixture-capture run
# (see CLAUDE.md's Tech decisions).
JOURNEY_PLANNER_API_HOST = "api.tpexpress.co.uk"

# The specific endpoint on JOURNEY_PLANNER_API_HOST that returns the
# outward-journeys/fares payload this scraper needs. The same host also
# serves sibling endpoints (observed live: "/jp/plusbus") for other data
# the page loads — src.scraper's response handler must match on this path
# too, not just the host, or it can capture the wrong endpoint's response
# whenever that sibling happens to reply after the real one.
JOURNEY_PLANNER_API_PATH = "/jp/journey-plan"

# Every page on tpexpress.co.uk (www., ticket., and api. subdomains all
# end in this suffix) loads third-party scripts (Usercentrics CMP, Google
# Maps, PayPal). None were observed redirecting the tab away during any
# probe or capture run, but src.scraper still guards against it — see its
# TPE_HOST_SUFFIX usage — as cheap defense in depth for an unattended
# daily job, since the ad/tracking ecosystem on the page is outside our
# control.
TPE_HOST_SUFFIX = "tpexpress.co.uk"

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
