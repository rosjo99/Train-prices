# Plan 001 — Train Price Alert Tool

Status: implemented (all tasks). Retained as a design record — CLAUDE.md
is the source of truth for current behaviour.
Author: planner
Date: 2026-08-31

Implements the project described in `CLAUDE.md`: check National Rail
Enquiries for the 07:25 and 07:30 Oxford → London Paddington departures
(one-way, 16-25 railcard) and email an alert when either fare drops below
GBP 10.00, only for travel dates that are a Tuesday/Thursday/Friday
inside school term time.

**How to read this now:** §1 is research justifying non-obvious
choices (why Trainline was abandoned, why NRE was safe to scrape). The
task record (§3) documents what was built and the decisions/bugs behind
it, not how to build it — the code is the spec; don't treat any
signature or acceptance-criteria list below as authoritative if it
disagrees with `src/`.

**Revision note (2026-08-31):** this plan originally targeted Trainline.
§1.1-1.3 below are that investigation, kept for the record since they're
why NRE was tried at all. §1.4 documents the actual pivot and what was
confirmed against the live NRE site before any production code was
written. Everything from §2 onward describes the current, NRE-based
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

The homepage HTML and the `/book/results` page HTML shell both contain
DataDome bootstrap markers (`pushDataDomeEvent`, `datadome`, `captcha`
references). Headers alone can't fix this: DataDome fingerprints
TLS/JA3 and header ordering, and requires a browser-executed JS
challenge to mint its clearance cookie.

### 1.2 What looked like it would work, and didn't

`GET https://www.thetrainline.com/book/results?...` returns HTTP 200 with
a ~750KB JS application shell containing no prices — the page fetches
those client-side from `/api/journey-search/` *after* DataDome's JS
challenge has run. The plan was to drive a real Chromium via Playwright
and let the page solve that challenge itself.

This did not survive contact with real GitHub Actions runs: **two
independent live runs, on two different dates, were both blocked
immediately** by DataDome. Confirms §1.3's risk was not hypothetical.

### 1.3 Principal risk (as assessed before it materialised)

DataDome scores IP reputation, and **GitHub-hosted runners use Azure
datacentre IP ranges, which are commonly flagged.** A self-hosted
runner on a home machine (residential IP) was identified as a possible
Plan B.

Per standing instruction, CAPTCHA-solving services, proxy rotation, or
other evasion escalation were never attempted. Given the confirmed
block in §1.2, the response was not to invoke Plan B but to look for a
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
journey-planner accepts a fully-parameterised query string (`OXF`/`PAD`
CRS codes, `YNG` for the 16-25 Railcard — the same value originally
hypothesised for Trainline, confirmed correct here too, coincidentally —
see CLAUDE.md's Tech decisions for the exact template) that skips
form-filling entirely. Navigating straight to this URL with tomorrow's
date and the 07:25 target time loaded directly into real results — **no
click needed, no redirect, no hijack, in every probe run** — showing
"07:25 journey from Oxford to London Paddington" and "07:30 journey from
Oxford to London Paddington" (both appear because the results list
continues past the anchor time), each priced "Single from £30.60" in the
rendered page text.

Confirming the railcard discount specifically (no alert without positive
confirmation, per the original design — see §2.1 for the later revision)
required looking past the rendered text to the underlying data: the page
makes a same-origin XHR to `jpservices.nationalrail.co.uk/journey-planner`
returning structured JSON where each fare option carries a
`railcardFares` array distinct from its `undiscountedPrices`, e.g.:
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

### 1.5 CrossCountry investigated (2026-09-01) and rejected — Cloudflare bot management

Motivation: NRE's journey planner doesn't expose every date NRE's own
fare-release horizon should allow (see `FULL_RETRY_HORIZON_DAYS` in
CLAUDE.md) — worth checking whether CrossCountry's own booking site,
which also supports a fully-parameterised deep-link
(`https://buy.crosscountrytrains.co.uk/search?origin=GBOXF&destination=GBQQP&adults=1&children=0&outboundTime=<ISO8601>&outboundTimeType=DEPARTURE&railcards=%5B%7B%22Code%22:%22UK_YOUTH%22,%22Number%22:1,%22Type%22:%22DISCOUNT_CARD%22%7D%5D&ls=LS_1_3&p=PRICE_P_1_19`),
could supplement or replace NRE for a longer booking horizon.

