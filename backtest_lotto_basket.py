#!/usr/bin/env python3
"""
backtest_lotto_basket.py
=========================
0DTE "lotto basket" backtest: each trading day, buy a small basket of cheap,
far-OTM SPY 0DTE calls and puts at market open, hold to expiration (let them
expire worthless or take intrinsic settlement value), and measure aggregate
expectancy across the period.

Rules (per day):
  - At 9:35 AM ET, pull that day's 0DTE (same-day expiration) SPY option chain.
  - On each side (calls, puts) independently, select the N contracts whose
    entry ask is in [$0.05, $0.15] that are closest to the money (ATM proxy
    derived from put-call parity on the chain itself -- no paid stock feed
    needed). Tested for N=3 and N=5 per side (6 or 10 contracts/day).
  - Buy 1 contract of each at the ask. Hold to expiration (no exits).
  - Final value = ThetaData EOD close price for that contract on the
    expiration date itself (worthless -> ~$0.01 tick, ITM -> real EOD value).

Data source: ThetaData (THETADATA_USERNAME/PASSWORD from .env), real EOD +
at-time quotes only -- no synthetic pricing.

Output:
  backtest_lotto_basket_report.txt   -- summary stats for N=3 and N=5
  backtest_lotto_basket_daily.csv    -- one row per trading day per N-config
  backtest_lotto_basket_legs.csv     -- one row per selected contract leg
"""

import csv
import pickle
import sys
import time as _time
import traceback
from datetime import date, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─── paths ──────────────────────────────────────────────────────────────────
ENV_PATH    = Path(r'C:\Users\sagla\.tastytrade-mcp\.env')
DATA_DIR    = Path(r'C:\Users\sagla\backtest_data')
REPORT_PATH = Path(r'C:\Users\sagla\backtest_lotto_basket_report.txt')
DAILY_CSV   = Path(r'C:\Users\sagla\backtest_lotto_basket_daily.csv')
LEGS_CSV    = Path(r'C:\Users\sagla\backtest_lotto_basket_legs.csv')
LOG_PATH    = Path(r'C:\Users\sagla\backtest_lotto_basket_progress.log')

DATA_DIR.mkdir(parents=True, exist_ok=True)
# All pickle files below are self-written local caches of ThetaData responses
# (no external/untrusted source) -- consistent with backtest_real_data.py.

# ─── backtest window ────────────────────────────────────────────────────────
BT_START = date(2023, 7, 1)
BT_END   = date(2026, 5, 31)

ENTRY_TIME   = dtime(9, 35)
PRICE_LO     = 0.05
PRICE_HI     = 0.15
N_CONFIGS    = [3, 5]
WORTHLESS_MAX = 0.01     # <= this per-contract close counts as "expired worthless"
BIG_DAY_MULT  = 5.0      # payout > 5x cost => "big" day
CONTRACT_MULT = 100


def _log(msg: str) -> None:
    line = f'[{_time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    with LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


# ══════════════════════════════════════════════════════════════════════════
# 1. ENV + THETADATA CLIENT
# ══════════════════════════════════════════════════════════════════════════

def _load_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


_client = None


def get_client():
    global _client
    if _client is None:
        from thetadata import ThetaClient
        env = _load_env()
        email = env.get('THETADATA_USERNAME')
        password = env.get('THETADATA_PASSWORD')
        if not email or not password:
            raise RuntimeError('THETADATA_USERNAME / THETADATA_PASSWORD missing from .env')
        _client = ThetaClient(email=email, password=password, dataframe_type='pandas')
        _log('ThetaData client connected.')
    return _client


# ══════════════════════════════════════════════════════════════════════════
# 2. EXPIRATIONS (reuse existing cache -- read only)
# ══════════════════════════════════════════════════════════════════════════

def get_trading_dates() -> List[date]:
    cache = DATA_DIR / 'theta_SPY_expirations.pkl'
    if not cache.exists():
        client = get_client()
        df = client.option_list_expirations('SPY')
        exps = sorted(date.fromisoformat(str(x)[:10]) for x in df['expiration'].tolist())
        with cache.open('wb') as f:
            pickle.dump(exps, f)
    else:
        with cache.open('rb') as f:
            exps = pickle.load(f)
    return [d for d in exps if BT_START <= d <= BT_END]


# ══════════════════════════════════════════════════════════════════════════
# 3. PER-DAY CHAIN FETCH (entry @ 9:35 + EOD close), disk-cached per day
# ══════════════════════════════════════════════════════════════════════════

