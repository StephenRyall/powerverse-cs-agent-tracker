#!/usr/bin/env python3
"""Check every credential before go-live, so the first 07:00 run isn't the test.

Exercises each dependency with the smallest real call that proves the scope is
granted, and reports one line per check. Safe to run any time - it reads only,
except for the optional Slack post which is opt-in.

Credentials come from the environment. Since the real values live in GitHub Actions
secrets and not on your Mac, this script also loads a local `.env` file if one is
present, so you can check them without exporting seven variables by hand. `.env` is
gitignored - never commit it.

Usage:
  cp .env.example .env      # then paste your values in
  python3 scripts/preflight.py            # read-only checks
  python3 scripts/preflight.py --slack    # also posts a one-line test to the channel
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))


def load_dotenv(path: str = ".env") -> int:
    """Minimal .env loader - KEY=value per line, # comments, optional quotes.

    Deliberately does not overwrite anything already exported, so a real shell
    variable always beats the file.
    """
    if not os.path.exists(path):
        return 0
    loaded = 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
            loaded += 1
    return loaded


_n = load_dotenv()
import pv_workspace_mcp as pv  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, fn) -> None:
    try:
        results.append((label, True, fn()))
    except Exception as exc:
        results.append((label, False, str(exc)[:220]))


def main() -> int:
    if _n:
        print(f"Loaded {_n} value(s) from .env")
    elif not os.environ.get("GOOGLE_CLIENT_ID"):
        print("No .env found and nothing exported - run `cp .env.example .env` and fill it in.\n")

    check("Claude auth", lambda: (
        "CLAUDE_CODE_OAUTH_TOKEN set" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        else "ANTHROPIC_API_KEY set" if os.environ.get("ANTHROPIC_API_KEY")
        else (_ for _ in ()).throw(RuntimeError(
            "neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set"))))

    check("Google token refresh", lambda: f"access token ok ({len(pv.google_access_token())} chars)")

    check("Gmail  (gmail.readonly)", lambda: pv.gmail_search_threads(
        "newer_than:2d", 1).splitlines()[0])

    check("Drive  (drive.readonly)", lambda: pv.drive_search(
        "name = 'CS Agent Tracker' and mimeType = 'application/vnd.google-apps.spreadsheet'",
        3).splitlines()[0])

    today = dt.date.today()
    check("Calendar (calendar.readonly)", lambda: pv.calendar_list_events(
        f"{today}T00:00:00Z", f"{today + dt.timedelta(days=7)}T00:00:00Z",
        max_results=5).splitlines()[0])

    check("Slack search (search:read)", lambda: pv.slack_search("powerverse", 1).splitlines()[0])

    def slack_auth() -> str:
        token = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_USER_TOKEN")
        if not token:
            raise RuntimeError("no SLACK_BOT_TOKEN / SLACK_USER_TOKEN")
        req = urllib.request.Request("https://slack.com/api/auth.test",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
        if not body.get("ok"):
            raise RuntimeError(body.get("error", "auth.test failed"))
        return f"{body.get('user')} @ {body.get('team')}"

    check("Slack post token (chat:write)", slack_auth)

    check("Channel id", lambda: os.environ.get("SLACK_CHANNEL_ID")
          or (_ for _ in ()).throw(RuntimeError("SLACK_CHANNEL_ID not set")))

    if "--slack" in sys.argv:
        os.makedirs("out", exist_ok=True)
        with open("out/_preflight.md", "w", encoding="utf-8") as fh:
            fh.write("*CS Agent preflight* - credentials verified, ignore this message.")
        code = os.system(f"{sys.executable} scripts/post_to_slack.py out/_preflight.md")
        results.append(("Slack test post", code == 0, "sent" if code == 0 else "failed"))

    width = max(len(r[0]) for r in results)
    print()
    for label, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label.ljust(width)}  {detail}")

    failed = [r[0] for r in results if not r[1]]
    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        print("See docs/PHASE3-MIGRATION.md for the matching setup step.")
        return 1
    print("\nAll checks passed - safe to enable the schedule.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
