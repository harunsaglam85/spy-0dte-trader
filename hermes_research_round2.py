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
if not BASE.exists():
    # Local Windows dev box: fall back to this script's own directory.
    BASE = Path(__file__).resolve().parent
DATA_DIR    = BASE / 'backtest_data'
if not DATA_DIR.exists():
    # Locally the shared data cache lives one level up, at C:\Users\sagla\backtest_data.
    DATA_DIR = BASE.parent / 'backtest_data'
RESEARCH    = BASE / 'hermes_research'
JOURNAL     = RESEARCH / 'journal_r2.md'
RESULTS_DIR = RESEARCH / 'results_r2'
RESEARCH.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)
# All pickle files loaded below (backtest_data/*.pkl) are self-written local
# caches from this project's own data-fetch scripts, not from an untrusted
# source, so the arbitrary-code-execution risk of pickle.load does not apply.

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

def load_spy_1min():
    with (DATA_DIR / 'spy_1min_alpaca_2021-01-04_2026-05-30.pkl').open('rb') as f:
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
# R7-POC-RR3 — Opening Range + Point-of-Control breakout, fixed 3:1 R:R
# Extends R7: 15-min OR (not 30-min), stop pinned to the OR's volume POC ± 2
# ticks instead of a flat option-premium multiple, target fixed at 3x that
# underlying-price risk. Entry/exit option pricing uses IV calibrated from
# real ThetaData EOD quotes (theta_SPY_*.pkl) where cached, not the vix/100
# proxy the rest of this file uses — see calibrate_iv_theta().
#
# IMPORTANT DATA CAVEAT: theta_SPY_*.pkl only has ONE EOD-style quote per
# (obs_date, strike, right) — there is no intraday options tick data cached
# anywhere in this project. So "real theta pricing" here means: real EOD
# quotes are used to calibrate that day's IV (same technique as
# backtest_real_data.py's OptionPricer._day_iv), which then feeds a
# Black-Scholes intraday walk. It is NOT literal real intraday bid/ask at
# the entry/exit bar — that data doesn't exist in this cache. Days lacking
# a usable same-day 0DTE theta file are skipped outright (see
# theta_0dte_usable) rather than silently falling back to synthetic pricing,
# so the "usable vs skipped" coverage counts below are meaningful.
# ══════════════════════════════════════════════════════════════════════════════

OR_POC_MIN         = 15     # opening range length in minutes (9:30-9:44)
OR_POC_TICK        = 0.01   # SPY underlying tick size
OR_POC_STOP_TICKS  = 2      # stop = POC +/- this many ticks
OR_POC_RR          = 3.0    # fixed target:risk ratio on the underlying move

_THETA_CACHE: Dict[str, Optional[dict]] = {}

def _load_theta_file(exp_ds: str) -> Optional[dict]:
    """Cache-only loader for backtest_data/theta_SPY_{exp_ds}.pkl.
    Returns None if the file is missing, the known 5-byte empty placeholder,
    or fails to unpickle. Memoized since one expiration file gets reused as
    the IV-calibration source for many different observation days."""
    if exp_ds in _THETA_CACHE:
        return _THETA_CACHE[exp_ds]
    p = DATA_DIR / f'theta_SPY_{exp_ds}.pkl'
    result = None
    if p.exists() and p.stat().st_size > 5:
        try:
            with p.open('rb') as f:
                d = pickle.load(f)
            if d:
                result = d
        except Exception:
            result = None
    _THETA_CACHE[exp_ds] = result
    return result

def theta_0dte_usable(ds: str) -> bool:
    """True if theta_SPY_{ds}.pkl (expiration == ds, i.e. the 0DTE chain for
    that trading day) contains at least one real same-day quote with ask>0."""
    chain = _load_theta_file(ds)
    if not chain:
        return False
    return any(k[0] == ds and v.get('ask', 0) > 0 for k, v in chain.items())

def calibrate_iv_theta(ds: str, spy_close: float, r: float) -> Tuple[Optional[float], str]:
    """Median IV backed out (via py_vollib) from real theta EOD quotes of
    3-15 DTE expirations observed on `ds`, near-ATM strikes only. Returns
    (iv, 'theta_calibrated') if any such cached chain exists, else
    (None, 'vix_fallback') so the caller can fall back to vix/100."""
    obs = date.fromisoformat(ds)
    ivs = []
    for dte in range(3, 16):
        exp = obs + timedelta(days=dte)
        if exp.weekday() >= 5:
            continue
        chain = _load_theta_file(exp.isoformat())
        if not chain:
            continue
        T = dte / 365.0
        for (kd, strike, right), q in chain.items():
            if kd != ds:
                continue
            if spy_close <= 0 or abs(strike - spy_close) > spy_close * 0.015:
                continue
            mid = (q.get('bid', 0) + q.get('ask', 0)) / 2.0
            if mid <= 0.01:
                continue
            flag = 'c' if right == 'C' else 'p'
            try:
                iv = _pv_iv(mid, spy_close, strike, T, r, flag)
                if iv and 0.02 < iv < 5.0:
                    ivs.append(iv)
            except Exception:
                pass
    if ivs:
        return float(np.median(ivs)), 'theta_calibrated'
    return None, 'vix_fallback'

def calc_poc(bars_1min: list, tick: float = OR_POC_TICK) -> Optional[float]:
    """Point of control from a set of 1-min OHLCV bars: each bar's volume is
    spread evenly across its [low, high] range in `tick` increments (the
    standard approximation for building a volume profile from bars instead
    of raw tick prints), then POC = the price level with the most
    accumulated volume."""
    vol_by_price: Dict[int, float] = defaultdict(float)
    for b in bars_1min:
        lo, hi, v = b['l'], b['h'], b['v']
        if v <= 0 or hi < lo:
            continue
        lo_t, hi_t = round(lo / tick), round(hi / tick)
        n = hi_t - lo_t + 1
        share = v / n
        for t in range(lo_t, hi_t + 1):
            vol_by_price[t] += share
    if not vol_by_price:
        return None
    best_t = max(vol_by_price.items(), key=lambda kv: kv[1])[0]
    return round(best_t * tick, 2)

def _et(b) -> datetime:
    return datetime.fromtimestamp(b['t'] / 1000, tz=ET)

def _or_poc_price(S, K, mins_left, iv, r, flag, spread_pct=0.07):
    T   = max(mins_left, 0.5) / TRADING_MINS
    mid = max(bs_price(S, K, T, r, iv, flag), 0.01)
    return mid * (1 - spread_pct / 2), mid * (1 + spread_pct / 2)   # bid, ask

@dataclass
class ORPocTrade:
    date:          str
    entry_date:    str   # alias of `date`, kept so calc_stats() works unmodified
    entry_time:    str
    exit_time:     str
    direction:     str
    strike:        float
    or_high:       float
    or_low:        float
    poc:           float
    entry_spy:     float
    stop_spy:      float
    target_spy:    float
    exit_spy:      float
    entry_option:  float
    exit_option:   float
    exit_reason:   str
    pnl:           float
    iv:            float
    iv_source:     str

def run_r7_or_poc_rr3(spy_1min: dict, spy_5min: dict, spy_daily: dict, vix_daily: dict,
                       dates: List[str]) -> Tuple[List[ORPocTrade], dict]:
    trades   = []
    coverage = {'total_days': 0, 'theta_usable': 0, 'theta_skipped': 0,
                'iv_theta_calibrated': 0, 'iv_vix_fallback': 0, 'signal_days': 0}

    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < OR_POC_MIN + 5 or len(bars_5min) < 4:
            continue
        dt_obj = date.fromisoformat(ds)
        coverage['total_days'] += 1

        if not theta_0dte_usable(ds):
            coverage['theta_skipped'] += 1
            continue
        coverage['theta_usable'] += 1

        sig = find_or_poc_signal(ds, bars_1min, bars_5min)
        if sig is None:
            continue
        coverage['signal_days'] += 1

        bars      = sig['bars']    # 1-min bars, used below for the exit walk
        direction = sig['direction']
        entry_idx = sig['entry_idx']
        entry_dt  = sig['entry_dt']
        entry_spy = sig['entry_spy']
        strike    = sig['strike']
        poc       = sig['poc']
        or_high   = sig['or_high']
        or_low    = sig['or_low']

        # Use the raw intraday close, NOT spy_daily's cached close: spy_daily was
        # fetched from yfinance with auto_adjust=True (dividend/split-adjusted),
        # which diverges from Alpaca's raw 1-min prices by up to ~7% on older
        # dates. Mixing the two would silently corrupt the near-ATM strike
        # filter in calibrate_iv_theta() below.
        spy_close = sig['or_close']
        r   = rfr(dt_obj)
        vix = vix_daily.get(ds, 16.0)
        iv, iv_source = calibrate_iv_theta(ds, spy_close, r)
        if iv is None:
            iv = vix / 100.0
        coverage['iv_theta_calibrated' if iv_source == 'theta_calibrated' else 'iv_vix_fallback'] += 1

        if direction == 'c':
            stop_spy = poc - OR_POC_STOP_TICKS * OR_POC_TICK
            if stop_spy >= entry_spy:
                continue
            risk       = entry_spy - stop_spy
            target_spy = entry_spy + OR_POC_RR * risk
        else:
            stop_spy = poc + OR_POC_STOP_TICKS * OR_POC_TICK
            if stop_spy <= entry_spy:
                continue
            risk       = stop_spy - entry_spy
            target_spy = entry_spy - OR_POC_RR * risk
        if risk < 0.02:
            continue

        mins_left_entry = max(16 * 60 - (entry_dt.hour * 60 + entry_dt.minute), 1)
        entry_bid, entry_ask = _or_poc_price(entry_spy, strike, mins_left_entry, iv, r, direction)
        if entry_ask <= 0 or entry_ask * 100 > 1500:
            continue

        exit_bar, exit_reason, exit_spy = None, 'EOD', None
        cutoff = (13, 30)
        for b2 in bars[entry_idx + 1:]:
            t2 = _et(b2)
            if (t2.hour, t2.minute) >= cutoff:
                exit_bar, exit_reason, exit_spy = b2, '1:30 cutoff', b2['c']
                break
            if direction == 'c':
                if b2['l'] <= stop_spy:
                    exit_bar, exit_reason, exit_spy = b2, 'stop (POC-2t)', stop_spy
                    break
                if b2['h'] >= target_spy:
                    exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                    break
            else:
                if b2['h'] >= stop_spy:
                    exit_bar, exit_reason, exit_spy = b2, 'stop (POC+2t)', stop_spy
                    break
                if b2['l'] <= target_spy:
                    exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                    break
        if exit_bar is None:
            exit_bar, exit_reason, exit_spy = bars[-1], 'EOD', bars[-1]['c']

        exit_dt        = _et(exit_bar)
        mins_left_exit = max(16 * 60 - (exit_dt.hour * 60 + exit_dt.minute), 1)
        exit_bid, _ = _or_poc_price(exit_spy, strike, mins_left_exit, iv, r, direction)
        pnl = round((exit_bid - entry_ask) * 100, 2)

        trades.append(ORPocTrade(
            date=ds, entry_date=ds, entry_time=entry_dt.strftime('%H:%M'),
            exit_time=exit_dt.strftime('%H:%M'),
            direction=('CALL' if direction == 'c' else 'PUT'), strike=strike,
            or_high=round(or_high, 2), or_low=round(or_low, 2), poc=poc,
            entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2),
            target_spy=round(target_spy, 2), exit_spy=round(exit_spy, 2),
            entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
            exit_reason=exit_reason, pnl=pnl, iv=round(iv, 4), iv_source=iv_source))

    return trades, coverage

