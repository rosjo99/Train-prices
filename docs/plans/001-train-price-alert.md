# Plan 001 — Train Price Alert Tool

Status: approved for implementation
Author: planner
Date: 2026-08-31

Implements the project described in `CLAUDE.md`: check National Rail
Enquiries for the 07:25 and 07:30 Oxford → London Paddington departures
(one-way, 16-25 railcard) and email an alert when either fare drops below
GBP 10.00, only for travel dates that are a Tuesday/Thursday/Friday
inside school term time.

**Revision note (2026-08-31):** this plan originally targeted Trainline.
§1.1-1.3 below are that investigation, kept for the record since they're
why NRE was tried at all. §1.4 documents the actual pivot and what was
confirmed against the live NRE site before any production code was
written, per an explicit standing instruction not to proceed until real
NRE fare data — including the railcard discount — was definitively
confirmed. Everything from §2 onward describes the current, NRE-based
design.

---

## 1. Research findings (why the tech decisions are what they are)

### 1.1 The prior script's approach does not work

The deleted `train_price_lookup.py` (recoverable via
`git show HEAD~1:train_price_lookup.py`) used plain `requests` against
three endpoints. All three were probed live on 2026-08-31:

| Endpoint | Method | Result |
| --- | --- | --- |
| `www.thetrainline.com/api/locations-service/v2/search?searchTerm=oxford` | GET | **HTTP 404**, body `[]` — endpoint shape no longer exists |
| `www.thetrainline.com/api/journey-search/` | POST | **HTTP 403** + DataDome CAPTCHA redirect (`geo.captcha-delivery.com/captcha/?...`) |
| `www.trainline.eu/api/v5_1/search` | POST | **HTTP 403** + DataDome CAPTCHA redirect |
| `www.trainline.eu/api/v5/stations?term=oxford` | GET | HTTP 200, but **ignores the search term** — returns Paris, London, Lyon, Lille… The old script's `stations[0]` would have resolved "oxford" to **Paris**. |

The homepage HTML contains DataDome bootstrap script (`pushDataDomeEvent`,
multiple `datadome` references), and the `/book/results` page HTML shell
contains ~17 `datadome` and ~6 `captcha` references.

Conclusion: **Trainline is behind DataDome bot protection.** Every
price-bearing endpoint returns a 403 CAPTCHA challenge to a plain HTTP
client. Raw `requests` scraping is not viable and cannot be made viable by
adding headers — DataDome fingerprints TLS/JA3, header ordering, and
requires a browser-executed JS challenge to mint the `datadome` cookie.

### 1.2 What looked like it would work, and didn't

`GET https://www.thetrainline.com/book/results?...` returns HTTP 200 with
the full ~750KB JS application shell. Prices are **not** in that HTML;
the page fetches them client-side from `/api/journey-search/` *after* the
DataDome JS challenge has run and set the clearance cookie. The plan was
to drive a real Chromium via Playwright, let the page solve DataDome's
passive challenge itself, and read the prices out of that XHR response.

This did not survive contact with real GitHub Actions runs: **two
independent live runs, on two different dates, were both blocked
immediately** by DataDome. Confirms §1.3's risk was not hypothetical.

### 1.3 Principal risk (as assessed before it materialised)

DataDome scores IP reputation. **GitHub-hosted runners use Azure
datacentre IP ranges, which are commonly flagged.** Mitigations
considered, in order: a realistic browser context + human-like pacing;
retry with exponential backoff; detecting the block explicitly and
failing loudly; and, if persistently blocked, a self-hosted runner on a
home machine (residential IP) as Plan B.

Per standing instruction, CAPTCHA-solving services, proxy rotation, or
other evasion escalation were never attempted. Given the confirmed block
in §1.2, the response was not to invoke Plan B but to look for a
different retailer first (§1.4) — a self-hosted runner is a bigger
operational commitment than switching retailers, and was not yet
justified.

### 1.4 The pivot to National Rail Enquiries

Two alternatives were evaluated and rejected before landing on NRE:
`traintimes.org.uk` has no fares at all (timetable-only) and its page
content included a prompt-injection attempt, which was identified and
not acted on; GWR/RailEasy/TrainSplit were found to sit behind their own
JS-challenge/CAPTCHA products (AWS WAF, hCaptcha/Turnstile) on initial
inspection.

**National Rail Enquiries (`nationalrail.co.uk`) has no bot protection
at all** — confirmed over more than 20 live probe runs from GitHub-hosted
runners (`scripts/probe_nre.py`, `scripts/probe_nre_deeplink.py`; not
part of the production tool): zero CAPTCHA/DataDome/Cloudflare-challenge
markers on any run, and one run's own Lucky Orange analytics beacon
explicitly reported `is_bot: false`.

Getting from "not blocked" to "real fare data, with the railcard discount
confirmed" took two more rounds of investigation, both driven by
iterating against the real site rather than assumption:

**Round 1 — interactive UI driving, and why it was abandoned.** The
first approach filled in NRE's journey-planner form (origin/destination
autocomplete, date, railcard selects, a "find hotels" checkbox) and
clicked its submit button, the same shape as the original Trainline
design. This surfaced a reproducible failure: submitting reliably
redirected the entire tab to a Booking.com hotel search
(`booking.com/searchresults.html?...&label=nre_journey_planner`),
destroying the page before any train results rendered. Multiple
JS-level mitigations were tried and empirically ruled out — neutralising
`window.open`, overriding `location.assign`/`.replace`, attempting to
override the `location.href` setter (browsers make this
non-configurable by design) — none stopped it, and a route-level network
backstop only replaced the destination's *content*, it didn't prevent
the tab navigating away from NRE. The eventual root cause, found by
inspecting screenshots rather than guessing: the interactive flow was
searching **today's date**, with both target departures (07:25/07:30)
already hours in the past by the time the scheduled job would run — NRE's
own "no valid journeys" handling for that case appears to substitute a
hotel search, independent of the "find hotels" checkbox's state (it still
fired with that checkbox confirmed unchecked). This was never fully
re-verified in the interactive flow because Round 2 made it moot.

**Round 2 — a deep-link URL, which is what's implemented.** NRE's
journey-planner accepts a fully-parameterised query string that skips
form-filling entirely:
```
https://www.nationalrail.co.uk/journey-planner/?type=single
  &origin=OXF&destination=PAD&leavingType=departing
  &leavingDate=DDMMYY&leavingHour=HH&leavingMin=MM
  &adults=1&railcards=YNG%7C1&extraTime=0
```
(`OXF`/`PAD` are NRE's own CRS station codes; `YNG` is the 16-25 Railcard
code — the same value originally hypothesised for Trainline, confirmed
correct here too, coincidentally.) Navigating straight to this URL with
tomorrow's date and the 07:25 target time loaded directly into real
results — **no click needed, no redirect, no hijack, in every probe
run** — showing "07:25 journey from Oxford to London Paddington" and
"07:30 journey from Oxford to London Paddington" (both appear because the
results list continues past the anchor time), each priced "Single from
£30.60" in the rendered page text.

Confirming the railcard discount specifically (CLAUDE.md: no alert
without positive confirmation) required looking past the rendered text to
the underlying data: the page makes a same-origin XHR to
`jpservices.nationalrail.co.uk/journey-planner` returning structured JSON
where each fare option carries a `railcardFares` array distinct from its
`undiscountedPrices`, e.g.:
```json
{
  "totalPrice": 3095,
  "undiscountedPrices": {"adult": 4650, "child": 2325},
  "railcardFares": [
    {"code": "YNG", "count": 1, "prices": {"adult": 3060, "child": 0}}
  ]
}
```
`code: "YNG"` matching the railcard passed in the URL, with its own
distinct (lower) price, is the positive, structured confirmation the
project requires — a real amount, not an inference from the presence of
the query parameter alone.

This is the confirmation the standing "don't move on" instruction was
gated on, and is why §2 onward below describes an NRE-based design
rather than the Trainline one originally planned.

---

