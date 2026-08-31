"""Dev/deploy tool: export src.term_dates.TERMS as JSON for the static
booked-dates site (site/app.js) to consume.

The site needs to compute "which dates are checkable" client-side (so it
always reflects the visitor's own current date, not a snapshot that goes
stale between deploys), but the term data itself lives in Python
(src/term_dates.py) as the single source of truth. Rather than
hand-duplicating that data in JavaScript — guaranteed to drift out of
sync the next time someone updates TERMS for a new school year and
forgets the JS copy — this script exports it fresh.

Run automatically by .github/workflows/deploy-pages.yml before every
Pages deploy. Only run manually if previewing the site locally.

Usage: python scripts/export_terms.py --out site/terms.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from src import config, term_dates


def _term_to_dict(term: term_dates.Term) -> dict:
    return {
        "name": term.name,
        "start": term.start.isoformat(),
        "end": term.end.isoformat(),
        "excluded_ranges": [
            [start.isoformat(), end.isoformat()] for start, end in term.excluded_ranges
        ],
        "excluded_days": [d.isoformat() for d in term.excluded_days],
    }


def build_export() -> dict:
    return {
        "terms": [_term_to_dict(term) for term in term_dates.TERMS],
        "check_weekdays": sorted(term_dates.CHECK_WEEKDAYS),
        "last_known_date": term_dates.LAST_KNOWN_DATE.isoformat(),
        # So the site can label its per-date price columns without
        # hand-duplicating the route's target departure times.
        "target_departures": list(config.TARGET_DEPARTURES),
        # Informational only (shown in the site's footer) — not used in
        # any checkable-date decision, so date.today() here isn't the
        # naive-clock-read the rest of this project avoids.
        "generated_at": date.today().isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(build_export(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
