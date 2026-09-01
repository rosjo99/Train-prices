# Train Price Alert Tool

## What this project does
Checks TransPennine Express's booking engine every 6 hours for the price of two
specific Oxford → London Paddington trains (searched with a 16-25
railcard applied) and emails an alert when either fare's cheapest price
falls below GBP 10 — whether or not that price is confirmed as the
railcard-discounted one (see Route details). Only travel dates that are
a Tuesday, Thursday or Friday inside school term time are checked. A
date already marked as booked (see "Marking a date as already booked")
is still checked and logged, just never alerted on.

## Constraints
- Must handle TPE's dynamic (client-rendered) journeys-grid — a
  headless browser drives it, but via a deep-link URL, not by filling
  in the form (see Tech decisions)
- Runs every 6 hours via GitHub Actions cron (no time-of-day gate in
  Python — see Hosting/scheduling)
- Email via a free-tier service
- Searches are made with a 16-25 railcard applied; the resulting
  discount is tracked but does not gate alerting (see Route details)
- Target days: Tuesday, Thursday, Friday only
- Only check dates that fall within school term time (see Term dates
  below) — including staff INSET days, but excluding half terms,
  occasional days, and bank holidays

## Tech decisions
Full rationale and measured evidence for every item below lives in
`docs/plans/` (referenced inline) — this section is the current-state
summary an agent needs to write correct code, not the history of how we
got here.

- **Language:** Python 3.12. Dependencies pinned in `requirements.txt`
  (`camoufox[geoip]` — which brings Playwright with it, since Camoufox
  drives it under the hood — `requests`, `pytest`). Money uses
  `decimal.Decimal`, never `float`; all dates/times go through
  `zoneinfo.ZoneInfo("Europe/London")`, never a naive clock read.
- **Retailer: TransPennine Express (`ticket.tpexpress.co.uk`).** No bot
  protection observed across the Camoufox diagnostic probe or the
  fixture-capture run (GitHub Actions runs 33525860120 / 33527007099,
  2026-09-01). See `docs/plans/005-migrate-to-tpe.md` §1.5. Historical
  note: National Rail Enquiries was the previous retailer this repo
  scraped, and Trainline was rejected before that for DataDome bot
  protection — see `docs/plans/001-train-price-alert.md` for that
  history, kept as a record, not as the current state.
- **Scraping approach:** Camoufox (headless, `humanize=True`, Firefox-
  based) rather than plain Playwright/Chromium — mandated by this file
  for any booking platform other than NRE, and validated live against the
  real TPE site (see `scripts/capture_fixture_tpe.py`, the reference
  implementation). Launch shape: `with Camoufox(headless=True,
  humanize=True, locale="en-GB") as browser:` then `NewContext(browser,
  locale="en-GB", timezone_id="Europe/London", viewport=...)`. Navigates
  straight to a fully-parameterised deep-link URL —
  `https://ticket.tpexpress.co.uk/journeys-grid/{origin}/{destination}/{date}T{hour}:{minute}//1//{railcard}x1?...`
  (ISO date, and the railcard as a `YNGx1`-style path segment) — never by
  driving the interactive form. The deep-link is anchored
  `config.ANCHOR_OFFSET_MINUTES` before the earliest target departure,
  not at it, because TPE's frontend hardcodes returning only the next 3
  journeys from the anchor with no lever to raise that — anchoring
  exactly at the earliest target risked an earlier service using up a
  "slot" and pushing the later target out of the window (confirmed live,
  see `docs/plans/005-migrate-to-tpe.md` §1.4). Prices are read from the
  same-origin **POST** to `api.tpexpress.co.uk/jp/journey-plan` (JSON,
  one already-discounted `totalPrice` per fare), matched on the exact
  path since the same host also serves sibling endpoints (e.g.
  `/jp/plusbus`) that could otherwise overwrite the real response. **No
  DOM fallback** — no TPE results selector has ever been captured or
  verified, so a truthful `TimeoutScrapeError` is kept as the failure
  mode rather than inventing one (`docs/plans/005-migrate-to-tpe.md` §4.2
  item 9). Iframe/hijack guards are still kept as defense in depth against
  `config.TPE_HOST_SUFFIX` (covering `www.`/`ticket.`/`api.`), since the
  page loads third-party scripts (Usercentrics CMP, Google Maps, PayPal)
  outside our control — even though no hijack has ever been observed on
  TPE. No proxies, stealth plugins, or CAPTCHA-solving — not needed.
