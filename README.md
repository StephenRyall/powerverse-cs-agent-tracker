# CS Agent Tracker

Proactive customer-success agent for Powerverse. One Google Sheet tracks every account; two
automations keep it live and warn Stephen in Slack (#cs-agent-alerts) before things slip.

The sheet is the source of truth. The agent never writes to it — it writes files, and scripts
validate and deliver them.

## Architecture

```
  Mon/Tue/Fri 03:00 UTC                                    08:00-09:00 UK daily
┌───────────────────────────────────────────┐      ┌────────────────────────────────────┐
│ GitHub Actions — cs-agent-daily.yml       │      │ Apps Script (bound to the sheet)   │
│                                           │      │  syncConnectedAssets(): CPs → sheet│
│  fetch_roster.py    → out/roster.txt      │      │  ingestSynthesis():  JSON → sheet  │
│         ↓                                 │      │  checkRenewals(): date-math alerts │
│  claude -p (opus, effort medium)          │      │        → Slack incoming webhook    │
│    via mcp/pv_workspace_mcp.py, READ-ONLY │      └───────────────▲────────────────────┘
│    Gmail · Drive · Calendar · Slack       │                      │
│         ↓                                 │           reads      │ live charge points
│    out/cs-agent-synthesis.json            │      ┌───────────────┴────────────────────┐
│    out/brief.md                           │      │ CS Delivery & KPI Dashboard        │
│    out/signal.json                        │      │ "Real-time Charge Points" tab      │
│         ↓                                 │      │ (populated by the Grafana agent)   │
│  validate_synthesis.py  ── fails → no ────┼──┐   └────────────────────────────────────┘
│         ↓                     delivery    │  │
│  upload_synthesis.py → Drive ─────────────┼──┴──→ picked up by ingestSynthesis()
│  post_to_slack.py    → #cs-agent-alerts   │
│  log_run_cost.py     → metrics/runs.csv   │
└───────────────────────────────────────────┘
```

### Cadence

`Mon/Tue/Fri` at 03:00 UTC. Monday is the weekly full refresh; Tuesday and Friday are where every
recurring customer meeting falls (Cord / Evtec / EnSmart on Tuesdays, Sevadis on Fridays).
Wednesday and Thursday have none, so they are not worth a run. One UTC cron is correct all year —
the target is "before the working day", not an exact local time, so there is no DST gate to
maintain.

The brief always covers today **and** every uncovered day until the next run.

### What the agent decides, and what it doesn't

- **Renewal Risk (Agent)** — Green/Amber/Red synthesised from the customer's SOW (linked per row
  in column H) plus Gmail signals. Written every run.
- **Context — Current State** — ≤5 bullets per account, from Gmail and Drive transcripts
  (primary) and Slack (lower weight). Written every run.
- **Connected Assets** — live charge-point count, read each morning by Apps Script from the KPI
  Dashboard. Value-only: it overwrites the cell and raises no alerts. The model never sees it.
- **Deterministic alerts** (≤90-day renewal window, overdue/TBC dates, Red accounts) come from
  Apps Script, so they fire even if the agent misses a run entirely.

Every agent field **overwrites**. Nothing accumulates, no history, no new columns.

### The validation gate

`scripts/validate_synthesis.py` runs before anything is delivered. Customer names must match
`out/roster.txt` byte for byte — the roster is fetched live from the sheet at the start of the
run, so a hallucinated or renamed account fails the run rather than landing in the sheet.

- **Monday** — `--require-full`: every roster account must be covered.
- **Tue/Fri** — `--allow-subset`: dormant accounts are deliberately skipped.

A failed gate means no Drive upload and no Slack post. The sheet keeps yesterday's values.

### When a run fails

A silent failure is worse than a bad brief. Any failing step posts a `:rotating_light:` notice to
the same channel that would otherwise have had a brief in it, linking the workflow run. Working
files are kept as a build artifact for 14 days.

### Account name matching

The KPI Dashboard names accounts slightly differently from the tracker. Rather than a
hand-maintained alias table, the script matches on normalised names first (`Cord (RAC)` →
`Cord RAC`), then falls back to token overlap, which resolves `Evtec (EON)` → `EON`,
`JPL Stevie` → `JPL` and `Evo` → `EVO EV`. Genuine ambiguity is left unmatched and logged rather
than guessed. All 15 tracker accounts currently resolve; `D2C` and `Ascent` exist in the source
but not the tracker, so they are ignored.

## Repo layout

