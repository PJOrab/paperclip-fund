"""
Pipeline unit-tests (stdlib-only, no DB/network).

Covers regressions fixed in recent cycles:
  - _parse_json: fence-strip with trailing explanation text (cycle 52)
  - _cross_check_devil_conviction: warning logic for reject/caution vs. conviction (cycle 50)

Run:  python3 -m agents.test_pipeline   (from repo root)
"""
import io
import sys

from .claude_cli import _parse_json, ClaudeError


def _check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"ok: {name}")


# ---------------------------------------------------------------------------
# _parse_json tests
# ---------------------------------------------------------------------------

def test_parse_json() -> None:
    # 1. Plain JSON — no fence.
    result = _parse_json('{"a": 1}')
    _check("plain JSON dict", result == {"a": 1})

    # 2. Fenced JSON, no trailing text.
    result = _parse_json('```json\n{"b": 2}\n```')
    _check("fenced JSON, no trailing text", result == {"b": 2})

    # 3. Fenced JSON WITH trailing explanation (the cycle-52 regression).
    payload = '```json\n{"c": 3}\n```\nHere is my explanation.'
    result = _parse_json(payload)
    _check("fenced JSON with trailing explanation", result == {"c": 3})

    # 4. Fenced array.
    result = _parse_json('```\n[1, 2, 3]\n```')
    _check("fenced JSON array", result == [1, 2, 3])

    # 5. Fallback brace-extraction when there is surrounding prose.
    result = _parse_json('Some text {"x": 99} more text')
    _check("brace-extraction fallback", result == {"x": 99})

    # 6. Truly unparseable — must raise ClaudeError.
    raised = False
    try:
        _parse_json("not json at all")
    except ClaudeError:
        raised = True
    _check("unparseable raises ClaudeError", raised)


# ---------------------------------------------------------------------------
# _cross_check_devil_conviction tests
# ---------------------------------------------------------------------------
# Import and test the function by capturing log output.

def _capture_cross_check(theses: list[dict], critiques: list[dict]) -> str:
    """Run _cross_check_devil_conviction and return concatenated log lines."""
    # We patch _log by importing from run and replacing it temporarily.
    import agents.run as run_mod
    captured: list[str] = []
    original = run_mod._log
    run_mod._log = captured.append  # type: ignore[assignment]
    try:
        run_mod._cross_check_devil_conviction(theses, critiques)
    finally:
        run_mod._log = original
    return "\n".join(captured)


def test_cross_check() -> None:
    theses = [
        {"id": "T1", "conviction": 0.60},
        {"id": "T2", "conviction": 0.70},
        {"id": "T3", "conviction": 0.35},
        {"id": "T4", "conviction": 0.50},
    ]
    critiques = [
        {"id": "T1", "verdict": "reject"},    # reject + 0.60 > 0.40 → warning
        {"id": "T2", "verdict": "caution"},   # caution + 0.70 > 0.55 → warning
        {"id": "T3", "verdict": "reject"},    # reject + 0.35 ≤ 0.40 → no warning
        {"id": "T4", "verdict": "agree"},     # agree → no warning
    ]

    log = _capture_cross_check(theses, critiques)

    _check("reject+high conviction triggers warning", "T1" in log and "reject" in log)
    _check("caution+high conviction triggers warning", "T2" in log and "caution" in log)
    _check("reject+low conviction is silent", "T3" not in log)
    _check("agree is always silent", "T4" not in log)

    # Missing critique — no crash, no spurious warning.
    log2 = _capture_cross_check([{"id": "TX", "conviction": 0.80}], [])
    _check("missing critique is silent", "TX" not in log2)


# ---------------------------------------------------------------------------
# classify_item tests (coverage_qc.py big-event heuristics)
# ---------------------------------------------------------------------------

