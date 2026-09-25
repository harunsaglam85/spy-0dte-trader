#!/usr/bin/env python3
"""
_amd_fade_breakout.py
======================
AMD (Accumulation-Manipulation-Distribution) model backtest: fade the
breakout instead of following it. Full parameter grid, real ThetaData
weekly-expiration fills.

Signal:
 1. Accumulation range - 3 variants:
    (a) am15       9:30-9:45 ET opening range
    (b) am30       9:30-10:00 ET opening range
    (c) overnight  prior day 16:00 ET close -> today 9:30 ET open, using
                   premarket 1-min high/low if bars exist in that window,
                   else both bounds = prior day's last 1-min close.
                   (Our cached 1-min bars are RTH-only [09:30-16:00], so in
                   practice this variant always falls back to the prior
                   close - confirmed empirically before writing this.)
 2. Manipulation - a 5-min candle sweeps beyond range high/low by a buffer
    ($0.05 / $0.15 / $0.30).
 3. Distribution/entry - enter OPPOSITE the sweep direction on the very next
    5-min candle if it confirms:
      - simple: closes back inside the range
      - strong: closes back inside AND its own (high-low) range >= 1.5x the
        average (high-low) of the 5 candles immediately before it
    If the next candle does NOT confirm (extends the sweep instead), it
    becomes the new manipulation candle and scanning continues from there
    (one signal per day, first qualifying setup wins).
 4. Stop - beyond the sweep extreme (max/min high/low across the manipulation
    candle AND the confirming candle), buffer $0.05 / $0.15.
 5. Target - fixed R:R off entry/stop risk: 1.5 / 2.5 / 3.5.
 6. Real ThetaData bid/ask fills, weekly expiration (3-5 DTE band, same as
    the OR/POC weekly work - h.WEEKLY_DTE_MIN/MAX), entry window 9:45 AM -
    3:00 PM ET, exit via stop/target/3:45 PM ET cutoff, walked on 1-min bars.

Reuses hermes_research_round2 (`h`) for data loading, the weekly-contract
ThetaData fetch/probe infrastructure (nearest_weekly_expiration,
find_weekly_contract, _nearest_prior_quote, _et), and calc_stats/fmt_pf.

Usage:
  python _amd_fade_breakout.py                    # full 2023-07-01..2026-05-30 grid
  python _amd_fade_breakout.py 2026-03-01 2026-05-30   # smoke-test a short window
"""
import sys, time, itertools, csv as _csv
from pathlib import Path
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_research_round2 as h

BASE_DIR = Path(__file__).resolve().parent
DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'

# ── Grid parameters ──────────────────────────────────────────────────────
RANGE_VARIANTS = ['am15', 'am30', 'overnight']
MANIP_BUFFERS  = [0.05, 0.15, 0.30]
CONFIRM_MODES  = ['simple', 'strong']
STOP_BUFFERS   = [0.05, 0.15]
TARGET_RRS     = [1.5, 2.5, 3.5]

ENTRY_WINDOW_START = (9, 45)
ENTRY_WINDOW_END   = (15, 0)
EXIT_CUTOFF        = (15, 45)
EXIT_CUTOFF_HMS    = '15:45:00'


def _et_hm(b) -> Tuple[int, int]:
    t = h._et(b)
    return (t.hour, t.minute)


# ── Accumulation range ───────────────────────────────────────────────────
def compute_range(variant: str, bars_1min: list, prev_close: Optional[float]):
    """Returns (range_high, range_low, range_end_hm) or None."""
    if variant == 'am15':
        win = [b for b in bars_1min if (9, 30) <= _et_hm(b) < (9, 45)]
        if len(win) < 10:
            return None
        return max(b['h'] for b in win), min(b['l'] for b in win), (9, 45)
    if variant == 'am30':
        win = [b for b in bars_1min if (9, 30) <= _et_hm(b) < (10, 0)]
        if len(win) < 20:
            return None
        return max(b['h'] for b in win), min(b['l'] for b in win), (10, 0)
    if variant == 'overnight':
        pre = [b for b in bars_1min if _et_hm(b) < (9, 30)]
        if pre:
            return max(b['h'] for b in pre), min(b['l'] for b in pre), (9, 30)
        if prev_close is None:
            return None
        return prev_close, prev_close, (9, 30)
    raise ValueError(variant)


