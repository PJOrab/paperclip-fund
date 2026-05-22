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
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ingestion.db import client

OUT_DEFAULT = os.environ.get("DASHBOARD_OUT", "/var/www/html/fund/index.html")

# Sektor-Taxonomie (HED-32, CIO-approved 2026-05-21). Namen sind die stehende
# Strategist-Referenz; in-universe Ticker stammen aus ingestion.watchlist.TICKERS.
# S4/S5/S6 sind thematisch/out-of-universe → kein Ticker → Placeholder-Kachel.
SECTOR_TAXONOMY = [
    {"id": "S1", "name": "Compute & Semis",
     "tickers": ["NVDA", "AMD", "TSM", "ASML", "AVGO", "MU", "ARM", "SMCI",
                 "QCOM", "MRVL", "INTC", "ANET", "VRT", "DELL"]},
    {"id": "S2", "name": "Hyperscaler & Big Tech",
     "tickers": ["MSFT", "GOOGL", "AMZN", "META", "AAPL"]},
    {"id": "S3", "name": "AI-Software & Apps",
     "tickers": ["PLTR", "ORCL", "NOW", "CRM", "SNOW", "CRWD", "ADBE"]},
    {"id": "S4", "name": "Models & Foundation",
     "tickers": [], "note": "Out-of-universe — kein börsennotierter Pure-Play"},
    {"id": "S5", "name": "Energy / Power / Infra",
     "tickers": [], "note": "Watchlist-Erweiterung pending Board (HED-32)"},
    {"id": "S6", "name": "Robotics & Autonomy",
     "tickers": [], "note": "Out-of-universe — thematisch beobachtet"},
]


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
        "track_record": load_track_record(),
        "sector_view": load_sector_view() or {
            "as_of": None,
            "sectors": [{"id": s["id"], "name": s["name"], "note": s.get("note"),
                         "tickers": [{"ticker": t, "price": None, "change_pct": None}
                                     for t in s["tickers"]]}
                        for s in SECTOR_TAXONOMY],
        },
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "built_at_iso": datetime.now(timezone.utc).isoformat(),
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


