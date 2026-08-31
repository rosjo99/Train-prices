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
