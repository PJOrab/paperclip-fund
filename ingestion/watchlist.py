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

# SEC EDGAR: welche Filing-Typen einsammeln (8-K = Material Events, 4 = Insider,
# SC 13D/G = >5%-Beteiligungen / Aktivisten-Stakes — Großkatalysatoren je Ticker)
EDGAR_FORMS = ["8-K", "4", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"]
# 3→5 days (Zyklus 14): SEC filings arrive Fri evening; Monday morning run
# needs 5-day window to guarantee coverage. 3 days = Friday midnight cutoff
# which may miss late-Friday Form 4s and 8-Ks (e.g. quarterly guidance updates).
EDGAR_LOOKBACK_DAYS = 5

# arXiv-Kategorien (AI/ML-Forschungsfront)
# cs.RO added (Zyklus 8): embodied AI / robotics.
# cs.AR added (Zyklus 14): computer architecture — GPU micro-architecture,
# AI accelerator design, memory systems (FlashAttention successors, custom
# ASIC papers). Directly relevant to NVDA/AMD/Cerebras/Groq thesis.
ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.RO", "cs.AR"]
ARXIV_MAX = 35

# GitHub: Themen für aufkommende AI-Projekte (zuletzt erstellt, nach Stars)
GITHUB_TOPICS = [
    "llm", "generative-ai", "ai-agents", "rag",
    # Added Zyklus 4: frontier model competition + inference efficiency stack.
    "multimodal",      # multi-modal AI (GPT-4o/Gemini/Claude-vision competitor repos)
    "llm-inference",   # inference engines, quantization, serving (compute-cost moat)
    # Added Zyklus 10: embodied AI / robotics — pairs with arXiv cs.RO (Zyklus 8).
    # NVDA Jetson, META robotics, Figure AI/1X private watch; GitHub repos lag arXiv
    # by weeks but show which codebases practitioners actually use.
    "robot-learning",  # sim-to-real transfer, RL for robots (Isaac Lab, MuJoCo wrappers)
    "embodied-ai",     # embodied AI projects (LLM + physical action interfaces)
]
GITHUB_PUSH_LOOKBACK_DAYS = 7

# Hacker News (Algolia): Begriffe + Mindest-Punktzahl (Story-Relevanz)
# Core AI terms + major watched-ticker companies not covered by generic AI terms.
HN_QUERIES = [
    "AI", "LLM", "OpenAI", "Nvidia", "Anthropic", "GPU",
    # Hyperscaler / Big Tech — major positions not in core AI query set
    "Microsoft", "Google", "Meta AI",
    # AI-native companies on watchlist
    "Palantir", "Mistral", "xAI",
    # Semiconductors / Hardware (Zyklus 11) — TSMC, ASML, AMD are top positions
    # but had zero HN coverage; these terms catch capex, supply chain, export
    # control, and competitive dynamics stories before they hit mainstream press.
    "semiconductor", "TSMC", "ASML", "AMD",
]
# Lowered 80→60 in Zyklus 12: specific-ticker queries (TSMC, ASML, AMD) rarely
# reach 80pts but are highly material. Triage reliability-weighting (Zyklus 5)
# handles noise from broader capture; better to over-capture and let AI filter.
HN_MIN_POINTS = 60

# NewsAPI-Query (falls NEWSAPI_KEY gesetzt)
NEWSAPI_QUERY = (
    '("artificial intelligence" OR "AI chip" OR GPU OR datacenter OR '
    '"large language model" OR semiconductor OR Nvidia OR OpenAI OR Anthropic) '
    'AND (earnings OR guidance OR revenue OR capex OR demand OR shortage OR '
    'export OR regulation OR funding OR launch)'
)

# Stabile AI/Tech-News-RSS-Feeds (failsafe — Fehler werden gefangen)
TECH_RSS_FEEDS = {
    "techcrunch_ai":    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "arstechnica":      "https://feeds.arstechnica.com/arstechnica/index",
    "theverge_ai":      "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    # Research-grade + financial-press additions (Zyklus 5): close the gap between
    # tech-blog coverage and serious editorial/market coverage of our tickers.
    "mit_tech_review":  "https://www.technologyreview.com/feed/",   # depth + AI policy
    "wsj_tech":         "https://feeds.a.dj.com/rss/RSSWSJD.xml",  # financial press, market-moving
    # CNBC Tech (Zyklus 15): first-mover on earnings reactions, analyst calls, M&A — covers
    # market-moving events (Workday beat, Anthropic/MSFT chip deal, SpaceX IPO) that
    # pure-tech blogs and WSJ lag on. Standard RSS 2.0, no CDATA in titles.
    "cnbc_tech":        "https://www.cnbc.com/id/19854910/device/rss/rss.html",
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
    # Hyperscalers / Infra
    "spacex", "openai", "anthropic", "databricks", "stripe", "xai", "x.ai",
    "scale ai", "anduril", "figure ai", "cerebras", "groq", "mistral",
    "perplexity", "canva", "discord", "epic games", "bytedance", "shein",
    "starlink", "neuralink", "waymo", "cruise", "rivian", "wiz",
    "coreweave", "lambda labs", "together ai", "cohere", "hugging face",
    "runway", "midjourney", "safe superintelligence", "thinking machines",
    # 2024-2026 vintage — large raises / IPO-watch / M&A candidates
    "harvey ai", "harvey",                  # legal AI, $300M+ Series D
    "cognition ai", "cognition", "devin",   # software agents, $175M
    "sierra ai", "sierra",                  # conversational AI agents, $175M
    "poolside",                             # code AI, $500M Series B
    "elevenlabs", "eleven labs",            # voice AI, $180M Series B
    "magic", "magic dev",                   # long-context coding, $465M
    "imbue",                                # reasoning AI, $200M
    "writer",                               # enterprise AI, $200M
    "glean",                                # enterprise search, $260M
    "luma ai", "luma",                      # video/3D AI, $120M
    "pika", "pika labs",                    # video generation, $80M
    "suno",                                 # music AI
    "stability ai", "stabilityai",          # image AI
    "inflection ai", "inflection",          # acquired by Microsoft — M&A watch
    "character ai", "character.ai",         # consumer AI, $150M
    "h company", "h",                       # French AI lab (ex-DeepMind)
    "mistral ai",                           # EU frontier model
    "black forest labs",                    # FLUX image models (ex-Stability)
    "nous research",                        # open-weights model research
    "moonshot ai", "kimi",                  # Chinese frontier model (Asia coverage)
    "01.ai", "zero one ai",                 # Kai-Fu Lee lab (China)
    "stepfun",                              # Chinese multimodal AI
]

# Default-Reliability je neuer Quellen-Kategorie (für sources-Tabelle)
# Yahoo Finance per-ticker RSS: full TICKERS watchlist.
# Covers market-moving ticker news (earnings, analyst calls, product events)
# that tech blogs miss. Rate: 1 req/ticker, 0.3s sleep → ~8s for 27 tickers.
YAHOO_FINANCE_TICKERS = TICKERS
YAHOO_FINANCE_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"

# Press wire feeds: official company press releases (BusinessWire, GlobeNewswire).
# These carry earnings releases, product launches, partnerships, and guidance
# updates hours before editorial coverage and before the 8-K hits SEC EDGAR.
# AI/tech keyword filtering applied in PressWireAdapter to avoid PR noise.
PRESS_WIRE_RSS_FEEDS = {
    "businesswire_tech":    "https://feed.businesswire.com/rss/home/?rss=G22",
    "globenewswire_tech":   "https://www.globenewswire.com/RssFeed/subjectcode/SC/typeofnews/PressRelease",
}

SOURCE_RELIABILITY = {
    "sec_8k": 0.95, "sec_form4": 0.90, "sec_registration": 0.92,
    # SC 13D/G = beneficial-ownership filings (>5% stake / activist position).
    # Authoritative SEC source; activist 13Ds are high-signal catalysts.
    "sec_13dg": 0.93,
    "arxiv": 0.80, "github_trending": 0.60,
    "hackernews": 0.55, "tech_news": 0.60,
    "yahoo_finance": 0.72,
    "funding_news": 0.80,
    "energy_news": 0.72,
    # Earnings dates from yfinance are authoritative forward-looking events.
    # High reliability: directly from exchange/company filings via Yahoo.
    "earnings_calendar": 0.88,
    # Federal Reserve official press releases and governor speeches.
    # Primary source for monetary policy decisions and forward guidance.
    # High reliability (official gov source) but macro — not ticker-specific.
    "fed_macro": 0.90,
    # Bureau of Labor Statistics (BLS) economic releases — CPI, PPI, jobs,
    # productivity. Primary economic data that drives Fed policy decisions.
    # Official government statistics: highest macro reliability.
    "bls_macro": 0.92,
    # Analyst rating / price-target actions detected in Yahoo Finance headlines.
    # Higher than generic yahoo_finance (0.72): analyst actions are structured
    # investment signals (upgrade/downgrade/PT change), not general news.
    "analyst_action": 0.85,
    # Confirmed earnings results (beats/misses) detected in Yahoo Finance headlines.
    # Factual confirmed data — higher than analyst_action (0.85) since earnings
    # results are reported facts, not forward-looking opinion.
    "earnings_result": 0.88,
    # Official company press releases from BusinessWire / GlobeNewswire.
    # Primary source (company-authored) but unverified/unedited — higher than
    # editorial tech news (0.60) but below SEC filings.
    "press_wire": 0.78,
    # Off-watchlist 8-K material events from AI/Tech companies (SECBroadEventsAdapter).
    # Same official SEC source as sec_8k but without item-type text extraction —
    # lower than sec_8k (0.95) since we only have the filing notice, not content.
    "sec_broad_event": 0.88,
}

# S5 Energy/Power sector feeds — AI-capex risk thesis (power/grid strain).
# datacenter_dynamics: AI data-center infra + power demand (OpenAI/Google/Hyperscaler).
# utilitydive: electric grid, utility regulation, demand growth from AI data centers.
ENERGY_RSS_FEEDS = {
    "datacenter_dynamics": "https://www.datacenterdynamics.com/en/rss/",
    "utilitydive":         "https://www.utilitydive.com/feeds/news/",
}
