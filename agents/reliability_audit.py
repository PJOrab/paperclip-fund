#!/usr/bin/env python3
"""
Per-source reliability audit — computes actual triage-inclusion rates from
recent briefing_runs and compares them to the hardcoded scores in watchlist.py.

A source configured with rel=0.80 that rarely makes triage clusters is
misleading: the model is told to trust it but the signal is low. This script
surfaces the gap so reliability scores can be tuned to reality.

Usage:
  python -m agents.reliability_audit [--runs N] [--patch]

  --runs N   : use last N done briefing_runs (default 20)
  --patch    : update watchlist.py SOURCE_RELIABILITY with smoothed scores
               (only adjusts sources with ≥5 ingestion events, to avoid noise)

Output:
  Markdown table to stdout + JSON to stderr
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

FUND_DIR = os.environ.get("FUND_DIR", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, FUND_DIR)

from ingestion.db import client  # noqa: E402
from ingestion.watchlist import SOURCE_RELIABILITY  # noqa: E402


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def load_done_runs(n: int) -> list[dict]:
    return (client().table("briefing_runs")
            .select("id,created_at,triage,window_hours")
            .eq("status", "done")
            .not_.is_("triage", "null")
            .order("created_at", desc=True)
            .limit(n)
            .execute().data or [])


def load_raw_items_in_window(run_created_at: str, window_hours: int) -> list[dict]:
    from datetime import timedelta
    cutoff = (datetime.fromisoformat(run_created_at.replace("Z", "+00:00"))
              - timedelta(hours=window_hours)).isoformat()
    return (client().table("raw_items")
            .select("id,source,fetched_at")
            .gte("fetched_at", cutoff)
            .lte("fetched_at", run_created_at)
            .limit(800)
            .execute().data or [])


# ---------------------------------------------------------------------------
# Compute inclusion rates
# ---------------------------------------------------------------------------

def audit(runs: list[dict]) -> dict[str, dict]:
    """
    For each source: count total ingested items and items that appeared in
    at least one triage cluster's item_refs across all analyzed runs.

    Returns {source: {"ingested": int, "in_cluster": int, "runs": int}}
    """
    stats: dict[str, dict] = defaultdict(lambda: {"ingested": 0, "in_cluster": 0, "runs": 0})

    for run in runs:
        created_at = run.get("created_at", "")
        window = run.get("window_hours", 24)
        triage = run.get("triage") or {}
        clusters = triage.get("clusters", []) if isinstance(triage, dict) else []

        # Collect referenced item positions
        referenced_indices: set[int] = set()
        for cl in clusters:
            for idx in (cl.get("item_refs") or []):
                if isinstance(idx, int):
                    referenced_indices.add(idx)

        # Load raw items for this run's window (by position order = fetched_at desc)
        items = load_raw_items_in_window(created_at, window)
        if not items:
            continue

        for i, item in enumerate(items):
            src = item.get("source") or "unknown"
            stats[src]["ingested"] += 1
            stats[src]["runs"] = stats[src].get("runs", 0)
            if i in referenced_indices:
                stats[src]["in_cluster"] += 1

        # Mark which sources appeared in this run
        seen_sources = {(item.get("source") or "unknown") for item in items}
        for src in seen_sources:
            stats[src]["runs"] = stats[src].get("runs", 0) + 1

    return dict(stats)


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def as_markdown(stats: dict[str, dict]) -> str:
    configured = SOURCE_RELIABILITY
    rows = []
    for src, s in sorted(stats.items(), key=lambda x: -x[1]["ingested"]):
        total = s["ingested"]
        hits = s["in_cluster"]
        rate = hits / total if total else 0.0
        cfg = configured.get(src)
        delta = (rate - cfg) if cfg is not None else None
        delta_s = f"{delta:+.2f}" if delta is not None else "—"
        cfg_s = f"{cfg:.2f}" if cfg is not None else "—"
        signal = "🔴" if (cfg is not None and delta is not None and delta < -0.15) else \
                 "🟡" if (cfg is not None and delta is not None and delta < -0.05) else "🟢"
        rows.append((src, total, hits, f"{rate:.2f}", cfg_s, delta_s, signal))

    lines = [
        "## Per-Source Reliability Audit\n",
        "| Source | Ingested | In-Cluster | Actual Rate | Configured | Delta | Signal |",
        "|--------|----------|------------|-------------|------------|-------|--------|",
    ]
    for src, total, hits, rate, cfg, delta, signal in rows:
        lines.append(f"| `{src}` | {total} | {hits} | {rate} | {cfg} | {delta} | {signal} |")

    lines.append("\n🔴 = miscalibrated high (configured >> actual), 🟡 = slightly high, 🟢 = OK\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Patch watchlist.py with smoothed scores
# ---------------------------------------------------------------------------

MIN_SAMPLES = 5   # don't patch sources with fewer ingested items
SMOOTHING = 0.3   # blend: new = 0.7 * actual + 0.3 * configured


def compute_patched(stats: dict[str, dict]) -> dict[str, float]:
    configured = dict(SOURCE_RELIABILITY)
    patched = {}
    for src, s in stats.items():
        total = s["ingested"]
        if total < MIN_SAMPLES:
            continue
        actual = s["in_cluster"] / total
        cfg = configured.get(src, actual)
        # Never go below 0.10 or above 0.95
        smoothed = max(0.10, min(0.95, round(SMOOTHING * cfg + (1 - SMOOTHING) * actual, 2)))
        if abs(smoothed - cfg) >= 0.03:  # only patch if meaningful change
            patched[src] = smoothed
    return patched


def patch_watchlist(patched: dict[str, float]) -> None:
    watchlist_path = Path(FUND_DIR) / "ingestion" / "watchlist.py"
    text = watchlist_path.read_text()
    for src, new_score in patched.items():
        # Find the source key in SOURCE_RELIABILITY and update its value
        pattern = rf'("{re.escape(src)}"\s*:\s*)([\d.]+)'
        replacement = rf'\g<1>{new_score}'
        new_text = re.sub(pattern, replacement, text)
        if new_text != text:
            text = new_text
            print(f"[patch] {src}: → {new_score}", file=sys.stderr)
        else:
            print(f"[patch] {src}: key not found in SOURCE_RELIABILITY, skipping", file=sys.stderr)
    watchlist_path.write_text(text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Audit per-source reliability vs. triage inclusion")
    ap.add_argument("--runs", type=int, default=20, help="Last N done runs to analyze")
    ap.add_argument("--patch", action="store_true",
                    help="Patch watchlist.py SOURCE_RELIABILITY with smoothed scores")
    args = ap.parse_args()

    runs = load_done_runs(args.runs)
    print(f"[audit] loaded {len(runs)} done runs", file=sys.stderr)
    if not runs:
        print("No done runs found — run a briefing first.", file=sys.stdout)
        return

    stats = audit(runs)
    print(f"[audit] {len(stats)} sources analyzed", file=sys.stderr)

    # JSON to stderr, markdown to stdout
    print(json.dumps(stats, ensure_ascii=False, indent=2), file=sys.stderr)
    print(as_markdown(stats))

    if args.patch:
        patched = compute_patched(stats)
        if patched:
            patch_watchlist(patched)
            print(f"\n[patch] Updated {len(patched)} reliability scores in watchlist.py",
                  file=sys.stderr)
        else:
            print("[patch] No scores needed updating (all within threshold)", file=sys.stderr)


if __name__ == "__main__":
    main()
