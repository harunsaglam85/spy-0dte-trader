#!/usr/bin/env python3
"""
experimental_strategies_e1_e10_backtest.py
===========================================
Backtests 10 experimental low-VIX strategies (E1-E10) designed to increase
daily trade frequency from ~2 to 8-10/day.

Methodology mirrors vix_ts_backtest.py:
  - Real SPY 5-min bars (ThetaData-aligned) drive intraday entry/exit.
  - Real ThetaData expiration dates gate which days have a 0DTE expiry.
  - Black-Scholes prices each leg with IV = VIX/100 (the established model
    used to validate the live R-strategies), with a 6.5% bid/ask spread.
  - Live MIN_CREDIT = $0.20 gate applied (matches execution_engine) so we
    only count trades the live engine would actually take.

Split: in-sample < 2023-07-01, OOS 2023-07-01..2026 (excl 2025), blind = 2025.
Kill criteria: OOS WR < 60% OR OOS Sharpe < 2.0.

Limitations:
  - Only daily VIX is available. E8's "VIX falling >1pt in 30min" intraday
    signal is proxied by day-over-day VIX drop (>1pt vs prior close).
"""

import csv, math, os, pickle, time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import numpy as np, pytz, warnings
warnings.filterwarnings("ignore")

BASE     = Path("/root/spy-0dte-trader")
DATA_DIR = BASE / "backtest_data"
OUT_DIR  = BASE / "backtests"
OUT_DIR.mkdir(exist_ok=True)
ET       = pytz.timezone("America/New_York")
TMINS    = 252 * 390

MIN_CREDIT = 0.20            # live execution_engine gate
SPLIT      = date(2023, 7, 1)
BLIND      = 2025

# BID_ASK calibrates the BS model to live fills. The vix_ts template assumed a
# 6.5% bid/ask (sp=0.065), but on 2026-06-15 (Day 5) the live engine saw a
# 0.20-delta $2-wide put spread at VIX 16.3 fetch $0.15-0.19 net credit; the
# model at sp=0.065 prices that same spread at $0.294 (~1.7x over-credit). A
# ~30% bid/ask (sp=0.30) on these $0.30-0.66 0DTE OTM legs reproduces the live
# $0.177 credit. Real 0DTE OTM spreads ARE this wide. sp=0.065 reproduces the
# raw template (inflated); sp=0.30 reflects realized low-VIX fills.
BID_ASK = float(os.getenv("BID_ASK", "0.065"))

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
    """Find strike whose |delta| is closest to tgt (rounded to 0.5)."""
    best_k, best_d = S, 999.
    lo = S*0.85 if f=="p" else S
    hi = S if f=="p" else S*1.15
    for i in range(n):
        k = lo + (hi-lo)*i/n
        d = abs(bsd(S,k,T,r,iv,f))
        if abs(d-tgt) < best_d: best_d, best_k = abs(d-tgt), k
    return round(best_k/0.5)*0.5

def opt(S, K, ml, vix, f, sp=None):
    sp = BID_ASK if sp is None else sp
    T = max(ml, 0.5) / TMINS
    r = 0.045
    iv = vix / 100
    mid = max(bsp(S,K,T,r,iv,f), 0.01)
    return mid*(1-sp/2), mid*(1+sp/2)

_RFR = {2021:.001,2022:.015,2023:.045,2024:.050,2025:.045,2026:.043}
rfr  = lambda d: _RFR.get(d.year, 0.045)

# ── Load data ─────────────────────────────────────────────────────────────────
print("Loading market data...")
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
dlist = sorted(spy_5.keys())
sorted_vix_dates = sorted(vix_d.keys())
print(f"  {len(dlist)} 5-min days | {len(exps)} theta expiries | {len(vix_d)} VIX days")

# prior-trading-day VIX (for E8 falling proxy)
prev_vix = {}
_prev = None
for ds in sorted_vix_dates:
    if _prev is not None:
        prev_vix[ds] = vix_d[_prev]
    _prev = ds

