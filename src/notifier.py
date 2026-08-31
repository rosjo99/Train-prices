"""Sends a price-alert email via Resend's HTTPS API.

Receives secrets (config.Secrets) but must never let one reach an
exception message or a log line — see _redact() and every place a
response body gets logged or wrapped in NotifierError.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import requests

from src import config
from src.models import AlertMatch

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
REQUEST_TIMEOUT_SECONDS = 20

# "Retries twice" per the plan: 2 retries after an initial attempt, 3
# total. Mirrors src.scraper's shape (a module-level backoff tuple,
# monkeypatched via time.sleep in tests rather than actually waiting).
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (5, 15)

# Retryable HTTP statuses: Resend rate-limiting (429) and any 5xx. A 4xx
# other than 429 (bad key, malformed request, etc.) is not retryable —
# hammering a request that will never succeed just delays the failure.
RETRYABLE_STATUSES_MIN_5XX = 500

MAX_TABLE_ROWS = 20


class NotifierError(Exception):
    """Raised when the alert email could not be sent after retries (or
    immediately, for a non-retryable failure). Never contains the API
    key — see _redact().
    """


def _redact(text: str, secret: str) -> str:
    """Scrub `secret` out of `text` before it can reach a log line or an
    exception message. Safe to call even if `secret` is falsy/empty.
    """
    if not text or not secret:
        return text
    return text.replace(secret, "***REDACTED***")


def _parse_recipients(email_to: str) -> list[str]:
    """ALERT_EMAIL_TO holds one or more comma-separated addresses (e.g.
    "a@example.com, b@example.com") — split, strip, and drop empties so a
    stray trailing comma doesn't send an empty "to" entry to Resend.
    """
    return [addr.strip() for addr in email_to.split(",") if addr.strip()]


def _format_price(price: Decimal) -> str:
    # Decimal's own __format__ handles the 'f' presentation type
    # natively, with no conversion to a binary float anywhere in the
    # path, so a Decimal("8.7") renders as "8.70" exactly.
    return f"£{price:.2f}"


def _format_date(d) -> str:
    return d.strftime("%a %d %b")


def _build_link(match: AlertMatch) -> str:
    hour, minute = match.option.departure_time.split(":")
    return config.build_journey_planner_url(match.travel_date, hour, minute)


def _build_subject(ordered_matches: list[AlertMatch]) -> str:
    cheapest = ordered_matches[0]
    subject = (
        f"Cheap train: {config.ORIGIN_NAME} → {config.DESTINATION_NAME} "
        f"{_format_price(cheapest.option.price)} on {_format_date(cheapest.travel_date)}"
    )
    extra = len(ordered_matches) - 1
    if extra > 0:
        subject += f" (+{extra} more)"
    return subject


def _build_text_body(ordered_matches: list[AlertMatch]) -> str:
    shown = ordered_matches[:MAX_TABLE_ROWS]
    lines = [
        f"Cheap fares found for {config.ORIGIN_NAME} → {config.DESTINATION_NAME}:",
        "",
    ]
    for match in shown:
        option = match.option
        changes = "Direct" if option.is_direct else "Changes"
        arrival = option.arrival_time or "?"
        railcard_note = "16-25 Railcard" if option.railcard_applied else "16-25 Railcard NOT confirmed"
        lines.append(
            f"{_format_date(match.travel_date)} {option.departure_time} -> {arrival}  "
            f"{_format_price(option.price)}  ({changes}, {railcard_note})  {_build_link(match)}"
        )
    extra = len(ordered_matches) - len(shown)
    if extra > 0:
        lines.append(f"(+{extra} more not shown)")
    return "\n".join(lines)


def _html_row(match: AlertMatch) -> str:
    option = match.option
    changes = "Direct" if option.is_direct else "Changes"
    arrival = option.arrival_time or "?"
    railcard_note = "Yes" if option.railcard_applied else "Not confirmed"
    link = _build_link(match)
    return (
        "<tr>"
        f"<td>{_format_date(match.travel_date)}</td>"
        f"<td>{option.departure_time}</td>"
        f"<td>{arrival}</td>"
        f"<td>{_format_price(option.price)}</td>"
        f"<td>{changes}</td>"
        f"<td>{railcard_note}</td>"
        f'<td><a href="{link}">Book</a></td>'
        "</tr>"
    )


def _build_html_body(ordered_matches: list[AlertMatch]) -> str:
    shown = ordered_matches[:MAX_TABLE_ROWS]
    rows = "".join(_html_row(m) for m in shown)
    extra = len(ordered_matches) - len(shown)
    extra_html = f"<p>+{extra} more not shown</p>" if extra > 0 else ""
    return (
        "<html><body>"
        f"<h2>Cheap fares — {config.ORIGIN_NAME} → {config.DESTINATION_NAME}</h2>"
        "<table border=\"1\" cellpadding=\"4\" cellspacing=\"0\">"
        "<tr><th>Date</th><th>Departs</th><th>Arrives</th><th>Price</th>"
        "<th>Direct?</th><th>16-25 Railcard</th><th>Link</th></tr>"
        f"{rows}"
        "</table>"
        f"{extra_html}"
        "</body></html>"
    )


def send_alert(
    matches: list[AlertMatch], secrets: config.Secrets, *, dry_run: bool = False
) -> None:
    """Send (or, in dry-run, print) a price-alert email for `matches`.

    Raises ValueError if `matches` is empty — the caller must not call
    this with nothing to say. Raises NotifierError if the email could
    not be sent after retries (or immediately, for a non-retryable
    failure like a bad API key).
    """
    if not matches:
        raise ValueError("send_alert called with no matches — nothing to alert about")

    ordered = sorted(matches, key=lambda m: m.option.price)
    subject = _build_subject(ordered)
    text_body = _build_text_body(ordered)
    html_body = _build_html_body(ordered)
    recipients = _parse_recipients(secrets.email_to)

    if dry_run:
        print("=== DRY RUN: email not sent ===")
        print(f"To: {', '.join(recipients)}")
        print(f"From: {secrets.email_from}")
        print(f"Subject: {subject}")
        print()
        print(text_body)
        return

    # requests serialises `json=` as UTF-8 automatically (via its own
    # json.dumps + explicit utf-8 encode), so the £/-> characters in the
    # body need no manual encoding here. Resend accepts "to" as either a
    # single string or a list of up to 50 addresses — always sending a
    # list here means one or many recipients need no special-casing.
    payload = {
        "from": secrets.email_from,
        "to": recipients,
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    headers = {"Authorization": f"Bearer {secrets.resend_api_key}"}

    last_exc: NotifierError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                RESEND_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            message = _redact(f"network error calling Resend: {exc}", secrets.resend_api_key)
            last_exc = NotifierError(message)
            logger.warning("attempt %d/%d: %s", attempt, MAX_ATTEMPTS, message)
        else:
            if 200 <= response.status_code < 300:
                logger.info("alert email sent (status=%d)", response.status_code)
                return

            body_snippet = _redact(response.text[:500], secrets.resend_api_key)
            retryable = response.status_code == 429 or response.status_code >= RETRYABLE_STATUSES_MIN_5XX
            message = (
                f"Resend returned HTTP {response.status_code} "
                f"({'retryable' if retryable else 'not retryable'}): {body_snippet}"
            )
            if not retryable:
                raise NotifierError(message)
            last_exc = NotifierError(message)
            logger.warning("attempt %d/%d: %s", attempt, MAX_ATTEMPTS, message)

        if attempt < MAX_ATTEMPTS:
            delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            time.sleep(delay)

    assert last_exc is not None  # pragma: no cover
    raise last_exc
