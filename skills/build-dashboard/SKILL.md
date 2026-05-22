---
name: build-dashboard
description: >
  Rebuild the static fund dashboard (workflow diagram, feed stats, and the latest
  briefing) served at hedgingalpha.com/fund. Use after a briefing is persisted, or when
  the CEO asks to refresh the dashboard. Reads Supabase server-side; no secrets reach
  the browser.
---

# build-dashboard

Regenerates the static dashboard HTML from the current Supabase data (feed + latest `briefing_runs`).

## How to run
```bash
cd /srv/ai-tech-fund && /srv/ai-tech-fund/venv/bin/python -m dashboard.build
```
Add `--stdout` to print the HTML instead of writing the file (for a dry check).

## Notes
- Output is written to the dashboard's configured path and served by nginx.
- A cron also rebuilds it periodically; run this when you want an immediate refresh after delivering a briefing.
- Read-only against Supabase; safe to run anytime.
