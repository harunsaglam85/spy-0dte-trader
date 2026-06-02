#!/usr/bin/env python3
"""
edge_scan_historical.py
========================
Scans next 12 SPY option expirations via Tradier API.
Finds put credit spread configs where breakeven win rate < 75%.

Breakeven WR formula:
    BE WR = (width - credit) / width
    Edge exists when credit > 25% of spread width -> BE WR < 75%

Credentials: C:/Users/sagla/.tastytrade-mcp/.env -> TRADIER_API_KEY
Rate limit : 0.5 s between API calls
"""

import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ── config ────────────────────────────────────────────────────────────────────
ENV_PATH        = Path(r'C:\Users\sagla\.tastytrade-mcp\.env')
REPORT_PATH     = Path(r'C:\Users\sagla\edge_scan_report.txt')
TRADIER_BASE    = 'https://api.tradier.com/v1'
SYMBOL          = 'SPY'
N_EXPIRATIONS   = 12
WIDTHS          = [1, 2, 3, 5, 7, 10]   # spread widths to test ($)
DELTA_MIN       = 0.10                   # min |delta| for short put
DELTA_MAX       = 0.45                   # max |delta| for short put
BE_WR_THRESHOLD = 75.0                   # flag anything below this (%)
RATE_LIMIT      = 0.5                    # seconds between API calls
MIN_BID         = 0.02                   # skip options with bid below this


# ── credentials ───────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


