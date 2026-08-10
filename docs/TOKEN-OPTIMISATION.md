# CS Agent Tracker — token optimisation analysis

**7 Aug 2026.** Measured against today's real run, not estimated. Headline: the run costs
~1.15M tokens and roughly **63% of that is avoidable without touching output quality** — the
cuts remove duplicated, deterministic and unchanged work, not analysis.

---

## 1. Where the tokens actually went

Measured from today's run telemetry.

| Stage | Tokens | Share |
|---|---:|---:|
| Research subagents (13 accounts, 5 batches, 127 tool calls) | 945,162 | 82% |
| Raw calendar payloads | 165,922 | 14% |
| Orchestration, sheet read, synthesis output | 38,078 | 3% |
| **Total** | **1,149,162** | |

Per account: **~72,700 tokens**. Per tool call: ~7,400. At Opus 5 rates
($5/$25 per M) that is **$9.19 a run, ~$2,390 a year** on 260 weekdays.

Two numbers stand out.

**The calendar was 98% waste.** The two `list_events` calls returned 613,910 characters of JSON —
~166k tokens. After filtering to the 13 roster names, the useful content was ~3,200 tokens. Today
I caught it and filtered with `jq` outside the context; the Phase 3 build would have fed a
formatted-but-still-large version straight in.

**Six of thirteen accounts had nothing new.** Go-E (66 days of silence), Injet (45), JPL Stevie
(no contact since 23 Jul), Teltonika (14), Vorsprung (no inbound in 60), Evo (one holiday
auto-reply). Each cost a full-price research pass to establish that nothing had happened —
about **46% of the research spend confirming the absence of news**.

---

## 2. What the research says

Seven searches across Anthropic's platform and Claude Code docs, plus independent sources.
The findings that change the design:

**Prompt caching cannot help you.** Maximum TTL is 1 hour. A daily job cache-misses its entire
prefix every single run. Any plan built on "it'll cache" is wrong. Caching still matters *within*
a run — so pick model and effort at launch and never switch mid-run, because both invalidate the
prefix. Note also that subagents build their own cache from zero and use the 5-minute TTL even on
subscription auth.

**Extended thinking is on by default at `high` effort**, billed as output tokens, and the docs
say the default budget can be "tens of thousands of tokens per request." Nobody set this. For
extraction-and-summarisation work it is straightforwardly overspend.

**`--max-budget-usd` exists and counts subagent spend.** That is the right safety net for this
job — `--max-turns` errors out mid-run and isn't denominated in the thing you care about.

**Anthropic's own measurement of the pattern we need**: moving data movement out of context and
into code took a Drive→Salesforce workflow from 150,000 tokens to 2,000 — 98.7%. That is a
vendor-reported single example, so treat it as an upper bound, but the mechanism is exactly what
the calendar finding above demonstrates independently.

**Cutting tokens naively costs quality.** Anthropic's multi-agent telemetry found token usage
alone explains ~80% of performance variance on their research benchmark. Chroma's independent
*Context Rot* study (18 models, Claude 4 included) found a single distractor measurably reduces
accuracy, and that the degradation is worst for Opus-class models on long inputs. So the goal is
removing *waste*, not shrinking the budget. Every cut below removes duplicated, stale or
mechanical content — none removes evidence the judgement depends on.

**Batch API is out.** 50% off both directions, but there is no batch support in Claude Code or
the Agent SDK — it's the raw Messages API with no agentic loop. Not usable here.

**Pricing changes in three weeks.** Sonnet 5 goes from $2/$10 to $3/$15 per million on
**1 September 2026**. Any model-choice maths done this week is wrong by month end.

---

## 3. The plan, in ROI order

Stacked, each applied on top of the last:

| | Change | Tokens after | Cumulative cut |
|---|---|---:|---:|
| — | baseline (measured today) | 1,149,162 | — |
| 1 | Calendar computed in Python, not by the model | 953,162 | 17% |
| 2 | Shared context bundle fetched once, not per subagent | 898,162 | 22% |
| 3 | Quoted email history stripped before it reaches context | 769,138 | 33% |
| 4 | SOW analysis cached, keyed on Drive file version | 674,090 | 41% |
| 5 | Delta gate — unchanged accounts carried forward | 419,654 | **63%** |

