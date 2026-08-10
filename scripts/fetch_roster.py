#!/usr/bin/env python3
"""Fetch the live customer roster from the CS Agent Tracker's Accounts tab.

Run FIRST in the pipeline. Two jobs:
  * writes out/roster.txt - one exact customer name per line, which
    validate_synthesis.py then uses to enforce name parity;
  * writes out/accounts.csv - the whole Accounts tab, which the agent reads
    instead of spending a tool call rediscovering the sheet.

Because the roster comes from the sheet on every run, accounts added or removed
by the CS team are picked up automatically - the agent brief's "do not assume a
fixed roster" rule is enforced mechanically rather than by good intentions.

Usage: python scripts/fetch_roster.py [output-dir]   (default: out)
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
from pv_workspace_mcp import DRIVE_API, google_access_token, _g  # noqa: E402

TRACKER_NAME = "CS Agent Tracker"


def find_tracker_id() -> str:
    """Locate the tracker by name, exactly as the Cowork agent did."""
    if os.environ.get("TRACKER_FILE_ID"):
        return os.environ["TRACKER_FILE_ID"]

    q = (f"name = '{TRACKER_NAME}' and "
         "mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false")
    data = _g(f"{DRIVE_API}/files", {
        "q": q, "fields": "files(id,name,modifiedTime)",
        "orderBy": "modifiedTime desc", "pageSize": 10,
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
    })
    files = data.get("files", [])
    if not files:
        raise SystemExit(f"FAIL: no Google Sheet named '{TRACKER_NAME}' found in Drive.")
    if len(files) > 1:
        print(f"WARN: {len(files)} sheets named '{TRACKER_NAME}'; using the most recently "
              f"modified ({files[0]['id']}). Set TRACKER_FILE_ID to pin one.", file=sys.stderr)
    return files[0]["id"]


def export_csv(file_id: str) -> str:
    url = (f"{DRIVE_API}/files/{file_id}/export?"
           + urllib.parse.urlencode({"mimeType": "text/csv"}))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {google_access_token()}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_roster(body: str) -> list[str]:
    """Pull the customer column out of the exported Accounts tab.

    The export can carry the sheet's other tabs below the Accounts rows, so we
    anchor on the 'Customer' header and stop at the first blank row or at a
    header belonging to another tab.
    """
    rows = list(csv.reader(io.StringIO(body)))
    header_idx = next((i for i, r in enumerate(rows[:20])
                       if r and r[0].strip().lower() == "customer"), None)
    if header_idx is None:
        raise SystemExit("FAIL: could not find a header row starting with 'Customer'.")

    stop_words = {"date", "setting", "how this sheet works", "customer"}
    names: list[str] = []
    for row in rows[header_idx + 1:]:
        name = row[0].strip() if row else ""
        if not name or name.lower() in stop_words:
            break
        names.append(name)

    if not names:
        raise SystemExit("FAIL: no customer names parsed from the Accounts tab.")
    return names


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "out"
    os.makedirs(out_dir, exist_ok=True)

    file_id = find_tracker_id()
    print(f"Tracker: {file_id}")
    body = export_csv(file_id)

    accounts_path = os.path.join(out_dir, "accounts.csv")
    with open(accounts_path, "w", encoding="utf-8") as fh:
        fh.write(body)

    names = parse_roster(body)

    # Calibration mode: research only the first N accounts so a first run costs a
    # fraction of a full one and can be extrapolated. The roster file is truncated
    # too, so the validator stays consistent and does not report the rest as missing.
    limit = int(os.environ.get("PV_ACCOUNT_LIMIT", "0") or 0)
    if limit > 0:
        full = len(names)
        names = names[:limit]
        print(f"CALIBRATION MODE: {len(names)} of {full} accounts "
              f"({', '.join(names)}). Multiply the measured cost by "
              f"{full / len(names):.2f} to estimate a full run.")
        with open(os.path.join(out_dir, "calibration.json"), "w", encoding="utf-8") as fh:
            json.dump({"sampled": len(names), "full_roster": full,
                       "scale_factor": round(full / len(names), 4)}, fh)

    roster_path = os.path.join(out_dir, "roster.txt")
    with open(roster_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(names) + "\n")

    print(f"Roster ({len(names)}): {', '.join(names)}")
    print(f"Wrote {roster_path} and {accounts_path}")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as gh:
        gh.write(f"tracker_id={file_id}\nroster_count={len(names)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
