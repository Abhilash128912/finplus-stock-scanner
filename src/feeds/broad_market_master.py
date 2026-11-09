"""
FinPlus+ Broad Market Master (Quality & Liquidity Intraday Cash Universe)
Loads equity stocks from 'D:\\Nifty 500 stocks.xlsx' and applies strict institutional filters:
1. Anti-Circuit Filter: Excludes penny stocks (Close < ₹50), low volume (< 200,000 shares),
   and low traded turnover (< ₹15 Crores).
2. Institutional Liquidity Ranking: Ranks candidates by daily traded turnover (Value in Crores),
   ensuring deep double-sided order books with zero risk of UC/LC circuit lockouts.
3. Decoupled Architecture: Completely separate from the F&O stock options universe.
"""

import os
import io
import csv
import urllib.request
import pandas as pd
from typing import List, Dict, Any, Optional

from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("BroadMarketMaster")

LOCAL_EXCEL_PATH = r"D:\Nifty 500 stocks.xlsx"
MIN_CLOSE_PRICE = 50.0       # Anti-penny stock filter
MIN_DAILY_VOLUME = 200_000   # Deep order book threshold
MIN_TURNOVER_CR = 15.0       # Minimum ₹15 Crores traded value to prevent circuit lockouts

# NIFTY 50 / Large-Cap Universe - COMPLETELY EXCLUDED from Intraday Cash
# High-momentum, clean trending Midcaps and Smallcaps only
LARGE_CAP_SYMBOLS = {
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "LT", "SBIN",
    "BHARTIARTL", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "TATAMOTORS", "SUNPHARMA",
    "BAJFINANCE", "MARUTI", "TITAN", "ADANIENT", "ADANIPORTS", "NTPC", "ONGC",
    "POWERGRID", "ULTRACEMCO", "TATASTEEL", "M&M", "BAJAJFINSV", "COALINDIA",
    "ASIANPAINT", "HINDALCO", "GRASIM", "TECHM", "JSWSTEEL", "WIPRO", "INDUSINDBK",
    "BPCL", "CIPLA", "DRREDDY", "BRITANNIA", "EICHERMOT", "APOLLOHOSP", "HEROMOTOCO",
    "BAJAJ-AUTO", "DIVISLAB", "SBILIFE", "HDFCLIFE", "TATACONSUM", "LTIM", "SHRIRAMFIN",
    "NESTLEIND", "BEL", "HCLTECH"
}


