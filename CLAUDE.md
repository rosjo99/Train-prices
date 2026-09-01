# Train Price Alert Tool

## What this project does
Checks National Rail Enquiries every 6 hours for the price of two
specific Oxford → London Paddington trains (searched with a 16-25
railcard applied) and emails an alert when either fare's cheapest price
falls below GBP 10 — whether or not that price is confirmed as the
railcard-discounted one (see Route details). Only travel dates that are
a Tuesday, Thursday or Friday inside school term time are checked. A
date already marked as booked (see "Marking a date as already booked")
is still checked and logged, just never alerted on.

## Constraints
- Must handle NRE's dynamic (client-rendered) journey planner — a
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
- **Language:** Python 3.12. Dependencies pinned in `requirements.txt`
  (`playwright`, `requests`, `pytest`). Money is handled with
  `decimal.Decimal`, never `float`; all dates and times go through
  `zoneinfo.ZoneInfo("Europe/London")`, never a naive clock read.
- **Retailer: National Rail Enquiries, not Trainline.** Trainline sits
  behind DataDome bot protection — probed live on 2026-08-31, both
  `thetrainline.com/api/journey-search/` and `trainline.eu/api/v5_1/search`
  returned HTTP 403 with a DataDome CAPTCHA redirect, and this was
  reconfirmed by two live GitHub Actions runs against a real deep-linked
  results URL, both blocked immediately — Azure datacentre IP ranges are
  evidently flagged. National Rail Enquiries (`nationalrail.co.uk`) has
  **no bot protection at all**: confirmed over 20+ live probe runs from
  GitHub-hosted runners, zero CAPTCHA/block markers, `is_bot: false`
  from its own analytics.
- **Scraping approach:** Playwright (sync API) driving headless
  Chromium, navigating straight to a fully-parameterised deep-link URL —
  `https://www.nationalrail.co.uk/journey-planner/?type=single&origin=OXF&destination=PAD&leavingType=departing&leavingDate=DDMMYY&leavingHour=HH&leavingMin=MM&adults=1&railcards=YNG%7C1&extraTime=0`
  — rather than driving the interactive form. This matters beyond
  convenience: interactively filling the form and clicking submit
  reliably triggered a third-party ad redirecting the whole tab to a
  Booking.com hotel search (root-caused, after many iterations, to
  searching *today's* date with both target departures already hours in
  the past — NRE's own "no journeys found" flow apparently offers a
  hotel search instead). The deep-link approach specifies the correct
  future date and time up front and, across every probe run, loaded
  straight into real results with **no redirect at all** and no click
  needed. The scraper reads prices out of the same-origin XHR the page
  itself makes to `jpservices.nationalrail.co.uk/journey-planner`
  (structured JSON, more stable than DOM text — includes a
  `railcardFares` array per fare with the 16-25 discount distinctly
  priced), with a DOM scrape as fallback. It still guards against the
  ad-redirect behaviour (blocking cross-origin iframe documents,
  backstopping any navigation away from `nationalrail.co.uk`) as cheap
  defense in depth for an unattended daily job, even though the
  deep-link approach itself never triggered it.
  No proxies, stealth plugins, or CAPTCHA-solving services — none of
  that is needed here since there's no bot protection to evade.
- **Term-date logic:** a plain Python module, `src/term_dates.py`,
  holding a commented `TERMS` data block (term name, inclusive start/end,
  excluded ranges, excluded single days) plus pure functions
  `is_in_term()`, `is_checkable_day()` and `checkable_dates()`. Chosen
  over YAML (extra dependency) and JSON (no comments — and the comments
  are what tell a human which range is a half term). Data is validated at
  import, and `python -m src.term_dates --list` prints every checkable
  date so a human can verify after editing. **To update for a new school
  year, edit only the `TERMS` block in that one file.**
- **Hosting/scheduling:** GitHub Actions, `schedule: "37 */6 * * *"`
  (every 6 hours, at :37 rather than the top of the hour so it doesn't
  land on GitHub Actions' own peak-load minute — jobs scheduled for `:00`
  are the ones most likely to be delayed) plus `workflow_dispatch` for
  manual runs. There is deliberately **no time-of-day gate in Python any
  more** (an earlier design ran once daily at a fixed London wall-clock
  hour via a dual BST/GMT cron pair plus a `RUN_HOUR_LONDON` check in
  `src/main.py` — dropped because a delayed cron firing meant the whole
  day's check silently no-op'd, which is worse than just checking a bit
  later). All that's left in Python is weekday/term-time gating (which
  travel **dates** get checked), which is independent of when the job
  itself happens to run. GitHub cron is still best-effort — delayed or
  occasionally skipped firings are harmless, since the same dates just
  get checked at the next firing instead. Note that GitHub disables
  scheduled workflows in repos with no activity for 60 days; any commit
  or manual dispatch re-arms them.
