"""Parses TransPennine Express' raw journey-plan JSON (as captured by
src.scraper) into TrainOption records.

See docs/plans/005-migrate-to-tpe.md Task 2 and
tests/fixtures/journey_plan_sample.json for the real response shape this
was written against: a top-level {"links": {...}, "result": {"outward":
[...]}} JSON:API-style graph. Each entry in result.outward carries a
"journey" ref (resolved via `links` into an object with
origin/destination "time.scheduledTime" naive-local timestamps, "changes"
and "legs") and a "fares" object whose "singles"/"cheapest.outwardSingle"
are themselves refs into `links`, resolving to fare objects each carrying
its own already-railcard-discounted "totalPrice" (in pence) and a
"tickets" array recording which railcard (if any) applied.

Price is the plain minimum "totalPrice" across every candidate single
fare — TPE returns one already-discounted price per fare, unlike NRE's
separate undiscounted/railcard-discounted pair, so there is no dual-
measure comparison to do here (see _best_fare). `railcard_applied` still
records whether the WINNING fare's ticket carried config.RAILCARD_CODE,
purely as information carried through to the CSV log and the email (see
src/notifier.py) — it does not gate whether that price counts (see
CLAUDE.md's Route details -> Alert threshold).
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
    TPE likely changed schema and a human must look.
    """


def _resolve(links: dict[str, Any], ref: Any) -> Any | None:
    """Resolve a single ref through `links`, or None if `ref` isn't a
    string or isn't present. Refs are percent-encoded strings used
    verbatim as dict keys — never unquoted. A missing ref is logged at
    warning level by the caller, not here (this stays a pure lookup).
    """
    if not isinstance(ref, str):
        return None
    return links.get(ref)


