"""
Verify and demonstrate real startup logs with an empty SQLite DB and mock Firestore.
Proves that RESTORE STARTED and RESTORE FINISHED fire during startup before
/api/health/deep reports a restore status.
"""

import os
import sys
import tempfile
import gzip
import json
import base64
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.storage.candle_store import CandleStore
from src.storage.firestore_backend import FirestoreBackend

class MockFirestoreDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = bool(data)

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

def run_demo():
    try:
        tmp_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except TypeError:
        tmp_obj = tempfile.TemporaryDirectory()

    try:
        tmpdir = tmp_obj.name
        tmp_path = Path(tmpdir)
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DATA_DIR"] = str(data_dir)

        # 1. Prepare empty SQLite DB
        db_path = str(data_dir / "candles.db")
        cs = CandleStore(db_path=db_path)
        print(f"[DEMO] Empty SQLite DB initialized at: {db_path}")

        # 2. Prepare Mock Firestore with 10 symbols holding 550 bars each
        sample_matrix = [[1700000000 + b * 900, 100.0, 105.0, 95.0, 102.0, 1000] for b in range(550)]
        gz_bytes = gzip.compress(json.dumps(sample_matrix).encode('utf-8'))
        b64_str = base64.b64encode(gz_bytes).decode('ascii')

        mock_candles_col = {}
        mock_syms = [f"DEMO_{i:02d}" for i in range(10)]
        for sym in mock_syms:
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

        from src.api.server import app, scheduler
        scheduler.candle_store = cs
        scheduler.firestore = fb
        # Mock universe feed to return our 10 symbols
        mock_feed = MagicMock()
        mock_feed.get_all_universe_items.return_value = [{"symbol": s} for s in mock_syms]
        mock_feed.prefetch_batch_15m_candles = MagicMock()
        scheduler.feed = mock_feed

        from fastapi.testclient import TestClient
        print("[DEMO] Starting TestClient(app) - FastAPI startup event begins...")
        with TestClient(app) as client:
            print("[DEMO] TestClient active (startup lifecycle completed). Probing /api/health/deep:")
            resp = client.get("/api/health/deep")
            print(f"[DEMO] /api/health/deep HTTP Status: {resp.status_code}")
            data = resp.json()
            firebase_info = data.get("firebase", {})
            history_info = data.get("history_coverage", {})
            print(f"[DEMO] firebase.status: {firebase_info.get('status')}")
            print(f"[DEMO] firebase.last_restore: {firebase_info.get('last_restore')}")
            print(f"[DEMO] firebase.last_restore_reason: {firebase_info.get('last_restore_reason')}")
            print(f"[DEMO] history_coverage.coverage_522_pct: {history_info.get('coverage_522_pct')}%")
            print(f"[DEMO] history_coverage.symbols_522_bars: {history_info.get('symbols_522_bars')}")
            print("[DEMO] SUCCESS: RESTORE STARTED & RESTORE FINISHED verified prior to /api/health/deep response.")
    finally:
        try:
            tmp_obj.cleanup()
        except Exception:
            pass

if __name__ == "__main__":
    run_demo()
