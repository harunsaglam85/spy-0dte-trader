#!/usr/bin/env python3
"""
monte_carlo.py
Monte Carlo simulator for SPY 0DTE options strategy.
Tests 15 WR x Kelly combinations with and without a circuit breaker.
"""

import random
import math
from pathlib import Path
from datetime import datetime

REPORT_PATH = Path(r'C:\Users\sagla\monte_carlo_report.txt')

# ── Simulation parameters ─────────────────────────────────────────────────────
STARTING    = 2_500
N_TRADES    = 80
N_SIMS      = 10_000
PAYOFF      = 1.10          # avg_win / avg_loss dollar ratio
WIN_RATES   = [0.55, 0.60, 0.64, 0.66, 0.70]
KELLY_FRACS = [0.25, 0.50, 1.00]
TARGET      = 100_000
RUIN_PCT    = 0.10          # ruin = losing 90%+ of starting capital
LOSS50_PCT  = 0.50
SEED        = 42

# Circuit breaker
CB_WINDOW   = 20
CB_THRESH   = 0.58
CB_FACTOR   = 0.50

W = 80


# ── Kelly math ────────────────────────────────────────────────────────────────
def full_kelly(p):
    """Full Kelly fraction for binary outcome with PAYOFF ratio."""
    q = 1.0 - p
    f = (PAYOFF * p - q) / PAYOFF
    return max(f, 0.0)


def kelly_log_growth(p, f):
    """Expected log-growth per trade at Kelly fraction f."""
    wf = 1.0 + f * PAYOFF
    lf = 1.0 - f
    if wf <= 0 or lf <= 0:
        return float('-inf')
    return p * math.log(wf) + (1 - p) * math.log(lf)


# ── Core simulation ───────────────────────────────────────────────────────────
def run(p, kf, rand_matrix, use_cb=False):
    """
    Simulate N_SIMS paths of N_TRADES each.
    rand_matrix: list[list[float]], pre-generated, shape N_SIMS x N_TRADES.
    Returns (final_balances, ever_reached_target_flags).
    """
    fk    = full_kelly(p)
    base_f = kf * fk

    w_mult = 1.0 + base_f * PAYOFF   # capital factor on win
    l_mult = 1.0 - base_f            # capital factor on loss

    # CB variants
    cb_f     = base_f * CB_FACTOR
    cb_w_mul = 1.0 + cb_f * PAYOFF
    cb_l_mul = 1.0 - cb_f

    finals    = []
    ever_hit  = []

    for row in rand_matrix:
        cap     = float(STARTING)
        peak    = cap
        history = []          # 1 = win, 0 = loss (last CB_WINDOW trades)

        for r in row:
            if cap < 1.0:
                history.append(0)
                continue

            win = r < p

            if use_cb and len(history) >= CB_WINDOW:
                recent_wr = sum(history[-CB_WINDOW:]) / CB_WINDOW
                if recent_wr < CB_THRESH:
                    cap *= cb_w_mul if win else cb_l_mul
                else:
                    cap *= w_mult if win else l_mult
            else:
                cap *= w_mult if win else l_mult

            if cap > peak:
                peak = cap
            history.append(1 if win else 0)

        finals.append(cap)
        ever_hit.append(peak >= TARGET)

    return finals, ever_hit


def pct(sorted_data, p):
    n = len(sorted_data)
    if n == 0:
        return 0.0
    idx = p / 100.0 * (n - 1)
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    return sorted_data[lo] + (idx - lo) * (sorted_data[hi] - sorted_data[lo])


def summarise(finals, ever_hit):
    n = len(finals)
    s = sorted(finals)
    return dict(
        median   = pct(s, 50),
        p10      = pct(s, 10),
        p90      = pct(s, 90),
        p_100k   = sum(ever_hit) / n * 100,
        p_loss50 = sum(1 for f in finals if f <= STARTING * LOSS50_PCT) / n * 100,
        p_ruin   = sum(1 for f in finals if f <= STARTING * RUIN_PCT) / n * 100,
    )


