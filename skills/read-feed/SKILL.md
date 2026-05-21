---
name: read-feed
description: >
  Read the AI/Tech Fund's recent raw_items feed window (SEC filings, insider Form 4,
  arXiv, GitHub trending, Hacker News, tech RSS, NewsAPI, X, FRED) as indexed JSON.
  Use at the start of a briefing run to get the material to triage/analyze. Each item
  has an integer index `i` so the Triage stage can reference items via item_refs.
---

# read-feed

Returns the most recent `raw_items` for a time window from Supabase. Reasoning happens in your context — this skill only fetches data.

## When to use
- **Triage**: pull the window, then cluster the material items.
- **Analyst**: re-read specific items if you need the original text behind a cluster.

## How to run
From the workspace, run:

```bash
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/read_feed.py --window 24
```

Options: `--window <hours>` (default 24), `--limit <n>` (default 400).

## Output
```json
{"window_hours": 24, "count": 192, "items": [{"i": 0, "source": "...", "text": "...", "url": "...", "reliability": 0.9, "fetched_at": "..."}]}
```
Use the `i` index values as `item_refs` in your triage clusters.

## Notes
- Read-only. Does not write anything.
- If `count` is 0, the feed is stale — flag it to the CIO (a Data-Ops / ingestion problem), do not fabricate items.