# ══════════════════════════════════════════════════════════════════════════════
# R7-POC-RR3 with REAL intraday ThetaData quotes (replaces the BS/IV-calibrated
# fill above with actual fetched bid/ask) + a stop-distance sweep.
#
# Signal detection (OR/POC/breakout/strike/entry) is IDENTICAL to
# run_r7_or_poc_rr3 above and reuses the same 552-day theta_0dte_usable gate.
# Only the option FILL PRICE changes: instead of Black-Scholes with a
# theta-calibrated IV, this fetches real 1-min bid/ask from ThetaData's
# option_history_quote for the exact contract chosen at entry, covering
# entry_time -> 13:30 cutoff (the widest possible exit window), fetched ONCE
# per signal day and cached to disk. Because entry/strike/direction don't
# depend on stop distance, the sweep reuses one fetched quote series per day
# across all stop-distance variants instead of re-fetching per variant.
# ══════════════════════════════════════════════════════════════════════════════

INTRADAY_CACHE_DIR = DATA_DIR / 'theta_intraday'
INTRADAY_CACHE_DIR.mkdir(exist_ok=True)
_THETA_ENV_PATH = Path(r'C:\Users\sagla\.tastytrade-mcp\.env')

_THETA_CLIENT     = None
THETA_API_CALLS   = 0
THETA_API_SECONDS = 0.0

def _load_theta_env() -> dict:
    env = {}
    if _THETA_ENV_PATH.exists():
        for line in _THETA_ENV_PATH.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env

def get_theta_client():
    global _THETA_CLIENT
    if _THETA_CLIENT is None:
        from thetadata import ThetaClient
        env = _load_theta_env()
        _THETA_CLIENT = ThetaClient(email=env.get('THETADATA_USERNAME'),
                                     password=env.get('THETADATA_PASSWORD'),
                                     dataframe_type='pandas')
    return _THETA_CLIENT

def fetch_intraday_chain(ds: str, strike: float, right: str,
                          start_hms: str, end_hms: str) -> Optional[List[dict]]:
    """Cache-first 1-min bid/ask fetch for one (day, strike, right) contract
    over [start_hms, end_hms]. Returns a list of {'hm': 'HH:MM', 'bid', 'ask'}
    or None if the API had no data / errored (also cached, so a bad day isn't
    re-hit on re-run). Tracks call count + wall time in the module-level
    THETA_API_CALLS / THETA_API_SECONDS counters."""
    global THETA_API_CALLS, THETA_API_SECONDS
    tag  = f"{ds}_{strike:g}_{right}_{start_hms.replace(':','')}_{end_hms.replace(':','')}"
    path = INTRADAY_CACHE_DIR / f'{tag}.pkl'
    if path.exists():
        with path.open('rb') as f:
            return pickle.load(f)

    client = get_theta_client()
    t0 = time.time()
    try:
        df = client.option_history_quote(
            symbol='SPY', expiration=date.fromisoformat(ds), interval='1m',
            date=date.fromisoformat(ds), strike=f'{strike:g}', right=right,
            start_time=start_hms, end_time=end_hms)
    except Exception:
        df = None
    THETA_API_CALLS   += 1
    THETA_API_SECONDS += time.time() - t0

    if df is None or len(df) == 0:
        with path.open('wb') as f:
            pickle.dump(None, f)
        return None

    records = []
    for _, row in df.iterrows():
        ts = row['timestamp']
        if ts is None:
            continue
        bid = float(row['bid']) if row['bid'] == row['bid'] else None   # NaN check
        ask = float(row['ask']) if row['ask'] == row['ask'] else None
        records.append({'hm': ts.strftime('%H:%M'), 'bid': bid, 'ask': ask})
    with path.open('wb') as f:
        pickle.dump(records, f)
    return records

def _nearest_prior_quote(chain: Dict[str, dict], hm: str, field: str, max_back: int = 5):
    """Walk backward up to `max_back` minutes from `hm` for the first non-null
    `field` (handles the rare illiquid minute with a NaN quote)."""
    h, m = int(hm[:2]), int(hm[3:])
    for _ in range(max_back + 1):
        key = f'{h:02d}:{m:02d}'
        q = chain.get(key)
        if q and q.get(field) is not None:
            return q[field]
        m -= 1
        if m < 0:
            h, m = h - 1, 59
    return None

def find_or_poc_signal(ds: str, bars_1min: list, bars_5min: list) -> Optional[dict]:
    """Signal-only half of run_r7_or_poc_rr3: OR/POC from the 15-min opening
    range (1-min bars, unchanged) + breakout ENTRY DETECTION on 5-MIN candles
    with NO volume filter - matches the original spec: any 5-min candle that
    closes outside OR high/low triggers, no volume-surge condition. Entry
    fills at the close of the NEXT 5-min candle after the trigger.

    The stop/target exit walk still runs on 1-min bars for finer resolution
    (unchanged from before - only the entry trigger's bar size changed), so
    entry_idx below is the first 1-min bar at or after the entry candle's
    CLOSE time (trigger_start + 5min), not its start - anchoring to the
    start would let the exit walk see 1-min bars from inside the still-
    forming entry candle, before that candle's close price was realized.

    Shared by both the BS-simulated and real-theta-quote execution paths so
    the signal logic stays identical between them.
    """
    or_bars = [b for b in bars_1min if (9, 30) <= (_et(b).hour, _et(b).minute) < (9, 30 + OR_POC_MIN)]
    if len(or_bars) < OR_POC_MIN * 2 // 3:
        return None
    or_high = max(b['h'] for b in or_bars)
    or_low  = min(b['l'] for b in or_bars)
    if or_high - or_low < 0.20:
        return None
    poc = calc_poc(or_bars)
    if poc is None:
        return None

    direction, trigger_i = None, None
    for i, b in enumerate(bars_5min):
        t = _et(b)
        if (t.hour, t.minute) < (9, 45):
            continue
        if (t.hour, t.minute) >= (12, 0):
            break
        if b['c'] > or_high:
            direction, trigger_i = 'c', i
            break
        if b['c'] < or_low:
            direction, trigger_i = 'p', i
            break
    if direction is None or trigger_i is None or trigger_i + 1 >= len(bars_5min):
        return None

    entry_bar_5m   = bars_5min[trigger_i + 1]
    entry_spy      = entry_bar_5m['c']
    entry_close_ts = _et(entry_bar_5m) + timedelta(minutes=5)
    strike         = round(entry_spy / 0.5) * 0.5

    entry_idx = next((i for i, b in enumerate(bars_1min) if _et(b) >= entry_close_ts), None)
    if entry_idx is None:
        return None
    entry_dt = _et(bars_1min[entry_idx])

    return {'ds': ds, 'bars': bars_1min, 'direction': direction, 'entry_idx': entry_idx,
            'entry_dt': entry_dt, 'entry_spy': entry_spy, 'strike': strike,
            'poc': poc, 'or_high': round(or_high, 2), 'or_low': round(or_low, 2),
            'or_close': or_bars[-1]['c']}

def snap_to_real_strike(ds: str, raw_strike: float, right: str) -> Optional[float]:
    """Nearest strike that actually exists in that day's real 0DTE theta EOD
    chain. Real intraday quotes only exist for strikes the exchange actually
    listed that day (SPY didn't list $0.50-wide 0DTE strikes broadly until
    later - many `round(spy/0.5)*0.5` strikes from find_or_poc_signal are not
    real contracts), unlike the Black-Scholes path, which can price any
    hypothetical strike. Only used by the real-quote pipeline; the BS path's
    signal/strike selection above is left untouched."""
    chain = _load_theta_file(ds)
    if not chain:
        return None
    strikes = sorted({k[1] for k in chain.keys() if k[0] == ds and k[2] == right})
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - raw_strike))

@dataclass
class ORPocTradeReal:
    date:          str
    entry_date:    str
    entry_time:    str
    exit_time:     str
    direction:     str
    strike:        float
    or_high:       float
    or_low:        float
    poc:           float
    entry_spy:     float
    stop_spy:      float
    target_spy:    float
    exit_spy:      float
    entry_option:  float
    exit_option:   float
    exit_reason:   str
    pnl:           float
    stop_distance: float

def _simulate_trade_real(sig: dict, stop_distance: float) -> Optional[ORPocTradeReal]:
    ds, direction, strike = sig['ds'], sig['direction'], sig['strike']
    poc, bars, entry_idx  = sig['poc'], sig['bars'], sig['entry_idx']
    entry_dt, entry_spy   = sig['entry_dt'], sig['entry_spy']
    chain = sig.get('chain')
    if not chain:
        return None

    if direction == 'c':
        stop_spy = poc - stop_distance
        if stop_spy >= entry_spy:
            return None
        risk       = entry_spy - stop_spy
        target_spy = entry_spy + OR_POC_RR * risk
    else:
        stop_spy = poc + stop_distance
        if stop_spy <= entry_spy:
            return None
        risk       = stop_spy - entry_spy
        target_spy = entry_spy - OR_POC_RR * risk
    if risk < 0.02:
        return None

    entry_hm = entry_dt.strftime('%H:%M')
    entry_ask = _nearest_prior_quote(chain, entry_hm, 'ask')
    if entry_ask is None or entry_ask <= 0:
        return None

    exit_bar, exit_reason, exit_spy = None, 'EOD', None
    cutoff = (13, 30)
    for b2 in bars[entry_idx + 1:]:
        t2 = _et(b2)
        if (t2.hour, t2.minute) >= cutoff:
            exit_bar, exit_reason, exit_spy = b2, '1:30 cutoff', b2['c']
            break
        if direction == 'c':
            if b2['l'] <= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (POC-{stop_distance:.2f})', stop_spy
                break
            if b2['h'] >= target_spy:
                exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                break
        else:
            if b2['h'] >= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (POC+{stop_distance:.2f})', stop_spy
                break
            if b2['l'] <= target_spy:
                exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                break
    if exit_bar is None:
        exit_bar, exit_reason, exit_spy = bars[-1], 'EOD', bars[-1]['c']
    exit_dt = _et(exit_bar)

    exit_bid = _nearest_prior_quote(chain, exit_dt.strftime('%H:%M'), 'bid')
    if exit_bid is None:
        return None

    pnl = round((exit_bid - entry_ask) * 100, 2)
    return ORPocTradeReal(
        date=ds, entry_date=ds, entry_time=entry_hm, exit_time=exit_dt.strftime('%H:%M'),
        direction=('CALL' if direction == 'c' else 'PUT'), strike=strike,
        or_high=sig['or_high'], or_low=sig['or_low'], poc=poc,
        entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2),
        target_spy=round(target_spy, 2), exit_spy=round(exit_spy, 2),
        entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
        exit_reason=exit_reason, pnl=pnl, stop_distance=stop_distance)