# ── Tradier API ───────────────────────────────────────────────────────────────
def _api_get(endpoint: str, params: dict, hdrs: dict) -> dict:
    url  = f'{TRADIER_BASE}/{endpoint}'
    resp = requests.get(url, params=params, headers=hdrs, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if 'fault' in data:
        raise RuntimeError(f'Tradier error: {data["fault"].get("faultstring", data["fault"])}')
    return data


def fetch_expirations(hdrs: dict) -> List[str]:
    data  = _api_get('markets/options/expirations',
                      {'symbol': SYMBOL, 'includeAllRoots': 'false'}, hdrs)
    dates = (data.get('expirations') or {}).get('date') or []
    if isinstance(dates, str):
        dates = [dates]
    today = date.today().isoformat()
    return sorted(d for d in dates if d > today)[:N_EXPIRATIONS]


def fetch_chain(expiration: str, hdrs: dict) -> List[dict]:
    data = _api_get('markets/options/chains',
                     {'symbol': SYMBOL, 'expiration': expiration, 'greeks': 'true'}, hdrs)
    opts = (data.get('options') or {}).get('option') or []
    if isinstance(opts, dict):   # single result: Tradier returns object not list
        opts = [opts]
    return opts


# ── spread scanner ────────────────────────────────────────────────────────────
def scan_put_spreads(chain: List[dict], expiration: str) -> List[dict]:
    """
    Tests every (short delta, width) pair for edge.
    Conservative pricing: sell short put at bid, buy long put at ask.

    BE WR = (width - credit) / width
    This is the minimum win rate required to break even — lower is better for the seller.
    Edge threshold: BE WR < 75% (credit > 25% of width).
    """
    dte = (date.fromisoformat(expiration) - date.today()).days

    # Build put map: strike (float) -> quote/greek dict
    put_map: Dict[float, dict] = {}
    for opt in chain:
        if opt.get('option_type') != 'put':
            continue
        strike = opt.get('strike')
        bid    = float(opt.get('bid') or 0)
        ask    = float(opt.get('ask') or 0)
        greeks = opt.get('greeks') or {}
        delta  = greeks.get('delta')

        if strike is None or delta is None:
            continue
        if bid < MIN_BID or ask <= 0 or ask < bid:
            continue

        iv_raw = (greeks.get('mid_iv') or greeks.get('smv_vol')
                  or greeks.get('bid_iv') or 0)
        put_map[float(strike)] = {
            'strike': float(strike),
            'bid':    bid,
            'ask':    ask,
            'mid':    (bid + ask) / 2.0,
            'delta':  float(delta),
            'iv':     float(iv_raw) * 100.0,   # stored as decimal, display as %
            'theta':  float(greeks.get('theta') or 0),
            'gamma':  float(greeks.get('gamma') or 0),
            'oi':     int(opt.get('open_interest') or 0),
        }

    if len(put_map) < 2:
        return []

    edges: List[dict] = []

    for short_k, sp in put_map.items():
        abs_d = abs(sp['delta'])
        if not (DELTA_MIN <= abs_d <= DELTA_MAX):
            continue

        for width in WIDTHS:
            target_long_k = short_k - width

            # Prefer exact strike; accept nearest within ±$0.50 (non-standard spacing)
            if target_long_k in put_map:
                lp = put_map[target_long_k]
            else:
                below = [k for k in put_map if k < short_k]
                if not below:
                    continue
                nearest = min(below, key=lambda k: abs(k - target_long_k))
                if abs(nearest - target_long_k) > 0.50:
                    continue
                lp = put_map[nearest]

            actual_width = short_k - lp['strike']
            if actual_width <= 0:
                continue

            # Credit at worst realistic fill
            credit = round(sp['bid'] - lp['ask'], 4)
            if credit < 0.01:
                continue

            # BE WR: the win rate you must achieve just to break even
            be_wr      = (actual_width - credit) / actual_width * 100.0
            credit_pct = credit / actual_width * 100.0

            if be_wr >= BE_WR_THRESHOLD:
                continue

            edges.append({
                'expiration':   expiration,
                'dte':          dte,
                'short_strike': short_k,
                'long_strike':  lp['strike'],
                'width':        actual_width,
                'delta':        round(sp['delta'], 4),
                'abs_delta':    round(abs_d, 4),
                'short_bid':    sp['bid'],
                'short_ask':    sp['ask'],
                'long_bid':     lp['bid'],
                'long_ask':     lp['ask'],
                'credit':       credit,
                'credit_mid':   round(sp['mid'] - lp['mid'], 4),
                'credit_pct':   round(credit_pct, 2),
                'max_gain':     round(credit * 100, 2),
                'max_loss':     round((actual_width - credit) * 100, 2),
                'target_profit': round(credit * 0.50 * 100, 2),  # 50% of max credit
                'be_wr':        round(be_wr, 2),
                'edge_bps':     round(BE_WR_THRESHOLD - be_wr, 2),
                'iv':           round(sp['iv'], 2),
                'theta':        round(sp['theta'], 4),
                'gamma':        round(sp['gamma'], 6),
                'oi':           sp['oi'],
            })

    return sorted(edges, key=lambda x: x['be_wr'])


# ── report builder ────────────────────────────────────────────────────────────
def build_report(per_exp: List[Tuple[str, List[dict]]], all_edges: List[dict]) -> str:
    SEP   = '=' * 84
    sep   = '-' * 84
    today = date.today()
    now   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with_edge = [(e, eds) for e, eds in per_exp if eds]
    no_edge   = [(e, eds) for e, eds in per_exp if not eds]

    lines = [
        SEP,
        '  SPY PUT CREDIT SPREAD — EDGE SCAN  (Tradier Live Data)',
        f'  Period    : {per_exp[0][0]}  ->  {per_exp[-1][0]}  ({len(per_exp)} expirations)',
        f'  Test grid : delta {DELTA_MIN:.2f}–{DELTA_MAX:.2f}  |  '
            f'widths ${" / $".join(str(w) for w in WIDTHS)}',
        f'  Threshold : BE WR < {BE_WR_THRESHOLD:.0f}%  '
            f'(credit must be > {100 - BE_WR_THRESHOLD:.0f}% of spread width)',
        f'  Pricing   : Conservative — short at bid, long at ask',
        f'  Generated : {now}',
        SEP,
        '',
        '  PER-EXPIRATION SCAN',
        sep,
    ]

    for i, (expiration, edges) in enumerate(per_exp, 1):
        dte = (date.fromisoformat(expiration) - today).days
        tag = f'  [{i:>2}/{len(per_exp)}]  {expiration}  {dte:>3} DTE'
        if edges:
            best = edges[0]
            lines.append(f'{tag}  --  EDGE FOUND  ({len(edges):>3} configs)')
            lines.append(
                f'             Best:  delta={best["delta"]:+.3f}  '
                f'width=${best["width"]:.0f}  '
                f'credit=${best["credit"]:.4f}  '
                f'BE WR={best["be_wr"]:.1f}%  '
                f'({best["edge_bps"]:.1f}% below threshold)'
            )
        else:
            lines.append(f'{tag}  --  No edge found')

    # Summary
    lines += [
        '',
        sep,
        '  SUMMARY',
        sep,
        '',
        f'  Expirations with edge  : {len(with_edge):>2} of {len(per_exp)}',
        f'  Expirations no edge    : {len(no_edge):>2} of {len(per_exp)}',
        f'  Total edge configs     : {len(all_edges)}',
    ]
    if all_edges:
        avg_be     = sum(e['be_wr']      for e in all_edges) / len(all_edges)
        avg_cr_pct = sum(e['credit_pct'] for e in all_edges) / len(all_edges)
        best_g     = all_edges[0]
        lines += [
            f'  Avg BE WR (edge only)  : {avg_be:.1f}%',
            f'  Avg credit / width     : {avg_cr_pct:.1f}%',
            f'  Best overall BE WR     : {best_g["be_wr"]:.1f}%  '
                f'({best_g["expiration"]} | '
                f'delta={best_g["delta"]:+.3f} | '
                f'${best_g["width"]:.0f} wide)',
        ]
    lines += ['', '']

    # Top 3
    if all_edges:
        top3 = sorted(all_edges, key=lambda x: x['be_wr'])[:3]
        lines += [SEP, '  TOP 3 OPPORTUNITIES  (Lowest Breakeven Win Rate)', SEP, '']
        for rank, e in enumerate(top3, 1):
            rr = e['max_gain'] / e['max_loss'] if e['max_loss'] > 0 else 0
            lines += [
                f'  #{rank}  {e["expiration"]}  |  {e["dte"]} DTE',
                f'      Structure   : Sell {e["short_strike"]:.2f}P / Buy {e["long_strike"]:.2f}P'
                    f'  (${e["width"]:.0f} wide put spread)',
                f'      Entry delta : {e["delta"]:+.3f}  (abs {e["abs_delta"]:.3f})',
                f'      Pricing     : Short bid ${e["short_bid"]:.4f}  '
                    f'| Long ask ${e["long_ask"]:.4f}',
                f'      Credit      : ${e["credit"]:.4f}/share  '
                    f'(mid-mkt: ${e["credit_mid"]:.4f})  '
                    f'= {e["credit_pct"]:.1f}% of width',
                f'      Per contract: Max gain ${e["max_gain"]:.2f}  '
                    f'| Max loss ${e["max_loss"]:.2f}  '
                    f'| 50% TP target ${e["target_profit"]:.2f}  '
                    f'| Reward/risk {rr:.2f}x',
                f'      Breakeven WR: {e["be_wr"]:.2f}%  '
                    f'-- {e["edge_bps"]:.1f}% BELOW {BE_WR_THRESHOLD:.0f}% threshold  '
                    f'[EDGE CONFIRMED]',
                f'      Greeks      : IV {e["iv"]:.1f}%  '
                    f'| Theta ${e["theta"]:.4f}/day  '
                    f'| Gamma {e["gamma"]:.6f}',
                f'      Open Int    : {e["oi"]:,}',
                '',
            ]

    # Full edge table grouped by expiration
    if all_edges:
        lines += [sep, '  ALL EDGE CONFIGURATIONS', sep]

        by_exp: Dict[str, List[dict]] = {}
        for e in all_edges:
            by_exp.setdefault(e['expiration'], []).append(e)

        for expiration in sorted(by_exp):
            exp_edges = sorted(by_exp[expiration], key=lambda x: x['be_wr'])
            dte       = exp_edges[0]['dte']
            lines += [
                '',
                f'  {expiration}  |  {dte} DTE  '
                    f'({len(exp_edges)} config{"s" if len(exp_edges) != 1 else ""})',
                f'  {"Delta":>7}  {"Width":>6}  {"Credit":>8}  {"Cr%Wid":>7}  '
                    f'{"BE WR":>7}  {"Edge":>6}  '
                    f'{"MaxGain":>8}  {"MaxLoss":>9}  {"IV%":>6}  {"OI":>7}',
                '  ' + '-' * 74,
            ]
            for e in exp_edges:
                lines.append(
                    f'  {e["delta"]:>+7.3f}  '
                    f'${e["width"]:>4.0f}   '
                    f'${e["credit"]:>7.4f}  '
                    f'{e["credit_pct"]:>6.1f}%  '
                    f'{e["be_wr"]:>6.1f}%  '
                    f'-{e["edge_bps"]:>5.1f}%  '
                    f'${e["max_gain"]:>7.2f}  '
                    f'-${e["max_loss"]:>7.2f}  '
                    f'{e["iv"]:>5.1f}%  '
                    f'{e["oi"]:>7,}'
                )

    lines += ['', SEP]
    return '\n'.join(lines)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    W = 84
    print(f'\n{"=" * W}')
    print('  SPY Put Credit Spread — Edge Scanner  (Tradier API)')
    print(f'  Threshold: BE WR < {BE_WR_THRESHOLD:.0f}% | '
          f'Delta {DELTA_MIN:.2f}-{DELTA_MAX:.2f} | '
          f'Widths ${"/".join(str(w) for w in WIDTHS)}')
    print('=' * W)

    print('\n  Loading credentials...')
    env     = _load_env()
    api_key = env.get('TRADIER_API_KEY')
    if not api_key:
        sys.exit('ERROR: TRADIER_API_KEY not found in .env')
    print(f'  API key  : {api_key[:8]}...[redacted]')

    hdrs = {
        'Authorization': f'Bearer {api_key}',
        'Accept':        'application/json',
    }

    print('\n  Fetching SPY expirations...')
    try:
        expirations = fetch_expirations(hdrs)
    except requests.HTTPError as exc:
        status = exc.response.status_code
        if status == 401:
            sys.exit('ERROR 401: Invalid API key. Check TRADIER_API_KEY in .env')
        sys.exit(f'ERROR {status}: {exc}')
    except Exception as exc:
        sys.exit(f'ERROR: {exc}')

    if not expirations:
        sys.exit('No expirations returned. Verify account has market data access.')

    today = date.today()
    print(f'  {len(expirations)} expirations: {expirations[0]} -> {expirations[-1]}')
    print(f'  Rate limit: {RATE_LIMIT}s between calls (~{len(expirations) * RATE_LIMIT:.0f}s minimum)\n')

    per_exp:   List[Tuple[str, List[dict]]] = []
    all_edges: List[dict]                   = []

    for i, exp in enumerate(expirations, 1):
        dte = (date.fromisoformat(exp) - today).days
        print(f'  [{i:>2}/{len(expirations)}]  {exp}  ({dte:>3} DTE)  ', end='', flush=True)

        time.sleep(RATE_LIMIT)

        try:
            chain  = fetch_chain(exp, hdrs)
            n_puts = sum(1 for o in chain if o.get('option_type') == 'put')
            print(f'{n_puts:>4} puts  ', end='', flush=True)

            edges = scan_put_spreads(chain, exp)
            per_exp.append((exp, edges))
            all_edges.extend(edges)

            if edges:
                best = edges[0]
                print(f'EDGE: {len(edges):>3} configs  '
                      f'best={best["be_wr"]:.1f}% BE WR  '
                      f'(d={best["delta"]:+.2f} ${best["width"]:.0f}w '
                      f'cr=${best["credit"]:.3f})')
            else:
                print('No edge')

        except requests.HTTPError as exc:
            print(f'HTTP {exc.response.status_code}')
            per_exp.append((exp, []))
        except Exception as exc:
            print(f'ERROR: {exc}')
            per_exp.append((exp, []))

    n_with = sum(1 for _, eds in per_exp if eds)
    print(f'\n  Done.  {n_with}/{len(per_exp)} expirations have edge.  '
          f'{len(all_edges)} total configs.')

    report = build_report(per_exp, all_edges)
    print('\n' + report)

    REPORT_PATH.write_text(report, encoding='utf-8')
    print(f'\n  Report saved: {REPORT_PATH}')


if __name__ == '__main__':
    main()
