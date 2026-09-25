#!/usr/bin/env python3
"""ORB debit-spread variant of the validated low+medium-0DTE-volume OR/POC
weekly (stop=$0.05) strategy: instead of a naked long call/put, buy the
same ATM/near-ATM contract already used and sell a contract ~3 strikes
further OTM in the same direction (call spread on a call breakout, put
spread on a put breakout), same weekly expiration.

Everything about signal detection, the stop/target underlying levels, and
the exit timing (stop/target/1:30-cutoff) is UNCHANGED from the already-
validated naked long-option run - this script reuses those trade objects
directly (their entry_time/exit_time/entry_ask/exit_bid ARE the long leg's
own real fills) and only adds a second, short leg priced at the same two
timestamps. So the only new ThetaData work is fetching the short leg's
quotes; the long leg and the underlying exit walk are not re-simulated.

Short-strike target = long_strike +/- 3.0 (assuming ~$1 real strike
spacing near the money, confirmed from a spot-check of the cached chains),
found via the same narrow-probe search used elsewhere in this project
(radius 2, $1 steps) rather than assumed to exist exactly at that price.

Uses the already-cached signals for the long leg; only the short leg
requires new live ThetaData calls (one per trade, cache-first/forever
afterward).
"""
import sys, csv, time
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
from datetime import date

DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'
STOP = 0.05
SHORT_OFFSET = 3.0
SHORT_STEP   = 1.0
SHORT_RADIUS = 2

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
all_dates_full = sorted(spy_daily.keys())
dates = [d for d in all_dates_full if DATE_LO <= d <= DATE_HI]

print("Rebuilding weekly signals/trades (cache-hit, no new API calls)...")
results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, [STOP])
original_trades = results_w[STOP]

# ── same real 0DTE volume + tercile split as the validated filter ───────
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
low_mid_days = set(d for d, v in vol_sorted[:t2])

naked_trades = [t for t in original_trades if t.date in low_mid_days]
print(f"{len(naked_trades)} naked-option trades on low+medium-volume days (should be 378)")


def spread_leg_at(ds, exp, target_strike, right, start_hms, end_hms):
    strike, chain_list = h.find_weekly_contract(ds, exp, target_strike, right, start_hms, end_hms,
                                                  radius=SHORT_RADIUS, step=SHORT_STEP)
    if chain_list is None:
        return None, None
    return strike, {r['hm']: r for r in chain_list}


print(f"\nFetching short leg (~{SHORT_OFFSET:.0f} strikes OTM) for each trade "
      f"(cache-first, new API calls where not cached)...")
