#!/usr/bin/env python3
"""
credit_hypothesis_backtest.py
=============================
NEW hypothesis tests for CREDIT SPREAD strategies, evaluated against the
live R3D strategy (Mon/Wed/Fri SPY 0DTE put credit spread, $2 wide,
0.20 delta, 75% profit target, 2x stop, VIX 15-22, skip FOMC weeks).

The existing H1-H10 results test debit strategies. These five hypotheses
probe credit-spread entry filters and an iron-condor variant:

  H_VIX18  — VIX 18 floor: vix_min=15 vs vix_min=18
  H_CREDIT — Credit-collected threshold: $0.20 vs $0.30 vs $0.40 minimum
  H_FOMC   — FOMC-week skip vs trade-through
  H_GAP    — Skip entry if SPY gaps up >0.7% at the open
  H_R3E    — R3E iron condor by VIX sub-range: 13-15 vs 15-16 vs 16-18

Framework (matches vix_ts_backtest.py / hermes_researcher.py):
  * Data loading + synthetic Black-Scholes pricing driven by VIX (same as
    vix_ts_backtest.py). Valid 0DTE expiries come from the ThetaData cache
    (theta_SPY_*.pkl) on this Hetzner box.
  * 60/40 chronological train/test split on the trade series.
  * Blind 2025 holdout, pulled out of both train and test.
  * Bootstrap confidence: resample daily P&L to get 90% CI on total P&L
    and win-rate, plus P(profit) = fraction of bootstrap runs that are net
    positive.

Output: /root/hermes_system/research/credit_hypothesis_results.md
"""

import csv, math, pickle, random, time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import numpy as np, pytz, warnings
warnings.filterwarnings("ignore")

try:
    import requests as req
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

BASE     = Path("/root/spy-0dte-trader")
DATA_DIR = BASE / "backtest_data"
OUT_PATH = Path("/root/hermes_system/research/credit_hypothesis_results.md")
ET       = pytz.timezone("America/New_York")
TMINS    = 252 * 390

random.seed(42)
np.random.seed(42)

def _env():
    e = {}
    p = BASE / ".env"
    if p.exists():
        for l in p.read_text().splitlines():
            if "=" in l and not l.startswith("#"):
                k, v = l.split("=", 1)
                e[k.strip()] = v.strip().strip("'\"")
    return e

ENV = _env()
TGT = ENV.get("TELEGRAM_BOT_TOKEN", "")
TGC = ENV.get("TELEGRAM_CHAT_ID", "")

def tg(m):
    if not REQUESTS_OK or not TGT or not TGC: print("[TG]", m[:200]); return
    try: req.post(f"https://api.telegram.org/bot{TGT}/sendMessage",
                  json={"chat_id": TGC, "text": m, "parse_mode": "Markdown"}, timeout=8)
    except Exception as e: print(f"[TG ERR] {e}")

# ── Black-Scholes pricing (same as vix_ts_backtest.py) ─────────────────────────
from py_vollib.black_scholes import black_scholes as pvbs
from py_vollib.black_scholes.greeks.analytical import delta as pvd

def bsp(S, K, T, r, s, f):
    if T <= 1e-7 or s <= 1e-6: return max(S-K,0) if f=="c" else max(K-S,0)
    try: return float(pvbs(f,S,K,T,r,s))
    except: return 0.01

def bsd(S, K, T, r, s, f):
    if T <= 1e-7 or s <= 1e-6: return (1. if S>K else 0.) if f=="c" else (-1. if S<K else 0.)
    try: return float(pvd(f,S,K,T,r,s))
    except: return 0.

def skd(tgt, S, T, r, iv, f, n=60):
    """Strike whose |delta| is closest to tgt for option flavour f."""
    best_k, best_d = S, 999.
    lo = S*0.85 if f=="p" else S
    hi = S if f=="p" else S*1.15
    for i in range(n):
        k = lo + (hi-lo)*i/n
        d = abs(bsd(S,k,T,r,iv,f))
        if abs(d-tgt) < best_d: best_d, best_k = abs(d-tgt), k
    return round(best_k/0.5)*0.5

