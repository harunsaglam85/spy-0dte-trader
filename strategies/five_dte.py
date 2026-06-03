"""
five_dte.py — 5-DTE Momentum Call strategy (paper trading only, NO real orders).

Entry days   : Any weekday Mon–Fri
VIX          : < 22
Time window  : 9:45 AM – 11:00 AM ET (morning momentum window)
Signal       : SPY > VWAP + $0.50
Confirms     : higher high vs last 5 bars, volume >= 1.5× avg-5, RSI(14) >= 55
Instrument   : SPY ATM call expiring ~5 trading days out
Entry fill   : Tradier mid × 1.02
Exit fill    : Tradier mid × 0.98
Target       : entry × 1.15 (15% gain)
Stop         : entry × 0.80 (20% loss)
Time stop    : exit after 2 trading days if neither target nor stop hit
Contracts    : 1  —  only ONE open position at a time
"""

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pytz

try:
    import ta
    HAS_TA = True
except ImportError:
    HAS_TA = False

NY_TZ = pytz.timezone('America/New_York')

# US market holidays that should not count as trading days.
# A minimal set; expand or replace with a proper calendar if needed.
_MARKET_HOLIDAYS_2026 = {
    '2026-01-01',  # New Year's Day
    '2026-01-19',  # MLK Day
    '2026-02-16',  # Presidents' Day
    '2026-04-03',  # Good Friday
    '2026-05-25',  # Memorial Day
    '2026-07-03',  # Independence Day (observed)
    '2026-09-07',  # Labor Day
    '2026-11-26',  # Thanksgiving
    '2026-12-25',  # Christmas
}


def _wilder_rsi(closes: List[float], period: int = 14) -> float:
    """Wilder's RSI. Returns NaN when insufficient data."""
    if len(closes) < period + 1:
        return float('nan')
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _compute_vwap(bars: List[dict]) -> float:
    """VWAP from OHLCV bar dicts."""
    cum_tp_vol = 0.0
    cum_vol = 0.0
    for bar in bars:
        tp = (bar['high'] + bar['low'] + bar['close']) / 3.0
        cum_tp_vol += tp * bar['volume']
        cum_vol += bar['volume']
    return cum_tp_vol / cum_vol if cum_vol > 0 else float('nan')


def _is_trading_day(d) -> bool:
    """Return True if d is a weekday and not in the holiday set."""
    from datetime import date as date_cls
    if isinstance(d, datetime):
        d = d.date()
    return d.weekday() < 5 and d.isoformat() not in _MARKET_HOLIDAYS_2026


def _count_trading_days_elapsed(from_dt: datetime, to_dt: datetime) -> int:
    """Count trading days between two timezone-aware datetimes (inclusive of boundary)."""
    from datetime import timedelta as td, date as date_cls
    from_date = from_dt.astimezone(NY_TZ).date()
    to_date = to_dt.astimezone(NY_TZ).date()

    count = 0
    current = from_date
    while current <= to_date:
        if _is_trading_day(current):
            count += 1
        current += td(days=1)
    # Subtract 1: we want "elapsed" not "inclusive count".
    return max(count - 1, 0)


