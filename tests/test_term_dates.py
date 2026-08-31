"""Tests for src.term_dates — see docs/plans/001-train-price-alert.md Task 2
for the full acceptance-criteria list these tests transcribe.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from src import term_dates
from src.term_dates import checkable_dates, is_checkable_day

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Individual date checks (acceptance criteria, one assertion each) ----


def test_term_begins_friday():
    assert is_checkable_day(date(2026, 9, 4)) is True


def test_inset_day_first_day_of_active_range():
    assert is_checkable_day(date(2026, 9, 1)) is True  # Tue, INSET


def test_last_day_is_a_wednesday_so_false():
    assert is_checkable_day(date(2026, 12, 16)) is False


def test_tuesday_before_last_day():
    assert is_checkable_day(date(2026, 12, 15)) is True


def test_inside_autumn_half_term():
    assert is_checkable_day(date(2026, 10, 22)) is False  # Thu, half term


def test_occasional_day():
    assert is_checkable_day(date(2026, 11, 20)) is False  # Fri, occasional day


def test_day_before_occasional_day():
    assert is_checkable_day(date(2026, 11, 19)) is True  # Thu


@pytest.mark.parametrize(
    "d",
    [
        date(2026, 9, 7),  # Monday, in term
        date(2026, 9, 9),  # Wednesday, in term
        date(2026, 9, 5),  # Saturday, in term
        date(2026, 9, 6),  # Sunday, in term
    ],
)
def test_wrong_weekdays_in_term_are_false(d: date):
    assert is_checkable_day(d) is False


def test_christmas_holiday_tuesday():
    assert is_checkable_day(date(2026, 12, 22)) is False


def test_summer_holiday_thursday():
    assert is_checkable_day(date(2027, 8, 5)) is False


def test_spring_inset_day():
    assert is_checkable_day(date(2027, 1, 7)) is True  # Thu, INSET


def test_day_before_spring_term():
    assert is_checkable_day(date(2027, 1, 5)) is False  # Tue, before term start


def test_spring_half_term():
    assert is_checkable_day(date(2027, 2, 18)) is False  # Thu


def test_summer_inset_monday():
    assert is_checkable_day(date(2027, 4, 19)) is False  # Mon, INSET


def test_summer_term_second_day():
    assert is_checkable_day(date(2027, 4, 20)) is True  # Tue


def test_summer_last_day():
    assert is_checkable_day(date(2027, 7, 8)) is True  # Thu, last day


def test_day_after_summer_term():
    assert is_checkable_day(date(2027, 7, 9)) is False  # Fri


def test_summer_half_term():
    assert is_checkable_day(date(2027, 6, 1)) is False  # Tue


# --- Exclusion boundary inclusivity ---------------------------------------


def test_half_term_start_boundary_excluded():
    assert is_checkable_day(date(2026, 10, 19)) is False  # Mon, not Tue/Thu/Fri anyway
    # Use a Thu/Fri within the boundary explicitly instead, per the plan's
    # own examples (2026-10-19 and 2026-10-30 tested directly).
    assert term_dates.is_in_term(date(2026, 10, 19)) is False


def test_half_term_end_boundary_excluded():
    assert term_dates.is_in_term(date(2026, 10, 30)) is False


# --- checkable_dates -------------------------------------------------------


def test_checkable_dates_skips_half_term_block():
    dates = checkable_dates(date(2026, 10, 15), date(2026, 11, 5))
    for start, end in term_dates.term_for(date(2026, 10, 20)).excluded_ranges:
        for d in dates:
            assert not (start <= d <= end)
    # Sanity: some dates before and after the half term are present.
    assert date(2026, 10, 15) in dates  # Thu, before half term
    assert date(2026, 11, 5) in dates  # Thu, after half term
    assert date(2026, 10, 22) not in dates  # Thu, inside half term


def test_checkable_dates_end_before_start_returns_empty():
    assert checkable_dates(date(2026, 9, 10), date(2026, 9, 1)) == []


def test_checkable_dates_ascending_and_inclusive():
    dates = checkable_dates(date(2026, 9, 1), date(2026, 9, 4))
    assert dates == [date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 4)]


# --- Module-level constants -------------------------------------------------


def test_last_known_date():
    assert term_dates.LAST_KNOWN_DATE == date(2027, 7, 8)


def test_dates_outside_every_term_return_false_not_raise():
    # Well outside any term range, e.g. deep summer holidays.
    assert is_checkable_day(date(2026, 1, 1)) is False
    assert is_checkable_day(date(2028, 1, 1)) is False


# --- CLI --------------------------------------------------------------------


def test_cli_list_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "src.term_dates", "--list"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Autumn Term 2026" in result.stdout


def test_cli_check_exits_zero_and_reports_reason():
    result = subprocess.run(
        [sys.executable, "-m", "src.term_dates", "--check", "2026-11-20"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "no:" in result.stdout
