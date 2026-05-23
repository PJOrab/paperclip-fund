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
        "macro_pulse": collect_macro_pulse(c),
        "options_tape": collect_options_tape(c),
        "short_squeeze": collect_short_squeeze(c),
        "eps_revisions": collect_eps_revisions(c),
        "tech_levels": collect_tech_levels(c),
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


# Macro-Pulse (HED-137 Zyklus 106): Bloomberg-MAC-style market-context strip.
# Pulls the FRED-Makro raw_items emitted by FREDMacroAdapter (no extra ingest
# work — we just surface what we already store), takes the freshest reading per
# series, and computes a single risk-regime verdict (VIX × HY OAS × yield curve)
# so a Bloomberg-trained PM sees the "what is the market doing right now" anchor
# above the fund-internal panels. Without this, the dashboard tells you what the
# fund believes but never what the macro environment is.
_FRED_RE = re.compile(
    r"^FRED:\s+(?P<name>.+?)\s+\((?P<sid>[A-Z0-9]+)\)\s+=\s+"
    r"(?P<val>[+-]?[\d.,]+)(?P<unit>pp|%)?"
    r"(?:\s+\[(?P<date>\d{4}-\d{2}-\d{2})\])?"
    r"(?:\s+\(Δ\s+(?P<dpct>[+-]?[\d.]+)%\s+(?:vs\.?|vs prior)\s+(?P<prev>[+-]?[\d.,]+)(?:pp|%)?\))?"
)
# Series we surface, in display order. (label, decimals, unit-suffix, hint)
# Order optimised for an AI/Tech investor: risk gauges first, then rate, then
# levels — VIX is the single most predictive number for tech multiples.
_MP_SERIES = [
    ("VIXCLS",       "VIX",        1, "",   "CBOE Volatility Index — equity fear gauge"),
    ("DGS10",        "10Y UST",    2, "%",  "10-Year Treasury yield — discount-rate proxy for long-duration tech"),
    ("T10Y2Y",       "2s10s",      2, "pp", "10Y minus 2Y yield spread — curve inversion = late-cycle warning"),
    ("BAMLH0A0HYM2", "HY OAS",     2, "pp", "High-Yield option-adjusted spread — credit-market stress canary"),
    ("DTWEXBGS",     "USD TWI",    2, "",   "Trade-Weighted US Dollar Index — global liquidity proxy"),
    ("DFF",          "Fed Funds",  2, "%",  "Effective Federal Funds Rate"),
    ("SP500",        "S&P 500",    2, "",   "S&P 500 Index level"),
    ("NASDAQCOM",    "Nasdaq",     2, "",   "Nasdaq Composite Index level"),
    ("ICSA",         "Init Claims",0, "K",  "Initial Jobless Claims (weekly, leading labor indicator)"),
]


def _mp_parse(text: str) -> dict | None:
    m = _FRED_RE.search(text or "")
    if not m:
        return None
    try:
        val = float(m.group("val").replace(",", ""))
    except (TypeError, ValueError):
        return None
    dpct = m.group("dpct")
    try:
        delta_pct = float(dpct) if dpct is not None else None
    except ValueError:
        delta_pct = None
    return {
        "sid": m.group("sid"),
        "name": m.group("name"),
        "value": val,
        "unit": m.group("unit") or "",
        "date": m.group("date"),
        "delta_pct": delta_pct,
    }


def collect_macro_pulse(c, lookback_days: int = 14) -> dict:
    """Latest FRED-Makro reading per series for the dashboard top-of-page bar.

    Returns
      {
        "as_of": iso,
        "verdict": {"label": "Risk-On|Neutral|Cautious|Risk-Off", "score": float, "color": "g|n|a|r", "tech_read": str},
        "tiles": [{"sid","label","value","display","delta_pct","date","unit","hint","tone": ""|"g"|"a"|"r"}, …],
        "stale": bool,
      }
    Always returns the shape; empty `tiles` if the FRED rows aren't fresh."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = (c.table("raw_items")
                .select("text,fetched_at")
                .eq("source", "fred_macro")
                .gte("fetched_at", cutoff)
                .order("fetched_at", desc=True)
                .limit(400)
                .execute().data or [])
    except Exception:
        rows = []

    # Latest reading per series — rows are already newest-first so the first
    # successful parse per sid wins. Also keep the previous reading we see to
    # provide a Δ even when the newer print didn't carry one inline.
    latest: dict[str, dict] = {}
    for r in rows:
        p = _mp_parse(r.get("text"))
        if not p:
            continue
        sid = p["sid"]
        # Take the freshest-by-print-date for that series (skip older rows)
        prev = latest.get(sid)
        cur_date = p.get("date") or (r.get("fetched_at") or "")[:10]
        if prev:
            prev_date = prev.get("date") or ""
            if cur_date and prev_date and cur_date <= prev_date:
                continue
        p["fetched_at"] = r.get("fetched_at")
        latest[sid] = p

    def _disp(sid: str, val: float, decimals: int, unit: str) -> str:
        if unit == "K":
            return f"{val/1000:,.0f}K"
        if sid in ("SP500", "NASDAQCOM"):
            return f"{val:,.0f}"
        if sid in ("T10Y2Y", "BAMLH0A0HYM2"):
            # signed pp display
            return f"{val:+.{decimals}f}{unit}"
        if unit == "%":
            return f"{val:.{decimals}f}%"
        return f"{val:.{decimals}f}{unit}"

    def _tone(sid: str, val: float) -> str:
        """Per-tile colour-tone (preattentive risk read). 'g'=benign, 'a'=elevated, 'r'=stressed, ''=neutral."""
        if sid == "VIXCLS":
            if val < 15: return "g"
            if val < 20: return ""
            if val < 25: return "a"
            return "r"
        if sid == "BAMLH0A0HYM2":
            if val < 3.0: return "g"
            if val < 4.5: return ""
            if val < 6.0: return "a"
            return "r"
        if sid == "T10Y2Y":
            if val > 0.5:  return "g"
            if val > 0.0:  return ""
            if val > -0.3: return "a"
            return "r"
        if sid == "DGS10":
            # Tech-specific: tone tracks how punitive the rate is for long-duration multiples
            if val < 3.5: return "g"
            if val < 4.5: return ""
            if val < 5.0: return "a"
            return "r"
        return ""

    tiles = []
    for sid, label, decimals, unit, hint in _MP_SERIES:
        p = latest.get(sid)
        if not p:
            tiles.append({
                "sid": sid, "label": label, "value": None, "display": "—",
                "delta_pct": None, "date": None, "unit": unit, "hint": hint, "tone": "",
            })
            continue
        val = p["value"]
        tiles.append({
            "sid": sid,
            "label": label,
            "value": val,
            "display": _disp(sid, val, decimals, unit),
            "delta_pct": p.get("delta_pct"),
            "date": p.get("date"),
            "unit": unit,
            "hint": hint,
            "tone": _tone(sid, val),
        })

    # Regime verdict — weight VIX heaviest (2x), HY OAS, then curve. Each bucket
    # is 0 (benign) → 3 (stressed); weighted average maps to a single label.
    def _bucket(sid: str, val: float | None) -> int | None:
        if val is None: return None
        if sid == "VIXCLS":
            if val < 15: return 0
            if val < 20: return 1
            if val < 25: return 2
            return 3
        if sid == "BAMLH0A0HYM2":
            if val < 3.0: return 0
            if val < 4.5: return 1
            if val < 6.0: return 2
            return 3
        if sid == "T10Y2Y":
            if val > 0.5:  return 0
            if val > 0.0:  return 1
            if val > -0.3: return 2
            return 3
        return None

    vix_b = _bucket("VIXCLS", (latest.get("VIXCLS") or {}).get("value"))
    hy_b  = _bucket("BAMLH0A0HYM2", (latest.get("BAMLH0A0HYM2") or {}).get("value"))
    cv_b  = _bucket("T10Y2Y", (latest.get("T10Y2Y") or {}).get("value"))
    weights = []
    if vix_b is not None: weights.append((vix_b, 2.0))
    if hy_b  is not None: weights.append((hy_b,  1.5))
    if cv_b  is not None: weights.append((cv_b,  1.0))
    if weights:
        score = sum(b * w for b, w in weights) / sum(w for _, w in weights)
    else:
        score = None
    if score is None:
        label, color = "Daten fehlen", "n"
    elif score < 0.5:
        label, color = "Risk-On", "g"
    elif score < 1.4:
        label, color = "Neutral", "n"
    elif score < 2.2:
        label, color = "Cautious", "a"
    else:
        label, color = "Risk-Off", "r"

    # Tech-Read: single-line interpretation aimed at an AI/Tech book. Built from
    # the same drivers but framed in terms of long-duration multiple exposure.
    tr_bits = []
    vix = (latest.get("VIXCLS") or {}).get("value")
    y10 = (latest.get("DGS10") or {}).get("value")
    y10_d = (latest.get("DGS10") or {}).get("delta_pct")
    hy  = (latest.get("BAMLH0A0HYM2") or {}).get("value")
    if vix is not None:
        tr_bits.append(f"Vol {'subdued' if vix < 15 else 'elevated' if vix > 22 else 'normal'} (VIX {vix:.1f})")
    if y10 is not None:
        rd = "stable"
        if y10_d is not None:
            if y10_d > 1.5: rd = "rising"
            elif y10_d < -1.5: rd = "falling"
        tone_word = "headwind" if (y10 > 4.5 and rd != "falling") else ("tailwind" if (y10 < 4.0 or rd == "falling") else "neutral")
        tr_bits.append(f"rates {rd} → multiple {tone_word}")
    if hy is not None and hy > 4.5:
        tr_bits.append("credit widening")
    tech_read = " · ".join(tr_bits) if tr_bits else "Insufficient macro data."

    # Staleness: if the freshest tile date is >7d old, the FRED feed is lagging
    fresh = [t["date"] for t in tiles if t.get("date")]
    stale = False
    if fresh:
        try:
            newest = max(datetime.fromisoformat(d).date() for d in fresh)
            stale = (datetime.now(timezone.utc).date() - newest).days > 7
        except Exception:
            stale = False

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "verdict": {"label": label, "score": (round(score, 2) if score is not None else None),
                    "color": color, "tech_read": tech_read},
        "tiles": tiles,
        "stale": stale,
    }


# Options-Tape (HED-137 Zyklus 107): Bloomberg-OMON-equivalent institutional-
# positioning panel. Parses the rows emitted by OptionsMarketAdapter into a
# structured per-ticker view (expected move ±%, P/C OI ratio, ATM IV skew,
# verdict) and cross-references against open calls so a PM sees in one scan:
# "where is institutional flow leaning, and does that confirm or contradict the
# book?" Source is yfinance-derived chain data on the nearest weekly expiry —
# we never refetch live (the adapter already wrote it), we just surface it.
_OT_RE = re.compile(
    r"^\[(?P<ticker>[A-Z][A-Z0-9.\-]{0,9})\]\s+Options:\s+"
    r"(?:.*?P/C\s+OI\s+(?P<pc>[\d.]+)(?:\s+\((?P<pc_label>[^)]+)\))?)?"
    r"(?:.*?IV\s+skew\s+(?P<skew>[+\-]?[\d.]+)pp(?:\s+\((?P<skew_label>[^)]+)\))?)?"
    r"(?:.*?expected\s+move\s+±(?P<emove>[\d.]+)%\s+by\s+(?P<exp>\d{4}-\d{2}-\d{2})(?:\s+\((?P<emove_label>[^)]+)\))?)?"
)


def _ot_parse(text: str) -> dict | None:
    m = _OT_RE.search(text or "")
    if not m:
        return None
    try:
        pc = float(m.group("pc")) if m.group("pc") else None
        skew = float(m.group("skew")) if m.group("skew") else None
        emove = float(m.group("emove")) if m.group("emove") else None
    except ValueError:
        return None
    return {
        "ticker": m.group("ticker").upper(),
        "pc": pc,
        "skew": skew,           # in pp (already divided by 100 of vol units? no — raw pp)
        "emove": emove,          # in %
        "exp": m.group("exp"),
        "pc_label": (m.group("pc_label") or "").strip(),
        "skew_label": (m.group("skew_label") or "").strip(),
        "emove_label": (m.group("emove_label") or "").strip(),
    }


def collect_options_tape(c, lookback_days: int = 4, ticker_cap: int = 22) -> dict:
    """Latest options-market positioning per ticker, ranked by signal strength.

    Returns
      {
        "as_of": iso,
        "lookback_days": int,
        "tickers": [
          {"ticker","pc","skew","emove","exp",
           "verdict": "bullish_setup|bearish_setup|hedge_bid|squeeze_risk|event_pending|neutral",
           "tone": "g|r|a|n",            # row colour cue
           "signals": [str, ...],         # human-readable annotations from the adapter
           "score": float,                # used for sorting; |signal-magnitude|
           },
          ...
        ],
        "stale": bool,
        "n_bullish": int, "n_bearish": int, "n_high_iv": int,
      }
    Always returns the shape — empty `tickers` if the options_market feed is silent."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = (c.table("raw_items")
                .select("text,fetched_at")
                .eq("source", "options_market")
                .gte("fetched_at", cutoff)
                .order("fetched_at", desc=True)
                .limit(400)
                .execute().data or [])
    except Exception:
        rows = []

    latest: dict[str, dict] = {}
    for r in rows:
        p = _ot_parse(r.get("text"))
        if not p:
            continue
        # First (newest) per ticker wins — rows are sorted desc
        if p["ticker"] not in latest:
            p["fetched_at"] = r.get("fetched_at")
            latest[p["ticker"]] = p

    # Verdict + tone + sort-score
    # Bloomberg-style read:
    #  - P/C OI < 0.50 = bullish positioning, > 1.20 = bearish positioning
    #  - IV skew > +5pp = put premium bid (hedging / downside fear)
    #  - IV skew < −5pp = call premium bid (squeeze / momentum chase)
    #  - Expected move ≥ 4% = event/catalyst pricing
    out = []
    for tk, p in latest.items():
        signals = []
        score = 0.0
        verdict = "neutral"
        tone = "n"
        bullish_pc = p["pc"] is not None and p["pc"] < 0.50
        bearish_pc = p["pc"] is not None and p["pc"] > 1.20
        put_bid = p["skew"] is not None and p["skew"] > 5.0
        call_bid = p["skew"] is not None and p["skew"] < -5.0
        big_move = p["emove"] is not None and p["emove"] >= 4.0

        if bullish_pc and not bearish_pc:
            signals.append("Bull-OI (P/C<0.5)")
            score += 2.0
        if bearish_pc:
            signals.append("Bear-OI (P/C>1.2)")
            score += 2.0
        if put_bid:
            signals.append(f"Put-Bid (skew +{p['skew']:.1f}pp)")
            score += abs(p["skew"]) / 5.0
        if call_bid:
            signals.append(f"Call-Bid (skew {p['skew']:.1f}pp)")
            score += abs(p["skew"]) / 5.0
        if big_move:
            signals.append(f"Event ±{p['emove']:.1f}%")
            score += p["emove"] / 4.0

        # Verdict ranking — pick the dominant story
        if bullish_pc and call_bid:
            verdict, tone = "squeeze_risk", "g"
        elif bearish_pc and put_bid:
            verdict, tone = "hedge_bid", "r"
        elif bullish_pc:
            verdict, tone = "bullish_setup", "g"
        elif bearish_pc:
            verdict, tone = "bearish_setup", "r"
        elif put_bid:
            verdict, tone = "hedge_bid", "a"
        elif call_bid:
            verdict, tone = "squeeze_risk", "a"
        elif big_move:
            verdict, tone = "event_pending", "a"
        else:
            verdict, tone = "neutral", "n"

        out.append({
            "ticker": tk,
            "pc": p["pc"], "skew": p["skew"], "emove": p["emove"], "exp": p["exp"],
            "verdict": verdict,
            "tone": tone,
            "signals": signals,
            "score": round(score, 3),
            "fetched_at": p.get("fetched_at"),
        })

    out.sort(key=lambda r: -r["score"])
    out = out[:ticker_cap]
    n_bullish = sum(1 for r in out if r["verdict"] in ("bullish_setup", "squeeze_risk"))
    n_bearish = sum(1 for r in out if r["verdict"] in ("bearish_setup", "hedge_bid"))
    n_high_iv = sum(1 for r in out if r["emove"] is not None and r["emove"] >= 4.0)

    # Staleness: freshest reading > 2 days old = stale options feed
    stale = False
    if out:
        try:
            newest = max(
                datetime.fromisoformat(str(r["fetched_at"]).replace("Z", "+00:00"))
                for r in out if r.get("fetched_at")
            )
            stale = (datetime.now(timezone.utc) - newest).days > 2
        except Exception:
            stale = False

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lookback_days": lookback_days,
        "tickers": out,
        "n_bullish": n_bullish,
        "n_bearish": n_bearish,
        "n_high_iv": n_high_iv,
        "stale": stale,
    }


# Short-Squeeze-Pressure (HED-137 Zyklus 110): Bloomberg-SI-equivalent short-interest
# panel. ShortInterestAdapter emits rows like "[ARM] Short interest: 11.4% of float —
# elevated short interest = potential squeeze setup on positive catalyst" and optionally
# "↑65% vs prior month" when the prior month delta is notable. We parse the latest reading
# per ticker, bucket by SI level (low<5 / elevated 5-10 / high 10-20 / extreme ≥20),
# surface the MoM trend, and tag the squeeze verdict against open-call direction so a PM
# sees in one scan: where is the crowded short, and does the book agree or disagree.
# Together with Options-Tape (gamma/IV), Insider-Tape (corp insiders) and Konsens-Spread
# (sell-side disagreement), this rounds out the positioning suite — the fourth Bloomberg
# positioning lens a PM scans every morning.
_SI_RE = re.compile(
    r"^\[(?P<ticker>[A-Z][A-Z0-9.\-]{0,9})\]\s+Short\s+interest:\s+"
    r"(?P<si>[\d.]+)%\s+of\s+float"
    r"(?:.*?(?P<arrow>[↑↓])(?P<mom>[\d.]+)%\s+vs\s+prior\s+month)?",
    re.IGNORECASE,
)


def _si_parse(text: str) -> dict | None:
    m = _SI_RE.search(text or "")
    if not m:
        return None
    try:
        si = float(m.group("si"))
    except (TypeError, ValueError):
        return None
    mom_pct = None
    arrow = m.group("arrow")
    mom = m.group("mom")
    if arrow and mom:
        try:
            v = float(mom)
            mom_pct = v if arrow == "↑" else -v
        except ValueError:
            mom_pct = None
    return {"ticker": m.group("ticker").upper(), "si": si, "mom_pct": mom_pct}


def collect_short_squeeze(c, lookback_days: int = 14, ticker_cap: int = 24) -> dict:
    """Latest per-ticker short-interest reading, sorted by squeeze-pressure score.

    Score combines absolute SI level (level matters most — a 20% short ratio is
    structurally different from a 5%) with MoM acceleration (rising SI = building
    bearish positioning; falling = covering pressure). Returns the same envelope
    shape as the other positioning panels for consistent rendering.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = (c.table("raw_items")
                .select("text,fetched_at")
                .eq("source", "yahoo_short_interest")
                .gte("fetched_at", cutoff)
                .order("fetched_at", desc=True)
                .limit(400)
                .execute().data or [])
    except Exception:
        rows = []

    latest: dict[str, dict] = {}
    for r in rows:
        p = _si_parse(r.get("text"))
        if not p:
            continue
        if p["ticker"] not in latest:
            p["fetched_at"] = r.get("fetched_at")
            latest[p["ticker"]] = p

    out = []
    for tk, p in latest.items():
        si = p["si"]
        mom = p.get("mom_pct")
        if si >= 20:
            bucket = "extreme"
        elif si >= 10:
            bucket = "high"
        elif si >= 5:
            bucket = "elevated"
        else:
            bucket = "low"

        if bucket == "extreme":
            verdict, tone = "squeeze_risk", "r"
        elif bucket == "high" and (mom is None or mom >= -5):
            verdict, tone = "squeeze_risk", "a"
        elif mom is not None and mom >= 30:
            verdict, tone = "building_short", "a"
        elif mom is not None and mom <= -15:
            verdict, tone = "covering", "g"
        elif bucket == "elevated":
            verdict, tone = "crowded_short", "a"
        else:
            verdict, tone = "baseline", "n"

        score = si + (abs(mom) * 0.1 if mom is not None else 0.0)

        out.append({
            "ticker": tk,
            "si": si,
            "mom_pct": mom,
            "bucket": bucket,
            "verdict": verdict,
            "tone": tone,
            "score": round(score, 3),
            "fetched_at": p.get("fetched_at"),
        })

    out.sort(key=lambda r: -r["score"])
    out = out[:ticker_cap]
    n_extreme = sum(1 for r in out if r["bucket"] == "extreme")
    n_high = sum(1 for r in out if r["bucket"] == "high")
    n_rising = sum(1 for r in out if r.get("mom_pct") is not None and r["mom_pct"] >= 25)

    stale = False
    if out:
        try:
            newest = max(
                datetime.fromisoformat(str(r["fetched_at"]).replace("Z", "+00:00"))
                for r in out if r.get("fetched_at")
            )
            stale = (datetime.now(timezone.utc) - newest).days > 7
        except Exception:
            stale = False

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lookback_days": lookback_days,
        "tickers": out,
        "n_extreme": n_extreme,
        "n_high": n_high,
        "n_rising": n_rising,
        "stale": stale,
    }


# EPS-Revisions-Velocity (HED-137 Zyklus 112): Bloomberg-EE/EM-equivalent
# sell-side estimate-revision panel. EpsRevisionsAdapter emits one row per
# watchlist ticker when the StarMine/IBES revision velocity is directional:
# "[NVDA] Sell-side EPS revisions POSITIVE (current quarter) — 7d: 8 up / 1 down ·
# 30d: 12 up / 2 down — consensus Q-EPS $0.85 (+4.2% vs 30d ago) — FY-30d: 18 up /
# 3 down (aligned) — 28 analysts in consensus". This is the single strongest
# academic forward-return predictor for individual equities (PEAD, Bernard &
# Thomas 1989; Jegadeesh-Titman; modern StarMine 1-yr alpha ≈3-5%). We render
# per-ticker rows showing 30d net-revision as a centered diverging bar
# (down=red left, up=green right), 7d count as compact reads, EPS drift % as
# the dollar-weighted magnitude, verdict pill, and a book cross-ref badge —
# long on a tailwind name = momentum-aligned; short on a tailwind = fighting
# the tape (asymmetry-risk). Together with Earnings-Playbook (historic beat
# behavior), Konsens-Spread (analyst disagreement on PT) and Quality-Scorecard
# (Rule-of-40 fundamentals), this completes the earnings/fundamental lens
# institutional PMs scan before every print.
_ER_RE = re.compile(
    r"^\[(?P<ticker>[A-Z][A-Z0-9.\-]{0,9})\]\s+Sell-side EPS revisions\s+"
    r"(?P<dir>POSITIVE|NEGATIVE)\b",
    re.IGNORECASE,
)
_ER_7D = re.compile(r"7d:\s*(\d+)\s*up\s*/\s*(\d+)\s*down", re.IGNORECASE)
_ER_30D = re.compile(r"30d:\s*(\d+)\s*up\s*/\s*(\d+)\s*down", re.IGNORECASE)
_ER_DRIFT = re.compile(
    r"consensus Q-EPS\s+\$([\d.\-]+)\s*\(([+\-]?[\d.]+)%\s*vs\s*30d ago\)",
    re.IGNORECASE,
)
_ER_FY = re.compile(
    r"FY-30d:\s*(\d+)\s*up\s*/\s*(\d+)\s*down\s*\(aligned\)",
    re.IGNORECASE,
)
_ER_ANAL = re.compile(r"(\d+)\s*analysts in consensus", re.IGNORECASE)


def _er_parse(text: str) -> dict | None:
    m = _ER_RE.search(text or "")
    if not m:
        return None
    out: dict = {
        "ticker": m.group("ticker").upper(),
        "direction": m.group("dir").upper(),
    }
    m7 = _ER_7D.search(text)
    if m7:
        try:
            out["up7"] = int(m7.group(1))
            out["down7"] = int(m7.group(2))
        except ValueError:
            pass
    m30 = _ER_30D.search(text)
    if m30:
        try:
            out["up30"] = int(m30.group(1))
            out["down30"] = int(m30.group(2))
        except ValueError:
            pass
    md = _ER_DRIFT.search(text)
    if md:
        try:
            out["eps_cur"] = float(md.group(1))
            out["drift_pct"] = float(md.group(2))  # already signed
        except ValueError:
            pass
    mfy = _ER_FY.search(text)
    if mfy:
        try:
            out["fy_up30"] = int(mfy.group(1))
            out["fy_down30"] = int(mfy.group(2))
        except ValueError:
            pass
    ma = _ER_ANAL.search(text)
    if ma:
        try:
            out["n_analysts"] = int(ma.group(1))
        except ValueError:
            pass
    return out


def collect_eps_revisions(c, lookback_days: int = 21, ticker_cap: int = 24) -> dict:
    """Latest per-ticker EPS-revision read, sorted by absolute conviction.

    Pulls source='eps_revisions' raw_items from the last `lookback_days`, keeps
    the freshest read per ticker, computes a synthesized verdict per row, and
    returns a render-ready envelope. Score sums |drift_pct|·1.0 + net_30d·0.4
    + net_7d·0.6 so the row order reflects "strongest combined velocity".
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = (c.table("raw_items")
                .select("text,fetched_at,url")
                .eq("source", "eps_revisions")
                .gte("fetched_at", cutoff)
                .order("fetched_at", desc=True)
                .limit(400)
                .execute().data or [])
    except Exception:
        rows = []

    latest: dict[str, dict] = {}
    for r in rows:
        p = _er_parse(r.get("text"))
        if not p:
            continue
        if p["ticker"] not in latest:
            p["fetched_at"] = r.get("fetched_at")
            p["url"] = r.get("url")
            latest[p["ticker"]] = p

    out = []
    for tk, p in latest.items():
        direction = p.get("direction") or "POSITIVE"
        is_pos = direction == "POSITIVE"
        drift = p.get("drift_pct")  # signed %; may be None
        net30 = (p.get("up30", 0) or 0) - (p.get("down30", 0) or 0)
        net7 = (p.get("up7", 0) or 0) - (p.get("down7", 0) or 0)
        abs_drift = abs(drift) if drift is not None else 0.0

        # Verdict bucketing — drift threshold dominates because the EPS-level
        # move is the dollar-weighted impact; revision counts are confirmation
        # of breadth. ≥5% drift = "strong"; ≥2% = standard; <2% = breadth only.
        if is_pos:
            if abs_drift >= 5.0 or net30 >= 8:
                verdict, tone = "strong_tailwind", "g"
            elif abs_drift >= 2.0 or net30 >= 5:
                verdict, tone = "tailwind", "g"
            else:
                verdict, tone = "breadth_pos", "n"
        else:
            if abs_drift >= 5.0 or -net30 >= 8:
                verdict, tone = "strong_headwind", "r"
            elif abs_drift >= 2.0 or -net30 >= 5:
                verdict, tone = "headwind", "r"
            else:
                verdict, tone = "breadth_neg", "n"

        # Acceleration: 7d net per-week pace vs 30d net per-week pace.
        # If the 7d rate exceeds the 30d rate by ≥50%, momentum is accelerating;
        # if it falls below 30% of the 30d rate, momentum is fading.
        accel = None
        if net30 != 0:
            pace_7d_norm = net7  # 7d net already = per-week
            pace_30d_norm = net30 / 4.3  # ~weeks in 30d
            sign_match = (pace_7d_norm * pace_30d_norm) > 0
            if sign_match and pace_30d_norm != 0:
                ratio = abs(pace_7d_norm) / abs(pace_30d_norm) if abs(pace_30d_norm) > 0 else 0
                if ratio >= 1.5:
                    accel = "accel"
                elif ratio <= 0.3:
                    accel = "fade"
            elif not sign_match and abs(net7) >= 2:
                accel = "reversal"

        # FY alignment: a second-derivative read — full-year revisions agreeing
        # with quarter is a much stronger conviction signal than just-quarter
        fy_aligned = ("fy_up30" in p and "fy_down30" in p)

        score = abs_drift + abs(net30) * 0.4 + abs(net7) * 0.6
        if fy_aligned:
            score += 1.5  # FY-aligned reads outrank pure-Q reads of similar magnitude

        out.append({
            "ticker": tk,
            "direction": direction,
            "up7": p.get("up7", 0),
            "down7": p.get("down7", 0),
            "up30": p.get("up30", 0),
            "down30": p.get("down30", 0),
            "net7": net7,
            "net30": net30,
            "drift_pct": drift,
            "eps_cur": p.get("eps_cur"),
            "n_analysts": p.get("n_analysts"),
            "fy_aligned": fy_aligned,
            "fy_up30": p.get("fy_up30"),
            "fy_down30": p.get("fy_down30"),
            "verdict": verdict,
            "tone": tone,
            "accel": accel,
            "score": round(score, 3),
            "fetched_at": p.get("fetched_at"),
            "url": p.get("url"),
        })

    out.sort(key=lambda r: -r["score"])
    out = out[:ticker_cap]

    n_pos = sum(1 for r in out if r["direction"] == "POSITIVE")
    n_neg = sum(1 for r in out if r["direction"] == "NEGATIVE")
    n_strong = sum(1 for r in out
                   if r["verdict"] in ("strong_tailwind", "strong_headwind"))
    n_accel = sum(1 for r in out if r.get("accel") == "accel")

    stale = False
    if out:
        try:
            newest = max(
                datetime.fromisoformat(str(r["fetched_at"]).replace("Z", "+00:00"))
                for r in out if r.get("fetched_at")
            )
            stale = (datetime.now(timezone.utc) - newest).days > 7
        except Exception:
            stale = False

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lookback_days": lookback_days,
        "tickers": out,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_strong": n_strong,
        "n_accel": n_accel,
        "stale": stale,
    }


