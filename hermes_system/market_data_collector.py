#!/usr/bin/env python3
"""
Hermes Market Data Collector (Batch 2 — Task 1).

Runs daily at 4:30 PM ET via cron and snapshots the market context the analysis
session and strategy development use, with ZERO AI calls — pure Python, no tokens.

Collected each run:
  • Top 10 SPY / market news headlines  (yfinance)
  • VIX, VIX3M and the contango ratio   (yfinance)
  • Upcoming earnings (next 5 days)      (yfinance, mega-cap + S4 universe)
  • Economic calendar (next 2 weeks)     (FOMC/CPI/jobs — computed, see note)

Output: /root/hermes_system/market_context.json  (atomic write)

NOTE on the economic calendar: yfinance does not expose a macro/economic
calendar, so FOMC dates come from the known 2025-2026 schedule and CPI / jobs
(NFP) release dates are computed from the BLS's regular cadence (NFP = first
Friday of the month; CPI ≈ mid-month business day). This stays "pure Python,
zero tokens" and is good enough to flag event-risk days for the analysis session.

Cron: 30 16 * * 1-5  cd /root/spy-0dte-trader && python3 hermes_system/market_data_collector.py
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
import yfinance as yf

# ── Timezone ───────────────────────────────────────────────────────────────────
# Cron's `TZ=` is not reliably inherited by the spawned shell/python (this job was
# firing at 05:20 ET = 09:20 UTC), so pin the process timezone here. This makes
# every datetime.now()/time.localtime() and logging timestamp ET regardless of how
# the job is invoked. Must run before logging is configured so asctime is ET too.
os.environ['TZ'] = 'America/New_York'
time.tzset()

# ── Paths / logging ──────────────────────────────────────────────────────────
HERMES_ROOT  = Path('/root/hermes_system')
LOG_DIR      = HERMES_ROOT / 'logs'
OUTPUT_FILE  = HERMES_ROOT / 'market_context.json'
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    # Log ONLY to the file. The cron line redirects stdout+stderr into this same
    # market_data.log (`>> … 2>&1`); a StreamHandler (→ stderr) would then write
    # every line a SECOND time via that shell redirect — the source of the
    # duplicated log lines. FileHandler alone = exactly one line per event.
    handlers=[logging.FileHandler(LOG_DIR / 'market_data.log')],
)
log = logging.getLogger('hermes.market_data')

ET = pytz.timezone('America/New_York')

# Universe scanned for upcoming earnings. yfinance has no market-wide earnings
# calendar, so we poll the S4 universe plus a few index-moving mega-caps.
EARNINGS_UNIVERSE = [
    'NVDA', 'TSLA', 'AMD', 'AAPL', 'MSFT', 'META', 'GOOGL', 'AMZN',
    'JPM', 'NFLX', 'AVGO', 'COST',
]

# Known FOMC decision days (mirrors execution_engine.FOMC_DATES, decision day = 3rd date).
FOMC_DECISION_DAYS = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 16),
    date(2025, 12, 17),
]


def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, path)


# ── News ─────────────────────────────────────────────────────────────────────

def collect_news(limit: int = 10) -> list:
    """Top SPY / market news headlines. Handles both the legacy yfinance news
    schema (title/publisher/link at top level) and the 1.4.x schema (nested
    under 'content')."""
    headlines = []
    try:
        raw = yf.Ticker('SPY').news or []
        for item in raw:
            content = item.get('content', item)  # 1.4.x nests under 'content'
            title = content.get('title') or item.get('title')
            if not title:
                continue
            publisher = (
                (content.get('provider') or {}).get('displayName')
                or item.get('publisher')
                or 'unknown'
            )
            link = (
                (content.get('canonicalUrl') or {}).get('url')
                or (content.get('clickThroughUrl') or {}).get('url')
                or item.get('link')
                or ''
            )
            published = (
                content.get('pubDate')
                or content.get('displayTime')
                or item.get('providerPublishTime')
                or ''
            )
            headlines.append({
                'title': title,
                'publisher': publisher,
                'link': link,
                'published': str(published),
            })
            if len(headlines) >= limit:
                break
        log.info('Collected %d news headlines.', len(headlines))
    except Exception as exc:
        log.error('collect_news failed: %s', exc)
    return headlines


# ── Volatility term structure ────────────────────────────────────────────────

def _last_close(symbol: str) -> float:
    """Most recent close for a yfinance symbol; 0.0 on failure."""
    try:
        hist = yf.Ticker(symbol).history(period='5d')
        if hist is None or hist.empty:
            return 0.0
        return round(float(hist['Close'].dropna().iloc[-1]), 2)
    except Exception as exc:
        log.warning('_last_close(%s) failed: %s', symbol, exc)
        return 0.0


def collect_volatility() -> dict:
    vix   = _last_close('^VIX')
    vix3m = _last_close('^VIX3M')
    contango = round(vix3m / vix, 4) if vix > 0 and vix3m > 0 else None
    out = {
        'vix': vix,
        'vix3m': vix3m,
        'contango_ratio': contango,
        'regime': (
            'contango' if (contango is not None and contango >= 1.05)
            else 'backwardation' if contango is not None
            else 'unknown'
        ),
    }
    log.info('Volatility: VIX=%.2f VIX3M=%.2f contango=%s', vix, vix3m, contango)
    return out


# ── Earnings calendar ────────────────────────────────────────────────────────

def collect_earnings(days_ahead: int = 5) -> list:
    """Upcoming earnings within the next *days_ahead* days for the universe."""
    today = datetime.now(ET).date()
    horizon = today + timedelta(days=days_ahead)
    events = []
    for ticker in EARNINGS_UNIVERSE:
        try:
            cal = yf.Ticker(ticker).calendar or {}
            raw = cal.get('Earnings Date') if isinstance(cal, dict) else None
            if not raw:
                continue
            dates = raw if isinstance(raw, (list, tuple)) else [raw]
            for d in dates:
                ed = d.date() if isinstance(d, datetime) else d
                if isinstance(ed, date) and today <= ed <= horizon:
                    events.append({'ticker': ticker, 'date': ed.isoformat()})
        except Exception as exc:
            log.warning('earnings lookup failed for %s: %s', ticker, exc)
    events.sort(key=lambda e: e['date'])
    log.info('Collected %d upcoming earnings events (next %d days).', len(events), days_ahead)
    return events


# ── Economic calendar (computed — yfinance has no macro calendar) ─────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th *weekday* (0=Mon) of month/year (n=1 → first)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def collect_economic_events(days_ahead: int = 14) -> list:
    """FOMC / CPI / jobs (NFP) within the next *days_ahead* days.

    NFP = first Friday of the month (BLS standard). CPI ≈ the second Tuesday's
    week — released ~10th-15th; we use the 2nd Wednesday as a stable proxy. FOMC
    from the known decision schedule. Best-effort flags, not an official feed.
    """
    today = datetime.now(ET).date()
    horizon = today + timedelta(days=days_ahead)
    events = []

    # FOMC decision days
    for d in FOMC_DECISION_DAYS:
        if today <= d <= horizon:
            events.append({'event': 'FOMC', 'date': d.isoformat()})

    # NFP (jobs) + CPI for this month and next (covers the 2-week window).
    for delta_m in (0, 1):
        y = today.year + (today.month - 1 + delta_m) // 12
        m = (today.month - 1 + delta_m) % 12 + 1
        nfp = _nth_weekday(y, m, 4, 1)            # first Friday
        cpi = _nth_weekday(y, m, 2, 2)            # second Wednesday (proxy)
        for name, d in (('Jobs Report (NFP)', nfp), ('CPI', cpi)):
            if today <= d <= horizon:
                events.append({'event': name, 'date': d.isoformat()})

    events.sort(key=lambda e: e['date'])
    log.info('Collected %d economic events (next %d days).', len(events), days_ahead)
    return events


# ── Main ─────────────────────────────────────────────────────────────────────


# ── SPY SMA5 directional filter ──────────────────────────────────────────────

def collect_sma5() -> dict:
    """SPY 5-day simple moving average directional filter.

    Compares the most recent SPY close to the SMA of the last 5 trading-day
    closes. Within 0.2% of the SMA is treated as neutral (condor). Result is
    stored in market_context.json and read by execution_engine at startup.
    """
    result = {"spy_sma5": None, "spy_vs_sma5": "unknown", "spy_sma5_bias": "condor"}
    try:
        hist = yf.Ticker("SPY").history(period="15d")  # extra buffer for holidays
        if hist is None or hist.empty:
            log.warning("collect_sma5: no SPY history returned.")
            return result
        closes = hist["Close"].dropna()
        if len(closes) < 5:
            log.warning("collect_sma5: only %d closes — need 5.", len(closes))
            return result
        sma5     = round(float(closes.iloc[-5:].mean()), 2)
        spy_last = round(float(closes.iloc[-1]), 2)
        pct_diff = (spy_last - sma5) / sma5 * 100.0
        if pct_diff > 0.2:
            vs_sma5, bias = "above", "puts"    # bullish — put spreads favored
        elif pct_diff < -0.2:
            vs_sma5, bias = "below", "calls"   # bearish — skip put spreads
        else:
            vs_sma5, bias = "neutral", "condor"
        result = {"spy_sma5": sma5, "spy_vs_sma5": vs_sma5, "spy_sma5_bias": bias}
        log.info("SMA5: SPY=%.2f SMA5=%.2f (%.2f%%) -> vs=%s bias=%s",
                 spy_last, sma5, pct_diff, vs_sma5, bias)
    except Exception as exc:
        log.error("collect_sma5 failed: %s", exc)
    return result


def _ensure_market_open(gate_hour: int = 9, gate_minute: int = 25) -> None:
    """Block until at/after the 09:25 ET data gate. VIX / quotes pulled before
    the cash open return stale pre-market values, so we wait rather than snapshot
    garbage. Cron fires this job at 09:20 ET, so the wait is normally ~5 min; a
    run already at/after 09:25 proceeds immediately."""
    now = datetime.now(ET)
    gate = now.replace(hour=gate_hour, minute=gate_minute, second=0, microsecond=0)
    if now < gate:
        wait_s = (gate - now).total_seconds()
        log.info('Market not open yet (%s ET) — waiting %.0fs until %02d:%02d ET.',
                 now.strftime('%H:%M:%S'), wait_s, gate_hour, gate_minute)
        time.sleep(wait_s)


def main() -> None:
    _ensure_market_open()
    now = datetime.now(ET)
    log.info('Market data collection starting (%s ET).', now.isoformat())
    sma5_data = collect_sma5()
    context = {
        'generated_at': now.isoformat(),
        'date': now.date().isoformat(),
        'news': collect_news(limit=10),
        'volatility': collect_volatility(),
        'earnings_next_5_days': collect_earnings(days_ahead=5),
        'economic_events_next_2_weeks': collect_economic_events(days_ahead=14),
        'spy_sma5':      sma5_data.get('spy_sma5'),
        'spy_vs_sma5':   sma5_data.get('spy_vs_sma5', 'unknown'),
        'spy_sma5_bias': sma5_data.get('spy_sma5_bias', 'condor'),
    }
    _atomic_write_json(OUTPUT_FILE, context)
    log.info('Wrote market context → %s', OUTPUT_FILE)


if __name__ == '__main__':
    main()