def rating(s, kf):
    """Human-readable rating for a result set."""
    if s['p_ruin'] > 15:              return 'BLOW-UP RISK'
    if s['p_ruin'] > 8:               return 'HIGH RISK'
    if s['p_100k'] >= 40:             return 'AGGRESSIVE GROWTH'
    if s['p_100k'] >= 20 and s['p_ruin'] <= 5: return '*** SWEET SPOT ***'
    if s['p_100k'] >= 10 and s['p_ruin'] <= 5: return 'GOOD'
    if s['p_loss50'] > 30:            return 'VOLATILE'
    if s['p_100k'] < 2:               return 'TOO CONSERVATIVE'
    return 'MODERATE'


# ── Formatting ────────────────────────────────────────────────────────────────
def fmt(v):
    """Format dollar values compactly."""
    if v >= 1_000_000: return f'${v/1_000_000:.1f}M'
    if v >= 100_000:   return f'${v/1_000:.0f}k'
    if v >= 10_000:    return f'${v/1_000:.1f}k'
    if v >= 1_000:     return f'${v:,.0f}'
    return f'${v:.0f}'


def fmt_pct(v):
    return f'{v:.1f}%'


def _hr(c='=', n=106): return c * n


# ── ASCII equity growth chart ─────────────────────────────────────────────────
def equity_chart(scenarios):
    """
    scenarios: list of (label, p, f) tuples.
    Plots median growth trajectories over N_TRADES trades.
    """
    HEIGHT = 18
    WIDTH  = 60

    curves = {}
    for label, p, kf in scenarios:
        fk = full_kelly(p)
        f  = kf * fk
        g  = kelly_log_growth(p, f)
        path = [STARTING * math.exp(g * t) for t in range(N_TRADES + 1)]
        curves[label] = path

    max_val = max(max(v) for v in curves.values())
    min_val = STARTING * 0.5

    def scale_y(v):
        return int((math.log(max(v, 1)) - math.log(min_val)) /
                   (math.log(max_val) - math.log(min_val)) * (HEIGHT - 1))

    grid = [[' '] * WIDTH for _ in range(HEIGHT)]
    symbols = ['#', '*', '+', '@', 'o']

    for idx, (label, p, kf) in enumerate(scenarios):
        sym = symbols[idx % len(symbols)]
        path = curves[label]
        step = (N_TRADES) / (WIDTH - 1)
        for col in range(WIDTH):
            trade_idx = int(col * step)
            v = path[min(trade_idx, len(path) - 1)]
            row = HEIGHT - 1 - scale_y(v)
            if 0 <= row < HEIGHT:
                grid[row][col] = sym

    lines = []
    y_labels = [
        (HEIGHT - 1 - scale_y(TARGET), fmt(TARGET)),
        (HEIGHT - 1 - scale_y(STARTING * 2), fmt(STARTING * 2)),
        (HEIGHT - 1 - scale_y(STARTING), fmt(STARTING)),
    ]

    for r in range(HEIGHT):
        y_lbl = next((lbl for row, lbl in y_labels if row == r), '')
        lines.append(f'  {y_lbl:>8}  |{"".join(grid[r])}|')

    lines.append('           +' + '-' * WIDTH + '+')
    lines.append(f'           Trade 0{" " * (WIDTH - 18)}Trade {N_TRADES}')

    legend = '  Legend: ' + '  '.join(
        f'{symbols[i % len(symbols)]}={lbl}' for i, (lbl, _, _) in enumerate(scenarios)
    )
    lines.append(legend)
    return '\n'.join(lines)