- **Concurrency:** up to `src.main.PARALLEL_DATES` (5) travel dates are
  scraped at once, each in its own headless Chromium browser, via a
  continuous queue scheduler in `main()` (`src/main.py`) rather than fixed
  batches: a rolling window of up to `PARALLEL_DATES` in-flight scrapes,
  refilled the instant any one finishes, so one slow date no longer idles
  the rest of the pool behind it. Results are still finalized — logged,
  counted toward `MAX_CONSECUTIVE_FAILURES`, written to
  `price-history.csv` — strictly in ascending travel-date order via a
  reorder buffer, regardless of the order scrapes actually complete in,
  so a straggler can never cause an already-scraped result to be dropped
  (the old batch scheduler could silently discard up to
  `PARALLEL_DATES - 1` already-fetched dates when the early stop fired
  mid-batch; the new one finalizes everything already in flight). One
  date — the latest candidate still inside `FULL_RETRY_HORIZON_DAYS` — is
  dispatched first regardless of its position in the date-ordered list
  (`src.main._dispatch_order`, zone-gated by
  `src.main.BOUNDARY_PRIORITY_ZONE_DAYS`), since it's the one predictably
  slow to fail and dispatching it early overlaps its worst case with the
  rest of the run instead of appending it to the tail. NRE has no bot
  protection to trip (see above), so this is purely a wall-clock lever,
  not something that needed rate-limiting first. Per-attempt timing was
  also tightened: the page-result wait budget (45s → 20s → 10s;
  navigation itself still gets its own 20s via
  `NAVIGATION_TIMEOUT_SECONDS`), the poll interval (500ms → 250ms), and
  the scrape-retry backoff (30s/90s → 10s/20s → 5s/10s) — see
  `src/scraper.py`. The fixed 5-15s pause previously inserted between
  every date (serial-only pacing, not needed once dates are scheduled
  concurrently) was removed entirely. Separately, `src.main.
  FULL_RETRY_HORIZON_DAYS` (95 days — the horizon has now measured at
  exactly 94 days three times, with zero drift) gives dates beyond NRE's
  observed fare-release horizon a single attempt instead of three — a
  timeout that far out is the expected answer, not a fault, so retrying
  it three times just buys the same answer three times slower. This only
  changes attempt count: the date is still fetched, still logged to
  `price-history.csv`, and still eligible to alert every run (see "Which
  dates get checked"); see `docs/plans/002-speed-up-price-check-run.md`
  and `docs/plans/003-scheduler-and-retry-horizon.md` for the measured
  evidence behind all of these numbers.
- **Email service:** Resend free tier (3,000 emails/month) via a single
  `POST https://api.resend.com/emails` with a Bearer token. No SMTP,
  OAuth, or app-password rotation. `ALERT_EMAIL_TO` may hold one or more
  comma-separated addresses (`src.notifier._parse_recipients` splits
  them, and Resend's `to` field is always sent as a list). The free
  `onboarding@resend.dev` sender may only deliver to the Resend account
  owner's own address, so sending to more than one recipient — or to any
  recipient other than the account owner — requires verifying a domain
  in Resend first and setting `ALERT_EMAIL_FROM` to an address on it
  (see README.md's "Sending to more than one address").
- **Secrets:** GitHub Actions repository secrets `RESEND_API_KEY`,
  `ALERT_EMAIL_TO`, and optional `ALERT_EMAIL_FROM`, injected as step
  `env:` vars. Never committed, never logged; the secrets dataclass
  redacts itself in `__repr__` and error messages must be checked to not
  contain the key.

## Route details
- From: Oxford
- To: London Paddington
- Departure window: two specific outbound trains from Oxford —
  07:25 departure and 07:30 departure. Check the price of both.
- Return: one-way only, no return leg
- Railcard: 16-25
- Alert threshold: GBP 10.00 for either train. The comparison is
  strictly `price < 10.00` — a fare of exactly £10.00 does **not**
  trigger an alert. The cheapest price found for a journey is used,
  whether or not it's confirmed as coming from a 16-25 railcard-priced
  fare — see the next bullet.
- Alerting does **not** require the 16-25 railcard discount to be
  positively confirmed. Any unbooked fare under threshold triggers an
  alert regardless (the user's explicit call: not alerting on a real
  sub-£10 fare because its railcard discount specifically couldn't be
  confirmed is a worse outcome than the reverse). Whether the discount
  was confirmed is still tracked as `railcard_applied` and shown in both
  the email and `price-history.csv`, purely as information.

## Which dates get checked
The gate applies to **travel dates**, not the day the job happens to run.
Each daily run enumerates **every** candidate travel date from tomorrow
through the end of the last known school term (currently Thu 8 Jul
2027), keeps those passing `is_checkable_day()`, and checks every one of
them — not just a short lookahead window. Outside term time the
candidate list is empty and the run is a clean no-op — no browser
launch, no email.

This is checked in full on every run by design, so prices are always
fresh for every remaining date, at the cost of a much larger workload:
early in a term this is over 100 dates checked per run, 100+ automated
requests to National Rail Enquiries from one IP, four times a day.
Unlike the abandoned Trainline approach, NRE has no bot protection to
trip (see Tech decisions), so this volume is not a known risk — see
`docs/plans/001-train-price-alert.md` §2.2/§1.4 for the original numbers
and the accepted tradeoff (mainly wall-clock run time, not IP
reputation). Since dates are now checked up to `PARALLEL_DATES` at once,
continuously scheduled, rather than strictly one at a time (see Tech
decisions), actual run time is a fraction of that original estimate. Most
of those 100+ requests
beyond NRE's fare-release horizon are now cheap, single-attempt probes
(`src.main.FULL_RETRY_HORIZON_DAYS`, see Tech decisions → Concurrency)
rather than full three-attempt retry cycles, since a request that far out
is expected to time out rather than genuinely fail — it is still made
and still logged every run, not skipped.

NRE's fare-release horizon is observed, not guaranteed: measured at 94
days (13.4 weeks) ahead as of the 2026-08-31/09-01 measurements in
`docs/plans/002-speed-up-price-check-run.md` §1.7 — more than the
"roughly 12 weeks" this doc previously assumed. Treat 94 as one season's
observation, expected to drift at NRE's own December/May timetable-change
dates, not a hard constant.

## Marking a date as already booked (no coding involved)

Once a ticket is booked for a date, that date should stop being alerted
on — but its price is still worth tracking, since the booked-dates
website (see `site/`) shows the last-recorded price for every date,
booked or not. This is controlled by a plain text file at the repo root,
`booked-dates.txt` — one `YYYY-MM-DD` per line, `#` for comments, blank
lines ignored. To use it: open the file on github.com, click the pencil
(edit) icon, add a line with the date, and commit directly from the
browser — no local setup, no Python, no pull request needed. The next
run reads the file fresh: a date listed in it is still scraped and
appended to `price-history.csv` exactly like any other candidate date,
it's just excluded before the alert-threshold check, so a genuinely
cheap fare on a booked date never triggers an email. See
`docs/plans/001-train-price-alert.md` §2.3 for the original design
rationale (written when booked dates were skipped entirely — since
revised to still check/log them, only suppressing the alert) and the
parsing rules (a malformed line is skipped with a warning rather than
failing the whole run).

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
  research findings, and the seven task specs.

## Claude Code workflow: use this repo's sub-agents

This repo defines four sub-agents in `.claude/agents/` — `planner`,
`coder`, `reviewer`, `scout`. For any substantive change in this repo (a
real feature, bug fix, or refactor — not a one-line edit, a doc typo, or
just answering a question about the code), route the work through them
rather than doing it all directly in the main conversation:

1. **`planner`** first, for any design decision, multi-step
   implementation plan, or task decomposition — before any code is
   written. Give it the goal and relevant context; it does not implement.
2. **`coder`** to implement, once there's a plan (from `planner`, or
   from the user directly for something simple enough not to need one).
   Hand it the plan/spec, not just the original goal — it doesn't make
   architectural decisions itself.
3. **`reviewer`** after implementation, to check the diff for bugs,
   security issues, and adherence to the plan before calling the work
   done.
4. **`scout`** for lightweight research along the way — checking docs,
   finding where something is defined, reading a handful of files — used
   in place of doing that search directly, whenever it doesn't need deep
   reasoning to interpret.

This is a standing instruction, not a one-off: treat it as the user
having explicitly asked for these named agents on every matching task in
this repo, current session included, so it applies without having to be
repeated. It doesn't relax any other rule about when to check with the
user first (risky/irreversible actions, ambiguous requirements, etc.) —
it only says who does the reading/writing/reviewing once the actual
approach is decided.
