#!/usr/bin/env python3
"""read-feed: print recent raw_items (the feed window) as JSON, indexed for item_refs.

Usage: python fund_skills/read_feed.py --window 24 [--limit 400]
Reuses the existing data layer (ingestion.db) and Supabase config.
"""
import argparse
import json
import os
import sys
from pathlib import Path

FUND_DIR = os.environ.get("FUND_DIR", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, FUND_DIR)

from datetime import datetime, timezone, timedelta  # noqa: E402
from ingestion.db import client  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=24, help="lookback window in hours")
    ap.add_argument("--limit", type=int, default=400)
    a = ap.parse_args()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=a.window)).isoformat()
    rows = (client().table("raw_items")
            .select("source,text,url,reliability,fetched_at")
            .gte("fetched_at", cutoff).order("fetched_at", desc=True)
            .limit(a.limit).execute().data or [])
    feed = [{"i": i, **r} for i, r in enumerate(rows)]
    print(json.dumps({"window_hours": a.window, "count": len(feed), "items": feed},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
