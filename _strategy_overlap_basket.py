#!/usr/bin/env python3
"""
_strategy_overlap_basket.py
============================
Day-level overlap + basket-combination analysis across the three validated
(or attempted) real-fill 0DTE/weekly strategies in this project:

  A. OR/POC weekly, low+medium 0DTE-options-VOLUME filtered (top-tercile
     0DTE contract volume days excluded), stop=$0.05 (see
     _or_poc_volume_filter_validation.py / .log). Clears all four splits.
     NOTE: an earlier version of this script used a DIFFERENT filter here -
     top-tercile SPY REALIZED-VOLATILITY days (_or_poc_rv_filter_and_
     validation.py) - which only overlaps this volume filter's signal days
     57% of the time and FAILS chronological OOS (PF 0.83, -$452). That was
     a mislabeling bug (both are legitimately called "OR/POC weekly
     filtered" but they are not the same experiment - one filters by SPY
     price volatility, this one by 0DTE contract volume). Verified by
     re-reading both source scripts: same base 572-trade unfiltered signal
     set, same date range, same stop - only the filter criterion differs.
     Use THIS (volume) variant going forward; it's the one that actually
     validates.
  B. AMD fade-breakout best combo (am15, buffer=$0.05, simple confirm,
     stop=$0.15, RR=3.5) - clears all four splits.
  C. VWAP Trend Continuation best combo (tolerance=$0.25, stop=$0.15,
     RR=3.5) - clears all four splits.

All three fire at most one signal/day, on real ThetaData weekly-expiration
fills, same underlying (SPY). This script:
 1. Builds a day-level indicator (which of A/B/C fired) for every trading
    day in [2023-07-01, 2026-05-30].
 2. Reports the overlap matrix (all 8 combinations of A/B/C firing).
 3. Splits each strategy's trades into "fired alone that day" vs "fired
    alongside >=1 other" and compares WR/PF.
 4. Combines all three into an equal-weight (1 contract/signal) basket and
    reports combined N/WR/PF/P&L, plus daily-pnl volatility for each single
    strategy vs the combined basket.
 5. Runs an event-driven (entry/exit timestamped, not just same-day-summed)
    capital simulation at a $2,500 starting account for each strategy alone
    and for the shared-capital basket - a trade is skipped if the account
    doesn't have entry_cost = entry_option*100 in cash at that exact minute,
    which correctly handles same-day overlapping holds using each trade's
    real entry_time/exit_time. Reports final equity, total return, max
    drawdown, and trades skipped for lack of capital.

Writes strategy_overlap_basket.json (all series + tables) for the follow-up
chart/artifact. No new ThetaData API calls - everything here is either a
cache-hit rebuild (OR/POC weekly) or a re-read of amd_grid_results.csv /
vtc_grid_results.csv already on disk.
"""
import sys, json, math, csv
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_research_round2 as h

BASE_DIR = Path(__file__).resolve().parent
DATE_LO, DATE_HI = '2023-07-01', '2026-05-30'


# ── Loaders ───────────────────────────────────────────────────────────────
def load_orpoc_weekly_filtered(spy_daily, spy_1min, spy_5min, all_dates_full, dates):
    """Low+medium 0DTE-options-VOLUME filter (excludes top-tercile-by-
    contract-volume days), matching _or_poc_volume_filter_validation.py -
    the variant that actually clears all four validation splits. NOT the
    SPY-realized-volatility filter (_or_poc_rv_filter_and_validation.py),
    which is a different criterion that fails chronological OOS - see the
    module docstring for how these two got confused for one another and
    how it was resolved."""
    print("Rebuilding OR/POC weekly signals (cache-hit, no new API calls)...")
    results_w, coverage_w, signals_w = h.run_r7_or_poc_weekly_rr3_real(spy_1min, spy_5min, dates, [0.05])
    original_trades = results_w[0.05]

    daily_vol = {}
    for ds in dates:
        chain = h._load_theta_file(ds)
        if not chain:
            continue
        v = sum(q.get('volume', 0) for k, q in chain.items() if k[0] == ds)
        if v > 0:
            daily_vol[ds] = v
    vol_sorted = sorted(daily_vol.items(), key=lambda kv: kv[1])
    n = len(vol_sorted)
    t1, t2 = n // 3, 2 * n // 3
    low_mid_days = set(d for d, v in vol_sorted[:t2])

    trades = [{'date': t.date, 'entry_time': t.entry_time, 'exit_time': t.exit_time,
               'pnl': t.pnl, 'entry_option': t.entry_option}
              for t in original_trades if t.date in low_mid_days]
    return trades


