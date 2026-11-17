"""
FinPlus+ Stock Scanner - Embedded Crypto Delta Configuration
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, List

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DELTA_API_BASE = os.getenv("DELTA_API_BASE", "https://api.india.delta.exchange")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "Finplus-Delta")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
ENABLE_DELTA_NTFY = os.getenv("ENABLE_DELTA_NTFY", "true").lower() in ("true", "1", "yes")

DB_PATH = DATA_DIR / "crypto_paper.db"
DEFAULT_USD_INR_RATE = float(os.getenv("USD_INR_RATE", "90.0"))
VIRTUAL_STARTING_EQUITY_INR = float(os.getenv("CRYPTO_STARTING_EQUITY", "100000.0"))
MAX_CONCURRENT_POSITIONS = 6
CONFLUENCE_TRIGGER_THRESHOLD = 80

def load_contracts() -> List[Dict[str, Any]]:
    contracts_file = CONFIG_DIR / "delta_contracts.json"
    if contracts_file.exists():
        with open(contracts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("tracked_instruments", [])
    return []
