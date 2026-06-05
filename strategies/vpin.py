"""
vpin.py — VPIN Order-Flow strategy (paper trading only, NO real orders).

VPIN algorithm (volume-clock bucket approach):
  1. Gather N = BUCKETS (50) equal-volume buckets from recent 5-min bars.
  2. For each bar, classify volume into buy/sell via:
       V_buy  = volume × (0.5 + 0.5 × tanh(return × 50))
       V_sell = volume - V_buy
  3. Fill buckets sequentially with a target volume V_target = total_volume / BUCKETS.
     Each bucket accumulates buy/sell volume until the target is reached (partial bars
     are split proportionally).
  4. Per-bucket VPIN: |V_buy_bucket - V_sell_bucket| / (V_buy_bucket + V_sell_bucket)
  5. Overall VPIN: mean of all 50 bucket values.

Signals:
  VPIN > 0.70 → SHORT SPY shares  (informed selling → mean reversion long play? No —
                                   high VPIN = high order-flow imbalance → directional)
  Actually interpretation: VPIN > 0.70 means strong informed flow → SHORT (contrarian
  here following the spec: sell into the imbalance).
  VPIN < 0.30 → LONG SPY shares

Position size : $1,000 notional
Stop          : 1% loss on position
Max hold      : 3 trading days → force exit
Only ONE open position (no double-stacking).
Minimum 50 bars of data required.
"""

import logging
import math
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pytz

NY_TZ = pytz.timezone('America/New_York')

# Market holidays (same minimal set as five_dte; expand with proper calendar as needed).
_MARKET_HOLIDAYS_2026 = {
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03',
    '2026-05-25', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
}


def _is_trading_day(d) -> bool:
    if isinstance(d, datetime):
        d = d.date()
    return d.weekday() < 5 and d.isoformat() not in _MARKET_HOLIDAYS_2026


def _count_trading_days_elapsed(from_dt: datetime, to_dt: datetime) -> int:
    """Trading days elapsed (exclusive of from, inclusive of to)."""
    from_date = from_dt.astimezone(NY_TZ).date()
    to_date = to_dt.astimezone(NY_TZ).date()
    count = 0
    current = from_date + timedelta(days=1)  # start day after entry
    while current <= to_date:
        if _is_trading_day(current):
            count += 1
        current += timedelta(days=1)
    return count


def compute_vpin(bars: List[dict], buckets: int = 50) -> Optional[float]:
    """Compute VPIN from a list of OHLCV bar dicts.

    Parameters
    ----------
    bars : list of dicts with keys 'open', 'close', 'volume'.
    buckets : number of equal-volume buckets (default 50).

    Returns
    -------
    float in [0, 1] or None if insufficient data.
    """
    if not bars or len(bars) < buckets:
        return None

    # ---- Step 1: compute bar-level buy/sell volumes ----
    bar_buy: List[float] = []
    bar_sell: List[float] = []
    bar_total: List[float] = []

    for bar in bars:
        vol = float(bar.get('volume', 0))
        if vol <= 0:
            bar_buy.append(0.0)
            bar_sell.append(0.0)
            bar_total.append(0.0)
            continue

        # Bar return: (close - open) / open
        o = float(bar.get('open', bar.get('close', 0)))
        c = float(bar.get('close', 0))
        if o > 0:
            ret = (c - o) / o
        else:
            ret = 0.0

        # Bulk-volume classification.
        # tanh is bounded [-1, 1]; multiply return by 50 to scale typical
        # intraday 5-min returns (~0.01) into a meaningful tanh range.
        alpha = 0.5 + 0.5 * math.tanh(ret * 50.0)
        v_buy = vol * alpha
        v_sell = vol - v_buy

        bar_buy.append(v_buy)
        bar_sell.append(v_sell)
        bar_total.append(vol)

    total_volume = sum(bar_total)
    if total_volume <= 0:
        return None

    # ---- Step 2: fill volume-clock buckets ----
    bucket_target = total_volume / buckets

    bucket_vpins: List[float] = []
    bk_buy = 0.0
    bk_sell = 0.0
    bk_total = 0.0
    filled = 0

    for i, vol in enumerate(bar_total):
        remaining_bar_buy = bar_buy[i]
        remaining_bar_sell = bar_sell[i]
        remaining_bar_vol = vol

        while remaining_bar_vol > 1e-9 and filled < buckets:
            space_in_bucket = bucket_target - bk_total
            take = min(remaining_bar_vol, space_in_bucket)

            # Proportional split.
            fraction = take / vol if vol > 1e-9 else 0.0
            bk_buy += remaining_bar_buy * fraction
            bk_sell += remaining_bar_sell * fraction
            bk_total += take

            remaining_bar_buy -= remaining_bar_buy * fraction
            remaining_bar_sell -= remaining_bar_sell * fraction
            remaining_bar_vol -= take

            if bk_total >= bucket_target - 1e-6:
                # Bucket full.
                bk_vol = bk_buy + bk_sell
                if bk_vol > 0:
                    bucket_vpins.append(abs(bk_buy - bk_sell) / bk_vol)
                else:
                    bucket_vpins.append(0.0)
                filled += 1
                bk_buy = 0.0
                bk_sell = 0.0
                bk_total = 0.0

        if filled >= buckets:
            break

    if len(bucket_vpins) < buckets:
        return None  # insufficient data to fill all buckets

    return float(np.mean(bucket_vpins[:buckets]))


