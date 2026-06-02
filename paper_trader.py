#!/usr/bin/env python3
"""
SPY 0DTE Momentum — Config D Final
Edge: 67% WR | PF 2.02 | Sharpe 12.26  (backtested 12 months, 100 trades)
Rules: Calls ONLY | Monday + Friday | VIX < 20 | Smart no-move exit
"""

import csv
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
import requests
import yfinance as yf
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv(r'C:\Users\sagla\.tastytrade-mcp\.env')

def _s(v: Optional[str]) -> str:
    return (v or '').strip("'\"")

TRADIER_KEY = _s(os.getenv('TRADIER_API_KEY'))
TT_SECRET   = _s(os.getenv('TASTYTRADE_CLIENT_SECRET'))
TT_REFRESH  = _s(os.getenv('TASTYTRADE_REFRESH_TOKEN'))
TT_ACCOUNT  = _s(os.getenv('TASTYTRADE_ACCOUNT_ID'))
USE_PROD    = _s(os.getenv('TASTYTRADE_USE_PRODUCTION', 'true')).lower() == 'true'
TT_BASE     = 'https://api.tastytrade.com' if USE_PROD else 'https://api.cert.tastytrade.com'
ET          = pytz.timezone('America/New_York')

# ─────────────────────────────────────────────────────────────────────────────
# Strategy parameters — Config D Final (do not change without re-running backtest)
# ─────────────────────────────────────────────────────────────────────────────
STRATEGY_NAME = 'SPY 0DTE Momentum — Config D Final'
EDGE_SUMMARY  = '67% WR | PF 2.02 | Sharpe 12.26 | 100 trades / 12 months'

TRADE_WDAYS     = frozenset({0, 4})   # Monday=0, Friday=4
VIX_MAX         = 20.0               # skip entry if VIX >= this
BIAS_MIN_SEP    = 0.50               # min SPY-vs-30m-VWAP gap to confirm call bias

OR_MIN          = 15                 # opening range: first 15 min (9:30–9:44)
VOL_PERIODS     = 20                 # bars for rolling volume average
OPEN_BELL_SKIP  = 5                  # exclude first N bars (9:30–9:34) from vol avg
ENTRY_VOL_SURGE = 1.5                # entry candle must be >= 1.5x 20-bar avg
HH_LOOKBACK     = 5                  # candles back for higher-high check
VWAP_GAP_MIN    = 0.10               # min SPY-VWAP gap to enter
VWAP_GAP_MAX    = 2.00               # max SPY-VWAP gap to enter

TARGET_PCT      = 0.04               # 4% profit target on option price
STOP_LOSS_PCT   = 0.35               # 35% loss hard stop  → stop = entry * 0.65
NO_MOVE_PCT     = 0.10               # option must gain 10%+ to be "alive" at 10m
COIL_VOL        = 0.8                # last-3-bar avg vol / 20-bar avg; below = dead
NO_MOVE_MIN     = 10                 # minutes before no-move vol check
COIL_MAX_MIN    = 20                 # max hold after passing vol check
MAX_HOLD_MIN    = 60                 # absolute time stop (minutes)

CONTRACTS       = 2                  # contracts per trade
MAX_COST        = 500.0              # max premium per contract ($)
MAX_TRADES_DAY  = 3                  # max entries per calendar day
MAX_DAILY_LOSS  = -300.0             # halt entries if day P&L hits this
COOLDOWN_MIN    = 10                 # minutes between any two trades

POLL_SEC        = 30
ENTRY_WINDOW    = [((9, 45), (12, 0))]
CUTOFF          = (15, 15)

CSV_PATH = Path(r'C:\Users\sagla\paper_trades.csv')
CSV_HEADERS = [
    'date', 'day_of_week', 'entry_time', 'exit_time', 'direction', 'opt_ticker',
    'entry_price', 'exit_price', 'peak_price', 'contracts', 'pnl_dollars',
    'exit_reason', 'vix_at_entry', 'spy_at_entry', 'vwap_at_entry',
    'gap_at_entry', 'volume_ratio', 'hold_minutes', 'running_wr',
]

W = 80   # dashboard width

# ─────────────────────────────────────────────────────────────────────────────
# Env validation
# ─────────────────────────────────────────────────────────────────────────────
def validate_env() -> None:
    missing = [k for k, v in {
        'TRADIER_API_KEY':          TRADIER_KEY,
        'TASTYTRADE_CLIENT_SECRET': TT_SECRET,
        'TASTYTRADE_REFRESH_TOKEN': TT_REFRESH,
        'TASTYTRADE_ACCOUNT_ID':    TT_ACCOUNT,
    }.items() if not v]
    if missing:
        sys.exit(f'[ERROR] Missing .env keys: {", ".join(missing)}')

