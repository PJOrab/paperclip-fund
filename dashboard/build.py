"""
Dashboard-Generator (MVP). Liest Supabase serverseitig und schreibt eine
statische, in sich geschlossene HTML-Seite nach DASHBOARD_OUT
(default /var/www/html/fund/index.html → https://hedgingalpha.com/fund/).

  python -m dashboard.build            # nach DASHBOARD_OUT schreiben
  python -m dashboard.build --stdout   # HTML auf stdout (Test)

Keine Secrets im Output: Daten werden zur Build-Zeit eingebettet.
"""
import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ingestion.db import client

OUT_DEFAULT = os.environ.get("DASHBOARD_OUT", "/var/www/html/fund/index.html")

# Static ticker→sector mapping aligned with the 6-sector taxonomy (sector_taxonomy.md)
TICKER_SECTOR: dict[str, str] = {
    # S1 Semiconductors & Hardware
    "NVDA": "S1 Semis/HW", "AMD": "S1 Semis/HW", "TSM": "S1 Semis/HW",
    "ASML": "S1 Semis/HW", "AVGO": "S1 Semis/HW", "MU": "S1 Semis/HW",
    "ARM": "S1 Semis/HW", "SMCI": "S1 Semis/HW", "QCOM": "S1 Semis/HW",
    "MRVL": "S1 Semis/HW", "INTC": "S1 Semis/HW",
    # S2 Infrastructure & Networking
    "ANET": "S2 Infra/Net", "VRT": "S2 Infra/Net", "DELL": "S2 Infra/Net",
    # S3 Hyperscalers & Big Tech
    "MSFT": "S3 Hyperscaler", "GOOGL": "S3 Hyperscaler", "AMZN": "S3 Hyperscaler",
    "META": "S3 Hyperscaler", "AAPL": "S3 Hyperscaler",
    # S4 AI Software & Applications
    "PLTR": "S4 AI-Software", "ORCL": "S4 AI-Software", "NOW": "S4 AI-Software",
    "CRM": "S4 AI-Software", "SNOW": "S4 AI-Software", "CRWD": "S4 AI-Software",
    "ADBE": "S4 AI-Software",
}


def _build_sector_summary(thesis_calls: list[dict]) -> list[dict]:
    """Aggregate thesis calls by sector: call count, avg conviction, long/short split."""
    from collections import defaultdict
    buckets: dict[str, dict] = defaultdict(lambda: {"calls": 0, "long": 0, "short": 0,
                                                      "convictions": [], "tickers": set()})
    for call in thesis_calls:
        for tk in (call.get("tickers") or []):
            sector = TICKER_SECTOR.get(tk, "Other")
            b = buckets[sector]
            b["calls"] += 1
            b["tickers"].add(tk)
            if call.get("direction") == "long":
                b["long"] += 1
            elif call.get("direction") == "short":
                b["short"] += 1
            conv = call.get("conviction")
            if isinstance(conv, (int, float)):
                b["convictions"].append(conv)
    result = []
    for sector, b in sorted(buckets.items()):
        avg_conv = round(sum(b["convictions"]) / len(b["convictions"]), 2) if b["convictions"] else None
        result.append({
            "sector": sector,
            "calls": b["calls"],
            "long": b["long"],
            "short": b["short"],
            "avg_conviction": avg_conv,
            "tickers": sorted(b["tickers"]),
        })
    result.sort(key=lambda x: -x["calls"])
    return result


