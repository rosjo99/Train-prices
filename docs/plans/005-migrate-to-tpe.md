# Plan 005 — Migrate the data source from National Rail Enquiries to TransPennine Express

Status: ready to implement. All research in §1 was confirmed live on
2026-09-01 via GitHub Actions runs **33525860120** and **33527007099** on
branch `claude/migrate-tpe-data-source-xh2g09` (Camoufox + the real TPE
site), not inferred. Three items are deliberately left *unverified and
labelled as such* rather than guessed — §7.

Scope: **swap the retailer and the browser.** Everything downstream of
`parse_journeys()` is untouched — `TrainOption`, `evaluate()`, the
scheduler in `src/main.py`, term dates, booked dates, the price log, the
email. Plans 001-004 stay historical records and are **not** edited; this
plan amends 001 §1/§Task 3/§Task 4 by reference, the way 003 amended 002.

Carried over unchanged from plan 002 as the hard correctness constraint,
with "NRE" now reading "TPE":

> **No date whose fares the retailer has actually released may ever go
> unchecked.**

---

## 1. Why this migration, and what was confirmed

### 1.1 What changes

| | NRE (today) | TPE (after this plan) |
| --- | --- | --- |
| Page | `www.nationalrail.co.uk/journey-planner/?...` | `ticket.tpexpress.co.uk/journeys-grid/...` |
| Date format in URL | `DDMMYY` | ISO `YYYY-MM-DD` |
| Railcard in URL | query param `railcards=YNG%7C1` | path segment `YNGx1` |
| Stations | CRS in query params | CRS in the path (TPE resolves to its own IDs) |
| Fares transport | GET XHR `jpservices.nationalrail.co.uk/journey-planner` | **POST** `api.tpexpress.co.uk/jp/journey-plan` |
| Response shape | flat `outwardJourneys` list | JSON:API-style `{links, result}` graph with ref indirection |
| Railcard price | undiscounted `totalPrice` + separate `railcardFares[]` | one already-discounted `totalPrice` per fare |
| Timestamps | offset-aware ISO (`...+01:00`) | **naive** ISO, already UK local wall clock |
| Journeys returned | the whole day from the anchor onward | a fixed window of the next 3 (§1.4) |
| Fare-release horizon | measured ~94 days | believed **much** further out (§7.1) |
| Browser | Playwright headless Chromium | **Camoufox** (headless, `humanize=True`) |
| Cookie banner | OneTrust (`#onetrust-accept-btn-handler`) | Usercentrics (`#usercentrics-root`, `data-testid="uc-*"`) |

### 1.2 Deep link (already in `src/config.py` on this branch)

`config.JOURNEY_PLANNER_URL_TEMPLATE`, `ORIGIN_CRS`/`DESTINATION_CRS`,
`RAILCARD_CODE`, `ANCHOR_OFFSET_MINUTES`, `JOURNEY_PLANNER_API_HOST`,
`JOURNEY_PLANNER_API_PATH` and `TPE_HOST_SUFFIX` were already repointed
directly in `src/config.py`, with their evidence in the comments there.
**Treat that diff as decided groundwork; do not redesign it.** It was
re-read during planning and is correct — in particular
`build_journey_planner_url()` keeps its `(travel_date, hour, minute)`
signature, so `src/notifier.py`'s per-fare email link (line ~259) and
`tests/test_notifier.py`'s assertion keep working with no edit at all.

The one thing to check while implementing: `TPE_HOST_SUFFIX =
"tpexpress.co.uk"` deliberately covers `www.`, `ticket.` **and** `api.`,
so the hijack guard does not fire on the page's own API calls.

### 1.3 Response shape (read the real fixture before writing any code)

`tests/fixtures/journey_plan_sample.json` is the **raw response body**
(top-level keys `links`, `result`) — not the capture wrapper that
`scripts/capture_fixture_tpe.py` writes. The parser receives exactly this.

```
{
  "result": {"outward": [
      {"journey": "/jp/journeys/352%3A352%3AOXF_PAD_W76844",
       "fares": {"cheapest": {"outwardSingle": "<fare ref>", "totalPrice": 930},
                 "singles": ["<fare ref>", ...],
                 "returns":  ["<fare ref>", ...]}},
      ...]},
  "links": { "<every ref above>": {...}, "/data/ticket-types/W2M": {...},
             "/data/railcards/YNG": {...}, "/data/stations/3115": {...} }
}
```

Resolved **journey** object (confirmed fields):
`origin.time.scheduledTime` = `"2026-12-18T07:25:00"` (naive),
`destination.time.scheduledTime` likewise, `changes: 0`, `legs: [...]`,
plus `isCancelled`, `status`, `callingPoints`.

Resolved **fare** object (confirmed):

```
{"totalPrice": 930, "originalTotalPrice": 1400,
 "ticketType": "/data/ticket-types/W2M", "category": "Advance",
 "tickets": [{"adults":1,"children":0,"price":930,"originalPrice":1400,
              "railcard":"/data/railcards/YNG","railcardDiscount":470,
              "statusCode":"003"}]}
```

`links["/data/ticket-types/W2M"]["name"] == "Advance Single"` — this is
the replacement for NRE's `typeDescription`.
`links["/data/railcards/YNG"] == {"code": "YNG", "name": "16-25 Railcard", ...}`.

Prices are **pence integers**, exactly as with NRE, so
`Decimal(pence) / 100` and the "no `float(` anywhere" rule carry over
unchanged.

Two shape facts worth knowing before writing the parser, both visible in
the real fixture:

- **Fare objects are shared across journeys.** All three journeys in the
  sample point at the *same* Advance fare ref, so all three get the same
  £9.30. That is TPE's model (an Advance fare is listed against every
  journey it is valid for), not a bug, and it means the 07:25 and 07:30
  prices are often identical. Do not "deduplicate" or second-guess it.
- **There is no currency field anywhere** in the response (checked
  directly against the fixture). `currency="GBP"` stays a fixed value for
  the same reason it was with NRE.

### 1.4 The pagination gotcha — why the anchor is offset

TPE's frontend hardcodes `"numJourneys": 3` in its own POST body; the
deep-link exposes no lever for it. Confirmed live:

| Anchor | Journeys returned | Both targets present? |
| --- | --- | --- |
| 07:00 | 07:02, 07:16, 07:25 | **no** — 07:30 pushed out |
| 07:20 | 07:25, 07:30, 07:53 | yes |

Hence `config.ANCHOR_OFFSET_MINUTES = 5` and the scraper anchoring at
`min(TARGET_DEPARTURES) - ANCHOR_OFFSET_MINUTES`. This is an **empirical
workaround, not a guarantee** — a service inserted in that 5-minute gap
on some future timetable could still push 07:30 out of the window. The
failure mode is benign and visible: `select_target_trains` returns `None`
for 07:30, `main()` logs it as not found, and no wrong price is ever
reported. See §7.3 for what to watch.

### 1.5 Bot protection and the cookie banner

No CAPTCHA, block page, or challenge was seen on TPE across the probe run
and the fixture-capture run. The Usercentrics banner is rendered
client-side into `#usercentrics-root`; the selector
`button:has-text('Accept All')` dismissed it successfully (log line
`[info] dismissed cookie banner via "button:has-text('Accept All')"`,
run 33527007099). Keep the prioritized-list, never-fatal
`_dismiss_cookie_banner` pattern, with the confirmed selector present and
the `uc-*` variants around it.

### 1.6 Why Camoufox

CLAUDE.md already states "when attempting other booking platforms use the
Camoufox browser", and `scripts/capture_fixture_tpe.py` is the **validated
reference implementation** — read it before writing `src/scraper.py`. Its
launch shape is what the scraper must adopt verbatim:

```python
with Camoufox(headless=True, humanize=True, locale="en-GB") as browser:
    context = NewContext(browser, locale="en-GB", timezone_id="Europe/London",
                         viewport={"width": 1366, "height": 768})
```

Consequence that is easy to miss: **delete the `USER_AGENT` constant.**
Camoufox is Firefox-based and generates its own coherent fingerprint;
injecting a hand-written desktop-Chrome UA string would make that
fingerprint internally inconsistent — strictly worse than not setting one.
Same for `--disable-blink-features=AutomationControlled` / `--no-sandbox`,
which are Chromium flags with no meaning here.

---

## 2. Task 1 — `src/config.py` (verification only, no rewrite)

Already done on this branch. The coder's job is a read-through, not an
edit: confirm every constant listed in §1.2 exists, that nothing still
references `NRE_HOST_SUFFIX`, and that `python -c "import src.config"`
still succeeds. If a leftover NRE-named constant is found, rename it and
fix the callers — nothing else.

---

## 3. Task 2 — rewrite `src/parser.py`

**Files:** `src/parser.py` (rewrite), `tests/test_parser.py` (rewrite —
see §6.1).

### 3.1 What must not change

The module's public surface is load-bearing for `src/main.py` and stays
identical in signature and semantics:

- `ParseError`
- `parse_journeys(raw: dict, travel_date: date) -> list[TrainOption]`
- `extract_price(...) -> Decimal | None`
- `select_target_trains(options, target_times) -> dict[str, TrainOption | None]`
  — **entirely unchanged, do not touch it.**
- `TrainOption` and every one of its fields, with today's meanings
  (`price=None` + `sold_out=True` for a journey with no priced fare;
  `railcard_applied` informational only, never gating).
- Money via `Decimal(pence) / Decimal("100")`, never `float`.

`extract_price` keeps taking *one journey entry* — but under TPE a
journey entry cannot be priced without the `links` dict, so its signature
becomes `extract_price(entry: dict, links: dict) -> Decimal | None`. This
is the one intentional signature change in the module; `main.py` does not
call it (grep to confirm before changing it), only tests do.

### 3.2 Structure to implement

1. **Container check.** `raw` must be a dict with `raw["result"]["outward"]`
   a list, and `raw["links"]` a dict. Otherwise `ParseError` naming the
   missing key and saying TPE may have changed its schema. Non-dict input
   (`[]`, `None`, `"not a dict"`, `{}`) → `ParseError`, same as today.
2. **`_resolve(links, ref)`** — returns `links.get(ref)` when `ref` is a
   `str` and present, else `None`. Every ref lookup goes through it; a
   missing ref is logged at warning level and skipped, never a crash.
   Refs are percent-encoded strings used verbatim as dict keys — **do not
   unquote them.**
3. **`_best_fare(entry, links)`** → `(fare_name, price, railcard_applied)`
   or `None`.
   - Candidate refs = `entry["fares"]["singles"]` plus
     `entry["fares"]["cheapest"]["outwardSingle"]` if present, deduped
     while preserving order. Including `cheapest` is belt-and-braces for
     the case where it is not also in `singles`; in the real fixture it is.
   - **Ignore `returns` entirely** — this project is one-way only.
   - Resolve each ref; skip any that is missing or whose `totalPrice` is
     absent/non-integer. Take the plain minimum `totalPrice`. No NRE-style
     dual-measure comparison or tie-break logic: TPE returns one
     already-discounted price per fare (§1.3).
   - Ties: first candidate in the deduped order wins (deterministic).
   - `fare_name` = `_resolve(links, fare["ticketType"])["name"]`, or
     `None` if unresolvable — a missing name must not lose the price.
   - `railcard_applied` = the winning fare has a `tickets` entry whose
     `railcard` ref's basename equals `config.RAILCARD_CODE` (i.e.
     `ref.rsplit("/", 1)[-1] == config.RAILCARD_CODE`). Read
     `config.RAILCARD_CODE` at call time so tests can monkeypatch it. Do
     **not** additionally require `railcardDiscount > 0`.
   - If no candidate resolves to a price → `None`.
