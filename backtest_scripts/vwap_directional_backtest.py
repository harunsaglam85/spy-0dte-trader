#!/usr/bin/env python3
"""
vwap_directional_backtest.py
============================
VWAP-based **directional debit spread** strategy on SPY 0DTE options, validated
on REAL ThetaData historical bid/ask (no Black-Scholes pricing).

STRATEGY
  At 09:45 ET, compare SPY to the session VWAP (built from real 1-minute bars):
    * SPY >= VWAP * (1 + 0.30%)  -> BULL CALL debit spread
        buy ATM call (delta ~0.50), sell call 2 strikes higher
    * SPY <= VWAP * (1 - 0.30%)  -> BEAR PUT debit spread
        buy ATM put (delta ~0.50),  sell put 2 strikes lower
    * within +/-0.30% of VWAP    -> NO TRADE
  Exit at the FIRST of:
    * 75% of max profit  (max profit = width - debit)
    * 100% loss          (spread value collapses to 0 -> entire debit lost)
    * 14:00 ET           (time stop)

DATA PROVENANCE — what is REAL
  Option bid/ask / entry / exit / P&L : ThetaData /v3/option/history/quote tick
                                        bid/ask (REAL), reused via replay_engine.
  SPY 1-minute bars + session VWAP    : backtest_data/spy_1min_alpaca_*.pkl (real)
  VIX daily (regime buckets)          : backtest_data/vix_2021-01-04_2025-12-31.csv
  0DTE expiration calendar            : backtest_data/theta_SPY_expirations.pkl
  Strike SELECTION only               : BS delta approx (real VIX/T) — never prices.

  NOTE ON THETADATA PORT: the task brief named localhost:25510, but the live
  terminal in this system serves the historical-quote API on localhost:25503
  (/v3/option/history/quote). We reuse the proven replay_engine quote layer on
  25503; change replay_engine.THETA_BASE if the terminal is ever moved.

COSTS & FILLS  (per brief)
  * Fill every leg at the MID of its real bid/ask.
  * Reject the trade if bid/ask spread > $0.10 on EITHER leg at entry.
  * Commission $0.65 / contract / leg (Tradier) — charged on entry AND exit
    (2 legs each way => $2.60 round-trip per 1-lot spread).

EVENT FILTER
  Skip FOMC decision days and CPI release days (lists imported from
  backtest_real_data.py — the same calendars the other strategies use).

CONFIDENCE  (same 90% framework as the other strategies)
  * Bootstrap P(total P&L > 0) via trade-level resampling (n=2000).
  * 90% bootstrap CI on mean per-trade P&L (5th..95th pct).
  * A-F grade (PF / WR / Sharpe / bootstrap), per backtest_90pct._grade.

OUTPUT
  * Full results table to stdout.
  * JSON -> /root/hermes_system/backtest_results/vwap_directional_YYYYMMDD.json
  * PROMOTE / WATCH / KILL verdict:
        PROMOTE : WR > 55%  AND PF > 1.3
        KILL    : WR < 50%  OR  PF < 1.1
        WATCH   : otherwise (WR 50-55% or PF 1.1-1.3)

Run:  python3 /root/spy-0dte-trader/backtest_scripts/vwap_directional_backtest.py
"""
import sys
sys.path.insert(0, "/root/spy-0dte-trader")

import csv
import json
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
# (importing is side-effect-free; replay_engine.main() is __main__-guarded.)
import replay_engine as RE
# FOMC / CPI calendars — reuse the exact sets the other strategies skip.
from backtest_real_data import FOMC_DATES, CPI_DATES

BASE      = Path("/root/spy-0dte-trader")
DATA_DIR  = BASE / "backtest_data"
RESULTS   = Path("/root/hermes_system/backtest_results")
RESULTS.mkdir(parents=True, exist_ok=True)
ET        = pytz.timezone("America/New_York")

