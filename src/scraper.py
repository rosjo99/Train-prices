"""Camoufox-driven scraper for TransPennine Express' own booking engine
(ticket.tpexpress.co.uk journeys-grid).

Camoufox is deliberately **not** imported at module level (neither is
Playwright, which Camoufox drives under the hood). Importing this module
must never require the Camoufox browser build to be installed (it's a
sizeable download only needed at scrape time, e.g. in CI or when actually
running a check) — `import src.scraper` must succeed in any environment,
including one with no browser installed at all, so unit tests for the
rest of the codebase never need a browser. Every `from camoufox.sync_api
import ...` (and every `from playwright.sync_api import ...`) lives
inside the functions that actually launch a browser.

This module receives no secrets and must never be given any — nothing
here should ever end up logging an API key or similar. It navigates to a
public results page; nothing it does is authenticated.

Terminology: "attempt" = one browser launch + navigation + wait cycle for
a single travel date. `fetch_journey_search` retries failed attempts with
backoff (see RETRY_BACKOFF_SECONDS), each attempt getting a brand new
browser context.

Why TransPennine Express and not National Rail Enquiries (the original
retailer) or Trainline: Trainline sits behind DataDome bot protection
(confirmed blocked on GitHub-hosted runners — see CLAUDE.md's Tech
decisions and docs/plans/001-train-price-alert.md §1.1). NRE worked but
this repo migrated off it — see docs/plans/005-migrate-to-tpe.md for the
full rationale. TPE showed no bot protection at all across the probe run
and the fixture-capture run (no CAPTCHA, block page, or challenge; see
BLOCK_MARKERS below). This module never drives TPE's interactive UI: it
navigates straight to a fully-parameterised deep-link URL (see
config.JOURNEY_PLANNER_URL_TEMPLATE) and reads the same-origin POST the
page itself makes to config.JOURNEY_PLANNER_API_HOST (see
config.JOURNEY_PLANNER_API_PATH) — no form-filling, no button clicks. The
iframe/navigation guards below are kept anyway as cheap defense in depth
for an unattended daily job, since the page loads third-party scripts
(Usercentrics CMP, Google Maps, PayPal) outside our control — see
HijackedError's docstring.

Why Camoufox: CLAUDE.md mandates Camoufox for booking platforms other
than NRE, and it's what was validated live against the real TPE site (see
scripts/capture_fixture_tpe.py, the reference this module's launch shape
was copied from verbatim).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from src import config

logger = logging.getLogger(__name__)

# Timeout for page.goto itself (navigation only — not the post-navigation
# wait for results, see PAGE_BUDGET_SECONDS below). Kept separate so the
# poll budget can be tuned independently of how long we'll wait for the
# page to even finish loading.
#
# PROVISIONAL, not measured: this and PAGE_BUDGET_SECONDS below were
# tuned against NRE + headless Chromium (docs/plans/002-speed-up-price-
# check-run.md §1) and that tuning does not transfer to Camoufox/Firefox
# with humanize=True, which launches and interacts more slowly by an
# unmeasured amount. Adopted here as the starting point are the working
# values from scripts/capture_fixture_tpe.py's validated live run (a 60s
# navigation timeout, a 20s result wait) — see
# docs/plans/005-migrate-to-tpe.md §4.2 item 8. Re-tighten both from the
# first full live run's real timings (see that plan's §7.2/§9) rather
# than assuming these are already right.
NAVIGATION_TIMEOUT_SECONDS: float = 60.0

# Post-navigation poll deadline: how long we'll wait, after navigation,
# for the journey-plan POST response to arrive. Monkeypatch this module
# attribute in tests instead of actually waiting. See
# NAVIGATION_TIMEOUT_SECONDS' comment: provisional, carried over from
# scripts/capture_fixture_tpe.py's validated value, not measured against
# a full live run of this scraper yet.
PAGE_BUDGET_SECONDS: float = 20.0

# How long to pause the polling loop between checks, in milliseconds,
# passed to Page.wait_for_timeout (a no-op wait in fake pages used by
# tests, so this doesn't slow tests down).
POLL_INTERVAL_MS = 250

# Retry backoff, in seconds, indexed by (attempt number - 1), clamped to
# the last entry for any further attempts. Carried over unchanged from
# the NRE-era scraper — this is still an unattended job hitting someone
# else's site, so a real (if shorter) backoff stays rather than going to
# zero.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (5, 10)

# Defense-in-depth block markers, checked against page URL/content. TPE
# itself has shown none of these across either the diagnostic Camoufox
# probe or the real fixture-capture run — this exists in case that ever
# changes, not because it's expected.
#
# IMPORTANT: do not add a bare "captcha" substring here. TPE's own page
# bootstrap config harmlessly embeds a reCAPTCHA site key
# ("googleRecaptchaKey":"6Le4ESkTAAAAAIW-1dS_obXeJ1oOlztiaNZ31hOE",
# almost certainly for some unrelated form elsewhere on the site) on
# every single page load — this was a proven false positive in
# production, not a hypothetical: GitHub Actions run 33530583374 failed
# every travel date instantly with BlockedError before any journey-plan
# response was ever captured, and the debug artifact
# page-2026-09-08.html from that run shows the "googleRecaptchaKey"
# field present in the raw HTML from the moment domcontentloaded fires.
# A bare "captcha" marker matches "...Recaptcha..." as a substring, so
# it fired a hard block on every page load, every date, unconditionally.
# Markers here must be specific challenge/block phrases unlikely to
# appear in ordinary page config, analytics, or third-party scripts
# (mirrors scripts/probe_camoufox_tpe.py's already-validated
# STRONG_BLOCK_MARKERS list).
BLOCK_MARKERS: tuple[str, ...] = (
    "are you a robot",
    "access denied",
    "datadome",
    "cloudflare-challenge",
    "verify you are human",
    "checking your browser",
    "just a moment",
)

# Best-effort cookie-banner selectors, tried in order. TPE's Usercentrics
# banner was confirmed dismissable via the first selector here
# (`button:has-text('Accept All')`, run 33527007099 — see
# docs/plans/005-migrate-to-tpe.md §1.5); the `uc-*` variants are kept
# around it as fallbacks. None of these are fatal if absent or if
# clicking fails — see _dismiss_cookie_banner.
COOKIE_BANNER_SELECTORS: tuple[str, ...] = (
    "button:has-text('Accept All')",
    "[data-testid='uc-accept-all-button']",
    "#uc-btn-accept-banner",
    "button:has-text('Accept')",
)


class ScraperError(Exception):
    """Base class for scraper failures."""


class BlockedError(ScraperError):
    """Raised when a bot-protection system blocks us (see BLOCK_MARKERS)."""


class HijackedError(ScraperError):
    """Raised when the page navigates away from tpexpress.co.uk entirely.

    Distinct from BlockedError: this isn't TPE blocking us, it's some
    third-party script on the page (Usercentrics, Google Maps, PayPal)
    redirecting the tab elsewhere. No hijack of this kind was ever
    observed on TPE, on either the diagnostic probe or the fixture-
    capture run — this is defense in depth for an unattended job on a
    page whose third-party script ecosystem is outside our control, not a
    documented TPE behaviour.
    """


class TimeoutScrapeError(ScraperError):
    """Raised when no usable data (the journey-plan response) appears
    within the page budget.
    """


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
    """Build the deep-linked journeys-grid URL for `travel_date`.

    Anchored a few minutes before the earliest of config.TARGET_DEPARTURES
    (see config.ANCHOR_OFFSET_MINUTES's comment for why): TPE's frontend
    hardcodes a fixed "numJourneys": 3 in its own POST body, with no lever
    exposed by this deep-link to raise it, so anchoring exactly at the
    earliest target risks an earlier direct service using up a "slot"
    before it and pushing the later target out of the window (confirmed
    live — see docs/plans/005-migrate-to-tpe.md §1.4).

    Edge case: if subtracting ANCHOR_OFFSET_MINUTES from the earliest
    target would cross midnight backwards (e.g. a target departure of
    00:02 with a 5-minute offset), the anchor is clamped to 00:00 rather
    than rolling the date back onto the previous day — rolling the date
    back would silently query the wrong travel date, which is worse than
    losing a few minutes of anchor lead time. Not reachable with today's
    07:25/07:30 targets, but handled anyway.
    """
    earliest = min(config.TARGET_DEPARTURES)
    hour, minute = (int(part) for part in earliest.split(":"))
    anchor_dt = datetime.combine(travel_date, dt_time(hour, minute)) - timedelta(
        minutes=config.ANCHOR_OFFSET_MINUTES
    )
    if anchor_dt.date() < travel_date:
        anchor_hour, anchor_minute = "00", "00"
    else:
        anchor_hour = f"{anchor_dt.hour:02d}"
        anchor_minute = f"{anchor_dt.minute:02d}"
    return config.build_journey_planner_url(travel_date, anchor_hour, anchor_minute)


def _looks_blocked(status: int | None, url: str | None, content: str | None) -> bool:
    """Pure block-detection logic: a non-2xx response status, or a block
    marker anywhere in the current page URL or content. See
    BLOCK_MARKERS' comment — this is defense in depth, not a documented
    TPE behaviour.
    """
    if status is not None and status >= 400:
        return True
    haystack = f"{url or ''} {content or ''}".lower()
    return any(marker in haystack for marker in BLOCK_MARKERS)


def _looks_hijacked(current_url: str) -> bool:
    """True once the page has navigated away from tpexpress.co.uk
    entirely (see HijackedError). An empty/unparsable URL is not treated
    as hijacked — that's "no navigation has happened yet", not "hijacked
    elsewhere". config.TPE_HOST_SUFFIX deliberately covers www., ticket.
    and api. subdomains, so the page's own same-origin API calls never
    trip this guard.
    """
    if not current_url:
        return False
    host = urlparse(current_url).hostname or ""
    return not host.endswith(config.TPE_HOST_SUFFIX)


def _is_journey_planner_response(url: str) -> bool:
    """True only for the specific journey-plan endpoint, not just any
    response from JOURNEY_PLANNER_API_HOST. The host also serves sibling
    endpoints (observed live: "/jp/plusbus") for other page data —
    matching on host alone let a sibling endpoint's response overwrite
    the real one whenever it happened to arrive later in the same page
    load.
    """
    if config.JOURNEY_PLANNER_API_HOST not in url:
        return False
    return urlparse(url).path.rstrip("/") == config.JOURNEY_PLANNER_API_PATH


def _make_response_handler(captured: dict[str, Any]) -> Callable[[Any], None]:
    """Build a Page "response" handler that captures the journey-plan
    POST response.

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
                "journey-plan response body was not valid JSON (url=%s)", url
            )
            body = None
        captured["status"] = status
        captured["url"] = url
        captured["body"] = body

    return _on_response


