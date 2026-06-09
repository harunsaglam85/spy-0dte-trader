#!/usr/bin/env python3
"""
hermes_research_engine.py
=========================
Autonomous overnight strategy research engine.
Runs a queue of hypothesis-backed backtests, grades each against kill criteria,
logs everything to /root/spy-0dte-trader/hermes_research/journal.md, and sends
a Telegram update every 5 strategies.

Kill criteria
─────────────
  • OOS Sharpe < 1.0
  • OOS WR < breakeven for strategy type
  • >60% profits from a single year
  • fewer than 20 OOS trades

Baseline to beat
────────────────
  • Credit Spread OOS WR 80%, P&L +$3,639
  • Earnings Momentum P&L +$53,106
"""
# ─── stdlib ───────────────────────────────────────────────────────────────────
import csv, json, math, os, pickle, random, sys, time, warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

warnings.filterwarnings('ignore')

# ─── third-party ──────────────────────────────────────────────────────────────
import numpy as np
import pytz
import yfinance as yf
from scipy.stats import norm
from scipy.optimize import brentq
from py_vollib.black_scholes import black_scholes as _pv_bs
from py_vollib.black_scholes.implied_volatility import implied_volatility as _pv_iv
from py_vollib.black_scholes.greeks.analytical import delta as _pv_delta

try:
    import requests as _req
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ─── paths ────────────────────────────────────────────────────────────────────
BASE        = Path('/root/spy-0dte-trader')
DATA_DIR    = BASE / 'backtest_data'
RESEARCH    = BASE / 'hermes_research'
JOURNAL     = RESEARCH / 'journal.md'
RESULTS_DIR = RESEARCH / 'results'
RESEARCH.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# ─── env ──────────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    p = BASE / '.env'
    if p.exists():
        for line in p.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env

ENV = _load_env()
TG_TOKEN = ENV.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT  = ENV.get('TELEGRAM_CHAT_ID', '')

# ─── telegram ─────────────────────────────────────────────────────────────────
def tg(msg: str):
    if not REQUESTS_OK or not TG_TOKEN or not TG_CHAT:
        print(f'[TG] {msg[:120]}')
        return
    try:
        _req.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10
        )
    except Exception as e:
        print(f'[TG ERROR] {e}')

# ─── constants ────────────────────────────────────────────────────────────────
ET              = pytz.timezone('America/New_York')
BT_START        = date(2021, 1, 4)
BT_END          = date(2026, 5, 30)
SPLIT_DATE      = date(2023, 7, 1)   # in-sample / out-of-sample split
BLIND_YEAR      = 2025
TRADING_DAYS_YR = 252
TRADING_MINS    = TRADING_DAYS_YR * 390

_RFR = {2021:0.001, 2022:0.015, 2023:0.045, 2024:0.050, 2025:0.045, 2026:0.043}
def rfr(dt) -> float:
    return _RFR.get(dt.year if hasattr(dt,'year') else int(str(dt)[:4]), 0.045)

# ─── data loaders (with disk cache) ───────────────────────────────────────────
def load_vix() -> dict:
    p = DATA_DIR / 'vix_2021-01-04_2026-05-30.csv'
    out = {}
    with p.open() as f:
        for row in csv.DictReader(f):
            out[row['date']] = float(row['vix'])
    return out

def load_spy_daily() -> dict:
    p = DATA_DIR / 'spy_daily_2021-01-04_2026-05-30.pkl'
    with p.open('rb') as f:
        return pickle.load(f)

def load_spy_5min() -> dict:
    p = DATA_DIR / 'spy_5min_2021-01-04_2026-05-30.pkl'
    with p.open('rb') as f:
        return pickle.load(f)

def load_theta_chain(exp: date) -> dict:
    p = DATA_DIR / f'theta_SPY_{exp}.pkl'
    if p.exists():
        with p.open('rb') as f:
            return pickle.load(f)
    return {}

def get_theta_expirations() -> List[date]:
    exps = []
    for p in sorted(DATA_DIR.glob('theta_SPY_????-??-??.pkl')):
        try:
            exps.append(date.fromisoformat(p.stem[len('theta_SPY_'):]))
        except ValueError:
            pass
    return exps

def load_stock_daily(ticker: str) -> dict:
    p = DATA_DIR / f'{ticker}_daily_2021-01-04_2026-05-30.pkl'
    if p.exists():
        with p.open('rb') as f:
            return pickle.load(f)
    return {}

# ─── math helpers ─────────────────────────────────────────────────────────────
def bs_price(S, K, T, r, sigma, flag) -> float:
    if T <= 1e-7 or sigma <= 1e-6:
        return max(S-K,0) if flag=='c' else max(K-S,0)
    try:
        return float(_pv_bs(flag, S, K, T, r, sigma))
    except Exception:
        d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
        d2 = d1 - sigma*math.sqrt(T)
        N  = lambda x: 0.5*(1+math.erf(x/math.sqrt(2)))
        if flag == 'c': return S*N(d1)-K*math.exp(-r*T)*N(d2)
        return K*math.exp(-r*T)*N(-d2)-S*N(-d1)

def bs_delta(S, K, T, r, sigma, flag) -> float:
    if T <= 1e-7 or sigma <= 1e-6:
        return (1.0 if S>K else 0.0) if flag=='c' else (-1.0 if S<K else 0.0)
    try:
        return float(_pv_delta(flag, S, K, T, r, sigma))
    except Exception:
        d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
        N  = lambda x: 0.5*(1+math.erf(x/math.sqrt(2)))
        return N(d1) if flag=='c' else N(d1)-1.0

def implied_vol(price, S, K, T, r, flag) -> Optional[float]:
    if T <= 0 or price <= 0: return None
    try:
        iv = _pv_iv(price, S, K, T, r, flag)
        if 0.01 <= iv <= 5.0: return iv
    except Exception:
        pass
    try:
        f  = lambda s: bs_price(S, K, T, r, s, flag) - price
        lo, hi = 0.001, 5.0
        if f(lo)*f(hi) > 0: return None
        iv = brentq(f, lo, hi, xtol=1e-4)
        return iv if 0.01 <= iv <= 5.0 else None
    except Exception:
        return None

def iv_rank(vix_daily: dict, ds: str, lb: int = 252) -> float:
    dates = sorted(vix_daily.keys())
    try: idx = dates.index(ds)
    except ValueError: return 50.0
    window = [vix_daily[d] for d in dates[max(0,idx-lb):idx+1]]
    if len(window) < 2: return 50.0
    lo, hi = min(window), max(window)
    if hi == lo: return 50.0
    return (vix_daily.get(ds, lo) - lo) / (hi - lo) * 100.0

def vwap(bars: list) -> float:
    tv = sum(b['v']*(b['h']+b['l']+b['c'])/3.0 for b in bars if b['v']>0)
    v  = sum(b['v'] for b in bars if b['v']>0)
    return tv/v if v>0 else (bars[-1]['c'] if bars else 0.0)

def ma(data: list, n: int) -> Optional[float]:
    if len(data) < n: return None
    return sum(data[-n:]) / n

def rsi(bars: list, period: int = 14) -> float:
    closes = [b['c'] for b in bars]
    if len(closes) < period+1: return 50.0
    deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d,0) for d in deltas[-period:]]
    losses = [abs(min(d,0)) for d in deltas[-period:]]
    ag, al = sum(gains)/period, sum(losses)/period
    if al == 0: return 100.0
    return 100.0 - 100.0/(1.0 + ag/al)