def load_csv_trades(path: Path, match: dict):
    trades = []
    with path.open() as f:
        for row in csv.DictReader(f):
            if all(row[k] == v for k, v in match.items()):
                trades.append({'date': row['date'], 'entry_time': row['entry_time'],
                                'exit_time': row['exit_time'], 'pnl': float(row['pnl']),
                                'entry_option': float(row['entry_option'])})
    return trades


def dedupe_check(trades, label):
    dates = [t['date'] for t in trades]
    if len(dates) != len(set(dates)):
        dupes = {d for d in dates if dates.count(d) > 1}
        raise ValueError(f"{label}: multiple trades on same day(s): {dupes}")


# ── Stats ────────────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {'n': 0, 'wr': 0.0, 'pf': 0.0, 'total_pnl': 0.0}
    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    gw = sum(t['pnl'] for t in wins)
    gl = sum(t['pnl'] for t in losses)
    pf = abs(gw / gl) if gl < 0 else float('inf')
    return {'n': len(trades), 'wr': len(wins) / len(trades) * 100.0,
            'pf': pf, 'total_pnl': sum(t['pnl'] for t in trades)}


def fmt_pf(s):
    p = s.get('pf', 0)
    return 'inf' if p == float('inf') else f'{p:.2f}'


# ── Capital simulation (event-driven, real entry/exit minutes) ────────────
def simulate_capital(trade_groups, starting_capital=2500.0):
    """trade_groups: list of (label, trades) - all pooled into ONE shared
    cash balance (pass a single-element list for a solo-strategy sim, or
    all three for the shared-capital basket sim)."""
    all_trades = []
    for label, trades in trade_groups:
        for t in trades:
            all_trades.append({**t, 'strategy': label})

    events = []
    for idx, t in enumerate(all_trades):
        entry_cost = t['entry_option'] * 100.0
        events.append((t['date'], t['entry_time'], 0, idx, 'entry', entry_cost))
        events.append((t['date'], t['exit_time'], 1, idx, 'exit', None))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    cash = starting_capital
    peak = starting_capital
    max_dd = 0.0
    entry_cost_by_idx = {}
    taken, skipped = set(), []
    curve = []  # (date, time, cash)

    for date, tm, kind, idx, ev, entry_cost in events:
        t = all_trades[idx]
        if ev == 'entry':
            if cash >= entry_cost:
                cash -= entry_cost
                entry_cost_by_idx[idx] = entry_cost
                taken.add(idx)
            else:
                skipped.append(t)
        else:
            if idx in taken:
                cash += entry_cost_by_idx[idx] + t['pnl']
        peak = max(peak, cash)
        max_dd = max(max_dd, peak - cash)
        curve.append({'date': date, 'time': tm, 'cash': round(cash, 2)})

    taken_trades = [all_trades[i] for i in taken]
    return {
        'starting_capital': starting_capital,
        'final_equity': round(cash, 2),
        'total_return_pct': round((cash - starting_capital) / starting_capital * 100.0, 2),
        'max_drawdown': round(max_dd, 2),
        'max_drawdown_pct': round(max_dd / peak * 100.0, 2) if peak else 0.0,
        'n_taken': len(taken_trades),
        'n_skipped_insufficient_capital': len(skipped),
        'curve': curve,
    }


def daily_pnl_series(trades, all_dates):
    by_date = defaultdict(float)
    for t in trades:
        by_date[t['date']] += t['pnl']
    return [by_date.get(d, 0.0) for d in all_dates]


def vol_stats(daily_pnls):
    n = len(daily_pnls)
    if n < 2:
        return {'mean': 0.0, 'std': 0.0, 'sharpe_like': 0.0}
    mu = sum(daily_pnls) / n
    var = sum((x - mu) ** 2 for x in daily_pnls) / (n - 1)
    std = math.sqrt(var)
    sharpe_like = (mu / std * math.sqrt(252)) if std > 0 else 0.0
    return {'mean': round(mu, 4), 'std': round(std, 4), 'sharpe_like': round(sharpe_like, 3)}


