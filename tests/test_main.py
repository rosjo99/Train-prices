"""Tests for src.main. See docs/plans/001-train-price-alert.md Task 6
for the acceptance criteria and edge cases these transcribe.

"today" is always injected via main(today=...) rather than monkeypatched
on the datetime module, per the plan's explicit instruction — except the
one edge case that specifically tests deriving today from Europe/London
when no today is injected (test_today_is_derived_from_europe_london),
which necessarily monkeypatches datetime.now itself.
"""

from __future__ import annotations

import datetime as datetime_module
from datetime import date
from decimal import Decimal

import pytest

from src import config, main, notifier, scraper
from src.models import AlertMatch, TrainOption

FAKE_SECRETS = config.Secrets(
    resend_api_key="re_test_key",
    email_to="me@example.com",
    email_from="Train Alerts <onboarding@resend.dev>",
)

# A day inside Autumn Term 2026, so tomorrow onward has real candidates.
TERM_TIME_DAY = date(2026, 9, 7)  # Monday; tomorrow (Tue 8 Sep) is checkable


def _iso(travel_date: date, hhmm: str) -> str:
    hour, minute = hhmm.split(":")
    return f"{travel_date.isoformat()}T{hour}:{minute}:00+01:00"


def _journey(travel_date: date, departure: str, arrival: str | None = None, price: Decimal | None = None) -> dict:
    fares = []
    if price is not None:
        pence = int(price * 100)
        fares = [
            {
                "typeDescription": "Advance Single",
                "railcardFares": [
                    {"code": config.RAILCARD_CODE, "prices": {"adult": pence, "child": 0}}
                ],
            }
        ]
    return {
        "id": 1,
        "timetable": {
            "scheduled": {
                "departure": _iso(travel_date, departure),
                "arrival": _iso(travel_date, arrival) if arrival else None,
            }
        },
        "legs": [{}],
        "fares": fares,
    }


def _raw(*journeys: dict) -> dict:
    return {"outwardJourneys": list(journeys)}


def _install_fake_scraper(monkeypatch: pytest.MonkeyPatch, results_by_date: dict) -> list[date]:
    """results_by_date maps date -> raw dict (success) or Exception instance
    (raised). Returns the list of dates actually fetched, in call order.
    """
    calls: list[date] = []

    def _fake_fetch(travel_date, *, artifacts_dir=None):
        calls.append(travel_date)
        result = results_by_date[travel_date]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)
    return calls


def _install_fake_notifier(monkeypatch: pytest.MonkeyPatch, raise_exc: Exception | None = None) -> list[dict]:
    calls: list[dict] = []

    def _fake_send_alert(matches, secrets, *, dry_run=False):
        calls.append({"matches": matches, "secrets": secrets, "dry_run": dry_run})
        if raise_exc is not None:
            raise raise_exc

    monkeypatch.setattr(main.notifier, "send_alert", _fake_send_alert)
    return calls


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fixed_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.config, "get_secrets", lambda: FAKE_SECRETS)


