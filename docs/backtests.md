# Backtest Scripts

## credit_hypothesis_backtest.py
Tests 5 hypotheses against 5 years of ThetaData (2021-2026) using R3D as base strategy:

| Hypothesis | Finding |
|---|---|
| H_VIX18 — VIX 18 floor | Marginal. Blind-2025 WR drops 82.4% → 64.5%. Keep as risk control only. |
| H_CREDIT — $0.30 min credit | Better blind WR (84.4% vs 82.4%). Implement after 200+ clean trades. |
| H_FOMC — Trade through FOMC | +2.0% WR, +$372 P&L. FOMC skip removed from R3D. |
| H_GAP — Skip gap-up >0.7% | No clear edge. Not implemented. |
| H_R3E — Iron condor VIX bands | Only VIX 16-18 has enough volume (N=61). |

## hermes_strike_formula_backtest.py
Tests Black-Scholes VIX-scaled delta formula vs current spot-anchored approximation.

**Finding:** Formula B (VIX-scaled) is significantly worse — selects strikes 2x further OTM, resulting in $0 bids on 92% of days. Keep Formula A (current).

## experimental_strategies_e1_e10_backtest.py
Tests 10 new low-VIX strategies. Only E6 and E7 survive realistic pricing:

| Strategy | OOS WR | Blind WR | Status |
|---|---|---|---|
| E6 — Daily 2PM decay ($3 wide) | 87.9% | 92.6% | ✅ Added to engine |
| E7 — Daily iron condor ($3 wide) | 94.1% | 99.1% | ✅ Added to engine |
| E1-E5, E8-E10 | Various | — | ❌ Never clear $0.20 live |
