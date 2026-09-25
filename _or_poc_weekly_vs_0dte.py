#!/usr/bin/env python3
"""Weekly-expiration variant of R7-POC-RR3 vs. the existing 0DTE real-quote
run, to test whether 0DTE's bid/ask spread (as a % of option price) is the
core problem versus the same underlying signal traded on a nearer-to-ATM,
higher-premium weekly contract.

Same signal logic as run_r7_or_poc_rr3_real() (15-min OR, POC from a volume
profile built off the OR's 1-min bars, 5-min candle close outside OR
high/low triggers, entry at the NEXT 5-min candle's close, no volume
filter) and the same theta_0dte_usable gate that keeps the signal-day set
identical to the existing 0DTE run. The ONLY thing that changes for the
weekly leg is which contract gets bought: instead of the same-day (0DTE)
contract, the nearest expiration that is 3-5 calendar days out (never
same-day). Stop = POC +/- stop_distance (swept over $0.05 and $0.10, the
two best 0DTE performers per prior sweeps). Target = fixed 3:1 R:R in
underlying points. Real intraday bid/ask fills via ThetaData for both legs.

Usage: python _or_poc_weekly_vs_0dte.py
Safe to re-run/resume — every ThetaData call this script makes (through
hermes_research_round2.py) is cache-first and cache-forever on disk, so an
interrupted run just picks up where it left off.
"""
import sys, time
sys.path.insert(0, r'C:\Users\sagla\spy-0dte-trader')
import hermes_research_round2 as h
from datetime import date
from pathlib import Path

DATE_LO = '2023-07-01'
DATE_HI = '2026-05-30'
STOP_DISTANCES = [0.05, 0.10]
OUT_DIR = Path(r'C:\Users\sagla\spy-0dte-trader')


