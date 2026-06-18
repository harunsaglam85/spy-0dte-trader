#!/usr/bin/env python3
"""
d1_strangle_backtest.py
=======================
5-year OOS validation of discovery candidate **D1_strangle_all** on REAL
ThetaData historical option bid/ask (no Black-Scholes pricing).

Candidate (from research/replay_candidates_real.json, in-sample on 6 June-2026
days: N=6, WR=83.3%, PF=2.12, Sharpe=4.37):

    spread_type   : strangle  (sell OTM put + sell OTM call, 0DTE)
    entry         : Mon-Fri, 14:00-15:00 ET (first eligible 5-min bar)
    vix filter    : 13 <= VIX <= 22  (daily VIX)
    delta target  : 0.30  (strike SELECTION only — BS delta, real VIX/T)
    profit target : 75% of credit captured  (close when cost <= 25% credit)
    stop          : 2.0x credit  (close when cost >= 2x credit)
    force exit     : 15:45 ET

We test TWO variants:
  * NAKED strangle — exactly as discovered (unlimited tail risk).
  * PROTECTED strangle — add a long put 5 strikes below + long call 5 strikes
    above (an iron condor / iron-fly shape), capping max loss to ~ (5 - credit).

DATA PROVENANCE
  Option credit / exit / P&L  : ThetaData /v3/option/history/quote tick bid/ask
                                (REAL, cached to backtest_data/theta_quote_cache).
    entry credit = Σ short@bid − Σ long@ask
    close cost   = Σ short@ask − Σ long@bid
  Underlying SPY 5-min        : backtest_data/spy_5min_*.pkl (real)
  VIX daily                   : backtest_data/vix_*.csv (real)
  Expirations (0DTE filter)   : backtest_data/theta_SPY_expirations.pkl (real)
  Strike SELECTION only       : BS delta approx (real VIX as sigma) — never prices.

VALIDATION
  * 60/40 walk-forward split on non-2025 trades (chronological).
  * Blind 2025 holdout (never touched while forming the hypothesis).
  * Bootstrap 95% CI on mean per-trade P&L.

Run:  python3 /root/spy-0dte-trader/d1_strangle_backtest.py
"""
import sys
sys.path.insert(0, "/root/spy-0dte-trader")

import csv
import math
import pickle
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytz

# Reuse the REAL-quote machinery + strike selection from the replay engine.
# (importing is side-effect-free; replay_engine.main() is __main__-guarded)
import replay_engine as RE

BASE     = Path("/root/spy-0dte-trader")
DATA_DIR = BASE / "backtest_data"
OUT_MD   = Path("/root/hermes_system/research/d1_strangle_backtest.md")
ET       = pytz.timezone("America/New_York")

# ── Candidate config ──────────────────────────────────────────────────────────
ENTRY_START_MIN = 14 * 60          # 14:00 ET
ENTRY_END_MIN   = 15 * 60          # 15:00 ET
FORCE_EXIT_MIN  = 15 * 60 + 45     # 15:45 ET
CLOSE_MIN       = 16 * 60          # 0DTE expiry
VIX_MIN, VIX_MAX = 13.0, 22.0
DELTA_TARGET    = 0.30
PROFIT_TARGET   = 0.75             # capture 75% of credit
STOP_MULTIPLE   = 2.0
WING_WIDTH      = 5                # $ protective wing for the PROTECTED variant
MIN_CREDIT      = 0.20

BLIND_YEAR = "2025"
TRAIN_FRAC = 0.60                  # 60/40 walk-forward on non-blind trades
SEED       = 42

# ── Load real underlying / VIX / expirations ──────────────────────────────────
print("Loading underlying, VIX, expirations …")
with (DATA_DIR / "spy_5min_2021-01-04_2026-05-30.pkl").open("rb") as f:
    SPY5 = pickle.load(f)
VIX = {}
with (DATA_DIR / "vix_2021-01-04_2026-05-30.csv").open() as f:
    for r in csv.DictReader(f):
        VIX[r["date"]] = float(r["vix"])
