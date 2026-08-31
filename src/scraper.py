"""Playwright-driven scraper for National Rail Enquiries' journey planner.

Playwright is deliberately **not** imported at module level. Importing this
module must never require the Playwright browsers to be installed (they're
a ~300MB download only needed at scrape time, e.g. in CI or when actually
running a check) — `import src.scraper` must succeed in any environment,
including one with no browsers installed at all, so unit tests for the rest
of the codebase never need a browser. Every `from playwright.sync_api import
...` lives inside the functions that actually launch a browser.

This module receives no secrets and must never be given any — nothing here
should ever end up logging an API key or similar. It navigates to a public
results page; nothing it does is authenticated.

Terminology: "attempt" = one browser launch + navigation + wait cycle for a
single travel date. `fetch_journey_search` retries failed attempts with
backoff (see RETRY_BACKOFF_SECONDS), each attempt getting a brand new
browser context.

Why National Rail Enquiries and not Trainline: Trainline sits behind
DataDome bot protection (confirmed blocked on GitHub-hosted runners, see
CLAUDE.md's Tech decisions and docs/plans/001-train-price-alert.md §1.1).
NRE has no bot protection at all (18+ probe runs, zero CAPTCHA/block
markers). NRE's journey-planner page also loads third-party ad/tracking
scripts, one of which was observed — only during *interactive* UI-driven
probing (filling the form, clicking through), never via the deep-link
approach this module actually uses — redirecting the whole tab to a
Booking.com hotel search. This module never drives that UI: it navigates
straight to a fully-parameterised deep-link URL (see
config.JOURNEY_PLANNER_URL_TEMPLATE) and reads the same-origin XHR the
page itself makes to config.JOURNEY_PLANNER_API_HOST — no form-filling, no
button clicks, and confirmed across every deep-link probe run to load
straight into real results with no redirect. The iframe/navigation guards
below are kept anyway as cheap defense in depth for an unattended daily
job, since the ad ecosystem on the page is outside our control.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from src import config

logger = logging.getLogger(__name__)

# A current desktop Chrome UA string. Update occasionally.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Overall per-attempt budget: how long we'll wait, after navigation, for
# either the journey-planner XHR to fire or the results DOM to render.
# Monkeypatch this module attribute in tests instead of actually waiting.
PAGE_BUDGET_SECONDS: float = 45.0

# How long to pause the polling loop between checks, in milliseconds,
# passed to Page.wait_for_timeout (a no-op wait in fake pages used by
# tests, so this doesn't slow tests down).
POLL_INTERVAL_MS = 500

# Retry backoff, in seconds, indexed by (attempt number - 1), clamped to
# the last entry for any further attempts.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (30, 90)

# Defense-in-depth block markers, checked against page URL/content. NRE
# itself has never shown any of these (confirmed "none" on every probe
# run) — this exists in case that ever changes, not because it's expected.
BLOCK_MARKERS: tuple[str, ...] = (
    "captcha",
    "are you a robot",
    "access denied",
    "datadome",
    "cloudflare-challenge",
)

# UNVERIFIED HYPOTHESIS: a CSS selector for the results list, used only as
# a fallback when the journey-planner XHR never fires. Not confirmed
# against the real site — every probe run captured the XHR successfully,
# so this fallback has never actually been exercised. If it never matches
# in practice, the fallback simply never fires and callers fall through to
# TimeoutScrapeError, which is the safe failure mode.
RESULTS_DOM_SELECTOR = "[id^='outward-journey-']"

# Best-effort cookie-banner selectors, tried in order. Confirmed present
# and dismissable via the first selector across every probe run. None of
# these are fatal if absent or if clicking fails — see
# _dismiss_cookie_banner.
COOKIE_BANNER_SELECTORS: tuple[str, ...] = (
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
)


class ScraperError(Exception):
    """Base class for scraper failures."""


class BlockedError(ScraperError):
    """Raised when a bot-protection system blocks us (see BLOCK_MARKERS)."""


class HijackedError(ScraperError):
    """Raised when the page navigates away from nationalrail.co.uk.

    Distinct from BlockedError: this isn't NRE blocking us, it's a
    third-party ad/tracking script on the page redirecting the tab
    elsewhere (observed during interactive UI probing — see this module's
    docstring). Never observed via the deep-link approach used here, but
    guarded against regardless.
    """


class TimeoutScrapeError(ScraperError):
    """Raised when no usable data (XHR or DOM) appears within the page budget."""


def _ensure_logging_configured() -> None:
    """Configure a stdout, timestamped logger if nothing has already.

    Idempotent: if the process (e.g. src.main, or a test) already called
    logging.basicConfig, this is a no-op so we don't duplicate handlers.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


