#!/usr/bin/env bash
# AI/Tech Fund — Ingestion-Wrapper für Cron. Absolute Pfade (Cron hat minimales Env).
set -uo pipefail
cd /root/ai-tech-fund || exit 1
echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') ingest start ====="
/root/ai-tech-fund/venv/bin/python -m ingestion.run_ingest
rc=$?
echo "===== $(date -u +'%Y-%m-%dT%H:%M:%SZ') ingest done (rc=$rc) ====="
exit $rc
