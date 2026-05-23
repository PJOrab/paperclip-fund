"""
Unit tests for HyperscalerFinancialsAdapter. Pure-fixture based, no network.

Locks the SEC EDGAR XBRL companyconcept response shape:
  { "units": { "USD": [ {start, end, val, fp, fy, form}, ... ] } }

The adapter must:
  - filter to discrete-quarter values (end-start ≤ 100d), dropping YTD overlaps,
  - pick the concept with the freshest period_end out of the fallback chain
    (filers leave older concepts populated with stale data after migrating tags),
  - compute capex YoY / QoQ vs the closest matched period,
  - compute revenue YoY and operating-margin pp delta vs year-ago,
  - emit one item per company with a stable per-(ticker, period_end) dedup URL,
  - tag the capex trajectory: accelerating / expanding / steady / decelerating /
    contracting,
  - degrade silently on per-ticker failures (adapter isolation).
"""
from datetime import date
from unittest.mock import patch

from ingestion.sources_aitech import HyperscalerFinancialsAdapter


def _xbrl_payload(rows):
    """Wrap a list of (start, end, val, fp, fy, form) tuples in the SEC XBRL
    companyconcept JSON shape."""
    return {
        "units": {
            "USD": [
                {"start": s, "end": e, "val": v, "fp": fp, "fy": fy, "form": fm}
                for (s, e, v, fp, fy, fm) in rows
            ]
        }
    }


# --- _capex_tag thresholds ---

def test_capex_tag_thresholds():
    f = HyperscalerFinancialsAdapter._capex_tag
    assert f(None) == "steady"
    assert f(50.0) == "accelerating"     # ≥30%
    assert f(30.0) == "accelerating"
    assert f(20.0) == "expanding"        # 15..30
    assert f(15.0) == "expanding"
    assert f(0.0) == "steady"            # -5..15
    assert f(-4.9) == "steady"
    assert f(-5.0) == "decelerating"     # ≤-5
    assert f(-14.9) == "decelerating"
    assert f(-15.0) == "contracting"     # ≤-15
    assert f(-80.0) == "contracting"


# --- _pct_change ---

def test_pct_change_basics_and_zero_prior():
    f = HyperscalerFinancialsAdapter._pct_change
    assert f(110, 100) == 10.0
    assert f(80, 100) == -20.0
    # Year-ago zero must NOT raise ZeroDivisionError → we'd ship a broken item.
    assert f(100, 0) is None
    assert f(100, None) is None


# --- _find_year_ago tolerance window ---

def test_find_year_ago_within_tolerance_and_outside():
    series = [
        {"end": date(2025, 3, 31), "val": 10.0},
        {"end": date(2025, 3, 15), "val": 999.0},  # 15d off → still in ±20d window
        {"end": date(2024, 12, 31), "val": 7.0},   # ~90d off → out
    ]
    # Target = current - 365d = 2026-03-31 - 365 = 2025-03-31
    r = HyperscalerFinancialsAdapter._find_year_ago(series, date(2026, 3, 31))
    assert r is not None
    assert r["val"] == 10.0  # exact match wins over 15d-off
    # If only out-of-window entries exist → None
    sparse = [{"end": date(2024, 12, 31), "val": 7.0}]
    assert HyperscalerFinancialsAdapter._find_year_ago(sparse, date(2026, 3, 31)) is None


# --- _fetch_concept_quarterly: discrete-quarter filter + freshest-series pick ---

def _adapter_with_fetch_stub(stub):
    a = HyperscalerFinancialsAdapter()
    return a, stub


def test_fetch_drops_ytd_and_full_year_keeps_discrete_quarter():
    # Two entries for end=2026-03-31: one YTD-9mo (270d), one discrete quarter (90d).
    payload = _xbrl_payload([
        ("2025-07-01", "2026-03-31", 80_000_000_000, "Q3", 2026, "10-Q"),  # YTD: 273d
        ("2026-01-01", "2026-03-31", 30_000_000_000, "Q3", 2026, "10-Q"),  # discrete: 90d
        ("2025-01-01", "2025-12-31", 120_000_000_000, "FY", 2025, "10-K"), # full-year: 365d
    ])
    calls = []

    def fake_fetch(url, headers=None, timeout=None):
        calls.append(url)
        return payload

    with patch("ingestion.sources_aitech.fetch_json", side_effect=fake_fetch):
        rows = HyperscalerFinancialsAdapter()._fetch_concept_quarterly(
            789019, ("PaymentsToAcquirePropertyPlantAndEquipment",)
        )
    # Only the 90d entry survives
    assert len(rows) == 1
    assert rows[0]["val"] == 30_000_000_000
    assert rows[0]["end"] == date(2026, 3, 31)


