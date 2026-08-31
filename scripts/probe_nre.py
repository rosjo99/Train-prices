"""Throwaway discovery probe for National Rail Enquiries' journey planner.

Not part of the production tool — used once to determine whether NRE's
site can be driven by a real browser without hitting bot protection, and
to capture whatever network requests carry fare data so src/scraper.py
and src/config.py can be rewritten against NRE instead of Trainline.

Usage: python scripts/probe_nre.py --date 2026-09-08 --out /tmp/nre_probe
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    captured_responses: list[dict] = []

    def on_response(response):
        url = response.url
        keywords = ("journey", "fare", "search", "price", "planner")
        if not any(k in url.lower() for k in keywords):
            return
        entry = {"url": url, "status": response.status}
        try:
            entry["json"] = response.json()
        except Exception:
            try:
                text = response.text()
                entry["text_snippet"] = text[:2000]
            except Exception:
                entry["body"] = "(unreadable)"
        captured_responses.append(entry)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.on("response", on_response)

        print("navigating to journey planner...", flush=True)
        page.goto(
            "https://www.nationalrail.co.uk/journey-planner/",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        page.wait_for_timeout(3000)
        page.screenshot(path=str(args.out / "01_landing.png"))
        (args.out / "01_landing.html").write_text(page.content(), encoding="utf-8")
        print("wrote landing screenshot/html", flush=True)

        # Try to dismiss a cookie banner, best-effort.
        for selector in (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
        ):
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    print(f"dismissed cookie banner via {selector}", flush=True)
                    break
            except Exception:
                continue

        # Fill the origin/destination fields discovered via static HTML
        # (name="jp_preview_origin" / name="jp_preview_destination").
        filled = False
        for origin_sel, dest_sel in [
            ("input[name='jp_preview_origin']", "input[name='jp_preview_destination']"),
            ("#jp_preview_origin", "#jp_preview_destination"),
        ]:
            try:
                origin_input = page.locator(origin_sel)
                dest_input = page.locator(dest_sel)
                if origin_input.count() > 0 and dest_input.count() > 0:
                    origin_input.first.fill("Oxford")
                    page.wait_for_timeout(1500)
                    page.screenshot(path=str(args.out / "02_origin_typed.png"))
                    # try to pick a dropdown suggestion if one appears
                    try:
                        page.keyboard.press("ArrowDown")
                        page.keyboard.press("Enter")
                    except Exception:
                        pass
                    dest_input.first.fill("London Paddington")
                    page.wait_for_timeout(1500)
                    try:
                        page.keyboard.press("ArrowDown")
                        page.keyboard.press("Enter")
                    except Exception:
                        pass
                    filled = True
                    print(f"filled origin/destination via {origin_sel}", flush=True)
                    break
            except Exception as exc:
                print(f"selector {origin_sel} failed: {exc}", flush=True)
                continue

        page.screenshot(path=str(args.out / "03_after_fill.png"))
        (args.out / "03_after_fill.html").write_text(page.content(), encoding="utf-8")

        if filled:
            # Try to submit the form / click a search button.
            for btn_sel in (
                "button[type='submit']",
                "button:has-text('Search')",
                "button:has-text('Plan')",
                "#jp-form-preview button",
            ):
                try:
                    btn = page.locator(btn_sel)
                    if btn.count() > 0:
                        btn.first.click(timeout=5000)
                        print(f"clicked submit via {btn_sel}", flush=True)
                        break
                except Exception:
                    continue

            page.wait_for_timeout(8000)
            page.screenshot(path=str(args.out / "04_after_submit.png"))
            (args.out / "04_after_submit.html").write_text(page.content(), encoding="utf-8")
            print("current URL after submit:", page.url, flush=True)

        (args.out / "captured_responses.json").write_text(
            json.dumps(captured_responses, indent=2, default=str)[:200000],
            encoding="utf-8",
        )
        print(f"captured {len(captured_responses)} matching responses", flush=True)

        content_lower = page.content().lower()
        blocked_markers = ("captcha", "access denied", "are you a robot", "datadome", "cloudflare")
        hits = [m for m in blocked_markers if m in content_lower]
        print("block markers present on final page:", hits or "none", flush=True)

        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
