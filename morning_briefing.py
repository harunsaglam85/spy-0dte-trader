import os
import time
import requests
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

# --Config ─────────────────────────────────────────────────────────────────
load_dotenv(r"C:\Users\sagla\.tastytrade-mcp\.env")
POLYGON_KEY = os.getenv("POLYGON_API_KEY")
OUTPUT_FILE = r"C:\Users\sagla\morning_briefing.txt"

# --Polygon helpers ─────────────────────────────────────────────────────────
def pg(path, params=None):
    p = dict(params or {})
    p["apiKey"] = POLYGON_KEY
    r = requests.get(f"https://api.polygon.io{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()

def last_trading_day(before=None):
    d = (before or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def get_daily_bars(ticker, days=65):
    end   = last_trading_day()
    start = end - timedelta(days=days + 40)
    data  = pg(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
               {"adjusted": "true", "sort": "asc", "limit": days + 40})
    return data.get("results", [])[-days:]

def ma(values, n):
    if len(values) < n:
        return None
    return round(sum(values[-n:]) / n, 2)

def pct(a, b):
    return round((b - a) / a * 100, 2) if a else 0.0

def fmt_p(v):
    return f"${v:,.2f}"

def fmt_chg(a, b):
    d = b - a
    p = pct(a, b)
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.2f} ({sign}{p:.2f}%)"

def arrow(v):
    return "^" if v >= 0 else "v"

# --Data fetchers ───────────────────────────────────────────────────────────
def fetch_spy_bars():
    bars = get_daily_bars("SPY", 60)
    if not bars:
        raise RuntimeError("No SPY data returned from Polygon")
    return bars

def fetch_premarket(prev: date):
    try:
        d = pg(f"/v1/open-close/SPY/{prev}", {"adjusted": "true"})
        return d.get("afterHours"), d.get("preMarket")
    except Exception:
        return None, None

def fetch_vxx_bars():
    try:
        return get_daily_bars("VXX", 30)
    except Exception:
        return []

def fetch_put_call(last_close: float, prev: date):
    """
    Build P/C from ATM +/- 4 strikes at next weekly expiry.
    Throttled to avoid free-tier rate limits.
    """
    # Use next Friday at least 7 days out -avoids distorted expiry-day volume
    exp = prev + timedelta(days=1)
    while exp.weekday() != 4 or (exp - prev).days < 7:
        exp += timedelta(days=1)
    exp_str = exp.strftime("%y%m%d")

    atm     = round(last_close / 5) * 5
    strikes = [atm + i * 5 for i in range(-4, 5)]   # 9 strikes (18 requests)

    put_vol  = 0
    call_vol = 0
    hit      = 0
    errors   = 0

    for strike in strikes:
        sk = str(int(strike * 1000)).zfill(8)
        for cp in ("C", "P"):
            ticker = f"O:SPY{exp_str}{cp}{sk}"
            try:
                data = pg(f"/v2/aggs/ticker/{ticker}/range/1/day/{prev}/{prev}",
                          {"adjusted": "true"})
                results = data.get("results", [])
                if results:
                    v = results[0].get("v", 0)
                    if cp == "C":
                        call_vol += v
                    else:
                        put_vol += v
                    hit += 1
            except Exception:
                errors += 1
            time.sleep(0.25)   # stay well inside free-tier rate limits

    if hit == 0 or call_vol == 0:
        return None, None, None
    ratio = round(put_vol / call_vol, 3)
    return int(put_vol), int(call_vol), ratio


def fetch_bull_bear_volume():
    """
    SPXL (3x bull SPY) vs SPXU (3x bear SPY) volume ratio.
    Bullish when SPXL volume > SPXU. Uses free-tier daily aggs.
    """
    try:
        spxl = get_daily_bars("SPXL", 5)
        spxu = get_daily_bars("SPXU", 5)
        if not spxl or not spxu:
            return None, None, None
        l_vol = spxl[-1]["v"]
        u_vol = spxu[-1]["v"]
        ratio = round(u_vol / l_vol, 3) if l_vol else None   # >1 = bearish
        return int(l_vol), int(u_vol), ratio
    except Exception:
        return None, None, None

def fetch_calendar():
    """Forex Factory weekly calendar -no key required. Includes FOMC speakers."""
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        today_str = date.today().isoformat()
        return [
            e for e in r.json()
            if e.get("country") == "USD" and e.get("date", "").startswith(today_str)
        ]
    except Exception:
        return []

# --Bias engine ─────────────────────────────────────────────────────────────
def compute_bias(closes, ma20, ma50, vxx_bars, pc_ratio, bb_ratio, pm_price, last_close):
    signals = []

    signals.append(("SPY above 20-day MA",    closes[-1] > ma20))
    signals.append(("SPY above 50-day MA",    closes[-1] > ma50))

    if len(closes) >= 5:
        signals.append(("5-day price momentum",  closes[-1] > closes[-5]))

    if len(closes) >= 3:
        signals.append(("3-day close trend",     closes[-1] > closes[-3]))

    if vxx_bars:
        vxx_c  = [b["c"] for b in vxx_bars]
        vxx_ma = ma(vxx_c, min(20, len(vxx_c)))
        if vxx_ma:
            signals.append(("VXX below 20MA (low fear)",  vxx_c[-1] < vxx_ma))
        if len(vxx_c) >= 3:
            signals.append(("VXX falling (fear fading)", vxx_c[-1] < vxx_c[-3]))

    if pm_price is not None:
        signals.append(("Pre-market positive",   pm_price > last_close))

    if pc_ratio is not None:
        signals.append(("P/C ratio < 0.80",      pc_ratio < 0.80))

    if bb_ratio is not None:
        signals.append(("Bear ETF vol < Bull ETF vol", bb_ratio < 1.0))

    bull = sum(1 for _, b in signals if b)
    tot  = len(signals)
    score = bull / tot if tot else 0.5

    if score >= 0.65:
        bias = "BULLISH"
    elif score <= 0.40:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return bias, bull, tot, signals

# --Main ────────────────────────────────────────────────────────────────────
def main():
    now      = datetime.now()
    prev     = last_trading_day()
    lines    = []
    add      = lines.append

    add("=" * 64)
    add(f"  MORNING BRIEFING  |  {now.strftime('%A, %B %d, %Y  %I:%M %p')}")
    add("=" * 64)

    # --SPY price action ──────────────────────────────────────────────
    spy = fetch_spy_bars()
    closes = [b["c"] for b in spy]
    highs  = [b["h"] for b in spy]
    lows   = [b["l"] for b in spy]

    last_close = spy[-1]["c"]
    prev_close = spy[-2]["c"]
    last_high  = spy[-1]["h"]
    last_low   = spy[-1]["l"]
    last_open  = spy[-1]["o"]
    last_vol   = spy[-1]["v"]

    three_avg = round(sum(closes[-3:]) / 3, 2)
    ma20      = ma(closes, 20)
    ma50      = ma(closes, 50)

    add("")
    add("[ SPY  PRICE ACTION ]")
    add(f"  Last Close        : {fmt_p(last_close)}  ({fmt_chg(prev_close, last_close)})")
    add(f"  Prev Day O/H/L/C  : {fmt_p(last_open)} / {fmt_p(last_high)} / {fmt_p(last_low)} / {fmt_p(last_close)}")
    add(f"  Volume            : {int(last_vol):,}")
    add(f"  3-Day Avg Close   : {fmt_p(three_avg)}")
    add(f"  20-Day MA         : {fmt_p(ma20)}  ({'ABOVE' if last_close > ma20 else 'BELOW'})")
    add(f"  50-Day MA         : {fmt_p(ma50)}  ({'ABOVE' if last_close > ma50 else 'BELOW'})")
    add(f"  Gap from Prior Day: {fmt_chg(prev_close, last_close)}")

    # --Pre-market ────────────────────────────────────────────────────
    after_hrs, premarket = fetch_premarket(prev)
    add("")
    add("[ PRE-MARKET ]")
    if after_hrs:
        chg = after_hrs - last_close
        add(f"  After-Hours Price : {fmt_p(after_hrs)}  [{arrow(chg)}] {fmt_chg(last_close, after_hrs)}")
    if premarket:
        chg = premarket - last_close
        add(f"  Pre-Market Price  : {fmt_p(premarket)}  [{arrow(chg)}] {fmt_chg(last_close, premarket)}")
    if not after_hrs and not premarket:
        add("  Not available (live pre-mkt requires Polygon paid plan)")

    # --VXX / Fear gauge ──────────────────────────────────────────────
    vxx_bars = fetch_vxx_bars()
    add("")
    add("[ VXX  FEAR GAUGE (VIX futures proxy) ]")
    if vxx_bars:
        vxx_c   = [b["c"] for b in vxx_bars]
        vxx_last = vxx_c[-1]
        vxx_ma  = ma(vxx_c, min(20, len(vxx_c)))
        vxx_vs  = round(vxx_last - vxx_ma, 2) if vxx_ma else None
        trend3  = vxx_c[-1] - vxx_c[-3] if len(vxx_c) >= 3 else 0
        trend_s = f"Rising  [{arrow(trend3)}] {fmt_chg(vxx_c[-3], vxx_c[-1])}" if trend3 > 0 \
                  else f"Falling [{arrow(trend3)}] {fmt_chg(vxx_c[-3], vxx_c[-1])}"
        add(f"  VXX Last Close    : {fmt_p(vxx_last)}")
        add(f"  VXX 20-Day MA     : {fmt_p(vxx_ma)}")
        if vxx_vs is not None:
            vs_label = "Elevated fear" if vxx_vs > 0 else "Below avg (calm)"
            add(f"  VXX vs MA         : {'+' if vxx_vs >= 0 else ''}{vxx_vs:.2f}  ({vs_label})")
        add(f"  VXX 3-Day Trend   : {trend_s}")
    else:
        add("  VXX data unavailable")

    # --Put/Call ratio ────────────────────────────────────────────────
    put_vol, call_vol, pc_ratio = fetch_put_call(last_close, prev)
    add("")
    add("[ PUT/CALL RATIO  SPY Near-the-Money Options ]")
    if pc_ratio is not None:
        pc_label = "Bullish" if pc_ratio < 0.70 else ("Bearish" if pc_ratio > 1.00 else "Neutral")
        add(f"  Put Volume        : {put_vol:,}")
        add(f"  Call Volume       : {call_vol:,}")
        add(f"  P/C Ratio         : {pc_ratio:.2f}  -> {pc_label}")
        add(f"  (NTM strip: ATM {fmt_p(round(last_close/5)*5)} +/- 4 strikes @ next Friday expiry)")
    else:
        add("  P/C data unavailable -no options volume found for NTM strip")

    # --SPXL/SPXU bull-bear volume ────────────────────────────────────
    spxl_vol, spxu_vol, bb_ratio = fetch_bull_bear_volume()
    add("")
    add("[ BULL/BEAR SENTIMENT  SPXL vs SPXU Volume ]")
    if bb_ratio is not None:
        bb_label = "Bullish" if bb_ratio < 0.80 else ("Bearish" if bb_ratio > 1.25 else "Neutral")
        add(f"  SPXL Vol (3x Bull): {spxl_vol:,}")
        add(f"  SPXU Vol (3x Bear): {spxu_vol:,}")
        add(f"  Bear/Bull Ratio   : {bb_ratio:.2f}  -> {bb_label}")
    else:
        add("  SPXL/SPXU data unavailable")

    # --Economic calendar ─────────────────────────────────────────────
    events    = fetch_calendar()
    fed_today = [e for e in events if any(k in e.get("title", "") for k in ("FOMC", "Fed ", "Federal"))]

    add("")
    add("[ ECONOMIC CALENDAR  TODAY (USD) ]")
    if events:
        impact_icon = {"High": "[H]", "Medium": "[M]", "Low": "[ ]", "Holiday": "[~]"}
        for e in events:
            try:
                dt       = datetime.fromisoformat(e["date"])
                time_str = dt.strftime("%I:%M %p")
            except Exception:
                time_str = "TBD"
            impact = e.get("impact", "Low")
            icon   = impact_icon.get(impact, "[ ]")
            fore   = f"  Forecast: {e['forecast']}" if e.get("forecast") else ""
            prev_v = f"  Prev: {e['previous']}"     if e.get("previous") else ""
            add(f"  {icon} {time_str:9s}  {e.get('title', '')}{fore}{prev_v}")
    else:
        add("  No USD events found (market may be closed today)")

    add("")
    add("[ FED / FOMC EVENTS TODAY ]")
    if fed_today:
        for e in fed_today:
            try:
                dt       = datetime.fromisoformat(e["date"])
                time_str = dt.strftime("%I:%M %p")
            except Exception:
                time_str = "TBD"
            add(f"  [*] {time_str:9s}  {e.get('title', '')}")
    else:
        add("  None scheduled today")

    # --Bias ──────────────────────────────────────────────────────────
    pm_for_bias = premarket if premarket else after_hrs
    bias, bull_n, total_n, sigs = compute_bias(
        closes, ma20, ma50, vxx_bars, pc_ratio, bb_ratio, pm_for_bias, last_close
    )

    add("")
    add("=" * 64)
    add(f"  MARKET BIAS:  {bias}   ({bull_n}/{total_n} signals bullish)")
    add("  Signals:")
    for label, is_bull in sigs:
        mark = " [+]" if is_bull else " [-]"
        add(f"    {mark}  {label}")
    add("=" * 64)

    # --Key levels ────────────────────────────────────────────────────
    res5   = max(highs[-5:])
    sup5   = min(lows[-5:])
    res20  = max(highs[-20:])
    sup20  = min(lows[-20:])

    add("")
    add("[ KEY LEVELS TO WATCH ]")
    add(f"  Resistance (5-day high)   : {fmt_p(res5)}")
    add(f"  Resistance (20-day high)  : {fmt_p(res20)}")
    add(f"  20-Day MA                 : {fmt_p(ma20)}")
    add(f"  50-Day MA                 : {fmt_p(ma50)}")
    add(f"  Support (5-day low)       : {fmt_p(sup5)}")
    add(f"  Support (20-day low)      : {fmt_p(sup20)}")

    # --Trade recommendation ──────────────────────────────────────────
    vxx_elevated = False
    if vxx_bars:
        vxx_c2  = [b["c"] for b in vxx_bars]
        vxx_ma2 = ma(vxx_c2, min(20, len(vxx_c2)))
        if vxx_ma2:
            vxx_elevated = vxx_c2[-1] > vxx_ma2

    add("")
    add("[ TRADE RECOMMENDATION ]")
    if bias == "BULLISH":
        note = "  (elevated vol - size down)" if vxx_elevated else ""
        add(f"  Direction   : CALLS [^]{note}")
        add(f"  Entry Zone  : {fmt_p(last_close)} to {fmt_p(ma20)}")
        add(f"  Target 1    : {fmt_p(res5)}")
        add(f"  Target 2    : {fmt_p(res20)}")
        add(f"  Stop        : Below {fmt_p(sup5)}")
    elif bias == "BEARISH":
        note = "  (elevated vol - size down)" if vxx_elevated else ""
        add(f"  Direction   : PUTS [v]{note}")
        add(f"  Entry Zone  : {fmt_p(last_close)} to {fmt_p(ma20)}")
        add(f"  Target 1    : {fmt_p(sup5)}")
        add(f"  Target 2    : {fmt_p(sup20)}")
        add(f"  Stop        : Above {fmt_p(res5)}")
    else:
        add("  Direction   : NO TRADE - conflicting signals, wait for breakout")
        add(f"  Breakout UP  > {fmt_p(res5)}  -> consider calls")
        add(f"  Breakdown   < {fmt_p(sup5)}  -> consider puts")

    # --Risk factors ──────────────────────────────────────────────────
    add("")
    add("[ RISK FACTORS ]")
    risks = []

    if vxx_bars and vxx_elevated:
        vxx_c3  = [b["c"] for b in vxx_bars]
        vxx_ma3 = ma(vxx_c3, min(20, len(vxx_c3)))
        risks.append(f"  [!] VXX above MA ({fmt_p(vxx_c3[-1])} vs MA {fmt_p(vxx_ma3)}) -fear elevated, vol is high")

    if vxx_bars and len(vxx_bars) >= 3:
        vxx_c4 = [b["c"] for b in vxx_bars]
        if vxx_c4[-1] > vxx_c4[-3]:
            risks.append(f"  [!] VXX rising 3 days -increasing fear/volatility")

    if last_close < ma20:
        risks.append(f"  [!] SPY below 20-Day MA ({fmt_p(ma20)}) -near-term bearish structure")
    if last_close < ma50:
        risks.append(f"  [!] SPY below 50-Day MA ({fmt_p(ma50)}) -intermediate bearish structure")

    if pc_ratio and pc_ratio > 1.0:
        risks.append(f"  [!] P/C ratio {pc_ratio:.2f} > 1.0 - heavy put buying / hedging")

    if bb_ratio and bb_ratio > 1.25:
        risks.append(f"  [!] SPXU/SPXL vol ratio {bb_ratio:.2f} - bear ETF vol >> bull ETF, institutional hedging")

    high_events = [e for e in events if e.get("impact") == "High"]
    for e in high_events:
        try:
            time_str = datetime.fromisoformat(e["date"]).strftime("%I:%M %p")
        except Exception:
            time_str = "today"
        risks.append(f"  [!] High-impact event: '{e.get('title')}' at {time_str}")

    for e in fed_today:
        risks.append(f"  [!] Fed speaker today: {e.get('title')} -potential vol spike")

    if not risks:
        risks.append("  [ok] No major risk flags detected")

    lines.extend(risks)

    add("")
    add(f"  Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}  |  Data: Polygon.io + Forex Factory")
    add("=" * 64)

    output = "\n".join(lines)
    print(output)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"\n  >> Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
