#!/usr/bin/env python3
"""Validate the 0DTE-volume filter: exclude the top-tercile-volume days,
keep only low+medium volume days, same stop=$0.05. Same methodology and
tercile cutoffs as the prior volume/crowding analysis (real 0DTE options
volume summed from the already-cached theta_SPY_{date}.pkl chain files).
Then run both validation splits used throughout today - chronological
70/30 IS/OOS and interleaved odd/even - same pass bar as everything else.

Uses only cached data - no new ThetaData calls.
"""
import sys
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h

DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'
STOP = 0.05

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
all_dates_full = sorted(spy_daily.keys())
dates = [d for d in all_dates_full if DATE_LO <= d <= DATE_HI]

print("Rebuilding weekly signals/trades (cache-hit, no new API calls)...")
results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, [STOP])
original_trades = results_w[STOP]

# ── same real 0DTE volume + tercile split as before ──────────────────────
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
t1, t2 = n // 3, 2 * n // 3
low_days  = set(d for d, v in vol_sorted[:t1])
mid_days  = set(d for d, v in vol_sorted[t1:t2])
high_days = set(d for d, v in vol_sorted[t2:])
low_mid_days = low_days | mid_days

filtered_trades = [t for t in original_trades if t.date in low_mid_days]
excluded_trades = [t for t in original_trades if t.date in high_days]
print(f"\n{len(filtered_trades)} trades on low+medium-volume days "
      f"({len(excluded_trades)} excluded, top-tercile-volume days)")

# ═══════════════════════════════════════════════════════════════════════
# 1. Combined low+medium stats
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*90}\n  1. LOW+MEDIUM VOLUME COMBINED (top-tercile-volume days excluded)\n{'='*90}")
s_all = h.calc_stats(filtered_trades)
orig_s = h.calc_stats(original_trades)
print(f"  UNFILTERED   N={orig_s['n']:>4}  WR={orig_s['wr']:>6.1f}%  PF={h.fmt_pf(orig_s):>6}  Total P&L=${orig_s['total_pnl']:>+10,.2f}")
print(f"  LOW+MEDIUM   N={s_all['n']:>4}  WR={s_all['wr']:>6.1f}%  PF={h.fmt_pf(s_all):>6}  Total P&L=${s_all['total_pnl']:>+10,.2f}  "
      f"AvgWin=${s_all['avg_win']:>7.2f}  AvgLoss=${s_all['avg_loss']:>8.2f}")

# ═══════════════════════════════════════════════════════════════════════
# 2+3. Both validation splits
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*90}\n  2+3. VALIDATION SPLITS on LOW+MEDIUM-VOLUME filter\n{'='*90}")

signal_dates = sorted({t.date for t in filtered_trades})
n_days = len(signal_dates)
split_idx = round(n_days * 0.70)
is_dates  = set(signal_dates[:split_idx])
oos_dates = set(signal_dates[split_idx:])
is_trades  = [t for t in filtered_trades if t.date in is_dates]
oos_trades = [t for t in filtered_trades if t.date in oos_dates]
s_is, s_oos = h.calc_stats(is_trades), h.calc_stats(oos_trades)

trades_sorted = sorted(filtered_trades, key=lambda t: (t.date, t.entry_time))
even_trades = trades_sorted[0::2]
odd_trades  = trades_sorted[1::2]
s_even, s_odd = h.calc_stats(even_trades), h.calc_stats(odd_trades)

print(f"\n  -- Chronological 70/30 -- ({n_days} signal days, split={split_idx})")
print(f"  IS   ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})  "
      f"N={s_is['n']:>4}  WR={s_is['wr']:>6.1f}%  PF={h.fmt_pf(s_is):>6}  Total P&L=${s_is['total_pnl']:>+10,.2f}")
print(f"  OOS  ({signal_dates[min(split_idx,n_days-1)]} -> {signal_dates[-1]})  "
      f"N={s_oos['n']:>4}  WR={s_oos['wr']:>6.1f}%  PF={h.fmt_pf(s_oos):>6}  Total P&L=${s_oos['total_pnl']:>+10,.2f}")

print(f"\n  -- Interleaved odd/even -- ({len(trades_sorted)} trades)")
print(f"  EVEN N={s_even['n']:>4}  WR={s_even['wr']:>6.1f}%  PF={h.fmt_pf(s_even):>6}  Total P&L=${s_even['total_pnl']:>+10,.2f}")
print(f"  ODD  N={s_odd['n']:>4}  WR={s_odd['wr']:>6.1f}%  PF={h.fmt_pf(s_odd):>6}  Total P&L=${s_odd['total_pnl']:>+10,.2f}")

# ═══════════════════════════════════════════════════════════════════════
# 4. Pass/fail, same bar as everything else today
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*90}\n  4. PASS/FAIL (PF>=1.0 and positive P&L in every subset)\n{'='*90}")
for label, s in [('IS', s_is), ('OOS', s_oos), ('EVEN', s_even), ('ODD', s_odd)]:
    passed = s['pf'] >= 1.0 and s['total_pnl'] > 0
    print(f"  {label:<5} PF={h.fmt_pf(s):>6}  P&L=${s['total_pnl']:>+9,.2f}  -> {'PASS' if passed else 'FAIL'}")
all_pass = all(s['pf'] >= 1.0 and s['total_pnl'] > 0 for s in [s_is, s_oos, s_even, s_odd])
print(f"\n  Clears all four splits: {'YES' if all_pass else 'NO'}")
