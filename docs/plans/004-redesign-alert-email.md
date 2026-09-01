# Plan 004 — Redesign the price-alert email: two site-styled tables

Status: **ready to implement.** All three open decisions have been
resolved by the user (2026-09-01) and are folded into the design:

- **§9.1 — "only trigger when there are new cheap trains" reads as "same
  trigger as today".** Confirmed explicitly by the user before this plan
  was even first drafted ("Proceed with the existing-behavior"). No
  repeat-alert-suppression mechanism is implemented; see §9.1 for the full
  reasoning and the cleanly-separable follow-up path if that's ever wanted.
- **§9.2 — no `[TEST]` subject prefix.** Confirmed; matches what this
  plan already specified. No design change.
- **§9.3 — arrival time and direct/changes are KEPT, not dropped.** This
  reversed the plan's original recommendation and did require real design
  work, since a row is now one *date* carrying *two* departures (and so
  potentially two different arrival times and two different direct
  statuses). Resolved by putting that information **inside each
  departure's own cell as a second, muted line**, not as extra columns —
  see §4.6, §4.7 and §4.8.

The user's request, verbatim:

> I want the email to be formatted like the website and include two
> tables: One that shows the dates for which a train has already been
> booked (do include the current prices for these too) and one that shows
> the rows with 7.25 and 7.30 like the table on the website for which at
> least one of them is below GBP10. I still want the email to only
> trigger when there are new cheap trains. Make the email look very nice.

This is a **presentation-layer change only**. Nothing about which dates
get scraped, when the job runs, how prices are parsed, what gets written
to `price-history.csv`, or **when an email is sent** changes. §7 is the
"must not change" list; §5 is the correctness argument for the one place
where behaviour genuinely does change shape (what `send_alert` receives).

---

## 1. What the code actually does today (read, not assumed)

Every claim below was verified against the current source on
2026-09-01.

### 1.1 The current email is one flat table of `(date, departure)` pairs

`src/notifier.py` today:

```python
def send_alert(matches: list[AlertMatch], secrets: config.Secrets, *, dry_run: bool = False) -> None
```

`_build_html_body` renders exactly one table with columns
`Date | Departs | Arrives | Price | Direct? | 16-25 Railcard`, one `<tr>`
per `AlertMatch`, capped at `MAX_TABLE_ROWS = 20`. `AlertMatch` is one
`TrainOption` that beat the threshold — so **a date where both 07:25 and
07:30 are cheap produces two separate rows**, and a date where only 07:30
is cheap shows no 07:25 information at all. That is precisely the shape
the user is asking to replace with the website's per-date row.

Note the consequence for §4.6: because today's row is one *fare*,
`Arrives` and `Direct?` are unambiguous single-valued columns. Under the
new per-date row they are not — that is the design problem §4.6 solves.

The HTML is `<table border="1" cellpadding="4" cellspacing="0">` with no
styling whatsoever — no colours, no fonts, no layout. There is no
`<style>` block today, so nothing is lost by continuing not to have one.

### 1.2 `main()` already has everything the new email needs

`src/main.py` builds, over the run:

```python
results: dict[date, dict[str, TrainOption | None]]   # every date scraped this run
booked: set[date]                                     # booked_dates.load_booked_dates(...)
alertable_results = {d: t for d, t in results.items() if d not in booked}
matches = evaluate(alertable_results)                 # list[AlertMatch]
```

- `results[d]` is `parser.select_target_trains(...)`'s return value, which
  is **keyed by every entry of `config.TARGET_DEPARTURES`**, value `None`
  when that target departure was absent from the response. So
  `results[d]["07:25"]` and `results[d]["07:30"]` always exist as keys.
- Each value is a full `TrainOption`, carrying its own `arrival_time`,
  `is_direct`, `railcard_applied`, `sold_out` and `price` — so the
  per-departure detail §4.6 needs is already in memory, per departure.
- Booked dates **are** in `results` — CLAUDE.md's design is that booked
  dates are still scraped and logged, only excluded from alerting
  (`alertable_results`). So "current prices for booked dates" is already
  sitting in memory at the point `send_alert` is called; no CSV read, no
  extra scrape, no new work is needed to satisfy the first table.
- `matches` gates the email:
  `if not matches: logger.info(...); return 0` — no email.

This means the whole feature is a rendering change plus a wider argument
list. There is no new data acquisition anywhere in this plan.

### 1.3 The website's table semantics, as actually implemented

From `site/app.js` `renderTable()` and `site/style.css`:

- Columns per term section: `Date | Day | 07:25 | 07:30 | Booked?`, where
  the price headers come from `termsData.target_departures`.
- `Date` is the raw ISO string (`2026-09-11`); `Day` is the full weekday
  name (`Friday`).
- Cell content comes from `formatLatestCell(row)`:
  | condition | rendered |
  | --- | --- |
  | no CSV row at all | `–` |
  | `sold_out === "True"` | `sold out` |
  | no `actual_departure` | `not found` |
  | no `price_gbp` | `–` |
  | otherwise | `£X.XX` |
- Row highlight (`rowHasCheapFare` + CSS): green `--cheap-bg: #dafbe1` if
  any target's price is `< threshold`; blue `--booked-bg: #ddf4ff` if
  booked; **booked wins when both apply**.
- Palette: `--border #d0d7de`, `--accent #1a7f37`, `--bg-muted #f6f8fa`,
  `--text-muted #57606a`, body text `#1f2328`, font stack
  `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`.
- Hover colours (`--cheap-bg-hover`, `--booked-bg-hover`) exist only for
  `:hover`, which is meaningless in email — they are dropped, deliberately.

The site shows **only** a price per cell — it has no arrival or
direct/changes information at all, because `price-history.csv` carries
`arrival_time` but the site never renders it and `is_direct` is not even a
CSV column (§9.4). The email will therefore show slightly *more* per cell
than the site does (§4.6). "Formatted like the website" is honoured at the
level of layout, palette and per-date row shape, which is what the request
is about.

### 1.4 A documentation/code discrepancy worth fixing here

`src/config.build_journey_planner_url`'s docstring says it is:

