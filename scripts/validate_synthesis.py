#!/usr/bin/env python3
"""Validate the agent's synthesis JSON before it is allowed near the sheet.

This is the safety gate that the Cowork version never had. The Apps Script
overwrites live cells from this file, so a malformed or hallucinated run must
fail here rather than in the sheet.

Checks, in order of how much damage they prevent:
  1. Valid JSON with the expected top-level shape.
  2. Every customer name matches the tracker's Accounts tab EXACTLY. Unknown names
     are ALWAYS fatal - a renamed account silently writing to nothing is the worst
     failure mode there is. Missing names are fatal only on a full-refresh day:
     the agent tiers its scope on Tue/Fri and deliberately omits dormant accounts,
     which must keep their previous values rather than be treated as an error.
  3. renewal_risk is one of Green / Amber / Red.
  4. Field-length rules from the agent brief (<=5 bullets at <=25 words,
     <=5 actions at <=20 words) - warn, do not fail.
  5. next_meeting / last_meeting_date match the required formats or the exact
     sentinel strings.

Usage:
  python scripts/validate_synthesis.py out/cs-agent-synthesis.json out/roster.txt
      [--require-full | --allow-subset]

Coverage mode is inferred from the day of the week - Monday is the weekly full
refresh - and can be forced either way with the flags.

`roster.txt` is one exact customer name per line, written by fetch_roster.py at
the start of the run - so the contract is checked against the live sheet, not a
hard-coded list that drifts.

Exit 0 = safe to upload. Exit 1 = do not upload.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys

VALID_RISK = {"Green", "Amber", "Red"}
NO_MEETING = "No meeting scheduled"
NO_RECENT = "No recent meeting"
REQUIRED_FIELDS = ("customer", "renewal_risk", "risk_rationale", "context_bullets",
                   "next_meeting", "last_meeting_date", "last_meeting_summary",
                   "outstanding_actions")

# "11 Aug 2026 12:00 — Powerverse/cord app meeting"  (em dash or hyphen tolerated)
NEXT_RE = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2} [—-] .+")
LAST_RE = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4}$")

errors: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) < 2:
        print("usage: validate_synthesis.py <synthesis.json> <roster.txt> "
              "[--require-full|--allow-subset]", file=sys.stderr)
        return 1
    synth_path, roster_path = args[0], args[1]

    # Monday is the weekly full refresh; Tue/Fri legitimately cover a subset.
    if "--require-full" in flags:
        require_full = True
    elif "--allow-subset" in flags:
        require_full = False
    else:
        require_full = dt.date.today().weekday() == 0

    try:
        with open(synth_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"FAIL: {synth_path} does not exist - the agent step produced no output.",
              file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"FAIL: {synth_path} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    with open(roster_path, encoding="utf-8") as fh:
        roster = [ln.strip() for ln in fh if ln.strip()]
    if not roster:
        print(f"FAIL: roster file {roster_path} is empty.", file=sys.stderr)
        return 1

    if not isinstance(data, dict) or "accounts" not in data:
        print("FAIL: top level must be an object with an 'accounts' key.", file=sys.stderr)
        return 1
    if not data.get("generated_at"):
        fail("missing 'generated_at'")

    accounts = data["accounts"]
    if not isinstance(accounts, list):
        print("FAIL: 'accounts' must be a list.", file=sys.stderr)
        return 1

    names = [a.get("customer") for a in accounts]

    # --- the contract that matters most: exact roster parity -----------------
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        fail(f"duplicate customer records: {sorted(dupes)}")

    missing = [n for n in roster if n not in names]
    extra = [n for n in names if n not in roster]

    # An unknown name is always fatal: it writes nowhere and the failure is silent.
    if extra:
        fail(f"not in the sheet's Accounts tab - these would write nowhere: {extra}")

    if missing:
        if require_full:
            fail(f"full-refresh day, but missing from synthesis: {missing}")
        else:
            print(f"INFO: tiered run - {len(names)}/{len(roster)} accounts covered, "
                  f"{len(missing)} skipped as dormant ({', '.join(missing)}). "
                  f"These keep their previous sheet values.")

    if not names:
        fail("no accounts in the synthesis at all - even a fully dormant day should "
             "cover the accounts with meetings or near renewals")

    # --- per-account field checks -------------------------------------------
    for acc in accounts:
        who = acc.get("customer", "<unnamed>")
        for field in REQUIRED_FIELDS:
            if field not in acc:
                fail(f"[{who}] missing field '{field}'")

        risk = acc.get("renewal_risk")
        if risk not in VALID_RISK:
            fail(f"[{who}] renewal_risk '{risk}' is not one of {sorted(VALID_RISK)}")

        if not (acc.get("risk_rationale") or "").strip():
            fail(f"[{who}] risk_rationale is empty")

        bullets = acc.get("context_bullets") or []
        if not isinstance(bullets, list):
            fail(f"[{who}] context_bullets must be a list")
        else:
            if not bullets:
                warn(f"[{who}] no context bullets")
            if len(bullets) > 5:
                warn(f"[{who}] {len(bullets)} context bullets (brief allows 5 unless vital)")
            for b in bullets:
                if len(str(b).split()) > 25:
                    warn(f"[{who}] context bullet over 25 words: {str(b)[:60]}...")

        actions = acc.get("outstanding_actions")
        if not isinstance(actions, list):
            fail(f"[{who}] outstanding_actions must be a list")
        else:
            if len(actions) > 5:
                warn(f"[{who}] {len(actions)} outstanding actions (brief allows 5 unless vital)")
            for a in actions:
                if len(str(a).split()) > 20:
                    warn(f"[{who}] action over 20 words: {str(a)[:60]}...")

        nxt = (acc.get("next_meeting") or "").strip()
        if nxt != NO_MEETING and not NEXT_RE.match(nxt):
            fail(f"[{who}] next_meeting must be 'dd MMM yyyy HH:mm — <title>' or "
                 f"'{NO_MEETING}', got: {nxt!r}")

        last = (acc.get("last_meeting_date") or "").strip()
        if last != NO_RECENT and not LAST_RE.match(last):
            fail(f"[{who}] last_meeting_date must be 'dd MMM yyyy' or '{NO_RECENT}', "
                 f"got: {last!r}")

        summary = acc.get("last_meeting_summary")
        if last == NO_RECENT and (summary or "").strip():
            warn(f"[{who}] last_meeting_date is '{NO_RECENT}' but a summary was written")
        if last != NO_RECENT and not (summary or "").strip():
            fail(f"[{who}] has last_meeting_date {last} but an empty last_meeting_summary")

        # A meeting cannot be both the last one and the next one.
        if last != NO_RECENT and nxt != NO_MEETING and nxt.startswith(last):
            fail(f"[{who}] next_meeting and last_meeting_date are the same event ({last})")

    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} blocking error(s) - NOT uploading to Drive.", file=sys.stderr)
        return 1

    mode = "full refresh" if require_full else "tiered"
    print(f"\nOK ({mode}): {len(accounts)}/{len(roster)} accounts, all names valid, "
          f"{len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
