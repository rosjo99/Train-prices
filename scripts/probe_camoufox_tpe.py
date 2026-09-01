"""Diagnostic: can Camoufox reach TPE pages?

This probe intentionally uses Camoufox rather than Playwright Chromium.

It records:
- final URL and title
- HTTP response status distribution
- visible body text
- page HTML
- screenshot
- likely API/XHR responses
- block/challenge markers with surrounding context
- basic browser fingerprint information
- whether the page appears to contain actual journey content

Usage:

    python scripts/probe_camoufox_tpe.py --out-dir /tmp/camoufox-probe

Optional:

    python scripts/probe_camoufox_tpe.py \
        --out-dir /tmp/camoufox-probe \
        --headed

    python scripts/probe_camoufox_tpe.py \
        --out-dir /tmp/camoufox-probe \
        --proxy "http://user:pass@host:port"
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

PROXIES = [
    # "http://user:pass@host:port",
    # "socks5://user:pass@host:port",
]

# Keep a fixed browser configuration per proxy if desired.
#
# IMPORTANT:
# Do not put a Chrome User-Agent here. Camoufox is Firefox-based and
# generates a consistent Firefox fingerprint. The key is to keep the same
# proxy -> configuration pairing rather than pretending to be Chrome.
PROXY_CONFIG_MAP: dict[str, dict[str, Any]] = {
    # "http://user:pass@host:port": {
    #     "os": "windows",
    # },
}

DEEP_LINK = (
    "https://ticket.tpexpress.co.uk/"
    "journeys-grid/OXF/PAD/2026-12-18T07:00//1//YNGx1"
    "?departNow=no&realTime=no&searchPreferences=%2C%2C%2C%2Ctrue"
    "&showAdditionalRoutes=no&showCheapest=no&tocSpecific=no"
)

HOMEPAGE = "https://www.tpexpress.co.uk/"

# These are deliberately separated from ordinary page text such as
# JavaScript source containing the word "captcha".
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

# Words that are useful evidence that the journey page actually rendered
# meaningful application content.
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
# Helpers
# ---------------------------------------------------------------------------

def human_delay(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY) -> None:
    time.sleep(random.uniform(min_s, max_s))


def random_mouse_move(page: Page, steps: int = 8) -> None:
    """Make a small amount of normal cursor movement."""
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
    """A small amount of normal interaction after navigation."""
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


def parse_proxy(proxy_url: str) -> dict[str, str]:
    """Convert a proxy URL into Playwright's proxy dictionary."""
    parsed = urlparse(proxy_url)

    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"Invalid proxy URL: {proxy_url!r}")

    if not parsed.port:
        raise ValueError(f"Proxy URL has no port: {proxy_url!r}")

    result = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
    }

    if parsed.username:
        result["username"] = parsed.username

    if parsed.password:
        result["password"] = parsed.password

    return result