class VPIN:
    """VPIN order-flow strategy — paper trading only."""

    NAME = 'vpin'
    BUCKETS = 50
    VPIN_HIGH = 0.70   # signal: SHORT
    VPIN_LOW = 0.30    # signal: LONG
    POSITION_SIZE = 1_000.0  # notional dollars
    MAX_HOLD_DAYS = 3
    STOP_PCT = 0.01    # 1% stop loss on position

    # ------------------------------------------------------------------ init
    def __init__(self, db, feeds):
        self.db = db
        self.feeds = feeds
        self.logger = logging.getLogger('strategy_vpin')

        self._open_positions: List[dict] = []
        self._daily_pnl: float = 0.0
        self._daily_loss_limit: float = 500.0
        self._paused: bool = False

    # ---------------------------------------------------------- public API
    def check_signals(self, market_state: dict) -> Optional[dict]:
        """Compute VPIN and return a signal dict, or None."""
        ts: datetime = market_state.get('timestamp', datetime.now(NY_TZ))
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        is_open = market_state.get('is_market_open', False)
        self.logger.info(
            'VPIN check_signals: time=%s is_open=%s paused=%s daily_pnl=%.2f open_positions=%d',
            ny_dt.strftime('%H:%M:%S'), is_open, self._paused, self._daily_pnl, len(self._open_positions),
        )
        if self._paused:
            self.logger.info('VPIN: SKIP — strategy paused')
            return None
        if self.daily_loss_exceeded():
            self.logger.info('VPIN: SKIP — daily loss limit reached (pnl=%.2f limit=%.2f)',
                             self._daily_pnl, self._daily_loss_limit)
            return None
        if not is_open:
            self.logger.info('VPIN: SKIP — market not open')
            return None

        # One position at a time.
        if self._open_positions:
            self.logger.info('VPIN: SKIP — position already open')
            return None

        # Only trade during regular market hours (after first 15 min).
        from datetime import time as dtime
        t = ny_dt.time()
        if not (dtime(9, 45) <= t <= dtime(15, 30)):
            self.logger.info('VPIN: SKIP — outside trading window 9:45-15:30 (now %s)',
                             ny_dt.strftime('%H:%M'))
            return None

        spy_price = market_state['spy_price']

        # Fetch sufficient 5-min bars (need at least BUCKETS bars).
        # Request 5 days of bars to ensure enough data.
        try:
            bars = self.feeds.get_intraday_bars('SPY', interval='5min', days=5)
        except Exception as exc:
            self.logger.warning('VPIN: bar fetch failed: %s', exc)
            return None

        if bars is None or bars.empty or len(bars) < self.BUCKETS:
            self.logger.debug(
                'VPIN: insufficient bars (%d < %d)', len(bars) if bars else 0, self.BUCKETS
            )
            return None

        # Use only the most recent bars (at least BUCKETS, more is fine for bucket-filling).
        analysis_bars = bars[-max(self.BUCKETS * 3, len(bars)):]

        vpin_value = compute_vpin(analysis_bars, buckets=self.BUCKETS)

        if vpin_value is None:
            self.logger.debug('VPIN: could not compute VPIN (insufficient bucket fill)')
            return None

        self.logger.debug('VPIN: current value = %.4f', vpin_value)

        # Determine direction.
        if vpin_value > self.VPIN_HIGH:
            direction = 'short_shares'
        elif vpin_value < self.VPIN_LOW:
            direction = 'long_shares'
        else:
            return None  # VPIN in neutral zone

        # Compute shares from notional position size.
        shares = int(self.POSITION_SIZE / spy_price) if spy_price > 0 else 0
        if shares <= 0:
            self.logger.warning('VPIN: computed 0 shares from notional, skip')
            return None

        entry_price = spy_price

        if direction == 'long_shares':
            stop_price = round(entry_price * (1.0 - self.STOP_PCT), 2)
            target_price = 0.0  # no fixed target; time-stop driven
        else:  # short_shares
            stop_price = round(entry_price * (1.0 + self.STOP_PCT), 2)
            target_price = 0.0

        signal = {
            'strategy': self.NAME,
            'symbol': 'SPY',
            'direction': direction,
            'strike': 0.0,       # not applicable (equity)
            'expiration': '',    # not applicable (equity)
            'contracts': shares, # re-using 'contracts' field for share count
            'entry_price': entry_price,
            'stop_price': stop_price,
            'target_price': target_price,
            'conditions': {
                'vpin': round(vpin_value, 4),
                'vix': market_state.get('vix', 0.0),
                'bars_used': len(analysis_bars),
                'notional': self.POSITION_SIZE,
                'shares': shares,
                'market_phase': market_state.get('market_phase', ''),
            },
        }
        self.logger.info(
            'VPIN SIGNAL: direction=%s vpin=%.4f entry=%.2f stop=%.2f shares=%d',
            direction, vpin_value, entry_price, stop_price, shares,
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
        self._open_positions.append(pos)
        self.logger.info(
            'VPIN: position opened direction=%s entry=%.2f stop=%.2f shares=%d',
            pos['direction'], pos['entry_price'], pos['stop_price'],
            pos['conditions']['shares'],
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
        """Evaluate stop / max-hold exit for a single equity position."""
        direction = pos['direction']
        entry_price = pos['entry_price']
        stop_price = pos['stop_price']
        entry_ts: datetime = pos['entry_ts']
        shares = pos['conditions']['shares']
        conds = pos.get('conditions', {})

        trading_days_elapsed = _count_trading_days_elapsed(entry_ts, ny_dt)

        exit_reason: Optional[str] = None
        exit_price = spy_price

        # Max hold: 3 trading days.
        if trading_days_elapsed >= self.MAX_HOLD_DAYS:
            exit_reason = 'time_stop'

        elif direction == 'long_shares':
            if spy_price <= stop_price:
                exit_reason = 'stop'

        elif direction == 'short_shares':
            if spy_price >= stop_price:
                exit_reason = 'stop'

        if exit_reason is None:
            return None

        # P&L:
        # Long:  (exit - entry) × shares
        # Short: (entry - exit) × shares
        if direction == 'long_shares':
            pnl = round((exit_price - entry_price) * shares, 2)
        else:
            pnl = round((entry_price - exit_price) * shares, 2)

        closed_trade = {
            'strategy': self.NAME,
            'symbol': 'SPY',
            'direction': direction,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl': pnl,
            'exit_reason': exit_reason,
            'entry_dt': pos['entry_dt'],
            'exit_dt': ny_dt.isoformat(),
            'vix': pos.get('vix', market_state.get('vix', 0.0)),
            'spy_price': spy_price,
            'market_regime': market_state.get('market_phase', ''),
            'conditions': conds,
        }
        self.logger.info(
            'VPIN EXIT: direction=%s reason=%s entry=%.2f exit=%.2f pnl=%.2f days=%d',
            direction, exit_reason, entry_price, exit_price, pnl, trading_days_elapsed,
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
            self.logger.error('VPIN: DB insert failed: %s', exc)

    # ------------------------------------------------ lifecycle helpers
    def daily_loss_exceeded(self) -> bool:
        return self._daily_pnl <= -self._daily_loss_limit

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self.logger.info('VPIN: daily state reset')

    def force_close_all(self, market_state: dict, reason: str = 'force_exit') -> list:
        """Close all open share positions at current SPY price."""
        closed = []
        spy_price = market_state.get('spy_price', 0.0)
        ts = market_state.get('timestamp', datetime.now(NY_TZ))
        for pos in list(self._open_positions):
            entry_price = pos.get('entry_price', spy_price)
            shares = pos.get('shares', 0)
            direction = pos.get('direction', 1)  # +1 long, -1 short
            exit_price = spy_price if spy_price > 0 else entry_price
            pnl = round(direction * shares * (exit_price - entry_price), 2)
            self._daily_pnl += pnl
            trade = {
                'strategy': self.NAME, 'symbol': 'SPY',
                'direction': 'long_shares' if direction > 0 else 'short_shares',
                'entry_price': entry_price, 'exit_price': exit_price,
                'pnl': pnl, 'exit_reason': reason,
                'entry_dt': str(pos.get('entry_ts', ts)),
                'exit_dt': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                'vix': market_state.get('vix', 0.0), 'spy_price': spy_price,
                'market_regime': market_state.get('market_phase', ''),
                'conditions': pos.get('conditions', {}),
            }
            self._log_trade_to_db(trade)
            closed.append(trade)
            self.logger.info('VPIN force_close: pnl=%.2f reason=%s', pnl, reason)
        self._open_positions.clear()
        return closed

    def check_earnings_calendar(self) -> None:
        """No-op for VPIN — not earnings-dependent."""
