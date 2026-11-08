@echo off
title FinPlus+ Stock & Options Ignition Scanner (APP-02 :8001)
echo ===================================================================
echo   Starting FinPlus+ Stock & Options Ignition Scanner (APP-02)
echo   Local Cockpit: http://127.0.0.1:8001
echo   App #1 Link:   http://127.0.0.1:8000
echo   NTFY Channel:  https://ntfy.sh/Finplus-Delta
echo   Timezone:      Logs in UTC | Market Logic in IST
echo ===================================================================
cd /d "D:\AI_APPS"
python -m uvicorn finplus_stock_scanner.src.api.server:app --host 127.0.0.1 --port 8001 --reload
pause
