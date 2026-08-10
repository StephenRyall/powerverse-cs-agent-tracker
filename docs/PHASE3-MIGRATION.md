# Phase 3 — moving the CS Agent from Cowork to GitHub Actions

About 45 minutes, once. Four things to set up, then a test run.

**What you gain over the Cowork schedule** (which already works fine):

- `--effort medium` and Sonnet research subagents — **neither is possible in Cowork**, and
  together they are the biggest remaining cost lever.
- `metrics/runs.csv`, committed every run: tokens, cost, and how many accounts actually changed.
  Cowork tells you nothing.
- A validator that rejects the output before it can touch the sheet.
- The prompt in version control instead of buried in a trigger config.
- A failure notice in Slack instead of silence.

**What you do not gain:** billing separation. Without a Console API key this still runs on your
subscription, same pool as your own work. That is why the schedule is 04:00 and why there is a
budget cap.

---

## Step 1 — Push the code (5 min)

Unzip over your existing clone, then:

```bash
cd powerverse-cs-agent-tracker
cat .gitignore.additions >> .gitignore && rm .gitignore.additions
git add -A && git commit -m "Phase 3: headless run in GitHub Actions"
git push
```

## Step 2 — Google credentials (15 min)

One OAuth client so the agent can read Gmail, Drive and Calendar with no browser.

1. <https://console.cloud.google.com> → new project, e.g. `powerverse-cs-agent`.
2. **APIs & Services → Library** → enable **Gmail API**, **Drive API**, **Calendar API**.
3. **OAuth consent screen** → User type **Internal**. This matters: an External app in "Testing"
   expires refresh tokens after 7 days and the job would break weekly.
4. **Credentials → Create credentials → OAuth client ID → Desktop app.** Copy the ID and secret.
5. On your Mac, signed in as `stephen.ryall@powerverse.com`:

   ```bash
   GOOGLE_CLIENT_ID=<id>.apps.googleusercontent.com \
   GOOGLE_CLIENT_SECRET=GOCSPX-<secret> \
   python3 scripts/get_google_refresh_token.py
   ```

   Approve in the browser; the refresh token prints to the terminal. Scopes are
   `gmail.readonly`, `drive.readonly`, `drive.file`, `calendar.readonly` — read what it needs,
   write only files it creates itself. Treat the token like a password.

## Step 3 — Slack tokens (10 min)

Two scopes that can't share a token: `search.messages` is user-only, posting is cleanest from a bot.

1. <https://api.slack.com/apps> → **Create New App → From scratch** → name `CS Agent`, Powerverse workspace.
2. **OAuth & Permissions** → Bot Token Scopes: `chat:write`. User Token Scopes: `search:read`.
3. **Install to Workspace.** Copy both tokens (`xoxb-…` and `xoxp-…`).
4. In Slack: `/invite @CS Agent` in **#cs-agent-alerts**.

The user token searches as you, so the agent sees what the Cowork connector saw, DMs included.

## Step 4 — Claude token (2 min)

```bash
claude setup-token
```

Copy the result. It lasts a year — diarise a renewal, because the job stops when it expires.

## Step 5 — Repo secrets (5 min)

**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | From |
|---|---|
| `GOOGLE_CLIENT_ID` | step 2 |
| `GOOGLE_CLIENT_SECRET` | step 2 |
| `GOOGLE_REFRESH_TOKEN` | step 2 |
| `SLACK_USER_TOKEN` | step 3 (`xoxp-`) |
| `SLACK_BOT_TOKEN` | step 3 (`xoxb-`) |
| `SLACK_CHANNEL_ID` | `C0BMQ4PGDA7` |
| `CLAUDE_CODE_OAUTH_TOKEN` | step 4 |

Optional, under **Variables** not Secrets: `CS_AGENT_MAX_USD` to change the per-run budget cap
(default 6).

## Step 6 — Preflight (2 min)

```bash
pip install -r mcp/requirements.txt
python scripts/preflight.py --slack
```

Eight checks, one line each, every one mapping back to a step above. Fix anything red before
going further.

