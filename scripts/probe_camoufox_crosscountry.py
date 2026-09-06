"""Diagnostic: can Camoufox reach CrossCountry's booking site?
Faster version – should finish in ~1.5–2.5 min even on slow runners.
"""
from __future__ import annotations
import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any
from camoufox.sync_api import Camoufox, NewContext
from playwright.sync_api import BrowserContext, Page

DEEP_LINK = (
    "https://buy.crosscountrytrains.co.uk/search"
    "?origin=GBOXF&destination=GBQQP&adults=1&children=0"
    "&outboundTime=2026-09-08T07:00:00&outboundTimeType=DEPARTURE"
    "&railcards=%5B%7B%22Code%22:%22UK_YOUTH%22,%22Number%22:1,"
    "%22Type%22:%22DISCOUNT_CARD%22%7D%5D"
)
HOMEPAGE = "https://www.crosscountrytrains.co.uk/"

STRONG_BLOCK_MARKERS = (
    "are you a robot", "access denied", "sorry, you have been blocked",
    "attention required", "just a moment", "cf-browser-verification",
    "challenge-platform", "enable javascript and cookies to continue",
    "verify you are human", "checking your browser",
    "performing security verification",
)
WEAK_MARKERS = ("captcha", "datadome")
JOURNEY_MARKERS = (
    "oxford", "paddington", "depart", "arrive", "journey",
    "ticket", "adult", "return", "single", "£",
)
PAGE_LOAD_TIMEOUT = 45_000  # slightly tighter

def human_delay(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))

def random_mouse_move(page: Page, steps: int = 5) -> None:
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    x = random.randint(80, min(350, viewport["width"] - 20))
    y = random.randint(80, min(350, viewport["height"] - 20))
    for _ in range(steps):
        x += random.randint(-50, 80)
        y += random.randint(-30, 50)
        x = max(10, min(viewport["width"] - 10, x))
        y = max(10, min(viewport["height"] - 10, y))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.04, 0.12))

def human_interaction(page: Page) -> None:
    try:
        random_mouse_move(page)
        human_delay(0.4, 1.0)
        viewport = page.viewport_size or {"width": 1366, "height": 768}
        page.mouse.move(
            viewport["width"] // 2 + random.randint(-60, 60),
            viewport["height"] // 3 + random.randint(-30, 30),
        )
        time.sleep(random.uniform(0.3, 0.7))
        page.mouse.wheel(0, random.randint(100, 300))
        human_delay(0.4, 1.0)
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
            left = max(0, pos - 200)
            right = min(len(text), pos + len(marker) + 400)
            results.append({
                "marker": marker,
                "snippet": text[left:right].replace("\n", " ")[:600],
            })
            start = pos + len(marker)
            if len(results) >= 15:
                return results
    return results