def collect() -> dict:
    c = client()
    total = c.table("raw_items").select("id", count="exact").limit(1).execute().count
    # Source distribution from recent 2000 items only — fetching all rows is O(N) network cost.
    rows = (c.table("raw_items").select("source,adapter")
            .order("fetched_at", desc=True).limit(2000).execute().data or [])
    recent = (c.table("raw_items").select("source,text,url,fetched_at")
              .order("fetched_at", desc=True).limit(25).execute().data or [])
    runs = (c.table("ingestion_runs").select("*")
            .order("started_at", desc=True).limit(1).execute().data or [])
    briefing = None
    briefing_history: list[dict] = []
    thesis_calls: list[dict] = []
    try:
        b = (c.table("briefing_runs")
             .select("id,created_at,status,theses,devils_advocate,window_hours,briefing_md")
             .order("created_at", desc=True).limit(14).execute().data or [])
        briefing = b[0] if b else None
        for run in b:
            theses_blob = run.get("theses") or {}
            theses = (theses_blob.get("theses", []) if isinstance(theses_blob, dict)
                      else theses_blob) or []
            devil_blob = run.get("devils_advocate") or {}
            critiques = (devil_blob.get("critiques", []) if isinstance(devil_blob, dict)
                         else devil_blob) or []
            crit_map = {cr.get("id"): cr for cr in critiques}
            convictions = [t.get("conviction") for t in theses
                           if isinstance(t.get("conviction"), (int, float))]
            tickers = list({tk for t in theses for tk in (t.get("tickers") or [])})[:5]
            run_date = (run.get("created_at") or "")[:10]
            briefing_history.append({
                "id": run["id"][:8],
                "date": (run.get("created_at") or "")[:16].replace("T", " "),
                "status": run.get("status", "?"),
                "thesis_count": len(theses),
                "avg_conviction": round(sum(convictions) / len(convictions), 2) if convictions else None,
                "top_tickers": tickers,
            })
            for t in theses:
                cr = crit_map.get(t.get("id")) or {}
                thesis_calls.append({
                    "date": run_date,
                    "run_id": run["id"][:8],
                    "tickers": (t.get("tickers") or [])[:3],
                    "direction": t.get("direction", "long"),
                    "conviction": t.get("conviction"),
                    "horizon": t.get("horizon", "?"),
                    "thesis": (t.get("thesis") or "")[:90],
                    "is_differentiated": bool(t.get("is_differentiated")),
                    "verdict": cr.get("verdict", "—"),
                })
    except Exception:
        briefing = None

    # Briefing freshness: find the most recent 'done' run and compute age in hours
    briefing_age_hours: float | None = None
    last_done_briefing_at: str | None = None
    for run in briefing_history:
        if run.get("status") == "done":
            try:
                ts_str = run["date"].replace(" ", "T") + ":00+00:00"
                ts = datetime.fromisoformat(ts_str)
                age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                briefing_age_hours = round(age_h, 1)
                last_done_briefing_at = run["date"]
            except Exception:
                pass
            break  # briefing_history is already newest-first

    # Sort thesis calls newest-first
    thesis_calls.sort(key=lambda x: x["date"], reverse=True)

    return {
        "total": total,
        "by_source": dict(Counter(r["source"] for r in rows).most_common()),
        "by_adapter": dict(Counter(r["adapter"] for r in rows).most_common()),
        "recent": recent,
        "last_run": runs[0] if runs else None,
        "briefing": briefing,
        "briefing_history": briefing_history,
        "thesis_calls": thesis_calls,
        "sector_summary": _build_sector_summary(thesis_calls),
        "track_record": load_track_record(),
        "briefing_age_hours": briefing_age_hours,
        "last_done_briefing_at": last_done_briefing_at,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def load_track_record() -> dict | None:
    """Thesen-Track-Record (HED-29 §5). Written upstream by the scoring step
    (HED-25) into a structured JSON next to this module; the UI never recomputes
    scoring (only move_pct colouring). Missing/broken file -> None -> empty state."""
    p = Path(__file__).with_name("track_record.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI/Tech Fund — Intelligence Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
:root{--bg:#0b0f17;--panel:#141a26;--panel2:#1b2333;--line:#263248;--txt:#e6edf6;
--mut:#8aa0bd;--accent:#4da3ff;--green:#3fb950;--red:#f85149;--amber:#d29922;
--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--s6:32px;
--fs-h1:22px;--fs-h2:13px;--fs-body:14px;--fs-cap:12px;--fs-micro:11px;--fs-kpi:30px;
--measure:72ch;--ok:#3fb950;--warn:#d29922;--err:#f85149;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:var(--fs-body)/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:var(--s5)}
h1{font-size:var(--fs-h1);margin:0}
h2{font-size:var(--fs-h2);text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:var(--s6) 0 var(--s3)}
.sub{color:var(--mut);font-size:var(--fs-cap);margin-top:var(--s1)}
.grid{display:grid;gap:var(--s3)}
.cards{grid-template-columns:repeat(5,1fr)}
.two-col{grid-template-columns:1fr 1fr}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:var(--s4)}
.kpi{font-size:var(--fs-kpi);font-weight:700}
.kpi small{font-size:var(--fs-h2);color:var(--mut);font-weight:400}
/* pipeline */
.flow{display:flex;align-items:stretch;gap:0;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}
.step{flex:0 0 auto;min-width:120px;background:var(--panel2);border:1px solid var(--line);
border-radius:10px;padding:var(--s3);text-align:center;position:relative}
.step .t{font-weight:600}.step .m{color:var(--mut);font-size:var(--fs-cap);margin-top:3px;white-space:nowrap}
.arrow{display:flex;align-items:center;color:var(--accent);font-size:20px;padding:0 6px}
.tag{display:inline-block;background:var(--panel2);border:1px solid var(--line);
border-radius:6px;padding:1px 7px;font-size:var(--fs-cap);color:var(--mut)}
.bar{height:8px;background:var(--panel2);border-radius:6px;overflow:hidden;margin-top:var(--s1)}
.bar>span{display:block;height:100%;background:var(--accent);min-width:2px}
.srcrow{display:flex;justify-content:space-between;font-size:var(--fs-cap);margin-bottom:2px}
.feed{max-height:320px;overflow:auto}
.feed .it{padding:var(--s2) 0;border-bottom:1px solid var(--line);font-size:13px}
.feed .it a{color:var(--accent);text-decoration:none}
.feed .s{color:var(--mut);font-size:11px;text-transform:uppercase}
.thesis{border-left:3px solid var(--accent);background:var(--panel2);
border-radius:8px;padding:var(--s3);margin-bottom:10px}
.thesis .h{font-weight:600}
.dir{font-size:11px;padding:1px 7px;border-radius:6px;border:1px solid var(--line)}
.long{color:var(--green)}.short{color:var(--red)}.pair{color:var(--amber)}
.devil{margin-top:var(--s2);padding:var(--s2) 10px;background:#1a1320;border:1px solid #3a2540;
border-radius:8px;font-size:13px}
.devil .v{font-weight:600;text-transform:uppercase;font-size:11px}
.brief{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:18px;
max-width:var(--measure);margin-inline:auto;line-height:1.7}
.brief h1{font-size:18px}.brief h2{color:var(--txt);text-transform:none;letter-spacing:0;font-size:15px}
.muted{color:var(--mut)}
.pill{display:inline-block;font-size:var(--fs-cap);padding:2px 8px;border-radius:6px;text-transform:capitalize}
.pill--ok{background:rgba(63,185,80,.15);color:var(--ok);border:1px solid var(--ok)}
.pill--warn{background:rgba(210,153,34,.15);color:var(--warn);border:1px solid var(--warn)}
.pill--err{background:rgba(248,81,73,.15);color:var(--err);border:1px solid var(--err)}
.foot{color:var(--mut);font-size:var(--fs-cap);margin-top:var(--s6);text-align:center}
/* track-record (HED-29) */
.tr-cards{grid-template-columns:repeat(4,1fr)}
.pill--neutral{background:rgba(138,160,189,.12);color:var(--mut);border:1px solid var(--line)}
.tr-tbl{display:grid;grid-template-columns:auto 1.4fr auto auto 1.3fr auto auto;
  gap:0 var(--s3);font-size:var(--fs-h2);align-items:center}
.tr-tbl .th{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
  padding-bottom:var(--s2);border-bottom:1px solid var(--line)}
.tr-tbl .cell{padding:var(--s2) 0;border-bottom:1px solid var(--line)}
.tr-tbl .num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.tr-tbl .dlabel{display:none;color:var(--mut);font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.04em}
.tr-lbl .t{font-weight:600}.tr-lbl .tk{color:var(--mut);font-size:var(--fs-micro)}
.move-up{color:var(--green)}.move-dn{color:var(--red)}
.devsig{cursor:help;color:var(--mut);margin-left:6px;font-size:var(--fs-cap)}
.calib{display:flex;gap:var(--s4);align-items:center;flex-wrap:wrap}
.calib svg{flex:0 0 auto}
.calib .lg{font-size:var(--fs-cap);color:var(--mut)}
.calib .lg .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}
.empty{text-align:center;padding:var(--s5) var(--s4)}
.empty .g{font-size:34px;line-height:1}
.empty .hl{font-weight:600;margin-top:var(--s2)}
.empty .ex{color:var(--mut);max-width:46ch;margin:var(--s2) auto 0}
.tr-progress{margin-top:var(--s3);max-width:28ch;margin-left:auto;margin-right:auto}
.tr-progress .tr-pb-label{display:flex;justify-content:space-between;font-size:var(--fs-cap);color:var(--mut);margin-bottom:4px}
.tr-progress .tr-pb-track{height:6px;background:var(--panel2);border-radius:3px;border:1px solid var(--line);overflow:hidden}
.tr-progress .tr-pb-fill{height:100%;border-radius:3px;background:var(--accent);transition:width .3s}
.conv-lo{color:var(--mut)}.conv-mid{color:var(--txt)}.conv-hi{color:var(--accent);font-weight:600}
.tkl{color:inherit;text-decoration:none;font-weight:inherit}
.tkl:hover{text-decoration:underline;text-underline-offset:2px;color:var(--accent)}
@media (max-width:760px){
  .cards{grid-template-columns:repeat(3,1fr)}
  .tr-cards{grid-template-columns:repeat(2,1fr)}
  .two-col{grid-template-columns:1fr}
  .flow{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .tr-tbl{display:block}
  .tr-tbl .th{display:none}
  .tr-tbl .row{display:block;padding:var(--s3) 0;border-bottom:1px solid var(--line)}
  .tr-tbl .cell{display:flex;justify-content:space-between;gap:var(--s3);padding:2px 0;border:0;text-align:right}
  .tr-tbl .num{text-align:right}
  .tr-tbl .dlabel{display:inline-block;min-width:90px;vertical-align:top}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important}
}
</style></head>
<body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px">
    <div><h1>🤖 AI/Tech Fund — Intelligence Dashboard</h1>
    <div class="sub">Live-Feed → Agenten-Gremium → CEO-Briefing · MVP</div></div>
    <div class="sub">aktualisiert: <span id="built"></span></div>
  </div>

  <h2>Workflow</h2>
  <div class="flow" id="flow"></div>

  <h2>Datenfeed <span id="feedstale"></span></h2>
  <div class="grid cards" id="kpis"></div>
  <div class="grid two-col" style="margin-top:14px">
    <div class="panel"><div class="muted" style="margin-bottom:8px">Quellen</div><div id="sources"></div></div>
    <div class="panel"><div class="muted" style="margin-bottom:8px">Neueste Items</div><div class="feed" id="feed"></div></div>
  </div>

  <h2>Letztes Briefing</h2>
  <div id="briefing"></div>

  <h2>Briefing-Verlauf</h2>
  <div class="panel" id="bhistory"></div>

  <h2>Thesen-Track-Record <span id="trstand" class="tag"></span></h2>
  <div id="trackrecord"></div>

  <h2>Sektor-Übersicht</h2>
  <div class="panel" id="sectorview"></div>

  <h2>Thesis Calls <span id="callsbadge"></span></h2>
  <div class="panel" id="thesiscalls"></div>

  <h2>Adapter Health <span id="adapterstale"></span></h2>
  <div class="panel" id="adapterhealth"></div>

  <div class="foot">AI/Tech Fund · generiert aus Supabase · keine Secrets im Browser</div>
</div>
<script>
const D = __DATA__;
const $ = (id)=>document.getElementById(id);
$("built").textContent = D.built_at;

// Pipeline
const steps = [
  {t:"Quellen",m:"EDGAR · arXiv · +4"},
  {t:"Ingestion",m:"Cron /30min"},
  {t:"Supabase",m:D.total+" raw_items"},
  {t:"Triage",m:"Opus"},{t:"Analyst",m:"Opus"},{t:"These",m:"Opus"},
  {t:"Devil's Advocate",m:"Opus"},{t:"Editor",m:"Opus"},{t:"Telegram",m:"Briefing"}
];
$("flow").innerHTML = steps.map((s,i)=>
  `<div class="step"><div class="t">${s.t}</div><div class="m">${s.m}</div></div>`+
  (i<steps.length-1?'<div class="arrow">›</div>':'')).join("");

// KPIs
const lr = D.last_run||{};
function statusPill(s){
  const map={done:"ok",completed:"ok",running:"warn",queued:"warn",pending:"warn",error:"err",failed:"err"};
  const k=map[(s||"").toLowerCase()]||"warn";
  return `<span class="pill pill--${k}">${esc(s)}</span>`;
}
const bstatus = (D.briefing && D.briefing.status) ? D.briefing.status : null;
const ageH = D.briefing_age_hours;
let briefingAgeLabel = "—";
if(ageH != null){
  briefingAgeLabel = ageH < 1 ? `<${Math.round(ageH*60)}min` : `${ageH.toFixed(1)}h`;
}
const ageClass = ageH==null?"":ageH>8?"pill--err":ageH>4?"pill--warn":"pill--ok";
const agePill = ageH!=null ? `<span class="pill ${ageClass}">${briefingAgeLabel}</span>` : "—";
$("kpis").innerHTML = [
  ["raw_items gesamt", D.total],
  ["Quellen", Object.keys(D.by_source).length],
  ["letzter Ingest", lr.items_inserted!=null?("+"+lr.items_inserted):"—"],
  ["Briefing-Alter", agePill],
  ["Briefing-Status", bstatus? statusPill(bstatus):"—"]
].map(([k,v])=>`<div class="panel"><div class="kpi">${v}</div><div class="muted">${k}</div></div>`).join("");

// Stale-Feed-State: letzter Ingest älter als ~2h
if(lr.started_at){
  const age = Date.now() - new Date(lr.started_at).getTime();
  if(age > 2*3600*1000){ $("feedstale").innerHTML = '<span class="pill pill--warn">veraltet</span>'; }
}

// Quellen-Balken (Top-6, Rest als "+N weitere")
const srcEntries = Object.entries(D.by_source);
const max = Math.max(1,...srcEntries.map(e=>e[1]));
const topSrc = srcEntries.slice(0,6), tail = srcEntries.slice(6);
let srcHtml = topSrc.map(([s,n])=>
  `<div class="srcrow"><span>${esc(s)}</span><span>${n}</span></div>
   <div class="bar"><span style="width:${Math.round(n/max*100)}%"></span></div>`).join("");
if(tail.length){
  const rest = tail.reduce((a,[,n])=>a+n,0);
  srcHtml += `<div class="srcrow muted"><span>+${tail.length} weitere</span><span>${rest}</span></div>`;
}
$("sources").innerHTML = srcHtml;

// Feed
$("feed").innerHTML = (D.recent||[]).map(r=>{
  const txt = r.url? `<a href="${r.url}" target="_blank">${esc(r.text)}</a>`:esc(r.text);
  return `<div class="it"><span class="s">${r.source}</span><br>${txt}</div>`;}).join("")
  || '<div class="muted">keine Daten</div>';

// Briefing
function dirClass(d){return d==="long"?"long":d==="short"?"short":"pair";}
const b = D.briefing;
if(!b){ $("briefing").innerHTML='<div class="panel muted">Noch kein Briefing. Sobald der n8n-Workflow lief, erscheint es hier.</div>'; }
else{
  const theses=((b.theses||{}).theses)||[];
  const crit=((b.devils_advocate||{}).critiques)||[];
  const cmap={}; crit.forEach(c=>cmap[c.id]=c);
  let html='';
  if(b.briefing_md){ html+=`<div class="brief">${marked.parse(b.briefing_md)}</div>`; }
  if(theses.length){
    html+='<h2 style="margin-top:20px">Thesen & Devil\'s Advocate</h2>';
    html+=theses.map(t=>{
      const c=cmap[t.id]||{};
      return `<div class="thesis"><div class="h">${(t.tickers||[]).join(", ")}
        <span class="dir ${dirClass(t.direction)}">${t.direction||""}</span>
        <span class="muted">· Conviction ${t.conviction??"—"}</span></div>
        <div style="margin-top:4px">${esc(t.thesis||"")}</div>
        ${c.strongest_counter?`<div class="devil"><span class="v">⚖️ Devil's Advocate (${c.verdict||"?"})</span><br>${esc(c.strongest_counter)}
        ${c.blind_spot?`<br><span class="muted">Blind spot: ${esc(c.blind_spot)}</span>`:""}</div>`:""}
      </div>`;}).join("");
  }
  $("briefing").innerHTML=html||'<div class="panel muted">Briefing vorhanden, aber leer.</div>';
}
function esc(s){return (s||"").replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));}

