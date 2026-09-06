"""Minimal fast probe – no long click loops.
Just load, wait briefly, screenshot, report.
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

def probe(context: BrowserContext, url: str, label: str, out_dir: Path) -> dict[str, Any]:
    page = context.new_page()
    page.set_default_timeout(20_000)
    page.set_default_navigation_timeout(20_000)
    result: dict[str, Any] = {"label": label, "url": url}

    try:
        print(f"[info] {label}: goto …", flush=True)
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            result["status"] = resp.status if resp else None
        except Exception as e:
            result["status"] = None
            result["goto_error"] = repr(e)
            print(f"[error] {label} goto: {e}", flush=True)

        # Short fixed wait – let any auto-solve finish
        time.sleep(random.uniform(6.0, 9.0))

        result["final_url"] = page.url
        result["title"] = page.title()
        try:
            text = page.locator("body").inner_text(timeout=5_000)
        except Exception:
            text = ""
        result["text_preview"] = text[:1500]
        result["still_challenge"] = any(
            m in (result["title"] + text).lower()
            for m in ("just a moment", "performing security verification",
                      "verify you are human", "checking your browser")
        )

        # Cookies (cf_clearance is the key signal)
        try:
            cookies = page.context.cookies()
            result["cookies"] = [
                {"name": c["name"], "domain": c.get("domain"), "value": c["value"][:40]}
                for c in cookies
            ]
            result["has_cf_clearance"] = any(c["name"] == "cf_clearance" for c in cookies)
        except Exception:
            result["cookies"] = []
            result["has_cf_clearance"] = False

        page.screenshot(path=str(out_dir / f"{label}.png"), full_page=True)
        (out_dir / f"{label}.txt").write_text(text, encoding="utf-8")
        result["ok"] = True
        print(
            f"[info] {label}: title={result['title']!r} "
            f"challenge={result['still_challenge']} "
            f"cf_clearance={result['has_cf_clearance']}",
            flush=True,
        )
    except Exception as e:
        result["ok"] = False
        result["error"] = repr(e)
        print(f"[error] {label}: {e}", flush=True)
    finally:
        page.close()
    return result

def main() -> int:
    print("[info] minimal probe starting", flush=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/tmp/camoufox-probe-cc")
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "headless": True,
        "humanize": True,
        "locale": "en-GB,en-US,en",
        "os": "windows",
    }
    print(f"[info] config={config}", flush=True)
    results = []

    try:
        with Camoufox(**config) as browser:
            ctx = NewContext(
                browser,
                os="windows",
                locale="en-GB,en-US,en",
                timezone_id="Europe/London",
                viewport={"width": 1366, "height": 768},
                color_scheme="light",
            )
            try:
                results.append(probe(ctx, HOMEPAGE, "homepage", out_dir))
                time.sleep(2)
                results.append(probe(ctx, DEEP_LINK, "deep-link", out_dir))
            finally:
                ctx.close()
    except Exception as e:
        results.append({"ok": False, "stage": "launch", "error": repr(e)})
        print(f"[error] launch: {e}", flush=True)

    summary = {"browser": "Camoufox", "results": results}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