def test_classify_item() -> None:
    from agents.coverage_qc import classify_item

    def labels(text: str) -> set[str]:
        return {label for label, _prio in classify_item(text)}

    # IPO / S-1
    _check("IPO keyword matches IPO/S-1/listing",
           "IPO/S-1/listing" in labels("Acme Inc. files S-1 for IPO on Nasdaq"))
    _check("direct listing matches IPO/S-1/listing",
           "IPO/S-1/listing" in labels("Stripe announces direct listing next month"))

    # Funding
    _check("Series B matches funding",
           "funding" in labels("Startup raises Series B of $200M at $1B valuation"))
    _check("raises $4B matches funding",
           "funding" in labels("Anthropic raises $4B from Google"))
    _check("raises $400M matches funding",
           "funding" in labels("xAI raises $400M in Series C"))

    # M&A
    _check("acquires matches M&A",
           "M&A" in labels("Microsoft acquires Inflection AI for $650M"))
    _check("definitive agreement matches M&A",
           "M&A" in labels("Companies sign definitive agreement for merger"))

    # Regulatory
    _check("FTC matches regulatory",
           "regulatory" in labels("FTC opens antitrust probe into OpenAI"))
    _check("SEC charges matches regulatory",
           "regulatory" in labels("SEC charges Binance with securities violations"))

    # Launch (product + launch verb)
    _check("launches model matches launch",
           "launch" in labels("Anthropic launches Claude 4 model with new capabilities"))

    # Insider trade
    _check("Form 4 matches insider_trade",
           "insider_trade" in labels("Form 4: CEO open market purchase of 50k shares"))

    # Earnings surprise
    _check("beats estimates matches earnings_surprise",
           "earnings_surprise" in labels("NVDA beats estimates by 15% on data center revenue"))
    _check("guidance raised matches earnings_surprise",
           "earnings_surprise" in labels("Apple guidance raised for Q3 on strong iPhone sales"))

    # analyst_action (from Zyklus 38)
    _check("upgrades to Buy matches analyst_action",
           "analyst_action" in labels("Goldman Sachs upgrades NVDA to Buy, raises price target to $180"))
    _check("raises price target matches analyst_action",
           "analyst_action" in labels("JPMorgan raises price target for MSFT to $450"))

    # exec_change (from Zyklus 38)
    _check("CEO resigns matches exec_change",
           "exec_change" in labels("Intel CEO Pat Gelsinger resigns after disappointing earnings"))
    _check("CFO appointed matches exec_change",
           "exec_change" in labels("ASML appoints new CFO effective Q2 2026"))

    # quarterly_results (new Zyklus 42)
    _check("Q1 revenue matches quarterly_results",
           "quarterly_results" in labels(
               "TSMC Reports Q1 2026 Revenue of NT$839.25 Billion, Up 41.6% Year-Over-Year"))
    _check("reports quarterly earnings matches quarterly_results",
           "quarterly_results" in labels(
               "ASML reports quarterly earnings: net sales €7.7B, EPS €18.96"))
    _check("fourth quarter results matches quarterly_results",
           "quarterly_results" in labels(
               "ARM Holdings fourth quarter results: revenue $1.24B, up 34% YoY"))
    _check("Q4 EPS matches quarterly_results",
           "quarterly_results" in labels(
               "[EDGAR 6-K Foreign Issuer Report] TSM: Q4 2025 EPS beat (filed 2026-01-16)"))

    # foreign_filing (new Zyklus 42)
    _check("6-K matches foreign_filing",
           "foreign_filing" in labels(
               "[EDGAR 6-K Foreign Issuer Report] ASML Holding N.V.: plans acquisition (filed 2026-04-23)"))
    _check("20-F matches foreign_filing",
           "foreign_filing" in labels(
               "[EDGAR 20-F Foreign Annual Report] TSM Taiwan: annual revenue FY2025 (filed 2026-02-20)"))
    _check("domestic 8-K does not match foreign_filing",
           "foreign_filing" not in labels(
               "[EDGAR 8-K:Earnings Results] NVDA NVIDIA Corp: Q1 results (filed 2026-05-20)"))

    # Buyback patterns (added HED-117 cycle 49)
    _check("share repurchase matches buyback",
           "buyback" in labels("Apple authorizes new $90 billion share repurchase program"))
    _check("stock buyback program matches buyback",
           "buyback" in labels("Microsoft board approves $60B stock repurchase program"))

    # Dividend patterns (added HED-117 cycle 49)
    _check("special dividend matches dividend",
           "dividend" in labels("NVDA declares special dividend of $0.10 per share"))
    _check("dividend increase matches dividend",
           "dividend" in labels("Broadcom increases its quarterly dividend by 14%"))

    # Clean text — no matches (analyst_action now detected; use truly routine news)
    _check("routine news has no big-event match",
           classify_item("Apple store opens in new mall location next quarter") == [])

    # S5 Energy/Power sector patterns (energy_power_deal — added HED-127 cycle 7)
    _check("PPA matches energy_power_deal",
           "energy_power_deal" in labels(
               "Microsoft signs 20-year power purchase agreement with Constellation Energy"))
    _check("nuclear deal matches energy_power_deal",
           "energy_power_deal" in labels(
               "Google signs nuclear energy deal with Vistra for 1.2GW of carbon-free power"))
    _check("SMR matches energy_power_deal",
           "energy_power_deal" in labels(
               "GE Vernova partners with AWS on small modular reactor development"))
    _check("transformer order matches energy_power_deal",
           "energy_power_deal" in labels(
               "Eaton reports record transformer order backlog driven by data center demand"))
    _check("data centre power matches energy_power_deal",
           "energy_power_deal" in labels(
               "Meta signs 3GW data centre power agreement with grid operator"))
    _check("grid infrastructure matches energy_power_deal",
           "energy_power_deal" in labels(
               "GE Vernova wins $2B grid infrastructure contract for AI build-out"))

    # capex_announcement (new pattern)
    _check("$100B capex matches capex_announcement",
           "capex_announcement" in labels(
               "Microsoft announces $100 billion capex plan for AI infrastructure through 2028"))
    _check("datacenter investment matches capex_announcement",
           "capex_announcement" in labels(
               "Google raises datacenter investment to $75bn for 2026"))
    _check("AI infrastructure spending matches capex_announcement",
           "capex_announcement" in labels(
               "Amazon Web Services AI infrastructure spending rises 40% YoY in Q1"))

    # improved launch patterns (previews, drops, open-sources, reasoning)
    _check("previews model matches launch",
           "launch" in labels(
               "Anthropic previews Claude 4 with extended reasoning capabilities"))
    _check("open-sources weights matches launch",
           "launch" in labels(
               "Meta open-sources Llama 4 model weights — 400B parameters"))

    print("ok: classify_item coverage_qc patterns (including capex + improved launch)")