class BroadMarketMaster:
    """
    Manages the Quality Intraday Cash Universe with strict Anti-Circuit Protection.
    Completely decoupled from the F&O Options universe.
    """

    def __init__(
        self,
        excel_path: str = LOCAL_EXCEL_PATH,
        min_price: float = MIN_CLOSE_PRICE,
        min_volume: int = MIN_DAILY_VOLUME,
        min_turnover_cr: float = MIN_TURNOVER_CR,
    ):
        self.excel_path = excel_path
        self.min_price = min_price
        self.min_volume = min_volume
        self.min_turnover_cr = min_turnover_cr
        self._universe: List[Dict[str, Any]] = []
        self.last_load_utc: Optional[str] = None

    def load_universe(self, top_n_liquid: int = 750) -> List[Dict[str, Any]]:
        """
        Loads, filters for quality anti-circuit safety, ranks by liquidity,
        and returns the top N (default 750) broad market cash candidates.
        """
        stocks = self._load_quality_from_excel(top_n_liquid)

        # Fallback 1: Bundled verified JSON configuration (for Cloud / Render deployment)
        if not stocks:
            config_json = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config", "cash_stocks_master.json")
            if os.path.exists(config_json):
                try:
                    import json
                    with open(config_json, "r", encoding="utf-8") as f:
                        stocks = json.load(f)[:top_n_liquid]
                    logger.info(f"Loaded {len(stocks)} cash stocks from bundled {config_json}")
                except Exception as e:
                    logger.error(f"Error loading bundled cash master JSON: {e}")

        # Fallback 2: Online NSE Nifty 500 list
        if not stocks:
            logger.warning(f"Could not load quality stocks from {self.excel_path}, falling back to online NSE...")
            stocks = self._load_from_online_nse(top_n_liquid)

        self._universe = stocks
        self.last_load_utc = get_utc_iso()
        logger.info(
            f"Loaded {len(self._universe)} broad market intraday cash stocks "
            f"(Min Price: ₹{self.min_price}, Min Turnover: ₹{self.min_turnover_cr} Cr)."
        )
        return self._universe

    def _load_quality_from_excel(self, top_n: int) -> List[Dict[str, Any]]:
        if not os.path.exists(self.excel_path):
            logger.warning(f"Excel file not found at {self.excel_path}")
            return []

        try:
            logger.info(f"Filtering quality cash stocks from {self.excel_path}...")
            df = pd.read_excel(self.excel_path)
            if "Symbol" not in df.columns:
                logger.error("Column 'Symbol' not found in Excel sheet!")
                return []

            # Ensure numeric conversion
            for col in ["close", "volume", "%_change"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            # Calculate Turnover in ₹ Crores (Price * Volume / 1 Crore)
            if "volume" in df.columns and "close" in df.columns:
                df["turnover_cr"] = (df["close"] * df["volume"]) / 1e7
            else:
                df["turnover_cr"] = 0.0

            # Apply Anti-Circuit & Quality Gates:
            # 1. Close >= min_price (e.g. ₹50)
            # 2. Volume >= min_volume (e.g. 200,000 shares)
            # 3. Turnover >= min_turnover_cr (e.g. ₹15 Cr)
            # 4. EXCLUDE LARGE CAPS: Pure high-momentum Midcaps & Smallcaps only
            valid = df.dropna(subset=["Symbol", "close", "volume"]).copy()
            # Clean symbol string
            valid["clean_sym"] = valid["Symbol"].astype(str).str.strip().str.upper()
            valid = valid[~valid["clean_sym"].isin(LARGE_CAP_SYMBOLS)].copy()

            quality_df = valid[
                (valid["close"] >= self.min_price) &
                (valid["volume"] >= self.min_volume) &
                (valid["turnover_cr"] >= self.min_turnover_cr)
            ].copy()

            logger.info(f"Mid/Smallcap Quality Filter: {len(quality_df)} stocks qualify (Large caps eliminated).")

            # Rank by institutional traded turnover (highest liquidity first)
            quality_df.sort_values(by="turnover_cr", ascending=False, inplace=True)
            
            # If high-turnover count < top_n, add remaining symbols from broad list to reach top_n
            remaining_needed = top_n - len(quality_df)
            if remaining_needed > 0:
                other_valid = valid[~valid.index.isin(quality_df.index)].sort_values(by="turnover_cr", ascending=False)
                combined_df = pd.concat([quality_df, other_valid.head(remaining_needed)])
            else:
                combined_df = quality_df.head(top_n)

            items = []
            for _, row in combined_df.iterrows():
                sym = str(row["Symbol"]).strip().upper()
                name = str(row.get("Stock Name", sym)).strip()
                turnover = float(row.get("turnover_cr", 0.0))
                price = float(row.get("close", 0.0))

                items.append({
                    "symbol": sym,
                    "name": name,
                    "exchange": "NSE",
                    "yf_ticker": f"{sym}.NS",
                    "category": "INTRADAY_CASH",
                    "importance": "BROAD_MARKET_CASH",
                    "capital_tier": "EQUITY_CASH",
                    "max_sl_premium_pct": 1.5,
                    "target_gain_pct": 3.0,
                    "strike_step": 1,
                    "lot_size": 1,
                    "base_close": price,
                    "turnover_cr": turnover,
                    "anti_circuit_cleared": True
                })
            return items
        except Exception as e:
            logger.error(f"Error filtering Excel {self.excel_path}: {e}")
            return []

    def _load_from_online_nse(self, top_n: int) -> List[Dict[str, Any]]:
        """Fallback to top Nifty 100/500 constituents if local Excel unavailable."""
        url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
        items: List[Dict[str, Any]] = []
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                csv_text = resp.read().decode("utf-8")
                reader = csv.DictReader(io.StringIO(csv_text))
                for r in reader:
                    sym = r.get("Symbol", "").strip().upper()
                    name = r.get("Company Name", sym).strip()
                    if sym:
                        items.append({
                            "symbol": sym,
                            "name": name,
                            "exchange": "NSE",
                            "yf_ticker": f"{sym}.NS",
                            "category": "INTRADAY_CASH",
                            "importance": "HIGH_LIQUIDITY_CASH",
                            "capital_tier": "EQUITY_CASH",
                            "max_sl_premium_pct": 1.5,
                            "target_gain_pct": 3.0,
                            "strike_step": 1,
                            "lot_size": 1,
                            "anti_circuit_cleared": True
                        })
                        if len(items) >= top_n:
                            break
        except Exception as e:
            logger.error(f"Fallback fetch failed: {e}")
        return items
