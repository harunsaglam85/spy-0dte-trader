#!/usr/bin/env python3
"""
Hermes SQLite trade database (Batch 2 — Task 5).

A thin, append-alongside layer for trade records. The engine keeps writing
trades/YYYY-MM-DD.json exactly as before (source of truth); this module ADDS a
queryable SQLite mirror at /root/hermes_system/trades.db with one row per trade:
inserted on entry, updated on exit. All writes are best-effort and swallow
errors so a DB problem can never break live trading.

Schema (trades table):
  id, date, strategy, mode, account, status, entry_time, exit_time,
  credit, pnl, vix_entry, contango_entry, spy_price, order_id, exit_reason

Usage from the engine:
  from hermes_system import db_manager  (or `import db_manager` when on sys.path)
  db_manager.record_entry(strategy=..., mode=..., account=..., entry_time=...,
                          credit=..., vix_entry=..., contango_entry=...,
                          spy_price=..., order_id=...)
  db_manager.record_exit(order_id=..., exit_time=..., pnl=..., exit_reason=...)

Query helper:
  db_manager.get_trades(days=30) -> pandas.DataFrame
"""
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

DB_PATH = Path('/root/hermes_system/trades.db')
log = logging.getLogger('hermes.db')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT,
    strategy       TEXT,
    mode           TEXT,
    account        TEXT,
    status         TEXT,
    entry_time     TEXT,
    exit_time      TEXT,
    credit         REAL,
    pnl            REAL,
    vix_entry      REAL,
    contango_entry REAL,
    spy_price      REAL,
    order_id       TEXT,
    exit_reason    TEXT
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the trades table if it does not exist. Safe to call repeatedly."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(_SCHEMA)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);')


def record_entry(strategy: str, mode: str, account: Optional[str], entry_time: str,
                 credit: Optional[float], vix_entry: Optional[float],
                 contango_entry: Optional[float], spy_price: Optional[float],
                 order_id: Optional[str]) -> None:
    """Insert an open trade row. Best-effort: logs and returns on any error."""
    try:
        init_db()
        with _connect() as conn:
            conn.execute(
                """INSERT INTO trades
                   (date, strategy, mode, account, status, entry_time, exit_time,
                    credit, pnl, vix_entry, contango_entry, spy_price, order_id, exit_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (date.today().isoformat(), strategy, mode, account, 'open', entry_time, None,
                 credit, None, vix_entry, contango_entry, spy_price, order_id, None),
            )
    except Exception as exc:
        log.warning('db record_entry failed (%s): %s', strategy, exc)


def record_exit(order_id: Optional[str], exit_time: str, pnl: Optional[float],
                exit_reason: Optional[str]) -> None:
    """Update the matching open row (by order_id) to closed. If no open row
    matches (e.g. DB created after the entry), insert a standalone closed row so
    P&L is still captured. Best-effort."""
    try:
        init_db()
        with _connect() as conn:
            cur = conn.execute(
                """UPDATE trades SET status='closed', exit_time=?, pnl=?, exit_reason=?
                   WHERE order_id=? AND status='open'""",
                (exit_time, pnl, exit_reason, order_id),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """INSERT INTO trades
                       (date, strategy, mode, account, status, entry_time, exit_time,
                        credit, pnl, vix_entry, contango_entry, spy_price, order_id, exit_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (date.today().isoformat(), None, None, None, 'closed', None, exit_time,
                     None, pnl, None, None, None, order_id, exit_reason),
                )
    except Exception as exc:
        log.warning('db record_exit failed (order_id=%s): %s', order_id, exc)


def get_trades(days: int = 30) -> pd.DataFrame:
    """Return trades from the last *days* days as a DataFrame (newest first)."""
    try:
        init_db()
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with _connect() as conn:
            return pd.read_sql_query(
                'SELECT * FROM trades WHERE date >= ? ORDER BY entry_time DESC, id DESC',
                conn, params=(cutoff,),
            )
    except Exception as exc:
        log.warning('db get_trades failed: %s', exc)
        return pd.DataFrame()


if __name__ == '__main__':
    init_db()
    print(f'Initialized {DB_PATH}')
    print(get_trades(days=30))
