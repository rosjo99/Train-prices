"""Tests for src.scraper.

No test in this file ever launches a real browser or makes a real network
call: `camoufox.sync_api.Camoufox` and `camoufox.sync_api.NewContext` are
always monkeypatched to a set of fake objects (FakeCamoufoxFactory/
FakeBrowser/FakeContext/FakePage/FakeResponse below) that mimic just
enough of the Camoufox/Playwright sync API surface for src.scraper to
drive. See docs/plans/005-migrate-to-tpe.md Task 3/§6.2 for the
acceptance criteria these tests transcribe.
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

TPE_JOURNEYS_GRID_URL = (
    "https://ticket.tpexpress.co.uk/journeys-grid/OXF/PAD/2026-09-08T07:20"
    "//1//YNGx1?departNow=no&realTime=no&searchPreferences=%2C%2C%2C%2Ctrue"
    "&showAdditionalRoutes=no&showCheapest=no&tocSpecific=no"
)
API_RESPONSE_URL = "https://api.tpexpress.co.uk/jp/journey-plan"


# ---------------------------------------------------------------------------
# Fakes: a minimal stand-in for the bits of Camoufox/Playwright's sync API
# that src.scraper touches.
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


class FakePage:
    def __init__(
        self,
        *,
        responses: list[FakeResponse] | None = None,
        banner_present: bool = False,
        url: str = TPE_JOURNEYS_GRID_URL,
        content: str = "<html>ok</html>",
        goto_raises: Exception | None = None,
    ):
        self._handlers: list[Any] = []
        self._responses = responses or []
        self.banner_present = banner_present
        self.url = url
        self._content = content
        self.goto_raises = goto_raises
        self.goto_calls: list[tuple[str, str | None, float | None]] = []
        self.screenshot_calls: list[str] = []
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

    def content(self) -> str:
        return self._content

    def screenshot(self, path: str | None = None) -> None:
        self.screenshot_calls.append(path)
        if path:
            Path(path).write_bytes(b"fake-png-bytes")


class FakeContext:
    def __init__(self, page: FakePage):
        self._page = page
        self.routes: list[tuple[str, Any]] = []
        self.closed = False

    def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def new_page(self) -> FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, page: FakePage):
        self._page = page
        self.contexts_created = 0


def fake_new_context(browser: FakeBrowser, **kwargs: Any) -> FakeContext:
    browser.contexts_created += 1
    return FakeContext(browser._page)


class FakeCamoufoxCM:
    """The object returned by calling FakeCamoufoxFactory(...), mimicking
    `Camoufox(...)` — a context manager whose __enter__ does the actual
    "launch" (and so is where a missing-binary error would surface, same
    as real Camoufox).
    """

    def __init__(self, factory: "FakeCamoufoxFactory", kwargs: dict[str, Any]):
        self._factory = factory
        self.kwargs = kwargs

    def __enter__(self) -> FakeBrowser:
        self._factory.launch_calls += 1
        if self._factory.launch_raises is not None:
            raise self._factory.launch_raises
        page = self._factory._pages[self._factory.launch_calls - 1]
        browser = FakeBrowser(page)
        self._factory.browsers.append(browser)
        return browser

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class FakeCamoufoxFactory:
    """Replaces ScenarioChromium: hands out one page per __enter__() call,
    in order, so a test can script what happens attempt-by-attempt. Called
    with (headless=..., humanize=..., locale=...), exactly like the real
    `Camoufox(...)` constructor.
    """

    def __init__(self, pages: list[FakePage] | None = None, launch_raises: Exception | None = None):
        self._pages = list(pages or [])
        self.launch_raises = launch_raises
        self.launch_calls = 0
        self.browsers: list[FakeBrowser] = []

    def __call__(self, **kwargs: Any) -> FakeCamoufoxCM:
        return FakeCamoufoxCM(self, kwargs)


def install_fake_camoufox(monkeypatch: pytest.MonkeyPatch, factory: FakeCamoufoxFactory) -> None:
    """Monkeypatch camoufox.sync_api.Camoufox/NewContext.

    src.scraper imports these from inside _attempt_once/_launch_browser,
    so patching the attributes on the camoufox.sync_api module (not on
    src.scraper) is what actually takes effect — same reason the NRE-era
    version of this file patched playwright.sync_api.sync_playwright.
    """
    monkeypatch.setattr("camoufox.sync_api.Camoufox", factory)
    monkeypatch.setattr("camoufox.sync_api.NewContext", fake_new_context)


@pytest.fixture(autouse=True)
def _set_route_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override config with fixed test CRS codes/railcard, independent of
    whatever real values config currently holds, so these tests don't
    depend on (or break when someone updates) the real route config.
    """
    monkeypatch.setattr(scraper.config, "ORIGIN_CRS", "OXF")
    monkeypatch.setattr(scraper.config, "DESTINATION_CRS", "PAD")
    monkeypatch.setattr(scraper.config, "RAILCARD_CODE", "YNG")
    monkeypatch.setattr(scraper.config, "ANCHOR_OFFSET_MINUTES", 5)
    monkeypatch.setattr(scraper.config, "TARGET_DEPARTURES", ("07:25", "07:30"))


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
    response doesn't spend real wall-clock time in the polling loop.
    """
    monkeypatch.setattr(scraper, "PAGE_BUDGET_SECONDS", 0.05)


# ---------------------------------------------------------------------------
# Import cleanliness
# ---------------------------------------------------------------------------


def test_no_top_level_playwright_import():
    """Neither Playwright nor Camoufox may be imported at module level, so
    that `import src.scraper` never requires a browser build to be present.
    """
    source = (REPO_ROOT / "src" / "scraper.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_prefixes = ("playwright", "camoufox")
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(banned_prefixes), (
                    f"found module-level `import {alias.name}`"
                )
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith(banned_prefixes), (
                f"found module-level `from {node.module} import ...`"
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


def test_build_journey_planner_url_contains_expected_pieces():
    url = scraper._build_journey_planner_url(date(2026, 12, 18))
    assert "/OXF/PAD/2026-12-18T07:20" in url
    assert "/YNGx1" in url
    assert "leavingDate=" not in url


def test_build_journey_planner_url_clamps_at_midnight_instead_of_rolling_date_back(monkeypatch):
    # A target departure earlier than ANCHOR_OFFSET_MINUTES would, without
    # the clamp, roll the anchor onto the previous day — silently querying
    # the wrong travel date. Not reachable with today's 07:25/07:30
    # targets, but must be handled anyway.
    monkeypatch.setattr(scraper.config, "TARGET_DEPARTURES", ("00:02",))
    monkeypatch.setattr(scraper.config, "ANCHOR_OFFSET_MINUTES", 5)

    url = scraper._build_journey_planner_url(date(2026, 12, 18))

    assert "/OXF/PAD/2026-12-18T00:00" in url


@pytest.mark.parametrize(
    "status,url,content,expected",
    [
        (403, TPE_JOURNEYS_GRID_URL, "ok", True),
        (500, TPE_JOURNEYS_GRID_URL, "ok", True),
        (200, TPE_JOURNEYS_GRID_URL, "please verify you are human", True),
        (200, TPE_JOURNEYS_GRID_URL, "are you a robot?", True),
        (200, TPE_JOURNEYS_GRID_URL, "ok", False),
        (None, TPE_JOURNEYS_GRID_URL, "ok", False),
        # Regression test: TPE's own page bootstrap config harmlessly
        # embeds a reCAPTCHA site key on every page load (GH Actions run
        # 33530583374, page-2026-09-08.html's "googleRecaptchaKey" field).
        # A bare "captcha" substring match previously misfired on this and
        # must not classify it as blocked.
        (
            200,
            TPE_JOURNEYS_GRID_URL,
            '"googleRecaptchaKey":"6Le4ESkTAAAAAIW-1dS_obXeJ1oOlztiaNZ31hOE"',
            False,
        ),
        (200, TPE_JOURNEYS_GRID_URL, "this mentions recaptcha somewhere", False),
    ],
)
def test_looks_blocked(status, url, content, expected):
    assert scraper._looks_blocked(status, url, content) is expected


@pytest.mark.parametrize(
    "current_url,expected",
    [
        (TPE_JOURNEYS_GRID_URL, False),
        ("https://ticket.tpexpress.co.uk/some-other-page", False),
        ("https://www.tpexpress.co.uk/some-other-page", False),
        (API_RESPONSE_URL, False),
        ("https://www.booking.com/searchresults.html?ss=London", True),
        ("", False),
    ],
)
def test_looks_hijacked(current_url, expected):
    assert scraper._looks_hijacked(current_url) is expected


# ---------------------------------------------------------------------------
# Successful end-to-end flow (fake Camoufox)
# ---------------------------------------------------------------------------


def _journey_plan_response(body: dict[str, Any]) -> FakeResponse:
    return FakeResponse(url=API_RESPONSE_URL, status=200, body=body)


def test_successful_fetch_returns_captured_json_body(monkeypatch):
    body = {"links": {}, "result": {"outward": []}}
    page = FakePage(responses=[_journey_plan_response(body)])
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert result == body
    assert factory.launch_calls == 1


def test_context_route_is_registered_before_new_page(monkeypatch):
    """The iframe/hijack route guard must be installed on the context
    before new_page() is called, so it's active for the very first
    navigation.
    """
    body = {"links": {}, "result": {"outward": []}}
    page = FakePage(responses=[_journey_plan_response(body)])
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    browser = factory.browsers[0]
    assert browser.contexts_created == 1


def test_sibling_endpoint_on_same_host_is_ignored_even_if_it_arrives_last(monkeypatch):
    """Regression test for a real live hazard: api.tpexpress.co.uk also
    serves sibling endpoints (e.g. "/jp/plusbus") for other page data. A
    same-host response handler matching on host alone would let this
    later, unrelated response overwrite the real journey-plan body,
    producing a "successful" result with no result.outward. The handler
    must match the specific "/jp/journey-plan" path, regardless of
    arrival order.
    """
    body = {"links": {}, "result": {"outward": []}}
    sibling_response = FakeResponse(
        url="https://api.tpexpress.co.uk/jp/plusbus", status=200, body={"unrelated": True}
    )
    real_response = _journey_plan_response(body)
    # Sibling arrives AFTER the real response — this ordering is exactly
    # what caused the live NRE bug this guards against, since a handler
    # matching on host alone keeps overwriting `captured` on every
    # same-host response with no path check.
    page = FakePage(responses=[real_response, sibling_response])
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert result == body


@pytest.mark.parametrize("banner_present", [True, False])
def test_cookie_banner_present_or_absent_both_succeed(monkeypatch, banner_present):
    body = {"links": {}, "result": {"outward": []}}
    page = FakePage(responses=[_journey_plan_response(body)], banner_present=banner_present)
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert result == body
    assert page.cookie_locator.clicked is banner_present


# ---------------------------------------------------------------------------
# No response, no fallback
# ---------------------------------------------------------------------------


def test_no_response_raises_timeout_not_empty_success(monkeypatch):
    page = FakePage(responses=[])
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


def test_blocked_via_403_status_raises_blocked_error(monkeypatch):
    body_response = FakeResponse(url=API_RESPONSE_URL, status=403, body=None)
    page = FakePage(responses=[body_response])
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.BlockedError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


def test_blocked_via_strong_marker_content_raises_blocked_error(monkeypatch):
    page = FakePage(responses=[], content="oops, are you a robot?")
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.BlockedError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


def test_harmless_recaptcha_key_in_page_content_does_not_raise_blocked_error(monkeypatch):
    """Regression test for a real production failure: TPE's own page
    bootstrap config embeds a harmless reCAPTCHA site key on every page
    load. GitHub Actions run 33530583374 failed every travel date
    instantly with BlockedError because a bare "captcha" substring
    matched "googleRecaptchaKey" (see page-2026-09-08.html). This must
    not raise BlockedError, and the page must be able to succeed.
    """
    body = {"links": {}, "result": {"outward": []}}
    page = FakePage(
        responses=[_journey_plan_response(body)],
        content='"googleRecaptchaKey":"6Le4ESkTAAAAAIW-1dS_obXeJ1oOlztiaNZ31hOE"',
    )
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert result == body


def test_blocked_error_retried_at_most_once(monkeypatch, _no_real_sleep):
    blocked_response = FakeResponse(url=API_RESPONSE_URL, status=403, body=None)
    pages = [FakePage(responses=[blocked_response]) for _ in range(3)]
    factory = FakeCamoufoxFactory(pages=pages)
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.BlockedError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=3)

    # Blocked twice (initial + one retry), never a third time even though
    # attempts=3 would otherwise allow it.
    assert factory.launch_calls == 2
    assert _no_real_sleep == [5]


def test_hijacked_raises_and_is_retried_at_most_once(monkeypatch, _no_real_sleep):
    """A page that ends up off tpexpress.co.uk entirely (never observed on
    TPE, but guarded against — see HijackedError's docstring) must raise
    HijackedError and back off exactly like a block, not be treated as "no
    results".
    """
    pages = [
        FakePage(responses=[], url="https://www.booking.com/searchresults.html")
        for _ in range(3)
    ]
    factory = FakeCamoufoxFactory(pages=pages)
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.HijackedError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=3)

    assert factory.launch_calls == 2
    assert _no_real_sleep == [5]


# ---------------------------------------------------------------------------
# Retry / backoff on non-blocking failures
# ---------------------------------------------------------------------------


def test_retry_backoff_and_fresh_context_each_attempt(monkeypatch, _no_real_sleep):
    ok_body = {"links": {}, "result": {"outward": []}}
    failing_page_1 = FakePage(responses=[])  # -> TimeoutScrapeError
    failing_page_2 = FakePage(responses=[])  # -> TimeoutScrapeError
    succeeding_page = FakePage(responses=[_journey_plan_response(ok_body)])
    factory = FakeCamoufoxFactory(pages=[failing_page_1, failing_page_2, succeeding_page])
    install_fake_camoufox(monkeypatch, factory)

    result = scraper.fetch_journey_search(date(2026, 9, 8), attempts=3)

    assert result == ok_body
    assert factory.launch_calls == 3  # a fresh browser (and context) each attempt
    for browser in factory.browsers:
        assert browser.contexts_created == 1
    assert _no_real_sleep == [5, 10]


def test_all_attempts_failing_raises_after_backoff(monkeypatch, _no_real_sleep):
    pages = [FakePage(responses=[]) for _ in range(3)]
    factory = FakeCamoufoxFactory(pages=pages)
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=3)

    assert factory.launch_calls == 3
    assert _no_real_sleep == [5, 10]


# ---------------------------------------------------------------------------
# Camoufox binary missing
# ---------------------------------------------------------------------------


def test_missing_camoufox_binary_gives_actionable_message(monkeypatch, _no_real_sleep):
    launch_error = RuntimeError(
        "Executable doesn't exist at /root/.cache/camoufox/camoufox-1234/camoufox\n"
        "Looks like Camoufox was just installed. Please run:\n\n"
        "    python -m camoufox fetch\n"
    )
    factory = FakeCamoufoxFactory(pages=[], launch_raises=launch_error)
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.ScraperError) as exc_info:
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)

    assert "python -m camoufox fetch" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Slow network / hard page budget
# ---------------------------------------------------------------------------


def test_slow_network_hits_hard_page_budget(monkeypatch):
    # No response ever arrives: the polling loop in _wait_for_result must
    # give up once PAGE_BUDGET_SECONDS (set tiny by the autouse fixture)
    # elapses, not hang forever.
    page = FakePage(responses=[])
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


# ---------------------------------------------------------------------------
# Navigation timeout
# ---------------------------------------------------------------------------


def test_navigation_timeout_raises_timeout_scrape_error(monkeypatch):
    page = FakePage(goto_raises=PlaywrightTimeoutError("Timeout 45000ms exceeded"))
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=1)


# ---------------------------------------------------------------------------
# Artifacts on final failure
# ---------------------------------------------------------------------------


def test_final_failure_writes_artifacts_before_raising(monkeypatch, tmp_path):
    partly_captured = FakeResponse(url=API_RESPONSE_URL, status=200, body=None, bad_json=True)
    page = FakePage(responses=[partly_captured], content="<html>no luck</html>")
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

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
    page = FakePage(responses=[])
    factory = FakeCamoufoxFactory(pages=[page])
    install_fake_camoufox(monkeypatch, factory)

    # Should simply raise, with no filesystem side effects and no crash
    # from a missing artifacts_dir.
    with pytest.raises(scraper.TimeoutScrapeError):
        scraper.fetch_journey_search(date(2026, 9, 8), artifacts_dir=None, attempts=1)


def test_attempts_must_be_at_least_one():
    with pytest.raises(ValueError):
        scraper.fetch_journey_search(date(2026, 9, 8), attempts=0)
