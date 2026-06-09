# Hermes Research Journal
*Autonomous overnight strategy research — started 2026-06-05 02:51:02*

**Baseline to beat:**
- Credit Spread: OOS WR 80%, P&L +$3,639
- Earnings Momentum: P&L +$53,106

**Kill criteria:**
- OOS Sharpe < 1.0
- OOS WR < breakeven for strategy type  
- >60% profits from single year
- fewer than 20 OOS trades

---

## H1 — Pre-FOMC Drift
**Hypothesis:** Buy 0DTE ATM calls on FOMC morning, exit by 1:45 PM (Lucca-Moench drift)
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=9 < 20 (insufficient trades)

### Full Period Stats
- N=26  WR=11.5%  P&L=$-1,541.20  PF=0.14  Sharpe=-16.81

### In-Sample (2021–Jun 2023)
- N=11  WR=0.0%  P&L=$-791.72  PF=N/A

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=9  WR=22.2%  P&L=$-364.24  PF=0.32  Sharpe=-8.65  Boot=4%

### OOS Year Breakdown
- 2023: N=2  WR=0.0%  P&L=$-120.40  PF=N/A
- 2024: N=5  WR=20.0%  P&L=$-221.87  PF=0.29
- 2026: N=2  WR=50.0%  P&L=$-21.97  PF=0.79

### Exit Reason Breakdown (OOS)
- `30% stop`: N=7  WR=0%  P&L=$-538.32
- `35% target`: N=2  WR=100%  P&L=$+174.08

---

## H2 — VIX Reversal Iron Butterfly
**Hypothesis:** 0DTE iron butterfly on day VIX turns down from spike (IVR>70)
**Type:** credit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=0 < 20 (insufficient trades)

### Full Period Stats
- N=15  WR=53.3%  P&L=$-387.46  PF=0.42  Sharpe=-5.57

### In-Sample (2021–Jun 2023)
- N=15  WR=53.3%  P&L=$-387.46  PF=0.42

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A  Sharpe=0.00  Boot=0%

### OOS Year Breakdown

### Exit Reason Breakdown (OOS)

---

## H3 — Monday Gap-Fill
**Hypothesis:** Buy 0DTE calls on Monday gap-down, target gap fill by 11:30 AM
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=2 < 20 (insufficient trades)

### Full Period Stats
- N=5  WR=80.0%  P&L=$+62.34  PF=1.61  Sharpe=2.62

### In-Sample (2021–Jun 2023)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=2  WR=100.0%  P&L=$+9.75  PF=inf  Sharpe=22.75  Boot=100%

### OOS Year Breakdown
- 2026: N=2  WR=100.0%  P&L=$+9.75  PF=inf

### Exit Reason Breakdown (OOS)
- `gap filled`: N=2  WR=100%  P&L=$+9.75

---

## H4 — Overnight Gap Momentum
**Hypothesis:** Buy 0DTE calls when SPY gaps up 0.5–2.5% at open, exit by 12:30
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS Sharpe=-5.71 < 1.0

### Full Period Stats
- N=131  WR=40.5%  P&L=$-3,258.96  PF=0.53  Sharpe=-4.62

### In-Sample (2021–Jun 2023)
- N=48  WR=50.0%  P&L=$-379.52  PF=0.80

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=55  WR=38.2%  P&L=$-1,672.52  PF=0.46  Sharpe=-5.71  Boot=0%

### OOS Year Breakdown
- 2023: N=16  WR=43.8%  P&L=$-294.15  PF=0.59
- 2024: N=29  WR=34.5%  P&L=$-1,097.45  PF=0.37
- 2026: N=10  WR=40.0%  P&L=$-280.92  PF=0.57

### Exit Reason Breakdown (OOS)
- `35% stop`: N=27  WR=0%  P&L=$-2731.48
- `40% target`: N=15  WR=100%  P&L=$+1217.15
- `12:30 cutoff`: N=13  WR=46%  P&L=$-158.19

---

## H5 — CPI Hot-Print Put Spread
**Hypothesis:** Debit put spread 2 days before known-hot CPI prints
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=2 < 20 (insufficient trades)

### Full Period Stats
- N=25  WR=36.0%  P&L=$-1,105.94  PF=0.46  Sharpe=-5.94

### In-Sample (2021–Jun 2023)
- N=21  WR=38.1%  P&L=$-799.13  PF=0.52

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=2  WR=0.0%  P&L=$-185.41  PF=N/A  Sharpe=-24.10  Boot=0%

### OOS Year Breakdown
- 2024: N=2  WR=0.0%  P&L=$-185.41  PF=N/A

### Exit Reason Breakdown (OOS)
- `2024-03 hot CPI`: N=1  WR=0%  P&L=$-135.89
- `2024-04 hot CPI`: N=1  WR=0%  P&L=$-49.52

---

## H6 — Friday PM Premium Decay
**Hypothesis:** Sell 0DTE put spread on Friday 1 PM, capture accelerated theta
**Type:** credit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=0 < 20 (insufficient trades)

### Full Period Stats
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A  Sharpe=0.00

### In-Sample (2021–Jun 2023)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A  Sharpe=0.00  Boot=0%