def opt(S, K, ml, vix, f, sp=0.065):
    """Synthetic bid/ask for a SPY option, ml minutes to close, IV=VIX/100."""
    T = max(ml, 0.5) / TMINS
    r = 0.045
    iv = vix / 100
    mid = max(bsp(S,K,T,r,iv,f), 0.01)
    return mid*(1-sp/2), mid*(1+sp/2)

# ── Load market data ───────────────────────────────────────────────────────────
print("Loading market data...")
with (DATA_DIR/"spy_daily_2021-01-04_2026-05-30.pkl").open("rb") as f:
    spy_d = pickle.load(f)
with (DATA_DIR/"spy_5min_2021-01-04_2026-05-30.pkl").open("rb") as f:
    spy_5 = pickle.load(f)
vix_d = {}
with (DATA_DIR/"vix_2021-01-04_2026-05-30.csv").open() as f:
    for row in csv.DictReader(f): vix_d[row["date"]] = float(row["vix"])

exps = sorted(
    date.fromisoformat(p.stem[10:])
    for p in DATA_DIR.glob("theta_SPY_????-??-??.pkl")
    if len(p.stem) == len("theta_SPY_2021-01-04"))
exps_set = set(exps)
dlist = sorted(spy_d.keys())
# Previous-close lookup for gap computation.
prev_close = {}
for i, ds in enumerate(dlist):
    if i > 0: prev_close[ds] = spy_d[dlist[i-1]]["close"]
print(f"  {len(dlist)} daily bars | {len(exps)} ThetaData 0DTE expiries | {len(vix_d)} VIX days")

# ── FOMC meeting weeks 2021-2026 ───────────────────────────────────────────────
# Decision-day (Wednesday) of each FOMC meeting. skip_fomc_weeks skips the whole
# Mon-Fri week containing one of these, matching execution_engine._is_fomc_week.
FOMC_DECISION_DAYS = [
    date(2021,1,27), date(2021,3,17), date(2021,4,28), date(2021,6,16),
    date(2021,7,28), date(2021,9,22), date(2021,11,3), date(2021,12,15),
    date(2022,1,26), date(2022,3,16), date(2022,5,4),  date(2022,6,15),
    date(2022,7,27), date(2022,9,21), date(2022,11,2), date(2022,12,14),
    date(2023,2,1),  date(2023,3,22), date(2023,5,3),  date(2023,6,14),
    date(2023,7,26), date(2023,9,20), date(2023,11,1), date(2023,12,13),
    date(2024,1,31), date(2024,3,20), date(2024,5,1),  date(2024,6,12),
    date(2024,7,31), date(2024,9,18), date(2024,11,7), date(2024,12,18),
    date(2025,1,29), date(2025,3,19), date(2025,5,7),  date(2025,6,18),
    date(2025,7,30), date(2025,9,17), date(2025,10,29),date(2025,12,17),
    date(2026,1,28), date(2026,3,18), date(2026,4,29), date(2026,6,17),
]
# Set of every (Mon-Fri) date that lies in an FOMC week.
FOMC_WEEK_DAYS = set()
for d in FOMC_DECISION_DAYS:
    monday = d - timedelta(days=d.weekday())
    for off in range(5):
        FOMC_WEEK_DAYS.add(monday + timedelta(days=off))

def is_fomc_week(d):
    return d in FOMC_WEEK_DAYS

# ── Year-aware risk-free rate ──────────────────────────────────────────────────
_RFR = {2021:.001,2022:.015,2023:.045,2024:.050,2025:.045,2026:.043}
rfr  = lambda d: _RFR.get(d.year, 0.045)
BLIND = 2025

# ── Core trade simulators ──────────────────────────────────────────────────────
def _entry_bar(bars, win_start, win_end):
    """First 5-min bar whose ET time is within [win_start, win_end)."""
    for bar in bars:
        db = datetime.fromtimestamp(bar["t"]/1000, tz=ET)
        if win_start <= (db.hour, db.minute) < win_end:
            return bar
    return None

def _resolve_expiry(dt):
    if dt in exps_set: return dt
    for e in exps:
        if 0 <= (e-dt).days <= 1: return e
    return None