# ── FOMC decision Wednesdays 2021-2026 (for E3 non-FOMC filter) ─────────────────
FOMC_DECISIONS = [
    date(2021,1,27),date(2021,3,17),date(2021,4,28),date(2021,6,16),date(2021,7,28),date(2021,9,22),date(2021,11,3),date(2021,12,15),
    date(2022,1,26),date(2022,3,16),date(2022,5,4),date(2022,6,15),date(2022,7,27),date(2022,9,21),date(2022,11,2),date(2022,12,14),
    date(2023,2,1),date(2023,3,22),date(2023,5,3),date(2023,6,14),date(2023,7,26),date(2023,9,20),date(2023,11,1),date(2023,12,13),
    date(2024,1,31),date(2024,3,20),date(2024,5,1),date(2024,6,12),date(2024,7,31),date(2024,9,18),date(2024,11,7),date(2024,12,18),
    date(2025,1,29),date(2025,3,19),date(2025,5,7),date(2025,6,18),date(2025,7,30),date(2025,9,17),date(2025,10,29),date(2025,12,17),
    date(2026,1,28),date(2026,3,18),date(2026,4,29),date(2026,6,17),date(2026,7,29),date(2026,9,16),date(2026,10,28),date(2026,12,16),
]
def _week_key(d): return d.isocalendar()[:2]
FOMC_WEEKS = {_week_key(d) for d in FOMC_DECISIONS}
def is_fomc_week(d): return _week_key(d) in FOMC_WEEKS

# ── Helpers ─────────────────────────────────────────────────────────────────────
def find_exp(dt):
    if dt in exps_set: return dt
    for e in exps:
        if 0 <= (e-dt).days <= 1: return e
    return None

def entry_bar(bars, start, end):
    """First bar whose ET (h,m) falls in [start,end)."""
    for bar in bars:
        db = datetime.fromtimestamp(bar["t"]/1000, tz=ET)
        t = (db.hour, db.minute)
        if start <= t < end:
            return bar, db
    return None, None

def vwap_at(bars, upto_t_ms):
    """Cumulative session VWAP through the entry bar (inclusive)."""
    pv = vol = 0.0
    for bar in bars:
        if bar["t"] > upto_t_ms: break
        tp = (bar["h"]+bar["l"]+bar["c"])/3.0
        pv += tp*bar["v"]; vol += bar["v"]
    return pv/vol if vol > 0 else 0.0

def build_put(spy_e, vix, ml, delta_t, width, r, iv):
    sk = skd(delta_t, spy_e, ml/TMINS, r, iv, "p", 60)
    lk = sk - width
    sc_bid, _ = opt(spy_e, sk, ml, vix, "p")
    _, lp_ask = opt(spy_e, lk, ml, vix, "p")
    return sk, lk, round(sc_bid - lp_ask, 4)

def build_call(spy_e, vix, ml, delta_t, width, r, iv):
    ck = skd(delta_t, spy_e, ml/TMINS, r, iv, "c", 60)
    lk = ck + width
    sc_bid, _ = opt(spy_e, ck, ml, vix, "c")
    _, lc_ask = opt(spy_e, lk, ml, vix, "c")
    return ck, lk, round(sc_bid - lc_ask, 4)

def put_debit(spx, sk, lk, ml, vix):
    sa, _ = opt(spx, sk, ml, vix, "p")
    _, lb = opt(spx, lk, ml, vix, "p")
    return max(sa - lb, 0)

def call_debit(spx, ck, lk, ml, vix):
    sa, _ = opt(spx, ck, ml, vix, "c")
    _, lb = opt(spx, lk, ml, vix, "c")
    return max(sa - lb, 0)