def run_r7_or_poc_rr3_real(spy_1min: dict, spy_5min: dict, dates: List[str],
                            stop_distances: List[float],
                            progress_every: int = 50) -> Tuple[Dict[float, List[ORPocTradeReal]], dict]:
    coverage = {'total_days': 0, 'theta_usable': 0, 'theta_skipped': 0, 'signal_days': 0,
                'strike_unavailable': 0, 'intraday_fetch_ok': 0, 'intraday_fetch_failed': 0}
    signals = []

    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < OR_POC_MIN + 5 or len(bars_5min) < 4:
            continue
        coverage['total_days'] += 1
        if not theta_0dte_usable(ds):
            coverage['theta_skipped'] += 1
            continue
        coverage['theta_usable'] += 1

        sig = find_or_poc_signal(ds, bars_1min, bars_5min)
        if sig is None:
            continue
        coverage['signal_days'] += 1

        # Real quotes only exist for strikes actually listed that day - snap
        # the BS path's round-to-$0.50 strike to the nearest real one.
        right = 'C' if sig['direction'] == 'c' else 'P'
        real_strike = snap_to_real_strike(ds, sig['strike'], right)
        if real_strike is None:
            coverage['strike_unavailable'] += 1
            continue
        sig['strike'] = real_strike
        signals.append(sig)

    print(f'  {len(signals)} signal days -> fetching real intraday quotes '
          f'(cache-first, one call/day covering entry->13:30)...')
    for n, sig in enumerate(signals, 1):
        right = 'C' if sig['direction'] == 'c' else 'P'
        chain_list = fetch_intraday_chain(
            sig['ds'], sig['strike'], right,
            sig['entry_dt'].strftime('%H:%M:%S'), '13:30:00')
        sig['chain'] = {r['hm']: r for r in chain_list} if chain_list else None
        if sig['chain']:
            coverage['intraday_fetch_ok'] += 1
        else:
            coverage['intraday_fetch_failed'] += 1
        if progress_every and n % progress_every == 0:
            print(f'    {n}/{len(signals)} fetched  '
                  f'(api_calls={THETA_API_CALLS}, api_time={THETA_API_SECONDS:.1f}s)')

    results = {}
    for sd in stop_distances:
        trades = []
        for sig in signals:
            t = _simulate_trade_real(sig, sd)
            if t is not None:
                trades.append(t)
        results[sd] = trades

    return results, coverage

def write_real_sweep_csv(results: Dict[float, List[ORPocTradeReal]], path: Path):
    import csv as _csv
    with path.open('w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['stop_distance', 'date', 'entry_time', 'entry_price', 'stop', 'target',
                    'exit_price', 'exit_reason', 'pnl', 'direction', 'strike', 'poc',
                    'or_high', 'or_low', 'exit_time', 'option_entry_price', 'option_exit_price'])
        for sd, trades in results.items():
            for t in trades:
                w.writerow([sd, t.date, t.entry_time, t.entry_spy, t.stop_spy, t.target_spy,
                            t.exit_spy, t.exit_reason, t.pnl, t.direction, t.strike, t.poc,
                            t.or_high, t.or_low, t.exit_time, t.entry_option, t.exit_option])

def run_orb_poc_real_cli():
    print(f"\n{'='*70}\n  R7-POC-RR3 - REAL intraday ThetaData quotes, stop-distance sweep\n{'='*70}\n")
    print("  Loading market data...")
    spy_daily = load_spy_daily()
    spy_1min  = load_spy_1min()
    spy_5min  = load_spy_5min()
    all_dates = sorted(spy_daily.keys())

    stop_distances = [0.05, 0.10, 0.15, 0.25, 0.50]
    t0 = time.time()
    results, coverage = run_r7_or_poc_rr3_real(spy_1min, spy_5min, all_dates, stop_distances)
    elapsed = time.time() - t0

    out_path = BASE / 'trades_rr3.0_real_sweep.csv'
    write_real_sweep_csv(results, out_path)

    print(f"\n  Elapsed: {elapsed:.1f}s\n")
    print("  -- Coverage --")
    print(f"  Total candidate days:     {coverage['total_days']}")
    print(f"  Theta 0DTE usable:        {coverage['theta_usable']}")
    print(f"  Theta 0DTE skipped:       {coverage['theta_skipped']}")
    print(f"  Signal days:              {coverage['signal_days']}")
    print(f"  Strike unavailable:       {coverage['strike_unavailable']}  (no real listed contract near that strike)")
    print(f"  Intraday fetch OK:        {coverage['intraday_fetch_ok']}")
    print(f"  Intraday fetch failed:    {coverage['intraday_fetch_failed']}")
    print(f"  ThetaData API calls:      {THETA_API_CALLS}  (total {THETA_API_SECONDS:.1f}s, "
          f"avg {THETA_API_SECONDS/max(THETA_API_CALLS,1)*1000:.0f}ms/call)")
    print()
    print("  -- Per-stop-distance stats (real intraday fills) --")
    for sd in stop_distances:
        s = calc_stats(results[sd])
        print(f"  stop=${sd:.2f}  N={s['n']:>4}  WR={s['wr']:>5.1f}%  "
              f"Total P&L=${s['total_pnl']:>+10,.2f}  PF={fmt_pf(s):>5}  "
              f"AvgWin=${s['avg_win']:>7.2f}  AvgLoss=${s['avg_loss']:>8.2f}  MaxDD=${s['max_dd']:>9,.2f}")
    print(f"\n  Wrote sweep results -> {out_path}")
    return results, coverage


# ══════════════════════════════════════════════════════════════════════════════
# R7-POC-RR3 WEEKLY variant — identical OR/POC/breakout signal logic and exit
# walk as run_r7_or_poc_rr3_real above, but instead of buying the same-day
# (0DTE) contract, buys the NEAREST expiration that is 3-5 calendar days out
# (a "weekly", never same-day). Tests whether the 0DTE version's edge is
# actually being eaten by 0DTE's wide bid/ask spread: weeklies have much
# higher absolute premium (more time value) but should have tighter
# spread-as-%-of-price.
#
# Expirations come from a real ThetaData API call (option_list_expirations),
# cached once to disk (backtest_data/theta_SPY_expirations.pkl — already
# present from prior research, reused here). Real strikes for the chosen
# weekly expiration come from option_list_strikes (real API call, cached per
# expiration). Fills are real 1-min bid/ask from option_history_quote, exactly
# like the 0DTE path, just with `expiration` != `date`.
# ══════════════════════════════════════════════════════════════════════════════

WEEKLY_DTE_MIN = 3
WEEKLY_DTE_MAX = 5

EXPIRATIONS_CACHE_PATH      = DATA_DIR / 'theta_SPY_expirations.pkl'
EXPIRATIONS_CACHE_META_PATH = DATA_DIR / 'theta_SPY_expirations_fetched_at.pkl'
_ALL_EXPS_CACHE: Optional[List[date]] = None
_ALL_EXPS_CACHE_DATE: Optional[date] = None

def _theta_retry(fn, attempts: int = 4, base_delay: float = 2.0):
    """Small retry wrapper for live ThetaData calls — the remote gRPC
    endpoint occasionally returns a transient 502 (observed in testing),
    which clears on retry. Raises the last exception if all attempts fail.

    Does NOT retry thetadata.errors.NoDataFoundError — that's a legitimate
    "this contract/day has no data" business response (e.g. a probed
    strike that was never listed), not a transient failure, and retrying
    it would just waste ~12s of sleep per miss for no benefit."""
    from thetadata.errors import NoDataFoundError
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except NoDataFoundError:
            raise
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(base_delay * (i + 1))
    raise last_exc

def _extract_expiration_dates(raw) -> List[date]:
    """Normalize option_list_expirations()'s return value to a list of
    `date` objects. Newer thetadata-python returns a DataFrame with an
    'expiration' column; older versions returned a plain iterable of
    date/str. Handling both means this doesn't silently mis-parse if the
    installed SDK version changes again."""
    if hasattr(raw, 'columns') and 'expiration' in raw.columns:
        raw = raw['expiration']
    out = []
    for e in raw:
        out.append(e if isinstance(e, date) else date.fromisoformat(str(e)))
    return out


def get_all_theta_expirations(force_refresh: bool = False) -> List[date]:
    """All SPY option expirations ThetaData currently lists. Exchanges add
    new near-term weeklies/dailies on a rolling basis, so a cache that never
    expires will silently miss expirations for recent/live observation days
    (confirmed 2026-09-24: a stale cache built before 2026-09-21 was missing
    every expiration from 2026-09-21 through 2026-10-09). Refreshed at most
    once per calendar day - cheap enough (one API call) not to need a
    tighter TTL, and per-day is enough since expirations are never listed
    and then un-listed within a day.

    EXPIRATIONS_CACHE_PATH itself stays a bare sorted list of dates, same
    format as always - other scripts (e.g. backtest_lotto_basket.py) read
    that file directly and expect that shape. The daily-refresh TTL is
    tracked in a separate sidecar file (EXPIRATIONS_CACHE_META_PATH) so this
    fix doesn't change the on-disk contract for those other readers."""
    global _ALL_EXPS_CACHE, _ALL_EXPS_CACHE_DATE
    today = date.today()
    if not force_refresh and _ALL_EXPS_CACHE is not None and _ALL_EXPS_CACHE_DATE == today:
        return _ALL_EXPS_CACHE
    if not force_refresh and EXPIRATIONS_CACHE_PATH.exists() and EXPIRATIONS_CACHE_META_PATH.exists():
        with EXPIRATIONS_CACHE_META_PATH.open('rb') as f:
            fetched_at = pickle.load(f)
        if fetched_at == today:
            with EXPIRATIONS_CACHE_PATH.open('rb') as f:
                _ALL_EXPS_CACHE = pickle.load(f)
            _ALL_EXPS_CACHE_DATE = today
            return _ALL_EXPS_CACHE
    client = get_theta_client()
    raw = _theta_retry(lambda: client.option_list_expirations('SPY'))
    exps = sorted(_extract_expiration_dates(raw))
    with EXPIRATIONS_CACHE_PATH.open('wb') as f:
        pickle.dump(exps, f)
    with EXPIRATIONS_CACHE_META_PATH.open('wb') as f:
        pickle.dump(today, f)
    _ALL_EXPS_CACHE, _ALL_EXPS_CACHE_DATE = exps, today
    return exps

def nearest_weekly_expiration(ds: str, all_exps: List[date],
                               dte_min: int = WEEKLY_DTE_MIN,
                               dte_max: int = WEEKLY_DTE_MAX) -> Optional[date]:
    """Nearest expiration with dte_min <= (exp - ds).days <= dte_max — i.e.
    the closest expiration that is NOT same-day but is still within about a
    week out. None if no listed expiration falls in that band (rare, e.g.
    around holidays)."""
    obs = date.fromisoformat(ds)
    candidates = [e for e in all_exps if dte_min <= (e - obs).days <= dte_max]
    if not candidates:
        return None
    return min(candidates, key=lambda e: (e - obs).days)

