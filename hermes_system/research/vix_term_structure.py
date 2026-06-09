"""
VIX Term Structure Research
Compares strategy performance in contango (VIX3M/VIX >= 1.05) vs backwardation (< 1.05) regimes.
"""

import os
import io
import numpy as np
import pandas as pd
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VIX3M_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv"
VIX_URL   = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"

TRADES_PATH   = os.path.join(BASE_DIR, "backtest_real_data_trades.csv")
FALLBACK_PATH = os.path.join(BASE_DIR, "backtest_90pct_trades.csv")
RESULTS_PATH  = os.path.join(os.path.dirname(__file__), "vix_term_structure_results.txt")

CONTANGO_THRESHOLD = 1.05


def fetch_cboe_index(url: str, name: str) -> pd.Series:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = df.columns.str.strip()
    df["DATE"] = pd.to_datetime(df["DATE"])
    df = df.set_index("DATE").sort_index()
    close_col = "CLOSE" if "CLOSE" in df.columns else df.columns[0]
    return df[close_col].rename(name)


def load_trades() -> pd.DataFrame:
    path = TRADES_PATH if os.path.exists(TRADES_PATH) else FALLBACK_PATH
    print(f"Loading trades from: {path}")
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def sharpe(returns: pd.Series) -> float:
    if returns.std() == 0 or len(returns) < 2:
        return float("nan")
    return (returns.mean() / returns.std()) * np.sqrt(252)


def bucket_stats(df: pd.DataFrame, label: str) -> dict:
    n = len(df)
    if n == 0:
        return {"bucket": label, "n_trades": 0, "win_rate": float("nan"),
                "total_pnl": 0.0, "avg_pnl": float("nan"), "sharpe": float("nan")}
    wins = (df["pnl"] > 0).sum()
    return {
        "bucket":    label,
        "n_trades":  n,
        "win_rate":  wins / n,
        "total_pnl": df["pnl"].sum(),
        "avg_pnl":   df["pnl"].mean(),
        "sharpe":    sharpe(df["pnl"]),
    }


def format_stats(s: dict) -> str:
    return (
        f"  Trades  : {s['n_trades']}\n"
        f"  Win Rate: {s['win_rate']:.1%}\n"
        f"  Total P&L: ${s['total_pnl']:,.2f}\n"
        f"  Avg P&L : ${s['avg_pnl']:.2f}\n"
        f"  Sharpe  : {s['sharpe']:.3f}\n"
    )


def main():
    print("Fetching VIX3M ...")
    vix3m = fetch_cboe_index(VIX3M_URL, "VIX3M")
    print("Fetching VIX ...")
    vix = fetch_cboe_index(VIX_URL, "VIX")

    term = pd.DataFrame({"VIX3M": vix3m, "VIX": vix}).dropna()
    term["contango_ratio"] = term["VIX3M"] / term["VIX"]

    trades = load_trades()

    merged = trades.merge(
        term[["contango_ratio"]],
        left_on="date",
        right_index=True,
        how="left",
    )

    unmatched = merged["contango_ratio"].isna().sum()
    if unmatched:
        print(f"Warning: {unmatched} trade(s) had no matching VIX date — excluded from regime buckets.")

    merged = merged.dropna(subset=["contango_ratio"])

    contango     = merged[merged["contango_ratio"] >= CONTANGO_THRESHOLD]
    backwardation = merged[merged["contango_ratio"] < CONTANGO_THRESHOLD]

    all_stats   = bucket_stats(merged,       f"All trades (n={len(merged)})")
    cont_stats  = bucket_stats(contango,     f"Contango  (ratio >= {CONTANGO_THRESHOLD})")
    back_stats  = bucket_stats(backwardation, f"Backwardation (ratio < {CONTANGO_THRESHOLD})")

    # Per-strategy breakdown
    strategy_lines = []
    for strat, grp in merged.groupby("strategy"):
        c = grp[grp["contango_ratio"] >= CONTANGO_THRESHOLD]
        b = grp[grp["contango_ratio"] < CONTANGO_THRESHOLD]
        strategy_lines.append(f"\n--- {strat} ---")
        strategy_lines.append(f"  Contango     : n={len(c)}, WR={c['pnl'].gt(0).mean():.1%}, P&L=${c['pnl'].sum():.2f}, Sharpe={sharpe(c['pnl']):.3f}")
        strategy_lines.append(f"  Backwardation: n={len(b)}, WR={b['pnl'].gt(0).mean():.1%}, P&L=${b['pnl'].sum():.2f}, Sharpe={sharpe(b['pnl']):.3f}")

    lines = [
        "=" * 60,
        "VIX TERM STRUCTURE ANALYSIS",
        f"Contango threshold: VIX3M/VIX >= {CONTANGO_THRESHOLD}",
        f"VIX3M date range : {term.index.min().date()} to {term.index.max().date()}",
        f"Trades matched   : {len(merged)} of {len(trades)}",
        "=" * 60,
        "",
        "[ OVERALL ]",
        format_stats(all_stats),
        f"[ CONTANGO (ratio >= {CONTANGO_THRESHOLD}) ]",
        format_stats(cont_stats),
        f"[ BACKWARDATION (ratio < {CONTANGO_THRESHOLD}) ]",
        format_stats(back_stats),
        "",
        "[ PER-STRATEGY BREAKDOWN ]",
        *strategy_lines,
        "",
        "[ REGIME DISTRIBUTION ]",
        f"  Contango days     : {(term['contango_ratio'] >= CONTANGO_THRESHOLD).sum()} / {len(term)}",
        f"  Backwardation days: {(term['contango_ratio'] < CONTANGO_THRESHOLD).sum()} / {len(term)}",
        f"  Avg ratio         : {term['contango_ratio'].mean():.4f}",
        f"  Median ratio      : {term['contango_ratio'].median():.4f}",
        "=" * 60,
    ]

    report = "\n".join(lines)
    print("\n" + report)

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        f.write(report + "\n")
    print(f"\nResults saved to: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