4. **`_to_hhmm(ts)`** — replaces `_to_london_hhmm`. TPE's
   `scheduledTime` is naive UK local wall-clock time, so this is
   `datetime.fromisoformat(ts).strftime("%H:%M")` with **no**
   `.astimezone()`. Return `None` for falsy input or `ValueError`.
   *Defensive detail:* if a future response ever does carry an offset,
   `fromisoformat` still parses it and `.strftime` still yields the local
   wall clock in that offset — acceptable, and the alternative
   (`.astimezone(LONDON)` on a naive value) would silently mis-shift
   times by assuming the runner's TZ. Say so in a comment; this is the
   single most likely place for a subtle bug.
5. **`parse_journeys`** — for each entry in `result.outward`:
   resolve `entry["journey"]`; skip-with-warning if unresolvable or if
   `origin.time.scheduledTime` is unparsable (mirrors today's
   skip-on-bad-departure rule, keyed on the journey ref for the log
   line). `arrival_time` from `destination.time.scheduledTime`, `None`
   tolerated. `is_direct` = `changes == 0` when `changes` is an int, else
   `len(legs) <= 1`. `price`/`fare_name`/`railcard_applied` from
   `_best_fare`; `sold_out = price is None`. `currency="GBP"` (§1.3).

### 3.3 Acceptance criteria

- Real fixture → 3 options at 07:25 / 07:30 / 07:53; the first two have
  `price == Decimal("9.30")`, `railcard_applied is True`,
  `sold_out is False`, `fare_name == "Advance Single"`, `is_direct is True`,
  arrival times `08:27` / `08:25`.
- `journey_plan_empty.json` → `[]`, no raise.
- `journey_plan_missing_container.json` → `ParseError`.
- `journey_plan_fareless_journey.json` → one option, `sold_out is True`,
  `price is None`, `fare_name is None`, `railcard_applied is False`.
- `journey_plan_no_railcard.json` → prices still returned (£9.30),
  `railcard_applied is False`.
- `journey_plan_only_0725.json` → `select_target_trains` gives
  `07:30 → None`.
- A ref present in `result.outward` but absent from `links` → that
  journey is skipped with a warning; other journeys still parse.
- `"float(" not in src/parser.py`.

---

## 4. Task 3 — rewrite `src/scraper.py`

**Files:** `src/scraper.py` (rewrite), `tests/test_scraper.py` (rewrite —
see §6.2).

### 4.1 What must not change

`tests/test_main.py` imports these by name and is otherwise decoupled
from the scraping format — all of them keep their names, inheritance and
meaning:

- `ScraperError`, `BlockedError`, `HijackedError`, `TimeoutScrapeError`
- `fetch_journey_search(travel_date, *, artifacts_dir=None, attempts=3)`,
  returning the raw response dict and **never** returning an
  empty-but-successful result.
- The retry loop verbatim: `attempts` tries, `RETRY_BACKOFF_SECONDS`
  backoff, a brand new browser per attempt, `Blocked`/`Hijacked` retried
  at most once total, `ValueError` for `attempts < 1`, and the existing
  per-attempt log lines.
- `_write_failure_artifacts` and its three artifact filenames.
- **No top-level browser import.** `import src.scraper` must still
  succeed with no browser binary installed. `from camoufox.sync_api
  import Camoufox, NewContext` goes *inside* `_attempt_once`, exactly
  where the playwright imports are today.

### 4.2 Changes

1. **Module docstring.** Rewrite for TPE/Camoufox: TPE's own booking
   engine, deep link to the journeys-grid, fares read from the
   same-origin POST to `/jp/journey-plan`, Camoufox because CLAUDE.md
   mandates it for non-NRE platforms and it is what was validated live.
   Keep the "this module receives no secrets" paragraph and the
   "attempt" terminology paragraph.
