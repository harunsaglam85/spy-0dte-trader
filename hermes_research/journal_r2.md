# Hermes Research Journal — Round 2
*Started: 2026-06-05 02:56:53.007176*

Lessons from Round 1: gap momentum fails, gap fill works (80% WR, too few), credit strike formula broken (fixed), debit strategies expensive with BS pricing.

---

## R1 — Monday Gap-Fill (Relaxed)
**Hypothesis:** Buy 0DTE calls on Mon gap-down > 0.15%, exit 12:30 PM. Signal: 80% WR in R1 (too few trades).
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=4 < 20

### Full  N=6 WR=83.3% P&L=$+369.16 PF=3.79 Sharpe=6.17
### IS    N=0  WR=0.0%  P&L=$+0.00  PF=N/A
### OOS   N=4 WR=75.0% P&L=$+256.06 PF=2.93 Sharpe=4.98 Boot=72%

**OOS Year Breakdown:**
- 2026: N=4 WR=75.0% P&L=$+256.06 PF=2.93

**Exit reasons (OOS):**
- `gap filled`: N=3 WR=100% P&L=$+388.48
- `12:30 stop`: N=1 WR=0% P&L=$-132.42

---

## R2 — Credit Spread (Fixed Delta)
**Hypothesis:** Mon/Wed/Fri 10:30 AM put spread, FIXED strike formula with proper delta search.
**Type:** credit | **Elapsed:** 0.3s
**Status:** ✅ ALIVE

### Full  N=524 WR=84.5% P&L=$+5,675.09 PF=4.73 Sharpe=13.05
### IS    N=192  WR=88.0%  P&L=$+2,316.29  PF=6.45
### OOS   N=205 WR=82.4% P&L=$+2,021.82 PF=4.16 Sharpe=11.66 Boot=100%

**OOS Year Breakdown:**
- 2023: N=63 WR=77.8% P&L=$+445.23 PF=2.77
- 2024: N=95 WR=81.1% P&L=$+873.56 PF=3.80
- 2026: N=47 WR=91.5% P&L=$+703.03 PF=10.21

**Exit reasons (OOS):**
- `75% target`: N=160 WR=100% P&L=$+2511.80
- `1.75x stop`: N=34 WR=0% P&L=$-631.23
- `3PM force`: N=11 WR=82% P&L=$+141.25

---

## R3 — Morning Gap Fade
**Hypothesis:** Fade gap-ups > 0.5% by buying puts. Learned from R1 H4: gaps fail to continue.
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS Sharpe=-5.42 < 1.0

### Full  N=163 WR=25.8% P&L=$-6,878.33 PF=0.32 Sharpe=-7.92
### IS    N=72  WR=22.2%  P&L=$-3,443.17  PF=0.23
### OOS   N=59 WR=27.1% P&L=$-1,888.21 PF=0.44 Sharpe=-5.42 Boot=1%

**OOS Year Breakdown:**
- 2023: N=16 WR=31.2% P&L=$-438.68 PF=0.28
- 2024: N=30 WR=26.7% P&L=$-832.64 PF=0.47
- 2026: N=13 WR=23.1% P&L=$-616.89 PF=0.48

**Exit reasons (OOS):**
- `11:30 cutoff`: N=39 WR=38% P&L=$-269.84
- `45% stop`: N=19 WR=0% P&L=$-1927.54
- `gap filled`: N=1 WR=100% P&L=$+309.17

---

## R4 — Post-FOMC IV Collapse
**Hypothesis:** Sell 0DTE put spread day after FOMC while IV premium still elevated.
**Type:** credit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=10 < 20

### Full  N=38 WR=73.7% P&L=$+262.64 PF=2.01 Sharpe=5.37
### IS    N=20  WR=80.0%  P&L=$+200.17  PF=2.86
### OOS   N=10 WR=70.0% P&L=$+44.57 PF=1.56 Sharpe=3.22 Boot=74%

**OOS Year Breakdown:**
- 2023: N=3 WR=66.7% P&L=$+4.71 PF=1.18
- 2024: N=4 WR=75.0% P&L=$+35.37 PF=2.21
- 2026: N=3 WR=66.7% P&L=$+4.49 PF=1.18

