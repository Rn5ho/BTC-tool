"""Read-only trend-flip analysis script. Run on VPS against btc_edge.db."""
import sqlite3, sys, bisect
import numpy as np
from collections import Counter, defaultdict
import datetime

sys.path.insert(0, ".")
from data.models import Candle
from signals.regime import RegimeDetector

conn = sqlite3.connect("btc_edge.db")
cur = conn.cursor()

cur.execute("""
    SELECT id, timestamp, side, entry_price, outcome, pnl, amount_usdc,
           regime_state, regime_strength, trade_tag
    FROM live_trades
    WHERE outcome IS NOT NULL AND outcome != 'EARLY_EXIT' AND success = 1
    ORDER BY timestamp
""")
trades = cur.fetchall()

cur.execute("SELECT timestamp, open, high, low, close, volume, taker_buy_volume, trades FROM candles ORDER BY timestamp")
all_candles_raw = cur.fetchall()
all_candle_objs = []
for row in all_candles_raw:
    c = Candle(timestamp=row[0], open=row[1], high=row[2], low=row[3],
               close=row[4], volume=row[5], taker_buy_volume=row[6], trades=row[7])
    all_candle_objs.append(c)

all_candle_objs.sort(key=lambda c: c.timestamp)
candle_timestamps = [c.timestamp for c in all_candle_objs]

detector = RegimeDetector(trend_threshold=0.30)

results = []
for trade in trades:
    tid, ts, side, entry_price, outcome, pnl, amount, db_regime, db_strength, tag = trade

    idx = bisect.bisect_right(candle_timestamps, ts)
    if idx < 30:
        regime_state = db_regime or "unknown"
        regime_strength = db_strength or 0.0
    else:
        recent_candles = all_candle_objs[max(0, idx - 30):idx]
        rs = detector.classify(recent_candles)
        regime_state = rs.regime
        regime_strength = rs.strength

    is_counter = ((regime_state == "trending_up" and side == "DOWN") or
                  (regime_state == "trending_down" and side == "UP"))
    is_with = ((regime_state == "trending_up" and side == "UP") or
               (regime_state == "trending_down" and side == "DOWN"))

    results.append({
        "id": tid, "ts": ts, "side": side, "entry_price": entry_price,
        "outcome": outcome, "pnl": pnl, "amount": amount,
        "regime": regime_state, "strength": regime_strength,
        "is_counter": is_counter, "is_with": is_with, "tag": tag
    })

counter_trades = [r for r in results if r["is_counter"]]
with_trades = [r for r in results if r["is_with"]]
ranging_trades = [r for r in results if r["regime"] == "ranging"]

# Summary
total = len(results)
print("OVERALL SUMMARY")
print("=" * 60)
print("Total settled trades (excl early exit): %d" % total)
print()

for regime in ["ranging", "trending_up", "trending_down"]:
    subset = [r for r in results if r["regime"] == regime]
    cnt = len(subset)
    wins = sum(1 for r in subset if r["outcome"] == "WIN")
    pnl_total = sum(r["pnl"] for r in subset if r["pnl"] is not None)
    wr = wins / cnt * 100 if cnt > 0 else 0
    print("  %s: %d trades, WR=%.1f%%, PnL=$%.2f" % (regime, cnt, wr, pnl_total))

print()
ct_wins = sum(1 for r in counter_trades if r["outcome"] == "WIN")
ct_losses = len(counter_trades) - ct_wins
ct_pnl = sum(r["pnl"] for r in counter_trades if r["pnl"] is not None)
print("Counter-trend: %d trades, %d W / %d L, WR=%.1f%%, PnL=$%.2f" % (
    len(counter_trades), ct_wins, ct_losses, ct_wins / len(counter_trades) * 100, ct_pnl))

