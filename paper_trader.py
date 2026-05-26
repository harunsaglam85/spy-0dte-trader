#!/usr/bin/env python3
"""
SPY 0DTE Paper Trading System
Market data: Polygon.io  |  Options chain: Tastytrade  |  Execution: paper
"""

import csv
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
import requests
from dotenv import load_dotenv

# --- Credentials -------------------------------------------------------------
load_dotenv(r'C:\Users\sagla\.tastytrade-mcp\.env')

def _s(v: Optional[str]) -> str:
    return (v or '').strip("'\"")

POLYGON_KEY = _s(os.getenv('POLYGON_API_KEY'))
TT_SECRET   = _s(os.getenv('TASTYTRADE_CLIENT_SECRET'))
TT_REFRESH  = _s(os.getenv('TASTYTRADE_REFRESH_TOKEN'))
TT_ACCOUNT  = _s(os.getenv('TASTYTRADE_ACCOUNT_ID'))
USE_PROD    = _s(os.getenv('TASTYTRADE_USE_PRODUCTION', 'true')).lower() == 'true'
TT_BASE     = 'https://api.tastytrade.com' if USE_PROD else 'https://api.cert.tastytrade.com'
POLY_BASE   = 'https://api.polygon.io'
ET          = pytz.timezone('America/New_York')

# --- Strategy parameters -----------------------------------------------------
OR_MIN        = 15      # opening range: first N minutes of trading
VOL_PERIODS   = 20      # candles for volume average
VOL_THRESH    = 1.5     # 50% above 20-bar average
VWAP_CONSEC   = 2       # consecutive candles above/below VWAP required
TREND_CONSEC  = 3       # consecutive directional candles required
MAX_COST      = 500.0   # max total premium per trade ($)
TARGET_MULT   = 2.0     # 2x entry
STOP_MULT     = 0.50    # 50% of entry
MAX_HOLD_MIN  = 60      # time stop in minutes
NO_MOVE_MIN   = 20      # no-movement window in minutes
NO_MOVE_PCT   = 0.10    # must gain >=10% within NO_MOVE_MIN to avoid exit
VIX_CALL_MAX  = 20.0    # VIX threshold for calls; puts: any VIX
SCORE_NEEDED  = 6

WINDOWS = [((9, 45), (12, 0)), ((14, 0), (15, 15))]  # ET entry windows
CUTOFF  = (15, 15)   # hard entry cutoff: 3:15 PM

CSV_PATH = Path(r'C:\Users\sagla\paper_trades.csv')
POLL_SEC = 60

# --- Env validation ----------------------------------------------------------
def validate_env():
    bad = [k for k, v in {
        'POLYGON_API_KEY':             POLYGON_KEY,
        'TASTYTRADE_CLIENT_SECRET':    TT_SECRET,
        'TASTYTRADE_REFRESH_TOKEN':    TT_REFRESH,
        'TASTYTRADE_ACCOUNT_ID':       TT_ACCOUNT,
    }.items() if not v]
    if bad:
        sys.exit(f'Missing .env vars: {", ".join(bad)}')

# --- Time helpers -------------------------------------------------------------
def now_et() -> datetime:
    return datetime.now(ET)

def in_market_hours(t: datetime) -> bool:
    if t.weekday() >= 5:
        return False
    return (9, 30) <= (t.hour, t.minute) < (16, 0)

def in_trade_window(t: datetime) -> bool:
    hm = (t.hour, t.minute)
    return any(s <= hm < e for s, e in WINDOWS)

def past_cutoff(t: datetime) -> bool:
    return (t.hour, t.minute) >= CUTOFF

def or_complete(t: datetime) -> bool:
    return (t.hour, t.minute) >= (9, 30 + OR_MIN)

# --- Tastytrade auth ----------------------------------------------------------
_tt_tok: Optional[str] = None
_tt_exp: float = 0.0

def tt_token() -> str:
    global _tt_tok, _tt_exp
    if _tt_tok and time.time() < _tt_exp:
        return _tt_tok
    r = requests.post(
        f'{TT_BASE}/oauth/token',
        data={'grant_type': 'refresh_token', 'refresh_token': TT_REFRESH, 'client_secret': TT_SECRET},
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f'TT auth {r.status_code}: {r.text[:200]}')
    d = r.json()
    tok = d.get('access_token') or d.get('session-token') or (d.get('data') or {}).get('session-token')
    if not tok:
        raise RuntimeError(f'No token in TT response: {d}')
    _tt_tok, _tt_exp = tok, time.time() + 3500
    return tok

