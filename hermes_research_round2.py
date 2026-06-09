#!/usr/bin/env python3
"""
hermes_research_round2.py
=========================
Round 2 research engine — fixes from round 1 + new hypotheses based on data signals.

Key learnings from Round 1:
- Gap-up momentum FAILS (gaps fade, not continue) — H4 dead WR 40.5%
- Monday gap-FILL has 80% WR but too few trades — H3 needs looser filters
- Pre-FOMC drift: real effect but BS pricing overprices options — need cheaper debit spreads
- Credit spreads generating 0 trades: strike formula bug fixed this round
- Post-earnings: IV proxy too expensive — need to use lower IV assumption

New directions:
- R1: Monday gap-fill RELAXED (any gap > 0.15%, not 0.3%) — exploit the real 80% WR signal
- R2: Put credit spread — correct delta formula using ATM × (1 - 1.5σ/sqrt(T)) 
- R3: Gap FADE (opposite of H4) — fade morning gap-ups by buying puts
- R4: Pre-FOMC debit spread (not naked call) — defined risk, cheaper entry
- R5: VIX-contango roll: sell near VIX, buy far (pure IV mean reversion structure)
- R6: End-of-week theta capture: wider time window credit spread Fri AM  
- R7: ATR-breakout momentum: buy calls when SPY breaks 14-day ATR range
- R8: Post-FOMC volatility collapse: sell straddle day AFTER FOMC (IV crush)
- R9: Opening range breakout: 30-min OR, entry on breakout with volume
- R10: Consecutive down-day reversal: 3+ red days → buy calls on day 4
"""

import csv, json, math, os, pickle, random, sys, time, warnings
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

warnings.filterwarnings('ignore')

import numpy as np
import pytz
import yfinance as yf
from scipy.optimize import brentq
from py_vollib.black_scholes import black_scholes as _pv_bs
from py_vollib.black_scholes.implied_volatility import implied_volatility as _pv_iv
from py_vollib.black_scholes.greeks.analytical import delta as _pv_delta

try:
    import requests as _req
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

BASE        = Path('/root/spy-0dte-trader')
DATA_DIR    = BASE / 'backtest_data'
RESEARCH    = BASE / 'hermes_research'
JOURNAL     = RESEARCH / 'journal_r2.md'
RESULTS_DIR = RESEARCH / 'results_r2'
RESEARCH.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

def _load_env():
    env = {}
    p = BASE / '.env'
    if p.exists():
        for line in p.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env

ENV      = _load_env()
TG_TOKEN = ENV.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT  = ENV.get('TELEGRAM_CHAT_ID', '')

def tg(msg: str):
    if not REQUESTS_OK or not TG_TOKEN or not TG_CHAT:
        print(f'[TG] {msg[:200]}'); return
    try:
        _req.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10)
    except Exception as e:
        print(f'[TG ERROR] {e}')

ET              = pytz.timezone('America/New_York')
BT_START        = date(2021, 1, 4)
BT_END          = date(2026, 5, 30)
SPLIT_DATE      = date(2023, 7, 1)
BLIND_YEAR      = 2025
TRADING_MINS    = 252 * 390

_RFR = {2021:0.001, 2022:0.015, 2023:0.045, 2024:0.050, 2025:0.045, 2026:0.043}
def rfr(dt) -> float:
    yr = dt.year if hasattr(dt,'year') else int(str(dt)[:4])
    return _RFR.get(yr, 0.045)

# ── Data loaders ──────────────────────────────────────────────────────────────
def load_vix():
    p = DATA_DIR / 'vix_2021-01-04_2026-05-30.csv'
    out = {}
    with p.open() as f:
        for row in csv.DictReader(f):
            out[row['date']] = float(row['vix'])
    return out

def load_spy_daily():
    with (DATA_DIR / 'spy_daily_2021-01-04_2026-05-30.pkl').open('rb') as f:
        return pickle.load(f)

def load_spy_5min():
    with (DATA_DIR / 'spy_5min_2021-01-04_2026-05-30.pkl').open('rb') as f:
        return pickle.load(f)

def get_theta_exps():
    return sorted(
        date.fromisoformat(p.stem[len('theta_SPY_'):])
        for p in DATA_DIR.glob('theta_SPY_????-??-??.pkl')
        if len(p.stem) == len('theta_SPY_2021-01-04'))

def load_stock_daily(ticker):
    p = DATA_DIR / f'{ticker}_daily_2021-01-04_2026-05-30.pkl'
    if p.exists():
        with p.open('rb') as f: return pickle.load(f)
    return {}

# ── Math ──────────────────────────────────────────────────────────────────────
def bs_price(S, K, T, r, sigma, flag):
    if T <= 1e-7 or sigma <= 1e-6:
        return max(S-K,0) if flag=='c' else max(K-S,0)
    try:
        return float(_pv_bs(flag, S, K, T, r, sigma))
    except Exception:
        d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
        d2 = d1 - sigma*math.sqrt(T)
        N  = lambda x: 0.5*(1+math.erf(x/math.sqrt(2)))
        return (S*N(d1)-K*math.exp(-r*T)*N(d2)) if flag=='c' else (K*math.exp(-r*T)*N(-d2)-S*N(-d1))

def bs_delta(S, K, T, r, sigma, flag):
    if T <= 1e-7 or sigma <= 1e-6:
        return (1.0 if S>K else 0.0) if flag=='c' else (-1.0 if S<K else 0.0)
    try: return float(_pv_delta(flag, S, K, T, r, sigma))
    except Exception:
        d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
        N  = lambda x: 0.5*(1+math.erf(x/math.sqrt(2)))
        return N(d1) if flag=='c' else N(d1)-1.0

def strike_for_delta(target_d, S, T, r, iv, flag, n=40):
    """Find strike closest to target delta using discrete search."""
    best_k, best_diff = S, 999.0
    step = S * 0.001
    lo = S * 0.85 if flag == 'p' else S
    hi = S * 1.15 if flag == 'c' else S
    for i in range(n):
        k = lo + (hi - lo) * i / n
        d = abs(bs_delta(S, k, T, r, iv, flag))
        diff = abs(d - target_d)
        if diff < best_diff:
            best_diff, best_k = diff, k
    return round(best_k / 0.5) * 0.5

def iv_rank(vix_daily, ds, lb=252):
    dates = sorted(vix_daily.keys())
    try: idx = dates.index(ds)
    except ValueError: return 50.0
    window = [vix_daily[d] for d in dates[max(0,idx-lb):idx+1]]
    if len(window) < 2: return 50.0
    lo, hi = min(window), max(window)
    return (vix_daily.get(ds,lo)-lo)/(hi-lo)*100.0 if hi>lo else 50.0