# Technical-Levels regexes (HED-137 Zyklus 113)
# Text format: "[TECH · NVDA] Golden Cross (50d SMA crossed above 200d today) — RSI-14 72 (overbought) (as of 2026-05-22)"
_TL_HEAD = re.compile(r"^\[TECH\s*·\s*([A-Z][A-Z0-9.\-]{0,9})\]\s+(.+?)(?:\s+\(as of (\d{4}-\d{2}-\d{2})\))?$", re.DOTALL)
_TL_GOLDEN = re.compile(r"Golden Cross", re.IGNORECASE)
_TL_DEATH = re.compile(r"Death Cross", re.IGNORECASE)
_TL_200D = re.compile(r"([+-]?[\d.]+)%\s+vs 200d SMA", re.IGNORECASE)
_TL_52H = re.compile(r"within [\d.]+% of 52w high", re.IGNORECASE)
_TL_52L = re.compile(r"within [\d.]+% of 52w low", re.IGNORECASE)
_TL_50D = re.compile(r"([+-]?[\d.]+)%\s+vs 50d SMA", re.IGNORECASE)
_TL_RSI_OS = re.compile(r"RSI-14\s+(\d+)\s*\(oversold\)", re.IGNORECASE)
_TL_RSI_OB = re.compile(r"RSI-14\s+(\d+)\s*\(overbought\)", re.IGNORECASE)
_TL_VOL = re.compile(r"volume\s+([\d.]+)x\s+20d-avg", re.IGNORECASE)
_TL_GAP_UP = re.compile(r"gap-up\s+([+\d.]+)%", re.IGNORECASE)
_TL_GAP_DN = re.compile(r"gap-down\s+(-?[\d.]+)%", re.IGNORECASE)


def _tl_parse(text: str) -> dict | None:
    """Parse one TechnicalLevelsAdapter raw_item text into a structured dict."""
    if not text:
        return None
    m = _TL_HEAD.match(text.strip())
    if not m:
        return None
    ticker = m.group(1).upper()
    body = m.group(2)
    as_of = m.group(3)

    # Tier detection drives card colour (higher = stronger signal)
    tier = 1
    tone = "neutral"  # bullish / bearish / neutral / mixed

    triggers = []

    if _TL_GOLDEN.search(body):
        tier = 4
        tone = "bullish"
        triggers.append({"label": "Golden Cross", "kind": "cross_bull"})
    if _TL_DEATH.search(body):
        tier = 4
        tone = "bearish"
        triggers.append({"label": "Death Cross", "kind": "cross_bear"})

    m200 = _TL_200D.search(body)
    if m200:
        pct = float(m200.group(1))
        if tier < 4:
            tier = 4
        if pct >= 0:
            tone = tone if tone == "bearish" else "bullish"
            triggers.append({"label": f"200d SMA {pct:+.1f}%", "kind": "sma200_bull"})
        else:
            tone = tone if tone == "bullish" else "bearish"
            triggers.append({"label": f"200d SMA {pct:+.1f}%", "kind": "sma200_bear"})

    if _TL_52H.search(body):
        if tier < 3:
            tier = 3
        tone = tone if tone == "bearish" else "bullish"
        triggers.append({"label": "Near 52w High", "kind": "52h"})
    if _TL_52L.search(body):
        if tier < 3:
            tier = 3
        tone = tone if tone == "bullish" else "bearish"
        triggers.append({"label": "Near 52w Low", "kind": "52l"})

    m50 = _TL_50D.search(body)
    if m50:
        pct = float(m50.group(1))
        if tier < 2:
            tier = 2
        if pct >= 0:
            tone = tone if tone == "bearish" else "bullish"
            triggers.append({"label": f"50d SMA {pct:+.1f}%", "kind": "sma50_bull"})
        else:
            tone = tone if tone == "bullish" else "bearish"
            triggers.append({"label": f"50d SMA {pct:+.1f}%", "kind": "sma50_bear"})

    rsi_os = _TL_RSI_OS.search(body)
    if rsi_os:
        triggers.append({"label": f"RSI {rsi_os.group(1)} Oversold", "kind": "rsi_os"})
    rsi_ob = _TL_RSI_OB.search(body)
    if rsi_ob:
        triggers.append({"label": f"RSI {rsi_ob.group(1)} Overbought", "kind": "rsi_ob"})

    vol = _TL_VOL.search(body)
    if vol:
        triggers.append({"label": f"Vol {float(vol.group(1)):.1f}× Avg", "kind": "vol"})

    gap_up = _TL_GAP_UP.search(body)
    if gap_up:
        triggers.append({"label": f"Gap-Up +{gap_up.group(1)}%", "kind": "gap_up"})
    gap_dn = _TL_GAP_DN.search(body)
    if gap_dn:
        triggers.append({"label": f"Gap-Down {gap_dn.group(1)}%", "kind": "gap_dn"})

    if not triggers:
        return None

    # Cross ↔ contradictory SMA can show "mixed" — only when both cross types present
    if any(t["kind"] == "cross_bull" for t in triggers) and any(t["kind"] == "cross_bear" for t in triggers):
        tone = "mixed"

    return {
        "ticker": ticker,
        "tier": tier,
        "tone": tone,
        "triggers": triggers,
        "as_of": as_of,
    }