# ─────────────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────────────
DOW_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

def now_et() -> datetime:
    return datetime.now(ET)

def in_market_hours(t: datetime) -> bool:
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)

def in_entry_window(t: datetime) -> bool:
    hm = (t.hour, t.minute)
    return any(s <= hm < e for s, e in ENTRY_WINDOW)

def past_cutoff(t: datetime) -> bool:
    return (t.hour, t.minute) >= CUTOFF

def or_complete(t: datetime) -> bool:
    return (t.hour, t.minute) >= (9, 45)

# ─────────────────────────────────────────────────────────────────────────────
# Tastytrade auth
# ─────────────────────────────────────────────────────────────────────────────
_tt_tok: Optional[str] = None
_tt_exp: float = 0.0

def tt_token() -> str:
    global _tt_tok, _tt_exp
    if _tt_tok and time.time() < _tt_exp:
        return _tt_tok
    r = requests.post(
        f'{TT_BASE}/oauth/token',
        data={'grant_type': 'refresh_token', 'refresh_token': TT_REFRESH,
              'client_secret': TT_SECRET},
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f'TT auth {r.status_code}: {r.text[:200]}')
    d   = r.json()
    tok = (d.get('access_token') or d.get('session-token')
           or (d.get('data') or {}).get('session-token'))
    if not tok:
        raise RuntimeError(f'No token in TT response: {list(d.keys())}')
    _tt_tok, _tt_exp = tok, time.time() + 3500
    return tok

def tt_get(path: str, **params) -> dict:
    r = requests.get(f'{TT_BASE}{path}',
                     headers={'Authorization': f'Bearer {tt_token()}'},
                     params=params, timeout=15)
    if not r.ok:
        raise RuntimeError(f'TT GET {path} -> {r.status_code}')
    return r.json()

def fetch_tt_balance() -> Optional[float]:
    try:
        d = tt_get(f'/accounts/{TT_ACCOUNT}/balances')
        return float((d.get('data') or {}).get('net-liquidating-value', 0) or 0)
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────────────────────
# Tradier helpers
# ─────────────────────────────────────────────────────────────────────────────
TRADIER_BASE = 'https://api.tradier.com/v1'

def tradier_get(path: str, **params) -> dict:
    r = requests.get(
        f'{TRADIER_BASE}{path}',
        headers={'Authorization': f'Bearer {TRADIER_KEY}', 'Accept': 'application/json'},
        params=params, timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f'Tradier {path} -> {r.status_code}')
    return r.json()

# ─────────────────────────────────────────────────────────────────────────────
# Market data — SPY 1-min bars (Tradier primary, yfinance fallback)
# ─────────────────────────────────────────────────────────────────────────────
def _yf_spy_bars(_today: str) -> list:
    try:
        df = yf.download('SPY', period='1d', interval='1m', progress=False, auto_adjust=True)
        if df.empty:
            return []
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.dropna(subset=['Close'])
        return [{'t': int(ts.timestamp() * 1000),
                 'o': float(r['Open']),  'h': float(r['High']),
                 'l': float(r['Low']),   'c': float(r['Close']),
                 'v': float(r['Volume'])} for ts, r in df.iterrows()]
    except Exception:
        return []

def fetch_spy_bars(today: str) -> list:
    try:
        d = tradier_get('/markets/timesales', symbol='SPY', interval='1min',
                        start=today, session_filter='open')
        series = (d.get('series') or {}).get('data') or []
        if isinstance(series, dict):
            series = [series]
        if not series:
            raise ValueError('empty response')
        bars = []
        for b in series:
            ts = ET.localize(datetime.fromisoformat(b['time'].replace(' ', 'T')))
            bars.append({'t': int(ts.timestamp() * 1000),
                         'o': float(b['open']), 'h': float(b['high']),
                         'l': float(b['low']),  'c': float(b['close']),
                         'v': float(b['volume'])})
        return bars
    except Exception as e:
        print(f'  [Tradier bars] {e} — yfinance fallback')
        return _yf_spy_bars(today)

# ─────────────────────────────────────────────────────────────────────────────
# VIX (Tradier primary, yfinance fallback, 2-min cache)
# ─────────────────────────────────────────────────────────────────────────────
_vix_cache: tuple = (0.0, 0.0)

def _yf_vix() -> float:
    try:
        df = yf.download('^VIX', period='1d', interval='1m', progress=False, auto_adjust=True)
        if not df.empty:
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            closes = df['Close'].dropna()
            return float(closes.iloc[-1]) if not closes.empty else 0.0
    except Exception:
        pass
    return 0.0