def main():
    print("Loading market data...")
    spy_daily = h.load_spy_daily()
    spy_1min  = h.load_spy_1min()
    spy_5min  = h.load_spy_5min()
    all_dates_full = sorted(spy_daily.keys())
    dates = [d for d in all_dates_full if DATE_LO <= d <= DATE_HI]
    print(f"  {len(dates)} trading days in [{DATE_LO}, {DATE_HI}]")

    orpoc = load_orpoc_weekly_filtered(spy_daily, spy_1min, spy_5min, all_dates_full, dates)
    amd   = load_csv_trades(BASE_DIR / 'amd_grid_results.csv',
                             {'variant': 'am15', 'buffer': '0.05', 'confirm_mode': 'simple',
                              'stop_buffer': '0.15', 'target_rr': '3.5'})
    vtc   = load_csv_trades(BASE_DIR / 'vtc_grid_results.csv',
                             {'tolerance': '0.25', 'stop_buffer': '0.15', 'target_rr': '3.5'})

    for label, trades in [('OR/POC weekly (low+med 0DTE-volume filtered)', orpoc),
                           ('AMD fade-breakout (best combo)', amd),
                           ('VWAP Trend Continuation (best combo)', vtc)]:
        dedupe_check(trades, label)
        s = stats(trades)
        print(f"  {label:<42} N={s['n']:>4}  WR={s['wr']:>6.1f}%  PF={fmt_pf(s):>6}  "
              f"Total P&L=${s['total_pnl']:>+10,.2f}")

    A = {t['date'] for t in orpoc}
    B = {t['date'] for t in amd}
    C = {t['date'] for t in vtc}

    # ── 2. Overlap matrix ────────────────────────────────────────────────
    only_A   = A - B - C
    only_B   = B - A - C
    only_C   = C - A - B
    AB_only  = (A & B) - C
    AC_only  = (A & C) - B
    BC_only  = (B & C) - A
    ABC      = A & B & C
    none3    = set(dates) - A - B - C

    print(f"\n{'='*90}\n  OVERLAP MATRIX (A=OR/POC filtered, B=AMD, C=VTC)\n{'='*90}")
    total_days = len(dates)
    for lbl, s in [('A only (OR/POC only)', only_A), ('B only (AMD only)', only_B),
                   ('C only (VTC only)', only_C), ('A & B only', AB_only),
                   ('A & C only', AC_only), ('B & C only', BC_only),
                   ('A & B & C (all three)', ABC), ('none fired', none3)]:
        print(f"  {lbl:<24} {len(s):>5} days  ({len(s)/total_days*100:>5.1f}% of {total_days} trading days)")
    print(f"  A total days: {len(A)}   B total days: {len(B)}   C total days: {len(C)}")

    # ── 3. Standalone vs overlap performance ─────────────────────────────
    print(f"\n{'='*90}\n  STANDALONE (fired alone) vs OVERALL performance\n{'='*90}")
    standalone_results = {}
    for label, trades, own_dates, other_dates in [
            ('OR/POC weekly filtered', orpoc, A, B | C),
            ('AMD fade-breakout',      amd,   B, A | C),
            ('VWAP Trend Continuation',vtc,   C, A | B)]:
        alone_trades   = [t for t in trades if t['date'] not in other_dates]
        overlap_trades = [t for t in trades if t['date'] in other_dates]
        s_all   = stats(trades)
        s_alone = stats(alone_trades)
        s_ovlp  = stats(overlap_trades)
        standalone_results[label] = {'overall': s_all, 'alone': s_alone, 'overlap_days': s_ovlp}
        print(f"\n  {label}")
        print(f"    OVERALL  N={s_all['n']:>4}  WR={s_all['wr']:>6.1f}%  PF={fmt_pf(s_all):>6}  Total P&L=${s_all['total_pnl']:>+9,.2f}")
        print(f"    ALONE    N={s_alone['n']:>4}  WR={s_alone['wr']:>6.1f}%  PF={fmt_pf(s_alone):>6}  Total P&L=${s_alone['total_pnl']:>+9,.2f}")
        print(f"    OVERLAP  N={s_ovlp['n']:>4}  WR={s_ovlp['wr']:>6.1f}%  PF={fmt_pf(s_ovlp):>6}  Total P&L=${s_ovlp['total_pnl']:>+9,.2f}")

    # ── 4. Basket combination ────────────────────────────────────────────
    print(f"\n{'='*90}\n  BASKET (equal-weight, 1 contract per signal, all three combined)\n{'='*90}")
    basket_trades = orpoc + amd + vtc
    s_basket = stats(basket_trades)
    print(f"  BASKET  N={s_basket['n']:>4}  WR={s_basket['wr']:>6.1f}%  PF={fmt_pf(s_basket):>6}  Total P&L=${s_basket['total_pnl']:>+10,.2f}")

    daily_A = daily_pnl_series(orpoc, dates)
    daily_B = daily_pnl_series(amd, dates)
    daily_C = daily_pnl_series(vtc, dates)
    daily_basket = [a + b + c for a, b, c in zip(daily_A, daily_B, daily_C)]

    vol_A, vol_B, vol_C = vol_stats(daily_A), vol_stats(daily_B), vol_stats(daily_C)
    vol_basket = vol_stats(daily_basket)
    print(f"\n  Daily P&L volatility (std dev of $ P&L per trading day, {total_days} days):")
    print(f"    OR/POC filtered   std=${vol_A['std']:>7.2f}  mean=${vol_A['mean']:>6.2f}  sharpe-like={vol_A['sharpe_like']:>6.2f}")
    print(f"    AMD               std=${vol_B['std']:>7.2f}  mean=${vol_B['mean']:>6.2f}  sharpe-like={vol_B['sharpe_like']:>6.2f}")
    print(f"    VTC               std=${vol_C['std']:>7.2f}  mean=${vol_C['mean']:>6.2f}  sharpe-like={vol_C['sharpe_like']:>6.2f}")
    print(f"    BASKET            std=${vol_basket['std']:>7.2f}  mean=${vol_basket['mean']:>6.2f}  sharpe-like={vol_basket['sharpe_like']:>6.2f}")

    # ── 5. Capital-constrained simulation at several account sizes ───────
    all_entry_costs = [t['entry_option'] * 100 for t in (orpoc + amd + vtc)]
    print(f"\n  Contract cost context: min=${min(all_entry_costs):.0f}  "
          f"median=${sorted(all_entry_costs)[len(all_entry_costs)//2]:.0f}  "
          f"mean=${sum(all_entry_costs)/len(all_entry_costs):.0f}  max=${max(all_entry_costs):.0f}")

    CAPITAL_LEVELS = [500, 1000, 2500, 5000, 10000]
    capital_sims = {}
    for cap in CAPITAL_LEVELS:
        print(f"\n{'='*90}\n  ${cap:,} STARTING CAPITAL - event-driven simulation (real entry/exit minutes)\n{'='*90}")
        sim_A = simulate_capital([('OR/POC', orpoc)], starting_capital=cap)
        sim_B = simulate_capital([('AMD', amd)], starting_capital=cap)
        sim_C = simulate_capital([('VTC', vtc)], starting_capital=cap)
        sim_basket = simulate_capital([('OR/POC', orpoc), ('AMD', amd), ('VTC', vtc)], starting_capital=cap)
        capital_sims[cap] = {'orpoc': sim_A, 'amd': sim_B, 'vtc': sim_C, 'basket': sim_basket}

        for label, sim in [('OR/POC weekly filtered', sim_A), ('AMD fade-breakout', sim_B),
                            ('VWAP Trend Continuation', sim_C), (f'BASKET (shared ${cap:,})', sim_basket)]:
            print(f"\n  {label}")
            print(f"    Final equity: ${sim['final_equity']:>10,.2f}   Total return: {sim['total_return_pct']:>+7.1f}%")
            print(f"    Max drawdown: ${sim['max_drawdown']:>10,.2f}   ({sim['max_drawdown_pct']:.1f}% of peak)")
            print(f"    Trades taken: {sim['n_taken']:>4}   Skipped (insufficient capital): {sim['n_skipped_insufficient_capital']}")

    sim_A, sim_B, sim_C, sim_basket = (capital_sims[2500]['orpoc'], capital_sims[2500]['amd'],
                                        capital_sims[2500]['vtc'], capital_sims[2500]['basket'])

    # ── Dump everything for the chart/artifact ───────────────────────────
    out = {
        'dates': dates,
        'overlap': {'only_A': len(only_A), 'only_B': len(only_B), 'only_C': len(only_C),
                    'AB_only': len(AB_only), 'AC_only': len(AC_only), 'BC_only': len(BC_only),
                    'ABC': len(ABC), 'none': len(none3), 'total_days': total_days,
                    'A_total': len(A), 'B_total': len(B), 'C_total': len(C)},
        'standalone': standalone_results,
        'basket_stats': s_basket,
        'individual_stats': {'orpoc': stats(orpoc), 'amd': stats(amd), 'vtc': stats(vtc)},
        'vol': {'orpoc': vol_A, 'amd': vol_B, 'vtc': vol_C, 'basket': vol_basket},
        'daily_pnl': {'dates': dates, 'orpoc': daily_A, 'amd': daily_B, 'vtc': daily_C, 'basket': daily_basket},
        'capital_sims': capital_sims,
    }
    out_path = BASE_DIR / 'strategy_overlap_basket.json'
    with out_path.open('w') as f:
        json.dump(out, f)
    print(f"\nWrote -> {out_path}")


if __name__ == '__main__':
    main()
