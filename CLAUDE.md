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
Full rationale and measured evidence for every item below lives in
`docs/plans/` (referenced inline) — this section is the current-state
summary an agent needs to write correct code, not the history of how we
got here.

- **Language:** Python 3.12. Dependencies pinned in `requirements.txt`
  (`playwright`, `requests`, `pytest`). Money uses `decimal.Decimal`,
  never `float`; all dates/times go through
  `zoneinfo.ZoneInfo("Europe/London")`, never a naive clock read.
- **Retailer: National Rail Enquiries, not any UK train retailer other
  than NRE.** Every non-NRE retailer checked so far is bot-protected —
  most (see exception below) block headless Chromium domain-wide
  (`net::ERR_CONNECTION_RESET` on the deep-link and on unrelated pages
  of the same site) even though plain `curl` gets through cleanly —
  treat this as the default expectation for any new UK train retailer,
  not something to assume safe until proven otherwise. Confirmed so
  far: Trainline itself
  (DataDome, 403 + CAPTCHA); CrossCountry
  (`buy.crosscountrytrains.co.uk`, Cloudflare bot management); East
  Midlands Railway, London Northwestern Railway, Northern, and West
  Midlands Railway (`buytickets.<operator>.co.uk` — all four are the
  same Trainline white-label deployment, confirmed via identical
  webpack bundle hashes, so inherit Trainline's DataDome protection —
  any other operator on this same `buytickets.<operator>.co.uk` shape
  can be assumed the same without a full re-investigation, see
  `docs/plans/001-train-price-alert.md` §1.7's closing note for the
  quick `curl`-only check); TransPennine Express
  (`ticket.tpexpress.co.uk`) — a genuinely different booking engine
  with no DataDome/Cloudflare markers in its static HTML, still blocked
  by an unidentified connection-level bot check (confirmed
  browser-agnostic, not Chromium- or headless-specific — Firefox and
  headed Chromium were also blocked, see §1.9's follow-up); and
  TrainPal (`www.mytrainpal.com`) and Trip.com's own UK rail search
  (`uk.trip.com`) — both the same Trip.com Group platform (shared
  `tripcdn.com` assets, `crash.trip.com` telemetry endpoint,
  `group:Trip`/`group:MyTrainPal` markers), whose embedded telemetry (a
  request-echoing beacon URL in the static page) directly confirms
  **Akamai with JA4 TLS fingerprinting**, the first retailer pair here
  where the connection-level fingerprint is confirmed by the target's
  own diagnostics rather than inferred, and good evidence this is the
  common mechanism (not any single vendor's JS challenge) behind every
  rejection since CrossCountry. None of this is fixable
  without TLS fingerprint spoofing, which is stealth tooling this
  project's standing decision already rules out. The exception to
  "curl gets through": Rail Europe (`www.raileurope.com`), Klook
  (`www.klook.com`) — the same DataDome product blocking Trainline,
  configured to challenge even a plain `curl` request outright (HTTP
  403, `x-datadome: protected`, an explicit CAPTCHA interstitial body
  — identical template on both sites) — and Omio (`www.omio.co.uk`) —
  Cloudflare's interactive "Just a moment..." challenge page instead of
  DataDome, but the same "`curl` alone settles it" outcome. No browser
  probe was needed for any of the three. NRE has no bot protection at
  all (confirmed via 20+ live probe runs). See
  `docs/plans/001-train-price-alert.md`
  §1.4/§1.5/§1.6/§1.7/§1.8/§1.9/§1.10/§1.11/§1.12/§1.13/§1.14/§1.15/§2.2.
- **Scraping approach:** Playwright (sync API), headless Chromium,
  navigating straight to a fully-parameterised deep-link URL —
  `https://www.nationalrail.co.uk/journey-planner/?type=single&origin=OXF&destination=PAD&leavingType=departing&leavingDate=DDMMYY&leavingHour=HH&leavingMin=MM&adults=1&railcards=YNG%7C1&extraTime=0`
  — never by driving the interactive form. Form-filling with a past
  departure time reliably triggered a third-party redirect to a
  Booking.com hotel search; the deep-link (always a valid future
  date/time) never has. Prices are read from the same-origin XHR to
  `jpservices.nationalrail.co.uk/journey-planner` (JSON, includes a
  `railcardFares` array per fare), with a DOM scrape as fallback. Still
  guards against the ad-redirect as defense in depth (blocks
  cross-origin iframes, backstops navigation away from
  `nationalrail.co.uk`) even though the deep-link itself never triggers
  it. No proxies, stealth plugins, or CAPTCHA-solving — not needed.
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
- **Concurrency:** up to `src.main.PARALLEL_DATES` (5) travel dates
  scraped at once, each its own headless Chromium, via a continuous
  queue scheduler in `main()` (`src/main.py`) — a rolling window
  refilled as soon as any scrape finishes, not fixed batches. Results
  are finalized (logged, counted toward `MAX_CONSECUTIVE_FAILURES`,
  written to `price-history.csv`) strictly in ascending travel-date
  order via a reorder buffer, regardless of completion order, so a
  straggler can never cause an already-scraped result to be dropped.
  The date closest to `FULL_RETRY_HORIZON_DAYS` is dispatched first
  (`src.main._dispatch_order`, gated by
  `src.main.BOUNDARY_PRIORITY_ZONE_DAYS`) since it's predictably the
  slowest to fail. `FULL_RETRY_HORIZON_DAYS` = 95 days (NRE's observed
  fare-release horizon, measured at 94 days three times) — dates beyond
  it get one attempt instead of three, since a timeout that far out is
  the expected answer, not a fault; the date is still fetched, logged,
  and alert-eligible every run. Per-attempt timing: page-result wait
  budget 10s, navigation timeout 20s
  (`NAVIGATION_TIMEOUT_SECONDS`), poll interval 250ms, scrape-retry
  backoff 5s/10s — see `src/scraper.py`. No fixed pause between dates
  (that was serial-only pacing, removed once scraping went concurrent).
  See `docs/plans/002-speed-up-price-check-run.md` and
  `docs/plans/003-scheduler-and-retry-horizon.md` for the measurements
  behind these numbers.
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
  `docs/plans/004-redesign-alert-email.md` §4.6.4.

## Which dates get checked
The gate applies to **travel dates**, not the day the job runs. Each
run enumerates every candidate travel date from tomorrow through the
end of the last known school term (currently Thu 8 Jul 2027), keeps
those passing `is_checkable_day()`, and checks all of them — not a
short lookahead window. Outside term time the candidate list is empty
and the run is a clean no-op (no browser launch, no email).

Early in a term this is 100+ dates per run, checked in full every run
by design so prices stay fresh — accepted tradeoff, mainly wall-clock
run time, not IP reputation (NRE has no bot protection to trip). See
`docs/plans/001-train-price-alert.md` §2.2/§1.4. Concurrency
(`PARALLEL_DATES`) and the `FULL_RETRY_HORIZON_DAYS` single-attempt
zone (see Tech decisions → Concurrency) both cut actual run time well
below a naive serial/three-attempt estimate.

NRE's fare-release horizon is observed, not guaranteed — 94 days as of
the 2026-08-31/09-01 measurements (`docs/plans/002-speed-up-price-check-run.md`
§1.7), expected to drift at NRE's December/May timetable changes.

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
  research findings, and the seven task specs.
- `docs/plans/002-speed-up-price-check-run.md`,
  `docs/plans/003-scheduler-and-retry-horizon.md`,
  `docs/plans/004-redesign-alert-email.md` — later changes; hold the
  measured evidence for the numbers in Tech decisions. Only read these
  on demand (e.g. when a related number needs re-justifying), not
  routinely.

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