## 2. Tech decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Language | Python 3.12 | Prior work was Python; Playwright has a first-class sync Python API; `zoneinfo` and `Decimal` are stdlib. |
| Retailer | National Rail Enquiries, not Trainline | Trainline is DataDome-blocked, confirmed by two live blocked runs (§1.1-1.2). NRE has no bot protection, confirmed by 20+ live runs (§1.4). |
| Scraping | Playwright (sync API) + Chromium, navigating straight to a deep-link URL (no form-filling) and reading the intercepted `jpservices.nationalrail.co.uk/journey-planner` JSON response; DOM scrape as fallback | The deep-link approach avoids the ad-hijack behaviour hit when interactively driving the form (§1.4). JSON is more stable than DOM text and carries a structured, positively-confirmable railcard discount. |
| Dependency mgmt | `requirements.txt` + pip | One file, no extra tooling in CI. |
| Money handling | `decimal.Decimal`, never `float` | `9.99` vs `9.989999…` must not decide whether an email is sent. |
| Time handling | `zoneinfo.ZoneInfo("Europe/London")` everywhere | Runners are UTC; a 23:30 UTC run is already "tomorrow" in London for part of the year. |
| Term dates | Python module `src/term_dates.py` with ISO date strings + comments + a `--list` CLI | A human edits one file per school year. Rejected YAML (extra dependency) and JSON (no comments — and the comments are what tell the human which range is a half term). Python gives comments *and* import-time validation. |
| Hosting/scheduling | GitHub Actions, `schedule` cron + `workflow_dispatch` | Free, no server. Cron is best-effort so **all gating lives in Python**, never in the cron expression. |
| Email | Resend free tier (HTTPS API, `POST https://api.resend.com/emails`) | 3,000/mo free, single Bearer-token call, no SMTP/OAuth/app-password rotation. The free `onboarding@resend.dev` sender may only send to the account owner's own address — which is exactly our recipient, so no domain verification is needed. |
| Secrets | GitHub Actions repository secrets → step `env:` | Never in the repo, never logged. |

### 2.1 Alert semantics (decided, do not re-litigate)

- Threshold fires on `price < Decimal("10.00")`. **Exactly £10.00 does
  not fire** ("below GBP 10").
- Prices compared are the cheapest one-way fare found for the journey —
  searched with a 16-25 railcard applied, but not required to be
  confirmed as the railcard-discounted price (see the revision note
  immediately below).

#### Revision: railcard confirmation no longer gates alerting

The original decision here was: if the railcard's application couldn't
be positively confirmed in the response, no alert is sent and the run
fails loudly (exit 1) — "a wrong price in an alert is worse than a
missed alert."

The user later clarified that this wasn't actually what they wanted.
Their concern was narrower: don't send an email unless there's a real,
unbooked fare below £10 (railcard-discounted or not) — not "never send
an email whose price wasn't confirmed as railcard-discounted." Asked
directly which behaviour to keep, the user chose: alert on any
sub-threshold fare, treated the same whether or not the railcard
discount is confirmed.

So as of this revision: `evaluate()` (Task 6) alerts on any option with
`price is not None`, `currency == "GBP"`, and `price < threshold` —
`railcard_applied` no longer participates in that decision at all. It's
still computed and carried through as informational metadata (shown in
the email and logged to `price-history.csv`), because it's still useful
context for a human deciding whether to book, but a `False` value no
longer suppresses the run or the email. There is no longer a
`railcard_unconfirmed` flag or a "no email if any priced option's
discount is unconfirmed" run-wide safety check — see Task 6 and Task 7's
`_best_effort_matches_for_test`.

### 2.2 Which travel dates get checked (decided)

`CLAUDE.md` says the tool runs daily and targets Tue/Thu/Fri term-time
dates. Those are **travel dates**, not run dates — alerting on the
morning of a 07:25 departure is useless.

Decision (per explicit user instruction, superseding this plan's
original 14-day-horizon draft): each run enumerates **every** candidate
travel date from **tomorrow** through the **end of the last known school
term** (`term_dates.LAST_KNOWN_DATE`, i.e. `max(t.end for t in TERMS)` —
currently 2027-07-08), keeps those passing `is_checkable_day()`, and
checks every one of them, every day.

This also subsumes the "running on a non-term / non-Tue-Thu-Fri day"
edge case: during the summer holidays the candidate list is empty and the
run is a clean no-op (log + exit 0, no email, no browser launched).

**Volume implication, flagged for visibility rather than left implicit:**
checked live against the actual term dates (see §1.5 below), a run on
the first day of Autumn Term 2026 has **102 candidate dates** to check
(and ~39 within just that one term). At an estimated 15-25s per date
(browser navigation + a randomised inter-request pause), a full run
early in the year takes on the order of 30-45 minutes and makes 100+
sequential automated requests to National Rail Enquiries from the same
IP, once a day, every day. Unlike the abandoned Trainline design, this
is not a known risk: NRE has no bot protection to trip (§1.4), so this
volume mainly costs wall-clock run time (sized into Task 7's workflow
timeout) rather than IP reputation. The user has explicitly chosen this
tradeoff (fresh data on every remaining date, checked daily) over the
lighter-weight alternative it superseded; it is documented here for
visibility, not as an accepted risk.

### 2.3 Excluding already-booked dates (decided)

Once the user books a ticket for a date, that date should stop being
checked — both to cut the daily workload down from §2.2's ~100 dates and
because an alert about a fare on a date already booked is noise.

Decision: a plain text file at the **repo root**, `booked-dates.txt`,
one `YYYY-MM-DD` per line, `#` for comments, blank lines ignored. This
is the "no coding involved" mechanism the user asked for: editing it is
a matter of opening the file on github.com, clicking the pencil (edit)
icon, adding one line, and committing directly from the browser — no
local checkout, no Python, no PR review needed for a personal repo. The
next scheduled run picks it up automatically since it reads the file
fresh from the checked-out repo on every run.

Rejected alternatives: a repo secret or Actions variable (edits require
the Settings UI, worse UX, secrets aren't meant to be listed items,
awkward to view what's already there at a glance); a GitHub Issue label
per date (over-engineered — parsing issue titles/labels is more moving
parts for the same result); a `workflow_dispatch` input (does not
persist across scheduled runs — it would have to be re-entered every
time). A committed text file is the simplest thing a non-programmer can
maintain and is fully version-controlled for free.

`src/booked_dates.py`:
- `load_booked_dates(path: Path) -> set[date]` — reads the file if it
  exists; returns `set()` if the file is missing entirely (first-time
  setup, or the user simply hasn't booked anything yet, must not be an
  error). Skips blank lines and lines starting with `#`. Parses the rest
  with `date.fromisoformat`; a line that fails to parse is **logged as a
  warning and skipped**, not fatal — one typo in a hand-edited file must
  not take down the whole day's price check.
- No pruning of past dates is needed or implemented: `main()` only ever
  considers candidates from tomorrow onward (§2.2), so a booked date
  that has already passed simply never matches anything and is harmless
  clutter. The README should mention it's fine to leave old lines or
  delete them, purely for tidiness.

`config.BOOKED_DATES_PATH = Path("booked-dates.txt")`, resolved relative
to the process's working directory (the repo root, both locally and in
the Actions job).

`main()` filters candidates through this set before scraping anything
(see Task 6, updated below) — a booked date costs zero requests, not
just zero alerts.

### 1.5 Checkable-date counts (computed, not estimated)

Computed directly from the `CLAUDE.md` term data with the same weekday
and exclusion rules `term_dates.py` implements:

| From | Checkable dates remaining |
| --- | --- |
| 2026-09-01 (whole year) | 102 |
| Just Autumn Term 2026 | 39 |

These numbers should be re-verified with
`python -m src.term_dates --list \| wc -l` once Task 2 lands, and used
to size the workflow timeout in Task 7.

---

## 3. Repository layout

```
.github/workflows/price-check.yml   # cron + manual dispatch
requirements.txt
README.md                           # setup, secrets, how to update term dates
booked-dates.txt                    # user-edited, no-code: dates already booked
.gitignore
src/__init__.py
src/config.py                       # env + constants (threshold, target times, URNs)
src/term_dates.py                   # term data, is_checkable_day(), CLI
src/booked_dates.py                 # reads booked-dates.txt
src/models.py                       # TrainOption dataclass
src/scraper.py                      # Playwright → raw journey-search JSON
src/parser.py                       # raw JSON → list[TrainOption]
src/notifier.py                     # Resend email
src/main.py                         # orchestration, exit codes
scripts/capture_fixture.py          # dev tool: save a real response as a fixture
tests/test_term_dates.py
tests/test_booked_dates.py
tests/test_parser.py
tests/test_notifier.py
tests/test_main.py
tests/fixtures/journey_search_*.json
```

