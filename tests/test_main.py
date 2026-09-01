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
import threading
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
    (raised). Returns the list of dates actually fetched.

    main() now runs a continuous scheduler with up to main.PARALLEL_DATES
    scrapes in flight at once (see src/main.py), refilling the window the
    instant any one completes — so with the default PARALLEL_DATES, the
    order dates are actually fetched in is a genuine thread race and not
    guaranteed to match dispatch order. list.append is thread-safe (the
    GIL makes it atomic), so no calls are lost, but tests that care about
    ORDER should set `monkeypatch.setattr(main, "PARALLEL_DATES", 1)`,
    which makes the scheduler degenerate to strictly serial submit / wait
    / finalize.
    """
    calls: list[date] = []

    def _fake_fetch(travel_date, *, artifacts_dir=None, attempts=None):
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
    # All three can be in flight at once (main.PARALLEL_DATES=5), so
    # fetch order isn't guaranteed — only that every one of them got
    # attempted.
    assert set(fetch_calls) == {d1, d2, d3}


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


def test_five_consecutive_failed_dates_stops_early(monkeypatch):
    # NRE only releases fares roughly 12 weeks ahead — dates beyond that
    # horizon fail every time, so this guards against burning the full
    # per-date retry budget on every remaining date of the school year.
    # PARALLEL_DATES=1 makes the scheduler strictly serial, so dispatch
    # (and therefore fetch) order is deterministic and d6 is provably
    # never submitted once d1-d5 latch the stop.
    monkeypatch.setattr(main, "PARALLEL_DATES", 1)
    d1, d2, d3, d4, d5 = (
        date(2026, 9, 8),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 15),
        date(2026, 9, 17),
    )
    d6 = date(2026, 9, 18)  # would succeed, but must never be attempted
    fetch_calls = _install_fake_scraper(
        monkeypatch,
        {
            d1: scraper.ScraperError("boom"),
            d2: scraper.ScraperError("boom"),
            d3: scraper.ScraperError("boom"),
            d4: scraper.ScraperError("boom"),
            d5: scraper.ScraperError("boom"),
            d6: _raw(_journey(d6, "07:25", "08:26", price=None)),
        },
    )
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 6)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    # Strictly serial (PARALLEL_DATES=1): d1-d5 fail in order, latching
    # the stop before d6 is ever submitted.
    assert fetch_calls == [d1, d2, d3, d4, d5]
    assert send_calls == []


def test_a_success_between_failures_resets_the_consecutive_count(monkeypatch):
    # Four failures, then a success, then four more failures — never five
    # in a row — must check every candidate date, not stop early.
    dates = [
        date(2026, 9, 8),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 15),
        date(2026, 9, 17),  # succeeds — resets the streak
        date(2026, 9, 18),
        date(2026, 9, 22),
        date(2026, 9, 24),
        date(2026, 9, 25),
    ]
    success_date = dates[4]
    results_by_date = {
        d: (scraper.ScraperError("boom") if d != success_date else _raw()) for d in dates
    }
    fetch_calls = _install_fake_scraper(monkeypatch, results_by_date)
    monkeypatch.setattr(main.config, "MAX_DATES", len(dates))

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    # Every candidate date must be attempted (never stops early). This is
    # scheduler-agnostic — it doesn't assert on order, so it's robust
    # under any concurrency, and doesn't need PARALLEL_DATES pinned.
    assert set(fetch_calls) == set(dates)
    assert len(fetch_calls) == len(dates)


def test_five_consecutive_parse_failures_also_stops_early(monkeypatch):
    # The consecutive-failure count spans both failure types (scrape and
    # parse), not just one — a mix of the two should still trip it.
    # PARALLEL_DATES=1 makes d6 provably never dispatched, so `results`
    # stays empty and the run returns 1 — with the default window, d6
    # could be dispatched (and succeed) before the stop latches, and the
    # run would return 0 instead.
    monkeypatch.setattr(main, "PARALLEL_DATES", 1)
    d1, d2, d3, d4, d5 = (
        date(2026, 9, 8),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 15),
        date(2026, 9, 17),
    )
    d6 = date(2026, 9, 18)
    _install_fake_scraper(
        monkeypatch,
        {
            d1: scraper.ScraperError("boom"),
            d2: _raw(),  # scrapes fine, but...
            d3: scraper.ScraperError("boom"),
            d4: _raw(),
            d5: scraper.ScraperError("boom"),
            d6: _raw(_journey(d6, "07:25", "08:26", price=None)),
        },
    )
    # ...parsing d2 and d4's raw bodies fails, so all five of d1-d5 count
    # as failed dates regardless of which stage failed.
    original_parse = main.parser.parse_journeys

    def _fake_parse(raw, travel_date):
        if raw == {"outwardJourneys": []} and travel_date in (d2, d4):
            raise main.parser.ParseError("boom")
        return original_parse(raw, travel_date)

    monkeypatch.setattr(main.parser, "parse_journeys", _fake_parse)
    monkeypatch.setattr(main.config, "MAX_DATES", 6)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1


def _seven_dates() -> list[date]:
    return [
        date(2026, 9, 8),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 15),
        date(2026, 9, 17),
        date(2026, 9, 18),  # must never be attempted
        date(2026, 9, 22),  # must never be attempted
    ]


def test_blocked_error_aborts_the_run(monkeypatch):
    # PARALLEL_DATES=1 makes the scheduler strictly serial: dates[0]
    # comes back BlockedError and the whole run aborts before any later
    # date is ever submitted.
    monkeypatch.setattr(main, "PARALLEL_DATES", 1)
    dates = _seven_dates()
    results_by_date = {d: _raw() for d in dates}
    results_by_date[dates[0]] = scraper.BlockedError("blocked")
    fetch_calls = _install_fake_scraper(monkeypatch, results_by_date)
    monkeypatch.setattr(main.config, "MAX_DATES", len(dates))

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    assert fetch_calls == [dates[0]]


def test_hijacked_error_aborts_the_run(monkeypatch):
    monkeypatch.setattr(main, "PARALLEL_DATES", 1)
    dates = _seven_dates()
    results_by_date = {d: _raw() for d in dates}
    results_by_date[dates[0]] = scraper.HijackedError("hijacked")
    fetch_calls = _install_fake_scraper(monkeypatch, results_by_date)
    monkeypatch.setattr(main.config, "MAX_DATES", len(dates))

    result = main.main(today=TERM_TIME_DAY)

    assert result == 1
    assert fetch_calls == [dates[0]]


# ---------------------------------------------------------------------------
# Continuous scheduler (docs/plans/003-scheduler-and-retry-horizon.md §4.2,
# §4.3) and boundary-first dispatch (§4.6). The two tests that need
# deterministic completion-order control use a threading.Event as a gate
# INSIDE the fakes, never a sleep — no wall-clock timing dependency.
# ---------------------------------------------------------------------------


def test_dispatch_starts_a_new_date_before_the_window_drains(monkeypatch):
    # Direct test of Change 2's whole purpose: with a straggler still
    # running, the scheduler must refill the window rather than waiting
    # for the whole batch to finish. c0 blocks; c2 (dispatched while c0
    # is still in flight, once c1 frees a slot) sets the gate that
    # unblocks it. Under the old fixed-batch scheduler this would time
    # out and record False.
    monkeypatch.setattr(main, "PARALLEL_DATES", 2)
    c0, c1, c2, c3 = (
        date(2026, 9, 8),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 15),
    )
    gate = threading.Event()
    gate_result: dict[str, bool] = {}

    def _fake_fetch(travel_date, *, artifacts_dir=None, attempts=None):
        if travel_date == c0:
            gate_result["opened_before_c0_returned"] = gate.wait(timeout=5)
        elif travel_date == c2:
            gate.set()
        return _raw()

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)
    monkeypatch.setattr(main.config, "MAX_DATES", 4)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert gate_result["opened_before_c0_returned"] is True


def test_stop_early_still_finalizes_results_already_in_flight(
    monkeypatch, _isolated_price_log
):
    # Direct test of §4.3 point 3 (and the §1.4-item-2 behaviour change):
    # c0-c4 all fail — the fifth failure (c4) latches the stop — but c5
    # was already dispatched and completes (with a real sub-threshold
    # fare) while c4 is still failing, so it must still be finalized:
    # logged to the price log and alerted on, not silently dropped.
    monkeypatch.setattr(main, "PARALLEL_DATES", 2)
    c0, c1, c2, c3, c4, c5 = (
        date(2026, 9, 8),
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 15),
        date(2026, 9, 17),
        date(2026, 9, 18),
    )
    gate = threading.Event()

    def _fake_fetch(travel_date, *, artifacts_dir=None, attempts=None):
        if travel_date == c4:
            gate.wait(timeout=5)
            raise scraper.ScraperError("boom")
        if travel_date == c5:
            gate.set()
            return _raw(_journey(c5, "07:25", "08:26", Decimal("5.00")))
        if travel_date in (c0, c1, c2, c3):
            raise scraper.ScraperError("boom")
        raise AssertionError(f"unexpected date {travel_date}")

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 6)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    content = _isolated_price_log.read_text(encoding="utf-8")
    assert c5.isoformat() in content
    assert len(send_calls) == 1
    assert send_calls[0]["matches"][0].travel_date == c5


def test_boundary_zone_date_is_dispatched_first(monkeypatch):
    # PARALLEL_DATES=1 makes dispatch order directly observable. The
    # candidate at exactly today + FULL_RETRY_HORIZON_DAYS falls in the
    # boundary priority zone and must be dispatched before every earlier,
    # nearer-term candidate.
    monkeypatch.setattr(main, "PARALLEL_DATES", 1)
    near = [date(2026, 9, 8), date(2026, 9, 10), date(2026, 9, 11)]
    boundary_date = TERM_TIME_DAY + datetime_module.timedelta(
        days=main.FULL_RETRY_HORIZON_DAYS
    )
    all_dates = near + [boundary_date]
    monkeypatch.setattr(main.term_dates, "checkable_dates", lambda start, end: all_dates)
    fetch_calls = _install_fake_scraper(monkeypatch, {d: _raw() for d in all_dates})

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert fetch_calls[0] == boundary_date
    assert fetch_calls[1:] == near


def test_boundary_zone_kill_switch_restores_ascending_order(monkeypatch):
    # Companion to the test above: BOUNDARY_PRIORITY_ZONE_DAYS = 0 must
    # disable the reordering entirely.
    monkeypatch.setattr(main, "PARALLEL_DATES", 1)
    monkeypatch.setattr(main, "BOUNDARY_PRIORITY_ZONE_DAYS", 0)
    near = [date(2026, 9, 8), date(2026, 9, 10), date(2026, 9, 11)]
    boundary_date = TERM_TIME_DAY + datetime_module.timedelta(
        days=main.FULL_RETRY_HORIZON_DAYS
    )
    all_dates = near + [boundary_date]
    monkeypatch.setattr(main.term_dates, "checkable_dates", lambda start, end: all_dates)
    fetch_calls = _install_fake_scraper(monkeypatch, {d: _raw() for d in all_dates})

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert fetch_calls == all_dates


def test_dispatch_never_exceeds_parallel_dates_in_flight(monkeypatch):
    # Cheap invariant check: never more than PARALLEL_DATES concurrently
    # in flight. Only asserts an upper bound, so it cannot flake.
    monkeypatch.setattr(main, "PARALLEL_DATES", 3)
    monkeypatch.setattr(main.config, "MAX_DATES", 12)

    lock = threading.Lock()
    counter = {"current": 0, "max": 0}

    def _fake_fetch(travel_date, *, artifacts_dir=None, attempts=None):
        with lock:
            counter["current"] += 1
            counter["max"] = max(counter["max"], counter["current"])
        try:
            return _raw()
        finally:
            with lock:
                counter["current"] -= 1

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert counter["max"] <= 3


# ---------------------------------------------------------------------------
# _dispatch_order — pure function, no threads (docs/plans/003-scheduler-
# and-retry-horizon.md §4.6/§8.4)
# ---------------------------------------------------------------------------


def test_dispatch_order_empty_list_is_empty():
    assert main._dispatch_order([], date(2026, 1, 1)) == []


def test_dispatch_order_single_candidate_returns_it():
    d = date(2026, 1, 1)
    assert main._dispatch_order([d], d) == [0]


def test_dispatch_order_no_candidate_in_zone_is_ascending():
    full_retry_until = date(2026, 12, 8)
    candidates = [date(2026, 9, 8), date(2026, 9, 10)]

    assert main._dispatch_order(candidates, full_retry_until) == [0, 1]


def test_dispatch_order_latest_in_zone_dispatched_first():
    full_retry_until = date(2026, 12, 8)
    # zone_start = Dec 1; Dec 3 and Dec 8 both fall in the zone — the
    # latest (Dec 8, index 2) wins and is moved to the front.
    candidates = [
        date(2026, 9, 8),
        date(2026, 12, 3),
        date(2026, 12, 8),
        date(2026, 12, 15),
    ]

    assert main._dispatch_order(candidates, full_retry_until) == [2, 0, 1, 3]


def test_dispatch_order_zone_disabled_is_ascending(monkeypatch):
    monkeypatch.setattr(main, "BOUNDARY_PRIORITY_ZONE_DAYS", 0)
    full_retry_until = date(2026, 12, 8)
    candidates = [date(2026, 9, 8), date(2026, 12, 8)]

    assert main._dispatch_order(candidates, full_retry_until) == [0, 1]


def test_dispatch_order_is_always_a_permutation():
    full_retry_until = date(2026, 12, 8)
    candidates = [
        date(2026, 9, 8),
        date(2026, 12, 3),
        date(2026, 12, 8),
        date(2026, 12, 15),
        date(2026, 12, 20),
    ]

    order = main._dispatch_order(candidates, full_retry_until)

    assert sorted(order) == list(range(len(candidates)))


# ---------------------------------------------------------------------------
# Railcard confirmation
# ---------------------------------------------------------------------------


def test_sub_threshold_price_without_railcard_confirmation_still_sends_alert(monkeypatch):
    # Per explicit decision, alerting no longer requires a positively
    # confirmed 16-25 railcard discount — any unbooked fare under
    # threshold alerts, confirmed or not (see src/parser.py's module
    # docstring). This stubs parser.parse_journeys directly with a
    # hand-built unconfirmed option to exercise that path.
    travel_date = date(2026, 9, 8)
    unconfirmed_option = _option(price=Decimal("5.00"), railcard_applied=False, departure_time="07:25")
    _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    monkeypatch.setattr(main.parser, "parse_journeys", lambda raw, d: [unconfirmed_option])
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(send_calls) == 1
    matches = send_calls[0]["matches"]
    assert matches[0].option.price == Decimal("5.00")
    assert matches[0].option.railcard_applied is False


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


def test_booked_date_is_scraped_and_logged_but_never_alerted(
    monkeypatch, _empty_booked_dates, _isolated_price_log
):
    # A booked date must still be scraped and appended to price-history.csv
    # (so the website's price column keeps updating for it) — only
    # ALERTING is suppressed for it. Priced well under threshold, so this
    # would alert if the date weren't booked.
    booked_date = TERM_TIME_DAY + datetime_module.timedelta(days=1)
    _empty_booked_dates.write_text(f"{booked_date.isoformat()}\n", encoding="utf-8")
    raw = _raw(_journey(booked_date, "07:25", "08:26", Decimal("5.00")))
    fetch_calls = _install_fake_scraper(monkeypatch, {booked_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert fetch_calls == [booked_date]
    assert send_calls == []
    assert booked_date.isoformat() in _isolated_price_log.read_text(encoding="utf-8")


def test_all_candidates_booked_still_scraped_but_no_alert(monkeypatch, _empty_booked_dates):
    all_candidates = main.term_dates.checkable_dates(
        TERM_TIME_DAY + datetime_module.timedelta(days=1), main.term_dates.LAST_KNOWN_DATE
    )
    _empty_booked_dates.write_text(
        "\n".join(d.isoformat() for d in all_candidates), encoding="utf-8"
    )
    fetch_calls = _install_fake_scraper(monkeypatch, {d: _raw() for d in all_candidates})
    send_calls = _install_fake_notifier(monkeypatch)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert set(fetch_calls) == set(all_candidates)
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

    def _fake_fetch(travel_date, *, artifacts_dir=None, attempts=None):
        fetch_calls.append(travel_date)
        return _raw()

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(fetch_calls) == 1


def test_first_day_of_autumn_term_has_102_candidates(monkeypatch):
    fetch_calls: list[date] = []

    def _fake_fetch(travel_date, *, artifacts_dir=None, attempts=None):
        fetch_calls.append(travel_date)
        return _raw()

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)

    result = main.main(today=date(2026, 8, 31))

    assert result == 0
    assert len(fetch_calls) == 102


# ---------------------------------------------------------------------------
# FULL_RETRY_HORIZON_DAYS / speculative attempts (plan 002 §4.3, §9)
# ---------------------------------------------------------------------------


def test_speculative_zone_dates_get_a_single_attempt(monkeypatch):
    # in_range_date is within FULL_RETRY_HORIZON_DAYS of "today" and must
    # get the full attempts=3 retry budget; speculative_date is beyond it
    # and must get only attempts=1 (main.SPECULATIVE_ATTEMPTS).
    in_range_date = TERM_TIME_DAY + datetime_module.timedelta(days=1)
    speculative_date = TERM_TIME_DAY + datetime_module.timedelta(
        days=main.FULL_RETRY_HORIZON_DAYS + 1
    )
    monkeypatch.setattr(
        main.term_dates,
        "checkable_dates",
        lambda start, end: [in_range_date, speculative_date],
    )

    attempts_by_date: dict[date, int] = {}

    def _fake_fetch(travel_date, *, artifacts_dir=None, attempts=None):
        attempts_by_date[travel_date] = attempts
        return _raw()

    monkeypatch.setattr(main.scraper, "fetch_journey_search", _fake_fetch)
    _install_fake_notifier(monkeypatch)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert attempts_by_date[in_range_date] == 3
    assert attempts_by_date[speculative_date] == main.SPECULATIVE_ATTEMPTS


def test_speculative_zone_dates_are_still_checked_and_logged(monkeypatch, _isolated_price_log):
    # Reduced retries must not mean reduced coverage: a date beyond
    # FULL_RETRY_HORIZON_DAYS that *does* return a real cheap fare is
    # still fetched, still written to price-history.csv, and still
    # alerts — the hard constraint in plan 002 §4.
    #
    # FULL_RETRY_HORIZON_DAYS + 1 (96) days out from TERM_TIME_DAY (early
    # September) lands in mid December — outside BST — so, unlike other
    # tests in this file, the journey's timestamps can't use
    # _journey()/_iso()'s hardcoded +01:00 offset (that would shift 07:25
    # to 06:25 once converted to Europe/London and fail to match
    # config.TARGET_DEPARTURES). Build the raw journey directly with the
    # correct GMT (+00:00) offset.
    speculative_date = TERM_TIME_DAY + datetime_module.timedelta(
        days=main.FULL_RETRY_HORIZON_DAYS + 1
    )
    monkeypatch.setattr(
        main.term_dates, "checkable_dates", lambda start, end: [speculative_date]
    )
    date_str = speculative_date.isoformat()
    raw = _raw(
        {
            "id": 1,
            "timetable": {
                "scheduled": {
                    "departure": f"{date_str}T07:25:00+00:00",
                    "arrival": f"{date_str}T08:26:00+00:00",
                }
            },
            "legs": [{}],
            "fares": [
                {
                    "typeDescription": "Advance Single",
                    "railcardFares": [
                        {"code": config.RAILCARD_CODE, "prices": {"adult": 870, "child": 0}}
                    ],
                }
            ],
        }
    )
    fetch_calls = _install_fake_scraper(monkeypatch, {speculative_date: raw})
    send_calls = _install_fake_notifier(monkeypatch)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert fetch_calls == [speculative_date]
    content = _isolated_price_log.read_text(encoding="utf-8")
    assert speculative_date.isoformat() in content
    assert len(send_calls) == 1
    matches = send_calls[0]["matches"]
    assert matches[0].travel_date == speculative_date


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
    matches = main.evaluate({travel_date: {"07:25": option}})

    assert matches == []


def test_evaluate_deduplicates_same_date_and_departure_time():
    travel_date = date(2026, 9, 8)
    option = _option(departure_time="07:25")
    # Same option, duplicated under two different target-time keys — the
    # dedup key is the option's OWN departure_time, not the dict key.
    matches = main.evaluate({travel_date: {"07:25": option, "07:30": option}})

    assert len(matches) == 1


def test_evaluate_alerts_even_when_railcard_unconfirmed():
    # Per explicit decision, a sub-threshold price alerts regardless of
    # whether the 16-25 railcard discount was positively confirmed — see
    # src/parser.py's module docstring. railcard_applied is carried
    # through as informational metadata only.
    travel_date = date(2026, 9, 8)
    option = _option(railcard_applied=False)
    matches = main.evaluate({travel_date: {"07:25": option}})

    assert len(matches) == 1
    assert matches[0].option.railcard_applied is False


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


def test_test_run_best_effort_includes_railcard_unconfirmed_fares(monkeypatch):
    # TEST_RUN's "send the cheapest real fare found" fallback no longer
    # filters on railcard_applied — an unconfirmed-discount fare is just
    # as real as a confirmed one (see src/parser.py's module docstring).
    monkeypatch.setattr(main.config, "TEST_RUN", True)
    travel_date = date(2026, 9, 8)
    unconfirmed_option = _option(price=Decimal("45.00"), railcard_applied=False, departure_time="07:25")
    _install_fake_scraper(monkeypatch, {travel_date: _raw()})
    monkeypatch.setattr(main.parser, "parse_journeys", lambda raw, d: [unconfirmed_option])
    send_calls = _install_fake_notifier(monkeypatch)
    monkeypatch.setattr(main.config, "MAX_DATES", 1)

    result = main.main(today=TERM_TIME_DAY)

    assert result == 0
    assert len(send_calls) == 1
    matches = send_calls[0]["matches"]
    assert matches[0].option.price == Decimal("45.00")
    assert matches[0].option.railcard_applied is False


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