def load_sector_view() -> dict | None:
    """Sektor-Performance-Ansicht (HED-48). Geschrieben upstream von
    `--gen-sector-view` (Yahoo-Finance-Kurse) in sector_view.json neben diesem
    Modul; die UI rechnet nichts neu (nur change_pct-Färbung). Fehlt/kaputt → None
    → Kacheln ohne Kurse (Taxonomie-Fallback)."""
    p = Path(__file__).with_name("sector_view.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _yahoo_quote(ticker: str) -> dict | None:
    """Letzter Kurs + Vortagesschluss via öffentliches Yahoo-Chart-JSON.
    Best-effort: bei Fehler None (Kachel zeigt dann '—')."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           "?range=5d&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            meta = json.load(r)["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return None
        change = round((price - prev) / prev * 100, 2) if prev else None
        return {"ticker": ticker, "price": round(price, 2),
                "prev_close": round(prev, 2) if prev else None,
                "change_pct": change}
    except Exception:
        return None


def gen_sector_view() -> dict:
    """Baut sector_view.json: pro Sektor die in-universe Ticker + letzter
    Yahoo-Kurs. Off-build-path (Netz!), per --gen-sector-view aufgerufen."""
    sectors = []
    for s in SECTOR_TAXONOMY:
        quotes = [q for q in (_yahoo_quote(t) for t in s["tickers"]) if q]
        sectors.append({"id": s["id"], "name": s["name"],
                        "note": s.get("note"), "tickers": quotes})
    return {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "sectors": sectors}


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI/Tech Fund — Intelligence Dashboard</title>
<meta name="description" content="Autonomes Multi-Agenten-Research zu KI- und Tech-Aktien: tägliches CEO-Briefing, Thesen-Track-Record und Sektor-Ansicht.">
<meta name="theme-color" content="#0b0f17">
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='7'%20fill='%230b0f17'/%3E%3Cpath%20d='M6%2021%20L13%2014%20L18%2018%20L26%209'%20fill='none'%20stroke='%234da3ff'%20stroke-width='2.6'%20stroke-linecap='round'%20stroke-linejoin='round'/%3E%3Ccircle%20cx='26'%20cy='9'%20r='2.4'%20fill='%233fb950'/%3E%3C/svg%3E">
<meta property="og:type" content="website">
<meta property="og:site_name" content="AI/Tech Fund">
<meta property="og:locale" content="de_DE">
<meta property="og:url" content="https://hedgingalpha.com/fund/">
<meta property="og:title" content="AI/Tech Fund — Intelligence Dashboard">
<meta property="og:description" content="Autonomes Multi-Agenten-Research zu KI- und Tech-Aktien: tägliches CEO-Briefing, Thesen-Track-Record und Sektor-Ansicht.">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="AI/Tech Fund — Intelligence Dashboard">
<meta name="twitter:description" content="Autonomes Multi-Agenten-Research zu KI- und Tech-Aktien: tägliches CEO-Briefing, Thesen-Track-Record und Sektor-Ansicht.">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
:root{--bg:#0b0f17;--panel:#141a26;--panel2:#1b2333;--line:#263248;--txt:#e6edf6;
--mut:#8aa0bd;--accent:#4da3ff;--green:#3fb950;--red:#f85149;--amber:#d29922;
--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--s6:32px;
--fs-h1:22px;--fs-h2:13px;--fs-body:14px;--fs-cap:12px;--fs-micro:11px;--fs-kpi:30px;
--measure:72ch;--ok:#3fb950;--warn:#d29922;--err:#f85149;
--devil-bg:#1a1320;--devil-line:#3a2540;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:var(--fs-body)/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:var(--s5)}
h1{font-size:var(--fs-h1);margin:0}
h2{font-size:var(--fs-h2);font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);
  margin:var(--s5) 0 var(--s3);padding-bottom:var(--s2);border-bottom:1px solid var(--line)}
.sub{color:var(--mut);font-size:var(--fs-cap);margin-top:var(--s1)}
.grid{display:grid;gap:var(--s3)}
.cards{grid-template-columns:repeat(4,1fr)}
.sectors{grid-template-columns:repeat(3,1fr)}
.two-col{grid-template-columns:1fr 1fr}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:var(--s4)}
.kpi{font-size:var(--fs-kpi);font-weight:700}
.kpi small{font-size:var(--fs-h2);color:var(--mut);font-weight:400}
.kpi-dl{margin:0;display:flex;flex-direction:column-reverse}
.kpi-dl dt,.kpi-dl dd{margin:0}
/* workflow collapsible de-emphasis */
.wf-details{margin-bottom:var(--s4)}
.wf-summary{display:flex;align-items:center;gap:var(--s2);cursor:pointer;list-style:none;
  color:var(--mut);font-size:var(--fs-cap);font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  user-select:none;padding:var(--s3) 0;min-height:44px}
.wf-summary::-webkit-details-marker{display:none}
.wf-summary::before{content:"▶";font-size:9px;transition:transform .15s;display:inline-block}
.wf-details[open] .wf-summary::before{transform:rotate(90deg)}
.wf-details[open] .wf-summary{margin-bottom:var(--s2)}
/* pipeline */
.flow-wrap{position:relative}
.flow{display:flex;align-items:stretch;gap:0;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;
scrollbar-width:thin;scrollbar-color:var(--line) transparent;padding-bottom:var(--s1)}
.flow::-webkit-scrollbar{height:6px}
.flow::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px}
.flow::-webkit-scrollbar-track{background:transparent}
.flow-wrap::after{content:"";position:absolute;top:0;right:0;bottom:0;width:var(--s6);pointer-events:none;
opacity:0;transition:opacity .2s;background:linear-gradient(to left,var(--bg),transparent)}
.flow-wrap[data-overflow="1"]:not([data-end="1"])::after{opacity:1}
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
.feed .it{padding:var(--s2) 0;border-bottom:1px solid var(--line);font-size:var(--fs-h2)}
.feed .it a{color:var(--accent);text-decoration:none}
.feed .s{color:var(--mut);font-size:var(--fs-micro);text-transform:uppercase}
.thesis{border-left:3px solid var(--accent);background:var(--panel2);
border-radius:8px;padding:var(--s3);margin-bottom:10px}
.thesis .h{font-weight:600}
.dir{font-size:var(--fs-micro);padding:1px 7px;border-radius:6px;border:1px solid var(--line)}
.long{color:var(--green)}.short{color:var(--red)}.pair{color:var(--amber)}
.devil{margin-top:var(--s2);padding:var(--s2) 10px;background:var(--devil-bg);border:1px solid var(--devil-line);
border-radius:8px;font-size:var(--fs-h2)}
.devil .v{font-weight:600;text-transform:uppercase;font-size:var(--fs-micro)}
.brief{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:20px 24px;
max-width:var(--measure);margin-inline:0;line-height:1.75}
.brief h1{font-size:18px;margin:0 0 var(--s3)}.brief h2{color:var(--txt);text-transform:none;letter-spacing:0;font-size:15px;margin-top:var(--s5)}
/* lede: first paragraph elevated as abstract */
.brief-lede{font-size:15px;color:var(--txt);line-height:1.65;margin:var(--s3) 0 var(--s4);
  padding-bottom:var(--s3);border-bottom:1px solid var(--line);font-weight:400}
/* collapsible analysis body */
.brief details{margin-top:var(--s2)}
.brief summary{cursor:pointer;font-size:var(--fs-cap);font-weight:600;text-transform:uppercase;
  letter-spacing:.06em;color:var(--accent);list-style:none;padding:var(--s3) 0;min-height:44px;
  display:flex;align-items:center;border-bottom:1px solid var(--line);margin-bottom:var(--s3);user-select:none}
.brief summary::-webkit-details-marker{display:none}
.brief summary::after{content:" ▾";font-size:var(--fs-micro)}
.brief details[open] summary::after{content:" ▴";font-size:var(--fs-micro)}
.brief summary:hover{color:var(--txt)}
.muted{color:var(--mut)}
.pill{display:inline-block;font-size:var(--fs-cap);padding:2px 8px;border-radius:6px;text-transform:capitalize}
.pill--ok{background:rgba(63,185,80,.15);color:var(--ok);border:1px solid var(--ok)}
.pill--warn{background:rgba(210,153,34,.15);color:var(--warn);border:1px solid var(--warn)}
.pill--err{background:rgba(248,81,73,.15);color:var(--err);border:1px solid var(--err)}
.foot{color:var(--mut);font-size:var(--fs-cap);margin-top:var(--s6);text-align:center}
/* track-record (HED-29) */
.pill--neutral{background:rgba(138,160,189,.12);color:var(--mut);border:1px solid var(--line)}
.tr-tbl{display:grid;grid-template-columns:auto 1.4fr auto auto 1.3fr auto auto;border-collapse:collapse;width:100%;
  gap:0 var(--s3);font-size:var(--fs-h2);align-items:center}
.tr-tbl thead,.tr-tbl tbody,.tr-tbl tr{display:contents}
.tr-tbl th{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
  padding-bottom:var(--s2);border-bottom:1px solid var(--line)}
.tr-tbl td{padding:var(--s2) 0;border-bottom:1px solid var(--line)}
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
.countdown{display:inline-block;margin-top:var(--s3);background:var(--panel2);border:1px solid var(--line);
  border-radius:6px;padding:4px 10px;font-size:var(--fs-cap);color:var(--mut)}
.tr-progress{margin-top:var(--s3);max-width:28ch;margin-left:auto;margin-right:auto}
.tr-progress .tr-pb-label{display:flex;justify-content:space-between;font-size:var(--fs-cap);color:var(--mut);margin-bottom:4px}
.tr-progress .tr-pb-track{height:6px;background:var(--panel2);border-radius:3px;border:1px solid var(--line);overflow:hidden}
.tr-progress .tr-pb-fill{height:100%;border-radius:3px;background:var(--accent);transition:width .3s}
/* conviction color ramp */
.conv-lo{color:var(--mut)}
.conv-mid{color:var(--txt)}
.conv-hi{color:var(--accent);font-weight:600}
abbr[title]{text-decoration:none;cursor:help}
/* ticker deep-links */
.tkl{color:inherit;text-decoration:none;font-weight:inherit}
.tkl:hover{text-decoration:underline;text-underline-offset:2px;color:var(--accent)}
/* briefing freshness badge */
.brief-ts{font-size:var(--fs-micro);color:var(--mut);text-transform:uppercase;letter-spacing:.04em;
  margin-bottom:var(--s2)}
/* briefing processing placeholder */
.brief-processing{display:flex;align-items:center;gap:var(--s3);padding:var(--s5);
  border:1px dashed var(--line);border-radius:6px;font-size:var(--fs-body);color:var(--mut)}
.brief-proc-icon{font-size:20px;flex-shrink:0;animation:spin 2s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* heutige calls hero strip */
.calls-strip{display:flex;flex-wrap:wrap;gap:var(--s2);margin-bottom:var(--s4)}
.call-chip{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);
  border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:var(--fs-h2);white-space:nowrap}
.call-chip .ck{font-weight:700;font-size:var(--fs-body)}
.call-chip .cd{font-size:var(--fs-micro);font-weight:600;letter-spacing:.04em;padding:1px 5px;
  border-radius:4px;text-transform:uppercase}
.cd-long{background:rgba(63,185,80,.18);color:var(--green)}
.cd-short{background:rgba(248,81,73,.18);color:var(--red)}
.cd-pair{background:rgba(210,153,34,.18);color:var(--amber)}
.call-chip .cc{color:var(--mut);font-size:var(--fs-cap);cursor:help;
  border-bottom:1px dotted currentColor;border-bottom-color:rgba(125,125,125,.5)}
.call-chip--empty{opacity:.55;border-style:dashed}
/* hover feedback on interactive cards/tiles/rows */
.panel{transition:border-color .15s,background .15s}
.panel:hover{border-color:var(--accent);background:var(--panel2)}
.sec-tile{transition:border-color .15s,background .15s}
.sec-tile:hover{border-color:var(--accent);background:var(--panel2)}
.sec-row{transition:background .12s}
.sec-row:hover{background:rgba(77,163,255,.07);border-radius:4px}
/* sector view (HED-48) */
.sec-tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:var(--s4)}
.sec-head{display:flex;align-items:baseline;gap:var(--s2);margin-bottom:var(--s3)}
.sec-head .id{font-size:var(--fs-micro);font-weight:700;color:var(--accent);letter-spacing:.06em}
.sec-head .nm{font-weight:600}
.sec-head .ct{margin-left:auto;color:var(--mut);font-size:var(--fs-micro)}
.sec-row{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s3);
  padding:var(--s2) 0;border-bottom:1px solid var(--line);font-size:var(--fs-h2)}
.sec-row:last-child{border-bottom:0}
.sec-row .tk{font-weight:600}
.sec-row .px{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.sec-row .ch{font-variant-numeric:tabular-nums;font-size:var(--fs-cap);min-width:62px;text-align:right}
.sec-ph{color:var(--mut);font-size:var(--fs-h2);padding:var(--s2) 0}
@media (max-width:760px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .sectors{grid-template-columns:1fr}
  .two-col{grid-template-columns:1fr}
  .flow{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .tr-tbl{display:block}
  .tr-tbl thead{display:none}
  .tr-tbl tbody,.tr-tbl tr{display:block}
  .tr-tbl tr{padding:var(--s3) 0;border-bottom:1px solid var(--line)}
  .tr-tbl td{display:flex;justify-content:space-between;gap:var(--s3);padding:2px 0;border:0;text-align:right}
  .tr-tbl .num{text-align:right}
  .tr-tbl .dlabel{display:inline-block;min-width:90px;vertical-align:top}
}
/* skip-to-content link: bypass plumbing/workflow chrome (WCAG 2.4.1 Bypass Blocks) */
.skip-link{position:absolute;left:var(--s4);top:-48px;z-index:100;
  background:var(--accent);color:#06121f;font-size:var(--fs-cap);font-weight:600;
  padding:var(--s2) var(--s4);border-radius:0 0 8px 8px;text-decoration:none;
  transition:top .15s}
.skip-link:focus{top:0}
main:focus{outline:none}
/* focus-visible ring: brand-consistent keyboard nav across all interactive elements (WCAG 2.4.7) */
:focus{outline:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.wf-summary:focus-visible,.brief summary:focus-visible,.df-summary:focus-visible{
  outline-offset:4px;border-radius:6px}
.panel:focus-visible,.sec-tile:focus-visible{
  outline-offset:0;border-radius:12px}
.tkl:focus-visible{border-radius:2px}
/* print stylesheet: CEO-friendly hard-copy of briefing (no dark bg, no hidden sections) */
@media print{
  :root{--bg:#fff;--panel:#f5f7fa;--panel2:#edf0f5;--line:#cdd3de;--txt:#0d1117;
    --mut:#4a5568;--accent:#1a6abf;--green:#276749;--red:#9b1c1c;--amber:#92400e}
  body{background:#fff;color:#0d1117}
  .wrap{max-width:100%;padding:0}
  /* force-open all collapsibles */
  details{display:block}
  details>summary{display:none}
  .wf-details>*:not(summary),.brief>*:not(summary),.df-details>*:not(summary){display:block!important}
  /* strip hover/transition chrome */
  .panel:hover,.sec-tile:hover,.sec-row:hover{background:var(--panel)!important;border-color:var(--line)!important}
  /* page-break hints */
  h2{page-break-after:avoid}
  .panel,.sec-tile{page-break-inside:avoid}
  /* hide plumbing sections (workflow, datenfeed) and inline scripts */
  .wf-details,.df-details{display:none}
  /* reset dark-mode backgrounds */
  .panel,.sec-tile,.step,.call-chip{background:var(--panel)!important;border-color:var(--line)!important}
  .brief-lede{background:var(--panel)!important;border-color:var(--line)!important}
  /* links: show href after text for print context */
  .tkl::after{content:" (" attr(href) ")";font-size:10px;color:var(--mut)}
}
/* respect reduced-motion: kill the infinite spinner + all transitions (WCAG 2.3.3, vestibular safety) */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important;scroll-behavior:auto!important}
  .brief-proc-icon{animation:none!important}
}
</style></head>
<body>
<a href="#main" class="skip-link">Zum Briefing springen</a>
<div class="wrap">
  <header style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px">
    <div><h1>🤖 AI/Tech Fund — Intelligence Dashboard</h1>
    <div class="sub">Live-Feed → Agenten-Gremium → CEO-Briefing · MVP</div></div>
    <div class="sub">aktualisiert: <span id="built"></span></div>
  </header>

  <details class="wf-details">
    <summary class="wf-summary">Workflow — Pipeline-Status</summary>
    <div class="flow-wrap"><div class="flow" id="flow"></div></div>
  </details>

  <main id="main" tabindex="-1">
  <h2>Letztes Briefing</h2>
  <div id="briefing" aria-live="polite" aria-atomic="false"></div>

  <h2>Thesen-Track-Record <span id="trstand" class="tag"></span></h2>
  <div id="trackrecord" aria-live="polite" aria-atomic="false"></div>

  <h2>Sektor-Ansicht <span id="secstand" class="tag"></span></h2>
  <div class="grid sectors" id="sectorview"></div>
  </main>

  <details class="wf-details">
    <summary class="wf-summary">Datenfeed <span id="feedstale" style="margin-left:4px"></span> — Ingest-Status</summary>
    <div class="grid cards" id="kpis"></div>
    <div class="grid two-col" style="margin-top:14px">
      <div class="panel"><div class="muted" style="margin-bottom:8px">Quellen</div><div id="sources"></div></div>
      <div class="panel"><div class="muted" style="margin-bottom:8px">Neueste Items</div><div class="feed" id="feed"></div></div>
    </div>
  </details>

  <div class="foot">AI/Tech Fund · generiert aus Supabase · keine Secrets im Browser</div>
</div>
<script>
const D = __DATA__;
const $ = (id)=>document.getElementById(id);
$("built").innerHTML = `<time datetime="${D.built_at_iso||''}">${D.built_at}</time>`;

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
// Scroll affordance: show right-edge fade only while the strip overflows and more remains right (HED-47)
(function(){
  const fw=$("flow"), wrap=fw.parentElement;
  function upd(){
    const ov=fw.scrollWidth>fw.clientWidth+1;
    const end=fw.scrollLeft+fw.clientWidth>=fw.scrollWidth-1;
    wrap.dataset.overflow=ov?"1":"0";
    wrap.dataset.end=end?"1":"0";
  }
  fw.addEventListener("scroll",upd,{passive:true});
  window.addEventListener("resize",upd);
  upd();
})();

// KPIs
const lr = D.last_run||{};
function statusPill(s){
  const map={done:"ok",completed:"ok",running:"warn",queued:"warn",pending:"warn",error:"err",failed:"err"};
  const k=map[(s||"").toLowerCase()]||"warn";
  const icon={ok:"✓",warn:"⚠",err:"✗"}[k]||"";
  return `<span class="pill pill--${k}">${icon} ${esc(s)}</span>`;
}
const bstatus = (D.briefing && D.briefing.status) ? D.briefing.status : null;
$("kpis").innerHTML = [
  ["raw_items gesamt", D.total],
  ["Quellen", Object.keys(D.by_source).length],
  ["letzter Ingest", lr.items_inserted!=null?("+"+lr.items_inserted):"—"],
  ["Briefing-Status", bstatus? statusPill(bstatus):"—"]
].map(([k,v])=>`<dl class="panel kpi-dl"><dt class="muted">${k}</dt><dd class="kpi">${v}</dd></dl>`).join("");

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
  // Freshness badge: relative time from created_at (Goal-Gradient, Information Scent)
  if(b.created_at){
    const ago=Date.now()-new Date(b.created_at).getTime();
    const mins=Math.round(ago/60000);
    const fresh=mins<2?"gerade eben":mins<60?`vor ${mins} Min.`:mins<1440?`vor ${Math.round(mins/60)} Std.`:`vor ${Math.round(mins/1440)} Tag${Math.round(mins/1440)===1?"":"en"}`;
    html+=`<div class="brief-ts">Briefing · <time datetime="${new Date(b.created_at).toISOString()}" title="${b.created_at}">${fresh}</time></div>`;
  }
  // "Heutige Calls" hero strip: compact chips before prose (Recognition>Recall, Goal-Gradient, F-pattern lede)
  // Conviction color ramp: low→mut, mid→txt, high→accent
  const convCls=c=>c==null?"":c>=0.6?"conv-hi":c>=0.35?"conv-mid":"conv-lo";
  const convLabel=c=>c==null?"":c>=0.6?"hoch":c>=0.35?"mittel":"niedrig";
  const convTip=c=>`Conviction ${c.toFixed(2)} — ${convLabel(c)}. Überzeugungsgrad der These (0–1): <0,35 niedrig · 0,35–0,6 mittel · ≥0,6 hoch.`;
  if(theses.length){
    const dirCls=d=>d==="long"?"cd-long":d==="short"?"cd-short":"cd-pair";
    const chips=theses.map(t=>{
      const tks=(t.tickers||[]).join(" · ")||"?";
      const dir=t.direction||"pair";
      const conv=t.conviction!=null?`<abbr title="Conviction">Conv</abbr> ${t.conviction.toFixed(2)}`:"";
      return `<div class="call-chip"><span class="ck">${esc(tks)}</span>`+
        `<span class="cd ${dirCls(dir)}">${esc(dir)}</span>`+
        (conv?`<span class="cc ${convCls(t.conviction)}" title="${esc(convTip(t.conviction))}" aria-label="${esc(convTip(t.conviction))}">${conv}</span>`:"")+
        `</div>`;
    }).join("");
    html+=`<div class="calls-strip">${chips}</div>`;
  } else {
    html+=`<div class="calls-strip"><div class="call-chip call-chip--empty"><span class="cc">Kein aktiver Call heute</span></div></div>`;
  }
  if(!b.briefing_md){
    html+=`<div class="panel brief-processing"><span class="brief-proc-icon">⏳</span><span class="muted">Briefing wird verarbeitet…</span></div>`;
  } else if(b.briefing_md){
    // Progressive Disclosure: elevate lede (first <p>) + collapse dense analysis body
    const raw = marked.parse(b.briefing_md);
    const tmp = document.createElement("div"); tmp.innerHTML = raw;
    const nodes = Array.from(tmp.childNodes);
    // find first non-empty <p> as lede
    const ledeIdx = nodes.findIndex(n=>n.nodeName==="P" && (n.textContent||"").trim().length>0);
    let briefHtml = "";
    if(ledeIdx>=0){
      // headings before lede (e.g. h1 title) pass through unchanged
      for(let i=0;i<ledeIdx;i++) briefHtml+=nodes[i].outerHTML||"";
      briefHtml+=`<p class="brief-lede">${nodes[ledeIdx].innerHTML}</p>`;
      const rest=nodes.slice(ledeIdx+1).map(n=>n.outerHTML||n.textContent||"").join("");
      if(rest.trim()) briefHtml+=`<details open><summary>Vollanalyse</summary>${rest}</details>`;
    } else {
      briefHtml=raw;
    }
    html+=`<div class="brief">${briefHtml}</div>`;
  }
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
// Thesen-Track-Record (HED-29)
function pct(x){return x==null?"—":Math.round(x*100)+"%";}
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
  let pts=buckets.map(b=>{
    const r=Math.min(8,3+(b.n||1));
    return `<circle cx="${X(b.conviction).toFixed(1)}" cy="${Y(b.observed_hit_rate).toFixed(1)}" r="${r}" `+
      `fill="var(--accent)" fill-opacity=".8"><title>conv ${pct(b.conviction)} · hit ${pct(b.observed_hit_rate)} · n=${b.n}</title></circle>`;
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
  // KPI-Strip
  $("trackrecord").innerHTML = `<div class="grid cards" id="trkpi"></div><div id="trbody" style="margin-top:14px"></div>`;
  const biasTxt = a.calibration_bias==null ? "—"
    : (a.calibration_bias>=0?"+":"−")+Math.abs(a.calibration_bias*100).toFixed(0)+"%";
  $("trkpi").innerHTML=[
    ["Hit-Rate", pct(a.hit_rate)],
    ["gewertet", scored+" / "+(a.total??"—")],
    ["too early", a.too_early??"—"],
    ['<abbr title="Kalibrierungs-Bias">Kalib.-Bias</abbr>', biasTxt]
  ].map(([k,v])=>`<dl class="panel kpi-dl"><dt class="muted">${k}</dt><dd class="kpi">${v}</dd></dl>`).join("");
  // Body: happy-path table+chart, oder Empty/Too-Early-State
  const scoredTheses=(tr.theses||[]).filter(t=>t.verdict && t.verdict!=="too_early");
  if(scored>0 && scoredTheses.length){
    const head=["Datum","These","Richtung","Conviction","Kurs","Move %","Verdikt"];
    const order={miss:0,hit:1,neutral:2,too_early:3};
    const rows=scoredTheses.slice().sort((x,y)=>
      (order[x.verdict]-order[y.verdict])||((y.conviction||0)-(x.conviction||0)));
    let tbl=`<table class="tr-tbl" aria-label="Thesen Track-Record"><thead><tr>${
      head.map(h=>`<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>`;
    tbl+=rows.map(t=>{
      const dev=t.devil?`<span class="devsig" title="⚖ Devil (${esc(t.devil.verdict||"?")}): ${esc(t.devil.note||"")}">⚖</span>`:"";
      const kurs=t.baseline_price!=null?`${t.baseline_price}${t.current_price!=null?" → "+t.current_price:""}`:"—";
      return `<tr>
        <td class="num"><span class="dlabel">Datum </span>${esc(t.date||"—")}</td>
        <td class="tr-lbl"><span class="dlabel">These </span><span class="t">${esc(t.label||"")}</span> <span class="tk">${(t.tickers||[]).map(tk=>`<a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">${esc(tk)}</a>`).join(", ")}</span></td>
        <td><span class="dlabel">Richtung </span><span class="dir ${dirClass(t.direction)}">${esc(t.direction||"")}</span></td>
        <td class="num"><span class="dlabel">Conviction </span><span class="${convCls(t.conviction)}">${t.conviction!=null?t.conviction.toFixed(2):"—"}</span></td>
        <td class="num"><span class="dlabel">Kurs </span>${esc(kurs)}</td>
        <td class="num"><span class="dlabel">Move </span>${moveCell(t.move_pct)}</td>
        <td><span class="dlabel">Verdikt </span>${verdictPill(t.verdict)}${dev}</td>
      </tr>`;}).join("");
    tbl+=`</tbody></table>`;
    $("trbody").innerHTML=`<div class="grid two-col">
      <div class="panel">${tbl}</div>
      <div class="panel"><div class="muted" style="margin-bottom:8px">Conviction-Kalibrierung</div>
        <div class="calib">${calibSvg(tr.calibration_buckets)}</div></div></div>`;
  } else {
    // Empty / Too-Early-State (geht zuerst live)
    const esd=tr.earliest_score_date;
    let cd="";
    if(esd){
      const days=Math.ceil((new Date(esd+"T00:00:00Z")-Date.now())/864e5);
      // Progress affordance: infer window as 21 days; show elapsed fraction toward scoring date
      const windowDays=21;
      const elapsed=Math.max(0,Math.min(windowDays,windowDays-days));
      const pct=Math.round((elapsed/windowDays)*100);
      const label=days>0?`Erste Wertung in ${days} Tag${days===1?"":"en"} (${esc(esd)})`:`Wertung fällig ab ${esc(esd)}`;
      cd=`<div class="tr-progress">
        <div class="tr-pb-label"><span>Reifung</span><span>${pct}%</span></div>
        <div class="tr-pb-track" title="${label}"><div class="tr-pb-fill" style="width:${pct}%"></div></div>
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

// Sektor-Ansicht (HED-48)
(function renderSectors(){
  const sv=D.sector_view, root=$("sectorview");
  if(!sv || !(sv.sectors||[]).length){
    root.innerHTML='<div class="panel muted">Sektor-Ansicht noch nicht verfügbar.</div>'; return; }
  $("secstand").innerHTML = sv.as_of
    ? `Kurse <time datetime="${(sv.as_of_iso||sv.as_of.replace(' UTC','').replace(' ','T')+'Z')}">${sv.as_of}</time>`
    : "Taxonomie (ohne Kurse)";
  function chCell(c){
    if(c==null) return '<span class="ch muted">—</span>';
    const up=c>=0;
    const arrow=up?"▲":"▼";
    return `<span class="ch ${up?"move-up":"move-dn"}" aria-label="${up?"steigt":"fällt"} ${Math.abs(c).toFixed(1)} Prozent">${arrow} ${up?"+":"−"}${Math.abs(c).toFixed(1)}%</span>`;
  }
  // Sort: sectors with price data first, sorted by max absolute move (most volatile first)
  const sorted=(sv.sectors||[]).slice().sort((a,b)=>{
    const maxAbs=s=>Math.max(...(s.tickers||[]).map(t=>t.change_pct!=null?Math.abs(t.change_pct):0),0);
    const hasTk=s=>(s.tickers||[]).length>0;
    if(hasTk(a)!==hasTk(b)) return hasTk(b)-hasTk(a);
    return maxAbs(b)-maxAbs(a);
  });
  root.innerHTML = sorted.map(s=>{
    const tks=s.tickers||[];
    let body;
    if(tks.length){
      body = tks.map(t=>{
        const px = t.price!=null ? t.price : '<span class="muted">—</span>';
        return `<div class="sec-row"><a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(t.ticker)}" target="_blank" rel="noopener">${esc(t.ticker)}</a>
          <span class="px">${px}</span>${chCell(t.change_pct)}</div>`;
      }).join("");
    } else {
      body = `<div class="sec-ph">${esc(s.note||"Keine in-universe Ticker.")}</div>`;
    }
    return `<div class="sec-tile"><div class="sec-head">
        <span class="id">${esc(s.id)}</span><span class="nm">${esc(s.name)}</span>
        <span class="ct">${tks.length||""}</span></div>${body}</div>`;
  }).join("");
})();

function esc(s){return (s||"").replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));}
</script></body></html>"""


def render(data: dict) -> str:
    return HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False, default=str))


def main():
    ap = argparse.ArgumentParser(description="AI/Tech Fund — Dashboard-Generator")
    ap.add_argument("--stdout", action="store_true", help="HTML auf stdout statt Datei")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--gen-sector-view", action="store_true",
                    help="sector_view.json aus Yahoo-Finance-Kursen neu bauen (Netz)")
    args = ap.parse_args()

    if args.gen_sector_view:
        p = Path(__file__).with_name("sector_view.json")
        p.write_text(json.dumps(gen_sector_view(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"geschrieben: {p}")
        return

    html = render(collect())
    if args.stdout:
        print(html); return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"geschrieben: {out} ({len(html)} bytes)")


if __name__ == "__main__":
    main()



