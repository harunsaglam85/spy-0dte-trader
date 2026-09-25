#!/usr/bin/env python3
"""Sanity-check pass on the OR/POC weekly backtest, requested after the
first weekly-vs-0DTE run came back with a negative chronological OOS split:

  1. (documented in the accompanying report, not here - a straight code
     read of find_or_poc_signal/calc_poc for look-ahead bias)
  2. Odd/even (interleaved) IS/OOS split on the best weekly combo
     (stop=$0.05), to compare against the chronological 70/30 split.
  3. Hand spot-check of 5 random trades, recomputing OR high/low and a
     POC estimate via INDEPENDENT logic (plain min/max and a
     coarser-bucket volume histogram) straight from the raw 1-min bars,
     not by re-calling calc_poc/find_or_poc_signal/_simulate_trade_weekly
     - the point is to not just re-run the same code and call it verified.
  4. Prints the raw chain quotes at the recorded entry/exit minute so the
     fill can be checked against the real bid/ask series by eye.

Uses only cached data - no new ThetaData calls.
"""
import sys, csv, random
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
from datetime import date, timedelta
from collections import defaultdict

STOP = 0.05

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
all_dates = sorted(spy_daily.keys())
dates = [d for d in all_dates if '2023-07-01' <= d <= '2026-05-30']

print("Rebuilding weekly signals/trades (cache-hit, no new API calls)...")
results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, [STOP])
trades = results_w[STOP]
print(f"  {len(trades)} weekly trades at stop=${STOP}")

# ─────────────────────────────────────────────────────────────────────────
# 2. Odd/even interleaved split
# ─────────────────────────────────────────────────────────────────────────
print(f"\n{'='*90}\n  2. ODD/EVEN INTERLEAVED SPLIT vs CHRONOLOGICAL 70/30\n{'='*90}")
trades_sorted = sorted(trades, key=lambda t: (t.date, t.entry_time))
even_trades = trades_sorted[0::2]
odd_trades  = trades_sorted[1::2]
s_all  = h.calc_stats(trades_sorted)
s_even = h.calc_stats(even_trades)
s_odd  = h.calc_stats(odd_trades)
print(f"  ALL   N={s_all['n']:>4}  WR={s_all['wr']:>6.1f}%  PF={h.fmt_pf(s_all):>6}  Total P&L=${s_all['total_pnl']:>+10,.2f}")
print(f"  EVEN  N={s_even['n']:>4}  WR={s_even['wr']:>6.1f}%  PF={h.fmt_pf(s_even):>6}  Total P&L=${s_even['total_pnl']:>+10,.2f}   (trade #0,2,4,...)")
print(f"  ODD   N={s_odd['n']:>4}  WR={s_odd['wr']:>6.1f}%  PF={h.fmt_pf(s_odd):>6}  Total P&L=${s_odd['total_pnl']:>+10,.2f}   (trade #1,3,5,...)")

# same split but on signal DAYS (not raw trade rows) in case of same-day
# duplicate entries - alternate by day index instead of trade-row index
days_sorted = sorted({t.date for t in trades})
even_days = set(days_sorted[0::2])
odd_days  = set(days_sorted[1::2])
s_even_d = h.calc_stats([t for t in trades if t.date in even_days])
s_odd_d  = h.calc_stats([t for t in trades if t.date in odd_days])
print(f"\n  -- same split, by alternating signal DAY instead of trade row --")
print(f"  EVEN days  N={s_even_d['n']:>4}  WR={s_even_d['wr']:>6.1f}%  PF={h.fmt_pf(s_even_d):>6}  Total P&L=${s_even_d['total_pnl']:>+10,.2f}")
print(f"  ODD  days  N={s_odd_d['n']:>4}  WR={s_odd_d['wr']:>6.1f}%  PF={h.fmt_pf(s_odd_d):>6}  Total P&L=${s_odd_d['total_pnl']:>+10,.2f}")

# ─────────────────────────────────────────────────────────────────────────
# 3+4. Hand spot-check of 5 random trades
# ─────────────────────────────────────────────────────────────────────────
print(f"\n{'='*90}\n  3+4. HAND SPOT-CHECK OF 5 RANDOM TRADES\n{'='*90}")
random.seed(20260919)
sample = random.sample(trades, 5)

def independent_or_high_low(bars_1min, ds):
    """Plain min/max over 9:30-9:44 bars - no calc_poc, no find_or_poc_signal."""
    or_bars = [b for b in bars_1min if (9, 30) <= (h._et(b).hour, h._et(b).minute) < (9, 45)]
    highs = [b['h'] for b in or_bars]
    lows  = [b['l'] for b in or_bars]
    return (max(highs), min(lows), len(or_bars)) if or_bars else (None, None, 0)