---

## 4. Task specs

Seven tasks, strictly sequential (Task 4 depends on the fixture produced
by Task 3). Each is sized for one `coder` session.

---

### Task 1 — Scaffolding and configuration

**Create:** `requirements.txt`, `.gitignore`, `src/__init__.py`,
`src/config.py`, `src/models.py`, `pytest.ini` (or `[tool:pytest]` in
`setup.cfg`), `tests/__init__.py`

**What the code does**

`requirements.txt`:
```
playwright==1.55.0
requests==2.32.3
pytest==8.3.3
```
(Pin whatever the latest stable versions resolve to at implementation
time; pin exactly, no ranges.)

`.gitignore`: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.venv/`,
`artifacts/`, `.env`.

`src/config.py` — module-level constants and an env-reading function
(**superseded by Task 3, see its "Modified" note — this is the original
scaffold spec, kept for history; the fields below named `_URN`/
`PASSENGER_DOB` no longer exist in the current `src/config.py`, replaced
by `ORIGIN_CRS`/`DESTINATION_CRS`/`JOURNEY_PLANNER_URL_TEMPLATE`, which
need no DOB**):
- `ORIGIN_NAME = "Oxford"`, `DESTINATION_NAME = "London Paddington"`
- `ORIGIN_URN` / `DESTINATION_URN` — leave as `None` with a `TODO(task-3)`
  comment; Task 3 fills them in with discovered values.
- `TARGET_DEPARTURES: tuple[str, ...] = ("07:25", "07:30")`
- `PRICE_THRESHOLD = Decimal("10.00")`
- `RAILCARD_CODE = "YNG"` with a comment marking it a hypothesis for
  Task 3 to confirm.
- `PASSENGER_DOB = "2003-01-01"` — a date of birth that is inside the
  16-25 eligibility window as of the target travel dates. Add a comment
  that this needs revisiting if the tool is still running past 2028.
- `LONDON = ZoneInfo("Europe/London")`
- `BOOKED_DATES_PATH = Path("booked-dates.txt")` — read by Task 6's
  `booked_dates.load_booked_dates()`; not env-configurable, it's a
  committed file at a fixed repo-relative path.
- `MAX_DATES` from env `MAX_DATES`, optional, default `None` (no cap) —
  when set, must parse as a positive int; used only to cap the candidate
  list for manual/debug `workflow_dispatch` runs, never by the scheduled
  cron run. Raise `ConfigError` if set but not a positive int.
- `DRY_RUN` from env `DRY_RUN` — truthy values `1/true/yes` (case
  insensitive). **Removed in Task 7's revision** — replaced by
  `SKIP_TIME_GATE`/`TEST_RUN` (see Task 7's "One test, not a grid of
  toggles"); there is no print-instead-of-send mode in the current
  code.
- `get_secrets()` returning a frozen dataclass with `resend_api_key`,
  `email_to`, `email_from` (from `RESEND_API_KEY`, `ALERT_EMAIL_TO`,
  `ALERT_EMAIL_FROM` defaulting to `"Train Alerts <onboarding@resend.dev>"`).
  Raise `ConfigError` listing **all** missing names at once. **Never
  include a secret value in any exception message or log line.**
- `ConfigError(Exception)`.

`src/models.py` — frozen dataclasses:
- `TrainOption(travel_date: date, departure_time: str, arrival_time: str | None, price: Decimal | None, currency: str, railcard_applied: bool, is_direct: bool, sold_out: bool, fare_name: str | None)`
- `CheckResult(travel_date: date, options: list[TrainOption], error: str | None)`

**Acceptance criteria**
- `python -c "import src.config, src.models"` succeeds with no env vars set (import must not require secrets).
- `pytest` runs and collects 0 tests without error.
- `get_secrets()` with none set raises `ConfigError` naming all three vars.
- `MAX_DATES=0` and `MAX_DATES=abc` both raise `ConfigError`; `MAX_DATES` unset → `None`.

**Edge cases**
- Import-time must never read secrets — only `get_secrets()` does.
- Empty-string env vars are treated as unset.

---

### Task 2 — Term-date logic

**Create:** `src/term_dates.py`, `tests/test_term_dates.py`

**What the code does**

Top of the file: a clearly-delimited, comment-annotated data block
transcribing the term dates from `CLAUDE.md`, with a header comment
explaining exactly how a human updates it each school year (add a new
`Term(...)`, delete finished ones, dates are inclusive, ISO format).

```python
@dataclass(frozen=True)
class Term:
    name: str
    start: date          # inclusive
    end: date            # inclusive
    excluded_ranges: tuple[tuple[date, date], ...] = ()   # inclusive both ends
    excluded_days: tuple[date, ...] = ()
```

`TERMS` must contain exactly, from `CLAUDE.md`:
- **Autumn 2026**: 2026-09-01 → 2026-12-16; excluded range
  2026-10-19 → 2026-10-30 (half term); excluded day 2026-11-20
  (occasional day).
- **Spring 2027**: 2027-01-06 → 2027-03-25; excluded range
  2027-02-15 → 2027-02-19 (half term).
- **Summer 2027**: 2027-04-19 → 2027-07-08; excluded range
  2027-05-31 → 2027-06-04 (half term); excluded day 2027-05-03
  (bank holiday — a Monday, kept for completeness).

Functions:
- `CHECK_WEEKDAYS = frozenset({1, 3, 4})  # Mon=0 … Tue=1, Thu=3, Fri=4`
- `term_for(d: date) -> Term | None` — the term whose active range
  contains `d`, ignoring exclusions.
- `is_in_term(d: date) -> bool` — inside a term range and not inside any
  of that term's exclusions.
- `is_checkable_day(d: date) -> bool` — `d.weekday() in CHECK_WEEKDAYS and is_in_term(d)`.
- `checkable_dates(start: date, end: date) -> list[date]` — inclusive
  both ends, ascending; returns `[]` if `end < start`.
- `LAST_KNOWN_DATE: date = max(t.end for t in TERMS)` — module-level
  constant, the end of the last term currently in `TERMS`. This is what
  the orchestrator (Task 6) uses as the upper bound when it checks every
  remaining date to the end of the school year; it advances automatically
  as new terms are added to `TERMS` each year, with no other code change
  needed.
- `_validate()` run at import: every term has `start <= end`; every
  excluded range is inside its term and has `start <= end`; terms do not
  overlap each other. Raise `ValueError` naming the offending term.
- `if __name__ == "__main__":` CLI — `python -m src.term_dates --list`
  prints every checkable date grouped by term with weekday names, and
  `--check YYYY-MM-DD` prints a yes/no plus the reason
  (e.g. "no: Saturday", "no: half term (Autumn Term 2026)",
  "no: outside all terms"). This is the human's verification tool after
  editing the data block; document it in the file header.

**Acceptance criteria** (tests must assert all of these explicitly)
- Fri 2026-09-04 (term begins) → True.
- Tue 2026-09-01 (INSET, first day of active range) → True — INSET days count.
- Wed 2026-12-16 (last day, a Wednesday) → False (weekday).
- Tue 2026-12-15 → True.
- Thu 2026-10-22 (inside autumn half term) → False.
- Fri 2026-11-20 (occasional day) → False.
- Thu 2026-11-19 → True.
- Any Mon/Wed/Sat/Sun in term → False.
- Tue 2026-12-22, Thu 2027-08-05 (holidays) → False.
- Thu 2027-01-07 (INSET) → True; Tue 2027-01-05 (day before term) → False.
- Thu 2027-02-18 (spring half term) → False.
- Mon 2027-04-19 (INSET, Monday) → False (weekday, not term).
- Tue 2027-04-20 → True.
- Thu 2027-07-08 (last day) → True; Fri 2027-07-09 → False.
- Tue 2027-06-01 (summer half term) → False.
- `checkable_dates(date(2026,10,15), date(2026,11,5))` skips the whole
  half-term block.
