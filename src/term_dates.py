"""School term-date data and the "should we check this date?" logic.

This module has two jobs: hold the term-date data as a plain Python data
structure (see the ``TERMS`` block below), and expose pure functions that
answer "is this travel date one we should check?" Nothing here reads the
clock (no ``date.today()``) and nothing hits the network — every function
takes an explicit ``date`` and returns an explicit answer, so the whole
module is trivially testable and side-effect-free at import time except
for ``_validate()``, which only checks the data below is internally
consistent and raises ``ValueError`` if not.

--------------------------------------------------------------------------
HOW TO UPDATE THIS FOR A NEW SCHOOL YEAR
--------------------------------------------------------------------------
Edit only the ``TERMS`` list below:

1. Add a new ``Term(...)`` entry for each new term, copying the dates from
   the school's term-dates document (and from ``CLAUDE.md``, which should
   be updated at the same time so the two stay in sync). All dates are
   **inclusive** and written in ISO format (``date(YYYY, M, D)``).
2. Delete ``Term`` entries for school years that have fully finished (the
   ``end`` date is in the past) — this is optional tidying, not required
   for correctness, but keeps the file from growing forever.
3. ``excluded_ranges`` is for half terms and other multi-day closures;
   each is a ``(start, end)`` tuple, inclusive on both ends. ``excluded_days``
   is for single days (INSET/occasional days that fall *within* term dates
   that are otherwise a Tue/Thu/Fri, and bank holidays) — most bank
   holidays land on a Monday and so are never selected anyway, but list
   them for completeness in case a school moves one.
4. After editing, run ``python -m src.term_dates --list`` from the repo
   root and read through the printed dates to eyeball that nothing looks
   wrong (a whole half term missing, a term running into the next one,
   etc.) — this also re-runs ``_validate()`` at import, which will raise
   loudly if a term's start is after its end, an exclusion falls outside
   its own term, or two terms overlap.
5. ``LAST_KNOWN_DATE`` (used by the orchestrator as the upper bound for
   "check every remaining date this school year") is computed
   automatically from whatever terms are in the list — no separate value
   to update.

Verification tool for after an edit: this file is a runnable CLI.
    python -m src.term_dates --list             # every checkable date, grouped by term
    python -m src.term_dates --check 2026-11-20  # yes/no + reason for one date
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Term:
    name: str
    start: date  # inclusive
    end: date  # inclusive
    excluded_ranges: tuple[tuple[date, date], ...] = ()  # inclusive both ends
    excluded_days: tuple[date, ...] = ()


# --------------------------------------------------------------------------
# TERM DATA — transcribed from CLAUDE.md "Term dates" section.
# Keep this block and CLAUDE.md in sync; see the header comment above for
# how to update it each school year.
# --------------------------------------------------------------------------
TERMS: tuple[Term, ...] = (
    Term(
        name="Autumn Term 2026",
        start=date(2026, 9, 1),
        end=date(2026, 12, 16),
        excluded_ranges=(
            (date(2026, 10, 19), date(2026, 10, 30)),  # half term
        ),
        excluded_days=(
            date(2026, 11, 20),  # occasional day
        ),
    ),
    Term(
        name="Spring Term 2027",
        start=date(2027, 1, 6),
        end=date(2027, 3, 25),
        excluded_ranges=(
            (date(2027, 2, 15), date(2027, 2, 19)),  # half term
        ),
    ),
    Term(
        name="Summer Term 2027",
        start=date(2027, 4, 19),
        end=date(2027, 7, 8),
        excluded_ranges=(
            (date(2027, 5, 31), date(2027, 6, 4)),  # half term
        ),
        excluded_days=(
            # Bank holiday, Mon 3 May 2027. Falls on a Monday so it would
            # never be selected by CHECK_WEEKDAYS anyway — listed here for
            # completeness in case a school ever moves it.
            date(2027, 5, 3),
        ),
    ),
)

# Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6 (datetime.date.weekday()).
CHECK_WEEKDAYS = frozenset({1, 3, 4})  # Tue, Thu, Fri

# The end of the last term currently in TERMS. Used by the orchestrator as
# the upper bound when it checks every remaining date to the end of the
# school year; advances automatically as new terms are added above, with
# no other code change needed.
LAST_KNOWN_DATE: date = max(term.end for term in TERMS)


def term_for(d: date) -> Term | None:
    """Return the term whose active range contains ``d``, ignoring exclusions.

    Returns None if ``d`` falls outside every term's [start, end] range.
    """
    for term in TERMS:
        if term.start <= d <= term.end:
            return term
    return None


def is_in_term(d: date) -> bool:
    """True if ``d`` is inside a term's active range and not excluded."""
    term = term_for(d)
    if term is None:
        return False
    if d in term.excluded_days:
        return False
    for start, end in term.excluded_ranges:
        if start <= d <= end:
            return False
    return True


