#!/usr/bin/env python3
"""
QC / Coverage-Gap-Check — run after each briefing.

Compares raw_items ingested in the briefing window against the triage
clusters. Items not referenced by any cluster are "uncovered". Among
those, keyword heuristics flag "big events" (IPO/S-1, funding rounds,
major launches, M&A, insider trades, earnings surprises, analyst rating
changes, C-suite changes) as coverage-bug Paperclip tickets.

Usage:
  python -m agents.coverage_qc [--run-id RUN_ID] [--dry-run]

Env (same as rest of pipeline):
  SUPABASE_URL, SUPABASE_KEY
  PAPERCLIP_API_URL, PAPERCLIP_API_KEY, PAPERCLIP_COMPANY_ID
  PAPERCLIP_AGENT_ID  (used as reporter; defaults to env var)
  DE_AGENT_ID         (assignee for bug tickets; optional)
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

FUND_DIR = os.environ.get("FUND_DIR", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, FUND_DIR)

from ingestion.db import client  # noqa: E402

# ---------------------------------------------------------------------------
# Big-event heuristics — keywords that indicate a material missed event
# ---------------------------------------------------------------------------
BIG_EVENT_PATTERNS = [
    # IPO / public market debut
    (r"\b(IPO|initial public offering|S-1|goes public|direct listing|SPAC|de-SPAC)\b",
     "IPO/S-1/listing", "high"),
    # Funding rounds — \$[\d,.]+[BMK]? matches $4B, $400M, $1.5B, $1,000
    (r"\b(Series [A-F]|seed round|raises \$[\d,.]+[BMKbmk]?|funding round"
     r"|valuation of \$[\d,.]+[BMKbmk]?|unicorn|raises [£€][\d,.]+[BMKbmk]?)\b",
     "funding", "high"),
    # Regulatory / antitrust
    (r"\b(FTC|DOJ|antitrust|SEC charges|CFPB|regulatory action|fine of \$[\d,]+)\b",
     "regulatory", "medium"),
    # Major product / model launches
    (r"\b(launches|announces|releases|unveils|introduces)\b.{0,80}"
     r"\b(model|agent|chip|product|platform|API|service)\b",
     "launch", "medium"),
    # Acquisition / merger
    (r"\b(acquires|acquisition|merger|takeover|buyout|LOI|definitive agreement)\b",
     "M&A", "high"),
    # Large insider trade (Form 4)
    (r"\b(Form 4|insider (buy|sell|purchase|sale)|open.market (purchase|sale))\b",
     "insider_trade", "medium"),
    # Earnings surprise
    (r"\b(beats? estimates?|misses? estimates?|earnings (beat|miss)|guidance (raised|lowered))\b",
     "earnings_surprise", "medium"),
    # Analyst rating changes — upgrades/downgrades/initiations/target changes (market-moving)
    (r"\b(initiates coverage|initiates at|upgrades? to|downgrades? to|reiterates? (buy|sell|hold)"
     r"|raises? (price target|pt|target price)|cuts? (price target|pt|target price)"
     r"|increases? (price target|target)|lowers? (price target|target)"
     r"|outperform|underperform|overweight|underweight)\b",
     "analyst_action", "medium"),
    # Executive departures / C-suite appointments (CEO/CTO/CFO changes are binary events)
    (r"\b(CEO|CFO|CTO|COO|president)\b.{0,60}"
     r"\b(resign\w*|retire\w*|depart\w*|step\w* down|appoint\w*|name\w*|hire\w*|join\w*)\b"
     r"|\b(appoint\w*|name\w*|hire\w*)\b.{0,60}\b(CEO|CFO|CTO|COO|president)\b"
     r"|\b(Exec Departure|Exec Appointment)\b",
     "exec_change", "high"),
    # Quarterly / annual results — catches "Q1 2026 revenue", "reports first quarter results",
    # "annual revenue of $X". Needed because TSM/ASML 6-Ks use plain reporting language
    # ("revenue of NT$839B, up 41.6% YoY") without "beats/misses estimates" phrasing.
    (r"\b(Q[1-4]\s+(?:fiscal\s+)?(?:20\d{2}\s+)?(?:results?|revenue|earnings|EPS)"
     r"|(?:first|second|third|fourth)\s+quarter\s+(?:results?|revenue|earnings|EPS|report\w*)"
     r"|(?:annual|full.year|full.?year)\s+(?:results?|revenue|earnings)"
     r"|reports?\s+(?:Q[1-4]|quarterly|annual)\s+(?:results?|revenue|earnings))\b",
     "quarterly_results", "high"),
    # Foreign issuer periodic filings (6-K / 20-F) — any item from a foreign issuer
    # (TSM, ASML, ARM) reporting quarterly revenue or an annual filing is high-signal.
    # Catches: "[EDGAR 6-K Foreign Issuer Report]" and "[EDGAR 20-F Foreign Annual Report]"
    (r"\[EDGAR (?:6-K|20-F) Foreign",
     "foreign_filing", "high"),
    # Share buyback / repurchase authorization — capital-allocation signal that boosts
    # EPS and signals management confidence. "$Xbn buyback" or "repurchase program" events
    # are filed via 8-K item 8.01 or press release and are frequently missed by triage.
    (r"\b(share\s+repurchase|stock\s+repurchase|buyback\s+program|repurchase\s+program"
     r"|authoriz\w+\s+(?:a\s+)?(?:new\s+)?\$[\d,.]+[BMKbmk]?\s+(?:share\s+)?(?:repurchase|buyback)"
     r"|\$[\d,.]+\s*(?:billion|million)\s+(?:share\s+)?(?:repurchase|buyback))\b",
     "buyback", "high"),
    # Special / increased dividend — material return-of-capital event signaling free-cash-flow
    # strength. Covers special dividends, dividend increases, and new dividend initiations.
    (r"\b(special\s+dividend|declares?\s+(?:a\s+)?(?:special|quarterly|annual)\s+dividend"
     r"|increases?\s+(?:its\s+)?(?:quarterly\s+)?dividend|dividend\s+(?:increase|raise|hike)"
     r"|initiates?\s+(?:a\s+)?dividend|dividend\s+of\s+\$[\d,.]+\s+per\s+share)\b",
     "dividend", "medium"),
    # Power / energy deals — S5 sector (VST, CEG, GEV, ETN). PPAs and nuclear capacity
    # announcements are direct AI-infrastructure revenue events (hyperscaler power contracts).
    # Grid/transformer orders (ETN, GEV) are capex-cycle signals for the AI build-out.
    (r"\b(power\s+purchase\s+agreement|PPA|offtake\s+agreement"
     r"|nuclear\s+(?:power|plant|reactor|capacity|energy|deal|agreement|restart|extension)"
     r"|data\s+cent(?:er|re)\s+power|clean\s+energy\s+(?:deal|agreement|contract)"
     r"|carbon.free\s+energy|24/7\s+(?:clean|carbon.free)\s+energy"
     r"|SMR|small\s+modular\s+reactor"
     r"|transformer\s+order|grid\s+(?:infrastructure|investment|contract|upgrade)"
     r"|electricity\s+(?:supply|contract|deal|agreement|capacity))\b",
     "energy_power_deal", "high"),
]

_compiled = [(re.compile(pat, re.IGNORECASE), label, prio)
             for pat, label, prio in BIG_EVENT_PATTERNS]


def classify_item(text: str) -> list[tuple[str, str]]:
    """Return list of (event_label, priority) for all matching patterns."""
    return [(label, prio) for rx, label, prio in _compiled if rx.search(text)]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_run(run_id: str | None) -> dict | None:
    t = client().table("briefing_runs")
    if run_id:
        data = t.select("*").eq("id", run_id).limit(1).execute().data
    else:
        data = t.select("*").eq("status", "done").order(
            "created_at", desc=True).limit(1).execute().data
    return data[0] if data else None


def get_raw_items(created_after: str) -> list[dict]:
    cutoff = created_after
    rows = (client().table("raw_items")
            .select("id,source,text,url,fetched_at,reliability")
            .gte("fetched_at", cutoff)
            .order("fetched_at", desc=True)
            .limit(600)
            .execute().data or [])
    return rows


# ---------------------------------------------------------------------------
# Paperclip ticket creation
# ---------------------------------------------------------------------------

def post_coverage_bug(item: dict, event_labels: list[str], priority: str,
                      dry_run: bool) -> str | None:
    api_url = os.environ.get("PAPERCLIP_API_URL", "").rstrip("/")
    company_id = os.environ.get("PAPERCLIP_COMPANY_ID", "")
    api_key = os.environ.get("PAPERCLIP_API_KEY", "")
    run_id = os.environ.get("PAPERCLIP_RUN_ID", "")
    de_agent_id = os.environ.get("DE_AGENT_ID", "78b79ccb-7011-4753-b282-584d6136bfb6")

    if not (api_url and company_id and api_key):
        print(f"[coverage_qc] SKIP ticket (no Paperclip env): {item.get('url', '')[:80]}",
              file=sys.stderr)
        return None

    labels_str = ", ".join(event_labels)
    source = item.get("source", "unknown")
    url = item.get("url", "")
    text_snippet = (item.get("text") or "")[:200].replace("\n", " ")

    title = f"📡 Coverage-Gap: {labels_str} — {source} [{text_snippet[:60]}…]"
    description = (
        f"**QC-Coverage-Gap-Check** flagged a big event not covered in the last briefing triage.\n\n"
        f"- **Event type(s):** {labels_str}\n"
        f"- **Source:** `{source}` (reliability={item.get('reliability', '?')})\n"
        f"- **URL:** {url}\n"
        f"- **Fetched at:** {item.get('fetched_at', '')}\n\n"
        f"**Snippet:**\n> {text_snippet}\n\n"
        f"**Action:** Investigate if this event should have been covered. "
        f"If yes, determine if the triage prompt/adapter needs adjustment."
    )

    payload = {
        "title": title,
        "description": description,
        "priority": priority,
        "assigneeAgentId": de_agent_id,
        "labels": ["coverage-gap"],
    }

    if dry_run:
        print(f"[DRY-RUN] would create ticket: {title[:100]}", file=sys.stderr)
        return "dry-run"

    import urllib.request
    req = urllib.request.Request(
        f"{api_url}/api/companies/{company_id}/issues",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Paperclip-Run-Id": run_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return body.get("identifier") or body.get("id")
    except Exception as e:
        print(f"[coverage_qc] ticket creation failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="QC coverage-gap check after briefing")
    ap.add_argument("--run-id", help="briefing_runs.id to check (default: latest done)")
    ap.add_argument("--dry-run", action="store_true", help="print gaps, don't create tickets")
    ap.add_argument("--min-priority", default="medium",
                    choices=["high", "medium"], help="only file tickets at this priority or higher")
    args = ap.parse_args()

    run = get_run(args.run_id)
    if not run:
        print("[coverage_qc] no done run found — nothing to check", file=sys.stderr)
        return

    rid = run["id"]
    window = run.get("window_hours", 24)
    run_created = run.get("created_at", "")
    cutoff = (datetime.fromisoformat(run_created.replace("Z", "+00:00"))
              - timedelta(hours=window)).isoformat() if run_created else (
              datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()

    # Build covered-text set from triage evidence (text[:300] strings stored per cluster).
    # item_refs are positional indices into the list passed to triage, NOT into the
    # freshly-fetched raw list — so index matching against re-fetched items is always wrong.
    # Evidence strings are the stable identity: match raw items by text prefix instead.
    triage = run.get("triage") or {}
    clusters = triage.get("clusters") if isinstance(triage, dict) else []
    covered_texts: set[str] = set()
    covered_urls: set[str] = set()
    if clusters:
        for cl in clusters:
            for ev in (cl.get("evidence") or []):
                if isinstance(ev, str) and ev:
                    covered_texts.add(ev[:300])
            # Also collect URLs from cluster items if stored
            for ref_text in (cl.get("evidence") or []):
                covered_texts.add((ref_text or "")[:300])

    # Fetch raw_items in window
    raw = get_raw_items(cutoff)
    total = len(raw)

    def _is_covered(item: dict) -> bool:
        t = (item.get("text") or "")[:300]
        if t and t in covered_texts:
            return True
        u = item.get("url") or ""
        return bool(u and u in covered_urls)

    covered_count = sum(1 for item in raw if _is_covered(item))

    print(f"[coverage_qc] run={rid[:8]} window={window}h items={total} "
          f"covered={covered_count}", file=sys.stderr)

    uncovered = [item for item in raw if not _is_covered(item)]

    # Classify uncovered items for big events
    PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
    min_p = PRIORITY_ORDER.get(args.min_priority, 1)

    tickets_filed = 0
    seen_urls: set[str] = set()

    for item in uncovered:
        text = item.get("text") or ""
        hits = classify_item(text)
        if not hits:
            continue
        # Take highest-priority hit
        hits_sorted = sorted(hits, key=lambda h: PRIORITY_ORDER.get(h[1], 9))
        top_labels = [h[0] for h in hits_sorted]
        top_prio = hits_sorted[0][1]

        if PRIORITY_ORDER.get(top_prio, 9) > min_p:
            continue

        url = item.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)

        ident = post_coverage_bug(item, top_labels, top_prio, args.dry_run)
        if ident:
            print(f"[coverage_qc] filed {ident}: {top_prio}/{top_labels} "
                  f"src={item.get('source')} url={url[:80]}", file=sys.stderr)
            tickets_filed += 1

    print(f"[coverage_qc] done — uncovered={len(uncovered)} big_events_filed={tickets_filed}",
          file=sys.stderr)
    # Summary JSON to stdout for callers
    print(json.dumps({
        "run_id": rid,
        "window_hours": window,
        "total_items": total,
        "uncovered_items": len(uncovered),
        "tickets_filed": tickets_filed,
    }))


if __name__ == "__main__":
    main()