- Boundary dates of every excluded range are themselves excluded
  (inclusive on both ends) — test 2026-10-19 and 2026-10-30 directly.
- `checkable_dates` with `end < start` returns `[]`.
- `LAST_KNOWN_DATE == date(2027, 7, 8)` (the Summer 2027 term end).
- `python -m src.term_dates --list` exits 0.

**Edge cases**
- Exclusion ranges are inclusive at both ends — off-by-one here silently
  causes a wasted or missed check.
- Dates outside every term must return `False`, not raise.
- Pure functions only: no `date.today()` inside this module.

---

### Task 3 — Playwright scraper + fixture capture

**Status: implemented**, against National Rail Enquiries rather than the
Trainline design this task originally specified — see §1.4 for the full
discovery story (two Trainline live-run blocks, an abandoned interactive
NRE approach that hit an ad-hijack redirect, and the deep-link approach
that replaced it). This section now documents what was actually built.

**Created:** `src/scraper.py`
**Modified:** `src/config.py` — `ORIGIN_CRS`/`DESTINATION_CRS`/
`RAILCARD_CODE`/`JOURNEY_PLANNER_URL_TEMPLATE`/`JOURNEY_PLANNER_API_HOST`/
`NRE_HOST_SUFFIX`, each with a comment recording how and when it was
confirmed (2026-08-31, via `scripts/probe_nre_deeplink.py`).
`scripts/capture_fixture.py` was run against a real date
(2026-09-08, a term-time Tuesday) via the new `capture-fixture.yml`
workflow and its output committed as
`tests/fixtures/journey_search_sample.json` — confirms 10 outward
journeys including both target departures (`07:25`→`08:26` and
`07:30`→`08:25`), each fare option's `railcardFares` array containing a
`code: "YNG"` entry with its own discounted `prices.adult` (e.g. one
Advance Single: `totalPrice: 3060`, `undiscountedPrices.adult: 4600`,
`railcardFares: [{"code": "YNG", "prices": {"adult": 3060}}]` — pence
throughout). Scanned for session-identifying fields (tokens, cookies,
customer/booking IDs) before committing — none found; the only
per-request identifier is `searchId` (a UUID), left in as harmless.

**Discovery, for the record**

Confirmed live via `scripts/probe_nre_deeplink.py` (a throwaway probe, not
part of the production tool):
1. The deep-link URL format (station CRS codes, `leavingDate` as
   `DDMMYY`, `leavingHour`/`leavingMin`, `railcards=YNG%7C1`) — see §1.4
   for the full URL and the real result it produced.