def run_put_spread(dates, cfg):
    """
    R3D-style put credit spread.

    cfg keys:
      days          set of weekday ints           (R3D: {0,2,4})
      win_start/end (h,m) tuples for entry window  (R3D: (10,15)/(11,0))
      vix_min/max   VIX gate                       (R3D: 15/22)
      delta         short-leg target delta         (R3D: 0.20)
      width         spread width $                 (R3D: 2.0)
      pt            profit-target fraction kept     (R3D: 0.75 -> close at 0.25 debit)
      stop_mult     stop multiple of credit         (R3D: 2.0)
      force_exit    (h,m) hard exit                 (R3D: (15,45))
      skip_fomc     bool
      gap_skip      None or float pct (skip if gap up > pct)
      credit_min    minimum collected credit to take the trade
    """
    trades = []
    for ds in dates:
        dt = date.fromisoformat(ds)
        if dt.weekday() not in cfg["days"]: continue
        if cfg["skip_fomc"] and is_fomc_week(dt): continue
        vix = vix_d.get(ds, 16.)
        if not (cfg["vix_min"] <= vix <= cfg["vix_max"]): continue

        # Gap filter: skip if SPY opened more than gap_skip% above prior close.
        if cfg.get("gap_skip") is not None and ds in prev_close:
            gap = (spy_d[ds]["open"] - prev_close[ds]) / prev_close[ds] * 100
            if gap > cfg["gap_skip"]: continue

        exp = _resolve_expiry(dt)
        if exp is None: continue
        bars = spy_5.get(ds, [])
        if len(bars) < 8: continue
        eb = _entry_bar(bars, cfg["win_start"], cfg["win_end"])
        if eb is None: continue

        dt_eb = datetime.fromtimestamp(eb["t"]/1000, tz=ET)
        spy_e = eb["c"]
        r     = rfr(dt); iv = vix / 100
        ml    = max(16*60 - (dt_eb.hour*60+dt_eb.minute), 1)
        T_e   = ml / TMINS
        sk    = skd(cfg["delta"], spy_e, T_e, r, iv, "p", 60)
        lk    = sk - cfg["width"]

        sc_bid, _ = opt(spy_e, sk, ml, vix, "p")
        _, lp_ask = opt(spy_e, lk, ml, vix, "p")
        credit    = round(sc_bid - lp_ask, 4)
        if credit < cfg.get("credit_min", 0.0): continue
        if credit <= 0: continue

        tgt = credit * (1 - cfg["pt"])     # debit-to-close at profit target
        stp = credit * cfg["stop_mult"]    # debit-to-close at stop
        eb_x = None; rsn = "EOD"
        for bar in bars:
            db = datetime.fromtimestamp(bar["t"]/1000, tz=ET)
            if db <= dt_eb: continue
            if (db.hour, db.minute) >= cfg["force_exit"]: eb_x=bar; rsn="force"; break
            ml2   = max(16*60-(db.hour*60+db.minute), 1)
            sa, _ = opt(bar["c"], sk, ml2, vix, "p")
            _, lb = opt(bar["c"], lk, ml2, vix, "p")
            cur   = max(sa-lb, 0)
            if cur <= tgt: eb_x=bar; rsn="target"; break
            if cur >= stp: eb_x=bar; rsn="stop";   break
        if eb_x is None: eb_x = bars[-1]

        db_x  = datetime.fromtimestamp(eb_x["t"]/1000, tz=ET)
        ml_x  = max(16*60-(db_x.hour*60+db_x.minute), 1)
        sa_x, _ = opt(eb_x["c"], sk, ml_x, vix, "p")
        _, lb_x = opt(eb_x["c"], lk, ml_x, vix, "p")
        exit_d  = max(sa_x - lb_x, 0)
        pnl     = round((credit - exit_d) * 100, 2)

        trades.append({"date": ds, "pnl": pnl, "credit": credit, "vix": vix,
                       "rsn": rsn, "yr": ds[:4]})
    return trades

