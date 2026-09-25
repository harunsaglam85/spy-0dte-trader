#!/usr/bin/env python3
"""Real-time partial-exit rule on the OR/POC weekly $0.05-stop set: exit the
FULL position the instant price reaches 25% of the distance to target
(applied to every trade as it happens, not conditioned on how the trade
would have ended - the original 3:1 target is never reachable under this
rule since 25% is always touched first on the way there, so the walk only
needs to check the original stop vs. the new 25% level).

Same conservative convention as the rest of this project's exit walks: if a
single 1-min bar's range covers both the original stop and the 25% level
(possible only on an unusually wide bar), the stop is assumed to have hit
first - stated explicitly here since it's a real assumption, not derivable
from OHLC bars alone.

Uses only cached data - no new ThetaData calls.
"""
import sys
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h

DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'
STOP  = 0.05
FRAC  = 0.25

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
all_dates = sorted(spy_daily.keys())
dates = [d for d in all_dates if DATE_LO <= d <= DATE_HI]

print("Rebuilding weekly signals/trades (cache-hit, no new API calls)...")
results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, [STOP])
original_trades = results_w[STOP]
sig_by_key = {(s['ds'], s['direction']): s for s in signals_w}
print(f"  {len(original_trades)} original trades at stop=${STOP}")


def simulate_partial_exit(sig, stop_distance, frac=FRAC):
    direction, poc, bars, entry_idx = sig['direction'], sig['poc'], sig['bars'], sig['entry_idx']
    entry_dt, entry_spy = sig['entry_dt'], sig['entry_spy']
    chain = sig.get('chain')
    if not chain:
        return None
    if direction == 'c':
        stop_spy = poc - stop_distance
        if stop_spy >= entry_spy:
            return None
        risk = entry_spy - stop_spy
        target_spy = entry_spy + h.OR_POC_RR * risk
        partial_level = entry_spy + frac * (target_spy - entry_spy)
    else:
        stop_spy = poc + stop_distance
        if stop_spy <= entry_spy:
            return None
        risk = stop_spy - entry_spy
        target_spy = entry_spy - h.OR_POC_RR * risk
        partial_level = entry_spy - frac * (entry_spy - target_spy)
    if risk < 0.02:
        return None

    entry_hm  = entry_dt.strftime('%H:%M')
    entry_bid = h._nearest_prior_quote(chain, entry_hm, 'bid')
    entry_ask = h._nearest_prior_quote(chain, entry_hm, 'ask')
    if entry_ask is None or entry_ask <= 0:
        return None

    exit_bar, exit_reason, exit_spy = None, 'EOD', None
    cutoff = (13, 30)
    for b2 in bars[entry_idx + 1:]:
        t2 = h._et(b2)
        if (t2.hour, t2.minute) >= cutoff:
            exit_bar, exit_reason, exit_spy = b2, '1:30 cutoff', b2['c']
            break
        if direction == 'c':
            if b2['l'] <= stop_spy:            # stop checked first (conservative, see docstring)
                exit_bar, exit_reason, exit_spy = b2, f'stop (POC-{stop_distance:.2f})', stop_spy
                break
            if b2['h'] >= partial_level:
                exit_bar, exit_reason, exit_spy = b2, f'partial exit ({frac*100:.0f}% of target)', partial_level
                break
        else:
            if b2['h'] >= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (POC+{stop_distance:.2f})', stop_spy
                break
            if b2['l'] <= partial_level:
                exit_bar, exit_reason, exit_spy = b2, f'partial exit ({frac*100:.0f}% of target)', partial_level
                break
    if exit_bar is None:
        exit_bar, exit_reason, exit_spy = bars[-1], 'EOD', bars[-1]['c']
    exit_dt = h._et(exit_bar)
    exit_hm = exit_dt.strftime('%H:%M')
    exit_bid = h._nearest_prior_quote(chain, exit_hm, 'bid')
    exit_ask = h._nearest_prior_quote(chain, exit_hm, 'ask')
    if exit_bid is None:
        return None
    pnl = round((exit_bid - entry_ask) * 100, 2)
    return h.ORPocTradeWeekly(
        date=sig['ds'], entry_date=sig['ds'], entry_time=entry_hm, exit_time=exit_hm,
        direction=('CALL' if direction == 'c' else 'PUT'), strike=sig['strike'],
        expiration=sig['expiration'].isoformat(), dte=sig['dte'],
        or_high=sig['or_high'], or_low=sig['or_low'], poc=poc,
        entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2),
        target_spy=round(target_spy, 2), exit_spy=round(exit_spy, 2),
        entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
        exit_reason=exit_reason, pnl=pnl, stop_distance=stop_distance,
        entry_bid=(round(entry_bid, 4) if entry_bid is not None else None),
        entry_ask=round(entry_ask, 4), exit_bid=round(exit_bid, 4),
        exit_ask=(round(exit_ask, 4) if exit_ask is not None else None))


