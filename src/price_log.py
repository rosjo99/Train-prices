"""Appends every checked price to a permanent, append-only CSV log.

Never truncates or rewrites existing rows — this is a running history of
what price was seen at what time, not a snapshot of current prices. The
file itself is committed back to the repo by the workflow after each run
(see .github/workflows/price-check.yml); this module only ever appends
to whatever local copy it's given.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from src.models import TrainOption

FIELDNAMES: tuple[str, ...] = (
    "checked_at",
    "travel_date",
    "target_departure",
    "actual_departure",
    "arrival_time",
    "price_gbp",
    "railcard_applied",
    "sold_out",
    "fare_name",
)


def append_price_log(
    path: Path,
    checked_at: datetime,
    entries: list[tuple[date, str, TrainOption | None]],
) -> None:
    """Append one row per (travel_date, target_departure, option) to the
    CSV at `path`, writing a header first if the file doesn't exist yet.

    `option` is None when that target departure wasn't found in the
    results at all (as opposed to found-but-sold-out, which is a real
    TrainOption with sold_out=True) — both are logged, since "this train
    stopped appearing in the timetable" is itself worth a historical
    record.
    """
    is_new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new_file:
            writer.writeheader()
        for travel_date, target_departure, option in entries:
            writer.writerow(
                {
                    "checked_at": checked_at.isoformat(),
                    "travel_date": travel_date.isoformat(),
                    "target_departure": target_departure,
                    "actual_departure": option.departure_time if option else "",
                    "arrival_time": (option.arrival_time or "") if option else "",
                    "price_gbp": str(option.price) if option and option.price is not None else "",
                    "railcard_applied": option.railcard_applied if option else "",
                    "sold_out": option.sold_out if option else "",
                    "fare_name": (option.fare_name or "") if option else "",
                }
            )
