"""
credit_spread.py — Bull Put Credit Spread strategy (paper trading only, NO real orders).

Entry days  : Monday, Wednesday, Friday only
VIX         : 15 – 22
Entry time  : 10:00 AM – 11:00 AM ET
Setup       : SPY bull put spread
              • Sell put ~0.20 delta
              • Buy put $2.00 lower strike
              • Credit = bid(short) - ask(long)  (min $0.10)
Target      : close when debit-to-close <= 30% of credit (i.e. kept 70%)
Stop        : close when debit-to-close >= 200% of credit received
Force exit  : 3:00 PM ET regardless
Expiration  : same-week Friday via feeds.get_next_expiry()
Contracts   : 1
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import pytz

NY_TZ = pytz.timezone('America/New_York')
_ENTRY_DAYS = {0, 2, 4}  # Mon, Wed, Fri


class CreditSpread:
    """Bull put credit spread — paper trading only."""

    NAME = 'credit_spread'

    # ------------------------------------------------------------------ init
    def __init__(self, db, feeds):
        self.db = db
        self.feeds = feeds
        self.logger = logging.getLogger('strategy_credit_spread')

        self._open_positions: List[dict] = []
        self._daily_pnl: float = 0.0
        self._daily_loss_limit: float = 500.0
        self._paused: bool = False

    # ---------------------------------------------------------- public API
    def check_signals(self, market_state: dict) -> Optional[dict]:
        """Evaluate entry conditions; return a signal dict or None."""
        ts: datetime = market_state.get('timestamp', datetime.now(NY_TZ))
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        vix = market_state.get('vix', 0.0)
        is_open = market_state.get('is_market_open', False)
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        self.logger.info(
            'CreditSpread check_signals: time=%s day=%s is_open=%s paused=%s vix=%.2f open_positions=%d',
            ny_dt.strftime('%H:%M:%S'), day_names[ny_dt.weekday()], is_open,
            self._paused, vix, len(self._open_positions),
        )
        if self._paused:
            self.logger.info('CreditSpread: SKIP — strategy paused')
            return None
        if self.daily_loss_exceeded():
            self.logger.info('CreditSpread: SKIP — daily loss limit reached (pnl=%.2f limit=%.2f)',
                             self._daily_pnl, self._daily_loss_limit)
            return None
        if not is_open:
            self.logger.info('CreditSpread: SKIP — market not open')
            return None

        # One spread at a time.
        if self._open_positions:
            self.logger.info('CreditSpread: SKIP — position already open')
            return None

        # Entry day filter.
        if ny_dt.weekday() not in _ENTRY_DAYS:
            self.logger.info('CreditSpread: SKIP — not entry day (need Mon/Wed/Fri, got %s)',
                             day_names[ny_dt.weekday()])
            return None

        # Time window: 10:00 – 11:00.
        from datetime import time as dtime
        t = ny_dt.time()
        if not (dtime(10, 0) <= t <= dtime(11, 0)):
            self.logger.info('CreditSpread: SKIP — outside entry window 10:00-11:00 (now %s)',
                             ny_dt.strftime('%H:%M'))
            return None

        # VIX filter: 15 – 22.
        if not (15.0 <= vix <= 22.0):
            self.logger.info('CreditSpread: SKIP — VIX %.2f outside [15, 22]', vix)
            return None

        # Sentiment filter: skip if strong bearish news.
        sentiment = market_state.get('sentiment_score', 0.0)
        if sentiment < -0.5:
            self.logger.info('CreditSpread: SKIP — bearish sentiment %.3f < -0.5', sentiment)
            return None

        spy_price = market_state['spy_price']

        # Get this week's expiration.
        try:
            expiration = self.feeds.get_next_expiry(dte_target=None, weekly=True)
        except Exception as exc:
            self.logger.warning('CreditSpread: expiry fetch failed: %s', exc)
            return None

        # Fetch options chain with Greeks to find ~0.20-delta put.
        try:
            chain = self.feeds.get_options_chain(
                symbol='SPY',
                expiration=expiration,
                option_type='put',
            )
        except Exception as exc:
            self.logger.warning('CreditSpread: chain fetch failed: %s', exc)
            return None

        if not chain:
            self.logger.warning('CreditSpread: empty options chain')
            return None

        # Filter puts with abs(delta) in [0.15, 0.25].
        candidates = [
            opt for opt in chain
            if opt.get('delta') is not None
            and 0.15 <= abs(opt['delta']) <= 0.25
        ]
        if not candidates:
            self.logger.debug('CreditSpread: no 0.20-delta puts found')
            return None

        # Pick the one whose abs(delta) is closest to 0.20.
        short_opt = min(candidates, key=lambda o: abs(abs(o['delta']) - 0.20))
        short_strike = float(short_opt['strike'])
        long_strike = short_strike - 2.00  # buy put $2 lower

        # Fetch bid/ask for both legs.
        try:
            short_bid = self.feeds.get_option_bid(
                symbol='SPY', expiration=expiration,
                strike=short_strike, option_type='put',
            )
            long_ask = self.feeds.get_option_ask(
                symbol='SPY', expiration=expiration,
                strike=long_strike, option_type='put',
            )
        except Exception as exc:
            self.logger.warning('CreditSpread: leg price fetch failed: %s', exc)
            return None

        if short_bid is None or long_ask is None:
            self.logger.debug('CreditSpread: could not get leg prices')
            return None

        credit = round(short_bid - long_ask, 2)

        if credit < 0.10:
            self.logger.debug(
                'CreditSpread: credit %.2f < min $0.10, skip', credit
            )
            return None

        # Targets expressed as debit-to-close thresholds.
        target_dtc = round(credit * 0.30, 2)   # profit target: keep 70%
        stop_dtc = round(credit * 2.00, 2)      # stop loss: 2× credit

        signal = {
            'strategy': self.NAME,
            'symbol': 'SPY',
            'direction': 'put_spread',
            'strike': short_strike,          # short leg strike for generic interface
            'expiration': expiration,
            'contracts': 1,
            'entry_price': credit,           # net credit received per share
            'stop_price': stop_dtc,          # used as "max debit-to-close" at stop
            'target_price': target_dtc,      # used as "min debit-to-close" at target
            'conditions': {
                'vix': vix,
                'spy_price': spy_price,
                'short_strike': short_strike,
                'long_strike': long_strike,
                'short_delta': round(float(short_opt.get('delta', 0.0)), 4),
                'credit_received': credit,
                'target_dtc': target_dtc,
                'stop_dtc': stop_dtc,
                'expiration': expiration,
                'market_phase': market_state.get('market_phase', ''),
            },
        }
        self.logger.info(
            'CreditSpread SIGNAL: short=%.1f long=%.1f credit=%.2f '
            'target_dtc=%.2f stop_dtc=%.2f exp=%s',
            short_strike, long_strike, credit, target_dtc, stop_dtc, expiration,
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
            'CreditSpread: position opened short=%.1f long=%.1f credit=%.2f exp=%s',
            pos['conditions']['short_strike'],
            pos['conditions']['long_strike'],
            pos['entry_price'],
            pos.get('expiration'),
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
        """Return closed-trade dict when an exit condition fires."""
        from datetime import time as dtime

        conds = pos.get('conditions', {})
        short_strike = conds['short_strike']
        long_strike = conds['long_strike']
        expiration = pos.get('expiration', '')
        credit_received = pos['entry_price']  # credit = entry_price per spec
        target_dtc = conds['target_dtc']
        stop_dtc = conds['stop_dtc']

        # Force exit at 3:00 PM.
        if ny_dt.time() >= dtime(15, 0):
            # Use current debit-to-close for P&L.
            dtc = self._get_debit_to_close(short_strike, long_strike, expiration)
            if dtc is None:
                dtc = credit_received  # worst-case fallback: break even
            exit_reason = 'force_exit'
        else:
            dtc = self._get_debit_to_close(short_strike, long_strike, expiration)
            if dtc is None:
                return None  # cannot evaluate, keep position

            exit_reason = None

            # Target: debit-to-close dropped to <= 30% of credit (kept 70%).
            if dtc <= target_dtc:
                exit_reason = 'target'

            # Stop: debit-to-close expanded to >= 200% of credit.
            elif dtc >= stop_dtc:
                exit_reason = 'stop'

        if exit_reason is None:
            return None

        # P&L for a credit spread (per share, 1 contract = 100 shares):
        # We sold the spread for `credit_received`.  To close we pay `dtc`.
        # Profit = (credit_received - dtc) × 100
        contracts = pos.get('contracts', 1)
        pnl = round(contracts * 100 * (credit_received - dtc), 2)

        closed_trade = {
            'strategy': self.NAME,
            'symbol': 'SPY',
            'direction': pos['direction'],
            'entry_price': credit_received,
            'exit_price': dtc,
            'pnl': pnl,
            'exit_reason': exit_reason,
            'entry_dt': pos['entry_dt'],
            'exit_dt': ny_dt.isoformat(),
            'vix': pos.get('vix', market_state.get('vix', 0.0)),
            'spy_price': market_state['spy_price'],
            'market_regime': market_state.get('market_phase', ''),
            'conditions': conds,
        }
        self.logger.info(
            'CreditSpread EXIT: reason=%s credit=%.2f dtc=%.2f pnl=%.2f',
            exit_reason, credit_received, dtc, pnl,
        )
        return closed_trade

    def _get_debit_to_close(
        self,
        short_strike: float,
        long_strike: float,
        expiration: str,
    ) -> Optional[float]:
        """Compute current debit-to-close for the spread.

        To close a bull put spread:
        • Buy back the short put  → pay ask(short_put)
        • Sell the long put       → receive bid(long_put)
        Debit-to-close = ask(short_put) - bid(long_put)
        """
        try:
            short_ask = self.feeds.get_option_ask(
                symbol='SPY', expiration=expiration,
                strike=short_strike, option_type='put',
            )
            long_bid = self.feeds.get_option_bid(
                symbol='SPY', expiration=expiration,
                strike=long_strike, option_type='put',
            )
        except Exception as exc:
            self.logger.warning('CreditSpread: DTC fetch error: %s', exc)
            return None

        if short_ask is None or long_bid is None:
            return None

        dtc = round(short_ask - long_bid, 2)
        # Debit cannot be negative (option prices > 0).
        return max(dtc, 0.0)

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
            self.logger.error('CreditSpread: DB insert failed: %s', exc)

    # ------------------------------------------------ lifecycle helpers
    def daily_loss_exceeded(self) -> bool:
        return self._daily_pnl <= -self._daily_loss_limit

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self.logger.info('CreditSpread: daily state reset')

    def force_close_all(self, market_state: dict, reason: str = 'force_exit') -> list:
        """Close all open spread positions at current market prices."""
        closed = []
        spy_price = market_state.get('spy_price', 0.0)
        ts = market_state.get('timestamp', datetime.now(NY_TZ))
        for pos in list(self._open_positions):
            expiration = pos.get('expiration', '')
            short_strike = pos.get('short_strike', 0.0)
            long_strike = pos.get('long_strike', 0.0)
            credit = pos.get('credit_received', 0.0)
            debit_to_close = credit * 1.5  # conservative: assume 150% of credit
            try:
                s_ask = self.feeds.get_option_ask('SPY', expiration, short_strike, 'put')
                l_bid = self.feeds.get_option_bid('SPY', expiration, long_strike, 'put')
                if s_ask > 0 and l_bid >= 0:
                    debit_to_close = max(s_ask - l_bid, 0.0)
            except Exception:
                pass
            pnl = round((credit - debit_to_close) * 100 * pos.get('contracts', 1), 2)
            self._daily_pnl += pnl
            trade = {
                'strategy': self.NAME, 'symbol': 'SPY', 'direction': 'put_spread',
                'entry_price': credit, 'exit_price': debit_to_close,
                'pnl': pnl, 'exit_reason': reason,
                'entry_dt': str(pos.get('entry_ts', ts)),
                'exit_dt': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                'vix': market_state.get('vix', 0.0), 'spy_price': spy_price,
                'market_regime': market_state.get('market_phase', ''),
                'conditions': pos.get('conditions', {}),
            }
            self._log_trade_to_db(trade)
            closed.append(trade)
            self.logger.info('CreditSpread force_close: pnl=%.2f reason=%s', pnl, reason)
        self._open_positions.clear()
        return closed

    def check_earnings_calendar(self) -> None:
        """No-op for Credit Spread — not earnings-dependent."""
