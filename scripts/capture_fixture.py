"""Dev tool: capture a live Trainline journey-search response as a fixture.

Usage:
    python scripts/capture_fixture.py --date 2026-09-08 \\
        --out tests/fixtures/journey_search_sample.json

Calls `src.scraper.fetch_journey_search` for the given travel date and
pretty-prints the raw JSON response to --out. Used both for the initial
fixture capture and to regenerate it later if Trainline changes its
response schema.

NOTE: as of this writing, `config.ORIGIN_URN`/`config.DESTINATION_URN` are
still None (real values require a live discovery run against
thetrainline.com — see src/config.py's RESULTS_URL_TEMPLATE comment), so
running this script will fail until a follow-up commit fills them in.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from src.scraper import ScraperError, fetch_journey_search


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        required=True,
        type=date.fromisoformat,
        help="Travel date to search for, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Path to write the pretty-printed JSON response to.",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Directory to write debug artifacts to on failure (optional).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        raw = fetch_journey_search(args.date, artifacts_dir=args.artifacts_dir)
    except ScraperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