wt_wins = sum(1 for r in with_trades if r["outcome"] == "WIN")
wt_pnl = sum(r["pnl"] for r in with_trades if r["pnl"] is not None)
print("With-trend:    %d trades, WR=%.1f%%, PnL=$%.2f" % (
    len(with_trades), wt_wins / max(1, len(with_trades)) * 100, wt_pnl))

rng_wins = sum(1 for r in ranging_trades if r["outcome"] == "WIN")
rng_pnl = sum(r["pnl"] for r in ranging_trades if r["pnl"] is not None)
print("Ranging:       %d trades, WR=%.1f%%, PnL=$%.2f" % (
    len(ranging_trades), rng_wins / max(1, len(ranging_trades)) * 100, rng_pnl))


# ============================================================
# ANALYSIS 1
# ============================================================
print()
print("=" * 60)
print("ANALYSIS 1: Entry Price Distribution for Counter-Trend Trades")
print("=" * 60)

flipped_in_range = 0
flipped_out_range = 0
flipped_prices = []
original_prices = []

for r in sorted(counter_trades, key=lambda x: x["entry_price"] or 0):
    ep = r["entry_price"]
    if ep is None:
        continue
    flipped = round(1.0 - ep, 2)
    flipped_prices.append(flipped)
    original_prices.append(ep)
    in_range = 0.25 <= flipped <= 0.65
    if in_range:
        flipped_in_range += 1
    else:
        flipped_out_range += 1

print()
print("Flipped prices in 0.25-0.65 range: %d/%d (%.1f%%)" % (
    flipped_in_range, len(flipped_prices), flipped_in_range / max(1, len(flipped_prices)) * 100))
print("Flipped prices OUT of range:       %d/%d (%.1f%%)" % (
    flipped_out_range, len(flipped_prices), flipped_out_range / max(1, len(flipped_prices)) * 100))

if flipped_prices:
    print()
    print("Original entry stats: min=%.2f, max=%.2f, mean=%.2f, median=%.2f" % (
        min(original_prices), max(original_prices), np.mean(original_prices), np.median(original_prices)))
    print("Flipped price stats:  min=%.2f, max=%.2f, mean=%.2f, median=%.2f" % (
        min(flipped_prices), max(flipped_prices), np.mean(flipped_prices), np.median(flipped_prices)))

    def bucket_of(p):
        if p < 0.25: return "<0.25"
        elif p <= 0.35: return "0.25-0.35"
        elif p <= 0.45: return "0.35-0.45"
        elif p <= 0.55: return "0.45-0.55"
        elif p <= 0.65: return "0.55-0.65"
        else: return ">0.65"

    buckets_orig = defaultdict(int)
    buckets_flip = defaultdict(int)
    for op, fp in zip(original_prices, flipped_prices):
        buckets_orig[bucket_of(op)] += 1
        buckets_flip[bucket_of(fp)] += 1

    bucket_names = ["<0.25", "0.25-0.35", "0.35-0.45", "0.45-0.55", "0.55-0.65", ">0.65"]
    print()
    print("%12s  %8s  %8s" % ("Bucket", "Original", "Flipped"))
    print("-" * 32)
    for name in bucket_names:
        print("%12s  %8d  %8d" % (name, buckets_orig.get(name, 0), buckets_flip.get(name, 0)))


# ============================================================
# ANALYSIS 2: Flip P&L by Strength Bucket
# ============================================================
print()
print("=" * 60)
print("ANALYSIS 2: Flip P&L by Regime Strength Bucket")
print("=" * 60)

strength_buckets = [
    ("0.30-0.40", 0.30, 0.40),
    ("0.40-0.50", 0.40, 0.50),
    ("0.50-0.60", 0.50, 0.60),
    ("0.60-0.80", 0.60, 0.80),
    ("0.80-1.00", 0.80, 1.00),
]

print()
print("Counter-trend trades by |strength| bucket:")
print("%12s %6s %6s %6s %10s %10s" % ("Bucket", "Count", "Wins", "WR%", "PnL", "AvgPnL"))
print("-" * 56)