def atr(spy_daily: dict, ds: str, n: int = 14) -> float:
    days  = sorted(spy_daily.keys())
    try: idx = days.index(ds)
    except ValueError: return 2.0
    window = days[max(0,idx-n):idx+1]
    trs = []
    for i in range(1, len(window)):
        d, p = window[i], window[i-1]
        h = spy_daily[d]['high']; l = spy_daily[d]['low']
        pc = spy_daily[p]['close']
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs)/len(trs) if trs else 2.0

def find_exp(obs: date, min_dte: int, max_dte: int, exps: List[date]) -> Optional[date]:
    for e in exps:
        dte = (e - obs).days
        if min_dte <= dte <= max_dte:
            return e
    return None

def prev_trading_day(ds: str, spy_daily: dict) -> Optional[str]:
    d = date.fromisoformat(ds) - timedelta(days=1)
    for _ in range(10):
        if d.isoformat() in spy_daily:
            return d.isoformat()
        d -= timedelta(days=1)
    return None

def load_ma50(spy_daily: dict) -> dict:
    dates  = sorted(spy_daily.keys())
    closes = [spy_daily[d]['close'] for d in dates]
    result = {}
    for i, ds in enumerate(dates):
        result[ds] = sum(closes[i-49:i+1])/50.0 if i >= 49 else None
    return result

# ─── statistics ───────────────────────────────────────────────────────────────
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

def calc_stats(trades: List[Trade]) -> dict:
    if not trades:
        return {'n':0,'wr':0.0,'total_pnl':0.0,'pf':0.0,'avg_win':0.0,'avg_loss':0.0,
                'expectancy':0.0,'max_dd':0.0,'sharpe':0.0,'sortino':0.0}
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gw     = sum(t.pnl for t in wins)
    gl     = sum(t.pnl for t in losses)
    wr     = len(wins)/len(trades)
    avg_w  = gw/len(wins)  if wins   else 0.0
    avg_l  = gl/len(losses) if losses else 0.0
    equity = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_date):
        equity += t.pnl
        peak    = max(peak, equity)
        max_dd  = max(max_dd, peak-equity)
    by_day = defaultdict(float)
    for t in trades:
        by_day[t.entry_date] += t.pnl
    daily = list(by_day.values())
    if len(daily) > 1:
        mu, sig = np.mean(daily), np.std(daily, ddof=1)
        sharpe  = (mu/sig*math.sqrt(252)) if sig > 0 else 0.0
        negs    = [p for p in daily if p < 0]
        sortino = (mu/np.std(negs)*math.sqrt(252)) if len(negs)>1 and np.std(negs)>0 else 0.0
    else:
        sharpe = sortino = 0.0
    pf = abs(gw/gl) if gl < 0 else float('inf')
    return {
        'n': len(trades), 'wr': wr*100, 'total_pnl': sum(t.pnl for t in trades),
        'pf': pf, 'avg_win': avg_w, 'avg_loss': avg_l,
        'expectancy': wr*avg_w+(1-wr)*avg_l,
        'max_dd': max_dd, 'sharpe': sharpe, 'sortino': sortino,
    }

def bootstrap_p(trades: List[Trade], n: int = 2000) -> float:
    if not trades: return 0.0
    pnls = [t.pnl for t in trades]
    return sum(1 for _ in range(n) if sum(random.choices(pnls,k=len(pnls)))>0)/n*100

def year_concentration(trades: List[Trade]) -> float:
    """Returns fraction of total P&L from the single best year."""
    if not trades: return 0.0
    total = sum(t.pnl for t in trades)
    if total <= 0: return 1.0
    by_year = defaultdict(float)
    for t in trades:
        by_year[t.entry_date[:4]] += t.pnl
    best_yr = max(by_year.values())
    return best_yr / total

def kill_check(name: str, is_trades: List[Trade], oos_trades: List[Trade],
               breakeven_wr: float = 50.0) -> Tuple[bool, str]:
    """Returns (is_dead, reason). breakeven_wr: minimum WR to be viable."""
    oos = calc_stats(oos_trades)
    if oos['n'] < 20:
        return True, f'OOS N={oos["n"]} < 20 (insufficient trades)'
    if oos['sharpe'] < 1.0:
        return True, f'OOS Sharpe={oos["sharpe"]:.2f} < 1.0'
    if oos['wr'] < breakeven_wr:
        return True, f'OOS WR={oos["wr"]:.1f}% < breakeven {breakeven_wr:.0f}%'
    conc = year_concentration(oos_trades)
    if conc > 0.60:
        by_year = defaultdict(float)
        for t in oos_trades: by_year[t.entry_date[:4]] += t.pnl
        best_yr = max(by_year, key=by_year.get)
        return True, f'Year concentration: {conc*100:.0f}% of OOS P&L from {best_yr}'
    return False, 'PASSES all kill criteria'

# ─── FOMC / CPI date sets ─────────────────────────────────────────────────────
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