Result: rejected. `buy.crosscountrytrains.co.uk` sits behind Cloudflare
bot management, confirmed live:
- `curl` (no browser) against the deep-link consistently gets a
  Cloudflare "Attention Required" / "Sorry, you have been blocked" page,
  HTTP 403, `server: cloudflare` — 3/3 attempts.
- Headless Chromium via Playwright (this project's own scraping
  approach, `--disable-blink-features=AutomationControlled`, real
  Chrome UA, `en-GB` locale) doesn't even get a response: the TLS
  connection is reset (`net::ERR_CONNECTION_RESET`) before any HTML
  loads — reproduced twice on the deep-link URL and again on the plain
  `www.crosscountrytrains.co.uk` and `buy.crosscountrytrains.co.uk`
  homepages, so this is a domain-wide block, not specific to the search
  endpoint. Confirmed not a proxy artifact — the outbound proxy's own
  status log shows the far side (`buy.crosscountrytrains.co.uk:443`)
  closing the tunnel mid-handshake, matching the client-side reset.

This is the same category of blocker Trainline was rejected for in
§1.1-1.3 (bot protection that fingerprints and drops automated
traffic), just a different vendor (Cloudflare here vs. DataDome for
Trainline) and a harder failure mode (connection-level reset vs. a
CAPTCHA page). Per this project's standing decision to use no proxies,
stealth plugins, or CAPTCHA-solving (CLAUDE.md's Tech decisions), there
is no further mitigation to try within the project's own constraints.
NRE remains the only viable retailer for this project; CrossCountry is
not a source of additional dates.

### 1.6 East Midlands Railway investigated (2026-09-01) and rejected — it's Trainline, DataDome-protected

Same motivation as §1.5: EMR's booking site
(`buytickets.eastmidlandsrailway.co.uk`) also accepts a
fully-parameterised deep-link
(`https://www.buytickets.eastmidlandsrailway.co.uk/book/results?journeySearchType=single&origin=<id>&destination=<id>&outwardDateType=departAfter&outwardDate=<ISO8601>&passengers%5B%5D=<dob>&passengerDiscountCards%5B%5D=<id>&directSearch=false&selectedCarrierFilterTab=ALL_TRAINS&bookingToken=&referrer=MKT&selectedOutward=<token>`),
worth checking as a second alternative source of dates.

Result: rejected — it's the same site as Trainline. The static HTML
`curl` fetches (HTTP 200, 3/3 attempts, real ~990KB page markup, no
block page) is served under `data-test="app-EastMidlandsRailwayWeb-EastMidlandsRailway"`
and itself loads `js.datadome.co/tags.js` and
`static.trainlinecontent.com` — EMR's booking engine is a Trainline
white-label deployment, not an independent site, so it inherits
Trainline's DataDome protection (the reason Trainline itself was
rejected in §1.1-1.3). Confirmed live: headless Chromium via
Playwright (this project's own approach) gets `net::ERR_CONNECTION_RESET`
on the deep-link, reproduced twice, and again on the unrelated
`www.eastmidlandsrailway.co.uk` marketing homepage — a domain-wide
block, same signature as §1.5's CrossCountry finding (outbound proxy's
own status log confirms the far side closing the tunnel mid-handshake,
ruling out a proxy-side cause). `curl` succeeding where headless
Chromium is reset outright shows DataDome here is fingerprinting and
blocking at the connection level specifically for the automated
browser, not blocking all traffic indiscriminately.

No further investigation needed: this is architecturally the same
site Trainline was already rejected for, so the same conclusion
applies without a separate cost/benefit case. NRE remains the only
viable retailer.

### 1.7 London Northwestern Railway investigated (2026-09-01) and rejected — also Trainline

Same shape again: LNR's booking site
(`buytickets.londonnorthwesternrailway.co.uk`) accepts the identical
deep-link URL shape as §1.6's EMR one, just with `<carrier>` swapped
in the hostname. Checked as a third possible additional source of
dates.

