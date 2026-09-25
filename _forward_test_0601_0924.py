#!/usr/bin/env python3
"""
Genuine forward validation: run all three validated strategies (OR/POC
weekly $0.05/3.0R, AMD fade am15/$0.05/simple/$0.15/3.5R, VTC $0.25/$0.15/
3.5R) over 2026-06-01 .. 2026-09-24 - the window immediately after the
original 2023-07-01..2026-05-29 backtest data ends, using data that did not
exist when any of these strategies were designed or grid-searched.

Real ThetaData intraday bid/ask fills throughout (ThetaTerminal running
locally), fresh Alpaca 1-min SPY bars for the underlying (cached already by
a prior run: spy_1min_alpaca_2026-06-01_2026-09-24.pkl).

Note on methodology: build_or_poc_weekly_signals() in hermes_research_round2
gates on theta_0dte_usable(ds)/snap_to_real_strike(ds,...), both of which
only ever check a LOCAL DISK CACHE of same-day (0DTE) quote files
(theta_SPY_{ds}.pkl) - a cache that was never built for these forward
months and never will be (they're not "the historical dataset"). Since we
only trade the WEEKLY contract here (never 0DTE) and fetch its quotes live,
that 0DTE-cache gate is reproduced here via find_weekly_contract's own
live existence check instead - if ThetaData has no real weekly quotes for a
signal, the trade is simply dropped, exactly as it would be in the original
pipeline. Everything else (signal detection, entry/stop/target/exit-walk,
real bid/ask fill logic) is the unmodified function from each strategy's
own script - no reimplementation of any trading logic.
"""
import sys
from datetime import date

sys.path.insert(0, r'C:\Users\sagla')
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')

from alpaca_loader import download_stock_1min
import hermes_research_round2 as h
from _amd_fade_breakout import build_amd_signals, fetch_amd_quotes, simulate_amd_trade
from _vwap_trend_continuation import build_vtc_signals, fetch_vtc_quotes, simulate_vtc_trade

START, END = date(2026, 6, 1), date(2026, 9, 24)

print(f"Forward-test window: {START} .. {END} (the original backtest covered up to 2026-05-29)")

days_1min = download_stock_1min('SPY', START, END, force=False)
dates = sorted(days_1min.keys())
print(f"{len(dates)} trading days loaded: {dates[0]} .. {dates[-1]}")


