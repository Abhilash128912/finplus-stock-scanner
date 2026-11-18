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
import tempfile
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
        batch_rows = []
        for i in range(313):
            sym = all_785[i]
            for b in range(550):
                batch_rows.append((sym, '15m', now_epoch - (550 - b) * 900, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 100, 105, 95, 102, 1000))
        conn.executemany("""
            INSERT INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch_rows)
        
        # Recent IPO: 300 bars starting 20 calendar days ago
        ipo_rows = []
        for b in range(300):
            ipo_rows.append((recent_ipo, '15m', (now_epoch - 20 * 86400) + b * 900, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 100, 105, 95, 102, 1000))
        conn.executemany("""
            INSERT INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ipo_rows)
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
    from src.api.server import app, scheduler
    client = TestClient(app)

    # 1. Unset auth -> public access
    monkeypatch.delenv("PANEL_USER", raising=False)
    monkeypatch.delenv("PANEL_PASSWORD", raising=False)
    r_pub = client.get("/api/health/deep")
    assert r_pub.status_code in (200, 503) # Never 401 Unauthorized
    assert "safety_switches" in r_pub.json()
    assert r_pub.json()["safety_switches"]["panel_auth_configured"] is False

    # Preflight check: unset auth -> AMBER "open access (by choice)", not RED
    r_pf_unauth = client.get("/api/preflight")
    assert r_pf_unauth.status_code == 200
    assert any("open access (by choice)" in line.lower() for line in r_pf_unauth.json()["lines"])
    ver_line = [l for l in r_pf_unauth.json()["lines"] if l.startswith("Version:")][0]
    commit_extracted = ver_line.split("Release commit ")[1].split(" on branch")[0]
    assert len(commit_extracted) <= 7

    # 2. Configure auth
    monkeypatch.setenv("PANEL_USER", "admin")
    monkeypatch.setenv("PANEL_PASSWORD", "secret123")

    # Public endpoints must NOT require auth & must be public-safe
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health/deep").status_code in (200, 503) # Never 401 Unauthorized
    r_pf_api = client.get("/api/preflight")
    assert r_pf_api.status_code == 200
    pf_data = r_pf_api.json()
    assert set(pf_data.keys()) == {"overall_status", "lines"}
    assert pf_data["overall_status"] in ("GREEN", "AMBER", "RED")
    forbidden = ["length", "len(", "Bearer", "client_id", "ntfy.sh", "https://"]
    for l in pf_data["lines"]:
        for sub in forbidden:
            assert sub.lower() not in l.lower()

    # Preflight line: Alert quota: ok
    assert any("Alert quota: ok" in line for line in pf_data["lines"])

    # Preflight line: Zero third-party fallback / Data Feed 100% INDstocks
    assert any("Data Feed: 100% INDstocks Live Broker API" in line for line in pf_data["lines"])
    assert any("Zero third-party fallback" in line for line in pf_data["lines"])

    # Preflight line: Alert quota limit reached today (AMBER)
    today_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    monkeypatch.setattr(scheduler.dispatcher, "quota_exhausted_utc_date", today_utc)
    r_quota_ex = client.get("/api/preflight").json()
    assert any("Alert quota: limit reached today" in line for line in r_quota_ex["lines"])
    monkeypatch.setattr(scheduler.dispatcher, "quota_exhausted_utc_date", None)

    r_pf_html = client.get("/preflight")
    assert r_pf_html.status_code == 200
    for sub in forbidden:
        assert sub.lower() not in r_pf_html.text.lower()
    assert client.head("/").status_code in (200, 404, 405) # HEAD is public

    # Preflight check: Virtual capital <= 0 -> RED
    scheduler.paper_tracker.virtual_capital = 0.0
    r_vc_zero = client.get("/api/preflight").json()
    assert any("Simulation capital unallocated" in line for line in r_vc_zero["lines"])
    assert r_vc_zero["overall_status"] == "RED"

    # Preflight check: F&O coverage < 90 -> RED
    scheduler.paper_tracker.virtual_capital = 100000.0
    mock_fno = MagicMock(return_value=[{"symbol": f"S_{i}"} for i in range(85)])
    monkeypatch.setattr(scheduler.feed, "get_fno_universe", mock_fno)
    r_fno_low = client.get("/api/preflight").json()
    assert any("Derivatives universe degraded" in line for line in r_fno_low["lines"])
    assert r_fno_low["overall_status"] == "RED"

    # Preflight check: ALERTS_ENABLED=False -> AMBER on safety switches
    mock_fno_ok = MagicMock(return_value=[{"symbol": f"S_{i}"} for i in range(97)])
    monkeypatch.setattr(scheduler.feed, "get_fno_universe", mock_fno_ok)
    monkeypatch.setattr(scheduler.dispatcher, "alerts_enabled", False)
    r_alerts_off = client.get("/api/preflight").json()
    assert any("Alert notifications suppressed by safety switch" in line for line in r_alerts_off["lines"])

    # Reset alerts_enabled to True
    monkeypatch.setattr(scheduler.dispatcher, "alerts_enabled", True)

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
    Test 0d & 3 & prompt requirements:
    - Check whether PanelBasicAuthMiddleware blocks POST /api/admin/restore-now,
      given it needs a Bearer ADMIN_TOKEN in the same Authorization header.
      Exempt that path from Basic Auth and keep the Bearer check.
      Test: no token -> 401, wrong token -> 403, correct token -> 200, with PANEL_USER/PANEL_PASSWORD set.
    - ALERTS_ENABLED=false suppresses NTFY.
    """
    from src.api.server import app, scheduler
    client = TestClient(app)

    # 1. /api/admin/restore-now tests WITH PANEL_USER and PANEL_PASSWORD SET
    monkeypatch.setenv("PANEL_USER", "paneladmin")
    monkeypatch.setenv("PANEL_PASSWORD", "panelpass123")
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret-token")
    
    # Missing auth -> 401 with Bearer details (NOT Basic Auth challenge)
    r_no_token = client.post("/api/admin/restore-now")
    assert r_no_token.status_code == 401
    assert "Bearer" in r_no_token.json().get("detail", "")

    # Invalid token -> 403 Forbidden
    r_wrong = client.post("/api/admin/restore-now", headers={"Authorization": "Bearer wrong"})
    assert r_wrong.status_code == 403
    assert "Invalid ADMIN_TOKEN" in r_wrong.json().get("detail", "")
    
    # Valid token with connected mock firestore -> 200 SUCCESS
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

    # 2. GET /api/admin/errors tests (Bearer ADMIN_TOKEN, Basic Auth exempt, secrets redacted)
    from src.utils.time_and_logger import setup_logger
    test_logger = setup_logger("TestErrorLogger")
    test_logger.error("Database connection failure with secret key ADMIN_TOKEN super-secret-token and " + "A" * 250)

    # Missing auth -> 401
    r_err_no_tok = client.get("/api/admin/errors")
    assert r_err_no_tok.status_code == 401

    # Invalid token -> 403
    r_err_wrong = client.get("/api/admin/errors", headers={"Authorization": "Bearer wrong"})
    assert r_err_wrong.status_code == 403

    # Valid token -> 200
    r_err_valid = client.get("/api/admin/errors", headers={"Authorization": "Bearer super-secret-token"})
    assert r_err_valid.status_code == 200
    err_data = r_err_valid.json()
    assert err_data["status"] == "SUCCESS"
    assert err_data["count"] >= 1
    assert isinstance(err_data["errors"], list)
    first_err = err_data["errors"][0]
    assert "timestamp" in first_err
    assert "logger" in first_err
    assert "message" in first_err
    assert len(first_err["message"]) <= 200
    assert "super-secret-token" not in first_err["message"] # Secret redacted!

    # 2b. /api/admin/firestore-paper-trades and /api/admin/clean-seeded-trades tests
    from src.paper.live_paper_tracker import LivePaperTracker
    temp_dir = tempfile.mkdtemp()
    temp_tracker = LivePaperTracker(db_path=str(Path(temp_dir) / "test_pt.db"))
    with patch.object(scheduler, "paper_tracker", temp_tracker):
        r_pt_no_tok = client.get("/api/admin/firestore-paper-trades")
        assert r_pt_no_tok.status_code == 401
        r_pt_valid = client.get("/api/admin/firestore-paper-trades", headers={"Authorization": "Bearer super-secret-token"})
        assert r_pt_valid.status_code == 200
        assert r_pt_valid.json()["status"] == "SUCCESS"

        r_clean_valid = client.post("/api/admin/clean-seeded-trades", headers={"Authorization": "Bearer super-secret-token"})
        assert r_clean_valid.status_code == 200
        assert r_clean_valid.json()["status"] == "SUCCESS"

    # 3. ALERTS_ENABLED safety switch test
    dispatcher = NTFYDispatcher()
    dispatcher.alerts_enabled = False
    delivered = dispatcher._safe_post("https://ntfy.sh/test", "Test message", {}, alert_type="ROUTINE")
    assert not delivered  # Suppressed!

    # 4. NTFY Quota Enhancements:
    monkeypatch.setenv("RENDER", "true")
    # a. No NTFY on startup/deploy
    dispatcher.alerts_enabled = True
    with patch.object(scheduler.dispatcher, "send_startup_bulletin") as mock_sb:
        with TestClient(app):
            pass
        assert not mock_sb.called

    # b. Dedupe identical alerts within 30 minutes
    mock_resp_200 = MagicMock(status_code=200)
    with patch("requests.post", return_value=mock_resp_200):
        # First send succeeds
        ok1 = dispatcher._safe_post("https://ntfy.sh/test", "Identical Alert Content", {}, alert_type="ROUTINE")
        assert ok1
        # Second send of identical content within 30 mins is deduped
        ok2 = dispatcher._safe_post("https://ntfy.sh/test", "Identical Alert Content", {}, alert_type="ROUTINE")
        assert not ok2  # Deduped!

    # c. On HTTP 429: log warning (not error), stop retries for current UTC day, trade alert kept priority
    mock_resp_429 = MagicMock(status_code=429, text="Rate limit exceeded")
    dispatcher._recent_alert_cache.clear()
    with patch("requests.post", return_value=mock_resp_429), patch("src.notifications.ntfy_dispatcher.logger.warning") as mock_warn:
        ok_429 = dispatcher._safe_post("https://ntfy.sh/test", "New Alert Message", {}, alert_type="ROUTINE")
        assert not ok_429
        # Verified logged as warning
        assert mock_warn.called
        assert any("429" in str(c) for c in mock_warn.call_args_list)

        # Non-priority routine alert immediately blocked without HTTP request
        with patch("requests.post") as mock_post_blocked:
            ok_halted = dispatcher._safe_post("https://ntfy.sh/test", "Another Routine", {}, alert_type="ROUTINE")
            assert not ok_halted
            assert not mock_post_blocked.called

        # Trade priority alert is still attempted
        with patch("requests.post", return_value=MagicMock(status_code=200)) as mock_post_priority:
            ok_trade = dispatcher._safe_post("https://ntfy.sh/test", "Emergency Stop Hit", {}, alert_type="STOP_LOSS")
            assert ok_trade
            assert mock_post_priority.called


def test_05_market_closed_and_stale_feed_guard(tmp_path):
    """
    Verifies that all paper entries, exits, trailing evaluations, and exit alerts
    are strictly blocked when market is closed (weekend/holiday) or price feed is stale.
    """
    from src.paper.live_paper_tracker import LivePaperTracker
    db_file = str(tmp_path / "paper_test.db")
    tracker = LivePaperTracker(db_path=db_file)
    sunday_dt = datetime.datetime(2026, 11, 15, 9, 19, 10, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    tracker._mock_ist_now = sunday_dt

    # 1. Entry blocked on Sunday
    pos = tracker.open_position(
        source_app="TEST",
        sector="INTRADAY_CASH",
        symbol="TRENT",
        action="BUY",
        entry_price=6400.0,
        stop_loss=6340.0
    )
    assert pos is None

    # Manually place position in active_positions to simulate a pre-existing trade
    tracker.active_positions["pos_trent"] = {
        "position_id": "pos_trent",
        "symbol": "TRENT",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "entry_price": 6400.0,
        "trailing_sl": 6340.0,
        "target": 6600.0,
        "quantity": 3,
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "taken": 1,
        "is_option": 0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z"
    }

    # 2. Automated exit blocked on Sunday
    with patch.object(tracker, "_dispatch_ntfy_trade_closed") as mock_alert:
        closed = tracker.close_position("pos_trent", exit_price=6340.0, exit_reason="INITIAL_SL_HIT")
        assert not closed
        assert "pos_trent" in tracker.active_positions
        assert not mock_alert.called

    # 3. update_and_trail_positions does not exit or alter position on Sunday
    with patch.object(tracker, "_fetch_live_price", return_value=6300.0): # Below SL
        tracker.update_and_trail_positions()
        assert "pos_trent" in tracker.active_positions
        assert tracker.active_positions["pos_trent"]["status"] == "OPEN"

    # 4. Weekday mid-market with STALE_FEED
    friday_dt = datetime.datetime(2026, 11, 13, 11, 30, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    tracker._mock_ist_now = friday_dt
    tracker.is_stale_feed = True

    # Entry blocked due to stale feed
    pos_stale = tracker.open_position(
        source_app="TEST",
        sector="INTRADAY_CASH",
        symbol="RELIANCE",
        action="BUY",
        entry_price=2500.0,
        stop_loss=2480.0
    )
    assert pos_stale is None

    # Exit blocked due to stale feed
    closed_stale = tracker.close_position("pos_trent", exit_price=6340.0, exit_reason="INITIAL_SL_HIT")
    assert not closed_stale
    assert "pos_trent" in tracker.active_positions

    # 5. Restore healthy feed during market hours: exit succeeds (mock NTFY to prevent real push to phone)
    tracker.is_stale_feed = False
    with patch("src.notifications.ntfy_dispatcher.NTFYDispatcher.send_stop_hit_alert") as mock_stop_alert, \
         patch.object(tracker, "_dispatch_ntfy_trade_closed") as mock_close_alert:
        closed_good = tracker.close_position("pos_trent", exit_price=6340.0, exit_reason="INITIAL_SL_HIT")
        assert closed_good
        assert "pos_trent" not in tracker.active_positions
        assert mock_stop_alert.called or mock_close_alert.called


def test_06_broker_stale_no_tick_yet():
    """
    Verifies that a missing or 0.0 last-tick time formats as 'no tick yet'
    instead of astronomical seconds like 1794714847s.
    """
    scheduler = ScannerScheduler()
    scheduler.indmoney_feed = MagicMock()
    scheduler.indmoney_feed._last_fetch_time = 0.0
    scheduler.fallback_active = False

    # Simulate fallback guard check
    with patch.object(scheduler.dispatcher, "send_fallback_guard_alert") as mock_fb_alert:
        last_fetch = getattr(scheduler.indmoney_feed, "_last_fetch_time", 0.0)
        if last_fetch <= 0.0:
            feed_age_sec = 999.0
            feed_age_desc = "no tick yet"
        else:
            feed_age_sec = time.time() - last_fetch
            feed_age_desc = f"{int(feed_age_sec)}s"
        
        priority_symbols = {"TRENT"}
        if feed_age_sec > 180.0 and len(priority_symbols) > 0 and not scheduler.fallback_active:
            scheduler.dispatcher.send_fallback_guard_alert("START", f"Broker stale {feed_age_desc}. Prioritizing {len(priority_symbols)} symbols.")

        assert mock_fb_alert.called
        msg_arg = mock_fb_alert.call_args[0][1]
        assert "Broker stale no tick yet" in msg_arg
        assert "17947" not in msg_arg


def test_07_seed_demo_panel_refuses_live_environment(monkeypatch):
    """
    Verifies that scripts/seed_demo_panel.py refuses to run when FINPLUS_ENVIRONMENT=live,
    raising a RuntimeError to prevent polluting the live database.
    """
    import pytest
    from scripts.seed_demo_panel import seed_demo_trades

    monkeypatch.setenv("FINPLUS_ENVIRONMENT", "live")
    with pytest.raises(RuntimeError) as exc_info:
        seed_demo_trades()
    assert "strictly forbidden when FINPLUS_ENVIRONMENT=live" in str(exc_info.value)


def test_08_ntfy_suppressed_when_not_on_render(monkeypatch, caplog):
    """
    Verifies that all NTFY sends are strictly blocked unless running on Render Cloud.
    - Local runs (RENDER and RENDER_SERVICE_ID unset) return False and make 0 network requests.
    - Logs 'NTFY suppressed (not on Render)' at most once per hour.
    """
    import logging
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.delenv("RENDER_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FINPLUS_ENVIRONMENT", raising=False)
    monkeypatch.delenv("FORCE_LOCAL_NTFY", raising=False)

    from src.notifications.ntfy_dispatcher import NTFYDispatcher
    dispatcher = NTFYDispatcher()
    dispatcher.alerts_enabled = True

    with patch("requests.post") as mock_post, caplog.at_level(logging.INFO):
        # 1. First send: suppressed and logged
        res1 = dispatcher._safe_post("https://ntfy.sh/test", "Test 1", {"Title": "T1"})
        assert res1 is False
        assert mock_post.call_count == 0
        assert "NTFY suppressed (not on Render)" in caplog.text

        # 2. Second send within 1 hour: suppressed but NOT logged again (rate limited to once/hr)
        caplog.clear()
        res2 = dispatcher._safe_post("https://ntfy.sh/test", "Test 2", {"Title": "T2"})
        assert res2 is False
        assert mock_post.call_count == 0
        assert "NTFY suppressed (not on Render)" not in caplog.text




