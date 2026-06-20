#!/usr/bin/env python3
"""
Hermes Assignment Checker (Batch 2 — Task 2).

Runs daily at 9:15 AM ET (before the 9:45 engine start) and checks the Tradier
PRODUCTION account for any SPY *equity* position — the signature of an overnight
option assignment on a 0DTE short put/call that finished in-the-money. The engine
trades only options, so any SPY shares in the account require manual handling
(the engine will not, and should not, auto-liquidate stock).

On detection it pages Telegram and logs to /root/hermes_system/logs/assignment.log.

Cron: 15 9 * * 1-5  cd /root/spy-0dte-trader && python3 hermes_system/assignment_checker.py
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import logging
import os
from datetime import datetime
from pathlib import Path

import pytz
import requests
from dotenv import load_dotenv

load_dotenv('/root/spy-0dte-trader/.env')

from core.telegram_alerts import send as tg_send

# ── Config ───────────────────────────────────────────────────────────────────
HERMES_ROOT = Path('/root/hermes_system')
LOG_DIR     = HERMES_ROOT / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

PROD_BASE    = 'https://api.tradier.com/v1'
PROD_KEY     = os.getenv('TRADIER_API_KEY', '')
PROD_ACCOUNT = os.getenv('TRADIER_ACCOUNT_ID', '')

ET = pytz.timezone('America/New_York')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_DIR / 'assignment.log'), logging.StreamHandler()],
)
log = logging.getLogger('hermes.assignment')


def _is_equity_symbol(symbol: str) -> bool:
    """An OCC option symbol is root + 15-char tail (YYMMDD + C/P + 8 digits).
    A bare 'SPY' (no option tail) is an equity position — i.e. assigned shares."""
    s = symbol.strip().upper()
    if not s.startswith('SPY'):
        return False
    tail = s[-15:]
    looks_like_option = (
        len(s) >= 16 and tail[6] in ('C', 'P')
        and tail[:6].isdigit() and tail[7:].isdigit()
    )
    return not looks_like_option


def fetch_positions() -> list:
    """Return Tradier production positions as a list of {symbol, quantity} dicts."""
    if not PROD_KEY or not PROD_ACCOUNT:
        log.error('Tradier production credentials not set — cannot check assignment.')
        return []
    try:
        r = requests.get(
            f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/positions',
            headers={'Authorization': f'Bearer {PROD_KEY}', 'Accept': 'application/json'},
            timeout=10,
        )
        if not r.ok:
            log.error('Positions fetch failed %d: %s', r.status_code, r.text[:200])
            return []
        positions = r.json().get('positions', {})
        if not positions or positions == 'null':
            return []
        pos_list = positions.get('position', [])
        if isinstance(pos_list, dict):
            pos_list = [pos_list]
        return [{'symbol': p['symbol'], 'quantity': int(p.get('quantity', 0))} for p in pos_list]
    except Exception as exc:
        log.error('fetch_positions error: %s', exc)
        return []


def main() -> None:
    now = datetime.now(ET)
    log.info('Assignment check starting (%s ET).', now.isoformat())
    positions = fetch_positions()

    equity = [p for p in positions
              if p['quantity'] != 0 and _is_equity_symbol(p['symbol'])]

    if not equity:
        log.info('No SPY equity positions — no assignment. (%d total position(s) open.)',
                 len(positions))
        return

    for p in equity:
        qty = p['quantity']
        log.critical('ASSIGNMENT DETECTED — %s equity position: %d shares.', p['symbol'], qty)
        tg_send(
            f"⚠️ ASSIGNMENT DETECTED — SPY equity position found: {qty} shares. "
            f"Manual action required."
        )


if __name__ == '__main__':
    main()