def tt_get(path: str, **params) -> dict:
    r = requests.get(f'{TT_BASE}{path}',
                     headers={'Authorization': f'Bearer {tt_token()}'},
                     params=params, timeout=15)
    if not r.ok:
        raise RuntimeError(f'TT GET {path} -> {r.status_code}: {r.text[:200]}')
    return r.json()

# --- Polygon helpers ----------------------------------------------------------
def poly_get(path: str, **params) -> dict:
    r = requests.get(f'{POLY_BASE}{path}',
                     params={'apiKey': POLYGON_KEY, **params}, timeout=15)
    if not r.ok:
        raise RuntimeError(f'Poly GET {path} -> {r.status_code}: {r.text[:200]}')
    return r.json()

# --- Market data -------------------------------------------------------------
def fetch_spy_bars(today: str) -> list[dict]:
    d = poly_get(f'/v2/aggs/ticker/SPY/range/1/minute/{today}/{today}',
                 adjusted='true', sort='asc', limit=500)
    return d.get('results', [])

_vix_cache: tuple[float, float] = (0.0, 0.0)   # (value, fetch_epoch)

def fetch_vix(today: str) -> float:
    global _vix_cache
    if time.time() - _vix_cache[1] < 120:        # cache 2 min
        return _vix_cache[0]
    try:
        d = poly_get(f'/v2/aggs/ticker/I:VIX/range/1/minute/{today}/{today}',
                     adjusted='false', sort='desc', limit=1)
        bars = d.get('results', [])
        val = float(bars[0]['c']) if bars else 0.0
    except Exception:
        val = _vix_cache[0]
    _vix_cache = (val, time.time())
    return val

def fetch_option_ask(opt_ticker: str) -> float:
    """Best ask for a Polygon option ticker (O:SPY...), falls back to last trade."""
    try:
        d = poly_get(f'/v3/snapshot/options/SPY/{opt_ticker}')
        res = d.get('results', {})
        ask = (res.get('last_quote') or {}).get('ask')
        if not ask:
            ask = (res.get('day') or {}).get('close', 0)
        return float(ask or 0)
    except Exception:
        return 0.0

# --- Indicators --------------------------------------------------------------
def calc_vwap(bars: list[dict]) -> float:
    if not bars:
        return 0.0
    tpv = sum(((b['h'] + b['l'] + b['c']) / 3) * b['v'] for b in bars)
    vol = sum(b['v'] for b in bars)
    return tpv / vol if vol else 0.0

def calc_opening_range(bars: list[dict]) -> tuple[float, float]:
    if not bars:
        return 0.0, 0.0
    t0  = bars[0]['t']
    cut = t0 + OR_MIN * 60_000
    ob  = [b for b in bars if b['t'] < cut]
    src = ob if ob else bars[:1]
    return max(b['h'] for b in src), min(b['l'] for b in src)

def calc_vol_avg(bars: list[dict]) -> float:
    """Rolling average of the preceding VOL_PERIODS candles (excludes current bar)."""
    hist = bars[-(VOL_PERIODS + 1):-1]
    return sum(b['v'] for b in hist) / len(hist) if hist else 0.0

# --- OR break-and-retest state machine ---------------------------------------
class ORTracker:
    TOL = 0.002  # 0.2% price tolerance for retest touch

    def __init__(self):
        self.break_dir: Optional[str] = None   # 'up' | 'down'
        self.retested  = False
        self.status    = 'watching'

    def reset(self):
        self.__init__()

    def update(self, bars: list[dict], or_h: float, or_l: float):
        if not bars or not or_h:
            return
        b  = bars[-1]
        c, hi, lo = b['c'], b['h'], b['l']
        if self.break_dir is None:
            if c > or_h:
                self.break_dir, self.status = 'up',   'broken-up'
            elif c < or_l:
                self.break_dir, self.status = 'down', 'broken-down'
            return
        if not self.retested:
            if self.break_dir == 'up'   and lo <= or_h * (1 + self.TOL):
                self.retested, self.status = True, 'retested-up'
            elif self.break_dir == 'down' and hi >= or_l * (1 - self.TOL):
                self.retested, self.status = True, 'retested-down'

    def satisfied(self, direction: str) -> bool:
        return self.retested and (
            (direction == 'call' and self.break_dir == 'up') or
            (direction == 'put'  and self.break_dir == 'down')
        )

# --- Option ticker builder ---------------------------------------------------
def build_opt_ticker(exp: date, opt_type: str, strike: float) -> str:
    """OCC/Polygon format: O:SPY260524C00590000"""
    return f'O:SPY{exp.strftime("%y%m%d")}{"C" if opt_type == "call" else "P"}{int(strike * 1000):08d}'