### 1. Compute the calendar fields in Python (½ day, saves ~196k tokens)

`next_meeting` and `last_meeting_date` are **not judgement calls** — they are a name match against
event titles and attendee domains, then earliest-future and latest-ended. Two of the eight fields
per account are pure mechanics being paid for at Opus rates.

Fetch ±90 days once, match the roster, write `out/meetings.json` with the two fields already
formatted. The agent then only needs the calendar for one thing: which transcript to look up for
`last_meeting_summary`.

This also **removes a whole bug class**. Today's run needed care to get "strictly ended, and never
the same event as next_meeting" right for Cord's 10:00–10:30 call. In Python that is a timestamp
comparison, correct every time. The validator already checks the invariant; this makes it
structurally impossible to violate.

### 2. Fetch shared context once (½ day, saves ~55k)

Five subagents independently searched Slack, and several separately pulled the same monthly
charge-point report and the same #cs-agent-alerts history. Anthropic names this exact
anti-pattern — duplicated work across parallel subagents caused by vague task boundaries.

Pre-fetch the shared artefacts once into `out/shared-context.md`: the monthly CPS report, the last
14 days of #cs-agent-alerts, the KPI dashboard row. Subagents read the file instead of
re-searching. Cheaper *and* it stops two subagents reporting the same fact with different numbers,
which happened today with Cord (RAC)'s asset count (56 vs 58).

### 3. Strip quoted email history in the MCP server (2 hours, saves ~129k)

`gmail_get_thread` currently returns every message's full body. In a ten-message thread the first
message is quoted nine times. Strip `>` -prefixed lines, `On … wrote:` blocks, signature
delimiters and disclaimer footers in `_extract_body()` — deterministic, and the model loses
nothing it hasn't already read higher up the thread.

Also drop `MAX_CHARS` from 120,000 to 25,000. At 3.7 chars/token the current ceiling lets a single
tool call inject ~32k tokens. Claude Code's own MCP default caps tool results at 25,000 *tokens*;
25k chars is a deliberately tighter bound for a job with 127 tool calls in it.

### 4. Cache the SOW analysis (1 day, saves ~95k)

`renewal_risk` is driven by the Statement of Work — term, renewal clause, notice period, volume
commitment. **SOWs do not change.** Yet all thirteen are re-downloaded, re-text-extracted and
re-reasoned-over every weekday.

Content-address it, the way build systems do:

```
cache_key = sha256(drive_md5Checksum + analysis_prompt_version + model_id)
```

Drive's `files.get` returns `md5Checksum` and `version` for free. Store the derived analysis in
the repo under `cache/sow/`. A contract analysed once is never re-analysed until the file itself
changes. Including the prompt version and model ID in the key is the part people leave out and
then can't explain stale results.

Worth noting this preserves today's most valuable finding rather than losing it — the three wrong
renewal dates (Vorsprung, Go-E, Injet) were found by reading the SOWs. Cached, that analysis
persists and gets re-asserted every run at zero cost.

### 5. The delta gate (2 days, saves ~254k — the biggest single win)

Before spending any model tokens, compute a per-account fingerprint deterministically:

| Source | Cursor |
|---|---|
| Gmail | `historyId` — `users.history.list?startHistoryId=…` returns only changes |
| Drive | changes feed — `changes.list(pageToken)`, persist `newStartPageToken` |
| Slack | latest `ts` per channel |
| Calendar | `syncToken` on `events.list` |

Fingerprint unchanged → **carry yesterday's record forward verbatim, spend nothing.** Changed →
research only the delta, passing yesterday's record as a compact prior rather than starting cold.

Copy dbt's incremental-model discipline, which is the mature version of this pattern: a
high-water-mark filter, an upsert on a unique key, logic valid on both cold and warm paths, and —
critically — **a `--full-refresh` escape hatch**. Run the full 13-account pass every Monday.
Incremental state always eventually corrupts, and a weekly rebuild is the cheapest insurance
there is.

