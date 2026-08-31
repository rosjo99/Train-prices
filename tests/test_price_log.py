"""Tests for src.price_log."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.models import TrainOption
from src.price_log import append_price_log


def _option(price=Decimal("8.70"), sold_out=False, railcard_applied=True) -> TrainOption:
    return TrainOption(
        travel_date=date(2026, 9, 8),
        departure_time="07:25",
        arrival_time="08:26",
        price=price,
        currency="GBP",
        railcard_applied=railcard_applied,
        is_direct=True,
        sold_out=sold_out,
        fare_name="Advance Single",
    )


def _read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_creates_file_with_header_on_first_write(tmp_path):
    path = tmp_path / "price-history.csv"
    checked_at = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)

    append_price_log(path, checked_at, [(date(2026, 9, 8), "07:25", _option())])

    rows = _read_rows(path)
    assert len(rows) == 1
    assert rows[0]["travel_date"] == "2026-09-08"
    assert rows[0]["target_departure"] == "07:25"
    assert rows[0]["price_gbp"] == "8.70"
    assert rows[0]["railcard_applied"] == "True"


def test_appends_without_truncating_existing_rows(tmp_path):
    path = tmp_path / "price-history.csv"
    checked_at = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)

    append_price_log(path, checked_at, [(date(2026, 9, 8), "07:25", _option(price=Decimal("8.70")))])
    append_price_log(path, checked_at, [(date(2026, 9, 8), "07:25", _option(price=Decimal("7.50")))])

    rows = _read_rows(path)
    assert len(rows) == 2
    assert rows[0]["price_gbp"] == "8.70"
    assert rows[1]["price_gbp"] == "7.50"
    # Only one header line total, not one per append.
    assert path.read_text(encoding="utf-8").count("checked_at") == 1


def test_none_option_logs_blank_fields_not_a_crash(tmp_path):
    path = tmp_path / "price-history.csv"
    checked_at = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)

    append_price_log(path, checked_at, [(date(2026, 9, 8), "07:30", None)])

    rows = _read_rows(path)
    assert rows[0]["actual_departure"] == ""
    assert rows[0]["price_gbp"] == ""
    assert rows[0]["sold_out"] == ""


def test_sold_out_option_logs_no_price(tmp_path):
    path = tmp_path / "price-history.csv"
    checked_at = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)

    append_price_log(
        path, checked_at, [(date(2026, 9, 8), "07:25", _option(price=None, sold_out=True))]
    )

    rows = _read_rows(path)
    assert rows[0]["price_gbp"] == ""
    assert rows[0]["sold_out"] == "True"


def test_creates_parent_directory_if_missing(tmp_path):
    path = tmp_path / "nested" / "dir" / "price-history.csv"
    checked_at = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)

    append_price_log(path, checked_at, [(date(2026, 9, 8), "07:25", _option())])

    assert path.exists()


def test_multiple_entries_in_one_call_all_written(tmp_path):
    path = tmp_path / "price-history.csv"
    checked_at = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)

    append_price_log(
        path,
        checked_at,
        [
            (date(2026, 9, 8), "07:25", _option(price=Decimal("8.70"))),
            (date(2026, 9, 8), "07:30", _option(price=Decimal("9.10"))),
        ],
    )

    rows = _read_rows(path)
    assert len(rows) == 2
