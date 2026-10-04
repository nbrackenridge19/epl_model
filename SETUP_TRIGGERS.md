# EPL timed triggers: setup

What this sets up: a small Cloudflare program (the "Worker") that acts like an alarm clock for each match. At 50, 25 and 5 minutes before kickoff it tells GitHub to run the lineup capture and the odds capture. Nothing about your model changes except when the data gets pulled.

| Time before kickoff | What runs | Where it ends up |
|---|---|---|
| 50 min | Lineup capture (checks every 5 min until both lineups are in) | `predicted_lineups` |
| 50 min | Odds capture | `odds`, labeled `lineup_release` (bets use this) |
| 25 min | Lineup backup run (stops at once if lineups are already in) | |
| 5 min | Odds capture | `odds`, labeled `closing` (kept for the data model) |

All commands below are PowerShell, one line each. Do the parts in order.

## Part A. GitHub token (the Worker's permission to start GitHub runs)

1. On github.com click your profile picture, then Settings, then Developer settings, then Personal access tokens, then Fine-grained tokens, then Generate new token.
2. Name it `epl-trigger-worker`. Set the expiration to the longest option.
3. Under Repository access choose Only select repositories and pick `epl_model`.
4. Under Repository permissions find Contents and set it to Read and write. Leave everything else alone.
5. Click Generate token and copy it into a note. GitHub only shows it once.

When this token expires the alarms stop working, so put the expiry date in your calendar.

## Part B. Make up a password

The Worker and GitHub need a shared password. Run this to generate one, then copy the result into your note:

```
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 40 | ForEach-Object {[char]$_})
```

## Part C. Send the new files to GitHub

```
cd "$HOME\Documents\epl-model-clone\epl_model"
```

```
git status --short
```

You should see 5 modified files (`generate_dashboard.yml`, `odds_scraper.yml`, `generate_dashboard.py`, `poll_espn_lineups.py`, `scrape_espn_odds.py`) and new files: three `epl_*.yml` workflows, `epl_scheduler.py`, `SETUP_TRIGGERS.md`, `.gitignore` and the `worker` folder. Messages saying "LF will be replaced by CRLF" are harmless. If you see anything else, stop and tell me.

```
git add -A; git commit -m "Add timed lineup and odds triggers"; git push
```

## Part D. Put the Worker on Cloudflare

```
cd "$HOME\Documents\epl-model-clone\epl_model\worker"
```

Then run these one at a time:

```
npm install
```
Should end with "found 0 vulnerabilities".

```
npx wrangler login
```
A browser tab opens. Log in with the same Cloudflare account as NHL and click Allow.

```
npx wrangler deploy
```
It prints an address ending in `.workers.dev`. Copy it into your note.

```
npx wrangler secret put GITHUB_TOKEN
```
Paste the token from Part A and press Enter. Nothing shows on screen while you paste; that is normal.

```
npx wrangler secret put SCHEDULE_SECRET
```
Paste the password from Part B and press Enter.

## Part E. Give GitHub the Worker's address and password

On github.com open `epl_model`, then Settings, Secrets and variables, Actions, New repository secret. Add two:

- `WORKER_URL`: the address from `wrangler deploy`, with no slash at the end.
- `SCHEDULE_SECRET`: the same password as Part D.

Your existing `DATABASE_URL` and `SCRAPERAPI_PROXY_URL` secrets are reused.

## Part F. Check it works

1. On github.com open the Actions tab, pick **EPL scheduler (sync Worker timers)**, click Run workflow with `dry_run = true`. The log should list the matches it would send. Run it again with `dry_run = false`; matches inside the next 3 days should say `OK`. Kickoff times fill in about 5 days before a match, so Oct 10 fixtures may say "no kickoff time stored yet" until about Oct 5.
2. Pick **EPL odds capture (timed)**, click Run workflow with `match_id = 68cd4d8a-a07a-4546-a8c9-0adb53a03344` (ars vs lee), `line_type = lineup_release`, `dry_run = true`. The log should show the DraftKings odds it would save.
3. To see what the Worker has scheduled for a match (replace the two capitalized parts):

```
curl.exe -H "Authorization: Bearer YOUR_PASSWORD" https://YOUR_WORKER_ADDRESS/match/68cd4d8a-a07a-4546-a8c9-0adb53a03344/status
```

## What changed

- New: the `worker` folder, `scripts/epl_scheduler.py`, three `epl_*.yml` workflows.
- `poll_espn_lineups.py`: handles one match per run, checks every 5 min until both lineups are posted, and ends red if it never gets them.
- `scrape_espn_odds.py`: the daily sweep now stores only `opening` odds; timed runs store one `lineup_release` or `closing` snapshot.
- `generate_dashboard.py`: bets use the `lineup_release` odds. If a match has none, it falls back to the newest odds row.
- Removed: the hourly lineup poller and the 15-minute odds job. The 07:30 UTC daily odds sweep stays.

## Good to know

- The 50-minute odds snapshot is time-based. If ESPN posts a lineup after that, the bet uses odds from before the lineup was out.
- The scheduler uses ScraperAPI credits on match days.