for bname, lo, hi in strength_buckets:
    subset = [r for r in counter_trades if lo <= abs(r["strength"]) < hi]
    if not subset:
        print("%12s %6d %6s %6s %10s %10s" % (bname, 0, "-", "-", "-", "-"))
        continue

    wins = sum(1 for r in subset if r["outcome"] == "WIN")
    wr = wins / len(subset) * 100
    total_pnl = sum(r["pnl"] for r in subset if r["pnl"] is not None)
    avg_pnl = total_pnl / len(subset)

    print("%12s %6d %6d %5.1f%% %10.2f %10.2f" % (
        bname, len(subset), wins, wr, total_pnl, avg_pnl))

# Detailed flip P&L estimate
print()
print("Detailed flip P&L estimate (counter-trend only):")
total_flip_pnl = 0
total_actual_pnl = 0

for bname, lo, hi in strength_buckets:
    subset = [r for r in counter_trades if lo <= abs(r["strength"]) < hi]
    if not subset:
        continue

    actual_pnl = sum(r["pnl"] for r in subset if r["pnl"] is not None)

    flip_pnl = 0
    for r in subset:
        ep = r["entry_price"]
        if ep is None:
            continue
        flipped_ep = 1.0 - ep
        amt = r["amount"]
        if r["outcome"] == "WIN":
            # Original won -> flip would lose the bet amount
            flip_pnl -= amt
        else:
            # Original lost -> flip would win
            fee_factor = 0.25 * flipped_ep
            tokens = amt / flipped_ep * (1.0 - fee_factor)
            payout = tokens * 1.0
            flip_pnl += (payout - amt)

    total_flip_pnl += flip_pnl
    total_actual_pnl += actual_pnl
    print("  %s: Actual PnL=$%.2f -> Flipped PnL=$%.2f (delta=$%.2f)" % (
        bname, actual_pnl, flip_pnl, flip_pnl - actual_pnl))

print("  TOTAL: Actual=$%.2f -> Flipped=$%.2f (delta=$%.2f)" % (
    total_actual_pnl, total_flip_pnl, total_flip_pnl - total_actual_pnl))

# Selective flip scenarios
print()
print("Selective flip scenarios:")
for threshold in [0.40, 0.50, 0.60]:
    strong = [r for r in counter_trades if abs(r["strength"]) >= threshold]
    weak = [r for r in counter_trades if abs(r["strength"]) < threshold]

    actual_strong_pnl = sum(r["pnl"] for r in strong if r["pnl"] is not None)
    actual_weak_pnl = sum(r["pnl"] for r in weak if r["pnl"] is not None)

    flip_strong_pnl = 0
    for r in strong:
        ep = r["entry_price"]
        if ep is None:
            continue
        flipped_ep = 1.0 - ep
        amt = r["amount"]
        if r["outcome"] == "WIN":
            flip_strong_pnl -= amt
        else:
            fee_factor = 0.25 * flipped_ep
            tokens = amt / flipped_ep * (1.0 - fee_factor)
            flip_strong_pnl += (tokens - amt)

    original_total = actual_strong_pnl + actual_weak_pnl
    flipped_total = flip_strong_pnl + actual_weak_pnl

    strong_wins = sum(1 for r in strong if r["outcome"] == "WIN")
    strong_losses = len(strong) - strong_wins

    print("  Flip only |str|>=%.2f: %d trades (%dW/%dL), PnL: $%.2f -> $%.2f (delta=$%.2f)" % (
        threshold, len(strong), strong_wins, strong_losses,
        original_total, flipped_total, flipped_total - original_total))


# ============================================================
# ANALYSIS 3: Time-of-Day Analysis
# ============================================================
print()
print("=" * 60)
print("ANALYSIS 3: Counter-Trend Trades by Hour (UTC)")
print("=" * 60)

