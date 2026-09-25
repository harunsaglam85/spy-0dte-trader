#!/usr/bin/env python3
"""
_momentum_continuation_v2.py
==============================
Improve the SPY momentum-continuation SHARE strategy (baseline:
_spy_momentum_continuation.py - buy at 10am if SPY is >=0.3% above the 9:30
open, exit 12pm; variant A no-stop PF=1.04 N=77, variant B -1% stop PF=1.18
N=77 - note the baseline B itself FAILS chronological OOS, PF=0.63) using
two levers:

  1. CROSS-SECTIONAL: at 10am, compare SPY/QQQ/IWM's move off their own
     9:30 open. Trade 100 shares of whichever is the single strongest
     positive mover, but only if it still clears the >=0.3% threshold.
     Skip the day if none qualify. (New Alpaca 1-min data fetched for QQQ/
     IWM this session, same range/format as the cached SPY data.)
  2. VOLUME FILTER: the same 0DTE-options-volume tercile split that
     validated OR/POC weekly today (_or_poc_volume_filter_validation.py
     methodology exactly - SPY 0DTE contract volume from cached
     theta_SPY_*.pkl chains, tercile over days that have a volume figure).
     Restrict entries to low+medium-volume days (exclude top tercile). This
     is a market-wide regime filter, applied regardless of which underlying
     is traded that day.

Grid: {no-stop, -1% stop} x {SPY-only, cross-sectional} x {unfiltered,
volume-filtered} = 8 cells. Same "price at HH:MM = open of the 1-min bar at
that timestamp" convention and same stop-fill convention (fills AT the stop
price, not the bar's low) as the baseline script.

Any cell that beats its same-stop-variant baseline (1.04 for no-stop cells,
1.18 for stop cells) gets BOTH validation splits (chronological 70/30 +
interleaved odd/even, must clear all four subsets) AND a leave-one-day-out
robustness check (does dropping any single trading day flip PF>=1 or
total P&L>0) - same discipline as the lotto-basket false-positive catch.
"""
import sys, csv
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
import pickle
from pathlib import Path

DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'
SHARES       = 100
ENTRY_THRESH = 0.003
STOP_PCT     = 0.01
DATA_DIR     = Path(r'C:\Users\sagla\backtest_data')

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
# Self-written caches from alpaca_loader.py's own fetch this session, not
# from an untrusted source - same convention as hermes_research_round2.py's
# other pickle.load calls (see its module docstring).
with (DATA_DIR / 'qqq_1min_alpaca_2023-07-01_2026-05-30.pkl').open('rb') as f:
    qqq_1min = pickle.load(f)
with (DATA_DIR / 'iwm_1min_alpaca_2023-07-01_2026-05-30.pkl').open('rb') as f:
    iwm_1min = pickle.load(f)
BARS = {'SPY': spy_1min, 'QQQ': qqq_1min, 'IWM': iwm_1min}

dates = [d for d in sorted(spy_daily.keys()) if DATE_LO <= d <= DATE_HI]
print(f"  {len(dates)} candidate days in [{DATE_LO}, {DATE_HI}]")


# ── same 0DTE-volume tercile filter as _or_poc_volume_filter_validation.py ──
def build_volume_lowmid_days():
    daily_vol = {}
    for ds in dates:
        chain = h._load_theta_file(ds)
        if not chain:
            continue
        v = sum(q.get('volume', 0) for k, q in chain.items() if k[0] == ds)
        if v > 0:
            daily_vol[ds] = v
    vol_sorted = sorted(daily_vol.items(), key=lambda kv: kv[1])
    n = len(vol_sorted)
    t2 = 2 * n // 3
    return set(d for d, v in vol_sorted[:t2])


VOL_LOWMID_DAYS = build_volume_lowmid_days()
print(f"  0DTE-volume low+medium days: {len(VOL_LOWMID_DAYS)} / {len(dates)}")


class ShareTrade:
    __slots__ = ('date', 'entry_date', 'symbol', 'entry_time', 'exit_time',
                 'entry_price', 'exit_price', 'exit_reason', 'move_pct', 'pnl')

    def __init__(self, date, symbol, entry_price, exit_price, exit_reason, move_pct):
        self.date = date
        self.entry_date = date
        self.symbol = symbol
        self.entry_time = '10:00'
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.move_pct = move_pct
        self.pnl = round((exit_price - entry_price) * SHARES, 2)