**Exit reasons (OOS):**
- `post-FOMC 2023-07-26`: N=1 WR=0% P&L=$-26.16
- `post-FOMC 2023-09-20`: N=1 WR=100% P&L=$+17.07
- `post-FOMC 2023-11-01`: N=1 WR=100% P&L=$+13.80
- `post-FOMC 2024-01-31`: N=1 WR=100% P&L=$+24.37
- `post-FOMC 2024-05-01`: N=1 WR=100% P&L=$+22.88
- `post-FOMC 2024-07-31`: N=1 WR=0% P&L=$-29.31
- `post-FOMC 2024-09-18`: N=1 WR=100% P&L=$+17.43
- `post-FOMC 2026-01-28`: N=1 WR=0% P&L=$-24.36
- `post-FOMC 2026-03-18`: N=1 WR=100% P&L=$+21.17
- `post-FOMC 2026-05-06`: N=1 WR=100% P&L=$+7.68

---

## R5 — 3+ Red Days Reversal
**Hypothesis:** Buy 0DTE calls after 3 consecutive red days + gap-up open.
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=15 < 20

### Full  N=41 WR=34.1% P&L=$-1,698.15 PF=0.52 Sharpe=-4.70
### IS    N=20  WR=35.0%  P&L=$-676.08  PF=0.49
### OOS   N=15 WR=40.0% P&L=$-545.03 PF=0.63 Sharpe=-3.44 Boot=19%

**OOS Year Breakdown:**
- 2023: N=1 WR=0.0% P&L=$-108.25 PF=N/A
- 2024: N=7 WR=57.1% P&L=$+97.52 PF=1.27
- 2026: N=7 WR=28.6% P&L=$-534.30 PF=0.46

**Exit reasons (OOS):**
- `3+ red days, drop=2.7%`: N=2 WR=50% P&L=$+125.84
- `3+ red days, drop=1.8%`: N=2 WR=50% P&L=$-90.83
- `3+ red days, drop=2.1%`: N=1 WR=0% P&L=$-105.80
- `3+ red days, drop=2.8%`: N=1 WR=100% P&L=$+139.84
- `3+ red days, drop=2.2%`: N=1 WR=0% P&L=$-100.22
- `3+ red days, drop=1.9%`: N=1 WR=100% P&L=$+123.61
- `3+ red days, drop=2.9%`: N=1 WR=100% P&L=$+130.92
- `3+ red days, drop=2.4%`: N=1 WR=0% P&L=$-163.62
- `3+ red days, drop=2.0%`: N=1 WR=100% P&L=$+222.77
- `3+ red days, drop=1.7%`: N=1 WR=0% P&L=$-212.46
- `3+ red days, drop=2.3%`: N=1 WR=0% P&L=$-185.06
- `3+ red days, drop=3.7%`: N=1 WR=0% P&L=$-226.59
- `3+ red days, drop=3.1%`: N=1 WR=0% P&L=$-203.43

---

## R6 — ATR Breakout Momentum
**Hypothesis:** Buy calls when SPY gaps above 5-day range by 0.5–2x ATR.
**Type:** debit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=8 < 20

### Full  N=14 WR=21.4% P&L=$-713.30 PF=0.21 Sharpe=-11.48
### IS    N=5  WR=20.0%  P&L=$-268.84  PF=0.17
### OOS   N=8 WR=25.0% P&L=$-333.02 PF=0.28 Sharpe=-8.97 Boot=5%

**OOS Year Breakdown:**
- 2023: N=4 WR=25.0% P&L=$-161.51 PF=0.17
- 2024: N=3 WR=33.3% P&L=$-25.56 PF=0.79
- 2026: N=1 WR=0.0% P&L=$-145.95 PF=N/A

**Exit reasons (OOS):**
- `gap=2.4 atr=3.6`: N=1 WR=0% P&L=$-63.24
- `gap=2.0 atr=3.5`: N=1 WR=0% P&L=$-66.15
- `gap=3.0 atr=5.8`: N=1 WR=100% P&L=$+32.99
- `gap=4.8 atr=5.2`: N=1 WR=0% P&L=$-65.11
- `gap=2.5 atr=4.2`: N=1 WR=100% P&L=$+96.41
- `gap=2.7 atr=4.9`: N=1 WR=0% P&L=$-54.32
- `gap=4.5 atr=5.2`: N=1 WR=0% P&L=$-67.65
- `gap=16.7 atr=11.2`: N=1 WR=0% P&L=$-145.95

