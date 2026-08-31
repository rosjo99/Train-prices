"""Reads booked-dates.txt: travel dates already booked, so main() can
stop checking them — both to cut down the daily workload and because an
alert about a fare on a date already booked is noise.

See CLAUDE.md's "Marking a date as already booked" and
docs/plans/001-train-price-alert.md §2.3 for the design rationale: a
plain, hand-editable text file rather than a repo secret, an Actions
variable, or a workflow_dispatch input, so editing it needs no local
setup — click the pencil icon on github.com, add a line, commit.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


def load_booked_dates(path: Path) -> set[date]:
    """Read `path` as one YYYY-MM-DD per line; '#' starts a comment, and
    blank lines are ignored.

    A missing file is treated as "nothing booked yet" — a set(), not an
    error — since that's the normal state on first-time setup, not a
    failure. A line that fails to parse is logged as a warning (with its
    line number and raw text) and skipped, never raised: one typo in a
    hand-edited file must not take down the whole day's price check.
    """
    if not path.exists():
        return set()

    booked: set[date] = set()
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            booked.add(date.fromisoformat(line))
        except ValueError:
            logger.warning(
                "%s:%d: could not parse %r as a date (expected YYYY-MM-DD) — skipping",
                path,
                line_number,
                raw_line,
            )
    return booked