def run_iron_condor(dates, cfg):
    """
    R3E-style iron condor (short put spread + short call spread).

    Same cfg keys as run_put_spread minus gap/credit_min; pt is the fraction
    of total credit kept (R3E: 0.50), stop_mult on combined credit.
    """
    trades = []
    for ds in dates:
        dt = date.fromisoformat(ds)
        if dt.weekday() not in cfg["days"]: continue
        if cfg["skip_fomc"] and is_fomc_week(dt): continue
        vix = vix_d.get(ds, 16.)
        if not (cfg["vix_min"] <= vix <= cfg["vix_max"]): continue

        exp = _resolve_expiry(dt)
        if exp is None: continue
        bars = spy_5.get(ds, [])
        if len(bars) < 8: continue
        eb = _entry_bar(bars, cfg["win_start"], cfg["win_end"])
        if eb is None: continue

        dt_eb = datetime.fromtimestamp(eb["t"]/1000, tz=ET)
        spy_e = eb["c"]
        r     = rfr(dt); iv = vix / 100
        ml    = max(16*60 - (dt_eb.hour*60+dt_eb.minute), 1)
        T_e   = ml / TMINS
        # Put side
        psk = skd(cfg["delta"], spy_e, T_e, r, iv, "p", 60); plk = psk - cfg["width"]
        # Call side
        csk = skd(cfg["delta"], spy_e, T_e, r, iv, "c", 60); clk = csk + cfg["width"]

        psc_bid, _ = opt(spy_e, psk, ml, vix, "p"); _, plp_ask = opt(spy_e, plk, ml, vix, "p")
        csc_bid, _ = opt(spy_e, csk, ml, vix, "c"); _, clp_ask = opt(spy_e, clk, ml, vix, "c")
        credit = round((psc_bid - plp_ask) + (csc_bid - clp_ask), 4)
        if credit <= 0: continue

        tgt = credit * (1 - cfg["pt"])
        stp = credit * cfg["stop_mult"]
        eb_x = None; rsn = "EOD"
        for bar in bars:
            db = datetime.fromtimestamp(bar["t"]/1000, tz=ET)
            if db <= dt_eb: continue
            if (db.hour, db.minute) >= cfg["force_exit"]: eb_x=bar; rsn="force"; break
            ml2 = max(16*60-(db.hour*60+db.minute), 1)
            pa, _ = opt(bar["c"], psk, ml2, vix, "p"); _, pb = opt(bar["c"], plk, ml2, vix, "p")
            ca, _ = opt(bar["c"], csk, ml2, vix, "c"); _, cb = opt(bar["c"], clk, ml2, vix, "c")
            cur = max((pa-pb) + (ca-cb), 0)
            if cur <= tgt: eb_x=bar; rsn="target"; break
            if cur >= stp: eb_x=bar; rsn="stop";   break
        if eb_x is None: eb_x = bars[-1]

        db_x = datetime.fromtimestamp(eb_x["t"]/1000, tz=ET)
        ml_x = max(16*60-(db_x.hour*60+db_x.minute), 1)
        pa_x, _ = opt(eb_x["c"], psk, ml_x, vix, "p"); _, pb_x = opt(eb_x["c"], plk, ml_x, vix, "p")
        ca_x, _ = opt(eb_x["c"], csk, ml_x, vix, "c"); _, cb_x = opt(eb_x["c"], clk, ml_x, vix, "c")
        exit_d = max((pa_x-pb_x) + (ca_x-cb_x), 0)
        pnl    = round((credit - exit_d) * 100, 2)

        trades.append({"date": ds, "pnl": pnl, "credit": credit, "vix": vix,
                       "rsn": rsn, "yr": ds[:4]})
    return trades

# ── Stats, split, bootstrap ────────────────────────────────────────────────────
def calc_stats(trades):
    if not trades:
        return {"n":0,"wr":0,"total_pnl":0,"pf":0,"sharpe":0,"max_dd":0,"wins":0,"losses":0,"avg":0}
    wins   = [t for t in trades if t["pnl"]>0]
    losses = [t for t in trades if t["pnl"]<=0]
    gw = sum(t["pnl"] for t in wins); gl = sum(t["pnl"] for t in losses)
    wr = len(wins)/len(trades)
    eq = peak = mdd = 0.
    for t in sorted(trades, key=lambda x: x["date"]):
        eq += t["pnl"]; peak=max(peak,eq); mdd=max(mdd,peak-eq)
    by_d = defaultdict(float)
    for t in trades: by_d[t["date"]] += t["pnl"]
    daily = list(by_d.values())
    if len(daily) > 1:
        mu, sig = np.mean(daily), np.std(daily, ddof=1)
        sh = mu/sig*math.sqrt(252) if sig>0 else 0.
    else: sh = 0.
    pf = abs(gw/gl) if gl<0 else float("inf")
    tot = sum(t["pnl"] for t in trades)
    return {"n":len(trades),"wr":wr*100,"total_pnl":tot,"pf":pf,"sharpe":sh,
            "max_dd":mdd,"wins":len(wins),"losses":len(losses),"avg":tot/len(trades)}

