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

# Default-Reliability je neuer Quellen-Kategorie (für sources-Tabelle)
SOURCE_RELIABILITY = {
    "sec_8k": 0.95, "sec_form4": 0.90,
    "arxiv": 0.80, "github_trending": 0.60,
    "hackernews": 0.55, "tech_news": 0.60,
}
