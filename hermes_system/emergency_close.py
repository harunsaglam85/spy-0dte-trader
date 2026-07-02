#!/usr/bin/env python3
"""
Hermes EMERGENCY CLOSE (Item 4) — flatten every open SHORT leg, now.

Invoked by kill_switch.sh AFTER the engine has been stopped (so nothing can
re-enter or fight the close). It:

  1. Reads the tracked positions from positions.json.
  2. Submits a buy_to_close MARKET order for every short leg (side
     'sell_to_open') against the Tradier PRODUCTION API. Shorts carry the risk
     in a defined-risk spread, so closing them is the safety-critical action;
     the long protective legs are left in place (they can only lose their
     remaining premium, never add risk).
  3. Confirms each fill by polling the order's own status — never via a
     positions snapshot, which the broker lags 10-30s behind accepted orders.
  4. Logs everything to /root/hermes_system/logs/emergency_close.log.

Exit code 0 means every short leg reached a 'filled' status. Any leg that did
not fill (rejected, timed out, submit error) exits non-zero so the caller can
raise a CHECK-MANUALLY alert instead of reporting a clean stop.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, '/root/spy-0dte-trader')

import requests
from dotenv import load_dotenv

# ── Bootstrap ──────────────────────────────────────────────────────────────────
load_dotenv('/root/spy-0dte-trader/.env')

POSITIONS_FILE = Path('/root/hermes_system/positions.json')
LOG_FILE       = Path('/root/hermes_system/logs/emergency_close.log')
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Audit H1: target the SAME account the engine trades. The endpoint was
# hardcoded to production while the engine switches on HERMES_MODE — a kill
# switch fired in paper mode would have hit the funded live account and left
# the actual sandbox positions open.
HERMES_MODE = os.getenv('HERMES_MODE', 'paper')  # 'paper' or 'live'

if HERMES_MODE == 'live':
    PROD_BASE    = 'https://api.tradier.com/v1'
    PROD_KEY     = os.getenv('TRADIER_API_KEY', '')
    PROD_ACCOUNT = os.getenv('TRADIER_ACCOUNT_ID', '')
else:
    PROD_BASE    = 'https://sandbox.tradier.com/v1'
    PROD_KEY     = os.getenv('TRADIER_SANDBOX_KEY', '')
    PROD_ACCOUNT = os.getenv('TRADIER_SANDBOX_ACCOUNT_ID', '')
HDRS         = {'Authorization': f'Bearer {PROD_KEY}', 'Accept': 'application/json'}

# Match the engine's order-tracking constants (audit C5 / sandbox fill-lag rule).
ORDER_TERMINAL_STATUSES = frozenset({'filled', 'rejected', 'canceled', 'expired', 'error'})
ORDER_FILL_TIMEOUT      = 30.0   # max seconds to wait for a market order to go terminal
ORDER_POLL_INTERVAL     = 2.0    # seconds between order-status polls

# ── Logging (file + stdout so kill_switch.sh's tee captures it) ─────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    force=True,
)
log = logging.getLogger('hermes.emergency_close')


def _occ_root(option_symbol: str) -> str:
    """Underlying root for the Tradier 'symbol' field: everything before the
    fixed 15-char OCC tail (YYMMDD + C/P + 8-digit strike). A naive slice like
    symbol[:3] mangles NVDA/AAPL/GOOGL and gets rejected (audit D8)."""
    return option_symbol[:-15] if len(option_symbol) > 15 else option_symbol


def _is_occ_option(symbol: str) -> bool:
    """True if symbol has the fixed 15-char OCC tail (YYMMDD + C/P + strike)."""
    if len(symbol) < 16:
        return False
    tail = symbol[-15:]
    return tail[6] in ('C', 'P') and tail[:6].isdigit() and tail[7:].isdigit()


def _fetch_broker_positions():
    """{symbol: quantity} for every open position at the broker, or None when
    the positions endpoint could not be read. Audit C2: None ≠ {} — a failed
    fetch must never be treated as 'nothing open'."""
    try:
        r = requests.get(f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/positions',
                         headers=HDRS, timeout=15)
    except Exception as exc:
        log.error('Broker positions fetch error: %s', exc)
        return None
    if not r.ok:
        log.error('Broker positions fetch failed HTTP %d: %s', r.status_code, r.text[:160])
        return None
    positions = (r.json() or {}).get('positions')
    if not positions or positions == 'null':
        return {}
    pos_list = positions.get('position', [])
    if isinstance(pos_list, dict):
        pos_list = [pos_list]
    out = {}
    for p in pos_list:
        try:
            out[p['symbol']] = int(p.get('quantity', 0))
        except (KeyError, TypeError, ValueError):
            log.warning('Unparseable broker position entry: %r', p)
    return out


def _load_short_legs():
    """Read positions.json and return a list of (option_symbol, quantity) for
    every short leg across all tracked positions."""
    if not POSITIONS_FILE.exists():
        log.warning('positions.json not found at %s — nothing to close.', POSITIONS_FILE)
        return []
    try:
        positions = json.loads(POSITIONS_FILE.read_text() or '[]')
    except Exception as exc:
        log.error('Could not parse positions.json (%s) — cannot determine legs to close.', exc)
        raise
    shorts = []
    for pos in positions:
        qty = int(pos.get('contracts', 1) or 1)
        for leg in pos.get('legs', []):
            if leg.get('side') == 'sell_to_open' and leg.get('option_symbol'):
                shorts.append((leg['option_symbol'], qty, pos.get('strategy', '?')))
    return shorts


def _submit_buy_to_close(option_symbol: str, quantity: int):
    """Submit a market buy_to_close for one short option leg. Returns the order
    id on acceptance, else None."""
    data = {
        'class':         'option',
        'symbol':        _occ_root(option_symbol),
        'option_symbol': option_symbol,
        'side':          'buy_to_close',
        'quantity':      str(quantity),
        'type':          'market',
        'duration':      'day',
    }
    try:
        r = requests.post(f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/orders',
                          data=data, headers=HDRS, timeout=15)
    except Exception as exc:
        log.error('  SUBMIT ERROR buy_to_close %s x%d: %s', option_symbol, quantity, exc)
        return None
    if not r.ok:
        log.error('  REJECTED buy_to_close %s x%d -> HTTP %d %s',
                  option_symbol, quantity, r.status_code, r.text[:160])
        return None
    oid = (r.json().get('order') or {}).get('id')
    log.info('  SUBMITTED buy_to_close %s x%d -> order %s', option_symbol, quantity, oid)
    return oid


def _await_fill(order_id) -> dict:
    """Poll one order's status until terminal or timeout. Returns the order dict."""
    deadline = time.monotonic() + ORDER_FILL_TIMEOUT
    last = {}
    while time.monotonic() < deadline:
        try:
            r = requests.get(f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/orders/{order_id}',
                             headers=HDRS, timeout=10)
            if r.ok:
                last = (r.json().get('order') or {})
                status = (last.get('status') or '').lower()
                if status in ORDER_TERMINAL_STATUSES:
                    return last
        except Exception as exc:
            log.warning('  poll error for order %s: %s', order_id, exc)
        time.sleep(ORDER_POLL_INTERVAL)
    return last