def marker_contexts(text: str, markers: tuple[str, ...]) -> list[dict[str, str]]:
    """Return useful snippets around detected markers."""
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

            # Avoid making the diagnostic enormous if a script contains
            # hundreds of harmless references to "captcha".
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
    """Collect browser-visible values for the diagnostic artifact."""
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

    strong_hits = [
        marker
        for marker in STRONG_BLOCK_MARKERS
        if marker in lowered
    ]

    weak_hits = [
        marker
        for marker in WEAK_MARKERS
        if marker in lowered
    ]

    journey_hits = [
        marker
        for marker in JOURNEY_MARKERS
        if marker in lowered
    ]

    error_statuses = sorted(
        {
            response["status"]
            for response in responses
            if response["status"] >= 400
        }
    )

    # A single occurrence of "captcha" is not considered a block.
    #
    # We only classify the page as blocked when there is stronger evidence:
    # an explicit challenge/block phrase or a substantial number of HTTP
    # errors with no meaningful journey content.
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

        # Give the site's JS application time to render.
        human_delay(4.0, 7.0)

        human_interaction(page)

        # Allow any XHR/fetch calls triggered by the page to settle.
        human_delay(3.0, 6.0)

        result["final_url"] = page.url
        result["title"] = page.title()

        html = page.content()

        try:
            visible_text = page.locator("body").inner_text(timeout=10)
        except Exception:
            visible_text = ""

        result["content_length"] = len(html)
        result["visible_text_length"] = len(visible_text)
        result["visible_text_preview"] = visible_text[:5000]

        result["fingerprint"] = get_browser_fingerprint(page)

        result["response_summary"] = summarise_responses(responses)

        result["strong_marker_contexts"] = marker_contexts(
            visible_text,
            STRONG_BLOCK_MARKERS,
        )

        result["weak_marker_contexts"] = marker_contexts(
            visible_text,
            WEAK_MARKERS,
        )

        result["assessment"] = page_assessment(
            title=result["title"],
            visible_text=visible_text,
            final_url=result["final_url"],
            responses=responses,
        )

        # Save artifacts regardless of whether the page appears blocked.
        (out_dir / f"{label}.html").write_text(
            html,
            encoding="utf-8",
        )

        (out_dir / f"{label}.txt").write_text(
            visible_text,
            encoding="utf-8",
        )

        page.screenshot(
            path=str(out_dir / f"{label}.png"),
            full_page=True,
        )

        (out_dir / f"{label}-responses.json").write_text(
            json.dumps(responses, indent=2),
            encoding="utf-8",
        )

        result["ok"] = True

        print(
            f"[info] {label}: "
            f"{result['assessment']['classification']}; "
            f"title={result['title']!r}; "
            f"status={result['initial_http_status']}"
        )

    except Exception as exc:
        result["ok"] = False
        result["error"] = repr(exc)

        try:
            page.screenshot(
                path=str(out_dir / f"{label}-error.png"),
                full_page=True,
            )
        except Exception:
            pass

        print(
            f"[error] {label}: {exc}",
            file=sys.stderr,
        )

    finally:
        result["responses"] = responses
        page.close()

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--out-dir",
        default="/tmp/camoufox-probe",
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser window.",
    )

    parser.add_argument(
        "--proxy",
        help="Use one proxy instead of the configured proxy pool.",
    )

    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.proxy:
        proxy_url = args.proxy
    elif PROXIES:
        proxy_url = random.choice(PROXIES)
    else:
        proxy_url = None

    proxy_config = None
    camoufox_config: dict[str, Any] = {}

    if proxy_url:
        proxy_config = parse_proxy(proxy_url)
        camoufox_config.update(
            PROXY_CONFIG_MAP.get(proxy_url, {})
        )

    # Camoufox is Firefox-based. Let Camoufox generate a coherent
    # fingerprint rather than injecting Chrome-specific navigator/WebGL
    # patches after the fact.
    #
    # geoip=True is particularly important when a proxy is supplied:
    # Camoufox can derive location/timezone information from the proxy exit IP.
    camoufox_config.update(
        {
            "headless": not args.headed,
            "humanize": True,
            "geoip": bool(proxy_config),
            "locale": "en-GB,en-US,en",
            "os": camoufox_config.get("os", "windows"),
        }
    )

    if proxy_config:
        camoufox_config["proxy"] = proxy_config

    print(
        "[info] Browser=Camoufox "
        f"proxy={proxy_url or 'none'} "
        f"config={camoufox_config}"
    )

    results: list[dict[str, Any]] = []

    try:
        with Camoufox(**camoufox_config) as browser:
            # NewContext is Camoufox's fingerprint-aware context creator.
            # It generates a coherent browser identity instead of relying
            # on the manual webdriver/canvas/WebGL patches used previously.
            context = NewContext(
                browser,
                os=camoufox_config.get("os"),
                locale="en-GB,en-US,en",
                timezone_id="Europe/London"
                if not proxy_config
                else None,
                viewport={
                    "width": 1366,
                    "height": 768,
                },
                color_scheme="light",
                java_script_enabled=True,
            )

            try:
                results.append(
                    probe(
                        context,
                        HOMEPAGE,
                        "homepage",
                        out_dir,
                    )
                )

                human_delay(5.0, 10.0)

                results.append(
                    probe(
                        context,
                        DEEP_LINK,
                        "deep-link",
                        out_dir,
                    )
                )

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
            json.dumps(error, indent=2),
            encoding="utf-8",
        )

        print(
            f"[error] Camoufox launch failed: {exc}",
            file=sys.stderr,
        )

    summary = {
        "browser": "Camoufox",
        "proxy": proxy_url or None,
        "results": results,
    }

    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))

    # Keep the GitHub Actions step successful so that the artifacts can
    # always be inspected, even when the diagnostic itself found a block.
    return 0


if __name__ == "__main__":
    sys.exit(main())