# ── Signal detection ─────────────────────────────────────────────────────
def find_amd_signal(bars_1min: list, bars_5min: list, prev_close: Optional[float],
                     variant: str, buffer: float, confirm_mode: str) -> Optional[dict]:
    rng = compute_range(variant, bars_1min, prev_close)
    if rng is None:
        return None
    range_high, range_low, range_end_hm = rng

    n = len(bars_5min)
    i = 0
    while i < n:
        b = bars_5min[i]
        t = _et_hm(b)
        if t < range_end_hm:
            i += 1
            continue
        if t >= ENTRY_WINDOW_END:
            break

        swept_up   = b['h'] >= range_high + buffer
        swept_down = b['l'] <= range_low - buffer
        if swept_up == swept_down:   # neither, or both (ambiguous) - not a clean sweep
            i += 1
            continue
        if i + 1 >= n:
            break

        confirm = bars_5min[i + 1]
        ct = _et_hm(confirm)
        if ct < ENTRY_WINDOW_START or ct > ENTRY_WINDOW_END:
            i += 1
            continue

        if swept_up:
            inside    = confirm['c'] < range_high
            extreme   = max(b['h'], confirm['h'])
            direction = 'p'   # fade the upside sweep -> PUT
        else:
            inside    = confirm['c'] > range_low
            extreme   = min(b['l'], confirm['l'])
            direction = 'c'   # fade the downside sweep -> CALL

        if not inside:
            i += 1
            continue

        if confirm_mode == 'strong':
            prior5 = bars_5min[max(0, i - 5):i]
            if len(prior5) < 5:
                i += 1
                continue
            avg_range    = sum(x['h'] - x['l'] for x in prior5) / len(prior5)
            candle_range = confirm['h'] - confirm['l']
            if avg_range <= 0 or candle_range < 1.5 * avg_range:
                i += 1
                continue

        entry_spy      = confirm['c']
        entry_close_ts = h._et(confirm) + timedelta(minutes=5)
        entry_idx = next((k for k, bb in enumerate(bars_1min) if h._et(bb) >= entry_close_ts), None)
        if entry_idx is None:
            i += 1
            continue
        entry_dt = h._et(bars_1min[entry_idx])
        strike   = round(entry_spy / 0.5) * 0.5

        return {
            'bars': bars_1min, 'direction': direction, 'entry_idx': entry_idx,
            'entry_dt': entry_dt, 'entry_spy': entry_spy, 'strike': strike,
            'range_high': round(range_high, 2), 'range_low': round(range_low, 2),
            'sweep_extreme': round(extreme, 2),
        }
    return None


def build_amd_signals(spy_1min, spy_5min, spy_daily, dates, variant, buffer, confirm_mode, all_exps):
    signals = []
    coverage = {'total_days': 0, 'no_signal': 0, 'signal_days': 0, 'no_weekly_exp': 0}
    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < 100 or len(bars_5min) < 10:
            continue
        coverage['total_days'] += 1

        prev_ds    = h.prev_trading_day(ds, spy_daily)
        prev_bars  = spy_1min.get(prev_ds) if prev_ds else None
        prev_close = prev_bars[-1]['c'] if prev_bars else None

        sig = find_amd_signal(bars_1min, bars_5min, prev_close, variant, buffer, confirm_mode)
        if sig is None:
            coverage['no_signal'] += 1
            continue

        exp = h.nearest_weekly_expiration(ds, all_exps, h.WEEKLY_DTE_MIN, h.WEEKLY_DTE_MAX)
        if exp is None:
            coverage['no_weekly_exp'] += 1
            continue

        sig['ds']           = ds
        sig['variant']       = variant
        sig['buffer']        = buffer
        sig['confirm_mode']  = confirm_mode
        sig['expiration']    = exp
        sig['dte']           = (exp - date.fromisoformat(ds)).days
        coverage['signal_days'] += 1
        signals.append(sig)
    return signals, coverage