2. The exact shape of the `jpservices.nationalrail.co.uk/journey-planner`
   response: a top-level `outwardJourneys` array, each entry with
   `origin`/`destination` (`crsCode`, `name`), `timetable.scheduled.
   {departure,arrival}` as full ISO 8601 timestamps with a UTC offset
   (e.g. `"2026-09-01T07:25:00+01:00"` — **not** a bare `"HH:MM"**), and a
   nested fares structure whose individual fare objects carry
   `totalPrice`/`undiscountedPrices` (integer **pence**, not pounds) and a
   `railcardFares` array of `{code, count, prices: {adult, child}}` — the
   presence of an entry with `code == config.RAILCARD_CODE` and its own
   `prices.adult` is the positive railcard confirmation CLAUDE.md
   requires (§2.1). Task 4 must capture a real fixture and inspect it
   directly for the exact nesting from `outwardJourneys` down to
   `railcardFares` before writing `parse_journeys` — the discovery above
   names the fields, not their exact path in the object graph.

**What `src/scraper.py` does (as built)**

`fetch_journey_search(travel_date: date, *, artifacts_dir: Path | None = None, attempts: int = 3) -> dict`

- Launch Chromium via the **sync** Playwright API, `headless=True`, args
  including `--disable-blink-features=AutomationControlled`,
  `--no-sandbox`.
- `browser.new_context(locale="en-GB", timezone_id="Europe/London",
  viewport={"width":1440,"height":900}, user_agent=<current desktop
  Chrome UA>)`.
- `context.route("**/*", handler)` — registered on the **context**,
  before `new_page()`, so it's active for the very first navigation.
  Blocks any cross-origin (non-`nationalrail.co.uk`) iframe *document*
  load outright, and backstops any main-frame navigation away from
  `nationalrail.co.uk` with a blank `fulfill()` (not `abort()`, which
  leaves a Chrome error page instead of the intended content). Defense in
  depth against the ad-hijack behaviour from §1.4 — never triggered by
  the deep-link approach in any probe run, but cheap insurance for an
  unattended daily job.
- Register `page.on("response", handler)` **before** navigating; the
  handler captures any response whose URL contains
  `config.JOURNEY_PLANNER_API_HOST`, storing `status` and parsed JSON
  body.
- Navigate to `_build_journey_planner_url(travel_date)` — anchored at
  `min(config.TARGET_DEPARTURES)` (07:25) so one fetch's results list
  covers both target departures — `wait_until="domcontentloaded"`.
- Wait for the captured response, or for the results list to render, with
  an overall page budget of 45s.
- Accept/dismiss the cookie banner if present (best-effort, never fatal —
  wrap in try/except and continue). Confirmed selector:
  `#onetrust-accept-btn-handler`.
- Detect a block: the captured XHR status is >= 400, or the page URL/
  content contains a marker like `captcha`/`datadome`/`are you a robot`
  (defense in depth — NRE has shown none of these on 20+ live runs).
  Raise `BlockedError`.
- Detect a hijack, distinct from a block: the current page URL's host is
  no longer under `nationalrail.co.uk` at all. Raise `HijackedError`.
- Retry on `ScraperError`/timeout up to `attempts` times with backoff
  (30s, 90s) and a fresh browser context each time. **Do not retry
  `BlockedError`/`HijackedError` more than once** — hammering either
  makes it worse.
- On final failure, if `artifacts_dir` is set, write
  `screenshot-<date>.png`, `page-<date>.html`, and any captured raw
  response to that directory before raising.
- Return the parsed JSON dict.
- Exceptions: `ScraperError` base, `BlockedError`, `HijackedError`,
  `TimeoutScrapeError`.
- Log every step to stdout with timestamps. **Never log secrets** (this
  module receives none — keep it that way).

`scripts/capture_fixture.py` — CLI wrapper: takes `--date YYYY-MM-DD` and
`--out PATH`, calls `fetch_journey_search`, pretty-prints the JSON to
`--out`. Used to regenerate fixtures if NRE changes its response schema.

Run it once for a real term-time Tue/Thu/Fri date and commit the result
as `tests/fixtures/journey_search_sample.json`. If the response contains
anything session-identifying (tokens, cookies, booking IDs, a real
customer id), redact those fields before committing and note the
redaction in the fixture's sibling `README` line or a `_note` key.

**Acceptance criteria**
- `python scripts/capture_fixture.py --date <a real term Tue/Thu/Fri> --out tests/fixtures/journey_search_sample.json` succeeds and produces a JSON file containing recognisable fares and a 07:25 and a 07:30 departure. **Done** — see above.
- `src/config.py` has real, non-placeholder CRS codes and a confirmed
  `JOURNEY_PLANNER_URL_TEMPLATE`, with a comment recording the date they
  were verified. **Done.**
- A forced-failure path (e.g. pointing at an unroutable URL) writes the
  screenshot/HTML artifacts and raises, rather than returning `{}`. Covered
  by `tests/test_scraper.py::test_final_failure_writes_artifacts_before_raising`.
- `src/scraper.py` imports cleanly without Playwright browsers installed
  (import Playwright lazily inside the function if needed) so unit tests
  for other modules don't need a browser. Covered by
  `tests/test_scraper.py::test_no_top_level_playwright_import` and
  `test_import_succeeds_in_subprocess`.

**Edge cases**
- Cookie/consent banner present or absent — both must work.
- Page renders but the XHR never fires (cached SSR) → fall back to
  reading the results DOM; if that also yields nothing, raise
  `TimeoutScrapeError`, never return empty-but-successful. The DOM
  fallback selector (`RESULTS_DOM_SELECTOR`) is an unverified hypothesis —
  every probe run captured the XHR successfully, so this path has never
  actually been exercised against the real site.
- Bot-protection markers → `BlockedError`, distinct from "no trains
  found". Not observed against NRE in practice; kept as defense in depth.
- Ad-hijack redirect away from `nationalrail.co.uk` → `HijackedError`,
  distinct from both of the above. Not observed via the deep-link
  approach in practice; kept as defense in depth (see §1.4).
- Chromium binary missing → clear, actionable error message naming
  `playwright install chromium`.
- Slow network → hard 45s page budget; the whole function must not exceed
  ~4 minutes including retries.

**If NRE starts blocking Playwright: stop and report.** Do not add
proxies, CAPTCHA solvers, or stealth plugins — this would be new,
unexpected behaviour from a site with no bot protection at any point in
this project's testing, worth investigating before working around.

---

### Task 4 — Response parser

**Status: implemented.**

**Created:** `src/parser.py`, `tests/test_parser.py`,
`tests/fixtures/journey_search_empty.json`,
`journey_search_missing_container.json`,
`journey_search_fareless_journey.json`, `journey_search_no_railcard.json`,
`journey_search_only_0725.json`

One implementation decision beyond the spec below: `sold_out` is defined
as "no fare on this journey carries a `railcardFares` entry for
`config.RAILCARD_CODE`" — the same condition as `railcard_applied is
False` — rather than "the `fares` list is empty". A fare existing without
our railcard's discount is functionally identical to no fare existing at
all for this tool's purposes (no price can ever be positively confirmed
and compared to the threshold), so both collapse to the same state. All
84 tests pass (`python -m pytest -q`), no `float` in the module.

**What the code does**

`parse_journeys(raw: dict, travel_date: date) -> list[TrainOption]`
- Walk `raw["outwardJourneys"]` (confirmed top-level key, see §1.4/Task
  3), producing one `TrainOption` per outbound journey.
- Departure/arrival time as `"HH:MM"` in **Europe/London**: each journey's
  `timetable.scheduled.departure`/`.arrival` is a full ISO 8601 timestamp
  **with its own UTC offset already applied** (e.g.
  `"2026-09-01T07:25:00+01:00"`) — parse with
  `datetime.fromisoformat(...).astimezone(config.LONDON)` and format
  `"%H:%M"`, don't assume the offset is always `+01:00` (it's `+00:00`
  outside BST).
- Price: the 16-25-railcard fare specifically, not the cheapest fare
  overall — find the fare option (whatever nested path Task 3's real
  fixture shows fares live under) whose `railcardFares` array contains an
  entry with `code == config.RAILCARD_CODE`, and use *that entry's*
  `prices.adult` (integer pence — confirmed from the real response, see
  Task 3), as `Decimal`, built by dividing the pence integer by
  `Decimal("100")`, never via `float`.
- `railcard_applied`: True only when such a `railcardFares` entry exists
  for that fare. Default False when absent — this is the positive
  confirmation CLAUDE.md requires; do not infer it from the request
  having included a `railcards=` param.
- `sold_out`: True when the journey exists but has no purchasable fare
  matching the railcard (confirm the real fixture's shape for "no fare
  available" before assuming a specific sentinel).
- Raise `ParseError` on a structurally unrecognisable response (missing
  `outwardJourneys`) — that means NRE changed schema and a human must
  look.

`select_target_trains(options, target_times) -> dict[str, TrainOption | None]`
- Exact `"HH:MM"` match against `config.TARGET_DEPARTURES`.
- Returns a dict keyed by target time; value `None` when that departure
  is absent from the results.

`extract_price(...)` helper kept pure and separately tested.

**Acceptance criteria**
- Against the committed real fixture: returns > 0 options, includes
  entries at `"07:25"` and `"07:30"`, all prices are `Decimal`, all
  `railcard_applied is True`.
- Hand-edited fixture with an empty journeys array → returns `[]`, does
  not raise.
- Hand-edited fixture missing the top-level container → raises `ParseError`.
- Hand-edited fixture with a fare-less journey → that option has
  `sold_out=True`, `price=None`.
- Hand-edited fixture with the railcard stripped → `railcard_applied=False`.
- `select_target_trains` on results with only 07:25 returns
  `{"07:25": <option>, "07:30": None}`.
- A price of `9.99` parses to exactly `Decimal("9.99")` (assert equality
  with the `Decimal`, and that `type(price) is Decimal`).
- No `float` anywhere in the module (grep-checkable).

**Edge cases**
- Zero journeys returned → `[]`, not an exception (that's a data
  condition, not a failure).
- Both target trains sold out → both `sold_out=True`.
- Timetable change: neither 07:25 nor 07:30 present → both `None`, and
  the caller logs the departure times that *were* found.
- Missing `arrival_time`, missing `fare_name` → `None`, not a crash.
- Currency: no explicit currency field was observed anywhere in the
  captured NRE response during discovery (unlike the field this task's
  `Non-GBP currency` handling originally assumed) — confirm this against
  the real committed fixture before assuming `"GBP"` unconditionally; if
  a currency field does turn up, keep the existing non-GBP handling
  (`TrainOption.currency` set from it; the alerting layer refuses to
  compare non-GBP against the threshold).
- Prices are in minor units (pence), confirmed from the real discovery
  response (e.g. `"totalPrice": 3095`, `"prices": {"adult": 3060}`) — the
  real fixture should be spot-checked to confirm this holds throughout,
  and a test should assert it with a comment.

#### Revision: price is the cheapest fare found, not the railcard fare specifically

Superseding the "Price: the 16-25-railcard fare specifically" bullet
above — see §2.1's "Revision: railcard confirmation no longer gates
alerting". `_find_best_fare()` now compares every fare's own
`totalPrice` **and** every matching `railcardFares` entry and returns
whichever is cheaper (ties favour the railcard entry, being the more
informative of two equal prices). `railcard_applied` records only
whether the *winning* price specifically came from a railcard entry;
`sold_out` is now `price is None` (a fare existing with only a plain
price, no railcard entry, is a real bookable option, not "sold out").
This didn't regress the real fixture: every one of its 10 journeys'
cheapest `totalPrice` and cheapest matching `railcardFares` price tie,
so `railcard_applied` stays `True` throughout for that fixture.

---

### Task 5 — Email notifier

**Status: implemented.**

**Created:** `src/notifier.py`, `tests/test_notifier.py`
**Modified:** `src/models.py` (added `AlertMatch`, per this task's spec —
travel date + `TrainOption` + the threshold it beat), `src/config.py`
(added `build_journey_planner_url(travel_date, hour, minute)`, factored
out of `src/scraper.py`'s private helper so both it and the notifier's
per-fare email links share one implementation of
`JOURNEY_PLANNER_URL_TEMPLATE`'s formatting instead of duplicating it).

Two implementation notes beyond the spec below:
- The subject line reuses `config.ORIGIN_NAME`/`DESTINATION_NAME`
  ("Oxford" / "London Paddington") rather than the plan's illustrative
  "Oxford → Paddington" — the spec's example was illustrative, not an
  exact string to match.
- Each emailed row links to that specific train's own results page
  (`config.build_journey_planner_url` anchored at the matched option's
  `departure_time`), not a single generic link for the whole date.

All 98 tests pass (`python -m pytest -q`), no `float` in the module.

**What the code does**

`send_alert(matches: list[AlertMatch], secrets: Secrets, *, dry_run: bool = False) -> None`
- `AlertMatch` = travel date + `TrainOption` + the threshold it beat.
- Builds a subject like
  `Cheap train: Oxford → Paddington £8.70 on Fri 11 Sep` (cheapest match
  first; append `(+N more)` when there are several).
- Body: plain text **and** a simple HTML table — one row per match with
  date, departure time, arrival, price, direct/changes, and a link to the
  National Rail Enquiries results page for that date (reuse
  `config.JOURNEY_PLANNER_URL_TEMPLATE`).
- POSTs to `https://api.resend.com/emails` with
  `Authorization: Bearer <key>`, JSON body `{from, to, subject, text, html}`,
  `timeout=20`.
- Retries twice on network error / HTTP 5xx / 429 with backoff.
- Raises `NotifierError` on non-2xx after retries. **The exception
  message must not contain the API key**, and any logged response body
  must be truncated and scanned for the key substring before logging.
- `dry_run=True` prints the fully-rendered email to stdout and makes no
  network call.

**Acceptance criteria**
- Tests mock `requests.post`; assert the request URL, that the
  `Authorization` header carries the key, and the JSON body's `to`/`from`.
- 200 → returns None. 500 → retries then raises `NotifierError`.
- 401 (bad key) → raises immediately without retrying, and the raised
  message does **not** contain the key (assert `key not in str(exc)`).
- `dry_run=True` → `requests.post` not called at all.
- Subject and body render correctly for one match and for three.
- Prices render as `£8.70` (two decimal places) — assert on a
  `Decimal("8.7")` input rendering as `8.70`.

**Edge cases**
- Empty `matches` list → raise `ValueError`; the caller must not call
  this with nothing to say.
- Resend rate limit (429) → treated as retryable.
- Non-ASCII in the body (£, →) → send UTF-8 explicitly.
- Very long match list → cap the emailed table at 20 rows plus a
  "+N more" line.

#### Revision: multiple comma-separated recipients

`ALERT_EMAIL_TO` may hold more than one address, comma-separated (e.g.
`a@example.com, b@example.com`). `notifier._parse_recipients()` splits,
strips, and drops empties, and the Resend payload's `"to"` is always
sent as a list (Resend accepts either a string or a list of up to 50
addresses, so sending a list unconditionally needs no single/multi
special-casing). The `Secrets.email_to` field itself is unchanged — it
stays the raw string from the environment; only `send_alert` parses it,
right before building the payload. This does **not** by itself make
Resend deliver to a second address: the free sandbox sender
(`onboarding@resend.dev`) only ever delivers to the Resend account
owner's own address, so a second, different recipient needs a verified
domain in Resend and an `ALERT_EMAIL_FROM` address on it — see README.md
for the verification steps.

#### Revision: per-row railcard-confirmation column

Following §2.1's revision (alerting no longer requires railcard
confirmation), the fixed "(16-25 Railcard)" heading was dropped from
the subject/body — it's no longer true of every row. Instead each row
now shows its own `railcard_applied` status ("16-25 Railcard" vs. "16-25
Railcard NOT confirmed" in the text body; a "16-25 Railcard" column with
"Yes"/"Not confirmed" in the HTML table), so the email stays accurate
per fare without reintroducing a gate on whether to send it at all.

