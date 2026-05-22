#!/usr/bin/env bash
# AI/Tech Fund — Ingestion-Wrapper für Cron. Absolute Pfade (Cron hat minimales Env).
set -uo pipefail
# Repo-Wurzel aus dem Skript-Pfad ableiten, statt sie hartzukodieren.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1
PYTHON="$REPO_DIR/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') ingest start ($REPO_DIR) ====="
"$PYTHON" -m ingestion.run_ingest
rc=$?
echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') ingest done (rc=$rc) ====="
if [ "$rc" -eq 0 ]; then
    echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') track-record refresh start ====="
    "$PYTHON" -c "
import sys; sys.path.insert(0,'.')
from agents.score_past_calls import write_track_record
print(write_track_record(), file=sys.stderr)
" || echo "WARN: track-record refresh failed (non-fatal)"
    echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') track-record refresh done ====="
    echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') sector-view refresh start ====="
    timeout 120 "$PYTHON" -m dashboard.build --gen-sector-view || echo "WARN: sector-view refresh failed/timed-out (non-fatal)"
    echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') sector-view refresh done ====="
    echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') dashboard rebuild start ====="
    "$PYTHON" -m dashboard.build || echo "WARN: dashboard rebuild failed (non-fatal)"
    echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') dashboard rebuild done ====="
fi
exit $rc