# ── Strategy config ───────────────────────────────────────────────────────────
START_DATE      = "2021-01-01"
END_DATE        = "2024-12-31"
ENTRY_MIN       = 9 * 60 + 45      # 09:45 ET decision/entry
TIME_STOP_MIN   = 14 * 60          # 14:00 ET time stop
CLOSE_MIN       = 16 * 60          # 0DTE expiry (for strike-pick T only)
VWAP_BAND       = 0.003            # 0.30% dead-band around VWAP
ATM_DELTA       = 0.50             # "ATM" long leg ~0.50 delta
STRIKE_WIDTH    = 2                # sell 2 strikes away ($2 wide on SPY)
PROFIT_TARGET   = 0.75             # exit at 75% of max profit
COMMISSION_LEG  = 0.65             # $/contract/leg
N_LEGS_ROUNDTRIP = 4               # 2 legs in + 2 legs out
MAX_LEG_SPREAD  = 0.10             # reject if bid/ask spread > $0.10 on a leg
CONTRACTS       = 1
SEED            = 42

WIDTH_PTS = float(STRIKE_WIDTH)    # spread width in option points (= $ for SPY)
COMMISSION_RT = COMMISSION_LEG * N_LEGS_ROUNDTRIP * CONTRACTS

DOW_NAME = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ── Load real underlying (1-min) / VIX / expirations ───────────────────────────
print("Loading SPY 1-minute bars, VIX, expirations …")
with (DATA_DIR / "spy_1min_alpaca_2021-01-04_2026-05-30.pkl").open("rb") as f:
    SPY1 = pickle.load(f)                         # {day: [ {t,o,h,l,c,v}, ... ]}

VIX = {}
with (DATA_DIR / "vix_2021-01-04_2025-12-31.csv").open() as f:
    reader = csv.reader(f)
    header = next(reader)
    # Date is the first column (sometimes an unnamed pandas index); vix is the last.
    for row in reader:
        if len(row) < 2:
            continue
        try:
            VIX[row[0]] = float(row[-1])
        except (TypeError, ValueError):
            continue

EXPS = set(pickle.load((DATA_DIR / "theta_SPY_expirations.pkl").open("rb")))

ALL_DAYS = sorted(d for d in SPY1 if START_DATE <= d <= END_DATE)
print(f"  {len(ALL_DAYS)} SPY session days in [{START_DATE} .. {END_DATE}]")


def bar_minute(bar):
    dt = datetime.fromtimestamp(bar["t"] / 1000, tz=ET)
    return dt.hour * 60 + dt.minute


def session_vwap_at_entry(day):
    """Cumulative session VWAP (typical-price * volume) through the 09:45 bar,
    plus the SPY price at 09:45. Returns (spy_price, vwap) or (None, None)."""
    cum_pv = cum_v = 0.0
    spy_price = vwap = None
    for b in SPY1[day]:
        m = bar_minute(b)
        if m < 9 * 60 + 30:          # ignore any pre-open ticks
            continue
        typ = (b["h"] + b["l"] + b["c"]) / 3.0
        cum_pv += typ * b["v"]
        cum_v  += b["v"]
        if m >= ENTRY_MIN:
            spy_price = b["c"]
            vwap = (cum_pv / cum_v) if cum_v else b["c"]
            return spy_price, round(vwap, 4)
    return spy_price, vwap


# ── Step 1: build the trade plan offline (signal + strike pick, NO network) ────
print("Evaluating VWAP signal + selecting strikes per day (offline) …")
PLAN = {}            # day -> dict(direction, S0, vwap, dist, vix, long_k, short_k, right)
skip_reasons = defaultdict(int)

for day in ALL_DAYS:
    d = date.fromisoformat(day)
    if d in FOMC_DATES:
        skip_reasons["fomc"] += 1
        continue
    if d in CPI_DATES:
        skip_reasons["cpi"] += 1
        continue
    if d not in EXPS:                       # no same-day 0DTE expiration
        skip_reasons["no_0dte"] += 1
        continue
    vix = VIX.get(day)
    if vix is None:
        skip_reasons["no_vix"] += 1
        continue

    S0, vwap = session_vwap_at_entry(day)
    if S0 is None or vwap is None or vwap <= 0:
        skip_reasons["no_bar"] += 1
        continue

    dist = (S0 - vwap) / vwap               # signed distance from VWAP
    if abs(dist) < VWAP_BAND:
        skip_reasons["within_band"] += 1
        continue

    ml = max(CLOSE_MIN - ENTRY_MIN, 1)
    if dist >= VWAP_BAND:                    # bullish -> bull call debit spread
        long_k  = RE.strike_at_delta(ATM_DELTA, S0, ml, vix, "c")
        short_k = long_k + STRIKE_WIDTH
        right, direction = "CALL", "bull_call"
    else:                                    # bearish -> bear put debit spread
        long_k  = RE.strike_at_delta(ATM_DELTA, S0, ml, vix, "p")
        short_k = long_k - STRIKE_WIDTH
        right, direction = "PUT", "bear_put"

    PLAN[day] = {"direction": direction, "right": right, "S0": S0, "vwap": vwap,
                 "dist": dist, "vix": vix, "long_k": long_k, "short_k": short_k}

