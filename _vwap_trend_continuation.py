#!/usr/bin/env python3
"""
_vwap_trend_continuation.py
============================
My own strategy, built from everything learned so far in this project:

  - Blind buy-and-hold has no edge (lotto basket, rejected).
  - Fading a false breakout has edge, but only with a wide R:R and a low win
    rate (AMD fade-breakout: PF 1.19 at WR 28%, best cell used RR=3.5).
  - Following the opening-range breakout (not fading it) was the prior best
    performer (OR/POC weekly work).
  - Real ThetaData weekly-expiration fills consistently beat 0DTE (tighter
    spread as % of premium) - every validated strategy so far uses weekly.
  - Realized-vol regime filtering (top-tercile / above-median) improved the
    OR/POC weekly results without new data.

Everything tried so far is either "fade the extreme" (AMD, VWAP reversion)
or "follow the breakout" (OR/POC). Neither is a trend-DAY continuation play.
This is: establish a directional bias from the first 30 minutes vs VWAP,
then buy the first pullback-to-VWAP-and-reclaim in that direction - i.e.
trade WITH the day's established trend, using VWAP as dynamic support/
resistance, instead of trading the open's first move.

Signal:
 1. Bias: at the first 5-min candle closing at/after 10:00 ET, compare its
    close to the causal cumulative VWAP (1-min bars from the 9:30 open, no
    lookahead) at that time. Price > VWAP + $0.02 -> bullish (calls) bias.
    Price < VWAP - $0.02 -> bearish (puts) bias. A dead-even tie skips the
    day (rare).
 2. Pullback: after the bias bar, the first 5-min candle whose low comes
    within `tolerance` of VWAP (bullish: low <= vwap+tolerance) or whose
    high comes within `tolerance` of VWAP (bearish: high >= vwap-tolerance).
    tolerance tested at $0.10 / $0.25.
 3. Reclaim/entry: the VERY NEXT 5-min candle, entered at its close, IF it
    closes back beyond VWAP in the bias direction by >= tolerance (bullish:
    close >= vwap+tolerance; bearish: close <= vwap-tolerance). If it
    doesn't reclaim, it becomes the new pullback candle and scanning
    continues (same cascading logic as the AMD script) - one signal/day.
 4. Stop: beyond the pullback extreme (min/max low or high across the
    pullback AND reclaim candles), buffer $0.05 / $0.15.
 5. Target: fixed R:R off entry/stop risk: 1.5 / 2.5 / 3.5 (same grid as
    AMD/OR-POC for apples-to-apples comparison).
 6. Real ThetaData bid/ask fills, weekly expiration (3-5 DTE band, same as
    the OR/POC and AMD work), 2023-07-01 to 2026-05-30, entry window
    10:00 AM (earliest possible: bias+pullback+reclaim) - 3:00 PM ET, exit
    via stop/target/3:45 PM ET cutoff, walked on 1-min bars.

After finding the best raw combo, also tests it under the realized-vol
top-tercile filter that helped OR/POC weekly (pure post-hoc filter, no new
API calls), then runs the same two validation splits used for every prior
strategy here (chronological 70/30 IS/OOS + interleaved odd/even) - must
clear both to count as validated.

Reuses hermes_research_round2 (`h`) for data loading, calc_vwap_bands /
_nearest_prior_vwap, the weekly-contract ThetaData fetch/probe
infrastructure, and calc_stats/fmt_pf.
"""
import sys, time, itertools, math, csv as _csv
from pathlib import Path
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_research_round2 as h

BASE_DIR = Path(__file__).resolve().parent
DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'

# ── Grid parameters ──────────────────────────────────────────────────────
TOLERANCES   = [0.10, 0.25]
STOP_BUFFERS = [0.05, 0.15]
TARGET_RRS   = [1.5, 2.5, 3.5]

BIAS_TIME           = (10, 0)
ENTRY_WINDOW_END    = (15, 0)
EXIT_CUTOFF         = (15, 45)
EXIT_CUTOFF_HMS     = '15:45:00'
BIAS_MIN_SEP        = 0.02


def _et_hm(b) -> Tuple[int, int]:
    t = h._et(b)
    return (t.hour, t.minute)


