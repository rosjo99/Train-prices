"""Throwaway discovery probe: does NRE's deep-link URL format work?

The user found real example URLs that appear to encode the entire
journey search as query params (origin/destination station codes,
leaving date/time, adults, railcard) directly on
/journey-planner/?..., e.g.:

  https://www.nationalrail.co.uk/journey-planner/?type=single&origin=OXF
  &destination=PAD&leavingType=departing&leavingDate=240926&leavingHour=14
  &leavingMin=15&adults=1&railcards=YNG%7C1&extraTime=0#O

If this loads straight into (or straight to a one-click-away view of)
real results, it sidesteps every UI-interaction bug hit so far in
probe_nre.py (decoy click, autocomplete listbox scoping, the
React-controlled find_hotels checkbox and leaving-date button that
don't visually update from a raw click/goto, native <select> railcard
fields) — and, more importantly, might avoid the Booking.com hijack
entirely if that turned out to be triggered by submitting a same-day
search for already-departed trains: a deep link can specify tomorrow's
date and the exact target time up front, no submit-button click even
required if the page auto-fetches on load.

Usage: python scripts/probe_nre_deeplink.py --out /tmp/nre_deeplink
       python scripts/probe_nre_deeplink.py --date 2026-09-24 --hour 14 --minute 15 --out /tmp/nre_deeplink
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

ORIGIN_CODE = "OXF"
DESTINATION_CODE = "PAD"
RAILCARD_CODE = "YNG"  # confirmed by the user's example URL: 16-25 Railcard


def build_url(target: date, hour: str, minute: str) -> str:
    return (
        "https://www.nationalrail.co.uk/journey-planner/"
        f"?type=single&origin={ORIGIN_CODE}&destination={DESTINATION_CODE}"
        f"&leavingType=departing&leavingDate={target.strftime('%d%m%y')}"
        f"&leavingHour={hour}&leavingMin={minute}"
        f"&adults=1&railcards={RAILCARD_CODE}%7C1&extraTime=0#O"
    )


def main() -> int:
    tomorrow = (datetime.now(LONDON).date() + timedelta(days=1)).isoformat()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date", default=tomorrow, type=date.fromisoformat,
        help=f"Travel date, YYYY-MM-DD (default: tomorrow, {tomorrow})",
    )
    parser.add_argument("--hour", default="07", help="Leaving hour, HH (default: 07)")
    parser.add_argument("--minute", default="25", help="Leaving minute, MM (default: 25)")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    url = build_url(args.date, args.hour, args.minute)
    print(f"deep-link URL: {url}", flush=True)

    captured_responses: list[dict] = []

    def on_response(response):
        resp_url = response.url
        keywords = ("journey", "fare", "search", "price", "planner")
        if not any(k in resp_url.lower() for k in keywords):
            return
        entry = {"url": resp_url, "status": response.status}
        try:
            entry["json"] = response.json()
        except Exception:
            try:
                entry["text_snippet"] = response.text()[:2000]
            except Exception:
                entry["body"] = "(unreadable)"
        captured_responses.append(entry)

    NRE_HOST_SUFFIX = "nationalrail.co.uk"

    def _route_handler(route):
        # Same protections as probe_nre.py: allowlist cross-origin iframe
        # documents (block everything except NRE's own) and backstop any
        # unexpected main-frame navigation away from NRE with a blank
        # fulfill rather than an abort (which leaves a browser error page).
        try:
            request = route.request
            try:
                frame = request.frame
            except Exception:
                frame = None

            is_subframe_doc = (
                request.resource_type == "document"
                and frame is not None
                and frame.parent_frame is not None
            )
            if is_subframe_doc:
                sub_host = urlparse(request.url).hostname or ""
                if not sub_host.endswith(NRE_HOST_SUFFIX):
                    print(f"blocked cross-origin iframe (allowlist): {request.url}", flush=True)
                    route.abort()
                    return

            is_main_frame_nav = (
                request.is_navigation_request()
                and frame is not None
                and frame.parent_frame is None
            )
            if is_main_frame_nav:
                host = urlparse(request.url).hostname or ""
                if not host.endswith(NRE_HOST_SUFFIX):
                    print(f"blocked hijack navigation to {request.url} (backstop)", flush=True)
                    route.fulfill(status=200, content_type="text/html", body="<html></html>")
                    return
        except Exception as exc:
            print(f"route handler error (falling through to continue): {exc}", flush=True)
        route.continue_()

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
        context.route("**/*", _route_handler)
        page = context.new_page()
        page.on("response", on_response)
        page.on(
            "console",
            lambda msg: print(f"[console.{msg.type}] {msg.text}", flush=True)
            if "error" in msg.type
            else None,
        )

        print("navigating to deep-link URL...", flush=True)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        page.screenshot(path=str(args.out / "01_deeplink_load.png"))
        (args.out / "01_deeplink_load.html").write_text(page.content(), encoding="utf-8")
        print("current URL after initial load:", page.url, flush=True)

        # Dismiss a cookie banner, best-effort.
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

        # Dump the field values the deep link should have pre-filled, to
        # confirm the URL params actually populated the form (rather than
        # being silently ignored) before deciding whether a submit click
        # is even needed.
        for fid in ("jp-origin", "jp-destination", "leaving-date"):
            try:
                val = page.locator(f"#{fid}").input_value(timeout=3000)
                print(f"deep-link pre-filled #{fid}: {val!r}", flush=True)
            except Exception as exc:
                print(f"reading #{fid} after deep-link load failed: {exc}", flush=True)

        # Check immediately whether results already rendered without any
        # click at all (best case: the SPA reads the query string on
        # mount and auto-fetches).
        body_text = ""
        try:
            body_text = page.inner_text("body")
        except Exception as exc:
            print(f"could not read body inner_text: {exc}", flush=True)
        print(f"body inner_text length (pre-submit): {len(body_text)}", flush=True)
        price_matches = re.findall(r"£\s?\d+(?:\.\d{2})?", page.content())
        print(f"£-price-shaped strings pre-submit: {price_matches[:20]}", flush=True)

        # If not, try clicking the real submit button (#button-jp), same
        # as the interactive probe, now that the form should already be
        # correctly filled by the URL — no autocomplete/date-picker/
        # railcard-select interaction needed at all.
        if not price_matches:
            try:
                btn = page.locator("#button-jp")
                if btn.count() > 0:
                    btn.first.click(timeout=5000)
                    print("clicked #button-jp after deep-link load", flush=True)
                else:
                    print("#button-jp not found after deep-link load", flush=True)
            except Exception as exc:
                print(f"clicking #button-jp failed: {exc}", flush=True)

            page.wait_for_timeout(15000)

        page.screenshot(path=str(args.out / "02_after_submit.png"))
        (args.out / "02_after_submit.html").write_text(page.content(), encoding="utf-8")
        print("current URL after submit:", page.url, flush=True)

        if "nationalrail.co.uk" not in page.url:
            print(
                f"WARNING: navigated away from nationalrail.co.uk to {page.url} "
                "— any prices found below are NOT NRE fares",
                flush=True,
            )

        price_matches = re.findall(r"£\s?\d+(?:\.\d{2})?", page.content())
        print(f"£-price-shaped strings found on final page: {price_matches[:20]}", flush=True)

        try:
            body_text = page.inner_text("body")
        except Exception as exc:
            body_text = ""
            print(f"could not read body inner_text: {exc}", flush=True)
        print(f"body inner_text length: {len(body_text)}", flush=True)
        print(f"body inner_text (first 3000 chars):\n{body_text[:3000]}", flush=True)
        print(f"body inner_text (last 2000 chars):\n{body_text[-2000:]}", flush=True)
        time_matches = re.findall(r"\b\d{2}:\d{2}\b", body_text)
        print(f"HH:MM-shaped strings in body text: {time_matches[:30]}", flush=True)
        for name in ("Oxford", "Paddington"):
            count = body_text.count(name)
            print(f"'{name}' appears {count} time(s) in visible body text", flush=True)

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
