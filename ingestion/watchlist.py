"""
AI/Tech-Watchlist & Quellen-Konfiguration — zentrale Stelle zum Anpassen.
Hier definierst du, welche Firmen, Accounts, arХiv-Kategorien etc. der Feed
beobachtet. Adapter in sources_aitech.py lesen ausschließlich von hier.
"""

# Kern-Universum (AI/Tech Public Equities). Frei editierbar.
TICKERS = [
    # Halbleiter & Hardware
    "NVDA", "AMD", "TSM", "ASML", "AVGO", "MU", "ARM", "SMCI",
    "QCOM", "MRVL", "INTC", "ANET", "VRT", "DELL",
    # Hyperscaler & Big Tech
    "MSFT", "GOOGL", "AMZN", "META", "AAPL",
    # AI-Software / Apps
    "PLTR", "ORCL", "NOW", "CRM", "SNOW", "CRWD", "ADBE",
]

# SEC EDGAR: welche Filing-Typen einsammeln (8-K = Material Events, 4 = Insider)
EDGAR_FORMS = ["8-K", "4"]
EDGAR_LOOKBACK_DAYS = 3

# arXiv-Kategorien (AI/ML-Forschungsfront)
ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL", "cs.CV"]
ARXIV_MAX = 15

# GitHub: Themen für aufkommende AI-Projekte (zuletzt erstellt, nach Stars)
GITHUB_TOPICS = ["llm", "generative-ai", "ai-agents", "rag"]
GITHUB_CREATED_LOOKBACK_DAYS = 30

# Hacker News (Algolia): Begriffe + Mindest-Punktzahl (Story-Relevanz)
HN_QUERIES = ["AI", "LLM", "OpenAI", "Nvidia", "Anthropic", "GPU"]
HN_MIN_POINTS = 80

# NewsAPI-Query (falls NEWSAPI_KEY gesetzt)
NEWSAPI_QUERY = (
    '("artificial intelligence" OR "AI chip" OR GPU OR datacenter OR '
    '"large language model" OR semiconductor OR Nvidia OR OpenAI OR Anthropic) '
    'AND (earnings OR guidance OR revenue OR capex OR demand OR shortage OR '
    'export OR regulation OR funding OR launch)'
)

# Stabile AI/Tech-News-RSS-Feeds (failsafe — Fehler werden gefangen)
TECH_RSS_FEEDS = {
    "techcrunch_ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "arstechnica":   "https://feeds.arstechnica.com/arstechnica/index",
    "theverge_ai":   "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
}

# Allgemeines RSS-Lookback-Fenster (Tage). Wird vom FundingNewsAdapter genutzt.
RSS_LOOKBACK_DAYS = 3

# Dedizierte Funding/VC/IPO/Launch-Feeds (alle RSS, kein API-Key). Schließt die
# Lücke, die den Exa-$250M-Miss (HED-24) verursachte: TechRSS/NewsAPI sind
# generalistisch, runden-/launch-spezifische Meldungen fallen sonst durch.
FUNDING_RSS_FEEDS = {
    "techcrunch_startups": "https://techcrunch.com/category/startups/feed/",
    "techcrunch_funding":  "https://techcrunch.com/tag/funding/feed/",
    "techcrunch_venture":  "https://techcrunch.com/category/venture/feed/",
    "venturebeat":         "https://venturebeat.com/feed/",
    # Crunchbase News = kanonische Runden-Quelle; fängt Rounds, die nur die
    # generalistischen Feeds verpassen (Exa/Mercury/Cohere-Misses, HED-27-QC).
    "crunchbase_news":     "https://news.crunchbase.com/feed/",
}