INTRADAY_WEEKLY_CACHE_DIR = DATA_DIR / 'theta_intraday_weekly'
INTRADAY_WEEKLY_CACHE_DIR.mkdir(exist_ok=True)

def fetch_intraday_chain_weekly(ds: str, exp: date, strike: float, right: str,
                                 start_hms: str, end_hms: str) -> Optional[List[dict]]:
    """Weekly-expiration analogue of fetch_intraday_chain: real 1-min
    bid/ask for one (observation day, expiration, strike, right) contract
    over [start_hms, end_hms], cache-first, cache-forever (including None
    results, so a day with no data isn't re-hit on re-run)."""
    global THETA_API_CALLS, THETA_API_SECONDS
    tag  = (f"{ds}_{exp.isoformat()}_{strike:g}_{right}_"
            f"{start_hms.replace(':','')}_{end_hms.replace(':','')}")
    path = INTRADAY_WEEKLY_CACHE_DIR / f'{tag}.pkl'
    if path.exists():
        with path.open('rb') as f:
            return pickle.load(f)

    client = get_theta_client()
    t0 = time.time()
    try:
        df = _theta_retry(lambda: client.option_history_quote(
            symbol='SPY', expiration=exp, interval='1m',
            date=date.fromisoformat(ds), strike=f'{strike:g}', right=right,
            start_time=start_hms, end_time=end_hms))
    except Exception:
        df = None
    THETA_API_CALLS   += 1
    THETA_API_SECONDS += time.time() - t0

    if df is None or len(df) == 0:
        with path.open('wb') as f:
            pickle.dump(None, f)
        return None

    records = []
    for _, row in df.iterrows():
        ts = row['timestamp']
        if ts is None:
            continue
        bid = float(row['bid']) if row['bid'] == row['bid'] else None
        ask = float(row['ask']) if row['ask'] == row['ask'] else None
        records.append({'hm': ts.strftime('%H:%M'), 'bid': bid, 'ask': ask})
    with path.open('wb') as f:
        pickle.dump(records, f)
    return records

STRIKE_STEP           = 0.5   # SPY's near-term strike increment
STRIKE_SEARCH_RADIUS  = 4     # probe up to +/- 4 steps ($2) from the raw guess

def find_weekly_contract(ds: str, exp: date, raw_strike: float, right: str,
                          start_hms: str, end_hms: str,
                          radius: int = STRIKE_SEARCH_RADIUS,
                          step: float = STRIKE_STEP
                          ) -> Tuple[Optional[float], Optional[List[dict]]]:
    """Find a real, quoted weekly contract near raw_strike by probing
    outward with narrow single-strike intraday quote fetches.

    NOTE: option_list_strikes and option_history_eod(strike='*') both
    returned persistent 502s from ThetaData's remote endpoint during
    testing (bulk/wildcard queries) while single-strike, single-day
    option_history_quote calls were reliable. So instead of listing the
    real chain and snapping to it, this probes candidate strikes
    (raw_strike, then +/-0.5, +/-1.0, ... out to `radius` steps) directly
    via the narrow endpoint and takes the first one with real data — which
    both confirms the strike is real AND fetches its quotes in the same
    call, so no separate snap step or extra round-trip is needed.
    """
    tried = set()
    offsets = [0.0]
    for k in range(1, radius + 1):
        offsets += [k * step, -k * step]
    for off in offsets:
        strike = round((raw_strike + off) / step) * step
        if strike in tried or strike <= 0:
            continue
        tried.add(strike)
        chain_list = fetch_intraday_chain_weekly(ds, exp, strike, right, start_hms, end_hms)
        if chain_list:
            return strike, chain_list
    return None, None

@dataclass
class ORPocTradeWeekly:
    date:          str
    entry_date:    str
    entry_time:    str
    exit_time:     str
    direction:     str
    strike:        float
    expiration:    str
    dte:           int
    or_high:       float
    or_low:        float
    poc:           float
    entry_spy:     float
    stop_spy:      float
    target_spy:    float
    exit_spy:      float
    entry_option:  float
    exit_option:   float
    exit_reason:   str
    pnl:           float
    stop_distance: float
    entry_bid:     Optional[float]
    entry_ask:     float
    exit_bid:      float
    exit_ask:      Optional[float]

def _simulate_trade_weekly(sig: dict, stop_distance: float) -> Optional[ORPocTradeWeekly]:
    """Weekly-expiration analogue of _simulate_trade_real: identical
    stop/target/exit-walk logic (unchanged from the 0DTE path — only the
    option contract being priced differs), but also records both sides of
    the entry/exit quote (not just the side used for P&L) so spread-as-%-
    of-price can be computed afterward without re-fetching anything."""
    ds, direction, strike = sig['ds'], sig['direction'], sig['strike']
    poc, bars, entry_idx  = sig['poc'], sig['bars'], sig['entry_idx']
    entry_dt, entry_spy   = sig['entry_dt'], sig['entry_spy']
    chain = sig.get('chain')
    if not chain:
        return None

    if direction == 'c':
        stop_spy = poc - stop_distance
        if stop_spy >= entry_spy:
            return None
        risk       = entry_spy - stop_spy
        target_spy = entry_spy + OR_POC_RR * risk
    else:
        stop_spy = poc + stop_distance
        if stop_spy <= entry_spy:
            return None
        risk       = stop_spy - entry_spy
        target_spy = entry_spy - OR_POC_RR * risk
    if risk < 0.02:
        return None

    entry_hm  = entry_dt.strftime('%H:%M')
    entry_bid = _nearest_prior_quote(chain, entry_hm, 'bid')
    entry_ask = _nearest_prior_quote(chain, entry_hm, 'ask')
    if entry_ask is None or entry_ask <= 0:
        return None

    exit_bar, exit_reason, exit_spy = None, 'EOD', None
    cutoff = (13, 30)
    for b2 in bars[entry_idx + 1:]:
        t2 = _et(b2)
        if (t2.hour, t2.minute) >= cutoff:
            exit_bar, exit_reason, exit_spy = b2, '1:30 cutoff', b2['c']
            break
        if direction == 'c':
            if b2['l'] <= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (POC-{stop_distance:.2f})', stop_spy
                break
            if b2['h'] >= target_spy:
                exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                break
        else:
            if b2['h'] >= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (POC+{stop_distance:.2f})', stop_spy
                break
            if b2['l'] <= target_spy:
                exit_bar, exit_reason, exit_spy = b2, '3:1 target', target_spy
                break
    if exit_bar is None:
        exit_bar, exit_reason, exit_spy = bars[-1], 'EOD', bars[-1]['c']
    exit_dt = _et(exit_bar)

    exit_hm  = exit_dt.strftime('%H:%M')
    exit_bid = _nearest_prior_quote(chain, exit_hm, 'bid')
    exit_ask = _nearest_prior_quote(chain, exit_hm, 'ask')
    if exit_bid is None:
        return None

    pnl = round((exit_bid - entry_ask) * 100, 2)
    return ORPocTradeWeekly(
        date=ds, entry_date=ds, entry_time=entry_hm, exit_time=exit_hm,
        direction=('CALL' if direction == 'c' else 'PUT'), strike=strike,
        expiration=sig['expiration'].isoformat(), dte=sig['dte'],
        or_high=sig['or_high'], or_low=sig['or_low'], poc=poc,
        entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2),
        target_spy=round(target_spy, 2), exit_spy=round(exit_spy, 2),
        entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
        exit_reason=exit_reason, pnl=pnl, stop_distance=stop_distance,
        entry_bid=(round(entry_bid, 4) if entry_bid is not None else None),
        entry_ask=round(entry_ask, 4), exit_bid=round(exit_bid, 4),
        exit_ask=(round(exit_ask, 4) if exit_ask is not None else None))

def build_or_poc_weekly_signals(spy_1min: dict, spy_5min: dict, dates: List[str],
                                 dte_min: int = WEEKLY_DTE_MIN,
                                 dte_max: int = WEEKLY_DTE_MAX) -> Tuple[list, dict]:
    """Signal-building half of the weekly pipeline, split out from the
    fetch+simulate half so callers (e.g. an IS/OOS driver) can get the
    exact same signal-day set used for trading without re-fetching quotes.

    Uses the SAME gates as run_r7_or_poc_rr3_real (theta_0dte_usable +
    find_or_poc_signal + a real 0DTE strike existing) so the signal-day set
    is identical to the 0DTE real-quote run — this keeps the two variants
    directly comparable (same days, same direction, same OR/POC/entry —
    only the traded contract's expiration differs). On top of that, each
    signal also needs a real weekly expiration in the DTE band; days
    failing that are excluded and counted separately in `coverage` (not
    folded into strike_0dte_unavailable). The weekly contract itself is
    NOT validated here — see find_weekly_contract, called from the
    fetch step in run_r7_or_poc_weekly_rr3_real, which probes for a real
    strike and fetches its quotes in one step (bulk strike-listing calls
    were unreliable against ThetaData's remote endpoint - see its
    docstring).
    """
    coverage = {'total_days': 0, 'theta_usable': 0, 'theta_skipped': 0,
                'signal_days': 0, 'strike_0dte_unavailable': 0, 'no_weekly_exp': 0}
    all_exps = get_all_theta_expirations()
    signals = []
    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < OR_POC_MIN + 5 or len(bars_5min) < 4:
            continue
        coverage['total_days'] += 1
        if not theta_0dte_usable(ds):
            coverage['theta_skipped'] += 1
            continue
        coverage['theta_usable'] += 1

        sig = find_or_poc_signal(ds, bars_1min, bars_5min)
        if sig is None:
            continue

        right = 'C' if sig['direction'] == 'c' else 'P'
        if snap_to_real_strike(ds, sig['strike'], right) is None:
            coverage['strike_0dte_unavailable'] += 1
            continue
        coverage['signal_days'] += 1

        exp = nearest_weekly_expiration(ds, all_exps, dte_min, dte_max)
        if exp is None:
            coverage['no_weekly_exp'] += 1
            continue
        sig['expiration'] = exp
        sig['dte']        = (exp - date.fromisoformat(ds)).days
        signals.append(sig)
    return signals, coverage

def run_r7_or_poc_weekly_rr3_real(spy_1min: dict, spy_5min: dict, dates: List[str],
                                   stop_distances: List[float],
                                   dte_min: int = WEEKLY_DTE_MIN,
                                   dte_max: int = WEEKLY_DTE_MAX,
                                   progress_every: int = 25
                                   ) -> Tuple[Dict[float, List[ORPocTradeWeekly]], dict, list]:
    signals, coverage = build_or_poc_weekly_signals(spy_1min, spy_5min, dates, dte_min, dte_max)
    coverage['weekly_contract_unavailable'] = 0
    coverage['intraday_fetch_ok'] = 0
    coverage['intraday_fetch_failed'] = 0

    print(f'  {len(signals)} signal days -> probing/fetching real WEEKLY intraday quotes '
          f'(cache-first, strike probe + one call/day covering entry->13:30)...')
    for n, sig in enumerate(signals, 1):
        right = 'C' if sig['direction'] == 'c' else 'P'
        strike, chain_list = find_weekly_contract(
            sig['ds'], sig['expiration'], sig['strike'], right,
            sig['entry_dt'].strftime('%H:%M:%S'), '13:30:00')
        if chain_list:
            sig['strike'] = strike
            sig['chain']  = {r['hm']: r for r in chain_list}
            coverage['intraday_fetch_ok'] += 1
        else:
            sig['chain'] = None
            coverage['weekly_contract_unavailable'] += 1
            coverage['intraday_fetch_failed'] += 1
        if progress_every and n % progress_every == 0:
            print(f'    {n}/{len(signals)} fetched  '
                  f'(api_calls={THETA_API_CALLS}, api_time={THETA_API_SECONDS:.1f}s)')

    results = {}
    for sd in stop_distances:
        trades = []
        for sig in signals:
            t = _simulate_trade_weekly(sig, sd)
            if t is not None:
                trades.append(t)
        results[sd] = trades

    return results, coverage, signals

