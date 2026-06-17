#!/usr/bin/env python3
"""
Strike Formula Backtest: Current (0.025 hardcoded) vs VIX-Scaled
Data: ThetaData SPY options EOD 2021-2026 (real bid/ask)
Key format: (expiry_str, strike, 'P'/'C') -> {bid, ask, volume, close}
"""

import pickle, os, math, json
from datetime import datetime, date
from collections import defaultdict

DATA_DIR = '/root/spy-0dte-trader/backtest_data'
OUTPUT   = '/root/hermes_system/research/strike_formula_results.json'

DELTA_TARGET = 0.20
SPREAD_WIDTH = 2.0
MIN_CREDIT   = 0.20

Z_SCORES = {0.15: 1.036, 0.16: 1.000, 0.20: 0.842, 0.25: 0.674}

def formula_current(spy, vix, delta=DELTA_TARGET):
    return spy * (1 - delta * 0.025)

def formula_vix_scaled(spy, vix, delta=DELTA_TARGET):
    z = Z_SCORES.get(delta, 0.842)
    return spy - (spy * (vix / 100) * math.sqrt(1/252) * z)

# ── Load VIX ──────────────────────────────────────────────────────────────────
print("Loading VIX...")
import yfinance as yf
vix_raw = yf.download('^VIX', start='2021-01-01', end='2026-06-16', progress=False)
vix_by_date = {}
for idx in vix_raw.index:
    d = idx.strftime('%Y-%m-%d')
    val = vix_raw.loc[idx, ('Close', '^VIX')]
    try:
        vix_by_date[d] = float(val)
    except:
        pass
print(f"  {len(vix_by_date)} VIX days loaded")

# ── Load SPY 5-min ────────────────────────────────────────────────────────────
print("Loading SPY 5-min...")
spy_5min = pickle.load(open(f'{DATA_DIR}/spy_5min_2021-01-04_2026-05-30.pkl','rb'))
spy_daily = pickle.load(open(f'{DATA_DIR}/spy_daily_2021-01-04_2026-05-30.pkl','rb'))

def get_spy_at_1030(date_str):
    bars = spy_5min.get(date_str, [])
    # Find bar closest to 14:30 UTC = 10:30 ET
    best = None
    for bar in bars:
        dt = datetime.utcfromtimestamp(bar['t']/1000)
        if dt.hour == 14 and 25 <= dt.minute <= 35:
            best = bar['c']
            break
        if dt.hour == 14 and dt.minute >= 0:
            best = bar['c']
    if best:
        return best
    return spy_daily.get(date_str, {}).get('open', None)

# ── Load theta files ──────────────────────────────────────────────────────────
print("Scanning theta files...")
theta_files = sorted([f for f in os.listdir(DATA_DIR)
                      if f.startswith('theta_SPY_2') and 'exp' not in f
                      and len(f) == len('theta_SPY_2021-01-04.pkl')])
print(f"  {len(theta_files)} files found")

# ── Main loop ─────────────────────────────────────────────────────────────────
results = []
by_vix  = defaultdict(lambda: {'a':[], 'b':[]})
by_year = defaultdict(lambda: {'a':[], 'b':[]})
by_spy  = defaultdict(lambda: {'a':[], 'b':[]})

n_empty = 0
n_no_vix = 0
n_no_spy = 0
n_no_puts = 0