print(f"  signal fired on {len(PLAN)} days; skipped: {dict(skip_reasons)}")


# ── Step 2: fetch every needed real option series concurrently (cached) ────────
needed = set()
for day, p in PLAN.items():
    exp = day.replace("-", "")
    needed.add((p["long_k"], p["right"], exp))
    needed.add((p["short_k"], p["right"], exp))
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
print(f"  fetch complete in {time.time()-t0:.0f}s "
      f"({RE._FETCH_COUNT} live ThetaData calls this run)")


# ── Step 3: simulate one day (DEBIT spread, fills at MID) ──────────────────────
def mid_at(series, minute):
    """Mid of the real bid/ask at-or-before `minute`, plus the raw [bid,ask].
    Returns (mid, bid, ask) or (None, None, None)."""
    q = RE.quote_at(series, minute)
    if q is None:
        return None, None, None
    bid, ask = q
    return (bid + ask) / 2.0, bid, ask


def simulate(day):
    p = PLAN[day]
    exp = day.replace("-", "")
    long_s  = RE.get_option_series(p["long_k"],  p["right"], exp)
    short_s = RE.get_option_series(p["short_k"], p["right"], exp)
    if long_s is None or short_s is None:
        return None, "no_quotes"

    # Entry quotes at 09:45.
    lmid, lbid, lask = mid_at(long_s,  ENTRY_MIN)
    smid, sbid, sask = mid_at(short_s, ENTRY_MIN)
    if lmid is None or smid is None:
        return None, "no_entry_quote"

    # Reject wide markets (> $0.10) on EITHER leg.
    if (lask - lbid) > MAX_LEG_SPREAD or (sask - sbid) > MAX_LEG_SPREAD:
        return None, "wide_spread"

    debit = lmid - smid                      # net debit paid (long mid − short mid)
    if debit <= 0 or debit >= WIDTH_PTS:     # degenerate quote — skip
        return None, "bad_debit"

    max_profit = WIDTH_PTS - debit
    target_value = debit + PROFIT_TARGET * max_profit   # spread value at 75% max profit
    # 100% loss => spread value collapses to ~0.

    # Scan real per-minute quotes after entry up to the 14:00 time stop.
    minutes = sorted({m for s in (long_s, short_s) for m in s["keys"]
                      if ENTRY_MIN < m <= TIME_STOP_MIN})
    exit_min = exit_val = None
    reason = "time"
    for m in minutes:
        lm, _, _ = mid_at(long_s, m)
        sm, _, _ = mid_at(short_s, m)
        if lm is None or sm is None:
            continue
        value = lm - sm
        if m >= TIME_STOP_MIN:
            exit_min, exit_val, reason = m, value, "time"
            break
        if value >= target_value:
            exit_min, exit_val, reason = m, value, "target"
            break
        if value <= 0.0:                     # full debit lost
            exit_min, exit_val, reason = m, value, "stop"
            break
    if exit_min is None:                     # no tick reached 14:00 — flatten at last quote
        last_m = max(m for s in (long_s, short_s) for m in s["keys"])
        lm, _, _ = mid_at(long_s, min(last_m, TIME_STOP_MIN))
        sm, _, _ = mid_at(short_s, min(last_m, TIME_STOP_MIN))
        if lm is None or sm is None:
            return None, "no_exit_quote"
        exit_min, exit_val, reason = min(last_m, TIME_STOP_MIN), lm - sm, "time"

    exit_val = max(0.0, min(WIDTH_PTS, exit_val))        # clamp to [0, width]
    gross = (exit_val - debit) * 100 * CONTRACTS
    pnl = round(gross - COMMISSION_RT, 2)

    return {
        "date": day, "dow": DOW_NAME[date.fromisoformat(day).weekday()],
        "direction": p["direction"], "right": p["right"],
        "long_k": p["long_k"], "short_k": p["short_k"],
        "spy_entry": round(p["S0"], 2), "vwap": p["vwap"],
        "dist_pct": round(p["dist"] * 100, 3), "vix": round(p["vix"], 2),
        "debit": round(debit, 3), "exit_value": round(exit_val, 3),
        "max_profit": round(max_profit, 3),
        "entry_min": ENTRY_MIN, "exit_min": exit_min, "hold": exit_min - ENTRY_MIN,
        "reason": reason, "pnl": pnl, "yr": day[:4], "month": day[:7],
    }, "ok"


