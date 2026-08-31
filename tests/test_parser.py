"""Tests for src.parser.

Uses the real captured fixture (tests/fixtures/journey_search_sample.json,
from scripts/capture_fixture.py against a live National Rail Enquiries
response) for the happy-path acceptance criteria, plus small hand-edited
fixture variants for specific edge cases — see
docs/plans/001-train-price-alert.md Task 4.
"""

from __future__ import annotations

import json
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


def test_real_fixture_returns_options_for_both_targets():
    raw = _load_fixture("journey_search_sample.json")
    travel_date = date(2026, 9, 8)

    options = parse_journeys(raw, travel_date)

    assert len(options) > 0
    targets = select_target_trains(options, TARGET_TIMES)
    assert targets["07:25"] is not None
    assert targets["07:30"] is not None
    # Confirmed by inspecting the real fixture directly: every one of its
    # 10 journeys carries at least one fare with a "YNG" railcardFares
    # entry, so none are sold out and every price is real.
    for option in options:
        assert isinstance(option.price, Decimal)
        assert option.railcard_applied is True
        assert option.sold_out is False


def test_real_fixture_target_departures_have_expected_times():
    raw = _load_fixture("journey_search_sample.json")
    options = parse_journeys(raw, date(2026, 9, 8))
    targets = select_target_trains(options, TARGET_TIMES)

    assert targets["07:25"].departure_time == "07:25"
    assert targets["07:25"].arrival_time == "08:26"
    assert targets["07:30"].departure_time == "07:30"
    assert targets["07:30"].arrival_time == "08:25"


# ---------------------------------------------------------------------------
# Structural failures
# ---------------------------------------------------------------------------


def test_empty_journeys_returns_empty_list_not_raise():
    raw = _load_fixture("journey_search_empty.json")

    options = parse_journeys(raw, date(2026, 9, 8))

    assert options == []


def test_missing_container_raises_parse_error():
    raw = _load_fixture("journey_search_missing_container.json")

    with pytest.raises(ParseError):
        parse_journeys(raw, date(2026, 9, 8))


@pytest.mark.parametrize("raw", [{}, [], None, "not a dict"])
def test_various_unrecognisable_shapes_raise_parse_error(raw):
    with pytest.raises(ParseError):
        parse_journeys(raw, date(2026, 9, 8))


# ---------------------------------------------------------------------------
# Sold out / railcard confirmation
# ---------------------------------------------------------------------------


def test_fareless_journey_is_sold_out_with_no_price():
    raw = _load_fixture("journey_search_fareless_journey.json")

    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.sold_out is True
    assert option.price is None
    assert option.railcard_applied is False
    assert option.fare_name is None


def test_railcard_stripped_from_fare_gives_railcard_applied_false():
    raw = _load_fixture("journey_search_no_railcard.json")

    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.railcard_applied is False
    assert option.price is None
    # The positive-confirmation rule ties sold_out to "no fare carries our
    # railcard discount", not to whether any fare exists at all — a fare
    # existing without our railcard is functionally the same "can't alert
    # on this" state as no fare existing.
    assert option.sold_out is True


def test_both_target_trains_sold_out(monkeypatch):
    raw = {
        "outwardJourneys": [
            {
                "id": 1,
                "timetable": {
                    "scheduled": {
                        "departure": "2026-09-08T07:25:00+01:00",
                        "arrival": "2026-09-08T08:26:00+01:00",
                    }
                },
                "legs": [{}],
                "fares": [],
            },
            {
                "id": 2,
                "timetable": {
                    "scheduled": {
                        "departure": "2026-09-08T07:30:00+01:00",
                        "arrival": "2026-09-08T08:25:00+01:00",
                    }
                },
                "legs": [{}],
                "fares": [],
            },
        ]
    }

    options = parse_journeys(raw, date(2026, 9, 8))
    targets = select_target_trains(options, TARGET_TIMES)

    assert targets["07:25"].sold_out is True
    assert targets["07:30"].sold_out is True


# ---------------------------------------------------------------------------
# select_target_trains
# ---------------------------------------------------------------------------


