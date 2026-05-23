"""
AI/Tech-spezifische Datenquellen.

Nutzt die HTTP-Helfer (fetch_url/fetch_json) und den XGraphQLAdapter aus
macro-agent wieder, ohne dieses Projekt zu verändern. Konfiguration kommt
ausschließlich aus watchlist.py. Jeder Adapter liefert .fetch() →
list[{text, source, url?, reliability?}].
"""
import hashlib
import html
import json as _json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from . import watchlist as W
from .adapters import _DEFAULT_UA, _load_macro, fetch_json, fetch_url


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


# --- RSS/Atom field extraction -----------------------------------------------

def _rss_text(block: str, tag: str) -> str:
    """Extract text content of <tag> or <tag attr="…">, strip CDATA then HTML tags."""
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL)
    if not m:
        return ""
    raw = m.group(1).replace("<![CDATA[", "").replace("]]>", "")
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _rss_desc(block: str, max_len: int = 150) -> str:
    """Return article description/summary truncated to max_len chars, or empty string."""
    desc = _rss_text(block, "description") or _rss_text(block, "summary") or _rss_text(block, "content")
    if not desc:
        return ""
    return desc[:max_len - 3] + "..." if len(desc) > max_len else desc


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


def _fmt_dollar(v: float) -> str:
    """Format a dollar amount as $2.1M / $271K / $50 for triage readability."""
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _parse_form4_txns(xml: str) -> list[tuple[str, float, float, str, str]]:
    """Return structured Form-4 non-derivative txns: [(code, shares, price, ad, post), ...]."""
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
    return txns


def _summarize_form4(xml: str) -> str:
    """
    Verdichtet eine Form-4-Ownership-XML zu einer bull/bear-lesbaren Zeile:
    Richtung (BUY/SELL/MIXED/routine), Shares, Preis je Code, Holdings danach.
    Gibt '' zurück, wenn keine nonDerivative-Transaktion gefunden wird.
    """
    txns = _parse_form4_txns(xml)
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

    # Prepend total dollar volume for open-market transactions (P=buy, S=sell)
    # so triage can immediately gauge significance without arithmetic.
    om_codes = {"P", "S"} if has_buy or has_sell else set()
    om_vol = sum(agg[c][1] for c in om_codes if c in agg)
    dollar_tag = f" {_fmt_dollar(om_vol)}" if om_vol > 0 else ""

    return f"{signal}{dollar_tag} — " + "; ".join(parts) + holds


def _build_insider_cluster_item(ticker: str, company: str, cluster: dict) -> dict | None:
    """
    Aggregate open-market (P/S) Form-4 transactions over the EDGAR lookback
    window into ONE cluster signal per ticker. Returns a raw_item dict on
    qualifying clusters, else None.

    Bar: ≥2 distinct execs in the same direction OR |net flow| ≥ 0K.
    Single isolated P/S filings below the bar fall through — they were already
    emitted as their own sec_form4 item.

    Verdict labels (most-bullish first): CLUSTER BUY (multi-exec same-side
    buys), STRONG BUY (single exec ≥ 0K), BUY (lone buy crossing dollar
    bar), MIXED (both sides active), SELL/STRONG SELL/CLUSTER SELL mirrored,
    NET BUY/SELL fallbacks when the dollar bar is crossed but the exec-count
    bar is not.
    """
    buy_d = cluster.get("buy_dollars", 0.0)
    sell_d = cluster.get("sell_dollars", 0.0)
    buy_execs = cluster.get("buy_execs", set())
    sell_execs = cluster.get("sell_execs", set())
    n_buy = len(buy_execs)
    n_sell = len(sell_execs)
    net = buy_d - sell_d
    DOLLAR_BAR = 500_000.0
    has_buy = buy_d > 0
    has_sell = sell_d > 0

    if (n_buy < 2 and n_sell < 2 and abs(net) < DOLLAR_BAR):
        return None

    if has_buy and has_sell and (n_buy >= 2 or n_sell >= 2):
        verdict = "MIXED"
    elif n_buy >= 2 and not has_sell:
        verdict = "CLUSTER BUY"
    elif n_sell >= 2 and not has_buy:
        verdict = "CLUSTER SELL"
    elif n_buy >= 2 and n_buy > n_sell:
        verdict = "MIXED (buy-heavy)"
    elif n_sell >= 2 and n_sell > n_buy:
        verdict = "MIXED (sell-heavy)"
    elif net >= DOLLAR_BAR:
        verdict = "NET BUY"
    elif net <= -DOLLAR_BAR:
        verdict = "NET SELL"
    else:
        return None

    parts = []
    if buy_d > 0:
        parts.append(f"buys {_fmt_dollar(buy_d)} across {n_buy} exec{'s' if n_buy != 1 else ''}")
    if sell_d > 0:
        parts.append(f"sells {_fmt_dollar(sell_d)} across {n_sell} exec{'s' if n_sell != 1 else ''}")
    body = "; ".join(parts) if parts else "no open-market activity"
    net_tag = (
        f"net {'+' if net >= 0 else '-'}{_fmt_dollar(abs(net))}"
        if (buy_d > 0 and sell_d > 0)
        else ""
    )
    tail = f" ({net_tag})" if net_tag else ""

    text = (
        f"[INSIDER CLUSTER {verdict}] {ticker} {company}: {body}{tail} "
        f"over last {W.EDGAR_LOOKBACK_DAYS}d"
    )
    return {
        "text": text,
        "source": "insider_cluster",
        "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=4",
        "reliability": W.SOURCE_RELIABILITY.get("insider_cluster", 0.92),
    }


_8K_ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)", re.IGNORECASE)
_HTML_ENTITY_RE = re.compile(r"&#?\w+;")

# SEC Regulation S-K Item-number → investment-relevant short label.
# Only items with material investment implications are listed; unknown items
# fall back to "Material Event" so triage still gets a type signal.
_8K_ITEM_LABELS: dict[str, str] = {
    "1.01": "Material Agreement",
    "1.02": "Agreement Terminated",
    "1.03": "Bankruptcy",
    "2.01": "Acquisition/Disposal",
    "2.02": "Earnings Results",
    "2.03": "Debt Obligation",
    "2.04": "Mining/Oil Trigger",
    "2.05": "Costs Associated with Exit",
    "2.06": "Asset Impairment",
    "3.01": "Exchange Delisting",
    "3.02": "Unregistered Securities",
    "4.01": "Auditor Change",
    "4.02": "Accounting Disagreement",
    "5.01": "Change in Control",
    "5.02": "Exec Departure/Appointment",
    "5.03": "Charter/Bylaws Change",
    "5.07": "Shareholder Vote",
    "5.08": "Failure to Meet Listing Requirements",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Material Event",
    "9.01": "Financial Statements",
}


def _8k_item_label(item_num: str) -> str:
    """Map a dotted Item number (e.g. '2.02') to a triage-friendly label."""
    return _8K_ITEM_LABELS.get(item_num.strip(), "Material Event")


# Per-item-type reliability overrides. All unlisted items inherit the base
# sec_8k reliability from SOURCE_RELIABILITY (currently 0.95). Items that
# are nearly always boilerplate or low-content get a lower score so triage
# deprioritises them relative to high-signal earnings/acquisition 8-Ks.
_8K_ITEM_RELIABILITY: dict[str, float] = {
    "2.01": 0.97,   # Acquisition/Disposal — high-certainty, binary event
    "2.02": 0.97,   # Earnings Results — definitive, market-moving
    "5.01": 0.97,   # Change in Control
    "1.01": 0.96,   # Material Agreement — usually substantive
    "4.02": 0.96,   # Accounting Disagreement — rare, very serious
    "1.03": 0.96,   # Bankruptcy
    "3.01": 0.95,   # Exchange Delisting
    "5.02": 0.94,   # Exec Departure/Appointment — signal, but sometimes routine
    "4.01": 0.93,   # Auditor Change
    "5.07": 0.92,   # Shareholder Vote
    "2.03": 0.91,   # Debt Obligation
    "8.01": 0.90,   # Other Material Event — catch-all, quality varies
    "7.01": 0.85,   # Regulation FD — often a conference slide deck
    "9.01": 0.75,   # Financial Statements / Exhibits — attachment notice, not content
}


def _8k_item_reliability(item_num: str) -> float | None:
    """Return a per-item-type reliability override, or None to use the base value."""
    return _8K_ITEM_RELIABILITY.get(item_num.strip())


# Guidance/Outlook section patterns — forward-looking statements in earnings press releases.
# Triggers on common section headings found in NVDA, MSFT, META, GOOGL, AMZN earnings releases.
_GUIDANCE_SECTION_RE = re.compile(
    # Matches section headings only — must be at a word boundary after whitespace
    # or start of string, and followed by ":" or end of word (section heading).
    r"(?:(?:^|\s)(?:Financial|Business)\s+Outlook[:\s]"
    r"|(?:^|\s)(?:First|Second|Third|Fourth|Q[1-4])\s+(?:Quarter|Fiscal)\s+20\d{2}\s+Outlook[:\s]"
    r"|(?:^|\s)Full[\s-]Year\s+(?:20\d{2}\s+)?(?:Outlook|Guidance)[:\s]"
    r"|(?:^|\s)(?:Outlook|Guidance)\s+for\s+(?:Q[1-4]|(?:First|Second|Third|Fourth)\s+Quarter)"
    r"|(?:^|\s)Forward[\s-]Looking\s+(?:Statements?|Guidance)[:\s]"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_guidance_snippet(plain_txt: str, max_chars: int = 300) -> str:
    """Return the first guidance/outlook paragraph from an already-stripped plain-text 8-K.
    Used to append forward guidance to earnings press release snippets so triage sees
    management's next-quarter revenue/EPS range, not just the headline beat/miss."""
    m = _GUIDANCE_SECTION_RE.search(plain_txt)
    if not m:
        return ""
    # Take text from the section heading forward; stop at the next section or max_chars
    start = m.start()
    chunk = plain_txt[start: start + max_chars + 200]
    # Trim at common next-section boundaries (Item X.XX, another ALL-CAPS heading, Notes)
    trim = re.search(r"\s+(?:Item\s+\d|Notes?\s+to|SIGNATURES|Safe\s+Harbor)", chunk, re.IGNORECASE)
    chunk = chunk[:trim.start()].strip() if trim else chunk[:max_chars].strip()
    return chunk[:max_chars]


def _extract_8k_text(html_src: str, max_chars: int = 400) -> tuple[str, str, str]:
    """
    Extract the first substantive Item paragraph from an 8-K HTML document.
    For earnings releases (item 2.02), also appends a forward-guidance snippet
    when an Outlook/Guidance section is found further in the document.
    Returns (snippet, item_num, item_label). All three are empty strings on
    failure so the caller keeps the fallback notice and base reliability.
    """
    try:
        txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_src)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = _HTML_ENTITY_RE.sub(" ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        m = _8K_ITEM_RE.search(txt)
        if not m:
            return "", "", ""
        item_num = m.group(1)
        snippet = txt[m.start(): m.start() + max_chars].strip()
        label = _8k_item_label(item_num)
        # For earnings releases, append forward guidance if present deeper in the document
        if item_num == "2.02":
            guidance = _extract_guidance_snippet(txt)
            if guidance:
                snippet = snippet + " [OUTLOOK: " + guidance + "]"
        return snippet, item_num, label
    except Exception:
        return "", "", ""


# 6-K: press-release dateline pattern — "[CITY, Country/State, Month Day, YYYY]"
# Marks the start of the actual news content after the SEC header boilerplate.
# Examples: "HSINCHU, Taiwan, R.O.C., May 15, 2026"  /  "VELDHOVEN, the Netherlands, Apr 23, 2026"
_6K_DATELINE_RE = re.compile(
    r"[A-Z][A-Z\s]+,\s+(?:[A-Za-z\s\.]+,\s+)?"  # CITY, Country[, State]
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)
# Also match "[City] - [Month Day, Year] -" format used by some press releases
_6K_RELEASE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}\s*[-–—]",
    re.IGNORECASE,
)


