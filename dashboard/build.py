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
# S4/S6 bleiben out-of-universe (kein börsennotierter Pure-Play). S5 jetzt aktiv.
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
     "tickers": ["VST", "CEG", "GEV", "ETN"]},
    {"id": "S6", "name": "Robotics & Autonomy",
     "tickers": [], "note": "Out-of-universe — thematisch beobachtet"},
]


def collect() -> dict:
    c = client()
    total = c.table("raw_items").select("id", count="exact").limit(1).execute().count
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


def _rsi14(closes: list[float]) -> float | None:
    """RSI-14 from a list of daily closes (need ≥15 values)."""
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, 15):
        d = closes[-14 + i] - closes[-14 + i - 1]
        (gains if d >= 0 else losses).append(abs(d))
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _yahoo_quote(ticker: str) -> dict | None:
    """Letzter Kurs + Vortagesschluss + 52-Wochen-Range + MA30 + RSI14 via Yahoo-Chart-JSON.
    range=3mo/interval=1d: enough closes for MA30 and RSI14; chartPreviousClose in meta
    gives yesterday's official session close for accurate 1-day change_pct."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           "?range=3mo&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.load(r)
        result0 = body["chart"]["result"][0]
        meta = result0["meta"]
        closes = (result0.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
        closes = [c for c in closes if c is not None]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return None
        change = round((price - prev) / prev * 100, 2) if prev else None
        w52_high = meta.get("fiftyTwoWeekHigh")
        w52_low = meta.get("fiftyTwoWeekLow")
        pct_of_52w_high = (round(price / w52_high * 100, 1)
                           if w52_high and w52_high > 0 else None)
        # MA30: simple 30-day moving average of closes
        ma30 = round(sum(closes[-30:]) / len(closes[-30:]), 2) if len(closes) >= 30 else None
        rsi14 = _rsi14(closes)
        result = {"ticker": ticker, "price": round(price, 2),
                  "prev_close": round(prev, 2) if prev else None,
                  "change_pct": change}
        if w52_high:
            result["w52_high"] = round(w52_high, 2)
        if w52_low:
            result["w52_low"] = round(w52_low, 2)
        if pct_of_52w_high is not None:
            result["pct_of_52w_high"] = pct_of_52w_high
        if ma30 is not None:
            result["ma30"] = ma30
            result["pct_vs_ma30"] = round((price - ma30) / ma30 * 100, 1)
        if rsi14 is not None:
            result["rsi14"] = rsi14
        # Sparkline: last 30 closes (rounded to 2dp) for mini chart in sector tile
        if len(closes) >= 5:
            spark = [round(c, 2) for c in closes[-30:]]
            result["spark"] = spark
        return result
    except Exception:
        return None


def _earnings_calendar() -> list[dict]:
    """Fetch upcoming earnings dates for all watchlist tickers via yfinance.
    Returns list of {ticker, date, days_out} sorted by date, capped at 14 days out."""
    try:
        import yfinance as yf
        from datetime import date as _date
        today = _date.today()
        results = []
        all_tickers = [t for s in SECTOR_TAXONOMY for t in s["tickers"]]
        for ticker in all_tickers:
            try:
                info = yf.Ticker(ticker).calendar
                if info is None:
                    continue
                # calendar returns dict with 'Earnings Date' as a list of timestamps
                ed = info.get("Earnings Date") or info.get("earningsDate") or []
                if hasattr(ed, "tolist"):
                    ed = ed.tolist()
                for dt in (ed[:2] if isinstance(ed, list) else []):
                    try:
                        if hasattr(dt, "date"):
                            d = dt.date()
                        else:
                            from datetime import datetime as _dt
                            d = _dt.fromisoformat(str(dt)[:10]).date()
                        days_out = (d - today).days
                        if 0 <= days_out <= 14:
                            results.append({"ticker": ticker, "date": d.isoformat(), "days_out": days_out})
                            break
                    except Exception:
                        continue
            except Exception:
                continue
        results.sort(key=lambda x: x["date"])
        return results
    except Exception:
        return []


def gen_sector_view() -> dict:
    """Baut sector_view.json: pro Sektor die in-universe Ticker + letzter
    Yahoo-Kurs. Off-build-path (Netz!), per --gen-sector-view aufgerufen."""
    sectors = []
    for s in SECTOR_TAXONOMY:
        quotes = [q for q in (_yahoo_quote(t) for t in s["tickers"]) if q]
        sectors.append({"id": s["id"], "name": s["name"],
                        "note": s.get("note"), "tickers": quotes})
    earnings_cal = _earnings_calendar()
    return {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "as_of_iso": datetime.now(timezone.utc).isoformat(),
            "sectors": sectors,
            "earnings_calendar": earnings_cal}


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
:root{color-scheme:dark;--bg:#0b0f17;--panel:#141a26;--panel2:#1b2333;--line:#263248;--txt:#e6edf6;
--mut:#8aa0bd;--accent:#4da3ff;--green:#3fb950;--red:#f85149;--amber:#d29922;
--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--s6:32px;
--fs-h1:22px;--fs-h2:13px;--fs-body:14px;--fs-cap:12px;--fs-micro:11px;--fs-kpi:30px;
--measure:72ch;--ok:#3fb950;--warn:#d29922;--err:#f85149;
--devil-bg:#1a1320;--devil-line:#3a2540;}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
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
#briefing{min-height:220px}#trackrecord{min-height:120px}#sectorview{min-height:140px}
/* loading skeleton: animated placeholder fills reserved space while CDN/JS loads (Doherty, perceived performance) */
.skel{position:relative;overflow:hidden;background:var(--panel2);border-radius:8px}
.skel::after{content:"";position:absolute;inset:0;transform:translateX(-100%);
  background:linear-gradient(90deg,transparent,rgba(230,237,246,.07),transparent);
  animation:skel-shimmer 1.4s ease-in-out infinite}
@keyframes skel-shimmer{100%{transform:translateX(100%)}}
.skel-line{height:12px;margin:10px 0;border-radius:6px}
.skel-chip{height:30px;width:160px;display:inline-block;margin:0 var(--s2) var(--s3) 0;border-radius:8px}
.skel-tile{height:120px;border-radius:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:var(--s4)}
.kpi{font-size:var(--fs-kpi);font-weight:700;font-variant-numeric:tabular-nums}
.kpi small{font-size:var(--fs-h2);color:var(--mut);font-weight:400}
/* pending KPI: not-yet-measurable figure reads as intentional, not broken/missing data */
.kpi--pending{color:var(--mut);font-weight:400;cursor:help}
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
border-radius:8px;padding:var(--s3);margin-bottom:10px;scroll-margin-top:80px}
.thesis:target{outline:2px solid var(--accent);outline-offset:2px}
.thesis .h{font-weight:600}
.devil{margin-top:var(--s2);padding:var(--s2) 10px;background:var(--devil-bg);border:1px solid var(--devil-line);
border-radius:8px;font-size:var(--fs-h2)}
.devil .v{font-weight:600;text-transform:uppercase;font-size:var(--fs-micro)}
.brief{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:20px 24px;
max-width:var(--measure);margin-inline:0;line-height:1.75}
/* two-column briefing on wide viewports: prose left at reading measure, thesis+Devil's-Advocate cards right — fills the desktop void beside the 72ch column, surfaces counter-arguments next to the call, shortens scroll (Gestalt Common Region/Proximity, F-pattern, Goal-Gradient) */
.brief-region{display:block}
.brief-aside-h2{margin-top:20px}
@media (min-width:1100px){
  .brief-region{display:grid;grid-template-columns:var(--measure) minmax(0,1fr);gap:var(--s5);align-items:start}
  .brief-region .brief{max-width:none;width:100%}
  .brief-aside-h2{margin-top:0}
  /* the thesis-card aside is usually far taller than the prose; pin the prose so the briefing summary stays in view while scrolling the counter-arguments, instead of leaving a dead left void (Gestalt common region, Goal-Gradient, "no dead space"). Falls back to static when the prose itself exceeds the viewport. */
  .brief-main{position:sticky;top:var(--s4);align-self:start}
}
.brief h1{font-size:18px;margin:0 0 var(--s3)}.brief h2{color:var(--txt);text-transform:none;letter-spacing:0;font-size:15px;margin-top:var(--s5)}
/* briefing title line (bold-only first paragraph) styled as a heading */
.brief-title{font-size:16px;font-weight:700;color:var(--txt);margin:0 0 var(--s3)}
/* lede: first real prose paragraph elevated as abstract */
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
.foot{color:var(--mut);font-size:var(--fs-cap);margin-top:var(--s6);text-align:center;border-top:1px solid var(--line);padding-top:var(--s4)}
.foot-disclaimer{max-width:var(--measure);margin:0 auto var(--s2);font-size:var(--fs-micro);line-height:1.5}
.foot-meta{margin:0}
/* track-record (HED-29) */
.pill--neutral{background:rgba(138,160,189,.12);color:var(--mut);border:1px solid var(--line)}
.tr-tbl{display:grid;grid-template-columns:auto 1.4fr auto auto 1.3fr auto auto;border-collapse:collapse;width:100%;
  gap:0 var(--s3);font-size:var(--fs-h2);align-items:center}
.tr-tbl thead,.tr-tbl tbody,.tr-tbl tr{display:contents}
.tr-tbl th{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
  padding-bottom:var(--s2);border-bottom:1px solid var(--line)}
.tr-tbl td,.tr-tbl tbody th{padding:var(--s2) 0;border-bottom:1px solid var(--line)}
.tr-tbl tbody th{font-weight:inherit;text-align:left;text-transform:none;letter-spacing:0;color:inherit}
/* screen-reader-only caption: semantic label without duplicating the visible h2 */
.tr-cap{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}
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
.tr-pending{width:100%;border-collapse:collapse;margin-top:var(--s4);font-size:var(--fs-cap)}
.tr-pending thead th{color:var(--mut);font-weight:500;text-align:left;padding:3px 8px 3px 0;border-bottom:1px solid var(--line);white-space:nowrap}
.tr-pending td,.tr-pending tbody th{padding:5px 8px 5px 0;border-bottom:1px solid var(--panel2);vertical-align:top}
.tr-pending tbody th{font-weight:400;text-align:left}
.tr-pending tr:last-child td,.tr-pending tr:last-child th{border-bottom:none}
.tr-pending .sd{color:var(--mut);white-space:nowrap}
.exit-trigger{font-size:var(--fs-cap);color:var(--mut);margin-top:3px;max-width:320px;white-space:normal;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;cursor:help}
.exit-trigger span{font-style:italic}
.sc-line{font-size:var(--fs-cap);margin-top:2px}
/* portfolio view */
.pf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:var(--s3);margin-bottom:var(--s3)}
.pf-bar-wrap{margin-top:var(--s3)}
.pf-bar-label{display:flex;justify-content:space-between;font-size:var(--fs-cap);color:var(--mut);margin-bottom:4px}
.pf-bar-track{height:8px;background:var(--panel2);border-radius:4px;overflow:hidden;margin-bottom:var(--s2)}
.pf-bar-fill{height:100%;border-radius:4px;transition:width .3s}
.pf-bar-long{background:var(--accent)}
.pf-bar-short{background:#f78166}
.pf-sec-row{display:flex;align-items:center;gap:var(--s3);font-size:var(--fs-cap);padding:3px 0}
.pf-sec-name{flex:1;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pf-sec-bar{flex:0 0 80px;height:6px;background:var(--accent);border-radius:3px}
.pf-sec-pct{width:32px;text-align:right;color:var(--mut)}
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
.brief-ts--stale{color:var(--amber);font-weight:600}
.brief-ts--stale .brief-ts-mark{margin-right:var(--s1)}
/* build-staleness: header timestamp goes amber when the dashboard itself hasn't rebuilt (deploy-bridge stall) */
.built--stale{color:var(--amber);font-weight:600}
.built--stale .build-stale-mark{margin-right:var(--s1)}
/* noscript fallback: amber-bordered panel visible only when JS is disabled */
.noscript-panel{border-color:var(--amber);max-width:var(--measure);
  margin:var(--s5) 0;text-align:center;padding:var(--s5)}
.noscript-icon{font-size:24px;margin-bottom:var(--s2)}
/* briefing processing placeholder */
.brief-processing{display:flex;align-items:center;gap:var(--s3);padding:var(--s5);
  border:1px dashed var(--line);border-radius:6px;font-size:var(--fs-body);color:var(--mut)}
.brief-proc-icon{font-size:20px;flex-shrink:0;animation:spin 2s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* heutige calls hero strip */
.calls-strip{display:flex;flex-wrap:wrap;gap:var(--s2);margin-bottom:var(--s4)}
.call-chip{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);cursor:pointer;
  border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:var(--fs-h2);white-space:nowrap;
  text-decoration:none;color:var(--txt);transition:border-color .12s,background .12s}
a.call-chip:hover{border-color:var(--accent);background:var(--panel)}
.call-chip .ck{font-weight:700;font-size:var(--fs-body)}
.cd{display:inline-block;font-size:var(--fs-micro);font-weight:600;letter-spacing:.04em;padding:1px 5px;
  border-radius:4px;text-transform:uppercase}
.cd-long{background:rgba(63,185,80,.18);color:var(--green)}
.cd-short{background:rgba(248,81,73,.18);color:var(--red)}
.cd-pair{background:rgba(210,153,34,.18);color:var(--amber)}
.call-chip .cc{color:var(--mut);font-size:var(--fs-cap);cursor:help;font-variant-numeric:tabular-nums;
  border-bottom:1px dotted currentColor;border-bottom-color:rgba(125,125,125,.5)}
.call-chip .call-move{font-size:var(--fs-cap);font-weight:600;font-variant-numeric:tabular-nums;cursor:help;
  padding-left:6px;margin-left:2px;border-left:1px solid var(--line)}
.call-move.move-confirm{opacity:1}.call-move.move-against{opacity:.85}
.call-chip--empty{opacity:.55;border-style:dashed;cursor:default}
/* call index badge — maps a hero chip to its numbered prose call + thesis card */
.idx-badge{display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;
  min-width:18px;height:18px;padding:0 4px;border-radius:9px;background:var(--panel);
  border:1px solid var(--line);color:var(--mut);font-size:var(--fs-micro);
  font-weight:700;font-variant-numeric:tabular-nums;line-height:1}
.thesis .idx-badge{margin-right:6px}
/* hover feedback on interactive cards/tiles/rows */
.panel{transition:border-color .15s,background .15s}
.panel:hover{border-color:var(--accent);background:var(--panel2)}
.sec-tile{transition:border-color .15s,background .15s}
.sec-tile:hover{border-color:var(--accent);background:var(--panel2)}
.sec-row{transition:background .12s}
.sec-row:hover{background:rgba(77,163,255,.07);border-radius:4px}
/* sector view (HED-48) */
.sec-tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:var(--s4)}
/* out-of-universe tile: dashed border + reduced opacity signal "not in portfolio", no hover affordance (comes after base to win cascade) */
.sec-tile--oot{border-style:dashed;opacity:.6}
.sec-tile--oot:hover{border-color:var(--line)!important;background:var(--panel)!important;cursor:default}
.sec-head{display:flex;align-items:baseline;gap:var(--s2);margin-bottom:var(--s3)}
.sec-head .id{font-size:var(--fs-micro);font-weight:700;color:var(--accent);letter-spacing:.06em}
.sec-head .nm{font-weight:600}
.sec-head .ct{margin-left:auto;color:var(--mut);font-size:var(--fs-micro)}
.sec-row{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s3);
  padding:var(--s2) 0;border-bottom:1px solid var(--line);font-size:var(--fs-h2)}
.sec-row:last-child{border-bottom:0}
.sec-row .tk{font-weight:600}
.sec-tk{display:inline-flex;align-items:center;gap:5px}
.sec-call-badge{font-size:10px;padding:0 4px;line-height:1.6;cursor:help}
.sec-row .px{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;min-width:64px;margin-left:auto}
.sec-row .ch{font-variant-numeric:tabular-nums;font-size:var(--fs-cap);min-width:62px;text-align:right}
.sec-row .w52{font-size:10px;color:var(--mut);min-width:36px;text-align:right;white-space:nowrap}
.sec-row .rsi{font-size:10px;min-width:32px;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.rsi-ob{color:#f78166}.rsi-os{color:#3fb950}.rsi-n{color:var(--mut)}
.spark{display:block;overflow:visible}
.spark-line{fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.spark-up{stroke:#3fb950}.spark-dn{stroke:#f78166}.spark-flat{stroke:var(--mut)}
.sec-ph{color:var(--mut);font-size:var(--fs-h2);padding:var(--s2) 0}
@media (max-width:760px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .sectors{grid-template-columns:1fr}
  #sectorview{min-height:320px}
  .two-col{grid-template-columns:1fr}
  .flow{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .tr-tbl{display:block}
  .tr-tbl thead{display:none}
  .tr-tbl tbody,.tr-tbl tr{display:block}
  .tr-tbl tr{padding:var(--s3) 0;border-bottom:1px solid var(--line)}
  .tr-tbl td,.tr-tbl tbody th{display:flex;justify-content:space-between;gap:var(--s3);padding:2px 0;border:0;text-align:right}
  .tr-tbl .num{text-align:right}
  .tr-tbl .dlabel{display:inline-block;min-width:90px;vertical-align:top}
  /* sticky calls-strip: trade signals stay in view while scrolling briefing text */
  .calls-strip{position:sticky;top:0;z-index:20;background:var(--bg);
    padding:var(--s2) 0;margin-bottom:var(--s3);
    border-bottom:1px solid var(--line)}
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
/* Section nav: compact in-page jump strip (Recognition>Recall, WCAG 2.4.1, landmark complement) */
.sec-nav{display:flex;gap:var(--s2);flex-wrap:wrap;margin:var(--s3) 0 var(--s4)}
.sec-nav a{display:inline-block;padding:3px var(--s3);border:1px solid var(--line);
  border-radius:12px;font-size:var(--fs-cap);text-transform:uppercase;letter-spacing:.04em;
  color:var(--mut);text-decoration:none;transition:color .15s,border-color .15s,background .15s}
.sec-nav a:hover{color:var(--txt);border-color:var(--accent)}
.sec-nav a.sn-active{color:var(--accent);border-color:var(--accent);background:rgba(88,166,255,.08)}
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
/* Back-to-top: long-page return affordance (Fitts's Law big circular target, Goal-Gradient) */
.to-top{position:fixed;right:var(--s4);bottom:var(--s4);z-index:30;
  width:44px;height:44px;display:flex;align-items:center;justify-content:center;
  border:1px solid var(--line);border-radius:50%;background:var(--panel2);
  color:var(--txt);font-size:18px;line-height:1;cursor:pointer;
  opacity:0;visibility:hidden;transform:translateY(8px);
  transition:opacity .2s,transform .2s,visibility .2s,border-color .15s,background .15s;
  box-shadow:0 2px 8px rgba(0,0,0,.35)}
.to-top.show{opacity:1;visibility:visible;transform:none}
.to-top:hover{border-color:var(--accent);background:var(--panel)}
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
    <div><h1><span aria-hidden="true">🤖</span> AI/Tech Fund — Intelligence Dashboard</h1>
    <div class="sub">Live-Feed → Agenten-Gremium → CEO-Briefing · MVP</div></div>
    <div class="sub" id="builtwrap">aktualisiert: <span id="built"></span></div>
  </header>

  <details class="wf-details">
    <summary class="wf-summary">Workflow — Pipeline-Status</summary>
    <div class="flow-wrap"><div class="flow" id="flow"></div></div>
  </details>

  <nav class="sec-nav" aria-label="Seitenabschnitte">
    <a href="#h-briefing">Briefing</a>
    <a href="#h-trackrecord">Track-Record</a>
    <a href="#h-portfolio">Portfolio</a>
    <a href="#h-sectorview">Sektoren</a>
  </nav>

  <main id="main" tabindex="-1">
  <noscript><div class="panel noscript-panel" role="alert"><div class="noscript-icon" aria-hidden="true">⚠</div><p class="muted">Dieses Dashboard benötigt JavaScript. Bitte aktiviere JavaScript in deinem Browser und lade die Seite neu.</p></div></noscript>
  <section aria-labelledby="h-briefing">
  <h2 id="h-briefing">Letztes Briefing</h2>
  <div id="briefing" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><span class="skel skel-chip"></span><span class="skel skel-chip"></span><div class="skel skel-line" style="width:92%"></div><div class="skel skel-line" style="width:84%"></div><div class="skel skel-line" style="width:88%"></div></div></div>
  </section>

  <section aria-labelledby="h-trackrecord">
  <h2 id="h-trackrecord">Thesen-Track-Record <span id="trstand" class="tag"></span></h2>
  <div id="trackrecord" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:60%"></div><div class="skel skel-line" style="width:75%"></div></div></div>
  </section>

  <section aria-labelledby="h-portfolio">
  <h2 id="h-portfolio">Portfolio-Übersicht</h2>
  <div id="portfolioview" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:55%"></div><div class="skel skel-line" style="width:70%"></div></div></div>
  </section>

  <section aria-labelledby="h-sectorview">
  <h2 id="h-sectorview">Sektor-Ansicht <span id="secstand" class="tag"></span></h2>
  <div class="grid sectors" id="sectorview" aria-busy="true"><div class="skel skel-tile" aria-hidden="true"></div><div class="skel skel-tile" aria-hidden="true"></div><div class="skel skel-tile" aria-hidden="true"></div></div>
  <div id="earnings-cal" style="margin-top:var(--s4)"></div>
  </section>
  </main>

  <details class="wf-details">
    <summary class="wf-summary">Datenfeed <span id="feedstale" style="margin-left:4px"></span> — Ingest-Status</summary>
    <div class="grid cards" id="kpis"></div>
    <div class="grid two-col" style="margin-top:14px">
      <div class="panel"><div class="muted" style="margin-bottom:8px">Quellen</div><div id="sources"></div></div>
      <div class="panel"><div class="muted" style="margin-bottom:8px">Neueste Items</div><div class="feed" id="feed"></div></div>
    </div>
  </details>

  <footer class="foot">
    <p class="foot-disclaimer">Research- und Demonstrations-MVP. Keine Anlageberatung und keine Kauf- oder Verkaufsempfehlung. Dargestellte Thesen, Konviktionen und Richtungs-Calls dienen ausschließlich Forschungs- und Bildungszwecken — keine Gewähr für Richtigkeit, Vollständigkeit oder Eignung. Investitionsentscheidungen erfolgen auf eigenes Risiko.</p>
    <p class="foot-meta">AI/Tech Fund · generiert aus Supabase · keine Secrets im Browser</p>
  </footer>
</div>
<button id="totop" class="to-top" aria-label="Zum Seitenanfang springen" title="Zum Seitenanfang">↑</button>
<script>
const D = __DATA__;
const $ = (id)=>document.getElementById(id);
// Build-Freshness: flag the page itself as stale if the generator hasn't rebuilt in >24h
// (catches a stalled deploy bridge — the CEO sees data may be outdated, Nielsen #1 / preattentive ⚠)
(function(){
  const el=$("built");
  let html=`<time datetime="${D.built_at_iso||''}">${D.built_at}</time>`;
  if(D.built_at_iso){
    const age=Date.now()-new Date(D.built_at_iso).getTime();
    if(age>24*3600*1000){
      const days=Math.max(1,Math.round(age/864e5));
      html=`<span class="build-stale-mark" aria-hidden="true">⚠</span>`+
        `<time datetime="${D.built_at_iso}">${D.built_at}</time>`+
        ` · veraltet (vor ${days} Tag${days===1?"":"en"})`;
      const wrap=$("builtwrap");
      wrap.classList.add("built--stale");
      wrap.setAttribute("role","status");
    }
  }
  el.innerHTML=html;
})();

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
  return `<span class="pill pill--${k}">${icon?`<span aria-hidden="true">${icon}</span> `:""}${esc(s)}</span>`;
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
function dirClass(d){return d==="long"?"cd-long":d==="short"?"cd-short":"cd-pair";}
function sparklineSvg(data,w,h){
  if(!data||data.length<2) return "";
  const mn=Math.min(...data),mx=Math.max(...data);
  const rng=mx-mn||1;
  const xStep=(w-4)/(data.length-1);
  const pts=data.map((v,i)=>`${(2+i*xStep).toFixed(1)},${(h-2-(v-mn)/rng*(h-4)).toFixed(1)}`).join(" ");
  const up=data[data.length-1]>=data[0];
  const flat=Math.abs(data[data.length-1]-data[0])/data[0]<0.001;
  const cls=flat?"spark-flat":up?"spark-up":"spark-dn";
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline class="spark-line ${cls}" points="${pts}"/></svg>`;
}
const b = D.briefing;
if(!b){ $("briefing").innerHTML='<div class="panel muted">Noch kein Briefing. Sobald der n8n-Workflow lief, erscheint es hier.</div>'; }
else{
  const rawTheses=((b.theses||{}).theses)||[];
  // Deduplicate: keep only highest-conviction call per unique ticker group
  const _bestByTicker={};
  rawTheses.forEach(t=>{const k=(t.tickers||[]).join(',');if(!(k in _bestByTicker)||(t.conviction??-1)>(_bestByTicker[k].conviction??-1))_bestByTicker[k]=t;});
  const theses=rawTheses.filter(t=>_bestByTicker[(t.tickers||[]).join(',')]===t);
  const crit=((b.devils_advocate||{}).critiques)||[];
  const cmap={}; crit.forEach(c=>cmap[c.id]=c);
  let html='';
  // Freshness badge: relative time from created_at (Goal-Gradient, Information Scent)
  if(b.created_at){
    const ago=Date.now()-new Date(b.created_at).getTime();
    const mins=Math.round(ago/60000);
    const fresh=mins<2?"gerade eben":mins<60?`vor ${mins} Min.`:mins<1440?`vor ${Math.round(mins/60)} Std.`:`vor ${Math.round(mins/1440)} Tag${Math.round(mins/1440)===1?"":"en"}`;
    const stale=mins>=1560; // >26h: daily briefing missed its slot
    const cls=stale?"brief-ts brief-ts--stale":"brief-ts";
    const mark=stale?`<span class="brief-ts-mark" aria-hidden="true">⚠</span>`:"";
    const lbl=stale?"Briefing veraltet":"Briefing";
    html+=`<div class="${cls}" role="status">${mark}${lbl} · <time datetime="${new Date(b.created_at).toISOString()}" title="${b.created_at}">${fresh}</time></div>`;
  }
  // "Heutige Calls" hero strip: compact chips before prose (Recognition>Recall, Goal-Gradient, F-pattern lede)
  // Conviction color ramp: low→mut, mid→txt, high→accent
  const convCls=c=>c==null?"":c>=0.6?"conv-hi":c>=0.35?"conv-mid":"conv-lo";
  const convLabel=c=>c==null?"":c>=0.6?"hoch":c>=0.35?"mittel":"niedrig";
  const convTip=c=>`Conviction ${c.toFixed(2)} — ${convLabel(c)}. Überzeugungsgrad der These (0–1): <0,35 niedrig · 0,35–0,6 mittel · ≥0,6 hoch.`;
  if(theses.length){
    const dirCls=d=>d==="long"?"cd-long":d==="short"?"cd-short":"cd-pair";
    // Day-move map (ticker→change_pct) from sector_view: ties each hero call to today's price action
    const moveByTicker={};
    ((D.sector_view||{}).sectors||[]).forEach(s=>(s.tickers||[]).forEach(tk=>{
      if(tk&&tk.ticker!=null&&tk.change_pct!=null) moveByTicker[String(tk.ticker).toUpperCase()]=tk.change_pct;
    }));
    const chips=theses.map((t,i)=>{
      const tks=(t.tickers||[]).join(" · ")||"?";
      const dir=t.direction||"pair";
      const conv=t.conviction!=null?`<abbr title="Conviction">Conv</abbr> ${t.conviction.toFixed(2)}`:"";
      // Day-move chip element + confirm/divergence hint vs call direction
      const mv=moveByTicker[String((t.tickers||[])[0]||"").toUpperCase()];
      let moveHtml="", moveTipPart="";
      if(mv!=null){
        const up=mv>=0, sign=up?"+":"−", arrow=up?"▲":"▼";
        const confirm=dir==="long"?up:dir==="short"?!up:null;
        const stance=confirm===true?" — bestätigt "+dir.toUpperCase():confirm===false?" — läuft gegen "+dir.toUpperCase():"";
        const mtip=`Tagesbewegung ${esc((t.tickers||[])[0]||"")}: ${sign}${Math.abs(mv).toFixed(1)}%${stance}`;
        moveHtml=`<span class="call-move ${up?"move-up":"move-dn"} ${confirm===true?"move-confirm":confirm===false?"move-against":""}" title="${esc(mtip)}" aria-label="${esc(mtip)}">${arrow} ${sign}${Math.abs(mv).toFixed(1)}%</span>`;
        moveTipPart=` · heute ${sign}${Math.abs(mv).toFixed(1)}%`;
      }
      // Thesis-preview tooltip: first 120 chars of thesis lets CEO distinguish identical tickers
      const snip=(t.thesis||"").trim().slice(0,120)+(((t.thesis||"").trim().length>120)?"…":"");
      const chipTip=`Call ${i+1}: ${tks} ${(dir||"").toUpperCase()}${t.conviction!=null?" · Conv "+t.conviction.toFixed(2):""}${moveTipPart}${snip?" — "+snip:""}`;
      return `<a class="call-chip" href="#thesis-${i+1}" title="${esc(chipTip)}" aria-label="${esc(chipTip)} — zur These springen"><span class="idx-badge" aria-label="Call ${i+1}">${i+1}</span><span class="ck">${esc(tks)}</span>`+
        `<span class="cd ${dirCls(dir)}">${esc(dir)}</span>`+
        (conv?`<span class="cc ${convCls(t.conviction)}" title="${esc(convTip(t.conviction))}" aria-label="${esc(convTip(t.conviction))}">${conv}</span>`:"")+
        moveHtml+
        `</a>`;
    }).join("");
    html+=`<div class="calls-strip">${chips}</div>`;
  } else {
    html+=`<div class="calls-strip"><div class="call-chip call-chip--empty"><span class="cc">Kein aktiver Call heute</span></div></div>`;
  }
  // Dynamic tab title: lead with the top-conviction call so a pinned/bookmarked tab surfaces the live signal
  // (Recognition>Recall, Information Scent in the tab strip). Client-side only — static og/social meta stays generic.
  if(theses.length){
    const topCall=theses.reduce((a,c)=>((c.conviction==null?-1:c.conviction)>(a.conviction==null?-1:a.conviction)?c:a));
    const ttks=(topCall.tickers||[]).join(" · ")||"?";
    const tdir=(topCall.direction||"").toUpperCase();
    document.title=`${ttks}${tdir?" "+tdir:""} · AI/Tech Fund`;
  } else {
    document.title="Kein aktiver Call · AI/Tech Fund";
  }
  html+=`<div class="brief-region"><div class="brief-main">`;
  if(!b.briefing_md){
    html+=`<div class="panel brief-processing"><span class="brief-proc-icon" aria-hidden="true">⏳</span><span class="muted">Briefing wird verarbeitet…</span></div>`;
  } else if(b.briefing_md){
    // Progressive Disclosure: elevate lede (first <p>) + collapse dense analysis body
    const raw = marked.parse(b.briefing_md);
    const tmp = document.createElement("div"); tmp.innerHTML = raw;
    const nodes = Array.from(tmp.childNodes);
    // a bold-only <p> (e.g. "<b>🗞 CEO-Briefing …</b>") is the title, not the abstract
    const isTitleP = n=>n.nodeName==="P" && n.children.length===1
      && /^(B|STRONG)$/.test(n.children[0].nodeName)
      && n.children[0].textContent.trim()===(n.textContent||"").trim();
    // elevate first real prose <p> as lede; skip leading title line(s)
    const ledeIdx = nodes.findIndex(n=>n.nodeName==="P" && (n.textContent||"").trim().length>0 && !isTitleP(n));
    let briefHtml = "";
    if(ledeIdx>=0){
      // title/headings before the lede pass through (title styled as a heading)
      for(let i=0;i<ledeIdx;i++){
        const n=nodes[i];
        briefHtml += isTitleP(n) ? `<p class="brief-title">${n.innerHTML}</p>` : (n.outerHTML||"");
      }
      briefHtml+=`<p class="brief-lede">${nodes[ledeIdx].innerHTML}</p>`;
      const rest=nodes.slice(ledeIdx+1).map(n=>n.outerHTML||n.textContent||"").join("");
      if(rest.trim()) briefHtml+=`<details open><summary>Vollanalyse</summary>${rest}</details>`;
    } else {
      briefHtml=raw;
    }
    html+=`<div class="brief">${briefHtml}</div>`;
  }
  html+=`</div>`; // .brief-main
  if(theses.length){
    html+='<div class="brief-aside"><h2 class="brief-aside-h2">Thesen & Devil\'s Advocate</h2>';
    html+=theses.map((t,i)=>{
      const c=cmap[t.id]||{};
      return `<div class="thesis" id="thesis-${i+1}" tabindex="-1"><div class="h"><span class="idx-badge" aria-label="These ${i+1}">${i+1}</span>${(t.tickers||[]).join(", ")}
        <span class="cd ${dirClass(t.direction)}">${t.direction||""}</span>
        <span class="${t.conviction!=null?convCls(t.conviction):'muted'}" title="${t.conviction!=null?convTip(t.conviction):''}">· Conv ${t.conviction!=null?t.conviction.toFixed(2):"—"}</span></div>
        <div lang="en" style="margin-top:4px">${esc(t.thesis||"")}</div>
        ${t.edge&&t.is_differentiated?`<div class="edge-line">🎯 ${esc(t.edge)}</div>`:""}
        ${t.scenarios?(()=>{const s=t.scenarios;const fmtS=(k,c)=>{if(!c)return null;const tgt=c.target?` → ${esc(c.target)}`:"";const p=c.prob!=null?` (P=${Math.round(c.prob*100)}%)`:"";return `${k}${c.trigger?" "+esc(c.trigger):""}${tgt}${p}`;};const parts=[fmtS("Bull",s.bull),fmtS("Base",s.base),fmtS("Bear",s.bear)].filter(Boolean);return parts.length?`<div class="sc-line">📐 ${parts.join(" | ")}</div>`:""})():""}
        ${t.exit_trigger?`<div class="exit-trigger">🚪 Exit: ${esc(t.exit_trigger)}</div>`:""}
        ${c.strongest_counter?`<div class="devil" lang="en"><span class="v">⚖️ Devil's Advocate (${c.verdict||"?"})</span><br>${esc(c.strongest_counter)}
        ${c.blind_spot?`<br><span class="muted">Blind spot: ${esc(c.blind_spot)}</span>`:""}</div>`:""}
      </div>`;}).join("");
    html+=`</div>`; // .brief-aside
  }
  html+=`</div>`; // .brief-region
  $("briefing").innerHTML=html||'<div class="panel muted">Briefing vorhanden, aber leer.</div>';
}
// Thesen-Track-Record (HED-29)
function pct(x){return x==null?"—":Math.round(x*100)+"%";}
function verdictPill(v){
  const map={hit:["ok","✓","Hit"],miss:["err","✗","Miss"],neutral:["neutral","","Neutral"],too_early:["warn","⏳","Zu früh"]};
  const [k,icon,lbl]=map[v]||["neutral","",esc(v||"—")];
  return `<span class="pill pill--${k}">${icon?`<span aria-hidden="true">${icon}</span> `:""}${lbl}</span>`;
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
  const pendTip="Wird nach der ersten gewerteten These berechnet";
  $("trkpi").innerHTML=[
    ["Hit-Rate", pct(a.hit_rate), a.hit_rate==null],
    ["gewertet", scored+" / "+(a.total??"—"), false],
    ["in Reifung", a.too_early??"—", false],
    ['<abbr title="Kalibrierungs-Bias">Kalib.-Bias</abbr>', biasTxt, a.calibration_bias==null]
  ].map(([k,v,pending])=>`<dl class="panel kpi-dl"><dt class="muted">${k}</dt>`
    +`<dd class="kpi${pending?" kpi--pending":""}"${pending?` title="${pendTip}" aria-label="${k.replace(/<[^>]+>/g,"")}: noch nicht verfügbar — ${pendTip}"`:""}>${v}</dd></dl>`).join("");
  // Body: happy-path table+chart, oder Empty/Too-Early-State
  const scoredTheses=(tr.theses||[]).filter(t=>t.verdict && t.verdict!=="too_early");
  if(scored>0 && scoredTheses.length){
    const head=["Datum","These","Richtung","Conviction","Kurs","Move %","Verdikt"];
    const order={miss:0,hit:1,neutral:2,too_early:3};
    const rows=scoredTheses.slice().sort((x,y)=>
      (order[x.verdict]-order[y.verdict])||((y.conviction||0)-(x.conviction||0)));
    let tbl=`<table class="tr-tbl"><caption class="tr-cap">Thesen Track-Record</caption><thead><tr>${
      head.map(h=>`<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>`;
    tbl+=rows.map(t=>{
      const dev=t.devil?`<span class="devsig" title="⚖ Devil (${esc(t.devil.verdict||"?")}): ${esc(t.devil.note||"")}">⚖</span>`:"";
      const kurs=t.baseline_price!=null?`${t.baseline_price}${t.current_price!=null?" → "+t.current_price:""}`:"—";
      return `<tr>
        <td class="num"><span class="dlabel">Datum </span>${esc(t.date||"—")}</td>
        <th scope="row" class="tr-lbl"><span class="dlabel">These </span><span class="t">${esc(t.label||"")}</span> <span class="tk">${(t.tickers||[]).map(tk=>`<a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">${esc(tk)}</a>`).join(", ")}</span></th>
        <td><span class="dlabel">Richtung </span><span class="cd ${dirClass(t.direction)}">${esc(t.direction||"")}</span></td>
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
    // Pending-theses list: show which theses are waiting (label/ticker/direction/conviction/score-date)
    // Build live price map from sector_view for mark-to-market on active calls
    const _priceMap={};
    ((D.sector_view||{}).sectors||[]).forEach(s=>{
      (s.tickers||[]).forEach(t=>{if(t.ticker&&t.price!=null)_priceMap[t.ticker.toUpperCase()]=t.price;});
    });
    function _mtm(t){
      // For single-ticker theses: compute unrealised move% vs baseline
      const tks=(t.tickers||[]);
      if(!tks.length||t.baseline_price==null) return "";
      const cur=_priceMap[tks[0].toUpperCase()];
      if(cur==null) return "";
      const dir=(t.direction||"").toLowerCase();
      const rawPct=((cur-t.baseline_price)/t.baseline_price)*100;
      const pnl=dir==="short"?-rawPct:rawPct;
      const up=pnl>=0;
      const cls=up?"move-up":"move-dn";
      return `<span class="${cls}" style="font-size:var(--fs-cap);font-variant-numeric:tabular-nums" title="Baseline $${t.baseline_price} → jetzt $${cur.toFixed(2)}">${up?"+":"−"}${Math.abs(pnl).toFixed(1)}%</span>`;
    }
    const pendingTheses=(tr.theses||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date));
    const pendingTbl=pendingTheses.length ? (()=>{
      const rows=pendingTheses.slice().sort((x,y)=>
        (x.earliest_score_date||"").localeCompare(y.earliest_score_date||""));
      const _cc=c=>c==null?"":c>=0.6?"conv-hi":c>=0.35?"conv-mid":"conv-lo";
      const devNote=t=>t.devil&&t.devil.note?` title="⚖ ${esc(t.devil.verdict||"?")} — ${esc(t.devil.note)}" style="cursor:help"`:""
      const exitNote=t=>t.exit_trigger?`<div class="exit-trigger" title="Exit wenn: ${esc(t.exit_trigger)}">🚪 <span>${esc(t.exit_trigger)}</span></div>`:"";
      const scenNote=t=>{
        const sc=t.scenarios; if(!sc) return "";
        const fmt=(k,s)=>{if(!s)return null;const tgt=s.target?` ${s.target}`:"";const p=`(P=${Math.round((s.prob||0)*100)}%)`;return `${k}${s.trigger?" "+esc(s.trigger):""}${tgt} ${p}`;};
        const parts=[fmt("Bull",sc.bull),fmt("Base",sc.base),fmt("Bear",sc.bear)].filter(Boolean);
        return parts.length?`<div class="sc-line muted">${parts.join(" · ")}</div>`:"";
      };
      return `<table class="tr-pending"><caption class="tr-cap">Offene Thesen — zu früh für Wertung</caption>
        <thead><tr><th scope="col">Diese</th><th scope="col">Richtung</th><th scope="col">Conv.</th><th scope="col">Unrealis. P&amp;L</th><th scope="col">Wertung ab</th></tr></thead>
        <tbody>${rows.map(t=>`<tr${devNote(t)}>
          <th scope="row"><span class="t" style="font-weight:600">${esc(t.label||"?")}</span>
            ${(t.tickers||[]).length?" <span class='muted'>("+
              (t.tickers||[]).map(tk=>`<a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">${esc(tk)}</a>`).join(", ")+
            ")</span>":""}
            ${exitNote(t)}${scenNote(t)}</th>
          <td><span class="cd ${dirClass(t.direction)}">${esc(t.direction||"—")}</span></td>
          <td class="num"><span class="${_cc(t.conviction)}" style="font-variant-numeric:tabular-nums">${t.conviction!=null?t.conviction.toFixed(2):"—"}</span></td>
          <td class="num">${_mtm(t)||'<span class="muted">—</span>'}</td>
          <td class="sd">${esc(t.earliest_score_date||"—")}</td>
        </tr>`).join("")}</tbody></table>`;
    })() : "";
    $("trbody").innerHTML=`<div class="panel"><div class="empty">
      <div class="g" aria-hidden="true">⏳</div>
      <div class="hl">Noch keine gewerteten Thesen</div>
      <div class="ex">${a.too_early||0} offene These${(a.too_early===1)?"":"n"} — der Zeithorizont (Wochen/Quartale) ist noch nicht abgelaufen. Gewertet wird gegen reale Kurse, keine Schätzungen.</div>
      ${cd}
    </div>${pendingTbl}</div>`;
  }
})();

// Portfolio-Übersicht: aktive Calls aus Track-Record + Sektorkonzentration
(function renderPortfolio(){
  const root=$("portfolioview");
  if(!root) return;
  const tr=D.track_record;
  const active=(tr&&tr.theses||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date));
  if(!active.length){
    root.innerHTML='<div class="panel muted">Keine aktiven Calls.</div>';
    root.setAttribute("aria-busy","false"); return;
  }
  // Build sector map from sector_view taxonomy
  const SECTOR_MAP={};
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(tk=>{ SECTOR_MAP[tk.toUpperCase()]=s.id+" "+s.name; });
  });
  const longCalls=active.filter(t=>(t.direction||"").toLowerCase()==="long");
  const shortCalls=active.filter(t=>(t.direction||"").toLowerCase()==="short");
  const totalConv=active.reduce((s,t)=>s+(t.conviction||0),0);
  const longConv=longCalls.reduce((s,t)=>s+(t.conviction||0),0);
  const shortConv=shortCalls.reduce((s,t)=>s+(t.conviction||0),0);
  const netPct=totalConv>0?Math.round(((longConv-shortConv)/totalConv)*100):0;
  // Sector concentration: conviction-weighted
  const secConv={};
  active.forEach(t=>{
    const tks=(t.tickers||[]);
    if(!tks.length) return;
    const sec=SECTOR_MAP[tks[0].toUpperCase()]||"Other";
    secConv[sec]=(secConv[sec]||0)+(t.conviction||0);
  });
  const secEntries=Object.entries(secConv).sort((a,b)=>b[1]-a[1]);
  const maxSec=secEntries[0]?secEntries[0][1]:1;
  // Devil cautions / rejects
  const rejects=active.filter(t=>t.devil&&t.devil.verdict==="reject").length;
  const cautions=active.filter(t=>t.devil&&t.devil.verdict==="caution").length;
  // KPI strip
  const kpis=[
    ["Aktive Calls", active.length, false],
    ["Long / Short", `${longCalls.length} / ${shortCalls.length}`, false],
    ["Net-Exposure", `${netPct>=0?"+":""}${netPct}%`, false],
    ["⚖ Devil (Reject)", rejects, rejects>0],
  ];
  const kpiHtml=kpis.map(([k,v,warn])=>
    `<dl class="panel kpi-dl"><dt class="muted">${k}</dt><dd class="kpi${warn?" kpi--pending":""}">${v}</dd></dl>`
  ).join("");
  // Net long/short bar
  const longPct=totalConv>0?Math.round(longConv/totalConv*100):0;
  const shortPct=100-longPct;
  const barHtml=`<div class="panel pf-bar-wrap">
    <div class="pf-bar-label"><span>Long ${longPct}%</span><span>Short ${shortPct}%</span></div>
    <div class="pf-bar-track"><div class="pf-bar-fill pf-bar-long" style="width:${longPct}%"></div></div>
    <div class="muted" style="font-size:var(--fs-cap)">${cautions} Caution · ${rejects} Reject vom Devil-Advocate</div>
  </div>`;
  // Sector concentration bars
  const secBarHtml=secEntries.length?`<div class="panel">
    <div class="muted" style="margin-bottom:var(--s3);font-size:var(--fs-cap)">Sektorkonzentration (conviction-gewichtet)</div>
    ${secEntries.map(([sec,cv])=>`<div class="pf-sec-row">
      <span class="pf-sec-name">${esc(sec)}</span>
      <div class="pf-bar-track" style="flex:0 0 100px;margin:0"><div class="pf-bar-fill pf-bar-long" style="width:${Math.round(cv/maxSec*100)}%"></div></div>
      <span class="pf-sec-pct">${Math.round(cv/totalConv*100)}%</span>
    </div>`).join("")}
  </div>`:"";
  root.innerHTML=`<div class="pf-grid">${kpiHtml}</div><div class="grid two-col" style="gap:var(--s3)">${barHtml}${secBarHtml}</div>`;
  root.setAttribute("aria-busy","false");
})();

// Sektor-Ansicht (HED-48)
(function renderSectors(){
  const sv=D.sector_view, root=$("sectorview");
  if(!sv || !(sv.sectors||[]).length){
    root.innerHTML='<div class="panel muted">Sektor-Ansicht noch nicht verfügbar.</div>'; return; }
  // Build active-call direction map from latest briefing theses
  const activeCalls={};
  const latestB=D.briefing||{};
  ((latestB.theses||{}).theses||[]).forEach(t=>{
    const dir=(t.direction||"").toLowerCase();
    (t.tickers||[]).forEach(tk=>{
      const key=tk.toUpperCase();
      const ex=activeCalls[key];
      if(!ex||(t.conviction!=null&&(ex.conv==null||t.conviction>ex.conv)))
        activeCalls[key]={dir,conv:t.conviction};
    });
  });
  const dirCls=d=>d==="long"?"cd-long":d==="short"?"cd-short":"cd-pair";
  $("secstand").innerHTML = sv.as_of
    ? `Kurse <time datetime="${(sv.as_of_iso||sv.as_of.replace(' UTC','').replace(' ','T')+'Z')}">${sv.as_of}</time>`
    : "Taxonomie (ohne Kurse)";
  function chCell(c){
    if(c==null) return '<span class="ch muted">—</span>';
    if(Math.abs(c)<0.05) return '<span class="ch muted" aria-label="unverändert">0.0%</span>';
    const up=c>=0;
    const arrow=up?"▲":"▼";
    return `<span class="ch ${up?"move-up":"move-dn"}" aria-label="${up?"steigt":"fällt"} ${Math.abs(c).toFixed(1)} Prozent">${arrow} ${up?"+":"−"}${Math.abs(c).toFixed(1)}%</span>`;
  }
  // Stable order: populated tiles first, then placeholders; both by numeric s.id ascending (S1→S6)
  const sorted=(sv.sectors||[]).slice().sort((a,b)=>{
    const hasTk=s=>(s.tickers||[]).length>0;
    if(hasTk(a)!==hasTk(b)) return hasTk(b)-hasTk(a);
    return String(a.id).localeCompare(String(b.id), undefined, {numeric:true});
  });
  root.innerHTML = sorted.map(s=>{
    const tks=s.tickers||[];
    let body;
    if(tks.length){
      // sort tickers by absolute move descending — biggest intraday movers lead the list (F-pattern, Information Scent); nulls trail
      body = tks.slice().sort((a,b)=>{const ma=a.change_pct!=null?Math.abs(a.change_pct):-1,mb=b.change_pct!=null?Math.abs(b.change_pct):-1;return mb-ma;}).map(t=>{
        const px = t.price!=null ? t.price.toFixed(2) : '<span class="muted">—</span>';
        const call=activeCalls[(t.ticker||"").toUpperCase()];
        const badge=call?`<span class="cd ${dirCls(call.dir)} sec-call-badge" title="Aktiver Call: ${esc(call.dir.toUpperCase())}${call.conv!=null?" · Conv "+call.conv.toFixed(2):""}" aria-label="Aktiver Call ${esc(call.dir.toUpperCase())}">${esc(call.dir.toUpperCase())}</span>`:"";
        const tkHtml=badge
          ? `<span class="sec-tk"><a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(t.ticker)}" target="_blank" rel="noopener">${esc(t.ticker)}</a>${badge}</span>`
          : `<a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(t.ticker)}" target="_blank" rel="noopener">${esc(t.ticker)}</a>`;
        const w52 = t.pct_of_52w_high!=null
          ? `<span class="w52" title="52-Wochen-Hoch: $${t.w52_high!=null?t.w52_high.toFixed(0):'?'} · Tief: $${t.w52_low!=null?t.w52_low.toFixed(0):'?'}">${t.pct_of_52w_high.toFixed(0)}%↑</span>`
          : "";
        const rsiCls=t.rsi14==null?"":t.rsi14>70?"rsi-ob":t.rsi14<30?"rsi-os":"rsi-n";
        const rsiTip=t.ma30!=null?`MA30: $${t.ma30} (${t.pct_vs_ma30>=0?"+":""}${t.pct_vs_ma30?.toFixed(1)}%) · `:"";
        const rsi = t.rsi14!=null
          ? `<span class="rsi ${rsiCls}" title="${rsiTip}RSI14: ${t.rsi14}${t.rsi14>70?" — overbought":t.rsi14<30?" — oversold":""}">${t.rsi14}</span>`
          : "";
        const spark=t.spark?sparklineSvg(t.spark,56,22):"";
        return `<div class="sec-row">${tkHtml}<span class="px">${px}</span>${chCell(t.change_pct)}${w52}${rsi}${spark}</div>`;
      }).join("");
    } else {
      body = `<div class="sec-ph">${esc(s.note||"Keine in-universe Ticker.")}</div>`;
    }
    const ootCls=tks.length===0?" sec-tile--oot":"";
    const validMoves=tks.map(t=>t.change_pct).filter(v=>v!=null);
    const avgMove=validMoves.length?validMoves.reduce((a,b)=>a+b,0)/validMoves.length:null;
    const avgHtml=avgMove!=null?chCell(avgMove):"";
    return `<div class="sec-tile${ootCls}"><div class="sec-head">
        <span class="id">${esc(s.id)}</span><span class="nm">${esc(s.name)}</span>
        <span class="ct">${tks.length||""}</span>${avgHtml}</div>${body}</div>`;
  }).join("");
})();

(function renderEarningsCal(){
  const cal=((D.sector_view||{}).earnings_calendar)||[];
  const root=$("earnings-cal");
  if(!root||!cal.length) return;
  const today=new Date().toISOString().slice(0,10);
  const pills=cal.map(e=>{
    const d=e.days_out;
    const cls=d===0?"pill pill--err":d<=2?"pill pill--warn":"pill pill--neutral";
    const when=d===0?"heute":d===1?"morgen":`in ${d}d`;
    return `<span class="${cls}" style="font-size:var(--fs-cap);margin:2px"><b>${esc(e.ticker)}</b> ${when} (${esc(e.date)})</span>`;
  }).join(" ");
  root.innerHTML=`<div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px"><span style="font-size:var(--fs-cap);color:var(--mut);margin-right:4px">📅 Earnings:</span>${pills}</div>`;
})();

function esc(s){return (s||"").replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));}
// loading complete: clear skeleton busy-state so assistive tech announces rendered content
["briefing","trackrecord","portfolioview","sectorview"].forEach(id=>{const el=$(id);if(el)el.setAttribute("aria-busy","false");});

// Section nav: highlight the anchor pill whose section is currently most in view
(function(){
  const links=document.querySelectorAll(".sec-nav a");
  if(!links.length) return;
  const ids=Array.from(links).map(a=>a.getAttribute("href").slice(1));
  // Scroll-position recalc: the pill whose h2 most recently crossed 40% from viewport top.
  // Replaces the old IO toggle which left multiple pills active on fast programmatic scrolls.
  const update=()=>{
    const cut=window.innerHeight*0.4;
    let best=null,bestTop=-Infinity;
    ids.forEach(id=>{
      const el=document.getElementById(id); if(!el) return;
      const top=el.getBoundingClientRect().top;
      if(top<=cut&&top>bestTop){bestTop=top;best=id;}
    });
    links.forEach(a=>a.classList.remove("sn-active"));
    (best?document.querySelector(`.sec-nav a[href="#${best}"]`):links[0])
      ?.classList.add("sn-active");
  };
  let ticking=false;
  window.addEventListener("scroll",()=>{
    if(!ticking){requestAnimationFrame(()=>{update();ticking=false;});ticking=true;}
  },{passive:true});
  update();
})();

// Back-to-top: reveal after scrolling past the briefing fold; smooth unless reduced-motion
(function(){
  const btn=$("totop"); if(!btn) return;
  const onScroll=()=>{ btn.classList.toggle("show", window.scrollY>600); };
  window.addEventListener("scroll",onScroll,{passive:true}); onScroll();
  btn.addEventListener("click",()=>{
    const reduce=window.matchMedia("(prefers-reduced-motion:reduce)").matches;
    window.scrollTo({top:0,behavior:reduce?"auto":"smooth"});
    const m=$("main"); if(m) m.focus({preventScroll:true});
  });
})();

// Visibility-aware auto-reload: when CEO returns to a pinned tab, reload if build data is >30 min stale.
// The pipeline overwrites the static HTML on every ingest cycle — location.reload() fetches the freshest build.
// Also fires a passive interval as fallback for long-idle tabs (uses Page Visibility API to skip background reloads).
(function(){
  const THRESHOLD_MS=30*60*1000; // 30 min — matches ingest cadence
  const builtAt=D.built_at_iso?new Date(D.built_at_iso).getTime():null;
  if(!builtAt) return; // no build timestamp → skip
  function maybeReload(){
    if(document.hidden) return; // don't reload while tab is in background
    if(Date.now()-builtAt>THRESHOLD_MS) location.reload();
  }
  document.addEventListener("visibilitychange", maybeReload);
  // Passive fallback: check every 5 min so a foreground-but-idle tab also refreshes eventually
  setInterval(maybeReload, 5*60*1000);
})();
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


















