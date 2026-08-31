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
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    """Raised for missing or malformed configuration."""


# --- Route -------------------------------------------------------------

ORIGIN_NAME = "Oxford"
DESTINATION_NAME = "London Paddington"

# TODO(task-3): fill in with the real station URNs discovered by driving
# the Trainline search form once, per plan docs/plans/001-train-price-alert.md
# Task 3.
ORIGIN_URN: str | None = None
DESTINATION_URN: str | None = None

TARGET_DEPARTURES: tuple[str, ...] = ("07:25", "07:30")

# --- Alerting ------------------------------------------------------------

PRICE_THRESHOLD = Decimal("10.00")

# Hypothesis only — Task 3 must confirm this is the correct Trainline
# railcard code for the 16-25 Railcard before it's relied on.
RAILCARD_CODE = "YNG"

# Date of birth used for the passenger, chosen to sit inside the 16-25
# railcard eligibility window (16th to 30th birthday) as of the target
# travel dates (school terms through 2027-07-08). NOTE: revisit this if
# the tool is still running past 2028 — by then this DOB will have aged
# out of the 16-25 window.
PASSENGER_DOB = "2003-01-01"

# --- Time ------------------------------------------------------------

LONDON = ZoneInfo("Europe/London")

# --- Booked dates ------------------------------------------------------

# Committed file at a fixed repo-relative path, read by a later task's
# booked_dates.load_booked_dates(). Not env-configurable.
BOOKED_DATES_PATH = Path("booked-dates.txt")


def _read_max_dates() -> int | None:
    raw = os.environ.get("MAX_DATES", "").strip()
    if not raw:
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


def _read_dry_run() -> bool:
    raw = os.environ.get("DRY_RUN", "").strip().lower()
    return raw in ("1", "true", "yes")


# Used only to cap the candidate list for manual/debug runs; never set by
# the scheduled cron run.
MAX_DATES: int | None = _read_max_dates()

DRY_RUN: bool = _read_dry_run()


@dataclass(frozen=True)
class Secrets:
    # repr=False so the API key never appears in a repr()/log line by
    # accident (e.g. an uncaught exception printing a Secrets instance).
    resend_api_key: str = field(repr=False)
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
