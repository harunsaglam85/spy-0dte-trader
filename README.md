# SPY 0DTE Algorithmic Options Trader

![Python](https://img.shields.io/badge/Python-3.10-3776AB?style=flat&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)
![Status](https://img.shields.io/badge/Status-Paper%20Trading-orange?style=flat)
![Cloud](https://img.shields.io/badge/Cloud-Hetzner%20CCX13-red?style=flat)
![Data](https://img.shields.io/badge/Data-ThetaData%20Standard-blue?style=flat)
![Strategies](https://img.shields.io/badge/Strategies-21%20Active-brightgreen?style=flat)

**Production-grade SPY 0DTE options trading system with AI-powered research, real-time risk management, and 24/7 autonomous operation.**

21 strategies run simultaneously on Hetzner CCX13, managed by Hermes — an AI agent that monitors performance, runs weekly strategy research, and sends real-time Telegram alerts.

---

![Strategy Win Rates](Screenshot%202026-06-17%20183456.png)

![VIX Regime Analysis](Screenshot%202026-06-17%20183507.png)

## Live System

| Component | Detail |
|---|---|
| Deployment | Hetzner CCX13 (Ubuntu 22.04) — 24/7 systemd managed |
| Strategies | 21 active (8 confirmed × 3 contracts, 13 experimental × 1 contract) |
| Execution engine | `hermes_system/execution_engine.py` — 60s risk loop |
| AI research agent | Hermes (Claude Sonnet) — daily briefs, Sunday strategy research |
| Options data | ThetaData Standard — real bid/ask, v3 REST API |
| Quotes + greeks | Tradier production API — VIX, VIX3M, SPY, options chain |
| Broker | Tradier sandbox (paper) → production at go-live |
| Alerts | Telegram bot — real-time entries, exits, rollbacks, daily reports |

---

## Strategy Performance (5-Year Backtest)

All strategies validated with 60/40 walk-forward split, blind 2025 holdout, and 2,000-session bootstrap CI.

### Confirmed Strategies (3 contracts each)

| Strategy | Entry | VIX Range | OOS WR | Blind WR | Sharpe | Status |
|---|---|---|---|---|---|---|
| R3A — Monday put spread | Mon 10:30 AM | 15–22 | 91.0% | 89.2% | 14.59 | ✅ Active |
| R3B — Wednesday put spread | Wed 10:30 AM | 15–22 | 78.6% | 81.3% | 11.2 | ✅ Active |
| R3D — Mon/Wed/Fri put spread | MWF 10:15 AM | 15–22 | 79.5% | 83.0% | 12.4 | ✅ Active |
| R3E — Wednesday iron condor | Wed 10:30 AM | 13–18 | 98.0% | 100% | 41.77 | ✅ Active |
| R8 — Friday 1PM + VWAP | Fri 1:00 PM | 15–22 | 80.4% | 82.1% | 10.8 | ✅ Active |
| R10 — Tuesday above VWAP | Tue 10:30 AM | 15–22 | 84.8% | 86.2% | 13.1 | ✅ Active |

### Combined Portfolio (OOS)
N=662  WR=83.5%  P&L=+$7,712  Sharpe=14.59

Blind 2025: N=410  WR=85.9%  P&L=+$4,949

Profitable in every year 2021–2026 including 2022 bear market
---

## Key Research Findings

### The VIX Term Structure Edge

The single most important filter discovered across 1,357 trading days:

| Regime | Condition | WR | Sharpe |
|---|---|---|---|
| Strong contango | VIX3M/VIX ≥ 1.10 | 86.2% | 13.65 |
| Contango | VIX3M/VIX ≥ 1.05 | 84.2% | 12.61 |
| Neutral | VIX3M/VIX 1.00–1.05 | 71.8% | 4.2 |
| Backwardation | VIX3M/VIX < 1.00 | 41.6% | -1.02 |

Trading only in contango eliminates 15% of trading days that account for nearly all losses.

### VIX Floor (18+)
At VIX < 17, 90.7% of days have no tradeable credit on $2-wide spreads. The VIX 18 global floor was added after live data confirmed this — paper trading on Jun 15 at VIX 16.3 saw zero fills all session.

### Credit Threshold
$0.20 minimum credit is the validated floor. The $0.10–$0.19 band shows 0% WR in live data. $0.40+ is unreachable on standard $2-wide 0.20-delta spreads.

---

## Architecture
┌─────────────────────────────────────────────────────────┐

│                      DATA LAYER                          │

│  Tradier │ ThetaData v3 │ Alpaca │ yfinance │ OpenBB    │

│              core/data_feeds.py                         │

└──────────────────────┬──────────────────────────────────┘

│

┌──────────────────────▼──────────────────────────────────┐

│                   STRATEGY LAYER                         │

│  R-series (confirmed) │ T-series (experimental)         │

│  Filters: VIX ≥18, contango ≥1.05, credit ≥$0.20       │

└──────────────────────┬──────────────────────────────────┘

│

┌──────────────────────▼──────────────────────────────────┐

│              HERMES EXECUTION ENGINE                     │

│  execution_engine.py                                    │

│  • Order-ID fill verification (C5)                      │

│  • Atomic writes — positions.json + trade logs          │

│  • Strike blacklist — repeated rejection guard          │

│  • VIX term structure check (hourly)                    │

│  • Force-exit sweep 3:58 PM ET                          │

│  • Heartbeat monitoring (U3)                            │

└──────┬──────────────────┬──────────────────┬────────────┘

│                  │                  │

┌──────▼──────┐  ┌────────▼────────┐  ┌─────▼──────────┐

│   Tradier   │  │  Hermes AI      │  │  Hetzner CCX13 │

│   Sandbox   │  │  Agent          │  │  systemd       │

│   (paper)   │  │  Telegram bot   │  │  24/7 uptime   │

└─────────────┘  └─────────────────┘  └────────────────┘

---

## Infrastructure

| Layer | Technology |
|---|---|
| Server | Hetzner CCX13 — 4 vCPU, 8GB RAM, Ubuntu 22.04 |
| Process management | systemd — `hermes-engine.service`, `thetadata.service` |
| Options data | ThetaData Terminal v3 — local REST API port 25503 |
| Quotes + greeks | Tradier production API |
| Intraday VWAP | Tradier timesales — real-time 5-min bars |
| AI agent | Hermes (Claude Sonnet via OpenRouter) |
| Alerts | Telegram Bot API — real-time two-way |
| News sentiment | yfinance + OpenBB |
| Language | Python 3.10 |
| Key packages | pandas, numpy, scipy, requests, pytz, python-dotenv |

---

## Risk Controls

| Control | Value |
|---|---|
| VIX minimum (global floor) | 18 |
| VIX3M/VIX contango minimum | 1.05 |
| Minimum credit | $0.20 |
| Max daily loss | $8,000 (paper) → $750 live |
| Confirmed strategy contracts | 3 |
| Experimental strategy contracts | 1 |
| Force exit time | 3:45–3:58 PM ET |
| Strike blacklist | 30min after 3 consecutive rejections |
| Rollback | Immediate on any leg rejection |

---

## Deployment

```bash
git clone https://github.com/harunsaglam85/spy-0dte-trader
cd spy-0dte-trader
cp .env.example .env
# Add API keys to .env

systemctl enable hermes-engine thetadata
systemctl start thetadata
sleep 10
systemctl start hermes-engine
```

### Environment Variables
TRADIER_API_KEY=...

TRADIER_SANDBOX_KEY=...

TRADIER_SANDBOX_ACCOUNT_ID=...

ALPACA_API_KEY=...

ALPACA_SECRET_KEY=...

THETADATA_USERNAME=...

THETADATA_PASSWORD=...

TELEGRAM_BOT_TOKEN=...

TELEGRAM_CHAT_ID=...

OPENROUTER_API_KEY=...
---

## Backtesting

```bash
python3 backtest_real_data.py          # 5-year real bid/ask backtest
python3 vix_ts_backtest.py             # VIX term structure analysis
python3 hermes_researcher.py --dry-run # Weekly research dry run
python3 backtests/credit_hypothesis_backtest.py  # Hypothesis testing
```

---

## Paper Trading Status

| Day | Date | P&L | Notes |
|---|---|---|---|
| 1–2 | Jun 9–10 | $0 | Infrastructure shakeout |
| 3 | Jun 11 | +$366 | First clean trades |
| 4 | Jun 12 | +$1,260 clean | Sandbox issues |
| 5 | Jun 15 | +$321 | Low VIX day |
| 6–7 | Jun 16–17 | $0 | VIX below floor |

**Cumulative clean P&L: +$1,947**

---

## Research Philosophy

1. **Assume dead until proven otherwise** — pre-registered kill criteria before any OOS data
2. **Real bid/ask or it did not happen** — ThetaData EOD pricing for all backtests
3. **Adversarial case first** — strongest argument against deployment written explicitly
4. **AI-assisted research** — Hermes runs weekly backtests, surfaces hypotheses, waits for human approval before any config change

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Built with ThetaData real options pricing, adversarial backtesting, and an AI research agent that learns from every trade.*
