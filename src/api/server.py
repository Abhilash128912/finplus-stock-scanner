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
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from src.scheduler.scanner_scheduler import ScannerScheduler
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str, is_market_open_ist

logger = setup_logger("APIServer")

app = FastAPI(
    title="FinPlus+ Stock Scanner & Options Ignition Engine",
    description="Early Ignition Options & High-Ticket Stock Scanner",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(_APP_ROOT, "finplus_stock_scanner", "frontend")
if not os.path.exists(FRONTEND_DIR):
    FRONTEND_DIR = os.path.join(_APP_ROOT, "frontend")

if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

scheduler = ScannerScheduler()
scheduler_task = None

@app.on_event("startup")
async def startup_event():
    global scheduler_task
    logger.info("Booting FinPlus+ Stock Scanner Server (Port 8001)...")
    scheduler_task = asyncio.create_task(scheduler.start_loop())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down FinPlus+ Stock Scanner Server...")
    scheduler.stop_loop()
    if scheduler_task:
        scheduler_task.cancel()

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>FinPlus+ Stock Scanner API Online</h1>")

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
    return {
        "timestamp_utc": scheduler.last_scan_utc,
        "ist_time": get_ist_time_str(),
        "universe_count": len(scheduler.latest_scan_results),
        "shortlist": scheduler.latest_shortlist,
        "results": scheduler.latest_scan_results
    }

@app.get("/api/shortlist")
async def get_shortlist():
    """Returns the #1 Best-in-Class picks per category with detailed points breakdown."""
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
    # Build sample 30-minute shortlist payload with Trend Continuation test
    sample_shortlist = {
        "timestamp_utc": get_utc_iso(),
        "ist_time": get_ist_time_str(),
        "has_actionable_trades": True,
        "best_index_option": {
            "symbol": "SENSEX",
            "last_price": 81350.0,
            "pct_change_pdc": 0.65,
            "recommended_contract": "SENSEX 81400 CE",
            "confluence_score": 85,
            "is_continuation": True,
            "points_breakdown": {
                "range_breakout_pts": 35,
                "pdh_pdl_pts": 30,
                "vwap_pts": 20,
                "volume_pts": 0,
                "total_score": 85
            },
            "risk_management": {
                "premium_stop_loss_pct": 45.0,
                "premium_target_pct": 180.0
            }
        },
        "best_stock_option": {
            "symbol": "MARUTI",
            "lot_size": 50,
            "last_price": 11450.0,
            "pct_change_pdc": 0.72,
            "recommended_contract": "MARUTI 11500 CE",
            "confluence_score": 85,
            "is_continuation": False,
            "points_breakdown": {
                "range_breakout_pts": 35,
                "pdh_pdl_pts": 30,
                "vwap_pts": 20,
                "volume_pts": 0,
                "total_score": 85
            },
            "risk_management": {
                "underlying_sl": 11380.0,
                "underlying_target": 11590.0
            }
        }
    }
    
    delivered = scheduler.dispatcher.send_30m_shortlist_pulse(sample_shortlist, force=True)
    return {
        "status": "SUCCESS" if delivered else "FAILED",
        "payload": sample_shortlist,
        "timestamp_utc": get_utc_iso()
    }
