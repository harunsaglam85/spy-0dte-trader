#!/usr/bin/env python3
"""
Cloud Trader Dashboard  —  Flask web server on port 8080.
Authentication, rate-limiting, read-only SQLite views, auto-refresh every 60s.

Security:
  - Session-based password auth (DASHBOARD_PASSWORD in .env)
  - Rate limiting: 60 req/min per IP (flask-limiter)
  - All POST blocked except /login and /logout
  - No credentials or API keys exposed
  - All DB output sanitized before rendering
"""

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

import pytz
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, request, session

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────
NY_TZ = pytz.timezone("America/New_York")
DB_PATH = os.getenv("DB_PATH", "trading.db")
LOG_PATH = Path("logs/main.log")
PID_FILE = Path("/tmp/cloud_trader.pid")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", secrets.token_hex(32))

BACKTEST_TARGETS = {
    "config_d":      {"wr": 0.67, "avg_pnl": 21.0,  "name": "Config-D"},
    "credit_spread": {"wr": 0.80, "avg_pnl": 14.0,  "name": "Credit Spread"},
    "five_dte":      {"wr": 0.60, "avg_pnl": 21.0,  "name": "5-DTE"},
    "earnings":      {"wr": 0.54, "avg_pnl": 420.0, "name": "Earnings"},
    "vpin":          {"wr": 0.58, "avg_pnl": 8.0,   "name": "VPIN"},
}

COLORS = {
    "config_d":      "#06d6f5",
    "credit_spread": "#00e676",
    "five_dte":      "#ffa726",
    "earnings":      "#ce93d8",
    "vpin":          "#ff7043",
}

STRAT_ORDER = ["config_d", "credit_spread", "five_dte", "earnings", "vpin"]

# ─── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"], storage_uri="memory://")
    def rate_limit(spec):
        return limiter.limit(spec)
except ImportError:
    logging.warning("flask-limiter not installed — rate limiting disabled")
    def rate_limit(_spec):
        def wrapper(f): return f
        return wrapper

# ─── Auth ─────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not session.get("ok"):
            return redirect("/login")
        return f(*a, **kw)
    return wrapped

def _pw_hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

# ─── DB ───────────────────────────────────────────────────────────────────────
def qdb(sql, params=(), one=False):
    """Read-only SQLite query; sanitized output."""
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return (rows[0] if rows else None) if one else rows
    except Exception as e:
        logging.error("DB: %s", e)
        return None if one else []

def _s(v) -> str:
    """Sanitize a value for HTML."""
    if v is None:
        return ""
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# ─── System helpers ───────────────────────────────────────────────────────────
def _bot_status():
    try:
        if not PID_FILE.exists():
            return False, None
        pid = int(PID_FILE.read_text().strip())
        try:
            import psutil
            return psutil.pid_exists(pid), pid
        except ImportError:
            return Path(f"/proc/{pid}").exists(), pid
    except Exception:
        return False, None

def _uptime(pid):
    try:
        import psutil
        secs = int(time.time() - psutil.Process(pid).create_time())
        h, r = divmod(secs, 3600)
        d, h = divmod(h, 24)
        return f"{d}d {h}h {r//60}m" if d else f"{h}h {r//60}m"
    except Exception:
        return "unknown"

def _parse_log():
    """Parse logs/main.log for latest VIX, SPY and heartbeat timestamp.

    Handles multiple log formats produced by main.py:
      "VIX from Tradier: 16.08"
      "VIX: 18.32  |  SPY current: $578.42"
      "'spy_price': 578.42"  (market-state dict repr)
    Falls back to the regimes DB table when no VIX line is found.
    """
    vix = spy = last_ts = None
    api_calls = 0
    try:
        if LOG_PATH.exists():
            with open(LOG_PATH, "rb") as f:
                try:
                    f.seek(-80000, 2)
                except OSError:
                    f.seek(0)
                lines = f.read().decode("utf-8", errors="ignore").splitlines()[-500:]

            for line in reversed(lines):
                # Most-recent log timestamp → heartbeat
                if last_ts is None:
                    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m:
                        try:
                            last_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass

                # VIX — try specific Tradier format first, then generic
                if vix is None:
                    for pat in (
                        r"VIX from Tradier:\s*([\d.]+)",
                        r"\bVIX:\s*([\d.]+)",
                        r"['\"]vix['\"]\s*:\s*([\d.]+)",
                    ):
                        m = re.search(pat, line, re.I)
                        if m:
                            vix = float(m.group(1))
                            break

                # SPY price
                if spy is None:
                    for pat in (
                        r"SPY current:\s*\$([\d.]+)",
                        r"SPY.*?\$([\d.]+)",
                        r"['\"]spy_price['\"]\s*:\s*([\d.]+)",
                    ):
                        m = re.search(pat, line, re.I)
                        if m:
                            spy = float(m.group(1))
                            break

                if re.search(r"tradier|alpaca|api call", line, re.I):
                    api_calls += 1

                if vix and spy and last_ts:
                    break

    except Exception as e:
        logging.error("Log parse: %s", e)

    # Fallback: regimes table has daily VIX snapshots
    if vix is None:
        r = qdb("SELECT vix FROM regimes ORDER BY date DESC LIMIT 1", one=True)
        if r and r.get("vix"):
            vix = float(r["vix"])

    return vix, spy, last_ts, api_calls

