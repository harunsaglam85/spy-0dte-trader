#!/usr/bin/env python3
"""
Re-run the real-trade-bootstrap Monte Carlo (see _monte_carlo_real.py) with
realistic per-contract fees now included, and report the delta against the
fee-free numbers - using the SAME bootstrap draws for both the fee-free and
fee-adjusted run (paired comparison) so the reported delta is the actual
effect of fees, not just re-sampling noise between two separate 10k-run MCs.

Fee model (see chat for sourcing):
  - Webull: $0 commission on standard equity/ETF options (SPY qualifies -
    only index options and >500-contract orders carry a per-contract fee).
  - Regulatory pass-throughs (ORF + OCC clearing + minor others) - these
    vary by which of ~16 options exchanges a given fill actually routed to
    and changed multiple times through 2026 (OCC clearing currently $0.025/
    contract; ORF ranges roughly $0.0008-$0.02/contract-side across
    exchanges this year). Since the historical CSVs don't record execution
    venue, this uses Webull's own disclosed BLENDED statement figure -
    "~$0.06 per contract, charged to open and to close" - as the realistic
    per-side estimate, rather than fabricating a fake venue-by-venue split.
  - Total modeled fee = $0.06 (entry) + $0.06 (exit) = $0.12 per contract
    round-trip trade. Applied to both the entry cost (affordability check)
    and subtracted from pnl (already reflected in pnl_after_fees, now
    written into both CSVs).
"""
import csv
import random
import statistics
from pathlib import Path

BASE = Path(r'C:\Users\sagla\spy-0dte-trader')
random.seed(20260924)

N_SIMS, N_TRADES, STARTS = 10_000, 100, [1500, 2000]
FEE_ENTRY = 0.06


def load_or_poc():
    trades = []
    with (BASE / 'trades_weekly_rr3.0_sweep.csv').open() as f:
        for row in csv.DictReader(f):
            if row['stop_distance'] != '0.05':
                continue
            trades.append({
                'cost': float(row['option_entry_ask']) * 100,
                'pnl': float(row['pnl']),
                'pnl_fee': float(row['pnl_after_fees']),
            })
    return trades


def load_vtc():
    trades = []
    with (BASE / 'vtc_grid_results.csv').open() as f:
        for row in csv.DictReader(f):
            if (row['tolerance'], row['stop_buffer'], row['target_rr']) != ('0.25', '0.15', '3.5'):
                continue
            trades.append({
                'cost': float(row['entry_option']) * 100,
                'pnl': float(row['pnl']),
                'pnl_fee': float(row['pnl_after_fees']),
            })
    return trades


or_poc_trades = load_or_poc()
vtc_trades = load_vtc()
combined_trades = or_poc_trades + vtc_trades


def pct(sorted_vals, p):
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = p / 100.0 * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo])


def run_mc_paired(trade_pool, starting_capital, with_fees, n_sims=N_SIMS, n_trades=N_TRADES, seed=None):
    bust_line = statistics.median(t['cost'] for t in trade_pool)
    rng = random.Random(seed)
    finals, ever_dd50, ever_bust = [], 0, 0

    for _ in range(n_sims):
        cap = float(starting_capital)
        peak = cap
        max_dd = 0.0
        busted = False
        for _t in range(n_trades):
            tr = trade_pool[rng.randrange(len(trade_pool))]
            entry_cost = tr['cost'] + (FEE_ENTRY if with_fees else 0.0)
            pnl = tr['pnl_fee'] if with_fees else tr['pnl']
            if cap >= entry_cost:
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
        'median': pct(s, 50), 'p10': pct(s, 10), 'p90': pct(s, 90),
        'p_dd50': ever_dd50 / n_sims * 100, 'p_bust': ever_bust / n_sims * 100,
    }


scenarios = [('OR/POC alone', or_poc_trades), ('VTC alone', vtc_trades),
             ('COMBINED (shared capital)', combined_trades)]

print(f"{'='*112}\n  FEE MODEL: $0 Webull commission + $0.06/contract/side regulatory pass-through "
      f"= $0.12/round-trip trade\n{'='*112}")

for start in STARTS:
    print(f"\n{'-'*112}\n  STARTING CAPITAL: ${start:,}   "
          f"(paired comparison - same 10,000 bootstrap draws for both rows)\n{'-'*112}")
    print(f"  {'Scenario':<28}{'Fees':<6}{'Median':>10}{'P10':>10}{'P90':>10}{'P(DD>=50%)':>12}{'P(bust)':>10}")
    for label, pool in scenarios:
        seed = hash((label, start)) & 0xFFFFFFFF
        r0 = run_mc_paired(pool, start, with_fees=False, seed=seed)
        r1 = run_mc_paired(pool, start, with_fees=True, seed=seed)
        print(f"  {label:<28}{'no':<6}${r0['median']:>8,.0f}  ${r0['p10']:>7,.0f}  ${r0['p90']:>7,.0f}  "
              f"{r0['p_dd50']:>10.1f}%  {r0['p_bust']:>8.1f}%")
        print(f"  {'':<28}{'yes':<6}${r1['median']:>8,.0f}  ${r1['p10']:>7,.0f}  ${r1['p90']:>7,.0f}  "
              f"{r1['p_dd50']:>10.1f}%  {r1['p_bust']:>8.1f}%")
        print(f"  {'':<28}{'diff':<6}${r1['median']-r0['median']:>+8,.0f}  ${r1['p10']-r0['p10']:>+7,.0f}  "
              f"${r1['p90']-r0['p90']:>+7,.0f}  {r1['p_dd50']-r0['p_dd50']:>+10.1f}%  "
              f"{r1['p_bust']-r0['p_bust']:>+8.1f}%")

print(f"\n{'='*112}\n  Done.\n{'='*112}")