def fetch_amd_quotes(signals: List[dict], progress_every: int = 50):
    for n, sig in enumerate(signals, 1):
        right = 'C' if sig['direction'] == 'c' else 'P'
        strike, chain_list = h.find_weekly_contract(
            sig['ds'], sig['expiration'], sig['strike'], right,
            sig['entry_dt'].strftime('%H:%M:%S'), EXIT_CUTOFF_HMS)
        if chain_list:
            sig['strike'] = strike
            sig['chain']  = {r['hm']: r for r in chain_list}
        else:
            sig['chain'] = None
        if progress_every and n % progress_every == 0:
            print(f'    {n}/{len(signals)} fetched  '
                  f'(api_calls={h.THETA_API_CALLS}, api_time={h.THETA_API_SECONDS:.1f}s)')


# ── Trade simulation ─────────────────────────────────────────────────────
@dataclass
class AmdTrade:
    date:          str
    entry_date:    str
    entry_time:    str
    exit_time:     str
    direction:     str
    strike:        float
    expiration:    str
    dte:           int
    range_variant: str
    manip_buffer:  float
    confirm_mode:  str
    range_high:    float
    range_low:     float
    sweep_extreme: float
    entry_spy:     float
    stop_spy:      float
    target_spy:    float
    exit_spy:      float
    entry_option:  float
    exit_option:   float
    exit_reason:   str
    pnl:           float
    stop_buffer:   float
    target_rr:     float


def simulate_amd_trade(sig: dict, stop_buffer: float, target_rr: float) -> Optional[AmdTrade]:
    direction = sig['direction']
    entry_spy = sig['entry_spy']
    extreme   = sig['sweep_extreme']
    chain     = sig.get('chain')
    if not chain:
        return None

    if direction == 'c':
        stop_spy = extreme - stop_buffer
        if stop_spy >= entry_spy:
            return None
        risk       = entry_spy - stop_spy
        target_spy = entry_spy + target_rr * risk
    else:
        stop_spy = extreme + stop_buffer
        if stop_spy <= entry_spy:
            return None
        risk       = stop_spy - entry_spy
        target_spy = entry_spy - target_rr * risk
    if risk < 0.02:
        return None

    entry_hm  = sig['entry_dt'].strftime('%H:%M')
    entry_ask = h._nearest_prior_quote(chain, entry_hm, 'ask')
    if entry_ask is None or entry_ask <= 0:
        return None

    bars, entry_idx = sig['bars'], sig['entry_idx']
    exit_bar, exit_reason, exit_spy = None, 'EOD', None
    for b2 in bars[entry_idx + 1:]:
        t2 = h._et(b2)
        if (t2.hour, t2.minute) >= EXIT_CUTOFF:
            exit_bar, exit_reason, exit_spy = b2, '3:45 cutoff', b2['c']
            break
        if direction == 'c':
            if b2['l'] <= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (extreme-{stop_buffer:.2f})', stop_spy
                break
            if b2['h'] >= target_spy:
                exit_bar, exit_reason, exit_spy = b2, f'{target_rr:.1f}:1 target', target_spy
                break
        else:
            if b2['h'] >= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (extreme+{stop_buffer:.2f})', stop_spy
                break
            if b2['l'] <= target_spy:
                exit_bar, exit_reason, exit_spy = b2, f'{target_rr:.1f}:1 target', target_spy
                break
    if exit_bar is None:
        exit_bar, exit_reason, exit_spy = bars[-1], 'EOD', bars[-1]['c']
    exit_dt = h._et(exit_bar)

    exit_hm  = exit_dt.strftime('%H:%M')
    exit_bid = h._nearest_prior_quote(chain, exit_hm, 'bid')
    if exit_bid is None:
        return None

    pnl = round((exit_bid - entry_ask) * 100, 2)
    return AmdTrade(
        date=sig['ds'], entry_date=sig['ds'], entry_time=entry_hm, exit_time=exit_hm,
        direction=('CALL' if direction == 'c' else 'PUT'), strike=sig['strike'],
        expiration=sig['expiration'].isoformat(), dte=sig['dte'],
        range_variant=sig['variant'], manip_buffer=sig['buffer'], confirm_mode=sig['confirm_mode'],
        range_high=sig['range_high'], range_low=sig['range_low'], sweep_extreme=extreme,
        entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2), target_spy=round(target_spy, 2),
        exit_spy=round(exit_spy, 2), entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
        exit_reason=exit_reason, pnl=pnl, stop_buffer=stop_buffer, target_rr=target_rr)