def vwap(bars):
    tv = sum(b['v']*(b['h']+b['l']+b['c'])/3.0 for b in bars if b['v']>0)
    v  = sum(b['v'] for b in bars if b['v']>0)
    return tv/v if v>0 else (bars[-1]['c'] if bars else 0.0)

def load_ma50(spy_daily):
    dates  = sorted(spy_daily.keys())
    closes = [spy_daily[d]['close'] for d in dates]
    result = {}
    for i, ds in enumerate(dates):
        result[ds] = sum(closes[i-49:i+1])/50.0 if i >= 49 else None
    return result

def prev_trading_day(ds, spy_daily):
    d = date.fromisoformat(ds) - timedelta(days=1)
    for _ in range(10):
        if d.isoformat() in spy_daily: return d.isoformat()
        d -= timedelta(days=1)
    return None

def atr14(spy_daily, ds):
    days = sorted(spy_daily.keys())
    try: idx = days.index(ds)
    except ValueError: return 2.0
    window = days[max(0,idx-14):idx+1]
    trs = []
    for i in range(1, len(window)):
        d, p = window[i], window[i-1]
        h, l, pc = spy_daily[d]['high'], spy_daily[d]['low'], spy_daily[p]['close']
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs)/len(trs) if trs else 3.0

# ── FOMC / CPI ────────────────────────────────────────────────────────────────
FOMC_DATES = {
    date(2021,1,27),date(2021,3,17),date(2021,4,28),date(2021,6,16),
    date(2021,7,28),date(2021,9,22),date(2021,11,3),date(2021,12,15),
    date(2022,2,2), date(2022,3,16),date(2022,5,4), date(2022,6,15),
    date(2022,7,27),date(2022,9,21),date(2022,11,2),date(2022,12,14),
    date(2023,2,1), date(2023,3,22),date(2023,5,3), date(2023,6,14),
    date(2023,7,26),date(2023,9,20),date(2023,11,1),date(2023,12,13),
    date(2024,1,31),date(2024,3,20),date(2024,5,1), date(2024,6,12),
    date(2024,7,31),date(2024,9,18),date(2024,11,7),date(2024,12,18),
    date(2025,1,29),date(2025,3,19),date(2025,5,7), date(2025,6,18),
    date(2025,7,30),date(2025,9,17),date(2025,11,5),date(2025,12,10),
    date(2026,1,28),date(2026,3,18),date(2026,5,6),
}

HOT_CPI = {
    '2021-03','2021-04','2021-05','2021-06','2021-07','2021-08',
    '2021-09','2021-10','2021-11','2021-12',
    '2022-01','2022-02','2022-03','2022-04','2022-05','2022-06',
    '2022-07','2022-08','2022-09','2023-01','2023-02',
    '2024-03','2024-04','2025-03','2025-04',
}

CPI_DATES = {
    date(2021,1,13),date(2021,2,10),date(2021,3,10),date(2021,4,13),
    date(2021,5,12),date(2021,6,10),date(2021,7,13),date(2021,8,11),
    date(2021,9,14),date(2021,10,13),date(2021,11,10),date(2021,12,10),
    date(2022,1,12),date(2022,2,10),date(2022,3,10),date(2022,4,12),
    date(2022,5,11),date(2022,6,10),date(2022,7,13),date(2022,8,10),
    date(2022,9,13),date(2022,10,13),date(2022,11,10),date(2022,12,13),
    date(2023,1,12),date(2023,2,14),date(2023,3,14),date(2023,4,12),
    date(2023,5,10),date(2023,6,13),date(2023,7,12),date(2023,8,10),
    date(2023,9,13),date(2023,10,12),date(2023,11,14),date(2023,12,12),
    date(2024,1,11),date(2024,2,13),date(2024,3,12),date(2024,4,10),
    date(2024,5,15),date(2024,6,12),date(2024,7,11),date(2024,8,14),
    date(2024,9,11),date(2024,10,10),date(2024,11,13),date(2024,12,11),
    date(2025,1,15),date(2025,2,12),date(2025,3,12),date(2025,4,10),
    date(2025,5,13),date(2025,6,11),date(2025,7,11),date(2025,8,13),
    date(2025,9,10),date(2025,10,15),date(2025,11,12),date(2025,12,10),
    date(2026,1,14),date(2026,2,11),date(2026,3,11),date(2026,4,10),date(2026,5,13),
}

# ── Trade ─────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    strategy:    str
    date:        str
    entry_date:  str
    exit_date:   str
    entry_price: float
    exit_price:  float
    pnl:         float
    vix:         float
    note:        str = ''

def find_exp(obs, min_dte, max_dte, exps):
    for e in exps:
        dte = (e-obs).days
        if min_dte <= dte <= max_dte: return e
    return None

def option_price(S, K, mins_left, vix, flag, spread_pct=0.065):
    T = max(mins_left, 0.5) / TRADING_MINS
    r = 0.045
    iv = vix / 100.0
    mid = bs_price(S, K, T, r, iv, flag)
    mid = max(mid, 0.01)
    if flag == 'c':
        return mid*(1-spread_pct/2), mid*(1+spread_pct/2)  # bid, ask
    return mid*(1-spread_pct/2), mid*(1+spread_pct/2)

# ── Stats ─────────────────────────────────────────────────────────────────────
def calc_stats(trades):
    if not trades:
        return {'n':0,'wr':0,'total_pnl':0,'pf':0,'avg_win':0,'avg_loss':0,
                'max_dd':0,'sharpe':0,'sortino':0,'wins':0,'losses':0}
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gw, gl = sum(t.pnl for t in wins), sum(t.pnl for t in losses)
    wr     = len(wins)/len(trades)
    avg_w  = gw/len(wins)  if wins   else 0.0
    avg_l  = gl/len(losses) if losses else 0.0
    equity = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_date):
        equity += t.pnl; peak = max(peak,equity); max_dd = max(max_dd,peak-equity)
    by_day = defaultdict(float)
    for t in trades: by_day[t.entry_date] += t.pnl
    daily = list(by_day.values())
    if len(daily) > 1:
        mu, sig = np.mean(daily), np.std(daily,ddof=1)
        sharpe  = mu/sig*math.sqrt(252) if sig>0 else 0.0
        negs    = [p for p in daily if p<0]
        sortino = mu/np.std(negs)*math.sqrt(252) if len(negs)>1 and np.std(negs)>0 else 0.0
    else:
        sharpe = sortino = 0.0
    pf = abs(gw/gl) if gl<0 else float('inf')
    return {'n':len(trades),'wr':wr*100,'total_pnl':sum(t.pnl for t in trades),
            'pf':pf,'avg_win':avg_w,'avg_loss':avg_l,'max_dd':max_dd,
            'sharpe':sharpe,'sortino':sortino,'wins':len(wins),'losses':len(losses)}

