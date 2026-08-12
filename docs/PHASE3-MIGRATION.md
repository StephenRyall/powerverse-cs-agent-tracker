# Phase 3 — moving the CS Agent from Cowork to GitHub Actions

> **Status: live.** Shipped 2026-08-10 in `ab3a8ae`. The scheduled run has been green since —
> see `metrics/runs.csv`. This is kept as the record of how it was set up and as
> the runbook for rebuilding or rotating credentials — steps 2–8 are still the live procedure for
> that. For how the pipeline works day to day, see the README.

**What it gained over the Cowork schedule** (which worked fine):

- `--effort medium` and Sonnet research subagents — neither was possible in Cowork, and together
  they were the biggest remaining cost lever.
- `metrics/runs.csv`, committed every run: tokens, cost, and how many accounts actually changed.
  Cowork told you nothing.
- A validator that rejects the output before it can touch the sheet.
- The prompt in version control instead of buried in a trigger config.
- A failure notice in Slack instead of silence.

**What it did not gain:** billing separation. Without a Console API key this still runs on
Stephen's subscription, same pool as his own work. That is why the schedule is 04:00 BST and why
there is a budget cap.

---

## Step 1 — Push the code ✅ done

Done in `ab3a8ae`. The code arrived as a zip dropped over the clone, and `.gitignore.additions`
was appended and deleted in the same commit — which is why the `.gitignore` comments sit slightly
adrift from the entries they describe. Nothing to repeat here; the repo is now the source.

Editing is no longer a zip shuffle either: work in the clone with Claude Code and use `/ship`.

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

## Step 5 — Collect the values locally FIRST (2 min)

Do this before touching GitHub. **GitHub secrets are write-only**: once saved you can replace one
but you can never read it back, and the Google refresh token is printed to your terminal exactly
once and stored nowhere. Enter them into GitHub first and you will find yourself re-minting
tokens to fill in `.env` later.

```bash
cd ~/Desktop/agents/cs-agent-tracker      # your clone
cp .env.example .env
```

Open `.env` and paste in the seven values from steps 2–4. That file is now your source of truth;
you will copy from it into GitHub in the next step.

`.env` is gitignored, but it holds a token with read access to your mailbox — confirm
`git status` doesn't list it before you commit anything.

## Step 6 — Paste them into GitHub (5 min)

These go in **GitHub**, on the `powerverse-cs-agent-tracker` repo — not Google, not Slack, not
Anthropic. GitHub encrypts them and exposes them only to the workflow at run time; they are never
visible in logs or to anyone browsing the repo.

Go to:

```
https://github.com/StephenRyall/powerverse-cs-agent-tracker/settings/secrets/actions
```

or click through: **your repo → Settings** (the tab along the top of the repo, not your account
settings) **→ Secrets and variables → Actions → New repository secret**. Add each of these,
pasting the value from the step shown:

| Secret | Copy from your `.env` |
|---|---|
| `GOOGLE_CLIENT_ID` | `GOOGLE_CLIENT_ID` |
| `GOOGLE_CLIENT_SECRET` | `GOOGLE_CLIENT_SECRET` |
| `GOOGLE_REFRESH_TOKEN` | `GOOGLE_REFRESH_TOKEN` |
| `SLACK_USER_TOKEN` | `SLACK_USER_TOKEN` (`xoxp-`) |
| `SLACK_BOT_TOKEN` | `SLACK_BOT_TOKEN` (`xoxb-`) |
| `SLACK_CHANNEL_ID` | `C0BMQ4PGDA7` |
| `CLAUDE_CODE_OAUTH_TOKEN` | `CLAUDE_CODE_OAUTH_TOKEN` |

Note the two tabs on that page. **Secrets** are encrypted and write-only — you can never read one
back, only replace it. **Variables** are plain text and visible. Everything in the table above is
a Secret.

Optional, on the **Variables** tab of the same page: `CS_AGENT_MAX_USD` to change the per-run
budget cap (default 6). It is a variable rather than a secret because there is nothing sensitive
about it and it is handy to see at a glance.

## Step 7 — Preflight (5 min)

Run this **from inside the repo folder**, not your home directory, and use `python3` — macOS has
no `python` or `pip` on the PATH, only the `3`-suffixed versions. It reads the `.env` you created
in step 5.

```bash
cd ~/Desktop/agents/cs-agent-tracker
python3 scripts/preflight.py --slack
```

Eight checks, one line each, every one mapping back to a step above. Fix anything red before
going further.

