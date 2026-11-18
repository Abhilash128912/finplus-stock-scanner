import os
import pytest
from src.utils.sensex_feed import get_live_sensex_spot
from src.paper.live_paper_tracker import LivePaperTracker
from src.crypto.crypto_paper_engine import CryptoPaperEngine
from src.feeds.fno_contract_master import FNOContractMaster


def test_sensex_live_spot_no_82300_fallback():
    """Verify Sensex spot resolves live from INDstocks and never falls back to 82,300."""
    from unittest.mock import patch
    from src.paper.indmoney_live_feed import INDMoneyLiveFeed
    from src.utils.sensex_feed import get_sensex_spot_source
    with patch.object(INDMoneyLiveFeed, "fetch_batch_quotes", return_value={"SENSEX": {"ltp": 82142.93, "change_pct": -0.47}}):
        spot, chg = get_live_sensex_spot()
        assert spot > 50000, f"Expected valid Sensex spot > 50000, got {spot}"
        assert abs(spot - 82300.0) > 0.01, f"Sensex spot must not be hardcoded 82300.0, got {spot}"
        assert get_sensex_spot_source() == "INDSTOCKS"


def test_midcap_nifty_contract_master_uses_correct_underlying():
    """Verify MIDCPNIFTY uses NIFTY_MID_SELECT.NS and lot size 120."""
    master = FNOContractMaster()
    uni = master.load_universe(top_n_equities=10)
    mid = next((x for x in uni if x.get("symbol") == "MIDCPNIFTY"), None)
    assert mid is not None, "MIDCPNIFTY must exist in universe"
    assert mid.get("yf_ticker") == "NIFTY_MID_SELECT.NS"
    assert mid.get("lot_size") == 120


def test_unified_max_30_active_positions_ceiling(tmp_path):
    """Verify that total active trades cannot exceed 30 across tracker and crypto engine."""
    tracker_db = str(tmp_path / "paper_test.db")
    crypto_db = str(tmp_path / "crypto_test.db")

    tracker = LivePaperTracker(db_path=tracker_db)
    crypto = CryptoPaperEngine(db_path=crypto_db)

    tracker.crypto_engine = crypto
    crypto.main_paper_tracker = tracker

    # Pre-populate 25 positions in tracker
    for i in range(25):
        tracker.active_positions[f"POS_STOCK_{i}"] = {
            "position_id": f"POS_STOCK_{i}",
            "symbol": f"STOCK_{i}",
            "sector": "INTRADAY_CASH",
            "status": "OPEN",
            "entry_time_ist": f"2026-11-18 09:{i:02d}:00 AM IST"
        }

    # Pre-populate 5 positions in crypto
    for j in range(5):
        crypto.active_positions[f"POS_CRYPTO_{j}"] = {
            "position_id": f"POS_CRYPTO_{j}",
            "symbol": f"CRYPTO_{j}",
            "action": "BUY_LONG",
            "status": "OPEN",
            "entry_time_ist": f"2026-11-18 09:{j:02d}:00 AM IST"
        }

    # Total is now exactly 30
    assert tracker.get_total_open_count() == 30

    # 31st trade in equity tracker must be blocked
    res_stock = tracker.open_position(
        source_app="TEST",
        sector="INTRADAY_CASH",
        symbol="EXCESS_STOCK",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0
    )
    assert res_stock is None, "Should block 31st trade in main tracker"

    # 31st trade in crypto engine must be blocked
    res_crypto = crypto.open_position(
        symbol="EXCESS_CRYPTO_USD",
        action="BUY_LONG",
        entry_price=100.0,
        stop_loss=99.0
    )
    assert res_crypto is None, "Should block 31st trade in crypto engine"