def nearest_exp() -> date:
    d = date.today()
    return d if d.weekday() < 5 else d + timedelta(days=7 - d.weekday())

# --- Tastytrade options chain ------------------------------------------------
def get_tt_chain(spy_price: float) -> dict:
    """Fetch nearest expiration ATM strike from Tastytrade nested chain."""
    try:
        d    = tt_get('/option-chains/SPY/nested')
        data = d.get('data', {})
        items = data.get('items', [])
        if not items:
            return {}
        chain = items[0]
        exps  = chain.get('expirations', [])
        if not exps:
            return {}
        today_s  = date.today().isoformat()
        exp_data = next((e for e in exps if e.get('expiration-date') == today_s), exps[0])
        exp_date = exp_data.get('expiration-date', today_s)
        strikes  = exp_data.get('strikes', [])
        if not strikes:
            return {}
        atm = min(strikes, key=lambda s: abs(float(s.get('strike-price', 0)) - spy_price))
        return {
            'expiration': exp_date,
            'strike':     float(atm.get('strike-price', round(spy_price))),
        }
    except Exception:
        return {}

def build_opt_info(spy_price: float) -> dict:
    chain  = get_tt_chain(spy_price)
    exp    = date.fromisoformat(chain['expiration']) if chain.get('expiration') else nearest_exp()
    strike = chain.get('strike', round(spy_price))
    c_tick = build_opt_ticker(exp, 'call', strike)
    p_tick = build_opt_ticker(exp, 'put',  strike)
    c_ask  = fetch_option_ask(c_tick)
    p_ask  = fetch_option_ask(p_tick)
    return {
        'expiration':   exp.isoformat(),
        'strike':       strike,
        'call_ticker':  c_tick,
        'put_ticker':   p_tick,
        'call_ask':     c_ask,
        'put_ask':      p_ask,
    }

# --- Signal scoring ----------------------------------------------------------
def score_conditions(
    bars:       list[dict],
    vwap:       float,
    or_h:       float,
    or_l:       float,
    or_tracker: ORTracker,
    vix:        float,
    direction:  str,
    now:        datetime,
) -> tuple[int, list[tuple[bool, str]]]:

    checks: list[tuple[bool, str]] = []

    # 1. VWAP x VWAP_CONSEC consecutive candles
    side = 'above' if direction == 'call' else 'below'
    if len(bars) >= VWAP_CONSEC:
        recent = bars[-VWAP_CONSEC:]
        ok1 = all(b['c'] > vwap for b in recent) if direction == 'call' \
              else all(b['c'] < vwap for b in recent)
        last_c = [f'${b["c"]:.2f}' for b in recent]
    else:
        ok1, last_c = False, []
    checks.append((ok1,
        f'SPY {side} VWAP x{VWAP_CONSEC}  [closes: {", ".join(last_c) or "N/A"}  VWAP ${vwap:.2f}]'))

    # 2. Opening range break + retest
    ok2 = or_tracker.satisfied(direction)
    checks.append((ok2,
        f'OR break+retest [{or_tracker.status}]  H:${or_h:.2f}  L:${or_l:.2f}'))

    # 3. Volume >= 50% above 20-bar average
    avg_v = calc_vol_avg(bars)
    cur_v = bars[-1]['v'] if bars else 0
    ratio = cur_v / avg_v if avg_v else 0.0
    ok3   = ratio >= VOL_THRESH
    checks.append((ok3,
        f'Volume >={VOL_THRESH:.1f}x avg  [{ratio:.2f}x  cur:{cur_v:,}  avg:{int(avg_v):,}]'))

    # 4. Time window
    ok4 = in_trade_window(now)
    checks.append((ok4,
        f'Time window [{now.strftime("%H:%M")} ET]  (9:45-12:00 | 14:00-15:15)'))

    # 5. VIX under 20 for calls; any value for puts
    if direction == 'call':
        ok5 = 0 < vix < VIX_CALL_MAX
        checks.append((ok5, f'VIX < {VIX_CALL_MAX} for calls  [VIX={vix:.2f}]'))
    else:
        ok5 = True
        checks.append((ok5, f'VIX any for puts  [VIX={vix:.2f}]'))

    # 6. TREND_CONSEC consecutive candles in trade direction
    if len(bars) >= TREND_CONSEC:
        recent3 = bars[-TREND_CONSEC:]
        ok6 = all(b['c'] > b['o'] for b in recent3) if direction == 'call' \
              else all(b['c'] < b['o'] for b in recent3)
        c3  = [f'{"+" if b["c"]>b["o"] else "-"}{abs(b["c"]-b["o"]):.2f}' for b in recent3]
    else:
        ok6, c3 = False, []
    label = 'bullish' if direction == 'call' else 'bearish'
    checks.append((ok6,
        f'3 consecutive {label} candles  [{", ".join(c3) or "N/A"}]'))

    return sum(1 for ok, _ in checks if ok), checks