def bootstrap_p(trades, n=1500):
    if not trades: return 0.0
    pnls = [t.pnl for t in trades]
    return sum(1 for _ in range(n) if sum(random.choices(pnls,k=len(pnls)))>0)/n*100

def year_conc(trades):
    if not trades: return 1.0
    total = sum(t.pnl for t in trades)
    if total <= 0: return 1.0
    by_yr = defaultdict(float)
    for t in trades: by_yr[t.entry_date[:4]] += t.pnl
    return max(by_yr.values())/total

def kill_check(oos_trades, breakeven_wr=50.0):
    s = calc_stats(oos_trades)
    if s['n'] < 20: return True, f"OOS N={s['n']} < 20"
    if s['sharpe'] < 1.0: return True, f"OOS Sharpe={s['sharpe']:.2f} < 1.0"
    if s['wr'] < breakeven_wr: return True, f"OOS WR={s['wr']:.1f}% < {breakeven_wr:.0f}%"
    c = year_conc(oos_trades)
    if c > 0.60:
        by_yr = defaultdict(float)
        for t in oos_trades: by_yr[t.entry_date[:4]] += t.pnl
        best = max(by_yr, key=by_yr.get)
        return True, f"Year concentration {c*100:.0f}% in {best}"
    return False, "PASSES all kill criteria"

def fmt_pf(s):
    p = s.get('pf',0)
    return 'inf' if p==float('inf') else (f'{p:.2f}' if p else 'N/A')


# ══════════════════════════════════════════════════════════════════════════════
# R1 — Monday Gap-Fill RELAXED
# Round 1 showed 80% WR with very tight filter (gap > 0.3%). Relaxing to 0.15%
# to get enough trades. Also extend exit window to 12:30 PM.
# ══════════════════════════════════════════════════════════════════════════════
def run_r1_monday_gapfill(spy_5min, spy_daily, vix_daily, exps, dates):
    trades = []
    d_list = sorted(spy_daily.keys())
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj.weekday() != 0: continue
        vix = vix_daily.get(ds, 16.0)
        if vix > 28: continue
        try: idx = d_list.index(ds)
        except ValueError: continue
        if idx < 1: continue
        prev_cls = spy_daily[d_list[idx-1]]['close']
        today_o  = spy_daily[ds].get('open', 0)
        if today_o <= 0: continue
        gap_pct = (today_o - prev_cls) / prev_cls * 100
        if gap_pct > -0.15: continue   # gap-down at least 0.15%
        if gap_pct < -3.0:  continue   # avoid crash opens (>3% gap)
        bars = spy_5min.get(ds, [])
        if len(bars) < 8: continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        # Entry: 9:45–10:00 AM
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if (dt_b.hour, dt_b.minute) >= (9,45) and dt_b.hour < 10:
                entry_bar = bar; break
        if entry_bar is None: continue
        spy_e = entry_bar['c']
        if spy_e >= prev_cls * 0.999: continue  # already filled
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        strike    = round(spy_e / 0.5) * 0.5
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        _, entry_ask = option_price(spy_e, strike, mins_left, vix, 'c')
        if entry_ask <= 0 or entry_ask * 100 > 1000: continue
        target_spy = prev_cls
        stop_ask   = entry_ask * 0.55
        cutoff     = (12, 30)
        exit_bar   = None; exit_rsn = 'time'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= cutoff:
                exit_bar = bar; exit_rsn = '12:30 stop'; break
            ml   = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            bid, _ = option_price(bar['c'], strike, ml, vix, 'c')
            if bar['c'] >= target_spy:
                exit_bar = bar; exit_rsn = 'gap filled'; break
            if bid <= stop_ask:
                exit_bar = bar; exit_rsn = '45% stop'; break
        if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'
        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid, _ = option_price(exit_bar['c'], strike, ml_exit, vix, 'c')
        pnl = round((exit_bid - entry_ask) * 100, 2)
        trades.append(Trade('R1_Monday_GapFill', ds, ds, ds, entry_ask, exit_bid, pnl, vix, exit_rsn))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R2 — Bull Put Credit Spread (FIXED strike formula)
# Mon/Wed/Fri entry, VIX 13-22, 10:30 AM. Using correct delta-based strike
# selection: target 0.16-delta (slightly more OTM than original 0.20).
# ══════════════════════════════════════════════════════════════════════════════
def run_r2_credit_spread_fixed(spy_5min, spy_daily, vix_daily, exps, dates):
    trades = []
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj.weekday() not in (0, 2, 4): continue
        vix = vix_daily.get(ds, 16.0)
        if not (13 <= vix <= 22): continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        bars = spy_5min.get(ds, [])
        if len(bars) < 8: continue
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and 30 <= dt_b.minute < 60:
                entry_bar = bar; break
        if entry_bar is None: continue
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS
        # FIXED: proper delta-based strike selection
        short_k = strike_for_delta(0.16, spy_e, T_e, r, iv, 'p', n=50)
        long_k  = short_k - 2.0
        sc_bid, _ = option_price(spy_e, short_k, mins_left, vix, 'p')
        _, lp_ask = option_price(spy_e, long_k,  mins_left, vix, 'p')
        credit = round(sc_bid - lp_ask, 4)
        min_cred = max(0.12, vix * 0.009)
        if credit < min_cred: continue
        target_exit = credit * 0.25
        stop_exit   = credit * 1.75
        exit_bar    = None; exit_rsn = 'EOD'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 0):
                exit_bar = bar; exit_rsn = '3PM force'; break
            ml = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            sp = bar['c']
            sc_a, _ = option_price(sp, short_k, ml, vix, 'p')
            _, lp_b = option_price(sp, long_k,  ml, vix, 'p')
            # Note: reversed for credit: we bought short_k back and sold long_k
            cur = max(sc_a - lp_b, 0)
            if cur <= target_exit:
                exit_bar = bar; exit_rsn = '75% target'; break
            if cur >= stop_exit:
                exit_bar = bar; exit_rsn = '1.75x stop'; break
        if exit_bar is None: exit_bar = bars[-1]
        dt_exit   = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit   = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        sp_x      = exit_bar['c']
        sc_ax, _  = option_price(sp_x, short_k, ml_exit, vix, 'p')
        _, lp_bx  = option_price(sp_x, long_k,  ml_exit, vix, 'p')
        exit_d    = max(sc_ax - lp_bx, 0)
        pnl = round((credit - exit_d) * 100, 2)
        trades.append(Trade('R2_CreditSpread_Fixed', ds, ds, ds, credit, exit_d, pnl, vix, exit_rsn))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R3 — Morning Gap Fade (opposite of H4)
