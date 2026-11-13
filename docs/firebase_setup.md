# 🔥 Firebase / Firestore Persistence Setup Guide (Phase 2B)

## 1. Overview
FinPlus+ Stock Scanner uses **Google Cloud Firestore** (under the Free Spark Plan) to mirror durable state:
- Active & closed paper trades
- Trailing stop-loss state and virtual capital
- Schedule ledger milestones and daily breakout latches
- Daily NTFY quota counters
- Historical 15m and 5m candle bars (compressed, 1 document per symbol)

**Local SQLite is always the primary, low-latency working store.** Firestore operates as a durable remote mirror that restores local state on cold container boots on Render without requiring a paid Render Persistent Disk.

---

## 2. Free Spark Plan Quotas & Guardrails
The Google Cloud / Firebase Free Spark tier provides:
- **Stored Data**: 1 GiB free (FinPlus+ uses $\sim 3.5$ MB total for all 783 symbols and paper trades)
- **Document Reads**: 50,000 reads/day
- **Document Writes**: 20,000 writes/day
- **Document Deletes**: 20,000 deletes/day

### Built-in Quota Guard:
- **80% Quota Trigger (16,000 writes / 40,000 reads)**: Pauses non-essential writes (stats, candle uploads). Dispatches NTFY Warning.
- **95% Quota Trigger (19,000 writes / 47,500 reads)**: Only critical trade execution writes permitted. Dispatches NTFY Emergency.
- **100% Quota**: Fails closed into local SQLite mode with zero trade disruption.

---

## 3. Environment Variable Configuration
Credentials are **never stored in the repository or logged**.

In Render Dashboard (or local `.env`):
```bash
# Service Account Private Key JSON content as a single-line string
FIREBASE_CREDENTIALS_JSON='{"type":"service_account","project_id":"finplus-stock-scanner",...}'

# Environment isolation prefix: "live" for production, "test" for local test runs
FIREBASE_ENV_PREFIX=live
```

---

## 4. Firestore Security Rules
All client SDK access is completely **DENIED**. Only the authorized backend service account using the Firebase Admin SDK can read or write.

In Firebase Console -> Firestore Database -> Rules:
```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    // DENY ALL client web/mobile access. Only Firebase Admin SDK backend has access.
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
```

---

## 5. Collection Structure
All documents are partitioned under environment root prefixes to strictly prevent development or test suites from altering live data:

```text
env/
├── live/
│   ├── candles/
│   │   └── {SYMBOL}_{TIMEFRAME}   # Gzipped Base64 matrix of all bars (~3 KB)
│   ├── paper_trades/
│   │   └── {POSITION_ID}          # Full position details and P&L
│   ├── schedule_ledger/
│   │   └── {DATE}_{SLOT_ID}       # Milestone status and bulletin payloads
│   ├── latches/
│   │   └── {DATE}_{SYMBOL}        # Daily breakout deduplication latch
│   └── state/
│       ├── virtual_capital        # Virtual account balance
│       └── ntfy_quota_{DATE}      # Daily alert quota counter
└── test/
    └── ...                        # Isolated test sandbox
```

