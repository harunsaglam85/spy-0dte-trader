#!/usr/bin/env python3
"""SPY momentum-continuation SHARE strategy - separate from the OR/POC options
infrastructure entirely. Trades 100 shares of SPY, not options: no ThetaData,
no bid/ask, no strike selection - just the already-cached 1-min SPY bars
(spy_1min_alpaca_*.pkl).

RULES:
- No trade before 10:00 AM ET.
- At 10:00 AM, check if SPY's price is >= 0.3% above the 9:30 AM open price.
- If yes: BUY 100 shares at the 10:00 AM price. If no: no trade that day.
- Exit ALL positions at 12:00 PM ET, regardless of price - no exceptions.
- Variant A: no stop-loss at all (only the 12:00 timed exit).
- Variant B: same entry/exit, plus a hard stop-loss at -1% from entry,
  checked continuously (1-min bar granularity) between 10:00 and 12:00.

Price convention (matches the "price at a given clock time" convention used
by the OR/POC engine elsewhere in this project, which reads OR-window prices
off 1-min bars): "the price at HH:MM" = the OPEN of the 1-min bar timestamped
HH:MM - the print at the instant that minute begins. Verified the cached
1-min data has exactly one bar per minute with no gaps in the 9:30-12:00
window, so this is an exact (not nearest-neighbor) lookup.

Stop-loss walk (variant B) starts at the bar AFTER the entry bar (same
"don't look inside the entry bar" convention the OR/POC stop/target walk
uses) and triggers on that bar's LOW <= stop price, filling at the stop
price itself (not the bar's low) - same fill assumption used throughout
this project's other stop-loss walks.
"""
import sys, csv
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
from datetime import time as dtime

DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'
SHARES        = 100
ENTRY_THRESH  = 0.003   # 0.3%
STOP_PCT      = 0.01    # 1%

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
dates = [d for d in sorted(spy_daily.keys()) if DATE_LO <= d <= DATE_HI]


class ShareTrade:
    __slots__ = ('date', 'entry_date', 'exit_date', 'entry_time', 'exit_time',
                 'entry_price', 'exit_price', 'exit_reason', 'pnl')

    def __init__(self, date, entry_time, exit_time, entry_price, exit_price, exit_reason):
        self.date = date
        self.entry_date = date
        self.exit_date = date
        self.entry_time = entry_time
        self.exit_time = exit_time
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.pnl = round((exit_price - entry_price) * SHARES, 2)


def bar_at_or_after(bars, hh, mm):
    """Index + bar of the first 1-min bar whose ET time is >= hh:mm."""
    for i, b in enumerate(bars):
        t = h._et(b)
        if (t.hour, t.minute) >= (hh, mm):
            return i, b
    return None, None


def build_trades(use_stop: bool):
    trades = []
    skipped_no_signal = 0
    skipped_no_data = 0
    for ds in dates:
        bars = spy_1min.get(ds, [])
        if not bars:
            skipped_no_data += 1
            continue

        open_930 = bars[0]['o']  # first bar of the regular session
        i1000, b1000 = bar_at_or_after(bars, 10, 0)
        if b1000 is None:
            skipped_no_data += 1
            continue
        entry_price = b1000['o']

        if entry_price < open_930 * (1 + ENTRY_THRESH):
            skipped_no_signal += 1
            continue

        i1200, b1200 = bar_at_or_after(bars, 12, 0)
        if b1200 is None:
            skipped_no_data += 1
            continue
        exit_price, exit_reason, exit_time = b1200['o'], '12:00 close', '12:00'

        if use_stop:
            stop_price = entry_price * (1 - STOP_PCT)
            for b2 in bars[i1000 + 1: i1200]:
                if b2['l'] <= stop_price:
                    exit_price = stop_price
                    exit_reason = 'stop (-1%)'
                    exit_time = h._et(b2).strftime('%H:%M')
                    break

        trades.append(ShareTrade(ds, '10:00', exit_time, entry_price, exit_price, exit_reason))
    return trades, skipped_no_signal, skipped_no_data