# Round 1 showed gaps FAIL to continue (H4 WR 40.5%). 
# Hypothesis: Gap-ups >0.5% fade back intraday. Buy puts after gap-up open.
# ══════════════════════════════════════════════════════════════════════════════
def run_r3_gap_fade(spy_5min, spy_daily, vix_daily, exps, dates):
    trades = []
    d_list = sorted(spy_daily.keys())
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        vix = vix_daily.get(ds, 16.0)
        if vix > 25: continue
        try: idx = d_list.index(ds)
        except ValueError: continue
        if idx < 1: continue
        prev_cls = spy_daily[d_list[idx-1]]['close']
        today_o  = spy_daily[ds].get('open', 0)
        if today_o <= 0: continue
        gap_pct = (today_o - prev_cls) / prev_cls * 100
        if gap_pct < 0.5: continue
        if gap_pct > 2.5: continue
        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        # Entry: 9:50–10:05 AM, after initial pop settles
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if (dt_b.hour, dt_b.minute) >= (9,50) and (dt_b.hour, dt_b.minute) < (10, 5):
                # Only enter fade if price is STILL near open (not already fading)
                if bar['c'] >= today_o * 0.997:
                    entry_bar = bar; break
        if entry_bar is None: continue
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        strike    = round(spy_e / 0.5) * 0.5
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        _, entry_ask = option_price(spy_e, strike, mins_left, vix, 'p')
        if entry_ask <= 0 or entry_ask * 100 > 1000: continue
        target_spy = prev_cls        # fade target: fill the gap
        stop_ask   = entry_ask * 0.55
        cutoff     = (11, 30)
        exit_bar   = None; exit_rsn = 'time'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= cutoff:
                exit_bar = bar; exit_rsn = '11:30 cutoff'; break
            ml = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            bid, _ = option_price(bar['c'], strike, ml, vix, 'p')
            if bar['c'] <= target_spy:
                exit_bar = bar; exit_rsn = 'gap filled'; break
            if bid <= stop_ask:
                exit_bar = bar; exit_rsn = '45% stop'; break
        if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'
        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid, _ = option_price(exit_bar['c'], strike, ml_exit, vix, 'p')
        pnl = round((exit_bid - entry_ask) * 100, 2)
        trades.append(Trade('R3_Gap_Fade', ds, ds, ds, entry_ask, exit_bid, pnl, vix, exit_rsn))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R4 — Post-FOMC IV Collapse (sell credit spread day AFTER FOMC)
# Hypothesis: After FOMC, implied volatility drops sharply next morning.
# Sell same-day credit spreads on the morning after FOMC while IV premium
# is still elevated from the event.
# ══════════════════════════════════════════════════════════════════════════════
def run_r4_post_fomc_collapse(spy_5min, spy_daily, vix_daily, exps, dates):
    trades   = []
    d_list   = sorted(spy_daily.keys())
    date_set = set(dates)
    for fomc_dt in sorted(FOMC_DATES):
        # Find day after FOMC
        try:
            idx = next(i for i,d in enumerate(d_list) if date.fromisoformat(d) > fomc_dt)
        except StopIteration: continue
        ds = d_list[idx]
        if ds not in date_set: continue
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        # After FOMC, VIX often elevated — sell into that premium
        if vix < 13: continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and dt_b.minute < 30:
                entry_bar = bar; break
        if entry_bar is None: continue
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS
        short_k   = strike_for_delta(0.20, spy_e, T_e, r, iv, 'p', n=50)
        long_k    = short_k - 2.0
        sc_bid, _ = option_price(spy_e, short_k, mins_left, vix, 'p')
        _, lp_ask = option_price(spy_e, long_k,  mins_left, vix, 'p')
        credit    = round(sc_bid - lp_ask, 4)
        if credit < max(0.10, vix * 0.008): continue
        target_exit = credit * 0.30
        stop_exit   = credit * 1.75
        exit_bar    = None; exit_rsn = 'EOD'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 0):
                exit_bar = bar; exit_rsn = '3PM force'; break
            ml = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            sp = bar['c']
            sc_a, _ = option_price(sp, short_k, ml, vix, 'p')
            _, lp_b = option_price(sp, long_k,  ml, vix, 'p')
            cur = max(sc_a - lp_b, 0)
            if cur <= target_exit:
                exit_bar = bar; exit_rsn = '70% target'; break
            if cur >= stop_exit:
                exit_bar = bar; exit_rsn = '1.75x stop'; break
        if exit_bar is None: exit_bar = bars[-1]
        dt_exit   = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit   = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        sp_x      = exit_bar['c']
        sc_ax, _  = option_price(sp_x, short_k, ml_exit, vix, 'p')
        _, lp_bx  = option_price(sp_x, long_k,  ml_exit, vix, 'p')
        exit_d    = max(sc_ax - lp_bx, 0)
        pnl = round((credit - exit_d) * 100, 2)
        trades.append(Trade('R4_PostFOMC_Collapse', ds, ds, ds, credit, exit_d, pnl, vix, f'post-FOMC {fomc_dt}'))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R5 — Consecutive Red Days Reversal
# Hypothesis: After 3+ consecutive down days, day 4 has strong reversal bias.
# Market oversold on short timeframe → institutional buy programs trigger.
# ══════════════════════════════════════════════════════════════════════════════
def run_r5_consec_red_reversal(spy_5min, spy_daily, vix_daily, exps, dates):
    trades  = []
    d_list  = sorted(spy_daily.keys())
    for i, ds in enumerate(d_list):
        if ds not in set(dates): continue
        if i < 4: continue
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if vix > 35: continue  # avoid crash conditions
        # Check 3 prior days are all red
        prior = d_list[i-3:i]
        if len(prior) < 3: continue
        all_red = all(
            spy_daily[prior[j]]['close'] < spy_daily[prior[j]]['open']
            for j in range(3))
        if not all_red: continue
        # Check cumulative drop is significant (1.5–8%)
        cum_drop = (spy_daily[prior[0]]['open'] - spy_daily[prior[-1]]['close']) / spy_daily[prior[0]]['open'] * 100
        if not (1.5 <= cum_drop <= 8.0): continue
        # Today: must open UP (initial reversal signal)
        today_o = spy_daily[ds].get('open', 0)
        prev_cls = spy_daily[prior[-1]]['close']
        if today_o <= prev_cls: continue  # not reversing yet
        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and dt_b.minute < 15:
                entry_bar = bar; break
        if entry_bar is None: continue
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        strike    = round(spy_e / 0.5) * 0.5
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        _, entry_ask = option_price(spy_e, strike, mins_left, vix, 'c')
        if entry_ask <= 0 or entry_ask * 100 > 1500: continue
        target = entry_ask * 1.60  # 60% gain
        stop   = entry_ask * 0.55  # 45% stop
        cutoff = (14, 0)
        exit_bar = None; exit_rsn = 'time'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= cutoff:
                exit_bar = bar; exit_rsn = '2PM cutoff'; break
            ml  = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            bid, _ = option_price(bar['c'], strike, ml, vix, 'c')
            if bid >= target:
                exit_bar = bar; exit_rsn = '60% target'; break
            if bid <= stop:
                exit_bar = bar; exit_rsn = '45% stop'; break
        if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'
        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid, _ = option_price(exit_bar['c'], strike, ml_exit, vix, 'c')
        pnl = round((exit_bid - entry_ask) * 100, 2)
        trades.append(Trade('R5_Consec_Red_Reversal', ds, ds, ds, entry_ask, exit_bid, pnl, vix,
                            f'{3}+ red days, drop={cum_drop:.1f}%'))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R6 — ATR Breakout Momentum
