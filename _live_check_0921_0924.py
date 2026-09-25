#!/usr/bin/env python3
"""
One-off live check: did OR/POC weekly, AMD fade, or VTC fire a signal on
2026-09-21 .. 2026-09-24, using fresh real Alpaca 1-min SPY bars (not the
cached backtest dataset, which stops 2026-05-30)? Reuses the exact signal
functions from the three strategy scripts / hermes_research_round2.py -
no reimplementation of the logic.
"""
import sys
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, r'C:\Users\sagla')
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')

from alpaca_loader import download_stock_1min
import hermes_research_round2 as h
from _amd_fade_breakout import find_amd_signal, simulate_amd_trade
from _vwap_trend_continuation import find_vtc_signal, simulate_vtc_trade

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


def simulate_spy_only(sig, stop_calc, target_calc, cutoff_hm):
    """Walk 1-min bars from entry using pure SPY price levels (no option
    quotes) to report exit_reason (stop/target/cutoff) and directional
    win/loss - used when no live option quote source is available."""
    direction = sig['direction']
    entry_spy = sig['entry_spy']
    stop_spy, target_spy = stop_calc(sig)
    bars, entry_idx = sig['bars'], sig['entry_idx']
    for b2 in bars[entry_idx + 1:]:
        t2 = h._et(b2)
        if (t2.hour, t2.minute) >= cutoff_hm:
            return 'cutoff', b2['c'], stop_spy, target_spy
        if direction == 'c':
            if b2['l'] <= stop_spy:
                return 'stop', stop_spy, stop_spy, target_spy
            if b2['h'] >= target_spy:
                return 'target', target_spy, stop_spy, target_spy
        else:
            if b2['h'] >= stop_spy:
                return 'stop', stop_spy, stop_spy, target_spy
            if b2['l'] <= target_spy:
                return 'target', target_spy, stop_spy, target_spy
    last = bars[-1]
    return 'eod', last['c'], stop_spy, target_spy


print(f"{'='*100}\nLIVE FRESH-DATA CHECK -- {TARGET_DATES}\n{'='*100}")

