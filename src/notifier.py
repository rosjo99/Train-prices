"""Sends a price-alert email via Resend's HTTPS API.

Receives secrets (config.Secrets) but must never let one reach an
exception message or a log line — see _redact() and every place a
response body gets logged or wrapped in NotifierError.

The email is two site-styled tables (cheap-and-unbooked, and already-
booked-with-current-prices), built as inline-styled HTML — no <style>
block anywhere, see _build_html_body — plus a plain-text fallback. See
docs/plans/004-redesign-alert-email.md for the full design.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from decimal import Decimal

import requests

from src import config
from src.models import DateRow, TrainOption

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

# Independent caps: a long booked list must never crowd out the cheap
# fares that are the actual point of the email (booked-dates.txt already
# holds 8 dates and grows through the school year). A row is now one
# travel DATE showing both departures, where it used to be one fare, so
# 25 rows is roughly 50 of the old rows.
MAX_CHEAP_ROWS = 25
MAX_BOOKED_ROWS = 25

SITE_URL = "https://rosjo99.github.io/Train-prices/"

# Copied from site/style.css's :root — the email deliberately mirrors the
# booked-dates website's palette. :hover variants are omitted (hover does
# not exist in email).
C_TEXT = "#1f2328"
C_TEXT_MUTED = "#57606a"
C_BORDER = "#d0d7de"
C_ACCENT = "#1a7f37"
C_BG_MUTED = "#f6f8fa"
C_CHEAP_BG = "#dafbe1"
C_BOOKED_BG = "#ddf4ff"
FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, "
    "Arial, sans-serif"
)

# Fixed-width text-table columns (see _build_text_body). Total line width
# 14 + 11 + 24 + 24 = 73 characters, comfortably inside a 78-column
# terminal or a plain-text mail client.
TEXT_COL_DATE = 14
TEXT_COL_DAY = 11
TEXT_COL_CELL = 24


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


def _format_date(d: date) -> str:
    # Used for the subject line only — see _format_table_date for tables.
    return d.strftime("%a %d %b")


def _format_table_date(d: date) -> str:
    # e.g. "8 Sep 2026". Not strftime("%-d ...") — "%-d" is a glibc
    # extension, not portable. The year is included because the
    # candidate range spans Sep 2026 - Jul 2027 and "8 Sep" alone is
    # ambiguous in an archived email.
    return f"{d.day} {d:%b %Y}"


def _format_weekday(d: date) -> str:
    return d.strftime("%A")  # "Tuesday"


# ---------------------------------------------------------------------------
# Pure row/cell helpers — deliberately split so the HTML and plain-text
# renderers compose the SAME facts into their own presentation, rather
# than one format string-munging the other's output.
# ---------------------------------------------------------------------------


def _option_price_is_cheap(option: TrainOption | None) -> bool:
    """Mirrors src.main.evaluate()'s per-fare test (priced, GBP, strictly
    below threshold) and site/app.js's rowHasCheapFare — used only to
    decide highlighting/bolding, never to decide whether to send."""
    return (
        option is not None
        and option.price is not None
        and option.currency == "GBP"
        and option.price < config.PRICE_THRESHOLD
    )


def _row_is_cheap(row: DateRow) -> bool:
    """True if ANY target departure on this row is cheap — the site's own
    row-highlight rule (site/app.js rowHasCheapFare)."""
    return any(_option_price_is_cheap(option) for option in row.options.values())


def _row_min_price(row: DateRow) -> Decimal:
    """Cheapest priced option on the row; Decimal("Infinity") if none —
    used for ordering/selection only."""
    priced = [option.price for option in row.options.values() if option is not None and option.price is not None]
    return min(priced) if priced else Decimal("Infinity")


def _cell_price_text(option: TrainOption | None) -> str:
    """The primary line of one departure's cell: the price, or why there
    isn't one. Mirrors site/app.js's formatLatestCell on in-memory data
    instead of CSV rows — the site's "no CSV row at all" case has no
    in-memory analogue, since select_target_trains always returns a key
    for every target departure."""
    if option is None:
        return "not found"
    if option.sold_out:
        return "sold out"
    if option.price is None:
        return "–"  # en dash
    return _format_price(option.price)


def _cell_needs_railcard_marker(option: TrainOption | None) -> bool:
    """True when this cell shows a real price that was NOT confirmed as a
    16-25 Railcard fare — rendered as a '*' plus a legend line."""
    return option is not None and option.price is not None and not option.railcard_applied


def _cell_arrival_text(option: TrainOption | None) -> str:
    """"arr 08:26", or "" when there is no option or no arrival time.

    Deliberately still returns a useful value for a SOLD-OUT option: the
    train exists and is in the timetable, it just has no fare. For
    `option is None` there is no timetable entry at all, so this returns
    "" — absence of data is not the same as "no arrival known"."""
    if option is None or not option.arrival_time:
        return ""
    return f"arr {option.arrival_time}"


def _cell_is_indirect(option: TrainOption | None) -> bool:
    """True when this journey is known NOT to be direct. False for a
    missing option — absence of data is not a claim about changes."""
    return option is not None and not option.is_direct


def _select_cheap(rows: list[DateRow]) -> tuple[list[DateRow], int]:
    """Cheapest MAX_CHEAP_ROWS rows (by _row_min_price), returned in
    ascending DATE order, plus the number omitted.

    Selecting by price but displaying by date is deliberate: truncation
    must never drop the cheapest date (the one the subject line names),
    while the table itself stays chronological like the website's, since
    these are travel dates a human is choosing between."""
    by_price = sorted(rows, key=_row_min_price)
    kept = by_price[:MAX_CHEAP_ROWS]
    omitted = len(rows) - len(kept)
    return sorted(kept, key=lambda r: r.travel_date), omitted


def _select_booked(rows: list[DateRow]) -> tuple[list[DateRow], int]:
    """First MAX_BOOKED_ROWS rows in ascending date order (nearest travel
    dates first — the ones a human still cares about), plus the number
    omitted."""
    ordered = sorted(rows, key=lambda r: r.travel_date)
    kept = ordered[:MAX_BOOKED_ROWS]
    omitted = len(rows) - len(kept)
    return kept, omitted


# ---------------------------------------------------------------------------
# Subject / preheader
# ---------------------------------------------------------------------------


def _build_subject(cheap_rows: list[DateRow]) -> str:
    cheapest_row = min(cheap_rows, key=lambda r: (_row_min_price(r), r.travel_date))
    subject = (
        f"Cheap train: {config.ORIGIN_NAME} → {config.DESTINATION_NAME} "
        f"{_format_price(_row_min_price(cheapest_row))} on {_format_date(cheapest_row.travel_date)}"
    )
    extra = len(cheap_rows) - 1
    if extra > 0:
        subject += f" (+{extra} more dates)"
    return subject


def _build_preheader(cheap_rows: list[DateRow]) -> str:
    cheapest_row = min(cheap_rows, key=lambda r: (_row_min_price(r), r.travel_date))
    return (
        f"Cheapest {_format_price(_row_min_price(cheapest_row))} on "
        f"{_format_date(cheapest_row.travel_date)} — {len(cheap_rows)} cheap date(s)."
    )


# ---------------------------------------------------------------------------
# HTML body
# ---------------------------------------------------------------------------


def _html_price_cell_html(row: DateRow, target: str, option: TrainOption | None, *, linked: bool) -> str:
    """The <a>/<span> price line for one departure's cell (line 1 only)."""
    price_text = _cell_price_text(option)
    is_priced = option is not None and option.price is not None and not option.sold_out

    if not is_priced:
        return f'<span style="color:{C_TEXT_MUTED};font-style:italic;">{price_text}</span>'

    marker = f'<span style="color:{C_TEXT_MUTED};"> *</span>' if _cell_needs_railcard_marker(option) else ""

    if linked:
        hour, minute = target.split(":")
        href = config.build_journey_planner_url(row.travel_date, hour, minute)
        if _option_price_is_cheap(option):
            link_style = f"color:{C_ACCENT};font-weight:700;text-decoration:none;"
        else:
            link_style = f"color:{C_TEXT};text-decoration:underline;"
        price_html = f'<a href="{href}" style="{link_style}">{price_text}</a>'
    else:
        color = C_ACCENT if _option_price_is_cheap(option) else C_TEXT
        weight = "font-weight:700;" if _option_price_is_cheap(option) else ""
        price_html = f'<span style="color:{color};{weight}">{price_text}</span>'

    return f'<span style="white-space:nowrap;">{price_html}{marker}</span>'


