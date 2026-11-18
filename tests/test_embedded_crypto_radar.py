"""
Tests for Embedded 24/7 Delta Crypto Radar in FinPlus+ Stock Scanner
"""

import pytest
from src.crypto.crypto_config import load_contracts
from src.crypto.crypto_scheduler import CryptoScheduler
from src.crypto.multi_timeframe_coiling import MultiTimeframeCoilingEngine
from src.crypto.crypto_paper_engine import CryptoPaperEngine

def test_contracts_loaded():
    contracts = load_contracts()
    assert len(contracts) >= 5
    symbols = [c["symbol"] for c in contracts]
    assert "ETHUSD" in symbols
    assert "SOLUSD" in symbols
    assert "OILUSD" in symbols

def test_stop_loss_widened_to_safe_distance():
    engine = MultiTimeframeCoilingEngine()
    # Mock candles for breakout
    candles_15m = [
        {"time": 1000 + i * 900, "open": 64000.0, "high": 64100.0, "low": 63900.0, "close": 64000.0, "volume": 100.0}
        for i in range(50)
    ]
    # Final candle breaches box top
    candles_15m[-1]["close"] = 64500.0
    candles_15m[-1]["high"] = 64550.0
    candles_15m[-1]["volume"] = 500.0

    res = engine.analyze_symbol(
        symbol="ETHUSD",
        candles_15m=candles_15m,
        candles_5m=candles_15m[-20:],
        ltp_override=64500.0
    )

    if res.get("action") == "BUY_LONG":
        sl = res["stop_loss"]
        ltp = res["ltp"]
        dist_pct = ((ltp - sl) / ltp) * 100.0
        # Assert stop loss is at least 1.5% and not a choke stop (previously was 0.06% - 0.3%)
        assert dist_pct >= 1.45, f"Stop loss distance {dist_pct:.2f}% is too narrow!"
        assert dist_pct <= 3.55, f"Stop loss distance {dist_pct:.2f}% is too wide!"

def test_paper_engine_breakeven_and_trailing():
    paper = CryptoPaperEngine(db_path=":memory:")
    pos = paper.open_position(
        symbol="ETHUSD",
        action="BUY_LONG",
        entry_price=3000.0,
        stop_loss=2946.0, # 1.8% SL
        contract_value=0.01,
        default_qty=10
    )
    assert pos is not None
    assert pos["current_sl"] == 2946.0
    assert pos["breakeven_latched"] == 0

    # +0.80% gain: Should NOT prematurely latch breakeven (previously did at +0.50%)
    closed, trailed = paper.update_positions({"ETHUSD": 3024.0})
    assert paper.active_positions[pos["position_id"]]["breakeven_latched"] == 0
    assert paper.active_positions[pos["position_id"]]["current_sl"] == 2946.0

    # +1.60% gain: Should now latch breakeven
    closed, trailed = paper.update_positions({"ETHUSD": 3048.0})
    assert paper.active_positions[pos["position_id"]]["breakeven_latched"] == 1
    assert paper.active_positions[pos["position_id"]]["current_sl"] == 3000.0

    # +3.00% gain: High-water trailing should advance to 2.0% behind peak
    closed, trailed = paper.update_positions({"ETHUSD": 3090.0})
    expected_trail = round(3090.0 * 0.980, 4)
    assert paper.active_positions[pos["position_id"]]["current_sl"] == expected_trail
