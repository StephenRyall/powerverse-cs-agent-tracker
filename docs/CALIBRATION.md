# Measuring what this agent costs you — before you let it run daily

You do not need to wait a fortnight, and you should not go live on guesswork. A
**two-account calibration run** costs roughly a sixth of a full one and tells you what
the full thing will draw. Half an hour, start to finish.

---

## First, the thing that changes the answer

**A subscription token and your own work share one pool.** `CLAUDE_CODE_OAUTH_TOKEN`
draws from the same rolling 5-hour and weekly windows as claude.ai, Claude Desktop,
Cowork and interactive Claude Code. Anthropic states this plainly: *"Your usage of all
different Claude product surfaces counts towards the same usage limit."*

So if a 07:00 run consumes a large share of the 5-hour window, **you are locked out of
your own tools until that window resets** — not throttled, locked out. There is no way
to check remaining headroom before a run starts, and no distinct exit code to branch on.

That is why the recommendation in `PHASE3-MIGRATION.md` changed. If you want a hard
guarantee that this agent can never eat into your working capacity, **do not put it on
your subscription** — see "The ringfenced option" below.

Anthropic's plan limits are deliberately **not published** as token counts, so "half my
tokens" cannot be computed from any documented number. It can only be *measured*, which
is what step 2 below does.

---

## The measurement

### 1. Set up credentials and preflight

Per `PHASE3-MIGRATION.md` steps 1–5. You need these before anything can be measured —
there is no way to test the real token cost without real access to Gmail, Drive,
Calendar and Slack.

### 2. Read your usage baseline (subscription auth only)

In an interactive Claude Code session on your Mac:

```
/usage
```

Note the **5-hour** and **weekly** percentages. This is the only reliable read on
subscription headroom — `/usage` is computed from local session history, so run it on
the same machine you'll test from.

### 3. Run a two-account calibration

**Locally**, which is the fastest loop:

```bash
PV_ACCOUNT_LIMIT=2 python scripts/fetch_roster.py out

claude -p "$(cat agent/daily-agent-prompt.md)" \
  --mcp-config .mcp.json --strict-mcp-config \
  --permission-mode dontAsk \
  --allowedTools "Read,Write,Edit,Bash,Glob,Grep,Task,mcp__pv-workspace" \
  --model opus --effort medium \
  --max-budget-usd 3 \
  --output-format json > out/run.log
```

Or **in Actions**: Run workflow → set `sample_size` to `2`. A sample run never writes to
Drive, never posts to Slack and never commits metrics, so it cannot disturb anything.

### 4. Read your usage again, then project

```
/usage
```

The difference is what two accounts cost you in plan terms. Feed both numbers in:

```bash
python scripts/calibrate.py out/run.log \
  --plan-share 4 \            # the % of the 5-hour window the sample just used
  --budget-usd-month 40       # what you're willing to spend a month
```

You get the measured tokens split by model, the projected full-run cost, monthly and
annual figures, and a verdict:

- **≥50% of your 5-hour window** → it fails. Move it off your subscription.
- **30–50%** → it fits, but it's a large standing draw and will compete with you.
- **<30%** → comfortable.

It also suggests a `--max-budget-usd` value with 35% headroom over the projection. Set
that as repo variable `CS_AGENT_MAX_USD` so drift can't become a surprise.

Exit code is 1 if either verdict fails, so you can gate go-live on it.

---

## The ringfenced option — recommended

Use a **Console API key in its own workspace with a spend limit.** This is Anthropic's
own guidance for anything beyond a single person's light automation, and it is the only
arrangement that makes the guarantee you actually want:

1. <https://console.anthropic.com> → **Workspaces** → create one, e.g. `cs-agent`.
2. Set a **monthly spend limit** on that workspace — say $40. This is a hard cap
   enforced server-side.
3. Create an API key scoped to that workspace. Add it as `ANTHROPIC_API_KEY`.
4. Remove `CLAUDE_CODE_OAUTH_TOKEN` from the repo secrets so the workflow can't fall
   back to your subscription.

Now the agent's ceiling is a number you set, in pounds, in a pool your own work never
touches. It cannot lock you out of Claude, and "less than half my capacity" stops being
something you have to monitor.

The trade is that it costs real money rather than using subscription headroom you have
already paid for — on today's measurements, roughly $60–70 a month before the week 2–3
optimisations, and well under half that after them.

Anthropic also documents **workload identity federation** (OIDC to a service account) if
you'd rather not hold a long-lived key in GitHub at all.

---

## What to do with the answer

| Projection | What to do |
|---|---|
| Fits your budget and <30% of plan window | Go live. Read `metrics/runs.csv` weekly. |
| Fits, but 30–50% of plan window | Go live on an API key workspace, not the subscription. |
| Over budget | `CLAUDE_CODE_SUBAGENT_MODEL=haiku` first (biggest single lever), re-calibrate, then build items 1 and 5 from `TOKEN-OPTIMISATION.md`. |
| Wildly over | Cut scope before cutting quality — run the full 13 accounts weekly and only the Amber/Red accounts daily. |

That last row is worth a thought regardless. Nine of thirteen accounts are Amber or Red;
the Green ones (Cord, Evtec) are Green precisely because their renewals are already
signed. A daily pass over the at-risk accounts and a Monday pass over everything would
cut roughly a third off every run and lose very little.

---

## A caveat on the cost figures

`total_cost_usd` is a **client-side estimate** from a price table bundled with the CLI.
Anthropic's docs say explicitly not to use it for billing. It is reliable for trend and
for extrapolation, which is all this procedure needs. Your Console usage page is the
authority on actual spend.
