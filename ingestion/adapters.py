"""
Brücke zu den bestehenden macro-agent-Adaptern.

Statt die Scraper zu duplizieren, importieren wir das macro-agent-Modul (es hat
einen __main__-Guard, ist also gefahrlos importierbar) und verwenden seine
Adapter-Klassen direkt wieder. Jeder Adapter liefert über .fetch() eine Liste
von Dicts der Form: {"text", "source", optional "url", optional "reliability"}.
"""
import hashlib
import importlib
import sys
from pathlib import Path

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
        ("arXiv", S.ArxivAdapter()),
        ("Hacker News", S.HackerNewsAdapter()),
        ("GitHub", S.GitHubTrendingAdapter()),
        ("Tech RSS", S.TechRSSAdapter()),
    ]
    if getattr(m, "NEWSAPI_KEY", ""):
        adapters.append(("NewsAPI AI", S.AITechNewsAPIAdapter()))
    if getattr(m, "FRED_API_KEY", ""):
        adapters.append(("FRED (Macro)", m.FREDAdapter()))
    if getattr(m, "X_AUTH_TOKEN", "") and getattr(m, "X_CT0", ""):
        adapters.append(("X/AI-Tech", S.XAITechAdapter()))
    return adapters


def _content_hash(text: str, source: str) -> str:
    return hashlib.md5(f"{text[:200]}{source}".encode()).hexdigest()


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
                    "content_hash": _content_hash(text, source),
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
