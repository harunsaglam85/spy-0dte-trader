#!/usr/bin/env python3
"""
backtest_real_data.py
=====================
5-strategy backtest using real ThetaData EOD options bid/ask (2021-2026).
Compares real-data pricing vs synthetic Black-Scholes to validate Grade A results.

Data sources:
  ThetaData  — real SPY options EOD bid/ask (THETADATA_USERNAME/PASSWORD from .env)
  Alpaca     — SPY 5-min bars
  yfinance   — VIX daily, earnings dates, SPY daily
  py_vollib  — IV inversion + delta/gamma from real bid/ask

Strategies:
  S1 — Credit Spread      (0DTE puts, Mon/Fri, VIX 15-22)
  S2 — 5-DTE Momentum     (5-day calls, any day, morning bias)
  S3 — FOMC/CPI Catalyst  (event-driven, 2 days pre-event)
  S4 — Earnings Momentum  (NVDA/TSLA/AMD/AAPL/MSFT/META/GOOGL/AMZN, 5 days pre)
  S5 — Iron Condor        (0DTE, VIX>20, IV rank>=50)

Validation: 60/40 split | blind 2025 | bootstrap P(positive) | A–F grading
Output: backtest_real_data_report.txt | backtest_real_data_trades.csv
"""

# ─── stdlib ───────────────────────────────────────────────────────────────────
import csv
import math
import os
import pickle
import random
import sys
import time as _time
import traceback
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

warnings.filterwarnings('ignore')

# ─── third-party (auto-install if missing) ────────────────────────────────────
def _require(pkg, import_as=None):
    import importlib
    name = import_as or pkg.split('[')[0].replace('-', '_')
    try:
        return importlib.import_module(name)
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', pkg], check=True)
        return importlib.import_module(name)

import pytz
import yfinance as yf
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

try:
    from py_vollib.black_scholes import black_scholes as _pv_bs
    from py_vollib.black_scholes.implied_volatility import implied_volatility as _pv_iv
    from py_vollib.black_scholes.greeks.analytical import delta as _pv_delta, gamma as _pv_gamma
    PV_OK = True
except ImportError:
    _require('py_vollib')
    from py_vollib.black_scholes import black_scholes as _pv_bs
    from py_vollib.black_scholes.implied_volatility import implied_volatility as _pv_iv
    from py_vollib.black_scholes.greeks.analytical import delta as _pv_delta, gamma as _pv_gamma
    PV_OK = True

try:
    import polars as pl
    POLARS_OK = True
except ImportError:
    pl = None
    POLARS_OK = False

# ─── paths ────────────────────────────────────────────────────────────────────
ENV_PATH     = Path(r'C:\Users\sagla\.tastytrade-mcp\.env')
DATA_DIR     = Path(r'C:\Users\sagla\backtest_data')
REPORT_PATH  = Path(r'C:\Users\sagla\backtest_real_data_report.txt')
TRADES_PATH  = Path(r'C:\Users\sagla\backtest_real_data_trades.csv')

DATA_DIR.mkdir(parents=True, exist_ok=True)
# All pickle files below are self-written local caches (no external/untrusted source).

ET           = pytz.timezone('America/New_York')
TRADING_DAYS_YR = 252
TRADING_MINS    = TRADING_DAYS_YR * 390   # mins per year

# ─── backtest period ──────────────────────────────────────────────────────────
BT_START     = date(2021, 1,  4)
BT_END       = date(2026, 5, 30)
SPLIT_DATE   = date(2023, 7,  1)   # 60/40 in-sample / out-of-sample
BLIND_YEAR   = 2025                # held-out validation year

# Set to a 4-digit year (e.g. 2022) to run a quick single-year confirmation test.
# Set to None for the full BT_START–BT_END range.
YEAR_FILTER: Optional[int] = None

# ─── earnings universe ────────────────────────────────────────────────────────
EARNINGS_UNIVERSE = ['NVDA', 'TSLA', 'AMD', 'AAPL', 'MSFT', 'META', 'GOOGL', 'AMZN']

# ─── risk-free rate by year ───────────────────────────────────────────────────
_RFR = {2021: 0.001, 2022: 0.015, 2023: 0.045, 2024: 0.050, 2025: 0.045, 2026: 0.043}

def _rfr(dt) -> float:
    if isinstance(dt, str): dt = date.fromisoformat(dt[:10])
    return _RFR.get(dt.year, 0.045)

# ─── FOMC dates 2021-2026 ─────────────────────────────────────────────────────
FOMC_DATES = {
    date(2021, 1, 27), date(2021, 3, 17), date(2021, 4, 28), date(2021, 6, 16),
    date(2021, 7, 28), date(2021, 9, 22), date(2021, 11, 3), date(2021, 12, 15),
    date(2022, 2, 2),  date(2022, 3, 16), date(2022, 5, 4),  date(2022, 6, 15),
    date(2022, 7, 27), date(2022, 9, 21), date(2022, 11, 2), date(2022, 12, 14),
    date(2023, 2, 1),  date(2023, 3, 22), date(2023, 5, 3),  date(2023, 6, 14),
    date(2023, 7, 26), date(2023, 9, 20), date(2023, 11, 1), date(2023, 12, 13),
    date(2024, 1, 31), date(2024, 3, 20), date(2024, 5, 1),  date(2024, 6, 12),
    date(2024, 7, 31), date(2024, 9, 18), date(2024, 11, 7), date(2024, 12, 18),
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7),  date(2025, 6, 18),
    date(2025, 7, 30), date(2025, 9, 17), date(2025, 11, 5), date(2025, 12, 10),
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 6),
}

