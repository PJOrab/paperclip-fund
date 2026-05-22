"""
Watchlist-Konsistenz-Tests (stdlib-only, kein Netz/DB). Sichern, dass die
SEC-Firmennamen-Filter EINE Quelle der Wahrheit haben: jeder Ticker in TICKERS
hat genau ein lowercase Namensfragment in WATCHLIST_NAME_FRAGMENTS, und der
Off-Watchlist-8-K-Adapter (SECBroadEventsAdapter) leitet seinen Skip-Filter
genau daraus ab. Ohne diesen Guard würde ein neu hinzugefügter Ticker ohne
Fragment dazu führen, dass dessen 8-K im Off-Watchlist-Sweep als Duplikat des
(reicheren) EDGARAdapter-Eintrags durchrutscht.

Lauf:  python3 -m ingestion.test_watchlist_sync   (aus dem Repo-Root)
"""
from . import watchlist as W
from .sources_aitech import SECBroadEventsAdapter


def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"ok: {name}")


def main():
    tickers = set(W.TICKERS)
    frag_keys = set(W.WATCHLIST_NAME_FRAGMENTS)

    missing = tickers - frag_keys
    _check(f"every TICKER has a name fragment (missing={sorted(missing)})", not missing)

    extra = frag_keys - tickers
    _check(f"no orphan fragments without a ticker (extra={sorted(extra)})", not extra)

    _check("all fragments are non-empty lowercase strings",
           all(isinstance(v, str) and v and v == v.lower()
               for v in W.WATCHLIST_NAME_FRAGMENTS.values()))

    # The adapter's skip-filter must BE the derived set (no hardcoded drift).
    _check("SECBroadEventsAdapter._WATCHLIST_NAMES == fragment values",
           SECBroadEventsAdapter._WATCHLIST_NAMES
           == frozenset(W.WATCHLIST_NAME_FRAGMENTS.values()))

    # Spot-check the skip logic on a watchlist legal name and an off-watchlist one.
    adp = SECBroadEventsAdapter()
    _check("watchlist company is skipped (NVIDIA Corp)",
           adp._is_watchlist("NVIDIA Corp"))
    _check("off-watchlist AI company is not skipped (Cohere Inc)",
           not adp._is_watchlist("Cohere Inc"))

    print("\nALL WATCHLIST-SYNC TESTS PASSED")


if __name__ == "__main__":
    main()
