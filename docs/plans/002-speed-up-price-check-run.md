# Plan 002 — Speed up the price-check run

Status: ready to implement — all open decisions resolved (see "Decisions"
below).
Amends `docs/plans/001-train-price-alert.md` §2.2 (check every remaining
date to `LAST_KNOWN_DATE`) and Task 7's workflow.

Goal: cut the scheduled run from ~5m15s to roughly 1m45s.

**Hard constraint, above the performance goal:** no date whose fares NRE
has actually released may go unchecked. §4 is the correctness argument;
every design choice below is subordinate to it.

## Decisions (user, 2026-09-01)

The plan below originally proposed six changes (A–F) and left five open
decisions in §8. The user has now decided all of them:

1. **No `SEARCH_HORIZON_DAYS` cap.** Change A is dropped. The candidate
   list's sole upper bound stays `term_dates.LAST_KNOWN_DATE`, exactly as
   today. Rationale accepted from §2: capping contributes ~0 to the
   speedup (the early-stop mechanism already bounds how many doomed
   dates get attempted), so the extra constant/config-surface isn't
   worth it given zero observed occurrences of the degenerate
   fast-empty-response scenario it would have guarded against (§7.1).
   Everything else in §4's correctness argument (properties 1, 3, 4)
   still holds — it just no longer needs property 2's horizon-margin
   reasoning, since there's no horizon constant left to justify.
2. `FULL_RETRY_HORIZON_DAYS = 98` — proceed with the plan's recommended
   value (14 weeks, 4 days past the measured 94-day boundary). Not
   separately relitigated.
