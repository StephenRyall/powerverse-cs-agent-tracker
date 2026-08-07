# CS Agent Tracker

Proactive customer-success agent for Powerverse. One Google Sheet tracks every account; two automations keep it live and warn Stephen in Slack (#cs-agent-alerts) before things slip.

## Architecture

```
                     07:00 UK weekdays                      08:00-09:00 UK daily
┌──────────────────────────────────────┐        ┌────────────────────────────────────┐
│ Cowork scheduled agent (Claude)      │        │ Apps Script (bound to the sheet)   │
│  reads: Sheet, Gmail, Drive          │        │  syncConnectedAssets(): CPs → sheet│
│  (transcripts!), Slack (low weight), │  JSON  │  ingestSynthesis(): JSON → sheet   │
│  SOWs, Google Calendar               │ ─────► │  checkRenewals(): date-math alerts │
│  writes: cs-agent-synthesis.json     │ Drive  │  → Slack incoming webhook          │
│  posts: morning brief → Slack        │        └────────────────▲───────────────────┘
└──────────────────────────────────────┘                         │ live charge points
                                            ┌────────────────────┴───────────────────┐
                                            │ CS Delivery & KPI Dashboard            │
                                            │ "Real-time Charge Points" tab          │
                                            │ (populated by the Grafana agent)       │
                                            └────────────────────────────────────────┘
```

- **Renewal Risk (Agent)** — Green/Amber/Red synthesised from the customer's SOW (linked per row in column H) + Gmail signals, cross-referenced with care. Written daily.
- **Context — Current State** — ≤5 bullets per account summarising the partnership, from Gmail + Drive transcripts (primary) and Slack (lower weight). Written daily.
- **Connected Assets** — live charge-point count per account, read each morning from the Real-time Charge Points tab of the KPI Dashboard. Value-only: it overwrites the cell and raises no alerts.
- Deterministic alerts (≤90-day window, overdue/TBC renewal dates, Red accounts) come from Apps Script so they fire even if the AI agent misses a run.

### Account name matching

The KPI Dashboard names accounts slightly differently from the tracker. Rather than a hand-maintained alias table, the script matches on normalised names first (`Cord (RAC)` → `Cord RAC`), then falls back to token overlap, which resolves `Evtec (EON)` → `EON`, `JPL Stevie` → `JPL` and `Evo` → `EVO EV`. Genuine ambiguity is left unmatched and logged rather than guessed. All 15 tracker accounts currently resolve; `D2C` and `Ascent` exist in the source but not the tracker, so they are ignored.

## Repo layout

| Path | What |
|---|---|
| `apps-script/Code.gs` | All Apps Script code (asset sync + ingest + alerts + trigger setup) |
| `agent/daily-agent-prompt.md` | The prompt the Cowork scheduled task runs each weekday 07:00 UK |
| `sheet/CS Agent Tracker.xlsx` | Source workbook (upload/convert to Google Sheets) |

## Setup

1. **Sheet**: In Drive, open `CS Agent Tracker.xlsx` → File → Save as Google Sheets. Keep the name "CS Agent Tracker". Paste each customer's SOW Drive link into column H.
2. **Slack webhook**: api.slack.com/apps → Create app → Incoming Webhooks → add to `#cs-agent-alerts` → copy URL.
3. **Apps Script**: In the Google Sheet, Extensions → Apps Script → paste `Code.gs`. Project Settings → Script Properties → add `SLACK_WEBHOOK_URL`. Run `setupTriggers()` once and authorise. The first run also asks for access to the KPI Dashboard, since `syncConnectedAssets()` opens it by ID — accept with the same Google account that can already open that file.
4. **Cowork scheduled task**: already created ("CS Agent Tracker — daily synthesis", weekdays 07:00 UK). The prompt lives in `agent/daily-agent-prompt.md`; edit the task in Cowork if the prompt changes.

## Phase 3 (planned)
Migrate the scheduled agent to a Claude Code routine on an always-on VM so nothing depends on a laptop being open. The Apps Script side is already serverless (Google-hosted) and carries over unchanged.
