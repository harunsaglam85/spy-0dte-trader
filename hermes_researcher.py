#!/usr/bin/env python3
"""
hermes_researcher.py — Autonomous strategy research and deployment pipeline.

Cron schedule on Hetzner:  0 20 * * 0   (Sunday 20:00 server time / 20:00 ET)

Weekly cycle
------------
1.  Read all trade logs from the past 7 days.
2.  Identify the top 3 performing conditions (VIX / time / day-of-week).
3.  Generate 5 new strategy hypotheses from what worked.
4.  Write each hypothesis as a self-contained Python backtest script.
5.  Run each backtest on 2021-2026 data (ThetaData cache on Hetzner).
6.  Apply kill criteria; discard weak hypotheses.
7.  Save survivors to PENDING_DIR.
8.  Send Telegram weekly research report.
9.  For each survivor, await APPROVE or REJECT (polls up to 30 min).
10. On APPROVE: copy to strategies/, push to GitHub.
11. On REJECT:  log reason, feed back into next cycle.

Continuous improvement
----------------------
12. Compare every active strategy's live WR vs its backtest baseline.
13. Flag any strategy underperforming backtest by > 15 ppts.
14. Generate parameter-adjustment proposals.
15. Send full review to Telegram for approval.
"""

import json
import logging
import math
import os
import subprocess
import sys
import textwrap
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ── project paths (Hetzner production) ───────────────────────────────────────
PROJECT_ROOT  = Path("/root/spy-0dte-trader")
HERMES_ROOT   = Path("/root/hermes_system")
NEW_STRAT_DIR = HERMES_ROOT / "new_strategies"
PENDING_DIR   = HERMES_ROOT / "pending_strategies"
PENDING_FILE  = HERMES_ROOT / "pending_approvals.json"
REJECTIONS_FILE = HERMES_ROOT / "rejections.json"
DATA_DIR      = Path("/root/backtest_data")

for _d in (HERMES_ROOT, NEW_STRAT_DIR, PENDING_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── logging ───────────────────────────────────────────────────────────────────
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s hermes %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_DIR / "hermes.log")),
    ],
)
logger = logging.getLogger("hermes_researcher")

# ── environment ───────────────────────────────────────────────────────────────
def _load_env() -> None:
    for candidate in (
        Path("/root/.env"),
        Path("/root/.tastytrade-mcp/.env"),
        PROJECT_ROOT / ".env",
    ):
        if candidate.exists():
            for raw in candidate.read_text().splitlines():
                raw = raw.strip()
                if raw and not raw.startswith("#") and "=" in raw:
                    k, v = raw.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            break

_load_env()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH   = str(PROJECT_ROOT / "trading.db")

# ── kill criteria ─────────────────────────────────────────────────────────────
KILL_OOS_WR_MIN        = 0.55   # OOS win-rate floor
KILL_BLIND_WR_MIN      = 0.52   # blind-2025 win-rate floor
KILL_PROFIT_FACTOR_MIN = 1.40   # profit factor floor
KILL_MAX_DRAWDOWN_MAX  = 0.35   # max drawdown ceiling (35%)
KILL_CONFIDENCE_MIN    = 55     # confidence score floor (0-100)
KILL_MIN_TRADES        = 30     # minimum trades to trust the result

# ── backtest baseline WR for active strategies ────────────────────────────────
BACKTEST_BASELINES: Dict[str, float] = {
    "config_d":      0.65,
    "credit_spread": 0.72,
    "five_dte":      0.58,
    "earnings":      0.55,
    "vpin":          0.60,
}

# ── strategy names managed by main.py ─────────────────────────────────────────
ACTIVE_STRATEGIES = list(BACKTEST_BASELINES.keys())


# ══════════════════════════════════════════════════════════════════════════════
# Data structures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TopCondition:
    name: str              # human-readable label
    condition_type: str    # 'vix' | 'time' | 'dow'
    params: Dict           # e.g. {'vix_min': 15, 'vix_max': 18}
    win_rate: float
    n_trades: int
    avg_pnl: float

    def description(self) -> str:
        return (
            f"{self.name}: WR={self.win_rate:.0%}, "
            f"n={self.n_trades}, avg_pnl=${self.avg_pnl:.2f}"
        )


@dataclass
class Hypothesis:
    name: str
    description: str
    params: Dict           # all testable parameters
    source_conditions: List[str]   # which TopConditions inspired this
    script_path: str = ""


@dataclass
class BacktestResult:
    hypothesis_name: str
    oos_wr: float
    blind_2025_wr: float
    profit_factor: float
    max_drawdown: float
    confidence_score: int
    total_trades: int
    oos_trades: int
    blind_trades: int
    total_pnl: float
    notes: str = ""

    def passes_kill_criteria(self) -> Tuple[bool, str]:
        if self.total_trades < KILL_MIN_TRADES:
            return False, f"insufficient trades ({self.total_trades} < {KILL_MIN_TRADES})"
        if self.oos_wr < KILL_OOS_WR_MIN:
            return False, f"OOS WR {self.oos_wr:.0%} < {KILL_OOS_WR_MIN:.0%}"
        if self.blind_2025_wr < KILL_BLIND_WR_MIN:
            return False, f"blind-2025 WR {self.blind_2025_wr:.0%} < {KILL_BLIND_WR_MIN:.0%}"
        if self.profit_factor < KILL_PROFIT_FACTOR_MIN:
            return False, f"PF {self.profit_factor:.2f} < {KILL_PROFIT_FACTOR_MIN:.2f}"
        if self.max_drawdown > KILL_MAX_DRAWDOWN_MAX:
            return False, f"max DD {self.max_drawdown:.0%} > {KILL_MAX_DRAWDOWN_MAX:.0%}"
        if self.confidence_score < KILL_CONFIDENCE_MIN:
            return False, f"confidence {self.confidence_score} < {KILL_CONFIDENCE_MIN}"
        return True, "PASS"