hour_stats = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0, "flip_pnl": 0.0})
for r in counter_trades:
    dt = datetime.datetime.fromtimestamp(r["ts"] / 1000, tz=datetime.timezone.utc)
    hour = dt.hour
    hour_stats[hour]["count"] += 1
    if r["outcome"] == "WIN":
        hour_stats[hour]["wins"] += 1
    if r["pnl"] is not None:
        hour_stats[hour]["pnl"] += r["pnl"]

    ep = r["entry_price"]
    if ep:
        flipped_ep = 1.0 - ep
        amt = r["amount"]
        if r["outcome"] == "WIN":
            hour_stats[hour]["flip_pnl"] -= amt
        else:
            fee_factor = 0.25 * flipped_ep
            tokens = amt / flipped_ep * (1.0 - fee_factor)
            hour_stats[hour]["flip_pnl"] += (tokens - amt)

print()
print("%5s %6s %6s %6s %10s %10s %10s" % ("Hour", "Count", "Wins", "WR%", "ActualPnL", "FlipPnL", "Delta"))
print("-" * 62)

for hour in range(24):
    s = hour_stats.get(hour)
    if s is None or s["count"] == 0:
        continue
    wr = s["wins"] / s["count"] * 100
    delta = s["flip_pnl"] - s["pnl"]
    print("%5d %6d %6d %5.1f%% %10.2f %10.2f %10.2f" % (
        hour, s["count"], s["wins"], wr, s["pnl"], s["flip_pnl"], delta))

# Also show ALL trades by hour for context
print()
print("ALL trades by hour (for context):")
all_hour_stats = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
for r in results:
    dt = datetime.datetime.fromtimestamp(r["ts"] / 1000, tz=datetime.timezone.utc)
    hour = dt.hour
    all_hour_stats[hour]["count"] += 1
    if r["outcome"] == "WIN":
        all_hour_stats[hour]["wins"] += 1
    if r["pnl"] is not None:
        all_hour_stats[hour]["pnl"] += r["pnl"]

print("%5s %6s %6s %6s %10s" % ("Hour", "Count", "Wins", "WR%", "PnL"))
print("-" * 40)
for hour in range(24):
    s = all_hour_stats.get(hour)
    if s is None or s["count"] == 0:
        continue
    wr = s["wins"] / s["count"] * 100
    print("%5d %6d %6d %5.1f%% %10.2f" % (hour, s["count"], s["wins"], wr, s["pnl"]))


# ============================================================
# ANALYSIS 4: Consecutive Windows
# ============================================================
print()
print("=" * 60)
print("ANALYSIS 4: Consecutive Trending Windows")
print("=" * 60)

print()
print("Computing regime for all 5-minute windows...")

min_ts = candle_timestamps[0]
max_ts = candle_timestamps[-1]
window_start = min_ts - (min_ts % 300000)

window_regimes = []
ts = window_start
while ts <= max_ts:
    idx = bisect.bisect_right(candle_timestamps, ts)
    if idx >= 30:
        recent = all_candle_objs[max(0, idx - 30):idx]
        rs = detector.classify(recent)
        window_regimes.append((ts, rs.regime, rs.strength))
    ts += 300000

print("Total 5-min windows analyzed: %d" % len(window_regimes))

regime_dist = Counter(wr[1] for wr in window_regimes)
print()
print("Regime distribution across all windows:")
for regime, count in sorted(regime_dist.items()):
    pct = count / len(window_regimes) * 100
    print("  %s: %d (%.1f%%)" % (regime, count, pct))

# Find consecutive streaks
streaks = []
if window_regimes:
    current_regime = window_regimes[0][1]
    streak_start = 0
    for i in range(1, len(window_regimes)):
        if window_regimes[i][1] != current_regime:
            if current_regime in ("trending_up", "trending_down"):
                streak_len = i - streak_start
                streaks.append((current_regime, streak_len, window_regimes[streak_start][0]))
            current_regime = window_regimes[i][1]
            streak_start = i
    if current_regime in ("trending_up", "trending_down"):
        streak_len = len(window_regimes) - streak_start
        streaks.append((current_regime, streak_len, window_regimes[streak_start][0]))

