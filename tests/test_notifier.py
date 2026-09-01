"""Tests for src.notifier.

Every test mocks src.notifier.requests.post directly — no test in this
file makes a real network call. See
docs/plans/004-redesign-alert-email.md §8 for the acceptance criteria
these transcribe.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from src import config, notifier
from src.config import Secrets
from src.models import DateRow, TrainOption
from src.notifier import NotifierError, send_alert

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRETS = Secrets(
    resend_api_key="re_secret_test_key_12345",
    email_to="me@example.com",
    email_from="Train Alerts <onboarding@resend.dev>",
)


def _option(
    departure_time: str = "07:25",
    price: Decimal | None = Decimal("8.70"),
    arrival_time: str | None = "08:26",
    is_direct: bool = True,
    railcard_applied: bool = True,
    sold_out: bool = False,
    travel_date: date = date(2026, 9, 11),
) -> TrainOption:
    return TrainOption(
        travel_date=travel_date,
        departure_time=departure_time,
        arrival_time=arrival_time,
        price=price,
        currency="GBP",
        railcard_applied=railcard_applied,
        is_direct=is_direct,
        sold_out=sold_out,
        fare_name="Advance Single",
    )


def _row(
    travel_date: date = date(2026, 9, 11),
    prices: dict[str, Decimal | None] | None = None,
    **option_kwargs: Any,
) -> DateRow:
    """prices maps a target departure ("07:25"/"07:30") to a Decimal, to
    None for 'sold out', or omits it entirely for 'not found'.
    """
    if prices is None:
        prices = {"07:25": Decimal("8.70")}
    options: dict[str, TrainOption | None] = {}
    for target in config.TARGET_DEPARTURES:
        if target not in prices:
            options[target] = None
        else:
            price = prices[target]
            options[target] = _option(
                departure_time=target,
                price=price,
                sold_out=price is None,
                travel_date=travel_date,
                **option_kwargs,
            )
    return DateRow(travel_date=travel_date, options=options)


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class RecordingPost:
    """Hands out a scripted sequence of responses/exceptions, in order,
    recording every call's args/kwargs for assertions.
    """

    def __init__(self, results: list[Any]):
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, json: dict, headers: dict, timeout: float):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        result = self._results[len(self.calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(notifier.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------


def test_empty_cheap_rows_raises_value_error():
    with pytest.raises(ValueError):
        send_alert([], SECRETS)


def test_dry_run_prints_and_never_calls_requests_post(monkeypatch, capsys):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("requests.post must not be called in dry_run")

    monkeypatch.setattr(notifier.requests, "post", _fail_if_called)

    send_alert([_row()], SECRETS, dry_run=True)

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert SECRETS.email_to in out
    assert "£8.70" in out


# ---------------------------------------------------------------------------
# Successful send
# ---------------------------------------------------------------------------


def test_successful_send_returns_none_with_correct_request(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    result = send_alert([_row()], SECRETS)

    assert result is None
    assert len(poster.calls) == 1
    call = poster.calls[0]
    assert call["url"] == notifier.RESEND_URL
    assert call["headers"]["Authorization"] == f"Bearer {SECRETS.resend_api_key}"
    assert call["json"]["to"] == [SECRETS.email_to]
    assert call["json"]["from"] == SECRETS.email_from
    assert "£8.70" in call["json"]["text"]
    assert "£8.70" in call["json"]["html"]


def test_comma_separated_email_to_sends_to_every_address(monkeypatch):
    secrets = Secrets(
        resend_api_key="re_secret_test_key_12345",
        email_to="a@example.com, b@example.com,,c@example.com ",
        email_from="Train Alerts <onboarding@resend.dev>",
    )
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row()], secrets)

    assert poster.calls[0]["json"]["to"] == ["a@example.com", "b@example.com", "c@example.com"]


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


def test_500_retries_then_raises_notifier_error(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(500, "server error")] * notifier.MAX_ATTEMPTS)
    monkeypatch.setattr(notifier.requests, "post", poster)

    with pytest.raises(NotifierError):
        send_alert([_row()], SECRETS)

    assert len(poster.calls) == notifier.MAX_ATTEMPTS
    assert len(_no_real_sleep) == notifier.MAX_ATTEMPTS - 1


def test_success_after_retryable_failures(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(500, "oops"), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row()], SECRETS)

    assert len(poster.calls) == 2
    assert _no_real_sleep == [notifier.RETRY_BACKOFF_SECONDS[0]]


def test_network_error_is_retried(monkeypatch, _no_real_sleep):
    poster = RecordingPost([requests.ConnectionError("boom"), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row()], SECRETS)

    assert len(poster.calls) == 2


def test_429_is_treated_as_retryable(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(429, "rate limited"), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row()], SECRETS)

    assert len(poster.calls) == 2


# ---------------------------------------------------------------------------
# Non-retryable failure, and key redaction
# ---------------------------------------------------------------------------


def test_401_raises_immediately_without_retry(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(401, "bad key")])
    monkeypatch.setattr(notifier.requests, "post", poster)

    with pytest.raises(NotifierError):
        send_alert([_row()], SECRETS)

    assert len(poster.calls) == 1
    assert _no_real_sleep == []


def test_key_never_appears_in_raised_message(monkeypatch):
    body_echoing_key = f"invalid credentials for key {SECRETS.resend_api_key}"
    poster = RecordingPost([FakeResponse(401, body_echoing_key)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    with pytest.raises(NotifierError) as exc_info:
        send_alert([_row()], SECRETS)

    assert SECRETS.resend_api_key not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Rendering — basics
# ---------------------------------------------------------------------------


def test_decimal_8_7_renders_as_two_decimal_places():
    assert notifier._format_price(Decimal("8.7")) == "£8.70"


def test_subject_for_single_row_has_no_more_suffix(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row(prices={"07:25": Decimal("8.70")})], SECRETS)

    subject = poster.calls[0]["json"]["subject"]
    assert "£8.70" in subject
    assert "more" not in subject


def test_subject_for_three_matches_shows_cheapest_and_count(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    rows = [
        _row(travel_date=date(2026, 9, 8), prices={"07:25": Decimal("9.50")}),
        _row(travel_date=date(2026, 9, 10), prices={"07:25": Decimal("6.20")}),
        _row(travel_date=date(2026, 9, 11), prices={"07:25": Decimal("9.99")}),
    ]
    send_alert(rows, SECRETS)

    subject = poster.calls[0]["json"]["subject"]
    assert "£6.20" in subject
    assert "(+2 more dates)" in subject


# ---------------------------------------------------------------------------
# Row/table capping
# ---------------------------------------------------------------------------


def test_cheap_table_capped_at_max_cheap_rows(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    rows = [
        _row(
            travel_date=date(2026, 9, 8) + timedelta(days=i),
            prices={"07:25": Decimal("5.00") + Decimal(i)},
        )
        for i in range(30)
    ]
    send_alert(rows, SECRETS)

    text = poster.calls[0]["json"]["text"]
    html = poster.calls[0]["json"]["html"]
    assert html.count(f'background-color:{notifier.C_CHEAP_BG}') >= notifier.MAX_CHEAP_ROWS
    assert "+5 more cheap" in text
    assert "+5 more cheap" in html


def test_booked_table_capped_at_max_booked_rows(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    cheap_rows = [_row(travel_date=date(2026, 9, 8), prices={"07:25": Decimal("5.00")})]
    booked_rows = [
        _row(
            travel_date=date(2026, 10, 1) + timedelta(days=i),
            prices={"07:25": Decimal("50.00")},
        )
        for i in range(30)
    ]
    send_alert(cheap_rows, SECRETS, booked_rows=booked_rows)

    text = poster.calls[0]["json"]["text"]
    html = poster.calls[0]["json"]["html"]
    assert "+5 more booked" in text
    assert "+5 more booked" in html


# ---------------------------------------------------------------------------
# No float
# ---------------------------------------------------------------------------


def test_no_float_anywhere_in_module():
    source = (REPO_ROOT / "src" / "notifier.py").read_text(encoding="utf-8")
    assert "float(" not in source


# ---------------------------------------------------------------------------
# New tests: two-table, per-departure-cell design
# (docs/plans/004-redesign-alert-email.md §8.3)
# ---------------------------------------------------------------------------


def test_row_shows_both_departures_in_one_row(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    row = _row(prices={"07:25": Decimal("8.70"), "07:30": Decimal("15.00")})
    send_alert([row], SECRETS)

    html = poster.calls[0]["json"]["html"]
    text = poster.calls[0]["json"]["text"]
    assert "£8.70" in html and "£15.00" in html
    assert "£8.70" in text and "£15.00" in text


@pytest.mark.parametrize(
    ("option_kwargs", "expected"),
    [
        ({"present": False}, "not found"),
        ({"sold_out": True, "price": None}, "sold out"),
        ({"price": Decimal("8.70")}, "£8.70"),
    ],
)
def test_cell_price_text_matches_the_website_formats(option_kwargs, expected):
    present = option_kwargs.pop("present", True)
    option = _option(**option_kwargs) if present else None
    assert notifier._cell_price_text(option) == expected


def test_cell_shows_arrival_time(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    row = _row(prices={"07:25": Decimal("8.70")}, arrival_time="08:26")
    send_alert([row], SECRETS)

    html = poster.calls[0]["json"]["html"]
    text = poster.calls[0]["json"]["text"]
    assert "arr 08:26" in html
    assert "arr 08:26" in text


def test_cell_omits_detail_line_when_no_arrival_and_direct(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    row = _row(prices={"07:25": Decimal("8.70")}, arrival_time=None, is_direct=True)
    send_alert([row], SECRETS)

    html = poster.calls[0]["json"]["html"]
    text = poster.calls[0]["json"]["text"]
    # "&rarr;" (in the arrow between Oxford/Paddington) also contains the
    # substring "arr" — the real check is for the "arr HH:MM" detail line.
    assert "arr 0" not in html
    assert "arr 0" not in text


def test_indirect_journey_is_flagged_and_direct_is_not(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    row = DateRow(
        travel_date=date(2026, 9, 11),
        options={
            "07:25": _option(departure_time="07:25", price=Decimal("8.70"), is_direct=True),
            "07:30": _option(departure_time="07:30", price=Decimal("9.00"), is_direct=False),
        },
    )
    send_alert([row], SECRETS)

    html = poster.calls[0]["json"]["html"]
    text = poster.calls[0]["json"]["text"]
    # Split off the shared legend line (which itself explains the word
    # "changes"/"chg") so the count reflects only the per-cell markers.
    html_tables = html.split("&ldquo;changes&rdquo;")[0]
    text_tables = text.split("chg = that journey")[0]
    assert html_tables.count("changes") == 1
    assert text_tables.count("chg") == 1
    # The 07:25 cell (direct) must not itself carry the word "direct".
    assert "direct<" not in html_tables and ">direct" not in html_tables


def test_changes_legend_only_when_an_indirect_cell_is_shown(monkeypatch):
    poster = RecordingPost([FakeResponse(200), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    indirect_row = DateRow(
        travel_date=date(2026, 9, 11),
        options={
            "07:25": _option(departure_time="07:25", price=Decimal("8.70"), is_direct=False),
            "07:30": None,
        },
    )
    send_alert([indirect_row], SECRETS)
    html_indirect = poster.calls[0]["json"]["html"]
    text_indirect = poster.calls[0]["json"]["text"]
    assert "not direct" in html_indirect
    assert "not direct" in text_indirect

    direct_row = _row(prices={"07:25": Decimal("8.70")}, is_direct=True)
    send_alert([direct_row], SECRETS)
    html_direct = poster.calls[1]["json"]["html"]
    text_direct = poster.calls[1]["json"]["text"]
    assert "not direct" not in html_direct
    assert "not direct" not in text_direct


def test_sold_out_cell_still_shows_its_arrival_time():
    option = _option(price=None, sold_out=True, arrival_time="08:26")
    assert notifier._cell_arrival_text(option) == "arr 08:26"
    assert notifier._cell_price_text(option) == "sold out"


def test_booked_table_rendered_with_booked_background(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    cheap_row = _row(travel_date=date(2026, 9, 8), prices={"07:25": Decimal("8.70")})
    booked_row = _row(
        travel_date=date(2026, 9, 11),
        prices={"07:25": Decimal("22.50")},
        arrival_time="09:00",
    )
    send_alert([cheap_row], SECRETS, booked_rows=[booked_row])

    html = poster.calls[0]["json"]["html"]
    text = poster.calls[0]["json"]["text"]
    assert notifier.C_BOOKED_BG in html
    assert "£22.50" in html and "£22.50" in text
    assert "arr 09:00" in html
    assert "arr 09:00" in text


def test_booked_section_omitted_when_no_booked_rows(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row()], SECRETS, booked_rows=[])

    html = poster.calls[0]["json"]["html"]
    assert "Already booked" not in html
    assert notifier.C_BOOKED_BG not in html


def test_booked_date_never_appears_in_cheap_section(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    cheap_row = _row(travel_date=date(2026, 9, 8), prices={"07:25": Decimal("8.70")})
    booked_row = _row(travel_date=date(2026, 9, 30), prices={"07:25": Decimal("22.50")})
    send_alert([cheap_row], SECRETS, booked_rows=[booked_row])

    html = poster.calls[0]["json"]["html"]
    under_idx = html.index("Under")
    booked_idx = html.index("Already booked")
    assert under_idx < booked_idx
    cheap_section = html[under_idx:booked_idx]
    assert "30 Sep 2026" not in cheap_section


def test_cheap_row_is_green_and_cheap_price_is_bold_green(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row(prices={"07:25": Decimal("8.70")})], SECRETS)

    html = poster.calls[0]["json"]["html"]
    assert notifier.C_CHEAP_BG in html
    assert notifier.C_ACCENT in html


def test_test_summary_row_is_not_tinted_green_and_banner_present(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    row = _row(prices={"07:25": Decimal("45.00")})
    send_alert([row], SECRETS, test_summary=True)

    html = poster.calls[0]["json"]["html"]
    assert "Manual test run" in html
    assert notifier.C_CHEAP_BG not in html


def test_prices_link_to_the_journey_planner(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    travel_date = date(2026, 9, 11)
    row = _row(travel_date=travel_date, prices={"07:25": Decimal("8.70")})
    send_alert([row], SECRETS)

    html = poster.calls[0]["json"]["html"]
    expected_href = config.build_journey_planner_url(travel_date, "07", "25")
    assert expected_href in html
    # The detail line is not inside the anchor.
    anchor_start = html.index(f'href="{expected_href}"')
    anchor_close = html.index("</a>", anchor_start)
    assert "arr" not in html[anchor_start:anchor_close]


def test_railcard_unconfirmed_gets_marker_and_legend(monkeypatch):
    poster = RecordingPost([FakeResponse(200), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    unconfirmed_row = _row(prices={"07:25": Decimal("8.70")}, railcard_applied=False)
    send_alert([unconfirmed_row], SECRETS)
    html_unconfirmed = poster.calls[0]["json"]["html"]
    text_unconfirmed = poster.calls[0]["json"]["text"]
    assert "not confirmed as a" in html_unconfirmed
    assert "not confirmed as a" in text_unconfirmed
    assert " *" in html_unconfirmed

    confirmed_row = _row(prices={"07:25": Decimal("8.70")}, railcard_applied=True)
    send_alert([confirmed_row], SECRETS)
    html_confirmed = poster.calls[1]["json"]["html"]
    assert "not confirmed as a" not in html_confirmed


def test_html_has_no_style_block(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row(prices={"07:25": Decimal("8.70")})], SECRETS)

    html = poster.calls[0]["json"]["html"]
    assert "<style" not in html
    assert f'<td style="padding:7px 8px;border-bottom:1px solid {notifier.C_BORDER};background-color:{notifier.C_CHEAP_BG}' in html


def test_text_body_contains_both_sections(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    cheap_row = _row(travel_date=date(2026, 9, 8), prices={"07:25": Decimal("8.70")})
    booked_row = _row(travel_date=date(2026, 9, 30), prices={"07:25": Decimal("22.50")})
    send_alert([cheap_row], SECRETS, booked_rows=[booked_row])

    text = poster.calls[0]["json"]["text"]
    cheap_idx = text.index("UNDER £10")
    booked_idx = text.index("ALREADY BOOKED")
    assert cheap_idx < booked_idx
    assert "8 Sep 2026" in text[cheap_idx:booked_idx]
    assert "30 Sep 2026" in text[booked_idx:]


def test_text_table_line_width_stays_within_78_columns(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    row = DateRow(
        travel_date=date(2026, 9, 11),
        options={
            "07:25": _option(
                departure_time="07:25",
                price=Decimal("12.30"),
                arrival_time="08:31",
                is_direct=False,
                railcard_applied=False,
            ),
            "07:30": None,
        },
    )
    send_alert([row], SECRETS)

    text = poster.calls[0]["json"]["text"]
    for line in text.splitlines():
        assert len(line) <= 78, line


def test_site_url_present_in_both_bodies(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_row()], SECRETS)

    html = poster.calls[0]["json"]["html"]
    text = poster.calls[0]["json"]["text"]
    assert notifier.SITE_URL in html
    assert notifier.SITE_URL in text