def _build_journey_planner_url(travel_date: date) -> str:
    """Build the deep-linked journey-planner URL for `travel_date`.

    Anchored at the earliest of config.TARGET_DEPARTURES: NRE's results
    page lists journeys departing from that time onward, so a single
    fetch at (e.g.) 07:25 also picks up the 07:30 departure — confirmed
    live, both target trains appeared in one response when anchored at
    07:25 (see docs/plans/001-train-price-alert.md Task 3).
    """
    earliest = min(config.TARGET_DEPARTURES)
    hour, minute = earliest.split(":")
    return config.build_journey_planner_url(travel_date, hour, minute)


def _looks_blocked(status: int | None, url: str | None, content: str | None) -> bool:
    """Pure block-detection logic: a non-2xx XHR status, or a block marker
    anywhere in the current page URL or content. See BLOCK_MARKERS'
    comment — this is defense in depth, not a documented NRE behaviour.
    """
    if status is not None and status >= 400:
        return True
    haystack = f"{url or ''} {content or ''}".lower()
    return any(marker in haystack for marker in BLOCK_MARKERS)


def _looks_hijacked(current_url: str) -> bool:
    """True once the page has navigated away from nationalrail.co.uk
    entirely (see HijackedError). An empty/unparsable URL is not treated
    as hijacked — that's "no navigation has happened yet", not "hijacked
    elsewhere".
    """
    if not current_url:
        return False
    host = urlparse(current_url).hostname or ""
    return not host.endswith(config.NRE_HOST_SUFFIX)


def _is_journey_planner_response(url: str) -> bool:
    """True only for the specific journey-search endpoint, not just any
    response from JOURNEY_PLANNER_API_HOST. The host also serves sibling
    endpoints (e.g. "/fare-info") for other page data — matching on host
    alone let a sibling endpoint's response overwrite the real one
    whenever it happened to arrive later in the same page load, which
    produced a response with no "outwardJourneys" key at all (observed
    live: a real run's captured "body" turned out to be the "/fare-info"
    response instead).
    """
    if config.JOURNEY_PLANNER_API_HOST not in url:
        return False
    return urlparse(url).path.rstrip("/") == config.JOURNEY_PLANNER_API_PATH


def _make_response_handler(captured: dict[str, Any]) -> Callable[[Any], None]:
    """Build a Page "response" handler that captures the journey-planner XHR.

    Stores status/url/body into `captured` in place. Never raises — a
    malformed or non-JSON response body is recorded as body=None rather
    than crashing the page event loop.
    """

    def _on_response(response: Any) -> None:
        try:
            url = response.url
        except Exception:
            return
        if not _is_journey_planner_response(url):
            return
        try:
            status = response.status
        except Exception:
            status = None
        try:
            body = response.json()
        except Exception:
            logger.warning(
                "journey-planner response body was not valid JSON (url=%s)", url
            )
            body = None
        captured["status"] = status
        captured["url"] = url
        captured["body"] = body

    return _on_response


def _make_route_handler() -> Callable[[Any], None]:
    """Build a Playwright route handler that blocks cross-origin iframe
    documents and backstops any main-frame navigation away from NRE.

    Defense in depth against the ad-hijack behaviour described in this
    module's docstring — never observed via the deep-link approach this
    scraper uses, but the ad ecosystem on the page is outside our control
    and this costs nothing on the happy path. Must never raise: an
    unhandled exception here (e.g. from a Service Worker request, which
    has no associated frame) would corrupt request handling for the whole
    browser session, not just this one request.
    """

    def _route_handler(route: Any) -> None:
        try:
            request = route.request
            try:
                frame = request.frame
            except Exception:
                frame = None

            is_subframe_doc = (
                request.resource_type == "document"
                and frame is not None
                and frame.parent_frame is not None
            )
            if is_subframe_doc:
                sub_host = urlparse(request.url).hostname or ""
                if not sub_host.endswith(config.NRE_HOST_SUFFIX):
                    route.abort()
                    return

            is_main_frame_nav = (
                request.is_navigation_request()
                and frame is not None
                and frame.parent_frame is None
            )
            if is_main_frame_nav:
                host = urlparse(request.url).hostname or ""
                if not host.endswith(config.NRE_HOST_SUFFIX):
                    logger.warning(
                        "blocking main-frame navigation away from NRE: %s",
                        request.url,
                    )
                    route.fulfill(status=200, content_type="text/html", body="<html></html>")
                    return
        except Exception:
            logger.debug("route handler error; falling through", exc_info=True)
        route.continue_()

    return _route_handler


