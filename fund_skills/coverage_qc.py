#!/usr/bin/env python3
"""coverage-qc: post-briefing quality gate — did the delivered briefing miss a
big story that was sitting in the feed?

For a given briefing run it (1) re-derives the run's raw_items window, (2) detects
"big events" in that window with conservative high-signal heuristics
(IPO / S-1 registrations, sizeable fundings, major launches), (3) checks each
against what the briefing actually delivered (triage cluster titles/why/tickers
+ briefing_md), and (4) reports every uncovered big event as a coverage gap.

With --open-tickets it files one high-priority Coverage-Bug issue per distinct
gap, assigned to the Data-Engineer, so a miss like the SpaceX S-1 (HED-12)
becomes a tracked bug instead of being silently dropped. Results are persisted
to the additive `coverage_qc` table for audit and cross-run dedup.

Usage:
  python fund_skills/coverage_qc.py [--run-id ID] [--open-tickets] [--json]
  # default: latest run with status=done, report only (no tickets)
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FUND_DIR = os.environ.get("FUND_DIR", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, FUND_DIR)

from dotenv import dotenv_values  # noqa: E402

from ingestion.db import client  # noqa: E402
from ingestion.watchlist import (  # noqa: E402
    NOTABLE_PRIVATE_PLAYERS,
    TICKERS,
    X_ACCOUNTS_AITECH,
)

# Merge .env file with os.environ so Paperclip-injected vars (PAPERCLIP_API_KEY,
# PAPERCLIP_COMPANY_ID, etc.) work both in the production SSH context and locally.
CFG = {**dotenv_values(str(Path(FUND_DIR) / ".env"), interpolate=False), **os.environ}

# --- entity surface forms: ticker/canonical -> spellings seen in the wild -----
# Coverage matching is symmetric over these forms, so "Nvidia" in a raw_item and
# "NVDA" in a triage cluster count as the same entity.
TICKER_ALIASES = {
    "NVDA": ["nvidia"], "AMD": ["amd", "advanced micro"], "TSM": ["tsmc", "taiwan semiconductor"],
    "ASML": ["asml"], "AVGO": ["broadcom"], "MU": ["micron"], "ARM": ["arm holdings", "arm ltd"],
    "SMCI": ["supermicro", "super micro"], "QCOM": ["qualcomm"], "MRVL": ["marvell"],
    "INTC": ["intel"], "ANET": ["arista"], "VRT": ["vertiv"], "DELL": ["dell"],
    "MSFT": ["microsoft"], "GOOGL": ["google", "alphabet"], "AMZN": ["amazon", "aws"],
    "META": ["meta", "facebook", "instagram"], "AAPL": ["apple"], "PLTR": ["palantir"],
    "ORCL": ["oracle"], "NOW": ["servicenow"], "CRM": ["salesforce"], "SNOW": ["snowflake"],
    "CRWD": ["crowdstrike"], "ADBE": ["adobe"],
}


def _entity_forms():
    """canonical label -> {"tickers": [...], "names": [...]}.

    Bare ticker symbols (NOW, MU, ARM, INTC...) are matched only as UPPERCASE
    standalone tokens in the original text — matching them lowercased as
    substrings produced false hits ("intel" inside "intelligence", "now" inside
    "is now a"). Company names are matched lowercased on word boundaries.
    """
    forms = {}
    for t in TICKERS:
        forms[t] = {"tickers": [t], "names": list(TICKER_ALIASES.get(t, []))}
    for p in NOTABLE_PRIVATE_PLAYERS:
        forms.setdefault(p, {"tickers": [], "names": []})["names"].append(p.lower())
    for name in X_ACCOUNTS_AITECH:
        forms.setdefault(name, {"tickers": [], "names": []})["names"].append(name.lower())
    # precompile a word-boundary regex per entity
    compiled = {}
    for label, d in forms.items():
        pats = [re.escape(n) for n in d["names"]]
        tick = [re.escape(s) for s in d["tickers"]]
        name_re = re.compile(r"\b(?:%s)\b" % "|".join(pats), re.IGNORECASE) if pats else None
        tick_re = re.compile(r"\b(?:%s)\b" % "|".join(tick)) if tick else None
        compiled[label] = (name_re, tick_re)
    return compiled


ENTITY_FORMS = _entity_forms()

# --- big-event heuristics (conservative; require strong signals) --------------
IPO_RE = re.compile(
    r"\b(s-1(?:/a)?|f-1(?:/a)?|424b\d?|files? (?:for|to go) (?:an? )?ipo|ipo filing"
    r"|registration statement|initial public offering|to go public|direct listing"
    r"|confidentially filed)\b", re.IGNORECASE)
# Funding: require an amount tied to a raise verb / round / valuation — not just a
# stray dollar figure (which appears in chatty tweets and partnership "deals").
_AMT = r"\$\s?\d[\d.,]*\s?(?:b|bn|billion|m|mn|million)?"
FUND_RES = [
    re.compile(rf"\b(raises?|raised|secures?|secured|closes?|closed|nets?|lands?|bags?"
               rf"|to raise|in)\s+(?:more than\s+|over\s+|nearly\s+|about\s+|~)?{_AMT}",
               re.IGNORECASE),
    re.compile(rf"{_AMT}\s+(?:\w+\s+){{0,3}}\b(round|series\s+[a-k]|funding|raise|financing"
               rf"|investment)\b", re.IGNORECASE),
    re.compile(rf"\b(series\s+[a-k])\b(?:\s+\w+){{0,4}}\s+{_AMT}", re.IGNORECASE),
    re.compile(rf"\b(valued at|valuation of)\s+(?:about\s+|~|over\s+)?{_AMT}", re.IGNORECASE),
]
LAUNCH_RE = re.compile(
    r"\b(launch(?:es|ed|ing)?|unveil(?:s|ed)?|introduc(?:es|ed|ing)|releas(?:es|ed|ing)"
    r"|debut(?:s|ed)?|roll(?:s|ed) out|now (?:generally )?available"
    r"|general availability|ships?)\b", re.IGNORECASE)
LAUNCH_OBJ_RE = re.compile(
    r"\b(model|models|chip|gpu|accelerator|platform|app|api|sdk|gpt|llm|agent)\b",
    re.IGNORECASE)
# Earnings / IR boilerplate that should never count as a "launch".
NOT_LAUNCH_RE = re.compile(
    r"\b(earnings|webinar|conference call|dividend|fiscal|quarter|release date"
    r"|annual meeting|investor (?:day|relations)|10-?[kq]\b)\b", re.IGNORECASE)

# Funding floor (USD millions): per HED-72 only rounds/valuations >= $100M count
# as a "big event". A round we cannot size (no parseable amount) is kept rather
# than dropped, so coverage stays conservative.
MIN_FUNDING_M = 100.0
_AMT_PARSE_RE = re.compile(
    r"\$\s?(\d[\d.,]*)\s*(billion|bn|million|mn|thousand|b|m|k)?\b", re.IGNORECASE)


def max_amount_m(text):
    """Largest dollar amount in `text`, normalized to USD millions. Returns None
    if no amount can be confidently sized. Suffix-less figures only count when
    they are large enough to be unambiguous raw-dollar amounts (>= $1M)."""
    best = None
    for m in _AMT_PARSE_RE.finditer(text):
        raw = m.group(1).rstrip(".,").replace(",", "")
        if not raw or raw in (".",):
            continue
        try:
            num = float(raw)
        except ValueError:
            continue
        suffix = (m.group(2) or "").lower()
        if suffix in ("billion", "bn", "b"):
            val = num * 1000.0
        elif suffix in ("million", "mn", "m"):
            val = num
        elif suffix in ("thousand", "k"):
            val = num / 1000.0
        elif num >= 1_000_000:  # bare large number -> treat as raw dollars
            val = num / 1_000_000.0
        else:
            continue  # un-suffixed small number: not confidently a $ magnitude
        if best is None or val > best:
            best = val
    return best


def detect_event(text):
    """Return event type ('ipo_s1' | 'funding' | 'launch') or None."""
    if IPO_RE.search(text):
        return "ipo_s1"
    if any(rx.search(text) for rx in FUND_RES):
        amt = max_amount_m(text)
        # Drop only when we can size the round AND it is below the floor.
        if amt is not None and amt < MIN_FUNDING_M:
            return None
        return "funding"
    if (LAUNCH_RE.search(text) and LAUNCH_OBJ_RE.search(text)
            and not NOT_LAUNCH_RE.search(text)):
        return "launch"
    return None


def entities_in(text):
    """text is the ORIGINAL-case string. Names match case-insensitively on word
    boundaries; bare ticker symbols match only as uppercase standalone tokens."""
    found = set()
    for label, (name_re, tick_re) in ENTITY_FORMS.items():
        if (name_re and name_re.search(text)) or (tick_re and tick_re.search(text)):
            found.add(label)
    return found


_PROPER = re.compile(r"\b([A-Z][a-zA-Z0-9.&'-]+(?:\s+[A-Z][a-zA-Z0-9.&'-]+){0,3})")
_STOP = {"The", "A", "An", "This", "That", "These", "Those", "It", "U.S.", "US",
         "AI", "I", "We", "On", "In", "At", "For", "And", "But", "New", "SEC"}
# Title-case verbs that signal the end of the subject's proper-noun run, so
# "Exa Raises $250M" and "Exa Labs" both collapse toward "Exa".
_SUBJ_CUT = {"Raises", "Raised", "Announces", "Announced", "Launches", "Launched",
             "Unveils", "Releases", "Released", "Files", "Filed", "Closes", "Valued",
             "Introduces", "Debuts", "Ships", "Inc", "Inc.", "LLC", "Ltd", "Corp",
             "Group", "Technologies", "Securities", "Labs"}


def fallback_subject(text):
    """Best-effort proper-noun subject when no known entity matched."""
    for m in _PROPER.finditer(text[:300]):
        cand = m.group(1).strip()
        words = cand.split()
        if words[0] in _STOP or len(cand) <= 2:
            continue
        kept = []
        for w in words:
            if w in _SUBJ_CUT:
                break
            kept.append(w)
        if kept:
            return " ".join(kept)
    return (text[:60] + "…") if len(text) > 60 else text


def build_delivered_blob(triage, briefing_md):
    parts = [briefing_md or ""]
    clusters = (triage or {}).get("clusters", []) if isinstance(triage, dict) else []
    for c in clusters:
        parts.append(c.get("title", ""))
        parts.append(c.get("why", ""))
        parts.append(" ".join(c.get("tickers", []) or []))
    return " \n ".join(parts)


def analyze(run):
    window = run.get("window_hours") or 24
    created = run.get("created_at")
    # bound the window to the run's own timestamp so QC is reproducible whenever
    # it runs, independent of items ingested afterwards.
    end = datetime.fromisoformat(str(created)) if created else datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(hours=window)
    rows = (client().table("raw_items")
            .select("source,text,url,reliability,fetched_at")
            .gte("fetched_at", start.isoformat())
            .lte("fetched_at", end.isoformat())
            .order("fetched_at", desc=True).limit(1000).execute().data or [])

    delivered = build_delivered_blob(run.get("triage"), run.get("briefing_md"))
    delivered_entities = entities_in(delivered)

    candidates = 0
    gaps = {}  # key -> gap dict (deduped by type+subject)
    for r in rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        etype = detect_event(text)
        if etype is None:
            continue
        ents = entities_in(text)
        # launches are noisy: only judge them when a known entity is involved.
        if etype == "launch" and not ents:
            continue
        candidates += 1
        if ents:
            covered = bool(ents & delivered_entities)
            subjects = sorted(ents)
        else:
            subj = fallback_subject(text)
            # For unknown entities, check whether the fallback name appears in the
            # delivered content (case-insensitive word-boundary match).
            covered = bool(re.search(r"\b" + re.escape(subj) + r"\b", delivered, re.IGNORECASE))
            subjects = [subj]
        if covered:
            continue
        for subj in subjects:
            key = f"{etype}|{subj.lower()}"
            g = gaps.setdefault(key, {
                "type": etype, "subject": subj, "item_count": 0,
                "sample_text": text[:280], "url": r.get("url"),
                "source": r.get("source"), "reliability": r.get("reliability"),
            })
            g["item_count"] += 1
    return {
        "run_id": run.get("id"), "window_hours": window,
        "items_scanned": len(rows), "big_events": candidates,
        "gaps": sorted(gaps.values(), key=lambda x: (-x["item_count"], x["type"])),
    }


# --- Paperclip ticketing ------------------------------------------------------
def papi(path, payload, method="POST"):
    api = (CFG.get("PAPERCLIP_API_BASE") or "https://paperclip.hedgingalpha.com").rstrip("/")
    req = urllib.request.Request(
        f"{api}{path}", data=json.dumps(payload).encode(), method=method,
        headers={"Authorization": f"Bearer {CFG['PAPERCLIP_API_KEY']}",
                 "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def recent_ticketed_subjects(days=7):
    """Subjects already ticketed recently — avoids re-filing the same gap."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = (client().table("coverage_qc").select("tickets,created_at")
                .gte("created_at", cutoff).execute().data or [])
    except Exception:  # noqa: BLE001 — table may not exist yet
        return set()
    seen = set()
    for r in rows:
        for tk in (r.get("tickets") or []):
            # only treat a subject as ticketed if an issue was actually filed —
            # entries skipped by the flood cap (max_tickets) must stay eligible.
            if tk.get("issue") and tk.get("key"):
                seen.add(tk["key"])
    return seen