2. **Delete `USER_AGENT`** (§1.6) and the Chromium `args=[...]`.
3. **`_launch_browser`** → a Camoufox equivalent. Camoufox's
   missing-browser error type is **not known** — do not guess at an
   exception class. Catch `Exception`, and if the message mentions any of
   `camoufox`, `fetch`, `not found`, `executable`, `no such file`
   (case-insensitive), raise
   ``ScraperError("Camoufox browser not found. Run `python -m camoufox
   fetch` and retry. (original error: ...)")``; otherwise
   `ScraperError(f"failed to launch Camoufox: {exc}")`. Because
   `Camoufox(...)` is a context manager, the cleanest shape is to let
   `_attempt_once` own the `with Camoufox(...) as browser:` block and
   wrap just that construction/entry in the translating try — pick
   whichever of the two placements keeps `_attempt_once` readable, but
   the actionable message must survive out to the caller.
4. **`_attempt_once`** — replace the `with sync_playwright() as p:` body
   with the validated shape from §1.6:
   `with Camoufox(...) as browser: context = NewContext(browser, ...)`,
   then `context.route("**/*", _make_route_handler())` **before**
   `context.new_page()`, then `page.on("response", ...)` **before**
   `page.goto(...)` — both orderings are existing, tested requirements.
   `context.close()` in a `finally` (Camoufox's `with` closes the
   browser). Everything else — goto with `wait_until="domcontentloaded"`,
   cookie banner, `_wait_for_result`, artifact writing, exception
   mapping — keeps its current shape. Keep the `PlaywrightError` /
   `PlaywrightTimeoutError` catches, still imported lazily from
   `playwright.sync_api` (still correct: Camoufox drives Playwright and
   raises Playwright's exception types).
5. **`_looks_hijacked` / `_make_route_handler`** — swap
   `config.NRE_HOST_SUFFIX` for `config.TPE_HOST_SUFFIX` and reword the
   messages ("navigated away from tpexpress.co.uk"). Logic unchanged.
   `HijackedError`'s docstring must be re-grounded honestly: no hijack
   was ever observed on TPE either; the guard is defense in depth for an
   unattended job on a page carrying third-party scripts (Usercentrics,
   Google Maps, PayPal).
6. **`_is_journey_planner_response`** — keep, unchanged in logic:
   host check plus `urlparse(url).path.rstrip("/") ==
   config.JOURNEY_PLANNER_API_PATH`. The sibling-endpoint hazard is real
   on TPE too (`/jp/plusbus` observed on the same host), so keep the
   regression test.
7. **`_build_journey_planner_url`** — anchor at
   `min(config.TARGET_DEPARTURES)` minus `config.ANCHOR_OFFSET_MINUTES`
   (§1.4). Implement with `datetime.combine(travel_date, time(...)) -
   timedelta(minutes=...)`, or equivalent minute arithmetic; format back
   to zero-padded `HH`/`MM`. **Edge case to handle explicitly:** if the
   subtraction would cross midnight backwards (a target departure earlier
   than the offset, e.g. `00:02`), clamp to `00:00` rather than rolling
   onto the previous day — rolling the date back would silently query the
   wrong travel date. Not reachable with today's 07:25/07:30 targets;
   handle it anyway and add a unit test.
8. **Timing constants — provisional, and say so.** `PAGE_BUDGET_SECONDS`
   (10) and `NAVIGATION_TIMEOUT_SECONDS` (20) were measured against
   NRE + Chromium (plan 002 §1). Camoufox launches Firefox with
   `humanize=True`, and the validated capture script used a 60s
   navigation timeout and a 20s result wait. **Adopt the validated
   values as the starting point: `NAVIGATION_TIMEOUT_SECONDS = 60.0`,
   `PAGE_BUDGET_SECONDS = 20.0`,** with a comment saying plainly that
   these come from the capture script's working values, that the NRE
   measurements do not transfer, and that they are to be re-tightened
   from the first full live run's timings (§7.2). `POLL_INTERVAL_MS = 250`
   and `RETRY_BACKOFF_SECONDS = (5, 10)` carry over as-is.
9. **Remove the DOM fallback** — `RESULTS_DOM_SELECTOR`,
   `_read_results_dom`, and the fallback branch in `_wait_for_result`.
   Rationale, stated so a reviewer can push back: the NRE selector
   (`[id^='outward-journey-']`) is meaningless on TPE, and inventing an
   unverified TPE one would be copying a lie forward. More importantly
   the fallback never *worked*: it returned
   `{"_source": "dom-fallback", "_raw_html": ...}`, which `parse_journeys`
   cannot parse, so its only effect was converting a truthful
   `TimeoutScrapeError` into a misleading `ParseError`. It was never
   exercised in any observed run. `_wait_for_result` therefore ends with
   an unconditional `TimeoutScrapeError`, which is the honest failure
   mode and the one `main()` already handles. *This is the one judgement
   call in this plan that a reviewer might reverse; if it is reversed,
   the replacement selector must be captured from a real TPE page, not
   guessed.*
10. **`BLOCK_MARKERS`** — keep as-is (generic markers). Update the
    comment: TPE showed none of them across both live runs.

### 4.3 Acceptance criteria

- Every behaviour the current `tests/test_scraper.py` asserts, minus the
  two DOM-fallback tests, still holds against the Camoufox fakes.
- `python -c "import src.scraper"` succeeds in a subprocess with no
  browser installed (existing test, keep it).
- No module-level `camoufox` or `playwright` import (existing AST test,
  extended to cover `camoufox`).
- The deep link built for `date(2026, 12, 18)` contains
  `/OXF/PAD/2026-12-18T07:20`, `/YNGx1`, and no `leavingDate=`.

---

## 5. Task 4 — dependencies, CI, and retired dev tooling

**Files:** `requirements.txt`, `.github/workflows/price-check.yml`,
`.github/workflows/test.yml` (comment only), delete
`scripts/capture_fixture.py` and `.github/workflows/capture-fixture.yml`.

1. **`requirements.txt`** — replace `playwright==1.62.0` with a pinned
   `camoufox[geoip]==<latest at implementation time>`. Camoufox depends
   on playwright itself, so playwright remains importable (the lazy
   `from playwright.sync_api import Error` in the scraper and the
   module-level one in `tests/test_scraper.py` keep working) — do **not**
   also pin playwright directly, or the two pins can conflict. Verify
   with `pip install -r requirements.txt && python -c "import camoufox,
   playwright"` before committing the pin.
2. **`price-check.yml`** —
   - `pip install -r requirements.txt` unchanged.
   - Replace `- run: playwright install chromium` with
     `- run: python -m camoufox fetch`, mirroring the already-working
     steps in `.github/workflows/Probe TPE with Camoufox (manual
     diagnostic).yml`.
   - Repoint the `actions/cache@v4` step: `~/.cache/ms-playwright` is a
     Chromium path. Camoufox's browser lives elsewhere — resolve it with
     `python -m camoufox path` (the probe workflow already prints it) and
     cache that directory, keyed on `requirements.txt` as today. If the
     path turns out to be non-obvious or user-scoped, **drop the cache
     step entirely** rather than caching the wrong directory; a
     re-download per run is a correctness-neutral cost.
   - Delete the long `--with-deps` comment block (lines ~69-82) — it is
     entirely about Chromium's shared libraries on the runner image and
     is now false. Replace it with one line stating that
     `python -m camoufox fetch` downloads the Camoufox build, validated
     live on the probe/capture runs.
   - `timeout-minutes`: raise **20 → 30**, and rewrite that comment. The
     1m50s-2m projection came from Chromium-based measurements (plans
     002/003) and no longer holds; a Firefox-based, humanized browser per
     date is expected to be slower, by an unmeasured factor, and §7.1
     removes the single-attempt speculative zone that was shortening
     runs. 30 minutes is provisional headroom until §7.2 produces a real
     number — say exactly that in the comment rather than implying it was
     measured.
   - Everything else (schedule, concurrency, permissions, secrets env,
     TEST_RUN derivation, price-history commit step, artifact upload)
     unchanged.
3. **`test.yml`** — its header comment says the tests "mock Playwright's
   sync API". Reword to Camoufox. No step changes: the suite still runs
   with no browser fetched, which is a property to preserve.
4. **Delete `scripts/capture_fixture.py` and
   `.github/workflows/capture-fixture.yml`** (NRE-only, now dead).
   `scripts/capture_fixture_tpe.py` and the probe workflow replace them
   and stay. `scripts/probe_camoufox_tpe.py` also stays.
   `git rm` both, and grep the repo for their filenames afterwards
   (README, workflow comments) — fix any live cross-reference, but **do
   not edit docs/plans/001-004**, which are historical.

---

## 6. Task 5 — tests

### 6.1 `tests/test_parser.py` — rewrite

Every existing test is written against NRE's flat schema and hand-builds
`outwardJourneys` dicts, so this is a rewrite, not an edit. Structure it
the same way (real fixture for the happy path, committed variants for
edge cases, small hand-built graphs for the rest) and cover exactly the
acceptance criteria in §3.3, plus:

- **Ref-indirection unit tests** on small hand-built `{links, result}`
  graphs: a `singles` ref missing from `links`; a `ticketType` ref
  missing from `links` (price still returned, `fare_name is None`);
  a fare whose `tickets[0].railcard` is a *different* railcard ref
  (`railcard_applied is False`, price still returned).
- **`extract_price(entry, links)`** direct tests: cheapest of several
  singles wins; `returns` refs are ignored even when cheaper than every
  single; `None` when nothing resolves.
- **Naive-timestamp test:** a journey with `"2027-01-05T07:25:00"` (no
  offset, winter) must yield `"07:25"` — and the test must **not** pass
  under an `.astimezone()` implementation. Force that by running it with
  a non-London `TZ` (e.g. `monkeypatch.setenv("TZ", "America/New_York")`
  + `time.tzset()`), and assert both a summer and a winter timestamp
  round-trip unchanged. This is the regression guard for §3.2 item 4.
- Keep `test_no_float_anywhere_in_module`.
- The `"07:53"` third journey in the real fixture is a useful extra
  assertion: it must parse, and `select_target_trains` must ignore it.

The hand-built `both_target_trains_sold_out` and `multi_leg` cases
translate directly: build two `result.outward` entries with empty
`singles`, and one entry whose journey has `changes: 1` / two legs.

### 6.2 `tests/test_scraper.py` — rewrite the fakes

The fakes themselves barely change shape; what changes is **what gets
monkeypatched**. `install_fake_playwright` → `install_fake_camoufox`,
patching **both**:

```python
monkeypatch.setattr("camoufox.sync_api.Camoufox", FakeCamoufoxFactory(...))
monkeypatch.setattr("camoufox.sync_api.NewContext", fake_new_context)
```

Note the scraper imports these *inside* `_attempt_once`, so patching the
attributes on the `camoufox.sync_api` module (not on `src.scraper`) is
what works — same reason the current file patches
`playwright.sync_api.sync_playwright`.

- `FakeCamoufoxFactory` replaces `ScenarioChromium`: it is *called* with
  `(headless=..., humanize=..., locale=...)` and returns a context
  manager whose `__enter__` yields the next scripted `FakeBrowser`. Keep
  the "one page per launch, in order" scripting so the retry tests still
  work, and keep a `launch_calls` counter — the assertions
  `launch_calls == 2/3` are the load-bearing retry evidence.
- `fake_new_context(browser, **kwargs)` returns the `FakeContext`. Add a
  `close()` method to `FakeContext` (the scraper now calls it).
- `FakePage.locator` matches on the **first** entry of the new
  `COOKIE_BANNER_SELECTORS` tuple, as today (it indexes
  `scraper.COOKIE_BANNER_SELECTORS[0]`, so it survives the value change
  for free).
- Constants: `NRE_JOURNEY_PLANNER_URL` → a real TPE journeys-grid URL;
  `API_RESPONSE_URL` → `https://api.tpexpress.co.uk/jp/journey-plan`;
  the sibling-endpoint regression test's URL →
  `https://api.tpexpress.co.uk/jp/plusbus` (confirmed to exist live).
  The hijack test's "off-site" URL can stay `booking.com`; add
  `https://ticket.tpexpress.co.uk/...` and
  `https://api.tpexpress.co.uk/...` as **not** hijacked, which is the
  new behaviour worth pinning.
- Fake response bodies become minimal `{"links": {}, "result":
  {"outward": []}}` shapes. The scraper does not parse them, so keep them
  tiny — this is deliberately not a second copy of the parser fixtures.
- `test_missing_chromium_binary_gives_actionable_message` →
  `test_missing_camoufox_binary_gives_actionable_message`, asserting
  `"python -m camoufox fetch"` appears in the message, with the fake
  launch raising a plausible "Executable doesn't exist" style error.
- `test_no_top_level_playwright_import` → assert no module-level import
  of **either** `playwright` or `camoufox`.
- **Delete** `test_dom_fallback_used_when_xhr_never_fires` (per §4.2
  item 9). Keep
  `test_no_xhr_and_no_dom_raises_timeout_not_empty_success`, renamed to
  `test_no_response_raises_timeout_not_empty_success`.
- Update `test_build_journey_planner_url_contains_expected_pieces` per
  §4.3, and add the midnight-clamp test from §4.2 item 7.

### 6.3 `tests/test_main.py` — three helpers, plus three one-off spots

Correcting an assumption it would be easy to make: `test_main.py` **does**
need changes. Its fake scraper replaces `scraper.fetch_journey_search`
only — the **real parser** then runs on whatever raw dict the fake
returned, and those dicts are NRE-shaped. The blast radius is small and
almost entirely confined to three helpers at the top of the file:

- **`_iso` (line ~33)** — drop the hardcoded `+01:00`; TPE timestamps are
  naive (`f"{date}T{hh}:{mm}:00"`).
- **`_journey` (line ~39)** — emit a TPE journey object plus the fare
  objects it needs, and return whatever `_raw` needs to assemble the
  graph. Simplest workable shape: `_journey` returns
  `(outward_entry, links_fragment)` and `_raw` merges the fragments into
  one `{"links": {...}, "result": {"outward": [...]}}`. Give each journey
  and fare a unique synthetic ref (e.g.
  `f"/jp/journeys/test-{departure}"`) so two journeys in one response
  don't collide. `price=None` → an entry with empty `singles` (today's
  sold-out case). Keep the fare name "Advance Single" reachable via a
  `/data/ticket-types/...` link, since assertions elsewhere depend on it.