# ── Signal detection ─────────────────────────────────────────────────────
def find_vtc_signal(bars_1min: list, bars_5min: list, tolerance: float) -> Optional[dict]:
    vwap_map = h.calc_vwap_bands(bars_1min)
    if not vwap_map:
        return None

    def vwap_at(bar5):
        end_min = h._et(bar5) + timedelta(minutes=4)
        band = h._nearest_prior_vwap(vwap_map, end_min.strftime('%H:%M'))
        return band[0] if band else None

    n = len(bars_5min)
    bias_idx = next((i for i, b in enumerate(bars_5min) if _et_hm(b) >= BIAS_TIME), None)
    if bias_idx is None:
        return None
    bias_bar = bars_5min[bias_idx]
    vwap0 = vwap_at(bias_bar)
    if vwap0 is None:
        return None
    if bias_bar['c'] > vwap0 + BIAS_MIN_SEP:
        bias = 'c'
    elif bias_bar['c'] < vwap0 - BIAS_MIN_SEP:
        bias = 'p'
    else:
        return None

    i = bias_idx + 1
    while i < n:
        b = bars_5min[i]
        t = _et_hm(b)
        if t >= ENTRY_WINDOW_END:
            break
        vwap = vwap_at(b)
        if vwap is None:
            i += 1
            continue

        if bias == 'c':
            is_pullback = b['l'] <= vwap + tolerance
        else:
            is_pullback = b['h'] >= vwap - tolerance
        if not is_pullback:
            i += 1
            continue
        if i + 1 >= n:
            break

        confirm  = bars_5min[i + 1]
        ct       = _et_hm(confirm)
        if ct > ENTRY_WINDOW_END:
            i += 1
            continue
        vwap_c   = vwap_at(confirm)
        if vwap_c is None:
            i += 1
            continue

        if bias == 'c':
            reclaimed = confirm['c'] >= vwap_c + tolerance
            extreme   = min(b['l'], confirm['l'])
        else:
            reclaimed = confirm['c'] <= vwap_c - tolerance
            extreme   = max(b['h'], confirm['h'])

        if not reclaimed:
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
            'bars': bars_1min, 'direction': bias, 'entry_idx': entry_idx,
            'entry_dt': entry_dt, 'entry_spy': entry_spy, 'strike': strike,
            'vwap_bias': round(vwap0, 2), 'pullback_extreme': round(extreme, 2),
        }
    return None


def build_vtc_signals(spy_1min, spy_5min, dates, tolerance, all_exps):
    signals = []
    coverage = {'total_days': 0, 'no_signal': 0, 'signal_days': 0, 'no_weekly_exp': 0}
    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < 100 or len(bars_5min) < 10:
            continue
        coverage['total_days'] += 1

        sig = find_vtc_signal(bars_1min, bars_5min, tolerance)
        if sig is None:
            coverage['no_signal'] += 1
            continue

        exp = h.nearest_weekly_expiration(ds, all_exps, h.WEEKLY_DTE_MIN, h.WEEKLY_DTE_MAX)
        if exp is None:
            coverage['no_weekly_exp'] += 1
            continue

        sig['ds']        = ds
        sig['tolerance'] = tolerance
        sig['expiration']= exp
        sig['dte']       = (exp - date.fromisoformat(ds)).days
        coverage['signal_days'] += 1
        signals.append(sig)
    return signals, coverage


def fetch_vtc_quotes(signals: List[dict], progress_every: int = 50):
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
class VtcTrade:
    date:             str
    entry_date:       str
    entry_time:       str
    exit_time:        str
    direction:        str
    strike:           float
    expiration:       str
    dte:              int
    tolerance:        float
    vwap_bias:        float
    pullback_extreme: float
    entry_spy:        float
    stop_spy:         float
    target_spy:       float
    exit_spy:         float
    entry_option:     float
    exit_option:      float
    exit_reason:      str
    pnl:              float
    stop_buffer:      float
    target_rr:        float


def simulate_vtc_trade(sig: dict, stop_buffer: float, target_rr: float) -> Optional[VtcTrade]:
    direction = sig['direction']
    entry_spy = sig['entry_spy']
    extreme   = sig['pullback_extreme']
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
    return VtcTrade(
        date=sig['ds'], entry_date=sig['ds'], entry_time=entry_hm, exit_time=exit_hm,
        direction=('CALL' if direction == 'c' else 'PUT'), strike=sig['strike'],
        expiration=sig['expiration'].isoformat(), dte=sig['dte'], tolerance=sig['tolerance'],
        vwap_bias=sig['vwap_bias'], pullback_extreme=extreme,
        entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2), target_spy=round(target_spy, 2),
        exit_spy=round(exit_spy, 2), entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
        exit_reason=exit_reason, pnl=pnl, stop_buffer=stop_buffer, target_rr=target_rr)