def main() -> int:
    log.info('=== EMERGENCY CLOSE invoked (%s mode, account %s) ===',
             HERMES_MODE.upper(), PROD_ACCOUNT or '(unset)')

    if not PROD_KEY or not PROD_ACCOUNT:
        log.error('Tradier production creds missing — CANNOT close positions. CHECK MANUALLY.')
        return 1

    # Audit C2: the BROKER is the source of truth for what is open.
    # positions.json supplies strategy labels and a cross-check only.
    try:
        tracked = _load_short_legs()
    except Exception:
        tracked = []          # corrupt positions.json no longer aborts the close
    strat_by_sym = {sym: strat for sym, _, strat in tracked}

    broker = _fetch_broker_positions()
    reconciled = broker is not None
    unclosable = 0
    if reconciled:
        shorts = []
        for sym, qty in broker.items():
            if qty >= 0:
                if qty > 0:
                    log.info('Leaving long position alone (bounded risk): %s x%d', sym, qty)
                continue
            if not _is_occ_option(sym):
                log.error('SHORT NON-OPTION at broker: %s x%d — cannot auto-close, '
                          'CHECK MANUALLY.', sym, qty)
                unclosable += 1
                continue
            strat = strat_by_sym.get(sym, 'untracked')
            if strat == 'untracked':
                log.warning('UNTRACKED short at broker will be closed: %s x%d', sym, abs(qty))
            shorts.append((sym, abs(qty), strat))
        for sym in set(strat_by_sym) - {s for s, _, _ in shorts}:
            log.warning('Tracked short %s not open at broker — already closed/expired, skipping.', sym)
    else:
        log.error('Cannot read broker positions — falling back to positions.json legs; '
                  'exit will be non-zero so the caller raises CHECK-MANUALLY.')
        shorts = tracked

    if not shorts:
        if reconciled and unclosable == 0:
            log.info('No open short option legs at the broker — nothing to close.')
            return 0
        log.error('No closable legs identified but state is NOT verified — CHECK MANUALLY.')
        return 1

    log.info('Closing %d short leg(s): %s', len(shorts),
             ', '.join(f'{s}x{q}' for s, q, _ in shorts))

    # Submit all closes first, then confirm — fills happen in parallel at the broker.
    pending = []  # (option_symbol, quantity, strategy, order_id)
    for option_symbol, qty, strat in shorts:
        oid = _submit_buy_to_close(option_symbol, qty)
        pending.append((option_symbol, qty, strat, oid))

    filled, failed = 0, 0
    for option_symbol, qty, strat, oid in pending:
        if not oid:
            failed += 1
            continue
        order = _await_fill(oid)
        status = (order.get('status') or 'unknown').lower()
        fill_px = order.get('avg_fill_price')
        if status == 'filled':
            filled += 1
            log.info('  FILLED %s [%s] order %s avg_fill_price=%s',
                     option_symbol, strat, oid, fill_px)
        else:
            failed += 1
            log.error('  NOT FILLED %s [%s] order %s status=%s — CHECK MANUALLY.',
                      option_symbol, strat, oid, status)

    log.info('=== EMERGENCY CLOSE complete: %d filled, %d failed (of %d short legs)%s ===',
             filled, failed, len(shorts),
             '' if reconciled else ' — BROKER STATE UNVERIFIED')
    return 0 if (failed == 0 and unclosable == 0 and reconciled) else 1


if __name__ == '__main__':
    sys.exit(main())
