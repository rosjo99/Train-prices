"""Dev tool: capture a live TransPennine Express journey-plan response.

Unlike scripts/capture_fixture.py (NRE, now retired), TPE serves its fares
from a POST fetch (api.tpexpress.co.uk/jp/journey-plan) whose response
body Playwright's response.json() can read directly — no XHR replay
needed. This script drives the same Camoufox browser + deep-link approach
scripts/probe_camoufox_tpe.py already validated, dismisses the
Usercentrics cookie banner, and dumps the first captured journey-plan
response (request payload too, for reference) to --out.

Usage:
    python scripts/capture_fixture_tpe.py \\
        --origin OXF --destination PAD \\
        --datetime 2026-12-18T07:00 \\
        --out /tmp/tpe-fixture.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from camoufox.sync_api import Camoufox, NewContext

JOURNEY_PLAN_PATH = "/jp/journey-plan"

COOKIE_BANNER_SELECTORS = (
    "[data-testid='uc-accept-all-button']",
    "#uc-btn-accept-banner",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
)

PAGE_LOAD_TIMEOUT_MS = 60_000
RESULT_WAIT_SECONDS = 20.0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument(
        "--datetime",
        required=True,
        help="Anchor departure, YYYY-MM-DDTHH:MM (TPE's own deep-link format).",
    )
    parser.add_argument("--railcard", default="YNGx1")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args(argv)


def _build_url(origin: str, destination: str, when: str, railcard: str) -> str:
    return (
        f"https://ticket.tpexpress.co.uk/journeys-grid/{origin}/{destination}/{when}"
        f"//1//{railcard}"
        "?departNow=no&realTime=no&searchPreferences=%2C%2C%2C%2Ctrue"
        "&showAdditionalRoutes=no&showCheapest=no&tocSpecific=no"
    )


def _dismiss_cookie_banner(page: Any) -> None:
    for selector in COOKIE_BANNER_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                locator.first.click(timeout=3000)
                print(f"[info] dismissed cookie banner via {selector!r}")
                return
        except Exception:
            continue
    print("[info] no cookie banner dismissed (absent, or all selectors failed)")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    url = _build_url(args.origin, args.destination, args.datetime, args.railcard)

    captured: dict[str, Any] = {}

    def on_response(response: Any) -> None:
        try:
            if JOURNEY_PLAN_PATH not in response.url:
                return
            captured["url"] = response.url
            captured["status"] = response.status
            try:
                captured["request_post_data"] = response.request.post_data
            except Exception:
                captured["request_post_data"] = None
            try:
                captured["body"] = response.json()
            except Exception as exc:
                captured["body_error"] = repr(exc)
        except Exception as exc:
            print(f"[warn] response handler error: {exc}", file=sys.stderr)

    with Camoufox(headless=not args.headed, humanize=True, locale="en-GB") as browser:
        context = NewContext(
            browser,
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 768},
        )
        try:
            page = context.new_page()
            page.on("response", on_response)

            print(f"[info] navigating: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

            time.sleep(2.0)
            _dismiss_cookie_banner(page)

            deadline = time.monotonic() + RESULT_WAIT_SECONDS
            while "body" not in captured and "body_error" not in captured and time.monotonic() < deadline:
                page.wait_for_timeout(250)

            if "body" not in captured:
                print(f"[error] no journey-plan response captured: {captured}", file=sys.stderr)
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(captured, indent=2, default=str), encoding="utf-8")
                return 1

        finally:
            context.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(captured, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