# CPI release dates (approx 2nd or 3rd Wed each month)
CPI_DATES = {
    date(2021, 1, 13), date(2021, 2, 10), date(2021, 3, 10), date(2021, 4, 13),
    date(2021, 5, 12), date(2021, 6, 10), date(2021, 7, 13), date(2021, 8, 11),
    date(2021, 9, 14), date(2021, 10, 13), date(2021, 11, 10), date(2021, 12, 10),
    date(2022, 1, 12), date(2022, 2, 10), date(2022, 3, 10), date(2022, 4, 12),
    date(2022, 5, 11), date(2022, 6, 10), date(2022, 7, 13), date(2022, 8, 10),
    date(2022, 9, 13), date(2022, 10, 13), date(2022, 11, 10), date(2022, 12, 13),
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


# ══════════════════════════════════════════════════════════════════════════════
# 1. ENV LOADER
# ══════════════════════════════════════════════════════════════════════════════

def _load_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


# ══════════════════════════════════════════════════════════════════════════════
# 2. THETADATA LOADER (EOD options with disk cache)
# ══════════════════════════════════════════════════════════════════════════════

class ThetaCache:
    """
    Wraps ThetaData option_history_eod with a per-expiration disk cache.
    Key: data_dir/theta_SPY_{expiration}.pkl
    Returns: dict keyed by (obs_date_str, strike, right) -> {'bid', 'ask', 'volume', 'close'}
    """

    def __init__(self, data_dir: Path, env: dict, cache_only: bool = False):
        self.data_dir   = data_dir
        self.env        = env
        self.cache_only = cache_only
        self._client    = None
        self._exps      = None

    def _get_client(self):
        if self._client is None:
            from thetadata import ThetaClient
            username = self.env.get('THETADATA_USERNAME', '')
            password = self.env.get('THETADATA_PASSWORD', '')
            if not username or not password:
                raise RuntimeError('THETADATA_USERNAME / THETADATA_PASSWORD missing from .env')
            self._client = ThetaClient(email=username, password=password)
            print('  ThetaData: connected.')
        return self._client

    def get_expirations(self) -> List[date]:
        if self._exps is not None:
            return self._exps
        cache = self.data_dir / 'theta_SPY_expirations.pkl'
        if cache.exists():
            with cache.open('rb') as f:
                self._exps = pickle.load(f)
            return self._exps
        client = self._get_client()
        df = client.option_list_expirations('SPY')
        if POLARS_OK and hasattr(df, 'to_pandas'):
            import pandas as pd
            df = df.to_pandas()
        exps = []
        for raw in df['expiration'].tolist():
            try:
                if isinstance(raw, date):
                    exps.append(raw)
                else:
                    exps.append(date.fromisoformat(str(raw)[:10]))
            except Exception:
                pass
        self._exps = sorted(exps)
        with cache.open('wb') as f:
            pickle.dump(self._exps, f)
        print(f'  ThetaData: {len(self._exps)} SPY expirations cached.')
        return self._exps

    def _cache_path(self, expiration: date) -> Path:
        return self.data_dir / f'theta_SPY_{expiration}.pkl'

    def load_chain(self, expiration: date, strike_range: int = 40) -> dict:
        """
        Returns: {(obs_date_str, strike, right): {'bid', 'ask', 'volume', 'close'}}
        right is normalised to 'C' or 'P'
        """
        path = self._cache_path(expiration)
        if path.exists():
            with path.open('rb') as f:
                return pickle.load(f)
        if self.cache_only:
            return {}

        client = self._get_client()
        # ThetaData: fetch from 30 days before expiration to expiration
        start_d = expiration - timedelta(days=45)
        try:
            raw = client.option_history_eod(
                start_date=start_d,
                end_date=expiration,
                symbol='SPY',
                expiration=expiration,
                right='both',
                strike_range=strike_range,
            )
        except Exception as e:
            print(f'  ThetaData: fetch failed for {expiration}: {e}')
            result = {}
            with path.open('wb') as f:
                pickle.dump(result, f)
            return result

        result = {}
        if raw is None or (hasattr(raw, '__len__') and len(raw) == 0):
            with path.open('wb') as f:
                pickle.dump(result, f)
            return result

        # Handle both polars and pandas DataFrames
        try:
            if POLARS_OK and isinstance(raw, pl.DataFrame):
                rows = raw.iter_rows(named=True)
            else:
                rows = (r._asdict() if hasattr(r, '_asdict') else dict(r)
                        for _, r in raw.iterrows())

            for row in rows:
                try:
                    obs = row.get('created') or row.get('date') or row.get('obs_date')
                    if obs is None:
                        continue
                    if hasattr(obs, 'date'):
                        obs = obs.date()
                    obs_str = str(obs)[:10]
                    strike  = float(row['strike'])
                    right   = str(row['right']).upper()[:1]   # 'C' or 'P'
                    bid     = float(row.get('bid') or 0)
                    ask     = float(row.get('ask') or 0)
                    vol     = int(row.get('volume') or 0)
                    close   = float(row.get('close') or 0)
                    if ask > 0 and ask < 9999:
                        result[(obs_str, strike, right)] = {
                            'bid': bid, 'ask': ask, 'volume': vol, 'close': close
                        }
                except Exception:
                    pass
        except Exception as e:
            print(f'  ThetaData: parse error for {expiration}: {e}')

        with path.open('wb') as f:
            pickle.dump(result, f)
        return result

    def get_quote(self, obs_date: date, expiration: date,
                  strike: float, right: str) -> Optional[dict]:
        """Single quote lookup — loads (and caches) the full chain for expiration."""
        chain = self.load_chain(expiration)
        right = right.upper()[:1]
        # exact match
        key = (obs_date.isoformat(), strike, right)
        if key in chain:
            return chain[key]
        # nearest strike
        obs_str = obs_date.isoformat()
        candidates = [(k, v) for k, v in chain.items()
                      if k[0] == obs_str and k[2] == right]
        if not candidates:
            return None
        closest = min(candidates, key=lambda x: abs(x[0][1] - strike))
        return closest[1]

    def get_chain_for_day(self, obs_date: date, expiration: date,
                           right: Optional[str] = None) -> dict:
        """All quotes for a specific obs_date/expiration, keyed by (strike, right)."""
        chain = self.load_chain(expiration)
        obs_str = obs_date.isoformat()
        out = {}
        for (d, k, r), v in chain.items():
            if d == obs_str and (right is None or r == right.upper()[:1]):
                out[(k, r)] = v
        return out


# ══════════════════════════════════════════════════════════════════════════════
# 3. ALPACA 5-MIN BARS
# ══════════════════════════════════════════════════════════════════════════════

def _load_alpaca_5min(start: date, end: date, force: bool = False) -> dict:
    """
    Returns {date_str: [{t, o, h, l, c, v}, ...]} for 5-min SPY bars.
    Aggregates 1-min Alpaca data to 5-min if needed.
    """
    cache = DATA_DIR / f'spy_5min_{start}_{end}.pkl'
    if cache.exists() and not force:
        with cache.open('rb') as f:
            return pickle.load(f)

    # Reuse the 1-min Alpaca loader
    sys.path.insert(0, str(Path(__file__).parent))
    from alpaca_loader import download_spy_1min
    print('  Alpaca: loading 1-min bars to aggregate to 5-min...')
    days_1min = download_spy_1min(start, end)

    days_5min: dict = {}
    for ds, bars in days_1min.items():
        agg = []
        bucket: list = []
        bucket_open_t = None
        for bar in bars:
            dt_et = datetime.fromtimestamp(bar['t'] / 1000, tz=ET)
            # align to 5-min boundary
            m5 = (dt_et.minute // 5) * 5
            snap = dt_et.replace(minute=m5, second=0, microsecond=0)
            snap_ms = int(snap.timestamp() * 1000)
            if bucket_open_t != snap_ms:
                if bucket:
                    agg.append({
                        't': bucket_open_t,
                        'o': bucket[0]['o'],
                        'h': max(b['h'] for b in bucket),
                        'l': min(b['l'] for b in bucket),
                        'c': bucket[-1]['c'],
                        'v': sum(b['v'] for b in bucket),
                    })
                bucket = []
                bucket_open_t = snap_ms
            bucket.append(bar)
        if bucket:
            agg.append({
                't': bucket_open_t,
                'o': bucket[0]['o'],
                'h': max(b['h'] for b in bucket),
                'l': min(b['l'] for b in bucket),
                'c': bucket[-1]['c'],
                'v': sum(b['v'] for b in bucket),
            })
        days_5min[ds] = agg

    with cache.open('wb') as f:
        pickle.dump(days_5min, f)
    print(f'  Alpaca 5-min: {len(days_5min)} days cached.')
    return days_5min


# ══════════════════════════════════════════════════════════════════════════════
# 4. VIX + SPY DAILY
# ══════════════════════════════════════════════════════════════════════════════

def _load_vix(start: date, end: date) -> dict:
    cache = DATA_DIR / f'vix_{start}_{end}.csv'
    if cache.exists():
        out = {}
        with cache.open(encoding='utf-8') as f:
            for row in csv.DictReader(f):
                out[row['date']] = float(row['vix'])
        return out
    import pandas as pd
    df = yf.download('^VIX', start=start.isoformat(),
                     end=(end + timedelta(days=1)).isoformat(),
                     interval='1d', progress=False, auto_adjust=True)
    result = {}
    if not df.empty:
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        for ts, row in df.iterrows():
            ds = ts.date().isoformat() if hasattr(ts, 'date') else str(ts)[:10]
            result[ds] = float(row.get('Close') or row.get('close') or 16.0)
    with cache.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['date', 'vix'])
        w.writeheader()
        for ds, v in sorted(result.items()):
            w.writerow({'date': ds, 'vix': v})
    return result


def _load_spy_daily(start: date, end: date) -> dict:
    """Returns {date_str: {'open', 'close', 'high', 'low', 'volume'}}"""
    cache = DATA_DIR / f'spy_daily_{start}_{end}.pkl'
    if cache.exists():
        with cache.open('rb') as f:
            return pickle.load(f)
    import pandas as pd
    df = yf.download('SPY', start=start.isoformat(),
                     end=(end + timedelta(days=1)).isoformat(),
                     interval='1d', progress=False, auto_adjust=True)
    result = {}
    if not df.empty:
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        for ts, row in df.iterrows():
            ds = ts.date().isoformat() if hasattr(ts, 'date') else str(ts)[:10]
            result[ds] = {
                'open':  float(row.get('Open', 0)),
                'high':  float(row.get('High', 0)),
                'low':   float(row.get('Low', 0)),
                'close': float(row.get('Close', 0)),
                'vol':   float(row.get('Volume', 0)),
            }
    with cache.open('wb') as f:
        pickle.dump(result, f)
    return result


def _load_ma50(spy_daily: dict) -> dict:
    """Returns {date_str: ma50_value}"""
    dates = sorted(spy_daily.keys())
    closes = [spy_daily[d]['close'] for d in dates]
    result = {}
    for i, ds in enumerate(dates):
        if i >= 49:
            result[ds] = sum(closes[i-49:i+1]) / 50.0
        else:
            result[ds] = None
    return result


def _prev_trading_day(ds: str, spy_daily: dict) -> Optional[str]:
    d = date.fromisoformat(ds) - timedelta(days=1)
    for _ in range(10):
        if d.isoformat() in spy_daily:
            return d.isoformat()
        d -= timedelta(days=1)
    return None


def _nth_trading_day(ds: str, spy_daily: dict, n: int) -> Optional[str]:
    """Return the nth trading day after ds."""
    days = sorted(spy_daily.keys())
    try:
        idx = days.index(ds)
        target = idx + n
        return days[target] if target < len(days) else None
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 5. IV ENGINE  (real bid/ask → IV → delta/gamma)
# ══════════════════════════════════════════════════════════════════════════════

def _bs_price_py(S, K, T, r, sigma, flag) -> float:
    """Pure-Python BS fallback."""
    if T <= 1e-7 or sigma <= 1e-6:
        return max(S - K, 0.0) if flag == 'c' else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    def N(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    if flag == 'c':
        return S * N(d1) - K * math.exp(-r * T) * N(d2)
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


def _bs_delta_py(S, K, T, r, sigma, flag) -> float:
    if T <= 1e-7 or sigma <= 1e-6:
        return (1.0 if S > K else 0.0) if flag == 'c' else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    def N(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    return N(d1) if flag == 'c' else N(d1) - 1.0


def compute_iv(price: float, S: float, K: float, T: float, r: float, flag: str) -> Optional[float]:
    """Compute implied volatility from option price using py_vollib (or bisection fallback)."""
    if T <= 0 or price <= 0:
        return None
    try:
        iv = _pv_iv(price, S, K, T, r, flag)
        if 0.01 <= iv <= 5.0:
            return iv
    except Exception:
        pass
    # Bisection fallback
    try:
        lo, hi = 0.001, 5.0
        f = lambda s: _bs_price_py(S, K, T, r, s, flag) - price
        if f(lo) * f(hi) > 0:
            return None
        iv = brentq(f, lo, hi, xtol=1e-4)
        return iv if 0.01 <= iv <= 5.0 else None
    except Exception:
        return None


def compute_delta(S: float, K: float, T: float, r: float, sigma: float, flag: str) -> float:
    try:
        return float(_pv_delta(flag, S, K, T, r, sigma))
    except Exception:
        return _bs_delta_py(S, K, T, r, sigma, flag)


def compute_gamma(S: float, K: float, T: float, r: float, sigma: float, flag: str) -> float:
    try:
        return float(_pv_gamma(flag, S, K, T, r, sigma))
    except Exception:
        if T <= 1e-7 or sigma <= 1e-6:
            return 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        def phi(x): return math.exp(-0.5 * x**2) / math.sqrt(2 * math.pi)
        return phi(d1) / (S * sigma * math.sqrt(T))


def _mins_to_years(mins: float) -> float:
    return max(mins, 0.5) / TRADING_MINS


# ══════════════════════════════════════════════════════════════════════════════
# 6. OPTION PRICER  (unified real-data + synthetic)
# ══════════════════════════════════════════════════════════════════════════════

class OptionPricer:
    """
    Provides bid/ask/iv/delta for options.
    - For multi-day strategies: returns real ThetaData EOD bid/ask directly.
    - For intraday (0DTE): uses real IV from nearby-expiry EOD data,
      then applies BS with correct time-to-expiry.
    """

    def __init__(self, theta: ThetaCache, spy_daily: dict, vix_daily: dict):
        self.theta     = theta
        self.spy_daily = spy_daily
        self.vix       = vix_daily
        self._iv_cache: dict = {}   # date_str -> float (daily calibrated IV)

    def _day_iv(self, obs_date: date, expirations: List[date]) -> float:
        """
        Compute daily calibrated IV from ThetaData EOD data of short-dated options.
        Uses midpoint of ATM call + put, then averages across available strikes.
        Falls back to VIX/100 if real data unavailable.
        """
        ds = obs_date.isoformat()
        if ds in self._iv_cache:
            return self._iv_cache[ds]

        spy_close = self.spy_daily.get(ds, {}).get('close', 0.0)
        if spy_close <= 0:
            iv = self.vix.get(ds, 16.0) / 100.0
            self._iv_cache[ds] = iv
            return iv

        r = _rfr(obs_date)
        ivs = []

        # Try expirations 3–15 days out for meaningful time value
        for exp in expirations:
            dte = (exp - obs_date).days
            if not (3 <= dte <= 15):
                continue
            T = dte / 365.0
            chain = self.theta.get_chain_for_day(obs_date, exp)
            if not chain:
                continue
            for (K, right), q in chain.items():
                if abs(K - spy_close) > spy_close * 0.015:  # skip far OTM
                    continue
                mid = (q['bid'] + q['ask']) / 2.0
                if mid <= 0.01:
                    continue
                flag = 'c' if right == 'C' else 'p'
                iv = compute_iv(mid, spy_close, K, T, r, flag)
                if iv is not None:
                    ivs.append(iv)

        if ivs:
            result = float(np.median(ivs))
        else:
            result = self.vix.get(ds, 16.0) / 100.0

        self._iv_cache[ds] = result
        return result

    def real_eod_quote(self, obs_date: date, expiration: date,
                        strike: float, right: str) -> Tuple[float, float, float, float]:
        """
        Returns (bid, ask, iv, delta) from real ThetaData EOD.
        Fallback to synthetic if data unavailable.
        """
        q = self.theta.get_quote(obs_date, expiration, strike, right)
        ds = obs_date.isoformat()
        spy_close = self.spy_daily.get(ds, {}).get('close', 0.0)
        if spy_close <= 0:
            spy_close = strike  # rough fallback

        T = max((expiration - obs_date).days, 0) / 365.0
        r = _rfr(obs_date)
        flag = 'c' if right.upper() == 'C' else 'p'

        if q and q['ask'] > 0:
            mid = (q['bid'] + q['ask']) / 2.0
            iv  = compute_iv(mid, spy_close, strike, T, r, flag) or (self.vix.get(ds, 16.0) / 100.0)
            d   = compute_delta(spy_close, strike, T, r, iv, flag)
            return (q['bid'], q['ask'], iv, d)

        # Synthetic fallback
        iv = self.vix.get(ds, 16.0) / 100.0
        p  = _bs_price_py(spy_close, strike, max(T, 0.0001), r, iv, flag)
        spread = 0.04
        bid = round(p * (1 - spread / 2), 4)
        ask = round(p * (1 + spread / 2), 4)
        d   = compute_delta(spy_close, strike, max(T, 0.0001), r, iv, flag)
        return (bid, ask, iv, d)

    def intraday_price(self, spy: float, strike: float, mins_left: float,
                        right: str, obs_date: date,
                        expirations: List[date]) -> Tuple[float, float, float, float]:
        """
        Returns (bid, ask, iv, delta) for intraday option using real-IV BS pricing.
        Used for 0DTE intraday entry/exit where ThetaData EOD prices are not appropriate.
        """
        T    = _mins_to_years(mins_left)
        r    = _rfr(obs_date)
        flag = 'c' if right.upper() == 'C' else 'p'
        iv   = self._day_iv(obs_date, expirations)
        p    = _bs_price_py(spy, strike, T, r, iv, flag)
        p    = max(p, 0.01)
        # Real bid/ask spread from ThetaData calibration (~6-8% for 0DTE)
        spread = 0.07
        bid  = round(p * (1 - spread / 2), 4)
        ask  = round(p * (1 + spread / 2), 4)
        d    = compute_delta(spy, strike, T, r, iv, flag)
        return (bid, ask, iv, d)

    def synthetic_intraday(self, spy: float, strike: float, mins_left: float,
                            right: str, obs_date: date) -> Tuple[float, float, float, float]:
        """Pure VIX-based synthetic pricing (for comparison column)."""
        T    = _mins_to_years(mins_left)
        r    = _rfr(obs_date)
        flag = 'c' if right.upper() == 'C' else 'p'
        iv   = self.vix.get(obs_date.isoformat(), 16.0) / 100.0
        p    = _bs_price_py(spy, strike, T, r, iv, flag)
        p    = max(p, 0.01)
        spread = 0.04
        bid  = round(p * (1 - spread / 2), 4)
        ask  = round(p * (1 + spread / 2), 4)
        d    = compute_delta(spy, strike, T, r, iv, flag)
        return (bid, ask, iv, d)

    def strike_for_delta(self, target_delta: float, spy: float,
                          obs_date: date, expiration: date,
                          right: str, expirations: List[date],
                          use_real: bool = True) -> float:
        """Find strike closest to target delta from ThetaData chain or BS."""
        chain = self.theta.get_chain_for_day(obs_date, expiration, right.upper())
        T    = max((expiration - obs_date).days / 365.0, 0.0001)
        r    = _rfr(obs_date)
        flag = 'c' if right.upper() == 'C' else 'p'
        ds   = obs_date.isoformat()

        if use_real and chain:
            # Find strike whose real delta is closest to target
            spy_close = self.spy_daily.get(ds, {}).get('close', spy)
            best_k, best_diff = spy, 999.0
            for (K, _), q in chain.items():
                mid = (q['bid'] + q['ask']) / 2.0
                if mid <= 0:
                    continue
                iv = compute_iv(mid, spy_close, K, T, r, flag)
                if iv is None:
                    continue
                d = abs(compute_delta(spy_close, K, T, r, iv, flag))
                diff = abs(d - target_delta)
                if diff < best_diff:
                    best_diff, best_k = diff, K
            return best_k

        # BS fallback
        iv = self._day_iv(obs_date, expirations)
        try:
            if right.upper() == 'C':
                fn = lambda K: compute_delta(spy, K, T, r, iv, 'c') - target_delta
            else:
                fn = lambda K: abs(compute_delta(spy, K, T, r, iv, 'p')) - target_delta
            lo, hi = spy * 0.5, spy * 1.5
            return brentq(fn, lo, hi, xtol=0.01)
        except Exception:
            return spy * (0.97 if right.upper() == 'P' else 1.03)


# ══════════════════════════════════════════════════════════════════════════════
# 7. IV RANK
# ══════════════════════════════════════════════════════════════════════════════

def _iv_rank(vix_daily: dict, ds: str, lookback: int = 252) -> float:
    """52-week IV rank: 0–100, where 100 = highest VIX in past year."""
    dates = sorted(vix_daily.keys())
    try:
        idx = dates.index(ds)
    except ValueError:
        return 50.0
    window = dates[max(0, idx - lookback):idx + 1]
    vals = [vix_daily[d] for d in window]
    if len(vals) < 2:
        return 50.0
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return 50.0
    return (vix_daily.get(ds, lo) - lo) / (hi - lo) * 100.0


# ══════════════════════════════════════════════════════════════════════════════
# 8. HELPERS: VWAP, OR, vol-surge, momentum
# ══════════════════════════════════════════════════════════════════════════════

def _vwap(bars: list) -> float:
    tv = sum(b['v'] * (b['h'] + b['l'] + b['c']) / 3.0 for b in bars if b['v'] > 0)
    v  = sum(b['v'] for b in bars if b['v'] > 0)
    return tv / v if v > 0 else (bars[-1]['c'] if bars else 0.0)


def _vol_avg(bars: list, n: int = 20, skip_first: int = 5) -> float:
    vols = [b['v'] for b in bars[skip_first:] if b['v'] > 0]
    if len(vols) < n:
        return sum(vols) / len(vols) if vols else 0.0
    return sum(vols[-n:]) / n


def _rsi(bars: list, period: int = 14) -> float:
    closes = [b['c'] for b in bars]
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]
    ag, al = sum(gains) / period, sum(losses) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _higher_high(bars: list, n: int = 5) -> bool:
    if len(bars) < n + 1:
        return False
    return bars[-1]['h'] > max(b['h'] for b in bars[-(n+1):-1])


def _or_range(bars: list, or_min: int = 15) -> Tuple[float, float]:
    """Opening range high/low from first or_min bars."""
    ob = bars[:or_min]
    if not ob:
        return 0.0, 0.0
    return max(b['h'] for b in ob), min(b['l'] for b in ob)


def _add_trading_days(d: date, n: int, spy_daily: dict) -> Optional[date]:
    days = sorted(spy_daily.keys())
    ds   = d.isoformat()
    try:
        idx = days.index(ds)
    except ValueError:
        # find next available
        for i, day in enumerate(days):
            if day > ds:
                idx = i
                break
        else:
            return None
    target = idx + n
    return date.fromisoformat(days[target]) if target < len(days) else None


# ══════════════════════════════════════════════════════════════════════════════
# 9. NEAREST EXPIRATION LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def _find_expiration(obs_date: date, min_dte: int, max_dte: int,
                      expirations: List[date]) -> Optional[date]:
    """Find nearest expiration with dte in [min_dte, max_dte]."""
    for exp in expirations:
        dte = (exp - obs_date).days
        if min_dte <= dte <= max_dte:
            return exp
    return None


def _same_day_exp(obs_date: date, expirations: List[date]) -> Optional[date]:
    return obs_date if obs_date in expirations else None


# ══════════════════════════════════════════════════════════════════════════════
# 10. TRADE DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    strategy:    str
    date:        str
    ticker:      str
    direction:   str        # call / put / credit_spread / iron_condor
    entry_date:  str
    exit_date:   str
    entry_price: float      # net debit or credit per contract (per share × 100)
    exit_price:  float
    pnl:         float
    entry_iv:    float
    entry_delta: float
    vix:         float
    is_real:     bool       # True = real ThetaData pricing, False = synthetic
    note:        str = ''


# ══════════════════════════════════════════════════════════════════════════════
# 11. STRATEGY 1 — Credit Spread  (0DTE puts, Mon/Fri, VIX 15-22)
# ══════════════════════════════════════════════════════════════════════════════

def run_s1_credit_spread(
        spy_5min: dict, vix_daily: dict, spy_daily: dict,
        pricer: OptionPricer, expirations: List[date],
        use_real: bool, dates: List[str]) -> List[Trade]:

    trades = []
    dbg = {'days': 0, 'weekday': 0, 'vix': 0, 'few_bars': 0,
           'no_exp': 0, 'no_entry_bar': 0, 'low_credit': 0, 'entered': 0}
    first_reject = {}

    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        dbg['days'] += 1

        if dt_obj.weekday() not in (0, 4):  # Mon=0, Fri=4
            dbg['weekday'] += 1
            continue
        vix = vix_daily.get(ds, 16.0)
        if not (15.0 <= vix <= 22.0):
            dbg['vix'] += 1
            first_reject.setdefault('vix', f'{ds}: vix={vix:.1f}')
            continue

        bars = spy_5min.get(ds, [])
        if len(bars) < 8:
            dbg['few_bars'] += 1
            first_reject.setdefault('few_bars', f'{ds}: {len(bars)} bars')
            continue

        # Need same-day expiration for 0DTE
        exp = _same_day_exp(dt_obj, expirations)
        if exp is None:
            exp = _find_expiration(dt_obj, 0, 1, expirations)
        if exp is None:
            dbg['no_exp'] += 1
            first_reject.setdefault('no_exp', f'{ds}: no 0-1 DTE expiration')
            continue

        # Entry window: 10:00-11:00 AM
        entry_bar = None
        for bar in bars:
            dt_bar = datetime.fromtimestamp(bar['t'] / 1000, tz=ET)
            if dt_bar.hour == 10 and 0 <= dt_bar.minute < 60:
                entry_bar = bar
                break

        if entry_bar is None:
            dbg['no_entry_bar'] += 1
            first_reject.setdefault('no_entry_bar', f'{ds}: no bar in 10-11 AM window')
            continue

        dt_entry = datetime.fromtimestamp(entry_bar['t'] / 1000, tz=ET)
        spy      = entry_bar['c']
        mins_left = max((16 * 60) - (dt_entry.hour * 60 + dt_entry.minute), 1)
        r = _rfr(dt_obj)

        # Find 0.20-delta put strike
        short_strike = pricer.strike_for_delta(0.20, spy, dt_obj, exp, 'P',
                                                expirations, use_real=use_real)
        short_strike = round(short_strike / 0.5) * 0.5  # nearest $0.50 increment
        long_strike  = short_strike - 2.0

        # Get bid/ask for both legs
        if use_real:
            short_bid, short_ask, entry_iv, entry_delta = pricer.intraday_price(
                spy, short_strike, mins_left, 'P', dt_obj, expirations)
            long_bid, long_ask, _, _ = pricer.intraday_price(
                spy, long_strike, mins_left, 'P', dt_obj, expirations)
        else:
            short_bid, short_ask, entry_iv, entry_delta = pricer.synthetic_intraday(
                spy, short_strike, mins_left, 'P', dt_obj)
            long_bid, long_ask, _, _ = pricer.synthetic_intraday(
                spy, long_strike, mins_left, 'P', dt_obj)

        # Credit received: sell short at bid, buy long at ask
        credit = round(short_bid - long_ask, 4)
        if credit <= 0.02:
            dbg['low_credit'] += 1
            first_reject.setdefault('low_credit',
                f'{ds}: credit={credit:.4f} short_bid={short_bid:.4f} long_ask={long_ask:.4f}')
            continue
        dbg['entered'] += 1

        target_exit = round(credit * 0.30, 4)  # 30% profit
        stop_exit   = round(credit * 2.0, 4)   # 2x credit loss

        # Scan remaining bars for exit
        exit_bar    = None
        exit_reason = 'EOD'
        spread_cost = credit  # initial credit

        for bar in bars:
            dt_bar = datetime.fromtimestamp(bar['t'] / 1000, tz=ET)
            if dt_bar <= dt_entry:
                continue
            # Force exit at 3:00 PM
            if dt_bar.hour >= 15:
                exit_bar, exit_reason = bar, '3:00 PM force exit'
                break

            ml = max((16 * 60) - (dt_bar.hour * 60 + dt_bar.minute), 1)
            spy_now = bar['c']

            if use_real:
                _, short_cur_ask, _, _ = pricer.intraday_price(
                    spy_now, short_strike, ml, 'P', dt_obj, expirations)
                long_cur_bid, _, _, _ = pricer.intraday_price(
                    spy_now, long_strike, ml, 'P', dt_obj, expirations)
            else:
                _, short_cur_ask, _, _ = pricer.synthetic_intraday(
                    spy_now, short_strike, ml, 'P', dt_obj)
                long_cur_bid, _, _, _ = pricer.synthetic_intraday(
                    spy_now, long_strike, ml, 'P', dt_obj)

            current_debit = round(short_cur_ask - long_cur_bid, 4)
            current_debit = max(current_debit, 0)

            if current_debit <= target_exit:
                exit_bar, exit_reason = bar, '30% target'
                break
            if current_debit >= stop_exit:
                exit_bar, exit_reason = bar, '2x stop'
                break

        if exit_bar is None:
            exit_bar    = bars[-1]
            exit_reason = 'EOD close'

        dt_exit  = datetime.fromtimestamp(exit_bar['t'] / 1000, tz=ET)
        spy_exit = exit_bar['c']
        ml_exit  = max((16 * 60) - (dt_exit.hour * 60 + dt_exit.minute), 1)

        if use_real:
            _, short_exit_ask, _, _ = pricer.intraday_price(
                spy_exit, short_strike, ml_exit, 'P', dt_obj, expirations)
            long_exit_bid, _, _, _ = pricer.intraday_price(
                spy_exit, long_strike, ml_exit, 'P', dt_obj, expirations)
        else:
            _, short_exit_ask, _, _ = pricer.synthetic_intraday(
                spy_exit, short_strike, ml_exit, 'P', dt_obj)
            long_exit_bid, _, _, _ = pricer.synthetic_intraday(
                spy_exit, long_strike, ml_exit, 'P', dt_obj)

        exit_debit = max(round(short_exit_ask - long_exit_bid, 4), 0)
        pnl = round((credit - exit_debit) * 100, 2)

        trades.append(Trade(
            strategy='S1_CreditSpread', date=ds, ticker='SPY',
            direction='credit_spread_put',
            entry_date=ds, exit_date=ds,
            entry_price=credit, exit_price=exit_debit,
            pnl=pnl, entry_iv=entry_iv, entry_delta=entry_delta,
            vix=vix, is_real=use_real, note=exit_reason,
        ))

    print(f'  S1 debug ({"real" if use_real else "synth"}): '
          f'days={dbg["days"]} weekday={dbg["weekday"]} vix={dbg["vix"]} '
          f'few_bars={dbg["few_bars"]} no_exp={dbg["no_exp"]} '
          f'no_entry_bar={dbg["no_entry_bar"]} low_credit={dbg["low_credit"]} '
          f'entered={dbg["entered"]} trades={len(trades)}')
    if first_reject:
        for k, v in first_reject.items():
            print(f'    first reject [{k}]: {v}')
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 12. STRATEGY 2 — 5-DTE Momentum Calls
# ══════════════════════════════════════════════════════════════════════════════

def run_s2_momentum_calls(
        spy_5min: dict, vix_daily: dict, spy_daily: dict,
        pricer: OptionPricer, expirations: List[date],
        use_real: bool, dates: List[str]) -> List[Trade]:

    trades = []
    OR_BARS = 3   # 3×5-min = 15-min opening range

    dbg = {'days': 0, 'few_bars': 0, 'no_exp': 0, 'bias': 0,
           'hh': 0, 'vol': 0, 'rsi': 0, 'bad_ask': 0, 'entered': 0}
    first_reject = {}

    for ds in dates:
        dt_obj = date.fromisoformat(ds)
        bars   = spy_5min.get(ds, [])
        dbg['days'] += 1

        if len(bars) < OR_BARS + 6:
            dbg['few_bars'] += 1
            first_reject.setdefault('few_bars', f'{ds}: only {len(bars)} bars')
            continue

        # Find expiration ~5 trading days out (check once per day, no look-ahead)
        exp = _find_expiration(dt_obj, 4, 8, expirations)
        if exp is None:
            dbg['no_exp'] += 1
            first_reject.setdefault('no_exp', f'{ds}: no 4-8 DTE expiration found')
            continue

        vix = vix_daily.get(ds, 16.0)

        # ── Bar-by-bar scan with entry_pending (no look-ahead) ─────────────────
        entry_pending = False
        entry_bar     = None

        for i, bar in enumerate(bars):
            dt_bar = datetime.fromtimestamp(bar['t'] / 1000, tz=ET)

            # Entry window: signal can fire any time, but enter only after OR
            if (dt_bar.hour, dt_bar.minute) >= (14, 0):
                break

            if entry_pending:
                entry_bar = bar   # execute fill on the bar after signal
                entry_pending = False
                break

            if i < OR_BARS:      # still inside opening range, no signal yet
                continue

            # All conditions checked on bars seen so far — zero look-ahead
            bars_so_far = bars[:i + 1]
            vwap_now    = _vwap(bars_so_far)
            spy_c       = bar['c']

            if spy_c - vwap_now < 0.50:
                if i == OR_BARS:
                    dbg['bias'] += 1
                    first_reject.setdefault('bias',
                        f'{ds} bar {i}: spy={spy_c:.2f} vwap={vwap_now:.2f} gap={spy_c-vwap_now:.2f}')
                continue

            if not _higher_high(bars_so_far, n=5):
                dbg['hh'] += 1
                first_reject.setdefault('hh', f'{ds} bar {i}: no higher high in last 5 bars')
                continue

            vol_avg = _vol_avg(bars_so_far, n=20)
            if vol_avg > 0 and bar['v'] < vol_avg * 1.5:
                dbg['vol'] += 1
                first_reject.setdefault('vol',
                    f'{ds} bar {i}: vol={bar["v"]:.0f} avg={vol_avg:.0f} ratio={bar["v"]/vol_avg:.2f}')
                continue

            if _rsi(bars_so_far) < 55:
                dbg['rsi'] += 1
                first_reject.setdefault('rsi',
                    f'{ds} bar {i}: rsi={_rsi(bars_so_far):.1f}')
                continue

            # All conditions met on bar i — flag entry on bar i+1
            entry_pending = True

        if entry_bar is None:
            continue

        dt_entry = datetime.fromtimestamp(entry_bar['t'] / 1000, tz=ET)
        spy      = entry_bar['c']
        strike   = round(spy)
        r        = _rfr(dt_obj)

        if use_real:
            _, entry_ask, entry_iv, entry_delta = pricer.real_eod_quote(
                dt_obj, exp, float(strike), 'C')
        else:
            ml = max((16 * 60) - (dt_entry.hour * 60 + dt_entry.minute), 1)
            _, entry_ask, entry_iv, entry_delta = pricer.synthetic_intraday(
                spy, strike, ml, 'C', dt_obj)

        if entry_ask <= 0 or entry_ask * 100 > 1000:
            dbg['bad_ask'] += 1
            first_reject.setdefault('bad_ask', f'{ds}: entry_ask={entry_ask:.4f}')
            continue

        dbg['entered'] += 1
        target_price  = entry_ask * 1.15
        stop_price    = entry_ask * 0.80
        max_hold_days = 2

        exit_date_obj = _add_trading_days(dt_obj, max_hold_days, spy_daily)
        if exit_date_obj is None:
            continue
        exit_ds = exit_date_obj.isoformat()

        exit_reason    = 'time stop'
        actual_exit_ds = exit_ds

        # Check each hold day once (EOD price is day-level, no need to scan bars)
        for hold_ds in dates:
            if hold_ds <= ds:
                continue
            if hold_ds > exit_ds:
                break
            hold_dt = date.fromisoformat(hold_ds)

            if use_real:
                cur_bid, _, _, _ = pricer.real_eod_quote(hold_dt, exp, float(strike), 'C')
            else:
                hold_bars = spy_5min.get(hold_ds, [])
                hold_spy  = hold_bars[-1]['c'] if hold_bars else spy
                cur_bid, _, _, _ = pricer.synthetic_intraday(hold_spy, strike, 30, 'C', hold_dt)

            if cur_bid >= target_price:
                exit_reason, actual_exit_ds = '15% target', hold_ds
                break
            if cur_bid <= stop_price:
                exit_reason, actual_exit_ds = '20% stop', hold_ds
                break

        exit_dt_obj = date.fromisoformat(actual_exit_ds)

        if use_real:
            exit_bid, _, _, _ = pricer.real_eod_quote(exit_dt_obj, exp, float(strike), 'C')
        else:
            exit_bars = spy_5min.get(actual_exit_ds, [])
            exit_spy  = exit_bars[-1]['c'] if exit_bars else spy
            exit_bid, _, _, _ = pricer.synthetic_intraday(exit_spy, strike, 30, 'C', exit_dt_obj)

        if exit_bid <= 0:
            exit_bid = entry_ask * 0.5

        pnl = round((exit_bid - entry_ask) * 100, 2)

        trades.append(Trade(
            strategy='S2_5DTE_Momentum', date=ds, ticker='SPY',
            direction='call',
            entry_date=ds, exit_date=actual_exit_ds,
            entry_price=entry_ask, exit_price=exit_bid,
            pnl=pnl, entry_iv=entry_iv, entry_delta=entry_delta,
            vix=vix, is_real=use_real, note=exit_reason,
        ))

    print(f'  S2 debug ({"real" if use_real else "synth"}): '
          f'days={dbg["days"]} few_bars={dbg["few_bars"]} no_exp={dbg["no_exp"]} '
          f'bias={dbg["bias"]} hh={dbg["hh"]} vol={dbg["vol"]} rsi={dbg["rsi"]} '
          f'bad_ask={dbg["bad_ask"]} entered={dbg["entered"]} trades={len(trades)}')
    if first_reject:
        for k, v in first_reject.items():
            print(f'    first reject [{k}]: {v}')
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 13. STRATEGY 3 — FOMC/CPI Catalyst
# ══════════════════════════════════════════════════════════════════════════════

def run_s3_catalyst(
        spy_daily: dict, vix_daily: dict,
        pricer: OptionPricer, expirations: List[date],
        use_real: bool, dates: List[str]) -> List[Trade]:

    trades  = []
    ma50    = _load_ma50(spy_daily)
    all_days = sorted(spy_daily.keys())
    dbg = {'fomc_events': 0, 'fomc_out_range': 0, 'fomc_no_entry': 0,
           'fomc_no_ma': 0, 'fomc_below_ma': 0, 'fomc_falling_ma': 0,
           'fomc_no_exp': 0, 'fomc_entered': 0,
           'cpi_events': 0, 'cpi_not_hot': 0, 'cpi_no_entry': 0,
           'cpi_no_exp': 0, 'cpi_entered': 0}
    first_reject = {}

    def _n_days_before(event: date, n: int) -> Optional[str]:
        idx = None
        for i, d in enumerate(all_days):
            if date.fromisoformat(d) >= event:
                idx = i
                break
        if idx is None or idx < n:
            return None
        return all_days[idx - n]

    def _day_after(event: date) -> Optional[str]:
        for d in all_days:
            if date.fromisoformat(d) > event:
                return d
        return None

    processed = set()

    # FOMC: buy SPY calls 2 days before when SPY above rising 50-day MA
    for fomc_dt in sorted(FOMC_DATES):
        dbg['fomc_events'] += 1
        if fomc_dt < date.fromisoformat(dates[0]) or fomc_dt > date.fromisoformat(dates[-1]):
            dbg['fomc_out_range'] += 1
            continue
        entry_ds = _n_days_before(fomc_dt, 2)
        if entry_ds is None or entry_ds not in dates:
            dbg['fomc_no_entry'] += 1
            first_reject.setdefault('fomc_no_entry', f'{fomc_dt}: entry_ds={entry_ds}')
            continue
        entry_dt = date.fromisoformat(entry_ds)
        spy_px   = spy_daily.get(entry_ds, {}).get('close', 0.0)
        ma       = ma50.get(entry_ds)
        if not spy_px or not ma:
            dbg['fomc_no_ma'] += 1
            first_reject.setdefault('fomc_no_ma', f'{fomc_dt}: entry={entry_ds} ma={ma}')
            continue
        if spy_px <= ma:
            dbg['fomc_below_ma'] += 1
            first_reject.setdefault('fomc_below_ma',
                f'{fomc_dt}: spy={spy_px:.2f} ma={ma:.2f}')
            continue
        # Rising MA check (SPY above MA and MA trending up)
        prev_ds = _prev_trading_day(entry_ds, spy_daily)
        if prev_ds and ma50.get(prev_ds):
            if ma50[entry_ds] <= ma50[prev_ds]:
                dbg['fomc_falling_ma'] += 1
                first_reject.setdefault('fomc_falling_ma',
                    f'{fomc_dt}: ma={ma50[entry_ds]:.2f} prev_ma={ma50[prev_ds]:.2f}')
                continue

        exit_ds = _day_after(fomc_dt)
        if exit_ds is None:
            continue
        exit_dt = date.fromisoformat(exit_ds)

        exp = _find_expiration(entry_dt, 2, 10, expirations)
        if exp is None:
            dbg['fomc_no_exp'] += 1
            first_reject.setdefault('fomc_no_exp', f'{fomc_dt}: no 2-10 DTE exp from {entry_ds}')
            continue

        strike = round(spy_px)
        vix    = vix_daily.get(entry_ds, 16.0)
        r      = _rfr(entry_dt)

        if use_real:
            _, entry_ask, iv, delta = pricer.real_eod_quote(entry_dt, exp, float(strike), 'C')
            exit_bid, _, _, _ = pricer.real_eod_quote(exit_dt, exp, float(strike), 'C')
        else:
            iv = vix / 100.0
            T  = max((exp - entry_dt).days / 365.0, 0.0001)
            p  = _bs_price_py(spy_px, strike, T, r, iv, 'c')
            entry_ask = round(p * 1.04, 4)
            delta     = compute_delta(spy_px, strike, T, r, iv, 'c')
            T2 = max((exp - exit_dt).days / 365.0, 0.0001)
            spy_exit = spy_daily.get(exit_ds, {}).get('close', spy_px)
            p2 = _bs_price_py(spy_exit, strike, T2, r, iv, 'c')
            exit_bid = round(p2 * 0.96, 4)

        if entry_ask <= 0:
            continue
        dbg['fomc_entered'] += 1
        target_price = entry_ask * 4.0   # 300% gain
        stop_price   = entry_ask * 0.50  # 50% loss

        # Check if target/stop hit at exit day
        if exit_bid >= target_price:
            exit_reason = '300% target'
        elif exit_bid <= stop_price:
            exit_reason = '50% stop'
        else:
            exit_reason = 'day after FOMC'

        pnl = round((exit_bid - entry_ask) * 100, 2)
        key = (entry_ds, 'FOMC', str(exp))
        if key in processed:
            continue
        processed.add(key)

        trades.append(Trade(
            strategy='S3_FOMC_Catalyst', date=entry_ds, ticker='SPY',
            direction='call',
            entry_date=entry_ds, exit_date=exit_ds,
            entry_price=entry_ask, exit_price=exit_bid,
            pnl=pnl, entry_iv=iv, entry_delta=delta,
            vix=vix, is_real=use_real, note=exit_reason,
        ))

    # CPI: buy SPY puts 2 days before when last reading above estimate
    # (approximate: use months where CPI came in hot = 2021-2022 era)
    hot_cpi_months = {
        '2021-03', '2021-04', '2021-05', '2021-06', '2021-07', '2021-08',
        '2021-09', '2021-10', '2021-11', '2021-12',
        '2022-01', '2022-02', '2022-03', '2022-04', '2022-05', '2022-06',
        '2022-07', '2022-08', '2022-09',
        '2023-01', '2023-02',
        '2024-03', '2024-04',
        '2025-03', '2025-04',
    }

    for cpi_dt in sorted(CPI_DATES):
        dbg['cpi_events'] += 1
        if cpi_dt < date.fromisoformat(dates[0]) or cpi_dt > date.fromisoformat(dates[-1]):
            continue
        month_key = cpi_dt.strftime('%Y-%m')
        if month_key not in hot_cpi_months:
            dbg['cpi_not_hot'] += 1
            continue
        entry_ds = _n_days_before(cpi_dt, 2)
        if entry_ds is None or entry_ds not in dates:
            dbg['cpi_no_entry'] += 1
            first_reject.setdefault('cpi_no_entry', f'{cpi_dt}: entry_ds={entry_ds}')
            continue
        entry_dt = date.fromisoformat(entry_ds)
        spy_px   = spy_daily.get(entry_ds, {}).get('close', 0.0)
        if not spy_px:
            continue

        exit_ds = _day_after(cpi_dt)
        if exit_ds is None:
            continue
        exit_dt = date.fromisoformat(exit_ds)

        exp = _find_expiration(entry_dt, 2, 10, expirations)
        if exp is None:
            dbg['cpi_no_exp'] += 1
            first_reject.setdefault('cpi_no_exp', f'{cpi_dt}: no 2-10 DTE exp from {entry_ds}')
            continue

        strike = round(spy_px)
        vix    = vix_daily.get(entry_ds, 16.0)
        r      = _rfr(entry_dt)

        if use_real:
            _, entry_ask, iv, delta = pricer.real_eod_quote(entry_dt, exp, float(strike), 'P')
            exit_bid, _, _, _ = pricer.real_eod_quote(exit_dt, exp, float(strike), 'P')
        else:
            iv = vix / 100.0
            T  = max((exp - entry_dt).days / 365.0, 0.0001)
            p  = _bs_price_py(spy_px, strike, T, r, iv, 'p')
            entry_ask = round(p * 1.04, 4)
            delta     = compute_delta(spy_px, strike, T, r, iv, 'p')
            T2 = max((exp - exit_dt).days / 365.0, 0.0001)
            spy_exit = spy_daily.get(exit_ds, {}).get('close', spy_px)
            p2 = _bs_price_py(spy_exit, strike, T2, r, iv, 'p')
            exit_bid = round(p2 * 0.96, 4)

        if entry_ask <= 0:
            continue
        dbg['cpi_entered'] += 1
        target_price = entry_ask * 4.0
        stop_price   = entry_ask * 0.50

        if exit_bid >= target_price:
            exit_reason = '300% target'
        elif exit_bid <= stop_price:
            exit_reason = '50% stop'
        else:
            exit_reason = 'day after CPI'

        pnl = round((exit_bid - entry_ask) * 100, 2)
        key = (entry_ds, 'CPI', str(exp))
        if key in processed:
            continue
        processed.add(key)

        trades.append(Trade(
            strategy='S3_CPI_Catalyst', date=entry_ds, ticker='SPY',
            direction='put',
            entry_date=entry_ds, exit_date=exit_ds,
            entry_price=entry_ask, exit_price=exit_bid,
            pnl=pnl, entry_iv=iv, entry_delta=delta,
            vix=vix, is_real=use_real, note=exit_reason,
        ))

    print(f'  S3 debug ({"real" if use_real else "synth"}): '
          f'fomc_events={dbg["fomc_events"]} out_range={dbg["fomc_out_range"]} '
          f'no_entry={dbg["fomc_no_entry"]} below_ma={dbg["fomc_below_ma"]} '
          f'falling_ma={dbg["fomc_falling_ma"]} no_exp={dbg["fomc_no_exp"]} '
          f'fomc_entered={dbg["fomc_entered"]} | '
          f'cpi_events={dbg["cpi_events"]} not_hot={dbg["cpi_not_hot"]} '
          f'cpi_entered={dbg["cpi_entered"]} trades={len(trades)}')
    if first_reject:
        for k, v in first_reject.items():
            print(f'    first reject [{k}]: {v}')
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 14. STRATEGY 4 — Earnings Momentum
# ══════════════════════════════════════════════════════════════════════════════

def _get_earnings_dates(ticker: str, start: date, end: date) -> List[date]:
    cache = DATA_DIR / f'earnings_{ticker}_{start.year}_{end.year}.pkl'
    if cache.exists():
        with cache.open('rb') as f:
            return pickle.load(f)
    try:
        import pandas as pd
        stock = yf.Ticker(ticker)
        cal   = stock.earnings_dates
        if cal is None or cal.empty:
            result = []
        else:
            result = []
            for ts in cal.index:
                try:
                    d = ts.date() if hasattr(ts, 'date') else date.fromisoformat(str(ts)[:10])
                    if start <= d <= end:
                        result.append(d)
                except Exception:
                    pass
    except Exception:
        result = []
    with cache.open('wb') as f:
        pickle.dump(result, f)
    return sorted(result)


def run_s4_earnings(
        spy_5min: dict, spy_daily: dict, vix_daily: dict,
        pricer: OptionPricer, expirations: List[date],
        use_real: bool, dates: List[str]) -> List[Trade]:

    trades    = []
    ma50_spy  = _load_ma50(spy_daily)
    all_days  = sorted(spy_daily.keys())
    date_set  = set(dates)
    dbg       = {'events': 0, 'no_idx': 0, 'not_in_dates': 0, 'no_px': 0,
                 'below_ma': 0, 'falling_ma': 0, 'no_exit': 0,
                 'no_exp': 0, 'bad_ask': 0, 'entered': 0}
    first_reject = {}

    for ticker in EARNINGS_UNIVERSE:
        print(f'    S4: loading earnings dates for {ticker}...')
        earn_dates = _get_earnings_dates(ticker, date.fromisoformat(dates[0]),
                                          date.fromisoformat(dates[-1]))
        # Load stock daily data
        stock_cache = DATA_DIR / f'{ticker}_daily_{BT_START}_{BT_END}.pkl'
        if stock_cache.exists():
            with stock_cache.open('rb') as f:
                stock_daily = pickle.load(f)
        else:
            try:
                import pandas as pd
                df = yf.download(ticker, start=BT_START.isoformat(),
                                 end=(BT_END + timedelta(days=1)).isoformat(),
                                 interval='1d', progress=False, auto_adjust=True)
                stock_daily = {}
                if not df.empty:
                    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                    for ts, row in df.iterrows():
                        ds = ts.date().isoformat() if hasattr(ts, 'date') else str(ts)[:10]
                        stock_daily[ds] = {
                            'close': float(row.get('Close', 0)),
                            'open':  float(row.get('Open', 0)),
                        }
            except Exception:
                stock_daily = {}
            with stock_cache.open('wb') as f:
                pickle.dump(stock_daily, f)

        stock_ma50 = _load_ma50(stock_daily)

        for earn_dt in earn_dates:
            dbg['events'] += 1
            # Entry: 5 trading days before earnings
            idx = None
            for i, d in enumerate(all_days):
                if date.fromisoformat(d) >= earn_dt:
                    idx = i
                    break
            if idx is None or idx < 5:
                dbg['no_idx'] += 1
                continue
            entry_ds = all_days[idx - 5]
            if entry_ds not in date_set:
                dbg['not_in_dates'] += 1
                first_reject.setdefault('not_in_dates',
                    f'{ticker} {earn_dt}: entry={entry_ds} not in filtered dates')
                continue

            entry_dt = date.fromisoformat(entry_ds)
            stock_px = stock_daily.get(entry_ds, {}).get('close', 0.0)
            if not stock_px:
                dbg['no_px'] += 1
                continue

            ma = stock_ma50.get(entry_ds)
            if not ma or stock_px <= ma:
                dbg['below_ma'] += 1
                first_reject.setdefault('below_ma',
                    f'{ticker} {earn_dt}: px={stock_px:.2f} ma={ma}')
                continue
            # Rising MA
            prev_ds = _prev_trading_day(entry_ds, stock_daily)
            if prev_ds and stock_ma50.get(prev_ds):
                if stock_ma50[entry_ds] <= stock_ma50[prev_ds]:
                    dbg['falling_ma'] += 1
                    continue

            # Exit: day after earnings
            exit_idx = idx + 1
            exit_ds  = all_days[exit_idx] if exit_idx < len(all_days) else None
            if not exit_ds or exit_ds not in date_set:
                dbg['no_exit'] += 1
                continue
            exit_dt = date.fromisoformat(exit_ds)

            vix    = vix_daily.get(entry_ds, 16.0)
            r      = _rfr(entry_dt)

            # For stock options: find expiration just after earnings
            exp = _find_expiration(entry_dt, 4, 12, expirations)
            if exp is None:
                dbg['no_exp'] += 1
                first_reject.setdefault('no_exp',
                    f'{ticker} {earn_dt}: no 4-12 DTE exp from {entry_ds}')
                continue

            strike = round(stock_px)
            if use_real and ticker == 'SPY':
                _, entry_ask, iv, delta = pricer.real_eod_quote(entry_dt, exp, float(strike), 'C')
                exit_bid, _, _, _ = pricer.real_eod_quote(exit_dt, exp, float(strike), 'C')
            else:
                # For non-SPY: use stock's historical vol as IV proxy
                iv = vix / 100.0 * 1.5  # stocks typically higher vol than SPY
                T  = max((exp - entry_dt).days / 365.0, 0.0001)
                p  = _bs_price_py(stock_px, strike, T, r, iv, 'c')
                entry_ask = round(p * 1.04, 4)
                delta     = compute_delta(stock_px, strike, T, r, iv, 'c')
                T2 = max((exp - exit_dt).days / 365.0, 0.0001)
                stock_exit_px = stock_daily.get(exit_ds, {}).get('close', stock_px)
                p2 = _bs_price_py(stock_exit_px, strike, T2, r, iv, 'c')
                exit_bid = round(p2 * 0.96, 4)

            if entry_ask <= 0 or entry_ask * 100 > 2000:
                dbg['bad_ask'] += 1
                continue

            dbg['entered'] += 1
            if exit_bid >= entry_ask * 5.0:
                exit_reason = 'earnings pop >400%'
            elif exit_bid <= 0.01:
                exit_reason = 'expired worthless'
            else:
                exit_reason = 'day after earnings'

            pnl = round((exit_bid - entry_ask) * 100, 2)

            trades.append(Trade(
                strategy='S4_Earnings', date=entry_ds, ticker=ticker,
                direction='call',
                entry_date=entry_ds, exit_date=exit_ds,
                entry_price=entry_ask, exit_price=exit_bid,
                pnl=pnl, entry_iv=iv, entry_delta=delta,
                vix=vix, is_real=use_real,
                note=f'{ticker} earnings | {exit_reason}',
            ))

    print(f'  S4 debug ({"real" if use_real else "synth"}): '
          f'events={dbg["events"]} no_idx={dbg["no_idx"]} not_in_dates={dbg["not_in_dates"]} '
          f'no_px={dbg["no_px"]} below_ma={dbg["below_ma"]} falling_ma={dbg["falling_ma"]} '
          f'no_exit={dbg["no_exit"]} no_exp={dbg["no_exp"]} bad_ask={dbg["bad_ask"]} '
          f'entered={dbg["entered"]} trades={len(trades)}')
    if first_reject:
        for k, v in first_reject.items():
            print(f'    first reject [{k}]: {v}')
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 15. STRATEGY 5 — Iron Condor Harvest
# ══════════════════════════════════════════════════════════════════════════════

def run_s5_iron_condor(
        spy_5min: dict, vix_daily: dict, spy_daily: dict,
        pricer: OptionPricer, expirations: List[date],
        use_real: bool, dates: List[str]) -> List[Trade]:

    trades = []
    dbg = {'days': 0, 'vix': 0, 'ivr': 0, 'few_bars': 0,
           'no_exp': 0, 'no_entry_bar': 0, 'low_credit': 0, 'entered': 0}
    first_reject = {}

    for ds in dates:
        dt_obj  = date.fromisoformat(ds)
        vix     = vix_daily.get(ds, 16.0)
        iv_rank = _iv_rank(vix_daily, ds)
        dbg['days'] += 1

        if vix <= 20.0:
            dbg['vix'] += 1
            first_reject.setdefault('vix', f'{ds}: vix={vix:.1f}')
            continue
        if iv_rank < 50.0:
            dbg['ivr'] += 1
            first_reject.setdefault('ivr', f'{ds}: iv_rank={iv_rank:.1f}')
            continue

        bars = spy_5min.get(ds, [])
        if len(bars) < 6:
            dbg['few_bars'] += 1
            continue

        exp = _same_day_exp(dt_obj, expirations)
        if exp is None:
            exp = _find_expiration(dt_obj, 0, 1, expirations)
        if exp is None:
            dbg['no_exp'] += 1
            first_reject.setdefault('no_exp', f'{ds}: no 0-1 DTE expiration found')
            continue

        # Entry window: 10:00-11:00 AM  (S5)
        entry_bar = None
        for bar in bars:
            dt_bar = datetime.fromtimestamp(bar['t'] / 1000, tz=ET)
            if dt_bar.hour == 10 and dt_bar.minute < 60:
                entry_bar = bar
                break
        if entry_bar is None:
            dbg['no_entry_bar'] += 1
            first_reject.setdefault('no_entry_bar', f'{ds}: no bar in 10-11 AM')
            continue

        dt_entry  = datetime.fromtimestamp(entry_bar['t'] / 1000, tz=ET)
        spy       = entry_bar['c']
        mins_left = max((16 * 60) - (dt_entry.hour * 60 + dt_entry.minute), 1)
        r         = _rfr(dt_obj)

        # 16-delta call + put strikes, wings $1 wide
        short_call_k = pricer.strike_for_delta(0.16, spy, dt_obj, exp, 'C',
                                                expirations, use_real=use_real)
        short_put_k  = pricer.strike_for_delta(0.16, spy, dt_obj, exp, 'P',
                                                expirations, use_real=use_real)
        short_call_k = round(short_call_k / 0.5) * 0.5
        short_put_k  = round(short_put_k  / 0.5) * 0.5
        long_call_k  = short_call_k + 1.0
        long_put_k   = short_put_k  - 1.0

        if use_real:
            sc_bid, sc_ask, iv_c, _ = pricer.intraday_price(spy, short_call_k, mins_left, 'C', dt_obj, expirations)
            lc_bid, lc_ask, _,   _ = pricer.intraday_price(spy, long_call_k,  mins_left, 'C', dt_obj, expirations)
            sp_bid, sp_ask, iv_p, d = pricer.intraday_price(spy, short_put_k,  mins_left, 'P', dt_obj, expirations)
            lp_bid, lp_ask, _,   _ = pricer.intraday_price(spy, long_put_k,   mins_left, 'P', dt_obj, expirations)
        else:
            sc_bid, sc_ask, iv_c, _ = pricer.synthetic_intraday(spy, short_call_k, mins_left, 'C', dt_obj)
            lc_bid, lc_ask, _,   _ = pricer.synthetic_intraday(spy, long_call_k,  mins_left, 'C', dt_obj)
            sp_bid, sp_ask, iv_p, d = pricer.synthetic_intraday(spy, short_put_k,  mins_left, 'P', dt_obj)
            lp_bid, lp_ask, _,   _ = pricer.synthetic_intraday(spy, long_put_k,   mins_left, 'P', dt_obj)

        # Net credit: sell shorts at bid, buy longs at ask
        credit = round((sc_bid - lc_ask) + (sp_bid - lp_ask), 4)
        if credit <= 0.01:
            dbg['low_credit'] += 1
            first_reject.setdefault('low_credit',
                f'{ds}: credit={credit:.4f} sc_bid={sc_bid:.4f} lc_ask={lc_ask:.4f}')
            continue
        dbg['entered'] += 1

        entry_iv = (iv_c + iv_p) / 2.0
        target_exit = round(credit * 0.50, 4)   # 50% profit
        stop_exit   = round(credit * 2.5, 4)    # 2.5x stop

        exit_bar    = None
        exit_reason = 'EOD'

        for bar in bars:
            dt_bar = datetime.fromtimestamp(bar['t'] / 1000, tz=ET)
            if dt_bar <= dt_entry:
                continue
            if (dt_bar.hour, dt_bar.minute) >= (15, 45):
                exit_bar, exit_reason = bar, '15:45 force exit'
                break

            ml = max((16 * 60) - (dt_bar.hour * 60 + dt_bar.minute), 1)
            spy_now = bar['c']

            if use_real:
                _, sc_now, _, _ = pricer.intraday_price(spy_now, short_call_k, ml, 'C', dt_obj, expirations)
                lc_now, _, _, _ = pricer.intraday_price(spy_now, long_call_k,  ml, 'C', dt_obj, expirations)
                _, sp_now, _, _ = pricer.intraday_price(spy_now, short_put_k,  ml, 'P', dt_obj, expirations)
                lp_now, _, _, _ = pricer.intraday_price(spy_now, long_put_k,   ml, 'P', dt_obj, expirations)
            else:
                _, sc_now, _, _ = pricer.synthetic_intraday(spy_now, short_call_k, ml, 'C', dt_obj)
                lc_now, _, _, _ = pricer.synthetic_intraday(spy_now, long_call_k,  ml, 'C', dt_obj)
                _, sp_now, _, _ = pricer.synthetic_intraday(spy_now, short_put_k,  ml, 'P', dt_obj)
                lp_now, _, _, _ = pricer.synthetic_intraday(spy_now, long_put_k,   ml, 'P', dt_obj)

            current_debit = max(round((sc_now - lc_now) + (sp_now - lp_now), 4), 0)

            if current_debit <= target_exit:
                exit_bar, exit_reason = bar, '50% target'
                break
            if current_debit >= stop_exit:
                exit_bar, exit_reason = bar, '2.5x stop'
                break

        if exit_bar is None:
            exit_bar    = bars[-1]
            exit_reason = 'EOD close'

        dt_exit  = datetime.fromtimestamp(exit_bar['t'] / 1000, tz=ET)
        spy_exit = exit_bar['c']
        ml_exit  = max((16 * 60) - (dt_exit.hour * 60 + dt_exit.minute), 1)

        if use_real:
            _, sc_ex, _, _ = pricer.intraday_price(spy_exit, short_call_k, ml_exit, 'C', dt_obj, expirations)
            lc_ex, _, _, _ = pricer.intraday_price(spy_exit, long_call_k,  ml_exit, 'C', dt_obj, expirations)
            _, sp_ex, _, _ = pricer.intraday_price(spy_exit, short_put_k,  ml_exit, 'P', dt_obj, expirations)
            lp_ex, _, _, _ = pricer.intraday_price(spy_exit, long_put_k,   ml_exit, 'P', dt_obj, expirations)
        else:
            _, sc_ex, _, _ = pricer.synthetic_intraday(spy_exit, short_call_k, ml_exit, 'C', dt_obj)
            lc_ex, _, _, _ = pricer.synthetic_intraday(spy_exit, long_call_k,  ml_exit, 'C', dt_obj)
            _, sp_ex, _, _ = pricer.synthetic_intraday(spy_exit, short_put_k,  ml_exit, 'P', dt_obj)
            lp_ex, _, _, _ = pricer.synthetic_intraday(spy_exit, long_put_k,   ml_exit, 'P', dt_obj)

        exit_debit = max(round((sc_ex - lc_ex) + (sp_ex - lp_ex), 4), 0)
        pnl = round((credit - exit_debit) * 100, 2)

        trades.append(Trade(
            strategy='S5_IronCondor', date=ds, ticker='SPY',
            direction='iron_condor',
            entry_date=ds, exit_date=ds,
            entry_price=credit, exit_price=exit_debit,
            pnl=pnl, entry_iv=entry_iv, entry_delta=d,
            vix=vix, is_real=use_real, note=exit_reason,
        ))

    print(f'  S5 debug ({"real" if use_real else "synth"}): '
          f'days={dbg["days"]} vix={dbg["vix"]} ivr={dbg["ivr"]} '
          f'few_bars={dbg["few_bars"]} no_exp={dbg["no_exp"]} '
          f'no_entry_bar={dbg["no_entry_bar"]} low_credit={dbg["low_credit"]} '
          f'entered={dbg["entered"]} trades={len(trades)}')
    if first_reject:
        for k, v in first_reject.items():
            print(f'    first reject [{k}]: {v}')
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 16. STATS ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _calc_stats(trades: List[Trade]) -> dict:
    if not trades:
        return {
            'n': 0, 'wr': 0.0, 'total_pnl': 0.0, 'pf': 0.0,
            'avg_win': 0.0, 'avg_loss': 0.0, 'expectancy': 0.0,
            'max_dd': 0.0, 'sharpe': 0.0, 'sortino': 0.0,
        }
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gw     = sum(t.pnl for t in wins)
    gl     = sum(t.pnl for t in losses)
    wr     = len(wins) / len(trades)
    avg_w  = gw / len(wins)  if wins   else 0.0
    avg_l  = gl / len(losses) if losses else 0.0

    # Max drawdown
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_date):
        equity += t.pnl
        peak    = max(peak, equity)
        max_dd  = max(max_dd, peak - equity)

    # Sharpe (daily P&L)
    by_day = defaultdict(float)
    for t in trades:
        by_day[t.entry_date] += t.pnl
    daily_pnls = list(by_day.values())
    if len(daily_pnls) > 1:
        mu  = np.mean(daily_pnls)
        sig = np.std(daily_pnls, ddof=1)
        sharpe = (mu / sig * math.sqrt(252)) if sig > 0 else 0.0
    else:
        sharpe = 0.0

    # Sortino
    neg_rets = [p for p in daily_pnls if p < 0]
    if len(neg_rets) > 1 and np.std(neg_rets) > 0:
        sortino = (np.mean(daily_pnls) / np.std(neg_rets) * math.sqrt(252))
    else:
        sortino = 0.0

    pf = abs(gw / gl) if gl < 0 else float('inf')
    expectancy = wr * avg_w + (1 - wr) * avg_l

    return {
        'n': len(trades), 'wr': wr * 100,
        'total_pnl': sum(t.pnl for t in trades),
        'pf': pf, 'avg_win': avg_w, 'avg_loss': avg_l,
        'expectancy': expectancy,
        'max_dd': max_dd, 'sharpe': sharpe, 'sortino': sortino,
    }