# Hypothesis: When SPY breaks above the 14-day ATR range on above-average
# volume, it continues for the day. ATR breakouts are NOT noise — they signal
# genuine regime shifts.
# ══════════════════════════════════════════════════════════════════════════════
def run_r6_atr_breakout(spy_5min, spy_daily, vix_daily, exps, dates):
    trades = []
    d_list = sorted(spy_daily.keys())
    for i, ds in enumerate(d_list):
        if ds not in set(dates): continue
        if i < 15: continue
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if vix > 25: continue
        # 14-day ATR
        atr_val = atr14(spy_daily, ds)
        today_o = spy_daily[ds].get('open', 0)
        if today_o <= 0: continue
        # Prior 5-day range
        prior5 = d_list[i-5:i]
        if len(prior5) < 5: continue
        range_hi = max(spy_daily[d]['high']  for d in prior5)
        range_lo = min(spy_daily[d]['low']   for d in prior5)
        # Need today to already open above 5-day range (momentum gap)
        if today_o <= range_hi: continue
        # Gap must be at least 0.5 ATR
        gap = today_o - range_hi
        if gap < atr_val * 0.5: continue
        if gap > atr_val * 2.0: continue  # too extended
        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        # Entry: 10:00–10:30 AM, after OR confirmation
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and dt_b.minute < 30:
                # Must still be above breakout level
                if bar['c'] >= range_hi:
                    entry_bar = bar; break
        if entry_bar is None: continue
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        strike    = round(spy_e / 0.5) * 0.5
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        _, entry_ask = option_price(spy_e, strike, mins_left, vix, 'c')
        if entry_ask <= 0 or entry_ask * 100 > 1500: continue
        target = entry_ask * 1.50
        stop   = entry_ask * 0.60
        cutoff = (13, 0)
        exit_bar = None; exit_rsn = 'time'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= cutoff:
                exit_bar = bar; exit_rsn = '1PM cutoff'; break
            ml  = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            bid, _ = option_price(bar['c'], strike, ml, vix, 'c')
            if bid >= target:
                exit_bar = bar; exit_rsn = '50% target'; break
            if bid <= stop:
                exit_bar = bar; exit_rsn = '40% stop'; break
        if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'
        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid, _ = option_price(exit_bar['c'], strike, ml_exit, vix, 'c')
        pnl = round((exit_bid - entry_ask) * 100, 2)
        trades.append(Trade('R6_ATR_Breakout', ds, ds, ds, entry_ask, exit_bid, pnl, vix,
                            f'gap={gap:.1f} atr={atr_val:.1f}'))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R7 — Opening Range Breakout (30-min)
# Hypothesis: Price breaking out of the first 30-min range on above-average
# volume signals institutional commitment. High continuation probability.
# ══════════════════════════════════════════════════════════════════════════════
def run_r7_or_breakout(spy_5min, spy_daily, vix_daily, exps, dates):
    trades = []
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if vix > 25: continue
        bars = spy_5min.get(ds, [])
        if len(bars) < 10: continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        # 30-min OR: bars before 10:00 AM
        or_bars = [b for b in bars
                   if datetime.fromtimestamp(b['t']/1000,tz=ET).hour == 9
                   and datetime.fromtimestamp(b['t']/1000,tz=ET).minute >= 30]
        if len(or_bars) < 4: continue
        or_high = max(b['h'] for b in or_bars)
        or_low  = min(b['l'] for b in or_bars)
        or_rng  = or_high - or_low
        if or_rng < 0.50: continue  # need meaningful OR
        avg_vol = sum(b['v'] for b in or_bars) / len(or_bars)
        traded  = False
        for i, bar in enumerate(bars):
            if traded: break
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour < 10: continue
            if (dt_b.hour, dt_b.minute) >= (12, 0): break
            # Bullish OR breakout
            if bar['c'] > or_high and bar['v'] >= avg_vol * 1.5:
                direction = 'c'
                entry_bar = bars[min(i+1, len(bars)-1)]
            # Bearish OR breakdown
            elif bar['c'] < or_low and bar['v'] >= avg_vol * 1.5:
                direction = 'p'
                entry_bar = bars[min(i+1, len(bars)-1)]
            else:
                continue
            dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
            spy_e     = entry_bar['c']
            strike    = round(spy_e / 0.5) * 0.5
            mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
            _, entry_ask = option_price(spy_e, strike, mins_left, vix, direction)
            if entry_ask <= 0 or entry_ask * 100 > 1200: continue
            target = entry_ask * 1.40
            stop   = entry_ask * 0.60
            cutoff = (13, 30)
            exit_bar = None; exit_rsn = 'time'
            for bar2 in bars[i+2:]:
                dt_b2 = datetime.fromtimestamp(bar2['t']/1000, tz=ET)
                if (dt_b2.hour, dt_b2.minute) >= cutoff:
                    exit_bar = bar2; exit_rsn = '1:30 cutoff'; break
                ml   = max(16*60-(dt_b2.hour*60+dt_b2.minute), 1)
                bid, _ = option_price(bar2['c'], strike, ml, vix, direction)
                if bid >= target:
                    exit_bar = bar2; exit_rsn = '40% target'; break
                if bid <= stop:
                    exit_bar = bar2; exit_rsn = '40% stop'; break
            if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'
            dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
            ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
            exit_bid, _ = option_price(exit_bar['c'], strike, ml_exit, vix, direction)
            pnl = round((exit_bid - entry_ask) * 100, 2)
            trades.append(Trade('R7_OR_Breakout', ds, ds, ds, entry_ask, exit_bid, pnl, vix,
                                f'{"bull" if direction=="c" else "bear"} OR_rng={or_rng:.2f}'))
            traded = True
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R8 — Friday Credit Spread with VWAP Confirmation
# Previous Friday credit (H6) got 0 trades due to strike formula bug.
# Fix: use proper delta-based strike selection + VWAP bullish bias filter.
# ══════════════════════════════════════════════════════════════════════════════
def run_r8_friday_credit_vwap(spy_5min, spy_daily, vix_daily, exps, dates):
    trades = []
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj.weekday() != 4: continue
        vix = vix_daily.get(ds, 16.0)
        if not (12 <= vix <= 23): continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        bars = spy_5min.get(ds, [])
        if len(bars) < 15: continue
        # Find 1 PM bar for entry
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 13 and dt_b.minute < 15:
                entry_bar = bar; break
        if entry_bar is None: continue
        # VWAP filter: SPY must be above VWAP at entry (bullish session bias)
        bars_so_far = [b for b in bars
                       if b['t'] <= entry_bar['t']]
        vwap_now = vwap(bars_so_far) if bars_so_far else 0
        if entry_bar['c'] < vwap_now: continue  # bearish bias, skip put spread
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS
        short_k   = strike_for_delta(0.18, spy_e, T_e, r, iv, 'p', n=50)
        long_k    = short_k - 2.0
        sc_bid, _ = option_price(spy_e, short_k, mins_left, vix, 'p')
        _, lp_ask = option_price(spy_e, long_k,  mins_left, vix, 'p')
        credit    = round(sc_bid - lp_ask, 4)
        if credit < max(0.08, vix * 0.007): continue
        target_exit = credit * 0.30
        stop_exit   = credit * 1.80
        exit_bar    = None; exit_rsn = 'EOD'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 30):
                exit_bar = bar; exit_rsn = '3:30 force'; break
            ml = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            sp = bar['c']
            sc_a, _ = option_price(sp, short_k, ml, vix, 'p')
            _, lp_b = option_price(sp, long_k,  ml, vix, 'p')
            cur = max(sc_a - lp_b, 0)
            if cur <= target_exit:
                exit_bar = bar; exit_rsn = '70% target'; break
            if cur >= stop_exit:
                exit_bar = bar; exit_rsn = '1.8x stop'; break
        if exit_bar is None: exit_bar = bars[-1]
        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        sp_x     = exit_bar['c']
        sc_ax, _ = option_price(sp_x, short_k, ml_exit, vix, 'p')
        _, lp_bx = option_price(sp_x, long_k,  ml_exit, vix, 'p')
        exit_d   = max(sc_ax - lp_bx, 0)
        pnl = round((credit - exit_d) * 100, 2)
        trades.append(Trade('R8_Friday_Credit_VWAP', ds, ds, ds, credit, exit_d, pnl, vix, exit_rsn))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R9 — High-VIX Day Credit Spread (VIX 20–30, non-event)
