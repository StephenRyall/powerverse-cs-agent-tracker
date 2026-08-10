#!/usr/bin/env python3
"""Powerverse CS Agent — headless Workspace MCP server (stdio).

Purpose-built MCP server exposing exactly the read tools the CS Agent Tracker
daily run needs, authenticated from environment variables so it works in CI with
no browser and no interactive OAuth.

Deliberately narrow by design:
  * Google access is READ-ONLY (gmail.readonly, drive.readonly, calendar.readonly).
    The only Drive write in the pipeline is the synthesis file, and that is done by
    scripts/upload_synthesis.py AFTER schema validation - not by the agent.
  * Slack access is search-only. Posting is done by scripts/post_to_slack.py.
  * No third-party MCP packages. Plain stdlib JSON-RPC over stdio, so there is no
    supply-chain surface on a server that reads the CS lead's mailbox.

Tools exposed:
  gmail_search_threads   gmail_get_thread
  drive_search           drive_read
  calendar_list_events
  slack_search

Environment:
  GOOGLE_CLIENT_ID       OAuth desktop-app client id
  GOOGLE_CLIENT_SECRET   OAuth desktop-app client secret
  GOOGLE_REFRESH_TOKEN   long-lived refresh token (see scripts/get_google_refresh_token.py)
  SLACK_USER_TOKEN       xoxp-... with search:read   (search.messages needs a USER token)

Run standalone for a smoke test:
  python mcp/pv_workspace_mcp.py --selftest
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pv-workspace"
SERVER_VERSION = "1.0.0"

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE_API = "https://www.googleapis.com/drive/v3"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SLACK_API = "https://slack.com/api"

HTTP_TIMEOUT = 60

# Caps. A run makes ~130 tool calls, so a generous per-call ceiling is how a context
# blows up. 25k chars is ~6.7k tokens; Claude Code's own MCP default is 25k *tokens*,
# so this is deliberately tighter. Override per-deployment if a real result gets clipped.
MAX_CHARS = int(os.environ.get("PV_MAX_TOOL_CHARS", 25_000))
MAX_BODY_CHARS = int(os.environ.get("PV_MAX_BODY_CHARS", 6_000))  # per email message


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _request(url, *, method="GET", headers=None, data=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code} from {url.split('?')[0]}: {body}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error calling {url.split('?')[0]}: {exc}") from None


def _request_bytes(url, *, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code} from {url.split('?')[0]}: {body}") from None


# --------------------------------------------------------------------------- #
# Google auth - refresh token -> short-lived access token, cached in-process
# --------------------------------------------------------------------------- #

_token_cache = {"value": None, "expires_at": 0.0}


def _real(value: str | None) -> str | None:
    """None for a missing OR unexpanded env var (".mcp.json" leaves "${VAR}" behind)."""
    if not value:
        return None
    v = value.strip()
    return None if (v.startswith("${") and v.endswith("}")) else v


def google_access_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["value"]

    # An unset secret reaches us from .mcp.json as the literal "${VAR}" rather than as
    # nothing at all. Left alone that gets posted to Google and comes back as a baffling
    # 401 invalid_client, so treat an unexpanded placeholder as missing.
    missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
               if not _real(os.environ.get(k))]
    if missing:
        raise RuntimeError(
            "Google credentials missing from the environment: " + ", ".join(missing)
            + ". Run scripts/get_google_refresh_token.py once locally, then set these as "
              "GitHub Actions secrets."
        )

    payload = urllib.parse.urlencode({
        "client_id": _real(os.environ.get("GOOGLE_CLIENT_ID")),
        "client_secret": _real(os.environ.get("GOOGLE_CLIENT_SECRET")),
        "refresh_token": _real(os.environ.get("GOOGLE_REFRESH_TOKEN")),
        "grant_type": "refresh_token",
    }).encode()

    body = _request(
        TOKEN_URL, method="POST", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"Google token refresh returned no access_token: {body}")

    _token_cache["value"] = token
    _token_cache["expires_at"] = now + float(body.get("expires_in", 3600))
    return token


def _g(url: str, params: dict | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    return _request(url, headers={"Authorization": f"Bearer {google_access_token()}"})


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n[... truncated, {len(text) - MAX_CHARS} more characters ...]"


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #

def _headers_map(payload: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in (payload or {}).get("headers", [])}


def _decode_b64url(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """Depth-first walk for the best text/plain part, falling back to stripped HTML."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _decode_b64url(body["data"])
    for part in payload.get("parts", []) or []:
        found = _extract_body(part)
        if found.strip():
            return found
    if mime == "text/html" and body.get("data"):
        import re
        html = _decode_b64url(body["data"])
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        html = re.sub(r"(?s)<[^>]+>", " ", html)
        return re.sub(r"[ \t\r\f\v]+", " ", html)
    return ""