def _dismiss_cookie_banner(page: Any) -> None:
    """Best-effort cookie-banner dismissal. Never fatal."""
    for selector in COOKIE_BANNER_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                locator.first.click(timeout=3000)
                logger.info("dismissed cookie banner (selector=%s)", selector)
                return
        except Exception:
            continue
    logger.info("no cookie banner dismissed (absent, or all selectors failed)")


def _read_results_dom(page: Any) -> dict[str, Any] | None:
    """Fallback for when the journey-planner XHR never fires.

    Returns None when nothing is found, so the caller can raise
    TimeoutScrapeError rather than pretend this succeeded. See
    RESULTS_DOM_SELECTOR's comment: unverified, since every probe run
    successfully captured the XHR and never needed this path.
    """
    try:
        element = page.query_selector(RESULTS_DOM_SELECTOR)
    except Exception:
        logger.warning("DOM fallback selector query failed", exc_info=True)
        return None
    if element is None:
        return None
    try:
        html = element.inner_html()
    except Exception:
        logger.warning("DOM fallback element had no readable inner_html", exc_info=True)
        return None
    if not html:
        return None
    return {"_source": "dom-fallback", "_raw_html": html}


def _current_page_url(page: Any) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _current_page_content(page: Any) -> str:
    try:
        return page.content() or ""
    except Exception:
        return ""


def _wait_for_result(
    page: Any, captured: dict[str, Any], travel_date: date
) -> dict[str, Any]:
    """Poll until the journey-planner XHR lands, a block/hijack is
    detected, or the page budget runs out; falls back to the DOM; raises
    TimeoutScrapeError if nothing usable ever appears.
    """
    deadline = time.monotonic() + PAGE_BUDGET_SECONDS
    while True:
        current_url = _current_page_url(page)
        if _looks_hijacked(current_url):
            raise HijackedError(
                f"navigated away from nationalrail.co.uk to {current_url!r} "
                f"while loading results for {travel_date.isoformat()}"
            )
        if _looks_blocked(
            captured.get("status"), current_url, _current_page_content(page)
        ):
            raise BlockedError(
                f"blocked while loading results for {travel_date.isoformat()}"
            )
        if "body" in captured:
            break
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(POLL_INTERVAL_MS)

    if "body" in captured:
        if captured.get("status") is not None and captured["status"] >= 400:
            raise BlockedError(
                f"journey-planner XHR returned HTTP {captured['status']}"
            )
        body = captured["body"]
        if body is not None:
            return body
        logger.warning(
            "journey-planner XHR captured but body was not JSON; trying DOM fallback"
        )

    dom_result = _read_results_dom(page)
    if dom_result is not None:
        logger.info("journey-planner XHR never fired; used DOM fallback")
        return dom_result

    raise TimeoutScrapeError(
        f"no journey-planner response and no DOM results within "
        f"{PAGE_BUDGET_SECONDS}s for {travel_date.isoformat()}"
    )


def _write_failure_artifacts(
    page: Any,
    artifacts_dir: Path | None,
    travel_date: date,
    captured: dict[str, Any],
) -> None:
    """Best-effort dump of debug artifacts on a failed attempt. Never
    raises — a failure to write artifacts must not mask the original
    scraper exception.
    """
    if artifacts_dir is None:
        return
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.warning("could not create artifacts dir %s", artifacts_dir, exc_info=True)
        return

    date_str = travel_date.isoformat()

    if page is not None:
        screenshot_path = artifacts_dir / f"screenshot-{date_str}.png"
        try:
            page.screenshot(path=str(screenshot_path))
            logger.info("wrote %s", screenshot_path)
        except Exception:
            logger.warning("could not capture screenshot", exc_info=True)

        html_path = artifacts_dir / f"page-{date_str}.html"
        try:
            html_path.write_text(_current_page_content(page), encoding="utf-8")
            logger.info("wrote %s", html_path)
        except Exception:
            logger.warning("could not capture page HTML", exc_info=True)

    if captured:
        response_path = artifacts_dir / f"response-{date_str}.json"
        try:
            response_path.write_text(
                json.dumps(captured, indent=2, default=str), encoding="utf-8"
            )
            logger.info("wrote %s", response_path)
        except Exception:
            logger.warning("could not write captured response", exc_info=True)