def _html_detail_line(option: TrainOption | None) -> str:
    """The muted line-2 <div>, or "" when there is nothing to say."""
    arrival = _cell_arrival_text(option)
    indirect = _cell_is_indirect(option)

    if not arrival and not indirect:
        return ""

    changes_span = '<span style="font-weight:600;">changes</span>'
    if arrival and indirect:
        content = f"{arrival} &middot; {changes_span}"
    elif arrival:
        content = arrival
    else:
        content = changes_span

    return f'<div style="margin-top:2px;font-size:12px;line-height:1.3;color:{C_TEXT_MUTED};">{content}</div>'


def _html_cell(row: DateRow, target: str, *, linked: bool, bg: str) -> str:
    option = row.options.get(target)
    price_html = _html_price_cell_html(row, target, option, linked=linked)
    detail_html = _html_detail_line(option)
    return (
        f'<td style="padding:7px 8px;border-bottom:1px solid {C_BORDER};background-color:{bg};'
        f'color:{C_TEXT};white-space:normal;vertical-align:top;">'
        f"{price_html}{detail_html}"
        "</td>"
    )


def _html_row(row: DateRow, *, linked: bool, bg: str) -> str:
    cells = "".join(_html_cell(row, target, linked=linked, bg=bg) for target in config.TARGET_DEPARTURES)
    return (
        f'<tr style="background-color:{bg};">'
        f'<td style="padding:7px 8px;border-bottom:1px solid {C_BORDER};background-color:{bg};'
        f'color:{C_TEXT};white-space:nowrap;vertical-align:top;">{_format_table_date(row.travel_date)}</td>'
        f'<td style="padding:7px 8px;border-bottom:1px solid {C_BORDER};background-color:{bg};'
        f'color:{C_TEXT_MUTED};white-space:nowrap;vertical-align:top;">{_format_weekday(row.travel_date)}</td>'
        f"{cells}"
        "</tr>"
    )


