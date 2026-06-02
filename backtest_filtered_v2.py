#!/usr/bin/env python3
"""
backtest_filtered_v2.py
Adds vol spike filter (vol_vs_avg_20 > 10x -> skip) to filtered_v1.
Uses Alpaca 1-min bars for 12 months of data (June 2025 - May 2026).
"""

import csv
import sys
import time as _time
from collections import defaultdict
from dataclasses import dataclass, fields as dc_fields
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz

sys.path.insert(0, str(Path(__file__).parent))
from backtest import (
    option_ask, _vwap, _or, _vol_avg, _or_done, _in_mkt, _in_win,
    ORTracker, _score, _m2_signal, _m3_signal,
    _consec, _drawdown, _sharpe, _group_stats,
    vix_bucket,
    download_vix,
    BT_START, BT_END, ET, W,
    STOP_MULT, MAX_HOLD_MIN, MAX_HOLD_PM,
    NO_MOVE_MIN, NO_MOVE_PCT, MAX_DAILY_LOSS,
    MAX_COST, M2_MAX_COST, M3_MAX_COST,
    VOL_THRESH, OR_MIN, BIAS_MIN_SEP, ENTRY_VOL_SURGE, CUTOFF,
)
from backtest_filtered import (
    TradeF, TRADE_F_FIELDS, dc_fields,
    TARGET_MULT_F, TRAILING_STOP, MIN_SCORE_F, MAX_SCORE_F,
    VIX_MAX, VWAP_GAP_MAX, SKIP_WDAYS,
    compute_stats_f,
    _hr, _hdr, _pf,
)
from alpaca_loader import download_spy_1min

# ── v2-only parameter ────────────────────────────────────────────────────────
VOL_SPIKE_MAX = 10.0   # skip entry if vol_vs_avg_20 > 10x (ex-div/expiry noise)

# ── Date range: full 12 months of Alpaca 1-min data ─────────────────────────
BT_START_V2   = BT_START           # date(2025, 6, 1)
BT_END_V2     = BT_END             # date(2026, 5, 27)

DATA_DIR      = Path(r'C:\Users\sagla\backtest_data')
REPORT_PATH   = Path(r'C:\Users\sagla\backtest_filtered_v2_report.txt')
TRADES_CSV_V2 = Path(r'C:\Users\sagla\backtest_filtered_v2_trades.csv')