EXPS = set(pickle.load((DATA_DIR / "theta_SPY_expirations.pkl").open("rb")))

ALL_DAYS = sorted(SPY5.keys())
ELIGIBLE = [d for d in ALL_DAYS
            if VIX_MIN <= VIX.get(d, 0) <= VIX_MAX
            and date.fromisoformat(d) in EXPS]
print(f"  {len(ELIGIBLE)} eligible days (VIX in [{VIX_MIN:.0f},{VIX_MAX:.0f}] + same-day 0DTE)")


def bar_minute(bar):
    return datetime.fromtimestamp(bar["t"] / 1000, tz=ET).timetuple().tm_hour * 60 + \
           datetime.fromtimestamp(bar["t"] / 1000, tz=ET).minute


def entry_bar(day):
    """First 5-min SPY bar at/after 14:00 ET (within the 14:00-15:00 window)."""
    for b in SPY5[day]:
        m = bar_minute(b)
        if ENTRY_START_MIN <= m < ENTRY_END_MIN:
            return b, m
    return None, None


# ── Step 1: pick strikes for every eligible day (NO network) ──────────────────
print("Selecting strikes per day (BS delta, real VIX/T) …")
PLAN = {}   # day -> dict(em, S0, vix, pk, ck, wpk, wck)
for day in ELIGIBLE:
    b, em = entry_bar(day)
    if b is None:
        continue
    S0  = b["c"]
    vix = VIX[day]
    ml  = max(CLOSE_MIN - em, 1)
    pk  = RE.strike_at_delta(DELTA_TARGET, S0, ml, vix, "p")
    ck  = RE.strike_at_delta(DELTA_TARGET, S0, ml, vix, "c")
    PLAN[day] = {"em": em, "S0": S0, "vix": vix,
                 "pk": pk, "ck": ck, "wpk": pk - WING_WIDTH, "wck": ck + WING_WIDTH}
print(f"  planned {len(PLAN)} days")

# ── Step 2: fetch every needed real option series concurrently ────────────────
needed = set()
for day, p in PLAN.items():
    exp = day.replace("-", "")
    needed.add((p["pk"], "PUT", exp))
    needed.add((p["ck"], "CALL", exp))
    needed.add((p["wpk"], "PUT", exp))
    needed.add((p["wck"], "CALL", exp))
print(f"Fetching {len(needed)} real option series from ThetaData (cached) …")

t0 = time.time()
done = [0]
def _fetch(args):
    strike, right, exp = args
    RE.get_option_series(strike, right, exp)
    done[0] += 1
    if done[0] % 200 == 0:
        print(f"  …{done[0]}/{len(needed)} fetched ({time.time()-t0:.0f}s)")

with ThreadPoolExecutor(max_workers=8) as ex:
    list(ex.map(_fetch, needed))
print(f"  fetch complete in {time.time()-t0:.0f}s")


