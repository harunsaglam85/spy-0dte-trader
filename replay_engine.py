#!/usr/bin/env python3
"""
replay_engine.py  (v2) — Historical replay & discovery on REAL option bid/ask.
==============================================================================

WHAT CHANGED FROM v1
--------------------
v1 priced every option leg with Black-Scholes (calibrated to a corrupted live
trade log) and was therefore a MODEL-DRIVEN backtest. v2 replaces all pricing
with REAL historical tick bid/ask pulled from the local ThetaData terminal:

    GET http://localhost:25503/v3/option/history/quote
        ?symbol=SPY&expiration=YYYYMMDD&strike=<int>&right=PUT|CALL
        &start_date=YYYYMMDD&end_date=YYYYMMDD

which returns tick-by-tick CSV (timestamp, bid, ask, ...). These are real,
exchange-sourced quotes — no Black-Scholes anywhere in the P&L.

DATA PROVENANCE — what is REAL:
  REAL option bid/ask (ThetaData /v3/option/history/quote, cached to disk):
    - entry credit   = Σ(short legs @ real bid) − Σ(long legs @ real ask)
    - exit close-cost = Σ(short legs @ real ask) − Σ(long legs @ real bid)
    - every credit / exit / P&L / WR / PF below is from real fills.
  REAL market context (Tradier, cached):
    - SPY 5-min OHLCV + per-bar VWAP        (/markets/timesales)
    - VIX 5-min intraday                     (/markets/timesales)
    - VIX3M daily OHLC -> contango ratio     (/markets/history)
  REAL ground truth (the "what actually happened" half):
    - /root/hermes_system/trades/*.json      (actual fills the live engine booked)

THE ONE REMAINING MODEL — strike SELECTION only (never price):
  To map a discovery "delta target" to a concrete $1 SPY strike we still use a
  Black-Scholes delta approximation (real VIX as sigma, real minutes-to-expiry).
  This chooses *which* strike to quote; it does NOT set any price. Once the
  strike is chosen, 100% of the pricing is real ThetaData bid/ask.

DAYS REPLAYED: 2026-06-09, -11, -12, -15, -16, -17.
  Jun 8 excluded (Day-1 credit-parsing artifacts); Jun 10 excluded (engine was
  crash-looping that day, confirmed from journalctl).

Run:  python3 /root/spy-0dte-trader/replay_engine.py
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import bisect
import csv
import io
import json
import math
import time
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import requests

warnings.filterwarnings("ignore")

# py_vollib delta is used ONLY to pick which strike sits nearest a delta target.
# It never prices a leg — all pricing below is real ThetaData bid/ask.
from py_vollib.black_scholes.greeks.analytical import delta as _pvd

# ── Paths ────────────────────────────────────────────────────────────────────
BASE       = Path("/root/spy-0dte-trader")
CACHE_DIR  = BASE / "backtest_data" / "replay_cache"
OPT_CACHE  = BASE / "backtest_data" / "theta_quote_cache"
TRADES_DIR = Path("/root/hermes_system/trades")
RESEARCH   = Path("/root/hermes_system/research")
REPORT_MD  = RESEARCH / "replay_june9_17_real.md"
CAND_JSON  = RESEARCH / "replay_candidates_real.json"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OPT_CACHE.mkdir(parents=True, exist_ok=True)
RESEARCH.mkdir(parents=True, exist_ok=True)

# ── Window (Jun 8 & Jun 10 deliberately excluded — see module docstring) ──────
TRADING_DAYS = ["2026-06-09", "2026-06-11", "2026-06-12",
                "2026-06-15", "2026-06-16", "2026-06-17"]
DOW = {"2026-06-09": 1, "2026-06-11": 3, "2026-06-12": 4,
       "2026-06-15": 0, "2026-06-16": 1, "2026-06-17": 2}  # Mon=0
DOW_NAME = ["Mon", "Tue", "Wed", "Thu", "Fri"]

# ── Constants ────────────────────────────────────────────────────────────────
TMINS              = 252 * 390     # annualised trading minutes (for strike-pick T only)
RFR                = 0.043
MIN_CREDIT         = 0.20          # engine's credit gate
CONTANGO_THRESHOLD = 1.05
CLOSE_MIN          = 16 * 60       # 0DTE expiry = 16:00 ET in minutes-of-day
FORCE_EXIT_MIN     = 15 * 60 + 45  # force flat at 15:45 ET
GLOBAL_VIX_FLOOR   = 18.0          # engine enforces max(cfg.vix_min, 18) at entry

# ── ThetaData (local terminal) ───────────────────────────────────────────────
THETA_BASE = "http://localhost:25503"

# ── Tradier ──────────────────────────────────────────────────────────────────
TRADIER_BASE = "https://api.tradier.com/v1"


def _load_env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


ENV = _load_env()
TRADIER_KEY = ENV.get("TRADIER_API_KEY", "")


def tg_send(msg):
    tok = ENV.get("TELEGRAM_BOT_TOKEN", "")
    chat = ENV.get("TELEGRAM_CHAT_ID", "")
    if not (tok and chat):
        print("  [telegram] no credentials — skipped")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                          timeout=10)
        if not r.ok:
            print(f"  [telegram] send failed: {r.status_code} {r.text[:160]}")
            return False
        return True
    except Exception as exc:
        print(f"  [telegram] error: {exc}")
        return False


def _tradier(endpoint, params):
    headers = {"Authorization": f"Bearer {TRADIER_KEY}", "Accept": "application/json"}
    for attempt in range(3):
        try:
            r = requests.get(f"{TRADIER_BASE}{endpoint}", headers=headers,
                             params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"  [tradier {endpoint} attempt {attempt+1}/3] {exc}")
            time.sleep(2 * (attempt + 1))
    return {}


# ── Tradier market-data layer (fetch once, cache to disk) ─────────────────────

def _cache_load(name):
    p = CACHE_DIR / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _cache_save(name, obj):
    (CACHE_DIR / name).write_text(json.dumps(obj))


def fetch_intraday(symbol, day):
    cache_name = f"{symbol}_5min_{day}.json"
    cached = _cache_load(cache_name)
    if cached is not None:
        return cached
    data = _tradier("/markets/timesales", params={
        "symbol": symbol, "interval": "5min",
        "start": f"{day} 09:30", "end": f"{day} 16:00", "session_filter": "open",
    })
    bars = (data.get("series") or {}).get("data") or []
    if isinstance(bars, dict):
        bars = [bars]
    out = []
    for b in bars:
        t = b.get("time", "")
        hh, mm = (int(t[11:13]), int(t[14:16])) if len(t) >= 16 else (0, 0)
        out.append({
            "t": t, "minute": hh * 60 + mm,
            "open": float(b.get("open") or 0), "high": float(b.get("high") or 0),
            "low": float(b.get("low") or 0), "close": float(b.get("close") or 0),
            "volume": float(b.get("volume") or 0), "vwap": float(b.get("vwap") or 0),
        })
    _cache_save(cache_name, out)
    time.sleep(1.5)
    return out


def fetch_daily(symbol):
    cache_name = f"{symbol}_daily.json"
    cached = _cache_load(cache_name)
    if cached is not None:
        return cached
    data = _tradier("/markets/history", params={
        "symbol": symbol, "interval": "daily",
        "start": TRADING_DAYS[0], "end": TRADING_DAYS[-1],
    })
    days = (data.get("history") or {}).get("day") or []
    if isinstance(days, dict):
        days = [days]
    out = {d["date"]: float(d.get("close") or 0) for d in days}
    _cache_save(cache_name, out)
    time.sleep(1.5)
    return out


def load_market_data():
    print("Loading real market data (Tradier, cached)…")
    vix3m_daily = fetch_daily("VIX3M")
    vix_daily   = fetch_daily("VIX")
    md = {}
    for day in TRADING_DAYS:
        spy = fetch_intraday("SPY", day)
        vix = fetch_intraday("VIX", day)
        vix_by_min = {b["minute"]: b["close"] for b in vix if b["close"] > 0}
        if not spy:
            print(f"  {day}: NO SPY bars (market holiday or feed gap) — skipped")
            continue
        cum_pv = cum_v = 0.0
        for b in spy:
            cum_pv += b["vwap"] * b["volume"]
            cum_v  += b["volume"]
            b["session_vwap"] = round(cum_pv / cum_v, 4) if cum_v else b["close"]
        md[day] = {
            "spy": spy,
            "vix_by_min": vix_by_min,
            "vix3m": vix3m_daily.get(day, 0.0),
            "vix_daily": vix_daily.get(day, 0.0),
        }
        v_open, v_close = spy[0]["close"], spy[-1]["close"]
        print(f"  {day} ({DOW_NAME[DOW[day]]}): {len(spy)} SPY bars, "
              f"SPY {v_open:.2f}->{v_close:.2f}, VIX~{(list(vix_by_min.values()) or [0])[0]:.2f}, "
              f"VIX3M {md[day]['vix3m']:.2f}")
    return md


def vix_at(md_day, minute):
    vbm = md_day["vix_by_min"]
    cands = [m for m in vbm if m <= minute]
    if cands:
        return vbm[max(cands)]
    if vbm:
        return vbm[min(vbm)]
    return md_day["vix_daily"]


def contango_ratio(md_day, vix_now):
    v3m = md_day["vix3m"]
    return (v3m / vix_now) if (v3m > 0 and vix_now > 0) else 0.0


# ── REAL option quotes (ThetaData historical tick → per-minute bid/ask) ───────
# We fetch the full session of ticks for one (strike, right, day) ONCE, then
# collapse to one quote per minute (the last valid tick in that minute). A tick
# is "valid" when ask > 0 and ask >= bid (deep-OTM legs legitimately have bid=0).
# Cached both on disk and in-process so the discovery sweep never re-fetches.

_OPT_MEM = {}          # key -> {"keys": [minute,...], "q": {minute: [bid, ask]}}
_FETCH_COUNT = 0


def _opt_key(strike, right, date_yyyymmdd):
    return f"SPY_{date_yyyymmdd}_{int(strike)}_{right}"


def get_option_series(strike, right, date_yyyymmdd):
    """Return {"keys": sorted minutes, "q": {minute:[bid,ask]}} of real per-minute
    quotes for one 0DTE leg, or None if ThetaData has no usable quotes."""
    global _FETCH_COUNT
    key = _opt_key(strike, right, date_yyyymmdd)
    if key in _OPT_MEM:
        return _OPT_MEM[key]
    disk = OPT_CACHE / f"{key}.json"
    if disk.exists():
        try:
            obj = json.loads(disk.read_text())
            obj = None if obj is None else {"keys": [int(k) for k in obj["keys"]],
                                            "q": {int(k): v for k, v in obj["q"].items()}}
            _OPT_MEM[key] = obj
            return obj
        except Exception:
            pass

    params = {
        "symbol": "SPY", "expiration": date_yyyymmdd, "strike": int(strike),
        "right": right, "start_date": date_yyyymmdd, "end_date": date_yyyymmdd,
    }
    text = None
    for attempt in range(3):
        try:
            r = requests.get(f"{THETA_BASE}/v3/option/history/quote",
                             params=params, timeout=30)
            r.raise_for_status()
            text = r.text
            break
        except Exception as exc:
            print(f"  [theta {key} attempt {attempt+1}/3] {exc}")
            time.sleep(1.5 * (attempt + 1))
    _FETCH_COUNT += 1

    obj = None
    if text:
        qmap = {}
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            try:
                ts = row.get("timestamp", "")
                bid = float(row.get("bid") or 0)
                ask = float(row.get("ask") or 0)
            except Exception:
                continue
            if ask <= 0 or ask < bid or len(ts) < 16:
                continue
            try:
                minute = int(ts[11:13]) * 60 + int(ts[14:16])
            except Exception:
                continue
            qmap[minute] = [round(bid, 4), round(ask, 4)]   # last valid tick in minute
        if qmap:
            obj = {"keys": sorted(qmap), "q": qmap}

    # Persist (write null for "no data" so we don't re-hit a dead strike).
    try:
        disk.write_text(json.dumps(
            None if obj is None else {"keys": obj["keys"], "q": obj["q"]}))
    except Exception:
        pass
    _OPT_MEM[key] = obj
    return obj


def quote_at(series, minute):
    """Last real quote at-or-before `minute`. Returns [bid, ask] or None."""
    if not series:
        return None
    keys = series["keys"]
    i = bisect.bisect_right(keys, minute) - 1
    if i < 0:
        return None
    return series["q"][keys[i]]


# ── Strike selection (BS delta — chooses WHICH strike, never the price) ───────

def _bs_delta(S, K, T, r, sigma, flag):
    if T <= 1e-7 or sigma <= 1e-6:
        if flag == "c":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    try:
        return float(_pvd(flag, S, K, T, r, sigma))
    except Exception:
        return 0.0


_STRIKE_MEMO = {}


def strike_at_delta(target, S, minutes_left, vix, flag, n=80):
    """Integer $1 SPY strike whose |BS delta| is nearest `target`."""
    mk = (round(target, 3), round(S, 1), int(minutes_left // 5),
          round(vix, 1), flag)
    if mk in _STRIKE_MEMO:
        return _STRIKE_MEMO[mk]
    T  = max(minutes_left, 0.5) / TMINS
    iv = max(vix / 100.0, 0.01)
    lo = S * 0.85 if flag == "p" else S
    hi = S if flag == "p" else S * 1.15
    best_k, best_err = int(round(S)), 9e9
    for i in range(n + 1):
        k = int(round(lo + (hi - lo) * i / n))
        err = abs(abs(_bs_delta(S, k, T, RFR, iv, flag)) - target)
        if err < best_err:
            best_err, best_k = err, k
    _STRIKE_MEMO[mk] = best_k
    return best_k


# ── Structure definition ──────────────────────────────────────────────────────
# A structure is a list of legs (right, strike, side): side +1 short, -1 long.
# All structures below are NET CREDIT (we sell premium):
#   entry credit  = Σ short@bid − Σ long@ask
#   close cost    = Σ short@ask − Σ long@bid   (debit to flatten)

def build_legs(stype, S, minutes_left, vix, delta_target, width):
    """Return list of (right, strike, side) or None if unbuildable."""
    atm = int(round(S))
    if stype == "put_spread":
        sk = strike_at_delta(delta_target, S, minutes_left, vix, "p")
        return [("PUT", sk, +1), ("PUT", int(sk - width), -1)]
    if stype == "call_spread":
        sk = strike_at_delta(delta_target, S, minutes_left, vix, "c")
        return [("CALL", sk, +1), ("CALL", int(sk + width), -1)]
    if stype == "iron_condor":
        ps = strike_at_delta(delta_target, S, minutes_left, vix, "p")
        cs = strike_at_delta(delta_target, S, minutes_left, vix, "c")
        return [("PUT", ps, +1), ("PUT", int(ps - width), -1),
                ("CALL", cs, +1), ("CALL", int(cs + width), -1)]
    if stype == "straddle":          # short ATM put + short ATM call
        return [("PUT", atm, +1), ("CALL", atm, +1)]
    if stype == "strangle":          # short OTM put + short OTM call (at delta)
        pk = strike_at_delta(delta_target, S, minutes_left, vix, "p")
        ck = strike_at_delta(delta_target, S, minutes_left, vix, "c")
        return [("PUT", pk, +1), ("CALL", ck, +1)]
    return None


def _entry_credit(leg_series, minute):
    """Real net credit at `minute`: short@bid − long@ask. None if any leg has no quote."""
    credit = 0.0
    for side, series in leg_series:
        q = quote_at(series, minute)
        if q is None:
            return None
        bid, ask = q
        credit += bid if side > 0 else -ask
    return round(credit, 4)


def _close_cost(leg_series, minute):
    """Real debit to flatten at `minute`: short@ask − long@bid. None if any leg missing."""
    cost = 0.0
    for side, series in leg_series:
        q = quote_at(series, minute)
        if q is None:
            return None
        bid, ask = q
        cost += ask if side > 0 else -bid
    return round(cost, 4)


# ── Trade simulation (REAL fills) ─────────────────────────────────────────────

_TRADE_MEMO = {}


def simulate_trade(md_day, day, stype, entry_minute, delta_target, width,
                   profit_target_pct, stop_multiple, force_exit_minute,
                   contracts=1):
    """Cached wrapper — same (day, request args) resolves identically across the sweep."""
    tk = (day, stype, entry_minute, delta_target, width,
          profit_target_pct, stop_multiple, force_exit_minute, contracts)
    if tk in _TRADE_MEMO:
        r = _TRADE_MEMO[tk]
        return dict(r) if r is not None else None
    r = _simulate_trade_impl(md_day, day, stype, entry_minute, delta_target, width,
                             profit_target_pct, stop_multiple, force_exit_minute, contracts)
    _TRADE_MEMO[tk] = r
    return dict(r) if r is not None else None


def _simulate_trade_impl(md_day, day, stype, entry_minute, delta_target, width,
                         profit_target_pct, stop_multiple, force_exit_minute,
                         contracts=1):
    """Open at first bar >= entry_minute using real bid/ask, then scan real quote
    history forward. profit_target_pct = fraction of credit captured (0.75 => close
    when cost <= 25% of credit). Returns a trade dict or None."""
    bars = md_day["spy"]
    ent = next((b for b in bars if b["minute"] >= entry_minute), None)
    if ent is None:
        return None
    S0   = ent["close"]
    ml0  = max(CLOSE_MIN - ent["minute"], 1)
    vix0 = vix_at(md_day, ent["minute"])
    legs = build_legs(stype, S0, ml0, vix0, delta_target, width)
    if not legs:
        return None

    expiration = day.replace("-", "")
    leg_series = []
    for (right, strike, side) in legs:
        s = get_option_series(strike, right, expiration)
        if s is None:
            return None        # no real quotes for this leg → can't trade it
        leg_series.append((side, s))

    em = ent["minute"]
    credit = _entry_credit(leg_series, em)
    if credit is None or credit < MIN_CREDIT:
        return None            # no entry quote, or below the $0.20 gate

    profit_at = credit * (1 - profit_target_pct)   # close-cost target
    stop_at   = credit * stop_multiple

    # Scan every real per-minute quote after entry, up to the 15:45 force-exit.
    minutes = sorted({m for (_, s) in leg_series for m in s["keys"]
                      if em < m <= force_exit_minute})
    exit_minute = exit_cost = None
    reason = "force"
    for m in minutes:
        cost = _close_cost(leg_series, m)
        if cost is None:
            continue
        if m >= force_exit_minute:
            exit_minute, exit_cost, reason = m, cost, "force"
            break
        if cost <= profit_at:
            exit_minute, exit_cost, reason = m, cost, "target"
            break
        if cost >= stop_at:
            exit_minute, exit_cost, reason = m, cost, "stop"
            break
    if exit_minute is None:
        # No tick reached the force minute (rare) — flatten at last available quote.
        cost = _close_cost(leg_series, force_exit_minute)
        if cost is None:
            last_m = max(m for (_, s) in leg_series for m in s["keys"])
            cost = _close_cost(leg_series, last_m)
            exit_minute = last_m
        else:
            exit_minute = force_exit_minute
        exit_cost, reason = cost, "force"
    if exit_cost is None:
        return None

    pnl = round((credit - exit_cost) * 100 * contracts, 2)
    strikes = "/".join(f"{r[0]}{s}{'+' if sd > 0 else '-'}" for (r, s, sd) in legs)
    return {
        "entry_minute": em, "exit_minute": exit_minute,
        "hold": exit_minute - em, "credit": round(credit, 3),
        "exit_cost": round(exit_cost, 3), "reason": reason, "pnl": pnl,
        "spy_entry": round(S0, 2), "vix_entry": round(vix0, 2),
        "spy_exit": round(bars[-1]["close"], 2), "strikes": strikes,
    }


# ── Stats & kill criteria ─────────────────────────────────────────────────────

def stats(trades):
    pnls = [t["pnl"] for t in trades]
    if not pnls:
        return {"n": 0, "wr": 0, "pnl": 0, "pf": 0, "sharpe": 0,
                "avg_win": 0, "max_loss": 0, "avg_credit": 0}
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), sum(loss)
    mu, sig = np.mean(pnls), (np.std(pnls, ddof=1) if len(pnls) > 1 else 0.0)
    sharpe = (mu / sig * math.sqrt(252)) if sig > 0 else 0.0
    return {
        "n": len(pnls), "wr": round(100 * len(wins) / len(pnls), 1),
        "pnl": round(sum(pnls), 2),
        "pf": round(abs(gw / gl), 2) if gl < 0 else float("inf"),
        "sharpe": round(sharpe, 2),
        "avg_win": round(np.mean(wins), 2) if wins else 0.0,
        "max_loss": round(min(pnls), 2),
        "avg_credit": round(np.mean([t["credit"] for t in trades]), 3),
    }


# Kill: WR<65, PF<1.5, N<5, or max single loss > 3x avg win.
MIN_TRADES = 5
MIN_WR     = 65.0


def passes_kill_criteria(s):
    reasons = []
    if s["n"] < MIN_TRADES:
        reasons.append(f"n={s['n']}<{MIN_TRADES}")
    if s["wr"] < MIN_WR:
        reasons.append(f"WR={s['wr']}<{MIN_WR:.0f}")
    if s["pf"] < 1.5:
        reasons.append(f"PF={s['pf']}<1.5")
    if s["avg_win"] > 0 and abs(s["max_loss"]) > 3 * s["avg_win"]:
        reasons.append(f"maxloss {abs(s['max_loss'])}>3x avgwin {s['avg_win']}")
    return (len(reasons) == 0), reasons


# ── Known-strategy configs (mirror execution_engine STRATEGIES subset) ────────
KNOWN = {
    "R3A":  dict(days={0}, win=(10, 15, 11, 0), stype="put_spread", vmin=15, vmax=22,
                 delta=0.20, ptp=0.75, stop=2.0, fx=(15, 45), width=2.0, contracts=4),
    "R3B":  dict(days={2}, win=(10, 30, 10, 45), stype="put_spread", vmin=15, vmax=22,
                 delta=0.20, ptp=0.75, stop=2.0, fx=(15, 45), width=2.0, contracts=3),
    "R3D":  dict(days={0, 2, 4}, win=(10, 15, 11, 0), stype="put_spread", vmin=15, vmax=22,
                 delta=0.20, ptp=0.75, stop=2.0, fx=(15, 45), width=2.0, contracts=3),
    "R8":   dict(days={4}, win=(13, 0, 13, 30), stype="put_spread", vmin=15, vmax=22,
                 delta=0.20, ptp=0.70, stop=1.8, fx=(15, 30), width=2.0, contracts=3,
                 require="spy_above_vwap"),
    "T7_high_vix": dict(days={0, 1, 2, 3, 4}, win=(10, 0, 11, 0), stype="put_spread",
                 vmin=20, vmax=28, delta=0.15, ptp=0.75, stop=2.0, fx=(15, 45),
                 width=2.0, contracts=1),
    "T12_max_data": dict(days={0, 1, 2, 3, 4}, win=(10, 0, 14, 0), stype="put_spread",
                 vmin=14, vmax=24, delta=0.20, ptp=0.75, stop=2.0, fx=(15, 45),
                 width=2.0, contracts=1),
    "T14_vix_transition": dict(days={0, 1, 2, 3, 4}, win=(10, 0, 14, 0), stype="put_spread",
                 vmin=17, vmax=20, delta=0.20, ptp=0.75, stop=2.0, fx=(15, 45),
                 width=2.0, contracts=1, require="vix_falling"),
    "E7_lowvix_ic": dict(days={0, 1, 3, 4}, win=(10, 30, 11, 0), stype="iron_condor",
                 vmin=13, vmax=18, delta=0.15, ptp=0.75, stop=2.0, fx=(15, 45),
                 width=3.0, contracts=1),
}


def first_eligible_entry(md_day, day, cfg, apply_floor=True):
    """First bar passing all real-data filters (VIX / contango / VWAP), or
    (None, reason). Filters use real Tradier data."""
    if DOW[day] not in cfg["days"]:
        return None, "dow"
    eff_vmin = max(cfg["vmin"], GLOBAL_VIX_FLOOR) if apply_floor else cfg["vmin"]
    h1, m1, h2, m2 = cfg["win"]
    wlo, whi = h1 * 60 + m1, h2 * 60 + m2
    bars = md_day["spy"]
    vix_open = vix_at(md_day, wlo)
    for b in bars:
        if not (wlo <= b["minute"] < whi):
            continue
        vix = vix_at(md_day, b["minute"])
        if contango_ratio(md_day, vix) < CONTANGO_THRESHOLD:
            continue
        if vix < eff_vmin or vix >= cfg["vmax"]:
            continue
        req = cfg.get("require")
        if req == "spy_above_vwap" and not (b["close"] > b["session_vwap"]):
            continue
        if req == "vix_falling" and not (vix < vix_open):
            continue
        return b["minute"], "ok"
    vix = vix_at(md_day, wlo)
    if contango_ratio(md_day, vix) < CONTANGO_THRESHOLD:
        return None, "backwardation"
    if vix < eff_vmin:
        return None, f"vix<{eff_vmin:.0f}"
    if vix >= cfg["vmax"]:
        return None, f"vix>={cfg['vmax']:.0f}"
    return None, "filter"


def replay_known(md):
    out = {}
    for name, cfg in KNOWN.items():
        trades, notes = [], []
        for day in TRADING_DAYS:
            if day not in md:
                continue
            em, why = first_eligible_entry(md[day], day, cfg)
            if em is None:
                if why != "dow":
                    notes.append(f"{day}:{why}")
                continue
            fx = cfg["fx"][0] * 60 + cfg["fx"][1]
            tr = simulate_trade(md[day], day, cfg["stype"], em, cfg["delta"], cfg["width"],
                                cfg["ptp"], cfg["stop"], fx, cfg["contracts"])
            if tr is None:
                notes.append(f"{day}:no-credit")
                continue
            tr["day"] = day
            trades.append(tr)
        out[name] = {"trades": trades, "stats": stats(trades), "notes": notes, "cfg": cfg}
    return out


# ── Discovery grid (unconstrained, REAL pricing) ──────────────────────────────

DISC_VIX     = [(13, 15), (15, 17), (17, 19), (19, 22), (13, 22)]
DISC_WINDOWS = [(9, 45, 10, 0), (10, 0, 10, 30), (10, 30, 11, 0),
                (11, 0, 12, 0), (13, 0, 14, 0), (14, 0, 15, 0)]
DISC_DELTAS  = [0.10, 0.15, 0.20, 0.25, 0.30]
DISC_WIDTHS  = [1.0, 2.0, 3.0, 5.0]
DISC_TYPES   = ["put_spread", "call_spread", "iron_condor", "straddle", "strangle"]
DISC_DOWSETS = [("all", {0, 1, 2, 3, 4}), ("Mon", {0}), ("Tue", {1}),
                ("Wed", {2}), ("Thu", {3}), ("Fri", {4})]


def run_discovery(md, apply_floor=False):
    """Unconstrained grid on REAL option pricing. apply_floor=False so the sub-18
    VIX regimes the live engine blocks are explorable (EXPLORATORY only)."""
    candidates, combos = [], 0
    for stype in DISC_TYPES:
        for (vmin, vmax) in DISC_VIX:
            for (h1, m1, h2, m2) in DISC_WINDOWS:
                for delta in DISC_DELTAS:
                    for width in DISC_WIDTHS:
                        # straddle is ATM (delta/width irrelevant) → run once;
                        # strangle uses delta but not width → run once per delta.
                        if stype == "straddle" and (delta != DISC_DELTAS[0]
                                                    or width != DISC_WIDTHS[0]):
                            continue
                        if stype == "strangle" and width != DISC_WIDTHS[0]:
                            continue
                        for dow_name, days in DISC_DOWSETS:
                            combos += 1
                            cfg = dict(days=days, win=(h1, m1, h2, m2), stype=stype,
                                       vmin=vmin, vmax=vmax, delta=delta)
                            trades = []
                            for day in TRADING_DAYS:
                                if day not in md:
                                    continue
                                em, _ = first_eligible_entry(md[day], day, cfg,
                                                             apply_floor=apply_floor)
                                if em is None:
                                    continue
                                tr = simulate_trade(md[day], day, stype, em, delta, width,
                                                    0.75, 2.0, FORCE_EXIT_MIN)
                                if tr:
                                    tr["day"] = day
                                    trades.append(tr)
                            s = stats(trades)
                            if s["n"] < MIN_TRADES or s["wr"] < MIN_WR:
                                continue
                            ok, reasons = passes_kill_criteria(s)
                            candidates.append({
                                "stype": stype, "vix": [vmin, vmax],
                                "window": [h1, m1, h2, m2], "delta": delta, "width": width,
                                "dow": dow_name, "days": sorted(days),
                                "stats": s, "survives_kill": ok, "kill_reasons": reasons,
                                "trades": trades,
                            })
    return candidates, combos


# ── Actual trades summary (REAL ground truth) ─────────────────────────────────

def load_actual_summary():
    by_day = {}
    for f in sorted(TRADES_DIR.glob("2026-06-*.json")):
        day = f.stem
        if day not in TRADING_DAYS:
            continue
        recs = json.loads(f.read_text())
        real = [r for r in recs if r.get("exit_reason") in ("stop_loss", "profit_target", "eod", "force")
                or (r.get("pnl") and r.get("entry_price"))]
        total = round(sum(r.get("pnl") or 0 for r in real), 2)
        wins = sum(1 for r in real if (r.get("pnl") or 0) > 0)
        by_day[day] = {"n": len(real), "wins": wins, "pnl": total,
                       "strats": sorted({r.get("strategy", "?") for r in real})}
    return by_day


# ── Report writers ─────────────────────────────────────────────────────────────

def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def write_report(md, known, candidates, combos, actual, kept):
    L = []
    P = L.append
    P("# Hermes Replay Engine v2 — June 9–17 2026 (REAL DATA — real bid/ask fills)")
    P("")
    P(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} • Replays the live "
      "paper-trading window bar-by-bar on REAL ThetaData historical option quotes._")
    P("")
    P("## ✅ Provenance & confidence")
    P("")
    P("| Input | Source | Real? |")
    P("|---|---|---|")
    P("| **Option credit / exit / P&L** | **ThetaData `/v3/option/history/quote` tick bid/ask** | ✅ **REAL** |")
    P("| SPY 5-min OHLCV + VWAP | Tradier `/markets/timesales` | ✅ real |")
    P("| VIX 5-min intraday | Tradier `/markets/timesales` | ✅ real |")
    P("| VIX3M daily → contango | Tradier `/markets/history` | ✅ real |")
    P("| Actual fills (ground truth) | `hermes_system/trades/*.json` | ✅ real |")
    P("| Strike *selection* from a delta target | BS delta approx (selection only) | ⚠️ model picks the strike, not the price |")
    P("")
    P("**This is a REAL-DATA replay — no Black-Scholes pricing anywhere in the P&L.** Entry "
      "credit = real short-leg bid − real long-leg ask; exits are scanned tick-by-tick "
      "(collapsed to 1-minute resolution) against the real close-cost (short ask − long bid), "
      "with a hard 15:45 ET force-exit on the last available quote. The only remaining model "
      "is *which* strike a delta target maps to (BS delta with real VIX); once chosen, 100% of "
      "pricing is real exchange bid/ask.")
    P("")
    P("**Days:** Jun 9, 11, 12, 15, 16, 17. Jun 8 excluded (Day-1 credit-parsing artifacts); "
      "Jun 10 excluded (engine crash-looping that day).")
    P("")

    # Market context
    P("## 1. Market context (real)")
    P("")
    P("| day | dow | SPY o→c | VIX (o) | VIX3M | contango | regime |")
    P("|---|---|---|---|---|---|---|")
    for day in TRADING_DAYS:
        if day not in md:
            P(f"| {day} | {DOW_NAME[DOW[day]]} | — | — | — | — | no data |")
            continue
        d = md[day]
        vo = vix_at(d, 9 * 60 + 35)
        ratio = contango_ratio(d, vo)
        spo, spc = d["spy"][0]["close"], d["spy"][-1]["close"]
        reg = "contango" if ratio >= CONTANGO_THRESHOLD else "BACKWARD."
        P(f"| {day} | {DOW_NAME[DOW[day]]} | {spo:.1f}→{spc:.1f} | {vo:.1f} | "
          f"{d['vix3m']:.1f} | {ratio:.3f} | {reg} |")
    P("")

    # Actual vs replay
    P("## 2. What actually happened vs replay (per day)")
    P("")
    P("Actual = real booked P&L from `trades/*.json`. Replay = known strategies on REAL "
      "option pricing with the same real filters.")
    P("")
    P("| day | actual trades | actual P&L | replay strategies fired | replay P&L (real) |")
    P("|---|---|---|---|---|")
    replay_by_day = defaultdict(lambda: {"n": 0, "pnl": 0.0, "names": []})
    for name, r in known.items():
        for t in r["trades"]:
            replay_by_day[t["day"]]["n"] += 1
            replay_by_day[t["day"]]["pnl"] += t["pnl"]
            replay_by_day[t["day"]]["names"].append(name)
    for day in TRADING_DAYS:
        a = actual.get(day, {"n": 0, "pnl": 0.0})
        rb = replay_by_day.get(day, {"n": 0, "pnl": 0.0, "names": []})
        names = ",".join(sorted(set(rb["names"]))) or "—"
        P(f"| {day} | {a['n']} | {a['pnl']:+.0f} | {names} | {rb['pnl']:+.0f} |")
    P("")

    # Known strategy detail
    P("## 3. Known-strategy replay detail (REAL pricing)")
    P("")
    P("| strategy | N | WR% | P&L | PF | avg credit | notes (why blocked on other days) |")
    P("|---|---|---|---|---|---|---|")
    for name, r in known.items():
        s = r["stats"]
        notes = "; ".join(r["notes"][:4]) if r["notes"] else "—"
        P(f"| {name} | {s['n']} | {s['wr']} | {s['pnl']:+.0f} | {fmt_pf(s['pf'])} | "
          f"{s['avg_credit']:.2f} | {notes} |")
    P("")

    # Discovery
    P("## 4. Discovery sweep (REAL pricing, EXPLORATORY)")
    P("")
    P(f"Swept **{combos:,} configs** × {len(md)} days on real bid/ask. Qualifier: "
      f"N≥{MIN_TRADES} & WR≥{MIN_WR:.0f}. Kill criteria: WR<65 / PF<1.5 / N<5 / "
      f"max-loss>3×avg-win → **{len(candidates)} flagged, {len(kept)} survive.**")
    P("")
    P("> With only 6 in-sample days, N≥5 forces a config to fire on ≥5 of 6 days — a much "
      "stricter bar than v1's N≥3. Survivors are still IN-SAMPLE hypotheses: OOS-validate on "
      "the full `backtest_data/` history (real quotes) before promotion to "
      "`execution_engine.py`.")
    P("")
    if kept:
        P(f"Top {min(15, len(kept))} survivors by WR→PF→P&L:")
        P("")
        P("| # | type | dow | VIX | window | δ | w | N | WR% | P&L | PF | Sharpe | avg cr | maxloss |")
        P("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, c in enumerate(kept[:15], 1):
            s = c["stats"]; w = c["window"]
            win = f"{w[0]:02d}:{w[1]:02d}-{w[2]:02d}:{w[3]:02d}"
            P(f"| {i} | {c['stype']} | {c['dow']} | {c['vix'][0]}-{c['vix'][1]} | {win} | "
              f"{c['delta']:.2f} | {c['width']:.0f} | {s['n']} | {s['wr']} | {s['pnl']:+.0f} | "
              f"{fmt_pf(s['pf'])} | {s['sharpe']} | {s['avg_credit']:.2f} | {s['max_loss']:+.0f} |")
        P("")
    else:
        P("_No config cleared N≥5 + the kill criteria — the honest read on 6 real-priced days: "
          "nothing here is robust enough to promote._")
        P("")

    P("## 5. Verdict")
    P("")
    P("All P&L / WR / PF above is from **real exchange bid/ask** — the v1 Black-Scholes "
      "modeling error (RMSE $0.29 > the $0.20 gate) is gone. Remaining caveats: (1) strike "
      "*selection* still uses a BS delta approximation; (2) exits are at 1-minute quote "
      "resolution; (3) discovery survivors are in-sample over 6 days and must be OOS-validated "
      "on the full history before going anywhere near execution.")
    P("")
    REPORT_MD.write_text("\n".join(L))
    print(f"\nReport written: {REPORT_MD}")


CAND_CAP = 15


def write_candidates(kept, candidates, combos):
    payload = {
        "_meta": {
            "generated": datetime.now().isoformat(),
            "status": "REAL DATA — real bid/ask fills (not modeled). EXPLORATORY / in-sample.",
            "provenance": "Option pricing = ThetaData /v3/option/history/quote historical tick "
                          "bid/ask. No Black-Scholes pricing. Strike selection only uses a BS "
                          "delta approximation.",
            "days_replayed": TRADING_DAYS,
            "days_excluded": {"2026-06-08": "Day-1 credit-parsing artifacts",
                              "2026-06-10": "engine crash-looping"},
            "kill_criteria": "WR<65 | PF<1.5 | n<5 | max_loss>3x avg_win",
            "min_trades_to_qualify": MIN_TRADES,
            "configs_swept": combos,
            "flagged_total": len(candidates),
            "survivors_total": len(kept),
            "emitted_top": min(CAND_CAP, len(kept)),
            "next_step": "OOS-validate any survivor on the full backtest_data/ history with "
                         "real option quotes before promotion to execution_engine.py.",
        },
        "candidates": [],
    }
    for i, c in enumerate(kept[:CAND_CAP], 1):
        w = c["window"]; s = c["stats"]
        payload["candidates"].append({
            "suggested_id": f"D{i}_{c['stype']}_{c['dow'].lower()}",
            "StrategyConfig": {
                "entry_days": sorted(c["days"]),
                "entry_start": [w[0], w[1]],
                "entry_end": [w[2], w[3]],
                "spread_type": c["stype"],
                "vix_min": c["vix"][0],
                "vix_max": c["vix"][1],
                "delta_target": c["delta"],
                "profit_target_pct": 0.75,
                "stop_multiple": 2.0,
                "force_exit_time": [15, 45],
                "contracts": 1,
                "spread_width": c["width"],
            },
            "insample_stats": s,
            "survives_kill": c["survives_kill"],
            "data": "REAL bid/ask",
        })
    CAND_JSON.write_text(json.dumps(payload, indent=2))
    print(f"Candidates written: {CAND_JSON}  "
          f"(top {min(CAND_CAP, len(kept))} of {len(kept)} survivors; REAL DATA, in-sample)")


def send_telegram_summary(kept, candidates, combos):
    lines = ["<b>Hermes Replay v2 — REAL ThetaData bid/ask</b>",
             "Jun 9,11,12,15,16,17 (Jun 8/10 excluded). No Black-Scholes.",
             f"Swept {combos:,} configs → {len(candidates)} flagged, "
             f"{len(kept)} survive (WR≥65, PF≥1.5, N≥5)."]
    if kept:
        lines.append("")
        lines.append("<b>Top candidates:</b>")
        for i, c in enumerate(kept[:5], 1):
            s = c["stats"]; w = c["window"]
            win = f"{w[0]:02d}:{w[1]:02d}-{w[2]:02d}:{w[3]:02d}"
            lines.append(f"{i}. {c['stype']} {c['dow']} VIX{c['vix'][0]}-{c['vix'][1]} "
                         f"{win} δ{c['delta']:.2f} w{c['width']:.0f} → "
                         f"N={s['n']} WR={s['wr']}% PF={fmt_pf(s['pf'])} P&L={s['pnl']:+.0f}")
        lines.append("")
        lines.append("In-sample (6 days). OOS-validate before promotion.")
    else:
        lines.append("")
        lines.append("No config survived — nothing promotable on real pricing.")
    ok = tg_send("\n".join(lines))
    print(f"  Telegram summary {'sent' if ok else 'NOT sent'}")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("  HERMES REPLAY ENGINE v2 — Jun 9-17 2026  (REAL ThetaData bid/ask)")
    print("=" * 72)
    md = load_market_data()
    if not md:
        print("No market data loaded — aborting.")
        return

    print("\nReplaying known strategies (real pricing)…")
    known = replay_known(md)
    for name, r in known.items():
        s = r["stats"]
        print(f"  {name:20s} N={s['n']} WR={s['wr']}% P&L={s['pnl']:+.0f} PF={fmt_pf(s['pf'])}")

    print("\nRunning discovery grid (real pricing)…")
    candidates, combos = run_discovery(md, apply_floor=False)
    candidates.sort(key=lambda c: (c["stats"]["wr"], c["stats"]["pf"]
                                   if c["stats"]["pf"] != float("inf") else 1e9,
                                   c["stats"]["pnl"]), reverse=True)
    kept = [c for c in candidates if c["survives_kill"]]
    print(f"  {combos:,} configs swept → {len(candidates)} flagged, {len(kept)} survive kill "
          f"criteria  ({_FETCH_COUNT} ThetaData fetches)")

    actual = load_actual_summary()
    write_report(md, known, candidates, combos, actual, kept)
    write_candidates(kept, candidates, combos)
    send_telegram_summary(kept, candidates, combos)
    print("\nDone.")


if __name__ == "__main__":
    main()