def write_weekly_sweep_csv(results: Dict[float, List[ORPocTradeWeekly]], path: Path):
    import csv as _csv
    with path.open('w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['stop_distance', 'date', 'entry_time', 'entry_price', 'stop', 'target',
                    'exit_price', 'exit_reason', 'pnl', 'direction', 'strike', 'expiration',
                    'dte', 'poc', 'or_high', 'or_low', 'exit_time', 'option_entry_bid',
                    'option_entry_ask', 'option_exit_bid', 'option_exit_ask'])
        for sd, trades in results.items():
            for t in trades:
                w.writerow([sd, t.date, t.entry_time, t.entry_spy, t.stop_spy, t.target_spy,
                            t.exit_spy, t.exit_reason, t.pnl, t.direction, t.strike,
                            t.expiration, t.dte, t.poc, t.or_high, t.or_low, t.exit_time,
                            t.entry_bid, t.entry_ask, t.exit_bid, t.exit_ask])


# ══════════════════════════════════════════════════════════════════════════════
# VWAP Reversion (fade) - reuses the OR/POC real-quote infrastructure wholesale
# (theta_0dte_usable, snap_to_real_strike, fetch_intraday_chain, THETA_API_*
# counters, _nearest_prior_quote, calc_stats) - only the signal/stop/target
# logic is new.
#
# Signal: cumulative intraday VWAP +/- 2 volume-weighted sigma bands, from
# 1-min bars starting at the 9:30 open. Entry window 9:45-12:00 ET. A 5-min
# candle closes beyond a band (breach), then if the VERY NEXT 5-min candle
# closes back inside the band (rejection), enter at that second candle's own
# close - fading back toward VWAP (PUT if faded from above, CALL if faded
# from below). Stop = the most extreme high/low reached across the breach +
# rejection candle pair, plus a buffer (swept $0.05-$0.50, same grid as
# OR/POC). Target = the VWAP value AT ENTRY, held fixed (not re-evaluated
# as VWAP drifts afterward). Exit: stop / target / 1:30 ET cutoff, whichever
# first, walked on 1-min bars exactly like OR/POC's exit walk.
# ══════════════════════════════════════════════════════════════════════════════

def calc_vwap_bands(bars_1min: list, band_mult: float = 2.0) -> Dict[str, Tuple[float, float, float]]:
    """Cumulative intraday VWAP + volume-weighted N-sigma bands, computed
    bar-by-bar from the 9:30 ET open using 1-min bars (no lookahead - each
    entry only uses bars up to and including that minute). Returns
    {'HH:MM': (vwap, upper, lower)}."""
    result: Dict[str, Tuple[float, float, float]] = {}
    cum_v = cum_pv = cum_pv2 = 0.0
    for b in bars_1min:
        t = _et(b)
        if (t.hour, t.minute) < (9, 30):
            continue
        tp = (b['h'] + b['l'] + b['c']) / 3.0
        v  = b['v']
        if v > 0:
            cum_pv  += v * tp
            cum_v   += v
            cum_pv2 += v * tp * tp
        if cum_v <= 0:
            continue
        vwap     = cum_pv / cum_v
        variance = max(cum_pv2 / cum_v - vwap * vwap, 0.0)
        std      = variance ** 0.5
        result[t.strftime('%H:%M')] = (vwap, vwap + band_mult * std, vwap - band_mult * std)
    return result

def _nearest_prior_vwap(vwap_map: Dict[str, tuple], hm: str, max_back: int = 4):
    h, m = int(hm[:2]), int(hm[3:])
    for _ in range(max_back + 1):
        key = f'{h:02d}:{m:02d}'
        if key in vwap_map:
            return vwap_map[key]
        m -= 1
        if m < 0:
            h, m = h - 1, 59
    return None

def find_vwap_reversion_signal(ds: str, bars_1min: list, bars_5min: list) -> Optional[dict]:
    """Breach-then-rejection VWAP fade signal. See module docstring above."""
    vwap_map = calc_vwap_bands(bars_1min)
    if not vwap_map:
        return None

    def band_at(bar5):
        end_min = _et(bar5) + timedelta(minutes=4)
        return _nearest_prior_vwap(vwap_map, end_min.strftime('%H:%M'))

    breach_dir, breach_bar = None, None
    direction, entry_bar_5m, vwap_entry, extreme = None, None, None, None

    for b in bars_5min:
        t = _et(b)
        if (t.hour, t.minute) < (9, 45):
            continue
        if (t.hour, t.minute) >= (12, 0):
            break
        band = band_at(b)
        if band is None:
            continue
        vwap, upper, lower = band

        if breach_dir is not None:
            if lower <= b['c'] <= upper:
                direction    = 'p' if breach_dir == 'above' else 'c'
                entry_bar_5m = b
                vwap_entry   = vwap
                extreme      = (max(breach_bar['h'], b['h']) if breach_dir == 'above'
                                 else min(breach_bar['l'], b['l']))
                break
            breach_dir, breach_bar = None, None   # no rejection -> pattern invalidated

        if breach_dir is None:
            if b['c'] > upper:
                breach_dir, breach_bar = 'above', b
            elif b['c'] < lower:
                breach_dir, breach_bar = 'below', b

    if direction is None:
        return None

    strike         = round(entry_bar_5m['c'] / 0.5) * 0.5
    entry_close_ts = _et(entry_bar_5m) + timedelta(minutes=5)
    entry_idx      = next((i for i, b in enumerate(bars_1min) if _et(b) >= entry_close_ts), None)
    if entry_idx is None:
        return None
    entry_dt = _et(bars_1min[entry_idx])

    return {'ds': ds, 'bars': bars_1min, 'direction': direction, 'entry_idx': entry_idx,
            'entry_dt': entry_dt, 'entry_spy': entry_bar_5m['c'], 'strike': strike,
            'vwap_entry': vwap_entry, 'extreme': round(extreme, 2)}

@dataclass
class VwapTradeReal:
    date:          str
    entry_date:    str
    entry_time:    str
    exit_time:     str
    direction:     str
    strike:        float
    vwap_entry:    float
    extreme:       float
    entry_spy:     float
    stop_spy:      float
    target_spy:    float
    exit_spy:      float
    entry_option:  float
    exit_option:   float
    exit_reason:   str
    pnl:           float
    stop_buffer:   float

def _simulate_vwap_trade_real(sig: dict, buffer: float) -> Optional[VwapTradeReal]:
    ds, direction         = sig['ds'], sig['direction']
    bars, entry_idx       = sig['bars'], sig['entry_idx']
    entry_dt, entry_spy   = sig['entry_dt'], sig['entry_spy']
    strike                = sig['strike']
    vwap_entry, extreme   = sig['vwap_entry'], sig['extreme']
    chain = sig.get('chain')
    if not chain:
        return None

    if direction == 'p':          # faded a move ABOVE the upper band -> short bias
        stop_spy = extreme + buffer
        if stop_spy <= entry_spy:
            return None
        target_spy = vwap_entry
        if target_spy >= entry_spy:
            return None
    else:                          # 'c', faded a move BELOW the lower band -> long bias
        stop_spy = extreme - buffer
        if stop_spy >= entry_spy:
            return None
        target_spy = vwap_entry
        if target_spy <= entry_spy:
            return None

    entry_hm  = entry_dt.strftime('%H:%M')
    entry_ask = _nearest_prior_quote(chain, entry_hm, 'ask')
    if entry_ask is None or entry_ask <= 0:
        return None

    exit_bar, exit_reason, exit_spy = None, 'EOD', None
    cutoff = (13, 30)
    for b2 in bars[entry_idx + 1:]:
        t2 = _et(b2)
        if (t2.hour, t2.minute) >= cutoff:
            exit_bar, exit_reason, exit_spy = b2, '1:30 cutoff', b2['c']
            break
        if direction == 'p':
            if b2['h'] >= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (extreme+{buffer:.2f})', stop_spy
                break
            if b2['l'] <= target_spy:
                exit_bar, exit_reason, exit_spy = b2, 'VWAP target', target_spy
                break
        else:
            if b2['l'] <= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (extreme-{buffer:.2f})', stop_spy
                break
            if b2['h'] >= target_spy:
                exit_bar, exit_reason, exit_spy = b2, 'VWAP target', target_spy
                break
    if exit_bar is None:
        exit_bar, exit_reason, exit_spy = bars[-1], 'EOD', bars[-1]['c']
    exit_dt = _et(exit_bar)

    exit_bid = _nearest_prior_quote(chain, exit_dt.strftime('%H:%M'), 'bid')
    if exit_bid is None:
        return None

    pnl = round((exit_bid - entry_ask) * 100, 2)
    return VwapTradeReal(
        date=ds, entry_date=ds, entry_time=entry_hm, exit_time=exit_dt.strftime('%H:%M'),
        direction=('PUT' if direction == 'p' else 'CALL'), strike=strike,
        vwap_entry=round(vwap_entry, 2), extreme=extreme,
        entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2),
        target_spy=round(target_spy, 2), exit_spy=round(exit_spy, 2),
        entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
        exit_reason=exit_reason, pnl=pnl, stop_buffer=buffer)

def run_vwap_reversion_real(spy_1min: dict, spy_5min: dict, dates: List[str],
                             stop_buffers: List[float],
                             progress_every: int = 50) -> Tuple[Dict[float, List[VwapTradeReal]], dict]:
    coverage = {'total_days': 0, 'theta_usable': 0, 'theta_skipped': 0, 'signal_days': 0,
                'strike_unavailable': 0, 'intraday_fetch_ok': 0, 'intraday_fetch_failed': 0}
    signals = []

    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        bars_5min = spy_5min.get(ds, [])
        if len(bars_1min) < 20 or len(bars_5min) < 4:
            continue
        coverage['total_days'] += 1
        if not theta_0dte_usable(ds):
            coverage['theta_skipped'] += 1
            continue
        coverage['theta_usable'] += 1

        sig = find_vwap_reversion_signal(ds, bars_1min, bars_5min)
        if sig is None:
            continue
        coverage['signal_days'] += 1

        right = 'C' if sig['direction'] == 'c' else 'P'
        real_strike = snap_to_real_strike(ds, sig['strike'], right)
        if real_strike is None:
            coverage['strike_unavailable'] += 1
            continue
        sig['strike'] = real_strike
        signals.append(sig)

    print(f'  {len(signals)} signal days -> fetching real intraday quotes '
          f'(cache-first, one call/day covering entry->13:30)...')
    for n, sig in enumerate(signals, 1):
        right = 'C' if sig['direction'] == 'c' else 'P'
        chain_list = fetch_intraday_chain(
            sig['ds'], sig['strike'], right,
            sig['entry_dt'].strftime('%H:%M:%S'), '13:30:00')
        sig['chain'] = {r['hm']: r for r in chain_list} if chain_list else None
        if sig['chain']:
            coverage['intraday_fetch_ok'] += 1
        else:
            coverage['intraday_fetch_failed'] += 1
        if progress_every and n % progress_every == 0:
            print(f'    {n}/{len(signals)} fetched  '
                  f'(api_calls={THETA_API_CALLS}, api_time={THETA_API_SECONDS:.1f}s)')

    results = {}
    for buf in stop_buffers:
        trades = []
        for sig in signals:
            t = _simulate_vwap_trade_real(sig, buf)
            if t is not None:
                trades.append(t)
        results[buf] = trades

    return results, coverage

