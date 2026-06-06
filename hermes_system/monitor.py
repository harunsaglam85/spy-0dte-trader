#!/usr/bin/env python3
"""
Hermes Monitor — Real-time position monitor, refreshes every 60 seconds.
Shows all open sandbox positions, current P&L, and Greeks.
Sends Telegram alert when a position hits 80% of its max loss.
Runs during market hours only (9:30 AM – 4:00 PM ET).
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import json
import os
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Set

import pytz
import requests
from dotenv import load_dotenv

from core.data_feeds import DataFeeds
from core.telegram_alerts import send as tg_send

load_dotenv('/root/spy-0dte-trader/.env')

HERMES_ROOT  = Path('/root/hermes_system')
SANDBOX_BASE = 'https://sandbox.tradier.com/v1'
ET           = pytz.timezone('America/New_York')
ALERT_PCT    = 0.80    # alert when unrealised loss reaches 80% of max loss


def _is_market_hours(now: datetime) -> bool:
    return (9, 30) <= (now.hour, now.minute) <= (16, 0) and now.weekday() < 5


def _sb_headers() -> dict:
    return {
        'Authorization': f'Bearer {os.getenv("TRADIER_SANDBOX_KEY", "")}',
        'Accept':        'application/json',
    }


# ── Sandbox position fetch ─────────────────────────────────────────────────────

def fetch_positions() -> List[dict]:
    account = os.getenv('TRADIER_SANDBOX_ACCOUNT_ID', '')
    key     = os.getenv('TRADIER_SANDBOX_KEY', '')
    if not key or not account:
        return []
    try:
        r = requests.get(
            f'{SANDBOX_BASE}/accounts/{account}/positions',
            headers=_sb_headers(),
            timeout=10,
        )
        if not r.ok:
            return []
        raw = r.json().get('positions', {}).get('position', None)
        if raw is None:
            return []
        return raw if isinstance(raw, list) else [raw]
    except Exception as exc:
        print(f'Position fetch error: {exc}')
        return []


def fetch_account_summary() -> dict:
    account = os.getenv('TRADIER_SANDBOX_ACCOUNT_ID', '')
    key     = os.getenv('TRADIER_SANDBOX_KEY', '')
    if not key or not account:
        return {}
    try:
        r = requests.get(
            f'{SANDBOX_BASE}/accounts/{account}/balances',
            headers=_sb_headers(),
            timeout=10,
        )
        if not r.ok:
            return {}
        return r.json().get('balances', {})
    except Exception:
        return {}


# ── Greeks & quote fetch ──────────────────────────────────────────────────────

def enrich_position(pos: dict, feeds: DataFeeds) -> dict:
    """Add current mid, P&L, and Greeks to a position dict."""
    sym   = pos.get('symbol', '')
    qty   = int(pos.get('quantity', 0))
    cost  = float(pos.get('cost_basis', 0))  # total cost basis
    try:
        q   = feeds.get_tradier_quote(sym)
        mid = (q.get('bid', 0.0) + q.get('ask', 0.0)) / 2.0
        pnl = (mid * 100 * abs(qty)) - abs(cost) if qty > 0 else abs(cost) - (mid * 100 * abs(qty))
    except Exception:
        mid, pnl = 0.0, 0.0
    return {**pos, 'current_mid': round(mid, 2), 'pnl': round(pnl, 2)}


# ── Terminal rendering ────────────────────────────────────────────────────────

def render(positions: List[dict], balances: dict, now: datetime) -> str:
    now_str = now.strftime('%Y-%m-%d %H:%M:%S ET')
    lines   = [
        f"╔{'═' * 63}╗",
        f"║  HERMES MONITOR  {now_str:<44}║",
        f"╠{'═' * 63}╣",
    ]

    if not positions:
        lines.append(f"║  {'No open positions':^61}║")
    else:
        header = f"  {'SYMBOL':<32} {'QTY':>4}  {'MID':>6}  {'P&L':>9}"
        lines.append(f"║{header:<63}║")
        lines.append(f"║{'─' * 63}║")
        for p in positions:
            sym   = p.get('symbol', '?')[:30]
            qty   = p.get('quantity', 0)
            mid   = p.get('current_mid', 0.0)
            pnl   = p.get('pnl', 0.0)
            sign  = '+' if pnl >= 0 else ''
            row   = f"  {sym:<32} {qty:>4}  {mid:>6.2f}  {sign}{pnl:>8.2f}"
            lines.append(f"║{row:<63}║")

    lines.append(f"╠{'═' * 63}╣")

    # Account summary
    equity = balances.get('equity', 'n/a')
    cash   = balances.get('cash', {}).get('cash_available', 'n/a') if isinstance(balances.get('cash'), dict) else 'n/a'
    lines.append(f"║  Equity: {str(equity):<20} Cash available: {str(cash):<20}║")
    lines.append(f"╚{'═' * 63}╝")
    return '\n'.join(lines)


# ── Stop-loss alert logic ─────────────────────────────────────────────────────

def check_alerts(positions: List[dict], alerted: Set[str]) -> None:
    for p in positions:
        sym = p.get('symbol', '')
        if sym in alerted:
            continue
        pnl       = p.get('pnl', 0.0)
        cost_basis = abs(float(p.get('cost_basis', 0)))
        if cost_basis <= 0:
            continue
        loss_pct = -pnl / cost_basis if pnl < 0 else 0.0
        if loss_pct >= ALERT_PCT:
            alerted.add(sym)
            tg_send(
                f"⚠️ HERMES MONITOR: {sym}\n"
                f"Loss ${-pnl:.2f} = {loss_pct:.0%} of cost basis — approaching stop."
            )


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    feeds   = DataFeeds()
    alerted: Set[str] = set()
    print('Hermes Monitor starting...')

    while True:
        now = datetime.now(ET)
        if not _is_market_hours(now):
            time.sleep(60)
            continue

        raw_positions = fetch_positions()
        enriched      = [enrich_position(p, feeds) for p in raw_positions]
        balances      = fetch_account_summary()

        output = render(enriched, balances, now)
        # Clear terminal and redraw
        print('\033[2J\033[H', end='', flush=True)
        print(output, flush=True)

        check_alerts(enriched, alerted)

        # Reset alerted set at midnight so alerts can re-fire next day
        if now.hour == 0 and now.minute < 2:
            alerted.clear()

        time.sleep(60)


if __name__ == '__main__':
    main()