def _slim_rows(df) -> Dict[Tuple[float, str], dict]:
    """{(strike, right): {'bid':, 'ask':, 'close':}}  right is 'C'/'P'."""
    out = {}
    for row in df.itertuples(index=False):
        try:
            strike = float(row.strike)
            right = str(row.right).upper()[0]
            bid = float(getattr(row, 'bid', 0) or 0)
            ask = float(getattr(row, 'ask', 0) or 0)
            close = float(getattr(row, 'close', 0) or 0)
            out[(strike, right)] = {'bid': bid, 'ask': ask, 'close': close}
        except Exception:
            continue
    return out


def get_entry_chain(d: date) -> Dict[Tuple[float, str], dict]:
    cache = DATA_DIR / f'lotto_entry_SPY_{d}.pkl'
    if cache.exists():
        with cache.open('rb') as f:
            return pickle.load(f)
    client = get_client()
    try:
        df = client.option_at_time_quote(
            symbol='SPY', start_date=d, end_date=d, time_of_day=ENTRY_TIME,
            expiration=d, strike='*', right='both')
        result = _slim_rows(df) if df is not None and len(df) else {}
    except Exception as e:
        _log(f'  entry fetch FAILED {d}: {e}')
        result = {}
    with cache.open('wb') as f:
        pickle.dump(result, f)
    return result


def get_eod_chain(d: date) -> Dict[Tuple[float, str], dict]:
    cache = DATA_DIR / f'lotto_eod_SPY_{d}.pkl'
    if cache.exists():
        with cache.open('rb') as f:
            return pickle.load(f)
    client = get_client()
    try:
        df = client.option_history_eod(
            start_date=d, end_date=d, symbol='SPY', expiration=d,
            strike='*', right='both')
        result = _slim_rows(df) if df is not None and len(df) else {}
    except Exception as e:
        _log(f'  eod fetch FAILED {d}: {e}')
        result = {}
    with cache.open('wb') as f:
        pickle.dump(result, f)
    return result


# ══════════════════════════════════════════════════════════════════════════
# 4. ATM PROXY (put-call parity on the entry chain -- no paid stock feed)
# ══════════════════════════════════════════════════════════════════════════

def estimate_spot(entry: Dict[Tuple[float, str], dict]) -> Optional[float]:
    calls = {k[0]: v for k, v in entry.items() if k[1] == 'C' and v['bid'] > 0 and v['ask'] > 0}
    puts = {k[0]: v for k, v in entry.items() if k[1] == 'P' and v['bid'] > 0 and v['ask'] > 0}
    common = sorted(set(calls) & set(puts))
    if not common:
        return None
    best_k, best_diff = None, None
    for k in common:
        cm = (calls[k]['bid'] + calls[k]['ask']) / 2.0
        pm = (puts[k]['bid'] + puts[k]['ask']) / 2.0
        diff = abs(cm - pm)
        if best_diff is None or diff < best_diff:
            best_diff, best_k = diff, k
    return best_k


# ══════════════════════════════════════════════════════════════════════════
# 5. LEG SELECTION
# ══════════════════════════════════════════════════════════════════════════

def select_legs(entry: Dict[Tuple[float, str], dict], spot: float,
                 right: str, n: int) -> List[Tuple[float, str, float]]:
    """Returns up to n (strike, right, ask) closest to spot with ask in band."""
    cands = []
    for (k, r), v in entry.items():
        if r != right:
            continue
        ask = v['ask']
        if PRICE_LO <= ask <= PRICE_HI:
            cands.append((abs(k - spot), k, ask))
    cands.sort(key=lambda x: x[0])
    return [(k, right, ask) for _, k, ask in cands[:n]]


# ══════════════════════════════════════════════════════════════════════════
# 6. MAIN BACKTEST LOOP
# ══════════════════════════════════════════════════════════════════════════

