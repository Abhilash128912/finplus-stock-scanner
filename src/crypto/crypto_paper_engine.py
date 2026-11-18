"""
FinPlus+ Crypto Delta - Paper Trading & Risk Management Engine
Currency: Indian Rupee (₹ INR)
Exit Strategy: Pure Dynamic Trailing Stop-Loss (No Arbitrary Target Caps)
"""

import os
import sqlite3
import time
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from src.utils.time_and_logger import setup_logger, get_ist_iso, get_utc_iso
from src.crypto.crypto_config import DB_PATH, VIRTUAL_STARTING_EQUITY_INR, DEFAULT_USD_INR_RATE

logger = setup_logger("CryptoPaperEngine")

class CryptoPaperEngine:
    def __init__(self, db_path: str = str(DB_PATH), usd_inr_rate: float = DEFAULT_USD_INR_RATE):
        self.db_path = str(db_path)
        self.usd_inr_rate = float(usd_inr_rate)
        self.virtual_capital_inr = float(VIRTUAL_STARTING_EQUITY_INR)
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.main_paper_tracker = None
        self._init_db()
        self._load_active_positions()

    def _get_connection(self) -> sqlite3.Connection:
        if str(self.db_path) == ":memory:":
            if not hasattr(self, "_mem_conn") or self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initializes tables for positions and closed trades."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crypto_active_positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    contract_value REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    notional_inr REAL NOT NULL,
                    initial_sl REAL NOT NULL,
                    current_sl REAL NOT NULL,
                    unrealized_pnl_usd REAL DEFAULT 0.0,
                    unrealized_pnl_inr REAL DEFAULT 0.0,
                    unrealized_pnl_pct REAL DEFAULT 0.0,
                    highest_favorable_price REAL NOT NULL,
                    breakeven_latched INTEGER DEFAULT 0,
                    entry_time_ist TEXT NOT NULL,
                    entry_time_utc TEXT NOT NULL,
                    notes TEXT,
                    grade TEXT DEFAULT 'B',
                    confluence_score REAL DEFAULT 0.0,
                    setup_key TEXT DEFAULT '15M_COILING_SQUEEZE'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crypto_closed_trades (
                    trade_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    contract_value REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    notional_inr REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    pnl_inr REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    entry_time_ist TEXT NOT NULL,
                    exit_time_ist TEXT NOT NULL,
                    exit_time_utc TEXT NOT NULL,
                    notes TEXT,
                    grade TEXT DEFAULT 'B',
                    confluence_score REAL DEFAULT 0.0,
                    setup_key TEXT DEFAULT '15M_COILING_SQUEEZE'
                )
            """)

            # Lightweight schema migration for existing databases
            cur.execute("PRAGMA table_info(crypto_active_positions)")
            existing_active_cols = {r["name"] for r in cur.fetchall()}
            if "unrealized_pnl_usd" not in existing_active_cols:
                cur.execute("ALTER TABLE crypto_active_positions ADD COLUMN unrealized_pnl_usd REAL DEFAULT 0.0")
            if "unrealized_pnl_inr" not in existing_active_cols:
                cur.execute("ALTER TABLE crypto_active_positions ADD COLUMN unrealized_pnl_inr REAL DEFAULT 0.0")
            if "notional_inr" not in existing_active_cols:
                cur.execute("ALTER TABLE crypto_active_positions ADD COLUMN notional_inr REAL DEFAULT 0.0")
            if "grade" not in existing_active_cols:
                cur.execute("ALTER TABLE crypto_active_positions ADD COLUMN grade TEXT DEFAULT 'B'")
            if "confluence_score" not in existing_active_cols:
                cur.execute("ALTER TABLE crypto_active_positions ADD COLUMN confluence_score REAL DEFAULT 0.0")
            if "setup_key" not in existing_active_cols:
                cur.execute("ALTER TABLE crypto_active_positions ADD COLUMN setup_key TEXT DEFAULT '15M_COILING_SQUEEZE'")

            cur.execute("PRAGMA table_info(crypto_closed_trades)")
            existing_closed_cols = {r["name"] for r in cur.fetchall()}
            if "notional_inr" not in existing_closed_cols:
                cur.execute("ALTER TABLE crypto_closed_trades ADD COLUMN notional_inr REAL DEFAULT 0.0")
            if "pnl_inr" not in existing_closed_cols:
                cur.execute("ALTER TABLE crypto_closed_trades ADD COLUMN pnl_inr REAL DEFAULT 0.0")
            if "grade" not in existing_closed_cols:
                cur.execute("ALTER TABLE crypto_closed_trades ADD COLUMN grade TEXT DEFAULT 'B'")
            if "confluence_score" not in existing_closed_cols:
                cur.execute("ALTER TABLE crypto_closed_trades ADD COLUMN confluence_score REAL DEFAULT 0.0")
            if "setup_key" not in existing_closed_cols:
                cur.execute("ALTER TABLE crypto_closed_trades ADD COLUMN setup_key TEXT DEFAULT '15M_COILING_SQUEEZE'")

            conn.commit()

    def _load_active_positions(self):
        """Restores in-flight positions from SQLite on engine boot."""
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM crypto_active_positions")
                rows = cur.fetchall()
                for r in rows:
                    pos = dict(r)
                    self.active_positions[pos["position_id"]] = pos
            logger.info(f"Loaded {len(self.active_positions)} active crypto paper positions from database.")
        except Exception as e:
            logger.error(f"Error loading active crypto positions: {e}")

    def open_position(
        self,
        symbol: str,
        action: str, # 'BUY_LONG' or 'SELL_SHORT'
        entry_price: float,
        stop_loss: float,
        target_1: Optional[float] = None, # Ignored: purely driven by trailing SL
        target_2: Optional[float] = None,
        contract_value: float = 1.0,
        default_qty: float = 1.0,
        notes: str = "",
        grade: Optional[str] = "B",
        confluence_score: Optional[float] = 0.0,
        setup_key: Optional[str] = "15M_COILING_SQUEEZE"
    ) -> Optional[Dict[str, Any]]:
        """Opens a new simulated perpetual position with dynamic trailing SL."""
        # Strict Permanent Blacklist: BTC trading permanently blocked
        if "BTC" in symbol.upper():
            logger.info(f"Position rejected: {symbol} is permanently blacklisted.")
            return None

        # Strict Maximum 30 Active Trades Ceiling
        MAX_TOTAL_ACTIVE_POSITIONS = int(os.getenv("MAX_ACTIVE_POSITIONS", "30"))
        main_open = 0
        if hasattr(self, "main_paper_tracker") and self.main_paper_tracker:
            main_open = sum(1 for p in self.main_paper_tracker.active_positions.values() if p.get("status") == "OPEN")
        total_open = len(self.active_positions) + main_open
        if total_open >= MAX_TOTAL_ACTIVE_POSITIONS:
            logger.info(f"Crypto Entry Blocked for {symbol}: Active positions limit reached ({total_open}/{MAX_TOTAL_ACTIVE_POSITIONS} Max).")
            return None

        pos_id = f"POS_{symbol}_{action}_{int(time.time())}"
        if pos_id in self.active_positions:
            return None

        # Prevent duplicate entries for the same symbol
        for p in self.active_positions.values():
            if p["symbol"] == symbol:
                logger.info(f"Skipping duplicate position for {symbol}: already active.")
                return None

        notional_usd = entry_price * contract_value * default_qty
        notional_inr = round(notional_usd * self.usd_inr_rate, 2)

        pos_record = {
            "position_id": pos_id,
            "symbol": symbol,
            "action": action,
            "entry_price": float(entry_price),
            "current_price": float(entry_price),
            "quantity": float(default_qty),
            "contract_value": float(contract_value),
            "notional_usd": round(notional_usd, 2),
            "notional_inr": notional_inr,
            "initial_sl": float(stop_loss),
            "current_sl": float(stop_loss),
            "target": "OPEN (TRAIL SL)",
            "unrealized_pnl": 0.0,
            "unrealized_pnl_inr": 0.0,
            "unrealized_pnl_pct": 0.0,
            "highest_favorable_price": float(entry_price),
            "breakeven_latched": 0,
            "entry_time_ist": get_ist_iso(),
            "entry_time_utc": get_utc_iso(),
            "notes": notes,
            "grade": grade or "B",
            "confluence_score": float(confluence_score or 0.0),
            "setup_key": setup_key or "15M_COILING_SQUEEZE"
        }

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO crypto_active_positions (
                    position_id, symbol, action, entry_price, current_price,
                    quantity, contract_value, notional_usd, notional_inr,
                    initial_sl, current_sl, unrealized_pnl_usd, unrealized_pnl_inr,
                    unrealized_pnl_pct, highest_favorable_price, breakeven_latched,
                    entry_time_ist, entry_time_utc, notes, grade, confluence_score, setup_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pos_record["position_id"], pos_record["symbol"], pos_record["action"],
                pos_record["entry_price"], pos_record["current_price"], pos_record["quantity"],
                pos_record["contract_value"], pos_record["notional_usd"], pos_record["notional_inr"],
                pos_record["initial_sl"], pos_record["current_sl"], 0.0, 0.0, 0.0,
                pos_record["highest_favorable_price"], pos_record["breakeven_latched"],
                pos_record["entry_time_ist"], pos_record["entry_time_utc"], pos_record["notes"],
                pos_record["grade"], pos_record["confluence_score"], pos_record["setup_key"]
            ))
            conn.commit()

        self.active_positions[pos_id] = pos_record
        logger.info(f"Opened paper position {pos_id} for {symbol} ({action}) @ ${entry_price:,.2f} (SL: ${stop_loss:,.2f} | Pure Trailing SL)")
        return pos_record

    def update_positions(self, live_prices: Dict[str, float]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Updates unrealized PnL in INR, dynamically trails stop-loss, and closes on SL hit.
        NO ARBITRARY TARGET CAP: Let winners run!
        Returns (closed_trades_this_tick, trailed_positions_this_tick).
        """
        closed_this_tick = []
        trailed_this_tick = []

        for pos_id, pos in list(self.active_positions.items()):
            sym = pos["symbol"]
            ltp = live_prices.get(sym)
            if not ltp or ltp <= 0:
                continue

            pos["current_price"] = ltp
            entry = pos["entry_price"]
            action = pos["action"]
            qty = pos["quantity"]
            cv = pos["contract_value"]

            # Calculate PnL
            if action in ["BUY", "BUY_LONG", "LONG"]:
                pnl_pts = ltp - entry
                pnl_usd = pnl_pts * cv * qty
                pnl_inr = round(pnl_usd * self.usd_inr_rate, 2)
                pnl_pct = round((pnl_pts / entry) * 100.0, 2) if entry > 0 else 0.0

                # 1. Update peak price
                if ltp > pos["highest_favorable_price"]:
                    pos["highest_favorable_price"] = ltp

                peak = pos["highest_favorable_price"]

                # 2. Breakeven Latch: when price gains >= 1.50% from entry (gives trade room to absorb initial retest)
                gain_from_entry_pct = ((peak - entry) / entry) * 100.0
                if gain_from_entry_pct >= 1.50 and pos["breakeven_latched"] == 0:
                    pos["current_sl"] = entry
                    pos["breakeven_latched"] = 1
                    trailed_this_tick.append(pos)
                    logger.info(f"[{sym}] Trailed SL to BREAKEVEN (${entry:,.2f}) as trade gained +{gain_from_entry_pct:.2f}%.")

                # 3. Dynamic High-Water Mark Trailing: once in profit >= 2.0%, trail 2.0% behind the peak
                if gain_from_entry_pct >= 2.0:
                    trail_level = round(peak * 0.980, 4)
                    if trail_level > pos["current_sl"]:
                        pos["current_sl"] = trail_level
                        trailed_this_tick.append(pos)
                        logger.info(f"[{sym}] Dynamic Trailing SL advanced to ${trail_level:,.2f} (2.0% behind peak ${peak:,.2f}).")

                pos["unrealized_pnl"] = round(pnl_usd, 2)
                pos["unrealized_pnl_inr"] = pnl_inr
                pos["unrealized_pnl_pct"] = pnl_pct

                # 4. Exit Condition: Strictly when price breaches current trailing Stop-Loss
                if ltp <= pos["current_sl"]:
                    reason = "TRAIL_SL_HIT" if pos["breakeven_latched"] else "INITIAL_SL_HIT"
                    closed = self.close_position(pos_id, exit_price=ltp, exit_reason=reason)
                    if closed:
                        closed_this_tick.append(closed)

            elif action in ["SELL", "SELL_SHORT", "SHORT"]:
                pnl_pts = entry - ltp
                pnl_usd = pnl_pts * cv * qty
                pnl_inr = round(pnl_usd * self.usd_inr_rate, 2)
                pnl_pct = round((pnl_pts / entry) * 100.0, 2) if entry > 0 else 0.0

                if ltp < pos["highest_favorable_price"]:
                    pos["highest_favorable_price"] = ltp

                trough = pos["highest_favorable_price"]

                # 2. Breakeven Latch for Shorts: when price gains >= 1.50% from entry
                gain_from_entry_pct = ((entry - trough) / entry) * 100.0
                if gain_from_entry_pct >= 1.50 and pos["breakeven_latched"] == 0:
                    pos["current_sl"] = entry
                    pos["breakeven_latched"] = 1
                    trailed_this_tick.append(pos)
                    logger.info(f"[{sym}] Trailed Short SL to BREAKEVEN (${entry:,.2f}) as trade gained +{gain_from_entry_pct:.2f}%.")

                # 3. Dynamic High-Water Mark Trailing for Shorts: once in profit >= 2.0%, trail 2.0% behind trough
                if gain_from_entry_pct >= 2.0:
                    trail_level = round(trough * 1.020, 4)
                    if trail_level < pos["current_sl"]:
                        pos["current_sl"] = trail_level
                        trailed_this_tick.append(pos)
                        logger.info(f"[{sym}] Dynamic Trailing SL trailed down to ${trail_level:,.2f} (2.0% behind trough ${trough:,.2f}).")

                pos["unrealized_pnl"] = round(pnl_usd, 2)
                pos["unrealized_pnl_inr"] = pnl_inr
                pos["unrealized_pnl_pct"] = pnl_pct

                if ltp >= pos["current_sl"]:
                    reason = "TRAIL_SL_HIT" if pos["breakeven_latched"] else "INITIAL_SL_HIT"
                    closed = self.close_position(pos_id, exit_price=ltp, exit_reason=reason)
                    if closed:
                        closed_this_tick.append(closed)

            # Persist update
            if pos_id in self.active_positions:
                self._sync_position_to_db(pos)

        return closed_this_tick, trailed_this_tick

    def _sync_position_to_db(self, pos: Dict[str, Any]):
        """Persists updated SL and unrealized PnL to SQLite."""
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE crypto_active_positions SET
                        current_price = ?,
                        current_sl = ?,
                        unrealized_pnl_usd = ?,
                        unrealized_pnl_inr = ?,
                        unrealized_pnl_pct = ?,
                        highest_favorable_price = ?,
                        breakeven_latched = ?
                    WHERE position_id = ?
                """, (
                    pos["current_price"], pos["current_sl"], pos.get("unrealized_pnl", 0.0),
                    pos.get("unrealized_pnl_inr", 0.0), pos.get("unrealized_pnl_pct", 0.0),
                    pos["highest_favorable_price"], pos["breakeven_latched"], pos["position_id"]
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Error syncing position {pos['position_id']} to DB: {e}")

    def close_position(self, pos_id: str, exit_price: float, exit_reason: str = "MANUAL_SQUARE_OFF") -> Optional[Dict[str, Any]]:
        """Closes an active position and registers the completed trade."""
        pos = self.active_positions.pop(pos_id, None)
        if not pos:
            return None

        entry = pos["entry_price"]
        action = pos["action"]
        qty = pos["quantity"]
        cv = pos["contract_value"]

        if action in ["BUY", "BUY_LONG", "LONG"]:
            pnl_pts = exit_price - entry
        else:
            pnl_pts = entry - exit_price

        pnl_usd = round(pnl_pts * cv * qty, 2)
        pnl_inr = round(pnl_usd * self.usd_inr_rate, 2)
        pnl_pct = round((pnl_pts / entry) * 100.0, 2) if entry > 0 else 0.0

        trade_id = f"TRD_{pos['symbol']}_{int(time.time())}"
        closed_record = {
            "trade_id": trade_id,
            "position_id": pos_id,
            "symbol": pos["symbol"],
            "action": action,
            "entry_price": entry,
            "exit_price": float(exit_price),
            "quantity": qty,
            "contract_value": cv,
            "notional_usd": pos["notional_usd"],
            "notional_inr": pos.get("notional_inr", round(pos["notional_usd"] * self.usd_inr_rate, 2)),
            "pnl_usd": pnl_usd,
            "pnl_inr": pnl_inr,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "entry_time_ist": pos["entry_time_ist"],
            "exit_time_ist": get_ist_iso(),
            "exit_time_utc": get_utc_iso(),
            "notes": pos.get("notes", ""),
            "grade": pos.get("grade", "B"),
            "confluence_score": pos.get("confluence_score", 0.0),
            "setup_key": pos.get("setup_key", "15M_COILING_SQUEEZE")
        }

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM crypto_active_positions WHERE position_id = ?", (pos_id,))
                cur.execute("""
                    INSERT INTO crypto_closed_trades (
                        trade_id, position_id, symbol, action, entry_price,
                        exit_price, quantity, contract_value, notional_usd, notional_inr,
                        pnl_usd, pnl_inr, pnl_pct, exit_reason, entry_time_ist,
                        exit_time_ist, exit_time_utc, notes, grade, confluence_score, setup_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    closed_record["trade_id"], closed_record["position_id"], closed_record["symbol"],
                    closed_record["action"], closed_record["entry_price"], closed_record["exit_price"],
                    closed_record["quantity"], closed_record["contract_value"], closed_record["notional_usd"],
                    closed_record["notional_inr"], closed_record["pnl_usd"], closed_record["pnl_inr"],
                    closed_record["pnl_pct"], closed_record["exit_reason"], closed_record["entry_time_ist"],
                    closed_record["exit_time_ist"], closed_record["exit_time_utc"], closed_record["notes"],
                    closed_record["grade"], closed_record["confluence_score"], closed_record["setup_key"]
                ))
                conn.commit()
            logger.info(f"Closed {pos['symbol']} paper position {pos_id} @ ${exit_price:,.2f} ({exit_reason}). PnL: Rs {pnl_inr:+,.2f} (${pnl_usd:+,.2f}) [{pnl_pct:+.2f}%]")
        except Exception as e:
            logger.error(f"Error persisting closed trade {trade_id}: {e}")

        return closed_record

    def get_status_dict(self) -> Dict[str, Any]:
        """Returns comprehensive real-time status of paper portfolio in INR."""
        total_unrealized_inr = sum(p.get("unrealized_pnl_inr", 0.0) for p in self.active_positions.values())
        total_unrealized_usd = sum(p.get("unrealized_pnl", 0.0) for p in self.active_positions.values())

        total_realized_inr = 0.0
        total_realized_usd = 0.0
        total_trades = 0
        winning_trades = 0
        recent_closed = []

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM crypto_closed_trades ORDER BY exit_time_utc DESC LIMIT 20")
                rows = cur.fetchall()
                for r in rows:
                    t = dict(r)
                    recent_closed.append(t)

                cur.execute("SELECT SUM(pnl_inr), SUM(pnl_usd), COUNT(*), SUM(CASE WHEN pnl_inr > 0 THEN 1 ELSE 0 END) FROM crypto_closed_trades")
                summary_row = cur.fetchone()
                if summary_row and summary_row[0] is not None:
                    total_realized_inr = float(summary_row[0])
                    total_realized_usd = float(summary_row[1])
                    total_trades = int(summary_row[2])
                    winning_trades = int(summary_row[3])
        except Exception as e:
            logger.error(f"Error fetching closed trades summary: {e}")

        win_rate = round((winning_trades / total_trades * 100.0), 1) if total_trades > 0 else 0.0
        current_equity_inr = round(self.virtual_capital_inr + total_realized_inr + total_unrealized_inr, 2)
        equity_pnl_pct = round(((current_equity_inr - self.virtual_capital_inr) / self.virtual_capital_inr * 100.0), 2)

        # Standardize position presentation
        positions_list = []
        for p in self.active_positions.values():
            pos_copy = dict(p)
            pos_copy["target"] = "OPEN (TRAIL SL)"
            pos_copy["unrealized_pnl_inr"] = round(pos_copy.get("unrealized_pnl_inr", 0.0), 2)
            pos_copy["notional_inr"] = round(pos_copy.get("notional_inr", pos_copy.get("notional_usd", 0.0) * self.usd_inr_rate), 2)
            positions_list.append(pos_copy)

        return {
            "status": "ONLINE_PAPER_ACTIVE",
            "currency": "INR",
            "currency_symbol": "₹",
            "usd_inr_rate": self.usd_inr_rate,
            "virtual_starting_capital_inr": self.virtual_capital_inr,
            "current_equity_inr": current_equity_inr,
            "equity_pnl_pct": equity_pnl_pct,
            "total_unrealized_pnl_inr": round(total_unrealized_inr, 2),
            "net_realized_pnl_inr": round(total_realized_inr, 2),
            "total_unrealized_pnl_usd": round(total_unrealized_usd, 2),
            "net_realized_pnl_usd": round(total_realized_usd, 2),
            "active_positions_count": len(self.active_positions),
            "total_trades_count": total_trades,
            "win_rate_pct": win_rate,
            "active_positions": positions_list,
            "recent_closed_trades": recent_closed,
            "ist_time": get_ist_iso(),
            "timestamp_utc": get_utc_iso()
        }