# Pure IV premium harvest: on high-VIX non-FOMC/non-CPI days, 0DTE put spreads
# collect much more premium. Target 0.12-delta (very OTM) for safety.
# ══════════════════════════════════════════════════════════════════════════════
def run_r9_highvix_credit(spy_5min, spy_daily, vix_daily, exps, dates):
    trades   = []
    fomc_set = {d.isoformat() for d in FOMC_DATES}
    cpi_set  = {d.isoformat() for d in CPI_DATES}
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if not (20 <= vix <= 32): continue
        if ds in fomc_set or ds in cpi_set: continue  # avoid binary events
        ivr = iv_rank(vix_daily, ds)
        if ivr < 60: continue  # IVR must be elevated (real spike, not trend)
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and 15 <= dt_b.minute < 45:
                entry_bar = bar; break
        if entry_bar is None: continue
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS
        # Very OTM: 0.12-delta — wider protection from big moves on high-VIX days
        short_k   = strike_for_delta(0.12, spy_e, T_e, r, iv, 'p', n=60)
        long_k    = short_k - 3.0  # wider wing
        sc_bid, _ = option_price(spy_e, short_k, mins_left, vix, 'p')
        _, lp_ask = option_price(spy_e, long_k,  mins_left, vix, 'p')
        credit    = round(sc_bid - lp_ask, 4)
        min_cred  = max(0.15, vix * 0.010)
        if credit < min_cred: continue
        target_exit = credit * 0.40
        stop_exit   = credit * 2.00
        exit_bar    = None; exit_rsn = 'EOD'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 0):
                exit_bar = bar; exit_rsn = '3PM force'; break
            ml = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            sp = bar['c']
            sc_a, _ = option_price(sp, short_k, ml, vix, 'p')
            _, lp_b = option_price(sp, long_k,  ml, vix, 'p')
            cur = max(sc_a - lp_b, 0)
            if cur <= target_exit:
                exit_bar = bar; exit_rsn = '60% target'; break
            if cur >= stop_exit:
                exit_bar = bar; exit_rsn = '2x stop'; break
        if exit_bar is None: exit_bar = bars[-1]
        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        sp_x     = exit_bar['c']
        sc_ax, _ = option_price(sp_x, short_k, ml_exit, vix, 'p')
        _, lp_bx = option_price(sp_x, long_k,  ml_exit, vix, 'p')
        exit_d   = max(sc_ax - lp_bx, 0)
        pnl = round((credit - exit_d) * 100, 2)
        trades.append(Trade('R9_HighVIX_Credit', ds, ds, ds, credit, exit_d, pnl, vix,
                            f'vix={vix:.1f} ivr={ivr:.0f}'))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# R10 — Weekly Options Tuesday Decay
