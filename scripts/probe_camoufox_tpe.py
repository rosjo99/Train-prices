"""Diagnostic: can a stealth Playwright browser reach TPE pages?
Much slower, proxy-aware, fixed UA per proxy, human-like interactions,
and extra fingerprint spoofing.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

# ---------------------------------------------------------------------------
# Configuration – edit these
# ---------------------------------------------------------------------------

# Add as many residential / mobile proxies as you want.
# Format: "http://user:pass@host:port" or "socks5://..."
PROXIES = [
    # "http://user:pass@1.2.3.4:8000",
    # "http://user:pass@5.6.7.8:8000",
    # ...
]

# Fixed User-Agent ↔ Proxy pairing (never rotate UA for the same proxy)
PROXY_UA_MAP: dict[str, str] = {
    # "http://user:pass@1.2.3.4:8000": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    # "http://user:pass@5.6.7.8:8000": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}

# Fallback UA when no proxy is used
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

DEEP_LINK = (
    "https://ticket.tpexpress.co.uk/journeys-grid/OXF/PAD/2026-12-18T07:00//1//YNGx1"
    "?departNow=no&realTime=no&searchPreferences=%2C%2C%2C%2Ctrue"
    "&showAdditionalRoutes=no&showCheapest=no&tocSpecific=no"
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
    "cf-browser-verification",
    "challenge-platform",
)

# How slow we want to be (seconds)
MIN_DELAY = 4.0
MAX_DELAY = 11.0
PAGE_LOAD_TIMEOUT = 60_000  # ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human_delay(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY) -> None:
    time.sleep(random.uniform(min_s, max_s))


def random_mouse_move(page: Page, steps: int = 12) -> None:
    """Move the mouse in a slightly jittery path across the viewport."""
    viewport = page.viewport_size or {"width": 1280, "height": 720}
    x, y = random.randint(100, 400), random.randint(100, 400)

    for _ in range(steps):
        x += random.randint(-80, 120)
        y += random.randint(-60, 90)
        x = max(10, min(viewport["width"] - 10, x))
        y = max(10, min(viewport["height"] - 10, y))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.04, 0.18))


def human_hover_and_scroll(page: Page) -> None:
    """A few realistic interactions before / after load."""
    random_mouse_move(page)
    human_delay(1.5, 3.5)

    # Hover somewhere in the middle
    viewport = page.viewport_size or {"width": 1280, "height": 720}
    page.mouse.move(
        viewport["width"] // 2 + random.randint(-100, 100),
        viewport["height"] // 3 + random.randint(-50, 50),
    )
    time.sleep(random.uniform(0.6, 1.8))

    # Small scroll
    page.mouse.wheel(0, random.randint(200, 600))
    human_delay(1.0, 2.5)

    random_mouse_move(page, steps=8)


def apply_stealth_and_fingerprint(context: BrowserContext, user_agent: str) -> None:
    """Inject the most common stealth + fingerprint patches."""

    # 1. Basic stealth init script (Playwright equivalent of puppeteer-stealth)
    context.add_init_script(
        """
        // Pass the Webdriver test
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // Fake plugins / mimeTypes length
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-GB', 'en-US', 'en'],
        });

        // Chrome runtime
        window.chrome = { runtime: {} };

        // Permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
        """
    )

    # 2. WebGL / Canvas / Audio fingerprint noise
    context.add_init_script(
        """
        // WebGL vendor/renderer spoof
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';           // UNMASKED_VENDOR_WEBGL
            if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
            return getParameter.call(this, parameter);
        };

        // Canvas noise
        const toBlob = HTMLCanvasElement.prototype.toBlob;
        const toDataURL = HTMLCanvasElement.prototype.toDataURL;
        const getImageData = CanvasRenderingContext2D.prototype.getImageData;

        function noise(canvas) {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const imageData = getImageData.call(ctx, 0, 0, canvas.width, canvas.height);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i]     ^= (Math.random() * 2) | 0;
                imageData.data[i + 1] ^= (Math.random() * 2) | 0;
            }
            ctx.putImageData(imageData, 0, 0);
        }

        HTMLCanvasElement.prototype.toBlob = function(...args) {
            noise(this);
            return toBlob.apply(this, args);
        };
        HTMLCanvasElement.prototype.toDataURL = function(...args) {
            noise(this);
            return toDataURL.apply(this, args);
        };

        // AudioContext fingerprint
        const originalGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function() {
            const results = originalGetChannelData.apply(this, arguments);
            for (let i = 0; i < results.length; i += 100) {
                results[i] = results[i] + Math.random() * 0.0001;
            }
            return results;
        };
        """
    )

    # 3. Force consistent User-Agent + extra headers
    context.set_extra_http_headers({
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    })


def create_context(
    browser: Browser,
    proxy: str | None = None,
    user_agent: str = DEFAULT_UA,
) -> BrowserContext:
    kwargs: dict[str, Any] = {
        "user_agent": user_agent,
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-GB",
        "timezone_id": "Europe/London",
        "geolocation": {"latitude": 51.5074, "longitude": -0.1278},  # London
        "permissions": ["geolocation"],
        "color_scheme": "light",
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "java_script_enabled": True,
    }

    if proxy:
        kwargs["proxy"] = {"server": proxy}

    context = browser.new_context(**kwargs)
    apply_stealth_and_fingerprint(context, user_agent)
    return context


# ---------------------------------------------------------------------------
# Probe logic
# ---------------------------------------------------------------------------

def probe(context: BrowserContext, url: str, label: str, out_dir: Path) -> dict:
    page = context.new_page()
    responses: list[dict] = []

    page.on("response", lambda r: responses.append({"status": r.status, "url": r.url}))

    result: dict[str, Any] = {"label": label, "url": url}

    try:
        # Slow, human-like navigation
        human_delay(2.0, 5.0)
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

        # Let the page settle + do fake interactions
        human_delay(3.0, 7.0)
        human_hover_and_scroll(page)
        human_delay(4.0, 9.0)

        result["final_url"] = page.url
        result["title"] = page.title()
        content = page.content()
        result["content_length"] = len(content)

        lowered = content.lower()
        result["block_markers_found"] = [m for m in BLOCK_MARKERS if m in lowered]

        (out_dir / f"{label}.html").write_text(content, encoding="utf-8")
        page.screenshot(path=str(out_dir / f"{label}.png"), full_page=True)

        result["ok"] = True
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)

    result["responses_sample"] = responses[:25]
    page.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/tmp/stealth-tpe-probe")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser")
    parser.add_argument("--proxy", help="Force a single proxy (overrides pool)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []

    with sync_playwright() as p:
        # Launch options
        launch_kwargs = {
            "headless": not args.headed,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1366,768",
            ],
        }

        browser = p.chromium.launch(**launch_kwargs)

        # Decide which proxy / UA pair to use
        if args.proxy:
            proxy = args.proxy
            ua = PROXY_UA_MAP.get(proxy, DEFAULT_UA)
        elif PROXIES:
            proxy = random.choice(PROXIES)
            ua = PROXY_UA_MAP.get(proxy, DEFAULT_UA)
        else:
            proxy = None
            ua = DEFAULT_UA

        print(f"[info] Using proxy={proxy or 'none'}  UA={ua[:60]}...")

        context = create_context(browser, proxy=proxy, user_agent=ua)

        try:
            results.append(probe(context, HOMEPAGE, "homepage", out_dir))
            human_delay(8.0, 15.0)  # long pause between the two pages
            results.append(probe(context, DEEP_LINK, "deep-link", out_dir))
        finally:
            context.close()
            browser.close()

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
