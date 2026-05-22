---
name: persist-run
description: >
  Create and update the briefing_runs row that carries pipeline state across stages
  (triage -> analyst -> thesis -> devil -> editor -> done). Use to start a run, save a
  stage's validated output to its column, and advance the status. State lives in the
  database; do not pass large JSON between issues — reference the run id.
---

# persist-run

Manages the `briefing_runs` state machine. Each stage saves its output to a column and advances `status` so the next stage can pick it up.

Columns by stage: `triage` (clusters), `analysis` (analyses), `theses`, `devils_advocate` (critiques), `briefing_md` (final markdown). Status flow: `triage → analyst → thesis → devil → editor → done` (or `error`).

## Start a run (CIO)
```bash
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/run_state.py create --window 24
# -> {"id": "<run-id>", "status": "triage"}
```

## Save a stage's output and advance status
Pipe the validated JSON to the script (use the matching field + next status):
```bash
echo "$TRIAGE_JSON"  | /srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/run_state.py set --id <run-id> --field triage          --status analyst
echo "$ANALYSIS_JSON"| ... run_state.py set --id <run-id> --field analysis        --status thesis
echo "$THESES_JSON"  | ... run_state.py set --id <run-id> --field theses          --status devil
echo "$DEVIL_JSON"   | ... run_state.py set --id <run-id> --field devils_advocate --status editor
echo "$BRIEFING_MD"  | ... run_state.py set --id <run-id> --field briefing_md      --status done
```
On failure, set `--field error --status error` with the message.

## Notes
- JSON fields expect valid JSON on stdin; `briefing_md`/`status`/`error` expect text.
- Validate stage output with the `validate-output` skill **before** persisting.
- Pair with `read-run` to load the prior stage's output.