spread_rows = []
t0 = time.time()
for i, lt in enumerate(naked_trades, 1):
    right = 'C' if lt.direction == 'CALL' else 'P'
    sign = 1.0 if right == 'C' else -1.0
    target_short_strike = lt.strike + sign * SHORT_OFFSET
    exp = date.fromisoformat(lt.expiration)
    entry_hms = lt.entry_time + ':00'

    short_strike, chain = spread_leg_at(lt.date, exp, target_short_strike, right, entry_hms, '13:30:00')
    if chain is None:
        continue
    s_entry_bid = h._nearest_prior_quote(chain, lt.entry_time, 'bid')
    s_entry_ask = h._nearest_prior_quote(chain, lt.entry_time, 'ask')
    s_exit_bid  = h._nearest_prior_quote(chain, lt.exit_time, 'bid')
    s_exit_ask  = h._nearest_prior_quote(chain, lt.exit_time, 'ask')
    if None in (s_entry_bid, s_entry_ask, s_exit_bid, s_exit_ask) or s_entry_bid <= 0:
        continue

    debit_paid   = lt.entry_ask - s_entry_bid
    if debit_paid <= 0:
        continue
    credit_recv  = lt.exit_bid - s_exit_ask
    pnl = round((credit_recv - debit_paid) * 100, 2)

    long_entry_mid = (lt.entry_bid + lt.entry_ask) / 2 if lt.entry_bid else None
    long_exit_mid  = (lt.exit_bid + lt.exit_ask) / 2 if lt.exit_ask else None
    short_entry_mid = (s_entry_bid + s_entry_ask) / 2
    short_exit_mid  = (s_exit_bid + s_exit_ask) / 2
    debit_mid  = (long_entry_mid - short_entry_mid) if long_entry_mid is not None else None
    credit_mid = (long_exit_mid - short_exit_mid) if long_exit_mid is not None else None
    entry_spread_cost = (debit_paid - debit_mid) if debit_mid is not None else None
    exit_spread_cost  = (credit_mid - credit_recv) if credit_mid is not None else None
    total_spread_cost = (entry_spread_cost + exit_spread_cost) if (entry_spread_cost is not None and exit_spread_cost is not None) else None

    spread_rows.append({
        'date': lt.date, 'entry_time': lt.entry_time, 'exit_time': lt.exit_time,
        'direction': lt.direction, 'long_strike': lt.strike, 'short_strike': short_strike,
        'expiration': lt.expiration, 'exit_reason': lt.exit_reason,
        'long_entry_ask': lt.entry_ask, 'short_entry_bid': s_entry_bid,
        'long_exit_bid': lt.exit_bid, 'short_exit_ask': s_exit_ask,
        'debit_paid': round(debit_paid, 4), 'credit_recv': round(credit_recv, 4),
        'pnl': pnl, 'total_spread_cost': (round(total_spread_cost, 4) if total_spread_cost is not None else None),
        'spread_cost_pct_of_debit': (round(total_spread_cost / debit_paid * 100, 2)
                                      if total_spread_cost is not None else None),
    })
    if i % 50 == 0:
        print(f"  {i}/{len(naked_trades)}  (api_calls={h.THETA_API_CALLS}, api_time={h.THETA_API_SECONDS:.1f}s, "
              f"elapsed={time.time()-t0:.1f}s)")

print(f"\n{len(spread_rows)}/{len(naked_trades)} spread trades built "
      f"(api_calls={h.THETA_API_CALLS}, api_time={h.THETA_API_SECONDS:.1f}s)")

# write CSV
out_path = r'C:\Users\sagla\spy-0dte-trader\trades_weekly_spread_lowmedvol.csv'
with open(out_path, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(spread_rows[0].keys()))
    w.writeheader()
    w.writerows(spread_rows)
print(f"Wrote -> {out_path}")


# ── summary stats ────────────────────────────────────────────────────────
class _T:
    def __init__(self, pnl, entry_date):
        self.pnl = pnl
        self.entry_date = entry_date

spread_trades_for_stats = [_T(r['pnl'], r['date']) for r in spread_rows]
s_spread = h.calc_stats(spread_trades_for_stats)
s_naked  = h.calc_stats(naked_trades)

print(f"\n{'='*90}\n  SPREAD vs NAKED (low+medium-volume days, stop=$0.05)\n{'='*90}")
print(f"  NAKED   N={s_naked['n']:>4}  WR={s_naked['wr']:>6.1f}%  PF={h.fmt_pf(s_naked):>6}  "
      f"Total P&L=${s_naked['total_pnl']:>+10,.2f}  AvgWin=${s_naked['avg_win']:>7.2f}  AvgLoss=${s_naked['avg_loss']:>8.2f}")
print(f"  SPREAD  N={s_spread['n']:>4}  WR={s_spread['wr']:>6.1f}%  PF={h.fmt_pf(s_spread):>6}  "
      f"Total P&L=${s_spread['total_pnl']:>+10,.2f}  AvgWin=${s_spread['avg_win']:>7.2f}  AvgLoss=${s_spread['avg_loss']:>8.2f}")

# ── spread cost comparison ───────────────────────────────────────────────
pct_vals = [r['spread_cost_pct_of_debit'] for r in spread_rows if r['spread_cost_pct_of_debit'] is not None]
avg_spread_pct = sum(pct_vals) / len(pct_vals) if pct_vals else float('nan')

naked_pct_vals = []
for t in naked_trades:
    if t.entry_bid and t.entry_ask and t.exit_bid and t.exit_ask and t.entry_ask > 0:
        cost = (t.entry_ask - t.entry_bid) + (t.exit_ask - t.exit_bid)
        naked_pct_vals.append(cost / t.entry_ask * 100)