- **`_raw` (line ~64)** — `{"links": merged, "result": {"outward": [...]}}`.

All ~40 call sites then work unchanged. Three spots to fix by hand:

- **Line ~393** compares `raw == {"outwardJourneys": []}` as a sentinel
  for "an empty `_raw()`". Change it to compare against `_raw()`.
- **`test_speculative_zone_dates_are_still_checked_and_logged`
  (line ~985)** hand-builds a raw journey with an explicit `+00:00`
  offset purely because December falls outside BST. With naive TPE
  timestamps that concern evaporates: replace the hand-built dict with
  `_raw(_journey(speculative_date, "07:25", "08:26", Decimal("8.70")))`
  and delete the six-line BST comment. Do not delete the test — but note
  it and `test_speculative_zone_dates_get_a_single_attempt` both compute
  `today + main.FULL_RETRY_HORIZON_DAYS + 1`, so they follow §7.1's much
  larger constant automatically and stay valid (they stub
  `checkable_dates`, so the date being outside term time is irrelevant).
- **Line ~292's comment** ("NRE only releases fares roughly 12 weeks
  ahead") — reword per §7.1; the test itself is unaffected.

Nothing about the scheduler, the exception classes, `PARALLEL_DATES`, or
any assertion changes. If an assertion *does* break, fix the
scraper/parser rather than the expectation — that would mean a
public-surface change §3.1/§4.1 forbade.

