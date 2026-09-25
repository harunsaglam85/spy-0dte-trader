#!/usr/bin/env python3
"""Two follow-up analyses on the weekly OR/POC (stop=$0.05) result set:

A) Give-back analysis on losing trades - did price move favorably (toward
   target) before reversing into the stop, and if so how far? Then a
   trailing-to-breakeven variant: once a trade reaches 50% of the distance
   to target, move the stop to entry (breakeven), re-run, report new stats.

B) Market-regime comparison (VIX, SPY realized vol, trend character) between
   the IS window (2023-07-01 - 2025-08-25) and the chronological OOS window
   (2025-08-26 - 2026-05-29) used in the earlier IS/OOS split, to check
   whether the OOS underperformance lines up with a genuine regime shift.

Uses only cached data - no new ThetaData calls.
"""
import sys, math
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
from datetime import date
from collections import defaultdict

STOP_DISTANCES = [0.05, 0.10]
GIVEBACK_FRAC  = 0.50
IS_LO, IS_HI   = '2023-07-01', '2025-08-25'
OOS_LO, OOS_HI = '2025-08-26', '2026-05-29'

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
vix       = h.load_vix()
all_dates = sorted(spy_daily.keys())
dates     = [d for d in all_dates if '2023-07-01' <= d <= '2026-05-30']

print("Rebuilding weekly signals/trades (cache-hit, no new API calls)...")
results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, STOP_DISTANCES)
sig_by_key = {(s['ds'], s['direction']): s for s in signals_w}


# ═══════════════════════════════════════════════════════════════════════
# A) Give-back analysis
# ═══════════════════════════════════════════════════════════════════════
def path_mfe_and_reexit(sig, stop_distance):
    """Independently walks the same 1-min path _simulate_trade_weekly does,
    tracking the best favorable excursion (in underlying points, clamped
    >=0) toward target before the trade's actual exit event, expressed as
    a % of the full entry->target distance. Also returns the exit_reason
    it derives, as a consistency check against the recorded trade."""
    direction, poc, bars, entry_idx = sig['direction'], sig['poc'], sig['bars'], sig['entry_idx']
    entry_spy = sig['entry_spy']
    if direction == 'c':
        stop_spy = poc - stop_distance
        if stop_spy >= entry_spy:
            return None
        risk = entry_spy - stop_spy
        target_spy = entry_spy + h.OR_POC_RR * risk
    else:
        stop_spy = poc + stop_distance
        if stop_spy <= entry_spy:
            return None
        risk = stop_spy - entry_spy
        target_spy = entry_spy - h.OR_POC_RR * risk
    if risk < 0.02:
        return None
    dist_to_target = abs(target_spy - entry_spy)

    best_favorable = 0.0
    exit_reason = 'EOD'
    cutoff = (13, 30)
    for b2 in bars[entry_idx + 1:]:
        t2 = h._et(b2)
        if (t2.hour, t2.minute) >= cutoff:
            exit_reason = '1:30 cutoff'
            break
        if direction == 'c':
            fav = b2['h'] - entry_spy
            if fav > best_favorable:
                best_favorable = fav
            if b2['l'] <= stop_spy:
                exit_reason = 'stop'
                break
            if b2['h'] >= target_spy:
                exit_reason = '3:1 target'
                break
        else:
            fav = entry_spy - b2['l']
            if fav > best_favorable:
                best_favorable = fav
            if b2['h'] >= stop_spy:
                exit_reason = 'stop'
                break
            if b2['l'] <= target_spy:
                exit_reason = '3:1 target'
                break
    mfe_pct = max(0.0, best_favorable / dist_to_target * 100.0)
    return {'exit_reason': exit_reason, 'mfe_pct': mfe_pct}


