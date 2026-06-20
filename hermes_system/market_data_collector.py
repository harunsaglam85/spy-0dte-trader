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
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
import yfinance as yf

# ── Paths / logging ──────────────────────────────────────────────────────────
HERMES_ROOT  = Path('/root/hermes_system')
LOG_DIR      = HERMES_ROOT / 'logs'
OUTPUT_FILE  = HERMES_ROOT / 'market_context.json'
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_DIR / 'market_data.log'), logging.StreamHandler()],
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

def main() -> None:
    now = datetime.now(ET)
    log.info('Market data collection starting (%s ET).', now.isoformat())
    context = {
        'generated_at': now.isoformat(),
        'date': now.date().isoformat(),
        'news': collect_news(limit=10),
        'volatility': collect_volatility(),
        'earnings_next_5_days': collect_earnings(days_ahead=5),
        'economic_events_next_2_weeks': collect_economic_events(days_ahead=14),
    }
    _atomic_write_json(OUTPUT_FILE, context)
    log.info('Wrote market context → %s', OUTPUT_FILE)


if __name__ == '__main__':
    main()