Result: rejected, immediately — it's the same Trainline white-label
engine as EMR, not a separate investigation. The static HTML `curl`
fetches (HTTP 200, 3/3 attempts) is served under
`data-test="app-LondonNorthwesternRailwayWeb-LondonNorthwesternRailway"`,
loads `js.datadome.co`/`static.trainlinecontent.com` exactly like
EMR's did, and — stronger evidence than EMR's page alone — ships the
*same webpack bundle filenames/hashes* as EMR's page
(`app.010e8419aad13e9266b2.mjs`, `runtime~app.c126f6b8ef1209832b95.mjs`,
`vendors.2a45cbf76fcf3d3d8ef1.mjs`), confirming EMR and LNR aren't
just similarly built but are served from the shared Trainline
white-label deployment. Confirmed live: headless Chromium gets
`net::ERR_CONNECTION_RESET` on both the deep-link and the unrelated
`www.londonnorthwesternrailway.co.uk` marketing homepage — same
domain-wide block signature as §1.5/§1.6.

Given the shared-infrastructure evidence, any other train operating
company found serving from `buytickets.<operator>.co.uk` with this
same page shape should be assumed Trainline/DataDome-protected without
a full re-investigation — check for the `app-<Operator>Web-<Operator>`
`data-test` marker and `js.datadome.co`/`static.trainlinecontent.com`
references in the plain HTML (visible even via `curl`, no browser
needed) before spending a live headless-browser probe on it. NRE
remains the only viable retailer.

---

## 2. Decisions not already in CLAUDE.md

Full tech-stack rationale (language, retailer, scraping approach,
money/time handling, term-date module, hosting, email, secrets) now
lives in `CLAUDE.md`'s Tech decisions section — not repeated here. This
section covers decisions and revisions specific to this plan.

### 2.1 Alert semantics (decided, do not re-litigate)

Threshold fires on `price < Decimal("10.00")` — exactly £10.00 does not
fire.

The original decision here was: if the railcard's application couldn't
be positively confirmed in the response, no alert is sent and the run
fails loudly (exit 1) — "a wrong price in an alert is worse than a
missed alert." The user later clarified this wasn't what they wanted:
their concern was narrower — don't send an email unless there's a real,
unbooked fare below £10 (railcard-discounted or not), not "never alert
on a fare whose railcard discount wasn't confirmed." Asked directly, the
user chose: alert on any sub-threshold fare, railcard confirmation or
not. `railcard_applied` is still computed and carried through as
informational metadata (email + `price-history.csv`) but no longer gates
whether an alert fires. This is why a future agent shouldn't "restore"
the gate.

### 2.2 Which travel dates get checked (decided)