## Step 7 — Calibrate on two accounts (10 min)

Don't go live on an estimate. In Claude Code, run `/usage` and note the **5-hour** and **weekly**
percentages. Then:

**Actions → CS Agent Tracker — synthesis → Run workflow**, set `sample_size` to `2`.

It researches two accounts, writes to nothing, and projects the full run. Run `/usage` again; the
difference is what two accounts cost you. Feed it back:

```bash
python scripts/calibrate.py out/run.log --plan-share <the % you just measured> --budget-usd-month 40
```

Verdict bands: **≥50%** of your 5-hour window fails, **30–50%** warns, **<30%** is comfortable.

## Step 8 — Full dry run, then live

1. **Run workflow** with `dry_run` ticked — full agent, no Drive write, no Slack post. Download
   the `cs-agent-run-*` artifact and read `out/cs-agent-synthesis.json` and `out/brief.md`.
2. Happy? Run it again with `dry_run` off.
3. **Disable the Cowork task** (`trig_01MpDQde8WqUwYYL2U5Mk7Mp`) — ask Claude, or turn it off in
   Cowork. Do not skip this: two agents writing `cs-agent-synthesis.json` means the Apps Script
   takes whichever landed last, and you get two briefs every morning.

---

## How a run works

```
Mon/Tue/Fri 03:00 UTC
  fetch_roster.py     sheet → out/roster.txt + out/accounts.csv
  claude -p …         researches in-scope accounts via the pv-workspace MCP server,
                      writes cs-agent-synthesis.json, brief.md, signal.json
  validate_synthesis  name parity, risk values, date formats  ── fails ⇒ stop
  upload_synthesis    new cs-agent-synthesis.json in Drive
  post_to_slack       brief → #cs-agent-alerts
  log_run_cost        appends metrics/runs.csv, commits it
08:00–09:00
  Apps Script         ingests the newest JSON, overwrites the sheet in place
```

The validator is the gate. If it fails nothing is uploaded or posted except a failure notice, and
the sheet keeps the previous values — the right failure mode.

**Monday is the full refresh** and the validator requires every account. **Tuesday and Friday**
are tiered: dormant accounts are deliberately skipped, keep their previous values, and are named
in the brief's Coverage line.

## Schedule

`0 3 * * 1,2,5` UTC — 04:00 BST, 03:00 GMT. Both are before the working day, so the 5-hour usage
window resets before you start. Because the target is "before work" rather than an exact local
time, one UTC cron is right all year and there is no DST edit.

Mon/Tue/Fri because every recurring customer meeting is Tuesday (Cord, Evtec, EnSmart) or Friday
(Sevadis). Wednesday and Thursday have none.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Google credentials missing from the environment` | a secret is unset or misnamed — step 5 |
| `invalid_grant` on token refresh | consent screen was External + Testing; switch to Internal, re-mint |
| `not_allowed_token_type` from Slack search | search needs the `xoxp-` user token, not the bot token |
| `not_in_channel` when posting | `/invite @CS Agent` in #cs-agent-alerts |
| `full-refresh day, but missing from synthesis` | Monday run skipped accounts — the tiering rule leaked into Monday; check STEP 0 of the prompt |
| `not in the sheet's Accounts tab` | agent invented or mistyped a name; if it repeats, check for odd whitespace in the sheet |
| `Budget limit reached` | check `metrics/runs.csv` first — a jump usually means a search loop, not growth. Raise via variable `CS_AGENT_MAX_USD` |
| Metrics step can't push | branch protection; non-fatal, the CSV is still in the artifact |
| Nothing fires | Actions disables schedules after 60 days of repo inactivity — push a commit or run manually monthly |
| Claude token expired | `claude setup-token` again, update the secret |

## After a fortnight

Read `metrics/runs.csv`. The `pct_unchanged` column is the business case for the remaining
optimisations in `docs/TOKEN-OPTIMISATION.md` — deterministic calendar fields, SOW caching, and
the full delta gate. Don't build them until that column says they're worth it.