- **Term-date logic:** plain Python module `src/term_dates.py` — a
  commented `TERMS` data block (term name, inclusive start/end, excluded
  ranges/days) plus pure functions `is_in_term()`, `is_checkable_day()`,
  `checkable_dates()`. Chosen over YAML (extra dependency) and JSON (no
  comments to explain what a range is). Validated at import;
  `python -m src.term_dates --list` prints every checkable date for
  manual verification. **To update for a new school year, edit only the
  `TERMS` block in that one file.**
- **Hosting/scheduling:** GitHub Actions, `schedule: "37 */6 * * *"`
  (every 6h, at :37 to avoid GitHub's peak-load `:00` minute) plus
  `workflow_dispatch`. No time-of-day gate in Python — only
  weekday/term-time gating on travel **dates**, independent of when the
  job runs (an earlier fixed-hour design meant a delayed cron firing
  silently skipped a whole day). GitHub cron is best-effort; a missed
  firing is harmless since the same dates get checked next time. GitHub
  disables scheduled workflows after 60 days of repo inactivity — any
  commit or manual dispatch re-arms them.
- **Concurrency:** up to `src.main.PARALLEL_DATES` (8) travel dates
  scraped at once, each its own Camoufox instance, via a continuous
  queue scheduler in `main()` (`src/main.py`) — a rolling window
  refilled as soon as any scrape finishes, not fixed batches. Results
  are finalized (logged, counted toward `MAX_CONSECUTIVE_FAILURES`,
  written to `price-history.csv`) strictly in ascending travel-date
  order via a reorder buffer, regardless of completion order, so a
  straggler can never cause an already-scraped result to be dropped.
  The date closest to `FULL_RETRY_HORIZON_DAYS` is dispatched first
  (`src.main._dispatch_order`, gated by
  `src.main.BOUNDARY_PRIORITY_ZONE_DAYS`) since it's predictably the
  slowest to fail. `FULL_RETRY_HORIZON_DAYS` = 168 days (24 weeks) — set
  from Great Western Railway (GWR), not TPE: GWR is the train operating
  company that actually sets fares on this Oxford → Paddington route
  (every fare object in the captured TPE fixture has its `"setter"`
  field pointing at `/data/tocs/GW`), and the user has confirmed, from
  their own domain knowledge of GWR — not a measurement this repo has
  made — that GWR releases weekday advance tickets up to 24 weeks ahead.
  This superseded the old 400-day placeholder set during the NRE→TPE
  migration (itself widened from the NRE-derived value of 95, based on
  94 days measured three times against NRE's fare-release horizon
  specifically — a measurement that never transferred to TPE/GWR). 168
  is comfortably inside the candidate range a run this school year can
  produce, so it reactivates rather than suspends the speculative-attempt
  machinery: dates beyond it are demoted to `SPECULATIVE_ATTEMPTS`, and
  the boundary-priority dispatch above now has a real boundary to work
  with — see `docs/plans/005-migrate-to-tpe.md` §7.1's addendum. The
  speculative-attempt code path itself remains the reactive fallback if
  168 turns out wrong; `MAX_CONSECUTIVE_FAILURES` bounds the cost of
  this assumption being wrong. Per-attempt timing: page-result wait
  budget 20s, navigation timeout 60s (`NAVIGATION_TIMEOUT_SECONDS`), poll
  interval 250ms, scrape-retry backoff 5s/10s — see `src/scraper.py`.
  These per-attempt numbers are themselves **updated but still
  provisional**: the old 10s/20s pair was measured against NRE +
  Chromium (`docs/plans/002-speed-up-price-check-run.md`,
  `docs/plans/003-scheduler-and-retry-horizon.md`); Camoufox/Firefox with
  `humanize=True` has not been measured yet, so 20s/60s are carried over
  from `scripts/capture_fixture_tpe.py`'s validated working values as a
  starting point, to be re-tightened from the first full live run's real
  timings (`docs/plans/005-migrate-to-tpe.md` §4.2 item 8/§7.2/§9). No
  fixed pause between dates (that was serial-only pacing, removed once
  scraping went concurrent).
- **Email service:** Resend free tier, single
  `POST https://api.resend.com/emails` with a Bearer token. No SMTP,
  OAuth, or app-password rotation. `ALERT_EMAIL_TO` may hold one or more
  comma-separated addresses (`src.notifier._parse_recipients`; Resend's
  `to` is always sent as a list). The free `onboarding@resend.dev`
  sender can only deliver to the Resend account owner — sending to
  anyone/anywhere else needs a verified domain and `ALERT_EMAIL_FROM`
  (see README "Sending to more than one address"). The email renders two
  site-styled tables — "under £10, not booked yet" and "already booked,
  current prices" — matching `site/`'s palette (see
  `docs/plans/004-redesign-alert-email.md`). Send trigger is unchanged:
  fires only when `evaluate()` finds ≥1 unbooked fare below threshold
  (or the `TEST_RUN` fallback). `src/notifier.py`'s HTML uses inline
  `style=` attributes only, no `<style>` block — several email clients
  (e.g. Gmail mobile web) strip `<head><style>`.