def _extract_6k_text(html_src: str, max_chars: int = 400) -> str:
    """
    Extract the press-release content from a 6-K HTML filing.
    Skips the SEC header boilerplate (Form 6-K, Exchange Act references,
    address, SIGNATURES block) and returns the first substantive paragraph.
    Returns empty string when no press release is embedded (exhibit-based 6-Ks).
    """
    try:
        txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_src)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = _HTML_ENTITY_RE.sub(" ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        # Skip past the SIGNATURES block (end of SEC header boilerplate).
        # Press release content follows the CFO/VP signature line.
        sig_m = re.search(
            r"(?:Senior Vice President|Chief Financial Officer|Chief Executive Officer|"
            r"President|Secretary|Director)[^.]{0,80}\n?",
            txt, re.IGNORECASE,
        )
        search_start = sig_m.end() if sig_m else 0
        # Find the dateline of the embedded press release
        dl_m = _6K_DATELINE_RE.search(txt, search_start)
        if dl_m:
            snippet = txt[dl_m.start(): dl_m.start() + max_chars].strip()
            guidance = _extract_guidance_snippet(txt[dl_m.start():])
            return (snippet + " [OUTLOOK: " + guidance + "]") if guidance else snippet
        # Fallback: date-release format "Month Day, YYYY —"
        rl_m = _6K_RELEASE_RE.search(txt, search_start)
        if rl_m:
            snippet = txt[rl_m.start(): rl_m.start() + max_chars].strip()
            guidance = _extract_guidance_snippet(txt[rl_m.start():])
            return (snippet + " [OUTLOOK: " + guidance + "]") if guidance else snippet
        # Exhibit-99.1 fallback: extract the press release title from the Exhibits table.
        # Use findall and take the last match to skip "EXHIBIT 99.1 TO THIS REPORT ON FORM 6-K
        # IS INCORPORATED BY REFERENCE" inline references that appear earlier in the document.
        ex_matches = list(re.finditer(
            r"(?:Exhibit[s]?\s+)?99\.1\s+(?!TO\s+THIS\s+REPORT)([^\n]{20,200})",
            txt, re.IGNORECASE,
        ))
        if ex_matches:
            ex_text = ex_matches[-1].group(1).strip()
            # Trim at common trailer keywords that follow the exhibit description
            ex_text = re.sub(r"\s+(?:SIGNATURES|Pursuant\s+to|EXHIBIT\s+\d|99\.\d)\b.*", "", ex_text).strip()
            if ex_text:
                return ex_text[:max_chars]
        # Final fallback: if text after signatures is long enough, it has embedded content
        tail = txt[search_start:].strip() if search_start else ""
        if len(tail) > 200:
            return tail[:max_chars]
        return ""
    except Exception:
        return ""


_10Q_SECTION_RE = re.compile(
    r"Item\s+2[\.\s]+(?:Management(?:'s|s)?\s+Discussion|Results?\s+of\s+Operations?)",
    re.IGNORECASE,
)
_10K_SECTION_RE = re.compile(
    r"Item\s+7[\.\s]+(?:Management(?:'s|s)?\s+Discussion|Results?\s+of\s+Operations?)",
    re.IGNORECASE,
)


def _extract_10q_text(html_src: str, is_10k: bool = False, max_chars: int = 400) -> str:
    """
    Extract the MD&A section (Item 2 for 10-Q, Item 7 for 10-K) from a periodic
    SEC filing. Returns a non-empty snippet on success, empty string on failure.
    Falls back to first revenue/income-bearing sentence if Item header not found.
    """
    try:
        txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_src)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = _HTML_ENTITY_RE.sub(" ", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        pat = _10K_SECTION_RE if is_10k else _10Q_SECTION_RE
        m = pat.search(txt)
        if m:
            return txt[m.start(): m.start() + max_chars].strip()
        # Fallback: find first mention of revenue/income with a numeric figure
        rev_m = re.search(
            r"(?:revenue|net\s+income|earnings\s+per\s+share|EPS).{0,80}\$[\d,]+",
            txt, re.IGNORECASE,
        )
        if rev_m:
            return txt[rev_m.start(): rev_m.start() + max_chars].strip()
        return ""
    except Exception:
        return ""


def _edgar_form_meta(form):
    """Map a SEC form type to (source_key, human label) for normalization."""
    if form == "4":
        return "sec_form4", "Insider Form 4"
    if form.startswith("SC 13D"):
        return "sec_13dg", ("Aktivisten-Stake 13D/A" if form.endswith("/A")
                            else "Aktivisten-Stake 13D")
    if form.startswith("SC 13G"):
        return "sec_13dg", ("Passive >5%-Beteiligung 13G/A" if form.endswith("/A")
                            else "Passive >5%-Beteiligung 13G")
    if form == "10-Q":
        return "sec_10q", "10-Q Quarterly Report"
    if form == "10-K":
        return "sec_10k", "10-K Annual Report"
    if form in ("6-K", "6-K/A"):
        suffix = "/A (Amended)" if form.endswith("/A") else ""
        return "sec_6k", f"6-K Foreign Issuer Report{suffix}"
    if form == "20-F":
        return "sec_20f", "20-F Foreign Annual Report"
    return "sec_8k", "8-K"


class EDGARAdapter:
    """SEC-Pflichtmeldungen: 8-K (Material Events), Form 4 (Insider-Trades),
    SC 13D/G (>5%-Beteiligungen / Aktivisten-Stakes) je Ticker."""
    TICKERS_MAP = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

    def __init__(self):
        self._cik = None  # {TICKER: (cik:int, title)}

    def _load_cik_map(self):
        data = fetch_json(self.TICKERS_MAP, headers=UA, timeout=20)
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
            data = fetch_json(self.SUBMISSIONS.format(cik=cik), headers=UA, timeout=20)
            time.sleep(0.2)  # SEC: max 10 req/s
            if not data:
                continue
            # Per-ticker insider-cluster accumulator. Sums open-market (P/S)
            # dollar volume across all Form-4 filings within EDGAR_LOOKBACK_DAYS
            # so triage sees ONE aggregated cluster signal per ticker instead of
            # 5-10 separate single-filing items lost in the noise. Academic
            # cluster-buying signal (Cohen-Malloy-Pomorski) is among the strongest
            # insider-activity factors.
            cluster: dict = {
                "buy_dollars": 0.0,
                "sell_dollars": 0.0,
                "buy_execs": set(),
                "sell_execs": set(),
                "filings": 0,
            }
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
                src, label = _edgar_form_meta(form)
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
                        xml = fetch_url(raw_url, headers=UA, timeout=20)
                        time.sleep(0.15)  # SEC: max 10 req/s
                        if xml:
                            summary = _summarize_form4(xml)
                            owner_m = _F4_OWNER_RE.search(xml)
                            owner = html.unescape(owner_m.group(1).strip()) if owner_m else ""
                            if summary:
                                role = _f4_role(xml)
                                who = owner + (f" ({role})" if role else "")
                                detail = f"{who} — {summary}" if who else summary
                            # Feed the cluster accumulator with open-market txns
                            # (P=buy, S=sell). Routine codes (A/M/F/G/C/X) are
                            # comp/exercise/tax and explicitly excluded — the
                            # cluster signal is about discretionary trading.
                            for code, shares, price, _ad, _post in _parse_form4_txns(xml):
                                if code not in ("P", "S") or shares <= 0 or price <= 0:
                                    continue
                                dvol = shares * price
                                if code == "P":
                                    cluster["buy_dollars"] += dvol
                                    if owner:
                                        cluster["buy_execs"].add(owner)
                                else:
                                    cluster["sell_dollars"] += dvol
                                    if owner:
                                        cluster["sell_execs"].add(owner)
                                cluster["filings"] += 1
                    except Exception:
                        pass
                item_num = ""
                item_type = ""
                if form == "8-K" and acc and doc:
                    # Fetch the primary 8-K HTML, extract the first Item paragraph,
                    # classify the Item type, and determine a per-item reliability
                    # override so triage ranks earnings/acquisition 8-Ks above
                    # boilerplate exhibit attachments (Item 9.01) or Reg-FD slides.
                    # Falls back to the metadata description on any fetch/parse error.
                    try:
                        html_src = fetch_url(doc_url, headers=UA, timeout=20)
                        time.sleep(0.15)  # SEC: max 10 req/s
                        if html_src:
                            snippet, item_num, item_type = _extract_8k_text(html_src)
                            if snippet:
                                detail = snippet
                    except Exception:
                        pass
                if form in ("10-Q", "10-K", "20-F") and acc and doc:
                    # Extract MD&A / Results-of-Operations section (Item 2 for 10-Q,
                    # Item 7 for 10-K/20-F) so triage sees revenue/guidance context.
                    # Falls back to metadata desc on any error.
                    try:
                        html_src = fetch_url(doc_url, headers=UA, timeout=30)
                        time.sleep(0.15)  # SEC: max 10 req/s
                        if html_src:
                            snippet = _extract_10q_text(
                                html_src, is_10k=(form in ("10-K", "20-F"))
                            )
                            if snippet:
                                detail = snippet
                    except Exception:
                        pass
                if form in ("6-K", "6-K/A") and acc and doc:
                    # 6-K filings (TSM, ASML, ARM) embed press releases after the SEC
                    # header boilerplate. _extract_6k_text skips the header and finds
                    # the dateline/content. For exhibit-only 6-Ks it returns "" and the
                    # filing notice (metadata) remains as the detail text.
                    try:
                        html_src = fetch_url(doc_url, headers=UA, timeout=20)
                        time.sleep(0.15)  # SEC: max 10 req/s
                        if html_src:
                            snippet = _extract_6k_text(html_src)
                            if snippet:
                                detail = snippet
                    except Exception:
                        pass
                # Include the 8-K item type in the label so triage can distinguish
                # "8-K:Earnings Results" from "8-K:Acquisition/Disposal" at a glance.
                display_label = f"{label}:{item_type}" if item_type else label
                # Apply per-item-type reliability override when available; fall back
                # to the source-level default (sec_8k=0.95, form4=0.90, etc.).
                base_rel = W.SOURCE_RELIABILITY.get(src)
                rel_override = _8k_item_reliability(item_num) if item_num else None
                reliability = rel_override if rel_override is not None else base_rel
                out.append({
                    # Accession-Nr. im Text → jede Einreichung distinkt (Dedup-Hash)
                    "text": f"[EDGAR {display_label}] {tk} {title}: {detail} (filed {date_l[i]}, {acc_disp})",
                    "source": src,
                    "url": doc_url,
                    "reliability": reliability,
                })
            # Per-ticker insider-cluster emit: turn the scattered Form-4 stream
            # over the lookback window into one aggregated, dollar-weighted
            # cluster verdict. Bar: ≥2 distinct execs in the same direction OR
            # |net flow| ≥ 0K. Single-filing-only clusters fall back to the
            # individual Form-4 items already emitted above.
            cluster_item = _build_insider_cluster_item(tk, title, cluster)
            if cluster_item:
                out.append(cluster_item)
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
            xml = fetch_url(url, headers=self.HEADERS, timeout=20)
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


class SECBroadEventsAdapter:
    """
    Off-watchlist 8-K material events — discovers AI/Tech events from companies
    NOT on our 26-ticker watchlist using the same SEC atom-feed as
    SECRegistrationsAdapter (which only covers S-1/F-1 IPO filings).

    Gap closed: an AI startup acquisition, major partnership, change of control,
    or CEO departure at a notable private player (e.g. xAI, Cohere, Scale AI)
    that files an 8-K is completely invisible to EDGARAdapter (watchlist-only).
    This adapter pulls the SEC current-8-K atom feed and passes AI/Tech company
    names through the same _is_aitech() + NOTABLE_PRIVATE_PLAYERS filter.

    Limits count=80 (broad sweep) and skips companies already on TICKERS to
    avoid duplicating EDGARAdapter items (which are richer: include text + type).
    """
    BASE = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type=8-K&output=atom&count=80")
    HEADERS = {"User-Agent": "HedgingAlphaFund research philipp.baro@gmail.com"}

    _ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
    _TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
    _HREF_RE = re.compile(r'<link[^>]*href="([^"]+)"')
    _ACC_RE = re.compile(r"accession-number=([0-9-]+)")
    _FILED_RE = re.compile(r"Filed:.*?(\d{4}-\d{2}-\d{2})")

    # Company-name fragments (lowercase) for the watchlist tickers, derived from
    # the single source of truth in watchlist.py (W.WATCHLIST_NAME_FRAGMENTS).
    # SEC atom feed uses full legal names; ticker-symbol matching misses them.
    # Keeping this derived (not a hardcoded copy) means a newly added ticker
    # cannot silently leak its 8-K into this off-watchlist sweep as a duplicate
    # of the richer EDGARAdapter item — the sync is enforced by test_watchlist_sync.
    _WATCHLIST_NAMES = frozenset(W.WATCHLIST_NAME_FRAGMENTS.values())

    def _is_aitech(self, name: str) -> bool:
        low = f" {name.lower()} "
        if any(p in low for p in W.NOTABLE_PRIVATE_PLAYERS):
            return True
        return any(k in low for k in W.AITECH_KEYWORDS)

    def _is_watchlist(self, name: str) -> bool:
        """Skip companies already covered by EDGARAdapter (richer data)."""
        low = name.lower()
        return any(frag in low for frag in self._WATCHLIST_NAMES)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        xml = fetch_url(self.BASE, headers=self.HEADERS, timeout=20)
        time.sleep(0.3)
        if not xml:
            return []
        for block in self._ENTRY_RE.findall(xml):
            t = self._TITLE_RE.search(block)
            if not t:
                continue
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            acc_m = self._ACC_RE.search(block)
            acc = acc_m.group(1) if acc_m else ""
            if not acc or acc in seen:
                continue
            # Title format: "8-K - Company Name (0001234567) (Filer)"
            company = title.split(" - ", 1)[1] if " - " in title else title
            company = re.sub(r"\s*\(\d{6,}\)\s*\(.*?\)\s*$", "", company).strip()
            if not company:
                continue
            if not self._is_aitech(company):
                continue
            if self._is_watchlist(company):
                continue  # EDGARAdapter already provides richer item for these
            seen.add(acc)
            href = self._HREF_RE.search(block)
            filed = self._FILED_RE.search(block)
            filed_s = filed.group(1) if filed else ""
            out.append({
                "text": (f"[SEC 8-K · off-watchlist AI/TECH] {company} — Material Event "
                         f"(filed {filed_s}, {acc})"),
                "source": "sec_broad_event",
                "url": href.group(1).strip() if href else None,
                "reliability": W.SOURCE_RELIABILITY.get("sec_broad_event", 0.88),
            })
        return out


class ArxivAdapter:
    """Neueste AI/ML-Paper (Forschungsfront) via arXiv-API."""
    def fetch(self):
        q = "+OR+".join(f"cat:{c}" for c in W.ARXIV_CATEGORIES)
        url = (f"http://export.arxiv.org/api/query?search_query={q}"
               f"&start=0&max_results={W.ARXIV_MAX}"
               f"&sortBy=submittedDate&sortOrder=descending")
        xml = fetch_url(url, timeout=20)
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
            data = fetch_json(url, timeout=15)
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
            data = fetch_json(url, headers=headers, timeout=15)
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
    """
    Kuratierte AI/Tech-News-RSS/Atom-Feeds (failsafe je Feed).

    Lookback = RSS_LOOKBACK_DAYS (default 3) filtert veraltete Artikel: viele
    Tech-Feeds liefern Items, die Wochen zurückreichen — ohne Cutoff würden die
    jeden 30-Min-Lauf als "frisch" eingelesen und belegen Triage-Slots. Items
    ohne parsbares Datum werden behalten (Coverage > Präzision), identisch zu
    FundingNewsAdapter.
    """
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)

    def fetch(self):
        m = _m()
        out = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        for name, feed in W.TECH_RSS_FEEDS.items():
            try:
                text = fetch_url(feed, timeout=15)
                if not text:
                    continue
                sep = "<item>" if "<item>" in text else "<entry>"
                for block in text.split(sep)[1:9]:  # 8 items/feed
                    title = _rss_text(block, "title")
                    if not title:
                        continue
                    date_m = (re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
                              or re.search(r"<updated>(.*?)</updated>", block, re.DOTALL)
                              or re.search(r"<published>(.*?)</published>", block, re.DOTALL))
                    if date_m:
                        pub = _parse_rss_date(date_m.group(1).strip())
                        if pub and pub < cutoff:
                            continue
                    desc = _rss_desc(block)
                    item_text = f"[{name}] {title}"
                    if desc:
                        item_text = f"{item_text} — {desc}"
                    link = (re.search(r'<link[^>]*href="([^"]+)"', block)
                            or re.search(r"<link>(.*?)</link>", block))
                    out.append({
                        "text": item_text[:400],
                        "source": "tech_news",
                        "url": link.group(1).strip() if link else None,
                        "reliability": W.SOURCE_RELIABILITY["tech_news"],
                    })
            except Exception:
                continue
        return out



# Keywords that make a funding/VC article relevant to this AI/Tech fund.
# Match is case-insensitive against title + description combined.
# Intentionally broad (coverage > precision): any AI, cloud, semiconductor,
# infrastructure, or energy-for-AI signal passes; pure consumer/lifestyle/bio
# items that match none of these terms are dropped.
_FUNDING_RELEVANCE_RE = re.compile(
    r"\b("
    # AI / ML core
    r"artificial intelligence|machine learning|deep learning|neural network|"
    r"\bAI\b|AI[-\s]powered|AI[-\s]native|GenAI|generative AI|"
    r"large language model|LLM|foundation model|GPT|transformer|"
    # Cloud / infrastructure / enterprise
    r"cloud|data center|data centre|infrastructure|enterprise software|"
    r"developer platform|API platform|developer tool|devtool|"
    r"SaaS|PaaS|IaaS|edge computing|distributed system|"
    # Semiconductors / hardware
    r"chip|GPU|semiconductor|ASIC|accelerator|silicon|wafer|fab|HBM|"
    r"processing unit|inference chip|training chip|"
    # Energy / power (S5 thesis)
    r"nuclear|power grid|data center power|energy storage|clean energy|"
    r"hyperscaler|compute|watt|megawatt|gigawatt|"
    # Robotics / automation
    r"robotics|autonomous|humanoid robot|automation|"
    # Cybersecurity
    r"cybersecurity|cyber security|endpoint security|SIEM|SOC\b|XDR|"
    # Big-number funding (≥\$100M rounds) — large rounds are more investable
    r"\$[1-9]\d{2}[MB]|\$\d+\.\d+[BMb]|\$[1-9]\d*B\b|"
    # Watchlist company names (direct mentions)
    r"NVIDIA|OpenAI|Anthropic|Google DeepMind|Microsoft|Amazon|Meta\b|Apple|"
    r"Palantir|Oracle|ServiceNow|Salesforce|Snowflake|CrowdStrike|Adobe|"
    r"TSMC|ASML|Broadcom|AMD|Qualcomm|Marvell|Arista|Vertiv|"
    r"Vistra|Constellation Energy|GE Vernova|Eaton"
    r")",
    re.IGNORECASE,
)