def summarise_responses(responses: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for r in responses:
        key = str(r["status"])
        status_counts[key] = status_counts.get(key, 0) + 1
    interesting = [
        r for r in responses
        if r["status"] >= 400 or any(
            t in r["url"].lower()
            for t in ("/api/", "graphql", "journey", "search", "availability", "booking", "fare", "ajax")
        )
    ]
    return {
        "total_responses": len(responses),
        "status_counts": status_counts,
        "interesting_responses": interesting[:80],
    }

def get_browser_fingerprint(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate("""
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
                    width: screen.width, height: screen.height,
                    availWidth: screen.availWidth, availHeight: screen.availHeight,
                    colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth
                },
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                viewport: {
                    width: window.innerWidth, height: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio
                }
            })
        """)
    except Exception as exc:
        return {"error": str(exc)}

def still_on_challenge(page: Page) -> bool:
    try:
        title = (page.title() or "").lower()
        if "just a moment" in title:
            return True
        text = page.locator("body").inner_text(timeout=2500).lower()
        return any(m in text for m in (
            "performing security verification", "verify you are human",
            "checking your browser", "just a moment",
            "enable javascript and cookies to continue",
        ))
    except Exception:
        return True

def page_assessment(*, title: str, visible_text: str, final_url: str, responses: list[dict[str, Any]]) -> dict[str, Any]:
    lowered = visible_text.lower()
    title_l = (title or "").lower()
    strong_hits = [m for m in STRONG_BLOCK_MARKERS if m in lowered]
    if "just a moment" in title_l and "just a moment" not in strong_hits:
        strong_hits.append("just a moment")
    if "performing security verification" in lowered and "performing security verification" not in strong_hits:
        strong_hits.append("performing security verification")
    weak_hits = [m for m in WEAK_MARKERS if m in lowered]
    journey_hits = [m for m in JOURNEY_MARKERS if m in lowered]
    error_statuses = sorted({r["status"] for r in responses if r["status"] >= 400})
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

def _do_mouse_click(page: Page, x: float, y: float) -> None:
    page.mouse.move(x, y, steps=random.randint(6, 12))
    time.sleep(random.uniform(0.05, 0.15))
    page.mouse.click(x, y, delay=random.randint(40, 90))

def try_click_challenge_checkbox(page: Page, out_dir: Path, label: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "attempted": True, "delay_s": None, "clicked": False,
        "method": None, "click_coords": None, "attempts": 0,
        "frames_seen": [], "challenge_cleared": False, "error": None,
    }
    try:
        delay = random.uniform(1.0, 2.5)
        info["delay_s"] = round(delay, 3)
        print(f"[info] Challenge: initial sleep {info['delay_s']:.2f}s")
        time.sleep(delay)

        PREFER_IFRAME = 8
        MAX_ATTEMPTS = 10

        for attempt in range(1, MAX_ATTEMPTS + 1):
            info["attempts"] = attempt
            frames_this: list[str] = []
            cf_frame = None

            for frame in page.frames:
                furl = (frame.url or "").strip()
                if furl:
                    frames_this.append(furl[:130])
                if furl.startswith("https://challenges.cloudflare.com"):
                    cf_frame = frame

            info["frames_seen"] = frames_this[-8:]

            # 1. Prefer real CF iframe
            if cf_frame is not None:
                try:
                    box = cf_frame.frame_element().bounding_box()
                    if box and box.get("width", 0) >= 20 and box.get("height", 0) >= 20:
                        cx = box["x"] + box["width"] / 9.0
                        cy = box["y"] + box["height"] / 2.0
                        print(f"[info] Attempt {attempt}: CF iframe {box['width']:.0f}x{box['height']:.0f} → ({cx:.1f},{cy:.1f})")
                        _do_mouse_click(page, cx, cy)
                        info.update(clicked=True, method="iframe_bbox_offset",
                                    click_coords=[round(cx, 1), round(cy, 1)])
                        break
                except Exception as e:
                    print(f"[warn] iframe click fail (att {attempt}): {e}", file=sys.stderr)

            # 2. Outer / fallback only after preferred window
            if attempt > PREFER_IFRAME:
                for sel in (
                    "iframe[src*='challenges.cloudflare.com']",
                    ".cf-turnstile", ".cf-turnstile-wrapper", "#cf-turnstile",
                    "div[class*='turnstile']", ".main-content p + div > div > div",
                ):
                    try:
                        loc = page.locator(sel).last
                        if loc.count() == 0:
                            continue
                        box = loc.bounding_box(timeout=500)
                        if not box or box["width"] < 15:
                            continue
                        if box["width"] > 70:
                            cx = box["x"] + box["width"] / 9.0
                            cy = box["y"] + box["height"] / 2.0
                        else:
                            cx = box["x"] + 26
                            cy = box["y"] + max(18, box["height"] / 2)
                        print(f"[info] Attempt {attempt}: outer {sel!r} → ({cx:.1f},{cy:.1f})")
                        _do_mouse_click(page, cx, cy)
                        info.update(clicked=True, method=f"outer:{sel}",
                                    click_coords=[round(cx, 1), round(cy, 1)])
                        break
                    except Exception:
                        continue
                if info["clicked"]:
                    break

                # Fixed fallback
                try:
                    vp = page.viewport_size or {"width": 1366, "height": 768}
                    cx, cy = vp["width"] * 0.5 - 110, vp["height"] * 0.42
                    print(f"[info] Attempt {attempt}: fixed → ({cx:.1f},{cy:.1f})")
                    _do_mouse_click(page, cx, cy)
                    info.update(clicked=True, method="fixed_fallback",
                                click_coords=[round(cx, 1), round(cy, 1)])
                    break
                except Exception:
                    pass

            if attempt < MAX_ATTEMPTS:
                time.sleep(0.9)

        # Intermediate screenshot
        try:
            page.screenshot(path=str(out_dir / f"{label}-after-click.png"), full_page=True)
            print(f"[info] Saved {label}-after-click.png")
        except Exception as e:
            print(f"[warn] after-click shot failed: {e}", file=sys.stderr)

        if not info["clicked"]:
            print("[info] No widget found to click")
            info["method"] = "none_found"
            return info

        # Short wait for challenge to clear
        print("[info] Waiting up to 8s for challenge clear…")
        for i in range(8):
            time.sleep(1.0)
            if not still_on_challenge(page):
                info["challenge_cleared"] = True
                print(f"[info] Cleared after ~{i+1}s")
                break
        else:
            print("[info] Still on challenge after 8s")

    except Exception as exc:
        info["error"] = repr(exc)
        print(f"[warn] try_click failed: {exc}", file=sys.stderr)
    return info

def probe(context: BrowserContext, url: str, label: str, out_dir: Path) -> dict[str, Any]:
    page = context.new_page()
    responses: list[dict[str, Any]] = []

    def on_response(response: Any) -> None:
        try:
            responses.append({
                "status": response.status,
                "method": response.request.method,
                "resource_type": response.request.resource_type,
                "url": response.url,
            })
        except Exception:
            pass

    page.on("response", on_response)
    result: dict[str, Any] = {"label": label, "url": url}

    try:
        print(f"[info] Navigating {label}: {url}")
        human_delay(1.0, 2.0)
        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        result["initial_http_status"] = response.status if response else None

        human_delay(2.5, 4.0)
        human_interaction(page)
        human_delay(1.5, 2.5)

        result["challenge_click"] = try_click_challenge_checkbox(page, out_dir, label)
        human_delay(1.5, 2.5)

        result["final_url"] = page.url
        result["title"] = page.title()
        html = page.content()
        try:
            visible_text = page.locator("body").inner_text(timeout=8_000)
        except Exception:
            visible_text = ""
        result["content_length"] = len(html)
        result["visible_text_length"] = len(visible_text)
        result["visible_text_preview"] = visible_text[:4000]
        result["fingerprint"] = get_browser_fingerprint(page)
        result["response_summary"] = summarise_responses(responses)
        result["strong_marker_contexts"] = marker_contexts(visible_text, STRONG_BLOCK_MARKERS)
        result["weak_marker_contexts"] = marker_contexts(visible_text, WEAK_MARKERS)
        result["assessment"] = page_assessment(
            title=result["title"], visible_text=visible_text,
            final_url=result["final_url"], responses=responses,
        )
        (out_dir / f"{label}.html").write_text(html, encoding="utf-8")
        (out_dir / f"{label}.txt").write_text(visible_text, encoding="utf-8")
        page.screenshot(path=str(out_dir / f"{label}.png"), full_page=True)
        (out_dir / f"{label}-responses.json").write_text(
            json.dumps(responses, indent=2), encoding="utf-8"
        )
        result["ok"] = True
        print(
            f"[info] {label}: {result['assessment']['classification']}; "
            f"title={result['title']!r}; status={result['initial_http_status']}; "
            f"challenge={result['challenge_click']}"
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

def main() -> int:
    print("[info] probe starting", flush=True)
    sys.stdout.flush()
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/tmp/camoufox-probe-cc")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

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
                human_delay(3.0, 5.0)
                results.append(probe(context, DEEP_LINK, "deep-link", out_dir))
            finally:
                context.close()
    except Exception as exc:
        error = {"ok": False, "stage": "camoufox_launch", "error": repr(exc)}
        results.append(error)
        (out_dir / "launch-error.json").write_text(json.dumps(error, indent=2), encoding="utf-8")
        print(f"[error] Camoufox launch failed: {exc}", file=sys.stderr)

    summary = {"browser": "Camoufox", "results": results}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
