"""
data_feeds.py — Multi-source market data layer.

Sources
-------
Tradier   — real-time quotes, options chains, and VIX (rate-limited to 200 calls/hr).
Alpaca    — OHLCV bars (5-min and daily) via the data API.

Rate limiting
-------------
Tradier enforces 200 calls/hour on the free paper-trading key.  That equals
one call every 18 seconds.  _tradier_get() sleeps as needed before every
outbound request.

Retry policy
------------
Both _tradier_get() and _alpaca_get() use 3-attempt exponential back-off
(1 s → 2 s → 4 s).  After exhausting retries the method returns {}/empty DF
and logs an error; it never raises to callers.
"""

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import pytz
import requests

TRADIER_BASE = "https://api.tradier.com/v1"
ALPACA_BASE_DATA = "https://data.alpaca.markets/v2"
NY_TZ = pytz.timezone("America/New_York")


class DataFeeds:
    """Unified data-feed client for Tradier and Alpaca."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("data_feeds")

        tradier_key = os.environ.get("TRADIER_API_KEY", "")
        alpaca_key = os.environ.get("ALPACA_API_KEY", "")
        alpaca_secret = os.environ.get("ALPACA_API_SECRET", "")

        if not tradier_key:
            self.logger.warning("TRADIER_API_KEY not set — Tradier calls will fail.")
        if not alpaca_key or not alpaca_secret:
            self.logger.warning(
                "ALPACA_API_KEY / ALPACA_API_SECRET not set — Alpaca calls will fail."
            )

        self._tradier_headers = {
            "Authorization": f"Bearer {tradier_key}",
            "Accept": "application/json",
        }
        self._alpaca_headers = {
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
        }

        # Tradier rate-limit state (shared across threads — use a lock).
        self._tradier_lock = threading.Lock()
        self._last_tradier_call: float = 0.0
        self._tradier_min_interval: float = 18.0  # 200 calls/hour → 1 per 18 s

        # VIX cache: (value, fetched_at_timestamp)
        self._vix_cache: tuple = (None, 0.0)
        self._vix_cache_ttl: float = 300.0  # 5 minutes

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _tradier_get(self, endpoint: str, params: dict = None) -> dict:
        """Rate-limited GET against the Tradier v1 API.

        Enforces a minimum gap of ``_tradier_min_interval`` seconds between
        consecutive outbound calls.  Retries up to 3 times with exponential
        back-off on transient errors.

        Returns {} on failure after all retries.
        """
        url = f"{TRADIER_BASE}{endpoint}"
        delay = 1.0

        with self._tradier_lock:
            now = time.monotonic()
            gap = now - self._last_tradier_call
            if gap < self._tradier_min_interval:
                sleep_for = self._tradier_min_interval - gap
                self.logger.debug(
                    "Tradier rate-limit: sleeping %.2f s before %s", sleep_for, endpoint
                )
                time.sleep(sleep_for)
            self._last_tradier_call = time.monotonic()

        for attempt in range(3):
            try:
                resp = requests.get(
                    url,
                    headers=self._tradier_headers,
                    params=params or {},
                    timeout=10,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as exc:
                self.logger.error(
                    "Tradier HTTP error [attempt %d/3] %s: %s", attempt + 1, endpoint, exc
                )
            except requests.exceptions.RequestException as exc:
                self.logger.error(
                    "Tradier request error [attempt %d/3] %s: %s", attempt + 1, endpoint, exc
                )
            except json.JSONDecodeError as exc:
                self.logger.error(
                    "Tradier JSON decode error [attempt %d/3] %s: %s", attempt + 1, endpoint, exc
                )

            if attempt < 2:
                time.sleep(delay)
                delay *= 2

        self.logger.error("Tradier: all retries exhausted for %s", endpoint)
        return {}

    def _alpaca_get(self, endpoint: str, params: dict = None) -> dict:
        """GET against the Alpaca data API with exponential back-off.

        Returns {} on failure after all retries.
        """
        url = f"{ALPACA_BASE_DATA}{endpoint}"
        delay = 1.0

        for attempt in range(3):
            try:
                resp = requests.get(
                    url,
                    headers=self._alpaca_headers,
                    params=params or {},
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as exc:
                self.logger.error(
                    "Alpaca HTTP error [attempt %d/3] %s: %s", attempt + 1, endpoint, exc
                )
            except requests.exceptions.RequestException as exc:
                self.logger.error(
                    "Alpaca request error [attempt %d/3] %s: %s", attempt + 1, endpoint, exc
                )
            except json.JSONDecodeError as exc:
                self.logger.error(
                    "Alpaca JSON decode error [attempt %d/3] %s: %s", attempt + 1, endpoint, exc
                )

            if attempt < 2:
                time.sleep(delay)
                delay *= 2

        self.logger.error("Alpaca: all retries exhausted for %s", endpoint)
        return {}

    # ------------------------------------------------------------------
    # Tradier market data
    # ------------------------------------------------------------------

    def get_tradier_quote(self, symbol: str) -> dict:
        """Fetch a real-time quote for *symbol* from Tradier.

        Returns
        -------
        dict with keys: symbol, bid, ask, last, volume, vwap, change_percentage.
        Returns {} on any error.
        """
        data = self._tradier_get("/markets/quotes", params={"symbols": symbol})
        try:
            quote = data["quotes"]["quote"]
            # Tradier returns a list when multiple symbols are requested;
            # normalise to a single dict.
            if isinstance(quote, list):
                quote = quote[0]
            return {
                "symbol": quote.get("symbol", symbol),
                "bid": float(quote.get("bid") or 0.0),
                "ask": float(quote.get("ask") or 0.0),
                "last": float(quote.get("last") or 0.0),
                "volume": int(quote.get("volume") or 0),
                "vwap": float(quote.get("vwap") or 0.0),
                "change_percentage": float(quote.get("change_percentage") or 0.0),
            }
        except (KeyError, TypeError, ValueError) as exc:
            self.logger.error("get_tradier_quote parse error for %s: %s", symbol, exc)
            return {}

    def get_tradier_options_chain(self, symbol: str, expiration: str) -> pd.DataFrame:
        """Fetch the options chain for *symbol* at *expiration* (YYYY-MM-DD).

        Returns
        -------
        pd.DataFrame with columns:
            strike, option_type, bid, ask, mid, delta, gamma, theta,
            volume, open_interest.
        Returns an empty DataFrame on any error.
        """
        data = self._tradier_get(
            "/markets/options/chains",
            params={"symbol": symbol, "expiration": expiration, "greeks": "false"},
        )
        try:
            options = data.get("options", {}).get("option", [])
            if not options:
                return pd.DataFrame()
            rows = []
            for opt in options:
                greeks = opt.get("greeks") or {}
                mid = (
                    (float(opt.get("bid") or 0.0) + float(opt.get("ask") or 0.0)) / 2.0
                )
                rows.append(
                    {
                        "strike": float(opt.get("strike") or 0.0),
                        "option_type": opt.get("option_type", "").lower(),
                        "bid": float(opt.get("bid") or 0.0),
                        "ask": float(opt.get("ask") or 0.0),
                        "mid": round(mid, 4),
                        "delta": float(greeks.get("delta") or 0.0),
                        "gamma": float(greeks.get("gamma") or 0.0),
                        "theta": float(greeks.get("theta") or 0.0),
                        "volume": int(opt.get("volume") or 0),
                        "open_interest": int(opt.get("open_interest") or 0),
                    }
                )
            return pd.DataFrame(rows)
        except (KeyError, TypeError, ValueError) as exc:
            self.logger.error(
                "get_tradier_options_chain parse error %s %s: %s",
                symbol, expiration, exc,
            )
            return pd.DataFrame()

    def get_tradier_expirations(self, symbol: str) -> list:
        """Return a sorted list of option-expiration date strings for *symbol*.

        Returns
        -------
        list[str]  ISO date strings (YYYY-MM-DD), sorted ascending.
        """
        data = self._tradier_get(
            "/markets/options/expirations", params={"symbol": symbol}
        )
        try:
            expirations = data.get("expirations", {}).get("date", [])
            if isinstance(expirations, str):
                expirations = [expirations]
            return sorted(expirations)
        except (KeyError, TypeError) as exc:
            self.logger.error(
                "get_tradier_expirations parse error for %s: %s", symbol, exc
            )
            return []

    def get_next_expiry(self, symbol: str = "SPY", dte_target: int = 0,
                        weekly: bool = False) -> str:
        """Return today's date if it is a valid expiry, otherwise the next valid expiry.

        Uses includeAllRoots=true to capture all SPY expirations (Mon/Wed/Fri).
        When dte_target > 0, returns the first expiry at least that many days out.
        When weekly=True, returns the first Friday expiry on or after today.

        Returns
        -------
        str  YYYY-MM-DD, or '' if no suitable expiry is found.
        """
        data = self._tradier_get(
            "/markets/options/expirations",
            params={"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
        )
        try:
            expirations = data.get("expirations", {}).get("date", [])
            if isinstance(expirations, str):
                expirations = [expirations]
            expirations = sorted(expirations)
        except (KeyError, TypeError) as exc:
            self.logger.error("get_next_expiry parse error for %s: %s", symbol, exc)
            return ""

        today = date.today()
        today_str = today.isoformat()

        # 0DTE default path: return today if valid, else next expiry after today
        if not weekly and not dte_target:
            if today_str in expirations:
                return today_str
            for exp_str in expirations:
                if exp_str > today_str:
                    return exp_str
            self.logger.warning("get_next_expiry: no expiry found for %s", symbol)
            return ""

        # Legacy paths for callers that specify weekly or dte_target
        for exp_str in expirations:
            try:
                exp_date = date.fromisoformat(exp_str)
                if weekly:
                    if exp_date >= today and exp_date.weekday() == 4:
                        return exp_str
                elif dte_target and (exp_date - today).days >= dte_target:
                    return exp_str
            except ValueError:
                continue

        self.logger.warning(
            "get_next_expiry: no expiry found for %s (weekly=%s, dte_target=%s)",
            symbol, weekly, dte_target,
        )
        return ""

    # ------------------------------------------------------------------
    # Alpaca bar data
    # ------------------------------------------------------------------

    def _parse_alpaca_bars(self, data: dict, symbol: str) -> pd.DataFrame:
        """Convert raw Alpaca bar JSON into a timezone-aware DataFrame."""
        bars = data.get("bars", [])
        if not bars:
            return pd.DataFrame()
        rows = []
        for bar in bars:
            rows.append(
                {
                    "timestamp": bar.get("t"),
                    "open": float(bar.get("o", 0.0)),
                    "high": float(bar.get("h", 0.0)),
                    "low": float(bar.get("l", 0.0)),
                    "close": float(bar.get("c", 0.0)),
                    "volume": int(bar.get("v", 0)),
                }
            )
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(NY_TZ)
        df = df.set_index("timestamp").sort_index()
        return df

    def get_spy_5min(self, limit: int = 100) -> pd.DataFrame:
        """Fetch the last *limit* 5-minute bars for SPY from Alpaca.

        Returns
        -------
        pd.DataFrame indexed by NY-localised datetime, columns: open, high, low, close, volume.
        Empty DataFrame on error.
        """
        data = self._alpaca_get(
            "/stocks/SPY/bars",
            params={"timeframe": "5Min", "limit": limit, "feed": "iex"},
        )
        if not data:
            return pd.DataFrame()
        df = self._parse_alpaca_bars(data, "SPY")
        if df.empty:
            self.logger.warning("get_spy_5min: received empty bar data from Alpaca.")
        return df

    def get_spy_daily(self, lookback: int = 60) -> pd.DataFrame:
        """Fetch the last *lookback* daily bars for SPY from Alpaca.

        Returns
        -------
        pd.DataFrame indexed by date, columns: open, high, low, close, volume.
        Empty DataFrame on error.
        """
        return self.get_ticker_daily("SPY", lookback=lookback)

    def get_ticker_daily(self, symbol: str, lookback: int = 60) -> pd.DataFrame:
        """Fetch the last *lookback* daily bars for *symbol* from Alpaca.

        Returns
        -------
        pd.DataFrame indexed by date, columns: open, high, low, close, volume.
        Empty DataFrame on error.
        """
        data = self._alpaca_get(
            f"/stocks/{symbol}/bars",
            params={"timeframe": "1Day", "limit": lookback, "feed": "iex"},
        )
        if not data:
            return pd.DataFrame()
        df = self._parse_alpaca_bars(data, symbol)
        if df.empty:
            self.logger.warning("get_ticker_daily: empty bar data for %s.", symbol)
            return df
        # Re-index on plain date objects for daily data.
        df.index = df.index.date
        df.index.name = "date"
        return df

    # ------------------------------------------------------------------
    # VIX and SPY spot price
    # ------------------------------------------------------------------

    def get_vix(self) -> float:
        """Return the latest VIX spot price via Tradier.

        Result is cached for 5 minutes.  Returns 0.0 on any error — 0.0 fails
        every strategy's vix_min check, so a feed outage blocks entries instead
        of fabricating a tradeable value.  Failures are not cached.
        """
        cached_value, fetched_at = self._vix_cache
        if cached_value is not None and (time.monotonic() - fetched_at) < self._vix_cache_ttl:
            return cached_value

        vix = None
        try:
            data = self._tradier_get("/markets/quotes", params={"symbols": "VIX"})
            quote = data["quotes"]["quote"]
            if isinstance(quote, list):
                quote = quote[0]
            vix = float(quote["last"])
        except Exception as exc:
            self.logger.warning("get_vix Tradier failed: %s. Returning 0.0 (blocks entries).", exc)
            return 0.0

        # Sanity-check: VIX should be between 5 and 150 under normal conditions.
        if vix is None or vix < 5.0 or vix > 150.0:
            self.logger.warning(
                "get_vix: suspicious value %.2f — returning 0.0 (blocks entries).", vix or -1
            )
            return 0.0

        self._vix_cache = (vix, time.monotonic())
        return vix

    def get_spy_vwap(self) -> float:
        """Compute cumulative VWAP from Tradier timesales bars since market open."""
        try:
            import datetime as _dtm
            now_ny = _dtm.datetime.now(NY_TZ)
            start_str = now_ny.replace(hour=9, minute=30, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M')
            end_str   = now_ny.strftime('%Y-%m-%d %H:%M')
            data  = self._tradier_get('/markets/timesales', params={
                'symbol': 'SPY', 'interval': '5min',
                'start': start_str, 'end': end_str, 'session_filter': 'open'
            })
            bars = data.get('series', {}).get('data', [])
            if not bars:
                self.logger.warning('get_spy_vwap: no timesales bars — returning 0.0')
                return 0.0
            total_pv = sum(float(b['vwap']) * float(b['volume']) for b in bars)
            total_v  = sum(float(b['volume']) for b in bars)
            if total_v == 0:
                return 0.0
            vwap = round(total_pv / total_v, 4)
            self.logger.info('get_spy_vwap: %.4f (%d bars)', vwap, len(bars))
            return vwap
        except Exception as exc:
            self.logger.error('get_spy_vwap error: %s — returning 0.0', exc)
            return 0.0
    def get_spy_price(self) -> float:
        """Return the latest SPY last-trade price via Tradier.

        Returns 0.0 on failure.
        """
        quote = self.get_tradier_quote("SPY")
        if quote and quote.get("last", 0.0) > 0.0:
            return quote["last"]

        self.logger.warning("get_spy_price: Tradier returned no valid price.")
        return 0.0

    # ------------------------------------------------------------------
    # Derived / utility
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Convenience wrappers used by strategy modules
    # ------------------------------------------------------------------

    def get_intraday_bars(self, symbol: str = "SPY", timeframe: str = "5Min",
                          limit: int = 100, interval: int = None,
                          days: int = 1) -> pd.DataFrame:
        """Return recent intraday OHLCV bars for *symbol* via Alpaca.

        *timeframe* follows Alpaca notation: '1Min', '5Min', '15Min', '1Hour'.
        *interval* (1, 5, or 15) is an alternative way to specify bar size in
        minutes; when provided it overrides *timeframe*.
        *days* sets the look-back window; overrides *limit* by computing
        start/end date params so Alpaca returns bars across that many calendar days.
        Returns empty DataFrame on failure.
        """
        if interval is not None:
            # Normalize string inputs like '5min' or '5Min' to an integer.
            if isinstance(interval, str):
                interval = int(''.join(filter(str.isdigit, interval)))
            _map = {1: "1Min", 5: "5Min", 15: "15Min", 60: "1Hour"}
            timeframe = _map.get(interval, f"{interval}Min")
        endpoint = f"/stocks/{symbol}/bars"
        now_ny = datetime.now(NY_TZ)
        start = (now_ny - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now_ny.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": limit,
            "adjustment": "split",
        }
        data = self._alpaca_get(endpoint, params)
        bars_list = data.get("bars", [])
        if not bars_list:
            return pd.DataFrame()
        df = pd.DataFrame(bars_list)
        df.rename(columns={"t": "time", "o": "open", "h": "high",
                            "l": "low", "c": "close", "v": "volume"}, inplace=True)
        df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(NY_TZ)
        df.set_index("time", inplace=True)
        return df[["open", "high", "low", "close", "volume"]].sort_index()

    def get_stock_price(self, symbol: str) -> float:
        """Return the latest last-trade price for *symbol* via Tradier. Returns 0.0 on failure."""
        quote = self.get_tradier_quote(symbol)
        if quote and quote.get("last", 0.0) > 0.0:
            return float(quote["last"])
        self.logger.warning("get_stock_price(%s): Tradier returned no valid price.", symbol)
        return 0.0

    def get_moving_average(self, symbol: str, period: int = 50) -> float:
        """Return the *period*-day simple moving average for *symbol*.

        Uses cached daily bars from Alpaca. Returns 0.0 on failure.
        """
        df = self.get_ticker_daily(symbol, lookback=period + 5)
        if df.empty or len(df) < period:
            self.logger.warning("get_moving_average(%s, %d): insufficient data (%d bars)",
                                symbol, period, len(df))
            return 0.0
        return float(df["close"].iloc[-period:].mean())

    def get_option_mid(self, symbol: str, expiration: str,
                       strike: float, option_type: str) -> float:
        """Return the mid-price for a specific option contract via Tradier.

        *option_type* should be 'call' or 'put'.
        Returns 0.0 on failure or when the option is not found.
        """
        chain = self.get_tradier_options_chain(symbol, expiration)
        if chain.empty:
            return 0.0
        mask = (
            (chain["strike"] == strike) &
            (chain["option_type"].str.lower() == option_type.lower())
        )
        row = chain[mask]
        if row.empty:
            self.logger.warning("get_option_mid: no match for %s %s %s %s",
                                symbol, expiration, strike, option_type)
            return 0.0
        bid = float(row["bid"].iloc[0])
        ask = float(row["ask"].iloc[0])
        if bid <= 0 and ask <= 0:
            return 0.0
        return round((bid + ask) / 2.0, 4)

    def get_option_bid(self, symbol: str, expiration: str,
                       strike: float, option_type: str) -> float:
        """Return the bid price for a specific option contract. Returns 0.0 on failure."""
        chain = self.get_tradier_options_chain(symbol, expiration)
        if chain.empty:
            return 0.0
        mask = (
            (chain["strike"] == strike) &
            (chain["option_type"].str.lower() == option_type.lower())
        )
        row = chain[mask]
        if row.empty:
            return 0.0
        return float(row["bid"].iloc[0])

    def get_option_ask(self, symbol: str, expiration: str,
                       strike: float, option_type: str) -> float:
        """Return the ask price for a specific option contract. Returns 0.0 on failure."""
        chain = self.get_tradier_options_chain(symbol, expiration)
        if chain.empty:
            return 0.0
        mask = (
            (chain["strike"] == strike) &
            (chain["option_type"].str.lower() == option_type.lower())
        )
        row = chain[mask]
        if row.empty:
            return 0.0
        return float(row["ask"].iloc[0])

    def get_options_chain(self, symbol: str, expiration: str) -> pd.DataFrame:
        """Alias for get_tradier_options_chain for backward compatibility."""
        return self.get_tradier_options_chain(symbol, expiration)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_vwap(self, bars: pd.DataFrame) -> float:
        """Calculate VWAP from an OHLCV DataFrame.

        VWAP = Σ(typical_price × volume) / Σ(volume)
        typical_price = (high + low + close) / 3

        Returns 0.0 if *bars* is empty or required columns are missing.
        """
        if bars is None or bars.empty:
            return 0.0
        try:
            tp = (bars["high"] + bars["low"] + bars["close"]) / 3.0
            total_volume = bars["volume"].sum()
            if total_volume == 0:
                return 0.0
            vwap = float((tp * bars["volume"]).sum() / total_volume)
            return round(vwap, 4)
        except (KeyError, ZeroDivisionError, TypeError) as exc:
            self.logger.error("_compute_vwap error: %s", exc)
            return 0.0

    def get_vix_term_structure(self) -> float:
        """Return VIX3M/VIX contango ratio via Tradier.

        > 1.05 = contango (safe to trade, 84.2% WR historically)
        < 1.05 = backwardation (pause all trading)

        Falls back to 0.0 on any error (conservative — triggers backwardation filter).
        """
        cached_value, fetched_at = getattr(self, '_vts_cache', (None, 0.0))
        if cached_value is not None and (time.monotonic() - fetched_at) < self._vix_cache_ttl:
            return cached_value

        ratio = None
        try:
            data = self._tradier_get("/markets/quotes", params={"symbols": "VIX,VIX3M"})
            quotes = data["quotes"]["quote"]
            if isinstance(quotes, list):
                q = {item["symbol"]: float(item["last"]) for item in quotes}
            else:
                q = {quotes["symbol"]: float(quotes["last"])}

            vix = q.get("VIX")
            vix3m = q.get("VIX3M")

            if vix and vix3m and vix > 0:
                ratio = vix3m / vix
            else:
                self.logger.warning("get_vix_term_structure: missing VIX or VIX3M, got: %s", q)
                ratio = 0.0

        except Exception as exc:
            self.logger.warning("get_vix_term_structure failed: %s. Defaulting to 0.0.", exc)
            ratio = 0.0

        if ratio is None or ratio < 0.5 or ratio > 2.0:
            self.logger.warning("get_vix_term_structure: suspicious ratio %.4f — defaulting to 0.0.", ratio or -1)
            ratio = 0.0

        self._vts_cache = (ratio, time.monotonic())
        return ratio

    def get_thetadata_options_chain(self, symbol: str, expiration: str) -> pd.DataFrame:
        """Fetch options chain from ThetaData v3 REST API.
        expiration: YYYY-MM-DD format (converted to YYYYMMDD for ThetaData).
        Returns DataFrame with same columns as get_tradier_options_chain.
        """
        try:
            exp_td = expiration.replace('-', '')
            url = f"http://localhost:25503/v3/option/snapshot/quote?symbol={symbol}&expiration={exp_td}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            from io import StringIO
            df_raw = pd.read_csv(StringIO(resp.text), skiprows=1, header=None, names=[
                'timestamp', 'symbol', 'expiration', 'strike', 'right',
                'bid_size', 'bid_exchange', 'bid', 'bid_condition',
                'ask_size', 'ask_exchange', 'ask', 'ask_condition'
            ])
            if df_raw.empty:
                return pd.DataFrame()
            df_raw['strike'] = pd.to_numeric(df_raw['strike'], errors='coerce')
            df_raw['bid']    = pd.to_numeric(df_raw['bid'], errors='coerce').fillna(0.0)
            df_raw['ask']    = pd.to_numeric(df_raw['ask'], errors='coerce').fillna(0.0)
            df_raw['mid']    = (df_raw['bid'] + df_raw['ask']) / 2
            df_raw['option_type'] = df_raw['right'].str.strip('"').str.upper().map({'CALL': 'call', 'PUT': 'put'}).fillna('')
            df_raw['delta']  = 0.0
            df_raw['gamma']  = 0.0
            df_raw['theta']  = 0.0
            df_raw['volume'] = 0
            df_raw['open_interest'] = 0
            return df_raw[['strike', 'option_type', 'bid', 'ask', 'mid',
                           'delta', 'gamma', 'theta', 'volume', 'open_interest']]
        except Exception as exc:
            self.logger.warning("get_thetadata_options_chain failed: %s", exc)
            return pd.DataFrame()
