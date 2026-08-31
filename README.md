# Train Price Alert Tool

Checks National Rail Enquiries every evening for the price of the 07:25
and 07:30 Oxford → London Paddington trains (one-way, 16-25 Railcard)
and emails an alert whenever either fare drops below £10 — but only for
travel dates that are a Tuesday, Thursday or Friday inside school term
time. See `CLAUDE.md` and `docs/plans/001-train-price-alert.md` for the
full design and the discovery process behind it.

## Marking a date as already booked

Once you've booked a ticket for a date, tell the tool so it stops
checking it — no coding involved, no need to touch this repo directly.

**Easiest way — the booked-dates website:** open
`https://rosjo99.github.io/Train-prices/` (once GitHub Pages is enabled
— see [Setting up the booked-dates website](#setting-up-the-booked-dates-website)
below) and tick the checkbox next to any date you've booked. It saves
immediately. This is the page meant to be shared with anyone who
doesn't use GitHub day to day.

**Fallback — editing the file directly:** open `booked-dates.txt` on
github.com, click the pencil (edit) icon, add a line `YYYY-MM-DD`, and
commit directly from the browser. Lines starting with `#` are comments
and are ignored.

The next run (scheduled or manual) reads whichever of these was used
most recently, since both ultimately edit the same file.

## Setting up the booked-dates website

The site lives in `site/` and is a plain static page — no server, no
database. It talks directly to GitHub's API from the visitor's own
browser to read and update `booked-dates.txt`. Two one-time steps:

**1. Turn on GitHub Pages** (you do this, once): Settings → Pages →
"Build and deployment" → Source → **GitHub Actions**. The
`Deploy booked-dates site` workflow (`.github/workflows/deploy-pages.yml`)
then publishes the site automatically on every push to `main` that
touches `site/`, and can also be run manually from the Actions tab. The
published URL appears on that same Settings → Pages screen (something
like `https://rosjo99.github.io/Train-prices/`).

**2. Create a personal access token** (whoever will use the checkbox
site does this, once — a normal GitHub account, free, is needed first):

1. Go to <https://github.com/settings/personal-access-tokens/new> (this
   works even without being a collaborator on the repo — anyone can
   generate one, though it will only actually be able to write to
   repos they have access to. If your girlfriend doesn't have access to
   this repo, add her as a collaborator first: Settings → Collaborators
   → Add people.)
2. **Token name:** anything memorable, e.g. `booked-dates-site`.
3. **Expiration:** pick something like 1 year (GitHub will prompt to
   renew it before it expires).
4. **Repository access:** "Only select repositories" → choose
   `rosjo99/Train-prices`.
5. **Permissions** → **Repository permissions** → find **Contents** →
   set it to **Read and write**. Leave every other permission as "No
   access".
6. Click **Generate token**, then **copy it immediately** (GitHub only
   shows it once).
7. Open the booked-dates website, paste the token into the box under
   "One-time setup", click **Save token**.

That token is stored only in that browser's local storage — it's never
sent anywhere except directly to `api.github.com` when a checkbox is
ticked. It doesn't need to be shared with anyone else, and it only ever
grants write access to this one repository's contents, nothing else on
the account.

If the token ever expires or is revoked, the site will show a clear
"token was rejected" message — just generate a new one and paste it in
again.

## Required repository secrets

Set these under Settings → Secrets and variables → Actions → New
repository secret:

| Secret | Required | Notes |
| --- | --- | --- |
| `RESEND_API_KEY` | Yes | From your [Resend](https://resend.com) account (free tier: 3,000 emails/month). Settings → API Keys → Create API Key. |
| `ALERT_EMAIL_TO` | Yes | The address alerts (and test emails) are sent to. |
| `ALERT_EMAIL_FROM` | No | Defaults to `Train Alerts <onboarding@resend.dev>`. The free Resend sender can only deliver to the Resend **account owner's own address** — if `ALERT_EMAIL_TO` is someone else's address, verify a domain in Resend first and set this to an address on it. |

No secret is ever logged or can appear in an email/exception body — see
`src/notifier.py`.

## Running a test

From the **Actions** tab → **Train price check** → **Run workflow**, you
can:

- Tick **`send_test_email`** to skip scraping entirely and send one real
  (not simulated) test email through Resend — the fastest way to confirm
  `RESEND_API_KEY`/`ALERT_EMAIL_TO` are set up correctly, since a normal
  run might go a long time without a fare actually dropping below £10.
- Tick **`dry_run`** instead to run the real scrape-and-decide pipeline
  but print the email to the log rather than sending it.
- Set **`max_dates`** to a small number (e.g. `1`) to limit how many
  travel dates a manual run checks, for a quicker test.
- **`skip_time_gate`** defaults to on for manual runs, so they always
  execute immediately regardless of what time it is — see below for why
  that gate exists at all.

## How the daily schedule works

The workflow (`.github/workflows/price-check.yml`) is scheduled for
**8pm British time, every day**. GitHub Actions cron is UTC-only and has
no idea about British Summer Time, so the workflow actually schedules
*two* cron lines — one for 8pm BST, one for 8pm GMT — and the Python
code itself (`src/main.py`'s `RUN_HOUR_LONDON` check) looks at the real
Europe/London clock and silently does nothing on whichever of the two
firings *isn't* actually 8pm there today. So two triggers fire daily,
but only one of them ever does real work, correctly across the March/
October clock changes with no manual maintenance.

Separately, all day-of-week/term-time gating (which travel *dates* get
checked) is independent of this and already described in `CLAUDE.md`.

**Operational notes:**
- GitHub Actions cron is best-effort and can be delayed or occasionally
  skipped — harmless, since all real gating is in Python, not the cron
  expression.
- **GitHub disables scheduled workflows in repositories with no commit
  activity for 60 days.** Any push (including the price-history.csv
  commit each run makes) or a manual dispatch re-arms it.
- If a run starts failing with `BlockedError` or `HijackedError`, that's
  unexpected — National Rail Enquiries has no bot protection as of this
  writing (see `CLAUDE.md`'s Tech decisions), so this means either its
  site changed or a third-party ad script is misbehaving; check the
  uploaded debug artifacts on the failed run first.

## Price history log

Every date successfully checked is appended — never overwritten — to
`price-history.csv` at the repo root, one row per (travel date, target
departure) per run: timestamp, travel date, target/actual departure
time, arrival time, price, whether the railcard discount was confirmed,
whether it was sold out, and the fare name. The workflow commits this
file back to the repo after every run that checked at least one date
successfully, so it's a permanent, growing record you can open directly
on github.com or download and load into a spreadsheet.

## Updating term dates each school year

Edit only the `TERMS` block in `src/term_dates.py` (and keep
`CLAUDE.md`'s "Term dates" section in sync — see that file's header
comment for exactly what to change), then run:

```
python -m src.term_dates --list
```

to print every checkable date and eyeball that nothing looks wrong.
This also re-validates the data at import time (a term's start after
its end, terms overlapping, etc. all raise loudly). The booked-dates
website picks up the new dates automatically the next time it's
deployed (any push to `main` touching `src/term_dates.py` redeploys it
— see `.github/workflows/deploy-pages.yml`).

## Running locally

```
pip install -r requirements.txt
playwright install chromium
DRY_RUN=1 SKIP_TIME_GATE=1 python -m src.main
```

`DRY_RUN=1` prints the email instead of sending it (no secrets needed).
`SKIP_TIME_GATE=1` runs immediately instead of only at 8pm London time.

## How failures are surfaced

The job exits non-zero on failure, GitHub emails the repository owner
when a *scheduled* run fails, and debug artifacts (screenshot + page
HTML from the failing date) are attached to the failed run for 7 days.