def _launch_browser(playwright_module: Any) -> Any:
    """Launch headless Chromium, translating a missing-binary error into a
    clear, actionable message.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        return playwright_module.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
    except PlaywrightError as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            raise ScraperError(
                "Chromium binary not found. Run `playwright install chromium` "
                f"and retry. (original error: {message})"
            ) from exc
        raise ScraperError(f"failed to launch Chromium: {message}") from exc


def _attempt_once(travel_date: date, *, artifacts_dir: Path | None) -> dict[str, Any]:
    """One full browser launch + navigate + wait cycle. Always uses a fresh
    browser and context. Raises ScraperError/BlockedError/HijackedError/
    TimeoutScrapeError on failure, writing debug artifacts first if
    artifacts_dir is set.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    url = _build_journey_planner_url(travel_date)
    captured: dict[str, Any] = {}

    with sync_playwright() as p:
        browser = None
        page = None
        try:
            browser = _launch_browser(p)

            context = browser.new_context(
                locale="en-GB",
                timezone_id="Europe/London",
                viewport={"width": 1440, "height": 900},
                user_agent=USER_AGENT,
            )
            context.route("**/*", _make_route_handler())
            page = context.new_page()
            # Registered before navigating, per plan, so we never miss the
            # journey-planner XHR racing the navigation itself.
            page.on("response", _make_response_handler(captured))

            logger.info(
                "[%s] navigating to journey-planner deep link: %s",
                travel_date.isoformat(),
                url,
            )
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_BUDGET_SECONDS * 1000,
                )
            except PlaywrightTimeoutError as exc:
                raise TimeoutScrapeError(f"navigation timed out: {exc}") from exc

            try:
                _dismiss_cookie_banner(page)
            except Exception:
                logger.warning("cookie banner handling raised; continuing", exc_info=True)

            logger.info(
                "[%s] waiting for journey-planner response or results DOM "
                "(budget=%ss)",
                travel_date.isoformat(),
                PAGE_BUDGET_SECONDS,
            )
            return _wait_for_result(page, captured, travel_date)
        except ScraperError:
            _write_failure_artifacts(page, artifacts_dir, travel_date, captured)
            raise
        except PlaywrightError as exc:
            _write_failure_artifacts(page, artifacts_dir, travel_date, captured)
            raise ScraperError(f"unexpected Playwright error: {exc}") from exc
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.debug("error closing browser", exc_info=True)


def fetch_journey_search(
    travel_date: date,
    *,
    artifacts_dir: Path | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    """Fetch the raw journey-planner JSON for `travel_date`.

    Retries on ScraperError/TimeoutScrapeError up to `attempts` times with
    backoff (RETRY_BACKOFF_SECONDS), a fresh browser context each time.
    BlockedError and HijackedError are retried at most once regardless of
    `attempts`, since hammering either makes it worse. On final failure,
    if artifacts_dir is set, debug artifacts (screenshot, page HTML,
    captured raw response) have already been written by the failing
    attempt before this re-raises.

    Never returns an empty-but-successful result: any failure to get real
    data raises rather than returning {}.
    """
    _ensure_logging_configured()
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    blocked_count = 0
    last_exc: ScraperError | None = None

    for attempt in range(1, attempts + 1):
        logger.info(
            "[%s] attempt %d/%d: starting", travel_date.isoformat(), attempt, attempts
        )
        try:
            result = _attempt_once(travel_date, artifacts_dir=artifacts_dir)
        except (BlockedError, HijackedError) as exc:
            blocked_count += 1
            last_exc = exc
            logger.error(
                "[%s] attempt %d/%d: blocked/hijacked: %s",
                travel_date.isoformat(),
                attempt,
                attempts,
                exc,
            )
            if blocked_count >= 2 or attempt >= attempts:
                logger.error(
                    "[%s] giving up after being blocked/hijacked; not retrying further",
                    travel_date.isoformat(),
                )
                raise
        except ScraperError as exc:
            last_exc = exc
            logger.warning(
                "[%s] attempt %d/%d failed: %s",
                travel_date.isoformat(),
                attempt,
                attempts,
                exc,
            )
            if attempt >= attempts:
                logger.error(
                    "[%s] all %d attempt(s) failed", travel_date.isoformat(), attempts
                )
                raise
        else:
            logger.info(
                "[%s] attempt %d/%d: succeeded",
                travel_date.isoformat(),
                attempt,
                attempts,
            )
            return result

        delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        logger.info(
            "[%s] backing off %ds before retrying", travel_date.isoformat(), delay
        )
        time.sleep(delay)

    # Unreachable: the loop above always either returns or raises on its
    # final iteration.
    assert last_exc is not None  # pragma: no cover
    raise last_exc