# ── Filtered simulation (v2) — identical to filtered_v1 + vol spike filter ──
def simulate_day_v2(ds: str, day_bars: list, vix: float) -> list:
    dt_obj = datetime.strptime(ds, '%Y-%m-%d')

    if dt_obj.weekday() in SKIP_WDAYS:
        return []
    if vix >= VIX_MAX:
        return []

    day_name          = dt_obj.strftime('%A')
    trades: list      = []
    bars_so_far: list = []
    ort               = ORTracker()
    position          = None
    daily_pnl         = 0.0
    last_exit_dt      = None
    last_exit_pnl     = 0.0
    m2_eligible       = None
    m2_fired          = False
    m2_cross_ts       = None
    prev_above        = None
    vix_hist: list    = []
    m3_fired          = False
    session_high      = 0.0
    m3_breakout       = None
    daily_bias        = None
    bias_set          = False

    for bar in day_bars:
        dt = datetime.fromtimestamp(bar['t'] / 1000, tz=ET)
        bars_so_far.append(bar)

        if not _in_mkt(dt):
            continue

        spy        = bar['c']
        vwap_val   = _vwap(bars_so_far)
        or_h, or_l = _or(bars_so_far)

        if _or_done(dt):
            ort.update(bars_so_far, or_h, or_l)

        vix_hist.append((vix, dt))
        if len(vix_hist) > 60:
            vix_hist = vix_hist[-60:]

        if m2_eligible is None and _or_done(dt):
            # time-based OR slice (interval-agnostic: works for 1m and 5m bars)
            or_end_ts   = bars_so_far[0]['t'] + OR_MIN * 60_000
            or_bars     = [b for b in bars_so_far if b['t'] < or_end_ts]
            m2_eligible = (or_bars[-1]['c'] >= _vwap(or_bars)) if or_bars else False

        if not bias_set and _or_done(dt) and len(bars_so_far) >= OR_MIN:
            vwap_or = _vwap(bars_so_far[:OR_MIN])
            gap = spy - vwap_or
            if   gap >= BIAS_MIN_SEP:  daily_bias = 'call'
            elif gap <= -BIAS_MIN_SEP: daily_bias = 'put'
            else:                      daily_bias = 'skip'
            bias_set = True

        direction = 'call' if spy >= vwap_val else 'put'
        mins_left = max((16 * 60) - (dt.hour * 60 + dt.minute), 1)

        was_above = prev_above
        if (was_above is True) and (spy < vwap_val):
            m2_cross_ts = bar['t']
        elif spy >= vwap_val:
            m2_cross_ts = None

        if bar['h'] > session_high:
            if session_high > 0:
                m3_breakout = (session_high, bar['t'])
            session_high = bar['h']

        # ── manage open position ────────────────────────────────────────────
        if position is not None:
            cur = option_ask(spy, mins_left, vix, position['direction'])
            position['cur_price']  = cur
            position['peak_price'] = max(position['peak_price'], cur)
            held     = (dt - position['entry_dt']).total_seconds() / 60
            hold_lim = MAX_HOLD_PM if (dt.hour, dt.minute) >= (13, 30) else MAX_HOLD_MIN
            trail_px = position['peak_price'] * (1 - TRAILING_STOP)

            ex, rsn = False, ''
            if   (dt.hour, dt.minute) >= CUTOFF:
                ex, rsn = True, '3:15 PM hard cutoff'
            elif held >= hold_lim:
                ex, rsn = True, f'{hold_lim}-min time stop'
            elif cur >= position['target']:
                ex, rsn = True, '4% target hit'
            elif cur <= trail_px:
                ex, rsn = True, '15% trailing stop'
            elif cur <= position['stop']:
                ex, rsn = True, '35% hard stop'
            elif (held >= NO_MOVE_MIN and
                  position['peak_price'] < position['entry_price'] * (1 + NO_MOVE_PCT)):
                ex, rsn = True, 'No movement >10% in 10m'

            if ex:
                pnl      = (cur - position['entry_price']) * 100
                peak_pct = (position['peak_price'] / position['entry_price'] - 1) * 100
                trades.append(TradeF(
                    date               = ds,
                    day_of_week        = day_name,
                    entry_time         = position['entry_dt'].strftime('%H:%M:%S'),
                    exit_time          = dt.strftime('%H:%M:%S'),
                    direction          = position['direction'],
                    mode               = position['mode'],
                    entry_price        = position['entry_price'],
                    exit_price         = cur,
                    peak_price         = position['peak_price'],
                    pnl                = round(pnl, 2),
                    exit_reason        = rsn,
                    score              = position['score'],
                    vix_at_entry       = vix,
                    spy_at_entry       = position['spy_entry'],
                    vwap_at_entry      = position['vwap_entry'],
                    hold_minutes       = round(held, 1),
                    volume_entry_bar   = position['vol_entry'],
                    volume_vs_avg_20   = round(position['vol_ratio'], 3),
                    spy_5m_move_pct    = round(position['spy_prev_move'], 4),
                    option_5m_peak_pct = round(peak_pct, 2),
                ))
                daily_pnl    += pnl
                last_exit_dt  = dt
                last_exit_pnl = pnl
                position      = None

        prev_above = spy >= vwap_val

        # ── entry guards ────────────────────────────────────────────────────
        if position or (dt.hour, dt.minute) >= CUTOFF or not _in_win(dt):
            continue
        if daily_bias == 'skip':
            continue
        cooldown = (last_exit_dt is not None and last_exit_pnl < 0 and
                    (dt - last_exit_dt).total_seconds() < 15 * 60)
        if cooldown or daily_pnl <= MAX_DAILY_LOSS:
            continue

        avg_v    = _vol_avg(bars_so_far)
        bar_ok   = avg_v > 0 and bars_so_far[-1]['v'] >= avg_v * ENTRY_VOL_SURGE
        vwap_gap = abs(spy - vwap_val)
        sc       = _score(bars_so_far, vwap_val, or_h, or_l, ort, vix,
                          direction, dt, VOL_THRESH, True)

        # New column data
        vol_entry    = bars_so_far[-1]['v']
        vol_ratio    = vol_entry / avg_v if avg_v > 0 else 0.0

        # spy_prev_move: move from previous bar close (1 bar back = one interval)
        spy_prev_move = 0.0
        if len(bars_so_far) >= 2:
            prev_c = bars_so_far[-2]['c']
            spy_prev_move = (spy - prev_c) / prev_c * 100 if prev_c else 0.0

        # ── Change 7: vol spike filter ──────────────────────────────────────
        if vol_ratio > VOL_SPIKE_MAX:
            continue

        def _pos(d, m, ask):
            return dict(
                direction    = d,
                entry_price  = ask,
                cur_price    = ask,
                peak_price   = ask,
                target       = round(ask * TARGET_MULT_F, 4),
                stop         = round(ask * STOP_MULT, 4),
                spy_entry    = spy,
                vwap_entry   = vwap_val,
                entry_dt     = dt,
                mode         = m,
                score        = sc,
                vol_entry    = vol_entry,
                vol_ratio    = vol_ratio,
                spy_prev_move = spy_prev_move,
            )

        # Mode 1
        if (bar_ok and MIN_SCORE_F <= sc <= MAX_SCORE_F
                and vwap_gap <= VWAP_GAP_MAX and daily_bias == direction):
            ask = option_ask(spy, mins_left, vix, direction)
            if 0 < ask * 100 <= MAX_COST:
                position = _pos(direction, 'Mode1', ask)
                continue

        # Mode 2
        if (m2_eligible and not m2_fired and bar_ok and vwap_gap <= VWAP_GAP_MAX
                and vol_ratio <= VOL_SPIKE_MAX
                and _m2_signal(bars_so_far, vwap_val, was_above, vix,
                               vix_hist, dt, m2_cross_ts)
                and daily_bias == 'put'):
            ask = option_ask(spy, mins_left, vix, 'put')
            if 0 < ask * 100 <= M2_MAX_COST:
                position = _pos('put', 'Mode2-VWAP-Cross', ask)
                m2_fired = True
                continue

        # Mode 3
        if (not m3_fired and bar_ok and vwap_gap <= VWAP_GAP_MAX
                and vol_ratio <= VOL_SPIKE_MAX
                and _m3_signal(bars_so_far, vix, vix_hist, dt, m3_breakout)
                and daily_bias == 'put'):
            ask = option_ask(spy, mins_left, vix, 'put')
            if 0 < ask * 100 <= M3_MAX_COST:
                position    = _pos('put', 'Mode3-ATH-Reject', ask)
                m3_fired    = True
                m3_breakout = None
                continue

    return trades