def collect_tech_levels(c, lookback_days: int = 7, ticker_cap: int = 20) -> dict:
    """Aggregate the most recent technical trigger per ticker from TechnicalLevelsAdapter.

    Pulls source='tech_level' raw_items from the last `lookback_days`, keeps
    freshest per ticker, and returns a render-ready envelope sorted by
    descending tier then by tone severity (bearish before bullish before neutral).
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = (c.table("raw_items")
                .select("text,fetched_at,url")
                .eq("source", "tech_level")
                .gte("fetched_at", cutoff)
                .order("fetched_at", desc=True)
                .limit(300)
                .execute().data or [])
    except Exception:
        rows = []

    latest: dict[str, dict] = {}
    for r in rows:
        p = _tl_parse(r.get("text"))
        if not p:
            continue
        tk = p["ticker"]
        if tk not in latest:
            p["fetched_at"] = r.get("fetched_at")
            p["url"] = r.get("url")
            latest[tk] = p

    tone_order = {"bearish": 0, "mixed": 1, "bullish": 2, "neutral": 3}
    out = sorted(
        latest.values(),
        key=lambda r: (-r["tier"], tone_order.get(r["tone"], 9))
    )[:ticker_cap]

    n_bull = sum(1 for r in out if r["tone"] == "bullish")
    n_bear = sum(1 for r in out if r["tone"] == "bearish")
    n_tier4 = sum(1 for r in out if r["tier"] == 4)

    stale = False
    if out:
        try:
            newest = max(
                datetime.fromisoformat(str(r["fetched_at"]).replace("Z", "+00:00"))
                for r in out if r.get("fetched_at")
            )
            stale = (datetime.now(timezone.utc) - newest).days > 3
        except Exception:
            stale = False

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lookback_days": lookback_days,
        "tickers": out,
        "n_bull": n_bull,
        "n_bear": n_bear,
        "n_tier4": n_tier4,
        "stale": stale,
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
        # Quality-Scorecard fundamentals (HED-137 Zyklus 111): margin profile,
        # FCF productivity, valuation multiples. Bloomberg-FA: Rule-of-40 = Rev
        # Growth % + FCF Margin % — the canonical SaaS/cloud quality screen.
        if info.get("grossMargins") is not None:
            result["gross_margin"] = round(float(info["grossMargins"]) * 100, 1)
        if info.get("operatingMargins") is not None:
            result["op_margin"] = round(float(info["operatingMargins"]) * 100, 1)
        if info.get("profitMargins") is not None:
            result["net_margin"] = round(float(info["profitMargins"]) * 100, 1)
        fcf = info.get("freeCashflow")
        rev = info.get("totalRevenue") or info.get("revenue")
        if fcf is not None and rev:
            try:
                if float(rev) > 0:
                    result["fcf_margin"] = round(float(fcf) / float(rev) * 100, 1)
            except Exception:
                pass
        if info.get("forwardPE") is not None:
            try:
                fpe = float(info["forwardPE"])
                if 0 < fpe < 1000:
                    result["fwd_pe"] = round(fpe, 1)
            except Exception:
                pass
        if info.get("trailingPE") is not None:
            try:
                tpe = float(info["trailingPE"])
                if 0 < tpe < 1000:
                    result["trail_pe"] = round(tpe, 1)
            except Exception:
                pass
        if info.get("enterpriseToRevenue") is not None:
            try:
                evs = float(info["enterpriseToRevenue"])
                if 0 < evs < 100:
                    result["ev_sales"] = round(evs, 1)
            except Exception:
                pass
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
/* Thesis cards: per-call mini-chart + scenario grid (HED-137 cycle 114) — replaces
   tr-pending table for active calls. Mobile-first single column, 2-col on wider. */
.thp-cap{margin-top:var(--s4);margin-bottom:var(--s2);font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:600}
.thp-grid{display:grid;grid-template-columns:1fr;gap:var(--s3)}
@media (min-width:780px){.thp-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.thp-card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:var(--s3);display:flex;flex-direction:column;gap:var(--s2)}
.thp-h{display:flex;justify-content:space-between;align-items:center;gap:var(--s2);flex-wrap:wrap}
.thp-h-left{display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0}
.thp-h-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.thp-tickers{display:inline-flex;gap:4px;font-weight:700;font-size:var(--fs-body);letter-spacing:.02em}
.thp-tk{color:var(--txt);text-decoration:none;border-bottom:1px dotted rgba(138,160,189,.4)}
.thp-tk:hover{color:var(--accent);border-bottom-color:var(--accent)}
.thp-dir{font-size:var(--fs-micro);padding:1px 6px;border-radius:4px;letter-spacing:.04em}
.thp-hz{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.04em;color:var(--mut);border:1px solid var(--line);padding:1px 5px;border-radius:4px}
.thp-pnl{font-variant-numeric:tabular-nums;font-weight:700;font-size:var(--fs-body);padding:2px 8px;border-radius:6px}
.thp-pnl-pos{color:var(--green);background:rgba(63,185,80,.10)}
.thp-pnl-neg{color:var(--red);background:rgba(248,81,73,.10)}
.thp-pnl-unp{color:var(--mut);background:var(--panel)}
.thp-conv{font-variant-numeric:tabular-nums;font-weight:600;font-size:var(--fs-cap);padding:2px 7px;border-radius:6px;background:var(--panel);border:1px solid var(--line)}
.thp-conv.conv-hi{color:var(--green);border-color:rgba(63,185,80,.4)}
.thp-conv.conv-mid{color:var(--amber);border-color:rgba(210,153,34,.4)}
.thp-conv.conv-lo{color:var(--mut)}
.thp-label{font-size:var(--fs-body);font-weight:600;color:var(--txt);line-height:1.35}
.thp-chart{position:relative;height:64px;width:100%;background:var(--panel);border-radius:6px;overflow:hidden}
.thp-chart-svg{width:100%;height:100%;display:block}
.thp-chart-empty{display:flex;align-items:center;justify-content:center;font-size:var(--fs-cap)}
.thp-meta{display:flex;flex-wrap:wrap;gap:var(--s2) var(--s3);font-size:var(--fs-cap);color:var(--mut);font-variant-numeric:tabular-nums;align-items:baseline}
.thp-base,.thp-cur,.thp-range,.thp-score{white-space:nowrap}
.thp-base{color:var(--txt)}
.thp-cur{color:var(--txt)}
.thp-score{margin-left:auto}
.thp-score b{color:var(--txt);font-weight:600}
.thp-scen{display:flex;flex-direction:column;gap:3px;margin-top:var(--s1);border-top:1px solid var(--line);padding-top:var(--s2)}
.thp-sc-row{display:grid;grid-template-columns:36px 70px auto 1fr;gap:8px;align-items:center;font-size:var(--fs-cap);line-height:1.3}
.thp-sc-lbl{font-size:var(--fs-micro);font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.thp-sc-bull .thp-sc-lbl{color:var(--green)}
.thp-sc-base .thp-sc-lbl{color:var(--accent)}
.thp-sc-bear .thp-sc-lbl{color:var(--red)}
.thp-sc-prob{position:relative;display:inline-block;height:14px;background:var(--panel);border:1px solid var(--line);border-radius:3px;overflow:hidden}
.thp-sc-pbar{position:absolute;left:0;top:0;bottom:0;background:rgba(138,160,189,.25)}
.thp-sc-bull .thp-sc-pbar{background:rgba(63,185,80,.35)}
.thp-sc-base .thp-sc-pbar{background:rgba(77,163,255,.35)}
.thp-sc-bear .thp-sc-pbar{background:rgba(248,81,73,.35)}
.thp-sc-pn{position:relative;display:inline-block;padding:0 4px;font-size:var(--fs-micro);font-variant-numeric:tabular-nums;color:var(--txt);font-weight:600}
.thp-sc-tgt{font-variant-numeric:tabular-nums;color:var(--txt);font-weight:600}
.thp-sc-trig{color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.thp-exit{font-size:var(--fs-cap);color:var(--mut);display:flex;gap:6px;align-items:baseline}
.thp-exit-lbl{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.05em;color:var(--amber);font-weight:700}
.thp-devil{font-size:var(--fs-cap);line-height:1.4;color:var(--txt);padding:var(--s2);border-radius:6px;border:1px solid var(--line);background:var(--panel)}
.thp-devil-reject{border-color:rgba(248,81,73,.4);background:rgba(248,81,73,.06)}
.thp-devil-caution{border-color:rgba(210,153,34,.35);background:rgba(210,153,34,.05)}
.thp-devil-lbl{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.05em;font-weight:700;color:var(--amber)}
.thp-devil-reject .thp-devil-lbl{color:var(--red)}
.thp-devil-note{color:var(--mut)}
@media (max-width:430px){
  .thp-h-right{width:100%;justify-content:flex-end}
  .thp-sc-row{grid-template-columns:32px 56px auto;}
  .thp-sc-trig{display:none}
  .thp-score{margin-left:0}
}
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
/* Fund-Performance Summary Bar (HED-137 Zyklus 115) — above-the-fold trust signal.
   Bloomberg PRTU-style: headline P&L number + active-book context. Visible on every load,
   before the investor scrolls. No skeleton — renders synchronously from embedded JSON. */
#fund-summary-bar{margin-bottom:var(--s3)}
.fsb{display:grid;grid-template-columns:auto 1fr auto;align-items:stretch;
  background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.fsb-main{display:flex;flex-wrap:wrap;align-items:center;gap:0;padding:var(--s3) var(--s4);
  border-right:1px solid var(--line);min-width:0}
.fsb-pnl-lbl{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.08em;font-weight:700;
  color:var(--mut);margin-right:var(--s2)}
.fsb-pnl-val{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1;margin-right:var(--s3)}
.fsb-pnl-pos{color:var(--green)}
.fsb-pnl-neg{color:var(--red)}
.fsb-pnl-flat{color:var(--mut)}
.fsb-pnl-tag{font-size:var(--fs-micro);color:var(--mut);display:block;margin-top:2px}
.fsb-stats{display:flex;flex-wrap:wrap;gap:0;align-items:stretch;padding:0 var(--s2);flex:1;min-width:0}
.fsb-stat{display:flex;flex-direction:column;justify-content:center;padding:var(--s3) var(--s4);
  border-right:1px solid var(--line);min-width:0;flex-shrink:0}
.fsb-stat:last-child{border-right:none}
.fsb-stat-lbl{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.07em;color:var(--mut);font-weight:600;white-space:nowrap}
.fsb-stat-val{font-size:var(--fs-body);font-weight:700;font-variant-numeric:tabular-nums;color:var(--txt);white-space:nowrap;margin-top:2px}
.fsb-stat-val.move-up{color:var(--green)}
.fsb-stat-val.move-dn{color:var(--red)}
.fsb-spark{display:flex;align-items:center;padding:var(--s3) var(--s4)}
.fsb-spark-svg{display:block;flex-shrink:0}
@media (max-width:760px){
  .fsb{grid-template-columns:1fr}
  .fsb-main{border-right:none;border-bottom:1px solid var(--line)}
  .fsb-stat{border-right:none;border-bottom:1px solid var(--line)}
  .fsb-stat:last-child{border-bottom:none}
  .fsb-spark{display:none}
}
@media (max-width:430px){
  .fsb-pnl-val{font-size:22px}
  .fsb-stats{gap:0}
  .fsb-stat{padding:var(--s2) var(--s3)}
}
/* Heute auf dem Tape (HED-137 Zyklus 116) — Bloomberg TOP-style daily action triage.
   Above-the-fold companion to FSB: today's book pulse, biggest book winner/loser, hottest
   universe mover. Answers "what should I look at first?" without scrolling through 16 panels. */
#todays-tape{margin:var(--s3) 0 var(--s4)}
.tm{display:grid;grid-template-columns:1.1fr 1fr 1fr 1fr;gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:12px;overflow:hidden}
.tm-cell{background:var(--panel);padding:var(--s3) var(--s4);display:flex;flex-direction:column;
  justify-content:space-between;min-height:88px;min-width:0}
.tm-cell--pulse{background:linear-gradient(180deg,var(--panel) 0%,rgba(77,163,255,.04) 100%)}
.tm-h{display:flex;align-items:baseline;justify-content:space-between;gap:var(--s2);min-width:0}
.tm-lbl{font-size:var(--fs-micro);text-transform:uppercase;letter-spacing:.08em;font-weight:700;color:var(--mut);white-space:nowrap}
.tm-tag{font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;
  padding:1px 5px;border-radius:3px;border:1px solid var(--line);background:var(--panel2);white-space:nowrap;flex-shrink:0}
.tm-tag--book{color:var(--accent);border-color:rgba(77,163,255,.32);background:rgba(77,163,255,.08)}
.tm-tag--univ{color:var(--mut)}
.tm-val{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1;letter-spacing:-.01em;
  display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;min-width:0}
.tm-val .tm-tk{font-size:16px;color:var(--txt);font-weight:700;letter-spacing:0}
.tm-val .tm-dir{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;letter-spacing:.05em;align-self:center}
.tm-val .tm-dir-l{background:rgba(77,163,255,.18);color:var(--accent);border:1px solid rgba(77,163,255,.32)}
.tm-val .tm-dir-s{background:rgba(248,112,90,.18);color:#f78166;border:1px solid rgba(248,112,90,.32)}
.tm-pos{color:var(--green)}
.tm-neg{color:var(--red)}
.tm-flat{color:var(--mut)}
.tm-sub{font-size:var(--fs-micro);color:var(--mut);line-height:1.35;font-variant-numeric:tabular-nums;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-sub b{color:var(--txt);font-weight:600}
.tm-empty{color:var(--mut);font-style:italic;font-size:var(--fs-cap)}
.tm-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s2);text-align:right;line-height:1.4}
@media (max-width:900px){
  .tm{grid-template-columns:1fr 1fr}
  .tm-cell{min-height:76px}
}
@media (max-width:430px){
  .tm-val{font-size:18px}
  .tm-val .tm-tk{font-size:14px}
  .tm-cell{padding:var(--s2) var(--s3);min-height:68px}
}
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
/* Macro-Pulse — Bloomberg-MAC-style market-context strip (HED-137 Zyklus 106).
   Dense top-of-page ribbon: 9 macro tiles + a one-line risk-regime verdict.
   Stranger-glance test: "what is the market doing right now?" — answered before
   the briefing fold. Pre-attentive coding via the verdict ribbon (green/amber/red)
   and per-tile risk dots (only adverse buckets are coloured to keep noise low). */
.mp-panel{padding:var(--s3);margin-bottom:var(--s4)}
.mp-verdict{display:flex;align-items:center;gap:var(--s3);flex-wrap:wrap;
  padding:10px 14px;border-radius:6px;border:1px solid var(--line);
  background:var(--panel2);margin-bottom:var(--s3)}
.mp-verdict-dot{width:12px;height:12px;border-radius:50%;flex:0 0 12px;box-shadow:0 0 0 3px rgba(255,255,255,.04)}
.mp-verdict-dot.g{background:var(--green)}
.mp-verdict-dot.n{background:var(--mut)}
.mp-verdict-dot.a{background:var(--amber)}
.mp-verdict-dot.r{background:var(--red)}
.mp-verdict-lbl{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);font-weight:600}
.mp-verdict-val{font-weight:800;font-size:15px;letter-spacing:.01em;color:var(--txt);line-height:1}
.mp-verdict-val.g{color:var(--green)}
.mp-verdict-val.a{color:var(--amber)}
.mp-verdict-val.r{color:var(--red)}
.mp-verdict-tr{color:var(--mut);font-size:var(--fs-cap);font-variant-numeric:tabular-nums;line-height:1.3;flex:1;min-width:200px}
.mp-verdict-tr b{color:var(--txt);font-weight:600}
.mp-verdict-asof{font-size:10px;color:var(--mut);font-variant-numeric:tabular-nums;letter-spacing:.04em;text-transform:uppercase;font-weight:600;white-space:nowrap}
.mp-grid{display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:var(--s2)}
.mp-tile{position:relative;padding:9px 10px 8px;border-radius:6px;border:1px solid var(--line);
  background:var(--panel2);font-variant-numeric:tabular-nums;cursor:help;
  display:flex;flex-direction:column;gap:2px;min-width:0;transition:border-color .12s}
.mp-tile:hover{border-color:var(--accent)}
.mp-tile.tone-g{border-left:3px solid var(--green)}
.mp-tile.tone-a{border-left:3px solid var(--amber)}
.mp-tile.tone-r{border-left:3px solid var(--red)}
.mp-tile.tone-{border-left:3px solid transparent}
.mp-tile-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);font-weight:700;line-height:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mp-tile-val{font-size:18px;font-weight:700;color:var(--txt);letter-spacing:-.01em;line-height:1.1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mp-tile-d{font-size:10px;font-weight:600;letter-spacing:.01em;line-height:1.1;display:flex;align-items:center;gap:3px}
.mp-tile-d.pos{color:var(--green)}
.mp-tile-d.neg{color:var(--red)}
.mp-tile-d.neu{color:var(--mut)}
.mp-tile-arrow{font-size:9px;line-height:1}
.mp-tile-asof{font-size:9px;color:var(--mut);opacity:.8;letter-spacing:.02em;margin-top:1px;font-variant-numeric:tabular-nums}
.mp-tile.empty .mp-tile-val{color:var(--mut);opacity:.5}
.mp-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.mp-foot b{color:var(--txt);font-weight:600}
.mp-stale{color:var(--amber);font-weight:600}
@media(max-width:980px){
  .mp-grid{grid-template-columns:repeat(5,minmax(0,1fr))}
}
@media(max-width:640px){
  .mp-panel{padding:var(--s2)}
  .mp-verdict{padding:8px 10px;gap:var(--s2)}
  .mp-verdict-val{font-size:13px}
  .mp-verdict-tr{font-size:var(--fs-micro);min-width:0;flex-basis:100%}
  .mp-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
  .mp-tile{padding:7px 8px 6px}
  .mp-tile-val{font-size:15px}
  .mp-tile-d{font-size:9px}
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
/* Signal-Matrix (HED-137 Zyklus 109): Bloomberg MOST-screen equivalent — per open-call
   synthesis table that joins Tech Setup, Options Tape, Vol-Edge, and Insider signals into
   one composite view. The PM sees at a glance: which positions have all signals aligned
   (high conviction) vs which have conflicting signals (review needed). Composite score
   (confirms − conflicts) drives a book-wide alignment verdict. Each signal cell is a
   color-coded pill so the dominant posture reads preattentively without parsing numbers.
   Mobile: outer scrolls horizontally; all 4 signal columns + composite always visible. */
.sm-panel{padding:var(--s3);margin-top:0}
.sm-h{margin-bottom:var(--s3)}
.sm-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.sm-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px;line-height:1.4}
.sm-wrap{overflow-x:auto;margin:0 calc(-1*var(--s3));padding:0 var(--s3)}
.sm-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.sm-tbl thead th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;text-align:left;padding:6px 8px 6px 6px;border-bottom:2px solid var(--line);white-space:nowrap}
.sm-tbl thead th.c{text-align:center}
.sm-tbl thead th.r{text-align:right}
.sm-tbl tbody tr{border-bottom:1px solid var(--panel2)}
.sm-tbl tbody tr:last-child{border-bottom:none}
.sm-tbl tbody tr:hover{background:rgba(77,163,255,.04)}
.sm-tbl tbody td{padding:8px 8px 8px 6px;vertical-align:middle;line-height:1.25}
.sm-tbl tbody td.c{text-align:center}
.sm-tbl tbody td.r{text-align:right}
/* Ticker + direction cell */
.sm-tk-wrap{display:flex;align-items:center;gap:6px}
.sm-tk{font-weight:700;font-size:13px;letter-spacing:.03em;color:var(--txt)}
.sm-dir{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em}
.sm-dir-long{background:rgba(63,185,80,.22);color:var(--green)}
.sm-dir-short{background:rgba(248,81,73,.22);color:var(--red)}
.sm-dir-pair{background:var(--panel2);color:var(--mut)}
/* Conviction bar + value */
.sm-conv-cell{display:flex;align-items:center;gap:6px;min-width:70px}
.sm-conv-bar{height:4px;border-radius:2px;min-width:36px;position:relative;background:var(--panel2)}
.sm-conv-fill{position:absolute;top:0;bottom:0;left:0;border-radius:2px}
.sm-conv-fill-hi{background:var(--accent)}
.sm-conv-fill-lo{background:var(--mut)}
.sm-conv-val{font-size:var(--fs-micro);font-weight:600;color:var(--mut);min-width:24px}
/* Signal pills — the core vocabulary of this panel */
.sm-sig{display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;
  padding:3px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;
  min-width:64px;line-height:1.3;cursor:default;gap:3px}
.sm-sig-conf{background:rgba(63,185,80,.20);color:var(--green)}          /* confirms call */
.sm-sig-conf::before{content:"✓\00a0"}
.sm-sig-conflict{background:rgba(248,81,73,.18);color:var(--red)}         /* contradicts call */
.sm-sig-conflict::before{content:"✗\00a0"}
.sm-sig-watch{background:rgba(210,153,34,.18);color:var(--amber)}         /* mixed / watch */
.sm-sig-watch::before{content:"◐\00a0"}
.sm-sig-none{background:var(--panel2);color:var(--mut)}                   /* no data */
/* Composite score cell */
.sm-comp{display:inline-flex;align-items:center;justify-content:center;gap:4px;
  font-size:13px;font-weight:800;min-width:36px}
.sm-comp-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.sm-comp-hi{color:var(--green)}.sm-comp-hi .sm-comp-dot{background:var(--green)}
.sm-comp-lo{color:var(--red)}.sm-comp-lo .sm-comp-dot{background:var(--red)}
.sm-comp-mid{color:var(--amber)}.sm-comp-mid .sm-comp-dot{background:var(--amber)}
.sm-comp-nil{color:var(--mut)}.sm-comp-nil .sm-comp-dot{background:var(--mut)}
.sm-verdict-bar{display:flex;flex-wrap:wrap;gap:var(--s2);align-items:center;margin-top:var(--s3);padding-top:var(--s3);border-top:1px solid var(--panel2)}
.sm-verdict-chip{font-size:11px;font-weight:700;padding:4px 10px;border-radius:5px;text-transform:uppercase;letter-spacing:.04em}
.sm-verdict-aligned{background:rgba(63,185,80,.18);color:var(--green)}
.sm-verdict-split{background:rgba(210,153,34,.18);color:var(--amber)}
.sm-verdict-conflict{background:rgba(248,81,73,.18);color:var(--red)}
.sm-verdict-insuf{background:var(--panel2);color:var(--mut)}
.sm-verdict-meta{font-size:var(--fs-micro);color:var(--mut);line-height:1.5}
.sm-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.sm-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);border:1px dashed var(--line);border-radius:6px;color:var(--mut);font-size:var(--fs-micro);font-style:italic}
.sm-col-tech,.sm-col-opt,.sm-col-vol,.sm-col-ins{white-space:nowrap}
@media(max-width:640px){
  .sm-tbl thead th{font-size:9px;padding:5px 5px}
  .sm-tbl tbody td{padding:7px 5px}
  .sm-sig{font-size:9px;padding:2px 5px;min-width:54px}
  .sm-tk{font-size:12px}
  .sm-comp{font-size:12px}
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
/* Options-Tape (HED-137 Zyklus 107): Bloomberg-OMON-style institutional-positioning panel.
   Per-ticker row showing expected move ±% with expiry, P/C OI ratio (bull/bear badge),
   ATM IV skew (put-bid / call-bid badge), verdict tag and a book cross-ref overlay
   (✓ positioning confirms our call direction, ⚠ contradicts). Tone-coloured left border
   gives the loudest signal preattentive treatment. Sorted by absolute signal magnitude. */
.ot-panel{padding:var(--s3);margin-top:var(--s3)}
.ot-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.ot-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.ot-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.ot-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.ot-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;min-width:54px}
.ot-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.ot-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.ot-metric .val.pos{color:var(--green)}
.ot-metric .val.neg{color:var(--red)}
.ot-metric .val.amb{color:var(--amber)}
.ot-table-wrap{overflow-x:auto;margin:0 calc(-1*var(--s3));padding:0 var(--s3)}
.ot-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.ot-table thead th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;text-align:left;padding:6px 8px 6px 8px;border-bottom:1px solid var(--line);background:transparent;white-space:nowrap}
.ot-table thead th.r{text-align:right}
.ot-table thead th.c{text-align:center}
.ot-table tbody tr{border-bottom:1px solid var(--line)}
.ot-table tbody tr:last-child{border-bottom:0}
.ot-table tbody tr:hover{background:rgba(77,163,255,.05)}
.ot-table tbody td{padding:7px 8px;vertical-align:middle;line-height:1.25}
.ot-table tbody td.r{text-align:right}
.ot-table tbody td.c{text-align:center}
.ot-tone-g{box-shadow:inset 3px 0 0 var(--green)}
.ot-tone-r{box-shadow:inset 3px 0 0 var(--red)}
.ot-tone-a{box-shadow:inset 3px 0 0 var(--amber)}
.ot-tone-n{box-shadow:inset 3px 0 0 var(--line)}
.ot-tk{font-weight:700;font-size:var(--fs-cap);letter-spacing:.02em;color:var(--txt)}
.ot-tk-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.ot-book{display:inline-flex;align-items:center;gap:3px;font-size:9px;font-weight:700;letter-spacing:.04em;padding:1px 6px;border-radius:3px;text-transform:uppercase;white-space:nowrap;line-height:1.4}
.ot-book-conf{background:rgba(63,185,80,.18);color:var(--green)}
.ot-book-conf::before{content:"✓\00a0"}
.ot-book-conflict{background:rgba(248,81,73,.18);color:var(--red)}
.ot-book-conflict::before{content:"⚠\00a0"}
.ot-book-watch{background:var(--panel2);color:var(--mut)}
.ot-em{font-weight:700;font-size:var(--fs-cap);color:var(--txt);font-variant-numeric:tabular-nums}
.ot-em.hi{color:var(--amber)}
.ot-em-exp{display:block;font-size:9px;color:var(--mut);font-weight:500;margin-top:1px;letter-spacing:.02em;text-transform:uppercase}
.ot-pc-cell{display:inline-flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}
.ot-pc-val{font-weight:700;font-size:var(--fs-cap);color:var(--txt);min-width:34px;text-align:right}
.ot-pc-badge{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em}
.ot-pc-bull{background:rgba(63,185,80,.18);color:var(--green)}
.ot-pc-bear{background:rgba(248,81,73,.18);color:var(--red)}
.ot-skew-cell{display:inline-flex;align-items:center;gap:6px}
.ot-skew-val{font-weight:700;font-size:var(--fs-cap);color:var(--txt);font-variant-numeric:tabular-nums;min-width:48px;text-align:right}
.ot-skew-val.hi{color:var(--red)}
.ot-skew-val.lo{color:var(--amber)}
.ot-skew-badge{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.ot-skew-put{background:rgba(248,81,73,.15);color:var(--red)}
.ot-skew-call{background:rgba(210,153,34,.18);color:var(--amber)}
.ot-vd{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;display:inline-block}
.ot-vd-bullish_setup{background:rgba(63,185,80,.22);color:var(--green)}
.ot-vd-squeeze_risk{background:rgba(210,153,34,.22);color:var(--amber)}
.ot-vd-bearish_setup{background:rgba(248,81,73,.22);color:var(--red)}
.ot-vd-hedge_bid{background:rgba(248,81,73,.15);color:var(--red)}
.ot-vd-event_pending{background:rgba(210,153,34,.18);color:var(--amber)}
.ot-vd-neutral{background:var(--panel2);color:var(--mut)}
.ot-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.ot-foot a{color:var(--mut);border-bottom:1px dotted var(--line)}
.ot-foot a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.ot-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);background:transparent;border-radius:6px;border:1px dashed var(--line);color:var(--mut);font-size:var(--fs-micro);font-style:italic;text-align:center}
@media(max-width:640px){
  .ot-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .ot-metrics{justify-content:space-between;gap:var(--s3)}
  .ot-metric{text-align:left;min-width:0;flex:1}
  .ot-metric .val{font-size:16px}
  .ot-table thead th{font-size:9px;padding:5px 5px}
  .ot-table tbody td{padding:6px 5px}
  .ot-table .col-hide-m{display:none}
  .ot-pc-val{min-width:28px}
  .ot-skew-val{min-width:36px}
  .ot-vd{font-size:9px;padding:1px 5px}
  .ot-em{font-size:var(--fs-micro)}
}
/* IV-vs-RV Edge (HED-137 Zyklus 108): Bloomberg HVR / IVOL screen equivalent.
   Pricing-side complement to Options-Tape: where Options-Tape shows institutional
   *positioning* (P/C, skew), this shows whether options are *expensive vs. realized*.
   IV-annualized = emove/79.79 × √(252/DTE) (Brenner-Subrahmanyam ATM straddle approx);
   RV30 = stdev(log-returns 30d) × √252; spread = IV − RV in pp. Positive ≫ → premium
   bid (sell vol / outright preferred over calls), negative ≫ → premium discount
   (event-protection cheap, catalyst plays favored). Same row-styling vocabulary as
   Options-Tape so the two panels read as one institutional vol view. */
.iv-panel{padding:var(--s3);margin-top:var(--s3)}
.iv-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.iv-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.iv-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.iv-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.iv-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;min-width:54px}
.iv-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.iv-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.iv-metric .val.pos{color:var(--red)}    /* expensive: red flag */
.iv-metric .val.neg{color:var(--green)}  /* cheap: green opportunity */
.iv-metric .val.amb{color:var(--amber)}
.iv-table-wrap{overflow-x:auto;margin:0 calc(-1*var(--s3));padding:0 var(--s3)}
.iv-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.iv-table thead th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.iv-table thead th.r{text-align:right}
.iv-table tbody tr{border-bottom:1px solid var(--line)}
.iv-table tbody tr:last-child{border-bottom:0}
.iv-table tbody tr:hover{background:rgba(77,163,255,.05)}
.iv-table tbody td{padding:7px 8px;vertical-align:middle;line-height:1.25}
.iv-table tbody td.r{text-align:right}
.iv-tone-r{box-shadow:inset 3px 0 0 var(--red)}     /* expensive premium */
.iv-tone-g{box-shadow:inset 3px 0 0 var(--green)}   /* cheap premium */
.iv-tone-n{box-shadow:inset 3px 0 0 var(--line)}
.iv-tk{font-weight:700;font-size:var(--fs-cap);letter-spacing:.02em;color:var(--txt)}
.iv-tk-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.iv-book{display:inline-flex;align-items:center;gap:3px;font-size:9px;font-weight:700;letter-spacing:.04em;padding:1px 6px;border-radius:3px;text-transform:uppercase;white-space:nowrap;line-height:1.4}
.iv-book-long{background:rgba(63,185,80,.18);color:var(--green)}
.iv-book-long::before{content:"L\00a0"}
.iv-book-short{background:rgba(248,81,73,.18);color:var(--red)}
.iv-book-short::before{content:"S\00a0"}
.iv-book-pair{background:var(--panel2);color:var(--mut)}
.iv-book-pair::before{content:"±\00a0"}
.iv-num{font-weight:700;font-size:var(--fs-cap);color:var(--txt);font-variant-numeric:tabular-nums}
.iv-num.mut{color:var(--mut);font-weight:500}
.iv-bar-cell{min-width:120px}
/* Symmetric divergent bar: anchor at center, expensive →red→right, cheap →green→left */
.iv-bar-wrap{position:relative;height:14px;background:var(--panel2);border-radius:3px;overflow:hidden}
.iv-bar-axis{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line);z-index:1}
.iv-bar{position:absolute;top:1px;bottom:1px;border-radius:2px}
.iv-bar-pos{background:linear-gradient(90deg,rgba(248,81,73,.45),rgba(248,81,73,.85));left:50%}
.iv-bar-neg{background:linear-gradient(270deg,rgba(63,185,80,.45),rgba(63,185,80,.85));right:50%}
.iv-spr-cell{display:flex;align-items:center;justify-content:flex-end;gap:6px;font-variant-numeric:tabular-nums}
.iv-spr-val{font-weight:700;min-width:54px;text-align:right}
.iv-spr-val.pos{color:var(--red)}
.iv-spr-val.neg{color:var(--green)}
.iv-vd{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;display:inline-block}
.iv-vd-exp{background:rgba(248,81,73,.22);color:var(--red)}
.iv-vd-cheap{background:rgba(63,185,80,.22);color:var(--green)}
.iv-vd-fair{background:var(--panel2);color:var(--mut)}
.iv-actn{font-size:9px;color:var(--mut);font-style:italic;display:block;margin-top:2px;letter-spacing:.01em;line-height:1.3}
.iv-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.iv-foot a{color:var(--mut);border-bottom:1px dotted var(--line)}
.iv-foot a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.iv-foot i{font-style:italic}
.iv-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);background:transparent;border-radius:6px;border:1px dashed var(--line);color:var(--mut);font-size:var(--fs-micro);font-style:italic;text-align:center}
@media(max-width:640px){
  .iv-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .iv-metrics{justify-content:space-between;gap:var(--s3)}
  .iv-metric{text-align:left;min-width:0;flex:1}
  .iv-metric .val{font-size:16px}
  .iv-table thead th{font-size:9px;padding:5px 5px}
  .iv-table tbody td{padding:6px 5px}
  .iv-table .col-hide-m{display:none}
  .iv-bar-cell{min-width:80px}
  .iv-spr-val{min-width:42px}
  .iv-vd{font-size:9px;padding:1px 5px}
  .iv-actn{font-size:8px}
}
/* Short-Squeeze-Pressure (HED-137 Zyklus 110): Bloomberg-SI-equivalent short-interest
   panel. Per-ticker row showing SI% of float as a saturating horizontal bar (longer +
   darker = more crowded short), MoM-change arrow with magnitude colored by direction
   (↑ red = building bearish positioning, ↓ green = covering), bucket badge (low /
   elevated / high / extreme) and a verdict tag. Open-call cross-ref: long against a
   high-SI name = squeeze tailwind (✓ green), short against high-SI = crowded-short
   risk (⚠ amber). Shares vocabulary with Options-Tape so the positioning panels
   read as one institutional suite. */
.ss-panel{padding:var(--s3);margin-top:var(--s3)}
.ss-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.ss-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.ss-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.ss-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.ss-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;min-width:54px}
.ss-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.ss-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.ss-metric .val.pos{color:var(--red)}     /* high SI / squeeze risk = red flag */
.ss-metric .val.neg{color:var(--green)}   /* covering = green */
.ss-metric .val.amb{color:var(--amber)}
.ss-table-wrap{overflow-x:auto;margin:0 calc(-1*var(--s3));padding:0 var(--s3)}
.ss-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.ss-table thead th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.ss-table thead th.r{text-align:right}
.ss-table tbody tr{border-bottom:1px solid var(--line)}
.ss-table tbody tr:last-child{border-bottom:0}
.ss-table tbody tr:hover{background:rgba(77,163,255,.05)}
.ss-table tbody td{padding:7px 8px;vertical-align:middle;line-height:1.25}
.ss-table tbody td.r{text-align:right}
.ss-tone-r{box-shadow:inset 3px 0 0 var(--red)}
.ss-tone-a{box-shadow:inset 3px 0 0 var(--amber)}
.ss-tone-g{box-shadow:inset 3px 0 0 var(--green)}
.ss-tone-n{box-shadow:inset 3px 0 0 var(--line)}
.ss-tk{font-weight:700;font-size:var(--fs-cap);letter-spacing:.02em;color:var(--txt)}
.ss-tk-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.ss-book{display:inline-flex;align-items:center;gap:3px;font-size:9px;font-weight:700;letter-spacing:.04em;padding:1px 6px;border-radius:3px;text-transform:uppercase;white-space:nowrap;line-height:1.4}
.ss-book-tailwind{background:rgba(63,185,80,.18);color:var(--green)}
.ss-book-tailwind::before{content:"✓\00a0"}
.ss-book-risk{background:rgba(210,153,34,.20);color:var(--amber)}
.ss-book-risk::before{content:"⚠\00a0"}
.ss-book-watch{background:var(--panel2);color:var(--mut)}
.ss-si{font-weight:700;font-size:var(--fs-cap);color:var(--txt);font-variant-numeric:tabular-nums;min-width:48px;text-align:right}
.ss-si.hi{color:var(--red)}
.ss-si.mid{color:var(--amber)}
/* Horizontal bar — width = SI% (cap 25% so >25 still saturates). Color shifts with bucket. */
.ss-bar-cell{min-width:120px}
.ss-bar-wrap{position:relative;height:14px;background:var(--panel2);border-radius:3px;overflow:hidden}
.ss-bar{position:absolute;left:0;top:1px;bottom:1px;border-radius:2px;background:linear-gradient(90deg,rgba(77,163,255,.45),rgba(77,163,255,.85))}
.ss-bar.b-elevated{background:linear-gradient(90deg,rgba(210,153,34,.45),rgba(210,153,34,.85))}
.ss-bar.b-high{background:linear-gradient(90deg,rgba(248,81,73,.45),rgba(248,81,73,.85))}
.ss-bar.b-extreme{background:linear-gradient(90deg,rgba(248,81,73,.65),rgba(248,81,73,1))}
.ss-mom{display:inline-flex;align-items:center;gap:3px;font-weight:700;font-size:var(--fs-cap);font-variant-numeric:tabular-nums}
.ss-mom-up{color:var(--red)}
.ss-mom-dn{color:var(--green)}
.ss-mom-mute{color:var(--mut);font-weight:500}
.ss-bk{display:inline-block;font-size:9px;font-weight:700;padding:1px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.ss-bk-low{background:var(--panel2);color:var(--mut)}
.ss-bk-elevated{background:rgba(210,153,34,.18);color:var(--amber)}
.ss-bk-high{background:rgba(248,81,73,.18);color:var(--red)}
.ss-bk-extreme{background:rgba(248,81,73,.32);color:#fff}
.ss-vd{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;display:inline-block}
.ss-vd-squeeze_risk{background:rgba(248,81,73,.22);color:var(--red)}
.ss-vd-crowded_short{background:rgba(210,153,34,.22);color:var(--amber)}
.ss-vd-building_short{background:rgba(210,153,34,.18);color:var(--amber)}
.ss-vd-covering{background:rgba(63,185,80,.22);color:var(--green)}
.ss-vd-baseline{background:var(--panel2);color:var(--mut)}
.ss-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.ss-foot a{color:var(--mut);border-bottom:1px dotted var(--line)}
.ss-foot a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.ss-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);background:transparent;border-radius:6px;border:1px dashed var(--line);color:var(--mut);font-size:var(--fs-micro);font-style:italic;text-align:center}
@media(max-width:640px){
  .ss-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .ss-metrics{justify-content:space-between;gap:var(--s3)}
  .ss-metric{text-align:left;min-width:0;flex:1}
  .ss-metric .val{font-size:16px}
  .ss-table thead th{font-size:9px;padding:5px 5px}
  .ss-table tbody td{padding:6px 5px}
  .ss-table .col-hide-m{display:none}
  .ss-bar-cell{min-width:80px}
  .ss-si{min-width:36px}
  .ss-vd{font-size:9px;padding:1px 5px}
}
/* EPS-Revisions-Velocity (HED-137 Zyklus 112): Bloomberg-EE/EM-equivalent
   sell-side estimate-revision panel. Per-ticker row: direction badge, a
   centered diverging bar where the green-right segment shows 30d upgrades
   (width = up/total) and the red-left segment 30d downgrades, 7d compact
   reads as a smaller secondary, signed drift % as the dollar-weighted impact,
   verdict pill, accel/fade/reversal tag and a book cross-ref badge. Visual
   grammar deliberately mirrors Short-Squeeze and Options-Tape so the four
   "positioning/momentum" panels read as a single suite. */
.er-panel{padding:var(--s3);margin-top:var(--s3)}
.er-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.er-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.er-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px;max-width:64ch;line-height:1.45}
.er-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.er-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;min-width:54px;cursor:help}
.er-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.er-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.er-metric .val.pos{color:var(--green)}
.er-metric .val.neg{color:var(--red)}
.er-metric .val.amb{color:var(--amber)}
.er-table-wrap{overflow-x:auto;margin:0 calc(-1*var(--s3));padding:0 var(--s3)}
.er-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.er-table thead th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.er-table thead th.r{text-align:right}
.er-table thead th.c{text-align:center}
.er-table tbody tr{border-bottom:1px solid var(--line)}
.er-table tbody tr:last-child{border-bottom:0}
.er-table tbody tr:hover{background:rgba(77,163,255,.05)}
.er-table tbody td{padding:8px 8px;vertical-align:middle;line-height:1.25}
.er-table tbody td.r{text-align:right}
.er-table tbody td.c{text-align:center}
.er-tone-g{box-shadow:inset 3px 0 0 var(--green)}
.er-tone-r{box-shadow:inset 3px 0 0 var(--red)}
.er-tone-n{box-shadow:inset 3px 0 0 var(--line)}
.er-tk{font-weight:700;font-size:var(--fs-cap);letter-spacing:.02em;color:var(--txt)}
.er-tk-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.er-book{display:inline-flex;align-items:center;gap:3px;font-size:9px;font-weight:700;letter-spacing:.04em;padding:1px 6px;border-radius:3px;text-transform:uppercase;white-space:nowrap;line-height:1.4}
.er-book-aligned{background:rgba(63,185,80,.18);color:var(--green)}
.er-book-aligned::before{content:"✓\00a0"}
.er-book-fighting{background:rgba(248,81,73,.20);color:var(--red)}
.er-book-fighting::before{content:"⚠\00a0"}
.er-book-watch{background:var(--panel2);color:var(--mut)}
.er-dir{font-size:10px;font-weight:700;padding:2px 6px;border-radius:3px;letter-spacing:.04em;white-space:nowrap;display:inline-block;text-transform:uppercase}
.er-dir-pos{background:rgba(63,185,80,.18);color:var(--green)}
.er-dir-pos::before{content:"↑\00a0"}
.er-dir-neg{background:rgba(248,81,73,.18);color:var(--red)}
.er-dir-neg::before{content:"↓\00a0"}
/* Diverging revision bar: a 110px track split at the midline. Left = down (red),
   right = up (green). Length encodes raw count, max ≈10 so 8 saturates ~80%. */
.er-bar-cell{min-width:144px}
.er-bar-wrap{position:relative;height:14px;background:var(--panel2);border:1px solid var(--line);border-radius:3px;overflow:hidden}
.er-bar-mid{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line);z-index:2}
.er-bar-up{position:absolute;left:50%;top:1px;bottom:1px;background:linear-gradient(90deg,rgba(63,185,80,.55),rgba(63,185,80,.95));border-radius:0 2px 2px 0;transition:width .15s}
.er-bar-dn{position:absolute;right:50%;top:1px;bottom:1px;background:linear-gradient(90deg,rgba(248,81,73,.95),rgba(248,81,73,.55));border-radius:2px 0 0 2px;transition:width .15s}
.er-bar-counts{display:flex;justify-content:space-between;align-items:center;margin-top:2px;font-size:10px;color:var(--mut);font-variant-numeric:tabular-nums;line-height:1}
.er-bar-counts .dn{color:var(--red);font-weight:600}
.er-bar-counts .up{color:var(--green);font-weight:600}
.er-bar-counts .mid{color:var(--mut);font-size:9px;letter-spacing:.04em;text-transform:uppercase}
.er-7d{display:inline-flex;align-items:center;gap:5px;font-variant-numeric:tabular-nums;font-size:var(--fs-cap);font-weight:600;color:var(--txt);white-space:nowrap}
.er-7d .up{color:var(--green)}
.er-7d .dn{color:var(--red)}
.er-7d .sep{color:var(--mut);font-weight:400}
.er-drift{font-weight:700;font-size:var(--fs-cap);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.er-drift.pos{color:var(--green)}
.er-drift.neg{color:var(--red)}
.er-drift.flat{color:var(--mut);font-weight:500}
.er-drift-strong{font-size:15px}
.er-eps{font-size:10px;color:var(--mut);font-variant-numeric:tabular-nums;display:block;margin-top:1px;line-height:1.2}
.er-vd{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;display:inline-block}
.er-vd-strong_tailwind{background:rgba(63,185,80,.32);color:#fff}
.er-vd-tailwind{background:rgba(63,185,80,.20);color:var(--green)}
.er-vd-breadth_pos{background:rgba(63,185,80,.10);color:var(--green)}
.er-vd-strong_headwind{background:rgba(248,81,73,.32);color:#fff}
.er-vd-headwind{background:rgba(248,81,73,.20);color:var(--red)}
.er-vd-breadth_neg{background:rgba(248,81,73,.10);color:var(--red)}
.er-accel{display:inline-block;font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;letter-spacing:.04em;text-transform:uppercase;margin-left:4px}
.er-accel-accel{background:rgba(63,185,80,.22);color:var(--green)}
.er-accel-fade{background:rgba(210,153,34,.18);color:var(--amber)}
.er-accel-reversal{background:rgba(248,81,73,.22);color:var(--red)}
.er-fy{display:inline-block;font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;letter-spacing:.04em;text-transform:uppercase;background:var(--panel2);color:var(--mut);margin-left:4px}
.er-fy.aligned{background:rgba(88,166,255,.18);color:var(--accent)}
.er-anal{font-size:10px;color:var(--mut);font-weight:500;font-variant-numeric:tabular-nums;white-space:nowrap}
.er-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.er-foot a{color:var(--mut);border-bottom:1px dotted var(--line)}
.er-foot a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.er-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);background:transparent;border-radius:6px;border:1px dashed var(--line);color:var(--mut);font-size:var(--fs-micro);font-style:italic;text-align:center}
@media(max-width:640px){
  .er-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .er-metrics{justify-content:space-between;gap:var(--s3)}
  .er-metric{text-align:left;min-width:0;flex:1}
  .er-metric .val{font-size:16px}
  .er-table thead th{font-size:9px;padding:5px 5px}
  .er-table tbody td{padding:7px 5px}
  .er-table .col-hide-m{display:none}
  .er-bar-cell{min-width:96px}
  .er-vd{font-size:9px;padding:1px 5px;white-space:normal;line-height:1.25;display:inline-block}
  .er-drift-strong{font-size:13px}
  .er-accel,.er-fy{font-size:8px;padding:1px 4px;margin-left:2px;margin-top:2px;display:inline-block}
}
/* Technical-Levels Heatmap (HED-137 Zyklus 113): institutional price-action triage grid */
.tl-panel{padding:var(--s3);margin-top:var(--s3)}
.tl-header{display:flex;align-items:center;gap:var(--s3);flex-wrap:wrap;margin-bottom:var(--s3)}
.tl-summary{display:flex;gap:var(--s3);flex-wrap:wrap;font-size:var(--fs-cap);color:var(--mut)}
.tl-summary span{display:inline-flex;align-items:center;gap:4px}
.tl-summary .bull{color:var(--green)}
.tl-summary .bear{color:var(--red)}
.tl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-top:var(--s3)}
.tl-card{border-radius:6px;border:1px solid var(--line);background:var(--panel);padding:10px 12px;position:relative;transition:border-color .15s}
.tl-card:hover{border-color:var(--accent)}
/* tier-4 = regime change — strong left border */
.tl-card.tier4{border-left:3px solid}
.tl-card.tier4.bullish{border-left-color:var(--green);background:rgba(63,185,80,.04)}
.tl-card.tier4.bearish{border-left-color:var(--red);background:rgba(248,81,73,.04)}
.tl-card.tier4.mixed{border-left-color:var(--accent);background:rgba(88,166,255,.04)}
/* tier-3 = notable extreme — subtle background */
.tl-card.tier3.bullish{background:rgba(63,185,80,.025)}
.tl-card.tier3.bearish{background:rgba(248,81,73,.025)}
/* tier-1/2 = structural signal */
.tl-card.tier2,.tl-card.tier1{border-left:2px solid var(--line)}
.tl-ticker{font-size:15px;font-weight:700;letter-spacing:.03em;line-height:1;color:var(--txt);font-variant-numeric:tabular-nums}
.tl-ticker.bullish{color:var(--green)}
.tl-ticker.bearish{color:var(--red)}
.tl-tier-badge{display:inline-block;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:1px 5px;border-radius:3px;margin-left:6px;vertical-align:middle}
.tl-tier4-badge{background:rgba(255,215,0,.18);color:#c9a500}
.tl-tier3-badge{background:rgba(88,166,255,.15);color:var(--accent)}
.tl-tier2-badge,.tl-tier1-badge{background:var(--panel2);color:var(--mut)}
.tl-triggers{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.tl-chip{display:inline-block;font-size:9px;font-weight:600;padding:2px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em;line-height:1.3;white-space:nowrap}
/* chip colours by kind */
.tl-chip-cross_bull{background:rgba(63,185,80,.22);color:var(--green)}
.tl-chip-cross_bear{background:rgba(248,81,73,.22);color:var(--red)}
.tl-chip-sma200_bull{background:rgba(63,185,80,.14);color:var(--green)}
.tl-chip-sma200_bear{background:rgba(248,81,73,.14);color:var(--red)}
.tl-chip-52h{background:rgba(63,185,80,.10);color:var(--green)}
.tl-chip-52l{background:rgba(248,81,73,.10);color:var(--red)}
.tl-chip-sma50_bull{background:rgba(63,185,80,.08);color:var(--green)}
.tl-chip-sma50_bear{background:rgba(248,81,73,.08);color:var(--red)}
.tl-chip-rsi_os{background:rgba(255,165,0,.15);color:#c87800}
.tl-chip-rsi_ob{background:rgba(255,165,0,.15);color:#c87800}
.tl-chip-vol{background:rgba(88,166,255,.12);color:var(--accent)}
.tl-chip-gap_up{background:rgba(63,185,80,.08);color:var(--green)}
.tl-chip-gap_dn{background:rgba(248,81,73,.08);color:var(--red)}
.tl-as-of{font-size:9px;color:var(--mut);margin-top:5px;letter-spacing:.04em}
.tl-empty{color:var(--mut);font-size:var(--fs-cap);padding:var(--s3) 0}
@media(max-width:640px){
  .tl-grid{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}
  .tl-ticker{font-size:13px}
  .tl-chip{font-size:8px;padding:1px 5px}
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
/* Quality-Scorecard — Bloomberg FA panel · Rule-of-40 SaaS quality screen (HED-137 cycle 111).
   Per ticker: stacked R40 bar (Rev-Growth + FCF-Margin) with 40% threshold marker, plus margin
   profile (Gross/Op/Net) and valuation multiples (Fwd P/E, EV/Sales). Answers the PM question
   "where is the quality bid and where am I paying for it?" — the rule-of-40 separator filters
   for compounders worth a premium multiple. Sorted by R40 desc, book positions get ★ overlay. */
.qs-panel{padding:var(--s3);margin-top:var(--s3)}
.qs-h{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:var(--s3);margin-bottom:var(--s3)}
.qs-h-title{font-weight:700;font-size:var(--fs-h2);text-transform:none;letter-spacing:0;color:var(--txt);line-height:1.2}
.qs-h-sub{font-size:var(--fs-micro);color:var(--mut);font-weight:400;margin-top:2px}
.qs-metrics{display:flex;gap:var(--s4);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.qs-metric{display:flex;flex-direction:column;gap:1px;font-size:var(--fs-micro);text-align:right;min-width:64px;cursor:help}
.qs-metric .lbl{color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.qs-metric .val{font-size:18px;font-weight:700;letter-spacing:-.01em;line-height:1.15;color:var(--txt)}
.qs-metric .val.r40-elite{color:var(--green)}
.qs-metric .val.r40-good{color:var(--accent)}
.qs-metric .val.r40-weak{color:var(--amber)}
.qs-metric .val.r40-poor{color:var(--red)}
.qs-tbl{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:var(--fs-cap)}
.qs-tbl thead th{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:600;text-align:right;padding:8px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.qs-tbl thead th.l{text-align:left}
.qs-tbl thead th.c{text-align:center}
.qs-tbl tbody td{padding:9px 6px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle}
.qs-tbl tbody td.l{text-align:left}
.qs-tbl tbody td.c{text-align:center}
.qs-tbl tbody tr:hover{background:rgba(77,163,255,.05)}
.qs-tbl tbody tr.is-held td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.qs-tk{display:flex;flex-direction:column;gap:1px;line-height:1.15;min-width:0}
.qs-tk-row{display:flex;align-items:center;gap:5px}
.qs-tk-sym{font-weight:700;color:var(--txt);font-size:var(--fs-cap);letter-spacing:.02em}
.qs-tk-star{font-size:11px;color:var(--accent);line-height:1}
.qs-tk-dir{font-size:9px;font-weight:700;letter-spacing:.06em;padding:1px 5px;border-radius:3px;text-transform:uppercase}
.qs-tk-dir.long{background:rgba(63,185,80,.18);color:var(--green)}
.qs-tk-dir.short{background:rgba(248,81,73,.18);color:var(--red)}
.qs-tk-meta{font-size:10px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}
/* Rule-of-40 stacked bar: growth (cyan) + fcf margin (green) summed on a 0..80% axis.
   The 40% gridline is the canonical pass/fail marker — names above it earn premium multiples.
   Visual stack lets a PM compare growth-heavy vs profitability-heavy R40 mixes side by side. */
.qs-r40-cell{display:flex;flex-direction:column;gap:3px;align-items:stretch;min-width:140px}
.qs-r40-bar{position:relative;height:14px;background:var(--panel2);border:1px solid var(--line);border-radius:3px;overflow:hidden;display:flex}
.qs-r40-seg{height:100%;display:block}
.qs-r40-seg.growth{background:linear-gradient(90deg,#2a6ea8,var(--accent))}
.qs-r40-seg.fcf{background:linear-gradient(90deg,#2a7a3d,var(--green))}
.qs-r40-seg.neg{background:repeating-linear-gradient(45deg,rgba(248,81,73,.5),rgba(248,81,73,.5) 3px,rgba(248,81,73,.2) 3px,rgba(248,81,73,.2) 6px)}
.qs-r40-mark{position:absolute;top:-2px;bottom:-2px;width:1px;background:var(--txt);opacity:.7;pointer-events:none}
.qs-r40-mark::after{content:"40";position:absolute;top:-11px;left:50%;transform:translateX(-50%);font-size:8px;color:var(--mut);font-weight:600;letter-spacing:0;background:var(--panel);padding:0 2px;border-radius:2px}
.qs-r40-lbl{display:flex;justify-content:space-between;gap:var(--s2);font-size:9px;color:var(--mut);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.qs-r40-lbl .g{color:var(--accent)}
.qs-r40-lbl .f{color:var(--green)}
.qs-r40-lbl .neg{color:var(--red)}
.qs-score{display:inline-flex;align-items:baseline;gap:3px;font-weight:700;font-variant-numeric:tabular-nums}
.qs-score.elite{color:var(--green)}
.qs-score.good{color:var(--accent)}
.qs-score.weak{color:var(--amber)}
.qs-score.poor{color:var(--red)}
.qs-score .unit{font-size:9px;color:var(--mut);font-weight:500;letter-spacing:.04em}
/* margin-profile micro-bars: gross over op, sharing a 100% scale for instant comparison. */
.qs-marg{display:flex;flex-direction:column;align-items:flex-end;gap:2px;min-width:74px}
.qs-marg-row{display:flex;align-items:center;gap:5px;width:100%}
.qs-marg-lbl{font-size:9px;color:var(--mut);font-weight:600;letter-spacing:.04em;text-transform:uppercase;width:16px;text-align:right}
.qs-marg-bar{flex:1;height:5px;background:var(--panel2);border:1px solid var(--line);border-radius:2px;overflow:hidden;min-width:30px}
.qs-marg-fill{display:block;height:100%}
.qs-marg-fill.gross{background:#9ad0ff}
.qs-marg-fill.op{background:var(--accent)}
.qs-marg-fill.neg{background:repeating-linear-gradient(45deg,rgba(248,81,73,.6),rgba(248,81,73,.6) 2px,transparent 2px,transparent 4px)}
.qs-marg-num{font-size:10px;font-weight:700;width:38px;text-align:right;font-variant-numeric:tabular-nums}
/* valuation cell: forward multiple + EV/Sales stacked compact */
.qs-val{display:flex;flex-direction:column;align-items:flex-end;line-height:1.15;gap:2px;font-variant-numeric:tabular-nums}
.qs-val .pe{font-size:var(--fs-cap);font-weight:700;color:var(--txt)}
.qs-val .pe.muted{color:var(--mut);font-weight:500}
.qs-val .evs{font-size:9px;color:var(--mut);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.qs-val .evs strong{color:var(--mut);font-weight:700}
.qs-peg{font-size:9px;color:var(--mut);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.qs-peg.cheap{color:var(--green)}
.qs-peg.fair{color:var(--accent)}
.qs-peg.rich{color:var(--amber)}
.qs-peg.expensive{color:var(--red)}
.qs-foot{font-size:var(--fs-micro);color:var(--mut);margin-top:var(--s3);line-height:1.5}
.qs-empty{display:flex;align-items:center;justify-content:center;padding:var(--s5) var(--s3);border:1px dashed var(--line);border-radius:6px;color:var(--mut);font-size:var(--fs-micro);font-style:italic}
@media(max-width:640px){
  .qs-h{flex-direction:column;align-items:stretch;gap:var(--s2)}
  .qs-metrics{justify-content:space-between;gap:var(--s3)}
  .qs-metric{text-align:left;min-width:0;flex:1}
  .qs-metric .val{font-size:16px}
  .qs-tbl thead th.col-hide-m,.qs-tbl tbody td.col-hide-m{display:none}
  .qs-tbl thead th{padding:6px 3px;font-size:9px}
  .qs-tbl tbody td{padding:7px 3px}
  .qs-tk-meta{display:none}
  .qs-r40-cell{min-width:96px}
  .qs-marg{min-width:60px}
  .qs-marg-num{width:30px;font-size:9px}
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
.sec-nav{display:flex;gap:var(--s2);flex-wrap:wrap;margin:var(--s3) 0 var(--s4);overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.sec-nav::-webkit-scrollbar{display:none}
@media (max-width:760px){.sec-nav{flex-wrap:nowrap;padding-bottom:var(--s1)}}
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

  <!-- Fund performance summary bar (HED-137 Zyklus 115): above-the-fold trust signal.
       Placed before nav so it's visible immediately on mobile without scrolling. -->
  <div id="fund-summary-bar" aria-label="Fund Performance Summary" style="margin:var(--s4) 0 var(--s3)"></div>

  <!-- Heute auf dem Tape (HED-137 Zyklus 116): 1d action triage — book pulse, top mover/laggard, hottest universe move. -->
  <div id="todays-tape" aria-label="Heute auf dem Tape — 1-Tages-Action-Triage"></div>

  <details class="wf-details">
    <summary class="wf-summary">Workflow — Pipeline-Status</summary>
    <div class="flow-wrap"><div class="flow" id="flow"></div></div>
  </details>

  <nav class="sec-nav" aria-label="Seitenabschnitte">
    <a href="#h-macropulse">Macro</a>
    <a href="#h-briefing">Briefing</a>
    <a href="#h-trackrecord">Track-Record</a>
    <a href="#h-portfolio">Portfolio</a>
    <a href="#h-signalmatrix">Signal-Matrix</a>
    <a href="#h-opttape">Optionen</a>
    <a href="#h-ivrvedge">Vol-Edge</a>
    <a href="#h-catalysts">Katalysatoren</a>
    <a href="#h-earnplay">Earnings-Playbook</a>
    <a href="#h-quality">Quality-Scorecard</a>
    <a href="#h-epsrev">EPS-Revisions</a>
    <a href="#h-techlevels">Tech-Levels</a>
    <a href="#h-scanner">Ideen-Scanner</a>
    <a href="#h-consspread">Konsens-Spread</a>
    <a href="#h-sectorview">Sektoren</a>
  </nav>

  <main id="main" tabindex="-1">
  <noscript><div class="panel noscript-panel" role="alert"><div class="noscript-icon" aria-hidden="true">⚠</div><p class="muted">Dieses Dashboard benötigt JavaScript. Bitte aktiviere JavaScript in deinem Browser und lade die Seite neu.</p></div></noscript>
  <section aria-labelledby="h-macropulse">
  <h2 id="h-macropulse">Macro-Pulse <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Markt-Regime · Risk-Anker</span></h2>
  <div id="macropulse" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:62%"></div><div class="skel skel-line" style="width:88%"></div></div></div>
  </section>

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

  <section aria-labelledby="h-signalmatrix">
  <h2 id="h-signalmatrix">Signal-Matrix <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Synthesis · Tech · Options · Vol-Edge · Insider</span></h2>
  <div id="signalmatrix" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:55%"></div><div class="skel skel-line" style="width:80%"></div><div class="skel skel-line" style="width:68%"></div></div></div>
  </section>

  <section aria-labelledby="h-insidertape">
  <h2 id="h-insidertape">Insider-Tape <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Smart-Money 30d</span></h2>
  <div id="insidertape" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:64%"></div><div class="skel skel-line" style="width:58%"></div></div></div>
  </section>

  <section aria-labelledby="h-opttape">
  <h2 id="h-opttape">Options-Tape <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Institutionelles Positioning · OMON</span></h2>
  <div id="opttape" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:52%"></div><div class="skel skel-line" style="width:66%"></div><div class="skel skel-line" style="width:60%"></div></div></div>
  </section>

  <section aria-labelledby="h-ivrvedge">
  <h2 id="h-ivrvedge">Vol-Edge <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">IV − RV · Premium-Pricing · HVR</span></h2>
  <div id="ivrvedge" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:50%"></div><div class="skel skel-line" style="width:68%"></div><div class="skel skel-line" style="width:58%"></div></div></div>
  </section>

  <section aria-labelledby="h-shortpress">
  <h2 id="h-shortpress">Short-Squeeze-Pressure <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Float-Short · MoM-Trend · SI</span></h2>
  <div id="shortpress" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:52%"></div><div class="skel skel-line" style="width:64%"></div><div class="skel skel-line" style="width:58%"></div></div></div>
  </section>

  <section aria-labelledby="h-catalysts">
  <h2 id="h-catalysts">Katalysator-Runway</h2>
  <div id="catalysts" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:62%"></div></div></div>
  </section>

  <section aria-labelledby="h-earnplay">
  <h2 id="h-earnplay">Earnings-Playbook <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Beat-Rate · Surprise · 1d-Reaktion</span></h2>
  <div id="earnplay" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:64%"></div><div class="skel skel-line" style="width:58%"></div></div></div>
  </section>

  <section aria-labelledby="h-quality">
  <h2 id="h-quality">Quality-Scorecard <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Rule-of-40 · Margin-Profil · Multiple</span></h2>
  <div id="qualityscore" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:55%"></div><div class="skel skel-line" style="width:68%"></div><div class="skel skel-line" style="width:62%"></div></div></div>
  </section>
  <section aria-labelledby="h-epsrev">
  <h2 id="h-epsrev">EPS-Revisions-Velocity <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Sell-Side-Momentum · StarMine · EE/EM</span></h2>
  <div id="epsrev" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:52%"></div><div class="skel skel-line" style="width:68%"></div><div class="skel skel-line" style="width:60%"></div></div></div>
  </section>

  <section aria-labelledby="h-techlevels">
  <h2 id="h-techlevels">Technical-Levels <span class="muted" style="font-weight:400;font-size:var(--fs-cap)">Preis-Regime · SMA-Crossovers · RSI · Institutionelle Trigger</span></h2>
  <div id="techlevels" aria-live="polite" aria-atomic="false" aria-busy="true"><div class="skel-loader" aria-hidden="true"><div class="skel skel-line" style="width:48%"></div><div class="skel skel-line" style="width:65%"></div><div class="skel skel-line" style="width:55%"></div></div></div>
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
    // Per-thesis sparkline source: 30 daily closes from sector_view (already fetched by build.py).
    // Card embeds the chart with: dashed baseline line, entry chevron, current-price dot,
    // P&L pill colored by direction-adjusted move. The strongest trust signal — institutional
    // investors expect to see price-action context, not just numbers in a table (HED-137 cycle 114).
    const _thpSparkMap={};
    ((D.sector_view||{}).sectors||[]).forEach(s=>{
      (s.tickers||[]).forEach(t=>{ if(t&&t.ticker&&Array.isArray(t.spark)&&t.spark.length>=5) _thpSparkMap[String(t.ticker).toUpperCase()]=t.spark; });
    });
    const _thpAsOf=((D.sector_view||{}).as_of_iso||(D.sector_view||{}).as_of||"").replace(/ UTC.*/,"").slice(0,10);
    function _thpBdaysBack(dStr, asOfStr){
      if(!dStr||!asOfStr) return null;
      try{ const d1=new Date(dStr+"T00:00:00Z"); const d2=new Date(asOfStr+"T00:00:00Z");
        if(isNaN(d1)||isNaN(d2)||d1>d2) return null;
        let cur=new Date(d1), n=0; while(cur<d2){ cur.setUTCDate(cur.getUTCDate()+1); const dow=cur.getUTCDay(); if(dow!==0&&dow!==6) n++; } return n;
      }catch(e){ return null; }
    }
    function _thpEntryIdx(spark, baseline, dateStr){
      if(!spark||!spark.length||baseline==null) return -1;
      if(dateStr && _thpAsOf){
        const back=_thpBdaysBack(dateStr, _thpAsOf);
        if(back!=null){ const idx=spark.length-1-back;
          if(idx>=0 && idx<spark.length && Math.abs(spark[idx]-baseline)/baseline<0.05) return idx; }
      }
      const start=Math.max(0, spark.length-6); let best=-1, bestDiff=Infinity;
      for(let i=start;i<spark.length;i++){ const d=Math.abs(spark[i]-baseline); if(d<bestDiff){ bestDiff=d; best=i; } }
      return (best>=0 && Math.abs(spark[best]-baseline)/baseline<0.01) ? best : -1;
    }
    // Inline sparkline SVG with baseline line + entry marker + current dot.
    // viewBox is content-only (no padding); CSS provides the box. preserveAspectRatio=none
    // lets the chart stretch to fill the container — sparklines, not precision charts.
    function _thpSpark(spark, baseline, dir){
      if(!spark||spark.length<3) return "";
      const w=300, h=64, pad=4;
      const vals=spark.slice();
      const lo=Math.min.apply(null, baseline!=null?vals.concat(baseline):vals);
      const hi=Math.max.apply(null, baseline!=null?vals.concat(baseline):vals);
      const range=Math.max(0.0001, hi-lo);
      const stepX=(w-pad*2)/(vals.length-1);
      const yOf=v=>pad+(h-pad*2)-((v-lo)/range)*(h-pad*2);
      const xOf=i=>pad+i*stepX;
      const last=vals[vals.length-1];
      const sign=(dir||"").toLowerCase()==="short"?-1:1;
      const pnl=baseline!=null?((last-baseline)/baseline*100)*sign:0;
      const stroke=baseline==null?"var(--accent)":pnl>=0?"var(--green)":"var(--red)";
      const fillCol=baseline==null?"77,163,255":pnl>=0?"63,185,80":"248,81,73";
      const d="M"+vals.map((v,i)=>`${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`).join(" L");
      const area=d+` L${xOf(vals.length-1).toFixed(1)},${(h-pad).toFixed(1)} L${xOf(0).toFixed(1)},${(h-pad).toFixed(1)} Z`;
      const baseY=baseline!=null?yOf(baseline):null;
      const eIdx=baseline!=null?_thpEntryIdx(vals, baseline, null):-1;
      const baselineLine=baseY!=null?`<line x1="${pad}" y1="${baseY.toFixed(1)}" x2="${(w-pad).toFixed(1)}" y2="${baseY.toFixed(1)}" stroke="var(--mut)" stroke-width="1" stroke-dasharray="3,3" opacity="0.7"/>`:"";
      const entryMark=eIdx>=0?`<g transform="translate(${xOf(eIdx).toFixed(1)},${yOf(vals[eIdx]).toFixed(1)})">
        <circle r="3.5" fill="var(--panel)" stroke="var(--mut)" stroke-width="1.2"/>
      </g>`:"";
      const lastDot=`<circle cx="${xOf(vals.length-1).toFixed(1)}" cy="${yOf(last).toFixed(1)}" r="3" fill="${stroke}"/>`;
      return `<svg class="thp-chart-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
        <path d="${area}" fill="rgba(${fillCol},0.08)"/>
        ${baselineLine}
        <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>
        ${entryMark}${lastDot}
      </svg>`;
    }
    const pendingTbl=pendingTheses.length ? (()=>{
      const rows=pendingTheses.slice().sort((x,y)=>
        ((y.conviction||0)-(x.conviction||0)) ||
        ((x.earliest_score_date||"").localeCompare(y.earliest_score_date||"")));
      const _cc=c=>c==null?"":c>=0.6?"conv-hi":c>=0.35?"conv-mid":"conv-lo";
      const exitNote=t=>t.exit_trigger?`<div class="thp-exit" title="Exit wenn: ${esc(t.exit_trigger)}"><span class="thp-exit-lbl">Exit</span> <span>${esc(t.exit_trigger)}</span></div>`:"";
      // Scenario grid: Bull/Base/Bear stacked rows with prob bar + target. Recognition>recall —
      // PM should see all three scenarios at once, not parse a one-line CSV.
      const scenGrid=t=>{
        const sc=t.scenarios; if(!sc) return "";
        const items=[["Bull","sc-bull",sc.bull],["Base","sc-base",sc.base],["Bear","sc-bear",sc.bear]]
          .filter(x=>x[2]);
        if(!items.length) return "";
        const rows=items.map(([lbl,cls,s])=>{
          const p=Math.round((s.prob||0)*100);
          const tgt=s.target?esc(s.target):"";
          const trig=s.trigger?esc(s.trigger):"";
          return `<div class="thp-sc-row thp-${cls}">
            <span class="thp-sc-lbl">${lbl}</span>
            <span class="thp-sc-prob"><span class="thp-sc-pbar" style="width:${p}%"></span><span class="thp-sc-pn">${p}%</span></span>
            <span class="thp-sc-tgt">${tgt}</span>
            <span class="thp-sc-trig">${trig}</span>
          </div>`;
        }).join("");
        return `<div class="thp-scen" aria-label="Szenario-Zusammenfassung">${rows}</div>`;
      };
      const cards=rows.map(t=>{
        const tks=(t.tickers||[]).map(x=>String(x).toUpperCase());
        const primaryTk=tks[0]||"";
        const spark=primaryTk?_thpSparkMap[primaryTk]:null;
        const cur=primaryTk?_priceMap[primaryTk]:null;
        const base=t.baseline_price;
        const dir=(t.direction||"").toLowerCase();
        const sign=dir==="short"?-1:1;
        const pnl=(base!=null && cur!=null) ? ((cur-base)/base*100)*sign : null;
        const pnlCls=pnl==null?"thp-pnl-unp":pnl>=0?"thp-pnl-pos":"thp-pnl-neg";
        const pnlTxt=pnl==null?"—":`${pnl>=0?"+":"−"}${Math.abs(pnl).toFixed(2)}%`;
        const lo=spark?Math.min.apply(null,spark):null;
        const hi=spark?Math.max.apply(null,spark):null;
        const scaleNote=spark
          ? `<span class="thp-range">30d $${lo.toFixed(0)}–$${hi.toFixed(0)}</span>`
          : `<span class="thp-range muted">kein Chart</span>`;
        const baseTag=base!=null?`<span class="thp-base">Entry $${base}</span>`:"";
        const curTag=cur!=null?`<span class="thp-cur">aktuell $${cur.toFixed(2)}</span>`:"";
        const chart=spark
          ? `<div class="thp-chart">${_thpSpark(spark, base, dir)}</div>`
          : `<div class="thp-chart thp-chart-empty muted">kein Sparkline-Datenpunkt</div>`;
        const tickersHtml=tks.length
          ? tks.map(tk=>`<a class="thp-tk" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">${esc(tk)}</a>`).join(" ")
          : `<span class="muted">—</span>`;
        const devil=t.devil&&t.devil.note
          ? `<div class="thp-devil thp-devil-${esc(t.devil.verdict||"caution")}" title="Devil's Advocate"><span class="thp-devil-lbl">⚖ ${esc(t.devil.verdict||"caution")}</span> <span class="thp-devil-note">${esc(t.devil.note)}</span></div>`
          : "";
        const horizon=t.horizon?`<span class="thp-hz">${esc(t.horizon)}</span>`:"";
        return `<article class="thp-card">
          <header class="thp-h">
            <div class="thp-h-left">
              <span class="thp-tickers">${tickersHtml}</span>
              <span class="cd ${dirClass(t.direction)} thp-dir">${esc((t.direction||"—").toUpperCase())}</span>
              ${horizon}
            </div>
            <div class="thp-h-right">
              <span class="thp-pnl ${pnlCls}">${pnlTxt}</span>
              <span class="thp-conv ${_cc(t.conviction)}" title="Conviction">${t.conviction!=null?t.conviction.toFixed(2):"—"}</span>
            </div>
          </header>
          <div class="thp-label">${esc(t.label||"?")}</div>
          ${chart}
          <div class="thp-meta">
            ${baseTag}
            ${curTag}
            ${scaleNote}
            <span class="thp-score">Wertung ab <b>${esc(t.earliest_score_date||"—")}</b></span>
          </div>
          ${scenGrid(t)}
          ${exitNote(t)}
          ${devil}
        </article>`;
      }).join("");
      return `<div class="thp-cap">Offene Thesen — zu früh für Wertung · <span class="muted">Sortiert nach Conviction</span></div>
        <div class="thp-grid">${cards}</div>`;
    })() : "";
    $("trbody").innerHTML=`<div class="panel"><div class="empty">
      <div class="g" aria-hidden="true">⏳</div>
      <div class="hl">Noch keine gewerteten Thesen</div>
      <div class="ex">${a.too_early||0} offene These${(a.too_early===1)?"":"n"} — der Zeithorizont (Wochen/Quartale) ist noch nicht abgelaufen. Gewertet wird gegen reale Kurse, keine Schätzungen.</div>
      ${cd}
    </div>${pendingTbl}</div>`;
  }
})();

// Fund Performance Summary Bar (HED-137 Zyklus 115) — above-the-fold PRTU-style trust signal.
// Shows the headline conviction-weighted book P&L, active call count, Long/Short split,
// SPX 30d benchmark, and score countdown. First number an investor sees on page load.
(function renderFundSummaryBar(){
  const root=$("fund-summary-bar");
  if(!root) return;
  const tr=D.track_record;
  const active=(tr&&tr.theses||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date));
  if(!active.length){ root.style.display="none"; return; }
  // Build live price map from sector_view
  const _pf={};
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(t=>{ if(t&&t.ticker&&t.price!=null) _pf[String(t.ticker).toUpperCase()]=t.price; });
  });
  // Conviction-weighted book P&L (same formula as renderPortfolio)
  const priced=active.filter(t=>{
    const tk=(t.tickers||[])[0]; return tk&&t.baseline_price!=null&&_pf[String(tk).toUpperCase()]!=null;
  });
  const wConv=priced.reduce((s,t)=>s+(t.conviction||0),0);
  const bookPnl=wConv>0?priced.reduce((s,t)=>{
    const tk=String((t.tickers||[])[0]).toUpperCase();
    const cur=_pf[tk]; const sign=(t.direction||"").toLowerCase()==="short"?-1:1;
    return s+(t.conviction||0)*((cur-t.baseline_price)/t.baseline_price*100)*sign;
  },0)/wConv:null;
  // Long/Short split
  const longs=active.filter(t=>(t.direction||"").toLowerCase()==="long").length;
  const shorts=active.filter(t=>(t.direction||"").toLowerCase()==="short").length;
  // Days to first score
  const esd=(tr&&tr.earliest_score_date)||null;
  const daysToScore=esd?Math.ceil((new Date(esd+"T00:00:00Z")-Date.now())/864e5):null;
  // SPX benchmark 30d return
  const spxSpark=((D.sector_view||{}).benchmarks||{})?.SPY?.spark||null;
  const spxPnl=spxSpark&&spxSpark.length>=2
    ? ((spxSpark[spxSpark.length-1]-spxSpark[0])/spxSpark[0]*100) : null;
  // Book equity mini-sparkline: last N days of conviction-weighted daily book value.
  // Reuse _curveSrc pattern from portfolio section — spark map + entry index.
  const _sparkMap={};
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(t=>{ if(t&&t.ticker&&Array.isArray(t.spark)&&t.spark.length>=2) _sparkMap[String(t.ticker).toUpperCase()]=t.spark; });
  });
  const _asOf=((D.sector_view||{}).as_of_iso||(D.sector_view||{}).as_of||"").replace(/ UTC.*/,"").slice(0,10);
  function _bd(dStr,asOf){ if(!dStr||!asOf) return null;
    try{ const d1=new Date(dStr+"T00:00:00Z"),d2=new Date(asOf+"T00:00:00Z"); if(isNaN(d1)||isNaN(d2)||d1>d2) return null;
      let c=new Date(d1),n=0; while(c<d2){ c.setUTCDate(c.getUTCDate()+1); const dw=c.getUTCDay(); if(dw&&dw!==6) n++; } return n;
    }catch(e){ return null; } }
  function _eIdx(spark,baseline,dateStr){ if(!spark||!spark.length||baseline==null) return -1;
    if(dateStr&&_asOf){ const b=_bd(dateStr,_asOf); if(b!=null){ const i=spark.length-1-b;
      if(i>=0&&i<spark.length&&Math.abs(spark[i]-baseline)/baseline<0.05) return i; } }
    const start=Math.max(0,spark.length-6); let best=-1,bd=Infinity;
    for(let i=start;i<spark.length;i++){ const d=Math.abs(spark[i]-baseline); if(d<bd){ bd=d; best=i; } }
    return (best>=0&&Math.abs(spark[best]-baseline)/baseline<0.01)?best:-1; }
  const curveSrc=[];
  active.forEach(t=>{ const tk=(t.tickers||[])[0]; if(!tk||t.baseline_price==null) return;
    const sp=_sparkMap[String(tk).toUpperCase()]; if(!sp||sp.length<2) return;
    const eIdx=_eIdx(sp,t.baseline_price,t.date); if(eIdx<0||eIdx>=sp.length-1) return;
    const eOff=sp.length-1-eIdx;
    curveSrc.push({conv:(t.conviction??0.5),baseline:t.baseline_price,spark:sp,eOff,sign:(t.direction||"").toLowerCase()==="short"?-1:1});
  });
  let sparkHtml="";
  if(curveSrc.length>=1){
    const incep=Math.max(...curveSrc.map(s=>s.eOff));
    const curve=[];
    for(let off=incep;off>=0;off--){
      let wS=0,rS=0; curveSrc.forEach(s=>{ if(s.eOff>=off){ const idx=s.spark.length-1-off;
        if(idx>=0){ const r=(s.spark[idx]-s.baseline)/s.baseline*100*s.sign; wS+=s.conv; rS+=s.conv*r; } }});
      curve.push(wS>0?rS/wS:0);
    }
    const W=120,H=40;
    const lo=Math.min.apply(null,curve), hi=Math.max.apply(null,curve);
    const rng=Math.max(0.0001,hi-lo);
    const stepX=(W-4)/(curve.length-1);
    const xOf=i=>2+i*stepX, yOf=v=>H-2-((v-lo)/rng)*(H-4);
    const pts=curve.map((v,i)=>`${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`).join(" ");
    const last=curve[curve.length-1];
    const scls=last>=0?"spark-up":"spark-dn";
    const fill=last>=0?"rgba(63,185,80,0.08)":"rgba(248,81,73,0.08)";
    const areaD=`M${xOf(0).toFixed(1)},${yOf(curve[0]).toFixed(1)} `+
      curve.slice(1).map((v,i)=>`L${xOf(i+1).toFixed(1)},${yOf(v).toFixed(1)}`).join(" ")+
      ` L${xOf(curve.length-1).toFixed(1)},${(H-2).toFixed(1)} L${xOf(0).toFixed(1)},${(H-2).toFixed(1)} Z`;
    sparkHtml=`<svg class="fsb-spark-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">
      <path d="${areaD}" fill="${fill}"/>
      <polyline class="spark-line ${scls}" points="${pts}" style="stroke-width:1.8"/>
    </svg>`;
  }
  // Render
  const pnlTxt=bookPnl==null?"—":`${bookPnl>=0?"+":"−"}${Math.abs(bookPnl).toFixed(2)}%`;
  const pnlCls=bookPnl==null?"fsb-pnl-flat":bookPnl>=0.01?"fsb-pnl-pos":"fsb-pnl-neg";
  const pnlTag=bookPnl==null?"kein Live-Kurs":"unrealisiert · konviktions-gew.";
  const spxTxt=spxPnl==null?"—":`${spxPnl>=0?"+":"−"}${Math.abs(spxPnl).toFixed(2)}%`;
  const spxCls=spxPnl==null?"":spxPnl>=0?"move-up":"move-dn";
  const scoreTxt=daysToScore==null?"—":daysToScore>0?`in ${daysToScore}d`:"fällig";
  const alpha=bookPnl!=null&&spxPnl!=null?bookPnl-spxPnl:null;
  const alphaTxt=alpha==null?"—":`${alpha>=0?"+":"−"}${Math.abs(alpha).toFixed(2)}%`;
  const alphaCls=alpha==null?"":alpha>=0?"move-up":"move-dn";
  root.innerHTML=`<div class="fsb" role="region" aria-label="Fund Performance">
    <div class="fsb-main">
      <span class="fsb-pnl-lbl">Buch P&amp;L</span>
      <div>
        <span class="fsb-pnl-val ${pnlCls}">${pnlTxt}</span>
        <span class="fsb-pnl-tag">${pnlTag}</span>
      </div>
    </div>
    <div class="fsb-stats">
      <div class="fsb-stat">
        <span class="fsb-stat-lbl">Positionen</span>
        <span class="fsb-stat-val">${active.length} Call${active.length===1?"":"s"} · ${longs}L ${shorts}S</span>
      </div>
      <div class="fsb-stat">
        <span class="fsb-stat-lbl">SPY 30d</span>
        <span class="fsb-stat-val ${spxCls}">${spxTxt}</span>
      </div>
      <div class="fsb-stat">
        <span class="fsb-stat-lbl">Alpha vs SPY</span>
        <span class="fsb-stat-val ${alphaCls}">${alphaTxt}</span>
      </div>
      <div class="fsb-stat">
        <span class="fsb-stat-lbl">Score-Window</span>
        <span class="fsb-stat-val">${scoreTxt}</span>
      </div>
    </div>
    ${sparkHtml?`<div class="fsb-spark" title="Buch-Equity-Kurve seit Inception (konviktions-gewichtet)">${sparkHtml}</div>`:""}
  </div>`;
})();

// Heute auf dem Tape (HED-137 Zyklus 116) — daily action triage above the fold.
// 4 quadrants: (1) book pulse 1d (conviction-weighted today's contribution),
// (2) top open-call winner 1d, (3) top open-call loser 1d, (4) hottest universe move
// (|%| max across all tracked tickers, in or out of book). PM-workflow morning view:
// answers "what should I look at first?" before the user scrolls into the 16 deeper panels.
(function renderTodaysTape(){
  const root=$("todays-tape");
  if(!root) return;
  const tr=D.track_record;
  const active=(tr&&tr.theses||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date));
  // Build live ticker map from sector_view: change_pct (1d) + price + sector name.
  // SANITY CAP: 1d single-stock moves >±30% are almost always stale-feed artifacts
  // (chartPreviousClose not refreshed), not real action. We mark these as suspect and
  // exclude them from the Hot Tape pick — better to show nothing than INTC +171%.
  const STALE_THRESHOLD=30.0;
  const tickByTk={}; const allUnivTicks=[]; let staleCount=0;
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(t=>{
      if(!t||!t.ticker) return;
      const sym=String(t.ticker).toUpperCase();
      const cp=t.change_pct;
      const stale=cp!=null&&Math.abs(cp)>STALE_THRESHOLD;
      if(stale) staleCount++;
      tickByTk[sym]={ticker:sym,price:t.price,change_pct:cp,sector:s.name||s.id||"",stale:stale};
      if(cp!=null) allUnivTicks.push(tickByTk[sym]);
    });
  });
  // Per open-call 1d move, sign-adjusted for direction. Stale-cap values excluded
  // from book pulse and winner/loser ranking to avoid misleading headline numbers.
  const bookMoves=[];
  active.forEach(t=>{
    const tk=(t.tickers||[])[0]; if(!tk) return;
    const m=tickByTk[String(tk).toUpperCase()]; if(!m||m.change_pct==null||m.stale) return;
    const sign=(t.direction||"").toLowerCase()==="short"?-1:1;
    bookMoves.push({
      ticker:String(tk).toUpperCase(),
      sector:m.sector,
      direction:(t.direction||"long").toLowerCase(),
      conviction:t.conviction||0,
      raw_pct:m.change_pct,        // market move
      pnl_pct:m.change_pct*sign,   // book contribution sign
      price:m.price
    });
  });
  // Book pulse 1d = conviction-weighted average of pnl_pct
  const wTot=bookMoves.reduce((s,b)=>s+(b.conviction||0),0);
  const bookPulse=wTot>0?bookMoves.reduce((s,b)=>s+(b.conviction||0)*b.pnl_pct,0)/wTot:null;
  const winners=bookMoves.slice().filter(b=>b.pnl_pct>0).sort((a,b)=>b.pnl_pct-a.pnl_pct);
  const losers =bookMoves.slice().filter(b=>b.pnl_pct<0).sort((a,b)=>a.pnl_pct-b.pnl_pct);
  // Universe hot: ticker with max |change_pct| across full tracked universe.
  // Tie-break: prefer tickers also in book (more actionable). Skip benchmarks (SPY/QQQ/etc)
  // and stale-flagged tickers (>±30% 1d, almost certainly feed artifacts).
  const BENCH=new Set(["SPY","QQQ","DIA","IWM","VIX","TLT","GLD","UUP"]);
  const bookSet=new Set(bookMoves.map(b=>b.ticker));
  const univCand=allUnivTicks.filter(t=>!BENCH.has(t.ticker)&&!t.stale);
  univCand.sort((a,b)=>{
    const da=Math.abs(a.change_pct||0), db=Math.abs(b.change_pct||0);
    if(Math.abs(da-db)>0.001) return db-da;
    return (bookSet.has(b.ticker)?1:0)-(bookSet.has(a.ticker)?1:0);
  });
  const univHot=univCand[0]||null;
  const univHot2=univCand[1]||null;
  // Tape freshness: use sector_view as_of
  const tapeAsOf=((D.sector_view||{}).as_of||"").replace(" UTC","");
  // Helpers
  const fmtPct=(v,digits=2)=>v==null?"—":`${v>=0?"+":"−"}${Math.abs(v).toFixed(digits)}%`;
  const clsOf=(v)=>v==null?"tm-flat":v>0.005?"tm-pos":v<-0.005?"tm-neg":"tm-flat";
  const dirChip=(d)=>d==="short"?'<span class="tm-dir tm-dir-s">SHORT</span>':'<span class="tm-dir tm-dir-l">LONG</span>';
  // Quadrant 1: Buch-Pulse 1d
  const pulseVal=fmtPct(bookPulse);
  const pulseCls=clsOf(bookPulse);
  const pulseSub=bookMoves.length
    ? `<span>${bookMoves.length} bepreiste Calls · konviktions-gewichtet</span>`
    : '<span class="tm-empty">keine Live-Kurse</span>';
  // Quadrant 2: Top Winner
  const w=winners[0];
  const winnerCell=w
    ? `<div class="tm-val ${clsOf(w.pnl_pct)}"><span class="tm-tk">${esc(w.ticker)}</span>${dirChip(w.direction)}<span>${fmtPct(w.pnl_pct)}</span></div>
       <div class="tm-sub">${esc(w.sector||"")}${w.price!=null?` · <b>$${w.price.toFixed(2)}</b>`:""}${w.direction==="short"?` · Markt ${fmtPct(w.raw_pct)}`:""}</div>`
    : `<div class="tm-val tm-flat">—</div><div class="tm-sub tm-empty">kein grüner Call heute</div>`;
  // Quadrant 3: Top Loser
  const l=losers[0];
  const loserCell=l
    ? `<div class="tm-val ${clsOf(l.pnl_pct)}"><span class="tm-tk">${esc(l.ticker)}</span>${dirChip(l.direction)}<span>${fmtPct(l.pnl_pct)}</span></div>
       <div class="tm-sub">${esc(l.sector||"")}${l.price!=null?` · <b>$${l.price.toFixed(2)}</b>`:""}${l.direction==="short"?` · Markt ${fmtPct(l.raw_pct)}`:""}</div>`
    : `<div class="tm-val tm-flat">—</div><div class="tm-sub tm-empty">kein roter Call heute</div>`;
  // Quadrant 4: Universe hot
  const u=univHot;
  const inBook=u?bookSet.has(u.ticker):false;
  const univCell=u
    ? `<div class="tm-val ${clsOf(u.change_pct)}"><span class="tm-tk">${esc(u.ticker)}</span>${inBook?'<span class="tm-dir tm-dir-l" title="Position im Buch">★ BOOK</span>':""}<span>${fmtPct(u.change_pct)}</span></div>
       <div class="tm-sub">${esc(u.sector||"")}${u.price!=null?` · <b>$${u.price.toFixed(2)}</b>`:""}${univHot2&&univHot2.ticker!==u.ticker?` · 2. <b>${esc(univHot2.ticker)}</b> ${fmtPct(univHot2.change_pct,1)}`:""}</div>`
    : `<div class="tm-val tm-flat">—</div><div class="tm-sub tm-empty">Universum unbewegt</div>`;
  // Don't render if zero signal (avoid empty terminal-style strip)
  if(!bookMoves.length && !univHot){ root.style.display="none"; return; }
  root.innerHTML=`<div class="tm" role="region" aria-label="Tagestriage">
    <div class="tm-cell tm-cell--pulse">
      <div class="tm-h"><span class="tm-lbl">Buch-Pulse 1d</span><span class="tm-tag tm-tag--book">unrealisiert</span></div>
      <div class="tm-val ${pulseCls}" style="font-size:26px">${pulseVal}</div>
      <div class="tm-sub">${pulseSub}</div>
    </div>
    <div class="tm-cell">
      <div class="tm-h"><span class="tm-lbl">Top-Winner 1d</span><span class="tm-tag tm-tag--book">Buch</span></div>
      ${winnerCell}
    </div>
    <div class="tm-cell">
      <div class="tm-h"><span class="tm-lbl">Top-Laggard 1d</span><span class="tm-tag tm-tag--book">Buch</span></div>
      ${loserCell}
    </div>
    <div class="tm-cell">
      <div class="tm-h"><span class="tm-lbl">Hot Tape</span><span class="tm-tag tm-tag--univ">Universum</span></div>
      ${univCell}
    </div>
  </div>${tapeAsOf?`<div class="tm-foot">Tape-Stand ${esc(tapeAsOf)} · 1d Close-zu-Close · Short-Calls vorzeichenkorrigiert${staleCount?` · <span title="Yahoo chartPreviousClose nicht aktualisiert — typischer Pre-Market-Artefakt">${staleCount} Ticker mit |Δ|>30% ausgefiltert (stale feed)</span>`:""}</div>`:""}`;
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

// Signal-Matrix (HED-137 Zyklus 109): Bloomberg MOST-screen equivalent.
// One row per open call; columns: Tech Setup, Options Tape verdict, Vol-Edge
// pricing, Insider Flow. Each cell is a confirm/conflict/watch/none pill.
// A composite score (confirms − conflicts) drives the per-row alignment read
// and an aggregate verdict: "Broadly Aligned", "Split", "Conflicted".
//
// Signal scoring (always relative to call direction):
//   Tech:     konfirmiert → +1; überdehnt/gemischt → 0; konflikt → −1
//   Options:  bullish/squeeze (long) or bearish/hedge (short) → +1; inverse → −1; neutral/event → 0
//   Vol-Edge: günstig (cheap options) → +1; teuer (expensive) → −1; fair → 0
//             [Rationale for long: cheap options = event-play viable; expensive = headwind for calls]
//   Insider:  net_dollar confirms direction → +1; contradicts → −1; silent → 0
//
// Max composite: 4 confirms. Score ≥ 2 → Aligned; 0 → Neutral; −1 → Mixed; ≤ −2 → Conflicted.
(function renderSignalMatrix(){
  const root=$("signalmatrix");
  if(!root) return;
  const tr=D.track_record||{}, sv=D.sector_view||{};
  const active=(tr.theses||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date));
  if(!active.length){
    root.innerHTML='<div class="panel sm-panel"><div class="sm-empty">Keine offenen Calls — Signal-Matrix benötigt aktive Positionen im Buch.</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }
  // --- Build lookup maps ---
  // Tech: from sector_view ticker objects
  const techMap={};
  (sv.sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(t=>{
      if(!t||!t.ticker) return;
      techMap[String(t.ticker).toUpperCase()]={
        ma30:t.ma30, pct_vs_ma30:t.pct_vs_ma30,
        rsi14:t.rsi14, pct_of_52w_high:t.pct_of_52w_high,
        spark:t.spark
      };
    });
  });
  // Options Tape: from options_tape.tickers (keyed by ticker)
  const otMap={};
  ((D.options_tape||{}).tickers||[]).forEach(r=>{ otMap[String(r.ticker).toUpperCase()]=r; });
  // Vol-Edge: compute inline (same logic as renderIVRVEdge)
  const today=new Date(); today.setUTCHours(0,0,0,0);
  function dteOf(exp){
    if(!exp) return null;
    const [y,m,d]=String(exp).split("-").map(Number);
    return isFinite(y)?Math.max(1,Math.round((new Date(Date.UTC(y,m-1,d)).getTime()-today.getTime())/86400000)):null;
  }
  function rv30(sp){
    if(!Array.isArray(sp)||sp.length<6) return null;
    const c=sp.slice(-31), lr=[];
    for(let i=1;i<c.length;i++){ if(c[i-1]>0&&c[i]>0) lr.push(Math.log(c[i]/c[i-1])); }
    if(lr.length<5) return null;
    const mu=lr.reduce((a,b)=>a+b,0)/lr.length;
    const v=lr.reduce((s,x)=>s+(x-mu)*(x-mu),0)/Math.max(1,lr.length-1);
    return Math.sqrt(v)*Math.sqrt(252);
  }
  const volEdgeMap={};
  ((D.options_tape||{}).tickers||[]).filter(r=>r.emove!=null&&r.exp).forEach(r=>{
    const tk=String(r.ticker).toUpperCase();
    const dte=dteOf(r.exp); if(!dte) return;
    const sp=techMap[tk]?.spark;
    const rv=rv30(sp); if(!rv) return;
    const iv=(r.emove/100)/0.7979/Math.sqrt(dte/252);
    if(!isFinite(iv)||iv<=0) return;
    const spreadPp=(iv-rv)*100;
    volEdgeMap[tk]={spreadPp, iv, rv, dte};
  });
  // Insider Tape: from insider_tape.tickers (keyed by ticker)
  const insMap={};
  ((D.insider_tape||{}).tickers||[]).forEach(r=>{ insMap[String(r.ticker).toUpperCase()]=r; });

  // --- Signal computation (direction-aware) ---
  function techSignal(tk, dir){
    const m=techMap[tk]; if(!m) return {score:0,label:"—",cls:"none",tip:""};
    const isLong=dir==="long", isShort=dir==="short";
    if(!isLong&&!isShort) return {score:0,label:"—",cls:"none",tip:""};
    const ma=m.pct_vs_ma30, rsi=m.rsi14, w52=m.pct_of_52w_high;
    const sign=isLong?1:-1;
    const trendSig=ma==null?0:(sign*ma>0?1:-1);
    let momSig=0, stretched=false;
    if(rsi!=null){
      if(isLong){
        if(rsi>=70){momSig=1;stretched=true;}
        else if(rsi>=50)momSig=1;
        else if(rsi<30)momSig=1;  // oversold long
      }else{
        if(rsi<=30){momSig=1;stretched=true;}
        else if(rsi<=50)momSig=1;
        else if(rsi>70)momSig=1;  // overbought short
      }
    }
    if(trendSig===1&&!stretched&&momSig===1)  return {score:1, label:"Konfirmiert",cls:"conf",  tip:`Trend + Momentum bestätigen ${dir}`};
    if(trendSig===1&&stretched)               return {score:0, label:"Überdehnt",  cls:"watch", tip:`Trend stützt, aber RSI im Extrem — Late-${dir} Warnung`};
    if(trendSig===-1)                         return {score:-1,label:"Konflikt",   cls:"conflict",tip:`Chart läuft gegen ${dir} — re-evaluieren`};
    return {score:0, label:"Gemischt", cls:"watch", tip:"Partielles Chart-Setup — thesis-getrieben"};
  }
  function optSignal(tk, dir){
    const r=otMap[tk]; if(!r) return {score:0,label:"—",cls:"none",tip:""};
    const bullish=r.verdict==="bullish_setup"||r.verdict==="squeeze_risk";
    const bearish=r.verdict==="bearish_setup"||r.verdict==="hedge_bid";
    const ev=r.verdict==="event_pending";
    if(dir==="long"){
      if(bullish) return {score:1, label:"Bull-Flow",  cls:"conf",    tip:`Optionsmarkt bestätigt Long: ${esc((r.signals||[]).join(", ")||r.verdict)}`};
      if(bearish) return {score:-1,label:"Bear-Flow",  cls:"conflict",tip:`Optionsmarkt widerspricht Long: ${esc((r.signals||[]).join(", ")||r.verdict)}`};
    }else if(dir==="short"){
      if(bearish) return {score:1, label:"Bear-Flow",  cls:"conf",    tip:`Optionsmarkt bestätigt Short: ${esc((r.signals||[]).join(", ")||r.verdict)}`};
      if(bullish) return {score:-1,label:"Bull-Flow",  cls:"conflict",tip:`Optionsmarkt widerspricht Short: ${esc((r.signals||[]).join(", ")||r.verdict)}`};
    }
    if(ev) return {score:0,label:"Event",cls:"watch",tip:`Event-IV ≥4% — Katalysator pricing`};
    return {score:0,label:"Neutral",cls:"none",tip:"Kein klares Optionssignal"};
  }
  function volSignal(tk, dir){
    const ve=volEdgeMap[tk]; if(!ve) return {score:0,label:"—",cls:"none",tip:""};
    const spr=ve.spreadPp;
    const sSign=spr>=0?"+":"−";
    const sAbs=Math.abs(spr).toFixed(1);
    // For long: cheap options (günstig) = +1 (event-calls, call-replacement viable)
    //           expensive (teuer) = −1 (avoid chasing calls; outright OK but premium bid)
    // For short: cheap options (günstig) = +1 (put protection cheap = favorable)
    //            expensive (teuer) = +1 (put bid = market is pricing downside risk = confirms short thesis)
    // Actually: teuer for short = market pricing protection = slight confirming signal. Let's be conservative:
    // For short: teuer → +0 (neutral, puts expensive but thesis confirms); günstig → +1 (puts cheap, hedge viable)
    // Simpler approach: günstig = +1 for both (cheap premium = favorable execution); teuer = -1 for long (costs money to hedge), 0 for short; fair = 0 always
    if(spr<=-5){
      return {score:1, label:"Günstig",  cls:"conf",    tip:`Premium günstig: IV−RV ${sSign}${sAbs}pp — Options-Execution attraktiv`};
    }else if(spr>=5){
      if(dir==="long") return {score:-1,label:"Teuer",cls:"conflict",tip:`Premium teuer: IV−RV +${sAbs}pp — Calls überteuert, Outright bevorzugt`};
      return {score:0, label:"Teuer",    cls:"watch",   tip:`Premium teuer: IV−RV +${sAbs}pp — Puts teuer, Put-Schutz begrenzt`};
    }
    return {score:0, label:"Fair",      cls:"none",    tip:`Vol-Edge fair: IV−RV ${sSign}${sAbs}pp`};
  }
  function insSignal(tk, dir){
    const r=insMap[tk]; if(!r) return {score:0,label:"—",cls:"none",tip:""};
    const net=r.net_dollar||0;
    if(Math.abs(net)<50000) return {score:0,label:"Neutral",cls:"none",tip:`Insider-Aktivität minimal (${ (Math.abs(net)/1e3).toFixed(0)}k netto)`};
    const fmtM=v=>(Math.abs(v)>=1e6?(Math.abs(v)/1e6).toFixed(1)+"M":(Math.abs(v)/1e3).toFixed(0)+"k");
    if(dir==="long"){
      if(net>0) return {score:1, label:"Net Buy",   cls:"conf",    tip:`Insider Netto-Kauf ${fmtM(net)} — bestätigt Long`};
      else      return {score:-1,label:"Net Sell",  cls:"conflict",tip:`Insider Netto-Verkauf ${fmtM(net)} — widerspricht Long`};
    }else if(dir==="short"){
      if(net<0) return {score:1, label:"Net Sell",  cls:"conf",    tip:`Insider Netto-Verkauf ${fmtM(net)} — bestätigt Short`};
      else      return {score:-1,label:"Net Buy",   cls:"conflict",tip:`Insider Netto-Kauf ${fmtM(net)} — widerspricht Short`};
    }
    return {score:0,label:"Neutral",cls:"none",tip:""};
  }
  function sigPill(s){
    const lbl=esc(s.label);
    const tip=s.tip?` title="${esc(s.tip)}"` : "";
    return `<span class="sm-sig sm-sig-${s.cls||'none'}"${tip}>${lbl}</span>`;
  }

  // --- Build rows ---
  let totalConf=0, totalConflict=0;
  const rows=active.map(t=>{
    const tks=(t.tickers||[]);
    const tk=(tks[0]||"").toUpperCase();
    const dir=(t.direction||"").toLowerCase();
    const conv=typeof t.conviction==="number"?t.conviction:null;
    const tech=techSignal(tk, dir);
    const opt =optSignal (tk, dir);
    const vol =volSignal (tk, dir);
    const ins =insSignal (tk, dir);
    const composite=tech.score+opt.score+vol.score+ins.score;
    const nSignals=[tech,opt,vol,ins].filter(s=>s.score!==0).length;
    // Per-row composite display
    let compCls="nil", compLabel="—";
    if(nSignals===0){ compCls="nil"; compLabel="—"; }
    else if(composite>=2){ compCls="hi"; compLabel="+"+composite; }
    else if(composite===1){ compCls="hi"; compLabel="+1"; }
    else if(composite===0){ compCls="mid"; compLabel="0"; }
    else if(composite===-1){ compCls="mid"; compLabel="−1"; }
    else { compCls="lo"; compLabel="−"+Math.abs(composite); }
    if(composite>=1) totalConf++;
    if(composite<=-1) totalConflict++;
    // Conviction bar
    const convPct=conv!=null?Math.round(conv*100):null;
    const convBar=convPct!=null
      ? `<div class="sm-conv-cell" title="Conviction: ${conv.toFixed(2)}">
           <div class="sm-conv-bar" style="width:48px"><div class="sm-conv-fill ${conv>=0.5?"sm-conv-fill-hi":"sm-conv-fill-lo"}" style="width:${convPct}%"></div></div>
           <span class="sm-conv-val">${conv.toFixed(2)}</span>
         </div>`
      : '<span class="muted">—</span>';
    const dirCls=dir==="long"?"long":dir==="short"?"short":"pair";
    const tkLabel=tks.join("·");
    return `<tr>
      <td>
        <div class="sm-tk-wrap">
          <span class="sm-tk">${esc(tkLabel)}</span>
          ${dir?`<span class="sm-dir sm-dir-${dirCls}">${esc(dir)}</span>`:""}
        </div>
      </td>
      <td class="r">${convBar}</td>
      <td class="c sm-col-tech">${sigPill(tech)}</td>
      <td class="c sm-col-opt">${sigPill(opt)}</td>
      <td class="c sm-col-vol">${sigPill(vol)}</td>
      <td class="c sm-col-ins">${sigPill(ins)}</td>
      <td class="c">
        <span class="sm-comp sm-comp-${compCls}" title="Composite = Bestätigungen − Konflikte aus ${nSignals} aktiven Signalen">
          <span class="sm-comp-dot"></span>${compLabel}
        </span>
      </td>
    </tr>`;
  }).join("");

  // --- Book-wide verdict ---
  const n=active.length;
  const pctAligned=Math.round(totalConf/n*100);
  const pctConflict=Math.round(totalConflict/n*100);
  let verdChip, verdMeta;
  if(totalConf>=n*0.7){ verdChip=`<span class="sm-verdict-chip sm-verdict-aligned">Broadly Aligned</span>`; verdMeta=`${totalConf} von ${n} Calls mit positiver Signal-Summe — Buch gut koordiniert.`; }
  else if(totalConflict>=n*0.5){ verdChip=`<span class="sm-verdict-chip sm-verdict-conflict">Konflikte</span>`; verdMeta=`${totalConflict} von ${n} Calls mit negativer Signal-Summe — Review empfohlen.`; }
  else { verdChip=`<span class="sm-verdict-chip sm-verdict-split">Split</span>`; verdMeta=`${totalConf} Aligned · ${totalConflict} Konflikt · ${n-totalConf-totalConflict} Neutral — gemischtes Bild.`; }

  root.innerHTML=`<div class="panel sm-panel">
    <div class="sm-h">
      <div class="sm-h-title">Signal-Matrix — Positionssignale im Überblick</div>
      <div class="sm-h-sub">Pro offenem Call: Tech-Setup · Options-Positioning · Vol-Pricing · Insider-Flow · Composite-Score (Konfirmierungen − Konflikte)</div>
    </div>
    <div class="sm-wrap">
      <table class="sm-tbl" role="table" aria-label="Signal-Synthese pro offenem Call">
        <thead><tr>
          <th scope="col">Ticker</th>
          <th scope="col" class="r" title="Conviction-Score des aktiven Calls">Conv.</th>
          <th scope="col" class="c" title="Tech-Setup: Trend (vs MA30) + Momentum (RSI14) + Cycle-Position (52w-High%)">Tech</th>
          <th scope="col" class="c" title="Options-Tape: P/C OI, IV-Skew-Positionierung relativ zur Call-Direction">Optionen</th>
          <th scope="col" class="c" title="Vol-Edge: IV−RV30 Spread — günstig = Optionen relativ billig, teuer = relativ teuer">Vol-Edge</th>
          <th scope="col" class="c" title="Insider-Flow (30d): Net-Dollar-Flow der Form-4-Transaktionen relativ zur Call-Direction">Insider</th>
          <th scope="col" class="c" title="Composite = Σ Scores (max +4, min −4). ≥+2 grün, 0 amber, ≤−2 rot">Score</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="sm-verdict-bar">
      ${verdChip}
      <span class="sm-verdict-meta">${verdMeta}</span>
    </div>
    <div class="sm-foot">
      Signals: <b>Tech</b> = Trend (MA30) + Momentum (RSI14) + Zyklusposition (52w-High%) relativ zur Call-Direction.
      <b>Optionen</b> = OMON-Verdict aus P/C OI und IV-Skew.
      <b>Vol-Edge</b> = IV−RV30-Spread: günstig (≤−5pp) bestätigt Options-Execution; teuer (≥+5pp) bedeutet für Long-Calls Gegenwind.
      <b>Insider</b> = 30d Form-4 Open-Market Netto-Dollar, richtungsbereinigt.
      Score-Banding: ≥+2 Aligned · +1 Lean · 0 Neutral · −1 Mixed · ≤−2 Conflicted.
      Bloomberg-Pendant: MOST / PORT-M.
    </div>
  </div>`;
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

// Options-Tape (HED-137 Zyklus 107): Bloomberg-OMON-equivalent institutional-positioning panel.
// Per-ticker row showing expected-move ±% (ATM straddle / spot for nearest weekly expiry),
// P/C OI ratio with bull/bear badge, ATM IV skew with put-bid/call-bid badge, and a verdict tag.
// Each row is cross-referenced against our open calls — positioning that *confirms* the book
// gets a green badge; positioning that *contradicts* it gets a red badge so a PM sees the
// risk-flank in one glance. Edge-Artikulation per STRATEGY.md: options flow is one of the
// cleanest non-headline reads on what institutional money is actually doing.
(function renderOptionsTape(){
  const root=$("opttape");
  if(!root) return;
  const ot=D.options_tape||{};
  const rows=ot.tickers||[];
  if(!rows.length){
    root.innerHTML='<div class="panel ot-panel"><div class="ot-empty">Keine Options-Market-Signale in den letzten '+(ot.lookback_days||4)+'d — der OptionsMarketAdapter hat keine notable Threshold-Crossings emittiert oder der Feed ist still.</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }
  // Map open-call ticker → direction (long|short) so we can mark confirm/conflict
  const dirMap={};
  ((D.track_record||{}).theses||[])
    .filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date))
    .forEach(t=>{(t.tickers||[]).forEach(tk=>{
      const k=String(tk).toUpperCase();
      // If we have both long and short on same ticker (pair), mark as "pair" → watch only
      if(dirMap[k]&&dirMap[k]!==(t.direction||"").toLowerCase()) dirMap[k]="pair";
      else if(!dirMap[k]) dirMap[k]=(t.direction||"").toLowerCase();
    });});
  function bookBadge(r){
    const dir=dirMap[r.ticker];
    if(!dir) return "";
    if(dir==="pair") return '<span class="ot-book ot-book-watch" title="Long+Short Paar im Buch — neutral zur Richtung">Pair</span>';
    const bullish=r.verdict==="bullish_setup"||r.verdict==="squeeze_risk";
    const bearish=r.verdict==="bearish_setup"||r.verdict==="hedge_bid";
    if(dir==="long"&&bullish) return '<span class="ot-book ot-book-conf" title="Optionsmarkt bestätigt unseren Long-Call">Long bestätigt</span>';
    if(dir==="long"&&bearish) return '<span class="ot-book ot-book-conflict" title="Optionsmarkt widerspricht unserem Long-Call — Risikoflanke prüfen">Long im Konflikt</span>';
    if(dir==="short"&&bearish) return '<span class="ot-book ot-book-conf" title="Optionsmarkt bestätigt unseren Short-Call">Short bestätigt</span>';
    if(dir==="short"&&bullish) return '<span class="ot-book ot-book-conflict" title="Optionsmarkt widerspricht unserem Short-Call — Risikoflanke prüfen">Short im Konflikt</span>';
    // Open call but neutral positioning → just mark as held
    return '<span class="ot-book ot-book-watch" title="Aktive '+esc(dir)+'-Position im Buch — Optionsmarkt neutral">Call</span>';
  }
  function pcCell(r){
    if(r.pc==null) return '<span class="ot-pc-val" style="color:var(--mut)">—</span>';
    let badge="";
    if(r.pc<0.50) badge='<span class="ot-pc-badge ot-pc-bull" title="P/C OI < 0.50 — Calls dominieren, bullische Positionierung">Bull</span>';
    else if(r.pc>1.20) badge='<span class="ot-pc-badge ot-pc-bear" title="P/C OI > 1.20 — Puts dominieren, bärische Positionierung">Bear</span>';
    return `<span class="ot-pc-cell"><span class="ot-pc-val">${r.pc.toFixed(2)}</span>${badge}</span>`;
  }
  function skewCell(r){
    if(r.skew==null) return '<span class="ot-skew-val" style="color:var(--mut)">—</span>';
    const cls=r.skew>5?"hi":r.skew<-5?"lo":"";
    const sign=r.skew>=0?"+":"";
    let badge="";
    if(r.skew>5) badge='<span class="ot-skew-badge ot-skew-put" title="Put-IV > Call-IV um >5pp — Hedge-Nachfrage / Downside-Protection bid">Put-Bid</span>';
    else if(r.skew<-5) badge='<span class="ot-skew-badge ot-skew-call" title="Call-IV > Put-IV um >5pp — ungewöhnliche Call-Nachfrage / Squeeze-Risk">Call-Bid</span>';
    return `<span class="ot-skew-cell"><span class="ot-skew-val ${cls}">${sign}${r.skew.toFixed(1)}pp</span>${badge}</span>`;
  }
  function emoveCell(r){
    if(r.emove==null) return '<span class="ot-em" style="color:var(--mut)">—</span>';
    const cls=r.emove>=4?"hi":"";
    const exp=r.exp?`<span class="ot-em-exp">bis ${esc(r.exp.slice(5))}</span>`:"";
    return `<div><span class="ot-em ${cls}">±${r.emove.toFixed(1)}%</span>${exp}</div>`;
  }
  function vdLabel(v){
    return ({
      bullish_setup:"Bullish Setup",
      squeeze_risk:"Squeeze-Risk",
      bearish_setup:"Bearish Setup",
      hedge_bid:"Hedge-Bid",
      event_pending:"Event Pending",
      neutral:"Neutral",
    })[v]||"Neutral";
  }
  const headerKpi=`
    <div class="ot-metrics">
      <div class="ot-metric" title="Anzahl Ticker mit aktiver Options-Market-Lesung in den letzten ${ot.lookback_days}d (yfinance nächste Weekly-Expiry)">
        <span class="lbl">Ticker</span><span class="val">${rows.length}</span>
      </div>
      <div class="ot-metric" title="Bullisch positionierte Ticker (P/C OI < 0.5 oder Call-Bid Skew)">
        <span class="lbl">Bullish</span><span class="val pos">${ot.n_bullish||0}</span>
      </div>
      <div class="ot-metric" title="Bärisch positionierte Ticker (P/C OI > 1.2 oder Put-Bid Skew)">
        <span class="lbl">Bearish</span><span class="val neg">${ot.n_bearish||0}</span>
      </div>
      <div class="ot-metric" title="Ticker mit elevated expected move (≥4% Straddle-implizierte Bewegung bis Verfall) — Event/Katalysator-Pricing">
        <span class="lbl">Event-IV</span><span class="val amb">${ot.n_high_iv||0}</span>
      </div>
    </div>`;
  const tbody=rows.map(r=>{
    const book=bookBadge(r);
    return `<tr class="ot-tone-${esc(r.tone||'n')}">
      <td>
        <div class="ot-tk-row">
          <span class="ot-tk">${esc(r.ticker)}</span>
          ${book}
        </div>
      </td>
      <td class="r">${emoveCell(r)}</td>
      <td>${pcCell(r)}</td>
      <td class="col-hide-m">${skewCell(r)}</td>
      <td><span class="ot-vd ot-vd-${esc(r.verdict)}" title="${esc((r.signals||[]).join(' · ')||'kein notable Signal')}">${esc(vdLabel(r.verdict))}</span></td>
    </tr>`;
  }).join("");
  const staleNote=ot.stale?` · <span style="color:var(--amber)">⚠ Feed >2 Tage alt</span>`:"";
  root.innerHTML=`<div class="panel ot-panel">
    <div class="ot-h">
      <div>
        <div class="ot-h-title">Options-Tape — Institutionelles Positioning</div>
        <div class="ot-h-sub">P/C OI · ATM IV-Skew · Straddle-implizierter Move pro Watchlist-Ticker · sortiert nach |Signal| · nächste Weekly-Expiry${staleNote}</div>
      </div>
      ${headerKpi}
    </div>
    <div class="ot-table-wrap">
      <table class="ot-table" role="table">
        <thead><tr>
          <th scope="col">Ticker</th>
          <th scope="col" class="r" title="Straddle-implizierter Move ±% bis nächste Weekly-Expiry (ATM-Call + ATM-Put / Spot)">Exp. Move</th>
          <th scope="col" title="Put/Call Open-Interest-Ratio — < 0.5 bullisch, > 1.2 bärisch">P/C OI</th>
          <th scope="col" class="col-hide-m" title="ATM Implied-Vol-Skew (Put-IV − Call-IV) in pp — positiv = Hedge-Bid, negativ = Call-Bid">IV-Skew</th>
          <th scope="col">Verdict</th>
        </tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>
    <div class="ot-foot">
      Optionsmarkt-Positionierung aus nächster Weekly-Expiry pro Ticker. <b>Bullish Setup</b> = P/C OI &lt; 0.50; <b>Squeeze-Risk</b> = zusätzlich Call-Bid-Skew (&gt;5pp Call-Prämium); <b>Bearish Setup</b> = P/C OI &gt; 1.20; <b>Hedge-Bid</b> = Put-Prämium &gt;5pp; <b>Event Pending</b> = expected move ≥4%. Ein <span class="ot-book ot-book-conflict">Konflikt</span>-Badge markiert offene Calls, deren Optionsmarkt-Positionierung gegen unsere Richtung läuft — Risikoflanke prüfen. Quelle: <a href="https://finance.yahoo.com/options" target="_blank" rel="noopener">yfinance options chain</a> via <code>OptionsMarketAdapter</code>; gespeicherte Reads aus den letzten ${ot.lookback_days}d, kein Live-Fetch im Build.
    </div>
  </div>`;
  root.setAttribute("aria-busy","false");
})();

// Vol-Edge (HED-137 Zyklus 108): Bloomberg HVR / IVOL screen equivalent.
// Pricing-side complement to Options-Tape — joins per-ticker implied vol (from ATM
// straddle in options_tape) with realized 30-day vol (from sector_view spark) and
// surfaces the spread. Positive spread = options expensive (prefer outright /
// short premium / avoid chasing calls); negative spread = options cheap (event
// protection / catalyst-plays favored). Cross-references the book: open-call rows
// show L/S badge and an inline directional read on premium pricing.
//
//   σ_IV (ann.) = emove/79.79 × √(252/DTE)       [Brenner-Subrahmanyam ATM straddle]
//   σ_RV30      = stdev(log returns, 30d) × √252
//   spread (pp) = (σ_IV − σ_RV30) × 100
//
// Verdict bands (institutional rule-of-thumb): ≥ +5pp → "Premium teuer"; ≤ −5pp →
// "Premium günstig"; in-band → "Fair". Sort by signed spread, descending (the most
// over-priced premiums at top → first place a vol-seller looks).
(function renderIVRVEdge(){
  const root=$("ivrvedge");
  if(!root) return;
  const ot=D.options_tape||{};
  const otRows=(ot.tickers||[]).filter(r=>r.emove!=null&&r.exp);
  // Build spark map from sector_view (sparkline=last 30 closes per ticker)
  const sparkMap={};
  ((D.sector_view||{}).sectors||[]).forEach(s=>{
    (s.tickers||[]).forEach(t=>{
      if(t&&t.ticker&&Array.isArray(t.spark)&&t.spark.length>=6) sparkMap[String(t.ticker).toUpperCase()]=t.spark;
    });
  });
  // Direction map of open calls — mirrors Options-Tape so the two panels read together
  const dirMap={};
  ((D.track_record||{}).theses||[])
    .filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date))
    .forEach(t=>{(t.tickers||[]).forEach(tk=>{
      const k=String(tk).toUpperCase();
      if(dirMap[k]&&dirMap[k]!==(t.direction||"").toLowerCase()) dirMap[k]="pair";
      else if(!dirMap[k]) dirMap[k]=(t.direction||"").toLowerCase();
    });});
  // Compute realized 30d annualized vol from spark (log returns, sample stdev)
  function realizedVol(sp){
    if(!sp||sp.length<6) return null;
    const closes=sp.slice(-31);  // up to 30 returns
    const lr=[];
    for(let i=1;i<closes.length;i++){
      const p0=closes[i-1], p1=closes[i];
      if(p0>0&&p1>0) lr.push(Math.log(p1/p0));
    }
    if(lr.length<5) return null;
    const mean=lr.reduce((a,b)=>a+b,0)/lr.length;
    const v=lr.reduce((s,x)=>s+(x-mean)*(x-mean),0)/Math.max(1,lr.length-1);
    return Math.sqrt(v)*Math.sqrt(252);  // annualized decimal
  }
  // Days to expiry (calendar days, floored to ≥1)
  const today=new Date(); today.setUTCHours(0,0,0,0);
  function dteOf(exp){
    const [y,m,d]=String(exp).split("-").map(Number);
    if(!y||!m||!d) return null;
    const ex=new Date(Date.UTC(y,m-1,d));
    return Math.max(1, Math.round((ex.getTime()-today.getTime())/86400000));
  }
  // Build joined rows
  const joined=[];
  otRows.forEach(r=>{
    const tk=String(r.ticker).toUpperCase();
    const dte=dteOf(r.exp);
    if(dte==null) return;
    const sp=sparkMap[tk];
    const rv=realizedVol(sp);
    if(rv==null) return;
    // Brenner-Subrahmanyam: straddle/spot ≈ σ√(T/252)·√(2/π) ≈ 0.7979·σ√(T/252)
    const iv=(r.emove/100)/0.7979/Math.sqrt(dte/252);  // annualized decimal
    if(!isFinite(iv)||iv<=0) return;
    const spreadPp=(iv-rv)*100;
    joined.push({
      ticker: tk,
      iv, rv, dte,
      spread: spreadPp,
      emove: r.emove,
      exp: r.exp,
      dir: dirMap[tk]||null,
    });
  });
  if(!joined.length){
    root.innerHTML='<div class="panel iv-panel"><div class="iv-empty">Keine Ticker mit gleichzeitig vorhandenen Optionsdaten (Straddle-Implied-Move) und 30d-Spark-Returns — Vol-Edge benötigt beide Inputs.</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }
  // Sort by signed spread descending (most over-priced premiums first)
  joined.sort((a,b)=>b.spread-a.spread);
  // Aggregate metrics
  const nExp=joined.filter(x=>x.spread>=5).length;
  const nCheap=joined.filter(x=>x.spread<=-5).length;
  const avgSpread=joined.reduce((s,x)=>s+x.spread,0)/joined.length;
  // Bar scale: cap visualization at ±25pp for compact rendering; clip beyond
  const BAR_CAP=25;
  function bookBadge(dir){
    if(!dir) return "";
    if(dir==="pair") return '<span class="iv-book iv-book-pair" title="Long+Short Paar im Buch — neutral zur Richtung">Pair</span>';
    if(dir==="long") return '<span class="iv-book iv-book-long" title="Aktiver Long-Call im Buch">Long</span>';
    if(dir==="short") return '<span class="iv-book iv-book-short" title="Aktiver Short-Call im Buch">Short</span>';
    return "";
  }
  function actionText(row){
    // Cross-reference: directional implication of premium pricing for the open call
    if(!row.dir||row.dir==="pair") return "";
    if(row.spread>=5){
      if(row.dir==="long")  return "Outright Long bevorzugt — Calls überteuert";
      if(row.dir==="short") return "Outright Short bevorzugt — Puts überteuert";
    }else if(row.spread<=-5){
      if(row.dir==="long")  return "Call-Replacement / Event-Calls günstig";
      if(row.dir==="short") return "Put-Schutz günstig — Hedge attraktiv";
    }
    return "";
  }
  const tbody=joined.map(r=>{
    const tone=r.spread>=5?"r":r.spread<=-5?"g":"n";
    const verdict=r.spread>=5?"exp":r.spread<=-5?"cheap":"fair";
    const verdLbl=verdict==="exp"?"Premium teuer":verdict==="cheap"?"Premium günstig":"Fair";
    const spreadCls=r.spread>=5?"pos":r.spread<=-5?"neg":"mut";
    const sprSign=r.spread>=0?"+":"−";
    const sprAbs=Math.abs(r.spread).toFixed(1);
    // Divergent bar: % of cap
    const barPct=Math.min(100, Math.abs(r.spread)/BAR_CAP*50);  // half-width usage
    const bar=r.spread>0
      ? `<div class="iv-bar iv-bar-pos" style="width:${barPct.toFixed(1)}%"></div>`
      : r.spread<0
        ? `<div class="iv-bar iv-bar-neg" style="width:${barPct.toFixed(1)}%"></div>`
        : "";
    const actn=actionText(r);
    const actnHtml=actn?`<span class="iv-actn">${actn}</span>`:"";
    return `<tr class="iv-tone-${tone}">
      <td>
        <div class="iv-tk-row">
          <span class="iv-tk">${esc(r.ticker)}</span>
          ${bookBadge(r.dir)}
        </div>
      </td>
      <td class="r"><span class="iv-num">${(r.iv*100).toFixed(1)}%</span></td>
      <td class="r col-hide-m"><span class="iv-num mut">${(r.rv*100).toFixed(1)}%</span></td>
      <td class="iv-bar-cell">
        <div class="iv-bar-wrap" title="IV − RV30 Spread: ${sprSign}${sprAbs}pp · bar capped at ±${BAR_CAP}pp">
          <div class="iv-bar-axis"></div>
          ${bar}
        </div>
      </td>
      <td class="r">
        <div class="iv-spr-cell">
          <span class="iv-spr-val ${spreadCls}">${sprSign}${sprAbs}pp</span>
        </div>
      </td>
      <td class="r col-hide-m"><span class="iv-num mut">${r.dte}d</span></td>
      <td>
        <span class="iv-vd iv-vd-${verdict}" title="IV (annualisiert): ${(r.iv*100).toFixed(1)}% · RV30: ${(r.rv*100).toFixed(1)}% · Spread: ${sprSign}${sprAbs}pp">${verdLbl}</span>
        ${actnHtml}
      </td>
    </tr>`;
  }).join("");
  const headerKpi=`
    <div class="iv-metrics">
      <div class="iv-metric" title="Anzahl Ticker mit beiden Inputs (Options-Straddle und 30d Spark)">
        <span class="lbl">Ticker</span><span class="val">${joined.length}</span>
      </div>
      <div class="iv-metric" title="Ticker mit IV deutlich über RV30 (Spread ≥ +5pp) — Optionsprämien institutionell als teuer eingepreist">
        <span class="lbl">Teuer</span><span class="val pos">${nExp}</span>
      </div>
      <div class="iv-metric" title="Ticker mit IV deutlich unter RV30 (Spread ≤ −5pp) — Optionsprämien günstig im Vergleich zur kürzlich realisierten Vol">
        <span class="lbl">Günstig</span><span class="val neg">${nCheap}</span>
      </div>
      <div class="iv-metric" title="Durchschnittlicher IV − RV30 Spread über alle Ticker in pp">
        <span class="lbl">⌀ Spread</span><span class="val ${avgSpread>=5?"pos":avgSpread<=-5?"neg":""}">${avgSpread>=0?"+":"−"}${Math.abs(avgSpread).toFixed(1)}pp</span>
      </div>
    </div>`;
  root.innerHTML=`<div class="panel iv-panel">
    <div class="iv-h">
      <div>
        <div class="iv-h-title">Vol-Edge — IV vs. realisierte Vol</div>
        <div class="iv-h-sub">Implied (Straddle, annualisiert) − Realized 30d · sortiert nach Spread · positive Spread = Optionen relativ teuer · negativ = relativ günstig</div>
      </div>
      ${headerKpi}
    </div>
    <div class="iv-table-wrap">
      <table class="iv-table" role="table" aria-label="IV vs. Realized-Vol Edge pro Ticker">
        <thead><tr>
          <th scope="col">Ticker</th>
          <th scope="col" class="r" title="Annualisierte Implied Vol aus ATM-Straddle (Brenner-Subrahmanyam-Approximation)">IV (ann.)</th>
          <th scope="col" class="r col-hide-m" title="Annualisierte realisierte 30-Tage-Vol aus log-Returns">RV 30d</th>
          <th scope="col" title="Visueller Spread (IV − RV); rot/rechts = teuer, grün/links = günstig">Bar</th>
          <th scope="col" class="r" title="Spread (pp) = IV − RV30; ≥ +5 = Premium teuer, ≤ −5 = Premium günstig">Spread</th>
          <th scope="col" class="r col-hide-m" title="Days-to-Expiry der Straddle-Quotierung">DTE</th>
          <th scope="col">Verdict</th>
        </tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>
    <div class="iv-foot">
      Vol-Edge identifiziert Pricing-Dislokationen zwischen Markterwartung (Implied) und kürzlich realisierter Bewegung. Methodik: <i>σ<sub>IV</sub> = emove/79.79 · √(252/DTE)</i> (Brenner-Subrahmanyam ATM-Straddle-Approximation) vs <i>σ<sub>RV30</sub> = stdev(log-returns 30d) · √252</i>. <b>Premium teuer</b> (≥+5pp) → Outright-Position bevorzugt, Optionen-Käufer im Hintertreffen; <b>Premium günstig</b> (≤−5pp) → Event-Calls / Put-Schutz attraktiv. <b>L</b>/<b>S</b>-Badge markiert offene Calls; die Aktion-Note koppelt das Pricing-Signal an die Buch-Richtung. Quellen: ATM-Straddle aus <code>OptionsMarketAdapter</code> · 30d-Closes aus <code>sector_view</code>. Bloomberg-Pendant: <a href="https://www.bloomberg.com/professional/" target="_blank" rel="noopener">HVR</a> / IVOL.
    </div>
  </div>`;
  root.setAttribute("aria-busy","false");
})();

// Short-Squeeze-Pressure (HED-137 Zyklus 110): Bloomberg-SI-equivalent short-interest
// panel. Reads `yahoo_short_interest` raw_items aggregated by collect_short_squeeze and
// renders one row per ticker, sorted by squeeze-pressure score (SI level + |MoM|).
// Visual grammar: a horizontal bar shows SI% (saturating at 25%), bucket badge + MoM
// arrow drive preattentive triage, verdict tag (squeeze_risk / crowded_short /
// building_short / covering / baseline) gives the synthesized read. Cross-references
// open calls — a long against a high-SI name is flagged squeeze-tailwind (potential
// upside catalyst); a short against a high-SI name is flagged crowded-short risk
// (further downside is what the crowd has already underwritten, asymmetry worse).
(function renderShortSqueeze(){
  const root=$("shortpress");
  if(!root) return;
  const ss=D.short_squeeze||{};
  const rows=ss.tickers||[];
  if(!rows.length){
    root.innerHTML='<div class="panel ss-panel"><div class="ss-empty">Keine Short-Interest-Lesungen in den letzten '+(ss.lookback_days||14)+'d — der ShortInterestAdapter hat keine Daten emittiert oder der Feed ist still.</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }
  // Direction map of open calls — mirrors Options-Tape so the two panels read together
  const dirMap={};
  ((D.track_record||{}).theses||[])
    .filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date))
    .forEach(t=>{(t.tickers||[]).forEach(tk=>{
      const k=String(tk).toUpperCase();
      if(dirMap[k]&&dirMap[k]!==(t.direction||"").toLowerCase()) dirMap[k]="pair";
      else if(!dirMap[k]) dirMap[k]=(t.direction||"").toLowerCase();
    });});
  function bookBadge(r){
    const dir=dirMap[r.ticker];
    if(!dir) return "";
    const highSI=r.bucket==="high"||r.bucket==="extreme";
    if(dir==="pair") return '<span class="ss-book ss-book-watch" title="Long+Short Paar im Buch — neutral zur SI-Lesung">Pair</span>';
    if(dir==="long"&&highSI) return '<span class="ss-book ss-book-tailwind" title="Long auf einem hochgeshorteten Namen — Squeeze-Tailwind, positiver Katalysator wirkt verstärkt">Long ⚡ Squeeze</span>';
    if(dir==="short"&&highSI) return '<span class="ss-book ss-book-risk" title="Short auf einem bereits hochgeshorteten Namen — Crowded Trade, Risiko/Reward asymmetrisch schlecht">Short crowded</span>';
    if(dir==="long") return '<span class="ss-book ss-book-watch" title="Aktiver Long-Call im Buch — Short-Interest baseline">Long</span>';
    if(dir==="short") return '<span class="ss-book ss-book-watch" title="Aktiver Short-Call im Buch — Short-Interest baseline">Short</span>';
    return "";
  }
  function siCell(r){
    const cls=r.bucket==="high"||r.bucket==="extreme"?"hi":r.bucket==="elevated"?"mid":"";
    // bar width: percentage of 25% cap (SMCI's 17.9 = 71.6%, ARM 11.4 = 45.6%, etc.)
    const w=Math.min(100, (r.si/25)*100);
    return `<div class="ss-bar-cell">
      <div class="ss-bar-wrap" title="Short Interest ${r.si.toFixed(1)}% of float — bucket: ${esc(r.bucket)}"><div class="ss-bar b-${esc(r.bucket)}" style="width:${w.toFixed(1)}%"></div></div>
    </div>`;
  }
  function momCell(r){
    if(r.mom_pct==null) return '<span class="ss-mom ss-mom-mute">—</span>';
    const m=r.mom_pct;
    const cls=m>=0?"ss-mom-up":"ss-mom-dn";
    const arr=m>=0?"↑":"↓";
    return `<span class="ss-mom ${cls}" title="Änderung Short Interest vs Vormonat: ${m>=0?'+':''}${m.toFixed(0)}%">${arr}${Math.abs(m).toFixed(0)}%</span>`;
  }
  function bucketBadge(r){
    const lbl=({low:"Niedrig",elevated:"Erhöht",high:"Hoch",extreme:"Extrem"})[r.bucket]||r.bucket;
    return `<span class="ss-bk ss-bk-${esc(r.bucket)}" title="SI-Level: low <5%, elevated 5–10%, high 10–20%, extreme ≥20% of float">${esc(lbl)}</span>`;
  }
  function vdLabel(v){
    return ({
      squeeze_risk:"Squeeze-Risk",
      crowded_short:"Crowded Short",
      building_short:"Building Short",
      covering:"Covering",
      baseline:"Baseline",
    })[v]||"Baseline";
  }
  function vdTooltip(r){
    if(r.verdict==="squeeze_risk") return "SI-Level ≥10% mit nicht klar covereinder Bewegung — positiver Katalysator löst überproportionale Aufwärtsbewegung aus";
    if(r.verdict==="crowded_short") return "5–10% des Floats geshorted — Konsens-Bearish-Setup, Long-Side hat Asymmetrie";
    if(r.verdict==="building_short") return "MoM ≥+30% Short-Aufbau — bärische Conviction nimmt zu, watch für Triggers";
    if(r.verdict==="covering") return "MoM ≤−15% Short-Eindeckung — Bears geben auf, mögliches Squeeze-Echo nach Bewegung";
    return "Baseline Short-Interest, keine notable Asymmetrie";
  }
  const headerKpi=`
    <div class="ss-metrics">
      <div class="ss-metric" title="Anzahl Ticker mit aktiver SI-Lesung in den letzten ${ss.lookback_days}d">
        <span class="lbl">Ticker</span><span class="val">${rows.length}</span>
      </div>
      <div class="ss-metric" title="Ticker mit ≥20% Short Interest of float — strukturelle Squeeze-Setups">
        <span class="lbl">Extreme</span><span class="val pos">${ss.n_extreme||0}</span>
      </div>
      <div class="ss-metric" title="Ticker mit 10–20% Short Interest of float — institutionelles Squeeze-Risiko">
        <span class="lbl">High</span><span class="val pos">${ss.n_high||0}</span>
      </div>
      <div class="ss-metric" title="Ticker mit ≥+25% MoM-Aufbau der Short-Position — bärische Conviction baut sich auf">
        <span class="lbl">Building</span><span class="val amb">${ss.n_rising||0}</span>
      </div>
    </div>`;
  const tbody=rows.map(r=>{
    const book=bookBadge(r);
    const siClass=r.bucket==="high"||r.bucket==="extreme"?"hi":r.bucket==="elevated"?"mid":"";
    return `<tr class="ss-tone-${esc(r.tone||'n')}">
      <td>
        <div class="ss-tk-row">
          <span class="ss-tk">${esc(r.ticker)}</span>
          ${book}
        </div>
      </td>
      <td class="r"><span class="ss-si ${siClass}">${r.si.toFixed(1)}%</span></td>
      <td>${siCell(r)}</td>
      <td class="r col-hide-m">${momCell(r)}</td>
      <td>${bucketBadge(r)}</td>
      <td><span class="ss-vd ss-vd-${esc(r.verdict)}" title="${esc(vdTooltip(r))}">${esc(vdLabel(r.verdict))}</span></td>
    </tr>`;
  }).join("");
  const staleNote=ss.stale?` · <span style="color:var(--amber)">⚠ Feed >7 Tage alt</span>`:"";
  root.innerHTML=`<div class="panel ss-panel">
    <div class="ss-h">
      <div>
        <div class="ss-h-title">Short-Squeeze-Pressure — Float-Short-Positionierung</div>
        <div class="ss-h-sub">SI% of float · MoM-Trend · pro Watchlist-Ticker · sortiert nach |Squeeze-Score| · Pendant zu Bloomberg SI / FINRA bi-monthly settle${staleNote}</div>
      </div>
      ${headerKpi}
    </div>
    <div class="ss-table-wrap">
      <table class="ss-table" role="table">
        <thead><tr>
          <th scope="col">Ticker</th>
          <th scope="col" class="r" title="Short Interest in Prozent des Float — der primäre Squeeze-Faktor">SI%</th>
          <th scope="col" title="SI%-Bar, Skala 0–25% (≥25% saturiert) — visueller Squeeze-Druck">Pressure</th>
          <th scope="col" class="r col-hide-m" title="Änderung vs Vormonat (positiv = Aufbau, negativ = Eindeckung)">MoM</th>
          <th scope="col" title="SI-Bucket: low <5%, elevated 5–10%, high 10–20%, extreme ≥20%">Bucket</th>
          <th scope="col">Verdict</th>
        </tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>
    <div class="ss-foot">
      Short-Interest ist die direkteste Lesung darauf, wie viel <em>strukturelles bärisches Kapital</em> bereits committed ist. <b>Squeeze-Risk</b> (≥10% SI mit stabilem oder steigendem Trend) = positiver Katalysator löst überproportionale Aufwärtsbewegung aus (klassisches Squeeze-Setup à la GME/BBBY/AMC); <b>Crowded Short</b> (5–10%) = Konsens-Bearish, Long-Asymmetrie; <b>Building</b> (MoM ≥+30%) = Bärische Conviction nimmt zu, watch für Triggers; <b>Covering</b> (MoM ≤−15%) = Bears decken ein, Momentum kann beschleunigen. Ein <span class="ss-book ss-book-tailwind">Squeeze</span>-Badge markiert offene Longs auf high-SI Namen (Tailwind), ein <span class="ss-book ss-book-risk">Crowded</span>-Badge offene Shorts auf high-SI Namen (Asymmetrie-Risiko). Quelle: <a href="https://finance.yahoo.com/" target="_blank" rel="noopener">yfinance shortPercentOfFloat / shortPercentOfFloatPriorMonth</a> via <code>ShortInterestAdapter</code>; FINRA-bi-monthly Settle-Daten, Feed-Lag ≤14d.
    </div>
  </div>`;
  root.setAttribute("aria-busy","false");
})();

// EPS-Revisions-Velocity (HED-137 Zyklus 112): Bloomberg-EE/EM-equivalent sell-side
// estimate-revision panel. Reads `eps_revisions` raw_items aggregated by
// collect_eps_revisions and renders one row per ticker, sorted by combined velocity
// score. Visual grammar: a centered diverging bar shows 30d upgrades (green-right)
// vs downgrades (red-left), 7d compact reads as confirmation, signed drift % as
// the dollar-weighted magnitude (large + colored when ≥|2%|), verdict pill, accel/
// fade/reversal tag and a book cross-ref badge. A long on a tailwind name = aligned
// (momentum-confirming); short on a tailwind = fighting the tape (asymmetry-risk).
// Why this is investment-grade: sell-side EPS-revision velocity is the single
// strongest academic forward-return predictor for individual equities (Bernard &
// Thomas 1989 PEAD; modern StarMine 1-yr alpha ≈3-5%). Hedge funds buy this signal
// at 6-figure annual cost from FactSet/Refinitiv; we get it free from Yahoo IBES.
(function renderEpsRevisions(){
  const root=$("epsrev");
  if(!root) return;
  const er=D.eps_revisions||{};
  const rows=er.tickers||[];
  if(!rows.length){
    root.innerHTML='<div class="panel er-panel"><div class="er-empty">Keine direktionalen Revisions-Lesungen in den letzten '+(er.lookback_days||21)+'d — EpsRevisionsAdapter hat keine emittiert (Watchlist im Neutral-Bereich oder Feed still).</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }
  // Direction map of open calls — mirrors Short-Squeeze / Options-Tape for consistency
  const dirMap={};
  ((D.track_record||{}).theses||[])
    .filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date))
    .forEach(t=>{(t.tickers||[]).forEach(tk=>{
      const k=String(tk).toUpperCase();
      if(dirMap[k]&&dirMap[k]!==(t.direction||"").toLowerCase()) dirMap[k]="pair";
      else if(!dirMap[k]) dirMap[k]=(t.direction||"").toLowerCase();
    });});
  function bookBadge(r){
    const dir=dirMap[r.ticker];
    if(!dir) return "";
    const isPos=r.direction==="POSITIVE";
    if(dir==="pair") return '<span class="er-book er-book-watch" title="Long+Short Paar im Buch — neutral zur Revisions-Lesung">Pair</span>';
    if(dir==="long"&&isPos) return '<span class="er-book er-book-aligned" title="Long aligned mit Sell-Side-Upgrades — Momentum-Tailwind, EPS-Drift bestätigt Long-These">Long aligned</span>';
    if(dir==="long"&&!isPos) return '<span class="er-book er-book-fighting" title="Long im Cut-Zyklus — Sell-Side senkt EPS, gegen die These; falsifiziert wenn Drift &lt;−5% anhält">Long fighting</span>';
    if(dir==="short"&&!isPos) return '<span class="er-book er-book-aligned" title="Short aligned mit Sell-Side-Cuts — Bearish-Tailwind">Short aligned</span>';
    if(dir==="short"&&isPos) return '<span class="er-book er-book-fighting" title="Short im Upgrade-Zyklus — Sell-Side hebt EPS, gegen die These; Crowded-Short-Risiko wenn EPS-Drift &gt;5% anhält">Short fighting</span>';
    return "";
  }
  // Diverging 30d revision bar. Scale: 0-10 each side (caps at 10 so 8/0 saturates ~80%).
  // The midline is fixed at 50%; bar grows outward from it.
  function barCell(r){
    const upMax=10, dnMax=10;
    const upW=Math.min(50,(r.up30/upMax)*50);
    const dnW=Math.min(50,(r.down30/dnMax)*50);
    const tip=`30d Revisionen: ${r.up30} up / ${r.down30} down (Netto ${r.net30>=0?'+':''}${r.net30})`;
    return `<div class="er-bar-cell">
      <div class="er-bar-wrap" title="${esc(tip)}">
        <div class="er-bar-dn" style="width:${dnW.toFixed(1)}%"></div>
        <div class="er-bar-up" style="width:${upW.toFixed(1)}%"></div>
        <div class="er-bar-mid"></div>
      </div>
      <div class="er-bar-counts">
        <span class="dn">${r.down30||0}↓</span>
        <span class="mid">30d</span>
        <span class="up">${r.up30||0}↑</span>
      </div>
    </div>`;
  }
  function sevDayCell(r){
    if(r.up7==null&&r.down7==null) return '<span class="er-7d" title="Keine 7d-Daten">—</span>';
    const tip=`7d Revisionen: ${r.up7||0} up / ${r.down7||0} down (Netto ${r.net7>=0?'+':''}${r.net7||0})`;
    return `<span class="er-7d" title="${esc(tip)}"><span class="up">${r.up7||0}↑</span><span class="sep">/</span><span class="dn">${r.down7||0}↓</span></span>`;
  }
  function driftCell(r){
    if(r.drift_pct==null){
      return '<span class="er-drift flat" title="Kein EPS-Drift in den Daten">—</span>';
    }
    const d=r.drift_pct;
    const cls=d>=2?"pos":d<=-2?"neg":"flat";
    const strong=Math.abs(d)>=5?" er-drift-strong":"";
    const sign=d>=0?"+":"";
    const eps=r.eps_cur!=null?`<span class="er-eps">${r.eps_cur>=0?'$':'−$'}${Math.abs(r.eps_cur).toFixed(2)} Q-EPS</span>`:"";
    const tip=`EPS-Drift gegen 30d-Konsens: ${sign}${d.toFixed(1)}% (Q-EPS ${r.eps_cur!=null?'$'+r.eps_cur.toFixed(2):'?'})`;
    return `<span class="er-drift ${cls}${strong}" title="${esc(tip)}">${sign}${d.toFixed(1)}%</span>${eps}`;
  }
  function dirBadge(r){
    const cls=r.direction==="POSITIVE"?"er-dir-pos":"er-dir-neg";
    const lbl=r.direction==="POSITIVE"?"Pos":"Neg";
    return `<span class="er-dir ${cls}" title="Richtungs-Synthese: ${r.direction==='POSITIVE'?'Sell-Side hebt EPS-Schätzungen':'Sell-Side senkt EPS-Schätzungen'}">${lbl}</span>`;
  }
  function vdLabel(v){
    return ({
      strong_tailwind:"Strong Tailwind",
      tailwind:"Tailwind",
      breadth_pos:"Breadth +",
      strong_headwind:"Strong Headwind",
      headwind:"Headwind",
      breadth_neg:"Breadth −",
    })[v]||v;
  }
  function vdTooltip(v){
    return ({
      strong_tailwind:"EPS-Drift ≥+5% oder ≥8 Netto-Upgrades — institutioneller Momentum-Bid, häufig PEAD-Setup für Long-Side",
      tailwind:"EPS-Drift ≥+2% oder ≥5 Netto-Upgrades — Sell-Side hebt aktiv, Tailwind",
      breadth_pos:"Direktionale Upgrades aber kleine EPS-Drift — Breadth ohne Magnitude, niedrig-Conviction Tailwind",
      strong_headwind:"EPS-Drift ≤−5% oder ≥8 Netto-Cuts — institutionelles De-Rating, häufig PEAD-Setup für Short-Side",
      headwind:"EPS-Drift ≤−2% oder ≥5 Netto-Cuts — Sell-Side senkt aktiv, Headwind",
      breadth_neg:"Direktionale Cuts aber kleine EPS-Drift — Breadth ohne Magnitude, niedrig-Conviction Headwind",
    })[v]||"";
  }
  function accelBadge(r){
    if(!r.accel) return "";
    const lbl=({accel:"Accel",fade:"Fade",reversal:"Reversal"})[r.accel]||r.accel;
    const tip=({
      accel:"7d-Pace ≥1.5× 30d-Pace — Momentum beschleunigt diese Woche",
      fade:"7d-Pace ≤0.3× 30d-Pace — Momentum verlangsamt sich",
      reversal:"7d-Pace gegenläufig zum 30d-Trend — Richtungswechsel im Aufbau",
    })[r.accel]||"";
    return `<span class="er-accel er-accel-${esc(r.accel)}" title="${esc(tip)}">${lbl}</span>`;
  }
  function fyBadge(r){
    if(!r.fy_aligned) return "";
    const tip=`Full-Year-Revisionen aligned mit Quartal: ${r.fy_up30||0} up / ${r.fy_down30||0} down über 30d — zweite Ableitung bestätigt die Q-Bewegung`;
    return `<span class="er-fy aligned" title="${esc(tip)}">FY-aligned</span>`;
  }
  const headerKpi=`
    <div class="er-metrics">
      <div class="er-metric" title="Anzahl Watchlist-Ticker mit aktiven direktionalen Revisions-Lesungen in den letzten ${er.lookback_days}d">
        <span class="lbl">Ticker</span><span class="val">${rows.length}</span>
      </div>
      <div class="er-metric" title="Sell-Side hebt EPS-Schätzungen — Tailwind-Kandidaten">
        <span class="lbl">Pos</span><span class="val pos">${er.n_pos||0}</span>
      </div>
      <div class="er-metric" title="Sell-Side senkt EPS-Schätzungen — Headwind-Kandidaten">
        <span class="lbl">Neg</span><span class="val neg">${er.n_neg||0}</span>
      </div>
      <div class="er-metric" title="Verdict 'Strong Tailwind' oder 'Strong Headwind' — institutionelle Re-Rate-Setups (|drift| ≥5% oder ≥8 Netto-Revisionen)">
        <span class="lbl">Strong</span><span class="val amb">${er.n_strong||0}</span>
      </div>
      <div class="er-metric" title="Tickers mit 7d-Pace ≥1.5× der 30d-Pace — Revisions-Momentum beschleunigt diese Woche">
        <span class="lbl">Accel</span><span class="val amb">${er.n_accel||0}</span>
      </div>
    </div>`;
  const tbody=rows.map(r=>{
    const book=bookBadge(r);
    const anal=r.n_analysts?`<span class="er-anal" title="Anzahl Sell-Side-Analysten im aktuellen Konsens">${r.n_analysts}a</span>`:'<span class="er-anal">—</span>';
    return `<tr class="er-tone-${esc(r.tone||'n')}">
      <td>
        <div class="er-tk-row">
          <span class="er-tk">${esc(r.ticker)}</span>
          ${dirBadge(r)}
          ${book}
        </div>
      </td>
      <td>${barCell(r)}</td>
      <td class="r col-hide-m">${sevDayCell(r)}</td>
      <td class="r">${driftCell(r)}</td>
      <td class="c col-hide-m">${anal}</td>
      <td>
        <span class="er-vd er-vd-${esc(r.verdict)}" title="${esc(vdTooltip(r.verdict))}">${esc(vdLabel(r.verdict))}</span>
        ${accelBadge(r)}${fyBadge(r)}
      </td>
    </tr>`;
  }).join("");
  const staleNote=er.stale?` · <span style="color:var(--amber)">⚠ Feed >7 Tage alt</span>`:"";
  root.innerHTML=`<div class="panel er-panel">
    <div class="er-h">
      <div>
        <div class="er-h-title">EPS-Revisions-Velocity — Sell-Side-Momentum pro Watchlist-Ticker</div>
        <div class="er-h-sub">Netto-Revisions-Zählung 30d/7d · Konsens-EPS-Drift in % vs 30d ago · pro aktivem Ticker · sortiert nach |Velocity-Score|. Quelle: yfinance IBES-aggregiert (StarMine-Äquivalent). PEAD-Faktor mit nachgewiesenem 3–5% 12M-Alpha (Bernard-Thomas, Jegadeesh-Titman)${staleNote}</div>
      </div>
      ${headerKpi}
    </div>
    <div class="er-table-wrap">
      <table class="er-table" role="table">
        <thead><tr>
          <th scope="col">Ticker</th>
          <th scope="col" title="30d Revisionen: diverging Bar links=down (rot), rechts=up (grün); Skala 0–10 je Seite">30d Revisions</th>
          <th scope="col" class="r col-hide-m" title="Letzte 7 Tage: up↑ / down↓ — Bestätigung des 30d-Trends">7d</th>
          <th scope="col" class="r" title="EPS-Drift = (aktueller Konsens − 30d-Konsens) / |30d-Konsens|. ≥|2%| = direktionaler Drift, ≥|5%| = institutioneller Re-Rate">EPS-Drift</th>
          <th scope="col" class="c col-hide-m" title="Anzahl Sell-Side-Analysten im aktuellen Konsens">#A</th>
          <th scope="col">Verdict</th>
        </tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>
    <div class="er-foot">
      EPS-Revisions-Velocity ist der akademisch nachgewiesen <em>stärkste</em> Forward-Return-Predictor für Einzelaktien — die <b>PEAD-Anomalie</b> (Post-Earnings-Announcement-Drift) zeigt, dass Konsens-Schätzungen Information mit Verzögerung integrieren, was einen mehrwöchigen Drift in Richtung der Revisionen erzeugt. <b>Strong Tailwind/Headwind</b> (|drift|≥5% oder ≥8 Netto-Revisionen) markiert die institutionellen Re-Rate-Setups; <b>Accel</b> = 7d-Pace beschleunigt sich gegen 30d, <b>Fade</b> = Pace verlangsamt, <b>Reversal</b> = 7d kehrt sich gegen 30d. <b>FY-aligned</b> = Full-Year-Revisionen bestätigen Q-Bewegung (zweite Ableitung der Conviction). Buch-Cross-Ref: <span class="er-book er-book-aligned">aligned</span> = Long auf Tailwind / Short auf Headwind (Momentum-konfirmiert); <span class="er-book er-book-fighting">fighting</span> = These gegen Revisions-Drift (falsifizierbar wenn Drift anhält). Quelle: <a href="https://finance.yahoo.com/" target="_blank" rel="noopener">yfinance eps_revisions / eps_trend</a> via <code>EpsRevisionsAdapter</code> — IBES-aggregiert, Update-Lag ≤24h.
    </div>
  </div>`;
  root.setAttribute("aria-busy","false");
})();

// Technical-Levels Heatmap (HED-137 Zyklus 113): institutional price-action triage panel.
// Reads D.tech_levels from collect_tech_levels() — TechnicalLevelsAdapter source='tech_level'.
// Cards sorted by descending trigger tier (4=cross/200d, 3=52w, 2=50d, 1=RSI/vol/gap) then by
// tone severity (bearish first, then mixed, bullish, neutral). Tier-4 cards get a coloured
// left border and tinted background — the Bloomberg TECHNICAL screen equivalent.
(function renderTechLevels(){
  const root=$("techlevels");
  if(!root) return;
  const tl=D.tech_levels||{};
  const tickers=tl.tickers||[];

  if(!tickers.length){
    root.innerHTML='<div class="panel tl-panel"><div class="tl-empty">Keine aktiven technischen Trigger in den letzten '+(tl.lookback_days||7)+'d — TechnicalLevelsAdapter hat keine Schwellwert-Ereignisse emittiert (Markt seitwärts, keine Extremlagen).</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }

  const TIER_LABELS={4:"Regime",3:"Extreme",2:"Struktur",1:"Signal"};
  const TIER_BADGE_CSS={4:"tl-tier4-badge",3:"tl-tier3-badge",2:"tl-tier2-badge",1:"tl-tier1-badge"};
  const TONE_LABELS={bullish:"Bullish",bearish:"Bearish",mixed:"Mixed",neutral:"Neutral"};

  function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}

  const cards=tickers.map(row=>{
    const tone=row.tone||"neutral";
    const tier=row.tier||1;
    const url=row.url?` href="${esc(row.url)}" target="_blank" rel="noopener"`:"";
    const tierLabel=TIER_LABELS[tier]||"Signal";
    const tierBadge=TIER_BADGE_CSS[tier]||"tl-tier1-badge";
    const chips=(row.triggers||[]).map(t=>{
      const kind=t.kind||"vol";
      return `<span class="tl-chip tl-chip-${esc(kind)}">${esc(t.label)}</span>`;
    }).join("");
    const asOf=row.as_of?`<div class="tl-as-of">as of ${esc(row.as_of)}</div>`:"";
    return `<a class="tl-card tier${tier} ${esc(tone)}"${url} style="text-decoration:none;color:inherit;display:block">
      <div>
        <span class="tl-ticker ${esc(tone)}">${esc(row.ticker)}</span>
        <span class="tl-tier-badge ${tierBadge}">${esc(tierLabel)}</span>
      </div>
      <div class="tl-triggers">${chips}</div>
      ${asOf}
    </a>`;
  }).join("");

  const nBull=tl.n_bull||0, nBear=tl.n_bear||0, nTier4=tl.n_tier4||0;
  const staleWarn=tl.stale?'<span style="color:var(--red);font-weight:600">⚠ Daten älter als 3d</span>':"";
  const summaryHtml=`<div class="tl-summary">
    <span>Active Trigger: <b>${tickers.length}</b></span>
    ${nTier4?`<span><b>${nTier4}</b> Regime-Events</span>`:""}
    ${nBull?`<span class="bull">▲ ${nBull} Bullish</span>`:""}
    ${nBear?`<span class="bear">▼ ${nBear} Bearish</span>`:""}
    ${staleWarn}
  </div>`;

  root.innerHTML=`<div class="panel tl-panel">
    ${summaryHtml}
    <div class="tl-grid">${cards}</div>
    <div class="er-foot" style="margin-top:var(--s3)">
      Technical-Levels triage zeigt nur Ticker mit aktiven institutionellen Triggern — kein Rauschen. <b>Tier 4 (Regime)</b>: Golden/Death Cross (50d ↔ 200d SMA-Kreuzung = Trendwechsel-Signal) und 200d-SMA-Nähe (±2% = kritische Regime-Linie). <b>Tier 3 (Extreme)</b>: 52-Wochen-Hoch/Tief-Proximity = Momentum-Breakout oder Kapitulaion. <b>Tier 2 (Struktur)</b>: 50d-SMA-Nähe = mittelfristige Trendlinie. <b>Tier 1 (Signal)</b>: RSI-14 Oversold/Overbought + Volumen-Spike (institutioneller Flow-Indikator) + Gap-Moves. Quelle: <a href="https://finance.yahoo.com/" target="_blank" rel="noopener">yfinance OHLCV</a> via <code>TechnicalLevelsAdapter</code>; OHLCV-Daten via Yahoo Finance API, Lookback 1 Jahr (260 Handelstage), ISO-Wochen-Dedup.
    </div>
  </div>`;
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

// Quality-Scorecard · Rule-of-40 / Margin-Profil / Valuation (HED-137 Zyklus 111 — Bloomberg FA-stil).
// Per-ticker SaaS-quality screen consumed straight off sector_view consensus fundamentals.
// Rule of 40 (RevGrowth% + FCF-Margin%) is the canonical filter separating compounders worth
// premium multiples from low-quality growth. The stacked bar makes the *mix* visible —
// growth-heavy vs profitability-heavy R40s often warrant different discount rates.
(function renderQualityScore(){
  const root=$("qualityscore"); if(!root) return;
  const sv=D.sector_view||{}, tr=D.track_record||{};
  // Held tickers for ★ overlay + direction tag
  const heldMap={};
  ((tr.theses)||[]).filter(t=>t.verdict==="too_early"||(!t.verdict&&t.earliest_score_date)).forEach(t=>{
    (t.tickers||[]).forEach(tk=>{ const k=String(tk).toUpperCase();
      if(!heldMap[k]) heldMap[k]={direction:(t.direction||"").toLowerCase(),conv:t.conviction||0,label:t.label||""}; });
  });
  // Flatten ticker rows from sectors — must have either rev_growth or fcf_margin to qualify
  const rows=[];
  ((sv.sectors)||[]).forEach(sec=>{
    (sec.tickers||[]).forEach(t=>{
      if(!t||!t.ticker||!t.consensus) return;
      const c=t.consensus;
      const rg=c.rev_growth_yoy, fm=c.fcf_margin;
      if(rg==null && fm==null && c.gross_margin==null) return;
      const TK=String(t.ticker).toUpperCase();
      const r40=(rg!=null && fm!=null)?(rg+fm):null;
      rows.push({tk:TK,sector:sec.id,sectorName:sec.name,c,rg,fm,r40,
        held:heldMap[TK]||null});
    });
  });
  if(!rows.length){
    root.innerHTML='<div class="panel qs-panel"><div class="qs-empty">Keine Fundamentaldaten verfügbar — sector_view muss mit --gen-sector-view (mit Margin/FCF-Feldern) aktualisiert werden.</div></div>';
    root.setAttribute("aria-busy","false"); return;
  }
  // Sort: R40 desc with NaN at bottom, then by rev_growth desc as fallback
  rows.sort((a,b)=>{
    if(a.r40!=null && b.r40!=null) return b.r40-a.r40;
    if(a.r40!=null) return -1;
    if(b.r40!=null) return 1;
    return (b.rg||-999)-(a.rg||-999);
  });
  // Aggregate KPIs across the book (held positions only)
  const heldRows=rows.filter(r=>r.held);
  let bookR40Sum=0, bookR40N=0, bookGrossSum=0, bookGrossN=0, bookFwdPESum=0, bookFwdPEN=0;
  let elite=0;
  heldRows.forEach(r=>{
    if(r.r40!=null){ bookR40Sum+=r.r40; bookR40N++; if(r.r40>=40) elite++; }
    if(r.c.gross_margin!=null){ bookGrossSum+=r.c.gross_margin; bookGrossN++; }
    if(r.c.fwd_pe!=null){ bookFwdPESum+=r.c.fwd_pe; bookFwdPEN++; }
  });
  const bookR40=bookR40N?(bookR40Sum/bookR40N):null;
  const bookGross=bookGrossN?(bookGrossSum/bookGrossN):null;
  const bookFwdPE=bookFwdPEN?(bookFwdPESum/bookFwdPEN):null;
  // Helpers
  const pct=(v,d=1)=>v==null?"—":(v>=0?"+":"")+v.toFixed(d)+"%";
  const r40Cls=v=>v==null?"":v>=60?"elite":v>=40?"good":v>=20?"weak":"poor";
  function r40BarHtml(rg,fm,r40){
    // Scale: 80% spans the bar (so R40=80 → full). Threshold marker at 40/80=50%.
    const MAX=80;
    const clip=v=>Math.max(0,Math.min(MAX,v));
    if(r40==null){
      // partial data — show whichever segment we have
      if(rg!=null){
        const w=clip(rg)/MAX*100;
        const negCls=rg<0?"neg":"growth";
        return `<div class="qs-r40-cell">
          <div class="qs-r40-bar" role="img" aria-label="Rule of 40 Bar nur Wachstum ${rg.toFixed(1)}%">
            <span class="qs-r40-seg ${negCls}" style="width:${w.toFixed(1)}%"></span>
            <span class="qs-r40-mark" style="left:50%"></span>
          </div>
          <div class="qs-r40-lbl"><span class="g">Wachs ${pct(rg,1)}</span><span class="muted">FCF n/v</span></div>
        </div>`;
      }
      if(fm!=null){
        const w=clip(fm)/MAX*100;
        const negCls=fm<0?"neg":"fcf";
        return `<div class="qs-r40-cell">
          <div class="qs-r40-bar" role="img" aria-label="Rule of 40 Bar nur FCF-Marge ${fm.toFixed(1)}%">
            <span class="qs-r40-seg ${negCls}" style="width:${w.toFixed(1)}%"></span>
            <span class="qs-r40-mark" style="left:50%"></span>
          </div>
          <div class="qs-r40-lbl"><span class="muted">Wachs n/v</span><span class="f">FCF ${pct(fm,1)}</span></div>
        </div>`;
      }
      return '<span class="muted">—</span>';
    }
    // Both present — stacked bar; if either is negative, draw the other and tag negative
    let segHtml="";
    if(rg>=0 && fm>=0){
      const gw=clip(rg)/MAX*100, fw=clip(fm)/MAX*100;
      segHtml=`<span class="qs-r40-seg growth" style="width:${gw.toFixed(1)}%" title="Wachs ${rg.toFixed(1)}%"></span>
        <span class="qs-r40-seg fcf" style="width:${fw.toFixed(1)}%" title="FCF-Marge ${fm.toFixed(1)}%"></span>`;
    } else if(rg>=0){
      const gw=clip(rg)/MAX*100;
      segHtml=`<span class="qs-r40-seg growth" style="width:${gw.toFixed(1)}%" title="Wachs ${rg.toFixed(1)}%"></span>`;
    } else if(fm>=0){
      const fw=clip(fm)/MAX*100;
      segHtml=`<span class="qs-r40-seg fcf" style="width:${fw.toFixed(1)}%" title="FCF-Marge ${fm.toFixed(1)}%"></span>`;
    } else {
      segHtml=`<span class="qs-r40-seg neg" style="width:20%" title="Beide negativ"></span>`;
    }
    return `<div class="qs-r40-cell">
      <div class="qs-r40-bar" role="img" aria-label="Rule of 40 Bar: Wachstum ${rg.toFixed(1)}% plus FCF-Marge ${fm.toFixed(1)}% gleich ${r40.toFixed(1)}%">
        ${segHtml}
        <span class="qs-r40-mark" style="left:50%"></span>
      </div>
      <div class="qs-r40-lbl"><span class="g">Wachs ${pct(rg,1)}</span><span class="f">FCF ${pct(fm,1)}</span></div>
    </div>`;
  }
  function scoreHtml(r40){
    if(r40==null) return '<span class="muted">—</span>';
    const cls=r40Cls(r40);
    return `<span class="qs-score ${cls}">${r40>=0?"+":""}${r40.toFixed(0)}<span class="unit">%</span></span>`;
  }
  function marginsHtml(c){
    const g=c.gross_margin, o=c.op_margin;
    if(g==null && o==null) return '<span class="muted">—</span>';
    const W=v=>v==null?0:Math.max(0,Math.min(100,v));
    const fmt=v=>v==null?'<span class="muted">—</span>':(v>=0?"+":"")+v.toFixed(0)+"%";
    return `<div class="qs-marg">
      <div class="qs-marg-row" title="Gross Margin ${g!=null?g.toFixed(1)+'%':'n/v'}">
        <span class="qs-marg-lbl">GM</span>
        <span class="qs-marg-bar"><span class="qs-marg-fill ${g!=null&&g<0?'neg':'gross'}" style="width:${W(g).toFixed(0)}%"></span></span>
        <span class="qs-marg-num">${fmt(g)}</span>
      </div>
      <div class="qs-marg-row" title="Operating Margin ${o!=null?o.toFixed(1)+'%':'n/v'}">
        <span class="qs-marg-lbl">OM</span>
        <span class="qs-marg-bar"><span class="qs-marg-fill ${o!=null&&o<0?'neg':'op'}" style="width:${W(o).toFixed(0)}%"></span></span>
        <span class="qs-marg-num">${fmt(o)}</span>
      </div>
    </div>`;
  }
  function valHtml(c,rg){
    const pe=c.fwd_pe, evs=c.ev_sales;
    if(pe==null && evs==null) return '<span class="muted">—</span>';
    // PEG-style heuristic on forward P/E vs revenue growth — flag where you are paying for growth
    let pegCls="", pegTxt="";
    if(pe!=null && rg!=null && rg>5){
      const peg=pe/rg;
      pegCls=peg<1?"cheap":peg<2?"fair":peg<3?"rich":"expensive";
      pegTxt=`PEG ${peg.toFixed(1)}`;
    }
    return `<div class="qs-val">
      <span class="pe${pe==null?' muted':''}">${pe!=null?pe.toFixed(1)+'×':'—'}</span>
      <span class="evs">${evs!=null?'EV/S <strong>'+evs.toFixed(1)+'×</strong>':'EV/S n/v'}</span>
      ${pegTxt?`<span class="qs-peg ${pegCls}">${pegTxt}</span>`:""}
    </div>`;
  }
  const tbody=rows.map(r=>{
    const heldHtml=r.held?`<span class="qs-tk-star" title="Im Buch — ${esc(r.held.label)}">★</span><span class="qs-tk-dir ${r.held.direction==="long"?"long":r.held.direction==="short"?"short":""}">${esc(r.held.direction||"")}</span>`:"";
    return `<tr class="${r.held?"is-held":""}">
      <td class="l"><div class="qs-tk"><div class="qs-tk-row"><span class="qs-tk-sym">${esc(r.tk)}</span>${heldHtml}</div><span class="qs-tk-meta">${esc(r.sectorName||"")}</span></div></td>
      <td class="c">${r40BarHtml(r.rg,r.fm,r.r40)}</td>
      <td>${scoreHtml(r.r40)}</td>
      <td class="col-hide-m">${marginsHtml(r.c)}</td>
      <td>${valHtml(r.c,r.rg)}</td>
    </tr>`;
  }).join("");
  const bookR40Cls=r40Cls(bookR40);
  const bookR40Txt=bookR40==null?"—":(bookR40>=0?"+":"")+bookR40.toFixed(0)+"%";
  const bookGrossTxt=bookGross==null?"—":bookGross.toFixed(0)+"%";
  const bookFwdPETxt=bookFwdPE==null?"—":bookFwdPE.toFixed(1)+"×";
  root.innerHTML=`<div class="panel qs-panel">
    <div class="qs-h">
      <div>
        <div class="qs-h-title">Quality-Scorecard · Rule-of-40 + Margin-Profil</div>
        <div class="qs-h-sub">Rev-Growth + FCF-Marge pro Ticker — Compounder-Bias-Screen. 40%-Linie ist die kanonische Pass/Fail-Schwelle für Premium-Multiples. Sortiert nach R40 desc. Buchpositionen mit ★.</div>
      </div>
      <div class="qs-metrics" aria-label="Aggregat-Kennzahlen offener Positionen">
        <div class="qs-metric" title="Durchschnittlicher Rule-of-40-Score der Buchpositionen"><span class="lbl">Buch Ø R40</span><span class="val ${bookR40Cls}">${esc(bookR40Txt)}</span></div>
        <div class="qs-metric" title="Anzahl Buchpositionen über der 40%-Schwelle (Elite-Quality)"><span class="lbl">≥40 Elite</span><span class="val">${elite}/${heldRows.length}</span></div>
        <div class="qs-metric" title="Durchschnittliche Bruttomarge der Buchpositionen"><span class="lbl">Ø Gross Marge</span><span class="val">${esc(bookGrossTxt)}</span></div>
        <div class="qs-metric" title="Durchschnittliches Forward P/E der Buchpositionen"><span class="lbl">Ø Fwd P/E</span><span class="val">${esc(bookFwdPETxt)}</span></div>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table class="qs-tbl" aria-label="Quality-Scorecard mit Rule-of-40 pro Ticker">
        <thead><tr>
          <th class="l">Ticker</th>
          <th class="c">Rule of 40 — Wachs + FCF</th>
          <th>R40</th>
          <th class="col-hide-m">Margin-Profil</th>
          <th>Valuation</th>
        </tr></thead>
        <tbody>${tbody}</tbody>
      </table>
    </div>
    <div class="qs-foot">
      <b>Lesart:</b> Rule of 40 = Umsatzwachstum YoY + FCF-Marge (Bloomberg-FA SaaS-Screen). Werte ≥40% sind die Schwelle für Premium-Multiples in Software/Cloud; die vertikale Markierung in der Bar zeigt sie. Grüne Score-Farbe = Elite (≥60%), Cyan = Pass (≥40%), Amber = Schwach (20-40%), Rot = Quality-Lücke (<20%). Im <b>Margin-Profil</b> zählt zuerst Gross Margin (Skaleneffekt-Potenzial), dann Op Margin (operative Disziplin). Im Valuation-Cell ist <b>PEG</b> = Forward P/E ÷ Rev-Growth %: <1 günstig, 1-2 fair, 2-3 reich, >3 teuer. <b>Aktion:</b> hohe R40 + niedrige PEG = bevorzugte Long-Setup; niedrige R40 + hohe PEG = klassische Short/Underweight-Konstellation. Quelle: yfinance .info (grossMargins, freeCashflow ÷ totalRevenue, forwardPE, enterpriseToRevenue, revenueGrowth).
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

// Macro-Pulse — Bloomberg-MAC-style top-of-page market-context strip (HED-137 Zyklus 106).
// Surfaces the FRED-Makro readings we already ingest (VIX, 10Y, 2s10s, HY OAS,
// USD, Fed Funds, S&P, Nasdaq, Init Claims) as a dense 9-tile ribbon plus a
// single-line regime verdict (Risk-On/Neutral/Cautious/Risk-Off) computed from
// VIX × HY OAS × yield-curve buckets. The verdict's "Tech-Read" caption frames
// the same drivers in terms of long-duration multiple exposure — what an AI/Tech
// PM actually needs to know before reading the briefing.
(function(){
  const root=$("macropulse"); if(!root) return;
  const mp=D.macro_pulse;
  if(!mp||!mp.tiles||!mp.tiles.length){
    root.innerHTML='<div class="panel mp-panel"><div class="cs-empty">Macro-Daten nicht verfügbar — FRED-Feed liefert keine frischen Werte.</div></div>';
    root.setAttribute("aria-busy","false");
    return;
  }
  const v=mp.verdict||{};
  const _arrow=(d)=>{
    if(d==null) return "";
    if(d>0.05) return "▲";
    if(d<-0.05) return "▼";
    return "■";
  };
  const _dcls=(sid,d)=>{
    if(d==null) return "neu";
    // For VIX & HY OAS & 10Y, falling = good for tech longs → green; rising = bad → red.
    // For SP500/NASDAQ, rising = green. T10Y2Y rising (steepening) = green. ICSA falling=green.
    const invert=new Set(["VIXCLS","BAMLH0A0HYM2","DGS10","DFF","DTWEXBGS","ICSA"]);
    const dir=invert.has(sid)?-d:d;
    if(dir>0.05) return "pos";
    if(dir<-0.05) return "neg";
    return "neu";
  };
  const _dfmt=(d)=>{
    if(d==null) return "—";
    const s=d>=0?"+":"";
    return s+d.toFixed(1)+"%";
  };
  const tilesHtml=mp.tiles.map(t=>{
    const tone="tone-"+(t.tone||"");
    const dcls=_dcls(t.sid,t.delta_pct);
    const arr=_arrow(t.delta_pct);
    const dlbl=_dfmt(t.delta_pct);
    const dateLbl=t.date?t.date.slice(5):""; // mm-dd
    return `<div class="mp-tile ${tone}${t.value==null?' empty':''}" title="${esc(t.hint||'')}${t.date?' · letzter Print '+esc(t.date):''}">
      <div class="mp-tile-lbl">${esc(t.label)}</div>
      <div class="mp-tile-val">${esc(t.display)}</div>
      <div class="mp-tile-d ${dcls}"><span class="mp-tile-arrow" aria-hidden="true">${arr}</span><span>${esc(dlbl)}</span></div>
      ${dateLbl?`<div class="mp-tile-asof" aria-label="letzter Print">${esc(dateLbl)}</div>`:''}
    </div>`;
  }).join("");
  const vcolor=v.color||"n";
  const vlabel=esc(v.label||"—");
  const techRead=esc(v.tech_read||"");
  const score=(v.score!=null)?` <span class="muted" style="font-weight:500">(${v.score.toFixed(2)})</span>`:"";
  const staleBadge=mp.stale?'<span class="mp-stale" title="FRED-Feed hat in &gt;7 Tagen keinen neuen Print geliefert">⚠ stale</span>':'';
  root.innerHTML=`<div class="panel mp-panel">
    <div class="mp-verdict" role="status" aria-label="Markt-Regime ${vlabel}">
      <span class="mp-verdict-dot ${vcolor}" aria-hidden="true"></span>
      <div style="display:flex;flex-direction:column;gap:1px;line-height:1">
        <span class="mp-verdict-lbl">Regime</span>
        <span class="mp-verdict-val ${vcolor}">${vlabel}${score}</span>
      </div>
      <div class="mp-verdict-tr"><b>Tech-Read:</b> ${techRead}</div>
      <div class="mp-verdict-asof">${staleBadge} as of ${esc(mp.as_of||'')}</div>
    </div>
    <div class="mp-grid" role="list" aria-label="Macro-Indikatoren">${tilesHtml}</div>
    <div class="mp-foot">
      <b>Lesart:</b> Der farbige Punkt links ist das aggregierte Risk-Regime (VIX × HY OAS × 2s10s-Bucket; <b>grün</b> Risk-On → <b>rot</b> Risk-Off). Die Tile-Ränder markieren stresselastische Levels einzelner Indikatoren — <b>VIX</b> &gt;20 amber/&gt;25 rot, <b>HY OAS</b> &gt;4.5pp amber/&gt;6 rot, <b>2s10s</b> &lt;0 amber (Inversion = Late-Cycle-Warnung). <b>Δ-Farben sind tech-zentriert:</b> fallende Vol/Renditen/Credit-Spreads = grün (Tailwind für Long-Duration), steigende SP500/Nasdaq = grün. <b>Tech-Read</b> übersetzt dieselben Treiber in Multiple-Exposure-Sprache. Quelle: FRED via fredgraph.csv (kein API-Key).
    </div>
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
["macropulse","briefing","trackrecord","portfolioview","catalysts","sectorview","universe-scanner","consspread","earnplay","qualityscore","epsrev","techlevels","insidertape","opttape","ivrvedge","signalmatrix"].forEach(id=>{const el=$(id);if(el)el.setAttribute("aria-busy","false");});

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




























