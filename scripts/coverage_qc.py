#!/usr/bin/env python3
"""Post-briefing Coverage-QC entry point (HED-27).

Thin launcher around the canonical implementation in
`fund_skills/coverage_qc.py`. Keeping a single implementation avoids drift
between the skill package and an operational script.

The QC pass takes a briefing run, re-derives its raw_items window (24h before
the run by default, from `briefing_runs.window_hours`), compares the big events
in that window against what the briefing actually delivered
(`briefing_runs.briefing_md` + triage clusters), and reports/files every
uncovered big event (IPO/S-1, sizeable funding, major launch) as a
Coverage-Bug ticket — the automated successor to the manual HED-24 Exa miss.

Usage:
  python scripts/coverage_qc.py --run-id <ID>                 # report only
  python scripts/coverage_qc.py --run-id <ID> --open-tickets  # file bugs
  python scripts/coverage_qc.py --json                        # latest done run

All flags are forwarded to fund_skills/coverage_qc.py (run with --help).
Errors inside the QC pass are logged, not raised, so a post-run hook never
crashes the pipeline.
"""
import importlib.util
import sys
from pathlib import Path

FUND_DIR = Path(__file__).resolve().parent.parent
_IMPL = FUND_DIR / "fund_skills" / "coverage_qc.py"


def _load_impl():
    spec = importlib.util.spec_from_file_location("coverage_qc_impl", _IMPL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if not _IMPL.exists():
        print(f"error: implementation not found at {_IMPL}", file=sys.stderr)
        return 2
    try:
        _load_impl().main()
        return 0
    except SystemExit as ex:  # argparse / explicit exits propagate their code
        return int(ex.code or 0)
    except Exception as ex:  # noqa: BLE001 — never crash a post-run hook
        print(f"error: coverage_qc failed ({type(ex).__name__}: {ex})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
