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


def _extract_8k_text(html_src: str, max_chars: int = 400) -> tuple[str, str, str]:
    """
    Extract the first substantive Item paragraph from an 8-K HTML document.
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
        return snippet, item_num, label
    except Exception:
        return "", "", ""


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
                item_num = ""
                item_type = ""
                if form == "8-K" and acc and doc:
                    # Fetch the primary 8-K HTML, extract the first Item paragraph,
                    # classify the Item type, and determine a per-item reliability
                    # override so triage ranks earnings/acquisition 8-Ks above
                    # boilerplate exhibit attachments (Item 9.01) or Reg-FD slides.
                    # Falls back to the metadata description on any fetch/parse error.
                    try:
                        html_src = m.fetch_url(doc_url, headers=UA, timeout=20)
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
                        html_src = m.fetch_url(doc_url, headers=UA, timeout=30)
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
                    # 6-K filings from foreign issuers (TSM, ASML, ARM) are HTML
                    # press releases or quarterly summaries. Reuse 8-K text extraction
                    # which finds the first substantive paragraph. Falls back on error.
                    try:
                        html_src = m.fetch_url(doc_url, headers=UA, timeout=20)
                        time.sleep(0.15)  # SEC: max 10 req/s
                        if html_src:
                            snippet, _, _ = _extract_8k_text(html_src)
                            if not snippet:
                                # 6-Ks often lack Item headers — grab first non-trivial paragraph
                                txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_src)
                                txt = re.sub(r"<[^>]+>", " ", txt)
                                txt = _HTML_ENTITY_RE.sub(" ", txt)
                                txt = re.sub(r"\s+", " ", txt).strip()
                                if len(txt) > 50:
                                    snippet = txt[:400]
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
        xml = m.fetch_url(self.BASE, headers=self.HEADERS, timeout=20)
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
                text = m.fetch_url(feed, timeout=15)
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
                    desc = _rss_desc(block)
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
                text = m.fetch_url(feed, timeout=20)
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
                text = m.fetch_url(feed_url, timeout=15)
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
                text = m.fetch_url(feed_url, timeout=15)
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
                text = m.fetch_url(self.BASE.format(sid=sid), headers=UA, timeout=15)
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
