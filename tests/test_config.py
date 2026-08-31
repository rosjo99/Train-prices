"""Tests for src.config's env-var parsing helpers.

Only `_read_max_dates()` is covered here — everything else in
src.config is either exercised indirectly through the other test files
or is a plain constant with nothing to test.
"""

from __future__ import annotations

import pytest

from src import config


def test_max_dates_unset_is_none(monkeypatch):
    monkeypatch.delenv("MAX_DATES", raising=False)
    assert config._read_max_dates() is None


def test_max_dates_blank_is_none(monkeypatch):
    monkeypatch.setenv("MAX_DATES", "  ")
    assert config._read_max_dates() is None


@pytest.mark.parametrize("raw", ["all", "ALL", "All"])
def test_max_dates_all_sentinel_is_none(monkeypatch, raw):
    # "all" means "no cap", same as blank/unset — needed because GitHub's
    # own "Run workflow" web UI re-substitutes the input's default value
    # whenever the field is submitted blank, so there's no reliable way
    # to actually submit an empty override from that UI. See
    # docs/plans/001-train-price-alert.md Task 1's "MAX_DATES=all
    # sentinel" revision note.
    monkeypatch.setenv("MAX_DATES", raw)
    assert config._read_max_dates() is None


def test_max_dates_positive_int_parses(monkeypatch):
    monkeypatch.setenv("MAX_DATES", "5")
    assert config._read_max_dates() == 5


def test_max_dates_zero_raises_config_error(monkeypatch):
    monkeypatch.setenv("MAX_DATES", "0")
    with pytest.raises(config.ConfigError):
        config._read_max_dates()


def test_max_dates_non_integer_raises_config_error(monkeypatch):
    monkeypatch.setenv("MAX_DATES", "abc")
    with pytest.raises(config.ConfigError):
        config._read_max_dates()