avg_naked_pct = sum(naked_pct_vals) / len(naked_pct_vals) if naked_pct_vals else float('nan')

print(f"\n{'='*90}\n  ROUND-TRIP SPREAD COST AS % OF DEBIT PAID\n{'='*90}")
print(f"  NAKED option (entry ask + exit bid/ask round trip, as % of entry ask):  {avg_naked_pct:.2f}%  (n={len(naked_pct_vals)})")
print(f"  SPREAD (net round-trip spread cost across both legs, as % of net debit): {avg_spread_pct:.2f}%  (n={len(pct_vals)})")

beats_or_matches = (s_spread['pf'] >= s_naked['pf']) or (s_spread['total_pnl'] >= s_naked['total_pnl'])
print(f"\n  Spread beats/matches naked on PF or P&L: {'YES' if beats_or_matches else 'NO'} "
      f"-> {'running validation splits' if beats_or_matches else 'skipping validation splits'}")

if beats_or_matches:
    print(f"\n{'='*90}\n  VALIDATION SPLITS on SPREAD version\n{'='*90}")
    signal_dates = sorted({r['date'] for r in spread_rows})
    n_days = len(signal_dates)
    split_idx = round(n_days * 0.70)
    is_dates  = set(signal_dates[:split_idx])
    oos_dates = set(signal_dates[split_idx:])
    is_trades  = [_T(r['pnl'], r['date']) for r in spread_rows if r['date'] in is_dates]
    oos_trades = [_T(r['pnl'], r['date']) for r in spread_rows if r['date'] in oos_dates]
    s_is, s_oos = h.calc_stats(is_trades), h.calc_stats(oos_trades)

    rows_sorted = sorted(spread_rows, key=lambda r: (r['date'], r['entry_time']))
    even_rows = rows_sorted[0::2]
    odd_rows  = rows_sorted[1::2]
    s_even = h.calc_stats([_T(r['pnl'], r['date']) for r in even_rows])
    s_odd  = h.calc_stats([_T(r['pnl'], r['date']) for r in odd_rows])

    print(f"\n  -- Chronological 70/30 -- ({n_days} signal days, split={split_idx})")
    print(f"  IS   ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})  "
          f"N={s_is['n']:>4}  WR={s_is['wr']:>6.1f}%  PF={h.fmt_pf(s_is):>6}  Total P&L=${s_is['total_pnl']:>+10,.2f}")
    print(f"  OOS  ({signal_dates[min(split_idx,n_days-1)]} -> {signal_dates[-1]})  "
          f"N={s_oos['n']:>4}  WR={s_oos['wr']:>6.1f}%  PF={h.fmt_pf(s_oos):>6}  Total P&L=${s_oos['total_pnl']:>+10,.2f}")

    print(f"\n  -- Interleaved odd/even -- ({len(rows_sorted)} trades)")
    print(f"  EVEN N={s_even['n']:>4}  WR={s_even['wr']:>6.1f}%  PF={h.fmt_pf(s_even):>6}  Total P&L=${s_even['total_pnl']:>+10,.2f}")
    print(f"  ODD  N={s_odd['n']:>4}  WR={s_odd['wr']:>6.1f}%  PF={h.fmt_pf(s_odd):>6}  Total P&L=${s_odd['total_pnl']:>+10,.2f}")

    print(f"\n  -- Pass/fail (PF>=1.0 and positive P&L in every subset) --")
    for label, s in [('IS', s_is), ('OOS', s_oos), ('EVEN', s_even), ('ODD', s_odd)]:
        passed = s['pf'] >= 1.0 and s['total_pnl'] > 0
        print(f"  {label:<5} PF={h.fmt_pf(s):>6}  P&L=${s['total_pnl']:>+9,.2f}  -> {'PASS' if passed else 'FAIL'}")
    all_pass = all(s['pf'] >= 1.0 and s['total_pnl'] > 0 for s in [s_is, s_oos, s_even, s_odd])
    print(f"\n  Clears all four splits: {'YES' if all_pass else 'NO'}")
