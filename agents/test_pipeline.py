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

    # Clean text — no matches
    _check("routine news has no big-event match",
           classify_item("Analyst reiterates Hold rating on AAPL") == [])


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
         "key_facts": [], "key_uncertainty": "x",
         "consensus_view": "aligned", "differentiation": ""}
    ]}
    errs = validate("analyst", bad_horizon)
    _check("analyst bad horizon caught", any("horizon" in e for e in errs))

    bad_read = {"analyses": [
        {"title": "x", "tickers": [], "read": "neutral",  # invalid
         "magnitude": "low", "horizon": "weeks",
         "key_facts": [], "key_uncertainty": "x",
         "consensus_view": "aligned", "differentiation": ""}
    ]}
    errs = validate("analyst", bad_read)
    _check("analyst bad read caught", any("read" in e for e in errs))

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
         "bull_case": [], "bear_case": [], "catalysts": [], "horizon": "weeks",
         "conviction": 0.30,  # below 0.40 floor
         "is_differentiated": False}
    ]}
    errs = validate("thesis", below_floor)
    _check("thesis conviction below floor caught", any("0.40" in e or "floor" in e for e in errs))

    non_bool_diff = {"theses": [
        {"id": "x", "tickers": [], "direction": "long", "thesis": "t",
         "bull_case": [], "bear_case": [], "catalysts": [], "horizon": "weeks",
         "conviction": 0.50, "is_differentiated": "yes"}  # string, not bool
    ]}
    errs = validate("thesis", non_bool_diff)
    _check("thesis is_differentiated non-bool caught", any("is_differentiated" in e for e in errs))

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


def main() -> None:
    test_parse_json()
    test_cross_check()
    test_classify_item()
    test_validate_output()
    test_watchlist_sync()
    print("\nALL PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