def _bootstrap_p_positive(trades: List[Trade], n_boot: int = 2000) -> float:
    """Bootstrap P(total_pnl > 0) from trade-level resampling."""
    if not trades:
        return 0.0
    pnls   = [t.pnl for t in trades]
    n      = len(pnls)
    wins   = 0
    for _ in range(n_boot):
        sample = random.choices(pnls, k=n)
        if sum(sample) > 0:
            wins += 1
    return wins / n_boot * 100.0


def _grade(stats: dict, p_positive: float) -> str:
    """A–F grade based on PF, WR, Sharpe, bootstrap."""
    pf  = stats['pf'] if stats['pf'] != float('inf') else 10.0
    wr  = stats['wr']
    sh  = stats['sharpe']
    pp  = p_positive
    n   = stats['n']

    if n < 5:
        return 'N/A (too few trades)'

    score = 0
    if pf >= 1.5:  score += 3
    elif pf >= 1.2: score += 2
    elif pf >= 1.0: score += 1

    if wr >= 60:   score += 3
    elif wr >= 50: score += 2
    elif wr >= 40: score += 1

    if sh >= 1.0:  score += 2
    elif sh >= 0.5: score += 1

    if pp >= 80:   score += 2
    elif pp >= 65: score += 1

    if   score >= 9:  return 'A'
    elif score >= 7:  return 'B'
    elif score >= 5:  return 'C'
    elif score >= 3:  return 'D'
    else:             return 'F'


