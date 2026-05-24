"""
Smoke tests that the FilingLanguageAdapter (source='filing_sentiment') has
its taxonomy block in the triage_user prompt AND its cluster-handling block
in ANALYST_SYSTEM. Without these the upstream adapter ships items that
downstream stages treat with the generic-sentiment fallback — losing the
σ / QoQ / extreme-aware importance tiering and the cross-reference recipes
(eps_revisions, insider_cluster, earnings_transcript) that make the
Loughran-McDonald signal investment-grade.
"""
from agents.prompts import ANALYST_SYSTEM, triage_user


def test_filing_sentiment_in_triage_user():
    body = triage_user([{"text": "x", "source": "filing_sentiment", "reliability": 0.88, "url": "u"}])
    # Source taxonomy block must be present
    assert "FILING SENTIMENT:" in body, "FILING SENTIMENT taxonomy block missing from triage_user"
    assert "filing_sentiment" in body
    # Materiality gate language (so triage trusts the pre-filter)
    assert "1.5σ" in body
    assert "20% QoQ" in body or "20 % QoQ" in body
    # Importance tiering present
    assert "Importance tiering" in body or "Importance:" in body
    # Cross-reference recipes (proves it teaches the model to combine signals)
    assert "eps_revisions" in body
    assert "insider_cluster" in body
    assert "earnings_transcript" in body
    # Output format anchor (trajectory line)
    assert "trajectory" in body.lower()
    assert "‰" in body  # per-mille unit referenced


def test_filing_sentiment_cluster_block_in_analyst_system():
    assert "FILING SENTIMENT CLUSTERS" in ANALYST_SYSTEM, \
        "FILING SENTIMENT CLUSTERS block missing from ANALYST_SYSTEM"
    assert "source='filing_sentiment'" in ANALYST_SYSTEM
    # Magnitude bands
    assert "2σ" in ANALYST_SYSTEM
    assert "40% QoQ" in ANALYST_SYSTEM
    # Conviction-impact arithmetic
    assert "+0.05-0.10 conviction" in ANALYST_SYSTEM
    # Differentiation logic
    assert "Loughran-McDonald edge" in ANALYST_SYSTEM
    # Horizon calibration (default quarters; weeks when earnings imminent)
    assert "horizon='quarters'" in ANALYST_SYSTEM
    assert "earnings_calendar" in ANALYST_SYSTEM
    # key_facts format anchor with the ‰ unit so the model emits the trajectory line verbatim
    assert "‰" in ANALYST_SYSTEM
