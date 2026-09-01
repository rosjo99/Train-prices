"""Tests for src.parser.

Uses the real captured fixture (tests/fixtures/journey_plan_sample.json,
from scripts/capture_fixture_tpe.py against a live TransPennine Express
response) for the happy-path acceptance criteria, plus small hand-edited
fixture variants for specific edge cases, and small hand-built {links,
result} graphs for the ref-indirection unit tests — see
docs/plans/005-migrate-to-tpe.md Task 2/§6.1.
"""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src import parser
from src.parser import ParseError, extract_price, parse_journeys, select_target_trains

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent

TARGET_TIMES = ("07:25", "07:30")


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Real fixture, happy path
# ---------------------------------------------------------------------------


def test_real_fixture_returns_options_for_both_targets_and_the_third():
    raw = _load_fixture("journey_plan_sample.json")
    travel_date = date(2026, 12, 18)

    options = parse_journeys(raw, travel_date)

    assert len(options) == 3
    targets = select_target_trains(options, TARGET_TIMES)
    assert targets["07:25"] is not None
    assert targets["07:30"] is not None
    # The 07:53 third journey in the real fixture must still parse, and
    # select_target_trains (only matching TARGET_TIMES) must ignore it.
    departure_times = {option.departure_time for option in options}
    assert departure_times == {"07:25", "07:30", "07:53"}


def test_real_fixture_target_departures_have_expected_values():
    raw = _load_fixture("journey_plan_sample.json")
    options = parse_journeys(raw, date(2026, 12, 18))
    targets = select_target_trains(options, TARGET_TIMES)

    for key, expected_arrival in (("07:25", "08:27"), ("07:30", "08:25")):
        option = targets[key]
        assert option.departure_time == key
        assert option.arrival_time == expected_arrival
        assert option.price == Decimal("9.30")
        assert option.railcard_applied is True
        assert option.sold_out is False
        assert option.fare_name == "Advance Single"
        assert option.is_direct is True


# ---------------------------------------------------------------------------
# Structural failures
# ---------------------------------------------------------------------------


def test_empty_journeys_returns_empty_list_not_raise():
    raw = _load_fixture("journey_plan_empty.json")

    options = parse_journeys(raw, date(2026, 12, 18))

    assert options == []


def test_missing_container_raises_parse_error():
    raw = _load_fixture("journey_plan_missing_container.json")

    with pytest.raises(ParseError):
        parse_journeys(raw, date(2026, 12, 18))


@pytest.mark.parametrize("raw", [{}, [], None, "not a dict"])
def test_various_unrecognisable_shapes_raise_parse_error(raw):
    with pytest.raises(ParseError):
        parse_journeys(raw, date(2026, 12, 18))


# ---------------------------------------------------------------------------
# Sold out / railcard confirmation
# ---------------------------------------------------------------------------


def test_fareless_journey_is_sold_out_with_no_price():
    raw = _load_fixture("journey_plan_fareless_journey.json")

    [option] = parse_journeys(raw, date(2026, 12, 18))

    assert option.sold_out is True
    assert option.price is None
    assert option.railcard_applied is False
    assert option.fare_name is None


def test_no_railcard_still_returns_prices_but_railcard_not_applied():
    raw = _load_fixture("journey_plan_no_railcard.json")

    options = parse_journeys(raw, date(2026, 12, 18))

    assert len(options) == 3
    for option in options:
        assert option.price == Decimal("9.30")
        assert option.railcard_applied is False
        assert option.sold_out is False


def test_both_target_trains_sold_out():
    raw = {
        "links": {
            "/jp/journeys/a": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:25:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T08:26:00"}},
            },
            "/jp/journeys/b": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:30:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T08:25:00"}},
            },
        },
        "result": {
            "outward": [
                {"journey": "/jp/journeys/a", "fares": {"singles": [], "returns": []}},
                {"journey": "/jp/journeys/b", "fares": {"singles": [], "returns": []}},
            ]
        },
    }

    options = parse_journeys(raw, date(2026, 9, 8))
    targets = select_target_trains(options, TARGET_TIMES)

    assert targets["07:25"].sold_out is True
    assert targets["07:30"].sold_out is True


