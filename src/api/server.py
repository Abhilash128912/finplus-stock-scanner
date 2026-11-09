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
    version="2.2.0"
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
        return FileResponse(cockpit_file)
    return HTMLResponse("<h1>FinPlus+ Cockpit Not Found</h1>")

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "app": "FinPlus-Stock-Scanner"}

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

@app.get("/api/live-indices")
async def get_live_indices():
    """
    Returns real-time 3-second live Nifty 50 and Sensex 30 spots directly
    from App #1's live feed, ensuring continuous live price updates.
    """
    import os, requests
    app1_url = os.getenv("APP1_URL", "https://finplus-index-breadth.onrender.com")
    local_url = "http://127.0.0.1:8000"
    
    endpoints = [f"{local_url}/api/breadth", f"{app1_url}/api/breadth"]
    for ep in endpoints:
        try:
            r = requests.get(ep, timeout=1.5)
            if r.status_code == 200:
                d = r.json()
                bm = d.get("broad_market", {})
                nf = d.get("nifty", {})
                sx = d.get("sensex", {})
                return {
                    "status": "LIVE_SYNCED",
                    "source": ep,
                    "timestamp": d.get("timestamp"),
                    "nifty_spot": float(bm.get("nifty_spot") or nf.get("spot_price") or 25024.2),
                    "nifty_change_pct": float(bm.get("nifty_change_pct", 0.0) if bm.get("nifty_change_pct") is not None else nf.get("change_pct", 0.0)),
                    "sensex_spot": float(bm.get("sensex_spot") or sx.get("spot_price") or 80757.2),
                    "sensex_change_pct": float(bm.get("sensex_change_pct", 0.0) if bm.get("sensex_change_pct") is not None else sx.get("change_pct", 0.0)),
                    "market_regime": d.get("market_regime", {})
                }
        except Exception:
            continue
            
    nf_item = scheduler.latest_shortlist.get("nifty_radar", {})
    sx_item = scheduler.latest_shortlist.get("sensex_radar", {})
    return {
        "status": "CACHED_FALLBACK",
        "nifty_spot": float(nf_item.get("last_price", 25024.2)),
        "nifty_change_pct": float(nf_item.get("pct_change_pdc", 0.0)),
        "sensex_spot": float(sx_item.get("last_price", 80757.2)),
        "sensex_change_pct": float(sx_item.get("pct_change_pdc", 0.0)),
    }

@app.get("/api/shortlist")
async def get_shortlist():
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
    delivered = scheduler.dispatcher.send_morning_tactical_briefing(force=True)
    return {
        "status": "SUCCESS" if delivered else "FAILED",
        "message": "09:00 AM Tactical Morning Briefing dispatched.",
        "topics": scheduler.dispatcher.topics,
        "timestamp_utc": get_utc_iso()
    }