| Path | What |
|---|---|
| `.github/workflows/cs-agent-daily.yml` | The scheduled run: roster → agent → validate → deliver |
| `agent/daily-agent-prompt.md` | The prompt the headless run executes |
| `mcp/pv_workspace_mcp.py` | Read-only Workspace MCP server (Gmail, Drive, Calendar, Slack) |
| `scripts/fetch_roster.py` | Pulls the live customer roster from the Accounts tab |
| `scripts/validate_synthesis.py` | The gate. Roster match + shape checks, offline |
| `scripts/upload_synthesis.py` | Uploads validated JSON to Drive — the only write in the repo |
| `scripts/post_to_slack.py` | Posts the brief to #cs-agent-alerts |
| `scripts/log_run_cost.py` | Appends cost and change-rate to `metrics/runs.csv` |
| `scripts/calibrate.py` | Projects a sample run to a full one, against a budget |
| `scripts/preflight.py` | Checks every credential before go-live |
| `apps-script/Code.gs` | Asset sync + synthesis ingest + deterministic alerts + triggers |
| `sheet/CS Agent Tracker.xlsx` | Source workbook (upload/convert to Google Sheets) |
| `docs/` | Phase 3 migration, calibration, token optimisation |

## The agent's three outputs

| File | What it is |
|---|---|
| `out/cs-agent-synthesis.json` | One record per in-scope customer, ingested by Apps Script |
| `out/brief.md` | The morning brief, posted as Slack mrkdwn |
| `out/signal.json` | Per customer, whether anything actually changed |

`signal.json` measures how much of each run was avoidable. It feeds `metrics/runs.csv` and is the
basis for tuning the cadence — be strict with it or it tells you nothing.

## Cost control

The run shares Stephen's Claude allowance (Powerverse IT will not issue a Console API key), which
is why it is scheduled at 03:00 UTC — the 5-hour usage window has reset before the working day.

- `--max-budget-usd` caps each run; override with the `CS_AGENT_MAX_USD` repo variable (default 6).
- Research subagents run Sonnet (`CLAUDE_CODE_SUBAGENT_MODEL`); judgement stays on Opus.
- `PV_MAX_TOOL_CHARS` / `PV_MAX_BODY_CHARS` bound each tool result. ~130 tool calls a run means a
  generous per-call ceiling is how the context blows up. Don't widen them without checking
  `metrics/runs.csv` afterwards.

Every run appends to `metrics/runs.csv`, committed by `cs-agent[bot]`. Measure before optimising —
see `docs/TOKEN-OPTIMISATION.md`.

## Setup

1. **Sheet** — In Drive, open `CS Agent Tracker.xlsx` → File → Save as Google Sheets. Keep the
   name "CS Agent Tracker". Paste each customer's SOW Drive link into column H.
2. **Slack** — Create an app at api.slack.com/apps. You need an incoming webhook for
   `#cs-agent-alerts` (Apps Script alerts), a bot token for posting the brief, and a user token
   for `search.messages`.
3. **Apps Script** — In the sheet, Extensions → Apps Script → paste `Code.gs`. Project Settings →
   Script Properties → add `SLACK_WEBHOOK_URL`. Run `setupTriggers()` once and authorise. The
   first run also asks for access to the KPI Dashboard, since `syncConnectedAssets()` opens it by
   ID — accept with the same Google account that can already open that file.
4. **GitHub secrets** — Settings → Secrets and variables → Actions:

   | Secret | For |
   |---|---|
   | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Workspace reads + the one Drive write |
   | `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` | Posting the brief |
   | `SLACK_USER_TOKEN` | `search.messages` |
   | `CLAUDE_CODE_OAUTH_TOKEN` | The agent itself |

   Mint the Google refresh token with `scripts/get_google_refresh_token.py` (gitignored, run
   locally). Full walkthrough in `docs/PHASE3-MIGRATION.md`.
5. **Verify** — `python scripts/preflight.py` checks every credential before go-live, so the
   first scheduled run isn't the test.
6. **Calibrate** — Run the workflow manually with `sample_size: 2` and a `budget_usd_month` to
   project the cost of a full run before committing to the schedule. See `docs/CALIBRATION.md`.

## Running it by hand

Actions → "CS Agent Tracker — synthesis" → Run workflow:

- `dry_run` — runs the agent, skips the Drive upload and the Slack post.
- `sample_size: N` — research only the first N accounts. Implies a dry run.

## Working on this repo

`.claude/settings.json` pre-approves the everyday git and `gh` calls and denies force-push and
hard-reset outright — this repo runs a production job on a schedule. `/ship` commits the current
work to a branch and opens a PR; it never pushes to `main`.

Note that the scheduled run commits `metrics/runs.csv` to `main`, so local work can fall behind
between sessions. `pull.rebase` is set locally to keep that tidy.

## Testing without credentials

```bash
python mcp/pv_workspace_mcp.py --selftest            # protocol + tool schemas, offline
python scripts/validate_synthesis.py <json> <roster> # gate logic, offline
python scripts/preflight.py                          # needs real credentials
```
