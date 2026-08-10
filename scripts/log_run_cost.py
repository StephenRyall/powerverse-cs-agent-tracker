#!/usr/bin/env python3
"""Record what each run cost, so optimisation is measured rather than guessed.

Reads the `--output-format json` payload from the agent step and appends one row to
metrics/runs.csv, which the workflow commits back to the repo. After a fortnight that
file answers the questions you actually need:

  * is the per-run cost trending the way the optimisation plan predicted?
  * what proportion of accounts genuinely change day to day?  (the delta-gate business
    case - measure it before building it)
  * which model tier is doing the work?

Also writes a GitHub step summary so the number is visible without downloading anything.

Note: total_cost_usd from the CLI is a client-side estimate from a bundled price table.
Good for trend and for spotting a runaway run. Not a billing source.

Usage: python scripts/log_run_cost.py out/run.log [out/signal.json]
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys

CSV_PATH = "metrics/runs.csv"
FIELDS = ["date", "run_number", "cost_usd", "duration_s", "turns", "input_tokens",
          "output_tokens", "cache_read_tokens", "accounts", "accounts_changed",
          "pct_unchanged", "models"]


def load_result(path: str) -> dict:
    """The log may carry stray lines around the JSON; find the payload."""
    raw = open(path, encoding="utf-8", errors="replace").read().strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for line in reversed(raw.splitlines()):          # stream-json: last object wins
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if "total_cost_usd" in obj or obj.get("type") == "result":
                    return obj
            except json.JSONDecodeError:
                continue
    start = raw.find("{")
    if start >= 0:
        try:
            return json.loads(raw[start:])
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    log_path = sys.argv[1] if len(sys.argv) > 1 else "out/run.log"
    signal_path = sys.argv[2] if len(sys.argv) > 2 else "out/signal.json"

    result = load_result(log_path) if os.path.exists(log_path) else {}
    usage = result.get("usage") or {}
    model_usage = result.get("modelUsage") or result.get("model_usage") or {}

    # modelUsage includes subagent spend; usage does not. Prefer the former.
    def across_models(key: str) -> int:
        return sum(int((m or {}).get(key, 0) or 0) for m in model_usage.values())

    input_tokens = across_models("inputTokens") or across_models("input_tokens") \
        or int(usage.get("input_tokens", 0) or 0)
    output_tokens = across_models("outputTokens") or across_models("output_tokens") \
        or int(usage.get("output_tokens", 0) or 0)
    cache_read = across_models("cacheReadInputTokens") \
        or across_models("cache_read_input_tokens") \
        or int(usage.get("cache_read_input_tokens", 0) or 0)

    accounts = changed = 0
    if os.path.exists(signal_path):
        try:
            sig = json.load(open(signal_path, encoding="utf-8"))
            rows = sig.get("accounts", sig) if isinstance(sig, dict) else sig
            accounts = len(rows)
            changed = sum(1 for r in rows if r.get("changed"))
        except (json.JSONDecodeError, AttributeError, TypeError):
            print(f"WARN: could not parse {signal_path}", file=sys.stderr)

    row = {
        "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "run_number": os.environ.get("GITHUB_RUN_NUMBER", "local"),
        "cost_usd": round(float(result.get("total_cost_usd", 0) or 0), 4),
        "duration_s": round(float(result.get("duration_ms", 0) or 0) / 1000),
        "turns": result.get("num_turns", ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "accounts": accounts,
        "accounts_changed": changed,
        "pct_unchanged": (round(100 * (1 - changed / accounts)) if accounts else ""),
        "models": " ".join(sorted(model_usage)) or result.get("model", ""),
    }

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)

    total = input_tokens + output_tokens
    summary = (
        f"### CS Agent run {row['run_number']}\n\n"
        f"| metric | value |\n|---|---|\n"
        f"| cost (estimated) | ${row['cost_usd']} |\n"
        f"| tokens | {total:,} ({input_tokens:,} in / {output_tokens:,} out) |\n"
        f"| cache reads | {cache_read:,} |\n"
        f"| duration | {row['duration_s']}s over {row['turns']} turns |\n"
        f"| accounts changed | {changed}/{accounts}"
        + (f" ({row['pct_unchanged']}% unchanged)" if accounts else "") + " |\n"
        f"| models | {row['models']} |\n"
    )
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a", encoding="utf-8") as fh:
        fh.write(summary)

    print(summary)
    if accounts and changed == 0:
        print("NOTE: no account changed today - the delta gate would have made this run free.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
