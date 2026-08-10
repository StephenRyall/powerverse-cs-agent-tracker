#!/usr/bin/env python3
"""Project a sample run up to a full run, and check it against a budget.

Answers the only question that matters before going live: does this agent fit in
the share of capacity I am willing to give it?

Run a calibration pass over 2 accounts (PV_ACCOUNT_LIMIT=2), then point this at
the result. It scales the measured tokens and cost to the full roster and reports
monthly figures, so the decision rests on your own numbers rather than my estimate.

Usage:
  python scripts/calibrate.py out/run.log [--budget-usd-month 40] [--plan-share 50]

  --budget-usd-month   what you are willing to spend a month on this agent
  --plan-share         if on subscription auth: the % of your 5-hour window the
                       sample consumed, read from /usage before and after (see
                       docs/CALIBRATION.md). Scaled the same way.

Exit 0 if the projection fits the stated budget, 1 if it does not - so CI can
refuse to go live on a job that would eat more than you agreed to.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from log_run_cost import load_result  # noqa: E402

WEEKDAYS_PER_MONTH = 21.7


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default="out/run.log")
    ap.add_argument("--calibration", default="out/calibration.json")
    ap.add_argument("--budget-usd-month", type=float)
    ap.add_argument("--plan-share", type=float,
                    help="%% of the 5-hour window the SAMPLE consumed (from /usage)")
    args = ap.parse_args()

    result = load_result(args.log)
    if not result:
        print(f"ERROR: no usable result payload in {args.log}. Was the agent run with "
              f"--output-format json?", file=sys.stderr)
        return 1

    scale, sampled, full = 1.0, None, None
    if os.path.exists(args.calibration):
        cal = json.load(open(args.calibration, encoding="utf-8"))
        scale = float(cal.get("scale_factor", 1.0))
        sampled, full = cal.get("sampled"), cal.get("full_roster")

    model_usage = result.get("modelUsage") or result.get("model_usage") or {}

    def total(*keys: str) -> int:
        return sum(int((m or {}).get(k, 0) or 0)
                   for m in model_usage.values() for k in keys if k in (m or {}))

    tin = total("inputTokens", "input_tokens")
    tout = total("outputTokens", "output_tokens")
    cost = float(result.get("total_cost_usd", 0) or 0)
    tokens = tin + tout

    print("=" * 66)
    if sampled and full and scale > 1:
        print(f"CALIBRATION: measured {sampled} of {full} accounts "
              f"(scaling by {scale:.2f}x)")
    else:
        print("FULL RUN (no calibration file - treating this as the whole roster)")
    print("=" * 66)
    print(f"\nMeasured    {tokens:>12,} tokens   ${cost:>7.2f}")
    if model_usage:
        for name in sorted(model_usage):
            m = model_usage[name]
            mt = int(m.get("inputTokens", m.get("input_tokens", 0)) or 0) \
                + int(m.get("outputTokens", m.get("output_tokens", 0)) or 0)
            print(f"              {mt:>12,}   {name}")

    p_tokens, p_cost = tokens * scale, cost * scale
    print(f"\nProjected full run")
    print(f"  tokens      {round(p_tokens):>12,}")
    print(f"  cost        ${p_cost:>11.2f} per run")
    print(f"  per month   ${p_cost * WEEKDAYS_PER_MONTH:>11.2f}   ({WEEKDAYS_PER_MONTH:.1f} weekdays)")
    print(f"  per year    ${p_cost * 260:>11.2f}")

    verdict = 0

    if args.plan_share is not None:
        projected_share = args.plan_share * scale
        print(f"\nSubscription plan impact (from your /usage reading)")
        print(f"  sample consumed      {args.plan_share:>5.1f}% of the 5-hour window")
        print(f"  full run would use   {projected_share:>5.1f}%")
        if projected_share >= 50:
            print(f"  VERDICT: OVER HALF your window. On subscription auth this agent and "
                  f"your own work\n           draw from the SAME pool - a run this size can "
                  f"lock you out until reset.\n           Move it to a Console API key "
                  f"(see docs/CALIBRATION.md).")
            verdict = 1
        elif projected_share >= 30:
            print(f"  VERDICT: fits under half, but {projected_share:.0f}% is a large standing "
                  f"draw.\n           Worth moving to an API key so it cannot compete with you.")
        else:
            print(f"  VERDICT: comfortable - {projected_share:.0f}% leaves you headroom.")

    if args.budget_usd_month is not None:
        monthly = p_cost * WEEKDAYS_PER_MONTH
        pct = 100 * monthly / args.budget_usd_month if args.budget_usd_month else 0
        print(f"\nAgainst your ${args.budget_usd_month:.2f}/month budget")
        print(f"  projected   ${monthly:.2f}  ({pct:.0f}% of budget)")
        if monthly > args.budget_usd_month:
            print(f"  VERDICT: OVER budget by ${monthly - args.budget_usd_month:.2f}/month.")
            print(f"           Next levers: CLAUDE_CODE_SUBAGENT_MODEL=haiku, then the "
                  f"delta gate\n           (docs/TOKEN-OPTIMISATION.md items 1 and 5).")
            verdict = 1
        else:
            print(f"  VERDICT: fits, ${args.budget_usd_month - monthly:.2f}/month spare.")
        # A per-run cap set just above the projection stops drift becoming a surprise.
        suggested = max(0.5, round(p_cost * 1.35, 1))
        print(f"\n  Suggested --max-budget-usd: {suggested}  "
              f"(35% headroom over the projection)")
        print(f"  Set it as repo variable CS_AGENT_MAX_USD={suggested}")

    if args.plan_share is None and args.budget_usd_month is None:
        print("\nTip: pass --budget-usd-month and/or --plan-share for a pass/fail verdict.")

    print()
    return verdict


if __name__ == "__main__":
    sys.exit(main())
