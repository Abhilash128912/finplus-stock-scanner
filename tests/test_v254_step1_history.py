import pandas as pd
from src.analytics.range_detector_engine import RangeDetectorEngine

def test_history_rule_atr100_runs_atr500_skipped():
    dates = pd.date_range("2026-11-01 09:15", periods=150, freq="15min", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "Open": [100.0] * 150,
        "High": [101.0] * 150,
        "Low": [99.0] * 150,
        "Close": [100.0] * 150,
        "Volume": [1000] * 150
    }, index=dates)

    meta = {"symbol": "TEST150", "strike_step": 10, "importance": "HIGH", "category": "STOCK"}

    # ATR 100 needs 100 + 20 + 2 = 122 bars -> 150 bars should succeed and return normal evaluation
    engine100 = RangeDetectorEngine(length=20, mult=1.0, atr_len=100)
    res100 = engine100.evaluate_instrument(df, meta)
    assert res100.get("status") != "INSUFFICIENT_DATA"
    assert "ignition_state" in res100
    assert engine100.skipped_insufficient_history_count == 0

    # ATR 500 needs 500 + 20 + 2 = 522 bars -> 150 bars should be skipped with counted reason
    engine500 = RangeDetectorEngine(length=20, mult=1.0, atr_len=500)
    res500 = engine500.evaluate_instrument(df, meta)
    assert res500.get("status") == "INSUFFICIENT_DATA"
    assert res500["bars_count"] == 150
    assert res500["required_bars"] == 522
    assert engine500.skipped_insufficient_history_count == 1