def to_5min(bars_1min):
    buckets = {}
    for b in bars_1min:
        t = h._et(b)
        floor_min = (t.minute // 5) * 5
        key = t.replace(minute=floor_min, second=0, microsecond=0)
        buckets.setdefault(key, []).append(b)
    out = []
    for key in sorted(buckets):
        blist = buckets[key]
        out.append({
            't': int(key.timestamp() * 1000),
            'o': blist[0]['o'], 'h': max(x['h'] for x in blist),
            'l': min(x['l'] for x in blist), 'c': blist[-1]['c'],
            'v': sum(x['v'] for x in blist),
        })
    return out


spy_1min = days_1min
spy_5min = {ds: to_5min(bars) for ds, bars in days_1min.items()}
spy_daily_old = h.load_spy_daily()   # only used by build_amd_signals for prev_trading_day; harmless if it misses

print("\nRefreshing live expirations list (daily-TTL cache, per the just-applied fix)...")
all_exps = h.get_all_theta_expirations()
print(f"  {len(all_exps)} expirations known, max={all_exps[-1]}")


# ── OR/POC weekly: custom signal builder that skips the 0DTE-disk-cache
#    gates (see module docstring) but is otherwise identical to
#    build_or_poc_weekly_signals -----------------------------------------
def build_or_poc_weekly_signals_forward(spy_1min, spy_5min, dates, all_exps):
    signals = []
    coverage = {'total_days': 0, 'no_signal': 0, 'signal_days': 0, 'no_weekly_exp': 0}
    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < h.OR_POC_MIN + 5 or len(bars_5min) < 4:
            continue
        coverage['total_days'] += 1
        sig = h.find_or_poc_signal(ds, bars_1min, bars_5min)
        if sig is None:
            coverage['no_signal'] += 1
            continue
        exp = h.nearest_weekly_expiration(ds, all_exps, h.WEEKLY_DTE_MIN, h.WEEKLY_DTE_MAX)
        if exp is None:
            coverage['no_weekly_exp'] += 1
            continue
        sig['expiration'] = exp
        sig['dte'] = (exp - date.fromisoformat(ds)).days
        coverage['signal_days'] += 1
        signals.append(sig)
    return signals, coverage


print(f"\n{'='*100}\n1. OR/POC WEEKLY (stop=$0.05, target R:R={h.OR_POC_RR})\n{'='*100}")
signals_orpoc, cov_orpoc = build_or_poc_weekly_signals_forward(spy_1min, spy_5min, dates, all_exps)
print(f"  coverage: {cov_orpoc}")
print(f"  fetching real weekly quotes for {len(signals_orpoc)} signals...")
for n, sig in enumerate(signals_orpoc, 1):
    right = 'C' if sig['direction'] == 'c' else 'P'
    strike, chain_list = h.find_weekly_contract(
        sig['ds'], sig['expiration'], sig['strike'], right,
        sig['entry_dt'].strftime('%H:%M:%S'), '13:30:00')
    if chain_list:
        sig['strike'] = strike
        sig['chain'] = {r['hm']: r for r in chain_list}
    else:
        sig['chain'] = None
    if n % 20 == 0:
        print(f"    {n}/{len(signals_orpoc)}  api_calls={h.THETA_API_CALLS}  api_time={h.THETA_API_SECONDS:.1f}s")

orpoc_trades = []
for sig in signals_orpoc:
    t = h._simulate_trade_weekly(sig, 0.05)
    if t is not None:
        orpoc_trades.append(t)
print(f"  {len(orpoc_trades)}/{len(signals_orpoc)} signals produced a real fillable trade "
      f"(rest had no live weekly quote, or bad entry/stop geometry)")


# ── AMD fade (reuse build_amd_signals/fetch_amd_quotes/simulate_amd_trade
#    from the strategy script verbatim) ----------------------------------
print(f"\n{'='*100}\n2. AMD FADE-BREAKOUT (am15, $0.05 sweep buffer, simple confirm, "
      f"$0.15 stop buffer, 3.5R target)\n{'='*100}")
amd_signals, amd_cov = build_amd_signals(spy_1min, spy_5min, spy_daily_old, dates,
                                          'am15', 0.05, 'simple', all_exps)
print(f"  coverage: {amd_cov}")
print(f"  fetching real weekly quotes for {len(amd_signals)} signals...")
fetch_amd_quotes(amd_signals, progress_every=20)

amd_trades = []
for sig in amd_signals:
    t = simulate_amd_trade(sig, 0.15, 3.5)
    if t is not None:
        amd_trades.append(t)
print(f"  {len(amd_trades)}/{len(amd_signals)} signals produced a real fillable trade")


# ── VTC (reuse build_vtc_signals/fetch_vtc_quotes/simulate_vtc_trade
#    verbatim) -------------------------------------------------------------
print(f"\n{'='*100}\n3. VWAP TREND CONTINUATION (tolerance=$0.25, stop buffer=$0.15, "
      f"target R:R=3.5)\n{'='*100}")
vtc_signals, vtc_cov = build_vtc_signals(spy_1min, spy_5min, dates, 0.25, all_exps)
print(f"  coverage: {vtc_cov}")
print(f"  fetching real weekly quotes for {len(vtc_signals)} signals...")
fetch_vtc_quotes(vtc_signals, progress_every=20)

vtc_trades = []
for sig in vtc_signals:
    t = simulate_vtc_trade(sig, 0.15, 3.5)
    if t is not None:
        vtc_trades.append(t)
print(f"  {len(vtc_trades)}/{len(vtc_signals)} signals produced a real fillable trade")


# ── Report ────────────────────────────────────────────────────────────────
print(f"\n{'='*100}\n  FORWARD-TEST RESULTS: {START} .. {END}  "
      f"({len(dates)} trading days, never seen during strategy design/grid-search)\n{'='*100}")

for label, trades in [('OR/POC weekly ($0.05/3.0R)', orpoc_trades),
                       ('AMD fade (am15/$0.05/simple/$0.15/3.5R)', amd_trades),
                       ('VTC ($0.25/$0.15/3.5R)', vtc_trades)]:
    s = h.calc_stats(trades)
    if s['n'] == 0:
        print(f"  {label:<42}  N=0 -- no fillable trades in this window")
        continue
    print(f"  {label:<42}  N={s['n']:>4}  WR={s['wr']:>6.1f}%  PF={h.fmt_pf(s):>6}  "
          f"Total P&L=${s['total_pnl']:>+10,.2f}  AvgWin=${s['avg_win']:>7.2f}  "
          f"AvgLoss=${s['avg_loss']:>8.2f}  MaxDD=${s['max_dd']:>9,.2f}")

print(f"\n  Total ThetaData API calls: {h.THETA_API_CALLS}  ({h.THETA_API_SECONDS:.1f}s API time)")

import csv as _csv
out_path = h.DATA_DIR.parent / 'spy-0dte-trader' / 'forward_test_0601_0924_trades.csv'
with open(out_path, 'w', newline='') as f:
    w = _csv.writer(f)
    w.writerow(['strategy', 'date', 'entry_time', 'exit_time', 'direction', 'strike',
                'expiration', 'dte', 'entry_spy', 'stop_spy', 'target_spy', 'exit_spy',
                'exit_reason', 'entry_option', 'exit_option', 'pnl'])
    for label, trades in [('OR_POC', orpoc_trades), ('AMD', amd_trades), ('VTC', vtc_trades)]:
        for t in trades:
            w.writerow([label, t.date, t.entry_time, t.exit_time, t.direction, t.strike,
                        t.expiration, t.dte, t.entry_spy, t.stop_spy, t.target_spy, t.exit_spy,
                        t.exit_reason, t.entry_option, t.exit_option, t.pnl])
print(f"\n  Wrote all trades -> {out_path}")