def _fmt_size(n):
    if not n:
        return "0 B"
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def _market_status():
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return "CLOSED"
    t = (now.hour, now.minute)
    return "OPEN" if (9, 30) <= t < (16, 0) else "CLOSED"

def _market_data():
    """VIX, SPY price and market phase — used for server-side header rendering."""
    vix, spy, _, _ = _parse_log()
    phase = _market_status()
    now = datetime.now(NY_TZ)
    return {
        "vix": vix,
        "spy_price": spy,
        "phase": phase,
        "et_time": now.strftime("%I:%M %p ET"),
    }

def _day_of_30():
    r = qdb("SELECT MIN(date(timestamp)) s FROM trades", one=True)
    if r and r.get("s"):
        try:
            return min(max((date.today() - date.fromisoformat(r["s"])).days + 1, 1), 30)
        except Exception:
            pass
    return 1

# ─── Data aggregation ─────────────────────────────────────────────────────────
def _cards():
    out = {}
    for sid, bt in BACKTEST_TARGETS.items():
        r = qdb("SELECT COUNT(*) n, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, SUM(pnl) tp FROM trades WHERE strategy=?", (sid,), one=True) or {}
        n = r.get("n") or 0
        wins = r.get("wins") or 0
        pnl = r.get("tp") or 0.0
        wr = wins / n if n else 0.0
        td = (qdb("SELECT COUNT(*) c FROM trades WHERE strategy=? AND date(timestamp)=date('now')", (sid,), one=True) or {}).get("c") or 0
        pr = qdb("SELECT status FROM performance WHERE strategy=? ORDER BY date DESC LIMIT 1", (sid,), one=True)
        raw = pr.get("status", "") if pr else ""
        badge = "DEAD" if raw == "DEAD" else ("PAUSED" if raw == "UNDERPERFORMING" else "ACTIVE")
        out[sid] = {
            "name": bt["name"], "color": COLORS[sid], "badge": badge,
            "today_n": td, "wr": round(wr * 100, 1),
            "pnl": round(pnl, 2), "vs_bt": round((wr - bt["wr"]) * 100, 1),
            "target_wr": round(bt["wr"] * 100, 1), "n": n,
        }
    return out

def _equity():
    rows = qdb("SELECT strategy, date(timestamp) d, SUM(pnl) p FROM trades GROUP BY strategy, d ORDER BY strategy, d")
    all_dates = sorted({r["d"] for r in rows})
    datasets = []
    for sid in STRAT_ORDER:
        pbd = {r["d"]: r["p"] for r in rows if r["strategy"] == sid}
        run, cum = 0.0, []
        for d in all_dates:
            run += pbd.get(d, 0.0)
            cum.append(round(run, 2))
        datasets.append({"label": BACKTEST_TARGETS[sid]["name"], "data": cum, "color": COLORS[sid]})
    return {"labels": all_dates, "datasets": datasets}

def _recent_trades():
    rows = qdb("SELECT timestamp,strategy,direction,entry_price,exit_price,pnl,exit_reason FROM trades ORDER BY timestamp DESC LIMIT 20")
    return [{
        "time": (r["timestamp"] or "")[:16],
        "strat": BACKTEST_TARGETS.get(r["strategy"], {}).get("name", _s(r["strategy"])),
        "sid": r["strategy"],
        "dir": _s(r["direction"]),
        "entry": round(r["entry_price"] or 0, 2),
        "exit_p": round(r["exit_price"] or 0, 2),
        "pnl": round(r["pnl"] or 0, 2),
        "reason": _s(r["exit_reason"] or ""),
    } for r in rows]

def _daily_bar():
    cut = (date.today() - timedelta(days=13)).isoformat()
    rows = qdb("SELECT strategy, date(timestamp) d, SUM(pnl) p FROM trades WHERE date(timestamp)>=? GROUP BY strategy, d", (cut,))
    dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    datasets = []
    for sid in STRAT_ORDER:
        pbd = {r["d"]: r["p"] for r in rows if r["strategy"] == sid}
        datasets.append({"label": BACKTEST_TARGETS[sid]["name"], "data": [round(pbd.get(d, 0), 2) for d in dates], "color": COLORS[sid]})
    return {"labels": [d[5:] for d in dates], "datasets": datasets}

def _suggestions():
    rows = qdb("SELECT id,strategy,suggestion_text,created_date FROM suggestions WHERE status='pending' ORDER BY created_date DESC LIMIT 10")
    return [{"id": r["id"], "strat": BACKTEST_TARGETS.get(r["strategy"], {}).get("name", _s(r["strategy"])), "text": _s(r["suggestion_text"]), "date": r["created_date"]} for r in rows]

def _health():
    running, pid = _bot_status()
    vix, spy, last_ts, api_c = _parse_log()
    hb = "—"
    if last_ts:
        delta = int((datetime.now() - last_ts).total_seconds())
        hb = f"{delta}s ago" if delta < 60 else (f"{delta//60}m ago" if delta < 3600 else f"{delta//3600}h ago")
    return {
        "running": running,
        "uptime": _uptime(pid) if running and pid else "—",
        "heartbeat": hb,
        "db_size": _fmt_size(Path(DB_PATH).stat().st_size if Path(DB_PATH).exists() else 0),
        "log_size": _fmt_size(LOG_PATH.stat().st_size if LOG_PATH.exists() else 0),
        "api_calls": api_c,
        "vix": vix,
        "spy": spy,
    }

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/api/dashboard")
@login_required
def api_dashboard():
    h = _health()
    now_et = datetime.now(NY_TZ)
    return jsonify({
        "cards": _cards(),
        "equity": _equity(),
        "trades": _recent_trades(),
        "daily": _daily_bar(),
        "suggestions": _suggestions(),
        "health": h,
        "meta": {
            "day30": _day_of_30(),
            "market": _market_status(),
            "et": now_et.strftime("%I:%M:%S %p ET"),
            "vix": h["vix"],
            "spy": h["spy"],
            "colors": COLORS,
            "targets": {k: v["wr"] for k, v in BACKTEST_TARGETS.items()},
        },
    })

