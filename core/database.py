"""
database.py — SQLite persistence layer for cloud paper-trader.

WAL mode is enabled for improved concurrent read performance.
All public methods are thread-safe via the check_same_thread=False
connection setting; the caller is responsible for not issuing concurrent
*writes* from multiple threads (single-writer, multi-reader SQLite rule).
"""

import json
import logging
import os
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


class Database:
    """SQLite database layer with WAL journal mode.

    Tables
    ------
    trades       – one row per closed trade
    performance  – one summary row per (strategy, date); REPLACE semantics
    suggestions  – auto-generated optimisation hints; reviewed by human
    regimes      – daily market-regime snapshot; REPLACE semantics on date
    """

    def __init__(self, db_path: str = "trading.db") -> None:
        self.db_path = db_path
        self.logger = logging.getLogger("database")

        # Ensure parent directory exists.
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.commit()

        self._create_tables()
        self.logger.info("Database opened: %s (WAL mode)", db_path)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        """Create all tables if they do not already exist."""
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                strategy        TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                entry_price     REAL    NOT NULL,
                exit_price      REAL    NOT NULL,
                pnl             REAL    NOT NULL,
                exit_reason     TEXT    NOT NULL,
                vix             REAL,
                spy_price       REAL,
                market_regime   TEXT,
                conditions_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS performance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy        TEXT    NOT NULL,
                date            TEXT    NOT NULL,
                total_trades    INTEGER NOT NULL DEFAULT 0,
                wins            INTEGER NOT NULL DEFAULT 0,
                losses          INTEGER NOT NULL DEFAULT 0,
                wr              REAL    NOT NULL DEFAULT 0.0,
                total_pnl       REAL    NOT NULL DEFAULT 0.0,
                vs_backtest_wr  REAL    NOT NULL DEFAULT 0.0,
                status          TEXT    NOT NULL DEFAULT 'INSUFFICIENT_DATA',
                UNIQUE(strategy, date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy        TEXT    NOT NULL,
                suggestion_text TEXT    NOT NULL,
                created_date    TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS regimes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    NOT NULL UNIQUE,
                vix          REAL,
                spy_trend    TEXT,
                market_phase TEXT
            )
            """,
        ]
        with self.conn:
            for stmt in ddl:
                self.conn.execute(stmt)
        self.logger.debug("Tables verified / created.")

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def insert_trade(
        self,
        strategy: str,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        exit_reason: str,
        vix: float,
        spy_price: float,
        market_regime: str,
        conditions_json: dict,
    ) -> int:
        """Insert a closed trade and return its row id."""
        timestamp = datetime.now().isoformat()
        conditions_str = json.dumps(conditions_json) if conditions_json else "{}"
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO trades
                    (timestamp, strategy, symbol, direction, entry_price,
                     exit_price, pnl, exit_reason, vix, spy_price,
                     market_regime, conditions_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, strategy, symbol, direction, entry_price,
                    exit_price, pnl, exit_reason, vix, spy_price,
                    market_regime, conditions_str,
                ),
            )
        self.logger.debug(
            "Trade inserted id=%d strategy=%s pnl=%.2f", cur.lastrowid, strategy, pnl
        )
        return cur.lastrowid

    def insert_performance(
        self,
        strategy: str,
        date_str: str,
        total_trades: int,
        wins: int,
        losses: int,
        wr: float,
        total_pnl: float,
        vs_backtest_wr: float,
        status: str,
    ) -> None:
        """Upsert a performance summary row for (strategy, date)."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO performance
                    (strategy, date, total_trades, wins, losses, wr,
                     total_pnl, vs_backtest_wr, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy, date_str, total_trades, wins, losses,
                    wr, total_pnl, vs_backtest_wr, status,
                ),
            )

    def insert_suggestion(self, strategy: str, suggestion_text: str) -> None:
        """Persist an auto-generated optimisation suggestion (status=pending)."""
        created_date = date.today().isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO suggestions (strategy, suggestion_text, created_date, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (strategy, suggestion_text, created_date),
            )

    def insert_regime(
        self, date_str: str, vix: float, spy_trend: str, market_phase: str
    ) -> None:
        """Upsert a market-regime snapshot for a given date."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO regimes (date, vix, spy_trend, market_phase)
                VALUES (?, ?, ?, ?)
                """,
                (date_str, vix, spy_trend, market_phase),
            )

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def get_trades_by_strategy(
        self, strategy: str, since_date: str = None
    ) -> list:
        """Return all trades for *strategy*, newest first.

        Parameters
        ----------
        since_date : str, optional
            ISO date string (YYYY-MM-DD).  Only trades on or after this date
            are returned.
        """
        if since_date:
            cur = self.conn.execute(
                """
                SELECT * FROM trades
                WHERE strategy = ?
                  AND date(timestamp) >= ?
                ORDER BY timestamp DESC
                """,
                (strategy, since_date),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT * FROM trades
                WHERE strategy = ?
                ORDER BY timestamp DESC
                """,
                (strategy,),
            )
        return [dict(row) for row in cur.fetchall()]

    def get_strategy_stats(self, strategy: str) -> dict:
        """Compute aggregate stats for *strategy* directly from the trades table.

        Returns
        -------
        dict with keys: n, wins, losses, wr, total_pnl, expectancy
        """
        cur = self.conn.execute(
            "SELECT pnl FROM trades WHERE strategy = ?", (strategy,)
        )
        rows = cur.fetchall()
        if not rows:
            return {
                "n": 0, "wins": 0, "losses": 0,
                "wr": 0.0, "total_pnl": 0.0, "expectancy": 0.0,
            }

        pnls = [row["pnl"] for row in rows]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = n - wins
        wr = wins / n if n > 0 else 0.0
        total_pnl = sum(pnls)
        expectancy = total_pnl / n if n > 0 else 0.0

        return {
            "n": n,
            "wins": wins,
            "losses": losses,
            "wr": round(wr, 4),
            "total_pnl": round(total_pnl, 2),
            "expectancy": round(expectancy, 2),
        }

    def get_pending_suggestions(self) -> list:
        """Return all suggestions with status='pending', oldest first."""
        cur = self.conn.execute(
            "SELECT * FROM suggestions WHERE status = 'pending' ORDER BY created_date ASC"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_performance_history(self, days: int = 30) -> list:
        """Return performance rows for the last *days* calendar days."""
        cutoff = (
            datetime.now().date()
            .__class__.fromordinal(
                datetime.now().date().toordinal() - days
            )
        ).isoformat()
        cur = self.conn.execute(
            """
            SELECT * FROM performance
            WHERE date >= ?
            ORDER BY date DESC
            """,
            (cutoff,),
        )
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def daily_backup(self) -> str:
        """Copy the live database file to reports/trading_YYYY-MM-DD.db.

        Returns
        -------
        str  Path of the backup file.
        """
        reports_dir = Path("reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        today_str = date.today().isoformat()
        dest = str(reports_dir / f"trading_{today_str}.db")

        # Flush WAL checkpoint so the backup is consistent.
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        shutil.copy2(self.db_path, dest)
        self.logger.info("Daily backup written to %s", dest)
        return dest

    def close(self) -> None:
        """Flush WAL and close the connection."""
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            self.conn.close()
            self.logger.info("Database connection closed.")
        except Exception as exc:  # pragma: no cover
            self.logger.error("Error closing database: %s", exc)
