#!/usr/bin/env python3
"""Does OR/POC's performance correlate with SPY 0DTE options volume
(a crowding/gamma-dynamics proxy), as opposed to realized vol (already
ruled out)?

Volume source: theta_SPY_{ds}.pkl (already cached locally, one file per
trading day, keyed by (observation_date, strike, right) -> {bid, ask,
volume, close}). Filtering to entries where observation_date == ds and
summing `volume` gives that day's REAL total 0DTE options volume across
strikes/rights - actual 0DTE volume, not an equity-volume proxy, and
needs zero new ThetaData calls since it's already on disk.

1. Build daily 0DTE volume for every day in [2023-07-01, 2026-05-30].
2. Tercile-split the OR/POC weekly $0.05-stop trade set by that day's
   volume (low/medium/high).
3. Report WR/PF/N/total P&L per tercile.
4. Check the volume trend over time (quarterly averages) - does it show a
   level/trend shift around mid-2025 (our OOS breakpoint), or steady
   gradual growth across the whole period?
"""
import sys
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
from collections import defaultdict

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
print(f"  {len(original_trades)} original trades at stop=${STOP}")

# ── 1. Daily real 0DTE options volume, from already-cached chain files ──
print("\nBuilding daily 0DTE options volume from cached theta_SPY_*.pkl chains...")
daily_vol = {}
for ds in dates:
    chain = h._load_theta_file(ds)
    if not chain:
        continue
    v = sum(q.get('volume', 0) for k, q in chain.items() if k[0] == ds)
    if v > 0:
        daily_vol[ds] = v

print(f"  {len(daily_vol)}/{len(dates)} days with a nonzero real 0DTE volume figure")

vol_sorted = sorted(daily_vol.items(), key=lambda kv: kv[1])
n = len(vol_sorted)
t1, t2 = n // 3, 2 * n // 3
low_days  = set(d for d, v in vol_sorted[:t1])
mid_days  = set(d for d, v in vol_sorted[t1:t2])
high_days = set(d for d, v in vol_sorted[t2:])
print(f"  Tercile cutoffs: low <{vol_sorted[t1][1]:,}  mid <{vol_sorted[t2][1]:,}  high >=  that")

# ── 2+3. Tercile split of the trade set ──────────────────────────────────
print(f"\n{'='*90}\n  OR/POC WEEKLY (stop=$0.05) BY 0DTE-VOLUME TERCILE\n{'='*90}")
orig_s = h.calc_stats(original_trades)
print(f"  {'ALL':<8} N={orig_s['n']:>4}  WR={orig_s['wr']:>6.1f}%  PF={h.fmt_pf(orig_s):>6}  "
      f"Total P&L=${orig_s['total_pnl']:>+10,.2f}")

trades_with_vol = [t for t in original_trades if t.date in daily_vol]
print(f"  ({len(trades_with_vol)}/{len(original_trades)} trades fall on a day with volume data)")

for label, day_set in [('LOW', low_days), ('MEDIUM', mid_days), ('HIGH', high_days)]:
    trades = [t for t in trades_with_vol if t.date in day_set]
    s = h.calc_stats(trades)
    print(f"  {label:<8} N={s['n']:>4}  WR={s['wr']:>6.1f}%  PF={h.fmt_pf(s):>6}  "
          f"Total P&L=${s['total_pnl']:>+10,.2f}  AvgWin=${s['avg_win']:>7.2f}  AvgLoss=${s['avg_loss']:>8.2f}")

# ── 4. Volume trend over time (quarterly averages) ──────────────────────
print(f"\n{'='*90}\n  0DTE VOLUME TREND OVER TIME (quarterly average daily volume)\n{'='*90}")
def quarter_key(ds):
    y, m, _ = ds.split('-')
    q = (int(m) - 1) // 3 + 1
    return f"{y}-Q{q}"

by_q = defaultdict(list)
for d, v in daily_vol.items():
    by_q[quarter_key(d)].append(v)

for q in sorted(by_q.keys()):
    vals = by_q[q]
    avg = sum(vals) / len(vals)
    print(f"  {q}   n_days={len(vals):>3}   avg_daily_0DTE_volume={avg:>14,.0f}")

pre  = [v for d, v in daily_vol.items() if d < '2025-05-01']
post = [v for d, v in daily_vol.items() if d >= '2025-05-01']
if pre and post:
    avg_pre, avg_post = sum(pre)/len(pre), sum(post)/len(post)
    print(f"\n  Avg daily volume before 2025-05-01: {avg_pre:>14,.0f}  (n={len(pre)})")
    print(f"  Avg daily volume from 2025-05-01 on: {avg_post:>14,.0f}  (n={len(post)})")
    print(f"  Ratio post/pre: {avg_post/avg_pre:.2f}x")