def test_select_target_trains_missing_departure_is_none():
    raw = _load_fixture("journey_search_only_0725.json")
    options = parse_journeys(raw, date(2026, 9, 8))

    targets = select_target_trains(options, TARGET_TIMES)

    assert targets["07:25"] is not None
    assert targets["07:30"] is None


def test_select_target_trains_empty_options_all_none():
    targets = select_target_trains([], TARGET_TIMES)

    assert targets == {"07:25": None, "07:30": None}


# ---------------------------------------------------------------------------
# Price / Decimal correctness
# ---------------------------------------------------------------------------


def test_price_of_999_pence_parses_to_exact_decimal_9_99():
    raw = _load_fixture("journey_search_only_0725.json")
    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.price == Decimal("9.99")
    assert type(option.price) is Decimal


def test_extract_price_pure_helper_finds_cheapest_matching_railcard_fare(monkeypatch):
    monkeypatch.setattr(parser.config, "RAILCARD_CODE", "YNG")
    journey = {
        "fares": [
            {"typeDescription": "Anytime", "railcardFares": [{"code": "YNG", "prices": {"adult": 2000}}]},
            {"typeDescription": "Advance", "railcardFares": [{"code": "YNG", "prices": {"adult": 999}}]},
            {"typeDescription": "Off-Peak", "railcardFares": [{"code": "OTHER", "prices": {"adult": 1}}]},
        ]
    }

    assert extract_price(journey) == Decimal("9.99")


def test_extract_price_returns_none_when_no_matching_railcard(monkeypatch):
    monkeypatch.setattr(parser.config, "RAILCARD_CODE", "YNG")
    journey = {"fares": [{"typeDescription": "Anytime", "railcardFares": []}]}

    assert extract_price(journey) is None


def test_extract_price_handles_missing_fares_key():
    assert extract_price({}) is None


def test_no_float_anywhere_in_module():
    source = (REPO_ROOT / "src" / "parser.py").read_text(encoding="utf-8")
    assert "float(" not in source


# ---------------------------------------------------------------------------
# Missing optional fields
# ---------------------------------------------------------------------------


def test_missing_arrival_timetable_is_none_not_a_crash():
    raw = _load_fixture("journey_search_only_0725.json")
    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.arrival_time is None
    assert option.departure_time == "07:25"


def test_journey_with_unparsable_departure_is_skipped(caplog):
    raw = {
        "outwardJourneys": [
            {"id": 1, "timetable": {"scheduled": {"departure": None, "arrival": None}}, "legs": [], "fares": []},
            {
                "id": 2,
                "timetable": {
                    "scheduled": {
                        "departure": "2026-09-08T07:30:00+01:00",
                        "arrival": "2026-09-08T08:25:00+01:00",
                    }
                },
                "legs": [{}],
                "fares": [],
            },
        ]
    }

    options = parse_journeys(raw, date(2026, 9, 8))

    assert len(options) == 1
    assert options[0].departure_time == "07:30"


# ---------------------------------------------------------------------------
# is_direct
# ---------------------------------------------------------------------------


def test_is_direct_true_for_single_leg_journey():
    raw = _load_fixture("journey_search_only_0725.json")
    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.is_direct is True


def test_is_direct_false_for_multi_leg_journey():
    raw = {
        "outwardJourneys": [
            {
                "id": 1,
                "timetable": {
                    "scheduled": {
                        "departure": "2026-09-08T07:25:00+01:00",
                        "arrival": "2026-09-08T09:00:00+01:00",
                    }
                },
                "legs": [{}, {}],
                "fares": [],
            }
        ]
    }

    [option] = parse_journeys(raw, date(2026, 9, 8))

    assert option.is_direct is False


# ---------------------------------------------------------------------------
# Timezone handling (winter, no BST offset)
# ---------------------------------------------------------------------------


def test_winter_offset_still_parses_correctly():
    raw = {
        "outwardJourneys": [
            {
                "id": 1,
                "timetable": {
                    "scheduled": {
                        "departure": "2027-01-05T07:25:00+00:00",
                        "arrival": "2027-01-05T08:26:00+00:00",
                    }
                },
                "legs": [{}],
                "fares": [],
            }
        ]
    }

    [option] = parse_journeys(raw, date(2027, 1, 5))

    assert option.departure_time == "07:25"
    assert option.arrival_time == "08:26"