def test_fetch_picks_freshest_series_across_fallback_chain():
    # Primary concept has stale data (latest end = 2025-03-31). Fallback has fresh
    # data (latest end = 2026-03-31). Adapter must pick the fresher series — not
    # the first-non-empty one (regression for the GOOGL Revenues vs
    # RevenueFromContractWithCustomer… migration).
    stale = _xbrl_payload([
        ("2025-01-01", "2025-03-31", 90_000_000_000, "Q1", 2025, "10-Q"),
    ])
    fresh = _xbrl_payload([
        ("2025-01-01", "2025-03-31", 91_000_000_000, "Q1", 2025, "10-Q"),
        ("2026-01-01", "2026-03-31", 109_000_000_000, "Q1", 2026, "10-Q"),
    ])
    seq = {"RevenueFromContractWithCustomerExcludingAssessedTax": stale,
           "Revenues": fresh}

    def fake_fetch(url, headers=None, timeout=None):
        for k, v in seq.items():
            if k in url:
                return v
        return None

    with patch("ingestion.sources_aitech.fetch_json", side_effect=fake_fetch):
        rows = HyperscalerFinancialsAdapter()._fetch_concept_quarterly(
            1652044, HyperscalerFinancialsAdapter.REVENUE_CONCEPTS,
        )
    assert rows[-1]["end"] == date(2026, 3, 31)
    assert rows[-1]["val"] == 109_000_000_000


def test_fetch_returns_empty_when_no_concept_yields_quarterly_data():
    # All concepts return only YTD/full-year entries.
    only_ytd = _xbrl_payload([
        ("2025-01-01", "2025-12-31", 1_000, "FY", 2025, "10-K"),
    ])
    with patch("ingestion.sources_aitech.fetch_json", return_value=only_ytd):
        rows = HyperscalerFinancialsAdapter()._fetch_concept_quarterly(
            789019, ("PaymentsToAcquirePropertyPlantAndEquipment",)
        )
    assert rows == []


# --- _build_company_item: end-to-end item shape ---

def _three_concept_stub(capex_payload, rev_payload, opinc_payload):
    """Return a fake fetch_json that routes by concept substring in URL."""
    def fake_fetch(url, headers=None, timeout=None):
        if "PaymentsToAcquire" in url:
            return capex_payload
        if "Revenue" in url:
            return rev_payload
        if "OperatingIncomeLoss" in url:
            return opinc_payload
        return None
    return fake_fetch


def test_build_item_full_pipeline_with_yoy_and_margin():
    # MSFT-like shape: Q3 FY26 capex with YoY +84% and clean revenue/opinc.
    capex = _xbrl_payload([
        ("2025-01-01", "2025-03-31", 16_750_000_000, "Q3", 2025, "10-Q"),  # year-ago Q
        ("2026-01-01", "2026-03-31", 30_876_000_000, "Q3", 2026, "10-Q"),  # current Q
    ])
    rev = _xbrl_payload([
        ("2025-01-01", "2025-03-31", 70_000_000_000, "Q3", 2025, "10-Q"),
        ("2026-01-01", "2026-03-31", 82_886_000_000, "Q3", 2026, "10-Q"),
    ])
    opinc = _xbrl_payload([
        ("2025-01-01", "2025-03-31", 31_900_000_000, "Q3", 2025, "10-Q"),
        ("2026-01-01", "2026-03-31", 38_398_000_000, "Q3", 2026, "10-Q"),
    ])
    with patch("ingestion.sources_aitech.fetch_json",
               side_effect=_three_concept_stub(capex, rev, opinc)):
        item = HyperscalerFinancialsAdapter()._build_company_item(
            "MSFT", 789019, "Microsoft", "AI capex anchor",
        )
    assert item is not None
    assert item["source"] == "hyperscaler_financials"
    assert item["reliability"] == 0.95
    # Stable per-period dedup URL
    assert "#xbrl-fin-2026-03-31" in item["url"]
    text = item["text"]
    # Capex velocity headline
    assert "Microsoft" in text
    assert "$30.88B" in text
    assert "+84.3%" in text or "+84.4%" in text  # rounding tolerance
    assert "accelerating" in text
    # Revenue + margin signal must be present
    assert "$82.89B" in text
    assert "OpM 46.3%" in text


def test_build_item_returns_none_when_capex_series_missing():
    # If the SEC endpoint returns no usable capex series for a ticker, the
    # adapter must skip cleanly — not emit a partial item or raise.
    empty = _xbrl_payload([])
    full = _xbrl_payload([
        ("2026-01-01", "2026-03-31", 1, "Q1", 2026, "10-Q"),
    ])
    with patch("ingestion.sources_aitech.fetch_json",
               side_effect=_three_concept_stub(empty, full, full)):
        item = HyperscalerFinancialsAdapter()._build_company_item(
            "FAKE", 0, "Fake Inc.", "hook",
        )
    assert item is None


def test_fetch_isolates_per_ticker_failures():
    # A raising fetch on one ticker must NOT take out the rest of the cycle.
    raise_for = {"AMZN"}
    capex = _xbrl_payload([
        ("2025-01-01", "2025-03-31", 100_000, "Q1", 2025, "10-Q"),
        ("2026-01-01", "2026-03-31", 200_000, "Q1", 2026, "10-Q"),
    ])

    def fake_fetch(url, headers=None, timeout=None):
        for tk_cik in raise_for:
            # AMZN CIK = 1018724 → 0001018724 in URL
            if "CIK0001018724" in url:
                raise RuntimeError("boom")
        if "PaymentsToAcquire" in url:
            return capex
        if "Revenue" in url:
            return capex  # reuse, ok for shape
        if "OperatingIncomeLoss" in url:
            return capex
        return None

    with patch("ingestion.sources_aitech.fetch_json", side_effect=fake_fetch):
        items = HyperscalerFinancialsAdapter().fetch()
    # 5 companies total; AMZN dropped → 4 items emitted, run not killed.
    tickers = [it["text"].split("·")[1].split("]")[0].strip() for it in items]
    assert "Amazon" not in tickers
    assert len(items) == 4