def open_tickets(result, max_tickets=5):
    cid = CFG.get("PAPERCLIP_COMPANY_ID") or "f386ec58-21f8-4273-81df-1a187c82dc54"
    de = CFG.get("PAPERCLIP_DE_AGENT_ID") or "78b79ccb-7011-4753-b282-584d6136bfb6"
    if not (cid and CFG.get("PAPERCLIP_API_KEY")):
        return []
    already = recent_ticketed_subjects()
    short = str(result["run_id"])[:8]
    created = []
    filed = 0
    label = {"ipo_s1": "IPO/S-1", "funding": "Funding", "launch": "Launch"}
    for g in result["gaps"]:
        key = f"{g['type']}|{g['subject'].lower()}"
        if key in already:
            created.append({"key": key, "skipped": "recently_ticketed"})
            continue
        if filed >= max_tickets:
            created.append({"key": key, "skipped": "max_tickets"})
            continue
        title = f"🛰 Coverage-Bug: {label.get(g['type'], g['type'])} '{g['subject']}' verpasst (Briefing {short})"
        body = (
            f"Automatischer Coverage-QC-Befund für Briefing-Run `{result['run_id']}`.\n\n"
            f"**Verpasste Großmeldung** ({label.get(g['type'], g['type'])}) — im Feed-Fenster "
            f"({result['window_hours']}h), aber NICHT im ausgelieferten Briefing.\n\n"
            f"- **Subjekt:** {g['subject']}\n- **Quelle:** {g.get('source')}\n"
            f"- **URL:** {g.get('url') or '—'}\n- **Treffer im Fenster:** {g['item_count']}\n\n"
            f"> {g['sample_text']}\n\n"
            f"**Auftrag (Data-Engineer):** Root-Cause finden (Adapter-Lücke? Triage-Filter? "
            f"Watchlist?), Coverage schließen und Guardrail ergänzen, damit es nicht wiederkehrt.")
        try:
            res = papi(f"/api/companies/{cid}/issues",
                       {"title": title, "description": body,
                        "assigneeAgentId": de, "priority": "high"})
            created.append({"key": key, "issue": res.get("id") or res.get("issue", {}).get("id")})
            filed += 1
        except Exception as ex:  # noqa: BLE001
            created.append({"key": key, "error": str(ex)[:200]})
    return created


