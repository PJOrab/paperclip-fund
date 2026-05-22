#!/usr/bin/env python3
"""
Thesis track-record scorer — compares past investment theses against actual
price action and outputs a conviction hit-rate table.

For each completed briefing_run with theses, and for each thesis with
a clear directional view (long/short) on a listed ticker, we:
  1. Fetch the price at thesis creation date (run's created_at).
  2. Fetch the current price (or price at horizon end).
  3. Compute the return and whether the direction call was correct.
  4. Score conviction calibration: high-conviction correct calls and
     low-conviction incorrect calls both score well.

Output: JSON track record + Markdown table (stdout).

Usage:
  python -m agents.score_past_calls [--days N] [--output json|markdown|both]
  python -m agents.score_past_calls --since 2026-05-01

Price data: Yahoo Finance yfinance library (already a dep via YahooFinanceTicker adapter).
Falls back to yfinance download if not importable directly.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

FUND_DIR = os.environ.get("FUND_DIR", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, FUND_DIR)

from ingestion.db import client  # noqa: E402


# ---------------------------------------------------------------------------
# Price fetching (yfinance)
# ---------------------------------------------------------------------------

def _get_yfinance():
    try:
        import yfinance as yf
        return yf
    except ImportError:
        return None


def fetch_price_on_date(ticker: str, date_str: str, yf) -> float | None:
    """Return closing price on or just before date_str (YYYY-MM-DD)."""
    if yf is None:
        return None
    try:
        target = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        # Download a ±5 day window to handle weekends/holidays
        start = (target - timedelta(days=7)).isoformat()
        end = (target + timedelta(days=1)).isoformat()
        hist = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if hist.empty:
            return None
        # Take the last row at or before target date
        hist.index = hist.index.date  # type: ignore
        valid = hist[hist.index <= target]
        if valid.empty:
            return None
        close = valid["Close"].iloc[-1]
        return float(close.item() if hasattr(close, "item") else close)
    except Exception as e:
        print(f"[score] price fetch error {ticker}@{date_str}: {e}", file=sys.stderr)
        return None


def fetch_current_price(ticker: str, yf) -> float | None:
    if yf is None:
        return None
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = getattr(info, "last_price", None)
        if price is None:
            hist = yf.download(ticker, period="2d", progress=False, auto_adjust=True)
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        return float(price) if price else None
    except Exception as e:
        print(f"[score] current price error {ticker}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Horizon → approximate days
# ---------------------------------------------------------------------------
HORIZON_DAYS = {"days": 7, "weeks": 30, "quarters": 90}


def horizon_elapsed(created_at: str, horizon: str) -> bool:
    """True if enough time has passed to judge the thesis."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - created).days
        return elapsed >= HORIZON_DAYS.get(horizon, 30)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_thesis(thesis: dict, entry_price: float | None, current_price: float | None,
                 horizon: str, conviction: float) -> dict:
    """Return a score record for one (thesis, ticker) pair."""
    direction = thesis.get("direction", "long")

    result: dict = {
        "entry_price": entry_price,
        "current_price": current_price,
        "return_pct": None,
        "direction_correct": None,
        "conviction": conviction,
        "horizon": horizon,
        "scored": False,
    }

    if entry_price is None or current_price is None or entry_price == 0:
        return result

    ret = (current_price - entry_price) / entry_price * 100
    result["return_pct"] = round(ret, 2)
    result["scored"] = True

    if direction == "long":
        result["direction_correct"] = ret > 0
    elif direction == "short":
        result["direction_correct"] = ret < 0
    else:
        # pair trade — skip direction scoring
        result["direction_correct"] = None

    # Conviction calibration score:
    # If correct: score = conviction (high conviction correct → higher score)
    # If wrong:   score = 1 - conviction (low conviction wrong → higher score)
    if result["direction_correct"] is True:
        result["cal_score"] = round(conviction, 3)
    elif result["direction_correct"] is False:
        result["cal_score"] = round(1.0 - conviction, 3)
    else:
        result["cal_score"] = 0.5  # neutral for pair

    return result


