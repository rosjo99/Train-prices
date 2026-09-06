"""Diagnostic: can Camoufox reach CrossCountry's booking site?
Faster + more resilient version with hard navigation timeouts and
aggressive interstitial click attempts.
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
    "performing security verification",
)
WEAK_MARKERS = ("captcha", "datadome")
JOURNEY_MARKERS = (
    "oxford", "paddington", "depart", "arrive", "journey",
    "ticket", "adult", "return", "single", "£",
)

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
        print(f"[warn] Interaction failed: {exc}", file=sys.stderr, flush=True)

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
            for t in ("/api/", "graphql", "journey", "search",
                      "availability", "booking", "fare", "ajax")
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
            "performing security verification",
            "verify you are human",
            "checking your browser",
            "just a moment",
            "enable javascript and cookies to continue",
        ))
    except Exception:
        return True

def page_assessment(
    *,
    title: str,
    visible_text: str,
    final_url: str,
    responses: list[dict[str, Any]],
) -> dict[str, Any]:
    lowered = visible_text.lower()
    title_l = (title or "").lower()
    strong_hits = [m for m in STRONG_BLOCK_MARKERS if m in lowered]
    if "just a moment" in title_l and "just a moment" not in strong_hits:
        strong_hits.append("just a moment")
    if ("performing security verification" in lowered
            and "performing security verification" not in strong_hits):
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

def try_click_challenge_checkbox(
    page: Page, out_dir: Path, label: str
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "attempted": True,
        "delay_s": None,
        "clicked": False,
        "method": None,
        "click_coords": None,
        "attempts": 0,
        "frames_seen": [],
        "iframe_srcs": [],
        "challenge_cleared": False,
        "error": None,
    }
    try:
        delay = random.uniform(3.0, 5.0)
        info["delay_s"] = round(delay, 3)
        print(
            f"[info] Challenge: initial sleep {info['delay_s']:.2f}s "
            "(waiting for widget)",
            flush=True,
        )
        time.sleep(delay)

        try:
            page.screenshot(
                path=str(out_dir / f"{label}-before-click.png"), full_page=True
            )
            print(f"[info] Saved {label}-before-click.png", flush=True)
        except Exception as e:
            print(f"[warn] before-click shot failed: {e}", file=sys.stderr, flush=True)

        MAX_ATTEMPTS = 12
        clicked = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            info["attempts"] = attempt
            frames_this: list[str] = []
            iframe_srcs: list[str] = []
            cf_frame = None

            for frame in page.frames:
                furl = (frame.url or "").strip()
                if furl:
                    frames_this.append(furl[:140])
                if furl.startswith("https://challenges.cloudflare.com"):
                    cf_frame = frame

            try:
                iframe_srcs = page.evaluate("""
                    () => Array.from(document.querySelectorAll('iframe'))
                           .map(i => i.src || i.getAttribute('src') || '')
                           .filter(Boolean)
                """) or []
            except Exception:
                iframe_srcs = []

            info["frames_seen"] = frames_this[-10:]
            info["iframe_srcs"] = [s[:120] for s in iframe_srcs][:8]

            if iframe_srcs:
                print(
                    f"[info] Attempt {attempt}: iframe srcs = {iframe_srcs[:3]}",
                    flush=True,
                )

            # 1. Real CF frame
            if cf_frame is not None:
                try:
                    box = cf_frame.frame_element().bounding_box()
                    if box and box.get("width", 0) >= 20 and box.get("height", 0) >= 20:
                        for frac in (1 / 9, 0.15, 0.22, 0.30):
                            cx = box["x"] + box["width"] * frac
                            cy = box["y"] + box["height"] / 2.0
                            print(
                                f"[info] Attempt {attempt}: CF iframe "
                                f"frac={frac:.2f} → ({cx:.1f},{cy:.1f})",
                                flush=True,
                            )
                            _do_mouse_click(page, cx, cy)
                            time.sleep(0.8)
                            if not still_on_challenge(page):
                                info.update(
                                    clicked=True,
                                    method="iframe_bbox_offset",
                                    click_coords=[round(cx, 1), round(cy, 1)],
                                    challenge_cleared=True,
                                )
                                clicked = True
                                break
                        if clicked:
                            break
                except Exception as e:
                    print(
                        f"[warn] iframe click fail: {e}",
                        file=sys.stderr,
                        flush=True,
                    )

            # 2. Outer containers + multiple offsets
            outer_selectors = [
                "iframe[src*='challenges.cloudflare.com']",
                ".cf-turnstile",
                ".cf-turnstile-wrapper",
                "#cf-turnstile",
                "div[class*='turnstile']",
                ".main-content p + div > div > div",
                ".main-content div",
            ]
            for sel in outer_selectors:
                try:
                    loc = page.locator(sel).last
                    if loc.count() == 0:
                        continue
                    box = loc.bounding_box(timeout=600)
                    if not box or box["width"] < 10:
                        continue

                    candidates = [
                        (box["x"] + 26, box["y"] + 25),
                        (box["x"] + 30, box["y"] + box["height"] / 2),
                        (box["x"] + box["width"] * 0.12, box["y"] + box["height"] * 0.5),
                        (box["x"] + 40, box["y"] + 28),
                        (box["x"] + 22, box["y"] + 22),
                    ]
                    for cx, cy in candidates:
                        print(
                            f"[info] Attempt {attempt}: outer {sel!r} "
                            f"→ ({cx:.1f},{cy:.1f})",
                            flush=True,
                        )
                        _do_mouse_click(page, cx, cy)
                        time.sleep(0.7)
                        if not still_on_challenge(page):
                            info.update(
                                clicked=True,
                                method=f"outer:{sel}",
                                click_coords=[round(cx, 1), round(cy, 1)],
                                challenge_cleared=True,
                            )
                            clicked = True
                            break
                    if clicked:
                        break
                except Exception:
                    continue
            if clicked:
                break

            # 3. Fixed positions
            vp = page.viewport_size or {"width": 1366, "height": 768}
            fixed_points = [
                (vp["width"] * 0.5 - 120, vp["height"] * 0.42),
                (210, 290),
                (vp["width"] * 0.5 - 90, vp["height"] * 0.38),
                (300, 320),
                (334, 338),
            ]
            for cx, cy in fixed_points:
                print(
                    f"[info] Attempt {attempt}: fixed → ({cx:.1f},{cy:.1f})",
                    flush=True,
                )
                _do_mouse_click(page, cx, cy)
                time.sleep(0.7)
                if not still_on_challenge(page):
                    info.update(
                        clicked=True,
                        method="fixed",
                        click_coords=[round(cx, 1), round(cy, 1)],
                        challenge_cleared=True,
                    )
                    clicked = True
                    break
            if clicked:
                break

            time.sleep(1.0)

        try:
            page.screenshot(
                path=str(out_dir / f"{label}-after-click.png"), full_page=True
            )
            print(f"[info] Saved {label}-after-click.png", flush=True)
        except Exception as e:
            print(f"[warn] after-click shot failed: {e}", file=sys.stderr, flush=True)

        if not info["clicked"]:
            info["method"] = "none_found"
            print("[info] No successful click that cleared the challenge", flush=True)

        if info["clicked"] and not info.get("challenge_cleared"):
            print("[info] Extra 5s wait after click…", flush=True)
            for _ in range(5):
                time.sleep(1)
                if not still_on_challenge(page):
                    info["challenge_cleared"] = True
                    print("[info] Cleared on extra wait", flush=True)
                    break

    except Exception as exc:
        info["error"] = repr(exc)
        print(f"[warn] try_click failed: {exc}", file=sys.stderr, flush=True)
    return info

def probe(
    context: BrowserContext,
    url: str,
    label: str,
    out_dir: Path,
) -> dict[str, Any]:
    page = context.new_page()
    page.set_default_timeout(30_000)
    page.set_default_navigation_timeout(30_000)

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
        print(f"[info] Navigating {label}: {url}", flush=True)
        human_delay(1.0, 2.0)

        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            result["initial_http_status"] = response.status if response else None
            print(
                f"[info] {label} goto finished, "
                f"status={result['initial_http_status']}",
                flush=True,
            )
        except Exception as goto_exc:
            print(
                f"[error] {label} goto failed/timed out: {goto_exc}",
                flush=True,
            )
            result["initial_http_status"] = None
            result["goto_error"] = repr(goto_exc)
            try:
                page.screenshot(
                    path=str(out_dir / f"{label}-goto-timeout.png"), full_page=True
                )
            except Exception:
                pass

        human_delay(2.0, 3.5)
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
            f"[info] {label}: {result['assessment']['classification']}; "
            f"title={result['title']!r}; "
            f"status={result.get('initial_http_status')}; "
            f"challenge={result['challenge_click']}",
            flush=True,
        )
    except Exception as exc:
        result["ok"] = False
        result["error"] = repr(exc)
        try:
            page.screenshot(
                path=str(out_dir / f"{label}-error.png"), full_page=True
            )
        except Exception:
            pass
        print(f"[error] {label}: {exc}", file=sys.stderr, flush=True)
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
    print(f"[info] Browser=Camoufox config={camoufox_config}", flush=True)
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
        error = {
            "ok": False,
            "stage": "camoufox_launch",
            "error": repr(exc),
        }
        results.append(error)
        (out_dir / "launch-error.json").write_text(
            json.dumps(error, indent=2), encoding="utf-8"
        )
        print(f"[error] Camoufox launch failed: {exc}", file=sys.stderr, flush=True)

    summary = {"browser": "Camoufox", "results": results}
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