def _to_hhmm(ts: str | None) -> str | None:
    """Convert a TPE "scheduledTime" timestamp to "HH:MM".

    TPE's scheduledTime is naive UK local wall-clock time (e.g.
    "2026-12-18T07:25:00", confirmed against the real fixture — see
    docs/plans/005-migrate-to-tpe.md Task 2 item 4), so this is a plain
    `strftime`, deliberately with NO `.astimezone()` call. Calling
    `.astimezone(config.LONDON)` on a naive value would silently
    reinterpret it as being in the *runner's* local timezone before
    converting to London time, mis-shifting the result whenever the
    runner isn't itself UK-local (which GitHub Actions runners are not).
    If a future TPE response ever *does* carry a UTC offset,
    `datetime.fromisoformat` still parses it fine and `.strftime` still
    yields that offset's local wall-clock time unchanged, which is
    exactly what we want to display — so this is safe either way.

    Returns None for falsy input or an unparsable value.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except ValueError:
        return None


def _best_fare(
    entry: dict[str, Any], links: dict[str, Any]
) -> tuple[str | None, Decimal, bool] | None:
    """Find the single cheapest single fare for `entry`, resolving every
    candidate ref through `links`.

    Candidates are entry["fares"]["singles"] plus
    entry["fares"]["cheapest"]["outwardSingle"] if present, deduplicated
    while preserving order (the latter is belt-and-braces for the case
    where it isn't already included in "singles" — in the real fixture it
    always is). "returns" is ignored entirely: this project is one-way
    only. Any ref that fails to resolve, or whose fare has no integer
    "totalPrice", is skipped with a warning rather than raising. Ties are
    broken by first-candidate-in-order (deterministic).

    Returns (fare_name, price, railcard_applied), or None if nothing
    resolves to a price at all.
    """
    fares = entry.get("fares") or {}
    candidates: list[str] = []
    for ref in fares.get("singles", None) or ():
        if ref not in candidates:
            candidates.append(ref)
    cheapest_ref = (fares.get("cheapest") or {}).get("outwardSingle")
    if cheapest_ref is not None and cheapest_ref not in candidates:
        candidates.append(cheapest_ref)

    best_price: Decimal | None = None
    best_fare_obj: dict[str, Any] | None = None

    for ref in candidates:
        fare = _resolve(links, ref)
        if fare is None:
            logger.warning("fare ref not found in links, skipping: %r", ref)
            continue
        total_price = fare.get("totalPrice")
        if not isinstance(total_price, int):
            continue
        price = Decimal(total_price) / PENCE_PER_POUND
        if best_price is None or price < best_price:
            best_price, best_fare_obj = price, fare

    if best_price is None or best_fare_obj is None:
        return None

    ticket_type_ref = best_fare_obj.get("ticketType")
    ticket_type = _resolve(links, ticket_type_ref)
    fare_name = ticket_type.get("name") if isinstance(ticket_type, dict) else None

    railcard_applied = False
    railcard_code = config.RAILCARD_CODE
    for ticket in best_fare_obj.get("tickets", None) or ():
        railcard_ref = ticket.get("railcard")
        if isinstance(railcard_ref, str) and railcard_ref.rsplit("/", 1)[-1] == railcard_code:
            railcard_applied = True
            break

    return fare_name, best_price, railcard_applied


def extract_price(entry: dict[str, Any], links: dict[str, Any]) -> Decimal | None:
    """The single cheapest price found on `entry`, or None if it has no
    priced single fare at all. Kept pure and separately tested — see
    _best_fare, which also needs the fare's name and railcard status.

    Note the signature: unlike NRE's version, this needs `links` to
    resolve fare refs — `entry` alone (one item of result.outward) is not
    self-contained under TPE's ref-indirection schema. src.main never
    calls this directly (only src.parser.parse_journeys does, via
    _best_fare); only tests call extract_price on its own.
    """
    found = _best_fare(entry, links)
    return found[1] if found is not None else None


def parse_journeys(raw: dict[str, Any], travel_date: date) -> list[TrainOption]:
    """Parse a raw journey-plan response into one TrainOption per outward
    entry.

    Raises ParseError if `raw` doesn't even have the top-level
    {"result": {"outward": [...]}, "links": {...}} container shape —
    that's a schema change, not a data condition. An individual outward
    entry that fails to parse (e.g. its journey ref doesn't resolve, or
    its departure timestamp is unparsable) is logged and skipped rather
    than failing the whole date.
    """
    if not isinstance(raw, dict):
        raise ParseError(
            f"response is not a dict ({type(raw).__name__}) — TPE may have "
            "changed its response schema"
        )
    result = raw.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("outward"), list):
        raise ParseError(
            "response missing top-level 'result.outward' list — TPE may "
            "have changed its response schema"
        )
    links = raw.get("links")
    if not isinstance(links, dict):
        raise ParseError(
            "response missing top-level 'links' dict — TPE may have "
            "changed its response schema"
        )

    options: list[TrainOption] = []
    for entry in result["outward"]:
        journey_ref = entry.get("journey")
        journey = _resolve(links, journey_ref)
        if journey is None:
            logger.warning(
                "[%s] skipping outward entry with unresolvable journey ref: %r",
                travel_date.isoformat(),
                journey_ref,
            )
            continue

        origin_time = (journey.get("origin") or {}).get("time") or {}
        departure_time = _to_hhmm(origin_time.get("scheduledTime"))
        if departure_time is None:
            logger.warning(
                "[%s] skipping journey with unparsable departure time: %r",
                travel_date.isoformat(),
                journey_ref,
            )
            continue

        destination_time = (journey.get("destination") or {}).get("time") or {}
        arrival_time = _to_hhmm(destination_time.get("scheduledTime"))

        changes = journey.get("changes")
        legs = journey.get("legs") or ()
        is_direct = changes == 0 if isinstance(changes, int) else len(legs) <= 1

        best_fare = _best_fare(entry, links)
        if best_fare is not None:
            fare_name, price, railcard_applied = best_fare
        else:
            fare_name, price, railcard_applied = None, None, False
        sold_out = price is None

        options.append(
            TrainOption(
                travel_date=travel_date,
                departure_time=departure_time,
                arrival_time=arrival_time,
                price=price,
                # No currency field appears anywhere in the real captured
                # response (checked directly against the committed
                # fixture) — TPE only ever quotes GBP, so this is a fixed
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
