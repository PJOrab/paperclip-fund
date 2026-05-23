"""
Unit tests for EarningsTranscriptAdapter. Pure-fixture based, no network.

Locks the Discounting Cash Flows transcript API shape: list of objects with
keys {symbol, quarter, year, date, content}; content is free-text English with
mixed prepared-remarks and Q&A. The adapter must:
  - pick the most recent (year, quarter) per ticker,
  - emit one item per ticker with a stable dedup URL,
  - extract a forward-guidance sentence and (if present) an AI/capex sentence,
  - tag the tone as raised | maintained | cut | mixed | n/a,
  - degrade silently when the endpoint returns no data or a non-list payload.
"""
from unittest.mock import patch

from ingestion.sources_aitech import EarningsTranscriptAdapter


def _entry(symbol: str, year: int, quarter: int, content: str,
           date: str = "2026-04-25") -> dict:
    return {
        "symbol": symbol,
        "year": year,
        "quarter": quarter,
        "date": date,
        "content": content,
    }


# Realistic transcript excerpt: prepared remarks with a guidance sentence and
# an AI-capex sentence, both of which the extractors must surface.
_NVDA_CONTENT = (
    "Operator: Welcome to the call.\n"
    "Jensen Huang: Thanks operator. We had a strong quarter driven by "
    "data center revenue of $26 billion, up 154% year over year.\n"
    "Colette Kress: For the next quarter, we expect revenue to be approximately "
    "$28 billion plus or minus two percent, ahead of consensus and reflecting "
    "continued Blackwell ramp into hyperscale customers.\n"
    "Colette Kress: We expect to invest billions in AI infrastructure and data "
    "center capacity over the fiscal year to support customer demand for "
    "training and inference workloads.\n"
    "Q&A operator: First question from Morgan Stanley.\n"
    "Analyst: Could you talk about supply constraints? Thanks.\n"
    "Jensen Huang: Yes, we are working closely with our foundry partners.\n"
)

_MSFT_CONTENT_CUT = (
    "Satya Nadella: Azure grew 31% constant currency. Copilot adoption is broad.\n"
    "Amy Hood: For the second half of fiscal year, we now expect Azure growth "
    "to moderate, weaker than we previously guided as capacity constraints "
    "push some workloads into the next year.\n"
    "Amy Hood: Capex will remain elevated as we invest in data center buildout "
    "to support AI compute demand from OpenAI and enterprise customers.\n"
)

_AAPL_CONTENT_NO_SIGNAL = (
    "Tim Cook: We had a great quarter. iPhone revenue was strong.\n"
    "Operator: Thanks for joining.\n"
    "Luca Maestri: Services hit an all-time high.\n"
)


# ---------------------------------------------------------------------------
# _pick_latest — chronological ordering
# ---------------------------------------------------------------------------

def test_pick_latest_prefers_highest_year_then_quarter():
    entries = [
        _entry("NVDA", 2025, 4, "old"),
        _entry("NVDA", 2026, 1, "newer"),
        _entry("NVDA", 2026, 2, "newest"),
        _entry("NVDA", 2026, 3, "newest3"),
    ]
    pick = EarningsTranscriptAdapter._pick_latest(entries)
    assert pick is not None
    assert pick["quarter"] == 3
    assert pick["year"] == 2026


def test_pick_latest_drops_empty_content():
    entries = [
        _entry("NVDA", 2026, 2, ""),  # empty content disqualifies
        _entry("NVDA", 2026, 1, "real content here that is long enough"),
    ]
    pick = EarningsTranscriptAdapter._pick_latest(entries)
    assert pick is not None
    assert pick["quarter"] == 1


def test_pick_latest_handles_garbage_year_quarter():
    entries = [
        {"symbol": "NVDA", "year": "bad", "quarter": 1, "content": "x"},
        {"symbol": "NVDA", "year": 2026, "quarter": "bad", "content": "x"},
        _entry("NVDA", 2026, 2, "real content"),
    ]
    pick = EarningsTranscriptAdapter._pick_latest(entries)
    assert pick is not None
    assert pick["year"] == 2026 and pick["quarter"] == 2


def test_pick_latest_empty_returns_none():
    assert EarningsTranscriptAdapter._pick_latest([]) is None
    assert EarningsTranscriptAdapter._pick_latest([{"not": "a transcript"}]) is None


# ---------------------------------------------------------------------------
# Signal extractors — forward guidance + AI/capex sentences
# ---------------------------------------------------------------------------

def test_forward_guidance_requires_verb_and_horizon():
    sentences = EarningsTranscriptAdapter._split_sentences(_NVDA_CONTENT)
    guidance = EarningsTranscriptAdapter._find_forward_guidance(sentences)
    assert guidance is not None
    assert "next quarter" in guidance.lower()
    assert "expect" in guidance.lower()


def test_forward_guidance_skips_retrospective():
    text = (
        "Last quarter we expected revenue to be flat and that was indeed the result. "
        "We had a great year and our shareholders are very pleased with results."
    )
    sentences = EarningsTranscriptAdapter._split_sentences(text)
    # 'expected' (past tense) appears but no forward horizon is present
    # in the same sentence, so it should NOT match.
    assert EarningsTranscriptAdapter._find_forward_guidance(sentences) is None


def test_ai_capex_extraction():
    sentences = EarningsTranscriptAdapter._split_sentences(_NVDA_CONTENT)
    ai_capex = EarningsTranscriptAdapter._find_ai_capex(sentences)
    assert ai_capex is not None
    low = ai_capex.lower()
    assert "ai" in low or "data center" in low
    assert "invest" in low or "billion" in low