---

### Task 6 — Orchestrator, booked-date exclusion, and alert decision

**Status: implemented.**

**Created:** `src/main.py`, `src/booked_dates.py`, `booked-dates.txt`,
`tests/test_main.py`, `tests/test_booked_dates.py`

`evaluate()` returns the single `list[AlertMatch]` this task's signature
sketch shows. (An earlier revision of this implementation had it return
`tuple[list[AlertMatch], bool]`, the second element a
`railcard_unconfirmed` flag — see the revision note after step 7 below
for why that was removed again.)

Confirmed against a real (if network-less) run in this environment (at
the time, via the now-removed `DRY_RUN=1`; the equivalent today is
`SKIP_TIME_GATE=1 TEST_RUN=1`, see Task 7): the invocation correctly
drove the whole pipeline down to a per-date scraper failure (no
Chromium binary installed in this sandbox) being caught, logged, and
surfaced as exit code 1 with "all 1 candidate date(s) failed" — the
intended failure path, not a crash. All 128 tests pass (`python -m
pytest -q`), no `float` in `src/main.py` or `src/booked_dates.py`.

**What the code does**

`src/booked_dates.py`:
- `load_booked_dates(path: Path) -> set[date]` per §2.3 of this plan:
  missing file → `set()`; blank lines and `#`-comments ignored; each
  remaining line parsed with `date.fromisoformat`; a line that fails to
  parse is logged as a warning (including the line number and raw text)
  and skipped, never raises.

`booked-dates.txt` (committed with placeholder content, not empty, so
the user sees the format immediately):
```
# Dates you've already booked a ticket for, one per line as YYYY-MM-DD.
# The tool will stop checking these dates — no code changes needed.
# To add one: click the pencil (edit) icon on this file on github.com,
# add a line, and commit. Lines starting with # are ignored, so this
# example below does nothing until you remove the leading #.
# 2026-09-08
```

`main() -> int`:
1. Compute `today = datetime.now(config.LONDON).date()`.
2. `all_candidates = term_dates.checkable_dates(today + 1 day, term_dates.LAST_KNOWN_DATE)`
   — every remaining checkable date to the end of the school year, not a
   fixed horizon.
   `booked = booked_dates.load_booked_dates(config.BOOKED_DATES_PATH)`.
   `candidates = [d for d in all_candidates if d not in booked]`; log how
   many were skipped as already booked (and which dates, at INFO level).
   If `config.MAX_DATES` is set (debug/manual runs only), truncate
   `candidates` to the first `MAX_DATES` entries.
3. If `candidates` is empty → log `"No checkable travel dates remaining this school year — nothing to do."` (or, if `all_candidates` was non-empty but all booked, `"All remaining dates are already booked — nothing to do."`), return `0`. **No browser launch, no email.**
4. Load secrets *before* scraping, so a misconfiguration fails fast.
   (Originally specced to skip this in `DRY_RUN` — removed in Task 7's
   revision, see there; secrets are always required now, since every
   run always sends a real email.)
5. For each candidate date, in ascending order, with a 5–15s randomised
   pause between dates:
   - `raw = scraper.fetch_journey_search(date, artifacts_dir=...)`
   - `options = parser.parse_journeys(raw, date)`
   - `targets = parser.select_target_trains(options, config.TARGET_DEPARTURES)`
   - Log a one-line summary per target: time, price or `sold out` or `not found`.
   - Catch per-date exceptions, record them in a failures list, and
     **continue to the next date** — one bad date must not lose the rest.
     Except `BlockedError`, which aborts the whole run immediately.
6. `evaluate(targets_by_date) -> list[AlertMatch]` — a pure, separately
   tested function:
   - Include an option iff `price is not None` **and** `currency == "GBP"`
     **and** `price < config.PRICE_THRESHOLD`. (`railcard_applied` does
     not gate inclusion — see the revision note below.)
7. If matches → `notifier.send_alert(...)`; log what was sent.
8. If no matches → log "no fares below threshold", return 0.
9. Exit code: `0` = ran cleanly (alert or not). `1` = every candidate
   date failed, or blocked, or the notifier failed. `0` with a warning
   when *some* dates failed but at least one succeeded.
10. `if __name__ == "__main__": sys.exit(main())`.

#### Revision: dropped the `railcard_unconfirmed` run-wide safety check

The original spec's steps 6–7 above (superseded) had `evaluate()` also
record a `railcard_unconfirmed` flag whenever any priced option's
railcard discount wasn't positively confirmed, and had `main()` suppress
the **entire** run's email whenever that flag was set — even if other,
genuinely-confirmed matches existed elsewhere in the same run.

The user clarified this wasn't the behaviour they wanted: their actual
concern was only "don't email me if there's no real sub-£10 fare",
not "never email me about a fare unless its railcard discount was
specifically confirmed." See §2.1's revision note for the full decision.
`evaluate()` now returns a plain `list[AlertMatch]` with no side flag,
and there is no run-wide suppression based on `railcard_applied` at all.

Structured, timestamped logging to stdout throughout (use `logging`
configured to stdout at INFO).

**Acceptance criteria** (tests stub `scraper.fetch_journey_search`,
`notifier.send_alert`, and freeze "today" via an injectable clock — pass
`today` as an optional `main(today=None)` parameter rather than
monkeypatching `datetime`)
- Today in the summer holidays → returns 0, scraper never called,
  notifier never called.
- One date with a £8.70 railcard-applied fare → `send_alert` called once
  with one match; returns 0.
- Price exactly `Decimal("10.00")` → **no alert**, returns 0.
- Price `Decimal("9.99")` → alert.
- Both trains sold out on every date → no alert, returns 0.
- Zero trains returned on every date → no alert, returns 0, warning logged.
- Scraper raises on date 1 of 3, succeeds on 2 and 3 → both remaining
  dates checked, returns 0, failure logged.
