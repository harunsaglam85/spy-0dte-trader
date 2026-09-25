#!/usr/bin/env python3
"""
Real Monte Carlo: bootstrap-resample ACTUAL historical trades (real dollar
P&L, real option entry cost per trade) from the two validated full-history
backtests, instead of the generic win-rate/fixed-payoff model in the old
monte_carlo.py.

Sources (unmodified, already-existing backtest output CSVs):
  - OR/POC weekly, unfiltered, stop=$0.05, target R:R=3.0 (module constant
    OR_POC_RR): trades_weekly_rr3.0_sweep.csv, stop_distance==0.05 rows.
    2023-07-01..2026-05-30, real ThetaData bid/ask fills.
  - VTC best combo, tolerance=$0.25, stop_buffer=$0.15, target R:R=3.5:
    vtc_grid_results.csv, filtered to that combo. Same date range/fills.

Methodology:
  - Each simulated "trade" is a full historical (entry_cost, pnl) record
    drawn WITH REPLACEMENT from the real trade list - preserving the real
    joint relationship between a trade's premium and its outcome (a bigger-
    premium trade isn't resampled with some unrelated trade's P&L).
  - Position sizing = 1 contract. A signal is SKIPPED (no capital change)
    if current capital < that trade's real entry cost (ask*100) - same
    "can't afford it, skip" rule used in the prior strategy_overlap_basket
    capital-reality analysis, not an assumed infinite-margin account.
  - "Effective bust" threshold = the MEDIAN real entry cost across that
    strategy's own trade history (a fixed, interpretable dollar line - the
    cost of a typical, not cheapest-ever, contract). Flagged if account
    equity ever drops below it at any point in the 100-trade sequence.
  - "50%+ drawdown" = standard peak-to-trough drawdown from the running
    equity peak (not just vs. starting capital) hitting >=50% at any point.
  - COMBINED (both traded simultaneously, one shared capital pool) pools
    both strategies' real trade records into one list and draws from the
    union - i.e. trading both means roughly double the trade frequency,
    drawing from real historical outcomes of either. 100 combined trades
    corresponds to roughly the same live-trading time horizon as running
    each strategy alone for ~2x as many calendar days.
"""
import csv
import random
import statistics
from pathlib import Path

BASE = Path(r'C:\Users\sagla\spy-0dte-trader')
random.seed(20260924)

N_SIMS      = 10_000
N_TRADES    = 100
STARTS      = [1500, 2000]


def load_or_poc():
    trades = []
    with (BASE / 'trades_weekly_rr3.0_sweep.csv').open() as f:
        for row in csv.DictReader(f):
            if row['stop_distance'] != '0.05':
                continue
            cost = float(row['option_entry_ask']) * 100
            pnl  = float(row['pnl'])
            trades.append((cost, pnl))
    return trades


def load_vtc():
    trades = []
    with (BASE / 'vtc_grid_results.csv').open() as f:
        for row in csv.DictReader(f):
            if (row['tolerance'], row['stop_buffer'], row['target_rr']) != ('0.25', '0.15', '3.5'):
                continue
            cost = float(row['entry_option']) * 100
            pnl  = float(row['pnl'])
            trades.append((cost, pnl))
    return trades


or_poc_trades = load_or_poc()
vtc_trades    = load_vtc()
combined_trades = or_poc_trades + vtc_trades

print(f"OR/POC real trades loaded: {len(or_poc_trades)}  "
      f"(WR={sum(1 for c,p in or_poc_trades if p>0)/len(or_poc_trades)*100:.1f}%, "
      f"total P&L=${sum(p for c,p in or_poc_trades):+,.0f}, "
      f"median entry cost=${statistics.median(c for c,p in or_poc_trades):,.0f})")
print(f"VTC real trades loaded:    {len(vtc_trades)}  "
      f"(WR={sum(1 for c,p in vtc_trades if p>0)/len(vtc_trades)*100:.1f}%, "
      f"total P&L=${sum(p for c,p in vtc_trades):+,.0f}, "
      f"median entry cost=${statistics.median(c for c,p in vtc_trades):,.0f})")


def pct(sorted_vals, p):
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = p / 100.0 * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo])


def run_mc(trade_pool, starting_capital, n_sims=N_SIMS, n_trades=N_TRADES):
    bust_line = statistics.median(c for c, p in trade_pool)
    finals, ever_dd50, ever_bust, n_skipped_total = [], 0, 0, 0

    for _ in range(n_sims):
        cap = float(starting_capital)
        peak = cap
        max_dd = 0.0
        busted = False
        for _t in range(n_trades):
            cost, pnl = trade_pool[random.randrange(len(trade_pool))]
            if cap < cost:
                n_skipped_total += 1
            else:
                cap += pnl
            if cap > peak:
                peak = cap
            dd = (peak - cap) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            if cap < bust_line:
                busted = True
        finals.append(cap)
        if max_dd >= 0.50:
            ever_dd50 += 1
        if busted:
            ever_bust += 1

    s = sorted(finals)
    return {
        'n': n_sims, 'bust_line': bust_line,
        'median': pct(s, 50), 'p10': pct(s, 10), 'p90': pct(s, 90),
        'p_dd50': ever_dd50 / n_sims * 100,
        'p_bust': ever_bust / n_sims * 100,
        'avg_skips_per_run': n_skipped_total / n_sims,
    }


print(f"\n{'='*100}\n  MONTE CARLO: {N_SIMS:,} bootstrapped {N_TRADES}-trade sequences, "
      f"real historical (cost, P&L) pairs, 1 contract/signal\n{'='*100}")

scenarios = [
    ('OR/POC alone',        or_poc_trades),
    ('VTC alone',           vtc_trades),
    ('COMBINED (shared capital)', combined_trades),
]

for start in STARTS:
    print(f"\n{'-'*100}\n  STARTING CAPITAL: ${start:,}\n{'-'*100}")
    print(f"  {'Scenario':<28}{'BustLine':>10}{'Median':>10}{'P10':>10}{'P90':>10}"
          f"{'P(DD>=50%)':>12}{'P(bust)':>10}{'AvgSkips':>10}")
    for label, pool in scenarios:
        r = run_mc(pool, start)
        print(f"  {label:<28}${r['bust_line']:>8,.0f}  ${r['median']:>7,.0f}  ${r['p10']:>7,.0f}  "
              f"${r['p90']:>7,.0f}  {r['p_dd50']:>10.1f}%  {r['p_bust']:>8.1f}%  {r['avg_skips_per_run']:>9.1f}")

print(f"\n{'='*100}\n  Done.\n{'='*100}")