class FiveDTE:
    """5-DTE momentum call strategy — paper trading only."""

    NAME = 'five_dte'

    # ------------------------------------------------------------------ init
    def __init__(self, db, feeds):
        self.db = db
        self.feeds = feeds
        self.logger = logging.getLogger('strategy_five_dte')

        self._open_positions: List[dict] = []
        self._daily_pnl: float = 0.0
        self._daily_loss_limit: float = 500.0
        self._paused: bool = False

    # ---------------------------------------------------------- public API
    def check_signals(self, market_state: dict) -> Optional[dict]:
        """Evaluate entry conditions; return a signal dict or None."""
        if self._paused:
            return None
        if self.daily_loss_exceeded():
            return None
        if not market_state.get('is_market_open', False):
            return None

        # One position at a time.
        if self._open_positions:
            return None

        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)

        # Any weekday.
        if ny_dt.weekday() >= 5:
            return None

        # Time window: 9:45 – 11:00.
        from datetime import time as dtime
        t = ny_dt.time()
        if not (dtime(9, 45) <= t <= dtime(11, 0)):
            return None

        # VIX < 22.
        vix = market_state.get('vix', 999.0)
        if vix >= 22.0:
            self.logger.debug('FiveDTE: VIX %.2f >= 22, skip', vix)
            return None

        spy_price = market_state['spy_price']

        # 5-min bars for VWAP, RSI, higher-high, volume check.
        try:
            bars = self.feeds.get_intraday_bars('SPY', interval='5min', days=1)
        except Exception as exc:
            self.logger.warning('FiveDTE: bar fetch failed: %s', exc)
            return None

        if not bars or len(bars) < 6:
            self.logger.debug('FiveDTE: insufficient bars (%d)', len(bars) if bars else 0)
            return None

        vwap = _compute_vwap(bars)
        if math.isnan(vwap):
            return None

        # Signal: SPY > VWAP + $0.50.
        if spy_price <= vwap + 0.50:
            self.logger.debug('FiveDTE: price %.2f <= VWAP+0.50 %.2f, skip',
                              spy_price, vwap + 0.50)
            return None

        # Higher high vs last 5 bars.
        recent_highs = [b['high'] for b in bars[-5:]]
        prev_high = max(recent_highs[:-1]) if len(recent_highs) > 1 else recent_highs[0]
        if spy_price <= prev_high:
            self.logger.debug('FiveDTE: no higher high, skip')
            return None

        # Volume >= 1.5× avg-5.
        recent_vols = [b['volume'] for b in bars[-5:]]
        avg_vol = np.mean(recent_vols[:-1]) if len(recent_vols) > 1 else recent_vols[0]
        cur_vol = bars[-1]['volume']
        if avg_vol > 0 and cur_vol < 1.5 * avg_vol:
            self.logger.debug('FiveDTE: volume %.0f < 1.5× avg %.0f, skip',
                              cur_vol, avg_vol)
            return None

        # RSI(14) >= 55.
        closes = [b['close'] for b in bars]
        if HAS_TA and len(closes) >= 15:
            import pandas as pd
            rsi_val = ta.momentum.RSIIndicator(
                pd.Series(closes), window=14
            ).rsi().iloc[-1]
        else:
            rsi_val = _wilder_rsi(closes, period=14)

        if math.isnan(rsi_val) or rsi_val < 55.0:
            self.logger.debug('FiveDTE: RSI %.2f < 55, skip', rsi_val)
            return None

        # Get expiration ~5 trading days out.
        try:
            expiration = self.feeds.get_next_expiry(dte_target=5)
        except Exception as exc:
            self.logger.warning('FiveDTE: expiry fetch failed: %s', exc)
            return None

        atm_strike = round(spy_price)

        try:
            option_mid = self.feeds.get_option_mid(
                symbol='SPY',
                expiration=expiration,
                strike=atm_strike,
                option_type='call',
            )
        except Exception as exc:
            self.logger.warning('FiveDTE: option price fetch failed: %s', exc)
            return None

        if option_mid is None or option_mid <= 0:
            return None

        entry_price = round(option_mid * 1.02, 2)
        target_price = round(entry_price * 1.15, 2)
        stop_price = round(entry_price * 0.80, 2)

        signal = {
            'strategy': self.NAME,
            'symbol': 'SPY',
            'direction': 'long_call',
            'strike': float(atm_strike),
            'expiration': expiration,
            'contracts': 1,
            'entry_price': entry_price,
            'stop_price': stop_price,
            'target_price': target_price,
            'conditions': {
                'vix': vix,
                'vwap': round(vwap, 3),
                'rsi': round(float(rsi_val), 2),
                'cur_vol': int(cur_vol),
                'avg_vol': round(float(avg_vol), 1),
                'market_phase': market_state.get('market_phase', ''),
            },
        }
        self.logger.info(
            'FiveDTE SIGNAL: strike=%s exp=%s entry=%.2f target=%.2f stop=%.2f '
            'rsi=%.1f vwap=%.2f',
            atm_strike, expiration, entry_price, target_price, stop_price,
            rsi_val, vwap,
        )
        return signal

    def open_position(self, signal: dict, market_state: dict) -> None:
        """Register a newly opened paper position (called by orchestrator)."""
        pos = dict(signal)
        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        pos['entry_dt'] = ny_dt.isoformat()
        pos['entry_ts'] = ny_dt
        pos['vix'] = market_state.get('vix', 0.0)
        pos['spy_price_at_entry'] = market_state['spy_price']
        self._open_positions.append(pos)
        self.logger.info(
            'FiveDTE: position opened entry=%.2f strike=%s exp=%s',
            pos['entry_price'], pos.get('strike'), pos.get('expiration'),
        )

    def update_positions(self, market_state: dict) -> List[dict]:
        """Check open positions for exits. Returns list of closed trade dicts."""
        closed: List[dict] = []
        if not self._open_positions:
            return closed

        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)

        remaining: List[dict] = []
        for pos in self._open_positions:
            result = self._evaluate_exit(pos, market_state, ny_dt)
            if result is not None:
                closed.append(result)
                self._daily_pnl += result['pnl']
                self._log_trade_to_db(result)
            else:
                remaining.append(pos)

        self._open_positions = remaining
        return closed

    # ---------------------------------------------------- internal helpers
    def _evaluate_exit(
        self,
        pos: dict,
        market_state: dict,
        ny_dt: datetime,
    ) -> Optional[dict]:
        """Evaluate exit conditions for a single position."""
        entry_price = pos['entry_price']
        target_price = pos['target_price']
        stop_price = pos['stop_price']
        entry_ts: datetime = pos['entry_ts']
        expiration = pos.get('expiration', '')
        strike = pos.get('strike', 0.0)
        contracts = pos.get('contracts', 1)

        # Fetch current mid price.
        try:
            current_mid = self.feeds.get_option_mid(
                symbol='SPY',
                expiration=expiration,
                strike=strike,
                option_type='call',
            )
        except Exception as exc:
            self.logger.warning('FiveDTE update: price fetch error: %s', exc)
            current_mid = None

        if current_mid is None:
            return None

        exit_price = round(current_mid * 0.98, 2)

        # Count trading days elapsed since entry.
        trading_days_elapsed = _count_trading_days_elapsed(entry_ts, ny_dt)

        exit_reason: Optional[str] = None

        # Time stop: 2 trading days elapsed.
        if trading_days_elapsed >= 2:
            exit_reason = 'time_stop'

        # Target hit.
        elif current_mid >= target_price:
            exit_reason = 'target'

        # Stop hit.
        elif current_mid <= stop_price:
            exit_reason = 'stop'

        if exit_reason is None:
            return None

        pnl = round(contracts * 100 * (exit_price - entry_price), 2)

        closed_trade = {
            'strategy': self.NAME,
            'symbol': 'SPY',
            'direction': pos['direction'],
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl': pnl,
            'exit_reason': exit_reason,
            'entry_dt': pos['entry_dt'],
            'exit_dt': ny_dt.isoformat(),
            'vix': pos.get('vix', market_state.get('vix', 0.0)),
            'spy_price': market_state['spy_price'],
            'market_regime': market_state.get('market_phase', ''),
            'conditions': pos.get('conditions', {}),
        }
        self.logger.info(
            'FiveDTE EXIT: reason=%s entry=%.2f exit=%.2f pnl=%.2f days=%d',
            exit_reason, entry_price, exit_price, pnl, trading_days_elapsed,
        )
        return closed_trade

    def _log_trade_to_db(self, trade: dict) -> None:
        try:
            self.db.insert_trade(
                strategy=trade['strategy'],
                symbol=trade['symbol'],
                direction=trade['direction'],
                entry_price=trade['entry_price'],
                exit_price=trade['exit_price'],
                pnl=trade['pnl'],
                exit_reason=trade['exit_reason'],
                vix=trade.get('vix', 0.0),
                spy_price=trade.get('spy_price', 0.0),
                market_regime=trade.get('market_regime', ''),
                conditions_json=trade.get('conditions', {}),
            )
        except Exception as exc:
            self.logger.error('FiveDTE: DB insert failed: %s', exc)

    # ------------------------------------------------ lifecycle helpers
    def daily_loss_exceeded(self) -> bool:
        return self._daily_pnl <= -self._daily_loss_limit

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self.logger.info('FiveDTE: daily state reset')

    def force_close_all(self, market_state: dict, reason: str = 'force_exit') -> list:
        """Close all open option positions at best-available price."""
        closed = []
        spy_price = market_state.get('spy_price', 0.0)
        ts = market_state.get('timestamp', datetime.now(NY_TZ))
        for pos in list(self._open_positions):
            mid = 0.0
            try:
                mid = self.feeds.get_option_mid(
                    symbol='SPY',
                    expiration=pos.get('expiration', ''),
                    strike=pos.get('strike', 0.0),
                    option_type='call',
                )
            except Exception:
                pass
            exit_price = mid * 0.98 if mid > 0 else pos['entry_price'] * 0.60
            pnl = round((exit_price - pos['entry_price']) * 100 * pos.get('contracts', 1), 2)
            self._daily_pnl += pnl
            trade = {
                'strategy': self.NAME, 'symbol': 'SPY', 'direction': 'long_call',
                'entry_price': pos['entry_price'], 'exit_price': exit_price,
                'pnl': pnl, 'exit_reason': reason,
                'entry_dt': str(pos.get('entry_ts', ts)),
                'exit_dt': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                'vix': market_state.get('vix', 0.0), 'spy_price': spy_price,
                'market_regime': market_state.get('market_phase', ''),
                'conditions': pos.get('conditions', {}),
            }
            self._log_trade_to_db(trade)
            closed.append(trade)
            self.logger.info('FiveDTE force_close: pnl=%.2f reason=%s', pnl, reason)
        self._open_positions.clear()
        return closed

    def check_earnings_calendar(self) -> None:
        """No-op for 5-DTE — not earnings-dependent."""
