# 📖 STANDARD OPERATING PROCEDURE: FINPLUS+ STOCK & OPTIONS IGNITION SCANNER (APP-02)
**Application ID:** APP-02  
**Ecosystem Location:** `D:\AI_APPS\finplus_stock_scanner`  
**Dedicated Port:** `http://127.0.0.1:8001`  
**Lead Quantitative Trader:** Abhilash Babu  
**Established:** 2026-11-08  
**Compliance Standard:** ISO-8601 UTC Logging • Indian Standard Time (IST) Market Logic  

---

### 1. Executive Summary & Purpose

Conventional momentum scanners suffer from a fatal flaw: **Late Execution Lag**. They alert traders only after a ticker has already surged or collapsed by $+4\%$ to $+7\%$. At this stage, implied volatility (IV) is inflated, and buying options frequently results in immediate IV crush, top-buying, or sudden mean-reversion whipsaws.

**App #2 (`finplus_stock_scanner`) solves this by identifying the exact moment of volatility expansion at its absolute infancy ($+0.4\%$ to $+1.0\%$ move)**. It couples the mathematical compression of the **LuxAlgo Range Detector** with daily **Previous Day High (PDH)** / **Previous Day Low (PDL)** boundary breaks, cross-verified by **App #1's Composite Trend Index (CTI)**.

---

### 2. Timezone Protocol

* **Internal Auditing & Persistence:** **Strictly ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SS.fZ`)**. Every log file in `logs/` and API JSON response records UTC timestamps.
* **Market Execution Engine:** **Indian Standard Time (IST, `Asia/Kolkata`, UTC+05:30)**.
  * `09:15 IST`: NSE/BSE Market Open
  * `09:30 to 14:45 IST`: Active Early Ignition Scanning Window
  * `15:15 IST`: Intraday Square-off Execution Target
  * `15:30 IST`: Exchange Closing Bell

---

### 3. Portfolio Allocation & Risk Weighting Matrix

| Instrument Category | Strategic Importance | Capital Allocation | Risk & Stop-Loss Protocol | Target & Reward |
| :--- | :--- | :--- | :--- | :--- |
| **BSE SENSEX 30 Options** | 🔴 **HIGH IMPORTANCE** | **Small Capital Tier** ($1$ to $2$ lots) | **Max 40% – 45% Stop Loss on Option Premium** (Defined risk; no harm in losing up to 45% for asymmetric return) | **$+150\%$ to $+250\%$ Option Premium** (Asymmetric Zero-to-Hero Expiry Scalp) |
| **High-Ticket Stock Options** *(Lot $< 500$ + Reliance)* | 🟢 **CORE HIGH IMPORTANCE & PROFITABILITY** | **Standard Position Sizing** | **Stop Loss at Opposite Range Boundary** (Underlying) or **25% Premium Cut** | **$1:2.0$ Risk-to-Reward Ratio on Underlying** |
| **NSE NIFTY 50 Options** | ⚪ **LOW IMPORTANCE** | **Strict Confluence Only** | **Stop Loss at Opposite Range Boundary** | **$1:1.5$ Risk-to-Reward** |

#### Tracked Stock Universe (18 High-Ticket Institutional Assets):
1. `RELIANCE` (Lot: 250)
2. `MARUTI` (Lot: 50)
3. `ULTRACEMCO` (Lot: 100)
4. `BAJFINANCE` (Lot: 125)
5. `COFORGE` (Lot: 150)
6. `LT` (Lot: 175)
7. `TCS` (Lot: 175)
8. `TITAN` (Lot: 175)
9. `EICHERMOT` (Lot: 175)
10. `DIXON` (Lot: 100)
11. `POLYCAB` (Lot: 100)
12. `PERSISTENT` (Lot: 100)
13. `DRREDDY` (Lot: 125)
14. `APOLLOHOSP` (Lot: 125)
15. `HINDUNILVR` (Lot: 300)
16. `INFY` (Lot: 400)
17. `KOTAKBANK` (Lot: 400)
18. `BHARTIARTL` (Lot: 475)

---

### 4. Mathematical Engine: LuxAlgo + PDH / PDL Confluence

1. **Range Coiling Detection:**
   $$\text{ATR} = \text{RMA}(\text{TrueRange}, 500) \times 1.0$$
   $$\text{SMA} = \frac{1}{20} \sum_{i=0}^{19} \text{Close}_{n-i}$$
   $$\text{Count} = \sum_{i=0}^{19} \mathbb{I}(|\text{Close}_{n-i} - \text{SMA}| > \text{ATR})$$
   * When $\text{Count} = 0$, the instrument enters **`COILING_ALERT`** (potential energy compressed).
2. **Ignition Trigger ($+0.4\%$ to $+1.0\%$ from base):**
   * **Bullish Strike (Call Option):** $\text{Close} > \text{Range Top}$ **AND** $\text{Close} > \text{PDH}$ **AND** $\text{Close} > \text{VWAP}$.
   * **Bearish Strike (Put Option):** $\text{Close} < \text{Range Bottom}$ **AND** $\text{Close} < \text{PDL}$ **AND** $\text{Close} < \text{VWAP}$.
3. **App #1 Air Traffic Control Interception:**
   * Automatically queries App #1 (`http://127.0.0.1:8000/api/regime`).
   * If App #1 regime is `CHOPPY / DIVERGENT` or conflicting, the trade is marked `VETOED_BY_CHOP` to prevent whipsaw losses.

---

### 5. Deployment & Execution Commands

- **Local Web Cockpit:** `http://127.0.0.1:8001`
- **Floating HUD:** `http://127.0.0.1:8001/static/floater.html`
- **Standalone Windows Runner:** Double-click [`run_stock_scanner.bat`](file:///D:/AI_APPS/finplus_stock_scanner/run_stock_scanner.bat)
- **NTFY Broadcast Channels:** `https://ntfy.sh/Finplus-Delta`, `https://ntfy.sh/Finplus-Breadth`
