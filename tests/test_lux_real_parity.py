"""
Acceptance Test Suite for Real LuxAlgo TradingView Parity.
Reads CSV files from tests/data/tradingview/ containing exported TradingView values
(box_top, box_bottom, start_bar, end_bar) alongside 15m candle data.
Skips cleanly with an explicit message if the folder is empty.
"""

from pathlib import Path
import pytest
import pandas as pd
from src.analytics.range_detector_engine import RangeDetectorEngine

TV_DATA_DIR = Path(__file__).resolve().parent / "data" / "tradingview"

def test_lux_real_parity_against_tradingview_export():
    """Validates RangeDetectorEngine against real exported TradingView LuxAlgo ranges."""
    csv_files = list(TV_DATA_DIR.glob("*.csv"))
    if not csv_files:
        pytest.skip("No TradingView export CSV files found in tests/data/tradingview/. Awaiting user files.")

    for csv_file in csv_files:
        df = pd.read_csv(csv_file, parse_dates=["timestamp"], index_col="timestamp")
        # Ensure mandatory columns exist in test fixture
        required_cols = {"Open", "High", "Low", "Close", "Volume", "tv_box_top", "tv_box_bottom"}
        if not required_cols.issubset(set(df.columns)):
            pytest.skip(f"CSV {csv_file.name} missing required comparison columns: {required_cols - set(df.columns)}")

        engine = RangeDetectorEngine(length=20, mult=1.0, atr_len=100)
        res = engine.evaluate_instrument(df, {"symbol": csv_file.stem})
        
        expected_top = df["tv_box_top"].dropna().iloc[-1]
        expected_bot = df["tv_box_bottom"].dropna().iloc[-1]
        
        assert abs(res["range_top"] - expected_top) <= 0.05 * expected_top, f"Mismatch in box top for {csv_file.name}"
        assert abs(res["range_bottom"] - expected_bot) <= 0.05 * expected_bot, f"Mismatch in box bottom for {csv_file.name}"
