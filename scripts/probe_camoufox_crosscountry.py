"""Clean Camoufox-only probe for CrossCountry.
No proxy, no external solver. Hard timeouts. Fast.
Success from a GitHub Actions IP is unlikely but this is the
best pure-Camoufox attempt.
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

CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies to continue",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def still_on_challenge(page: Page) -> bool:
    try:
        blob = ((page.title() or "") + " " + page.locator("body").inner_text(timeout=3000)).lower()
        return any(m in blob for m in CHALLENGE_MARKERS)
    except Exception:
        return True


def has_cf_clearance(page: Page) -> bool:
    try:
        return any(c.get("name") == "cf_clearance" for c in page.context.cookies())
    except Exception:
        return False


def try_one_smart_click(page: Page) -> dict[str, Any]:
    """Only click if a real challenges.cloudflare.com frame is present."""
    info: dict[str, Any] = {"clicked": False, "method": None, "coords": None}
    try:
        for frame in page.frames:
            url = (frame.url or "").strip()
            if not url.startswith("https://challenges.cloudflare.com"):
                continue
            el = frame.frame_element()
            box = el.bounding_box()
            if not box or box["width"] < 20:
                continue
            # Classic left-side checkbox location
            x = box["x"] + box["width"] / 9.0
            y = box["y"] + box["height"] / 2.0
            log(f"[info] CF frame found – clicking ({x:.0f}, {y:.0f})")
            page.mouse.move(x, y, steps=random.randint(8, 14))
            time.sleep(random.uniform(0.08, 0.18))
            page.mouse.click(x, y, delay=random.randint(40, 90))
            info.update(clicked=True, method="iframe_bbox", coords=[round(x), round(y)])
            time.sleep(4)
            return info
    except Exception as e:
        info["error"] = repr(e)
    return info


def probe(context: BrowserContext, url: str, label: str, out_dir: Path) -> dict[str, Any]:
    page = context.new_page()
    page.set_default_timeout(25_000)
    page.set_default_navigation_timeout(25_000)

    result: dict[str, Any] = {"label": label, "url": url}

    try:
        log(f"[info] {label}: navigating…")
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            result["http_status"] = resp.status if resp else None
            log(f"[info] {label}: goto ok, status={result['http_status']}")
        except Exception as e:
            result["http_status"] = None
            result["goto_error"] = repr(e)
            log(f"[error] {label}: goto failed – {e}")
            try:
                page.screenshot(path=str(out_dir / f"{label}-goto-fail.png"), full_page=True)
            except Exception:
                pass

        # Let any auto-solve run
        wait = random.uniform(7.0, 10.0)
        log(f"[info] {label}: waiting {wait:.1f}s for possible auto-solve")
        time.sleep(wait)

        # One smart click only if a real CF frame exists
        result["click"] = try_one_smart_click(page)

        # Final short settle
        time.sleep(3)

        result["final_url"] = page.url
        result["title"] = page.title()
        try:
            text = page.locator("body").inner_text(timeout=5_000)
        except Exception:
            text = ""
        result["text_preview"] = text[:2000]
        result["still_challenge"] = still_on_challenge(page)
        result["has_cf_clearance"] = has_cf_clearance(page)

        page.screenshot(path=str(out_dir / f"{label}.png"), full_page=True)
        (out_dir / f"{label}.txt").write_text(text, encoding="utf-8")

        result["ok"] = True
        log(
            f"[info] {label}: title={result['title']!r} "
            f"challenge={result['still_challenge']} "
            f"cf_clearance={result['has_cf_clearance']} "
            f"click={result['click']}"
        )
    except Exception as e:
        result["ok"] = False
        result["error"] = repr(e)
        log(f"[error] {label}: {e}")
        try:
            page.screenshot(path=str(out_dir / f"{label}-error.png"), full_page=True)
        except Exception:
            pass
    finally:
        page.close()
    return result


def main() -> int:
    log("[info] clean Camoufox probe starting")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/tmp/camoufox-probe-cc")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    config: dict[str, Any] = {
        "headless": not args.headed,
        "humanize": True,
        "locale": "en-GB,en-US,en",
        "os": "windows",
        "geoip": True,          # best pure-Camoufox setting we can use
    }
    log(f"[info] config={config}")

    results: list[dict[str, Any]] = []
    try:
        with Camoufox(**config) as browser:
            ctx = NewContext(
                browser,
                os="windows",
                locale="en-GB,en-US,en",
                timezone_id="Europe/London",
                viewport={"width": 1366, "height": 768},
                color_scheme="light",
                java_script_enabled=True,
            )
            try:
                results.append(probe(ctx, HOMEPAGE, "homepage", out_dir))
                time.sleep(random.uniform(2.0, 4.0))
                results.append(probe(ctx, DEEP_LINK, "deep-link", out_dir))
            finally:
                ctx.close()
    except Exception as e:
        results.append({"ok": False, "stage": "launch", "error": repr(e)})
        log(f"[error] launch failed: {e}")

    summary = {
        "browser": "Camoufox",
        "note": "Pure Camoufox on GitHub Actions IP – no proxy, no solver",
        "results": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
