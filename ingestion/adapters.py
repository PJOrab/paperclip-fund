"""
Brücke zu den bestehenden macro-agent-Adaptern.

Statt die Scraper zu duplizieren, importieren wir das macro-agent-Modul (es hat
einen __main__-Guard, ist also gefahrlos importierbar) und verwenden seine
Adapter-Klassen direkt wieder. Jeder Adapter liefert über .fetch() eine Liste
von Dicts der Form: {"text", "source", optional "url", optional "reliability"}.
"""
import hashlib
import importlib
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config

_macro = None


def _load_macro():
    """Importiert das macro-agent main.py einmalig als Modul."""
    global _macro
    if _macro is not None:
        return _macro
    macro_dir = Path(config.MACRO_AGENT_DIR)
    if not (macro_dir / "main.py").exists():
        raise SystemExit(f"macro-agent main.py nicht gefunden unter {macro_dir} "
                         f"(setze MACRO_AGENT_DIR in .env)")
    sys.path.insert(0, str(macro_dir))
    _macro = importlib.import_module("main")
    return _macro


def build_adapters():
    """
    AI/Tech-Quellen-Set. Eigene Adapter aus sources_aitech.py; nur FRED bleibt
    aus macro-agent als Rates-/Liquiditäts-Overlay (Tech ist zinssensitiv).
    Funktionsinterner Import vermeidet Zirkularität (sources_aitech → adapters).
    """
    m = _load_macro()
    from . import sources_aitech as S

    adapters = [
        ("SEC EDGAR", S.EDGARAdapter()),
        ("SEC Registrierungen", S.SECRegistrationsAdapter()),
        ("arXiv", S.ArxivAdapter()),
        ("Hacker News", S.HackerNewsAdapter()),
        ("GitHub", S.GitHubTrendingAdapter()),
        ("Tech RSS", S.TechRSSAdapter()),
        ("Funding News", S.FundingNewsAdapter()),
        ("Energy/Power", S.EnergyNewsAdapter()),
        ("Yahoo Finance", S.YahooFinanceTickerAdapter()),
        ("Earnings Calendar", S.EarningsCalendarAdapter()),
    ]
    if getattr(m, "NEWSAPI_KEY", ""):
        adapters.append(("NewsAPI AI", S.AITechNewsAPIAdapter()))
    if getattr(m, "FRED_API_KEY", ""):
        adapters.append(("FRED (Macro)", m.FREDAdapter()))
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
    return m.SOURCE_RELIABILITY.get(source)


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