@app.route("/api/market")
@login_required
def api_market():
    """Lightweight endpoint: VIX, SPY price, market phase, ET time."""
    return jsonify(_market_data())

@app.route("/login", methods=["GET", "POST"])
@rate_limit("10 per minute")
def login():
    err = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if _pw_hash(pw) == _pw_hash(DASHBOARD_PASSWORD):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        err = "Invalid password."
    return make_response(
        LOGIN_HTML.replace("__ERR__", f'<p class="err">{err}</p>' if err else ""),
        200, {"Content-Type": "text/html; charset=utf-8"},
    )

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
@login_required
def index():
    """Render dashboard with VIX, SPY, and market status embedded at response time.
    The page carries a 60-second meta-refresh so values stay current without JS fetches.
    """
    mkt = _market_data()
    vix_str  = f"VIX {mkt['vix']:.2f}"        if mkt["vix"]       else "VIX —"
    spy_str  = f"SPY ${mkt['spy_price']:.2f}"  if mkt["spy_price"] else "SPY —"
    phase    = mkt["phase"]                    # "OPEN" or "CLOSED"
    mkt_cls  = "open" if phase == "OPEN" else "closed"

    html = (
        DASHBOARD_HTML
        .replace("__VIX__",     vix_str)
        .replace("__SPY__",     spy_str)
        .replace("__MARKET__",  phase)
        .replace("__MKTCLS__",  mkt_cls)
    )
    return make_response(html, 200, {"Content-Type": "text/html; charset=utf-8"})

# ─── HTML: Login ──────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cloud Trader — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#030b1a;--surf:#0a1628;--border:#1e3a5f;--cyan:#06d6f5;--text:#cdd9e5;--muted:#546e7a}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;
  display:flex;align-items:center;justify-content:center;
  background-image:radial-gradient(ellipse at 50% 0%,rgba(6,214,245,0.07) 0%,transparent 60%)}
.card{background:var(--surf);border:1px solid var(--border);border-radius:16px;padding:48px 40px;
  width:380px;position:relative;box-shadow:0 0 60px rgba(6,214,245,0.04)}
.card::before{content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);
  width:60%;height:1px;background:linear-gradient(90deg,transparent,var(--cyan),transparent)}
.logo{font-size:1.8rem;font-weight:800;letter-spacing:-0.02em;margin-bottom:6px}
.logo span{color:var(--cyan)}
.sub{color:var(--muted);font-size:0.75rem;font-family:'JetBrains Mono',monospace;
  letter-spacing:0.1em;text-transform:uppercase;margin-bottom:36px}
label{display:block;font-size:0.7rem;font-family:'JetBrains Mono',monospace;color:var(--muted);
  text-transform:uppercase;letter-spacing:0.12em;margin-bottom:8px}
input{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-family:'JetBrains Mono',monospace;font-size:1rem;
  padding:12px 16px;outline:none;transition:border-color .2s}
input:focus{border-color:var(--cyan)}
button{width:100%;margin-top:20px;background:var(--cyan);color:#030b1a;border:none;
  border-radius:8px;padding:13px;font-family:'Syne',sans-serif;font-weight:700;
  font-size:0.875rem;cursor:pointer;letter-spacing:0.06em;text-transform:uppercase;
  transition:opacity .2s}
button:hover{opacity:.85}
.err{color:#ff3d57;font-size:0.78rem;font-family:'JetBrains Mono',monospace;
  margin-top:14px;text-align:center}
.note{color:var(--muted);font-size:0.68rem;font-family:'JetBrains Mono',monospace;
  margin-top:28px;text-align:center;line-height:1.6}
.note code{color:var(--cyan)}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Cloud <span>Trader</span></div>
  <div class="sub">Secure Dashboard Access</div>
  <form method="POST">
    <label for="pw">Password</label>
    <input type="password" id="pw" name="password" autocomplete="current-password" autofocus required>
    <button type="submit">Enter Dashboard</button>
    __ERR__
  </form>
  <p class="note">Recommended: access via SSH tunnel only<br><code>ssh -L 8080:localhost:8080 user@server</code></p>
