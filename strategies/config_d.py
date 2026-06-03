"""
config_d.py — Config D 0DTE Strategy (paper trading only, NO real orders).

Entry days  : Monday and Friday only
VIX         : < 20
Time window : 9:45 AM – 12:00 PM ET
Signal      : SPY > VWAP + $0.50 (bullish morning bias)
Confirms    : higher high vs last 5 bars, volume >= 1.5× avg-5, RSI(14) >= 55
VWAP gap    : price – VWAP in [$0.10, $2.00]
Instrument  : SPY ATM call, 0DTE
Contracts   : 2
Target      : entry × 1.04
Stop        : entry × 0.65
Smart exit  : after 10 min, if gain < 1% AND volume declining → exit
"""

import logging
import math
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pytz

try:
    import ta
    HAS_TA = True
except ImportError:
    HAS_TA = False

NY_TZ = pytz.timezone('America/New_York')

# Weekday indices: 0=Mon, 4=Fri
_ENTRY_DAYS = {0, 4}


def _wilder_rsi(closes: List[float], period: int = 14) -> float:
    """Compute Wilder's RSI from a list of close prices.

    Returns NaN when not enough data is available.
    """
    if len(closes) < period + 1:
        return float('nan')

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed with simple average of first `period` values.
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_vwap(bars: List[dict]) -> float:
    """Compute VWAP from a list of OHLCV bar dicts with keys
    'high', 'low', 'close', 'volume'.
    """
    cum_tp_vol = 0.0
    cum_vol = 0.0
    for bar in bars:
        tp = (bar['high'] + bar['low'] + bar['close']) / 3.0
        v = bar['volume']
        cum_tp_vol += tp * v
        cum_vol += v
    if cum_vol == 0.0:
        return float('nan')
    return cum_tp_vol / cum_vol