def run():
    LOG_PATH.write_text('', encoding='utf-8')
    dates = get_trading_dates()
    _log(f'{len(dates)} trading days in [{BT_START}, {BT_END}]')

    daily_rows = []     # one row per (date, n_config)
    leg_rows = []        # one row per selected contract leg
    skipped = {'no_entry': 0, 'no_spot': 0, 'no_eod': 0}

    for i, d in enumerate(dates):
        entry = get_entry_chain(d)
        if not entry:
            skipped['no_entry'] += 1
            continue
        spot = estimate_spot(entry)
        if spot is None:
            skipped['no_spot'] += 1
            continue
        eod = get_eod_chain(d)
        if not eod:
            skipped['no_eod'] += 1
            continue

        # Build the full sorted candidate list once (largest N), then slice per config.
        max_n = max(N_CONFIGS)
        call_pool = select_legs(entry, spot, 'C', max_n)
        put_pool = select_legs(entry, spot, 'P', max_n)

        for n in N_CONFIGS:
            legs = call_pool[:n] + put_pool[:n]
            if not legs:
                continue
            cost = 0.0
            payout = 0.0
            for strike, right, ask in legs:
                q = eod.get((strike, right))
                close = q['close'] if q else 0.0
                leg_cost = round(ask * CONTRACT_MULT, 2)
                leg_payout = round(close * CONTRACT_MULT, 2)
                cost += leg_cost
                payout += leg_payout
                leg_rows.append({
                    'date': d.isoformat(), 'n_config': n, 'right': right,
                    'strike': strike, 'entry_ask': ask, 'eod_close': close,
                    'cost': leg_cost, 'payout': leg_payout,
                    'net': round(leg_payout - leg_cost, 2),
                    'worthless': close <= WORTHLESS_MAX,
                })
            net = round(payout - cost, 2)
            all_worthless = all(eod.get((k, r), {'close': 0})['close'] <= WORTHLESS_MAX
                                 for k, r, _ in legs)
            big_day = payout > BIG_DAY_MULT * cost if cost > 0 else False
            daily_rows.append({
                'date': d.isoformat(), 'n_config': n, 'num_legs': len(legs),
                'cost': round(cost, 2), 'payout': round(payout, 2), 'net': net,
                'all_worthless': all_worthless, 'big_day': big_day,
            })

        if (i + 1) % 25 == 0:
            _log(f'  ...{i + 1}/{len(dates)} days processed')

    _log(f'Done fetching. skipped={skipped}')

    # ── write leg-level CSV ──
    if leg_rows:
        with LEGS_CSV.open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(leg_rows[0].keys()))
            w.writeheader()
            w.writerows(leg_rows)

    # ── write daily CSV ──
    if daily_rows:
        with DAILY_CSV.open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(daily_rows[0].keys()))
            w.writeheader()
            w.writerows(daily_rows)

    # ── summary stats per N-config ──
    report_lines = []
    report_lines.append('0DTE LOTTO BASKET BACKTEST')
    report_lines.append(f'Period: {BT_START} to {BT_END}   Entry: {ENTRY_TIME} ET')
    report_lines.append(f'Price band: ${PRICE_LO:.2f}-${PRICE_HI:.2f} ask, per side, closest-to-ATM')
    report_lines.append(f'Skipped days: {skipped}')
    report_lines.append('=' * 78)

    for n in N_CONFIGS:
        rows = [r for r in daily_rows if r['n_config'] == n]
        if not rows:
            report_lines.append(f'\n--- N={n} per side: NO DATA ---')
            continue
        days = len(rows)
        total_cost = sum(r['cost'] for r in rows)
        total_payout = sum(r['payout'] for r in rows)
        net_pnl = total_payout - total_cost
        win_days = sum(1 for r in rows if r['net'] > 0)
        win_rate = 100.0 * win_days / days
        avg_day_pnl = net_pnl / days
        zero_days = sum(1 for r in rows if r['all_worthless'])
        big_days = sum(1 for r in rows if r['big_day'])
        some_days = days - zero_days - big_days
        avg_legs = sum(r['num_legs'] for r in rows) / days

        report_lines.append(f'\n--- N={n} per side ({int(round(avg_legs))} contracts/day target) ---')
        report_lines.append(f'  Trading days:        {days}')
        report_lines.append(f'  Avg legs filled/day: {avg_legs:.2f}')
        report_lines.append(f'  Total spent:         ${total_cost:,.2f}')
        report_lines.append(f'  Total returned:      ${total_payout:,.2f}')
        report_lines.append(f'  Net P&L:             ${net_pnl:,.2f}')
        report_lines.append(f'  Win rate (days):     {win_rate:.1f}%  ({win_days}/{days})')
        report_lines.append(f'  Avg day P&L:         ${avg_day_pnl:.2f}')
        report_lines.append(f'  Zero-payout days:    {zero_days} ({100 * zero_days / days:.1f}%)  -- all legs worthless')
        report_lines.append(f'  Some-payout days:    {some_days} ({100 * some_days / days:.1f}%)  -- partial recovery, not big')
        report_lines.append(f'  Big-payout days:     {big_days} ({100 * big_days / days:.1f}%)  -- payout > {BIG_DAY_MULT:.0f}x day cost')

    report_txt = '\n'.join(report_lines)
    REPORT_PATH.write_text(report_txt, encoding='utf-8')
    _log('Report written to ' + str(REPORT_PATH))
    print('\n' + report_txt)


if __name__ == '__main__':
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