</div>
</body>
</html>"""

# ─── HTML: Dashboard ──────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Cloud Trader — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js" integrity="sha384-NrKB+u6Ts6AtkIhwPixiKTzgSKNblyhlk0Sohlgar9UHUBzai/sgnNNWWd291xqt" crossorigin="anonymous"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#030b1a; --surf:#08111f; --surf2:#0d1a2e; --surf3:#111f36;
  --border:#1a3050; --border2:#243d5c;
  --cyan:#06d6f5; --cyan-dim:rgba(6,214,245,0.12);
  --green:#00e676; --green-dim:rgba(0,230,118,0.12);
  --red:#ff3d57; --red-dim:rgba(255,61,87,0.12);
  --amber:#ffa726; --amber-dim:rgba(255,167,38,0.12);
  --purple:#ce93d8; --orange:#ff7043;
  --text:#cdd9e5; --muted:#546e7a; --dim:#2d4a63;
  --radius:12px; --radius-sm:8px;
}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;
  min-height:100vh;overflow-x:hidden;
  background-image:
    radial-gradient(ellipse at 20% 0%,rgba(6,214,245,0.05) 0%,transparent 50%),
    radial-gradient(ellipse at 80% 100%,rgba(0,230,118,0.04) 0%,transparent 50%)}
body::before{content:'';position:fixed;inset:0;
  background-image:linear-gradient(var(--dim) 1px,transparent 1px),
    linear-gradient(90deg,var(--dim) 1px,transparent 1px);
  background-size:40px 40px;opacity:0.15;pointer-events:none;z-index:0}

/* ── Header ── */
header{position:sticky;top:0;z-index:100;
  background:rgba(3,11,26,0.92);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;padding:0 24px;height:56px;gap:24px}
header::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--cyan),transparent);opacity:0.3}
.hdr-brand{display:flex;align-items:center;gap:10px;flex-shrink:0}
.hdr-brand h1{font-size:1.05rem;font-weight:800;letter-spacing:-0.01em}
.hdr-brand h1 span{color:var(--cyan)}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.running{background:var(--green);box-shadow:0 0 8px var(--green);
  animation:pulse-dot 2s ease-in-out infinite}
.dot.stopped{background:var(--red)}
@keyframes pulse-dot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.6;transform:scale(0.85)}}
.hdr-center{flex:1;display:flex;align-items:center;justify-content:center;gap:20px;
  font-family:'JetBrains Mono',monospace;font-size:0.78rem}
#clock{color:var(--text);letter-spacing:0.04em}
.market-badge{padding:2px 10px;border-radius:4px;font-size:0.7rem;font-weight:600;
  letter-spacing:0.1em;text-transform:uppercase}
.market-badge.open{background:var(--green-dim);color:var(--green);border:1px solid var(--green)}
.market-badge.closed{background:var(--red-dim);color:var(--red);border:1px solid rgba(255,61,87,0.4)}
#vix-val{color:var(--muted)}
.hdr-right{display:flex;align-items:center;gap:20px;font-family:'JetBrains Mono',monospace;
  font-size:0.8rem;flex-shrink:0}
#day-val{color:var(--cyan)}
#spy-val{color:var(--text);font-weight:600}
#last-update{color:var(--dim);font-size:0.68rem;display:none}
.logout-btn{background:none;border:1px solid var(--border);color:var(--muted);
  padding:4px 12px;border-radius:4px;cursor:pointer;font-family:'Syne',sans-serif;
  font-size:0.72rem;letter-spacing:0.06em;text-transform:uppercase;transition:all .2s}
.logout-btn:hover{border-color:var(--border2);color:var(--text)}

/* ── Layout ── */
main{position:relative;z-index:1;padding:24px;max-width:1600px;margin:0 auto;display:flex;flex-direction:column;gap:24px}
.section-title{font-size:0.65rem;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;
  color:var(--muted);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}
.card{background:var(--surf2);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.card-title{font-size:0.72rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;
  color:var(--muted);margin-bottom:16px}

/* ── Strategy cards ── */
#cards-container{display:grid;grid-template-columns:repeat(5,1fr);gap:14px}
.strat-card{background:var(--surf2);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px;position:relative;overflow:hidden;transition:transform .2s,border-color .2s,box-shadow .2s;cursor:default}
.strat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--accent);opacity:0.9}
.strat-card::after{content:'';position:absolute;inset:0;
  background:radial-gradient(ellipse at 50% 0%,rgba(var(--accent-rgb),0.06) 0%,transparent 60%);
  pointer-events:none}
.strat-card:hover{transform:translateY(-3px);border-color:var(--border2);box-shadow:0 8px 32px rgba(0,0,0,0.3)}
.card-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:14px;gap:8px}
.card-name{font-size:0.9rem;font-weight:700;letter-spacing:-0.01em}
.badge{padding:2px 8px;border-radius:3px;font-size:0.65rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;white-space:nowrap}
.badge-active{background:var(--green-dim);color:var(--green);border:1px solid rgba(0,230,118,0.3)}
.badge-paused{background:var(--amber-dim);color:var(--amber);border:1px solid rgba(255,167,38,0.3)}
.badge-dead{background:var(--red-dim);color:var(--red);border:1px solid rgba(255,61,87,0.3)}
.card-stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.stat{display:flex;flex-direction:column;gap:2px}
.stat-label{font-size:0.62rem;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted)}
.stat-val{font-family:'JetBrains Mono',monospace;font-size:0.88rem;font-weight:600;color:var(--text)}
.card-n{font-family:'JetBrains Mono',monospace;font-size:0.65rem;color:var(--dim)}

/* ── Colors ── */
.pos{color:var(--green)!important}
.neg{color:var(--red)!important}
.bold{font-weight:600}
.mono{font-family:'JetBrains Mono',monospace}
.muted{color:var(--muted)}

/* ── Chart containers ── */
.chart-wrap{position:relative;height:320px}
.chart-wrap-sm{position:relative;height:280px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:1100px){.two-col{grid-template-columns:1fr}#cards-container{grid-template-columns:repeat(3,1fr)}}

/* ── Trades table ── */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:0.8rem}
thead th{font-size:0.65rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;
  color:var(--muted);padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);
  background:var(--surf);white-space:nowrap}
tbody tr{border-bottom:1px solid rgba(26,48,80,0.5);transition:background .15s}
tbody tr:hover{background:rgba(6,214,245,0.03)}
tbody tr.win{border-left:2px solid var(--green)}
tbody tr.loss{border-left:2px solid var(--red)}
tbody td{padding:9px 12px;color:var(--text);white-space:nowrap}
.strat-chip{font-family:'JetBrains Mono',monospace;font-size:0.75rem;font-weight:500}
.dir{padding:1px 7px;border-radius:3px;font-size:0.65rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;font-family:'JetBrains Mono',monospace}
.dir.buy,.dir.long,.dir.call{background:rgba(0,230,118,0.12);color:var(--green)}
.dir.sell,.dir.short,.dir.put{background:rgba(255,61,87,0.12);color:var(--red)}
.dir.credit{background:rgba(206,147,216,0.15);color:var(--purple)}
.reason{font-size:0.72rem;color:var(--muted);max-width:160px;overflow:hidden;text-overflow:ellipsis}
.empty{text-align:center;padding:32px;color:var(--muted);font-size:0.8rem;font-style:italic}

/* ── Gauges ── */
#gauges-container{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.gauge-wrap{display:flex;flex-direction:column;align-items:center;gap:4px;padding:16px 12px;
  background:var(--surf);border:1px solid var(--border);border-radius:var(--radius-sm)}
.gauge-svg{width:100%;max-width:160px}
.gauge-label{font-size:0.72rem;font-weight:700;letter-spacing:0.06em;text-align:center}
.gauge-target{font-size:0.62rem;text-align:center}

/* ── Suggestions ── */
.btn-approve{background:var(--green-dim);color:var(--green);border:1px solid rgba(0,230,118,0.3);
  padding:3px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;transition:all .2s;margin-right:4px}
.btn-approve:hover{background:rgba(0,230,118,0.2)}
.btn-reject{background:var(--red-dim);color:var(--red);border:1px solid rgba(255,61,87,0.3);
  padding:3px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;transition:all .2s}
.btn-reject:hover{background:rgba(255,61,87,0.2)}
.suggestion-text{font-size:0.75rem;color:var(--text);max-width:480px;line-height:1.5}
.strat-name{font-family:'JetBrains Mono',monospace;font-size:0.75rem;color:var(--cyan)}
.actions{white-space:nowrap}

/* ── Health grid ── */
.health-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.health-item{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:14px 16px;display:flex;flex-direction:column;gap:4px}
.health-label{font-size:0.62rem;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted)}
.health-val{font-family:'JetBrains Mono',monospace;font-size:0.95rem;font-weight:600;color:var(--text)}
.health-status{display:flex;align-items:center;gap:8px}
.status-indicator{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.status-ok{background:var(--green);box-shadow:0 0 6px var(--green)}
.status-err{background:var(--red)}

/* ── Section layout ── */
.sec-row{display:grid;grid-template-columns:1.2fr 1fr;gap:16px}
@media(max-width:900px){.sec-row{grid-template-columns:1fr}#gauges-container{grid-template-columns:repeat(3,1fr)}}

/* ── Loading skeleton ── */
@keyframes shimmer{0%{opacity:.4}50%{opacity:.8}100%{opacity:.4}}
.loading{animation:shimmer 1.5s ease-in-out infinite;background:var(--surf3);border-radius:4px;min-height:120px}
</style>
</head>
<body>

<!-- Header -->
<header>
  <div class="hdr-brand">
    <span class="dot running" id="bot-dot" title="Checking..."></span>
    <h1>Cloud <span>Trader</span></h1>
  </div>
  <div class="hdr-center">
    <span id="clock" class="mono">--:--:-- ET</span>
    <span class="market-badge __MKTCLS__" id="market-status">__MARKET__</span>
    <span id="vix-val" class="mono muted">__VIX__</span>
  </div>
  <div class="hdr-right">
    <span id="day-val" class="mono">Day -- / 30</span>
    <span id="spy-val" class="mono">__SPY__</span>
    <span id="last-update"></span>
    <form action="/logout" method="POST" style="margin:0">
      <button class="logout-btn" type="submit">Logout</button>
    </form>
  </div>
</header>

<main>

  <!-- Section 1: Strategy Cards -->
  <section>
    <div class="section-title">Strategy Performance</div>
    <div id="cards-container">
      <div class="loading" style="height:140px"></div>
      <div class="loading" style="height:140px"></div>
      <div class="loading" style="height:140px"></div>
      <div class="loading" style="height:140px"></div>
      <div class="loading" style="height:140px"></div>
    </div>
  </section>

  <!-- Section 2: Equity Curve -->
  <section>
    <div class="card">
      <div class="card-title">Cumulative P&L by Strategy</div>
      <div class="chart-wrap">
        <canvas id="equity-chart"></canvas>
      </div>
    </div>
  </section>

  <!-- Section 3: Recent Trades -->
  <section>
    <div class="card">
      <div class="card-title">Recent Trades (Last 20)</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time (ET)</th>
              <th>Strategy</th>
              <th>Direction</th>
              <th>Entry $</th>
              <th>Exit $</th>
              <th>P&amp;L</th>
              <th>Exit Reason</th>
            </tr>
          </thead>
          <tbody id="trades-tbody">
            <tr><td colspan="7" class="empty">Loading trades...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- Sections 4+5: Daily Chart + Win Rate Gauges -->
  <section>
    <div class="two-col">
      <div class="card">
        <div class="card-title">Daily P&amp;L — Last 14 Days</div>
        <div class="chart-wrap-sm">
          <canvas id="daily-chart"></canvas>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Win Rate Gauges — Live vs Target</div>
        <div id="gauges-container">
          <div class="loading" style="height:120px"></div>
          <div class="loading" style="height:120px"></div>
          <div class="loading" style="height:120px"></div>
          <div class="loading" style="height:120px"></div>
          <div class="loading" style="height:120px"></div>
        </div>
      </div>
    </div>
  </section>

  <!-- Sections 6+7: Suggestions + System Health -->
  <section>
    <div class="sec-row">
      <div class="card">
        <div class="card-title">Learning Engine Suggestions</div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Strategy</th>
                <th>Suggestion</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="suggestions-tbody">
              <tr><td colspan="4" class="empty">Loading...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <div class="card-title">System Health</div>
        <div class="health-grid">
          <div class="health-item">
            <span class="health-label">Bot Status</span>
            <div class="health-status">
              <span class="status-indicator" id="h-status-dot" style="background:var(--muted)"></span>
              <span class="health-val" id="h-status-text">—</span>
            </div>
          </div>
          <div class="health-item">
            <span class="health-label">Uptime</span>
            <span class="health-val" id="h-uptime">—</span>
          </div>
          <div class="health-item">
            <span class="health-label">Last Heartbeat</span>
            <span class="health-val" id="h-heartbeat">—</span>
          </div>
          <div class="health-item">
            <span class="health-label">Database Size</span>
            <span class="health-val" id="h-dbsize">—</span>
          </div>
          <div class="health-item">
            <span class="health-label">Log File Size</span>
            <span class="health-val" id="h-logsize">—</span>
          </div>
          <div class="health-item">
            <span class="health-label">API Calls (log)</span>
            <span class="health-val" id="h-api">—</span>
          </div>
        </div>
      </div>
    </div>
  </section>

</main>

<script>
"use strict";

let equityChart = null, dailyChart = null;
const STRAT_ORDER = ['config_d','credit_spread','five_dte','earnings','vpin'];

// Chart.js global defaults
Chart.defaults.color = '#546e7a';
Chart.defaults.borderColor = 'rgba(26,48,80,0.5)';
Chart.defaults.font.family = "'JetBrains Mono', monospace";

function hex2rgba(hex, a) {
  const r=parseInt(hex.slice(1,3),16), g=parseInt(hex.slice(3,5),16), b=parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${a})`;
}

function $id(id) { return document.getElementById(id); }

// ── Clock ──
function updateClock() {
  const et = new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true}).format(new Date());
  const el = $id('clock'); if(el) el.textContent = et + ' ET';
}

// ── Header ──
function updateHeader(meta) {
  const mkt = $id('market-status');
  if(mkt){ mkt.textContent = meta.market; mkt.className = 'market-badge ' + (meta.market==='OPEN'?'open':'closed'); }
  if(meta.vix) { const e=$id('vix-val'); if(e) e.textContent='VIX '+meta.vix.toFixed(2); }
  if(meta.spy) { const e=$id('spy-val'); if(e) e.textContent='SPY $'+meta.spy.toFixed(2); }
  const d=$id('day-val'); if(d) d.textContent='Day '+meta.day30+' / 30';
}

// ── Strategy Cards ──
function renderCards(cards) {
  const c = $id('cards-container'); if(!c) return;
  c.innerHTML = '';
  STRAT_ORDER.forEach(sid => {
    const d = cards[sid]; if(!d) return;
    const pnlS = d.pnl>=0?'pos':'neg';
    const vsS  = d.vs_bt>=0?'pos':'neg';
    const wrS  = d.wr>=d.target_wr?'pos':'neg';
    const badgeC = d.badge==='ACTIVE'?'badge-active':d.badge==='DEAD'?'badge-dead':'badge-paused';
    c.innerHTML += `
    <div class="strat-card" style="--accent:${d.color}">
      <div class="card-top">
        <span class="card-name">${d.name}</span>
        <span class="badge ${badgeC}">${d.badge}</span>
      </div>
      <div class="card-stats">
        <div class="stat">
          <span class="stat-label">Today</span>
          <span class="stat-val">${d.today_n} trade${d.today_n!==1?'s':''}</span>
        </div>
        <div class="stat">
          <span class="stat-label">Win Rate</span>
          <span class="stat-val ${wrS}">${d.wr}%</span>
        </div>
        <div class="stat">
          <span class="stat-label">P&amp;L</span>
          <span class="stat-val ${pnlS}">${d.pnl>=0?'+':''}$${d.pnl.toFixed(2)}</span>
        </div>
        <div class="stat">
          <span class="stat-label">vs Backtest</span>
          <span class="stat-val ${vsS}">${d.vs_bt>=0?'+':''}${d.vs_bt}pp</span>
        </div>
      </div>
      <div class="card-n mono muted">${d.n} total trades &middot; Target ${d.target_wr}%</div>
    </div>`;
  });
}

// ── Equity Curve ──
function renderEquity(equity) {
  const ctx = $id('equity-chart'); if(!ctx) return;
  if(equityChart){ equityChart.destroy(); equityChart=null; }
  equityChart = new Chart(ctx, {
    type:'line',
    data:{
      labels: equity.labels,
      datasets: equity.datasets.map(d=>({
        label:d.label, data:d.data,
        borderColor:d.color, backgroundColor:hex2rgba(d.color,0.07),
        borderWidth:2, tension:0.35, fill:true,
        pointRadius:0, pointHoverRadius:5, pointHoverBackgroundColor:d.color,
      }))
    },
    options:{
      responsive:true, maintainAspectRatio:false, animation:{duration:700},
      plugins:{
        legend:{position:'top',labels:{color:'#8899a6',font:{size:11},boxWidth:12,padding:16}},
        tooltip:{mode:'index',intersect:false,backgroundColor:'#0a1628',
          borderColor:'#1e3a5f',borderWidth:1,titleColor:'#cdd9e5',bodyColor:'#8899a6',
          padding:10,callbacks:{label:c=>` ${c.dataset.label}: $${c.raw.toFixed(2)}`}}
      },
      scales:{
        x:{grid:{color:'rgba(26,48,80,0.4)'},ticks:{color:'#546e7a',maxTicksLimit:12,font:{size:10}}},
        y:{grid:{color:'rgba(26,48,80,0.4)'},ticks:{color:'#546e7a',font:{size:10},
          callback:v=>'$'+(v>=0?'':'')+v.toLocaleString()}}
      },
      interaction:{mode:'nearest',axis:'x',intersect:false}
    }
  });
}

// ── Recent Trades Table ──
function renderTrades(trades) {
  const tb = $id('trades-tbody'); if(!tb) return;
  if(!trades.length){ tb.innerHTML='<tr><td colspan="7" class="empty">No trades recorded yet</td></tr>'; return; }
  tb.innerHTML = trades.map(t => {
    const dirC = t.dir?t.dir.toLowerCase():'';
    const pnlFmt = (t.pnl>=0?'+':'')+'$'+t.pnl.toFixed(2);
    return `<tr class="${t.pnl>0?'win':'loss'}">
      <td class="mono muted" style="font-size:.75rem">${t.time}</td>
      <td><span class="strat-chip" style="color:var(--c-${t.sid},#8899a6)">${t.strat}</span></td>
      <td><span class="dir ${dirC}">${t.dir||'—'}</span></td>
      <td class="mono" style="font-size:.8rem">${t.entry}</td>
      <td class="mono" style="font-size:.8rem">${t.exit_p}</td>
      <td class="mono bold ${t.pnl>0?'pos':'neg'}">${pnlFmt}</td>
      <td class="reason muted">${t.reason}</td>
    </tr>`;
  }).join('');
}

// ── Daily Bar Chart ──
function renderDaily(daily) {
  const ctx = $id('daily-chart'); if(!ctx) return;
  if(dailyChart){ dailyChart.destroy(); dailyChart=null; }
  dailyChart = new Chart(ctx, {
    type:'bar',
    data:{
      labels:daily.labels,
      datasets:daily.datasets.map(d=>({
        label:d.label, data:d.data,
        backgroundColor:hex2rgba(d.color,0.65),
        borderColor:d.color, borderWidth:1, borderRadius:2,
      }))
    },
    options:{
      responsive:true, maintainAspectRatio:false, animation:{duration:700},
      plugins:{
        legend:{position:'top',labels:{color:'#8899a6',font:{size:10},boxWidth:10,padding:12}},
        tooltip:{backgroundColor:'#0a1628',borderColor:'#1e3a5f',borderWidth:1,
          titleColor:'#cdd9e5',bodyColor:'#8899a6',
          callbacks:{label:c=>` ${c.dataset.label}: $${c.raw.toFixed(2)}`}}
      },
      scales:{
        x:{grid:{color:'rgba(26,48,80,0.3)'},ticks:{color:'#546e7a',font:{size:9},maxRotation:45}},
        y:{grid:{color:'rgba(26,48,80,0.4)'},ticks:{color:'#546e7a',font:{size:10},
          callback:v=>'$'+v}}
      }
    }
  });
}

// ── Win Rate Gauges (SVG) ──
function updateGauge(sid, wr, targetWr, color) {
  const R=75, CX=100, CY=105;
  const arcLen = Math.PI * R; // semicircle = π*r ≈ 235.6

  const arcEl = $id('g-arc-'+sid);
  if(arcEl) {
    const fill = Math.min(wr/100, 1) * arcLen;
    const above = wr >= targetWr;
    arcEl.setAttribute('stroke-dasharray', fill.toFixed(1)+' '+(arcLen+20).toFixed(1));
    arcEl.setAttribute('stroke', above ? '#00e676' : '#ff3d57');
  }

  const mEl = $id('g-mk-'+sid);
  if(mEl) {
    const angle = Math.PI * (1 - targetWr/100);
    mEl.setAttribute('cx', (CX + R*Math.cos(angle)).toFixed(1));
    mEl.setAttribute('cy', (CY - R*Math.sin(angle)).toFixed(1));
  }

  const pEl = $id('g-pct-'+sid);
  if(pEl) {
    const above = wr >= targetWr;
    pEl.textContent = wr.toFixed(1)+'%';
    pEl.setAttribute('fill', above ? '#00e676' : '#ff3d57');
  }

  const vEl = $id('g-vs-'+sid);
  if(vEl) {
    const delta = wr - targetWr;
    const above = delta >= 0;
    vEl.textContent = (above?'+':'')+delta.toFixed(1)+'pp';
    vEl.setAttribute('fill', above ? '#00e676' : '#ff3d57');
  }
}

function renderGauges(cards) {
  const c = $id('gauges-container'); if(!c) return;
  if(!c.dataset.built) {
    c.dataset.built = '1';
    c.innerHTML = STRAT_ORDER.map(sid => {
      const d = cards[sid]; if(!d) return '';
      return `<div class="gauge-wrap">
        <svg viewBox="0 0 200 130" class="gauge-svg">
          <path d="M 25 105 A 75 75 0 0 1 175 105"
                fill="none" stroke="#1a3050" stroke-width="10" stroke-linecap="round"/>
          <path id="g-arc-${sid}" d="M 25 105 A 75 75 0 0 1 175 105"
                fill="none" stroke="#06d6f5" stroke-width="10" stroke-linecap="round"
                stroke-dasharray="0 256"/>
          <circle id="g-mk-${sid}" cx="25" cy="105" r="5" fill="white" opacity="0.7"/>
          <text id="g-pct-${sid}" x="100" y="91" text-anchor="middle"
                font-family="'JetBrains Mono',monospace" font-size="22" font-weight="600" fill="#cdd9e5">--%</text>
          <text id="g-vs-${sid}" x="100" y="110" text-anchor="middle"
                font-family="'JetBrains Mono',monospace" font-size="12" fill="#546e7a">--</text>
        </svg>
        <div class="gauge-label" style="color:${d.color}">${d.name}</div>
        <div class="gauge-target mono muted" style="font-size:.6rem">Target: ${d.target_wr}%</div>
      </div>`;
    }).join('');
  }
  STRAT_ORDER.forEach(sid => {
    if(cards[sid]) updateGauge(sid, cards[sid].wr, cards[sid].target_wr, cards[sid].color);
  });
}

// ── Suggestions ──
function renderSuggestions(suggs) {
  const tb = $id('suggestions-tbody'); if(!tb) return;
  if(!suggs.length) {
    tb.innerHTML='<tr><td colspan="4" class="empty">No pending suggestions</td></tr>'; return;
  }
  tb.innerHTML = suggs.map(s=>`<tr>
    <td class="mono muted" style="font-size:.72rem">${s.date}</td>
    <td class="strat-name">${s.strat}</td>
    <td class="suggestion-text">${s.text}</td>
    <td class="actions">
      <button class="btn-approve" title="Approve (visual only)">&#10003;</button>
      <button class="btn-reject" title="Reject (visual only)">&#10007;</button>
    </td>
  </tr>`).join('');
  tb.querySelectorAll('.btn-approve,.btn-reject').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      const row = btn.closest('tr');
      row.style.transition='opacity .5s';
      row.style.opacity='0.2';
    });
  });
}

// ── System Health ──
function renderHealth(h) {
  const s = id => { const el=$id(id); return el ? (v=>el.textContent=v) : (()=>{}); };
  s('h-uptime')(h.uptime);
  s('h-heartbeat')(h.heartbeat);
  s('h-dbsize')(h.db_size);
  s('h-logsize')(h.log_size);
  s('h-api')(h.api_calls + ' references');

  const dot = $id('bot-dot');
  const stDot = $id('h-status-dot');
  const stTxt = $id('h-status-text');
  if(h.running) {
    if(dot){ dot.className='dot running'; dot.title='Bot running'; }
    if(stDot){ stDot.style.background='var(--green)'; stDot.style.boxShadow='0 0 6px var(--green)'; }
    if(stTxt) stTxt.textContent='Running';
  } else {
    if(dot){ dot.className='dot stopped'; dot.title='Bot not running'; }
    if(stDot){ stDot.style.background='var(--red)'; stDot.style.boxShadow='none'; }
    if(stTxt) stTxt.textContent='Stopped';
  }
}

// ── Inject strategy CSS variables ──
function injectColors(colors) {
  const root = document.documentElement;
  Object.entries(colors||{}).forEach(([k,v]) => root.style.setProperty('--c-'+k, v));
}

// ── Main fetch + render ──
async function fetchAndRender() {
  try {
    const res = await fetch('/api/dashboard');
    if(res.status===401){ window.location.href='/login'; return; }
    if(!res.ok) throw new Error('HTTP '+res.status);
    const data = await res.json();

    injectColors(data.meta.colors);
    renderCards(data.cards);
    renderEquity(data.equity);
    renderTrades(data.trades);
    renderDaily(data.daily);
    renderGauges(data.cards);
    renderSuggestions(data.suggestions);
    renderHealth(data.health);
    updateHeader(data.meta);

    const u=$id('last-update');
    if(u){ u.style.display='block'; u.textContent='Updated '+new Date().toLocaleTimeString(); }
  } catch(e) {
    console.error('Dashboard error:', e);
    const u=$id('last-update');
    if(u){ u.style.display='block'; u.textContent='Error: '+e.message; u.style.color='#ff3d57'; }
  }
}

// ── Boot ──
setInterval(updateClock, 1000);
updateClock();
fetchAndRender();
setInterval(fetchAndRender, 60000);
</script>
</body>
</html>"""

# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Cloud Trader Dashboard starting on 0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