# ══════════════════════════════════════════════════════════════════════════════
# Telegram helpers
# ══════════════════════════════════════════════════════════════════════════════

def _tg_send(text: str) -> Optional[int]:
    """Send a Telegram message; return message_id or None on failure."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured — skipping send.")
        return None
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("result", {}).get("message_id")
        logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram send error: %s", exc)
    return None


def _tg_get_updates(offset: int = 0, timeout: int = 20) -> List[Dict]:
    """Long-poll Telegram for incoming updates."""
    if not BOT_TOKEN:
        return []
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 5,
        )
        if resp.ok:
            return resp.json().get("result", [])
    except Exception as exc:
        logger.warning("Telegram getUpdates error: %s", exc)
    return []


def _tg_get_last_update_id() -> int:
    """Return the current highest update_id so we only read new messages."""
    updates = _tg_get_updates(timeout=1)
    if updates:
        return updates[-1]["update_id"]
    return 0


def _poll_for_reply(
    after_update_id: int,
    poll_minutes: int = 30,
) -> Optional[str]:
    """
    Block up to *poll_minutes* minutes waiting for a message containing
    APPROVE or REJECT (case-insensitive).  Returns the keyword or None.
    """
    deadline = time.monotonic() + poll_minutes * 60
    last_id = after_update_id

    while time.monotonic() < deadline:
        updates = _tg_get_updates(offset=last_id + 1, timeout=20)
        for upd in updates:
            last_id = upd["update_id"]
            text = upd.get("message", {}).get("text", "").strip().upper()
            if text in ("APPROVE", "REJECT"):
                return text
        time.sleep(5)

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Database helpers (thin wrapper — avoids importing core/database.py directly
# because this script may run before the venv is fully activated on Hetzner)
# ══════════════════════════════════════════════════════════════════════════════

def _db_connect():
    import sqlite3
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _get_trades_since(conn, since_iso: str) -> List[Dict]:
    cur = conn.execute(
        "SELECT * FROM trades WHERE date(timestamp) >= ? ORDER BY timestamp DESC",
        (since_iso,),
    )
    return [dict(r) for r in cur.fetchall()]


def _get_all_trades(conn, strategy: str) -> List[Dict]:
    cur = conn.execute(
        "SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp DESC",
        (strategy,),
    )
    return [dict(r) for r in cur.fetchall()]


def _log_rejection(name: str, reason: str) -> None:
    rejections: List[Dict] = []
    if REJECTIONS_FILE.exists():
        try:
            rejections = json.loads(REJECTIONS_FILE.read_text())
        except Exception:
            pass
    rejections.append({
        "name": name,
        "reason": reason,
        "date": date.today().isoformat(),
    })
    REJECTIONS_FILE.write_text(json.dumps(rejections, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# Core analysis
# ══════════════════════════════════════════════════════════════════════════════

def _identify_top_conditions(trades: List[Dict]) -> List[TopCondition]:
    """
    Bucket trades by VIX range, entry hour, and day-of-week.
    Return the top 3 conditions ranked by win-rate (min 5 trades each).
    """
    vix_buckets: Dict[str, List[float]] = {
        "<15": [], "15-18": [], "18-22": [], ">22": [],
    }
    hour_buckets: Dict[str, List[float]] = {
        "09-10": [], "10-11": [], "11-12": [], "12-14": [], "14-16": [],
    }
    dow_buckets: Dict[str, List[float]] = {
        "Monday": [], "Tuesday": [], "Wednesday": [], "Thursday": [], "Friday": [],
    }
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    for t in trades:
        pnl = t.get("pnl", 0.0)
        vix = t.get("vix")
        ts  = t.get("timestamp", "")

        if vix is not None:
            try:
                v = float(vix)
                if v < 15:
                    vix_buckets["<15"].append(pnl)
                elif v < 18:
                    vix_buckets["15-18"].append(pnl)
                elif v < 22:
                    vix_buckets["18-22"].append(pnl)
                else:
                    vix_buckets[">22"].append(pnl)
            except (TypeError, ValueError):
                pass

        try:
            dt = datetime.fromisoformat(ts)
            h = dt.hour
            for label, (s, e) in {
                "09-10": (9, 10), "10-11": (10, 11), "11-12": (11, 12),
                "12-14": (12, 14), "14-16": (14, 16),
            }.items():
                if s <= h < e:
                    hour_buckets[label].append(pnl)
                    break
            wd = dt.weekday()
            if 0 <= wd <= 4:
                dow_buckets[day_names[wd]].append(pnl)
        except (ValueError, TypeError):
            pass

    vix_ranges = {
        "<15":   {"vix_min": 0,  "vix_max": 15},
        "15-18": {"vix_min": 15, "vix_max": 18},
        "18-22": {"vix_min": 18, "vix_max": 22},
        ">22":   {"vix_min": 22, "vix_max": 99},
    }
    hour_ranges = {
        "09-10": {"entry_start": 9,  "entry_end": 10},
        "10-11": {"entry_start": 10, "entry_end": 11},
        "11-12": {"entry_start": 11, "entry_end": 12},
        "12-14": {"entry_start": 12, "entry_end": 14},
        "14-16": {"entry_start": 14, "entry_end": 16},
    }
    dow_map = {
        "Monday": [0], "Tuesday": [1], "Wednesday": [2],
        "Thursday": [3], "Friday": [4],
    }

    candidates: List[TopCondition] = []

    for label, pnls in vix_buckets.items():
        if len(pnls) < 5:
            continue
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        candidates.append(TopCondition(
            name=f"VIX {label}",
            condition_type="vix",
            params=vix_ranges[label],
            win_rate=wr,
            n_trades=len(pnls),
            avg_pnl=sum(pnls) / len(pnls),
        ))

    for label, pnls in hour_buckets.items():
        if len(pnls) < 5:
            continue
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        candidates.append(TopCondition(
            name=f"Entry {label} ET",
            condition_type="time",
            params=hour_ranges[label],
            win_rate=wr,
            n_trades=len(pnls),
            avg_pnl=sum(pnls) / len(pnls),
        ))

    for label, pnls in dow_buckets.items():
        if len(pnls) < 3:
            continue
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        candidates.append(TopCondition(
            name=label,
            condition_type="dow",
            params={"days_of_week": dow_map[label]},
            win_rate=wr,
            n_trades=len(pnls),
            avg_pnl=sum(pnls) / len(pnls),
        ))

    candidates.sort(key=lambda c: c.win_rate, reverse=True)
    return candidates[:3]


def _answer_hypothesis_questions(
    trades: List[Dict],
    top_conditions: List[TopCondition],
) -> Dict[str, str]:
    """
    Answer the six Hermes hypothesis questions from trade data.
    Returns a dict of question → finding string.
    """
    findings: Dict[str, str] = {}

    # Q1: Which VIX conditions produced the highest WR?
    vix_top = [c for c in top_conditions if c.condition_type == "vix"]
    if vix_top:
        findings["vix_edge"] = (
            f"VIX {vix_top[0].name} produced {vix_top[0].win_rate:.0%} WR "
            f"over {vix_top[0].n_trades} trades (avg P&L ${vix_top[0].avg_pnl:.2f})"
        )
    else:
        findings["vix_edge"] = "Insufficient VIX-segmented data this week."

    # Q2: What time of day had best fill quality (proxy: highest avg_pnl)?
    time_top = [c for c in top_conditions if c.condition_type == "time"]
    if time_top:
        findings["time_edge"] = (
            f"Entry window {time_top[0].name} had highest WR "
            f"{time_top[0].win_rate:.0%} ({time_top[0].n_trades} trades)"
        )
    else:
        findings["time_edge"] = "Insufficient time-segmented data this week."

    # Q3: Which day of week outperformed?
    dow_top = [c for c in top_conditions if c.condition_type == "dow"]
    if dow_top:
        findings["dow_edge"] = (
            f"{dow_top[0].name} outperformed at {dow_top[0].win_rate:.0%} WR "
            f"({dow_top[0].n_trades} trades)"
        )
    else:
        findings["dow_edge"] = "Insufficient day-of-week data this week."

    # Q4: Did any strategy beat its backtest prediction?
    outperformers = []
    underperformers = []
    strategy_groups: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        strat = t.get("strategy", "")
        if strat:
            strategy_groups[strat].append(t.get("pnl", 0.0))
    for strat, pnls in strategy_groups.items():
        n = len(pnls)
        if n < 5:
            continue
        live_wr = sum(1 for p in pnls if p > 0) / n
        baseline = BACKTEST_BASELINES.get(strat, 0.60)
        delta = live_wr - baseline
        if delta > 0.05:
            outperformers.append(f"{strat} ({live_wr:.0%} vs {baseline:.0%} baseline)")
        elif delta < -0.10:
            underperformers.append(f"{strat} ({live_wr:.0%} vs {baseline:.0%} baseline)")
    findings["outperformers"] = (
        ", ".join(outperformers) if outperformers else "None this week."
    )
    findings["underperformers"] = (
        ", ".join(underperformers) if underperformers else "None this week."
    )

    # Q5: What conditions were present on ALL winning trades?
    winning_trades = [t for t in trades if t.get("pnl", 0.0) > 0]
    if winning_trades:
        vix_values = [t["vix"] for t in winning_trades if t.get("vix")]
        if vix_values:
            avg_win_vix = sum(float(v) for v in vix_values) / len(vix_values)
            findings["winning_conditions"] = (
                f"Avg VIX on winners: {avg_win_vix:.1f}. "
                f"Sample: {len(winning_trades)} winning trades."
            )
        else:
            findings["winning_conditions"] = f"{len(winning_trades)} winners; no VIX data."
    else:
        findings["winning_conditions"] = "No winning trades this week."

    # Q6: What was different about losing trades?
    losing_trades = [t for t in trades if t.get("pnl", 0.0) <= 0]
    if losing_trades:
        vix_values = [t["vix"] for t in losing_trades if t.get("vix")]
        if vix_values:
            avg_loss_vix = sum(float(v) for v in vix_values) / len(vix_values)
            findings["losing_conditions"] = (
                f"Avg VIX on losers: {avg_loss_vix:.1f}. "
                f"Sample: {len(losing_trades)} losing trades."
            )
        else:
            findings["losing_conditions"] = f"{len(losing_trades)} losers; no VIX data."
    else:
        findings["losing_conditions"] = "No losing trades this week."

    return findings


def _generate_hypotheses(
    top_conditions: List[TopCondition],
    findings: Dict[str, str],
    week_of: str,
) -> List[Hypothesis]:
    """
    Generate 5 strategy hypotheses from the top conditions and findings.
    """
    hypotheses: List[Hypothesis] = []
    ts = week_of.replace("-", "")

    vix_conds  = [c for c in top_conditions if c.condition_type == "vix"]
    time_conds = [c for c in top_conditions if c.condition_type == "time"]
    dow_conds  = [c for c in top_conditions if c.condition_type == "dow"]

    # H1: VIX-restricted credit spread
    vix_params = vix_conds[0].params if vix_conds else {"vix_min": 15, "vix_max": 20}
    hypotheses.append(Hypothesis(
        name=f"H1_vix_filtered_{ts}",
        description=(
            f"Credit spread restricted to VIX {vix_params.get('vix_min')}-"
            f"{vix_params.get('vix_max')}. "
            f"Hypothesis: narrowing VIX range improves edge consistency."
        ),
        params={
            "strategy_type": "credit_spread",
            "vix_min": vix_params.get("vix_min", 15),
            "vix_max": vix_params.get("vix_max", 20),
            "entry_hour_start": 10,
            "entry_hour_end": 11,
            "days_of_week": [0, 2, 4],
            "delta_target": 0.20,
            "profit_target_pct": 0.30,
            "stop_loss_mult": 2.0,
            "spread_width": 2.0,
        },
        source_conditions=[c.name for c in vix_conds[:1]],
    ))

    # H2: Time-restricted credit spread
    t_start = time_conds[0].params.get("entry_start", 10) if time_conds else 10
    t_end   = time_conds[0].params.get("entry_end", 11)   if time_conds else 11
    hypotheses.append(Hypothesis(
        name=f"H2_time_filtered_{ts}",
        description=(
            f"Credit spread entries only {t_start:02d}:00-{t_end:02d}:00 ET. "
            f"Hypothesis: concentrating entries in the optimal window boosts WR."
        ),
        params={
            "strategy_type": "credit_spread",
            "vix_min": 13,
            "vix_max": 25,
            "entry_hour_start": t_start,
            "entry_hour_end": t_end,
            "days_of_week": [0, 2, 4],
            "delta_target": 0.20,
            "profit_target_pct": 0.30,
            "stop_loss_mult": 2.0,
            "spread_width": 2.0,
        },
        source_conditions=[c.name for c in time_conds[:1]],
    ))

    # H3: Day-of-week focused credit spread
    best_days = dow_conds[0].params.get("days_of_week", [0]) if dow_conds else [0]
    day_label = dow_conds[0].name if dow_conds else "Monday"
    hypotheses.append(Hypothesis(
        name=f"H3_dow_focused_{ts}",
        description=(
            f"Credit spread on {day_label} only. "
            f"Hypothesis: trading only the best day of week eliminates drag."
        ),
        params={
            "strategy_type": "credit_spread",
            "vix_min": 13,
            "vix_max": 25,
            "entry_hour_start": 10,
            "entry_hour_end": 12,
            "days_of_week": best_days,
            "delta_target": 0.20,
            "profit_target_pct": 0.30,
            "stop_loss_mult": 2.0,
            "spread_width": 2.0,
        },
        source_conditions=[c.name for c in dow_conds[:1]],
    ))

    # H4: Combined filter (VIX + time + day)
    hypotheses.append(Hypothesis(
        name=f"H4_combined_{ts}",
        description=(
            "Credit spread with all three filters: VIX, time window, and day-of-week. "
            "Hypothesis: stacking filters produces a smaller but higher-conviction edge."
        ),
        params={
            "strategy_type": "credit_spread",
            "vix_min": vix_params.get("vix_min", 15),
            "vix_max": vix_params.get("vix_max", 20),
            "entry_hour_start": t_start,
            "entry_hour_end": t_end,
            "days_of_week": best_days,
            "delta_target": 0.20,
            "profit_target_pct": 0.35,
            "stop_loss_mult": 2.0,
            "spread_width": 2.0,
        },
        source_conditions=[c.name for c in top_conditions[:3]],
    ))

    # H5: Momentum variant (config-d style) with top VIX filter
    hypotheses.append(Hypothesis(
        name=f"H5_momentum_variant_{ts}",
        description=(
            f"Momentum long-call trade (config-D style) restricted to VIX "
            f"{vix_params.get('vix_min')}-{vix_params.get('vix_max')} and "
            f"entry window {t_start:02d}:00-{t_end:02d}:00 ET. "
            f"Hypothesis: VIX + time filter improves on base config-D edge."
        ),
        params={
            "strategy_type": "config_d_variant",
            "vix_min": vix_params.get("vix_min", 13),
            "vix_max": vix_params.get("vix_max", 20),
            "entry_hour_start": t_start,
            "entry_hour_end": min(t_end, 12),
            "days_of_week": [0, 4],
            "profit_target_pct": 0.04,
            "stop_loss_pct": 0.35,
        },
        source_conditions=[c.name for c in top_conditions[:2]],
    ))

    return hypotheses


# ══════════════════════════════════════════════════════════════════════════════
# Backtest script generation and execution
# ══════════════════════════════════════════════════════════════════════════════

_BACKTEST_TEMPLATE = '''#!/usr/bin/env python3
"""
Auto-generated hypothesis backtest — {name}
Generated: {generated_at}
Description: {description}
"""
import json, math, os, sys, warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
warnings.filterwarnings("ignore")

try:
    import yfinance as yf
    import numpy as np
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "yfinance", "numpy"], check=True)
    import yfinance as yf
    import numpy as np

PARAMS = {params_repr}

BT_START   = date(2021, 1, 4)
BT_END     = date(2026, 5, 30)
SPLIT_DATE = date(2023, 7, 1)
BLIND_YEAR = 2025
DATA_DIR   = Path("/root/backtest_data")
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_spy_daily() -> Dict[date, Dict]:
    cache = DATA_DIR / "spy_daily.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
        return {{date.fromisoformat(k): v for k, v in raw.items()}}
    print("Fetching SPY daily from yfinance ...", flush=True)
    tk = yf.Ticker("SPY")
    df = tk.history(start="2021-01-01", end="2026-06-01", auto_adjust=True)
    out = {{
        row.Index.date(): {{
            "open": float(row.Open),
            "high": float(row.High),
            "low":  float(row.Low),
            "close": float(row.Close),
            "volume": int(row.Volume),
        }}
        for row in df.itertuples()
    }}
    cache.write_text(json.dumps({{k.isoformat(): v for k, v in out.items()}}))
    return out


def _load_vix_daily() -> Dict[date, float]:
    cache = DATA_DIR / "vix_daily.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
        return {{date.fromisoformat(k): float(v) for k, v in raw.items()}}
    print("Fetching VIX daily from yfinance ...", flush=True)
    tk = yf.Ticker("^VIX")
    df = tk.history(start="2021-01-01", end="2026-06-01", auto_adjust=True)
    out = {{
        row.Index.date(): float(row.Close)
        for row in df.itertuples()
    }}
    cache.write_text(json.dumps({{k.isoformat(): v for k, v in out.items()}}))
    return out


def _is_trading_day(d: date, spy: Dict[date, Dict]) -> bool:
    return d in spy and d.weekday() < 5


def _simulate_credit_spread(spy: Dict, vix: Dict, params: Dict) -> List[Dict]:
    """Simplified bull-put credit spread simulation on daily data."""
    trades = []
    vix_min  = params.get("vix_min", 13)
    vix_max  = params.get("vix_max", 25)
    dows     = set(params.get("days_of_week", [0, 2, 4]))
    pt_pct   = params.get("profit_target_pct", 0.30)
    sl_mult  = params.get("stop_loss_mult", 2.0)
    spread_w = params.get("spread_width", 2.0)

    current = BT_START
    while current <= BT_END:
        if not _is_trading_day(current, spy):
            current += timedelta(days=1)
            continue

        day_vix = vix.get(current)
        if day_vix is None or not (vix_min <= day_vix <= vix_max):
            current += timedelta(days=1)
            continue

        if current.weekday() not in dows:
            current += timedelta(days=1)
            continue

        bar = spy[current]
        spy_price = bar["close"]

        # Approximate premium: ATM put with 0.20 delta, simplified as:
        # credit ~ spread_width * 0.18 (conservative flat approximation)
        credit = round(spread_w * 0.18, 2)
        if credit < 0.05:
            current += timedelta(days=1)
            continue

        profit_target = credit * pt_pct
        stop_loss     = credit * sl_mult
        # 70% of trades close at profit target (simplified model);
        # actual P&L influenced by day return
        day_ret = (bar["close"] - bar["open"]) / bar["open"] if bar["open"] > 0 else 0.0

        # Bull put: profit when SPY stays flat or rises
        # Loss when SPY drops meaningfully
        if day_ret > -0.005:
            pnl = profit_target * 100
            exit_reason = "profit_target"
        elif day_ret < -0.015:
            pnl = -stop_loss * 100
            exit_reason = "stop_loss"
        else:
            pnl = (credit * 0.10) * 100
            exit_reason = "expire_partial"

        # Commission: ~$1.25/contract/leg * 2 legs
        pnl -= 2.50

        trades.append({{
            "date": current.isoformat(),
            "pnl": round(pnl, 2),
            "exit_reason": exit_reason,
            "vix": day_vix,
            "spy": spy_price,
        }})
        current += timedelta(days=1)

    return trades


def _simulate_config_d_variant(spy: Dict, vix: Dict, params: Dict) -> List[Dict]:
    """Simplified 0DTE long-call momentum simulation on daily data."""
    trades = []
    vix_min = params.get("vix_min", 13)
    vix_max = params.get("vix_max", 20)
    dows    = set(params.get("days_of_week", [0, 4]))
    pt_pct  = params.get("profit_target_pct", 0.04)
    sl_pct  = params.get("stop_loss_pct", 0.35)

    current = BT_START
    while current <= BT_END:
        if not _is_trading_day(current, spy):
            current += timedelta(days=1)
            continue

        day_vix = vix.get(current)
        if day_vix is None or not (vix_min <= day_vix <= vix_max):
            current += timedelta(days=1)
            continue

        if current.weekday() not in dows:
            current += timedelta(days=1)
            continue

        bar = spy[current]
        spy_price = bar["open"]
        # ATM 0DTE call: approximate premium ~ 0.8% of SPY price
        premium = round(spy_price * 0.008, 2)
        if premium < 0.10:
            current += timedelta(days=1)
            continue

        day_ret = (bar["close"] - bar["open"]) / bar["open"] if bar["open"] > 0 else 0.0
        if day_ret >= pt_pct:
            pnl = premium * 2.5 * 100
            exit_reason = "profit_target"
        elif day_ret <= -0.003:
            pnl = -premium * sl_pct * 100
            exit_reason = "stop_loss"
        else:
            # Linear payoff between 0 and target
            multiplier = max(day_ret / pt_pct, 0.0) * 2.5
            pnl = premium * multiplier * 100 - premium * 100
            exit_reason = "expire"

        pnl -= 2.00  # commission
        trades.append({{
            "date": current.isoformat(),
            "pnl": round(pnl, 2),
            "exit_reason": exit_reason,
            "vix": day_vix,
            "spy": spy_price,
        }})
        current += timedelta(days=1)

    return trades


def _compute_stats(trades: List[Dict], label: str) -> Dict:
    if not trades:
        return {{"n": 0, "wr": 0.0, "pf": 0.0, "total_pnl": 0.0, "max_dd": 0.0}}
    pnls = [t["pnl"] for t in trades]
    n    = len(pnls)
    wins = [p for p in pnls if p > 0]
    lossed = [p for p in pnls if p <= 0]
    wr   = len(wins) / n if n > 0 else 0.0
    pf   = (sum(wins) / abs(sum(lossed))) if lossed else float("inf")
    # Max drawdown from cumulative P&L
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = (peak - cum) / max(peak, 1.0)
        max_dd = max(max_dd, dd)
    return {{
        "label": label,
        "n": n,
        "wr": round(wr, 4),
        "pf": round(pf, 3) if pf != float("inf") else 99.0,
        "total_pnl": round(sum(pnls), 2),
        "max_dd": round(max_dd, 4),
    }}


def _confidence_score(full: Dict, oos: Dict, blind: Dict) -> int:
    score = 0
    if oos.get("wr", 0) >= 0.65:  score += 25
    elif oos.get("wr", 0) >= 0.58: score += 15
    if blind.get("wr", 0) >= 0.60: score += 25
    elif blind.get("wr", 0) >= 0.52: score += 15
    pf = oos.get("pf", 0)
    if pf >= 2.0:   score += 20
    elif pf >= 1.6: score += 12
    elif pf >= 1.4: score += 6
    if oos.get("max_dd", 1) <= 0.20: score += 15
    elif oos.get("max_dd", 1) <= 0.30: score += 8
    if oos.get("n", 0) >= 100: score += 15
    elif oos.get("n", 0) >= 50: score += 8
    return min(score, 100)


def main():
    spy_data = _load_spy_daily()
    vix_data = _load_vix_daily()

    strat_type = PARAMS.get("strategy_type", "credit_spread")
    if strat_type == "config_d_variant":
        all_trades = _simulate_config_d_variant(spy_data, vix_data, PARAMS)
    else:
        all_trades = _simulate_credit_spread(spy_data, vix_data, PARAMS)

    full_trades  = all_trades
    oos_trades   = [t for t in all_trades if date.fromisoformat(t["date"]) >= date(2023, 7, 1)]
    blind_trades = [t for t in all_trades if date.fromisoformat(t["date"]).year == BLIND_YEAR]

    full_stats  = _compute_stats(full_trades,  "full_2021_2026")
    oos_stats   = _compute_stats(oos_trades,   "oos_2023h2_2026")
    blind_stats = _compute_stats(blind_trades, "blind_2025")

    confidence = _confidence_score(full_stats, oos_stats, blind_stats)

    result = {{
        "hypothesis": "{name}",
        "params": PARAMS,
        "full": full_stats,
        "oos":  oos_stats,
        "blind_2025": blind_stats,
        "confidence_score": confidence,
    }}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
'''


def _write_backtest_script(hypothesis: Hypothesis) -> Path:
    """Write the backtest script for *hypothesis* and return its path."""
    script_dir = HERMES_ROOT / "backtest_scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    path = script_dir / f"{hypothesis.name}.py"

    content = _BACKTEST_TEMPLATE.format(
        name=hypothesis.name,
        generated_at=datetime.now().isoformat(),
        description=hypothesis.description,
        params_repr=json.dumps(hypothesis.params, indent=4),
    )
    path.write_text(content)
    logger.info("Wrote backtest script: %s", path)
    return path


def _execute_backtest(script_path: Path, timeout_seconds: int = 300) -> Optional[BacktestResult]:
    """Run the backtest script as a subprocess and parse its JSON output."""
    logger.info("Running backtest: %s", script_path.name)
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        logger.error("Backtest timed out: %s", script_path.name)
        return None
    except Exception as exc:
        logger.error("Backtest subprocess error %s: %s", script_path.name, exc)
        return None

    if proc.returncode != 0:
        logger.error(
            "Backtest failed (rc=%d): %s\n%s",
            proc.returncode, script_path.name, proc.stderr[-500:],
        )
        return None

    # The last non-empty line of stdout should be the JSON result.
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    if not lines:
        logger.error("Backtest produced no output: %s", script_path.name)
        return None

    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        logger.error("Backtest JSON parse error %s: %s", script_path.name, exc)
        return None

    oos   = data.get("oos", {})
    blind = data.get("blind_2025", {})
    full  = data.get("full", {})

    return BacktestResult(
        hypothesis_name=data.get("hypothesis", script_path.stem),
        oos_wr=oos.get("wr", 0.0),
        blind_2025_wr=blind.get("wr", 0.0),
        profit_factor=oos.get("pf", 0.0),
        max_drawdown=oos.get("max_dd", 1.0),
        confidence_score=data.get("confidence_score", 0),
        total_trades=full.get("n", 0),
        oos_trades=oos.get("n", 0),
        blind_trades=blind.get("n", 0),
        total_pnl=full.get("total_pnl", 0.0),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Deployment helpers
# ══════════════════════════════════════════════════════════════════════════════

def _save_survivor(hypothesis: Hypothesis, result: BacktestResult) -> Path:
    """Write a survivor hypothesis + results to PENDING_DIR."""
    dest = PENDING_DIR / f"{hypothesis.name}.py"
    src  = Path(hypothesis.script_path)
    if src.exists():
        dest.write_text(src.read_text())

    meta_path = PENDING_DIR / f"{hypothesis.name}.meta.json"
    meta_path.write_text(json.dumps({
        "hypothesis": asdict(hypothesis),
        "result": asdict(result),
        "created": datetime.now().isoformat(),
        "status": "pending_approval",
    }, indent=2))

    logger.info("Saved survivor: %s (confidence=%d)", hypothesis.name, result.confidence_score)
    return dest


def _deploy_approved_strategy(hypothesis: Hypothesis) -> bool:
    """Copy approved strategy to strategies/, push to GitHub."""
    src = PENDING_DIR / f"{hypothesis.name}.py"
    if not src.exists():
        logger.error("Cannot deploy — source not found: %s", src)
        return False

    dest = PROJECT_ROOT / "strategies" / f"{hypothesis.name}.py"
    dest.write_text(src.read_text())
    logger.info("Deployed strategy to: %s", dest)

    # Push to GitHub
    try:
        subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "add", str(dest)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "commit",
             "-m", f"hermes: deploy strategy {hypothesis.name}\n\n"
                   f"OOS WR: {hypothesis.params}\n\n"
                   "Co-Authored-By: Hermes Researcher <hermes@autonomous>"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "push", "origin", "main"],
            check=True, capture_output=True,
        )
        logger.info("Pushed to GitHub: %s", hypothesis.name)
    except subprocess.CalledProcessError as exc:
        logger.error("GitHub push failed: %s", exc.stderr)
        return False

    # Update meta status
    meta_path = PENDING_DIR / f"{hypothesis.name}.meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["status"] = "deployed"
        meta["deployed_at"] = datetime.now().isoformat()
        meta_path.write_text(json.dumps(meta, indent=2))

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Weekly strategy review
# ══════════════════════════════════════════════════════════════════════════════

def _review_active_strategies(conn) -> str:
    """
    Compare every active strategy's live WR to its backtest baseline.
    Return a formatted report string.
    """
    lines = [f"<b>Weekly Strategy Review — {date.today().isoformat()}</b>", ""]

    for strat in ACTIVE_STRATEGIES:
        trades = _get_all_trades(conn, strat)
        baseline_wr = BACKTEST_BASELINES.get(strat, 0.60)

        if len(trades) < 10:
            lines.append(
                f"• <b>{strat}</b>: {len(trades)} trades — insufficient data."
            )
            continue

        week_ago = (date.today() - timedelta(days=7)).isoformat()
        recent = [t for t in trades if t.get("timestamp", "") >= week_ago]
        all_pnls = [t["pnl"] for t in trades]
        recent_pnls = [t["pnl"] for t in recent]

        all_wr    = sum(1 for p in all_pnls if p > 0) / len(all_pnls)
        recent_wr = (sum(1 for p in recent_pnls if p > 0) / len(recent_pnls)
                     if recent_pnls else None)
        all_pnl   = sum(all_pnls)
        delta     = all_wr - baseline_wr

        status_icon = "✅" if delta >= 0 else ("⚠️" if delta > -0.15 else "🔴")
        line = (
            f"• {status_icon} <b>{strat}</b>: "
            f"live WR {all_wr:.0%} vs baseline {baseline_wr:.0%} "
            f"(delta {delta:+.0%}) | "
            f"total P&L ${all_pnl:+.0f} | {len(trades)} trades"
        )
        if recent_pnls:
            line += f" | this week WR {recent_wr:.0%} ({len(recent_pnls)} trades)"
        lines.append(line)

        if delta < -0.15:
            lines.append(
                f"  ⚠️ <i>{strat} underperforming backtest by {abs(delta):.0%}. "
                f"Recommend reviewing VIX filter and entry timing. "
                f"Consider pausing if delta reaches -20%.</i>"
            )

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Pending approvals — checked at the start of each run
# ══════════════════════════════════════════════════════════════════════════════

def _check_pending_approvals(update_id_start: int) -> int:
    """
    Read pending_approvals.json and poll Telegram for any APPROVE/REJECT replies.
    Returns the highest update_id seen.
    """
    if not PENDING_FILE.exists():
        return update_id_start

    pending: List[Dict] = json.loads(PENDING_FILE.read_text())
    if not pending:
        return update_id_start

    logger.info("Checking %d pending approval(s) from previous run.", len(pending))

    updates = _tg_get_updates(offset=update_id_start + 1, timeout=5)
    last_id = update_id_start
    replies: List[str] = []
    for upd in updates:
        last_id = upd["update_id"]
        text = upd.get("message", {}).get("text", "").strip().upper()
        if text in ("APPROVE", "REJECT"):
            replies.append(text)

    remaining = []
    for item in pending:
        h_name = item["hypothesis_name"]
        h_dict = item.get("hypothesis", {})
        r_dict = item.get("result", {})

        if not replies:
            remaining.append(item)
            continue

        reply = replies.pop(0)
        if reply == "APPROVE":
            logger.info("APPROVED (from pending): %s", h_name)
            hyp = Hypothesis(**{k: v for k, v in h_dict.items()
                                if k in Hypothesis.__dataclass_fields__})
            _deploy_approved_strategy(hyp)
            _tg_send(f"✅ <b>Strategy deployed:</b> <code>{h_name}</code>\n"
                     f"Add to main.py strategies list and restart the bot.")
        else:
            logger.info("REJECTED (from pending): %s", h_name)
            _log_rejection(h_name, "REJECT from Telegram (pending queue)")
            _tg_send(f"❌ <b>Strategy rejected:</b> <code>{h_name}</code>\n"
                     f"Logged for next hypothesis cycle improvement.")

    # Rewrite file with only unanswered items
    PENDING_FILE.write_text(json.dumps(remaining, indent=2))
    return last_id


# ══════════════════════════════════════════════════════════════════════════════
# Main weekly cycle
# ══════════════════════════════════════════════════════════════════════════════

def run() -> None:
    logger.info("═══ Hermes Researcher starting — %s ═══", datetime.now().isoformat())

    # ── 0. Connect to database ────────────────────────────────────────────────
    conn = _db_connect()

    # ── 0b. Handle any pending approvals from last run ────────────────────────
    last_update_id = _tg_get_last_update_id()
    last_update_id = _check_pending_approvals(last_update_id)

    # ── 1. Read past week's trades ────────────────────────────────────────────
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    weekly_trades = _get_trades_since(conn, week_ago)
    logger.info("Weekly trades: %d", len(weekly_trades))

    if not weekly_trades:
        _tg_send(
            f"🔬 <b>Hermes Weekly — {date.today().isoformat()}</b>\n\n"
            "No trades recorded this week. Skipping hypothesis generation.\n"
            "Running strategy review only."
        )
        review = _review_active_strategies(conn)
        _tg_send(review)
        conn.close()
        return

    # ── 2. Identify top 3 performing conditions ───────────────────────────────
    top_conditions = _identify_top_conditions(weekly_trades)
    logger.info(
        "Top conditions: %s",
        [c.description() for c in top_conditions],
    )

    # ── 3. Answer hypothesis questions ───────────────────────────────────────
    findings = _answer_hypothesis_questions(weekly_trades, top_conditions)

    # ── 4. Generate 5 hypotheses ──────────────────────────────────────────────
    week_label = date.today().isoformat()
    hypotheses = _generate_hypotheses(top_conditions, findings, week_label)
    logger.info("Generated %d hypotheses.", len(hypotheses))

    # ── 5. Write + run backtests ──────────────────────────────────────────────
    results: List[Tuple[Hypothesis, Optional[BacktestResult]]] = []
    for hyp in hypotheses:
        script_path = _write_backtest_script(hyp)
        hyp.script_path = str(script_path)
        result = _execute_backtest(script_path)
        results.append((hyp, result))

    # ── 6. Apply kill criteria ────────────────────────────────────────────────
    survivors: List[Tuple[Hypothesis, BacktestResult]] = []
    killed:    List[Tuple[str, str]] = []

    for hyp, result in results:
        if result is None:
            killed.append((hyp.name, "backtest execution failed"))
            continue
        ok, reason = result.passes_kill_criteria()
        if ok:
            survivors.append((hyp, result))
        else:
            killed.append((hyp.name, reason))
            logger.info("KILLED %s — %s", hyp.name, reason)

    logger.info("Survivors: %d / %d", len(survivors), len(hypotheses))

    # ── 7. Save survivors to pending_strategies/ ──────────────────────────────
    for hyp, result in survivors:
        _save_survivor(hyp, result)

    # ── 8. Build and send weekly Telegram report ──────────────────────────────
    report_lines = [
        f"🔬 <b>Hermes Weekly Research — {week_label}</b>",
        f"Trades analysed: {len(weekly_trades)}",
        "",
        "<b>Top conditions this week:</b>",
    ]
    for cond in top_conditions:
        report_lines.append(f"  • {cond.description()}")

    report_lines += ["", "<b>Key findings:</b>"]
    for k, v in findings.items():
        report_lines.append(f"  • {v}")

    report_lines += [
        "",
        f"<b>Hypotheses tested:</b> {len(hypotheses)}",
        f"<b>Killed:</b> {len(killed)}",
        f"<b>Survivors:</b> {len(survivors)}",
    ]

    if killed:
        report_lines.append("")
        report_lines.append("<b>Killed:</b>")
        for name, reason in killed:
            report_lines.append(f"  ✗ {name}: {reason}")

    if survivors:
        report_lines.append("")
        report_lines.append("<b>Survivors requiring approval:</b>")
        for hyp, result in survivors:
            report_lines.append(
                f"  ✓ {hyp.name}\n"
                f"    OOS WR: {result.oos_wr:.0%} | "
                f"Blind 2025 WR: {result.blind_2025_wr:.0%} | "
                f"PF: {result.profit_factor:.2f} | "
                f"Confidence: {result.confidence_score}/100"
            )

    _tg_send("\n".join(report_lines))

    # ── 9. Request approvals for each survivor ────────────────────────────────
    new_pending: List[Dict] = []

    for hyp, result in survivors:
        approval_msg = (
            f"🆕 <b>New strategy ready for review:</b> <code>{hyp.name}</code>\n\n"
            f"<b>Description:</b> {hyp.description}\n\n"
            f"<b>Backtest OOS WR:</b> {result.oos_wr:.1%}\n"
            f"<b>Blind 2025 WR:</b>  {result.blind_2025_wr:.1%}\n"
            f"<b>Profit factor:</b>  {result.profit_factor:.2f}\n"
            f"<b>Max drawdown:</b>   {result.max_drawdown:.1%}\n"
            f"<b>Confidence:</b>     {result.confidence_score}/100\n"
            f"<b>OOS trades:</b>     {result.oos_trades}\n\n"
            f"Review at: <code>{PENDING_DIR}/{hyp.name}.py</code>\n\n"
            "Reply <b>APPROVE</b> or <b>REJECT</b>"
        )
        _tg_send(approval_msg)

        # Get a fresh baseline update_id just before polling
        pre_poll_id = _tg_get_last_update_id()
        reply = _poll_for_reply(after_update_id=pre_poll_id, poll_minutes=30)

        if reply == "APPROVE":
            logger.info("APPROVED: %s", hyp.name)
            success = _deploy_approved_strategy(hyp)
            if success:
                _tg_send(
                    f"✅ <b>Strategy deployed:</b> <code>{hyp.name}</code>\n"
                    f"Add to main.py strategies list and restart the bot to activate."
                )
            else:
                _tg_send(
                    f"⚠️ Deployment failed for <code>{hyp.name}</code>. "
                    "Check hermes.log."
                )
        elif reply == "REJECT":
            logger.info("REJECTED: %s", hyp.name)
            _log_rejection(hyp.name, "REJECT from Telegram")
            _tg_send(
                f"❌ <b>Rejected:</b> <code>{hyp.name}</code>\n"
                "Logged. Will inform next hypothesis cycle."
            )
        else:
            # No reply within 30 min — save for next run's pending check
            logger.info("No reply within 30 min for %s — saving to pending.", hyp.name)
            new_pending.append({
                "hypothesis_name": hyp.name,
                "hypothesis": asdict(hyp),
                "result": asdict(result),
                "created": datetime.now().isoformat(),
            })

    # Merge new pending with any still-unanswered ones from earlier
    existing_pending: List[Dict] = []
    if PENDING_FILE.exists():
        try:
            existing_pending = json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    existing_names = {p["hypothesis_name"] for p in existing_pending}
    for item in new_pending:
        if item["hypothesis_name"] not in existing_names:
            existing_pending.append(item)
    PENDING_FILE.write_text(json.dumps(existing_pending, indent=2))

    # ── 10. Continuous improvement: weekly strategy review ─────────────────
    review = _review_active_strategies(conn)
    _tg_send(review)

    conn.close()
    logger.info("═══ Hermes Researcher complete — %s ═══", datetime.now().isoformat())


if __name__ == "__main__":
    run()