def bar_at_or_after(bars, hh, mm):
    for i, b in enumerate(bars):
        t = h._et(b)
        if (t.hour, t.minute) >= (hh, mm):
            return i, b
    return None, None


def symbol_move_at_10am(symbol, ds):
    """Returns (open_930, i1000, b1000, move_pct) or None if data's missing."""
    bars = BARS[symbol].get(ds, [])
    if not bars:
        return None
    open_930 = bars[0]['o']
    i1000, b1000 = bar_at_or_after(bars, 10, 0)
    if b1000 is None or open_930 <= 0:
        return None
    entry_price = b1000['o']
    return bars, open_930, i1000, entry_price, (entry_price - open_930) / open_930


def build_trades(cross_sectional: bool, use_stop: bool, volume_filter: bool):
    trades = []
    symbols = ['SPY', 'QQQ', 'IWM'] if cross_sectional else ['SPY']
    for ds in dates:
        if volume_filter and ds not in VOL_LOWMID_DAYS:
            continue

        candidates = []
        for sym in symbols:
            r = symbol_move_at_10am(sym, ds)
            if r is None:
                continue
            bars, open_930, i1000, entry_price, move_pct = r
            if move_pct >= ENTRY_THRESH:
                candidates.append((move_pct, sym, bars, i1000, entry_price))
        if not candidates:
            continue
        candidates.sort(reverse=True)   # strongest mover first
        move_pct, sym, bars, i1000, entry_price = candidates[0]

        i1200, b1200 = bar_at_or_after(bars, 12, 0)
        if b1200 is None:
            continue
        exit_price, exit_reason = b1200['o'], '12:00 close'

        if use_stop:
            stop_price = entry_price * (1 - STOP_PCT)
            for b2 in bars[i1000 + 1: i1200]:
                if b2['l'] <= stop_price:
                    exit_price, exit_reason = stop_price, 'stop (-1%)'
                    break

        trades.append(ShareTrade(ds, sym, entry_price, exit_price, exit_reason, move_pct))
    return trades


def four_split_validation(trades, label):
    if len(trades) < 8:
        print(f"    Too few trades ({len(trades)}) for a meaningful split - not validating.")
        return False
    signal_dates = sorted({t.date for t in trades})
    n_days = len(signal_dates)
    split_idx = round(n_days * 0.70)
    is_dates  = set(signal_dates[:split_idx])
    oos_dates = set(signal_dates[split_idx:])
    is_trades  = [t for t in trades if t.date in is_dates]
    oos_trades = [t for t in trades if t.date in oos_dates]
    s_is, s_oos = h.calc_stats(is_trades), h.calc_stats(oos_trades)

    trades_sorted = sorted(trades, key=lambda t: t.date)
    even_trades = trades_sorted[0::2]
    odd_trades  = trades_sorted[1::2]
    s_even, s_odd = h.calc_stats(even_trades), h.calc_stats(odd_trades)

    print(f"\n    -- Chronological 70/30 -- ({n_days} signal days, split={split_idx})")
    print(f"    IS   ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})  "
          f"N={s_is['n']:>4}  WR={s_is['wr']:>6.1f}%  PF={h.fmt_pf(s_is):>6}  Total P&L=${s_is['total_pnl']:>+9,.2f}")
    print(f"    OOS  ({signal_dates[min(split_idx,n_days-1)]} -> {signal_dates[-1]})  "
          f"N={s_oos['n']:>4}  WR={s_oos['wr']:>6.1f}%  PF={h.fmt_pf(s_oos):>6}  Total P&L=${s_oos['total_pnl']:>+9,.2f}")
    print(f"    -- Interleaved odd/even -- ({len(trades_sorted)} trades)")
    print(f"    EVEN N={s_even['n']:>4}  WR={s_even['wr']:>6.1f}%  PF={h.fmt_pf(s_even):>6}  Total P&L=${s_even['total_pnl']:>+9,.2f}")
    print(f"    ODD  N={s_odd['n']:>4}  WR={s_odd['wr']:>6.1f}%  PF={h.fmt_pf(s_odd):>6}  Total P&L=${s_odd['total_pnl']:>+9,.2f}")

    all_pass = True
    for lbl, s in [('IS', s_is), ('OOS', s_oos), ('EVEN', s_even), ('ODD', s_odd)]:
        passed = s['pf'] >= 1.0 and s['total_pnl'] > 0
        all_pass = all_pass and passed
        print(f"    {lbl:<5} PF={h.fmt_pf(s):>6}  P&L=${s['total_pnl']:>+9,.2f}  -> {'PASS' if passed else 'FAIL'}")
    print(f"    Clears both validation splits: {'YES' if all_pass else 'NO'}")
    return all_pass


