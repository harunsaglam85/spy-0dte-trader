#!/usr/bin/env python3
"""Realized-vol filter on the OR/POC weekly backtest (stop=$0.05):
  1. Median filter - only trade when trailing 20-day annualized RV (causal,
     no-lookahead - same measure as the earlier regime analysis) is above
     the dataset median.
  2. Top-tercile filter - same cutoff methodology as the earlier low-vol
     tercile analysis, inverted (top 33% instead of bottom 33%).
Then, on whichever of the two performs better, run BOTH validation splits
used earlier: chronological 70/30 IS/OOS and interleaved odd/even. It only
counts as validated if it clears both.

Uses only cached data - no new ThetaData calls.
"""
import sys, math
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h

DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'
STOP      = 0.05
RV_WINDOW = 20

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
all_dates_full = sorted(spy_daily.keys())
dates = [d for d in all_dates_full if DATE_LO <= d <= DATE_HI]

print("Rebuilding weekly signals/trades (cache-hit, no new API calls)...")
results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, [STOP])
original_trades = results_w[STOP]
print(f"  {len(original_trades)} original (unfiltered) trades at stop=${STOP}")


# ─────────────────────────────────────────────────────────────────────────
# Same rolling, causal realized-vol measure as the earlier regime analysis
# ─────────────────────────────────────────────────────────────────────────
def build_rolling_rv(spy_daily, all_dates_full, window=RV_WINDOW):
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

rv_by_day = build_rolling_rv(spy_daily, all_dates_full, RV_WINDOW)
rv_in_range = sorted([(d, rv_by_day[d]) for d in dates if d in rv_by_day], key=lambda x: x[1])
n_rv = len(rv_in_range)
median_val = rv_in_range[n_rv // 2][1] if n_rv % 2 else (rv_in_range[n_rv//2-1][1] + rv_in_range[n_rv//2][1]) / 2
median_days = set(d for d, v in rv_in_range if v > median_val)

tercile_size = n_rv // 3
top_tercile = rv_in_range[-tercile_size:]
top_tercile_days = set(d for d, v in top_tercile)
top_cutoff = top_tercile[0][1]

print(f"\n{n_rv} days with a valid {RV_WINDOW}-day rolling RV in [{DATE_LO}, {DATE_HI}]")
print(f"Median RV: {median_val:.2f}%  -> {len(median_days)} days above median")
print(f"Top tercile: RV >= {top_cutoff:.2f}%  -> {len(top_tercile_days)} days")


def filtered_stats(label, day_set):
    trades = [t for t in original_trades if t.date in day_set]
    s = h.calc_stats(trades)
    print(f"  {label:<16} N={s['n']:>4}  WR={s['wr']:>6.1f}%  PF={h.fmt_pf(s):>6}  "
          f"Total P&L=${s['total_pnl']:>+10,.2f}  AvgWin=${s['avg_win']:>7.2f}  AvgLoss=${s['avg_loss']:>8.2f}")
    return trades, s


print(f"\n{'='*90}\n  REALIZED-VOL FILTERS ON OR/POC WEEKLY (stop=$0.05)\n{'='*90}")
orig_s = h.calc_stats(original_trades)
print(f"  {'UNFILTERED':<16} N={orig_s['n']:>4}  WR={orig_s['wr']:>6.1f}%  PF={h.fmt_pf(orig_s):>6}  "
      f"Total P&L=${orig_s['total_pnl']:>+10,.2f}  AvgWin=${orig_s['avg_win']:>7.2f}  AvgLoss=${orig_s['avg_loss']:>8.2f}")
median_trades, median_s = filtered_stats('ABOVE MEDIAN', median_days)
top_trades, top_s       = filtered_stats('TOP TERCILE', top_tercile_days)


def pf_key(s):
    return (s['pf'] if s['pf'] != float('inf') else 1e9, s['total_pnl'])

if pf_key(top_s) >= pf_key(median_s):
    best_label, best_trades, best_s = 'TOP TERCILE', top_trades, top_s
else:
    best_label, best_trades, best_s = 'ABOVE MEDIAN', median_trades, median_s
print(f"\n  Better performer: {best_label} (PF={h.fmt_pf(best_s)}, P&L=${best_s['total_pnl']:+,.2f}) "
      f"-> running both validation splits on this one")


# ─────────────────────────────────────────────────────────────────────────
# Validation: chronological 70/30 IS/OOS + interleaved odd/even
# ─────────────────────────────────────────────────────────────────────────
print(f"\n{'='*90}\n  VALIDATION SPLITS on {best_label} filter\n{'='*90}")

signal_dates = sorted({t.date for t in best_trades})
n = len(signal_dates)
split_idx = round(n * 0.70)
is_dates  = set(signal_dates[:split_idx])
oos_dates = set(signal_dates[split_idx:])
is_trades  = [t for t in best_trades if t.date in is_dates]
oos_trades = [t for t in best_trades if t.date in oos_dates]
s_is, s_oos = h.calc_stats(is_trades), h.calc_stats(oos_trades)

trades_sorted = sorted(best_trades, key=lambda t: (t.date, t.entry_time))
even_trades = trades_sorted[0::2]
odd_trades  = trades_sorted[1::2]
s_even, s_odd = h.calc_stats(even_trades), h.calc_stats(odd_trades)

print(f"\n  -- Chronological 70/30 -- ({n} signal days, split={split_idx})")
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