Those are **travel dates**, not run dates — alerting on the morning of a
07:25 departure is useless. Decision (per explicit user instruction,
superseding this plan's original 14-day-horizon draft): each run
enumerates **every** candidate travel date from tomorrow through the end
of the last known school term (`term_dates.LAST_KNOWN_DATE` — currently
2027-07-08), keeps those passing `is_checkable_day()`, and checks every
one of them, every run.

**Volume implication, flagged for visibility rather than left implicit:**
computed directly from the term data, a run on the first day of Autumn
Term 2026 has **102 candidate dates** to check (39 within just that one
term). At an estimated 15-25s per date, a full serial run early in the
year would take on the order of 30-45 minutes and make 100+ requests to
National Rail Enquiries from the same IP, once a run. Unlike the
abandoned Trainline design, this is not a known risk: NRE has no bot
protection to trip (§1.4), so this volume mainly costs wall-clock run
time, not IP reputation — the user has explicitly chosen this tradeoff
(fresh data on every remaining date, checked every run) over a
lighter-weight alternative. Since dates are now batched concurrently
(see CLAUDE.md's Concurrency), actual run time is a fraction of that
original serial estimate.

### 2.3 Excluding already-booked dates (decided)

Decision: a plain text file at the repo root, `booked-dates.txt`, one
`YYYY-MM-DD` per line, `#` for comments, blank lines ignored — the "no
coding involved" mechanism the user asked for (edit on github.com, click
the pencil icon, commit from the browser).

Rejected alternatives: a repo secret or Actions variable (edits require
the Settings UI, worse UX, secrets aren't meant to be listed items,
awkward to view what's already there at a glance); a GitHub Issue label
per date (over-engineered — parsing issue titles/labels is more moving
parts for the same result); a `workflow_dispatch` input (does not
persist across scheduled runs — it would have to be re-entered every
time). A committed text file is the simplest thing a non-programmer can
maintain and is fully version-controlled for free.

Parsing rules: a missing file returns an empty set (first-time setup
must not be an error); blank lines and `#`-comments are ignored; a line
that fails to parse as a date is **logged as a warning and skipped**,
never fatal — one typo in a hand-edited file must not take down the
whole run.

**Superseded note:** an earlier revision of this design excluded booked
dates from scraping entirely ("a booked date costs zero requests, not
just zero alerts"). This was later changed: booked dates are still
scraped and appended to `price-history.csv` (so the booked-dates website
keeps showing a fresh price for them), only suppressed at the
alert-threshold check — see CLAUDE.md's "Marking a date as already
booked" for the current behaviour.

---

## 3. Task record

Eight tasks were implemented (originally specced as seven; Task 8 was
added later per explicit user request). What follows is what each
actually built and the decisions/bugs behind it — not a spec to
re-implement, since the code already exists.

### Task 1 — Scaffolding and configuration

`requirements.txt` (pinned `playwright`, `requests`, `pytest`),
`.gitignore`, `src/config.py`, `src/models.py` (`TrainOption`,
`CheckResult` dataclasses). The original scaffold specified
`ORIGIN_URN`/`PASSENGER_DOB` fields for a URN-based Trainline design;
these were superseded once Task 3 pivoted to NRE, which addresses
stations by CRS code and needs no passenger date of birth at all.

### Task 2 — Term-date logic

`src/term_dates.py` + `tests/test_term_dates.py`. Chosen as a plain
Python module over YAML (extra dependency) or JSON (no comments — and
the comments are what tell a human which range is a half term; also
noted in CLAUDE.md). The `--list`/`--check` CLI is the human's
verification tool after editing the data block each school year.

Edge case worth flagging: exclusion ranges are inclusive at both ends —
off-by-one here silently causes a wasted or missed check.

Spot-checked `tests/test_term_dates.py` against this doc's original
acceptance criteria: `test_last_known_date` asserts
`LAST_KNOWN_DATE == date(2027, 7, 8)`, and
`test_half_term_start_boundary_excluded`/`test_half_term_end_boundary_excluded`
assert `is_in_term()` is `False` at both 2026-10-19 and 2026-10-30. All
three are present, so no gap to flag here.

### Task 3 — Playwright scraper + fixture capture

Implemented against National Rail Enquiries rather than the Trainline
design this task originally specced — see §1.4 for the full discovery
story.

Response-shape discovery: top-level `outwardJourneys` array; each
journey's `timetable.scheduled.{departure,arrival}` is a full ISO 8601
timestamp **with its own UTC offset already applied** (e.g.
`"2026-09-01T07:25:00+01:00"`), not a bare `"HH:MM"`; prices
(`totalPrice`, `undiscountedPrices.*`, and each `railcardFares[].prices`)
are integer **pence**; `railcardFares` entries are
`{code, count, prices: {adult, child}}`.

Fixture provenance: `tests/fixtures/journey_search_sample.json` was
captured 2026-09-08 (a real term-time Tuesday) via the
`capture-fixture.yml` workflow, then scanned for session-identifying
fields (tokens, cookies, booking/customer IDs) before committing — the
only per-request identifier left in is `searchId`, a UUID.

Known unknown: the DOM-scrape fallback (`RESULTS_DOM_SELECTOR`) has
never actually been exercised against the real site — every probe run
to date captured the XHR successfully, so this path is unverified.

Bug fix (host-vs-path matching): `jpservices.nationalrail.co.uk` serves
sibling endpoints (e.g. `/fare-info`) on the same host. The response
handler originally matched on host substring alone, so whichever
endpoint's response landed *last* in a given page load — a race
unrelated to the travel date itself — silently overwrote the real
`/journey-planner` body, producing intermittent, non-deterministic
`ParseError`s on otherwise-good dates. Fixed by matching on the actual
URL path via `urlparse`, not a host substring; regression test
`test_sibling_endpoint_on_same_host_is_ignored_even_if_it_arrives_last`
in `tests/test_scraper.py` replays a `/fare-info` response arriving after
the real one and asserts the real journey data still wins.

**Standing instruction:** if NRE starts blocking Playwright, stop and
report — do not add proxies, CAPTCHA solvers, or stealth plugins, since
that would be new, unexpected behaviour from a site with no bot
protection at any point in this project's testing, worth investigating
before working around. (Repeated in §5's review checklist.)

### Task 4 — Response parser

`src/parser.py`. Prices are converted pence→`Decimal` by dividing the
pence integer by `Decimal("100")`, never via `float`. ISO timestamps
carry their own UTC offset already — don't assume `+01:00` unconditionally,
it's `+00:00` outside BST. No explicit currency field was ever observed
in any captured NRE response, which is why `TrainOption.currency` is
hardcoded `"GBP"` rather than read from the payload.

Revision (superseding the original "railcard fare specifically" spec,
following §2.1's revision): price is now the cheaper of a fare's own
plain `totalPrice` and any matching `railcardFares` entry, ties favouring
the railcard entry; `railcard_applied` records only whether the winning
price actually came from a railcard entry; `sold_out` is simply
`price is None`.

### Task 5 — Email notifier

`src/notifier.py`. The API key must never appear in an exception message
or a logged response body — response bodies are truncated and scanned
for the key substring before logging. Each row shows its own
`railcard_applied` status (following §2.1's revision — there's no longer
a single fixed "16-25 Railcard" heading true of every row). Per explicit
request, the per-fare booking link was dropped entirely — the email is
now purely informational, with no outbound link.

### Task 6 — Orchestrator, booked-date exclusion, and alert decision

`src/main.py`, `src/booked_dates.py`. The booked-dates revision (still
scraped/logged, only alert-suppressed) is covered in §2.3, not repeated
here. The `railcard_unconfirmed` run-wide safety-check removal is
covered in §2.1, not repeated here.

Revision: stop early after `MAX_CONSECUTIVE_FAILURES = 5` consecutive
failed candidate dates (not attempts). National Rail Enquiries only
publishes fares roughly 12 weeks ahead, so dates beyond that horizon
fail deterministically every time — without this, a run early in a term
would burn its full per-date retry budget on ~100 dates that were
certain to fail anyway. The counter increments once per failed date
(`ScraperError`/`ParseError`, not `BlockedError`/`HijackedError`, which
already abort the whole run) and resets on any date that succeeds; a
single date's own internal scrape retries don't count separately, only
its final outcome does.

### Task 7 — GitHub Actions workflow and documentation

`.github/workflows/price-check.yml`, `test.yml`, `README.md`.

The original "run at a fixed London wall-clock hour" design (a dual
19:00/20:00 UTC cron pair plus a `RUN_HOUR_LONDON` gate in
`src/main.py`) was later superseded entirely — see CLAUDE.md's
Hosting/scheduling for why (a delayed cron firing meant the whole day
silently no-op'd). It's recoverable from git history if ever needed
again; not reproduced here.

Revision: "one test, not a grid of toggles." An early version exposed
three independent `workflow_dispatch` checkboxes (`dry_run`,
`send_test_email`, `skip_time_gate`); explicit user feedback was that
they wanted one type of test that does the complete real thing (scrape,
log, send an email), not a combination of toggles to reason about.
`DRY_RUN` and a synthetic £7.77 fake fare were removed outright; a test
run never invents data — `_best_effort_matches_for_test()` sends the
single cheapest real fare found across the whole run when nothing was
genuinely below threshold, and sends nothing at all if literally
nothing priced was found.

Relocated from Task 1 — the `MAX_DATES=all` sentinel: a live manual run
showed clearing the `max_dates` field in GitHub's own "Run workflow" web
UI still ran only 1 date, because that UI always re-submits a
`workflow_dispatch` input's declared default whenever the field is left
blank — there is no way to actually submit a blank override from that
specific UI (API/CLI dispatch with the input omitted is unaffected).
`_read_max_dates()` therefore also treats the literal string `"all"`
(case-insensitive) as "no cap," since that value does survive being
typed into the web form.

### Task 8 — Price history log and booked-dates website

`src/price_log.py`, `scripts/export_terms.py`, `site/`,
`.github/workflows/deploy-pages.yml`. Added per explicit user request
after Task 7, superseding §4's original deferral of price history and a
web dashboard — delivered in a much narrower form than a full dashboard.

Price history CSV: append-only, one row per (travel_date,
target_departure, option-or-None), written once per successfully-parsed
candidate date — never for a date that failed to scrape/parse. The
column set is self-documenting from the CSV header. Persisting across
runs (each a fresh Actions checkout) requires committing the file back;
the commit step does `git pull --rebase` first, in case the booked-dates
website or another run committed in the meantime.

Booked-dates site: a static page (`site/`) with no backend, using the
GitHub Contents API directly from the visitor's browser, authenticated
with a fine-grained personal access token stored only in that browser's
`localStorage`. `scripts/export_terms.py` regenerates term data at
deploy time; the JS port of `checkable_dates()` compares ISO date
*strings* (not JS `Date` objects) to sidestep JS's local-timezone
footguns, and was verified byte-for-byte identical to the real Python
output across all 102 dates from 2026-09-01.

Public-repo write access: confirmed live via `list_repository_collaborators`
that write access is independently gated by GitHub's own collaborator
permissions regardless of repo visibility or the token used — making the
repo public does not grant a stranger's token new permissions. A custom
client-side login layered on a backend-less static site would be
strictly weaker than relying on GitHub's own auth (any shared secret
baked into client-side JS is visible to anyone who opens dev tools) —
this pre-empts a plausible future "let's add a password to the site"
request.

Revision: cheap/booked rows are colour-coded on the site (green for a
last-recorded price under threshold, blue for booked, booked taking
priority when both apply).

Bug fix (409 on save): `githubGetFile()` was missing `cache: "no-store"`,
so a stale cached `sha` could make a legitimate save get rejected as a
conflict. Fixed with `no-store` plus a 3-attempt retry loop in
`toggleDate()` that re-fetches and retries specifically on a 409.

Bug fix (fresh saves appearing to vanish on refresh): reads went through
`raw.githubusercontent.com`, a CDN-cached endpoint that can lag a commit
by several minutes, so a refresh right after your own save could show
stale data. Fixed by reading through the authenticated Contents API (no
CDN, already `no-store`) whenever a token is present, falling back to the
raw endpoint for anonymous read-only viewing.

---

## 4. Out of scope / future work

Do not implement these without a new plan:

- **Duplicate-alert suppression.** If a fare sits below £10 for a week
  you get an email every day. The obvious fix is a small state file (or
  deriving "already alerted" from `price-history.csv`, now that Task 8
  added it) keyed on (travel date, departure time, price bucket). Still
  deferred — Task 8 removed the original "adds write permissions"
  objection, but this is state-management complexity for a comfort
  problem nobody has asked for yet.
- Return legs, other routes, other railcards, other thresholds.
- Split-ticketing or comparison against other retailers.
- A full browsable dashboard over `price-history.csv` — Task 8 delivered
  a narrower form instead (an append-only CSV log, plus a single-purpose
  booked-dates checkbox page), not a general price-history UI.

## 5. Review checklist (for the reviewer agent, after implementation)

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
8. Decimal precision at the alert boundary: `9.99` vs `9.989999...` must
   not decide whether an email is sent.
9. GitHub Actions runners are UTC-only; a 23:30 UTC run is already
   "tomorrow" in London for part of the year — a real trap for naive
   datetime handling.
10. If NRE starts blocking, stop and report — do not add proxies,
    CAPTCHA solvers, or stealth plugins.
11. `price-history.csv` is append-only — no code path should ever
    truncate or rewrite its existing rows.