def _html_table(rows: list[DateRow], *, linked: bool, row_bg) -> str:
    """`row_bg` is a callable row -> background colour, since the cheap
    table's tint is derived per-row (_row_is_cheap) while the booked
    table's is constant."""
    headers = "".join(
        f'<th scope="col" style="text-align:left;padding:6px 8px;border-bottom:1px solid {C_BORDER};'
        f'color:{C_TEXT_MUTED};font-size:12px;font-weight:600;text-transform:uppercase;'
        f'letter-spacing:0.03em;">{label}</th>'
        for label in ("Date", "Day", *config.TARGET_DEPARTURES)
    )
    body = "".join(_html_row(row, linked=linked, bg=row_bg(row)) for row in rows)
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;font-family:{FONT_STACK};font-size:14px;">'
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table>"
    )


def _any_shown_needs_railcard_marker(rows: list[DateRow]) -> bool:
    return any(
        _cell_needs_railcard_marker(option)
        for row in rows
        for option in row.options.values()
    )


def _any_shown_is_indirect(rows: list[DateRow]) -> bool:
    return any(_cell_is_indirect(option) for row in rows for option in row.options.values())


def _build_html_body(
    cheap_rows: list[DateRow], booked_rows: list[DateRow], *, test_summary: bool
) -> str:
    shown_cheap, omitted_cheap = _select_cheap(cheap_rows)
    shown_booked, omitted_booked = _select_booked(booked_rows)

    preheader = _build_preheader(cheap_rows)

    banner_html = ""
    if test_summary:
        banner_html = (
            '<tr><td style="padding:8px 20px 0 20px;font-family:'
            f'{FONT_STACK};">'
            f'<p style="margin:0;padding:10px 12px;background-color:{C_BG_MUTED};'
            f'border:1px solid {C_BORDER};border-radius:6px;'
            f'font-size:13px;line-height:1.5;color:{C_TEXT_MUTED};">'
            "Manual test run: nothing is currently below &pound;10.00, so the "
            "cheapest fare found is shown instead."
            "</p></td></tr>"
        )

    cheap_table_html = _html_table(shown_cheap, linked=True, row_bg=lambda r: C_CHEAP_BG if _row_is_cheap(r) else "#ffffff")
    cheap_more_html = (
        f'<p style="margin:8px 0 0 0;font-size:12px;color:{C_TEXT_MUTED};">'
        f"+{omitted_cheap} more cheap date(s) not shown</p>"
        if omitted_cheap > 0
        else ""
    )

    booked_section_html = ""
    if booked_rows:
        booked_table_html = _html_table(shown_booked, linked=False, row_bg=lambda r: C_BOOKED_BG)
        booked_more_html = (
            f'<p style="margin:8px 0 0 0;font-size:12px;color:{C_TEXT_MUTED};">'
            f"+{omitted_booked} more booked date(s) not shown</p>"
            if omitted_booked > 0
            else ""
        )
        booked_section_html = (
            f'<tr><td style="padding:20px 20px 0 20px;font-family:{FONT_STACK};color:{C_TEXT};">'
            f'<h2 style="margin:0 0 8px 0;font-size:15px;font-weight:600;'
            f'border-bottom:1px solid {C_BORDER};padding-bottom:6px;">'
            "Already booked &mdash; current prices</h2>"
            f'<p style="margin:0 0 8px 0;font-size:13px;color:{C_TEXT_MUTED};">'
            "For information only. These dates are still checked every run, but "
            "never trigger an alert.</p>"
            f"{booked_table_html}{booked_more_html}"
            "</td></tr>"
        )

    legend_lines = []
    if _any_shown_needs_railcard_marker(shown_cheap) or _any_shown_needs_railcard_marker(shown_booked):
        legend_lines.append(
            "* cheapest fare found for that train, but not confirmed as a "
            "16-25 Railcard price."
        )
    if _any_shown_is_indirect(shown_cheap) or _any_shown_is_indirect(shown_booked):
        legend_lines.append(
            "&ldquo;changes&rdquo; means that journey is not direct. Everything "
            "else shown is direct."
        )
    legend_html = ""
    if legend_lines:
        legend_html = (
            f'<tr><td style="padding:12px 20px 0 20px;font-family:{FONT_STACK};">'
            f'<p style="margin:0;font-size:12px;line-height:1.5;color:{C_TEXT_MUTED};">'
            + "<br>".join(legend_lines)
            + "</p></td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>Cheap fares: {config.ORIGIN_NAME} → {config.DESTINATION_NAME}</title>
</head>
<body style="margin:0;padding:0;background-color:{C_BG_MUTED};">

  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    {preheader}
  </div>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:{C_BG_MUTED};">
    <tr>
      <td align="center" style="padding:16px;">

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="max-width:640px;width:100%;background-color:#ffffff;
                      border:1px solid {C_BORDER};border-radius:8px;">
          <tr>
            <td style="padding:20px 20px 8px 20px;font-family:{FONT_STACK};color:{C_TEXT};">
              <h1 style="margin:0 0 4px 0;font-size:18px;line-height:1.3;font-weight:600;">
                Cheap fares: {config.ORIGIN_NAME} &rarr; {config.DESTINATION_NAME}
              </h1>
              <p style="margin:0;font-size:13px;line-height:1.5;color:{C_TEXT_MUTED};">
                Alert threshold &pound;{config.PRICE_THRESHOLD:.2f} &middot; 16-25 Railcard &middot; one-way
              </p>
            </td>
          </tr>

          {banner_html}

          <tr>
            <td style="padding:16px 20px 0 20px;font-family:{FONT_STACK};color:{C_TEXT};">
              <h2 style="margin:0 0 8px 0;font-size:15px;font-weight:600;
                         border-bottom:1px solid {C_BORDER};padding-bottom:6px;">
                Under &pound;10 &mdash; not booked yet
              </h2>
              {cheap_table_html}
              {cheap_more_html}
            </td>
          </tr>

          {booked_section_html}

          {legend_html}

          <tr>
            <td style="padding:20px;font-family:{FONT_STACK};">
              <a href="{SITE_URL}"
                 style="display:inline-block;padding:9px 14px;background-color:{C_ACCENT};
                        color:#ffffff;font-size:14px;font-weight:600;
                        text-decoration:none;border-radius:6px;">
                Open the booked-dates site
              </a>
              <p style="margin:10px 0 0 0;font-size:12px;line-height:1.5;color:{C_TEXT_MUTED};">
                Tick a date there once you've booked it and it will stop triggering
                alerts (its price keeps being checked and shown).
              </p>
            </td>
          </tr>
        </table>

      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Plain-text body
# ---------------------------------------------------------------------------


def _text_cell(option: TrainOption | None) -> str:
    price_text = _cell_price_text(option)
    marker = " *" if _cell_needs_railcard_marker(option) else ""
    arrival = _cell_arrival_text(option)
    chg = "chg" if _cell_is_indirect(option) else ""
    return " ".join(part for part in (price_text + marker, arrival, chg) if part)


def _text_table(rows: list[DateRow]) -> list[str]:
    lines = [
        (
            f"{'Date'.ljust(TEXT_COL_DATE)}{'Day'.ljust(TEXT_COL_DAY)}"
            + "".join(target.ljust(TEXT_COL_CELL) for target in config.TARGET_DEPARTURES)
        ).rstrip()
    ]
    for row in rows:
        cells = "".join(
            _text_cell(row.options.get(target)).ljust(TEXT_COL_CELL) for target in config.TARGET_DEPARTURES
        )
        lines.append(
            f"{_format_table_date(row.travel_date).ljust(TEXT_COL_DATE)}"
            f"{_format_weekday(row.travel_date).ljust(TEXT_COL_DAY)}{cells}".rstrip()
        )
    return lines


def _build_text_body(
    cheap_rows: list[DateRow], booked_rows: list[DateRow], *, test_summary: bool
) -> str:
    shown_cheap, omitted_cheap = _select_cheap(cheap_rows)
    shown_booked, omitted_booked = _select_booked(booked_rows)

    lines = [
        f"Cheap fares: {config.ORIGIN_NAME} -> {config.DESTINATION_NAME}",
        f"Alert threshold {_format_price(config.PRICE_THRESHOLD)} · 16-25 Railcard · one-way",
        "",
    ]

    if test_summary:
        lines += [
            "Manual test run: nothing is currently below £10.00, so the cheapest fare",
            "found is shown instead.",
            "",
        ]

    lines.append("UNDER £10 — NOT BOOKED YET")
    lines += _text_table(shown_cheap)
    if omitted_cheap > 0:
        lines.append(f"(+{omitted_cheap} more cheap date(s) not shown)")
    lines.append("")

    if booked_rows:
        lines.append("ALREADY BOOKED — CURRENT PRICES (information only, never alerted on)")
        lines += _text_table(shown_booked)
        if omitted_booked > 0:
            lines.append(f"(+{omitted_booked} more booked date(s) not shown)")
        lines.append("")

    needs_railcard_legend = _any_shown_needs_railcard_marker(shown_cheap) or _any_shown_needs_railcard_marker(
        shown_booked
    )
    needs_indirect_legend = _any_shown_is_indirect(shown_cheap) or _any_shown_is_indirect(shown_booked)

    if needs_railcard_legend:
        lines += [
            "* cheapest fare found for that train, but not confirmed as a 16-25",
            "  Railcard price.",
        ]
    if needs_indirect_legend:
        lines.append("chg = that journey is not direct; everything else shown is direct.")
    if needs_railcard_legend or needs_indirect_legend:
        lines.append("")

    lines.append(f"All dates and booking status: {SITE_URL}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def send_alert(
    cheap_rows: list[DateRow],
    secrets: config.Secrets,
    *,
    booked_rows: list[DateRow] | None = None,
    test_summary: bool = False,
    dry_run: bool = False,
) -> None:
    """Send (or, in dry-run, print) a price-alert email.

    `cheap_rows` is the "under threshold, not booked" table and is
    required — raises ValueError if empty, since an email containing
    only a booked-dates table is never sent (the booked table is context
    attached to an alert, never a reason for one). `booked_rows` is the
    "already booked, current prices" table, keyword-only so the two
    same-typed lists can never be passed in the wrong order; `None` is
    normalised to an empty list. `test_summary` is passed explicitly by
    the caller (main() already knows whether this is the TEST_RUN
    best-effort fallback) rather than inferred here.

    Both tables are rendered in ascending date order; the caller owns the
    decision of which dates belong in which list. Raises NotifierError if
    the email could not be sent after retries (or immediately, for a
    non-retryable failure like a bad API key).
    """
    if not cheap_rows:
        raise ValueError("send_alert called with no cheap rows — nothing to alert about")

    booked_rows = booked_rows or []

    subject = _build_subject(cheap_rows)
    text_body = _build_text_body(cheap_rows, booked_rows, test_summary=test_summary)
    html_body = _build_html_body(cheap_rows, booked_rows, test_summary=test_summary)
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