# ── Generic strategy runner ─────────────────────────────────────────────────────
def run_strategy(cfg):
    """cfg dict keys: days, start, end, vix_min, vix_max, delta, width,
       spread('put'|'ic'), keep(profit_target_pct), stop_mult, force_exit,
       skip_fomc, require_below_vwap, require_vix_falling."""
    trades = []
    for ds in dlist:
        dt = date.fromisoformat(ds)
        if dt.weekday() not in cfg["days"]: continue
        vix = vix_d.get(ds)
        if vix is None: continue
        if not (cfg["vix_min"] <= vix < cfg["vix_max"]): continue
        if cfg.get("skip_fomc") and is_fomc_week(dt): continue
        if cfg.get("require_vix_falling"):
            pv = prev_vix.get(ds)
            if pv is None or (pv - vix) <= 1.0: continue

        exp = find_exp(dt)
        if exp is None: continue
        bars = spy_5.get(ds, [])
        if len(bars) < 8: continue

        eb, dt_eb = entry_bar(bars, cfg["start"], cfg["end"])
        if eb is None: continue
        spy_e = eb["c"]

        if cfg.get("require_below_vwap"):
            vw = vwap_at(bars, eb["t"])
            if vw <= 0 or spy_e >= vw: continue

        r  = rfr(dt); iv = vix/100
        ml = max(16*60 - (dt_eb.hour*60+dt_eb.minute), 1)

        if cfg["spread"] == "put":
            sk, lk, credit = build_put(spy_e, vix, ml, cfg["delta"], cfg["width"], r, iv)
        else:  # iron condor
            sk, lk, pc = build_put(spy_e, vix, ml, cfg["delta"], cfg["width"], r, iv)
            ck, clk, cc = build_call(spy_e, vix, ml, cfg["delta"], cfg["width"], r, iv)
            credit = round(pc + cc, 4)

        if credit < MIN_CREDIT: continue

        keep   = cfg["keep"]
        tgt    = credit * (1 - keep)
        stp    = credit * cfg["stop_mult"]
        fexit  = cfg["force_exit"]
        eb_x = None; rsn = "EOD"

        for bar in bars:
            db = datetime.fromtimestamp(bar["t"]/1000, tz=ET)
            if db <= dt_eb: continue
            if (db.hour, db.minute) >= fexit: eb_x=bar; rsn="force"; break
            ml2 = max(16*60-(db.hour*60+db.minute), 1)
            if cfg["spread"] == "put":
                cur = put_debit(bar["c"], sk, lk, ml2, vix)
            else:
                cur = put_debit(bar["c"], sk, lk, ml2, vix) + call_debit(bar["c"], ck, clk, ml2, vix)
            if cur <= tgt: eb_x=bar; rsn="target"; break
            if cur >= stp: eb_x=bar; rsn="stop";   break

        if eb_x is None: eb_x = bars[-1]
        db_x = datetime.fromtimestamp(eb_x["t"]/1000, tz=ET)
        ml_x = max(16*60-(db_x.hour*60+db_x.minute), 1)
        if cfg["spread"] == "put":
            exit_d = put_debit(eb_x["c"], sk, lk, ml_x, vix)
        else:
            exit_d = put_debit(eb_x["c"], sk, lk, ml_x, vix) + call_debit(eb_x["c"], ck, clk, ml_x, vix)
        pnl = round((credit - exit_d) * 100, 2)
        trades.append({"date": ds, "pnl": pnl, "credit": credit, "vix": vix,
                       "rsn": rsn, "yr": ds[:4]})
    return trades

def calc_stats(trades):
    if not trades:
        return {"n":0,"wr":0,"total_pnl":0,"pf":0,"sharpe":0,"max_dd":0,"wins":0,"losses":0}
    wins   = [t for t in trades if t["pnl"]>0]
    losses = [t for t in trades if t["pnl"]<=0]
    gw = sum(t["pnl"] for t in wins); gl = sum(t["pnl"] for t in losses)
    wr = len(wins)/len(trades)
    eq=peak=mdd=0.
    for t in sorted(trades, key=lambda x:x["date"]):
        eq+=t["pnl"]; peak=max(peak,eq); mdd=max(mdd,peak-eq)
    by_d=defaultdict(float)
    for t in trades: by_d[t["date"]]+=t["pnl"]
    daily=list(by_d.values())
    if len(daily)>1:
        mu,sig=np.mean(daily),np.std(daily,ddof=1)
        sh=mu/sig*math.sqrt(252) if sig>0 else 0.
    else: sh=0.
    pf=abs(gw/gl) if gl<0 else float("inf")
    return {"n":len(trades),"wr":wr*100,"total_pnl":sum(t["pnl"] for t in trades),
            "pf":pf,"sharpe":sh,"max_dd":mdd,"wins":len(wins),"losses":len(losses)}