partial_trades = []
partial_by_key = {}
for s in signals_w:
    t = simulate_partial_exit(s, STOP)
    if t is not None:
        partial_trades.append(t)
        partial_by_key[(t.date, t.direction)] = t

orig_stats = h.calc_stats(original_trades)
part_stats = h.calc_stats(partial_trades)

print(f"\n{'='*90}\n  ORIGINAL (hold to target/stop) vs PARTIAL-EXIT-AT-{FRAC*100:.0f}%-OF-TARGET\n{'='*90}")
print(f"  ORIGINAL  N={orig_stats['n']:>4}  WR={orig_stats['wr']:>6.1f}%  PF={h.fmt_pf(orig_stats):>6}  "
      f"Total P&L=${orig_stats['total_pnl']:>+10,.2f}  AvgWin=${orig_stats['avg_win']:>7.2f}  AvgLoss=${orig_stats['avg_loss']:>8.2f}")
print(f"  PARTIAL   N={part_stats['n']:>4}  WR={part_stats['wr']:>6.1f}%  PF={h.fmt_pf(part_stats):>6}  "
      f"Total P&L=${part_stats['total_pnl']:>+10,.2f}  AvgWin=${part_stats['avg_win']:>7.2f}  AvgLoss=${part_stats['avg_loss']:>8.2f}")

# ── cost specifically on originally-winning trades ──────────────────────
orig_winners = [t for t in original_trades if t.pnl > 0]
cut_short = []
still_win_same = []
for ot in orig_winners:
    key = (ot.date, ot.direction)
    nt = partial_by_key.get(key)
    if nt is None:
        continue
    if nt.exit_reason.startswith('partial exit'):
        cut_short.append((ot, nt))
    else:
        still_win_same.append((ot, nt))

pnl_cost = sum(nt.pnl - ot.pnl for ot, nt in cut_short)
print(f"\n{'='*90}\n  COST TO ORIGINALLY-WINNING TRADES\n{'='*90}")
print(f"  {len(orig_winners)} trades were winners in the original (hold-to-target/stop) run")
print(f"  {len(cut_short)} of those were cut short by the 25% partial-exit rule")
print(f"  {len(still_win_same)} of those exited the same way under the new rule (e.g. hit stop/cutoff before ever reaching 25% - contradiction check, should be ~0)")
print(f"  Original P&L from those {len(cut_short)} trades: ${sum(ot.pnl for ot,nt in cut_short):>+10,.2f}")
print(f"  New      P&L from those {len(cut_short)} trades: ${sum(nt.pnl for ot,nt in cut_short):>+10,.2f}")
print(f"  P&L given up specifically by cutting these winners short: ${pnl_cost:>+10,.2f}")

# ── other side: originally-losing trades that became winners under the new rule ──
orig_losers = [t for t in original_trades if t.pnl <= 0]
flipped_to_win = []
for ot in orig_losers:
    key = (ot.date, ot.direction)
    nt = partial_by_key.get(key)
    if nt is not None and nt.pnl > 0:
        flipped_to_win.append((ot, nt))
gain_from_flips = sum(nt.pnl - ot.pnl for ot, nt in flipped_to_win)
print(f"\n  For context - the other side of the ledger:")
print(f"  {len(flipped_to_win)} originally-LOSING trades became winners under the new rule "
      f"(previously reached >=25% before reversing into the stop)")
print(f"  P&L gained from those flips: ${gain_from_flips:>+10,.2f}")
print(f"\n  Net change vs original: ${part_stats['total_pnl'] - orig_stats['total_pnl']:>+10,.2f}  "
      f"(cost from cut-short winners ${pnl_cost:>+,.2f}  +  gain from flipped losers ${gain_from_flips:>+,.2f})")
