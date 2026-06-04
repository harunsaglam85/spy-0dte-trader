"""
reporter.py — Daily report generator for the cloud paper-trader.

Generates a structured plain-text report at 4:30 PM ET covering:
    - Market summary
    - Per-strategy performance breakdown
    - Today's winner / loser
    - Pending human-review suggestions
    - 30-day scorecard
    - Kill-switch status for any paused / dead strategies

Reports are saved to reports/YYYY-MM-DD.txt.
"""

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pytz

NY_TZ = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# Report-formatting helpers
# ---------------------------------------------------------------------------

_SEPARATOR_WIDE = "=" * 70
_SEPARATOR_THIN = "-" * 70


def _pct(value: float) -> str:
    """Format a fraction as a percentage string, e.g. 0.712 → '71.2%'."""
    return f"{value * 100:.1f}%"


def _money(value: float) -> str:
    """Format a dollar value with sign, e.g. 123.4 → '+$123.40'."""
    sign = "+" if value >= 0 else ""
    return f"{sign}${value:.2f}"


class Reporter:
    """Generate and persist daily performance reports.

    Parameters
    ----------
    db :
        Initialised ``Database`` instance.
    strategies : dict
        Mapping of ``{strategy_name: strategy_instance}``.  Strategy instances
        are stored but not called directly — they are used only for metadata
        (e.g., checking a ``is_paused`` attribute if present).
    """

    def __init__(self, db, strategies: dict) -> None:
        self.db = db
        self.strategies = strategies
        self.logger = logging.getLogger("reporter")
        self.reports_dir = Path("reports")
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_start = date(2026, 6, 2)
        self.experiment_days = 90

    # ------------------------------------------------------------------
    # Main public API
    # ------------------------------------------------------------------

    def generate_daily_report(self, report_date, daily_pnl: dict) -> str:
        """Build, save, and return the file path of the daily report.

        Parameters
        ----------
        report_date : date
            The trading date for this report.
        daily_pnl : dict
            Mapping of {strategy_name: pnl_float} accumulated during the session.
        """
        now_ny = datetime.now(NY_TZ)
        market_summary: dict = {}

        sections: list = []

        sections.append(self._build_header(report_date, now_ny))
        sections.append(self._build_market_summary(market_summary))
        sections.append(self._build_strategy_breakdown(report_date, market_summary))
        sections.append(self._build_winner_loser(report_date))
        sections.append(self._build_pending_suggestions())
        sections.append(self._build_scorecard_30d())
        sections.append(self._build_kill_switch_status())

        report = "\n\n".join(sections)
        return self.save_report(report, report_date)

    def save_report(self, content: str, report_date: date = None) -> str:
        """Write *content* to reports/YYYY-MM-DD.txt.

        Returns
        -------
        str  Absolute path of the saved report file.
        """
        if report_date is None:
            report_date = date.today()
        filename = f"{report_date.isoformat()}.txt"
        path = self.reports_dir / filename
        path.write_text(content, encoding="utf-8")
        self.logger.info("Daily report saved to %s", path)
        return str(path)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_header(self, today: date, now_ny: datetime) -> str:
        elapsed = (today - self.experiment_start).days + 1
        remaining = max(0, self.experiment_days - elapsed)
        lines = [
            _SEPARATOR_WIDE,
            "  CLOUD PAPER-TRADER — DAILY REPORT",
            _SEPARATOR_WIDE,
            f"  Date        : {today.isoformat()}",
            f"  Generated   : {now_ny.strftime('%H:%M:%S ET')}",
            f"  Experiment  : Day {elapsed} of {self.experiment_days} "
            f"({remaining} days remaining)",
            _SEPARATOR_WIDE,
        ]
        return "\n".join(lines)

    def _build_market_summary(self, market_summary: dict) -> str:
        vix = market_summary.get("vix", 0.0)
        spy = market_summary.get("spy_price", 0.0)
        spy_chg = market_summary.get("spy_pct_change", 0.0)
        regime = market_summary.get("market_regime", "UNKNOWN")
        total_today = market_summary.get("total_trades_today", 0)

        vix_label = self._classify_vix(vix)
        lines = [
            "MARKET SUMMARY",
            _SEPARATOR_THIN,
            f"  VIX              : {vix:.2f}  ({vix_label})",
            f"  SPY              : ${spy:.2f}  ({spy_chg * 100:+.2f}%)",
            f"  Market Regime    : {regime}",
            f"  Total Trades Today : {total_today}",
        ]
        return "\n".join(lines)

    def _build_strategy_breakdown(self, today: date, market_summary: dict) -> str:
        lines = ["STRATEGY BREAKDOWN", _SEPARATOR_THIN]
        if not self.strategies:
            lines.append("  No strategies registered.")
            return "\n".join(lines)

        for name in self.strategies:
            trades_today = self._get_today_trades(name)
            all_stats = self.db.get_strategy_stats(name)
            section = self._format_strategy_section(name, trades_today, all_stats)
            lines.append(section)

        return "\n".join(lines)

    def _build_winner_loser(self, today: date) -> str:
        today_str = today.isoformat()
        strategy_pnl: dict = {}

        for name in self.strategies:
            trades_today = self._get_today_trades(name)
            strategy_pnl[name] = sum(t.get("pnl", 0.0) for t in trades_today)

        if not strategy_pnl:
            return "TODAY'S WINNER / LOSER\n" + _SEPARATOR_THIN + "\n  No trades today."

        winner = max(strategy_pnl, key=strategy_pnl.get)
        loser = min(strategy_pnl, key=strategy_pnl.get)

        lines = [
            "TODAY'S WINNER / LOSER",
            _SEPARATOR_THIN,
            f"  WINNER : {winner}  {_money(strategy_pnl[winner])}",
            f"  LOSER  : {loser}   {_money(strategy_pnl[loser])}",
        ]
        return "\n".join(lines)

    def _build_pending_suggestions(self) -> str:
        suggestions = self.db.get_pending_suggestions()
        lines = ["PENDING SUGGESTIONS", _SEPARATOR_THIN]
        if not suggestions:
            lines.append("  None.")
        else:
            for s in suggestions:
                lines.append(
                    f"  [{s.get('strategy', '?')}] {s.get('suggestion_text', '')} "
                    f"(created: {s.get('created_date', '?')})"
                )
        return "\n".join(lines)

    def _build_scorecard_30d(self) -> str:
        history = self.db.get_performance_history(days=30)

        # Collapse to the latest row per strategy.
        latest: dict = {}
        for row in history:
            strat = row.get("strategy", "")
            if strat not in latest:
                latest[strat] = row

        lines = ["30-DAY SCORECARD", _SEPARATOR_THIN]
        header = f"  {'Strategy':<20} {'Trades':>7} {'WR':>8} {'P&L':>12} {'Status':<20}"
        lines.append(header)
        lines.append("  " + "-" * 66)

        if not latest:
            lines.append("  No data available.")
        else:
            for strat, row in sorted(latest.items()):
                wr_str = _pct(row.get("wr", 0.0))
                pnl_str = _money(row.get("total_pnl", 0.0))
                line = (
                    f"  {strat:<20} "
                    f"{row.get('total_trades', 0):>7} "
                    f"{wr_str:>8} "
                    f"{pnl_str:>12} "
                    f"{row.get('status', 'UNKNOWN'):<20}"
                )
                lines.append(line)

        return "\n".join(lines)

    def _build_kill_switch_status(self) -> str:
        lines = ["KILL SWITCH STATUS", _SEPARATOR_THIN]
        any_dead = False

        for name, strat in self.strategies.items():
            # Check for a paused/dead flag on the strategy object if available.
            is_paused = getattr(strat, "is_paused", False)
            status_from_db = self._get_latest_db_status(name)

            if is_paused or status_from_db in ("DEAD", "UNDERPERFORMING"):
                any_dead = True
                reason = "PAUSED" if is_paused else status_from_db
                lines.append(f"  !! {name:<20}  Status: {reason}")

        if not any_dead:
            lines.append("  All strategies running normally.")

        lines.append(_SEPARATOR_WIDE)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _get_today_trades(self, strategy_name: str) -> list:
        """Return all trades for *strategy_name* entered today."""
        today_str = date.today().isoformat()
        all_trades = self.db.get_trades_by_strategy(strategy_name, since_date=today_str)
        # since_date already filters to today and beyond; return as-is.
        return all_trades

    def _format_strategy_section(
        self, name: str, trades_today: list, stats: dict
    ) -> str:
        """Format a single strategy sub-section for the breakdown."""
        n_today = len(trades_today)
        pnl_today = sum(t.get("pnl", 0.0) for t in trades_today)
        wins_today = sum(1 for t in trades_today if t.get("pnl", 0.0) > 0)

        # Best and worst trade today.
        best_trade = worst_trade = None
        if trades_today:
            best_trade = max(trades_today, key=lambda t: t.get("pnl", 0.0))
            worst_trade = min(trades_today, key=lambda t: t.get("pnl", 0.0))

        status = self._get_latest_db_status(name)

        lines = [
            f"  Strategy : {name}",
            f"    Today  : {n_today} trades  "
            f"({wins_today}W / {n_today - wins_today}L)  "
            f"P&L: {_money(pnl_today)}",
            f"    All-time WR  : {_pct(stats.get('wr', 0.0))}  "
            f"({stats.get('wins', 0)}W / {stats.get('losses', 0)}L  "
            f"from {stats.get('n', 0)} trades)",
            f"    All-time P&L : {_money(stats.get('total_pnl', 0.0))}  "
            f"(expectancy: {_money(stats.get('expectancy', 0.0))}/trade)",
            f"    Status       : {status}",
        ]

        if best_trade:
            lines.append(
                f"    Best today   : {_money(best_trade.get('pnl', 0.0))}  "
                f"({best_trade.get('exit_reason', '?')})"
            )
        if worst_trade and worst_trade is not best_trade:
            lines.append(
                f"    Worst today  : {_money(worst_trade.get('pnl', 0.0))}  "
                f"({worst_trade.get('exit_reason', '?')})"
            )

        return "\n".join(lines)

    def _get_latest_db_status(self, strategy: str) -> str:
        """Look up the most recent status label for *strategy* in the DB."""
        history = self.db.get_performance_history(days=365)
        for row in history:
            if row.get("strategy") == strategy:
                return row.get("status", "INSUFFICIENT_DATA")
        return "INSUFFICIENT_DATA"

    @staticmethod
    def _classify_vix(vix: float) -> str:
        """Return a human-readable VIX regime label."""
        if vix < 15:
            return "LOW VOLATILITY"
        if vix < 18:
            return "NORMAL"
        if vix < 22:
            return "ELEVATED"
        if vix < 30:
            return "HIGH VOLATILITY"
        return "EXTREME VOLATILITY"