3. **Update `CLAUDE.md` and `README.md`** to match the new behaviour and
   the measured numbers (94-day observed horizon vs. the old "roughly 12
   weeks", new poll/backoff constants, `--with-deps` removal). In scope
   for this change.
4. **`timeout-minutes: 45 → 20`.** Confirmed.

Sections below are marked accordingly; §5.1's `horizon_end` /
`SEARCH_HORIZON_DAYS` code block is **not** implemented — see the revised
§5.1.

---

## 1. Measured evidence

Everything in this section comes from real GitHub Actions runs
(`33489993285`, 09:00:50Z–09:06:10Z on 2026-09-01; cross-checked against
`33481097236` and `33432003430`) and from the committed
`price-history.csv`. Nothing is inferred from the code alone.

### 1.1 Where the 5m15s goes

| Step | Duration |
| --- | --- |
| Set up job / checkout / setup-python | 6s |
| `pip install -r requirements.txt` | 6s |
| `actions/cache@v4` (playwright browsers) | 6s — cache **hit** |
| `playwright install --with-deps chromium` | **17s** |
| **Run price check** (`python -m src.main`) | **276s** |
| Commit price history | 2s |
| **Total job** | **315s** |

### 1.2 Inside the 276s

Batch boundaries from the `attempt 1/3: starting` log lines
(`PARALLEL_DATES = 5`, batches dispatched strictly one after another):

| Batch | Dates | Wall clock |
| --- | --- | --- |
| 1 | 2026-09-03 … 09-11 | 09:01:30 → 09:02:08 (**38s**) |
| 2–7 | 2026-09-15 … 12-04 | 09:02:08 → 09:02:45 (**37s** total, 5–12s each) |
| 8 | 2026-11-27 … **2026-12-08** | 09:02:45 → 09:04:22 (**97s**) |
| 9 | 2026-12-10 … **2027-01-08** | 09:04:22 → 09:06:05 (**103s**) |

Batches 8 and 9 produced zero usable data and cost **200s** — 73% of the
step. Batch 8 held four dates that succeeded in ~6s plus one
(2026-12-08) that burned the full retry budget; a batch's wall clock is
its slowest member, so **one** doomed date costs a whole batch.

Batch 1's 38s is also almost entirely one date's retry (§1.5).

### 1.3 What a doomed date costs, and why retrying is wasted on it

The failure is always `scraper.TimeoutScrapeError`:

```
[2026-12-10] attempt 3/3 failed: no journey-planner response and no DOM
results within 20.0s for 2026-12-10
```

Verified in the code (not assumed):

- `PAGE_BUDGET_SECONDS = 20.0` is used **twice** per attempt — as
  `page.goto(..., timeout=PAGE_BUDGET_SECONDS * 1000)` and again as the
  `_wait_for_result` poll deadline.
- `RETRY_BACKOFF_SECONDS = (10, 20)`, indexed
  `[min(attempt - 1, len - 1)]` → 10s after attempt 1, 20s after
  attempt 2.
- `attempts = 3` by default.

Measured, end to end, for 2026-12-10: 23 + 10 + 22 + 20 + 21 = **~96s**.
Attempts 2 and 3 can only ever fail identically, because the date is
deterministically outside NRE's search window.

### 1.4 The early-stop mechanism works — it is not the bug

`MAX_CONSECUTIVE_FAILURES = 5` fired exactly as designed:

```
[2027-01-07] 5 consecutive dates failed — stopping early ...;
62 further candidate date(s) were not attempted
5 of 39 attempted candidate date(s) failed; continuing with the 34 that succeeded
```

Also confirmed: the `fares: []` / `sold_out=True` shape from
`tests/fixtures/journey_search_fareless_journey.json` has **never**
occurred in production. `price-history.csv` has 356 rows, of which
`sold_out == True`: 0, blank `actual_departure`: 0. See §7.1.

### 1.5 Retries are load-bearing for **in-range** dates

Critical counter-evidence against the obvious "just retry less" fix.
2026-09-04 — a normal, in-range date that produced real prices — failed
its first attempt and recovered on the second:

```
09:01:34.25 [2026-09-04] waiting for journey-planner response ... (budget=20.0s)
09:01:54.62 [2026-09-04] attempt 1/3 failed: no journey-planner response ...
09:01:54.62 [2026-09-04] backing off 10s before retrying
09:02:04.62 [2026-09-04] attempt 2/3: starting
09:02:05.68 [2026-09-04] waiting for journey-planner response ... (budget=20.0s)
09:02:08.08 [2026-09-04] attempt 2/3: succeeded
```

So `attempts` must stay ≥ 2. **Do not reduce the attempt count.**

### 1.6 A successful XHR always arrives in ~2.4 seconds

From the same lines — time from "waiting for journey-planner response"
to "succeeded":

- 2026-09-03, attempt 1: 09:01:33.60 → 09:01:35.96 = **2.36s**
- 2026-09-04, attempt 2: 09:02:05.68 → 09:02:08.08 = **2.40s**

And the failure mode is **binary**, not slow: 2026-09-04's first attempt
sat the full 20s and never saw the XHR at all, then the very next
attempt got it in 2.4s. So a poll budget of 20s is ~8× the observed
success time, and waiting past ~10s has never once converted a failure
into a success.

### 1.7 The horizon is real and measurable

First-failing date per run, from the `all 3 attempt(s) failed` lines:

| Run date | Last date returning prices | First date failing | Implied bound |
| --- | --- | --- | --- |
| 2026-08-31 | 2026-12-03 | 2026-12-04 | good at +94, fails at +95 |
| 2026-09-01 07:41 | 2026-12-04 | 2026-12-08 | good at +94 |
| 2026-09-01 09:06 | 2026-12-04 | 2026-12-08 | good at +94 |

The boundary advanced by exactly one day between 8/31 and 9/1, so it is
a **daily-rolling** window, not a fixed date stepping weekly. The 8/31
run brackets it to `[94, 95)` days. CLAUDE.md's "roughly 12 weeks" is an
**underestimate**; the observed value is 13.4 weeks.

Treat 94 as *one season's observation*, not a constant. Timetable
horizons move at the December and May timetable-change dates, and
operators sometimes release earlier. §4 is built on the assumption that
this number will drift.

### 1.8 `--with-deps` installs no libraries on the runner

```
0 upgraded, 9 newly installed, 0 to remove and 74 not upgraded.
```

26 packages logged `is already the newest version` — that is every
shared library Chromium needs (`libasound2t64`, `libatk1.0-0t64`,
`libatk-bridge2.0-0t64`, `libatspi2.0-0t64`, `libcairo2`, `libnss3`, …),
already on the `ubuntu-24.04` image. The 9 newly installed are **all
fonts**: `fonts-ipafont-gothic`, `fonts-freefont-ttf`,
`fonts-tlwg-loma-otf`, `fonts-unifont`, `fonts-wqy-zenhei`,
`xfonts-encodings`, `xfonts-utils`, `xfonts-cyrillic`,
`xfonts-scalable`.

The 17s is `apt-get update` (11.7 MB of package lists) plus dependency
resolution, 09:01:13 → 09:01:29. The `playwright install chromium`
portion took <1s thanks to the `actions/cache@v4` hit.

This scraper reads a JSON XHR. It never depends on rendered glyphs — the
cookie-banner click uses id/text selectors resolved from the DOM, and
screenshots are written only on failure, for humans. None of those font
packages are needed.

---

## 2. The key finding, stated plainly

**Capping the candidate date list is not where the speedup comes from.**

The run already stops early after 39 of 101 candidates (§1.4). The 200s
is not "too many dates in the list" — it is the **retry cost of six
doomed dates**, at ~96s each, spread across two batches (§1.2, §1.3).
Even an infinitely generous candidate list costs the same 200s, because
`MAX_CONSECUTIVE_FAILURES` bounds how many doomed dates ever get
attempted.

This is *why* the user's decision to drop the horizon cap (Change A)
costs nothing: the speedup comes entirely from making each attempt and
each doomed date cheaper (§5), not from shrinking the candidate list.

---

## 3. Summary of changes

| # | Change | File | Saves |
| --- | --- | --- | --- |
| ~~A~~ | ~~Generous candidate horizon~~ — **dropped, see Decisions** | — | — |
| B | One attempt, not three, for dates beyond the expected-release horizon | `src/main.py`, `src/scraper.py` | ~140s |
| C | Poll budget 20s → 10s (navigation timeout unchanged) | `src/scraper.py` | ~20s |
| D | Retry backoff (10, 20) → (5, 10) | `src/scraper.py` | ~15s |
| E | Drop `--with-deps` | `.github/workflows/price-check.yml` | ~16s |
| F | `timeout-minutes` 45 → 20 | `.github/workflows/price-check.yml` | — |
| G | Update `CLAUDE.md` / `README.md` to match | docs | — |

Explicitly **unchanged**: `attempts = 3` for in-range dates (§1.5),
`MAX_CONSECUTIVE_FAILURES = 5` (§4.4), `PARALLEL_DATES = 5`, the
candidate list's upper bound (`term_dates.LAST_KNOWN_DATE`, no new cap),
all alerting/CSV/booked-date behaviour (§6).

---

## 4. Correctness: why no releasable date can go unchecked

This section is the acceptance criterion for change B (§4.1's property 1
also documents why dropping the horizon cap is safe by construction — it
simply removes a bound, it doesn't add one). A reviewer should check the
implementation against these properties by name.

### 4.1 Property 1 — nothing added here can ever cause a permanent skip

`main()` derives its candidate list from scratch on **every** invocation,
starting at `today + 1` and ending at `term_dates.LAST_KNOWN_DATE`,
unchanged from today. No new upper bound is introduced. The only new
per-date behaviour (change B) is *how many attempts* a date beyond
`FULL_RETRY_HORIZON_DAYS` gets — it is still fetched, parsed, logged to
`price-history.csv`, and eligible to alert (§4.3). Nothing here removes a
date from the list; nothing here needs a rolling-window argument because
there is no cap to roll.

The implementation must preserve this:

- **No persistence.** Do not add any cache, marker file, CSV column, or
  in-repo record of "this date was beyond the horizon, don't retry it".
  None exists today and none may be added. Every run must independently
  re-derive candidates from `today`.
- **No memoisation across runs.** The only inputs are `today`,
  `term_dates.TERMS`, `booked-dates.txt`, and the one new constant
  (`FULL_RETRY_HORIZON_DAYS`, which only affects attempt count, not
  membership).

### 4.2 Property 2 — `FULL_RETRY_HORIZON_DAYS` carries real margin over the observed lag

Observed release boundary: **94 days** (13.4 weeks), bracketed to
`[94, 95)` (§1.7). Chosen value:

- `FULL_RETRY_HORIZON_DAYS = 98` (14 weeks) — the boundary between "a
  timeout here is a fault, retry it" and "a timeout here is the expected
  answer" (§5.1). 98 is 4 days past the observed 94.

This constant affects *retry budget only*, never list membership (per
§4.1), so getting it slightly wrong has a bounded, self-correcting cost
described in §4.3 rather than a coverage risk.

### 4.3 Property 3 — dates in the speculative zone are still genuinely checked

Change B gives dates beyond `FULL_RETRY_HORIZON_DAYS` **one** attempt
rather than three. They are still fetched, still parsed, and — if NRE
has in fact released them early — still produce real prices, still get
written to `price-history.csv`, and still alert. This is a
retry-budget change, not an exclusion.

The residual risk is narrow and bounded: a *transient* flake on a
speculative-zone date that NRE had actually released. Observed flake
rate is 1 in 39 attempts (§1.5). If that happens, the date is retried in
full 6 hours later, and once it crosses inside 98 days it gets the full
three attempts on every run thereafter. So the worst case is one
skipped observation of a date more than 14 weeks out, not a lost date.

### 4.4 Property 4 — the reactive backstop stays

`MAX_CONSECUTIVE_FAILURES = 5` keeps its value and its mechanism,
unchanged, specifically to cover the case where `FULL_RETRY_HORIZON_DAYS`
turns out to be wrong for a given run (NRE having a bad day, or the
release lag drifting past 98 days). If the static assumption
under-covers, the reactive stop is what prevents burning the whole run —
and per Property 1 nothing is lost when it fires, because the next run
re-derives everything, and the list's only bound is still
`LAST_KNOWN_DATE`.

Do not lower it to make the tail shorter. Change B is the tail fix; the
backstop's job is safety, and a lower threshold only makes a transient
NRE wobble more likely to truncate a run.

---

## 5. Implementation

### 5.1 Change B — `src/main.py`

Add one constant next to `PARALLEL_DATES` / `MAX_CONSECUTIVE_FAILURES`
(orchestration knobs belong together). Do **not** put it in
`src/config.py` (route/alerting/URL config) or `src/term_dates.py`
(deliberately pure school-calendar data with no scraping knowledge).

```python
FULL_RETRY_HORIZON_DAYS = 98
SPECULATIVE_ATTEMPTS = 1
```

**Per the Decisions above: do not add `SEARCH_HORIZON_DAYS`, do not
change the `candidates = term_dates.checkable_dates(...)` line, do not
change the "no checkable travel dates" log message, and do not add any
horizon-cap log line.** The candidate list's construction is untouched
from the current code — only per-date attempt count changes.

Comments (this repo's style: "why", not "what") must record, concisely:

- NRE returns no journeys at all past a daily-rolling window; past it the
  page never makes its `journey-planner` XHR and the scraper times out.
- Measured at 94 days on 2026-08-31 (good at +94, failed at +95) and
  again on 2026-09-01 — i.e. further out than CLAUDE.md's "roughly 12
  weeks", and expected to drift at timetable-change dates.
- `FULL_RETRY_HORIZON_DAYS` is deliberately set *past* the observed
  value: past it a timeout is the expected answer rather than a fault,
  so retrying it three times only buys the same answer three times
  slower; the date is still fetched with one attempt, and it regains the
  full retry budget once it comes inside 98 days. This affects attempt
  count only — it is not a cap on which dates get checked (see
  `MAX_CONSECUTIVE_FAILURES`/`LAST_KNOWN_DATE`, which still bound that).

**Per-date attempt budget.** `_fetch_and_parse_one` currently calls
`scraper.fetch_journey_search(travel_date, artifacts_dir=ARTIFACTS_DIR)`
and takes `fetch_journey_search`'s `attempts=3` default. Give it an
explicit parameter and pass it through:

```python
def _fetch_and_parse_one(
    travel_date: date, attempts: int = 3
) -> dict[str, TrainOption | None] | Exception:
    ...
    raw = scraper.fetch_journey_search(
        travel_date, artifacts_dir=ARTIFACTS_DIR, attempts=attempts
    )
```

In `main()`, compute the cutoff once before the loop and pass a
per-date attempts list into `executor.map`:

```python
full_retry_until = today + timedelta(days=FULL_RETRY_HORIZON_DAYS)
...
    batch = candidates[batch_start : batch_start + PARALLEL_DATES]
    batch_attempts = [
        3 if d <= full_retry_until else SPECULATIVE_ATTEMPTS for d in batch
    ]
    outcomes = list(zip(batch, executor.map(_fetch_and_parse_one, batch, batch_attempts)))
```

(`executor.map` accepts multiple iterables and zips them; keep the
existing `list(zip(batch, ...))` pattern so results are still processed
in original date order after the whole batch finishes.)

Prefer a named local for the literal `3` if the coder finds a natural
one, but do not introduce a constant just to avoid the literal —
`fetch_journey_search`'s own default is already 3 and this must stay
consistent with it.

**`_log_stopped_early` wording.** It currently asserts a cause
("assuming fares aren't released yet this far out") that is now only
*sometimes* the explanation, since most doomed dates now fail fast with
one attempt rather than three, and 5-in-a-row is still a real signal
worth investigating. Reword to say what was observed and what it
implies — e.g. that this many consecutive failures suggests either NRE's
window has moved closer than `FULL_RETRY_HORIZON_DAYS` assumes, or NRE
is unavailable. Keep all three existing arguments (last failed date,
threshold, remaining count) so it stays as diagnosable as today. Update
the comment block above `MAX_CONSECUTIVE_FAILURES` to describe its role
as the reactive backstop of §4.4 — but **do not change the value**.

### 5.2 Changes C + D — `src/scraper.py`

**C. Split the two uses of `PAGE_BUDGET_SECONDS`.** Add a separate
navigation timeout so the poll budget can be cut without also tightening
`page.goto`:

```python
NAVIGATION_TIMEOUT_SECONDS: float = 20.0   # used by page.goto
PAGE_BUDGET_SECONDS: float = 10.0          # post-navigation poll deadline
```

`_attempt_once` uses `NAVIGATION_TIMEOUT_SECONDS * 1000` for
`page.goto`; `_wait_for_result` keeps using `PAGE_BUDGET_SECONDS` for
its deadline and for the `TimeoutScrapeError` message.

Comment must carry the evidence: every observed successful attempt
captured the XHR **2.4s** after navigation settled (2026-09-03 attempt 1:
2.36s; 2026-09-04 attempt 2: 2.40s), and the failure mode is binary —
2026-09-04's first attempt sat the full 20s with no XHR, then the next
attempt got one in 2.4s. Waiting past ~10s has never converted a failure
into a success, so 10s keeps ~4× headroom.

**D. `RETRY_BACKOFF_SECONDS: (10, 20) → (5, 10)`.** Evidence: the one
observed recovery (§1.5) came from a fresh browser context on the very
next attempt, not from having waited 10s. Keep a real backoff rather
than zero — this is still an unattended job hitting someone else's site.

Do **not** touch `attempts`'s default of 3 (§1.5), the block/hijack
retry rule, or `POLL_INTERVAL_MS`.

### 5.3 Changes E + F — `.github/workflows/price-check.yml`

**E.** `- run: playwright install --with-deps chromium`
→ `- run: playwright install chromium`.

Add a comment recording *why* `--with-deps` is absent, since its absence
is the surprising part — on the `ubuntu-24.04` runner image every shared
library Chromium needs is already installed, so `--with-deps` only ever
ran `apt-get update` (~16s, every run, never cacheable because runners
are fresh VMs) and installed 9 font packages this scraper does not need.
Cite run `33489993285`, in the same style as `src/config.py`'s
"confirmed live on 2026-08-31" comments, and note the revert path: if a
future runner image drops one of those libraries, Chromium fails to
launch, `scraper._launch_browser` already turns that into a clear
`ScraperError`, the job fails loudly and uploads artifacts, and the fix
is to put `--with-deps` back — a one-word revert.

Keep the `actions/cache@v4` step exactly as is; it is what makes the
remaining `playwright install chromium` a sub-second no-op.

**F.** `timeout-minutes: 45` → `20`. The existing comment says "tighten
once real durations are observed" — they now are. Update that comment
block to reflect the measured numbers rather than the stale "up to ~100
dates" figure — the candidate count itself is unchanged (still up to
~100+ early in a term, see Decisions §1), but per-date cost is now much
lower.

**Not changed:** `.github/workflows/capture-fixture.yml` also uses
`--with-deps`, but it is a rarely-run manual dev tool where 16s is
irrelevant, and keeping the `--with-deps` form there preserves a
known-good recipe if E ever needs reverting.
`.github/workflows/test.yml` never touches Playwright.

### 5.4 Change G — `CLAUDE.md` and `README.md`

In scope per Decisions §3. Update:

- **`CLAUDE.md` "Which dates get checked"** — still accurate in
  substance (every candidate date through `LAST_KNOWN_DATE` is
  enumerated; the candidate list itself is unchanged since Change A was
  dropped). What needs updating is the *cost* description: dates beyond
  NRE's release horizon now cost one attempt (~12s) instead of three
  (~96s) once past `FULL_RETRY_HORIZON_DAYS`, so the "100+ automated
  requests... four times a day" framing should note that most of those
  requests beyond the release horizon are now cheap, single-attempt
  probes rather than full retry cycles.
- **`CLAUDE.md` "Tech decisions → Concurrency"** — update the quoted
  per-attempt timing figures: poll budget 20s → 10s (navigation timeout
  stays 20s, now a separate constant), retry backoff 10s/20s → 5s/10s.
  Add a sentence on `FULL_RETRY_HORIZON_DAYS` (98 days): dates beyond it
  get one attempt instead of three, since a timeout there is the
  expected answer, not a fault — still checked and logged every run, not
  excluded.
- **CLAUDE.md's "roughly 12 weeks" reference(s)** — correct to the
  measured value: NRE's release horizon is observed at 94 days (13.4
  weeks) as of the 2026-08-31/09-01 measurements (§1.7), not ~12 weeks.
  Frame it as an observed figure that may drift with NRE's own
  timetable-change dates, not a hard constant.
- **`README.md` ~line 153** — the `max_dates` input's `all` value is
  described as "check every remaining date this school year"; this
  remains **accurate** since Change A was dropped (no new cap), so this
  likely needs no change, or at most a note that dates beyond NRE's
  release horizon get fewer retry attempts but are still checked. Verify
  the exact wording against the current file before deciding whether an
  edit is needed at all — don't invent a change that isn't there.

Do not describe a horizon *cap* anywhere in either doc, since Change A
was dropped — only describe the retry-count behaviour change.

---

## 6. What must not change

- `price_log.append_price_log` is still called for **every** date that
  scrapes and parses successfully — booked or not, alertable or not,
  speculative-zone or not — before any alert filtering. The site in
  `site/` joins `price-history.csv` against a client-side checkable-date
  list from `terms.json`, so it already renders far-future dates with no
  recorded price; nothing here regresses it (§1.4: zero sold-out rows,
  zero blank rows have ever been written).
- Booked dates are still scraped and logged, only excluded from
  `evaluate()`.
- `if not results: return 1` ("all attempted candidate dates failed"):
  unchanged.
- `evaluate()`, `_best_effort_matches_for_test()`, the `TEST_RUN` path,
  and the `BlockedError` / `HijackedError` whole-run abort: untouched.
- `config.MAX_DATES` slicing stays where it is.
- The candidate list's construction and bounds
  (`term_dates.checkable_dates(today + timedelta(days=1),
  term_dates.LAST_KNOWN_DATE)`): **unchanged** — Change A dropped.
- No new dependency, no new file, no new env var, no new secret.

---

## 7. Considered and deliberately rejected

### 7.1 Counting "scraped fine but found no fares" toward `MAX_CONSECUTIVE_FAILURES`

Rejected. This was the original hypothesis and it is not what happens
(§1.4): the observed failure is a genuine `TimeoutScrapeError`, and no
run has ever produced a `sold_out` or `option is None` result. Adding it
would create a real false positive — five consecutive dates whose
advance fares are genuinely sold out (plausible around Christmas) would
truncate the run and stop checking later dates that *do* have fares,
which is precisely the correctness failure §4 exists to prevent. Leave
the existing `sold_out` handling exactly as it is.

### 7.2 Reducing `attempts` below 3 for in-range dates

Rejected on direct evidence: 2026-09-04, an in-range date with real
prices, failed attempt 1 and succeeded on attempt 2 (§1.5). With change
C+D an extra attempt now costs ~12s rather than ~23s, so keeping three
is cheap insurance.

### 7.3 Lowering `MAX_CONSECUTIVE_FAILURES` to shorten the tail

Rejected — §4.4. It does not even help much: the check runs once per
completed batch, so with a boundary that falls mid-list the floor is two
batch-times whether the threshold is 3 or 5. Change B attacks the actual
cost instead.

### 7.4 Switching to the Playwright Docker container

Rejected. `container: mcr.microsoft.com/playwright/python:v1.62.0-noble`
would replace 15s of setup-python + pip + cache with a ~2 GB image pull
that typically takes longer than that on a GitHub-hosted runner — likely
a net loss. It also creates a second place to bump the Playwright
version (image tag *and* `requirements.txt`) that can silently drift, and
complicates the `git` commit/push step. The measured avoidable cost is a
single `apt-get update` (§1.8), which change E removes with a one-word
edit.

### 7.5 Detecting NRE's "no journeys this far ahead" state and failing fast

Attractive in principle — it would make a doomed date cost ~3s instead
of ~12s, non-retryably, with no horizon constant needed at all. Deferred
because nobody has captured what NRE actually renders in that state
(failure artifacts are only uploaded when the *job* fails, and these
runs succeeded). Worth revisiting if `FULL_RETRY_HORIZON_DAYS` proves
annoying to maintain; the recon step would be to run
`capture-fixture.yml` against a date ~20 weeks out and read the
resulting page HTML.

### 7.6 Inferring the horizon at runtime from `price-history.csv`

Rejected: fragile, adds a read-your-own-output dependency, and would
need care not to violate §4.1's no-persistence property.

### 7.7 Raising `PARALLEL_DATES`

Not in scope. It would compress the doomed tail into fewer batches, but
it also raises memory/CPU contention on a 4-vCPU runner and is a
separate tuning exercise. Revisit only if §9's projection is not met.

### 7.8 Capping the candidate list (`SEARCH_HORIZON_DAYS`) — original Change A

Dropped per user decision (see Decisions above). Recorded here for the
record: the plan originally proposed `SEARCH_HORIZON_DAYS = 126` (18
weeks) purely as a robustness guard against a degenerate scenario where
NRE might someday return fast, successful, empty responses past its real
release horizon (which would never trip `MAX_CONSECUTIVE_FAILURES`,
since that only counts thrown exceptions — see §7.1). That scenario has
never been observed in production (§1.4), the cap would have contributed
~0 to the actual speedup (§2), and the user judged the simpler
"one fewer constant, nothing new that could be gotten wrong" option
preferable given the hard correctness constraint. If NRE's behaviour
ever changes such that this scenario starts occurring, `MAX_CONSECUTIVE_FAILURES`
will not catch it and this would need revisiting.

---

## 8. Implementation notes for the coder

The plan previously listed five open decisions here; all are now
resolved (see Decisions, top of document) and folded into §5. Nothing
remains open. One reminder carried over from the original plan's risk
list:

- The most likely implementation trip-hazard: `main()` will now pass
  `attempts=` into the scraper, and every `_fake_fetch` in
  `tests/test_main.py` has signature `(travel_date, *,
  artifacts_dir=None)`. All of them will `TypeError` unless updated —
  see §9.3.

---

## 9. Tests

`tests/test_main.py` — must update:

1. **All `_fake_fetch` helpers.** Their signature is
   `(travel_date, *, artifacts_dir=None)`. `main()` will now pass
   `attempts=` through, so every fake in the file (in
   `_install_fake_scraper` and any inline ones) needs an `attempts=None`
   keyword or they will `TypeError`. This is the most likely thing to be
   missed — check every one.
2. No change expected to candidate-count tests
   (`test_first_day_of_autumn_term_has_102_candidates` and similar)
   since the candidate list's construction is unchanged (Change A
   dropped) — verify they still pass unmodified.