def write_grid_csv(grid_results: Dict[tuple, List[VtcTrade]], path: Path):
    with path.open('w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['tolerance', 'stop_buffer', 'target_rr', 'date', 'entry_time', 'exit_time',
                    'direction', 'strike', 'expiration', 'dte', 'vwap_bias', 'pullback_extreme',
                    'entry_spy', 'stop_spy', 'target_spy', 'exit_spy', 'exit_reason',
                    'entry_option', 'exit_option', 'pnl'])
        for key, trades in grid_results.items():
            for t in trades:
                w.writerow([t.tolerance, t.stop_buffer, t.target_rr, t.date, t.entry_time, t.exit_time,
                            t.direction, t.strike, t.expiration, t.dte, t.vwap_bias, t.pullback_extreme,
                            t.entry_spy, t.stop_spy, t.target_spy, t.exit_spy, t.exit_reason,
                            t.entry_option, t.exit_option, t.pnl])


# ── Realized-vol regime filter (same measure/method as the OR/POC RV work) ─
def build_rolling_rv(spy_daily, all_dates_full, window=20):
    rv = {}
    rets = []
    prev_close = None
    for d in all_dates_full:
        bar = spy_daily.get(d)
        if not bar:
            continue
        c = bar['close']
        if prev_close and prev_close > 0:
            rets.append(math.log(c / prev_close))
        prev_close = c
        if len(rets) >= window:
            w = rets[-window:]
            mu = sum(w) / window
            var = sum((x - mu) ** 2 for x in w) / (window - 1)
            rv[d] = math.sqrt(var) * math.sqrt(252) * 100.0
    return rv


