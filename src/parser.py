"""Parses National Rail Enquiries' raw journey-planner JSON (as captured
by src.scraper) into TrainOption records.

See docs/plans/001-train-price-alert.md Task 4 and
tests/fixtures/journey_search_sample.json for the real response shape
this was written against: a top-level "outwardJourneys" list, each
journey carrying "timetable.scheduled.{departure,arrival}" as full ISO
8601 timestamps (own UTC offset already applied) and a "fares" list,
each fare option optionally carrying a "railcardFares" array of
{code, prices: {adult, child}} entries.

Deliberately price-per-railcard, not price-per-journey: CLAUDE.md
requires the 16-25 railcard discount to be positively confirmed before
any alert fires, so this module only ever reports the price of a fare
that has a matching "railcardFares" entry — never a journey's cheapest
fare overall, which might not carry a discount at all.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from src import config
from src.models import TrainOption

logger = logging.getLogger(__name__)

PENCE_PER_POUND = Decimal("100")


class ParseError(Exception):
    """Raised when the raw response is structurally unrecognisable —
    NRE likely changed schema and a human must look.
    """


def _to_london_hhmm(iso_timestamp: str | None) -> str | None:
    """Convert a full ISO 8601 timestamp (with its own UTC offset, e.g.
    "2026-09-08T07:25:00+01:00") to "HH:MM" in Europe/London. The offset
    is NOT assumed to always be +01:00 (BST) — outside BST it's +00:00,
    and .astimezone(config.LONDON) handles both correctly.
    """
    if not iso_timestamp:
        return None
    try:
        return datetime.fromisoformat(iso_timestamp).astimezone(config.LONDON).strftime("%H:%M")
    except ValueError:
        return None


def _find_railcard_fare(journey: dict[str, Any]) -> tuple[str | None, Decimal] | None:
    """Find the cheapest fare option on `journey` that has a
    "railcardFares" entry matching config.RAILCARD_CODE.

    Returns (fare_name, price) for the cheapest such fare, or None if no
    fare on this journey carries that railcard's discount at all — the
    positive-confirmation signal CLAUDE.md requires, never inferred from
    the request having asked for a railcard.
    """
    best: tuple[str | None, Decimal] | None = None
    for fare in journey.get("fares", None) or ():
        for railcard_fare in fare.get("railcardFares", None) or ():
            if railcard_fare.get("code") != config.RAILCARD_CODE:
                continue
            try:
                pence = railcard_fare["prices"]["adult"]
            except (KeyError, TypeError):
                continue
            price = Decimal(pence) / PENCE_PER_POUND
            if best is None or price < best[1]:
                best = (fare.get("typeDescription"), price)
    return best


def extract_price(journey: dict[str, Any]) -> Decimal | None:
    """The cheapest config.RAILCARD_CODE-discounted price on `journey`,
    or None if no fare carries that discount. Kept pure and separately
    tested — see _find_railcard_fare, which also needs the fare's name.
    """
    found = _find_railcard_fare(journey)
    return found[1] if found is not None else None


def parse_journeys(raw: dict[str, Any], travel_date: date) -> list[TrainOption]:
    """Parse a raw journey-planner response into one TrainOption per
    outbound journey.

    Raises ParseError if `raw` doesn't even have the top-level
    "outwardJourneys" container — that's a schema change, not a data
    condition. An individual journey entry that fails to parse (e.g. an
    unparsable departure timestamp) is logged and skipped rather than
    failing the whole date, since a schema change on the ORIGIN
    (departure_time is a required field, unlike arrival_time) is not the
    kind of change a real journey should normally hit day to day.
    """
    if not isinstance(raw, dict) or "outwardJourneys" not in raw:
        raise ParseError(
            "response missing top-level 'outwardJourneys' — NRE may have "
            "changed its response schema"
        )

    options: list[TrainOption] = []
    for journey in raw["outwardJourneys"]:
        scheduled = (journey.get("timetable") or {}).get("scheduled") or {}
        departure_time = _to_london_hhmm(scheduled.get("departure"))
        if departure_time is None:
            logger.warning(
                "[%s] skipping journey with unparsable departure timetable: %r",
                travel_date.isoformat(),
                journey.get("id"),
            )
            continue
        arrival_time = _to_london_hhmm(scheduled.get("arrival"))

        railcard_fare = _find_railcard_fare(journey)
        fare_name = railcard_fare[0] if railcard_fare is not None else None
        price = railcard_fare[1] if railcard_fare is not None else None
        railcard_applied = railcard_fare is not None
        sold_out = not railcard_applied

        legs = journey.get("legs") or ()
        is_direct = len(legs) <= 1

        options.append(
            TrainOption(
                travel_date=travel_date,
                departure_time=departure_time,
                arrival_time=arrival_time,
                price=price,
                # No currency field appears anywhere in the real captured
                # response (checked directly against the committed
                # fixture) — NRE only ever quotes GBP, so this is a fixed
                # value, not an assumption made without checking.
                currency="GBP",
                railcard_applied=railcard_applied,
                is_direct=is_direct,
                sold_out=sold_out,
                fare_name=fare_name,
            )
        )
    return options


def select_target_trains(
    options: list[TrainOption], target_times: tuple[str, ...]
) -> dict[str, TrainOption | None]:
    """Exact "HH:MM" match of `options` against `target_times`.

    Returns a dict keyed by every entry in target_times, value None for
    any target departure absent from `options`. If more than one option
    shares a departure time (not expected in practice), the first one
    encountered wins.
    """
    by_time: dict[str, TrainOption] = {}
    for option in options:
        by_time.setdefault(option.departure_time, option)
    return {target: by_time.get(target) for target in target_times}
