"""Diagnostic: can Camoufox reach CrossCountry's booking site?
Mirrors scripts/probe_camoufox_tpe.py's approach and output shape, for a
CrossCountry deep-link instead of TPE's. This exists to answer one
question: does CrossCountry's Cloudflare protection (confirmed via plain
curl — see docs/plans/006-cross-country-feasibility.md if one gets
written) also block a real GitHub-Actions-hosted Camoufox browser, or only
non-JS requests? A curl request from this repo's dev environment got a
Cloudflare "Sorry, you have been blocked" page on both the deep-link URL
and the bare homepage — this probe re-tests that from an actual GitHub
Actions runner IP with real JS execution, since a WAF block on a
datacenter/cloud IP range would apply regardless of browser fidelity.
Usage:
    python scripts/probe_camoufox_crosscountry.py --out-dir /tmp/camoufox-probe-cc
Optional:
    python scripts/probe_camoufox_crosscountry.py \
        --out-dir /tmp/camoufox-probe-cc \
        --headed
"""
from __future__ import annotations
import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from camoufox.sync_api import Camoufox, NewContext
from playwright.sync_api import Browser, BrowserContext, Page

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEEP_LINK = (
    "https://buy.crosscountrytrains.co.uk/search"
    "?origin=GBOXF&destination=GBQQP&adults=1&children=0"
    "&outboundTime=2026-09-08T07:00:00&outboundTimeType=DEPARTURE"
    "&railcards=%5B%7B%22Code%22:%22UK_YOUTH%22,%22Number%22:1,"
    "%22Type%22:%22DISCOUNT_CARD%22%7D%5D"
)
HOMEPAGE = "https://www.crosscountrytrains.co.uk/"

# Same vocabulary as scripts/probe_camoufox_tpe.py, plus the exact phrases
# observed on the curl-fetched Cloudflare block page for this site
# ("Attention Required! | Cloudflare", "Sorry, you have been blocked").
STRONG_BLOCK_MARKERS = (
    "are you a robot",
    "access denied",
    "sorry, you have been blocked",
    "attention required",
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "enable javascript and cookies to continue",
    "verify you are human",
    "checking your browser",
)
WEAK_MARKERS = (
    "captcha",
    "datadome",
)
JOURNEY_MARKERS = (
    "oxford",
    "paddington",
    "depart",
    "arrive",
    "journey",
    "ticket",
    "adult",
    "return",
    "single",
    "£",
)
MIN_DELAY = 3.0
MAX_DELAY = 8.0
PAGE_LOAD_TIMEOUT = 60_000

# ---------------------------------------------------------------------------
# Helpers (unchanged from scripts/probe_camoufox_tpe.py)
# ---------------------------------------------------------------------------
def human_delay(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY) -> None:
    time.sleep(random.uniform(min_s, max_s))

def random_mouse_move(page: Page, steps: int = 8) -> None:
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    x = random.randint(100, min(400, viewport["width"] - 20))
    y = random.randint(100, min(400, viewport["height"] - 20))
    for _ in range(steps):
        x += random.randint(-60, 100)
        y += random.randint(-40, 70)
        x = max(10, min(viewport["width"] - 10, x))
        y = max(10, min(viewport["height"] - 10, y))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.15))

def human_interaction(page: Page) -> None:
    try:
        random_mouse_move(page)
        human_delay(0.8, 2.0)
        viewport = page.viewport_size or {"width": 1366, "height": 768}
        page.mouse.move(
            viewport["width"] // 2 + random.randint(-80, 80),
            viewport["height"] // 3 + random.randint(-40, 40),
        )
        time.sleep(random.uniform(0.5, 1.2))
        page.mouse.wheel(0, random.randint(150, 450))
        human_delay(0.8, 2.0)
    except Exception as exc:
        print(f"[warn] Interaction failed: {exc}", file=sys.stderr)

def marker_contexts(text: str, markers: tuple[str, ...]) -> list[dict[str, str]]:
    lowered = text.lower()
    results: list[dict[str, str]] = []
    for marker in markers:
        start = 0
        while True:
            pos = lowered.find(marker, start)
            if pos == -1:
                break
            left = max(0, pos - 250)
            right = min(len(text), pos + len(marker) + 500)
            results.append(
                {
                    "marker": marker,
                    "snippet": text[left:right].replace("\n", " ")[:750],
                }
            )
            start = pos + len(marker)
            if len(results) >= 20:
                return results
    return results