def fetch_vix(_today: str) -> float:
    global _vix_cache
    if time.time() - _vix_cache[1] < 120:
        return _vix_cache[0]
    try:
        d   = tradier_get('/markets/quotes', symbols='VIX')
        q   = (d.get('quotes') or {}).get('quote') or {}
        val = float(q.get('last') or q.get('close') or 0)
        if val <= 0:
            raise ValueError('bad VIX value')
    except Exception:
        val = _yf_vix() or _vix_cache[0]
    _vix_cache = (val, time.time())
    return val

# ─────────────────────────────────────────────────────────────────────────────
# Options chain (Tradier primary, yfinance fallback, 90-s cache)
# ─────────────────────────────────────────────────────────────────────────────
_tradier_chain_cache: dict = {}
_yf_chain_cache:      dict = {}

def _tradier_chain(exp_date: str) -> list:
    cached = _tradier_chain_cache.get(exp_date)
    if cached and time.time() - cached[0] < 90:
        return cached[1]
    try:
        d    = tradier_get('/markets/options/chains', symbol='SPY',
                           expiration=exp_date, greeks='true')
        opts = (d.get('options') or {}).get('option') or []
        if isinstance(opts, dict):
            opts = [opts]
        _tradier_chain_cache[exp_date] = (time.time(), opts)
        return opts
    except Exception as e:
        print(f'  [Tradier chain] {e}')
        return cached[1] if cached else []

def _yf_call_ask(exp_date: str, strike: float) -> float:
    cached = _yf_chain_cache.get(exp_date)
    if not (cached and time.time() - cached[0] < 90):
        try:
            chain = yf.Ticker('SPY').option_chain(exp_date)
            _yf_chain_cache[exp_date] = (time.time(), chain.calls)
        except Exception:
            if cached:
                _yf_chain_cache[exp_date] = (cached[0], cached[1])
            return 0.0
    calls = _yf_chain_cache[exp_date][1]
    if calls is None or calls.empty:
        return 0.0
    row = calls[calls['strike'] == strike]
    if row.empty:
        row = calls.iloc[(calls['strike'] - strike).abs().argsort()[:1]]
    ask = float(row['ask'].iloc[0])
    return ask if ask > 0 else float(row['lastPrice'].iloc[0])

def nearest_exp() -> date:
    d = date.today()
    return d if d.weekday() < 5 else d + timedelta(days=7 - d.weekday())

def build_opt_ticker(exp: date, strike: float) -> str:
    return f'O:SPY{exp.strftime("%y%m%d")}C{int(strike * 1000):08d}'

def _parse_ticker(ticker: str) -> tuple:
    m = re.match(r'O:SPY(\d{6})C(\d{8})', ticker)
    if not m:
        return None, None
    exp    = datetime.strptime(m.group(1), '%y%m%d').date().isoformat()
    strike = int(m.group(2)) / 1000
    return exp, strike

def fetch_option_ask(ticker: str) -> float:
    exp, strike = _parse_ticker(ticker)
    if not exp:
        return 0.0
    for o in _tradier_chain(exp):
        if o.get('option_type') == 'call' and float(o.get('strike', -1)) == strike:
            ask = float(o.get('ask') or 0)
            return ask if ask > 0 else float(o.get('last') or 0)
    return _yf_call_ask(exp, strike)