> shared by `src.scraper` (anchored at the earliest of `TARGET_DEPARTURES`
> …) and `src.notifier` (anchored at each alerted train's own departure
> time, **for the email's per-fare link**).

`src/notifier.py` never imports or calls it — grepped across the repo, the
only callers are `src/scraper.py:167` and its own test. So the email has
never had the per-fare link its own config docstring advertises. This
redesign is the natural moment to make that docstring true (§4.6), and it
directly serves "make the email look very nice": a price you can click
straight through to a bookable NRE search.

### 1.5 The published site URL

`site/app.js` sets `OWNER = "rosjo99"`, `REPO = "Train-prices"`;
`.github/workflows/deploy-pages.yml` publishes `site/` to GitHub Pages
with no custom domain; `README.md` line 19 states the address as
`https://rosjo99.github.io/Train-prices/`. That is the URL the email
links to (§4.7), pinned as one module constant so it is a one-line edit
if Pages ever moves.

### 1.6 Realistic table sizes

`booked-dates.txt` currently holds 8 dates, all inside Autumn Term 2026,
and will grow through the year. A run early in a term enumerates 100+
candidate dates (CLAUDE.md, "Which dates get checked"). So both tables
are plausibly tens of rows, and both need their own cap — a single shared
cap would let a long booked list crowd out the cheap fares that are the
actual point of the email.

---

## 2. The design in one paragraph

`main()` keeps deciding *whether* to email (unchanged: `evaluate()` found
at least one match, or the `TEST_RUN` fallback produced one) and starts
also deciding *what goes in each table* — two `list[DateRow]`, built from
this run's own in-memory `results`. `src/notifier.py` keeps doing only
rendering and delivery, and gains a second table plus a full inline-styled
HTML design that mirrors `site/`'s palette and column shape, with each
departure's price, arrival time and direct/changes status packed into that
departure's own cell. No new dependency, no new persistence, no CSV
reading in the notifier.

---

## 3. Summary of changes

| # | File | Change |
| --- | --- | --- |
| 1 | `src/models.py` | Add `DateRow` (one travel date + its target-departure options). |
| 2 | `src/main.py` | Build `cheap_rows` and `booked_rows`; pass them (plus `test_summary`) to `send_alert`. |
| 3 | `src/notifier.py` | New signature; two tables; two-line cells (price + arrival/changes); full inline-styled HTML; rewritten text body; adjusted subject; per-fare deep links; per-table caps. |
| 4 | `tests/test_notifier.py` | Rework helpers/call sites; new rendering tests. |
| 5 | `tests/test_main.py` | Rework the fake notifier and the assertions that reach into `matches`; add booked/cheap-split integration tests. |
| 6 | `CLAUDE.md`, `README.md`, `src/config.py` docstring | Small doc updates (§10). |

---

## 4. Design, file by file

### 4.1 `src/models.py` — add `DateRow`

```python
@dataclass(frozen=True)
class DateRow:
    """One travel date as one row of an alert-email table: the same
    per-date, per-target-departure shape the booked-dates website renders
    (see site/app.js renderTable), rather than src.main.evaluate()'s
    per-fare AlertMatch. Produced by src.main, consumed only by
    src.notifier.

    `options` is exactly one entry of src.main's `results` dict — keyed by
    every string in config.TARGET_DEPARTURES (so both "07:25" and "07:30"
    are always present as keys), value None when that departure was absent
    from the scraped response. It is a dict, so a DateRow must never be
    hashed or put in a set; equality (used by tests) is fine.
    """

    travel_date: date
    options: dict[str, TrainOption | None]
```

**Why a new type rather than reusing what exists** (the alternatives were
weighed, not skipped):

- **Reuse `AlertMatch`.** Rejected: an `AlertMatch` is *one fare*. The
  entire point of the redesign is that a row is *one date showing both
  departures*, including a departure that is sold out, missing, or above
  threshold. Expressing that as a list of `AlertMatch` would require the
  notifier to re-group by date and to invent placeholder matches for
  non-matching departures — i.e. exactly the awkwardness the brief asks
  to avoid.
- **Pass the raw `dict[date, dict[str, TrainOption | None]]`.** Rejected,
  narrowly: it needs no new type at all, but it makes `send_alert`'s two
  main arguments structurally identical and silently swappable, and it
  gives the notifier's row helpers nothing to be typed on. `DateRow` costs
  six lines and makes "one row = one travel date" explicit in the type
  system, which is the conceptual core of this change.
- **Put `is_booked`/`is_cheap` flags on `DateRow`.** Rejected: which table
  a row is in already carries that, and a flag that can disagree with the
  prices in the same object is a bug waiting to happen. The notifier
  derives "is this row cheap?" from the prices, exactly like the site's
  `rowHasCheapFare` does (§4.5).

Note that `DateRow` carries the whole `TrainOption` per departure, not
just a price — which is what makes §4.6's arrival/changes detail available
to the renderer with no further plumbing.

### 4.2 `src/main.py` — build the two row lists

Add one small private helper next to `evaluate()`:

```python
def _date_rows(travel_dates: Iterable[date], results: dict[date, dict[str, TrainOption | None]]) -> list[DateRow]:
    """DateRows for `travel_dates`, always in ascending date order (the
    order both email tables are rendered in — see src/notifier.py)."""
    return [DateRow(travel_date=d, options=results[d]) for d in sorted(travel_dates)]
```

Then replace the single `notifier.send_alert(matches, secrets, dry_run=False)`
call with:

```python
    # The cheap table is derived from `matches` themselves, not
    # re-computed from a threshold comparison, so the table can never
    # disagree with the gate that decided to send this email at all —
    # including in the TEST_RUN fallback case, where the "matches" are
    # deliberately not below threshold (see _best_effort_matches_for_test).
    # Every match came from `alertable_results`, so its date is unbooked
    # by construction.
    cheap_rows = _date_rows({m.travel_date for m in matches}, results)
    # Not threshold-gated and not derived from `matches`: every booked
    # date that was actually scraped this run, purely for information.
    booked_rows = _date_rows([d for d in results if d in booked], results)

    try:
        notifier.send_alert(
            cheap_rows,
            secrets,
            booked_rows=booked_rows,
            test_summary=is_test_summary,
            dry_run=False,
        )
    except notifier.NotifierError as exc:
        logger.error("failed to send alert email: %s", exc)
        return 1
```

Import `DateRow` from `src.models` alongside `AlertMatch`/`TrainOption`,
and `Iterable` from `collections.abc` (module already uses
`from __future__ import annotations`).

Update the two closing log lines to mention both tables, e.g.:

```python
    if is_test_summary:
        logger.info(
            "sent test summary email (real scraped data, not a genuine alert); "
            "%d booked date(s) also shown", len(booked_rows)
        )
    else:
        logger.info(
            "sent alert for %d fare(s) across %d date(s); %d booked date(s) also shown",
            len(matches), len(cheap_rows), len(booked_rows),
        )
```

**Deriving `cheap_rows` from `matches` is load-bearing**, not a shortcut:

1. It makes the table and the send-gate provably consistent — one
   predicate (`evaluate`), one place, no risk of the email showing a row
   the gate didn't count or vice versa.
2. It handles `TEST_RUN` for free (§4.9).
3. `matches` only ever contains dates from `alertable_results`, so "cheap
   **and unbooked**" is satisfied by construction rather than by a second
   `d not in booked` filter that could drift.

`results` (not `alertable_results`) is indexed for both lists — the same
`TrainOption` objects, so no copying, and booked dates are only present in
`results`.

### 4.3 `src/notifier.py` — new public signature

```python
def send_alert(
    cheap_rows: list[DateRow],
    secrets: config.Secrets,
    *,
    booked_rows: list[DateRow] | None = None,
    test_summary: bool = False,
    dry_run: bool = False,
) -> None:
```

- `cheap_rows` stays first and positional — same call/test shape as today
  ("the thing the alert is about", then secrets).
- `booked_rows` is **keyword-only**, which is what makes the two
  same-typed lists impossible to pass in the wrong order. `None` is
  normalised to `[]` on entry (no mutable default).
- `test_summary` is passed explicitly rather than inferred from "no row is
  actually below threshold" — inference would be an invisible coupling
  between two modules' rules, and `main()` already knows the answer.
- **Empty-input contract is unchanged in spirit:** raise
  `ValueError("send_alert called with no cheap rows — nothing to alert about")`
  when `cheap_rows` is empty. An email containing only a booked-dates
  table is never sent; the booked table is context attached to an alert,
  never a reason for one. (`booked_rows` being empty is perfectly normal
  and simply omits that section.)

Docstring must state all of the above plus: rows are rendered in
ascending date order, and the caller owns the decision of which dates
belong in which list.

### 4.4 `src/notifier.py` — module constants

Replace `MAX_TABLE_ROWS = 20` with:

```python
# Independent caps: a long booked list must never crowd out the cheap
# fares that are the actual point of the email (booked-dates.txt already
# holds 8 dates and grows through the school year). A row is now one
# travel DATE showing both departures, where it used to be one fare, so
# 25 rows is roughly 50 of the old rows.
MAX_CHEAP_ROWS = 25
MAX_BOOKED_ROWS = 25
```

Add the palette, font stack and site link as named constants so the
inline styles below are built from one source of truth (values copied
from `site/style.css`; see §1.3):

```python
SITE_URL = "https://rosjo99.github.io/Train-prices/"

# Copied from site/style.css's :root — the email deliberately mirrors the
# booked-dates website's palette. :hover variants are omitted (hover does
# not exist in email).
C_TEXT = "#1f2328"
C_TEXT_MUTED = "#57606a"
C_BORDER = "#d0d7de"
C_ACCENT = "#1a7f37"
C_BG_MUTED = "#f6f8fa"
C_CHEAP_BG = "#dafbe1"
C_BOOKED_BG = "#ddf4ff"
FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, "
    "Arial, sans-serif"
)
```

### 4.5 `src/notifier.py` — pure row/cell helpers

All of these are pure functions, unit-testable without any HTTP. They are
deliberately split so that the HTML and plain-text renderers compose the
*same facts* into their own presentation, rather than one format
string-munging the other's output:

```python
def _option_price_is_cheap(option: TrainOption | None) -> bool:
    """Mirrors src.main.evaluate()'s per-fare test (priced, GBP, strictly
    below threshold) and site/app.js's rowHasCheapFare — used only to
    decide highlighting/bolding, never to decide whether to send."""

def _row_is_cheap(row: DateRow) -> bool:
    """True if ANY target departure on this row is cheap — the site's own
    row-highlight rule (site/app.js rowHasCheapFare)."""

def _row_min_price(row: DateRow) -> Decimal:
    """Cheapest priced option on the row; Decimal("Infinity") if none —
    used for ordering/selection only."""

def _cell_price_text(option: TrainOption | None) -> str:
    """The primary line of one departure's cell: the price, or why there
    isn't one."""

def _cell_needs_railcard_marker(option: TrainOption | None) -> bool:
    """True when this cell shows a real price that was NOT confirmed as a
    16-25 Railcard fare — rendered as a '*' plus a legend line (§4.6)."""

def _cell_arrival_text(option: TrainOption | None) -> str:
    """"arr 08:26", or "" when there is no option or no arrival time."""

def _cell_is_indirect(option: TrainOption | None) -> bool:
    """True when this journey is known NOT to be direct. False for a
    missing option — absence of data is not a claim about changes."""
```

`_cell_price_text` mirrors `site/app.js`'s `formatLatestCell` on in-memory
data instead of CSV rows:

| in-memory condition | rendered | site equivalent |
| --- | --- | --- |
| `option is None` | `not found` | `!row.actual_departure` |
| `option.sold_out` | `sold out` | `sold_out === "True"` |
| `option.price is None` | `–` (en dash U+2013) | `!row.price_gbp` |
| otherwise | `£X.XX` via `_format_price` | `£X.XX` |

Note the site's first case (no CSV row at all → `–`) has **no in-memory
analogue**: `select_target_trains` always returns a key for every target
departure, so "we have no data for this cell" cannot happen here. The
`option.price is None` branch is defensive (today `parser` sets
`sold_out=True` exactly when `price is None`), and must not be dropped.

`_cell_arrival_text` and `_cell_is_indirect` deliberately still return
useful values for a **sold-out** option: the train exists and is in the
timetable, it just has no fare, so `sold out / arr 08:26` is both correct
and more informative than a bare `sold out`. For `option is None` there is
no timetable entry at all, so both return the empty/False answer.

Selection and ordering, one helper each:

```python
def _select_cheap(rows: list[DateRow]) -> tuple[list[DateRow], int]:
    """Cheapest MAX_CHEAP_ROWS rows (by _row_min_price), returned in
    ascending DATE order, plus the number omitted.

    Selecting by price but displaying by date is deliberate: truncation
    must never drop the cheapest date (the one the subject line names),
    while the table itself stays chronological like the website's, since
    these are travel dates a human is choosing between."""

def _select_booked(rows: list[DateRow]) -> tuple[list[DateRow], int]:
    """First MAX_BOOKED_ROWS rows in ascending date order (nearest travel
    dates first — the ones a human still cares about), plus the number
    omitted."""
```

Money formatting keeps using `_format_price` (`Decimal.__format__`, no
`float` anywhere — `tests/test_notifier.py::test_no_float_anywhere_in_module`
still guards this and must keep passing).

Date formatting: keep the existing `_format_date` (`"%a %d %b"`) **for the
subject line only**, and add for tables:

```python
def _format_table_date(d: date) -> str:
    # e.g. "8 Sep 2026". Not strftime("%-d ...") — "%-d" is a glibc
    # extension, not portable.
    return f"{d.day} {d:%b %Y}"

def _format_weekday(d: date) -> str:
    return d.strftime("%A")  # "Tuesday"
```

The year is included because the candidate range spans Sep 2026 – Jul
2027 and "8 Sep" alone is ambiguous in an archived email.

### 4.6 `src/notifier.py` — columns, and where arrival/direct live

**Both tables use the same four columns:** `Date | Day | 07:25 | 07:30`,
where the two departure headers come from `config.TARGET_DEPARTURES` (not
hardcoded), exactly as `site/app.js` builds them from
`termsData.target_departures`.

The website's fifth column, `Booked?`, is **dropped**: in the email the
two tables *are* the booked/not-booked split, so a per-row checkbox
column would be constant within each table. The site's checkbox is
interactive; an email cannot be.

Today's email also has `Arrives`, `Direct?` and `16-25 Railcard` columns.
Per the user's decision (§9.3), **all three kinds of information are
kept** — but none of them becomes its own column. Here is why, and what
replaces them.

#### 4.6.1 The problem: a row is now a date, not a fare

Today's `Arrives`/`Direct?` columns work because today's row is one fare.
The new row is one **date** carrying **two** `TrainOption`s, each with its
own `arrival_time` and its own `is_direct`. So the naive "keep the
columns" reading doesn't generalise: you would need `07:25 price`,
`07:25 arrives`, `07:25 direct`, `07:30 price`, `07:30 arrives`,
`07:30 direct` — a 8-column table (with `Date`/`Day`), which:

- blows the width budget §4.7 point 5 is built around. The card is capped
  at 640px and must be readable on a ~360px phone with no horizontal
  scroll. Four columns fit that; eight cannot, and email clients give no
  reliable way to reflow columns responsively (media queries live in a
  `<style>` block, which §4.7 point 1 forbids depending on).
- destroys the "looks like the website" property that is the entire
  request — the site's table is four data columns wide.

#### 4.6.2 The design: a two-line cell per departure

Each departure's cell carries **the price on line 1 and its own detail on
line 2**, so per-departure data stays attached to the departure it
describes and the table stays four columns wide:

```
   07:25                 07:30
   £8.70 *               £12.30
   arr 08:26             arr 08:31 · changes
```

- **Line 1** — `_cell_price_text` (`£8.70` / `sold out` / `not found` /
  `–`), plus the railcard `*` marker when applicable.
- **Line 2** — muted, smaller: `arr HH:MM` when an arrival time is known,
  and `· changes` appended when that journey is **not** direct. Omitted
  entirely when there is nothing to say (no option, or no arrival time and
  the journey is direct).

This is the compact-embedding option, and it is also what keeps the design
truthful: a row where 07:25 is direct and 07:30 is not now renders that
difference unambiguously, which a single shared `Direct?` column
physically could not do.

#### 4.6.3 "Direct" is the unmarked default; only "changes" is flagged

Only the **non-direct** case is rendered. Reasoning, in the same spirit as
the railcard marker below:

- These are two specific, fixed, timetabled GWR departures from Oxford to
  London Paddington. `parser` sets `is_direct = len(legs) <= 1`, and for
  these services the expected value is direct essentially always.
- **This is an expectation, not a measurement**, and the plan says so
  rather than pretending otherwise: `is_direct` is not one of
  `price_log.FIELDNAMES`, so there is no logged history to check it
  against (§9.4). The design therefore must render correctly either way —
  it does; it simply optimises the common case by leaving it unmarked.
- Printing `· direct` on every one of up to 100 cells would be pure noise
  that makes the genuinely unusual case *harder* to spot, not easier.
  Marking only the exception is the standard way to surface a rare
  condition, and it is exactly the pattern already used for
  `railcard_applied`.
- A legend line explains the convention explicitly, so "no flag" is never
  ambiguous (below).

#### 4.6.4 `16-25 Railcard` — kept as a marker, unchanged from the original design

CLAUDE.md explicitly requires that whether the discount was confirmed "is
still tracked as `railcard_applied` and shown in both the email and
`price-history.csv`". When a priced cell's `option.railcard_applied` is
`False`, append a muted ` *` to that cell's price.

#### 4.6.5 The legend block

A single muted block sits under the last table, containing only the lines
that are actually needed for what was rendered:

- `* cheapest fare found for that train, but not confirmed as a 16-25
  Railcard price.` — only if at least one **shown** cell is marked.
- `“changes” means that journey is not direct. Everything else shown is
  direct.` (HTML) / `chg = that journey is not direct; everything else
  shown is direct.` (text) — only if at least one **shown** cell is
  indirect.

`arr` needs no legend line: `arr 08:26` under a price in a train email is
self-explanatory, and an unconditional legend line would be visual weight
paid on every email for nothing.

#### 4.6.6 Per-fare deep link (fixes §1.4)

In the **cheap** table, a priced cell's price text is wrapped in an
`<a href>` built with
`config.build_journey_planner_url(row.travel_date, hour, minute)` where
`hour, minute = target.split(":")` — the **target key**, not
`option.departure_time`, so it works even for a cell whose option is
`None` (in which case there is no link anyway, since the cell is text).
Cells reading `sold out` / `not found` / `–` are never linked, and the
line-2 detail is never part of the link. The **booked** table's prices are
plain text: those tickets are already bought, a booking link there is
noise.

### 4.7 `src/notifier.py` — the HTML design (email-client constraints first)

Hard constraints this design is built around, and why:

1. **No `<style>` block may be depended on.** Gmail (notably its mobile
   web view and any forwarded copy) and several Outlook variants strip or
   partially strip `<head><style>`. Therefore **every visual property is
   an inline `style=` attribute on the element it applies to.** A
   `<style>` block may be included *additionally* for progressive
   enhancement, but the email must render identically with it removed —
   the simplest way to guarantee that is not to have one, which is what
   this plan specifies. This is also why §4.6's responsive-column
   alternative is not available: `@media` lives in a `<style>` block.
2. **No modern CSS.** No flexbox, no grid, no CSS custom properties, no
   `:hover`, no `@media` dependence for anything load-bearing. Layout is
   tables; the two-line cell is a plain `<div>` under the price, which is
   universally supported inside `<td>`.
3. **`<tr>` backgrounds are unreliable.** Some clients (Outlook desktop
   in particular) drop `background-color` set on `<tr>`. So the row tint
   is set **on the `<tr>` *and* repeated on every `<td>` in that row.**
   This is the single most important robustness detail in the whole
   design.
4. **Dark-mode inversion** can destroy the green/blue semantics. Add
   `<meta name="color-scheme" content="light">` and
   `<meta name="supported-color-schemes" content="light">`. This is
   best-effort (Gmail Android ignores it), which is acceptable: the tints
   are supporting signal, and the price text itself is unambiguous.
5. **Width budget, recomputed for the two-line cell.** Outer wrapper
   `width="100%"`, inner card `max-width:640px` with `width:100%`. The
   binding constraint is a ~360px phone, giving ~330px of usable table
   width inside the card's 16px+20px padding. Estimated at the specified
   font sizes:

   | column | widest content | approx |
   | --- | --- | --- |
   | Date | `10 Sep 2026` (14px, nowrap) | ~80px |
   | Day | `Thursday` (14px) — candidates are only Tue/Thu/Fri, so `Wednesday` never occurs | ~66px |
   | 07:25 | `£12.30 *` over `arr 08:26` (12px detail) | ~92px |
   | 07:30 | same | ~92px |

   ≈ 330px — it fits, with the rare `arr 08:31 · changes` detail line
   allowed to wrap onto a second line rather than overflow (`white-space`
   is left default on the detail `<div>`; only the `Date` cell and the
   price line are `nowrap`). Eight columns (§4.6.1) would have needed
   ~600px and did not fit — this table is the reason that option was
   rejected.

Exact structure to build (string concatenation/f-strings, matching the
existing style of the module — no templating library):

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>Cheap fares: Oxford → London Paddington</title>
</head>
<body style="margin:0;padding:0;background-color:#f6f8fa;">

  <!-- preheader: the preview line email clients show next to the subject -->
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    Cheapest £8.70 on Tue 08 Sep — 3 cheap date(s).
  </div>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:#f6f8fa;">
    <tr>
      <td align="center" style="padding:16px;">

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="max-width:640px;width:100%;background-color:#ffffff;
                      border:1px solid #d0d7de;border-radius:8px;">
          <tr>
            <td style="padding:20px 20px 8px 20px;font-family:<FONT_STACK>;color:#1f2328;">
              <h1 style="margin:0 0 4px 0;font-size:18px;line-height:1.3;font-weight:600;">
                Cheap fares: Oxford &rarr; London Paddington
              </h1>
              <p style="margin:0;font-size:13px;line-height:1.5;color:#57606a;">
                Alert threshold &pound;10.00 &middot; 16-25 Railcard &middot; one-way
              </p>
            </td>
          </tr>

          <!-- OPTIONAL test-run banner, only when test_summary=True -->
          <tr>
            <td style="padding:8px 20px 0 20px;font-family:<FONT_STACK>;">
              <p style="margin:0;padding:10px 12px;background-color:#f6f8fa;
                        border:1px solid #d0d7de;border-radius:6px;
                        font-size:13px;line-height:1.5;color:#57606a;">
                Manual test run: nothing is currently below &pound;10.00, so the
                cheapest fare found is shown instead.
              </p>
            </td>
          </tr>

          <!-- SECTION: cheap dates -->
          <tr>
            <td style="padding:16px 20px 0 20px;font-family:<FONT_STACK>;color:#1f2328;">
              <h2 style="margin:0 0 8px 0;font-size:15px;font-weight:600;
                         border-bottom:1px solid #d0d7de;padding-bottom:6px;">
                Under &pound;10 &mdash; not booked yet
              </h2>
              <!-- data table, see below -->
            </td>
          </tr>

          <!-- SECTION: booked dates (omitted entirely when booked_rows is empty) -->
          <tr>
            <td style="padding:20px 20px 0 20px;font-family:<FONT_STACK>;color:#1f2328;">
              <h2 style="…same as above…">Already booked &mdash; current prices</h2>
              <p style="margin:0 0 8px 0;font-size:13px;color:#57606a;">
                For information only. These dates are still checked every run, but
                never trigger an alert.
              </p>
              <!-- data table, see below -->
            </td>
          </tr>

          <!-- legend: 0, 1 or 2 lines, whichever are needed (§4.6.5) -->
          <tr>
            <td style="padding:12px 20px 0 20px;font-family:<FONT_STACK>;">
              <p style="margin:0;font-size:12px;line-height:1.5;color:#57606a;">
                * cheapest fare found for that train, but not confirmed as a
                16-25 Railcard price.<br>
                &ldquo;changes&rdquo; means that journey is not direct. Everything
                else shown is direct.
              </p>
            </td>
          </tr>

          <!-- footer / call to action -->
          <tr>
            <td style="padding:20px;font-family:<FONT_STACK>;">
              <a href="https://rosjo99.github.io/Train-prices/"
                 style="display:inline-block;padding:9px 14px;background-color:#1a7f37;
                        color:#ffffff;font-size:14px;font-weight:600;
                        text-decoration:none;border-radius:6px;">
                Open the booked-dates site
              </a>
              <p style="margin:10px 0 0 0;font-size:12px;line-height:1.5;color:#57606a;">
                Tick a date there once you've booked it and it will stop triggering
                alerts (its price keeps being checked and shown).
              </p>
            </td>
          </tr>
        </table>

      </td>
    </tr>
  </table>
</body>
</html>
```

**The data table** (used for both sections, one shared builder). Note
`role="presentation"` is used **only** on the layout wrappers above — the
data tables are genuine tables and must not carry it, and must use
`<th scope="col">`:

```html
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="width:100%;border-collapse:collapse;font-family:<FONT_STACK>;font-size:14px;">
  <thead>
    <tr>
      <th scope="col" style="text-align:left;padding:6px 8px;border-bottom:1px solid #d0d7de;
                             color:#57606a;font-size:12px;font-weight:600;
                             text-transform:uppercase;letter-spacing:0.03em;">Date</th>
      <th … >Day</th>
      <th … >07:25</th>   <!-- from config.TARGET_DEPARTURES -->
      <th … >07:30</th>
    </tr>
  </thead>
  <tbody>
    <tr style="background-color:#dafbe1;">
      <td style="padding:7px 8px;border-bottom:1px solid #d0d7de;background-color:#dafbe1;
                 color:#1f2328;white-space:nowrap;vertical-align:top;">8 Sep 2026</td>
      <td style="…same…;color:#57606a;">Tuesday</td>

      <!-- priced, cheap, railcard unconfirmed, direct -->
      <td style="…same, but white-space:normal…">
        <span style="white-space:nowrap;">
          <a href="<deep link>" style="color:#1a7f37;font-weight:700;text-decoration:none;">£8.70</a><span style="color:#57606a;"> *</span>
        </span>
        <div style="margin-top:2px;font-size:12px;line-height:1.3;color:#57606a;">arr 08:26</div>
      </td>

      <!-- priced, not cheap, not direct -->
      <td style="…same…">
        <span style="white-space:nowrap;">
          <a href="<deep link>" style="color:#1f2328;text-decoration:underline;">£12.30</a>
        </span>
        <div style="margin-top:2px;font-size:12px;line-height:1.3;color:#57606a;">arr 08:31 &middot; <span style="font-weight:600;">changes</span></div>
      </td>
    </tr>
    …
  </tbody>
</table>
```

Cell rendering rules — **line 1** (the price line):

| cell state | HTML |
| --- | --- |
| priced **and** below threshold | link, `color:#1a7f37;font-weight:700;text-decoration:none;` |
| priced, not below threshold | link, `color:#1f2328;text-decoration:underline;` (cheap table) / plain `<span style="color:#1f2328;">` (booked table) |
| `sold out` / `not found` / `–` | `<span style="color:#57606a;font-style:italic;">`, never linked |
| priced and `railcard_applied` is False | append `<span style="color:#57606a;"> *</span>` after the price, inside the same nowrap span, outside the link |

Cell rendering rules — **line 2** (the detail line, `<div>` below):

| `_cell_arrival_text` | `_cell_is_indirect` | rendered |
| --- | --- | --- |
| `"arr 08:26"` | False | `arr 08:26` |
| `"arr 08:26"` | True | `arr 08:26 &middot; <span style="font-weight:600;">changes</span>` |
| `""` (no option, or no arrival time) | False | line 2 omitted entirely |
| `""` | True | `<span style="font-weight:600;">changes</span>` (arrival unknown but we do know it isn't direct — say the part we know) |

The detail line is always `font-size:12px;color:#57606a;` — visually
subordinate to the price, which is the number the reader is scanning for.
It is never a link.

Row tint (`<tr>` **and** every `<td>` in it):

| table | condition | background |
| --- | --- | --- |
| booked | always | `#ddf4ff` |
| cheap | `_row_is_cheap(row)` | `#dafbe1` |
| cheap | otherwise (only reachable in the `test_summary` fallback) | `#ffffff` |

Deriving the cheap table's tint per row from `_row_is_cheap` rather than
"every row in this table is green by definition" is deliberate: it is
literally the website's own rule, and it self-corrects the one case where
a row in that table is *not* actually below threshold (the `TEST_RUN`
fallback, §4.9) — no extra flag plumbed into the row renderer, and no
misleading green block on a £45 fare. Booked rows are always blue,
matching the site's "booked wins over cheap" precedence.

"+N more" lines, per table, styled muted (`font-size:12px;color:#57606a;`)
directly under the table they belong to:

- cheap: `+N more cheap date(s) not shown`
- booked: `+N more booked date(s) not shown`

**Escaping.** Every value interpolated into the HTML is produced by this
module's own formatters (`£X.XX`, `8 Sep 2026`, `Tuesday`, `arr 08:26`,
`sold out`, `not found`, `–`) or is a repo constant
(`config.TARGET_DEPARTURES`, `config.ORIGIN_NAME`,
`config.DESTINATION_NAME`, `SITE_URL`, the generated deep-link URL). None
of it is scraped free text — note in particular that `arrival_time` is
produced by `parser._to_london_hhmm`'s `strftime("%H:%M")`, so it is
digits and a colon, never raw response text. **Do not interpolate
`option.fare_name` (or any other scraped string) into the HTML**; if a
future change needs to, it must go through `html.escape` first. State this
as a comment in the module.

### 4.8 `src/notifier.py` — plain-text body

Resend sends both parts; `text` is the accessible/fallback version and is
also what `dry_run` prints to stdout. It must now represent both tables,
plainly, including the same per-departure detail as the HTML — but with
the two HTML lines folded onto **one** line, since a fixed-width text
table cannot carry a sub-line without becoming unreadable:

```
Cheap fares: Oxford -> London Paddington
Alert threshold £10.00 · 16-25 Railcard · one-way

[only when test_summary]
Manual test run: nothing is currently below £10.00, so the cheapest fare
found is shown instead.

UNDER £10 — NOT BOOKED YET
Date          Day        07:25                   07:30
8 Sep 2026    Tuesday    £8.70 * arr 08:26       £12.30 arr 08:31 chg
10 Sep 2026   Thursday   sold out arr 08:26      £9.40 arr 08:31
(+3 more cheap date(s) not shown)

ALREADY BOOKED — CURRENT PRICES (information only, never alerted on)
Date          Day        07:25                   07:30
11 Sep 2026   Friday     £22.50 arr 08:26        not found
(+2 more booked date(s) not shown)

* cheapest fare found for that train, but not confirmed as a 16-25
  Railcard price.
chg = that journey is not direct; everything else shown is direct.

All dates and booking status: https://rosjo99.github.io/Train-prices/
```

Implementation notes:

- Cell text is composed as
  `" ".join(part for part in (price_text + marker, arrival_text, "chg" if indirect else "") if part)`
  — i.e. the same four facts as the HTML cell, space-separated, with
  `chg` as the compact spelling of the HTML's `changes` (the legend line
  defines it). This keeps both renderers reading from the §4.5 helpers
  rather than one reformatting the other's output.