def write_grid_csv(grid_results: Dict[tuple, List[AmdTrade]], path: Path):
    with path.open('w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['variant', 'buffer', 'confirm_mode', 'stop_buffer', 'target_rr',
                    'date', 'entry_time', 'exit_time', 'direction', 'strike', 'expiration', 'dte',
                    'range_high', 'range_low', 'sweep_extreme', 'entry_spy', 'stop_spy', 'target_spy',
                    'exit_spy', 'exit_reason', 'entry_option', 'exit_option', 'pnl'])
        for key, trades in grid_results.items():
            for t in trades:
                w.writerow([t.range_variant, t.manip_buffer, t.confirm_mode, t.stop_buffer, t.target_rr,
                            t.date, t.entry_time, t.exit_time, t.direction, t.strike, t.expiration, t.dte,
                            t.range_high, t.range_low, t.sweep_extreme, t.entry_spy, t.stop_spy, t.target_spy,
                            t.exit_spy, t.exit_reason, t.entry_option, t.exit_option, t.pnl])


# ── Validation (same bar as OR/POC: must clear both splits) ─────────────
def run_validation(trades: List[AmdTrade]):
    signal_dates = sorted({t.date for t in trades})
    n = len(signal_dates)
    split_idx = round(n * 0.70)
    is_dates  = set(signal_dates[:split_idx])
    oos_dates = set(signal_dates[split_idx:])
    is_trades  = [t for t in trades if t.date in is_dates]
    oos_trades = [t for t in trades if t.date in oos_dates]
    s_is, s_oos = h.calc_stats(is_trades), h.calc_stats(oos_trades)

    trades_sorted = sorted(trades, key=lambda t: (t.date, t.entry_time))
    even_trades = trades_sorted[0::2]
    odd_trades  = trades_sorted[1::2]
    s_even, s_odd = h.calc_stats(even_trades), h.calc_stats(odd_trades)

    print(f"\n{'='*90}\n  VALIDATION SPLITS on best combo\n{'='*90}")
    print(f"\n  -- Chronological 70/30 -- ({n} signal days, split={split_idx})")
    if n:
        print(f"  IS   ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})  "
              f"N={s_is['n']:>4}  WR={s_is['wr']:>6.1f}%  PF={h.fmt_pf(s_is):>6}  Total P&L=${s_is['total_pnl']:>+10,.2f}")
        print(f"  OOS  ({signal_dates[min(split_idx,n-1)]} -> {signal_dates[-1]})  "
              f"N={s_oos['n']:>4}  WR={s_oos['wr']:>6.1f}%  PF={h.fmt_pf(s_oos):>6}  Total P&L=${s_oos['total_pnl']:>+10,.2f}")

    print(f"\n  -- Interleaved odd/even -- ({len(trades_sorted)} trades)")
    print(f"  EVEN N={s_even['n']:>4}  WR={s_even['wr']:>6.1f}%  PF={h.fmt_pf(s_even):>6}  Total P&L=${s_even['total_pnl']:>+10,.2f}")
    print(f"  ODD  N={s_odd['n']:>4}  WR={s_odd['wr']:>6.1f}%  PF={h.fmt_pf(s_odd):>6}  Total P&L=${s_odd['total_pnl']:>+10,.2f}")

    print(f"\n  -- Pass/fail (PF>=1.0 and positive P&L in every subset) --")
    for label, s in [('IS', s_is), ('OOS', s_oos), ('EVEN', s_even), ('ODD', s_odd)]:
        passed = s['pf'] >= 1.0 and s['total_pnl'] > 0
        print(f"  {label:<5} PF={h.fmt_pf(s):>6}  P&L=${s['total_pnl']:>+9,.2f}  -> {'PASS' if passed else 'FAIL'}")
    all_pass = all(s['pf'] >= 1.0 and s['total_pnl'] > 0 for s in [s_is, s_oos, s_even, s_odd])
    print(f"\n  Clears all four splits: {'YES' if all_pass else 'NO'}")
    return all_pass