---

## R7 — Opening Range Breakout
**Hypothesis:** Buy 0DTE directional option on 30-min OR breakout with volume surge.
**Type:** debit | **Elapsed:** 0.3s
**Status:** ❌ DEAD — OOS Sharpe=-5.55 < 1.0

### Full  N=399 WR=26.3% P&L=$-13,362.97 PF=0.39 Sharpe=-6.75
### IS    N=173  WR=20.8%  P&L=$-7,089.92  PF=0.26
### OOS   N=164 WR=30.5% P&L=$-4,544.66 PF=0.46 Sharpe=-5.55 Boot=0%

**OOS Year Breakdown:**
- 2023: N=52 WR=32.7% P&L=$-1,079.40 PF=0.48
- 2024: N=88 WR=29.5% P&L=$-2,567.22 PF=0.42
- 2026: N=24 WR=29.2% P&L=$-898.04 PF=0.52

**Exit reasons (OOS):**
- `bull OR_rng=1.17`: N=4 WR=0% P&L=$-303.78
- `bull OR_rng=1.50`: N=3 WR=33% P&L=$-65.45
- `bear OR_rng=1.00`: N=3 WR=67% P&L=$+98.68
- `bear OR_rng=0.71`: N=2 WR=50% P&L=$+83.65
- `bear OR_rng=0.92`: N=2 WR=50% P&L=$+2.43
- `bear OR_rng=0.84`: N=2 WR=50% P&L=$-47.20
- `bear OR_rng=1.35`: N=2 WR=0% P&L=$-151.60
- `bear OR_rng=0.94`: N=2 WR=0% P&L=$-118.52
- `bull OR_rng=1.12`: N=2 WR=0% P&L=$-131.52
- `bear OR_rng=2.47`: N=2 WR=50% P&L=$+77.29
- `bull OR_rng=1.27`: N=2 WR=0% P&L=$-202.55
- `bear OR_rng=1.25`: N=2 WR=50% P&L=$+42.90
- `bear OR_rng=1.42`: N=2 WR=50% P&L=$-8.73
- `bear OR_rng=1.62`: N=2 WR=100% P&L=$+184.86
- `bull OR_rng=1.00`: N=2 WR=50% P&L=$+12.41
- `bear OR_rng=1.17`: N=2 WR=0% P&L=$-103.85
- `bear OR_rng=1.31`: N=2 WR=0% P&L=$-98.10
- `bull OR_rng=0.69`: N=2 WR=0% P&L=$-146.55
- `bear OR_rng=1.28`: N=2 WR=50% P&L=$+7.80
- `bull OR_rng=0.94`: N=2 WR=50% P&L=$-23.22
- `bear OR_rng=1.50`: N=2 WR=50% P&L=$-18.71
- `bear OR_rng=0.77`: N=2 WR=0% P&L=$-116.93
- `bear OR_rng=0.98`: N=2 WR=50% P&L=$-88.54
- `bear OR_rng=1.71`: N=2 WR=0% P&L=$-139.89
- `bull OR_rng=1.15`: N=2 WR=0% P&L=$-169.75
- `bull OR_rng=1.06`: N=2 WR=50% P&L=$-114.64
- `bear OR_rng=1.45`: N=2 WR=0% P&L=$-127.99
- `bull OR_rng=0.88`: N=1 WR=0% P&L=$-24.08
- `bear OR_rng=1.19`: N=1 WR=0% P&L=$-54.52
- `bull OR_rng=0.66`: N=1 WR=0% P&L=$-18.00
- `bull OR_rng=1.42`: N=1 WR=100% P&L=$+29.00
- `bear OR_rng=0.67`: N=1 WR=0% P&L=$-52.35
- `bull OR_rng=1.66`: N=1 WR=0% P&L=$-49.84
- `bear OR_rng=1.11`: N=1 WR=100% P&L=$+89.44
- `bull OR_rng=1.13`: N=1 WR=0% P&L=$-69.33
- `bear OR_rng=1.61`: N=1 WR=0% P&L=$-76.05
- `bear OR_rng=1.40`: N=1 WR=0% P&L=$-68.58
- `bull OR_rng=1.70`: N=1 WR=100% P&L=$+76.57
- `bear OR_rng=1.06`: N=1 WR=100% P&L=$+66.03
- `bear OR_rng=1.27`: N=1 WR=100% P&L=$+6.49
- `bear OR_rng=1.96`: N=1 WR=0% P&L=$-74.61
- `bear OR_rng=1.34`: N=1 WR=0% P&L=$-94.88
- `bull OR_rng=1.97`: N=1 WR=0% P&L=$-68.44
- `bull OR_rng=1.43`: N=1 WR=100% P&L=$+5.26
- `bull OR_rng=1.20`: N=1 WR=0% P&L=$-60.50
- `bear OR_rng=0.68`: N=1 WR=100% P&L=$+87.82
- `bull OR_rng=1.23`: N=1 WR=100% P&L=$+62.06
- `bull OR_rng=1.34`: N=1 WR=0% P&L=$-69.85
- `bull OR_rng=1.57`: N=1 WR=100% P&L=$+11.14
- `bear OR_rng=0.61`: N=1 WR=0% P&L=$-55.45
- `bear OR_rng=0.57`: N=1 WR=0% P&L=$-54.48
- `bull OR_rng=1.41`: N=1 WR=0% P&L=$-57.39
- `bear OR_rng=0.83`: N=1 WR=0% P&L=$-61.36
- `bull OR_rng=1.38`: N=1 WR=100% P&L=$+23.54
- `bear OR_rng=2.33`: N=1 WR=0% P&L=$-63.07
- `bull OR_rng=0.86`: N=1 WR=0% P&L=$-67.68
- `bear OR_rng=0.81`: N=1 WR=0% P&L=$-56.61
- `bull OR_rng=1.05`: N=1 WR=0% P&L=$-71.48
- `bull OR_rng=1.03`: N=1 WR=0% P&L=$-2.45
- `bear OR_rng=1.04`: N=1 WR=0% P&L=$-70.32
- `bull OR_rng=0.81`: N=1 WR=0% P&L=$-75.11
- `bull OR_rng=0.92`: N=1 WR=100% P&L=$+46.93
- `bull OR_rng=1.35`: N=1 WR=0% P&L=$-16.88
- `bear OR_rng=2.29`: N=1 WR=100% P&L=$+90.34
- `bear OR_rng=1.65`: N=1 WR=0% P&L=$-71.70
- `bull OR_rng=1.56`: N=1 WR=0% P&L=$-70.36
- `bull OR_rng=0.99`: N=1 WR=100% P&L=$+66.43
- `bull OR_rng=0.97`: N=1 WR=0% P&L=$-13.76
- `bull OR_rng=2.30`: N=1 WR=100% P&L=$+87.04
- `bear OR_rng=1.88`: N=1 WR=0% P&L=$-83.59
- `bull OR_rng=1.74`: N=1 WR=0% P&L=$-81.78
- `bear OR_rng=1.16`: N=1 WR=100% P&L=$+135.12
- `bull OR_rng=1.46`: N=1 WR=100% P&L=$+68.92
- `bull OR_rng=0.74`: N=1 WR=0% P&L=$-77.82
- `bull OR_rng=1.51`: N=1 WR=100% P&L=$+41.95
- `bull OR_rng=0.76`: N=1 WR=0% P&L=$-64.83
- `bull OR_rng=0.53`: N=1 WR=0% P&L=$-60.22
- `bull OR_rng=0.51`: N=1 WR=0% P&L=$-69.30
- `bear OR_rng=1.67`: N=1 WR=100% P&L=$+64.75
- `bear OR_rng=1.75`: N=1 WR=100% P&L=$+65.63
- `bull OR_rng=0.98`: N=1 WR=0% P&L=$-81.34
- `bull OR_rng=1.28`: N=1 WR=100% P&L=$+63.56
- `bull OR_rng=1.72`: N=1 WR=100% P&L=$+77.44
- `bull OR_rng=0.63`: N=1 WR=0% P&L=$-17.47
- `bull OR_rng=1.96`: N=1 WR=0% P&L=$-60.67
- `bull OR_rng=0.83`: N=1 WR=100% P&L=$+81.54
- `bull OR_rng=1.98`: N=1 WR=0% P&L=$-77.45
- `bull OR_rng=1.14`: N=1 WR=0% P&L=$-24.20
- `bear OR_rng=0.88`: N=1 WR=0% P&L=$-36.03
- `bull OR_rng=1.87`: N=1 WR=0% P&L=$-76.41
- `bear OR_rng=2.08`: N=1 WR=0% P&L=$-70.27
- `bear OR_rng=2.81`: N=1 WR=0% P&L=$-17.07
- `bear OR_rng=1.86`: N=1 WR=0% P&L=$-107.60
- `bear OR_rng=1.56`: N=1 WR=100% P&L=$+98.73
- `bear OR_rng=2.99`: N=1 WR=0% P&L=$-118.01
- `bull OR_rng=3.42`: N=1 WR=0% P&L=$-150.88
- `bull OR_rng=1.25`: N=1 WR=100% P&L=$+23.75
- `bull OR_rng=0.90`: N=1 WR=0% P&L=$-158.66
- `bear OR_rng=1.01`: N=1 WR=100% P&L=$+93.40
- `bear OR_rng=2.53`: N=1 WR=0% P&L=$-9.23
- `bear OR_rng=3.70`: N=1 WR=100% P&L=$+120.56
- `bear OR_rng=4.10`: N=1 WR=0% P&L=$-116.63
- `bear OR_rng=1.26`: N=1 WR=0% P&L=$-97.91
- `bull OR_rng=1.29`: N=1 WR=0% P&L=$-124.66
- `bull OR_rng=1.55`: N=1 WR=0% P&L=$-127.43
- `bull OR_rng=1.53`: N=1 WR=0% P&L=$-123.38
- `bear OR_rng=0.72`: N=1 WR=0% P&L=$-27.25
- `bull OR_rng=0.87`: N=1 WR=0% P&L=$-64.26
- `bull OR_rng=0.68`: N=1 WR=0% P&L=$-118.00
- `bull OR_rng=0.96`: N=1 WR=0% P&L=$-41.31
- `bull OR_rng=1.62`: N=1 WR=0% P&L=$-3.34
- `bull OR_rng=1.78`: N=1 WR=0% P&L=$-116.79
- `bear OR_rng=1.78`: N=1 WR=0% P&L=$-102.51
- `bear OR_rng=3.02`: N=1 WR=100% P&L=$+12.48
- `bull OR_rng=1.60`: N=1 WR=0% P&L=$-103.19
- `bull OR_rng=4.39`: N=1 WR=0% P&L=$-58.72
- `bear OR_rng=3.50`: N=1 WR=100% P&L=$+150.78
- `bull OR_rng=3.23`: N=1 WR=0% P&L=$-45.55
- `bear OR_rng=3.00`: N=1 WR=100% P&L=$+166.77
- `bull OR_rng=3.48`: N=1 WR=0% P&L=$-132.25
- `bull OR_rng=3.49`: N=1 WR=0% P&L=$-172.48
- `bull OR_rng=5.41`: N=1 WR=0% P&L=$-44.91
- `bear OR_rng=2.69`: N=1 WR=0% P&L=$-132.37
- `bull OR_rng=2.47`: N=1 WR=0% P&L=$-143.48
- `bull OR_rng=3.84`: N=1 WR=0% P&L=$-142.59
- `bear OR_rng=3.08`: N=1 WR=100% P&L=$+173.91
- `bull OR_rng=4.73`: N=1 WR=0% P&L=$-158.01
- `bull OR_rng=1.31`: N=1 WR=100% P&L=$+183.25
- `bear OR_rng=1.60`: N=1 WR=0% P&L=$-130.69
- `bear OR_rng=2.09`: N=1 WR=100% P&L=$+123.74
- `bull OR_rng=2.10`: N=1 WR=0% P&L=$-114.73
- `bull OR_rng=2.86`: N=1 WR=100% P&L=$+151.92
- `bull OR_rng=1.11`: N=1 WR=0% P&L=$-49.05