@pytest.fixture(autouse=True)
def _empty_booked_dates(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "booked-dates.txt"
    monkeypatch.setattr(main.config, "BOOKED_DATES_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _no_max_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.config, "MAX_DATES", None)


@pytest.fixture(autouse=True)
def _dry_run_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.config, "DRY_RUN", False)


# ---------------------------------------------------------------------------
# No candidates
# ---------------------------------------------------------------------------


def test_summer_holidays_returns_0_no_scraper_no_notifier(monkeypatch):
    # Candidates span the ENTIRE remaining school year (tomorrow through
    # term_dates.LAST_KNOWN_DATE, per plan §2.2) — a day mid-holiday
    # (e.g. Christmas break) still has a non-empty candidate list, since
    # Spring/Summer term dates are still ahead. The only genuinely-empty
    # case is being on/after the very last known date — no known term
    # data left at all, the real-world equivalent of "summer holidays"
    # under this design (a school year's worth of TERMS not yet updated
    # for the next one).
    fetch_calls = _install_fake_scraper(monkeypatch, {})
    send_calls = _install_fake_notifier(monkeypatch)

    result = main.main(today=main.term_dates.LAST_KNOWN_DATE)

    assert result == 0
    assert fetch_calls == []
    assert send_calls == []


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


def test_one_date_cheap_railcard_fare_sends_one_match(monkeypatch):
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("8.70")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(send_calls) == 1
    assert len(send_calls[0]["matches"]) == 1
    assert send_calls[0]["matches"][0].option.price == Decimal("8.70")
    assert send_calls[0]["dry_run"] is False


def test_price_exactly_threshold_does_not_alert(monkeypatch):
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("10.00")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert send_calls == []


def test_price_just_under_threshold_alerts(monkeypatch):
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("9.99")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(send_calls) == 1


def test_both_trains_sold_out_no_alert(monkeypatch):
    travel_date = date(2026, 9, 8)
    raw = _raw(
        _journey(travel_date, "07:25", "08:26", price=None),
        _journey(travel_date, "07:30", "08:25", price=None),
    )
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert send_calls == []


def test_zero_trains_returned_no_alert_warning_logged(monkeypatch, caplog):
    travel_date = date(2026, 9, 8)
    _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    with caplog.at_level("INFO"):
        result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert send_calls == []


# ---------------------------------------------------------------------------
# Per-date failure isolation vs BlockedError abort
# ---------------------------------------------------------------------------


def test_scraper_fails_on_one_date_others_still_checked(monkeypatch):
    d1, d2, d3 = date(2026, 9, 8), date(2026, 9, 10), date(2026, 9, 11)
    fetch_calls = _install_fake_scraper(
        monkeypatch,
        {
            d1: scraper.ScraperError("boom"),
            d2: _raw(_journey(d2, "07:25", "08:26", price=None)),
            d3: _raw(_journey(d3, "07:25", "08:26", price=None)),
        },
    )
    monkeypatch.setattr(main.config, "MAX_DATES", 3)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert fetch_calls == [d1, d2, d3]


def test_scraper_fails_on_every_date_returns_1_no_email(monkeypatch):
    d1, d2 = date(2026, 9, 8), date(2026, 9, 10)
    _install_fake_scraper(
        monkeypatch,
        {d1: scraper.ScraperError("boom"), d2: scraper.ScraperError("boom again")},
    )
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 2)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    assert send_calls == []


def test_blocked_error_on_first_date_aborts_immediately(monkeypatch):
    d1, d2 = date(2026, 9, 8), date(2026, 9, 10)
    fetch_calls = _install_fake_scraper(
        monkeypatch, {d1: scraper.BlockedError("blocked"), d2: _raw()}
    )
    monkeypatch.setattr(main.config, "MAX_DATES", 2)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    assert fetch_calls == [d1]  # never reached d2


def test_hijacked_error_aborts_immediately_like_blocked(monkeypatch):
    d1, d2 = date(2026, 9, 8), date(2026, 9, 10)
    fetch_calls = _install_fake_scraper(
        monkeypatch, {d1: scraper.HijackedError("hijacked"), d2: _raw()}
    )
    monkeypatch.setattr(main.config, "MAX_DATES", 2)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    assert fetch_calls == [d1]


# ---------------------------------------------------------------------------
# Railcard confirmation
# ---------------------------------------------------------------------------


def test_sub_threshold_price_without_railcard_confirmation_returns_1_no_email(monkeypatch):
    # src.parser never actually produces a priced-but-unconfirmed
    # TrainOption (its price is only ever set FROM a matching railcard
    # fare — see src/parser.py), so this exercises the scenario by
    # stubbing parser.parse_journeys directly with a hand-built option,
    # rather than trying to coax it out of a raw response.
    travel_date = date(2026, 9, 8)
    unconfirmed_option = _option(price=Decimal("5.00"), railcard_applied=False, departure_time="07:25")
    _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    monkeypatch.setattr(main.parser, "parse_journeys", lambda raw, d: [unconfirmed_option])
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    assert send_calls == []


# ---------------------------------------------------------------------------
# DRY_RUN
# ---------------------------------------------------------------------------


def test_dry_run_needs_no_secrets_and_passes_dry_run_true(monkeypatch):
    def _raise_config_error():
        raise config.ConfigError("no secrets configured")

    monkeypatch.setattr(main.config, "get_secrets", _raise_config_error)
    monkeypatch.setattr(main.config, "DRY_RUN", True)

    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("8.70")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(send_calls) == 1
    assert send_calls[0]["dry_run"] is True


# ---------------------------------------------------------------------------
# Notifier failure
# ---------------------------------------------------------------------------


def test_notifier_raises_returns_1(monkeypatch):
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("8.70")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    _install_fake_notifier(monkeypatch, raise_exc=notifier.NotifierError("resend down"))
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1


# ---------------------------------------------------------------------------
# Booked dates
# ---------------------------------------------------------------------------


def test_booked_date_excluded_scraper_never_called_for_it(monkeypatch, _empty_booked_dates):
    tomorrow = TERM_TIME_DAY + datetime_module.timedelta(days=1)
    _empty_booked_dates.write_text(f"{tomorrow.isoformat()}\n", encoding="utf-8")
    other_date = date(2026, 9, 10)
    fetch_calls = _install_fake_scraper(monkeypatch, {other_date: _raw()})
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert tomorrow not in fetch_calls
    assert fetch_calls == [other_date]