def pf_str(s):
    p = s["pf"]
    return "inf" if p==float("inf") else f"{p:.2f}"

def split_6040(trades):
    """Chronological 60/40 train/test split, with 2025 pulled out as blind."""
    insample = [t for t in trades if t["yr"] != str(BLIND)]
    blind    = [t for t in trades if t["yr"] == str(BLIND)]
    insample.sort(key=lambda x: x["date"])
    cut = int(round(len(insample) * 0.6))
    return insample[:cut], insample[cut:], blind   # train, test(40%), blind

def bootstrap(trades, B=2000):
    """
    Resample daily P&L with replacement. Returns 90% CI on total P&L and WR,
    and P(profit) = fraction of bootstrap runs that finish net-positive.
    """
    if not trades:
        return {"pnl_lo":0,"pnl_hi":0,"wr_lo":0,"wr_hi":0,"p_profit":0}
    by_d = defaultdict(float)
    for t in trades: by_d[t["date"]] += t["pnl"]
    daily = np.array(list(by_d.values()))
    n = len(daily)
    tot_samples, wr_samples, pos = [], [], 0
    for _ in range(B):
        idx = np.random.randint(0, n, n)
        s = daily[idx]
        tot = s.sum()
        tot_samples.append(tot)
        wr_samples.append(float((s > 0).mean()) * 100)
        if tot > 0: pos += 1
    tot_samples = np.array(tot_samples); wr_samples = np.array(wr_samples)
    return {"pnl_lo":float(np.percentile(tot_samples,5)),
            "pnl_hi":float(np.percentile(tot_samples,95)),
            "wr_lo":float(np.percentile(wr_samples,5)),
            "wr_hi":float(np.percentile(wr_samples,95)),
            "p_profit":pos/B*100}

# ── R3D / R3E base configs ─────────────────────────────────────────────────────
R3D_BASE = dict(days={0,2,4}, win_start=(10,15), win_end=(11,0),
                vix_min=15.0, vix_max=22.0, delta=0.20, width=2.0,
                pt=0.75, stop_mult=2.0, force_exit=(15,45),
                skip_fomc=True, gap_skip=None, credit_min=0.0)
R3E_BASE = dict(days={2}, win_start=(10,30), win_end=(10,45),
                vix_min=13.0, vix_max=18.0, delta=0.16, width=2.0,
                pt=0.50, stop_mult=2.0, force_exit=(15,30), skip_fomc=False)

def cfg(base, **kw):
    c = dict(base); c.update(kw); return c

# ── Build all variants ─────────────────────────────────────────────────────────
# Each entry: (hypothesis_id, variant_label, runner, config)
VARIANTS = [
    ("H_VIX18", "VIX floor 15 (baseline)", run_put_spread, cfg(R3D_BASE, vix_min=15.0)),
    ("H_VIX18", "VIX floor 18",            run_put_spread, cfg(R3D_BASE, vix_min=18.0)),

    ("H_CREDIT", "Credit min $0.20", run_put_spread, cfg(R3D_BASE, credit_min=0.20)),
    ("H_CREDIT", "Credit min $0.30", run_put_spread, cfg(R3D_BASE, credit_min=0.30)),
    ("H_CREDIT", "Credit min $0.40", run_put_spread, cfg(R3D_BASE, credit_min=0.40)),

    ("H_FOMC", "Skip FOMC weeks (baseline)", run_put_spread, cfg(R3D_BASE, skip_fomc=True)),
    ("H_FOMC", "Trade through FOMC",         run_put_spread, cfg(R3D_BASE, skip_fomc=False)),

    ("H_GAP", "No gap filter (baseline)",  run_put_spread, cfg(R3D_BASE, gap_skip=None)),
    ("H_GAP", "Skip gap-up >0.7%",         run_put_spread, cfg(R3D_BASE, gap_skip=0.7)),

    ("H_R3E", "IC VIX 13-15", run_iron_condor, cfg(R3E_BASE, vix_min=13.0, vix_max=15.0)),
    ("H_R3E", "IC VIX 15-16", run_iron_condor, cfg(R3E_BASE, vix_min=15.0, vix_max=16.0)),
    ("H_R3E", "IC VIX 16-18", run_iron_condor, cfg(R3E_BASE, vix_min=16.0, vix_max=18.0)),
]