def leave_one_day_out(trades, label):
    """For each signal day, recompute PF/total P&L with that day's trade(s)
    excluded. Flags whether removing ANY single day flips PF<1 or P&L<=0 -
    the same robustness check that caught the lotto-basket false positive."""
    by_day = {}
    for t in trades:
        by_day.setdefault(t.date, []).append(t)
    base_s = h.calc_stats(trades)
    worst_swing_pf = base_s['pf']
    worst_day = None
    flips = 0
    for ds in by_day:
        remaining = [t for t in trades if t.date != ds]
        s = h.calc_stats(remaining)
        if s['pf'] < 1.0 or s['total_pnl'] <= 0:
            flips += 1
            if worst_day is None or s['pf'] < worst_swing_pf:
                worst_swing_pf = s['pf']
                worst_day = ds
    n_days = len(by_day)
    print(f"\n    Leave-one-day-out ({n_days} distinct signal days): "
          f"{flips}/{n_days} single-day removals flip PF<1.0 or P&L<=0.")
    if flips > 0:
        print(f"    Most fragile day: {worst_day} (removing it alone drops PF to {worst_swing_pf:.2f})")
        print(f"    -> NOT robust - result depends on a small number of days, same pattern as the lotto-basket false positive.")
    else:
        print(f"    -> Robust to any single-day removal.")
    return flips == 0


BASELINE = {False: 1.04, True: 1.18}   # keyed by use_stop

print(f"\n{'='*100}\n  8-CELL GRID: {{no-stop, -1% stop}} x {{SPY-only, cross-sectional}} x {{unfiltered, volume-filtered}}\n{'='*100}")
results = {}
for cross in [False, True]:
    for stop in [False, True]:
        for vfilt in [False, True]:
            trades = build_trades(cross, stop, vfilt)
            s = h.calc_stats(trades)
            key = (cross, stop, vfilt)
            results[key] = (trades, s)
            label = (f"{'cross-sec' if cross else 'SPY-only ':<9} "
                     f"{'-1%stop' if stop else 'no-stop':<8} "
                     f"{'vol-filt' if vfilt else 'unfilt  '}")
            print(f"  {label}  N={s['n']:>4}  WR={s['wr']:>6.1f}%  PF={h.fmt_pf(s):>6}  Total P&L=${s['total_pnl']:>+9,.2f}")

print(f"\n{'='*100}\n  CELLS THAT BEAT THEIR BASELINE (no-stop vs PF 1.04, -1%-stop vs PF 1.18)\n{'='*100}")
any_beats = False
validated_cells = []
for (cross, stop, vfilt), (trades, s) in results.items():
    if s['n'] == 0:
        continue
    baseline_pf = BASELINE[stop]
    if s['pf'] > baseline_pf:
        any_beats = True
        label = f"{'cross-sec' if cross else 'SPY-only'} / {'-1% stop' if stop else 'no-stop'} / {'vol-filtered' if vfilt else 'unfiltered'}"
        print(f"\n  >> {label}  PF={h.fmt_pf(s)} (beats {baseline_pf:.2f})  N={s['n']}  Total P&L=${s['total_pnl']:+,.2f}")
        validated = four_split_validation(trades, label)
        robust = False
        if validated:
            robust = leave_one_day_out(trades, label)
        if validated and robust:
            validated_cells.append((label, s))

print(f"\n{'='*100}\n  FINAL VERDICT\n{'='*100}")
if not any_beats:
    print("  No cell beat its baseline PF. Nothing to validate - baseline stands as-is (and note the")
    print("  baseline itself already fails chronological OOS, so this strategy has no validated form today.")
elif not validated_cells:
    print("  At least one cell beat baseline on raw PF, but NONE cleared both validation splits AND the")
    print("  leave-one-day-out robustness check. Nothing here should be reported as working.")
else:
    print("  Validated AND robust:")
    for label, s in validated_cells:
        print(f"    - {label}: PF={h.fmt_pf(s)}, N={s['n']}, Total P&L=${s['total_pnl']:+,.2f}")
