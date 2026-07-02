#!/usr/bin/env python3
"""
kill_switch.py — Telegram command handler for zero-token Hermes system control.

Polls Telegram for messages from ALLOWED_CHAT_ID and dispatches shell/file
operations with no AI API calls. Runs as hermes-kill-switch.service.

Commands:
  /restart   — systemctl restart hermes-engine, reply status
  /logs      — last 20 journal lines for hermes-engine
  /vix       — VIX, VIX3M, contango ratio, regime from market_context.json
  /positions — open positions from positions.json
  /pnl       — today's closed-trade P&L sum from trades/YYYY-MM-DD.json
  /health    — systemctl is-active for engine, thetadata, heartbeat timer
"""
import json
import logging
import os
import subprocess
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv('/root/spy-0dte-trader/.env')

BOT_TOKEN       = os.getenv('TELEGRAM_BOT_TOKEN', '')
ALLOWED_CHAT_ID = 8708514665
HERMES_ROOT     = Path('/root/hermes_system')
TRADES_DIR      = HERMES_ROOT / 'trades'
TG_BASE         = f'https://api.telegram.org/bot{BOT_TOKEN}'

LOG_DIR = HERMES_ROOT / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s kill_switch %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'kill_switch.log'),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger('kill_switch')


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg_send(chat_id: int, text: str) -> None:
    """Send message; truncate to Telegram's 4096-char limit."""
    if not BOT_TOKEN:
        return
    if len(text) > 4096:
        text = text[:4090] + '\n...'
    try:
        r = requests.post(
            f'{TG_BASE}/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
        if not r.ok:
            log.warning('tg_send failed: %s %s', r.status_code, r.text[:200])
    except Exception as exc:
        log.warning('tg_send error: %s', exc)


def get_updates(offset: int) -> list:
    try:
        r = requests.get(
            f'{TG_BASE}/getUpdates',
            params={'timeout': 30, 'offset': offset},
            timeout=40,
        )
        if not r.ok:
            log.warning('getUpdates failed: %s', r.status_code)
            return []
        return r.json().get('result', [])
    except Exception as exc:
        log.warning('getUpdates error: %s', exc)
        return []


# ── Shell helper ──────────────────────────────────────────────────────────────

def run_cmd(cmd: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        out = (result.stdout + result.stderr).strip()
        return out if out else '(no output)'
    except subprocess.TimeoutExpired:
        return f'(timed out after {timeout}s)'
    except Exception as exc:
        return f'(error: {exc})'


# ── Command handlers ──────────────────────────────────────────────────────────

def handle_restart(chat_id: int) -> None:
    tg_send(chat_id, 'Restarting hermes-engine...')
    run_cmd('systemctl restart hermes-engine')
    time.sleep(3)
    active = run_cmd('systemctl is-active hermes-engine').strip()
    pid    = run_cmd('systemctl show hermes-engine --property=MainPID --value').strip()
    tg_send(chat_id, f'hermes-engine: {active} (PID {pid})')


# ── /stop: emergency kill switch (audit C1) ──────────────────────────────────
# Two-step confirmation: the first /stop arms the switch, a second /stop within
# STOP_CONFIRM_WINDOW seconds executes kill_switch.sh (engine stop + flatten).
STOP_CONFIRM_WINDOW = 60.0
_stop_armed_at = 0.0


def handle_stop(chat_id: int) -> None:
    global _stop_armed_at
    now = time.monotonic()
    if now - _stop_armed_at > STOP_CONFIRM_WINDOW:
        _stop_armed_at = now
        tg_send(chat_id,
                '⚠️ KILL SWITCH armed. This will STOP hermes-engine and flatten '
                'all open short legs at the broker.\n'
                f'Send /stop again within {int(STOP_CONFIRM_WINDOW)}s to confirm.')
        return
    _stop_armed_at = 0.0
    tg_send(chat_id, '🛑 KILL SWITCH confirmed — stopping engine and flattening positions...')
    out = run_cmd('bash /root/hermes_system/kill_switch.sh', timeout=300)
    tg_send(chat_id, f'kill_switch.sh finished:\n{out[-1500:]}')


def handle_logs(chat_id: int) -> None:
    out = run_cmd(
        'journalctl -u hermes-engine --since "today" --no-pager | tail -20',
        timeout=25,
    )
    tg_send(chat_id, f'hermes-engine logs (last 20 lines):\n{out}')


def handle_vix(chat_id: int) -> None:
    ctx_file = HERMES_ROOT / 'market_context.json'
    if not ctx_file.exists():
        tg_send(chat_id, 'market_context.json not found')
        return
    try:
        ctx = json.loads(ctx_file.read_text())
    except Exception as exc:
        tg_send(chat_id, f'Cannot read market_context.json: {exc}')
        return

    vol    = ctx.get('volatility', {})
    vix    = vol.get('vix', 'n/a')
    vix3m  = vol.get('vix3m', 'n/a')
    ratio  = vol.get('contango_ratio', 'n/a')
    regime = vol.get('regime', 'n/a')
    gen_at = ctx.get('generated_at', '?')[:16]

    try:
        vix   = f'{float(vix):.2f}'
        vix3m = f'{float(vix3m):.2f}'
        ratio = f'{float(ratio):.4f}'
    except (TypeError, ValueError):
        pass

    tg_send(chat_id, (
        f'VIX Status ({gen_at})\n'
        f'VIX:    {vix}\n'
        f'VIX3M:  {vix3m}\n'
        f'Ratio:  {ratio}\n'
        f'Regime: {regime}'
    ))


def handle_positions(chat_id: int) -> None:
    pos_file = HERMES_ROOT / 'positions.json'
    if not pos_file.exists():
        tg_send(chat_id, 'No open positions')
        return
    try:
        data = json.loads(pos_file.read_text() or '[]')
    except Exception as exc:
        tg_send(chat_id, f'Cannot read positions.json: {exc}')
        return

    if not data:
        tg_send(chat_id, 'No open positions')
        return

    lines = [f'Open Positions ({len(data)})']
    for p in data:
        strat  = p.get('strategy', '?')
        stype  = p.get('spread_type', '?')
        credit = p.get('entry_credit', 'n/a')
        etime  = str(p.get('entry_time', '?'))[:16]
        legs   = p.get('legs', [])
        syms   = ' / '.join(l.get('option_symbol', '?') for l in legs)
        try:
            credit = f'${float(credit):.2f}'
        except (TypeError, ValueError):
            pass
        lines.append(f'{strat} {stype} | Credit: {credit} | {etime}\n  {syms}')

    tg_send(chat_id, '\n'.join(lines))


def handle_pnl(chat_id: int) -> None:
    today_file = TRADES_DIR / f'{date.today().isoformat()}.json'
    if not today_file.exists():
        tg_send(chat_id, f'P&L today: $0.00 (no trades yet)')
        return
    try:
        trades = json.loads(today_file.read_text() or '[]')
    except Exception as exc:
        tg_send(chat_id, f'Cannot read trades: {exc}')
        return

    closed = [t for t in trades if t.get('status') == 'closed' and 'pnl' in t]
    total  = sum(float(t['pnl']) for t in closed)
    sign   = '+' if total >= 0 else ''
    n      = len(closed)
    tg_send(chat_id, (
        f'P&L today: {sign}${total:.2f}\n'
        f'Closed trades: {n} | Open stubs: {len(trades) - n}'
    ))


def handle_health(chat_id: int) -> None:
    checks = [
        ('hermes-engine',         'systemctl is-active hermes-engine'),
        ('thetadata',             'systemctl is-active thetadata'),
        ('hermes-heartbeat.timer','systemctl is-active hermes-heartbeat.timer'),
    ]
    lines = ['System Health']
    for name, cmd in checks:
        status = run_cmd(cmd).strip()
        icon   = 'OK' if status == 'active' else 'DOWN'
        lines.append(f'[{icon}] {name}: {status}')
    tg_send(chat_id, '\n'.join(lines))


# ── Dispatch table ────────────────────────────────────────────────────────────

HANDLERS = {
    '/stop':      handle_stop,
    '/restart':   handle_restart,
    '/logs':      handle_logs,
    '/vix':       handle_vix,
    '/positions': handle_positions,
    '/pnl':       handle_pnl,
    '/health':    handle_health,
}

HELP_TEXT = 'Commands: ' + ' '.join(HANDLERS)


# ── Main polling loop ─────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        log.error('TELEGRAM_BOT_TOKEN not set — kill switch cannot start')
        return

    log.info('Kill switch started (authorized chat: %s)', ALLOWED_CHAT_ID)
    offset = 0
    # Audit C1/L4: discard any updates queued while the service was down so a
    # stale command (especially an old /stop pair) can never replay on startup.
    backlog = get_updates(offset)
    if backlog:
        offset = backlog[-1]['update_id'] + 1
        log.info('Discarded %d stale queued update(s) on startup.', len(backlog))

    while True:
        updates = get_updates(offset)
        for upd in updates:
            offset = upd['update_id'] + 1
            msg = upd.get('message') or upd.get('edited_message')
            if not msg:
                continue
            chat_id = msg.get('chat', {}).get('id')
            if chat_id != ALLOWED_CHAT_ID:
                log.info('Ignoring message from unauthorized chat_id %s', chat_id)
                continue
            text = (msg.get('text') or '').strip()
            if not text:
                continue
            # Strip @BotName suffix (e.g. /restart@MyBot → /restart)
            cmd = text.split()[0].split('@')[0].lower()
            handler = HANDLERS.get(cmd)
            if handler:
                log.info('Command: %s from %s', cmd, chat_id)
                try:
                    handler(chat_id)
                except Exception as exc:
                    log.error('Command %s raised: %s', cmd, exc)
                    tg_send(chat_id, f'Command failed: {exc}')
            elif cmd.startswith('/'):
                tg_send(chat_id, f'Unknown command: {cmd}\n{HELP_TEXT}')

        if not updates:
            time.sleep(1)


if __name__ == '__main__':
    main()
