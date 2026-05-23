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


def _consensus_estimates(ticker: str) -> dict | None:
    """Fetch analyst consensus data via yfinance for a ticker.
    Returns dict with targetMeanPrice, numberOfAnalystOpinions, recommendationKey,
    forwardEps — used by analyst/thesis prompts as consensus_anchor context."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        result: dict = {}
        if info.get("targetMeanPrice"):
            result["pt_mean"] = round(float(info["targetMeanPrice"]), 2)
        if info.get("targetLowPrice"):
            result["pt_low"] = round(float(info["targetLowPrice"]), 2)
        if info.get("targetHighPrice"):
            result["pt_high"] = round(float(info["targetHighPrice"]), 2)
        if info.get("numberOfAnalystOpinions"):
            result["analyst_count"] = int(info["numberOfAnalystOpinions"])
        if info.get("recommendationKey"):
            result["rec"] = str(info["recommendationKey"])
        if info.get("forwardEps"):
            result["fwd_eps"] = round(float(info["forwardEps"]), 3)
        if info.get("revenueGrowth"):
            result["rev_growth_yoy"] = round(float(info["revenueGrowth"]) * 100, 1)
        return result if result else None
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
        enriched = []
        for t in s["tickers"]:
            q = _yahoo_quote(t)
            if q:
                cons = _consensus_estimates(t)
                if cons:
                    q["consensus"] = cons
                enriched.append(q)
        sectors.append({"id": s["id"], "name": s["name"],
                        "note": s.get("note"), "tickers": enriched})
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
/* analytical depth: surface bull_case / bear_case / catalysts that already live in the thesis payload but never rendered — direction-agnostic labels (Pro/Risiken/Katalysatoren), color-coded left rails */
.ta{margin-top:var(--s3);border-top:1px solid var(--line);padding-top:var(--s2)}
.ta-sect{border-left:2px solid var(--line);padding:var(--s1) 0 var(--s1) var(--s3);margin-top:var(--s2)}
.ta-sect--pro{border-left-color:var(--green)}
.ta-sect--contra{border-left-color:var(--red)}
.ta-sect--cat{border-left-color:var(--accent)}
.ta-h{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;margin-bottom:var(--s1);display:flex;align-items:baseline;gap:6px}
.ta-h .ta-n{color:var(--txt);font-variant-numeric:tabular-nums}
.ta-list{margin:0;padding-left:18px;list-style:disc;font-size:var(--fs-cap);color:var(--txt);line-height:1.5}
.ta-list li{margin-bottom:var(--s1)}
.ta-list li:last-child{margin-bottom:0}
.ta-list li::marker{color:var(--mut)}
/* horizon badge on thesis card: shows time-frame of the call without crowding the header */
.th-horizon{font-size:var(--fs-micro);font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:1px 6px;border-radius:4px;background:var(--panel);border:1px solid var(--line);color:var(--mut);margin-left:auto;flex-shrink:0}
.thesis .h{display:flex;align-items:center;flex-wrap:wrap;gap:6px}
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
/* concentration-risk readout: descriptive bars → actual risk signal (amber+icon when a threshold breaches) */
.pf-risk{margin-top:var(--s3)}
.pf-risk-h{font-size:var(--fs-cap);color:var(--mut);margin-bottom:var(--s2)}
.pf-risk-chips{display:flex;flex-wrap:wrap;gap:var(--s2)}
.pf-risk-chip{display:inline-flex;align-items:center;gap:6px;font-size:var(--fs-cap);
  padding:4px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);
  color:var(--mut);cursor:help;font-variant-numeric:tabular-nums}
.pf-risk-chip b{color:var(--txt);font-weight:600}
.pf-risk-chip--warn{border-color:var(--amber);color:var(--amber)}
.pf-risk-chip--warn b{color:var(--amber)}
.pf-risk-mark{font-weight:700}
/* Buch-Performance (unrealisiert): per-call P&L bars + aggregate KPI coloring */
.pf-pnl-h{font-size:var(--fs-cap);color:var(--mut);margin-bottom:var(--s2);display:flex;justify-content:space-between;align-items:baseline;gap:var(--s3)}
.pf-pnl-h .pf-pnl-meta{font-size:var(--fs-micro)}
.pf-pnl-row{display:grid;grid-template-columns:minmax(72px,auto) 26px 1fr 58px;align-items:center;gap:var(--s2);padding:5px 0;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
.pf-pnl-row:first-of-type{border-top:0}
.pf-pnl-tk{display:flex;align-items:center;gap:6px;font-size:var(--fs-cap);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pf-pnl-tk .pf-pnl-lbl{color:var(--mut);font-size:var(--fs-micro);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pf-pnl-dir{font-size:var(--fs-micro);padding:1px 5px;border-radius:3px;font-weight:600;letter-spacing:.04em}
.pf-pnl-track{position:relative;height:10px;background:transparent;border-left:1px solid var(--line);border-right:1px solid var(--line)}
.pf-pnl-track::before{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--line)}
.pf-pnl-bar{position:absolute;top:1px;bottom:1px;border-radius:2px}
.pf-pnl-bar-pos{background:var(--green);left:50%}
.pf-pnl-bar-neg{background:var(--red);right:50%}
.pf-pnl-val{text-align:right;font-size:var(--fs-cap);font-weight:600}
.pf-pnl-empty{font-size:var(--fs-micro);color:var(--mut);padding:var(--s2) 0}
.kpi--pos{color:var(--green)}
.kpi--neg{color:var(--red)}
/* Buch-Equity-Kurve: marquee performance chart — conviction-weighted, since-inception (HED-137 cycle 81) */
.ec-panel{padding:var(--s3) var(--s3) var(--s2)}
.ec-h{display:flex;justify-content:space-between;align-items:flex-end;gap:var(--s3);margin-bottom:var(--s2);flex-wrap:wrap}
.ec-h-l{display:flex;flex-direction:column;gap:2px;min-width:0}
.ec-title{font-size:var(--fs-cap);color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.ec-h-sub{font-size:var(--fs-micro)}
.ec-kpis{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-shrink:0}
.ec-kpi{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;cursor:help}
.ec-kpi b{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.1}
.ec-svg{width:100%;height:auto;display:block;max-height:160px}
.ec-line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ec-line.ec-pos{stroke:var(--green)}
.ec-line.ec-neg{stroke:var(--red)}
.ec-area{opacity:.15}
.ec-area.ec-pos{fill:var(--green)}
.ec-area.ec-neg{fill:var(--red)}
.ec-dot{stroke:var(--bg);stroke-width:2}
.ec-dot.ec-pos{fill:var(--green)}
.ec-dot.ec-neg{fill:var(--red)}
.ec-tick{fill:var(--mut);opacity:.55;cursor:help}
.ec-zero{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.ec-ylab{font-size:10px;fill:var(--mut);font-variant-numeric:tabular-nums;font-family:inherit}
.ec-xlab{font-size:10px;fill:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-family:inherit}
.ec-foot{font-size:var(--fs-micro);margin-top:6px;line-height:1.4}
/* Benchmark overlay: SPY dashed line on equity curve + alpha KPI (HED-137 cycle 91) */
.ec-bench{fill:none;stroke:var(--mut);stroke-width:1.5;stroke-dasharray:4 3;opacity:.65}
.ec-bench-label{font-size:9px;fill:var(--mut);font-family:inherit;font-weight:600;letter-spacing:.04em;opacity:.8}
.ec-kpi-alpha{border-left:1px solid var(--line);padding-left:12px;margin-left:4px}
/* Underwater (drawdown) curve — risk profile paired with equity curve (HED-137 cycle 90) */
.dd-svg{width:100%;height:auto;display:block;max-height:90px;margin-top:2px}
.dd-line{fill:none;stroke:var(--red);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.dd-area{fill:var(--red);opacity:.20}
.dd-min{fill:var(--red);stroke:var(--bg);stroke-width:2}
.dd-cur{fill:none;stroke:var(--red);stroke-width:2}
.dd-title{font-size:10px;fill:var(--mut);font-variant-numeric:tabular-nums;font-family:inherit;text-transform:uppercase;letter-spacing:.05em}
.dd-title-cur{fill:var(--red);font-weight:700}
@media(max-width:640px){
  .ec-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .ec-kpis{justify-content:space-between;gap:var(--s3)}
  .ec-kpi{text-align:left}
  .ec-kpi b{font-size:15px}
  .ec-svg{max-height:130px}
  .dd-svg{max-height:70px}
}
/* Conviction-vs-P&L Scatter — "are my high-conviction calls working?" (HED-137 cycle 84) */
.pf-scatter{margin-top:var(--s3);padding:var(--s3)}
.pf-scatter-h{font-size:var(--fs-cap);color:var(--mut);margin-bottom:var(--s2);display:flex;
  justify-content:space-between;align-items:baseline;gap:var(--s3)}
.pf-scatter-h .pf-scatter-sub{font-size:var(--fs-micro)}
.pf-scatter-svg{width:100%;height:auto;display:block;max-height:220px}
.sc-zero{stroke:var(--line);stroke-width:1;stroke-dasharray:3 4}
.sc-axis{stroke:var(--line);stroke-width:1}
.sc-dot{stroke:var(--bg);stroke-width:2;cursor:help;transition:r .12s}
.sc-dot-pos{fill:var(--green)}
.sc-dot-neg{fill:var(--red)}
.sc-dot-flat{fill:var(--mut)}
.sc-label{font-size:11px;fill:var(--txt);font-weight:700;font-family:inherit;pointer-events:none}
.sc-qlabel{font-size:9px;fill:var(--mut);text-transform:uppercase;letter-spacing:.04em;font-family:inherit}
.sc-axlabel{font-size:9px;fill:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-family:inherit}
.sc-foot{font-size:var(--fs-micro);margin-top:6px;line-height:1.4}
/* Korrelationsmatrix — Diversifikations-Diagnose (HED-137 Zyklus 86): pairwise 30d return correlation */
.pf-corr{margin-top:var(--s3);padding:var(--s3)}
.pf-corr-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:var(--s2);margin-bottom:var(--s3);font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt)}
.pf-corr-sub{font-size:var(--fs-micro);font-weight:400}
.pf-corr-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:8px}
.pf-corr-tbl{border-collapse:separate;border-spacing:2px;font-variant-numeric:tabular-nums;font-size:var(--fs-cap);width:auto;min-width:100%}
.pf-corr-tbl th,.pf-corr-tbl td{padding:6px 8px;text-align:center;border-radius:5px;font-weight:600;white-space:nowrap}
.pf-corr-tbl thead th{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:600;background:transparent}
.pf-corr-corner{background:transparent}
.pf-corr-col{min-width:60px}
.pf-corr-row{text-align:left;background:var(--panel2);position:sticky;left:0;z-index:1;min-width:150px;display:flex;align-items:center;justify-content:space-between;gap:var(--s2)}
.pf-corr-lbl{display:inline-block;max-width:120px;overflow:hidden;text-overflow:ellipsis;vertical-align:middle;font-weight:700}
.pf-corr-cell{min-width:54px}
.pf-corr-diag{background:var(--panel2);color:var(--mut)}
.pf-corr-na{background:var(--panel2);color:var(--mut)}
.pf-corr-strong{font-weight:800;outline:1.5px solid var(--line);outline-offset:-1px}
.pf-corr-diag-row{display:flex;flex-wrap:wrap;gap:var(--s5);margin-top:var(--s3);font-size:var(--fs-cap);font-variant-numeric:tabular-nums}
.pf-corr-diag-row b{font-weight:700}
.pf-corr-verd{font-size:var(--fs-cap);margin-top:6px;line-height:1.5;color:var(--txt)}
.pf-corr-foot{font-size:var(--fs-micro);margin-top:var(--s2);line-height:1.4}
.pf-corr-legend{display:inline-flex;gap:var(--s3);align-items:center;flex-wrap:wrap}
.pf-corr-leg-chip{display:inline-flex;align-items:center;gap:4px}
.pf-corr-leg-sw{display:inline-block;width:14px;height:10px;border-radius:3px;border:1px solid var(--line)}
@media(max-width:560px){
  .pf-corr-tbl th,.pf-corr-tbl td{padding:5px 6px;font-size:var(--fs-micro)}
  .pf-corr-row{min-width:104px}
  .pf-corr-col{min-width:46px}
  .pf-corr-cell{min-width:46px}
  .pf-corr-lbl{max-width:80px}
  .pf-corr-diag-row{gap:var(--s3)}
}
/* Buch-Allokation — Position-Sizing-Stack (HED-137 Zyklus 87): Long/Short side stacks, conviction-weighted widths, P&L-colored segments */
.pf-alloc{margin-top:var(--s3);padding:var(--s3)}
.pf-alloc-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.pf-alloc-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.pf-alloc-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.pf-alloc-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.pf-alloc-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;cursor:help;min-width:54px}
.pf-alloc-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.pf-alloc-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.pf-alloc-sides{display:flex;flex-direction:column;gap:var(--s3)}
.pf-alloc-side-h{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s2);font-size:var(--fs-micro);color:var(--mut);margin-bottom:5px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.pf-alloc-side-h b{color:var(--txt);font-weight:700;font-variant-numeric:tabular-nums}
.pf-alloc-bar{display:flex;height:36px;background:var(--panel2);border-radius:6px;overflow:hidden;border:1px solid var(--line)}
.pf-alloc-seg{display:flex;align-items:center;justify-content:center;font-size:var(--fs-micro);font-weight:700;color:#fff;border-right:1px solid rgba(0,0,0,.32);overflow:hidden;white-space:nowrap;cursor:help;transition:filter .12s;font-variant-numeric:tabular-nums;padding:0 6px;min-width:0}
.pf-alloc-seg:last-child{border-right:0}
.pf-alloc-seg:hover{filter:brightness(1.18)}
.pf-alloc-seg-pos-strong{background:#2da347}
.pf-alloc-seg-pos{background:rgba(63,185,80,.55);color:var(--txt)}
.pf-alloc-seg-flat{background:rgba(138,160,189,.25);color:var(--txt)}
.pf-alloc-seg-neg{background:rgba(248,81,73,.55);color:var(--txt)}
.pf-alloc-seg-neg-strong{background:#d23a32}
.pf-alloc-seg-unpriced{background:repeating-linear-gradient(45deg,var(--panel2),var(--panel2) 4px,rgba(138,160,189,.18) 4px,rgba(138,160,189,.18) 8px);color:var(--mut)}
.pf-alloc-empty{display:flex;align-items:center;justify-content:center;height:36px;background:transparent;border-radius:6px;border:1px dashed var(--line);font-size:var(--fs-micro);color:var(--mut);font-style:italic}
.pf-alloc-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.pf-alloc-legend{display:inline-flex;gap:var(--s3);flex-wrap:wrap;align-items:center}
.pf-alloc-legend-chip{display:inline-flex;align-items:center;gap:4px}
.pf-alloc-legend-sw{display:inline-block;width:12px;height:10px;border-radius:2px;border:1px solid rgba(255,255,255,.06)}
@media(max-width:640px){
  .pf-alloc-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .pf-alloc-metrics{justify-content:space-between;gap:var(--s3)}
  .pf-alloc-metric{text-align:left;min-width:0;flex:1}
  .pf-alloc-metric .val{font-size:16px}
  .pf-alloc-bar{height:30px}
  .pf-alloc-seg{font-size:10px;padding:0 4px}
}
/* Katalysator-Runway: event-driven timeline (earnings + thesis-horizon) for the next 30 days (HED-137 Zyklus 85) */
.cat-panel{padding:var(--s3)}
.cat-svg{width:100%;height:auto;display:block;max-height:160px;margin-top:var(--s2)}
.cat-grid{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3;opacity:.55}
.cat-axis{stroke:var(--line);stroke-width:1}
.cat-xlab{font-size:10px;fill:var(--mut);font-family:inherit;letter-spacing:.04em}
.cat-mark{cursor:help}
.cat-mark-th{stroke:var(--bg);stroke-width:2}
.cat-mark-er-watch{fill:none;stroke-width:1.6}
.cat-label{font-size:9px;font-family:inherit;fill:var(--mut);font-variant-numeric:tabular-nums;pointer-events:none}
.cat-label-held{fill:var(--txt);font-weight:600}
.cat-legend{display:flex;flex-wrap:wrap;gap:var(--s3);align-items:center;font-size:var(--fs-micro);color:var(--mut);margin-top:6px}
.cat-leg-item{display:inline-flex;align-items:center;gap:5px}
.cat-leg-dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex-shrink:0}
.cat-leg-dot--held{background:var(--green)}
.cat-leg-dot--watch{background:transparent;border:1.5px solid var(--mut)}
.cat-leg-dia{display:inline-block;width:9px;height:9px;background:var(--amber);transform:rotate(45deg);flex-shrink:0}
.cat-list{margin-top:var(--s3);font-size:var(--fs-cap)}
.cat-list-row{display:grid;grid-template-columns:64px 44px 22px 1fr auto;align-items:center;gap:var(--s3);padding:var(--s2) 0;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
.cat-list-row:first-of-type{border-top:0}
.cat-list-row .d-when{color:var(--txt);font-weight:600}
.cat-list-row .d-out{color:var(--mut);font-size:var(--fs-micro)}
.cat-list-row .d-sym{font-size:13px;text-align:center;line-height:1}
.cat-list-row .d-tk{font-weight:700}
.cat-list-row .d-kind{color:var(--mut);font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.04em;margin-left:6px}
.cat-list-row .d-tag{font-size:var(--fs-micro);padding:2px 7px;border-radius:4px;white-space:nowrap;font-weight:600;letter-spacing:.03em;text-transform:uppercase}
.cat-tag-held-long{background:rgba(63,185,80,.18);color:var(--green)}
.cat-tag-held-short{background:rgba(248,81,73,.18);color:var(--red)}
.cat-tag-held-pair{background:rgba(210,153,34,.18);color:var(--amber)}
.cat-tag-watch{background:var(--panel2);color:var(--mut);font-weight:500}
.cat-empty{color:var(--mut);font-size:var(--fs-micro);padding:var(--s3) 0}
.cat-foot{color:var(--mut);font-size:var(--fs-micro);margin-top:var(--s2);line-height:1.4}
@media(max-width:640px){
  .cat-svg{max-height:200px}
  .cat-list-row{grid-template-columns:50px 38px 20px 1fr auto;gap:var(--s2);font-size:var(--fs-micro)}
  .cat-list-row .d-kind{display:none}
}
/* Thesis price-context bar: Bloomberg-style market data row embedded in each call card */
.th-mkt{display:flex;flex-wrap:wrap;align-items:center;gap:0;margin:var(--s2) 0 var(--s3);
  background:var(--bg);border:1px solid var(--line);border-radius:8px;overflow:hidden;font-variant-numeric:tabular-nums}
.th-mkt-cell{display:flex;flex-direction:column;justify-content:center;padding:5px var(--s3);
  border-right:1px solid var(--line);min-width:0;flex:1 1 auto}
.th-mkt-cell:last-child{border-right:none}
.th-mkt-lbl{font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.th-mkt-val{font-size:var(--fs-cap);font-weight:700;white-space:nowrap}
.th-mkt-val.up{color:var(--green)}
.th-mkt-val.dn{color:var(--red)}
.th-mkt-val.flat{color:var(--mut)}
/* 52w range bar: compact horizontal thermometer showing price position within 52-week band */
.th-52w-bar{margin-top:3px;height:4px;background:var(--line);border-radius:2px;position:relative;width:100%;min-width:40px}
.th-52w-fill{position:absolute;left:0;top:0;bottom:0;border-radius:2px;background:var(--accent);min-width:3px}
/* call-direction confirm/diverge tint on the since-call cell */
.th-mkt-cell.mkt-confirm{background:rgba(63,185,80,.08);border-bottom:2px solid var(--green)}
.th-mkt-cell.mkt-against{background:rgba(248,81,73,.08);border-bottom:2px solid var(--red)}
/* embedded 30-day price chart in thesis cards (HED-137 cycle 88):
   makes baseline-vs-current visible at a glance, P&L shown as shaded area between
   baseline reference line and price polyline. Direction-aware (shorts: down=green). */
.th-ch{position:relative;margin:var(--s2) 0 var(--s3);background:var(--panel2);
  border:1px solid var(--line);border-radius:8px;overflow:hidden;height:68px}
.th-ch svg{display:block;width:100%;height:100%}
.th-ch-line{fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;
  vector-effect:non-scaling-stroke}
.th-ch-line-up{stroke:var(--green)}
.th-ch-line-dn{stroke:#f78166}
.th-ch-line-flat{stroke:var(--mut)}
.th-ch-base{stroke:var(--amber);stroke-width:1;stroke-dasharray:3 2;opacity:.75;
  vector-effect:non-scaling-stroke}
.th-ch-area-up{fill:var(--green);fill-opacity:.14}
.th-ch-area-dn{fill:#f78166;fill-opacity:.14}
.th-ch-area-flat{fill:var(--mut);fill-opacity:.08}
.th-ch-pt-up{fill:var(--green)}
.th-ch-pt-dn{fill:#f78166}
.th-ch-pt-flat{fill:var(--mut)}
.th-ch-tag{position:absolute;top:4px;left:6px;font-size:9px;font-weight:700;
  letter-spacing:.05em;color:var(--mut);background:rgba(20,26,38,.6);padding:1px 5px;
  border-radius:3px;font-variant-numeric:tabular-nums}
.th-ch-cur{position:absolute;top:4px;right:6px;font-size:10px;font-weight:700;
  color:var(--txt);font-variant-numeric:tabular-nums;background:rgba(20,26,38,.6);
  padding:1px 5px;border-radius:3px}
.th-ch-base-lbl{position:absolute;right:6px;font-size:9px;color:var(--amber);
  font-weight:700;font-variant-numeric:tabular-nums;transform:translateY(-50%);
  background:rgba(11,15,23,.85);padding:0 4px;border-radius:3px;line-height:1.4;
  white-space:nowrap;pointer-events:none}
.th-ch-pnl{position:absolute;bottom:4px;right:6px;font-size:10px;font-weight:800;
  font-variant-numeric:tabular-nums;background:rgba(11,15,23,.85);padding:1px 5px;
  border-radius:3px;letter-spacing:.01em}
.th-ch-pnl-up{color:var(--green)}
.th-ch-pnl-dn{color:#f78166}
.th-ch-pnl-flat{color:var(--mut)}
.th-ch-empty{padding:18px;text-align:center;font-size:var(--fs-micro);color:var(--mut)}
/* Risk/Reward-Barometer (HED-137 Zyklus 89): current price vs Street PT-Low/Mean/High range,
   call-perspective R/R ratio. Forward-looking complement to the 30D mini-chart. */
.th-rr{margin:var(--s2) 0 var(--s3);background:var(--panel2);
  border:1px solid var(--line);border-radius:8px;padding:8px 10px 7px}
.th-rr-h{display:flex;justify-content:space-between;align-items:baseline;
  font-size:9px;font-weight:700;letter-spacing:.05em;color:var(--mut);
  text-transform:uppercase;margin-bottom:6px}
.th-rr-rr{font-size:11px;font-variant-numeric:tabular-nums;letter-spacing:.02em;text-transform:none;font-weight:700}
.th-rr-rr-good{color:var(--green)}
.th-rr-rr-warn{color:var(--amber)}
.th-rr-rr-bad{color:#f78166}
.th-rr-bar{position:relative;height:12px;background:rgba(255,255,255,.04);
  border-radius:3px;border:1px solid rgba(255,255,255,.05)}
.th-rr-up{position:absolute;top:0;height:100%;background:rgba(63,185,80,.28);border-radius:2px}
.th-rr-dn{position:absolute;top:0;height:100%;background:rgba(248,81,73,.28);border-radius:2px}
.th-rr-cur{position:absolute;top:-3px;width:2px;height:18px;background:var(--txt);
  transform:translateX(-1px);box-shadow:0 0 0 2px rgba(11,15,23,.7);pointer-events:none}
.th-rr-mean{position:absolute;top:50%;width:6px;height:6px;background:var(--amber);
  border-radius:50%;transform:translate(-50%,-50%);box-shadow:0 0 0 1px rgba(11,15,23,.6)}
.th-rr-tick{position:absolute;top:0;width:1px;height:100%;background:rgba(255,255,255,.18)}
.th-rr-labels{display:flex;justify-content:space-between;font-size:9.5px;margin-top:5px;
  color:var(--mut);font-variant-numeric:tabular-nums;letter-spacing:.01em;gap:6px}
.th-rr-lbl{font-weight:600;white-space:nowrap}
.th-rr-lbl-r{text-align:right}
.th-rr-cur-lbl{color:var(--txt);font-weight:700;white-space:nowrap}
@media(max-width:430px){
  .th-mkt{flex-wrap:wrap}
  .th-mkt-cell{flex:1 1 46%;min-width:80px;padding:4px var(--s2)}
  .th-mkt-cell:nth-child(even){border-right:none}
  .th-mkt-cell:nth-child(odd):not(:last-child){border-right:1px solid var(--line)}
  .th-ch{height:60px}
  .th-ch-tag,.th-ch-cur,.th-ch-base-lbl,.th-ch-pnl{font-size:9px}
  .th-rr{padding:7px 8px 6px}
  .th-rr-h{font-size:8px}
  .th-rr-rr{font-size:10px}
  .th-rr-labels{font-size:9px}
}
@media(max-width:560px){
  .pf-pnl-row{grid-template-columns:1fr 50px;grid-template-areas:"tk val" "bar bar";row-gap:3px}
  .pf-pnl-row > .pf-pnl-tk{grid-area:tk;min-width:0}
  .pf-pnl-row > .pf-pnl-dir{display:none}
  .pf-pnl-row > .pf-pnl-track{grid-area:bar}
  .pf-pnl-row > .pf-pnl-val{grid-area:val}
}
/* Sector Performance Attribution — Brinson-style "where is my book working?" (HED-137 cycle 83) */
.pf-attrib{margin-top:var(--s3);padding:var(--s3)}
.pf-attrib-h{font-size:var(--fs-cap);color:var(--mut);margin-bottom:var(--s2);display:flex;
  justify-content:space-between;align-items:baseline;gap:var(--s3)}
.pf-attrib-h .pf-attrib-foot{font-size:var(--fs-micro)}
.pf-attrib-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.pf-attrib-tbl thead th{font-size:var(--fs-micro);font-weight:600;text-transform:uppercase;letter-spacing:.05em;
  color:var(--mut);padding:4px 6px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
.pf-attrib-tbl thead th:first-child{text-align:left;padding-left:0}
.pf-attrib-tbl thead th.pf-attrib-bar-col{text-align:left;width:32%}
.pf-attrib-tbl tbody td{padding:6px;border-top:1px solid var(--line);text-align:right;vertical-align:middle}
.pf-attrib-tbl tbody td:first-child{text-align:left;padding-left:0}
.pf-attrib-tbl tbody tr:first-child td{border-top:0}
.pf-attrib-tbl tbody tr:hover{background:rgba(77,163,255,.06)}
.pf-attrib-sec{display:flex;align-items:center;gap:6px;min-width:0}
.pf-attrib-sec .id{font-size:var(--fs-micro);color:var(--mut);font-weight:600;font-variant-numeric:tabular-nums;
  background:var(--panel2);padding:1px 5px;border-radius:3px;flex-shrink:0}
.pf-attrib-sec .nm{color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pf-attrib-tbl tfoot td{padding:6px;border-top:2px solid var(--line);font-weight:700;text-align:right}
.pf-attrib-tbl tfoot td:first-child{text-align:left;padding-left:0;color:var(--mut);font-weight:600;font-size:var(--fs-micro);
  text-transform:uppercase;letter-spacing:.05em}
/* contribution bar: diverging around 0, half-track each side */
.pf-attrib-ctrack{position:relative;height:8px;background:transparent;border-left:1px solid var(--line);
  border-right:1px solid var(--line);min-width:60px}
.pf-attrib-ctrack::before{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--line)}
.pf-attrib-cbar{position:absolute;top:1px;bottom:1px;border-radius:2px}
.pf-attrib-cbar-pos{background:var(--green);left:50%}
.pf-attrib-cbar-neg{background:var(--red);right:50%}
@media(max-width:560px){
  .pf-attrib-tbl thead th.pf-attrib-hide-mob,.pf-attrib-tbl tbody td.pf-attrib-hide-mob,.pf-attrib-tbl tfoot td.pf-attrib-hide-mob{display:none}
  .pf-attrib-tbl thead th,.pf-attrib-tbl tbody td{padding:5px 4px}
  .pf-attrib-tbl thead th.pf-attrib-bar-col{width:30%}
}
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
  /* scored track-record table → card layout */
  .tr-tbl{display:block}
  .tr-tbl thead{display:none}
  .tr-tbl tbody,.tr-tbl tr{display:block}
  .tr-tbl tr{padding:var(--s3) 0;border-bottom:1px solid var(--line)}
  .tr-tbl td,.tr-tbl tbody th{display:flex;justify-content:space-between;gap:var(--s3);padding:2px 0;border:0;text-align:right}
  .tr-tbl .num{text-align:right}
  .tr-tbl .dlabel{display:inline-block;min-width:90px;vertical-align:top}
  /* pending-theses table (5-col) → card layout on mobile to prevent horizontal overflow */
  .tr-pending{display:block;width:100%;overflow:hidden}
  .tr-pending thead{display:none}
  .tr-pending tbody,.tr-pending tr{display:block}
  .tr-pending tr{padding:var(--s3) 0;border-bottom:1px solid var(--line)}
  .tr-pending td,.tr-pending tbody th{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s2);padding:3px 0;border:0;font-size:var(--fs-cap)}
  .tr-pending td::before,.tr-pending tbody th::before{content:attr(data-label);color:var(--mut);font-size:var(--fs-micro);flex-shrink:0;min-width:80px}
  /* call chips strip: thumb-friendly sticky bar when briefing is scrolled */
  .calls-strip{position:sticky;top:0;z-index:20;background:var(--bg);
    padding:var(--s2) 0;margin-bottom:var(--s3);
    border-bottom:1px solid var(--line)}
  /* sector row: hide secondary data columns to prevent overflow; live price + change_pct stay */
  .sec-row .w52{display:none}
  .sec-row .rsi{display:none}
  /* section nav: slightly larger tap targets */
  .sec-nav a{padding:5px var(--s3);min-height:36px;display:inline-flex;align-items:center}
}
/* ============================================================
   MOBILE-FIRST: 430px — phone viewport (iPhone 14/SE, Pixel 7)
   Goal: one-thumb navigation during market open, no pinch-zoom
   ============================================================ */
@media (max-width:430px){
  /* Tighter lateral padding: 12px gutter frees 24px viewport width vs desktop 24px */
  .wrap{padding:var(--s4) var(--s3)}
  /* h1 shrink: "AI/Tech Fund — Intelligence Dashboard" wraps on 390px at 22px */
  h1{font-size:17px;line-height:1.35}
  /* sub-text: keep readable */
  .sub{font-size:10px}
  /* KPI cards: 2-col is fine at 430px, but shrink padding so numbers breathe */
  .kpi{font-size:26px}
  .panel.kpi-dl{padding:var(--s3)}
  /* Call chips: 44px touch target (WCAG 2.5.5 AAA, Apple HIG) */
  .call-chip{min-height:44px;padding:var(--s2) var(--s3);gap:5px;border-radius:8px}
  /* Thesis cards: reduce padding on phone */
  .thesis{padding:var(--s3) var(--s2)}
  /* Back-to-top: respect iOS safe area at bottom */
  .to-top{bottom:calc(var(--s4) + env(safe-area-inset-bottom, 0px))}
  /* Section nav pills: ensure 44px touch height */
  .sec-nav a{min-height:44px;padding:var(--s2) var(--s3)}
  /* Portfolio risk chips: allow wrap so no overflow at 375px */
  .pf-risk-chips{gap:var(--s1)}
  .pf-risk-chip{font-size:10px;padding:3px 8px}
  /* Sector tile: tighter internal padding */
  .sec-tile{padding:var(--s3) var(--s3)!important}
}
/* Very narrow: iPhone SE (375px) and Galaxy A series (360px) */
@media (max-width:375px){
  .wrap{padding:var(--s3) var(--s2)}
  .cards{grid-template-columns:1fr}
  .kpi{font-size:24px}
  /* KPI grid single column: 180px min too wide for two at 360px - 16px gutter */
  .pf-grid{grid-template-columns:1fr 1fr}
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
    <a href="#h-catalysts">Katalysatoren</a>
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

  <section aria-labelledby="h-catalysts">
  <h2 id="h-catalysts">Katalysator-Runway</h2>
  <div id="catalysts" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:62%"></div></div></div>
  </section>

  <section aria-labelledby="h-sectorview">
  <h2 id="h-sectorview">Sektor-Ansicht <span id="secstand" class="tag"></span></h2>
  <div class="grid sectors" id="sectorview" aria-busy="true"><div class="skel skel-tile" aria-hidden="true"></div><div class="skel skel-tile" aria-hidden="true"></div><div class="skel skel-tile" aria-hidden="true"></div></div>
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
  // Show ALL theses sorted by conviction desc — distinct horizons on same ticker are complementary, not duplicates
  const theses=rawTheses.slice().sort((a,b)=>(b.conviction??-1)-(a.conviction??-1));
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
  // Price-context map: ticker → sector_view data for embedding in thesis cards
  const _mktMap={};
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(tk=>{if(tk&&tk.ticker)_mktMap[String(tk.ticker).toUpperCase()]=tk;});
  });
  // Track-record baseline map: ticker → {bp, dir} from highest-conviction active thesis per ticker
  const _baseMap={};
  ((D.track_record||{}).theses||[]).forEach(t=>{
    if(t.baseline_price==null) return;
    (t.tickers||[]).forEach(tk=>{
      const key=String(tk).toUpperCase();
      if(!_baseMap[key]||(_baseMap[key].conv||0)<(t.conviction||0))
        _baseMap[key]={bp:t.baseline_price,dir:(t.direction||"").toLowerCase(),conv:t.conviction||0};
    });
  });
  // thMktHtml: returns the Bloomberg-style price-context bar for a thesis card
  function thMktHtml(t){
    const tks=(t.tickers||[]).map(x=>String(x).toUpperCase()).filter(Boolean);
    if(!tks.length) return "";
    const cells=tks.map(tk=>{
      const m=_mktMap[tk]; if(!m||m.price==null) return "";
      const up1=(m.change_pct||0)>0.005,dn1=(m.change_pct||0)<-0.005;
      const sign1=up1?"+":"−",cls1=up1?"up":dn1?"dn":"flat";
      const chgTip=`${tk} — heute ${sign1}${Math.abs(m.change_pct||0).toFixed(2)}%`;
      const priceCell=`<div class="th-mkt-cell"><div class="th-mkt-lbl">${esc(tk)}</div><div class="th-mkt-val ${cls1}" title="${esc(chgTip)}">$${m.price.toFixed(2)} <span style="font-weight:500;opacity:.8">${sign1}${Math.abs(m.change_pct||0).toFixed(1)}%</span></div></div>`;
      let callCell="";
      const base=_baseMap[tk];
      if(base){
        const dir=(t.direction||base.dir||"").toLowerCase();
        const rawPct=((m.price-base.bp)/base.bp)*100;
        const pnl=dir==="short"?-rawPct:rawPct;
        const up=pnl>=0.1,dn=pnl<=-0.1;
        const confCls=up?"mkt-confirm":dn?"mkt-against":"";
        const clsPnl=up?"up":dn?"dn":"flat";
        const pnlTip=`Baseline $${base.bp} → $${m.price.toFixed(2)} — ${up?"+":"−"}${Math.abs(pnl).toFixed(2)}% seit Call (${dir.toUpperCase()})`;
        callCell=`<div class="th-mkt-cell ${confCls}"><div class="th-mkt-lbl">seit Call</div><div class="th-mkt-val ${clsPnl}" title="${esc(pnlTip)}">${up?"+":"−"}${Math.abs(pnl).toFixed(2)}%</div></div>`;
      }
      let rangeCell="";
      if(m.pct_of_52w_high!=null){
        const pct52=Math.min(100,Math.max(2,m.pct_of_52w_high));
        const rTip=`52W Hoch $${m.w52_high??'?'} · Tief $${m.w52_low??'?'} · ${pct52.toFixed(0)}% vom Jahreshoch`;
        rangeCell=`<div class="th-mkt-cell" title="${esc(rTip)}"><div class="th-mkt-lbl">52W-Position</div><div class="th-mkt-val flat">${pct52.toFixed(0)}%</div><div class="th-52w-bar" aria-hidden="true"><div class="th-52w-fill" style="width:${pct52}%"></div></div></div>`;
      }
      let techCell="";
      if(m.rsi14!=null||m.pct_vs_ma30!=null){
        const rsiCls=m.rsi14>70?"up":m.rsi14<30?"dn":"flat";
        const rsiTip=(m.rsi14!=null?`RSI14: ${m.rsi14} (${m.rsi14>70?"overbought":m.rsi14<30?"oversold":"neutral"})`:"")+
          (m.pct_vs_ma30!=null?(m.rsi14!=null?" · ":"")+`MA30: ${m.pct_vs_ma30>=0?"+":""}${m.pct_vs_ma30?.toFixed(1)}%`:"");
        const techLbl=m.rsi14!=null&&m.pct_vs_ma30!=null?"RSI · MA30":m.rsi14!=null?"RSI14":"MA30";
        const techVal=m.rsi14!=null&&m.pct_vs_ma30!=null?`${m.rsi14} / ${m.pct_vs_ma30>=0?"+":""}${m.pct_vs_ma30?.toFixed(1)}%`:
          m.rsi14!=null?String(m.rsi14):`${m.pct_vs_ma30>=0?"+":""}${m.pct_vs_ma30?.toFixed(1)}%`;
        techCell=`<div class="th-mkt-cell"><div class="th-mkt-lbl">${techLbl}</div><div class="th-mkt-val ${rsiCls}" title="${esc(rsiTip)}">${techVal}</div></div>`;
      }
      return priceCell+callCell+rangeCell+techCell;
    }).filter(Boolean).join("");
    return cells?`<div class="th-mkt">${cells}</div>`:"";
  }
  // thChart: 30-day price-history mini-chart embedded inside the thesis card.
  // Visualises the call's risk/reward visually: baseline reference, current price endpoint,
  // and the P&L area between baseline and price (direction-aware coloring: shorts invert).
  function thChart(t){
    const tks=(t.tickers||[]).map(x=>String(x).toUpperCase()).filter(Boolean);
    if(!tks.length) return "";
    const tk=tks[0];
    const m=_mktMap[tk];
    if(!m||!Array.isArray(m.spark)||m.spark.length<5) return "";
    const closes=m.spark.slice();
    if(m.price!=null && Math.abs(m.price-closes[closes.length-1])>0.005) closes.push(m.price);
    const dir=(t.direction||(_baseMap[tk]&&_baseMap[tk].dir)||"").toLowerCase();
    const baseline=_baseMap[tk]?_baseMap[tk].bp:null;
    const W=400,H=68,padT=10,padB=10;
    const vals=baseline!=null?closes.concat([baseline]):closes;
    let yMin=Math.min(...vals),yMax=Math.max(...vals);
    const buf=(yMax-yMin)*0.05||yMax*0.02||1;
    yMin-=buf; yMax+=buf;
    const rng=(yMax-yMin)||1;
    const xStep=W/(closes.length-1);
    const xAt=i=>i*xStep;
    const yAt=v=>padT+(yMax-v)/rng*(H-padT-padB);
    const linePts=closes.map((v,i)=>`${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
    const cur=closes[closes.length-1];
    let pnlPct=null;
    if(baseline!=null){
      const rawPct=((cur-baseline)/baseline)*100;
      pnlPct=dir==="short"?-rawPct:rawPct;
    }
    const sem=pnlPct==null?"flat":pnlPct>=0.1?"up":pnlPct<=-0.1?"dn":"flat";
    let areaShade="",baseLine="";
    if(baseline!=null){
      const by=yAt(baseline).toFixed(1);
      const rev=`${xAt(closes.length-1).toFixed(1)},${by} ${xAt(0).toFixed(1)},${by}`;
      areaShade=`<polygon class="th-ch-area-${sem}" points="${linePts} ${rev}"/>`;
      baseLine=`<line class="th-ch-base" x1="0" y1="${by}" x2="${W}" y2="${by}"/>`;
    }
    const lastX=xAt(closes.length-1),lastY=yAt(cur);
    const dot=`<circle class="th-ch-pt-${sem}" cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="2.8"/>`;
    const fmt=v=>v<10?v.toFixed(2):v<100?v.toFixed(1):v.toFixed(0);
    const baseLblTop=baseline!=null?`${(yAt(baseline)/H*100).toFixed(1)}%`:null;
    const overlays=[
      `<span class="th-ch-tag">30D · ${esc(tk)}</span>`,
      `<span class="th-ch-cur">$${fmt(cur)}</span>`,
      baseline!=null?`<span class="th-ch-base-lbl" style="top:${baseLblTop}">$${fmt(baseline)} Call</span>`:"",
      pnlPct!=null?`<span class="th-ch-pnl th-ch-pnl-${sem}">${pnlPct>=0?"+":"−"}${Math.abs(pnlPct).toFixed(1)}%</span>`:""
    ].filter(Boolean).join("");
    const tip=`${tk} — 30 Tage Preisverlauf${baseline!=null?` · Baseline $${baseline.toFixed(2)}`:""} · aktuell $${cur.toFixed(2)}${pnlPct!=null?` (${pnlPct>=0?"+":"−"}${Math.abs(pnlPct).toFixed(1)}% seit Call ${dir.toUpperCase()})`:""}`;
    return `<div class="th-ch" title="${esc(tip)}" role="img" aria-label="${esc(tip)}">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="${H}">
        ${areaShade}${baseLine}
        <polyline class="th-ch-line th-ch-line-${sem}" points="${linePts}"/>
        ${dot}
      </svg>${overlays}</div>`;
  }
  // thRiskReward: Street-consensus Risk/Reward barometer (PT-Low | current | PT-High).
  // Shows call-perspective reward (toward target) vs risk (toward stop) and the R/R ratio.
  // Long: pt_high = target/green, pt_low = risk/red. Short: inverted.
  function thRiskReward(t){
    const tks=(t.tickers||[]).map(x=>String(x).toUpperCase()).filter(Boolean);
    if(!tks.length) return "";
    const tk=tks[0];
    const m=_mktMap[tk];
    if(!m||!m.consensus||m.price==null) return "";
    const c=m.consensus;
    const lo=c.pt_low, hi=c.pt_high, mean=c.pt_mean;
    if(lo==null||hi==null||hi<=lo) return "";
    const cur=m.price;
    const dir=(t.direction||(_baseMap[tk]&&_baseMap[tk].dir)||"long").toLowerCase();
    const rngLo=Math.min(lo,cur,mean!=null?mean:cur),rngHi=Math.max(hi,cur,mean!=null?mean:cur);
    const span=rngHi-rngLo; if(span<=0) return "";
    const pos=v=>((v-rngLo)/span*100);
    const posCur=pos(cur),posLo=pos(lo),posHi=pos(hi);
    const posMean=mean!=null?pos(mean):null;
    const dLo=((cur-lo)/cur)*100;  // % drop to reach pt_low
    const dHi=((hi-cur)/cur)*100;  // % rise to reach pt_high
    const reward=dir==="short"?dLo:dHi;
    const risk  =dir==="short"?dHi:dLo;
    const rr=risk>0.5?reward/risk:null;
    const rrCls=rr==null?"muted":rr>=2?"th-rr-rr-good":rr>=1?"th-rr-rr-warn":"th-rr-rr-bad";
    const fmtP=v=>v.toFixed(0);
    const fmt$=v=>v<10?v.toFixed(2):v<100?v.toFixed(1):v.toFixed(0);
    const leftIsReward=dir==="short";
    const leftClass=leftIsReward?"th-rr-up":"th-rr-dn";
    const rightClass=leftIsReward?"th-rr-dn":"th-rr-up";
    const leftW=Math.max(0,posCur-posLo);
    const rightW=Math.max(0,posHi-posCur);
    const meanMarker=posMean!=null?`<div class="th-rr-mean" style="left:${posMean.toFixed(1)}%" title="Street PT-Mean $${fmt$(mean)}"></div>`:"";
    const tickLo=`<div class="th-rr-tick" style="left:${posLo.toFixed(1)}%" aria-hidden="true"></div>`;
    const tickHi=`<div class="th-rr-tick" style="left:${posHi.toFixed(1)}%" aria-hidden="true"></div>`;
    const aCount=c.analyst_count?` · n=${c.analyst_count}`:"";
    const recTxt=c.rec?` · ${esc(String(c.rec).replace(/_/g," "))}`:"";
    const tip=`Street PT-Range $${fmt$(lo)} → $${fmt$(hi)}${mean!=null?` · Mean $${fmt$(mean)}`:""}${aCount}${recTxt}`;
    const leftLbl=leftIsReward?`Ziel −${fmtP(Math.abs(dLo))}%`:`Risiko −${fmtP(Math.abs(dLo))}%`;
    const rightLbl=leftIsReward?`Risiko +${fmtP(Math.abs(dHi))}%`:`Ziel +${fmtP(Math.abs(dHi))}%`;
    const rrLbl=rr!=null?`R/R ${rr.toFixed(1)} : 1`:"R/R —";
    return `<div class="th-rr" role="img" aria-label="${esc(tip)}" title="${esc(tip)}">
      <div class="th-rr-h"><span>R/R · Street${aCount}${recTxt}</span>
        <span class="th-rr-rr ${rrCls}">${rrLbl}</span></div>
      <div class="th-rr-bar">
        <div class="${leftClass}" style="left:${posLo.toFixed(1)}%;width:${leftW.toFixed(1)}%"></div>
        <div class="${rightClass}" style="left:${posCur.toFixed(1)}%;width:${rightW.toFixed(1)}%"></div>
        ${tickLo}${tickHi}${meanMarker}
        <div class="th-rr-cur" style="left:${posCur.toFixed(1)}%" title="aktuell $${fmt$(cur)}"></div>
      </div>
      <div class="th-rr-labels">
        <span class="th-rr-lbl">$${fmt$(lo)} · ${leftLbl}</span>
        <span class="th-rr-cur-lbl">$${fmt$(cur)} jetzt</span>
        <span class="th-rr-lbl th-rr-lbl-r">$${fmt$(hi)} · ${rightLbl}</span>
      </div>
    </div>`;
  }
  if(theses.length){
    html+='<div class="brief-aside"><h2 class="brief-aside-h2">Thesen & Devil\'s Advocate</h2>';
    html+=theses.map((t,i)=>{
      const c=cmap[t.id]||{};
      return `<div class="thesis" id="thesis-${i+1}" tabindex="-1"><div class="h"><span class="idx-badge" aria-label="Diese ${i+1}">${i+1}</span>${(t.tickers||[]).join(", ")}
        <span class="cd ${dirClass(t.direction)}">${t.direction||""}</span>
        <span class="${t.conviction!=null?convCls(t.conviction):'muted'}" title="${t.conviction!=null?convTip(t.conviction):''}">· Conv ${t.conviction!=null?t.conviction.toFixed(2):"—"}</span>
        ${t.horizon?`<span class="th-horizon" title="Zeithorizont der These">${esc(t.horizon)}</span>`:""}</div>
        ${thMktHtml(t)}
        ${thChart(t)}
        ${thRiskReward(t)}
        <div lang="en" style="margin-top:4px">${esc(t.thesis||"")}</div>
        ${t.edge&&t.is_differentiated?`<div class="edge-line">🎯 ${esc(t.edge)}</div>`:""}
        ${t.scenarios?(()=>{const s=t.scenarios;const fmtS=(k,c)=>{if(!c)return null;const tgt=c.target?` → ${esc(c.target)}`:"";const p=c.prob!=null?` (P=${Math.round(c.prob*100)}%)`:"";return `${k}${c.trigger?" "+esc(c.trigger):""}${tgt}${p}`;};const parts=[fmtS("Bull",s.bull),fmtS("Base",s.base),fmtS("Bear",s.bear)].filter(Boolean);return parts.length?`<div class="sc-line">📐 ${parts.join(" | ")}</div>`:""})():""}
        ${t.exit_trigger?`<div class="exit-trigger">🚪 Exit: ${esc(t.exit_trigger)}</div>`:""}
        ${(()=>{const pro=Array.isArray(t.bull_case)?t.bull_case.filter(Boolean):[];const con=Array.isArray(t.bear_case)?t.bear_case.filter(Boolean):[];const cat=Array.isArray(t.catalysts)?t.catalysts.filter(Boolean):[];if(!pro.length&&!con.length&&!cat.length)return"";const sect=(cls,icon,label,items)=>items.length?`<div class="ta-sect ta-sect--${cls}"><div class="ta-h"><span aria-hidden="true">${icon}</span> ${label} <span class="ta-n">${items.length}</span></div><ul class="ta-list" lang="en">${items.map(x=>`<li>${esc(x)}</li>`).join("")}</ul></div>`:"";return `<div class="ta">${sect("pro","✅","Pro-These",pro)}${sect("contra","⚠️","Risiken",con)}${sect("cat","🗓","Katalysatoren",cat)}</div>`;})()}
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
          <th scope="row" data-label="These"><span class="t" style="font-weight:600">${esc(t.label||"?")}</span>
            ${(t.tickers||[]).length?" <span class='muted'>("+
              (t.tickers||[]).map(tk=>`<a class="tkl" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">${esc(tk)}</a>`).join(", ")+
            ")</span>":""}
            ${exitNote(t)}${scenNote(t)}</th>
          <td data-label="Richtung"><span class="cd ${dirClass(t.direction)}">${esc(t.direction||"—")}</span></td>
          <td class="num" data-label="Conv."><span class="${_cc(t.conviction)}" style="font-variant-numeric:tabular-nums">${t.conviction!=null?t.conviction.toFixed(2):"—"}</span></td>
          <td class="num" data-label="P&L">${_mtm(t)||'<span class="muted">—</span>'}</td>
          <td class="sd" data-label="Wertung ab">${esc(t.earliest_score_date||"—")}</td>
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
    (s.tickers||[]).forEach(tk=>{ const sym=tk&&tk.ticker!=null?String(tk.ticker).toUpperCase():(typeof tk==="string"?tk.toUpperCase():null); if(sym) SECTOR_MAP[sym]=s.id+" "+s.name; });
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
  // Live price map for mark-to-market — same source the pending-theses table uses
  const _pricePf={};
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(t=>{if(t&&t.ticker&&t.price!=null)_pricePf[String(t.ticker).toUpperCase()]=t.price;});
  });
  // Per-call mark-to-market: average per-ticker % move, sign-flipped for shorts.
  // Multi-ticker baskets (e.g. TSM+AVGO) get the unweighted mean — they entered together.
  function _callPnl(t){
    const tks=(t.tickers||[]).map(x=>String(x).toUpperCase());
    if(!tks.length||t.baseline_price==null) return null;
    const moves=tks.map(tk=>{const cur=_pricePf[tk];return cur!=null?(cur-t.baseline_price)/t.baseline_price*100:null;}).filter(v=>v!=null);
    if(!moves.length) return null;
    const raw=moves.reduce((a,b)=>a+b,0)/moves.length;
    return (t.direction||"").toLowerCase()==="short" ? -raw : raw;
  }
  const pnlRows=active.map(t=>({t,pnl:_callPnl(t)}));
  const priced=pnlRows.filter(r=>r.pnl!=null);
  const unpriced=pnlRows.length-priced.length;
  // Conviction-weighted aggregate book P&L (the "is my book up?" answer)
  const wConv=priced.reduce((s,r)=>s+(r.t.conviction||0),0);
  const bookPnl=wConv>0?priced.reduce((s,r)=>s+(r.t.conviction||0)*r.pnl,0)/wConv:null;
  const bookSign=bookPnl==null?"":bookPnl>=0?"+":"−";
  const bookCls=bookPnl==null?"kpi--pending":bookPnl>=0.05?"kpi--pos":bookPnl<=-0.05?"kpi--neg":"";
  const bookTxt=bookPnl==null?"—":`${bookSign}${Math.abs(bookPnl).toFixed(2)}%`;
  // KPI strip — Buch-P&L leads (Recognition>Recall: PM scans this first)
  const kpis=[
    ['Buch P&amp;L <span class="muted" style="font-weight:400">(unrealis.)</span>', bookTxt, bookCls, "Konviktions-gewichtete unrealisierte Performance aller aktiven Calls (sign-flipped für Shorts) gegen Live-Kurse"],
    ["Aktive Calls", active.length, "", null],
    ["Long / Short", `${longCalls.length} / ${shortCalls.length}`, "", null],
    ["Net-Exposure", `${netPct>=0?"+":""}${netPct}%`, "", null],
    ["⚖ Devil (Reject)", rejects, rejects>0?"kpi--pending":"", null],
  ];
  const kpiHtml=kpis.map(([k,v,cls,tip])=>
    `<dl class="panel kpi-dl"${tip?` title="${esc(tip)}"`:""}><dt class="muted">${k}</dt><dd class="kpi ${cls||""}">${v}</dd></dl>`
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
  // Buch-Allokation — Position-Sizing-Stack (HED-137 Zyklus 87).
  // Each call rendered as a segment within its side's stack: width = within-side conviction %,
  // color = unrealized P&L bucket. Sorted by conviction desc — biggest position-sizing visible left.
  // Gross / Net / Leverage above the stacks answer the institutional question:
  // "How direktional ist das Buch wirklich, oder hedgt es sich?"
  const grossConv=longConv+shortConv;
  const netConvSigned=longConv-shortConv;
  const dirPctOfGross=grossConv>0?Math.abs(netConvSigned)/grossConv*100:0;
  const dirTxt=grossConv>0?Math.round(dirPctOfGross)+"%":"—";
  const dirTip=grossConv<=0
    ?"Keine aktiven Calls"
    :dirPctOfGross>=90?"Vollkommen einseitig — kein Hedge-Offset, Buch entspricht netto seiner Brutto-Aufnahme"
    :dirPctOfGross>=60?"Stark direktional — Long und Short sind weit auseinander, schwacher Hedge"
    :dirPctOfGross>=30?"Moderat direktional — teilweise Hedge-Wirkung, aber Buch tendiert zu einer Seite"
    :dirPctOfGross>=10?"Überwiegend gehedged — Long und Short fast in Balance"
    :"Market-neutral — Long und Short halten sich annähernd die Waage";
  const allocSegCls=function(pnl){
    if(pnl==null) return "pf-alloc-seg-unpriced";
    if(pnl>=5) return "pf-alloc-seg-pos-strong";
    if(pnl>0.5) return "pf-alloc-seg-pos";
    if(pnl<=-5) return "pf-alloc-seg-neg-strong";
    if(pnl<-0.5) return "pf-alloc-seg-neg";
    return "pf-alloc-seg-flat";
  };
  function _allocSideStack(rows, sideConv, sideLabel){
    if(!rows.length) return `<div class="pf-alloc-empty">Keine ${esc(sideLabel)}-Positionen</div>`;
    const sorted=rows.slice().sort((a,b)=>(b.t.conviction||0)-(a.t.conviction||0));
    const segs=sorted.map(r=>{
      const t=r.t, conv=(t.conviction||0);
      const w=sideConv>0?(conv/sideConv*100):0;
      const tk=(t.tickers||[]).join("·")||"?";
      const pnl=r.pnl;
      const cls=allocSegCls(pnl);
      const pnlTxt=pnl!=null?`${pnl>=0?"+":"−"}${Math.abs(pnl).toFixed(2)}%`:"kein Live-Kurs";
      // Show ticker if segment is wide enough; add P&L if even wider
      const label=w>=14?(pnl!=null?`${tk} ${pnl>=0?"+":"−"}${Math.abs(pnl).toFixed(1)}%`:tk)
                  :w>=7?tk
                  :"";
      const tip=`${tk} · Konv ${conv.toFixed(2)} (${w.toFixed(0)}% der ${sideLabel}-Seite) · ${pnlTxt}${t.label?" · "+t.label:""}`;
      return `<div class="pf-alloc-seg ${cls}" style="flex:${w.toFixed(2)} 0 auto" title="${esc(tip)}" aria-label="${esc(tip)}">${esc(label)}</div>`;
    }).join("");
    return `<div class="pf-alloc-bar" role="img" aria-label="Position-Sizing-Stack ${esc(sideLabel)}: ${rows.length} Calls">${segs}</div>`;
  }
  const longRows=pnlRows.filter(r=>(r.t.direction||"").toLowerCase()==="long");
  const shortRows=pnlRows.filter(r=>(r.t.direction||"").toLowerCase()==="short");
  const netSideLabel=netConvSigned>=0?"Long":"Short";
  const netSign=netConvSigned>=0?"+":"−";
  const allocHtml=`<div class="panel pf-alloc">
    <div class="pf-alloc-h">
      <div>
        <div class="pf-alloc-h-title">Buch-Allokation — Position-Sizing</div>
        <div class="pf-alloc-h-sub">Konviktions-gewichtete Größen je Seite · Segment-Farbe = unrealisierte Performance</div>
      </div>
      <div class="pf-alloc-metrics">
        <div class="pf-alloc-metric" title="Brutto-Exposure: Σ Konviktion (Long + Short) — Gesamt-Risikoaufnahme des Buchs">
          <span class="lbl">Brutto</span><span class="val">${grossConv.toFixed(2)}</span>
        </div>
        <div class="pf-alloc-metric" title="Netto-Exposure: Long − Short — ${Math.round(dirPctOfGross)}% der Brutto-Aufnahme, ${esc(netSideLabel)}-skewed">
          <span class="lbl">Netto ${esc(netSideLabel)}</span><span class="val">${netSign}${Math.abs(netConvSigned).toFixed(2)}</span>
        </div>
        <div class="pf-alloc-metric" title="${esc(dirTip)} · |Netto|/Brutto · 0% = market-neutral, 100% = vollkommen einseitig">
          <span class="lbl">Direktional</span><span class="val">${esc(dirTxt)}</span>
        </div>
      </div>
    </div>
    <div class="pf-alloc-sides">
      <div>
        <div class="pf-alloc-side-h"><span>Long-Seite</span><span><b>${longCalls.length}</b> Call${longCalls.length===1?"":"s"} · <b>${longConv.toFixed(2)}</b> Konviktion · <b>${totalConv>0?Math.round(longConv/totalConv*100):0}%</b> der Brutto</span></div>
        ${_allocSideStack(longRows, longConv, "Long")}
      </div>
      <div>
        <div class="pf-alloc-side-h"><span>Short-Seite</span><span><b>${shortCalls.length}</b> Call${shortCalls.length===1?"":"s"} · <b>${shortConv.toFixed(2)}</b> Konviktion · <b>${totalConv>0?Math.round(shortConv/totalConv*100):0}%</b> der Brutto</span></div>
        ${_allocSideStack(shortRows, shortConv, "Short")}
      </div>
    </div>
    <div class="pf-alloc-foot">
      <span class="pf-alloc-legend">
        <span class="pf-alloc-legend-chip"><span class="pf-alloc-legend-sw" style="background:#2da347"></span>≥+5%</span>
        <span class="pf-alloc-legend-chip"><span class="pf-alloc-legend-sw" style="background:rgba(63,185,80,.55)"></span>0 bis +5%</span>
        <span class="pf-alloc-legend-chip"><span class="pf-alloc-legend-sw" style="background:rgba(248,81,73,.55)"></span>−5 bis 0%</span>
        <span class="pf-alloc-legend-chip"><span class="pf-alloc-legend-sw" style="background:#d23a32"></span>≤−5%</span>
        <span class="pf-alloc-legend-chip"><span class="pf-alloc-legend-sw" style="background:repeating-linear-gradient(45deg,var(--panel2),var(--panel2) 3px,rgba(138,160,189,.18) 3px,rgba(138,160,189,.18) 6px)"></span>kein Live-Kurs</span>
      </span>
    </div>
  </div>`;
  // Konzentrationsrisiko: turn the descriptive bars into an actual risk readout a PM would scan
  const dirPct=Math.max(longPct,shortPct);
  const dirSide=longPct>=shortPct?"Long":"Short";
  const nNames=new Set(active.flatMap(t=>(t.tickers||[]).map(x=>String(x).toUpperCase()))).size;
  const topSecPct=secEntries.length?Math.round(secEntries[0][1]/totalConv*100):0;
  const topSecId=secEntries.length?String(secEntries[0][0]).split(" ")[0]:"—";
  const nSectors=secEntries.length;
  const riskChip=(label,val,warn,tip)=>
    `<span class="pf-risk-chip${warn?" pf-risk-chip--warn":""}" title="${esc(tip)}" aria-label="${esc(label+": "+val+(warn?" — Risiko erhöht":" — im Rahmen"))}">${warn?'<span class="pf-risk-mark" aria-hidden="true">⚠</span>':""}${label} · <b>${esc(val)}</b></span>`;
  const riskHtml=`<div class="panel pf-risk">
    <div class="pf-risk-h">Konzentrationsrisiko</div>
    <div class="pf-risk-chips">
      ${riskChip("Richtung",`${dirPct}% ${dirSide}`,dirPct>=80,"Einseitig positioniert — kein Gegen-Hedge bei Marktrückgang (Schwelle ≥80%)")}
      ${riskChip("Top-Sektor",`${topSecId} ${topSecPct}%`,topSecPct>=50,"Hohe Konzentration auf einen Sektor (Schwelle ≥50%)")}
      ${riskChip("Streuung",`${nSectors} Sekt. · ${nNames} Titel`,nSectors<=2,"Geringe Diversifikation über Sektoren (Schwelle ≤2 Sektoren)")}
    </div>
  </div>`;
  // Per-call P&L bars: best→worst signed. Diverging chart around 0, half-track each side.
  let pnlPanelHtml="";
  if(priced.length){
    const maxAbs=Math.max(...priced.map(r=>Math.abs(r.pnl)),0.5); // floor at 0.5% so tiny moves still register
    const sorted=priced.slice().sort((a,b)=>b.pnl-a.pnl);
    const dirCls=d=>d==="long"?"cd-long":d==="short"?"cd-short":"cd-pair";
    const bestPnl=sorted[0].pnl, worstPnl=sorted[sorted.length-1].pnl;
    const rowsHtml=sorted.map(r=>{
      const t=r.t, tks=(t.tickers||[]).join("·"), pnl=r.pnl;
      const up=pnl>=0;
      const w=Math.min(48,Math.abs(pnl)/maxAbs*48); // each half is 48% of track (margin for axis labels)
      const barCls=up?"pf-pnl-bar-pos":"pf-pnl-bar-neg";
      const valCls=up?"move-up":"move-dn";
      const dir=(t.direction||"").toLowerCase();
      const sign=up?"+":"−";
      const tip=`${esc(t.label||"")} — Baseline $${t.baseline_price} · ${tks} · ${dir.toUpperCase()}${t.conviction!=null?" · Conv "+t.conviction.toFixed(2):""}`;
      return `<div class="pf-pnl-row" title="${tip}"><div class="pf-pnl-tk"><b>${esc(tks)}</b><span class="pf-pnl-lbl">${esc(t.label||"")}</span></div>`+
        `<span class="pf-pnl-dir cd ${dirCls(dir)}" aria-label="${esc(dir.toUpperCase())}">${esc(dir.toUpperCase().slice(0,1))}</span>`+
        `<div class="pf-pnl-track" aria-label="${sign}${Math.abs(pnl).toFixed(2)} Prozent"><div class="pf-pnl-bar ${barCls}" style="width:${w}%"></div></div>`+
        `<span class="pf-pnl-val ${valCls}">${sign}${Math.abs(pnl).toFixed(2)}%</span></div>`;
    }).join("");
    // Best/worst caption — gives PM the headline before he scans the bars
    const bestTk=(sorted[0].t.tickers||[])[0]||"?";
    const worstTk=(sorted[sorted.length-1].t.tickers||[])[0]||"?";
    const meta=sorted.length>1
      ? `Bester: <b>${esc(bestTk)}</b> ${bestPnl>=0?"+":"−"}${Math.abs(bestPnl).toFixed(2)}% · Schwächster: <b>${esc(worstTk)}</b> ${worstPnl>=0?"+":"−"}${Math.abs(worstPnl).toFixed(2)}%`
      : "";
    const unpricedNote=unpriced>0?` <span class="muted">· ${unpriced} ohne Live-Kurs</span>`:"";
    pnlPanelHtml=`<div class="panel" style="margin-top:var(--s3)">
      <div class="pf-pnl-h"><span>Buch-Performance — per Call (unrealisiert)${unpricedNote}</span><span class="pf-pnl-meta muted">${meta}</span></div>
      ${rowsHtml}
    </div>`;
  } else if(active.length){
    pnlPanelHtml=`<div class="panel" style="margin-top:var(--s3)">
      <div class="pf-pnl-h"><span>Buch-Performance — per Call (unrealisiert)</span></div>
      <div class="pf-pnl-empty">Noch keine Live-Kurse für die aktiven Calls. Wird mit dem nächsten Sector-View-Refresh befüllt.</div>
    </div>`;
  }
  // Buch-Equity-Kurve: konviktions-gewichtete Performance-Linie seit Inception.
  // Quelle: spark (letzte 30 Tagesschlüsse) je Ticker im sector_view; Entry-Tag wird
  // anhand des Spark-Index ermittelt, der dem baseline_price am nächsten liegt — kein
  // Datums-Schema im Spark, daher Auto-Snap. Kurve wächst täglich.
  const _sparkMap={};
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(t=>{ if(t&&t.ticker&&Array.isArray(t.spark)&&t.spark.length>=2) _sparkMap[String(t.ticker).toUpperCase()]=t.spark; });
  });
  // Map thesis entry date → spark index. Spark[-1] = last close before sv.as_of (today).
  // Count business days between entry-date and as_of-date to derive the offset.
  // Verify against baseline_price within 5% (slack: baselines sometimes set at prior-day close).
  // Fall back to tight auto-snap within last 5 trading days only — prevents misleading older snaps.
  const _asOfDateStr=((D.sector_view||{}).as_of_iso||(D.sector_view||{}).as_of||"").replace(/ UTC.*/,"").slice(0,10);
  function _bdaysBack(dStr, asOfStr){
    if(!dStr||!asOfStr) return null;
    try{
      const d1=new Date(dStr+"T00:00:00Z");
      const d2=new Date(asOfStr+"T00:00:00Z");
      if(isNaN(d1)||isNaN(d2)||d1>d2) return null;
      let cur=new Date(d1), n=0;
      while(cur<d2){ cur.setUTCDate(cur.getUTCDate()+1); const dow=cur.getUTCDay(); if(dow!==0&&dow!==6) n++; }
      return n;
    }catch(e){ return null; }
  }
  function _entryIdx(spark, baseline, dateStr){
    if(!spark||!spark.length||baseline==null) return -1;
    // 1) Prefer date-derived index from thesis.date when within 5% of baseline
    if(dateStr && _asOfDateStr){
      const back=_bdaysBack(dateStr, _asOfDateStr);
      if(back!=null){
        const idx=spark.length-1-back;
        if(idx>=0 && idx<spark.length && Math.abs(spark[idx]-baseline)/baseline<0.05) return idx;
      }
    }
    // 2) Tight auto-snap within last 5 closes — avoids snapping to look-alike prices weeks back
    const start=Math.max(0, spark.length-6);
    let best=-1, bestDiff=Infinity;
    for(let i=start;i<spark.length;i++){
      const d=Math.abs(spark[i]-baseline);
      if(d<bestDiff){ bestDiff=d; best=i; }
    }
    return (best>=0 && Math.abs(spark[best]-baseline)/baseline<0.01) ? best : -1;
  }
  const _curveSrc=[];
  active.forEach(t=>{
    const tk=(t.tickers||[])[0];
    if(!tk||t.baseline_price==null) return;
    const sp=_sparkMap[String(tk).toUpperCase()];
    if(!sp||sp.length<2) return;
    const eIdx=_entryIdx(sp, t.baseline_price, t.date);
    if(eIdx<0||eIdx>=sp.length-1) return;
    const eOff=sp.length-1-eIdx;
    const sign=(t.direction||"").toLowerCase()==="short"?-1:1;
    _curveSrc.push({conv:(t.conviction!=null?t.conviction:0.5), baseline:t.baseline_price, spark:sp, eOff, sign});
  });
  let curvePanelHtml="";
  if(_curveSrc.length){
    const _incep=Math.max(..._curveSrc.map(s=>s.eOff));
    const _curve=[];
    for(let off=_incep; off>=0; off--){
      let wS=0, rS=0, n=0;
      _curveSrc.forEach(s=>{
        if(s.eOff>=off){
          const idx=s.spark.length-1-off;
          if(idx>=0){
            const close=s.spark[idx];
            const r=(close-s.baseline)/s.baseline*100*s.sign;
            wS+=s.conv; rS+=s.conv*r; n++;
          }
        }
      });
      if(wS>0) _curve.push({off, pct:rS/wS, n});
    }
    if(_curve.length){
      const W=720, H=140;
      const pad={l:42,r:16,t:14,b:24};
      const iW=W-pad.l-pad.r, iH=H-pad.t-pad.b;
      const pcts=_curve.map(c=>c.pct);
      let lo=Math.min(0,...pcts), hi=Math.max(0,...pcts);
      if(hi-lo<1){ const mid=(hi+lo)/2; lo=mid-0.5; hi=mid+0.5; }
      const yPad=(hi-lo)*0.15; lo-=yPad; hi+=yPad;
      const yPct=v=>pad.t+(hi-v)/(hi-lo)*iH;
      const yZero=yPct(0);
      const xStep=_curve.length>1?iW/(_curve.length-1):0;
      const ptCoords=_curve.map((c,i)=>[pad.l+i*xStep, yPct(c.pct)]);
      const linePts=ptCoords.map(([x,y])=>`${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
      const lastPct=_curve[_curve.length-1].pct;
      const lineCls=lastPct>=0?"ec-pos":"ec-neg";
      // Area between line and zero baseline (clipped to chart area visually via the polyline shape)
      const areaPath = ptCoords.length>1
        ? `M ${pad.l},${yZero.toFixed(1)} ` + ptCoords.map(([x,y])=>`L ${x.toFixed(1)},${y.toFixed(1)}`).join(" ") + ` L ${(pad.l+(ptCoords.length-1)*xStep).toFixed(1)},${yZero.toFixed(1)} Z`
        : "";
      const fmt=v=>(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%";
      // Y-axis: top and bottom labels; zero label only if zero is well inside the band
      const showZeroLab = (yZero>pad.t+8 && yZero<pad.t+iH-8);
      const yLabHtml = [
        `<text class="ec-ylab" x="${pad.l-6}" y="${(pad.t+4).toFixed(1)}" text-anchor="end">${fmt(hi)}</text>`,
        showZeroLab ? `<text class="ec-ylab" x="${pad.l-6}" y="${(yZero+3).toFixed(1)}" text-anchor="end">0.00%</text>` : "",
        `<text class="ec-ylab" x="${pad.l-6}" y="${(pad.t+iH).toFixed(1)}" text-anchor="end">${fmt(lo)}</text>`
      ].filter(Boolean).join("");
      const xLabHtml = `<text class="ec-xlab" x="${pad.l}" y="${(H-7).toFixed(1)}">Inception</text>`+
                       `<text class="ec-xlab" x="${(pad.l+iW).toFixed(1)}" y="${(H-7).toFixed(1)}" text-anchor="end">Heute</text>`;
      const zeroLine = showZeroLab
        ? `<line class="ec-zero" x1="${pad.l}" y1="${yZero.toFixed(1)}" x2="${(pad.l+iW).toFixed(1)}" y2="${yZero.toFixed(1)}"/>`
        : `<line class="ec-zero" x1="${pad.l}" y1="${yZero.toFixed(1)}" x2="${(pad.l+iW).toFixed(1)}" y2="${yZero.toFixed(1)}"/>`;
      const lastP=ptCoords[ptCoords.length-1];
      const lastDot = `<circle class="ec-dot ${lineCls}" cx="${lastP[0].toFixed(1)}" cy="${lastP[1].toFixed(1)}" r="4"/>`;
      // Daily markers — tick + tooltip on each point (Recognition>Recall: hovering reveals exact value)
      const dots = ptCoords.map(([x,y],i)=>{
        const c=_curve[i];
        return `<circle class="ec-tick" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5"><title>Tag −${c.off}: ${fmt(c.pct)} (${c.n} aktive Calls)</title></circle>`;
      }).join("");
      // Benchmark overlay: SPY 30-day spark vs book — shows alpha generation context (HED-137 cycle 91).
      // Align from right (both series end at "today"); normalize SPY at the overlap start.
      let benchSvg="", spyAlphaPct=null;
      const _spySpark=((D.sector_view||{}).benchmarks||{})?.SPY?.spark;
      if(_spySpark && _spySpark.length>=2 && ptCoords.length>=2){
        const _overlap=Math.min(ptCoords.length, _spySpark.length);
        const _spySlice=_spySpark.slice(_spySpark.length-_overlap); // last _overlap prices
        const _spyBase=_spySlice[0];
        if(_spyBase>0){
          const _spyPcts=_spySlice.map(p=>(p-_spyBase)/_spyBase*100);
          // x coords: align to the RIGHT portion of ptCoords (last _overlap points)
          const _xOff=ptCoords.length-_overlap;
          const _spyCoords=_spyPcts.map((sp,i)=>[ptCoords[_xOff+i][0], yPct(sp)]);
          const _spyPts=_spyCoords.map(([x,y])=>`${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
          const _spyEnd=_spyPcts[_spyPcts.length-1];
          // Label at end of line — right edge
          const _spyLabelX=(ptCoords[ptCoords.length-1][0]+4).toFixed(1);
          const _spyLabelY=yPct(_spyEnd).toFixed(1);
          spyAlphaPct=lastPct-_spyEnd; // positive = outperforming
          benchSvg=`<polyline class="ec-bench" points="${_spyPts}"/>`
            +`<text class="ec-bench-label" x="${_spyLabelX}" y="${_spyLabelY}" dy="3">SPY</text>`;
        }
      }
      const svg = `<svg class="ec-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Buch-Equity-Kurve seit Inception als Liniendiagramm — aktuell ${fmt(lastPct)}">
        ${zeroLine}
        ${benchSvg}
        ${areaPath?`<path class="ec-area ${lineCls}" d="${areaPath}"/>`:""}
        ${_curve.length>1?`<polyline class="ec-line ${lineCls}" points="${linePts}"/>`:""}
        ${dots}
        ${lastDot}
        ${yLabHtml}
        ${xLabHtml}
      </svg>`;
      // Peak / max-drawdown stats over the live window
      const daysLive=_curve.length-1;
      const peakPct=Math.max(0,...pcts);
      let runMax=-Infinity, maxDD=0, _maxDDIdx=0;
      const ddArr=pcts.map((p,i)=>{ if(p>runMax) runMax=p; const dd=p-runMax; if(dd<maxDD){ maxDD=dd; _maxDDIdx=i; } return dd; });
      const curDD=ddArr[ddArr.length-1];
      // Days underwater: consecutive trailing days with DD < -5bp
      let daysUnderwater=0;
      for(let i=ddArr.length-1;i>=0 && ddArr[i] < -0.005; i--) daysUnderwater++;
      // Underwater chart — aligned to equity-curve x-axis (same pad.l, xStep).
      // Floor the y-axis at ≥0.5% so a flat curve stays visible.
      let ddSvg="";
      if(_curve.length>=2){
        const ddH=72, ddPadT=14, ddPadB=12;
        const ddIH=ddH-ddPadT-ddPadB;
        const ddLo=Math.min(-0.5, maxDD*1.15);
        const yDD=v=>ddPadT+(0-v)/(0-ddLo)*ddIH;
        const ddPts=ddArr.map((d,i)=>[pad.l+i*xStep, yDD(d)]);
        const ddLine=ddPts.map(([x,y])=>`${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
        const xLast=pad.l+(ddPts.length-1)*xStep;
        const ddArea=`M ${pad.l.toFixed(1)},${yDD(0).toFixed(1)} `
          + ddPts.map(([x,y])=>`L ${x.toFixed(1)},${y.toFixed(1)}`).join(" ")
          + ` L ${xLast.toFixed(1)},${yDD(0).toFixed(1)} Z`;
        const ddZeroLine=`<line class="ec-zero" x1="${pad.l}" y1="${yDD(0).toFixed(1)}" x2="${(pad.l+iW).toFixed(1)}" y2="${yDD(0).toFixed(1)}"/>`;
        // Max-DD marker dot
        const minMarker = maxDD<-0.05
          ? `<circle class="dd-min" cx="${(pad.l+_maxDDIdx*xStep).toFixed(1)}" cy="${yDD(maxDD).toFixed(1)}" r="3.5"><title>Tiefster Punkt: ${fmt(maxDD)} (Tag −${_curve[_maxDDIdx].off})</title></circle>`
          : "";
        // Current DD marker (only if currently underwater)
        const curMarker = curDD<-0.05
          ? `<circle class="dd-cur" cx="${xLast.toFixed(1)}" cy="${yDD(curDD).toFixed(1)}" r="3"/>`
          : "";
        const ddYLab=`<text class="ec-ylab" x="${(pad.l-6).toFixed(1)}" y="${(ddPadT+ddIH).toFixed(1)}" text-anchor="end">${fmt(ddLo)}</text>`
          + `<text class="ec-ylab" x="${(pad.l-6).toFixed(1)}" y="${(yDD(0)+3).toFixed(1)}" text-anchor="end">0%</text>`;
        const uwTxt = daysUnderwater>0
          ? `${daysUnderwater} Tag${daysUnderwater===1?"":"e"} unter Hoch`
          : "Auf Hoch";
        const curCls = curDD<-0.05 ? ' class="dd-title-cur"' : '';
        const ddTitle=`<text class="dd-title" x="${pad.l}" y="${(ddPadT-3).toFixed(1)}">Underwater · aktuell <tspan${curCls}>${fmt(curDD)}</tspan> · max ${fmt(maxDD)} · ${uwTxt}</text>`;
        ddSvg=`<svg class="dd-svg" viewBox="0 0 ${W} ${ddH}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Underwater-Drawdown-Chart — aktuell ${fmt(curDD)}, maximal ${fmt(maxDD)}, ${uwTxt}">
          ${ddZeroLine}
          <path class="dd-area" d="${ddArea}"/>
          <polyline class="dd-line" points="${ddLine}"/>
          ${minMarker}
          ${curMarker}
          ${ddYLab}
          ${ddTitle}
        </svg>`;
      }
      const lastCls=lastPct>=0?"move-up":"move-dn";
      const peakCls=peakPct>0?"move-up":"muted";
      const ddCls=maxDD<-0.05?"move-dn":"muted";
      const inceptDate=(active.map(t=>t.date).filter(Boolean).sort()[0])||null;
      curvePanelHtml=`<div class="panel ec-panel" style="margin-top:var(--s3)">
        <div class="ec-h">
          <div class="ec-h-l">
            <div class="ec-title">Buch-Equity-Kurve <span class="muted" style="font-weight:400">(seit Inception, konv-gewichtet)</span></div>
            ${inceptDate?`<div class="ec-h-sub muted">Erste Position: ${esc(inceptDate)} · ${daysLive} Handelstag${daysLive===1?"":"e"} live · ${_curveSrc.length} Calls</div>`:""}
          </div>
          <div class="ec-kpis">
            <div class="ec-kpi" title="Aktuelle konviktions-gewichtete Buch-Performance"><span class="muted">Aktuell</span><b class="${lastCls}">${fmt(lastPct)}</b></div>
            <div class="ec-kpi" title="Höchster Buch-Stand seit Inception"><span class="muted">Hoch</span><b class="${peakCls}">${fmt(peakPct)}</b></div>
            <div class="ec-kpi" title="Größter Rückgang vom Hoch (Drawdown)"><span class="muted">Max DD</span><b class="${ddCls}">${maxDD<-0.005?fmt(maxDD):"0.00%"}</b></div>
            ${spyAlphaPct!=null?`<div class="ec-kpi ec-kpi-alpha" title="Alpha vs SPY über den gemeinsamen Beobachtungszeitraum (Buch − SPY, Prozentpunkte)"><span class="muted">vs SPY</span><b class="${spyAlphaPct>=0?"move-up":"move-dn"}">${spyAlphaPct>=0?"+":"−"}${Math.abs(spyAlphaPct).toFixed(2)}pp</b></div>`:""}
          </div>
        </div>
        ${svg}
        ${ddSvg}
        <div class="ec-foot muted">Honestes Inception-Tracking — die Kurve wächst mit jedem Handelstag. Indexiert bei 0% am Entry-Tag, sign-flipped für Shorts. Underwater-Chart zeigt Drawdown vom rollierenden Hoch (immer ≤ 0).</div>
      </div>`;
    }
  }
  // Sector Performance Attribution — "where is my book working?" Brinson-style.
  // Per sector: conviction-weighted P&L of priced calls in that sector,
  // and contribution to book = sector_weight × sector_pnl.
  // Sum of contributions reconciles to bookPnl (within rounding).
  let attribPanelHtml="";
  if(priced.length){
    const _secAgg={}; // sec → {nCalls, conv, pnlW, calls:[]}
    pnlRows.forEach(r=>{
      const tk=(r.t.tickers||[])[0];
      if(!tk) return;
      const sec=SECTOR_MAP[String(tk).toUpperCase()]||"Other";
      const g=_secAgg[sec]||(_secAgg[sec]={nCalls:0,nPriced:0,conv:0,pnlW:0,convPriced:0});
      g.nCalls++;
      g.conv+=(r.t.conviction||0);
      if(r.pnl!=null){ g.nPriced++; g.convPriced+=(r.t.conviction||0); g.pnlW+=(r.t.conviction||0)*r.pnl; }
    });
    const _attribRows=Object.entries(_secAgg).map(([sec,g])=>{
      const weight=totalConv>0?g.conv/totalConv*100:0;
      const secPnl=g.convPriced>0?g.pnlW/g.convPriced:null;
      const contrib=secPnl!=null?(g.conv/totalConv)*secPnl:null; // weight as decimal × secPnl
      return {sec, nCalls:g.nCalls, nPriced:g.nPriced, weight, secPnl, contrib};
    });
    // Sort by absolute contribution descending — biggest movers (positive or negative) first.
    // Unpriced sectors (contrib=null) trail.
    _attribRows.sort((a,b)=>{
      if((a.contrib==null)!==(b.contrib==null)) return a.contrib==null?1:-1;
      if(a.contrib==null) return b.weight-a.weight;
      return Math.abs(b.contrib)-Math.abs(a.contrib);
    });
    const maxAbsC=Math.max(...(_attribRows.map(r=>r.contrib!=null?Math.abs(r.contrib):0)),0.25); // floor at 0.25% so small contribs still register
    const fmtPct=v=>(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%";
    const fmtWt=v=>v.toFixed(0)+"%";
    const cls=v=>v==null?"muted":v>=0.05?"move-up":v<=-0.05?"move-dn":"muted";
    const totContrib=_attribRows.reduce((s,r)=>s+(r.contrib||0),0);
    const totWeight=_attribRows.reduce((s,r)=>s+r.weight,0);
    const bodyRows=_attribRows.map(r=>{
      const secParts=String(r.sec).split(/\s+/);
      const secId=secParts[0]||"—";
      const secName=secParts.slice(1).join(" ")||r.sec;
      const callsCell=r.nPriced<r.nCalls?`${r.nCalls} <span class="muted" title="${r.nCalls-r.nPriced} ohne Live-Kurs">(${r.nPriced} live)</span>`:`${r.nCalls}`;
      const pnlCell=r.secPnl!=null?`<span class="${cls(r.secPnl)}">${fmtPct(r.secPnl)}</span>`:'<span class="muted">—</span>';
      const contribCell=r.contrib!=null?`<span class="${cls(r.contrib)}"><b>${fmtPct(r.contrib)}</b></span>`:'<span class="muted">—</span>';
      let barCell='<span class="muted" aria-hidden="true">—</span>';
      if(r.contrib!=null){
        const w=Math.min(48,Math.abs(r.contrib)/maxAbsC*48);
        const barCls=r.contrib>=0?"pf-attrib-cbar-pos":"pf-attrib-cbar-neg";
        barCell=`<div class="pf-attrib-ctrack" aria-label="Beitrag ${fmtPct(r.contrib)}"><div class="pf-attrib-cbar ${barCls}" style="width:${w}%"></div></div>`;
      }
      return `<tr>
        <td><div class="pf-attrib-sec"><span class="id">${esc(secId)}</span><span class="nm">${esc(secName)}</span></div></td>
        <td class="pf-attrib-hide-mob">${callsCell}</td>
        <td>${fmtWt(r.weight)}</td>
        <td>${pnlCell}</td>
        <td>${contribCell}</td>
        <td class="pf-attrib-hide-mob">${barCell}</td>
      </tr>`;
    }).join("");
    const reconNote=Math.abs(totContrib-(bookPnl||0))>0.05?` <span class="muted" title="Differenz zwischen Beitrags-Summe und Buch-P&amp;L entsteht durch unpriced Calls">(Rundung)</span>`:"";
    attribPanelHtml=`<div class="panel pf-attrib">
      <div class="pf-attrib-h">
        <span>Performance-Attribution nach Sektor (unrealisiert)</span>
        <span class="pf-attrib-foot muted">Beitrag = Sektor-Gewicht × Sektor-P&amp;L${reconNote}</span>
      </div>
      <table class="pf-attrib-tbl" role="table" aria-label="Performance-Attribution nach Sektor">
        <thead><tr>
          <th>Sektor</th>
          <th class="pf-attrib-hide-mob" title="Anzahl aktive Calls in diesem Sektor">Calls</th>
          <th title="Anteil am Buch (konviktions-gewichtet)">Gewicht</th>
          <th title="Konviktions-gewichtete Performance der Calls in diesem Sektor">Sektor-P&amp;L</th>
          <th title="Beitrag zum Gesamt-Buch-P&L = Gewicht × Sektor-P&L">Beitrag</th>
          <th class="pf-attrib-hide-mob pf-attrib-bar-col" aria-hidden="true"></th>
        </tr></thead>
        <tbody>${bodyRows}</tbody>
        <tfoot><tr>
          <td>Buch gesamt</td>
          <td class="pf-attrib-hide-mob"></td>
          <td>${fmtWt(totWeight)}</td>
          <td></td>
          <td><span class="${cls(bookPnl)}">${bookPnl!=null?fmtPct(bookPnl):"—"}</span></td>
          <td class="pf-attrib-hide-mob"></td>
        </tr></tfoot>
      </table>
    </div>`;
  }
  // Conviction-vs-P&L Scatter — "are my high-conviction calls working?"
  // X: conviction (0.0–1.0); Y: unrealised P&L %; one dot per priced active call.
  // Quadrant dividers at x=0.5 (conviction midpoint) and y=0.
  // If high-conviction dots cluster in the "working" quadrant, the scoring model has edge.
  let scatterPanelHtml="";
  if(priced.length>=2){
    const W=480, H=200;
    const pad={l:44,r:16,t:20,b:34};
    const iW=W-pad.l-pad.r, iH=H-pad.t-pad.b;
    const pnlVals=priced.map(r=>r.pnl);
    const convVals=priced.map(r=>r.t.conviction||0);
    let yLo=Math.min(0,...pnlVals), yHi=Math.max(0,...pnlVals);
    const yRange=yHi-yLo; if(yRange<2){ const mid=(yHi+yLo)/2; yLo=mid-1; yHi=mid+1; }
    const yPad=yRange*0.18; yLo-=yPad; yHi+=yPad;
    const xLo=0, xHi=1;
    const xMap=v=>pad.l+(v-xLo)/(xHi-xLo)*iW;
    const yMap=v=>pad.t+(yHi-v)/(yHi-yLo)*iH;
    const yZero=yMap(0), xMid=xMap(0.5);
    const fmt=v=>(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%";
    // Y-axis labels
    const yLabTop=`<text class="sc-axlabel" x="${pad.l-6}" y="${pad.t+4}" text-anchor="end">${fmt(yHi)}</text>`;
    const yLabBot=`<text class="sc-axlabel" x="${pad.l-6}" y="${pad.t+iH+3}" text-anchor="end">${fmt(yLo)}</text>`;
    const yLabZero=`<text class="sc-axlabel" x="${pad.l-6}" y="${(yZero+3).toFixed(1)}" text-anchor="end">0%</text>`;
    // X-axis labels
    const xLab0=`<text class="sc-axlabel" x="${pad.l}" y="${H-6}">Conv 0.0</text>`;
    const xLab1=`<text class="sc-axlabel" x="${W-pad.r}" y="${H-6}" text-anchor="end">1.0</text>`;
    const xLab5=`<text class="sc-axlabel" x="${xMid.toFixed(1)}" y="${H-6}" text-anchor="middle">0.5</text>`;
    // Axis lines
    const axisLeft=`<line class="sc-axis" x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${pad.t+iH}"/>`;
    const axisBot=`<line class="sc-axis" x1="${pad.l}" y1="${pad.t+iH}" x2="${pad.l+iW}" y2="${pad.t+iH}"/>`;
    // Quadrant dividers
    const hZero=`<line class="sc-zero" x1="${pad.l}" y1="${yZero.toFixed(1)}" x2="${(pad.l+iW).toFixed(1)}" y2="${yZero.toFixed(1)}"/>`;
    const vMid=`<line class="sc-zero" x1="${xMid.toFixed(1)}" y1="${pad.t}" x2="${xMid.toFixed(1)}" y2="${pad.t+iH}"/>`;
    // Quadrant labels (corners)
    const qLabelPad=5;
    const qTR=`<text class="sc-qlabel" x="${xMid+qLabelPad}" y="${pad.t+10}">High conv · Working</text>`;
    const qTL=`<text class="sc-qlabel" x="${pad.l+qLabelPad}" y="${pad.t+10}">Low conv · Working</text>`;
    const qBR=`<text class="sc-qlabel" x="${xMid+qLabelPad}" y="${pad.t+iH-4}">High conv · Dragging</text>`;
    const qBL=`<text class="sc-qlabel" x="${pad.l+qLabelPad}" y="${pad.t+iH-4}">Low conv · Dragging</text>`;
    // Dots + labels
    // Detect overlapping labels: nudge y if two dots within 12px horizontally
    const dotData=priced.map(r=>{
      const t=r.t, tks=(t.tickers||[]).join("+"), pnl=r.pnl, conv=t.conviction||0;
      const cx=xMap(conv), cy=yMap(pnl);
      const cls=pnl>0.05?"sc-dot-pos":pnl<-0.05?"sc-dot-neg":"sc-dot-flat";
      const tip=`${tks} · Conv ${conv.toFixed(2)} · ${pnl>=0?"+":"−"}${Math.abs(pnl).toFixed(2)}% · ${(t.direction||"").toUpperCase()}${t.label?" · "+t.label:""}`;
      return {tks,pnl,conv,cx,cy,cls,tip};
    });
    const dots=dotData.map(d=>`<circle class="sc-dot ${d.cls}" cx="${d.cx.toFixed(1)}" cy="${d.cy.toFixed(1)}" r="8"><title>${esc(d.tip)}</title></circle>`).join("");
    // Label placement: above dot if in lower half (pnl<0 or cy>height/2), else below
    const labels=dotData.map(d=>{
      const above=d.cy>(pad.t+iH/2);
      const ly=above?d.cy-11:d.cy+16;
      return `<text class="sc-label" x="${d.cx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="middle">${esc(d.tks)}</text>`;
    }).join("");
    // X-axis title
    const xAxisTitle=`<text class="sc-axlabel" x="${(pad.l+iW/2).toFixed(1)}" y="${H}" text-anchor="middle">Conviction</text>`;
    // Y-axis title (rotated)
    const yAxisTitle=`<text class="sc-axlabel" transform="rotate(-90)" x="${-(pad.t+iH/2).toFixed(0)}" y="11" text-anchor="middle">P&amp;L %</text>`;
    // Correlation note: are high-conv calls working more than low-conv?
    // Pearson r between conv and pnl
    const n=priced.length;
    const meanC=priced.reduce((s,r)=>s+(r.t.conviction||0),0)/n;
    const meanP=priced.reduce((s,r)=>s+r.pnl,0)/n;
    const cov=priced.reduce((s,r)=>s+((r.t.conviction||0)-meanC)*(r.pnl-meanP),0)/n;
    const sdC=Math.sqrt(priced.reduce((s,r)=>s+((r.t.conviction||0)-meanC)**2,0)/n);
    const sdP=Math.sqrt(priced.reduce((s,r)=>s+(r.pnl-meanP)**2,0)/n);
    const pearsonR=(sdC>0&&sdP>0)?cov/(sdC*sdP):0;
    const rStr=(pearsonR>=0?"+":"")+pearsonR.toFixed(2);
    const rInterpret=n<3?"(zu wenig Daten für Korrelation)":pearsonR>0.3?"(positive Edge: höhere Konv → bessere Performance)":pearsonR<-0.3?"(Warnung: höhere Konv → schlechtere Performance — Überzeugung nicht kalibriert)":"(kein signifikanter Zusammenhang bei aktuellem Buch)";
    const svg=`<svg class="pf-scatter-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet"
      role="img" aria-label="Conviction vs P&L Streudiagramm — ${n} aktive Calls">
      ${axisLeft}${axisBot}${hZero}${vMid}
      ${qTL}${qTR}${qBL}${qBR}
      ${yLabTop}${yLabBot}${yLabZero}${xLab0}${xLab1}${xLab5}
      ${yAxisTitle}${xAxisTitle}
      ${dots}${labels}
    </svg>`;
    const highConvWorking=priced.filter(r=>r.pnl>0&&(r.t.conviction||0)>=0.5).length;
    const highConvTotal=priced.filter(r=>(r.t.conviction||0)>=0.5).length;
    const edgeNote=highConvTotal>0?`${highConvWorking}/${highConvTotal} High-Conv-Calls im Plus · Pearson r ${rStr} ${rInterpret}`:`Pearson r ${rStr} ${rInterpret}`;
    scatterPanelHtml=`<div class="panel pf-scatter">
      <div class="pf-scatter-h">
        <span>Conviction vs. P&amp;L — Kalibrierungs-Scatter</span>
        <span class="pf-scatter-sub muted">${esc(edgeNote)}</span>
      </div>
      ${svg}
      <div class="sc-foot muted">Ideal: High-Conviction-Calls rechts oben. Systematischer Drift in "Dragging" = Conviction-Scoring neu kalibrieren.</div>
    </div>`;
  }
  // Korrelationsmatrix der aktiven Calls — Diversifikations-Diagnose (HED-137 Zyklus 86).
  // Pairwise Pearson r über die letzten 25 Tagesrenditen aus den sector_view-Sparks,
  // sign-flipped für Shorts (auf P&L-Ebene, nicht Underlying-Ebene). Off-diagonal-Mittel
  // und max-Paar beantworten die echte Diversifikations-Frage: bewegen sich meine
  // "verschiedenen" Positionen wirklich unabhängig, oder ist das Buch in Wahrheit
  // N Varianten derselben Makro-Wette? Sektor-Streuung ≠ echte Diversifikation.
  let corrPanelHtml="";
  {
    const _rets=[];
    active.forEach(t=>{
      const tk=(t.tickers||[])[0];
      if(!tk) return;
      const sp=_sparkMap[String(tk).toUpperCase()];
      if(!sp||sp.length<6) return;
      const N=Math.min(25, sp.length-1);
      const r=[];
      for(let i=sp.length-N;i<sp.length;i++){
        if(i<=0) continue;
        const p0=sp[i-1], p1=sp[i];
        if(p0>0) r.push((p1-p0)/p0);
      }
      if(r.length<5) return;
      const sign=(t.direction||"").toLowerCase()==="short"?-1:1;
      _rets.push({
        label:(t.tickers||[]).join("·")||tk,
        dir:(t.direction||"").toLowerCase(),
        r: r.map(v=>v*sign)
      });
    });
    if(_rets.length>=2){
      const _pearson=(a,b)=>{
        const n=Math.min(a.length,b.length); if(n<3) return null;
        const A=a.slice(-n), B=b.slice(-n);
        let mA=0,mB=0; for(let i=0;i<n;i++){mA+=A[i];mB+=B[i];} mA/=n; mB/=n;
        let cov=0,vA=0,vB=0;
        for(let i=0;i<n;i++){ const da=A[i]-mA, db=B[i]-mB; cov+=da*db; vA+=da*da; vB+=db*db; }
        if(vA===0||vB===0) return null;
        return cov/Math.sqrt(vA*vB);
      };
      const N=_rets.length;
      const M=Array.from({length:N},()=>Array(N).fill(null));
      for(let i=0;i<N;i++){
        M[i][i]=1;
        for(let j=i+1;j<N;j++){ const r=_pearson(_rets[i].r,_rets[j].r); M[i][j]=r; M[j][i]=r; }
      }
      let offSum=0, offN=0, maxR=-2, maxPair=null, minR=2, minPair=null;
      for(let i=0;i<N;i++) for(let j=i+1;j<N;j++){
        const r=M[i][j]; if(r==null) continue;
        offSum+=r; offN++;
        if(r>maxR){ maxR=r; maxPair=[i,j]; }
        if(r<minR){ minR=r; minPair=[i,j]; }
      }
      const avgR=offN>0?offSum/offN:null;
      const _corrColor=r=>{
        if(r==null) return "background:var(--panel2);color:var(--mut)";
        const t=Math.max(-1,Math.min(1,r));
        if(t>=0){
          const a=(t*0.85).toFixed(2);
          return `background:rgba(248,81,73,${a});color:${t>0.4?"#fff":"var(--txt)"}`;
        }
        const a=(Math.abs(t)*0.7).toFixed(2);
        return `background:rgba(88,166,255,${a});color:${Math.abs(t)>0.4?"#fff":"var(--txt)"}`;
      };
      const _dirCls=d=>d==="long"?"cd-long":d==="short"?"cd-short":"cd-pair";
      const _fmtR=v=>v==null?"—":(v>=0?"+":"")+v.toFixed(2);
      const headCells=_rets.map(s=>`<th class="pf-corr-col" scope="col" title="${esc(s.label)}">${esc(s.label)}</th>`).join("");
      const bodyRows=_rets.map((s,i)=>{
        const cells=_rets.map((_,j)=>{
          if(i===j) return `<td class="pf-corr-cell pf-corr-diag" title="${esc(s.label)} mit sich selbst">1.00</td>`;
          const r=M[i][j];
          if(r==null) return `<td class="pf-corr-cell pf-corr-na" title="zu wenig Überlappung">—</td>`;
          const cls=Math.abs(r)>=0.7?"pf-corr-cell pf-corr-strong":"pf-corr-cell";
          return `<td class="${cls}" style="${_corrColor(r)}" title="${esc(_rets[i].label)} × ${esc(_rets[j].label)}: r = ${_fmtR(r)}">${_fmtR(r)}</td>`;
        }).join("");
        const dirChip=`<span class="cd ${_dirCls(s.dir)}" aria-label="${esc(s.dir.toUpperCase())}">${esc((s.dir.slice(0,1)||"·").toUpperCase())}</span>`;
        return `<tr><th class="pf-corr-row" scope="row"><span class="pf-corr-lbl">${esc(s.label)}</span>${dirChip}</th>${cells}</tr>`;
      }).join("");
      let diag="";
      if(avgR!=null){
        const avgCls=avgR>=0.6?"move-dn":avgR<=0.2?"move-up":"";
        const verd=avgR>=0.7?"Buch stark konzentriert — viele Positionen bewegen sich zusammen, geringe echte Diversifikation. Drawdown-Risiko unterschätzt."
                  :avgR>=0.4?"Buch moderat korreliert — Risiken nicht unabhängig, Drawdown-Korridor enger als nominell."
                  :avgR>=0.1?"Buch gemischt — gesunder Diversifikations-Mix, Positionen bewegen sich nicht systematisch zusammen."
                  :"Buch breit diversifiziert oder teilweise hedgend — Positionen kompensieren sich.";
        const maxLbl=maxPair?`${_rets[maxPair[0]].label} × ${_rets[maxPair[1]].label}`:"—";
        const maxCls=maxR>=0.7?"move-dn":"";
        const minLbl=(minPair&&minR<-0.1)?`${_rets[minPair[0]].label} × ${_rets[minPair[1]].label}`:null;
        const minPart=minLbl?`<span><span class="muted">niedrigstes Paar</span> <b>${esc(minLbl)}</b> <span class="move-up">${_fmtR(minR)}</span></span>`:"";
        diag=`<div class="pf-corr-diag-row">
          <span><span class="muted">Ø off-diagonal</span> <b class="${avgCls}">${_fmtR(avgR)}</b></span>
          <span><span class="muted">höchstes Paar</span> <b>${esc(maxLbl)}</b> <span class="${maxCls}">${_fmtR(maxR)}</span></span>
          ${minPart}
        </div>
        <div class="pf-corr-verd">${esc(verd)}</div>`;
      }
      const win=Math.min(25,(_rets[0]&&_rets[0].r.length)||0);
      const skipped=active.length-_rets.length;
      const skipNote=skipped>0?` · ${skipped} Call${skipped===1?"":"s"} ohne ausreichende Spark-Daten ausgeblendet`:"";
      corrPanelHtml=`<div class="panel pf-corr">
        <div class="pf-corr-h">
          <span>Korrelationsmatrix — Diversifikations-Diagnose</span>
          <span class="pf-corr-sub muted">Pearson r über letzte ${win} Handelstage · sign-flipped für Shorts${skipNote}</span>
        </div>
        <div class="pf-corr-wrap">
          <table class="pf-corr-tbl" role="table" aria-label="Pairwise Korrelationsmatrix der aktiven Calls">
            <thead><tr><th scope="col" class="pf-corr-corner" aria-label=""></th>${headCells}</tr></thead>
            <tbody>${bodyRows}</tbody>
          </table>
        </div>
        ${diag}
        <div class="pf-corr-foot muted">
          <span class="pf-corr-legend">
            <span class="pf-corr-leg-chip"><span class="pf-corr-leg-sw" style="background:rgba(248,81,73,0.85)"></span>r ≈ +1 (gleiche Bewegung)</span>
            <span class="pf-corr-leg-chip"><span class="pf-corr-leg-sw" style="background:var(--panel2)"></span>r ≈ 0 (unabhängig)</span>
            <span class="pf-corr-leg-chip"><span class="pf-corr-leg-sw" style="background:rgba(88,166,255,0.7)"></span>r ≈ −1 (Hedge)</span>
          </span>
          · Diversifikation ≠ Sektor-Streuung: zwei "verschiedene" Calls in derselben Makro-Welle bewegen sich trotzdem zusammen.
        </div>
      </div>`;
    }
  }
  root.innerHTML=`<div class="pf-grid">${kpiHtml}</div>${curvePanelHtml}${allocHtml}<div class="grid two-col" style="gap:var(--s3)">${barHtml}${secBarHtml}</div>${pnlPanelHtml}${attribPanelHtml}${scatterPanelHtml}${corrPanelHtml}${riskHtml}`;
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
        const cons=t.consensus;
        const pt=cons&&cons.pt_mean&&t.price?(()=>{const up=((cons.pt_mean-t.price)/t.price*100);const sign=up>=0?"+":"";const cls=up>=10?"move-up":up<=-5?"move-dn":"muted";return `<span class="${cls} w52" title="Analyst PT: $${cons.pt_mean} · ${cons.analyst_count||'?'} analysts · rec=${cons.rec||'?'}${cons.fwd_eps?" · fwdEPS=$"+cons.fwd_eps:""}">${sign}${up.toFixed(0)}%▲</span>`})():"";
        return `<div class="sec-row">${tkHtml}<span class="px">${px}</span>${chCell(t.change_pct)}${w52}${rsi}${pt}${spark}</div>`;
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

// Katalysator-Runway (HED-137 Zyklus 85): 30-day event timeline combining earnings calendar
// and active-thesis horizon-resolution dates. Distinguishes positions im Buch (direkter P&L-Impact)
// from Watchlist (re-entry signal). Bloomberg ECO/ER equivalent, customised to our actual book.
(function renderCatalysts(){
  const root=$("catalysts");
  if(!root) return;
  const sv=D.sector_view||{}, tr=D.track_record||{};
  const earnings=(sv.earnings_calendar||[]).slice();
  const theses=(tr.theses||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date));
  // Sector map for tagging events with sector id
  const SECTOR_MAP={};
  ((sv.sectors)||[]).forEach(s=>{
    (s.tickers||[]).forEach(tk=>{ const sym=tk&&tk.ticker!=null?String(tk.ticker).toUpperCase():(typeof tk==="string"?tk.toUpperCase():null); if(sym) SECTOR_MAP[sym]=s.id; });
  });
  // Map held tickers → first matching thesis (for direction tagging on earnings events)
  const heldMap={};
  theses.forEach(t=>{
    (t.tickers||[]).forEach(tk=>{
      const k=String(tk).toUpperCase();
      if(!heldMap[k]) heldMap[k]={direction:(t.direction||"").toLowerCase(),conv:t.conviction||0,label:t.label||""};
    });
  });
  // As-of date drives days_out for horizon events
  const asOfStr=(sv.as_of_iso||sv.as_of||"").replace(/ UTC.*/,"").slice(0,10);
  const asOfDate=asOfStr?new Date(asOfStr+"T00:00:00Z"):new Date();
  function _daysOut(dStr){
    if(!dStr) return null;
    try{ const d=new Date(String(dStr)+"T00:00:00Z"); if(isNaN(d)) return null; return Math.round((d-asOfDate)/86400000); }catch(e){ return null; }
  }
  function _weekday(dStr){
    const wd=["So","Mo","Di","Mi","Do","Fr","Sa"];
    try{ const d=new Date(String(dStr)+"T00:00:00Z"); if(isNaN(d)) return ""; return wd[d.getUTCDay()]; }catch(e){ return ""; }
  }
  function _ddmm(dStr){
    const m=String(dStr||"").match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m?`${m[3]}.${m[2]}`:String(dStr||"");
  }
  // Build event list
  const events=[];
  earnings.forEach(e=>{
    const tk=String(e.ticker||"").toUpperCase(); if(!tk) return;
    const dOut=e.days_out!=null?e.days_out:_daysOut(e.date);
    if(dOut==null||dOut<0) return;
    const held=heldMap[tk];
    events.push({type:"earnings",ticker:tk,date:e.date,daysOut:dOut,
      held:!!held,direction:held?held.direction:null,
      sector:SECTOR_MAP[tk]||null,
      sort:dOut*10+1});
  });
  theses.forEach(t=>{
    const dOut=_daysOut(t.earliest_score_date);
    if(dOut==null||dOut<0) return;
    const tks=(t.tickers||[]).map(x=>String(x).toUpperCase());
    const tk=tks[0]||"?";
    events.push({type:"horizon",ticker:tk,tickers:tks,date:t.earliest_score_date,daysOut:dOut,
      held:true,direction:(t.direction||"").toLowerCase(),
      conv:t.conviction,label:t.label||"",
      sector:SECTOR_MAP[tk]||null,
      sort:dOut*10+2});
  });
  if(!events.length){
    root.innerHTML='<div class="panel cat-panel"><div class="cat-empty">Keine anstehenden Katalysatoren — keine offenen Calls oder Earnings im Datenfeed.</div></div>';
    root.setAttribute("aria-busy","false"); return;
  }
  events.sort((a,b)=>a.sort-b.sort);
  // KPIs (count distinct events, not tickers — back-to-back earnings still count separately)
  const inWindow=events.filter(e=>e.daysOut<=30);
  const beyond=events.length-inWindow.length;
  const week=events.filter(e=>e.daysOut<=7).length;
  const weekHeld=events.filter(e=>e.daysOut<=7&&e.held).length;
  const twoWeek=events.filter(e=>e.daysOut<=14).length;
  // SVG timeline (today=0 → +30d). Same-day events stack vertically by sort order.
  const WIN=30, W=720, H=140;
  const pad={l:24,r:60,t:14,b:30};
  const iW=W-pad.l-pad.r, iH=H-pad.t-pad.b;
  const xAt=d=>pad.l+(Math.min(d,WIN)/WIN)*iW;
  const baseY=pad.t+8;
  // Group by daysOut for stacking
  const byDay={};
  inWindow.forEach(e=>{ (byDay[e.daysOut]=byDay[e.daysOut]||[]).push(e); });
  // Gridlines at weeks
  const ticks=[0,7,14,21,28];
  const gridSvg=ticks.map(d=>`<line class="cat-grid" x1="${xAt(d).toFixed(1)}" y1="${pad.t}" x2="${xAt(d).toFixed(1)}" y2="${(H-pad.b).toFixed(1)}"/>`).join("");
  const xlabSvg=ticks.map(d=>`<text class="cat-xlab" x="${xAt(d).toFixed(1)}" y="${(H-12).toFixed(1)}" text-anchor="middle">${d===0?"Heute":"+"+d+"d"}</text>`).join("");
  // Today marker (vertical accent)
  const todayMark=`<line class="cat-axis" x1="${xAt(0).toFixed(1)}" y1="${pad.t}" x2="${xAt(0).toFixed(1)}" y2="${(H-pad.b).toFixed(1)}" stroke="var(--accent)" stroke-width="1.5"/>`;
  // Marks
  const markSvg=[];
  Object.keys(byDay).sort((a,b)=>Number(a)-Number(b)).forEach(dKey=>{
    const evs=byDay[dKey];
    const x=xAt(Number(dKey));
    evs.forEach((e,i)=>{
      const y=baseY+i*16;
      if(y>H-pad.b-4) return; // overflow safety
      const dirColor=e.direction==="long"?"var(--green)":e.direction==="short"?"var(--red)":"var(--mut)";
      const tipParts=[
        `${e.ticker} · ${e.type==="earnings"?"Earnings":"Thesis-Horizont"}`,
        `${_weekday(e.date)} ${e.date} (+${e.daysOut}d)`,
      ];
      if(e.held&&e.type==="earnings") tipParts.push(`im Buch (${(e.direction||"?").toUpperCase()})`);
      else if(!e.held) tipParts.push("Watchlist");
      if(e.type==="horizon"&&e.conv!=null) tipParts.push(`Conv ${e.conv.toFixed(2)}`);
      if(e.label) tipParts.push(e.label);
      const tip=tipParts.join(" · ");
      if(e.type==="earnings"){
        if(e.held){
          markSvg.push(`<circle class="cat-mark cat-mark-th" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5" fill="${dirColor}"><title>${esc(tip)}</title></circle>`);
        } else {
          markSvg.push(`<circle class="cat-mark cat-mark-er-watch" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4.5" stroke="var(--mut)"><title>${esc(tip)}</title></circle>`);
        }
      } else {
        // diamond for horizon (rotated square)
        markSvg.push(`<rect class="cat-mark cat-mark-th" x="${(x-5).toFixed(1)}" y="${(y-5).toFixed(1)}" width="10" height="10" transform="rotate(45 ${x.toFixed(1)} ${y.toFixed(1)})" fill="${dirColor}"><title>${esc(tip)}</title></rect>`);
      }
      const lblCls=e.held?"cat-label cat-label-held":"cat-label";
      // Right-anchor labels for events near right edge so they don't run off
      const rightEdge=(x>pad.l+iW-50);
      const lblX=rightEdge?(x-8).toFixed(1):(x+8).toFixed(1);
      const lblAnchor=rightEdge?` text-anchor="end"`:"";
      markSvg.push(`<text class="${lblCls}" x="${lblX}" y="${(y+3).toFixed(1)}"${lblAnchor}>${esc(e.ticker)}</text>`);
    });
  });
  const svg=`<svg class="cat-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Katalysator-Timeline für die nächsten 30 Tage — ${inWindow.length} Events">
    ${gridSvg}
    ${todayMark}
    ${markSvg.join("")}
    ${xlabSvg}
  </svg>`;
  // Detail list — first 8 events in window
  const sym=t=>t==="earnings"?"📊":"⏰";
  const tagFor=e=>{
    if(!e.held) return '<span class="d-tag cat-tag-watch">Watch</span>';
    const d=e.direction;
    const cls=d==="long"?"cat-tag-held-long":d==="short"?"cat-tag-held-short":"cat-tag-held-pair";
    const lbl=d==="long"?"Long":d==="short"?"Short":"Pair";
    return `<span class="d-tag ${cls}" title="Im Buch · ${esc(lbl)}">${lbl} · Buch</span>`;
  };
  const kindLbl=e=>{
    if(e.type==="earnings") return "Earnings"+(e.sector?` · ${e.sector}`:"");
    const dirTxt=e.direction?` · ${e.direction.toUpperCase()}`:"";
    const convTxt=e.conv!=null?` · Conv ${e.conv.toFixed(2)}`:"";
    return `Horizont${dirTxt}${convTxt}`;
  };
  const listRows=inWindow.slice(0,8).map(e=>`<div class="cat-list-row" title="${esc(e.label||e.ticker+' '+e.date)}">
    <span class="d-when">${esc(_weekday(e.date))} ${esc(_ddmm(e.date))}</span>
    <span class="d-out">+${e.daysOut}d</span>
    <span class="d-sym" aria-hidden="true">${sym(e.type)}</span>
    <span><b class="d-tk">${esc(e.ticker)}</b><span class="d-kind">${esc(kindLbl(e))}</span></span>
    ${tagFor(e)}
  </div>`).join("");
  const more=inWindow.length>8?`<div class="cat-foot">+${inWindow.length-8} weitere Event${inWindow.length-8===1?"":"s"} im 30-Tage-Fenster</div>`:"";
  const beyondFoot=beyond?`<div class="cat-foot">${beyond} Katalysator${beyond===1?"":"en"} jenseits 30d (Quartals-Horizonte, später skaliert)</div>`:"";
  root.innerHTML=`<div class="panel cat-panel">
    <div class="ec-h">
      <div class="ec-h-l">
        <div class="ec-title">Katalysator-Runway <span class="muted" style="font-weight:400">(30 Tage)</span></div>
        <div class="ec-h-sub muted">Earnings + Thesis-Horizont, kombiniert mit aktivem Buch</div>
      </div>
      <div class="ec-kpis">
        <div class="ec-kpi" title="Katalysatoren in den nächsten 7 Tagen"><span class="muted">≤ 7d</span><b>${week}</b></div>
        <div class="ec-kpi" title="Davon mit Position im Buch (direkter P&amp;L-Impact)"><span class="muted">im Buch</span><b class="${weekHeld>0?'move-up':''}">${weekHeld}</b></div>
        <div class="ec-kpi" title="Katalysatoren in den nächsten 14 Tagen"><span class="muted">≤ 14d</span><b>${twoWeek}</b></div>
      </div>
    </div>
    ${svg}
    <div class="cat-legend" aria-label="Legende">
      <span class="cat-leg-item"><span class="cat-leg-dot cat-leg-dot--held"></span>Earnings · im Buch (Farbe = Direction)</span>
      <span class="cat-leg-item"><span class="cat-leg-dot cat-leg-dot--watch"></span>Earnings · Watchlist</span>
      <span class="cat-leg-item"><span class="cat-leg-dia"></span>Thesis-Horizont (Score-Termin)</span>
    </div>
    <div class="cat-list">${listRows}</div>
    ${more}
    ${beyondFoot}
  </div>`;
  root.setAttribute("aria-busy","false");
})();

function esc(s){return (s||"").replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));}
// loading complete: clear skeleton busy-state so assistive tech announces rendered content
["briefing","trackrecord","portfolioview","catalysts","sectorview"].forEach(id=>{const el=$(id);if(el)el.setAttribute("aria-busy","false");});

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




























