"""Data models shared across the scraper, parser, and orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class TrainOption:
    travel_date: date
    departure_time: str
    arrival_time: str | None
    price: Decimal | None
    currency: str
    railcard_applied: bool
    is_direct: bool
    sold_out: bool
    fare_name: str | None


@dataclass(frozen=True)
class CheckResult:
    travel_date: date
    options: list[TrainOption]
    error: str | None


@dataclass(frozen=True)
class AlertMatch:
    """One TrainOption that beat the price threshold, produced by
    src.main's evaluate() (Task 6) and consumed by src.notifier.
    `travel_date` duplicates `option.travel_date` — kept as its own field
    since that's how docs/plans/001-train-price-alert.md Task 5 specifies
    it, not because the two could ever legitimately disagree.
    """

    travel_date: date
    option: TrainOption
    threshold: Decimal
