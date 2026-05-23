"""
Brücke zu den bestehenden macro-agent-Adaptern.

Statt die Scraper zu duplizieren, importieren wir das macro-agent-Modul (es hat
einen __main__-Guard, ist also gefahrlos importierbar) und verwenden seine
Adapter-Klassen direkt wieder. Jeder Adapter liefert über .fetch() eine Liste
von Dicts der Form: {"text", "source", optional "url", optional "reliability"}.
"""
import gzip
import hashlib
import importlib
import json as _json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config

_macro = None
_macro_missing = False

# Default contact UA — SEC verlangt Kontakt, GitHub verlangt überhaupt einen UA.
_DEFAULT_UA = {"User-Agent": "ai-tech-fund/0.1 (research; philipp.baro@gmail.com)"}


def fetch_url(url, headers=None, timeout=20):
    """
    Selbstständiger HTTP-GET → decodierter Text ("" bei jedem Fehlschlag).

    Bewusst stdlib-only (urllib, kein `requests`) und ohne macro-agent-Abhängigkeit,
    damit die Ingestion auch dann läuft, wenn das optionale Makro-Overlay fehlt
    (HED-125). Liefert "" statt zu werfen, passend zu den `if not text`-Guards in
    den Adaptern; gzip-Antworten werden transparent dekomprimiert.
    """
    hdrs = dict(_DEFAULT_UA)
    if headers:
        hdrs.update(headers)
    hdrs.setdefault("Accept-Encoding", "gzip")
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception:
        return ""


def fetch_json(url, headers=None, timeout=20):
    """HTTP-GET + JSON-Parse → geparstes Objekt (None bei jedem Fehlschlag)."""
    text = fetch_url(url, headers=headers, timeout=timeout)
    if not text:
        return None
    try:
        return _json.loads(text)
    except Exception:
        return None


def _load_macro():
    """
    Importiert das (optionale) macro-agent main.py einmalig als Modul.

    Das Makro-Modul liefert nur noch ein Overlay: optionale NewsAPI/X-Keys und
    die SOURCE_RELIABILITY-Map. Fehlt das Verzeichnis, läuft die Ingestion ohne
    dieses Overlay weiter (NewsAPI/X werden mangels Keys übersprungen,
    reliability fällt auf None zurück) statt mit SystemExit den GESAMTEN Lauf zu
    killen — eine fehlende optionale Quelle darf den Run nie abbrechen.
    """
    global _macro, _macro_missing
    if _macro is not None:
        return _macro
    if _macro_missing:
        return None
    macro_dir = Path(config.MACRO_AGENT_DIR)
    if not (macro_dir / "main.py").exists():
        _macro_missing = True
        print(f"⚠  macro-agent main.py nicht gefunden unter {macro_dir} — "
              f"Ingestion läuft ohne Makro-Overlay weiter "
              f"(setze MACRO_AGENT_DIR in .env)")
        return None
    sys.path.insert(0, str(macro_dir))
    _macro = importlib.import_module("main")
    return _macro


def build_adapters():
    """
    AI/Tech-Quellen-Set. Eigene Adapter aus sources_aitech.py; FRED-Makro läuft als
    schlüsselloser Adapter (fredgraph.csv) als Rates-/Liquiditäts-Overlay (Tech ist
    zinssensitiv) — unabhängig vom FRED_API_KEY, der oft fehlt (HED-92).
    Funktionsinterner Import vermeidet Zirkularität (sources_aitech → adapters).
    """
    m = _load_macro()
    from . import sources_aitech as S

    adapters = [
        ("SEC EDGAR", S.EDGARAdapter()),
        ("SEC Registrierungen", S.SECRegistrationsAdapter()),
        ("SEC Broad Events", S.SECBroadEventsAdapter()),
        ("arXiv", S.ArxivAdapter()),
        ("Hacker News", S.HackerNewsAdapter()),
        ("GitHub", S.GitHubTrendingAdapter()),
        ("Tech RSS", S.TechRSSAdapter()),
        ("Funding News", S.FundingNewsAdapter()),
        ("Energy/Power", S.EnergyNewsAdapter()),
        ("Press Wire", S.PressWireAdapter()),
        ("Fed Macro", S.MacroFedAdapter()),
        ("BLS Macro", S.MacroBLSAdapter()),
        ("Yahoo Finance", S.YahooFinanceTickerAdapter()),
        ("Earnings Calendar", S.EarningsCalendarAdapter()),
        ("FRED (Macro)", S.FREDMacroAdapter()),
        ("Short Interest", S.ShortInterestAdapter()),
        ("Options Market", S.OptionsMarketAdapter()),
        ("EPS Revisions", S.EpsRevisionsAdapter()),
        ("Gov Contracts", S.GovContractsAdapter()),
    ]
    if getattr(m, "NEWSAPI_KEY", ""):
        adapters.append(("NewsAPI AI", S.AITechNewsAPIAdapter()))
    if getattr(m, "X_AUTH_TOKEN", "") and getattr(m, "X_CT0", ""):
        adapters.append(("X/AI-Tech", S.XAITechAdapter()))
    return adapters


