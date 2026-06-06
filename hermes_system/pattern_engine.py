#!/usr/bin/env python3
"""
Hermes Pattern Engine — Zero-token nightly trade analysis.
Reads all trade JSON files and computes per-strategy statistics.
Cron: 30 16 * * 1-5  (4:30 PM ET)
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import itertools
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERMES_ROOT  = Path('/root/hermes_system')
TRADES_DIR   = HERMES_ROOT / 'trades'
SUMMARY_PATH = HERMES_ROOT / 'pattern_summary.json'

STRATEGIES   = [
    # Confirmed (3 contracts each)
    'R3A', 'R3B', 'R3C', 'R3D', 'R3E', 'R8', 'R10', 'S4',
    # Experimental T-strategies (1 contract each)
    'T1_thursday_put', 'T2_monday_afternoon', 'T3_wednesday_afternoon', 'T4_friday_morning',
    'T5_tuesday_bear_call', 'T6_thursday_bear_call', 'T7_high_vix',
    'T8_delta_015', 'T9_delta_025', 'T10_wide_spread', 'T11_narrow_spread', 'T12_max_data',
    'T13_thursday_afternoon', 'T14_vix_transition',
]
VIX_BUCKETS  = [(13, 15), (15, 17), (17, 19), (19, 21), (21, 23)]
DAYS         = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
TIME_BUCKETS = ['09:30-10:00', '10:00-10:30', '10:30-11:00',
                '11:00-12:00', '12:00-13:00', '13:00-14:00']
REGIMES      = ['low_vol', 'normal', 'elevated', 'high_vol']

# Kill criteria thresholds
KILL_MIN_TRADES  = 10
KILL_MIN_WR      = 0.40
KILL_MAX_AVG_PNL = -30.0


# ── Data loading ──────────────────────────────────────────────────────────────

def load_all_trades() -> List[dict]:
    trades = []
    for f in sorted(TRADES_DIR.glob('*.json')):
        try:
            batch = json.loads(f.read_text())
            if isinstance(batch, list):
                trades.extend(batch)
        except Exception as exc:
            print(f'Warning: could not parse {f.name}: {exc}')
    return trades


# ── Core stats ────────────────────────────────────────────────────────────────

def _stats(trades: List[dict]) -> dict:
    if not trades:
        return {
            'n': 0, 'wins': 0, 'wr': None,
            'avg_pnl': None, 'avg_pnl_per_contract': None, 'total_pnl': 0.0,
        }
    wins      = sum(1 for t in trades if t.get('pnl', 0) > 0)
    total_pnl = sum(t.get('pnl', 0) for t in trades)
    avg_pnl   = total_pnl / len(trades)
    # Per-contract P&L normalises confirmed (3c) vs experimental (1c) strategies
    per_contract = [
        t.get('realized_pnl_per_contract',
              t.get('pnl', 0) / max(int(t.get('contracts', 1)), 1))
        for t in trades
    ]
    avg_pnl_per_contract = sum(per_contract) / len(per_contract)
    return {
        'n':                    len(trades),
        'wins':                 wins,
        'wr':                   round(wins / len(trades), 3),
        'avg_pnl':              round(avg_pnl, 2),
        'avg_pnl_per_contract': round(avg_pnl_per_contract, 2),
        'total_pnl':            round(total_pnl, 2),
    }


def _time_bucket(entry_time_iso: str) -> str:
    try:
        t   = datetime.fromisoformat(entry_time_iso)
        hm  = t.hour * 60 + t.minute
        if hm <  10 * 60:           return '09:30-10:00'
        if hm <  10 * 60 + 30:      return '10:00-10:30'
        if hm <  11 * 60:           return '10:30-11:00'
        if hm <  12 * 60:           return '11:00-12:00'
        if hm <  13 * 60:           return '12:00-13:00'
        return '13:00-14:00'
    except Exception:
        return 'unknown'


def _vix_bucket_label(vix: float) -> str:
    for lo, hi in VIX_BUCKETS:
        if lo <= vix < hi:
            return f'{lo}-{hi}'
    return 'other'


# ── Rolling win rate ──────────────────────────────────────────────────────────

def _rolling_wr(trades: List[dict], n: int = 10) -> Optional[float]:
    if not trades:
        return None
    window = trades[-n:] if len(trades) >= n else trades
    wins   = sum(1 for t in window if t.get('pnl', 0) > 0)
    return round(wins / len(window), 3)


# ── Slippage analysis ─────────────────────────────────────────────────────────

def _slippage(trades: List[dict]) -> dict:
    valid = [
        t for t in trades
        if t.get('theoretical_mid') and abs(t['theoretical_mid']) > 0
        and t.get('entry_price') is not None
    ]
    if not valid:
        return {'n': 0, 'avg_pct': None}
    slippages = []
    for t in valid:
        theo = abs(float(t['theoretical_mid']))
        fill = abs(float(t['entry_price']))
        if theo > 0:
            slippages.append(abs(fill - theo) / theo * 100)
    return {
        'n':       len(slippages),
        'avg_pct': round(sum(slippages) / len(slippages), 2) if slippages else 0.0,
    }


# ── Kill criteria ─────────────────────────────────────────────────────────────

def _kill_flag(trades: List[dict]) -> dict:
    if len(trades) < KILL_MIN_TRADES:
        return {'flag': False, 'reason': f'insufficient_data (n={len(trades)})'}
    s = _stats(trades)
    if s['wr'] < KILL_MIN_WR:
        return {'flag': True, 'reason': f'WR {s["wr"]:.1%} below minimum {KILL_MIN_WR:.0%}'}
    if s['avg_pnl'] < KILL_MAX_AVG_PNL:
        return {'flag': True, 'reason': f'avg_pnl ${s["avg_pnl"]:.0f} below ${KILL_MAX_AVG_PNL:.0f}'}
    return {'flag': False, 'reason': 'ok'}


# ── Per-strategy analysis ─────────────────────────────────────────────────────

def _strategy_stats(trades: List[dict]) -> dict:
    result = {}
    for name in STRATEGIES:
        st = [t for t in trades if t.get('strategy') == name]
        if not st:
            result[name] = {'no_data': True}
            continue

        by_vix = {}
        for lo, hi in VIX_BUCKETS:
            label     = f'{lo}-{hi}'
            subset    = [t for t in st if t.get('vix_entry') is not None and lo <= float(t['vix_entry']) < hi]
            by_vix[label] = _stats(subset)

        by_day = {d: _stats([t for t in st if t.get('day_of_week') == d]) for d in DAYS}

        by_time = {b: _stats([t for t in st if _time_bucket(t.get('entry_time', '')) == b])
                   for b in TIME_BUCKETS}

        by_regime = {r: _stats([t for t in st if t.get('market_regime') == r]) for r in REGIMES}

        result[name] = {
            'total':            _stats(st),
            'by_vix_bucket':    by_vix,
            'by_day_of_week':   by_day,
            'by_entry_time':    by_time,
            'by_market_regime': by_regime,
            'slippage':         _slippage(st),
            'rolling_10_wr':    _rolling_wr(st, 10),
            'kill_flag':        _kill_flag(st),
        }
    return result


# ── Inter-strategy correlation ────────────────────────────────────────────────

def _correlation(trades: List[dict]) -> dict:
    daily: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        day   = (t.get('exit_time') or t.get('entry_time', ''))[:10]
        strat = t.get('strategy', '')
        if day and strat:
            daily[strat][day] += float(t.get('pnl', 0))

    active = [s for s in STRATEGIES if s in daily]
    result = {}
    for a, b in itertools.combinations(active, 2):
        shared = sorted(set(daily[a]) & set(daily[b]))
        if len(shared) < 5:
            continue
        xa = [daily[a][d] for d in shared]
        xb = [daily[b][d] for d in shared]
        n  = len(xa)
        mx, my = sum(xa) / n, sum(xb) / n
        num    = sum((xa[i] - mx) * (xb[i] - my) for i in range(n))
        denom  = (sum((v - mx) ** 2 for v in xa) ** 0.5) * (sum((v - my) ** 2 for v in xb) ** 0.5)
        corr   = round(num / denom, 3) if denom > 0 else 0.0
        result[f'{a}_vs_{b}'] = {'corr': corr, 'n_days': n}
    return result


# ── Exit reason breakdown ─────────────────────────────────────────────────────

def _exit_reasons(trades: List[dict]) -> dict:
    result = {}
    for name in STRATEGIES:
        st = [t for t in trades if t.get('strategy') == name]
        if not st:
            continue
        reasons: Dict[str, int] = defaultdict(int)
        for t in st:
            reasons[t.get('exit_reason', 'unknown')] += 1
        result[name] = dict(reasons)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    trades = load_all_trades()
    print(f'Loaded {len(trades)} trades from {TRADES_DIR}')

    summary = {
        'generated_at':  datetime.utcnow().isoformat() + 'Z',
        'total_trades':  len(trades),
        'strategies':    _strategy_stats(trades),
        'correlation':   _correlation(trades),
        'exit_reasons':  _exit_reasons(trades),
    }

    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, default=str))
    print(f'Pattern summary written to {SUMMARY_PATH}')

    # Print quick summary to stdout
    print('\n--- Quick Summary ---')
    confirmed    = {'R3A', 'R3B', 'R3C', 'R3D', 'R3E', 'R8', 'R10', 'S4'}
    print(f"  {'Strategy':<26} {'WR':>6} {'n':>5} {'avg$/trade':>11} {'avg$/c':>8} {'total$':>9} {'kill':>6}")
    print(f"  {'-'*26} {'-'*6} {'-'*5} {'-'*11} {'-'*8} {'-'*9} {'-'*6}")
    for name in STRATEGIES:
        s = summary['strategies'].get(name, {})
        tier = 'CFM' if name in confirmed else 'EXP'
        if s.get('no_data'):
            print(f"  {name:<26} [{tier}] no data")
            continue
        t  = s.get('total', {})
        kf = s.get('kill_flag', {})
        flag = 'KILL' if kf.get('flag') else 'ok'
        wr  = f"{t.get('wr', 0):.1%}" if t.get('wr') is not None else 'n/a'
        apc = f"${t.get('avg_pnl_per_contract', 0):+.0f}" if t.get('avg_pnl_per_contract') is not None else 'n/a'
        print(
            f"  {name:<26} {wr:>6} {t.get('n', 0):>5} "
            f"${t.get('avg_pnl', 0):>+10.0f} {apc:>8} "
            f"${t.get('total_pnl', 0):>+8.0f} {flag:>6}  [{tier}]"
        )


if __name__ == '__main__':
    main()
