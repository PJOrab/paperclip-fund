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
import re
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
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
        "insider_tape": collect_insider_tape(c),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "built_at_iso": datetime.now(timezone.utc).isoformat(),
    }


# Insider-Tape (HED-137 Zyklus 103): aggregate the last 30 days of SEC Form-4
# OPEN-MARKET buys/sells per ticker into a single Bloomberg-INSI-style rollup.
# Form-4 P-codes (purchases) and S-codes (sales) are the only Form-4 transactions
# that carry an actual conviction signal — routine grants/exercises/tax-withholds
# are excluded. The dashboard renders this as a diverging $-bar per ticker, sorted
# by absolute net flow; tickers we have an active call on get a ★ overlay.
_F4_TEXT_RE = re.compile(
    r"\[EDGAR Insider Form 4\]\s+([A-Z][A-Z0-9.\-]{0,9})\s+"
    r"([^:]+?):\s+(.+?)\s+\(([^)]+)\)\s+—\s+"
    r"(OPEN-MARKET BUY|OPEN-MARKET SALE|MIXED open-market|routine[^—\n]*)"
    r"(?:\s+\$([\d.]+)([MK]))?",
    re.IGNORECASE,
)


def _parse_form4_row(row: dict) -> dict | None:
    """Parse one sec_form4 raw_items row into a structured txn dict; None if it's a routine/header-only filing."""
    text = (row.get("text") or "").strip()
    m = _F4_TEXT_RE.search(text)
    if not m:
        return None
    ticker, company, person, role, verdict_raw, amt, unit = m.groups()
    verdict = verdict_raw.upper().strip()
    if verdict.startswith("ROUTINE"):
        return None  # grants/exercises/tax — no open-market signal
    if amt is None:
        # MIXED rows without an amount — count exec, but skip dollar aggregation
        dollars = 0.0
    else:
        try:
            dollars = float(amt) * (1_000_000 if unit.upper() == "M" else 1_000)
        except ValueError:
            dollars = 0.0
    side = "buy" if "BUY" in verdict else ("sell" if "SALE" in verdict else "mixed")
    return {
        "ticker": ticker.upper(),
        "company": company.strip(),
        "person": person.strip(),
        "role": role.strip(),
        "side": side,
        "dollars": dollars,
        "fetched_at": row.get("fetched_at"),
        "url": row.get("url"),
    }