---

## R8 — Friday Credit + VWAP Filter
**Hypothesis:** Friday 1 PM put spread with VWAP bullish confirmation (fixed from H6).
**Type:** credit | **Elapsed:** 0.1s
**Status:** ✅ ALIVE

### Full  N=128 WR=87.5% P&L=$+1,459.80 PF=5.77 Sharpe=13.95
### IS    N=58  WR=94.8%  P&L=$+812.52  PF=13.13
### OOS   N=46 WR=80.4% P&L=$+393.75 PF=3.34 Sharpe=8.48 Boot=100%

**OOS Year Breakdown:**
- 2023: N=14 WR=64.3% P&L=$+30.26 PF=1.31
- 2024: N=22 WR=86.4% P&L=$+199.43 PF=3.97
- 2026: N=10 WR=90.0% P&L=$+164.06 PF=48.01

**Exit reasons (OOS):**
- `70% target`: N=35 WR=100% P&L=$+534.21
- `1.8x stop`: N=6 WR=0% P&L=$-164.40
- `3:30 force`: N=4 WR=50% P&L=$+23.94
- `EOD`: N=1 WR=0% P&L=$+0.00

---

## R9 — High-VIX Credit Spread
**Hypothesis:** 0DTE very-OTM put spread (0.12 delta) on VIX 20-32 non-event days.
**Type:** credit | **Elapsed:** 0.0s
**Status:** ❌ DEAD — OOS N=1 < 20