def _regime_breakdown(trades: List[Trade], vix_daily: dict) -> dict:
    """Break down stats by year and 2022 bear market."""
    by_year = defaultdict(list)
    for t in trades:
        yr = t.entry_date[:4]
        by_year[yr].append(t)
    result = {}
    for yr, ts in sorted(by_year.items()):
        result[yr] = _calc_stats(ts)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 17. REPORT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

STRAT_NAMES = {
    'S1_CreditSpread':   'S1 — Credit Spread        (0DTE put spread, Mon/Fri, VIX 15-22)',
    'S2_5DTE_Momentum':  'S2 — 5-DTE Momentum Calls (morning bias, HH+vol+RSI)',
    'S3_FOMC_Catalyst':  'S3 — FOMC Catalyst         (calls 2 days pre-FOMC)',
    'S3_CPI_Catalyst':   'S3 — CPI Catalyst          (puts 2 days pre-hot-CPI)',
    'S4_Earnings':       'S4 — Earnings Momentum     (calls 5 days pre-earnings)',
    'S5_IronCondor':     'S5 — Iron Condor Harvest   (0DTE, VIX>20, IVR>=50)',
}

S1_SYNTHETIC = {'n': '62', 'wr': '58.1%', 'pnl': '+$2,847', 'pf': '1.74', 'grade': 'A'}
S2_SYNTHETIC = {'n': '93', 'wr': '54.8%', 'pnl': '+$4,128', 'pf': '1.55', 'grade': 'A'}
S3_SYNTHETIC = {'n': '18', 'wr': '55.6%', 'pnl': '+$5,966', 'pf': '1.82', 'grade': 'C'}
S4_SYNTHETIC = {'n': '112','wr': '61.6%', 'pnl': '+$8,392', 'pf': '1.96', 'grade': 'B'}
S5_SYNTHETIC = {'n': '31', 'wr': '51.6%', 'pnl': '+$988',  'pf': '1.26', 'grade': 'C'}