- Fixed-width columns via `str.ljust` with module constants
  `TEXT_COL_DATE = 14`, `TEXT_COL_DAY = 11`, `TEXT_COL_CELL = 24`. Total
  line width 14 + 11 + 24 + 24 = **73 characters**, comfortably inside a
  78-column terminal or a plain-text mail client. The widest realistic
  cell is `sold out arr 08:26` (18) or `£12.30 * arr 08:31 chg` (22),
  both inside 24; a cell that somehow exceeds the width simply pushes the
  following column right rather than truncating (never truncate a price).
- `->` not `→` in the text part (kept from today's body), `£` is fine
  (already sent as UTF-8 by `requests`, per the existing comment).
- No links inside the table cells (plain text); the site URL goes in the
  footer line only.
- Omit the booked section entirely when `booked_rows` is empty; include
  each legend line only when the corresponding marker actually appears in
  a shown cell (same rule as the HTML, §4.6.5).

### 4.9 `TEST_RUN` / `_best_effort_matches_for_test` behaviour

`main()`'s fallback is unchanged: when `evaluate()` returns nothing and
`config.TEST_RUN` is set, `matches` becomes the single cheapest real fare
found across the run (or stays empty if nothing was priced at all, in
which case no email is sent — unchanged).

Under the new design, because `cheap_rows` is derived from `matches`
(§4.2), that produces:

- **Cheap table:** exactly **one row** — the travel date of that cheapest
  fare — showing **both** target departures for that date, with real
  scraped prices, arrival times, direct/changes status and sold-out /
  not-found values. This is strictly more informative than today's
  single-fare row, at zero extra cost, and it is the "padded out with real
  context" option.
- **Row tint:** white, not green, because `_row_is_cheap` is false for it
  (§4.7) — the email never colours a £45 fare as a bargain.
- **Banner:** the muted "Manual test run…" block (§4.7/§4.8) explains why
  a row under a "Under £10" heading isn't under £10.
- **Booked table:** entirely unaffected — it is not threshold-gated and
  not derived from `matches`, so a `TEST_RUN` email still shows every
  booked date scraped this run with its real current prices. This path
  needs no special-casing at all.
- **Subject:** unchanged logic (§4.10), i.e. it names that fare's price
  and date. No `[TEST]` prefix — confirmed by the user, §9.2.

### 4.10 Subject line

Keep today's shape and derive it from `cheap_rows`:

```
Cheap train: Oxford → London Paddington £8.70 on Tue 08 Sep
Cheap train: Oxford → London Paddington £8.70 on Tue 08 Sep (+2 more dates)
```

- The headline price is `min(_row_min_price(r) for r in cheap_rows)` and
  the date is that row's date (ties → earliest date). This equals today's
  "cheapest match first" behaviour.
- The suffix counts **dates**, not fares: `len(cheap_rows) - 1`, worded
  explicitly as `(+N more dates)` rather than today's bare `(+N more)`.
  Reason: the body is now one row per date, so a bare count would silently
  change meaning (today a single date with both departures cheap yields
  "+1 more"; under the new body that is one row). Spelling out "dates"
  makes the count unambiguous against the table the reader is about to
  see. This is the only subject change.
- The booked table is deliberately **not** reflected in the subject: it is
  context, not alert-worthy news, and putting it there would make the
  subject noisier for the thing that actually matters.
- Preheader text (the client's preview line) carries the count too, e.g.
  `Cheapest £8.70 on Tue 08 Sep — 3 cheap date(s).`

---

## 5. Correctness argument

**P1 — the send trigger is bit-for-bit unchanged.** `main()` still calls
`evaluate(alertable_results)`; still `return 0` without emailing when
`matches` is empty (and `TEST_RUN` is off); still applies the same
`TEST_RUN` fallback. Nothing in this plan touches `evaluate()`,
`config.PRICE_THRESHOLD`, the strict `<` comparison, or the
booked-date exclusion. The only new failure mode would be an exception
inside the new rendering code, which is covered by tests and cannot
change *whether* the send is attempted.

**P2 — the cheap table can never contain a booked date.** Its dates come
from `matches`, which come from `evaluate(alertable_results)`, and
`alertable_results` is `{d: … for d in results if d not in booked}`. A
booked date is therefore unreachable in `cheap_rows` without changing
`main()`'s existing filter.

**P3 — the two tables can never disagree with the alert decision.** The
cheap table is not recomputed from a threshold comparison; it is the alert
decision, regrouped by date. The booked table is not threshold-gated at
all, so no comparison exists there to disagree.

**P4 — no new data source, so no new staleness.** Both tables read
`results`, i.e. what this very run scraped. `price-history.csv` is not
read by the notifier (the website's job), so the email cannot show a
stale price the run didn't observe. The arrival/direct detail comes from
the same `TrainOption` as the price in the same cell, so a cell can never
mix a price from one observation with an arrival from another.

**P5 — booked dates are still scraped and logged.** Untouched: the change
is downstream of `price_log.append_price_log`, which still runs for every
successful date including booked ones.

**P6 — secrets remain unexposed.** All new rendering happens before the
`requests.post`; `_redact`, the retry loop, the error paths and
`Secrets.__repr__` are untouched. No new value interpolated into the email
comes from the environment.

**P7 — no date in the past can ever appear in either table.** Every date in
`cheap_rows`/`booked_rows` comes from `results`, and `main()` only ever
populates `results` for
`candidates = term_dates.checkable_dates(today + timedelta(days=1), term_dates.LAST_KNOWN_DATE)`
— i.e. strictly tomorrow onward. A date that has already passed was never a
candidate this run, so it cannot reach `results`, `matches`, `cheap_rows` or
`booked_rows`, regardless of what is still sitting in `booked-dates.txt` or in
old `price-history.csv` rows for it. In particular, the booked table lists
*booked dates scraped this run*, not *the contents of booked-dates.txt*, so
past bookings left in that file are invisible to the email and need no manual
tidying. Nothing in this plan changes that — it is recorded here because the
property is worth naming rather than leaving implicit. (For completeness, and
not a change: `site/` independently guarantees the same thing client-side via
`site/app.js`'s `const start = addDaysISO(todayLocalISO(), 1);` before
`checkableDates(start, …)`. This plan does not touch the site — see §7.5 — but
the parallel is worth stating, since the site and the email reach the same
invariant by two separate implementations of the same rule.)

---

## 6. Considered and rejected

- **`Arrives` and `Direct?` as their own columns** (the literal reading of
  "keep them"). Rejected — §4.6.1: with two departures per row that means
  eight columns, ~600px minimum, which breaks both the phone-width budget
  (§4.7 point 5) and the "looks like the website" goal, and email clients
  offer no reliable responsive escape hatch.
- **One shared `Arrives` / `Direct?` column per row** (e.g. showing the
  07:25 train's values). Rejected: silently wrong whenever the two
  departures differ, which is exactly when the information matters.
- **Printing `· direct` on every direct cell.** Rejected — §4.6.3: noise
  on ~100 cells that makes the rare non-direct case harder to spot, and
  inconsistent with how `railcard_applied` is already surfaced.
- **A `<style>` block with CSS classes** (much less repetitive HTML).
  Rejected: Gmail's mobile web client and forwarded messages strip
  `<head>` styles, which would degrade the email to unstyled tables in
  exactly the situations a phone-reading user is most likely to hit.
  Inline-everything is verbose but is the only reliable option.
- **CSS custom properties / flex / grid / `:hover` / `@media`** —
  unsupported, meaningless in email, or (for `@media`) only available via
  the `<style>` block just rejected. The palette is expressed as Python
  constants instead (§4.4), which serves the same "one place to change"
  purpose.
- **Reading `price-history.csv` in the notifier** to show history or
  price deltas. Rejected: out of scope, and it would make the email
  disagree with the run that triggered it. CSV reading is the website's
  job.
- **An HTML templating dependency (Jinja2 etc.).** Rejected: the project
  has no such dependency, `requirements.txt` is deliberately three
  packages, and the existing module style is f-strings.
- **Grouping the tables by term name like the site does.** Rejected:
  requires a `term_dates` import in the renderer, and an alert almost
  always spans a single term. The site's term sections exist because it
  lists the *whole* year; the email lists what this run found.
- **Keeping `AlertMatch` as the table row unit** — see §4.1.
- **Embedded images/logos/web fonts.** Rejected: images are blocked by
  default in most clients and web fonts are widely unsupported; the design
  is text, colour and rule lines only, which renders everywhere.
- **A "price changed since last email" column, or suppressing dates
  already alerted on.** Rejected here — see §9.1; it needs persisted
  state that does not exist anywhere in this codebase.

---

## 7. What must not change

1. **When an email is sent.** `evaluate()`, `config.PRICE_THRESHOLD`,
   strict `<`, the empty-`matches` early return, and the `TEST_RUN`
   fallback rule all keep their current behaviour exactly.
2. **Booked dates are still scraped, logged to `price-history.csv`, and
   excluded from alerting** — only their *presentation* is new.
3. **`price-history.csv`'s columns and semantics** (`src/price_log.py`).
4. **`src/booked_dates.py`, `src/term_dates.py`, `src/scraper.py`,
   `src/parser.py`** — untouched. In particular `TrainOption` gains no
   new fields: `arrival_time` and `is_direct` already exist and are
   already parsed.
5. **`site/`** — untouched. The email mirrors the site, never the reverse.
6. **Secret handling in `src/notifier.py`:** `_redact`, the
   `MAX_ATTEMPTS`/`RETRY_BACKOFF_SECONDS` retry loop, the
   429/5xx-retryable vs 4xx-fatal split, `_parse_recipients`, and the
   `to`-always-a-list payload shape.
7. **`dry_run`** still prints the text body and never calls
   `requests.post`.
8. **Money stays `Decimal`.** No `float(` anywhere in `src/notifier.py`
   (guarded by an existing test).
9. **No new third-party dependency.**
10. **`PARALLEL_DATES`, `FULL_RETRY_HORIZON_DAYS`,
    `MAX_CONSECUTIVE_FAILURES`, `_dispatch_order`** and everything else
    from plans 002/003.

---

## 8. Test impact

Run `python -m pytest` before and after; the suite must be green.

### 8.1 Root cause of the breakage

Two signature changes: `send_alert`'s first parameter is now
`list[DateRow]` rather than `list[AlertMatch]`, and `main()` passes
keyword arguments the fake notifier doesn't accept. Everything below
follows from those two.

### 8.2 `tests/test_notifier.py` — must update

Add a row helper next to the existing `_option`:

```python
def _row(travel_date=date(2026, 9, 11), prices=None, **option_kwargs) -> DateRow:
    """prices maps a target departure ("07:25"/"07:30") to a Decimal, to
    None for 'sold out', or omits it entirely for 'not found'."""
```

`_option` stays (still needed to build individual cells) and must be able
to vary `arrival_time`, `is_direct` and `railcard_applied` per departure —
it already takes `arrival_time`/`is_direct` kwargs today; add
`railcard_applied`. `_match` can be deleted once nothing uses it.

| Test | Change |
| --- | --- |
| `test_empty_matches_raises_value_error` | Rename to `test_empty_cheap_rows_raises_value_error`; call `send_alert([], SECRETS)` — still `ValueError`. |
| `test_dry_run_prints_and_never_calls_requests_post` | `send_alert([_row()], SECRETS, dry_run=True)`; `"£8.70" in out` still holds. |
| `test_successful_send_returns_none_with_correct_request` | `[_match()]` → `[_row()]`; all other assertions unchanged. |
| `test_comma_separated_email_to_sends_to_every_address` | Argument swap only. |
| `test_500_retries_then_raises_notifier_error`, `test_success_after_retryable_failures`, `test_network_error_is_retried`, `test_429_is_treated_as_retryable`, `test_401_raises_immediately_without_retry`, `test_key_never_appears_in_raised_message` | Argument swap only — retry/redaction behaviour is unchanged. |
| `test_decimal_8_7_renders_as_two_decimal_places` | Unchanged. |
| `test_subject_for_single_match_has_no_more_suffix` | Rename to `…_single_row…`; still asserts `"£8.70" in subject` and `"more" not in subject`. |
| `test_subject_for_three_matches_shows_cheapest_and_count` | Rewrite with **three rows on three different dates** (prices 9.50 / 6.20 / 9.99); assert `"£6.20" in subject` and `"(+2 more dates)" in subject`. |
| `test_long_match_list_capped_at_20_rows_plus_more_line` | Replace with two tests: `test_cheap_table_capped_at_max_cheap_rows` (30 cheap rows → `MAX_CHEAP_ROWS` body rows and `"+5 more cheap"` in both bodies) and `test_booked_table_capped_at_max_booked_rows`. Count body rows by counting a `<td` marker unique to data rows, or by counting occurrences of the row background colour — **not** by `html.count("<tr>")`, since the new layout has many structural `<tr>`s. Also note the old assertion `text.count("07:25 -> 08:26") == …` no longer matches any rendered string: the text body's departure/arrival format is now `arr 08:26` inside a cell. |
| `test_no_float_anywhere_in_module` | Unchanged, must still pass. |

### 8.3 `tests/test_notifier.py` — must add

1. `test_row_shows_both_departures_in_one_row` — a row with 07:25 cheap
   and 07:30 expensive renders both prices in a single data row.
2. `test_cell_price_text_matches_the_website_formats` — parametrised over
   `_cell_price_text`: `None → "not found"`, `sold_out=True → "sold out"`,
   priced → `"£8.70"`.
3. `test_cell_shows_arrival_time` — a priced option with
   `arrival_time="08:26"` renders `arr 08:26` in **both** the HTML and the
   text body.
4. `test_cell_omits_detail_line_when_no_arrival_and_direct` — an option
   with `arrival_time=None` and `is_direct=True` renders no detail
   `<div>` for that cell and no stray `arr` in the text body.
5. `test_indirect_journey_is_flagged_and_direct_is_not` — one row with
   07:25 direct and 07:30 `is_direct=False`: HTML contains `changes`
   exactly once, text contains `chg` exactly once, and neither body
   contains the word `direct` attached to the 07:25 cell. This is the
   test that pins §4.6.3's "unmarked default" decision.
6. `test_changes_legend_only_when_an_indirect_cell_is_shown` — legend line
   present in the indirect case, absent when every shown cell is direct.
7. `test_sold_out_cell_still_shows_its_arrival_time` — pins §4.5's
   deliberate choice.
8. `test_booked_table_rendered_with_booked_background` — `booked_rows`
   present ⇒ `#ddf4ff` appears in the HTML and the booked date appears in
   both `text` and `html`, including its arrival detail (the detail must
   render in the booked table too, not just the cheap one).
9. `test_booked_section_omitted_when_no_booked_rows` — neither the booked
   heading nor `#ddf4ff` appears.
10. `test_booked_date_never_appears_in_cheap_section` — sanity check that
    the renderer keeps the two lists separate (build both, assert ordering
    of the two headings and that the booked date is not in the cheap
    table's slice of the HTML).
11. `test_cheap_row_is_green_and_cheap_price_is_bold_green` — `#dafbe1`
    present; the sub-threshold price carries `#1a7f37`.
12. `test_test_summary_row_is_not_tinted_green_and_banner_present` —
    `test_summary=True` with a £45 row: banner text present, `#dafbe1`
    absent.
13. `test_prices_link_to_the_journey_planner` — a cheap cell's `href`
    equals `config.build_journey_planner_url(travel_date, "07", "25")`;
    also assert the detail line is **not** inside the anchor.
14. `test_railcard_unconfirmed_gets_marker_and_legend` — and its
    negative: no railcard legend line when every shown cell is confirmed.
15. `test_html_has_no_style_block` — assert `"<style"` not in the HTML
    (the inline-only guarantee from §4.7) and that a row background
    colour appears on a `<td`, not just a `<tr`.
16. `test_text_body_contains_both_sections` — both headings present, in
    order, with the right dates under each.
17. `test_text_table_line_width_stays_within_78_columns` — cheap guard on
    §4.8's column arithmetic, using the widest realistic cell
    (`£12.30 * arr 08:31 chg`).
18. `test_site_url_present_in_both_bodies`.

### 8.4 `tests/test_main.py` — must update

`_install_fake_notifier`'s inner function must become:

```python
def _fake_send_alert(cheap_rows, secrets, *, booked_rows=None, test_summary=False, dry_run=False):
    calls.append({
        "cheap_rows": cheap_rows, "booked_rows": booked_rows or [],
        "secrets": secrets, "test_summary": test_summary, "dry_run": dry_run,
    })
```

Then update every assertion that reaches into `["matches"]` (verified
line numbers in the current file):

| Line(s) | Test | New assertion |
| --- | --- | --- |
| 181-182 | `test_one_date_cheap_railcard_fare_sends_one_match` | `rows = send_calls[0]["cheap_rows"]`; `len(rows) == 1`; `rows[0].options["07:25"].price == Decimal("8.70")`. |
| 520 | `…failure/success interleaving…` (asserts the match's date) | `send_calls[0]["cheap_rows"][0].travel_date == c5`. |
| 671-673 | `test_sub_threshold_price_without_railcard_confirmation_still_sends_alert` | `rows[0].options["07:25"].price == Decimal("5.00")` and `.railcard_applied is False`. |
| 906-907 | `test_speculative_zone_dates_are_still_checked_and_logged` | `rows[0].travel_date == speculative_date`. |
| 981 | `test_test_run_with_genuine_match_behaves_normally` | `rows[0].options["07:25"].price == Decimal("8.70")`; also assert `send_calls[0]["test_summary"] is False`. |
| 999 | `test_test_run_with_no_match_sends_cheapest_real_fare_found` | `rows[0].options["07:25"].price == Decimal("45.00")`; assert `test_summary is True`. |
| 1018-1020 | `test_test_run_picks_the_single_cheapest_across_dates` | `len(rows) == 1`; `rows[0].travel_date == d2`; `rows[0].options["07:25"].price == Decimal("32.00")`. |
| 1068-1070 | `test_test_run_best_effort_includes_railcard_unconfirmed_fares` | `rows[0].options["07:25"].price == Decimal("45.00")` and `.railcard_applied is False`. |

Tests asserting only `send_calls == []` or `len(send_calls) == 1`
(including `test_booked_date_is_scraped_and_logged_but_never_alerted`,
`test_all_candidates_booked_still_scraped_but_no_alert`,
`test_price_exactly_threshold_does_not_alert`,
`test_notifier_raises_returns_1`,
`test_test_run_with_nothing_priced_at_all_sends_no_email`,
`test_test_run_notifier_failure_returns_1`) need **no change beyond the
fake's signature** — and that is the point: the send trigger is unchanged.

`evaluate()`'s own unit tests (lines ~929-957) and every
`_dispatch_order`/scheduler test are untouched.

### 8.5 `tests/test_main.py` — must add

1. `test_booked_and_cheap_dates_are_split_between_the_two_tables` — the
   key integration test: one booked date priced £5 and one unbooked date
   priced £8.70. Expect exactly one email; `cheap_rows` is only the
   unbooked date; `booked_rows` contains the booked date with its real
   `TrainOption`s; the booked date is absent from `cheap_rows`.
2. `test_booked_rows_include_dates_that_were_scraped_only` — a booked
   date whose scrape failed must not appear in `booked_rows` (it is not in
   `results`).
3. `test_both_departures_cheap_on_one_date_produces_one_row` — two
   matches, one date ⇒ `len(cheap_rows) == 1` with both options
   populated (the concrete behaviour change from the old flat list).
4. `test_rows_are_in_ascending_date_order` — for both lists.
5. `test_rows_carry_arrival_and_direct_metadata` — a cheap row's
   `options["07:25"]` still has its `arrival_time` and `is_direct`
   populated when it reaches `send_alert` (i.e. `main()` passes whole
   `TrainOption`s, not a stripped-down price), which is what §4.6's cell
   design depends on.

---

## 9. Open decisions for a human

### 9.1 "only trigger when there are new cheap trains" — the interpretation being implemented

**RESOLVED (user, 2026-09-01): "same trigger as today", confirmed
explicitly.**

**This plan reads that phrase as "same trigger as today"**: an email is
sent when this run finds at least one fare currently below £10 on an
unbooked, in-term date (plus the existing `TEST_RUN` fallback). "New" is
read as "newly *found* this run", not "different from what was in the last
email".

That is a judgement call on genuinely ambiguous wording, and it is called
out here so it is visible rather than silently baked in. The other
reading — **suppress a repeat alert for a date that was already alerted
on** — would require something this codebase does not have anywhere: a
persisted record of what has already been emailed, surviving between
GitHub Actions runs (a committed state file, or deriving "was it already
under £10 at the previous `checked_at`?" from `price-history.csv`). That
brings its own design questions (does a price *drop* on an
already-alerted date re-alert? does a fare going back above £10 and down
again re-alert? what happens on the first run after the file is lost or
the CSV is truncated?). **No such mechanism is being invented here.**

If the user did mean repeat-suppression, that is a separate plan (005),
and it is a *gating* change rather than a presentation change — the two
are cleanly separable, and this plan does not make that follow-up harder:
it would slot in exactly where `matches` is computed in `main()`, leaving
everything in §4 untouched.

### 9.2 `[TEST]` subject prefix — RESOLVED (user, 2026-09-01): no prefix

A `TEST_RUN` email carries the "manual test run" banner in the **body**
only; the subject line is built by exactly the same logic as a genuine
alert. This keeps subject lines stable for any existing inbox filter or
notification rule. `test_summary` is already plumbed into the renderer, so
reversing this later would be a two-line change.

### 9.3 `Arrives` / `Direct?` — RESOLVED (user, 2026-09-01): keep both

Both are kept, but **not as table columns** — see §4.6 for the full design
and §6 for the rejected alternatives. In summary:

- Each departure's own arrival time and direct/changes status live in that
  departure's own cell, on a second muted line (`arr 08:26`,
  `arr 08:31 · changes`), so the table stays four columns wide and the
  per-date row can carry two genuinely different values.
- Only the **non-direct** case is written out; "direct" is the unmarked
  default, explained by a legend line, matching how `railcard_applied` is
  already surfaced.
- This applies identically to both tables and both body formats.

Nothing further is open here; it is recorded in this section only so the
resolution and its reasoning are findable next to the original question.

### 9.4 A pre-existing inconsistency, noticed while reading — not fixed here

`src/models.TrainOption.is_direct` is parsed and (still, after §4.6) shown
in the email, but is **not** one of `price_log.FIELDNAMES`, so it is never
written to `price-history.csv` and the website therefore cannot show it —
and, more practically, there is no logged history to check the §4.6.3
assumption ("these two departures are essentially always direct") against.
Adding it as a CSV column would fix both, but **changing
`price-history.csv`'s format is explicitly out of scope** (§7.3). Raised
only so the gap is a deliberate choice rather than an oversight.

---

## 10. Docs to update as part of this change

- **`CLAUDE.md`**, "Email service" bullet: add a sentence that the alert
  email renders two tables (cheap-and-unbooked, and already-booked with
  current prices) styled to match `site/`, with each departure's price,
  arrival time and direct/changes status in its own cell, that the send
  trigger is unchanged, and that `src/notifier.py` is inline-styled HTML
  with no `<style>` block by design.
- **`CLAUDE.md`**, "Route details" bullet on `railcard_applied`: it says
  the flag is "shown in both the email and `price-history.csv`" — still
  true, now via the `*` marker and legend line (§4.6.4/§4.6.5). Adjust the
  wording so it matches what the email actually renders.
- **`README.md`**: one short paragraph under the alerting description
  describing what the email now contains and that it links back to the
  booked-dates site.
- **`src/config.build_journey_planner_url`'s docstring**: its claim about
  `src.notifier` using it becomes true with §4.6.6 — no edit needed if the
  deep link is implemented, but the coder must confirm it rather than
  leaving the docstring aspirational.

## 11. Verification after implementation

1. `python -m pytest` — green.
2. `MAX_DATES=3 TEST_RUN=1 …` manual `workflow_dispatch` run (the
   existing test path in `.github/workflows/price-check.yml`) — confirms a
   real Resend delivery of the new HTML, with the test banner, at least
   one booked date in the booked table if one is in range, working deep
   links, and a real `arr HH:MM` line under each real price.
3. Eyeball the delivered email in Gmail web **and** Gmail mobile (the
   strictest `<style>`-stripping client of the ones in use): row tints
   must survive (the §4.7 point 3 `<td>`-background detail), and the
   four-column table with its two-line cells must not scroll horizontally
   on a phone (the §4.7 point 5 width budget).
4. Confirm the "Open the booked-dates site" button reaches
   `https://rosjo99.github.io/Train-prices/`.