# ── Validation (same bar as every prior strategy here) ──────────────────
def run_validation(trades: List[VtcTrade], label: str) -> bool:
    signal_dates = sorted({t.date for t in trades})
    n = len(signal_dates)
    if n < 4:
        print(f"\n  Too few signal days ({n}) to validate {label}.")
        return False
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

    print(f"\n{'='*90}\n  VALIDATION SPLITS - {label}\n{'='*90}")
    print(f"\n  -- Chronological 70/30 -- ({n} signal days, split={split_idx})")
    print(f"  IS   ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})  "
          f"N={s_is['n']:>4}  WR={s_is['wr']:>6.1f}%  PF={h.fmt_pf(s_is):>6}  Total P&L=${s_is['total_pnl']:>+10,.2f}")
    print(f"  OOS  ({signal_dates[min(split_idx,n-1)]} -> {signal_dates[-1]})  "
          f"N={s_oos['n']:>4}  WR={s_oos['wr']:>6.1f}%  PF={h.fmt_pf(s_oos):>6}  Total P&L=${s_oos['total_pnl']:>+10,.2f}")

    print(f"\n  -- Interleaved odd/even -- ({len(trades_sorted)} trades)")
    print(f"  EVEN N={s_even['n']:>4}  WR={s_even['wr']:>6.1f}%  PF={h.fmt_pf(s_even):>6}  Total P&L=${s_even['total_pnl']:>+10,.2f}")
    print(f"  ODD  N={s_odd['n']:>4}  WR={s_odd['wr']:>6.1f}%  PF={h.fmt_pf(s_odd):>6}  Total P&L=${s_odd['total_pnl']:>+10,.2f}")

    print(f"\n  -- Pass/fail (PF>=1.0 and positive P&L in every subset) --")
    for lbl, s in [('IS', s_is), ('OOS', s_oos), ('EVEN', s_even), ('ODD', s_odd)]:
        passed = s['pf'] >= 1.0 and s['total_pnl'] > 0
        print(f"  {lbl:<5} PF={h.fmt_pf(s):>6}  P&L=${s['total_pnl']:>+9,.2f}  -> {'PASS' if passed else 'FAIL'}")
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

    total_combos = len(TOLERANCES) * len(STOP_BUFFERS) * len(TARGET_RRS)
    print(f"\n{len(TOLERANCES)} signal-detection configs (tolerance) x {len(STOP_BUFFERS)} stop-buffers x "
          f"{len(TARGET_RRS)} target R:Rs = {total_combos} total combos")

    grid_results: Dict[tuple, List[VtcTrade]] = {}
    t_start = time.time()
    for ci, tolerance in enumerate(TOLERANCES, 1):
        print(f"\n[{ci}/{len(TOLERANCES)}] tolerance=${tolerance:.2f}")
        signals, coverage = build_vtc_signals(spy_1min, spy_5min, dates, tolerance, all_exps)
        print(f"  coverage: {coverage}")
        if signals:
            fetch_vtc_quotes(signals)
        for sd in STOP_BUFFERS:
            for rr in TARGET_RRS:
                trades = []
                for sig in signals:
                    t = simulate_vtc_trade(sig, sd, rr)
                    if t is not None:
                        trades.append(t)
                grid_results[(tolerance, sd, rr)] = trades
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

    print(f"\n{'='*90}\n  ALL {len(ranked)} COMBOS BY PROFIT FACTOR\n{'='*90}")
    print(f"{'Tol':>6}{'StopBuf':>10}{'RR':>6}{'N':>6}{'WR':>8}{'PF':>8}{'Total P&L':>14}")
    for key, s in ranked:
        tol, sd, rr = key
        print(f"{tol:>6.2f}{sd:>10.2f}{rr:>6.1f}{s['n']:>6}{s['wr']:>7.1f}%{h.fmt_pf(s):>8}{s['total_pnl']:>+14,.2f}")

    out_csv = BASE_DIR / 'vtc_grid_results.csv'
    write_grid_csv(grid_results, out_csv)
    print(f"\nWrote full grid -> {out_csv}")

    if not ranked:
        print("\nNo combo produced any trades - nothing to validate.")
        return

    best_key, best_s = ranked[0]
    best_trades = grid_results[best_key]
    print(f"\nBest raw combo: tolerance=${best_key[0]:.2f} stop_buffer=${best_key[1]:.2f} target_rr={best_key[2]:.1f}")
    print(f"  N={best_s['n']} WR={best_s['wr']:.1f}% PF={h.fmt_pf(best_s)} Total P&L=${best_s['total_pnl']:+,.2f}")
    raw_pass = run_validation(best_trades, "RAW best combo (no regime filter)")

    # ── RV top-tercile filter on the best raw combo (no new API calls) ──
    print(f"\n{'='*90}\n  REALIZED-VOL TOP-TERCILE FILTER on best raw combo\n{'='*90}")
    rv_by_day = build_rolling_rv(spy_daily, all_dates_full, window=20)
    rv_in_range = sorted([(d, rv_by_day[d]) for d in dates if d in rv_by_day], key=lambda x: x[1])
    n_rv = len(rv_in_range)
    tercile_size = n_rv // 3
    top_tercile_days = set(d for d, v in rv_in_range[-tercile_size:]) if tercile_size else set()
    filtered_trades = [t for t in best_trades if t.date in top_tercile_days]
    s_filt = h.calc_stats(filtered_trades)
    print(f"  {n_rv} days with valid 20-day rolling RV; top tercile = {len(top_tercile_days)} days")
    print(f"  UNFILTERED  N={best_s['n']:>4}  WR={best_s['wr']:>6.1f}%  PF={h.fmt_pf(best_s):>6}  Total P&L=${best_s['total_pnl']:>+10,.2f}")
    print(f"  TOP TERCILE N={s_filt['n']:>4}  WR={s_filt['wr']:>6.1f}%  PF={h.fmt_pf(s_filt):>6}  Total P&L=${s_filt['total_pnl']:>+10,.2f}")

    filt_pass = False
    if s_filt['n'] >= 20:
        filt_pass = run_validation(filtered_trades, "RV top-tercile filtered")
    else:
        print(f"\n  Filtered N={s_filt['n']} too small to validate separately.")

    print(f"\n{'='*90}\n  FINAL VERDICT\n{'='*90}")
    print(f"  Raw best combo validated:         {'YES' if raw_pass else 'NO'}")
    print(f"  RV top-tercile filter validated:  {'YES' if filt_pass else 'NO'}")


if __name__ == '__main__':
    main()