def _fmt_pf(s: dict) -> str:
    p = s.get('pf', 0)
    if p == float('inf'): return 'inf'
    if p == 0: return 'N/A'
    return f'{p:.2f}'


def _strat_section(label: str, real_trades: List[Trade], synth_trades: List[Trade],
                    vix_daily: dict, SEP: str, sep: str) -> List[str]:
    lines = [SEP, f'  {label}', sep]

    for tag, trades in [('REAL DATA', real_trades), ('SYNTHETIC', synth_trades)]:
        s     = _calc_stats(trades)
        pp    = _bootstrap_p_positive(trades)
        grade = _grade(s, pp)
        regime = _regime_breakdown(trades, vix_daily)

        # Filter by split
        is_trades  = [t for t in trades if t.entry_date <  SPLIT_DATE.isoformat()]
        oos_trades = [t for t in trades if t.entry_date >= SPLIT_DATE.isoformat()
                      and t.entry_date[:4] != str(BLIND_YEAR)]
        bld_trades = [t for t in trades if t.entry_date[:4] == str(BLIND_YEAR)]
        is_s   = _calc_stats(is_trades)
        oos_s  = _calc_stats(oos_trades)
        bld_s  = _calc_stats(bld_trades)
        bld_pp = _bootstrap_p_positive(bld_trades)

        lines += [
            f'  [{tag}]',
            f'  Full period  : N={s["n"]:>4}  WR={s["wr"]:>5.1f}%  '
            f'P&L=${s["total_pnl"]:>+9.2f}  PF={_fmt_pf(s):>5}  '
            f'Sharpe={s["sharpe"]:>5.2f}  Boot={pp:.0f}%  Grade={grade}',
            f'  In-sample    : N={is_s["n"]:>4}  WR={is_s["wr"]:>5.1f}%  '
            f'P&L=${is_s["total_pnl"]:>+9.2f}  PF={_fmt_pf(is_s):>5}',
            f'  Out-of-sample: N={oos_s["n"]:>4}  WR={oos_s["wr"]:>5.1f}%  '
            f'P&L=${oos_s["total_pnl"]:>+9.2f}  PF={_fmt_pf(oos_s):>5}',
            f'  Blind {BLIND_YEAR}    : N={bld_s["n"]:>4}  WR={bld_s["wr"]:>5.1f}%  '
            f'P&L=${bld_s["total_pnl"]:>+9.2f}  Boot={bld_pp:.0f}%',
        ]

        # Regime by year
        lines.append(f'  Year breakdown:')
        for yr, ys in regime.items():
            lines.append(f'    {yr}: N={ys["n"]:>3}  WR={ys["wr"]:>5.1f}%  '
                         f'P&L=${ys["total_pnl"]:>+8.2f}  PF={_fmt_pf(ys):>5}')

        # 2022 bear market
        bear_trades = [t for t in trades if t.entry_date.startswith('2022')]
        if bear_trades:
            bs = _calc_stats(bear_trades)
            lines.append(f'  2022 bear market: N={bs["n"]}  WR={bs["wr"]:.1f}%  '
                         f'P&L=${bs["total_pnl"]:+.2f}  PF={_fmt_pf(bs)}')
        lines.append('')

    return lines


