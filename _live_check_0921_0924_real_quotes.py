#!/usr/bin/env python3
"""
Follow-up to _live_check_0921_0924.py: ThetaTerminal is now running with a
real subscription, so this fetches REAL historical intraday option bid/ask
for the OR/POC weekly, AMD fade, and VTC signals found on 2026-09-21..24,
using the exact simulate_*_trade functions from each strategy script (no
reimplementation). The only wrinkle: get_all_theta_expirations() caches a
stale expirations list to disk that predates these weeklies being listed -
so this re-fetches the live list directly and uses it in place of the cache.
"""
import sys
from datetime import date

sys.path.insert(0, r'C:\Users\sagla')
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')

from alpaca_loader import download_stock_1min
import hermes_research_round2 as h
from _amd_fade_breakout import find_amd_signal, simulate_amd_trade, fetch_amd_quotes
from _vwap_trend_continuation import find_vtc_signal, simulate_vtc_trade, fetch_vtc_quotes

TARGET_DATES = ['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24']

days_1min = download_stock_1min('SPY', date(2026, 9, 15), date(2026, 9, 24), force=False)


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


# ── Fresh live expirations list (bypass the stale on-disk cache) ──────────
print("Fetching LIVE expirations list from ThetaData (bypassing stale disk cache)...")
client = h.get_theta_client()
raw = h._theta_retry(lambda: client.option_list_expirations('SPY'))
live_exps = sorted(date.fromisoformat(str(e)) for e in raw['expiration'])
print(f"  {len(live_exps)} expirations live, max={live_exps[-1]}")


def nearest_weekly(ds):
    return h.nearest_weekly_expiration(ds, live_exps, h.WEEKLY_DTE_MIN, h.WEEKLY_DTE_MAX)


print(f"{'='*100}\nREAL-QUOTE CHECK -- {TARGET_DATES}\n{'='*100}")

for ds in TARGET_DATES:
    bars_1min = days_1min.get(ds, [])
    bars_5min = to_5min(bars_1min)
    print(f"\n{'#'*100}\n### {ds}\n{'#'*100}")

    exp = nearest_weekly(ds)
    print(f"  Nearest weekly expiration (3-5 DTE, live list): {exp}")

    # ---------------- OR/POC weekly ----------------
    sig = h.find_or_poc_signal(ds, bars_1min, bars_5min)
    print(f"\n  -- OR/POC weekly (stop=$0.05, target R:R={h.OR_POC_RR}) --")
    if sig is None:
        print("     NO SIGNAL.")
    elif exp is None:
        print("     Signal fired but no weekly expiration in DTE band -- skipping quote fetch.")
    else:
        sig['expiration'] = exp
        sig['dte'] = (exp - date.fromisoformat(ds)).days
        right = 'C' if sig['direction'] == 'c' else 'P'
        strike, chain_list = h.find_weekly_contract(
            ds, exp, sig['strike'], right, sig['entry_dt'].strftime('%H:%M:%S'), '13:30:00')
        if not chain_list:
            print(f"     SIGNAL: {('CALL' if sig['direction']=='c' else 'PUT')} strike~{sig['strike']} "
                  f"exp={exp} -- NO REAL QUOTE DATA FOUND for this contract/day.")
        else:
            sig['strike'] = strike
            sig['chain'] = {r['hm']: r for r in chain_list}
            t = h._simulate_trade_weekly(sig, 0.05)
            if t is None:
                print("     Signal fired, real chain found, but simulate returned None "
                      "(bad entry/stop geometry or missing entry ask).")
            else:
                print(f"     {t.direction} strike={t.strike} exp={t.expiration} (DTE={t.dte})")
                print(f"     entry {t.entry_time} SPY=${t.entry_spy} option_ask=${t.entry_option}  "
                      f"exit {t.exit_time} SPY=${t.exit_spy} option_bid=${t.exit_option}  "
                      f"reason={t.exit_reason}")
                print(f"     REAL P&L (per contract, x100): ${t.pnl:+,.2f}  "
                      f"-> {'WIN' if t.pnl > 0 else 'LOSS'}")

    # ---------------- AMD fade ----------------
    print(f"\n  -- AMD fade-breakout (am15, $0.05 buffer, simple confirm, $0.15 stop, 3.5R) --")
    sig = find_amd_signal(bars_1min, bars_5min, None, 'am15', 0.05, 'simple')
    if sig is None:
        print("     NO SIGNAL.")
    elif exp is None:
        print("     Signal fired but no weekly expiration in DTE band -- skipping quote fetch.")
    else:
        sig['ds'] = ds
        sig['variant'] = 'am15'
        sig['buffer'] = 0.05
        sig['confirm_mode'] = 'simple'
        sig['expiration'] = exp
        sig['dte'] = (exp - date.fromisoformat(ds)).days
        fetch_amd_quotes([sig], progress_every=0)
        if not sig.get('chain'):
            print(f"     SIGNAL: {('CALL' if sig['direction']=='c' else 'PUT')} strike~{sig['strike']} "
                  f"exp={exp} -- NO REAL QUOTE DATA FOUND for this contract/day.")
        else:
            t = simulate_amd_trade(sig, 0.15, 3.5)
            if t is None:
                print("     Signal fired, real chain found, but simulate returned None.")
            else:
                print(f"     {t.direction} strike={t.strike} exp={t.expiration} (DTE={t.dte})")
                print(f"     entry {t.entry_time} SPY=${t.entry_spy} option_ask=${t.entry_option}  "
                      f"exit {t.exit_time} SPY=${t.exit_spy} option_bid=${t.exit_option}  "
                      f"reason={t.exit_reason}")
                print(f"     REAL P&L (per contract, x100): ${t.pnl:+,.2f}  "
                      f"-> {'WIN' if t.pnl > 0 else 'LOSS'}")

    # ---------------- VTC ----------------
    print(f"\n  -- VWAP Trend Continuation (tolerance=$0.25, stop buffer=$0.15, target R:R=3.5) --")
    sig = find_vtc_signal(bars_1min, bars_5min, 0.25)
    if sig is None:
        print("     NO SIGNAL.")
    elif exp is None:
        print("     Signal fired but no weekly expiration in DTE band -- skipping quote fetch.")
    else:
        sig['ds'] = ds
        sig['tolerance'] = 0.25
        sig['expiration'] = exp
        sig['dte'] = (exp - date.fromisoformat(ds)).days
        fetch_vtc_quotes([sig], progress_every=0)
        if not sig.get('chain'):
            print(f"     SIGNAL: {('CALL' if sig['direction']=='c' else 'PUT')} strike~{sig['strike']} "
                  f"exp={exp} -- NO REAL QUOTE DATA FOUND for this contract/day.")
        else:
            t = simulate_vtc_trade(sig, 0.15, 3.5)
            if t is None:
                print("     Signal fired, real chain found, but simulate returned None.")
            else:
                print(f"     {t.direction} strike={t.strike} exp={t.expiration} (DTE={t.dte})")
                print(f"     entry {t.entry_time} SPY=${t.entry_spy} option_ask=${t.entry_option}  "
                      f"exit {t.exit_time} SPY=${t.exit_spy} option_bid=${t.exit_option}  "
                      f"reason={t.exit_reason}")
                print(f"     REAL P&L (per contract, x100): ${t.pnl:+,.2f}  "
                      f"-> {'WIN' if t.pnl > 0 else 'LOSS'}")

print(f"\n{'='*100}\nDone. api_calls={h.THETA_API_CALLS} api_time={h.THETA_API_SECONDS:.1f}s\n{'='*100}")
