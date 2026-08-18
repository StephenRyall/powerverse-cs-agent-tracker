#!/usr/bin/env python3
"""Post the morning brief to #cs-agent-alerts via chat.postMessage.

Slack posting is kept out of the agent on purpose (house pattern): the agent
writes out/brief.md, this script posts it. A deterministic poster cannot hang on
a permission prompt, cannot post twice, and cannot post to the wrong channel.

Briefs written to the STEP 5 shape (bold title line, bold section headings) are
rendered as Block Kit: a header, a context line, then one attachment per
section. Every "Risk changes" bullet gets its own red/amber/green colour bar
keyed off that bullet's own rating change; every other section is an
uncoloured attachment, so severity is visible per account rather than as one
bar for the whole message. Anything that doesn't match that shape (preflight
pings, failure notices) falls back to the original plain-text post, so those
paths never depend on the brief format.

Env:
  SLACK_BOT_TOKEN   xoxb-... with chat:write (bot must be in the channel).
                    Falls back to SLACK_USER_TOKEN if no bot token is set.
  SLACK_CHANNEL_ID  target channel (C0BMQ4PGDA7 = #cs-agent-alerts)

Usage: python scripts/post_to_slack.py out/brief.md [--dry-run]
       --dry-run prints the payloads instead of posting (no tokens needed).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

SLACK_URL = "https://slack.com/api/chat.postMessage"
MAX_CHARS = 38_000     # plain-text fallback: chat.postMessage text limit is 40k
BLOCK_CHARS = 2_900    # per section block: Slack's mrkdwn limit is 3000
MSG_ATTACHMENTS = 40   # attachments per message; generous headroom under Slack's cap
MSG_CHARS = 11_000     # char budget per message, so payloads stay small

GREEN, AMBER, RED = "#2EB67D", "#ECB22E", "#E01E5A"

HEADING_RE = re.compile(r"^\*[^*]+\*$")  # a line that is nothing but *bold text*


def post(token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        SLACK_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_brief(text: str) -> dict | None:
    """Split the brief into title / context / sections, or None if it doesn't
    look like a STEP 5 brief (then the caller falls back to plain text)."""
    lines = text.split("\n")
    if not lines or not HEADING_RE.match(lines[0].strip()):
        return None

    title = lines[0].strip().strip("*")
    body = lines[1:]

    context = None
    while body and not body[0].strip():
        body.pop(0)
    if body and re.match(r"^_[^_]+_$", body[0].strip()):
        context = body.pop(0).strip()

    sections: list[tuple[str, list[str]]] = []
    for line in body:
        if HEADING_RE.match(line.strip()):
            sections.append((line.strip(), []))
        elif sections:
            sections[-1][1].append(line)

    if not sections:
        return None
    return {"title": title, "context": context, "sections": sections}


def line_colour(text: str) -> str | None:
    """Red/amber/green if this text names its own rating change, else None —
    a bullet that didn't move (or isn't a rating line at all) gets no bar."""
    if ":red_circle:" in text or "→ *Red*" in text or "→ Red" in text:
        return RED
    if ":large_orange_circle:" in text or "→ *Amber*" in text or "→ Amber" in text:
        return AMBER
    if ":large_green_circle:" in text or "→ *Green*" in text or "→ Green" in text:
        return GREEN
    return None


def split_risk_paragraphs(body_lines: list[str]) -> list[tuple[str | None, str]]:
    """Split the Risk changes body into (colour, paragraph) pairs — one pair
    per bullet, each coloured off that bullet alone, so a mixed brief (one Red
    change, one Amber, others holding) shows every colour it actually has."""
    body = "\n".join(body_lines).strip("\n")
    pairs = []
    for para in body.split("\n\n"):
        if not para.strip():
            continue
        stripped = para.strip()
        colour = line_colour(stripped) if stripped.startswith(("•", "-")) else None
        pairs.append((colour, para))
    return pairs


def chunk(text: str, limit: int = BLOCK_CHARS) -> list[str]:
    """Split on paragraphs, then lines, so no chunk exceeds a block's limit."""
    chunks, current = [], ""
    pieces = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            pieces.append(para)
        else:  # single oversized paragraph: split on lines
            sub = ""
            for line in para.split("\n"):
                if len(sub) + len(line) + 1 > limit and sub:
                    pieces.append(sub)
                    sub = line
                else:
                    sub = f"{sub}\n{line}" if sub else line
            if sub:
                pieces.append(sub)
    for piece in pieces:
        if len(current) + len(piece) + 2 > limit and current:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def section_blocks(heading: str, body_lines: list[str]) -> list[dict]:
    body = "\n".join(body_lines).strip()
    text = f"{heading}\n{body}" if body else heading
    return [{"type": "section", "text": {"type": "mrkdwn", "text": part}}
            for part in chunk(text)]


