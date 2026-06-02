# Graph Report - C:\Users\sagla\spy-0dte-trader  (2026-05-29)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 82 nodes · 199 edges · 16 communities (11 shown, 5 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]

## God Nodes (most connected - your core abstractions)
1. `main()` - 23 edges
2. `datetime` - 14 edges
3. `float` - 14 edges
4. `main()` - 13 edges
5. `str` - 13 edges
6. `score_conditions()` - 11 edges
7. `Position` - 8 edges
8. `log_trade()` - 8 edges
9. `bool` - 7 edges
10. `fetch_option_ask()` - 7 edges

## Surprising Connections (you probably didn't know these)
- `get_daily_bars()` --calls--> `pg()`  [EXTRACTED]
  morning_briefing.py → morning_briefing.py  _Bridges community 4 → community 3_
- `main()` --calls--> `last_trading_day()`  [EXTRACTED]
  morning_briefing.py → morning_briefing.py  _Bridges community 3 → community 7_
- `main()` --calls--> `fmt_chg()`  [EXTRACTED]
  morning_briefing.py → morning_briefing.py  _Bridges community 11 → community 7_
- `main()` --calls--> `fetch_premarket()`  [EXTRACTED]
  morning_briefing.py → morning_briefing.py  _Bridges community 4 → community 7_
- `main()` --calls--> `fetch_calendar()`  [EXTRACTED]
  morning_briefing.py → morning_briefing.py  _Bridges community 12 → community 7_

## Communities (16 total, 5 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.38
Nodes (8): build_report(), _f(), get_account_balance(), get_quote(), get_session_token(), main(), float, str

### Community 1 - "Community 1"
Cohesion: 0.44
Nodes (8): str, fetch_spy_bars(), fetch_vix(), poly_get(), _s(), tt_get(), tt_token(), validate_env()

### Community 2 - "Community 2"
Cohesion: 0.46
Nodes (7): bool, datetime, in_market_hours(), in_trade_window(), or_complete(), past_cutoff(), score_conditions()

### Community 3 - "Community 3"
Cohesion: 0.48
Nodes (6): fetch_bull_bear_volume(), fetch_spy_bars(), fetch_vxx_bars(), get_daily_bars(), last_trading_day(), SPXL (3x bull SPY) vs SPXU (3x bear SPY) volume ratio.     Bullish when SPXL vol

### Community 4 - "Community 4"
Cohesion: 0.40
Nodes (6): date, float, fetch_premarket(), fetch_put_call(), pg(), Build P/C from ATM +/- 4 strikes at next weekly expiry.     Throttled to avoid f

### Community 5 - "Community 5"
Cohesion: 0.53
Nodes (3): main(), now_et(), ORTracker

### Community 6 - "Community 6"
Cohesion: 0.47
Nodes (3): hr(), Position, print_dashboard()

### Community 7 - "Community 7"
Cohesion: 0.50
Nodes (5): arrow(), compute_bias(), fmt_p(), ma(), main()

### Community 8 - "Community 8"
Cohesion: 0.50
Nodes (5): date, build_opt_info(), build_opt_ticker(), nearest_exp(), OCC/Polygon format: O:SPY260524C00590000

### Community 9 - "Community 9"
Cohesion: 0.40
Nodes (5): float, calc_opening_range(), calc_vol_avg(), calc_vwap(), Rolling average of the preceding VOL_PERIODS candles (excludes current bar).

### Community 10 - "Community 10"
Cohesion: 0.40
Nodes (3): fetch_spy_close(), date, float

## Knowledge Gaps
- **4 isolated node(s):** `float`, `date`, `float`, `float`
  These have ≤1 connection - possible missing edges or undocumented components.
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `datetime` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 5`, `Community 6`, `Community 10`, `Community 15`?**
  _High betweenness centrality (0.629) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 5` to `Community 1`, `Community 2`, `Community 6`, `Community 8`, `Community 9`, `Community 13`, `Community 15`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Why does `score_conditions()` connect `Community 2` to `Community 1`, `Community 5`, `Community 9`, `Community 15`?**
  _High betweenness centrality (0.063) - this node is a cross-community bridge._
- **What connects `float`, `Build P/C from ATM +/- 4 strikes at next weekly expiry.     Throttled to avoid f`, `SPXL (3x bull SPY) vs SPXU (3x bear SPY) volume ratio.     Bullish when SPXL vol` to the rest of the system?**
  _11 weakly-connected nodes found - possible documentation gaps or missing edges._