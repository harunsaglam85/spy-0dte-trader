"""
earnings_strategy.py — Earnings Momentum strategy (paper trading only, NO real orders).

Universe    : NVDA, TSLA, AMD, AAPL, MSFT, META, GOOGL, AMZN
Calendar    : data/earnings_calendar.json  {"TICKER": ["YYYY-MM-DD", ...], ...}
Entry       : 5 calendar days before earnings date
Condition   : stock price > 50-day MA (rising trend)
Instrument  : ATM call expiring AFTER earnings (dte_target = days_to_earnings + 2)
Max cost    : $150 per trade (skip if ATM call mid > $150)
Entry fill  : Tradier mid × 1.02
Exit        : day after earnings at open (first available price that day)
Exit fill   : Tradier mid × 0.98
Stop        : none (defined risk = premium paid)
Positions   : multiple allowed (one per ticker)
"""

import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pytz

NY_TZ = pytz.timezone('America/New_York')

UNIVERSE = ['NVDA', 'TSLA', 'AMD', 'AAPL', 'MSFT', 'META', 'GOOGL', 'AMZN']

# Number of calendar days before earnings to enter.
_ENTRY_LEAD_DAYS = 5

# Maximum option cost per trade (in dollars, not per-share).
_MAX_COST_DOLLARS = 150.0

# Default path relative to the project root; absolute path is resolved at runtime.
_CALENDAR_PATH = Path('data/earnings_calendar.json')


