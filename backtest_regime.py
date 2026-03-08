"""Backtest regime filter on historical live trades.

Recomputes the regime classification at the time of each trade using
stored candle data, then compares:
  A) Actual results (all trades taken)
  B) With regime filter (counter-trend trades skipped)

Includes ALL outcomes: WIN, LOSS, EARLY_EXIT, and maker fills.

Usage:
    python backtest_regime.py
    python backtest_regime.py --threshold 0.25  # try different threshold
"""

import argparse
import sqlite3
import sys

# Add project root to path for imports
sys.path.insert(0, ".")

from data.models import Candle
from signals.regime import RegimeDetector


def load_candles(conn: sqlite3.Connection) -> list[Candle]:
    """Load all candles sorted by timestamp."""
    rows = conn.execute(
        "SELECT timestamp, open, high, low, close, volume, taker_buy_volume, trades "
        "FROM candles ORDER BY timestamp"
    ).fetchall()
    return [
        Candle(
            timestamp=r[0], open=r[1], high=r[2], low=r[3],
            close=r[4], volume=r[5], taker_buy_volume=r[6], trades=r[7],
        )
        for r in rows
    ]


def load_trades(conn: sqlite3.Connection) -> list[dict]:
    """Load ALL settled live trades (WIN, LOSS, EARLY_EXIT) including maker fills."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM live_trades "
        "WHERE success = 1 AND outcome IS NOT NULL AND pnl IS NOT NULL "
        "ORDER BY timestamp"
    ).fetchall()
    conn.row_factory = None
    return [dict(r) for r in rows]


def get_candles_at(all_candles: list[Candle], trade_ts_ms: int, n: int = 50) -> list[Candle]:
    """Get the last N candles before the given timestamp (binary search)."""
    lo, hi = 0, len(all_candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if all_candles[mid].timestamp <= trade_ts_ms:
            lo = mid + 1
        else:
            hi = mid
    end = lo
    start = max(0, end - n)
    return all_candles[start:end]


def fmt_wr(wins, total):
    return f"{wins/total*100:.1f}%" if total > 0 else "N/A"


def print_group(label, trades):
    """Print summary for a group of trades."""
    if not trades:
        print(f"  {label}: 0 trades")
        return
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    early = sum(1 for t in trades if t["outcome"] == "EARLY_EXIT")
    pnl = sum(t["pnl"] for t in trades)
    wl_total = wins + losses  # early exits excluded from WR
    wr_str = fmt_wr(wins, wl_total) if wl_total > 0 else "N/A"
    early_str = f" + {early}ee" if early > 0 else ""
    print(f"  {label}: {len(trades):3d} trades | W/L: {wins}/{losses}{early_str} | "
          f"WR: {wr_str} | P&L: ${pnl:+.2f}")


def run_backtest(db_path: str, threshold: float = 0.30) -> None:
    conn = sqlite3.connect(db_path)

    print(f"Loading data from {db_path}...")
    all_candles = load_candles(conn)
    trades = load_trades(conn)
    conn.close()

    # Separate by source
    taker_trades = [t for t in trades if t.get("trade_tag") != "maker_fill"]
    maker_trades = [t for t in trades if t.get("trade_tag") == "maker_fill"]

    print(f"Candles: {len(all_candles)}")
    print(f"Trades: {len(trades)} total ({len(taker_trades)} taker, {len(maker_trades)} maker)")
    print(f"Regime threshold: {threshold}\n")

    detector = RegimeDetector(trend_threshold=threshold)

    # Classify each trade
    results = []
    for trade in trades:
        candles = get_candles_at(all_candles, trade["timestamp"], n=50)
        if len(candles) < 30:
            regime_label = "insufficient_data"
            strength = 0.0
        else:
            state = detector.classify(candles)
            regime_label = state.regime
            strength = state.strength

        side = trade["side"]
        is_counter = (
            (regime_label == "trending_up" and side == "DOWN") or
            (regime_label == "trending_down" and side == "UP")
        )

        results.append({
            **trade,
            "regime": regime_label,
            "strength": strength,
            "is_counter": is_counter,
            "is_maker": trade.get("trade_tag") == "maker_fill",
        })

    # ================================================================
    # A) ACTUAL RESULTS
    # ================================================================
    all_pnl = sum(r["pnl"] for r in results)
    all_wins = sum(1 for r in results if r["outcome"] == "WIN")
    all_losses = sum(1 for r in results if r["outcome"] == "LOSS")
    all_early = sum(1 for r in results if r["outcome"] == "EARLY_EXIT")
    all_wl = all_wins + all_losses

    print("=" * 65)
    print("A) ACTUAL RESULTS (all trades)")
    print("=" * 65)
    print(f"  Trades: {len(results)} | W/L: {all_wins}/{all_losses} + {all_early} early exits")
    print(f"  Win rate: {fmt_wr(all_wins, all_wl)} (excl. early exits)")
    print(f"  P&L: ${all_pnl:+.2f}")

    # Sub-breakdown
    taker_r = [r for r in results if not r["is_maker"]]
    maker_r = [r for r in results if r["is_maker"]]
    early_r = [r for r in results if r["outcome"] == "EARLY_EXIT"]
    wl_r = [r for r in results if r["outcome"] in ("WIN", "LOSS")]
    print(f"\n  Taker W/L P&L:   ${sum(r['pnl'] for r in taker_r if r['outcome'] in ('WIN','LOSS')):+.2f}")
    print(f"  Early exit P&L:  ${sum(r['pnl'] for r in early_r):+.2f}")
    print(f"  Maker fill P&L:  ${sum(r['pnl'] for r in maker_r):+.2f}")
    print()

    # ================================================================
    # B) WITH REGIME FILTER (counter-trend skipped)
    # Note: early exits happen AFTER entry. If the trade was never
    # entered (counter-trend skip), the early exit wouldn't exist either.
    # So we must skip the entire trade including its early exit.
    # ================================================================
    kept = [r for r in results if not r["is_counter"]]
    skipped = [r for r in results if r["is_counter"]]

    kept_pnl = sum(r["pnl"] for r in kept)
    skip_pnl = sum(r["pnl"] for r in skipped)

    print("=" * 65)
    print("B) WITH REGIME FILTER (counter-trend trades skipped)")
    print("=" * 65)
    print_group("Kept   ", kept)
    print_group("Skipped", skipped)
    print()
    print(f"  Filter saves: ${-skip_pnl:+.2f} (avoided losses)")
    print(f"  Filtered P&L: ${kept_pnl:+.2f} vs actual ${all_pnl:+.2f}")
    print()

    # ================================================================
    # BREAKDOWN BY REGIME
    # ================================================================
    print("=" * 65)
    print("BREAKDOWN BY REGIME (all outcomes)")
    print("=" * 65)
    for regime in ["trending_up", "trending_down", "ranging", "insufficient_data"]:
        subset = [r for r in results if r["regime"] == regime]
        if not subset:
            continue
        print_group(f"{regime:<20s}", subset)
    print()

    # ================================================================
    # BREAKDOWN BY REGIME + ALIGNMENT
    # ================================================================
    print("=" * 65)
    print("COUNTER-TREND vs ALIGNED (excl. maker fills)")
    print("=" * 65)
    non_maker = [r for r in results if not r["is_maker"]]
    for regime in ["trending_up", "trending_down"]:
        subset = [r for r in non_maker if r["regime"] == regime]
        if not subset:
            continue
        aligned = [r for r in subset if not r["is_counter"]]
        counter = [r for r in subset if r["is_counter"]]
        print_group(f"{regime} ALIGNED", aligned)
        print_group(f"{regime} COUNTER", counter)

    ranging = [r for r in non_maker if r["regime"] == "ranging"]
    print_group("ranging            ", ranging)
    print()

    # ================================================================
    # EARLY EXITS BY REGIME
    # ================================================================
    print("=" * 65)
    print("EARLY EXITS BY REGIME")
    print("=" * 65)
    for regime in ["trending_up", "trending_down", "ranging"]:
        ee = [r for r in results if r["regime"] == regime and r["outcome"] == "EARLY_EXIT"]
        if ee:
            ee_pnl = sum(r["pnl"] for r in ee)
            ct = sum(1 for r in ee if r["is_counter"])
            print(f"  {regime:<20s}: {len(ee)} exits (${ee_pnl:+.2f}) | "
                  f"{ct} counter-trend (would be skipped)")
    print()

    # ================================================================
    # BY SIDE
    # ================================================================
    print("=" * 65)
    print("BY SIDE (all outcomes)")
    print("=" * 65)
    for side in ["UP", "DOWN"]:
        subset = [r for r in non_maker if r["side"] == side]
        kept_s = [r for r in subset if not r["is_counter"]]
        print_group(f"{side} all     ", subset)
        print_group(f"{side} filtered", kept_s)
    print()

    # ================================================================
    # THRESHOLD SENSITIVITY
    # ================================================================
    print("=" * 65)
    print("THRESHOLD SENSITIVITY (full P&L incl. early exits + maker)")
    print("=" * 65)
    for t in [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]:
        det = RegimeDetector(trend_threshold=t)
        kept_t = []
        skipped_n = 0
        for trade in trades:
            candles = get_candles_at(all_candles, trade["timestamp"], n=50)
            if len(candles) < 30:
                kept_t.append(trade)
                continue
            st = det.classify(candles)
            is_ct = (
                (st.regime == "trending_up" and trade["side"] == "DOWN") or
                (st.regime == "trending_down" and trade["side"] == "UP")
            )
            if is_ct:
                skipped_n += 1
            else:
                kept_t.append(trade)

        kpnl = sum(r["pnl"] or 0 for r in kept_t)
        kw = sum(1 for r in kept_t if r["outcome"] == "WIN")
        kl = sum(1 for r in kept_t if r["outcome"] == "LOSS")
        kee = sum(1 for r in kept_t if r["outcome"] == "EARLY_EXIT")
        kwr = fmt_wr(kw, kw + kl)
        marker = " <-- current" if abs(t - threshold) < 0.001 else ""
        print(f"  t={t:.2f}: kept={len(kept_t):3d} skip={skipped_n:3d} | "
              f"WR: {kwr} | {kw}W/{kl}L/{kee}ee | P&L: ${kpnl:+.2f}{marker}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest regime filter on historical trades")
    parser.add_argument("--db", default="btc_edge.db", help="Path to SQLite database")
    parser.add_argument("--threshold", type=float, default=0.30, help="Regime trend threshold")
    args = parser.parse_args()
    run_backtest(args.db, args.threshold)