def collect_insider_tape(c, lookback_days: int = 30, ticker_cap: int = 24) -> dict:
    """Pull last `lookback_days` of sec_form4 raw_items and roll up per ticker.
    Returns:
      {
        "as_of": iso,
        "lookback_days": int,
        "total_dollar": float,          # gross open-market $ volume across all tickers
        "tickers": [                     # sorted by |net_dollar| desc, capped
          {"ticker": "PLTR", "company": "...", "buy_dollar": ..., "sell_dollar": ...,
           "net_dollar": ..., "n_buy_execs": int, "n_sell_execs": int,
           "n_buy_filings": int, "n_sell_filings": int,
           "last_date": "YYYY-MM-DD",
           "top_actors": [{"person": "...", "role": "...", "side": "buy|sell", "dollars": ...}, ...]},
        ]
      }
    Always returns the shape — empty `tickers` if the query fails or there are no qualifying rows."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = (c.table("raw_items")
                .select("text,url,fetched_at")
                .eq("source", "sec_form4")
                .gte("fetched_at", cutoff)
                .order("fetched_at", desc=True)
                .limit(2000)
                .execute().data or [])
    except Exception:
        rows = []

    by_ticker: dict[str, dict] = {}
    parsed_total = 0
    for r in rows:
        p = _parse_form4_row(r)
        if not p:
            continue
        parsed_total += 1
        tk = p["ticker"]
        entry = by_ticker.setdefault(tk, {
            "ticker": tk,
            "company": p["company"],
            "buy_dollar": 0.0,
            "sell_dollar": 0.0,
            "buy_execs": set(),
            "sell_execs": set(),
            "n_buy_filings": 0,
            "n_sell_filings": 0,
            "last_date": "",
            "actors": [],  # raw transaction list, ranked & trimmed below
        })
        if p["side"] == "buy":
            entry["buy_dollar"] += p["dollars"]
            entry["buy_execs"].add(p["person"])
            entry["n_buy_filings"] += 1
        elif p["side"] == "sell":
            entry["sell_dollar"] += p["dollars"]
            entry["sell_execs"].add(p["person"])
            entry["n_sell_filings"] += 1
        else:  # mixed
            entry["buy_execs"].add(p["person"])
            entry["sell_execs"].add(p["person"])
        entry["actors"].append({
            "person": p["person"], "role": p["role"], "side": p["side"], "dollars": p["dollars"],
        })
        d = (p.get("fetched_at") or "")[:10]
        if d > entry["last_date"]:
            entry["last_date"] = d

    out_list = []
    for tk, e in by_ticker.items():
        net = e["buy_dollar"] - e["sell_dollar"]
        # Drop tickers with no signal at all (no $ moved and ≤1 exec on each side)
        if e["buy_dollar"] + e["sell_dollar"] < 50_000 and len(e["buy_execs"]) + len(e["sell_execs"]) < 2:
            continue
        actors_sorted = sorted(e["actors"], key=lambda a: -a["dollars"])[:3]
        out_list.append({
            "ticker": tk,
            "company": e["company"],
            "buy_dollar": round(e["buy_dollar"], 0),
            "sell_dollar": round(e["sell_dollar"], 0),
            "net_dollar": round(net, 0),
            "n_buy_execs": len(e["buy_execs"]),
            "n_sell_execs": len(e["sell_execs"]),
            "n_buy_filings": e["n_buy_filings"],
            "n_sell_filings": e["n_sell_filings"],
            "last_date": e["last_date"],
            "top_actors": actors_sorted,
        })
    out_list.sort(key=lambda r: -abs(r["net_dollar"]))
    out_list = out_list[:ticker_cap]
    total_dollar = sum(r["buy_dollar"] + r["sell_dollar"] for r in out_list)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lookback_days": lookback_days,
        "rows_parsed": parsed_total,
        "total_dollar": total_dollar,
        "tickers": out_list,
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


def _earnings_history(ticker: str, max_quarters: int = 4) -> dict | None:
    """Per-ticker earnings beat-profile (HED-137 Zyklus 104): last `max_quarters`
    reported earnings — EPS surprise % and 1-day post-earnings close-to-close
    reaction. Off-build-path (Netz!); aggregated into sector_view.json.

    Why this is investment-grade: Bloomberg's ERN screen answers "does this ticker
    historically beat/miss, and does the surprise translate to the stock?" Beat-rate
    + sign-match (surprise direction == reaction direction) flags names that are
    *already priced in* vs names where the catalyst still moves the tape."""
    try:
        import yfinance as yf
        import pandas as pd
        tk = yf.Ticker(ticker)
        ed = tk.earnings_dates
        if ed is None or ed.empty:
            return None
        reported = ed.dropna(subset=["Reported EPS"]).head(max_quarters)
        if reported.empty:
            return None
        earliest = reported.index.min().to_pydatetime()
        start = (earliest - pd.Timedelta(days=10)).date().isoformat()
        end_dt = datetime.now(timezone.utc).date().isoformat()
        try:
            prices = tk.history(start=start, end=end_dt, auto_adjust=False)
        except Exception:
            prices = None
        quarters = []
        for idx, row in reported.iterrows():
            ed_date = idx.date()
            surprise = (float(row["Surprise(%)"])
                        if pd.notna(row.get("Surprise(%)")) else None)
            reaction = None
            if prices is not None and not prices.empty:
                try:
                    px_idx = prices.index.date
                    before = prices[px_idx < ed_date]
                    after = prices[px_idx >= ed_date]
                    if not before.empty and not after.empty:
                        pre_close = float(before["Close"].iloc[-1])
                        post_close = float(after["Close"].iloc[0])
                        if pre_close > 0:
                            reaction = round((post_close - pre_close) / pre_close * 100, 2)
                except Exception:
                    reaction = None
            quarters.append({
                "date": ed_date.isoformat(),
                "surprise_pct": round(surprise, 2) if surprise is not None else None,
                "reaction_1d_pct": reaction,
                "eps_est": (round(float(row["EPS Estimate"]), 3)
                            if pd.notna(row.get("EPS Estimate")) else None),
                "eps_actual": (round(float(row["Reported EPS"]), 3)
                               if pd.notna(row.get("Reported EPS")) else None),
            })
        surprises = [q["surprise_pct"] for q in quarters if q["surprise_pct"] is not None]
        reactions = [q["reaction_1d_pct"] for q in quarters if q["reaction_1d_pct"] is not None]
        beats = sum(1 for s in surprises if s > 0)
        avg_surprise = round(sum(surprises) / len(surprises), 2) if surprises else None
        avg_reaction = round(sum(reactions) / len(reactions), 2) if reactions else None
        # Earnings volatility: stdev of abs 1d reaction — signals whether this name
        # typically *moves* on earnings or trades through quietly.
        if len(reactions) >= 2:
            mean_r = sum(reactions) / len(reactions)
            var_r = sum((r - mean_r) ** 2 for r in reactions) / len(reactions)
            std_reaction = round(var_r ** 0.5, 2)
        else:
            std_reaction = None
        sign_hits = sum(1 for q in quarters
                        if q["surprise_pct"] is not None and q["reaction_1d_pct"] is not None
                        and ((q["surprise_pct"] > 0 and q["reaction_1d_pct"] > 0)
                             or (q["surprise_pct"] < 0 and q["reaction_1d_pct"] < 0)))
        return {
            "quarters": quarters,
            "beat_n": beats,
            "beat_total": len(surprises) if surprises else 0,
            "beat_pct": round(beats / len(surprises) * 100) if surprises else None,
            "avg_surprise_pct": avg_surprise,
            "avg_reaction_1d_pct": avg_reaction,
            "std_reaction_1d_pct": std_reaction,
            "sign_hits": sign_hits,
            "sign_total": len(reactions) if reactions else 0,
        }
    except Exception:
        return None


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
                eh = _earnings_history(t)
                if eh:
                    q["earnings_history"] = eh
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
/* Risk-adjusted metrics panel — Sharpe, Vol, Beta, Korr, Tracking Error, Info-Ratio (HED-137 cycle 92) */
.rs-panel{padding:var(--s3)}
.rs-h{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s3);margin-bottom:var(--s2);flex-wrap:wrap}
.rs-title{font-size:var(--fs-cap);color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.rs-notice{font-size:var(--fs-micro);color:var(--mut);font-variant-numeric:tabular-nums;cursor:help;
  padding:1px 6px;border:1px solid var(--line);border-radius:3px;letter-spacing:.02em}
.rs-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:4px;overflow:hidden}
/* 8-cell grid (6 original + Sortino + Calmar) — wrap at 4 on medium, 2 on mobile */
.rs-grid{grid-template-columns:repeat(8,minmax(0,1fr))}
@media(max-width:960px){.rs-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
/* Rolling Sharpe mini-chart — companion to underwater chart, same width */
.rs-chart-svg{width:100%;height:auto;display:block;margin-top:4px}
.rs-line{fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.rs-line.rs-pos{stroke:var(--green)}.rs-line.rs-neg{stroke:var(--red)}
.rs-dot-pos{fill:var(--green)}.rs-dot-neg{fill:var(--red)}
.rs-cell{background:var(--bg);padding:var(--s2) var(--s3);display:flex;flex-direction:column;gap:2px;
  cursor:help;font-variant-numeric:tabular-nums}
.rs-lbl{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.05em;line-height:1.1}
.rs-val{font-size:20px;font-weight:700;letter-spacing:-.01em;line-height:1.1}
.rs-val.move-up{color:var(--green)}
.rs-val.move-dn{color:var(--red)}
.rs-sub{font-size:var(--fs-micro);line-height:1.2}
.rs-foot{font-size:var(--fs-micro);margin-top:var(--s2);line-height:1.4}
@media(max-width:840px){.rs-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:480px){
  .rs-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .rs-val{font-size:17px}
}
/* Open Calls Live-Monitor — sortable positions table (HED-137 Zyklus 96) */
.lm-panel{margin-top:var(--s3);padding:var(--s3)}
.lm-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:var(--s3);margin-bottom:var(--s3)}
.lm-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.lm-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.lm-hint{font-size:var(--fs-micro);color:var(--mut);display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border:1px solid var(--line);border-radius:4px;cursor:default;letter-spacing:.02em;white-space:nowrap}
.lm-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:6px}
.lm-tbl{width:100%;border-collapse:separate;border-spacing:0;font-variant-numeric:tabular-nums;font-size:var(--fs-cap);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.lm-tbl thead th{background:var(--panel2);color:var(--mut);font-size:var(--fs-micro);font-weight:600;text-transform:uppercase;letter-spacing:.05em;padding:6px 10px;white-space:nowrap;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;position:sticky;top:0}
.lm-tbl thead th:hover{color:var(--txt)}
.lm-tbl thead th.lm-sorted{color:var(--accent)}
.lm-sort-ind{display:inline-block;margin-left:3px;opacity:.55;font-size:9px}
.lm-tbl tbody tr{background:var(--bg)}
.lm-tbl tbody tr:nth-child(even){background:rgba(138,160,189,.04)}
.lm-tbl tbody tr:hover{background:var(--panel2)}
.lm-tbl td{padding:7px 10px;border-bottom:1px solid rgba(138,160,189,.1);vertical-align:middle}
.lm-tbl tbody tr:last-child td{border-bottom:0}
.lm-tk{font-weight:700;color:var(--txt);font-size:13px;letter-spacing:-.01em}
.lm-lbl{display:block;font-weight:400;color:var(--mut);font-size:var(--fs-micro);margin-top:1px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lm-dir{display:inline-block;padding:1px 5px;border-radius:3px;font-size:var(--fs-micro);font-weight:700;letter-spacing:.04em;text-transform:uppercase;white-space:nowrap}
.lm-dir-long{background:rgba(63,185,80,.18);color:#3fb950;border:1px solid rgba(63,185,80,.3)}
.lm-dir-short{background:rgba(248,81,73,.18);color:#f85149;border:1px solid rgba(248,81,73,.3)}
.lm-date{color:var(--txt);white-space:nowrap}
.lm-days{color:var(--mut);font-size:var(--fs-micro);white-space:nowrap}
.lm-price{color:var(--txt);white-space:nowrap}
.lm-price-base{color:var(--mut)}
.lm-arrow{color:var(--mut);font-size:10px;margin:0 3px}
.lm-pnl{font-weight:700;white-space:nowrap}
.lm-spy{white-space:nowrap}
.lm-alpha{font-weight:600;white-space:nowrap}
.lm-conv-wrap{display:flex;align-items:center;gap:5px}
.lm-conv-bar{flex:0 0 44px;height:5px;background:var(--panel2);border-radius:3px;overflow:hidden}
.lm-conv-fill{height:100%;border-radius:3px;background:var(--accent)}
.lm-exit{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mut);font-size:var(--fs-micro);cursor:help}
.lm-na{color:var(--mut)}
.lm-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s2);line-height:1.5}
/* Mobile: collapse to cards */
@media(max-width:760px){
  .lm-wrap{overflow-x:visible}
  .lm-tbl,.lm-tbl thead,.lm-tbl tbody,.lm-tbl th,.lm-tbl td,.lm-tbl tr{display:block}
  .lm-tbl{border:none;background:transparent}
  .lm-tbl thead{display:none}
  .lm-tbl tbody tr{border:1px solid var(--line);border-radius:6px;margin-bottom:var(--s2);padding:var(--s2) var(--s3);display:grid;grid-template-columns:1fr auto;row-gap:4px;background:var(--bg)}
  .lm-tbl td{border:none;padding:0}
  .lm-tbl td[data-col="ticker"]{grid-column:1/2;grid-row:1}
  .lm-tbl td[data-col="dir"]{grid-column:2/3;grid-row:1;text-align:right}
  .lm-tbl td[data-col="pnl"]{grid-column:1/3;grid-row:2;font-size:20px}
  .lm-tbl td[data-col="alpha"]{grid-column:1/2;grid-row:3}
  .lm-tbl td[data-col="spy"]{grid-column:2/3;grid-row:3;text-align:right}
  .lm-tbl td[data-col="date"]{grid-column:1/2;grid-row:4}
  .lm-tbl td[data-col="conv"]{grid-column:2/3;grid-row:4;text-align:right}
  .lm-tbl td[data-col="exit"],.lm-tbl td[data-col="baseline"]{display:none}
  .lm-pnl{font-size:inherit}
  .lm-hint{display:none}
}
/* Stress-Test-Panel — szenario shocks (HED-137 Zyklus 95): bookwise Impact-Schätzung pro Standard-Shock */
.st-panel{margin-top:var(--s3);padding:var(--s3)}
.st-h{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s3);margin-bottom:var(--s3);flex-wrap:wrap}
.st-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.st-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.st-method{font-size:var(--fs-micro);color:var(--mut);font-variant-numeric:tabular-nums;
  padding:2px 8px;border:1px solid var(--line);border-radius:4px;cursor:help;letter-spacing:.02em;white-space:nowrap}
.st-method b{color:var(--txt);font-weight:700}
.st-grid{display:grid;grid-template-columns:1fr;gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.st-row{display:grid;grid-template-columns:minmax(150px,1.4fr) minmax(110px,1fr) minmax(140px,2fr) 80px;
  align-items:center;gap:var(--s3);padding:var(--s2) var(--s3);background:var(--bg);font-variant-numeric:tabular-nums}
.st-row-cap{font-size:var(--fs-micro);color:var(--mut);text-transform:uppercase;letter-spacing:.04em;font-weight:600;
  background:var(--panel2);padding:6px var(--s3)}
.st-row-cap div:nth-child(4){text-align:right}
.st-label{font-size:var(--fs-cap);font-weight:700;color:var(--txt);line-height:1.25}
.st-label-sub{display:block;font-weight:400;color:var(--mut);font-size:var(--fs-micro);margin-top:1px;text-transform:none;letter-spacing:0}
.st-assume{font-size:var(--fs-cap);color:var(--txt);font-weight:500}
.st-assume-tag{display:inline-block;padding:1px 6px;border:1px solid var(--line);border-radius:3px;font-size:var(--fs-micro);background:var(--panel2);white-space:nowrap}
.st-bar-track{position:relative;height:18px;background:var(--panel2);border-radius:3px;overflow:hidden}
.st-bar-mid{position:absolute;top:0;bottom:0;left:50%;width:1px;background:var(--line)}
.st-bar-fill{position:absolute;top:1px;bottom:1px;border-radius:2px}
.st-bar-pos{background:linear-gradient(90deg,rgba(63,185,80,.35),#3fb950);left:50%}
.st-bar-neg{background:linear-gradient(90deg,#f85149,rgba(248,81,73,.35));right:50%}
.st-val{text-align:right;font-weight:700;font-size:15px;letter-spacing:-.01em}
.st-val.move-up{color:var(--green)}
.st-val.move-dn{color:var(--red)}
.st-val.muted{color:var(--mut);font-weight:500}
.st-axis{display:grid;grid-template-columns:minmax(150px,1.4fr) minmax(110px,1fr) minmax(140px,2fr) 80px;
  gap:var(--s3);padding:0 var(--s3);font-size:var(--fs-micro);color:var(--mut);font-variant-numeric:tabular-nums;margin-top:4px}
.st-axis-bar{display:flex;justify-content:space-between}
.st-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s2);line-height:1.5}
.st-foot b{color:var(--txt);font-weight:700}
.st-worst{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border-radius:4px;
  background:rgba(248,81,73,.12);border:1px solid rgba(248,81,73,.3);color:var(--red);
  font-weight:700;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
@media(max-width:760px){
  .st-row{grid-template-columns:1fr 90px;grid-template-rows:auto auto;row-gap:6px;padding:var(--s3)}
  .st-row-cap{display:none}
  .st-axis{display:none}
  .st-label{grid-column:1/2;grid-row:1}
  .st-val{grid-column:2/3;grid-row:1;font-size:17px}
  .st-assume{grid-column:1/3;grid-row:2;font-size:var(--fs-micro);color:var(--mut)}
  .st-bar-track{grid-column:1/3;grid-row:3;height:14px}
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
/* Risk-Decomposition — Component CTR pro Call (HED-137 Zyklus 100, Bloomberg PORT-R / Aladdin-Stil) */
.pf-rd{padding:var(--s3)}
.pf-rd-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:var(--s2);margin-bottom:var(--s3)}
.pf-rd-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt)}
.pf-rd-meta{font-size:var(--fs-micro);font-variant-numeric:tabular-nums}
.pf-rd-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.pf-rd-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.pf-rd-tbl thead th{font-size:var(--fs-micro);font-weight:600;text-transform:uppercase;letter-spacing:.05em;
  color:var(--mut);padding:4px 6px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
.pf-rd-tbl thead th.pf-rd-th-name,.pf-rd-tbl thead th.pf-rd-th-bar{text-align:left}
.pf-rd-tbl thead th.pf-rd-th-bar{width:42%}
.pf-rd-tbl tbody td{padding:7px 6px;border-top:1px solid var(--line);text-align:right;vertical-align:middle}
.pf-rd-tbl tbody tr:first-child td,.pf-rd-tbl tbody tr:first-child th{border-top:0}
.pf-rd-tbl tbody tr:hover{background:rgba(77,163,255,.06)}
.pf-rd-lbl{padding:7px 6px 7px 0;text-align:left;font-weight:700;color:var(--txt);white-space:nowrap;
  display:flex;align-items:center;gap:6px;border-top:1px solid var(--line)}
.pf-rd-name{overflow:hidden;text-overflow:ellipsis;max-width:160px}
.pf-rd-bar-cell{padding-left:0!important;padding-right:var(--s2)!important}
.pf-rd-bar-stack{display:flex;flex-direction:column;gap:3px;min-width:120px}
.pf-rd-bar{height:9px;border-radius:2px;min-width:1px}
.pf-rd-bar-w{background:linear-gradient(90deg,#3a4a66,#5a7196)}
.pf-rd-bar-r{background:linear-gradient(90deg,#a14b48,#f85149)}
.pf-rd-num{font-weight:600;min-width:54px}
.pf-rd-d-num{font-weight:700}
.pf-rd-verd{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s2);
  margin-top:var(--s3);padding:var(--s2) var(--s3);background:var(--panel2);border-radius:6px;
  font-size:var(--fs-cap);font-variant-numeric:tabular-nums}
.pf-rd-verd b{font-weight:700}
.pf-rd-chip{display:inline-block;padding:2px 8px;border-radius:99px;font-size:var(--fs-micro);
  font-weight:700;text-transform:uppercase;letter-spacing:.04em;
  border:1px solid var(--line);background:var(--panel)}
.pf-rd-chip.move-dn{color:var(--red);border-color:rgba(248,81,73,.4)}
.pf-rd-chip.move-up{color:var(--green);border-color:rgba(63,185,80,.4)}
.pf-rd-legend{display:flex;flex-wrap:wrap;gap:var(--s3);align-items:center;font-size:var(--fs-micro);margin-top:var(--s2)}
.pf-rd-leg{display:inline-flex;align-items:center;gap:5px}
.pf-rd-leg-sw{display:inline-block;width:18px;height:8px;border-radius:2px}
.pf-rd-foot{font-size:var(--fs-micro);margin-top:var(--s2);line-height:1.45}
@media(max-width:560px){
  .pf-rd-tbl thead th,.pf-rd-tbl tbody td{padding:5px 4px;font-size:var(--fs-micro)}
  .pf-rd-tbl thead th.pf-rd-th-bar{width:38%}
  .pf-rd-lbl{padding:5px 4px 5px 0}
  .pf-rd-name{max-width:96px}
  .pf-rd-bar-stack{min-width:64px}
  .pf-rd-num{min-width:42px}
  .pf-rd-d-num{display:none}
  .pf-rd-tbl thead th:last-child{display:none}
  .pf-rd-verd{font-size:var(--fs-micro)}
}
/* Tech-Setup — Chart-Konfirmation pro offenem Call (HED-137 Zyklus 101).
   Bridges fundamental thesis to technical setup: trend (vs MA30), momentum
   (RSI14), and cycle position (52w range). Answers the PM's first morning
   question per call: "is the chart still on my side?" */
.pf-tech{padding:var(--s3);margin-top:var(--s3)}
.pf-tech-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:var(--s2);margin-bottom:var(--s3)}
.pf-tech-title{font-weight:700;font-size:var(--fs-h2);color:var(--txt)}
.pf-tech-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.pf-tech-chips{display:flex;gap:var(--s2);flex-wrap:wrap;font-variant-numeric:tabular-nums}
.pf-tech-chip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:99px;
  font-size:var(--fs-micro);font-weight:700;border:1px solid var(--line);background:var(--panel2)}
.pf-tech-chip-confirm{color:var(--green);border-color:rgba(63,185,80,.4)}
.pf-tech-chip-mixed{color:#e8b341;border-color:rgba(232,179,65,.4)}
.pf-tech-chip-stretch{color:#f0883e;border-color:rgba(240,136,62,.42)}
.pf-tech-chip-conflict{color:var(--red);border-color:rgba(248,81,73,.4)}
.pf-tech-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.pf-tech-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.pf-tech-tbl thead th{font-size:var(--fs-micro);font-weight:600;text-transform:uppercase;letter-spacing:.05em;
  color:var(--mut);padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
.pf-tech-tbl thead th.num{text-align:right}
.pf-tech-tbl thead th.center{text-align:center}
.pf-tech-tbl tbody td{padding:9px 8px;border-top:1px solid var(--line);vertical-align:middle}
.pf-tech-tbl tbody tr:hover{background:rgba(77,163,255,.06)}
.pf-tech-name{display:flex;flex-direction:column;gap:1px;line-height:1.15;min-width:120px}
.pf-tech-tk{font-weight:700;color:var(--txt);font-size:var(--fs-cap)}
.pf-tech-lbl{font-size:var(--fs-micro);color:var(--mut);overflow:hidden;text-overflow:ellipsis;max-width:200px;white-space:nowrap}
.pf-tech-dir{display:inline-block;padding:2px 7px;border-radius:4px;font-size:var(--fs-micro);font-weight:700;letter-spacing:.04em}
.pf-tech-dir-long{background:rgba(63,185,80,.18);color:var(--green);border:1px solid rgba(63,185,80,.32)}
.pf-tech-dir-short{background:rgba(248,81,73,.18);color:var(--red);border:1px solid rgba(248,81,73,.32)}
.pf-tech-ma{text-align:right;font-weight:600}
.pf-tech-ma .arrow{display:inline-block;margin-right:3px;font-size:11px}
.pf-tech-rsi{display:flex;flex-direction:column;align-items:flex-end;gap:3px;min-width:80px}
.pf-tech-rsi-val{font-weight:700;font-size:var(--fs-cap)}
.pf-tech-rsi-bar{width:78px;height:5px;background:linear-gradient(90deg,
  rgba(63,185,80,.45) 0%,rgba(63,185,80,.45) 30%,
  var(--panel2) 30%,var(--panel2) 70%,
  rgba(248,81,73,.45) 70%,rgba(248,81,73,.45) 100%);
  border-radius:99px;position:relative;border:1px solid var(--line)}
.pf-tech-rsi-mark{position:absolute;top:-3px;width:2px;height:11px;background:var(--txt);border-radius:1px}
.pf-tech-rsi-zone{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.pf-tech-range{min-width:110px}
.pf-tech-range-bar{position:relative;height:8px;background:linear-gradient(90deg,
  rgba(248,81,73,.22) 0%,var(--panel2) 50%,rgba(63,185,80,.22) 100%);border-radius:99px;border:1px solid var(--line)}
.pf-tech-range-mark{position:absolute;top:-3px;width:3px;height:14px;background:var(--txt);border-radius:1px;transform:translateX(-50%)}
.pf-tech-range-lbl{display:flex;justify-content:space-between;font-size:10px;color:var(--mut);margin-top:3px}
.pf-tech-range-pct{font-size:var(--fs-micro);color:var(--mut);text-align:center;margin-top:2px;font-variant-numeric:tabular-nums}
.pf-tech-verd{text-align:center;min-width:96px}
.pf-tech-verd-pill{display:inline-block;padding:3px 9px;border-radius:99px;font-size:var(--fs-micro);
  font-weight:700;text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);background:var(--panel)}
.pf-tech-verd-confirm{color:var(--green);border-color:rgba(63,185,80,.4)}
.pf-tech-verd-mixed{color:#e8b341;border-color:rgba(232,179,65,.4)}
.pf-tech-verd-stretch{color:#f0883e;border-color:rgba(240,136,62,.42)}
.pf-tech-verd-conflict{color:var(--red);border-color:rgba(248,81,73,.4)}
.pf-tech-verd-note{display:block;margin-top:3px;font-size:10px;color:var(--mut);line-height:1.25}
.pf-tech-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.pf-tech-na{color:var(--mut);font-style:italic;font-size:var(--fs-micro)}
@media(max-width:640px){
  .pf-tech-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .pf-tech-wrap{overflow:visible}
  .pf-tech-tbl{display:block}
  .pf-tech-tbl thead{display:none}
  .pf-tech-tbl tbody{display:block}
  .pf-tech-tbl tbody tr{
    display:grid;
    grid-template-columns:1fr auto;
    gap:6px 10px;
    padding:10px 2px;
    border-top:1px solid var(--line);
    align-items:center;
  }
  .pf-tech-tbl tbody tr:first-child{border-top:0}
  .pf-tech-tbl tbody tr:hover{background:transparent}
  .pf-tech-tbl tbody td{
    display:flex;align-items:center;gap:6px;
    padding:0;border:0;font-size:var(--fs-micro);
  }
  .pf-tech-tbl tbody td.col-range{display:none}
  .pf-tech-tbl tbody td[data-label]::before{
    content:attr(data-label);color:var(--mut);font-size:10px;
    text-transform:uppercase;letter-spacing:.05em;font-weight:600;
    margin-right:2px;min-width:54px;
  }
  .pf-tech-tbl tbody td.col-name,.pf-tech-tbl tbody td.col-verd{grid-column:1 / -1}
  .pf-tech-tbl tbody td.col-name::before,.pf-tech-tbl tbody td.col-verd::before{display:none}
  .pf-tech-tbl tbody td.col-dir{justify-content:flex-end}
  .pf-tech-tbl tbody td.col-dir::before{display:none}
  .pf-tech-tbl tbody td.col-verd{justify-content:flex-start;margin-top:4px}
  .pf-tech-name{min-width:0;flex:1}
  .pf-tech-tk{font-size:14px}
  .pf-tech-lbl{max-width:none;font-size:11px}
  .pf-tech-rsi{flex-direction:row;align-items:center;gap:6px;min-width:0}
  .pf-tech-rsi-val{font-size:13px;order:1}
  .pf-tech-rsi-bar{width:60px;order:2}
  .pf-tech-rsi-zone{order:3;font-size:9px}
  .pf-tech-verd{text-align:left;min-width:0}
  .pf-tech-verd-note{display:inline;margin-top:0;margin-left:8px}
}
/* Universum-Ideen-Scanner — Bloomberg EQSCRN-Stil (HED-137 Zyklus 102).
   Screens all non-open-call universe tickers on analyst consensus, RSI,
   trend (MA30), and 52w-range position. Composite score 0-6 ranks ideas.
   Answers: "What should we consider next?" — turns monitoring into decision support. */
.us-panel{padding:var(--s3)}
.us-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:var(--s2);margin-bottom:var(--s3)}
.us-title{font-weight:700;font-size:var(--fs-h2);color:var(--txt)}
.us-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.us-meta{display:flex;gap:var(--s3);flex-wrap:wrap;font-size:var(--fs-micro);color:var(--mut);font-variant-numeric:tabular-nums;align-items:center}
.us-meta-kpi{display:flex;flex-direction:column;align-items:flex-end;gap:1px}
.us-meta-kpi .lbl{text-transform:uppercase;letter-spacing:.05em;font-weight:600;font-size:10px;color:var(--mut)}
.us-meta-kpi .val{font-size:18px;font-weight:700;line-height:1.1;color:var(--txt)}
.us-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.us-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.us-tbl thead th{font-size:var(--fs-micro);font-weight:600;text-transform:uppercase;letter-spacing:.05em;
  color:var(--mut);padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
.us-tbl thead th.r{text-align:right}
.us-tbl tbody td{padding:10px 8px;border-top:1px solid var(--line);vertical-align:middle}
.us-tbl tbody tr:hover{background:rgba(77,163,255,.06)}
.us-name{display:flex;flex-direction:column;gap:1px;min-width:120px}
.us-tk{font-weight:700;font-size:15px;color:var(--txt);line-height:1.1}
.us-sec{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.us-score{display:inline-flex;flex-direction:column;align-items:center;gap:3px;min-width:48px}
.us-score-num{font-size:22px;font-weight:800;line-height:1;letter-spacing:-.02em}
.us-score-pip{display:flex;gap:2px}
.us-score-dot{width:6px;height:6px;border-radius:50%;background:var(--panel2);border:1px solid var(--line);transition:background .1s}
.us-score-dot.on{background:var(--accent);border-color:var(--accent)}
.us-score-0,.us-score-1{color:var(--mut)}
.us-score-2{color:#e8b341}
.us-score-3,.us-score-4{color:var(--green)}
.us-score-5,.us-score-6{color:#3be88e}
.us-rec{display:inline-block;padding:2px 7px;border-radius:4px;font-size:var(--fs-micro);font-weight:700;letter-spacing:.03em;white-space:nowrap}
.us-rec-sb{background:rgba(63,185,80,.18);color:var(--green);border:1px solid rgba(63,185,80,.32)}
.us-rec-b {background:rgba(63,185,80,.10);color:var(--green);border:1px solid rgba(63,185,80,.18)}
.us-rec-h {background:rgba(138,160,189,.18);color:var(--mut);border:1px solid rgba(138,160,189,.28)}
.us-rec-s {background:rgba(248,81,73,.14);color:var(--red);border:1px solid rgba(248,81,73,.28)}
.us-rsi{display:flex;align-items:center;gap:6px;min-width:80px;font-size:var(--fs-cap);font-weight:700}
.us-rsi-bar{width:60px;height:5px;background:linear-gradient(90deg,rgba(63,185,80,.45) 0%,rgba(63,185,80,.45) 30%,var(--panel2) 30%,var(--panel2) 70%,rgba(248,81,73,.45) 70%,rgba(248,81,73,.45) 100%);border-radius:99px;position:relative;border:1px solid var(--line);flex-shrink:0}
.us-rsi-mark{position:absolute;top:-3px;width:2px;height:11px;background:var(--txt);border-radius:1px}
.us-ma{font-weight:600;white-space:nowrap}
.us-range-bar{position:relative;height:7px;background:linear-gradient(90deg,rgba(248,81,73,.18) 0%,var(--panel2) 50%,rgba(63,185,80,.18) 100%);border-radius:99px;border:1px solid var(--line);width:70px}
.us-range-mark{position:absolute;top:-3px;width:3px;height:13px;background:var(--txt);border-radius:1px;transform:translateX(-50%)}
.us-range-val{font-size:var(--fs-micro);color:var(--mut);text-align:center;margin-top:2px;white-space:nowrap}
.us-signals{display:flex;flex-wrap:wrap;gap:4px}
.us-sig{display:inline-flex;align-items:center;gap:3px;padding:2px 6px;border-radius:99px;font-size:10px;font-weight:700;border:1px solid var(--line);background:var(--panel2)}
.us-sig-pos{background:rgba(63,185,80,.12);color:var(--green);border-color:rgba(63,185,80,.28)}
.us-sig-neg{background:rgba(248,81,73,.12);color:var(--red);border-color:rgba(248,81,73,.28)}
.us-sig-neu{background:rgba(138,160,189,.10);color:var(--mut);border-color:rgba(138,160,189,.22)}
.us-empty{padding:var(--s4);text-align:center;color:var(--mut);font-size:var(--fs-cap);font-style:italic}
.us-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.us-oc-lbl{display:inline-block;padding:1px 5px;border-radius:3px;background:rgba(77,163,255,.18);
  color:var(--accent);border:1px solid rgba(77,163,255,.28);font-size:10px;font-weight:700;vertical-align:middle;margin-left:4px}
@media(max-width:640px){
  .us-h{flex-direction:column;gap:var(--s2)}
  .us-tbl thead th,.us-tbl tbody td{padding:7px 5px;font-size:var(--fs-micro)}
  .us-tbl thead th.col-signals,.us-tbl tbody td.col-signals{display:none}
  .us-tbl thead th.col-range,.us-tbl tbody td.col-range{display:none}
  .us-rsi-bar{width:44px}
  .us-tk{font-size:13px}
}
/* Konsens-Spread — Bloomberg-ANR-Stil (HED-137 Zyklus 105).
   Visualisiert für jedes Universum-Ticker die analyst PT-Range (low|mean|high)
   und wo der aktuelle Preis darauf sitzt. Hoher Spread = viel
   Analystenuneinigkeit = Raum für variant perception = Alpha-Potenzial.
   Beantwortet: "Wo lohnt es sich, eine differenzierte These zu bauen?" */
.cs-panel{padding:var(--s3)}
.cs-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:var(--s2);margin-bottom:var(--s3)}
.cs-title{font-weight:700;font-size:var(--fs-h2);color:var(--txt)}
.cs-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.cs-meta{display:flex;gap:var(--s3);flex-wrap:wrap;font-variant-numeric:tabular-nums;align-items:center}
.cs-meta-kpi{display:flex;flex-direction:column;align-items:flex-end;gap:1px}
.cs-meta-kpi .lbl{text-transform:uppercase;letter-spacing:.05em;font-weight:600;font-size:10px;color:var(--mut)}
.cs-meta-kpi .val{font-size:18px;font-weight:700;line-height:1.1;color:var(--txt)}
.cs-meta-kpi .val.hot{color:var(--accent)}
.cs-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.cs-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.cs-tbl thead th{font-size:var(--fs-micro);font-weight:600;text-transform:uppercase;letter-spacing:.05em;
  color:var(--mut);padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
.cs-tbl thead th.r{text-align:right}
.cs-tbl thead th.c{text-align:center}
.cs-tbl tbody td{padding:11px 8px;border-top:1px solid var(--line);vertical-align:middle}
.cs-tbl tbody tr:hover{background:rgba(77,163,255,.06)}
.cs-tbl tbody tr.own td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.cs-tk-cell{min-width:130px}
.cs-tk-row{display:flex;align-items:center;gap:6px;line-height:1.1}
.cs-tk{font-weight:700;font-size:15px;color:var(--txt)}
.cs-sec-tag{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin-top:2px}
.cs-own-lbl{display:inline-block;padding:1px 5px;border-radius:3px;background:rgba(77,163,255,.18);
  color:var(--accent);border:1px solid rgba(77,163,255,.28);font-size:10px;font-weight:700}
/* Range-Bar: low → high, with mean diamond + price marker.
   Stranger glance test: where does the price sit relative to consensus? */
.cs-rng-cell{min-width:220px;width:42%}
.cs-rng{position:relative;height:28px;margin:2px 0 4px;cursor:help}
.cs-rng-track{position:absolute;left:0;right:0;top:13px;height:4px;border-radius:2px;
  background:linear-gradient(90deg,rgba(248,81,73,.32) 0%,rgba(138,160,189,.28) 50%,rgba(63,185,80,.32) 100%);
  border:1px solid var(--line)}
.cs-rng-mean{position:absolute;top:8px;width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:7px solid var(--accent);transform:translateX(-50%)}
.cs-rng-mean-d{position:absolute;top:14px;width:6px;height:6px;background:var(--accent);transform:translateX(-50%) rotate(45deg);border:1px solid var(--bg)}
.cs-rng-price{position:absolute;top:7px;width:3px;height:17px;background:var(--txt);transform:translateX(-50%);border-radius:1px;box-shadow:0 0 0 1px var(--bg)}
.cs-rng-lbl{position:absolute;font-size:9px;color:var(--mut);font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;top:0}
.cs-rng-lbl.lo{left:0}
.cs-rng-lbl.hi{right:0;text-align:right}
.cs-up{font-weight:800;font-size:14px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.cs-up.pos{color:var(--green)}
.cs-up.neg{color:var(--red)}
.cs-up.neu{color:var(--mut)}
.cs-spread-cell{min-width:96px}
.cs-spread-bar{position:relative;height:5px;background:var(--panel2);border-radius:99px;border:1px solid var(--line);overflow:hidden;width:72px;margin-top:2px}
.cs-spread-fill{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--accent) 0%,#e8b341 60%,var(--red) 100%);border-radius:99px}
.cs-spread-val{font-weight:700;font-size:13px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.cs-spread-val.hi{color:#e8b341}
.cs-spread-val.vhi{color:var(--accent)}
.cs-n{color:var(--mut);font-variant-numeric:tabular-nums;font-size:13px}
.cs-rec{display:inline-block;padding:2px 7px;border-radius:4px;font-size:var(--fs-micro);font-weight:700;letter-spacing:.03em;white-space:nowrap}
.cs-rec-sb{background:rgba(63,185,80,.18);color:var(--green);border:1px solid rgba(63,185,80,.32)}
.cs-rec-b {background:rgba(63,185,80,.10);color:var(--green);border:1px solid rgba(63,185,80,.18)}
.cs-rec-h {background:rgba(138,160,189,.18);color:var(--mut);border:1px solid rgba(138,160,189,.28)}
.cs-rec-s {background:rgba(248,81,73,.14);color:var(--red);border:1px solid rgba(248,81,73,.28)}
.cs-flag{display:inline-block;padding:1px 6px;border-radius:99px;font-size:10px;font-weight:700;letter-spacing:.02em;white-space:nowrap;border:1px solid var(--line)}
.cs-flag-contested{background:rgba(77,163,255,.14);color:var(--accent);border-color:rgba(77,163,255,.32)}
.cs-flag-priced{background:rgba(138,160,189,.10);color:var(--mut);border-color:rgba(138,160,189,.22)}
.cs-flag-room{background:rgba(63,185,80,.12);color:var(--green);border-color:rgba(63,185,80,.28)}
.cs-flag-rich{background:rgba(248,81,73,.14);color:var(--red);border-color:rgba(248,81,73,.28)}
.cs-empty{padding:var(--s4);text-align:center;color:var(--mut);font-size:var(--fs-cap);font-style:italic}
.cs-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.55}
@media(max-width:640px){
  .cs-h{flex-direction:column;gap:var(--s2)}
  .cs-tbl thead th,.cs-tbl tbody td{padding:8px 5px;font-size:var(--fs-micro)}
  .cs-tbl thead th.col-hide-m,.cs-tbl tbody td.col-hide-m{display:none}
  .cs-tk{font-size:13px}
  .cs-rng-cell{min-width:160px}
  .cs-spread-bar{width:48px}
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
/* Insider-Tape (HED-137 Zyklus 103): Bloomberg-INSI-style per-ticker rollup of the last 30d
   of SEC Form-4 open-market buys/sells. Each row is a diverging $-bar around a center axis:
   sells extend left in red, buys right in green. Sorted by absolute |net $| desc so the
   highest-magnitude smart-money signal is the first thing a PM sees. Tickers we have an
   active call on get a ★ overlay so confirming/conflicting insider flow is preattentive. */
.it-panel{padding:var(--s3);margin-top:var(--s3)}
.it-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.it-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.it-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.it-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.it-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;min-width:54px}
.it-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.it-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.it-metric .val.neg{color:var(--red)}
.it-metric .val.pos{color:var(--green)}
.it-rows{display:flex;flex-direction:column;gap:6px}
.it-row{display:grid;grid-template-columns:96px 1fr 96px;gap:var(--s3);align-items:center;padding:7px 4px;border-top:1px solid var(--line);font-variant-numeric:tabular-nums;color:inherit;text-decoration:none;border-radius:3px}
.it-row:first-of-type{border-top:0}
.it-row:hover{background:rgba(77,163,255,.05);text-decoration:none}
.it-row:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.it-tk{display:flex;flex-direction:column;gap:1px;line-height:1.15;min-width:0}
.it-tk-row{display:flex;align-items:center;gap:5px}
.it-tk-sym{font-weight:700;color:var(--txt);font-size:var(--fs-cap);letter-spacing:.02em}
.it-tk-star{font-size:11px;color:var(--accent);line-height:1}
.it-tk-meta{font-size:10px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px}
.it-bar-wrap{position:relative;height:22px;background:var(--panel2);border-radius:4px;border:1px solid var(--line);overflow:hidden}
.it-bar-axis{position:absolute;top:0;bottom:0;left:50%;width:1px;background:var(--line);z-index:2}
.it-bar-sell{position:absolute;top:0;bottom:0;right:50%;background:linear-gradient(90deg,#d23a32 0%,rgba(248,81,73,.55) 100%);border-radius:3px 0 0 3px}
.it-bar-buy{position:absolute;top:0;bottom:0;left:50%;background:linear-gradient(90deg,rgba(63,185,80,.55) 0%,#2da347 100%);border-radius:0 3px 3px 0}
.it-bar-lbl{position:absolute;top:50%;transform:translateY(-50%);font-size:10px;font-weight:600;letter-spacing:.02em;color:#fff;padding:0 5px;white-space:nowrap;pointer-events:none;text-shadow:0 1px 2px rgba(0,0,0,.45)}
.it-bar-lbl.sell{right:51%}
.it-bar-lbl.buy{left:51%}
.it-bar-empty{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:10px;color:var(--mut);font-style:italic}
.it-net{text-align:right;font-weight:700;font-size:var(--fs-cap);line-height:1.15}
.it-net.neg{color:var(--red)}
.it-net.pos{color:var(--green)}
.it-net-sub{display:block;font-size:10px;font-weight:500;color:var(--mut);margin-top:1px;text-transform:uppercase;letter-spacing:.04em}
.it-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.it-foot a{color:var(--mut);border-bottom:1px dotted var(--line)}
.it-foot a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.it-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);background:transparent;border-radius:6px;border:1px dashed var(--line);color:var(--mut);font-size:var(--fs-micro);font-style:italic}
@media(max-width:640px){
  .it-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .it-metrics{justify-content:space-between;gap:var(--s3)}
  .it-metric{text-align:left;min-width:0;flex:1}
  .it-metric .val{font-size:16px}
  .it-row{grid-template-columns:64px 1fr 76px;gap:var(--s2);padding:5px 0}
  .it-tk-meta{display:none}
  .it-bar-wrap{height:20px}
  .it-bar-lbl{font-size:9px}
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
/* Earnings-Playbook (HED-137 Zyklus 104): Bloomberg ERN-screen equivalent.
   Per-ticker beat-profile — last 4 reported EPS surprises (sequence bar),
   average reaction, sign-match (does surprise direction translate to stock?).
   Answers: "How does this name historically behave at earnings?" — the
   exact question a PM asks the night before a print. Sorted by upcoming
   earnings proximity so the next print is the top row. */
.ep-panel{padding:var(--s3);margin-top:var(--s3)}
.ep-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.ep-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.ep-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.ep-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.ep-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;min-width:54px;cursor:help}
.ep-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.ep-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.ep-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.ep-tbl thead th{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600;text-align:right;padding:8px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.ep-tbl thead th.l{text-align:left}
.ep-tbl thead th.c{text-align:center}
.ep-tbl tbody td{padding:9px 6px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle}
.ep-tbl tbody td.l{text-align:left}
.ep-tbl tbody td.c{text-align:center}
.ep-tbl tbody tr:hover{background:rgba(77,163,255,.05)}
.ep-tbl tbody tr.is-held td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.ep-tk{display:flex;flex-direction:column;gap:1px;line-height:1.15;min-width:0}
.ep-tk-row{display:flex;align-items:center;gap:5px}
.ep-tk-sym{font-weight:700;color:var(--txt);font-size:var(--fs-cap);letter-spacing:.02em}
.ep-tk-star{font-size:11px;color:var(--accent);line-height:1}
.ep-tk-dir{font-size:9px;font-weight:700;letter-spacing:.06em;padding:1px 5px;border-radius:3px;text-transform:uppercase}
.ep-tk-dir.long{background:rgba(63,185,80,.18);color:var(--green)}
.ep-tk-dir.short{background:rgba(248,81,73,.18);color:var(--red)}
.ep-tk-meta{font-size:10px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}
/* Surprise-history sequence bars: 4 cells, oldest left, newest right.
   Color encodes sign (green=beat / red=miss), saturation encodes magnitude. */
.ep-seq{display:inline-flex;gap:2px;align-items:center}
.ep-cell{display:inline-flex;align-items:center;justify-content:center;width:30px;height:22px;font-size:10px;font-weight:700;border-radius:3px;color:#fff;letter-spacing:-.01em;cursor:help;font-variant-numeric:tabular-nums}
.ep-cell.empty{background:repeating-linear-gradient(45deg,var(--panel2),var(--panel2) 3px,rgba(138,160,189,.18) 3px,rgba(138,160,189,.18) 6px);color:transparent;cursor:default}
.ep-cell.beat-strong{background:#2da347}
.ep-cell.beat{background:rgba(63,185,80,.65);color:var(--txt)}
.ep-cell.beat-weak{background:rgba(63,185,80,.30);color:var(--txt)}
.ep-cell.miss-weak{background:rgba(248,81,73,.32);color:var(--txt)}
.ep-cell.miss{background:rgba(248,81,73,.65);color:var(--txt)}
.ep-cell.miss-strong{background:#d23a32}
.ep-beat{font-weight:700;font-size:var(--fs-cap);letter-spacing:-.01em}
.ep-beat .nbeat{color:var(--green)}
.ep-beat .ntot{color:var(--mut);font-weight:500}
.ep-pct{display:inline-flex;align-items:baseline;gap:3px;font-size:var(--fs-cap);font-weight:600}
.ep-pct .sign{font-weight:700}
.ep-pct.pos{color:var(--green)}
.ep-pct.neg{color:var(--red)}
.ep-pct.flat{color:var(--mut)}
.ep-react-cell{display:flex;flex-direction:column;align-items:flex-end;line-height:1.15;gap:1px}
.ep-react-cell .ep-std{font-size:9px;color:var(--mut);font-weight:500;letter-spacing:.04em;text-transform:uppercase}
.ep-next{display:flex;flex-direction:column;align-items:flex-end;line-height:1.15;gap:1px;font-variant-numeric:tabular-nums}
.ep-next .when{font-weight:700;color:var(--txt);font-size:var(--fs-cap)}
.ep-next .out{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.ep-next.imminent .when{color:var(--amber)}
.ep-next.imminent .out{color:var(--amber)}
.ep-next.soon .when{color:var(--accent)}
.ep-next .dash{color:var(--mut);font-weight:400}
.ep-sign-bar{display:inline-block;height:6px;width:48px;background:var(--panel2);border-radius:3px;overflow:hidden;border:1px solid var(--line);vertical-align:middle;margin-right:5px}
.ep-sign-bar-f{display:block;height:100%;background:linear-gradient(90deg,var(--mut),var(--accent))}
.ep-sign-txt{font-size:10px;color:var(--mut);font-weight:500;font-variant-numeric:tabular-nums}
.ep-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.ep-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);border:1px dashed var(--line);border-radius:6px;color:var(--mut);font-size:var(--fs-micro);font-style:italic}
@media(max-width:640px){
  .ep-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .ep-metrics{justify-content:space-between;gap:var(--s3)}
  .ep-metric{text-align:left;min-width:0;flex:1}
  .ep-metric .val{font-size:16px}
  .ep-tbl thead th.col-hide-m,.ep-tbl tbody td.col-hide-m{display:none}
  .ep-tbl thead th{padding:6px 3px;font-size:9px}
  .ep-tbl tbody td{padding:7px 3px}
  .ep-cell{width:26px;height:20px;font-size:9px}
  .ep-tk-meta{display:none}
  .ep-sign-bar{width:32px}
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
/* Szenario-Korridor (HED-137 Zyklus 94): Bear→Base→Bull price-target bracket per thesis.
   Probability-weighted E[R] makes "what does this trade pay if it works / costs if it doesn't" visible at a glance — the PM-grade complement to the Street R/R panel. */
.th-sc{margin:var(--s2) 0 var(--s3);background:var(--panel2);
  border:1px solid var(--line);border-radius:8px;padding:8px 10px 9px}
.th-sc-h{display:flex;justify-content:space-between;align-items:baseline;
  font-size:9px;font-weight:700;letter-spacing:.05em;color:var(--mut);
  text-transform:uppercase;margin-bottom:8px;gap:8px;flex-wrap:wrap}
.th-sc-h-r{font-size:11px;font-variant-numeric:tabular-nums;text-transform:none;letter-spacing:0;font-weight:700;color:var(--txt)}
.th-sc-er-up{color:var(--green)}
.th-sc-er-dn{color:#f78166}
.th-sc-er-flat{color:var(--mut)}
.th-sc-bar{position:relative;height:10px;background:rgba(255,255,255,.04);
  border-radius:3px;border:1px solid rgba(255,255,255,.05);margin:18px 0 4px}
.th-sc-span{position:absolute;top:0;height:100%;background:linear-gradient(90deg,rgba(248,81,73,.22) 0%,rgba(255,193,7,.18) 50%,rgba(63,185,80,.22) 100%);border-radius:2px}
.th-sc-mk{position:absolute;top:50%;width:10px;height:10px;border-radius:50%;
  transform:translate(-50%,-50%);box-shadow:0 0 0 2px var(--panel2);cursor:help}
.th-sc-mk-bear{background:#f78166}
.th-sc-mk-base{background:var(--amber)}
.th-sc-mk-bull{background:var(--green)}
.th-sc-base-tick{position:absolute;top:-4px;bottom:-4px;width:1px;background:var(--amber);opacity:.55}
.th-sc-cur{position:absolute;top:-5px;width:2px;height:20px;background:var(--txt);
  transform:translateX(-1px);box-shadow:0 0 0 2px rgba(11,15,23,.7);pointer-events:none}
.th-sc-prob{position:absolute;top:-16px;font-size:9px;color:var(--mut);font-variant-numeric:tabular-nums;
  transform:translateX(-50%);white-space:nowrap;letter-spacing:.02em;font-weight:600}
.th-sc-cap{display:flex;justify-content:space-between;gap:8px;font-size:9.5px;
  color:var(--mut);font-variant-numeric:tabular-nums;margin-top:8px;letter-spacing:.02em;flex-wrap:wrap}
.th-sc-cap b{color:var(--txt);font-weight:700}
.th-sc-cap-now{color:var(--txt);font-weight:700}
.th-sc-cap-entry{color:var(--amber);font-weight:600}
.th-sc-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;font-size:10px;margin-top:6px;
  font-variant-numeric:tabular-nums;color:var(--mut)}
.th-sc-cell{display:flex;flex-direction:column;line-height:1.25}
.th-sc-cell-base{text-align:center}
.th-sc-cell-bull{text-align:right}
.th-sc-cell-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.th-sc-cell-lbl-bear{color:#f78166}
.th-sc-cell-lbl-base{color:var(--amber)}
.th-sc-cell-lbl-bull{color:var(--green)}
.th-sc-cell-px{color:var(--txt);font-weight:700}
.th-sc-cell-trig{color:var(--mut);font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px;font-weight:500}
.th-sc-foot{font-size:9px;color:var(--mut);margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:.02em}
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
  .th-sc{padding:7px 8px 8px}
  .th-sc-h{font-size:8px}
  .th-sc-h-r{font-size:10px}
  .th-sc-row{font-size:9px}
  .th-sc-cell-trig{display:none}
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
/* Sektor-Heatmap (HED-137 Zyklus 93): Bloomberg-/Finviz-style universe scan.
   Cells = tickers, color = intraday move, grouped by sector. Single visual scan
   of where the heat is across the AI/Tech book. Above the detail tiles. */
.sec-hmap{margin-bottom:var(--s4);padding:var(--s3) var(--s3) var(--s2)}
.sec-hmap-h{display:flex;align-items:baseline;justify-content:space-between;gap:var(--s3);
  margin-bottom:var(--s2);flex-wrap:wrap}
.sec-hmap-h-title{font-weight:600;font-size:var(--fs-h2)}
.sec-hmap-h-sub{color:var(--mut);font-size:var(--fs-cap)}
.sec-hmap-legend{display:flex;align-items:center;gap:6px;font-size:var(--fs-micro);color:var(--mut)}
.sec-hmap-legend-sw{display:inline-block;width:14px;height:10px;border-radius:2px;vertical-align:middle}
.sec-hmap-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--s2)}
.sec-hmap-grp{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:var(--s2);
  display:flex;flex-direction:column;gap:6px}
.sec-hmap-grp-h{display:flex;align-items:baseline;gap:6px;font-size:var(--fs-micro);
  border-bottom:1px solid var(--line);padding-bottom:4px}
.sec-hmap-grp-h .id{color:var(--accent);font-weight:700;letter-spacing:.06em}
.sec-hmap-grp-h .nm{color:var(--txt);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sec-hmap-grp-h .avg{font-variant-numeric:tabular-nums;font-weight:600}
.sec-hmap-cells{display:grid;grid-template-columns:repeat(auto-fill,minmax(72px,1fr));gap:4px}
.sec-hmap-cell{display:flex;flex-direction:column;justify-content:center;align-items:stretch;
  padding:6px 4px;border-radius:5px;text-align:center;text-decoration:none;color:inherit;
  position:relative;cursor:help;min-height:48px;
  transition:transform .1s ease, box-shadow .1s ease;border:1px solid transparent}
.sec-hmap-cell:hover{transform:scale(1.05);box-shadow:0 2px 8px rgba(0,0,0,.45);z-index:2}
.sec-hmap-cell .tk{font-weight:700;font-size:12px;letter-spacing:.02em;line-height:1.1}
.sec-hmap-cell .ch{font-variant-numeric:tabular-nums;font-size:11px;line-height:1.15;margin-top:2px;opacity:.95}
/* Color tiers — 9-step diverging (red ⇄ green) with white text for contrast on saturated cells */
.hmap-c-pp3{background:#0d6f2d;color:#fff}
.hmap-c-pp2{background:#1f8b3f;color:#fff}
.hmap-c-pp1{background:#3aa454;color:#fff}
.hmap-c-pp0{background:rgba(63,185,80,.30);color:var(--txt)}
.hmap-c-z  {background:var(--panel);color:var(--mut)}
.hmap-c-nn0{background:rgba(248,81,73,.30);color:var(--txt)}
.hmap-c-nn1{background:#c44d44;color:#fff}
.hmap-c-nn2{background:#a73229;color:#fff}
.hmap-c-nn3{background:#7a201a;color:#fff}
.hmap-c-na {background:repeating-linear-gradient(45deg,var(--panel),var(--panel) 4px,var(--panel2) 4px,var(--panel2) 8px);
  color:var(--mut)}
/* Active-book marker: left accent + dir letter top-right */
.sec-hmap-cell.in-book{border-left:3px solid var(--accent)}
.sec-hmap-cell.in-book.book-long{border-left-color:var(--green)}
.sec-hmap-cell.in-book.book-short{border-left-color:var(--red)}
.sec-hmap-cell .book-tag{position:absolute;top:2px;right:3px;font-size:8px;font-weight:700;
  letter-spacing:.04em;padding:0 3px;border-radius:2px;
  background:rgba(11,15,23,.55);color:rgba(255,255,255,.92)}
@media (max-width:760px){
  .sec-hmap-groups{grid-template-columns:1fr}
  .sec-hmap-cells{grid-template-columns:repeat(auto-fill,minmax(64px,1fr))}
  .sec-hmap-cell{min-height:44px}
}
@media (max-width:430px){
  .sec-hmap{padding:var(--s2)}
  .sec-hmap-cells{grid-template-columns:repeat(auto-fill,minmax(60px,1fr));gap:3px}
  .sec-hmap-cell{padding:4px 3px;min-height:42px}
  .sec-hmap-cell .tk{font-size:11px}
  .sec-hmap-cell .ch{font-size:10px}
}
/* Sektor-Rotation-Matrix (HED-137 Zyklus 97): relative-strength matrix that shows
   which sector is accelerating / decelerating across multiple horizons, with book
   exposure overlay — answers "is the book positioned with the rotation?" at a glance. */
.sec-rot{padding:var(--s3);margin-bottom:var(--s3)}
.sec-rot-h{display:flex;justify-content:space-between;align-items:flex-end;gap:var(--s3);flex-wrap:wrap;margin-bottom:var(--s3)}
.sec-rot-title{font-size:var(--fs-h2);font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.sec-rot-sub{color:var(--mut);font-size:var(--fs-micro);margin-top:3px;line-height:1.4}
.sec-rot-callout{font-size:var(--fs-micro);color:var(--mut);font-variant-numeric:tabular-nums}
.sec-rot-callout b{color:var(--txt);font-weight:600}
.sec-rot-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.sec-rot-tbl th,.sec-rot-tbl td{padding:7px 8px;border-bottom:1px solid var(--line);font-size:var(--fs-cap);text-align:right;white-space:nowrap}
.sec-rot-tbl th:nth-child(1),.sec-rot-tbl td:nth-child(1),
.sec-rot-tbl th:nth-child(2),.sec-rot-tbl td:nth-child(2){text-align:left}
.sec-rot-tbl thead th{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:600;border-bottom:1px solid var(--line);padding-bottom:6px}
.sec-rot-tbl tbody tr:last-child td{border-bottom:none}
.sec-rot-tbl .sr-id{font-weight:700;color:var(--mut);font-size:var(--fs-micro);letter-spacing:.04em}
.sec-rot-tbl .sr-nm{color:var(--txt);font-weight:600}
.sec-rot-tbl .sr-n{color:var(--mut);font-size:var(--fs-micro);margin-left:4px;font-weight:400}
.sec-rot-tbl .sr-cell{border-radius:4px;padding:5px 6px;display:inline-block;min-width:54px;text-align:right;font-weight:600}
.sec-rot-tbl .sr-trend{font-size:14px;line-height:1;width:18px;display:inline-block;text-align:center}
.sec-rot-tbl .sr-trend-acc{color:var(--green)}
.sec-rot-tbl .sr-trend-dec{color:var(--red)}
.sec-rot-tbl .sr-trend-flat{color:var(--mut)}
.sec-rot-tbl .sr-bookcell{color:var(--txt);font-weight:600}
.sec-rot-tbl .sr-bookcell.long{color:var(--green)}
.sec-rot-tbl .sr-bookcell.short{color:var(--red)}
.sec-rot-tbl .sr-bookcell.none{color:var(--mut);font-weight:400}
.sec-rot-tbl tbody tr.sr-bench{background:rgba(110,153,184,.06)}
.sec-rot-tbl tbody tr.sr-bench td:first-child{color:var(--mut)}
.sec-rot-foot{color:var(--mut);font-size:var(--fs-micro);margin-top:var(--s2);line-height:1.4}
@media (max-width:760px){
  /* mobile: hide alpha column, reduce paddings; keep the core 5d/20d picture */
  .sec-rot-tbl th.sr-hide-mob,.sec-rot-tbl td.sr-hide-mob{display:none}
  .sec-rot-tbl th,.sec-rot-tbl td{padding:6px 5px;font-size:var(--fs-micro)}
  .sec-rot-tbl .sr-cell{min-width:42px;padding:4px 5px;font-size:var(--fs-micro)}
  .sec-rot-tbl .sr-nm{font-size:var(--fs-cap)}
}
@media (max-width:430px){
  /* very narrow: also drop the 30d column and 1d to keep table readable */
  .sec-rot-tbl th.sr-hide-narrow,.sec-rot-tbl td.sr-hide-narrow{display:none}
}
/* Valuation-Edge-Scatter (HED-137 cycle 99): Forward P/E × Revenue-Growth scatter for the
   in-universe watchlist — Bloomberg EQS-style positioning map. Quadrants split at axis
   medians; book positions get a green/red ring. Answers "is the book in cheap-growth
   or expensive-quality?" at a glance — fundamental backdrop next to the rotation/momentum view. */
.vs-panel{padding:var(--s3);margin-bottom:var(--s3)}
.vs-h{display:flex;justify-content:space-between;align-items:flex-end;gap:var(--s3);flex-wrap:wrap;margin-bottom:var(--s2)}
.vs-title{font-size:var(--fs-h2);font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.vs-sub{color:var(--mut);font-size:var(--fs-micro);margin-top:3px;line-height:1.4;max-width:62ch}
.vs-callout{font-size:var(--fs-micro);color:var(--mut);font-variant-numeric:tabular-nums}
.vs-callout b{color:var(--txt);font-weight:600}
.vs-chart{width:100%;height:auto;display:block;font-variant-numeric:tabular-nums}
.vs-chart .vs-grid{stroke:var(--line);stroke-width:1;opacity:.5}
.vs-chart .vs-med{stroke:var(--mut);stroke-width:1;stroke-dasharray:3 3;opacity:.55}
.vs-chart .vs-ax{stroke:var(--line);stroke-width:1}
.vs-chart text{fill:var(--mut);font-size:10px;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
.vs-chart .vs-axlbl{fill:var(--txt);font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.vs-chart .vs-qlbl{fill:var(--mut);font-size:9px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;opacity:.7}
.vs-chart .vs-dot{stroke:var(--bg);stroke-width:1.2;cursor:pointer;transition:r .12s,stroke-width .12s}
.vs-chart .vs-dot:hover{stroke-width:2}
.vs-chart .vs-dot.vs-book{stroke-width:2.4}
.vs-chart .vs-dot.vs-book-long{stroke:var(--green)}
.vs-chart .vs-dot.vs-book-short{stroke:var(--red)}
.vs-chart .vs-tklbl{fill:var(--txt);font-size:10px;font-weight:600;pointer-events:none}
.vs-chart .vs-tklbl-bg{fill:var(--bg);opacity:.78}
.vs-legend{display:flex;flex-wrap:wrap;gap:var(--s3) var(--s4);margin-top:var(--s2);font-size:var(--fs-micro);color:var(--mut)}
.vs-leg-item{display:inline-flex;align-items:center;gap:5px}
.vs-leg-sw{display:inline-block;width:10px;height:10px;border-radius:50%;border:1px solid var(--bg)}
.vs-leg-ring{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--panel2);border:2px solid var(--accent)}
.vs-foot{color:var(--mut);font-size:var(--fs-micro);margin-top:var(--s2);line-height:1.4}
@media (max-width:760px){
  .vs-panel{padding:var(--s2)}
  .vs-chart text{font-size:9px}
  .vs-chart .vs-tklbl{font-size:9px}
  .vs-chart .vs-axlbl{font-size:9px}
  .vs-chart .vs-qlbl{display:none}
  .vs-legend{font-size:10px;gap:var(--s2) var(--s3)}
}
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
    <a href="#h-earnplay">Earnings-Playbook</a>
    <a href="#h-scanner">Ideen-Scanner</a>
    <a href="#h-consspread">Konsens-Spread</a>
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

  <section aria-labelledby="h-insidertape">
  <h2 id="h-insidertape">Insider-Tape <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Smart-Money 30d</span></h2>
  <div id="insidertape" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:64%"></div><div class="skel skel-line" style="width:58%"></div></div></div>
  </section>

  <section aria-labelledby="h-catalysts">
  <h2 id="h-catalysts">Katalysator-Runway</h2>
  <div id="catalysts" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:62%"></div></div></div>
  </section>

  <section aria-labelledby="h-earnplay">
  <h2 id="h-earnplay">Earnings-Playbook <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Beat-Rate · Surprise · 1d-Reaktion</span></h2>
  <div id="earnplay" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:64%"></div><div class="skel skel-line" style="width:58%"></div></div></div>
  </section>

  <section aria-labelledby="h-scanner">
  <h2 id="h-scanner">Ideen-Scanner <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Universum-Screening</span></h2>
  <div id="universe-scanner" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:55%"></div><div class="skel skel-line" style="width:70%"></div></div></div>
  </section>

  <section aria-labelledby="h-consspread">
  <h2 id="h-consspread">Konsens-Spread <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Analystenuneinigkeit · Variant-Perception-Map</span></h2>
  <div id="consspread" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:52%"></div><div class="skel skel-line" style="width:68%"></div><div class="skel skel-line" style="width:60%"></div></div></div>
  </section>

  <section aria-labelledby="h-sectorview">
  <h2 id="h-sectorview">Sektor-Ansicht <span id="secstand" class="tag"></span></h2>
  <div class="panel sec-rot" id="sectorrotation" hidden></div>
  <div class="panel vs-panel" id="valuationscatter" hidden></div>
  <div class="panel sec-hmap" id="sectorheatmap" aria-busy="true" hidden></div>
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
  // Szenario-Korridor (HED-137 Zyklus 94): Bear→Base→Bull price-target bracket with
  // probability-weighted E[R]. Parses scenario.target ("$260 (+19%)" / "$240" / "+15%"),
  // anchors to baseline (entry) so % targets work even without live price.
  // Falls back to "" when fewer than 2 scenarios are parseable — caller then renders text line.
  function thScenarioCorridor(t){
    const sc=t.scenarios; if(!sc) return "";
    const tks=(t.tickers||[]).map(x=>String(x).toUpperCase()).filter(Boolean);
    const tk=tks[0]||null;
    const m=tk?_mktMap[tk]:null;
    const cur=(m&&m.price!=null)?m.price:null;
    const baseline=t.baseline_price!=null?t.baseline_price:((tk&&_baseMap[tk])?_baseMap[tk].bp:null);
    const anchor=baseline!=null?baseline:cur;
    function parseTgt(str){
      if(str==null) return null;
      const s=String(str).trim(); if(!s) return null;
      const pm=s.match(/\$\s*(\d{1,3}(?:[, ]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)/);
      if(pm){const num=parseFloat(pm[1].replace(/[, ]/g,"").replace(",",".")); if(isFinite(num)&&num>0) return num;}
      const ppm=s.match(/([+-]?\d+(?:[.,]\d+)?)\s*%/);
      if(ppm && anchor!=null){const p=parseFloat(ppm[1].replace(",",".")); if(isFinite(p)) return anchor*(1+p/100);}
      const bn=s.match(/^([+-]?\d+(?:[.,]\d+)?)$/);
      if(bn){const n=parseFloat(bn[1].replace(",",".")); if(isFinite(n)&&n>=1) return n;}
      return null;
    }
    const dir=(t.direction||(tk&&_baseMap[tk]&&_baseMap[tk].dir)||"long").toLowerCase();
    const sign=dir==="short"?-1:1;
    const rows=[
      {k:"bear",lbl:"Bear",obj:sc.bear},
      {k:"base",lbl:"Base",obj:sc.base},
      {k:"bull",lbl:"Bull",obj:sc.bull},
    ].map(r=>{
      const px=r.obj?parseTgt(r.obj.target):null;
      const prob=r.obj&&r.obj.prob!=null?Math.max(0,Math.min(1,Number(r.obj.prob))):null;
      const trig=r.obj&&r.obj.trigger?String(r.obj.trigger):"";
      return Object.assign({},r,{px:px,prob:prob,trig:trig});
    });
    const parsed=rows.filter(r=>r.px!=null);
    if(parsed.length<2) return "";
    const axisVals=parsed.map(r=>r.px).concat(baseline!=null?[baseline]:[]).concat(cur!=null?[cur]:[]);
    let lo=Math.min.apply(null,axisVals), hi=Math.max.apply(null,axisVals);
    if(hi<=lo){const pad0=Math.abs(lo)*0.02||1;lo-=pad0;hi+=pad0;}
    const padR=(hi-lo)*0.08; lo-=padR; hi+=padR;
    const range=hi-lo;
    const pos=v=>((v-lo)/range)*100;
    const refPx=baseline!=null?baseline:cur;
    let er=null, sd=null;
    if(refPx!=null && refPx>0){
      const r=parsed.filter(x=>x.prob!=null).map(x=>({p:x.prob,ret:sign*((x.px-refPx)/refPx)}));
      const totalP=r.reduce((a,c)=>a+c.p,0);
      if(r.length && totalP>0.05){
        const norm=r.map(x=>({p:x.p/totalP,ret:x.ret}));
        er=norm.reduce((a,c)=>a+c.p*c.ret,0);
        const v=norm.reduce((a,c)=>a+c.p*Math.pow(c.ret-er,2),0);
        sd=Math.sqrt(v);
      }
    }
    const fmt$=v=>v<10?v.toFixed(2):v<100?v.toFixed(1):Math.round(v).toString();
    const fmtP=p=>`${p>=0?"+":"−"}${Math.abs(p*100).toFixed(1)}%`;
    const erCls=er==null?"th-sc-er-flat":er>=0.005?"th-sc-er-up":er<=-0.005?"th-sc-er-dn":"th-sc-er-flat";
    const erTxt=er!=null?`E[R] ${fmtP(er)}`:"E[R] —";
    const sdTxt=sd!=null?` · σ ${(sd*100).toFixed(1)}%`:"";
    const lefts=parsed.map(r=>pos(r.px));
    const spanL=Math.min.apply(null,lefts), spanR=Math.max.apply(null,lefts);
    const spanBar=`<div class="th-sc-span" style="left:${spanL.toFixed(1)}%;width:${(spanR-spanL).toFixed(1)}%"></div>`;
    const mks=parsed.map(r=>{
      const x=pos(r.px);
      const retPct=refPx!=null&&refPx>0?sign*((r.px-refPx)/refPx)*100:null;
      const retLbl=retPct!=null?` (${retPct>=0?"+":"−"}${Math.abs(retPct).toFixed(1)}%)`:"";
      const probTxt=r.prob!=null?`P=${Math.round(r.prob*100)}%`:"";
      const probBadge=probTxt?`<span class="th-sc-prob" style="left:${x.toFixed(1)}%">${probTxt}</span>`:"";
      const tip=`${r.lbl}: $${fmt$(r.px)}${retLbl}${r.prob!=null?` · P=${Math.round(r.prob*100)}%`:""}${r.trig?` — ${r.trig}`:""}`;
      return `${probBadge}<span class="th-sc-mk th-sc-mk-${r.k}" style="left:${x.toFixed(1)}%" title="${esc(tip)}"></span>`;
    }).join("");
    let baseEl="";
    if(baseline!=null && baseline>=lo && baseline<=hi){
      const bx=pos(baseline);
      baseEl=`<div class="th-sc-base-tick" style="left:${bx.toFixed(1)}%" title="Entry $${fmt$(baseline)}"></div>`;
    }
    let curEl="";
    if(cur!=null && cur>=lo && cur<=hi){
      const cx=pos(cur);
      curEl=`<div class="th-sc-cur" style="left:${cx.toFixed(1)}%" title="aktuell $${fmt$(cur)}"></div>`;
    }
    // caption row under the bar — Now / Entry / vs-Entry, no overlap with marker probabilities
    let capL="", capR="";
    if(cur!=null) capL=`<span class="th-sc-cap-now">Now <b>$${fmt$(cur)}</b></span>`;
    if(baseline!=null) capL+=`${cur!=null?'<span style="opacity:.5"> · </span>':''}<span class="th-sc-cap-entry">Entry $${fmt$(baseline)}</span>`;
    if(cur!=null && baseline!=null && baseline>0){
      const livePct=sign*((cur-baseline)/baseline)*100;
      const cls=livePct>=0.05?"move-up":livePct<=-0.05?"move-dn":"muted";
      capR=`<span class="${cls}" style="font-weight:700">vs Entry ${livePct>=0?"+":"−"}${Math.abs(livePct).toFixed(2)}%</span>`;
    }
    const capRow=(capL||capR)?`<div class="th-sc-cap"><span>${capL||""}</span><span>${capR||""}</span></div>`:"";
    const cellHtml=(r,clsSuffix)=>{
      if(!r.obj) return `<div class="th-sc-cell th-sc-cell-${clsSuffix}"><span class="th-sc-cell-lbl th-sc-cell-lbl-${r.k}">${r.lbl}</span><span class="muted">—</span></div>`;
      const retPct=(r.px!=null&&refPx!=null&&refPx>0)?(sign*((r.px-refPx)/refPx)*100):null;
      const retLbl=retPct!=null?`<span class="${retPct>=0?'move-up':'move-dn'}" style="font-size:10px;font-weight:600">${retPct>=0?'+':'−'}${Math.abs(retPct).toFixed(1)}%</span>`:'<span class="muted">—</span>';
      const pxLbl=r.px!=null?`<span class="th-sc-cell-px">$${fmt$(r.px)}</span>`:'<span class="muted">—</span>';
      const probLbl=r.prob!=null?` <span class="muted">· P=${Math.round(r.prob*100)}%</span>`:"";
      return `<div class="th-sc-cell th-sc-cell-${clsSuffix}">
        <span class="th-sc-cell-lbl th-sc-cell-lbl-${r.k}">${r.lbl}${probLbl}</span>
        <span>${pxLbl} ${retLbl}</span>
        ${r.trig?`<span class="th-sc-cell-trig" title="${esc(r.trig)}">${esc(r.trig)}</span>`:""}
      </div>`;
    };
    const detRow=`<div class="th-sc-row">${cellHtml(rows[0],"bear")}${cellHtml(rows[1],"base")}${cellHtml(rows[2],"bull")}</div>`;
    const totalP=parsed.filter(x=>x.prob!=null).reduce((a,c)=>a+c.prob,0);
    const probNote=totalP>0 && Math.abs(totalP-1)>0.02 ? ` · Σp=${Math.round(totalP*100)}% (re-normalisiert)` : "";
    const refTxt=baseline!=null?`Entry $${fmt$(baseline)}`:(cur!=null?`Live $${fmt$(cur)}`:"keine Referenz");
    const foot=`<div class="th-sc-foot">Erwartungswert bezogen auf ${refTxt}, prob.-gewichtet${probNote}.${dir==="short"?" Richtung: SHORT — Returns invertiert.":""}</div>`;
    return `<div class="th-sc" role="img" aria-label="Szenario-Korridor: ${erTxt}${sdTxt}">
      <div class="th-sc-h"><span>Szenarien · Bear / Base / Bull</span><span class="th-sc-h-r ${erCls}">${erTxt}${sdTxt}</span></div>
      <div class="th-sc-bar">${spanBar}${mks}${baseEl}${curEl}</div>
      ${capRow}
      ${detRow}
      ${foot}
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
        ${(()=>{const corr=thScenarioCorridor(t); if(corr) return corr; if(!t.scenarios) return ""; const s=t.scenarios;const fmtS=(k,c)=>{if(!c)return null;const tgt=c.target?` → ${esc(c.target)}`:"";const p=c.prob!=null?` (P=${Math.round(c.prob*100)}%)`:"";return `${k}${c.trigger?" "+esc(c.trigger):""}${tgt}${p}`;};const parts=[fmtS("Bull",s.bull),fmtS("Base",s.base),fmtS("Bear",s.bear)].filter(Boolean);return parts.length?`<div class="sc-line">📐 ${parts.join(" | ")}</div>`:""})()}
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
  const _portSpySpark=((D.sector_view||{}).benchmarks||{})?.SPY?.spark||null;
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
  let curvePanelHtml="", riskStatsPanelHtml="", stressPanelHtml="", rollSharpeSvg="";
  let _portBeta=null, _portVolAnn=null, _portObs=0;
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
      // Risk-adjusted statistics panel — Sharpe, Vol, Beta, Korr, Tracking Error, Info-Ratio (HED-137 cycle 92).
      // Aligned daily returns: book cumulative pct → daily simple return via index-equivalent; SPY raw closes → simple return.
      // Common observation window = min(book days, SPY spark days). Standard institutional risk panel.
      if(_spySpark && _spySpark.length>=3 && _curve.length>=3){
        const ov=Math.min(_curve.length, _spySpark.length);
        if(ov>=3){
          const bookSlice=pcts.slice(pcts.length-ov);
          const spySlice=_spySpark.slice(_spySpark.length-ov);
          const rB=[], rS=[];
          for(let i=1;i<ov;i++){
            const b0=bookSlice[i-1], b1=bookSlice[i];
            const s0=spySlice[i-1], s1=spySlice[i];
            if(s0<=0) continue;
            rB.push((b1-b0)/(100+b0));
            rS.push((s1-s0)/s0);
          }
          const n=rB.length;
          if(n>=2){
            const mB=rB.reduce((s,v)=>s+v,0)/n;
            const mS=rS.reduce((s,v)=>s+v,0)/n;
            const mTE=mB-mS;
            let vB=0, vS=0, cov=0, vTE=0;
            for(let i=0;i<n;i++){
              const eB=rB[i]-mB, eS=rS[i]-mS;
              vB+=eB*eB; vS+=eS*eS; cov+=eB*eS;
              const eTE=(rB[i]-rS[i])-mTE;
              vTE+=eTE*eTE;
            }
            const denom=Math.max(1, n-1);
            vB/=denom; vS/=denom; cov/=denom; vTE/=denom;
            const sdB=Math.sqrt(Math.max(vB,0));
            const sdS=Math.sqrt(Math.max(vS,0));
            const sdTE=Math.sqrt(Math.max(vTE,0));
            const ANN=Math.sqrt(252);
            const volAnn=sdB*ANN*100;
            const sharpe=sdB>1e-9 ? mB/sdB*ANN : null;
            const beta=vS>1e-12 ? cov/vS : null;
            _portBeta=beta; _portVolAnn=volAnn; _portObs=n;
            const corr=(sdB>1e-9 && sdS>1e-9) ? cov/(sdB*sdS) : null;
            const trackErrAnn=sdTE*ANN*100;
            const infoRatio=sdTE>1e-9 ? mTE/sdTE*ANN : null;
            // Sortino Ratio — downside-deviation-adjusted Sharpe (HED-137 Zyklus 98).
            // Downside std = sqrt(mean(min(r,0)^2)) over full sample (denominator=n, not dN).
            let dsSumSq=0;
            rB.forEach(r=>{ if(r<0) dsSumSq+=r*r; });
            const dsStd=Math.sqrt(Math.max(dsSumSq/n,0));
            const sortino=dsStd>1e-9 ? mB/dsStd*ANN : null;
            // Calmar Ratio — CAGR / |maxDD%|. CAGR estimated from total return + observation days.
            const calmarMaxDD=Math.min(0,...pcts); // use same pcts as the equity curve
            const cagr=n>1?((Math.pow(Math.max(1e-9,1+lastPct/100),252/n)-1)*100):null;
            const calmar=(cagr!=null&&calmarMaxDD<-0.1)?cagr/Math.abs(calmarMaxDD):null;
            // Rolling 10d Sharpe: slide window across rB, aligned from inception to today.
            const ROLL_W=Math.max(5,Math.min(10,Math.floor(n/2)));
            const rollArr=[];
            if(n>=ROLL_W+1){
              for(let end=ROLL_W;end<=n;end++){
                const sl=rB.slice(end-ROLL_W,end);
                const rm=sl.reduce((a,b)=>a+b,0)/ROLL_W;
                let rv2=0; sl.forEach(x=>{const e=x-rm;rv2+=e*e;}); rv2/=Math.max(1,ROLL_W-1);
                const rsd=Math.sqrt(Math.max(rv2,0));
                rollArr.push({i:end-1, rs:rsd>1e-9?rm/rsd*ANN:null});
              }
            }
            const fmtR=v=>(v==null||!isFinite(v))?'—':(v>=0?'+':'−')+Math.abs(v).toFixed(2);
            const fmtP=v=>(v==null||!isFinite(v))?'—':v.toFixed(2)+'%';
            const sharpeBand=v=>v==null?'':v>=2?'exzellent':v>=1?'solide':v>=0?'unterdurchschn.':'negativ';
            const corrBand=v=>v==null?'':Math.abs(v)>=0.7?'stark gekoppelt':Math.abs(v)>=0.3?'moderat':'schwach';
            const betaBand=v=>v==null?'':v>=1.2?'hoch-Beta':v>=0.8?'≈ Markt':v>=0.3?'low-Beta':v>=-0.3?'marktneutral':'invers';
            const irBand=v=>v==null?'':v>=0.5?'starkes Alpha':v>=0?'positiv':'negativ';
            const sortinoBand=v=>v==null?'':v>=3?'exzellent':v>=2?'stark':v>=1?'solide':v>=0?'neutral':'negativ';
            const calmarBand=v=>v==null?'':v>=3?'exzellent':v>=1?'stark':v>=0.5?'solide':v>=0?'niedrig':'negativ';
            const sharpeCls=sharpe==null?'':sharpe>=1?'move-up':sharpe<0?'move-dn':'';
            const irCls=infoRatio==null?'':infoRatio>=0.5?'move-up':infoRatio<0?'move-dn':'';
            const sortinoCls=sortino==null?'':sortino>=2?'move-up':sortino<0?'move-dn':'';
            const calmarCls=calmar==null?'':calmar>=1?'move-up':calmar<0?'move-dn':'';
            const notice = n<10
              ? `<span class="rs-notice" title="Bei wenigen Beobachtungen sind die Schätzer verrauscht — Werte stabilisieren sich mit jedem Live-Tag">n=${n} · noisy</span>`
              : `<span class="muted" style="font-size:var(--fs-micro);font-variant-numeric:tabular-nums">n=${n} Tagesreturns</span>`;
            riskStatsPanelHtml=`<div class="panel rs-panel" style="margin-top:var(--s3)">
              <div class="rs-h">
                <div class="rs-title">Risiko-Kennzahlen <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">· annualisiert · vs SPY</span></div>
                ${notice}
              </div>
              <div class="rs-grid">
                <div class="rs-cell" title="Sharpe Ratio (rf=0): Mittelwert der Tagesreturns / Standardabweichung × √252. &gt;2 exzellent, &gt;1 solide, &lt;0 verliert risikoadjustiert."><div class="rs-lbl muted">Sharpe</div><div class="rs-val ${sharpeCls}">${fmtR(sharpe)}</div><div class="rs-sub muted">${sharpeBand(sharpe)}</div></div>
                <div class="rs-cell" title="Annualisierte Volatilität — Standardabweichung der Tagesreturns × √252."><div class="rs-lbl muted">Vola</div><div class="rs-val">${fmtP(volAnn)}</div><div class="rs-sub muted">ann.</div></div>
                <div class="rs-cell" title="Beta vs SPY — Cov(Buch, SPY)/Var(SPY). 1 ≈ Marktrisiko, &lt;1 defensiver, &gt;1 hebelhafter."><div class="rs-lbl muted">Beta</div><div class="rs-val">${fmtR(beta)}</div><div class="rs-sub muted">${betaBand(beta)}</div></div>
                <div class="rs-cell" title="Korrelation zu SPY — Pearson r der Tagesreturns. Niedrige Korrelation = diversifiziertes Alpha."><div class="rs-lbl muted">Korr.</div><div class="rs-val">${fmtR(corr)}</div><div class="rs-sub muted">${corrBand(corr)}</div></div>
                <div class="rs-cell" title="Tracking Error — Standardabweichung der aktiven Returns (Buch − SPY) × √252."><div class="rs-lbl muted">Tr-Error</div><div class="rs-val">${fmtP(trackErrAnn)}</div><div class="rs-sub muted">ann.</div></div>
                <div class="rs-cell" title="Information Ratio — annualisiertes Alpha geteilt durch Tracking Error. &gt;0.5 starkes Alpha pro Risikoeinheit."><div class="rs-lbl muted">Info-Ratio</div><div class="rs-val ${irCls}">${fmtR(infoRatio)}</div><div class="rs-sub muted">${irBand(infoRatio)}</div></div>
                <div class="rs-cell" title="Sortino Ratio — wie Sharpe, aber nur Downside-Volatilität im Nenner. Bestraft ausschließlich negative Returns. &gt;3 exzellent, &gt;2 stark, &gt;1 solide."><div class="rs-lbl muted">Sortino</div><div class="rs-val ${sortinoCls}">${fmtR(sortino)}</div><div class="rs-sub muted">${sortinoBand(sortino)}</div></div>
                <div class="rs-cell" title="Calmar Ratio — annualisierter Return geteilt durch Max Drawdown. Bewertet Return pro Einheit des schlimmsten Verlusts. &gt;3 exzellent, &gt;1 stark. Nur aussagekräftig wenn Drawdown &gt; 0.1%."><div class="rs-lbl muted">Calmar</div><div class="rs-val ${calmarCls}">${calmar!=null?fmtR(calmar):'—'}</div><div class="rs-sub muted">${calmarBand(calmar)}</div></div>
              </div>
              <div class="rs-foot muted">Aus täglichen Returns der konv.-gewichteten Buch-Kurve und SPY über den gemeinsamen Beobachtungszeitraum. Sortino: Downside-Std (nur neg. Returns). Calmar: CAGR-Schätzung / |Max-DD|. Standard-Risk-Panel (Bloomberg PORT-Stil). Werte stabilisieren sich mit längerer Live-Historie.</div>
            </div>`;
            // Rolling Sharpe mini-chart (HED-137 Zyklus 98): time-series consistency view.
            // Uses the same rB[] daily returns from the risk block, rolling ROLL_W-day windows.
            // X-axis: same horizontal extent as equity curve (pad.l / pad.r), scaled over rollArr.
            // Colored by current value (green = positive edge, red = negative).
            if(rollArr.length>=2){
              const rsH=56, rsPadT=14, rsPadB=8, rsIH=rsH-rsPadT-rsPadB;
              const rsIW=W-pad.l-16;
              const rsVals=rollArr.map(p=>p.rs).filter(v=>v!=null&&isFinite(v));
              if(rsVals.length>=2){
                const rsRawLo=Math.min(...rsVals), rsRawHi=Math.max(...rsVals);
                const rsSpan=Math.max(0.5,rsRawHi-rsRawLo);
                const rsLo=rsRawLo-rsSpan*0.15, rsHi=rsRawHi+rsSpan*0.15;
                const _yRS=v=>rsPadT+(rsHi-v)/(rsHi-rsLo)*rsIH;
                const rsZeroY=_yRS(0);
                const xS=rollArr.length>1?rsIW/(rollArr.length-1):0;
                const rsCoords=rollArr.map((p,j)=>[pad.l+j*xS, p.rs!=null&&isFinite(p.rs)?_yRS(p.rs):null]);
                const rsPts=rsCoords.filter(([,y])=>y!=null).map(([x,y])=>`${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
                const lastRS=rollArr[rollArr.length-1]?.rs;
                const rsCls=(lastRS!=null&&lastRS>=0)?"rs-pos":"rs-neg";
                const dotCls=(lastRS!=null&&lastRS>=0)?"rs-dot-pos":"rs-dot-neg";
                const lastRSfmt=lastRS!=null?((lastRS>=0?"+":"−")+Math.abs(lastRS).toFixed(2)):"—";
                const lastXY=rsCoords[rsCoords.length-1];
                const rsZeroLn=`<line class="ec-zero" x1="${pad.l}" y1="${rsZeroY.toFixed(1)}" x2="${(pad.l+rsIW).toFixed(1)}" y2="${rsZeroY.toFixed(1)}"/>`;
                const rsTitleCls=(lastRS!=null&&lastRS>=0)?'move-up':'move-dn';
                const rsHiTxt=rsHi.toFixed(1), rsLoTxt=rsLo.toFixed(1);
                rollSharpeSvg=`<svg class="rs-chart-svg" viewBox="0 0 ${W} ${rsH}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Rolling ${ROLL_W}d Sharpe Ratio — aktuell ${lastRSfmt}">
                  ${rsZeroLn}
                  ${rsPts?`<polyline class="rs-line ${rsCls}" points="${rsPts}"/>`:""}
                  ${(lastXY&&lastXY[1]!=null)?`<circle class="rs-dot ${dotCls}" cx="${lastXY[0].toFixed(1)}" cy="${lastXY[1].toFixed(1)}" r="3.5"><title>Rolling Sharpe ${lastRSfmt}</title></circle>`:""}
                  <text class="dd-title" x="${pad.l}" y="${(rsPadT-3).toFixed(1)}">Rolling Sharpe (${ROLL_W}d) · aktuell <tspan class="${rsTitleCls}">${lastRSfmt}</tspan></text>
                  <text class="ec-ylab" x="${(pad.l-6).toFixed(1)}" y="${(rsPadT+4).toFixed(1)}" text-anchor="end">${rsHiTxt}</text>
                  <text class="ec-ylab" x="${(pad.l-6).toFixed(1)}" y="${(rsPadT+rsIH).toFixed(1)}" text-anchor="end">${rsLoTxt}</text>
                </svg>`;
              }
            }
          }
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
        ${rollSharpeSvg}
        <div class="ec-foot muted">Honestes Inception-Tracking — die Kurve wächst mit jedem Handelstag. Indexiert bei 0% am Entry-Tag, sign-flipped für Shorts. Underwater-Chart: Drawdown vom rollierenden Hoch. Rolling Sharpe: rollierendes Fenster über Tagesreturns (annualisiert, rf=0) — zeigt ob die risikoadjustierte Kante konsistent bleibt oder verblasst.</div>
      </div>`;
    }
  }
  // Stress-Test-Panel (HED-137 Zyklus 95): Szenario-Shock-Schätzung — was passiert mit dem Buch,
  // wenn der Markt korrigiert, Tech rotiert oder eine Top-Position blowt up?
  // Systematische Shocks (SPY ±x%): β × shock (aus Risiko-Kennzahlen; Fallback Netto-Direktion wenn β fehlt).
  // Tech-Sektor-Shock: Σ(konv-gewichtete Exposure zu S1-S4) × shock × Richtung.
  // Single-Stock-Shock: Top-Conviction-Position × shock × Richtung.
  // Long-Positionen verlieren bei negativem Shock, Shorts profitieren — Vorzeichen-Logik beachtet das.
  if(active.length && totalConv>0){
    const _betaSrc = _portBeta!=null ? "β" : "Netto-Direktion";
    const _netDir = totalConv>0 ? (longConv-shortConv)/totalConv : 0;
    // Systematic impact: prefer β, fall back to net-direction proxy until enough live history exists
    function sysImpact(shockPct){
      if(_portBeta!=null) return _portBeta*shockPct;
      return _netDir*shockPct; // honest fallback — labels it as such
    }
    // Tech-sector exposure: sum conv-weighted, direction-signed long/short exposure to S1-S4
    const TECH=new Set(["S1","S2","S3","S4"]);
    let techNetExp=0, techGrossExp=0;
    active.forEach(t=>{
      const tk=(t.tickers||[])[0]; if(!tk) return;
      const secId=String(SECTOR_MAP[String(tk).toUpperCase()]||"").split(/\s+/)[0];
      if(!TECH.has(secId)) return;
      const w=(t.conviction||0)/totalConv;
      const dir=(t.direction||"").toLowerCase()==="short"?-1:1;
      techNetExp+=w*dir;
      techGrossExp+=w;
    });
    // Top-Conviction-Position (Single-Stock-Blow-up-Kandidat)
    const _topCall = active.slice().sort((a,b)=>(b.conviction||0)-(a.conviction||0))[0];
    const _topTk = (_topCall.tickers||[])[0]||"?";
    const _topW = (_topCall.conviction||0)/totalConv;
    const _topDir = (_topCall.direction||"").toLowerCase()==="short"?-1:1;
    const _topDirLbl = _topDir>0?"Long":"Short";
    // Define standard shocks
    const shocks=[
      {label:"Markt-Korrektur",     sub:"akuter Sell-off-Tag",     assume:"SPY −5%",  impact:sysImpact(-5),  kind:"sys", betaApplied:true},
      {label:"Bear-Markt-Eintritt", sub:"10% vom Hoch",            assume:"SPY −10%", impact:sysImpact(-10), kind:"sys", betaApplied:true},
      {label:"Tech-Rotation raus",  sub:"Sektor-Drehung S1–S4",   assume:"Tech −10%",
        impact:techGrossExp>0?(techNetExp*-10):null, kind:"tech", betaApplied:false,
        meta:`Netto-Tech-Exposure: ${(techNetExp*100>=0?"+":"−")}${Math.abs(techNetExp*100).toFixed(0)}% · brutto ${Math.round(techGrossExp*100)}%`},
      {label:"Single-Stock-Blow-up",sub:`Top: ${_topTk} (${_topDirLbl})`, assume:`${_topTk} −20%`,
        impact:_topW>0?(_topDir*-20*_topW):null, kind:"single", betaApplied:false,
        meta:`Conv-Gewicht: ${Math.round(_topW*100)}% · Move −20% wirkt ${_topDir>0?"belastend":"entlastend"} (${_topDirLbl})`},
      {label:"Risk-on Rally",       sub:"breite Tech-Rotation rein", assume:"SPY +5%",  impact:sysImpact(5),  kind:"sys", betaApplied:true},
    ];
    // Worst-case for header callout
    const _impacted = shocks.filter(s=>s.impact!=null);
    const _worst = _impacted.length ? _impacted.reduce((a,b)=>(a.impact<b.impact?a:b)) : null;
    // Bar sizing — symmetric around 0 using max |impact| across shocks (floor at 2pp so small books still register)
    const _maxAbs = Math.max(2, ..._impacted.map(s=>Math.abs(s.impact)));
    const _scaleLbl = Math.ceil(_maxAbs/2)*2;
    const _fmt = v => (v==null||!isFinite(v))?"—":(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%";
    const _cls = v => v==null?"muted":v>=0.05?"move-up":v<=-0.05?"move-dn":"muted";
    const rowsHtml = shocks.map(s=>{
      const v=s.impact;
      const w = v!=null ? Math.min(48, Math.abs(v)/_maxAbs*48) : 0;
      const barCls = v==null?"":v>=0?"st-bar-pos":"st-bar-neg";
      const barHtml = v==null
        ? '<div class="st-bar-track" title="Modellierung nicht anwendbar"><div class="st-bar-mid"></div></div>'
        : `<div class="st-bar-track" title="${_fmt(v)} Buch-Impact"><div class="st-bar-mid"></div><div class="st-bar-fill ${barCls}" style="width:${w}%"></div></div>`;
      const valCls = _cls(v);
      const tip = `${s.label} — ${s.assume}. ${s.meta||(s.betaApplied?`Methode: β=${_portBeta!=null?_portBeta.toFixed(2):"—"} × Shock`: "Modell: Direktion × Shock")}`;
      const subLine = s.meta ? `<span class="st-label-sub">${esc(s.meta)}</span>` : `<span class="st-label-sub">${esc(s.sub)}</span>`;
      return `<div class="st-row" title="${esc(tip)}">
        <div class="st-label">${esc(s.label)}${subLine}</div>
        <div class="st-assume"><span class="st-assume-tag">${esc(s.assume)}</span></div>
        ${barHtml}
        <div class="st-val ${valCls}">${_fmt(v)}</div>
      </div>`;
    }).join("");
    const _methodNote = _portBeta!=null
      ? `Systematische Schocks: β=<b>${_portBeta.toFixed(2)}</b> × Shock · n=${_portObs}`
      : `Systematische Schocks: Netto-Direktion <b>${(_netDir*100>=0?"+":"−")}${Math.abs(_netDir*100).toFixed(0)}%</b> × Shock (β kommt mit längerer Historie)`;
    const _worstCallout = _worst
      ? `<span class="st-worst" title="Schwerster modellierter Drawdown im Szenario-Set">📉 Worst-Case: ${esc(_worst.label)} → ${_fmt(_worst.impact)}</span>`
      : "";
    stressPanelHtml=`<div class="panel st-panel">
      <div class="st-h">
        <div>
          <div class="st-title">Stress-Test <span class="muted" style="font-weight:400">— Szenario-Shocks aufs Buch</span></div>
          <div class="st-sub">Geschätzter unrealisierter Impact pro Standard-Shock · Long verliert bei Down-Shock, Short gewinnt</div>
        </div>
        ${_worstCallout}
      </div>
      <div class="st-grid">
        <div class="st-row st-row-cap"><div>Szenario</div><div>Annahme</div><div>Impact-Bar</div><div style="text-align:right">Buch-Δ</div></div>
        ${rowsHtml}
      </div>
      <div class="st-axis"><div></div><div></div><div class="st-axis-bar"><span>−${_scaleLbl}%</span><span>0</span><span>+${_scaleLbl}%</span></div><div></div></div>
      <div class="st-foot">${_methodNote} · Tech-Shock = Netto-Exposure S1–S4 × Shock · Single-Stock = Top-Conviction-Position × Shock · Schätzer, keine echte Korrelations-Matrix.</div>
    </div>`;
  }
  // Open Calls Live-Monitor (HED-137 Zyklus 96): sortierbare PM-Positions-Tabelle.
  // Morgen-Scan: Ticker | Richtung | Entry-Datum | Tage live | Baseline → Aktuell | P&L% | vs-SPY% | α | Conviction | Exit-Trigger.
  // vs-SPY = SPY-Return über die gleiche Haltedauer (entry-date bis heute), abgeleitet aus SPY-Spark.
  // α = P&L% − vs-SPY% (excess return der Position über Benchmark-Periode).
  // Sortierbar: Klick auf Spaltenheader sortiert ab/aufsteigend; aktuelle Spalte zeigt Pfeil-Indikator.
  // Mobile: Karten-Layout (Label, P&L groß, α/vs-SPY, Datum/Conv in 2 Zeilen; Baseline/Exit versteckt).
  let liveMonitorHtml="";
  if(active.length){
    // SPY holding-period return for each call (entry → today)
    function _spyReturnForCall(dateStr){
      if(!_portSpySpark||_portSpySpark.length<2||!dateStr||!_asOfDateStr) return null;
      const back=_bdaysBack(dateStr, _asOfDateStr);
      if(back==null||back<1) return null;
      const eIdx=_portSpySpark.length-1-back;
      if(eIdx<0) return null; // older than spark history
      const base=_portSpySpark[eIdx];
      const last=_portSpySpark[_portSpySpark.length-1];
      if(!base||base<=0) return null;
      return (last-base)/base*100;
    }
    // Build rows
    const lmRows=pnlRows.map(r=>{
      const t=r.t;
      const tks=(t.tickers||[]).join("·")||"?";
      const dir=(t.direction||"").toLowerCase();
      const conv=t.conviction!=null?t.conviction:null;
      const dateStr=t.date||null;
      const daysLive=dateStr&&_asOfDateStr?_bdaysBack(dateStr,_asOfDateStr):null;
      const pnl=r.pnl; // already sign-flipped for shorts
      const spyRet=dateStr?_spyReturnForCall(dateStr):null;
      // For alpha: pnl is the call's return already sign-aware. SPY return is always raw (long-only bench).
      // For short calls: alpha = -(pnl_signed_for_short) - (-spyRet) ? No:
      // pnl here is already direction-aware (short P&L positive when price falls).
      // alpha = call_excess = pnl (dir-aware) - spyRet (raw SPY long return for same period)
      // This is correct: long 10% when SPY +5% = alpha +5. Short +8% when SPY +5% = alpha +3.
      const alpha=(pnl!=null&&spyRet!=null)?(pnl-spyRet):null;
      const curPrice=r.pnl!=null ? (() => {
        // Back-calculate current price from baseline + pnl% (or read from _pricePf)
        const tk=(t.tickers||[])[0];
        return tk?_pricePf[String(tk).toUpperCase()]||null:null;
      })() : null;
      return {t, tks, dir, conv, dateStr, daysLive, pnl, spyRet, alpha, curPrice};
    });
    // Default sort: P&L desc (winners first — PM scan for best/worst)
    let _lmSort="pnl", _lmAsc=false;
    function _sortRows(rows, col, asc){
      const vOf=r=>col==="pnl"?r.pnl:col==="alpha"?r.alpha:col==="spy"?r.spyRet:col==="days"?r.daysLive:col==="conv"?r.conv:r.pnl;
      return rows.slice().sort((a,b)=>{
        const va=vOf(a), vb=vOf(b);
        if(va==null&&vb==null) return 0;
        if(va==null) return 1; if(vb==null) return -1;
        return asc?(va-vb):(vb-va);
      });
    }
    const sortedRows=_sortRows(lmRows, _lmSort, _lmAsc);
    const fmtPct=v=>(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%";
    const fmtPctCls=v=>v==null?"lm-na":v>=0.05?"move-up":v<=-0.05?"move-dn":"muted";
    const fmtPrice=v=>v==null?"—":"$"+v.toFixed(2);
    const fmtDays=v=>v==null?"—":v===0?"heute":v===1?"1 Tag":`${v} Tage`;
    const cols=[
      {key:"ticker", label:"Call / Ticker", tip:"Ticker(s) und Thesis-Label"},
      {key:"dir",    label:"Dir",           tip:"Richtung: Long oder Short"},
      {key:"date",   label:"Entry",         tip:"Entry-Datum und Handelstage seit Einstieg"},
      {key:"baseline", label:"Basis → Kurs", tip:"Baseline (Entry-Preis) → aktueller Kurs"},
      {key:"pnl",    label:"P&L%",          tip:"Unrealisierte Performance seit Entry (sign-flipped für Shorts)"},
      {key:"spy",    label:"SPY%",           tip:"SPY-Return über dieselbe Haltedauer (benchmark)"},
      {key:"alpha",  label:"α",             tip:"Alpha = P&L% − SPY% (Excess-Return über Benchmark-Periode)"},
      {key:"conv",   label:"Conv",          tip:"Conviction-Score (0–1)"},
      {key:"exit",   label:"Exit-Trigger",  tip:"Definierter Exit-Auslöser"},
    ];
    const hdr=cols.map(c=>{
      const isSorted=c.key===_lmSort;
      const sortArrow=isSorted?(_lmAsc?"▲":"▼"):"";
      const sortable=["pnl","spy","alpha","days","conv"].includes(c.key);
      return `<th scope="col" title="${esc(c.tip)}" ${sortable?`data-sort="${c.key}"`:""}class="${isSorted?"lm-sorted":""}">${esc(c.label)}${sortable?`<span class="lm-sort-ind">${sortArrow}</span>`:""}</th>`;
    }).join("");
    const bodyRows=sortedRows.map(r=>{
      const dir=r.dir, long=dir==="long";
      const dirChip=`<span class="lm-dir ${long?"lm-dir-long":"lm-dir-short"}">${long?"LONG":"SHORT"}</span>`;
      const convBarW=r.conv!=null?Math.round(r.conv*100):0;
      const convDisp=r.conv!=null?r.conv.toFixed(2):'—';
      const convCell=`<div class="lm-conv-wrap"><div class="lm-conv-bar"><div class="lm-conv-fill" style="width:${convBarW}%"></div></div><span>${convDisp}</span></div>`;
      const exitCell=r.t.exit_trigger
        ? `<span class="lm-exit" title="${esc("Exit wenn: "+r.t.exit_trigger)}">${esc(r.t.exit_trigger)}</span>`
        : `<span class="lm-na">—</span>`;
      const baseTxt=r.t.baseline_price!=null?`<span class="lm-price-base">$${r.t.baseline_price.toFixed(2)}</span><span class="lm-arrow">→</span>${fmtPrice(r.curPrice)}`:'<span class="lm-na">—</span>';
      return `<tr>
        <td data-col="ticker"><span class="lm-tk">${esc(r.tks)}</span><span class="lm-lbl" title="${esc(r.t.label||"")}">${esc(r.t.label||"")}</span></td>
        <td data-col="dir">${dirChip}</td>
        <td data-col="date"><span class="lm-date">${esc(r.dateStr||"—")}</span><span class="lm-days">${fmtDays(r.daysLive)}</span></td>
        <td data-col="baseline"><span class="lm-price">${baseTxt}</span></td>
        <td data-col="pnl"><span class="lm-pnl ${fmtPctCls(r.pnl)}">${r.pnl!=null?fmtPct(r.pnl):'<span class="lm-na">—</span>'}</span></td>
        <td data-col="spy"><span class="lm-spy ${fmtPctCls(r.spyRet)}">${r.spyRet!=null?fmtPct(r.spyRet):'<span class="lm-na">—</span>'}</span></td>
        <td data-col="alpha"><span class="lm-alpha ${fmtPctCls(r.alpha)}">${r.alpha!=null?fmtPct(r.alpha):'<span class="lm-na">—</span>'}</span></td>
        <td data-col="conv">${convCell}</td>
        <td data-col="exit">${exitCell}</td>
      </tr>`;
    }).join("");
    // Summary chips: how many positive alpha calls
    const nAlpha=lmRows.filter(r=>r.alpha!=null&&r.alpha>0).length;
    const nPricedLm=lmRows.filter(r=>r.pnl!=null).length;
    const alphaTxt=nPricedLm?`${nAlpha} von ${nPricedLm} Calls mit positivem Alpha`:"";
    liveMonitorHtml=`<div class="panel lm-panel">
      <div class="lm-h">
        <div>
          <div class="lm-title">Open Calls — Live-Monitor</div>
          <div class="lm-sub">Positions-Tabelle · P&L und Alpha per Call · ${alphaTxt?`${alphaTxt} ·`:""} täglich mit Sector-View-Refresh</div>
        </div>
        <span class="lm-hint" title="Klick auf Spaltenheader zum Sortieren (P&L, SPY, α, Conv)">sortierbar</span>
      </div>
      <div class="lm-wrap">
        <table class="lm-tbl" id="lm-tbl">
          <thead><tr>${hdr}</tr></thead>
          <tbody>${bodyRows}</tbody>
        </table>
      </div>
      <div class="lm-foot">P&L sign-flipped für Shorts (Short +X% = Preis fiel um X%). vs-SPY = SPY-Return über dieselbe Haltedauer aus dem Spark. α = Excess Return. Klick auf P&L / α / Conv / SPY zum Sortieren.</div>
    </div>`;
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
  // Risk-Decomposition (HED-137 Zyklus 100 — Meilenstein): per-position contribution to
  // BOOK VOLATILITY, separately from weight and from return. Bloomberg PORT-R / BlackRock
  // Aladdin core risk view. Answers "which position drives my risk?" — a position can
  // hold 30% of capital weight but 55% of risk share; that gap is where concentrated
  // volatility actually lives, invisible in conviction-weight or sector breakdowns.
  //
  //   - Daily returns per ticker from 25d sparks, sign-flipped for shorts (P&L-Ebene)
  //   - Weights w_i = conviction_i / Σconviction (book share)
  //   - Sample covariance Cov[i][j] over the common observation window
  //   - Portfolio variance σ²_p = Σ_i Σ_j w_i w_j Cov[i][j]
  //   - Marginal Contribution to Risk:  MCTR_i = (Σ_j w_j Cov[i][j]) / σ_p
  //   - Component Contribution:         CTR_i  = w_i × MCTR_i        (Σ CTR_i = σ_p)
  //   - Risk share:                     s_i    = CTR_i / σ_p          (Σ s_i = 100%)
  //
  // Δ R−w = s_i − w_i. Positive = risk-concentrating, negative = risk-diluting (the
  // textbook diversification benefit). Top-2 risk share gives the concentration verdict.
  let riskDecompPanelHtml="";
  {
    const _decRets=[];
    active.forEach(t=>{
      const tk=(t.tickers||[])[0];
      if(!tk || t.conviction==null) return;
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
      _decRets.push({
        label:(t.tickers||[]).join("·")||tk,
        dir:(t.direction||"").toLowerCase(),
        conv:+t.conviction,
        r: r.map(v=>v*sign)
      });
    });
    if(_decRets.length>=2){
      const obs=Math.min(..._decRets.map(s=>s.r.length));
      const N=_decRets.length;
      if(obs>=3){
        const R=_decRets.map(s=>s.r.slice(-obs));
        const Mn=R.map(arr=>arr.reduce((a,b)=>a+b,0)/obs);
        const C=Array.from({length:N},()=>Array(N).fill(0));
        for(let i=0;i<N;i++) for(let j=i;j<N;j++){
          let s=0;
          for(let k=0;k<obs;k++) s+=(R[i][k]-Mn[i])*(R[j][k]-Mn[j]);
          const c=s/Math.max(1,obs-1);
          C[i][j]=c; C[j][i]=c;
        }
        const totConv=_decRets.reduce((s,x)=>s+x.conv,0)||1;
        const W=_decRets.map(x=>x.conv/totConv);
        let varP=0;
        for(let i=0;i<N;i++) for(let j=0;j<N;j++) varP+=W[i]*W[j]*C[i][j];
        const sdP=Math.sqrt(Math.max(varP,0));
        if(sdP>1e-9){
          const ANN=Math.sqrt(252);
          const ctr=[];
          for(let i=0;i<N;i++){
            let sumWC=0;
            for(let j=0;j<N;j++) sumWC+=W[j]*C[i][j];
            const mctr=sumWC/sdP;
            const ctc =W[i]*mctr;
            const share=ctc/sdP;
            ctr.push({
              i, label:_decRets[i].label, dir:_decRets[i].dir,
              w: W[i], share, mctr, ctc,
              delta: share - W[i]
            });
          }
          ctr.sort((a,b)=>b.share-a.share);
          const maxShare=Math.max(...ctr.map(x=>x.share));
          const maxW=Math.max(...ctr.map(x=>x.w));
          const barMax=Math.max(maxShare, maxW, 0.01);
          const fmtPct=v=>(v*100).toFixed(1)+"%";
          const fmtPP=v=>(v>=0?"+":"−")+Math.abs(v*100).toFixed(1)+"pp";
          const _dirCls=d=>d==="long"?"cd-long":d==="short"?"cd-short":"cd-pair";
          const rows=ctr.map(x=>{
            const wBarW=(x.w/barMax*100).toFixed(1);
            const rBarW=(x.share/barMax*100).toFixed(1);
            const deltaCls=x.delta>0.02?"move-dn":x.delta<-0.02?"move-up":"muted";
            const deltaTip=x.delta>0.02
              ? "Risiko-konzentrierend: Position trägt mehr zur Buch-Volatilität bei als ihr Gewicht vermuten lässt"
              : x.delta<-0.02
                ? "Risiko-verdünnend: Position trägt weniger zur Buch-Volatilität bei als ihr Gewicht — Diversifikations-Effekt"
                : "Beitrag entspricht ungefähr dem Gewicht";
            const dirChip=`<span class="cd ${_dirCls(x.dir)}" aria-label="${esc(x.dir.toUpperCase())}">${esc((x.dir.slice(0,1)||"·").toUpperCase())}</span>`;
            return `<tr>
              <th class="pf-rd-lbl" scope="row"><span class="pf-rd-name">${esc(x.label)}</span>${dirChip}</th>
              <td class="pf-rd-bar-cell">
                <div class="pf-rd-bar-stack">
                  <div class="pf-rd-bar pf-rd-bar-w" style="width:${wBarW}%" title="Gewicht (conviction-share): ${fmtPct(x.w)}"></div>
                  <div class="pf-rd-bar pf-rd-bar-r" style="width:${rBarW}%" title="Risiko-Beitrag: ${fmtPct(x.share)} der Buch-Volatilität"></div>
                </div>
              </td>
              <td class="pf-rd-num pf-rd-w-num" title="Gewicht im Buch (conviction-share)">${fmtPct(x.w)}</td>
              <td class="pf-rd-num pf-rd-r-num" title="Anteil an der Gesamt-Buch-Volatilität">${fmtPct(x.share)}</td>
              <td class="pf-rd-num pf-rd-d-num ${deltaCls}" title="${deltaTip}">${fmtPP(x.delta)}</td>
            </tr>`;
          }).join("");
          const top1=ctr[0], top2=ctr[1]||null;
          const top2Share=top1.share + (top2?top2.share:0);
          const concVerd = top2Share>=0.75 ? "Hoch konzentriert" : top2Share>=0.55 ? "Mäßig konzentriert" : "Breit verteilt";
          const concCls  = top2Share>=0.75 ? "move-dn"           : top2Share>=0.55 ? ""                    : "move-up";
          const sdAnnPct=(sdP*ANN*100).toFixed(2);
          const top2Lbl = top2 ? `${top1.label} + ${top2.label}` : top1.label;
          riskDecompPanelHtml=`<div class="panel pf-rd" style="margin-top:var(--s3)">
            <div class="pf-rd-h">
              <div class="pf-rd-title">Risiko-Dekomposition <span class="muted" style="font-weight:400">— Component CTR pro Call</span></div>
              <div class="pf-rd-meta muted">σ Buch (ann.) ${sdAnnPct}% · n=${obs} Tagesreturns</div>
            </div>
            <div class="pf-rd-wrap">
              <table class="pf-rd-tbl" role="table" aria-label="Risiko-Beitrag pro offenem Call">
                <thead>
                  <tr>
                    <th scope="col" class="pf-rd-th-name">Position</th>
                    <th scope="col" class="pf-rd-th-bar">Gewicht vs Risiko</th>
                    <th scope="col" class="pf-rd-num">w</th>
                    <th scope="col" class="pf-rd-num">σ-Anteil</th>
                    <th scope="col" class="pf-rd-num" title="Differenz Risiko-Anteil minus Gewicht. Positiv = Position konzentriert Risiko über ihr Gewicht hinaus.">Δ R−w</th>
                  </tr>
                </thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
            <div class="pf-rd-verd">
              <span class="muted">Top-2 Risiko-Treiber</span> <b>${esc(top2Lbl)}</b>
              <span class="${concCls}"><b>${(top2Share*100).toFixed(0)}%</b> der Buch-Volatilität</span>
              <span class="pf-rd-chip ${concCls}">${concVerd}</span>
            </div>
            <div class="pf-rd-legend muted">
              <span class="pf-rd-leg"><span class="pf-rd-leg-sw pf-rd-bar-w"></span>Gewicht (conviction-share)</span>
              <span class="pf-rd-leg"><span class="pf-rd-leg-sw pf-rd-bar-r"></span>Risiko-Beitrag (σ-Anteil)</span>
            </div>
            <div class="pf-rd-foot">Component Contribution to Risk: <i>w<sub>i</sub> · (Σ<sub>j</sub> w<sub>j</sub> Cov<sub>ij</sub>) / σ<sub>p</sub></i>. Summiert exakt auf σ<sub>p</sub>. Identifiziert Positionen, die mehr Risiko ins Buch tragen als ihr Gewicht impliziert — Risiko-Konzentration ohne Kapital-Konzentration. Methodik: Bloomberg PORT-R / BlackRock Aladdin.</div>
          </div>`;
        }
      }
    }
  }
  // Tech-Setup-Panel — Chart-Konfirmation pro offenem Call (HED-137 Zyklus 101).
  // Bridges fundamental thesis to technical setup. Per active call, evaluates:
  //   trend filter   = price vs 30-day MA (price above MA = uptrend, below = downtrend)
  //   momentum       = RSI14 (band: oversold <30, neutral 30-70, overbought >70)
  //   cycle position = pct_of_52w_high (≥90 = late-cycle, ≤40 = early)
  // Combines into per-call verdict aligned with call direction:
  //   Konfirmiert: chart and momentum agree with direction (high-conviction execution window)
  //   Überdehnt:   trend agrees BUT momentum exhausted in trade direction (late-chase warning)
  //   Gemischt:    partial confirmation; thesis-driven, not chart-driven
  //   Konflikt:    chart actively disagrees with direction (re-evaluate or wait for setup)
  // What a PM does every morning — look at the chart on every position before re-affirming it.
  let techPanelHtml="";
  if(active.length){
    const _techMap={};
    ((D.sector_view||{}).sectors||[]).forEach(s=>{
      (s.tickers||[]).forEach(t=>{
        if(!t||!t.ticker) return;
        _techMap[String(t.ticker).toUpperCase()]={
          price:t.price, ma30:t.ma30, pct_vs_ma30:t.pct_vs_ma30,
          rsi14:t.rsi14, w52_high:t.w52_high, w52_low:t.w52_low,
          pct_of_52w_high:t.pct_of_52w_high
        };
      });
    });
    const rows=active.map(t=>{
      const tk=(t.tickers||[])[0]; if(!tk) return null;
      const TK=String(tk).toUpperCase();
      const tech=_techMap[TK]; if(!tech) return null;
      const dir=(t.direction||"").toLowerCase();
      const isLong=dir==="long", isShort=dir==="short";
      if(!isLong&&!isShort) return null;
      const sign=isLong?1:-1;
      const ma=tech.pct_vs_ma30!=null?tech.pct_vs_ma30:null;
      const rsi=tech.rsi14!=null?tech.rsi14:null;
      const w52=tech.pct_of_52w_high!=null?tech.pct_of_52w_high:null;
      // Trend signal in direction terms: +1 confirms direction, -1 conflicts
      const trendSig = ma==null?0:(sign*ma>0?1:-1);
      // Momentum signal: +1 confirms, -1 conflicts, also flag "stretched" if RSI past extreme in trade direction
      let momSig=0, stretched=false;
      if(rsi!=null){
        if(isLong){
          if(rsi>=70){ momSig=1; stretched=true; }      // long & overbought = late-cycle
          else if(rsi>=50) momSig=1;
          else if(rsi>=30) momSig=0;
          else momSig=1;                                  // oversold = mean-rev long opp
        }else{ // short
          if(rsi<=30){ momSig=1; stretched=true; }      // short & oversold = late-cycle
          else if(rsi<=50) momSig=1;
          else if(rsi<=70) momSig=0;
          else momSig=1;                                  // overbought = mean-rev short opp
        }
      }
      // Cycle position signal: long near 52w high = late, long near low = room. Inverse for short.
      let cycSig=0;
      if(w52!=null){
        if(isLong){
          if(w52>=92) cycSig=-1;       // long but already at top of range
          else if(w52<=55) cycSig=1;   // long with runway
        }else{
          if(w52<=15) cycSig=-1;       // short but already crushed to lows
          else if(w52>=70) cycSig=1;   // short with downside room
        }
      }
      const score=trendSig+momSig+cycSig;
      // Verdict priority: stretched > clean-confirm > trend+momentum-conflict > partial-mixed.
      // Cycle-only-negative or trend-only-negative do not earn Konflikt — only when the two
      // primary signals (trend & momentum) both run against the call is the chart actively
      // hostile. Otherwise it's a mix, with a specific note explaining which leg is weak.
      let verd, vCls, vNote;
      if(stretched && trendSig>=0){
        verd="Überdehnt"; vCls="stretch";
        vNote=isLong?"RSI überkauft — Spät-Einstieg":"RSI überverkauft — Spät-Einstieg";
      } else if(trendSig===1 && momSig===1 && cycSig>=0){
        verd="Konfirmiert"; vCls="confirm"; vNote="Trend + Momentum stützen Call";
      } else if(trendSig===-1 && momSig===-1){
        verd="Konflikt"; vCls="conflict"; vNote="Trend und Momentum gegen Direction";
      } else if(trendSig===-1){
        verd="Gemischt"; vCls="mixed"; vNote=isLong?"Spot unter MA30 — Trend gegen Long":"Spot über MA30 — Trend gegen Short";
      } else if(cycSig===-1){
        verd="Gemischt"; vCls="mixed";
        vNote=isLong?"Nahe 52w-Hoch — wenig Runway":"Nahe 52w-Tief — wenig Downside";
      } else if(momSig===-1){
        verd="Gemischt"; vCls="mixed"; vNote="Momentum gegen Direction";
      } else {
        verd="Gemischt"; vCls="mixed"; vNote="Teilkonfirmation — thesis-getrieben";
      }
      return {
        t, tk:TK, label:(t.tickers||[]).join("·")||TK, dir, sign,
        ma, rsi, w52, price:tech.price, w52_high:tech.w52_high, w52_low:tech.w52_low,
        trendSig, momSig, cycSig, score, verd, vCls, vNote
      };
    }).filter(Boolean);
    if(rows.length){
      // Aggregate counts for header chips
      const cConf=rows.filter(r=>r.vCls==="confirm").length;
      const cMix =rows.filter(r=>r.vCls==="mixed"  ).length;
      const cStr =rows.filter(r=>r.vCls==="stretch").length;
      const cCon =rows.filter(r=>r.vCls==="conflict").length;
      const skipped=active.length-rows.length;
      const _fmtPct=v=>v==null?"—":(v>=0?"+":"−")+Math.abs(v).toFixed(1)+"%";
      const _maCell=(r)=>{
        if(r.ma==null) return `<span class="pf-tech-na">—</span>`;
        const supports=r.sign*r.ma>0;
        const cls=supports?"move-up":"move-dn";
        const arrow=r.ma>=0?"▲":"▼";
        return `<span class="pf-tech-ma ${cls}" title="Spot ${r.ma>=0?"über":"unter"} MA30 um ${Math.abs(r.ma).toFixed(2)}% · ${supports?"stützt":"konfligiert mit"} ${r.dir.toUpperCase()}"><span class="arrow">${arrow}</span>${_fmtPct(r.ma)}</span>`;
      };
      const _rsiCell=(r)=>{
        if(r.rsi==null) return `<span class="pf-tech-na">—</span>`;
        const v=Math.max(0,Math.min(100,r.rsi));
        const zone=r.rsi>=70?"überkauft":r.rsi<=30?"überverkauft":"neutral";
        const zCls=r.rsi>=70?"move-dn":r.rsi<=30?"move-up":"muted";
        return `<div class="pf-tech-rsi" title="RSI14 = ${r.rsi.toFixed(1)} · ${zone}">
          <span class="pf-tech-rsi-val">${r.rsi.toFixed(0)}</span>
          <div class="pf-tech-rsi-bar"><span class="pf-tech-rsi-mark" style="left:calc(${v.toFixed(1)}% - 1px)"></span></div>
          <span class="pf-tech-rsi-zone ${zCls}">${zone}</span>
        </div>`;
      };
      const _rangeCell=(r)=>{
        if(r.w52==null) return `<span class="pf-tech-na">—</span>`;
        const v=Math.max(0,Math.min(100,r.w52));
        const tip=`${r.w52.toFixed(0)}% des 52w-Hochs · Spot ${r.price!=null?"$"+r.price.toFixed(2):"—"} · Range ${r.w52_low!=null?"$"+r.w52_low.toFixed(2):"—"} – ${r.w52_high!=null?"$"+r.w52_high.toFixed(2):"—"}`;
        return `<div class="pf-tech-range" title="${esc(tip)}">
          <div class="pf-tech-range-bar"><span class="pf-tech-range-mark" style="left:${v.toFixed(1)}%"></span></div>
          <div class="pf-tech-range-lbl"><span>52w Tief</span><span>Hoch</span></div>
          <div class="pf-tech-range-pct">${r.w52.toFixed(0)}% Range</div>
        </div>`;
      };
      // Sort: conflicts first, then stretched, then mixed, then confirmed (PM scans risks first)
      const order={conflict:0,stretch:1,mixed:2,confirm:3};
      const sorted=rows.slice().sort((a,b)=>(order[a.vCls]-order[b.vCls])||((b.t.conviction||0)-(a.t.conviction||0)));
      const bodyRows=sorted.map(r=>{
        const dirChip=`<span class="pf-tech-dir pf-tech-dir-${r.dir}">${r.dir.toUpperCase()}</span>`;
        return `<tr>
          <td class="col-name"><div class="pf-tech-name"><span class="pf-tech-tk">${esc(r.label)}</span><span class="pf-tech-lbl" title="${esc(r.t.label||"")}">${esc(r.t.label||"")}</span></div></td>
          <td class="col-dir">${dirChip}</td>
          <td class="num col-ma" data-label="vs MA30">${_maCell(r)}</td>
          <td class="col-rsi" data-label="RSI14">${_rsiCell(r)}</td>
          <td class="col-range" data-label="52w">${_rangeCell(r)}</td>
          <td class="pf-tech-verd col-verd"><span class="pf-tech-verd-pill pf-tech-verd-${r.vCls}" title="${esc(r.vNote)}">${r.verd}</span><span class="pf-tech-verd-note">${esc(r.vNote)}</span></td>
        </tr>`;
      }).join("");
      const chip=(n,lbl,cls)=>n>0?`<span class="pf-tech-chip pf-tech-chip-${cls}"><b>${n}</b> ${lbl}</span>`:"";
      const chips=[chip(cConf,"konfirmiert","confirm"),chip(cStr,"überdehnt","stretch"),chip(cMix,"gemischt","mixed"),chip(cCon,"in Konflikt","conflict")].filter(Boolean).join("");
      const skipNote=skipped>0?` · ${skipped} Call${skipped===1?"":"s"} ohne Tech-Daten ausgeblendet`:"";
      techPanelHtml=`<div class="panel pf-tech">
        <div class="pf-tech-h">
          <div>
            <div class="pf-tech-title">Tech-Setup — Chart-Konfirmation pro Call</div>
            <div class="pf-tech-sub">Bestätigt der Chart die These? Trend (MA30) · Momentum (RSI14) · Cycle-Position (52w-Range)${skipNote}</div>
          </div>
          <div class="pf-tech-chips">${chips}</div>
        </div>
        <div class="pf-tech-wrap">
          <table class="pf-tech-tbl" role="table" aria-label="Tech-Setup pro offenem Call">
            <thead><tr>
              <th scope="col">Call</th>
              <th scope="col">Dir</th>
              <th scope="col" class="num" title="Spot relativ zur 30-Tage-gleitenden Durchschnitt — Trendfilter">vs MA30</th>
              <th scope="col" class="num" title="Relative Strength Index 14 — Momentum / Über- vs Verkauft">RSI14</th>
              <th scope="col" class="center col-range" title="Position innerhalb der 52-Wochen-Range">52w-Range</th>
              <th scope="col" class="center">Verdikt</th>
            </tr></thead>
            <tbody>${bodyRows}</tbody>
          </table>
        </div>
        <div class="pf-tech-foot">Konfirmiert = Trend + Momentum stützen die Call-Direction (Chart-Druck im Rücken). Überdehnt = Trend stützt, aber RSI im Extrem in Trade-Richtung (Spät-Einstieg-Warnung). Gemischt = thesis-getrieben, ohne klares Chart-Setup. Konflikt = Chart läuft gegen die Direction — re-evaluieren oder Setup abwarten. Methodik: Trend-Filter + Momentum-Oszillator + Cycle-Position, klassische 3-Faktor-Tech-Setup-Logik.</div>
      </div>`;
    }
  }
  root.innerHTML=`<div class="pf-grid">${kpiHtml}</div>${curvePanelHtml}${riskStatsPanelHtml}${stressPanelHtml}${liveMonitorHtml}${techPanelHtml}${allocHtml}<div class="grid two-col" style="gap:var(--s3)">${barHtml}${secBarHtml}</div>${pnlPanelHtml}${attribPanelHtml}${scatterPanelHtml}${corrPanelHtml}${riskDecompPanelHtml}${riskHtml}`;
  // Live-Monitor sort — attach after innerHTML so DOM nodes exist.
  // Re-orders <tr> nodes by parsing numeric data-* attrs stamped here.
  (function initLmSort(){
    const tbl=document.getElementById("lm-tbl");
    if(!tbl) return;
    function parseValSigned(el){
      if(!el) return null;
      const raw=(el.textContent||"").trim();
      if(!raw||raw==="—") return null;
      const neg=raw.startsWith("−");
      const num=parseFloat(raw.replace("−","").replace(/[^0-9.]/g,""));
      return isNaN(num)?null:(neg?-num:num);
    }
    // Stamp sort keys on each row
    tbl.querySelectorAll("tbody tr").forEach(tr=>{
      tr.dataset.pnl  = parseValSigned(tr.querySelector("[data-col='pnl'] .lm-pnl")) ?? "";
      tr.dataset.spy  = parseValSigned(tr.querySelector("[data-col='spy'] .lm-spy"))  ?? "";
      tr.dataset.alpha= parseValSigned(tr.querySelector("[data-col='alpha'] .lm-alpha")) ?? "";
      const cs=tr.querySelector("[data-col='conv'] .lm-conv-wrap span:last-child");
      tr.dataset.conv = cs?parseFloat(cs.textContent)||"":"";
      const dt=tr.querySelector("[data-col='date'] .lm-days");
      const dm=(dt?.textContent||"").match(/\d+/);
      tr.dataset.days = dm?dm[0]:"";
    });
    let _col="pnl", _asc=false;
    function resort(){
      const tbody=tbl.querySelector("tbody");
      const rows=Array.from(tbody.querySelectorAll("tr"));
      rows.sort((a,b)=>{
        const va=parseFloat(a.dataset[_col]), vb=parseFloat(b.dataset[_col]);
        const na=isNaN(va), nb=isNaN(vb);
        if(na&&nb) return 0; if(na) return 1; if(nb) return -1;
        return _asc?(va-vb):(vb-va);
      });
      rows.forEach(r=>tbody.appendChild(r));
    }
    tbl.querySelectorAll("thead th[data-sort]").forEach(th=>{
      th.addEventListener("click",()=>{
        const col=th.dataset.sort;
        if(col===_col) _asc=!_asc; else {_col=col;_asc=false;}
        resort();
        tbl.querySelectorAll("thead th").forEach(h=>{
          h.classList.toggle("lm-sorted",h.dataset.sort===_col);
          const ind=h.querySelector(".lm-sort-ind");
          if(ind) ind.textContent=h.dataset.sort===_col?(_asc?"▲":"▼"):"";
        });
      });
    });
  })();
  root.setAttribute("aria-busy","false");
})();

// Sektor-Rotation-Matrix (HED-137 Zyklus 97): relative-strength matrix across
// 5d / 20d / 30d windows, with alpha-vs-SPY and book net-exposure overlays.
// Answers the PM rotation question: which sector is accelerating, which is
// fading, and is the book positioned WITH or AGAINST the move?
//
//   - returns are equal-weight average of per-ticker pct change over the window,
//     computed from the 30-day spark of each ticker (spark[-1] = today's close)
//   - alpha column = sector_avg_20d − SPY_20d (excess return, in pp)
//   - trend indicator: compares 5d daily rate vs 20d daily rate; ≥1.5× = ↗ accel,
//     ≤0.5× = ↘ decel, else → flat (also flags a sign flip as inflection)
//   - book exposure = Σ(conviction × dir_sign) / Σ(conviction) over track-record
//     theses mapped into the sector
(function renderSectorRotation(){
  const sv=D.sector_view, root=$("sectorrotation");
  if(!sv || !(sv.sectors||[]).length || !root) return;
  const sectors=sv.sectors.filter(s=>(s.tickers||[]).length>0);
  if(!sectors.length){ return; }
  // Window-based avg return over the spark window. spark[-1] = latest close.
  // pct = (last - prior) / prior * 100, where prior = spark[len-1-N].
  function _retOverWindow(spark, n){
    if(!Array.isArray(spark)||spark.length<n+1) return null;
    const last=spark[spark.length-1], prior=spark[spark.length-1-n];
    if(!(prior>0)||!isFinite(last)) return null;
    return (last-prior)/prior*100;
  }
  function _avgRet(tickers, n){
    const vals=[];
    (tickers||[]).forEach(t=>{
      const r=_retOverWindow(t.spark, n);
      if(r!=null && isFinite(r)) vals.push(r);
    });
    if(!vals.length) return null;
    return {avg:vals.reduce((a,b)=>a+b,0)/vals.length, n:vals.length};
  }
  const spySpark=((sv.benchmarks||{}).SPY||{}).spark;
  const qqqSpark=((sv.benchmarks||{}).QQQ||{}).spark;
  const spy5=_retOverWindow(spySpark,5), spy20=_retOverWindow(spySpark,20), spy30=_retOverWindow(spySpark,29);
  const spy1=_retOverWindow(spySpark,1);
  const qqq5=_retOverWindow(qqqSpark,5), qqq20=_retOverWindow(qqqSpark,20), qqq30=_retOverWindow(qqqSpark,29);
  const qqq1=_retOverWindow(qqqSpark,1);
  // Book exposure by sector — net conviction-weighted (long − short) / total
  const SECTOR_MAP={};
  sectors.forEach(s=>{
    (s.tickers||[]).forEach(tk=>{
      const sym=tk&&tk.ticker!=null?String(tk.ticker).toUpperCase():null;
      if(sym) SECTOR_MAP[sym]=s.id;
    });
  });
  const tr=D.track_record;
  const theses=(tr&&Array.isArray(tr.theses))?tr.theses:[];
  // active = not closed/scored
  const isClosed=t=>{ const v=String(t.verdict||"").toLowerCase(); return v==="hit"||v==="miss"||v==="closed"||v==="exit"; };
  const active=theses.filter(t=>!isClosed(t));
  const bookBySec={}; let totalConv=0;
  active.forEach(t=>{
    const tk=(t.tickers||[])[0]; if(!tk) return;
    const sec=SECTOR_MAP[String(tk).toUpperCase()]; if(!sec) return;
    const conv=t.conviction!=null?+t.conviction:0;
    if(!(conv>0)) return;
    const sign=(String(t.direction||"").toLowerCase()==="short")?-1:1;
    if(!bookBySec[sec]) bookBySec[sec]={net:0, gross:0};
    bookBySec[sec].net+=sign*conv;
    bookBySec[sec].gross+=conv;
    totalConv+=conv;
  });
  // Build rows
  const rows=sectors.map(s=>{
    const r1 =_avgRet(s.tickers, 1);
    const r5 =_avgRet(s.tickers, 5);
    const r20=_avgRet(s.tickers, 20);
    const r30=_avgRet(s.tickers, 29);
    const alpha20=(r20&&spy20!=null)?(r20.avg-spy20):null;
    const book=bookBySec[s.id];
    const bookNetW=book?(book.net/Math.max(totalConv,1e-9)):0; // share of total conviction, signed
    // Trend: 5d daily rate vs 20d daily rate
    let trend="flat";
    if(r5 && r20 && r5.avg!=null && r20.avg!=null){
      const d5=r5.avg/5, d20=r20.avg/20;
      // sign-flip = potential inflection
      if(Math.sign(d5)!==Math.sign(d20) && Math.abs(d5)>0.1) trend = d5>0?"acc":"dec";
      else if(d20!==0){
        const ratio=d5/d20;
        if(d20>0){ trend = ratio>=1.5?"acc":ratio<=0.5?"dec":"flat"; }
        else     { trend = ratio>=1.5?"dec":ratio<=0.5?"acc":"flat"; }
      } else if(Math.abs(d5)>0.1){ trend = d5>0?"acc":"dec"; }
    }
    return {id:s.id, name:s.name, r1, r5, r20, r30, alpha20, bookNetW, gross:book?book.gross:0, trend};
  });
  // Drop sectors that have no data at all
  const usable=rows.filter(r=>r.r1||r.r5||r.r20||r.r30);
  if(!usable.length){ return; }
  // Sort by 5d desc (rotation leaders first); fall back to 20d, then 30d
  usable.sort((a,b)=>{
    const av=(a.r5&&a.r5.avg!=null)?a.r5.avg:((a.r20&&a.r20.avg!=null)?a.r20.avg:((a.r30&&a.r30.avg!=null)?a.r30.avg:-1e9));
    const bv=(b.r5&&b.r5.avg!=null)?b.r5.avg:((b.r20&&b.r20.avg!=null)?b.r20.avg:((b.r30&&b.r30.avg!=null)?b.r30.avg:-1e9));
    return bv-av;
  });
  // Color tier shared with heatmap (hmap-c-*) — scale by magnitude
  function _tier(c, scaleMax){
    if(c==null) return "na";
    const a=Math.abs(c);
    const lo=scaleMax*0.06, m1=scaleMax*0.2, m2=scaleMax*0.5, m3=scaleMax*0.9;
    if(a<lo) return "z";
    if(c>0)  return a>=m3?"pp3":a>=m2?"pp2":a>=m1?"pp1":"pp0";
    return            a>=m3?"nn3":a>=m2?"nn2":a>=m1?"nn1":"nn0";
  }
  function _cellHtml(rv, scaleMax){
    if(!rv||rv.avg==null) return '<span class="sr-cell hmap-c-na" title="keine Daten">—</span>';
    const v=rv.avg, cls=_tier(v, scaleMax);
    const txt=(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%";
    return `<span class="sr-cell hmap-c-${cls}" title="${rv.n} Ticker, Ø ${txt}">${txt}</span>`;
  }
  function _alphaHtml(a){
    if(a==null) return '<span class="sr-cell hmap-c-na" title="kein SPY-Vergleich">—</span>';
    const cls=_tier(a, 5);
    const txt=(a>=0?"+":"−")+Math.abs(a).toFixed(2)+"pp";
    return `<span class="sr-cell hmap-c-${cls}" title="α vs SPY 20d: ${txt}">${txt}</span>`;
  }
  function _trendHtml(t){
    if(t==="acc") return '<span class="sr-trend sr-trend-acc" title="Beschleunigt: 5d-Rate &gt; 20d-Rate (×1.5+)">↗</span>';
    if(t==="dec") return '<span class="sr-trend sr-trend-dec" title="Verlangsamt sich: 5d-Rate &lt; 20d-Rate (×0.5−)">↘</span>';
    return '<span class="sr-trend sr-trend-flat" title="Stabiles Tempo: 5d ≈ 20d">→</span>';
  }
  function _bookHtml(r){
    if(!r.gross){ return '<span class="sr-bookcell none" title="keine offene Position in diesem Sektor">—</span>'; }
    const netPct=r.bookNetW*100;
    const cls=netPct>0.5?"long":netPct<-0.5?"short":"none";
    const sign=netPct>=0?"+":"−";
    const txt=`${sign}${Math.abs(netPct).toFixed(0)}%`;
    const grossPct=(r.gross/Math.max(totalConv,1e-9))*100;
    return `<span class="sr-bookcell ${cls}" title="Netto-Exposure: ${txt} der Buch-Conviction (brutto ${grossPct.toFixed(0)}%)">${txt}</span>`;
  }
  // Scale picks: 1d uses ±3%, 5d uses ±6%, 20d uses ±12%, 30d uses ±15% so the
  // heat tiers stay meaningful across windows (one day moves 5× faster than a month).
  const SC1=3, SC5=6, SC20=12, SC30=15;
  const tbodyHtml=usable.map(r=>{
    const tickN=Math.max(...[r.r1,r.r5,r.r20,r.r30].filter(Boolean).map(x=>x.n||0));
    return `<tr>
      <td><span class="sr-id">${esc(r.id)}</span></td>
      <td><span class="sr-nm">${esc(r.name)}</span><span class="sr-n">n=${tickN}</span></td>
      <td class="sr-hide-narrow">${_cellHtml(r.r1, SC1)}</td>
      <td>${_cellHtml(r.r5, SC5)}</td>
      <td>${_cellHtml(r.r20, SC20)}</td>
      <td class="sr-hide-narrow">${_cellHtml(r.r30, SC30)}</td>
      <td class="sr-hide-mob">${_alphaHtml(r.alpha20)}</td>
      <td>${_bookHtml(r)}</td>
      <td>${_trendHtml(r.trend)}</td>
    </tr>`;
  }).join("");
  // Benchmark rows
  function _benchRow(label, r1, r5, r20, r30){
    const cell=(v,sc)=>(v==null)
      ? '<span class="sr-cell hmap-c-na">—</span>'
      : `<span class="sr-cell hmap-c-${_tier(v,sc)}" title="${label}: ${(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%"}">${(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%"}</span>`;
    return `<tr class="sr-bench">
      <td><span class="sr-id">BM</span></td>
      <td><span class="sr-nm">${esc(label)}</span></td>
      <td class="sr-hide-narrow">${cell(r1,SC1)}</td>
      <td>${cell(r5,SC5)}</td>
      <td>${cell(r20,SC20)}</td>
      <td class="sr-hide-narrow">${cell(r30,SC30)}</td>
      <td class="sr-hide-mob"><span class="sr-cell hmap-c-z" title="Benchmark — keine α-Berechnung">—</span></td>
      <td><span class="sr-bookcell none">—</span></td>
      <td><span class="sr-trend sr-trend-flat" title="Benchmark">·</span></td>
    </tr>`;
  }
  const benchHtml=_benchRow("SPY (Markt)", spy1, spy5, spy20, spy30)
                + _benchRow("QQQ (Nasdaq-100)", qqq1, qqq5, qqq20, qqq30);
  // Callout: leader and laggard over 5d, with book-context.
  const lead=usable[0], lag=usable[usable.length-1];
  let calloutHtml="";
  function _fmtPct(v){ return v==null?"—":(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%"; }
  if(lead && lead.r5 && lead.r5.avg!=null){
    const leadTxt=_fmtPct(lead.r5.avg);
    const lagTxt =(lag&&lag.r5&&lag.r5.avg!=null)?_fmtPct(lag.r5.avg):"—";
    calloutHtml=`<div class="sec-rot-callout">5d-Leader: <b>${esc(lead.name)}</b> ${leadTxt}` +
      ((lag&&lag!==lead)?` · 5d-Schlusslicht: <b>${esc(lag.name)}</b> ${lagTxt}`:"") + `</div>`;
  }
  root.hidden=false;
  root.innerHTML=`
    <div class="sec-rot-h">
      <div>
        <div class="sec-rot-title">Sektor-Rotation — Relative-Stärke-Matrix</div>
        <div class="sec-rot-sub">Wer beschleunigt, wer fällt zurück · gleichgewichteter Sektor-Avg über 1d/5d/20d/30d · α vs SPY 20d · Buch-Netto-Conviction-Exposure</div>
      </div>
      ${calloutHtml}
    </div>
    <table class="sec-rot-tbl" role="table" aria-label="Sektor-Rotation-Matrix: Avg-Returns, Alpha und Buch-Exposure">
      <thead><tr>
        <th>ID</th>
        <th>Sektor</th>
        <th class="sr-hide-narrow" title="Tagesveränderung (Sektor-Avg)">1d</th>
        <th title="5-Tage-Avg-Return">5d</th>
        <th title="20-Tage-Avg-Return">20d</th>
        <th class="sr-hide-narrow" title="30-Tage-Avg-Return">30d</th>
        <th class="sr-hide-mob" title="Alpha vs SPY über 20d (Sektor − Markt, in Prozentpunkten)">α 20d</th>
        <th title="Konv.-gewichtetes Netto-Exposure des Buchs in diesem Sektor (+ long / − short)">Buch</th>
        <th title="Trend-Indikator: ↗ beschleunigt, → stabil, ↘ verliert Tempo (5d-Rate vs 20d-Rate)">Trend</th>
      </tr></thead>
      <tbody>${tbodyHtml}${benchHtml}</tbody>
    </table>
    <div class="sec-rot-foot">Sortiert nach 5d-Performance. Heat-Skalen pro Spalte: 1d ±3% · 5d ±6% · 20d ±12% · 30d ±15%. Trend-Logik vergleicht 5d-Tagesrate (5d/5) mit 20d-Tagesrate (20d/20); ≥1.5× = ↗, ≤0.5× = ↘. Buch-Netto: + long, − short, leer = keine Position. Equal-weight, nicht Marktkap.</div>
  `;
})();

// Valuation-Edge-Scatter (HED-137 Zyklus 99): Bloomberg EQS-style Forward P/E × Revenue-Growth
// positioning map for the in-universe watchlist. Each dot = one ticker, coloured by sector,
// ringed green/red if the book holds an active long/short call. Quadrants split at axis medians:
//   top-left  = high growth, low P/E   → "cheap growth" (attractive)
//   top-right = high growth, high P/E  → "priced-in growth"
//   bot-left  = low growth,  low P/E   → "value-trap"
//   bot-right = low growth,  high P/E  → "expensive low-growth"
// Answers "where on the valuation/growth grid does the book sit?" in one glance — the
// fundamental backdrop next to the technical rotation table.
(function renderValuationScatter(){
  const sv=D.sector_view, root=$("valuationscatter");
  if(!sv || !(sv.sectors||[]).length || !root) return;
  // Build {ticker → sectorId} map and gather (fwdPE, growth) datapoints
  const SEC_COLOR={S1:"#4da3ff",S2:"#3fb950",S3:"#d29922",S4:"#a371f7",S5:"#f78166",S6:"#76e4f7"};
  const SEC_NAME={};
  const pts=[];
  (sv.sectors||[]).forEach(s=>{
    SEC_NAME[s.id]=s.name||s.id;
    (s.tickers||[]).forEach(t=>{
      const c=t.consensus; if(!c) return;
      const px=t.price, eps=c.fwd_eps, g=c.rev_growth_yoy;
      if(!(px>0)||!(eps>0)||g==null||!isFinite(g)) return;
      const pe=px/eps;
      // exclude obviously broken inputs (negative or implausibly extreme)
      if(!isFinite(pe)||pe<=0||pe>200) return;
      pts.push({tk:String(t.ticker||"").toUpperCase(),sec:s.id,pe,g});
    });
  });
  if(pts.length<4) return;  // need enough points to make the scatter meaningful
  // Active book direction map — track-record theses that are not yet closed/scored
  const tr=D.track_record;
  const theses=(tr&&Array.isArray(tr.theses))?tr.theses:[];
  const isClosed=t=>{const v=String(t.verdict||"").toLowerCase();return v==="hit"||v==="miss"||v==="closed"||v==="exit";};
  const bookDir={};
  theses.filter(t=>!isClosed(t)).forEach(t=>{
    const dir=String(t.direction||"").toLowerCase();
    const tk=((t.tickers||[])[0]||"").toUpperCase();
    if(!tk) return;
    const ex=bookDir[tk];
    if(!ex||((t.conviction||0)>(ex.conv||0))) bookDir[tk]={dir,conv:t.conviction||0};
  });
  // Also fall back to latest briefing theses (matches Open Calls / Heatmap behaviour)
  const latestB=D.briefing||{};
  ((latestB.theses||{}).theses||[]).forEach(t=>{
    const dir=String(t.direction||"").toLowerCase();
    (t.tickers||[]).forEach(tk=>{
      const k=String(tk).toUpperCase();
      if(!bookDir[k]||(t.conviction||0)>(bookDir[k].conv||0)) bookDir[k]={dir,conv:t.conviction||0};
    });
  });
  // Axis ranges — clamp outliers but keep grid readable
  const peClamp=v=>Math.max(0,Math.min(v,90));
  const gClamp =v=>Math.max(-20,Math.min(v,120));
  pts.forEach(p=>{p.peC=peClamp(p.pe); p.gC=gClamp(p.g);});
  const peMin=0, peMax=Math.max(60, Math.ceil(Math.max(...pts.map(p=>p.peC))/10)*10);
  const gValsRaw=pts.map(p=>p.gC);
  const gMin=Math.min(-10, Math.floor(Math.min(...gValsRaw)/10)*10);
  const gMax=Math.max(60, Math.ceil(Math.max(...gValsRaw)/10)*10);
  // Medians for quadrant lines
  const sorted=arr=>arr.slice().sort((a,b)=>a-b);
  const median=arr=>{const s=sorted(arr); const n=s.length; return n?(n%2?s[(n-1)/2]:(s[n/2-1]+s[n/2])/2):0;};
  const peMed=median(pts.map(p=>p.peC));
  const gMed =median(pts.map(p=>p.gC));
  // SVG geometry
  const W=800,H=380, mL=46,mR=14,mT=14,mB=42;
  const pw=W-mL-mR, ph=H-mT-mB;
  const xOf=v=>mL+((v-peMin)/(peMax-peMin))*pw;
  const yOf=v=>mT+ph-((v-gMin)/(gMax-gMin))*ph;
  // Grid + axis ticks
  const peTicks=[]; for(let v=10;v<=peMax;v+=10) peTicks.push(v);
  const gTicks=[]; for(let v=Math.ceil(gMin/20)*20; v<=gMax; v+=20) gTicks.push(v);
  const gridLines=[
    ...peTicks.map(v=>`<line class="vs-grid" x1="${xOf(v).toFixed(1)}" y1="${mT}" x2="${xOf(v).toFixed(1)}" y2="${(mT+ph).toFixed(1)}"/>`),
    ...gTicks.map(v=>`<line class="vs-grid" x1="${mL}" y1="${yOf(v).toFixed(1)}" x2="${(mL+pw).toFixed(1)}" y2="${yOf(v).toFixed(1)}"/>`),
  ].join("");
  const tickLabels=[
    ...peTicks.map(v=>`<text x="${xOf(v).toFixed(1)}" y="${(mT+ph+14).toFixed(1)}" text-anchor="middle">${v}×</text>`),
    ...gTicks.map(v=>`<text x="${(mL-6).toFixed(1)}" y="${(yOf(v)+3.5).toFixed(1)}" text-anchor="end">${v>0?"+":""}${v}%</text>`),
  ].join("");
  // Median quadrant lines
  const medX=xOf(peMed).toFixed(1), medY=yOf(gMed).toFixed(1);
  const medLines=`<line class="vs-med" x1="${medX}" y1="${mT}" x2="${medX}" y2="${(mT+ph).toFixed(1)}"/>
                  <line class="vs-med" x1="${mL}" y1="${medY}" x2="${(mL+pw).toFixed(1)}" y2="${medY}"/>`;
  // Quadrant labels (only on non-mobile via CSS)
  const qLabels=`
    <text class="vs-qlbl" x="${(mL+6)}" y="${(mT+14)}" text-anchor="start">Cheap Growth</text>
    <text class="vs-qlbl" x="${(mL+pw-6)}" y="${(mT+14)}" text-anchor="end">Priced-In Growth</text>
    <text class="vs-qlbl" x="${(mL+6)}" y="${(mT+ph-6)}" text-anchor="start">Value-Trap</text>
    <text class="vs-qlbl" x="${(mL+pw-6)}" y="${(mT+ph-6)}" text-anchor="end">Expensive Low-Growth</text>`;
  // Axes
  const axes=`<line class="vs-ax" x1="${mL}" y1="${(mT+ph).toFixed(1)}" x2="${(mL+pw).toFixed(1)}" y2="${(mT+ph).toFixed(1)}"/>
              <line class="vs-ax" x1="${mL}" y1="${mT}" x2="${mL}" y2="${(mT+ph).toFixed(1)}"/>`;
  // Axis titles
  const axTitles=`
    <text class="vs-axlbl" x="${(mL+pw/2).toFixed(1)}" y="${(H-6).toFixed(1)}" text-anchor="middle">Forward P/E</text>
    <text class="vs-axlbl" x="${-((mT+ph/2))}" y="12" text-anchor="middle" transform="rotate(-90)">Rev Growth YoY</text>`;
  // Sort so book dots render on top
  pts.sort((a,b)=>(bookDir[a.tk]?1:0)-(bookDir[b.tk]?1:0));
  const esc=window._esc||(s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])));
  const dots=pts.map(p=>{
    const cx=xOf(p.peC), cy=yOf(p.gC);
    const fill=SEC_COLOR[p.sec]||"#8aa0bd";
    const bk=bookDir[p.tk];
    const cls=["vs-dot"];
    if(bk){ cls.push("vs-book"); cls.push(bk.dir==="short"?"vs-book-short":"vs-book-long"); }
    const r=bk?6.4:4.2;
    const tip=`${p.tk} · ${SEC_NAME[p.sec]||p.sec} · Fwd P/E ${p.pe.toFixed(1)}× · Rev-Growth ${p.g>=0?"+":""}${p.g.toFixed(1)}%${bk?` · ${bk.dir.toUpperCase()}${bk.conv?" "+bk.conv.toFixed(2):""}`:""}`;
    return `<circle class="${cls.join(" ")}" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${r}" fill="${fill}"><title>${esc(tip)}</title></circle>`;
  }).join("");
  // Ticker labels for book positions only (else too cluttered)
  const labels=pts.filter(p=>bookDir[p.tk]).map(p=>{
    const cx=xOf(p.peC), cy=yOf(p.gC);
    // place label to the right by default; flip if too close to right edge
    const flip=cx>mL+pw-46;
    const tx=flip?cx-9:cx+9;
    const anchor=flip?"end":"start";
    const w=p.tk.length*5.6+4;
    const bx=flip?tx-w:tx-2;
    return `<rect class="vs-tklbl-bg" x="${bx.toFixed(1)}" y="${(cy-7).toFixed(1)}" width="${w.toFixed(1)}" height="13" rx="2"/>
            <text class="vs-tklbl" x="${tx.toFixed(1)}" y="${(cy+3.5).toFixed(1)}" text-anchor="${anchor}">${esc(p.tk)}</text>`;
  }).join("");
  // Legend — sector swatches + book ring explainer
  const secsUsed=Array.from(new Set(pts.map(p=>p.sec))).sort();
  const legend=`<div class="vs-legend">${secsUsed.map(sid=>{
    const col=SEC_COLOR[sid]||"#8aa0bd";
    return `<span class="vs-leg-item"><span class="vs-leg-sw" style="background:${col}"></span>${esc(sid)} · ${esc(SEC_NAME[sid]||sid)}</span>`;
  }).join("")}<span class="vs-leg-item"><span class="vs-leg-ring" style="border-color:var(--green)"></span>Buch LONG</span><span class="vs-leg-item"><span class="vs-leg-ring" style="border-color:var(--red)"></span>Buch SHORT</span></div>`;
  // Stats callout: where does the book sit? share of long names in cheap-growth quadrant
  const bookPts=pts.filter(p=>bookDir[p.tk]);
  let calloutHtml="";
  if(bookPts.length){
    const longPts=bookPts.filter(p=>bookDir[p.tk].dir!=="short");
    const cgLong=longPts.filter(p=>p.peC<=peMed && p.gC>=gMed).length;
    const pricedIn=longPts.filter(p=>p.peC>peMed && p.gC>=gMed).length;
    const avgPE = bookPts.reduce((s,p)=>s+p.peC,0)/bookPts.length;
    const avgG  = bookPts.reduce((s,p)=>s+p.gC,0)/bookPts.length;
    const univAvgPE = pts.reduce((s,p)=>s+p.peC,0)/pts.length;
    const univAvgG  = pts.reduce((s,p)=>s+p.gC,0)/pts.length;
    const pePremium=((avgPE-univAvgPE)/Math.max(univAvgPE,1e-6))*100;
    const gPremium = avgG-univAvgG;
    calloutHtml=`<div class="vs-callout">Buch: <b>${bookPts.length}</b> Position${bookPts.length===1?"":"en"} · Ø Fwd P/E <b>${avgPE.toFixed(1)}×</b> (Universum ${univAvgPE.toFixed(1)}×, ${pePremium>=0?"+":""}${pePremium.toFixed(0)}%) · Ø Growth <b>${avgG>=0?"+":""}${avgG.toFixed(1)}%</b> (Universum ${univAvgG>=0?"+":""}${univAvgG.toFixed(1)}%, ${gPremium>=0?"+":""}${gPremium.toFixed(1)}pp)${longPts.length?` · davon <b>${cgLong}</b> in Cheap-Growth, <b>${pricedIn}</b> in Priced-In-Growth`:""}</div>`;
  } else {
    calloutHtml=`<div class="vs-callout"><b>${pts.length}</b> Ticker mit Konsens-EPS · Median Fwd P/E <b>${peMed.toFixed(1)}×</b> · Median Growth <b>${gMed>=0?"+":""}${gMed.toFixed(1)}%</b></div>`;
  }
  root.hidden=false;
  root.innerHTML = `
    <div class="vs-h">
      <div>
        <div class="vs-title">Valuation × Growth — Edge-Map</div>
        <div class="vs-sub">Forward P/E (= Preis ÷ Konsens-EPS) gegen Umsatzwachstum YoY für die in-Universe Watchlist. Quadranten teilen sich an den Median-Achsen — links-oben = günstiges Wachstum, rechts-unten = teuer ohne Wachstum. Buch-Positionen sind grün/rot beringt.</div>
      </div>
      ${calloutHtml}
    </div>
    <svg class="vs-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Valuation-Edge-Scatter — Forward P/E gegen Umsatzwachstum, ${pts.length} Ticker">
      ${gridLines}
      ${medLines}
      ${qLabels}
      ${axes}
      ${tickLabels}
      ${axTitles}
      ${dots}
      ${labels}
    </svg>
    ${legend}
    <div class="vs-foot">Forward P/E = aktueller Preis ÷ Konsens-Forward-EPS (Yahoo). Growth = trailing Umsatzwachstum YoY. Ticker ohne EPS-Schätzung oder mit negativem Wachstum unter −20% sind nicht eingezeichnet. Median-Linien dienen als Quadranten-Trenner — sie verschieben sich mit dem Universum, nicht absolut.</div>`;
})();

// Sektor-Heatmap (HED-137 Zyklus 93): Bloomberg-/Finviz-style scan of the AI/Tech
// universe. Cells per ticker colored by intraday move, grouped by sector, with
// active-book accent. A PM-grade single-glance read on where the action is —
// runs above the detailed sector tiles which keep the per-ticker drill-down.
(function renderHeatmap(){
  const sv=D.sector_view, root=$("sectorheatmap");
  if(!sv || !(sv.sectors||[]).length || !root) return;
  // Active-book map (ticker → {dir, conv}) from the latest briefing theses
  const book={};
  const latestB=D.briefing||{};
  ((latestB.theses||{}).theses||[]).forEach(t=>{
    const dir=(t.direction||"").toLowerCase();
    (t.tickers||[]).forEach(tk=>{
      const k=String(tk||"").toUpperCase(); if(!k) return;
      const ex=book[k];
      if(!ex||(t.conviction!=null&&(ex.conv==null||t.conviction>ex.conv)))
        book[k]={dir,conv:t.conviction,label:t.label||""};
    });
  });
  // 9-step color tier from change % — symmetric red/green diverging scale, ±5% terminal
  function tier(c){
    if(c==null) return "na";
    const a=Math.abs(c);
    if(a<0.3) return "z";
    if(c>0)  return a>=5?"pp3":a>=3?"pp2":a>=1?"pp1":"pp0";
    return            a>=5?"nn3":a>=3?"nn2":a>=1?"nn1":"nn0";
  }
  const allTks=sv.sectors.flatMap(s=>s.tickers||[]);
  const anyPriced=allTks.some(t=>t.change_pct!=null);
  if(!anyPriced){ root.hidden=true; return; }
  root.hidden=false; root.setAttribute("aria-busy","false");
  // Universe stats for the header subtitle
  const moves=allTks.map(t=>t.change_pct).filter(v=>v!=null);
  const uniAvg=moves.length?moves.reduce((a,b)=>a+b,0)/moves.length:null;
  const upN=moves.filter(v=>v>=0.3).length, dnN=moves.filter(v=>v<=-0.3).length;
  const breadth=moves.length?`${upN} ↑ · ${dnN} ↓ · ${moves.length-upN-dnN} flat`:"";
  const avgTxt=uniAvg!=null?`${uniAvg>=0?"+":"−"}${Math.abs(uniAvg).toFixed(2)}%`:"—";
  const avgCls=uniAvg==null?"muted":uniAvg>=0.3?"move-up":uniAvg<=-0.3?"move-dn":"muted";
  // Sector order: priced sectors only, hottest |avg move| first (Information Scent, Von Restorff)
  const sorted=sv.sectors.slice().filter(s=>(s.tickers||[]).length>0).map(s=>{
    const ms=(s.tickers||[]).map(t=>t.change_pct).filter(v=>v!=null);
    const a=ms.length?ms.reduce((x,y)=>x+y,0)/ms.length:null;
    return {s,avg:a};
  }).sort((x,y)=>{
    const ax=x.avg==null?-1:Math.abs(x.avg), ay=y.avg==null?-1:Math.abs(y.avg);
    return ay-ax || String(x.s.id).localeCompare(String(y.s.id),undefined,{numeric:true});
  });
  function cellHtml(t){
    const tk=String(t.ticker||"").toUpperCase();
    const c=t.change_pct;
    const cls=`hmap-c-${tier(c)}`;
    const px=t.price!=null?(t.price<10?t.price.toFixed(2):t.price<100?t.price.toFixed(1):t.price.toFixed(0)):"—";
    const chTxt=c!=null?`${c>=0?"+":"−"}${Math.abs(c).toFixed(1)}%`:"n/a";
    const b=book[tk];
    const bookCls=b?` in-book book-${b.dir==="short"?"short":"long"}`:"";
    const tag=b?`<span class="book-tag" aria-label="aktiver Call ${b.dir.toUpperCase()}">${b.dir.toUpperCase().slice(0,1)}</span>`:"";
    const tipParts=[
      `${tk} — $${px}`,
      c!=null?`Tagesveränd. ${chTxt}`:null,
      t.pct_of_52w_high!=null?`${t.pct_of_52w_high.toFixed(0)}% vom 52W-Hoch`:null,
      t.rsi14!=null?`RSI14 ${t.rsi14}`:null,
      b?`Aktiver Call: ${b.dir.toUpperCase()}${b.conv!=null?` · Conv ${b.conv.toFixed(2)}`:""}${b.label?` · "${b.label}"`:""}`:null
    ].filter(Boolean).join(" · ");
    const url=`https://finance.yahoo.com/quote/${encodeURIComponent(t.ticker)}`;
    return `<a class="sec-hmap-cell ${cls}${bookCls}" href="${url}" target="_blank" rel="noopener" `
      +`title="${esc(tipParts)}" aria-label="${esc(tipParts)}">${tag}`
      +`<span class="tk">${esc(tk)}</span><span class="ch">${esc(chTxt)}</span></a>`;
  }
  const groupsHtml=sorted.map(({s,avg})=>{
    const cells=(s.tickers||[])
      .slice()
      .sort((a,b)=>{const ma=a.change_pct!=null?a.change_pct:-1e9, mb=b.change_pct!=null?b.change_pct:-1e9; return mb-ma;})
      .map(cellHtml).join("");
    const avgTxt=avg!=null?`${avg>=0?"+":"−"}${Math.abs(avg).toFixed(1)}%`:"—";
    const avgCls=avg==null?"muted":avg>=0.3?"move-up":avg<=-0.3?"move-dn":"muted";
    return `<div class="sec-hmap-grp"><div class="sec-hmap-grp-h">`
      +`<span class="id">${esc(s.id)}</span><span class="nm">${esc(s.name)}</span>`
      +`<span class="avg ${avgCls}">${esc(avgTxt)}</span></div>`
      +`<div class="sec-hmap-cells">${cells}</div></div>`;
  }).join("");
  const legend=`<div class="sec-hmap-legend" aria-hidden="true">
    <span>≤−5%</span>
    <span class="sec-hmap-legend-sw hmap-c-nn3"></span>
    <span class="sec-hmap-legend-sw hmap-c-nn2"></span>
    <span class="sec-hmap-legend-sw hmap-c-nn1"></span>
    <span class="sec-hmap-legend-sw hmap-c-nn0"></span>
    <span class="sec-hmap-legend-sw hmap-c-z"></span>
    <span class="sec-hmap-legend-sw hmap-c-pp0"></span>
    <span class="sec-hmap-legend-sw hmap-c-pp1"></span>
    <span class="sec-hmap-legend-sw hmap-c-pp2"></span>
    <span class="sec-hmap-legend-sw hmap-c-pp3"></span>
    <span>≥+5%</span></div>`;
  root.innerHTML=`<div class="sec-hmap-h">
      <div><div class="sec-hmap-h-title">Universum-Heatmap — Tagesbewegung</div>
        <div class="sec-hmap-h-sub">Ø Universum <b class="${avgCls}">${esc(avgTxt)}</b> · ${esc(breadth)} · Kachel mit Akzent = aktiver Call (L/S)</div></div>
      ${legend}
    </div>
    <div class="sec-hmap-groups" role="group" aria-label="Sektor-Heatmap der AI/Tech-Universum-Ticker">${groupsHtml}</div>`;
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

// Insider-Tape (HED-137 Zyklus 103): Bloomberg-INSI-style smart-money panel.
// Rolls up the last 30 days of SEC Form-4 OPEN-MARKET buys/sells per ticker into one
// diverging $-bar (sells left/red, buys right/green) sized against the panel's max gross.
// Tickers we hold an active call on get a ★ overlay so conflicting/confirming insider
// flow is preattentive — e.g. "PLTR long but $128M of insider selling across 7 execs".
// Edge-Artikulation per STRATEGY.md: smart money is one of the cleanest non-price signals.
(function renderInsiderTape(){
  const root=$("insidertape");
  if(!root) return;
  const tape=D.insider_tape||{};
  const rows=tape.tickers||[];
  if(!rows.length){
    root.innerHTML='<div class="panel it-panel"><div class="it-empty">Keine Form-4 Open-Market-Aktivität in den letzten '+(tape.lookback_days||30)+'d — die EDGAR-Pipeline ist still oder das Universum schweigt.</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }
  // Active-call ticker set (for ★ overlay) and direction (for tooltip)
  const activeMap={};
  ((D.track_record||{}).theses||[])
    .filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date))
    .forEach(t=>{(t.tickers||[]).forEach(tk=>{activeMap[String(tk).toUpperCase()]=(t.direction||"").toLowerCase();});});
  // Scale: gross $ on the heaviest ticker — both sides scaled against the same anchor
  const maxGross=Math.max(...rows.map(r=>Math.max(r.buy_dollar,r.sell_dollar)),1);
  const fmt$=(v)=>{
    const a=Math.abs(v);
    if(a>=1e6) return "$"+(v/1e6).toFixed(a>=10e6?0:1)+"M";
    if(a>=1e3) return "$"+(v/1e3).toFixed(0)+"K";
    return "$"+Math.round(v);
  };
  const totalSell=rows.reduce((s,r)=>s+r.sell_dollar,0);
  const totalBuy=rows.reduce((s,r)=>s+r.buy_dollar,0);
  const totalNet=totalBuy-totalSell;
  const netSign=totalNet>=0?"pos":"neg";
  const netSym=totalNet>=0?"+":"−";
  const skew=(totalSell+totalBuy)>0?Math.round(totalSell/(totalSell+totalBuy)*100):0;
  const skewLabel=skew>=80?"Verkäufer-dominant":skew>=60?"verkaufslastig":skew>=40?"balanciert":skew>=20?"kauflastig":"Käufer-dominant";
  const headerKpi=`
    <div class="it-metrics">
      <div class="it-metric" title="Anzahl Ticker mit qualifizierender Form-4 Open-Market-Aktivität in den letzten ${tape.lookback_days}d (sortiert nach |Netto-$| desc, Top ${rows.length})">
        <span class="lbl">Ticker</span><span class="val">${rows.length}</span>
      </div>
      <div class="it-metric" title="Brutto-Käufe (OPEN-MARKET BUY, Code P) aller Form-4-Filings im Fenster">
        <span class="lbl">Käufe</span><span class="val pos">${fmt$(totalBuy)}</span>
      </div>
      <div class="it-metric" title="Brutto-Verkäufe (OPEN-MARKET SALE, Code S) aller Form-4-Filings im Fenster">
        <span class="lbl">Verkäufe</span><span class="val neg">${fmt$(totalSell)}</span>
      </div>
      <div class="it-metric" title="Netto-Aktivität: Käufe − Verkäufe · ${skewLabel} (${skew}% Verkäufer-Anteil am Brutto)">
        <span class="lbl">Netto</span><span class="val ${netSign}">${netSym}${fmt$(Math.abs(totalNet))}</span>
      </div>
    </div>`;
  function _rowHtml(r){
    const tk=r.ticker;
    const isActive=Object.prototype.hasOwnProperty.call(activeMap,tk);
    const dir=isActive?activeMap[tk]:"";
    const star=isActive
      ?`<span class="it-tk-star" title="Aktiver ${esc(dir||"call")}-Call im Buch — Insider-Flow ${r.net_dollar>0?"bestätigt":"widerspricht"} die These" aria-label="Aktiver Call">★</span>`
      :"";
    const sellW=Math.min(50,r.sell_dollar/maxGross*50);
    const buyW=Math.min(50,r.buy_dollar/maxGross*50);
    // Inline labels only if the bar is wide enough to host them; otherwise the right-column $-net carries the value
    const sellLbl=sellW>=14?`<span class="it-bar-lbl sell">${esc(fmt$(r.sell_dollar))}</span>`:"";
    const buyLbl=buyW>=14?`<span class="it-bar-lbl buy">${esc(fmt$(r.buy_dollar))}</span>`:"";
    const showEmpty=(r.sell_dollar+r.buy_dollar)<1;
    const netCls=r.net_dollar>0?"pos":r.net_dollar<0?"neg":"";
    const netSym2=r.net_dollar>0?"+":r.net_dollar<0?"−":"";
    const execLbl=`${r.n_buy_execs} Käufer · ${r.n_sell_execs} Verkäufer`;
    const top=(r.top_actors||[]).map(a=>{
      const sideTag=a.side==="buy"?"BUY":a.side==="sell"?"SELL":"MIX";
      return `${sideTag} ${fmt$(a.dollars)} — ${a.person}${a.role?" ("+a.role+")":""}`;
    }).join("\n");
    const tip=`${tk} ${r.company}\n` +
              `Käufe ${fmt$(r.buy_dollar)} (${r.n_buy_execs} Execs, ${r.n_buy_filings} Filings)\n` +
              `Verkäufe ${fmt$(r.sell_dollar)} (${r.n_sell_execs} Execs, ${r.n_sell_filings} Filings)\n` +
              `Netto ${netSym2}${fmt$(Math.abs(r.net_dollar))} · letzte Aktivität ${r.last_date}` +
              (top?`\n\n${top}`:"");
    const edgarUrl=`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${encodeURIComponent(tk)}&type=4`;
    return `<a class="it-row" href="${esc(edgarUrl)}" target="_blank" rel="noopener" title="${esc(tip)}" aria-label="${esc(tk+" Insider-Flow netto "+netSym2+fmt$(Math.abs(r.net_dollar)))}">
      <div class="it-tk">
        <div class="it-tk-row"><span class="it-tk-sym">${esc(tk)}</span>${star}</div>
        <div class="it-tk-meta">${esc(r.company||"")}</div>
      </div>
      <div class="it-bar-wrap" role="img" aria-label="Käufe ${esc(fmt$(r.buy_dollar))}, Verkäufe ${esc(fmt$(r.sell_dollar))}">
        <div class="it-bar-axis"></div>
        ${r.sell_dollar>0?`<div class="it-bar-sell" style="width:${sellW.toFixed(2)}%"></div>${sellLbl}`:""}
        ${r.buy_dollar>0?`<div class="it-bar-buy" style="width:${buyW.toFixed(2)}%"></div>${buyLbl}`:""}
        ${showEmpty?'<span class="it-bar-empty">keine $-Aktivität</span>':""}
      </div>
      <div class="it-net ${netCls}">${netSym2}${esc(fmt$(Math.abs(r.net_dollar)))}<span class="it-net-sub">${esc(execLbl)}</span></div>
    </a>`;
  }
  const html=`<div class="panel it-panel">
    <div class="it-h">
      <div>
        <div class="it-h-title">Insider-Tape — Smart-Money-Flow</div>
        <div class="it-h-sub">SEC Form-4 Open-Market-Käufe/Verkäufe · letzte ${tape.lookback_days}d · sortiert nach |Netto-$| · ★ = aktiver Call</div>
      </div>
      ${headerKpi}
    </div>
    <div class="it-rows" role="list">
      ${rows.map(_rowHtml).join("")}
    </div>
    <div class="it-foot">
      Insider mit Pflicht-Disclosure (Officers, Directors, ≥10%-Owner) signalisieren mit Open-Market-Käufen (Code P) Überzeugung und mit Open-Market-Verkäufen (Code S) Reduktion. Routine-Vesting, Optionsausübungen und Steuer-Withholding sind ausgeklammert. Ein ★-Ticker mit gegen-direktionalem Insider-Flow ist eine offene Risikoflanke — prüfe, ob die These das antizipiert. Quelle: <a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&amp;type=4" target="_blank" rel="noopener">SEC EDGAR Form 4</a> · ${tape.rows_parsed||0} Filings im Fenster geparsed.
    </div>
  </div>`;
  root.innerHTML=html;
  root.setAttribute("aria-busy","false");
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

// Earnings-Playbook (HED-137 Zyklus 104 — Bloomberg ERN-screen equivalent).
// Per-ticker beat-profile: last 4 EPS surprises as a sequence bar (oldest left →
// newest right, green = beat / red = miss, saturation = magnitude), average
// 1-day post-earnings reaction, and sign-match (does surprise direction translate
// to stock direction, or is it already priced in?). Sorted so upcoming earnings
// surface at the top — directly actionable for the Katalysator-Runway above.
(function renderEarnPlay(){
  const root=$("earnplay"); if(!root) return;
  const sv=D.sector_view||{}, tr=D.track_record||{};
  // Held tickers (open calls) — for ★ overlay + direction tag
  const heldMap={};
  ((tr.theses)||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date)).forEach(t=>{
    (t.tickers||[]).forEach(tk=>{ const k=String(tk).toUpperCase();
      if(!heldMap[k]) heldMap[k]={direction:(t.direction||"").toLowerCase(),conv:t.conviction||0,label:t.label||""}; });
  });
  // Upcoming earnings date map: ticker → days_out
  const upMap={};
  ((sv.earnings_calendar)||[]).forEach(e=>{
    const tk=String(e.ticker||"").toUpperCase();
    if(tk&&!(tk in upMap)) upMap[tk]={date:e.date,daysOut:e.days_out};
  });
  // Flatten ticker rows from sectors
  const rows=[];
  ((sv.sectors)||[]).forEach(sec=>{
    (sec.tickers||[]).forEach(t=>{
      if(!t||!t.ticker) return;
      const eh=t.earnings_history; if(!eh||!eh.quarters||!eh.quarters.length) return;
      const TK=String(t.ticker).toUpperCase();
      rows.push({tk:TK,sector:sec.id,sectorName:sec.name,price:t.price,eh,held:heldMap[TK]||null,
        next:upMap[TK]||null});
    });
  });
  if(!rows.length){
    root.innerHTML='<div class="panel ep-panel"><div class="ep-empty">Keine Earnings-Historie verfügbar — sector_view muss mit --gen-sector-view aktualisiert werden.</div></div>';
    root.setAttribute("aria-busy","false"); return;
  }
  // Sort: 1) upcoming earnings within 30d ascending, 2) then held positions by beat_pct desc,
  // 3) then everything else by beat_pct desc. Surfaces the most actionable name first.
  rows.sort((a,b)=>{
    const ad=a.next&&a.next.daysOut!=null&&a.next.daysOut<=30?a.next.daysOut:999;
    const bd=b.next&&b.next.daysOut!=null&&b.next.daysOut<=30?b.next.daysOut:999;
    if(ad!==bd) return ad-bd;
    const ah=a.held?1:0,bh=b.held?1:0;
    if(ah!==bh) return bh-ah;
    return (b.eh.beat_pct||0)-(a.eh.beat_pct||0);
  });
  // Aggregate KPIs (the "what is the book's print history?" answer)
  const heldRows=rows.filter(r=>r.held);
  let bookBeatN=0,bookBeatTot=0,bookSurpSum=0,bookSurpN=0,bookReactSum=0,bookReactN=0;
  heldRows.forEach(r=>{
    bookBeatN+=r.eh.beat_n||0; bookBeatTot+=r.eh.beat_total||0;
    if(r.eh.avg_surprise_pct!=null){ bookSurpSum+=r.eh.avg_surprise_pct; bookSurpN++; }
    if(r.eh.avg_reaction_1d_pct!=null){ bookReactSum+=r.eh.avg_reaction_1d_pct; bookReactN++; }
  });
  const bookBeatPct=bookBeatTot?Math.round(bookBeatN/bookBeatTot*100):null;
  const bookAvgSurp=bookSurpN?(bookSurpSum/bookSurpN):null;
  const bookAvgReact=bookReactN?(bookReactSum/bookReactN):null;
  // KPI rendering helpers
  const pct=(v,d=1)=>v==null?"—":(v>=0?"+":"")+v.toFixed(d)+"%";
  const pctCls=v=>v==null?"":v>0.05?"move-up":v<-0.05?"move-dn":"muted";
  // Surprise → cell class & label
  function cellCls(s){
    if(s==null) return "empty";
    if(s>=10) return "beat-strong"; if(s>=3) return "beat"; if(s>0) return "beat-weak";
    if(s<=-10) return "miss-strong"; if(s<=-3) return "miss"; return "miss-weak";
  }
  // Build sequence cells: pad to 4 from the LEFT with empty cells if fewer quarters
  function seqHtml(quarters){
    const PAD=4;
    const qs=quarters.slice(0,PAD); // already newest first from yfinance
    const ordered=qs.slice().reverse(); // render oldest → newest
    const empties=Math.max(0,PAD-ordered.length);
    const out=[];
    for(let i=0;i<empties;i++) out.push('<span class="ep-cell empty" aria-hidden="true">·</span>');
    ordered.forEach(q=>{
      const s=q.surprise_pct;
      const cls=cellCls(s);
      const lab=s==null?"·":(s>=0?"+":"")+s.toFixed(s>=10?0:1);
      const tip=`${q.date} · Surprise ${lab}%`+(q.reaction_1d_pct!=null?` · 1d-Reaktion ${q.reaction_1d_pct>=0?"+":""}${q.reaction_1d_pct.toFixed(1)}%`:"")+(q.eps_actual!=null&&q.eps_est!=null?` · EPS $${q.eps_actual} vs Cons $${q.eps_est}`:"");
      out.push(`<span class="ep-cell ${cls}" title="${esc(tip)}">${esc(lab)}</span>`);
    });
    return `<span class="ep-seq" aria-label="Letzte 4 EPS-Surprises, links alt → rechts neu">${out.join("")}</span>`;
  }
  function nextCellHtml(next){
    if(!next||next.daysOut==null) return '<span class="ep-next"><span class="dash">—</span></span>';
    const d=next.daysOut;
    const cls=d<=7?"imminent":d<=21?"soon":"";
    const m=String(next.date||"").match(/^(\d{4})-(\d{2})-(\d{2})/);
    const dd=m?`${m[3]}.${m[2]}`:String(next.date||"");
    return `<span class="ep-next ${cls}"><span class="when">${esc(dd)}</span><span class="out">+${d}d</span></span>`;
  }
  function reactCellHtml(eh){
    if(eh.avg_reaction_1d_pct==null) return '<span class="muted">—</span>';
    const v=eh.avg_reaction_1d_pct, cls=pctCls(v);
    const std=eh.std_reaction_1d_pct;
    return `<span class="ep-react-cell"><span class="${cls}" style="font-weight:700">${pct(v,1)}</span>${std!=null?`<span class="ep-std">σ ${std.toFixed(1)}%</span>`:""}</span>`;
  }
  function signCellHtml(eh){
    if(!eh.sign_total) return '<span class="muted">—</span>';
    const ratio=eh.sign_hits/eh.sign_total;
    return `<span class="ep-sign-bar" title="Wie oft Stock-Reaktion mit Surprise-Vorzeichen übereinstimmt"><span class="ep-sign-bar-f" style="width:${(ratio*100).toFixed(0)}%"></span></span><span class="ep-sign-txt">${eh.sign_hits}/${eh.sign_total}</span>`;
  }
  const tbody=rows.map(r=>{
    const eh=r.eh;
    const beatTxt=eh.beat_total?`<span class="nbeat">${eh.beat_n}</span><span class="ntot">/${eh.beat_total}</span>`:'<span class="muted">—</span>';
    const beatPct=eh.beat_pct!=null?`<span class="muted" style="font-weight:500;margin-left:4px">${eh.beat_pct}%</span>`:"";
    const heldHtml=r.held?`<span class="ep-tk-star" title="Im Buch — ${esc(r.held.label)}">★</span><span class="ep-tk-dir ${r.held.direction==="long"?"long":r.held.direction==="short"?"short":""}">${esc(r.held.direction||"")}</span>`:"";
    const surpCls=pctCls(eh.avg_surprise_pct);
    return `<tr class="${r.held?"is-held":""}">
      <td class="l"><div class="ep-tk"><div class="ep-tk-row"><span class="ep-tk-sym">${esc(r.tk)}</span>${heldHtml}</div><span class="ep-tk-meta">${esc(r.sectorName||"")}</span></div></td>
      <td class="c">${seqHtml(eh.quarters||[])}</td>
      <td><span class="ep-beat">${beatTxt}</span>${beatPct}</td>
      <td class="${surpCls}" style="font-weight:700">${pct(eh.avg_surprise_pct,1)}</td>
      <td>${reactCellHtml(eh)}</td>
      <td class="col-hide-m">${signCellHtml(eh)}</td>
      <td>${nextCellHtml(r.next)}</td>
    </tr>`;
  }).join("");
  const bookBeatTxt=bookBeatPct!=null?`${bookBeatN}/${bookBeatTot} · ${bookBeatPct}%`:"—";
  const bookSurpTxt=bookAvgSurp!=null?(bookAvgSurp>=0?"+":"")+bookAvgSurp.toFixed(1)+"%":"—";
  const bookReactTxt=bookAvgReact!=null?(bookAvgReact>=0?"+":"")+bookAvgReact.toFixed(1)+"%":"—";
  const surpCls=bookAvgSurp==null?"":bookAvgSurp>0.1?"move-up":bookAvgSurp<-0.1?"move-dn":"muted";
  const reactCls=bookAvgReact==null?"":bookAvgReact>0.1?"move-up":bookAvgReact<-0.1?"move-dn":"muted";
  root.innerHTML=`<div class="panel ep-panel">
    <div class="ep-h">
      <div>
        <div class="ep-h-title">Earnings-Playbook · Beat-Profil pro Ticker</div>
        <div class="ep-h-sub">Letzte 4 EPS-Reports — Surprise %, 1-Tages-Stock-Reaktion, Sign-Match. Sortiert nach nächstem Earnings-Termin. Buchpositionen mit ★.</div>
      </div>
      <div class="ep-metrics" aria-label="Aggregat-Kennzahlen offener Positionen">
        <div class="ep-metric" title="Beat-Rate aller Buchpositionen über die letzten 4 Quartale"><span class="lbl">Buch Beat</span><span class="val">${esc(bookBeatTxt)}</span></div>
        <div class="ep-metric" title="Durchschnittliche EPS-Surprise % über alle offenen Calls (Avg-of-Avg)"><span class="lbl">Ø Surprise</span><span class="val ${surpCls}">${esc(bookSurpTxt)}</span></div>
        <div class="ep-metric" title="Durchschnittliche 1-Tages-Stock-Reaktion auf Earnings-Print (Avg-of-Avg)"><span class="lbl">Ø 1d Reaktion</span><span class="val ${reactCls}">${esc(bookReactTxt)}</span></div>
        <div class="ep-metric" title="Anzahl Ticker mit Earnings im 30-Tage-Fenster"><span class="lbl">≤30d Prints</span><span class="val">${rows.filter(r=>r.next&&r.next.daysOut!=null&&r.next.daysOut<=30).length}</span></div>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table class="ep-tbl" aria-label="Earnings Beat-Profil pro Ticker">
        <thead><tr>
          <th class="l">Ticker</th>
          <th class="c">Surprise-Sequenz (alt → neu)</th>
          <th>Beat</th>
          <th>Ø Surprise</th>
          <th>Ø 1d Reaktion</th>
          <th class="col-hide-m">Sign-Match</th>
          <th>Nächster</th>
        </tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>
    <div class="ep-foot">
      <b>Lesart:</b> Surprise-Sequenz zeigt die letzten 4 EPS-Quartale (links alt → rechts neu). Grün = Beat, Rot = Miss, Sättigung = Magnitude (|surprise|≥10% kräftig, ≥3% mittel, sonst schwach). <b>Sign-Match</b> = wie oft die 1-Tages-Stock-Reaktion das Vorzeichen der Surprise teilt. Niedrige Sign-Match-Rate trotz hoher Beat-Rate = <i>bereits eingepreist</i> (Markt erwartet Beats). Hohe Sign-Match-Rate = der Print bewegt die Tape; das ist die einzige Konstellation, in der ein direktionaler Earnings-Trade rationale Edge hat. Quelle: yfinance earnings_dates + 1d Close-zu-Close um den Report-Termin.
    </div>
  </div>`;
  root.setAttribute("aria-busy","false");
})();

// Universum Ideen-Scanner (HED-137 Zyklus 102 — Bloomberg EQSCRN-Stil).
// Screens ALL universe tickers NOT already in an active open call for long-side
// opportunity quality using four independently-scored factors:
//   Analyst consensus rec   → +2 strong_buy / +1 buy / 0 hold / −1 sell
//   RSI14 momentum          → +2 deeply oversold (<35) / +1 neutral-low (35-50) / 0 (50-65) / −1 overbought (>65)
//   Trend (vs MA30)         → +1 above MA >2% / 0 near MA / −1 below MA
//   Cycle position (52w %)  → +1 below 65% (runway) / 0 (65-85%) / −1 above 85% (near peak)
// Max score = 6 (strong_buy + deeply oversold + strong trend + plenty of runway).
// Presents top 8 ranked ideas with visual score-bar, signal chips, and comparative columns.
// Answers: "What should we look at next given today's data?"
(function(){
  const root=$("universe-scanner"); if(!root) return;
  const sv=D.sector_view; if(!sv) return;
  const tr=D.track_record;
  const active=(tr&&tr.theses?tr.theses.filter(t=>t.verdict!=="win"&&t.verdict!=="loss"):[])||[];
  const openTickers=new Set();
  active.forEach(t=>(t.tickers||[]).forEach(tk=>openTickers.add(String(tk).toUpperCase())));
  const candidates=[];
  (sv.sectors||[]).forEach(sec=>{
    (sec.tickers||[]).forEach(t=>{
      if(!t||!t.ticker) return;
      const TK=String(t.ticker).toUpperCase();
      const isOpen=openTickers.has(TK);
      let score=0; const signals=[];
      // Analyst rec
      const rec=(t.consensus&&t.consensus.rec)||"";
      if(rec==="strong_buy"){ score+=2; signals.push({k:"Rec",v:"Strong Buy",cls:"pos"}); }
      else if(rec==="buy"){ score+=1; signals.push({k:"Rec",v:"Buy",cls:"pos"}); }
      else if(rec==="hold"){ signals.push({k:"Rec",v:"Hold",cls:"neu"}); }
      else if(rec==="sell"||rec==="strong_sell"){ score-=1; signals.push({k:"Rec",v:"Sell",cls:"neg"}); }
      // RSI14
      const rsi=t.rsi14;
      if(rsi!=null){
        if(rsi<35){ score+=2; signals.push({k:"RSI",v:rsi.toFixed(0),cls:"pos",note:"Überverkauft"}); }
        else if(rsi<50){ score+=1; signals.push({k:"RSI",v:rsi.toFixed(0),cls:"pos"}); }
        else if(rsi<=65){ signals.push({k:"RSI",v:rsi.toFixed(0),cls:"neu"}); }
        else { score-=1; signals.push({k:"RSI",v:rsi.toFixed(0),cls:"neg",note:"Überkauft"}); }
      }
      // Trend vs MA30
      const ma=t.pct_vs_ma30;
      if(ma!=null){
        if(ma>2){ score+=1; signals.push({k:"Trend",v:"+"+(ma).toFixed(1)+"%",cls:"pos"}); }
        else if(ma>=-2){ signals.push({k:"Trend",v:(ma>=0?"+"+(ma).toFixed(1):ma.toFixed(1))+"%",cls:"neu"}); }
        else { signals.push({k:"Trend",v:(ma).toFixed(1)+"%",cls:"neg"}); }
      }
      // Cycle position
      const w52=t.pct_of_52w_high;
      if(w52!=null){
        if(w52<=65){ score+=1; signals.push({k:"52w",v:w52.toFixed(0)+"%",cls:"pos",note:"Runway"}); }
        else if(w52<=85){ signals.push({k:"52w",v:w52.toFixed(0)+"%",cls:"neu"}); }
        else { score-=1; signals.push({k:"52w",v:w52.toFixed(0)+"%",cls:"neg",note:"Nahe Hoch"}); }
      }
      candidates.push({t, TK, sec:sec.name, score, signals, rec, isOpen, rsi, ma, w52});
    });
  });
  // Sort non-open candidates by score desc, then by RSI asc (lower RSI = better entry within score tier)
  const nonOpen=candidates.filter(c=>!c.isOpen).sort((a,b)=>
    (b.score-a.score)||((a.rsi==null?100:a.rsi)-(b.rsi==null?100:b.rsi)));
  const openCands=candidates.filter(c=>c.isOpen).sort((a,b)=>b.score-a.score);
  const top8=nonOpen.slice(0,8);
  if(!top8.length){
    root.innerHTML='<div class="panel us-panel"><div class="us-empty">Keine Screening-Daten verfügbar.</div></div>';
    return;
  }
  const _recChip=rec=>{
    if(rec==="strong_buy") return '<span class="us-rec us-rec-sb">Strong Buy</span>';
    if(rec==="buy") return '<span class="us-rec us-rec-b">Buy</span>';
    if(rec==="hold") return '<span class="us-rec us-rec-h">Hold</span>';
    if(rec==="sell"||rec==="strong_sell") return '<span class="us-rec us-rec-s">Sell</span>';
    return '<span class="us-rec us-rec-h">—</span>';
  };
  const _rsiCell=(rsi)=>{
    if(rsi==null) return '<span class="muted">—</span>';
    const v=Math.max(0,Math.min(100,rsi));
    const cls=rsi<35?"move-up":rsi>65?"move-dn":"muted";
    return `<div class="us-rsi"><span class="${cls}">${rsi.toFixed(0)}</span><div class="us-rsi-bar"><span class="us-rsi-mark" style="left:calc(${v.toFixed(1)}% - 1px)"></span></div></div>`;
  };
  const _maCell=(ma)=>{
    if(ma==null) return '<span class="muted">—</span>';
    const cls=ma>0?"move-up":"move-dn";
    const arrow=ma>=0?"▲":"▼";
    return `<span class="us-ma ${cls}">${arrow} ${ma>=0?"+":""}${ma.toFixed(1)}%</span>`;
  };
  const _rangeCell=(w52)=>{
    if(w52==null) return '<span class="muted">—</span>';
    const v=Math.max(0,Math.min(100,w52));
    return `<div><div class="us-range-bar"><span class="us-range-mark" style="left:${v.toFixed(1)}%"></span></div><div class="us-range-val">${w52.toFixed(0)}% Range</div></div>`;
  };
  const _scoreDisp=(score)=>{
    const s=Math.max(0,Math.min(6,score));
    const cls=`us-score-${Math.min(6,Math.max(0,s))}`;
    const pips=Array.from({length:6},(_,i)=>`<span class="us-score-dot${i<s?" on":""}"></span>`).join("");
    return `<div class="us-score"><span class="us-score-num ${cls}">${s}</span><div class="us-score-pip">${pips}</div></div>`;
  };
  const _sigChips=(signals)=>signals.map(s=>
    `<span class="us-sig us-sig-${s.cls}" title="${esc(s.k+(s.note?" — "+s.note:""))}">${esc(s.k)} ${esc(s.v)}</span>`
  ).join("");
  const rows=top8.map((c,i)=>`<tr>
    <td class="col-name"><div class="us-name"><span class="us-tk">${esc(c.TK)}</span><span class="us-sec">${esc(c.sec)}</span></div></td>
    <td class="col-score">${_scoreDisp(c.score)}</td>
    <td class="col-rec">${_recChip(c.rec)}</td>
    <td class="col-rsi r">${_rsiCell(c.rsi)}</td>
    <td class="col-ma r">${_maCell(c.ma)}</td>
    <td class="col-range col-range">${_rangeCell(c.w52)}</td>
    <td class="col-signals">${_sigChips(c.signals)}</td>
  </tr>`).join("");
  // Also show which open calls would rank in the scanner (informational)
  const openScores=openCands.slice(0,3).map(c=>`${c.TK} (${c.score})`).join(", ");
  const openNote=openCands.length?`Offene Calls im Vergleich: ${openScores}`:"";
  const highScore=top8[0]&&top8[0].score>=4;
  root.innerHTML=`<div class="panel us-panel">
    <div class="us-h">
      <div>
        <div class="us-title">Ideen-Scanner — Universum-Screening</div>
        <div class="us-sub">Top-Ideen aus ${nonOpen.length} nicht-positionierten Tickers · Composite-Score: Rec + RSI + Trend + 52w-Runway · Long-Bias</div>
      </div>
      <div class="us-meta">
        <div class="us-meta-kpi"><span class="lbl">Kandidaten</span><span class="val">${nonOpen.length}</span></div>
        <div class="us-meta-kpi"><span class="lbl">Top-Score</span><span class="val ${highScore?"move-up":""}">${top8[0]?top8[0].score:0}<span class="muted" style="font-size:var(--fs-micro)">/6</span></span></div>
      </div>
    </div>
    <div class="us-wrap">
      <table class="us-tbl" role="table" aria-label="Top-Ideen aus dem Universum-Screening">
        <thead><tr>
          <th scope="col">Ticker</th>
          <th scope="col" title="Composite-Score 0-6 (Rec + RSI + Trend + Range)">Score</th>
          <th scope="col">Analyst-Rec</th>
          <th scope="col" class="r" title="RSI14 — Momentum/Overbought-Oversold">RSI</th>
          <th scope="col" class="r" title="Spot vs 30-Tage gleitender Durchschnitt">vs MA30</th>
          <th scope="col" class="col-range" title="Position in der 52-Wochen-Range">52w-Range</th>
          <th scope="col" class="col-signals">Signale</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="us-foot">${openNote?`<span class="muted">${esc(openNote)}</span> · `:""}Score-Methodik: Rec (+2 Strong Buy, +1 Buy, −1 Sell) + RSI (+2 Überverkauft<35, +1 <50, −1 >65) + Trend (+1 vs MA30 >+2%) + 52w-Range (+1 unter 65%). Max Score = 6. Long-Bias-Screen — kein Short-Screening, keine Gewichtung. Sortiert nach Score dann RSI (niedrigster RSI = bester Einstieg). Nur Tickers ohne aktive Position im Buch.</div>
  </div>`;
  root.setAttribute("aria-busy","false");
})();

// Konsens-Spread — Bloomberg-ANR/EE-Stil (HED-137 Zyklus 105).
// Variant-perception map: für jedes Universum-Ticker visualisiere die analyst PT-Range
// (low | mean | high) und wo der aktuelle Preis darauf sitzt. Hoher Spread =
// Analystenuneinigkeit = Raum für eine differenzierte These. Mean PT > Price =
// noch Upside-Headroom; Mean PT ≤ Price = consensus eingepreist.
// Sortierung: Spread% desc (=größte Disagreement zuerst).
// Highlight-Tags pro Reihe: "Contested" (spread>50%), "Room" (upside>15%),
// "Rich" (upside<0, consensus already passed), "Priced In" (upside<5%).
(function(){
  const root=$("consspread"); if(!root) return;
  const sv=D.sector_view;
  if(!sv){ root.innerHTML='<div class="panel cs-panel"><div class="cs-empty">Konsens-Daten nicht verfügbar — sector_view fehlt.</div></div>'; return; }
  const tr=D.track_record;
  const active=(tr&&tr.theses?tr.theses.filter(t=>t.verdict!=="win"&&t.verdict!=="loss"):[])||[];
  const ownSet=new Set();
  active.forEach(t=>(t.tickers||[]).forEach(tk=>ownSet.add(String(tk).toUpperCase())));
  const rows=[];
  (sv.sectors||[]).forEach(sec=>{
    (sec.tickers||[]).forEach(t=>{
      if(!t||!t.ticker) return;
      const c=t.consensus||{};
      const price=t.price, lo=c.pt_low, hi=c.pt_high, mn=c.pt_mean, n=c.analyst_count;
      if(price==null||lo==null||hi==null||mn==null||hi<=lo) return;
      const spread=(hi-lo)/mn*100;            // disagreement metric
      const upside=(mn-price)/price*100;       // implied upside to mean PT
      // Where the price sits on the [low..high] track, clamped for the marker
      const rangeSpan=hi-lo;
      const pricePos=Math.max(0,Math.min(100,(price-lo)/rangeSpan*100));
      const meanPos=Math.max(0,Math.min(100,(mn-lo)/rangeSpan*100));
      rows.push({TK:String(t.ticker).toUpperCase(),sec:sec.name,price,lo,hi,mn,n:n||0,
        spread,upside,pricePos,meanPos,rec:c.rec||"",own:ownSet.has(String(t.ticker).toUpperCase())});
    });
  });
  if(!rows.length){
    root.innerHTML='<div class="panel cs-panel"><div class="cs-empty">Keine Analyst-Preisziel-Daten verfügbar.</div></div>';
    return;
  }
  rows.sort((a,b)=>b.spread-a.spread);
  const top=rows.slice(0,12);
  // Universum-Aggregate für KPI-Strip
  const median=(arr)=>{const s=arr.slice().sort((a,b)=>a-b);const m=Math.floor(s.length/2);return s.length%2?s[m]:(s[m-1]+s[m])/2;};
  const medSpread=median(rows.map(r=>r.spread));
  const medUpside=median(rows.map(r=>r.upside));
  const contested=rows.filter(r=>r.spread>50).length;
  const room=rows.filter(r=>r.upside>15).length;
  const ownContested=rows.filter(r=>r.own&&r.spread>50).length;
  const _money=(v)=>"$"+(v>=1000?v.toFixed(0):v.toFixed(1));
  const _recChip=(r)=>{
    if(r==="strong_buy") return '<span class="cs-rec cs-rec-sb">Strong Buy</span>';
    if(r==="buy") return '<span class="cs-rec cs-rec-b">Buy</span>';
    if(r==="hold") return '<span class="cs-rec cs-rec-h">Hold</span>';
    if(r==="sell"||r==="strong_sell") return '<span class="cs-rec cs-rec-s">Sell</span>';
    return '<span class="cs-rec cs-rec-h">—</span>';
  };
  const _flag=(r)=>{
    const f=[];
    if(r.spread>50) f.push('<span class="cs-flag cs-flag-contested" title="Spread >50% — hohe Analystenuneinigkeit, Raum für variant perception">Contested</span>');
    if(r.upside>15) f.push('<span class="cs-flag cs-flag-room" title="Mean PT impliziert >15% Upside">Room</span>');
    else if(r.upside<0) f.push('<span class="cs-flag cs-flag-rich" title="Preis bereits über Mean PT — consensus eingepreist oder überschritten">Rich</span>');
    else if(r.upside<5) f.push('<span class="cs-flag cs-flag-priced" title="Upside <5% — Mean-PT-Niveau praktisch erreicht">Priced In</span>');
    return f.join(" ");
  };
  const _rngBar=(r)=>{
    return `<div class="cs-rng" title="Range $${r.lo.toFixed(2)} → $${r.hi.toFixed(2)} · Mean $${r.mn.toFixed(2)} · Spot $${r.price.toFixed(2)}">
      <span class="cs-rng-lbl lo">${esc(_money(r.lo))}</span>
      <span class="cs-rng-lbl hi">${esc(_money(r.hi))}</span>
      <div class="cs-rng-track"></div>
      <span class="cs-rng-mean" style="left:${r.meanPos.toFixed(1)}%"></span>
      <span class="cs-rng-mean-d" style="left:${r.meanPos.toFixed(1)}%"></span>
      <span class="cs-rng-price" style="left:${r.pricePos.toFixed(1)}%"></span>
    </div>`;
  };
  const _spread=(s)=>{
    const cls=s>70?"vhi":s>40?"hi":"";
    const fillW=Math.max(4,Math.min(100,s));
    return `<div><span class="cs-spread-val ${cls}">${s.toFixed(0)}%</span>
      <div class="cs-spread-bar" title="High-Low als % der Mean — Disagreement-Proxy"><span class="cs-spread-fill" style="width:${fillW.toFixed(1)}%"></span></div></div>`;
  };
  const _ups=(u)=>{
    const cls=u>=15?"pos":u>=0?"neu":"neg";
    const sign=u>=0?"+":"";
    return `<span class="cs-up ${cls}">${sign}${u.toFixed(1)}%</span>`;
  };
  const tbody=top.map(r=>`<tr${r.own?' class="own"':''}>
    <td class="cs-tk-cell"><div class="cs-tk-row"><span class="cs-tk">${esc(r.TK)}</span>${r.own?'<span class="cs-own-lbl" title="Aktive Position im Buch">CALL</span>':''}</div><div class="cs-sec-tag">${esc(r.sec)}</div></td>
    <td class="cs-rng-cell">${_rngBar(r)}</td>
    <td class="r">${_ups(r.upside)}</td>
    <td class="cs-spread-cell">${_spread(r.spread)}</td>
    <td class="r col-hide-m"><span class="cs-n">${r.n}</span></td>
    <td class="c col-hide-m">${_recChip(r.rec)}</td>
    <td>${_flag(r)}</td>
  </tr>`).join("");
  root.innerHTML=`<div class="panel cs-panel">
    <div class="cs-h">
      <div>
        <div class="cs-title">Konsens-Spread — Analystenuneinigkeit</div>
        <div class="cs-sub">PT-Range &amp; Variant-Perception-Karte über ${rows.length} Universum-Tickers · sortiert nach Spread (=Disagreement-Proxy)</div>
      </div>
      <div class="cs-meta">
        <div class="cs-meta-kpi" title="Median (High-Low)/Mean über Universum"><span class="lbl">Median Spread</span><span class="val">${medSpread.toFixed(0)}%</span></div>
        <div class="cs-meta-kpi" title="Tickers mit Spread > 50% — contested names"><span class="lbl">Contested</span><span class="val hot">${contested}<span class="muted" style="font-size:var(--fs-micro)">/${rows.length}</span></span></div>
        <div class="cs-meta-kpi" title="Median impliziter Upside zur Mean PT"><span class="lbl">Median Upside</span><span class="val ${medUpside>=0?'':'cs-up neg'}">${medUpside>=0?'+':''}${medUpside.toFixed(1)}%</span></div>
        <div class="cs-meta-kpi" title="Eigene Calls auf contested Tickers"><span class="lbl">Eigene · Contested</span><span class="val">${ownContested}<span class="muted" style="font-size:var(--fs-micro)">/${ownSet.size||0}</span></span></div>
      </div>
    </div>
    <div class="cs-wrap">
      <table class="cs-tbl" role="table" aria-label="Konsens-Spread — Analystenuneinigkeit pro Ticker">
        <thead><tr>
          <th scope="col">Ticker</th>
          <th scope="col">PT-Range · Preis</th>
          <th scope="col" class="r" title="Mean PT vs aktueller Preis">Upside</th>
          <th scope="col" title="(High-Low)/Mean — höher = mehr Analystenuneinigkeit">Spread</th>
          <th scope="col" class="r col-hide-m" title="Analyst-Count">n</th>
          <th scope="col" class="c col-hide-m">Rec</th>
          <th scope="col">Setup</th>
        </tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>
    <div class="cs-foot">
      <b>Lesart:</b> Die Range-Bar zeigt die Spannweite der Analyst-Kursziele (links = Low, rechts = High); das blaue ◆ markiert die Mean PT, der weiße Strich den aktuellen Spot. <b>Spread</b> = (High − Low) / Mean — ein einfacher Proxy für Analystenuneinigkeit. <b>Investoren-Lesart:</b> hoher Spread + meaningful Upside = Raum für eine differenzierte These (variant perception). Niedriger Spread + Upside ≈ 0 = consensus crowded, kein Alpha. <b>CALL</b>-Markierung zeigt: dort haben wir bereits eine eigene These — idealerweise auf den "Contested"-Namen, nicht auf den "Priced In"-Namen. Quelle: yfinance recommendations + targetMean/Low/High (sector_view).
    </div>
  </div>`;
  root.setAttribute("aria-busy","false");
})();

// loading complete: clear skeleton busy-state so assistive tech announces rendered content
["briefing","trackrecord","portfolioview","catalysts","sectorview","universe-scanner","consspread","earnplay","insidertape"].forEach(id=>{const el=$(id);if(el)el.setAttribute("aria-busy","false");});

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




























