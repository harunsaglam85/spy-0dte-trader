# Algorithmic Options Trading System

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)
![Status](https://img.shields.io/badge/Status-Active-brightgreen?style=flat)
![Cloud](https://img.shields.io/badge/Cloud-Oracle%20Free%20Tier-red?style=flat&logo=oracle)
![Data](https://img.shields.io/badge/Data-ThetaData%20Real%20Bid%2FAsk-blue?style=flat)

**Production-grade SPY options trading system with adversarial backtesting, real bid/ask validation, and 24/7 cloud deployment.**

Five strategies run simultaneously on Oracle Cloud Always Free, managed by a self-learning performance tracker that monitors VIX regime, time-of-day, and day-of-week performance drift in real time.

---

## Live System

| Component | Detail |
|---|---|
| Deployment | Oracle Cloud Always Free (Ubuntu 20.04) — 24/7 uptime |
| Strategies running | 5 simultaneously (Config D, Credit Spread, 5-DTE, Earnings, VPIN) |
| Orchestrator | `main.py` — polls every 5 min, manages kill switches & daily resets |
| Self-learning engine | `core/performance.py` — VIX bucket, time-of-day, day-of-week analysis |
| Dashboard | Flask + Chart.js web UI with live P&L and strategy diagnostics |
| Broker | Tastytrade (OAuth2) + Tradier for real-time quotes |
| Database | SQLite WAL — all trades, suggestions, and metrics persisted |

> The performance tracker runs after every closed trade and every 10 trades per strategy. It surfaces parameter-drift suggestions to human review — it **never auto-applies them**.

---

## Key Research Findings

**2,500+ backtested scenarios. 5 years of real ThetaData bid/ask pricing. One finding dominated everything else:**

> *Only strategies with large expected moves survive real bid/ask spreads.*
> Strategies that looked profitable on synthetic Black-Scholes pricing lost 50-90% of their edge when re-run on actual EOD bid/ask quotes. Small-premium strategies that appear viable on midpoints get destroyed by real fills.

### The Bid/Ask Reality Check

| Strategy | Synthetic P&L | Real Data P&L | Delta | Verdict |
|---|---|---|---|---|
| Credit Spread 0DTE | +$2,948 | +$2,540 | -14% | **Survived** |
| 5-DTE Momentum | +$28,098 | +$3,995 | -86% | Downgraded B to C |
| FOMC/CPI Catalyst | +$5,404 | +$178 | -97% | **Dead** |
| Earnings Momentum | +$26,882 | +$26,882 | 0% | **Survived** |
| Iron Condor 0DTE | -$1,990 | -$4,064 | worse | **Dead** |

The Credit Spread kept 86% of its synthetic edge on real pricing — the only 0DTE strategy to do so. Everything with small expected moves (FOMC directional bets, iron condor premium harvesting) collapsed.

---

## Strategy Research Results

All strategies were validated with:
- **Pre-registered kill criteria** locked before seeing out-of-sample results
- **5-year walk-forward** (2021-2026) with expanding training windows
- **Blind 2025 holdout** — never touched during development
- **Bootstrap P(positive)** with 10,000 simulated sessions
- **Minimum 30 OOS trades** required for any grade above C

### Full Strategy Comparison Table

| Strategy | IS Trades | OOS Trades | OOS WR | OOS PF | Real P&L | Grade | Status |
|---|---|---|---|---|---|---|---|
| Credit Spread 0DTE | 207 | 70 | **81.4%** | **2.32** | **+$3,639** | **A** | **Active** |
| Earnings Momentum | 31 | 33 | 33.3% | 2.30 | +$26,882 | **A** | Active |
| Config D (Mon/Fri 0DTE) | — | 100 | 67.0% | 2.02 | — | B | Active |
| 5-DTE Momentum | 262 | 127 | 46.5% | 1.66 | +$3,995 | C | Research |
| Earnings Straddle | 74 | 67 | 46.3% | 1.56 | — | C | Research |
| FOMC/CPI Catalyst | 31 | 9 | **11.1%** | 0.01 | -$4,443 | **F** | **Dead** |
| Iron Condor 0DTE | 135 | 5 | 0.0% | N/A | -$4,064 | **F** | **Dead** |
| VIX Spike Mean Reversion | 1 | 6 | 50.0% | 1.54 | — | Dead | Dead |
| Post-Earnings Continuation | 19 | 18 | 22.2% | 0.72 | — | Dead | Dead |
| Single Stock Momentum | 16 | 19 | 42.1% | 1.33 | — | Dead | Dead |

**Grade Key:** A = Deploy-ready | B = Pilot only | C = Research | F = No edge | Dead = Failed pre-registered kill criterion

### Credit Spread Deep Dive (Grade A)

The only 0DTE strategy that passed every kill criterion, including blind 2025 validation:

```
Full period  (2021-2026): N=277  WR=78.7%  PF=2.32  Sharpe=5.94  Boot=100%
Blind 2025 (unseen):      N= 70  WR=81.4%  P&L=+$849
Profitable in every calendar year from 2021 to 2026, including the 2022 bear market.
```

Year-by-year breakdown (real ThetaData bid/ask, no synthetic pricing):

| Year | Trades | Win Rate | P&L | PF |
|---|---|---|---|---|
| 2021 | 77 | 80.5% | +$1,438 | 2.99 |
| 2022 (bear) | 20 | 60.0% | +$9 | 1.02 |
| 2023 | 54 | 79.6% | +$708 | 2.03 |
| 2024 | 29 | 96.6% | +$384 | 21.31 |
| 2025 (blind) | 70 | 81.4% | +$849 | — |

**Why it works:** The put credit spread on SPY 0DTE captures structural short-vol premium on low-VIX trend days. The entry filter (VIX 15-22, Mon/Fri only, morning bias confirmed) eliminates the events where spreads blow out. Real bid/ask pricing barely dents the edge because the spread width is wide enough to absorb realistic fills.

### What Killed Every Other Strategy

**FOMC/CPI (OOS WR 11.1%):** Market makers widen spreads most aggressively on macro event days. Real bid/ask pricing revealed the synthetic P&L of +$5,404 was entirely an artifact of midpoint pricing. OOS real P&L: -$4,443.

**Iron Condor 0DTE:** 39.3% WR with wide spreads. The short wing gets hit by the same tail events that iron condors are supposed to profit from. Real pricing reduced already-negative returns further.

**5-DTE Momentum:** Synthetic pricing showed a Grade B result (+$28k). Real data collapsed this to +$4k (Grade C). 5-DTE ATM options have wide bid/ask that consume most of the expected edge on marginal signal entries.

---

## Architecture

```
+---------------------------------------------------------------------+
|                         DATA LAYER                                  |
|                                                                     |
|  +-----------+  +-----------+  +-----------+  +------------------+  |
|  |  Tradier  |  |  Alpaca   |  | ThetaData |  |    yfinance      |  |
|  |  (quotes) |  | (5m bars) |  | (EOD opts)|  |  (VIX, daily)   |  |
|  +-----+-----+  +-----+-----+  +-----+-----+  +--------+---------+  |
|        +---------------+---------------+----------------+           |
|                         core/data_feeds.py                          |
+--------------------------------+------------------------------------+
                                 |
+--------------------------------v------------------------------------+
|                      STRATEGY LAYER                                 |
|                                                                     |
|  +-----------+  +---------------+  +--------+  +---------------+   |
|  | Config D  |  | Credit Spread |  | 5-DTE  |  |   Earnings    |   |
|  |(Mon/Fri   |  |(0DTE put      |  |(ATM    |  |(pre-earnings  |   |
|  | 0DTE call)|  | spread)       |  | calls) |  | momentum)     |   |
|  +-----+-----+  +------+--------+  +---+----+  +-------+-------+   |
|        +----------------+--------------+--------------+            |
|                                          +----------+              |
|                                          |   VPIN   |              |
|                                          |(flow tox)|              |
|                                          +----------+              |
+--------------------------------+------------------------------------+
                                 |
+--------------------------------v------------------------------------+
|                    ORCHESTRATION LAYER                              |
|                                                                     |
|   main.py — Master Orchestrator                                     |
|   +-- 5-minute polling loop (respects Tradier rate limits)          |
|   +-- Per-strategy loss limits ($500/strategy, $2,000/day total)    |
|   +-- Kill switches (file-based, no restart needed)                 |
|   +-- End-of-day reporting                                          |
|                                                                     |
|   core/performance.py — Self-Learning Engine                        |
|   +-- VIX bucket analysis (calm / normal / stressed / crisis)       |
|   +-- Time-of-day performance breakdown                             |
|   +-- Day-of-week analysis — suggestions only, never auto-applied   |
|                                                                     |
|   core/database.py — SQLite WAL                                     |
|   +-- All trades, suggestions, and daily summaries persisted        |
+--------------------------------+------------------------------------+
                                 |
+--------------------------------v------------------------------------+
|                   DEPLOYMENT & MONITORING                           |
|                                                                     |
|   Oracle Cloud Always Free (Ubuntu 20.04 ARM)                       |
|   +-- Docker container — python:3.12-slim                           |
|   +-- Healthcheck: log recency check every 30s                      |
|   +-- dashboard.py — Flask web UI (Chart.js)                        |
|   +-- morning_briefing.py — pre-market signal summary               |
+---------------------------------------------------------------------+
```

---

## Tech Stack

### Core
| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Containerization | Docker (python:3.12-slim) |
| Cloud | Oracle Cloud Always Free — ARM Ubuntu 20.04 |
| Database | SQLite with WAL mode |

### Data & Pricing
| Source | Data |
|---|---|
| ThetaData | Real EOD option bid/ask quotes (backtesting) |
| Tradier | Real-time option quotes, live fills |
| Alpaca Markets | 5-minute SPY bars (intraday) |
| yfinance | VIX daily, earnings calendar |
| Polygon | Supplementary tick data |

### Libraries
| Package | Purpose |
|---|---|
| `py_vollib` | Black-Scholes IV and Greeks from real bid/ask |
| `alpaca-py` | Alpaca Markets SDK |
| `pandas` / `numpy` | Data processing |
| `scipy` | Bootstrap statistics, confidence intervals |
| `Flask` | Web dashboard |
| `Chart.js` | Frontend charting |
| `schedule` | Polling loop management |
| `pytz` | US Eastern time handling |

### Brokers & APIs
| Broker | Auth | Use |
|---|---|---|
| Tastytrade | OAuth2 refresh token | Live paper/real trading |
| Tradier | API key | Real-time option chain quotes |
| Alpaca Markets | API key + secret | 5-min SPY bars |

---

## Backtesting Methodology

Every strategy was subject to the same adversarial pipeline before being considered for deployment.

### Pipeline

```
1. Hypothesis definition
   +-- Pre-register kill criteria BEFORE seeing any OOS data

2. In-sample fitting (2021 - mid-2023)
   +-- Parameter search, filter design, entry/exit logic

3. Out-of-sample test (mid-2023 - end-2024)
   +-- No further parameter changes after this step

4. Blind holdout (all of 2025)
   +-- Opened only after OOS verdict is recorded

5. Real bid/ask validation (ThetaData EOD)
   +-- Re-run every IS/OOS/blind split with real fills
   +-- Synthetic results discarded if real delta > 30%

6. Bootstrap statistical validation
   +-- 10,000 sessions with daily-block resampling
   +-- Report P(positive total return) and 95% CI

7. Adversarial case construction
   +-- Write the strongest possible argument AGAINST deployment
   +-- Deployment approved only if adversarial case cannot identify
       a structural flaw in the real-data OOS result
```

### Standards

- **Look-ahead bias:** All indicators computed using only prior-bar data. Verified by code review.
- **Survivorship bias:** Universe fixed at the start of the study; no adding tickers that performed well.
- **Real pricing:** ThetaData EOD bid/ask used for all final P&L calculations. Synthetic Black-Scholes used only for initial exploration.
- **Minimum sample size:** 30 OOS trades required for Grade B or above.
- **Pre-registered kill criteria:** Written and locked in the report header before any OOS data is opened.

---

## Project Structure

```
spy-0dte-trader/
+-- main.py                    # Master orchestrator
+-- dashboard.py               # Flask web UI
+-- morning_briefing.py        # Pre-market signal summary
+-- paper_trader.py            # Standalone paper trading bot
|
+-- core/
|   +-- database.py            # SQLite WAL
|   +-- data_feeds.py          # Unified data layer
|   +-- performance.py         # Self-learning performance tracker
|   +-- reporter.py            # End-of-day P&L reporting
|
+-- strategies/
|   +-- config_d.py            # 0DTE momentum — Mon/Fri, VIX<20, calls only
|   +-- credit_spread.py       # 0DTE put credit spread — Grade A
|   +-- five_dte.py            # 5-DTE ATM momentum — Grade C
|   +-- earnings.py            # Pre-earnings momentum — Grade A
|   +-- vpin.py                # Volume-price imbalance — experimental
|
+-- backtest_real_data.py      # Full 5-year real bid/ask backtest engine
+-- find_edge.py               # Edge scanner
+-- edge_scan_historical.py    # Historical edge detection
+-- monte_carlo.py             # Monte Carlo simulation and risk analysis
|
+-- data/
|   +-- earnings_calendar.json
|
+-- Dockerfile                 # python:3.12-slim, 30s healthcheck
```

---

## Deployment

### Oracle Cloud Setup

```bash
git clone https://github.com/yourusername/spy-0dte-trader
cd spy-0dte-trader
cp .env.example .env

docker build -t spy-trader .
docker run -d \
  --name spy-trader \
  --restart unless-stopped \
  --env-file .env \
  -p 5000:5000 \
  spy-trader
```

### Environment Variables

```
TRADIER_API_KEY=...
TASTYTRADE_CLIENT_SECRET=...
TASTYTRADE_REFRESH_TOKEN=...
TASTYTRADE_ACCOUNT_ID=...
TASTYTRADE_USE_PRODUCTION=true
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
THETADATA_USERNAME=...
THETADATA_PASSWORD=...
```

### Risk Controls

| Control | Value | Layer |
|---|---|---|
| Max loss per strategy per day | $500 | Orchestrator |
| Max total daily loss | $2,000 | Orchestrator |
| Per-trade max premium | $500/contract | Strategy |
| Max contracts per trade | 2 | Strategy |
| Cooldown between trades | 10 min | Strategy |
| Hard time stop | 15:15 ET | All strategies |
| Kill switch | file-based | Orchestrator |

---

## Running Backtests

```bash
python backtest_real_data.py      # 5-year real bid/ask backtest
python backtest_adversarial.py    # adversarial multi-strategy report
python find_edge.py               # hypothesis scanner
python monte_carlo.py             # risk simulation
```

---

## Research Philosophy

Three rules shaped every decision in this project:

**1. Assume the strategy is dead until proven otherwise.**
Every strategy starts with pre-registered kill criteria written before any out-of-sample data is opened. The default verdict is DEAD.

**2. Real bid/ask or it did not happen.**
Every strategy that showed a synthetic edge was re-run on ThetaData real EOD bid/ask quotes. Strategies where the delta exceeded 30% were rejected regardless of in-sample performance. This is how the 5-DTE strategy went from Grade B (synthetic) to Grade C (real), and how FOMC/CPI was killed entirely.

**3. Write the adversarial case first.**
Before declaring any strategy viable, the strongest possible argument against deploying it is written explicitly. If the adversarial case identifies a structural flaw — survivorship bias, regime dependency, small sample — the strategy is not deployed until that concern is addressed.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Built with real market data, adversarial backtesting, and a healthy respect for what bid/ask spreads actually cost.*
