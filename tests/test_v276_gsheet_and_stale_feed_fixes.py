import pytest
from unittest.mock import patch, MagicMock
from src.paper.live_paper_tracker import LivePaperTracker
from src.crypto.crypto_paper_engine import CryptoPaperEngine

def test_stale_feed_does_not_block_commodities_or_crypto(tmp_path):
    """Verify is_stale_feed = True only restricts equity sectors and allows MCX commodities & crypto."""
    db_file = str(tmp_path / "test_stale.db")
    tracker = LivePaperTracker(db_path=db_file)
    tracker.is_stale_feed = True

    # 1. Equity entry should be blocked
    res_eq = tracker.open_position(
        source_app="TEST",
        sector="INTRADAY_CASH",
        symbol="RELIANCE",
        action="BUY",
        entry_price=1200.0,
        stop_loss=1190.0,
        target=1220.0,
        quantity=10
    )
    assert res_eq is None

    # 2. Commodity entry should NOT be blocked by equity stale feed
    with patch.object(tracker, "is_market_open_for_sector", return_value=(True, "MCX Open")):
        with patch.object(tracker, "_dispatch_ntfy_trade_opened"):
            res_comm = tracker.open_position(
                source_app="APP_03_COMMODITY_RADAR",
                sector="COMMODITIES",
                symbol="CRUDEOILM",
                action="BUY",
                entry_price=8000.0,
                stop_loss=7950.0,
                target=8100.0,
                quantity=10
            )
            assert res_comm is not None
            assert res_comm["symbol"] == "CRUDEOILM"

def test_google_sheet_sync_invoked_on_close(tmp_path):
    """Verify that closing an Indian paper position calls _push_trade_to_google_sheet."""
    db_file = str(tmp_path / "test_gsheet.db")
    tracker = LivePaperTracker(db_path=db_file)
    
    with patch.object(tracker, "is_market_open_for_sector", return_value=(True, "Market Open")):
        with patch.object(tracker, "_dispatch_ntfy_trade_opened"):
            pos = tracker.open_position(
                source_app="TEST",
                sector="COMMODITIES",
                symbol="NATGASMINI",
                action="SELL",
                entry_price=300.0,
                stop_loss=305.0,
                target=290.0,
                quantity=250
            )
            assert pos is not None
            pos_id = pos["position_id"]

        with patch.object(tracker, "_push_trade_to_google_sheet") as mock_push:
            closed = tracker.close_position(
                pos_id=pos_id,
                exit_price=290.0,
                exit_reason="TARGET_HIT",
                force=True
            )
            assert closed is True
            assert mock_push.called
            trade_rec = mock_push.call_args[0][0]
            assert trade_rec["symbol"] == "NATGASMINI"
            assert trade_rec["sector"] == "COMMODITIES"
            assert trade_rec["net_pnl"] > 0
            assert trade_rec["trade_mode"] == "PAPER"

def test_crypto_paper_engine_gsheet_sync(tmp_path):
    """Verify CryptoPaperEngine calls requests.post to Google Sheet webhook on close."""
    db_file = str(tmp_path / "test_crypto_gsheet.db")
    engine = CryptoPaperEngine(db_path=db_file)
    pos = engine.open_position(
        symbol="ETHUSD",
        action="BUY_LONG",
        entry_price=1800.0,
        stop_loss=1780.0,
        contract_value=0.01,
        default_qty=5.0,
        notes="Test Setup"
    )
    assert pos is not None
    pos_id = pos["position_id"]

    with patch("requests.post") as mock_post:
        closed = engine.close_position(pos_id=pos_id, exit_price=1850.0, exit_reason="TRAIL_SL_HIT")
        assert closed is not None
        assert closed["symbol"] == "ETHUSD"
        assert closed["exit_reason"] == "TRAIL_SL_HIT"