for sd in STOP_DISTANCES:
    print(f"\n{'='*90}\n  A) GIVE-BACK ANALYSIS -- stop=${sd:.2f}\n{'='*90}")
    trades = results_w[sd]
    losers = [t for t in trades if t.pnl <= 0]
    print(f"  {len(trades)} total trades, {len(losers)} losers ({len(losers)/len(trades)*100:.1f}%)")

    buckets = defaultdict(int)
    mismatches = 0
    giveback_ct = straight_ct = 0
    rows = []
    for t in losers:
        sig = sig_by_key.get((t.date, t.direction[0].lower()))
        if sig is None:
            continue
        r = path_mfe_and_reexit(sig, sd)
        if r is None:
            continue
        # consistency check vs the recorded trade's own exit_reason
        recorded_is_stop = t.exit_reason.startswith('stop')
        derived_is_stop  = r['exit_reason'] == 'stop'
        if recorded_is_stop != derived_is_stop and t.exit_reason != '1:30 cutoff':
            mismatches += 1
        mfe = r['mfe_pct']
        rows.append((t.date, t.direction, mfe, t.exit_reason, t.pnl))
        if mfe >= 75:
            buckets['>=75%'] += 1
        elif mfe >= 50:
            buckets['50-75%'] += 1
        elif mfe >= 25:
            buckets['25-50%'] += 1
        elif mfe >= 5:
            buckets['5-25%'] += 1
        else:
            buckets['<5% (straight to stop)'] += 1
        if mfe >= 50:
            giveback_ct += 1
        if mfe < 5:
            straight_ct += 1

    n_checked = len(rows)
    print(f"  {n_checked} losers with a valid signal/path (consistency mismatches: {mismatches})")
    print(f"\n  MFE-toward-target distribution among losers:")
    for k in ['>=75%', '50-75%', '25-50%', '5-25%', '<5% (straight to stop)']:
        c = buckets.get(k, 0)
        print(f"    {k:<26} {c:>4}  ({c/n_checked*100:>5.1f}%)")
    print(f"\n  Give-back losses (reached >=50% of target distance): {giveback_ct}  "
          f"({giveback_ct/n_checked*100:.1f}% of losers)")
    print(f"  Straight-to-stop losses (never reached 5% of target distance): {straight_ct}  "
          f"({straight_ct/n_checked*100:.1f}% of losers)")


# ═══════════════════════════════════════════════════════════════════════
# A3) Trailing-to-breakeven variant
# ═══════════════════════════════════════════════════════════════════════
def simulate_trailing_be(sig, stop_distance, giveback_frac=GIVEBACK_FRAC):
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
        trigger_level = entry_spy + giveback_frac * (target_spy - entry_spy)
    else:
        stop_spy = poc + stop_distance
        if stop_spy <= entry_spy:
            return None
        risk = stop_spy - entry_spy
        target_spy = entry_spy - h.OR_POC_RR * risk
        trigger_level = entry_spy - giveback_frac * (entry_spy - target_spy)
    if risk < 0.02:
        return None

    entry_hm = entry_dt.strftime('%H:%M')
    entry_bid = h._nearest_prior_quote(chain, entry_hm, 'bid')
    entry_ask = h._nearest_prior_quote(chain, entry_hm, 'ask')
    if entry_ask is None or entry_ask <= 0:
        return None

    cur_stop, moved_to_be = stop_spy, False
    exit_bar, exit_reason, exit_spy = None, 'EOD', None
    cutoff = (13, 30)
    for b2 in bars[entry_idx + 1:]:
        t2 = h._et(b2)
        if (t2.hour, t2.minute) >= cutoff:
            exit_bar, exit_reason, exit_spy = b2, '1:30 cutoff', b2['c']
            break
        if direction == 'c':
            if b2['l'] <= cur_stop:
                exit_bar = b2
                exit_reason = 'stop BE' if moved_to_be else f'stop (POC-{stop_distance:.2f})'
                exit_spy = cur_stop
                break
            if b2['h'] >= target_spy:
                exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                break
            if not moved_to_be and b2['h'] >= trigger_level:
                cur_stop, moved_to_be = entry_spy, True
        else:
            if b2['h'] >= cur_stop:
                exit_bar = b2
                exit_reason = 'stop BE' if moved_to_be else f'stop (POC+{stop_distance:.2f})'
                exit_spy = cur_stop
                break
            if b2['l'] <= target_spy:
                exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                break
            if not moved_to_be and b2['l'] <= trigger_level:
                cur_stop, moved_to_be = entry_spy, True
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


