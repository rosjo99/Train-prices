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
    csp_injected_once = False

    def _route_handler(route):
        # A route handler must never raise — an unhandled exception here
        # (e.g. from a Service Worker request, which has no associated
        # frame and raises on `request.frame` access rather than returning
        # None) crashes request handling for the whole browser session, not
        # just this one request. Always fall through to route.continue_()
        # on anything unexpected.
        nonlocal csp_injected_once
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
            if is_subframe_doc:
                from urllib.parse import urlparse as _urlparse

                sub_host = _urlparse(request.url).hostname or ""
                # Run 18 evidence: the top-frame JS overrides below (fake
                # window.open, suppress location.assign/.replace, override
                # the href setter) never once logged as firing, yet the
                # booking.com redirect still happened ~400ms after the
                # click — regardless. Browsers let a CROSS-ORIGIN iframe
                # call top.location.assign()/.replace() directly, as a
                # deliberate carve-out that a parent page's own JS cannot
                # intercept or shadow (unlike same-origin navigation calls).
                # That fully explains the pattern: the redirect isn't coming
                # from our own frame's JS at all, it's issued by some ad
                # iframe we haven't identified. A curated domain keyword
                # list is always one unseen domain behind. Flip from
                # denylist to allowlist: block ANY cross-origin iframe
                # *document* load outright, whatever its domain, keeping
                # only nationalrail.co.uk's own iframes (if any) alive.
                # This only blocks iframe navigations — other resource
                # types (script/xhr/img) from ad hosts still load, which
                # earlier testing showed the page's own click handling
                # depends on.
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
                from urllib.parse import urlparse

                host = urlparse(request.url).hostname or ""
                if not host.endswith(NRE_HOST_SUFFIX):
                    print(f"blocked hijack navigation to {request.url} (backstop)", flush=True)
                    route.fulfill(status=200, content_type="text/html", body="<html></html>")
                    return

                # Kitchen-sink measure: this is NRE's own top-level document
                # load (the only one we expect — everything after this is a
                # client-side SPA state change, not a real navigation). Inject
                # a strict CSP that blocks ALL third-party script execution
                # outright, cutting off the entire ad ecosystem (including
                # whatever fires the redirect, wherever it actually lives) at
                # the root, rather than trying to enumerate every possible
                # trigger domain one at a time. 'self' covers NRE's own
                # same-origin bundle (served from this same host); inline/eval
                # stay allowed since Next.js hydration data and the app's own
                # bundle commonly need them.
                if not csp_injected_once and request.resource_type == "document":
                    try:
                        response = route.fetch()
                        body = response.text()
                        csp_meta = (
                            '<meta http-equiv="Content-Security-Policy" '
                            "content=\"script-src 'self' 'unsafe-inline' 'unsafe-eval';\">"
                        )
                        if "<head>" in body:
                            body = body.replace("<head>", "<head>" + csp_meta, 1)
                            csp_injected_once = True
                            print("injected strict CSP into main document", flush=True)
                            route.fulfill(response=response, body=body)
                            return
                    except Exception as exc:
                        print(f"CSP injection failed, serving unmodified: {exc}", flush=True)
                        # fall through to route.continue_()
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
            r"""
            // Evidence from a previous run: window.open() gets called, we
            // suppressed it by returning null, and ~68ms later the SAME
            // script fell back to a same-tab redirect that .assign()/
            // .replace() overrides didn't catch — meaning it wasn't calling
            // those methods, and returning null (a standard "popup blocked"
            // signal) is itself what triggers this fallback-to-same-tab
            // logic. Test A: return a truthy, window-shaped dummy object
            // instead of null, so the calling script believes the popup
            // succeeded and never reaches its fallback branch at all.
            window.open = () => {
              console.log('[probe] window.open faked (dummy object returned)');
              return {
                closed: false,
                close: () => {},
                focus: () => {},
                blur: () => {},
                postMessage: () => {},
                location: { href: 'about:blank' },
                document: { write: () => {}, close: () => {} },
              };
            };
            try {
              const origAssign = window.location.assign.bind(window.location);
              const origReplace = window.location.replace.bind(window.location);
              window.location.assign = function(url) {
                if (!String(url).includes('nationalrail.co.uk')) {
                  console.log('[probe] location.assign suppressed: ' + url);
                  return;
                }
                return origAssign(url);
              };
              window.location.replace = function(url) {
                if (!String(url).includes('nationalrail.co.uk')) {
                  console.log('[probe] location.replace suppressed: ' + url);
                  return;
                }
                return origReplace(url);
              };
            } catch (e) {
              console.log('[probe] location override failed: ' + e);
            }
            // Test B, independent of A: try to intercept a direct
            // `location.href = url` assignment (neither assign() nor
            // replace(), the most common plain redirect pattern) by
            // overriding the href setter on Location.prototype. Location
            // objects are spec-mandated to resist exactly this kind of
            // override for cross-origin security reasons, so this may
            // simply fail — caught safely either way, it's a free attempt
            // layered on top of A, not a replacement for it.
            try {
              const locProto = Object.getPrototypeOf(window.location);
              const hrefDesc = Object.getOwnPropertyDescriptor(locProto, 'href');
              if (hrefDesc && hrefDesc.configurable && hrefDesc.set) {
                const originalSet = hrefDesc.set.bind(window.location);
                Object.defineProperty(locProto, 'href', {
                  configurable: true,
                  enumerable: hrefDesc.enumerable,
                  get: hrefDesc.get,
                  set: function(url) {
                    if (!String(url).includes('nationalrail.co.uk')) {
                      console.log('[probe] location.href setter suppressed: ' + url);
                      return;
                    }
                    return originalSet(url);
                  },
                });
                console.log('[probe] location.href setter override installed');
              } else {
                console.log('[probe] location.href descriptor not configurable, skipped');
              }
            } catch (e) {
              console.log('[probe] location.href override failed: ' + e);
            }
            """
        )

        page = context.new_page()

        # Mitigation #3 (independent of the two JS overrides above):
        # registered only after our own main page exists, so it never
        # fires for that intentional page — if a popup/new tab genuinely
        # opens anyway (e.g. via a plain <a target="_blank"> the browser
        # handles natively, not through window.open() at all), catch and
        # close it immediately so it can never affect the main page —
        # exactly "open in a new tab that I could close/ignore".
        def _on_new_page(new_page):
            if new_page is page:
                return
            try:
                print(f"[popup] new page opened: {new_page.url} — closing it", flush=True)
                new_page.close()
            except Exception as exc:
                print(f"[popup] failed to close new page: {exc}", flush=True)

        context.on("page", _on_new_page)

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

        def _force_uncheck_via_native_setter(label: str) -> None:
            """A plain .click() on the checkbox executed without error in a
            previous run but never actually changed its checked state — a
            strong sign this is a React-controlled checkbox where the real
            clickable surface is a different element (no <label> exists in
            the DOM), and/or React's own event handling doesn't register a
            raw DOM click the way it expects. The standard way to force a
            controlled input's value in a way React's synthetic event system
            actually notices: set the value via the native property setter
            (bypassing React's own tracked-value shadowing of the property),
            then dispatch real 'input'/'change' events that bubble up to
            wherever React attached its listeners.
            """
            try:
                result = page.evaluate(
                    """
                    () => {
                      const el = document.querySelector(
                        "input[type='checkbox'][value='find_hotels']"
                      );
                      if (!el) return 'not_found';
                      if (!el.checked) return 'already_unchecked';
                      try {
                        const proto = window.HTMLInputElement.prototype;
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                          proto, 'checked'
                        ).set;
                        nativeSetter.call(el, false);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                      } catch (e) {
                        return 'error: ' + e;
                      }
                      return el.checked ? 'still_checked' : 'confirmed_unchecked';
                    }
                    """
                )
                print(f"[{label}] native-setter uncheck result: {result}", flush=True)
            except Exception as exc:
                print(f"[{label}] native-setter uncheck failed: {exc}", flush=True)

        def _try_uncheck_find_hotels(label: str) -> None:
            """Try several interaction strategies against the custom-styled
            find_hotels checkbox, since .uncheck() timed out in a previous
            run (Locator.uncheck also waits to verify the resulting checked
            state, which a custom checkbox implementation may never satisfy
            the way it expects) — a plain .click() doesn't wait for that,
            but also didn't work (checkbox still checked afterward). Try the
            native-setter approach first since it's the more targeted fix
            for a React-controlled input; fall back to a plain click.
            """
            _force_uncheck_via_native_setter(label)
            try:
                hotels_checkbox = page.locator("input[type='checkbox'][value='find_hotels']")
                count = hotels_checkbox.count()
                if count == 0:
                    print(f"[{label}] find_hotels checkbox not found", flush=True)
                    return
                if not hotels_checkbox.first.is_checked():
                    print(f"[{label}] find_hotels checkbox already unchecked", flush=True)
                    return
                try:
                    hotels_checkbox.first.click(force=True, timeout=3000)
                    print(f"[{label}] clicked find_hotels checkbox (uncheck attempt)", flush=True)
                except Exception as exc:
                    print(f"[{label}] clicking find_hotels checkbox failed: {exc}", flush=True)
                    return
                if not hotels_checkbox.first.is_checked():
                    print(f"[{label}] find_hotels checkbox confirmed unchecked", flush=True)
                else:
                    print(f"[{label}] find_hotels checkbox still checked after click", flush=True)
            except Exception as exc:
                print(f"[{label}] find_hotels handling error: {exc}", flush=True)

        _try_uncheck_find_hotels("before-fill")

        # Confirmed via the previous probe's element dump: the decoy click
        # opens a real modal with #jp-origin / #jp-destination (both
        # enabled, role=combobox with a live results list), single/return/
        # open radios (name="jp-ticket-type", "single" checked by default —
        # matches our need), an "Add Railcard" button, and the real submit
        # button #button-jp labeled "Get times and prices" (strong signal
        # NRE shows fares directly, not just timetables).
        def _select_autocomplete_option(field_id: str, field_label: str) -> None:
            """Prefer clicking the actual visible autocomplete suggestion
            with the mouse over blind ArrowDown+Enter. Bug found in a
            previous run: querying `[role='option']` globally grabbed the
            wrong listbox's option once both origin and destination had
            active suggestion lists on screen simultaneously (origin ended
            up as an unresolved "9 stations found" instead of a confirmed
            selection). Fix: read the input's own `aria-controls` attribute
            (e.g. "sp-jp-origin-results-list") to scope the option query to
            THIS field's listbox specifically. Falls back to keyboard if
            that fails for any reason.
            """
            try:
                list_id = page.locator(f"#{field_id}").get_attribute("aria-controls")
                if list_id:
                    option = page.locator(f"#{list_id} [role='option']").first
                    if option.count() > 0:
                        option.click(timeout=3000)
                        print(
                            f"clicked scoped autocomplete option for {field_label} "
                            f"(listbox #{list_id})",
                            flush=True,
                        )
                        return
                    print(f"no options found in listbox #{list_id} for {field_label}", flush=True)
                else:
                    print(f"{field_id} has no aria-controls attribute yet", flush=True)
            except Exception as exc:
                print(f"clicking scoped autocomplete option for {field_label} failed: {exc}", flush=True)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            print(f"fell back to ArrowDown+Enter for {field_label}", flush=True)

        try:
            origin_input = page.locator("#jp-origin")
            origin_input.fill("Oxford", timeout=8000)
            page.wait_for_timeout(1200)
            _select_autocomplete_option("jp-origin", "origin")
            page.wait_for_timeout(500)

            dest_input = page.locator("#jp-destination")
            dest_input.fill("London Paddington", timeout=8000)
            page.wait_for_timeout(1200)
            _select_autocomplete_option("jp-destination", "destination")
            page.wait_for_timeout(500)

            filled = True
            print("filled #jp-origin and #jp-destination", flush=True)
            for fid in ("jp-origin", "jp-destination"):
                try:
                    val = page.locator(f"#{fid}").input_value()
                    print(f"confirmed value of #{fid}: {val!r}", flush=True)
                except Exception as exc:
                    print(f"reading value of #{fid} failed: {exc}", flush=True)
        except Exception as exc:
            print(f"filling #jp-origin/#jp-destination failed: {exc}", flush=True)

        # Re-check right after filling too, in case the destination
        # selection re-renders the widget and re-checks it.
        _try_uncheck_find_hotels("after-fill")

        # Railcard selection (per user request: 16-25 Railcard specifically).
        # A previous run's element dump revealed the real structure once the
        # panel is open: #railcard-0 (a <label for="railcard-0">Choose 1st
        # railcard</label> pairing — a native <select>) for the railcard
        # TYPE, and a separate #railcard-0-count for the QUANTITY — the user
        # confirmed via the screenshots that both need to be set explicitly.
        # The earlier text-matching click approach never found a "16-25"
        # element because it's <option> text inside a closed <select>, not
        # visible/clickable DOM content.
        try:
            add_railcard_btn = page.locator("button[aria-label='Add railcard']")
            if add_railcard_btn.count() > 0:
                add_railcard_btn.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                print("clicked Add Railcard", flush=True)
                page.screenshot(path=str(args.out / "03a_railcard_panel.png"))
                (args.out / "03a_railcard_panel.html").write_text(page.content(), encoding="utf-8")
                elements = page.eval_on_selector_all(
                    "input, button, select, [role='option'], label",
                    "els => els.map(e => e.outerHTML.slice(0, 300))",
                )
                print(f"--- {len(elements)} elements after clicking Add Railcard ---", flush=True)
                for html in elements:
                    print("  RAILCARD_ELEM:", html, flush=True)

                railcard_select = page.locator("#railcard-0")
                if railcard_select.count() > 0:
                    try:
                        railcard_select.select_option(label="16-25 Railcard")
                        print("selected 16-25 Railcard via #railcard-0 select_option", flush=True)
                    except Exception as exc:
                        print(f"select_option on #railcard-0 failed: {exc}", flush=True)
                        # Fall back to a plain click-based approach in case
                        # this isn't actually a native <select>.
                        for sel in ("text=16-25 Railcard", "text=16-25"):
                            try:
                                opt = page.locator(sel)
                                if opt.count() > 0:
                                    opt.first.click(timeout=3000)
                                    print(f"selected 16-25 railcard via fallback {sel!r}", flush=True)
                                    break
                            except Exception:
                                continue
                else:
                    print("#railcard-0 not found", flush=True)

                count_select = page.locator("#railcard-0-count")
                if count_select.count() > 0:
                    try:
                        count_select.select_option("1")
                        print("selected quantity 1 via #railcard-0-count select_option", flush=True)
                    except Exception as exc:
                        print(f"select_option on #railcard-0-count failed: {exc}", flush=True)
                else:
                    print("#railcard-0-count not found", flush=True)

                page.wait_for_timeout(500)
                page.screenshot(path=str(args.out / "03b_after_railcard_select.png"))
                (args.out / "03b_after_railcard_select.html").write_text(page.content(), encoding="utf-8")
            else:
                print("Add Railcard button not found", flush=True)
        except Exception as exc:
            print(f"railcard flow failed: {exc}", flush=True)

        # Confirmed via the DOM dump: after clicking Add Railcard, the
        # find_hotels checkbox was STILL checked="" despite two earlier
        # uncheck passes (both of which ran BEFORE the railcard panel
        # opened) — the panel expanding evidently re-renders or re-checks
        # it. A third pass here, after the railcard flow, right before
        # submit, is needed to actually catch it in its final state.
        _try_uncheck_find_hotels("after-railcard")

        page.screenshot(path=str(args.out / "04_after_fill.png"))
        (args.out / "04_after_fill.html").write_text(page.content(), encoding="utf-8")

        if filled:
            try:
                page.locator("#button-jp").click(timeout=5000)
                print("clicked #button-jp (Get times and prices)", flush=True)
            except Exception as exc:
                print(f"clicking #button-jp failed: {exc}", flush=True)

            # Give the results more time: the previous run reached this
            # point without being hijacked (a first!) but found zero prices
            # after only an 8s wait — plausibly not enough time for the
            # journey-search round trip plus render.
            page.wait_for_timeout(15000)
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

            # The raw HTML head alone runs to hundreds of script tags (the
            # workflow's log printer only shows the first 100 lines of each
            # HTML file, which never reaches the body) — print the page's
            # actual VISIBLE TEXT directly here instead, bypassing that
            # truncation entirely, plus a targeted scan for departure-time-
            # shaped strings and station names near them.
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
