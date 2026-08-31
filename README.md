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
below). The table (including the last price recorded for each date,
once the first successful run has happened) is visible to anyone with
the link straight away — no setup needed just to look. A row turns
**green** if either train's last-recorded price was under £10, and
**blue** once the date is marked as booked (blue wins if a row is
somehow both). Ticking a checkbox needs the one-time token setup
described below; it saves immediately, including the highlight. This is
the page meant to be shared with anyone who doesn't use GitHub day to
day.

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

**Making the repo public doesn't mean anyone can edit it.** Repo
visibility only ever controls who can *read* it. Whether a save
actually succeeds is separately gated by whether the token's owner is
an invited **collaborator** with write access (Settings →
Collaborators) — a stranger's token gets rejected by GitHub regardless
of the repo being public. So the collaborator list *is* the access
control for this site; add someone there (and have them generate their
own token per the steps above) to let them edit, and remove them there
to revoke it.

If the token ever expires or is revoked, the site will show a clear
"token was rejected" message — just generate a new one and paste it in
again.

## Required repository secrets

Set these under Settings → Secrets and variables → Actions → New
repository secret:

| Secret | Required | Notes |
| --- | --- | --- |
| `RESEND_API_KEY` | Yes | From your [Resend](https://resend.com) account (free tier: 3,000 emails/month). Settings → API Keys → Create API Key. |
| `ALERT_EMAIL_TO` | Yes | The address alerts (and test emails) are sent to. For more than one recipient, separate addresses with commas, e.g. `a@example.com, b@example.com`. |
| `ALERT_EMAIL_FROM` | No | Defaults to `Train Alerts <onboarding@resend.dev>`. The free Resend sender can only deliver to the Resend **account owner's own address** — if `ALERT_EMAIL_TO` names any other address (including a second recipient), verify a domain in Resend first and set this to an address on it. |

No secret is ever logged or can appear in an email/exception body — see
`src/notifier.py`.

### Sending to more than one address

The code supports multiple recipients out of the box — just put more
than one address in `ALERT_EMAIL_TO`, comma-separated. The blocker is
Resend's own free-tier restriction, not this tool: **the sandbox sender
`onboarding@resend.dev` can only ever deliver to the single address that
owns the Resend account**, so a second, different address will silently
fail to receive anything even though the run reports success. To
actually deliver to two (or more) addresses:

1. In Resend, go to **Domains** → **Add Domain**, and add a domain you
   own (a free domain-provider subdomain works too, as long as you can
   edit its DNS).
2. Add the DNS records Resend shows you (SPF/DKIM, usually 2-3 TXT/CNAME
   records) at your domain registrar, then click **Verify** in Resend —
   this can take a few minutes to propagate.
3. Once verified, set `ALERT_EMAIL_FROM` to an address on that domain,
   e.g. `Train Alerts <alerts@yourdomain.com>` — this is what lifts the
   "owner's own address only" restriction.
4. `ALERT_EMAIL_TO` can then list any addresses, e.g.
   `you@example.com, partner@example.com`.

This is still on Resend's free tier (verifying a domain costs nothing) —
it's a one-time setup, not an upgrade.

## Running a test

There's exactly one way to test this, deliberately — no checkboxes to
pick a combination from. From the **Actions** tab → **Train price
check** → **Run workflow**, then **Run workflow** again to confirm.

A manual run always does the complete real thing: it scrapes a real
date, writes the result to `price-history.csv`, and sends a genuine
email through Resend — using real, current fare data throughout. If
that date's fare happens to be below £10, it's a normal alert; if not
(the common case), it sends the cheapest real fare it found instead, so
you still get a real email confirming the whole pipeline — scraping,
the CSV log, and Resend delivery — works end to end. It also never
waits for 8pm London; a manual run always executes immediately.

By default a manual run only checks **one** date (`max_dates: 1`
in the "Run workflow" dialog), so it normally finishes in well under a
minute. Type a bigger number to test against more dates at once, or
type `all` to check every remaining date this school year. **Don't just
clear the field to blank** — GitHub's own "Run workflow" web UI silently
re-applies the default value (`1`) whenever this field is submitted
empty, so a genuinely blank field never actually reaches the workflow;
`all` is the only reliable way to ask for every date from this UI.

The scheduled run at 8pm every day behaves differently in exactly the
two ways described above: it waits for the real 8pm London time slot,
and it only ever emails when a fare has genuinely dropped below £10 —
see the next section.

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
RESEND_API_KEY=... ALERT_EMAIL_TO=... MAX_DATES=1 SKIP_TIME_GATE=1 TEST_RUN=1 python -m src.main
```

`SKIP_TIME_GATE=1` runs immediately instead of only at 8pm London time.
`TEST_RUN=1` sends a genuine email even if nothing found is below
threshold (using the cheapest real fare it did find) — see "Running a
test" above for what this does and why; it's the same thing a manual
GitHub Actions run does automatically, just triggered from a local
shell instead. There is no simulated/dry-run mode — this always sends a
real email, so real secrets are required.

## How failures are surfaced

The job exits non-zero on failure, GitHub emails the repository owner
when a *scheduled* run fails, and debug artifacts (screenshot + page
HTML from the failing date) are attached to the failed run for 7 days.
