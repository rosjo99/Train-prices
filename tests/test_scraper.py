"""Tests for src.scraper.

No test in this file ever launches a real browser or makes a real network
call: Playwright's sync_playwright() factory is always monkeypatched to a
set of fake objects (FakePlaywright/FakeBrowser/FakeContext/FakePage/
FakeResponse below) that mimic just enough of the sync API surface for
src.scraper to drive. See docs/plans/001-train-price-alert.md Task 3 for
the acceptance criteria these tests transcribe.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src import scraper

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fakes: a minimal stand-in for the bits of Playwright's sync API that
# src.scraper touches.
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, url: str, status: int = 200, body: Any = None, bad_json: bool = False):
        self.url = url
        self.status = status
        self._body = body
        self._bad_json = bad_json

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("response body is not valid JSON")
        return self._body


class FakeLocator:
    def __init__(self, present: bool):
        self.present = present
        self.clicked = False

    def count(self) -> int:
        return 1 if self.present else 0

    @property
    def first(self) -> "FakeLocator":
        return self

    def click(self, timeout: int | None = None) -> None:
        self.clicked = True


class FakeElement:
    def __init__(self, html: str):
        self._html = html

    def inner_html(self) -> str:
        return self._html


class FakePage:
    def __init__(
        self,
        *,
        responses: list[FakeResponse] | None = None,
        banner_present: bool = False,
        dom_html: str | None = None,
        url: str = "https://www.thetrainline.com/book/results",
        content: str = "<html>ok</html>",
        goto_raises: Exception | None = None,
    ):
        self._handlers: list[Any] = []
        self._responses = responses or []
        self.banner_present = banner_present
        self.dom_html = dom_html
        self.url = url
        self._content = content
        self.goto_raises = goto_raises
        self.goto_calls: list[tuple[str, str | None, float | None]] = []
        self.screenshot_calls: list[str] = []
        self.closed = False
        self.cookie_locator = FakeLocator(present=banner_present)

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self._handlers.append(handler)

    def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        if self.goto_raises is not None:
            raise self.goto_raises
        for response in self._responses:
            for handler in self._handlers:
                handler(response)

    def wait_for_timeout(self, ms: int) -> None:
        # No real delay: keeps the polling loop in _wait_for_result from
        # actually costing wall-clock time in tests.
        pass

    def locator(self, selector: str) -> FakeLocator:
        # Only the first (accept) selector "matches" a present banner; the
        # rest behave as absent, matching real Playwright's "not found".
        if selector == scraper.COOKIE_BANNER_SELECTORS[0]:
            return self.cookie_locator
        return FakeLocator(present=False)

    def query_selector(self, selector: str) -> FakeElement | None:
        if self.dom_html is None:
            return None
        return FakeElement(self.dom_html)

    def content(self) -> str:
        return self._content

    def screenshot(self, path: str | None = None) -> None:
        self.screenshot_calls.append(path)
        if path:
            Path(path).write_bytes(b"fake-png-bytes")


class FakeContext:
    def __init__(self, page: FakePage):
        self._page = page

    def new_page(self) -> FakePage:
        return self._page


class FakeBrowser:
    def __init__(self, page: FakePage):
        self._page = page
        self.contexts_created = 0
        self.closed = False

    def new_context(self, **kwargs: Any) -> FakeContext:
        self.contexts_created += 1
        return FakeContext(self._page)

    def close(self) -> None:
        self.closed = True


class ScenarioChromium:
    """A chromium launcher that hands out one page per launch() call, in
    order, so a test can script what happens attempt-by-attempt.
    """

    def __init__(self, pages: list[FakePage] | None = None, launch_raises: Exception | None = None):
        self._pages = list(pages or [])
        self.launch_raises = launch_raises
        self.launch_calls = 0
        self.browsers: list[FakeBrowser] = []

    def launch(self, **kwargs: Any) -> FakeBrowser:
        self.launch_calls += 1
        if self.launch_raises is not None:
            raise self.launch_raises
        page = self._pages[self.launch_calls - 1]
        browser = FakeBrowser(page)
        self.browsers.append(browser)
        return browser


class FakePlaywright:
    def __init__(self, chromium: ScenarioChromium):
        self.chromium = chromium

    def __enter__(self) -> "FakePlaywright":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False


def install_fake_playwright(monkeypatch: pytest.MonkeyPatch, chromium: ScenarioChromium) -> None:
    """Monkeypatch playwright.sync_api.sync_playwright to hand out
    FakePlaywright(chromium) every time it's called (once per attempt).
    """
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: FakePlaywright(chromium)
    )


@pytest.fixture(autouse=True)
def _set_route_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override config with fixed test URNs, independent of whatever real
    values config.ORIGIN_URN/DESTINATION_URN currently hold, so these tests
    don't depend on (or break when someone updates) the real route config.
    """
    monkeypatch.setattr(scraper.config, "ORIGIN_URN", "urn:trainline:generic:loc:1234")
    monkeypatch.setattr(scraper.config, "DESTINATION_URN", "urn:trainline:generic:loc:5678")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record calls to time.sleep and never actually sleep."""
    calls: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(scraper.time, "sleep", _fake_sleep)
    return calls


@pytest.fixture(autouse=True)
def _small_page_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the page budget so any test that never gets a captured
    response or DOM result doesn't spend real wall-clock time in the
    polling loop.
    """
    monkeypatch.setattr(scraper, "PAGE_BUDGET_SECONDS", 0.05)