class ConfigD:
    """Config D 0DTE call strategy — paper trading only."""

    NAME = 'config_d'

    # ------------------------------------------------------------------ init
    def __init__(self, db, feeds):
        self.db = db
        self.feeds = feeds
        self.logger = logging.getLogger('strategy_config_d')

        self._open_positions: List[dict] = []
        self._daily_pnl: float = 0.0
        self._daily_loss_limit: float = 500.0
        self._paused: bool = False

    # ---------------------------------------------------------- public API
    def check_signals(self, market_state: dict) -> Optional[dict]:
        """Evaluate entry conditions; return a signal dict or None.

        Does NOT open a position – the orchestrator opens it and then calls
        open_position() to register it here.
        """
        if self._paused:
            return None
        if self.daily_loss_exceeded():
            return None
        if not market_state.get('is_market_open', False):
            return None

        # Already have an open position – one at a time.
        if self._open_positions:
            return None

        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)

        # Entry day filter.
        if ny_dt.weekday() not in _ENTRY_DAYS:
            return None

        # Time window: 9:45 – 12:00.
        t = ny_dt.time()
        from datetime import time as dtime
        if not (dtime(9, 45) <= t <= dtime(12, 0)):
            return None

        # VIX filter.
        vix = market_state.get('vix', 999.0)
        if vix >= 20.0:
            self.logger.debug('ConfigD: VIX %.2f >= 20, skip', vix)
            return None

        spy_price = market_state['spy_price']

        # Fetch today's 5-min bars (today only, from market open).
        try:
            bars = self.feeds.get_intraday_bars('SPY', interval='5min', days=1)
        except Exception as exc:
            self.logger.warning('ConfigD: could not fetch bars: %s', exc)
            return None

        if not bars or len(bars) < 6:
            self.logger.debug('ConfigD: insufficient bars (%d)', len(bars))
            return None

        # VWAP from today's bars.
        vwap = _compute_vwap(bars)
        if math.isnan(vwap):
            return None

        gap = spy_price - vwap

        # Price must be above VWAP + $0.50.
        if gap < 0.50:
            self.logger.debug('ConfigD: gap %.3f < 0.50, skip', gap)
            return None

        # VWAP gap must be between $0.10 and $2.00.
        if not (0.10 <= gap <= 2.00):
            self.logger.debug('ConfigD: gap %.3f out of [0.10, 2.00], skip', gap)
            return None

        # Higher high vs last 5 bars.
        recent_highs = [b['high'] for b in bars[-5:]]
        if spy_price <= max(recent_highs[:-1]) if len(recent_highs) > 1 else recent_highs[0]:
            self.logger.debug('ConfigD: no higher high, skip')
            return None

        # Volume: current bar volume >= 1.5× avg of last 5 bars.
        recent_vols = [b['volume'] for b in bars[-5:]]
        avg_vol = np.mean(recent_vols[:-1]) if len(recent_vols) > 1 else recent_vols[0]
        cur_vol = bars[-1]['volume']
        if avg_vol > 0 and cur_vol < 1.5 * avg_vol:
            self.logger.debug(
                'ConfigD: volume %.0f < 1.5× avg %.0f, skip', cur_vol, avg_vol
            )
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
            self.logger.debug('ConfigD: RSI %.2f < 55, skip', rsi_val)
            return None

        # Get ATM 0DTE call from Tradier.
        today_str = ny_dt.date().isoformat()
        atm_strike = round(spy_price)  # nearest integer strike

        try:
            option_price = self.feeds.get_option_mid(
                symbol='SPY',
                expiration=today_str,
                strike=atm_strike,
                option_type='call',
            )
        except Exception as exc:
            self.logger.warning('ConfigD: option price fetch failed: %s', exc)
            return None

        if option_price is None or option_price <= 0:
            self.logger.warning('ConfigD: invalid option mid price')
            return None

        entry_price = round(option_price * 1.02, 2)  # realistic fill
        target_price = round(entry_price * 1.04, 2)
        stop_price = round(entry_price * 0.65, 2)

        signal = {
            'strategy': self.NAME,
            'symbol': 'SPY',
            'direction': 'long_call',
            'strike': float(atm_strike),
            'expiration': today_str,
            'contracts': 2,
            'entry_price': entry_price,
            'stop_price': stop_price,
            'target_price': target_price,
            'conditions': {
                'vix': vix,
                'vwap': round(vwap, 3),
                'gap': round(gap, 3),
                'rsi': round(float(rsi_val), 2),
                'cur_vol': int(cur_vol),
                'avg_vol': round(float(avg_vol), 1),
                'market_phase': market_state.get('market_phase', ''),
            },
        }
        self.logger.info(
            'ConfigD SIGNAL: strike=%s exp=%s entry=%.2f target=%.2f stop=%.2f '
            'rsi=%.1f vwap=%.2f gap=%.3f',
            atm_strike, today_str, entry_price, target_price, stop_price,
            rsi_val, vwap, gap,
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
            'ConfigD: position opened entry=%.2f strike=%s exp=%s',
            pos['entry_price'], pos.get('strike'), pos.get('expiration'),
        )

    def update_positions(self, market_state: dict) -> List[dict]:
        """Check open positions for exits. Returns list of closed trade dicts."""
        closed: List[dict] = []
        if not self._open_positions:
            return closed

        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        spy_price = market_state['spy_price']

        remaining: List[dict] = []
        for pos in self._open_positions:
            result = self._evaluate_exit(pos, market_state, ny_dt, spy_price)
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
        spy_price: float,
    ) -> Optional[dict]:
        """Return a closed-trade dict if an exit condition is triggered."""
        from datetime import time as dtime

        entry_price = pos['entry_price']
        target_price = pos['target_price']
        stop_price = pos['stop_price']
        entry_ts: datetime = pos['entry_ts']
        expiration = pos.get('expiration', '')
        strike = pos.get('strike', 0.0)

        # Fetch current option mid price.
        try:
            current_mid = self.feeds.get_option_mid(
                symbol='SPY',
                expiration=expiration,
                strike=strike,
                option_type='call',
            )
        except Exception as exc:
            self.logger.warning('ConfigD update: price fetch error: %s', exc)
            current_mid = None

        if current_mid is None:
            # Cannot evaluate – keep position.
            return None

        exit_price = round(current_mid * 0.98, 2)  # realistic exit fill

        exit_reason: Optional[str] = None
        contracts = pos.get('contracts', 2)

        # EOD: force exit at 3:55 PM (0DTE, must close before expiry).
        if ny_dt.time() >= dtime(15, 55):
            exit_reason = 'eod'

        # Target hit.
        elif current_mid >= target_price:
            exit_reason = 'target'

        # Stop hit.
        elif current_mid <= stop_price:
            exit_reason = 'stop'

        else:
            # Smart exit: 10 minutes after entry, check gain and volume.
            elapsed_minutes = (ny_dt - entry_ts).total_seconds() / 60.0
            if elapsed_minutes >= 10.0:
                gain_pct = (current_mid - entry_price) / entry_price
                if gain_pct < 0.01:
                    # Gain < 1% – check if volume is declining.
                    try:
                        bars = self.feeds.get_intraday_bars(
                            'SPY', interval='5min', days=1
                        )
                        if bars and len(bars) >= 3:
                            recent_vols = [b['volume'] for b in bars[-3:]]
                            volume_declining = recent_vols[-1] < recent_vols[-2] < recent_vols[-3]
                        else:
                            volume_declining = False
                    except Exception:
                        volume_declining = False

                    if volume_declining:
                        exit_reason = 'time_stop'
                        self.logger.info(
                            'ConfigD smart exit: gain=%.2f%% volume declining',
                            gain_pct * 100
                        )

        if exit_reason is None:
            return None

        # PnL: contracts × 100 shares per contract × (exit - entry).
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
            'spy_price': spy_price,
            'market_regime': market_state.get('market_phase', ''),
            'conditions': pos.get('conditions', {}),
        }
        self.logger.info(
            'ConfigD EXIT: reason=%s entry=%.2f exit=%.2f pnl=%.2f',
            exit_reason, entry_price, exit_price, pnl,
        )
        return closed_trade

    def _log_trade_to_db(self, trade: dict) -> None:
        """Persist a closed trade to the database."""
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
            self.logger.error('ConfigD: DB insert failed: %s', exc)

    # ------------------------------------------------ lifecycle helpers
    def daily_loss_exceeded(self) -> bool:
        return self._daily_pnl <= -self._daily_loss_limit

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self.logger.info('ConfigD: daily state reset')

    def force_close_all(self, market_state: dict, reason: str = 'force_exit') -> list:
        """Close all open positions immediately at best-available price."""
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
            exit_price = mid * 0.98 if mid > 0 else pos['entry_price'] * 0.50
            pnl = round((exit_price - pos['entry_price']) * 100 * pos.get('contracts', 2), 2)
            self._daily_pnl += pnl
            trade = {
                'strategy': self.NAME, 'symbol': 'SPY', 'direction': 'long_call',
                'entry_price': pos['entry_price'], 'exit_price': exit_price,
                'pnl': pnl, 'exit_reason': reason,
                'entry_dt': pos.get('entry_ts', ts).isoformat() if hasattr(pos.get('entry_ts', ts), 'isoformat') else str(pos.get('entry_ts', ts)),
                'exit_dt': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                'vix': market_state.get('vix', 0.0),
                'spy_price': spy_price, 'market_regime': market_state.get('market_phase', ''),
                'conditions': pos.get('conditions', {}),
            }
            self._log_trade_to_db(trade)
            closed.append(trade)
            self.logger.info('ConfigD force_close: pnl=%.2f reason=%s', pnl, reason)
        self._open_positions.clear()
        return closed

    def check_earnings_calendar(self) -> None:
        """No-op for Config D — not earnings-dependent."""
