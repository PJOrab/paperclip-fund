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
    rows = c.table("raw_items").select("source,adapter").execute().data or []
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
--mut:#8aa0bd;--accent:#4da3ff;--green:#3fb950;--red:#f85149;--amber:#d29922;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:28px 0 12px}
.sub{color:var(--mut);font-size:12px;margin-top:4px}
.grid{display:grid;gap:14px}
.cards{grid-template-columns:repeat(4,1fr)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi{font-size:30px;font-weight:700}
.kpi small{font-size:13px;color:var(--mut);font-weight:400}
/* pipeline */
.flow{display:flex;align-items:stretch;gap:0;flex-wrap:wrap}
.step{flex:1;min-width:120px;background:var(--panel2);border:1px solid var(--line);
border-radius:10px;padding:12px;text-align:center;position:relative}
.step .t{font-weight:600}.step .m{color:var(--mut);font-size:12px;margin-top:3px}
.arrow{display:flex;align-items:center;color:var(--accent);font-size:20px;padding:0 6px}
.tag{display:inline-block;background:var(--panel2);border:1px solid var(--line);
border-radius:6px;padding:1px 7px;font-size:12px;color:var(--mut)}
.bar{height:8px;background:var(--panel2);border-radius:6px;overflow:hidden;margin-top:4px}
.bar>span{display:block;height:100%;background:var(--accent)}
.srcrow{display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px}
.feed{max-height:320px;overflow:auto}
.feed .it{padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}
.feed .it a{color:var(--accent);text-decoration:none}
.feed .s{color:var(--mut);font-size:11px;text-transform:uppercase}
.thesis{border-left:3px solid var(--accent);background:var(--panel2);
border-radius:8px;padding:12px;margin-bottom:10px}
.thesis .h{font-weight:600}
.dir{font-size:11px;padding:1px 7px;border-radius:6px;border:1px solid var(--line)}
.long{color:var(--green)}.short{color:var(--red)}.pair{color:var(--amber)}
.devil{margin-top:8px;padding:8px 10px;background:#1a1320;border:1px solid #3a2540;
border-radius:8px;font-size:13px}
.devil .v{font-weight:600;text-transform:uppercase;font-size:11px}
.brief{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:18px}
.brief h1{font-size:18px}.brief h2{color:var(--txt);text-transform:none;letter-spacing:0;font-size:15px}
.muted{color:var(--mut)}
.status{font-size:11px;padding:2px 8px;border-radius:6px;border:1px solid var(--line)}
.foot{color:var(--mut);font-size:12px;margin-top:28px;text-align:center}
</style></head>
<body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px">
    <div><h1>🤖 AI/Tech Fund — Intelligence Dashboard</h1>
    <div class="sub">Live-Feed → Agenten-Gremium → CEO-Briefing · MVP</div></div>
    <div class="sub">aktualisiert: <span id="built"></span></div>
  </div>

  <h2>Workflow</h2>
  <div class="flow" id="flow"></div>

  <h2>Datenfeed</h2>
  <div class="grid cards" id="kpis"></div>
  <div class="grid" style="grid-template-columns:1fr 1fr;margin-top:14px">
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
  {t:"Quellen",m:"EDGAR·arXiv·HN·GitHub·X·News"},
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
$("kpis").innerHTML = [
  ["raw_items gesamt", D.total],
  ["Quellen", Object.keys(D.by_source).length],
  ["letzter Ingest", lr.items_inserted!=null?("+"+lr.items_inserted):"—"],
  ["Briefing-Status", D.briefing? (D.briefing.status||"—"):"—"]
].map(([k,v])=>`<div class="panel"><div class="kpi">${v}</div><div class="muted">${k}</div></div>`).join("");

// Quellen-Balken
const max = Math.max(1,...Object.values(D.by_source));
$("sources").innerHTML = Object.entries(D.by_source).map(([s,n])=>
  `<div class="srcrow"><span>${s}</span><span>${n}</span></div>
   <div class="bar"><span style="width:${Math.round(n/max*100)}%"></span></div>`).join("");

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