def write_vwap_csv(results: Dict[float, List[VwapTradeReal]], path: Path):
    import csv as _csv
    with path.open('w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['stop_buffer', 'date', 'entry_time', 'entry_price', 'stop', 'target',
                    'exit_price', 'exit_reason', 'pnl', 'direction', 'strike', 'vwap_entry',
                    'extreme', 'exit_time', 'option_entry_price', 'option_exit_price'])
        for buf, trades in results.items():
            for t in trades:
                w.writerow([buf, t.date, t.entry_time, t.entry_spy, t.stop_spy, t.target_spy,
                            t.exit_spy, t.exit_reason, t.pnl, t.direction, t.strike,
                            t.vwap_entry, t.extreme, t.exit_time, t.entry_option, t.exit_option])

def run_vwap_reversion_cli():
    print(f"\n{'='*70}\n  VWAP Reversion (fade) - REAL intraday ThetaData quotes, stop-buffer sweep\n{'='*70}\n")
    print("  Loading market data...")
    spy_daily = load_spy_daily()
    spy_1min  = load_spy_1min()
    spy_5min  = load_spy_5min()
    all_dates = sorted(spy_daily.keys())

    stop_buffers = [0.05, 0.10, 0.15, 0.25, 0.50]
    t0 = time.time()
    results, coverage = run_vwap_reversion_real(spy_1min, spy_5min, all_dates, stop_buffers)
    elapsed = time.time() - t0

    out_path = BASE / 'trades_vwap_reversion.csv'
    write_vwap_csv(results, out_path)

    print(f"\n  Elapsed: {elapsed:.1f}s\n")
    print("  -- Coverage --")
    print(f"  Total candidate days:     {coverage['total_days']}")
    print(f"  Theta 0DTE usable:        {coverage['theta_usable']}")
    print(f"  Theta 0DTE skipped:       {coverage['theta_skipped']}")
    print(f"  Signal days:              {coverage['signal_days']}")
    print(f"  Strike unavailable:       {coverage['strike_unavailable']}  (no real listed contract near that strike)")
    print(f"  Intraday fetch OK:        {coverage['intraday_fetch_ok']}")
    print(f"  Intraday fetch failed:    {coverage['intraday_fetch_failed']}")
    print(f"  ThetaData API calls:      {THETA_API_CALLS}  (total {THETA_API_SECONDS:.1f}s, "
          f"avg {THETA_API_SECONDS/max(THETA_API_CALLS,1)*1000:.0f}ms/call)")
    print()
    print("  -- Per-stop-buffer stats (real intraday fills) --")
    for buf in stop_buffers:
        s = calc_stats(results[buf])
        print(f"  buffer=${buf:.2f}  N={s['n']:>4}  WR={s['wr']:>5.1f}%  "
              f"Total P&L=${s['total_pnl']:>+10,.2f}  PF={fmt_pf(s):>5}  "
              f"AvgWin=${s['avg_win']:>7.2f}  AvgLoss=${s['avg_loss']:>8.2f}  MaxDD=${s['max_dd']:>9,.2f}")
    print(f"\n  Wrote sweep results -> {out_path}")
    return results, coverage


# ══════════════════════════════════════════════════════════════════════════════
# S/R Retest (session high/low rejection) — reuses the OR/POC real-quote
# infrastructure wholesale (theta_0dte_usable, snap_to_real_strike,
# fetch_intraday_chain, THETA_API_* counters, _nearest_prior_quote,
# calc_stats, calibrate_iv_theta) — only the signal/stop/target logic is new.
#
# Signal: track the running session high and session low from the 9:30 ET
# open using 1-min bars. A level becomes eligible for retest once it has held
# (unbroken) for >= 30 minutes. Once eligible, a 1-min candle whose wick
# comes within $0.20 of the level (touching or slightly piercing it) while
# closing back on the original side is a rejection: retest of the low
# holding -> CALL, retest of the high holding -> PUT. Entry at the close of
# that candle. Any bar that prints a new session extreme resets that level's
# eligibility clock (it's a break of the old level, not a hold), which is
# handled implicitly by always testing against the live running high/low.
# Stop = a fixed distance PAST the level itself (not the rejection wick),
# swept $0.10 / $0.15. Target = fixed R:R off that risk, swept 1.5-3.5.
# Exit: stop / target / 15:45 ET cutoff, walked on 1-min bars exactly like
# OR/POC's exit walk.
# ══════════════════════════════════════════════════════════════════════════════

SR_ENTRY_START   = (9, 45)
SR_ENTRY_END     = (15, 0)
SR_EXIT_CUTOFF   = (15, 45)
SR_LEVEL_MIN_AGE = 30        # minutes a level must hold before it's retest-eligible
SR_PROXIMITY     = 0.20      # wick must come within this of the level
SR_STOP_WIDTHS   = [0.10, 0.15]
SR_RR_TARGETS    = [1.5, 2.0, 2.5, 3.0, 3.5]

def find_sr_retest_signal(ds: str, bars_1min: list) -> Optional[dict]:
    """First session-high/low retest-and-hold signal of the day, scanning
    1-min bars chronologically from the 9:30 open. See module comment above."""
    bars = bars_1min
    if len(bars) < 20:
        return None

    session_high = session_low = None
    high_set_t = low_set_t = None
    direction = level = entry_idx = entry_dt = entry_spy = None

    for i, b in enumerate(bars):
        t = _et(b)
        if session_high is None:
            session_high, session_low = b['h'], b['l']
            high_set_t = low_set_t = t
            continue
        if (t.hour, t.minute) > SR_ENTRY_END:
            break

        if (t.hour, t.minute) >= SR_ENTRY_START:
            low_age  = (t - low_set_t).total_seconds() / 60.0
            high_age = (t - high_set_t).total_seconds() / 60.0
            if (low_age >= SR_LEVEL_MIN_AGE and b['l'] <= session_low + SR_PROXIMITY
                    and b['c'] > session_low):
                direction, level = 'c', session_low
                entry_idx, entry_dt, entry_spy = i, t, b['c']
                break
            if (high_age >= SR_LEVEL_MIN_AGE and b['h'] >= session_high - SR_PROXIMITY
                    and b['c'] < session_high):
                direction, level = 'p', session_high
                entry_idx, entry_dt, entry_spy = i, t, b['c']
                break

        if b['h'] > session_high:
            session_high, high_set_t = b['h'], t
        if b['l'] < session_low:
            session_low, low_set_t = b['l'], t

    if direction is None:
        return None

    strike = round(entry_spy / 0.5) * 0.5
    return {'ds': ds, 'bars': bars, 'direction': direction, 'entry_idx': entry_idx,
            'entry_dt': entry_dt, 'entry_spy': entry_spy, 'strike': strike,
            'level_retested': round(level, 2)}

@dataclass
class SRRetestTradeReal:
    date:           str
    entry_date:     str
    entry_time:     str
    exit_time:      str
    direction:      str
    strike:         float
    level_retested: float
    entry_spy:      float
    stop_spy:       float
    target_spy:     float
    exit_spy:       float
    entry_option:   float
    exit_option:    float
    exit_reason:    str
    pnl:            float
    stop_width:     float
    rr:             float
    iv:             float
    iv_source:      str

def _simulate_sr_trade_real(sig: dict, stop_width: float, rr: float) -> Optional[SRRetestTradeReal]:
    ds, direction, strike = sig['ds'], sig['direction'], sig['strike']
    level, bars, entry_idx = sig['level_retested'], sig['bars'], sig['entry_idx']
    entry_dt, entry_spy    = sig['entry_dt'], sig['entry_spy']
    iv, iv_source          = sig['iv'], sig['iv_source']
    chain = sig.get('chain')
    if not chain:
        return None

    if direction == 'c':
        stop_spy = level - stop_width
        if stop_spy >= entry_spy:
            return None
        risk       = entry_spy - stop_spy
        target_spy = entry_spy + rr * risk
    else:
        stop_spy = level + stop_width
        if stop_spy <= entry_spy:
            return None
        risk       = stop_spy - entry_spy
        target_spy = entry_spy - rr * risk
    if risk < 0.02:
        return None

    entry_hm  = entry_dt.strftime('%H:%M')
    entry_ask = _nearest_prior_quote(chain, entry_hm, 'ask')
    if entry_ask is None or entry_ask <= 0:
        return None

    exit_bar, exit_reason, exit_spy = None, 'EOD', None
    for b2 in bars[entry_idx + 1:]:
        t2 = _et(b2)
        if (t2.hour, t2.minute) >= SR_EXIT_CUTOFF:
            exit_bar, exit_reason, exit_spy = b2, '3:45 cutoff', b2['c']
            break
        if direction == 'c':
            if b2['l'] <= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (level-{stop_width:.2f})', stop_spy
                break
            if b2['h'] >= target_spy:
                exit_bar, exit_reason, exit_spy = b2, f'{rr:.1f}:1 target', target_spy
                break
        else:
            if b2['h'] >= stop_spy:
                exit_bar, exit_reason, exit_spy = b2, f'stop (level+{stop_width:.2f})', stop_spy
                break
            if b2['l'] <= target_spy:
                exit_bar, exit_reason, exit_spy = b2, f'{rr:.1f}:1 target', target_spy
                break
    if exit_bar is None:
        exit_bar, exit_reason, exit_spy = bars[-1], 'EOD', bars[-1]['c']
    exit_dt = _et(exit_bar)

    exit_bid = _nearest_prior_quote(chain, exit_dt.strftime('%H:%M'), 'bid')
    if exit_bid is None:
        return None

    pnl = round((exit_bid - entry_ask) * 100, 2)
    return SRRetestTradeReal(
        date=ds, entry_date=ds, entry_time=entry_hm, exit_time=exit_dt.strftime('%H:%M'),
        direction=('CALL' if direction == 'c' else 'PUT'), strike=strike,
        level_retested=level, entry_spy=round(entry_spy, 2), stop_spy=round(stop_spy, 2),
        target_spy=round(target_spy, 2), exit_spy=round(exit_spy, 2),
        entry_option=round(entry_ask, 4), exit_option=round(exit_bid, 4),
        exit_reason=exit_reason, pnl=pnl, stop_width=stop_width, rr=rr,
        iv=round(iv, 4), iv_source=iv_source)