`tests/test_notifier.py`, `test_price_log.py`, `test_term_dates.py`,
`test_booked_dates.py`, `test_models.py`, `test_config.py`: **no changes
expected.** `test_notifier.py` builds its expected link via
`config.build_journey_planner_url`, so it follows the new URL
automatically.

Whole suite must pass: `python -m pytest`.

---

## 7. Honest unknowns — what this plan refuses to invent

### 7.1 `FULL_RETRY_HORIZON_DAYS` — widen it past the school year

**Recommendation: raise `FULL_RETRY_HORIZON_DAYS` from 95 to a value that
covers every candidate date through `term_dates.LAST_KNOWN_DATE`, and
document it as a deliberate "no known TPE horizon within the school year"
working assumption rather than a measurement.**

The evidence, stated with its provenance so nobody later mistakes it for
a measured number:

- The 94-day figure behind the current 95 is a measurement of **NRE's**
  fare-release window (plan 003 §1.1, three consistent runs). It is not
  evidence about TPE at all.
- **The user's direct domain knowledge** (stated 2026-09-01, during this
  migration) is that TPE's booking engine sells **many more months** ahead
  than NRE's ~94-day window. That is the only evidence about TPE's
  horizon that currently exists — it is a person's knowledge of the
  retailer, **not** a live measurement from this repo, and the comment in
  `src/main.py` must say so in those terms.
- Nobody has measured TPE's actual horizon. Inventing a precise new
  number (e.g. "180") would dress up a guess as a finding.

So the honest move is not a guessed horizon but an explicit **suspension**
of the speculative-attempt optimisation until it can be measured. Set the
constant comfortably past the furthest candidate any run can produce:
`LAST_KNOWN_DATE` is currently Thu 8 Jul 2027, ~310 days from
2026-09-01, so **`FULL_RETRY_HORIZON_DAYS = 400`** puts every candidate
date, on every run day of this school year, inside the full 3-attempt
budget. (400, not a computed expression: the constant must stay a plain
integer a human can change, and it must not silently drift when
`LAST_KNOWN_DATE` is updated for a new school year — §9's verification is
what will replace it.)

