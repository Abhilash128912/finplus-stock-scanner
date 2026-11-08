"""
FinPlus+ Stock Scanner & Options Ignition Engine
Universe Configuration & Asset Class Parameters
"""

# Market Hours & Timezone Standards
TIMEZONE_LOGS = "UTC"            # All internal logs and persistence strictly in ISO-8601 UTC
TIMEZONE_MARKET = "Asia/Kolkata" # Exchange market hours strictly in IST (UTC+05:30)

MARKET_HOURS = {
    "open_time": "09:15",
    "ignition_scan_start": "09:30",
    "ignition_scan_end": "14:45",
    "intraday_squareoff": "15:15",
    "market_close": "15:30"
}

# 1. BSE SENSEX 30 (High Importance, Small Capital Asymmetric Scalper)
SENSEX_CONFIG = {
    "symbol": "SENSEX",
    "exchange": "BSE",
    "yf_ticker": "^BSESN",
    "importance": "HIGH",
    "category": "INDEX_OPTIONS",
    "capital_tier": "SMALL_CAPITAL",
    "max_sl_premium_pct": 45.0,  # 40% - 50% max risk on option premium
    "target_gain_pct": 180.0,    # Asymmetric target (1.5x - 2.5x premium)
    "strike_step": 100,
    "lot_size": 20
}

# 2. NSE NIFTY 50 (Low Importance, High-Risk Selective Confluence Only)
NIFTY_CONFIG = {
    "symbol": "NIFTY",
    "exchange": "NSE",
    "yf_ticker": "^NSEI",
    "importance": "LOW",
    "category": "INDEX_OPTIONS",
    "capital_tier": "STRICT_CONFLUENCE",
    "max_sl_premium_pct": 30.0,
    "target_gain_pct": 60.0,
    "strike_step": 50,
    "lot_size": 25
}

# 3. HIGH-TICKET LIQUID STOCK OPTIONS (Lot Size < 500 + Reliance)
STOCK_UNIVERSE = [
    {
        "symbol": "RELIANCE",
        "name": "Reliance Industries",
        "exchange": "NSE",
        "yf_ticker": "RELIANCE.NS",
        "lot_size": 250,
        "strike_step": 20,
        "sector": "OIL_GAS_CONGLOMERATE"
    },
    {
        "symbol": "MARUTI",
        "name": "Maruti Suzuki India",
        "exchange": "NSE",
        "yf_ticker": "MARUTI.NS",
        "lot_size": 50,
        "strike_step": 100,
        "sector": "AUTO"
    },
    {
        "symbol": "ULTRACEMCO",
        "name": "UltraTech Cement",
        "exchange": "NSE",
        "yf_ticker": "ULTRACEMCO.NS",
        "lot_size": 100,
        "strike_step": 100,
        "sector": "CEMENT"
    },
    {
        "symbol": "BAJFINANCE",
        "name": "Bajaj Finance",
        "exchange": "NSE",
        "yf_ticker": "BAJFINANCE.NS",
        "lot_size": 125,
        "strike_step": 100,
        "sector": "NBFC"
    },
    {
        "symbol": "COFORGE",
        "name": "Coforge",
        "exchange": "NSE",
        "yf_ticker": "COFORGE.NS",
        "lot_size": 150,
        "strike_step": 100,
        "sector": "IT"
    },
    {
        "symbol": "LT",
        "name": "Larsen & Toubro",
        "exchange": "NSE",
        "yf_ticker": "LT.NS",
        "lot_size": 175,
        "strike_step": 50,
        "sector": "INFRASTRUCTURE"
    },
    {
        "symbol": "TCS",
        "name": "Tata Consultancy Services",
        "exchange": "NSE",
        "yf_ticker": "TCS.NS",
        "lot_size": 175,
        "strike_step": 50,
        "sector": "IT"
    },
    {
        "symbol": "TITAN",
        "name": "Titan Company",
        "exchange": "NSE",
        "yf_ticker": "TITAN.NS",
        "lot_size": 175,
        "strike_step": 50,
        "sector": "CONSUMER_DURABLES"
    },
    {
        "symbol": "EICHERMOT",
        "name": "Eicher Motors",
        "exchange": "NSE",
        "yf_ticker": "EICHERMOT.NS",
        "lot_size": 175,
        "strike_step": 50,
        "sector": "AUTO"
    },
    {
        "symbol": "DIXON",
        "name": "Dixon Technologies",
        "exchange": "NSE",
        "yf_ticker": "DIXON.NS",
        "lot_size": 100,
        "strike_step": 100,
        "sector": "ELECTRONICS"
    },
    {
        "symbol": "POLYCAB",
        "name": "Polycab India",
        "exchange": "NSE",
        "yf_ticker": "POLYCAB.NS",
        "lot_size": 100,
        "strike_step": 100,
        "sector": "CABLES_WIRES"
    },
    {
        "symbol": "PERSISTENT",
        "name": "Persistent Systems",
        "exchange": "NSE",
        "yf_ticker": "PERSISTENT.NS",
        "lot_size": 100,
        "strike_step": 100,
        "sector": "IT"
    },
    {
        "symbol": "DRREDDY",
        "name": "Dr. Reddy's Laboratories",
        "exchange": "NSE",
        "yf_ticker": "DRREDDY.NS",
        "lot_size": 125,
        "strike_step": 50,
        "sector": "PHARMA"
    },
    {
        "symbol": "APOLLOHOSP",
        "name": "Apollo Hospitals",
        "exchange": "NSE",
        "yf_ticker": "APOLLOHOSP.NS",
        "lot_size": 125,
        "strike_step": 100,
        "sector": "HEALTHCARE"
    },
    {
        "symbol": "HINDUNILVR",
        "name": "Hindustan Unilever",
        "exchange": "NSE",
        "yf_ticker": "HINDUNILVR.NS",
        "lot_size": 300,
        "strike_step": 20,
        "sector": "FMCG"
    },
    {
        "symbol": "INFY",
        "name": "Infosys",
        "exchange": "NSE",
        "yf_ticker": "INFY.NS",
        "lot_size": 400,
        "strike_step": 20,
        "sector": "IT"
    },
    {
        "symbol": "KOTAKBANK",
        "name": "Kotak Mahindra Bank",
        "exchange": "NSE",
        "yf_ticker": "KOTAKBANK.NS",
        "lot_size": 400,
        "strike_step": 20,
        "sector": "BANKING"
    },
    {
        "symbol": "BHARTIARTL",
        "name": "Bharti Airtel",
        "exchange": "NSE",
        "yf_ticker": "BHARTIARTL.NS",
        "lot_size": 475,
        "strike_step": 20,
        "sector": "TELECOM"
    }
]

# App #1 Air Traffic Control Endpoint
APP1_REGIME_URL = "http://127.0.0.1:8000/api/regime"
APP1_FALLBACK_URL = "https://finplus-index-breadth.onrender.com/api/regime"
