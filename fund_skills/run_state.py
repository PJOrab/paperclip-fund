#!/usr/bin/env python3
"""persist-run / read-run: manage a briefing_runs row — the pipeline state machine.

Subcommands:
  create --window 24                      -> insert a new run (status=triage), print {id,status}
  get [--id ID | --status S] [--field F]  -> print the run row, or just one field (JSON)
  set --id ID --field F [--status S]      -> update field F (value from --file or stdin) + optional status

Fields (= briefing_runs columns):
  triage, analysis, theses, devils_advocate  (JSON/JSONB)
  briefing_md, status, error                 (text)
  window_hours                               (int)

Status flow: triage -> analyst -> thesis -> devil -> editor -> done (or error)
"""
import argparse
import json
import os
import sys
from pathlib import Path

FUND_DIR = os.environ.get("FUND_DIR", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, FUND_DIR)

from datetime import datetime, timezone  # noqa: E402
from ingestion.db import client  # noqa: E402

JSON_FIELDS = {"triage", "analysis", "theses", "devils_advocate"}
TEXT_FIELDS = {"briefing_md", "status", "error"}
ALL_FIELDS = JSON_FIELDS | TEXT_FIELDS | {"window_hours"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--window", type=int, default=24)

    g = sub.add_parser("get")
    g.add_argument("--id")
    g.add_argument("--status")
    g.add_argument("--field")

    s = sub.add_parser("set")
    s.add_argument("--id", required=True)
    s.add_argument("--field", required=True, choices=sorted(ALL_FIELDS))
    s.add_argument("--status")
    s.add_argument("--file", default="-", help="value source ('-' = stdin)")

    a = ap.parse_args()
    t = client().table("briefing_runs")

    if a.cmd == "create":
        row = t.insert({"status": "triage", "window_hours": a.window}).execute().data[0]
        print(json.dumps({"id": row["id"], "status": row["status"]}))
        return

    if a.cmd == "get":
        if a.id:
            data = t.select("*").eq("id", a.id).limit(1).execute().data
        elif a.status:
            data = (t.select("*").eq("status", a.status)
                    .order("created_at", desc=True).limit(1).execute().data)
        else:
            data = t.select("*").order("created_at", desc=True).limit(1).execute().data
        row = data[0] if data else None
        if row is None:
            print("null")
            return
        print(json.dumps(row.get(a.field) if a.field else row, ensure_ascii=False))
        return

    if a.cmd == "set":
        raw = sys.stdin.read() if a.file == "-" else open(a.file).read()
        value = raw.strip() if a.field in TEXT_FIELDS else json.loads(raw)
        if a.field == "window_hours":
            value = int(raw.strip())
        fields = {a.field: value, "updated_at": _now()}
        if a.status:
            fields["status"] = a.status
        t.update(fields).eq("id", a.id).execute()
        print(json.dumps({"ok": True, "id": a.id, "updated": a.field, "status": a.status}))
        return


if __name__ == "__main__":
    main()
