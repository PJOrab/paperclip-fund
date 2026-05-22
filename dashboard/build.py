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
             .select("id,created_at,status,theses,devils_advocate,window_hours")
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
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


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
--fs-h1:22px;--fs-h2:13px;--fs-body:14px;--fs-cap:12px;--fs-kpi:30px;
--measure:72ch;--ok:#3fb950;--warn:#d29922;--err:#f85149;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:var(--fs-body)/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:var(--s5)}
h1{font-size:var(--fs-h1);margin:0}
h2{font-size:var(--fs-h2);text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:var(--s6) 0 var(--s3)}
.sub{color:var(--mut);font-size:var(--fs-cap);margin-top:var(--s1)}
.grid{display:grid;gap:var(--s3)}
.cards{grid-template-columns:repeat(4,1fr)}
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
@media (max-width:760px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .two-col{grid-template-columns:1fr}
  .flow{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}
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
$("kpis").innerHTML = [
  ["raw_items gesamt", D.total],
  ["Quellen", Object.keys(D.by_source).length],
  ["letzter Ingest", lr.items_inserted!=null?("+"+lr.items_inserted):"—"],
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