// Briefing history table
const hist = D.briefing_history||[];
if(hist.length){
  const rows=hist.map(h=>{
    const pill=statusPill(h.status);
    const conv=h.avg_conviction!=null?h.avg_conviction.toFixed(2):"—";
    const ticks=h.top_tickers.length?h.top_tickers.join(", "):"—";
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:6px 8px;color:var(--mut)">${esc(h.date)}</td>
      <td style="padding:6px 8px">${pill}</td>
      <td style="padding:6px 8px;text-align:center">${h.thesis_count}</td>
      <td style="padding:6px 8px;text-align:center">${conv}</td>
      <td style="padding:6px 8px;color:var(--accent)">${esc(ticks)}</td>
    </tr>`;}).join("");
  $("bhistory").innerHTML=`<table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="border-bottom:1px solid var(--line);color:var(--mut)">
      <th style="padding:6px 8px;text-align:left">Datum</th>
      <th style="padding:6px 8px;text-align:left">Status</th>
      <th style="padding:6px 8px;text-align:center">Thesen</th>
      <th style="padding:6px 8px;text-align:center">⌀ Conviction</th>
      <th style="padding:6px 8px;text-align:left">Top Ticker</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}else{
  $("bhistory").innerHTML='<div class="muted">Noch keine Briefing-Runs.</div>';
}

// Thesen-Track-Record (HED-29) — real-price scoring from calibration_log
function pct(x){return x==null?"—":Math.round(x*100)+"%";}
function convCls(c){return c==null?"":c>=0.6?"conv-hi":c>=0.35?"conv-mid":"conv-lo";}
function verdictPill(v){
  const map={hit:["ok","✓ Hit"],miss:["err","✗ Miss"],neutral:["neutral","Neutral"],too_early:["warn","⏳ too early"]};
  const [k,lbl]=map[v]||["neutral",esc(v||"—")];
  return `<span class="pill pill--${k}">${lbl}</span>`;
}
function moveCell(m){
  if(m==null) return '<span class="muted">—</span>';
  const up=m>=0, sign=up?"+":"−";
  return `<span class="${up?"move-up":"move-dn"}">${sign}${Math.abs(m).toFixed(1)}%</span>`;
}
function calibSvg(buckets){
  if(!buckets || buckets.length<3){
    return '<div class="muted" style="font-size:13px">Zu wenige Datenpunkte für eine Kalibrierungs-Linie '+
      '(min. 3 Conviction-Buckets nötig).</div>';
  }
  const W=240,H=160,p=28, X=v=>p+v*(W-2*p), Y=v=>H-p-v*(H-2*p);
  let pts=buckets.map(bk=>{
    const r=Math.min(8,3+(bk.n||1));
    return `<circle cx="${X(bk.conviction).toFixed(1)}" cy="${Y(bk.observed_hit_rate).toFixed(1)}" r="${r}" `+
      `fill="var(--accent)" fill-opacity=".8"><title>conv ${pct(bk.conviction)} · hit ${pct(bk.observed_hit_rate)} · n=${bk.n}</title></circle>`;
  }).join("");
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Kalibrierung: Conviction vs. beobachtete Hit-Rate">
    <line x1="${X(0)}" y1="${Y(0)}" x2="${X(1)}" y2="${Y(1)}" stroke="var(--mut)" stroke-dasharray="4 3" stroke-width="1"/>
    <line x1="${p}" y1="${H-p}" x2="${W-p}" y2="${H-p}" stroke="var(--line)"/>
    <line x1="${p}" y1="${p}" x2="${p}" y2="${H-p}" stroke="var(--line)"/>
    <text x="${W/2}" y="${H-6}" fill="var(--mut)" font-size="10" text-anchor="middle">Conviction →</text>
    <text x="10" y="${H/2}" fill="var(--mut)" font-size="10" text-anchor="middle" transform="rotate(-90 10 ${H/2})">Hit-Rate →</text>
    ${pts}
  </svg>
  <div class="lg"><span class="sw" style="background:var(--accent)"></span>Conviction-Bucket<br>
  <span class="sw" style="background:var(--mut)"></span>perfekte Kalibrierung<br>
  <span class="muted">über Linie = unterconfident · darunter = overconfident</span></div>`;
}
(function renderTrackRecord(){
  const tr=D.track_record;
  const root=$("trackrecord");
  if(!tr || !tr.aggregate){ root.innerHTML='<div class="panel muted">Track-Record noch nicht verfügbar.</div>'; return; }
  const a=tr.aggregate, scored=a.scored||0;
  $("trstand").textContent = scored+" gewertet";
  root.innerHTML = `<div class="grid tr-cards" id="trkpi"></div><div id="trbody" style="margin-top:14px"></div>`;
  const biasTxt = a.calibration_bias==null ? "—"
    : (a.calibration_bias>=0?"+":"−")+Math.abs(a.calibration_bias*100).toFixed(0)+"%";
  $("trkpi").innerHTML=[
    ["Hit-Rate", pct(a.hit_rate)],
    ["gewertet", scored+" / "+(a.total??"—")],
    ["too early", a.too_early??"—"],
    ["Kalib.-Bias", biasTxt]
  ].map(([k,v])=>`<div class="panel"><div class="kpi">${v}</div><div class="muted">${k}</div></div>`).join("");
  const scoredTheses=(tr.theses||[]).filter(t=>t.verdict && t.verdict!=="too_early");
  if(scored>0 && scoredTheses.length){
    const head=["Datum","These","Richtung","Conviction","Kurs","Move %","Verdikt"];
    const order={miss:0,hit:1,neutral:2,too_early:3};
    const rows=scoredTheses.slice().sort((x,y)=>
      (order[x.verdict]-order[y.verdict])||((y.conviction||0)-(x.conviction||0)));
    let tbl='<div class="tr-tbl">'+head.map(h=>`<div class="th">${h}</div>`).join("");
    tbl+=rows.map(t=>{
      const dev=t.devil?`<span class="devsig" title="⚖ Devil (${esc(t.devil.verdict||"?")}): ${esc(t.devil.note||"")}">⚖</span>`:"";
      const kurs=t.baseline_price!=null?`${t.baseline_price}${t.current_price!=null?" → "+t.current_price:""}`:"—";
      return `<div class="row" style="display:contents">
        <div class="cell num"><span class="dlabel">Datum </span>${esc(t.date||"—")}</div>
        <div class="cell tr-lbl"><span class="dlabel">These </span><span class="t">${esc(t.label||"")}</span> <span class="tk">${(t.tickers||[]).map(tk=>`<a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">${esc(tk)}</a>`).join(", ")}</span></div>
        <div class="cell"><span class="dlabel">Richtung </span><span class="dir ${dirClass(t.direction)}">${esc(t.direction||"")}</span></div>
        <div class="cell num"><span class="dlabel">Conviction </span><span class="${convCls(t.conviction)}">${t.conviction!=null?t.conviction.toFixed(2):"—"}</span></div>
        <div class="cell num"><span class="dlabel">Kurs </span>${esc(kurs)}</div>
        <div class="cell num"><span class="dlabel">Move </span>${moveCell(t.move_pct)}</div>
        <div class="cell"><span class="dlabel">Verdikt </span>${verdictPill(t.verdict)}${dev}</div>
      </div>`;}).join("");
    tbl+='</div>';
    $("trbody").innerHTML=`<div class="grid two-col">
      <div class="panel">${tbl}</div>
      <div class="panel"><div class="muted" style="margin-bottom:8px">Conviction-Kalibrierung</div>
        <div class="calib">${calibSvg(tr.calibration_buckets)}</div></div></div>`;
  } else {
    const esd=tr.earliest_score_date;
    let cd="";
    if(esd){
      const days=Math.ceil((new Date(esd+"T00:00:00Z")-Date.now())/864e5);
      const windowDays=21;
      const elapsed=Math.max(0,Math.min(windowDays,windowDays-days));
      const pctv=Math.round((elapsed/windowDays)*100);
      const label=days>0?`Erste Wertung in ${days} Tag${days===1?"":"en"} (${esc(esd)})`:`Wertung fällig ab ${esc(esd)}`;
      cd=`<div class="tr-progress">
        <div class="tr-pb-label"><span>Reifung</span><span>${pctv}%</span></div>
        <div class="tr-pb-track" title="${label}"><div class="tr-pb-fill" style="width:${pctv}%"></div></div>
        <div style="margin-top:4px;font-size:var(--fs-cap);color:var(--mut);text-align:center">${label}</div>
      </div>`;
    }
    $("trbody").innerHTML=`<div class="panel"><div class="empty">
      <div class="g">⏳</div>
      <div class="hl">Noch keine gewerteten Thesen</div>
      <div class="ex">${a.too_early||0} offene These${(a.too_early===1)?"":"n"} — der Zeithorizont (Wochen/Quartale) ist noch nicht abgelaufen. Gewertet wird gegen reale Kurse, keine Schätzungen.</div>
      ${cd}
    </div></div>`;
  }
})();

// Sector view
const sectors = D.sector_summary||[];
if(sectors.length){
  const maxCalls = Math.max(1,...sectors.map(s=>s.calls));
  const rows = sectors.map(s=>{
    const pct = Math.max(4, Math.round(s.calls/maxCalls*100));
    const conv = s.avg_conviction!=null?s.avg_conviction.toFixed(2):'—';
    const ticks = (s.tickers||[]).slice(0,5).join(', ');
    const ls = s.long||0, ss = s.short||0;
    const lsPill = ls>0?`<span class="pill pill--ok">${ls}L</span> `:'';
    const ssPill = ss>0?`<span class="pill pill--err">${ss}S</span> `:'';
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:5px 8px;font-size:13px;white-space:nowrap">${esc(s.sector)}</td>
      <td style="padding:5px 8px;text-align:center;font-weight:600">${s.calls}</td>
      <td style="padding:5px 8px">${lsPill}${ssPill}</td>
      <td style="padding:5px 8px;text-align:center">${conv}</td>
      <td style="padding:5px 8px;width:30%;min-width:80px">
        <div class="bar"><span style="width:${pct}%"></span></div></td>
      <td style="padding:5px 8px;font-size:11px;color:var(--mut)">${esc(ticks)}</td>
    </tr>`;}).join('');
  $("sectorview").innerHTML=`
    <div style="font-size:12px;color:var(--mut);margin-bottom:8px">Aus den letzten 14 Briefing-Runs · nach Sektor aggregiert</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="border-bottom:1px solid var(--line);color:var(--mut);font-size:11px">
        <th style="padding:5px 8px;text-align:left">Sektor</th>
        <th style="padding:5px 8px;text-align:center">Calls</th>
        <th style="padding:5px 8px;text-align:left">L/S</th>
        <th style="padding:5px 8px;text-align:center">⌀ Conv</th>
        <th style="padding:5px 8px;text-align:left">Anteil</th>
        <th style="padding:5px 8px;text-align:left">Ticker</th>
      </tr></thead><tbody>${rows}</tbody>
    </table>`;
}else{
  $("sectorview").innerHTML='<div class="muted">Noch keine Sector-Daten.</div>';
}