def split_stats(trades):
    oos = [t for t in trades if t["date"] >= SPLIT.isoformat() and not t["date"].startswith(str(BLIND))]
    bld = [t for t in trades if t["date"].startswith(str(BLIND))]
    ins = [t for t in trades if t["date"] < SPLIT.isoformat()]
    return calc_stats(trades), calc_stats(ins), calc_stats(oos), calc_stats(bld)

# ── Strategy definitions ─────────────────────────────────────────────────────────
STRATS = {
"E1": {"desc":"Monday morning low-VIX put spread","days":{0},"start":(10,0),"end":(10,30),
       "vix_min":13,"vix_max":18,"delta":0.20,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45)},
"E2": {"desc":"Tuesday low-VIX put spread","days":{1},"start":(10,0),"end":(10,30),
       "vix_min":13,"vix_max":18,"delta":0.20,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45)},
"E3": {"desc":"Wednesday low-VIX (non-FOMC) put spread","days":{2},"start":(10,0),"end":(10,15),
       "vix_min":13,"vix_max":18,"delta":0.20,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45),"skip_fomc":True},
"E4": {"desc":"Thursday low-VIX morning put spread","days":{3},"start":(9,45),"end":(10,15),
       "vix_min":13,"vix_max":18,"delta":0.20,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45)},
"E5": {"desc":"Friday low-VIX morning put spread","days":{4},"start":(9,45),"end":(10,15),
       "vix_min":13,"vix_max":18,"delta":0.20,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45)},
"E6": {"desc":"Daily afternoon decay put spread $3w","days":{0,1,2,3,4},"start":(14,0),"end":(14,30),
       "vix_min":13,"vix_max":20,"delta":0.15,"width":3.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45)},
"E7": {"desc":"Low-VIX iron condor daily $3w","days":{0,1,3,4},"start":(10,30),"end":(11,0),
       "vix_min":13,"vix_max":18,"delta":0.15,"width":3.0,"spread":"ic","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45)},
"E8": {"desc":"VIX spike recovery put spread (VIX falling >1pt proxy)","days":{0,1,2,3,4},"start":(10,0),"end":(11,0),
       "vix_min":18,"vix_max":22,"delta":0.25,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45),"require_vix_falling":True},
"E9": {"desc":"VWAP rejection put spread (SPY below VWAP)","days":{0,1,2,3,4},"start":(11,0),"end":(12,0),
       "vix_min":15,"vix_max":22,"delta":0.20,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":2.0,"force_exit":(15,45),"require_below_vwap":True},
"E10":{"desc":"End-of-day premium put spread tight 1.5x stop","days":{0,1,2,3},"start":(15,0),"end":(15,30),
       "vix_min":13,"vix_max":20,"delta":0.10,"width":2.0,"spread":"put","keep":0.75,
       "stop_mult":1.5,"force_exit":(15,55)},
}

# ── Run ───────────────────────────────────────────────────────────────────────
print("\nRunning E1-E10...\n")
results = {}
for sid, cfg in STRATS.items():
    t0 = time.time()
    trades = run_strategy(cfg)
    s_all, s_ins, s_oos, s_bld = split_stats(trades)
    results[sid] = {"cfg":cfg,"all":s_all,"ins":s_ins,"oos":s_oos,"bld":s_bld,"trades":trades}
    print(f"  {sid}: N={s_all['n']:>4} elapsed {time.time()-t0:.1f}s")

# ── Kill criteria ──────────────────────────────────────────────────────────────
def survives(r):
    o = r["oos"]
    if o["n"] < 20: return False, f"OOS N={o['n']} <20 (insufficient)"
    if o["wr"] < 60.0: return False, f"OOS WR {o['wr']:.1f}% <60%"
    if o["sharpe"] < 2.0: return False, f"OOS Sharpe {o['sharpe']:.2f} <2.0"
    return True, "PASS"

