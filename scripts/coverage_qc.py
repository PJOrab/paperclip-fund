#!/usr/bin/env python3
"""Post-briefing Coverage-QC entry point (HED-27).

Thin launcher around the canonical implementation in `agents/coverage_qc.py`.
Keeping a single implementation avoids drift between the operational script
and the in-pipeline call in stage_editor().

The QC pass takes a briefing run, re-derives its raw_items window (24h before
the run by default, from `briefing_runs.window_hours`), compares the big events
in that window against what the briefing actually delivered
(`briefing_runs.briefing_md` + triage clusters), and reports/files every
uncovered big event as a Coverage-Bug ticket.

Usage:
  python scripts/coverage_qc.py --run-id <ID>                 # report only
  python scripts/coverage_qc.py --run-id <ID> --open-tickets  # file bugs
  python scripts/coverage_qc.py --json                        # latest done run

All flags are forwarded to agents/coverage_qc.py (run with --help).
Errors inside the QC pass are logged, not raised, so a post-run hook never
crashes the pipeline.
"""
import sys

from agents.coverage_qc import main as _main


def main() -> int:
    try:
        _main()
        return 0
    except SystemExit as ex:
        return int(ex.code or 0)
    except Exception as ex:  # noqa: BLE001 — never crash a post-run hook
        print(f"error: coverage_qc failed ({type(ex).__name__}: {ex})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
