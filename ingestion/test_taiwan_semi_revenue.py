"""
Unit tests for TaiwanSemiRevenueAdapter. Pure-fixture based, no network.
Locks the TWSE OpenAPI t187ap05_L schema: keys are Chinese, revenue values are
NTD-thousands strings, %% fields are unbounded-precision string floats, and the
data year-month is ROC (Republic of China) era (year - 1911).
"""
from unittest.mock import patch

from ingestion.sources_aitech import TaiwanSemiRevenueAdapter


def _row(code: str, name_zh: str, ym: str, rev_k: str,
         mom: str, yoy: str, ytd_yoy: str) -> dict:
    """Build a TWSE OpenAPI t187ap05_L row in the on-the-wire shape."""
    return {
        "出表日期": "1150517",
        "資料年月": ym,
        "公司代號": code,
        "公司名稱": name_zh,
        "產業別": "半導體業",
        "營業收入-當月營收": rev_k,
        "營業收入-上月營收": "0",
        "營業收入-去年當月營收": "0",
        "營業收入-上月比較增減(%)": mom,
        "營業收入-去年同月增減(%)": yoy,
        "累計營業收入-當月累計營收": "0",
        "累計營業收入-去年累計營收": "0",
        "累計營業收入-前期比較增減(%)": ytd_yoy,
        "備註": "-",
    }


# ---------------------------------------------------------------------------
# _roc_to_iso_month — ROC-to-Gregorian conversion
# ---------------------------------------------------------------------------

def test_roc_to_iso_month_typical():
    # 11504 = ROC 115 / Apr → 2026-04
    assert TaiwanSemiRevenueAdapter._roc_to_iso_month("11504") == (2026, 4)


def test_roc_to_iso_month_december():
    # 11412 = ROC 114 / Dec → 2025-12
    assert TaiwanSemiRevenueAdapter._roc_to_iso_month("11412") == (2025, 12)


def test_roc_to_iso_month_garbage_returns_none():
    assert TaiwanSemiRevenueAdapter._roc_to_iso_month("") is None
    assert TaiwanSemiRevenueAdapter._roc_to_iso_month("abc") is None
    assert TaiwanSemiRevenueAdapter._roc_to_iso_month("11500") is None  # month=00
    assert TaiwanSemiRevenueAdapter._roc_to_iso_month("11513") is None  # month=13


# ---------------------------------------------------------------------------
# _fmt_revenue_ntd — thousands-NTD to human bn/m
# ---------------------------------------------------------------------------

def test_fmt_revenue_billions():
    # 410,725,118 thousand NTD = ~410.7 bn NTD (TSMC Apr-2026 print)
    s = TaiwanSemiRevenueAdapter._fmt_revenue_ntd(410_725_118.0)
    assert "NT$410.7 bn" == s


def test_fmt_revenue_millions():
    s = TaiwanSemiRevenueAdapter._fmt_revenue_ntd(750_000.0)  # 750m thousand = 750bn? no -- 750_000 K = 750 m
    assert s.startswith("NT$") and "m" in s


# ---------------------------------------------------------------------------
# Item construction — full schema round-trip
# ---------------------------------------------------------------------------

def test_fetch_filters_to_watched_semis():
    """Adapter must drop rows whose 公司代號 is not in COMPANIES, regardless of size."""
    payload = [
        _row("1101", "台泥", "11504", "12000000", "1.0", "2.0", "3.0"),  # cement, NOT watched
        _row("2330", "台積電", "11504", "410725118", "-1.07", "17.49", "29.87"),
        _row("2317", "鴻海", "11504", "832097956", "3.52", "29.73", "29.73"),
    ]
    with patch("ingestion.sources_aitech.fetch_json", return_value=payload):
        items = TaiwanSemiRevenueAdapter().fetch()
    assert len(items) == 2
    labels = [it["text"].split("·")[1].split("]")[0].strip() for it in items]
    assert set(labels) == {"TSMC", "Hon Hai"}


def test_item_shape_and_signal_tags():
    """Verify every field used by downstream triage is present and well-formed."""
    payload = [
        # YoY +17.5% → "accelerating"
        _row("2330", "台積電", "11504", "410725118", "-1.0757", "17.4954", "29.8716"),
        # YoY -4.1% → "steady" (|YoY|<5)
        _row("2454", "聯發科", "11504", "46736664", "-26.07", "-4.13", "-3.13"),
        # YoY -12.0% → "contracting"
        _row("3711", "日月光", "11504", "62247107", "1.08", "-12.0", "17.7"),
    ]
    with patch("ingestion.sources_aitech.fetch_json", return_value=payload):
        items = TaiwanSemiRevenueAdapter().fetch()
    by_label = {it["text"].split("·")[1].split("]")[0].strip(): it for it in items}

    tsmc = by_label["TSMC"]
    assert tsmc["source"] == "tw_semi_revenue"
    assert tsmc["reliability"] == 0.92
    assert tsmc["url"] == "https://mops.twse.com.tw/mops/web/t05st10_ifrs?co_id=2330&ym=202604"
    assert "2026-04" in tsmc["text"]
    assert "NT$410.7 bn" in tsmc["text"]
    assert "YoY +17.5%" in tsmc["text"]
    assert "accelerating" in tsmc["text"]

    # MediaTek YoY -4.1% must read "steady" (band is |YoY|<5)
    assert "steady" in by_label["MediaTek"]["text"]
    # ASE YoY -12% must read "contracting"
    assert "contracting" in by_label["ASE"]["text"]


def test_invalid_revenue_dropped():
    """Zero/negative/garbage revenue must not produce an item."""
    payload = [
        _row("2330", "台積電", "11504", "0", "0", "0", "0"),
        _row("2303", "聯電", "11504", "not-a-number", "0", "0", "0"),
        _row("2454", "聯發科", "BAD-YM", "1000", "0", "0", "0"),
    ]
    with patch("ingestion.sources_aitech.fetch_json", return_value=payload):
        items = TaiwanSemiRevenueAdapter().fetch()
    assert items == []


def test_fetch_handles_non_list_response():
    """Endpoint returning an error object (not a list) must not crash the run."""
    with patch("ingestion.sources_aitech.fetch_json", return_value=None):
        assert TaiwanSemiRevenueAdapter().fetch() == []
    with patch("ingestion.sources_aitech.fetch_json", return_value={"error": "rate-limited"}):
        assert TaiwanSemiRevenueAdapter().fetch() == []


def test_dedup_url_stable_per_month():
    """Same ticker + same data-month → identical URL → content_hash stays put across daily wakes."""
    row = _row("2330", "台積電", "11504", "410725118", "0", "17.5", "29.9")
    with patch("ingestion.sources_aitech.fetch_json", return_value=[row]):
        first = TaiwanSemiRevenueAdapter().fetch()
        second = TaiwanSemiRevenueAdapter().fetch()
    assert first[0]["url"] == second[0]["url"]
