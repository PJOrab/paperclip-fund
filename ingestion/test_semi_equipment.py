"""Unit tests for SemiEquipmentBookingsAdapter."""
import types
import sys
import importlib
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest


def _make_adapter():
    from ingestion.sources_aitech import SemiEquipmentBookingsAdapter
    return SemiEquipmentBookingsAdapter()


# ── helpers ──────────────────────────────────────────────────────────────────

def _q(end_str, val, fp="Q1", fy=2025, start_offset_days=91):
    end = date.fromisoformat(end_str)
    return {
        "end": end,
        "start": end - timedelta(days=start_offset_days),
        "val": float(val),
        "fp": fp,
        "fy": fy,
        "form": "10-Q",
    }


# ── _fmt_usd ─────────────────────────────────────────────────────────────────

def test_fmt_usd_billions():
    a = _make_adapter()
    assert a._fmt_usd(3.5e9) == "$3.50B"


def test_fmt_usd_millions():
    a = _make_adapter()
    assert a._fmt_usd(250e6) == "$250M"


# ── _pct_change ──────────────────────────────────────────────────────────────

def test_pct_change_basic():
    a = _make_adapter()
    assert abs(a._pct_change(110.0, 100.0) - 10.0) < 0.01


def test_pct_change_zero_prior():
    a = _make_adapter()
    assert a._pct_change(100.0, 0.0) is None


# ── _cycle_tag ───────────────────────────────────────────────────────────────

def test_cycle_tag_upcycle():
    a = _make_adapter()
    assert a._cycle_tag(25.0, None) == "upcycle"


def test_cycle_tag_downcycle():
    a = _make_adapter()
    assert a._cycle_tag(-20.0, None) == "downcycle"


def test_cycle_tag_orders_accelerating():
    a = _make_adapter()
    assert a._cycle_tag(5.0, 20.0) == "orders-accelerating"  # rev=5% below 8% threshold


def test_cycle_tag_orders_accelerating_flat_rev():
    a = _make_adapter()
    assert a._cycle_tag(2.0, 20.0) == "orders-accelerating"


def test_cycle_tag_stable():
    a = _make_adapter()
    assert a._cycle_tag(3.0, 5.0) == "stable"


# ── _find_year_ago / _find_prior_quarter ────────────────────────────────────

def test_find_year_ago_exact():
    a = _make_adapter()
    series = [_q("2024-01-31", 1000), _q("2025-01-31", 1200)]
    result = a._find_year_ago(series, date(2025, 1, 31))
    assert result["end"] == date(2024, 1, 31)


def test_find_year_ago_within_tolerance():
    a = _make_adapter()
    series = [_q("2024-02-01", 1000), _q("2025-01-31", 1200)]
    result = a._find_year_ago(series, date(2025, 1, 31))
    assert result is not None  # 1-day delta is within ±20d


def test_find_year_ago_too_far():
    a = _make_adapter()
    series = [_q("2024-06-30", 1000), _q("2025-01-31", 1200)]
    result = a._find_year_ago(series, date(2025, 1, 31))
    assert result is None


def test_find_prior_quarter():
    a = _make_adapter()
    series = [_q("2024-10-31", 1000), _q("2025-01-31", 1200)]
    result = a._find_prior_quarter(series, date(2025, 1, 31))
    assert result["end"] == date(2024, 10, 31)


# ── _build_item with mocked _fetch_concept_quarterly ─────────────────────────

def _make_series(n=4, base_val=3_000_000_000, growth=0.25):
    """4 quarters of revenue data with strong YoY growth."""
    quarters = []
    start = date(2024, 1, 31)
    for i in range(n):
        end = date(start.year + (start.month + i * 3 - 1) // 12,
                   ((start.month + i * 3 - 1) % 12) + 1,
                   28)
        quarters.append({
            "end": end,
            "start": end - timedelta(days=91),
            "val": base_val * (1 + growth * (i / 4)),
            "fp": f"Q{i+1}",
            "fy": 2024 + i // 4,
            "form": "10-Q",
        })
    return quarters