# ---------------------------------------------------------------------------
# Import cleanliness
# ---------------------------------------------------------------------------


def test_no_top_level_playwright_import():
    """Playwright must only be imported lazily, inside functions, so that
    `import src.scraper` never requires Playwright browsers to be present.
    """
    source = (REPO_ROOT / "src" / "scraper.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("playwright"), (
                    "found module-level `import playwright...`"
                )
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith("playwright"), (
                "found module-level `from playwright... import ...`"
            )


def test_import_succeeds_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", "import src.scraper"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_results_url_contains_expected_pieces():
    url = scraper._build_results_url(date(2026, 9, 8))
    assert "urn:trainline:generic:loc:1234" in url
    assert "urn:trainline:generic:loc:5678" in url
    assert "2026-09-08T07:00:00" in url
    assert "railcards[]=" in url


@pytest.mark.parametrize(
    "status,url,content,expected",
    [
        (403, "https://www.thetrainline.com/book/results", "ok", True),
        (200, "https://geo.captcha-delivery.com/captcha/?x=1", "ok", True),
        (200, "https://www.thetrainline.com/book/results", "please solve this captcha-delivery challenge", True),
        (200, "https://www.thetrainline.com/book/results", "ok", False),
        (None, "https://www.thetrainline.com/book/results", "ok", False),
    ],
)
def test_looks_blocked(status, url, content, expected):
    assert scraper._looks_blocked(status, url, content) is expected


# ---------------------------------------------------------------------------
# Successful end-to-end flow (fake Playwright)
# ---------------------------------------------------------------------------


def _journey_search_response(body: dict[str, Any]) -> FakeResponse:
    return FakeResponse(
        url="https://www.thetrainline.com/api/journey-search/",
        status=200,
        body=body,
    )


def test_successful_fetch_returns_captured_json_body(monkeypatch):
    body = {"journeys": [{"departure": "07:25"}]}
    page = FakePage(responses=[_journey_search_response(body)])
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert result == body
    assert chromium.launch_calls == 1


@pytest.mark.parametrize("banner_present", [True, False])
def test_cookie_banner_present_or_absent_both_succeed(monkeypatch, banner_present):
    body = {"journeys": []}
    page = FakePage(responses=[_journey_search_response(body)], banner_present=banner_present)
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert result == body
    assert page.cookie_locator.clicked is banner_present


# ---------------------------------------------------------------------------
# DOM fallback
# ---------------------------------------------------------------------------


def test_dom_fallback_used_when_xhr_never_fires(monkeypatch):
    page = FakePage(responses=[], dom_html="<div>some journeys</div>")
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert result == {"_source": "dom-fallback", "_raw_html": "<div>some journeys</div>"}


def test_no_xhr_and_no_dom_raises_timeout_not_empty_success(monkeypatch):
    page = FakePage(responses=[], dom_html=None)
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


def test_blocked_via_403_status_raises_blocked_error(monkeypatch):
    body_response = FakeResponse(
        url="https://www.thetrainline.com/api/journey-search/", status=403, body=None
    )
    page = FakePage(responses=[body_response])
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.BlockedError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


def test_blocked_via_captcha_content_raises_blocked_error(monkeypatch):
    page = FakePage(responses=[], content="oops, a captcha-delivery interstitial")
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.BlockedError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


def test_blocked_error_retried_at_most_once(monkeypatch, _no_real_sleep):
    blocked_response = FakeResponse(
        url="https://www.thetrainline.com/api/journey-search/", status=403, body=None
    )
    pages = [FakePage(responses=[blocked_response]) for _ in range(3)]
    chromium = ScenarioChromium(pages=pages)
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.BlockedError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=3)

    # Blocked twice (initial + one retry), never a third time even though
    # attempts=3 would otherwise allow it.
    assert chromium.launch_calls == 2
    assert _no_real_sleep == [30]