# ---------------------------------------------------------------------------
# validate_output schema tests
# ---------------------------------------------------------------------------

def test_validate_output() -> None:
    from fund_skills.validate_output import validate

    # --- triage ---
    good_triage = {"clusters": [
        {"title": "NVDA earnings", "tickers": ["NVDA"], "category": "earnings",
         "why": "beat estimates", "importance": 5, "item_refs": [0]}
    ]}
    _check("triage valid cluster passes", validate("triage", good_triage) == [])

    missing_field = {"clusters": [{"title": "x", "tickers": [], "category": "earnings", "why": "y"}]}
    errs = validate("triage", missing_field)
    _check("triage missing importance caught", any("importance" in e for e in errs))

    bad_cat = {"clusters": [{"title": "x", "tickers": [], "category": "BADCAT", "why": "y", "importance": 3}]}
    errs = validate("triage", bad_cat)
    _check("triage bad category caught", any("category" in e for e in errs))

    bad_imp = {"clusters": [{"title": "x", "tickers": [], "category": "earnings", "why": "y", "importance": 6}]}
    errs = validate("triage", bad_imp)
    _check("triage importance > 5 caught", any("importance" in e for e in errs))

    empty_title = {"clusters": [{"title": "", "tickers": [], "category": "earnings", "why": "beat estimates", "importance": 3, "item_refs": []}]}
    errs = validate("triage", empty_title)
    _check("triage empty title caught", any("title" in e for e in errs))

    empty_why = {"clusters": [{"title": "NVDA earnings", "tickers": [], "category": "earnings", "why": "", "importance": 3, "item_refs": []}]}
    errs = validate("triage", empty_why)
    _check("triage empty why caught", any("why" in e for e in errs))

    bad_tickers_type = {"clusters": [{"title": "NVDA", "tickers": "NVDA", "category": "earnings", "why": "beat", "importance": 3, "item_refs": []}]}
    errs = validate("triage", bad_tickers_type)
    _check("triage tickers non-list caught", any("tickers" in e for e in errs))

    # --- analyst ---
    good_analyst = {"analyses": [
        {"title": "NVDA", "tickers": ["NVDA"], "read": "bullish", "magnitude": "high",
         "horizon": "days", "key_facts": ["beat"], "key_uncertainty": "macro",
         "consensus_view": "differentiated", "differentiation": "non-consensus"}
    ]}
    _check("analyst valid analysis passes", validate("analyst", good_analyst) == [])

    bad_horizon = {"analyses": [
        {"title": "x", "tickers": [], "read": "bullish", "magnitude": "low",
         "horizon": "months",  # invalid
         "key_facts": ["some fact"], "key_uncertainty": "x",
         "consensus_view": "aligned", "differentiation": ""}
    ]}
    errs = validate("analyst", bad_horizon)
    _check("analyst bad horizon caught", any("horizon" in e for e in errs))

    bad_read = {"analyses": [
        {"title": "x", "tickers": [], "read": "neutral",  # invalid
         "magnitude": "low", "horizon": "weeks",
         "key_facts": ["some fact"], "key_uncertainty": "x",
         "consensus_view": "aligned", "differentiation": ""}
    ]}
    errs = validate("analyst", bad_read)
    _check("analyst bad read caught", any("read" in e for e in errs))

    empty_key_facts = {"analyses": [
        {"title": "x", "tickers": [], "read": "bullish", "magnitude": "low",
         "horizon": "weeks", "key_facts": [], "key_uncertainty": "x",
         "consensus_view": "aligned", "differentiation": ""}
    ]}
    errs = validate("analyst", empty_key_facts)
    _check("analyst empty key_facts caught", any("key_facts" in e for e in errs))

    empty_uncertainty = {"analyses": [
        {"title": "x", "tickers": [], "read": "bullish", "magnitude": "low",
         "horizon": "weeks", "key_facts": ["fact"], "key_uncertainty": "",
         "consensus_view": "aligned", "differentiation": ""}
    ]}
    errs = validate("analyst", empty_uncertainty)
    _check("analyst empty key_uncertainty caught", any("key_uncertainty" in e for e in errs))

    empty_differentiation = {"analyses": [
        {"title": "x", "tickers": [], "read": "bullish", "magnitude": "low",
         "horizon": "weeks", "key_facts": ["fact"], "key_uncertainty": "risk",
         "consensus_view": "differentiated", "differentiation": ""}  # empty when differentiated
    ]}
    errs = validate("analyst", empty_differentiation)
    _check("analyst empty differentiation when consensus_view=differentiated caught",
           any("differentiation" in e for e in errs))

    # --- thesis ---
    good_thesis = {"theses": [
        {"id": "nvda-long", "tickers": ["NVDA"], "direction": "long",
         "thesis": "NVDA dominates", "bull_case": ["capex"], "bear_case": ["macro"],
         "catalysts": ["earnings Q3"], "horizon": "weeks",
         "conviction": 0.55, "is_differentiated": True}
    ]}
    _check("thesis valid passes", validate("thesis", good_thesis) == [])

    below_floor = {"theses": [
        {"id": "x", "tickers": [], "direction": "long", "thesis": "t",
         "bull_case": ["upside"], "bear_case": ["risk"], "catalysts": ["cat1"], "horizon": "weeks",
         "conviction": 0.30,  # below 0.40 floor
         "is_differentiated": False}
    ]}
    errs = validate("thesis", below_floor)
    _check("thesis conviction below floor caught", any("0.40" in e or "floor" in e for e in errs))

    non_bool_diff = {"theses": [
        {"id": "x", "tickers": [], "direction": "long", "thesis": "t",
         "bull_case": ["upside"], "bear_case": ["risk"], "catalysts": ["cat1"], "horizon": "weeks",
         "conviction": 0.50, "is_differentiated": "yes"}  # string, not bool
    ]}
    errs = validate("thesis", non_bool_diff)
    _check("thesis is_differentiated non-bool caught", any("is_differentiated" in e for e in errs))

    empty_catalysts = {"theses": [
        {"id": "x", "tickers": [], "direction": "long", "thesis": "t",
         "bull_case": ["upside"], "bear_case": ["risk"], "catalysts": [], "horizon": "weeks",
         "conviction": 0.50, "is_differentiated": False}
    ]}
    errs = validate("thesis", empty_catalysts)
    _check("thesis empty catalysts caught", any("catalysts" in e for e in errs))

    empty_bull_case = {"theses": [
        {"id": "x", "tickers": [], "direction": "long", "thesis": "t",
         "bull_case": [], "bear_case": ["risk"], "catalysts": ["cat1"], "horizon": "weeks",
         "conviction": 0.50, "is_differentiated": False}
    ]}
    errs = validate("thesis", empty_bull_case)
    _check("thesis empty bull_case caught", any("bull_case" in e for e in errs))

    empty_bear_case = {"theses": [
        {"id": "x", "tickers": [], "direction": "long", "thesis": "t",
         "bull_case": ["upside"], "bear_case": [], "catalysts": ["cat1"], "horizon": "weeks",
         "conviction": 0.50, "is_differentiated": False}
    ]}
    errs = validate("thesis", empty_bear_case)
    _check("thesis empty bear_case caught", any("bear_case" in e for e in errs))

    # --- devil ---
    good_devil = {"critiques": [
        {"id": "nvda-long", "strongest_counter": "macro", "already_priced_in": "no",
         "falsification": ["Q3 DC revenue misses $28bn"], "blind_spot": "china", "verdict": "caution"}
    ]}
    _check("devil valid critique passes", validate("devil", good_devil) == [])

    empty_falsification = {"critiques": [
        {"id": "x", "strongest_counter": "s", "already_priced_in": "n",
         "falsification": [],  # empty list
         "blind_spot": "b", "verdict": "agree"}
    ]}
    errs = validate("devil", empty_falsification)
    _check("devil empty falsification caught", any("falsification" in e for e in errs))

    bad_verdict = {"critiques": [
        {"id": "x", "strongest_counter": "s", "already_priced_in": "n",
         "falsification": ["event X"], "blind_spot": "b", "verdict": "maybe"}  # invalid
    ]}
    errs = validate("devil", bad_verdict)
    _check("devil bad verdict caught", any("verdict" in e for e in errs))