`tests/test_main.py` — must add:

3. `test_speculative_zone_dates_get_a_single_attempt` — a date inside
   `FULL_RETRY_HORIZON_DAYS` is fetched with `attempts=3`; a date beyond
   it is fetched with `attempts=1`. Have the fake record the `attempts`
   it was called with. This is the §4.3 property.
4. `test_speculative_zone_dates_are_still_checked_and_logged` — a
   beyond-`FULL_RETRY_HORIZON_DAYS` date that *does* return real prices
   still lands in `price-history.csv` and still alerts if under
   threshold. This is the direct test of the hard constraint: reduced
   retries must not mean reduced coverage.

`tests/test_main.py` — must keep passing unchanged (verify, don't edit):

- `test_summer_holidays_returns_0_no_scraper_no_notifier`.
- `test_today_before_last_known_date_leaves_exactly_one_candidate`.
- `test_today_is_derived_from_europe_london` — depends on
  `checkable_dates` being called **once**, with `start == today + 1`.
  Unaffected since the candidate-list code path is untouched.
- All five early-stop / abort tests
  (`test_five_consecutive_failed_dates_stops_early`,
  `test_a_success_between_failures_resets_the_consecutive_count`,
  `test_five_consecutive_parse_failures_also_stops_early`, and the
  blocked/hijacked abort pair) — the mechanism is unchanged; only
  `_log_stopped_early`'s wording moves, so confirm none assert on it.
- `test_successful_date_appends_to_price_log`,
  `test_failed_date_does_not_append_to_price_log` (§6).

`tests/test_scraper.py` — expected to need little or nothing: it already
monkeypatches `PAGE_BUDGET_SECONDS` (line 238), passes `attempts`
explicitly everywhere, and has a `_no_real_sleep` fixture, so the new
constant values do not leak in. But add a check that no test asserts on
the literal string `"within 20.0s"`, and if `_attempt_once`'s `page.goto`
timeout is asserted anywhere, point it at `NAVIGATION_TIMEOUT_SECONDS`.

Full suite must pass: `python -m pytest`.

---

## 10. Expected result and verification

Per-doomed-date cost after C+D+B: ~2s navigation + 10s poll = **~12s**
(one attempt), down from ~96s.

| | Before (measured) | After (projected) |
| --- | --- | --- |
| Setup steps | 35s | ~19s |
| Batch 1 (incl. one in-range retry) | 38s | ~21s |
| Batches 2–7 (real work) | 37s | ~37s |
| Doomed tail | 200s | ~30s |
| **Total** | **315s (5m15s)** | **~107s (1m47s)** |

Roughly a 2.9× speedup, with no change to which fares are alerted on or
what is written to `price-history.csv`, and no change to which dates are
ever candidates (Change A dropped — the list still runs to
`LAST_KNOWN_DATE`).

Verify after merge by triggering `workflow_dispatch` with
`max_dates = all`, then confirming from the run log that:

1. dates through roughly `today + 94` returned real prices, and the
   count committed to `price-history.csv` matches;
2. any `all N attempt(s) failed` lines are for dates past ~14 weeks and
   show **one** attempt, not three;
3. if `5 consecutive dates failed` appears, note the first failing date —
   if it is materially earlier than `today + 94`,
   `FULL_RETRY_HORIZON_DAYS` needs revisiting;
4. total job time is under ~2 minutes.
