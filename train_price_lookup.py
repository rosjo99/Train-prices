#!/usr/bin/env python3
"""
Looks up UK train prices via Trainline's EU API.
Plain requests — no browser automation needed.

Setup:
    pip install --user requests
"""

import argparse
import json
import sys
import uuid
from datetime import datetime

import requests

RAILCARD_DISCOUNT = {
    "16-25": 1 / 3,
    "26-30": 1 / 3,
    "senior": 1 / 3,
    "two-together": 1 / 3,
    "network": 1 / 3,
    "none": 0,
}

# trainline.eu railcard IDs (from their internal DB)
RAILCARD_IDS = {
    "16-25": "railcard-16-25",
    "26-30": "railcard-26-30",
    "senior": "railcard-senior",
    "network": "railcard-network",
    "two-together": "railcard-two-together",
}

PEAK_MIN_FARE_RAILCARDS = {"16-25", "26-30"}
PEAK_MIN_FARE = 12.00

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.thetrainline.com",
    "Referer": "https://www.thetrainline.com/",
    "x-version": "4.19.2",
}


def search_station(session: requests.Session, term: str, debug: bool = False) -> dict:
    """Find a station using Trainline's location search."""
    # try the thetrainline.com locations API first (public, no auth)
    url = f"https://www.thetrainline.com/api/locations-service/v2/search?searchTerm={term}&limit=5"
    resp = session.get(url, headers=HEADERS)

    if debug:
        print(f"[debug] station search '{term}': HTTP {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        hits = data.get("searchLocations", [])
        if hits:
            loc = hits[0]
            if debug:
                print(f"[debug] found: {loc.get('name')} (urn: {loc.get('urn')})")
            return loc

    # fallback: trainline.eu stations endpoint
    url2 = f"https://www.trainline.eu/api/v5/stations?term={term}"
    resp2 = session.get(url2, headers=HEADERS)

    if debug:
        print(f"[debug] EU station search '{term}': HTTP {resp2.status_code}")

    if resp2.status_code == 200:
        data = resp2.json()
        stations = data.get("stations", [])
        if stations:
            st = stations[0]
            if debug:
                print(f"[debug] found: {st.get('name')} (id: {st.get('id')})")
            return st

    print(f"error: could not find station '{term}'", file=sys.stderr)
    sys.exit(1)


def search_journeys_eu(
    session: requests.Session,
    station_from_id: str,
    station_to_id: str,
    departure_date: str,
    railcard: str,
    debug: bool = False,
) -> dict | None:
    """Try the trainline.eu v5_1 search endpoint."""
    passenger_id = str(uuid.uuid4())
    cards = []
    if railcard != "none" and railcard in RAILCARD_IDS:
        cards = [{"id": RAILCARD_IDS[railcard]}]

    payload = {
        "search": {
            "departure_date": departure_date,
            "return_date": None,
            "passengers": [
                {
                    "id": passenger_id,
                    "label": "adult",
                    "age": 22,
                    "cards": cards,
                }
            ],
            "systems": ["atoc"],
            "exchangeable_part": None,
            "departure_station_id": station_from_id,
            "via_station_id": None,
            "arrival_station_id": station_to_id,
            "exchangeable_pnr_id": None,
        }
    }

    url = "https://www.trainline.eu/api/v5_1/search"

    if debug:
        print(f"[debug] POST {url}")
        print(f"[debug] payload: {json.dumps(payload, indent=2)}")

    resp = session.post(url, json=payload, headers=HEADERS)

    if debug:
        print(f"[debug] HTTP {resp.status_code}, {len(resp.text)} bytes")
        if resp.status_code != 200:
            print(f"[debug] response: {resp.text[:500]}")

    if resp.status_code == 200:
        return resp.json()

    return None


def search_journeys_uk(
    session: requests.Session,
    origin_urn: str,
    dest_urn: str,
    departure_iso: str,
    railcard: str,
    debug: bool = False,
) -> dict | None:
    """Try the thetrainline.com journey-search endpoint."""
    passenger_id = str(uuid.uuid4())

    # thetrainline.com uses a different payload shape
    payload = {
        "passengers": [
            {
                "dateOfBirth": "2002-01-01",  # ~24yo for 16-25 railcard
                "id": passenger_id,
            }
        ],
        "isReturn": False,
        "transitDefinitions": [
            {
                "direction": "outward",
                "origin": origin_urn,
                "destination": dest_urn,
                "journeyDate": {
                    "type": "departAfter",
                    "time": departure_iso,
                },
            }
        ],
        "type": "single",
        "requestedCurrencyCode": "GBP",
    }

    url = "https://www.thetrainline.com/api/journey-search/"

    if debug:
        print(f"[debug] POST {url}")

    resp = session.post(url, json=payload, headers=HEADERS)

    if debug:
        print(f"[debug] HTTP {resp.status_code}, {len(resp.text)} bytes")
        if resp.status_code != 200:
            print(f"[debug] response: {resp.text[:500]}")

    if resp.status_code == 200:
        return resp.json()

    return None


def parse_eu_results(data: dict) -> list[dict]:
    """Parse trainline.eu API response into a flat list of trains."""
    trains = []
    trips = data.get("trips", [])
    folders = data.get("folders", [])
    segments = {s["id"]: s for s in data.get("segments", [])}

    for folder in folders:
        trip_ids = folder.get("trip_ids", [])
        if not trip_ids:
            continue

        # take cheapest trip in folder
        min_cents = folder.get("min_price_in_cents")
        if min_cents is None:
            continue

        price = min_cents / 100.0
        currency = folder.get("currency", "GBP")

        # get segment details from first trip
        for tid in trip_ids:
            trip = next((t for t in trips if t["id"] == tid), None)
            if not trip:
                continue

            seg_ids = trip.get("segment_ids", [])
            if not seg_ids:
                continue

            first_seg = segments.get(seg_ids[0], {})
            last_seg = segments.get(seg_ids[-1], {})

            dep = first_seg.get("departure_date", "")
            arr = last_seg.get("arrival_date", "")

            dep_time = dep[11:16] if len(dep) > 16 else "?"
            arr_time = arr[11:16] if len(arr) > 16 else "?"

            trains.append({
                "departure": dep_time,
                "arrival": arr_time,
                "dep_arr": f"{dep_time} → {arr_time}",
                "direct": len(seg_ids) == 1,
                "fare_type": "Cheapest",
                "base_price": price,
                "railcard_price": price,  # already includes railcard if specified
                "duration": "?",
            })
            break  # one per folder

    return trains


def main():
    parser = argparse.ArgumentParser(
        description="Look up UK train prices via Trainline API"
    )
    parser.add_argument("origin", help="e.g. 'oxford'")
    parser.add_argument("destination", help="e.g. 'london paddington'")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--time", default="07:00", help="departure time HH:MM (default: 07:00)"
    )
    parser.add_argument(
        "--railcard",
        choices=list(RAILCARD_DISCOUNT.keys()),
        default="none",
        help="railcard type (default: none)",
    )
    parser.add_argument("--debug", action="store_true", help="verbose output")
    args = parser.parse_args()

    print(f"Searching {args.origin} → {args.destination} on {args.date} from {args.time}")
    if args.railcard != "none":
        print(f"Railcard: {args.railcard}")
    print()

    session = requests.Session()

    # warm up session with a page visit
    if args.debug:
        print("[debug] warming up session...")
    session.get("https://www.thetrainline.com/", headers={
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html",
    })

    # resolve stations
    print("Looking up stations...")
    origin_info = search_station(session, args.origin, debug=args.debug)
    dest_info = search_station(session, args.destination, debug=args.debug)

    departure_iso = f"{args.date}T{args.time}:00+01:00"

    # try EU API first (simpler, returns prices directly)
    print("Searching for journeys...\n")

    eu_origin_id = origin_info.get("id")
    eu_dest_id = dest_info.get("id")

    if eu_origin_id and eu_dest_id:
        data = search_journeys_eu(
            session, str(eu_origin_id), str(eu_dest_id),
            departure_iso, args.railcard, debug=args.debug,
        )
        if data:
            trains = parse_eu_results(data)
            if trains:
                for t in trains:
                    direct = "direct" if t["direct"] else "changes"
                    print(
                        f"  {t['dep_arr']:<16s}  {direct:<8s}  "
                        f"£{t['base_price']:.2f} {t['fare_type']}"
                    )
                return

    # try UK API as fallback
    origin_urn = origin_info.get("urn")
    dest_urn = dest_info.get("urn")

    if origin_urn and dest_urn:
        data = search_journeys_uk(
            session, origin_urn, dest_urn,
            departure_iso, args.railcard, debug=args.debug,
        )
        if data:
            print(json.dumps(data, indent=2)[:2000])
            return

    print("No results — both API endpoints failed. Use --debug for details.")


if __name__ == "__main__":
    main()