- Scraper raises on *all* dates → returns 1, no email.
- `BlockedError` on the first date → returns 1 immediately, no further
  scrapes.
- A sub-threshold price with `railcard_applied=False` → returns 0 and
  `send_alert` **is** called with that match (see the revision note
  after step 7 above).
- ~~`DRY_RUN=1` → no secrets required, `send_alert` receives
  `dry_run=True`~~ — removed in Task 7's revision; see
  `TEST_RUN`/`SKIP_TIME_GATE` there instead, both of which still
  require real secrets since they always send a genuine email.
- Notifier raises → returns 1.
- `booked-dates.txt` lists tomorrow's date → that date is excluded from
  `candidates` and the scraper is never called for it.
- `booked-dates.txt` lists every remaining candidate → returns 0,
  scraper never called, "all remaining dates are already booked" logged.
- Missing `booked-dates.txt` → behaves exactly as if the file were
  empty (no candidates excluded, no error).

`tests/test_booked_dates.py` acceptance criteria:
- A file with `2026-09-08\n# comment\n\n2026-10-01` parses to exactly
  `{date(2026,9,8), date(2026,10,1)}`.
- A missing path → `set()`, no exception.
- A line `not-a-date` → skipped with a logged warning; the other valid
  lines in the same file still parse correctly.