# ---------------------------------------------------------------------------
# Retry / backoff on non-blocking failures
# ---------------------------------------------------------------------------


def test_retry_backoff_and_fresh_context_each_attempt(monkeypatch, _no_real_sleep):
    ok_body = {"journeys": [{"departure": "07:25"}]}
    failing_page_1 = FakePage(responses=[], dom_html=None)  # -> TimeoutScrapeError
    failing_page_2 = FakePage(responses=[], dom_html=None)  # -> TimeoutScrapeError
    succeeding_page = FakePage(responses=[_journey_search_response(ok_body)])
    chromium = ScenarioChromium(pages=[failing_page_1, failing_page_2, succeeding_page])
    install_fake_playwright(monkeypatch, chromium)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=3)

    assert result == ok_body
    assert chromium.launch_calls == 3  # a fresh browser (and context) each attempt
    for browser in chromium.browsers:
        assert browser.contexts_created == 1
    assert _no_real_sleep == [30, 90]


def test_all_attempts_failing_raises_after_backoff(monkeypatch, _no_real_sleep):
    pages = [FakePage(responses=[], dom_html=None) for _ in range(3)]
    chromium = ScenarioChromium(pages=pages)
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=3)

    assert chromium.launch_calls == 3
    assert _no_real_sleep == [30, 90]


# ---------------------------------------------------------------------------
# Chromium binary missing
# ---------------------------------------------------------------------------


def test_missing_chromium_binary_gives_actionable_message(monkeypatch, _no_real_sleep):
    launch_error = PlaywrightError(
        "Executable doesn't exist at /root/.cache/ms-playwright/chromium-1234/chrome\n"
        "Looks like Playwright was just installed. Please run:\n\n"
        "    playwright install\n"
    )
    chromium = ScenarioChromium(pages=[], launch_raises=launch_error)
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.ScraperError) as exc_info:
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert "playwright install chromium" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Slow network / hard page budget
# ---------------------------------------------------------------------------


def test_slow_network_hits_hard_page_budget(monkeypatch):
    # No responses ever arrive and there's no DOM fallback: the polling
    # loop in _wait_for_result must give up once PAGE_BUDGET_SECONDS (set
    # tiny by the autouse fixture) elapses, not hang forever.
    page = FakePage(responses=[], dom_html=None)
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


# ---------------------------------------------------------------------------
# Navigation timeout
# ---------------------------------------------------------------------------


def test_navigation_timeout_raises_timeout_scrape_error(monkeypatch):
    page = FakePage(goto_raises=PlaywrightTimeoutError("Timeout 45000ms exceeded"))
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


# ---------------------------------------------------------------------------
# Artifacts on final failure
# ---------------------------------------------------------------------------


def test_final_failure_writes_artifacts_before_raising(monkeypatch, tmp_path):
    partly_captured = FakeResponse(
        url="https://www.thetrainline.com/api/journey-search/",
        status=200,
        body=None,
        bad_json=True,
    )
    page = FakePage(responses=[partly_captured], dom_html=None, content="<html>no luck</html>")
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    artifacts_dir = tmp_path / "artifacts"
    travel_date = date(2026, 9, 8)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(travel_date, artifacts_dir=artifacts_dir, attempts=1)

    screenshot = artifacts_dir / f"screenshot-{travel_date.isoformat()}.png"
    html = artifacts_dir / f"page-{travel_date.isoformat()}.html"
    response_json = artifacts_dir / f"response-{travel_date.isoformat()}.json"

    assert screenshot.exists() and screenshot.read_bytes()
    assert html.exists()
    assert html.read_text(encoding="utf-8") == "<html>no luck</html>"
    assert response_json.exists()
    captured = json.loads(response_json.read_text(encoding="utf-8"))
    assert captured["status"] == 200
    assert captured["body"] is None


def test_no_artifacts_written_when_artifacts_dir_is_none(monkeypatch):
    page = FakePage(responses=[], dom_html=None)
    chromium = ScenarioChromium(pages=[page])
    install_fake_playwright(monkeypatch, chromium)

    # Should simply raise, with no filesystem side effects and no crash
    # from a missing artifacts_dir.
    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), artifacts_dir=None, attempts=1)


def test_attempts_must_be_at_least_one():
    with pytest.raises(ValueError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=0)
