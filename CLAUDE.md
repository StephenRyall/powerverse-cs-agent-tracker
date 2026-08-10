# powerverse-cs-agent-tracker — project rules

Google Sheet is the source of truth. Two automations serve it: an Apps Script bound to the
sheet, and a daily Claude Code run in GitHub Actions.

## Non-negotiables

- The agent **never** writes to the sheet, to Drive, or to Slack. It writes
  `out/cs-agent-synthesis.json` and `out/brief.md`. Scripts validate and deliver.
- Customer names must match `out/roster.txt` byte for byte. The validator enforces this.
- Every agent field **overwrites**. Nothing accumulates, no history, no new columns.
- The Apps Script resolves columns by header name, never by index. Keep it that way — fixed
  indexes are what caused the phantom-column bug in v1.
- Google credentials in this repo are read-only. The one write is `scripts/upload_synthesis.py`.

## Cost discipline

- Research subagents run a cheaper tier (`CLAUDE_CODE_SUBAGENT_MODEL`); judgement stays on Opus.
- Never widen `PV_MAX_TOOL_CHARS` / `PV_MAX_BODY_CHARS` without checking `metrics/runs.csv` after.
- Anything the pipeline can compute deterministically must not be computed by the model.
- Every run appends to `metrics/runs.csv`. Measure before optimising; see docs/TOKEN-OPTIMISATION.md.

## Conventions

- Python 3.12, stdlib only where possible. `pypdf` is the sole runtime dependency, for SOWs.
- Scripts are single-purpose, exit non-zero on failure, and print one clear line per outcome.
- Secrets only ever arrive via environment variables. Never commit a token, never log one.
- UK English in anything a human reads.

## Testing without credentials

```bash
python mcp/pv_workspace_mcp.py --selftest            # protocol + tool schemas, offline
python scripts/validate_synthesis.py <json> <roster> # gate logic, offline
python scripts/preflight.py                          # needs real credentials
```
