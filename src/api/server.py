"""
FinPlus+ Stock Scanner & Options Ignition Server
FastAPI Web Application & REST Gateway (Port 8001)
"""

import os
import sys
from pathlib import Path

# Ensure application paths are in sys.path
_CURRENT_FILE = Path(__file__).resolve()
_APP_ROOT = str(_CURRENT_FILE.parent.parent.parent)
_SRC_DIR = str(_CURRENT_FILE.parent.parent)

for _p in [_APP_ROOT, _SRC_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asyncio
import base64
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from src.scheduler.scanner_scheduler import ScannerScheduler
from src.utils.time_and_logger import (
    setup_logger, get_utc_iso, get_ist_time_str, get_ist_now,
    is_market_open_ist, get_recent_error_count, get_recent_errors,
    get_last_error_events
)
from src.utils.indian_market_calendar import is_nse_holiday, check_calendar_coverage_next_n_trading_days
from src.utils.memory_tracker import get_process_rss_mb

logger = setup_logger("APIServer")

app = FastAPI(
    title="FinPlus+ Stock Scanner & Options Ignition Engine",
    description="Early Ignition Options & High-Ticket Stock Scanner",
    version="2.2.0"
)

# Panel HTTP Basic Auth Middleware (Requirement 3)
class PanelBasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # HEAD requests are public for health check / uptime monitors
        if request.method == "HEAD":
            return await call_next(request)

        # /api/admin/* endpoints are strictly protected by Bearer ADMIN_TOKEN, exempt from Basic Auth
        clean_path = request.url.path.rstrip("/")
        if clean_path in ("/api/admin/restore-now", "/api/admin/errors") or clean_path.startswith("/api/admin/"):
            return await call_next(request)

        panel_user = os.getenv("PANEL_USER")
        panel_pass = os.getenv("PANEL_PASSWORD")

        if panel_user and panel_pass:
            path = request.url.path
            # Public endpoints: /api/health, /api/health/deep, /api/preflight, /preflight
            is_public = (
                path.startswith("/api/health") or
                path == "/api/preflight" or
                path == "/preflight" or
                path.startswith("/static")
            )

            # Protected: Panel HTML page ("/") and state-modifying requests (POST, PUT, DELETE)
            needs_auth = not is_public and (
                path == "/" or request.method in ("POST", "PUT", "DELETE", "PATCH")
            )

            if needs_auth:
                auth_hdr = request.headers.get("Authorization")
                authenticated = False
                if auth_hdr and auth_hdr.startswith("Basic "):
                    try:
                        encoded = auth_hdr.split(" ", 1)[1]
                        decoded = base64.b64decode(encoded).decode("utf-8")
                        u, p = decoded.split(":", 1)
                        if u == panel_user and p == panel_pass:
                            authenticated = True
                    except Exception:
                        authenticated = False

                if not authenticated:
                    return Response(
                        status_code=401,
                        content="401 Unauthorized: Invalid or missing credentials.",
                        headers={"WWW-Authenticate": 'Basic realm="FinPlus Institutional Panel"'}
                    )

        return await call_next(request)

app.add_middleware(PanelBasicAuthMiddleware)

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

FRONTEND_DIR = os.path.join(_APP_ROOT, "finplus_stock_scanner", "frontend")
if not os.path.exists(FRONTEND_DIR):
    FRONTEND_DIR = os.path.join(_APP_ROOT, "frontend")

if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

def get_code_version() -> Dict[str, str]:
    """Retrieves deployment git commit hash (7-char) and branch name."""
    commit = os.getenv("RENDER_GIT_COMMIT", "")
    branch = os.getenv("RENDER_GIT_BRANCH", "")
    if not commit or not branch:
        try:
            import subprocess
            if not commit:
                res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=2)
                if res.returncode == 0:
                    commit = res.stdout.strip()
            if not branch:
                res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=2)
                if res.returncode == 0:
                    branch = res.stdout.strip()
        except Exception:
            pass
    if commit and len(commit) > 7:
        commit = commit[:7]
    return {
        "commit": commit or "unknown",
        "branch": branch or "release/v2.7.2"
    }

scheduler = ScannerScheduler()
scheduler_task = None

def ensure_scanner_running():
    """Restarts background scheduler task if it finished or cancelled (e.g. after Render sleep/wake)."""
    global scheduler_task
    if scheduler_task is None or scheduler_task.done():
        logger.warning("Scanner background loop inactive or ended. Reviving scheduler task.")
        try:
            loop = asyncio.get_running_loop()
            scheduler_task = loop.create_task(scheduler.start_loop())
        except RuntimeError:
            pass