Why widening is safe, and why keeping 95 is not:

- Keeping 95 would mean **every candidate more than ~3 months out gets
  one attempt instead of three**, on dates TPE can very likely price
  perfectly well. Per plan 002 §4.3 a single attempt still fetches, logs
  and can alert — but a transient flake on that one attempt loses the
  observation until the next run. Applying NRE's tighter number to a
  retailer believed to sell much further ahead is exactly the silent,
  quiet-failure direction plan 003 §4.1 warned about.
- Widening costs **wall-clock time only, and only if TPE's real horizon
  turns out to be closer than believed**: doomed far-out dates would get
  3 attempts instead of 1. That cost is capped by
  `MAX_CONSECUTIVE_FAILURES = 5`, which is untouched and remains the
  reactive backstop — five consecutive failures still latch the early
  stop and abandon the rest of the run's dispatch (plan 003 §4.3). This
  is precisely the scenario that mechanism was built for.
- The constant still affects **retry budget only**, never which dates are
  candidates (plan 003 §5.1/§5.2). Nothing about coverage changes in
  either direction.

Required action for the coder: replace the comment block above the
constant in `src/main.py` (currently lines ~37-53, all NRE provenance)
with one that says plainly (a) 94 days was an NRE measurement and does
not transfer, (b) TPE is understood — from the user's own knowledge of
the retailer, not from a measurement made by this repo — to sell many
months further ahead, (c) 400 is therefore a deliberately generous
placeholder chosen so no candidate date this school year is demoted to
`SPECULATIVE_ATTEMPTS`, (d) `MAX_CONSECUTIVE_FAILURES` is what bounds the
cost if that turns out wrong, and (e) §9's first full live run is what
should replace it with a real number. Do **not** delete
`SPECULATIVE_ATTEMPTS` or the speculative-zone code path — it is
correct machinery that is simply dormant while the horizon is unknown,
and re-arming it is then a one-constant change.

The other NRE-flavoured comments in `src/main.py` need the same
treatment: line ~31 ("NRE has no bot protection to trip"), line ~63, and
`_log_stopped_early`'s message at line ~195, which names NRE twice —
reword to TPE, and to "TPE's fare-release window has moved closer than
FULL_RETRY_HORIZON_DAYS assumes, or TPE is unavailable".

**Update, day of merge (2026-09-01):** the 400-day placeholder above has
been revised down to **`FULL_RETRY_HORIZON_DAYS = 168`** (24 weeks). The
captured TPE fixture shows every fare object's `"setter"` field pointing
at `/data/tocs/GW` — i.e. Great Western Railway, not TPE, is the train
operating company actually setting fares on this Oxford → Paddington
route. Per the user's own domain knowledge of GWR (not a measurement
this repo has made), GWR releases weekday advance tickets up to 24
weeks (168 days) ahead. 168 is comfortably inside the candidate range
this school year can produce, so — unlike 400 — it reactivates rather
than suspends the speculative-attempt/boundary-priority machinery
described above.

### 7.2 Per-date wall-clock cost under Camoufox is unmeasured

Everything in plans 002/003 about per-attempt cost (~12s), the 51s
three-attempt failure, and the 1m50s-2m projection is Chromium-derived.
Firefox + `humanize=True` + a fresh browser per attempt will differ, very
likely upward — and §7.1 removes the speculative single-attempt zone that
used to shorten the tail. Handled provisionally above: §4.2 item 8
(timeouts), §5.2 (`timeout-minutes: 30`).

`PARALLEL_DATES` is currently **8** in `src/main.py` (CLAUDE.md still says
5 — see §8, a stale doc that predates this migration and should be
corrected while editing). Eight concurrent Firefox instances on a 2-core
GitHub runner is a genuinely different memory/CPU proposition from eight
Chromium ones. **Do not change it as part of this migration** — it is a
one-constant tune, and changing it at the same time as the retailer would
make the first live run's timings uninterpretable. Flag it as the first
thing to revisit if the first full run is slow or shows a spike of
otherwise-inexplicable timeouts.

### 7.3 The 3-journey window (§1.4) is an empirical workaround

`ANCHOR_OFFSET_MINUTES = 5` is validated for exactly one date/timetable.
Verification (§9) must confirm both targets appear; the standing signal
that it has stopped working is a run in which `07:30` is consistently
missing from the results while `07:25` prices fine.

---

## 8. `CLAUDE.md` edits

Precisely these, and nothing else — the term dates, "Which dates get
checked" (except the horizon sentence), booked dates, email/Resend,
secrets and scheduling sections are untouched by this migration.

1. **"What this project does", first line** — "Checks National Rail
   Enquiries" → TransPennine Express's booking engine. The rest of the
   paragraph (threshold, railcard, term-time gating) is unchanged.
2. **Constraints, bullet 1** — "NRE's dynamic (client-rendered) journey
   planner" → TPE's journeys-grid, still deep-link-driven, not
   form-filled.
3. **Tech decisions → Language** — `requirements.txt` pins
   `camoufox[geoip]` (which brings Playwright with it), `requests`,
   `pytest`. The `Decimal` / `ZoneInfo` sentences stay verbatim.
4. **Tech decisions → Retailer** — replace the NRE bullet with TPE:
   `ticket.tpexpress.co.uk`, no bot protection observed across the
   Camoufox probe and fixture-capture runs (runs 33525860120 /
   33527007099, 2026-09-01), citing this plan §1.5 rather than 001's
   NRE-era section list. Keep a one-sentence historical note that NRE was
   the previous source and Trainline was rejected for DataDome, pointing
   at plan 001 — do not delete that history, just stop presenting it as
   current.