def persist(result, tickets):
    try:
        client().table("coverage_qc").insert({
            "run_id": result["run_id"], "window_hours": result["window_hours"],
            "items_scanned": result["items_scanned"], "big_events": result["big_events"],
            "gap_count": len(result["gaps"]), "gaps": result["gaps"],
            "tickets": tickets,
        }).execute()
        return True
    except Exception as ex:  # noqa: BLE001
        print(f"warn: persist failed ({type(ex).__name__}: {str(ex)[:120]})", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", help="briefing_runs id (default: latest done)")
    ap.add_argument("--open-tickets", action="store_true", help="file Coverage-Bug issues for gaps")
    ap.add_argument("--max-tickets", type=int, default=5, help="flood guard: max issues per run")
    ap.add_argument("--no-persist", action="store_true", help="skip writing coverage_qc row")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    t = client().table("briefing_runs")
    if a.run_id:
        data = t.select("*").eq("id", a.run_id).limit(1).execute().data
    else:
        data = (t.select("*").eq("status", "done")
                .order("created_at", desc=True).limit(1).execute().data)
    if not data:
        print("no briefing run found", file=sys.stderr)
        sys.exit(2)
    run = data[0]

    result = analyze(run)
    tickets = open_tickets(result, a.max_tickets) if a.open_tickets else []
    if not a.no_persist:
        persist(result, tickets)

    if a.json:
        print(json.dumps({**result, "tickets": tickets}, ensure_ascii=False))
        return
    print(f"Coverage-QC for run {result['run_id']} (window {result['window_hours']}h)")
    print(f"  items scanned: {result['items_scanned']}  big events: {result['big_events']}  "
          f"gaps: {len(result['gaps'])}")
    for g in result["gaps"]:
        print(f"  [GAP/{g['type']}] {g['subject']}  (x{g['item_count']}, {g.get('source')})")
        print(f"        {g['sample_text'][:140]}")
    if a.open_tickets:
        print(f"  tickets: {json.dumps(tickets, ensure_ascii=False)}")
    elif result["gaps"]:
        print("  (report-only; pass --open-tickets to file Coverage-Bug issues)")


if __name__ == "__main__":
    main()
