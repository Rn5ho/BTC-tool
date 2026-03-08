"""Backtest regime flip strategy on historical candle data.

For every non-overlapping 5-minute window in the candle DB, classifies
the regime from the preceding candles, then simulates:
  - Trend-following: bet WITH the trend during trending periods
  - Counter-trend: bet AGAINST the trend (what the ML model naturally does)
  - By strength bucket, hour, day-of-week

This tests the regime signal itself across all windows, not just traded ones.

Usage:
    python backtest_flip.py
    python backtest_flip.py --db btc_edge.db
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, ".")
from data.models import Candle
from signals.regime import RegimeDetector


def load_candles(conn):
    rows = conn.execute(
        "SELECT timestamp, open, high, low, close, volume, taker_buy_volume, trades "
        "FROM candles ORDER BY timestamp"
    ).fetchall()
    return [
        Candle(timestamp=r[0], open=r[1], high=r[2], low=r[3],
               close=r[4], volume=r[5], taker_buy_volume=r[6], trades=r[7])
        for r in rows
    ]


def build_windows(candles):
    """Build non-overlapping 5-minute windows from 1-min candles."""
    windows = []
    for i in range(0, len(candles) - 4):
        t_start = candles[i].timestamp
        t_end = candles[i + 4].timestamp
        if (t_end - t_start) > 360000:
            continue
        minute_of_hour = (t_start // 60000) % 60
        if minute_of_hour % 5 != 0:
            continue
        btc_open = candles[i].open
        btc_close = candles[i + 4].close
        windows.append({
            "ts": t_start,
            "open": btc_open,
            "close": btc_close,
            "went_up": btc_close > btc_open,
            "idx": i,
        })
    return windows


def classify_windows(candles, windows):
    """Add regime classification to each window."""
    detector = RegimeDetector(trend_threshold=0.30)
    results = []
    for w in windows:
        lookback = candles[max(0, w["idx"] - 50):w["idx"]]
        if len(lookback) < 30:
            continue
        state = detector.classify(lookback)
        results.append({
            **w,
            "regime": state.regime,
            "strength": state.strength,
        })
    return results


def sim_pnl(wins, losses, bet=3.50, entry=0.50, fee_factor=0.015):
    """Simulate P&L with fixed bet size."""
    shares_per = (bet / entry) * (1 - fee_factor)
    return (shares_per - bet) * wins + (-bet) * losses


def trend_won(r):
    """Did betting WITH the trend win this window?"""
    return (r["strength"] > 0 and r["went_up"]) or (r["strength"] < 0 and not r["went_up"])


def run_backtest(db_path):
    conn = sqlite3.connect(db_path)
    candles = load_candles(conn)
    conn.close()

    print(f"Loaded {len(candles)} candles")
    t0 = datetime.fromtimestamp(candles[0].timestamp / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(candles[-1].timestamp / 1000, tz=timezone.utc)
    days = (candles[-1].timestamp - candles[0].timestamp) / 1000 / 86400
    print(f"Period: {t0:%Y-%m-%d %H:%M} to {t1:%Y-%m-%d %H:%M} UTC ({days:.1f} days)")

    windows = build_windows(candles)
    print(f"Built {len(windows)} non-overlapping 5-min windows")

    results = classify_windows(candles, windows)
    print(f"Classified {len(results)} windows with regime data\n")

    # Base rate
    up_count = sum(1 for r in results if r["went_up"])
    print(f"Base rate: BTC up in {up_count}/{len(results)} windows ({up_count / len(results) * 100:.1f}%)\n")

    # ================================================================
    # STRATEGY COMPARISON
    # ================================================================
    print("=" * 65)
    print("STRATEGY COMPARISON")
    print("=" * 65)

    # C: Trend-follow at threshold 0.30
    c_sub = [r for r in results if r["regime"] != "ranging"]
    c_wins = sum(1 for r in c_sub if trend_won(r))
    c_losses = len(c_sub) - c_wins
    if c_sub:
        print(f"  Trend-follow (t=0.30, skip ranging): {c_wins}/{len(c_sub)} = "
              f"{c_wins / len(c_sub) * 100:.1f}% WR | "
              f"{len(c_sub)} trades ({len(c_sub) * 100 // len(results)}% of windows) | "
              f"PnL=${sim_pnl(c_wins, c_losses):+.2f}")

    # D: Trend-follow at strength >= 0.40
    d_sub = [r for r in results if abs(r["strength"]) >= 0.40]
    d_wins = sum(1 for r in d_sub if trend_won(r))
    d_losses = len(d_sub) - d_wins
    if d_sub:
        print(f"  Trend-follow (str>=0.40, skip rest): {d_wins}/{len(d_sub)} = "
              f"{d_wins / len(d_sub) * 100:.1f}% WR | "
              f"{len(d_sub)} trades ({len(d_sub) * 100 // len(results)}% of windows) | "
              f"PnL=${sim_pnl(d_wins, d_losses):+.2f}")

    # Counter-trend at strength >= 0.40 (what model does)
    ct_wins = d_losses  # counter-trend wins = trend losses
    ct_losses = d_wins
    if d_sub:
        print(f"  Counter-trend (str>=0.40):            {ct_wins}/{len(d_sub)} = "
              f"{ct_wins / len(d_sub) * 100:.1f}% WR | "
              f"PnL=${sim_pnl(ct_wins, ct_losses):+.2f}")

    # Flip delta
    if d_sub:
        delta = sim_pnl(d_wins, d_losses) - sim_pnl(ct_wins, ct_losses)
        print(f"\n  FLIP DELTA (trend vs counter at str>=0.40): ${delta:+.2f}")
        print(f"  Per trade: ${delta / len(d_sub):+.3f}")

    # Ranging
    ranging = [r for r in results if abs(r["strength"]) < 0.30]
    r_up = sum(1 for r in ranging if r["went_up"])
    print(f"\n  Ranging (|s|<0.30): {len(ranging)} windows | "
          f"UP wins {r_up}/{len(ranging)} ({r_up / len(ranging) * 100:.1f}%) — no directional edge")

    # ================================================================
    # REGIME DISTRIBUTION
    # ================================================================
    print("\n" + "=" * 65)
    print("REGIME DISTRIBUTION")
    print("=" * 65)
    for regime in ["trending_up", "trending_down", "ranging"]:
        subset = [r for r in results if r["regime"] == regime]
        if not subset:
            continue
        up = sum(1 for r in subset if r["went_up"])
        pct = len(subset) * 100 / len(results)
        print(f"  {regime:15s}: {len(subset):4d} windows ({pct:.0f}%) | "
              f"UP wins: {up}/{len(subset)} ({up / len(subset) * 100:.1f}%)")

    # ================================================================
    # STRENGTH BUCKET ANALYSIS
    # ================================================================
    print("\n" + "=" * 65)
    print("WIN RATE BY REGIME STRENGTH (betting WITH trend)")
    print("=" * 65)
    buckets = [
        ("Ranging (|s|<0.30)", 0.00, 0.30),
        ("Weak trend (0.30-0.40)", 0.30, 0.40),
        ("Moderate (0.40-0.50)", 0.40, 0.50),
        ("Strong (0.50-0.70)", 0.50, 0.70),
        ("Very strong (>=0.70)", 0.70, 2.0),
    ]
    for label, lo, hi in buckets:
        subset = [r for r in results if lo <= abs(r["strength"]) < hi]
        if not subset:
            print(f"  {label:30s}: 0 windows")
            continue
        wins = sum(1 for r in subset if trend_won(r))
        pnl = sim_pnl(wins, len(subset) - wins)
        print(f"  {label:30s}: {wins}/{len(subset)} = {wins / len(subset) * 100:.1f}% WR "
              f"| {len(subset)} windows | PnL=${pnl:+.2f}")

    # ================================================================
    # HOURLY BREAKDOWN (trend-follow, str>=0.40)
    # ================================================================
    print("\n" + "=" * 65)
    print("TREND-FOLLOW WR BY HOUR (str>=0.40)")
    print("=" * 65)
    hour_data = defaultdict(list)
    for r in d_sub:
        dt = datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc)
        hour_data[dt.hour].append(trend_won(r))

    for h in sorted(hour_data):
        ws = hour_data[h]
        wr = sum(ws) / len(ws) * 100
        marker = " ***" if wr >= 55 else (" --" if wr < 45 else "")
        print(f"  {h:02d}:00 UTC: {sum(ws):3d}/{len(ws):3d} = {wr:.0f}%{marker}")

    # ================================================================
    # DAY-OF-WEEK BREAKDOWN
    # ================================================================
    print("\n" + "=" * 65)
    print("TREND-FOLLOW WR BY DAY (str>=0.40)")
    print("=" * 65)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_data = defaultdict(list)
    for r in d_sub:
        dt = datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc)
        day_data[dt.weekday()].append(trend_won(r))

    for d in sorted(day_data):
        ws = day_data[d]
        wr = sum(ws) / len(ws) * 100
        print(f"  {day_names[d]}: {sum(ws):3d}/{len(ws):3d} = {wr:.0f}%")

    # ================================================================
    # CONSECUTIVE TREND WINDOWS
    # ================================================================
    print("\n" + "=" * 65)
    print("TREND STREAK ANALYSIS")
    print("=" * 65)
    streaks = []
    current_streak = 0
    current_dir = None
    for r in results:
        if r["regime"] != "ranging" and abs(r["strength"]) >= 0.40:
            d = "up" if r["strength"] > 0 else "down"
            if d == current_dir:
                current_streak += 1
            else:
                if current_streak > 0:
                    streaks.append(current_streak)
                current_streak = 1
                current_dir = d
        else:
            if current_streak > 0:
                streaks.append(current_streak)
            current_streak = 0
            current_dir = None
    if current_streak > 0:
        streaks.append(current_streak)

    if streaks:
        streaks.sort()
        print(f"  Total trend streaks: {len(streaks)}")
        print(f"  Median streak: {streaks[len(streaks)//2]} windows ({streaks[len(streaks)//2]*5} min)")
        print(f"  Mean streak: {sum(streaks)/len(streaks):.1f} windows")
        print(f"  Max streak: {max(streaks)} windows ({max(streaks)*5} min)")
        print(f"  P90 streak: {streaks[int(len(streaks)*0.9)]} windows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest regime flip on candle data")
    parser.add_argument("--db", default="btc_edge.db", help="Path to SQLite database")
    args = parser.parse_args()
    run_backtest(args.db)
