#!/usr/bin/env python3
"""
Hermes Execution Engine — Autonomous 19-strategy paper trading engine.
8 confirmed strategies (3 contracts each) + 12 experimental T-strategies (1 contract each).
Runs all strategies simultaneously via Tradier sandbox.
Cron: 45 9 * * 1-5  (9:45 AM ET, Mon-Fri)
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import io

import pandas as pd
import pytz
import requests
import yfinance as yf
from dotenv import load_dotenv

from core.data_feeds import DataFeeds
from core.telegram_alerts import send as tg_send

# ── Bootstrap ──────────────────────────────────────────────────────────────────
load_dotenv('/root/spy-0dte-trader/.env')

# ── Paths ──────────────────────────────────────────────────────────────────────
HERMES_ROOT    = Path('/root/hermes_system')
TRADES_DIR     = HERMES_ROOT / 'trades'
LOG_DIR        = HERMES_ROOT / 'logs'
POSITIONS_FILE = HERMES_ROOT / 'positions.json'
for _d in (TRADES_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'execution.log'),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger('hermes.execution')

# ── Tradier sandbox ────────────────────────────────────────────────────────────
SANDBOX_BASE    = 'https://sandbox.tradier.com/v1'
SANDBOX_KEY     = os.getenv('TRADIER_SANDBOX_KEY', '')
SANDBOX_ACCOUNT = os.getenv('TRADIER_SANDBOX_ACCOUNT_ID', '')

# ── Risk ───────────────────────────────────────────────────────────────────────
# Confirmed (3 contracts): max loss $600/day. Experimental (1 contract): $200/day.
MAX_LOSS_PER_CONTRACT = 200.0
MAX_DAILY_LOSS        = 8_000.0   # total paper money daily stop — halts ALL strategies

# ── Credit gate (BUG 4) ────────────────────────────────────────────────────────
# Day 1 data: every trade below $0.20 credit was stopped out; above $0.20 was profitable.
MIN_CREDIT = 0.20

# ── Watchdog limits (BUG 2) ───────────────────────────────────────────────────
MAX_CRASHES_PER_HOUR = 5
CRASH_WINDOW_MINUTES = 60
RESTART_DELAY_SECS   = 30

ET = pytz.timezone('America/New_York')

CONTANGO_THRESHOLD = 1.05
VIX3M_CBOE_URL     = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv'

# ── FOMC meeting dates 2025-2026 ───────────────────────────────────────────────
FOMC_DATES: frozenset = frozenset({
    date(2025,  1, 27), date(2025,  1, 28), date(2025,  1, 29),
    date(2025,  3, 17), date(2025,  3, 18), date(2025,  3, 19),
    date(2025,  5,  5), date(2025,  5,  6), date(2025,  5,  7),
    date(2025,  6, 16), date(2025,  6, 17), date(2025,  6, 18),
    date(2025,  7, 28), date(2025,  7, 29), date(2025,  7, 30),
    date(2025,  9, 15), date(2025,  9, 16), date(2025,  9, 17),
    date(2025, 10, 27), date(2025, 10, 28), date(2025, 10, 29),
    date(2025, 12, 15), date(2025, 12, 16), date(2025, 12, 17),
    date(2026,  1, 26), date(2026,  1, 27), date(2026,  1, 28),
    date(2026,  3, 16), date(2026,  3, 17), date(2026,  3, 18),
    date(2026,  4, 27), date(2026,  4, 28), date(2026,  4, 29),
    date(2026,  6, 15), date(2026,  6, 16), date(2026,  6, 17),
    date(2026,  7, 27), date(2026,  7, 28), date(2026,  7, 29),
    date(2026,  9, 14), date(2026,  9, 15), date(2026,  9, 16),
    date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28),
    date(2026, 12, 14), date(2026, 12, 15), date(2026, 12, 16),
})

S4_UNIVERSE = ['NVDA', 'TSLA', 'AMD', 'AAPL', 'MSFT', 'META', 'GOOGL', 'AMZN']


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    name:              str
    entry_days:        frozenset
    entry_start:       Tuple[int, int]
    entry_end:         Tuple[int, int]
    spread_type:       str             # put_spread | call_spread | iron_condor | earnings
    vix_min:           float
    vix_max:           float
    delta_target:      float
    profit_target_pct: float           # fraction of credit to keep
    stop_multiple:     float           # stop if debit-to-close >= credit * this
    force_exit_time:   Tuple[int, int]
    contracts:         int  = 1        # number of contracts per entry
    spread_width:      float = 2.0    # dollar width between short and long strikes
    extra:             dict = field(default_factory=dict)


@dataclass
class Leg:
    option_symbol: str
    side:          str   # sell_to_open | buy_to_open
    fill_price:    float
    delta:         float = 0.0
    theta:         float = 0.0


@dataclass
class Position:
    strategy:      str
    underlying:    str
    spread_type:   str
    legs:          List[Leg]
    entry_time:    datetime
    entry_credit:  float          # credit received; negative for earnings debit
    profit_thresh: float          # for spreads: debit-to-close <= this → profit target
                                  # for earnings: current mid >= this → profit target
    stop_thresh:   float          # for spreads: debit-to-close >= this → stop
                                  # for earnings: current mid <= this → stop
    force_exit:    Tuple[int, int]
    contracts:     int  = 1
    entry_state:   dict = field(default_factory=dict)
    s4_exit_date:  Optional[date] = None


# ── Strategy definitions ───────────────────────────────────────────────────────

STRATEGIES: Dict[str, StrategyConfig] = {
    # ── Confirmed strategies — 3 contracts each ($600/day max loss) ────────────
    'R3A': StrategyConfig(
        name='R3A', entry_days=frozenset({0}),
        entry_start=(10, 15), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=4, spread_width=2.0,
    ),
    'R3B': StrategyConfig(
        name='R3B', entry_days=frozenset({2}),
        entry_start=(10, 30), entry_end=(10, 45),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=3, spread_width=2.0,
    ),
    'R3D': StrategyConfig(
        name='R3D', entry_days=frozenset({0, 2, 4}),
        entry_start=(10, 15), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=3, spread_width=2.0,
        extra={'skip_fomc_weeks': True},
    ),
    'R3E': StrategyConfig(
        name='R3E', entry_days=frozenset({2}),
        entry_start=(10, 30), entry_end=(10, 45),
        spread_type='iron_condor', vix_min=13.0, vix_max=18.0, delta_target=0.16,
        profit_target_pct=0.50, stop_multiple=2.0, force_exit_time=(15, 30),
        contracts=1, spread_width=2.0,
    ),
    'R8': StrategyConfig(
        name='R8', entry_days=frozenset({4}),
        entry_start=(13, 0), entry_end=(13, 30),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.70, stop_multiple=1.8, force_exit_time=(15, 30),
        contracts=3, spread_width=2.0,
        extra={'require_spy_above_vwap': True},
    ),
    'R10': StrategyConfig(
        name='R10', entry_days=frozenset({1}),
        entry_start=(10, 30), entry_end=(11, 30),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=5, spread_width=2.0,
        extra={'require_spy_above_ma50_and_vwap': True},
    ),
    'S4': StrategyConfig(
        name='S4', entry_days=frozenset({0, 1, 2, 3, 4}),
        entry_start=(9, 45), entry_end=(10, 15),
        spread_type='earnings', vix_min=0.0, vix_max=100.0, delta_target=0.50,
        profit_target_pct=0.35, stop_multiple=0.0, force_exit_time=(15, 55),
        contracts=1,
        extra={'pre_earnings_days': 5, 'stop_pct': 0.25},
    ),
    # ── Experimental T-strategies — 1 contract each ($200/day max loss) ────────
    'T1_thursday_put': StrategyConfig(
        name='T1_thursday_put', entry_days=frozenset({3}),
        entry_start=(10, 30), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T2_monday_afternoon': StrategyConfig(
        name='T2_monday_afternoon', entry_days=frozenset({0}),
        entry_start=(13, 0), entry_end=(13, 30),
        spread_type='put_spread', vix_min=19.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T3_wednesday_afternoon': StrategyConfig(
        name='T3_wednesday_afternoon', entry_days=frozenset({2}),
        entry_start=(13, 0), entry_end=(13, 30),
        spread_type='put_spread', vix_min=19.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T4_friday_morning': StrategyConfig(
        name='T4_friday_morning', entry_days=frozenset({4}),
        entry_start=(10, 0), entry_end=(10, 30),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 30),
        contracts=1, spread_width=2.0,
    ),
    'T7_high_vix': StrategyConfig(
        name='T7_high_vix', entry_days=frozenset({0, 1, 2, 3, 4}),
        entry_start=(10, 0), entry_end=(11, 0),
        spread_type='put_spread', vix_min=20.0, vix_max=28.0, delta_target=0.15,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T8_delta_015': StrategyConfig(
        name='T8_delta_015', entry_days=frozenset({0, 2, 4}),
        entry_start=(10, 30), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.15,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T9_delta_025': StrategyConfig(
        name='T9_delta_025', entry_days=frozenset({0, 2, 4}),
        entry_start=(10, 30), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.25,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T10_wide_spread': StrategyConfig(
        name='T10_wide_spread', entry_days=frozenset({0, 2, 4}),
        entry_start=(10, 30), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=3.0,
    ),
    'T11_narrow_spread': StrategyConfig(
        name='T11_narrow_spread', entry_days=frozenset({0, 2, 4}),
        entry_start=(10, 30), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=1.0,
    ),
    'T12_max_data': StrategyConfig(
        name='T12_max_data', entry_days=frozenset({0, 1, 2, 3, 4}),
        entry_start=(10, 0), entry_end=(14, 0),
        spread_type='put_spread', vix_min=14.0, vix_max=24.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T13_thursday_afternoon': StrategyConfig(
        name='T13_thursday_afternoon', entry_days=frozenset({3}),
        entry_start=(13, 0), entry_end=(14, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'T14_vix_transition': StrategyConfig(
        name='T14_vix_transition', entry_days=frozenset({0, 1, 2, 3, 4}),
        entry_start=(10, 0), entry_end=(14, 0),
        spread_type='put_spread', vix_min=17.0, vix_max=20.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
        extra={'require_vix_falling': True},
    ),
}


# ── Utilities ──────────────────────────────────────────────────────────────────

def _occ_symbol(underlying: str, expiry: str, option_type: str, strike: float) -> str:
    """Build OCC option symbol: UNDERLYING + YYMMDD + C/P + 8-digit strike*1000."""
    yy = expiry[2:4]; mm = expiry[5:7]; dd = expiry[8:10]
    cp = 'C' if option_type.lower().startswith('c') else 'P'
    return f"{underlying}{yy}{mm}{dd}{cp}{int(round(strike * 1000)):08d}"


# ── Engine ─────────────────────────────────────────────────────────────────────

class HermesEngine:

    def __init__(self):
        self.feeds       = DataFeeds()
        self.positions:  List[Position] = []
        self.daily_pnl:  Dict[str, float] = {}
        self.total_pnl:  float = 0.0
        self.today:      date = date.today()
        self.entered:       Dict[str, bool] = {}
        self.entered_today: set = set()
        self._sweep_done: bool = False
        self._sb_hdrs    = {
            'Authorization': f'Bearer {SANDBOX_KEY}',
            'Accept':        'application/json',
        }
        self._contango_today: Optional[bool] = None
        self._contango_date:  Optional[date] = None
        # Options chain cache: (symbol, expiry) → (DataFrame, fetched_at monotonic)
        self._chain_cache: Dict[Tuple[str, str], Tuple[pd.DataFrame, float]] = {}
        self._chain_cache_ttl: float = 120.0  # 2 minutes
        self._load_positions()

    # ── Main loop (watchdog) ──────────────────────────────────────────────────

    def run(self) -> None:
        log.info('Hermes Engine starting — 22 strategies active (8 confirmed, 14 experimental).')
        tg_send('🚀 Hermes Engine started — 22 strategies active (8 confirmed @ 3 contracts, 14 experimental @ 1 contract).')
        crash_times: List[datetime] = []
        while True:
            try:
                self._run_loop()
            except Exception as exc:
                tb        = traceback.format_exc()
                now_et    = datetime.now(ET)
                crash_times.append(now_et)
                cutoff    = now_et - timedelta(minutes=CRASH_WINDOW_MINUTES)
                crash_times = [t for t in crash_times if t > cutoff]

                crash_log = LOG_DIR / 'crash.log'
                with crash_log.open('a') as f:
                    f.write(f'\n[{now_et.isoformat()}] CRASH #{len(crash_times)}\n{tb}\n')

                error_short = str(exc)[:200]

                if len(crash_times) >= MAX_CRASHES_PER_HOUR:
                    msg = (
                        f'🚨 Engine unstable — manual intervention needed. '
                        f'{len(crash_times)} crashes in {CRASH_WINDOW_MINUTES} minutes.'
                    )
                    log.critical('%s\n%s', msg, tb)
                    tg_send(msg)
                    return

                msg = f'⚠️ Engine crashed — restarting in {RESTART_DELAY_SECS}s. Error: {error_short}'
                log.error('%s\n%s', msg, tb)
                tg_send(msg)
                time.sleep(RESTART_DELAY_SECS)

    def _run_loop(self) -> None:
        while True:
            now = datetime.now(ET)
            if now.date() != self.today:
                self._reset_daily()
            if not self._is_market_hours(now):
                time.sleep(60)
                continue
            ms = self._get_market_state(now)
            self._check_entries(ms, now)
            self._check_exits(ms, now)
            time.sleep(60)

    # ── Market state ──────────────────────────────────────────────────────────

    def _get_market_state(self, now: datetime) -> dict:
        spy_q     = self.feeds.get_tradier_quote('SPY')
        spy       = spy_q.get('last', 0.0)
        vwap      = self.feeds.get_spy_vwap()  # FIX 5: computed from bars, not quote field
        vix       = self.feeds.get_vix()
        vix_prev  = self._get_vix_yesterday()
        ma50      = self._calc_ma50('SPY')
        if vwap == 0.0:
            log.warning('VWAP is 0.0 — feed not ready, blocking VWAP-dependent entries.')
        return {
            'timestamp':      now,
            'spy':            spy,
            'vix':            vix,
            'vix_yesterday':  vix_prev,
            'vix_falling':    bool(vix < vix_prev) if vix_prev else None,
            'vwap':           vwap,
            'ma50':           ma50,
            'spy_above_vwap': bool(spy > vwap) if vwap else False,
            'spy_above_ma50': bool(spy > ma50) if ma50 else None,
            'vix_direction':  'neutral',
            'market_regime':  self._regime(vix),
        }

    def _get_vix_yesterday(self) -> float:
        try:
            q = self.feeds.get_tradier_quote('VIX')
            current = q.get('last', 0.0)
            change_pct = q.get('change_percentage', 0.0)
            if current <= 0:
                return 0.0
            denominator = 1.0 + change_pct / 100.0
            if abs(denominator) < 0.001:
                return 0.0
            return round(current / denominator, 2)
        except Exception:
            return 0.0

    def _calc_ma50(self, symbol: str) -> float:
        try:
            df = self.feeds.get_ticker_daily(symbol, lookback=55)
            if df.empty or len(df) < 50:
                return 0.0
            return float(df['close'].iloc[-50:].mean())
        except Exception:
            return 0.0

    def _regime(self, vix: float) -> str:
        if vix < 15:  return 'low_vol'
        if vix < 20:  return 'normal'
        if vix < 25:  return 'elevated'
        return 'high_vol'

    # ── Options chain cache ───────────────────────────────────────────────────

    def _get_options_chain(self, symbol: str, expiry: str) -> pd.DataFrame:
        """Return a cached options chain (greeks=false), refreshed every 2 minutes."""
        key = (symbol, expiry)
        cached_df, fetched_at = self._chain_cache.get(key, (pd.DataFrame(), 0.0))
        if not cached_df.empty and (time.monotonic() - fetched_at) < self._chain_cache_ttl:
            return cached_df
        df = self.feeds.get_thetadata_options_chain(symbol, expiry)
        if df.empty:
            log.warning('ThetaData chain empty, falling back to Tradier sandbox')
            df = self.feeds.get_tradier_options_chain(symbol, expiry)
        if not df.empty:
            df['option_type'] = df['option_type'].str.lower()
        if not df.empty:
            self._chain_cache[key] = (df, time.monotonic())
        return df

    # ── VIX term structure filter ─────────────────────────────────────────────

    def _get_vix3m(self) -> float:
        """Get VIX3M from Tradier (same source as get_vix). Falls back to 0.0 on error."""
        try:
            data = self.feeds._tradier_get("/markets/quotes", params={"symbols": "VIX3M"})
            quote = data["quotes"]["quote"]
            if isinstance(quote, list):
                quote = quote[0]
            price = float(quote["last"])
            if price > 0:
                return price
        except Exception as exc:
            log.error("_get_vix3m Tradier failed: %s — returning 0.0", exc)
        return 0.0

    def _check_term_structure(self) -> bool:
        """Returns True (contango — trade normally) or False (backwardation — skip all entries).
        Result is cached for the current trading day."""
        today = date.today()
        if self._contango_date == today and self._contango_today is not None:
            return self._contango_today

        vix3m = self._get_vix3m()
        vix   = self.feeds.get_vix()

        if vix3m <= 0 or vix <= 0:
            log.warning('Term structure check incomplete (VIX3M=%.2f VIX=%.2f) — defaulting to contango.', vix3m, vix)
            self._contango_today = True
            self._contango_date  = today
            return True

        ratio       = vix3m / vix
        is_contango = ratio >= CONTANGO_THRESHOLD
        self._contango_today = is_contango
        self._contango_date  = today

        log.info('VIX term structure: VIX3M=%.2f VIX=%.2f ratio=%.4f regime=%s',
                 vix3m, vix, ratio, 'CONTANGO' if is_contango else 'BACKWARDATION')

        if not is_contango:
            log.warning('Backwardation regime — skipping all strategies today')
            tg_send(f'⚠️ Backwardation detected (ratio={ratio:.2f}) — no trades today')

        return is_contango

    # ── Entry gate ────────────────────────────────────────────────────────────

    def _check_entries(self, ms: dict, now: datetime) -> None:
        if not self._check_term_structure():
            return
        if not self._total_loss_ok():
            log.warning('Daily loss limit hit — skipping all entries.')
            return
        expiry = date.today().strftime('%Y-%m-%d')
        for name, cfg in STRATEGIES.items():
            if cfg.spread_type == 'earnings':
                if not self.entered.get('S4_checked'):
                    self._enter_earnings(cfg, ms, now)
                    self.entered['S4_checked'] = True
                continue
            if name in self.entered_today:
                continue
            # Hard guard: check live Tradier positions to survive restarts
            if name not in ('S4',) and self._strategy_already_open(name):
                self.entered_today.add(name)
                continue
            if not self._strategy_loss_ok(name):
                continue
            if not self._in_window(cfg, now):
                continue
            if not (cfg.vix_min <= ms['vix'] < cfg.vix_max):
                continue
            if not self._extra_ok(cfg, ms):
                continue
            log.info('Entry signal: %s', name)
            self._enter_spread(cfg, ms, now, expiry)

    def _in_window(self, cfg: StrategyConfig, now: datetime) -> bool:
        if now.weekday() not in cfg.entry_days:
            return False
        t = (now.hour, now.minute)
        return cfg.entry_start <= t < cfg.entry_end

    def _extra_ok(self, cfg: StrategyConfig, ms: dict) -> bool:
        x = cfg.extra
        if x.get('skip_fomc_weeks') and ms['timestamp'].date() in FOMC_DATES:
            log.debug('%s: FOMC week — skip.', cfg.name)
            return False
        if x.get('require_spy_above_vwap') and not ms.get('spy_above_vwap'):
            return False
        if x.get('require_spy_below_vwap') and ms.get('spy_above_vwap') is not False:
            return False
        if x.get('require_spy_above_ma50_and_vwap'):
            if not (ms.get('spy_above_vwap') and ms.get('spy_above_ma50')):
                return False
        if x.get('require_vix_falling') and not ms.get('vix_falling'):
            return False
        return True

    # ── Spread entry ──────────────────────────────────────────────────────────

    def _enter_spread(self, cfg: StrategyConfig, ms: dict, now: datetime, expiry: str) -> None:
        df = self._get_options_chain('SPY', expiry)
        if df.empty:
            log.warning('%s: empty options chain for %s.', cfg.name, expiry)
            return

        if cfg.spread_type == 'put_spread':
            legs, credit = self._build_put_spread(df, expiry, cfg.delta_target, cfg.spread_width)
        elif cfg.spread_type == 'call_spread':
            legs, credit = self._build_call_spread(df, expiry, cfg.delta_target, cfg.spread_width)
        elif cfg.spread_type == 'iron_condor':
            legs, credit = self._build_iron_condor(df, expiry, cfg.delta_target, cfg.spread_width)
        else:
            return

        if not legs or credit < MIN_CREDIT:
            log.info('%s: credit $%.2f below minimum $%.2f — skip.', cfg.name, credit, MIN_CREDIT)
            return

        theoretical  = credit
        slippage_pct = 0.02
        fill         = round(credit * (1.0 - slippage_pct), 2)

        submitted_legs: List[Leg] = []
        for leg in legs:
            if self._submit_order(leg.option_symbol, leg.side, cfg.contracts):
                submitted_legs.append(leg)
            else:
                log.error('%s: order rejected for %s — rolling back %d submitted leg(s).',
                          cfg.name, leg.option_symbol, len(submitted_legs))
                self._rollback_legs(cfg.name, submitted_legs, cfg.contracts)
                return

        # Verify both legs actually filled in Tradier before recording the position.
        if SANDBOX_KEY and SANDBOX_ACCOUNT:
            time.sleep(2)  # allow sandbox to settle fills
            open_syms = self._fetch_tradier_positions()
            missing = [leg for leg in legs if leg.option_symbol not in open_syms]
            if missing:
                filled = [leg for leg in legs if leg.option_symbol in open_syms]
                log.error('%s: fill verification failed — missing %s — rolling back %d filled leg(s).',
                          cfg.name, [l.option_symbol for l in missing], len(filled))
                self._rollback_legs(cfg.name, filled, cfg.contracts)
                return

        self.entered_today.add(cfg.name)
        profit_thresh = round(fill * (1.0 - cfg.profit_target_pct), 2)
        stop_thresh   = round(fill * cfg.stop_multiple, 2)

        pos = Position(
            strategy      = cfg.name,
            underlying    = 'SPY',
            spread_type   = cfg.spread_type,
            legs          = legs,
            entry_time    = now,
            entry_credit  = fill,
            profit_thresh = profit_thresh,
            stop_thresh   = stop_thresh,
            force_exit    = cfg.force_exit_time,
            contracts     = cfg.contracts,
            entry_state   = {
                'vix':               ms['vix'],
                'spy_entry':         ms['spy'],
                'vwap_entry':        ms.get('vwap'),
                'delta_entry':       legs[0].delta,
                'theta_entry':       legs[0].theta,
                'theoretical_mid':   theoretical,
                'fill_slippage_pct': round(slippage_pct * 100, 1),
                'day_of_week':       now.strftime('%A'),
                'days_to_fomc':      self._days_to_fomc(now.date()),
                'spy_vs_ma50':       round(ms['spy'] - ms['ma50'], 2) if ms.get('ma50') else None,
                'vix_direction':     ms['vix_direction'],
                'market_regime':     ms['market_regime'],
            },
        )
        self.positions.append(pos)
        self.entered[cfg.name] = True
        self._save_positions()
        log.info('Entered %s: %dc credit=%.2f profit_at=%.2f stop_at=%.2f', cfg.name, cfg.contracts, fill, profit_thresh, stop_thresh)
        tg_send(
            f"🟢 HERMES ENTRY: {cfg.name} {cfg.spread_type.upper()} ({cfg.contracts}c)\n"
            f"SPY {ms['spy']:.2f} | VIX {ms['vix']:.2f} | Credit ${fill:.2f}\n"
            f"Target ≤${profit_thresh:.2f} | Stop ≥${stop_thresh:.2f}"
        )

    # ── Spread builders ───────────────────────────────────────────────────────

    def _build_put_spread(self, df: pd.DataFrame, expiry: str, delta_target: float, spread_width: float = 2.0) -> Tuple[List[Leg], float]:
        puts = df[df['option_type'] == 'put'].copy()
        if puts.empty:
            return [], 0.0
        has_greeks = puts['delta'].abs().max() > 0.01
        if has_greeks:
            puts = puts[puts['delta'].abs() > 0.01]
        else:
            puts = puts[puts['bid'] > 0]
        if puts.empty:
            return [], 0.0
        if has_greeks:
            idx = (puts['delta'].abs() - delta_target).abs().idxmin()
        else:
            # No greeks — use SPY spot to find OTM puts within 5-30 pts of ATM
            spy_price = self.feeds.get_spy_price()
            otm_puts = puts[(puts['strike'] >= spy_price - 30) & (puts['strike'] <= spy_price)]
            if otm_puts.empty:
                return [], 0.0
            # Pick strike closest to delta_target * spy_price below spot
            target_strike = spy_price * (1 - delta_target * 0.025)  # ~0.20 delta ≈ 0.5% OTM for SPY 0DTE
            idx = (otm_puts['strike'] - target_strike).abs().idxmin()
        short = puts.loc[idx]
        long_strike = round(float(short['strike']) - spread_width, 0)
        long_rows   = puts[puts['strike'] == long_strike]
        if long_rows.empty:
            return [], 0.0
        long   = long_rows.iloc[0]
        credit = round(float(short['bid']) - float(long['ask']), 2)
        legs   = [
            Leg(_occ_symbol('SPY', expiry, 'P', short['strike']), 'sell_to_open',
                float(short['bid']), float(short['delta']), float(short['theta'])),
            Leg(_occ_symbol('SPY', expiry, 'P', long['strike']),  'buy_to_open',
                float(long['ask']),  float(long['delta']),  float(long['theta'])),
        ]
        return legs, credit

    def _build_call_spread(self, df: pd.DataFrame, expiry: str, delta_target: float, spread_width: float = 2.0) -> Tuple[List[Leg], float]:
        calls = df[df['option_type'] == 'call'].copy()
        if calls.empty:
            return [], 0.0
        has_greeks = calls['delta'].abs().max() > 0.01
        if has_greeks:
            calls = calls[calls['delta'].abs() > 0.01]
        else:
            calls = calls[calls['bid'] > 0]
        if calls.empty:
            return [], 0.0
        if has_greeks:
            idx = (calls['delta'].abs() - delta_target).abs().idxmin()
        else:
            idx = calls['bid'].idxmax()
        short = calls.loc[idx]
        long_strike = round(float(short['strike']) + spread_width, 0)
        long_rows   = calls[calls['strike'] == long_strike]
        if long_rows.empty:
            return [], 0.0
        long   = long_rows.iloc[0]
        credit = round(float(short['bid']) - float(long['ask']), 2)
        legs   = [
            Leg(_occ_symbol('SPY', expiry, 'C', short['strike']), 'sell_to_open',
                float(short['bid']), float(short['delta']), float(short['theta'])),
            Leg(_occ_symbol('SPY', expiry, 'C', long['strike']),  'buy_to_open',
                float(long['ask']),  float(long['delta']),  float(long['theta'])),
        ]
        return legs, credit

    def _build_iron_condor(self, df: pd.DataFrame, expiry: str, delta_target: float, spread_width: float = 2.0) -> Tuple[List[Leg], float]:
        p_legs, p_credit = self._build_put_spread(df, expiry, delta_target, spread_width)
        c_legs, c_credit = self._build_call_spread(df, expiry, delta_target, spread_width)
        if not p_legs or not c_legs:
            return [], 0.0
        return p_legs + c_legs, round(p_credit + c_credit, 2)

    # ── Earnings entry (S4) ───────────────────────────────────────────────────

    def _enter_earnings(self, cfg: StrategyConfig, ms: dict, now: datetime) -> None:
        if not self._in_window(cfg, now):
            return
        cal_path = Path('/root/spy-0dte-trader/data/earnings_calendar.json')
        if not cal_path.exists():
            log.warning('S4: earnings_calendar.json not found at %s.', cal_path)
            return
        calendar = json.loads(cal_path.read_text())
        today    = now.date()
        lead     = cfg.extra['pre_earnings_days']
        stop_pct = cfg.extra['stop_pct']

        for ticker in S4_UNIVERSE:
            entry_key = f'S4_{ticker}'
            if self.entered.get(entry_key):
                continue
            raw_dates = calendar.get(ticker, [])
            upcoming  = sorted(date.fromisoformat(d) for d in raw_dates if date.fromisoformat(d) > today)
            if not upcoming:
                continue
            next_earn = upcoming[0]
            if (next_earn - today).days != lead:
                continue
            ma50 = self._calc_ma50(ticker)
            px   = self.feeds.get_tradier_quote(ticker).get('last', 0.0)
            if ma50 and px < ma50:
                log.info('S4 %s: below MA50 (%.2f < %.2f) — skip.', ticker, px, ma50)
                continue
            expiry = (next_earn + timedelta(days=2)).strftime('%Y-%m-%d')
            df = self._get_options_chain(ticker, expiry)
            if df.empty:
                log.warning('S4 %s: empty chain for %s.', ticker, expiry)
                continue
            calls = df[df['option_type'] == 'call']
            if calls.empty:
                continue
            atm_idx = (calls['strike'] - px).abs().idxmin()
            atm     = calls.loc[atm_idx]
            debit   = round(float(atm['ask']) * 1.02, 2)
            if debit > 150.0:
                log.info('S4 %s: debit $%.2f > $150 max — skip.', ticker, debit)
                continue
            sym = _occ_symbol(ticker, expiry, 'C', float(atm['strike']))
            if not self._submit_order(sym, 'buy_to_open', cfg.contracts):
                continue
            mid      = float(atm['mid']) if float(atm.get('mid', 0)) > 0 else float(atm['ask'])
            slip_pct = round((debit / mid - 1.0) * 100, 1) if mid > 0 else 2.0
            pos = Position(
                strategy      = 'S4',
                underlying    = ticker,
                spread_type   = 'earnings',
                legs          = [Leg(sym, 'buy_to_open', debit, float(atm['delta']), float(atm['theta']))],
                entry_time    = now,
                entry_credit  = -debit,
                profit_thresh = round(debit * (1.0 + cfg.profit_target_pct), 2),
                stop_thresh   = round(debit * (1.0 - stop_pct), 2),
                force_exit    = cfg.force_exit_time,
                contracts     = cfg.contracts,
                entry_state   = {
                    'vix':               ms['vix'],
                    'spy_entry':         ms['spy'],
                    'vwap_entry':        ms.get('vwap'),
                    'delta_entry':       float(atm['delta']),
                    'theta_entry':       float(atm['theta']),
                    'theoretical_mid':   mid,
                    'fill_slippage_pct': slip_pct,
                    'day_of_week':       now.strftime('%A'),
                    'days_to_fomc':      self._days_to_fomc(now.date()),
                    'spy_vs_ma50':       round(ms['spy'] - ms['ma50'], 2) if ms.get('ma50') else None,
                    'vix_direction':     ms['vix_direction'],
                    'market_regime':     ms['market_regime'],
                    'ticker':            ticker,
                    'earnings_date':     next_earn.isoformat(),
                    'days_to_earnings':  lead,
                },
                s4_exit_date  = next_earn,
            )
            self.positions.append(pos)
            self.entered[entry_key] = True
            self._save_positions()
            log.info('S4 entered %s: debit=%.2f exp=%s earnings=%s', ticker, debit, expiry, next_earn)
            tg_send(
                f"🟢 HERMES S4: {ticker} earnings call\n"
                f"Debit ${debit:.2f} | Exp {expiry} | Earnings {next_earn}"
            )

    # ── Exit gate ─────────────────────────────────────────────────────────────

    def _check_exits(self, ms: dict, now: datetime) -> None:
        remaining = []
        for pos in self.positions:
            val    = self._current_value(pos)
            reason = self._exit_reason(pos, val, now)
            if reason:
                self._close_position(pos, reason, val, now)
            else:
                remaining.append(pos)
        self.positions = remaining
        self._save_positions()
        # At 15:58 sweep Tradier for any positions still open (catches crash-restart gaps).
        if (now.hour, now.minute) >= (15, 58) and not self._sweep_done:
            self._tradier_force_exit_sweep(now)
            self._sweep_done = True

    def _current_value(self, pos: Position) -> float:
        """Debit-to-close for spreads; current mid for long (earnings) positions."""
        try:
            total = 0.0
            for leg in pos.legs:
                q = self.feeds.get_tradier_quote(leg.option_symbol)
                if not q:
                    return abs(pos.entry_credit)
                if leg.side == 'sell_to_open':
                    total += q.get('ask', 0.0)  # cost to buy back short
                else:
                    if pos.spread_type == 'earnings':
                        total = (q.get('bid', 0.0) + q.get('ask', 0.0)) / 2.0
                    else:
                        total -= q.get('bid', 0.0)  # proceeds from selling long
            return round(total, 2)
        except Exception as exc:
            log.warning('current_value error: %s', exc)
            return abs(pos.entry_credit)

    def _exit_reason(self, pos: Position, val: float, now: datetime) -> str:
        t = (now.hour, now.minute)
        if t >= pos.force_exit:
            return 'force_exit'
        if pos.spread_type == 'earnings':
            if pos.s4_exit_date and now.date() >= pos.s4_exit_date:
                return 'earnings_date'
            if val >= pos.profit_thresh:
                return 'profit_target'
            if val <= pos.stop_thresh:
                return 'stop_loss'
        else:
            if val <= pos.profit_thresh:
                return 'profit_target'
            if val >= pos.stop_thresh:
                return 'stop_loss'
        return ''

    def _close_position(self, pos: Position, reason: str, exit_val: float, now: datetime) -> None:
        for leg in pos.legs:
            close_side = 'buy_to_close' if leg.side == 'sell_to_open' else 'sell_to_close'
            self._submit_order(leg.option_symbol, close_side, pos.contracts)

        if pos.spread_type == 'earnings':
            debit = abs(pos.entry_credit)
            pnl   = round((exit_val - debit) * 100 * pos.contracts, 2)
        else:
            pnl = round((pos.entry_credit - exit_val) * 100 * pos.contracts, 2)

        pnl_per_contract = round(pnl / pos.contracts, 2)
        hold_min = int((now - pos.entry_time).total_seconds() / 60)
        self.daily_pnl[pos.strategy] = self.daily_pnl.get(pos.strategy, 0.0) + pnl
        self.total_pnl += pnl

        trade = {
            'strategy':                  pos.strategy,
            'contracts':                 pos.contracts,
            'entry_time':                pos.entry_time.isoformat(),
            'entry_price':               pos.entry_credit,
            'theoretical_mid':           pos.entry_state.get('theoretical_mid'),
            'fill_slippage_pct':         pos.entry_state.get('fill_slippage_pct'),
            'vix_entry':                 pos.entry_state.get('vix'),
            'spy_entry':                 pos.entry_state.get('spy_entry'),
            'vwap_entry':                pos.entry_state.get('vwap_entry'),
            'delta_entry':               pos.entry_state.get('delta_entry'),
            'theta_entry':               pos.entry_state.get('theta_entry'),
            'spy_vs_ma50':               pos.entry_state.get('spy_vs_ma50'),
            'vix_direction':             pos.entry_state.get('vix_direction'),
            'day_of_week':               pos.entry_state.get('day_of_week'),
            'days_to_fomc':              pos.entry_state.get('days_to_fomc'),
            'exit_time':                 now.isoformat(),
            'exit_price':                exit_val,
            'exit_reason':               reason,
            'pnl':                       pnl,
            'realized_pnl_per_contract': pnl_per_contract,
            'total_realized_pnl':        round(self.daily_pnl[pos.strategy], 2),
            'hold_minutes':              hold_min,
            'market_regime':             pos.entry_state.get('market_regime'),
        }
        self._log_trade(trade)
        log.info('Closed %s: reason=%s pnl=%.2f (%.2f/c) hold=%dm', pos.strategy, reason, pnl, pnl_per_contract, hold_min)
        tg_send(
            f"{'🟩' if pnl >= 0 else '🟥'} HERMES EXIT: {pos.strategy} | {reason}\n"
            f"P&L: {'+' if pnl >= 0 else ''}${pnl:.2f} ({'+' if pnl_per_contract >= 0 else ''}${pnl_per_contract:.2f}/c) | Hold: {hold_min}m"
        )

    # ── Tradier sandbox ───────────────────────────────────────────────────────

    def _rollback_legs(self, strategy: str, filled_legs: List[Leg], contracts: int) -> None:
        """Close any legs that filled when another leg in the same spread was rejected."""
        for leg in filled_legs:
            close_side = 'buy_to_close' if leg.side == 'sell_to_open' else 'sell_to_close'
            log.warning('%s: ROLLBACK — submitting %s %s to close orphaned leg.',
                        strategy, close_side, leg.option_symbol)
            if not self._submit_order(leg.option_symbol, close_side, contracts):
                log.error('%s: ROLLBACK FAILED for %s — MANUAL CLOSE REQUIRED.',
                          strategy, leg.option_symbol)
                tg_send(
                    f'🚨 HERMES ROLLBACK FAILED: {strategy} {leg.option_symbol} '
                    f'naked leg open — manual close required!'
                )
        if filled_legs:
            tg_send(f'⚠️ HERMES {strategy}: leg rejection — {len(filled_legs)} leg(s) rolled back.')

    def _submit_order(self, option_symbol: str, side: str, qty: int) -> bool:
        if not SANDBOX_KEY or not SANDBOX_ACCOUNT:
            log.info('Sandbox creds not set — simulating %s %s.', side, option_symbol)
            return True
        underlying = option_symbol[:3] if len(option_symbol) >= 3 else 'SPY'
        data = {
            'class':         'option',
            'symbol':        underlying,
            'option_symbol': option_symbol,
            'side':          side,
            'quantity':      str(qty),
            'type':          'market',
            'duration':      'day',
        }
        try:
            r = requests.post(
                f'{SANDBOX_BASE}/accounts/{SANDBOX_ACCOUNT}/orders',
                data=data,
                headers=self._sb_hdrs,
                timeout=10,
            )
            if r.ok:
                log.info('Sandbox order accepted: %s %s', side, option_symbol)
                return True
            log.error('Sandbox order failed %d: %s', r.status_code, r.text[:200])
            return False
        except Exception as exc:
            log.error('Sandbox order error: %s', exc)
            return False

    def _fetch_tradier_positions_full(self) -> Dict[str, int]:
        """Returns {symbol: quantity} for all open Tradier positions."""
        if not SANDBOX_KEY or not SANDBOX_ACCOUNT:
            return {}
        try:
            r = requests.get(
                f'{SANDBOX_BASE}/accounts/{SANDBOX_ACCOUNT}/positions',
                headers=self._sb_hdrs,
                timeout=10,
            )
            if not r.ok:
                log.warning('Tradier positions fetch failed %d: %s', r.status_code, r.text[:100])
                return {}
            data = r.json()
            positions = data.get('positions', {})
            if not positions or positions == 'null':
                return {}
            pos_list = positions.get('position', [])
            if isinstance(pos_list, dict):
                pos_list = [pos_list]
            return {p['symbol']: int(p.get('quantity', 0)) for p in pos_list}
        except Exception as exc:
            log.error('fetch_tradier_positions_full error: %s', exc)
            return {}

    def _fetch_tradier_positions(self) -> set:
        """Returns set of option symbols currently open in Tradier sandbox."""
        return set(self._fetch_tradier_positions_full().keys())

    def _tradier_force_exit_sweep(self, now: datetime) -> None:
        """Close all positions open in Tradier regardless of in-memory state."""
        log.info('Tradier force-exit sweep at %s', now.strftime('%H:%M'))
        open_pos = self._fetch_tradier_positions_full()
        if not open_pos:
            log.info('Force-exit sweep: no open Tradier positions found.')
            return
        for symbol, qty in open_pos.items():
            if qty == 0:
                continue
            # positive qty = long (sell_to_close), negative qty = short (buy_to_close)
            side    = 'sell_to_close' if qty > 0 else 'buy_to_close'
            abs_qty = abs(qty)
            log.info('Force-exit sweep: %s %s x%d reason=force_exit', side, symbol, abs_qty)
            self._submit_order(symbol, side, abs_qty)
        tg_send(
            f'⚠️ Tradier force-exit sweep: closed {len(open_pos)} position(s) at {now.strftime("%H:%M")} ET.'
        )

    # ── Position persistence ──────────────────────────────────────────────────

    def _pos_to_dict(self, pos: Position) -> dict:
        return {
            'strategy':     pos.strategy,
            'underlying':   pos.underlying,
            'spread_type':  pos.spread_type,
            'legs': [
                {
                    'option_symbol': l.option_symbol,
                    'side':          l.side,
                    'fill_price':    l.fill_price,
                    'delta':         l.delta,
                    'theta':         l.theta,
                }
                for l in pos.legs
            ],
            'entry_time':    pos.entry_time.isoformat(),
            'entry_credit':  pos.entry_credit,
            'profit_thresh': pos.profit_thresh,
            'stop_thresh':   pos.stop_thresh,
            'force_exit':    list(pos.force_exit),
            'contracts':     pos.contracts,
            'entry_state':   pos.entry_state,
            's4_exit_date':  pos.s4_exit_date.isoformat() if pos.s4_exit_date else None,
        }

    def _pos_from_dict(self, d: dict) -> Position:
        return Position(
            strategy      = d['strategy'],
            underlying    = d['underlying'],
            spread_type   = d['spread_type'],
            legs          = [
                Leg(
                    option_symbol = l['option_symbol'],
                    side          = l['side'],
                    fill_price    = l['fill_price'],
                    delta         = l.get('delta', 0.0),
                    theta         = l.get('theta', 0.0),
                )
                for l in d['legs']
            ],
            entry_time    = datetime.fromisoformat(d['entry_time']),
            entry_credit  = d['entry_credit'],
            profit_thresh = d['profit_thresh'],
            stop_thresh   = d['stop_thresh'],
            force_exit    = tuple(d['force_exit']),
            contracts     = d.get('contracts', 1),
            entry_state   = d.get('entry_state', {}),
            s4_exit_date  = date.fromisoformat(d['s4_exit_date']) if d.get('s4_exit_date') else None,
        )

    def _save_positions(self) -> None:
        try:
            POSITIONS_FILE.write_text(
                json.dumps([self._pos_to_dict(p) for p in self.positions], indent=2, default=str)
            )
        except Exception as exc:
            log.error('Failed to save positions: %s', exc)

    def _load_positions(self) -> None:
        if not POSITIONS_FILE.exists():
            return
        try:
            saved = json.loads(POSITIONS_FILE.read_text())
            if not saved:
                return
            if SANDBOX_KEY and SANDBOX_ACCOUNT:
                tradier_syms = self._fetch_tradier_positions()
                reconciled: List[Position] = []
                for d in saved:
                    pos      = self._pos_from_dict(d)
                    leg_syms = {l.option_symbol for l in pos.legs}
                    if leg_syms & tradier_syms:
                        reconciled.append(pos)
                        log.info('Reconciled: restored %s from positions.json + Tradier.', pos.strategy)
                    else:
                        log.info('Reconciled: %s not found in Tradier — dropped.', pos.strategy)
                covered = {l.option_symbol for p in reconciled for l in p.legs}
                unknown = tradier_syms - covered
                if unknown:
                    log.warning('Tradier has untracked open positions: %s — cannot auto-restore metadata.', unknown)
                    tg_send(f'⚠️ Startup: untracked Tradier position(s) found: {", ".join(sorted(unknown))}')
                self.positions = reconciled
            else:
                self.positions = [self._pos_from_dict(d) for d in saved]
            log.info('Startup: loaded %d active position(s).', len(self.positions))
        except Exception as exc:
            log.error('Failed to load positions: %s', exc)

        # Rebuild entered_today from still-open positions so a crash/restart
        # mid-day does not allow a second entry for the same strategy (T12 in
        # particular has a 4-hour window and would otherwise re-fire every minute
        # after a restart).
        for p in self.positions:
            if p.strategy == 'S4':
                self.entered[f'S4_{p.underlying}'] = True
            else:
                self.entered_today.add(p.strategy)

        # Also scan today's closed trade log so strategies that already exited
        # earlier today are still blocked from re-entering.
        trades_today = TRADES_DIR / f'{date.today().isoformat()}.json'
        if trades_today.exists():
            try:
                for t in json.loads(trades_today.read_text()):
                    strat = t.get('strategy', '')
                    if strat == 'S4':
                        ticker = t.get('entry_state', {}).get('ticker', '')
                        if ticker:
                            self.entered[f'S4_{ticker}'] = True
                    elif strat:
                        self.entered_today.add(strat)
                log.info('Startup: restored entered_today=%s from today trade log.', self.entered_today)
            except Exception as exc:
                log.error('Failed to restore entered_today from today trade log: %s', exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log_trade(self, trade: dict) -> None:
        path   = TRADES_DIR / f'{date.today().isoformat()}.json'
        trades = json.loads(path.read_text()) if path.exists() else []
        trades.append(trade)
        path.write_text(json.dumps(trades, indent=2, default=str))

    def _reset_daily(self) -> None:
        self.today           = date.today()
        self.daily_pnl       = {}
        self.total_pnl       = 0.0
        self.entered         = {}
        self.entered_today   = set()
        self._sweep_done     = False
        self._contango_today = None
        self._contango_date  = None
        log.info('Daily reset: %s', self.today)

    def _strategy_already_open(self, name: str) -> bool:
        """Check live Tradier positions to see if this strategy already has open legs today."""
        try:
            open_syms = self._fetch_tradier_positions()
            today_str = date.today().strftime('%y%m%d')  # YYMMDD as in OCC symbol
            for sym in open_syms:
                # OCC format: SPY260611P00725000 — date is chars 3-8
                if len(sym) >= 15 and sym[3:9] == today_str:
                    # Check if this position belongs to this strategy via positions list
                    for pos in self.positions:
                        if pos.strategy == name:
                            leg_syms = {l.option_symbol for l in pos.legs}
                            if sym in leg_syms:
                                return True
            return False
        except Exception as exc:
            log.warning('_strategy_already_open check failed: %s — allowing entry', exc)
            return False

    def _strategy_loss_ok(self, name: str) -> bool:
        cfg   = STRATEGIES.get(name)
        limit = (cfg.contracts if cfg else 1) * MAX_LOSS_PER_CONTRACT
        return self.daily_pnl.get(name, 0.0) > -limit

    def _total_loss_ok(self) -> bool:
        return self.total_pnl > -MAX_DAILY_LOSS

    def _is_market_hours(self, now: datetime) -> bool:
        return (9, 30) <= (now.hour, now.minute) <= (16, 0) and now.weekday() < 5

    def _days_to_fomc(self, today: date) -> int:
        future = sorted(d for d in FOMC_DATES if d >= today)
        return (future[0] - today).days if future else 999


if __name__ == '__main__':
    HermesEngine().run()