5. **Tech decisions → Scraping approach** — full rewrite: Camoufox
   (headless, `humanize=True`, `NewContext` with `en-GB`/`Europe/London`)
   rather than plain Playwright Chromium; the TPE deep-link template with
   ISO date and `YNGx1` path railcard; anchor offset of
   `ANCHOR_OFFSET_MINUTES` because TPE returns only the next 3 journeys
   (§1.4); prices read from the same-origin **POST** to
   `api.tpexpress.co.uk/jp/journey-plan`, matched on exact path because
   `/jp/plusbus` shares the host; **no DOM fallback** (§4.2 item 9);
   hijack/iframe guards kept as defense in depth against
   `TPE_HOST_SUFFIX`. Remove the Booking.com-redirect anecdote from the
   current-state text (it is NRE history; plan 001 keeps it).
6. **Tech decisions → the "use Camoufox for other booking platforms"
   line** — now the actual primary browser, so fold it into the scraping
   bullet instead of leaving it as an aside.
7. **Tech decisions → Concurrency** — several edits:
   - "each its own headless Chromium" → Camoufox.
   - State `PARALLEL_DATES`'s **real** value (8, §7.2), not the stale 5.
   - Rewrite the `FULL_RETRY_HORIZON_DAYS` sentences entirely. They
     currently assert "95 days (NRE's observed fare-release horizon,
     measured at 94 days three times)". Replace with the §7.1 position,
     in CLAUDE.md's existing evidence-honesty style: the 94-day figure
     was NRE's and does not transfer; TPE is understood **from the user's
     own knowledge of the retailer — not from any measurement this repo
     has made** — to release fares many months further out; the constant
     is therefore set to a deliberately generous 400 so that no candidate
     date within the current school year is demoted to a single attempt;
     the speculative-attempt path still exists and re-arms as soon as a
     real TPE horizon is measured; and `MAX_CONSECUTIVE_FAILURES` bounds
     the cost of that assumption being wrong.
   - Mark the per-attempt timing numbers (page budget 20s, navigation
     timeout 60s) as updated and **provisional**, citing this plan §4.2
     item 8 alongside the existing 002/003 references.
8. **"Which dates get checked", final paragraph** — it currently ends
   with "NRE's fare-release horizon is observed, not guaranteed — 94 days
   as of the 2026-08-31/09-01 measurements ... expected to drift at NRE's
   December/May timetable changes." Replace with the TPE position from
   §7.1: no TPE horizon has been measured; it is believed to be many
   months out; the working assumption until measured is that every
   candidate date in the school year is priceable, and §9's first full
   run is what will replace belief with a number. Also drop or requalify
   the sentence about `FULL_RETRY_HORIZON_DAYS` cutting run time, since
   the single-attempt zone is now dormant.
9. **Route details** — unchanged except the railcard-confirmation bullet,
   which should note that TPE returns one already-discounted price per
   fare, so `railcard_applied` is now determined by the winning fare's
   `tickets[].railcard` ref rather than NRE's separate `railcardFares`
   array. The alerting rule itself does not change.
10. **Plans list** — add `docs/plans/005-migrate-to-tpe.md` as the
    migration's research/evidence, and note that 001-004's NRE specifics
    are now historical.

Also update **`README.md`**: line 3 ("Checks National Rail Enquiries"),
line 14 ("bookable National Rail" link — the email now links to TPE),
line ~194 (the `BlockedError`/`HijackedError` operational note naming
NRE), and the "Running locally" block's `playwright install chromium` →
`python -m camoufox fetch`. `site/app.js` was checked and contains no
retailer reference.

---

## 9. Verification after merge

1. `python -m pytest` green locally and in `test.yml`, with no browser
   fetched — proves the lazy-import property survived.
2. Trigger `price-check.yml` via `workflow_dispatch` (which is a full,
   real run — no `max_dates` input exists any more) and check the log for:
   - `dismissed cookie banner` on essentially every date;
   - both `07:25` and `07:30` present in the per-date summary lines
     (§7.3);
   - realistic prices, and `railcard_applied` true on Advance fares;
   - **the last travel date returning real prices** — this is the TPE
     fare-release horizon measurement §7.1 is waiting for. Two outcomes:
     if every candidate through `LAST_KNOWN_DATE` prices, the horizon is
     beyond the school year and 400 can stay (record that finding); if
     there is a clean boundary, record it and open a follow-up to set
     `FULL_RETRY_HORIZON_DAYS` from the measured value, one day of margin
     above it, exactly the way plan 003 §4.1 did for NRE.
   - whether `MAX_CONSECUTIVE_FAILURES` fired — with the widened horizon
     this is now the main defence against a far-out failure zone, so a
     `stopping early` line is expected and healthy if TPE's real horizon
     is closer than believed. It is also the signal to re-measure.
   - per-attempt and total wall-clock timings, for §7.2 — then re-tighten
     `NAVIGATION_TIMEOUT_SECONDS` / `PAGE_BUDGET_SECONDS` /
     `timeout-minutes` from measured values in a follow-up, exactly the
     way plan 002 did for Chromium.
   - no `BlockedError` / `HijackedError` anywhere. Either one on TPE is
     new information and should be investigated from the uploaded
     artifacts before tuning anything else.
3. Confirm `price-history.csv` gained rows in ascending travel-date order
   and that the run's email rendered with working per-fare TPE links.
