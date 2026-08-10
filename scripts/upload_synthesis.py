#!/usr/bin/env python3
"""Upload the validated synthesis JSON to Drive as `cs-agent-synthesis.json`.

Runs only after validate_synthesis.py exits 0. Creates a NEW file each run with
the same name and text/plain MIME type - the Apps Script ingests the newest file
with that name, so history is preserved and the sheet always reads the latest.

This is deliberately the ONLY write the pipeline makes to Google. The agent's
Google credentials are read-only; this script uses a separate drive.file scope,
so a misbehaving agent cannot alter anything in Drive on its own.

Env:
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN
  SYNTHESIS_PARENT_ID   optional Drive folder id (defaults to My Drive root)

Usage: python scripts/upload_synthesis.py out/cs-agent-synthesis.json
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
from pv_workspace_mcp import google_access_token  # noqa: E402

UPLOAD_URL = ("https://www.googleapis.com/upload/drive/v3/files"
              "?uploadType=multipart&supportsAllDrives=true&fields=id,name,webViewLink")
FILENAME = "cs-agent-synthesis.json"
BOUNDARY = "pvcsagentboundary"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "out/cs-agent-synthesis.json"
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.", file=sys.stderr)
        return 1

    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    json.loads(content)  # belt and braces: never upload something unparseable

    metadata = {"name": FILENAME, "mimeType": "text/plain"}
    if os.environ.get("SYNTHESIS_PARENT_ID"):
        metadata["parents"] = [os.environ["SYNTHESIS_PARENT_ID"]]

    body = (
        f"--{BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{BOUNDARY}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
        f"{content}\r\n"
        f"--{BOUNDARY}--\r\n"
    ).encode("utf-8")

    req = urllib.request.Request(
        UPLOAD_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {google_access_token()}",
            "Content-Type": f"multipart/related; boundary={BOUNDARY}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read().decode("utf-8"))

    print(f"Uploaded {FILENAME} -> id={out.get('id')} {out.get('webViewLink', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
