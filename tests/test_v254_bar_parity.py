"""
Tests for Phase 2C Bar Parity Recorder & Extreme Wick Capture.
Verifies:
1. Simulated INDmoney bar with 0.5% deviation from Yahoo is flagged as is_within_tolerance = 0.
2. Missing INDmoney bar is recorded with missing_source = 'INDMONEY_MISSING' and counted.
3. READY rule reports NOT READY on session 1 and READY TO SWITCH OFF YAHOO after 2 consecutive qualifying sessions.
4. CandleStore extreme wick capture updates bar high/low when day_high/day_low expands inside the bar interval.
"""
import os
import pytest
import sqlite3
import tempfile
import datetime
from src.storage.bar_parity_recorder import BarParityRecorder
from src.storage.candle_store import CandleStore


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.remove(path)
    except Exception:
        pass


def test_deviation_tolerance_flagging(temp_db):
    """A bar with 0.5% deviation in close (> 0.1% tolerance) must be flagged is_within_tolerance = 0."""
    recorder = BarParityRecorder(db_path=temp_db)
    
    ind_bar = {
        "timestamp": 1731470400,
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 100.5, # 0.5% deviation from Yahoo's 100.0 (> 0.1% threshold)
        "volume": 1000
    }
    yf_bar = {
        "timestamp": 1731470400,
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 1000
    }
    
    res = recorder.record_bar_comparison("RELIANCE", "15m", 1731470400, ind_bar, yf_bar)
    assert res["is_within_tolerance"] == 0
    assert abs(res["close_dev_pct"] - 0.5) < 1e-3

    # Another bar within all tolerances
    good_ind = {
        "timestamp": 1731471300,
        "open": 100.02, # 0.02%
        "high": 102.1,  # 0.1%
        "low": 98.9,   # 0.1%
        "close": 100.05,# 0.05%
        "volume": 1020  # 2.0%
    }
    good_yf = {
        "timestamp": 1731471300,
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 1000
    }
    res_good = recorder.record_bar_comparison("RELIANCE", "15m", 1731471300, good_ind, good_yf)
    assert res_good["is_within_tolerance"] == 1


def test_missing_bar_accounting(temp_db):
    """Missing INDmoney bar must be stored with missing_source = 'INDMONEY_MISSING'."""
    recorder = BarParityRecorder(db_path=temp_db)
    
    yf_bar = {
        "timestamp": 1731470400,
        "open": 500.0,
        "high": 505.0,
        "low": 498.0,
        "close": 502.0,
        "volume": 5000
    }
    
    res = recorder.record_bar_comparison("TCS", "15m", 1731470400, ind_bar=None, yf_bar=yf_bar, date_str="2026-11-13")
    assert res["missing_source"] == "INDMONEY_MISSING"
    assert res["is_within_tolerance"] == 0

    # Summary must count 1 missing IND bar
    summary = recorder.get_daily_summary(date_str="2026-11-13")
    assert summary["missing_ind_bars"] == 1
    assert summary["total_compared_bars"] == 1
    assert summary["bars_within_tolerance_pct"] == 0.0


def test_ready_rule_two_consecutive_sessions(temp_db):
    """READY rule reports NOT READY on session 1 and READY TO SWITCH OFF YAHOO after 2 consecutive qualifying sessions."""
    recorder = BarParityRecorder(db_path=temp_db)

    # Session 1: Good bar
    bar_good = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000}
    recorder.record_bar_comparison("INFY", "15m", 1731211200, bar_good, bar_good, date_str="2026-11-10")
    summary_1 = recorder.get_daily_summary(date_str="2026-11-10")
    assert summary_1["ready_to_switch_off_yahoo"] is False
    assert "NOT READY" in summary_1["ready_status"]

    # Session 2: Good bar on consecutive date
    recorder.record_bar_comparison("INFY", "15m", 1731297600, bar_good, bar_good, date_str="2026-11-11")
    summary_2 = recorder.get_daily_summary(date_str="2026-11-11")
    assert summary_2["ready_to_switch_off_yahoo"] is True
    assert "READY TO SWITCH OFF YAHOO" in summary_2["ready_status"]


def test_candlestore_wick_capture(temp_db):
    """CandleStore extreme wick capture updates bar high and low when day_high or day_low expands."""
    cs = CandleStore(db_path=temp_db)
    
    fixed_time = datetime.datetime(2026, 11, 13, 9, 16, 0)
    
    # 1. First tick in a new 15m bar
    bar = cs.update_live_candle_bar(
        symbol="INFY",
        timeframe="15m",
        ltp=1500.0,
        tick_volume=100,
        ist_now=fixed_time,
        day_high=1505.0,
        day_low=1495.0
    )
    assert bar is not None
    assert bar["high"] == 1505.0
    assert bar["low"] == 1495.0

    # 2. Incoming tick has normal price 1502.0, but broker reports new day_high 1515.0 and day_low 1490.0
    fixed_time_2 = datetime.datetime(2026, 11, 13, 9, 20, 0)
    bar2 = cs.update_live_candle_bar(
        symbol="INFY",
        timeframe="15m",
        ltp=1502.0,
        tick_volume=50,
        ist_now=fixed_time_2,
        day_high=1515.0,
        day_low=1490.0
    )
    assert bar2["high"] == 1515.0 # Wick captured from day_high!
    assert bar2["low"] == 1490.0  # Wick captured from day_low!
    assert bar2["close"] == 1502.0
    assert bar2["volume"] == 150