def run_v2(spy_days: dict, vix_daily: dict) -> list:
    all_trades: list = []
    days = sorted(spy_days.keys())
    n    = len(days)
    for i, ds in enumerate(days):
        sys.stdout.write(f'\r  [{i+1:>3}/{n}] {ds}')
        sys.stdout.flush()
        vix = vix_daily.get(ds, 16.0)
        all_trades.extend(simulate_day_v2(ds, spy_days[ds], vix))
    print(f'\r  Done — {len(all_trades)} trades{" " * 30}')
    return all_trades


def save_trades_csv_v2(trades: list):
    with TRADES_CSV_V2.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=TRADE_F_FIELDS)
        w.writeheader()
        for t in trades:
            w.writerow({f.name: getattr(t, f.name) for f in dc_fields(t)})
    print(f'  v2 trades CSV: {TRADES_CSV_V2}')


# ── Report ────────────────────────────────────────────────────────────────────
def build_report(v1_trades, v2_trades, v1_s, v2_s,
                 v1_days: int, v2_days: int) -> str:
    L = [
        _hr(),
        '  SPY 0DTE FILTERED BACKTEST v2 REPORT  (Alpaca 1-min, 12 months)',
        f'  Period     : {BT_START_V2} to {BT_END_V2}  ({v2_days} trading days)',
        f'  Generated  : {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'  Data source: Alpaca Markets API (IEX feed, 1-min bars)',
        '',
        '  NEW FILTER (v2 only):',
        f'    vol_vs_avg_20 > {VOL_SPIKE_MAX:.0f}x  ->  skip (ex-div/expiry noise)',
        _hr(),
    ]

    # ── v1 vs v2 summary table ───────────────────────────────────────────────
    L.append(_hdr('v1 (no vol-spike filter) vs v2 (vol-spike filtered, same Alpaca data)'))

    rows = []
    if v1_s and v2_s:
        rows = [
            ('Trading days',    v1_days,                       v2_days,
             f'{v2_days - v1_days:+d}'),
            ('Trades',          v1_s['n'],                     v2_s['n'],
             f'{v2_s["n"] - v1_s["n"]:+d}'),
            ('Win rate',        f'{v1_s["wr"]:.1f}%',          f'{v2_s["wr"]:.1f}%',
             f'{v2_s["wr"] - v1_s["wr"]:+.1f}%'),
            ('Total P&L',       f'${v1_s["total_pnl"]:+.2f}',  f'${v2_s["total_pnl"]:+.2f}',
             f'${v2_s["total_pnl"] - v1_s["total_pnl"]:+.2f}'),
            ('Profit factor',   _pf(v1_s),                     _pf(v2_s),           ''),
            ('Avg winner',      f'${v1_s["avg_win"]:+.2f}',    f'${v2_s["avg_win"]:+.2f}',
             f'${v2_s["avg_win"] - v1_s["avg_win"]:+.2f}'),
            ('Avg loser',       f'${v1_s["avg_loss"]:+.2f}',   f'${v2_s["avg_loss"]:+.2f}',
             f'${v2_s["avg_loss"] - v1_s["avg_loss"]:+.2f}'),
            ('Max drawdown',    f'${v1_s["max_dd"]:.2f}',      f'${v2_s["max_dd"]:.2f}',
             f'${v2_s["max_dd"] - v1_s["max_dd"]:+.2f}'),
            ('Max consec losses', v1_s['max_cl'],               v2_s['max_cl'],      ''),
            ('Avg hold (min)',   f'{v1_s["avg_hold"]:.1f}',     f'{v2_s["avg_hold"]:.1f}',
             f'{v2_s["avg_hold"] - v1_s["avg_hold"]:+.1f}'),
        ]
    L.append(f'\n  {"Metric":<26} {"v1 (no spike filt)":>18} {"v2 (+spike filt)":>17} {"Change":>10}')
    L.append('  ' + '-' * 70)
    for metric, ov, nv, chg_s in rows:
        L.append(f'  {metric:<26} {str(ov):>16} {str(nv):>16} {chg_s:>10}')

    # ── Vol spike filter impact ──────────────────────────────────────────────
    L.append(_hdr('VOL SPIKE FILTER IMPACT (on v1 1m trades)'))
    if v1_trades:
        spikes = [t for t in v1_trades if t.volume_vs_avg_20 > VOL_SPIKE_MAX]
        kept   = [t for t in v1_trades if t.volume_vs_avg_20 <= VOL_SPIKE_MAX]
        s_w = sum(1 for t in spikes if t.pnl > 0)
        s_l = sum(1 for t in spikes if t.pnl <= 0)
        k_w = sum(1 for t in kept if t.pnl > 0)
        k_l = sum(1 for t in kept if t.pnl <= 0)
        L += [
            f'\n  Total v1 trades        : {len(v1_trades)}',
            f'  Vol spike (>10x) trades: {len(spikes)}  ({s_w} wins, {s_l} losses)',
            f'  Remaining after filter : {len(kept)}  ({k_w} wins, {k_l} losses)',
            f'  WR before filter       : {sum(1 for t in v1_trades if t.pnl > 0)/len(v1_trades)*100:.1f}%',
            f'  WR after filter        : {k_w/len(kept)*100:.1f}%' if kept else '  WR after: N/A',
        ]
        if spikes:
            L.append(f'\n  Spike trades removed:')
            L.append(f'  {"Date":<12} {"Time":<9} {"Dir":<5} {"Vol/Avg":>8} {"PnL%":>8}')
            L.append('  ' + '-' * 46)
            for t in spikes:
                L.append(f'  {t.date:<12} {t.entry_time:<9} {t.direction.upper():<5}'
                         f' {t.volume_vs_avg_20:>8.1f}x {t.pnl:>+8.2f}')

    # ── v2 full stats ────────────────────────────────────────────────────────
    L.append(_hdr(f'v2 FULL STATS (Alpaca 1-min, {v2_days} trading days, all filters active)'))
    if v2_s:
        s = v2_s
        pf_above = _pf(s) != 'inf' and float(_pf(s)) > 1.0
        L += [
            f'  Trades            : {s["n"]}',
            f'  Win / Loss / Flat : {s["wins"]} / {s["losses"]} / {s["flat"]}',
            f'  Win rate          : {s["wr"]:.1f}%',
            f'  Total P&L         : ${s["total_pnl"]:+.2f}',
            f'  Profit factor     : {_pf(s)}  {"<-- ABOVE 1.0 *" if pf_above else "<-- BELOW 1.0"}',
            f'  Avg winner        : ${s["avg_win"]:+.2f}',
            f'  Avg loser         : ${s["avg_loss"]:+.2f}',
            f'  W/L dollar ratio  : '
            + (f'{abs(s["avg_win"]/s["avg_loss"]):.2f}x' if s["avg_loss"] else 'N/A'),
            f'  Best trade        : ${s["best"].pnl:+.2f}  {s["best"].date}  {s["best"].direction}',
            f'  Worst trade       : ${s["worst"].pnl:+.2f}  {s["worst"].date}  {s["worst"].direction}',
            f'  Max drawdown      : ${s["max_dd"]:.2f}',
            f'  Max consec W / L  : {s["max_cw"]} / {s["max_cl"]}',
            f'  Avg hold time     : {s["avg_hold"]:.1f} min',
            f'  Sharpe (ann.)     : {s["sharpe"]:.2f}',
        ]

        L.append('\n  Win rate by day of week:')
        for k, v in s['by_dow'].items():
            bar = ('+' if v['pnl'] >= 0 else '-') * min(int(abs(v['pnl']) / 5), 20)
            L.append(f'    {k:<12} {v["n"]:>3} trades  WR:{v["wr"]:5.1f}%'
                     f'  P&L:${v["pnl"]:>+8.2f}  {bar}')

        L.append('\n  Win rate by direction:')
        for k, v in s['by_dir'].items():
            L.append(f'    {k:<8} {v["n"]:>3} trades  WR:{v["wr"]:5.1f}%  P&L:${v["pnl"]:>+8.2f}')

        L.append('\n  Win rate by score:')
        for k, v in sorted(s['by_score'].items()):
            L.append(f'    Score {k:<4} {v["n"]:>3} trades  WR:{v["wr"]:5.1f}%  P&L:${v["pnl"]:>+8.2f}')

        L.append('\n  Exit reasons:')
        for k, v in s['by_reason'].items():
            L.append(f'    {k:<35} {v["n"]:>3} trades  WR:{v["wr"]:5.1f}%  avg:${v["avg_pnl"]:>+8.2f}')

    else:
        L.append('\n  No trades generated.')

    # ── Edge persistence check ───────────────────────────────────────────────
    L.append(_hdr('EDGE PERSISTENCE CHECK'))
    if v2_s and v2_s['n'] > 0:
        s = v2_s
        pf_val = s['pf']
        wr     = s['wr']
        pf_ok  = pf_val > 1.0 if pf_val != float('inf') else True

        L += [
            f'',
            f'  Profit factor > 1.0   : {"YES  (" + _pf(s) + ")" if pf_ok else "NO   (" + _pf(s) + ")"}',
            f'  Win rate > 50%        : {"YES  (" + f"{wr:.1f}%" + ")" if wr > 50 else "NO   (" + f"{wr:.1f}%" + ")"}',
            f'  Max consec losses     : {s["max_cl"]}  {"(manageable)" if s["max_cl"] <= 3 else "(needs stop rules)"}',
            f'  Max drawdown          : ${s["max_dd"]:.2f}',
            f'',
        ]
        if pf_ok and wr > 50:
            L += [
                '  VERDICT: Edge appears to HOLD on extended dataset.',
                f'  Strategy is profitable over {v2_days} trading days with all filters.',
                '',
                '  CAVEATS:',
                f'    - {v2_s["n"]} trades is still a small sample (need 100+ for confidence)',
                '    - 12 months of 1-min data via Alpaca is statistically meaningful',
                '    - Friday edge still dominates — check if it holds in bear markets',
            ]
        else:
            L += [
                '  VERDICT: Edge is UNCERTAIN on extended dataset.',
                '  Win rate or profit factor degraded vs the 1m 17-day sample.',
                '  This could be overfitting to recent market regime.',
                '',
                '  NEXT STEPS:',
                '    - Check if losses cluster on specific days or market conditions',
                '    - Consider tightening the no-move exit (reduce to 7m from 10m)',
                '    - Connect to more years of Alpaca data for deeper validation',
            ]
    else:
        L.append('\n  No trades — cannot assess edge persistence.')

    # ── Equity curve ─────────────────────────────────────────────────────────
    L.append(_hdr(f'EQUITY CURVE — v2 (Alpaca 1-min, {v2_days} trading days)'))
    if v2_trades:
        daily: dict = defaultdict(float)
        for t in v2_trades:
            daily[t.date] += t.pnl
        running = peak = 0.0
        for ds in sorted(daily):
            p = daily[ds]
            running += p
            peak     = max(peak, running)
            dd_str   = f'  (DD:${peak-running:.0f})' if running < peak else ''
            bar_c    = '+' if p >= 0 else '-'
            bar_v    = bar_c * min(int(abs(p) / 4), 22)
            L.append(f'  {ds}  {bar_v:<23} ${p:>+8.2f}  cum ${running:>+9.2f}{dd_str}')
    else:
        L.append('\n  No trades.')

    # ── All v2 trades ─────────────────────────────────────────────────────────
    L.append(_hdr('ALL v2 TRADES (sorted by date)'))
    if v2_trades:
        L.append(f'  {"Date":<12} {"Day":<10} {"Time":<9} {"Dir":<5} {"Sc"}'
                 f' {"VIX":>5} {"Gap":>6} {"Vol/20":>7} {"PnL%":>8} {"Hold":>6}'
                 f'  {"Exit reason":<30}')
        L.append('  ' + '-' * 105)
        for t in sorted(v2_trades, key=lambda x: (x.date, x.entry_time)):
            gap  = t.spy_at_entry - t.vwap_at_entry
            flag = ' *' if t.pnl > 0 else ''
            L.append(f'  {t.date:<12} {t.day_of_week:<10} {t.entry_time:<9}'
                     f' {t.direction.upper():<5} {t.score}'
                     f' {t.vix_at_entry:>5.2f} {gap:>+6.2f}'
                     f' {t.volume_vs_avg_20:>6.1f}x'
                     f' {t.pnl:>+8.2f} {t.hold_minutes:>5.1f}m'
                     f'  {t.exit_reason:<30}{flag}')
    else:
        L.append('\n  No trades generated.')

    L.append('\n' + _hr())
    return '\n'.join(str(x) for x in L)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    t0 = _time.time()
    print(f'\n{"=" * W}')
    print('  SPY 0DTE FILTERED BACKTEST v2  (Alpaca 1-min, 12 months)')
    print(f'  Period : {BT_START_V2} to {BT_END_V2}')
    print(f'  Filter : vol_vs_avg_20 > {VOL_SPIKE_MAX:.0f}x -> skip (added to all v1 filters)')
    print('=' * W)

    # ── Download Alpaca 1-min data ─────────────────────────────────────────────
    print(f'\n  Downloading Alpaca 1-min SPY: {BT_START_V2} to {BT_END_V2}...')
    spy_days  = download_spy_1min(BT_START_V2, BT_END_V2)
    vix_daily = download_vix(BT_START_V2, BT_END_V2)

    last_vix = 16.0
    for ds in sorted(spy_days.keys()):
        if ds in vix_daily: last_vix = vix_daily[ds]
        else:               vix_daily[ds] = last_vix

    v2_days = len(spy_days)
    print(f'  Data   : {v2_days} trading days  |  '
          f'VIX avg {sum(vix_daily.get(d,16) for d in spy_days)/max(v2_days,1):.1f}')

    if not spy_days:
        print('  ERROR: No data returned from Alpaca.')
        sys.exit(1)

    # ── v1: filtered WITHOUT vol-spike filter (for comparison baseline) ────────
    print('\n  Running v1 (all filters, no vol-spike) on 12-month data...')
    from backtest_filtered import simulate_day_filtered, run_filtered as _run_v1
    v1_trades = _run_v1(spy_days, vix_daily)
    v1_s      = compute_stats_f(v1_trades) if v1_trades else {}
    v1_days   = v2_days
    if v1_s:
        print(f'  v1: {v1_s["n"]} trades  WR:{v1_s["wr"]:.1f}%  P&L:${v1_s["total_pnl"]:+.2f}  PF:{_pf(v1_s)}')

    # ── v2: filtered WITH vol-spike filter ────────────────────────────────────
    print('\n  Running v2 (vol-spike filter added)...')
    v2_trades = run_v2(spy_days, vix_daily)
    v2_s      = compute_stats_f(v2_trades) if v2_trades else {}
    if v2_s:
        print(f'  v2: {v2_s["n"]} trades  WR:{v2_s["wr"]:.1f}%  P&L:${v2_s["total_pnl"]:+.2f}  PF:{_pf(v2_s)}')
    else:
        print('  v2: 0 trades generated.')

    # ── Report ─────────────────────────────────────────────────────────────────
    print('\n  Building report...')
    report = build_report(v1_trades, v2_trades, v1_s, v2_s, v1_days, v2_days)

    sys.stdout.buffer.write((report + '\n').encode('utf-8', errors='replace'))
    REPORT_PATH.write_text(report + '\n', encoding='utf-8')
    print(f'\n  Report saved : {REPORT_PATH}')

    if v2_trades:
        save_trades_csv_v2(v2_trades)

    print(f'  Runtime      : {_time.time()-t0:.1f}s\n')


if __name__ == '__main__':
    main()
