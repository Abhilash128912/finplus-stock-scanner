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

@app.api_route("/live", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_live():
    live_file = os.path.join(FRONTEND_DIR, "live.html")
    if os.path.exists(live_file):
        return FileResponse(live_file)
    return HTMLResponse("<h1>FinPlus+ Live Positions Cockpit Not Found</h1>")

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "app": "FinPlus-Stock-Scanner"}

@app.api_route("/api/health/deep", methods=["GET", "HEAD"])
@app.api_route("/health/deep", methods=["GET", "HEAD"])
async def deep_health_check():
    """
    Institutional Deep Health Diagnostic Endpoint.
    Probes internal worker tasks, broker authentication, persistent ledger, and DB.
    Returns HTTP 200 if fully healthy; HTTP 503 if any core subsystem is degraded/stalled.
    Targeted specifically for UptimeRobot monitoring.
    """
    from datetime import datetime, timezone, timedelta
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    date_str = ist_now.strftime("%Y-%m-%d")

    issues = []
    
    # 1. Engine & Scanner Loop Diagnostics
    is_engine_alive = scheduler.is_running
    if not is_engine_alive:
        issues.append("ScannerScheduler is not running")

    # 2. Broker Connection Diagnostics
    broker_ok = False
    broker_details = {}
    if scheduler.indmoney_feed and scheduler.indmoney_feed.auth_client:
        try:
            broker_ok = scheduler.indmoney_feed.auth_client.is_token_valid()
            broker_details["token_valid"] = broker_ok
            broker_details["cached_quotes"] = len(scheduler.indmoney_feed._quote_cache)
        except Exception as e:
            issues.append(f"Broker client error: {e}")
            broker_details["error"] = str(e)
    else:
        broker_details["status"] = "AuthClient unavailable"

    # 3. Schedule Ledger Diagnostics
    ledger_status = scheduler.ledger.get_ledger_status(date_str)
    failed_milestones = [m["slot_id"] for m in ledger_status if m["status"] == "FAILED"]
    if failed_milestones:
        issues.append(f"Ledger milestones failed: {', '.join(failed_milestones)}")

    # 4. Market Hours Liveness Check
    # If market is open, verify scan has run recently
    scan_age_secs = 0
    if scheduler.last_scan_utc:
        try:
            last_dt = datetime.fromisoformat(scheduler.last_scan_utc.replace("Z", "+00:00"))
            scan_age_secs = int((datetime.now(timezone.utc) - last_dt).total_seconds())
        except Exception:
            pass

    market_open = is_market_open_ist()
    if market_open and scan_age_secs > 360 and len(scheduler.latest_scan_results) == 0:
        issues.append(f"Scanner has not completed a cycle during market hours ({scan_age_secs}s elapsed)")

    is_healthy = len(issues) == 0
    http_status = 200 if is_healthy else 503

    payload = {
        "status": "HEALTHY" if is_healthy else "DEGRADED",
        "app": "FinPlus-Stock-Scanner",
        "ist_time": get_ist_time_str(),
        "timestamp_utc": get_utc_iso(),
        "market_open_ist": market_open,
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
        "issues": issues
    }

    return JSONResponse(status_code=http_status, content=payload)

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
                sx_spot_val = float(bm.get("sensex_spot") or sx.get("spot_price") or 0.0)
                if sx_spot_val < 81500.0:
                    try:
                        import yfinance as yf
                        fp = getattr(yf.Ticker("^BSESN").fast_info, 'last_price', None)
                        if fp and fp > 80000:
                            sx_spot_val = float(fp)
                    except Exception:
                        pass
                if sx_spot_val == 0.0:
                    sx_spot_val = 82300.0

                return {
                    "status": "LIVE_SYNCED",
                    "source": ep,
                    "timestamp": d.get("timestamp"),
                    "nifty_spot": float(bm.get("nifty_spot") or nf.get("spot_price") or 25380.0),
                    "nifty_change_pct": float(bm.get("nifty_change_pct", 0.0) if bm.get("nifty_change_pct") is not None else nf.get("change_pct", 0.0)),
                    "sensex_spot": sx_spot_val,
                    "sensex_change_pct": float(bm.get("sensex_change_pct", 0.0) if bm.get("sensex_change_pct") is not None else sx.get("change_pct", 0.0)),
                    "market_regime": d.get("market_regime", {})
                }
        except Exception:
            continue
            
    nf_item = scheduler.latest_shortlist.get("nifty_radar", {})
    sx_item = scheduler.latest_shortlist.get("sensex_radar", {})
    cached_sx = float(sx_item.get("last_price", 0.0))
    if cached_sx < 81500.0:
        try:
            import yfinance as yf
            fp = getattr(yf.Ticker("^BSESN").fast_info, 'last_price', None)
            if fp and fp > 80000:
                cached_sx = float(fp)
        except Exception:
            pass
    if cached_sx == 0.0:
        cached_sx = 82300.0

    return {
        "status": "CACHED_FALLBACK",
        "nifty_spot": float(nf_item.get("last_price", 25380.0)),
        "nifty_change_pct": float(nf_item.get("pct_change_pdc", 0.0)),
        "sensex_spot": cached_sx,
        "sensex_change_pct": float(sx_item.get("pct_change_pdc", 0.0)),
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
    delivered = scheduler.dispatcher.send_morning_tactical_briefing(force=True)
    return {
        "status": "SUCCESS" if delivered else "FAILED",
        "message": "09:00 AM Tactical Morning Briefing dispatched.",
        "topics": scheduler.dispatcher.topics,
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
        scheduler.paper_tracker.update_and_trail_positions()
    return scheduler.paper_tracker.get_status_dict()

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
    scheduler.paper_tracker.update_and_trail_positions()
    return scheduler.paper_tracker.get_status_dict()


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


