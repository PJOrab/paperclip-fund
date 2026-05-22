---
name: read-run
description: >
  Load a briefing_runs row (or one field of it) so a stage can read the prior stage's
  output — e.g. the Analyst reads the triage clusters, the Devil's Advocate reads the
  theses. Use at the start of every stage after Triage to get your input.
---

# read-run

Reads the pipeline state for a run. Use it to fetch exactly the input your stage needs.

## Get the whole latest run, or a specific run by id
```bash
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/run_state.py get
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/run_state.py get --id <run-id>
```

## Get the latest run in a given status (handy for "the run waiting for me")
```bash
... run_state.py get --status analyst   # the run waiting for the Analyst
```

## Get just one field (your input)
```bash
... run_state.py get --id <run-id> --field triage           # Analyst input
... run_state.py get --id <run-id> --field analysis         # Strategist input
... run_state.py get --id <run-id> --field theses           # Devil's Advocate input
... run_state.py get --id <run-id> --field devils_advocate  # Editor input (with theses + triage)
```

## Notes
- Read-only. Prints JSON (or text for `briefing_md`/`status`/`error`), or `null` if not found.
- Pair with `persist-run` to save your own stage's output.