- **Secrets:** GitHub Actions repo secrets `RESEND_API_KEY`,
  `ALERT_EMAIL_TO`, optional `ALERT_EMAIL_FROM`, injected as step `env:`
  vars. Never committed, never logged; the secrets dataclass redacts
  itself in `__repr__`, and error messages must not leak the key.

## Route details
- From: Oxford
- To: London Paddington
- Departure window: two specific outbound trains from Oxford —
  07:25 departure and 07:30 departure. Check the price of both.
- Return: one-way only, no return leg
- Railcard: 16-25
- Alert threshold: GBP 10.00 for either train. Strictly `price < 10.00`
  — exactly £10.00 does **not** alert. The cheapest price found for a
  journey is used, whether or not it's confirmed as the railcard-priced
  fare — see next bullet.
- Alerting does **not** require the 16-25 discount to be positively
  confirmed. Any unbooked fare under threshold alerts regardless
  (explicit user call: missing a real sub-£10 fare over an unconfirmed
  discount is worse than the reverse). Whether the discount was
  confirmed is still tracked as `railcard_applied` and shown in the
  email (muted `*` + legend, only when some displayed fare's discount
  wasn't confirmed) and in `price-history.csv` — see
  `docs/plans/004-redesign-alert-email.md` §4.6.4. TPE returns one
  already-discounted `totalPrice` per fare (not NRE's separate
  undiscounted/railcard-discounted pair), so `railcard_applied` is
  determined by whether the winning fare's `tickets[].railcard` ref
  matches `config.RAILCARD_CODE`, rather than from a separate
  `railcardFares` array — see `src/parser.py` and
  `docs/plans/005-migrate-to-tpe.md` §3.2.

## Which dates get checked
The gate applies to **travel dates**, not the day the job runs. Each
run enumerates every candidate travel date from tomorrow through the
end of the last known school term (currently Thu 8 Jul 2027), keeps
those passing `is_checkable_day()`, and checks all of them — not a
short lookahead window. Outside term time the candidate list is empty
and the run is a clean no-op (no browser launch, no email).

Early in a term this is 100+ dates per run, checked in full every run
by design so prices stay fresh — accepted tradeoff, mainly wall-clock
run time, not IP reputation (TPE has shown no bot protection to trip).
See `docs/plans/001-train-price-alert.md` §2.2/§1.4. Concurrency
(`PARALLEL_DATES`) is unchanged from the NRE era. The
`FULL_RETRY_HORIZON_DAYS` single-attempt zone that used to also cut run
time was dormant while that constant sat at a 400-day placeholder past
every candidate date; it is now **active** — see Tech decisions →
Concurrency — since `FULL_RETRY_HORIZON_DAYS` = 168 days falls well
within the candidate range, so dates beyond it get demoted to a single
attempt and run time should be somewhat shorter than the fully-dormant
placeholder era implied, though still not directly comparable to old
NRE-era projections.

No TPE fare-release horizon has been measured from this codebase — the
94-day figure NRE-era runs measured (`docs/plans/002-speed-up-price-check-run.md`
§1.7) was specific to NRE and is not evidence about TPE. Fares on this
route, though, are actually set by Great Western Railway (GWR), not TPE
itself — confirmed via the `"setter"` field (`/data/tocs/GW`) on every
fare object in the captured TPE fixture — and the user has confirmed,
from their own domain knowledge of GWR (not a measurement this repo has
made), that GWR releases weekday advance tickets up to 24 weeks
(168 days) ahead. `FULL_RETRY_HORIZON_DAYS` is set to that figure
accordingly (see `docs/plans/005-migrate-to-tpe.md` §7.1's addendum).
§9 of that plan is what should replace this belief with a real number
from the first full live run, should one ever be measured directly
against TPE/GWR.

## Marking a date as already booked (no coding involved)

Once a ticket is booked for a date, that date stops being alerted on —
but its price is still tracked, since the booked-dates website (see
`site/`) shows the last-recorded price for every date, booked or not.
Controlled by a plain text file at the repo root, `booked-dates.txt` —
one `YYYY-MM-DD` per line, `#` for comments, blank lines ignored. To
use it: edit the file on github.com and commit directly from the
browser — no local setup needed. A booked date is still scraped and
appended to `price-history.csv` like any other candidate date; it's
just excluded before the alert-threshold check. See
`docs/plans/001-train-price-alert.md` §2.3 for the parsing rules (a
malformed line is skipped with a warning, not a failed run).

## Term dates (only check Tue/Thu/Fri that fall within these ranges)

Dates are inclusive. A day only counts if it is Tuesday, Thursday, or
Friday AND falls within one of the active ranges below AND is not in
one of the excluded ranges/dates.

### Autumn Term 2026
- Staff INSET: Tue 1 Sep 2026 & Wed 2 Sep 2026
- Induction / HE events (MIV, VII & VIII): Thu 3 Sep 2026
- Term begins: Fri 4 Sep 2026
- Term ends: Wed 16 Dec 2026
- Active range: Tue 1 Sep 2026 – Wed 16 Dec 2026
- Excluded: Half term Mon 19 Oct 2026 – Fri 30 Oct 2026
- Excluded: Occasional Day Fri 20 Nov 2026

### Spring Term 2027
- Staff INSET / VI mock exams start: Wed 6 Jan 2027
- Staff INSET: Thu 7 Jan 2027
- Term begins: Fri 8 Jan 2027
- Term ends: Thu 25 Mar 2027
- Active range: Wed 6 Jan 2027 – Thu 25 Mar 2027
- Excluded: Half term Mon 15 Feb 2027 – Fri 19 Feb 2027

### Summer Term 2027
- Staff INSET: Mon 19 Apr 2027
- Term begins: Tue 20 Apr 2027
- Term ends: Thu 8 Jul 2027
- Active range: Mon 19 Apr 2027 – Thu 8 Jul 2027
- Excluded: Bank holiday Mon 3 May 2027 (falls on a Monday, so would
  never be selected anyway, but listed for completeness)
- Excluded: Half term Mon 31 May 2027 – Fri 4 Jun 2027

Outside of these three active ranges (e.g. summer holidays, Christmas
holidays), no checks should run at all.

## Plans
- `docs/plans/001-train-price-alert.md` — full implementation plan,
  research findings, and the seven task specs. Its NRE-specific research
  (retailer choice, scraping shape, fare-release horizon) is now
  historical — superseded by `docs/plans/005-migrate-to-tpe.md` — kept as
  a record of how this repo got here, not as the current state.
- `docs/plans/002-speed-up-price-check-run.md`,
  `docs/plans/003-scheduler-and-retry-horizon.md`,
  `docs/plans/004-redesign-alert-email.md` — later changes; hold the
  measured evidence for the numbers in Tech decisions, though the
  specific timing/horizon numbers from 002/003 are NRE-era and superseded
  by 005 where the two disagree. Only read these on demand (e.g. when a
  related number needs re-justifying), not routinely.
- `docs/plans/005-migrate-to-tpe.md` — the NRE→TPE migration: retailer
  and browser swap (Playwright/Chromium → Camoufox/Firefox), the
  research/evidence behind every TPE-specific constant in this file, and
  the honest unknowns (TPE's fare-release horizon, Camoufox per-attempt
  timing, `PARALLEL_DATES` under Firefox) still awaiting a real
  measurement.

## Claude Code workflow: use this repo's sub-agents

This repo defines four sub-agents in `.claude/agents/` — `planner`,
`coder`, `reviewer`, `scout`. For a real feature, bug fix, or refactor
(not a one-line edit, a doc typo, or answering a question), route work
through them instead of doing it all in the main conversation. Scale
the pipeline to the change:

1. **`planner`** for anything with real design decisions or multiple
   steps. Skip it for a small, well-scoped change (clear cause, clear
   fix, one or two files) — write a one- or two-line spec yourself and
   hand it straight to `coder`. Don't spawn a planning pass just to
   restate an obvious fix.
2. **`coder`** to implement, given a plan or a direct spec. It doesn't
   make architectural decisions itself.
3. **`reviewer`** after implementation, to check the diff for bugs,
   security issues, and plan adherence.
4. **`scout`** for lightweight research (finding where something is
   defined, checking a doc) in place of doing that search directly.

Keep agent handoffs and reports terse: point at file:line and quote
only the few lines under discussion, never a whole file or a large
diff back into the conversation — the caller can already read the repo.

This is a standing instruction, not a one-off: treat it as the user
having explicitly asked for these named agents on every matching task
in this repo, current session included. It doesn't relax any other rule
about when to check with the user first (risky/irreversible actions,
ambiguous requirements, etc.) — it only says who does the
reading/writing/reviewing once the actual approach is decided.
