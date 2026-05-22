"""
AI/Tech-spezifische Datenquellen.

Nutzt die HTTP-Helfer (fetch_url/fetch_json) und den XGraphQLAdapter aus
macro-agent wieder, ohne dieses Projekt zu verändern. Konfiguration kommt
ausschließlich aus watchlist.py. Jeder Adapter liefert .fetch() →
list[{text, source, url?, reliability?}].
"""
import hashlib
import html
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from . import watchlist as W
from .adapters import _load_macro


def _parse_rss_date(s: str):
    """RFC822 (RSS pubDate) oder ISO8601 (Atom) → tz-aware datetime; None bei Fehlschlag."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

# SEC verlangt einen User-Agent mit Kontakt; GitHub verlangt überhaupt einen UA.
UA = {"User-Agent": "ai-tech-fund/0.1 (research; philipp.baro@gmail.com)"}


def _m():
    return _load_macro()


# --- Form 4 (Insider) XML-Parsing -------------------------------------------
# Transaction-Code-Bedeutungen (SEC General Instruction 8). Open-Market-Käufe
# (P) und -Verkäufe (S) sind das diskretionäre Signal; A/M/F/G sind
# Comp/Routine und für bull/bear-Lesbarkeit klar abzugrenzen.
_F4_CODE_MEANING = {
    "P": "open-market buy", "S": "open-market sale", "A": "grant/award",
    "M": "option exercise", "F": "tax-withholding", "G": "gift",
    "C": "conversion", "X": "option exercise", "D": "disposition to issuer",
    "V": "voluntary report", "J": "other acq/disp",
}
_F4_TXN_RE = re.compile(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", re.DOTALL)
_F4_OWNER_RE = re.compile(r"<rptOwnerName>(.*?)</rptOwnerName>", re.DOTALL)
_F4_CODE_RE = re.compile(r"<transactionCode>([^<]*)</transactionCode>")


def _f4_val(block: str, outer: str) -> str:
    """Extrahiert <outer>…<value>X</value>… aus einem Form-4-Block."""
    m = re.search(rf"<{outer}>\s*<value>([^<]*)</value>", block, re.DOTALL)
    return m.group(1).strip() if m else ""


def _f4_role(xml: str) -> str:
    rel = re.search(r"<reportingOwnerRelationship>(.*?)</reportingOwnerRelationship>", xml, re.DOTALL)
    if not rel:
        return ""
    blk = rel.group(1)
    roles = []
    if re.search(r"<isDirector>\s*(1|true)\s*</isDirector>", blk, re.I):
        roles.append("director")
    if re.search(r"<isOfficer>\s*(1|true)\s*</isOfficer>", blk, re.I):
        title = (re.search(r"<officerTitle>(.*?)</officerTitle>", blk, re.DOTALL) or [None, ""])
        t = html.unescape(title.group(1).strip()) if hasattr(title, "group") else ""
        roles.append(f"officer: {t}" if t else "officer")
    if re.search(r"<isTenPercentOwner>\s*(1|true)\s*</isTenPercentOwner>", blk, re.I):
        roles.append("10% owner")
    return ", ".join(roles)


def _fmt_sh(n: float) -> str:
    return f"{int(round(n)):,}"


def _fmt_px(p: float) -> str:
    return f"${p:,.2f}" if p else "$0"


def _summarize_form4(xml: str) -> str:
    """
    Verdichtet eine Form-4-Ownership-XML zu einer bull/bear-lesbaren Zeile:
    Richtung (BUY/SELL/MIXED/routine), Shares, Preis je Code, Holdings danach.
    Gibt '' zurück, wenn keine nonDerivative-Transaktion gefunden wird.
    """
    txns = []
    for blk in _F4_TXN_RE.findall(xml):
        cm = _F4_CODE_RE.search(blk)
        code = cm.group(1).strip() if cm else ""
        try:
            shares = float(_f4_val(blk, "transactionShares") or 0)
        except ValueError:
            shares = 0.0
        try:
            price = float(_f4_val(blk, "transactionPricePerShare") or 0)
        except ValueError:
            price = 0.0
        ad = _f4_val(blk, "transactionAcquiredDisposedCode")
        post = _f4_val(blk, "sharesOwnedFollowingTransaction")
        txns.append((code, shares, price, ad, post))
    if not txns:
        return ""

    has_buy = any(c == "P" for c, *_ in txns)
    has_sell = any(c == "S" for c, *_ in txns)
    if has_buy and not has_sell:
        signal = "OPEN-MARKET BUY"
    elif has_sell and not has_buy:
        signal = "OPEN-MARKET SALE"
    elif has_buy and has_sell:
        signal = "MIXED open-market"
    else:
        signal = "routine (grant/exercise/tax)"

    # Pro Code aggregieren: Shares (Summe) und Volumen-gewichteter Preis.
    agg = {}  # code -> [shares, dollar_vol, ad]
    for code, shares, price, ad, _post in txns:
        a = agg.setdefault(code, [0.0, 0.0, ad])
        a[0] += shares
        a[1] += shares * price
        if ad:
            a[2] = ad
    parts = []
    for code in sorted(agg):
        sh, dvol, ad = agg[code]
        meaning = _F4_CODE_MEANING.get(code, code or "?")
        vwap = (dvol / sh) if sh else 0.0
        dirw = "+" if ad == "A" else ("-" if ad == "D" else "")
        parts.append(f"{code} {meaning} {dirw}{_fmt_sh(sh)} @ {_fmt_px(vwap)}")
    post_final = next((p for *_x, p in reversed(txns) if p), "")
    holds = f"; holds {_fmt_sh(float(post_final))} after" if post_final else ""
    return f"{signal} — " + "; ".join(parts) + holds


class EDGARAdapter:
    """SEC-Pflichtmeldungen (8-K Material Events, Form 4 Insider-Trades) je Ticker."""
    TICKERS_MAP = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

    def __init__(self):
        self._cik = None  # {TICKER: (cik:int, title)}

    def _load_cik_map(self):
        data = _m().fetch_json(self.TICKERS_MAP, headers=UA, timeout=20)
        out = {}
        if isinstance(data, dict):
            for row in data.values():
                out[str(row.get("ticker", "")).upper()] = (
                    int(row["cik_str"]), row.get("title", ""))
        return out

    def fetch(self):
        m = _m()
        if self._cik is None:
            self._cik = self._load_cik_map()
        if not self._cik:
            return []
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=W.EDGAR_LOOKBACK_DAYS)
        forms = set(W.EDGAR_FORMS)
        out = []
        for tk in W.TICKERS:
            ent = self._cik.get(tk.upper())
            if not ent:
                continue
            cik, title = ent
            data = m.fetch_json(self.SUBMISSIONS.format(cik=cik), headers=UA, timeout=20)
            time.sleep(0.2)  # SEC: max 10 req/s
            if not data:
                continue
            recent = data.get("filings", {}).get("recent", {})
            form_l = recent.get("form", [])
            date_l = recent.get("filingDate", [])
            acc_l = recent.get("accessionNumber", [])
            doc_l = recent.get("primaryDocument", [])
            desc_l = recent.get("primaryDocDescription", [])
            for i, form in enumerate(form_l):
                if form not in forms:
                    continue
                try:
                    fdate = datetime.strptime(date_l[i], "%Y-%m-%d").date()
                except Exception:
                    continue
                if fdate < cutoff:
                    continue
                acc = acc_l[i].replace("-", "") if i < len(acc_l) else ""
                doc = doc_l[i] if i < len(doc_l) else ""
                desc = (desc_l[i] if i < len(desc_l) else "") or form
                src = "sec_form4" if form == "4" else "sec_8k"
                label = "Insider Form 4" if form == "4" else "8-K"
                acc_disp = acc_l[i] if i < len(acc_l) else ""
                doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
                detail = desc
                if form == "4" and acc and doc:
                    # Roh-Ownership-XML liegt unter demselben Pfad ohne den
                    # xslF345XNN/-Render-Prefix. Anreichern um Richtung/Größe/Preis,
                    # damit Insider-Cluster bull/bear-lesbar werden. Schlägt der
                    # Fetch/Parse fehl, bleibt die Notice-Zeile erhalten (ein
                    # einzelnes Filing darf den Run nie kippen).
                    try:
                        raw_doc = doc.rsplit("/", 1)[-1]
                        raw_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{raw_doc}"
                        xml = m.fetch_url(raw_url, headers=UA, timeout=20)
                        time.sleep(0.15)  # SEC: max 10 req/s
                        if xml:
                            summary = _summarize_form4(xml)
                            if summary:
                                owner_m = _F4_OWNER_RE.search(xml)
                                owner = html.unescape(owner_m.group(1).strip()) if owner_m else ""
                                role = _f4_role(xml)
                                who = owner + (f" ({role})" if role else "")
                                detail = f"{who} — {summary}" if who else summary
                    except Exception:
                        pass
                out.append({
                    # Accession-Nr. im Text → jede Einreichung distinkt (Dedup-Hash)
                    "text": f"[EDGAR {label}] {tk} {title}: {detail} (filed {date_l[i]}, {acc_disp})",
                    "source": src,
                    "url": doc_url,
                    "reliability": W.SOURCE_RELIABILITY.get(src),
                })
        return out


class SECRegistrationsAdapter:
    """
    Breiter SEC-Registrierungs-/IPO-Feed (OFF-WATCHLIST). Zieht den getcurrent-
    Atom-Feed je Formtyp (S-1, S-1/A, F-1, 424B …) über ALLE Filer — nicht nur
    Watchlist-CIKs. Fängt IPO-Registrierungen wie den SpaceX-Fall, die der
    watchlist-gebundene EDGARAdapter strukturell verpasst.

    AI/Tech-Relevanz wird per Firmenname markiert ([AI/TECH] vs [other]) als
    Signal für die Triage. Nicht-relevante Micro-Cap/SPAC-Filings werden hier
    weggelassen, um das Feed-Budget zu schützen — die Watchlist bleibt aber eine
    Untergrenze, kein Zaun: jeder AI/Tech- oder Notable-Player-Treffer kommt rein.
    """
    BASE = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type={typ}&output=atom&count={count}")
    # SEC-Pflicht: User-Agent mit Organisation + Kontakt.
    HEADERS = {"User-Agent": "HedgingAlphaFund research philipp.baro@gmail.com"}

    _ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
    _TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
    _HREF_RE = re.compile(r'<link[^>]*href="([^"]+)"')
    _TERM_RE = re.compile(r'term="([^"]+)"')
    _ACC_RE = re.compile(r"accession-number=([0-9-]+)")
    _FILED_RE = re.compile(r"Filed:.*?(\d{4}-\d{2}-\d{2})")

    def _is_aitech(self, name: str) -> bool:
        low = f" {name.lower()} "
        if any(p in low for p in W.NOTABLE_PRIVATE_PLAYERS):
            return True
        return any(k in low for k in W.AITECH_KEYWORDS)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        for typ in W.REGISTRATION_FORMS:
            url = self.BASE.format(typ=quote(typ), count=W.REGISTRATION_COUNT)
            xml = m.fetch_url(url, headers=self.HEADERS, timeout=20)
            time.sleep(0.3)  # SEC: max 10 req/s
            if not xml:
                continue
            for block in self._ENTRY_RE.findall(xml):
                t = self._TITLE_RE.search(block)
                if not t:
                    continue
                title = re.sub(r"\s+", " ", t.group(1)).strip()
                acc_m = self._ACC_RE.search(block)
                acc = acc_m.group(1) if acc_m else ""
                if not acc or acc in seen:
                    continue
                # Title-Format: "S-1 - Company Name (0001234567) (Filer)"
                term = self._TERM_RE.search(block)
                form = (term.group(1) if term
                        else (title.split(" - ", 1)[0] if " - " in title else "?"))
                company = title.split(" - ", 1)[1] if " - " in title else title
                company = re.sub(r"\s*\(\d{6,}\)\s*\(.*?\)\s*$", "", company).strip()
                if not company:
                    continue
                relevant = self._is_aitech(company)
                if not relevant:
                    continue  # Budgetschutz: nur AI/Tech- oder Notable-Player-Filings
                seen.add(acc)
                href = self._HREF_RE.search(block)
                filed = self._FILED_RE.search(block)
                filed_s = filed.group(1) if filed else ""
                out.append({
                    "text": (f"[SEC {form} · AI/TECH] {company} — Registrierung/IPO-Filing "
                             f"(filed {filed_s}, {acc})"),
                    "source": "sec_registration",
                    "url": href.group(1).strip() if href else None,
                    "reliability": W.SOURCE_RELIABILITY.get("sec_registration"),
                })
        return out


class ArxivAdapter:
    """Neueste AI/ML-Paper (Forschungsfront) via arXiv-API."""
    def fetch(self):
        q = "+OR+".join(f"cat:{c}" for c in W.ARXIV_CATEGORIES)
        url = (f"http://export.arxiv.org/api/query?search_query={q}"
               f"&start=0&max_results={W.ARXIV_MAX}"
               f"&sortBy=submittedDate&sortOrder=descending")
        xml = _m().fetch_url(url, timeout=20)
        if not xml:
            return []
        out = []
        for entry in xml.split("<entry>")[1:]:
            t = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
            link = re.search(r"<id>(.*?)</id>", entry)
            if not t:
                continue
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            if not title:
                continue
            # Include abstract so triage/analyst can reason about content,
            # not just paper titles. Truncate to 250 chars to stay token-efficient.
            summary_m = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
            abstract = ""
            if summary_m:
                abstract = re.sub(r"\s+", " ", summary_m.group(1)).strip()
                if len(abstract) > 250:
                    abstract = abstract[:247] + "..."
            text = f"[arXiv] {title}" + (f" — {abstract}" if abstract else "")
            out.append({
                "text": text,
                "source": "arxiv",
                "url": link.group(1).strip() if link else None,
                "reliability": W.SOURCE_RELIABILITY["arxiv"],
            })
        return out


class HackerNewsAdapter:
    """Hochbewertete HN-Stories zu AI/Tech (Algolia-Suche, nach Datum + Punkten)."""
    def fetch(self):
        m = _m()
        out, seen = [], set()
        for q in W.HN_QUERIES:
            url = (f"https://hn.algolia.com/api/v1/search_by_date?tags=story"
                   f"&query={quote(q)}&numericFilters=points>{W.HN_MIN_POINTS}"
                   f"&hitsPerPage=5")
            data = m.fetch_json(url, timeout=15)
            time.sleep(0.3)
            if not data:
                continue
            for h in data.get("hits", []):
                oid = h.get("objectID")
                title = h.get("title") or ""
                if not oid or oid in seen or not title:
                    continue
                seen.add(oid)
                pts = h.get("points", 0)
                out.append({
                    "text": f"[HN {pts}pts] {title}",
                    "source": "hackernews",
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    "reliability": W.SOURCE_RELIABILITY["hackernews"],
                })
        return out


class GitHubTrendingAdapter:
    """
    Active AI-Repos: recently-pushed high-star repos per topic (GitHub Search-API).
    Switched from created:>30d to pushed:>7d so we capture development activity on
    established projects (vLLM, llama.cpp, SGLang, etc.) — more actionable for the
    AI-compute thesis than newly-created repos with few stars.
    """
    def fetch(self):
        m = _m()
        headers = {**UA, "Accept": "application/vnd.github+json"}
        since = (datetime.now(timezone.utc).date()
                 - timedelta(days=getattr(W, "GITHUB_PUSH_LOOKBACK_DAYS", 7))).isoformat()
        out, seen = [], set()
        for topic in W.GITHUB_TOPICS:
            q = f"topic:{topic} pushed:>{since}"
            url = (f"https://api.github.com/search/repositories?q={quote(q)}"
                   f"&sort=stars&order=desc&per_page=5")
            data = m.fetch_json(url, headers=headers, timeout=15)
            time.sleep(2)  # Search-API: ~10 req/min unauthentifiziert
            if not data:
                continue
            for r in data.get("items", []):
                full = r.get("full_name")
                if not full or full in seen:
                    continue
                seen.add(full)
                stars = r.get("stargazers_count", 0)
                desc = (r.get("description") or "").strip()
                out.append({
                    "text": f"[GitHub ★{stars}] {full}: {desc}"[:280],
                    "source": "github_trending",
                    "url": r.get("html_url"),
                    "reliability": W.SOURCE_RELIABILITY["github_trending"],
                })
        return out


class TechRSSAdapter:
    """Kuratierte AI/Tech-News-RSS/Atom-Feeds (failsafe je Feed)."""
    def fetch(self):
        m = _m()
        out = []
        for name, feed in W.TECH_RSS_FEEDS.items():
            try:
                text = m.fetch_url(feed, timeout=15)
                if not text:
                    continue
                sep = "<item>" if "<item>" in text else "<entry>"
                for block in text.split(sep)[1:6]:
                    # Match <title> OR <title type="html"> (Atom feeds use attributes)
                    t = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
                    if not t:
                        continue
                    # Strip CDATA wrapper first (before HTML-tag strip, or CDATA is
                    # treated as one big tag and the whole title disappears).
                    raw = t.group(1).replace("<![CDATA[", "").replace("]]>", "")
                    title = re.sub(r"<[^>]+>", "", raw)
                    title = (title.replace("&amp;", "&").replace("&#039;", "'").strip())
                    if not title:
                        continue
                    link = (re.search(r'<link[^>]*href="([^"]+)"', block)
                            or re.search(r"<link>(.*?)</link>", block))
                    out.append({
                        "text": f"[{name}] {title}",
                        "source": "tech_news",
                        "url": link.group(1).strip() if link else None,
                        "reliability": W.SOURCE_RELIABILITY["tech_news"],
                    })
            except Exception:
                continue
        return out


class FundingNewsAdapter:
    """
    Dedizierter Funding/VC/IPO/Launch-Feed (RSS, kein API-Key). Schließt die
    Lücke, die den Exa-$250M-Miss (HED-24) verursachte: die generalistischen
    TechRSS-/NewsAPI-Adapter lassen runden-/launch-spezifische Meldungen
    durchfallen. Quellen: TechCrunch Startups + Funding, VentureBeat.

    - Dedup pro Lauf via URL-Hash (mehrere Feeds überlappen).
    - Lookback W.RSS_LOOKBACK_DAYS (Fallback 3 Tage), Items ohne parsbares
      Datum werden behalten (Coverage > Präzision: lieber rein als verpassen).
    - Fehler je Feed sind gefangen — ein toter Feed killt den Adapter nicht.
    """
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        for name, feed in W.FUNDING_RSS_FEEDS.items():
            try:
                text = m.fetch_url(feed, timeout=15)
                if not text:
                    continue
                sep = "<item>" if "<item>" in text else "<entry>"
                for block in text.split(sep)[1:21]:
                    t = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
                    if not t:
                        continue
                    raw = t.group(1).replace("<![CDATA[", "").replace("]]>", "")
                    title = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
                    if not title:
                        continue
                    link_m = (re.search(r'<link[^>]*href="([^"]+)"', block)
                              or re.search(r"<link>(.*?)</link>", block))
                    url = link_m.group(1).strip() if link_m else None
                    key = hashlib.md5((url or title).encode()).hexdigest()
                    if key in seen:
                        continue
                    seen.add(key)
                    date_m = (re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
                              or re.search(r"<updated>(.*?)</updated>", block, re.DOTALL)
                              or re.search(r"<published>(.*?)</published>", block, re.DOTALL))
                    if date_m:
                        pub = _parse_rss_date(date_m.group(1).strip())
                        if pub and pub < cutoff:
                            continue
                    out.append({
                        "text": f"[Funding · {name}] {title}",
                        "source": "funding_news",
                        "url": url,
                        "reliability": W.SOURCE_RELIABILITY.get("funding_news", 0.80),
                    })
            except Exception:
                continue
        return out


class EnergyNewsAdapter:
    """
    S5 Energy/Power sector feed — AI-capex risk thesis (power/grid strain).
    Quellen: Data Center Dynamics (AI data-center infra + hyperscaler power demand)
    + Utility Dive (electric grid / utility regulation). Gleiche RSS-Parse-Logik
    wie FundingNewsAdapter; Fehler je Feed gefangen, lookback = RSS_LOOKBACK_DAYS.
    """
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        for name, feed in W.ENERGY_RSS_FEEDS.items():
            try:
                text = m.fetch_url(feed, timeout=15)
                if not text:
                    continue
                sep = "<item>" if "<item>" in text else "<entry>"
                for block in text.split(sep)[1:21]:
                    t = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
                    if not t:
                        continue
                    raw = t.group(1).replace("<![CDATA[", "").replace("]]>", "")
                    title = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
                    if not title:
                        continue
                    link_m = (re.search(r'<link[^>]*href="([^"]+)"', block)
                              or re.search(r"<link>(.*?)</link>", block))
                    url = link_m.group(1).strip() if link_m else None
                    key = hashlib.md5((url or title).encode()).hexdigest()
                    if key in seen:
                        continue
                    seen.add(key)
                    date_m = (re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
                              or re.search(r"<updated>(.*?)</updated>", block, re.DOTALL)
                              or re.search(r"<published>(.*?)</published>", block, re.DOTALL))
                    if date_m:
                        pub = _parse_rss_date(date_m.group(1).strip())
                        if pub and pub < cutoff:
                            continue
                    out.append({
                        "text": f"[Energy · {name}] {title}",
                        "source": "energy_news",
                        "url": url,
                        "reliability": W.SOURCE_RELIABILITY.get("energy_news", 0.72),
                    })
            except Exception:
                continue
        return out


class YahooFinanceTickerAdapter:
    """
    Yahoo Finance per-ticker RSS headlines for top watchlist positions.
    Carries market-moving ticker news (earnings, analyst upgrades/downgrades,
    product announcements) that tech-blog adapters routinely miss.
    One request per ticker with polite 0.3s sleep. Dedup by URL across tickers.
    Lookback = RSS_LOOKBACK_DAYS (default 3) to filter stale articles.
    """
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        for ticker in W.YAHOO_FINANCE_TICKERS:
            try:
                url = W.YAHOO_FINANCE_RSS.format(ticker=ticker)
                text = m.fetch_url(url, timeout=15)
                time.sleep(0.3)
                if not text:
                    continue
                for block in text.split("<item>")[1:]:
                    t = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
                    if not t:
                        continue
                    raw = t.group(1).replace("<![CDATA[", "").replace("]]>", "")
                    title = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
                    if not title:
                        continue
                    link_m = re.search(r"<link>(.*?)</link>", block)
                    item_url = link_m.group(1).strip() if link_m else None
                    key = item_url or title
                    if key in seen:
                        continue
                    seen.add(key)
                    # Skip stale articles; keep undated items (Coverage > Precision)
                    date_m = re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
                    if date_m:
                        pub = _parse_rss_date(date_m.group(1).strip())
                        if pub and pub < cutoff:
                            continue
                    out.append({
                        "text": f"[{ticker}] {title}",
                        "source": "yahoo_finance",
                        "url": item_url,
                        "reliability": W.SOURCE_RELIABILITY.get("yahoo_finance", 0.72),
                    })
            except Exception:
                continue
        return out


class AITechNewsAPIAdapter:
    """NewsAPI mit AI/Tech-Equity-Query (nur wenn NEWSAPI_KEY gesetzt)."""
    def fetch(self):
        m = _m()
        key = getattr(m, "NEWSAPI_KEY", "")
        if not key:
            return []
        url = (f"https://newsapi.org/v2/everything?q={quote(W.NEWSAPI_QUERY)}"
               f"&language=en&sortBy=publishedAt&pageSize=15&apiKey={key}")
        data = m.fetch_json(url, timeout=15)
        out = []
        if data and data.get("status") == "ok":
            for art in data.get("articles", []):
                title = art.get("title") or ""
                if not title:
                    continue
                desc = art.get("description") or ""
                out.append({
                    "text": f"{title}. {desc[:200]}",
                    "source": "tech_news",
                    "url": art.get("url"),
                    "reliability": W.SOURCE_RELIABILITY.get("tech_news", 0.60),
                })
        return out


class EarningsCalendarAdapter:
    """
    Upcoming earnings dates for all watchlist tickers via yfinance.

    Generates early-warning items when a company is about to report:
      "[NVDA] Earnings in 3 days (2026-05-28) — Nvidia Corp"
      "[MSFT] Earnings TOMORROW (2026-05-22) — Microsoft Corp"
      "[GOOGL] Earnings today! (2026-05-22) — Alphabet Inc"

    Items are generated for events 0-14 days out. The text is stable
    per (ticker, date) so dedup suppresses repeats across runs.
    Silently skips tickers where yfinance returns no data.
    """

    LOOKAHEAD_DAYS = 14

    def fetch(self) -> list[dict]:
        try:
            import yfinance as yf
        except ImportError:
            return []

        today = datetime.now(timezone.utc).date()
        out: list[dict] = []

        for ticker in W.TICKERS:
            try:
                t = yf.Ticker(ticker)
                cal = t.calendar  # dict or DataFrame depending on yfinance version
                if cal is None:
                    continue

                # yfinance ≥0.2 returns a dict with key "Earnings Date"
                earnings_date = None
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        # may be a list or a single value
                        if isinstance(ed, list):
                            ed = ed[0] if ed else None
                        from datetime import date as _date
                        if isinstance(ed, _date) and not isinstance(ed, datetime):
                            # yfinance returns datetime.date directly
                            earnings_date = ed
                        elif hasattr(ed, "date"):
                            earnings_date = ed.date()
                        elif isinstance(ed, str):
                            try:
                                earnings_date = datetime.fromisoformat(ed).date()
                            except Exception:
                                pass
                else:
                    # older yfinance returns a DataFrame; grab first value from row
                    try:
                        col = cal.loc["Earnings Date"] if "Earnings Date" in cal.index else None
                        if col is not None:
                            v = col.iloc[0]
                            earnings_date = v.date() if hasattr(v, "date") else None
                    except Exception:
                        pass

                if earnings_date is None:
                    continue

                days_out = (earnings_date - today).days
                if days_out < 0 or days_out > self.LOOKAHEAD_DAYS:
                    continue

                if days_out == 0:
                    when = "today!"
                elif days_out == 1:
                    when = "TOMORROW"
                else:
                    when = f"in {days_out} days"

                try:
                    name = t.info.get("shortName") or t.info.get("longName") or ticker
                except Exception:
                    name = ticker

                text = f"[{ticker}] Earnings {when} ({earnings_date}) — {name}"
                out.append({
                    "text": text,
                    "source": "earnings_calendar",
                    # Date-scoped URL so each (ticker, date) gets a unique dedup hash.
                    # A bare quote URL would collide across days, silencing the countdown.
                    "url": f"https://finance.yahoo.com/quote/{ticker}?earnings_date={earnings_date}",
                    "reliability": W.SOURCE_RELIABILITY.get("earnings_calendar", 0.88),
                })

                time.sleep(0.2)

            except Exception:
                continue

        return out


class XAITechAdapter:
    """
    Account-Timelines der AI/Tech-Accounts. Verwendet den XGraphQLAdapter aus
    macro-agent wieder, indem dessen Account-Globals (nur in diesem Prozess)
    auf die AI/Tech-Liste aus watchlist.py gesetzt werden.
    """
    def __init__(self):
        m = _m()
        m.X_ACCOUNTS_TIERED = dict(W.X_ACCOUNTS_AITECH)
        m.X_ACCOUNTS = list(W.X_ACCOUNTS_AITECH.keys())
        self._inner = m.XGraphQLAdapter()

    def fetch(self):
        return self._inner.fetch()