def _strip_quoted(text: str) -> str:
    """Remove quoted history, signatures and legal footers from an email body.

    In a ten-message thread the first message is quoted nine times. The model has
    already read it higher up the thread, so every repeat is pure cost. This is the
    single largest cheap saving in the pipeline - typically 30-40% of Gmail volume.

    Conservative by design: it only cuts at markers that unambiguously begin quoted
    or boilerplate content, and it never returns empty when the original had text.
    """
    import re

    if not text:
        return ""

    lines = text.replace("\r\n", "\n").split("\n")
    kept: list[str] = []

    # Anything at or below one of these starts quoted history / boilerplate.
    cut_at = re.compile(
        r"^\s*(?:"
        r"On .{0,120}\bwrote:\s*$"                      # On 6 Aug 2026, X wrote:
        r"|On .{0,80},? at .{0,40},? .{0,80} wrote:"    # Apple Mail variant
        r"|-{2,}\s*Original Message\s*-{2,}"
        r"|-{2,}\s*Forwarded message\s*-{2,}"
        r"|_{10,}"                                       # Outlook divider rule
        r"|From:\s.+\[mailto:"                           # Outlook quoted header block
        r"|Sent from my \w+"
        r"|Get Outlook for \w+"
        r"|This (?:e-?mail|message) (?:and any attachments )?(?:is|are) (?:confidential|intended)"
        r"|CONFIDENTIALITY NOTICE"
        r"|Please consider the environment"
        r"|Unsubscribe(?: from this list)?\s*$"
        r")", re.IGNORECASE)

    sig = re.compile(r"^--\s*$")            # RFC 3676 signature delimiter
    quoted = re.compile(r"^\s*>")

    for line in lines:
        if cut_at.match(line) or sig.match(line):
            break
        if quoted.match(line):
            continue                         # drop quoted lines wherever they appear
        kept.append(line)

    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    # Never hand back nothing when there was something - a body we failed to parse is
    # far worse than a body with some quoting left in it.
    if not out and text.strip():
        out = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(out) > MAX_BODY_CHARS:
        out = out[:MAX_BODY_CHARS] + f"\n[... {len(out) - MAX_BODY_CHARS} more characters ...]"
    return out


def gmail_search_threads(query: str, max_results: int = 20) -> str:
    data = _g(f"{GMAIL_API}/threads", {"q": query, "maxResults": min(int(max_results), 50)})
    threads = data.get("threads", [])
    if not threads:
        return f"No threads matched: {query}"

    lines = [f"{len(threads)} thread(s) matching: {query}", ""]
    for th in threads:
        meta = _g(f"{GMAIL_API}/threads/{th['id']}", {"format": "metadata"})
        msgs = meta.get("messages", [])
        if not msgs:
            continue
        first, last = _headers_map(msgs[0].get("payload", {})), _headers_map(msgs[-1].get("payload", {}))
        lines.append(
            f"- thread_id: {th['id']}\n"
            f"  subject: {first.get('subject', '(no subject)')}\n"
            f"  messages: {len(msgs)} | last from: {last.get('from', '?')} | last date: {last.get('date', '?')}\n"
            f"  snippet: {(th.get('snippet') or '')[:300]}"
        )
    return _clip("\n".join(lines))


def gmail_get_thread(thread_id: str) -> str:
    data = _g(f"{GMAIL_API}/threads/{thread_id}", {"format": "full"})
    out = [f"Thread {thread_id} - {len(data.get('messages', []))} message(s)", ""]
    for i, msg in enumerate(data.get("messages", []), 1):
        h = _headers_map(msg.get("payload", {}))
        body = _strip_quoted(_extract_body(msg.get("payload", {})))
        out.append(
            f"--- message {i} ---\n"
            f"From: {h.get('from', '?')}\nTo: {h.get('to', '?')}\n"
            f"Cc: {h.get('cc', '')}\nDate: {h.get('date', '?')}\n"
            f"Subject: {h.get('subject', '')}\n\n{body}"
        )
    return _clip("\n\n".join(out))


# --------------------------------------------------------------------------- #
# Drive
# --------------------------------------------------------------------------- #

EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def drive_search(query: str, page_size: int = 20) -> str:
    data = _g(f"{DRIVE_API}/files", {
        "q": query,
        "pageSize": min(int(page_size), 100),
        "fields": "files(id,name,mimeType,modifiedTime,createdTime,owners(emailAddress),webViewLink)",
        "orderBy": "modifiedTime desc",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    })
    files = data.get("files", [])
    if not files:
        return f"No files matched: {query}"
    lines = [f"{len(files)} file(s) matching: {query}", ""]
    for f in files:
        owner = (f.get("owners") or [{}])[0].get("emailAddress", "?")
        lines.append(
            f"- {f['name']}\n  id: {f['id']}\n  type: {f.get('mimeType')}\n"
            f"  modified: {f.get('modifiedTime')} | owner: {owner}"
        )
    return _clip("\n".join(lines))


def drive_read(file_id: str) -> str:
    meta = _g(f"{DRIVE_API}/files/{file_id}",
              {"fields": "id,name,mimeType,modifiedTime", "supportsAllDrives": "true"})
    mime = meta.get("mimeType", "")
    auth = {"Authorization": f"Bearer {google_access_token()}"}
    header = f"# {meta.get('name')}\n(type: {mime}, modified: {meta.get('modifiedTime')})\n\n"

    if mime in EXPORT_AS:
        url = f"{DRIVE_API}/files/{file_id}/export?" + urllib.parse.urlencode(
            {"mimeType": EXPORT_AS[mime]})
        return _clip(header + _request_bytes(url, headers=auth).decode("utf-8", "replace"))

    raw = _request_bytes(
        f"{DRIVE_API}/files/{file_id}?alt=media&supportsAllDrives=true", headers=auth)

    if mime == "application/pdf":
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
            return _clip(header + (text.strip() or "[PDF contained no extractable text layer]"))
        except ImportError:
            return header + "[PDF: pypdf not installed - add pypdf to mcp/requirements.txt]"
        except Exception as exc:
            return header + f"[PDF could not be parsed: {exc}]"

    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return _clip(header + raw.decode("utf-8", "replace"))

    return header + f"[Binary file, {len(raw)} bytes - no text extractor for {mime}]"


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #

def calendar_list_events(time_min: str, time_max: str, query: str | None = None,
                         max_results: int = 250) -> str:
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": min(int(max_results), 250),
        "timeZone": "Europe/London",
    }
    if query:
        params["q"] = query

    events, page = [], None
    while True:
        if page:
            params["pageToken"] = page
        data = _g(f"{CALENDAR_API}/calendars/primary/events", params)
        events.extend(data.get("items", []))
        page = data.get("nextPageToken")
        if not page or len(events) >= 500:
            break

    if not events:
        return f"No events between {time_min} and {time_max}" + (f" matching '{query}'" if query else "")

    lines = [f"{len(events)} event(s) {time_min} -> {time_max}"
             + (f" matching '{query}'" if query else ""), ""]
    for e in events:
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", "?")
        end = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date", "?")
        guests = ";".join(a.get("email", "") for a in (e.get("attendees") or []))
        lines.append(f"- {start} -> {end} | {e.get('summary', '(no title)')}\n  attendees: {guests}")
    return _clip("\n".join(lines))


# --------------------------------------------------------------------------- #
# Slack (search only - posting is done deterministically by scripts/post_to_slack.py)
# --------------------------------------------------------------------------- #

def slack_search(query: str, count: int = 20) -> str:
    token = _real(os.environ.get("SLACK_USER_TOKEN"))
    if not token:
        raise RuntimeError(
            "SLACK_USER_TOKEN is not set. search.messages requires a USER token (xoxp-) "
            "with the search:read scope - a bot token cannot call it."
        )
    url = f"{SLACK_API}/search.messages?" + urllib.parse.urlencode({
        "query": query, "count": min(int(count), 50), "sort": "timestamp", "sort_dir": "desc",
    })
    body = _request(url, headers={"Authorization": f"Bearer {token}"})
    if not body.get("ok"):
        raise RuntimeError(f"Slack search failed: {body.get('error')}")

    matches = (body.get("messages") or {}).get("matches", [])
    if not matches:
        return f"No Slack messages matched: {query}"

    import datetime as _dt
    lines = [f"{len(matches)} Slack message(s) matching: {query}", ""]
    for m in matches:
        try:
            when = _dt.datetime.fromtimestamp(float(m.get("ts", 0))).strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = m.get("ts", "?")
        chan = (m.get("channel") or {}).get("name", "?")
        lines.append(f"- [{when}] #{chan} @{m.get('username', m.get('user', '?'))}: "
                     f"{(m.get('text') or '')[:1200]}\n  link: {m.get('permalink', '')}")
    return _clip("\n".join(lines))