# ---------------------------------------------------------------------------
# select_target_trains
# ---------------------------------------------------------------------------


def test_select_target_trains_missing_departure_is_none():
    raw = _load_fixture("journey_plan_only_0725.json")
    options = parse_journeys(raw, date(2026, 12, 18))

    targets = select_target_trains(options, TARGET_TIMES)

    assert targets["07:25"] is not None
    assert targets["07:30"] is None


def test_select_target_trains_empty_options_all_none():
    targets = select_target_trains([], TARGET_TIMES)

    assert targets == {"07:25": None, "07:30": None}


# ---------------------------------------------------------------------------
# is_direct / multi-leg
# ---------------------------------------------------------------------------


def test_multi_leg_journey_is_not_direct():
    raw = {
        "links": {
            "/jp/journeys/multi": {
                "changes": 1,
                "legs": [{}, {}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:25:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T09:00:00"}},
            },
        },
        "result": {"outward": [{"journey": "/jp/journeys/multi", "fares": {"singles": [], "returns": []}}]},
    }

    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.is_direct is False


# ---------------------------------------------------------------------------
# Ref-indirection edge cases
# ---------------------------------------------------------------------------


def _fare(price: int, ticket_type_ref: str | None = "/data/ticket-types/W2M", railcard_ref: str | None = "/data/railcards/YNG") -> dict:
    fare = {"totalPrice": price}
    if ticket_type_ref is not None:
        fare["ticketType"] = ticket_type_ref
    tickets = []
    if railcard_ref is not None:
        tickets.append({"adults": 1, "children": 0, "price": price, "railcard": railcard_ref})
    else:
        tickets.append({"adults": 1, "children": 0, "price": price})
    fare["tickets"] = tickets
    return fare


def test_singles_ref_missing_from_links_is_skipped_with_warning(caplog):
    raw = {
        "links": {
            "/jp/journeys/a": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:25:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T08:26:00"}},
            },
            "/data/ticket-types/W2M": {"name": "Advance Single"},
            "/jp/fares/present": _fare(999),
        },
        "result": {
            "outward": [
                {
                    "journey": "/jp/journeys/a",
                    "fares": {"singles": ["/jp/fares/missing", "/jp/fares/present"], "returns": []},
                }
            ]
        },
    }

    with caplog.at_level("WARNING"):
        [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.price == Decimal("9.99")
    assert any("fare ref not found" in message for message in caplog.messages)


def test_ticket_type_ref_missing_from_links_price_still_returned():
    raw = {
        "links": {
            "/jp/journeys/a": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:25:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T08:26:00"}},
            },
            "/jp/fares/present": _fare(999, ticket_type_ref="/data/ticket-types/MISSING"),
        },
        "result": {
            "outward": [
                {"journey": "/jp/journeys/a", "fares": {"singles": ["/jp/fares/present"], "returns": []}}
            ]
        },
    }

    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.price == Decimal("9.99")
    assert option.fare_name is None


def test_fare_with_different_railcard_ref_is_not_railcard_applied():
    raw = {
        "links": {
            "/jp/journeys/a": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:25:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T08:26:00"}},
            },
            "/data/ticket-types/W2M": {"name": "Advance Single"},
            "/jp/fares/present": _fare(999, railcard_ref="/data/railcards/OTHER"),
        },
        "result": {
            "outward": [
                {"journey": "/jp/journeys/a", "fares": {"singles": ["/jp/fares/present"], "returns": []}}
            ]
        },
    }

    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.price == Decimal("9.99")
    assert option.railcard_applied is False