Add a `changed / unchanged` flag next to the token count per account. That single field turns
this from a guess into a measurement.

---

## 4. Model and flag changes (an afternoon, no code)

These stack on top of the 63% token cut and are where most of the *money* is.

| Change | Why |
|---|---|
| `--effort medium` on research subagents | Thinking is on by default at `high`, billed as output. Extraction doesn't need it. |
| Research subagents on **Haiku 4.5**, synthesis + brief on **Opus 5** | Per-subagent `model:` override is supported. Research is extract-and-summarise; the judgement calls stay on Opus. |
| `--max-budget-usd 6` | Hard stop that counts subagent spend. A runaway run can't quietly cost £200. |
| `--output-format json` | Emits `total_cost_usd` and a per-model breakdown so every run logs its own cost. (Client-side estimate — fine for trend, not for billing.) |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | Explicit compaction window rather than the 200k default boundary. |

Combined effect:

| Scenario | $/run | $/year |
|---|---:|---:|
| Today: all Opus 5, no cuts | 9.19 | 2,390 |
| + the five token cuts, still all Opus 5 | 3.36 | 873 |
| + research on Sonnet 5 (post-1 Sep pricing) | 2.01 | 524 |
| + research on Haiku 4.5, synthesis on Opus 5 | **1.07** | **279** |

**88% cheaper, 63% fewer tokens.**

On subscription auth (`CLAUDE_CODE_OAUTH_TOKEN`) the same cuts translate into plan headroom
rather than invoice, which matters more — a 1.15M-token weekday job is a large standing draw on
plan limits.

---

## 5. What I would not do

**Don't drop the subagents.** They are the right shape here: 13 genuinely independent accounts,
and each one's verbose tool output stays in its own window rather than the parent's. Cognition
argue against multi-agent designs, but their objection is about work requiring shared decisions —
not parallel research over independent subjects.

**Don't shorten the agent prompt to save tokens.** It's ~1,900 tokens against a 1.15M run. The
GOLDEN RULES and the field specs are what keep the output ingestible. This is not where the money
is, and cutting here trades a rounding error for correctness.

**Don't trim `CLAUDE.md` aggressively either** — but do keep it tight, because it loads into
*every* subagent's context. At ~350 tokens it's currently fine.

**Don't chase prompt caching.** One hour maximum TTL, 24 hours between runs.

---

## 6. Suggested sequence

**Week 1 — free wins, no architecture change.** Flags and models (§4), plus the quoted-email strip
and the `MAX_CHARS` drop (§3). Roughly an afternoon, and it lands ~45% of the cost saving on its
own.

**Week 2 — deterministic calendar (§1) and shared bundle (§2).** Both self-contained, both also
improve correctness.

**Week 3 — SOW cache (§4), then the delta gate (§5).** The gate last, because it's the only change
that introduces persistent state, and it wants the per-account token instrumentation from week 1
to prove it's working.

Instrument first, in all cases: log tokens per account per run with a changed/unchanged flag.
Everything above is measured from a single day. A fortnight of that flag tells you whether 46% of
accounts being unchanged is typical or was just a quiet Friday in August.

---

## Sources

[Manage costs — Claude Code](https://code.claude.com/docs/en/costs) ·
[CLI reference](https://code.claude.com/docs/en/cli-reference) ·
[Subagents](https://code.claude.com/docs/en/sub-agents) ·
[Prompt caching (Claude Code)](https://code.claude.com/docs/en/prompt-caching) ·
[Prompt caching (API)](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) ·
[Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) ·
[Pricing](https://platform.claude.com/docs/en/about-claude/pricing) ·
[Batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing) ·
[Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) ·
[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) ·
[Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) ·
[Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ·
[Context Rot — Chroma](https://www.trychroma.com/research/context-rot) ·
[dbt incremental models](https://docs.getdbt.com/docs/build/incremental-models) ·
[Gmail partial sync](https://developers.google.com/workspace/gmail/api/guides/sync) ·
[Drive changes feed](https://developers.google.com/workspace/drive/api/guides/manage-changes)