print(f"\n{'='*90}\n  A3) TRAILING-TO-BREAKEVEN VARIANT (trigger at {GIVEBACK_FRAC*100:.0f}% of target distance)\n{'='*90}")
for sd in STOP_DISTANCES:
    orig = h.calc_stats(results_w[sd])
    trailing_trades = [t for s in signals_w if (t := simulate_trailing_be(s, sd)) is not None]
    trail = h.calc_stats(trailing_trades)
    be_exits = sum(1 for t in trailing_trades if t.exit_reason == 'stop BE')
    print(f"\n-- stop=${sd:.2f} --")
    print(f"  ORIGINAL  N={orig['n']:>4}  WR={orig['wr']:>6.1f}%  PF={h.fmt_pf(orig):>6}  "
          f"Total P&L=${orig['total_pnl']:>+10,.2f}  AvgWin=${orig['avg_win']:>7.2f}  AvgLoss=${orig['avg_loss']:>8.2f}")
    print(f"  TRAILING  N={trail['n']:>4}  WR={trail['wr']:>6.1f}%  PF={h.fmt_pf(trail):>6}  "
          f"Total P&L=${trail['total_pnl']:>+10,.2f}  AvgWin=${trail['avg_win']:>7.2f}  AvgLoss=${trail['avg_loss']:>8.2f}"
          f"   (BE-stop exits: {be_exits})")


# ═══════════════════════════════════════════════════════════════════════
# B) Market-regime comparison: IS vs chronological OOS window
# ═══════════════════════════════════════════════════════════════════════
def regime_stats(lo, hi):
    ds_range = [d for d in all_dates if lo <= d <= hi]
    vix_vals = [vix[d] for d in ds_range if d in vix]
    rets = []
    open_close_moves = []
    prev_close = None
    for d in ds_range:
        bar = spy_daily.get(d)
        if not bar:
            continue
        o, c = bar['open'], bar['close']
        if prev_close and prev_close > 0:
            rets.append(math.log(c / prev_close))
        prev_close = c
        if o > 0:
            open_close_moves.append(abs(c - o) / o * 100.0)
    n = len(ds_range)
    avg_vix = sum(vix_vals) / len(vix_vals) if vix_vals else float('nan')
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        realized_vol_ann = math.sqrt(var) * math.sqrt(252) * 100.0
    else:
        realized_vol_ann = float('nan')
    n_moves = len(open_close_moves)
    pct_choppy   = sum(1 for m in open_close_moves if m <= 0.3) / n_moves * 100.0 if n_moves else float('nan')
    pct_trending = sum(1 for m in open_close_moves if m > 1.0) / n_moves * 100.0 if n_moves else float('nan')
    avg_move = sum(open_close_moves) / n_moves if n_moves else float('nan')
    return {'n_days': n, 'avg_vix': avg_vix, 'realized_vol_ann': realized_vol_ann,
            'pct_choppy': pct_choppy, 'pct_trending': pct_trending, 'avg_oc_move_pct': avg_move}

print(f"\n{'='*90}\n  B) MARKET REGIME: IS window vs chronological OOS window\n{'='*90}")
is_stats  = regime_stats(IS_LO, IS_HI)
oos_stats = regime_stats(OOS_LO, OOS_HI)
print(f"  IS  ({IS_LO} -> {IS_HI}):  {is_stats}")
print(f"  OOS ({OOS_LO} -> {OOS_HI}):  {oos_stats}")
print(f"\n  {'Metric':<28} {'IS':>14} {'OOS':>14}")
print(f"  {'Trading days':<28} {is_stats['n_days']:>14} {oos_stats['n_days']:>14}")
print(f"  {'Avg VIX':<28} {is_stats['avg_vix']:>14.2f} {oos_stats['avg_vix']:>14.2f}")
print(f"  {'Realized vol (ann, %)':<28} {is_stats['realized_vol_ann']:>14.2f} {oos_stats['realized_vol_ann']:>14.2f}")
print(f"  {'Avg open->close move (%)':<28} {is_stats['avg_oc_move_pct']:>14.2f} {oos_stats['avg_oc_move_pct']:>14.2f}")
print(f"  {'% days closing <=0.3% of open':<28} {is_stats['pct_choppy']:>14.1f} {oos_stats['pct_choppy']:>14.1f}")
print(f"  {'% days with >1% move':<28} {is_stats['pct_trending']:>14.1f} {oos_stats['pct_trending']:>14.1f}")