# Volatile metric badges that change every fetch cycle for the SAME story
# (HN points climb, GitHub stars climb) and are baked into the item text. If
# they enter the dedup hash, one story re-ingests 8-10x/day as its score ticks
# up (observed noise pattern). Strip them so the dedup key stays stable.
_VOLATILE_BADGE_RE = re.compile(r"^\[(?:HN\s+\d+\s*pts|GitHub\s+★\s*\d+)\]\s*", re.I)
_WS_RE = re.compile(r"\s+")

# Query params that carry no content identity (campaign/click tracking).
_TRACKING_PARAM_KEYS = {
    "fbclid", "gclid", "ref", "ref_src", "ref_url", "cmpid",
    "mc_cid", "mc_eid", "igshid", "spm",
}


def _canonical_url(url: str) -> str:
    """
    Canonicalize a URL into a stable identity: drop fragment, lowercase
    scheme+host, fold http/https together, strip a trailing slash, and remove
    tracking query params (utm_*, fbclid, …). Same article via different feeds
    or with different tracking tails collapses to one dedup key.
    """
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return url.strip()
    if not parts.netloc:
        return url.strip()
    scheme = "https" if parts.scheme.lower() in ("http", "https", "") else parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAM_KEYS]
    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def _normalize_text(text: str) -> str:
    """Strip volatile metric badges, collapse whitespace, lowercase, cap length."""
    t = _VOLATILE_BADGE_RE.sub("", text or "")
    return _WS_RE.sub(" ", t).strip().lower()[:200]


def _content_hash(text: str, source: str, url: str | None = None) -> str:
    """
    Stable dedup key for raw_items. Prefer the canonical URL (the strongest
    cross-fetch identity); fall back to normalized text + source when no URL
    is present. Keying on URL (not the raw point/star-laden text) is what
    stops the per-cycle re-ingestion of the same HN/GitHub story.
    """
    canon = _canonical_url(url) if url else ""
    if canon:
        return hashlib.md5(canon.encode()).hexdigest()
    return hashlib.md5(f"{_normalize_text(text)}{source}".encode()).hexdigest()


def _source_reliability(m, source: str):
    if m is None:
        return None
    return getattr(m, "SOURCE_RELIABILITY", {}).get(source)


def collect():
    """
    Führt alle Adapter aus und normalisiert die Items für raw_items.

    Returns:
        items: list[dict] bereit für DB-Upsert
        per_adapter: dict[name] = Anzahl gefetchter Items
        errors: dict[name] = Fehlertext
    """
    m = _load_macro()
    items, per_adapter, errors = [], {}, {}

    for name, adapter in build_adapters():
        try:
            fetched = adapter.fetch() or []
            per_adapter[name] = len(fetched)
            for it in fetched:
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                source = it.get("source") or "unknown"
                items.append({
                    "content_hash": _content_hash(text, source, it.get("url")),
                    "adapter": name,
                    "source": source,
                    "text": text,
                    "url": it.get("url"),
                    "reliability": it.get("reliability", _source_reliability(m, source)),
                    "raw": it,
                })
        except Exception as e:  # ein Adapter darf den Lauf nicht killen
            errors[name] = f"{type(e).__name__}: {e}"
            per_adapter[name] = 0

    return items, per_adapter, errors