trades_a, skip_sig, skip_data = build_trades(use_stop=False)
trades_b, _, _ = build_trades(use_stop=True)

print(f"\n{len(dates)} candidate days -> {len(trades_a)} signal days triggered "
      f"({skip_sig} no-signal [<0.3% by 10am], {skip_data} missing-data)")

# ── write CSVs ───────────────────────────────────────────────────────────
for label, trades in [('A_no_stop', trades_a), ('B_stop1pct', trades_b)]:
    out_path = fr'C:\Users\sagla\spy-0dte-trader\trades_spy_momentum_{label}.csv'
    with open(out_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['date', 'entry_time', 'exit_time', 'entry_price', 'exit_price',
                     'exit_reason', 'pnl'])
        for t in trades:
            w.writerow([t.date, t.entry_time, t.exit_time, round(t.entry_price, 4),
                        round(t.exit_price, 4), t.exit_reason, t.pnl])
    print(f"Wrote -> {out_path}")

s_a = h.calc_stats(trades_a)
s_b = h.calc_stats(trades_b)

print(f"\n{'='*95}\n  SPY MOMENTUM CONTINUATION - VARIANT A (no stop) vs VARIANT B (-1% stop)\n{'='*95}")
for label, s in [('A: no-stop  ', s_a), ('B: -1% stop ', s_b)]:
    print(f"  {label} N={s['n']:>4}  WR={s['wr']:>6.1f}%  PF={h.fmt_pf(s):>6}  "
          f"Total P&L=${s['total_pnl']:>+10,.2f}  AvgWin=${s['avg_win']:>7.2f}  "
          f"AvgLoss=${s['avg_loss']:>8.2f}  MaxDD=${s['max_dd']:>9,.2f}")

# ── pick the better-performing variant (total P&L primary, PF as tiebreak) ─
if s_b['total_pnl'] != s_a['total_pnl']:
    better_label, better_trades, better_s = (
        ('B', trades_b, s_b) if s_b['total_pnl'] > s_a['total_pnl'] else ('A', trades_a, s_a))
else:
    better_label, better_trades, better_s = (
        ('B', trades_b, s_b) if s_b['pf'] >= s_a['pf'] else ('A', trades_a, s_a))
print(f"\n  Better-performing variant: {better_label}  "
      f"(N={better_s['n']}, PF={h.fmt_pf(better_s)}, P&L=${better_s['total_pnl']:+,.2f})")

# ── validation splits on the better variant ─────────────────────────────────
print(f"\n{'='*95}\n  VALIDATION SPLITS on variant {better_label}\n{'='*95}")
signal_dates = sorted({t.date for t in better_trades})
n_days = len(signal_dates)
split_idx = round(n_days * 0.70)
is_dates  = set(signal_dates[:split_idx])
oos_dates = set(signal_dates[split_idx:])
is_trades  = [t for t in better_trades if t.date in is_dates]
oos_trades = [t for t in better_trades if t.date in oos_dates]
s_is, s_oos = h.calc_stats(is_trades), h.calc_stats(oos_trades)

trades_sorted = sorted(better_trades, key=lambda t: t.date)
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

print(f"\n  -- Pass/fail (PF>=1.0 and positive P&L in every subset) --")
all_pass = True
for label, s in [('IS', s_is), ('OOS', s_oos), ('EVEN', s_even), ('ODD', s_odd)]:
    passed = s['pf'] >= 1.0 and s['total_pnl'] > 0
    all_pass = all_pass and passed
    print(f"  {label:<5} PF={h.fmt_pf(s):>6}  P&L=${s['total_pnl']:>+9,.2f}  -> {'PASS' if passed else 'FAIL'}")
print(f"\n  Clears both validation splits: {'YES' if all_pass else 'NO'}")