def build_call_info(spy_price: float) -> dict:
    exp  = nearest_exp()
    opts = _tradier_chain(exp.isoformat())
    if opts:
        calls   = [o for o in opts if o.get('option_type') == 'call']
        strikes = sorted(set(float(o['strike']) for o in calls if 'strike' in o))
        if not strikes:
            raise RuntimeError('no call strikes in chain')
        strike = min(strikes, key=lambda s: abs(s - spy_price))
        ask    = next((float(o.get('ask') or o.get('last') or 0)
                       for o in calls if float(o.get('strike', -1)) == strike), 0.0)
    else:
        strike = float(round(spy_price))
        ask    = _yf_call_ask(exp.isoformat(), strike)
    if ask <= 0:
        raise RuntimeError('option ask unavailable — no chain data')
    return {
        'ticker': build_opt_ticker(exp, strike),
        'ask':    ask,
        'strike': strike,
        'exp':    exp.isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────
def calc_vwap(bars: list) -> float:
    if not bars:
        return 0.0
    tpv = sum(((b['h'] + b['l'] + b['c']) / 3) * b['v'] for b in bars)
    vol = sum(b['v'] for b in bars)
    return tpv / vol if vol else 0.0

def _vol_avg(bars: list) -> float:
    def _after_open(b: dict) -> bool:
        t = datetime.fromtimestamp(b['t'] / 1000, tz=ET)
        return t.hour > 9 or t.minute >= 30 + OPEN_BELL_SKIP

    hist = [b for b in bars[:-1] if _after_open(b)][-VOL_PERIODS:]
    return sum(b['v'] for b in hist) / len(hist) if hist else 0.0

def _last3_vol_avg(bars: list) -> float:
    last3 = [b['v'] for b in bars[-3:] if b['v'] > 0]
    return sum(last3) / len(last3) if last3 else 0.0

def _higher_high(bars: list) -> tuple:
    if len(bars) < HH_LOOKBACK + 1:
        return False, 0.0, 0.0
    cur      = bars[-1]['h']
    prev_max = max(b['h'] for b in bars[-(HH_LOOKBACK + 1):-1])
    return cur > prev_max, cur, prev_max

# ─────────────────────────────────────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────────────────────────────────────
class Position:
    def __init__(self, ticker: str, ask: float, spy: float, vwap_val: float,
                 gap: float, vol_ratio: float, vix: float, entry_t: datetime):
        self.ticker      = ticker
        self.entry_ask   = ask
        self.spy_entry   = spy
        self.vwap_entry  = vwap_val
        self.gap_entry   = gap
        self.vol_ratio   = vol_ratio
        self.vix_entry   = vix
        self.entry_time  = entry_t
        self.target      = round(ask * (1.0 + TARGET_PCT),    4)
        self.stop        = round(ask * (1.0 - STOP_LOSS_PCT), 4)
        self.cur_price   = ask
        self.peak_price  = ask
        self.coil_mode   = False

    def update(self, price: float) -> None:
        if price > 0:
            self.cur_price  = price
            self.peak_price = max(self.peak_price, price)

    def pnl(self) -> float:
        return (self.cur_price - self.entry_ask) * 100 * CONTRACTS

    def held_min(self, t: datetime) -> float:
        return (t - self.entry_time).total_seconds() / 60

    def check_exit(self, t: datetime, bars: list) -> tuple:
        held    = self.held_min(t)
        no_move = self.peak_price < self.entry_ask * (1.0 + NO_MOVE_PCT)

        if self.cur_price >= self.target:
            return True, '4% target hit'
        if self.cur_price <= self.stop:
            return True, '35% stop hit'
        if past_cutoff(t):
            return True, '3:15 PM cutoff'
        if held >= MAX_HOLD_MIN:
            return True, f'{MAX_HOLD_MIN}-min time stop'

        # Smart no-move exit (Test C / coil logic)
        if not self.coil_mode and held >= NO_MOVE_MIN and no_move:
            avg_v  = _vol_avg(bars)
            last3v = _last3_vol_avg(bars)
            dead   = (avg_v == 0) or (last3v < avg_v * COIL_VOL)
            if dead:
                return True, 'No move + dead vol (10m)'
            self.coil_mode = True   # volume alive — hold to 20m
        elif self.coil_mode and held >= COIL_MAX_MIN and no_move:
            return True, f'No move in {COIL_MAX_MIN}m (coil expired)'

        return False, ''

    def smart_status(self, t: datetime) -> str:
        held = self.held_min(t)
        if self.coil_mode:
            rem = max(0.0, COIL_MAX_MIN - held)
            return f'COIL — exit in {rem:.0f}m if no move'
        elif held < NO_MOVE_MIN:
            rem = NO_MOVE_MIN - held
            return f'vol check in {rem:.0f}m'
        return 'monitoring'

# ─────────────────────────────────────────────────────────────────────────────
# Running stats — Kelly position sizing tracker
# ─────────────────────────────────────────────────────────────────────────────
class RunningStats:
    def __init__(self):
        self.wins       = 0
        self.losses     = 0
        self.gross_win  = 0.0
        self.gross_loss = 0.0

    def load_csv(self) -> None:
        if not CSV_PATH.exists():
            return
        with CSV_PATH.open(encoding='utf-8') as f:
            for row in csv.DictReader(f):
                try:
                    pnl = float(row.get('pnl_dollars', 0))
                    if pnl > 0:
                        self.wins      += 1
                        self.gross_win  += pnl
                    elif pnl < 0:
                        self.losses    += 1
                        self.gross_loss += pnl
                except (ValueError, KeyError):
                    pass

    def record(self, pnl: float) -> None:
        if pnl > 0:
            self.wins      += 1
            self.gross_win  += pnl
        elif pnl < 0:
            self.losses    += 1
            self.gross_loss += pnl

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def wr(self) -> float:
        return self.wins / self.total if self.total else 0.0

    @property
    def kelly(self) -> Optional[float]:
        if self.total < 5 or self.wins == 0 or self.losses == 0:
            return None
        avg_win  = self.gross_win  / self.wins
        avg_loss = abs(self.gross_loss) / self.losses
        if avg_loss == 0:
            return None
        w = self.wr
        r = avg_win / avg_loss
        return max(0.0, w - (1.0 - w) / r) * 100.0

    def wr_str(self) -> str:
        return f'{self.wr * 100:.1f}%' if self.total else 'N/A'

    def kelly_str(self) -> str:
        k = self.kelly
        return f'{k:.1f}%' if k is not None else f'need {max(0, 5 - self.total)} more trades'

# ─────────────────────────────────────────────────────────────────────────────
# CSV logging
# ─────────────────────────────────────────────────────────────────────────────
def log_trade(pos: Position, exit_px: float, exit_t: datetime,
              reason: str, running_wr: str) -> float:
    pnl = (exit_px - pos.entry_ask) * 100 * CONTRACTS
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if new_file:
            w.writeheader()
        w.writerow({
            'date':          pos.entry_time.date().isoformat(),
            'day_of_week':   pos.entry_time.strftime('%A'),
            'entry_time':    pos.entry_time.strftime('%H:%M:%S'),
            'exit_time':     exit_t.strftime('%H:%M:%S'),
            'direction':     'call',
            'opt_ticker':    pos.ticker,
            'entry_price':   f'{pos.entry_ask:.4f}',
            'exit_price':    f'{exit_px:.4f}',
            'peak_price':    f'{pos.peak_price:.4f}',
            'contracts':     CONTRACTS,
            'pnl_dollars':   f'{pnl:.2f}',
            'exit_reason':   reason,
            'vix_at_entry':  f'{pos.vix_entry:.2f}',
            'spy_at_entry':  f'{pos.spy_entry:.2f}',
            'vwap_at_entry': f'{pos.vwap_entry:.2f}',
            'gap_at_entry':  f'{pos.gap_entry:.2f}',
            'volume_ratio':  f'{pos.vol_ratio:.2f}',
            'hold_minutes':  f'{pos.held_min(exit_t):.1f}',
            'running_wr':    running_wr,
        })
    return pnl

# ─────────────────────────────────────────────────────────────────────────────
# Recover today's stats from CSV on restart
# ─────────────────────────────────────────────────────────────────────────────
def recover_daily_stats() -> tuple:
    today            = date.today().isoformat()
    daily_pnl        = 0.0
    daily_entries    = 0     # trades entered today (wins + losses + open)
    daily_wins       = 0
    daily_losses     = 0
    last_trade_time: Optional[datetime] = None

    if not CSV_PATH.exists():
        return daily_pnl, daily_entries, daily_wins, daily_losses, last_trade_time

    with CSV_PATH.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('date') != today:
                continue
            try:
                pnl = float(row.get('pnl_dollars', 0))
                daily_pnl     += pnl
                daily_entries += 1
                if pnl > 0:
                    daily_wins   += 1
                elif pnl < 0:
                    daily_losses += 1
                exit_str = f"{row['date']} {row['exit_time']}"
                exit_t   = ET.localize(datetime.strptime(exit_str, '%Y-%m-%d %H:%M:%S'))
                if last_trade_time is None or exit_t > last_trade_time:
                    last_trade_time = exit_t
            except (ValueError, KeyError):
                pass

    return daily_pnl, daily_entries, daily_wins, daily_losses, last_trade_time

# ─────────────────────────────────────────────────────────────────────────────
# Entry condition checklist
# ─────────────────────────────────────────────────────────────────────────────
def check_entry(bars: list, vwap: float, vix: float, now: datetime,
                daily_bias: Optional[str], position: Optional[Position],
                last_trade_time: Optional[datetime],
                daily_pnl: float, daily_entries: int) -> tuple:
    spy = bars[-1]['c'] if bars else 0.0
    gap = spy - vwap

    hh_ok, cur_h, prev_h = _higher_high(bars)
    avg_v    = _vol_avg(bars)
    cur_v    = bars[-1]['v'] if bars else 0
    vol_r    = cur_v / avg_v if avg_v else 0.0
    vol_ok   = vol_r >= ENTRY_VOL_SURGE

    if last_trade_time is not None:
        cd_elapsed = (now - last_trade_time).total_seconds() / 60
        cd_ok      = cd_elapsed >= COOLDOWN_MIN
        cd_str     = f'{cd_elapsed:.0f}m since last trade'
    else:
        cd_ok, cd_str = True, 'no trades yet today'

    checks = [
        (daily_bias == 'call',
         'Daily bias = CALL',
         f'[{daily_bias or "PENDING"}]'),
        (hh_ok,
         f'Higher high vs last {HH_LOOKBACK} bars',
         f'[${cur_h:.2f} > ${prev_h:.2f}]' if len(bars) > HH_LOOKBACK else '[need more bars]'),
        (vol_ok,
         f'Volume >= {ENTRY_VOL_SURGE}x avg',
         f'[{vol_r:.2f}x  cur:{int(cur_v):,}  avg:{int(avg_v):,}]'),
        (spy > vwap,
         'SPY above VWAP',
         f'[${spy:.2f} vs VWAP ${vwap:.2f}]'),
        (VWAP_GAP_MIN <= gap <= VWAP_GAP_MAX,
         f'VWAP gap ${VWAP_GAP_MIN:.2f}–${VWAP_GAP_MAX:.2f}',
         f'[gap ${gap:.2f}]'),
        (in_entry_window(now),
         'Time window 9:45–12:00 ET',
         f'[{now.strftime("%H:%M")}]'),
        (0 < vix < VIX_MAX,
         f'VIX < {VIX_MAX}',
         f'[{vix:.2f}]'),
        (position is None,
         'No open position',
         '[None]' if position is None else '[OPEN]'),
        (cd_ok,
         f'Cooldown >= {COOLDOWN_MIN}m',
         f'[{cd_str}]'),
        (daily_pnl > MAX_DAILY_LOSS,
         f'Daily loss limit (${MAX_DAILY_LOSS:.0f})',
         f'[day P&L ${daily_pnl:+.2f}]'),
        (daily_entries < MAX_TRADES_DAY,
         f'Trades today < {MAX_TRADES_DAY}',
         f'[{daily_entries} of {MAX_TRADES_DAY}]'),
    ]

    all_ok = all(ok for ok, _, _ in checks)
    return all_ok, checks, vol_r

# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────
def hr(c: str = '=') -> str:
    return c * W

def mk(ok: bool) -> str:
    return 'YES' if ok else ' NO'

def print_dashboard(
    now:               datetime,
    spy:               float,
    vwap:              float,
    vix:               float,
    daily_bias:        Optional[str],
    bias_gap:          float,
    bias_set:          bool,
    checks:            list,
    all_ok:            bool,
    position:          Optional[Position],
    daily_pnl:         float,
    daily_entries:     int,
    daily_wins:        int,
    daily_losses:      int,
    rstats:            RunningStats,
    is_trading_day:    bool,
    skip_day_reason:   Optional[str],
    vix_skip:          bool,
) -> None:

    gap = spy - vwap

    # ── Line 1 — date / time / market status ──────────────────────────────────
    mkt_status = 'MARKET OPEN' if in_market_hours(now) else (
        'PRE-MARKET' if now.hour < 12 else 'AFTER HOURS')
    print(f'\n{hr()}')
    print(f'  {STRATEGY_NAME}')
    print(f'  {now.strftime("%Y-%m-%d  %H:%M:%S")} ET    {mkt_status}    {EDGE_SUMMARY}')
    print(hr())

    # ── Line 2 — day status ────────────────────────────────────────────────────
    dow = DOW_NAMES[now.weekday()]
    if skip_day_reason:
        day_line = f'  DAY STATUS   : SKIP — {skip_day_reason}'
    elif vix_skip:
        day_line = f'  DAY STATUS   : VIX FILTER ACTIVE — VIX {vix:.2f} >= {VIX_MAX} (no entries)'
    else:
        day_line = f'  DAY STATUS   : TRADING [{dow}]'
    print(day_line)

    # ── Line 3 — bias status ───────────────────────────────────────────────────
    if not bias_set:
        bias_line = '  BIAS STATUS  : PENDING — locks at 9:45 AM (first 15 candles)'
    elif daily_bias == 'call':
        bias_line = f'  BIAS STATUS  : CONFIRMED CALL  +${bias_gap:.2f} above 30m VWAP'
    elif daily_bias == 'skip':
        bias_line = f'  BIAS STATUS  : NO BIAS — gap ${bias_gap:+.2f} (need >= +${BIAS_MIN_SEP:.2f}) — skip day'
    else:
        bias_line = f'  BIAS STATUS  : BEARISH — gap ${bias_gap:+.2f} — calls not valid today'
    print(bias_line)

    # ── Line 4 — market data ───────────────────────────────────────────────────
    print(f'  MARKET       : SPY ${spy:.2f}  |  VWAP ${vwap:.2f}  |  Gap ${gap:+.2f}  |  VIX {vix:.2f}')
    print(hr('-'))

    # ── Line 5 — entry checklist ───────────────────────────────────────────────
    signal_tag = '  *** ALL CONDITIONS MET — ENTRY SIGNAL ***' if all_ok else ''
    print(f'  ENTRY CHECKLIST{signal_tag}')
    for ok, label, val in checks:
        print(f'    [{mk(ok)}]  {label:<38} {val}')
    print(hr('-'))

    # ── Line 6 — open position ─────────────────────────────────────────────────
    if position:
        held = position.held_min(now)
        pnl  = position.pnl()
        pct  = (position.cur_price / position.entry_ask - 1) * 100
        smart = position.smart_status(now)
        print(f'  OPEN POSITION : CALL  {position.ticker}')
        print(f'    Entry  ${position.entry_ask:.4f}  |  Current ${position.cur_price:.4f}'
              f'  |  Peak ${position.peak_price:.4f}  |  P&L ${pnl:+.2f} ({pct:+.1f}%)')
        print(f'    Target ${position.target:.4f}  |  Stop   ${position.stop:.4f}'
              f'  |  Held {held:.0f}m  |  Smart: {smart}')
    else:
        print(f'  OPEN POSITION : None')
    print(hr('-'))

    # ── Line 7 — daily summary ─────────────────────────────────────────────────
    print(f'  TODAY         : entries: {daily_entries}  |  wins: {daily_wins}'
          f'  |  losses: {daily_losses}  |  P&L: ${daily_pnl:+.2f}')

    # ── Line 8 — running lifetime stats + Kelly ────────────────────────────────
    print(f'  LIFETIME      : WR: {rstats.wr_str()}  |  trades: {rstats.total}'
          f'  |  Kelly rec: {rstats.kelly_str()}')
    print(hr())

# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    validate_env()

    print(f'\n{hr()}')
    print(f'  {STRATEGY_NAME}')
    print(f'  {EDGE_SUMMARY}')
    print(f'  Log    : {CSV_PATH}')
    print(f'  Poll   : every {POLL_SEC}s')
    print(f'  Config : {CONTRACTS} contracts | ${MAX_COST:.0f}/contract max'
          f' | {MAX_TRADES_DAY} trades/day | ${MAX_DAILY_LOSS:.0f} daily loss limit')
    print(hr())

    # Load running lifetime stats from CSV
    rstats = RunningStats()
    rstats.load_csv()
    print(f'  Lifetime stats loaded: {rstats.total} trades, WR {rstats.wr_str()}, Kelly {rstats.kelly_str()}')

    # Recover today's state from CSV
    daily_pnl, daily_entries, daily_wins, daily_losses, last_trade_time = recover_daily_stats()
    if daily_entries:
        print(f'  Recovered today : {daily_entries} trade(s)  P&L ${daily_pnl:+.2f}')
    print()

    position:   Optional[Position] = None
    bars:       list = []
    vwap:       float = 0.0
    vix:        float = 0.0
    spy:        float = 0.0
    last_date:  Optional[str] = None

    # Morning bias state (reset each day)
    daily_bias:  Optional[str] = None   # 'call' | 'skip' (no puts in this strategy)
    bias_set:    bool = False
    bias_gap:    float = 0.0

    # Entry checklist cache for dashboard
    last_checks: list = []
    last_all_ok: bool = False

    while True:
        now   = now_et()
        today = now.date().isoformat()

        # ── Daily reset ───────────────────────────────────────────────────────
        if today != last_date:
            if last_date is not None:
                # New day — flush daily counters
                daily_pnl     = 0.0
                daily_entries = 0
                daily_wins    = 0
                daily_losses  = 0
                last_trade_time = None
                bars          = []

            daily_bias = None
            bias_set   = False
            bias_gap   = 0.0
            last_checks = []
            last_all_ok = False

            dow = now.weekday()
            if dow not in TRADE_WDAYS:
                daily_bias = 'skip'
                bias_set   = True
                print(f'  [{today}] {DOW_NAMES[dow]} — not a trading day, all entries skipped')

            last_date = today
            # Log Kelly recommendation each new day
            k = rstats.kelly
            if k is not None:
                print(f'  [Kelly] Recommendation: {k:.1f}% of capital per trade'
                      f'  (tracking only — not yet acted on)')

        # ── Outside market hours — brief status line, then sleep ──────────────
        if not in_market_hours(now):
            label = 'pre-market' if now.hour < 12 else 'after hours'
            print(f'\r  [{now.strftime("%H:%M:%S")}] {label} — next check in {POLL_SEC}s   ',
                  end='', flush=True)
            time.sleep(POLL_SEC)
            continue

        try:
            # ── Fetch data ────────────────────────────────────────────────────
            bars = fetch_spy_bars(today)
            if not bars:
                print(f'  [{now.strftime("%H:%M")}] No bars yet — waiting...')
                time.sleep(POLL_SEC)
                continue

            spy  = bars[-1]['c']
            vwap = calc_vwap(bars)
            vix  = fetch_vix(today)

            # ── Morning bias lock at 9:45 ─────────────────────────────────────
            if not bias_set and or_complete(now) and len(bars) >= OR_MIN:
                vwap_30m = calc_vwap(bars[:OR_MIN])
                bias_gap = spy - vwap_30m
                if bias_gap >= BIAS_MIN_SEP:
                    daily_bias = 'call'
                    print(f'  [BIAS] CONFIRMED CALL  SPY ${spy:.2f}  30m-VWAP ${vwap_30m:.2f}'
                          f'  gap +${bias_gap:.2f}')
                else:
                    daily_bias = 'skip'
                    print(f'  [BIAS] NO BIAS — gap ${bias_gap:+.2f} (need +${BIAS_MIN_SEP:.2f})'
                          f' — skip day, no trades')
                bias_set = True

            # ── Determine day/vix skip state for dashboard ────────────────────
            is_tday = now.weekday() in TRADE_WDAYS
            skip_rsn = (f'Not a trading day ({DOW_NAMES[now.weekday()]})'
                        if not is_tday else None)
            vix_skip = is_tday and vix >= VIX_MAX

            # ── Entry checklist (always compute for dashboard) ────────────────
            last_all_ok, last_checks, vol_r = check_entry(
                bars, vwap, vix, now, daily_bias, position,
                last_trade_time, daily_pnl, daily_entries)

            # ── Manage open position ──────────────────────────────────────────
            if position is not None:
                cur = fetch_option_ask(position.ticker)
                position.update(cur)
                should_exit, reason = position.check_exit(now, bars)
                if should_exit:
                    exit_px = position.cur_price
                    pnl     = log_trade(position, exit_px, now, reason, rstats.wr_str())
                    rstats.record(pnl)
                    daily_pnl       += pnl
                    last_trade_time  = now
                    if pnl > 0:
                        daily_wins  += 1
                    else:
                        daily_losses += 1
                    print(f'\n  [EXIT] CALL {position.ticker}'
                          f'  entry ${position.entry_ask:.4f}  exit ${exit_px:.4f}'
                          f'  P&L ${pnl:+.2f}  hold {position.held_min(now):.0f}m'
                          f'  reason: {reason}')
                    position = None

            # ── Entry ─────────────────────────────────────────────────────────
            if last_all_ok and position is None and not past_cutoff(now):
                try:
                    info = build_call_info(spy)
                    ask  = info['ask']
                    cost_total = ask * 100 * CONTRACTS
                    if ask * 100 <= MAX_COST:
                        position = Position(
                            ticker    = info['ticker'],
                            ask       = ask,
                            spy       = spy,
                            vwap_val  = vwap,
                            gap       = spy - vwap,
                            vol_ratio = vol_r,
                            vix       = vix,
                            entry_t   = now,
                        )
                        daily_entries += 1
                        last_trade_time = now
                        print(f'\n  [ENTER] CALL {info["ticker"]}'
                              f'  strike ${info["strike"]:.0f}  ask ${ask:.4f}'
                              f'  cost ${cost_total:.2f} ({CONTRACTS} contracts)'
                              f'  SPY ${spy:.2f}  VIX {vix:.2f}'
                              f'  target ${position.target:.4f}  stop ${position.stop:.4f}')
                    else:
                        print(f'  [SKIP ENTRY] ask ${ask:.4f} -> ${ask*100:.2f}/contract'
                              f' > ${MAX_COST:.0f} cap')
                except Exception as e:
                    print(f'  [SKIP ENTRY] options chain error: {e}')

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f'  [ERROR] {e}')

        # ── Dashboard ─────────────────────────────────────────────────────────
        print_dashboard(
            now             = now,
            spy             = spy,
            vwap            = vwap,
            vix             = vix,
            daily_bias      = daily_bias,
            bias_gap        = bias_gap,
            bias_set        = bias_set,
            checks          = last_checks,
            all_ok          = last_all_ok,
            position        = position,
            daily_pnl       = daily_pnl,
            daily_entries   = daily_entries,
            daily_wins      = daily_wins,
            daily_losses    = daily_losses,
            rstats          = rstats,
            is_trading_day  = now.weekday() in TRADE_WDAYS,
            skip_day_reason = (f'Not a trading day ({DOW_NAMES[now.weekday()]})'
                               if now.weekday() not in TRADE_WDAYS else None),
            vix_skip        = now.weekday() in TRADE_WDAYS and vix >= VIX_MAX,
        )

        try:
            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            print('\nStopped.')
            break


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.')
