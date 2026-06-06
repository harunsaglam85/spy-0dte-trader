# Hermes System — Autonomous 8-Strategy Trading Engine

Autonomous paper trading system running 8 backtested SPY 0DTE strategies via Tradier sandbox. Includes nightly pattern analysis, AI-generated daily insights, and real-time position monitoring.

---

## Architecture

```
hermes_system/
├── execution_engine.py    # Main trading loop — 8 strategies, 60s poll
├── pattern_engine.py      # Nightly zero-token trade analysis
├── hermes_daily_brief.py  # 300-word structured daily brief
├── hermes_trigger.py      # OpenRouter AI insights (claude-sonnet-4-6)
├── monitor.py             # Real-time position monitor
├── setup.sh               # Hetzner one-shot setup script
└── README.md

Runtime data (on Hetzner, /root/hermes_system/):
├── trades/
│   └── YYYY-MM-DD.json    # one file per trading day
├── insights/
│   └── YYYY-MM-DD.md      # daily AI insights
├── pattern_summary.json   # latest nightly analysis
├── daily_brief.txt        # latest plain-text brief
└── logs/
    ├── execution.log
    ├── pattern.log
    ├── brief.log
    ├── trigger.log
    └── researcher.log
```

All files import `core/` from the parent `spy-0dte-trader/` package via `sys.path.insert(0, '/root/spy-0dte-trader')`.

---

## Strategies

| ID  | Days            | Entry Time    | Type          | VIX Range | Delta | Conditions                 |
|-----|-----------------|---------------|---------------|-----------|-------|----------------------------|
| R3A | Mon             | 10:30–10:45   | Put spread    | 15–22     | 0.20  | —                          |
| R3B | Wed             | 10:30–10:45   | Put spread    | 15–22     | 0.20  | —                          |
| R3C | Any             | 10:00–11:00   | Call spread   | 15–22     | 0.20  | SPY < VWAP                 |
| R3D | Mon/Wed/Fri     | 10:30–10:45   | Put spread    | 15–22     | 0.20  | Skip FOMC weeks            |
| R3E | Wed             | 10:30–10:45   | Iron condor   | 13–18     | 0.16  | —                          |
| R8  | Fri             | 13:00–13:30   | Put spread    | 15–22     | 0.20  | SPY > VWAP                 |
| R10 | Tue             | 10:45–11:00   | Put spread    | 15–22     | 0.20  | SPY > MA50 + VWAP          |
| S4  | Any             | 09:45–10:15   | Earnings call | —         | ATM   | 5 days pre-earnings, 8 tickers |

**Spread construction**: sell strike at target delta, buy strike ±$2.00.

---

## Exit Rules

| Strategy    | Profit Target | Stop Loss    | Force Exit |
|-------------|---------------|--------------|------------|
| R3A/B/C/D   | 75% of credit | 2× credit    | 3:45 PM    |
| R3E         | 50% of credit | 2× credit    | 3:30 PM    |
| R8          | 70% of credit | 1.8× credit  | 3:30 PM    |
| R10         | 75% of credit | 2× credit    | 3:45 PM    |
| S4          | 35% gain      | 25% loss     | Earnings date or 3:55 PM |

---

## Risk Limits

- **Per-strategy daily loss**: $200 max
- **Total daily loss**: $1,000 max (halts all new entries)
- **Contracts**: 1 per strategy per day

---

## S4 Earnings Universe

NVDA, TSLA, AMD, AAPL, MSFT, META, GOOGL, AMZN

Earnings calendar: `/root/spy-0dte-trader/data/earnings_calendar.json`

Format:
```json
{
  "NVDA": ["2025-02-26", "2025-05-28", "2025-08-27"],
  "TSLA": ["2025-01-29", "2025-04-22"]
}
```

---

## Trade Log Schema

Each entry in `trades/YYYY-MM-DD.json`:

```json
{
  "strategy":          "R3A",
  "entry_time":        "2025-06-02T10:31:00-04:00",
  "entry_price":       0.48,
  "theoretical_mid":   0.49,
  "fill_slippage_pct": 2.0,
  "vix_entry":         17.3,
  "spy_entry":         524.10,
  "vwap_entry":        523.45,
  "delta_entry":       -0.21,
  "theta_entry":       -0.08,
  "spy_vs_ma50":       12.50,
  "vix_direction":     "neutral",
  "day_of_week":       "Monday",
  "days_to_fomc":      18,
  "exit_time":         "2025-06-02T12:15:00-04:00",
  "exit_price":        0.12,
  "exit_reason":       "profit_target",
  "pnl":               36.00,
  "hold_minutes":      104,
  "market_regime":     "normal"
}
```

---

## Cron Schedule (America/New_York)

| Time              | Script                  | Purpose                         |
|-------------------|-------------------------|---------------------------------|
| 9:45 AM  Mon–Fri  | execution_engine.py     | Start trading engine            |
| 3:50 PM  Mon–Fri  | (log entry)             | Force-exit safety net           |
| 4:30 PM  Mon–Fri  | pattern_engine.py       | Nightly analysis                |
| 4:45 PM  Mon–Fri  | hermes_daily_brief.py   | Generate daily brief            |
| 5:00 PM  Mon–Fri  | hermes_trigger.py       | AI insights (~$0.05/day)        |
| 8:00 PM  Friday   | hermes_trigger.py       | Weekly review                   |
| 8:00 PM  Sunday   | hermes_researcher.py    | Strategy research pipeline      |

---

## Required .env Variables

```
# Market data (Tradier production)
TRADIER_API_KEY=

# Paper orders (Tradier sandbox)
TRADIER_SANDBOX_KEY=
TRADIER_SANDBOX_ACCOUNT_ID=

# Daily bars (Alpaca)
ALPACA_API_KEY=
ALPACA_API_SECRET=

# Alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# AI insights
OPENROUTER_API_KEY=
```

---

## Deployment

```bash
# 1. Push code to Hetzner
git push origin master
ssh root@<hetzner-ip> "cd /root/spy-0dte-trader && git pull"

# 2. Run setup (first time only)
ssh root@<hetzner-ip> "bash /root/spy-0dte-trader/hermes_system/setup.sh"

# 3. Start live monitor (optional — runs in a separate tmux pane)
tmux new-session -d -s monitor "python /root/spy-0dte-trader/hermes_system/monitor.py"

# 4. Check logs
tail -f /root/hermes_system/logs/execution.log
```

---

## Pattern Engine Output

`pattern_summary.json` is regenerated nightly at 4:30 PM. Each strategy has:

- `total` — overall win rate, trade count, avg P&L
- `by_vix_bucket` — win rates for VIX 13-15, 15-17, 17-19, 19-21, 21-23
- `by_day_of_week` — Monday through Friday
- `by_entry_time` — six 30-minute windows from 9:30 AM to 2:00 PM
- `by_market_regime` — low_vol / normal / elevated / high_vol
- `slippage` — average fill slippage vs theoretical mid
- `rolling_10_wr` — win rate on the last 10 trades
- `kill_flag` — triggered when WR < 40% or avg P&L < -$30 over ≥10 trades

Cross-strategy Pearson correlation is computed on daily P&L series.

---

## Kill Criteria

A strategy is flagged for review (not auto-disabled) when, over ≥10 live trades:
- Win rate < 40%, **or**
- Average P&L per trade < -$30

Flagged strategies appear in the daily brief and Telegram alerts. Manual review and disable via `execution_engine.py` STRATEGIES dict.