# ---------------------------------------------------------------------------
# Tone tag
# ---------------------------------------------------------------------------

def test_tone_tag_raised():
    sentences = EarningsTranscriptAdapter._split_sentences(_NVDA_CONTENT)
    guidance = EarningsTranscriptAdapter._find_forward_guidance(sentences)
    assert EarningsTranscriptAdapter._tone_tag(guidance) == "raised"


def test_tone_tag_cut():
    sentences = EarningsTranscriptAdapter._split_sentences(_MSFT_CONTENT_CUT)
    guidance = EarningsTranscriptAdapter._find_forward_guidance(sentences)
    assert guidance is not None
    assert EarningsTranscriptAdapter._tone_tag(guidance) == "cut"


def test_tone_tag_na_when_no_guidance():
    assert EarningsTranscriptAdapter._tone_tag(None) == "n/a"


def test_tone_tag_maintained_when_neutral():
    assert EarningsTranscriptAdapter._tone_tag(
        "We expect revenue next quarter to land within our prior range."
    ) == "maintained"


# ---------------------------------------------------------------------------
# Item shape — end-to-end with mocked fetch_json
# ---------------------------------------------------------------------------

def test_item_shape_full_signal():
    payload = [_entry("NVDA", 2026, 1, _NVDA_CONTENT)]
    with patch("ingestion.sources_aitech.fetch_json", return_value=payload):
        item = EarningsTranscriptAdapter()._fetch_one("NVDA")
    assert item is not None
    assert item["source"] == "earnings_transcript"
    assert item["reliability"] == 0.86
    assert item["url"] == "https://discountingcashflows.com/company/NVDA/transcripts/2026-Q1/"
    text = item["text"]
    assert "NVDA Q1 2026" in text
    assert "tone=raised" in text
    assert "Guide:" in text
    assert "AI/capex:" in text


def test_item_shape_thin_signal_still_emits():
    """A transcript with no forward-guidance or AI-capex sentence should
    still surface a pointer item so triage sees that the call happened."""
    payload = [_entry("AAPL", 2026, 2, _AAPL_CONTENT_NO_SIGNAL)]
    with patch("ingestion.sources_aitech.fetch_json", return_value=payload):
        item = EarningsTranscriptAdapter()._fetch_one("AAPL")
    assert item is not None
    assert "AAPL Q2 2026" in item["text"]
    assert "tone=n/a" in item["text"]
    assert "no forward-guidance" in item["text"].lower()


def test_fetch_one_picks_latest_when_multiple_quarters_present():
    payload = [
        _entry("NVDA", 2025, 4, "Old transcript with no relevant signal text."),
        _entry("NVDA", 2026, 1, _NVDA_CONTENT),
    ]
    with patch("ingestion.sources_aitech.fetch_json", return_value=payload):
        item = EarningsTranscriptAdapter()._fetch_one("NVDA")
    assert item is not None
    assert "Q1 2026" in item["text"]


def test_fetch_one_returns_none_on_empty_payload():
    with patch("ingestion.sources_aitech.fetch_json", return_value=None):
        assert EarningsTranscriptAdapter()._fetch_one("NVDA") is None
    with patch("ingestion.sources_aitech.fetch_json", return_value=[]):
        assert EarningsTranscriptAdapter()._fetch_one("NVDA") is None
    with patch("ingestion.sources_aitech.fetch_json", return_value="rate-limited"):
        assert EarningsTranscriptAdapter()._fetch_one("NVDA") is None


def test_dedup_url_stable_per_ticker_quarter():
    """Re-running on the same day must produce the identical URL so the
    raw_items unique-key (content_hash on url+text) keeps deduplication tight."""
    payload = [_entry("NVDA", 2026, 1, _NVDA_CONTENT)]
    with patch("ingestion.sources_aitech.fetch_json", return_value=payload):
        first = EarningsTranscriptAdapter()._fetch_one("NVDA")
        second = EarningsTranscriptAdapter()._fetch_one("NVDA")
    assert first["url"] == second["url"]
    assert first["text"] == second["text"]


# ---------------------------------------------------------------------------
# Full fetch loop — iterates COVERAGE
# ---------------------------------------------------------------------------

def test_fetch_iterates_coverage_and_handles_partial_failure():
    """If some tickers return data and others return None, the run must
    keep going and emit one item per successful ticker."""
    by_url: dict[str, object] = {}
    for t in EarningsTranscriptAdapter.COVERAGE:
        url = EarningsTranscriptAdapter.ENDPOINT_TEMPLATE.format(ticker=t)
        # Only NVDA and MSFT return data; everything else simulates outage.
        if t in ("NVDA",):
            by_url[url] = [_entry(t, 2026, 1, _NVDA_CONTENT)]
        elif t in ("MSFT",):
            by_url[url] = [_entry(t, 2026, 1, _MSFT_CONTENT_CUT)]
        else:
            by_url[url] = None

    def fake_fetch_json(url, headers=None, timeout=20):  # noqa: ARG001
        return by_url.get(url)

    with patch("ingestion.sources_aitech.fetch_json", side_effect=fake_fetch_json):
        items = EarningsTranscriptAdapter().fetch()

    sources = {it["source"] for it in items}
    assert sources == {"earnings_transcript"}
    tickers = sorted({it["url"].split("/")[-4] for it in items})
    assert tickers == ["MSFT", "NVDA"]
    # Tone tags should reflect the content
    by_t = {it["url"].split("/")[-4]: it for it in items}
    assert "tone=raised" in by_t["NVDA"]["text"]
    assert "tone=cut" in by_t["MSFT"]["text"]