### Full  N=1 WR=100.0% P&L=$+15.58 PF=inf Sharpe=0.00
### IS    N=0  WR=0.0%  P&L=$+0.00  PF=N/A
### OOS   N=1 WR=100.0% P&L=$+15.58 PF=inf Sharpe=0.00 Boot=100%

**OOS Year Breakdown:**
- 2024: N=1 WR=100.0% P&L=$+15.58 PF=inf

**Exit reasons (OOS):**
- `vix=23.4 ivr=100`: N=1 WR=100% P&L=$+15.58

---

## R10 — Tuesday Decay + VWAP
**Hypothesis:** Tuesday 0DTE put spread, 10:45 AM, above VWAP + MA50 regime filter.
**Type:** credit | **Elapsed:** 0.1s
**Status:** ✅ ALIVE

### Full  N=80 WR=87.5% P&L=$+859.79 PF=6.00 Sharpe=15.43
### IS    N=28  WR=89.3%  P&L=$+324.08  PF=7.48
### OOS   N=33 WR=84.8% P&L=$+296.02 PF=4.33 Sharpe=11.91 Boot=100%

**OOS Year Breakdown:**
- 2023: N=10 WR=100.0% P&L=$+126.07 PF=inf
- 2024: N=15 WR=73.3% P&L=$+82.60 PF=2.23
- 2026: N=8 WR=87.5% P&L=$+87.35 PF=5.02

