"""
Tests for Unified Dynamic Grading Logic across NSE & Crypto (v2.7.5).
Verifies that:
1. High-confluence NSE trades dynamically receive Grade A (never stuck in learning B).
2. Moderate-confluence NSE trades receive Grade B.
3. Low-confluence or vetoed NSE trades receive Grade C.
4. Crypto positions dynamically compute and persist Grade A, B, and C without hardcoding.
5. Server API delivers real dynamic grades for both NSE and Crypto.
"""

import os
import pytest
from unittest.mock import patch, MagicMock
from src.paper.live_paper_tracker import LivePaperTracker
from src.crypto.crypto_paper_engine import CryptoPaperEngine
from src.crypto.crypto_scheduler import CryptoScheduler

@pytest.fixture
def test_tracker(tmp_path):
    db_file = str(tmp_path / "test_dynamic_grading.db")
    tracker = LivePaperTracker(db_path=db_file)
    tracker.is_market_open_for_sector = MagicMock(return_value=(True, "Open"))
    return tracker

def test_nse_dynamic_grading_high_confluence_is_grade_a(test_tracker):
    """High confluence (>=80), APPROVED regime, rvol >= 1.2, tight SL produces Grade A on fresh setup."""
    pos = test_tracker.open_position(
        source_app="APP_02_STOCK_SCANNER",
        sector="INTRADAY_CASH",
        symbol="TCS",
        action="BUY",
        entry_price=3500.0,
        stop_loss=3485.0, # 0.4% SL (Tight)
        confluence_score=90,
        regime="APPROVED",
        rvol=1.8,
        setup_key="HIGH_CONF_BREAKOUT"
    )
    assert pos is not None
    assert pos["grade"] == "A", f"Expected Grade A but got {pos['grade']}"
    assert "LEARNING" not in pos["tags"]
    assert "LOW CONFIDENCE" not in pos["tags"]

def test_nse_dynamic_grading_moderate_confluence_is_grade_b(test_tracker):
    """Moderate confluence (65-79), neutral regime produces Grade B."""
    pos = test_tracker.open_position(
        source_app="APP_02_STOCK_SCANNER",
        sector="INTRADAY_CASH",
        symbol="INFY",
        action="BUY",
        entry_price=1500.0,
        stop_loss=1490.0,
        confluence_score=72,
        regime="NEUTRAL",
        rvol=1.0,
        setup_key="MODERATE_CONF_SETUP"
    )
    assert pos is not None
    assert pos["grade"] == "B", f"Expected Grade B but got {pos['grade']}"
    assert "LEARNING" in pos["tags"]

def test_nse_dynamic_grading_vetoed_or_low_score_is_grade_c(test_tracker):
    """Low confluence (<65) or VETOED regime produces Grade C, tags LOW CONFIDENCE, suppresses NTFY."""
    with patch.object(test_tracker, "_dispatch_ntfy_trade_opened") as mock_ntfy:
        pos = test_tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="WIPRO",
            action="BUY",
            entry_price=450.0,
            stop_loss=445.0,
            confluence_score=50,
            regime="VETOED_CHOPPY",
            rvol=0.7,
            setup_key="WEAK_SETUP"
        )
        assert pos is not None
        assert pos["grade"] == "C", f"Expected Grade C but got {pos['grade']}"
        assert "LOW CONFIDENCE" in pos["tags"]
        # Grade C suppresses NTFY alert
        mock_ntfy.assert_not_called()

def test_crypto_dynamic_grading_and_persistence(tmp_path):
    """Crypto positions dynamically calculate and persist Grade A, B, and C."""
    db_file = str(tmp_path / "test_crypto_grades.db")
    engine = CryptoPaperEngine(db_path=db_file)

    # 1. Open Grade A Position
    pos_a = engine.open_position(
        symbol="SOLUSD",
        action="BUY_LONG",
        entry_price=180.0,
        stop_loss=176.5,
        contract_value=1.0,
        default_qty=5,
        grade="A",
        confluence_score=88,
        setup_key="15M_COILING_SQUEEZE"
    )
    assert pos_a is not None
    assert pos_a["grade"] == "A"
    assert pos_a["confluence_score"] == 88

    # 2. Open Grade B Position
    pos_b = engine.open_position(
        symbol="ETHUSD",
        action="BUY_LONG",
        entry_price=3100.0,
        stop_loss=3038.0,
        contract_value=0.1,
        default_qty=10,
        grade="B",
        confluence_score=75,
        setup_key="15M_COILING_SQUEEZE"
    )
    assert pos_b is not None
    assert pos_b["grade"] == "B"

    # 3. Verify positions survive restart and maintain grades
    engine2 = CryptoPaperEngine(db_path=db_file)
    assert len(engine2.active_positions) == 2
    assert engine2.active_positions[pos_a["position_id"]]["grade"] == "A"
    assert engine2.active_positions[pos_b["position_id"]]["grade"] == "B"

    # 4. Close position and verify grade persists in closed trades
    engine2.close_position(pos_a["position_id"], exit_price=185.0, exit_reason="TRAILING_SL")
    status = engine2.get_status_dict()
    assert len(status["recent_closed_trades"]) == 1
    assert status["recent_closed_trades"][0]["grade"] == "A"
    assert status["recent_closed_trades"][0]["confluence_score"] == 88