for fname in theta_files:
    date_str = fname.replace('theta_SPY_','').replace('.pkl','')

    vix = vix_by_date.get(date_str)
    if not vix:
        n_no_vix += 1
        continue

    spy = get_spy_at_1030(date_str)
    if not spy:
        n_no_spy += 1
        continue

    chain = pickle.load(open(f'{DATA_DIR}/{fname}','rb'))
    if not chain:
        n_empty += 1
        continue

    # Extract 0DTE puts: keys are (expiry, strike, type)
    # 0DTE = expiry matches the file date
    puts_by_strike = {}
    for key, data in chain.items():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        exp_str, strike, opt_type = key
        if opt_type != 'P':
            continue
        if exp_str != date_str:
            continue
        bid = data.get('bid', 0) or 0
        ask = data.get('ask', 0) or 0
        if bid <= 0:
            continue
        puts_by_strike[float(strike)] = {'bid': bid, 'ask': ask, 'strike': float(strike)}

    if not puts_by_strike:
        n_no_puts += 1
        continue

    # Filter OTM puts within 30 pts of ATM
    otm_puts = {s: p for s, p in puts_by_strike.items()
                if spy - 30 <= s < spy}

    if not otm_puts:
        n_no_puts += 1
        continue

    # Find nearest put to target strike
    def nearest(target):
        if not otm_puts:
            return None
        return min(otm_puts.values(), key=lambda p: abs(p['strike'] - target))

    def credit(short_put):
        ls = short_put['strike'] - SPREAD_WIDTH
        long_candidates = {s: p for s, p in otm_puts.items()
                           if abs(s - ls) <= 0.5 and p.get('ask', 0) > 0}
        if not long_candidates:
            return 0.0
        long_put = min(long_candidates.values(), key=lambda p: abs(p['strike'] - ls))
        c = round(short_put['bid'] - long_put['ask'], 2)
        return max(c, 0.0)

    tgt_a = formula_current(spy, vix)
    tgt_b = formula_vix_scaled(spy, vix)

    put_a = nearest(tgt_a)
    put_b = nearest(tgt_b)

    if not put_a or not put_b:
        continue

    cr_a = credit(put_a)
    cr_b = credit(put_b)

    otm_a = (spy - put_a['strike']) / spy * 100
    otm_b = (spy - put_b['strike']) / spy * 100

    year = date_str[:4]
    vbkt = '<17' if vix<17 else ('17-19' if vix<19 else ('19-21' if vix<21 else '21+'))
    sbkt = ('<400' if spy<400 else '400-500' if spy<500 else '500-600' if spy<600
            else '600-700' if spy<700 else '700-750' if spy<750 else '750+')

    results.append({
        'date': date_str, 'year': year, 'spy': round(spy,2), 'vix': round(vix,2),
        'vix_bucket': vbkt, 'spy_bucket': sbkt,
        'a': {'strike': put_a['strike'], 'target': round(tgt_a,1), 'credit': cr_a, 'pct_otm': round(otm_a,3)},
        'b': {'strike': put_b['strike'], 'target': round(tgt_b,1), 'credit': cr_b, 'pct_otm': round(otm_b,3)},
        'credit_diff': round(cr_b - cr_a, 3),
        'strike_diff': round(put_b['strike'] - put_a['strike'], 1),
    })

    by_vix[vbkt]['a'].append(cr_a); by_vix[vbkt]['b'].append(cr_b)
    by_year[year]['a'].append(cr_a); by_year[year]['b'].append(cr_b)
    by_spy[sbkt]['a'].append(cr_a);  by_spy[sbkt]['b'].append(cr_b)

print(f"\nValid results: {len(results)}")
print(f"Skipped: no VIX={n_no_vix}, no SPY={n_no_spy}, empty chain={n_empty}, no OTM puts={n_no_puts}")

if not results:
    print("No results — exiting")
    exit(1)

def stats(lst):
    if not lst: return {'n':0}
    n = len(lst)
    avg = sum(lst)/n
    std = math.sqrt(sum((x-avg)**2 for x in lst)/n)
    return {
        'n': n,
        'avg': round(avg,4),
        'std': round(std,4),
        'pct_above_020': round(sum(1 for x in lst if x>=0.20)/n*100,1),
        'pct_above_040': round(sum(1 for x in lst if x>=0.40)/n*100,1),
        'pct_zero':      round(sum(1 for x in lst if x==0.0)/n*100,1),
    }

print("\n" + "="*70)
print("OVERALL SUMMARY")
print("="*70)
all_a = [r['a']['credit'] for r in results]
all_b = [r['b']['credit'] for r in results]
sa = stats(all_a); sb = stats(all_b)
print(f"Formula A (current 0.025): avg=${sa['avg']:.4f} std={sa['std']:.4f} | ≥$0.20: {sa['pct_above_020']}% | ≥$0.40: {sa['pct_above_040']}% | $0.00: {sa['pct_zero']}%")
print(f"Formula B (VIX-scaled):    avg=${sb['avg']:.4f} std={sb['std']:.4f} | ≥$0.20: {sb['pct_above_020']}% | ≥$0.40: {sb['pct_above_040']}% | $0.00: {sb['pct_zero']}%")

all_otm_a = [r['a']['pct_otm'] for r in results]
all_otm_b = [r['b']['pct_otm'] for r in results]
sa_otm = stats(all_otm_a); sb_otm = stats(all_otm_b)
print(f"\n%OTM Consistency:")
print(f"  Formula A: avg={sa_otm['avg']:.3f}% OTM  std={sa_otm['std']:.3f}%")
print(f"  Formula B: avg={sb_otm['avg']:.3f}% OTM  std={sb_otm['std']:.3f}%")
winner = 'B (VIX-scaled)' if sb_otm['std'] < sa_otm['std'] else 'A (current)'
print(f"  → {winner} has more consistent delta targeting (lower std)")

print("\n" + "="*70)
print("BY VIX REGIME")
print("="*70)
for bkt in ['<17','17-19','19-21','21+']:
    d = by_vix[bkt]
    if not d['a']: continue
    sa = stats(d['a']); sb = stats(d['b'])
    delta_avg = sb['avg'] - sa['avg']
    indicator = '↑B' if delta_avg > 0.02 else ('↓B' if delta_avg < -0.02 else '≈')
    print(f"VIX {bkt:6s} [n={sa['n']:3d}]: A avg=${sa['avg']:.3f} ≥$0.20:{sa['pct_above_020']:5.1f}% | B avg=${sb['avg']:.3f} ≥$0.20:{sb['pct_above_020']:5.1f}% {indicator} Δ=${delta_avg:+.3f}")