# ---------------------------------------------------------------------------

def test_watchlist_sync() -> None:
    from ingestion.test_watchlist_sync import main as _wl_main
    _wl_main()


def test_dedup() -> None:
    from ingestion.test_dedup import main as _dedup_main
    _dedup_main()


def test_triage_user_prompt() -> None:
    """triage_user() must embed age= tag and age-guidance text."""
    from datetime import datetime, timezone, timedelta
    from agents.prompts import triage_user

    now = datetime.now(timezone.utc)
    items = [
        {"source": "reuters", "reliability": 0.90, "text": "NVDA beat earnings",
         "fetched_at": (now - timedelta(hours=2)).isoformat()},
        {"source": "techcrunch", "reliability": 0.70, "text": "Old recap article",
         "fetched_at": (now - timedelta(hours=22)).isoformat()},
        {"source": "unknown_src", "reliability": None, "text": "No timestamp item"},
    ]
    prompt = triage_user(items, max_clusters=6)
    _check("triage_user includes age=2h for fresh item", "age=2h" in prompt)
    _check("triage_user includes age=22h for stale item", "age=22h" in prompt)
    _check("triage_user handles missing fetched_at gracefully", "unknown_src" in prompt)
    _check("triage_user includes age-guidance text", "age<4h" in prompt)
    print("ok: triage_user age tags correct")