def run_sr_retest_real(spy_1min: dict, vix_daily: dict, dates: List[str],
                        stop_widths: List[float], rr_targets: List[float],
                        progress_every: int = 50
                        ) -> Tuple[Dict[Tuple[float, float], List[SRRetestTradeReal]], dict, List[str]]:
    coverage = {'total_days': 0, 'theta_usable': 0, 'theta_skipped': 0, 'signal_days': 0,
                'strike_unavailable': 0, 'intraday_fetch_ok': 0, 'intraday_fetch_failed': 0}
    signals = []

    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        if len(bars_1min) < 20:
            continue
        coverage['total_days'] += 1
        if not theta_0dte_usable(ds):
            coverage['theta_skipped'] += 1
            continue
        coverage['theta_usable'] += 1

        sig = find_sr_retest_signal(ds, bars_1min)
        if sig is None:
            continue
        coverage['signal_days'] += 1

        right = 'C' if sig['direction'] == 'c' else 'P'
        real_strike = snap_to_real_strike(ds, sig['strike'], right)
        if real_strike is None:
            coverage['strike_unavailable'] += 1
            continue
        sig['strike'] = real_strike

        r = rfr(date.fromisoformat(ds))
        iv, iv_source = calibrate_iv_theta(ds, sig['entry_spy'], r)
        if iv is None:
            iv, iv_source = vix_daily.get(ds, 16.0) / 100.0, 'vix_fallback'
        sig['iv'], sig['iv_source'] = iv, iv_source
        signals.append(sig)

    print(f'  {len(signals)} signal days -> fetching real intraday quotes '
          f'(cache-first, one call/day covering entry->15:45)...')
    for n, sig in enumerate(signals, 1):
        right = 'C' if sig['direction'] == 'c' else 'P'
        chain_list = fetch_intraday_chain(
            sig['ds'], sig['strike'], right,
            sig['entry_dt'].strftime('%H:%M:%S'), '15:45:00')
        sig['chain'] = {r['hm']: r for r in chain_list} if chain_list else None
        if sig['chain']:
            coverage['intraday_fetch_ok'] += 1
        else:
            coverage['intraday_fetch_failed'] += 1
        if progress_every and n % progress_every == 0:
            print(f'    {n}/{len(signals)} fetched  '
                  f'(api_calls={THETA_API_CALLS}, api_time={THETA_API_SECONDS:.1f}s)')

    results = {}
    for sw in stop_widths:
        for rr in rr_targets:
            trades = []
            for sig in signals:
                t = _simulate_sr_trade_real(sig, sw, rr)
                if t is not None:
                    trades.append(t)
            results[(sw, rr)] = trades

    signal_dates = sorted(sig['ds'] for sig in signals)
    return results, coverage, signal_dates

def write_sr_retest_csv(results: Dict[Tuple[float, float], List[SRRetestTradeReal]], path: Path):
    import csv as _csv
    with path.open('w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['stop_width', 'rr', 'date', 'entry_time', 'entry_price', 'stop', 'target',
                    'exit_price', 'exit_reason', 'pnl', 'direction', 'strike', 'level_retested',
                    'exit_time', 'option_entry_price', 'option_exit_price', 'iv', 'iv_source'])
        for (sw, rr), trades in results.items():
            for t in trades:
                w.writerow([sw, rr, t.date, t.entry_time, t.entry_spy, t.stop_spy, t.target_spy,
                            t.exit_spy, t.exit_reason, t.pnl, t.direction, t.strike,
                            t.level_retested, t.exit_time, t.entry_option, t.exit_option,
                            t.iv, t.iv_source])

def run_sr_retest_cli():
    print(f"\n{'='*70}\n  S/R Retest (session high/low rejection) - REAL intraday ThetaData "
          f"quotes, stop x R:R grid\n{'='*70}\n")
    print("  Loading market data...")
    spy_daily = load_spy_daily()
    spy_1min  = load_spy_1min()
    vix_daily = load_vix()
    all_dates = sorted(spy_daily.keys())

    t0 = time.time()
    results, coverage, signal_dates = run_sr_retest_real(
        spy_1min, vix_daily, all_dates, SR_STOP_WIDTHS, SR_RR_TARGETS)
    elapsed = time.time() - t0

    out_path = BASE / 'trades_sr_retest.csv'
    write_sr_retest_csv(results, out_path)

    print(f"\n  Elapsed: {elapsed:.1f}s\n")
    print("  -- Coverage --")
    print(f"  Total candidate days:     {coverage['total_days']}")
    print(f"  Theta 0DTE usable:        {coverage['theta_usable']}")
    print(f"  Theta 0DTE skipped:       {coverage['theta_skipped']}")
    print(f"  Signal days:              {coverage['signal_days']}")
    print(f"  Strike unavailable:       {coverage['strike_unavailable']}  (no real listed contract near that strike)")
    print(f"  Intraday fetch OK:        {coverage['intraday_fetch_ok']}")
    print(f"  Intraday fetch failed:    {coverage['intraday_fetch_failed']}")
    print(f"  ThetaData API calls:      {THETA_API_CALLS}  (total {THETA_API_SECONDS:.1f}s, "
          f"avg {THETA_API_SECONDS/max(THETA_API_CALLS,1)*1000:.0f}ms/call)")
    print()
    print("  -- Per-combo stats (real intraday fills) --")
    grid_stats = {}
    for sw in SR_STOP_WIDTHS:
        for rr in SR_RR_TARGETS:
            s = calc_stats(results[(sw, rr)])
            grid_stats[(sw, rr)] = s
            print(f"  stop=${sw:.2f}  rr={rr:.1f}  N={s['n']:>4}  WR={s['wr']:>5.1f}%  "
                  f"Total P&L=${s['total_pnl']:>+10,.2f}  PF={fmt_pf(s):>5}  "
                  f"AvgWin=${s['avg_win']:>7.2f}  AvgLoss=${s['avg_loss']:>8.2f}  MaxDD=${s['max_dd']:>9,.2f}")
    print(f"\n  Wrote sweep results -> {out_path}")

    finite = {k: v for k, v in grid_stats.items() if v['n'] > 0 and v['pf'] != float('inf')}
    pool   = finite if finite else grid_stats
    best_combo = max(pool, key=lambda k: (pool[k]['pf'], pool[k]['total_pnl']))
    print(f"\n  Best combo by PF (tiebreak total P&L): stop=${best_combo[0]:.2f} rr={best_combo[1]:.1f}  "
          f"{grid_stats[best_combo]}")

    n = len(signal_dates)
    split_idx = round(n * 0.70)
    is_dates  = set(signal_dates[:split_idx])
    oos_dates = set(signal_dates[split_idx:])
    best_trades = results[best_combo]
    is_trades   = [t for t in best_trades if t.date in is_dates]
    oos_trades  = [t for t in best_trades if t.date in oos_dates]
    s_is, s_oos = calc_stats(is_trades), calc_stats(oos_trades)
    print(f"\n  -- IS/OOS split on best combo (stop=${best_combo[0]:.2f} rr={best_combo[1]:.1f}) --")
    print(f"  {n} signal days total, 70% split = {split_idx}")
    if signal_dates:
        print(f"  IS:  {len(is_dates)} signal days ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})")
        print(f"  OOS: {len(oos_dates)} signal days ({signal_dates[min(split_idx,n-1)]} -> {signal_dates[-1]})")
    print(f"  IS  trades: N={s_is['n']:>4}  WR={s_is['wr']:>5.1f}%  Total P&L=${s_is['total_pnl']:>+10,.2f}  PF={fmt_pf(s_is):>5}")
    print(f"  OOS trades: N={s_oos['n']:>4}  WR={s_oos['wr']:>5.1f}%  Total P&L=${s_oos['total_pnl']:>+10,.2f}  PF={fmt_pf(s_oos):>5}")

    return results, coverage, grid_stats, best_combo, (s_is, s_oos)


# ══════════════════════════════════════════════════════════════════════════════
# S/R Retest — FILTERED variant. Two extra conditions layered on top of
# find_sr_retest_signal() above:
#   1. Only the FIRST retest of a given level fires. "A given level" = the
#      specific numeric session-high/session-low VALUE, not the category —
#      once THIS high (or low) has produced a signal, no further signal on
#      that same price for the rest of the session. If the running high/low
#      later moves to a genuinely new extreme, that new value has never been
#      retested, so it's independently eligible once it ages 30 min. This
#      means a day can now produce more than one trade (e.g. the session low
#      retests-and-holds early, then a later, deeper low does the same).
#   2. No signal may trigger in the 11:30-13:30 ET lunch window, even if
#      every other condition is met. Level tracking / aging is NOT paused
#      during that window — only signal firing is suppressed.
# Everything else (0.20 proximity, 30-min age, rejection-candle logic, real
# ThetaData fills, stop/target/3:45 cutoff) is unchanged from the base
# strategy and fully reused below.
# ══════════════════════════════════════════════════════════════════════════════

SR_LUNCH_START = (11, 30)
SR_LUNCH_END   = (13, 30)

def find_sr_retest_signals_filtered(ds: str, bars_1min: list) -> List[dict]:
    """Like find_sr_retest_signal(), but scans the WHOLE session (doesn't stop
    at the first hit) subject to the two filters described above. Returns a
    list of 0+ signal dicts, one per (level, first-retest) event."""
    bars = bars_1min
    if len(bars) < 20:
        return []

    session_high = session_low = None
    high_set_t = low_set_t = None
    high_signaled_level = low_signaled_level = None
    out = []

    for i, b in enumerate(bars):
        t = _et(b)
        if session_high is None:
            session_high, session_low = b['h'], b['l']
            high_set_t = low_set_t = t
            continue
        if (t.hour, t.minute) > SR_ENTRY_END:
            break

        hm = (t.hour, t.minute)
        in_window = SR_ENTRY_START <= hm <= SR_ENTRY_END
        in_lunch  = SR_LUNCH_START <= hm < SR_LUNCH_END
        if in_window and not in_lunch:
            low_age  = (t - low_set_t).total_seconds() / 60.0
            high_age = (t - high_set_t).total_seconds() / 60.0
            if (low_age >= SR_LEVEL_MIN_AGE and session_low != low_signaled_level
                    and b['l'] <= session_low + SR_PROXIMITY and b['c'] > session_low):
                out.append({'ds': ds, 'bars': bars, 'direction': 'c', 'entry_idx': i,
                            'entry_dt': t, 'entry_spy': b['c'],
                            'strike': round(b['c'] / 0.5) * 0.5,
                            'level_retested': round(session_low, 2)})
                low_signaled_level = session_low
            if (high_age >= SR_LEVEL_MIN_AGE and session_high != high_signaled_level
                    and b['h'] >= session_high - SR_PROXIMITY and b['c'] < session_high):
                out.append({'ds': ds, 'bars': bars, 'direction': 'p', 'entry_idx': i,
                            'entry_dt': t, 'entry_spy': b['c'],
                            'strike': round(b['c'] / 0.5) * 0.5,
                            'level_retested': round(session_high, 2)})
                high_signaled_level = session_high

        if b['h'] > session_high:
            session_high, high_set_t = b['h'], t
        if b['l'] < session_low:
            session_low, low_set_t = b['l'], t

    return out

