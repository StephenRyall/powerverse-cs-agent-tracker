#!/usr/bin/env python3
"""One-time helper: mint the Google refresh token the headless run needs.

Run this ONCE on your own Mac, signed in as stephen.ryall@powerverse.com. It
opens a browser, you approve the scopes, and it prints a refresh token to paste
into GitHub Actions secrets. Nothing is written to disk.

Prerequisite - a Google Cloud OAuth client (see docs/PHASE3-MIGRATION.md step 1):
  * OAuth consent screen: Internal (Powerverse Workspace)
  * Application type: Desktop app
  * Gmail, Drive and Calendar APIs enabled on the project

Usage:
  GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com \\
  GOOGLE_CLIENT_SECRET=GOCSPX-xxx \\
  python3 scripts/get_google_refresh_token.py
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/callback"

# Least privilege: read everything the agent needs, write only files this app creates.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_received: dict[str, str] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _received.update({k: v[0] for k, v in qs.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _received
        self.wfile.write(
            b"<h2>Done - you can close this tab and go back to the terminal.</h2>"
            if ok else b"<h2>Authorisation failed. Check the terminal.</h2>")

    def log_message(self, *_args):  # silence the default access log
        pass


def main() -> int:
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not csec:
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first. See the docstring.",
              file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",       # forces a refresh_token even on re-authorisation
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("localhost", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("Opening your browser to approve access...")
    print(f"If it does not open, paste this into a browser:\n\n{url}\n")
    webbrowser.open(url)

    while "code" not in _received and "error" not in _received:
        pass
    server.shutdown()

    if "error" in _received:
        print(f"Authorisation failed: {_received['error']}", file=sys.stderr)
        return 1
    if _received.get("state") != state:
        print("State mismatch - aborting.", file=sys.stderr)
        return 1

    data = urllib.parse.urlencode({
        "code": _received["code"], "client_id": cid, "client_secret": csec,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())

    token = body.get("refresh_token")
    if not token:
        print("No refresh_token returned. Revoke the app at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        print(json.dumps(body, indent=2), file=sys.stderr)
        return 1

    print("\n" + "=" * 72)
    print("GOOGLE_REFRESH_TOKEN (paste into GitHub -> Settings -> Secrets -> Actions):")
    print(token)
    print("=" * 72)
    print("\nTreat this like a password. It grants read access to your mailbox.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
