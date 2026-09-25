#!/usr/bin/env python3
"""
backtest_lotto_basket_tuned.py
================================
Applies the tweaks found by analyzing backtest_lotto_basket.py's raw
results (calls >> puts, Wed/Thu >> other days, cheaper legs >> pricier
legs within the $0.05-0.15 band) and re-scores the SAME cached ThetaData
(no new API calls -- reuses backtest_lotto_basket.py's per-day pkl cache).

Tweaks tested, each togglable below:
  RIGHTS        -- which side(s) to trade: {'C'} calls-only vs {'C','P'} both
  DAY_FILTER    -- 'all' | 'wed_thu' | 'catalyst' (FOMC/CPI day) |
                   'wed_thu_or_catalyst'
  PRICE_LO/HI   -- entry-ask band (tests the full $0.05-0.15 vs a cheaper
                   $0.05-0.09 sub-band)

Runs every combination in COMBOS and reports full-period stats for each,
so the winning combo is chosen by evidence, not a single blind guess.
"""

import csv
from collections import defaultdict
from datetime import date
from pathlib import Path

import backtest_lotto_basket as base

CONTRACT_MULT = base.CONTRACT_MULT
WORTHLESS_MAX = base.WORTHLESS_MAX
BIG_DAY_MULT = base.BIG_DAY_MULT

REPORT_PATH = Path(r'C:\Users\sagla\backtest_lotto_basket_tuned_report.txt')
BEST_DAILY_CSV = Path(r'C:\Users\sagla\backtest_lotto_basket_tuned_best_daily.csv')

# ─── FOMC + CPI catalyst dates (2023-2026), same universe used elsewhere
# in this project (backtest_real_data.py) -- reproduced here as a static
# constant so this script has no import-time dependency on that module.
FOMC_DATES = {
    date(2023, 2, 1),  date(2023, 3, 22), date(2023, 5, 3),  date(2023, 6, 14),
    date(2023, 7, 26), date(2023, 9, 20), date(2023, 11, 1), date(2023, 12, 13),
    date(2024, 1, 31), date(2024, 3, 20), date(2024, 5, 1),  date(2024, 6, 12),
    date(2024, 7, 31), date(2024, 9, 18), date(2024, 11, 7), date(2024, 12, 18),
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7),  date(2025, 6, 18),
    date(2025, 7, 30), date(2025, 9, 17), date(2025, 11, 5), date(2025, 12, 10),
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 6),
}
CPI_DATES = {
    date(2023, 1, 12), date(2023, 2, 14), date(2023, 3, 14), date(2023, 4, 12),
    date(2023, 5, 10), date(2023, 6, 13), date(2023, 7, 12), date(2023, 8, 10),
    date(2023, 9, 13), date(2023, 10, 12), date(2023, 11, 14), date(2023, 12, 12),
    date(2024, 1, 11), date(2024, 2, 13), date(2024, 3, 12), date(2024, 4, 10),
    date(2024, 5, 15), date(2024, 6, 12), date(2024, 7, 11), date(2024, 8, 14),
    date(2024, 9, 11), date(2024, 10, 10), date(2024, 11, 13), date(2024, 12, 11),
    date(2025, 1, 15), date(2025, 2, 12), date(2025, 3, 12), date(2025, 4, 10),
    date(2025, 5, 13), date(2025, 6, 11), date(2025, 7, 11), date(2025, 8, 13),
    date(2025, 9, 10), date(2025, 10, 15), date(2025, 11, 12), date(2025, 12, 10),
    date(2026, 1, 14), date(2026, 2, 11), date(2026, 3, 11), date(2026, 4, 10),
    date(2026, 5, 13),
}
CATALYST_DATES = FOMC_DATES | CPI_DATES


def day_passes(d: date, day_filter: str) -> bool:
    if day_filter == 'all':
        return True
    if day_filter == 'wed_thu':
        return d.weekday() in (2, 3)
    if day_filter == 'catalyst':
        return d in CATALYST_DATES
    if day_filter == 'wed_thu_or_catalyst':
        return d.weekday() in (2, 3) or d in CATALYST_DATES
    raise ValueError(day_filter)


def select_legs_band(entry, spot, right, n, lo, hi):
    cands = []
    for (k, r), v in entry.items():
        if r != right:
            continue
        ask = v['ask']
        if lo <= ask <= hi:
            cands.append((abs(k - spot), k, ask))
    cands.sort(key=lambda x: x[0])
    return [(k, right, ask) for _, k, ask in cands[:n]]