def is_checkable_day(d: date) -> bool:
    """True if ``d`` is a Tue/Thu/Fri inside term time (not excluded)."""
    return d.weekday() in CHECK_WEEKDAYS and is_in_term(d)


def checkable_dates(start: date, end: date) -> list[date]:
    """All checkable dates in [start, end], inclusive, ascending.

    Returns [] if end < start.
    """
    if end < start:
        return []
    result = []
    d = start
    one_day = timedelta(days=1)
    while d <= end:
        if is_checkable_day(d):
            result.append(d)
        d += one_day
    return result


def _validate() -> None:
    """Sanity-check TERMS at import time; raise ValueError if inconsistent."""
    for term in TERMS:
        if term.start > term.end:
            raise ValueError(
                f"Term {term.name!r} has start {term.start} after end {term.end}"
            )
        for r_start, r_end in term.excluded_ranges:
            if r_start > r_end:
                raise ValueError(
                    f"Term {term.name!r} has an excluded range with start "
                    f"{r_start} after end {r_end}"
                )
            if r_start < term.start or r_end > term.end:
                raise ValueError(
                    f"Term {term.name!r} has an excluded range "
                    f"({r_start} - {r_end}) outside its own term range "
                    f"({term.start} - {term.end})"
                )
        for excluded_day in term.excluded_days:
            if excluded_day < term.start or excluded_day > term.end:
                raise ValueError(
                    f"Term {term.name!r} has an excluded day {excluded_day} "
                    f"outside its own term range ({term.start} - {term.end})"
                )

    for i, a in enumerate(TERMS):
        for b in TERMS[i + 1 :]:
            if a.start <= b.end and b.start <= a.end:
                raise ValueError(
                    f"Terms {a.name!r} and {b.name!r} overlap"
                )


_validate()


# --------------------------------------------------------------------------
# CLI — human verification tool after editing the TERMS block above.
# --------------------------------------------------------------------------

def _weekday_name(d: date) -> str:
    return d.strftime("%A")


def _check_reason(d: date) -> tuple[bool, str]:
    """Return (is_checkable, human-readable reason) for a single date."""
    if d.weekday() not in CHECK_WEEKDAYS:
        return False, f"no: {_weekday_name(d)}"

    term = term_for(d)
    if term is None:
        return False, "no: outside all terms"

    if d in term.excluded_days:
        return False, f"no: excluded day ({term.name})"

    for start, end in term.excluded_ranges:
        if start <= d <= end:
            return False, f"no: half term ({term.name})"

    return True, "yes"


def _cmd_list() -> int:
    for term in TERMS:
        dates = checkable_dates(term.start, term.end)
        print(f"{term.name} ({len(dates)} checkable dates):")
        for d in dates:
            print(f"  {d.isoformat()} ({_weekday_name(d)})")
    return 0


def _cmd_check(date_str: str) -> int:
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        print(f"error: {date_str!r} is not a valid YYYY-MM-DD date", file=sys.stderr)
        return 2
    is_checkable, reason = _check_reason(d)
    print(f"{d.isoformat()} ({_weekday_name(d)}): {reason}")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify term-date data and checkable-day logic."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list", action="store_true", help="List every checkable date, grouped by term."
    )
    group.add_argument(
        "--check", metavar="YYYY-MM-DD", help="Check whether one date is checkable."
    )
    args = parser.parse_args(argv)

    if args.list:
        return _cmd_list()
    return _cmd_check(args.check)


if __name__ == "__main__":
    sys.exit(_main())