def test_guidance_extraction() -> None:
    """_extract_8k_text must append [OUTLOOK: ...] for item 2.02 with guidance section."""
    from ingestion.sources_aitech import _extract_guidance_snippet

    # Typical NVDA-style earnings release
    fake_html = """<html><body>
    <p>Item 2.02 Results of Operations. NVIDIA Corporation reported Q1 FY2026 revenue of
    $44.1 billion, up 69% year on year. Diluted EPS was $0.96.</p>
    <p>Financial Outlook: For the second quarter of fiscal 2026, NVIDIA expects revenue of
    approximately $45 billion, plus or minus 2 percent.</p>
    </body></html>"""

    from ingestion.sources_aitech import _extract_8k_text
    snippet, item_num, label = _extract_8k_text(fake_html, max_chars=400)
    _check("8k extract returns item 2.02", item_num == "2.02")
    _check("8k extract appends OUTLOOK tag for earnings", "[OUTLOOK:" in snippet)
    _check("8k extract guidance contains revenue figure", "$45 billion" in snippet)

    # Non-earnings 8-K (item 8.01) should NOT have OUTLOOK tag
    non_earnings = """<html><body>
    <p>Item 8.01 Other Events. Company announces strategic partnership with XYZ Corp.</p>
    </body></html>"""
    s2, i2, _ = _extract_8k_text(non_earnings, max_chars=400)
    _check("non-earnings 8-K does not get OUTLOOK tag", "[OUTLOOK:" not in s2)

    # guidance_snippet returns empty when no guidance section present
    plain = "Revenue was $10B. Net income $2B. No forward guidance here."
    _check("_extract_guidance_snippet returns empty when absent",
           _extract_guidance_snippet(plain) == "")
    print("ok: guidance extraction correct")


def main() -> None:
    test_parse_json()
    test_cross_check()
    test_classify_item()
    test_validate_output()
    test_watchlist_sync()
    test_dedup()
    test_triage_user_prompt()
    test_guidance_extraction()
    print("\nALL PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
