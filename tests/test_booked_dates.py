"""Tests for src.booked_dates. See docs/plans/001-train-price-alert.md
Task 6 for the acceptance criteria these transcribe.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.booked_dates import load_booked_dates


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "booked-dates.txt"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_lines_comments_and_blanks(tmp_path):
    path = _write(tmp_path, "2026-09-08\n# comment\n\n2026-10-01")

    assert load_booked_dates(path) == {date(2026, 9, 8), date(2026, 10, 1)}


def test_missing_path_returns_empty_set_no_exception(tmp_path):
    path = tmp_path / "does-not-exist.txt"

    assert load_booked_dates(path) == set()


def test_unparsable_line_is_skipped_with_warning_others_still_parse(tmp_path, caplog):
    path = _write(tmp_path, "2026-09-08\nnot-a-date\n2026-10-01")

    with caplog.at_level("WARNING"):
        result = load_booked_dates(path)

    assert result == {date(2026, 9, 8), date(2026, 10, 1)}
    assert any("not-a-date" in message for message in caplog.messages)


def test_entirely_empty_file_returns_empty_set(tmp_path):
    path = _write(tmp_path, "")

    assert load_booked_dates(path) == set()


def test_duplicate_dates_collapse_to_one(tmp_path):
    path = _write(tmp_path, "2026-09-08\n2026-09-08\n2026-09-08")

    assert load_booked_dates(path) == {date(2026, 9, 8)}


def test_whitespace_only_lines_are_treated_as_blank(tmp_path):
    path = _write(tmp_path, "2026-09-08\n   \n\t\n2026-10-01")

    assert load_booked_dates(path) == {date(2026, 9, 8), date(2026, 10, 1)}


def test_indented_comment_is_still_treated_as_comment(tmp_path):
    path = _write(tmp_path, "  # 2026-01-01\n2026-09-08")

    assert load_booked_dates(path) == {date(2026, 9, 8)}