### OOS Year Breakdown

### Exit Reason Breakdown (OOS)

---

## H7 — Post-Earnings IV Collapse
**Hypothesis:** Sell put spreads on NVDA/TSLA etc day after earnings (IV crush)
**Type:** credit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS Sharpe=-2.86 < 1.0

### Full Period Stats
- N=174  WR=60.3%  P&L=$-5,518.26  PF=0.72  Sharpe=-1.90

### In-Sample (2021–Jun 2023)
- N=79  WR=65.8%  P&L=$+295.28  PF=1.05

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=63  WR=57.1%  P&L=$-3,290.60  PF=0.63  Sharpe=-2.86  Boot=9%

### OOS Year Breakdown
- 2023: N=16  WR=62.5%  P&L=$+369.11  PF=1.52
- 2024: N=31  WR=54.8%  P&L=$-2,030.55  PF=0.54
- 2026: N=16  WR=56.2%  P&L=$-1,629.16  PF=0.56

### Exit Reason Breakdown (OOS)
- `TSLA post-earnings`: N=8  WR=88%  P&L=$+972.27
- `AMD post-earnings`: N=8  WR=75%  P&L=$+298.80
- `AAPL post-earnings`: N=8  WR=75%  P&L=$+155.27
- `MSFT post-earnings`: N=8  WR=50%  P&L=$-1251.09
- `META post-earnings`: N=8  WR=38%  P&L=$-1713.31
- `GOOGL post-earnings`: N=8  WR=38%  P&L=$-801.05
- `AMZN post-earnings`: N=8  WR=50%  P&L=$-186.19
- `NVDA post-earnings`: N=7  WR=43%  P&L=$-765.30

---

## H8 — MA50 Bounce
**Hypothesis:** Buy 0DTE calls when SPY bounces off MA50 on elevated VIX
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=0 < 20 (insufficient trades)

### Full Period Stats
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A  Sharpe=0.00

### In-Sample (2021–Jun 2023)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A  Sharpe=0.00  Boot=0%

### OOS Year Breakdown

### Exit Reason Breakdown (OOS)

---

## H9 — Wednesday Credit Spread
**Hypothesis:** Sell 0DTE put spread Wednesday 10:30 AM, VIX 13–20
**Type:** credit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=0 < 20 (insufficient trades)

### Full Period Stats
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A  Sharpe=0.00

### In-Sample (2021–Jun 2023)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=0  WR=0.0%  P&L=$+0.00  PF=N/A  Sharpe=0.00  Boot=0%

### OOS Year Breakdown

### Exit Reason Breakdown (OOS)

---

## H10 — VWAP-Cross Momentum
**Hypothesis:** Buy 0DTE calls on bullish VWAP cross with 2x volume surge
**Type:** debit | **Elapsed:** 0.5s
**Status:** ❌ DEAD — OOS Sharpe=-7.44 < 1.0

### Full Period Stats
- N=76  WR=21.1%  P&L=$-2,056.47  PF=0.19  Sharpe=-11.20

### In-Sample (2021–Jun 2023)
- N=28  WR=21.4%  P&L=$-747.32  PF=0.15

### Out-of-Sample (Jul 2023–2026, excl 2025 blind)
- N=31  WR=25.8%  P&L=$-554.72  PF=0.32  Sharpe=-7.44  Boot=0%

### OOS Year Breakdown
- 2023: N=5  WR=40.0%  P&L=$-80.91  PF=0.36
- 2024: N=23  WR=26.1%  P&L=$-354.82  PF=0.38
- 2026: N=3  WR=0.0%  P&L=$-118.99  PF=N/A

### Exit Reason Breakdown (OOS)
- `1-hour time stop`: N=23  WR=17%  P&L=$-545.52
- `28% stop`: N=4  WR=0%  P&L=$-220.70
- `30% target`: N=3  WR=100%  P&L=$+201.58
- `EOD`: N=1  WR=100%  P&L=$+9.92

---


---
## FINAL SUMMARY

*Completed: 2026-06-05 02:51:15*

Tested 10 hypotheses | 0 survivors | 10 eliminated


### Survivors (ranked by OOS P&L)


### Eliminated

- ❌ H1 Pre-FOMC Drift: OOS N=9 < 20 (insufficient trades)
- ❌ H2 VIX Reversal Iron Butterfly: OOS N=0 < 20 (insufficient trades)
- ❌ H3 Monday Gap-Fill: OOS N=2 < 20 (insufficient trades)
- ❌ H4 Overnight Gap Momentum: OOS Sharpe=-5.71 < 1.0
- ❌ H5 CPI Hot-Print Put Spread: OOS N=2 < 20 (insufficient trades)
- ❌ H6 Friday PM Premium Decay: OOS N=0 < 20 (insufficient trades)
- ❌ H7 Post-Earnings IV Collapse: OOS Sharpe=-2.86 < 1.0
- ❌ H8 MA50 Bounce: OOS N=0 < 20 (insufficient trades)
- ❌ H9 Wednesday Credit Spread: OOS N=0 < 20 (insufficient trades)
- ❌ H10 VWAP-Cross Momentum: OOS Sharpe=-7.44 < 1.0