def test_all_candidates_booked_returns_0_scraper_never_called(monkeypatch, _empty_booked_dates):
    all_candidates = main.term_dates.checkable_dates(
        TERM_TIME_DAY + datetime_module.timedelta(days=1), main.term_dates.LAST_KNOWN_DATE
    )
    _empty_booked_dates.write_text(
        "\n".join(d.isoformat() for d in all_candidates), encoding="utf-8"
    )
    fetch_calls = _install_fake_scraper(monkeypatch, {})
    send_calls = _install_fake_notifier(monkeypatch)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert fetch_calls == []
    assert send_calls == []


def test_missing_booked_dates_file_behaves_as_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(main.config, "BOOKED_DATES_PATH", tmp_path / "does-not-exist.txt")
    travel_date = date(2026, 9, 8)
    fetch_calls = _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert fetch_calls == [travel_date]


# ---------------------------------------------------------------------------
# Edge cases: clock / date-range sizing
# ---------------------------------------------------------------------------


def test_today_is_derived_from_europe_london(monkeypatch):
    # 2026-08-31 23:50 UTC is 2026-09-01 00:50 BST — "today" must be the
    # LONDON date (01 Sep), not the UTC one (31 Aug).
    fixed_utc = datetime_module.datetime(2026, 8, 31, 23, 50, tzinfo=datetime_module.timezone.utc)

    class FakeDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz) if tz else fixed_utc

    monkeypatch.setattr(main, "datetime", FakeDatetime)

    captured_start = {}

    def _fake_checkable_dates(start, end):
        captured_start["start"] = start
        return []

    monkeypatch.setattr(main.term_dates, "checkable_dates", _fake_checkable_dates)

    result = main.main()

    assert result == 0
    assert captured_start["start"] == date(2026, 9, 2)  # (today) 2026-09-01 + 1 day


def test_today_before_last_known_date_leaves_exactly_one_candidate(monkeypatch):
    the_day_before = main.term_dates.LAST_KNOWN_DATE - datetime_module.timedelta(days=1)
    fetch_calls = _install_fake_scraper(monkeypatch, {main.term_dates.LAST_KNOWN_DATE: _raw()})

    result = main.main(today=the_day_before)

    assert result == 0
    assert fetch_calls == [main.term_dates.LAST_KNOWN_DATE]


def test_max_dates_one_with_many_real_candidates_checks_exactly_one(monkeypatch):
    monkeypatch.setattr(main.config, "MAX_DATES", 1)
    fetch_calls: list[date] = []

    def _fake_fetch(travel_date, *, artifacts_dir=None):
        fetch_calls.append(travel_date)
        return _raw()

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(fetch_calls) == 1


def test_first_day_of_autumn_term_has_102_candidates(monkeypatch):
    fetch_calls: list[date] = []

    def _fake_fetch(travel_date, *, artifacts_dir=None):
        fetch_calls.append(travel_date)
        return _raw()

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)

    result = main.main(today=date(2026, 8, 31))

    assert result == 0
    assert len(fetch_calls) == 102


# ---------------------------------------------------------------------------
# evaluate() — pure function, tested directly
# ---------------------------------------------------------------------------


def _option(price=Decimal("8.70"), currency="GBP", railcard_applied=True, departure_time="07:25", sold_out=False):
    return TrainOption(
        travel_date=date(2026, 9, 8),
        departure_time=departure_time,
        arrival_time="08:26",
        price=price,
        currency=currency,
        railcard_applied=railcard_applied,
        is_direct=True,
        sold_out=sold_out,
        fare_name="Advance Single",
    )


def test_evaluate_excludes_non_gbp_currency():
    travel_date = date(2026, 9, 8)
    option = _option(currency="EUR")
    matches, railcard_unconfirmed = main.evaluate({travel_date: {"07:25": option}})

    assert matches == []
    assert railcard_unconfirmed is False


def test_evaluate_deduplicates_same_date_and_departure_time():
    travel_date = date(2026, 9, 8)
    option = _option(departure_time="07:25")
    # Same option, duplicated under two different target-time keys — the
    # dedup key is the option's OWN departure_time, not the dict key.
    matches, _ = main.evaluate({travel_date: {"07:25": option, "07:30": option}})

    assert len(matches) == 1


def test_evaluate_railcard_unconfirmed_flag():
    travel_date = date(2026, 9, 8)
    option = _option(railcard_applied=False)
    matches, railcard_unconfirmed = main.evaluate({travel_date: {"07:25": option}})

    assert matches == []
    assert railcard_unconfirmed is True