**Exit reasons (OOS):**
- `75% target`: N=26 WR=100% P&L=$+354.07
- `1.75x stop`: N=5 WR=0% P&L=$-88.81
- `3PM force`: N=2 WR=100% P&L=$+30.76

---


---
## Round 2 Summary
*2026-06-05 02:56:55.779317*

3/10 survived


### Survivors

- **R2 Credit Spread (Fixed Delta)**: OOS N=205 WR=82.4% P&L=$+2,022 Sharpe=11.66
- **R8 Friday Credit + VWAP Filter**: OOS N=46 WR=80.4% P&L=$+394 Sharpe=8.48
- **R10 Tuesday Decay + VWAP**: OOS N=33 WR=84.8% P&L=$+296 Sharpe=11.91

### Dead

- ❌ R1 Monday Gap-Fill (Relaxed): OOS N=4 < 20
- ❌ R3 Morning Gap Fade: OOS Sharpe=-5.42 < 1.0
- ❌ R4 Post-FOMC IV Collapse: OOS N=10 < 20
- ❌ R5 3+ Red Days Reversal: OOS N=15 < 20
- ❌ R6 ATR Breakout Momentum: OOS N=8 < 20
- ❌ R7 Opening Range Breakout: OOS Sharpe=-5.55 < 1.0
- ❌ R9 High-VIX Credit Spread: OOS N=1 < 20
