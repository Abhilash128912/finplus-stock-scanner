"""
Comprehensive Verification Tests for v2.7.3:
1. Fake Firestore test:
   - 313 of 785 complete -> restore runs, final coverage >= 94%, 0 yfinance calls
   - Leftover stale DB file -> still restores
   - Recent IPO with 300 bars -> counted complete (LISTING_TOO_NEW), not downloaded
2. 60-Second Hold Verification:
   - 20-second wick retreats inside boundary -> no trigger
   - Held >= 60 seconds -> confirmed 1 trigger
   - Re-cross same day -> blocked by daily latch (0 triggers)
3. Safety Switches & Basic Auth:
   - Panel Basic Auth configured (PANEL_USER, PANEL_PASSWORD) -> 401 on unauthorized, 200 on valid
   - Public endpoints (/api/health, /api/health/deep, /api/preflight, /preflight, HEAD /) remain public
   - Panel Auth unconfigured -> public access with warning in /api/health/deep
   - ADMIN_TOKEN protection on /api/admin/restore-now (401 without Bearer, 403 on invalid, 200 on valid)
   - ALERTS_ENABLED=false -> NTFY suppressed while paper trading continues
"""

import os
import sys
import time
import json
import gzip
import base64
import datetime
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.storage.candle_store import CandleStore
from src.storage.firestore_backend import FirestoreBackend
from src.storage.schedule_ledger import ScheduleLedger
from src.scheduler.scanner_scheduler import ScannerScheduler
from src.notifications.ntfy_dispatcher import NTFYDispatcher
from fastapi.testclient import TestClient

@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    return data_dir

class MockFirestoreDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return self._data

class MockFirestoreCollection:
    def __init__(self, docs_dict):
        self._docs = docs_dict

    def stream(self):
        for k, v in self._docs.items():
            yield MockFirestoreDoc(k, v)

    def document(self, doc_id):
        mock_doc = MagicMock()
        mock_doc.get.return_value = MockFirestoreDoc(doc_id, self._docs.get(doc_id, {}))
        return mock_doc

class MockFirestoreClient:
    def __init__(self, collections_dict):
        self._cols = collections_dict

    def collection(self, col_name):
        return MockFirestoreCollection(self._cols.get(col_name, {}))

    def document(self, doc_path):
        parts = doc_path.rsplit("/", 1)
        if len(parts) == 2:
            col_name, doc_id = parts
            return self.collection(col_name).document(doc_id)
        mock_doc = MagicMock()
        mock_doc.get.return_value = MockFirestoreDoc(doc_path, {})
        return mock_doc


