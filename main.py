"""
CloudTrader — Master Orchestrator
Polls every 5 minutes during market hours; manages all strategies,
kill switches, daily resets, and end-of-day reporting.
"""

import os
import sys
import time
import logging
import signal
import shutil
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple, Union

import pytz
import schedule
from dotenv import load_dotenv

from core.database import Database
from core.data_feeds import DataFeeds
from core.performance import PerformanceTracker
from core.reporter import Reporter
from core.sentiment import fetch_sentiment
from strategies.config_d import ConfigD
from strategies.credit_spread import CreditSpread
from strategies.five_dte import FiveDTE
from strategies.earnings import Earnings
from strategies.vpin import VPIN
from core.telegram_alerts import (
    trade_entered as tg_trade_entered,
    trade_exited as tg_trade_exited,
    strategy_paused as tg_strategy_paused,
    daily_report as tg_daily_report,
    bot_restarted as tg_bot_restarted,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NY_TZ = pytz.timezone("America/New_York")
KILL_FILE = Path("/tmp/kill_all")
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)
PRE_MARKET = (8, 0)
POST_MARKET = (16, 30)
POLL_INTERVAL_SECONDS = 300   # 5 minutes — respects Tradier rate limits
DAILY_STRATEGY_LOSS_LIMIT = 500.0
DAILY_TOTAL_LOSS_LIMIT = 2_000.0

LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Configure rotating file + stdout logging, plus a dedicated errors file."""
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Main rotating log
    main_handler = RotatingFileHandler(
        LOG_DIR / "main.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=7,
        encoding="utf-8",
    )
    main_handler.setFormatter(fmt)
    main_handler.setLevel(logging.INFO)

    # Error-only rotating log
    error_handler = RotatingFileHandler(
        LOG_DIR / "errors.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    error_handler.setFormatter(fmt)
    error_handler.setLevel(logging.ERROR)

    # Console
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logging.INFO)

    root.addHandler(main_handler)
    root.addHandler(error_handler)
    root.addHandler(stream_handler)


# ---------------------------------------------------------------------------
# Market timing helpers
# ---------------------------------------------------------------------------

def is_trading_day() -> bool:
    """Return True on weekdays (Monday=0 … Friday=4)."""
    return datetime.now(NY_TZ).weekday() < 5


def get_market_phase(now: datetime) -> str:
    """
    Classify the current ET time into one of six phases:
      premarket  : 08:00 – 09:30
      morning    : 09:30 – 11:00
      midday     : 11:00 – 14:00
      afternoon  : 14:00 – 16:00
      postmarket : 16:00 – 16:30
      closed     : all other times
    """
    t = (now.hour, now.minute)

    def ge(a, b):
        return (a[0], a[1]) >= (b[0], b[1])

    def lt(a, b):
        return (a[0], a[1]) < (b[0], b[1])

    if ge(t, PRE_MARKET) and lt(t, MARKET_OPEN):
        return "premarket"
    if ge(t, MARKET_OPEN) and lt(t, (11, 0)):
        return "morning"
    if ge(t, (11, 0)) and lt(t, (14, 0)):
        return "midday"
    if ge(t, (14, 0)) and lt(t, MARKET_CLOSE):
        return "afternoon"
    if ge(t, MARKET_CLOSE) and lt(t, POST_MARKET):
        return "postmarket"
    return "closed"


def _seconds_until_next_trading_day_premarket() -> float:
    """
    Return the number of seconds until 08:00 ET on the next weekday.
    Advances over consecutive weekend days.
    """
    now = datetime.now(NY_TZ)
    candidate = now.replace(hour=PRE_MARKET[0], minute=PRE_MARKET[1], second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    # Advance past weekends
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(0.0, (candidate - now).total_seconds())


# ---------------------------------------------------------------------------
# Kill-switch checker
# ---------------------------------------------------------------------------

def check_kill_switches(strategies: dict, total_daily_pnl_tracker: dict) -> dict:
    """
    Evaluate all kill conditions.  Returns {strategy_name: reason_str_or_None}.
    - KILL_FILE presence pauses every strategy.
    - Per-strategy daily loss > DAILY_STRATEGY_LOSS_LIMIT pauses that strategy.
    - Aggregate daily loss > DAILY_TOTAL_LOSS_LIMIT pauses every strategy.
    """
    logger = logging.getLogger("kill_switch")
    paused = {name: None for name in strategies}

    # 1. Hard kill file
    if KILL_FILE.exists():
        reason = "KILL_FILE present at /tmp/kill_all"
        logger.critical("KILL FILE DETECTED — pausing ALL strategies. Reason: %s", reason)
        for name in paused:
            paused[name] = reason
        return paused

    # 2. Per-strategy loss
    for name, strat in strategies.items():
        try:
            if hasattr(strat, "daily_loss_exceeded") and strat.daily_loss_exceeded():
                reason = (
                    f"Daily loss limit hit: "
                    f"${abs(total_daily_pnl_tracker.get(name, 0.0)):.2f} >= "
                    f"${DAILY_STRATEGY_LOSS_LIMIT:.2f}"
                )
                logger.warning("Strategy %s PAUSED — %s", name, reason)
                paused[name] = reason
        except Exception as exc:
            logger.error("Error checking loss limit for %s: %s", name, exc)

    # 3. Aggregate daily loss
    total_loss = sum(
        v for v in total_daily_pnl_tracker.values() if v < 0
    )
    if abs(total_loss) > DAILY_TOTAL_LOSS_LIMIT:
        reason = (
            f"Total daily loss ${abs(total_loss):.2f} exceeds limit "
            f"${DAILY_TOTAL_LOSS_LIMIT:.2f} — pausing ALL strategies"
        )
        logger.critical(reason)
        for name in paused:
            if paused[name] is None:
                paused[name] = reason

    return paused


# ---------------------------------------------------------------------------
# Main orchestrator class
# ---------------------------------------------------------------------------

class CloudTrader:
    """
    Master controller.  Instantiates all strategies, runs the polling loop,
    enforces kill switches, and drives daily lifecycle events.
    """

    def __init__(self) -> None:
        load_dotenv()
        self.logger = logging.getLogger("main")

        db_path = os.getenv("DB_PATH", "trading.db")
        self.db = Database(db_path=db_path)
        self.feeds = DataFeeds()

        self.strategies: dict = {
            "config_d":      ConfigD(self.db, self.feeds),
            "credit_spread": CreditSpread(self.db, self.feeds),
            "five_dte":      FiveDTE(self.db, self.feeds),
            "earnings":      Earnings(self.db, self.feeds),
            "vpin":          VPIN(self.db, self.feeds),
        }

        self.perf = PerformanceTracker(self.db)
        self.reporter = Reporter(self.db, self.strategies)

        self._running: bool = True
        self._paused_strategies: set = set()
        self._last_report_date: date = None
        self._daily_reset_date: date = None
        self._pre_market_done_date: date = None
        self._spy_price_yesterday: float = 0.0
        self._sentiment_score: float = 0.0
        self._daily_pnl: dict = {name: 0.0 for name in self.strategies}

        # Graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self.logger.info(
            "CloudTrader initialised — strategies: %s",
            ", ".join(self.strategies.keys()),
        )

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _handle_signal(self, signum, frame) -> None:  # noqa: ANN001
        sig_name = signal.Signals(signum).name
        self.logger.warning("Received signal %s — initiating graceful shutdown", sig_name)
        self._running = False

    # ------------------------------------------------------------------
    # Market state snapshot
    # ------------------------------------------------------------------

    def build_market_state(self) -> dict:
        """Collect a point-in-time snapshot of market conditions."""
        now = datetime.now(NY_TZ)
        spy_price = self.feeds.get_spy_price()
        vix = self.feeds.get_vix()
        phase = get_market_phase(now)

        spy_pct_change = (
            (spy_price - self._spy_price_yesterday) / self._spy_price_yesterday * 100
            if self._spy_price_yesterday > 0
            else 0.0
        )

        market_state = {
            "timestamp": now,
            "phase": phase,
            "is_market_open": phase in ("morning", "midday", "afternoon"),
            "spy_price": spy_price,
            "spy_pct_change": spy_pct_change,
            "vix": vix,
            "sentiment_score": self._sentiment_score,
            "paused_strategies": set(self._paused_strategies),
            "daily_pnl": dict(self._daily_pnl),
        }
        return market_state

    # ------------------------------------------------------------------
    # Daily lifecycle
    # ------------------------------------------------------------------

    def pre_market_tasks(self) -> None:
        """One-shot tasks executed during pre-market (08:00–09:30 ET)."""
        self.logger.info("=== Pre-market tasks starting ===")

        # Capture yesterday's closing price for daily % change calculation
        try:
            hist = self.feeds.get_spy_daily(lookback=2)
            if hist is not None and len(hist) >= 2:
                self._spy_price_yesterday = float(hist[-2])
                self.logger.info(
                    "SPY previous close: $%.2f", self._spy_price_yesterday
                )
        except Exception as exc:
            self.logger.error("Could not fetch SPY daily history: %s", exc)

        # Log current market conditions
        try:
            vix = self.feeds.get_vix()
            spy_now = self.feeds.get_spy_price()
            self.logger.info("VIX: %.2f  |  SPY current: $%.2f", vix, spy_now)
        except Exception as exc:
            self.logger.error("Could not fetch VIX/SPY during pre-market: %s", exc)

        # Notify strategies about today's earnings
        try:
            for name, strat in self.strategies.items():
                if hasattr(strat, "check_earnings_calendar"):
                    strat.check_earnings_calendar()
                    self.logger.debug("Earnings calendar checked for %s", name)
        except Exception as exc:
            self.logger.error("Earnings calendar check failed: %s", exc)

        today = datetime.now(NY_TZ).date()

        # Fetch morning sentiment score
        try:
            self._sentiment_score = fetch_sentiment()
            self.logger.info("Morning sentiment score: %.3f", self._sentiment_score)
        except Exception as exc:
            self.logger.error("Sentiment fetch failed: %s", exc)
            self._sentiment_score = 0.0

        self._pre_market_done_date = today
        self.logger.info("=== Pre-market tasks complete ===")

    def _reset_daily_state(self) -> None:
        """Called at the start of each new trading day."""
        today = datetime.now(NY_TZ).date()
        self.logger.info("Resetting daily state for %s", today)
        for name, strat in self.strategies.items():
            try:
                if hasattr(strat, "reset_daily"):
                    strat.reset_daily()
            except Exception as exc:
                self.logger.error("reset_daily failed for %s: %s", name, exc)

        self._daily_pnl = {name: 0.0 for name in self.strategies}
        self._paused_strategies.clear()
        self._daily_reset_date = today
        self.logger.info("Daily state reset complete")

    # ------------------------------------------------------------------
    # Strategy cycle
    # ------------------------------------------------------------------

    def run_strategy_checks(self, market_state: dict) -> None:
        """
        Core polling loop body.  For each active strategy:
          1. update_positions() — manages existing trades, captures closed ones
          2. check_signals()    — looks for new entry opportunities
        After all strategies, re-evaluate kill switches.
        """
        self.logger.info(
            "run_strategy_checks: %d strategies | phase=%s | is_market_open=%s | vix=%.2f | spy=%.2f",
            len(self.strategies),
            market_state.get("phase", "?"),
            market_state.get("is_market_open", False),
            market_state.get("vix", 0.0),
            market_state.get("spy_price", 0.0),
        )

        for name, strat in self.strategies.items():
            if name in self._paused_strategies:
                self.logger.info("Strategy %s is paused — skipping", name)
                continue

            # --- Position management ------------------------------------------
            try:
                closed_trades = strat.update_positions(market_state)
                if closed_trades:
                    for trade in closed_trades:
                        pnl = trade.get("pnl", 0.0)
                        self._daily_pnl[name] = self._daily_pnl.get(name, 0.0) + pnl
                        self.perf.on_trade_closed(trade, name)
                        self.db.record_trade(trade)
                        self.logger.info(
                            "CLOSED [%s] %s  P&L: $%.2f  cum_daily: $%.2f",
                            name,
                            trade.get("description", ""),
                            pnl,
                            self._daily_pnl[name],
                        )
                        tg_trade_exited(
                            strategy=name,
                            pnl=pnl,
                            reason=trade.get("exit_reason", "unknown"),
                        )
            except Exception as exc:
                self.logger.error(
                    "update_positions error in strategy %s: %s", name, exc, exc_info=True
                )

            # --- Signal check -------------------------------------------------
            self.logger.info("Checking signals for strategy: %s", name)
            try:
                signal_result = strat.check_signals(market_state)
                if signal_result is not None:
                    self.logger.info(
                        "SIGNAL [%s]: %s", name, signal_result
                    )
                    strat._open_positions.append(signal_result)
                    tg_trade_entered(
                        strategy=name,
                        direction=signal_result.get("direction", ""),
                        price=signal_result.get("entry_price", 0.0),
                        vix=market_state.get("vix", 0.0),
                    )
                else:
                    self.logger.info("No signal from strategy: %s", name)
            except Exception as exc:
                self.logger.error(
                    "check_signals error in strategy %s: %s", name, exc, exc_info=True
                )

        # Re-evaluate kill switches after processing all strategies
        kill_state = check_kill_switches(self.strategies, self._daily_pnl)
        for name, reason in kill_state.items():
            if reason and name not in self._paused_strategies:
                self._paused_strategies.add(name)
                self.logger.warning("Strategy %s added to paused set: %s", name, reason)
                tg_strategy_paused(name, reason)

    # ------------------------------------------------------------------
    # Post-market / EOD tasks
    # ------------------------------------------------------------------

    def post_market_tasks(self, market_state: dict) -> None:
        """Force-close remaining positions, generate daily report, back up DB."""
        today = datetime.now(NY_TZ).date()
        self.logger.info("=== Post-market tasks starting for %s ===", today)

        # 1. Force-exit any remaining open positions
        for name, strat in self.strategies.items():
            try:
                if hasattr(strat, "force_close_all"):
                    closed = strat.force_close_all(market_state, reason="EOD force-close")
                    for trade in (closed or []):
                        pnl = trade.get("pnl", 0.0)
                        self._daily_pnl[name] = self._daily_pnl.get(name, 0.0) + pnl
                        self.perf.on_trade_closed(trade, name)
                        self.db.record_trade(trade)
                        self.logger.info(
                            "EOD FORCE-CLOSE [%s] %s  P&L: $%.2f",
                            name,
                            trade.get("description", ""),
                            pnl,
                        )
            except Exception as exc:
                self.logger.error(
                    "force_close_all error in strategy %s: %s", name, exc, exc_info=True
                )

        # 2. Generate and save daily report
        try:
            market_summary = {
                "vix": market_state.get("vix", 0.0),
                "spy_price": market_state.get("spy_price", 0.0),
                "spy_pct_change": market_state.get("spy_pct_change", 0.0),
                "market_regime": market_state.get("phase", "unknown"),
                "total_trades_today": 0,
            }
            report_content = self.reporter.generate_daily_report(market_summary)
            report_path = self.reporter.save_report(report_content, today)
            self.logger.info("Daily report saved to %s", report_path)
            total_pnl = sum(self._daily_pnl.values())
            n_trades = sum(1 for v in self._daily_pnl.values() if v != 0.0)
            from datetime import date as _date
            elapsed = (_date.today() - _date(2026, 6, 2)).days + 1
            tg_daily_report(day=elapsed, total_days=90, total_pnl=total_pnl, n_trades=n_trades)
        except Exception as exc:
            self.logger.error("Failed to generate daily report: %s", exc, exc_info=True)

        # 3. DB daily backup
        try:
            self.db.daily_backup()
        except Exception as exc:
            self.logger.error("DB backup failed: %s", exc, exc_info=True)

        # 4. EOD summary log
        total_pnl = sum(self._daily_pnl.values())
        self.logger.info(
            "=== EOD Summary %s | Total P&L: $%.2f | Per-strategy: %s ===",
            today,
            total_pnl,
            {k: f"${v:.2f}" for k, v in self._daily_pnl.items()},
        )
        self._last_report_date = today

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """
        Perpetual polling loop.
        - Skips non-trading days.
        - Handles pre-market, intraday, and post-market phases.
        - Auto-restarts after transient errors (60 s cooldown).
        """
        self.logger.info("run_forever() started")

        while self._running:
            try:
                # --- Kill file global check -----------------------------------
                if KILL_FILE.exists():
                    self.logger.critical(
                        "KILL FILE detected at %s — halting all activity", KILL_FILE
                    )
                    for name in list(self.strategies.keys()):
                        self._paused_strategies.add(name)

                now = datetime.now(NY_TZ)
                today = now.date()

                # --- Non-trading day ------------------------------------------
                if not is_trading_day():
                    sleep_secs = _seconds_until_next_trading_day_premarket()
                    self.logger.info(
                        "Non-trading day (%s) — sleeping %.0f s until next pre-market",
                        now.strftime("%A"),
                        sleep_secs,
                    )
                    self._interruptible_sleep(sleep_secs)
                    continue

                phase = get_market_phase(now)
                will_check = phase in ("morning", "midday", "afternoon")
                self.logger.info(
                    "Loop tick: time=%s phase=%s will_run_strategy_checks=%s paused=%s",
                    now.strftime("%H:%M:%S ET"),
                    phase,
                    will_check,
                    list(self._paused_strategies) or "none",
                )

                # --- Daily reset (once per trading day) -----------------------
                if self._daily_reset_date != today:
                    self._reset_daily_state()

                # --- Pre-market tasks (once per trading day) ------------------
                if phase == "premarket" and self._pre_market_done_date != today:
                    self.pre_market_tasks()

                # --- Intraday strategy polling ---------------------------------
                if phase in ("morning", "midday", "afternoon"):
                    try:
                        market_state = self.build_market_state()
                        self.run_strategy_checks(market_state)
                    except Exception as exc:
                        self.logger.error(
                            "Error during intraday strategy checks: %s", exc, exc_info=True
                        )

                # --- Post-market EOD tasks ------------------------------------
                elif phase == "postmarket" and self._last_report_date != today:
                    try:
                        market_state = self.build_market_state()
                        self.post_market_tasks(market_state)
                    except Exception as exc:
                        self.logger.error(
                            "Error during post-market tasks: %s", exc, exc_info=True
                        )

                # --- Market closed — sleep until next pre-market --------------
                elif phase == "closed":
                    sleep_secs = _seconds_until_next_trading_day_premarket()
                    self.logger.info(
                        "Market closed — sleeping %.0f s until next pre-market open",
                        sleep_secs,
                    )
                    self._interruptible_sleep(sleep_secs)
                    continue

                # Standard poll interval between phases
                self._interruptible_sleep(POLL_INTERVAL_SECONDS)

            except Exception as exc:
                self.logger.error(
                    "Unhandled exception in main loop: %s — retrying in 60 s",
                    exc,
                    exc_info=True,
                )
                self._interruptible_sleep(60)

        self.logger.info("run_forever() exited cleanly")

    def _interruptible_sleep(self, seconds: float) -> None:
        """
        Sleep for `seconds` but wake immediately if self._running becomes False.
        Checks every 1 second so SIGTERM/SIGINT is handled promptly.
        """
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    logger = logging.getLogger("main")
    logger.info("CloudTrader starting up — PID %d", os.getpid())
    tg_bot_restarted(os.getpid())

    # Write PID file for startup.sh
    try:
        Path("/tmp/cloud_trader.pid").write_text(str(os.getpid()))
    except OSError:
        pass

    trader = CloudTrader()
    trader.run_forever()
    logger.info("CloudTrader shut down cleanly")


if __name__ == "__main__":
    main()