No dependencies are needed for preflight — everything it uses is in the Python standard library.
`pypdf` (from `mcp/requirements.txt`) is only required if you later run the **agent** locally, so
it can read SOW PDFs; GitHub Actions installs it automatically. If you do want it:

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r mcp/requirements.txt
```

A virtual environment matters on macOS — recent versions refuse a plain `pip install` into the
system Python with an "externally managed environment" error.

**If `python3` itself is missing**, install Apple's command line tools: `xcode-select --install`.

## Step 8 — Calibrate on two accounts (10 min)

Don't go live on an estimate. In Claude Code, run `/usage` and note the **5-hour** and **weekly**
percentages. Then:

**Actions → CS Agent Tracker — synthesis → Run workflow**, set `sample_size` to `2`.

It researches two accounts, writes to nothing, and projects the full run. Run `/usage` again; the
difference is what two accounts cost you. Feed it back:

```bash
python3 scripts/calibrate.py out/run.log --plan-share <the % you just measured> --budget-usd-month 40
```

Verdict bands: **≥50%** of your 5-hour window fails, **30–50%** warns, **<30%** is comfortable.

## Step 9 — Full dry run, then live ✅ done

1. **Run workflow** with `dry_run` ticked — full agent, no Drive write, no Slack post. Download
   the `cs-agent-run-*` artifact and read `out/cs-agent-synthesis.json` and `out/brief.md`.
2. Happy? Run it again with `dry_run` off.
3. **Disable the Cowork task** (`trig_01MpDQde8WqUwYYL2U5Mk7Mp`) — ask Claude, or turn it off in
   Cowork. Do not skip this: two agents writing `cs-agent-synthesis.json` means the Apps Script
   takes whichever landed last, and you get two briefs every morning.

Steps 1 and 2 were done on 2026-08-10 (runs 3 and 4 in `metrics/runs.csv`). **If you have been
getting two briefs a morning, step 3 was missed** — that is the only thing that causes it.

---

## How a run works

Moved to the README, which is now the canonical description of the pipeline, the cadence and the
validation gate. Keeping a second copy here only guarantees the two drift apart.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Google credentials missing from the environment` | a value is missing from `.env` (local) or from repo secrets (Actions) — steps 5 and 6 |
| `invalid_grant` on token refresh | consent screen was External + Testing; switch to Internal, re-mint |
| `not_allowed_token_type` from Slack search | search needs the `xoxp-` user token, not the bot token |
| `not_in_channel` when posting | `/invite @CS Agent` in #cs-agent-alerts |
| `full-refresh day, but missing from synthesis` | Monday run skipped accounts — the tiering rule leaked into Monday; check STEP 0 of the prompt |
| `not in the sheet's Accounts tab` | agent invented or mistyped a name; if it repeats, check for odd whitespace in the sheet |
| `Budget limit reached` | check `metrics/runs.csv` first — a jump usually means a search loop, not growth. Raise via variable `CS_AGENT_MAX_USD` |
| Metrics step can't push | branch protection; non-fatal, the CSV is still in the artifact |
| `command not found: python` / `pip` | macOS only has `python3` / `pip3`. Use `python3 -m pip`, never bare `pip` |
| `No such file or directory: mcp/requirements.txt` | you are in `~`, not the repo — `cd` into the clone first |
| `externally-managed-environment` on pip install | use a venv: `python3 -m venv .venv && source .venv/bin/activate` |
| Lost a GitHub secret's value | You cannot read one back — they are write-only. Re-mint it (`get_google_refresh_token.py` forces a fresh token) and update **both** `.env` and the GitHub secret so only one is in circulation |
| Nothing fires | Actions disables schedules after 60 days of repo inactivity — push a commit or run manually monthly |
| Claude token expired | `claude setup-token` again, update the secret |

## After a fortnight — still pending

Read `metrics/runs.csv`. The `pct_unchanged` column is the business case for the remaining
optimisations in `docs/TOKEN-OPTIMISATION.md` — deterministic calendar fields, SOW caching, and
the full delta gate. Don't build them until that column says they're worth it.

Two runs in as of 2026-08-12, which is not yet a sample:

| Run | Cost | `pct_unchanged` |
|---|---|---|
| 3 (2026-08-10, manual) | $6.12 | 85 |
| 4 (2026-08-11, scheduled) | $3.97 | 62 |

Two things to watch rather than act on yet. Run 3 came in at $6.12 against a $6 cap, so the cap
bounds the agent's own spend and not the total. And an early `pct_unchanged` of 62–85 is the
signal those optimisations are aimed at — if it holds over a fortnight, most of each run is
re-deriving things that did not change.