def test_journey_ref_absent_from_links_is_skipped_others_still_parse(caplog):
    raw = {
        "links": {
            "/jp/journeys/b": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:30:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T08:25:00"}},
            },
        },
        "result": {
            "outward": [
                {"journey": "/jp/journeys/missing", "fares": {"singles": [], "returns": []}},
                {"journey": "/jp/journeys/b", "fares": {"singles": [], "returns": []}},
            ]
        },
    }

    with caplog.at_level("WARNING"):
        options = parse_journeys(raw, date(2026, 9, 8))

    assert len(options) == 1
    assert options[0].departure_time == "07:30"


def test_journey_with_unparsable_departure_is_skipped():
    raw = {
        "links": {
            "/jp/journeys/bad": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": None}},
                "destination": {"time": {}},
            },
            "/jp/journeys/good": {
                "changes": 0,
                "legs": [{}],
                "origin": {"time": {"scheduledTime": "2026-09-08T07:30:00"}},
                "destination": {"time": {"scheduledTime": "2026-09-08T08:25:00"}},
            },
        },
        "result": {
            "outward": [
                {"journey": "/jp/journeys/bad", "fares": {"singles": [], "returns": []}},
                {"journey": "/jp/journeys/good", "fares": {"singles": [], "returns": []}},
            ]
        },
    }

    options = parse_journeys(raw, date(2026, 9, 8))

    assert len(options) == 1
    assert options[0].departure_time == "07:30"


# ---------------------------------------------------------------------------
# extract_price(entry, links) direct tests
# ---------------------------------------------------------------------------


def test_extract_price_cheapest_of_several_singles_wins(monkeypatch):
    monkeypatch.setattr(parser.config, "RAILCARD_CODE", "YNG")
    links = {
        "/jp/fares/a": _fare(2000),
        "/jp/fares/b": _fare(999),
        "/jp/fares/c": _fare(1500),
    }
    entry = {"fares": {"singles": ["/jp/fares/a", "/jp/fares/b", "/jp/fares/c"], "returns": []}}

    assert extract_price(entry, links) == Decimal("9.99")


def test_extract_price_ignores_returns_even_when_cheaper():
    links = {
        "/jp/fares/single": _fare(2000),
        "/jp/fares/return_cheap": _fare(1),
    }
    entry = {
        "fares": {
            "singles": ["/jp/fares/single"],
            "returns": ["/jp/fares/return_cheap"],
        }
    }

    assert extract_price(entry, links) == Decimal("20.00")


def test_extract_price_none_when_nothing_resolves():
    entry = {"fares": {"singles": ["/jp/fares/missing"], "returns": []}}

    assert extract_price(entry, {}) is None


def test_extract_price_handles_missing_fares_key():
    assert extract_price({}, {}) is None


def test_no_float_anywhere_in_module():
    source = (REPO_ROOT / "src" / "parser.py").read_text(encoding="utf-8")
    assert "float(" not in source


# ---------------------------------------------------------------------------
# Naive-timestamp handling (the load-bearing regression guard for the
# NRE-era `.astimezone()` bug class — see src.parser._to_hhmm's docstring)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "iso_ts,expected",
    [
        ("2026-07-08T07:25:00", "07:25"),  # summer (BST would apply to an aware value)
        ("2027-01-05T07:25:00", "07:25"),  # winter
    ],
)
def test_naive_timestamps_round_trip_unchanged_under_non_london_tz(
    monkeypatch, iso_ts, expected
):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        raw = {
            "links": {
                "/jp/journeys/a": {
                    "changes": 0,
                    "legs": [{}],
                    "origin": {"time": {"scheduledTime": iso_ts}},
                    "destination": {"time": {}},
                },
            },
            "result": {
                "outward": [{"journey": "/jp/journeys/a", "fares": {"singles": [], "returns": []}}]
            },
        }
        [option] = parse_journeys(raw, date(2026, 1, 1))
        assert option.departure_time == expected
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