# --- Position ----------------------------------------------------------------
class Position:
    def __init__(self, direction: str, ticker: str, ask: float, spy: float, t: datetime):
        self.direction  = direction
        self.ticker     = ticker
        self.entry_ask  = ask
        self.spy_entry  = spy
        self.entry_time = t
        self.target     = round(ask * TARGET_MULT, 4)
        self.stop       = round(ask * STOP_MULT,   4)
        self.cur_price  = ask
        self.peak_price = ask

    def update(self, price: float):
        if price > 0:
            self.cur_price  = price
            self.peak_price = max(self.peak_price, price)

    def pnl(self) -> float:
        return (self.cur_price - self.entry_ask) * 100   # 1 contract x 100 multiplier

    def held_min(self, t: datetime) -> float:
        return (t - self.entry_time).total_seconds() / 60

    def check_exit(self, t: datetime) -> tuple[bool, str]:
        held = self.held_min(t)
        if past_cutoff(t):
            return True, '3:15 PM hard cutoff'
        if held >= MAX_HOLD_MIN:
            return True, f'{MAX_HOLD_MIN}-min time stop'
        if self.cur_price >= self.target:
            return True, f'2x target hit (${self.target:.4f})'
        if self.cur_price <= self.stop:
            return True, f'50% stop hit (${self.stop:.4f})'
        if held >= NO_MOVE_MIN and self.peak_price < self.entry_ask * (1 + NO_MOVE_PCT):
            return True, f'No movement >{NO_MOVE_PCT*100:.0f}% in {NO_MOVE_MIN} min'
        return False, ''

# --- CSV log -----------------------------------------------------------------
HEADERS = ['date', 'entry_time', 'exit_time', 'direction', 'opt_ticker',
           'entry_ask', 'exit_price', 'contracts', 'pnl_dollars', 'exit_reason', 'score']

def log_trade(pos: Position, exit_px: float, exit_t: datetime, reason: str, score: int):
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        if new_file:
            w.writeheader()
        w.writerow({
            'date':        pos.entry_time.date().isoformat(),
            'entry_time':  pos.entry_time.strftime('%H:%M:%S'),
            'exit_time':   exit_t.strftime('%H:%M:%S'),
            'direction':   pos.direction,
            'opt_ticker':  pos.ticker,
            'entry_ask':   f'{pos.entry_ask:.4f}',
            'exit_price':  f'{exit_px:.4f}',
            'contracts':   1,
            'pnl_dollars': f'{(exit_px - pos.entry_ask) * 100:.2f}',
            'exit_reason': reason,
            'score':       score,
        })

# --- Dashboard ---------------------------------------------------------------
W = 66

def hr(c='='): return c * W