class FundingNewsAdapter:
    """
    Dedizierter Funding/VC/IPO/Launch-Feed (RSS, kein API-Key). Schließt die
    Lücke, die den Exa-$250M-Miss (HED-24) verursachte: die generalistischen
    TechRSS-/NewsAPI-Adapter lassen runden-/launch-spezifische Meldungen
    durchfallen. Quellen: TechCrunch Startups + Funding, VentureBeat.

    - Relevance filter: items must match _FUNDING_RELEVANCE_RE (AI/Tech/Semis/
      cloud/energy keywords or big-round dollar amounts). Purely off-topic
      consumer/lifestyle items are dropped to reduce triage noise.
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
                text = fetch_url(feed, timeout=15)
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
                    desc = _rss_desc(block)
                    # Drop items with no AI/Tech relevance signal — reduces noise
                    # in raw_items so triage doesn't waste tokens on consumer/
                    # lifestyle rounds (fragrance tech, beauty booking, etc.).
                    if not _FUNDING_RELEVANCE_RE.search(title + " " + (desc or "")):
                        continue
                    item_text = f"[Funding · {name}] {title}"
                    if desc:
                        item_text = f"{item_text} — {desc}"
                    out.append({
                        "text": item_text[:400],
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
                text = fetch_url(feed, timeout=15)
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
                    desc = _rss_desc(block)
                    item_text = f"[Energy · {name}] {title}"
                    if desc:
                        item_text = f"{item_text} — {desc}"
                    out.append({
                        "text": item_text[:400],
                        "source": "energy_news",
                        "url": url,
                        "reliability": W.SOURCE_RELIABILITY.get("energy_news", 0.72),
                    })
            except Exception:
                continue
        return out


class PressWireAdapter:
    """
    Official company press releases from BusinessWire and GlobeNewswire.
    Press wires carry earnings releases, product launches, partnership
    announcements, and guidance updates hours before editorial coverage
    and before the 8-K lands on SEC EDGAR.

    Filters to AI/tech-relevant items using AITECH_KEYWORDS + NOTABLE_PRIVATE_PLAYERS
    so broad sector feeds don't flood triage with irrelevant PRs.
    Dedup by URL across feeds. Lookback = RSS_LOOKBACK_DAYS (default 3).
    Per-feed try/except — one dead wire never kills the adapter.
    """
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)
    # Lowercase keyword set for fast title+desc matching
    _AITECH_KW = frozenset(kw.lower() for kw in W.AITECH_KEYWORDS)
    _NOTABLE_KW = frozenset(n.lower() for n in W.NOTABLE_PRIVATE_PLAYERS)

    def _is_aitech(self, title: str, desc: str) -> bool:
        low = (title + " " + desc).lower()
        if any(kw in low for kw in self._AITECH_KW):
            return True
        if any(n in low for n in self._NOTABLE_KW):
            return True
        return False

    def fetch(self):
        m = _m()
        out, seen = [], set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        feeds = getattr(W, "PRESS_WIRE_RSS_FEEDS", {})
        for name, feed in feeds.items():
            try:
                text = fetch_url(feed, timeout=20)
                if not text:
                    continue
                sep = "<item>" if "<item>" in text else "<entry>"
                for block in text.split(sep)[1:40]:
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
                    desc = _rss_desc(block, max_len=200)
                    if not self._is_aitech(title, desc or ""):
                        continue
                    item_text = f"[PressWire · {name}] {title}"
                    if desc:
                        item_text = f"{item_text} — {desc}"
                    out.append({
                        "text": item_text[:450],
                        "source": "press_wire",
                        "url": url,
                        "reliability": W.SOURCE_RELIABILITY.get("press_wire", 0.78),
                    })
            except Exception:
                continue
        return out


class MacroFedAdapter:
    """
    Federal Reserve macro context feed — monetary policy press releases and
    Fed chair/governor speeches. Provides macro financing conditions that
    directly affect AI capex thesis: rate changes alter data-center financing
    costs and hyperscaler capex timelines. Previously zero macro data in the
    pipeline. Two feeds (both official, no auth, SEC-compatible UA):
    - press_monetary: FOMC rate decisions, policy statements, balance-sheet ops
    - press_speeches: Fed chair and governor speeches (forward guidance, risk signals)
    Lookback = RSS_LOOKBACK_DAYS (default 3 days); per-feed try/except isolation.
    """
    FEEDS = {
        "fomc": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "fed_speeches": "https://www.federalreserve.gov/feeds/press_speeches.xml",
    }
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        for name, feed_url in self.FEEDS.items():
            try:
                text = fetch_url(feed_url, timeout=15)
                if not text:
                    continue
                sep = "<item>" if "<item>" in text else "<entry>"
                for block in text.split(sep)[1:15]:
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
                              or re.search(r"<dc:date>(.*?)</dc:date>", block, re.DOTALL)
                              or re.search(r"<updated>(.*?)</updated>", block, re.DOTALL))
                    if date_m:
                        pub = _parse_rss_date(date_m.group(1).strip())
                        if pub and pub < cutoff:
                            continue
                    desc = _rss_desc(block)
                    item_text = f"[Fed · {name}] {title}"
                    if desc:
                        item_text = f"{item_text} — {desc}"
                    out.append({
                        "text": item_text[:350],
                        "source": "fed_macro",
                        "url": url,
                        "reliability": W.SOURCE_RELIABILITY.get("fed_macro", 0.90),
                    })
            except Exception:
                continue
        return out


class MacroBLSAdapter:
    """
    Bureau of Labor Statistics (BLS) economic releases — CPI, PPI, jobs reports,
    and productivity data. These are the primary economic data points that drive
    Fed policy decisions and thus AI capex cycles: hot CPI keeps rates high,
    which tightens hyperscaler financing and slows data-center build-out.
    Previously triage saw Fed signals (Zyklus 23) but not the underlying data
    that forces Fed action.

    Single official BLS RSS feed (no auth, public):
    - bls_latest.rss: all major BLS press releases (CPI, PPI, JOLTS, payrolls, GDP)

    Lookback = RSS_LOOKBACK_DAYS (3 days). Per-feed try/except isolation.
    Source key bls_macro, reliability=0.92 (official government statistics).
    """
    FEEDS = {
        "bls_releases": "https://www.bls.gov/feed/bls_latest.rss",
    }
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        for name, feed_url in self.FEEDS.items():
            try:
                text = fetch_url(feed_url, timeout=15)
                if not text:
                    continue
                sep = "<item>" if "<item>" in text else "<entry>"
                for block in text.split(sep)[1:15]:
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
                              or re.search(r"<dc:date>(.*?)</dc:date>", block, re.DOTALL)
                              or re.search(r"<updated>(.*?)</updated>", block, re.DOTALL))
                    if date_m:
                        pub = _parse_rss_date(date_m.group(1).strip())
                        if pub and pub < cutoff:
                            continue
                    desc = _rss_desc(block)
                    item_text = f"[BLS · macro] {title}"
                    if desc:
                        item_text = f"{item_text} — {desc}"
                    out.append({
                        "text": item_text[:350],
                        "source": "bls_macro",
                        "url": url,
                        "reliability": W.SOURCE_RELIABILITY.get("bls_macro", 0.92),
                    })
            except Exception:
                continue
        return out


# Regex to detect actual earnings results (beats/misses/inline) in Yahoo Finance
# headlines. These are confirmed factual outcomes (not forecasts), so reliability
# is set higher than analyst actions. Source="earnings_result".
_EARNINGS_RESULT_RE = re.compile(
    r"(?:"
    # beat/miss/top/exceed against eps, revenue, estimates etc.
    r"\b(?:beats?|misses?|tops?|exceeded?|surpassed?|fell\s+short\s+of)\s+"
    r"(?:eps|earnings|revenue|estimates?|expectations?|consensus)\b|"
    # eps/earnings/revenue result verbs
    r"\b(?:eps|earnings|revenue)\s+(?:beat|miss|in[- ]line|top|exceed|fell\s+short|surpass)\b|"
    # "reports quarterly earnings/results" or "reports Q1-4 earnings/results"
    r"\breports?\s+(?:quarterly\s+|q[1-4]\s+)?(?:earnings|results|eps|revenue)\b|"
    # "Q2 EPS:" or "Q1 EPS " — the quarter+eps combo signals an earnings headline
    r"\bq[1-4]\s+eps\b|"
    # quarterly/q[1-4] earnings/results + outcome verb
    r"\b(?:quarterly|q[1-4])\s+(?:earnings|results)\s+(?:beat|miss|top|exceed|disappoint|surpass)\b|"
    # posts record/quarterly revenue/earnings
    r"\bposts?\s+(?:record|quarterly)\s+(?:revenue|earnings|profit|loss)\b|"
    # revenue/profit/loss rises/falls/jumps X% — factual result framing
    r"\b(?:profit|loss|revenue)\s+(?:rises?|falls?|jumps?|drops?|soars?|slumps?)\s+\d"
    r")",
    re.IGNORECASE,
)

# Regex to detect analyst rating/price-target actions in Yahoo Finance headlines.
# Matched items get source="analyst_action" and higher reliability than generic news.
_ANALYST_ACTION_RE = re.compile(
    r"\b(upgrades?|downgrades?|raises?\s+price\s+target|lowers?\s+price\s+target|"
    r"cuts?\s+price\s+target|initiates?\s+coverage|reinitiates?|resumes?\s+coverage|"
    r"reiterates?|maintains?\s+(buy|sell|hold|neutral|outperform|underperform)|"
    r"price\s+target\s+(raised|lowered|cut|increased|decreased|lifted)|"
    r"\b(outperform|underperform|overweight|underweight|buy|sell|hold|neutral)\s+(rating|to))\b",
    re.IGNORECASE,
)


class YahooFinanceTickerAdapter:
    """
    Yahoo Finance per-ticker RSS headlines for top watchlist positions.
    Carries market-moving ticker news (earnings, analyst upgrades/downgrades,
    product announcements) that tech-blog adapters routinely miss.
    One request per ticker with polite 0.3s sleep. Dedup by URL across tickers.
    Lookback = RSS_LOOKBACK_DAYS (default 3) to filter stale articles.

    Analyst-action detection: headlines matching upgrade/downgrade/price-target
    language are re-tagged as source="analyst_action" (reliability=0.85) so
    triage can immediately distinguish them from generic financial news.
    """
    LOOKBACK_DAYS = getattr(W, "RSS_LOOKBACK_DAYS", 3)

    def fetch(self):
        m = _m()
        out, seen = [], set()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)
        for ticker in W.YAHOO_FINANCE_TICKERS:
            try:
                url = W.YAHOO_FINANCE_RSS.format(ticker=ticker)
                text = fetch_url(url, timeout=15)
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
                    desc = _rss_desc(block, max_len=200)
                    # Priority order: earnings result > analyst action > generic.
                    # Earnings results are confirmed factual data (highest signal);
                    # analyst actions are structured investment signals (medium-high);
                    # generic yahoo_finance items are general financial news (baseline).
                    is_earnings = bool(_EARNINGS_RESULT_RE.search(title))
                    is_analyst = not is_earnings and bool(_ANALYST_ACTION_RE.search(title))
                    if is_earnings:
                        src = "earnings_result"
                        rel = W.SOURCE_RELIABILITY.get("earnings_result", 0.88)
                        item_text = f"[Earnings · {ticker}] {title}"
                    elif is_analyst:
                        src = "analyst_action"
                        rel = W.SOURCE_RELIABILITY.get("analyst_action", 0.85)
                        item_text = f"[Analyst · {ticker}] {title}"
                    else:
                        src = "yahoo_finance"
                        rel = W.SOURCE_RELIABILITY.get("yahoo_finance", 0.72)
                        item_text = f"[{ticker}] {title}"
                    if desc:
                        item_text = f"{item_text} — {desc}"
                    out.append({
                        "text": item_text[:450],
                        "source": src,
                        "url": item_url,
                        "reliability": rel,
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
        data = fetch_json(url, timeout=15)
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

                # Pull consensus EPS and revenue estimates from the same calendar dict.
                # Both fields are present in yfinance ≥0.2 when estimates are available.
                est_parts: list[str] = []
                if isinstance(cal, dict):
                    eps_avg = cal.get("Earnings Average")
                    rev_avg = cal.get("Revenue Average")
                    if eps_avg is not None:
                        try:
                            eps_val = float(eps_avg)
                            est_parts.append(f"est. EPS ${eps_val:.2f}")
                        except (TypeError, ValueError):
                            pass
                    if rev_avg is not None:
                        try:
                            rev_val = float(rev_avg)
                            if rev_val >= 1e9:
                                est_parts.append(f"rev ${rev_val / 1e9:.1f}B")
                            elif rev_val >= 1e6:
                                est_parts.append(f"rev ${rev_val / 1e6:.0f}M")
                        except (TypeError, ValueError):
                            pass

                est_suffix = "; " + ", ".join(est_parts) if est_parts else ""
                text = f"[{ticker}] Earnings {when} ({earnings_date}) — {name}{est_suffix}"
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


class FREDMacroAdapter:
    """
    FRED-Makro über den schlüssellosen fredgraph.csv-Endpunkt — kein API-Key nötig,
    daher robust gegen ein fehlendes FRED_API_KEY (der alte macro-agent-FREDAdapter
    gab bei leerem Key still [] zurück → komplett leeres Makro-Fenster, vgl. HED-92).

    Liefert je Serie die jüngste Beobachtung plus Δ vs. vorherige. Die Kern-Serien
    (Initial/Continued Jobless Claims, Effective Fed Funds Rate) sichern eine
    Mindest-Makro-Abdeckung an Veröffentlichungstagen (Do = Erstanträge); jeder neue
    Print erzeugt durch das Datum im Text einen neuen content_hash → eigene raw_items-
    Zeile im 24h-Fenster. Ergänzt MacroFed/MacroBLS (RSS): die liefern Fed-Statements
    und BLS-Releases, aber nicht die wöchentlichen Erstanträge (DOL/ETA) oder
    quantitative Zins-/Spread-Niveaus. Tech ist zinssensitiv → Rates/Liquidität-Overlay.
    """
    BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
    MAX_AGE_DAYS = 14  # Tages-/Wochenserien; Continued Claims lagen ~1 Woche hinter Initial

    # series_id -> (Anzeigename, "value"|"pct"|"pp"|"count"|"index")
    # "index" = display as integer with comma-separator (equity/equity-volatility levels)
    SERIES = {
        "ICSA":         ("Initial Jobless Claims", "count"),
        "CCSA":         ("Continued Jobless Claims", "count"),
        "DFF":          ("Effective Fed Funds Rate", "pct"),
        "DGS10":        ("10Y Treasury Yield", "pct"),
        "T10Y2Y":       ("10Y-2Y Yield Spread", "pp"),
        "BAMLH0A0HYM2": ("US HY OAS Spread", "pp"),
        "DTWEXBGS":     ("Trade-Weighted USD", "value"),
        # Market risk-regime / equity-context overlay (daily, key AI/Tech signals)
        "VIXCLS":       ("CBOE VIX (Market Volatility)", "value"),
        "SP500":        ("S&P 500", "index"),
        "NASDAQCOM":    ("NASDAQ Composite", "index"),
    }

    @staticmethod
    def _fmt(kind: str, v: float) -> str:
        if kind == "count":
            return f"{v:,.0f}"
        if kind == "pct":
            return f"{v:.2f}%"
        if kind == "pp":
            return f"{v:+.2f}pp"
        if kind == "index":
            return f"{v:,.2f}"
        return f"{v:.2f}"

    @staticmethod
    def _parse_csv(text: str):
        """fredgraph.csv → [(date_str, float)] für numerische Beobachtungen ('.' = fehlend)."""
        out = []
        for line in text.strip().splitlines()[1:]:  # Header 'observation_date,SID' überspringen
            parts = line.split(",")
            if len(parts) < 2:
                continue
            val = parts[1].strip()
            if not val or val == ".":
                continue
            try:
                out.append((parts[0].strip(), float(val)))
            except ValueError:
                continue
        return out

    def fetch(self):
        m = _m()
        out = []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.MAX_AGE_DAYS)).date()
        for sid, (name, kind) in self.SERIES.items():
            try:
                text = fetch_url(self.BASE.format(sid=sid), headers=UA, timeout=15)
                if not text:
                    continue
                obs = self._parse_csv(text)
                if not obs:
                    continue
                date_str, latest = obs[-1]
                try:
                    if datetime.fromisoformat(date_str).date() < cutoff:
                        continue  # veralteter Wert (kein frischer Print) → nicht surfacen
                except ValueError:
                    pass
                delta = ""
                if len(obs) >= 2:
                    prev = obs[-2][1]
                    if prev != 0:
                        delta = f" (Δ {(latest - prev) / abs(prev):+.1%} vs. {self._fmt(kind, prev)})"
                out.append({
                    "text": f"FRED: {name} ({sid}) = {self._fmt(kind, latest)} "
                            f"[{date_str}]{delta}",
                    "source": "fred_macro",
                    "url": f"https://fred.stlouisfed.org/series/{sid}",
                    "reliability": W.SOURCE_RELIABILITY.get("fred_macro", 0.95),
                })
                time.sleep(0.2)
            except Exception:
                continue
        return out


class ShortInterestAdapter:
    """
    Short-interest data for watchlist tickers via Yahoo Finance quoteSummary API.

    Emits items only when short interest is notable (>3% of float) or has moved
    significantly vs prior month (>20% change in shares short). This surfaces
    squeeze-setup conditions and institutional de-risking signals that don't
    appear in news feeds.

    Source: 'yahoo_short_interest'. Updated bi-weekly on Yahoo (settlement-date
    lag), so daily runs will re-ingest the same data — dedup via content_hash
    keyed on ticker + sharesShort value keeps raw_items clean.

    Reliability 0.75: derived from public Yahoo Finance (best-effort, no API key).
    """

    SOURCE = "yahoo_short_interest"
    # Only emit when short % of float exceeds this threshold
    MIN_SHORT_PCT = 0.03
    # Only emit when month-over-month change in shares short exceeds this
    MIN_CHANGE_PCT = 0.20
    RELIABILITY = 0.75

    def fetch(self) -> list[dict]:
        out = []
        for ticker in W.TICKERS:
            item = self._fetch_one(ticker)
            if item:
                out.append(item)
            time.sleep(0.15)  # gentle rate-limit: ~30 tickers × 150ms ≈ 4.5s total
        return out

    def _fetch_one(self, ticker: str) -> dict | None:
        # Primary: yfinance info (more reliable than direct quoteSummary API call)
        short_pct = None
        shares_short = None
        prior_shares = None
        short_ratio = None  # days-to-cover at avg daily volume
        date_short = None   # FINRA settlement date of this snapshot
        try:
            import yfinance as _yf
            info = _yf.Ticker(ticker).info
            raw_pct = info.get("shortPercentOfFloat")
            if raw_pct is not None:
                short_pct = float(raw_pct)
            raw_ss = info.get("sharesShort")
            if raw_ss is not None:
                shares_short = int(raw_ss)
            raw_pr = info.get("sharesShortPriorMonth")
            if raw_pr is not None:
                prior_shares = int(raw_pr)
            raw_sr = info.get("shortRatio")
            if raw_sr is not None:
                try:
                    short_ratio = float(raw_sr)
                except (TypeError, ValueError):
                    pass
            raw_ds = info.get("dateShortInterest")
            if raw_ds is not None:
                try:
                    date_short = int(raw_ds)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
        # Fallback: direct quoteSummary API
        if short_pct is None:
            url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
                   "?modules=defaultKeyStatistics")
            try:
                data = fetch_json(url, headers={"User-Agent": "Mozilla/5.0"})
                if data:
                    stats = (data.get("quoteSummary") or {}).get("result") or []
                    if stats:
                        ks = stats[0].get("defaultKeyStatistics") or {}
                        raw = (ks.get("shortPercentOfFloat") or {}).get("raw")
                        if raw is not None:
                            short_pct = float(raw)
                        raw_ss = (ks.get("sharesShort") or {}).get("raw")
                        if raw_ss:
                            shares_short = int(raw_ss)
                        raw_pr = (ks.get("sharesShortPriorMonth") or {}).get("raw")
                        if raw_pr:
                            prior_shares = int(raw_pr)
                        raw_sr = (ks.get("shortRatio") or {}).get("raw")
                        if raw_sr is not None:
                            try:
                                short_ratio = float(raw_sr)
                            except (TypeError, ValueError):
                                pass
                        raw_ds = (ks.get("dateShortInterest") or {}).get("raw")
                        if raw_ds:
                            try:
                                date_short = int(raw_ds)
                            except (TypeError, ValueError):
                                pass
            except Exception:
                pass
        try:
            if short_pct is None:
                return None
            # MoM share-count delta — drives direction tag AND trigger gate
            change_pct = None
            if prior_shares and shares_short and prior_shares > 0:
                change_pct = (shares_short - prior_shares) / prior_shares
            # Three orthogonal trigger gates (any one fires the emit):
            #   - elevated %float (>=3%) — bearish positioning base level
            #   - big MoM change (>=20%) — significant repositioning
            #   - days-to-cover >=4 — concentrated squeeze fuel even if pct low
            elevated_pct = short_pct >= self.MIN_SHORT_PCT
            big_mom = change_pct is not None and abs(change_pct) >= self.MIN_CHANGE_PCT
            squeeze_fuel = short_ratio is not None and short_ratio >= 4.0
            if not (elevated_pct or big_mom or squeeze_fuel):
                return None
            # Direction tag — actionable interpretation: RISING = bearish add,
            # COVERING = squeeze in progress, FLAT = stable, ELEVATED = no prior
            if change_pct is None:
                direction_tag = "ELEVATED"
            elif change_pct >= 0.05:
                direction_tag = "RISING"
            elif change_pct <= -0.05:
                direction_tag = "COVERING"
            else:
                direction_tag = "FLAT"
            head = f"[{ticker}] Short interest {direction_tag}"
            if date_short:
                try:
                    snap = datetime.utcfromtimestamp(date_short).strftime("%Y-%m-%d")
                    head += f" (FINRA snap {snap})"
                except (TypeError, ValueError, OSError):
                    pass
            parts = [head, f"{short_pct*100:.1f}% of float"]
            change_direction = ""
            if change_pct is not None and abs(change_pct) >= self.MIN_CHANGE_PCT:
                arrow = "↑" if change_pct > 0 else "↓"
                change_direction = "up" if change_pct > 0 else "down"
                parts.append(f"{arrow}{abs(change_pct)*100:.0f}% MoM share-count")
            if short_ratio is not None:
                parts.append(f"{short_ratio:.1f}d to cover")
            squeeze_note = ""
            if short_pct >= 0.10 and change_direction == "up":
                squeeze_note = "elevated short + rising = squeeze-setup risk"
            elif short_pct >= 0.08:
                squeeze_note = "elevated short interest = potential squeeze setup on positive catalyst"
            elif squeeze_fuel and short_pct >= 0.05:
                squeeze_note = "concentrated days-to-cover = squeeze-fuel"
            if squeeze_note:
                parts.append(squeeze_note)
            text = " — ".join(parts)
            return {
                "text": text,
                "source": self.SOURCE,
                "url": f"https://finance.yahoo.com/quote/{ticker}",
                "reliability": self.RELIABILITY,
            }
        except Exception:
            return None


class OptionsMarketAdapter:
    """
    Options-market positioning signals for top watchlist tickers via yfinance.

    Computes three non-consensus signals per ticker using the nearest weekly
    options expiry (skipping same-day 0DTE):

      1. Put/Call OI ratio — aggregate open interest across all strikes.
         < 0.5 = notably bullish positioning; > 1.2 = notably bearish.
      2. ATM IV skew (put IV − call IV at nearest strike to spot).
         > 5% = put premium elevated (market buying downside protection).
         < −5% = call premium elevated (unusual, signals call demand/squeeze risk).
      3. Expected move ±% = ATM straddle price / spot × 100. Captures the
         market-implied move for the expiry window regardless of direction.

    Items are emitted only when at least one signal crosses a notable threshold,
    reducing noise to signal-worthy events. Dedup key is stable per
    (ticker, expiry, rounded_pc_ratio) so daily re-runs don't flood raw_items
    when the options picture is unchanged.

    Source: 'options_market'. Reliability 0.82 (exchange-derived via Yahoo Finance;
    higher than editorial, lower than SEC primary source).
    """

    SOURCE = "options_market"
    RELIABILITY = 0.82
    # Emit only for liquid-options top positions to keep runtime ≤ 20s
    TICKERS = [
        "NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL",
        "AMD", "TSM", "ASML", "ARM", "AVGO", "PLTR",
        "ORCL", "NOW", "CRM", "SNOW", "CRWD",
    ]
    # Thresholds for "notable" signals
    PC_BULLISH = 0.50   # P/C ratio below this = notably bullish
    PC_BEARISH = 1.20   # P/C ratio above this = notably bearish
    IV_SKEW_HIGH = 0.05  # put IV > call IV by 5pp = elevated fear
    IV_SKEW_LOW = -0.05  # call IV > put IV by 5pp = unusual call demand
    EXPECTED_MOVE_HIGH = 0.04  # ±4% expected move = elevated uncertainty

    def fetch(self) -> list[dict]:
        try:
            import yfinance as yf
        except ImportError:
            return []
        out = []
        for ticker in self.TICKERS:
            try:
                item = self._fetch_one(ticker, yf)
                if item:
                    out.append(item)
                time.sleep(0.2)
            except Exception:
                pass
        return out

    def _fetch_one(self, ticker: str, yf) -> dict | None:
        t = yf.Ticker(ticker)
        price = getattr(t.fast_info, "last_price", None)
        if not price or price <= 0:
            return None
        exps = t.options
        if not exps:
            return None

        # Skip same-day 0DTE; prefer next weekly expiry
        from datetime import date as _date
        today_str = _date.today().isoformat()
        exp = next((e for e in exps if e > today_str), exps[-1])

        chain = t.option_chain(exp)
        calls = chain.calls
        puts = chain.puts
        if calls.empty or puts.empty:
            return None

        # 1. Put/Call OI ratio
        total_call_oi = int(calls["openInterest"].sum())
        total_put_oi = int(puts["openInterest"].sum())
        pc_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else None

        # 2. ATM IV skew
        atm_call_idx = (calls["strike"] - price).abs().idxmin()
        atm_put_idx = (puts["strike"] - price).abs().idxmin()
        atm_call_iv = float(calls.loc[atm_call_idx, "impliedVolatility"])
        atm_put_iv = float(puts.loc[atm_put_idx, "impliedVolatility"])
        iv_skew = atm_put_iv - atm_call_iv  # positive = puts pricier (normal)

        # 3. Expected move from ATM straddle
        atm_call_price = float(calls.loc[atm_call_idx, "lastPrice"])
        atm_put_price = float(puts.loc[atm_put_idx, "lastPrice"])
        straddle = atm_call_price + atm_put_price
        expected_move_pct = straddle / price  # as fraction

        # Only emit when at least one signal is notable
        notable = False
        signals = []

        if pc_ratio is not None:
            if pc_ratio < self.PC_BULLISH:
                signals.append(f"P/C OI {pc_ratio:.2f} (bullish positioning)")
                notable = True
            elif pc_ratio > self.PC_BEARISH:
                signals.append(f"P/C OI {pc_ratio:.2f} (bearish positioning)")
                notable = True
            else:
                signals.append(f"P/C OI {pc_ratio:.2f}")

        if iv_skew > self.IV_SKEW_HIGH:
            signals.append(f"IV skew +{iv_skew*100:.1f}pp (put premium elevated, downside protection bid)")
            notable = True
        elif iv_skew < self.IV_SKEW_LOW:
            signals.append(f"IV skew {iv_skew*100:.1f}pp (call premium elevated, squeeze/momentum risk)")
            notable = True
        else:
            signals.append(f"IV skew {iv_skew*100:.1f}pp")

        if expected_move_pct >= self.EXPECTED_MOVE_HIGH:
            signals.append(f"expected move ±{expected_move_pct*100:.1f}% by {exp} (elevated uncertainty)")
            notable = True
        else:
            signals.append(f"expected move ±{expected_move_pct*100:.1f}% by {exp}")

        if not notable:
            return None

        text = f"[{ticker}] Options: {'; '.join(signals)}"
        # Stable dedup key: ticker + expiry + coarse P/C bucket
        pc_bucket = f"{round(pc_ratio, 1)}" if pc_ratio else "n/a"
        dedup_url = f"https://finance.yahoo.com/quote/{ticker}/options?exp={exp}&pc={pc_bucket}"
        return {
            "text": text,
            "source": self.SOURCE,
            "url": dedup_url,
            "reliability": self.RELIABILITY,
        }


class EpsRevisionsAdapter:
    """
    Sell-side analyst EPS estimate revision velocity per watchlist ticker.

    Pulls yfinance `eps_revisions` (# analysts who raised/cut EPS estimates over
    the last 7d / 30d) and `eps_trend` (the EPS estimate level current vs 30d
    ago) for the CURRENT quarter (0q) and CURRENT fiscal year (0y). This is the
    StarMine/IBES-style estimate-revision factor: in academic asset-pricing the
    single strongest forward-return predictor for individual equities (PEAD;
    Jegadeesh-Titman-style momentum). Hedge funds buy this signal at six-figure
    annual cost from FactSet/Refinitiv; we get it free from Yahoo.

    Emits per ticker ONLY when there is directional momentum (one side
    dominates), filtering out tug-of-war and neutral periods to keep signal
    density high:

      - Strong 7d direction: |up_7d − down_7d| ≥ 3 AND one side ≥ 3× the other
      - Strong 30d direction: |up_30d − down_30d| ≥ 5 AND one side ≥ 2× the other
      - Estimate drift: |current EPS − 30d-ago EPS| / |30d-ago EPS| ≥ 3%

    Text includes 0q revision count, 30d EPS drift %, current consensus EPS,
    and adds a 0y line when the FY revision direction is also notable. Stable
    dedup key per (ticker, 0q-direction-bucket, drift-bucket) so re-runs in the
    same direction collapse to one row.

    Source: 'eps_revisions'. Reliability 0.85 (analyst-consensus aggregated by
    Yahoo from IBES contributors; high signal but second-order vs primary
    company filings).
    """

    SOURCE = "eps_revisions"
    RELIABILITY = 0.85
    # Same 17 liquid watchlist names used by OptionsMarketAdapter / ShortInterest
    TICKERS = [
        "NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL",
        "AMD", "TSM", "ASML", "ARM", "AVGO", "PLTR",
        "ORCL", "NOW", "CRM", "SNOW", "CRWD",
    ]
    # Thresholds for "directional" — designed to filter out routine churn
    NET_7D_MIN = 3       # |up_7d - down_7d|
    NET_30D_MIN = 5      # |up_30d - down_30d|
    DOMINANCE_7D = 3.0   # one side must be ≥ 3x the other (7d)
    DOMINANCE_30D = 2.0  # ≥ 2x (30d)
    DRIFT_PCT_MIN = 0.03  # ≥3% EPS estimate drift vs 30d ago

    def fetch(self) -> list[dict]:
        try:
            import yfinance as yf
        except ImportError:
            return []
        out = []
        for ticker in self.TICKERS:
            try:
                item = self._fetch_one(ticker, yf)
                if item:
                    out.append(item)
                time.sleep(0.2)
            except Exception:
                pass
        return out

    @staticmethod
    def _row(df, period: str):
        """Safe row lookup — returns dict or None."""
        if df is None:
            return None
        try:
            if df.empty or period not in df.index:
                return None
            row = df.loc[period]
            return row.to_dict() if hasattr(row, "to_dict") else dict(row)
        except Exception:
            return None

    @staticmethod
    def _direction(up: float, down: float, net_min: float, dominance: float) -> str | None:
        """Return 'up' / 'down' / None based on count + dominance gates."""
        try:
            u = float(up or 0)
            d = float(down or 0)
        except (TypeError, ValueError):
            return None
        net = u - d
        if abs(net) < net_min:
            return None
        if net > 0:
            # up side must dominate
            if d == 0 or u / max(d, 1.0) >= dominance:
                return "up"
        else:
            if u == 0 or d / max(u, 1.0) >= dominance:
                return "down"
        return None

    def _fetch_one(self, ticker: str, yf) -> dict | None:
        t = yf.Ticker(ticker)
        # yfinance occasionally returns None instead of a DataFrame for sparse tickers
        try:
            rev_df = t.eps_revisions
        except Exception:
            rev_df = None
        try:
            est_df = t.earnings_estimate
        except Exception:
            est_df = None
        try:
            trend_df = t.eps_trend
        except Exception:
            trend_df = None

        q_rev = self._row(rev_df, "0q")
        q_est = self._row(est_df, "0q")
        q_trd = self._row(trend_df, "0q")
        if not (q_rev and q_est and q_trd):
            return None

        up7 = q_rev.get("upLast7days", 0)
        # yfinance casing inconsistency: confirmed downLast7Days has capital D
        down7 = q_rev.get("downLast7Days", q_rev.get("downLast7days", 0))
        up30 = q_rev.get("upLast30days", 0)
        down30 = q_rev.get("downLast30days", 0)

        # Direction signals
        dir_7d = self._direction(up7, down7, self.NET_7D_MIN, self.DOMINANCE_7D)
        dir_30d = self._direction(up30, down30, self.NET_30D_MIN, self.DOMINANCE_30D)

        # EPS drift current vs 30d ago
        cur = q_trd.get("current")
        ago30 = q_trd.get("30daysAgo")
        drift_pct = None
        if cur is not None and ago30 is not None:
            try:
                cur_f = float(cur)
                ago_f = float(ago30)
                if abs(ago_f) > 0.01:  # avoid div-by-near-zero noise
                    drift_pct = (cur_f - ago_f) / abs(ago_f)
            except (TypeError, ValueError):
                pass

        drift_notable = drift_pct is not None and abs(drift_pct) >= self.DRIFT_PCT_MIN

        if not (dir_7d or dir_30d or drift_notable):
            return None

        # Confirm direction alignment — if drift and revision counts contradict,
        # downgrade to "mixed" — happens rarely but avoids false reads
        signs = []
        if dir_7d:
            signs.append(1 if dir_7d == "up" else -1)
        if dir_30d:
            signs.append(1 if dir_30d == "up" else -1)
        if drift_notable:
            signs.append(1 if drift_pct > 0 else -1)
        if len(set(signs)) > 1:
            # Mixed signals — skip (tug of war)
            return None

        direction = "POSITIVE" if (signs and signs[0] > 0) else "NEGATIVE"

        # Build the text
        parts = [f"[{ticker}] Sell-side EPS revisions {direction} (current quarter)"]
        parts.append(
            f"7d: {int(up7 or 0)} up / {int(down7 or 0)} down · "
            f"30d: {int(up30 or 0)} up / {int(down30 or 0)} down"
        )
        if drift_pct is not None:
            drift_sign = "+" if drift_pct >= 0 else ""
            parts.append(
                f"consensus Q-EPS ${cur_f:.2f} ({drift_sign}{drift_pct*100:.1f}% vs 30d ago)"
            )

        # Optional FY (0y) addendum when also directionally notable AND aligned
        y_rev = self._row(rev_df, "0y")
        if y_rev:
            yup7 = y_rev.get("upLast7days", 0)
            ydown7 = y_rev.get("downLast7Days", y_rev.get("downLast7days", 0))
            yup30 = y_rev.get("upLast30days", 0)
            ydown30 = y_rev.get("downLast30days", 0)
            y_dir = self._direction(yup30, ydown30, self.NET_30D_MIN, self.DOMINANCE_30D)
            if y_dir and ((y_dir == "up") == (direction == "POSITIVE")):
                parts.append(
                    f"FY-30d: {int(yup30 or 0)} up / {int(ydown30 or 0)} down (aligned)"
                )

        # Number of contributing analysts gives confidence sizing
        n_anal = q_est.get("numberOfAnalysts")
        if n_anal:
            try:
                parts.append(f"{int(n_anal)} analysts in consensus")
            except (TypeError, ValueError):
                pass

        text = " — ".join(parts)

        # Stable dedup key per (ticker, direction, drift bucket in pp)
        drift_bucket = "n/a"
        if drift_pct is not None:
            drift_bucket = f"{int(round(drift_pct * 100))}"
        dedup_url = (
            f"https://finance.yahoo.com/quote/{ticker}/analysis"
            f"?dir={direction.lower()}&dr={drift_bucket}"
        )
        return {
            "text": text,
            "source": self.SOURCE,
            "url": dedup_url,
            "reliability": self.RELIABILITY,
        }


class AnalystConsensusAdapter:
    """
    Sell-side analyst price-target & recommendation consensus per liquid
    watchlist ticker. The complement to EpsRevisionsAdapter: EPS revisions
    capture estimate-momentum (PEAD), this adapter captures VALUATION-anchor
    (PT-implied upside/downside vs current price) + DISPERSION (high-low PT
    spread = analyst disagreement) + 3-month RATING DRIFT (bucket migration
    from strong-buy/buy → hold/sell or vice versa).

    Free yfinance fields (no key):
      - targetMeanPrice / targetHighPrice / targetLowPrice / targetMedianPrice
      - numberOfAnalystOpinions, recommendationMean (1=SB ... 5=S), recommendationKey
      - currentPrice / regularMarketPrice (the spot to anchor implied upside against)
      - Ticker.recommendations DataFrame: per-month rating-bucket counts (0m,
        -1m, -2m, -3m) → drift = (strongBuy+buy)@0m − (strongBuy+buy)@-3m
        minus (sell+strongSell)@0m − @-3m. Positive = upgrades, negative = downgrades.

    Same 17 liquid watchlist names as EpsRevisions / Options / ShortInterest.
    Quant funds (Millennium, Citadel, Point72) pay FactSet / Refinitiv /
    Bloomberg EE/ANR mid-five-figures/yr for the same aggregated consensus;
    we get it free from Yahoo's IBES-derived feed.

    Emits per ticker ONLY when at least one trigger fires (keeps signal
    density high):
      - |implied_upside| ≥ UPSIDE_MIN (20%) — directional valuation anchor
      - dispersion = (high − low) / mean ≥ DISPERSION_MIN (75%) — analyst
        disagreement, often setup for post-earnings re-rating
      - |3m rating drift| ≥ DRIFT_MIN (4) — bucket migration over a quarter,
        a forward-return signal in Womack (1996) / Jegadeesh-Kim research

    Stable dedup per (ticker, upside-bucket-5pp, recommendation-key) so
    re-runs within the same state collapse to one row; a 5pp move in
    implied upside (e.g. price rallies into PT) or a recommendationKey
    transition (buy → strong_buy) re-emits.

    Source: 'analyst_consensus'. Reliability 0.83 — Yahoo-aggregated IBES
    consensus (above editorial tech_news 0.60, parallel to options_market
    0.82, below primary SEC filings 0.95+). Forward-looking opinion, not
    confirmed fact.
    """

    SOURCE = "analyst_consensus"
    RELIABILITY = 0.83
    TICKERS = [
        "NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL",
        "AMD", "TSM", "ASML", "ARM", "AVGO", "PLTR",
        "ORCL", "NOW", "CRM", "SNOW", "CRWD",
    ]
    UPSIDE_MIN = 0.20         # ≥20% implied upside/downside vs spot
    DISPERSION_MIN = 0.75     # high−low spread ≥75% of mean PT
    DRIFT_MIN = 4             # |3m bucket migration| ≥ 4 net analysts
    MIN_ANALYSTS = 8          # require ≥8 analysts for the consensus to be meaningful

    def fetch(self) -> list[dict]:
        try:
            import yfinance as yf
        except ImportError:
            return []
        out = []
        for ticker in self.TICKERS:
            try:
                item = self._fetch_one(ticker, yf)
                if item:
                    out.append(item)
                time.sleep(0.2)
            except Exception:
                # One ticker failing must never kill the adapter — yfinance
                # info dicts go empty for delisted/halted/sparse tickers.
                pass
        return out

    def _fetch_one(self, ticker: str, yf) -> dict | None:
        t = yf.Ticker(ticker)
        try:
            info = t.info or {}
        except Exception:
            return None
        mean = info.get("targetMeanPrice")
        high = info.get("targetHighPrice")
        low = info.get("targetLowPrice")
        n = info.get("numberOfAnalystOpinions") or 0
        rec_mean = info.get("recommendationMean")
        rec_key = (info.get("recommendationKey") or "").strip()
        px = info.get("currentPrice") or info.get("regularMarketPrice")
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            n_int = 0
        if not (mean and px) or n_int < self.MIN_ANALYSTS:
            return None
        try:
            mean_f = float(mean)
            px_f = float(px)
            high_f = float(high) if high else None
            low_f = float(low) if low else None
        except (TypeError, ValueError):
            return None
        if px_f <= 0 or mean_f <= 0:
            return None
        upside = (mean_f - px_f) / px_f
        dispersion = None
        if high_f is not None and low_f is not None and mean_f:
            dispersion = (high_f - low_f) / mean_f
        # 3-month rating drift from the recommendations bucket table
        drift = self._rating_drift(t)
        # Trigger gates
        upside_notable = abs(upside) >= self.UPSIDE_MIN
        disp_notable = dispersion is not None and dispersion >= self.DISPERSION_MIN
        drift_notable = drift is not None and abs(drift) >= self.DRIFT_MIN
        if not (upside_notable or disp_notable or drift_notable):
            return None
        # Direction tag for triage — anchor on implied upside when present,
        # otherwise fall back to drift direction
        if upside_notable:
            direction = "BULLISH" if upside > 0 else "BEARISH"
        elif drift_notable and drift is not None:
            direction = "UPGRADES" if drift > 0 else "DOWNGRADES"
        else:
            direction = "DISPERSION"
        # Build the text
        sign = "+" if upside >= 0 else ""
        parts = [
            f"[{ticker}] Analyst-Konsens {direction}: "
            f"PT-Mittel ${mean_f:.2f} vs Spot ${px_f:.2f} → {sign}{upside*100:.1f}% implied"
        ]
        if high_f is not None and low_f is not None:
            parts.append(f"Range ${low_f:.2f}-${high_f:.2f}")
        rec_label = rec_key.replace("_", " ").title() if rec_key else ""
        if rec_mean is not None:
            try:
                rec_str = f"Rating-Mean {float(rec_mean):.2f}"
                if rec_label:
                    rec_str += f" ({rec_label})"
                parts.append(rec_str)
            except (TypeError, ValueError):
                pass
        parts.append(f"{n_int} Analysten")
        if dispersion is not None and disp_notable:
            parts.append(f"Spread ±{dispersion*100:.0f}% (Disagreement)")
        if drift_notable and drift is not None:
            dsign = "+" if drift > 0 else ""
            label = "Upgrades" if drift > 0 else "Downgrades"
            parts.append(f"3m-Drift {dsign}{drift} net ({label})")
        text = " — ".join(parts)
        # Stable dedup: bucket implied upside in 5pp steps + rec key
        upside_bucket = int(round(upside * 20)) * 5  # 5pp granularity
        rec_bucket = rec_key or "na"
        dedup_url = (
            f"https://finance.yahoo.com/quote/{ticker}/analysis"
            f"?pt_up={upside_bucket}&rec={rec_bucket}"
        )
        return {
            "text": text,
            "source": self.SOURCE,
            "url": dedup_url,
            "reliability": self.RELIABILITY,
        }

    @staticmethod
    def _rating_drift(ticker_obj) -> int | None:
        """Return net rating-bucket migration over the last 3 months.
        Positive = net upgrades (bullish), negative = net downgrades.
        ``(strongBuy+buy)`` shift minus ``(sell+strongSell)`` shift between
        the 0m and -3m rows of ``Ticker.recommendations``."""
        try:
            df = ticker_obj.recommendations
            if df is None or df.empty:
                return None
            row_0 = df[df["period"] == "0m"]
            row_3 = df[df["period"] == "-3m"]
            if row_0.empty or row_3.empty:
                return None
            r0 = row_0.iloc[0]
            r3 = row_3.iloc[0]
            bull_0 = int(r0["strongBuy"]) + int(r0["buy"])
            bull_3 = int(r3["strongBuy"]) + int(r3["buy"])
            bear_0 = int(r0["sell"]) + int(r0["strongSell"])
            bear_3 = int(r3["sell"]) + int(r3["strongSell"])
            return (bull_0 - bull_3) - (bear_0 - bear_3)
        except Exception:
            return None


class GovContractsAdapter:
    """
    US Federal contract awards per watchlist company via USAspending.gov.

    Strategy.md tier-1 supply-chain / forward-revenue signal: a $1B+ DoD or
    Treasury contract obligation routinely appears in USAspending 7-14 days
    before the contractor announces it in a press release / 8-K. Quant funds
    pay Quiver Quant / GovTribe / Bloomberg GOVCON five-figure annual fees for
    this same data; the official source is free, JSON, no API key required.

    The signal is highest for software/cloud primes whose revenue is heavily
    federal (PLTR — ~55% gov rev, ORCL — Oracle Cloud Gov + JWCC, MSFT/AMZN/
    GOOGL — JWCC cloud, DELL — server hardware) and for networking primes
    (ANET federal). Hardware-OEM tickers (NVDA, AMD, ASML, TSM, etc.) typically
    reach federal buyers via integrator resellers and have ~zero direct prime
    flow — they are intentionally excluded to keep API budget focused (validated
    empirically 2026-05-23: 0 contracts ≥$1M past 2 months for NVDA/AMD/CRWD/
    NOW/SNOW/CRM/VRT/ADBE).

    Per ticker, queries award_type A/B/C/D (definitive + IDV contracts; excludes
    grants, loans, direct payments) with action_date in the last 14 days and
    amount ≥ $1M. Emits one item per qualifying award. Stable dedup per
    USAspending `generated_internal_id`.

    Source: 'gov_contracts'. Reliability 0.90 — official US Treasury / SAM.gov
    data; same authoritative tier as Fed/BLS macro releases.
    """

    SOURCE = "gov_contracts"
    RELIABILITY = 0.90
    LOOKBACK_DAYS = 14
    AMOUNT_FLOOR = 1_000_000
    MAX_AWARDS_PER_TICKER = 10
    API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

    # Validated empirically 2026-05-23 against USAspending for awards >=$1M signed
    # 2026-04-01 -> present: only these 7 watchlist tickers have measurable direct
    # federal prime flow. The other 23 watchlist tickers returned 0 awards at this
    # threshold and are omitted (saves 23 API calls/cycle for zero signal).
    TICKER_TO_RECIPIENT = {
        "PLTR":  ["PALANTIR"],
        "MSFT":  ["MICROSOFT"],
        "AMZN":  ["AMAZON WEB SERVICES", "AMAZON.COM"],
        "GOOGL": ["GOOGLE LLC"],
        "ORCL":  ["ORACLE AMERICA", "ORACLE CORPORATION"],
        "DELL":  ["DELL FEDERAL", "DELL MARKETING"],
        "ANET":  ["ARISTA NETWORKS"],
    }

    # A=BPA Call, B=Purchase Order, C=Delivery Order, D=Definitive Contract.
    # Excludes grants, loans, direct payments.
    AWARD_TYPE_CODES = ["A", "B", "C", "D"]

    @staticmethod
    def _fmt_dollar(v) -> str:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "$?"
        if abs(v) >= 1e9:
            return f"${v/1e9:.2f}B"
        if abs(v) >= 1e6:
            return f"${v/1e6:.1f}M"
        if abs(v) >= 1e3:
            return f"${v/1e3:.0f}K"
        return f"${v:.0f}"

    @classmethod
    def _post_json(cls, body: dict, timeout: int = 20):
        try:
            data = _json.dumps(body).encode("utf-8")
            headers = dict(_DEFAULT_UA)
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"
            req = urllib.request.Request(cls.API_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return _json.loads(raw)
        except Exception:
            return None

    def fetch(self) -> list[dict]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=self.LOOKBACK_DAYS)
        start_s, end_s = start.isoformat(), end.isoformat()

        out: list[dict] = []
        seen_award_ids: set[str] = set()
        for ticker, recipients in self.TICKER_TO_RECIPIENT.items():
            body = {
                "filters": {
                    "award_type_codes": self.AWARD_TYPE_CODES,
                    "recipient_search_text": recipients,
                    "time_period": [{"start_date": start_s, "end_date": end_s}],
                    "award_amounts": [{"lower_bound": self.AMOUNT_FLOOR}],
                },
                "fields": [
                    "Award ID", "Recipient Name", "Award Amount", "Description",
                    "Awarding Agency", "Awarding Sub Agency", "Start Date",
                    "Last Modified Date", "Base Obligation Date", "psc_description",
                ],
                "sort": "Award Amount",
                "order": "desc",
                "limit": self.MAX_AWARDS_PER_TICKER,
                "page": 1,
            }
            resp = self._post_json(body)
            if not resp:
                time.sleep(0.4)
                continue
            for r in resp.get("results", []) or []:
                gen_id = r.get("generated_internal_id") or ""
                award_id = r.get("Award ID") or gen_id
                if not award_id or award_id in seen_award_ids:
                    continue
                seen_award_ids.add(award_id)
                item = self._format_item(ticker, r)
                if item:
                    out.append(item)
            time.sleep(0.4)
        return out

    def _format_item(self, ticker: str, r: dict) -> dict | None:
        amount = r.get("Award Amount")
        if amount is None or amount < self.AMOUNT_FLOOR:
            return None
        agency = (r.get("Awarding Agency") or "").strip()
        sub_agency = (r.get("Awarding Sub Agency") or "").strip()
        if sub_agency and sub_agency.lower() != agency.lower():
            agency_str = f"{agency} ({sub_agency})"
        else:
            agency_str = agency or "Federal agency"
        # Use Last Modified Date as the "action" date — that's WHEN the recent
        # activity (new obligation, modification, exercise) occurred, regardless
        # of when the parent IDV was originally signed. The Base Obligation Date
        # is the parent contract's signing date and can be years older for
        # ongoing IDVs (AMZN AWS BPAs, ORCL DoD cloud contracts, etc.).
        last_mod = (r.get("Last Modified Date") or "")[:10]
        base_obl = r.get("Base Obligation Date") or ""
        action_date = last_mod or base_obl or r.get("Start Date") or "n/a"
        # If the parent IDV is older than the recent activity, surface both
        # so analyst sees "recent mod on a 2021-vintage contract" vs "fresh award".
        base_hint = ""
        if base_obl and last_mod and base_obl[:7] != last_mod[:7]:
            base_hint = f" (base contract: {base_obl})"

        psc = (r.get("psc_description") or "").strip()
        desc = (r.get("Description") or "").strip()
        if desc:
            desc = re.sub(r"\s+", " ", desc)[:220]
        recipient = (r.get("Recipient Name") or "").strip().title()

        amount_str = self._fmt_dollar(amount)
        parts = [
            f"[{ticker}] Federal contract activity {amount_str} from {agency_str} (action {action_date}){base_hint}",
            f"Recipient: {recipient}",
        ]
        if psc:
            parts.append(f"PSC: {psc[:90]}")
        if desc:
            parts.append(f"Scope: {desc}")
        text = " — ".join(parts)

        gen_id = r.get("generated_internal_id") or ""
        url = f"https://www.usaspending.gov/award/{gen_id}" if gen_id else None

        return {
            "text": text,
            "source": self.SOURCE,
            "url": url,
            "reliability": self.RELIABILITY,
        }


# ---------------------------------------------------------------------------
# Job-postings velocity (Greenhouse + Lever public boards)
# ---------------------------------------------------------------------------

# Title-keyword buckets — hiring composition reveals capex direction. ML/AI
# growth signals product/research intensification; Sales/GTM growth signals
# forward revenue pipeline buildout; Infra/Platform growth signals capacity /
# capex (compute, datacenter, networking — direct demand for NVDA/AMD/AVGO/
# ANET/VRT thesis). Patterns are conservative — false positives diluted by
# only counting clear matches.
#
# Order matters because some titles match multiple buckets — earlier patterns
# win. We check the most-role-specific tokens first so a title like
# "Account Executive, AI Native" lands in sales_gtm (the role is AE — the AI
# is product-vertical context), and "ML Solutions Engineer" lands in sales_gtm
# too (Solutions Engineer is pre-sales). Then ml_ai before infra_dc so an
# "ML Platform Engineer" lands in ml_ai (the platform is for ML), not infra.
_JOB_BUCKET_PATTERNS = [
    ("sales_gtm", re.compile(
        r"\b(account\s+executive|\bsales\b|business\s+development|"
        r"go-to-market|\bgtm\b|forward\s+deployed|customer\s+success|"
        r"solutions\s+engineer|\bsdr\b|\bbdr\b)", re.I)),
    ("ml_ai", re.compile(
        r"\b(machine\s+learning|applied\s+scientist|research\s+scientist|"
        r"research\s+engineer|deep\s+learning|llm|nlp|computer\s+vision|"
        r"\bml\b|\bai\b|generative)", re.I)),
    ("infra_dc", re.compile(
        r"\b(infrastructure|platform\s+engineer|site\s+reliability|\bsre\b|"
        r"devops|kubernetes|data\s*cent(?:er|re)|\bgpu\b|\bhpc\b|"
        r"distributed\s+systems|networking\s+engineer)", re.I)),
]


def _classify_job_title(title: str) -> str | None:
    """Return bucket key for a job title or None if no clear match."""
    if not title:
        return None
    for key, pat in _JOB_BUCKET_PATTERNS:
        if pat.search(title):
            return key
    return None


class JobPostingsAdapter:
    """
    Job-posting velocity per AI/Tech company via Greenhouse + Lever public boards.

    Strategy.md tier-1 target ('Job-Posting-Velocity → Forward-Revenue-Indikator,
    Kapazitätsaufbau'): hiring is the single strongest forward indicator of
    revenue and capex direction — months ahead of guidance, quarters ahead of
    earnings. Quant funds (Citadel, Millennium, Point72) pay Revelio Labs /
    LinkUp / Thinknum five-figures/month for this exact signal; Greenhouse and
    Lever publish their customers' boards as free public JSON, no API key.

    Two flavours of signal layered into one snapshot per company per day:

    1. **Volume** — total open requisitions and # NEW in last 7d
       (using Greenhouse `updated_at` / Lever `createdAt`). A 7d-new burst is
       the actionable instantaneous read (e.g. Anthropic opening 78 new reqs
       in a single week — AI lab on hyper-growth = compute / NVDA demand).

    2. **Composition** — bucket-count by title pattern (ML/AI, Infra/DC,
       Sales/GTM). The composition is the leading indicator:
       - ML/AI heavy → product/research push, model-cycle thesis
       - Infra/DC heavy → capex / datacenter buildout (NVDA/AVGO/ANET demand)
       - Sales/GTM heavy → revenue pipeline buildout, monetization push

    Coverage is intentionally split:
    - Direct watchlist signal (PLTR on Lever — direct ticker read)
    - AI ecosystem proxy (Anthropic, xAI, Scale AI, Together AI, SambaNova,
      Databricks, Mistral on Greenhouse/Lever) — these private AI labs
      collectively drive ~40-60% of incremental NVDA/AMD compute demand,
      and their hiring pace is the cleanest leading indicator of AI-capex
      direction available outside the hyperscalers' own quarterly guidance.

    Emission discipline:
    - One item per company per day (date bucket in URL query → canonical dedup
      collapses within-day re-emissions to one row).
    - Skip boards returning <5 total postings (dead/closed/empty board noise).
    - Reliability 0.85 — Greenhouse/Lever are official ATS systems run by the
      companies themselves; data is the actual hiring system, not an editorial
      summary.

    Source: 'job_postings'. Free public APIs, no key required, stdlib HTTP only.
    """

    SOURCE = "job_postings"
    RELIABILITY = 0.85
    MIN_TOTAL = 5
    GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    LEVER_URL = "https://api.lever.co/v0/postings/{slug}"

    def fetch(self) -> list[dict]:
        out: list[dict] = []
        as_of = datetime.now(timezone.utc).date().isoformat()
        gh_targets = getattr(W, "JOB_POSTINGS_GREENHOUSE", {})
        lv_targets = getattr(W, "JOB_POSTINGS_LEVER", {})
        for slug, meta in gh_targets.items():
            try:
                item = self._fetch_greenhouse(slug, meta, as_of)
                if item:
                    out.append(item)
            except Exception:
                continue
            time.sleep(0.3)
        for slug, meta in lv_targets.items():
            try:
                item = self._fetch_lever(slug, meta, as_of)
                if item:
                    out.append(item)
            except Exception:
                continue
            time.sleep(0.3)
        return out

    @classmethod
    def _fetch_greenhouse(cls, slug: str, meta: dict, as_of: str) -> dict | None:
        url = cls.GREENHOUSE_URL.format(slug=slug)
        data = fetch_json(url, timeout=12)
        if not data:
            return None
        jobs = data.get("jobs") or []
        if len(jobs) < cls.MIN_TOTAL:
            return None
        now = datetime.now(timezone.utc)
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)
        new_7d, new_30d = 0, 0
        buckets = {"ml_ai": 0, "infra_dc": 0, "sales_gtm": 0}
        for j in jobs:
            ua = j.get("updated_at") or j.get("first_published") or ""
            dt = cls._parse_iso(ua)
            if dt:
                if dt >= cutoff_7d:
                    new_7d += 1
                if dt >= cutoff_30d:
                    new_30d += 1
            bucket = _classify_job_title(j.get("title") or "")
            if bucket:
                buckets[bucket] += 1
        public_url = f"https://boards.greenhouse.io/{slug}?as_of={as_of}"
        return cls._format_item(meta, len(jobs), new_7d, new_30d, buckets, public_url)

    @classmethod
    def _fetch_lever(cls, slug: str, meta: dict, as_of: str) -> dict | None:
        url = cls.LEVER_URL.format(slug=slug)
        data = fetch_json(url, timeout=12)
        if not data or not isinstance(data, list):
            return None
        if len(data) < cls.MIN_TOTAL:
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cutoff_7d_ms = now_ms - 7 * 24 * 3600 * 1000
        cutoff_30d_ms = now_ms - 30 * 24 * 3600 * 1000
        new_7d, new_30d = 0, 0
        buckets = {"ml_ai": 0, "infra_dc": 0, "sales_gtm": 0}
        for j in data:
            created = j.get("createdAt") or 0
            if isinstance(created, (int, float)):
                if created >= cutoff_7d_ms:
                    new_7d += 1
                if created >= cutoff_30d_ms:
                    new_30d += 1
            bucket = _classify_job_title(j.get("text") or "")
            if bucket:
                buckets[bucket] += 1
        public_url = f"https://jobs.lever.co/{slug}?as_of={as_of}"
        return cls._format_item(meta, len(data), new_7d, new_30d, buckets, public_url)

    @staticmethod
    def _parse_iso(s: str) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    @classmethod
    def _format_item(cls, meta: dict, total: int, new_7d: int, new_30d: int,
                     buckets: dict, public_url: str) -> dict:
        label = meta.get("label") or meta.get("ticker") or "?"
        ticker = meta.get("ticker") or "?"
        kind = meta.get("kind") or "proxy"   # 'direct' = watchlist ticker, 'proxy' = ecosystem
        # Composition direction tag — what KIND of hiring dominates.
        # Threshold 25% of classified-positions, min 5 absolute, to avoid
        # spurious labels when one bucket has 2/100.
        classified = sum(buckets.values())
        tags = []
        if classified >= 5:
            for key, label_short in (("ml_ai", "ML/AI"),
                                     ("infra_dc", "Infra/DC"),
                                     ("sales_gtm", "Sales/GTM")):
                if buckets[key] >= 5 and buckets[key] / max(classified, 1) >= 0.25:
                    tags.append(f"{label_short}={buckets[key]}")
        # Headline format: [JOBS · TICKER] LABEL: total open, N new 7d (M 30d); ML/AI=x; Infra/DC=y
        parts = [f"[JOBS · {ticker}] {label} ({kind}): {total} open requisitions"]
        if new_7d > 0:
            if new_30d > new_7d:
                parts.append(f"{new_7d} new last 7d ({new_30d} last 30d)")
            else:
                parts.append(f"{new_7d} new last 7d")
        if tags:
            parts.append("; ".join(tags))
        # Add interpretive hint when 7d-new is a meaningful burst (>= 5% of total OR >=20 absolute).
        if new_7d >= 20 or (total > 0 and new_7d / total >= 0.05):
            parts.append("ACTIVE HIRING BURST")
        text = " — ".join(parts)
        return {
            "text": text[:400],
            "source": cls.SOURCE,
            "url": public_url,
            "reliability": W.SOURCE_RELIABILITY.get(cls.SOURCE, cls.RELIABILITY),
        }


# ---------------------------------------------------------------------------
# Technical-levels pure helpers (testable without yfinance)
# ---------------------------------------------------------------------------

def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values. None if too short."""
    if not values or len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window) / period


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI on the last (period+1) closes. None if too short."""
    if not closes or len(closes) < period + 1 or period <= 0:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    # Wilder seed = simple average of first `period` deltas
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Smooth the rest
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _detect_cross(fast_today: float | None, fast_yest: float | None,
                  slow_today: float | None, slow_yest: float | None) -> str | None:
    """Return 'golden', 'death', or None for SMA-fast vs SMA-slow crossover today."""
    vals = (fast_today, fast_yest, slow_today, slow_yest)
    if any(v is None for v in vals):
        return None
    if fast_yest <= slow_yest and fast_today > slow_today:
        return "golden"
    if fast_yest >= slow_yest and fast_today < slow_today:
        return "death"
    return None


class TechnicalLevelsAdapter:
    """
    Technical-level + price-action triage signals per liquid watchlist ticker.

    The track-record loop and every thesis card today lack objective price
    context. Without persistent technical levels, the Devil's Advocate stage
    has no empirical anchor for "what does the market already price in?" and
    briefings read as headline summaries instead of investment-grade calls.

    Pulls ~260 trading days of OHLCV via yfinance and emits one item per
    ticker only when an institutional technical trigger is present. Multiple
    triggers compose into one richer headline (avoids item flood).

    Triggers (rated by structural importance):
      - Golden / Death cross  : 50d SMA crosses 200d SMA today (rare pivot)
      - 200d SMA breach       : close crosses 200d ±2% band (regime change)
      - 52w high / low        : close within 1% of 252d extreme
      - 50d SMA breach        : close crosses 50d ±2% band (trend break)
      - RSI-14 extreme        : < 30 (oversold) or > 70 (overbought)
      - Volume spike          : day volume > 2× 20d-avg (institutional flow)
      - Gap-up / gap-down     : open vs prior close > ±3%

    Reliability tiered by strongest trigger present:
      - Cross events / 200d breach : 0.90
      - 52w extreme               : 0.87
      - 50d breach                : 0.83
      - RSI / volume / gap only   : 0.78

    Source: 'tech_level' (exchange-derived OHLCV; same authoritative tier as
    options_market). Stable dedup per (ticker, top-trigger, ISO-week) so daily
    re-runs don't flood raw_items when the technical picture is unchanged.
    """

    SOURCE = "tech_level"
    RELIABILITY = 0.85
    SMA_FAST = 50
    SMA_SLOW = 200
    RSI_PERIOD = 14
    RSI_OVERSOLD = 30.0
    RSI_OVERBOUGHT = 70.0
    VOL_SPIKE_MULTIPLE = 2.0
    GAP_THRESHOLD = 0.03
    NEAR_EXTREME_PCT = 0.01   # within 1% of 52w high/low = "near"
    MA_BREACH_BAND = 0.02     # within ±2% of MA = "near the breach"
    HISTORY_PERIOD = "1y"     # ~252 trading days, enough for 200d SMA + 52w

    # Same liquid-options universe as OptionsMarketAdapter — these are the
    # names where technical levels actually matter for institutional flow.
    TICKERS = [
        "NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL",
        "AMD", "TSM", "ASML", "ARM", "AVGO", "PLTR",
        "ORCL", "NOW", "CRM", "SNOW", "CRWD",
    ]

    def fetch(self) -> list[dict]:
        try:
            import yfinance as yf
        except ImportError:
            return []
        out: list[dict] = []
        for ticker in self.TICKERS:
            try:
                item = self._fetch_one(ticker, yf)
                if item:
                    out.append(item)
                time.sleep(0.2)
            except Exception:
                continue
        return out

    def _fetch_one(self, ticker: str, yf) -> dict | None:
        t = yf.Ticker(ticker)
        hist = t.history(period=self.HISTORY_PERIOD, auto_adjust=True)
        if hist is None or hist.empty or len(hist) < self.SMA_SLOW + 5:
            return None

        closes = [float(c) for c in hist["Close"].tolist()]
        opens = [float(o) for o in hist["Open"].tolist()]
        highs = [float(h) for h in hist["High"].tolist()]
        lows = [float(l) for l in hist["Low"].tolist()]
        vols = [float(v) for v in hist["Volume"].tolist()]
        last_date = hist.index[-1].date().isoformat()

        price = closes[-1]
        prior_close = closes[-2]
        open_today = opens[-1]
        gap_pct = (open_today / prior_close - 1.0) if prior_close else 0.0

        sma50_today = _sma(closes, self.SMA_FAST)
        sma50_yest = _sma(closes[:-1], self.SMA_FAST)
        sma200_today = _sma(closes, self.SMA_SLOW)
        sma200_yest = _sma(closes[:-1], self.SMA_SLOW)
        rsi = _rsi(closes, self.RSI_PERIOD)
        avg_vol_20 = _sma(vols[:-1], 20)
        vol_today = vols[-1]

        # 252-trading-day high/low (52-week)
        window = closes[-252:] if len(closes) >= 252 else closes
        window_high = max(window)
        window_low = min(window)

        triggers: list[tuple[int, str]] = []  # (tier, label) — higher = stronger

        # Cross — tier 4
        cross = _detect_cross(sma50_today, sma50_yest, sma200_today, sma200_yest)
        if cross == "golden":
            triggers.append((4, "Golden Cross (50d SMA crossed above 200d today)"))
        elif cross == "death":
            triggers.append((4, "Death Cross (50d SMA crossed below 200d today)"))

        # 200d breach — tier 4
        if sma200_today:
            d200 = price / sma200_today - 1.0
            if abs(d200) <= self.MA_BREACH_BAND:
                side = "above" if d200 >= 0 else "below"
                triggers.append((4, f"close {price:.2f} sits {d200*100:+.1f}% vs 200d SMA {sma200_today:.2f} ({side}, regime line)"))

        # 52w extreme — tier 3
        near_high = (window_high - price) / window_high if window_high else 1.0
        near_low = (price - window_low) / window_low if window_low else 1.0
        if near_high <= self.NEAR_EXTREME_PCT:
            triggers.append((3, f"close {price:.2f} within {near_high*100:.1f}% of 52w high {window_high:.2f}"))
        elif near_low <= self.NEAR_EXTREME_PCT:
            triggers.append((3, f"close {price:.2f} within {near_low*100:.1f}% of 52w low {window_low:.2f}"))

        # 50d breach — tier 2
        if sma50_today:
            d50 = price / sma50_today - 1.0
            if abs(d50) <= self.MA_BREACH_BAND:
                side = "above" if d50 >= 0 else "below"
                triggers.append((2, f"close {d50*100:+.1f}% vs 50d SMA {sma50_today:.2f} ({side})"))

        # RSI extreme — tier 1
        if rsi is not None:
            if rsi < self.RSI_OVERSOLD:
                triggers.append((1, f"RSI-14 {rsi:.0f} (oversold)"))
            elif rsi > self.RSI_OVERBOUGHT:
                triggers.append((1, f"RSI-14 {rsi:.0f} (overbought)"))

        # Volume spike — tier 1
        if avg_vol_20 and vol_today > 0:
            v_mult = vol_today / avg_vol_20
            if v_mult >= self.VOL_SPIKE_MULTIPLE:
                triggers.append((1, f"volume {v_mult:.1f}× 20d-avg (institutional flow)"))

        # Gap — tier 1
        if abs(gap_pct) >= self.GAP_THRESHOLD:
            direction = "gap-up" if gap_pct > 0 else "gap-down"
            triggers.append((1, f"{direction} {gap_pct*100:+.1f}% vs prior close"))

        if not triggers:
            return None

        # Strongest trigger drives reliability and dedup bucket
        top_tier = max(t[0] for t in triggers)
        if top_tier >= 4:
            reliability = 0.90
            top_bucket = "cross_or_200d"
        elif top_tier == 3:
            reliability = 0.87
            top_bucket = "52w_extreme"
        elif top_tier == 2:
            reliability = 0.83
            top_bucket = "50d_breach"
        else:
            reliability = 0.78
            top_bucket = "rsi_vol_gap"

        # Order labels by descending tier
        ordered = sorted(triggers, key=lambda x: -x[0])
        labels = [lbl for _, lbl in ordered]

        text = f"[TECH · {ticker}] {labels[0]}"
        if len(labels) > 1:
            text += " — " + "; ".join(labels[1:])
        text += f" (as of {last_date})"

        # ISO-week dedup so multiple intra-week wakes don't flood raw_items
        iso_year, iso_week, _ = datetime.fromisoformat(last_date).isocalendar()
        dedup_url = (
            f"https://finance.yahoo.com/quote/{ticker}/chart"
            f"?tech={top_bucket}&w={iso_year}W{iso_week:02d}"
        )

        return {
            "text": text[:450],
            "source": self.SOURCE,
            "url": dedup_url,
            "reliability": reliability,
        }


class TaiwanSemiRevenueAdapter:
    """
    Taiwan semiconductor monthly-revenue feed — supply-chain lead indicator for
    the AI-semi book (NVDA / AMD / AVGO / ARM / SMCI / ASML upstream demand).

    Investor edge:
      Listed Taiwanese semis are legally required to disclose net revenue by the
      10th of the following month — typically 3-6 weeks BEFORE the corresponding
      US-listed customer/peer reports its quarter. TSMC's monthly revenue is the
      hardest publicly-available leading indicator for the data-center capex
      cycle: when TSMC's wafer billings accelerate YoY, the H100/B200/MI300
      pull-through is already booked; when they decelerate, the AI-capex pause
      shows up here before it hits any Nvidia print. Quant funds buy this signal
      via Bloomberg or pay Taipei-based shops five figures/month for the same
      data; TWSE publishes it free in structured JSON.

    Source: TWSE OpenAPI `t187ap05_L` (Monthly Operating Revenue of TWSE-listed
    companies). One row per company per data-month with:
      - 資料年月 (data year-month, ROC fmt e.g. "11504" = 2026-04)
      - 出表日期 (publication date, ROC fmt)
      - 營業收入-當月營收 (current-month revenue, NTD thousands)
      - 營業收入-上月比較增減(%) (MoM %)
      - 營業收入-去年同月增減(%) (YoY %)
      - 累計營業收入-前期比較增減(%) (YTD YoY %)

    Coverage (5 large-cap Taiwan semis with the tightest read on AI demand):
      2330 TSMC      — foundry leader, AI accelerator wafer billings
      2303 UMC       — mature-node foundry, broad-based semi demand proxy
      2454 MediaTek  — fabless, edge-AI / smartphone SoC demand
      3711 ASE       — OSAT (advanced packaging — CoWoS bottleneck visibility)
      2317 Hon Hai   — Foxconn, AI-server assembly (NVDA/SMCI/DELL customer)

    Emits one item per (ticker, data-month). Dedup key is the ROC year-month in
    the URL so daily re-runs after the monthly press date don't flood raw_items
    until the next month's release lands.

    Reliability 0.92 (TWSE official mandatory disclosure; same tier as sec_13dg
    and sec_registration — primary regulatory source with strict legal deadlines).
    """

    SOURCE = "tw_semi_revenue"
    RELIABILITY = 0.92
    ENDPOINT = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"

    # Code → (display name, peer/customer hook for triage)
    COMPANIES: dict[str, tuple[str, str]] = {
        "2330": ("TSMC",     "foundry · NVDA/AMD/AVGO/AAPL wafer demand"),
        "2303": ("UMC",      "mature-node foundry · broad semi demand proxy"),
        "2454": ("MediaTek", "fabless SoC · edge-AI / smartphone demand"),
        "3711": ("ASE",      "OSAT · CoWoS advanced packaging (NVDA bottleneck)"),
        "2317": ("Hon Hai",  "Foxconn · AI-server assembly (NVDA/SMCI/DELL)"),
    }

    def fetch(self) -> list[dict]:
        data = fetch_json(self.ENDPOINT, timeout=25)
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for row in data:
            code = (row.get("公司代號") or "").strip()
            meta = self.COMPANIES.get(code)
            if not meta:
                continue
            label, hook = meta
            item = self._build_item(code, label, hook, row)
            if item:
                out.append(item)
        return out

    @staticmethod
    def _roc_to_iso_month(roc_ym: str) -> tuple[int, int] | None:
        """Convert ROC year-month string '11504' → (2026, 4). Returns None on failure."""
        s = (roc_ym or "").strip()
        if len(s) < 5 or not s.isdigit():
            return None
        roc_year = int(s[:-2])
        month = int(s[-2:])
        if not (1 <= month <= 12):
            return None
        return roc_year + 1911, month

    @staticmethod
    def _fmt_revenue_ntd(rev_k_ntd: float) -> str:
        """Format thousands-NTD as 'NT$X.X bn' or 'NT$XXX m'. Negative values get a sign."""
        ntd = rev_k_ntd * 1_000.0
        if abs(ntd) >= 1e9:
            return f"NT${ntd/1e9:,.1f} bn"
        if abs(ntd) >= 1e6:
            return f"NT${ntd/1e6:,.0f} m"
        return f"NT${ntd:,.0f}"

    @staticmethod
    def _fmt_pct(p: float) -> str:
        return f"{p:+.1f}%"

    def _build_item(self, code: str, label: str, hook: str, row: dict) -> dict | None:
        ym = self._roc_to_iso_month(row.get("資料年月", ""))
        if not ym:
            return None
        year, month = ym
        try:
            rev = float(row.get("營業收入-當月營收") or 0)
        except (TypeError, ValueError):
            return None
        if rev <= 0:
            return None
        try:
            mom = float(row.get("營業收入-上月比較增減(%)") or 0)
        except (TypeError, ValueError):
            mom = 0.0
        try:
            yoy = float(row.get("營業收入-去年同月增減(%)") or 0)
        except (TypeError, ValueError):
            yoy = 0.0
        try:
            ytd_yoy = float(row.get("累計營業收入-前期比較增減(%)") or 0)
        except (TypeError, ValueError):
            ytd_yoy = 0.0

        month_label = f"{year}-{month:02d}"
        rev_disp = self._fmt_revenue_ntd(rev)

        # Direction tag for triage: a |YoY| >=15% is institutional-actionable.
        # |YoY| <5% is a non-event; intermediate is noted as steady.
        if yoy >= 15.0:
            tag = "accelerating"
        elif yoy <= -10.0:
            tag = "contracting"
        elif yoy >= 5.0:
            tag = "expanding"
        elif yoy <= -5.0:
            tag = "softening"
        else:
            tag = "steady"

        text = (
            f"[TWSE Monthly Revenue · {label}] {month_label}: {rev_disp} — "
            f"MoM {self._fmt_pct(mom)} / YoY {self._fmt_pct(yoy)} "
            f"(YTD YoY {self._fmt_pct(ytd_yoy)}, {tag}). {hook}."
        )

        # Stable dedup per (ticker, ROC year-month). The MOPS company-page URL
        # is the canonical public artefact for TWSE-listed monthly revenue.
        dedup_url = (
            f"https://mops.twse.com.tw/mops/web/t05st10_ifrs"
            f"?co_id={code}&ym={year}{month:02d}"
        )

        return {
            "text": text[:450],
            "source": self.SOURCE,
            "url": dedup_url,
            "reliability": self.RELIABILITY,
        }


class EarningsTranscriptAdapter:
    """
    Earnings-call transcript feed — management tone, forward guidance and
    unexpected formulations from quarterly earnings conferences for watchlist
    tickers. STRATEGY.md tier-1 missing data source.

    Investor edge:
      The 8-K Item 2.02 press release carries the *number*; the conference call
      carries the *explanation*. Forward guidance ranges, capex plans,
      hyperscaler AI-spend colour and product-cycle commentary live in the
      prepared remarks and Q&A — not in the press release we already ingest.
      A miss on the press release with a confident, raised-guidance call is a
      buy; an in-line print with hedged guidance and capex pull-ins is a sell.
      Hedge funds buy this signal via Refinitiv StreetEvents, AlphaSense,
      S&P Cap-IQ or Bloomberg ERN <GO> (mid-five-figures/yr each); the
      Discounting Cash Flows public API publishes the same transcripts free
      with no key, 24-48h after the call.

    Source: `https://discountingcashflows.com/api/transcript/{ticker}/`
    returns a JSON array of {symbol, quarter, year, date, content} objects
    (most recent first). We take the latest entry per ticker and extract:
      - one forward-guidance sentence (highest signal: guide/outlook/expect
        bound to a quarter or fiscal year)
      - one AI-capex sentence (AI / data-center / GPU / compute spend)
      - a structured tag-line per item (raised | maintained | cut | n/a)

    Coverage: the high-conviction core of the watchlist. Pulling all 31 names
    every cycle would be feed noise — most call cycles are quarterly so the
    same content would repeat for weeks. Dedup key is the (ticker, year,
    quarter) so re-runs between earnings are idempotent.

    Reliability 0.86 — primary source (company management on the record), but
    the signal we emit is interpretive extraction from natural-language Q&A,
    not a parsed regulatory field. Sits just below sec_8k (0.95) and at par
    with eps_revisions (0.85) / tech_level (0.85).
    """

    SOURCE = "earnings_transcript"
    RELIABILITY = 0.86
    ENDPOINT_TEMPLATE = "https://discountingcashflows.com/api/transcript/{ticker}/"

    # High-conviction core (≈ S1 semis + S2 hyperscalers + S3 AI-software top names).
    # Restricting coverage keeps the feed signal-dense; Q1/Q2 transcripts repeat for
    # weeks otherwise. Off-list watchlist names still get 8-K and analyst signals.
    COVERAGE = (
        "NVDA", "AMD", "AVGO", "TSM", "ARM", "MU", "ANET",
        "MSFT", "GOOGL", "AMZN", "META", "AAPL",
        "PLTR", "ORCL", "NOW", "CRM", "SNOW",
    )

    # Forward-guidance signal: a verb pattern bound to a forward time window.
    # The intersection (verb + horizon) is what separates real guidance from
    # retrospective commentary ("we expected X last quarter" doesn't count).
    _GUIDE_VERBS = (
        "expect", "anticipate", "guide", "guidance", "outlook", "forecast",
        "project", "plan to", "intend to", "target", "see ", "look for",
    )
    _GUIDE_HORIZONS = (
        "next quarter", "this quarter", "second half", "first half",
        "fiscal year", "full year", "fy20", "fy 20", "next year",
        "q1", "q2", "q3", "q4",
    )

    # AI-capex sentinel: spend / capex / investment language bound to AI /
    # compute / data-center / GPU. This is the single most valuable colour
    # from a hyperscaler call for the AI-semi book.
    _AI_TERMS = (
        "ai ", " ai,", " ai.", " ai;", "artificial intelligence",
        "data center", "data-center", "gpu", "gpus", "h100", "h200", "b100",
        "b200", "mi300", "mi325", "blackwell", "hopper", "compute",
        "training", "inference", "accelerator",
    )
    _SPEND_TERMS = (
        "capex", "capital expenditure", "capital expenditures",
        "spend", "spending", "invest", "investment", "billion",
        "buildout", "build-out", "infrastructure",
    )

    # Tone tag derived from guidance verbs in proximity to direction words.
    # We do not pretend to do sentiment analysis here — this is a coarse
    # classification that lets the triage stage cluster items quickly.
    _RAISE_TERMS = ("raise", "raised", "raising", "increase", "increased",
                    "above", "higher", "stronger than", "ahead of")
    _CUT_TERMS = ("lower", "lowered", "lowering", "reduce", "reduced",
                  "below", "weaker than", "softer", "headwind", "cautious")

    def fetch(self) -> list[dict]:
        out: list[dict] = []
        for ticker in self.COVERAGE:
            item = self._fetch_one(ticker)
            if item:
                out.append(item)
        return out

    def _fetch_one(self, ticker: str) -> dict | None:
        url = self.ENDPOINT_TEMPLATE.format(ticker=ticker)
        data = fetch_json(url, headers=UA, timeout=20)
        if not data:
            return None
        # API returns either a list of transcripts or a single dict; normalize.
        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            return None
        # Pick the most recent entry (highest year, then highest quarter).
        latest = self._pick_latest(entries)
        if not latest:
            return None
        return self._build_item(ticker, latest)

    @staticmethod
    def _pick_latest(entries: list) -> dict | None:
        candidates: list[tuple[int, int, dict]] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                yr = int(e.get("year") or 0)
                qt = int(e.get("quarter") or 0)
            except (TypeError, ValueError):
                continue
            content = (e.get("content") or "").strip()
            if not content or yr <= 0 or qt <= 0:
                continue
            candidates.append((yr, qt, e))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return candidates[0][2]

    @classmethod
    def _split_sentences(cls, text: str) -> list[str]:
        # Cheap sentence split — transcripts are conversational, periods and
        # newlines are reliable boundaries. Keep sentences with real content
        # only (>=8 words) so we don't surface "Thanks, operator."
        raw = re.split(r"(?<=[.!?])\s+|\n+", text)
        return [s.strip() for s in raw if s and len(s.split()) >= 8]

    @classmethod
    def _find_forward_guidance(cls, sentences: list[str]) -> str | None:
        for s in sentences:
            low = s.lower()
            if any(v in low for v in cls._GUIDE_VERBS) and any(h in low for h in cls._GUIDE_HORIZONS):
                return s
        return None

    @classmethod
    def _find_ai_capex(cls, sentences: list[str]) -> str | None:
        for s in sentences:
            low = s.lower()
            if any(t in low for t in cls._AI_TERMS) and any(sp in low for sp in cls._SPEND_TERMS):
                return s
        return None

    @classmethod
    def _tone_tag(cls, guidance: str | None) -> str:
        if not guidance:
            return "n/a"
        low = guidance.lower()
        raise_hit = any(t in low for t in cls._RAISE_TERMS)
        cut_hit = any(t in low for t in cls._CUT_TERMS)
        if raise_hit and not cut_hit:
            return "raised"
        if cut_hit and not raise_hit:
            return "cut"
        if raise_hit and cut_hit:
            return "mixed"
        return "maintained"

    def _build_item(self, ticker: str, entry: dict) -> dict | None:
        content = (entry.get("content") or "").strip()
        try:
            year = int(entry.get("year"))
            quarter = int(entry.get("quarter"))
        except (TypeError, ValueError):
            return None
        if not content or year <= 0 or not (1 <= quarter <= 4):
            return None

        sentences = self._split_sentences(content)
        guidance = self._find_forward_guidance(sentences)
        ai_capex = self._find_ai_capex(sentences)

        # If neither signal is extractable, the transcript is still evidence
        # that the call occurred — surface a thin pointer rather than swallow
        # the event entirely. Triage can then go read it directly.
        tone = self._tone_tag(guidance)

        snippets: list[str] = []
        if guidance:
            snippets.append(f"Guide: \"{guidance[:240]}\"")
        if ai_capex:
            snippets.append(f"AI/capex: \"{ai_capex[:240]}\"")
        if not snippets:
            snippets.append("Transcript published; no forward-guidance/AI-capex sentence matched extractors.")

        text = (
            f"[Earnings Call · {ticker} Q{quarter} {year} · tone={tone}] "
            + " | ".join(snippets)
        )

        # Stable per-(ticker, year, quarter) dedup. The transcript URL on the
        # source site is the canonical public artefact.
        dedup_url = f"https://discountingcashflows.com/company/{ticker}/transcripts/{year}-Q{quarter}/"

        return {
            "text": text[:600],
            "source": self.SOURCE,
            "url": dedup_url,
            "reliability": self.RELIABILITY,
        }


class HyperscalerFinancialsAdapter:
    """
    Hyperscaler quarterly financial velocity via SEC EDGAR XBRL — capex,
    revenue, and operating-margin trajectory for MSFT / GOOGL / AMZN / META /
    ORCL, the 5 companies whose AI-infrastructure spending IS the entire
    NVDA / AMD / AVGO / ANET / VRT / SMCI demand thesis.

    Investor edge:
      Quant funds pay FactSet / Sentieo / Bloomberg five figures per year for
      structured XBRL trajectory series — the same numbers that appear in 10-Q
      financial tables, already normalized into time-series form so QoQ / YoY
      accelerations can be computed without re-parsing filing HTML each
      quarter. SEC publishes the same data via the
      `data.sec.gov/api/xbrl/companyconcept/...` endpoint — free, no API key,
      official primary source. NVDA has historically moved >10% on a single
      hyperscaler-capex print (MSFT FQ4-23 capex jump, META FY24 raise) — a
      structured velocity read on the 5 buyers of AI compute is the single
      strongest leading indicator we can build for the entire semi-infra book.
      Complements `earnings_transcript` (qualitative tone) with the hard
      numbers; complements `tw_semi_revenue` (upstream wafer demand) with the
      downstream buyer behaviour.

    What's emitted per company per quarter:
      One item with the most-recent reported quarter's discrete (3-month)
      capex, revenue, and operating margin, plus:
        - capex YoY % change (most direct AI-spend velocity read)
        - capex QoQ % change (recent inflection)
        - revenue YoY % growth
        - operating margin and YoY margin delta (capex absorption health)
      A direction tag (`accelerating` / `expanding` / `steady` /
      `decelerating` / `contracting`) summarizes the capex trajectory.

    Discrete-quarter filtering:
      XBRL returns BOTH YTD AND single-quarter values for the same period_end
      in 10-Qs (e.g. MSFT Q3-26 capex = 80.146B YTD AND 30.876B discrete). We
      keep ONLY discrete-quarter values where (end - start) ≤ 100 days, which
      drops YTD overlaps and 10-K full-year values and leaves one figure per
      reported quarter.

    Coverage choice:
      MSFT/GOOGL/AMZN/META are the 4 canonical "Big Cloud" hyperscalers. ORCL
      is included because its Stargate / OCI Gen2 capex commitment to OpenAI
      makes it a peer-level AI-infra buyer despite the smaller absolute size,
      and an outlier vs the Big-4 is itself a thesis signal (ORCL accelerating
      while MSFT decelerates = market-share shift). All 5 have stable, well-
      tagged XBRL with the same primary concepts; validated 2026-05-23
      against the live endpoint.

    Source: `https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/
      us-gaap/{Concept}.json`. Three concepts per company = 15 requests/cycle.
      0.15s sleep between calls keeps us well under SEC's 10 req/s cap.

    Reliability 0.95 — XBRL is the strictest tier we ingest: it IS the
    company's official 10-Q/10-K financial table after iXBRL re-tagging by
    EDGAR. Same primary tier as sec_13dg and sec_8k.
    """

    SOURCE = "hyperscaler_financials"
    RELIABILITY = 0.95
    ENDPOINT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{concept}.json"

    # ticker → (CIK, display name, triage hook)
    COMPANIES: dict[str, tuple[int, str, str]] = {
        "MSFT":  (789019,
                  "Microsoft",
                  "AI capex anchor · OpenAI/Copilot infra → NVDA/AMD/AVGO demand"),
        "GOOGL": (1652044,
                  "Alphabet",
                  "Google Cloud TPU + NVDA GPU capex · secondary AI-compute buyer"),
        "AMZN":  (1018724,
                  "Amazon",
                  "AWS Trainium/Inferentia + NVDA capacity build · largest hyperscaler"),
        "META":  (1326801,
                  "Meta",
                  "Llama infra buildout · GPU buyer #3 by capex velocity"),
        "ORCL":  (1341439,
                  "Oracle",
                  "OpenAI Stargate / OCI Gen2 · NVDA H100/B200 anchor customer"),
    }

    # Concept fallback chains. Filers don't all tag the same balance line under
    # the same us-gaap concept: AMZN tags capex as PaymentsToAcquireProductive-
    # Assets (broader productive-asset bucket including equipment-finance lease
    # additions); GOOGL/ORCL prefer the legacy `Revenues` tag over the post-606
    # `RevenueFromContractWithCustomer…`. Order = preferred first; first hit
    # with a non-empty series wins per company. Validated 2026-05-23.
    CAPEX_CONCEPTS = (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    )
    REVENUE_CONCEPTS = (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    )
    OPINC_CONCEPTS = ("OperatingIncomeLoss",)

    # Discrete-quarter window: 10-Q quarters are 89-94d; 10-K Q4 can be ~92d
    # (or 13/14 weeks for 52-/53-week filers). 100d safely separates discrete
    # quarter values from YTD overlaps (next category is ~180d for H1).
    QUARTER_MAX_DAYS = 100

    def fetch(self) -> list[dict]:
        out: list[dict] = []
        for tk, (cik, label, hook) in self.COMPANIES.items():
            try:
                item = self._build_company_item(tk, cik, label, hook)
            except Exception:
                # Adapter isolation: a single failing ticker (concept gone,
                # SEC 5xx) must not kill the rest of the run.
                continue
            if item:
                out.append(item)
        return out

    def _fetch_concept_quarterly(self, cik: int, concepts: tuple[str, ...]) -> list[dict]:
        """
        Try every concept in the fallback chain, return the series with the
        most recent period_end. Filers sometimes leave an older concept
        populated with stale historical data after migrating to a newer tag
        (e.g. GOOGL kept reporting `Revenues` after Q2-25 while leaving the
        `RevenueFromContractWithCustomer…` series frozen at Q1-25). "First
        non-empty" picks the stale one and silently emits prior-year numbers
        with `n/a` deltas; picking the freshest series fixes that without
        merging across incompatible scope definitions.

        Returns ascending-by-end list of discrete-quarter dicts. Each carries:
          end, start (date), val (float), fp (str), fy (int), form (str).
        """
        best: list[dict] = []
        best_end = None
        for concept in concepts:
            url = self.ENDPOINT.format(cik=cik, concept=concept)
            data = fetch_json(url, headers=UA, timeout=20)
            time.sleep(0.15)  # SEC rate limit: well under 10 req/s
            if not isinstance(data, dict):
                continue
            units = (data.get("units") or {}).get("USD") or []
            if not units:
                continue
            kept: list[dict] = []
            for u in units:
                try:
                    end = datetime.strptime(u["end"], "%Y-%m-%d").date()
                    start = datetime.strptime(u["start"], "%Y-%m-%d").date()
                except (KeyError, ValueError, TypeError):
                    continue
                if (end - start).days > self.QUARTER_MAX_DAYS:
                    continue  # YTD or full-year — drop
                try:
                    val = float(u.get("val") or 0)
                except (TypeError, ValueError):
                    continue
                kept.append({
                    "end": end,
                    "start": start,
                    "val": val,
                    "fp": u.get("fp", ""),
                    "fy": u.get("fy", 0),
                    "form": u.get("form", ""),
                })
            if not kept:
                continue
            # Last-write-wins per period_end (10-K/A restatements override
            # original 10-Q if same end repeats).
            dedup: dict = {}
            for r in kept:
                dedup[r["end"]] = r
            series = sorted(dedup.values(), key=lambda r: r["end"])
            latest_end = series[-1]["end"]
            if best_end is None or latest_end > best_end:
                best = series
                best_end = latest_end
        return best

    @staticmethod
    def _find_year_ago(series: list[dict], current_end) -> dict | None:
        """Closest entry whose end is within ±20d of (current_end - 1y).
        Handles 52/53-week filers (META, AMZN occasionally) and restated periods."""
        target = current_end - timedelta(days=365)
        best = None
        best_delta = 9999
        for r in series:
            d = abs((r["end"] - target).days)
            if d < best_delta and d <= 20:
                best, best_delta = r, d
        return best

    @staticmethod
    def _pct_change(curr: float, prior: float) -> float | None:
        if prior == 0 or prior is None:
            return None
        return (curr - prior) / abs(prior) * 100.0

    @staticmethod
    def _fmt_usd(v: float) -> str:
        if abs(v) >= 1e9:
            return f"${v/1e9:,.2f}B"
        if abs(v) >= 1e6:
            return f"${v/1e6:,.0f}M"
        return f"${v:,.0f}"

    @staticmethod
    def _fmt_pct(p: float | None) -> str:
        return "n/a" if p is None else f"{p:+.1f}%"

    @staticmethod
    def _capex_tag(yoy: float | None) -> str:
        if yoy is None:
            return "steady"
        if yoy >= 30.0:
            return "accelerating"
        if yoy >= 15.0:
            return "expanding"
        if yoy <= -15.0:
            return "contracting"
        if yoy <= -5.0:
            return "decelerating"
        return "steady"

    def _build_company_item(self, ticker: str, cik: int, label: str, hook: str) -> dict | None:
        capex_s = self._fetch_concept_quarterly(cik, self.CAPEX_CONCEPTS)
        rev_s = self._fetch_concept_quarterly(cik, self.REVENUE_CONCEPTS)
        opinc_s = self._fetch_concept_quarterly(cik, self.OPINC_CONCEPTS)
        if not capex_s or not rev_s:
            return None

        # Anchor on the most recent capex report (alpha-bearing variable for
        # the AI-infra thesis). Revenue / opinc are matched to that period.
        latest = capex_s[-1]
        end_d = latest["end"]
        capex_curr = latest["val"]

        capex_prior = self._find_year_ago(capex_s, end_d)
        capex_yoy = self._pct_change(capex_curr, capex_prior["val"]) if capex_prior else None

        # QoQ: closest prior discrete quarter (60-120d back).
        prior_q = None
        for r in reversed(capex_s[:-1]):
            gap = (end_d - r["end"]).days
            if 60 <= gap <= 120:
                prior_q = r
                break
        capex_qoq = self._pct_change(capex_curr, prior_q["val"]) if prior_q else None

        # Revenue at same end (or ±20d).
        rev_curr_r = next((r for r in rev_s if r["end"] == end_d), None)
        if rev_curr_r is None:
            rev_curr_r = min(
                (r for r in rev_s if abs((r["end"] - end_d).days) <= 20),
                key=lambda r: abs((r["end"] - end_d).days),
                default=None,
            )
        rev_yoy_pct: float | None = None
        rev_curr_val: float | None = None
        if rev_curr_r is not None:
            rev_curr_val = rev_curr_r["val"]
            rev_prior = self._find_year_ago(rev_s, rev_curr_r["end"])
            if rev_prior:
                rev_yoy_pct = self._pct_change(rev_curr_val, rev_prior["val"])

        # Operating margin (current + YoY delta in pp).
        margin_curr: float | None = None
        margin_delta_pp: float | None = None
        opinc_curr_r = next((r for r in opinc_s if r["end"] == end_d), None)
        if opinc_curr_r is None:
            opinc_curr_r = min(
                (r for r in opinc_s if abs((r["end"] - end_d).days) <= 20),
                key=lambda r: abs((r["end"] - end_d).days),
                default=None,
            )
        if opinc_curr_r and rev_curr_r and rev_curr_r["val"]:
            margin_curr = opinc_curr_r["val"] / rev_curr_r["val"] * 100.0
            opinc_prior = self._find_year_ago(opinc_s, opinc_curr_r["end"])
            rev_prior_for_margin = self._find_year_ago(rev_s, rev_curr_r["end"])
            if opinc_prior and rev_prior_for_margin and rev_prior_for_margin["val"]:
                margin_prior = opinc_prior["val"] / rev_prior_for_margin["val"] * 100.0
                margin_delta_pp = margin_curr - margin_prior

        fp = latest.get("fp", "") or "Q?"
        fy = latest.get("fy", 0) or end_d.year
        period_label = f"{fp} FY{fy}" if fp and fy else end_d.isoformat()

        tag = self._capex_tag(capex_yoy)
        capex_disp = self._fmt_usd(capex_curr)
        rev_disp = self._fmt_usd(rev_curr_val) if rev_curr_val else "n/a"

        margin_part = (
            f"OpM {margin_curr:.1f}% ({self._fmt_pct(margin_delta_pp)} YoY pp)"
            if margin_curr is not None
            else "OpM n/a"
        )

        text = (
            f"[Hyperscaler XBRL · {label}] {period_label} (end {end_d.isoformat()}): "
            f"capex {capex_disp} — YoY {self._fmt_pct(capex_yoy)} / "
            f"QoQ {self._fmt_pct(capex_qoq)} ({tag}); "
            f"rev {rev_disp} YoY {self._fmt_pct(rev_yoy_pct)}; "
            f"{margin_part}. {hook}."
        )

        # Stable per-(ticker, period_end) dedup. SEC submissions page is the
        # canonical artefact; period suffix makes it unique per reporting period
        # without depending on accession numbers (not exposed at this endpoint).
        dedup_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik:010d}&type=10-Q&dateb=&owner=include&count=40"
            f"#xbrl-fin-{end_d.isoformat()}"
        )

        return {
            "text": text[:450],
            "source": self.SOURCE,
            "url": dedup_url,
            "reliability": self.RELIABILITY,
        }