def main():
    date_lo, date_hi = DATE_LO, DATE_HI
    if len(sys.argv) >= 3:
        date_lo, date_hi = sys.argv[1], sys.argv[2]

    print("Loading market data...")
    spy_daily = h.load_spy_daily()
    spy_1min  = h.load_spy_1min()
    spy_5min  = h.load_spy_5min()
    all_dates_full = sorted(spy_daily.keys())
    dates = [d for d in all_dates_full if date_lo <= d <= date_hi]
    print(f"  {len(dates)} trading days in [{date_lo}, {date_hi}]")

    all_exps = h.get_all_theta_expirations()

    combos_18 = list(itertools.product(RANGE_VARIANTS, MANIP_BUFFERS, CONFIRM_MODES))
    total_combos = len(combos_18) * len(STOP_BUFFERS) * len(TARGET_RRS)
    print(f"\n{len(combos_18)} signal-detection configs x {len(STOP_BUFFERS)} stop-buffers x "
          f"{len(TARGET_RRS)} target R:Rs = {total_combos} total combos")

    grid_results: Dict[tuple, List[AmdTrade]] = {}
    t_start = time.time()
    for ci, (variant, buffer, confirm_mode) in enumerate(combos_18, 1):
        print(f"\n[{ci}/{len(combos_18)}] variant={variant}  buffer=${buffer:.2f}  confirm={confirm_mode}")
        signals, coverage = build_amd_signals(spy_1min, spy_5min, spy_daily, dates,
                                               variant, buffer, confirm_mode, all_exps)
        print(f"  coverage: {coverage}")
        if signals:
            fetch_amd_quotes(signals)
        for sd in STOP_BUFFERS:
            for rr in TARGET_RRS:
                trades = []
                for sig in signals:
                    t = simulate_amd_trade(sig, sd, rr)
                    if t is not None:
                        trades.append(t)
                grid_results[(variant, buffer, confirm_mode, sd, rr)] = trades
        elapsed = time.time() - t_start
        print(f"  running total: api_calls={h.THETA_API_CALLS}  api_time={h.THETA_API_SECONDS:.1f}s  "
              f"elapsed={elapsed/60:.1f}min")

    elapsed = time.time() - t_start
    print(f"\nTotal elapsed: {elapsed/60:.1f} min, {h.THETA_API_CALLS} ThetaData API calls "
          f"({h.THETA_API_SECONDS:.1f}s of API time)")

    ranked = []
    for key, trades in grid_results.items():
        s = h.calc_stats(trades)
        if s['n'] == 0:
            continue
        ranked.append((key, s))

    def pf_key(s):
        return (s['pf'] if s['pf'] != float('inf') else 1e9, s['total_pnl'])
    ranked.sort(key=lambda kv: pf_key(kv[1]), reverse=True)

    print(f"\n{'='*104}\n  TOP 10 BY PROFIT FACTOR\n{'='*104}")
    print(f"{'Variant':<11}{'Buf':>6}  {'Confirm':<7}{'StopBuf':>8}{'RR':>6}{'N':>6}{'WR':>8}{'PF':>8}{'Total P&L':>14}")
    for key, s in ranked[:10]:
        variant, buffer, confirm_mode, sd, rr = key
        print(f"{variant:<11}{buffer:>6.2f}  {confirm_mode:<7}{sd:>8.2f}{rr:>6.1f}{s['n']:>6}"
              f"{s['wr']:>7.1f}%{h.fmt_pf(s):>8}{s['total_pnl']:>+14,.2f}")

    out_csv = BASE_DIR / 'amd_grid_results.csv'
    write_grid_csv(grid_results, out_csv)
    print(f"\nWrote full grid -> {out_csv}")

    if ranked:
        best_key, best_s = ranked[0]
        best_trades = grid_results[best_key]
        print(f"\nBest combo: variant={best_key[0]} buffer=${best_key[1]:.2f} confirm={best_key[2]} "
              f"stop_buffer=${best_key[3]:.2f} target_rr={best_key[4]:.1f}")
        print(f"  N={best_s['n']} WR={best_s['wr']:.1f}% PF={h.fmt_pf(best_s)} Total P&L=${best_s['total_pnl']:+,.2f}")
        run_validation(best_trades)
    else:
        print("\nNo combo produced any trades - nothing to validate.")


if __name__ == '__main__':
    main()
