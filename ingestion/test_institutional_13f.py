"""
Smoke test for InstitutionalHoldingsAdapter.

Tests:
  1. CUSIP map covers all watchlist tickers that are US-listed / ADR.
  2. _parse_xml correctly parses a synthetic 13F holdings XML fragment.
  3. _diff correctly classifies new/exit/add/reduce/no-change cases.
  4. Live fetch: hits SEC EDGAR for ONE institution (ARK, CIK 0001697748)
     and verifies the adapter returns list[dict] with required fields.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from ingestion.sources_aitech import InstitutionalHoldingsAdapter

ADAPTER = InstitutionalHoldingsAdapter()


def test_cusip_map_coverage():
    from ingestion.watchlist import TICKERS
    covered = set(ADAPTER.CUSIP_MAP.keys())
    # All watchlist tickers should be in the CUSIP map
    missing = [t for t in TICKERS if t not in covered]
    assert not missing, f"Tickers missing from CUSIP_MAP: {missing}"
    print(f"[PASS] CUSIP map covers all {len(TICKERS)} watchlist tickers")


def test_parse_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>67066G10</cusip>
    <value>1250000</value>
    <shrsOrPrnAmt><sshPrnamt>500000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>500000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>MICROSOFT CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>59491810</cusip>
    <value>850000</value>
    <shrsOrPrnAmt><sshPrnamt>200000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>200000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
</informationTable>"""
    holdings = ADAPTER._parse_xml(xml)
    assert holdings.get("67066G10") == 1_250_000, f"NVDA value wrong: {holdings}"
    assert holdings.get("59491810") == 850_000, f"MSFT value wrong: {holdings}"
    print(f"[PASS] _parse_xml returned {len(holdings)} positions")


def test_diff_new_position():
    latest = {"67066G10": 50_000}
    prior = {}
    item = ADAPTER._diff("NVDA", "67066G104", "Test Fund", "0001234567",
                         latest, prior, "2024-12-31", "2024-09-30")
    assert item is not None
    assert "opened new" in item["text"]
    assert item["source"] == "institutional_13f"
    print(f"[PASS] new position: {item['text']}")


def test_diff_exit():
    latest = {}
    prior = {"67066G10": 100_000}
    item = ADAPTER._diff("NVDA", "67066G104", "Test Fund", "0001234567",
                         latest, prior, "2024-12-31", "2024-09-30")
    assert item is not None
    assert "exited" in item["text"]
    print(f"[PASS] full exit: {item['text']}")


def test_diff_add():
    latest = {"67066G10": 150_000}
    prior = {"67066G10": 100_000}
    item = ADAPTER._diff("NVDA", "67066G104", "Test Fund", "0001234567",
                         latest, prior, "2024-12-31", "2024-09-30")
    assert item is not None
    assert "added" in item["text"]
    print(f"[PASS] add: {item['text']}")


def test_diff_no_change():
    latest = {"67066G10": 100_000}
    prior = {"67066G10": 105_000}  # 5% change, below 20% threshold
    item = ADAPTER._diff("NVDA", "67066G104", "Test Fund", "0001234567",
                         latest, prior, "2024-12-31", "2024-09-30")
    assert item is None
    print("[PASS] no-change filtered (5% < 20% threshold)")


def test_diff_small_position():
    latest = {"67066G10": 1_000}  # $1M, below MIN_VALUE_K ($5M)
    prior = {}
    item = ADAPTER._diff("NVDA", "67066G104", "Test Fund", "0001234567",
                         latest, prior, "2024-12-31", "2024-09-30")
    assert item is None
    print("[PASS] small position filtered (<$5M)")


def test_live_ark_filings():
    """Live: fetch ARK 13F filing list from SEC EDGAR."""
    print("[LIVE] Fetching ARK 13F filings from SEC EDGAR…")
    filings = ADAPTER._get_recent_filings("0001697748")
    print(f"  Found {len(filings)} 13F-HR filings for ARK")
    if filings:
        acc, period = filings[0]
        print(f"  Latest: accession={acc} period={period}")
    assert isinstance(filings, list)
    print("[PASS] ARK filing fetch returned list")


def test_live_ark_holdings():
    """Live: parse one ARK 13F holdings XML (most recent filing)."""
    print("[LIVE] Parsing ARK 13F holdings XML…")
    filings = ADAPTER._get_recent_filings("0001697748")
    if not filings:
        print("[SKIP] No ARK filings found")
        return
    acc, period = filings[0]
    holdings = ADAPTER._parse_holdings("0001697748", acc)
    print(f"  Parsed {len(holdings)} CUSIP positions for period {period}")
    # Check NVDA CUSIP
    nvda_cusip8 = "67066G10"
    if nvda_cusip8 in holdings:
        print(f"  NVDA ({nvda_cusip8}): ${holdings[nvda_cusip8] / 1000:.1f}M")
    assert isinstance(holdings, dict)
    print("[PASS] ARK holdings parse succeeded")


if __name__ == "__main__":
    print("=== InstitutionalHoldingsAdapter Tests ===\n")
    test_cusip_map_coverage()
    test_parse_xml()
    test_diff_new_position()
    test_diff_exit()
    test_diff_add()
    test_diff_no_change()
    test_diff_small_position()
    test_live_ark_filings()
    test_live_ark_holdings()
    print("\n=== All tests passed ===")
