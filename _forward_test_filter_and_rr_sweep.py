#!/usr/bin/env python3
"""
Two follow-up checks on the same 2026-06-01..2026-09-24 forward window as
_forward_test_0601_0924.py:

  1. OR/POC weekly with the 0DTE-volume top-tercile filter applied (same
     method as _or_poc_volume_filter_validation.py: real 0DTE options
     volume summed per day, tercile split computed over THIS window's own
     dates, top-tercile-volume days excluded) vs the unfiltered result.
     0DTE volume is fetched live via option_history_eod (real ThetaData
     call, one per day) since no local theta_SPY_{ds}.pkl cache exists for
     these forward months - each day's real chain is cached to disk in
     that exact filename/format as a side effect, extending the same
     local cache convention used everywhere else in this project.

  2. All three strategies re-simulated at target R:R=2.0 (same stop widths,
     same entry logic, same signals) vs their current best-combo R:R
     (OR/POC 3.0, AMD/VTC 3.5). Re-simulating a different R:R needs no new
     ThetaData calls - fetch_intraday_chain_weekly's disk cache already
     covers the full entry->cutoff window per signal regardless of R:R, so
     this is a pure re-run of each script's own simulate_*_trade with a
     different target_rr argument.
"""
import sys
import pickle
from datetime import date

sys.path.insert(0, r'C:\Users\sagla')
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')

from alpaca_loader import download_stock_1min
import hermes_research_round2 as h
from _amd_fade_breakout import build_amd_signals, fetch_amd_quotes, simulate_amd_trade
from _vwap_trend_continuation import build_vtc_signals, fetch_vtc_quotes, simulate_vtc_trade

START, END = date(2026, 6, 1), date(2026, 9, 24)

days_1min = download_stock_1min('SPY', START, END, force=False)
dates = sorted(days_1min.keys())
print(f"{len(dates)} trading days: {dates[0]} .. {dates[-1]}")


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
        out.append({'t': int(key.timestamp() * 1000), 'o': blist[0]['o'],
                     'h': max(x['h'] for x in blist), 'l': min(x['l'] for x in blist),
                     'c': blist[-1]['c'], 'v': sum(x['v'] for x in blist)})
    return out


spy_1min = days_1min
spy_5min = {ds: to_5min(bars) for ds, bars in days_1min.items()}
spy_daily_old = h.load_spy_daily()
all_exps = h.get_all_theta_expirations()


def build_or_poc_weekly_signals_forward(spy_1min, spy_5min, dates, all_exps):
    signals = []
    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < h.OR_POC_MIN + 5 or len(bars_5min) < 4:
            continue
        sig = h.find_or_poc_signal(ds, bars_1min, bars_5min)
        if sig is None:
            continue
        exp = h.nearest_weekly_expiration(ds, all_exps, h.WEEKLY_DTE_MIN, h.WEEKLY_DTE_MAX)
        if exp is None:
            continue
        sig['expiration'] = exp
        sig['dte'] = (exp - date.fromisoformat(ds)).days
        signals.append(sig)
    return signals


print(f"\n{'='*100}\nRebuilding signals (deterministic, no API cost) + fetching real weekly quotes "
      f"(disk-cache-hit for anything already fetched by the prior forward-test run)\n{'='*100}")

orpoc_signals = build_or_poc_weekly_signals_forward(spy_1min, spy_5min, dates, all_exps)
for sig in orpoc_signals:
    right = 'C' if sig['direction'] == 'c' else 'P'
    strike, chain_list = h.find_weekly_contract(
        sig['ds'], sig['expiration'], sig['strike'], right,
        sig['entry_dt'].strftime('%H:%M:%S'), '13:30:00')
    sig['strike'] = strike if chain_list else sig['strike']
    sig['chain'] = {r['hm']: r for r in chain_list} if chain_list else None
print(f"  OR/POC: {len(orpoc_signals)} signals, "
      f"{sum(1 for s in orpoc_signals if s['chain'])} with a real chain")

amd_signals, _ = build_amd_signals(spy_1min, spy_5min, spy_daily_old, dates, 'am15', 0.05, 'simple', all_exps)
fetch_amd_quotes(amd_signals, progress_every=0)
print(f"  AMD:    {len(amd_signals)} signals, "
      f"{sum(1 for s in amd_signals if s.get('chain'))} with a real chain")