# ── Step 3: simulate one day for a given variant ──────────────────────────────
def simulate(day, protected):
    p = PLAN[day]
    em, exp = p["em"], day.replace("-", "")
    legs = [("PUT", p["pk"], +1), ("CALL", p["ck"], +1)]
    if protected:
        legs += [("PUT", p["wpk"], -1), ("CALL", p["wck"], -1)]

    leg_series = []
    for right, strike, side in legs:
        s = RE.get_option_series(strike, right, exp)
        if s is None:
            return None
        leg_series.append((side, s))

    credit = RE._entry_credit(leg_series, em)
    if credit is None or credit < MIN_CREDIT:
        return None

    profit_at = credit * (1 - PROFIT_TARGET)
    stop_at   = credit * STOP_MULTIPLE

    minutes = sorted({m for (_, s) in leg_series for m in s["keys"]
                      if em < m <= FORCE_EXIT_MIN})
    exit_min = exit_cost = None
    reason = "force"
    for m in minutes:
        cost = RE._close_cost(leg_series, m)
        if cost is None:
            continue
        if m >= FORCE_EXIT_MIN:
            exit_min, exit_cost, reason = m, cost, "force"
            break
        if cost <= profit_at:
            exit_min, exit_cost, reason = m, cost, "target"
            break
        if cost >= stop_at:
            exit_min, exit_cost, reason = m, cost, "stop"
            break
    if exit_min is None:
        cost = RE._close_cost(leg_series, FORCE_EXIT_MIN)
        if cost is None:
            last_m = max(m for (_, s) in leg_series for m in s["keys"])
            cost, exit_min = RE._close_cost(leg_series, last_m), last_m
        else:
            exit_min = FORCE_EXIT_MIN
        exit_cost, reason = cost, "force"
    if exit_cost is None:
        return None

    pnl = round((credit - exit_cost) * 100, 2)
    return {"date": day, "pnl": pnl, "credit": round(credit, 3),
            "exit_cost": round(exit_cost, 3), "reason": reason,
            "vix": p["vix"], "yr": day[:4]}


def run_variant(protected):
    trades = []
    for day in sorted(PLAN.keys()):
        t = simulate(day, protected)
        if t:
            trades.append(t)
    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {"n": 0, "wr": 0, "pnl": 0, "pf": 0, "sharpe": 0,
                "avg_win": 0, "avg_loss": 0, "max_loss": 0, "max_dd": 0,
                "avg_credit": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), sum(loss)
    mu, sig = np.mean(pnls), (np.std(pnls, ddof=1) if len(pnls) > 1 else 0.0)
    sharpe = (mu / sig * math.sqrt(252)) if sig > 0 else 0.0
    eq = peak = mdd = 0.0
    for t in sorted(trades, key=lambda x: x["date"]):
        eq += t["pnl"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    return {
        "n": len(pnls),
        "wr": round(100 * len(wins) / len(pnls), 1),
        "pnl": round(sum(pnls), 2),
        "pf": round(abs(gw / gl), 2) if gl < 0 else float("inf"),
        "sharpe": round(sharpe, 2),
        "avg_win": round(np.mean(wins), 2) if wins else 0.0,
        "avg_loss": round(np.mean(loss), 2) if loss else 0.0,
        "max_loss": round(min(pnls), 2),
        "max_dd": round(mdd, 2),
        "avg_credit": round(np.mean([t["credit"] for t in trades]), 3),
    }


def bootstrap_ci(trades, n_boot=10000):
    """95% CI on mean per-trade P&L via resampling with replacement."""
    if len(trades) < 2:
        return (0.0, 0.0)
    pnls = np.array([t["pnl"] for t in trades])
    rng = np.random.default_rng(SEED)
    means = rng.choice(pnls, size=(n_boot, len(pnls)), replace=True).mean(axis=1)
    return (round(float(np.percentile(means, 2.5)), 2),
            round(float(np.percentile(means, 97.5)), 2))


def split_trades(trades):
    """Blind 2025 held out; remaining split 60/40 chronologically."""
    blind = [t for t in trades if t["yr"] == BLIND_YEAR]
    rest  = sorted([t for t in trades if t["yr"] != BLIND_YEAR], key=lambda x: x["date"])
    cut   = int(len(rest) * TRAIN_FRAC)
    return rest[:cut], rest[cut:], blind   # train(IS), test(OOS), blind


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ── Run both variants ─────────────────────────────────────────────────────────
print("\nSimulating NAKED strangle …")
naked = run_variant(protected=False)
print(f"  {len(naked)} trades")
print("Simulating PROTECTED strangle ($5 wings) …")
prot = run_variant(protected=True)
print(f"  {len(prot)} trades")

random.seed(SEED)
variants = [("Naked strangle", naked), ("Protected strangle ($5 wings)", prot)]

# ── Report ────────────────────────────────────────────────────────────────────
L = []
P = L.append
P("# D1 Strangle Candidate — 5-Year OOS Validation (REAL ThetaData bid/ask)")
P("")
P(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} • "
  f"{RE._FETCH_COUNT} live ThetaData fetches this run._")