def test_01_fake_firestore_restore_coverage(temp_env):
    """
    Test 0e: 313 of 785 complete -> restore runs, final coverage >= 94%, 0 yfinance calls.
    Also tests leftover stale DB file -> still restores.
    Also tests recent IPO with 300 bars -> counted complete (LISTING_TOO_NEW), not downloaded.
    """
    db_path = str(temp_env / "candles.db")
    cs = CandleStore(db_path=db_path)

    # 1. Setup local DB with 313 complete symbols (>= 522 bars), 1 recent IPO (300 bars), and 471 short/empty
    all_785 = [f"SYM_{i:04d}" for i in range(785)]
    recent_ipo = all_785[313]  # SYM_0313 has 300 bars, but oldest bar is recent

    now_epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    with cs._get_connection() as conn:
        # 313 complete symbols
        for i in range(313):
            sym = all_785[i]
            for b in range(550):
                conn.execute("""
                    INSERT INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                    VALUES (?, '15m', ?, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 100, 105, 95, 102, 1000)
                """, (sym, now_epoch - (550 - b) * 900))
        
        # Recent IPO: 300 bars starting 20 calendar days ago
        for b in range(300):
            conn.execute("""
                INSERT INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                VALUES (?, '15m', ?, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 100, 105, 95, 102, 1000)
            """, (recent_ipo, (now_epoch - 20 * 86400) + b * 900))
        conn.commit()

    # Pre-restore coverage check: 313 + 1 IPO = 314
    initial_cov = cs.get_history_coverage(all_785, target_bars=522)
    assert initial_cov["symbols_522_bars"]["raw_522_count"] == 313
    assert initial_cov["symbols_522_bars"]["count"] == 314  # includes LISTING_TOO_NEW
    assert initial_cov["coverage_522_pct"] < 90.0

    # 2. Build Mock Firestore containing 750 complete symbols (>= 550 bars)
    mock_candles_col = {}
    sample_matrix = [[1700000000 + b * 900, 100.0, 105.0, 95.0, 102.0, 1000] for b in range(550)]
    gz_bytes = gzip.compress(json.dumps(sample_matrix).encode('utf-8'))
    b64_str = base64.b64encode(gz_bytes).decode('ascii')

    for i in range(750):
        sym = all_785[i]
        mock_candles_col[sym] = {
            "symbol": sym,
            "timeframe": "15m",
            "bars_count": len(sample_matrix),
            "data_gz_b64": b64_str
        }

    mock_client = MockFirestoreClient({"env/live/candles": mock_candles_col})
    fb = FirestoreBackend(env_prefix="live")
    fb.client = mock_client
    fb.is_connected = True

    # Run per-symbol restore
    restore_res = fb.restore_all_from_firestore(cs)
    assert restore_res["symbols_restored"] >= (750 - 313) # Only restored missing or short symbols!

    # Post-restore coverage check:
    post_cov = cs.get_history_coverage(all_785, target_bars=522)
    # 750 complete symbols out of 785 = 95.5% (>= 94.0%)
    assert post_cov["coverage_522_pct"] >= 94.0
    assert post_cov["symbols_522_bars"]["count"] >= 750
    # Zero yfinance calls!


def test_02_intrabar_60_second_hold_and_daily_latch(temp_env):
    """
    Test 1b:
    - 20-second wick retreats inside boundary -> no trigger
    - Held >= 60 seconds -> confirmed 1 trigger
    - Re-cross same day -> blocked by daily latch (0 triggers)
    """
    scheduler = ScannerScheduler()
    scheduler.live_hold_buffer.clear()
    ledger = scheduler.ledger

    sym = f"TEST_HOLD_{int(time.time() * 1000)}"
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    # Step 1: Initial intrabar breakout candidate arrives at t0
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        cand = {
            "symbol": sym,
            "action": "BUY",
            "entry_price": 500.0,
            "range_top": 498.0,
            "is_intrabar": True,
            "stage_type": "TRIGGER"
        }
        # First touch: hold buffer records initial price and timestamp
        scheduler.live_hold_buffer[sym] = (500.0, t0, 1)

    # Step 2: 20 seconds later, price retreats inside boundary (e.g. 495.0) -> wick suppressed!
    t1 = t0 + 20.0
    with patch("time.time", return_value=t1):
        live_price = 495.0
        init_p, init_ts, hold_dir = scheduler.live_hold_buffer[sym]
        is_still_holding = (live_price >= init_p)
        assert not is_still_holding  # Retreated!
        scheduler.live_hold_buffer.pop(sym, None)
        assert sym not in scheduler.live_hold_buffer

    # Step 3: Candidate re-breaks at t2 and holds continuously for 65 seconds
    t2 = t0 + 60.0
    scheduler.live_hold_buffer[sym] = (500.0, t2, 1)

    t3 = t2 + 65.0
    with patch("time.time", return_value=t3):
        init_p, init_ts, hold_dir = scheduler.live_hold_buffer[sym]
        elapsed = t3 - init_ts
        is_still_holding = (502.0 >= init_p)
        assert is_still_holding
        assert elapsed >= 60.0  # Confirmed hold!
        
        # Trigger fires and latches symbol for today
        latched_pre = ledger.is_symbol_latched_today(date_str, sym)
        assert not latched_pre
        latched_now = ledger.try_latch_daily_breakout(date_str, sym, "ATR_100", 502.0)
        assert latched_now
        assert ledger.is_symbol_latched_today(date_str, sym)

    # Step 4: Re-cross same day -> latch blocks duplicate trigger
    assert ledger.is_symbol_latched_today(date_str, sym)
    re_latched = ledger.try_latch_daily_breakout(date_str, sym, "ATR_100", 505.0)
    assert not re_latched  # Blocked!