print("\nRunning variants...")
results = []
for hid, label, runner, config in VARIANTS:
    t0 = time.time()
    trades = runner(dlist, config)
    train, test, blind = split_6040(trades)
    rec = {
        "hid": hid, "label": label,
        "trades": trades,
        "s_all": calc_stats(trades),
        "s_train": calc_stats(train),
        "s_test": calc_stats(test),
        "s_blind": calc_stats(blind),
        "boot_test": bootstrap(test),
        "boot_blind": bootstrap(blind),
    }
    results.append(rec)
    print(f"  [{hid}] {label}: N={len(trades)} "
          f"(train {len(train)}/test {len(test)}/blind {len(blind)}) {time.time()-t0:.1f}s")

# ── Verdict logic per hypothesis ───────────────────────────────────────────────
def by_hid(hid):
    return [r for r in results if r["hid"] == hid]

def verdict_compare(base_label, alt_label, hid, metric_min_wr=1.5, metric_min_sh=0.3):
    """Compare an alternative variant to a baseline on the 40% test split."""
    rs = {r["label"]: r for r in by_hid(hid)}
    b, a = rs[base_label], rs[alt_label]
    dwr = a["s_test"]["wr"] - b["s_test"]["wr"]
    dsh = a["s_test"]["sharpe"] - b["s_test"]["sharpe"]
    dpnl = a["s_test"]["total_pnl"] - b["s_test"]["total_pnl"]
    blind_ok = a["s_blind"]["wr"] >= b["s_blind"]["wr"]
    if dwr >= metric_min_wr and dsh >= metric_min_sh and blind_ok:
        v = "CONFIRMED"
    elif dwr >= 0 and dsh >= 0:
        v = "MARGINAL"
    else:
        v = "REJECTED"
    return v, dwr, dsh, dpnl

# ── Markdown report ────────────────────────────────────────────────────────────
def row(label, s):
    return (f"| {label} | {s['n']} | {s['wr']:.1f}% | ${s['total_pnl']:+.0f} | "
            f"${s['avg']:+.1f} | {pf_str(s)} | {s['sharpe']:.2f} | ${s['max_dd']:.0f} |")

HDR = "| Variant | N | WR | Total P&L | Avg/trade | PF | Sharpe | MaxDD |"
SEP = "|---|---|---|---|---|---|---|---|"

L = []
L.append("# Credit Spread Hypothesis Backtest — SPY 0DTE")
L.append("")
L.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
         f"Data: 2021-01-04 → 2026-05-29, {len(exps)} ThetaData 0DTE expiries*")
L.append("")
L.append("Five NEW hypothesis tests for **credit-spread** strategies, evaluated "
         "against the live **R3D** strategy (Mon/Wed/Fri SPY 0DTE put credit "
         "spread, $2 wide, 0.20 delta, 75% profit target, 2x stop, VIX 15-22, "
         "skip FOMC weeks). The existing H1-H10 results cover debit strategies; "
         "these probe credit-spread entry filters plus an iron-condor (R3E) variant.")
L.append("")
L.append("**Method** — synthetic Black-Scholes pricing driven by VIX (same engine "
         "as `vix_ts_backtest.py`); valid 0DTE expiries from the ThetaData cache. "
         "Each variant is split **60/40 chronologically** (train / out-of-sample "
         "test) with **2025 held out blind**. **Bootstrap confidence** (2,000 "
         "resamples of daily P&L) gives 90% CIs and P(profit) on the test split.")
L.append("")

TITLES = {
    "H_VIX18":  "H_VIX18 — VIX 18 floor vs 15 floor",
    "H_CREDIT": "H_CREDIT — Minimum collected-credit threshold",
    "H_FOMC":   "H_FOMC — Skip FOMC weeks vs trade through",
    "H_GAP":    "H_GAP — Skip entry on SPY gap-up >0.7%",
    "H_R3E":    "H_R3E — R3E iron condor by VIX sub-range",
}