# ---------------------------------------------------------------------------
# DB: load past runs with theses
# ---------------------------------------------------------------------------

def load_runs(since_date: str | None, days: int) -> list[dict]:
    t = client().table("briefing_runs")
    q = t.select("id,created_at,theses").eq("status", "done").not_.is_("theses", "null")
    if since_date:
        q = q.gte("created_at", since_date)
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q = q.gte("created_at", cutoff)
    return q.order("created_at", desc=False).limit(200).execute().data or []


# ---------------------------------------------------------------------------
# Main scoring loop
# ---------------------------------------------------------------------------

def run_scoring(runs: list[dict], yf, include_pending: bool = False) -> list[dict]:
    records = []
    seen: set[str] = set()  # deduplicate (run_id, thesis_id, ticker)

    for run in runs:
        rid = run["id"]
        created_at = run.get("created_at", "")
        theses_blob = run.get("theses") or {}
        theses = (theses_blob.get("theses", []) if isinstance(theses_blob, dict)
                  else theses_blob) if theses_blob else []

        for th in theses:
            th_id = th.get("id", "?")
            direction = th.get("direction", "long")
            conviction = float(th.get("conviction", 0.3))
            horizon = th.get("horizon", "weeks")
            tickers = th.get("tickers") or []
            thesis_txt = th.get("thesis", "")[:120]

            if not tickers:
                continue

            elapsed = horizon_elapsed(created_at, horizon)
            if not elapsed and not include_pending:
                print(f"[score] skip {th_id} — horizon not elapsed yet", file=sys.stderr)
                continue

            for ticker in tickers[:3]:  # cap at 3 tickers per thesis
                key = f"{rid}:{th_id}:{ticker}"
                if key in seen:
                    continue
                seen.add(key)

                entry = fetch_price_on_date(ticker, created_at, yf)
                current = fetch_current_price(ticker, yf)
                scored = score_thesis(th, entry, current, horizon, conviction)

                rec = {
                    "run_id": rid[:8],
                    "created_at": created_at[:10],
                    "thesis_id": th_id,
                    "ticker": ticker,
                    "direction": direction,
                    "conviction": conviction,
                    "horizon": horizon,
                    "thesis": thesis_txt,
                    **scored,
                }
                # Carry forward structured fields added in later prompt versions
                if th.get("exit_trigger"):
                    rec["exit_trigger"] = th["exit_trigger"]
                if th.get("scenarios"):
                    rec["scenarios"] = th["scenarios"]
                records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def as_markdown(records: list[dict]) -> str:
    scored = [r for r in records if r.get("scored")]
    unscored = [r for r in records if not r.get("scored")]

    if not records:
        return "## Track Record\n\n*No theses to score yet.*\n"

    lines = ["## Thesis Track Record\n"]

    if scored:
        correct = [r for r in scored if r.get("direction_correct") is True]
        incorrect = [r for r in scored if r.get("direction_correct") is False]
        directional = [r for r in scored if r.get("direction_correct") is not None]
        hit_rate = len(correct) / len(directional) * 100 if directional else 0
        avg_cal = sum(r.get("cal_score", 0.5) for r in directional) / len(directional) if directional else 0
        avg_ret_long = (sum(r["return_pct"] for r in scored if r["direction"] == "long" and r["return_pct"] is not None)
                        / max(1, sum(1 for r in scored if r["direction"] == "long" and r["return_pct"] is not None)))

        lines.append(f"**Scored calls:** {len(scored)} ({len(directional)} directional) | "
                     f"**Hit rate:** {hit_rate:.0f}% ({len(correct)}/{len(directional)}) | "
                     f"**Avg calibration score:** {avg_cal:.2f} | "
                     f"**Avg long return:** {avg_ret_long:+.1f}%\n")
        lines.append("")

        lines.append("| Date | Thesis | Ticker | Dir | Conv | Entry | Now | Ret% | ✓ | Cal |")
        lines.append("|------|--------|--------|-----|------|-------|-----|------|---|-----|")
        for r in sorted(scored, key=lambda x: x["created_at"]):
            dc = r.get("direction_correct")
            chk = "✅" if dc is True else ("❌" if dc is False else "—")
            entry_s = f"${r['entry_price']:.2f}" if r["entry_price"] else "—"
            cur_s = f"${r['current_price']:.2f}" if r["current_price"] else "—"
            ret_s = f"{r['return_pct']:+.1f}%" if r["return_pct"] is not None else "—"
            thesis_s = (r["thesis"][:50] + "…") if len(r["thesis"]) > 50 else r["thesis"]
            lines.append(
                f"| {r['created_at']} | {thesis_s} | {r['ticker']} | {r['direction']} "
                f"| {r['conviction']:.2f} | {entry_s} | {cur_s} | {ret_s} | {chk} | {r.get('cal_score', '—'):.2f} |"
            )
        lines.append("")

    if unscored:
        lines.append(f"\n*{len(unscored)} call(s) could not be priced (no yfinance data or no ticker).*\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dashboard-compatible JSON writer
# ---------------------------------------------------------------------------

def build_dashboard_payload(records: list[dict], runs_meta: list[dict] | None = None) -> dict:
    """Convert flat scoring records into the track_record.json structure the dashboard expects.

    Dashboard schema:
      aggregate: {hit_rate, scored, total, too_early, calibration_bias}
      theses: [{date, label, tickers, direction, conviction, baseline_price,
                current_price, move_pct, verdict, devil}]
      calibration_buckets: [{label, accuracy, n}]
      earliest_score_date: str|None
    """
    from collections import defaultdict

    # Group records by (run_id, thesis_id) → thesis-level view
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        groups[(r["run_id"], r["thesis_id"])].append(r)

    # Build devil verdict lookup from runs_meta if available
    devil_by_run_thesis: dict[tuple, dict] = {}
    for run in (runs_meta or []):
        rid = run["id"][:8]
        da = (run.get("devils_advocate") or {})
        for c in (da.get("critiques") if isinstance(da, dict) else []) or []:
            devil_by_run_thesis[(rid, c.get("id", ""))] = {
                "verdict": c.get("verdict", ""),
                "note": (c.get("strongest_counter") or "")[:120],
            }

    theses_out = []
    for (rid, th_id), recs in groups.items():
        # Use the first record for metadata, aggregate prices across tickers
        first = recs[0]
        # If any ticker is scored, use the primary ticker's data
        scored_recs = [r for r in recs if r.get("scored")]
        primary = scored_recs[0] if scored_recs else first

        # verdict: hit/miss/neutral/too_early
        if not primary.get("scored"):
            verdict = "too_early"
        elif primary.get("direction_correct") is True:
            verdict = "hit"
        elif primary.get("direction_correct") is False:
            verdict = "miss"
        else:
            verdict = "neutral"

        entry_out: dict = {
            "date": first["created_at"],
            "label": first["thesis"][:80],
            "tickers": list({r["ticker"] for r in recs}),
            "direction": first["direction"],
            "conviction": first["conviction"],
            "baseline_price": primary.get("entry_price"),
            "current_price": primary.get("current_price"),
            "move_pct": primary.get("return_pct"),
            "verdict": verdict,
            "devil": devil_by_run_thesis.get((rid, th_id)),
        }
        # Forward structured fields from newer prompt versions when present
        if first.get("exit_trigger"):
            entry_out["exit_trigger"] = first["exit_trigger"]
        if first.get("scenarios"):
            entry_out["scenarios"] = first["scenarios"]
        theses_out.append(entry_out)

    # Aggregate stats
    total = len(theses_out)
    scored = [t for t in theses_out if t["verdict"] != "too_early"]
    too_early = total - len(scored)
    hits = [t for t in scored if t["verdict"] == "hit"]
    directional = [t for t in scored if t["verdict"] in ("hit", "miss")]
    hit_rate = len(hits) / len(directional) if directional else None

    # Calibration bias: mean(conviction) for hits - mean(conviction) for misses
    hit_convs = [t["conviction"] for t in scored if t["verdict"] == "hit"]
    miss_convs = [t["conviction"] for t in scored if t["verdict"] == "miss"]
    cal_bias = None
    if hit_convs and miss_convs:
        cal_bias = round(sum(hit_convs) / len(hit_convs) - sum(miss_convs) / len(miss_convs), 3)

    # Calibration buckets: group by conviction tier
    buckets: dict[str, dict] = {
        "0.40-0.49": {"correct": 0, "total": 0},
        "0.50-0.59": {"correct": 0, "total": 0},
        "0.60-0.74": {"correct": 0, "total": 0},
        "0.75+":     {"correct": 0, "total": 0},
    }
    for t in directional:
        c = t["conviction"]
        key = ("0.40-0.49" if c < 0.50 else
               "0.50-0.59" if c < 0.60 else
               "0.60-0.74" if c < 0.75 else "0.75+")
        buckets[key]["total"] += 1
        if t["verdict"] == "hit":
            buckets[key]["correct"] += 1
    cal_buckets = [
        {"label": k, "accuracy": round(v["correct"] / v["total"], 2) if v["total"] else None, "n": v["total"]}
        for k, v in buckets.items()
    ]

    date_list = sorted({t["date"] for t in theses_out if t["date"]})
    earliest = date_list[0] if date_list else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aggregate": {
            "total": total,
            "scored": len(scored),
            "too_early": too_early,
            "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
            "calibration_bias": cal_bias,
        },
        "theses": sorted(theses_out, key=lambda t: t.get("date") or "", reverse=True),
        "calibration_buckets": cal_buckets,
        "earliest_score_date": earliest,
    }


