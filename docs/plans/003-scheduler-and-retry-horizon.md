# Plan 003 — Tighten the retry horizon, and replace batching with a continuous scheduler

Status: ready to implement — all open decisions resolved (user, 2026-09-01):
§9.1 Change 3 is **in scope**, implemented as a separable last commit per
its own recommendation; §9.2 `FULL_RETRY_HORIZON_DAYS = 95` confirmed.

Follow-up to `docs/plans/002-speed-up-price-check-run.md`, which is
implemented and merged. Plan 002's hard correctness constraint carries
over unchanged and outranks everything here:

> **No date whose fares NRE has actually released may ever go unchecked.**

Nothing in this plan changes which dates are candidates. All three changes
affect only *retry budget* (Change 1), *dispatch timing* (Change 2), and
*dispatch order* (Change 3). §5 is the correctness argument.

**Out of scope, deliberately:** `PARALLEL_DATES` keeps its value of 5. The
user will tune it by hand as a separate follow-up once this lands, so the
new scheduler must keep it exactly as easy to tune as it is today — one
module-level constant, read inside `main()`, controlling both
`max_workers` and the in-flight ceiling. §7 lists this as a hard "must not
change".

---

## 1. Measured evidence

From the real GitHub Actions run on 2026-09-01 (the one that first ran
plan 002's merged code), cross-checked against plan 002 §1's runs, and
verified against the current source of `src/main.py`, `src/scraper.py`,
`src/price_log.py` and `tests/test_main.py`.

### 1.1 The fare-release horizon has now measured at 94 days three times

| Run date | Last date returning prices | First date failing | Implied bound |
| --- | --- | --- | --- |
| 2026-08-31 | 2026-12-03 (+94) | 2026-12-04 (+95) | bracketed to exactly 94 |
| 2026-09-01 07:41 | 2026-12-04 (+94) | 2026-12-08 (+98) | good at +94 |
| 2026-09-01 09:52 | 2026-12-04 (+94) | 2026-12-08 (+98) | good at +94 |

The 2026-08-31 run is the only one that brackets the boundary tightly (it
had a candidate at both +94 and +95). The two 2026-09-01 runs confirm
+94 is still good after the window rolled forward by a day, but their
next candidate after +94 is +98 (there is no Tue/Thu/Fri at +95, +96 or
+97 from a Tuesday), so on their own they only bound the horizon to
`(94, 98]`.

Taken together: **the horizon is 94 days, rolling daily, with zero
observed drift across three measurements over two days.**

Important caveat, and the reason this is not a certainty: all three
measurements were taken between 07:41 and 09:52 London time. The
scheduled workflow (`37 */6 * * *`) also fires at roughly 00:37, 06:37,
12:37 and 18:37 UTC. Nobody has measured the horizon from a 00:37 run, so
if NRE rolls its window forward at some point during the morning, the
true horizon at 00:37 could be 93, not 94. Nothing in this plan depends
on resolving that — see §5.2.

### 1.2 The 98-day margin cost 50 seconds on the 2026-09-01 09:52 run

```
2026-12-04  ... succeeded, real prices
2026-12-08  attempt 1/3 failed ... attempt 2/3 failed ... attempt 3/3 failed
            — all 3 attempt(s) failed
```

`2026-12-08` is `today + 98` — **exactly** `FULL_RETRY_HORIZON_DAYS`, so
`d <= full_retry_until` is true and it got the full three-attempt budget.
It is genuinely past the release horizon, so all three attempts failed
identically. Wall clock 09:52:39 → 09:53:29 = **~50s**, which is 40% of
the run's 123s scraping phase and the main reason the run took 2m27s
rather than plan 002 §10's projected ~1m47s.

That 50s matches plan 002's own per-attempt arithmetic exactly, which is
a useful confirmation the merged constants behave as designed:
`NAVIGATION_TIMEOUT_SECONDS` settle (~2s) + `PAGE_BUDGET_SECONDS` (10s) =
~12s per attempt, plus `RETRY_BACKOFF_SECONDS = (5, 10)`:
12 + 5 + 12 + 10 + 12 = **51s**.

By contrast, the batch of 5 dates genuinely beyond 98 days — correctly
getting `SPECULATIVE_ATTEMPTS = 1` each — cost **~14s in total for all
five**. The speculative-attempt mechanism works; the 98-day margin is
what is expensive.

### 1.3 Four of five workers sat idle for ~40s

`2026-12-08` shared a batch with four dates that each finished in ~5-10s.
The current code in `main()` is:

```python
outcomes = list(zip(batch, executor.map(_fetch_and_parse_one, batch, batch_attempts)))
```

`executor.map`'s result iterator yields in submission order and blocks
until each item is ready, and `list(...)` does not return until the whole
batch is consumed. The next batch is not submitted at all until that
returns. So the four fast workers had nothing to do for ~40s while the
straggler retried.

This is separate from Change 1 and survives it: **any** straggler in a
batch idles its batch-mates' workers. Plan 002 §1.5 established that
transient retries on genuinely in-range dates are real, expected and
load-bearing (2026-09-04 failed attempt 1 and succeeded on attempt 2), so
stragglers will keep happening even with a perfect horizon constant.

### 1.4 Two claims in the brief that the code does not support

Verified by reading the code, and both matter for the design:

1. **`price_log.append_price_log` is already called per date, not per
   batch.** It is inside the `for offset, (travel_date, outcome) in
   enumerate(outcomes)` loop in `main()`, once per successfully-parsed
   date, with that one date's two rows. There is no per-batch batching of
   CSV writes to preserve or change. See §4.4.

2. **Today's code already discards completed scrape results.** When
   `consecutive_failures >= MAX_CONSECUTIVE_FAILURES` fires, it `break`s
   out of the `for offset, ...` loop. Every remaining outcome in that
   batch — already fetched, already parsed, possibly containing a real
   sub-£10 fare — is silently dropped: not logged to `price-history.csv`,
   not added to `results`, not even counted in `failures`. Up to
   `PARALLEL_DATES - 1` dates per run. Fixing this is a side effect of
   Change 2 and is a strict improvement; see §4.3.

### 1.5 `ThreadPoolExecutor` semantics, checked rather than assumed

Three properties determine whether manual submission control is needed:

- **`Executor.map()` is eager.** It is implemented as
  `fs = [self.submit(fn, *args) for args in zip(*iterables)]` — every
  task is submitted to the executor's internal work queue before the
  iterator yields anything. Calling `map()` over the whole candidate list
  would therefore submit all ~100 dates immediately, and no amount of
  early-stopping while iterating results could prevent them running. This
  is why "just `map()` the whole list" is not an option, exactly as the
  brief says.
- **`Future.cancel()` only works before a worker picks the task up.**
  With `max_workers == PARALLEL_DATES` and a self-imposed ceiling of
  `PARALLEL_DATES` in-flight futures, there is never a queued-but-unstarted
  future to cancel: `ThreadPoolExecutor._adjust_thread_count` spawns a new
  worker on `submit()` until `max_workers` threads exist, so each of the
  first five submissions gets picked up essentially immediately, and every
  later submission only happens once a worker has freed up. **Cancellation
  is therefore useless here and must not be relied on.** Manual submission
  control is the only thing that provides the early-stop property.
- **`concurrent.futures.wait(fs, return_when=FIRST_COMPLETED)` returns
  immediately with every future that is already done**, not just one. It
  returns `(done, not_done)`. Calling it with an empty `fs` returns
  immediately with two empty sets, so the loop must guard against that.

- **`ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)`.** Returning
  from inside the `with` block therefore blocks until in-flight scrapes
  finish. This is already true today (the current code returns 1 from
  inside the block on `BlockedError`), so it is not a new property —
  but under the new scheduler there can be up to `PARALLEL_DATES - 1`
  in-flight futures at that moment instead of zero. See §4.5.

---

## 2. The key findings, stated plainly

1. **The 98-day margin is now the single most expensive thing in the
   run** (§1.2): ~50s, 40% of the scraping phase, for one date that is
   deterministically doomed and known in advance to be doomed.

2. **Fixed-size batching wastes the fix.** Even after Change 1 makes
   3-attempt failures rarer, one slow date still freezes four workers
   (§1.3). The batch boundary is an artificial synchronisation point that
   buys nothing — the only thing that genuinely needs ordering is
   *finalization* (failure counting and CSV logging), not *dispatch*.

3. **The whole design hinges on separating those two.** Once dispatch and
   finalization are decoupled, dispatch is free to be eager (Change 2)
   *and* free to be reordered (Change 3), while `MAX_CONSECUTIVE_FAILURES`
   and the price log keep byte-for-byte the same date-ordered semantics
   they have today.

---

## 3. Summary of changes

| # | Change | File | Effect |
| --- | --- | --- | --- |
| 1 | `FULL_RETRY_HORIZON_DAYS` 98 → 95 | `src/main.py` | ~38s on runs with a candidate in the old margin |
| 2 | Continuous queue scheduler replacing fixed batches | `src/main.py` | removes all idle-worker time behind stragglers |
| 3 | Dispatch the boundary-zone date first | `src/main.py` | ~16s on the ~3-in-7 runs that have one; ~16 fewer wasted NRE probes |
| 4 | Docs: `CLAUDE.md`, `.github/workflows/price-check.yml` comment | docs | keep them true |

Explicitly **unchanged**: `PARALLEL_DATES = 5`, `SPECULATIVE_ATTEMPTS = 1`,
`MAX_CONSECUTIVE_FAILURES = 5`, `attempts = 3` for in-range dates, every
constant in `src/scraper.py`, the candidate list's construction and
bounds, `evaluate()`, the TEST_RUN path, booked-date handling, the price
log format. See §7.

---

## 4. Design

All of this lives in `src/main.py`'s `main()`. It is orchestration logic
and must stay out of `src/scraper.py`, which knows about one date at a
time and nothing about scheduling.

### 4.1 Change 1 — `FULL_RETRY_HORIZON_DAYS = 98 → 95`

```python
FULL_RETRY_HORIZON_DAYS = 95
```

The constant's only effect is `attempts = 3 if d <= full_retry_until else
SPECULATIVE_ATTEMPTS`. It has never affected, and must never affect,
which dates are candidates.

**Why 95 and not something else.** Margin above the observed 94 only ever
buys one thing: tolerance to the horizon moving *further out* than
measured. It cannot help with the horizon being *closer* (§1.1's
rollover-timing caveat) — that case costs the same at any value ≥ 94.
So the question is purely how much a day of unused margin costs versus
what it insures against.

Cost of margin, per run. Candidates are Tue/Thu/Fri only, so ~3 of every
7 days out from `today` is a candidate. A candidate landing in the margin
zone `(94, FULL_RETRY_HORIZON_DAYS]` is doomed but gets 3 attempts, at
~51s instead of ~12s — **~39s of pure waste each**:

| Value | Margin days | Expected doomed 3-attempt dates per run | Expected waste |
| --- | --- | --- | --- |
| 98 (today) | 95, 96, 97, 98 | ~1.7 | ~66s |
| **95 (recommended)** | 95 | ~0.43 | **~17s** |
| 94 (zero margin) | none | 0 | 0s |

Cost of *insufficient* margin, if the horizon ever drifts outward: a
genuinely-released date beyond the constant gets one attempt instead of
three. Per plan 002 §4.3 that is bounded and self-correcting — the date is
still fetched, still parsed, still logged, still able to alert; only a
*transient* flake on that one attempt loses an observation, at an observed
flake rate of ~1 in 39 attempts, and the next run four to six hours later
retries it. And `MAX_CONSECUTIVE_FAILURES` remains the reactive backstop
for the opposite direction (plan 002 §4.4), untouched by this plan.

**95 is the recommendation.** It removes three of the four margin days —
about three-quarters of the measured waste — while keeping one day of
slack, so that a single day of outward drift (an operator releasing a day
early, or a boundary shift at a December/May timetable-change date) does
not silently demote a genuinely-released date to a single attempt. The
failure mode of too little margin is *quiet* (one attempt instead of
three, visible only by reading attempt counts in the log), whereas the
failure mode of too much is *loud and measurable* (a 50s line in the run
log, which is exactly how this was caught). Paying ~17s to keep the
failure mode on the loud side is worth it.

94 (zero margin) is a legitimate alternative and would be defensible on
the evidence — three consistent measurements, and the demotion cost is
genuinely small. It is recorded here as a one-character change if the user
prefers it. Do not go below 94; that would demote a date that all three
measurements say is released.

The comment block above the constant (currently `src/main.py` lines
36-48) must be updated to record: three consistent measurements of 94
(2026-08-31 bracketed to exactly 94, plus two 2026-09-01 runs), that all
three were taken mid-morning London time so the 00:37 run's horizon is
unmeasured, that 95 keeps exactly one day of margin, and — kept from the
existing comment — that this affects attempt count only and is not a cap
on which dates get checked.

### 4.2 Change 2 — the continuous scheduler

Replace the `for batch_start in range(...)` loop entirely. New imports:

```python
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
```

**State.** All indices are indices into `candidates` (which stays exactly
as built today: ascending, `MAX_DATES`-sliced).

| Name | Type | Meaning |
| --- | --- | --- |
| `order` | `list[int]` | indices into `candidates`, in dispatch order. Without Change 3 this is just `range(len(candidates))`; with it, see §4.6. A permutation — every index appears exactly once. |
| `cursor` | `int` | position in `order` of the next date to submit |
| `submitted` | `set[int]` | candidate indices that have been submitted |
| `in_flight` | `dict[Future, int]` | future → candidate index, for futures submitted but not yet harvested |
| `completed` | `dict[int, object]` | candidate index → outcome, harvested but not yet finalized (the reorder buffer) |
| `next_to_finalize` | `int` | candidate index of the next date to finalize, **strictly ascending** |
| `stop_submitting` | `bool` | latched once `MAX_CONSECUTIVE_FAILURES` fires |

`results`, `failures`, `consecutive_failures` and `full_retry_until` keep
their current meaning and initialisation.

**The loop.** Exact structure — the ordering of the four steps is
load-bearing, see the note after it:

```
with ThreadPoolExecutor(max_workers=PARALLEL_DATES) as executor:
    while True:
        # 1. REFILL — submit until the window is full or there's nothing left
        while (not stop_submitting
               and len(in_flight) < PARALLEL_DATES
               and cursor < len(order)):
            idx = order[cursor]; cursor += 1
            travel_date = candidates[idx]
            attempts = 3 if travel_date <= full_retry_until else SPECULATIVE_ATTEMPTS
            in_flight[executor.submit(_fetch_and_parse_one, travel_date, attempts)] = idx
            submitted.add(idx)

        # 2. DONE?
        if not in_flight:
            break

        # 3. HARVEST — block until at least one completes, take all that have
        done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
        for future in done:
            completed[in_flight.pop(future)] = future.result()

        # 4. FINALIZE — strictly in ascending candidate-index order
        while True:
            if next_to_finalize in completed:
                idx = next_to_finalize; next_to_finalize += 1
                <process candidates[idx] with completed.pop(idx)>   # see below
            elif stop_submitting and next_to_finalize not in submitted \
                    and next_to_finalize < len(candidates):
                # never submitted and never will be — skip over it so
                # later already-completed results can still be finalized
                next_to_finalize += 1
            else:
                break
```

Notes on why it is shaped this way:

- **Refill before wait, finalize last.** With refill at the top of the
  loop body and finalize at the bottom, a stop decided during finalization
  takes effect before any further submission. Putting refill after
  finalize instead would be equivalent; putting it *between* harvest and
  finalize would submit work the stop was about to forbid.
- **`wait(list(in_flight), ...)`** — snapshot the keys into a list; do not
  pass the dict view, which is mutated by the harvest loop.
- **`future.result()` is called bare, with no `try`.** `_fetch_and_parse_one`
  already converts `ScraperError` and `ParseError` into returned values,
  so `result()` only raises for a genuinely unexpected exception (a bug).
  Today `executor.map`'s iterator re-raises those out of `main()` and the
  run dies with a traceback and a non-zero exit; keep exactly that. Do
  not add a catch-all.
- **The `elif` gap-skip in step 4 exists only for Change 3.** With
  ascending dispatch, never-submitted indices are always a contiguous
  suffix, so `next_to_finalize` can never land on a gap. Change 3's
  reordering can leave a result for a high index in `completed` while a
  lower index was never submitted, and without the skip that already-paid-
  for result would be silently dropped. If Change 3 is dropped, drop the
  `elif` and the `submitted` set with it.
- **`PARALLEL_DATES` stays the single tuning knob**, read from the module
  global inside `main()` in exactly two places (`max_workers=` and the
  refill condition). It must remain monkeypatchable — several tests
  depend on that (§8).

**Per-date processing (step 4's `<process ...>`)** is the body of today's
`for offset, (travel_date, outcome)` loop, moved verbatim except for two
things:

- The `remaining` computation (see §4.4).
- Instead of `stop_early = True; break`, set `stop_submitting = True` and
  **keep going**. Finalization of everything already harvested continues.
  Guard the `_log_stopped_early` call with `if not stop_submitting:` so it
  is logged exactly once even though later dates may also fail.

Everything else — the `BlockedError`/`HijackedError` `return 1`, the
warning log, `failures.append`, `consecutive_failures = 0` on success,
`_log_target_summary`, `results[travel_date] = targets`, the
`price_log.append_price_log` call — is unchanged and stays in the same
order.

### 4.3 `MAX_CONSECUTIVE_FAILURES` semantics under the new scheduler

**Decision: counting stays strictly date-ordered, exactly as today.** This
is not a judgement call — plan 002 §1.7/§4.4's entire premise is that the
release horizon is a boundary *in date order*, so "5 in a row" only means
anything when "in a row" means "in ascending travel-date order".
Completion-order counting would be a different, untested mechanism that
could both false-trigger (five slow dates finishing together while an
earlier fast success is still unfinalized) and fail to trigger. The
reorder buffer exists precisely so this does not have to change.

Precise semantics, for the reviewer to check against:

1. `consecutive_failures` is only ever read or written inside step 4, on
   the main thread, in ascending `next_to_finalize` order. Its value
   sequence is therefore **identical** to what a fully serial run over
   `candidates` would produce. This is strictly *more* faithful than
   today, which only checks it after a whole batch completes.
2. Once the counter reaches `MAX_CONSECUTIVE_FAILURES`, `stop_submitting`
   latches and **no further date is ever submitted**. Refill is skipped
   for the rest of the run.
3. Futures already in flight at that moment (≤ `PARALLEL_DATES - 1`) are
   **allowed to finish naturally** and their results **are still
   finalized**: logged via `_log_target_summary`, added to `results`,
   appended to `price-history.csv`, and eligible to alert. Same for
   anything already sitting in `completed`. Nothing already scraped is
   discarded. This is a deliberate improvement over today's behaviour
   (§1.4 item 2), and the reason is the hard constraint: throwing away a
   successfully-scraped sub-£10 fare because a neighbouring date timed
   out is exactly the kind of silent coverage loss this plan is not
   allowed to introduce.
4. Failures finalized *after* the stop still increment the counter (which
   is now moot) but must not re-log `_log_stopped_early`. Successes after
   the stop still reset it, which is also moot. Neither can un-latch
   `stop_submitting`.
5. **Overshoot is larger than under batching, and unbounded in principle.**
   Today, at most `PARALLEL_DATES - 1` dates beyond the trigger have been
   dispatched (the rest of the trigger's batch). Under the new scheduler,
   a result sitting in `completed` frees its worker slot, so while the
   earliest unfinalized date is still running, dispatch keeps advancing.
   The practical bound is "however much work `PARALLEL_DATES - 1` workers
   can complete during the slowest unfinalized date", i.e. worst case
   ~4 workers × ~51s ÷ ~12s per doomed date ≈ **~16 extra dates**.

   This was considered carefully and accepted:
   - It costs **~0 wall clock** — those workers would otherwise be idle,
     which is the entire point of the change.
   - It cannot cause a date to be missed; it can only cause extra dates to
     be *checked*, and their data is kept, not discarded (point 3).
   - Per CLAUDE.md, request volume to NRE is not a known risk (no bot
     protection, 100+ requests per run already accepted).
   - Crucially, the scenarios `MAX_CONSECUTIVE_FAILURES` exists for do not
     produce this runaway. Both "the horizon moved closer" and "NRE is
     unavailable" make failures *homogeneous* in duration, so nothing
     completes early to free a slot and the overshoot collapses back to
     ~`PARALLEL_DATES`. The runaway needs a slow head plus fast
     subsequent failures, which is a mild, transient case.
   - Change 3 reduces it further in the one predictable case (§4.6).

   The obvious alternative — also capping `cursor - next_to_finalize` at
   `PARALLEL_DATES` — is **rejected**: it reintroduces precisely the
   head-of-line blocking this change exists to remove (a straggler at
   `next_to_finalize` would again idle every other worker), just relocated
   from the batch boundary to the reorder buffer.

### 4.4 `_log_stopped_early`'s "not attempted" count

New computation, at the moment the stop fires:

```python
_log_stopped_early(travel_date, len(order) - cursor, len(in_flight))
```

`len(order) - cursor` is exactly the number of candidate dates never
submitted to the executor, which is what the message claims. This stays
correct under Change 3's reordering, because `order` is a permutation of
all candidate indices — the never-submitted set is no longer a contiguous
tail of the date list, but its *size* is still `len(order) - cursor`.

Add a third argument for the in-flight count, since "some dates are still
running and will still be logged" is new, observable behaviour a human
reading the log needs to understand. Signature becomes
`_log_stopped_early(last_failed_date, remaining_count, in_flight_count)`;
extend the message with something like `"; %d already in flight will be
allowed to finish and are still logged"`. Keep the existing three pieces
of information (last failed date, `MAX_CONSECUTIVE_FAILURES`,
`FULL_RETRY_HORIZON_DAYS`) exactly as they are. No test asserts on this
message — verified across `tests/test_main.py`; the only `caplog` use is
`test_zero_trains_returned_no_alert_warning_logged`, which asserts nothing
about content.

### 4.5 `price_log.append_price_log` — no change

Contrary to the brief, it is already per-date (§1.4 item 1). It stays
exactly where it is, in the per-date processing body, called once per
successfully-parsed date with that date's rows.

This is not incidental — it must stay in step 4, which runs on the **main
thread in ascending date order**. Two consequences to preserve:

- The CSV is appended in ascending travel-date order within a run, exactly
  as today. A reader diffing `price-history.csv` after this change should
  see no structural difference.
- There is no file-locking or interleaving hazard, because
  `append_price_log` is never called from a worker thread. **Do not move
  it into `_fetch_and_parse_one`** as an "optimisation"; it opens the file
  in append mode with no lock and would race.

### 4.6 Change 3 — dispatch the boundary-zone date first

**The idea.** Classic longest-processing-time-first scheduling. The one
date that is *predictably* slow is the latest candidate still inside
`FULL_RETRY_HORIZON_DAYS`: it gets 3 attempts, and if the real horizon is
a day or two closer than the constant assumes, it burns all three (~51s)
and fails. In ascending dispatch order it is dispatched near the *end* of
the useful work, so its 51s extends the tail. Dispatched first, its 51s
overlaps the bulk of the run.

**The rule** — at most one date is reordered:

```python
# The candidate most likely to spend the full 3-attempt budget and still
# fail is the latest one still inside FULL_RETRY_HORIZON_DAYS: nearer
# dates reliably succeed on attempt 1, and dates past the horizon only
# ever get SPECULATIVE_ATTEMPTS regardless. Dispatching it first overlaps
# its ~51s worst case with the bulk of the run instead of appending it to
# the tail. Zone-gated so that short candidate lists (a manual max_dates
# run, or the last weeks of the school year) are left in plain ascending
# order — reordering only kicks in when there really is a boundary date.
# Set to 0 to disable the reordering entirely.
BOUNDARY_PRIORITY_ZONE_DAYS = 7


def _dispatch_order(candidates: list[date], full_retry_until: date) -> list[int]:
    """Indices into `candidates`, in the order they should be submitted.

    A permutation of range(len(candidates)) — dispatch order only; every
    result is still finalized in ascending date order (see main()).
    """
    zone_start = full_retry_until - timedelta(days=BOUNDARY_PRIORITY_ZONE_DAYS)
    boundary = [i for i, d in enumerate(candidates) if zone_start < d <= full_retry_until]
    first = boundary[-1:]          # at most one; [] when the zone is empty
    return first + [i for i in range(len(candidates)) if i not in set(first)]
```

Properties worth checking in review:

- `BOUNDARY_PRIORITY_ZONE_DAYS = 0` makes `zone_start == full_retry_until`,
  the strict `<` makes `boundary` empty, and the function returns the
  identity order. A clean kill switch, and a clean way for tests to opt
  out.
- Exactly one date is ever moved. This is deliberate. Moving the whole
  full-retry portion into descending order (the obvious "sort by
  descending days out" reading) is **rejected**: it would dispatch the
  earliest, most-likely-to-succeed dates *last*, which means
  finalization — and therefore `MAX_CONSECUTIVE_FAILURES` — could not
  fire until nearly the entire full-retry set had been dispatched. In the
  "NRE is unavailable" scenario that turns a ~50s early stop into a
  ~400s full sweep. That is a serious regression in exactly the scenario
  the backstop exists for. Moving *one* date costs one worker slot and
  cannot have that effect.
- The moved date is always attempted, even if the early stop would
  otherwise have prevented it. That is one extra doomed date's worth of
  work, running concurrently with useful work, so ~0 wall clock. Document
  it in the constant's comment.
- It composes with §4.2 by construction: the scheduler already indexes
  everything by candidate index and finalizes by index, so dispatch order
  is a free variable. The only extra machinery is the `submitted` set and
  the gap-skip `elif` (§4.2).

**Expected benefit, modelled on the 2026-09-01 run's numbers** (~34 useful
dates at ~6s each ≈ 224 worker-seconds; boundary date 51s; speculative
probes ~12s each; `PARALLEL_DATES = 5`):

| | Without Change 3 | With Change 3 |
| --- | --- | --- |
| Boundary date dispatched at | t ≈ 36-40s (its position in the list) | t = 0 |
| Boundary date finalized at | t ≈ 91s | t ≈ 55s (waits for earlier dates) |
| Stop fires at | t ≈ 91s | t ≈ 67s |
| Scraping phase ends | **~95s** | **~79s** |
| Wasted speculative probes dispatched during the stall | ~16 | ~0 |

So: **~16s on runs that have a doomed boundary date, ~0s otherwise.**
Because `today + 95` has a fixed weekday for a given run day, roughly 3
run-days in 7 have a Tue/Thu/Fri candidate at exactly +95; the other 4 in
7 get nothing from this change. Average ≈ 7s/run. The secondary benefit —
~16 fewer pointless requests to NRE on those runs — is arguably worth as
much as the seconds.

Note this is genuinely the smallest of the three changes by expected
value, and it is the one to drop if it complicates the scheduler in
practice. See §9.1.

### 4.7 Interaction between the three changes — they are not additive

Worth stating so nobody double-counts the projections:

- Change 1 removes the *cause* of the observed 50s on 2026-09-01
  (`2026-12-08` at +98 drops from 3 attempts to 1). On that specific run
  day, `today + 95 = 2026-12-05` is a Saturday, so after Change 1 there is
  no doomed full-retry boundary date at all, and Change 3 would have
  nothing to do.
- Change 2 removes the *amplification* (four idle workers), which Change 1
  alone does not fix for transient in-range retries.
- Change 3 removes the *tail placement* of whatever 3-attempt failures
  remain after Change 1.

Rough projection for the 2026-09-01 run's 123s scraping phase: ~85s after
Change 1 alone, ~75s with Change 2 as well, and unchanged by Change 3 on
that particular date. On a run day where +95 *is* a Tue/Thu/Fri, expect
roughly 95s → 79s from Change 3. Total job time should land around
1m50s-2m rather than 2m27s. These are estimates from the per-attempt
arithmetic in §1.2, not measurements — verify per §10.

---

## 5. Correctness argument

Plan 002 §4's four properties are the acceptance criteria and all still
hold. Restating them against this plan's changes:

### 5.1 Property 1 — nothing here can cause a permanent skip

`main()` still derives `candidates` from scratch on every invocation, from
`today + 1` to `term_dates.LAST_KNOWN_DATE`, with no new bound, no
persistence, no cross-run memoisation. Change 1 alters attempt count only.
Change 2 alters *when* a date is submitted. Change 3 alters *in what
order*. None of them removes a date from the list. The line

```python
candidates = term_dates.checkable_dates(today + timedelta(days=1), term_dates.LAST_KNOWN_DATE)
```

is untouched, as is the `config.MAX_DATES` slice below it.

### 5.2 Property 2 — the horizon constant still carries margin

`FULL_RETRY_HORIZON_DAYS = 95` is one day past the observed 94, itself
measured three times with zero drift (§1.1). §4.1 gives the full
cost/benefit. The constant affects retry budget only, so being wrong in
either direction has the bounded, self-correcting cost described in plan
002 §4.3, never a coverage loss.

### 5.3 Property 3 — every date is still genuinely checked and logged

Strengthened, not weakened, by this plan:

- Every candidate that gets submitted is fetched, parsed, logged and
  eligible to alert, exactly as before.
- Dates that were previously *scraped and then thrown away* when the early
  stop fired mid-batch (§1.4 item 2) are now finalized and logged
  (§4.3 point 3).
- Change 3 requires the gap-skip in step 4 specifically so that a
  priority-dispatched result is never stranded unlogged (§4.2).

### 5.4 Property 4 — the reactive backstop stays

`MAX_CONSECUTIVE_FAILURES = 5` keeps its value and its date-ordered
counting semantics (§4.3). Its trigger point is unchanged; only what
happens to already-dispatched work afterwards is improved. Do not lower
it, and do not change it to count in completion order.

---

## 6. Considered and rejected

- **`executor.map()` over the whole candidate list.** Eager submission
  (§1.5) destroys the early stop entirely: by the time five consecutive
  failures are observed, every remaining doomed date is already queued and
  will run.
- **Cancelling queued futures instead of controlling submission.** With
  the in-flight window equal to `max_workers`, there is never an
  unstarted future to cancel (§1.5). `Future.cancel()` would return
  `False` essentially always.
- **Counting `MAX_CONSECUTIVE_FAILURES` in completion order.** §4.3.
- **Capping the reorder-buffer lookahead** (`cursor - next_to_finalize <=
  PARALLEL_DATES`). Reintroduces head-of-line blocking. §4.3 point 5.
- **Sorting the whole full-retry portion by descending days-out for
  dispatch.** Delays the early stop catastrophically in the NRE-down
  scenario. §4.6.
- **Prioritising more than one boundary date** (e.g. the last
  `PARALLEL_DATES - 1`). Ties up most of the worker pool at t=0; in the
  NRE-down case it leaves only one or two workers for the early dates and
  delays the stop several-fold. One date captures nearly all the benefit.
- **Moving `append_price_log` into the worker thread.** §4.5 — unlocked
  append-mode writes from multiple threads.
- **Changing `PARALLEL_DATES`.** Explicitly out of scope; the user is
  tuning it by hand next (§7).
- **Anything in `src/scraper.py`.** Its constants were tuned to measured
  values in plan 002 and there is no new evidence about them. The 51s
  figure in §1.2 confirms they behave exactly as designed.

---

## 7. What must not change

- `PARALLEL_DATES = 5`, and it must remain a single module-level constant,
  read inside `main()`, controlling both `max_workers` and the in-flight
  ceiling — nothing else may need editing to change the concurrency level,
  and it must remain monkeypatchable from tests.
- `MAX_CONSECUTIVE_FAILURES = 5` and `SPECULATIVE_ATTEMPTS = 1`.
- `attempts = 3` for in-range dates (plan 002 §1.5 / §7.2).
- Every constant and behaviour in `src/scraper.py`.
- The candidate list's construction and bounds, and the `config.MAX_DATES`
  slice.
- `price_log.append_price_log` is still called for **every** date that
  scrapes and parses successfully — booked or not, alertable or not,
  speculative-zone or not, before/after the early stop or not — from the
  main thread, in ascending date order.
- Booked dates still scraped and logged, only excluded from `evaluate()`.
- `if not results: return 1`; the `failures` summary warning; `evaluate()`;
  `_best_effort_matches_for_test()`; the `TEST_RUN` path; the
  `BlockedError`/`HijackedError` whole-run abort returning 1.
- No new dependency, no new file, no new env var, no new secret. The only
  new module-level names are `BOUNDARY_PRIORITY_ZONE_DAYS` and
  `_dispatch_order` (Change 3 only).

---

## 8. Test impact

Read `tests/test_main.py` in full for this section; the findings below are
from the actual file, not inferred.

### 8.1 The root cause of most test breakage

The current tests lean on one guarantee that the new scheduler removes,
stated explicitly in `_install_fake_scraper`'s docstring (lines 70-77):

> "batch N+1 is never dispatched until batch N has fully finished"

Under a continuous scheduler, a date from "the next batch" can be
dispatched as soon as **any** single earlier future completes. With the
fakes returning instantly, whether that happens is a genuine thread race
(does `wait()` return with one done future or five?), so tests asserting
on it become **flaky, not merely wrong**.

**Recommended fix for all affected tests: `monkeypatch.setattr(main,
"PARALLEL_DATES", 1)`.** With a window of one, the scheduler degenerates
to strictly serial submit → wait → finalize, which makes dispatch order
fully deterministic *and* makes the assertions stronger (exact list
equality instead of set equality). It also directly exercises the
requirement that `PARALLEL_DATES` remains a live, monkeypatchable knob.
Concurrency itself is then covered by the dedicated new tests in §8.4.

### 8.2 Must update — named, with the reason for each

1. **`_install_fake_scraper`'s docstring** (lines 70-77) — describes batch
   dispatch, which no longer exists. Docs-only, but it is the thing that
   will mislead the next reader.

2. **`test_five_consecutive_failed_dates_stops_early`** (line 278) —
   asserts `set(fetch_calls) == {d1..d5}`, i.e. d6 is never attempted.
   All six dates are within `FULL_RETRY_HORIZON_DAYS`, so with a window of
   5, d6 gets submitted the moment any of d1-d5 completes. Race. Fix: add
   `monkeypatch.setattr(main, "PARALLEL_DATES", 1)` and tighten the
   assertion to `fetch_calls == [d1, d2, d3, d4, d5]` (ordered). Update
   the comment, which explains the batch reasoning.

3. **`test_a_success_between_failures_resets_the_consecutive_count`**
   (line 314) — asserts `set(fetch_calls[:5]) == set(dates[:5])` and
   `set(fetch_calls[5:]) == set(dates[5:])`, i.e. a hard batch boundary
   after the first five. Same race. Fix: the property actually under test
   is "never stops early, so every candidate is attempted" — replace both
   assertions with `set(fetch_calls) == set(dates)` and
   `len(fetch_calls) == len(dates)`. That is scheduler-agnostic and robust
   under any concurrency, so this one does **not** need
   `PARALLEL_DATES = 1`.

4. **`test_five_consecutive_parse_failures_also_stops_early`** (line 346)
   — only asserts `result == 1`, which looks safe but is not. It gets 1
   today solely because d6 (which would succeed) is never dispatched, so
   `results` is empty. If d6 is dispatched and finalized under the new
   scheduler, `results` is non-empty and the function returns **0**. Fix:
   `monkeypatch.setattr(main, "PARALLEL_DATES", 1)`.

5. **`test_blocked_error_aborts_before_the_next_batch`** (line 397) —
   asserts `dates[5]` and `dates[6]` are never fetched. With a window of
   5, if `dates[1..4]` complete before `dates[0]` is harvested, refill
   submits `dates[5]` and `dates[6]`. Race. Fix: `PARALLEL_DATES = 1`,
   assert `fetch_calls == [dates[0]]`, rename to
   `test_blocked_error_aborts_the_run`, rewrite the batch-centric comment.

6. **`test_hijacked_error_aborts_before_the_next_batch`** (line 416) —
   identical treatment to 5.

7. **`_seven_dates()`** (line 385) — its per-line comments say "in the 2nd
   batch — must never be attempted". Reword; the list itself is fine.

8. **`test_scraper_fails_on_one_date_others_still_checked`** (line 242) —
   the assertion (`set(fetch_calls) == {d1, d2, d3}`) is already
   scheduler-agnostic and safe; only its comment mentions batches. Comment
   fix only.

### 8.3 Verified safe as-is — do not edit

Each checked against the new scheduler and, where relevant, against
Change 3's reordering rule:

- `test_summer_holidays_returns_0_no_scraper_no_notifier` — no candidates.
- Every `MAX_DATES = 1` or `= 2` alerting test
  (`test_one_date_cheap_railcard_fare_sends_one_match`,
  `test_price_exactly_threshold_does_not_alert`,
  `test_price_just_under_threshold_alerts`,
  `test_both_trains_sold_out_no_alert`,
  `test_zero_trains_returned_no_alert_warning_logged`,
  `test_notifier_raises_returns_1`,
  `test_sub_threshold_price_without_railcard_confirmation_still_sends_alert`)
  — one or two candidates, no stop, no reordering.
- `test_scraper_fails_on_every_date_returns_1_no_email` — two failures,
  below the threshold of 5.
- All six `TEST_RUN` tests — one or two candidates.
- `test_booked_date_is_scraped_and_logged_but_never_alerted`,
  `test_all_candidates_booked_still_scraped_but_no_alert`,
  `test_missing_booked_dates_file_behaves_as_empty`.
- `test_today_is_derived_from_europe_london` — `checkable_dates` is stubbed
  to return `[]`; never reaches the scheduler.
- `test_today_before_last_known_date_leaves_exactly_one_candidate`.
- `test_max_dates_one_with_many_real_candidates_checks_exactly_one`.
- `test_first_day_of_autumn_term_has_102_candidates` — asserts a count,
  not an order; all 102 fakes succeed so nothing stops early.
- `test_speculative_zone_dates_get_a_single_attempt` — the two stubbed
  candidates are `today + 1` and `today + FULL_RETRY_HORIZON_DAYS + 1`.
  It reads `main.FULL_RETRY_HORIZON_DAYS` rather than hardcoding 98, so
  Change 1 does not touch it, and neither date falls in Change 3's
  priority zone.
- `test_speculative_zone_dates_are_still_checked_and_logged` — one
  candidate, beyond the horizon. **Note for the coder:** its comment says
  "98 days out from TERM_TIME_DAY ... lands in mid December — outside BST"
  and hand-builds a `+00:00` journey for that reason. With
  `FULL_RETRY_HORIZON_DAYS = 95` the date becomes 2026-12-08 instead of
  2026-12-11 — still December, still GMT, so the test still works, but
  update the "98 days" wording in the comment.
- `test_successful_date_appends_to_price_log`,
  `test_failed_date_does_not_append_to_price_log`.
- All `evaluate()` unit tests — pure function, untouched.

Also verified: **no test in the file asserts on `_log_stopped_early`'s
message text**, so §4.4's rewording and extra argument are free.

`tests/test_scraper.py` and every other test module: **no change
expected** — nothing in this plan touches `src/scraper.py`.

### 8.4 Must add

Four new tests. Two of them need deterministic control over completion
order; the mechanism for that is a `threading.Event` used as a gate
*inside the fakes* (never a `sleep`), so there is no wall-clock timing
dependency and no flakiness.

1. **`test_dispatch_starts_a_new_date_before_the_window_drains`** — the
   direct test of Change 2's whole purpose. `PARALLEL_DATES = 2`, four
   candidates `c0..c3`, all succeeding. `c0`'s fake blocks on
   `gate.wait(timeout=5)` and records the boolean result; `c2`'s fake
   calls `gate.set()`. Assert the recorded boolean is `True` — i.e. `c2`
   was dispatched while `c0` was still running. Under the old batch
   scheduler this would time out and record `False`. (Trace: `c0`, `c1`
   submitted; `c1` completes and is harvested into the reorder buffer,
   freeing a slot; refill submits `c2`; `c2` sets the gate; `c0`
   unblocks.) Use a generous timeout so a genuine failure costs 5s, not a
   hang.

2. **`test_stop_early_still_finalizes_results_already_in_flight`** — the
   direct test of §4.3 point 3 and of the §1.4-item-2 behaviour change.
   `PARALLEL_DATES = 2`; candidates `c0..c5` where `c0..c4` raise
   `ScraperError` and `c5` returns a real sub-threshold fare. `c4`'s fake
   blocks on the gate; `c5`'s fake sets it. So `c5` is dispatched and
   completes while `c4` is still failing; `c4` then finalizes as the
   fifth consecutive failure and latches the stop; `c5`'s already-present
   result must still be finalized. Assert: `result == 0`, `c5`'s date
   appears in the isolated price log, and exactly one alert was sent for
   `c5`.

3. **`test_boundary_zone_date_is_dispatched_first`** (Change 3 only) —
   `PARALLEL_DATES = 1` so dispatch order is observable directly. Stub
   `term_dates.checkable_dates` to return a handful of near-term dates
   plus one at `today + FULL_RETRY_HORIZON_DAYS`. Assert
   `fetch_calls[0] == that boundary date`, and that the remaining calls
   are in ascending date order. Add a companion assertion (or a second
   test) that with `monkeypatch.setattr(main, "BOUNDARY_PRIORITY_ZONE_DAYS",
   0)` the order is plain ascending — proving the kill switch.

4. **`test_dispatch_never_exceeds_parallel_dates_in_flight`** — a cheap
   invariant check: the fake increments a counter on entry and decrements
   on exit (under a `threading.Lock`), recording the maximum. With
   `PARALLEL_DATES = 3` and ~12 candidates, assert the maximum never
   exceeded 3. Only asserts an upper bound, so it cannot flake.

Consider also a `_dispatch_order` unit test (pure function, no threads):
empty list, single candidate, no candidate in the zone, several in the
zone (latest wins), `BOUNDARY_PRIORITY_ZONE_DAYS = 0`, and that the result
is always a permutation of `range(len(candidates))`. Cheap and it pins the
one piece of Change 3 that is easy to get subtly wrong.

Full suite must pass: `python -m pytest`.

---

## 9. Open decisions for a human

### 9.1 Is Change 3 worth it? (the one genuinely open question)

Everything else in this plan is resolved. Change 3 is the marginal call,
so here it is stated plainly rather than decided silently:

- **For:** ~16s on the ~3-in-7 run days that have a doomed boundary date,
  ~16 fewer wasted NRE probes on those days, and it is the only part of
  the design that addresses tail placement at all. ~15 lines: one
  constant, one pure function, one `set`, one `elif`. Zero correctness
  surface — finalization order is untouched, and §8.3 confirms it breaks
  no existing test.
- **Against:** it is the smallest of the three wins by expected value
  (~7s/run averaged over the week, versus Change 1's ~38s on the observed
  run). It makes the run log's dispatch order non-obvious to a human — a
  December date's `attempt 1/3: starting` line appears among September
  ones, which reads like a bug until you know the rule. And it is the sole
  reason the scheduler needs the `submitted` set and the gap-skip branch.
- **Recommendation: implement it**, because the code cost is genuinely
  small and confined to building one list, and because the reduction in
  wasted requests to NRE is worth about as much as the seconds. But
  implement it **last, as a separable commit**, so it can be dropped
  without touching Changes 1 and 2 if the reviewer finds the gap-skip
  branch harder to justify than the ~16s it buys.

### 9.2 `FULL_RETRY_HORIZON_DAYS`: 95 or 94?

Recommending 95 (§4.1). 94 is defensible on the same evidence and saves a
further ~17s/run. This is a one-character change if the user prefers zero
margin. Not blocking.

---

## 10. Docs to update (Change 4)

- **`CLAUDE.md`, "Tech decisions → Concurrency"** (lines 90-105). Two
  edits: replace "`ThreadPoolExecutor` batching candidates in fixed-size
  groups" with a description of the continuous scheduler (a rolling window
  of up to `PARALLEL_DATES` in-flight scrapes, refilled the instant any
  one finishes, with results finalized — logged, counted toward
  `MAX_CONSECUTIVE_FAILURES` — strictly in travel-date order regardless of
  the order they complete in); and update `FULL_RETRY_HORIZON_DAYS` from
  98 to 95 days, noting the horizon has now measured at exactly 94 three
  times. If Change 3 lands, add one sentence on boundary-first dispatch.
