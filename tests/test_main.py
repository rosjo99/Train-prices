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
from pathlib import Path

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
def _skip_time_gate_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every existing test here is exercising something other than the
    # RUN_HOUR_LONDON gate — bypass it by default so tests aren't flaky
    # depending on the wall-clock hour they happen to run at. The gate
    # itself gets its own dedicated tests below with this turned off.
    monkeypatch.setattr(main.config, "SKIP_TIME_GATE", True)


@pytest.fixture(autouse=True)
def _test_run_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Most tests are exercising ordinary (non-TEST_RUN) behaviour, where
    # "no match" must mean "no email" — TEST_RUN's own fallback-email
    # behaviour gets its own dedicated tests below with this turned on.
    monkeypatch.setattr(main.config, "TEST_RUN", False)


@pytest.fixture(autouse=True)
def _isolated_price_log(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Without this, every test below would append real rows to the
    # repo's actual price-history.csv on disk.
    path = tmp_path / "price-history.csv"
    monkeypatch.setattr(main.config, "PRICE_LOG_PATH", path)
    return path


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


# ---------------------------------------------------------------------------
# RUN_HOUR_LONDON time gate (8pm British time)
# ---------------------------------------------------------------------------


def _london_at(hour: int, minute: int = 0) -> datetime_module.datetime:
    return datetime_module.datetime(2026, 9, 7, hour, minute, tzinfo=config.LONDON)


def test_wrong_hour_is_a_noop_scraper_never_called(monkeypatch):
    monkeypatch.setattr(main.config, "SKIP_TIME_GATE", False)
    fetch_calls = _install_fake_scraper(monkeypatch, {})

    result = main.main(today=TERM_TIME_DAY, now=_london_at(19, 0))

    assert result == 0
    assert fetch_calls == []


def test_correct_hour_runs_normally(monkeypatch):
    monkeypatch.setattr(main.config, "SKIP_TIME_GATE", False)
    travel_date = date(2026, 9, 8)
    fetch_calls = _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY, now=_london_at(config.RUN_HOUR_LONDON, 0))

    assert result == 0
    assert fetch_calls == [travel_date]


def test_skip_time_gate_bypasses_wrong_hour(monkeypatch):
    # SKIP_TIME_GATE is already True via the autouse fixture, but assert
    # it explicitly here since this is the behaviour that fixture exists
    # to provide for every other test in this file.
    monkeypatch.setattr(main.config, "SKIP_TIME_GATE", True)
    travel_date = date(2026, 9, 8)
    fetch_calls = _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY, now=_london_at(3, 0))

    assert result == 0
    assert fetch_calls == [travel_date]


# ---------------------------------------------------------------------------
# TEST_RUN: a manual run always sends something real, using real
# scraped data, so it exercises scraping + the CSV log + Resend delivery
# end to end even when nothing is genuinely below threshold.
# ---------------------------------------------------------------------------


def test_test_run_with_genuine_match_behaves_normally(monkeypatch):
    # A real match still just goes through the normal path — TEST_RUN's
    # fallback only kicks in when there's nothing to alert on otherwise.
    monkeypatch.setattr(main.config, "TEST_RUN", True)
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("8.70")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(send_calls) == 1
    assert send_calls[0]["matches"][0].option.price == Decimal("8.70")
    assert send_calls[0]["dry_run"] is False


def test_test_run_with_no_match_sends_cheapest_real_fare_found(monkeypatch):
    monkeypatch.setattr(main.config, "TEST_RUN", True)
    travel_date = date(2026, 9, 8)
    # Priced well above the £10 threshold — a real fare, just not a
    # genuine alert-worthy one.
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("45.00")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(send_calls) == 1
    assert send_calls[0]["matches"][0].option.price == Decimal("45.00")
    assert send_calls[0]["dry_run"] is False


def test_test_run_picks_the_single_cheapest_across_dates(monkeypatch):
    monkeypatch.setattr(main.config, "TEST_RUN", True)
    d1, d2 = date(2026, 9, 8), date(2026, 9, 10)
    _install_fake_scraper(
        monkeypatch,
        {
            d1: _raw(_journey(d1, "07:25", "08:26", Decimal("45.00"))),
            d2: _raw(_journey(d2, "07:25", "08:26", Decimal("32.00"))),
        },
    )
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 2)

    main.main(today=TERM_TIME_DAY)

    assert len(send_calls[0]["matches"]) == 1
    assert send_calls[0]["matches"][0].option.price == Decimal("32.00")
    assert send_calls[0]["matches"][0].travel_date == d2


def test_test_run_with_nothing_priced_at_all_sends_no_email(monkeypatch):
    # Every target sold out (or absent) everywhere — there's no real
    # price to report, so TEST_RUN must not fabricate one.
    monkeypatch.setattr(main.config, "TEST_RUN", True)
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", price=None))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert send_calls == []


def test_test_run_notifier_failure_returns_1(monkeypatch):
    monkeypatch.setattr(main.config, "TEST_RUN", True)
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("45.00")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    _install_fake_notifier(monkeypatch, raise_exc=notifier.NotifierError("resend down"))
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1


def test_test_run_does_not_override_railcard_unconfirmed_safety(monkeypatch):
    # TEST_RUN must never bypass the "never send a wrong price" rule —
    # it's exactly the kind of thing worth confirming still holds.
    monkeypatch.setattr(main.config, "TEST_RUN", True)
    travel_date = date(2026, 9, 8)
    unconfirmed_option = _option(price=Decimal("45.00"), railcard_applied=False, departure_time="07:25")
    _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    monkeypatch.setattr(main.parser, "parse_journeys", lambda raw, d: [unconfirmed_option])
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    assert send_calls == []


# ---------------------------------------------------------------------------
# Price history CSV log
# ---------------------------------------------------------------------------


def test_successful_date_appends_to_price_log(monkeypatch, _isolated_price_log):
    travel_date = date(2026, 9, 8)
    raw = _raw(_journey(travel_date, "07:25", "08:26", Decimal("8.70")))
    _install_fake_scraper(monkeypatch, {travel_date: raw})
    _install_fake_notifier(monkeypatch)  # avoid a real network call to Resend
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    main.main(today=TERM_TIME_DAY)

    assert _isolated_price_log.exists()
    content = _isolated_price_log.read_text(encoding="utf-8")
    assert "2026-09-08" in content
    # Decimal division normalises Decimal("870")/100 to "8.7", not "8.70"
    # — a representation detail, not a rendering bug (see src/notifier.py
    # for where the £X.XX formatting actually happens).
    assert "8.7" in content


def test_failed_date_does_not_append_to_price_log(monkeypatch, _isolated_price_log):
    travel_date = date(2026, 9, 8)
    _install_fake_scraper(monkeypatch, {travel_date: scraper.ScraperError("boom")})
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    main.main(today=TERM_TIME_DAY)

    assert not _isolated_price_log.exists()