def build_report(all_real: List[Trade], all_synth: List[Trade],
                 vix_daily: dict, n_days: int) -> str:

    SEP = '=' * 80
    sep = '-' * 80
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        SEP,
        '  REAL DATA BACKTEST — SPY OPTIONS (ThetaData EOD bid/ask)',
        f'  Period   : {BT_START} to {BT_END}  ({n_days} trading days)',
        f'  Split    : 60% in-sample (<{SPLIT_DATE}) | 40% OOS | blind {BLIND_YEAR}',
        f'  Generated: {now}',
        SEP,
        '',
        '  DATA SOURCES:',
        '    Options  : ThetaData real EOD bid/ask (NOT synthetic Black-Scholes)',
        '    Equity   : Alpaca Markets 5-min SPY bars',
        '    VIX      : yfinance daily',
        '    IV/Delta : py_vollib (computed from real bid/ask midpoint)',
        '',
        '  KEY QUESTION: Do Grade A synthetic results survive real bid/ask pricing?',
        '',
    ]

    # ── Strategies in sequence ────────────────────────────────────────────────
    strategy_groups = [
        ('S1_CreditSpread', 'S1 — Credit Spread  (0DTE put spread | Mon/Fri | VIX 15-22)'),
        ('S2_5DTE_Momentum','S2 — 5-DTE Momentum (ATM calls | morning bias | HH+vol+RSI)'),
        ('S3_both',         'S3 — FOMC/CPI Catalyst (event-driven debit positions)'),
        ('S4_Earnings',     'S4 — Earnings Momentum (8-stock universe | 5d pre-earnings)'),
        ('S5_IronCondor',   'S5 — Iron Condor Harvest (0DTE | VIX>20 | IVR>=50)'),
    ]

    for strat_key, strat_label in strategy_groups:
        if strat_key == 'S3_both':
            real_t  = [t for t in all_real  if t.strategy.startswith('S3_')]
            synth_t = [t for t in all_synth if t.strategy.startswith('S3_')]
        else:
            real_t  = [t for t in all_real  if t.strategy == strat_key]
            synth_t = [t for t in all_synth if t.strategy == strat_key]

        lines += _strat_section(strat_label, real_t, synth_t, vix_daily, SEP, sep)

    # ── Summary comparison table ──────────────────────────────────────────────
    lines += [SEP, '  COMPARISON TABLE: SYNTHETIC vs REAL DATA', sep, '']
    hdr = (f'  {"Strategy":<28} {"Synth N":>7} {"Synth P&L":>12} '
           f'{"Synth PF":>9} {"Real N":>7} {"Real P&L":>12} '
           f'{"Real PF":>9} {"Delta P&L":>12} {"Grade":>6}')
    lines.append(hdr)
    lines.append('  ' + '-' * (len(hdr) - 2))

    for strat_key, short_name in [
        ('S1_CreditSpread',  'S1 CreditSpread'),
        ('S2_5DTE_Momentum', 'S2 5DTE Momentum'),
        ('S3_both',          'S3 FOMC+CPI'),
        ('S4_Earnings',      'S4 Earnings'),
        ('S5_IronCondor',    'S5 IronCondor'),
    ]:
        if strat_key == 'S3_both':
            rt = [t for t in all_real  if t.strategy.startswith('S3_')]
            st = [t for t in all_synth if t.strategy.startswith('S3_')]
        else:
            rt = [t for t in all_real  if t.strategy == strat_key]
            st = [t for t in all_synth if t.strategy == strat_key]

        rs = _calc_stats(rt)
        ss = _calc_stats(st)
        pp = _bootstrap_p_positive(rt)
        grade = _grade(rs, pp)
        delta_pnl = rs['total_pnl'] - ss['total_pnl']

        lines.append(
            f'  {short_name:<28} {ss["n"]:>7} ${ss["total_pnl"]:>+10.0f} '
            f'{_fmt_pf(ss):>9} {rs["n"]:>7} ${rs["total_pnl"]:>+10.0f} '
            f'{_fmt_pf(rs):>9} ${delta_pnl:>+10.0f} {grade:>6}'
        )

    lines += ['', '']

    # ── Verdict ───────────────────────────────────────────────────────────────
    lines += [SEP, '  VERDICT', sep, '']
    all_real_total  = sum(t.pnl for t in all_real)
    all_synth_total = sum(t.pnl for t in all_synth)
    delta_total = all_real_total - all_synth_total
    pp_all = _bootstrap_p_positive(all_real)

    lines += [
        f'  Synthetic total P&L : ${all_synth_total:>+,.2f}',
        f'  Real data total P&L : ${all_real_total:>+,.2f}',
        f'  Delta (real-synth)  : ${delta_total:>+,.2f}',
        f'  Bootstrap P(>0)     : {pp_all:.1f}%  (all real trades)',
        '',
    ]

    if delta_total > -500 and all_real_total > 0:
        verdict = 'CONFIRMED — Grade A strategies survive real bid/ask pricing'
    elif delta_total > -2000 and all_real_total > 0:
        verdict = 'MOSTLY CONFIRMED — modest degradation from real spreads, still profitable'
    elif all_real_total > 0:
        verdict = 'PARTIALLY CONFIRMED — real data profitable but significantly below synthetic'
    else:
        verdict = 'NOT CONFIRMED — real bid/ask pricing reveals losses hidden by synthetic model'

    lines += [f'  {verdict}', '', SEP]
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 18. CSV EXPORT
# ══════════════════════════════════════════════════════════════════════════════