P("")
P("## Candidate under test — `D1_strangle_all`")
P("")
P("Discovered in-sample on 6 June-2026 days (N=6, WR=83.3%, PF=2.12, Sharpe=4.37). "
  "This report is the **out-of-sample** check on 5 years of real option quotes.")
P("")
P("| Param | Value |")
P("|---|---|")
P("| Structure | strangle — sell OTM put + sell OTM call, 0DTE |")
P("| Entry | Mon–Fri, first 5-min bar in 14:00–15:00 ET |")
P("| VIX filter | 13 ≤ VIX ≤ 22 (daily) |")
P("| Delta target | 0.30 (strike selection only) |")
P("| Profit target | 75% of credit |")
P("| Stop | 2.0× credit |")
P("| Force exit | 15:45 ET |")
P(f"| Protective wing (variant 2) | long put −${WING_WIDTH}, long call +${WING_WIDTH} |")
P("")
P("**Provenance:** every credit / exit / P&L is real ThetaData "
  "`/v3/option/history/quote` tick bid/ask (entry = short@bid − long@ask; "
  "exit = short@ask − long@bid), scanned at 1-minute resolution with a hard "
  "15:45 ET force-exit. The only model is which $1 strike a 0.30 delta maps to "
  "(BS delta, real VIX). No Black-Scholes anywhere in the P&L.")
P("")
P(f"**Universe:** {len(ELIGIBLE)} eligible days 2021–2026 "
  f"(VIX in [{VIX_MIN:.0f},{VIX_MAX:.0f}] with a same-day 0DTE expiration). "
  "Note: pre-2022 only Mon/Wed/Fri had same-day expirations, so early years are "
  "lighter; 2022 is thin because VIX sat above 22 for most of the year.")
P("")

# Headline table
P("## Headline — full sample (2021–2026)")
P("")
P("| Variant | N | WR% | Total P&L | PF | Sharpe | Avg credit | Max loss | Max DD |")
P("|---|---|---|---|---|---|---|---|---|")
for name, tr in variants:
    s = stats(tr)
    P(f"| {name} | {s['n']} | {s['wr']} | ${s['pnl']:+,.0f} | {fmt_pf(s['pf'])} | "
      f"{s['sharpe']} | ${s['avg_credit']:.2f} | ${s['max_loss']:+,.0f} | ${s['max_dd']:,.0f} |")
P("")

# Walk-forward
P("## Walk-forward 60/40 split + blind 2025 holdout")
P("")
P("Train (IS) = earliest 60% of non-2025 trades; Test (OOS) = latest 40% of "
  "non-2025 trades; Blind = all of 2025 (never used to form the hypothesis).")
P("")
for name, tr in variants:
    train, test, blind = split_trades(tr)
    P(f"### {name}")
    P("")
    P("| Segment | N | WR% | P&L | PF | Sharpe | Max DD |")
    P("|---|---|---|---|---|---|---|")
    for seg, st in [("Train (IS, 60%)", train), ("Test (OOS, 40%)", test),
                    ("Blind 2025", blind)]:
        s = stats(st)
        P(f"| {seg} | {s['n']} | {s['wr']} | ${s['pnl']:+,.0f} | {fmt_pf(s['pf'])} | "
          f"{s['sharpe']} | ${s['max_dd']:,.0f} |")
    lo, hi = bootstrap_ci(tr)
    s = stats(tr)
    mean_pnl = round(s["pnl"] / s["n"], 2) if s["n"] else 0
    P("")
    P(f"Bootstrap 95% CI on mean per-trade P&L (full sample): "
      f"**${lo:+.2f} … ${hi:+.2f}** (point est ${mean_pnl:+.2f}). "
      f"{'CI excludes 0 → edge is significant.' if lo > 0 else 'CI straddles 0 → edge NOT significant.'}")
    P("")

