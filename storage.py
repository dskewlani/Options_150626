"""
storage.py — SQLite persistence for trades, P&L history, and settings.
"""

import sqlite3
import json
import os
from datetime import datetime, date
from typing import List, Optional, Dict, Any

DB_PATH = os.getenv("DB_PATH", "trading.db")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    """Create tables if they don't exist."""
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            expiry      TEXT NOT NULL,
            strike      REAL NOT NULL,
            option_type TEXT NOT NULL,
            action      TEXT NOT NULL,   -- BUY / SELL
            qty         INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            exit_price  REAL,
            status      TEXT DEFAULT 'OPEN',   -- OPEN / CLOSED
            pnl         REAL,
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS pnl_history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            session_pnl REAL NOT NULL,
            cumulative_pnl REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tick_snapshots (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            symbol  TEXT NOT NULL,
            ltp     REAL NOT NULL,
            vix     REAL,
            pcr     REAL
        );
        """)


# ── Trades ─────────────────────────────────────────────────────────────────────

def log_trade(symbol: str, expiry: str, strike: float, option_type: str,
              action: str, qty: int, entry_price: float,
              notes: str = "") -> int:
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO trades
              (ts, symbol, expiry, strike, option_type, action, qty, entry_price, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), symbol, expiry, strike, option_type,
              action, qty, entry_price, notes))
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float) -> float:
    with _conn() as con:
        row = con.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return 0.0
        pnl = (exit_price - row["entry_price"]) * row["qty"] * 50  # lot size 50
        if row["action"] == "SELL":
            pnl = -pnl
        con.execute("""
            UPDATE trades SET exit_price=?, status='CLOSED', pnl=?
            WHERE id=?
        """, (exit_price, pnl, trade_id))
        return pnl


def get_open_trades() -> List[Dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY ts DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_today_trades() -> List[Dict]:
    today = date.today().isoformat()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE ts LIKE ? ORDER BY ts DESC",
            (f"{today}%",)
        ).fetchall()
        return [dict(r) for r in rows]


def get_today_pnl() -> float:
    trades = get_today_trades()
    return sum(t["pnl"] or 0 for t in trades if t["status"] == "CLOSED")


def get_session_win_rate() -> float:
    trades = get_today_trades()
    closed = [t for t in trades if t["status"] == "CLOSED"]
    if not closed:
        return 0.0
    wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
    return round(wins / len(closed) * 100, 1)


# ── P&L History ────────────────────────────────────────────────────────────────

def record_pnl_snapshot(session_pnl: float, cumulative_pnl: float):
    with _conn() as con:
        con.execute("""
            INSERT INTO pnl_history (ts, session_pnl, cumulative_pnl)
            VALUES (?, ?, ?)
        """, (datetime.now().isoformat(), session_pnl, cumulative_pnl))


def get_pnl_history(limit: int = 60) -> List[Dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM pnl_history ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]


# ── Settings ───────────────────────────────────────────────────────────────────

def get_setting(key: str, default: Any = None) -> Any:
    with _conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]


def set_setting(key: str, value: Any):
    with _conn() as con:
        con.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, json.dumps(value)))


# ── Tick snapshots ─────────────────────────────────────────────────────────────

def save_tick_snapshot(symbol: str, ltp: float,
                        vix: float = 0.0, pcr: float = 0.0):
    with _conn() as con:
        con.execute("""
            INSERT INTO tick_snapshots (ts, symbol, ltp, vix, pcr)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), symbol, ltp, vix, pcr))


def get_price_history(symbol: str, limit: int = 100) -> List[float]:
    with _conn() as con:
        rows = con.execute("""
            SELECT ltp FROM tick_snapshots
            WHERE symbol=? ORDER BY id DESC LIMIT ?
        """, (symbol, limit)).fetchall()
        return [r["ltp"] for r in reversed(rows)]
