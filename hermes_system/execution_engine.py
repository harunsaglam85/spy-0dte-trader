#!/usr/bin/env python3
"""
Hermes Execution Engine — Autonomous multi-strategy LIVE trading engine.
PHASE 1 LIVE (go-live June 22 2026): $1,000 real-money capital, 1 contract per
strategy, $150 daily loss limit. Order submission, balances, positions and fills
run against the Tradier PRODUCTION API (account 6YB83257). Spread entries are
submitted as single multileg combo orders so a partial fill can never leave a
naked leg. Supervised by systemd (hermes-engine.service).
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
HEARTBEAT_FILE = HERMES_ROOT / 'heartbeat.txt'
for _d in (TRADES_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
# FIX 7: doubled lines in execution.log were NOT a duplicate handler in this
# module — basicConfig registers exactly one FileHandler (→execution.log) and one
# StreamHandler (→stdout). The duplication came from a second engine launched in a
# `screen` session that piped stdout through `tee -a execution.log` (see FIX 4):
# every record was written once by the FileHandler and again by tee'ing the
# StreamHandler into the same file (hence identical millisecond timestamps).
# systemd is now the sole supervisor and sends stdout to the journal
# (StandardOutput=journal), so the FileHandler is the single writer to the file.
# force=True drops any pre-existing root handlers before installing ours, so a
# re-import or re-exec can never stack a second FileHandler on the same file.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'execution.log'),
        logging.StreamHandler(),
    ],
    force=True,
)
log = logging.getLogger('hermes.execution')

# ── Tradier PRODUCTION (LIVE real-money account) ────────────────────────────────
# GO-LIVE June 22 2026: order submission, balances, positions and fills all run
# against the live api.tradier.com endpoint and the real account. The quotes/data
# feed in core.data_feeds already uses api.tradier.com and is unchanged.
PROD_BASE    = 'https://api.tradier.com/v1'
PROD_KEY     = os.getenv('TRADIER_API_KEY', '')
PROD_ACCOUNT = os.getenv('TRADIER_ACCOUNT_ID', '')

# ── Risk ───────────────────────────────────────────────────────────────────────
# PHASE 1 LIVE ($1,000 capital): 1 contract per strategy, max loss $200/contract.
MAX_LOSS_PER_CONTRACT = 200.0
MAX_DAILY_LOSS        = 150.0     # LIVE daily stop — halts ALL new entries for the day

# ── Credit gate (BUG 4) ────────────────────────────────────────────────────────
# Day 1 data: every trade below $0.20 credit was stopped out; above $0.20 was profitable.
MIN_CREDIT = 0.20

# ── Global VIX floor (FIX 2) ───────────────────────────────────────────────────
# 442-day backtest: VIX < 17 produced no tradable credit ($0.20+) on 90.7% of
# days, and configured minimums of 15/17 let strategies fire into those
# low-volatility regimes where credits sit below $0.20. Rather than edit each
# strategy's vix_min in the STRATEGIES dict (configs are not modified), enforce a
# single hard floor at entry time: the effective minimum is max(cfg.vix_min, 18).
# Strategies already at 20+ are unaffected.
GLOBAL_VIX_FLOOR = 15.0

# ── Order fill tracking (audit C5) ─────────────────────────────────────────────
# Sandbox positions can lag accepted orders by 10-30s, so fills are verified by
# polling each order's own status, never by waiting and snapshotting positions.
ORDER_TERMINAL_STATUSES = frozenset({'filled', 'rejected', 'canceled', 'expired', 'error'})
ORDER_FILL_TIMEOUT      = 30.0   # max seconds to wait for a market order to go terminal
ORDER_POLL_INTERVAL     = 2.0    # seconds between order-status polls
SIM_ORDER_ID            = 'SIM'  # sentinel order ID when running without sandbox creds

# ── Alert throttling (FIX 3) ───────────────────────────────────────────────────
# Sandbox rejects a leg roughly every 90s, and _rollback_legs fired a Telegram
# alert on every rollback — dozens of identical pings. Keep logging every rollback
# to file, but only page Telegram once per this many seconds per strategy.
ROLLBACK_ALERT_COOLDOWN = 600.0  # 10 minutes

# ── Order blacklist (FIX 1: retry-loop backoff) ────────────────────────────────
# A strike whose buy_to_open leg keeps getting rejected was re-submitted every
# loop with no backoff — June 15 fired 50+ orders on 750P/751P. After this many
# consecutive buy_to_open rejections inside the window, the strike is blacklisted
# so the entry path skips it until the cooldown expires. In-memory only (per the
# spec): a restart clears it, acceptable for a short 30-minute backoff.
BLACKLIST_REJECT_THRESHOLD = 3                    # consecutive buy_to_open rejects
BLACKLIST_REJECT_WINDOW    = timedelta(minutes=10)  # ...within this window
BLACKLIST_DURATION         = timedelta(minutes=30)  # ...blacklists the strike this long

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

# 2026 NYSE full-day market holidays — the engine idles, no polling.
MARKET_HOLIDAYS_2026: frozenset = frozenset({
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day observed
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
})

# 2026 NYSE early closes — market closes 1PM ET instead of 4PM ET.
EARLY_CLOSE_2026: frozenset = frozenset({
    date(2026, 11, 27), # Day after Thanksgiving — closes 1PM ET
    date(2026, 12, 24), # Christmas Eve — closes 1PM ET
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
    # PHASE 1 LIVE: 1 contract per strategy — scale up after 2 weeks validation
    # ── Confirmed strategies ────────────────────────────────────────────────────
    'R3A': StrategyConfig(
        name='R3A', entry_days=frozenset({0}),
        entry_start=(10, 15), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'R3B': StrategyConfig(
        name='R3B', entry_days=frozenset({2}),
        entry_start=(10, 30), entry_end=(10, 45),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
    ),
    'R3D': StrategyConfig(
        name='R3D', entry_days=frozenset({0, 2, 4}),
        entry_start=(10, 15), entry_end=(11, 0),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
        extra={},
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
        contracts=1, spread_width=2.0,
        extra={'require_spy_above_vwap': True},
    ),
    'R10': StrategyConfig(
        name='R10', entry_days=frozenset({1}),
        entry_start=(10, 30), entry_end=(11, 30),
        spread_type='put_spread', vix_min=15.0, vix_max=22.0, delta_target=0.20,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=2.0,
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
    # ── E-strategies (June 17 low-VIX frequency study) — 1 contract each ────────
    # Only E6 and E7 survived the live-calibrated backtest (sp=0.30 bid/ask, which
    # reproduces the Day-5 live fill of $0.15-0.19 at VIX 16.3); E1-E5/E8-E10 never
    # clear MIN_CREDIT in low VIX once realistic spreads are applied. See
    # /root/hermes_system/research/new_strategies_june17.md.
    # NOTE: both keep their as-designed vix_min, so GLOBAL_VIX_FLOOR=17 — lowered Jun 18 (backtest: blind-2025 WR improves vs 18 floor)
    # governs — E7 (13-18) will NOT fire until/unless the floor is revisited, and
    # E6 (13-20) only fires in 18-20. Added per operator decision to collect live
    # data behind the existing gates, not to bypass them.
    'E6_afternoon_decay': StrategyConfig(
        name='E6_afternoon_decay', entry_days=frozenset({0, 1, 2, 3, 4}),
        entry_start=(14, 0), entry_end=(14, 30),
        spread_type='put_spread', vix_min=13.0, vix_max=20.0, delta_target=0.15,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=3.0,
    ),
    'E7_lowvix_ic': StrategyConfig(
        name='E7_lowvix_ic', entry_days=frozenset({0, 1, 3, 4}),
        entry_start=(10, 30), entry_end=(11, 0),
        spread_type='iron_condor', vix_min=13.0, vix_max=18.0, delta_target=0.15,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=3.0,
    ),
    # ── E8 (afternoon strangle, D1 candidate) — 1 contract ──────────────────────
    # 5yr OOS validated on real ThetaData quotes (see research/d1_strangle_backtest.md).
    # PROTECTED variant ($5 wings): short OTM put + long put -$5, short OTM call +
    # long call +$5 — this is structurally an iron_condor, which is how the engine
    # builds it (spread_width=5.0). The naked variant was rejected for unbounded
    # tail risk. Protected blind-2025 WR 64.9% (full-sample 66.7%), PF 1.47,
    # max single loss $-232. Contango gate (VIX3M/VIX>=1.05) is applied globally in
    # _check_entries, so no per-strategy flag is needed. As with E6/E7 the global
    # VIX floor (17) governs, so despite vix_min=13 this only fires at VIX 17-22.
    'E8_afternoon_strangle': StrategyConfig(
        name='E8_afternoon_strangle', entry_days=frozenset({0, 1, 2, 3, 4}),
        entry_start=(14, 0), entry_end=(15, 0),
        spread_type='iron_condor', vix_min=13.0, vix_max=22.0, delta_target=0.30,
        profit_target_pct=0.75, stop_multiple=2.0, force_exit_time=(15, 45),
        contracts=1, spread_width=5.0,
    ),
}


# ── Utilities ──────────────────────────────────────────────────────────────────

def _occ_symbol(underlying: str, expiry: str, option_type: str, strike: float) -> str:
    """Build OCC option symbol: UNDERLYING + YYMMDD + C/P + 8-digit strike*1000."""
    yy = expiry[2:4]; mm = expiry[5:7]; dd = expiry[8:10]
    cp = 'C' if option_type.lower().startswith('c') else 'P'
    return f"{underlying}{yy}{mm}{dd}{cp}{int(round(strike * 1000)):08d}"


def parse_occ(option_symbol: str) -> Tuple[str, str, str, float]:
    """Parse an OCC option symbol into (underlying, expiry_yymmdd, type, strike).

    The OCC tail is always exactly 15 characters — YYMMDD + C/P + 8-digit
    strike*1000 — so the underlying root is everything before it. This is the
    only correct way to extract the root: a fixed slice like symbol[:3] turns
    NVDA/TSLA/AAPL/GOOGL into NVD/TSL/AAP/GOO, which brokers reject (audit D8).

    Example: parse_occ('NVDA260619C00140000') → ('NVDA', '260619', 'C', 140.0)

    Raises ValueError if the symbol cannot contain a root plus the 15-char tail
    (e.g. an equity symbol from an assigned position).
    """
    if len(option_symbol) < 16:
        raise ValueError(f'not an OCC option symbol: {option_symbol!r}')
    tail = option_symbol[-15:]
    if tail[6] not in ('C', 'P') or not tail[:6].isdigit() or not tail[7:].isdigit():
        raise ValueError(f'not an OCC option symbol: {option_symbol!r}')
    return option_symbol[:-15], tail[:6], tail[6], int(tail[7:]) / 1000.0


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
        # Item 2: page Telegram exactly once when the $150 daily loss limit trips.
        self._daily_limit_alerted: bool = False
        # Item 6: pre-flight gate. Entries are blocked until pre-flight passes; a
        # failure pauses entries (positions still monitored/exited) without crashing.
        self.preflight_ok: bool = False
        # FIX 3: last time a rollback Telegram alert was sent, per strategy.
        self.last_rollback_alert: Dict[str, float] = {}
        # FIX 1: retry-loop backoff. order_blacklist maps an option symbol to the
        # ET datetime its 30-min cooldown ends; reject_history tracks recent
        # buy_to_open rejection times per symbol. Both in-memory only.
        self.order_blacklist: Dict[str, datetime] = {}
        self.reject_history:  Dict[str, List[datetime]] = {}
        self._sb_hdrs    = {
            'Authorization': f'Bearer {PROD_KEY}',
            'Accept':        'application/json',
        }
        self._contango_today:      Optional[bool] = None
        self._contango_checked_at: float = 0.0
        self._contango_ttl:        float = 3600.0  # re-evaluate hourly
        # R1: yesterday's VIX close cannot change intraday — cache it per day.
        self._vix_yesterday:       float = 0.0
        self._vix_yesterday_date:  Optional[date] = None
        # Options chain cache: (symbol, expiry) → (DataFrame, fetched_at monotonic)
        self._chain_cache: Dict[Tuple[str, str], Tuple[pd.DataFrame, float]] = {}
        self._chain_cache_ttl: float = 120.0  # 2 minutes
        self._load_positions()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        # R3: systemd is the single supervisor. On a crash we log, alert, and
        # re-raise so the process exits nonzero — Restart=always brings it back,
        # StartLimitIntervalSec/StartLimitBurst (5/hour) stop a crash loop, and
        # the OnFailure= unit pages Telegram when the unit finally enters the
        # failed state. No internal restart/crash-counter logic.
        # L1: count from the source of truth, not a hardcoded string. The
        # knowledge base also lists R3C, T5, and T6, which exist there but are
        # NOT implemented in STRATEGIES — reconcile before deploying them.
        n = len(STRATEGIES)
        log.info('LIVE MODE — production account %s', PROD_ACCOUNT or '(unset)')
        log.info('Hermes Engine starting — %d strategies active. Daily loss limit $%.0f, 1 contract/strategy.',
                 n, MAX_DAILY_LOSS)
        tg_send(f'🚀 Hermes Engine started — LIVE account {PROD_ACCOUNT} — {n} strategies, '
                f'$150 daily loss limit, 1 contract each.')
        # Item 6: pre-flight before any trading. A failure pauses entries (sets
        # preflight_ok=False) but does not stop the loop, so exits keep running.
        self._preflight()
        try:
            self._run_loop()
        except Exception as exc:
            tb     = traceback.format_exc()
            now_et = datetime.now(ET)
            with (LOG_DIR / 'crash.log').open('a') as f:
                f.write(f'\n[{now_et.isoformat()}] CRASH\n{tb}\n')
            log.critical('Engine crashed — exiting for systemd restart.\n%s', tb)
            tg_send(f'⚠️ Engine crashed — systemd will restart. Error: {str(exc)[:200]}')
            raise

    def _run_loop(self) -> None:
        while True:
            now = datetime.now(ET)
            if now.date() != self.today:
                self._reset_daily()
            self._touch_heartbeat(now)
            if not self._is_market_hours(now):
                time.sleep(60)
                continue
            # R1: one batched Tradier quote call per iteration covers SPY, VIX,
            # and every open leg — instead of one 18s-throttled call each, which
            # stretched the "60-second" risk loop to 2-5 minutes under load.
            quotes = self.feeds.get_tradier_quotes(self._batch_quote_symbols())
            ms = self._get_market_state(now, quotes)
            # Item 2: log current daily P&L against the limit every loop.
            log.info('Daily P&L: $%.2f / limit -$%.2f', self.total_pnl, MAX_DAILY_LOSS)
            self._check_entries(ms, now)
            self._check_exits(ms, now, quotes)
            time.sleep(60)

    def _batch_quote_symbols(self) -> List[str]:
        """BUG3: exit monitoring must quote exactly the leg symbols recorded at
        entry — never strikes re-derived from spot, which drift as SPY moves
        (R3D was monitored at 736 while the position's leg was 737, freezing
        its exit logic at entry credit). Symbols come from the open positions'
        legs, unioned with positions.json on disk so an engine whose in-memory
        state has drifted from the persisted record still quotes every leg
        actually open at the broker."""
        syms = {leg.option_symbol for pos in self.positions for leg in pos.legs}
        try:
            if POSITIONS_FILE.exists():
                for d in json.loads(POSITIONS_FILE.read_text() or '[]'):
                    for l in d.get('legs', []):
                        if l.get('option_symbol'):
                            syms.add(l['option_symbol'])
        except Exception as exc:
            log.warning('Could not read %s for batch quote symbols: %s', POSITIONS_FILE, exc)
        return ['SPY', 'VIX'] + sorted(syms)

    def _touch_heartbeat(self, now: datetime) -> None:
        """U3: external liveness signal, touched each in-hours loop iteration.
        A hung process (stuck socket, not crashed) is invisible to systemd;
        hermes-heartbeat.timer checks this file every 5 minutes and pages
        Telegram if it goes stale during market hours."""
        try:
            HEARTBEAT_FILE.write_text(now.isoformat())
        except Exception as exc:
            log.warning('heartbeat write failed: %s', exc)

    # ── Market state ──────────────────────────────────────────────────────────

    def _get_market_state(self, now: datetime, quotes: Dict[str, dict]) -> dict:
        spy       = quotes.get('SPY', {}).get('last', 0.0)
        vwap      = self.feeds.get_spy_vwap()  # FIX 5: computed from bars, not quote field
        vix_q     = quotes.get('VIX', {})
        vix       = vix_q.get('last', 0.0)
        if not (5.0 <= vix <= 150.0):
            vix = 0.0  # fail closed — 0.0 fails every vix_min check (same as get_vix)
        vix_prev  = self._get_vix_yesterday(vix_q)
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

    def _get_vix_yesterday(self, vix_quote: Optional[dict] = None) -> float:
        """Yesterday's VIX close, derived from today's quote + change_percentage.
        Cached for the day (R1) — it cannot change intraday, so it should never
        cost a rate-limit slot after the first successful read."""
        if self._vix_yesterday_date == datetime.now(ET).date() and self._vix_yesterday > 0:
            return self._vix_yesterday
        try:
            q = vix_quote if vix_quote else self.feeds.get_tradier_quote('VIX')
            current = q.get('last', 0.0)
            change_pct = q.get('change_percentage', 0.0)
            if current <= 0:
                return 0.0
            denominator = 1.0 + change_pct / 100.0
            if abs(denominator) < 0.001:
                return 0.0
            value = round(current / denominator, 2)
            self._vix_yesterday = value
            self._vix_yesterday_date = datetime.now(ET).date()
            return value
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
        """Return a cached options chain with real greeks, refreshed every 2 minutes."""
        key = (symbol, expiry)
        cached_df, fetched_at = self._chain_cache.get(key, (pd.DataFrame(), 0.0))
        if not cached_df.empty and (time.monotonic() - fetched_at) < self._chain_cache_ttl:
            return cached_df
        df = self.feeds.get_tradier_options_chain(symbol, expiry)
        if df.empty:
            log.warning('Tradier chain empty, falling back to ThetaData (no greeks — flag trades in validation dataset)')
            df = self.feeds.get_thetadata_options_chain(symbol, expiry)
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
        """Returns True (contango — trade normally) or False (backwardation or
        missing data — skip all entries). Fails closed: incomplete data blocks
        entries and is not cached, so the next loop retries. A valid reading is
        cached for one hour so an intraday regime flip is picked up."""
        if self._contango_today is not None and \
                (time.monotonic() - self._contango_checked_at) < self._contango_ttl:
            return self._contango_today

        vix3m = self._get_vix3m()
        vix   = self.feeds.get_vix()

        if vix3m <= 0 or vix <= 0:
            log.warning('Term structure check incomplete (VIX3M=%.2f VIX=%.2f) — failing closed, blocking entries; will retry.', vix3m, vix)
            return False

        ratio       = vix3m / vix
        is_contango = ratio >= CONTANGO_THRESHOLD
        self._contango_today      = is_contango
        self._contango_checked_at = time.monotonic()

        log.info('VIX term structure: VIX3M=%.2f VIX=%.2f ratio=%.4f regime=%s',
                 vix3m, vix, ratio, 'CONTANGO' if is_contango else 'BACKWARDATION')

        if not is_contango:
            log.warning('Backwardation regime — skipping all strategies today')
            tg_send(f'⚠️ Backwardation detected (ratio={ratio:.2f}) — no trades today')

        return is_contango

    # ── Pre-flight (Item 6) ───────────────────────────────────────────────────

    def _account_balance(self) -> Optional[float]:
        """Total account equity from the Tradier production /balances endpoint.
        Returns None on any error so pre-flight treats it as a failed check."""
        if not PROD_KEY or not PROD_ACCOUNT:
            return None
        try:
            r = requests.get(f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/balances',
                             headers=self._sb_hdrs, timeout=10)
            if not r.ok:
                log.warning('Balance fetch failed %d: %s', r.status_code, r.text[:100])
                return None
            b = r.json().get('balances') or {}
            for key in ('total_equity', 'total_cash', 'option_buying_power'):
                v = b.get(key)
                if v is not None:
                    return float(v)
            log.warning('Balance response missing equity fields: %s', str(b)[:200])
            return None
        except Exception as exc:
            log.warning('Balance fetch error: %s', exc)
            return None

    def _preflight(self) -> None:
        """Item 6: verify the live trading stack before any entries are allowed.
        Checks (1) production API auth, (2) account balance >= $500, (3) ThetaData
        snapshot endpoint, (4) VIX feed. Any failure leaves preflight_ok False and
        pages Telegram; it never raises, so the loop keeps running and open
        positions are still monitored and exited."""
        failures: List[str] = []

        # 1. Production API connection / auth.
        if PROD_KEY and PROD_ACCOUNT:
            try:
                r = requests.get(f'{PROD_BASE}/user/profile', headers=self._sb_hdrs, timeout=10)
                if not r.ok:
                    failures.append(f'API profile HTTP {r.status_code}')
            except Exception as exc:
                failures.append(f'API profile error: {exc}')
        else:
            failures.append('production API creds not set')

        # 2. Account balance >= $500.
        bal = self._account_balance()
        if bal is None:
            failures.append('balance unavailable')
        elif bal < 500.0:
            failures.append(f'balance ${bal:.2f} < $500 minimum')

        # 3. ThetaData snapshot endpoint responding.
        try:
            r = requests.get('http://localhost:25503/v3/option/snapshot/quote?symbol=SPY', timeout=10)
            if not r.ok:
                failures.append(f'ThetaData HTTP {r.status_code}')
        except Exception as exc:
            failures.append(f'ThetaData error: {exc}')

        # 4. VIX feed working.
        try:
            vix = self.feeds.get_vix()
            if not vix or vix <= 0:
                failures.append('VIX feed returned 0')
        except Exception as exc:
            failures.append(f'VIX feed error: {exc}')

        if failures:
            self.preflight_ok = False
            reason = '; '.join(failures)
            log.warning('⚠️ Pre-flight failed: %s — entries paused.', reason)
            tg_send(f'⚠️ Pre-flight failed: {reason} — entries paused (positions still monitored).')
        else:
            self.preflight_ok = True
            log.info('✅ Pre-flight passed — LIVE trading active (balance $%.2f).', bal)
            tg_send(f'✅ Pre-flight passed — LIVE trading active. Balance ${bal:.2f}.')

    # ── Entry gate ────────────────────────────────────────────────────────────

    def _check_entries(self, ms: dict, now: datetime) -> None:
        # Item 6: never enter while pre-flight is failing (API/balance/data/VIX).
        if not self.preflight_ok:
            log.warning('Pre-flight not passed — entries paused (positions still monitored).')
            return
        if not self._check_term_structure():
            return
        if not self._total_loss_ok():
            # Item 2: page Telegram once, then keep blocking entries for the day.
            if not self._daily_limit_alerted:
                tg_send(f'⛔ DAILY LOSS LIMIT HIT — no more entries today. P&L: ${self.total_pnl:.2f}')
                self._daily_limit_alerted = True
            log.warning('Daily loss limit hit (P&L $%.2f / limit -$%.2f) — skipping all entries.',
                        self.total_pnl, MAX_DAILY_LOSS)
            return
        expiry = date.today().strftime('%Y-%m-%d')
        # R1: snapshot broker positions at most once per loop and share it
        # across strategies, instead of one fetch per entry-eligible strategy.
        open_syms: Optional[set] = None
        for name, cfg in STRATEGIES.items():
            if cfg.spread_type == 'earnings':
                # C3: S4 is DISABLED — triply broken as coded (audit June 11):
                #   1. Never evaluates: S4_checked was set on the first loop
                #      iteration of the day (~9:30), but _enter_earnings requires
                #      the 9:45-10:15 window — so it was checked once, failed the
                #      window test, and was locked out for the day, every day.
                #   2. Invalid OCC parsing: _submit_order derived the underlying
                #      as option_symbol[:3], turning NVDA/TSLA/AAPL/... into
                #      NVD/TSL/AAP — guaranteed broker rejections (see D8).
                #   3. Can't hold overnight: S4 is a multi-day pre-earnings hold,
                #      but the generic force_exit check closes it at 15:55 and the
                #      15:58 sweep flattens everything at the broker — so even a
                #      successful entry would never trade the backtested strategy.
                # TODO: rebuild S4 — move the check inside the entry window,
                # exempt multi-day positions from force_exit and the sweep, and
                # fix the no-op debit cap (debit * 100 > 150) — then re-enable.
                if not self.entered.get('S4_disabled_logged'):
                    log.info('S4 disabled pending rebuild (audit C3) — earnings strategy will not trade.')
                    self.entered['S4_disabled_logged'] = True
                continue
            if name in self.entered_today:
                continue
            # Hard guard: check live Tradier positions to survive restarts
            if name not in ('S4',):
                if open_syms is None:
                    open_syms = self._fetch_tradier_positions()
                if self._strategy_already_open(name, open_syms):
                    self.entered_today.add(name)
                    continue
            if not self._strategy_loss_ok(name):
                continue
            if not self._in_window(cfg, now):
                continue
            # FIX 2: hard global VIX floor of 18 layered over each strategy's own
            # vix_min, without touching the STRATEGIES dict.
            eff_vix_min = max(cfg.vix_min, GLOBAL_VIX_FLOOR)
            # FIX 5: a strategy in its entry window that doesn't fire because VIX is
            # below its (effective) minimum should say so, not skip silently — this
            # is the dominant reason strategies don't fire on low-VIX days.
            if ms['vix'] < eff_vix_min:
                log.info('%s: VIX %.2f below minimum %.2f — skip.', cfg.name, ms['vix'], eff_vix_min)
                continue
            if ms['vix'] >= cfg.vix_max:
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
        if x.get('skip_fomc_weeks') and self._is_fomc_week(ms['timestamp'].date()):
            log.info('%s: FOMC week — skip.', cfg.name)
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

    # ── Order blacklist (FIX 1) ─────────────────────────────────────────────────

    def _is_blacklisted(self, option_symbol: str, now: datetime) -> bool:
        """True while a strike is in retry-loop backoff. Expired entries are
        cleared lazily here so the dict never grows unbounded."""
        until = self.order_blacklist.get(option_symbol)
        if until is None:
            return False
        if now >= until:
            del self.order_blacklist[option_symbol]
            self.reject_history.pop(option_symbol, None)
            return False
        return True

    def _record_buy_reject(self, option_symbol: str, now: datetime) -> None:
        """Count a buy_to_open rejection. After BLACKLIST_REJECT_THRESHOLD
        rejections within BLACKLIST_REJECT_WINDOW, blacklist the strike for
        BLACKLIST_DURATION so the engine stops resubmitting it every loop."""
        hist = [t for t in self.reject_history.get(option_symbol, [])
                if now - t <= BLACKLIST_REJECT_WINDOW]
        hist.append(now)
        self.reject_history[option_symbol] = hist
        if len(hist) >= BLACKLIST_REJECT_THRESHOLD:
            until = now + BLACKLIST_DURATION
            self.order_blacklist[option_symbol] = until
            self.reject_history.pop(option_symbol, None)
            mins = int(BLACKLIST_REJECT_WINDOW.total_seconds() // 60)
            log.warning('FIX1: %s blacklisted until %s ET after %d buy_to_open rejections in %dmin.',
                        option_symbol, until.strftime('%H:%M'), BLACKLIST_REJECT_THRESHOLD, mins)
            tg_send(f'⛔ HERMES: {option_symbol} blacklisted 30m after '
                    f'{BLACKLIST_REJECT_THRESHOLD} buy_to_open rejections (retry-loop backoff).')

    def _clear_buy_reject(self, option_symbol: str) -> None:
        """A confirmed buy_to_open fill resets the consecutive-rejection counter."""
        self.reject_history.pop(option_symbol, None)

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

        # FIX 1: if any leg's strike is in retry-loop backoff, skip the whole
        # entry before submitting anything — otherwise the sell leg fills and
        # then gets rolled back the moment the blacklisted buy_to_open is
        # rejected again. (Close/rollback orders are never blacklisted.)
        blacklisted = [l.option_symbol for l in legs if self._is_blacklisted(l.option_symbol, now)]
        if blacklisted:
            log.info('%s: skip — strike(s) in retry-loop backoff: %s', cfg.name, blacklisted)
            return

        theoretical  = credit
        slippage_pct = 0.02
        fill         = round(credit * (1.0 - slippage_pct), 2)

        # Item 5 (critical safety fix): submit BOTH legs — all four for an iron
        # condor — as ONE Tradier multileg combo order (class=multileg, type=
        # credit). The combo fills atomically or not at all, so a partial fill can
        # never leave a naked short leg (the old leg-by-leg path could fill the
        # sell and then have the buy rejected → unlimited downside). The limit is
        # the conservative net credit from the chain (short bid − long ask), which
        # is marketable, so a live order should fill quickly.
        entry_order_id: Optional[str] = self._submit_multileg_order(
            'SPY', legs, 'credit', credit, cfg.contracts)
        if not entry_order_id:
            # Combo rejected outright — nothing opened, no naked leg to roll back.
            # Count the buy-side strike(s) for retry-loop backoff (FIX 1).
            for leg in legs:
                if leg.side == 'buy_to_open':
                    self._record_buy_reject(leg.option_symbol, now)
            log.error('%s: multileg combo order rejected — no position opened.', cfg.name)
            return

        # C5: verify the fill by polling the combo order's own ID. Because the
        # combo is atomic, a non-'filled' status means NOTHING opened — there is no
        # naked leg to unwind; cancel if still working, record the backoff, skip.
        if PROD_KEY and PROD_ACCOUNT:
            st     = self._await_orders([entry_order_id]).get(entry_order_id, {})
            status = st.get('status')
            if status != 'filled':
                if status not in ORDER_TERMINAL_STATUSES:
                    if not self._cancel_order(entry_order_id):
                        tg_send(f'🚨 HERMES {cfg.name}: combo order {entry_order_id} '
                                f'uncancelable — may fill unattended, check manually!')
                for leg in legs:
                    if leg.side == 'buy_to_open':
                        self._record_buy_reject(leg.option_symbol, now)
                log.error('%s: multileg combo order %s not filled (status=%s) — no position opened.',
                          cfg.name, entry_order_id, status)
                return
            # C5: record the broker's actual per-leg fills and replace the modeled
            # 2%-slippage credit with the real net credit when every leg reports a
            # price — recorded P&L should be broker truth, not model-on-model.
            leg_fills = self._extract_leg_fills(st)
            reported  = 0
            for leg in legs:
                px = leg_fills.get(leg.option_symbol, 0.0)
                if px > 0:
                    leg.fill_price = px
                    reported += 1
            if reported == len(legs):
                actual_credit = round(sum(
                    leg.fill_price if leg.side == 'sell_to_open' else -leg.fill_price
                    for leg in legs), 2)
                if actual_credit > 0:
                    fill         = actual_credit
                    slippage_pct = (1.0 - fill / theoretical) if theoretical > 0 else 0.0
                    log.info('%s: actual combo fills recorded — net credit $%.2f (modeled $%.2f).',
                             cfg.name, fill, round(theoretical * 0.98, 2))
                else:
                    log.critical('%s: actual net credit $%.2f is non-positive — keeping modeled '
                                 'credit $%.2f for thresholds; INVESTIGATE FILLS.',
                                 cfg.name, actual_credit, fill)
                    tg_send(f'🚨 HERMES {cfg.name}: filled at non-positive net credit '
                            f'${actual_credit:.2f} — check fills manually.')
            else:
                log.warning('%s: avg_fill_price missing for %d/%d legs — keeping modeled credit $%.2f.',
                            cfg.name, len(legs) - reported, len(legs), fill)

        # FIX 1: combo is confirmed filled here — reset the rejection counter for
        # each buy_to_open strike so a future bad streak starts clean.
        for leg in legs:
            if leg.side == 'buy_to_open':
                self._clear_buy_reject(leg.option_symbol)

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
                'entry_order_id':    entry_order_id,  # Item 7: production order ID
            },
        )
        self.positions.append(pos)
        self.entered[cfg.name] = True
        # FIX 1: persist the tracked position to positions.json BEFORE writing the
        # trade-log stub. By this point every leg is confirmed filled at the broker,
        # so positions.json must become the first durable record of the live spread.
        # If the engine restarts in the gap, _load_positions reconciles the saved
        # leg symbols against Tradier and restores the position with full metadata
        # (exit thresholds, entry_state), and entered_today is rebuilt from it.
        # The stub is written second and now carries the leg symbols, so the
        # trade-log rebuild path can also match the legs and block re-entry even if
        # positions.json reconciliation drops the position (e.g. already expired).
        self._save_positions()
        self._log_trade({
            'strategy':        cfg.name,
            'status':          'open',
            'mode':            'LIVE',                # Item 7
            'account':         PROD_ACCOUNT,         # Item 7
            'entry_time':      str(now),
            'legs':            [l.option_symbol for l in legs],
            'order_id':        entry_order_id,       # Item 7: production order ID
            'real_fill_price': fill,                 # Item 7: actual net credit fill
        })
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
            # No greeks — use SPY spot to find OTM calls within 30 pts above ATM
            spy_price = self.feeds.get_spy_price()
            otm_calls = calls[(calls['strike'] >= spy_price) & (calls['strike'] <= spy_price + 30)]
            if otm_calls.empty:
                return [], 0.0
            # Pick strike closest to delta_target * spy_price above spot
            target_strike = spy_price * (1 + delta_target * 0.025)  # ~0.20 delta ≈ 0.5% OTM for SPY 0DTE
            idx = (otm_calls['strike'] - target_strike).abs().idxmin()
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

    def _check_exits(self, ms: dict, now: datetime, quotes: Dict[str, dict]) -> None:
        remaining = []
        for pos in self.positions:
            val    = self._current_value(pos, quotes)
            reason = self._exit_reason(pos, val, now)
            if reason:
                if not self._close_position(pos, reason, val, now):
                    remaining.append(pos)  # close not confirmed — keep tracking, retry next loop
            else:
                remaining.append(pos)
        self.positions = remaining
        self._save_positions()
        # At 15:58 sweep Tradier for any positions still open (catches crash-restart gaps).
        if (now.hour, now.minute) >= (15, 58) and not self._sweep_done:
            self._tradier_force_exit_sweep(now)
            self._sweep_done = True

    def _current_value(self, pos: Position, quotes: Dict[str, dict]) -> float:
        """Debit-to-close for spreads; current mid for long (earnings) positions.
        Leg quotes come from the per-loop batched fetch (R1) — no extra calls."""
        try:
            total = 0.0
            for leg in pos.legs:
                q = quotes.get(leg.option_symbol)
                if not q:
                    log.warning('%s: no quote for %s in batch — freezing value at entry credit.',
                                pos.strategy, leg.option_symbol)
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

    def _close_position(self, pos: Position, reason: str, exit_val: float, now: datetime) -> bool:
        """Close all legs and book P&L. Returns False if any close order was
        rejected or fill could not be verified — caller keeps the position and
        retries on the next loop."""
        close_orders: List[Tuple[Leg, str]] = []
        rejected:     List[Leg] = []
        for leg in pos.legs:
            close_side = 'buy_to_close' if leg.side == 'sell_to_open' else 'sell_to_close'
            order_id = self._submit_order(leg.option_symbol, close_side, pos.contracts)
            if not order_id:
                log.error('%s: close order rejected for %s %s — retrying once.',
                          pos.strategy, close_side, leg.option_symbol)
                time.sleep(2)
                order_id = self._submit_order(leg.option_symbol, close_side, pos.contracts)
            if order_id:
                close_orders.append((leg, order_id))
            else:
                rejected.append(leg)

        if rejected:
            syms = [l.option_symbol for l in rejected]
            log.critical('%s: CLOSE FAILED for %s (reason=%s) — position still open, will retry.',
                         pos.strategy, syms, reason)
            tg_send(
                f'🚨 HERMES CLOSE FAILED: {pos.strategy} {", ".join(syms)} '
                f'(reason={reason}) — leg(s) still open, retrying next loop.'
            )
            return False

        # BUG4/C5: verify the closes by polling each close order's own ID until
        # filled — not a fixed sleep + positions diff, which both raced the
        # sandbox's 10-30s position lag and was ambiguous for strikes shared
        # with another strategy.
        if PROD_KEY and PROD_ACCOUNT:
            states = self._await_orders([oid for _, oid in close_orders])
            unconfirmed = [
                (leg.option_symbol, states.get(oid, {}).get('status') or 'unknown')
                for leg, oid in close_orders
                if states.get(oid, {}).get('status') != 'filled'
            ]
            if unconfirmed:
                log.critical('%s: close fill verification failed — %s — will retry.',
                             pos.strategy, unconfirmed)
                tg_send(
                    f'🚨 HERMES CLOSE UNVERIFIED: {pos.strategy} '
                    f'{", ".join(f"{s} ({st})" for s, st in unconfirmed)} '
                    f'after close orders — retrying next loop.'
                )
                return False
            # Book P&L off the broker's actual close fills when every leg
            # reports one; otherwise keep the quote-derived estimate.
            fills: Dict[str, float] = {}
            for leg, oid in close_orders:
                try:
                    px = float(states.get(oid, {}).get('avg_fill_price') or 0.0)
                except (TypeError, ValueError):
                    px = 0.0
                if px > 0:
                    fills[leg.option_symbol] = px
            if len(fills) == len(pos.legs):
                if pos.spread_type == 'earnings':
                    actual_exit = fills[pos.legs[0].option_symbol]
                else:
                    actual_exit = round(sum(
                        fills[leg.option_symbol] if leg.side == 'sell_to_open'
                        else -fills[leg.option_symbol]
                        for leg in pos.legs), 2)
                log.info('%s: actual close fills — exit value $%.2f (estimated $%.2f).',
                         pos.strategy, actual_exit, exit_val)
                exit_val = actual_exit
            else:
                log.warning('%s: avg_fill_price missing for %d/%d close legs — using quote-derived exit $%.2f.',
                            pos.strategy, len(pos.legs) - len(fills), len(pos.legs), exit_val)

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
            # FIX 4: mark close records explicitly. The entry path writes a
            # {'status': 'open'} stub; without a matching 'closed' marker here the
            # close record had no status field at all, so Hermes (filtering on
            # status=='closed') saw 0 closed trades and misreported P&L. exit_reason,
            # pnl, exit_time and hold_minutes below complete the spec'd close record.
            'status':                    'closed',
            'mode':                      'LIVE',                                    # Item 7
            'account':                   PROD_ACCOUNT,                              # Item 7
            'entry_order_id':            pos.entry_state.get('entry_order_id'),     # Item 7
            'close_order_ids':           [oid for _, oid in close_orders],          # Item 7
            'contracts':                 pos.contracts,
            'entry_time':                pos.entry_time.isoformat(),
            'entry_price':               pos.entry_credit,
            'real_fill_price':           exit_val,                                  # Item 7: actual close fill
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
            'real_pnl':                  pnl,   # Item 7: P&L from real broker fills
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
        return True

    # ── Tradier sandbox ───────────────────────────────────────────────────────

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel a still-working order. Returns True if Tradier accepted the cancel."""
        if order_id == SIM_ORDER_ID:
            return True
        try:
            r = requests.delete(
                f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/orders/{order_id}',
                headers=self._sb_hdrs,
                timeout=10,
            )
            if r.ok:
                log.info('Order %s cancel accepted.', order_id)
                return True
            log.warning('Order %s cancel failed %d: %s', order_id, r.status_code, r.text[:100])
            return False
        except Exception as exc:
            log.warning('Order %s cancel error: %s', order_id, exc)
            return False

    def _rollback_submitted(self, strategy: str, submitted: List[Tuple[Leg, str]],
                            contracts: int) -> None:
        """BUG2: when any leg of a spread is rejected, flatten exactly the legs
        that FILLED — whichever side they are. The old path closed previously
        *accepted* legs without checking fills, so a sell_to_open rejection
        after the buy leg filled left the long leg untouched (naked long
        accumulation), and closing a merely-accepted-but-unfilled leg would
        itself open a fresh unintended position. Polls each submitted order ID,
        cancels anything still working, and closes only confirmed fills."""
        if not submitted:
            return
        states = self._await_orders([oid for _, oid in submitted])
        filled:  List[Leg] = []
        for leg, oid in submitted:
            status = states.get(oid, {}).get('status')
            if status == 'filled':
                filled.append(leg)
            elif status not in ORDER_TERMINAL_STATUSES:
                # Still working at timeout — cancel so it cannot fill after we
                # walk away and recreate the naked-leg problem.
                if not self._cancel_order(oid):
                    log.error('%s: ROLLBACK — could not cancel working order %s (%s) — '
                              'it may still fill. MANUAL CHECK REQUIRED.',
                              strategy, oid, leg.option_symbol)
                    tg_send(f'🚨 HERMES {strategy}: order {oid} ({leg.option_symbol}) '
                            f'uncancelable during rollback — may fill unattended, check manually!')
        self._rollback_legs(strategy, filled, contracts)

    def _rollback_legs(self, strategy: str, filled_legs: List[Leg], contracts: int) -> None:
        """Close any legs that filled when another leg in the same spread was
        rejected. Each close uses the side matching the filled leg
        (sell_to_open → buy_to_close, buy_to_open → sell_to_close) and is
        verified by order-ID polling — an unverified rollback is a naked leg."""
        closes: List[Tuple[Leg, str]] = []
        for leg in filled_legs:
            close_side = 'buy_to_close' if leg.side == 'sell_to_open' else 'sell_to_close'
            log.warning('%s: ROLLBACK — submitting %s %s to close orphaned leg.',
                        strategy, close_side, leg.option_symbol)
            order_id = self._submit_order(leg.option_symbol, close_side, contracts)
            if order_id:
                closes.append((leg, order_id))
            else:
                log.error('%s: ROLLBACK FAILED for %s — MANUAL CLOSE REQUIRED.',
                          strategy, leg.option_symbol)
                tg_send(
                    f'🚨 HERMES ROLLBACK FAILED: {strategy} {leg.option_symbol} '
                    f'naked leg open — manual close required!'
                )
        if closes and PROD_KEY and PROD_ACCOUNT:
            states = self._await_orders([oid for _, oid in closes])
            for leg, oid in closes:
                status = states.get(oid, {}).get('status')
                if status != 'filled':
                    log.error('%s: ROLLBACK close %s for %s did not fill (status=%s) — '
                              'MANUAL CLOSE REQUIRED.', strategy, oid, leg.option_symbol, status)
                    tg_send(
                        f'🚨 HERMES ROLLBACK UNVERIFIED: {strategy} {leg.option_symbol} '
                        f'close order {oid} status={status} — leg may still be open, check manually!'
                    )
        if filled_legs:
            # FIX 3: always record the rollback to the log file; rate-limit the
            # Telegram alert to once per ROLLBACK_ALERT_COOLDOWN per strategy so a
            # broker rejecting a leg every ~90s does not produce dozens of pings.
            log.warning('%s: leg rejection — %d leg(s) rolled back.', strategy, len(filled_legs))
            now_mono = time.monotonic()
            if now_mono - self.last_rollback_alert.get(strategy, 0.0) > ROLLBACK_ALERT_COOLDOWN:
                tg_send(f'⚠️ HERMES {strategy}: leg rejection — {len(filled_legs)} leg(s) rolled back.')
                self.last_rollback_alert[strategy] = now_mono
            else:
                log.info('%s: rollback Telegram alert suppressed (cooldown active).', strategy)

    def _submit_order(self, option_symbol: str, side: str, qty: int) -> Optional[str]:
        """Submit a market order. Returns the Tradier order ID on acceptance
        (SIM_ORDER_ID when running without sandbox creds), None on rejection.
        Acceptance is NOT a fill — callers must verify via _await_orders (C5)."""
        if not PROD_KEY or not PROD_ACCOUNT:
            log.info('Tradier creds not set — simulating %s %s.', side, option_symbol)
            return SIM_ORDER_ID
        try:
            underlying = parse_occ(option_symbol)[0]
        except ValueError:
            log.error('Cannot parse OCC symbol %r — refusing to submit order.', option_symbol)
            return None
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
                f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/orders',
                data=data,
                headers=self._sb_hdrs,
                timeout=10,
            )
            if r.ok:
                order_id = (r.json().get('order') or {}).get('id')
                if order_id is None:
                    log.error('Tradier order accepted but no order ID in response: %s', r.text[:200])
                    return None
                log.info('Tradier order accepted: %s %s (order_id=%s)', side, option_symbol, order_id)
                return str(order_id)
            log.error('Tradier order failed %d: %s', r.status_code, r.text[:200])
            return None
        except Exception as exc:
            log.error('Tradier order error: %s', exc)
            return None

    def _submit_multileg_order(self, underlying: str, legs: List[Leg],
                               order_type: str, price: float, qty: int) -> Optional[str]:
        """Item 5: submit all legs of a spread as ONE Tradier multileg combo order
        (class=multileg). The combo fills atomically or not at all, so a partial
        fill can never leave a naked short leg. *order_type* is 'credit' for the
        credit spreads this engine trades; *price* is the net-credit limit (the
        minimum credit to accept). Returns the order ID on acceptance (SIM_ORDER_ID
        without creds), None on rejection. Acceptance is NOT a fill — callers must
        verify via _await_orders, exactly like _submit_order."""
        if not PROD_KEY or not PROD_ACCOUNT:
            log.info('Tradier creds not set — simulating multileg %s order (%d legs).',
                     order_type, len(legs))
            return SIM_ORDER_ID
        data = {
            'class':    'multileg',
            'symbol':   underlying,
            'type':     order_type,
            'duration': 'day',
            'price':    f'{abs(price):.2f}',
        }
        for i, leg in enumerate(legs):
            data[f'option_symbol[{i}]'] = leg.option_symbol
            data[f'side[{i}]']          = leg.side
            data[f'quantity[{i}]']      = str(qty)
        try:
            r = requests.post(
                f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/orders',
                data=data,
                headers=self._sb_hdrs,
                timeout=10,
            )
            if r.ok:
                order_id = (r.json().get('order') or {}).get('id')
                if order_id is None:
                    log.error('Tradier multileg order accepted but no order ID: %s', r.text[:200])
                    return None
                log.info('Tradier multileg %s order accepted: %s @ %s (order_id=%s) legs=%s',
                         order_type, underlying, data['price'], order_id,
                         [(l.side, l.option_symbol) for l in legs])
                return str(order_id)
            log.error('Tradier multileg order failed %d: %s', r.status_code, r.text[:300])
            return None
        except Exception as exc:
            log.error('Tradier multileg order error: %s', exc)
            return None

    @staticmethod
    def _extract_leg_fills(order: dict) -> Dict[str, float]:
        """Map option_symbol → avg_fill_price for each leg of a (multileg) order
        status dict. Tradier nests the per-leg fills under order['leg']. Legs with
        a missing or unparseable price are omitted, so callers can detect an
        incomplete fill report by comparing len(result) to the leg count."""
        fills: Dict[str, float] = {}
        legs = order.get('leg')
        if isinstance(legs, dict):
            legs = [legs]
        if not isinstance(legs, list):
            return fills
        for lg in legs:
            sym = lg.get('option_symbol') or lg.get('symbol')
            try:
                px = float(lg.get('avg_fill_price') or 0.0)
            except (TypeError, ValueError):
                px = 0.0
            if sym and px > 0:
                fills[sym] = px
        return fills

    def _get_order(self, order_id: str) -> dict:
        """Fetch one order's current state from Tradier. Returns {} on any
        error. Simulated orders report as immediately filled."""
        if order_id == SIM_ORDER_ID:
            return {'status': 'filled', 'avg_fill_price': 0.0}
        try:
            r = requests.get(
                f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/orders/{order_id}',
                headers=self._sb_hdrs,
                timeout=10,
            )
            if not r.ok:
                log.warning('Order %s status fetch failed %d: %s', order_id, r.status_code, r.text[:100])
                return {}
            return r.json().get('order') or {}
        except Exception as exc:
            log.warning('Order %s status fetch error: %s', order_id, exc)
            return {}

    def _await_orders(self, order_ids: List[str]) -> Dict[str, dict]:
        """C5: order-ID based fill tracking. Poll every order each
        ORDER_POLL_INTERVAL seconds until all reach a terminal status or
        ORDER_FILL_TIMEOUT elapses. Returns {order_id: last seen order dict};
        an order still non-terminal at timeout keeps its last (possibly empty)
        state and must be treated as unfilled by callers."""
        deadline = time.monotonic() + ORDER_FILL_TIMEOUT
        state: Dict[str, dict] = {oid: {} for oid in order_ids}
        while True:
            for oid in order_ids:
                if state[oid].get('status') in ORDER_TERMINAL_STATUSES:
                    continue
                state[oid] = self._get_order(oid) or state[oid]
            if all(s.get('status') in ORDER_TERMINAL_STATUSES for s in state.values()):
                return state
            if time.monotonic() >= deadline:
                pending = [oid for oid, s in state.items()
                           if s.get('status') not in ORDER_TERMINAL_STATUSES]
                log.warning('Order polling timed out after %.0fs — still non-terminal: %s',
                            ORDER_FILL_TIMEOUT, pending)
                return state
            time.sleep(ORDER_POLL_INTERVAL)

    def _fetch_tradier_positions_full(self) -> Dict[str, int]:
        """Returns {symbol: quantity} for all open Tradier positions."""
        if not PROD_KEY or not PROD_ACCOUNT:
            return {}
        try:
            r = requests.get(
                f'{PROD_BASE}/accounts/{PROD_ACCOUNT}/positions',
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

    @staticmethod
    def _atomic_write_json(path: Path, obj) -> None:
        """Write JSON via tmp file + os.replace so a crash mid-write can never
        leave a truncated file (which would crash-loop the engine on reload)."""
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(obj, indent=2, default=str))
        os.replace(tmp, path)

    def _save_positions(self) -> None:
        try:
            self._atomic_write_json(
                POSITIONS_FILE, [self._pos_to_dict(p) for p in self.positions]
            )
        except Exception as exc:
            log.error('Failed to save positions: %s', exc)

    def _load_positions(self) -> None:
        # FIX 2: loading tracked positions and rebuilding today's state from the
        # trade log are INDEPENDENT. The early returns this method used to start
        # with (`if not POSITIONS_FILE.exists(): return` and `if not saved: return`)
        # skipped the trade-log scan below — so whenever positions.json was absent
        # or empty (every position already closed/expired, or it was never written),
        # entered_today and the daily P&L counters were NOT rebuilt, and a strategy
        # that already fired today could re-enter after a restart. Load positions
        # only when the file has content, but ALWAYS fall through to the rebuild.
        if POSITIONS_FILE.exists():
            try:
                saved = json.loads(POSITIONS_FILE.read_text() or '[]')
                if saved:
                    if PROD_KEY and PROD_ACCOUNT:
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
                    # Rebuild daily loss counters from closed trades so a
                    # restart mid-day does not re-arm the daily stops from $0.
                    pnl = t.get('pnl')
                    if strat and pnl is not None:
                        self.daily_pnl[strat] = self.daily_pnl.get(strat, 0.0) + float(pnl)
                        self.total_pnl += float(pnl)
                log.info('Startup: restored entered_today=%s from today trade log.', self.entered_today)
                if self.daily_pnl:
                    log.info('Startup: restored daily P&L from trade log: total=%.2f per-strategy=%s',
                             self.total_pnl, self.daily_pnl)
            except Exception as exc:
                log.error('Failed to restore entered_today from today trade log: %s', exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log_trade(self, trade: dict) -> None:
        # Item 7: guarantee EVERY trade record — entry stub or close — carries the
        # live-trading provenance fields, regardless of which call site built it.
        # The entry and close paths already set these explicitly; setdefault only
        # backfills a field a caller left out, so no record is ever written
        # without them. mode/account are constant for the live account; order_id
        # falls back to the entry combo's order ID (so close records, which carry
        # entry_order_id/close_order_ids, still expose a top-level order_id); the
        # real fill price is left null when a caller hasn't recorded one.
        trade.setdefault('mode', 'LIVE')
        trade.setdefault('account', os.getenv('TRADIER_ACCOUNT_ID'))
        trade.setdefault('order_id', trade.get('entry_order_id'))
        trade.setdefault('real_fill_price', None)
        path   = TRADES_DIR / f'{date.today().isoformat()}.json'
        trades = json.loads(path.read_text()) if path.exists() else []
        trades.append(trade)
        self._atomic_write_json(path, trades)

    def _reset_daily(self) -> None:
        self.today           = date.today()
        self.daily_pnl       = {}
        self.total_pnl       = 0.0
        self.entered         = {}
        self.entered_today   = set()
        self._sweep_done          = False
        self._daily_limit_alerted = False
        self._contango_today      = None
        self._contango_checked_at = 0.0
        log.info('Daily reset: %s', self.today)
        # Item 6: re-run pre-flight at the start of each new trading day.
        self._preflight()

    def _strategy_already_open(self, name: str, open_syms: set) -> bool:
        """Check a shared snapshot of live Tradier positions (fetched once per
        loop by the caller — R1) to see if this strategy already has open legs today."""
        try:
            today_str = date.today().strftime('%y%m%d')  # YYMMDD as in OCC symbol
            for sym in open_syms:
                try:
                    expiry_yymmdd = parse_occ(sym)[1]
                except ValueError:
                    continue  # not an option (e.g. assigned shares)
                if expiry_yymmdd == today_str:
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
        today = now.date()
        if today in MARKET_HOLIDAYS_2026:
            log.info('Market holiday — engine idle')
            return False
        close = (13, 0) if today in EARLY_CLOSE_2026 else (16, 0)
        return (9, 30) <= (now.hour, now.minute) <= close and now.weekday() < 5

    def _days_to_fomc(self, today: date) -> int:
        future = sorted(d for d in FOMC_DATES if d >= today)
        return (future[0] - today).days if future else 999

    def _is_fomc_week(self, d: date) -> bool:
        """FIX 6: skip_fomc_weeks must skip the ENTIRE Mon–Fri week that contains
        an FOMC meeting, matching R3D's backtest. The old check only matched exact
        meeting dates in FOMC_DATES, so on a week whose meeting falls Tue–Wed (e.g.
        June 16–17, 2026) R3D's Friday entry (June 19) leaked through. FOMC_DATES
        already lists June 15/16/17, 2026; this widens the gate to the whole week.
        Returns True if any weekday of d's week is an FOMC meeting day."""
        monday = d - timedelta(days=d.weekday())
        week   = {monday + timedelta(days=i) for i in range(5)}  # Mon–Fri
        return bool(week & FOMC_DATES)


if __name__ == '__main__':
    HermesEngine().run()
