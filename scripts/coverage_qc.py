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
  python scripts/coverage_qc.py --run-id <ID>    # report + file tickets
  python scripts/coverage_qc.py --dry-run        # print gaps only, no tickets
  python scripts/coverage_qc.py                  # latest done run + tickets

Legacy flags from the old fund_skills implementation (--json, --open-tickets)
are silently ignored — agents/coverage_qc always outputs JSON and opens tickets
by default.
"""
import sys

# Strip legacy flags that were accepted by fund_skills/coverage_qc but are
# either defaults or redundant in agents/coverage_qc (--json always outputs
# JSON; --open-tickets is the default, suppressed only by --dry-run).
_LEGACY_FLAGS = {"--json", "--open-tickets"}
sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a not in _LEGACY_FLAGS]

from agents.coverage_qc import main as _main  # noqa: E402


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
