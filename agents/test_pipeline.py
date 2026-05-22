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

def main() -> None:
    test_parse_json()
    test_cross_check()
    test_classify_item()
    print("\nALL PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