def test_build_item_emits_on_strong_rev_yoy():
    a = _make_adapter()
    rev_series = [
        _q("2024-01-31", 3_000_000_000, fp="Q1", fy=2024),
        _q("2024-04-30", 3_100_000_000, fp="Q2", fy=2024),
        _q("2024-07-31", 3_200_000_000, fp="Q3", fy=2024),
        _q("2024-10-31", 3_300_000_000, fp="Q4", fy=2024),
        _q("2025-01-31", 4_000_000_000, fp="Q1", fy=2025),  # +33% YoY
    ]
    gp_series = [
        _q("2024-01-31", 1_500_000_000),
        _q("2025-01-31", 2_100_000_000),
    ]
    with patch.object(a, "_fetch_concept_quarterly", side_effect=[
        rev_series,  # REVENUE_CONCEPTS call
        gp_series,   # GROSS_PROFIT_CONCEPTS call
        [],          # RPO_CONCEPTS call (empty)
    ]):
        item = a._build_item("AMAT", 796343, "Applied Materials", "test hook")

    assert item is not None
    assert "upcycle" in item["text"]
    assert "AMAT" in item["text"]
    assert item["source"] == "semi_equipment_bookings"
    assert item["reliability"] == 0.95
    assert len(item["text"]) <= 550


def test_build_item_suppressed_on_flat_rev():
    a = _make_adapter()
    rev_series = [
        _q("2024-01-31", 3_000_000_000, fp="Q1", fy=2024),
        _q("2024-04-30", 3_000_000_000, fp="Q2", fy=2024),
        _q("2025-01-31", 3_050_000_000, fp="Q1", fy=2025),  # +1.7% — below 10% gate
    ]
    with patch.object(a, "_fetch_concept_quarterly", side_effect=[
        rev_series,
        [],   # gp
        [],   # rpo
    ]):
        item = a._build_item("KLAC", 319201, "KLA Corporation", "test hook")

    assert item is None


def test_build_item_rpo_triggers_emission():
    a = _make_adapter()
    rev_series = [
        _q("2024-01-31", 3_000_000_000, fp="Q1", fy=2024),
        _q("2025-01-31", 3_050_000_000, fp="Q1", fy=2025),  # only +1.7% YoY
    ]
    rpo_series = [
        _q("2024-10-31", 2_000_000_000),  # prior quarter RPO
        _q("2025-01-31", 2_500_000_000),  # +25% QoQ → above RPO_THRESHOLD_PCT
    ]
    with patch.object(a, "_fetch_concept_quarterly", side_effect=[
        rev_series,
        [],         # gp
        rpo_series, # rpo
    ]):
        item = a._build_item("LRCX", 707549, "Lam Research", "test hook")

    assert item is not None
    assert "orders-accelerating" in item["text"] or "backlog" in item["text"]


def test_build_item_no_rev_returns_none():
    a = _make_adapter()
    with patch.object(a, "_fetch_concept_quarterly", return_value=[]):
        item = a._build_item("AMAT", 796343, "Applied Materials", "test hook")
    assert item is None


# ── fetch() integration (adapter isolation) ──────────────────────────────────

def test_fetch_isolates_per_company():
    a = _make_adapter()
    rev_ok = [
        _q("2024-01-31", 3_000_000_000),
        _q("2025-01-31", 4_000_000_000),  # +33% → emits
    ]

    call_count = [0]
    def side_effect(cik, concepts):
        call_count[0] += 1
        # Only AMAT (cik=796343) gets real data; others raise
        if cik == 796343 and "RevenueFromContract" in str(concepts):
            return rev_ok
        if cik != 796343:
            raise RuntimeError("simulated failure")
        return []

    with patch.object(a, "_fetch_concept_quarterly", side_effect=side_effect):
        results = a.fetch()

    # Failures for LRCX/KLAC should be swallowed; AMAT item may or may not emit
    # depending on gp/rpo calls, but the run must not raise.
    assert isinstance(results, list)
