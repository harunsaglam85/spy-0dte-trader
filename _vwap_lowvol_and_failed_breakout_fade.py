#!/usr/bin/env python3
"""Two follow-ups:

A) Re-run the existing VWAP reversion strategy (run_vwap_reversion_real,
   unmodified) restricted to the bottom tercile of days by rolling realized
   vol, same $0.05-$0.50 stop-buffer sweep, compare vs. the unfiltered
   baseline over the same date range.

B) On those same low-realized-vol days, for every OR/POC weekly trade
   (stop=$0.05) that FAILED (got stopped out), check what fraction of the
   time a fade taken right at that stop-out point - in the opposite
   direction, targeting that moment's VWAP (the same target rule the real
   VWAP reversion strategy uses) - would have reached that target before
   the 1:30 ET cutoff. This is a price-level feasibility check (does the
   underlying reach the target level), not a full option-fill simulation -
   noted explicitly in the report.

Realized-vol measure: trailing 20-trading-day annualized stdev of SPY daily
log returns, computed causally (each day's value only uses that day and
the 19 before it - no look-ahead), same log-return methodology as the
earlier IS/OOS regime comparison. Terciles are computed over the 2023-07 -
2026-05 analysis window.

Uses only cached data - no new ThetaData calls expected (VWAP reversion's
underlying chain cache should already cover this window from prior runs;
any gaps get fetched fresh, cache-first as always).
"""
import sys, math
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
from datetime import date, timedelta

DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'
STOP_GRID        = [0.05, 0.10, 0.15, 0.25, 0.50]
RV_WINDOW        = 20
OR_POC_STOP      = 0.05   # best-performing weekly combo from earlier work

print("Loading market data...")
spy_daily = h.load_spy_daily()
spy_1min  = h.load_spy_1min()
spy_5min  = h.load_spy_5min()
all_dates_full = sorted(spy_daily.keys())
dates = [d for d in all_dates_full if DATE_LO <= d <= DATE_HI]


# ─────────────────────────────────────────────────────────────────────────
# Rolling (causal, no look-ahead) realized vol per day + tercile split
# ─────────────────────────────────────────────────────────────────────────
def build_rolling_rv(spy_daily, all_dates_full, window=RV_WINDOW):
    rv = {}
    rets = []
    prev_close = None
    for d in all_dates_full:
        bar = spy_daily.get(d)
        if not bar:
            continue
        c = bar['close']
        if prev_close and prev_close > 0:
            rets.append(math.log(c / prev_close))
        prev_close = c
        if len(rets) >= window:
            w = rets[-window:]
            mu = sum(w) / window
            var = sum((x - mu) ** 2 for x in w) / (window - 1)
            rv[d] = math.sqrt(var) * math.sqrt(252) * 100.0
    return rv

rv_by_day = build_rolling_rv(spy_daily, all_dates_full, RV_WINDOW)
rv_in_range = sorted([(d, rv_by_day[d]) for d in dates if d in rv_by_day], key=lambda x: x[1])
n = len(rv_in_range)
tercile_size = n // 3
low_tercile = rv_in_range[:tercile_size]
low_vol_dates = sorted(d for d, v in low_tercile)
cutoff_val = low_tercile[-1][1]
print(f"\n{n} days with a valid {RV_WINDOW}-day rolling RV in [{DATE_LO}, {DATE_HI}]")
print(f"Low tercile: {tercile_size} days, rolling RV <= {cutoff_val:.2f}% (range "
      f"{low_tercile[0][1]:.2f}% - {low_tercile[-1][1]:.2f}%)")
print(f"Low-vol days span {low_vol_dates[0]} -> {low_vol_dates[-1]}")


# ═══════════════════════════════════════════════════════════════════════
# A) VWAP reversion: baseline vs. low-vol-filtered
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*90}\n  A) VWAP REVERSION -- baseline (all days) vs low-RV-tercile days\n{'='*90}")
print(f"  Baseline: {len(dates)} candidate days")
results_base, cov_base = h.run_vwap_reversion_real(spy_1min, spy_5min, dates, STOP_GRID)
print(f"  Low-vol:  {len(low_vol_dates)} candidate days")
results_lv, cov_lv = h.run_vwap_reversion_real(spy_1min, spy_5min, low_vol_dates, STOP_GRID)