# ── spread helpers ──────────────────────────────────────────────────────────
def spread_pct(bid, ask):
    """Bid/ask spread as a % of the option's mid price. None if either side
    is missing/non-positive (illiquid minute, no quote)."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 100.0 if mid > 0 else None


def avg_spread_pct(pairs):
    vals = [v for v in (spread_pct(b, a) for b, a in pairs) if v is not None]
    return (sum(vals) / len(vals)) if vals else None


# ── 0DTE side: rebuild signals + chains (cache-first, matches the existing
#    theta_0dte_usable-gated pipeline exactly, same pattern as
#    _or_poc_isoos_sweep.py's signal_dates reconstruction) ──────────────────
def build_0dte_signals_with_chains(spy_1min, spy_5min, dates):
    signals = []
    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < h.OR_POC_MIN + 5 or len(bars_5min) < 4:
            continue
        if not h.theta_0dte_usable(ds):
            continue
        sig = h.find_or_poc_signal(ds, bars_1min, bars_5min)
        if sig is None:
            continue
        right = 'C' if sig['direction'] == 'c' else 'P'
        real_strike = h.snap_to_real_strike(ds, sig['strike'], right)
        if real_strike is None:
            continue
        sig['strike'] = real_strike
        chain_list = h.fetch_intraday_chain(
            ds, real_strike, right, sig['entry_dt'].strftime('%H:%M:%S'), '13:30:00')
        sig['chain'] = {r['hm']: r for r in chain_list} if chain_list else None
        if sig['chain']:
            signals.append(sig)
    return signals


def zero_dte_spread_pairs(signals, stop_distance):
    """(entry_bid,entry_ask) and (exit_bid,exit_ask) pairs for every 0DTE
    signal that actually produces a trade at this stop distance. Reuses the
    private _simulate_trade_real for the exit-time walk (same exit logic
    the real stats are computed from) rather than re-deriving it."""
    entry_pairs, exit_pairs = [], []
    for sig in signals:
        t = h._simulate_trade_real(sig, stop_distance)
        if t is None:
            continue
        chain = sig['chain']
        eb = h._nearest_prior_quote(chain, t.entry_time, 'bid')
        ea = h._nearest_prior_quote(chain, t.entry_time, 'ask')
        xb = h._nearest_prior_quote(chain, t.exit_time, 'bid')
        xa = h._nearest_prior_quote(chain, t.exit_time, 'ask')
        entry_pairs.append((eb, ea))
        exit_pairs.append((xb, xa))
    return entry_pairs, exit_pairs


def print_stats_row(label, s):
    print(f"  {label:<10} N={s['n']:>4}  WR={s['wr']:>6.1f}%  PF={h.fmt_pf(s):>6}  "
          f"Total P&L=${s['total_pnl']:>+11,.2f}  AvgWin=${s['avg_win']:>7.2f}  "
          f"AvgLoss=${s['avg_loss']:>8.2f}")


def main():
    print("Loading market data...")
    spy_daily = h.load_spy_daily()
    spy_1min  = h.load_spy_1min()
    spy_5min  = h.load_spy_5min()
    all_dates = sorted(spy_daily.keys())
    dates = [d for d in all_dates if DATE_LO <= d <= DATE_HI]
    print(f"  {len(dates)} candidate trading days in [{DATE_LO}, {DATE_HI}]")

    # ── Weekly run ───────────────────────────────────────────────────────
    print(f"\n{'='*90}\n  WEEKLY (3-5 DTE) R7-POC-RR3 — real intraday ThetaData quotes\n{'='*90}")
    t0 = time.time()
    results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(
        spy_1min, spy_5min, dates, STOP_DISTANCES)
    print(f"  Elapsed: {time.time()-t0:.1f}s")
    print("  -- Coverage --")
    for k, v in coverage_w.items():
        print(f"  {k:<28} {v}")

    h.write_weekly_sweep_csv(results_w, OUT_DIR / 'trades_weekly_rr3.0_sweep.csv')

    # ── 0DTE reference run (cache-first; should already be fully cached
    #    from the prior real-quote sweep, so this makes ~0 new API calls) ─
    print(f"\n{'='*90}\n  0DTE reference — real intraday ThetaData quotes (cache-first)\n{'='*90}")
    t0 = time.time()
    results_0, coverage_0 = h.run_r7_or_poc_rr3_real(spy_1min, spy_5min, dates, STOP_DISTANCES)
    print(f"  Elapsed: {time.time()-t0:.1f}s")
    signals_0 = build_0dte_signals_with_chains(spy_1min, spy_5min, dates)
    print(f"  {len(signals_0)} 0DTE signals with a usable cached chain (for spread calc)")

    # ── Stats comparison ────────────────────────────────────────────────
    print(f"\n{'='*90}\n  STATS: 0DTE vs WEEKLY, by stop distance\n{'='*90}")
    for sd in STOP_DISTANCES:
        print(f"\n-- stop=${sd:.2f} --")
        print_stats_row('0DTE', h.calc_stats(results_0[sd]))
        print_stats_row('Weekly', h.calc_stats(results_w[sd]))

    # ── Spread-as-%-of-price comparison ─────────────────────────────────
    print(f"\n{'='*90}\n  SPREAD AS % OF OPTION PRICE\n{'='*90}")
    for sd in STOP_DISTANCES:
        e0, x0 = zero_dte_spread_pairs(signals_0, sd)
        ew = [(t.entry_bid, t.entry_ask) for t in results_w[sd]]
        xw = [(t.exit_bid, t.exit_ask) for t in results_w[sd]]
        print(f"\n-- stop=${sd:.2f} --")
        print(f"  0DTE    entry spread: {avg_spread_pct(e0):>6.2f}%  "
              f"(n={sum(1 for p in e0 if spread_pct(*p) is not None)})   "
              f"exit spread: {avg_spread_pct(x0):>6.2f}%  "
              f"(n={sum(1 for p in x0 if spread_pct(*p) is not None)})")
        print(f"  Weekly  entry spread: {avg_spread_pct(ew):>6.2f}%  "
              f"(n={sum(1 for p in ew if spread_pct(*p) is not None)})   "
              f"exit spread: {avg_spread_pct(xw):>6.2f}%  "
              f"(n={sum(1 for p in xw if spread_pct(*p) is not None)})")

    # ── IS/OOS 70/30 split on the best-performing weekly combo ─────────
    def pf_key(sd):
        s = h.calc_stats(results_w[sd])
        return (s['pf'] if s['pf'] != float('inf') else 1e9, s['total_pnl'])
    best_sd = max(STOP_DISTANCES, key=pf_key)
    print(f"\n{'='*90}\n  IS/OOS 70/30 split — best weekly combo: stop=${best_sd:.2f}\n{'='*90}")

    weekly_signal_dates = sorted({sig['ds'] for sig in signals_w})
    n = len(weekly_signal_dates)
    split_idx = round(n * 0.70)
    is_dates  = set(weekly_signal_dates[:split_idx])
    oos_dates = set(weekly_signal_dates[split_idx:])
    print(f"  {n} weekly signal days total, 70% split = {split_idx}")
    if weekly_signal_dates:
        print(f"  IS:  {len(is_dates)} days ({weekly_signal_dates[0]} -> "
              f"{weekly_signal_dates[max(split_idx-1,0)]})")
        print(f"  OOS: {len(oos_dates)} days ({weekly_signal_dates[min(split_idx,n-1)]} -> "
              f"{weekly_signal_dates[-1]})")

    trades = results_w[best_sd]
    is_trades  = [t for t in trades if t.date in is_dates]
    oos_trades = [t for t in trades if t.date in oos_dates]
    s_all = h.calc_stats(trades)
    s_is  = h.calc_stats(is_trades)
    s_oos = h.calc_stats(oos_trades)
    print(f"\n  ALL  N={s_all['n']:>4}  WR={s_all['wr']:>6.1f}%  PF={h.fmt_pf(s_all):>6}  "
          f"Total P&L=${s_all['total_pnl']:>+12,.2f}")
    print(f"  IS   N={s_is['n']:>4}  WR={s_is['wr']:>6.1f}%  PF={h.fmt_pf(s_is):>6}  "
          f"Total P&L=${s_is['total_pnl']:>+12,.2f}")
    print(f"  OOS  N={s_oos['n']:>4}  WR={s_oos['wr']:>6.1f}%  PF={h.fmt_pf(s_oos):>6}  "
          f"Total P&L=${s_oos['total_pnl']:>+12,.2f}")

    print(f"\n  Wrote -> {OUT_DIR / 'trades_weekly_rr3.0_sweep.csv'}")


if __name__ == '__main__':
    main()
