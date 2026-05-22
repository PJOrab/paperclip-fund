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
    try:
        b = (c.table("briefing_runs").select("*")
             .order("created_at", desc=True).limit(1).execute().data or [])
        briefing = b[0] if b else None
    except Exception:
        briefing = None

    return {
        "total": total,
        "by_source": dict(Counter(r["source"] for r in rows).most_common()),
        "by_adapter": dict(Counter(r["adapter"] for r in rows).most_common()),
        "recent": recent,
        "last_run": runs[0] if runs else None,
        "briefing": briefing,
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