# X/Twitter: AI/Tech-Accounts (ersetzen die Makro-Liste von macro-agent).
# category → wird zum 'source'-Wert in raw_items.
X_ACCOUNTS_AITECH = {
    # Firmen / offizielle Kanäle
    "OpenAI":         {"tier": 1, "reliability": 0.85, "category": "x_ai_company"},
    "AnthropicAI":    {"tier": 1, "reliability": 0.85, "category": "x_ai_company"},
    "GoogleDeepMind": {"tier": 1, "reliability": 0.85, "category": "x_ai_company"},
    "nvidia":         {"tier": 1, "reliability": 0.85, "category": "x_ai_company"},
    # Führungskräfte / Insider
    "sama":           {"tier": 2, "reliability": 0.80, "category": "x_ai_insider"},
    "satyanadella":   {"tier": 2, "reliability": 0.80, "category": "x_ai_insider"},
    "elonmusk":       {"tier": 2, "reliability": 0.65, "category": "x_ai_insider"},
    # Forscher
    "ylecun":         {"tier": 2, "reliability": 0.80, "category": "x_ai_research"},
    "karpathy":       {"tier": 2, "reliability": 0.80, "category": "x_ai_research"},
    "DrJimFan":       {"tier": 2, "reliability": 0.75, "category": "x_ai_research"},
    # Analysten / Markt
    "deedydas":       {"tier": 3, "reliability": 0.70, "category": "x_tech_analyst"},
    "swyx":           {"tier": 3, "reliability": 0.70, "category": "x_tech_analyst"},
    "dnystedt":       {"tier": 3, "reliability": 0.75, "category": "x_tech_analyst"},  # Asia/Semis
    "EMostaque":      {"tier": 3, "reliability": 0.65, "category": "x_tech_analyst"},
}

# SEC-Registrierungen / IPO-Pipeline (OFF-WATCHLIST, breit über alle Filer).
# getcurrent-Atom-Feed je Formtyp — fängt IPO-Registrierungen wie den SpaceX-Fall,
# die der watchlist-gebundene EDGAR-Adapter strukturell verpasst.
REGISTRATION_FORMS = ["S-1", "S-1/A", "F-1", "F-1/A", "424B4"]
REGISTRATION_COUNT = 100

# AI/Tech-Relevanz-Heuristik für off-watchlist-Filings. Treffer im Firmennamen
# → als AI/TECH markiert (Triage stuft solche Cluster hoch). Generös halten:
# jede verpasste Großmeldung ist ein Coverage-Bug (Standing Rule, COMPANY.md).
AITECH_KEYWORDS = [
    "artificial intelligence", " ai ", " ai,", " ai.", "a.i.", "machine learning",
    "deep learning", "neural", "llm", "large language", "generative", "semiconductor",
    "chip", "silicon", "gpu", "data center", "datacenter", "cloud", "software",
    "saas", "robot", "autonom", "self-driving", "quantum", "space", "satellite",
    "rocket", "launch", "cyber", "fintech", "platform", "compute", "inference",
    "foundation model", "biotech", "fusion", "battery", "lidar", "drone",
]

# Notable private/neue Player — bei Registrierung sofort hochgestuft, auch wenn
# der Name kein generisches Keyword trifft. Watchlist ist Untergrenze, kein Zaun.
NOTABLE_PRIVATE_PLAYERS = [
    "spacex", "openai", "anthropic", "databricks", "stripe", "xai", "x.ai",
    "scale ai", "anduril", "figure ai", "cerebras", "groq", "mistral",
    "perplexity", "canva", "discord", "epic games", "bytedance", "shein",
    "starlink", "neuralink", "waymo", "cruise", "rivian", "wiz",
    "coreweave", "lambda labs", "together ai", "cohere", "hugging face",
    "runway", "midjourney", "safe superintelligence", "thinking machines",
]

# Default-Reliability je neuer Quellen-Kategorie (für sources-Tabelle)
SOURCE_RELIABILITY = {
    "sec_8k": 0.95, "sec_form4": 0.90, "sec_registration": 0.92,
    "arxiv": 0.80, "github_trending": 0.60,
    "hackernews": 0.55, "tech_news": 0.60,
    "funding_news": 0.80,
}
