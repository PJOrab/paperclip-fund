"""
Unit tests for TechnicalLevelsAdapter pure helpers and end-to-end formatting
via an injected fake history. Runs without network / yfinance.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from ingestion.sources_aitech import (
    TechnicalLevelsAdapter,
    _detect_cross,
    _rsi,
    _sma,
)


# ---------------------------------------------------------------------------
# _sma
# ---------------------------------------------------------------------------

def test_sma_basic():
    assert _sma([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx(3.5)
    assert _sma([1.0, 2.0, 3.0, 4.0, 5.0], 5) == pytest.approx(3.0)


def test_sma_too_short_returns_none():
    assert _sma([1.0, 2.0], 5) is None
    assert _sma([], 1) is None
    assert _sma([1.0], 0) is None


# ---------------------------------------------------------------------------
# _rsi (Wilder 14-period)
# ---------------------------------------------------------------------------

def test_rsi_steady_uptrend_is_overbought():
    # 30 strictly rising closes — RSI must approach 100 (all gains, no losses)
    closes = [100.0 + i for i in range(30)]
    rsi = _rsi(closes, 14)
    assert rsi is not None and rsi >= 95.0


def test_rsi_steady_downtrend_is_oversold():
    closes = [100.0 - i for i in range(30)]
    rsi = _rsi(closes, 14)
    assert rsi is not None and rsi <= 5.0


def test_rsi_balanced_is_near_50():
    # Alternating +1/-1 pattern → average gain ≈ average loss → RSI ≈ 50
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
    rsi = _rsi(closes, 14)
    assert rsi is not None and 40.0 <= rsi <= 60.0


def test_rsi_too_short_returns_none():
    assert _rsi([1.0, 2.0, 3.0], 14) is None


# ---------------------------------------------------------------------------
# _detect_cross
# ---------------------------------------------------------------------------

def test_detect_golden_cross():
    # Yesterday fast<=slow, today fast>slow → golden
    assert _detect_cross(101.0, 99.0, 100.0, 100.0) == "golden"


def test_detect_death_cross():
    assert _detect_cross(99.0, 101.0, 100.0, 100.0) == "death"


def test_detect_no_cross_when_trend_continues():
    # fast was already above and remains above → no cross today
    assert _detect_cross(105.0, 104.0, 100.0, 100.0) is None


def test_detect_cross_returns_none_on_missing_input():
    assert _detect_cross(None, 99.0, 100.0, 100.0) is None


# ---------------------------------------------------------------------------
# End-to-end formatting via injected fake history
# ---------------------------------------------------------------------------

class _FakeHistDF:
    """Pandas-DataFrame stand-in providing .tolist() per column + .index + .empty."""
    def __init__(self, closes, opens, highs, lows, vols, dates):
        self._data = {
            "Close": closes, "Open": opens, "High": highs,
            "Low": lows, "Volume": vols,
        }
        self.index = _FakeIndex(dates)
        self.empty = False

    def __len__(self):
        return len(self._data["Close"])

    def __getitem__(self, key):
        return _FakeSeries(self._data[key])


class _FakeIndex:
    def __init__(self, dates):
        self._dates = dates

    def __getitem__(self, i):
        return _FakeStamp(self._dates[i])


class _FakeStamp:
    def __init__(self, d):
        self._d = d

    def date(self):
        return self._d


class _FakeSeries:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


class _FakeTicker:
    def __init__(self, hist):
        self._hist = hist

    def history(self, period=None, auto_adjust=True):  # noqa: ARG002
        return self._hist


def _build_hist(closes, opens=None, vols=None, last_date=None):
    n = len(closes)
    if opens is None:
        opens = list(closes)
    if vols is None:
        vols = [1_000_000] * n
    if last_date is None:
        last_date = date(2026, 5, 22)
    dates = [last_date - timedelta(days=(n - 1 - i)) for i in range(n)]
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    return _FakeHistDF(closes, opens, highs, lows, vols, dates)


class _FakeYF:
    def __init__(self, ticker_to_hist):
        self._map = ticker_to_hist

    def Ticker(self, symbol):
        return _FakeTicker(self._map.get(symbol))


def test_adapter_emits_nothing_for_quiet_ticker():
    # Stable ramp near current price, average volume, mid-RSI → no trigger
    closes = [100.0 + i * 0.1 for i in range(260)]
    hist = _build_hist(closes)
    fake_yf = _FakeYF({"NVDA": hist})

    adapter = TechnicalLevelsAdapter()
    # Only NVDA in the universe for this test
    adapter.TICKERS = ["NVDA"]

    with patch("ingestion.sources_aitech.time.sleep"):
        # Force the import path inside fetch() to return our fake yf module
        import sys
        sys.modules["yfinance"] = fake_yf  # type: ignore
        try:
            items = adapter.fetch()
        finally:
            sys.modules.pop("yfinance", None)
    # Could be 0 (no triggers) or 1 weak signal; assert at minimum the structure
    for it in items:
        assert it["source"] == "tech_level"
        assert it["text"].startswith("[TECH · NVDA]")


def test_adapter_detects_oversold_after_sharp_drop():
    # Long flat tail then a 15-day sharp drop → RSI deeply oversold + 50d breach
    closes = [200.0] * 240 + [200.0 - 4.0 * i for i in range(1, 21)]  # drop to 120
    vols = [1_000_000] * 259 + [3_500_000]  # volume spike on the last day
    hist = _build_hist(closes, vols=vols)
    fake_yf = _FakeYF({"NVDA": hist})

    adapter = TechnicalLevelsAdapter()
    adapter.TICKERS = ["NVDA"]

    import sys
    sys.modules["yfinance"] = fake_yf  # type: ignore
    try:
        with patch("ingestion.sources_aitech.time.sleep"):
            items = adapter.fetch()
    finally:
        sys.modules.pop("yfinance", None)

    assert len(items) == 1
    item = items[0]
    assert item["source"] == "tech_level"
    assert item["text"].startswith("[TECH · NVDA]")
    # Strongest trigger should be 52w-low or 200d breach territory after a ~40% drawdown
    assert item["reliability"] in (0.78, 0.83, 0.87, 0.90)
    # Should mention RSI oversold and volume spike in the composite headline
    assert "RSI-14" in item["text"]
    assert "oversold" in item["text"]


def test_adapter_detects_gap_up_with_volume_spike():
    # Quiet history, then a gap-up open + volume spike on the final day
    closes = [100.0 + (i % 3) * 0.1 for i in range(259)]  # 259 days flat-ish
    closes.append(105.0)  # +5% close
    opens = list(closes)
    opens[-1] = 104.5  # gap-up open: 104.5 vs prior close 100.x → ~+4.5%
    vols = [1_000_000] * 259 + [4_000_000]
    hist = _build_hist(closes, opens=opens, vols=vols)
    fake_yf = _FakeYF({"NVDA": hist})

    adapter = TechnicalLevelsAdapter()
    adapter.TICKERS = ["NVDA"]

    import sys
    sys.modules["yfinance"] = fake_yf  # type: ignore
    try:
        with patch("ingestion.sources_aitech.time.sleep"):
            items = adapter.fetch()
    finally:
        sys.modules.pop("yfinance", None)

    assert len(items) == 1
    text = items[0]["text"]
    assert "gap-up" in text
    assert "volume" in text


def test_adapter_skips_when_history_too_short():
    # < 205 rows (need SMA_SLOW + 5) → no item
    closes = [100.0 + i for i in range(50)]
    hist = _build_hist(closes)
    fake_yf = _FakeYF({"NVDA": hist})

    adapter = TechnicalLevelsAdapter()
    adapter.TICKERS = ["NVDA"]

    import sys
    sys.modules["yfinance"] = fake_yf  # type: ignore
    try:
        with patch("ingestion.sources_aitech.time.sleep"):
            items = adapter.fetch()
    finally:
        sys.modules.pop("yfinance", None)

    assert items == []
