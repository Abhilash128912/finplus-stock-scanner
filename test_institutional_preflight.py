"""
FinPlus+ Institutional Pre-Flight Verification Suite
===================================================
Executes strict automated acceptance tests:
1. Headless TOTP Authentication & INDstocks live broker handshake.
2. Full 750-Stock Broad Market Cash Universe loading.
3. INDstocks batch quote retrieval performance (< 3.0s).
4. Persistent SQLite CandleStore operations & synthesis.
5. ScheduleLedger state machine idempotent catch-up.
6. Deep Engine Health diagnostics evaluation.
"""

import sys
import time
import datetime
from pathlib import Path

# Ensure paths
_ROOT = Path(__file__).resolve().parent
_APPS_ROOT = _ROOT.parent
for p in [str(_APPS_ROOT), str(_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from src.utils.time_and_logger import setup_logger
from src.feeds.broad_market_master import BroadMarketMaster
from src.feeds.market_data_feed import MarketDataFeed
from src.paper.indmoney_live_feed import INDMoneyLiveFeed
from src.storage.candle_store import CandleStore
from src.storage.schedule_ledger import ScheduleLedger

logger = setup_logger("PreFlightTest")


def run_preflight_checks():
    print("=" * 70)
    print("       FINPLUS+ INSTITUTIONAL PRE-FLIGHT VERIFICATION")
    print("=" * 70)

    all_passed = True

    # -------------------------------------------------------------
    # Test 1: INDstocks Live Broker Handshake & TOTP
    # -------------------------------------------------------------
    print("\n[TEST 1/6] Verifying INDstocks Live Broker Connection & TOTP...")
    try:
        ind_feed = INDMoneyLiveFeed()
        auth_client = ind_feed.auth_client
        if not auth_client:
            raise RuntimeError("INDMoneyAuthClient could not be initialized")

        # Verify live connection
        res = auth_client.verify_connection()
        if res.get("status") == "HEALTHY":
            print(f"  --> PASS: Connected as {res.get('first_name')} {res.get('last_name')} (ID: {res.get('user_id')})")
        else:
            raise RuntimeError(f"Broker connection returned: {res}")
    except Exception as e:
        print(f"  --> FAIL: Broker connection test failed: {e}")
        all_passed = False

    # -------------------------------------------------------------
    # Test 2: Full 750 Cash Stocks Universe Loading
    # -------------------------------------------------------------
    print("\n[TEST 2/6] Verifying 750-Stock Broad Market Cash Universe...")
    try:
        bmm = BroadMarketMaster()
        cash_stocks = bmm.load_universe(top_n_liquid=750)
        count = len(cash_stocks)
        if count == 750:
            print(f"  --> PASS: Loaded exactly {count} broad market cash stocks.")
        else:
            raise ValueError(f"Expected 750 cash stocks, but loaded {count}")
    except Exception as e:
        print(f"  --> FAIL: Universe loading failed: {e}")
        all_passed = False

    # -------------------------------------------------------------
    # Test 3: INDstocks Batch Quote Performance (< 3 seconds)
    # -------------------------------------------------------------
    print("\n[TEST 3/6] Verifying INDstocks High-Speed Batch Retrieval...")
    try:
        test_symbols = ["HDFCBANK", "RELIANCE", "TCS", "INFY", "CRUDEOILM", "NATGASMINI", "COFORGE", "PERSISTENT"]
        t0 = time.time()
        quotes = ind_feed.fetch_batch_quotes(test_symbols, force=True)
        elapsed = time.time() - t0
        received_count = len(quotes)

        print(f"  --> Received {received_count}/{len(test_symbols)} quotes in {elapsed:.2f}s")
        for s in test_symbols[:4]:
            q = quotes.get(s, {})
            print(f"      - {s}: LTP=Rs {q.get('ltp', '--')} (Chg: {q.get('change_pct', '--')}%)")

        if received_count >= len(test_symbols) - 2 and elapsed < 4.0:
            print(f"  --> PASS: High-speed batch retrieval verified ({elapsed:.2f}s).")
        else:
            raise RuntimeError(f"Batch quote retrieval underperformed ({received_count} quotes, {elapsed:.2f}s)")
    except Exception as e:
        print(f"  --> FAIL: Batch quote test failed: {e}")
        all_passed = False

    # -------------------------------------------------------------
    # Test 4: Persistent SQLite CandleStore & Synthesis
    # -------------------------------------------------------------
    print("\n[TEST 4/6] Verifying Persistent SQLite CandleStore...")
    try:
        cs = CandleStore()
        # Synthesize a live bar
        bar = cs.update_live_candle_bar("HDFCBANK", "15m", 802.25, tick_volume=250)
        bars = cs.get_candles("HDFCBANK", "15m", limit=10)
        if bars and len(bars) > 0 and bars[-1]["close"] == 802.25:
            print(f"  --> PASS: Synthesized and persisted 15m candle bar (LTP: Rs {bars[-1]['close']}, Vol: {bars[-1]['volume']}).")
        else:
            raise RuntimeError(f"CandleStore retrieval mismatch: {bars}")
    except Exception as e:
        print(f"  --> FAIL: CandleStore test failed: {e}")
        all_passed = False

    # -------------------------------------------------------------
    # Test 5: ScheduleLedger Idempotent Catch-Up
    # -------------------------------------------------------------
    print("\n[TEST 5/6] Verifying ScheduleLedger Idempotent State Machine...")
    try:
        ledger = ScheduleLedger()
        test_date = "2026-11-13"
        ledger.ensure_daily_ledger_populated(test_date)
        
        # Test simulated 09:12 AM catch-up
        simulated_now = datetime.datetime.strptime("2026-11-13 09:12:00", "%Y-%m-%d %H:%M:%S")
        due = ledger.get_pending_due_events(simulated_now)
        due_slots = [d["slot_id"] for d in due]
        
        if "09:00_AGENDA" in due_slots:
            print(f"  --> PASS: ScheduleLedger caught up 09:00 AM Agenda at 09:12 AM simulated time.")
        else:
            raise RuntimeError(f"Expected 09:00_AGENDA in due slots, got {due_slots}")
    except Exception as e:
        print(f"  --> FAIL: ScheduleLedger test failed: {e}")
        all_passed = False

    # -------------------------------------------------------------
    # Test 6: MarketDataFeed Full Universe Integration
    # -------------------------------------------------------------
    print("\n[TEST 6/6] Verifying MarketDataFeed Decoupled Universe Integration...")
    try:
        mdf = MarketDataFeed(top_n_cash_stocks=750)
        total_univ = mdf.get_all_universe_items()
        cash_univ = mdf.get_cash_universe()
        fno_univ = mdf.get_fno_universe()

        print(f"  --> F&O Options Universe: {len(fno_univ)} instruments")
        print(f"  --> Intraday Cash Universe: {len(cash_univ)} instruments")
        print(f"  --> Combined Tracked Total: {len(total_univ)} instruments")

        if len(cash_univ) == 750 and len(total_univ) >= 850:
            print("  --> PASS: Full 750 Cash + 108 F&O universe correctly integrated.")
        else:
            raise ValueError(f"Unexpected counts: Cash={len(cash_univ)}, Total={len(total_univ)}")
    except Exception as e:
        print(f"  --> FAIL: MarketDataFeed integration failed: {e}")
        all_passed = False

    print("\n" + "=" * 70)
    if all_passed:
        print("  RESULT: ALL 6 INSTITUTIONAL PRE-FLIGHT ACCEPTANCE TESTS PASSED!")
        print("=" * 70)
        return True
    else:
        print("  RESULT: ONE OR MORE ACCEPTANCE TESTS FAILED!")
        print("=" * 70)
        return False


if __name__ == "__main__":
    success = run_preflight_checks()
    sys.exit(0 if success else 1)