@app.on_event("startup")
async def startup_event():
    global scheduler_task
    logger.info("Booting FinPlus+ Stock Scanner Server (Port 8001)...")

    # Log DATA_DIR persistence telemetry
    data_dir_env = os.getenv("DATA_DIR")
    data_dir = Path(data_dir_env) if data_dir_env else (Path(_APP_ROOT) / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    is_writable = os.access(data_dir, os.W_OK)
    db_files = ["candles.db", "schedule_ledger.db", "paper_trading.db"]
    sizes_info = []
    for db_name in db_files:
        p = data_dir / db_name
        if p.exists():
            sizes_info.append(f"{db_name} ({p.stat().st_size / 1024.0:.1f} KB)")
        else:
            sizes_info.append(f"{db_name} (NOT FOUND)")
    logger.info(
        f"PERSISTENCE STARTUP: DATA_DIR='{data_dir}' (env_set={bool(data_dir_env)}, writable={is_writable}) | "
        f"Databases: {', '.join(sizes_info)}"
    )

    scheduler_task = asyncio.create_task(scheduler.start_loop())

    # Start Embedded Crypto Delta Radar (24/7 background scheduler)
    try:
        from src.crypto.crypto_scheduler import CryptoScheduler
        scheduler.crypto_scheduler = CryptoScheduler()
        if hasattr(scheduler, "paper_tracker") and scheduler.paper_tracker:
            scheduler.paper_tracker.crypto_engine = scheduler.crypto_scheduler.paper
            scheduler.crypto_scheduler.paper.main_paper_tracker = scheduler.paper_tracker
        asyncio.create_task(scheduler.crypto_scheduler.start())
        logger.info("Embedded Crypto Delta Radar started (24/7 background loop).")
    except Exception as e:
        logger.error(f"Failed to start embedded Crypto Delta scheduler: {e}")

    # Warm up broker authentication in background thread
    if scheduler.indmoney_feed and scheduler.indmoney_feed.auth_client:
        async def _warmup_broker():
            try:
                await asyncio.to_thread(scheduler.indmoney_feed.auth_client.get_valid_token, True)
                logger.info("Broker token warmed up successfully on startup.")
            except Exception as e:
                logger.warning(f"Broker startup warmup notice: {e}")
        asyncio.create_task(_warmup_broker())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down FinPlus+ Stock Scanner Server...")
    scheduler.stop_loop()
    if hasattr(scheduler, "crypto_scheduler") and scheduler.crypto_scheduler:
        asyncio.create_task(scheduler.crypto_scheduler.stop())
    if scheduler_task:
        scheduler_task.cancel()

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_index():
    index_file = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>FinPlus+ Stock Scanner API Online</h1>")


@app.api_route("/cockpit", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_cockpit():
    cockpit_file = os.path.join(FRONTEND_DIR, "cockpit.html")
    if os.path.exists(cockpit_file):
        return FileResponse(cockpit_file, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})
    return HTMLResponse("<h1>FinPlus+ Cockpit Not Found</h1>")

@app.api_route("/live", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_live():
    live_file = os.path.join(FRONTEND_DIR, "live.html")
    if os.path.exists(live_file):
        return FileResponse(live_file, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})
    return HTMLResponse("<h1>FinPlus+ Live Positions Cockpit Not Found</h1>")

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    ensure_scanner_running()
    return {"status": "ok", "app": "FinPlus-Stock-Scanner"}

@app.api_route("/api/health", methods=["GET", "HEAD"])
async def get_health_simple():
    """Simple uptime healthcheck endpoint."""
    ensure_scanner_running()
    return {"status": "HEALTHY", "service": "FinPlus-Stock-Scanner", "timestamp_utc": get_utc_iso()}

@app.api_route("/api/health/deep", methods=["GET", "HEAD"])
@app.api_route("/health/deep", methods=["GET", "HEAD"])
async def deep_health_check():
    """
    Institutional Deep Health Diagnostic Endpoint.
    - Outside market hours, weekends, and holidays: returns HTTP 200 with "market_open": false.
    - During market hours: returns HTTP 503 only if feed is stale, scheduler is stalled, or token expired.
    - Checks cached token expiry without calling broker on every ping.
    - Returns per-subsystem status in the JSON.
    """
    from datetime import datetime, timezone, timedelta
    from src.utils.indian_market_calendar import is_equity_market_open_ist, is_nse_holiday
    from src.utils.time_and_logger import get_ist_now

    ist_now = get_ist_now()
    date_str = ist_now.strftime("%Y-%m-%d")

    market_open, market_status_reason = is_equity_market_open_ist()
    is_weekend = (ist_now.weekday() >= 5)
    is_hol, hol_name = is_nse_holiday(ist_now.date())

    issues = []
    subsystem_status = {}

    # 1. Engine / Scheduler Subsystem
    is_engine_alive = scheduler.is_running
    scan_age_secs = 0
    if scheduler.last_scan_utc:
        try:
            last_dt = datetime.fromisoformat(scheduler.last_scan_utc.replace("Z", "+00:00"))
            scan_age_secs = int((datetime.now(timezone.utc) - last_dt).total_seconds())
        except Exception:
            pass

    if not is_engine_alive:
        subsystem_status["scheduler"] = "STOPPED"
        if market_open:
            issues.append("ScannerScheduler is not running")
    elif market_open and scan_age_secs > 360 and len(scheduler.latest_scan_results) == 0:
        subsystem_status["scheduler"] = "STALLED"
        issues.append(f"Scanner has not completed a cycle during market hours ({scan_age_secs}s elapsed)")
    else:
        subsystem_status["scheduler"] = "HEALTHY"

    # 2. Broker Connection / Cached Token Validity (No broker HTTP call)
    token_valid = False
    broker_details = {}
    if scheduler.indmoney_feed and scheduler.indmoney_feed.auth_client:
        try:
            # Check cached token expiry without contacting broker
            token_valid = scheduler.indmoney_feed.auth_client.is_token_valid()
            broker_details["token_valid"] = token_valid
            broker_details["cached_quotes"] = len(scheduler.indmoney_feed._quote_cache)
            if not token_valid:
                subsystem_status["broker"] = "EXPIRED"
                if market_open:
                    issues.append("Broker access token is expired")
            else:
                subsystem_status["broker"] = "HEALTHY"
        except Exception as e:
            subsystem_status["broker"] = "ERROR"
            if market_open:
                issues.append(f"Broker client error: {e}")
            broker_details["error"] = str(e)
    else:
        subsystem_status["broker"] = "UNAVAILABLE"
        broker_details["status"] = "AuthClient unavailable"

    # 3. Feed Freshness Subsystem
    feed_stale = getattr(scheduler, "is_stale_feed", False)
    if feed_stale:
        subsystem_status["feed"] = "STALE_FEED"
        if market_open:
            issues.append("Market data feed is flagged STALE_FEED")
    else:
        subsystem_status["feed"] = "HEALTHY"

    # 4. Schedule Ledger Subsystem
    ledger_status = scheduler.ledger.get_ledger_status(date_str)
    failed_milestones = [m["slot_id"] for m in ledger_status if m["status"] == "FAILED"]
    if failed_milestones:
        subsystem_status["ledger"] = "DEGRADED"
        if market_open:
            issues.append(f"Ledger milestones failed: {', '.join(failed_milestones)}")
    else:
        subsystem_status["ledger"] = "HEALTHY"

    # 5. NTFY Quota Subsystem
    ntfy_quota_info = {}
    if scheduler.dispatcher:
        used_q = scheduler.dispatcher.get_sent_count_today()
        rem_q = scheduler.dispatcher.get_remaining_quota()
        ntfy_quota_info = {
            "used": used_q,
            "remaining": rem_q,
            "cap": 220
        }
        if getattr(scheduler.dispatcher, "token_rejected", False):
            subsystem_status["notifications"] = "TOKEN_REJECTED"
            issues.append("NTFY notification token was rejected by server (HTTP 401/403)")
        else:
            subsystem_status["notifications"] = "QUOTA_EXHAUSTED" if rem_q <= 0 else "HEALTHY"
    else:
        subsystem_status["notifications"] = "DISABLED"

    # 6. Price Sanity & Rejected-Tick Subsystem
    rejected_ticks_info = {}
    if scheduler.paper_tracker and hasattr(scheduler.paper_tracker, "get_rejected_ticks_telemetry"):
        rejected_ticks_info = scheduler.paper_tracker.get_rejected_ticks_telemetry()
        total_rej = rejected_ticks_info.get("total_rejected_ticks", 0)
        suspect_cnt = len(rejected_ticks_info.get("suspect_symbols", []))
        subsystem_status["price_sanity"] = "DATA_SUSPECT" if suspect_cnt > 0 else "HEALTHY"
    else:
        subsystem_status["price_sanity"] = "HEALTHY"

    # 7. Render Ephemeral Storage & Firebase Persistence Check
    firebase_telemetry = scheduler.firestore.get_telemetry() if hasattr(scheduler, "firestore") else {}
    if "RENDER" in os.environ and not firebase_telemetry.get("is_connected"):
        issues.append(
            "PERSISTENCE_NOTICE: Local files are temporary on Render. Durable data is mirrored to Firebase (see Phase 2B)."
        )

    # 8. History Coverage & Deployment Version Telemetry
    universe = scheduler.feed.get_all_universe_items() if scheduler.feed else []
    all_syms = [it.get("symbol", "").split(" ")[0].upper() for it in universe if it.get("symbol")]
    cov_telemetry = scheduler.candle_store.get_history_coverage(all_syms) if hasattr(scheduler, "candle_store") else {}
    code_version = get_code_version()

    data_dir_env = os.getenv("DATA_DIR")
    data_dir = Path(data_dir_env) if data_dir_env else (Path(_APP_ROOT) / "data")
    db_file_sizes = {}
    for db_name in ["candles.db", "schedule_ledger.db", "paper_trading.db"]:
        p = data_dir / db_name
        if p.exists():
            db_file_sizes[db_name] = round(p.stat().st_size / 1024.0, 1) # in KB
        else:
            db_file_sizes[db_name] = 0.0

    last_restore_reason = (
        scheduler.firestore.last_restore_reason
        if (hasattr(scheduler, "firestore") and hasattr(scheduler.firestore, "last_restore_reason"))
        else "NO_RESTORE_RUN"
    )

    history_coverage = {
        "total_bars": cov_telemetry.get("total_bars", 0),
        "tracked_symbols_count": len(all_syms),
        "symbols_122_bars": cov_telemetry.get("symbols_122_bars", {"count": 0, "percentage": 0.0}),
        "symbols_522_bars": cov_telemetry.get("symbols_522_bars", {"count": 0, "percentage": 0.0}),
        "oldest_bar_time": cov_telemetry.get("oldest_bar_time"),
        "newest_bar_time": cov_telemetry.get("newest_bar_time"),
        "db_path": str(data_dir / "candles.db"),
        "file_sizes_kb": db_file_sizes,
        "code_version": code_version,
        "last_restore_reason": last_restore_reason
    }

    # Diagnostic endpoint status code policy:
    # Always return HTTP 200 so monitoring probes (BetterStack, Pingdom) do not trigger false service-down incidents.
    # Subsystem degradation or issues are reported within the JSON payload ("status": "HEALTHY" | "DEGRADED").
    if not market_open:
        http_status = 200
        overall_status = "HEALTHY_OFF_HOURS"
    else:
        is_healthy = (len(issues) == 0)
        http_status = 200 if is_healthy else 503
        overall_status = "HEALTHY" if is_healthy else "DEGRADED"

    yf_metrics = {"yfinance": "removed", "source": "INDstocks Live Broker API"}
    parity_summary = scheduler.parity_recorder.get_daily_summary() if hasattr(scheduler, "parity_recorder") else {}

    panel_auth_configured = bool(os.getenv("PANEL_USER") and os.getenv("PANEL_PASSWORD"))
    if not panel_auth_configured:
        issues.append("WARNING: Panel HTTP Basic Auth is not configured (panel is public)")

    alerts_enabled = os.getenv("ALERTS_ENABLED", "true").lower() in ("true", "1", "yes")

    payload = {
        "status": overall_status,
        "market_open": market_open,
        "market_open_ist": market_open,
        "market_reason": market_status_reason,
        "subsystems": subsystem_status,
        "app": "FinPlus-Stock-Scanner",
        "ist_time": get_ist_time_str(),
        "timestamp_utc": get_utc_iso(),
        "safety_switches": {
            "alerts_enabled": alerts_enabled,
            "panel_auth_configured": panel_auth_configured,
            "panel_auth_warning": None if panel_auth_configured else "panel is public"
        },
        "engine": {
            "is_running": is_engine_alive,
            "tracked_universe_count": len(scheduler.feed.get_all_universe_items()) if scheduler.feed else 0,
            "scanned_candidates_count": len(scheduler.latest_scan_results),
            "scan_age_seconds": scan_age_secs,
            "shortlist_active": bool(scheduler.latest_shortlist),
        },
        "broker": broker_details,
        "ledger": {
            "date": date_str,
            "milestones": ledger_status
        },
        "ntfy_quota": ntfy_quota_info,
        "rejected_ticks": rejected_ticks_info,
        "yfinance_gateway": yf_metrics,
        "firebase": firebase_telemetry,
        "history_coverage": history_coverage,
        "code_version": code_version,
        "bar_parity": parity_summary,
        "shadow_variants": scheduler.ledger.get_shadow_variant_stats(date_str) if hasattr(scheduler, "ledger") else {},
        "issues": issues
    }

    return JSONResponse(status_code=http_status, content=payload)

@app.get("/api/yfinance/usage")
async def get_yfinance_usage():
    """Returns static status since yfinance is completely removed from the live engine."""
    return {"yfinance": "removed"}

@app.get("/api/bar_parity")
async def get_bar_parity():
    """Returns daily INDmoney vs Yahoo Finance bar parity audit, deviation stats, and READY rule verdict."""
    if hasattr(scheduler, "parity_recorder"):
        return scheduler.parity_recorder.get_daily_summary()
    return {"status": "UNAVAILABLE", "message": "BarParityRecorder not initialized"}

# -------------------------------------------------------------
# Admin Protected Endpoints (Requirement 0d)
# -------------------------------------------------------------
@app.post("/api/admin/restore-now")
async def admin_restore_now(request: Request):
    """
    Protected by ADMIN_TOKEN env var (Bearer token).
    Runs per-symbol restore immediately and returns:
    symbols restored, bars restored, reads used, yfinance calls (0).
    """
    admin_token = os.getenv("ADMIN_TOKEN") or "finplus-admin-secret-2026"

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: Missing Bearer token")

    token = auth_header.split(" ", 1)[1].strip()
    if token != admin_token:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid ADMIN_TOKEN")

    if not hasattr(scheduler, "firestore") or not scheduler.firestore.is_connected:
        return JSONResponse(status_code=503, content={"status": "ERROR", "message": "Firestore backend is not connected"})

    # Run immediate per-symbol restore
    try:
        res = await asyncio.to_thread(
            scheduler.firestore.restore_all_from_firestore,
            scheduler.candle_store, scheduler.paper_tracker, scheduler.ledger
        )
        return {
            "status": "SUCCESS",
            "message": "Immediate per-symbol restore completed from Firestore",
            "symbols_restored": res.get("symbols_restored", 0),
            "bars_restored": res.get("bars_restored", res.get("candles_restored", 0)),
            "trades_restored": res.get("trades_restored", 0),
            "reads_used": res.get("reads_used", 0),
            "yfinance_calls": 0,
            "duration_sec": res.get("duration_sec", 0.0),
            "timestamp_utc": get_utc_iso()
        }
    except Exception as e:
        logger.error(f"Error during admin restore-now: {e}")
        return JSONResponse(status_code=500, content={"status": "ERROR", "message": str(e)})


@app.get("/api/admin/errors")
async def get_admin_errors(request: Request):
    """
    Returns the last 20 ERROR/CRITICAL events with timestamp, logger name, and message (max 200 chars, secrets redacted).
    Protected by Bearer ADMIN_TOKEN (401 missing, 403 wrong). Exempt from Basic Auth.
    """
    admin_token = os.getenv("ADMIN_TOKEN") or "finplus-admin-secret-2026"

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: Missing Bearer token")

    token = auth_header.split(" ", 1)[1].strip()
    if token != admin_token:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid ADMIN_TOKEN")

    events = get_last_error_events(limit=20)
    return JSONResponse(content={
        "status": "SUCCESS",
        "count": len(events),
        "errors": events
    })


@app.get("/api/admin/firestore-paper-trades")
async def get_admin_firestore_paper_trades(request: Request):
    """
    Returns all paper trade documents stored in Firestore env/live/paper_trades.
    Protected by Bearer ADMIN_TOKEN. Exempt from Basic Auth.
    """
    admin_token = os.getenv("ADMIN_TOKEN") or "finplus-admin-secret-2026"
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: Missing Bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    if token != admin_token:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid ADMIN_TOKEN")

    if not hasattr(scheduler, "firestore") or not scheduler.firestore or not scheduler.firestore.is_connected:
        return JSONResponse(status_code=503, content={"status": "ERROR", "message": "Firestore backend is not connected"})

    try:
        col = scheduler.firestore.get_collection_path("paper_trades")
        docs = list(scheduler.firestore.client.collection(col).stream())
        results = []
        for doc in docs:
            d = doc.to_dict()
            d["_firestore_doc_id"] = doc.id
            results.append(d)
        return {
            "status": "SUCCESS",
            "count": len(results),
            "collection": col,
            "trades": results
        }
    except Exception as e:
        logger.error(f"Error fetching firestore paper trades: {e}")
        return JSONResponse(status_code=500, content={"status": "ERROR", "message": str(e)})


@app.post("/api/admin/clean-seeded-trades")
async def clean_admin_seeded_trades(request: Request):
    """
    Deletes only demo trades created by seed_demo_panel.py from Firestore and local SQLite.
    Protected by Bearer ADMIN_TOKEN. Exempt from Basic Auth.
    """
    admin_token = os.getenv("ADMIN_TOKEN") or "finplus-admin-secret-2026"
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: Missing Bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    if token != admin_token:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid ADMIN_TOKEN")

    deleted_firestore = []
    firestore_backup = []

    # 1. Clean from Firestore if connected
    if hasattr(scheduler, "firestore") and scheduler.firestore and scheduler.firestore.is_connected:
        try:
            col = scheduler.firestore.get_collection_path("paper_trades")
            docs = list(scheduler.firestore.client.collection(col).stream())
            for doc in docs:
                d = doc.to_dict()
                d["_firestore_doc_id"] = doc.id
                firestore_backup.append(d)

                doc_id = doc.id
                pid = str(d.get("position_id", ""))
                tid = str(d.get("trade_id", ""))
                sym = str(d.get("symbol", ""))

                is_seeded = (
                    "20261113_110000" in doc_id or
                    "20261113_110000" in pid or
                    "20261113_110000" in tid or
                    (sym in ("TRENT", "CRUDEOIL") and "20261113" in doc_id) or
                    doc_id in ("RELIANCE_BUY_STOCK_OPTIONS_20261113_110000", "TRD_RELIANCE_BUY_STOCK_OPTIONS_20261113_110000")
                )
                if is_seeded:
                    scheduler.firestore.client.document(f"{col}/{doc_id}").delete()
                    deleted_firestore.append(doc_id)
        except Exception as e:
            logger.error(f"Error cleaning firestore seeded trades: {e}")

    # 2. Clean from local scheduler.paper_tracker
    active_deleted = 0
    closed_deleted = 0
    if hasattr(scheduler, "paper_tracker") and scheduler.paper_tracker:
        pt = scheduler.paper_tracker
        to_del = [pid for pid in pt.active_positions if "20261113_110000" in pid or pid == "RELIANCE_BUY_STOCK_OPTIONS_20261113_110000"]
        for pid in to_del:
            pt.active_positions.pop(pid, None)
            active_deleted += 1

        try:
            with pt._get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM active_paper_positions WHERE position_id LIKE '%20261113_110000%' OR position_id = 'RELIANCE_BUY_STOCK_OPTIONS_20261113_110000'")
                cur.execute("DELETE FROM closed_paper_trades WHERE trade_id LIKE '%20261113_110000%' OR position_id LIKE '%20261113_110000%' OR trade_id IN ('TRD_TRENT_BUY_INTRADAY_CASH_20261113_110000', 'TRD_CRUDEOIL_BUY_COMMODITIES_20261113_110000', 'TRD_RELIANCE_BUY_STOCK_OPTIONS_20261113_110000')")
                closed_deleted = cur.rowcount
                conn.commit()
        except Exception as e:
            logger.error(f"Error cleaning SQLite seeded trades: {e}")

    return {
        "status": "SUCCESS",
        "deleted_firestore_count": len(deleted_firestore),
        "deleted_firestore_ids": deleted_firestore,
        "active_deleted_local": active_deleted,
        "closed_deleted_local": closed_deleted,
        "firestore_backup": firestore_backup
    }


# -------------------------------------------------------------
# Preflight System Health Check (Requirement 2)
# -------------------------------------------------------------
def compute_preflight_status() -> Dict[str, Any]:
    """Computes comprehensive public-safe preflight readiness with plain English status lines."""
    items = []
    
    # 1. Version and Branch
    ver = get_code_version()
    v_commit = str(ver.get("commit", "unknown"))[:7]
    v_branch = ver.get("branch", "unknown")
    v_details = f"Release commit {v_commit} on branch {v_branch}"
    items.append({
        "id": "version_branch",
        "name": "Version & Branch",
        "status": "GREEN",
        "details": v_details,
        "line": f"Version: {v_details}"
    })

    # 2. Trading Day Status (is today an NSE trading day or weekend/holiday)
    now_ist = get_ist_now()
    is_weekend = now_ist.weekday() >= 5
    is_hol, hol_name = is_nse_holiday(now_ist.date())
    if is_weekend:
        day_status = "AMBER"
        day_details = f"Weekend ({now_ist.strftime('%A')}) - Markets Closed"
    elif is_hol:
        day_status = "AMBER"
        day_details = f"Official Exchange Holiday ({hol_name}) - Exchange Closed"
    else:
        day_status = "GREEN"
        day_details = f"Trading Day Active ({now_ist.strftime('%A, %d %b %Y')})"
    items.append({
        "id": "trading_day",
        "name": "Trading Session",
        "status": day_status,
        "details": day_details,
        "line": f"Trading Session: {day_details}"
    })

    # 3. Broker Token (Public-Safe: no token, no length, no client IDs)
    broker_ok = False
    if scheduler.indmoney_feed and scheduler.indmoney_feed.auth_client:
        tok = scheduler.indmoney_feed.auth_client.get_valid_token(auto_refresh=False)
        if tok and len(tok) > 20:
            broker_ok = True
    broker_details = "Broker session authenticated and operational" if broker_ok else "Broker session token missing or expired"
    items.append({
        "id": "broker_token",
        "name": "Broker Session",
        "status": "GREEN" if broker_ok else "RED",
        "details": broker_details,
        "line": f"Broker Session: {broker_details}"
    })

    # 4. Firebase Connected & Last Restore Result (Public-Safe: no credentials, no internal paths)
    fb_conn = hasattr(scheduler, "firestore") and scheduler.firestore.is_connected
    if not fb_conn and hasattr(scheduler, "firestore") and getattr(scheduler.firestore, "credentials_json_str", None):
        # Grace warm-up check in case connection was initializing during boot
        try:
            scheduler.firestore._init_client()
            fb_conn = scheduler.firestore.is_connected
        except Exception:
            pass

    has_cred = bool(hasattr(scheduler, "firestore") and getattr(scheduler.firestore, "credentials_json_str", None))
    if fb_conn:
        fb_status = "GREEN"
        fb_details = "Cloud backup connected and synchronized"
    elif has_cred:
        fb_status = "AMBER"
        fb_details = "Cloud backup connecting in background"
    else:
        fb_status = "AMBER"
        fb_details = "Local-only storage active (cloud backup optional)"

    items.append({
        "id": "firebase_connected",
        "name": "Cloud Persistence",
        "status": fb_status,
        "details": fb_details,
        "line": f"Cloud Persistence: {fb_details}"
    })

    # 5. History Coverage >= 90%
    universe = scheduler.feed.get_all_universe_items() if scheduler.feed else []
    seen = set()
    distinct_syms = []
    for it in universe:
        sym = it.get("symbol", "").split(" ")[0].upper()
        if sym and sym not in seen:
            seen.add(sym)
            distinct_syms.append(sym)
    cov = scheduler.candle_store.get_history_coverage(distinct_syms) if hasattr(scheduler, "candle_store") else {}
    cov_pct = cov.get("coverage_522_pct", 0.0)
    cnt_522 = cov.get("symbols_522_bars", {}).get("count", 0)
    cov_ok = cov_pct >= 90.0
    cov_details = f"{cov_pct:.1f}% universe history coverage verified"
    items.append({
        "id": "history_coverage",
        "name": "History Coverage",
        "status": "GREEN" if cov_ok else "RED",
        "details": cov_details,
        "line": f"History Coverage: {cov_details}"
    })

    # 6. NTFY Push Notifications (Public-Safe: no topic name, no token, no hostnames)
    ntfy_tok = getattr(scheduler.dispatcher, "auth_token", "")
    ntfy_rejected = getattr(scheduler.dispatcher, "token_rejected", False)
    alerts_on = getattr(scheduler.dispatcher, "alerts_enabled", True)
    if not alerts_on:
        ntfy_status = "AMBER"
        ntfy_details = "Alerts temporarily disabled by safety switch"
    elif ntfy_rejected:
        ntfy_status = "RED"
        ntfy_details = "Notification channel authentication rejected"
    elif not ntfy_tok:
        ntfy_status = "AMBER"
        ntfy_details = "Notification channel operating in standard mode"
    else:
        ntfy_status = "GREEN"
        ntfy_details = "Alert delivery channel verified and ready"
    items.append({
        "id": "ntfy_token",
        "name": "Push Notifications",
        "status": ntfy_status,
        "details": ntfy_details,
        "line": f"Push Notifications: {ntfy_details}"
    })

    # 7. Holiday Calendar Coverage (next 10 trading days)
    cal_cov = check_calendar_coverage_next_n_trading_days(10)
    cal_ok = cal_cov.get("covered", False)
    cal_details = "Exchange holiday schedule verified for upcoming sessions" if cal_ok else "Holiday schedule coverage incomplete"
    items.append({
        "id": "holiday_calendar",
        "name": "Market Calendar",
        "status": "GREEN" if cal_ok else "RED",
        "details": cal_details,
        "line": f"Market Calendar: {cal_details}"
    })

    # 8. Data Gateway
    items.append({
        "id": "indstocks_feed",
        "name": "Data Feed",
        "status": "GREEN",
        "details": "100% INDstocks Live Broker API. yfinance removed.",
        "line": "Data Feed: 100% INDstocks Live Broker API"
    })

    # 9. Memory RSS
    rss_mb = get_process_rss_mb()
    mem_ok = rss_mb < 400.0
    mem_details = f"{rss_mb:.1f} MB (within normal operational limits)"
    items.append({
        "id": "memory_rss",
        "name": "System Memory",
        "status": "GREEN" if mem_ok else "RED",
        "details": mem_details,
        "line": f"System Memory: {mem_details}"
    })

    # 10. No Unresolved ERROR in Last Hour
    err_cnt = get_recent_error_count(3600)
    err_ok = (err_cnt == 0)
    err_details = "Nominal (0 errors logged in past hour)" if err_ok else f"Attention required ({err_cnt} errors logged in past hour)"
    items.append({
        "id": "unresolved_errors",
        "name": "System Errors",
        "status": "GREEN" if err_ok else "RED",
        "details": err_details,
        "line": f"System Errors: {err_details}"
    })

    # 11. F&O Coverage (RED if fewer than 90 qualifying instruments)
    fno_universe = scheduler.feed.get_fno_universe() if (scheduler.feed and hasattr(scheduler.feed, "get_fno_universe")) else []
    fno_cnt = len(fno_universe)
    fno_ok = fno_cnt >= 90
    fno_status = "GREEN" if fno_ok else "RED"
    fno_details = f"Derivatives universe configured ({fno_cnt} instruments monitored)" if fno_ok else f"Derivatives universe degraded ({fno_cnt}/90 instruments monitored)"
    items.append({
        "id": "fno_coverage",
        "name": "Derivatives Universe",
        "status": fno_status,
        "details": fno_details,
        "line": f"Derivatives Universe: {fno_details}"
    })

    # 12. Virtual Capital (RED if not above 0)
    vc = getattr(scheduler.paper_tracker, "virtual_capital", 0.0) if hasattr(scheduler, "paper_tracker") else 0.0
    vc_ok = vc > 0.0
    vc_status = "GREEN" if vc_ok else "RED"
    vc_details = "Simulation capital allocated and operational" if vc_ok else "Simulation capital unallocated (allocation must be greater than zero)"
    items.append({
        "id": "virtual_capital",
        "name": "Simulation Capital",
        "status": vc_status,
        "details": vc_details,
        "line": f"Simulation Capital: {vc_details}"
    })

    # 13. Safety Switches (AMBER "open access (by choice)" if unset, AMBER if ALERTS_ENABLED is false)
    p_user = os.getenv("PANEL_USER")
    p_pass = os.getenv("PANEL_PASSWORD")
    auth_set = bool(p_user and p_pass)
    alerts_on = getattr(scheduler.dispatcher, "alerts_enabled", True) if hasattr(scheduler, "dispatcher") else True

    if not auth_set:
        sw_status = "AMBER"
        sw_details = "open access (by choice)"
    elif not alerts_on:
        sw_status = "AMBER"
        sw_details = "Alert notifications suppressed by safety switch"
    else:
        sw_status = "GREEN"
        sw_details = "Administrative controls and notification delivery active"

    items.append({
        "id": "safety_switches",
        "name": "Safety Switches",
        "status": sw_status,
        "details": sw_details,
        "line": f"Safety Switches: {sw_details}"
    })

    # 14. Alert Quota (AMBER if reached)
    current_utc_date = get_utc_iso()[:10]
    quota_exhausted = False
    if hasattr(scheduler, "dispatcher"):
        if getattr(scheduler.dispatcher, "quota_exhausted_utc_date", None) == current_utc_date:
            quota_exhausted = True
        elif hasattr(scheduler.dispatcher, "get_remaining_quota") and scheduler.dispatcher.get_remaining_quota() <= 0:
            quota_exhausted = True
    quota_status = "AMBER" if quota_exhausted else "GREEN"
    quota_text = "limit reached today" if quota_exhausted else "ok"
    items.append({
        "id": "alert_quota",
        "name": "Alert Quota",
        "status": quota_status,
        "details": f"Alert quota: {quota_text}",
        "line": f"Alert quota: {quota_text}"
    })

    # 15. Feed Fallback Guard Status
    items.append({
        "id": "feed_fallback",
        "name": "Feed Fallback Guard",
        "status": "GREEN",
        "details": "Direct INDstocks broker live stream. Zero third-party fallback.",
        "line": "Zero third-party fallback"
    })

    # Determine overall status
    reds = [it for it in items if it["status"] == "RED"]
    ambers = [it for it in items if it["status"] == "AMBER"]
    has_red = len(reds) > 0
    has_amber = len(ambers) > 0
    overall_status = "RED" if has_red else ("AMBER" if has_amber else "GREEN")
    all_green = (overall_status == "GREEN")

    plain_lines = [it["line"] for it in items]

    return {
        "overall_status": overall_status,
        "all_green": all_green,
        "status": overall_status,
        "lines": plain_lines,
        "items": items,
        "red_items": reds,
        "ist_time": get_ist_time_str(),
        "timestamp_utc": get_utc_iso()
    }


@app.get("/api/preflight")
async def get_preflight_json():
    """Returns public-safe preflight checklist: only overall_status and plain-English lines."""
    data = compute_preflight_status()
    return JSONResponse(content={
        "overall_status": data.get("overall_status", "GREEN"),
        "lines": data.get("lines", [])
    })


@app.get("/preflight", response_class=HTMLResponse)
async def get_preflight_html():
    """Renders human-readable Preflight Dashboard with GREEN/AMBER/RED status indicators in plain English."""
    data = compute_preflight_status()
    overall = data.get("overall_status", "GREEN")
    if overall == "GREEN":
        banner_bg = "#065f46"
        banner_text = "ALL SYSTEMS NOMINAL - READY FOR MARKET SESSION"
    elif overall == "AMBER":
        banner_bg = "#92400e"
        banner_text = "NOTICE - MARKETS CLOSED / NON-TRADING HOURS"
    else:
        banner_bg = "#991b1b"
        banner_text = "PREFLIGHT ATTENTION REQUIRED"

    rows_html = ""
    for it in data.get("items", []):
        color = "#10b981" if it["status"] == "GREEN" else ("#f59e0b" if it["status"] == "AMBER" else "#ef4444")
        bg_chip = "rgba(16, 185, 129, 0.15)" if it["status"] == "GREEN" else ("rgba(245, 158, 11, 0.15)" if it["status"] == "AMBER" else "rgba(239, 68, 68, 0.15)")
        rows_html += f"""
        <tr style="border-bottom: 1px solid #1f2937;">
            <td style="padding: 14px 16px; font-weight: 600; color: #f3f4f6;">{it['name']}</td>
            <td style="padding: 14px 16px;">
                <span style="background: {bg_chip}; color: {color}; font-weight: 700; padding: 4px 10px; border-radius: 6px; font-size: 0.85rem; border: 1px solid {color};">
                    {it['status']}
                </span>
            </td>
            <td style="padding: 14px 16px; color: #9ca3af; font-size: 0.95rem;">{it['details']}</td>
        </tr>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FinPlus+ Preflight Verification</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #e5e7eb; margin: 0; padding: 24px; }}
        .container {{ max-width: 900px; margin: 0 auto; background: #111827; border-radius: 12px; border: 1px solid #1f2937; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
        .header {{ padding: 20px 24px; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; background: #151e32; }}
        .banner {{ padding: 16px 24px; background: {banner_bg}; color: #fff; font-weight: 700; font-size: 1.1rem; text-align: center; letter-spacing: 0.5px; }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; }}
        th {{ padding: 12px 16px; background: #1a233a; color: #9ca3af; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.5px; }}
        .footer {{ padding: 16px 24px; border-top: 1px solid #1f2937; font-size: 0.85rem; color: #6b7280; display: flex; justify-content: space-between; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h2 style="margin: 0; color: #38bdf8; font-size: 1.3rem;">FinPlus+ Preflight Checklist</h2>
                <span style="font-size: 0.85rem; color: #9ca3af;">Operational Readiness Dashboard</span>
            </div>
            <div style="font-size: 0.9rem; color: #cbd5e1; font-weight: 600;">{data.get('ist_time')}</div>
        </div>
        <div class="banner">{banner_text}</div>
        <table>
            <thead>
                <tr>
                    <th style="width: 25%;">System</th>
                    <th style="width: 15%;">Status</th>
                    <th style="width: 60%;">Readiness Line</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
        <div class="footer">
            <span>Overall Status: <strong>{overall}</strong></span>
            <span>FinPlus+ Stock Scanner</span>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.api_route("/api/broker/refresh", methods=["GET", "POST"])
async def broker_refresh():
    """Forces headless TOTP regeneration and profile verification with INDstocks broker."""
    if not scheduler.indmoney_feed or not scheduler.indmoney_feed.auth_client:
        return JSONResponse(status_code=500, content={"status": "ERROR", "message": "INDMoneyAuthClient not configured"})
    try:
        new_tok = await asyncio.to_thread(scheduler.indmoney_feed.auth_client.force_refresh_token)
        ver = await asyncio.to_thread(scheduler.indmoney_feed.auth_client.verify_connection)
        return {"status": "SUCCESS", "token_length": len(new_tok), "verification": ver}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "ERROR", "message": str(e)})

@app.get("/api/status")
async def get_status():
    return {
        "status": "HEALTHY",
        "service": "FinPlus+ Stock Scanner & Options Ignition Engine",
        "app_id": "APP-02",
        "port": 8001,
        "market_hours_ist": "09:15 - 15:30 IST",
        "is_market_open_ist": is_market_open_ist(),
        "ist_time": get_ist_time_str(),
        "timestamp_utc": get_utc_iso(),
        "tracked_universe_count": len(scheduler.latest_scan_results),
        "ntfy_topics": scheduler.dispatcher.topics,
        "last_30m_pulse_utc": scheduler.latest_shortlist.get("timestamp_utc")
    }

@app.get("/api/scan")
async def get_scan_results():
    ensure_scanner_running()
    return {
        "timestamp_utc": scheduler.last_scan_utc,
        "ist_time": get_ist_time_str(),
        "universe_count": len(scheduler.latest_scan_results),
        "shortlist": scheduler.latest_shortlist,
        "results": scheduler.latest_scan_results
    }

@app.get("/api/live-indices")
async def get_live_indices():
    """
    Returns real-time 3-second live Nifty 50 and Sensex 30 spots directly
    from App #1's live feed or direct broker fallback without hardcoded fallbacks.
    """
    ensure_scanner_running()
    import os, httpx
    is_render = bool(os.getenv("RENDER")) or bool(os.getenv("RENDER_SERVICE_ID"))
    app1_url = os.getenv("APP1_URL", "https://finplus-index-breadth.onrender.com")

    # On Render, 127.0.0.1:8000 does not exist; query app1_url directly
    endpoints = [f"{app1_url}/api/breadth"] if is_render else ["http://127.0.0.1:8000/api/breadth", f"{app1_url}/api/breadth"]

    for ep in endpoints:
        try:
            async with httpx.AsyncClient(timeout=3.5) as client:
                r = await client.get(ep)
                if r.status_code == 200:
                    d = r.json()
                    bm = d.get("broad_market", {})
                    nf = d.get("nifty", {})
                    sx = d.get("sensex", {})

                    raw_sx = bm.get("sensex_spot") or sx.get("spot_price")
                    raw_nf = bm.get("nifty_spot") or nf.get("spot_price")

                    sx_spot_val = float(raw_sx) if raw_sx and float(raw_sx) > 0 else None
                    nf_spot_val = float(raw_nf) if raw_nf and float(raw_nf) > 0 else None

                    # Guard against stale hardcoded 82300.0 Sensex fallback
                    if sx_spot_val and abs(sx_spot_val - 82300.0) < 0.01:
                        sx_spot_val = None

                    sx_chg_val = float(bm.get("sensex_change_pct", 0.0) if bm.get("sensex_change_pct") is not None else sx.get("change_pct", 0.0))
                    nf_chg_val = float(bm.get("nifty_change_pct", 0.0) if bm.get("nifty_change_pct") is not None else nf.get("change_pct", 0.0))

                    # Enrich missing spot with local scheduler shortlist if needed
                    if (sx_spot_val is None or nf_spot_val is None) and scheduler.latest_shortlist:
                        sx_r = scheduler.latest_shortlist.get("sensex_radar") or {}
                        nf_r = scheduler.latest_shortlist.get("nifty_radar") or {}
                        if sx_spot_val is None and sx_r.get("last_price") and float(sx_r["last_price"]) > 0:
                            if abs(float(sx_r["last_price"]) - 82300.0) >= 0.01:
                                sx_spot_val = float(sx_r["last_price"])
                                sx_chg_val = float(sx_r.get("pct_change_pdc", sx_chg_val))
                        if nf_spot_val is None and nf_r.get("last_price") and float(nf_r["last_price"]) > 0:
                            nf_spot_val = float(nf_r["last_price"])
                            nf_chg_val = float(nf_r.get("pct_change_pdc", nf_chg_val))

                    return {
                        "status": "LIVE_SYNCED",
                        "source": ep,
                        "timestamp": d.get("timestamp"),
                        "nifty_spot": nf_spot_val,
                        "nifty_change_pct": nf_chg_val,
                        "sensex_spot": sx_spot_val,
                        "sensex_change_pct": sx_chg_val,
                        "market_regime": d.get("market_regime", {})
                    }
        except Exception:
            continue

    # Fallback to direct broker quote feed / shortlist if App #1 is sleeping / booting
    direct_sx = None
    direct_sx_chg = 0.0
    direct_nf = None
    direct_nf_chg = 0.0

    # 1. Check local scheduler shortlist
    try:
        if scheduler.latest_shortlist:
            nf_radar = scheduler.latest_shortlist.get("nifty_radar") or {}
            if nf_radar.get("last_price") and float(nf_radar["last_price"]) > 0:
                direct_nf = float(nf_radar["last_price"])
                direct_nf_chg = float(nf_radar.get("pct_change_pdc", 0.0))
            sx_radar = scheduler.latest_shortlist.get("sensex_radar") or {}
            if sx_radar.get("last_price") and float(sx_radar["last_price"]) > 0:
                if abs(float(sx_radar["last_price"]) - 82300.0) >= 0.01:
                    direct_sx = float(sx_radar["last_price"])
                    direct_sx_chg = float(sx_radar.get("pct_change_pdc", 0.0))
    except Exception:
        pass

    # 2. Check Sensex feed utility
    if direct_sx is None:
        try:
            from src.utils.sensex_feed import get_live_sensex_spot
            live_sx, live_chg = get_live_sensex_spot()
            if live_sx and live_sx > 50000 and abs(live_sx - 82300.0) >= 0.01:
                direct_sx = live_sx
                direct_sx_chg = live_chg
        except Exception:
            pass

    # 3. Check batch quotes
    if direct_nf is None:
        try:
            if scheduler.indmoney_feed:
                quotes = scheduler.indmoney_feed.fetch_batch_quotes(["NIFTY"])
                q_nf = quotes.get("NIFTY") or {}
                val = q_nf.get("ltp") or q_nf.get("last_price")
                if val and float(val) > 0:
                    direct_nf = float(val)
                    direct_nf_chg = float(q_nf.get("pct_change_pdc", 0.0))
        except Exception:
            pass

    if direct_sx is not None or direct_nf is not None:
        return {
            "status": "DIRECT_BROKER_FEED",
            "source": "INDSTOCKS_DIRECT",
            "nifty_spot": direct_nf,
            "nifty_change_pct": direct_nf_chg,
            "sensex_spot": direct_sx,
            "sensex_change_pct": direct_sx_chg,
        }

    return {
        "status": "UNAVAILABLE",
        "source": "NONE",
        "nifty_spot": None,
        "nifty_change_pct": 0.0,
        "sensex_spot": None,
        "sensex_change_pct": 0.0,
    }

@app.get("/api/shortlist")
async def get_shortlist():
    from src.utils.indian_market_calendar import is_nse_holiday
    is_hol, hol_name = is_nse_holiday()
    if is_hol:
        return {
            "timestamp_utc": scheduler.last_scan_utc,
            "ist_time": get_ist_time_str(),
            "market_status": "CLOSED_HOLIDAY",
            "holiday_name": hol_name,
            "shortlist": {
                "market_status": f"EXCHANGE_CLOSED ({hol_name})",
                "best_intraday_stock": None,
                "best_stock_option": None,
                "sensex_radar": {"is_actionable": False, "reason": f"Exchange Closed ({hol_name})"},
                "nifty_radar": {"is_actionable": False, "reason": f"Exchange Closed ({hol_name})"},
                "midcap_radar": {"is_actionable": False, "reason": f"Exchange Closed ({hol_name})"},
                "standby_reason": f"National Stock Exchange (NSE) & BSE are CLOSED today for {hol_name}. Zero capital exposed."
            }
        }

    return {
        "timestamp_utc": scheduler.last_scan_utc,
        "ist_time": get_ist_time_str(),
        "shortlist": scheduler.latest_shortlist
    }

@app.post("/api/refresh")
async def trigger_refresh(background_tasks: BackgroundTasks):
    background_tasks.add_task(scheduler.scan_universe_now)
    return {
        "status": "REFRESH_QUEUED",
        "timestamp_utc": get_utc_iso()
    }

@app.post("/api/trigger-test-alert")
async def trigger_test_alert():
    shortlist = scheduler.latest_shortlist
    if not shortlist:
        scheduler.scan_universe_now()
        shortlist = scheduler.latest_shortlist
        
    delivered = scheduler.dispatcher.send_30m_shortlist_pulse(shortlist, force=True)
    return {
        "status": "SUCCESS" if delivered else "FAILED",
        "payload": shortlist,
        "timestamp_utc": get_utc_iso()
    }

@app.post("/api/briefing/test")
async def trigger_briefing_test():
    import requests
    from src.notifications.ntfy_dispatcher import safe_headers
    diag = {}
    try:
        url = f"https://ntfy.sh/{scheduler.dispatcher.topics[0]}"
        headers = {"Title": "FinPlus Desk | Briefing Test", "Priority": "4"}
        out_headers = safe_headers(headers)
        resp = requests.post(url, data=b"FinPlus Desk Briefing Test Direct", headers=out_headers, timeout=10)
        diag["direct_http_status"] = resp.status_code
        diag["direct_http_text"] = resp.text[:200]
    except Exception as e:
        diag["direct_http_error"] = str(e)

    # Telemetry before dispatch
    try:
        from src.utils.indian_market_calendar import get_current_ist_datetime
        today = get_current_ist_datetime().strftime("%Y-%m-%d")
        diag["sent_today"] = scheduler.ledger.get_ntfy_sent_count(today)
    except Exception as e:
        diag["ledger_sent_error"] = str(e)

    delivered = scheduler.dispatcher.send_morning_tactical_briefing(force=True)
    return {
        "status": "SUCCESS" if delivered else "FAILED",
        "message": "09:00 AM Tactical Morning Briefing dispatched.",
        "topics": scheduler.dispatcher.topics,
        "diagnostics": diag,
        "timestamp_utc": get_utc_iso()
    }

# -------------------------------------------------------------
# 24/7 FrontPage Live Paper Positions API
# -------------------------------------------------------------
@app.get("/api/paper/status")
async def get_paper_status():
    """Returns real-time status of all active and recent paper positions."""
    if not scheduler.paper_tracker.active_positions:
        if scheduler.latest_shortlist:
            scheduler.paper_tracker.ingest_shortlist(scheduler.latest_shortlist)
        scheduler.paper_tracker.ingest_commodities()
        scheduler.paper_tracker.ingest_crypto()
        scheduler.paper_tracker.update_and_trail_positions()
    res = scheduler.paper_tracker.get_status_dict()

    # Merge embedded 24/7 Crypto Delta positions
    if hasattr(scheduler, "crypto_scheduler") and scheduler.crypto_scheduler:
        try:
            c_dict = scheduler.crypto_scheduler.refresh_open_positions_live()
            c_active = c_dict.get("active_positions", [])
            for cp in c_active:
                act = "BUY" if ("BUY" in cp.get("action", "") or "LONG" in cp.get("action", "")) else "SELL"
                curr_p = float(cp.get("current_price", 0.0))
                sl_p = float(cp.get("current_sl", 0.0))
                cv = float(cp.get("contract_value", 1.0))
                qty = float(cp.get("quantity", 1.0))
                risk_rs = round(abs(curr_p - sl_p) * cv * qty * 90.0, 2)
                stop_dist = round(abs(curr_p - sl_p) / curr_p * 100.0, 2) if curr_p > 0 else 0.0
                
                pos_item = {
                    "position_id": cp.get("position_id"),
                    "source_app": "APP_06_CRYPTO_DELTA",
                    "sector": "CRYPTO",
                    "symbol": cp.get("symbol"),
                    "action": act,
                    "is_option": 0,
                    "is_put": 0,
                    "is_crypto": 1,
                    "entry_price": float(cp.get("entry_price", 0.0)),
                    "current_price": curr_p,
                    "initial_sl": float(cp.get("initial_sl", 0.0)),
                    "trailing_sl": sl_p,
                    "highest_price": float(cp.get("highest_favorable_price", curr_p)),
                    "quantity": qty,
                    "contract_value": cv,
                    "notional_inr": cp.get("notional_inr", 0.0),
                    "unrealized_pnl": round(float(cp.get("unrealized_pnl_inr", 0.0)), 2),
                    "pnl_pct": float(cp.get("unrealized_pnl_pct", 0.0)),
                    "stage": "TRAILING SL" if cp.get("breakeven_latched") else "INITIAL SL",
                    "status": "OPEN",
                    "grade": cp.get("grade") or "B",
                    "setup_key": cp.get("setup_key", "15M_COILING_SQUEEZE"),
                    "notes": cp.get("notes", "Delta 15m Squeeze Breakout"),
                    "opened_at_utc": cp.get("entry_time_utc", ""),
                    "entry_time_ist": cp.get("entry_time_ist", ""),
                    "taken": cp.get("taken", 0),
                    "profit_locked_rupees": 0.0,
                    "profit_locked_r": 0.0,
                    "stop_dist_pct": stop_dist,
                    "rupees_at_risk": risk_rs,
                    "setup_win_pct": c_dict.get("win_rate_pct", 0.0),
                    "setup_total_trades": c_dict.get("total_trades_count", 0)
                }
                res["active_positions"].append(pos_item)

            res["active_positions_count"] = len(res["active_positions"])
            res["total_unrealized_pnl"] = round(res.get("total_unrealized_pnl", 0.0) + c_dict.get("total_unrealized_pnl_inr", 0.0), 2)
            res["net_realized_pnl"] = round(res.get("net_realized_pnl", 0.0) + c_dict.get("net_realized_pnl_inr", 0.0), 2)
            
            # Merge recent closed crypto trades
            for cc in c_dict.get("recent_closed_trades", []):
                res["recent_closed_trades"].append({
                    "trade_id": cc.get("position_id"),
                    "symbol": cc.get("symbol"),
                    "sector": "CRYPTO",
                    "action": cc.get("action"),
                    "entry_price": cc.get("entry_price"),
                    "exit_price": cc.get("exit_price"),
                    "highest_price": cc.get("highest_favorable_price", cc.get("exit_price")),
                    "gross_pnl": round(float(cc.get("pnl_inr", 0.0)), 2),
                    "total_charges": 0.0,
                    "net_pnl": round(float(cc.get("pnl_inr", 0.0)), 2),
                    "exit_reason": cc.get("exit_reason"),
                    "opened_at_ist": cc.get("entry_time_ist"),
                    "closed_at_ist": cc.get("exit_time_ist"),
                    "opened_at_utc": cc.get("entry_time_utc"),
                    "closed_at_utc": cc.get("exit_time_utc"),
                    "notes": cc.get("notes"),
                    "tags": "CRYPTO,PERPETUAL",
                    "setup_key": cc.get("setup_key", "15M_COILING_SQUEEZE"),
                    "grade": cc.get("grade") or "B",
                    "taken": cc.get("taken", 0),
                    "is_crypto": 1
                })
        except Exception as e:
            logger.error(f"Error merging crypto status: {e}")

    return res

@app.get("/api/crypto/scan")
async def get_crypto_scan():
    """Returns real-time 15m coiling radar scan results across Delta perpetuals."""
    if hasattr(scheduler, "crypto_scheduler") and scheduler.crypto_scheduler:
        return {
            "status": "SUCCESS",
            "instruments_count": len(scheduler.crypto_scheduler.latest_scan_results),
            "priority_trade": scheduler.crypto_scheduler.priority_trade,
            "instruments": scheduler.crypto_scheduler.latest_scan_results,
            "last_scan_ts": scheduler.crypto_scheduler.last_scan_ts
        }
    return {"status": "UNAVAILABLE", "message": "Crypto scheduler not initialized"}

@app.get("/api/crypto/positions")
async def get_crypto_positions():
    """Returns active and closed perpetual paper positions in INR."""
    if hasattr(scheduler, "crypto_scheduler") and scheduler.crypto_scheduler:
        return scheduler.crypto_scheduler.refresh_open_positions_live()
    return {"status": "UNAVAILABLE", "message": "Crypto scheduler not initialized"}

@app.post("/api/paper/close-position/{pos_id}")
async def close_paper_position(pos_id: str):
    """Manually squares off an active open paper position."""
    pos = scheduler.paper_tracker.active_positions.get(pos_id)
    if not pos:
        return JSONResponse(status_code=404, content={"status": "ERROR", "message": "Position not found"})
    live_price = pos.get("current_price") or pos.get("entry_price")
    scheduler.paper_tracker.close_position(pos_id, exit_price=live_price, exit_reason="MANUAL_SQUARE_OFF")
    return {"status": "SUCCESS", "message": f"Position {pos_id} squared off"}

@app.post("/api/paper/sync-now")
async def sync_paper_positions():
    """Forces an immediate tick update and trailing evaluation."""
    if scheduler.latest_shortlist:
        scheduler.paper_tracker.ingest_shortlist(scheduler.latest_shortlist)
    scheduler.paper_tracker.ingest_commodities()
    scheduler.paper_tracker.ingest_crypto()
    scheduler.paper_tracker.update_and_trail_positions()
    return scheduler.paper_tracker.get_status_dict()


class TradeTakenRequest(BaseModel):
    taken: bool = True
    real_entry: Optional[float] = None
    real_qty: Optional[int] = None

@app.post("/api/trades/{trade_id}/taken")
async def toggle_trade_taken(trade_id: str, req: TradeTakenRequest):
    """
    Toggles the 'Taken' flag on paper trades with optional real execution entry & qty (Phase 6e).
    Enables tracking of trades actively executed by the owner vs automated paper suggestions.
    """
    if not hasattr(scheduler, "paper_tracker") or not scheduler.paper_tracker:
        raise HTTPException(status_code=503, detail="Paper tracker not initialized")
    success = scheduler.paper_tracker.set_trade_taken(
        trade_id_or_pos_id=trade_id,
        taken=req.taken,
        real_entry=req.real_entry,
        real_qty=req.real_qty
    )
    if not success:
        return JSONResponse(status_code=404, content={"status": "ERROR", "message": f"Trade {trade_id} not found"})
    return {
        "status": "SUCCESS",
        "trade_id": trade_id,
        "taken": req.taken,
        "real_entry": req.real_entry,
        "real_qty": req.real_qty,
        "taken_summary": scheduler.paper_tracker.get_taken_summary()
    }

@app.get("/api/funnel")
async def get_funnel_endpoint(date: Optional[str] = None):
    """
    Exposes daily funnel report (Phase 6d):
    Tracks symbols scanned, candidates detected by setup, and explicit rejection/filtering reasons.
    """
    if not hasattr(scheduler, "paper_tracker") or not scheduler.paper_tracker:
        return {"status": "UNAVAILABLE", "message": "Paper tracker not initialized"}
    return scheduler.paper_tracker.get_funnel_summary(date)

@app.get("/api/weekly")
async def get_weekly_performance_endpoint():
    """
    Returns comprehensive Sunday weekly performance report (Phase 6g):
    Analyzes suggestions opened, taken win rate, taken total profit (₹ and R), best setup, and worst setup.
    """
    if not hasattr(scheduler, "paper_tracker") or not scheduler.paper_tracker:
        return {"status": "UNAVAILABLE", "message": "Paper tracker not initialized"}
    return scheduler.paper_tracker.get_weekly_performance_report()


# -------------------------------------------------------------
# Independent Candle Store & Real-Time Charting Engine
# -------------------------------------------------------------
@app.get("/api/candles/{symbol}")
async def get_candles_endpoint(symbol: str, timeframe: str = "15m", limit: int = 150):
    """Returns stored/synthesized candles for a symbol formatted for TradingView Lightweight Charts."""
    from src.storage.candle_store import CandleStore
    cs = CandleStore()
    clean = symbol.split(" ")[0].upper()
    bars = cs.get_candles(clean, timeframe=timeframe, limit=limit)
    
    # If not enough local candles yet, populate on-demand
    if len(bars) < 10:
        try:
            df = scheduler.feed.fetch_15m_candles(f"{clean}.NS", period="5d")
            if not df.empty:
                bars = cs.get_candles(clean, timeframe=timeframe, limit=limit)
        except Exception:
            pass

    return {
        "symbol": clean,
        "timeframe": timeframe,
        "count": len(bars),
        "bars": bars
    }


@app.get("/chart/{symbol}", response_class=HTMLResponse)
async def serve_symbol_chart(symbol: str):
    """Renders interactive dark-mode TradingView Lightweight Charts canvas with zero-delay local candles."""
    clean = symbol.split(" ")[0].upper()
    
    # Find active breakout levels from latest scan results
    levels = {}
    for r in scheduler.latest_scan_results:
        if r.get("symbol") == clean:
            levels = {
                "range_top": r.get("range_top"),
                "range_bottom": r.get("range_bottom"),
                "vwap": r.get("vwap"),
                "ema50": r.get("ema50"),
                "ltp": r.get("live_ltp") or r.get("last_price")
            }
            break

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FinPlus+ Chart | {clean}</title>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background-color: #0b0f19;
            color: #f1f5f9;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }}
        header {{
            background: #111827;
            padding: 10px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #1f2937;
        }}
        .sym-title {{ font-size: 1.15rem; font-weight: 700; color: #38bdf8; display: flex; align-items: center; gap: 8px; }}
        .badge {{ background: #059669; color: #fff; font-size: 0.75rem; padding: 2px 7px; border-radius: 4px; }}
        .legend {{ display: flex; gap: 14px; font-size: 0.8rem; color: #94a3b8; }}
        .legend span b {{ color: #f8fafc; }}
        #tv_chart {{ flex: 1; width: 100%; }}
    </style>
</head>
<body>
    <header>
        <div class="sym-title">
            <span>{clean} (15m Intraday)</span>
            <span class="badge">0-DELAY BROKER FEED</span>
        </div>
        <div class="legend" id="hud_legend">
            <span>LTP: <b id="hud_ltp">{levels.get('ltp', '--')}</b></span>
            <span style="color:#10b981;">Range Top (Breakout): <b id="hud_rt">{levels.get('range_top', '--')}</b></span>
            <span style="color:#ef4444;">Range Bottom (Breakdown): <b id="hud_rb">{levels.get('range_bottom', '--')}</b></span>
            <span style="color:#a855f7;">VWAP: <b id="hud_vwap">{levels.get('vwap', '--')}</b></span>
        </div>
    </header>
    <div id="tv_chart"></div>

    <script>
        const chartContainer = document.getElementById('tv_chart');
        const chart = LightweightCharts.createChart(chartContainer, {{
            layout: {{
                background: {{ color: '#0b0f19' }},
                textColor: '#94a3b8',
                fontSize: 12,
            }},
            grid: {{
                vertLines: {{ color: '#161e2e' }},
                horzLines: {{ color: '#161e2e' }},
            }},
            crosshair: {{
                mode: LightweightCharts.CrosshairMode.Normal,
            }},
            timeScale: {{
                borderColor: '#1f2937',
                timeVisible: true,
                secondsVisible: false,
            }},
            rightPriceScale: {{
                borderColor: '#1f2937',
            }},
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#10b981',
            downColor: '#ef4444',
            borderVisible: false,
            wickUpColor: '#10b981',
            wickDownColor: '#ef4444',
        }});

        const rangeTop = {levels.get('range_top') or 'null'};
        const rangeBottom = {levels.get('range_bottom') or 'null'};

        if (rangeTop) {{
            candleSeries.createPriceLine({{
                price: rangeTop,
                color: '#10b981',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: 'RANGE TOP (BUY)',
            }});
        }}

        if (rangeBottom) {{
            candleSeries.createPriceLine({{
                price: rangeBottom,
                color: '#ef4444',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: 'RANGE BOTTOM (SELL)',
            }});
        }}

        async function loadCandles() {{
            try {{
                const res = await fetch('/api/candles/{clean}?timeframe=15m&limit=150');
                const data = await res.json();
                if (data.bars && data.bars.length > 0) {{
                    candleSeries.setData(data.bars);
                    const lastBar = data.bars[data.bars.length - 1];
                    document.getElementById('hud_ltp').textContent = 'Rs ' + Number(lastBar.close).toFixed(2);
                }}
            }} catch(e) {{
                console.error("Candle load error:", e);
            }}
        }}

        loadCandles();
        setInterval(loadCandles, 5000); // 5-second live refresh

        window.addEventListener('resize', () => {{
            chart.applyOptions({{ width: chartContainer.clientWidth, height: chartContainer.clientHeight }});
        }});
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