def section_attachments(heading: str, body_lines: list[str]) -> list[dict]:
    """One attachment for an ordinary section, or one heading attachment plus
    one per bullet — each with its own colour — for Risk changes."""
    if "risk changes" not in heading.lower():
        return [{"blocks": section_blocks(heading, body_lines)}]

    attachments = [{"blocks": [{"type": "section",
                                 "text": {"type": "mrkdwn", "text": heading}}]}]
    for colour, para in split_risk_paragraphs(body_lines):
        att = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": part}}
                           for part in chunk(para)]}
        if colour:
            att["color"] = colour
        attachments.append(att)
    return attachments


def build_messages(brief: dict) -> list[dict]:
    """One parent message (header + context, then one attachment per
    section) plus threaded continuations if the brief is unusually long.
    Attachments render in the order given, so section order is preserved
    even though only some of them carry a colour bar."""
    all_attachments: list[dict] = []
    for heading, body_lines in brief["sections"]:
        all_attachments.extend(section_attachments(heading, body_lines))

    # Greedily pack attachments into messages within count/char budgets.
    batches, batch, batch_chars = [], [], 0
    for att in all_attachments:
        size = len(json.dumps(att))
        if batch and (len(batch) >= MSG_ATTACHMENTS or batch_chars + size > MSG_CHARS):
            batches.append(batch)
            batch, batch_chars = [], 0
        batch.append(att)
        batch_chars += size
    if batch:
        batches.append(batch)

    messages = []
    for i, attachments in enumerate(batches):
        top: list[dict] = []
        if i == 0:
            top.append({"type": "header",
                        "text": {"type": "plain_text", "text": brief["title"]}})
            if brief["context"]:
                top.append({"type": "context",
                            "elements": [{"type": "mrkdwn",
                                          "text": brief["context"]}]})
        messages.append({
            "text": brief["title"] if i == 0 else f"{brief['title']} (cont.)",
            "blocks": top,
            "attachments": attachments,
            "unfurl_links": False, "unfurl_media": False,
        })
    return messages


def plain_messages(text: str) -> list[dict]:
    """The original behaviour, for content that isn't a structured brief."""
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > MAX_CHARS and current:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return [{"text": c, "mrkdwn": True,
             "unfurl_links": False, "unfurl_media": False} for c in chunks]


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]
    path = args[0] if args else "out/brief.md"

    token = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_USER_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not dry_run and (not token or not channel):
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

    brief = parse_brief(text)
    messages = build_messages(brief) if brief else plain_messages(text)

    if dry_run:
        print(f"{'structured brief' if brief else 'plain text'}: "
              f"{len(messages)} message(s)")
        for msg in messages:
            print(json.dumps(msg, indent=2, ensure_ascii=False))
        return 0

    thread_ts = None
    try:
        for i, msg in enumerate(messages):
            payload = {"channel": channel, **msg}
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
                print(f"  ...continued in thread ({i + 1}/{len(messages)})")
    except urllib.error.URLError as exc:
        print(f"ERROR: request to Slack failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