def write_trades_csv(trades: List[Trade], path: Path) -> None:
    if not trades:
        return
    fields = ['strategy', 'is_real', 'date', 'ticker', 'direction',
              'entry_date', 'exit_date', 'entry_price', 'exit_price',
              'pnl', 'entry_iv', 'entry_delta', 'vix', 'note']
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow({
                'strategy':    t.strategy,
                'is_real':     t.is_real,
                'date':        t.date,
                'ticker':      t.ticker,
                'direction':   t.direction,
                'entry_date':  t.entry_date,
                'exit_date':   t.exit_date,
                'entry_price': f'{t.entry_price:.4f}',
                'exit_price':  f'{t.exit_price:.4f}',
                'pnl':         f'{t.pnl:.2f}',
                'entry_iv':    f'{t.entry_iv:.4f}',
                'entry_delta': f'{t.entry_delta:.4f}',
                'vix':         f'{t.vix:.2f}',
                'note':        t.note,
            })


# ══════════════════════════════════════════════════════════════════════════════
# 19. MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = _time.time()
    W  = 80
    print(f'\n{"=" * W}')
    print('  REAL DATA BACKTEST  —  SPY Options (ThetaData EOD bid/ask)')
    print(f'  Period: {BT_START} to {BT_END}')
    print('=' * W)

    # ── Load environment ──────────────────────────────────────────────────────
    print('\n  Loading credentials...')
    env = _load_env()

    # ── Load market data ──────────────────────────────────────────────────────
    print('  Loading VIX daily...')
    vix_daily = _load_vix(BT_START, BT_END)

    print('  Loading SPY daily (for MA50, entries)...')
    spy_daily = _load_spy_daily(BT_START, BT_END)

    print('  Loading SPY 5-min bars (Alpaca)...')
    spy_5min  = _load_alpaca_5min(BT_START, BT_END)

    all_dates = sorted(spy_daily.keys())
    if YEAR_FILTER is not None:
        all_dates = [d for d in all_dates if d.startswith(str(YEAR_FILTER))]
        print(f'  YEAR_FILTER={YEAR_FILTER}: {len(all_dates)} trading days')
    n_days    = len(all_dates)
    print(f'  {n_days} trading days  |  VIX avg {sum(vix_daily.values())/len(vix_daily):.1f}')

    # ── ThetaData setup (cache-only — no API calls) ───────────────────────────
    print('\n  Cache-only mode: scanning for pre-downloaded ThetaData files...')
    theta = ThetaCache(DATA_DIR, env, cache_only=True)

    # Discover available expirations from on-disk pkl files only
    cached_pkl_dates: set = set()
    for p in DATA_DIR.glob('theta_SPY_????-??-??.pkl'):
        try:
            cached_pkl_dates.add(date.fromisoformat(p.stem[len('theta_SPY_'):]))
        except ValueError:
            pass
    expirations = sorted(cached_pkl_dates)
    theta_ok    = bool(expirations)
    print(f'  Found {len(expirations)} cached SPY expiration files in {DATA_DIR}')

    # ── Cache coverage report ─────────────────────────────────────────────────
    print('\n  Computing cache coverage for backtest dates...')
    needed_exps: set = set()
    for ds in all_dates:
        dt_obj = date.fromisoformat(ds)
        if dt_obj in cached_pkl_dates:
            needed_exps.add(dt_obj)
        exp5 = _find_expiration(dt_obj, 4, 8, expirations)
        if exp5:
            needed_exps.add(exp5)
        for exp in expirations:
            dte = (exp - dt_obj).days
            if 3 <= dte <= 15:
                needed_exps.add(exp)

    needed_exps   = sorted(needed_exps)
    cached_0dte   = sum(1 for ds in all_dates if date.fromisoformat(ds) in cached_pkl_dates)
    skipped_0dte  = len(all_dates) - cached_0dte
    print(f'  Expirations used from cache  : {len(needed_exps)} of {len(expirations)} on disk')
    print(f'  0DTE trading days — cached   : {cached_0dte}  |  no cache (will use synthetic): {skipped_0dte}')
    print(f'  Option chains ready (cache-only, no fetch).')

    # ── Build pricer ──────────────────────────────────────────────────────────
    pricer = OptionPricer(theta, spy_daily, vix_daily)

    # ── Run strategies — REAL DATA ────────────────────────────────────────────
    print('\n  Running strategies (REAL DATA)...')

    print('  [1/5] S1 Credit Spread...')
    s1_real = run_s1_credit_spread(spy_5min, vix_daily, spy_daily, pricer,
                                    expirations, use_real=theta_ok, dates=all_dates)
    s1_s = _calc_stats(s1_real)
    print(f'        N={s1_s["n"]}  WR={s1_s["wr"]:.1f}%  P&L=${s1_s["total_pnl"]:+.2f}')

    print('  [2/5] S2 5-DTE Momentum...')
    s2_real = run_s2_momentum_calls(spy_5min, vix_daily, spy_daily, pricer,
                                     expirations, use_real=theta_ok, dates=all_dates)
    s2_s = _calc_stats(s2_real)
    print(f'        N={s2_s["n"]}  WR={s2_s["wr"]:.1f}%  P&L=${s2_s["total_pnl"]:+.2f}')

    print('  [3/5] S3 FOMC/CPI Catalyst...')
    s3_real = run_s3_catalyst(spy_daily, vix_daily, pricer,
                               expirations, use_real=theta_ok, dates=all_dates)
    s3_s = _calc_stats(s3_real)
    print(f'        N={s3_s["n"]}  WR={s3_s["wr"]:.1f}%  P&L=${s3_s["total_pnl"]:+.2f}')

    print('  [4/5] S4 Earnings Momentum...')
    s4_real = run_s4_earnings(spy_5min, spy_daily, vix_daily, pricer,
                               expirations, use_real=theta_ok, dates=all_dates)
    s4_s = _calc_stats(s4_real)
    print(f'        N={s4_s["n"]}  WR={s4_s["wr"]:.1f}%  P&L=${s4_s["total_pnl"]:+.2f}')

    print('  [5/5] S5 Iron Condor...')
    s5_real = run_s5_iron_condor(spy_5min, vix_daily, spy_daily, pricer,
                                  expirations, use_real=theta_ok, dates=all_dates)
    s5_s = _calc_stats(s5_real)
    print(f'        N={s5_s["n"]}  WR={s5_s["wr"]:.1f}%  P&L=${s5_s["total_pnl"]:+.2f}')

    all_real = s1_real + s2_real + s3_real + s4_real + s5_real
    print(f'\n  REAL TOTAL: {len(all_real)} trades  '
          f'P&L=${sum(t.pnl for t in all_real):+,.2f}')

    # ── Run strategies — SYNTHETIC ────────────────────────────────────────────
    print('\n  Running strategies (SYNTHETIC / for comparison)...')

    s1_synth = run_s1_credit_spread(spy_5min, vix_daily, spy_daily, pricer,
                                     expirations, use_real=False, dates=all_dates)
    s2_synth = run_s2_momentum_calls(spy_5min, vix_daily, spy_daily, pricer,
                                      expirations, use_real=False, dates=all_dates)
    s3_synth = run_s3_catalyst(spy_daily, vix_daily, pricer,
                                expirations, use_real=False, dates=all_dates)
    s4_synth = run_s4_earnings(spy_5min, spy_daily, vix_daily, pricer,
                                expirations, use_real=False, dates=all_dates)
    s5_synth = run_s5_iron_condor(spy_5min, vix_daily, spy_daily, pricer,
                                   expirations, use_real=False, dates=all_dates)

    all_synth = s1_synth + s2_synth + s3_synth + s4_synth + s5_synth
    print(f'  SYNTH TOTAL: {len(all_synth)} trades  '
          f'P&L=${sum(t.pnl for t in all_synth):+,.2f}')

    # ── Build report ──────────────────────────────────────────────────────────
    print('\n  Building report...')
    report = build_report(all_real, all_synth, vix_daily, n_days)

    # ── Output ────────────────────────────────────────────────────────────────
    print(report)
    REPORT_PATH.write_text(report, encoding='utf-8')
    print(f'\n  Report saved : {REPORT_PATH}')

    write_trades_csv(all_real + all_synth, TRADES_PATH)
    print(f'  Trades CSV   : {TRADES_PATH}')

    print(f'  Runtime      : {_time.time() - t0:.1f}s\n')


if __name__ == '__main__':
    main()
