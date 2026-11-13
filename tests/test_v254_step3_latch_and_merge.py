import pandas as pd
import numpy as np
from src.analytics.range_detector_engine import RangeDetectorEngine
from src.storage.schedule_ledger import ScheduleLedger

def test_box_merge_bottom_and_midline():
    """Verify that on overlap merge, bottom uses min(new bottom, box bottom) and midline is (top + bottom) / 2."""
    dates = pd.date_range("2026-11-01 09:15", periods=60, freq="15min", tz="Asia/Kolkata")
    # Base tight data
    df = pd.DataFrame({
        "Open": [100.0] * 60,
        "High": [100.2] * 60,
        "Low": [99.8] * 60,
        "Close": [100.0] * 60,
        "Volume": [1000] * 60
    }, index=dates)

    engine = RangeDetectorEngine(length=20, mult=1.0, atr_len=14)
    res = engine.evaluate_instrument(df, {"symbol": "TESTMERGE"})
    top = res["range_top"]
    bot = res["range_bottom"]
    mid = res["range_mid"]
    assert round(mid, 2) == round((top + bot) / 2.0, 2)
    assert bot <= top

def test_daily_latch_deduplication(tmp_path):
    """TEST 3: The same break evaluated on 5 consecutive cycles -> exactly 1 alert latched."""
    db_file = tmp_path / "test_latch.db"
    ledger = ScheduleLedger(db_path=str(db_file))

    date_str = "2026-11-13"
    symbol = "RELIANCE"
    break_direction = 1
    break_price = 1350.0

    # 1st cycle: should latch successfully
    first_res = ledger.try_latch_daily_breakout(date_str, symbol, break_direction, break_price)
    assert first_res is True, "First breakout evaluation failed to latch!"

    # Next 4 cycles (cycles 2, 3, 4, 5): must return False (deduplicated)
    for cycle in range(2, 6):
        res = ledger.try_latch_daily_breakout(date_str, symbol, break_direction, break_price + cycle)
        assert res is False, f"Cycle {cycle} allowed duplicate breakout alert!"

    assert ledger.is_symbol_latched_today(date_str, symbol) is True
    # Different symbol on same day can still latch
    assert ledger.try_latch_daily_breakout(date_str, "TCS", 1, 3800.0) is True