def run_combo(rights, day_filter, lo, hi, n_configs, dates):
    """Returns {n: [{date, cost, payout, net, all_worthless, big_day}, ...]}"""
    out = {n: [] for n in n_configs}
    for d in dates:
        if not day_passes(d, day_filter):
            continue
        entry = base.get_entry_chain(d)
        if not entry:
            continue
        spot = base.estimate_spot(entry)
        if spot is None:
            continue
        eod = base.get_eod_chain(d)
        if not eod:
            continue

        max_n = max(n_configs)
        pools = {r: select_legs_band(entry, spot, r, max_n, lo, hi) for r in rights}

        for n in n_configs:
            legs = []
            for r in rights:
                legs.extend(pools[r][:n])
            if not legs:
                continue
            cost = sum(round(ask * CONTRACT_MULT, 2) for _, _, ask in legs)
            payout = 0.0
            worthless_flags = []
            for strike, right, ask in legs:
                q = eod.get((strike, right))
                close = q['close'] if q else 0.0
                payout += round(close * CONTRACT_MULT, 2)
                worthless_flags.append(close <= WORTHLESS_MAX)
            net = round(payout - cost, 2)
            out[n].append({
                'date': d.isoformat(), 'num_legs': len(legs),
                'cost': round(cost, 2), 'payout': round(payout, 2), 'net': net,
                'all_worthless': all(worthless_flags),
                'big_day': payout > BIG_DAY_MULT * cost if cost > 0 else False,
            })
    return out


def summarize(rows, label):
    if not rows:
        return f'{label}: NO DATA'
    days = len(rows)
    total_cost = sum(r['cost'] for r in rows)
    total_payout = sum(r['payout'] for r in rows)
    net = total_payout - total_cost
    win_days = sum(1 for r in rows if r['net'] > 0)
    zero_days = sum(1 for r in rows if r['all_worthless'])
    big_days = sum(1 for r in rows if r['big_day'])
    roc = 100 * net / total_cost if total_cost else 0.0
    lines = [
        f'{label}',
        f'  days={days}  spent=${total_cost:,.0f}  returned=${total_payout:,.0f}  '
        f'net=${net:,.0f}  return_on_capital={roc:+.1f}%',
        f'  win_rate={100*win_days/days:.1f}%  avg_day_pnl=${net/days:.2f}  '
        f'zero_days={100*zero_days/days:.1f}%  big_days={100*big_days/days:.1f}%',
    ]
    return '\n'.join(lines)


def main():
    dates = base.get_trading_dates()
    n_configs = [3, 5]

    combos = [
        ('baseline: C+P, all days, $.05-.15',   {'C', 'P'}, 'all', 0.05, 0.15),
        ('calls-only, all days, $.05-.15',       {'C'},      'all', 0.05, 0.15),
        ('calls-only, Wed/Thu, $.05-.15',        {'C'},      'wed_thu', 0.05, 0.15),
        ('calls-only, FOMC/CPI days, $.05-.15',  {'C'},      'catalyst', 0.05, 0.15),
        ('calls-only, Wed/Thu OR catalyst, $.05-.15', {'C'}, 'wed_thu_or_catalyst', 0.05, 0.15),
        ('calls-only, Wed/Thu OR catalyst, $.05-.09', {'C'}, 'wed_thu_or_catalyst', 0.05, 0.09),
    ]

    report_lines = ['0DTE LOTTO BASKET -- TUNED VARIANTS (reusing cached ThetaData)',
                     f'Universe: {len(dates)} trading days, {base.BT_START} to {base.BT_END}',
                     '=' * 78]

    best = None  # (net, label, n, rows)
    for label, rights, day_filter, lo, hi in combos:
        result = run_combo(rights, day_filter, lo, hi, n_configs, dates)
        report_lines.append('')
        for n in n_configs:
            rows = result[n]
            tag = f'{label}  [N={n}]'
            report_lines.append(summarize(rows, tag))
            if rows:
                net = sum(r['net'] for r in rows)
                if best is None or net > best[0]:
                    best = (net, tag, n, rows)

    report_lines.append('')
    report_lines.append('=' * 78)
    report_lines.append(f'BEST COMBO: {best[1]}  ->  net = ${best[0]:,.2f}')

    report_txt = '\n'.join(report_lines)
    REPORT_PATH.write_text(report_txt, encoding='utf-8')
    print(report_txt)

    with BEST_DAILY_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(best[3][0].keys()))
        w.writeheader()
        w.writerows(best[3])


if __name__ == '__main__':
    main()