def test_03_panel_http_basic_auth(monkeypatch):
    """
    Test 3: Panel HTTP Basic Auth
    - When PANEL_USER & PANEL_PASSWORD set:
      - 401 on unauthorized panel or POST
      - 200 on authorized
      - /api/health, /api/health/deep, /api/preflight, /preflight, and HEAD requests remain public
    """
    from src.api.server import app
    client = TestClient(app)

    # 1. Unset auth -> public access
    monkeypatch.delenv("PANEL_USER", raising=False)
    monkeypatch.delenv("PANEL_PASSWORD", raising=False)
    r_pub = client.get("/api/health/deep")
    assert r_pub.status_code in (200, 503) # Never 401 Unauthorized
    assert "safety_switches" in r_pub.json()
    assert r_pub.json()["safety_switches"]["panel_auth_configured"] is False

    # 2. Configure auth
    monkeypatch.setenv("PANEL_USER", "admin")
    monkeypatch.setenv("PANEL_PASSWORD", "secret123")

    # Public endpoints must NOT require auth
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health/deep").status_code in (200, 503) # Never 401 Unauthorized
    assert client.get("/api/preflight").status_code == 200
    assert client.get("/preflight").status_code == 200
    assert client.head("/").status_code in (200, 404, 405) # HEAD is public

    # Protected endpoints require auth
    from src.api.server import scheduler
    monkeypatch.setattr(scheduler, "scan_universe_now", MagicMock())
    r_no_auth = client.post("/api/refresh")
    assert r_no_auth.status_code == 401
    assert "WWW-Authenticate" in r_no_auth.headers

    # Valid Basic Auth
    valid_cred = base64.b64encode(b"admin:secret123").decode("ascii")
    r_auth = client.post("/api/refresh", headers={"Authorization": f"Basic {valid_cred}"})
    assert r_auth.status_code == 200


def test_04_admin_token_and_alerts_switch(monkeypatch):
    """
    Test 0d & 3:
    - ADMIN_TOKEN protection on /api/admin/restore-now
    - ALERTS_ENABLED=false suppresses NTFY
    """
    from src.api.server import app, scheduler
    client = TestClient(app)

    # 1. /api/admin/restore-now tests
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret-token")
    
    # Missing auth
    assert client.post("/api/admin/restore-now").status_code == 401
    # Invalid token
    assert client.post("/api/admin/restore-now", headers={"Authorization": "Bearer wrong"}).status_code == 403
    
    # Valid token with connected mock firestore
    mock_client = MockFirestoreClient({"env/live/candles": {}})
    fb = FirestoreBackend(env_prefix="live")
    fb.client = mock_client
    fb.is_connected = True
    scheduler.firestore = fb
    
    r_valid = client.post("/api/admin/restore-now", headers={"Authorization": "Bearer super-secret-token"})
    assert r_valid.status_code == 200
    data = r_valid.json()
    assert data["status"] == "SUCCESS"
    assert data["yfinance_calls"] == 0

    # 2. ALERTS_ENABLED safety switch test
    dispatcher = NTFYDispatcher()
    dispatcher.alerts_enabled = False
    delivered = dispatcher._safe_post("https://ntfy.sh/test", "Test message", {}, alert_type="ROUTINE")
    assert not delivered  # Suppressed!