# Hypothesis: Tuesday 0DTE on weeks with M/W/F expirations has highest theta
# efficiency (midweek, no weekend risk, no end-of-week pinning).
# Use stricter VWAP + trend filter for entries.
# ══════════════════════════════════════════════════════════════════════════════
def run_r10_tuesday_decay(spy_5min, spy_daily, vix_daily, exps, dates):
    trades  = []
    ma50_d  = load_ma50(spy_daily)
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj.weekday() != 1: continue  # Tuesday only
        vix = vix_daily.get(ds, 16.0)
        if not (13 <= vix <= 21): continue
        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue
        # MA50 bullish regime filter
        spy_px = spy_daily[ds]['close']
        m50    = ma50_d.get(ds)
        if not m50 or spy_px < m50 * 0.98: continue  # need to be near/above MA50
        bars = spy_5min.get(ds, [])
        if len(bars) < 10: continue
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and 45 <= dt_b.minute < 60:
                entry_bar = bar; break
        if entry_bar is None: continue
        # VWAP filter
        bars_before = [b for b in bars if b['t'] <= entry_bar['t']]
        vwap_now    = vwap(bars_before) if bars_before else 0
        spy_e       = entry_bar['c']
        if spy_e < vwap_now: continue  # must be above VWAP
        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS
        short_k   = strike_for_delta(0.15, spy_e, T_e, r, iv, 'p', n=50)
        long_k    = short_k - 2.0
        sc_bid, _ = option_price(spy_e, short_k, mins_left, vix, 'p')
        _, lp_ask = option_price(spy_e, long_k,  mins_left, vix, 'p')
        credit    = round(sc_bid - lp_ask, 4)
        if credit < max(0.08, vix * 0.006): continue
        target_exit = credit * 0.25
        stop_exit   = credit * 1.75
        exit_bar    = None; exit_rsn = 'EOD'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 0):
                exit_bar = bar; exit_rsn = '3PM force'; break
            ml = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            sp = bar['c']
            sc_a, _ = option_price(sp, short_k, ml, vix, 'p')
            _, lp_b = option_price(sp, long_k,  ml, vix, 'p')
            cur = max(sc_a - lp_b, 0)
            if cur <= target_exit:
                exit_bar = bar; exit_rsn = '75% target'; break
            if cur >= stop_exit:
                exit_bar = bar; exit_rsn = '1.75x stop'; break
        if exit_bar is None: exit_bar = bars[-1]
        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        sp_x     = exit_bar['c']
        sc_ax, _ = option_price(sp_x, short_k, ml_exit, vix, 'p')
        _, lp_bx = option_price(sp_x, long_k,  ml_exit, vix, 'p')
        exit_d   = max(sc_ax - lp_bx, 0)
        pnl = round((credit - exit_d) * 100, 2)
        trades.append(Trade('R10_Tuesday_Decay', ds, ds, ds, credit, exit_d, pnl, vix, exit_rsn))
    return trades


# ── Hypotheses registry ───────────────────────────────────────────────────────
HYPOTHESES = [
    {'id':'R1',  'name':'Monday Gap-Fill (Relaxed)',     'runner':run_r1_monday_gapfill,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':42.0, 'type':'debit',
     'desc':'Buy 0DTE calls on Mon gap-down > 0.15%, exit 12:30 PM. Signal: 80% WR in R1 (too few trades).'},
    {'id':'R2',  'name':'Credit Spread (Fixed Delta)',   'runner':run_r2_credit_spread_fixed,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':55.0, 'type':'credit',
     'desc':'Mon/Wed/Fri 10:30 AM put spread, FIXED strike formula with proper delta search.'},
    {'id':'R3',  'name':'Morning Gap Fade',              'runner':run_r3_gap_fade,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':42.0, 'type':'debit',
     'desc':'Fade gap-ups > 0.5% by buying puts. Learned from R1 H4: gaps fail to continue.'},
    {'id':'R4',  'name':'Post-FOMC IV Collapse',         'runner':run_r4_post_fomc_collapse,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':55.0, 'type':'credit',
     'desc':'Sell 0DTE put spread day after FOMC while IV premium still elevated.'},
    {'id':'R5',  'name':'3+ Red Days Reversal',          'runner':run_r5_consec_red_reversal,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':42.0, 'type':'debit',
     'desc':'Buy 0DTE calls after 3 consecutive red days + gap-up open.'},
    {'id':'R6',  'name':'ATR Breakout Momentum',         'runner':run_r6_atr_breakout,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':42.0, 'type':'debit',
     'desc':'Buy calls when SPY gaps above 5-day range by 0.5–2x ATR.'},
    {'id':'R7',  'name':'Opening Range Breakout',        'runner':run_r7_or_breakout,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':42.0, 'type':'debit',
     'desc':'Buy 0DTE directional option on 30-min OR breakout with volume surge.'},
    {'id':'R8',  'name':'Friday Credit + VWAP Filter',   'runner':run_r8_friday_credit_vwap,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':55.0, 'type':'credit',
     'desc':'Friday 1 PM put spread with VWAP bullish confirmation (fixed from H6).'},
    {'id':'R9',  'name':'High-VIX Credit Spread',        'runner':run_r9_highvix_credit,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':55.0, 'type':'credit',
     'desc':'0DTE very-OTM put spread (0.12 delta) on VIX 20-32 non-event days.'},
    {'id':'R10', 'name':'Tuesday Decay + VWAP',          'runner':run_r10_tuesday_decay,
     'args':['spy_5min','spy_daily','vix_daily','exps','dates'], 'bwr':55.0, 'type':'credit',
     'desc':'Tuesday 0DTE put spread, 10:45 AM, above VWAP + MA50 regime filter.'},
]

# ── Journal / reporting ───────────────────────────────────────────────────────
def fmt_pf(s):
    p = s.get('pf',0)
    return 'inf' if p==float('inf') else (f'{p:.2f}' if p else 'N/A')

def write_result(h, all_t, is_t, oos_t, dead, kill_r, elapsed):
    s_all = calc_stats(all_t); s_is = calc_stats(is_t); s_oos = calc_stats(oos_t)
    pp    = bootstrap_p(oos_t)
    by_yr = defaultdict(list)
    for t in oos_t: by_yr[t.entry_date[:4]].append(t)
    er    = defaultdict(lambda:{'n':0,'pnl':0.0,'w':0})
    for t in oos_t:
        er[t.note]['n']+=1; er[t.note]['pnl']+=t.pnl
        if t.pnl>0: er[t.note]['w']+=1
    lines = [
        f"## {h['id']} — {h['name']}",
        f"**Hypothesis:** {h['desc']}",
        f"**Type:** {h['type']} | **Elapsed:** {elapsed:.1f}s",
        f"**Status:** {'❌ DEAD — '+kill_r if dead else '✅ ALIVE'}",
        "",
        f"### Full  N={s_all['n']} WR={s_all['wr']:.1f}% P&L=${s_all['total_pnl']:+,.2f} PF={fmt_pf(s_all)} Sharpe={s_all['sharpe']:.2f}",
        f"### IS    N={s_is['n']}  WR={s_is['wr']:.1f}%  P&L=${s_is['total_pnl']:+,.2f}  PF={fmt_pf(s_is)}",
        f"### OOS   N={s_oos['n']} WR={s_oos['wr']:.1f}% P&L=${s_oos['total_pnl']:+,.2f} PF={fmt_pf(s_oos)} Sharpe={s_oos['sharpe']:.2f} Boot={pp:.0f}%",
        "",
        "**OOS Year Breakdown:**",
    ]
    for yr, ts in sorted(by_yr.items()):
        ys = calc_stats(ts)
        lines.append(f"- {yr}: N={ys['n']} WR={ys['wr']:.1f}% P&L=${ys['total_pnl']:+,.2f} PF={fmt_pf(ys)}")
    lines.append("\n**Exit reasons (OOS):**")
    for rsn, v in sorted(er.items(), key=lambda x:-x[1]['n']):
        wr = v['w']/v['n']*100 if v['n'] else 0
        lines.append(f"- `{rsn[:40]}`: N={v['n']} WR={wr:.0f}% P&L=${v['pnl']:+.2f}")
    lines += ["\n---\n"]
    txt = '\n'.join(lines)
    (RESULTS_DIR/f"{h['id']}.md").write_text(txt)
    return txt