print(f"\n  {'stop':>6}  {'BASE N':>7} {'WR':>7} {'PF':>7} {'P&L':>12}   ||   "
      f"{'LOWVOL N':>9} {'WR':>7} {'PF':>7} {'P&L':>12}")
for buf in STOP_GRID:
    sb = h.calc_stats(results_base[buf])
    sl = h.calc_stats(results_lv[buf])
    print(f"  ${buf:>4.2f}  {sb['n']:>7} {sb['wr']:>6.1f}% {h.fmt_pf(sb):>7} {sb['total_pnl']:>+11,.2f}   ||   "
          f"{sl['n']:>9} {sl['wr']:>6.1f}% {h.fmt_pf(sl):>7} {sl['total_pnl']:>+11,.2f}")


# ═══════════════════════════════════════════════════════════════════════
# B) On low-vol days: of OR/POC's FAILED (stopped-out) trades, what
#    fraction would a same-point fade toward VWAP have reached before
#    the 1:30 cutoff?
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*90}\n  B) OR/POC FAILED BREAKOUTS on low-RV days -- would a fade-to-VWAP have worked?\n{'='*90}")
print("  Rebuilding OR/POC weekly signals/trades (cache-hit, no new API calls)...")
results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, [OR_POC_STOP])
sig_by_key = {(s['ds'], s['direction']): s for s in signals_w}

trades = results_w[OR_POC_STOP]
lowvol_set = set(low_vol_dates)
failed = [t for t in trades if t.date in lowvol_set and t.exit_reason.startswith('stop')]
print(f"  {len(trades)} total OR/POC trades (stop=${OR_POC_STOP}); "
      f"{len(failed)} of them are stopped-out failures on a low-RV day")

reached, valid_direction, n_checked = 0, 0, 0
dist_samples = []
for t in failed:
    sig = sig_by_key.get((t.date, t.direction[0].lower()))
    if sig is None:
        continue
    bars = sig['bars']
    # locate the 1-min bar matching the OR/POC trade's own exit time
    exit_idx = next((i for i, b in enumerate(bars) if h._et(b).strftime('%H:%M') == t.exit_time), None)
    if exit_idx is None:
        continue
    vwap_map = h.calc_vwap_bands(bars)
    band = h._nearest_prior_vwap(vwap_map, t.exit_time)
    if band is None:
        continue
    vwap_at_fail = band[0]
    n_checked += 1

    fade_down = (t.direction == 'CALL')  # OR/POC bought calls and failed -> fade is short/down
    fail_price = t.exit_spy
    if fade_down:
        target_valid = vwap_at_fail < fail_price
    else:
        target_valid = vwap_at_fail > fail_price
    if not target_valid:
        continue
    valid_direction += 1
    dist = abs(vwap_at_fail - fail_price)
    dist_samples.append(dist)

    cutoff = (13, 30)
    hit = False
    for b2 in bars[exit_idx + 1:]:
        t2 = h._et(b2)
        if (t2.hour, t2.minute) >= cutoff:
            break
        if fade_down:
            if b2['l'] <= vwap_at_fail:
                hit = True
                break
        else:
            if b2['h'] >= vwap_at_fail:
                hit = True
                break
    if hit:
        reached += 1

print(f"\n  {n_checked} failed trades had a computable VWAP-at-failure reference")
print(f"  {valid_direction} of those had VWAP on the correct side to fade toward "
      f"(the rest: price already past/at VWAP when OR/POC stopped out, no fade room)")
print(f"  {reached} of {valid_direction} directionally-valid fades reached VWAP before the 1:30 cutoff "
      f"-> {reached/valid_direction*100 if valid_direction else 0:.1f}%")
if dist_samples:
    print(f"  Avg distance from failure point to VWAP target: "
          f"${sum(dist_samples)/len(dist_samples):.2f}  (underlying points)")
print(f"\n  NOTE: this is a price-level feasibility check (did SPY reach the VWAP level),")
print(f"  not a full option-priced fade simulation with real bid/ask fills.")