def write_track_record(days: int = 60, out_path: str | None = None) -> str:
    """Score past theses and write dashboard/track_record.json. Returns summary string."""
    yf = _get_yfinance()
    runs = load_runs(None, days)
    # include_pending=True: fetch current prices even for too-early theses so the
    # dashboard shows day-1/week-1 price moves while the horizon is still running.
    records = run_scoring(runs, yf, include_pending=True)

    # Load runs again with devils_advocate for devil verdict enrichment
    try:
        runs_with_devil = (
            client().table("briefing_runs")
            .select("id,devils_advocate")
            .eq("status", "done")
            .not_.is_("theses", "null")
            .order("created_at", desc=False)
            .limit(200)
            .execute().data or []
        )
    except Exception:
        runs_with_devil = []

    payload = build_dashboard_payload(records, runs_with_devil)

    if out_path is None:
        out_path = str(Path(__file__).resolve().parent.parent / "dashboard" / "track_record.json")
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    agg = payload["aggregate"]
    return (f"track_record written: {agg['total']} theses, {agg['scored']} scored, "
            f"hit_rate={agg['hit_rate']}, cal_bias={agg['calibration_bias']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Score past investment theses vs price action")
    ap.add_argument("--days", type=int, default=30, help="Look back N days (default 30)")
    ap.add_argument("--since", help="ISO date string to look back from (overrides --days)")
    ap.add_argument("--output", choices=["json", "markdown", "both"], default="both")
    ap.add_argument("--include-pending", action="store_true",
                    help="Score theses even if horizon hasn't elapsed")
    args = ap.parse_args()

    yf = _get_yfinance()
    if yf is None:
        print("[score] WARNING: yfinance not available — install with: pip install yfinance",
              file=sys.stderr)

    runs = load_runs(args.since, args.days)
    print(f"[score] loaded {len(runs)} completed runs", file=sys.stderr)

    records = run_scoring(runs, yf, include_pending=args.include_pending)
    scored_n = sum(1 for r in records if r.get("scored"))
    print(f"[score] {len(records)} thesis-ticker pairs, {scored_n} priced", file=sys.stderr)

    if args.output in ("json", "both"):
        print(json.dumps(records, ensure_ascii=False, indent=2))

    if args.output in ("markdown", "both"):
        print(as_markdown(records), file=sys.stderr if args.output == "both" else sys.stdout)


if __name__ == "__main__":
    main()
