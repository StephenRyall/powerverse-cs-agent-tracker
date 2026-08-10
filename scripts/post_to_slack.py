#!/usr/bin/env python3
"""Post the morning brief to #cs-agent-alerts via chat.postMessage.

Slack posting is kept out of the agent on purpose (house pattern): the agent
writes out/brief.md, this script posts it. A deterministic poster cannot hang on
a permission prompt, cannot post twice, and cannot post to the wrong channel.

Env:
  SLACK_BOT_TOKEN   xoxb-... with chat:write (bot must be in the channel).
                    Falls back to SLACK_USER_TOKEN if no bot token is set.
  SLACK_CHANNEL_ID  target channel (C0BMQ4PGDA7 = #cs-agent-alerts)

Usage: python scripts/post_to_slack.py out/brief.md
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SLACK_URL = "https://slack.com/api/chat.postMessage"
MAX_CHARS = 38_000  # chat.postMessage text limit is 40k; leave headroom


def post(token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        SLACK_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "out/brief.md"
    token = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_USER_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")

    if not token or not channel:
        print("ERROR: need SLACK_BOT_TOKEN (or SLACK_USER_TOKEN) and SLACK_CHANNEL_ID.",
              file=sys.stderr)
        return 1
    if not os.path.exists(path):
        print(f"ERROR: {path} not found - the agent step did not write a brief.", file=sys.stderr)
        return 1

    text = open(path, encoding="utf-8").read().strip()
    if not text:
        print(f"ERROR: {path} is empty.", file=sys.stderr)
        return 1

    # Long briefs go out as a parent message plus threaded continuation, so a
    # busy morning never silently truncates.
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > MAX_CHARS and current:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)

    thread_ts = None
    try:
        for i, chunk in enumerate(chunks):
            payload = {"channel": channel, "text": chunk, "mrkdwn": True,
                       "unfurl_links": False, "unfurl_media": False}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            body = post(token, payload)
            if not body.get("ok"):
                print(f"ERROR: Slack rejected message {i + 1}: {body.get('error')}",
                      file=sys.stderr)
                return 1
            if i == 0:
                thread_ts = body.get("ts")
                print(f"Posted to {body.get('channel')} ts={thread_ts}")
            else:
                print(f"  ...continued in thread ({i + 1}/{len(chunks)})")
    except urllib.error.URLError as exc:
        print(f"ERROR: request to Slack failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