vtc_signals, _ = build_vtc_signals(spy_1min, spy_5min, dates, 0.25, all_exps)
fetch_vtc_quotes(vtc_signals, progress_every=0)
print(f"  VTC:    {len(vtc_signals)} signals, "
      f"{sum(1 for s in vtc_signals if s.get('chain'))} with a real chain")

print(f"  api_calls so far: {h.THETA_API_CALLS}  ({h.THETA_API_SECONDS:.1f}s) "
      f"-- should be ~0 new calls if the prior forward-test run already populated the disk cache")


# ═══════════════════════════════════════════════════════════════════════
# CHECK 1: OR/POC 0DTE-volume top-tercile filter (real live 0DTE volume)
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*100}\n  CHECK 1: OR/POC WEEKLY - 0DTE-VOLUME TOP-TERCILE FILTER (real live volume)\n{'='*100}")


def fetch_0dte_day_volume(ds: str) -> float:
    """Real same-day (0DTE) options volume, summed across the full chain.
    Caches to backtest_data/theta_SPY_{ds}.pkl in the SAME format/location
    hermes_research_round2._load_theta_file already expects, so this also
    fixes theta_0dte_usable()/_load_theta_file() for these forward dates
    for any future script that wants it."""
    path = h.DATA_DIR / f'theta_SPY_{ds}.pkl'
    if path.exists() and path.stat().st_size > 5:
        with path.open('rb') as f:
            chain = pickle.load(f)
    else:
        client = h.get_theta_client()
        exp = date.fromisoformat(ds)
        try:
            df = h._theta_retry(lambda: client.option_history_eod(
                start_date=exp, end_date=exp, symbol='SPY', expiration=exp,
                right='both', strike_range=40))
        except Exception as e:
            print(f"    0DTE chain fetch FAILED for {ds}: {type(e).__name__}: {e}")
            df = None
        chain = {}
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                right = 'C' if str(row['right']).upper().startswith('C') else 'P'
                key = (ds, float(row['strike']), right)
                chain[key] = {'bid': float(row['bid']), 'ask': float(row['ask']),
                              'volume': float(row['volume']), 'close': float(row['close'])}
        with path.open('wb') as f:
            pickle.dump(chain, f)
    return sum(q.get('volume', 0) for k, q in chain.items() if k[0] == ds)


print(f"  Fetching real 0DTE chain volume for all {len(dates)} forward-window days "
      f"(cached to theta_SPY_{{ds}}.pkl per-day, same format as the original dataset)...")
daily_vol = {}
for i, ds in enumerate(dates, 1):
    v = fetch_0dte_day_volume(ds)
    if v > 0:
        daily_vol[ds] = v
    if i % 20 == 0:
        print(f"    {i}/{len(dates)}  api_calls={h.THETA_API_CALLS}  api_time={h.THETA_API_SECONDS:.1f}s")

vol_sorted = sorted(daily_vol.items(), key=lambda kv: kv[1])
n_vol = len(vol_sorted)
t1, t2 = n_vol // 3, 2 * n_vol // 3
low_days  = set(d for d, v in vol_sorted[:t1])
mid_days  = set(d for d, v in vol_sorted[t1:t2])
high_days = set(d for d, v in vol_sorted[t2:])
low_mid_days = low_days | mid_days
print(f"  {n_vol} days with real 0DTE volume; tercile sizes: low={len(low_days)} "
      f"mid={len(mid_days)} high={len(high_days)}")

orpoc_trades_unfiltered = [t for sig in orpoc_signals if (t := h._simulate_trade_weekly(sig, 0.05)) is not None]
orpoc_trades_filtered   = [t for t in orpoc_trades_unfiltered if t.date in low_mid_days]
orpoc_trades_excluded   = [t for t in orpoc_trades_unfiltered if t.date in high_days]

s_unf = h.calc_stats(orpoc_trades_unfiltered)
s_filt = h.calc_stats(orpoc_trades_filtered)
s_excl = h.calc_stats(orpoc_trades_excluded)
print(f"\n  UNFILTERED        N={s_unf['n']:>4}  WR={s_unf['wr']:>6.1f}%  PF={h.fmt_pf(s_unf):>6}  "
      f"Total P&L=${s_unf['total_pnl']:>+10,.2f}")