# ─── STRATEGY RUNNERS ─────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# H1 — Pre-FOMC Drift: buy 0DTE ATM calls on FOMC morning, exit by 1:45 PM
# Hypothesis: SPY drifts +0.3–0.8% in the 3h before FOMC decisions (Lucca-Moench)
# ══════════════════════════════════════════════════════════════════════════════
def run_h1_prefomc_drift(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    ma50   = load_ma50(spy_daily)
    trades = []
    for fomc_dt in sorted(FOMC_DATES):
        ds = fomc_dt.isoformat()
        if ds not in spy_daily: continue

        # MA50 filter: SPY above rising MA50
        spy_px = spy_daily[ds]['close']
        m50    = ma50.get(ds)
        if not m50 or spy_px <= m50: continue
        prev_ds = prev_trading_day(ds, spy_daily)
        if prev_ds and ma50.get(prev_ds) and ma50[ds] <= ma50[prev_ds]: continue

        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue

        vix = vix_daily.get(ds, 16.0)

        # Entry: first bar at/after 10:00 AM
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and dt_b.minute < 30:
                entry_bar = bar; break
        if not entry_bar: continue

        dt_entry = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e    = entry_bar['c']
        strike   = round(spy_e / 0.5) * 0.5  # nearest $0.50

        # Find same-day expiration
        exp = fomc_dt if fomc_dt in exps else find_exp(fomc_dt, 0, 1, exps)
        if exp is None: continue

        T_entry  = max((16*60 - (dt_entry.hour*60+dt_entry.minute)), 1) / TRADING_MINS
        r        = rfr(fomc_dt)
        iv       = vix_daily.get(ds, 16.0) / 100.0
        entry_ask = round(bs_price(spy_e, strike, T_entry, r, iv, 'c') * 1.07, 4)
        if entry_ask <= 0 or entry_ask * 100 > 1500: continue

        # Exit: hard stop at 1:45 PM; take profit target 35%
        target    = entry_ask * 1.35
        stop_pct  = entry_ask * 0.70
        exit_bar  = None
        exit_rsn  = 'time-1:45PM'

        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (13, 45):
                exit_bar = bar; exit_rsn = '1:45PM hard stop'; break
            ml   = max(16*60 - (dt_b.hour*60+dt_b.minute), 1)
            cur  = bs_price(bar['c'], strike, ml/TRADING_MINS, r, iv, 'c') * 1.035
            if cur >= target:
                exit_bar = bar; exit_rsn = '35% target'; break
            if cur <= stop_pct:
                exit_bar = bar; exit_rsn = '30% stop'; break

        if exit_bar is None:
            exit_bar = bars[-1]; exit_rsn = 'EOD'

        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid = bs_price(exit_bar['c'], strike, ml_exit/TRADING_MINS, r, iv, 'c') * 0.965
        pnl      = round((exit_bid - entry_ask) * 100, 2)

        trades.append(Trade('H1_PreFOMC_Drift', ds, ds, ds, entry_ask, exit_bid, pnl, vix, exit_rsn))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H2 — VIX Spike Reversal Iron Butterfly (0DTE)
# Hypothesis: On day-1 of VIX reversal (IVR>70, VIX ticking down), 
#             IV crush = oversized premium decay. Sell $3-wide iron butterfly.
# ══════════════════════════════════════════════════════════════════════════════
def run_h2_vix_reversal_butterfly(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades  = []
    d_list  = sorted(vix_daily.keys())

    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if vix < 22.0: continue  # must be genuinely elevated

        ivr    = iv_rank(vix_daily, ds)
        if ivr < 70.0: continue  # spike-level IVR

        # VIX must be turning DOWN (today < yesterday)
        try: idx = d_list.index(ds)
        except ValueError: continue
        if idx < 2: continue
        vix_yest      = vix_daily.get(d_list[idx-1], vix)
        vix_two_ago   = vix_daily.get(d_list[idx-2], vix)
        if vix >= vix_yest: continue             # not turning down
        if vix_yest <= vix_two_ago: continue     # yesterday not a local peak

        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue

        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue

        # Entry: 10:15 AM bar
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

        # $3-wide iron butterfly: ATM short straddle + $3 wings
        K_atm     = round(spy_e / 0.5) * 0.5
        K_call_w  = K_atm + 3.0
        K_put_w   = K_atm - 3.0

        sc_bid  = bs_price(spy_e, K_atm,   T_e, r, iv, 'c') * 0.965
        sp_bid  = bs_price(spy_e, K_atm,   T_e, r, iv, 'p') * 0.965
        lc_ask  = bs_price(spy_e, K_call_w,T_e, r, iv, 'c') * 1.035
        lp_ask  = bs_price(spy_e, K_put_w, T_e, r, iv, 'p') * 1.035

        credit  = round((sc_bid + sp_bid) - (lc_ask + lp_ask), 4)
        if credit < 0.20: continue  # need meaningful premium

        target_exit = credit * 0.50   # 50% profit
        stop_exit   = credit * 2.0    # 2x stop
        exit_bar    = None
        exit_rsn    = 'EOD'

        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 30):
                exit_bar = bar; exit_rsn = '3:30PM force'; break
            ml    = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            T_cur = ml / TRADING_MINS
            sp_n  = bar['c']
            sc_a  = bs_price(sp_n, K_atm,   T_cur, r, iv, 'c') * 1.035
            sp_a  = bs_price(sp_n, K_atm,   T_cur, r, iv, 'p') * 1.035
            lc_b  = bs_price(sp_n, K_call_w,T_cur, r, iv, 'c') * 0.965
            lp_b  = bs_price(sp_n, K_put_w, T_cur, r, iv, 'p') * 0.965
            cur_debit = max((sc_a + sp_a) - (lc_b + lp_b), 0)
            if cur_debit <= target_exit:
                exit_bar = bar; exit_rsn = '50% target'; break
            if cur_debit >= stop_exit:
                exit_bar = bar; exit_rsn = '2x stop'; break

        if exit_bar is None: exit_bar = bars[-1]

        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        T_ex     = ml_exit / TRADING_MINS
        sp_x     = exit_bar['c']
        sc_ax    = bs_price(sp_x, K_atm,   T_ex, r, iv, 'c') * 1.035
        sp_ax    = bs_price(sp_x, K_atm,   T_ex, r, iv, 'p') * 1.035
        lc_bx    = bs_price(sp_x, K_call_w,T_ex, r, iv, 'c') * 0.965
        lp_bx    = bs_price(sp_x, K_put_w, T_ex, r, iv, 'p') * 0.965
        exit_debit = max((sc_ax+sp_ax)-(lc_bx+lp_bx), 0)
        pnl      = round((credit - exit_debit) * 100, 2)

        trades.append(Trade('H2_VIXReversal_Butterfly', ds, ds, ds, credit, exit_debit, pnl, vix, exit_rsn))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H3 — Monday Gap-Fill: SPY opens down Mon morning, buy 0DTE ATM call
# Hypothesis: Monday gap-downs are mean-reverting 65%+ of the time
#             (weekend theta drain + institutional rebalancing)
# ══════════════════════════════════════════════════════════════════════════════
def run_h3_monday_gapfill(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades  = []
    d_list  = sorted(spy_daily.keys())

    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj.weekday() != 0: continue  # Monday only

        vix = vix_daily.get(ds, 16.0)
        if vix > 25: continue  # avoid panic-Monday entries

        # Need prior Friday close
        try: idx = d_list.index(ds)
        except ValueError: continue
        if idx < 1: continue
        prev_ds  = d_list[idx-1]
        prev_cls = spy_daily[prev_ds]['close']
        today_o  = spy_daily[ds].get('open', 0)
        if today_o <= 0: continue

        gap_pct = (today_o - prev_cls) / prev_cls * 100
        if gap_pct > -0.3: continue   # need a meaningful gap-down (at least -0.3%)
        if gap_pct < -2.0: continue   # avoid crash opens

        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue

        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue

        # Entry: 9:45 AM (after initial volatility settles)
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if (dt_b.hour, dt_b.minute) >= (9, 45) and dt_b.hour < 10:
                entry_bar = bar; break
        if entry_bar is None: continue

        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']

        # Only enter if SPY still below gap (not already filled)
        if spy_e >= prev_cls: continue

        strike    = round(spy_e / 0.5) * 0.5
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS

        entry_ask = round(bs_price(spy_e, strike, T_e, r, iv, 'c') * 1.07, 4)
        if entry_ask <= 0 or entry_ask * 100 > 800: continue

        # Target: gap fill (SPY returns to prior close) OR 11:00 AM time stop
        target_spy  = prev_cls           # gap-fill price
        time_stop   = (11, 30)           # exit by 11:30 AM
        stop_pct    = entry_ask * 0.60   # 40% stop

        exit_bar = None; exit_rsn = 'time'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= time_stop:
                exit_bar = bar; exit_rsn = f'{time_stop[0]}:{time_stop[1]:02d} time stop'; break
            ml    = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            T_cur = ml / TRADING_MINS
            cur   = bs_price(bar['c'], strike, T_cur, r, iv, 'c') * 1.035
            if bar['c'] >= target_spy:
                exit_bar = bar; exit_rsn = 'gap filled'; break
            if cur <= stop_pct:
                exit_bar = bar; exit_rsn = '40% stop'; break

        if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'

        dt_exit   = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit   = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid  = bs_price(exit_bar['c'], strike, ml_exit/TRADING_MINS, r, iv, 'c') * 0.965
        pnl       = round((exit_bid - entry_ask) * 100, 2)

        trades.append(Trade('H3_Monday_GapFill', ds, ds, ds, entry_ask, exit_bid, pnl, vix, exit_rsn))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H4 — Overnight Gap Momentum: SPY gaps up >0.5% at open, buy 0DTE call
# Hypothesis: Strong overnight gaps (futures driven) continue intraday
#             because institutional positioning follows the gap direction
# ══════════════════════════════════════════════════════════════════════════════
def run_h4_gap_momentum(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades  = []
    d_list  = sorted(spy_daily.keys())

    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if vix > 22 or vix < 12: continue

        try: idx = d_list.index(ds)
        except ValueError: continue
        if idx < 1: continue

        prev_cls = spy_daily[d_list[idx-1]]['close']
        today_o  = spy_daily[ds].get('open', 0)
        if today_o <= 0: continue

        gap_pct = (today_o - prev_cls) / prev_cls * 100
        if gap_pct < 0.5: continue   # need strong gap up
        if gap_pct > 2.5: continue   # avoid parabolic opens

        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue

        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue

        # Entry: 9:45 AM — let first 15 mins settle
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if (dt_b.hour, dt_b.minute) >= (9, 45) and dt_b.hour < 10:
                entry_bar = bar; break
        if entry_bar is None: continue

        # Confirmation: SPY still above gap open (not fading)
        spy_e = entry_bar['c']
        if spy_e < today_o * 0.999: continue  # already fading

        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        strike    = round(spy_e / 0.5) * 0.5
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS

        entry_ask = round(bs_price(spy_e, strike, T_e, r, iv, 'c') * 1.07, 4)
        if entry_ask <= 0 or entry_ask * 100 > 1200: continue

        target  = entry_ask * 1.40   # 40% profit
        stop    = entry_ask * 0.65   # 35% stop
        cutoff  = (12, 30)           # exit by 12:30 — momentum fades midday

        exit_bar = None; exit_rsn = 'time'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= cutoff:
                exit_bar = bar; exit_rsn = '12:30 cutoff'; break
            ml    = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            T_cur = ml / TRADING_MINS
            cur   = bs_price(bar['c'], strike, T_cur, r, iv, 'c') * 1.035
            if cur >= target:
                exit_bar = bar; exit_rsn = '40% target'; break
            if cur <= stop:
                exit_bar = bar; exit_rsn = '35% stop'; break

        if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'

        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid = bs_price(exit_bar['c'], strike, ml_exit/TRADING_MINS, r, iv, 'c') * 0.965
        pnl      = round((exit_bid - entry_ask) * 100, 2)

        trades.append(Trade('H4_Gap_Momentum', ds, ds, ds, entry_ask, exit_bid, pnl, vix, exit_rsn))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H5 — CPI Hot-Print Put Spread (IMPROVED S3)
# Hypothesis: Hot CPI prints cause sustained SPY drops.
#             Buy 2DTE put spread (not naked put) for better risk/reward.
# ══════════════════════════════════════════════════════════════════════════════
HOT_CPI_MONTHS = {
    '2021-03','2021-04','2021-05','2021-06','2021-07','2021-08',
    '2021-09','2021-10','2021-11','2021-12',
    '2022-01','2022-02','2022-03','2022-04','2022-05','2022-06',
    '2022-07','2022-08','2022-09',
    '2023-01','2023-02',
    '2024-03','2024-04',
    '2025-03','2025-04',
}

def run_h5_cpi_put_spread(spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades  = []
    d_list  = sorted(spy_daily.keys())

    for cpi_dt in sorted(CPI_DATES):
        month_key = cpi_dt.strftime('%Y-%m')
        if month_key not in HOT_CPI_MONTHS: continue

        # Entry: 2 trading days before CPI
        try: idx = next(i for i,d in enumerate(d_list) if date.fromisoformat(d) >= cpi_dt)
        except StopIteration: continue
        if idx < 2: continue
        entry_ds = d_list[idx-2]
        if entry_ds not in set(dates): continue

        entry_dt = date.fromisoformat(entry_ds)
        spy_px   = spy_daily.get(entry_ds, {}).get('close', 0.0)
        if not spy_px: continue

        exit_ds = d_list[idx+1] if idx+1 < len(d_list) else None
        if not exit_ds: continue
        exit_dt = date.fromisoformat(exit_ds)

        exp = find_exp(entry_dt, 2, 8, exps)
        if exp is None: continue

        vix   = vix_daily.get(entry_ds, 16.0)
        r     = rfr(entry_dt)
        iv    = vix / 100.0
        T_e   = max((exp - entry_dt).days / 365.0, 0.0001)

        # Put spread: buy ATM put, sell put 3 points lower
        short_k   = round(spy_px / 0.5) * 0.5
        long_k    = short_k - 3.0  # buy lower put (this is the long put / protection)
        # We want a DEBIT put spread: buy ATM put, sell lower put
        long_ask  = round(bs_price(spy_px, short_k, T_e, r, iv, 'p') * 1.04, 4)
        short_bid = round(bs_price(spy_px, long_k,  T_e, r, iv, 'p') * 0.96, 4)
        debit     = round(long_ask - short_bid, 4)
        if debit <= 0 or debit * 100 > 400: continue

        T_x      = max((exp - exit_dt).days / 365.0, 0.0001)
        spy_exit = spy_daily.get(exit_ds, {}).get('close', spy_px)
        long_bid  = round(bs_price(spy_exit, short_k, T_x, r, iv, 'p') * 0.96, 4)
        short_ask = round(bs_price(spy_exit, long_k,  T_x, r, iv, 'p') * 1.04, 4)
        exit_val  = max(long_bid - short_ask, 0)
        pnl       = round((exit_val - debit) * 100, 2)

        trades.append(Trade('H5_CPI_PutSpread', entry_ds, entry_ds, exit_ds,
                            debit, exit_val, pnl, vix,
                            f'{month_key} hot CPI'))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H6 — Friday PM Premium Decay: sell 0DTE put spread on Friday 1 PM
# Hypothesis: Friday PM has accelerated theta decay + low realized vol
#             (market makers reducing books). Selling at 1 PM with 3h left
#             captures maximum theta/gamma ratio.
# ══════════════════════════════════════════════════════════════════════════════
def run_h6_friday_pm_decay(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades = []
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj.weekday() != 4: continue  # Friday only

        vix = vix_daily.get(ds, 16.0)
        if not (14 <= vix <= 22): continue  # VIX-scaled: slightly wider

        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue

        bars = spy_5min.get(ds, [])
        if len(bars) < 20: continue

        # Entry: 1:00–1:15 PM bar
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 13 and dt_b.minute < 15:
                entry_bar = bar; break
        if entry_bar is None: continue

        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS

        # Dynamic min credit scaled to VIX
        min_credit = max(0.10, vix * 0.008)

        # Find 0.18-delta put (slightly OTM)
        best_k = spy_e * 0.982  # approx 0.18-delta at VIX=18
        for k_test in [spy_e*(1-i*0.002) for i in range(5, 20)]:
            d = abs(bs_delta(spy_e, k_test, T_e, r, iv, 'p'))
            if abs(d - 0.18) < 0.04:
                best_k = k_test; break
        short_k = round(best_k / 0.5) * 0.5
        long_k  = short_k - 2.0

        sc_bid  = bs_price(spy_e, short_k, T_e, r, iv, 'p') * 0.965
        lp_ask  = bs_price(spy_e, long_k,  T_e, r, iv, 'p') * 1.035
        credit  = round(sc_bid - lp_ask, 4)
        if credit < min_credit: continue

        target_exit = credit * 0.30  # 70% profit keep
        stop_exit   = credit * 2.00
        exit_bar    = None; exit_rsn = 'EOD'

        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 30):
                exit_bar = bar; exit_rsn = '3:30 force'; break
            ml    = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            T_cur = ml / TRADING_MINS
            sp_n  = bar['c']
            sc_a  = bs_price(sp_n, short_k, T_cur, r, iv, 'p') * 1.035
            lp_b  = bs_price(sp_n, long_k,  T_cur, r, iv, 'p') * 0.965
            cur   = max(sc_a - lp_b, 0)
            if cur <= target_exit:
                exit_bar = bar; exit_rsn = '70% target'; break
            if cur >= stop_exit:
                exit_bar = bar; exit_rsn = '2x stop'; break

        if exit_bar is None: exit_bar = bars[-1]

        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        T_ex     = ml_exit / TRADING_MINS
        sp_x     = exit_bar['c']
        sc_ax    = bs_price(sp_x, short_k, T_ex, r, iv, 'p') * 1.035
        lp_bx    = bs_price(sp_x, long_k,  T_ex, r, iv, 'p') * 0.965
        exit_debit = max(sc_ax - lp_bx, 0)
        pnl      = round((credit - exit_debit) * 100, 2)

        trades.append(Trade('H6_Friday_PM_Decay', ds, ds, ds, credit, exit_debit, pnl, vix, exit_rsn))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H7 — Post-Earnings Volatility Collapse
# Hypothesis: After earnings, IV drops sharply for 2–3 days.
#             Sell credit spreads on individual stocks DAY AFTER earnings.
# ══════════════════════════════════════════════════════════════════════════════
EARNINGS_UNIVERSE = ['NVDA','TSLA','AMD','AAPL','MSFT','META','GOOGL','AMZN']

def run_h7_postearnings_iv_collapse(spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades   = []
    d_list   = sorted(spy_daily.keys())
    date_set = set(dates)

    for ticker in EARNINGS_UNIVERSE:
        earn_cache = DATA_DIR / f'earnings_{ticker}_2021_2026.pkl'
        if not earn_cache.exists(): continue
        with earn_cache.open('rb') as f:
            earn_dates = pickle.load(f)

        stock_daily = load_stock_daily(ticker)
        if not stock_daily: continue

        for earn_dt in earn_dates:
            # Entry: DAY AFTER earnings
            try:
                idx = next(i for i,d in enumerate(d_list) if date.fromisoformat(d) >= earn_dt)
            except StopIteration:
                continue
            if idx+1 >= len(d_list): continue
            entry_ds = d_list[idx+1]
            if entry_ds not in date_set: continue

            entry_dt = date.fromisoformat(entry_ds)
            stock_px = stock_daily.get(entry_ds, {}).get('close', 0.0)
            if not stock_px: continue

            # Exit: 3 trading days later
            if idx+4 >= len(d_list): continue
            exit_ds  = d_list[idx+4]
            exit_dt  = date.fromisoformat(exit_ds)

            exp = find_exp(entry_dt, 3, 10, exps)
            if exp is None: continue

            vix = vix_daily.get(entry_ds, 16.0)
            r   = rfr(entry_dt)
            # Post-earnings IV is elevated — use 1.8x VIX as proxy
            iv  = vix / 100.0 * 1.8
            T_e = max((exp - entry_dt).days / 365.0, 0.0001)
            T_x = max((exp - exit_dt).days  / 365.0, 0.0001)

            # Sell put spread: sell ATM put, buy 2% OTM put
            short_k = round(stock_px / 0.5) * 0.5
            long_k  = round(stock_px * 0.98 / 0.5) * 0.5

            sc_bid  = bs_price(stock_px, short_k, T_e, r, iv, 'p') * 0.96
            lp_ask  = bs_price(stock_px, long_k,  T_e, r, iv, 'p') * 1.04
            credit  = round(sc_bid - lp_ask, 4)
            if credit < 0.15: continue

            # At exit: IV typically collapses to 1.2x VIX (earnings premium gone)
            iv_exit = vix / 100.0 * 1.2
            stock_exit_px = stock_daily.get(exit_ds, {}).get('close', stock_px)
            sc_ax   = bs_price(stock_exit_px, short_k, T_x, r, iv_exit, 'p') * 1.04
            lp_bx   = bs_price(stock_exit_px, long_k,  T_x, r, iv_exit, 'p') * 0.96
            exit_debit = max(sc_ax - lp_bx, 0)
            pnl     = round((credit - exit_debit) * 100, 2)

            trades.append(Trade('H7_PostEarnings_IVCollapse', entry_ds, entry_ds, exit_ds,
                                credit, exit_debit, pnl, vix, f'{ticker} post-earnings'))
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H8 — MA50 Bounce: SPY pulls back to MA50 on elevated VIX, buy calls
# Hypothesis: SPY MA50 is the dominant institutional support level.
#             Touching MA50 with VIX>16 (fear spike) = high-conviction entry.
# ══════════════════════════════════════════════════════════════════════════════
def run_h8_ma50_bounce(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades  = []
    ma50_d  = load_ma50(spy_daily)
    d_list  = sorted(spy_daily.keys())

    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if vix < 16 or vix > 30: continue

        spy_px = spy_daily[ds]['close']
        m50    = ma50_d.get(ds)
        if not m50: continue

        # Previous day SPY was at or below MA50, today bouncing
        try: idx = d_list.index(ds)
        except ValueError: continue
        if idx < 1: continue
        prev_ds  = d_list[idx-1]
        prev_px  = spy_daily[prev_ds]['close']
        prev_m50 = ma50_d.get(prev_ds, m50)

        # Bounce: previous close was within 0.5% of MA50, today recovering
        if not (0.995 * prev_m50 <= prev_px <= 1.005 * prev_m50): continue
        if spy_px <= prev_px: continue  # need to be moving up today

        bars = spy_5min.get(ds, [])
        if len(bars) < 6: continue

        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 3, exps)
        if exp is None: continue

        # Entry: 10:00–10:30 AM
        entry_bar = None
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b.hour == 10 and dt_b.minute < 30:
                entry_bar = bar; break
        if entry_bar is None: continue

        dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
        spy_e     = entry_bar['c']
        if spy_e <= m50 * 0.998: continue  # not confirmed bounce yet

        strike    = round(spy_e / 0.5) * 0.5
        r         = rfr(dt_obj)
        iv        = vix / 100.0
        mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
        T_e       = mins_left / TRADING_MINS

        entry_ask = round(bs_price(spy_e, strike, T_e, r, iv, 'c') * 1.07, 4)
        if entry_ask <= 0 or entry_ask * 100 > 1500: continue

        target  = entry_ask * 1.50  # 50% gain
        stop    = entry_ask * 0.60  # 40% stop
        cutoff  = (14, 0)

        exit_bar = None; exit_rsn = 'time'
        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= cutoff:
                exit_bar = bar; exit_rsn = '2:00PM cutoff'; break
            ml    = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            T_cur = ml / TRADING_MINS
            cur   = bs_price(bar['c'], strike, T_cur, r, iv, 'c') * 1.035
            if cur >= target:
                exit_bar = bar; exit_rsn = '50% target'; break
            if cur <= stop:
                exit_bar = bar; exit_rsn = '40% stop'; break

        if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'

        dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        exit_bid = bs_price(exit_bar['c'], strike, ml_exit/TRADING_MINS, r, iv, 'c') * 0.965
        pnl      = round((exit_bid - entry_ask) * 100, 2)

        trades.append(Trade('H8_MA50_Bounce', ds, ds, ds, entry_ask, exit_bid, pnl, vix, exit_rsn))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H9 — Low-VIX Wednesday Credit Spread
# Hypothesis: Wednesday is the highest theta/gamma ratio day for 0DTE
#             (equidistant from Mon and Fri, most predictable range)
# ══════════════════════════════════════════════════════════════════════════════
def run_h9_wednesday_credit(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades = []
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj.weekday() != 2: continue  # Wednesday only

        vix = vix_daily.get(ds, 16.0)
        if not (13 <= vix <= 20): continue  # tighter VIX band

        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue

        bars = spy_5min.get(ds, [])
        if len(bars) < 8: continue

        # Entry: 10:30–11:00 AM (after first hour noise settles)
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

        # 0.15-delta put for higher OTM safety
        short_k  = round(spy_e * (1-iv*0.35) / 0.5) * 0.5  # approx 0.15 delta
        long_k   = short_k - 2.0

        sc_bid   = bs_price(spy_e, short_k, T_e, r, iv, 'p') * 0.965
        lp_ask   = bs_price(spy_e, long_k,  T_e, r, iv, 'p') * 1.035
        credit   = round(sc_bid - lp_ask, 4)
        min_cred = max(0.08, vix * 0.006)
        if credit < min_cred: continue

        target_exit = credit * 0.25  # take 75% of credit
        stop_exit   = credit * 1.75  # tighter stop for Wednesday
        exit_bar    = None; exit_rsn = 'EOD'

        for bar in bars:
            dt_b = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if dt_b <= dt_entry: continue
            if (dt_b.hour, dt_b.minute) >= (15, 0):
                exit_bar = bar; exit_rsn = '3PM exit'; break
            ml    = max(16*60-(dt_b.hour*60+dt_b.minute), 1)
            T_cur = ml / TRADING_MINS
            sp_n  = bar['c']
            sc_a  = bs_price(sp_n, short_k, T_cur, r, iv, 'p') * 1.035
            lp_b  = bs_price(sp_n, long_k,  T_cur, r, iv, 'p') * 0.965
            cur   = max(sc_a - lp_b, 0)
            if cur <= target_exit:
                exit_bar = bar; exit_rsn = '75% target'; break
            if cur >= stop_exit:
                exit_bar = bar; exit_rsn = '1.75x stop'; break

        if exit_bar is None: exit_bar = bars[-1]

        dt_exit    = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
        ml_exit    = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
        T_ex       = ml_exit / TRADING_MINS
        sp_x       = exit_bar['c']
        sc_ax      = bs_price(sp_x, short_k, T_ex, r, iv, 'p') * 1.035
        lp_bx      = bs_price(sp_x, long_k,  T_ex, r, iv, 'p') * 0.965
        exit_debit = max(sc_ax - lp_bx, 0)
        pnl        = round((credit - exit_debit) * 100, 2)

        trades.append(Trade('H9_Wednesday_Credit', ds, ds, ds, credit, exit_debit, pnl, vix, exit_rsn))

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# H10 — VWAP-Cross Momentum: SPY crosses VWAP with volume surge, buy 0DTE
# Hypothesis: VWAP crosses with 2x volume = genuine directional conviction.
#             High-probability short-term continuation (30–90 min hold).
# ══════════════════════════════════════════════════════════════════════════════
def run_h10_vwap_momentum(spy_5min, spy_daily, vix_daily, exps, dates) -> List[Trade]:
    trades = []
    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        vix    = vix_daily.get(ds, 16.0)
        if vix > 25: continue

        bars = spy_5min.get(ds, [])
        if len(bars) < 12: continue

        exp = dt_obj if dt_obj in exps else find_exp(dt_obj, 0, 1, exps)
        if exp is None: continue

        # Scan bars after 10:30 AM for VWAP cross with volume surge
        r      = rfr(dt_obj)
        iv     = vix / 100.0
        traded = False  # one trade per day

        for i in range(6, len(bars)):
            if traded: break
            bar    = bars[i]
            dt_b   = datetime.fromtimestamp(bar['t']/1000, tz=ET)
            if (dt_b.hour, dt_b.minute) < (10, 30): continue
            if (dt_b.hour, dt_b.minute) >= (13, 30): break

            bars_so_far = bars[:i+1]
            vwap_now    = vwap(bars_so_far)
            prev_bar    = bars[i-1]
            prev_vwap   = vwap(bars[:i])
            spy_c       = bar['c']
            prev_c      = prev_bar['c']

            # Bullish VWAP cross: prev bar below VWAP, current bar above
            bullish_cross = prev_c < prev_vwap and spy_c > vwap_now
            # Volume surge: current bar 2x average of last 10 bars
            avg_vol = sum(b['v'] for b in bars_so_far[-11:-1]) / 10 if len(bars_so_far) >= 11 else 0
            vol_surge = bar['v'] >= avg_vol * 2.0 if avg_vol > 0 else False

            if not (bullish_cross and vol_surge): continue

            # Enter on next bar
            if i+1 >= len(bars): break
            entry_bar = bars[i+1]
            dt_entry  = datetime.fromtimestamp(entry_bar['t']/1000, tz=ET)
            spy_e     = entry_bar['c']
            strike    = round(spy_e / 0.5) * 0.5
            mins_left = max(16*60-(dt_entry.hour*60+dt_entry.minute), 1)
            T_e       = mins_left / TRADING_MINS

            entry_ask = round(bs_price(spy_e, strike, T_e, r, iv, 'c') * 1.07, 4)
            if entry_ask <= 0 or entry_ask * 100 > 1000: continue

            target  = entry_ask * 1.30
            stop    = entry_ask * 0.72
            cutoff  = (dt_entry.hour + 1, dt_entry.minute)  # 1-hour max hold

            exit_bar = None; exit_rsn = 'time'
            for bar2 in bars[i+2:]:
                dt_b2 = datetime.fromtimestamp(bar2['t']/1000, tz=ET)
                if (dt_b2.hour, dt_b2.minute) >= cutoff or (dt_b2.hour, dt_b2.minute) >= (14,0):
                    exit_bar = bar2; exit_rsn = '1-hour time stop'; break
                ml    = max(16*60-(dt_b2.hour*60+dt_b2.minute), 1)
                T_cur = ml / TRADING_MINS
                cur   = bs_price(bar2['c'], strike, T_cur, r, iv, 'c') * 1.035
                if cur >= target:
                    exit_bar = bar2; exit_rsn = '30% target'; break
                if cur <= stop:
                    exit_bar = bar2; exit_rsn = '28% stop'; break

            if exit_bar is None: exit_bar = bars[-1]; exit_rsn = 'EOD'

            dt_exit  = datetime.fromtimestamp(exit_bar['t']/1000, tz=ET)
            ml_exit  = max(16*60-(dt_exit.hour*60+dt_exit.minute), 1)
            exit_bid = bs_price(exit_bar['c'], strike, ml_exit/TRADING_MINS, r, iv, 'c') * 0.965
            pnl      = round((exit_bid - entry_ask) * 100, 2)

            trades.append(Trade('H10_VWAP_Momentum', ds, ds, ds, entry_ask, exit_bid, pnl, vix, exit_rsn))
            traded = True

    return trades


# ─── HYPOTHESIS REGISTRY ──────────────────────────────────────────────────────
HYPOTHESES = [
    {
        'id': 'H1', 'name': 'Pre-FOMC Drift',
        'description': 'Buy 0DTE ATM calls on FOMC morning, exit by 1:45 PM (Lucca-Moench drift)',
        'runner': run_h1_prefomc_drift,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 45.0,  # debit trade — needs 45% WR to be profitable
        'type': 'debit',
    },
    {
        'id': 'H2', 'name': 'VIX Reversal Iron Butterfly',
        'description': '0DTE iron butterfly on day VIX turns down from spike (IVR>70)',
        'runner': run_h2_vix_reversal_butterfly,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 55.0,
        'type': 'credit',
    },
    {
        'id': 'H3', 'name': 'Monday Gap-Fill',
        'description': 'Buy 0DTE calls on Monday gap-down, target gap fill by 11:30 AM',
        'runner': run_h3_monday_gapfill,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 45.0,
        'type': 'debit',
    },
    {
        'id': 'H4', 'name': 'Overnight Gap Momentum',
        'description': 'Buy 0DTE calls when SPY gaps up 0.5–2.5% at open, exit by 12:30',
        'runner': run_h4_gap_momentum,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 45.0,
        'type': 'debit',
    },
    {
        'id': 'H5', 'name': 'CPI Hot-Print Put Spread',
        'description': 'Debit put spread 2 days before known-hot CPI prints',
        'runner': run_h5_cpi_put_spread,
        'args': ['spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 45.0,
        'type': 'debit',
    },
    {
        'id': 'H6', 'name': 'Friday PM Premium Decay',
        'description': 'Sell 0DTE put spread on Friday 1 PM, capture accelerated theta',
        'runner': run_h6_friday_pm_decay,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 55.0,
        'type': 'credit',
    },
    {
        'id': 'H7', 'name': 'Post-Earnings IV Collapse',
        'description': 'Sell put spreads on NVDA/TSLA etc day after earnings (IV crush)',
        'runner': run_h7_postearnings_iv_collapse,
        'args': ['spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 55.0,
        'type': 'credit',
    },
    {
        'id': 'H8', 'name': 'MA50 Bounce',
        'description': 'Buy 0DTE calls when SPY bounces off MA50 on elevated VIX',
        'runner': run_h8_ma50_bounce,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 45.0,
        'type': 'debit',
    },
    {
        'id': 'H9', 'name': 'Wednesday Credit Spread',
        'description': 'Sell 0DTE put spread Wednesday 10:30 AM, VIX 13–20',
        'runner': run_h9_wednesday_credit,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 55.0,
        'type': 'credit',
    },
    {
        'id': 'H10', 'name': 'VWAP-Cross Momentum',
        'description': 'Buy 0DTE calls on bullish VWAP cross with 2x volume surge',
        'runner': run_h10_vwap_momentum,
        'args': ['spy_5min','spy_daily','vix_daily','exps','dates'],
        'breakeven_wr': 45.0,
        'type': 'debit',
    },
]


# ─── REPORT / JOURNAL ─────────────────────────────────────────────────────────
def fmt_pf(s): 
    p = s.get('pf', 0)
    return 'inf' if p == float('inf') else (f'{p:.2f}' if p else 'N/A')

def write_result(h, all_trades, is_trades, oos_trades, dead, kill_reason, elapsed):
    s_all = calc_stats(all_trades)
    s_is  = calc_stats(is_trades)
    s_oos = calc_stats(oos_trades)
    pp    = bootstrap_p(oos_trades)

    by_year = defaultdict(list)
    for t in oos_trades: by_year[t.entry_date[:4]].append(t)

    lines = [
        f"## {h['id']} — {h['name']}",
        f"**Hypothesis:** {h['description']}",
        f"**Type:** {h['type']} | **Elapsed:** {elapsed:.1f}s",
        f"**Status:** {'❌ DEAD — ' + kill_reason if dead else '✅ ALIVE'}",
        "",
        "### Full Period Stats",
        f"- N={s_all['n']}  WR={s_all['wr']:.1f}%  P&L=${s_all['total_pnl']:+,.2f}  PF={fmt_pf(s_all)}  Sharpe={s_all['sharpe']:.2f}",
        "",
        "### In-Sample (2021–Jun 2023)",
        f"- N={s_is['n']}  WR={s_is['wr']:.1f}%  P&L=${s_is['total_pnl']:+,.2f}  PF={fmt_pf(s_is)}",
        "",
        "### Out-of-Sample (Jul 2023–2026, excl 2025 blind)",
        f"- N={s_oos['n']}  WR={s_oos['wr']:.1f}%  P&L=${s_oos['total_pnl']:+,.2f}  PF={fmt_pf(s_oos)}  Sharpe={s_oos['sharpe']:.2f}  Boot={pp:.0f}%",
        "",
        "### OOS Year Breakdown",
    ]
    for yr, ts in sorted(by_year.items()):
        ys = calc_stats(ts)
        lines.append(f"- {yr}: N={ys['n']}  WR={ys['wr']:.1f}%  P&L=${ys['total_pnl']:+,.2f}  PF={fmt_pf(ys)}")

    lines += ["", "### Exit Reason Breakdown (OOS)"]
    er = defaultdict(lambda: {'n':0,'pnl':0.0,'w':0})
    for t in oos_trades:
        er[t.note]['n'] += 1; er[t.note]['pnl'] += t.pnl
        if t.pnl > 0: er[t.note]['w'] += 1
    for rsn, v in sorted(er.items(), key=lambda x: -x[1]['n']):
        wr = v['w']/v['n']*100 if v['n'] else 0
        lines.append(f"- `{rsn}`: N={v['n']}  WR={wr:.0f}%  P&L=${v['pnl']:+.2f}")

    lines += ["", "---", ""]
    result_path = RESULTS_DIR / f"{h['id']}.md"
    result_path.write_text('\n'.join(lines))
    return '\n'.join(lines)

def append_journal(text: str):
    with JOURNAL.open('a') as f:
        f.write(text + '\n')

def init_journal():
    header = f"""# Hermes Research Journal
*Autonomous overnight strategy research — started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*

**Baseline to beat:**
- Credit Spread: OOS WR 80%, P&L +$3,639
- Earnings Momentum: P&L +$53,106

**Kill criteria:**
- OOS Sharpe < 1.0
- OOS WR < breakeven for strategy type  
- >60% profits from single year
- fewer than 20 OOS trades

---

"""
    JOURNAL.write_text(header)

def tg_batch_update(results_batch: list, batch_num: int):
    alive = [r for r in results_batch if not r['dead']]
    dead  = [r for r in results_batch if r['dead']]
    
    msg = f"📊 *Hermes Research — Batch {batch_num} Update*\n"
    msg += f"Tested {len(results_batch)} hypotheses: ✅ {len(alive)} alive | ❌ {len(dead)} dead\n\n"
    
    if alive:
        msg += "*SURVIVORS:*\n"
        for r in alive:
            s = r['s_oos']
            msg += (f"✅ *{r['id']} — {r['name']}*\n"
                    f"   OOS: N={s['n']} WR={s['wr']:.1f}% P&L=${s['total_pnl']:+,.0f} "
                    f"Sharpe={s['sharpe']:.2f}\n")
    if dead:
        msg += "\n*ELIMINATED:*\n"
        for r in dead:
            msg += f"❌ {r['id']} {r['name']}: {r['kill_reason']}\n"
    
    tg(msg)


# ─── MAIN RESEARCH LOOP ───────────────────────────────────────────────────────
def main():
    print(f"\n{'='*70}")
    print("  HERMES AUTONOMOUS RESEARCH ENGINE")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    # Load all shared data once
    print("  Loading market data...")
    vix_daily = load_vix()
    spy_daily = load_spy_daily()
    spy_5min  = load_spy_5min()
    exps      = get_theta_expirations()
    all_dates = sorted(spy_daily.keys())
    print(f"  Loaded: {len(all_dates)} trading days | {len(exps)} ThetaData expirations")
    print(f"  VIX range: {min(vix_daily.values()):.1f} – {max(vix_daily.values()):.1f}")

    is_dates  = [d for d in all_dates if d < SPLIT_DATE.isoformat()]
    oos_dates = [d for d in all_dates if d >= SPLIT_DATE.isoformat() and not d.startswith(str(BLIND_YEAR))]
    bld_dates = [d for d in all_dates if d.startswith(str(BLIND_YEAR))]
    print(f"  IS: {len(is_dates)} days | OOS: {len(oos_dates)} days | Blind {BLIND_YEAR}: {len(bld_dates)} days\n")

    data_ctx = {
        'spy_5min':  spy_5min,
        'spy_daily': spy_daily,
        'vix_daily': vix_daily,
        'exps':      exps,
        'dates':     all_dates,
    }

    init_journal()
    tg("🚀 *Hermes Research Engine started*\n10 hypotheses queued. Running all night.\nBaseline: Credit Spread OOS WR 80% | Earnings P&L +$53K")

    batch_results = []
    all_survivors = []

    for i, h in enumerate(HYPOTHESES):
        print(f"\n  [{i+1}/{len(HYPOTHESES)}] Running {h['id']}: {h['name']}...")
        print(f"  Hypothesis: {h['description']}")
        t0 = time.time()

        # Build kwargs
        kwargs = {k: data_ctx[k] for k in h['args']}

        try:
            all_trades = h['runner'](**kwargs)
        except Exception as e:
            import traceback
            print(f"  ERROR: {e}")
            traceback.print_exc()
            all_trades = []

        elapsed  = time.time() - t0
        is_t     = [t for t in all_trades if t.entry_date < SPLIT_DATE.isoformat()]
        oos_t    = [t for t in all_trades if t.entry_date >= SPLIT_DATE.isoformat()
                    and not t.entry_date.startswith(str(BLIND_YEAR))]
        bld_t    = [t for t in all_trades if t.entry_date.startswith(str(BLIND_YEAR))]

        s_all  = calc_stats(all_trades)
        s_oos  = calc_stats(oos_t)
        dead, kill_reason = kill_check(h['name'], is_t, oos_t, h['breakeven_wr'])

        print(f"  Total:  N={s_all['n']:>4}  WR={s_all['wr']:>5.1f}%  P&L=${s_all['total_pnl']:>+10,.2f}  PF={fmt_pf(s_all):>5}  Sharpe={s_all['sharpe']:>5.2f}")
        print(f"  OOS:    N={s_oos['n']:>4}  WR={s_oos['wr']:>5.1f}%  P&L=${s_oos['total_pnl']:>+10,.2f}  PF={fmt_pf(s_oos):>5}  Sharpe={s_oos['sharpe']:>5.2f}")
        print(f"  Status: {'❌ DEAD — ' + kill_reason if dead else '✅ ALIVE — passes all kill criteria'}")

        result_text = write_result(h, all_trades, is_t, oos_t, dead, kill_reason, elapsed)
        append_journal(result_text)

        r = {
            'id': h['id'], 'name': h['name'], 'dead': dead,
            'kill_reason': kill_reason, 's_oos': s_oos,
            'total_pnl': s_all['total_pnl'], 'n': s_all['n'],
        }
        batch_results.append(r)
        if not dead:
            all_survivors.append({**r, 'h': h, 'trades': all_trades})

        # Send Telegram every 5 strategies
        if (i+1) % 5 == 0 or (i+1) == len(HYPOTHESES):
            batch_num = (i+1) // 5 if (i+1) % 5 == 0 else math.ceil((i+1)/5)
            tg_batch_update(batch_results[-5:], batch_num)

    # ── Final summary ────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("  RESEARCH COMPLETE")
    print(f"{'='*70}")
    print(f"  Total hypotheses tested: {len(HYPOTHESES)}")
    print(f"  Survivors: {len(all_survivors)}")
    print(f"  Dead: {len(HYPOTHESES)-len(all_survivors)}")

    summary_lines = [
        "\n---\n## FINAL SUMMARY\n",
        f"*Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n",
        f"Tested {len(HYPOTHESES)} hypotheses | {len(all_survivors)} survivors | {len(HYPOTHESES)-len(all_survivors)} eliminated\n",
        "\n### Survivors (ranked by OOS P&L)\n",
    ]
    survivors_sorted = sorted(all_survivors, key=lambda x: x['s_oos']['total_pnl'], reverse=True)
    for r in survivors_sorted:
        s = r['s_oos']
        summary_lines.append(
            f"- **{r['id']} {r['name']}**: OOS N={s['n']} WR={s['wr']:.1f}% "
            f"P&L=${s['total_pnl']:+,.0f} Sharpe={s['sharpe']:.2f}"
        )

    summary_lines += ["\n### Eliminated\n"]
    for r in batch_results:
        if r['dead']:
            summary_lines.append(f"- ❌ {r['id']} {r['name']}: {r['kill_reason']}")

    append_journal('\n'.join(summary_lines))

    # Final Telegram summary
    final_msg = f"🏁 *Hermes Research Complete*\n\n"
    final_msg += f"Tested {len(HYPOTHESES)} hypotheses\n"
    final_msg += f"✅ {len(all_survivors)} survivors | ❌ {len(HYPOTHESES)-len(all_survivors)} dead\n\n"
    if survivors_sorted:
        final_msg += "*Top strategies:*\n"
        for r in survivors_sorted[:3]:
            s = r['s_oos']
            final_msg += (f"🥇 *{r['id']} — {r['name']}*\n"
                          f"   OOS: N={s['n']} WR={s['wr']:.1f}% P&L=${s['total_pnl']:+,.0f} Sharpe={s['sharpe']:.2f}\n")
    final_msg += f"\nJournal: /root/spy-0dte-trader/hermes_research/journal.md"
    tg(final_msg)

    print(f"\n  Journal: {JOURNAL}")
    print(f"  Results: {RESULTS_DIR}/")
    print("\n  Done.\n")


if __name__ == '__main__':
    main()