- An entirely empty file → `set()`.
- Duplicate dates in the file → collapse to one (it's a set).

**Edge cases**
- Run at 23:50 UTC in winter vs summer — assert `today` is derived from
  Europe/London (test by injecting a `datetime` near midnight UTC).
- `today` set to the day before `term_dates.LAST_KNOWN_DATE` → exactly
  one candidate remains, if it qualifies.
- `MAX_DATES=1` with many real candidates → exactly one date checked.
- Today the first day of Autumn Term 2026 → candidate list has 102
  entries (regression guard on the "check everything to year end" scope,
  matching §1.5 of the plan).
- Non-GBP currency → excluded from matches, warning logged.
- Duplicate matches for the same date+time → de-duplicate before emailing.

---

### Task 7 — GitHub Actions workflow and documentation

**Status: implemented, revised beyond the original spec per explicit
user request** (run at a fixed local time rather than a fixed UTC cron,
and a real end-to-end test-email path) — see below for what changed and
why. Task 8 covers two further additions from that same request (the
price-history CSV and the booked-dates website) kept separate since
they're substantial enough to warrant their own task record.

**Created:** `.github/workflows/price-check.yml`,
`.github/workflows/test.yml`, `README.md`

#### Running at a fixed local time, not a fixed UTC cron

The original spec above used a single `cron: "0 7 * * *"` (accepting
that London-local time would drift by an hour across the BST/GMT
boundary). The user's actual request was 8pm **British time**, not 8pm
UTC or 7pm/9pm depending on season — GitHub Actions cron can't express
a timezone-aware schedule directly, so the implemented design instead:

- Schedules **two** cron lines, `0 19 * * *` and `0 20 * * *` (19:00 and
  20:00 UTC — the two times that are ever 8pm somewhere across the DST
  year).
- Adds `config.RUN_HOUR_LONDON = 20` and a gate at the top of
  `src/main.py`'s `main()`: if the real `datetime.now(config.LONDON)`
  hour isn't 20, log and return 0 immediately — no candidates computed,
  no browser launched. Exactly one of the two daily firings ever passes
  this check on any given day, automatically correct across the March/
  October clock change with zero manual cron maintenance.
- `main(today=None, now=None)` — `now` was added as its own parameter
  (distinct from `today`) specifically so tests can freeze the gate's
  clock independently of the date-computation clock.
- A manual `workflow_dispatch` run sets `config.SKIP_TIME_GATE` so
  testing isn't itself gated on happening to click "Run workflow" at
  8pm — see the next section for why this isn't a user-facing toggle.

This is the same principle §1.3/§2.2 already established for weekday/
term gating (`is_checkable_day`) applied to time-of-day: **all timing
decisions live in Python, never in the cron expression itself.**

#### One test, not a grid of toggles

The first working version of this exposed three independent
`workflow_dispatch` checkboxes (`dry_run`, `send_test_email`,
`skip_time_gate`) — the user's explicit follow-up feedback was that
they wanted **one type of test**, that does the complete real thing
(scrape, write to `price-history.csv`, send an email), not a
combination of toggles to reason about. Revised to:

- `workflow_dispatch` has exactly one input, `max_dates` (default `1`,
  so a manual run is fast; blank checks everything). No boolean
  toggles at all.
- `config.SKIP_TIME_GATE` and `config.TEST_RUN` are both set purely
  from `github.event_name == 'workflow_dispatch'` in the workflow's
  `env:` block — not from separate inputs. Kept as two internal flags
  (not one) only because they answer genuinely different questions and
  a single test (in `tests/test_main.py`) exercising the RUN_HOUR_LONDON
  gate shouldn't also have to reason about the email-fallback behaviour,
  and vice versa.
- `DRY_RUN` and the synthetic-fare `SEND_TEST_EMAIL` path (a fixed
  £7.77 fake fare, no scraping) were both **removed entirely** —
  `src/main.py` no longer has any print-instead-of-send mode, and a
  test run's data is always real, never fabricated.
- `src/main.py`'s `_best_effort_matches_for_test()`: when `TEST_RUN` is
  set and `evaluate()` found nothing genuinely below threshold, picks
  the single cheapest real fare found across the whole run — confirmed
  railcard discount or not, per Task 6's revision note — and sends
  *that* through the normal `notifier.send_alert` path — a real email
  using real data, confirming scraping + the CSV log + Resend delivery
  together, without fabricating a price. Returns no email (not a
  fabricated one) if literally nothing priced was found at all (e.g.
  everything sold out) — even a test run never invents data.

#### Workflow permissions

`permissions: contents: write` (not `read`, as originally specced) —
required for the price-history CSV commit-back in Task 8 below. Scoped
to nothing beyond repo contents.

#### Splitting the test job into its own workflow

Rather than a second job inside `price-check.yml` (as the original spec
suggested), `pytest` runs in a separate `test.yml` workflow triggered on
`push`/`pull_request` — it has nothing to do with the cron schedule and
shouldn't share that workflow's concurrency group or run history.

**README.md** covers everything the original spec asked for (what the
tool does, marking a date as booked, updating term dates, required
secrets, Resend setup, local run instructions, operational caveats,
how failures surface) plus the two Task 8 additions below and the
single `max_dates` manual-run input.

**Acceptance criteria**
- `.github/workflows/price-check.yml`, `test.yml`, and (Task 8's)
  `deploy-pages.yml` all parse as valid YAML. **Done.**
- No secret value appears in any workflow log. **Done** — see
  `src/notifier.py`'s redaction, unconditional of `TEST_RUN`'s
  synthetic fare never touches a real secret path.
- The test workflow passes on a clean checkout (all 150+ tests).
  **Done.**

**Edge cases**
- Secrets absent (e.g. a fork) → the job fails fast with a clear
  `ConfigError` naming the missing variables, not a stack trace deep in
  the notifier — see `src/main.py`'s `_load_secrets_for_run()`.
- Playwright cache miss → `playwright install` still runs and succeeds.
- Run overlaps a previous run → `concurrency` prevents it.
- A scheduled run that's outside the 8pm London hour → clean no-op,
  covered by `tests/test_main.py`'s `test_wrong_hour_is_a_noop_*`.

---

### Task 8 — Price history log and booked-dates website

**Status: implemented**, per explicit user request after Task 7.

**Created:** `src/price_log.py`, `tests/test_price_log.py`,
`scripts/export_terms.py`, `site/index.html`, `site/app.js`,
`site/style.css`, `.github/workflows/deploy-pages.yml`
**Modified:** `src/main.py` (appends to the log after each successfully
parsed date), `.gitignore` (`site/terms.json` is generated at deploy
time, not committed)

This supersedes §5's original deferral of "price history" and "a web
dashboard" as out-of-scope future work — the user asked for both
directly, in a much narrower form than a full dashboard (an
append-only CSV; a single-purpose checkbox page, not a browsable price
history UI).

#### Price history CSV

`src/price_log.append_price_log(path, checked_at, entries)` appends one
row per (travel_date, target_departure, option-or-None) to a CSV at
`config.PRICE_LOG_PATH` (`price-history.csv`, repo root), writing a
header only if the file doesn't exist yet — **never** truncates or
rewrites existing rows, so this is a permanent history, not a snapshot.
Called from `main()` once per successfully-parsed candidate date (not
called at all for a date that failed to scrape/parse, since there's no
real data to log). Columns: `checked_at` (UTC timestamp of the check),
`travel_date`, `target_departure`, `actual_departure`, `arrival_time`,
`price_gbp`, `railcard_applied`, `sold_out`, `fare_name`.

Persisting this across runs (each a fresh Actions checkout) needs the
updated file committed back to the repo — `price-check.yml`'s "Commit
updated price history" step does `git add price-history.csv && git
commit && git push` (skipped entirely if the file is unchanged, e.g. a
run where every date failed), with a `git pull --rebase` first in case
the booked-dates website or another run committed in the meantime.

#### Booked-dates website

A static page (`site/`) published via GitHub Pages, replacing "edit
`booked-dates.txt` on github.com" as the primary workflow for someone
not used to GitHub — see the chosen design's tradeoffs in this
project's chat history: GitHub Pages can only serve static files (no
server-side code), so the page calls the **GitHub Contents API
directly from the visitor's own browser**, authenticated with a
fine-grained personal access token (repo-scoped, Contents:
read-and-write only) that the visitor generates themselves and pastes
into the page once — stored only in that browser's `localStorage`,
never sent anywhere but `api.github.com`. No backend server exists or
is needed.

- `scripts/export_terms.py` exports `src.term_dates.TERMS` (+
  `CHECK_WEEKDAYS`, `LAST_KNOWN_DATE`) as JSON, run fresh by
  `deploy-pages.yml` before every deploy — the site's "which dates are
  checkable" logic is a direct JS port of `term_dates.py`'s
  `is_checkable_day()`/`checkable_dates()` (operating on ISO date
  *strings*, which compare correctly lexicographically, sidestepping JS
  `Date`'s local-timezone footguns entirely), computed against the
  visitor's own current date rather than a value baked in at export
  time. Verified byte-for-byte identical to the real Python output
  across all 102 dates from 2026-09-01 (see this project's chat
  history for the cross-check).
- Ticking a checkbox re-fetches the current file (fresh `sha`, to avoid
  clobbering a concurrent edit from another device), adds/removes that
  one date, and `PUT`s the result back, preserving `booked-dates.txt`'s
  original leading comment block.
- `.github/workflows/deploy-pages.yml` publishes `site/` via
  `actions/upload-pages-artifact` + `actions/deploy-pages` on push to
  `main` (paths: `site/**`, `src/term_dates.py`,
  `scripts/export_terms.py`) or `workflow_dispatch`. Requires a one-time
  Settings → Pages → Source → "GitHub Actions" toggle (see README.md) —
  no available tool could do this on the user's behalf, so it's
  documented as a manual step.
- `OWNER`/`REPO`/`BRANCH` are constants at the top of `site/app.js`,
  hardcoded to `rosjo99`/`Train-prices`/`main` — this only actually
  controls the same `booked-dates.txt` the scheduled job reads once
  this branch is merged to `main`.
- **Revised per explicit user follow-up request** to also show the
  last recorded price for each date: the repo is public (a separate
  user decision, made when GitHub Pages turned out to require a paid
  plan for a private repo — see this project's chat history), so
  `booked-dates.txt` **and** `price-history.csv` are both now read via
  GitHub's unauthenticated raw-content host
  (`raw.githubusercontent.com`), not the authenticated Contents API —
  no token needed just to *view* the table, only to *save* a change
  (`toggleDate` still re-fetches via the authenticated API with a fresh
  `sha` immediately before writing). `site/app.js` gained a minimal CSV
  parser (`parseCsv`/`parseCsvLine`, quote-aware since `fare_name`
  could in principle contain a comma) and
  `latestPriceByDateAndTarget()`, which keeps only the most-recently-
  checked row per (travel_date, target_departure) — verified against a
  real `price_log.append_price_log()`-written sample, including that a
  train disappearing from the latest check correctly overrides an
  older price rather than leaving it stale. `export_terms.py` now also
  exports `config.TARGET_DEPARTURES` so the site's price columns are
  labelled without hand-duplicating the route's departure times.
  Checkboxes are disabled (with an explanatory `title` tooltip) until a
  token is present, rather than the table being hidden entirely.
- **A related question the same follow-up raised — is the site now
  editable by anyone, since the repo is public?** No: GitHub repo
  visibility only ever controls *read* access; *write* access (a `PUT`
  through the Contents API succeeding at all) is independently gated by
  GitHub's own collaborator permissions on the repo, checked at request
  time regardless of the token used or how public the repo is. A
  visitor's token only ever carries the write access their own GitHub
  account already has — there is no scenario where making the repo
  public grants a stranger's token new permissions. Confirmed live via
  `list_repository_collaborators`: as of this revision the repo has
  exactly one collaborator (the owner), so literally no one else,
  including the intended second user (the "girlfriend" in the original
  request), can write until explicitly added as a collaborator — which
  is also the entire mechanism for granting her access, without any
  separate login system needing to be built. A custom login layered on
  a backend-less static site could not exceed this: any shared secret
  baked into client-side JS is visible to anyone who opens dev tools,
  strictly weaker than relying on GitHub's own auth.

**Acceptance criteria**
- `append_price_log` never truncates existing rows; writes a header
  once; handles a `None` option (target departure absent from results)
  without crashing. Covered by `tests/test_price_log.py` (7 tests).
- `main()` calls the price log only for dates that were successfully
  scraped and parsed, never for a failed date. Covered by
  `tests/test_main.py`'s price-log tests.
- The JS port of `checkable_dates()` matches the real Python output
  exactly (spot-checked directly, not just by test count).
- `deploy-pages.yml` and the site's HTML/CSS/JS all parse/lint cleanly
  (`node --check site/app.js`).

**Edge cases**
- A date with no matching target departure at all (`option is None`) is
  still logged, with blank price/departure fields — "this train
  disappeared from the timetable" is itself worth a historical record.
- Two near-simultaneous booked-dates edits (e.g. the website and a
  direct file edit) → the website always re-fetches the current `sha`
  immediately before writing, so the GitHub API itself rejects a stale
  write with a 409 rather than silently losing an edit; the page
  surfaces this as a "could not save" message rather than a silent
  failure.
- The price-history commit racing the *next* scheduled run's own commit
  → `git pull --rebase` before push in the workflow step, matching the
  same race-handling already used for the booked-dates website.

---

## 5. Out of scope / future work

Do not implement these without a new plan:

- **Duplicate-alert suppression.** If a fare sits below £10 for a week
  you get an email every day. The obvious fix is a small state file (or
  simply deriving "already alerted" from `price-history.csv`, now that
  Task 8 added it) keyed on (travel date, departure time, price bucket).
  Still deferred — Task 8 removed the original "adds write permissions"
  objection, but this is state-management complexity for a comfort
  problem nobody has asked for yet.
- Return legs, other routes, other railcards, other thresholds.
- Split-ticketing or comparison against other retailers.
- ~~A web dashboard or price history~~ — **implemented in narrower
  form by Task 8**: an append-only CSV log (not a browsable dashboard)
  and a single-purpose booked-dates checkbox page (not a general price
  history UI). A full browsable dashboard over `price-history.csv`
  remains out of scope.

## 6. Review checklist (for the reviewer agent, after implementation)

1. Every acceptance criterion above has a corresponding passing test.
2. No `float` used for money anywhere (`grep -rn "float(" src/`).
3. No secret can reach stdout: check `notifier.py` error paths and any
   `repr()` of the secrets dataclass (it should define `__repr__` to
   redact).
4. `date.today()` / naive `datetime.now()` appear nowhere — all clock
   reads go through `ZoneInfo("Europe/London")`.
5. Threshold comparison is `<`, never `<=`.
6. `is_checkable_day` results match `CLAUDE.md` for all three terms,
   spot-checked by hand against the dates listed there.
7. The scraper distinguishes "blocked", "timed out", and "no trains
   found" — these must never collapse into one silent code path.
