#!/usr/bin/env python3
"""
Hermes Daily Brief — Reads pattern_summary.json and today's trades.
Generates a structured ~300-word brief. Flags kill criteria breaches.
Saves to /root/hermes_system/daily_brief.txt
Cron: 45 16 * * 1-5  (4:45 PM ET)
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import json
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERMES_ROOT  = Path('/root/hermes_system')
TRADES_DIR   = HERMES_ROOT / 'trades'
SUMMARY_PATH = HERMES_ROOT / 'pattern_summary.json'
BRIEF_PATH   = HERMES_ROOT / 'daily_brief.txt'

ENGINE_UNIT  = 'hermes-engine'

STRATEGIES = ['R3A', 'R3B', 'R3C', 'R3D', 'R3E', 'R8', 'R10', 'S4']


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_today() -> List[dict]:
    # FIX 3: P&L source of truth is the per-day trade JSON, never execution.log /
    # main.log (legacy stale files from old screen sessions). The engine appends a
    # close record with status='closed' and a pnl field for every closed trade.
    path = TRADES_DIR / f'{date.today().isoformat()}.json'
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def load_summary() -> dict:
    if not SUMMARY_PATH.exists():
        return {}
    try:
        return json.loads(SUMMARY_PATH.read_text())
    except Exception:
        return {}


def engine_status() -> str:
    """FIX 3: live engine state via systemd, not by tailing legacy log files.
    Returns 'active', 'inactive', 'failed', etc., or 'unknown' on error."""
    try:
        r = subprocess.run(
            ['systemctl', 'is-active', ENGINE_UNIT],
            capture_output=True, text=True, timeout=10,
        )
        # is-active exits non-zero when not active but still prints the state.
        return (r.stdout.strip() or r.stderr.strip() or 'unknown')
    except Exception as exc:
        return f'unknown ({exc})'


def engine_activity(max_lines: int = 8) -> List[str]:
    """FIX 3: the last few journald lines for the engine unit, today only —
    replaces reading execution.log / main.log."""
    try:
        r = subprocess.run(
            ['journalctl', '-u', ENGINE_UNIT, '--since', 'today', '--no-pager'],
            capture_output=True, text=True, timeout=15,
        )
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        return lines[-max_lines:] if lines else ['(no journald entries today)']
    except Exception as exc:
        return [f'journald read failed: {exc}']


# ── Formatting ────────────────────────────────────────────────────────────────

def _sep(title: str = '') -> str:
    if title:
        pad  = (58 - len(title)) // 2
        return f"\n{'=' * pad} {title} {'=' * (58 - pad - len(title))}"
    return f"\n{'=' * 60}"


def _pnl_str(pnl: float) -> str:
    sign = '+' if pnl >= 0 else ''
    return f'{sign}${pnl:.2f}'


def _trade_line(name: str, trade: Optional[dict]) -> str:
    if trade is None:
        return f'  {name:<4}  no trade today'
    pnl  = float(trade.get('pnl', 0))
    return (
        f"  {name:<4}  {_pnl_str(pnl):<10}  "
        f"exit={trade.get('exit_reason', '?'):<14}  "
        f"VIX={float(trade.get('vix_entry') or 0):.1f}  "
        f"hold={trade.get('hold_minutes', 0)}m"
    )


def _best_worst(trades: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    if not trades:
        return None, None
    s = sorted(trades, key=lambda t: float(t.get('pnl', 0)), reverse=True)
    return s[0], s[-1]


def _kill_alerts(summary: dict) -> List[str]:
    lines = []
    for name in STRATEGIES:
        kf = summary.get('strategies', {}).get(name, {}).get('kill_flag', {})
        if kf.get('flag'):
            lines.append(f'  ⚠️  {name}: KILL CRITERIA — {kf["reason"]}')
    return lines


# ── Brief generation ──────────────────────────────────────────────────────────

def generate_brief(today_trades: List[dict], summary: dict) -> str:
    today_str   = date.today().strftime('%A, %B %d, %Y')
    by_strategy = {t['strategy']: t for t in today_trades}
    total_pnl   = sum(float(t.get('pnl', 0)) for t in today_trades)
    wins        = sum(1 for t in today_trades if float(t.get('pnl', 0)) > 0)
    n           = len(today_trades)
    lines       = [f'HERMES DAILY BRIEF — {today_str}']

    # ── Engine status (FIX 3) ─────────────────────────────────────────────────
    # Placed up top so it stays inside the trigger's 2800-char input trim.
    status = engine_status()
    icon   = '🟢' if status == 'active' else '🔴'
    lines.append(_sep('ENGINE'))
    lines.append(f'  {icon} {ENGINE_UNIT}: {status}')
    lines.append('  recent activity (journald, today):')
    for ln in engine_activity():
        lines.append(f'    {ln[:100]}')

    # ── Today's trades ────────────────────────────────────────────────────────
    lines.append(_sep('TODAY'))
    for name in STRATEGIES:
        lines.append(_trade_line(name, by_strategy.get(name)))
    wr_str = f'{wins}/{n}' if n else '0/0'
    lines.append(f'\n  Daily P&L: {_pnl_str(total_pnl)}  |  Win rate: {wr_str}')

    # ── Best / worst ──────────────────────────────────────────────────────────
    best, worst = _best_worst(today_trades)
    if best or worst:
        lines.append(_sep('BEST / WORST'))
        if best:
            lines.append(f"  Best:  {best['strategy']} {_pnl_str(float(best.get('pnl', 0)))} "
                         f"({best.get('exit_reason', '?')})")
        if worst and worst is not best:
            lines.append(f"  Worst: {worst['strategy']} {_pnl_str(float(worst.get('pnl', 0)))} "
                         f"({worst.get('exit_reason', '?')})")

    # ── Historical stats ──────────────────────────────────────────────────────
    lines.append(_sep('HISTORICAL WIN RATES'))
    lines.append(f"  {'Strat':<6} {'WR':>6}  {'n':>5}  {'avg $':>8}  {'roll10':>7}  {'kill':>5}")
    lines.append('  ' + '-' * 52)
    for name in STRATEGIES:
        s = summary.get('strategies', {}).get(name, {})
        if s.get('no_data'):
            lines.append(f'  {name:<6} {"no data":>6}')
            continue
        t     = s.get('total', {})
        wr    = f"{t.get('wr', 0):.1%}" if t.get('wr') is not None else ' n/a'
        n_s   = str(t.get('n', 0))
        avg   = f"${t.get('avg_pnl', 0):.0f}"
        roll  = f"{s.get('rolling_10_wr', 0):.1%}" if s.get('rolling_10_wr') is not None else ' n/a'
        flag  = '⚠️ ' if s.get('kill_flag', {}).get('flag') else '  '
        lines.append(f'  {name:<6} {wr:>6}  {n_s:>5}  {avg:>8}  {roll:>7}  {flag}')

    # ── Kill flags ────────────────────────────────────────────────────────────
    alerts = _kill_alerts(summary)
    lines.append(_sep('KILL CRITERIA'))
    if alerts:
        lines.extend(alerts)
    else:
        lines.append('  All strategies within acceptable ranges.')

    # ── VIX sweet spots ───────────────────────────────────────────────────────
    lines.append(_sep('VIX SWEET SPOTS'))
    for name in STRATEGIES:
        s      = summary.get('strategies', {}).get(name, {})
        by_vix = s.get('by_vix_bucket', {})
        valid  = {k: v for k, v in by_vix.items() if v.get('n', 0) >= 3}
        if not valid:
            continue
        best_b = max(valid.items(), key=lambda kv: kv[1].get('wr') or 0)
        bk     = best_b[1]
        lines.append(f"  {name:<4}: best VIX {best_b[0]} → WR={bk.get('wr', 0):.1%} (n={bk.get('n', 0)})")

    # ── Day-of-week sweet spots ───────────────────────────────────────────────
    lines.append(_sep('DAY-OF-WEEK NOTES'))
    for name in STRATEGIES:
        s      = summary.get('strategies', {}).get(name, {})
        by_day = s.get('by_day_of_week', {})
        valid  = {k: v for k, v in by_day.items() if v.get('n', 0) >= 3}
        if not valid:
            continue
        best_d = max(valid.items(), key=lambda kv: kv[1].get('wr') or 0)
        bk     = best_d[1]
        lines.append(f"  {name:<4}: best day {best_d[0]} → WR={bk.get('wr', 0):.1%} (n={bk.get('n', 0)})")

    # ── Slippage ──────────────────────────────────────────────────────────────
    lines.append(_sep('FILL SLIPPAGE'))
    for name in STRATEGIES:
        s    = summary.get('strategies', {}).get(name, {})
        slip = s.get('slippage', {})
        if slip.get('n', 0) >= 3:
            lines.append(f"  {name:<4}: avg slippage {slip.get('avg_pct', 0):.1f}% (n={slip['n']})")

    lines.append(_sep())
    lines.append(f'Generated: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    lines.append('=' * 60)
    return '\n'.join(lines)


def main() -> None:
    today_trades = load_today()
    summary      = load_summary()
    brief        = generate_brief(today_trades, summary)
    BRIEF_PATH.write_text(brief)
    print(brief)
    print(f'\nBrief saved → {BRIEF_PATH}')


if __name__ == '__main__':
    main()