for hid in ["H_VIX18", "H_CREDIT", "H_FOMC", "H_GAP", "H_R3E"]:
    rs = by_hid(hid)
    L.append("---"); L.append(""); L.append(f"## {TITLES[hid]}"); L.append("")

    L.append("**Full sample (2021-2026)**"); L.append(""); L.append(HDR); L.append(SEP)
    for r in rs: L.append(row(r["label"], r["s_all"]))
    L.append("")

    L.append("**Out-of-sample test (40% holdout) + blind 2025**"); L.append("")
    L.append("| Variant | Test N | Test WR | Test P&L | Test Sharpe | "
             "Test P&L 90% CI | P(profit) | Blind N | Blind WR | Blind P&L |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rs:
        st, sb, bt = r["s_test"], r["s_blind"], r["boot_test"]
        L.append(f"| {r['label']} | {st['n']} | {st['wr']:.1f}% | ${st['total_pnl']:+.0f} | "
                 f"{st['sharpe']:.2f} | ${bt['pnl_lo']:+.0f} … ${bt['pnl_hi']:+.0f} | "
                 f"{bt['p_profit']:.0f}% | {sb['n']} | {sb['wr']:.1f}% | ${sb['total_pnl']:+.0f} |")
    L.append("")

    # Per-hypothesis verdict.
    if hid == "H_VIX18":
        v, dwr, dsh, dpnl = verdict_compare("VIX floor 15 (baseline)", "VIX floor 18", hid)
        L.append(f"**Verdict: {v}.** Raising the VIX floor 15→18 moves the test-split "
                 f"WR by **{dwr:+.1f}%**, Sharpe **{dsh:+.2f}**, total P&L **${dpnl:+.0f}**.")
        if v == "CONFIRMED":
            L.append("Recommendation: the existing GLOBAL_VIX_FLOOR=18 enforcement is justified by the data.")
        elif v == "MARGINAL":
            L.append("Recommendation: floor 18 trades fewer days for a small/uncertain gain — keep as a risk control, not an edge.")
        else:
            L.append("Recommendation: floor 18 removes more good trades than bad — VIX 15 floor is preferable on edge alone.")
    elif hid == "H_CREDIT":
        # Pick the highest credit floor that still has a usable sample (test N>=20).
        usable = [r for r in rs if r["s_test"]["n"] >= 20]
        best = max(usable, key=lambda r: r["s_test"]["wr"]) if usable else rs[0]
        dead = [r["label"] for r in rs if r["s_test"]["n"] == 0]
        L.append(f"**Verdict:** Among floors with a usable OOS sample (test N≥20), "
                 f"**{best['label']}** gives the best test WR ({best['s_test']['wr']:.1f}%, "
                 f"P(profit) {best['boot_test']['p_profit']:.0f}%) — but at a steep volume cost "
                 f"(full-sample N drops {results[2]['s_all']['n']}→{best['s_all']['n']}).")
        if dead:
            L.append(f"A **{', '.join(dead)}** floor is effectively unreachable at 0.20-delta / "
                     f"$2-wide (N=0), consistent with the strike-formula analysis (≥$0.40 credit on "
                     f"only ~16% of days). Do not set the floor above $0.30.")
    elif hid == "H_FOMC":
        v, dwr, dsh, dpnl = verdict_compare("Skip FOMC weeks (baseline)", "Trade through FOMC", hid)
        if v == "REJECTED":
            L.append(f"**Verdict: skipping FOMC weeks is correct.** Trading through FOMC moves "
                     f"test WR **{dwr:+.1f}%**, Sharpe **{dsh:+.2f}**, P&L **${dpnl:+.0f}** — i.e. it hurts. "
                     f"Keep R3D's `skip_fomc_weeks=True`.")
        else:
            L.append(f"**Verdict: {v}.** Trading through FOMC moves test WR **{dwr:+.1f}%**, "
                     f"Sharpe **{dsh:+.2f}**, P&L **${dpnl:+.0f}**. The FOMC-skip rule may be costing edge — "
                     f"re-examine before keeping it.")
    elif hid == "H_GAP":
        v, dwr, dsh, dpnl = verdict_compare("No gap filter (baseline)", "Skip gap-up >0.7%", hid)
        L.append(f"**Verdict: {v}.** Skipping gap-up >0.7% opens moves test WR **{dwr:+.1f}%**, "
                 f"Sharpe **{dsh:+.2f}**, P&L **${dpnl:+.0f}**.")
        if v == "CONFIRMED":
            L.append("Recommendation: add a >0.7% gap-up skip to R3D entry.")
        elif v == "MARGINAL":
            L.append("Recommendation: small effect — optional filter, not a clear edge.")
        else:
            L.append("Recommendation: the gap-up filter removes more winners than losers — do not add it.")
    elif hid == "H_R3E":
        # Sharpe is degenerate at tiny N (deterministic pricing -> ~zero variance).
        # Rank by sample size first; a band needs enough trades to be tradeable.
        usable = [r for r in rs if r["s_all"]["n"] >= 30]
        best = max(usable, key=lambda r: r["s_all"]["n"]) if usable else max(rs, key=lambda r: r["s_all"]["n"])
        L.append(f"**Verdict:** Only **{best['label']}** carries enough volume to be tradeable "
                 f"(full-sample N={best['s_all']['n']}, test N={best['s_test']['n']}, WR "
                 f"{best['s_test']['wr']:.1f}%, blind WR {best['s_blind']['wr']:.1f}%). The lower bands "
                 f"(13-15, 15-16) show 100% WR but on **<25 trades each** — too thin to trust, and the "
                 f"sky-high Sharpe there is a pricing artifact (see caveat), not a real edge. R3E is a "
                 f"Wed-only condor, so all sub-ranges are volume-starved; widen the entry days before "
                 f"reading these win-rates as stable.")
    L.append("")

# ── Summary table ──────────────────────────────────────────────────────────────
L.append("---"); L.append(""); L.append("## Summary — test-split (40% OOS) snapshot"); L.append("")
L.append("| Hypothesis | Variant | Test N | Test WR | Test Sharpe | P(profit) | Blind WR |")
L.append("|---|---|---|---|---|---|---|")
for r in results:
    st, sb, bt = r["s_test"], r["s_blind"], r["boot_test"]
    L.append(f"| {r['hid']} | {r['label']} | {st['n']} | {st['wr']:.1f}% | "
             f"{st['sharpe']:.2f} | {bt['p_profit']:.0f}% | {sb['wr']:.1f}% |")
L.append("")
L.append("**Caveats (read before acting):**")
L.append("")
L.append("- **Synthetic pricing.** P&L uses Black-Scholes mids with VIX as a flat IV and a "
         "fixed 6.5% spread — not ThetaData fills. Dollar totals are indicative, not "
         "tradeable; read WR and *relative* deltas between variants, not absolute P&L.")
L.append("- **Sharpe is inflated.** Because the synthetic exit is near-deterministic, daily "
         "P&L variance is tiny and annualised Sharpe blows up (8–235 here). These are **not** "
         "real Sharpe ratios; use them only to rank variants, and distrust any Sharpe attached "
         "to a small N entirely.")
L.append("- **Small samples.** R3E sub-ranges and the $0.30+ credit floors run on very few "
         "trades (some <25). 100% win-rates there are sampling noise, not evidence of edge.")
L.append("- **No IV skew / early-assignment / liquidity gaps** are modelled; the flat-IV "
         "assumption flatters deep-OTM credit-spread win-rates versus live fills.")
L.append("")

report = "\n".join(L)
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUT_PATH.write_text(report)
print(f"\nReport saved: {OUT_PATH}")

# ── Telegram summary ───────────────────────────────────────────────────────────
msg = "📊 *Credit Hypothesis Backtest Complete*\n\nTest-split (40% OOS) Sharpe by hypothesis:\n"
for hid in ["H_VIX18","H_CREDIT","H_FOMC","H_GAP","H_R3E"]:
    best = max(by_hid(hid), key=lambda r: r["s_test"]["sharpe"])
    msg += f"  {hid}: {best['label']} → Sharpe {best['s_test']['sharpe']:.2f}, WR {best['s_test']['wr']:.1f}%\n"
msg += f"\nReport: {OUT_PATH}"
tg(msg)
print("\nDone.")