def append_journal(text):
    with JOURNAL.open('a') as f: f.write(text+'\n')

def tg_update(batch, batch_n):
    alive = [r for r in batch if not r['dead']]
    dead  = [r for r in batch if r['dead']]
    msg   = f"📊 *Round 2 — Batch {batch_n}*\n{len(alive)} alive | {len(dead)} dead\n\n"
    if alive:
        msg += "*SURVIVORS:*\n"
        for r in alive:
            s = r['s_oos']
            msg += f"✅ *{r['id']} {r['name']}*\n   OOS: N={s['n']} WR={s['wr']:.1f}% P&L=${s['total_pnl']:+,.0f} Sharpe={s['sharpe']:.2f}\n"
    if dead:
        msg += "\n*Dead:*\n"
        for r in dead[:5]:
            msg += f"❌ {r['id']} {r['name']}: {r['kill_r']}\n"
    tg(msg)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*70}")
    print("  HERMES RESEARCH ENGINE — ROUND 2")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("  Applying lessons from Round 1 (10/10 dead)")
    print(f"{'='*70}\n")

    print("  Loading market data...")
    vix_daily = load_vix()
    spy_daily = load_spy_daily()
    spy_5min  = load_spy_5min()
    exps      = get_theta_exps()
    all_dates = sorted(spy_daily.keys())
    print(f"  {len(all_dates)} days | {len(exps)} expirations | VIX {min(vix_daily.values()):.1f}–{max(vix_daily.values()):.1f}")

    is_dates  = [d for d in all_dates if d < SPLIT_DATE.isoformat()]
    oos_dates = [d for d in all_dates if SPLIT_DATE.isoformat() <= d and not d.startswith(str(BLIND_YEAR))]
    print(f"  IS: {len(is_dates)} | OOS: {len(oos_dates)} | Blind: {sum(1 for d in all_dates if d.startswith(str(BLIND_YEAR)))}\n")

    ctx = {'spy_5min':spy_5min,'spy_daily':spy_daily,'vix_daily':vix_daily,'exps':exps,'dates':all_dates}

    JOURNAL.write_text(f"# Hermes Research Journal — Round 2\n*Started: {datetime.now()}*\n\nLessons from Round 1: gap momentum fails, gap fill works (80% WR, too few), credit strike formula broken (fixed), debit strategies expensive with BS pricing.\n\n---\n\n")
    tg("🔄 *Hermes Research Round 2 started*\nApplying Round 1 lessons: fixed strike formula, gap fade vs gap momentum, post-FOMC IV collapse, consecutive red reversal.")

    batch       = []
    all_alive   = []

    for i, h in enumerate(HYPOTHESES):
        print(f"\n  [{i+1}/{len(HYPOTHESES)}] {h['id']}: {h['name']}")
        print(f"  {h['desc']}")
        t0 = time.time()
        kwargs = {k: ctx[k] for k in h['args']}
        try:
            all_t = h['runner'](**kwargs)
        except Exception as e:
            import traceback; traceback.print_exc()
            all_t = []
        elapsed = time.time()-t0
        is_t    = [t for t in all_t if t.entry_date < SPLIT_DATE.isoformat()]
        oos_t   = [t for t in all_t if SPLIT_DATE.isoformat() <= t.entry_date and not t.entry_date.startswith(str(BLIND_YEAR))]
        bld_t   = [t for t in all_t if t.entry_date.startswith(str(BLIND_YEAR))]
        s_all   = calc_stats(all_t)
        s_oos   = calc_stats(oos_t)
        dead, kill_r = kill_check(oos_t, h['bwr'])
        print(f"  Full: N={s_all['n']:>4} WR={s_all['wr']:>5.1f}% P&L=${s_all['total_pnl']:>+10,.2f} Sharpe={s_all['sharpe']:>5.2f}")
        print(f"  OOS:  N={s_oos['n']:>4} WR={s_oos['wr']:>5.1f}% P&L=${s_oos['total_pnl']:>+10,.2f} Sharpe={s_oos['sharpe']:>5.2f}")
        print(f"  {'❌ DEAD: '+kill_r if dead else '✅ ALIVE'}")
        txt = write_result(h, all_t, is_t, oos_t, dead, kill_r, elapsed)
        append_journal(txt)
        r = {'id':h['id'],'name':h['name'],'dead':dead,'kill_r':kill_r,'s_oos':s_oos}
        batch.append(r)
        if not dead: all_alive.append({**r,'trades':all_t})
        if (i+1) % 5 == 0 or (i+1) == len(HYPOTHESES):
            tg_update(batch[-(5 if (i+1)%5==0 else (i+1)%5 or 5):], (i+1)//5 or 1)

    print(f"\n{'='*70}")
    print(f"  ROUND 2 COMPLETE: {len(all_alive)}/{len(HYPOTHESES)} survived")
    print(f"{'='*70}")

    summary = [f"\n---\n## Round 2 Summary\n*{datetime.now()}*\n",
               f"{len(all_alive)}/{len(HYPOTHESES)} survived\n",
               "\n### Survivors\n"]
    for r in sorted(all_alive, key=lambda x: x['s_oos']['total_pnl'], reverse=True):
        s = r['s_oos']
        summary.append(f"- **{r['id']} {r['name']}**: OOS N={s['n']} WR={s['wr']:.1f}% P&L=${s['total_pnl']:+,.0f} Sharpe={s['sharpe']:.2f}")
    summary += ["\n### Dead\n"]
    for r in batch:
        if r['dead']: summary.append(f"- ❌ {r['id']} {r['name']}: {r['kill_r']}")
    append_journal('\n'.join(summary))

    msg = f"🏁 *Round 2 Complete*\n{len(all_alive)}/{len(HYPOTHESES)} survived\n\n"
    if all_alive:
        msg += "*Survivors:*\n"
        for r in sorted(all_alive, key=lambda x: x['s_oos']['total_pnl'], reverse=True):
            s = r['s_oos']
            msg += f"✅ {r['id']} {r['name']}: N={s['n']} WR={s['wr']:.1f}% P&L=${s['total_pnl']:+,.0f} Sharpe={s['sharpe']:.2f}\n"
    else:
        msg += "No survivors — launching Round 3 with deeper hypothesis revision."
    tg(msg)
    print(f"\n  Journal: {JOURNAL}\n")

if __name__ == '__main__':
    main()
