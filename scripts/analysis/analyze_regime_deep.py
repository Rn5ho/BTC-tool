"""Deep analysis: what actually makes the model lose?

Examines volatility, move magnitude, volume spikes, consecutive candles,
and our actual losing streaks to understand when the model's edge disappears.
"""

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, ".")
from data.models import Candle
from signals.regime import RegimeDetector


def main():
    conn = sqlite3.connect("btc_edge.db")
    rows = conn.execute(
        "SELECT timestamp, open, high, low, close, volume, taker_buy_volume, trades "
        "FROM candles ORDER BY timestamp"
    ).fetchall()
    candles = [
        Candle(timestamp=r[0], open=r[1], high=r[2], low=r[3],
               close=r[4], volume=r[5], taker_buy_volume=r[6], trades=r[7])
        for r in rows
    ]
    conn.row_factory = sqlite3.Row
    live_trades = [dict(r) for r in conn.execute(
        "SELECT * FROM live_trades WHERE success=1 AND outcome IS NOT NULL AND pnl IS NOT NULL ORDER BY timestamp"
    ).fetchall()]
    conn.close()

    print(f"Candles: {len(candles)} | Live trades: {len(live_trades)}")

    detector = RegimeDetector(trend_threshold=0.30)
    windows = []

    for i in range(60, len(candles) - 5):
        minute = (candles[i].timestamp // 60000) % 60
        if minute % 5 != 0:
            continue
        t_end = candles[i + 4].timestamp
        if (t_end - candles[i].timestamp) > 360000:
            continue

        lookback = candles[max(0, i - 50):i]
        if len(lookback) < 30:
            continue

        btc_open = candles[i].open
        btc_close = candles[i + 4].close
        went_up = btc_close > btc_open
        move_pct = (btc_close - btc_open) / btc_open * 100  # signed

        closes = [c.close for c in lookback[-20:]]
        returns = [(closes[j] - closes[j - 1]) / closes[j - 1] for j in range(1, len(closes))]
        volatility = np.std(returns) if returns else 0

        # Recent move magnitude (last 5 candles before window)
        recent_move = abs(lookback[-1].close - lookback[-5].close) / lookback[-5].close
        recent_dir = 1 if lookback[-1].close > lookback[-5].close else -1

        # Volume
        volumes = [c.volume for c in lookback[-20:]]
        avg_vol = np.mean(volumes) if volumes else 1
        vol_ratio = lookback[-1].volume / avg_vol if avg_vol > 0 else 1

        # Consecutive same-direction candles
        consec = 0
        last_dir = lookback[-1].close > lookback[-1].open
        for j in range(len(lookback) - 2, max(len(lookback) - 10, -1), -1):
            if (lookback[j].close > lookback[j].open) == last_dir:
                consec += 1
            else:
                break

        state = detector.classify(lookback)

        windows.append({
            "ts": candles[i].timestamp,
            "went_up": went_up,
            "move_pct": move_pct,
            "volatility": volatility,
            "recent_move": recent_move,
            "recent_dir": recent_dir,
            "vol_ratio": vol_ratio,
            "regime": state.regime,
            "strength": state.strength,
            "abs_strength": abs(state.strength),
            "consec_candles": consec,
        })

    print(f"Analyzed {len(windows)} windows\n")

    # ================================================================
    print("=" * 65)
    print("1. BY VOLATILITY (std of 1-min returns in lookback)")
    print("=" * 65)
    vols = [w["volatility"] for w in windows]
    p25, p50, p75, p90 = np.percentile(vols, [25, 50, 75, 90])
    vol_buckets = [
        ("Low vol (0-25th pct)", 0, p25),
        ("Med vol (25-50th)", p25, p50),
        ("High vol (50-75th)", p50, p75),
        ("Very high (75-90th)", p75, p90),
        ("Extreme (>90th)", p90, 999),
    ]
    for label, lo, hi in vol_buckets:
        subset = [w for w in windows if lo <= w["volatility"] < hi]
        if not subset:
            continue
        up = sum(1 for w in subset if w["went_up"])
        majority = max(up, len(subset) - up)
        # Check if mean-reversion works here (bet against recent direction)
        mr_wins = sum(1 for w in subset if
                      (w["recent_dir"] > 0 and not w["went_up"]) or
                      (w["recent_dir"] < 0 and w["went_up"]))
        mr_wr = mr_wins / len(subset) * 100
        print(f"  {label:28s}: {len(subset):4d} win | "
              f"mean-rev WR={mr_wr:.1f}% | UP rate={up / len(subset) * 100:.1f}%")

    # ================================================================
    print("\n" + "=" * 65)
    print("2. DOES DIRECTION REVERSE AFTER LARGE MOVES?")
    print("=" * 65)
    move_buckets = [
        ("Tiny (<0.05%)", 0, 0.0005),
        ("Small (0.05-0.10%)", 0.0005, 0.001),
        ("Medium (0.10-0.20%)", 0.001, 0.002),
        ("Large (0.20-0.40%)", 0.002, 0.004),
        ("Very large (>0.40%)", 0.004, 999),
    ]
    for label, lo, hi in move_buckets:
        up_movers = [w for w in windows if lo <= w["recent_move"] < hi and w["recent_dir"] > 0]
        dn_movers = [w for w in windows if lo <= w["recent_move"] < hi and w["recent_dir"] < 0]

        if up_movers:
            rev = sum(1 for w in up_movers if not w["went_up"])
            print(f"  {label:22s} after UP:   {rev}/{len(up_movers)} reversed "
                  f"({rev / len(up_movers) * 100:.1f}%)")
        if dn_movers:
            rev = sum(1 for w in dn_movers if w["went_up"])
            print(f"  {label:22s} after DOWN: {rev}/{len(dn_movers)} reversed "
                  f"({rev / len(dn_movers) * 100:.1f}%)")

    # ================================================================
    print("\n" + "=" * 65)
    print("3. CONSECUTIVE SAME-DIRECTION CANDLES")
    print("=" * 65)
    for n in range(0, 9):
        subset = [w for w in windows if w["consec_candles"] == n]
        if len(subset) < 10:
            continue
        # After N bullish candles in a row, does it continue or reverse?
        mr_wins = sum(1 for w in subset if
                      (w["recent_dir"] > 0 and not w["went_up"]) or
                      (w["recent_dir"] < 0 and w["went_up"]))
        print(f"  {n} consecutive: {len(subset):4d} windows | "
              f"mean-rev WR={mr_wins / len(subset) * 100:.1f}%")

    # ================================================================
    print("\n" + "=" * 65)
    print("4. VOLUME SPIKE + TREND = ?")
    print("=" * 65)
    for vol_label, vlo, vhi in [("Normal (<1.5x)", 0, 1.5),
                                 ("Elevated (1.5-2.5x)", 1.5, 2.5),
                                 ("High (2.5-4x)", 2.5, 4),
                                 ("Spike (>4x)", 4, 999)]:
        subset = [w for w in windows if vlo <= w["vol_ratio"] < vhi]
        if not subset:
            continue
        trending = [w for w in subset if w["abs_strength"] >= 0.40]
        ranging = [w for w in subset if w["abs_strength"] < 0.30]
        if trending:
            mr = sum(1 for w in trending if
                     (w["strength"] > 0 and not w["went_up"]) or
                     (w["strength"] < 0 and w["went_up"]))
            tf = len(trending) - mr
            print(f"  {vol_label:22s} TRENDING: {mr}/{len(trending)} "
                  f"mean-rev ({mr / len(trending) * 100:.0f}%) vs "
                  f"{tf}/{len(trending)} trend-follow ({tf / len(trending) * 100:.0f}%)")
        if ranging:
            up = sum(1 for w in ranging if w["went_up"])
            print(f"  {vol_label:22s} RANGING:  UP {up}/{len(ranging)} "
                  f"({up / len(ranging) * 100:.0f}%) — coin flip")

    # ================================================================
    print("\n" + "=" * 65)
    print("5. OUR ACTUAL LOSING STREAKS (live trades)")
    print("=" * 65)
    streak = 0
    streak_trades = []
    all_streaks = []
    for t in live_trades:
        if t["outcome"] == "LOSS":
            streak += 1
            streak_trades.append(t)
        else:
            if streak >= 3:
                all_streaks.append(list(streak_trades))
            streak = 0
            streak_trades = []
    if streak >= 3:
        all_streaks.append(list(streak_trades))

    all_streaks.sort(key=len, reverse=True)
    print(f"Found {len(all_streaks)} losing streaks of 3+\n")

    for s in all_streaks[:5]:
        pnl = sum(t["pnl"] for t in s)
        dt = datetime.fromtimestamp(s[0]["timestamp"] / 1000, tz=timezone.utc)
        sides = [t["side"] for t in s]
        up_count = sides.count("UP")
        dn_count = sides.count("DOWN")
        tags = set(t.get("trade_tag") or "taker" for t in s)
        # Check regime at the time
        regime_states = set(t.get("regime_state") or "?" for t in s)
        print(f"  {len(s):2d} losses at {dt:%m-%d %H:%M} | PnL=${pnl:+.2f} | "
              f"UP={up_count} DN={dn_count} | regime={regime_states} | tags={tags}")

    # ================================================================
    print("\n" + "=" * 65)
    print("6. REGIME DETECTOR: EARLY vs LATE in a trend")
    print("=" * 65)
    # Are the first few windows of a trend different from late windows?
    # Track trend "age" — how many consecutive windows has this regime been active?
    prev_regime = None
    trend_age = 0
    age_data = []
    for w in windows:
        if w["abs_strength"] >= 0.40:
            regime_dir = "up" if w["strength"] > 0 else "down"
            if regime_dir == prev_regime:
                trend_age += 1
            else:
                trend_age = 0
                prev_regime = regime_dir
            tf_won = (w["strength"] > 0 and w["went_up"]) or (w["strength"] < 0 and not w["went_up"])
            age_data.append({"age": trend_age, "tf_won": tf_won, "strength": w["abs_strength"]})
        else:
            prev_regime = None
            trend_age = 0

    age_buckets = [
        ("First window (age=0)", 0, 1),
        ("Early (age 1-2)", 1, 3),
        ("Mid (age 3-5)", 3, 6),
        ("Late (age 6+)", 6, 999),
    ]
    for label, lo, hi in age_buckets:
        subset = [a for a in age_data if lo <= a["age"] < hi]
        if not subset:
            continue
        tf_wins = sum(1 for a in subset if a["tf_won"])
        mr_wins = len(subset) - tf_wins
        print(f"  {label:25s}: {len(subset):4d} windows | "
              f"trend-follow={tf_wins / len(subset) * 100:.1f}% | "
              f"mean-rev={mr_wins / len(subset) * 100:.1f}%")

    # ================================================================
    print("\n" + "=" * 65)
    print("7. COMBINED: VOLATILITY x REGIME x DIRECTION")
    print("=" * 65)
    # The key question: is there ANY combination where trend-following wins?
    med_vol = np.median(vols)
    combos = [
        ("Low vol + Trending", lambda w: w["volatility"] < med_vol and w["abs_strength"] >= 0.40),
        ("High vol + Trending", lambda w: w["volatility"] >= med_vol and w["abs_strength"] >= 0.40),
        ("Low vol + Ranging", lambda w: w["volatility"] < med_vol and w["abs_strength"] < 0.30),
        ("High vol + Ranging", lambda w: w["volatility"] >= med_vol and w["abs_strength"] < 0.30),
        ("Very high vol + Strong trend", lambda w: w["volatility"] > np.percentile(vols, 75) and w["abs_strength"] >= 0.60),
        ("Low vol + Weak trend", lambda w: w["volatility"] < med_vol and 0.30 <= w["abs_strength"] < 0.45),
    ]
    for label, filt in combos:
        subset = [w for w in windows if filt(w)]
        if len(subset) < 20:
            print(f"  {label:35s}: {len(subset):4d} windows (too few)")
            continue
        tf = sum(1 for w in subset if
                 (w["strength"] > 0 and w["went_up"]) or
                 (w["strength"] < 0 and not w["went_up"]))
        mr = len(subset) - tf
        print(f"  {label:35s}: {len(subset):4d} windows | "
              f"trend={tf / len(subset) * 100:.1f}% | "
              f"mean-rev={mr / len(subset) * 100:.1f}%")

    # ================================================================
    print("\n" + "=" * 65)
    print("8. THE REAL QUESTION: When should we NOT trade?")
    print("=" * 65)
    # For each window, compute a simple "danger score"
    # and see if high danger = lower WR for mean-reversion
    for label, filt in [
        ("All windows", lambda w: True),
        ("High vol + strong trend", lambda w: w["volatility"] > np.percentile(vols, 75) and w["abs_strength"] >= 0.50),
        ("Low vol + ranging", lambda w: w["volatility"] < med_vol and w["abs_strength"] < 0.25),
        ("Normal conditions", lambda w: p25 <= w["volatility"] < p75 and w["abs_strength"] < 0.40),
        ("Vol spike (>2x) + any trend", lambda w: w["vol_ratio"] > 2.0 and w["abs_strength"] >= 0.30),
        ("Calm (vol <1x) + ranging", lambda w: w["vol_ratio"] < 1.0 and w["abs_strength"] < 0.30),
    ]:
        subset = [w for w in windows if filt(w)]
        if len(subset) < 20:
            continue
        mr_wins = sum(1 for w in subset if
                      (w["recent_dir"] > 0 and not w["went_up"]) or
                      (w["recent_dir"] < 0 and w["went_up"]))
        # Also trend-follow WR for trending subset
        trending_sub = [w for w in subset if w["abs_strength"] >= 0.40]
        if trending_sub:
            tf = sum(1 for w in trending_sub if
                     (w["strength"] > 0 and w["went_up"]) or
                     (w["strength"] < 0 and not w["went_up"]))
            trend_info = f"| trend-follow={tf / len(trending_sub) * 100:.0f}% ({len(trending_sub)})"
        else:
            trend_info = ""
        print(f"  {label:35s}: {len(subset):4d} | "
              f"mean-rev={mr_wins / len(subset) * 100:.1f}% {trend_info}")


if __name__ == "__main__":
    main()
