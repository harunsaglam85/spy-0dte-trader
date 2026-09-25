#!/usr/bin/env python3
"""One-off: IS/OOS 70/30 split for R7-POC-RR3 stop widths 0.10/0.15/0.25/0.50,
using the same methodology as the existing SR-retest IS/OOS split (signal
days sorted chronologically, split_idx = round(n*0.70), split applied
uniformly across stop widths since signal detection doesn't depend on
stop distance). Reuses cached ThetaData intraday quotes -> no new API calls.
"""
import sys
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
all_dates = sorted(spy_daily.keys())

stop_distances = [0.05, 0.10, 0.15, 0.25, 0.50]

results, coverage = h.run_r7_or_poc_rr3_real(spy_1min, spy_5min, all_dates, stop_distances)

# Recompute signal_dates the same way run_r7_or_poc_rr3_real builds `signals`
# internally (theta_0dte_usable gate + find_or_poc_signal + real-strike snap),
# so the 70/30 split is over ALL signal days, independent of stop distance -
# matching the SR-retest pattern (signal_dates collected before stop/rr grid).
signal_dates = []
for ds in all_dates:
    bars_1min = spy_1min.get(ds, [])
    bars_5min = spy_5min.get(ds, [])
    if len(bars_1min) < h.OR_POC_MIN + 5 or len(bars_5min) < 4:
        continue
    if not h.theta_0dte_usable(ds):
        continue
    sig = h.find_or_poc_signal(ds, bars_1min, bars_5min)
    if sig is None:
        continue
    right = 'C' if sig['direction'] == 'c' else 'P'
    if h.snap_to_real_strike(ds, sig['strike'], right) is None:
        continue
    signal_dates.append(ds)
signal_dates = sorted(signal_dates)

n = len(signal_dates)
split_idx = round(n * 0.70)
is_dates  = set(signal_dates[:split_idx])
oos_dates = set(signal_dates[split_idx:])

print(f"\n{n} signal days total, 70% split = {split_idx}")
if signal_dates:
    print(f"IS:  {len(is_dates)} signal days ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})")
    print(f"OOS: {len(oos_dates)} signal days ({signal_dates[min(split_idx,n-1)]} -> {signal_dates[-1]})")

print(f"\n{'='*100}")
print(f"{'stop':>6}  {'':>4} {'N':>5} {'WR':>7} {'PF':>7} {'Total P&L':>14}")
print('='*100)
for sd in stop_distances:
    trades = results[sd]
    is_trades  = [t for t in trades if t.date in is_dates]
    oos_trades = [t for t in trades if t.date in oos_dates]
    s_all = h.calc_stats(trades)
    s_is  = h.calc_stats(is_trades)
    s_oos = h.calc_stats(oos_trades)
    print(f"\n-- stop=${sd:.2f} --  (all: N={s_all['n']} WR={s_all['wr']:.1f}% PF={h.fmt_pf(s_all)} P&L=${s_all['total_pnl']:+,.2f})")
    print(f"  IS   N={s_is['n']:>4}  WR={s_is['wr']:>6.1f}%  PF={h.fmt_pf(s_is):>6}  Total P&L=${s_is['total_pnl']:>+12,.2f}")
    print(f"  OOS  N={s_oos['n']:>4}  WR={s_oos['wr']:>6.1f}%  PF={h.fmt_pf(s_oos):>6}  Total P&L=${s_oos['total_pnl']:>+12,.2f}")