def run_sr_retest_filtered_real(spy_1min: dict, vix_daily: dict, dates: List[str],
                                 stop_widths: List[float], rr_targets: List[float],
                                 progress_every: int = 50
                                 ) -> Tuple[Dict[Tuple[float, float], List[SRRetestTradeReal]], dict, List[str]]:
    coverage = {'total_days': 0, 'theta_usable': 0, 'theta_skipped': 0, 'signal_days': 0,
                'signal_count': 0, 'strike_unavailable': 0, 'intraday_fetch_ok': 0,
                'intraday_fetch_failed': 0}
    signals = []

    for ds in dates:
        bars_1min = spy_1min.get(ds, [])
        if len(bars_1min) < 20:
            continue
        coverage['total_days'] += 1
        if not theta_0dte_usable(ds):
            coverage['theta_skipped'] += 1
            continue
        coverage['theta_usable'] += 1

        day_sigs = find_sr_retest_signals_filtered(ds, bars_1min)
        if not day_sigs:
            continue
        coverage['signal_days']  += 1
        coverage['signal_count'] += len(day_sigs)

        for sig in day_sigs:
            right = 'C' if sig['direction'] == 'c' else 'P'
            real_strike = snap_to_real_strike(ds, sig['strike'], right)
            if real_strike is None:
                coverage['strike_unavailable'] += 1
                continue
            sig['strike'] = real_strike

            r = rfr(date.fromisoformat(ds))
            iv, iv_source = calibrate_iv_theta(ds, sig['entry_spy'], r)
            if iv is None:
                iv, iv_source = vix_daily.get(ds, 16.0) / 100.0, 'vix_fallback'
            sig['iv'], sig['iv_source'] = iv, iv_source
            signals.append(sig)

    print(f'  {len(signals)} signals across {coverage["signal_days"]} signal days -> '
          f'fetching real intraday quotes (cache-first, one call/signal covering entry->15:45)...')
    for n, sig in enumerate(signals, 1):
        right = 'C' if sig['direction'] == 'c' else 'P'
        chain_list = fetch_intraday_chain(
            sig['ds'], sig['strike'], right,
            sig['entry_dt'].strftime('%H:%M:%S'), '15:45:00')
        sig['chain'] = {r['hm']: r for r in chain_list} if chain_list else None
        if sig['chain']:
            coverage['intraday_fetch_ok'] += 1
        else:
            coverage['intraday_fetch_failed'] += 1
        if progress_every and n % progress_every == 0:
            print(f'    {n}/{len(signals)} fetched  '
                  f'(api_calls={THETA_API_CALLS}, api_time={THETA_API_SECONDS:.1f}s)')

    results = {}
    for sw in stop_widths:
        for rr in rr_targets:
            trades = []
            for sig in signals:
                t = _simulate_sr_trade_real(sig, sw, rr)
                if t is not None:
                    trades.append(t)
            results[(sw, rr)] = trades

    signal_dates = sorted({sig['ds'] for sig in signals})
    return results, coverage, signal_dates

def run_sr_retest_filtered_cli():
    print(f"\n{'='*70}\n  S/R Retest FILTERED (first-retest-per-level + no lunch signals) - "
          f"REAL intraday ThetaData quotes, stop x R:R grid\n{'='*70}\n")
    print("  Loading market data...")
    spy_daily = load_spy_daily()
    spy_1min  = load_spy_1min()
    vix_daily = load_vix()
    all_dates = sorted(spy_daily.keys())

    t0 = time.time()
    results, coverage, signal_dates = run_sr_retest_filtered_real(
        spy_1min, vix_daily, all_dates, SR_STOP_WIDTHS, SR_RR_TARGETS)
    elapsed = time.time() - t0

    out_path = BASE / 'trades_sr_retest_filtered.csv'
    write_sr_retest_csv(results, out_path)

    print(f"\n  Elapsed: {elapsed:.1f}s\n")
    print("  -- Coverage --")
    print(f"  Total candidate days:     {coverage['total_days']}")
    print(f"  Theta 0DTE usable:        {coverage['theta_usable']}")
    print(f"  Theta 0DTE skipped:       {coverage['theta_skipped']}")
    print(f"  Signal days:              {coverage['signal_days']}  (was 579 unfiltered)")
    print(f"  Signal instances:         {coverage['signal_count']}  (can exceed signal days - multiple levels/day allowed)")
    print(f"  Strike unavailable:       {coverage['strike_unavailable']}  (no real listed contract near that strike)")
    print(f"  Intraday fetch OK:        {coverage['intraday_fetch_ok']}")
    print(f"  Intraday fetch failed:    {coverage['intraday_fetch_failed']}")
    print(f"  ThetaData API calls:      {THETA_API_CALLS}  (total {THETA_API_SECONDS:.1f}s, "
          f"avg {THETA_API_SECONDS/max(THETA_API_CALLS,1)*1000:.0f}ms/call)")
    print()
    print("  -- Per-combo stats (real intraday fills) --")
    grid_stats = {}
    for sw in SR_STOP_WIDTHS:
        for rr in SR_RR_TARGETS:
            s = calc_stats(results[(sw, rr)])
            grid_stats[(sw, rr)] = s
            print(f"  stop=${sw:.2f}  rr={rr:.1f}  N={s['n']:>4}  WR={s['wr']:>5.1f}%  "
                  f"Total P&L=${s['total_pnl']:>+10,.2f}  PF={fmt_pf(s):>5}  "
                  f"AvgWin=${s['avg_win']:>7.2f}  AvgLoss=${s['avg_loss']:>8.2f}  MaxDD=${s['max_dd']:>9,.2f}")
    print(f"\n  Wrote sweep results -> {out_path}")

    finite = {k: v for k, v in grid_stats.items() if v['n'] > 0 and v['pf'] != float('inf')}
    pool   = finite if finite else grid_stats
    best_combo = max(pool, key=lambda k: (pool[k]['pf'], pool[k]['total_pnl']))
    print(f"\n  Best combo by PF (tiebreak total P&L): stop=${best_combo[0]:.2f} rr={best_combo[1]:.1f}  "
          f"{grid_stats[best_combo]}")

    n = len(signal_dates)
    split_idx = round(n * 0.70)
    is_dates  = set(signal_dates[:split_idx])
    oos_dates = set(signal_dates[split_idx:])
    best_trades = results[best_combo]
    is_trades   = [t for t in best_trades if t.date in is_dates]
    oos_trades  = [t for t in best_trades if t.date in oos_dates]
    s_is, s_oos = calc_stats(is_trades), calc_stats(oos_trades)
    print(f"\n  -- IS/OOS split on best combo (stop=${best_combo[0]:.2f} rr={best_combo[1]:.1f}) --")
    print(f"  {n} signal days total, 70% split = {split_idx}")
    if signal_dates:
        print(f"  IS:  {len(is_dates)} signal days ({signal_dates[0]} -> {signal_dates[max(split_idx-1,0)]})")
        print(f"  OOS: {len(oos_dates)} signal days ({signal_dates[min(split_idx,n-1)]} -> {signal_dates[-1]})")
    print(f"  IS  trades: N={s_is['n']:>4}  WR={s_is['wr']:>5.1f}%  Total P&L=${s_is['total_pnl']:>+10,.2f}  PF={fmt_pf(s_is):>5}")
    print(f"  OOS trades: N={s_oos['n']:>4}  WR={s_oos['wr']:>5.1f}%  Total P&L=${s_oos['total_pnl']:>+10,.2f}  PF={fmt_pf(s_oos):>5}")

    return results, coverage, grid_stats, best_combo, (s_is, s_oos)


def write_orb_poc_csv(trades: List[ORPocTrade], path: Path):
    """Columns date/entry_time/entry_price/stop/target/exit_price/exit_reason/pnl
    hold SPY UNDERLYING price levels (for overlaying OR/POC/entry/stop/target
    directly on a SPY candlestick chart), plus extra columns with the actual
    traded option premiums and calibration metadata for full traceability."""
    import csv as _csv
    with path.open('w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['date', 'entry_time', 'entry_price', 'stop', 'target', 'exit_price',
                    'exit_reason', 'pnl', 'direction', 'strike', 'poc', 'or_high', 'or_low',
                    'exit_time', 'option_entry_price', 'option_exit_price', 'iv', 'iv_source'])
        for t in trades:
            w.writerow([t.date, t.entry_time, t.entry_spy, t.stop_spy, t.target_spy, t.exit_spy,
                        t.exit_reason, t.pnl, t.direction, t.strike, t.poc, t.or_high, t.or_low,
                        t.exit_time, t.entry_option, t.exit_option, t.iv, t.iv_source])

def run_orb_poc_cli():
    print(f"\n{'='*70}\n  R7-POC-RR3 - OR + Point of Control, fixed 3:1 R:R\n{'='*70}\n")
    print("  Loading market data...")
    vix_daily = load_vix()
    spy_daily = load_spy_daily()
    spy_1min  = load_spy_1min()
    spy_5min  = load_spy_5min()
    all_dates = sorted(spy_daily.keys())
    print(f"  {len(all_dates)} candidate trading days, {len(spy_1min)} days with 1-min bars\n")

    t0 = time.time()
    trades, coverage = run_r7_or_poc_rr3(spy_1min, spy_5min, spy_daily, vix_daily, all_dates)
    elapsed = time.time() - t0

    out_path = BASE / 'trades_rr3.0.csv'
    write_orb_poc_csv(trades, out_path)

    s = calc_stats(trades)
    print(f"  Elapsed: {elapsed:.1f}s\n")
    print("  -- Theta cache coverage --")
    print(f"  Total candidate days:     {coverage['total_days']}")
    print(f"  Theta 0DTE usable:        {coverage['theta_usable']}")
    print(f"  Theta 0DTE skipped:       {coverage['theta_skipped']}  (missing/empty/unreadable theta_SPY_{{date}}.pkl)")
    print(f"  IV theta-calibrated:      {coverage['iv_theta_calibrated']}")
    print(f"  IV vix/100 fallback:      {coverage['iv_vix_fallback']}  (no cached 3-15 DTE chain for that day)")
    print(f"  Usable days with signal:  {coverage['signal_days']}")
    print()
    print("  -- Trade stats --")
    print(f"  N={s['n']}  WR={s['wr']:.1f}%  Total P&L=${s['total_pnl']:+,.2f}  "
          f"PF={fmt_pf(s)}  AvgWin=${s['avg_win']:.2f}  AvgLoss=${s['avg_loss']:.2f}  MaxDD=${s['max_dd']:.2f}")
    print(f"\n  Wrote {len(trades)} trades -> {out_path}")
    return trades, coverage, s


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
    if '--sr-retest-filtered' in sys.argv:
        run_sr_retest_filtered_cli()
    elif '--sr-retest' in sys.argv:
        run_sr_retest_cli()
    elif '--vwap-reversion' in sys.argv:
        run_vwap_reversion_cli()
    elif '--orb-poc-real' in sys.argv:
        run_orb_poc_real_cli()
    elif '--orb-poc' in sys.argv:
        run_orb_poc_cli()
    else:
        main()