def summarise_responses(responses: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for response in responses:
        key = str(response["status"])
        status_counts[key] = status_counts.get(key, 0) + 1
    interesting = []
    for response in responses:
        url = response["url"].lower()
        if (
            response["status"] >= 400
            or any(
                token in url
                for token in (
                    "/api/",
                    "graphql",
                    "journey",
                    "search",
                    "availability",
                    "booking",
                    "fare",
                    "ajax",
                )
            )
        ):
            interesting.append(response)
    return {
        "total_responses": len(responses),
        "status_counts": status_counts,
        "interesting_responses": interesting[:100],
    }

def get_browser_fingerprint(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            """
            () => ({
                userAgent: navigator.userAgent,
                platform: navigator.platform,
                language: navigator.language,
                languages: Array.from(navigator.languages || []),
                webdriver: navigator.webdriver,
                hardwareConcurrency: navigator.hardwareConcurrency,
                deviceMemory: navigator.deviceMemory ?? null,
                maxTouchPoints: navigator.maxTouchPoints,
                screen: {
                    width: screen.width,
                    height: screen.height,
                    availWidth: screen.availWidth,
                    availHeight: screen.availHeight,
                    colorDepth: screen.colorDepth,
                    pixelDepth: screen.pixelDepth
                },
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                viewport: {
                    width: window.innerWidth,
                    height: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio
                }
            })
            """
        )
    except Exception as exc:
        return {"error": str(exc)}

def page_assessment(
    *,
    title: str,
    visible_text: str,
    final_url: str,
    responses: list[dict[str, Any]],
) -> dict[str, Any]:
    lowered = visible_text.lower()
    strong_hits = [marker for marker in STRONG_BLOCK_MARKERS if marker in lowered]
    weak_hits = [marker for marker in WEAK_MARKERS if marker in lowered]
    journey_hits = [marker for marker in JOURNEY_MARKERS if marker in lowered]
    error_statuses = sorted(
        {response["status"] for response in responses if response["status"] >= 400}
    )
    if strong_hits:
        classification = "challenge_or_block"
    elif not journey_hits and error_statuses:
        classification = "possible_http_failure"
    elif journey_hits:
        classification = "page_loaded"
    else:
        classification = "loaded_but_unclear"
    return {
        "classification": classification,
        "final_url": final_url,
        "title": title,
        "journey_marker_hits": journey_hits,
        "strong_block_markers": strong_hits,
        "weak_markers": weak_hits,
        "http_error_statuses": error_statuses,
        "looks_like_journey_content": len(journey_hits) >= 3,
    }

def try_click_challenge_checkbox(page: Page) -> dict[str, Any]:
    """
    After a random 1–3 s delay, attempt to click a Cloudflare Turnstile /
    "Verify you are human" style checkbox.
    Primary strategy: locate challenges.cloudflare.com iframe, get its
    bounding box, and click at the classic left-side checkbox offset
    (width/9, height/2). Fallback: common on-page selectors.
    Returns a small diagnostic dict so the probe result can record what
    happened.
    """
    info: dict[str, Any] = {
        "attempted": True,
        "delay_s": None,
        "clicked": False,
        "method": None,
        "error": None,
    }
    try:
        delay = random.uniform(1.0, 3.0)
        info["delay_s"] = round(delay, 3)
        print(f"[info] Challenge checkbox: sleeping {info['delay_s']:.2f}s before click attempt")
        time.sleep(delay)

        # --- Primary: Cloudflare Turnstile / challenge iframe by URL ---
        for frame in page.frames:
            frame_url = (frame.url or "").lower()
            if "challenges.cloudflare.com" not in frame_url:
                continue
            try:
                frame_el = frame.frame_element()
                box = frame_el.bounding_box()
                if not box or box.get("width", 0) < 20 or box.get("height", 0) < 20:
                    continue
                # Checkbox sits on the left of the widget
                click_x = box["x"] + (box["width"] / 9.0)
                click_y = box["y"] + (box["height"] / 2.0)
                print(
                    f"[info] Clicking CF challenge iframe checkbox at "
                    f"({click_x:.1f}, {click_y:.1f}) "
                    f"(frame={frame_url[:80]}…)"
                )
                page.mouse.move(click_x, click_y, steps=random.randint(5, 12))
                time.sleep(random.uniform(0.08, 0.25))
                page.mouse.click(click_x, click_y)
                info["clicked"] = True
                info["method"] = "iframe_bbox_offset"
                # Give the challenge a moment to process
                time.sleep(random.uniform(2.0, 4.0))
                return info
            except Exception as frame_exc:
                print(f"[warn] Frame click attempt failed: {frame_exc}", file=sys.stderr)
                continue

        # --- Fallback: common visible selectors on the main page ---
        selectors = [
            "label.ctp-checkbox-label",
            "input[type='checkbox']",
            ".cf-turnstile",
            ".cf-turnstile-wrapper",
            "[data-sitekey]",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[title*='Cloudflare']",
            "iframe[title*='security challenge']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                if not loc.is_visible(timeout=1500):
                    continue
                print(f"[info] Clicking challenge element via selector: {sel}")
                loc.click(timeout=5000, force=False)
                info["clicked"] = True
                info["method"] = f"selector:{sel}"
                time.sleep(random.uniform(2.0, 4.0))
                return info
            except Exception:
                continue

        print("[info] No challenge checkbox / Turnstile iframe found to click")
        info["method"] = "none_found"
    except Exception as exc:
        info["error"] = repr(exc)
        print(f"[warn] try_click_challenge_checkbox failed: {exc}", file=sys.stderr)
    return info

# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
def probe(
    context: BrowserContext,
    url: str,
    label: str,
    out_dir: Path,
) -> dict[str, Any]:
    page = context.new_page()
    responses: list[dict[str, Any]] = []

    def on_response(response: Any) -> None:
        try:
            responses.append(
                {
                    "status": response.status,
                    "method": response.request.method,
                    "resource_type": response.request.resource_type,
                    "url": response.url,
                }
            )
        except Exception:
            pass

    page.on("response", on_response)
    result: dict[str, Any] = {
        "label": label,
        "url": url,
    }
    try:
        print(f"[info] Navigating {label}: {url}")
        human_delay(1.5, 3.0)
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_TIMEOUT,
        )
        result["initial_http_status"] = response.status if response else None
        human_delay(4.0, 7.0)
        human_interaction(page)
        human_delay(3.0, 6.0)

        # Attempt to clear a visible Cloudflare / Turnstile tickbox
        # (especially relevant for the deep-link page).
        result["challenge_click"] = try_click_challenge_checkbox(page)

        # Extra settle time after a possible challenge solve
        human_delay(2.0, 4.0)

        result["final_url"] = page.url
        result["title"] = page.title()
        html = page.content()
        try:
            visible_text = page.locator("body").inner_text(timeout=10_000)
        except Exception:
            visible_text = ""
        result["content_length"] = len(html)
        result["visible_text_length"] = len(visible_text)
        result["visible_text_preview"] = visible_text[:5000]
        result["fingerprint"] = get_browser_fingerprint(page)
        result["response_summary"] = summarise_responses(responses)
        result["strong_marker_contexts"] = marker_contexts(
            visible_text, STRONG_BLOCK_MARKERS
        )
        result["weak_marker_contexts"] = marker_contexts(visible_text, WEAK_MARKERS)
        result["assessment"] = page_assessment(
            title=result["title"],
            visible_text=visible_text,
            final_url=result["final_url"],
            responses=responses,
        )
        (out_dir / f"{label}.html").write_text(html, encoding="utf-8")
        (out_dir / f"{label}.txt").write_text(visible_text, encoding="utf-8")
        page.screenshot(path=str(out_dir / f"{label}.png"), full_page=True)
        (out_dir / f"{label}-responses.json").write_text(
            json.dumps(responses, indent=2), encoding="utf-8"
        )
        result["ok"] = True
        print(
            f"[info] {label}: "
            f"{result['assessment']['classification']}; "
            f"title={result['title']!r}; "
            f"status={result['initial_http_status']}; "
            f"challenge_click={result['challenge_click']}"
        )
    except Exception as exc:
        result["ok"] = False
        result["error"] = repr(exc)
        try:
            page.screenshot(path=str(out_dir / f"{label}-error.png"), full_page=True)
        except Exception:
            pass
        print(f"[error] {label}: {exc}", file=sys.stderr)
    finally:
        result["responses"] = responses
        page.close()
    return result

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/tmp/camoufox-probe-cc")
    parser.add_argument(
        "--headed", action="store_true", help="Run with a visible browser window."
    )
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Camoufox is Firefox-based. Let Camoufox generate a coherent
    # fingerprint rather than injecting Chrome-specific patches. No proxy
    # here — the point of this probe is to see how CrossCountry's
    # Cloudflare protection treats a plain GitHub Actions runner IP.
    camoufox_config: dict[str, Any] = {
        "headless": not args.headed,
        "humanize": True,
        "locale": "en-GB,en-US,en",
        "os": "windows",
    }
    print(f"[info] Browser=Camoufox config={camoufox_config}")
    results: list[dict[str, Any]] = []
    try:
        with Camoufox(**camoufox_config) as browser:
            context = NewContext(
                browser,
                os=camoufox_config.get("os"),
                locale="en-GB,en-US,en",
                timezone_id="Europe/London",
                viewport={"width": 1366, "height": 768},
                color_scheme="light",
                java_script_enabled=True,
            )
            try:
                results.append(probe(context, HOMEPAGE, "homepage", out_dir))
                human_delay(5.0, 10.0)
                results.append(probe(context, DEEP_LINK, "deep-link", out_dir))
            finally:
                context.close()
    except Exception as exc:
        error = {
            "ok": False,
            "stage": "camoufox_launch",
            "error": repr(exc),
        }
        results.append(error)
        (out_dir / "launch-error.json").write_text(
            json.dumps(error, indent=2), encoding="utf-8"
        )
        print(f"[error] Camoufox launch failed: {exc}", file=sys.stderr)
    summary = {
        "browser": "Camoufox",
        "results": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    # Keep the GitHub Actions step successful so artifacts can always be
    # inspected, even when the diagnostic itself found a block.
    return 0

if __name__ == "__main__":
    sys.exit(main())