print()
print("Trending streaks (consecutive 5-min windows in same trend):")
print("Total trending streaks found: %d" % len(streaks))

if streaks:
    streak_lengths = [s[1] for s in streaks]
    print()
    print("Streak length stats:")
    print("  Min: %d windows (%d min)" % (min(streak_lengths), min(streak_lengths) * 5))
    print("  Max: %d windows (%d min)" % (max(streak_lengths), max(streak_lengths) * 5))
    print("  Mean: %.1f windows (%.0f min)" % (np.mean(streak_lengths), np.mean(streak_lengths) * 5))
    print("  Median: %.0f windows (%.0f min)" % (np.median(streak_lengths), np.median(streak_lengths) * 5))

    len_dist = defaultdict(int)
    for s in streaks:
        if s[1] <= 3:
            len_dist["1-3 (5-15min)"] += 1
        elif s[1] <= 6:
            len_dist["4-6 (20-30min)"] += 1
        elif s[1] <= 12:
            len_dist["7-12 (35-60min)"] += 1
        elif s[1] <= 24:
            len_dist["13-24 (65-120min)"] += 1
        else:
            len_dist["25+ (>2hr)"] += 1

    print()
    print("Streak length distribution:")
    for name in ["1-3 (5-15min)", "4-6 (20-30min)", "7-12 (35-60min)", "13-24 (65-120min)", "25+ (>2hr)"]:
        cnt = len_dist.get(name, 0)
        bar = "#" * cnt
        print("  %20s: %3d %s" % (name, cnt, bar))

    # Up vs Down streaks
    up_streaks = [s for s in streaks if s[0] == "trending_up"]
    down_streaks = [s for s in streaks if s[0] == "trending_down"]
    print()
    print("By direction:")
    if up_streaks:
        up_lens = [s[1] for s in up_streaks]
        print("  trending_up:   %d streaks, avg=%.1f windows, max=%d" % (len(up_streaks), np.mean(up_lens), max(up_lens)))
    if down_streaks:
        down_lens = [s[1] for s in down_streaks]
        print("  trending_down: %d streaks, avg=%.1f windows, max=%d" % (len(down_streaks), np.mean(down_lens), max(down_lens)))

    # Top 10 longest
    top_streaks = sorted(streaks, key=lambda s: s[1], reverse=True)[:10]
    print()
    print("Top 10 longest trending streaks:")
    for s in top_streaks:
        dt = datetime.datetime.fromtimestamp(s[2] / 1000, tz=datetime.timezone.utc)
        print("  %s: %d windows (%d min) starting %s" % (s[0], s[1], s[1] * 5, dt.strftime("%Y-%m-%d %H:%M")))

    # How many trades would occur during long streaks?
    print()
    print("Trades during top trending streaks:")
    for s in sorted(streaks, key=lambda s: s[1], reverse=True)[:5]:
        streak_start_ts = s[2]
        streak_end_ts = s[2] + s[1] * 300000
        trades_in_streak = [r for r in counter_trades
                            if streak_start_ts <= r["ts"] <= streak_end_ts]
        if trades_in_streak:
            wins_in = sum(1 for r in trades_in_streak if r["outcome"] == "WIN")
            pnl_in = sum(r["pnl"] for r in trades_in_streak if r["pnl"] is not None)
            dt = datetime.datetime.fromtimestamp(s[2] / 1000, tz=datetime.timezone.utc)
            print("  %s %s (%d windows): %d counter-trend trades, %dW/%dL, PnL=$%.2f" % (
                dt.strftime("%m-%d %H:%M"), s[0], s[1],
                len(trades_in_streak), wins_in, len(trades_in_streak) - wins_in, pnl_in))

conn.close()
print()
print("Analysis complete.")