# Year-by-year
P("## Year-by-year")
P("")
for name, tr in variants:
    P(f"### {name}")
    P("")
    P("| Year | N | WR% | P&L | PF | Max loss |")
    P("|---|---|---|---|---|---|")
    by_yr = defaultdict(list)
    for t in tr:
        by_yr[t["yr"]].append(t)
    for yr in sorted(by_yr):
        s = stats(by_yr[yr])
        P(f"| {yr} | {s['n']} | {s['wr']} | ${s['pnl']:+,.0f} | {fmt_pf(s['pf'])} | "
          f"${s['max_loss']:+,.0f} |")
    P("")

# Exit reason breakdown
P("## Exit-reason mix")
P("")
P("| Variant | target | stop | force | mean P&L/trade |")
P("|---|---|---|---|---|")
for name, tr in variants:
    rc = defaultdict(int)
    for t in tr:
        rc[t["reason"]] += 1
    s = stats(tr)
    mean_pnl = round(s["pnl"] / s["n"], 2) if s["n"] else 0
    P(f"| {name} | {rc['target']} | {rc['stop']} | {rc['force']} | ${mean_pnl:+.2f} |")
P("")

# Verdict
ns, ps = stats(naked), stats(prot)
n_train, n_test, n_blind = split_trades(naked)
p_train, p_test, p_blind = split_trades(prot)
nlo, nhi = bootstrap_ci(naked)
plo, phi = bootstrap_ci(prot)
P("## Verdict")
P("")
P("Compared with the 6-day in-sample discovery (WR 83.3%, PF 2.12, Sharpe 4.37):")
P("")
P(f"- **Naked:** full-sample WR {ns['wr']}%, PF {fmt_pf(ns['pf'])}, Sharpe "
  f"{ns['sharpe']}; OOS WR {stats(n_test)['wr']}%, blind-2025 WR {stats(n_blind)['wr']}%. "
  f"Max single loss ${ns['max_loss']:+,.0f}, max DD ${ns['max_dd']:,.0f}. "
  f"Bootstrap mean/trade CI ${nlo:+.2f}…${nhi:+.2f}.")
P(f"- **Protected:** full-sample WR {ps['wr']}%, PF {fmt_pf(ps['pf'])}, Sharpe "
  f"{ps['sharpe']}; OOS WR {stats(p_test)['wr']}%, blind-2025 WR {stats(p_blind)['wr']}%. "
  f"Max single loss ${ps['max_loss']:+,.0f}, max DD ${ps['max_dd']:,.0f}. "
  f"Bootstrap mean/trade CI ${plo:+.2f}…${phi:+.2f}.")
P("")
P("_The naked variant carries unbounded tail risk; the protected variant caps "
  f"per-trade loss to ≈ (${WING_WIDTH} − credit)×100 at the cost of a thinner "
  "credit. Compare the max-loss and max-DD columns above before promotion._")
P("")
P("> Caveat: the original discovery applied the live engine's contango (VIX3M/VIX "
  "≥ 1.05) and VWAP gates; this OOS test uses only the stated config (VIX + 0DTE "
  "+ entry window) to test those exact rules on unseen data. Strike selection is "
  "BS-delta; exits are 1-minute resolution.")
P("")

OUT_MD.write_text("\n".join(L))
print(f"\nReport written: {OUT_MD}")

# Console summary
print("\n" + "=" * 60)
for name, tr in variants:
    s = stats(tr)
    _, test, blind = split_trades(tr)
    print(f"{name}: N={s['n']} WR={s['wr']}% P&L=${s['pnl']:+,.0f} "
          f"PF={fmt_pf(s['pf'])} Sharpe={s['sharpe']} "
          f"OOS_WR={stats(test)['wr']}% Blind2025_WR={stats(blind)['wr']}% "
          f"maxDD=${s['max_dd']:,.0f} maxloss=${s['max_loss']:+,.0f}")
print("=" * 60)