# ── Report builder ────────────────────────────────────────────────────────────
def build_report(all_results, cb_results):
    """
    all_results: list of (p, kf, s_nocp, s_cb)
    """
    L = [
        _hr(),
        '  MONTE CARLO SIMULATOR — SPY 0DTE OPTIONS STRATEGY',
        f'  Starting capital : ${STARTING:,}',
        f'  Trades simulated : {N_TRADES}  (~{N_TRADES // 10} months at 10 trades/month)',
        f'  Simulations      : {N_SIMS:,}  per combination',
        f'  Payoff ratio     : {PAYOFF}x  (avg winner / avg loser)',
        f'  Win rates tested : {[f"{int(p*100)}%" for p in WIN_RATES]}',
        f'  Kelly fractions  : {[f"{k}x" for k in KELLY_FRACS]}',
        f'  Goal             : ${TARGET:,}  |  Ruin = lose 90%+  |  Bad = lose 50%+',
        '',
        f'  CIRCUIT BREAKER  : If live WR over last {CB_WINDOW} trades < {CB_THRESH:.0%} ',
        f'                     -> cut bet size to {CB_FACTOR:.0%} of normal until WR recovers',
        f'  Generated        : {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        _hr(),
    ]

    # ── SECTION 1: Main results table ─────────────────────────────────────────
    L.append(_hdr('SECTION 1: RESULTS WITHOUT CIRCUIT BREAKER'))
    C = [5, 6, 6, 6, 9, 9, 9, 8, 8, 8, 20]

    def hrow(*vals):
        return '  ' + ''.join(f'{str(v):>{c}}' for v, c in zip(vals, C))

    L.append('\n' + hrow('WR', 'Kelly', 'fK%', 'Bet%',
                          'Median', 'P10(bad)', 'P90(good)',
                          'P(100k)', 'P(-50%)', 'P(ruin)', 'Rating'))
    L.append('  ' + '-' * sum(C))

    sweet_spots = []
    for p, kf, s, _ in all_results:
        fk   = full_kelly(p)
        bet  = kf * fk
        rtg  = rating(s, kf)
        if '***' in rtg:
            sweet_spots.append((p, kf, s))

        marker = ' <--' if '***' in rtg else ''
        L.append(hrow(
            f'{int(p*100)}%',
            f'{kf}x',
            f'{fk*100:.1f}%',
            f'{bet*100:.1f}%',
            fmt(s['median']),
            fmt(s['p10']),
            fmt(s['p90']),
            fmt_pct(s['p_100k']),
            fmt_pct(s['p_loss50']),
            fmt_pct(s['p_ruin']),
            rtg + marker,
        ))

        # Separator between Kelly groups
        if kf == KELLY_FRACS[-1] and p != WIN_RATES[-1]:
            L.append('  ' + '-' * sum(C))

    # ── SECTION 2: Circuit breaker impact ─────────────────────────────────────
    L.append('\n' + _hdr('SECTION 2: CIRCUIT BREAKER IMPACT'))
    L.append(f'  CB fires when: WR over last {CB_WINDOW} trades < {CB_THRESH:.0%}')
    L.append(f'  Effect: bet size halved for the remainder of that trade\n')

    CB2 = [5, 6, 9, 9, 9, 8, 8, 8, 12, 12]

    def crow(*vals):
        return '  ' + ''.join(f'{str(v):>{c}}' for v, c in zip(vals, CB2))

    L.append(crow('WR', 'Kelly', 'Med(noCB)', 'Med(CB)',
                  'dMedian', 'P100k(no)', 'P100k(CB)',
                  'Ruin(no)', 'Ruin(CB)', 'CB verdict'))
    L.append('  ' + '-' * sum(CB2))

    for p, kf, s_no, s_cb in all_results:
        d_med  = s_cb['median']   - s_no['median']
        d_100k = s_cb['p_100k']   - s_no['p_100k']
        d_ruin = s_cb['p_ruin']   - s_no['p_ruin']

        if d_ruin < -2 and d_med > -s_no['median'] * 0.15:
            cb_ver = 'SAFETY NET (+)'
        elif d_ruin < -1:
            cb_ver = 'Reduces risk'
        elif abs(d_ruin) < 0.5 and abs(d_100k) < 1:
            cb_ver = 'Minimal effect'
        else:
            cb_ver = f'Costs ${-d_med:,.0f} median'

        L.append(crow(
            f'{int(p*100)}%',
            f'{kf}x',
            fmt(s_no['median']),
            fmt(s_cb['median']),
            f'${d_med:+,.0f}',
            fmt_pct(s_no['p_100k']),
            fmt_pct(s_cb['p_100k']),
            fmt_pct(s_no['p_ruin']),
            fmt_pct(s_cb['p_ruin']),
            cb_ver,
        ))
        if kf == KELLY_FRACS[-1] and p != WIN_RATES[-1]:
            L.append('  ' + '-' * sum(CB2))

    # ── SECTION 3: Ranked by P(100k) ─────────────────────────────────────────
    L.append('\n' + _hdr('SECTION 3: RANKED BY PROBABILITY OF REACHING $100k'))

    ranked = sorted(all_results, key=lambda x: x[2]['p_100k'], reverse=True)

    L.append(f'\n  {"Rank":<5} {"WR":>5} {"Kelly":>6} {"P(100k)":>8}'
             f' {"Median":>9} {"P(ruin)":>8} {"Sharpe-ish":>11} {"Rating"}')
    L.append('  ' + '-' * 74)

    for i, (p, kf, s, _) in enumerate(ranked, 1):
        # Sharpe-ish: log(median/start) / sqrt(n_trades) normalized
        if s['median'] > 0 and s['p10'] > 0:
            log_med = math.log(s['median'] / STARTING)
            log_p10 = math.log(max(s['p10'], 1) / STARTING)
            log_p90 = math.log(max(s['p90'], 1) / STARTING)
            pseudo_sharpe = log_med / max(log_med - log_p10, 0.001)
        else:
            pseudo_sharpe = 0.0

        marker = ' ***' if i <= 3 else ''
        L.append(f'  {i:<5} {int(p*100):>4}% {kf:>5}x'
                 f' {fmt_pct(s["p_100k"]):>8}'
                 f' {fmt(s["median"]):>9}'
                 f' {fmt_pct(s["p_ruin"]):>8}'
                 f' {pseudo_sharpe:>10.2f}'
                 f'  {rating(s, kf)}{marker}')

    # ── SECTION 4: Blow-up candidates ─────────────────────────────────────────
    L.append('\n' + _hdr('SECTION 4: BLOW-UP RISK ANALYSIS'))

    risky = [(p, kf, s, s_cb) for p, kf, s, s_cb in all_results if s['p_ruin'] > 5]
    safe  = [(p, kf, s, s_cb) for p, kf, s, s_cb in all_results if s['p_ruin'] <= 2]

    L.append(f'\n  Combinations with ruin risk > 5%:')
    if risky:
        for p, kf, s, s_cb in risky:
            L.append(f'    WR {int(p*100)}% / Kelly {kf}x  ->  '
                     f'P(ruin) = {s["p_ruin"]:.1f}%  '
                     f'P(ruin with CB) = {s_cb["p_ruin"]:.1f}%  '
                     f'(CB saves {s["p_ruin"] - s_cb["p_ruin"]:+.1f}%)')
    else:
        L.append('    None — all combinations have ruin risk below 5%.')

    L.append(f'\n  Combinations with ruin risk <= 2% (safest):')
    for p, kf, s, s_cb in safe:
        L.append(f'    WR {int(p*100)}% / Kelly {kf}x  ->  '
                 f'P(ruin) = {s["p_ruin"]:.1f}%  '
                 f'Median = {fmt(s["median"])}  '
                 f'P(100k) = {fmt_pct(s["p_100k"])}')

    # ── SECTION 5: Sweet spot analysis ────────────────────────────────────────
    L.append('\n' + _hdr('SECTION 5: SWEET SPOT ANALYSIS'))

    # Score each combination: reward P(100k), penalise ruin and loss50
    def score(s):
        if s['p_ruin'] > 10: return -999
        return (s['p_100k'] * 2.0
                - s['p_ruin'] * 3.0
                - s['p_loss50'] * 0.5
                + math.log(max(s['median'] / STARTING, 0.001)) * 5)

    scored = sorted(all_results, key=lambda x: score(x[2]), reverse=True)

    L.append('')
    L.append('  Composite score = P(100k)*2 - P(ruin)*3 - P(-50%)*0.5 + log(median/start)*5')
    L.append(f'\n  {"Rank":<5} {"WR":>5} {"Kelly":>6} {"Score":>7}'
             f' {"P(100k)":>8} {"P(-50%)":>8} {"P(ruin)":>8}'
             f' {"Median":>9} {"P10":>9} {"P90":>9}')
    L.append('  ' + '-' * 80)

    for i, (p, kf, s, _) in enumerate(scored[:10], 1):
        sc = score(s)
        marker = ' <-- OPTIMAL' if i == 1 else (' <-- 2nd' if i == 2 else '')
        L.append(f'  {i:<5} {int(p*100):>4}% {kf:>5}x {sc:>7.1f}'
                 f' {fmt_pct(s["p_100k"]):>8}'
                 f' {fmt_pct(s["p_loss50"]):>8}'
                 f' {fmt_pct(s["p_ruin"]):>8}'
                 f' {fmt(s["median"]):>9}'
                 f' {fmt(s["p10"]):>9}'
                 f' {fmt(s["p90"]):>9}'
                 f'{marker}')

    best_p, best_kf, best_s, best_cb = scored[0]
    L += [
        '',
        f'  OPTIMAL COMBINATION: WR {int(best_p*100)}% / Kelly {best_kf}x',
        f'    Full Kelly fraction : {full_kelly(best_p)*100:.1f}%  of capital per trade',
        f'    Actual bet fraction : {best_kf*full_kelly(best_p)*100:.1f}%  of capital per trade',
        f'    At ${STARTING:,} starting: initial bet = ${best_kf*full_kelly(best_p)*STARTING:,.0f} per trade',
        f'    Expected log-growth : {kelly_log_growth(best_p, best_kf*full_kelly(best_p))*100:.2f}% per trade',
        f'    Median after {N_TRADES} trades: {fmt(best_s["median"])}',
        f'    10th pct (bad luck) : {fmt(best_s["p10"])}',
        f'    90th pct (good luck): {fmt(best_s["p90"])}',
        f'    P(reach $100k)      : {fmt_pct(best_s["p_100k"])}  (without CB)  /  {fmt_pct(best_cb["p_100k"])} (with CB)',
        f'    P(lose 50%+)        : {fmt_pct(best_s["p_loss50"])}',
        f'    P(ruin 90%+)        : {fmt_pct(best_s["p_ruin"])}  (without CB)  /  {fmt_pct(best_cb["p_ruin"])} (with CB)',
    ]

    # ── SECTION 6: Median growth trajectory (ASCII) ────────────────────────────
    L.append('\n' + _hdr('SECTION 6: MEDIAN GROWTH TRAJECTORY (log scale, theoretical)'))
    chart_scenarios = [
        ('64%/0.25K', 0.64, 0.25),
        ('64%/0.50K', 0.64, 0.50),
        ('66%/0.50K', 0.66, 0.50),
        ('64%/1.0K',  0.64, 1.00),
        ('70%/0.50K', 0.70, 0.50),
    ]
    L.append('')
    L.append(equity_chart(chart_scenarios))

    # ── SECTION 7: Reality check ───────────────────────────────────────────────
    L.append('\n' + _hdr('SECTION 7: REALITY CHECK & PRACTICAL NOTES'))
    L.append(f"""
  WHAT THIS SIMULATION ASSUMES (and where reality differs):
  ─────────────────────────────────────────────────────────
  1. FIXED PAYOFF RATIO  : Model uses {PAYOFF}x (win/loss).
     Reality              : Varies per trade. Avg winner $22.88, avg loser $18.86 = 1.21x.
     Effect               : Results may be slightly pessimistic; actual edge is better.

  2. CONSTANT WIN RATE   : Model treats each trade as independent with fixed WR.
     Reality              : WR clusters by market regime. Bull runs string winners;
                            vol spikes string losers. Drawdowns are correlated, not random.
     Effect               : P10 (bad luck scenario) is likely worse than shown — real
                            losing streaks are longer and more painful than Monte Carlo implies.

  3. VARIABLE BET SIZE   : Model scales bet with capital (Kelly). At $2,500 start,
                            0.5x Kelly at 64% WR = ~$195/trade initially.
     Reality              : You have a fixed $500/contract cost floor. You can't trade
                            fractional contracts. So true scaling only works at:
                            $1 contract = up to ~$3,200 capital (15.6% Kelly fraction).
                            Below that, you're forced to use MORE than Kelly (over-betting).
     Practical cap        : Start 1 contract. Add 2nd at ~$6,400. 3rd at ~$9,600.

  4. B-S PRICING         : Backtests use Black-Scholes + 4% spread. Real fill slippage
                            and bid-ask on 0DTE options can be $0.05-$0.10/share ($5-$10
                            per contract). This is 25-45% of avg winner $22.
     Effect               : Real WR and payoff ratio may be 5-10% worse than backtested.

  RECOMMENDED APPROACH FOR LIVE TRADING:
  ─────────────────────────────────────────────────────────
  Phase 1 (first 30 trades): 1 contract, paper track actual WR and payoff.
    - Target WR: >=58%  (minimum to keep positive EV)
    - If WR < 55% after 30 trades: strategy has failed, stop.
    - If WR >= 60%: continue to Phase 2.

  Phase 2 (trades 31-80): Scale to 2 contracts if capital >= $5,000 and WR >= 60%.
    - This corresponds approximately to 0.5x Kelly at 64% WR.
    - Expected median after 80 total trades: ~$50-70k (if WR holds).

  Phase 3 (80+ trades): Scale to 3+ contracts only after confirming:
    - Consistent WR >= 62% over at least 50 live trades.
    - Max drawdown in live trading <= 30% (vs simulated P10).

  CIRCUIT BREAKER — HOW TO IMPLEMENT LIVE:
  ─────────────────────────────────────────────────────────
  After every trade, calculate: WR over last {CB_WINDOW} trades.
  If WR < {CB_THRESH:.0%}:  drop to 1 contract regardless of capital.
  When WR recovers to {CB_THRESH + 0.05:.0%}+ over last {CB_WINDOW} trades: restore normal sizing.
  This costs some median P&L but meaningfully cuts ruin risk at full Kelly.
""")

    L.append(_hr())
    return '\n'.join(str(x) for x in L)