// Thesis Calls table
const calls = D.thesis_calls||[];
if(calls.length){
  const diffCount = calls.filter(c=>c.is_differentiated).length;
  $("callsbadge").innerHTML = `<span class="pill pill--ok">${calls.length} calls</span>`
    + (diffCount?` <span class="pill pill--warn">${diffCount} non-consensus</span>`:'');
  const verdictPill=(v)=>{
    if(v==='agree') return '<span class="pill pill--ok">agree</span>';
    if(v==='reject') return '<span class="pill pill--err">reject</span>';
    if(v==='caution') return '<span class="pill pill--warn">caution</span>';
    return `<span class="tag">${esc(v)}</span>`;
  };
  const dirC=(d)=>d==='long'?'long':d==='short'?'short':'pair';
  const rows=calls.map(c=>{
    const ticks=(c.tickers||[]).join(', ')||'—';
    const diff=c.is_differentiated?'<span class="pill pill--warn" title="Non-consensus">★</span>':'';
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:5px 8px;color:var(--mut);font-size:12px">${esc(c.date)}</td>
      <td style="padding:5px 8px;color:var(--accent);font-weight:600">${esc(ticks)}</td>
      <td style="padding:5px 8px"><span class="dir ${dirC(c.direction)}">${esc(c.direction)}</span></td>
      <td style="padding:5px 8px;text-align:center">${c.conviction!=null?c.conviction.toFixed(2):'—'}</td>
      <td style="padding:5px 8px">${esc(c.horizon)}</td>
      <td style="padding:5px 8px">${verdictPill(c.verdict)}</td>
      <td style="padding:5px 8px">${diff}</td>
      <td style="padding:5px 8px;font-size:12px;color:var(--mut);max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.thesis)}</td>
    </tr>`;}).join('');
  $("thesiscalls").innerHTML=`
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="border-bottom:1px solid var(--line);color:var(--mut);font-size:11px">
        <th style="padding:5px 8px;text-align:left">Datum</th>
        <th style="padding:5px 8px;text-align:left">Ticker</th>
        <th style="padding:5px 8px;text-align:left">Dir</th>
        <th style="padding:5px 8px;text-align:center">Conv</th>
        <th style="padding:5px 8px;text-align:left">Horizont</th>
        <th style="padding:5px 8px;text-align:left">Devil</th>
        <th style="padding:5px 8px;text-align:left">NC</th>
        <th style="padding:5px 8px;text-align:left">These</th>
      </tr></thead><tbody>${rows}</tbody>
    </table>`;
}else{
  $("thesiscalls").innerHTML='<div class="muted">Noch keine Thesen in den letzten 14 Runs.</div>';
}

// Adapter Health
const pa = (lr.per_adapter)||{};
const errs = (lr.errors)||{};
const adapterNames = Object.keys(pa);
if(adapterNames.length){
  const runTime = lr.started_at ? new Date(lr.started_at).toISOString().replace("T"," ").slice(0,16)+" UTC" : "—";
  const dead = adapterNames.filter(n=>pa[n]===0 && !errs[n]);
  if(dead.length){ $("adapterstale").innerHTML='<span class="pill pill--warn">'+dead.length+' tot</span>'; }
  const maxItems = Math.max(1,...adapterNames.map(n=>pa[n]||0));
  const rows = adapterNames.map(n=>{
    const cnt = pa[n]||0;
    const err = errs[n]||null;
    let pill, bar;
    if(err){ pill='<span class="pill pill--err">error</span>'; bar='var(--err)'; }
    else if(cnt===0){ pill='<span class="pill pill--warn">0 items</span>'; bar='var(--warn)'; }
    else { pill='<span class="pill pill--ok">'+cnt+'</span>'; bar='var(--accent)'; }
    const pct = Math.max(2, Math.round(cnt/maxItems*100));
    return `<tr style="border-bottom:1px solid var(--line)">
      <td style="padding:5px 8px;font-size:13px">${esc(n)}</td>
      <td style="padding:5px 8px">${pill}</td>
      <td style="padding:5px 8px;width:35%;min-width:100px">
        <div class="bar"><span style="width:${pct}%;background:${bar}"></span></div></td>
      <td style="padding:5px 8px;font-size:11px;color:var(--mut);max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${err?esc(err.slice(0,120)):""}</td>
    </tr>`;}).join("");
  $("adapterhealth").innerHTML=`
    <div style="font-size:12px;color:var(--mut);margin-bottom:8px">Letzter Lauf: ${esc(runTime)} · ${adapterNames.length} Adapter · ${lr.items_fetched??0} fetched · +${lr.items_inserted??0} neu</div>
    <table style="width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid var(--line);color:var(--mut);font-size:11px">
        <th style="padding:5px 8px;text-align:left">Adapter</th>
        <th style="padding:5px 8px;text-align:left">Items</th>
        <th style="padding:5px 8px;text-align:left">Anteil</th>
        <th style="padding:5px 8px;text-align:left">Fehler</th>
      </tr></thead><tbody>${rows}</tbody>
    </table>`;
}else{
  $("adapterhealth").innerHTML='<div class="muted">Noch kein Ingestion-Lauf.</div>';
}
</script></body></html>"""


def render(data: dict) -> str:
    return HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False, default=str))


def main():
    ap = argparse.ArgumentParser(description="AI/Tech Fund — Dashboard-Generator")
    ap.add_argument("--stdout", action="store_true", help="HTML auf stdout statt Datei")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    html = render(collect())
    if args.stdout:
        print(html); return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"geschrieben: {out} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