print(f"  LOW+MEDIUM VOL    N={s_filt['n']:>4}  WR={s_filt['wr']:>6.1f}%  PF={h.fmt_pf(s_filt):>6}  "
      f"Total P&L=${s_filt['total_pnl']:>+10,.2f}")
print(f"  (excluded) HIGH   N={s_excl['n']:>4}  WR={s_excl['wr']:>6.1f}%  PF={h.fmt_pf(s_excl):>6}  "
      f"Total P&L=${s_excl['total_pnl']:>+10,.2f}")


# ═══════════════════════════════════════════════════════════════════════
# CHECK 2: 2.0R vs current best-combo R:R, all three strategies
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*100}\n  CHECK 2: TARGET R:R = 2.0 vs CURRENT, SAME STOPS/SIGNALS, ALL THREE STRATEGIES\n{'='*100}")

# OR/POC: _simulate_trade_weekly's target uses the module-level OR_POC_RR
# constant, not a parameter - patch it for the 2.0R run, restore after.
orig_rr = h.OR_POC_RR
h.OR_POC_RR = 2.0
orpoc_trades_2r = [t for sig in orpoc_signals if (t := h._simulate_trade_weekly(sig, 0.05)) is not None]
h.OR_POC_RR = orig_rr

amd_trades_35r = [t for sig in amd_signals if (t := simulate_amd_trade(sig, 0.15, 3.5)) is not None]
amd_trades_2r  = [t for sig in amd_signals if (t := simulate_amd_trade(sig, 0.15, 2.0)) is not None]

vtc_trades_35r = [t for sig in vtc_signals if (t := simulate_vtc_trade(sig, 0.15, 3.5)) is not None]
vtc_trades_2r  = [t for sig in vtc_signals if (t := simulate_vtc_trade(sig, 0.15, 2.0)) is not None]

rows = [
    ('OR/POC weekly  stop=$0.05  R:R=3.0 (current, unfiltered)', orpoc_trades_unfiltered),
    ('OR/POC weekly  stop=$0.05  R:R=2.0',                        orpoc_trades_2r),
    ('AMD fade       stop=$0.15  R:R=3.5 (current)',              amd_trades_35r),
    ('AMD fade       stop=$0.15  R:R=2.0',                        amd_trades_2r),
    ('VTC            stop=$0.15  R:R=3.5 (current)',              vtc_trades_35r),
    ('VTC            stop=$0.15  R:R=2.0',                        vtc_trades_2r),
]
print(f"\n  {'Strategy/config':<52}{'N':>5}{'WR':>8}{'PF':>8}{'Total P&L':>14}")
for label, trades in rows:
    s = h.calc_stats(trades)
    print(f"  {label:<52}{s['n']:>5}{s['wr']:>7.1f}%{h.fmt_pf(s):>8}{s['total_pnl']:>+14,.2f}")

print(f"\n{'='*100}\n  FULL COMPARISON TABLE - forward window {START}..{END} ({len(dates)} days)\n{'='*100}")
print(f"\n  {'Config':<58}{'N':>5}{'WR':>8}{'PF':>8}{'Total P&L':>14}")
all_rows = [
    ('OR/POC  $0.05 stop / 3.0R  UNFILTERED',            orpoc_trades_unfiltered),
    ('OR/POC  $0.05 stop / 3.0R  0DTE-vol filtered',     orpoc_trades_filtered),
    ('OR/POC  $0.05 stop / 2.0R  UNFILTERED',            orpoc_trades_2r),
    ('AMD     $0.15 stop / 3.5R  (current best)',        amd_trades_35r),
    ('AMD     $0.15 stop / 2.0R',                        amd_trades_2r),
    ('VTC     $0.15 stop / 3.5R  (current best)',        vtc_trades_35r),
    ('VTC     $0.15 stop / 2.0R',                        vtc_trades_2r),
]
for label, trades in all_rows:
    s = h.calc_stats(trades)
    print(f"  {label:<58}{s['n']:>5}{s['wr']:>7.1f}%{h.fmt_pf(s):>8}{s['total_pnl']:>+14,.2f}")

print(f"\n  Total ThetaData API calls this run: {h.THETA_API_CALLS}  ({h.THETA_API_SECONDS:.1f}s)")