for ds in TARGET_DATES:
    bars_1min = days_1min.get(ds, [])
    bars_5min = to_5min(bars_1min)
    print(f"\n{'#'*100}\n### {ds}  ({len(bars_1min)} 1-min bars, {len(bars_5min)} 5-min bars)\n{'#'*100}")
    if not bars_1min:
        print("  NO DATA for this date (not fetched / not a trading day / too recent).")
        continue
    day_open  = bars_1min[0]['o']
    day_close = bars_1min[-1]['c']
    day_high  = max(b['h'] for b in bars_1min)
    day_low   = min(b['l'] for b in bars_1min)
    print(f"  SPY: open={day_open:.2f} high={day_high:.2f} low={day_low:.2f} close={day_close:.2f}")

    # ---------------- OR/POC weekly (stop=$0.05, RR fixed at h.OR_POC_RR) ----------------
    sig = h.find_or_poc_signal(ds, bars_1min, bars_5min)
    print(f"\n  -- OR/POC weekly (stop=$0.05, target R:R={h.OR_POC_RR}) --")
    if sig is None:
        print("     NO SIGNAL (no 5-min close outside the 9:30-9:45 OR high/low by 12:00 ET, "
              "or OR range < $0.20).")
    else:
        stop_dist = 0.05
        def or_poc_calc(sig, poc=sig['poc'], direction=sig['direction']):
            if direction == 'c':
                stop = poc - stop_dist
                target = sig['entry_spy'] + h.OR_POC_RR * (sig['entry_spy'] - stop)
            else:
                stop = poc + stop_dist
                target = sig['entry_spy'] - h.OR_POC_RR * (stop - sig['entry_spy'])
            return stop, target
        reason, exit_spy, stop_spy, target_spy = simulate_spy_only(sig, or_poc_calc, None, (13, 30))
        print(f"     SIGNAL: {('CALL' if sig['direction']=='c' else 'PUT')}  "
              f"OR high/low=${sig['or_high']:.2f}/${sig['or_low']:.2f}  POC=${sig['poc']:.2f}  "
              f"entry@{sig['entry_dt'].strftime('%H:%M')} SPY=${sig['entry_spy']:.2f}  strike~{sig['strike']}")
        print(f"     Stop=${stop_spy:.2f}  Target=${target_spy:.2f}  "
              f"-> SPY-side exit: {reason.upper()} @ ${exit_spy:.2f} "
              f"({'WIN' if reason=='target' else ('LOSS' if reason=='stop' else 'depends on option decay (cutoff/EOD)')})")

    # ---------------- AMD fade (am15, $0.05 buffer, simple confirm, $0.15 stop, 3.5R) -----
    print(f"\n  -- AMD fade-breakout (am15 range, $0.05 sweep buffer, simple confirm, "
          f"$0.15 stop buffer, 3.5R target) --")
    sig = find_amd_signal(bars_1min, bars_5min, None, 'am15', 0.05, 'simple')
    if sig is None:
        print("     NO SIGNAL (no clean am15-range sweep + same-direction-inside confirm "
              "5-min pair between 9:45 and 15:00 ET).")
    else:
        stop_buffer, target_rr = 0.15, 3.5
        def amd_calc(sig, extreme=sig['sweep_extreme'], direction=sig['direction']):
            if direction == 'c':
                stop = extreme - stop_buffer
                target = sig['entry_spy'] + target_rr * (sig['entry_spy'] - stop)
            else:
                stop = extreme + stop_buffer
                target = sig['entry_spy'] - target_rr * (stop - sig['entry_spy'])
            return stop, target
        reason, exit_spy, stop_spy, target_spy = simulate_spy_only(sig, amd_calc, None, (15, 45))
        print(f"     SIGNAL: {('CALL' if sig['direction']=='c' else 'PUT')}  "
              f"range=${sig['range_low']:.2f}-${sig['range_high']:.2f}  sweep_extreme=${sig['sweep_extreme']:.2f}  "
              f"entry@{sig['entry_dt'].strftime('%H:%M')} SPY=${sig['entry_spy']:.2f}  strike~{sig['strike']}")
        print(f"     Stop=${stop_spy:.2f}  Target=${target_spy:.2f}  "
              f"-> SPY-side exit: {reason.upper()} @ ${exit_spy:.2f} "
              f"({'WIN' if reason=='target' else ('LOSS' if reason=='stop' else 'depends on option decay (cutoff/EOD)')})")

    # ---------------- VTC (tolerance=$0.25, stop buffer=$0.15, target R:R=3.5) -----------
    print(f"\n  -- VWAP Trend Continuation (tolerance=$0.25, stop buffer=$0.15, target R:R=3.5) --")
    sig = find_vtc_signal(bars_1min, bars_5min, 0.25)
    if sig is None:
        print("     NO SIGNAL (no 10:00 ET bias >= $0.02 vs cumulative VWAP, or no "
              "pullback-to-VWAP-then-reclaim 5-min pair by 15:00 ET).")
    else:
        stop_buffer, target_rr = 0.15, 3.5
        def vtc_calc(sig, extreme=sig['pullback_extreme'], direction=sig['direction']):
            if direction == 'c':
                stop = extreme - stop_buffer
                target = sig['entry_spy'] + target_rr * (sig['entry_spy'] - stop)
            else:
                stop = extreme + stop_buffer
                target = sig['entry_spy'] - target_rr * (stop - sig['entry_spy'])
            return stop, target
        reason, exit_spy, stop_spy, target_spy = simulate_spy_only(sig, vtc_calc, None, (15, 45))
        print(f"     SIGNAL: {('CALL' if sig['direction']=='c' else 'PUT')}  "
              f"vwap_bias=${sig['vwap_bias']:.2f}  pullback_extreme=${sig['pullback_extreme']:.2f}  "
              f"entry@{sig['entry_dt'].strftime('%H:%M')} SPY=${sig['entry_spy']:.2f}  strike~{sig['strike']}")
        print(f"     Stop=${stop_spy:.2f}  Target=${target_spy:.2f}  "
              f"-> SPY-side exit: {reason.upper()} @ ${exit_spy:.2f} "
              f"({'WIN' if reason=='target' else ('LOSS' if reason=='stop' else 'depends on option decay (cutoff/EOD)')})")

print(f"\n{'='*100}\nDone.\n{'='*100}")
