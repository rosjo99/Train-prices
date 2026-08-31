# Train Price Alert Tool

## What this project does
Checks Trainline daily for the price of two specific Oxford → London
Paddington trains and emails an alert when either fare falls below
GBP 10 with a 16-25 railcard applied. Only travel dates that are a
Tuesday, Thursday or Friday inside school term time are checked.

## Constraints
- Must handle Trainline's dynamic rendering — confirmed to require a
  headless browser (see Tech decisions)
- Runs once daily via GitHub Actions cron
- Email via a free-tier service
- Prices must reflect 16-25 railcard discount
- Target days: Tuesday, Thursday, Friday only
- Only check dates that fall within school term time (see Term dates
  below) — including staff INSET days, but excluding half terms,
  occasional days, and bank holidays

## Tech decisions
- **Language:** Python 3.12. Dependencies pinned in `requirements.txt`
  (`playwright`, `requests`, `pytest`). Money is handled with
  `decimal.Decimal`, never `float`; all dates and times go through
  `zoneinfo.ZoneInfo("Europe/London")`, never a naive clock read.
- **Scraping approach:** Playwright (sync API) driving headless Chromium.
  Raw HTTP is **not viable**: probed on 2026-08-31, both
  `thetrainline.com/api/journey-search/` and `trainline.eu/api/v5_1/search`
  return HTTP 403 with a DataDome CAPTCHA redirect, and the old
  `locations-service/v2/search` endpoint now 404s. Trainline sits behind
  DataDome bot protection, which needs a browser-executed JS challenge.
  The scraper navigates to a deep-linked `/book/results` URL and reads
  prices out of the `/api/journey-search/` XHR response the page itself
  makes (structured JSON, more stable than DOM text), with a DOM scrape
  as fallback.
  *Known risk:* DataDome scores IP reputation and GitHub-hosted runners
  use Azure datacentre ranges. If runs start returning `BlockedError`,
  the fallback is a self-hosted runner on a home machine (residential
  IP) — a one-line `runs-on:` change. No proxies, stealth plugins, or
  CAPTCHA-solving services.
- **Term-date logic:** a plain Python module, `src/term_dates.py`,
  holding a commented `TERMS` data block (term name, inclusive start/end,
  excluded ranges, excluded single days) plus pure functions
  `is_in_term()`, `is_checkable_day()` and `checkable_dates()`. Chosen
  over YAML (extra dependency) and JSON (no comments — and the comments
  are what tell a human which range is a half term). Data is validated at
  import, and `python -m src.term_dates --list` prints every checkable
  date so a human can verify after editing. **To update for a new school
  year, edit only the `TERMS` block in that one file.**
- **Hosting/scheduling:** GitHub Actions, `schedule: "0 7 * * *"` (07:00
  UTC) plus `workflow_dispatch` for manual runs. GitHub cron is
  best-effort — it can be delayed or skipped — so **all weekday and
  term-time gating happens inside the Python job**, never in the cron
  expression. A delayed or missed run is therefore harmless. Note that
  GitHub disables scheduled workflows in repos with no activity for 60
  days; any commit or manual dispatch re-arms them.
- **Email service:** Resend free tier (3,000 emails/month) via a single
  `POST https://api.resend.com/emails` with a Bearer token. No SMTP,
  OAuth, or app-password rotation. The free `onboarding@resend.dev`
  sender may only deliver to the Resend account owner's own address,
  which is exactly this project's recipient, so no domain verification is
  needed.
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
- Alert threshold: GBP 10.00 (after railcard discount) for either train.
  The comparison is strictly `price < 10.00` — a fare of exactly £10.00
  does **not** trigger an alert.
- If the railcard discount cannot be positively confirmed in the
  response, no alert is sent and the run fails loudly. A wrong price in
  an alert is worse than a missed alert.

## Which dates get checked
The gate applies to **travel dates**, not the day the job happens to run.
Each daily run enumerates candidate travel dates from tomorrow through
today + `HORIZON_DAYS` (default 14), keeps those passing
`is_checkable_day()`, and checks each. Outside term time the candidate
list is empty and the run is a clean no-op — no browser launch, no email.

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
