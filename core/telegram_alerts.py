"""
telegram_alerts.py — Simple Telegram notification helper.

Uses requests.post directly — no extra libraries required.
Gracefully no-ops if TELEGRAM_BOT_TOKEN is not set.
"""

import logging
import os

import requests

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_logger = logging.getLogger("telegram_alerts")


def _enabled() -> bool:
    return bool(_BOT_TOKEN and _CHAT_ID)


def send(message: str) -> None:
    """Send a Telegram message. Silently skips if credentials not configured."""
    if not _enabled():
        return
    try:
        url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": _CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
        if not resp.ok:
            _logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        _logger.warning("Telegram send error: %s", exc)


def trade_entered(strategy: str, direction: str, price: float, vix: float) -> None:
    send(f"🟢 TRADE ENTERED: {strategy} {direction} @ ${price:.2f} | VIX: {vix:.2f}")


def trade_exited(strategy: str, pnl: float, reason: str) -> None:
    sign = "+" if pnl >= 0 else ""
    send(f"🔴 TRADE EXITED: {strategy} P&L: {sign}${pnl:.2f} | Reason: {reason}")


def strategy_paused(name: str, reason: str = "daily loss limit hit") -> None:
    send(f"⚠️ STRATEGY PAUSED: {name} — {reason}")


def daily_report(day: int, total_days: int, total_pnl: float, n_trades: int) -> None:
    sign = "+" if total_pnl >= 0 else ""
    send(f"📊 DAILY REPORT Day {day}/{total_days} | P&L: {sign}${total_pnl:.2f} | Trades: {n_trades}")


def bot_restarted(pid: int) -> None:
    send(f"🔄 BOT RESTARTED — PID {pid}")
