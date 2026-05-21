---
name: validate-output
description: >
  Validate a pipeline stage's JSON against its strict schema (triage | analyst | thesis |
  devil) before persisting it. Use at the end of every reasoning stage to guarantee the
  next stage receives well-formed input. Exit 0 = valid; non-zero lists the defects.
---

# validate-output

Structural check of a stage's JSON. Run it on your output **before** calling `persist-run`. If it fails, fix the defects and re-validate — never persist invalid output.

## How to run
Pipe your JSON in and name the schema:
```bash
echo "$YOUR_JSON" | /srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/validate_output.py --schema triage
```
Schemas: `triage` | `analyst` | `thesis` | `devil`.

## Output
- Valid: `{"valid": true}` (exit 0)
- Invalid: `{"valid": false, "errors": ["clusters[2] missing 'why'", ...]}` (exit 1)

## What each schema checks
- **triage**: `clusters[]` with title, tickers, category (enum), why, importance (1–5).
- **analyst**: `analyses[]` with read (bullish/bearish/mixed), magnitude, horizon, key_facts, key_uncertainty.
- **thesis**: `theses[]` with id, direction (long/short/pair), bull_case, bear_case, catalysts, conviction (0–1).
- **devil**: `critiques[]` with id, strongest_counter, already_priced_in, falsification, blind_spot, verdict (agree/caution/reject).
