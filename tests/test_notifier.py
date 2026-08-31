"""Tests for src.notifier.

Every test mocks src.notifier.requests.post directly — no test in this
file makes a real network call. See docs/plans/001-train-price-alert.md
Task 5 for the acceptance criteria these transcribe.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from src import notifier
from src.config import Secrets
from src.models import AlertMatch, TrainOption
from src.notifier import NotifierError, send_alert

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRETS = Secrets(
    resend_api_key="re_secret_test_key_12345",
    email_to="me@example.com",
    email_from="Train Alerts <onboarding@resend.dev>",
)


def _option(
    departure_time: str = "07:25",
    price: Decimal = Decimal("8.70"),
    arrival_time: str | None = "08:26",
    is_direct: bool = True,
) -> TrainOption:
    return TrainOption(
        travel_date=date(2026, 9, 11),
        departure_time=departure_time,
        arrival_time=arrival_time,
        price=price,
        currency="GBP",
        railcard_applied=True,
        is_direct=is_direct,
        sold_out=False,
        fare_name="Advance Single",
    )


def _match(
    travel_date: date = date(2026, 9, 11),
    price: Decimal = Decimal("8.70"),
    **option_kwargs: Any,
) -> AlertMatch:
    return AlertMatch(
        travel_date=travel_date,
        option=_option(price=price, **option_kwargs),
        threshold=Decimal("10.00"),
    )


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


def test_empty_matches_raises_value_error():
    with pytest.raises(ValueError):
        send_alert([], SECRETS)


def test_dry_run_prints_and_never_calls_requests_post(monkeypatch, capsys):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("requests.post must not be called in dry_run")

    monkeypatch.setattr(notifier.requests, "post", _fail_if_called)

    send_alert([_match()], SECRETS, dry_run=True)

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

    result = send_alert([_match()], SECRETS)

    assert result is None
    assert len(poster.calls) == 1
    call = poster.calls[0]
    assert call["url"] == notifier.RESEND_URL
    assert call["headers"]["Authorization"] == f"Bearer {SECRETS.resend_api_key}"
    assert call["json"]["to"] == SECRETS.email_to
    assert call["json"]["from"] == SECRETS.email_from
    assert "£8.70" in call["json"]["text"]
    assert "£8.70" in call["json"]["html"]


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


def test_500_retries_then_raises_notifier_error(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(500, "server error")] * notifier.MAX_ATTEMPTS)
    monkeypatch.setattr(notifier.requests, "post", poster)

    with pytest.raises(NotifierError):
        send_alert([_match()], SECRETS)

    assert len(poster.calls) == notifier.MAX_ATTEMPTS
    assert len(_no_real_sleep) == notifier.MAX_ATTEMPTS - 1


def test_success_after_retryable_failures(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(500, "oops"), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_match()], SECRETS)

    assert len(poster.calls) == 2
    assert _no_real_sleep == [notifier.RETRY_BACKOFF_SECONDS[0]]


def test_network_error_is_retried(monkeypatch, _no_real_sleep):
    poster = RecordingPost([requests.ConnectionError("boom"), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_match()], SECRETS)

    assert len(poster.calls) == 2


def test_429_is_treated_as_retryable(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(429, "rate limited"), FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_match()], SECRETS)

    assert len(poster.calls) == 2


# ---------------------------------------------------------------------------
# Non-retryable failure, and key redaction
# ---------------------------------------------------------------------------


def test_401_raises_immediately_without_retry(monkeypatch, _no_real_sleep):
    poster = RecordingPost([FakeResponse(401, "bad key")])
    monkeypatch.setattr(notifier.requests, "post", poster)

    with pytest.raises(NotifierError):
        send_alert([_match()], SECRETS)

    assert len(poster.calls) == 1
    assert _no_real_sleep == []


def test_key_never_appears_in_raised_message(monkeypatch):
    body_echoing_key = f"invalid credentials for key {SECRETS.resend_api_key}"
    poster = RecordingPost([FakeResponse(401, body_echoing_key)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    with pytest.raises(NotifierError) as exc_info:
        send_alert([_match()], SECRETS)

    assert SECRETS.resend_api_key not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_decimal_8_7_renders_as_two_decimal_places():
    assert notifier._format_price(Decimal("8.7")) == "£8.70"


def test_subject_for_single_match_has_no_more_suffix(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    send_alert([_match(price=Decimal("8.70"))], SECRETS)

    subject = poster.calls[0]["json"]["subject"]
    assert "£8.70" in subject
    assert "more" not in subject


def test_subject_for_three_matches_shows_cheapest_and_count(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    matches = [
        _match(price=Decimal("9.50"), departure_time="07:30"),
        _match(price=Decimal("6.20"), departure_time="07:25"),
        _match(price=Decimal("9.99"), departure_time="07:25"),
    ]
    send_alert(matches, SECRETS)

    subject = poster.calls[0]["json"]["subject"]
    assert "£6.20" in subject
    assert "(+2 more)" in subject


# ---------------------------------------------------------------------------
# Long match list capping
# ---------------------------------------------------------------------------


def test_long_match_list_capped_at_20_rows_plus_more_line(monkeypatch):
    poster = RecordingPost([FakeResponse(200)])
    monkeypatch.setattr(notifier.requests, "post", poster)

    matches = [
        _match(price=Decimal("5.00") + Decimal(i), departure_time="07:25")
        for i in range(25)
    ]
    send_alert(matches, SECRETS)

    text = poster.calls[0]["json"]["text"]
    html = poster.calls[0]["json"]["html"]
    assert text.count("07:25 -> 08:26") == notifier.MAX_TABLE_ROWS
    assert html.count("<tr>") == notifier.MAX_TABLE_ROWS + 1  # +1 header row
    assert "+5 more" in text
    assert "+5 more" in html


# ---------------------------------------------------------------------------
# No float
# ---------------------------------------------------------------------------


def test_no_float_anywhere_in_module():
    source = (REPO_ROOT / "src" / "notifier.py").read_text(encoding="utf-8")
    assert "float(" not in source