- **`CLAUDE.md`, line ~100** — "not needed once dates are batched
  concurrently" — reword away from "batched".
- **`CLAUDE.md`, line ~163** — "dates are now checked
  `PARALLEL_DATES`-at-a-time" is still true in substance (that is the
  concurrency ceiling) but "rather than strictly one at a time" now
  understates it; adjust to say up to `PARALLEL_DATES` at once,
  continuously scheduled.
- **`.github/workflows/price-check.yml`, lines 42-55** — the
  `timeout-minutes` comment cites plan 002's ~107s projection and says
  "scraped PARALLEL_DATES-at-a-time". Update the projection reference to
  this plan and reword the batching phrase. **Keep `timeout-minutes: 20`**
  — it still has ample headroom and nothing here justifies retuning it.
- **`docs/plans/001-*.md` and `docs/plans/002-*.md` are historical
  records and must not be edited.** This plan amends them by reference,
  the same way 002 amended 001.
- **`README.md`** — grep for "batch"/"batches" before editing; if the word
  does not appear, no change is needed. Do not invent one.

---

## 11. Verification after merge

Trigger `workflow_dispatch` with `max_dates = all` and check the run log
for:

1. **Change 1** — no date more than ~95 days out shows `attempt 2/3` or
   `attempt 3/3`. Any `all N attempt(s) failed` line beyond +95 must show
   `all 1 attempt(s) failed`.
2. **Change 1's risk** — the last date returning real prices should still
   be ~`today + 94`. If it is materially *further* out than +95, the
   horizon has drifted outward and `FULL_RETRY_HORIZON_DAYS` should be
   raised; that is the one thing this change could get wrong, and it is
   visible right here.
3. **Change 2** — `attempt 1/N: starting` lines should no longer come in
   clean groups of five separated by pauses. Expect interleaving; every
   line is date-prefixed, so it stays readable. Specifically, look for a
   new date starting while an earlier one is still between attempts.
4. **Change 2's invariant** — the `[date] 07:25: £X` summary lines and the
   rows appended to `price-history.csv` must still be in strictly
   ascending travel-date order, even though the scrape logs are not.
5. **Change 3** — if a candidate lands at `today + 95`, its
   `attempt 1/3: starting` should be the **first** such line of the run,
   well before the September dates.
6. **Early stop** — if `consecutive dates failed — stopping early` appears,
   check that the dates named as still in flight do appear afterwards in
   `price-history.csv` (§4.3 point 3), and that no date starts after it.
7. Total job time should be roughly 1m50s-2m, down from 2m27s.
