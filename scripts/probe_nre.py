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
import re
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

    # NRE's page loads third-party ad networks that fire an uncontrolled
    # top-level redirect (observed: Booking.com hotel search) part-way
    # through the flow. Two things learned the hard way:
    # 1. Blocking ad domains outright breaks the page's own click handler
    #    (it depends on one of those sub-resource requests completing
    #    before it opens the search modal) — so sub-resources must load.
    # 2. Aborting an in-progress *main-frame* navigation leaves the tab on
    #    Chrome's own network-error page (chrome-error://chromewebdata/),
    #    not back on the original page — worse than the hijack itself.
    # The actual fix: block the ad IFRAME's document load itself (a
    # sub-frame, not the main frame) from known ad hosts, so its creative
    # never executes and never gets the chance to redirect the top frame in
    # the first place. The main-frame guard below is kept only as a last-
    # resort backstop using fulfill() (a harmless blank page) instead of
    # abort(), so even an unanticipated redirect source doesn't blank the
    # tab with a browser error. None of this relates to NRE's own bot
    # protection (block markers came back "none"/benign on every run) —
    # it's purely "don't let an ad hijack the tab", same as a pop-up
    # blocker.
    NRE_HOST_SUFFIX = "nationalrail.co.uk"
    AD_IFRAME_HOST_KEYWORDS = (
        "doubleclick.net", "googlesyndication.com", "openx.net",
        "booking.com", "adnxs.com", "taboola.com", "outbrain.com",
        "criteo.com", "amazon-adsystem.com", "pubmatic.com",
        "rubiconproject.com", "casalemedia.com", "adsrvr.org",
    )

    def _route_handler(route):
        # A route handler must never raise — an unhandled exception here
        # (e.g. from a Service Worker request, which has no associated
        # frame and raises on `request.frame` access rather than returning
        # None) crashes request handling for the whole browser session, not
        # just this one request. Always fall through to route.continue_()
        # on anything unexpected.
        try:
            request = route.request
            try:
                frame = request.frame
            except Exception:
                frame = None  # e.g. Service Worker / other frameless requests

            is_subframe_doc = (
                request.resource_type == "document"
                and frame is not None
                and frame.parent_frame is not None
            )
            if is_subframe_doc and any(kw in request.url for kw in AD_IFRAME_HOST_KEYWORDS):
                print(f"blocked ad iframe: {request.url}", flush=True)
                route.abort()
                return

            if request.is_navigation_request() and frame is not None and frame.parent_frame is None:
                from urllib.parse import urlparse

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

        # User's hypothesis, worth taking seriously: in a real browser this
        # "Find hotels" widget likely opens window.open(url, "_blank") — a
        # harmless new tab. window.open() only actually opens a new tab when
        # called synchronously inside a trusted user gesture; if the ad
        # script does it after an async step (a common pattern — ping an ad
        # server, then open the deal page), the gesture window can lapse and
        # some scripts fall back to window.location = url instead, hijacking
        # the current tab. Playwright's synthetic clicks may not sustain
        # that gesture the way continuous real input does, which would
        # explain why this never bothers a human but reliably hijacks us
        # here. Neutralising window.open at the JS level, before any page
        # script runs, tests this directly and stops the redirect at its
        # origin rather than reacting to it over the network after the tab
        # has already started navigating away.
        context.add_init_script(
            "window.open = () => { console.log('[probe] window.open suppressed'); return null; };"
        )
        page = context.new_page()
        page.on("response", on_response)
        page.on(
            "console",
            lambda msg: print(f"[console.{msg.type}] {msg.text}", flush=True)
            if "probe" in msg.text or "error" in msg.type
            else None,
        )

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

        # Dump every input/button element on the landing page so selectors
        # can be figured out from text even without viewing the screenshot.
        try:
            elements = page.eval_on_selector_all(
                "input, button, [role='combobox'], [role='listbox'], [role='option']",
                "els => els.map(e => e.outerHTML.slice(0, 300))",
            )
            print(f"--- {len(elements)} input/button/combobox elements on landing page ---", flush=True)
            for html in elements:
                print("  ELEM:", html, flush=True)
        except Exception as exc:
            print(f"element dump failed: {exc}", flush=True)

        # The static HTML's jp_preview_origin/destination inputs turned out
        # to be disabled, aria-hidden decoys (confirmed by an earlier probe
        # run) — likely a "click to open the real widget" pattern. Try a
        # forced click on the decoy first, then look for whatever became
        # enabled/interactable afterwards, before falling back to filling
        # the decoy directly.
        filled = False
        try:
            decoy = page.locator("#jp-preview-origin")
            if decoy.count() > 0:
                decoy.click(force=True, timeout=5000)
                page.wait_for_timeout(1000)
                print("force-clicked #jp-preview-origin", flush=True)
        except Exception as exc:
            print(f"force-click on decoy failed: {exc}", flush=True)

        page.screenshot(path=str(args.out / "02_after_decoy_click.png"))
        (args.out / "02_after_decoy_click.html").write_text(page.content(), encoding="utf-8")
        try:
            elements = page.eval_on_selector_all(
                "input, button, [role='combobox'], [role='listbox'], [role='option']",
                "els => els.map(e => e.outerHTML.slice(0, 300))",
            )
            print(f"--- {len(elements)} input/button/combobox elements after decoy click ---", flush=True)
            for html in elements:
                print("  ELEM:", html, flush=True)
        except Exception as exc:
            print(f"element dump failed: {exc}", flush=True)

        # Uncheck the pre-checked "Find hotels" affiliate widget BEFORE
        # touching origin/destination at all. Root-cause finding: the
        # previous run's uncheck attempt ran AFTER filling+selecting both
        # origin and destination, and the Booking.com redirect fired
        # essentially concurrently with the destination selection itself
        # (half a second apart in the log) — the uncheck code never even
        # got to print success or failure, meaning the page was likely
        # already gone (navigated to the blank backstop page) by the time
        # it ran. The trigger is almost certainly "selecting a destination
        # while this checkbox is checked", so it must be unchecked first.
        try:
            hotels_checkbox = page.locator("input[type='checkbox'][value='find_hotels']")
            if hotels_checkbox.count() > 0 and hotels_checkbox.first.is_checked():
                hotels_checkbox.first.uncheck(force=True, timeout=3000)
                print("unchecked find_hotels checkbox (before filling destination)", flush=True)
            else:
                print(
                    f"find_hotels checkbox not found/not checked "
                    f"(count={hotels_checkbox.count()})",
                    flush=True,
                )
        except Exception as exc:
            print(f"unchecking find_hotels failed: {exc}", flush=True)

        # Confirmed via the previous probe's element dump: the decoy click
        # opens a real modal with #jp-origin / #jp-destination (both
        # enabled, role=combobox with a live results list), single/return/
        # open radios (name="jp-ticket-type", "single" checked by default —
        # matches our need), an "Add Railcard" button, and the real submit
        # button #button-jp labeled "Get times and prices" (strong signal
        # NRE shows fares directly, not just timetables).
        try:
            origin_input = page.locator("#jp-origin")
            origin_input.fill("Oxford", timeout=8000)
            page.wait_for_timeout(1200)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)

            dest_input = page.locator("#jp-destination")
            dest_input.fill("London Paddington", timeout=8000)
            page.wait_for_timeout(1200)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)

            filled = True
            print("filled #jp-origin and #jp-destination", flush=True)
        except Exception as exc:
            print(f"filling #jp-origin/#jp-destination failed: {exc}", flush=True)

        # Re-check right after filling too, in case the destination
        # selection re-renders the widget and re-checks it (a plausible
        # explanation if the first uncheck didn't stick).
        try:
            hotels_checkbox = page.locator("input[type='checkbox'][value='find_hotels']")
            if hotels_checkbox.count() > 0 and hotels_checkbox.first.is_checked():
                hotels_checkbox.first.uncheck(force=True, timeout=3000)
                print("unchecked find_hotels checkbox (again, after filling)", flush=True)
        except Exception as exc:
            print(f"second unchecking find_hotels failed: {exc}", flush=True)

        # Railcard selection (per user request: 16-25 Railcard specifically).
        try:
            add_railcard_btn = page.locator("button[aria-label='Add railcard']")
            if add_railcard_btn.count() > 0:
                add_railcard_btn.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                print("clicked Add Railcard", flush=True)
                page.screenshot(path=str(args.out / "03a_railcard_panel.png"))
                (args.out / "03a_railcard_panel.html").write_text(page.content(), encoding="utf-8")
                elements = page.eval_on_selector_all(
                    "input, button, [role='option'], label",
                    "els => els.map(e => e.outerHTML.slice(0, 300))",
                )
                print(f"--- {len(elements)} elements after clicking Add Railcard ---", flush=True)
                for html in elements:
                    print("  RAILCARD_ELEM:", html, flush=True)

                selected = False
                for sel in (
                    "text=16-25 Railcard",
                    "label:has-text('16-25')",
                    "button:has-text('16-25')",
                    "[role='option']:has-text('16-25')",
                    "text=16-25",
                ):
                    try:
                        opt = page.locator(sel)
                        if opt.count() > 0:
                            opt.first.click(timeout=5000)
                            print(f"selected 16-25 railcard via {sel!r}", flush=True)
                            selected = True
                            page.wait_for_timeout(500)
                            break
                    except Exception:
                        continue
                if not selected:
                    print("could not find a 16-25 railcard option to click", flush=True)

                # Look for a confirm/apply/done button to close the panel.
                for sel in (
                    "button:has-text('Apply')",
                    "button:has-text('Done')",
                    "button:has-text('Add')",
                    "button:has-text('Confirm')",
                ):
                    try:
                        btn = page.locator(sel)
                        if btn.count() > 0:
                            btn.first.click(timeout=3000)
                            print(f"confirmed railcard panel via {sel!r}", flush=True)
                            break
                    except Exception:
                        continue

                page.wait_for_timeout(500)
                page.screenshot(path=str(args.out / "03b_after_railcard_select.png"))
                (args.out / "03b_after_railcard_select.html").write_text(page.content(), encoding="utf-8")
            else:
                print("Add Railcard button not found", flush=True)
        except Exception as exc:
            print(f"railcard flow failed: {exc}", flush=True)

        page.screenshot(path=str(args.out / "04_after_fill.png"))
        (args.out / "04_after_fill.html").write_text(page.content(), encoding="utf-8")

        if filled:
            try:
                page.locator("#button-jp").click(timeout=5000)
                print("clicked #button-jp (Get times and prices)", flush=True)
            except Exception as exc:
                print(f"clicking #button-jp failed: {exc}", flush=True)

            page.wait_for_timeout(8000)
            page.screenshot(path=str(args.out / "05_after_submit.png"))
            (args.out / "05_after_submit.html").write_text(page.content(), encoding="utf-8")
            print("current URL after submit:", page.url, flush=True)

            if "nationalrail.co.uk" not in page.url:
                print(
                    f"WARNING: navigated away from nationalrail.co.uk to {page.url} "
                    "— any prices found below are NOT NRE fares",
                    flush=True,
                )

            # Look for anything price-shaped (£ followed by digits) anywhere
            # in the resulting page — a quick, format-agnostic signal for
            # whether fares actually rendered, independent of guessing the
            # right result-card selector.
            price_matches = re.findall(r"£\s?\d+(?:\.\d{2})?", page.content())
            print(f"£-price-shaped strings found on results page: {price_matches[:20]}", flush=True)

        (args.out / "captured_responses.json").write_text(
            json.dumps(captured_responses, indent=2, default=str)[:200000],
            encoding="utf-8",
        )
        print(f"captured {len(captured_responses)} matching responses", flush=True)

        full_content = page.content()
        content_lower = full_content.lower()
        blocked_markers = ("captcha", "access denied", "are you a robot", "datadome", "cloudflare")
        hits = [m for m in blocked_markers if m in content_lower]
        print("block markers present on final page:", hits or "none", flush=True)
        # Print surrounding context for each hit so it's clear whether this
        # is a real block page or just an incidental mention (e.g. a
        # CDN/CAPTCHA script tag used elsewhere on the site, unrelated to
        # this specific page load).
        for marker in hits:
            idx = content_lower.find(marker)
            print(
                f"  context for {marker!r}: ...{full_content[max(0, idx - 150):idx + 150]}...",
                flush=True,
            )

        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