def _hdr(title, w=106):
    return f'\n{"=" * w}\n  {title}\n{"=" * w}'


def _hr(c='=', n=106): return c * n


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import time
    t0 = time.time()

    print(f'\n{"=" * W}')
    print('  MONTE CARLO SIMULATOR — SPY 0DTE OPTIONS')
    print(f'  {len(WIN_RATES)} win rates x {len(KELLY_FRACS)} Kelly fractions = '
          f'{len(WIN_RATES)*len(KELLY_FRACS)} combinations')
    print(f'  {N_SIMS:,} simulations x {N_TRADES} trades each')
    print('=' * W)

    # Pre-generate random numbers once — shared across all WR/Kelly combinations
    print(f'\n  Pre-generating {N_SIMS:,} x {N_TRADES} random numbers...')
    random.seed(SEED)
    rand_matrix = [[random.random() for _ in range(N_TRADES)]
                   for _ in range(N_SIMS)]

    all_results = []
    total = len(WIN_RATES) * len(KELLY_FRACS)
    run_n = 0

    for p in WIN_RATES:
        for kf in KELLY_FRACS:
            run_n += 1
            fk  = full_kelly(p)
            bet = kf * fk
            print(f'  [{run_n:>2}/{total}] WR={int(p*100)}%  Kelly={kf}x  '
                  f'fK={fk*100:.1f}%  bet={bet*100:.1f}%', end='  ', flush=True)

            finals_no, hits_no = run(p, kf, rand_matrix, use_cb=False)
            finals_cb, hits_cb = run(p, kf, rand_matrix, use_cb=True)

            s_no = summarise(finals_no, hits_no)
            s_cb = summarise(finals_cb, hits_cb)

            all_results.append((p, kf, s_no, s_cb))

            print(f'Median ${s_no["median"]:>8,.0f}  P(100k) {s_no["p_100k"]:>5.1f}%  '
                  f'P(ruin) {s_no["p_ruin"]:>4.1f}%  |CB P(ruin) {s_cb["p_ruin"]:>4.1f}%')

    print(f'\n  Building report...')
    report = build_report(all_results, None)

    import sys
    sys.stdout.buffer.write((report + '\n').encode('utf-8', errors='replace'))
    REPORT_PATH.write_text(report + '\n', encoding='utf-8')
    print(f'\n  Report saved : {REPORT_PATH}')
    print(f'  Runtime      : {time.time()-t0:.1f}s\n')


if __name__ == '__main__':
    main()