def pf_str(s):
    return "inf" if s["pf"]==float("inf") else f"{s['pf']:.2f}"

lines = ["="*78,
         "  EXPERIMENTAL STRATEGIES E1-E10 BACKTEST — Low-VIX Frequency Expansion",
         f"  Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
         "  Data: SPY 5-min + ThetaData expiries 2021-2026 | BS pricing IV=VIX/100",
         f"  BID_ASK={BID_ASK:.3f} ({'TEMPLATE/inflated' if BID_ASK < 0.15 else 'live-calibrated'}) "
         f"| MIN_CREDIT=${MIN_CREDIT:.2f} | Split <{SPLIT} IS / OOS / blind {BLIND}",
         "  Kill: OOS WR<60% OR OOS Sharpe<2.0 (also drop OOS N<20)",
         "="*78, ""]

hdr = f"  {'ID':<4}{'N':>5}{'WR':>7}{'P&L':>11}{'PF':>7}{'Sharpe':>8}  | {'oosN':>5}{'oosWR':>7}{'oosSh':>7} | {'bldN':>5}{'bldWR':>7}"
for scope in ["FULL SAMPLE + OOS + BLIND SUMMARY"]:
    lines += [f"  {scope}", "  "+"-"*74, hdr, "  "+"-"*74]
for sid in STRATS:
    r=results[sid]; a=r["all"]; o=r["oos"]; b=r["bld"]
    lines.append(f"  {sid:<4}{a['n']:>5}{a['wr']:>6.1f}%${a['total_pnl']:>+9.2f}{pf_str(a):>7}{a['sharpe']:>8.2f}  |"
                 f"{o['n']:>6}{o['wr']:>6.1f}%{o['sharpe']:>7.2f} |{b['n']:>6}{b['wr']:>6.1f}%")

lines += ["", "="*78, "  VERDICT (kill criteria applied to OOS)", "="*78, ""]
survivors=[]
for sid in STRATS:
    r=results[sid]
    ok,why=survives(r)
    tag="✅ SURVIVES" if ok else "❌ KILLED"
    lines.append(f"  {sid:<4} {tag:<12} {why:<34} {r['cfg']['desc']}")
    if ok: survivors.append(sid)

lines += ["", f"  Survivors: {survivors if survivors else 'NONE'}", "="*78]

# Per-strategy detail
lines += ["", "  PER-STRATEGY DETAIL (IS / OOS / BLIND)", "  "+"-"*74]
for sid in STRATS:
    r=results[sid]; c=r["cfg"]
    lines.append(f"\n  {sid} — {c['desc']}")
    lines.append(f"     days={sorted(c['days'])} window={c['start']}-{c['end']} VIX[{c['vix_min']},{c['vix_max']}) "
                 f"delta={c['delta']} width=${c['width']} {c['spread']} stop={c['stop_mult']}x")
    for nm,s in [("IS ",r["ins"]),("OOS",r["oos"]),("BLD",r["bld"]),("ALL",r["all"])]:
        lines.append(f"       {nm}: N={s['n']:>4} WR={s['wr']:>5.1f}% P&L=${s['total_pnl']:>+9.2f} "
                     f"PF={pf_str(s):>5} Sharpe={s['sharpe']:>6.2f} (W{s['wins']}/L{s['losses']})")

report="\n".join(lines)
print("\n"+report)
(OUT_DIR/"e1_e10_report.txt").write_text(report)
print(f"\nReport saved: {OUT_DIR/'e1_e10_report.txt'}")

import json
(OUT_DIR/"e1_e10_results.json").write_text(json.dumps(
    {sid:{"cfg":{k:(sorted(v) if isinstance(v,set) else v) for k,v in results[sid]['cfg'].items()},
          "all":results[sid]["all"],"ins":results[sid]["ins"],
          "oos":results[sid]["oos"],"bld":results[sid]["bld"]} for sid in STRATS},
    indent=2, default=str))
print(f"Results JSON saved: {OUT_DIR/'e1_e10_results.json'}")
print(f"\nSURVIVORS: {survivors}")