# --------------------------------------------------------------------------- #
# Tool registry
# --------------------------------------------------------------------------- #

TOOLS = [
    {
        "name": "gmail_search_threads",
        "description": ("Search the CS lead's Gmail and return matching threads with subject, "
                        "participants, dates and a snippet. Uses standard Gmail query syntax "
                        "(e.g. \"cord-ev.com newer_than:14d\"). Returns thread_ids to pass to "
                        "gmail_get_thread."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max threads (default 20, cap 50)"},
            },
            "required": ["query"],
        },
        "fn": lambda a: gmail_search_threads(a["query"], a.get("max_results", 20)),
    },
    {
        "name": "gmail_get_thread",
        "description": "Fetch one Gmail thread in full, with every message's headers and plain-text body.",
        "inputSchema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
        },
        "fn": lambda a: gmail_get_thread(a["thread_id"]),
    },
    {
        "name": "drive_search",
        "description": ("Search Google Drive using Drive query syntax, e.g. "
                        "\"name contains 'Notes by Gemini' and modifiedTime > '2026-07-24T00:00:00Z'\". "
                        "Note this is the Drive v3 API, so the field is `name`, not `title`. "
                        "Returns file ids to pass to drive_read."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Drive v3 `q` expression"},
                "page_size": {"type": "integer", "description": "Max files (default 20, cap 100)"},
            },
            "required": ["query"],
        },
        "fn": lambda a: drive_search(a["query"], a.get("page_size", 20)),
    },
    {
        "name": "drive_read",
        "description": ("Read a Drive file as text. Google Docs and Slides export as plain text, "
                        "Sheets as CSV, PDFs are text-extracted. Requires a file id from drive_search."),
        "inputSchema": {
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
        "fn": lambda a: drive_read(a["file_id"]),
    },
    {
        "name": "calendar_list_events",
        "description": ("List primary-calendar events in an ISO-8601 time range, with start, end "
                        "and attendee emails. Recurring events are expanded to single instances."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO-8601 lower bound"},
                "time_max": {"type": "string", "description": "ISO-8601 upper bound"},
                "query": {"type": "string", "description": "Optional free-text filter"},
                "max_results": {"type": "integer"},
            },
            "required": ["time_min", "time_max"],
        },
        "fn": lambda a: calendar_list_events(a["time_min"], a["time_max"],
                                             a.get("query"), a.get("max_results", 250)),
    },
    {
        "name": "slack_search",
        "description": ("Search Slack messages across channels and DMs the token owner can see. "
                        "Supports Slack modifiers (in:, from:, after:). Read-only - posting the "
                        "morning brief is handled outside the agent."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer", "description": "Max results (default 20, cap 50)"},
            },
            "required": ["query"],
        },
        "fn": lambda a: slack_search(a["query"], a.get("count", 20)),
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def public_tools():
    return [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]


# --------------------------------------------------------------------------- #
# JSON-RPC over stdio
# --------------------------------------------------------------------------- #

def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(rid, payload):
    _send({"jsonrpc": "2.0", "id": rid, "result": payload})


def handle(msg: dict):
    method, rid = msg.get("method"), msg.get("id")

    if method == "initialize":
        _result(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method in ("notifications/initialized", "initialized"):
        pass  # notification, no reply
    elif method == "tools/list":
        _result(rid, {"tools": public_tools()})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if not tool:
            _result(rid, {"content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                          "isError": True})
            return
        try:
            text = tool["fn"](args)
            _result(rid, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # surface as tool error, never crash the server
            _result(rid, {"content": [{"type": "text", "text": f"ERROR: {exc}"}],
                          "isError": True})
    elif method == "ping":
        _result(rid, {})
    elif rid is not None:
        _send({"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32601, "message": f"Method not found: {method}"}})


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(msg)
        except Exception as exc:  # last-resort guard
            if msg.get("id") is not None:
                _send({"jsonrpc": "2.0", "id": msg["id"],
                       "error": {"code": -32603, "message": str(exc)}})


def selftest() -> int:
    """Offline check: protocol shape and tool schemas. Makes no network calls."""
    names = [t["name"] for t in public_tools()]
    assert len(names) == 6, names
    for t in public_tools():
        assert t["description"] and t["inputSchema"]["type"] == "object", t["name"]
        for req in t["inputSchema"].get("required", []):
            assert req in t["inputSchema"]["properties"], (t["name"], req)
    print("selftest OK -", ", ".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else (serve() or 0))