def print_dashboard(now, spy, vwap, or_h, or_l, vix,
                    score, checks, pos, daily_pnl, trade_count, opt, direction):
    def mk(ok): return 'YES' if ok else ' NO'
    spy_diff = ((spy - vwap) / vwap * 100) if vwap else 0.0
    exp    = opt.get('expiration', 'N/A')
    strike = opt.get('strike', 0)
    c_ask  = opt.get('call_ask', 0.0)
    p_ask  = opt.get('put_ask',  0.0)
    signal = '  *** SIGNAL ***' if score >= SCORE_NEEDED else ''

    print(f'\n{hr()}')
    print(f'  PAPER TRADING DASHBOARD     {now.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print(hr())
    print(f'  SPY    : ${spy:>8.2f}   ({spy_diff:+.2f}% vs VWAP)   Bias: {direction.upper()}')
    print(f'  VWAP   : ${vwap:>8.2f}')
    print(f'  OR     :  H ${or_h:.2f}  /  L ${or_l:.2f}')
    print(f'  VIX    : {vix:>6.2f}')
    print(f'  Chain  : {exp}  ${strike:.0f} strike  |  C ask ${c_ask:.2f}  P ask ${p_ask:.2f}')
    print(hr('-'))
    print(f'  CHECKLIST  [{score}/{SCORE_NEEDED}]{signal}')
    for ok, desc in checks:
        print(f'    [{mk(ok)}] {desc}')
    print(hr('-'))
    if pos:
        held = pos.held_min(now)
        pnl  = pos.pnl()
        print(f'  OPEN POSITION : {pos.direction.upper()}  {pos.ticker}')
        print(f'    Entry   : ${pos.entry_ask:.4f}  ->  Current ${pos.cur_price:.4f}  (peak ${pos.peak_price:.4f})')
        print(f'    P&L     : ${pnl:+.2f}   target ${pos.target:.4f}   stop ${pos.stop:.4f}')
        print(f'    Held    : {held:.0f} min   SPY entry ${pos.spy_entry:.2f}')
    else:
        print(f'  OPEN POSITION : None')
    print(hr('-'))
    print(f'  TODAY\'S P&L : ${daily_pnl:+.2f}   ({trade_count} closed trade{"s" if trade_count != 1 else ""})')
    print(hr())

# --- Main loop ---------------------------------------------------------------
def main():
    validate_env()
    print('SPY Paper Trader')
    print(f'  Polygon key : {POLYGON_KEY[:8]}...')
    print(f'  TT account  : {TT_ACCOUNT}')
    print(f'  Log         : {CSV_PATH}')
    print(f'  Poll        : every {POLL_SEC}s')
    print(f'  Max cost    : ${MAX_COST:.0f}/trade   Target: {TARGET_MULT}x   Stop: {STOP_MULT*100:.0f}%')
    print()

    or_tracker = ORTracker()
    position: Optional[Position] = None
    daily_pnl   = 0.0
    trade_count = 0
    last_score  = 0
    last_checks: list[tuple[bool, str]] = []
    last_opt:    dict = {}
    last_dir     = 'call'
    bars:        list[dict] = []
    vwap = or_h = or_l = vix = spy = 0.0
    last_date: Optional[str] = None

    while True:
        now   = now_et()
        today = now.date().isoformat()

        # Daily reset
        if today != last_date:
            if last_date is not None:
                daily_pnl   = 0.0
                trade_count = 0
            or_tracker.reset()
            last_date = today

        if not in_market_hours(now):
            label = 'pre-market' if now.hour < 12 else 'after hours'
            print(f'\r[{now.strftime("%H:%M:%S")} ET] {label} -- next check in {POLL_SEC}s.   ',
                  end='', flush=True)
            time.sleep(POLL_SEC)
            continue

        try:
            # Fetch bars
            bars = fetch_spy_bars(today)
            if not bars:
                print(f'[{now.strftime("%H:%M")}] No bars yet...')
                time.sleep(POLL_SEC)
                continue

            spy  = bars[-1]['c']
            vwap = calc_vwap(bars)
            or_h, or_l = calc_opening_range(bars)

            if or_complete(now):
                or_tracker.update(bars, or_h, or_l)

            vix = fetch_vix(today)
            direction = 'call' if spy >= vwap else 'put'
            last_dir  = direction

            # Score
            score, checks = score_conditions(bars, vwap, or_h, or_l,
                                             or_tracker, vix, direction, now)
            last_score  = score
            last_checks = checks

            # Options chain + ask prices
            try:
                last_opt = build_opt_info(spy)
            except Exception as e:
                print(f'  [options] {e}')

            # Manage open position
            if position:
                cur = fetch_option_ask(position.ticker)
                position.update(cur)
                should_exit, reason = position.check_exit(now)
                if should_exit:
                    exit_px = position.cur_price
                    pnl     = position.pnl()
                    log_trade(position, exit_px, now, reason, last_score)
                    daily_pnl  += pnl
                    trade_count += 1
                    print(f'\n  [EXIT] {position.direction.upper()} @ ${exit_px:.4f}'
                          f'  P&L ${pnl:+.2f}  Reason: {reason}')
                    position = None

            # Entry
            if (not position
                    and score >= SCORE_NEEDED
                    and in_trade_window(now)
                    and not past_cutoff(now)):
                ask    = last_opt.get(f'{direction}_ask', 0.0)
                ticker = last_opt.get(f'{direction}_ticker', '')
                cost   = ask * 100   # 1 contract x 100 multiplier
                if 0 < cost <= MAX_COST and ticker:
                    position = Position(direction, ticker, ask, spy, now)
                    print(f'\n  [ENTER] {direction.upper()} {ticker}'
                          f' @ ${ask:.4f}  cost ${cost:.2f}'
                          f'  score {score}/{SCORE_NEEDED}  SPY ${spy:.2f}')
                elif score >= SCORE_NEEDED:
                    print(f'  [SKIP] Signal 6/6 but option ask ${ask:.2f} (${cost:.0f}) > ${MAX_COST:.0f} cap')

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f'  [ERROR] {e}')

        # Dashboard
        print_dashboard(now, spy, vwap, or_h, or_l, vix,
                        last_score, last_checks, position,
                        daily_pnl, trade_count, last_opt, last_dir)

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