class Earnings:
    """Earnings momentum call strategy — paper trading only."""

    NAME = 'earnings'
    CALENDAR_PATH = _CALENDAR_PATH

    # ------------------------------------------------------------------ init
    def __init__(self, db, feeds):
        self.db = db
        self.feeds = feeds
        self.logger = logging.getLogger('strategy_earnings')

        self._open_positions: List[dict] = []
        self._daily_pnl: float = 0.0
        self._daily_loss_limit: float = 500.0
        self._paused: bool = False

        # Cache the earnings calendar; reloaded once per day.
        self._calendar: dict = {}
        self._calendar_cache: dict = {}
        self._calendar_loaded_date: Optional[date] = None

    # ---------------------------------------------------------- public API
    def check_signals(self, market_state: dict) -> Optional[dict]:
        """Check all universe tickers for an earnings entry signal.

        Returns the first valid signal found, or None.  The orchestrator
        should call this in a loop until it returns None to pick up all
        simultaneous entries.
        """
        ts: datetime = market_state.get('timestamp', datetime.now(NY_TZ))
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        vix = market_state.get('vix', 0.0)
        is_open = market_state.get('is_market_open', False)
        self.logger.info(
            'Earnings check_signals: time=%s is_open=%s paused=%s daily_pnl=%.2f vix=%.2f open_positions=%d',
            ny_dt.strftime('%H:%M:%S'), is_open, self._paused, self._daily_pnl, vix, len(self._open_positions),
        )
        if self._paused:
            self.logger.info('Earnings: SKIP — strategy paused')
            return None
        if self.daily_loss_exceeded():
            self.logger.info('Earnings: SKIP — daily loss limit reached (pnl=%.2f limit=%.2f)',
                             self._daily_pnl, self._daily_loss_limit)
            return None
        if not is_open:
            self.logger.info('Earnings: SKIP — market not open')
            return None

        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        today = ny_dt.date()

        # Refresh calendar once per day.
        if self._calendar_loaded_date != today:
            self._load_calendar()
            self._calendar_loaded_date = today

        # Tickers that already have an open position.
        open_tickers = {p['symbol'] for p in self._open_positions}

        for ticker in UNIVERSE:
            if ticker in open_tickers:
                continue

            earnings_date = self._next_earnings_date(ticker, today)
            if earnings_date is None:
                continue

            days_to_earnings = (earnings_date - today).days

            # Enter exactly 5 calendar days before.
            if days_to_earnings != _ENTRY_LEAD_DAYS:
                continue

            signal = self._build_signal(
                ticker=ticker,
                earnings_date=earnings_date,
                days_to_earnings=days_to_earnings,
                market_state=market_state,
                ny_dt=ny_dt,
            )
            if signal is not None:
                return signal

        return None

    def check_signals_all(self, market_state: dict) -> List[dict]:
        """Return ALL valid entry signals (one per ticker) for this bar.

        Convenience wrapper so the orchestrator can collect all signals
        without calling check_signals() in a loop manually.
        """
        signals: List[dict] = []
        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        today = ny_dt.date()

        if self._paused or self.daily_loss_exceeded():
            return signals
        if not market_state.get('is_market_open', False):
            return signals

        if self._calendar_loaded_date != today:
            self._load_calendar()
            self._calendar_loaded_date = today

        open_tickers = {p['symbol'] for p in self._open_positions}

        for ticker in UNIVERSE:
            if ticker in open_tickers:
                continue
            earnings_date = self._next_earnings_date(ticker, today)
            if earnings_date is None:
                continue
            days_to_earnings = (earnings_date - today).days
            if days_to_earnings != _ENTRY_LEAD_DAYS:
                continue
            signal = self._build_signal(
                ticker=ticker,
                earnings_date=earnings_date,
                days_to_earnings=days_to_earnings,
                market_state=market_state,
                ny_dt=ny_dt,
            )
            if signal is not None:
                signals.append(signal)

        return signals

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
            'Earnings: position opened %s entry=%.2f exp=%s earnings=%s',
            pos['symbol'], pos['entry_price'], pos.get('expiration'),
            pos['conditions'].get('earnings_date'),
        )

    def update_positions(self, market_state: dict) -> List[dict]:
        """Check open positions for exits. Returns list of closed trade dicts."""
        closed: List[dict] = []
        if not self._open_positions:
            return closed

        ts: datetime = market_state['timestamp']
        ny_dt = ts.astimezone(NY_TZ) if ts.tzinfo else NY_TZ.localize(ts)
        today = ny_dt.date()

        remaining: List[dict] = []
        for pos in self._open_positions:
            result = self._evaluate_exit(pos, market_state, ny_dt, today)
            if result is not None:
                closed.append(result)
                self._daily_pnl += result['pnl']
                self._log_trade_to_db(result)
            else:
                remaining.append(pos)

        self._open_positions = remaining
        return closed

    # ---------------------------------------------------- internal helpers
    def _build_signal(
        self,
        ticker: str,
        earnings_date: date,
        days_to_earnings: int,
        market_state: dict,
        ny_dt: datetime,
    ) -> Optional[dict]:
        """Build and validate a signal for a single ticker."""
        # Stock must be above its 50-day MA.
        try:
            stock_price = self.feeds.get_stock_price(ticker)
            ma50 = self.feeds.get_moving_average(ticker, period=50)
        except Exception as exc:
            self.logger.warning('Earnings: price/MA fetch failed for %s: %s', ticker, exc)
            return None

        if stock_price is None or ma50 is None:
            return None

        if stock_price <= ma50:
            self.logger.debug(
                'Earnings: %s price %.2f <= 50MA %.2f, skip', ticker, stock_price, ma50
            )
            return None

        # Expiration: after earnings + 2 extra calendar days.
        dte_target = days_to_earnings + 2
        try:
            expiration = self.feeds.get_next_expiry(
                dte_target=dte_target, symbol=ticker
            )
        except Exception as exc:
            self.logger.warning('Earnings: expiry fetch failed for %s: %s', ticker, exc)
            return None

        atm_strike = round(stock_price)

        try:
            option_mid = self.feeds.get_option_mid(
                symbol=ticker,
                expiration=expiration,
                strike=atm_strike,
                option_type='call',
            )
        except Exception as exc:
            self.logger.warning('Earnings: option mid fetch failed for %s: %s', ticker, exc)
            return None

        if option_mid is None or option_mid <= 0:
            return None

        # Skip if cost per contract > $150 (1 contract = 100 shares).
        cost_per_contract = option_mid * 100
        if cost_per_contract > _MAX_COST_DOLLARS:
            self.logger.info(
                'Earnings: %s ATM call cost $%.0f > max $%.0f, skip',
                ticker, cost_per_contract, _MAX_COST_DOLLARS,
            )
            return None

        entry_price = round(option_mid * 1.02, 2)
        # No hard stop — defined risk is the premium paid.
        stop_price = 0.0
        target_price = 0.0  # target = exit day after earnings at open

        signal = {
            'strategy': self.NAME,
            'symbol': ticker,
            'direction': 'long_call',
            'strike': float(atm_strike),
            'expiration': expiration,
            'contracts': 1,
            'entry_price': entry_price,
            'stop_price': stop_price,
            'target_price': target_price,
            'conditions': {
                'earnings_date': earnings_date.isoformat(),
                'days_to_earnings': days_to_earnings,
                'dte_target': dte_target,
                'stock_price': round(stock_price, 2),
                'ma50': round(ma50, 2),
                'option_mid_raw': round(option_mid, 2),
                'cost_per_contract': round(cost_per_contract, 2),
                'vix': market_state.get('vix', 0.0),
                'market_phase': market_state.get('market_phase', ''),
            },
        }
        self.logger.info(
            'Earnings SIGNAL: %s entry=%.2f strike=%s exp=%s earnings=%s '
            'cost_per_contract=$%.0f ma50=%.2f',
            ticker, entry_price, atm_strike, expiration,
            earnings_date.isoformat(), cost_per_contract, ma50,
        )
        return signal

    def _evaluate_exit(
        self,
        pos: dict,
        market_state: dict,
        ny_dt: datetime,
        today: date,
    ) -> Optional[dict]:
        """Exit the day AFTER earnings at open (first available mid price)."""
        conds = pos.get('conditions', {})
        earnings_date_str = conds.get('earnings_date')
        if not earnings_date_str:
            return None

        earnings_date = date.fromisoformat(earnings_date_str)
        exit_date = earnings_date + timedelta(days=1)

        # Exit on the day after earnings.
        if today < exit_date:
            return None

        ticker = pos['symbol']
        expiration = pos.get('expiration', '')
        strike = pos.get('strike', 0.0)
        entry_price = pos['entry_price']
        contracts = pos.get('contracts', 1)

        # Fetch current mid price for exit.
        try:
            current_mid = self.feeds.get_option_mid(
                symbol=ticker,
                expiration=expiration,
                strike=strike,
                option_type='call',
            )
        except Exception as exc:
            self.logger.warning('Earnings update: mid fetch error for %s: %s', ticker, exc)
            current_mid = None

        # If we can't get a price and option has expired (past expiration),
        # mark as worthless (exit_price = 0).
        if current_mid is None:
            exp_date = date.fromisoformat(expiration) if expiration else None
            if exp_date and today > exp_date:
                current_mid = 0.0
            else:
                return None  # wait until price is available

        exit_price = round(current_mid * 0.98, 2) if current_mid > 0 else 0.0
        pnl = round(contracts * 100 * (exit_price - entry_price), 2)

        # Determine exit reason.
        if today == exit_date:
            exit_reason = 'eod'  # planned exit: day after earnings
        else:
            exit_reason = 'force_exit'  # should not normally happen

        closed_trade = {
            'strategy': self.NAME,
            'symbol': ticker,
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
            'conditions': conds,
        }
        self.logger.info(
            'Earnings EXIT: %s reason=%s entry=%.2f exit=%.2f pnl=%.2f',
            ticker, exit_reason, entry_price, exit_price, pnl,
        )
        return closed_trade

    def _next_earnings_date(self, ticker: str, from_date: date) -> Optional[date]:
        """Return the nearest future earnings date for ticker, or None."""
        dates_raw = self._calendar_cache.get(ticker, [])
        future_dates = [
            date.fromisoformat(d) for d in dates_raw
            if date.fromisoformat(d) >= from_date
        ]
        return min(future_dates) if future_dates else None

    def _load_calendar(self) -> None:
        """Load earnings calendar from JSON file; silently skip if missing."""
        path = self.CALENDAR_PATH
        if not path.is_absolute():
            # Try relative to CWD, then relative to this file's parent's parent.
            cwd_path = Path.cwd() / path
            pkg_path = Path(__file__).parent.parent / path
            if cwd_path.exists():
                path = cwd_path
            elif pkg_path.exists():
                path = pkg_path

        if not path.exists():
            self.logger.warning(
                'Earnings: calendar not found at %s; no signals will fire', path
            )
            self._calendar_cache = {}
            return

        try:
            with open(path, 'r', encoding='utf-8') as fh:
                self._calendar_cache = json.load(fh)
            self.logger.info(
                'Earnings: calendar loaded from %s (%d tickers)',
                path, len(self._calendar_cache)
            )
        except Exception as exc:
            self.logger.error('Earnings: calendar load error: %s', exc)
            self._calendar_cache = {}

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
            self.logger.error('Earnings: DB insert failed: %s', exc)

    # ------------------------------------------------ lifecycle helpers
    def daily_loss_exceeded(self) -> bool:
        return self._daily_pnl <= -self._daily_loss_limit

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self.logger.info('Earnings: daily state reset')

    def force_close_all(self, market_state: dict, reason: str = 'force_exit') -> list:
        """Close all open earnings option positions at best-available price."""
        closed = []
        ts = market_state.get('timestamp', datetime.now(NY_TZ))
        spy_price = market_state.get('spy_price', 0.0)
        for pos in list(self._open_positions):
            symbol = pos.get('symbol', 'SPY')
            mid = 0.0
            try:
                mid = self.feeds.get_option_mid(
                    symbol=symbol,
                    expiration=pos.get('expiration', ''),
                    strike=pos.get('strike', 0.0),
                    option_type='call',
                )
            except Exception:
                pass
            exit_price = mid * 0.98 if mid > 0 else pos['entry_price'] * 0.50
            pnl = round((exit_price - pos['entry_price']) * 100 * pos.get('contracts', 1), 2)
            self._daily_pnl += pnl
            trade = {
                'strategy': self.NAME, 'symbol': symbol, 'direction': 'long_call',
                'entry_price': pos['entry_price'], 'exit_price': exit_price,
                'pnl': pnl, 'exit_reason': reason,
                'entry_dt': str(pos.get('entry_dt', ts)),
                'exit_dt': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                'vix': market_state.get('vix', 0.0), 'spy_price': spy_price,
                'market_regime': market_state.get('market_phase', ''),
                'conditions': pos.get('conditions', {}),
            }
            self._log_trade_to_db(trade)
            closed.append(trade)
            self.logger.info('Earnings force_close: %s pnl=%.2f reason=%s', symbol, pnl, reason)
        self._open_positions.clear()
        return closed

    def check_earnings_calendar(self) -> None:
        """Reload earnings calendar from disk. Called in pre-market by orchestrator."""
        self._load_calendar()
        self.logger.info('Earnings: calendar refreshed, %d tickers loaded',
                         len(self._calendar_cache))
