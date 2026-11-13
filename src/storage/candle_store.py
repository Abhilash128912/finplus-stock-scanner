"""
FinPlus+ Independent Candle Store & Real-Time Candle Synthesizer
================================================================
Stores, synthesizes, and serves high-fidelity historical and real-time intraday
OHLCV candlestick bars directly from INDstocks broker live ticks.
Eliminates reliance on delayed external chart feeds.
Outputs clean time-series data for TradingView Lightweight Charts canvas.
"""

import os
import os
import sqlite3
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
import pandas as pd

from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("CandleStore")

DB_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent.parent / "data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "candles.db"


class CandleStore:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    timestamp_ist TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY (symbol, timeframe, epoch)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_candles_lookup 
                ON candles(symbol, timeframe, epoch ASC)
            """)
            conn.commit()

    def save_dataframe_candles(self, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        """Saves historical DataFrame candles (with DatetimeIndex and Open/High/Low/Close/Volume)."""
        if df is None or df.empty:
            return 0

        clean_sym = symbol.split(" ")[0].upper()
        records = []
        for idx, row in df.iterrows():
            try:
                # Convert index to UTC timestamp
                if hasattr(idx, 'timestamp'):
                    epoch = int(idx.timestamp())
                    dt_utc = idx.tz_convert('UTC') if idx.tz else idx.tz_localize('UTC')
                else:
                    dt = pd.to_datetime(idx)
                    epoch = int(dt.timestamp())
                    dt_utc = dt

                ist_dt = dt_utc + datetime.timedelta(hours=5, minutes=30)
                records.append((
                    clean_sym,
                    timeframe,
                    epoch,
                    dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ist_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    round(float(row.get("Open", row.get("open", 0.0))), 2),
                    round(float(row.get("High", row.get("high", 0.0))), 2),
                    round(float(row.get("Low", row.get("low", 0.0))), 2),
                    round(float(row.get("Close", row.get("close", 0.0))), 2),
                    int(row.get("Volume", row.get("volume", 0)))
                ))
            except Exception:
                continue

        if not records:
            return 0

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO candles 
                (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records)
            conn.commit()

        return len(records)

    def update_live_candle_bar(
        self,
        symbol: str,
        timeframe: str,
        ltp: float,
        tick_volume: int = 0,
        ist_now: Optional[datetime.datetime] = None
    ) -> Dict[str, Any]:
        """
        Synthesizes / updates the active ongoing candle bar directly from live INDmoney tick.
        Rolls over automatically at 15m/5m timeframe boundaries.
        """
        clean_sym = symbol.split(" ")[0].upper()
        if not ist_now:
            ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)

        # Calculate bar start bucket
        tf_mins = 15 if timeframe == "15m" else (5 if timeframe == "5m" else 1)
        minute_bucket = (ist_now.minute // tf_mins) * tf_mins
        bar_start_ist = ist_now.replace(minute=minute_bucket, second=0, microsecond=0)
        epoch = int(bar_start_ist.timestamp())

        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT open, high, low, close, volume 
                FROM candles 
                WHERE symbol = ? AND timeframe = ? AND epoch = ?
            """, (clean_sym, timeframe, epoch)).fetchone()

            if row:
                new_open = row["open"]
                new_high = max(row["high"], ltp)
                new_low = min(row["low"], ltp)
                new_close = ltp
                new_vol = row["volume"] + tick_volume
            else:
                new_open = ltp
                new_high = ltp
                new_low = ltp
                new_close = ltp
                new_vol = tick_volume

            utc_dt = bar_start_ist - datetime.timedelta(hours=5, minutes=30)
            conn.execute("""
                INSERT OR REPLACE INTO candles 
                (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                clean_sym,
                timeframe,
                epoch,
                utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                bar_start_ist.strftime("%Y-%m-%d %H:%M:%S"),
                new_open,
                new_high,
                new_low,
                new_close,
                new_vol
            ))
            conn.commit()

        return {
            "time": epoch,
            "open": new_open,
            "high": new_high,
            "low": new_low,
            "close": new_close,
            "volume": new_vol
        }

    def get_latest_candle_timestamp(self, symbol: str, timeframe: str = "15m") -> Optional[int]:
        """Returns the latest bar epoch timestamp for a given symbol and timeframe."""
        clean_sym = symbol.split(" ")[0].upper()
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT MAX(epoch) FROM candles WHERE symbol = ? AND timeframe = ?
            """, (clean_sym, timeframe)).fetchone()
            return row[0] if row and row[0] is not None else None

    def get_candles(self, symbol: str, timeframe: str = "15m", limit: int = 1000) -> List[Dict[str, Any]]:
        """
        Retrieves candles formatted specifically for TradingView Lightweight Charts:
        [{ 'time': epoch, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': int }]
        """
        clean_sym = symbol.split(" ")[0].upper()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT epoch, open, high, low, close, volume, timestamp_ist
                FROM candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY epoch DESC
                LIMIT ?
            """, (clean_sym, timeframe, limit))
            rows = cursor.fetchall()

        # Reverse to chronological order (oldest to newest) for chart plotting
        chart_bars = []
        for r in reversed(rows):
            chart_bars.append({
                "time": r["epoch"],
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
                "ist": r["timestamp_ist"]
            })
        return chart_bars

    def get_candles_as_dataframe(self, symbol: str, timeframe: str = "15m", limit: int = 1000) -> pd.DataFrame:
        """Retrieves candles formatted as a pandas DataFrame matching yfinance structure."""
        bars = self.get_candles(symbol, timeframe=timeframe, limit=limit)
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        df.rename(columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume"
        }, inplace=True)
        df.index = pd.to_datetime(df["time"], unit="s")
        return df[["Open", "High", "Low", "Close", "Volume"]]
