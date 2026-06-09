#!/usr/bin/env python3
"""
vix_term_structure_backtest.py
==============================
Tests whether VIX term structure (VIX3M/VIX contango ratio) improves
credit spread WR and Sharpe when used as an entry filter.

Hypothesis: Trades entered on contango days (VIX3M/VIX >= 1.05) have
higher WR and Sharpe than baseline. Backwardation days (<1.05) are the
source of most stop-outs.

Data: VIX3M from CBOE (yfinance ^VIX3M), VIX from existing CSV cache.
Filter applied to R2 strategy (Mon/Wed/Fri put spread, VIX 13-22).
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
OUT_DIR  = BASE / "hermes_research"
LOG      = OUT_DIR / "vix_ts_output.log"
ET       = pytz.timezone("America/New_York")
TMINS    = 252 * 390

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
    best_k, best_d = S, 999.
    lo = S*0.85 if f=="p" else S
    hi = S if f=="p" else S*1.15
    for i in range(n):
        k = lo + (hi-lo)*i/n
        d = abs(bsd(S,k,T,r,iv,f))
        if abs(d-tgt) < best_d: best_d, best_k = abs(d-tgt), k
    return round(best_k/0.5)*0.5

def opt(S, K, ml, vix, f, sp=0.065):
    T = max(ml, 0.5) / TMINS
    r = 0.045
    iv = vix / 100
    mid = max(bsp(S,K,T,r,iv,f), 0.01)
    return mid*(1-sp/2), mid*(1+sp/2)

# ── Load data ─────────────────────────────────────────────────────────────────
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
print(f"  {len(dlist)} days loaded")

# ── Download or load VIX3M ────────────────────────────────────────────────────
vix3m_cache = DATA_DIR / "vix3m_daily.csv"

def download_vix3m():
    """Download ^VIX3M from yfinance and cache."""
    import yfinance as yf
    print("  Downloading VIX3M from yfinance...")
    df = yf.download("^VIX3M", start="2021-01-01", end="2026-06-01",
                     interval="1d", progress=False, auto_adjust=True)
    result = {}
    if not df.empty:
        df.columns = [c[0] if isinstance(c,tuple) else c for c in df.columns]
        for ts, row in df.iterrows():
            ds = ts.date().isoformat() if hasattr(ts,"date") else str(ts)[:10]
            result[ds] = float(row.get("Close", row.get("close", 0)))
    with vix3m_cache.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date","vix3m"])
        w.writeheader()
        for ds, v in sorted(result.items()):
            w.writerow({"date":ds,"vix3m":v})
    print(f"  VIX3M cached: {len(result)} days")
    return result

if vix3m_cache.exists():
    vix3m_d = {}
    with vix3m_cache.open() as f:
        for row in csv.DictReader(f):
            vix3m_d[row["date"]] = float(row["vix3m"])
    print(f"  VIX3M loaded from cache: {len(vix3m_d)} days")
else:
    vix3m_d = download_vix3m()

# ── Compute contango ratio ─────────────────────────────────────────────────────
# ratio = VIX3M / VIX  (>1 = contango/normal, <1 = backwardation/stress)
contango = {}
for ds in dlist:
    v1  = vix_d.get(ds, 0)
    v3m = vix3m_d.get(ds, 0)
    if v1 > 0 and v3m > 0:
        contango[ds] = v3m / v1
    else:
        contango[ds] = None

valid_days = [ds for ds in dlist if contango[ds] is not None]
ratios = [contango[ds] for ds in valid_days]
print(f"  Contango ratio: {len(valid_days)} days with data")
print(f"  Ratio range: {min(ratios):.3f} – {max(ratios):.3f}, avg {sum(ratios)/len(ratios):.3f}")
contango_days = sum(1 for r in ratios if r >= 1.05)
backw_days    = sum(1 for r in ratios if r < 1.00)
print(f"  Contango (>=1.05): {contango_days} days | Backwardation (<1.00): {backw_days} days")

# ── Run R2 strategy with and without filter ───────────────────────────────────
_RFR = {2021:.001,2022:.015,2023:.045,2024:.050,2025:.045,2026:.043}
rfr  = lambda d: _RFR.get(d.year, 0.045)
SPLIT = date(2023, 7, 1)
BLIND = 2025

def run_r2(dates, ts_filter=None):
    """
    ts_filter: None = no filter (baseline)
               'contango'      = only enter when ratio >= 1.05
               'strong_contango' = ratio >= 1.10
               'no_backwardation' = ratio >= 1.00
    """
    trades = []
    for ds in dates:
        dt = date.fromisoformat(ds)
        if dt.weekday() not in (0, 2, 4): continue
        vix = vix_d.get(ds, 16.)
        if not (13 <= vix <= 22): continue

        # Apply term structure filter
        if ts_filter is not None:
            ratio = contango.get(ds)
            if ratio is None: continue
            if ts_filter == "contango"        and ratio < 1.05: continue
            if ts_filter == "strong_contango" and ratio < 1.10: continue
            if ts_filter == "no_backwardation" and ratio < 1.00: continue

        exp = dt if dt in exps_set else None
        if exp is None:
            for e in exps:
                if 0 <= (e-dt).days <= 1: exp=e; break
        if exp is None: continue

        bars = spy_5.get(ds, [])
        if len(bars) < 8: continue

        eb = None
        for bar in bars:
            db = datetime.fromtimestamp(bar["t"]/1000, tz=ET)
            if db.hour == 10 and 30 <= db.minute < 60: eb=bar; break
        if eb is None: continue

        dt_eb = datetime.fromtimestamp(eb["t"]/1000, tz=ET)
        spy_e = eb["c"]
        r     = rfr(dt)
        iv    = vix / 100
        ml    = max(16*60 - (dt_eb.hour*60+dt_eb.minute), 1)
        T_e   = ml / TMINS
        sk    = skd(0.16, spy_e, T_e, r, iv, "p", 60)
        lk    = sk - 2.0

        sc_bid, _ = opt(spy_e, sk, ml, vix, "p")
        _, lp_ask = opt(spy_e, lk, ml, vix, "p")
        credit    = round(sc_bid - lp_ask, 4)
        if credit < max(0.12, vix * 0.009): continue

        tgt_e = credit * 0.25
        stp_e = credit * 1.75
        eb_x  = None; rsn = "EOD"

        for bar in bars:
            db = datetime.fromtimestamp(bar["t"]/1000, tz=ET)
            if db <= dt_eb: continue
            if (db.hour, db.minute) >= (15, 0): eb_x=bar; rsn="3PM"; break
            ml2     = max(16*60-(db.hour*60+db.minute), 1)
            sa, _   = opt(bar["c"], sk, ml2, vix, "p")
            _, lb   = opt(bar["c"], lk, ml2, vix, "p")
            cur     = max(sa-lb, 0)
            if cur <= tgt_e: eb_x=bar; rsn="target"; break
            if cur >= stp_e: eb_x=bar; rsn="stop";   break

        if eb_x is None: eb_x = bars[-1]
        db_x  = datetime.fromtimestamp(eb_x["t"]/1000, tz=ET)
        ml_x  = max(16*60-(db_x.hour*60+db_x.minute), 1)
        sa_x, _ = opt(eb_x["c"], sk, ml_x, vix, "p")
        _, lb_x = opt(eb_x["c"], lk, ml_x, vix, "p")
        exit_d  = max(sa_x - lb_x, 0)
        pnl     = round((credit - exit_d) * 100, 2)
        ratio_val = contango.get(ds, 0)

        trades.append({
            "date": ds, "pnl": pnl, "credit": credit, "vix": vix,
            "rsn": rsn, "ratio": ratio_val, "yr": ds[:4],
            "dow": ["Mon","Tue","Wed","Thu","Fri"][dt.weekday()]
        })
    return trades

def calc_stats(trades):
    if not trades:
        return {"n":0,"wr":0,"total_pnl":0,"pf":0,"sharpe":0,"max_dd":0}
    wins   = [t for t in trades if t["pnl"]>0]
    losses = [t for t in trades if t["pnl"]<=0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = sum(t["pnl"] for t in losses)
    wr     = len(wins)/len(trades)
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
    return {"n":len(trades),"wr":wr*100,"total_pnl":sum(t["pnl"] for t in trades),
            "pf":pf,"sharpe":sh,"max_dd":mdd,"wins":len(wins),"losses":len(losses)}

def pf_str(s):
    p = s["pf"]
    return "inf" if p==float("inf") else f"{p:.2f}"

# ── Run all variants ───────────────────────────────────────────────────────────
print("\nRunning backtest variants...")

variants = [
    ("Baseline (no filter)",    None),
    ("Contango only (>=1.05)",  "contango"),
    ("Strong contango (>=1.10)","strong_contango"),
    ("No backwardation (>=1.00)","no_backwardation"),
]

results = {}
for label, filt in variants:
    t0 = time.time()
    trades = run_r2(dlist, filt)
    oos    = [t for t in trades if t["date"] >= SPLIT.isoformat() and not t["date"].startswith(str(BLIND))]
    bld    = [t for t in trades if t["date"].startswith(str(BLIND))]
    results[label] = {"all":trades,"oos":oos,"bld":bld,
                      "s_all":calc_stats(trades),"s_oos":calc_stats(oos),"s_bld":calc_stats(bld)}
    print(f"  {label}: N={len(trades)}, elapsed {time.time()-t0:.1f}s")

# ── Stop-out analysis: where do losses cluster by ratio? ─────────────────────
print("\nStop-out ratio analysis (baseline trades)...")
base_trades = results["Baseline (no filter)"]["all"]
buckets = {"backwardation (<1.00)":[], "neutral (1.00-1.05)":[], "contango (1.05-1.10)":[], "strong contango (>=1.10)":[]}
for t in base_trades:
    r = t.get("ratio") or 0
    if r < 1.00:   buckets["backwardation (<1.00)"].append(t)
    elif r < 1.05: buckets["neutral (1.00-1.05)"].append(t)
    elif r < 1.10: buckets["contango (1.05-1.10)"].append(t)
    else:          buckets["strong contango (>=1.10)"].append(t)

# ── Year breakdown for contango filter ───────────────────────────────────────
contango_trades = results["Contango only (>=1.05)"]["all"]
by_yr = defaultdict(list)
for t in contango_trades: by_yr[t["yr"]].append(t)

# ── Write report ──────────────────────────────────────────────────────────────
lines = [
    "="*65,
    "  VIX TERM STRUCTURE BACKTEST — SPY 0DTE Credit Spread",
    f"  Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    "="*65,
    "",
    "  Hypothesis: Trading only in VIX contango (VIX3M/VIX >= 1.05)",
    "  improves WR and Sharpe by avoiding stress-regime entries.",
    "",
    f"  VIX3M data: {len(vix3m_d)} days",
    f"  Contango days (>=1.05): {contango_days}  |  Backwardation (<1.00): {backw_days}",
    "",
    "="*65,
    "  VARIANT COMPARISON (R2 strategy, Mon/Wed/Fri, VIX 13-22)",
    "="*65,
    f"  {'Variant':<35} {'N':>5} {'WR':>7} {'P&L':>10} {'PF':>6} {'Sharpe':>7}",
    "  " + "-"*62,
]

for label, _ in variants:
    r = results[label]
    s = r["s_all"]
    lines.append(f"  {label:<35} {s['n']:>5} {s['wr']:>6.1f}% ${s['total_pnl']:>+9.2f} {pf_str(s):>6} {s['sharpe']:>7.2f}")

lines += [
    "",
    "  OUT-OF-SAMPLE (Jul 2023–2024, excl 2025 blind):",
    f"  {'Variant':<35} {'N':>5} {'WR':>7} {'P&L':>10} {'Sharpe':>7}",
    "  " + "-"*55,
]
for label, _ in variants:
    r = results[label]
    s = r["s_oos"]
    lines.append(f"  {label:<35} {s['n']:>5} {s['wr']:>6.1f}% ${s['total_pnl']:>+9.2f} {s['sharpe']:>7.2f}")

lines += [
    "",
    "  BLIND 2025 (held-out year):",
    f"  {'Variant':<35} {'N':>5} {'WR':>7} {'P&L':>10} {'Sharpe':>7}",
    "  " + "-"*55,
]
for label, _ in variants:
    r = results[label]
    s = r["s_bld"]
    lines.append(f"  {label:<35} {s['n']:>5} {s['wr']:>6.1f}% ${s['total_pnl']:>+9.2f} {s['sharpe']:>7.2f}")

lines += [
    "",
    "="*65,
    "  STOP-OUT ANALYSIS — Where do losses cluster by contango ratio?",
    "="*65,
    f"  {'Regime':<30} {'N':>5} {'WR':>7} {'P&L':>10} {'Stops':>6}",
    "  " + "-"*57,
]
for bucket, ts in sorted(buckets.items()):
    s  = calc_stats(ts)
    stops = sum(1 for t in ts if t["rsn"]=="stop")
    lines.append(f"  {bucket:<30} {s['n']:>5} {s['wr']:>6.1f}% ${s['total_pnl']:>+9.2f} {stops:>6}")

lines += [
    "",
    "="*65,
    "  CONTANGO FILTER — Year-by-Year Breakdown",
    "="*65,
    f"  {'Year':<8} {'N':>5} {'WR':>7} {'P&L':>10}",
    "  " + "-"*35,
]
for yr in sorted(by_yr.keys()):
    ys = calc_stats(by_yr[yr])
    lines.append(f"  {yr:<8} {ys['n']:>5} {ys['wr']:>6.1f}% ${ys['total_pnl']:>+9.2f}")

# Verdict
base_oos = results["Baseline (no filter)"]["s_oos"]
cont_oos  = results["Contango only (>=1.05)"]["s_oos"]
wr_delta  = cont_oos["wr"] - base_oos["wr"]
sh_delta  = cont_oos["sharpe"] - base_oos["sharpe"]
n_lost    = base_oos["n"] - cont_oos["n"]

lines += ["", "="*65, "  VERDICT", "="*65, ""]
if wr_delta >= 2.0 and sh_delta >= 0.5:
    verdict = "CONFIRMED — Contango filter adds meaningful edge."
    rec     = "ADD to all credit spread strategies as entry gate."
elif wr_delta >= 0 and sh_delta >= 0:
    verdict = "MARGINAL — Small improvement, trades lost may not be worth it."
    rec     = "OPTIONAL — add only if backwardation days show clear loss clustering."
else:
    verdict = "NOT CONFIRMED — Filter hurts by removing good trades."
    rec     = "SKIP — term structure is not a useful filter for this strategy."

lines += [
    f"  WR delta (contango vs baseline, OOS): {wr_delta:+.1f}%",
    f"  Sharpe delta: {sh_delta:+.2f}",
    f"  Trades removed by filter (OOS): {n_lost}",
    "",
    f"  {verdict}",
    f"  Recommendation: {rec}",
    "",
    "="*65,
]

report = "\n".join(lines)
print("\n" + report)

report_path = OUT_DIR / "vix_ts_report.txt"
report_path.write_text(report)
print(f"\n  Report saved: {report_path}")

# Telegram summary
msg = "📊 *VIX Term Structure Backtest Complete*\n\n"
msg += f"Contango filter (>=1.05) vs baseline (OOS):\n"
msg += f"  Baseline:  N={base_oos['n']} WR={base_oos['wr']:.1f}% Sharpe={base_oos['sharpe']:.2f}\n"
msg += f"  Contango:  N={cont_oos['n']} WR={cont_oos['wr']:.1f}% Sharpe={cont_oos['sharpe']:.2f}\n"
msg += f"  WR delta: {wr_delta:+.1f}% | Sharpe delta: {sh_delta:+.2f}\n\n"
msg += f"*Verdict:* {verdict}\n"
msg += f"*Rec:* {rec}"
tg(msg)
