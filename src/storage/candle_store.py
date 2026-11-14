"""
FinPlus+ Independent Candle Store & Real-Time Candle Synthesizer
================================================================
Stores, synthesizes, and serves high-fidelity historical and real-time intraday
OHLCV candlestick bars directly from INDstocks broker live ticks.
Eliminates reliance on delayed external chart feeds.
Outputs clean time-series data for TradingView Lightweight Charts canvas.
"""

import os
import sqlite3
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Tuple
import pandas as pd

from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("CandleStore")

DB_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent.parent / "data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "candles.db"


class CandleStore:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is not None:
            self.db_path = db_path
        else:
            data_dir = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent.parent / "data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(data_dir / "candles.db")
        self.symbol_last_extremes: Dict[str, Tuple[Optional[float], Optional[float], int]] = {}
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
        ist_now: Optional[datetime.datetime] = None,
        day_high: Optional[float] = None,
        day_low: Optional[float] = None,
        day_volume: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Synthesizes / updates the active ongoing candle bar directly from live INDmoney tick.
        Rolls over automatically at 15m/5m timeframe boundaries.
        Captures wicks using quote's day_high/day_low fields when day extremes expand inside the bar.
        """
        clean_sym = symbol.split(" ")[0].upper()
        if not ist_now:
            ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)

        # Calculate bar start bucket
        tf_mins = 15 if timeframe == "15m" else (5 if timeframe == "5m" else 1)
        minute_bucket = (ist_now.minute // tf_mins) * tf_mins
        bar_start_ist = ist_now.replace(minute=minute_bucket, second=0, microsecond=0)
        epoch = int(bar_start_ist.timestamp())

        # Check day extreme change for wick capture
        bar_key = f"{clean_sym}_{timeframe}"
        last_day_h, last_day_l, last_epoch = self.symbol_last_extremes.get(bar_key, (None, None, 0))

        wick_high = ltp
        wick_low = ltp

        if epoch != last_epoch:
            # New bar interval: record baseline extremes
            self.symbol_last_extremes[bar_key] = (day_high, day_low, epoch)
            if day_high is not None and day_high > ltp:
                wick_high = max(wick_high, day_high)
            if day_low is not None and 0 < day_low < ltp:
                wick_low = min(wick_low, day_low)
        else:
            # Ongoing bar: if day_high expanded, stock wicked to new high
            if day_high is not None:
                if last_day_h is not None and day_high > last_day_h:
                    wick_high = max(wick_high, day_high)
                self.symbol_last_extremes[bar_key] = (day_high, self.symbol_last_extremes[bar_key][1], epoch)

            # If day_low expanded downwards, stock wicked to new low
            if day_low is not None and day_low > 0:
                if last_day_l is not None and day_low < last_day_l:
                    wick_low = min(wick_low, day_low)
                self.symbol_last_extremes[bar_key] = (self.symbol_last_extremes[bar_key][0], day_low, epoch)

        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT open, high, low, close, volume 
                FROM candles 
                WHERE symbol = ? AND timeframe = ? AND epoch = ?
            """, (clean_sym, timeframe, epoch)).fetchone()

            if row:
                new_open = row["open"]
                new_high = max(row["high"], wick_high)
                new_low = min(row["low"], wick_low)
                new_close = ltp
                new_vol = row["volume"] + tick_volume
            else:
                new_open = ltp
                new_high = max(ltp, wick_high)
                new_low = min(ltp, wick_low)
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

    def get_history_coverage(
        self,
        tracked_symbols: Optional[List[str]] = None,
        timeframe: str = "15m",
        target_bars: int = 522,
        listing_too_new_symbols: Optional[Set[str]] = None
    ) -> Dict[str, Any]:
        """
        Computes granular history coverage metrics for distinct tracked tickers:
        - total bars stored
        - count and % of symbols with >= 122 bars (ATR 100 threshold)
        - count and % of symbols with >= target_bars (ATR 500 threshold, default 522)
        - recognizes LISTING_TOO_NEW symbols (recent IPOs with all available bars since listing) as complete
        - oldest and newest bar timestamps
        - list of symbols missing or short of target_bars (excluding LISTING_TOO_NEW)
        """
        if listing_too_new_symbols is None:
            listing_too_new_symbols = set()
        else:
            listing_too_new_symbols = {s.split(" ")[0].upper() for s in listing_too_new_symbols}

        with self._get_connection() as conn:
            agg_row = conn.execute("""
                SELECT COUNT(*) as total_bars, MIN(timestamp_ist) as oldest_bar, MAX(timestamp_ist) as newest_bar
                FROM candles
                WHERE timeframe = ?
            """, (timeframe,)).fetchone()
            
            total_bars = agg_row["total_bars"] if agg_row and agg_row["total_bars"] else 0
            oldest_bar = agg_row["oldest_bar"] if agg_row and agg_row["oldest_bar"] else None
            newest_bar = agg_row["newest_bar"] if agg_row and agg_row["newest_bar"] else None

            rows = conn.execute("""
                SELECT symbol, COUNT(*) as cnt, MIN(epoch) as min_ep, MAX(epoch) as max_ep
                FROM candles
                WHERE timeframe = ?
                GROUP BY symbol
            """, (timeframe,)).fetchall()
            counts = {r["symbol"].upper(): r["cnt"] for r in rows}
            min_epochs = {r["symbol"].upper(): r["min_ep"] for r in rows}

        if tracked_symbols is None:
            symbols_to_check = list(counts.keys())
        else:
            # Preserve tracked_symbols order while deduplicating distinct uppercase tickers
            seen = set()
            symbols_to_check = []
            for s in tracked_symbols:
                if s:
                    clean = s.split(" ")[0].upper()
                    if clean not in seen:
                        seen.add(clean)
                        symbols_to_check.append(clean)

        total_tracked = len(symbols_to_check) if symbols_to_check else 1

        # Automatically detect newly listed symbols if they hold continuous bars since recent listing (< 45 trading days ago)
        now_epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        detected_new_listings = set(listing_too_new_symbols)
        for s in symbols_to_check:
            cnt = counts.get(s, 0)
            if cnt > 0 and cnt < target_bars:
                min_ep = min_epochs.get(s, 0)
                # If oldest bar is within the last 40 calendar days (recent listing) and has >= 50 bars
                if min_ep and (now_epoch - min_ep) <= (40 * 86400) and cnt >= 50:
                    detected_new_listings.add(s)

        cnt_122 = sum(1 for s in symbols_to_check if counts.get(s, 0) >= 122 or s in detected_new_listings)
        cnt_522_raw = sum(1 for s in symbols_to_check if counts.get(s, 0) >= target_bars)
        # Complete count includes symbols >= target_bars PLUS valid new listings
        cnt_522_complete = sum(1 for s in symbols_to_check if counts.get(s, 0) >= target_bars or s in detected_new_listings)
        
        missing_or_short = [
            s for s in symbols_to_check
            if counts.get(s, 0) < target_bars and s not in detected_new_listings
        ]

        pct_122 = round((cnt_122 / total_tracked) * 100.0, 1) if total_tracked > 0 else 0.0
        pct_522 = round((cnt_522_complete / total_tracked) * 100.0, 1) if total_tracked > 0 else 0.0

        return {
            "total_bars": total_bars,
            "tracked_symbols_count": len(symbols_to_check),
            "symbols_122_bars": {
                "count": cnt_122,
                "percentage": pct_122
            },
            "symbols_522_bars": {
                "count": cnt_522_complete,
                "percentage": pct_522,
                "raw_522_count": cnt_522_raw
            },
            "coverage_522_pct": pct_522,
            "listing_too_new_count": len(detected_new_listings),
            "listing_too_new_symbols": sorted(list(detected_new_listings)),
            "oldest_bar_time": oldest_bar,
            "newest_bar_time": newest_bar,
            "missing_or_short_symbols": missing_or_short,
            "db_path": self.db_path
        }