print("Simulating …")
trades = []
sim_skip = defaultdict(int)
for day in sorted(PLAN):
    t, why = simulate(day)
    if t:
        trades.append(t)
    else:
        sim_skip[why] += 1
print(f"  {len(trades)} trades booked; sim skips: {dict(sim_skip)}")


# ── Stats ───────────────────────────────────────────────────────────────────
def stats(ts):
    if not ts:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "pf": 0.0, "sharpe": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "max_dd": 0.0, "max_loss": 0.0}
    pnls = [t["pnl"] for t in ts]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), sum(loss)
    mu, sig = np.mean(pnls), (np.std(pnls, ddof=1) if len(pnls) > 1 else 0.0)
    sharpe = (mu / sig * math.sqrt(252)) if sig > 0 else 0.0
    eq = peak = mdd = 0.0
    for t in sorted(ts, key=lambda x: x["date"]):
        eq += t["pnl"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    return {
        "n": len(pnls),
        "wr": round(100 * len(wins) / len(pnls), 1),
        "pnl": round(sum(pnls), 2),
        "pf": round(abs(gw / gl), 2) if gl < 0 else float("inf"),
        "sharpe": round(sharpe, 2),
        "avg_win": round(np.mean(wins), 2) if wins else 0.0,
        "avg_loss": round(np.mean(loss), 2) if loss else 0.0,
        "max_dd": round(mdd, 2),
        "max_loss": round(min(pnls), 2),
    }


def bootstrap_p_positive(ts, n_boot=2000):
    """90% framework: bootstrap P(total P&L > 0) via trade-level resampling."""
    if not ts:
        return 0.0
    pnls = [t["pnl"] for t in ts]
    rng = random.Random(SEED)
    wins = sum(1 for _ in range(n_boot) if sum(rng.choices(pnls, k=len(pnls))) > 0)
    return round(wins / n_boot * 100.0, 1)


def bootstrap_ci_mean(ts, n_boot=10000, lo=5, hi=95):
    """90% CI on mean per-trade P&L (5th..95th percentile)."""
    if len(ts) < 2:
        return (0.0, 0.0)
    pnls = np.array([t["pnl"] for t in ts])
    rng = np.random.default_rng(SEED)
    means = rng.choice(pnls, size=(n_boot, len(pnls)), replace=True).mean(axis=1)
    return (round(float(np.percentile(means, lo)), 2),
            round(float(np.percentile(means, hi)), 2))


def grade(s, pp):
    """A-F grade — same rubric as backtest_90pct._grade."""
    if s["n"] < 5:
        return "N/A (too few trades)"
    pf = s["pf"] if s["pf"] != float("inf") else 10.0
    score = 0
    if pf >= 1.5:   score += 3
    elif pf >= 1.2: score += 2
    elif pf >= 1.0: score += 1
    if s["wr"] >= 60:   score += 3
    elif s["wr"] >= 50: score += 2
    elif s["wr"] >= 40: score += 1
    if s["sharpe"] >= 1.0:   score += 2
    elif s["sharpe"] >= 0.5: score += 1
    if pp >= 80:   score += 2
    elif pp >= 65: score += 1
    return {9: "A", 7: "B", 5: "C", 3: "D"}.get(
        next((k for k in (9, 7, 5, 3) if score >= k), 0), "F")


def grouped(ts, keyfn):
    out = {}
    g = defaultdict(list)
    for t in ts:
        g[keyfn(t)].append(t)
    for k in sorted(g):
        out[k] = stats(g[k])
    return out


def vix_regime(v):
    return "below_15" if v < 15 else ("15_to_20" if v <= 20 else "above_20")


def dist_bucket(d):
    a = abs(d)
    return "0.3_0.5pct" if a < 0.5 else ("0.5_1.0pct" if a < 1.0 else "above_1.0pct")


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ── Compute all breakdowns ─────────────────────────────────────────────────────
S        = stats(trades)
PP       = bootstrap_p_positive(trades)
CI_LO, CI_HI = bootstrap_ci_mean(trades)
GRADE    = grade(S, PP)
by_month = grouped(trades, lambda t: t["month"])
by_year  = grouped(trades, lambda t: t["yr"])
by_dow   = grouped(trades, lambda t: t["dow"])
by_vix   = grouped(trades, lambda t: vix_regime(t["vix"]))
by_dist  = grouped(trades, lambda t: dist_bucket(t["dist_pct"]))
by_dir   = grouped(trades, lambda t: t["direction"])
reasons  = defaultdict(int)
for t in trades:
    reasons[t["reason"]] += 1

# ── Verdict ─────────────────────────────────────────────────────────────────
wr, pf = S["wr"], (S["pf"] if S["pf"] != float("inf") else 99.0)
if wr > 55 and pf > 1.3:
    verdict = "PROMOTE"
elif wr < 50 or pf < 1.1:
    verdict = "KILL"
else:
    verdict = "WATCH"


# ── Console report ─────────────────────────────────────────────────────────────
DOW_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri"]
VIX_ORDER = ["below_15", "15_to_20", "above_20"]
DIST_ORDER = ["0.3_0.5pct", "0.5_1.0pct", "above_1.0pct"]
SEP = "=" * 72


def line(s=""):
    print(s)


line(SEP)
line("  VWAP DIRECTIONAL DEBIT SPREAD — SPY 0DTE  (REAL ThetaData bid/ask)")
line(f"  Window {START_DATE} .. {END_DATE}  •  generated {datetime.now():%Y-%m-%d %H:%M:%S}")
line(SEP)
line("  Fills at MID; reject leg spread > $0.10; "
     f"commission ${COMMISSION_LEG}/leg (${COMMISSION_RT:.2f} round-trip).")
line("  Entry 09:45 ET • exit 75% max-profit / 100% loss / 14:00 ET. FOMC+CPI skipped.")
line("")
line("  HEADLINE")
line(f"    Total trades ........ {S['n']}")
line(f"    Win rate ............ {S['wr']}%")
line(f"    Avg win ............. ${S['avg_win']:+.2f}")
line(f"    Avg loss ............ ${S['avg_loss']:+.2f}")
line(f"    Profit factor ....... {fmt_pf(S['pf'])}")
line(f"    Total P&L ........... ${S['pnl']:+,.2f}")
line(f"    Sharpe (ann.) ....... {S['sharpe']}")
line(f"    Max drawdown ........ ${S['max_dd']:,.2f}")
line(f"    Max single loss ..... ${S['max_loss']:+,.2f}")
line(f"    Exit mix ............ {dict(reasons)}")
line("")
line("  90% CONFIDENCE FRAMEWORK")
line(f"    Bootstrap P(total P&L > 0) .. {PP}%  (n=2000 resamples)")
line(f"    90% CI on mean P&L/trade .... ${CI_LO:+.2f} … ${CI_HI:+.2f}  "
     f"({'excludes 0 → significant' if CI_LO > 0 else 'straddles 0 → NOT significant'})")
line(f"    Grade ....................... {GRADE}")
line("")

line("  MONTHLY P&L")
line(f"    {'Month':<9} {'N':>4} {'WR%':>6} {'P&L':>12} {'PF':>6}")
for m in sorted(by_month):
    s = by_month[m]
    line(f"    {m:<9} {s['n']:>4} {s['wr']:>6} {s['pnl']:>+12,.2f} {fmt_pf(s['pf']):>6}")
line("")

line("  YEAR-BY-YEAR")
line(f"    {'Year':<6} {'N':>4} {'WR%':>6} {'P&L':>12} {'PF':>6} {'MaxDD':>10}")
for y in sorted(by_year):
    s = by_year[y]
    line(f"    {y:<6} {s['n']:>4} {s['wr']:>6} {s['pnl']:>+12,.2f} "
         f"{fmt_pf(s['pf']):>6} {s['max_dd']:>10,.2f}")
line("")

line("  WIN RATE BY DAY OF WEEK")
line(f"    {'Day':<6} {'N':>4} {'WR%':>6} {'P&L':>12} {'PF':>6}")
for d in DOW_ORDER:
    if d in by_dow:
        s = by_dow[d]
        line(f"    {d:<6} {s['n']:>4} {s['wr']:>6} {s['pnl']:>+12,.2f} {fmt_pf(s['pf']):>6}")
line("")

line("  WIN RATE BY VIX REGIME")
line(f"    {'Regime':<10} {'N':>4} {'WR%':>6} {'P&L':>12} {'PF':>6}")
for r in VIX_ORDER:
    if r in by_vix:
        s = by_vix[r]
        line(f"    {r:<10} {s['n']:>4} {s['wr']:>6} {s['pnl']:>+12,.2f} {fmt_pf(s['pf']):>6}")
line("")

line("  WIN RATE BY VWAP DISTANCE")
line(f"    {'Bucket':<13} {'N':>4} {'WR%':>6} {'P&L':>12} {'PF':>6}")
for b in DIST_ORDER:
    if b in by_dist:
        s = by_dist[b]
        line(f"    {b:<13} {s['n']:>4} {s['wr']:>6} {s['pnl']:>+12,.2f} {fmt_pf(s['pf']):>6}")
line("")

line("  BY DIRECTION")
line(f"    {'Side':<11} {'N':>4} {'WR%':>6} {'P&L':>12} {'PF':>6}")
for d in sorted(by_dir):
    s = by_dir[d]
    line(f"    {d:<11} {s['n']:>4} {s['wr']:>6} {s['pnl']:>+12,.2f} {fmt_pf(s['pf']):>6}")
line("")

line(SEP)
line(f"  VERDICT: {verdict}")
line("    PROMOTE: WR>55% AND PF>1.3 | KILL: WR<50% OR PF<1.1 | WATCH: otherwise")
line(SEP)


# ── JSON output ─────────────────────────────────────────────────────────────
def jsonable_stats(s):
    s = dict(s)
    if s.get("pf") == float("inf"):
        s["pf"] = None
    return s


out = {
    "strategy": "vwap_directional_debit_spread",
    "generated": datetime.now().isoformat(timespec="seconds"),
    "window": {"start": START_DATE, "end": END_DATE},
    "config": {
        "entry_et": "09:45", "time_stop_et": "14:00",
        "vwap_band_pct": VWAP_BAND * 100, "atm_delta": ATM_DELTA,
        "strike_width": STRIKE_WIDTH, "profit_target_pct": PROFIT_TARGET * 100,
        "fill": "mid", "max_leg_spread": MAX_LEG_SPREAD,
        "commission_per_leg": COMMISSION_LEG, "commission_roundtrip": COMMISSION_RT,
        "contracts": CONTRACTS, "skip_fomc": True, "skip_cpi": True,
        "data_source": "ThetaData /v3/option/history/quote @ localhost:25503 (real bid/ask)",
    },
    "skip_reasons": dict(skip_reasons),
    "sim_skips": dict(sim_skip),
    "headline": jsonable_stats(S),
    "confidence_90pct": {
        "bootstrap_p_positive": PP,
        "ci90_mean_pnl_per_trade": [CI_LO, CI_HI],
        "ci90_excludes_zero": CI_LO > 0,
        "grade": GRADE,
    },
    "exit_reason_mix": dict(reasons),
    "monthly_pnl": {m: jsonable_stats(s) for m, s in by_month.items()},
    "by_year": {y: jsonable_stats(s) for y, s in by_year.items()},
    "win_rate_by_dow": {d: jsonable_stats(s) for d, s in by_dow.items()},
    "win_rate_by_vix_regime": {r: jsonable_stats(s) for r, s in by_vix.items()},
    "win_rate_by_vwap_distance": {b: jsonable_stats(s) for b, s in by_dist.items()},
    "by_direction": {d: jsonable_stats(s) for d, s in by_dir.items()},
    "verdict": verdict,
    "verdict_rules": "PROMOTE: WR>55% AND PF>1.3 | KILL: WR<50% OR PF<1.1 | WATCH: otherwise",
    "trades": trades,
}

out_path = RESULTS / f"vwap_directional_{datetime.now():%Y%m%d}.json"
out_path.write_text(json.dumps(out, indent=2))
print(f"\nResults JSON written: {out_path}")
print(f"VERDICT: {verdict}  (WR {S['wr']}%, PF {fmt_pf(S['pf'])}, N {S['n']})")