def _make_route_handler() -> Callable[[Any], None]:
    """Build a Playwright route handler that blocks cross-origin iframe
    documents and backstops any main-frame navigation away from TPE.

    Defense in depth against the hijack behaviour described in
    HijackedError's docstring — never observed on TPE, but the ad/
    tracking ecosystem on the page is outside our control and this costs
    nothing on the happy path. Must never raise: an unhandled exception
    here (e.g. from a Service Worker request, which has no associated
    frame) would corrupt request handling for the whole browser session,
    not just this one request.
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
                if not sub_host.endswith(config.TPE_HOST_SUFFIX):
                    route.abort()
                    return

            is_main_frame_nav = (
                request.is_navigation_request()
                and frame is not None
                and frame.parent_frame is None
            )
            if is_main_frame_nav:
                host = urlparse(request.url).hostname or ""
                if not host.endswith(config.TPE_HOST_SUFFIX):
                    logger.warning(
                        "blocking main-frame navigation away from tpexpress.co.uk: %s",
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
                logger.info("dismissed cookie banner via %r", selector)
                return
        except Exception:
            continue
    logger.info("no cookie banner dismissed (absent, or all selectors failed)")


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
    """Poll until the journey-plan response lands, a block/hijack is
    detected, or the page budget runs out. Raises TimeoutScrapeError if
    nothing usable ever appears — there is no DOM fallback (see
    docs/plans/005-migrate-to-tpe.md §4.2 item 9: no TPE results selector
    has ever been captured or verified, so inventing one would be
    guessing at a schema, and a truthful TimeoutScrapeError is the safer
    failure mode than converting it into a misleading ParseError).
    """
    deadline = time.monotonic() + PAGE_BUDGET_SECONDS
    while True:
        current_url = _current_page_url(page)
        if _looks_hijacked(current_url):
            raise HijackedError(
                f"navigated away from tpexpress.co.uk to {current_url!r} "
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
                f"journey-plan response returned HTTP {captured['status']}"
            )
        body = captured["body"]
        if body is not None:
            return body
        logger.warning("journey-plan response captured but body was not JSON")

    raise TimeoutScrapeError(
        f"no journey-plan response within {PAGE_BUDGET_SECONDS}s for "
        f"{travel_date.isoformat()}"
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


def _launch_browser(camoufox_cm: Any) -> Any:
    """Enter a Camoufox context manager, translating a missing-binary
    error into a clear, actionable message.

    Camoufox is a context manager (`with Camoufox(...) as browser:`), and
    the actual browser process is launched on `__enter__`, not on
    construction — so that's the call this wraps. Camoufox's own missing-
    browser exception type is not known (not guessed at here — see
    docs/plans/005-migrate-to-tpe.md §4.2 item 3), so any exception is
    caught here and its message inspected for the usual missing-binary
    vocabulary before deciding how to re-raise.
    """
    try:
        return camoufox_cm.__enter__()
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if any(
            marker in lowered
            for marker in ("camoufox", "fetch", "not found", "executable", "no such file")
        ):
            raise ScraperError(
                "Camoufox browser not found. Run `python -m camoufox fetch` "
                f"and retry. (original error: {message})"
            ) from exc
        raise ScraperError(f"failed to launch Camoufox: {message}") from exc


def _attempt_once(travel_date: date, *, artifacts_dir: Path | None) -> dict[str, Any]:
    """One full browser launch + navigate + wait cycle. Always uses a
    fresh browser and context. Raises ScraperError/BlockedError/
    HijackedError/TimeoutScrapeError on failure, writing debug artifacts
    first if artifacts_dir is set.
    """
    from camoufox.sync_api import Camoufox, NewContext
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    url = _build_journey_planner_url(travel_date)
    captured: dict[str, Any] = {}

    # _attempt_once owns the Camoufox context manager itself (per plan);
    # _launch_browser only wraps the __enter__() call, so a launch
    # failure still gets translated into an actionable ScraperError.
    camoufox_cm = Camoufox(headless=True, humanize=True, locale="en-GB")
    browser = _launch_browser(camoufox_cm)
    page = None
    context = None
    try:
        try:
            context = NewContext(
                browser,
                locale="en-GB",
                timezone_id="Europe/London",
                viewport={"width": 1366, "height": 768},
            )
            context.route("**/*", _make_route_handler())
            page = context.new_page()
            # Registered before navigating, per plan, so we never miss the
            # journey-plan response racing the navigation itself.
            page.on("response", _make_response_handler(captured))

            logger.info(
                "[%s] navigating to journeys-grid deep link: %s",
                travel_date.isoformat(),
                url,
            )
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATION_TIMEOUT_SECONDS * 1000,
                )
            except PlaywrightTimeoutError as exc:
                raise TimeoutScrapeError(f"navigation timed out: {exc}") from exc

            try:
                _dismiss_cookie_banner(page)
            except Exception:
                logger.warning("cookie banner handling raised; continuing", exc_info=True)

            logger.info(
                "[%s] waiting for journey-plan response (budget=%ss)",
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
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.debug("error closing context", exc_info=True)
    finally:
        camoufox_cm.__exit__(None, None, None)


def fetch_journey_search(
    travel_date: date,
    *,
    artifacts_dir: Path | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    """Fetch the raw journey-plan JSON for `travel_date`.

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