def independent_poc_estimate(bars_1min, bucket=0.05):
    """Different bucket size (0.05 vs calc_poc's 0.01) and a simpler
    single-point-per-bar (typical price) volume tally instead of
    calc_poc's spread-across-the-bar-range approach - an intentionally
    DIFFERENT algorithm, used only to sanity-check calc_poc's result
    lands in a plausible spot, not to reproduce it exactly."""
    or_bars = [b for b in bars_1min if (9, 30) <= (h._et(b).hour, h._et(b).minute) < (9, 45)]
    vol_by_bucket = defaultdict(float)
    for b in or_bars:
        tp = round((b['h'] + b['l'] + b['c']) / 3.0 / bucket) * bucket
        vol_by_bucket[tp] += b['v']
    if not vol_by_bucket:
        return None
    return max(vol_by_bucket.items(), key=lambda kv: kv[1])[0]

for i, t in enumerate(sample, 1):
    ds = t.date
    bars_1min = spy_1min[ds]
    bars_5min = spy_5min[ds]
    ind_high, ind_low, n_or_bars = independent_or_high_low(bars_1min, ds)
    ind_poc = independent_poc_estimate(bars_1min)

    print(f"\n--- Trade {i}: {ds}  {t.direction}  strike={t.strike}  exp={t.expiration} (DTE={t.dte}) ---")
    print(f"  RECORDED: or_high={t.or_high}  or_low={t.or_low}  poc={t.poc}")
    print(f"  INDEPENDENT (plain min/max, n={n_or_bars} OR bars): "
          f"or_high={ind_high:.2f}  or_low={ind_low:.2f}")
    print(f"  INDEPENDENT POC estimate (different algo, $0.05 buckets): ~{ind_poc:.2f}"
          f"   (calc_poc says {t.poc} - should be in the same neighborhood, not necessarily identical)")
    match_hi = abs(ind_high - t.or_high) < 0.005
    match_lo = abs(ind_low - t.or_low) < 0.005
    print(f"  OR high/low match: {'YES' if match_hi and match_lo else 'MISMATCH -- INVESTIGATE'}")

    # entry/exit bar-level sanity: locate the recorded entry/exit minute
    # bars and print raw OHLC around them
    entry_bar = next((b for b in bars_1min if h._et(b).strftime('%H:%M') == t.entry_time), None)
    exit_bar  = next((b for b in bars_1min if h._et(b).strftime('%H:%M') == t.exit_time), None)
    print(f"  Entry 1-min bar @ {t.entry_time}: {entry_bar}")
    print(f"  Exit  1-min bar @ {t.exit_time}: {exit_bar}")
    print(f"  RECORDED entry_spy={t.entry_spy}  stop_spy={t.stop_spy}  target_spy={t.target_spy}  "
          f"exit_spy={t.exit_spy}  exit_reason={t.exit_reason}")

    # verify the exit reason is consistent with the exit bar's OHLC
    if t.direction == 'CALL':
        stop_hit_here   = exit_bar is not None and exit_bar['l'] <= t.stop_spy
        target_hit_here = exit_bar is not None and exit_bar['h'] >= t.target_spy
    else:
        stop_hit_here   = exit_bar is not None and exit_bar['h'] >= t.stop_spy
        target_hit_here = exit_bar is not None and exit_bar['l'] <= t.target_spy
    print(f"  Exit-bar OHLC consistent with exit_reason '{t.exit_reason}': "
          f"stop_condition_true={stop_hit_here}  target_condition_true={target_hit_here}")

    # re-derive risk/target arithmetic by hand
    if t.direction == 'CALL':
        risk = t.entry_spy - t.stop_spy
        hand_target = round(t.entry_spy + 3.0 * risk, 2)
    else:
        risk = t.stop_spy - t.entry_spy
        hand_target = round(t.entry_spy - 3.0 * risk, 2)
    print(f"  Hand-recomputed target (entry {'+ ' if t.direction=='CALL' else '- '}3x risk of "
          f"${risk:.2f}): {hand_target}  (recorded: {t.target_spy})  "
          f"{'MATCH' if abs(hand_target - t.target_spy) < 0.01 else 'MISMATCH'}")

    # option quotes at entry/exit minute, straight from the cached chain
    sig = next((s for s in signals_w if s['ds'] == ds and s['direction'] == t.direction[0].lower()), None)
    if sig and sig.get('chain'):
        chain = sig['chain']
        eq = chain.get(t.entry_time)
        xq = chain.get(t.exit_time)
        print(f"  Raw chain quote AT entry minute {t.entry_time}: {eq}")
        print(f"  Raw chain quote AT exit  minute {t.exit_time}: {xq}")
        print(f"  RECORDED entry_ask={t.entry_ask}  entry_bid={t.entry_bid}  "
              f"exit_bid={t.exit_bid}  exit_ask={t.exit_ask}")
        print(f"  P&L = (exit_bid - entry_ask) * 100 = "
              f"({t.exit_bid} - {t.entry_ask}) * 100 = {round((t.exit_bid - t.entry_ask)*100, 2)}  "
              f"(recorded pnl: {t.pnl})")
