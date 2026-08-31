# Train Price Alert Tool

## What this project does
Scrapes Trainline daily for train prices on Tuesday, Thursday, Friday
for a specific school commute. Alerts via email when prices fall
below GBP 10 (with 16-25 railcard applied).

## Constraints
- Must handle Trainline's dynamic rendering (likely needs headless
  browser or their undocumented API)
- Runs once daily, probably via GitHub Actions cron
- Email via a free-tier service (e.g. Resend, SendGrid, or Gmail SMTP)
- Prices must reflect 16-25 railcard discount
- Target days: Tuesday, Thursday, Friday only
- Only check dates that fall within school term time (see Term dates
  below) — including staff INSET days, but excluding half terms,
  occasional days, and bank holidays

## Tech decisions (to be filled in by planner)
- Language: TBD
- Scraping approach: TBD
- Hosting/scheduling: TBD
- Email service: TBD

## Route details
- From: Oxford
- To: London Paddington
- Departure window: two specific outbound trains from Oxford —
  07:25 departure and 07:30 departure. Check the price of both.
- Return: one-way only, no return leg
- Railcard: 16-25
- Alert threshold: GBP 10.00 (after railcard discount) for either train

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
