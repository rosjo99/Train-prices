"""Diagnostic: can Camoufox reach buy.crosscountrytrains.co.uk?

Not part of the price-check pipeline. Run only via the
probe-camoufox-crosscountry workflow_dispatch workflow. See
docs/plans/001-train-price-alert.md for why this is being re-checked:
a prior investigation found CrossCountry blocked by Cloudflare bot
management for both curl and headless Chromium.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from camoufox.sync_api import Camoufox

DEEP_LINK = (
    "https://ticket.tpexpress.co.uk/journeys-grid/OXF/PAD/2026-12-18T07:00//1//YNGx1?departNow=no"
    "&realTime=no&searchPreferences=%2C%2C%2C%2Ctrue&showAdditionalRoutes=no&showCheapest=no&tocSpecific=no"
)
HOMEPAGE = "https://www.tpexpress.co.uk/"

BLOCK_MARKERS = (
    "captcha",
    "are you a robot",
    "access denied",
    "sorry, you have been blocked",
    "attention required",
    "just a moment",
    "datadome",
)


def probe(browser, url: str, label: str, out_dir: Path) -> dict:
    page = browser.new_page()
    responses = []
    page.on(
        "response",
        lambda r: responses.append({"status": r.status, "url": r.url}),
    )

    result = {"label": label, "url": url}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(6000)
        result["final_url"] = page.url
        result["title"] = page.title()
        content = page.content()
        result["content_length"] = len(content)
        lowered = content.lower()
        result["block_markers_found"] = [m for m in BLOCK_MARKERS if m in lowered]
        (out_dir / f"{label}.html").write_text(content, encoding="utf-8")
        page.screenshot(path=str(out_dir / f"{label}.png"), full_page=True)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - diagnostic script, want the message
        result["ok"] = False
        result["error"] = str(exc)
    result["responses_sample"] = responses[:20]
    page.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/tmp/camoufox-probe")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    with Camoufox(headless=True, humanize=True, geoip=True) as browser:
        results.append(probe(browser, HOMEPAGE, "homepage", out_dir))
        results.append(probe(browser, DEEP_LINK, "deep-link", out_dir))

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
