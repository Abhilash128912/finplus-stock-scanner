"""
FinPlus+ NSE F&O Official Contract Master Loader
Directly ingests https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv at startup.
Dynamically filters liquid equities with lot size <= 500 (plus RELIANCE),
resolving official NSE symbols (e.g. INFY, TCS, MARUTI) and revised SEBI lot sizes.
"""

import os
import csv
import io
import time
import urllib.request
from typing import List, Dict, Any
from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("FNOContractMaster")

NSE_MKT_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
CACHE_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config", "fo_mktlots.csv"))

# Primary Indices
INDEX_SPECS = [
    {
        "symbol": "SENSEX",
        "name": "BSE Sensex 30",
        "exchange": "BSE",
        "yf_ticker": "^BSESN",
        "importance": "HIGH",
        "category": "INDEX_OPTIONS",
        "capital_tier": "SMALL_CAPITAL",
        "max_sl_premium_pct": 45.0,
        "target_gain_pct": 180.0,
        "strike_step": 100,
        "lot_size": 20
    },
    {
        "symbol": "NIFTY",
        "name": "Nifty 50 Index",
        "exchange": "NSE",
        "yf_ticker": "^NSEI",
        "importance": "LOW",
        "category": "INDEX_OPTIONS",
        "capital_tier": "STRICT_CONFLUENCE",
        "max_sl_premium_pct": 30.0,
        "target_gain_pct": 60.0,
        "strike_step": 50,
        "lot_size": 65 # Revised official SEBI lot
    },
    {
        "symbol": "MIDCPNIFTY",
        "name": "Nifty Midcap Select",
        "exchange": "NSE",
        "yf_ticker": "^NSEMDCP50",
        "importance": "HIGH_SWING",
        "category": "INDEX_OPTIONS",
        "capital_tier": "SWING_ACCUMULATION",
        "max_sl_premium_pct": 60.0,
        "target_gain_pct": 160.0,
        "strike_step": 25,
        "lot_size": 75
    }
]

class FNOContractMaster:
    def __init__(self, max_lot_size: int = 500):
        self.max_lot_size = max_lot_size
        self._universe = []
        self.last_load_utc = None

    def fetch_nse_mktlots_csv(self) -> str:
        """Fetches the official NSE market lots CSV with local backup fallback."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,text/csv;q=0.9,*/*;q=0.8"
        }
        try:
            logger.info(f"Downloading official NSE F&O contract master from {NSE_MKT_LOTS_URL}...")
            req = urllib.request.Request(NSE_MKT_LOTS_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                content = resp.read().decode('utf-8', errors='ignore')
                if len(content) > 500:
                    # Save to local cache
                    os.makedirs(os.path.dirname(CACHE_FILE_PATH), exist_ok=True)
                    with open(CACHE_FILE_PATH, 'w', encoding='utf-8') as f:
                        f.write(content)
                    logger.info("Successfully fetched and cached live NSE fo_mktlots.csv")
                    return content
        except Exception as e:
            logger.warning(f"Could not reach live NSE URL ({e}). Checking local cache...")

        # Fallback to local cache
        if os.path.exists(CACHE_FILE_PATH):
            with open(CACHE_FILE_PATH, 'r', encoding='utf-8') as f:
                logger.info(f"Loaded fo_mktlots.csv from local cache: {CACHE_FILE_PATH}")
                return f.read()

        logger.error("No live or cached fo_mktlots.csv available!")
        return ""

    def load_universe(self, top_n_equities: int = 25) -> List[Dict[str, Any]]:
        """
        Parses CSV, filters equities where lot <= 500 (+ RELIANCE),
        and returns structured metadata list.
        """
        raw_csv = self.fetch_nse_mktlots_csv()
        equities = []

        if raw_csv:
            reader = csv.reader(io.StringIO(raw_csv.strip()))
            try:
                header = [h.strip() for h in next(reader)]
                # Look for current month lot col (col 2)
                for row in reader:
                    if len(row) >= 3:
                        underlying = row[0].strip()
                        sym = row[1].strip()
                        lot_str = row[2].strip()
                        
                        # Skip broad indices already handled
                        if sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]:
                            continue
                            
                        try:
                            lot = int(lot_str)
                            # Filter rule: lot <= 500 OR RELIANCE
                            if lot <= self.max_lot_size or sym == "RELIANCE":
                                # Determine reasonable strike step based on price/lot
                                strike_step = 50
                                if lot <= 100:
                                    strike_step = 100
                                elif lot >= 300:
                                    strike_step = 20
                                    
                                equities.append({
                                    "symbol": sym,
                                    "name": underlying,
                                    "exchange": "NSE",
                                    "yf_ticker": f"{sym}.NS",
                                    "lot_size": lot,
                                    "strike_step": strike_step,
                                    "importance": "HIGH",
                                    "category": "STOCK_OPTIONS"
                                })
                        except ValueError:
                            continue
            except Exception as e:
                logger.error(f"Error parsing fo_mktlots.csv: {e}")

        # Limit to top N liquid equities for fast 15m scanning
        selected_equities = equities[:top_n_equities]
        
        # Combine: Sensex (BSE), Nifty (NSE), and selected equities
        self._universe = INDEX_SPECS + selected_equities
        self.last_load_utc = get_utc_iso()
        logger.info(f"Loaded {len(self._universe)} total instruments ({len(selected_equities)} liquid equities lot <= {self.max_lot_size}).")
        return self._universe