print("\n" + "="*70)
print("BY YEAR")
print("="*70)
for yr in sorted(by_year):
    d = by_year[yr]
    sa = stats(d['a']); sb = stats(d['b'])
    delta_avg = sb['avg'] - sa['avg']
    print(f"{yr} [n={sa['n']:3d}]: A avg=${sa['avg']:.3f} ≥$0.20:{sa['pct_above_020']:5.1f}% | B avg=${sb['avg']:.3f} ≥$0.20:{sb['pct_above_020']:5.1f}% Δ=${delta_avg:+.3f}")

print("\n" + "="*70)
print("BY SPY LEVEL")
print("="*70)
for bkt in ['<400','400-500','500-600','600-700','700-750','750+']:
    d = by_spy[bkt]
    if not d['a']: continue
    sa = stats(d['a']); sb = stats(d['b'])
    delta_avg = sb['avg'] - sa['avg']
    print(f"SPY {bkt:8s} [n={sa['n']:3d}]: A avg=${sa['avg']:.3f} ≥$0.20:{sa['pct_above_020']:5.1f}% | B avg=${sb['avg']:.3f} ≥$0.20:{sb['pct_above_020']:5.1f}% Δ=${delta_avg:+.3f}")

# Rescue days
rescue_b = [r for r in results if r['a']['credit'] == 0.0 and r['b']['credit'] > 0]
rescue_a = [r for r in results if r['b']['credit'] == 0.0 and r['a']['credit'] > 0]
print(f"\n{'='*70}")
print(f"RESCUE ANALYSIS")
print(f"{'='*70}")
print(f"Days B finds credit when A finds $0.00: {len(rescue_b)}")
print(f"Days A finds credit when B finds $0.00: {len(rescue_a)}")
print(f"Net advantage to B: {len(rescue_b) - len(rescue_a)} additional tradeable days")

# High SPY analysis (the June 15 problem)
high_spy_days = [r for r in results if r['spy'] >= 745]
print(f"\n{'='*70}")
print(f"HIGH SPY (≥$745) ANALYSIS — {len(high_spy_days)} days")
print(f"{'='*70}")
if high_spy_days:
    ha = [r['a']['credit'] for r in high_spy_days]
    hb = [r['b']['credit'] for r in high_spy_days]
    sa=stats(ha); sb=stats(hb)
    print(f"A: avg=${sa['avg']:.3f} ≥$0.20:{sa['pct_above_020']}% | B: avg=${sb['avg']:.3f} ≥$0.20:{sb['pct_above_020']}%")
    print("\nSample (last 8 days):")
    for r in sorted(high_spy_days, key=lambda x: x['date'])[-8:]:
        flag = '← RESCUE' if r['a']['credit'] == 0 and r['b']['credit'] > 0 else ''
        print(f"  {r['date']} SPY={r['spy']:6.1f} VIX={r['vix']:5.1f} | A=${r['a']['credit']:.2f}@{r['a']['strike']:.0f} | B=${r['b']['credit']:.2f}@{r['b']['strike']:.0f} {flag}")

# Verdict
print(f"\n{'='*70}")
print("VERDICT")
print(f"{'='*70}")
total_days = len(results)
b_better = sum(1 for r in results if r['b']['credit'] > r['a']['credit'])
a_better = sum(1 for r in results if r['a']['credit'] > r['b']['credit'])
same = total_days - b_better - a_better
print(f"B better credit: {b_better}/{total_days} days ({b_better/total_days*100:.1f}%)")
print(f"A better credit: {a_better}/{total_days} days ({a_better/total_days*100:.1f}%)")
print(f"Same:            {same}/{total_days} days ({same/total_days*100:.1f}%)")

avg_improvement = sum(r['credit_diff'] for r in results) / total_days
print(f"Average credit improvement with B: ${avg_improvement:+.4f}/trade")

# Save
summary = {
    'generated': datetime.now().isoformat(),
    'n_valid_days': total_days,
    'formula_a_overall': stats(all_a),
    'formula_b_overall': stats(all_b),
    'pct_otm_consistency': {'a': sa_otm, 'b': sb_otm},
    'by_vix': {k: {'a': stats(v['a']), 'b': stats(v['b'])} for k,v in by_vix.items()},
    'by_year': {k: {'a': stats(v['a']), 'b': stats(v['b'])} for k,v in by_year.items()},
    'by_spy_level': {k: {'a': stats(v['a']), 'b': stats(v['b'])} for k,v in by_spy.items()},
    'rescue_days_b': len(rescue_b),
    'rescue_days_a': len(rescue_a),
    'b_better_days': b_better,
    'a_better_days': a_better,
    'avg_credit_improvement_b': round(avg_improvement, 4),
    'sample_high_spy': [r for r in high_spy_days[-10:]],
}
os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT,'w') as f:
    json.dump(summary, f, indent=2)
print(f"\n✅ Saved: {OUTPUT}")
