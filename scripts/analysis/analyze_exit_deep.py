"""Deep dive into early exit opportunities.

Angles: bid trajectory, time-to-peak, profit targets, stop-losses,
time-based decay thresholds, ROI-based exits, bid oscillation analysis.
"""
import sqlite3
import sys
import numpy as np
from datetime import datetime, timezone

sys.path.insert(0, ".")


def main():
    conn = sqlite3.connect("btc_edge.db")
    conn.row_factory = sqlite3.Row

    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM live_trades WHERE success=1 AND outcome IS NOT NULL AND pnl IS NOT NULL ORDER BY timestamp"
    ).fetchall()]

    enriched = []
    for trade in trades:
        if trade["outcome"] == "EARLY_EXIT":
            continue
        slug = trade["market_slug"]
        side = trade["side"]
        entry_price = trade.get("entry_price", 0)
        if entry_price <= 0:
            continue
        ts = trade["timestamp"]
        ts_sec = ts // 1000

        snapshots = conn.execute(
            "SELECT * FROM market_snapshots WHERE slug = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (slug, ts_sec - 30, ts_sec + 330)
        ).fetchall()

        if len(snapshots) < 5:
            continue

        bids = []
        for snap in snapshots:
            snap = dict(snap)
            t = snap["timestamp"] - ts_sec
            if side == "UP":
                bid = snap.get("up_best_bid") or 0
            else:
                bid = snap.get("down_best_bid") or 0
            if bid:
                bids.append((t, bid))

        if len(bids) < 5:
            continue

        bid_values = [b for _, b in bids]
        max_bid = max(bid_values)
        min_bid = min(bid_values)
        max_bid_time = [t for t, b in bids if b == max_bid][0]

        enriched.append({
            "outcome": trade["outcome"],
            "entry_price": entry_price,
            "amount": trade["amount_usdc"],
            "actual_pnl": trade["pnl"],
            "side": side,
            "tag": trade.get("trade_tag") or "taker",
            "bids": bids,
            "max_bid": max_bid,
            "min_bid": min_bid,
            "max_bid_time": max_bid_time,
            "bid_std": np.std(bid_values),
        })

    conn.close()

    losses = [r for r in enriched if r["outcome"] == "LOSS"]
    wins = [r for r in enriched if r["outcome"] == "WIN"]
    print(f"Enriched trades: {len(enriched)} ({len(wins)}W / {len(losses)}L)")

    # ================================================================
    print("\n" + "=" * 70)
    print("1. BID TRAJECTORY AT PEAK: Rising or falling?")
    print("=" * 70)
    for r in enriched:
        peak_idx = None
        for i, (t, b) in enumerate(r["bids"]):
            if b == r["max_bid"]:
                peak_idx = i
                break
        if peak_idx is None:
            r["peak_trajectory"] = "unknown"
            continue
        if peak_idx >= 2:
            before = r["bids"][peak_idx - 2][1]
            r["pre_peak_delta"] = r["max_bid"] - before
        else:
            r["pre_peak_delta"] = 0
        if peak_idx < len(r["bids"]) - 2:
            after = r["bids"][peak_idx + 2][1]
            r["post_peak_delta"] = after - r["max_bid"]
        else:
            r["post_peak_delta"] = 0

        if r["pre_peak_delta"] > 0.02:
            r["peak_trajectory"] = "rising_fast"
        elif r["pre_peak_delta"] > 0:
            r["peak_trajectory"] = "rising_slow"
        elif r["pre_peak_delta"] < -0.02:
            r["peak_trajectory"] = "falling_fast"
        else:
            r["peak_trajectory"] = "falling_slow"

    for traj in ["rising_fast", "rising_slow", "falling_slow", "falling_fast"]:
        subset = [r for r in losses if r.get("peak_trajectory") == traj]
        if not subset:
            continue
        avg_max = np.mean([r["max_bid"] for r in subset])
        avg_post = np.mean([r["post_peak_delta"] for r in subset])
        print(f"  {traj:15s}: {len(subset):3d} losing trades | "
              f"avg max bid={avg_max:.3f} | avg drop after peak={avg_post:+.3f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("2. TIME-TO-PEAK: How quickly do profitable spikes happen?")
    print("=" * 70)
    peaked_losses = [r for r in losses if r["max_bid"] >= 0.60]
    print(f"  Losses that peaked >= 0.60: {len(peaked_losses)}")
    for label, lo, hi in [("Peak in 0-30s", 0, 30), ("30-60s", 30, 60),
                           ("60-120s", 60, 120), ("120-180s", 120, 180),
                           ("180-300s", 180, 300)]:
        subset = [r for r in peaked_losses if lo <= r["max_bid_time"] < hi]
        if subset:
            avg_peak = np.mean([r["max_bid"] for r in subset])
            print(f"    {label:15s}: {len(subset):3d} trades | avg peak={avg_peak:.3f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("3. WINNERS WE CUT SHORT: How much profit do we give up?")
    print("=" * 70)
    for threshold in [0.70, 0.80, 0.85, 0.90, 0.95]:
        triggered = [r for r in wins if r["max_bid"] >= threshold]
        if not triggered:
            continue
        total_exit = 0
        total_hold = 0
        for r in triggered:
            fee = 0.015
            tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
            exit_pnl = tokens * threshold * (1 - fee) - r["amount"]
            total_exit += exit_pnl
            total_hold += r["actual_pnl"]
        lost = total_hold - total_exit
        print(f"  t={threshold:.2f}: {len(triggered):3d}/{len(wins)} wins triggered | "
              f"exit=${total_exit:+.2f} vs hold=${total_hold:+.2f} | "
              f"gave up ${lost:.2f} (${lost / len(triggered):.2f}/trade)")

    # ================================================================
    print("\n" + "=" * 70)
    print("4. STOP-LOSS: Exit when bid drops below entry - X%")
    print("=" * 70)
    for stop_pct in [0.10, 0.15, 0.20, 0.25, 0.30]:
        stopped = 0
        total_with = 0
        total_without = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            stop_price = r["entry_price"] * (1 - stop_pct)
            hit_stop = any(b <= stop_price for t, b in r["bids"] if t > 10)
            if hit_stop:
                stopped += 1
                fee = 0.015
                tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
                total_with += tokens * stop_price * (1 - fee) - r["amount"]
            else:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        print(f"  Stop at entry-{stop_pct * 100:.0f}%: {stopped:3d} stopped | "
              f"PnL=${total_with:+.2f} vs ${total_without:+.2f} | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("5. TIERED EXIT + TIME WINDOW")
    print("=" * 70)
    tiers = {(0.25, 0.35): 0.70, (0.35, 0.50): 0.85, (0.50, 0.65): 0.95}
    for name, time_limit in [("Moderate anytime", 999),
                              ("Moderate first 180s", 180),
                              ("Moderate first 120s", 120),
                              ("Moderate last 180s only", -180)]:
        total_with = 0
        total_without = 0
        exits = 0
        rescued = 0
        cut_short = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            ep = r["entry_price"]
            thresh = None
            for (lo, hi), t in tiers.items():
                if lo <= ep < hi:
                    thresh = t
                    break
            if thresh is None:
                total_with += r["actual_pnl"]
                continue
            if time_limit > 0:
                hit = any(b >= thresh for t_sec, b in r["bids"] if 10 < t_sec <= time_limit)
            else:
                hit = any(b >= thresh for t_sec, b in r["bids"] if t_sec >= (300 + time_limit))
            if hit:
                exits += 1
                fee = 0.015
                tokens = (r["amount"] / ep) * (1 - fee)
                total_with += tokens * thresh * (1 - fee) - r["amount"]
                if r["outcome"] == "LOSS":
                    rescued += 1
                elif r["outcome"] == "WIN":
                    cut_short += 1
            else:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        print(f"  {name:30s}: exits={exits:3d} ({rescued}L/{cut_short}W) | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("6. ROI-BASED EXIT: Exit at X% return on investment")
    print("=" * 70)
    for roi_target in [0.20, 0.30, 0.50, 0.75, 1.00, 1.50, 2.00]:
        total_with = 0
        total_without = 0
        exits = 0
        rescued = 0
        cut_short = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            fee = 0.015
            tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
            target_bid = (r["amount"] * (1 + roi_target)) / (tokens * (1 - fee))
            hit = any(b >= target_bid for _, b in r["bids"])
            if hit:
                exits += 1
                total_with += tokens * target_bid * (1 - fee) - r["amount"]
                if r["outcome"] == "LOSS":
                    rescued += 1
                elif r["outcome"] == "WIN":
                    cut_short += 1
            else:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        print(f"  ROI>={roi_target * 100:5.0f}%: exits={exits:3d} ({rescued}L/{cut_short}W) | "
              f"PnL=${total_with:+.2f} vs ${total_without:+.2f} | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("7. BID OSCILLATION: Do bids bounce around or trend?")
    print("=" * 70)
    for r in enriched:
        bid_vals = [b for _, b in r["bids"]]
        r["bid_range"] = max(bid_vals) - min(bid_vals)
        changes = 0
        for i in range(2, len(bid_vals)):
            if (bid_vals[i] - bid_vals[i - 1]) * (bid_vals[i - 1] - bid_vals[i - 2]) < 0:
                changes += 1
        r["bid_oscillations"] = changes / max(len(bid_vals) - 2, 1)

    for label, lo, hi in [("Low oscillation", 0, 0.3), ("Medium", 0.3, 0.5), ("High", 0.5, 1.0)]:
        subset = [r for r in enriched if lo <= r["bid_oscillations"] < hi]
        if not subset:
            continue
        w = sum(1 for r in subset if r["outcome"] == "WIN")
        l = sum(1 for r in subset if r["outcome"] == "LOSS")
        avg_range = np.mean([r["bid_range"] for r in subset])
        print(f"  {label:18s}: {len(subset):3d} trades | W/L={w}/{l} | "
              f"avg bid range={avg_range:.3f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("8. DYNAMIC THRESHOLD: Lower exit target as window progresses")
    print("=" * 70)
    decay_configs = [
        ("No decay (flat 0.90)", lambda t: 0.90),
        ("Linear 0.95->0.75", lambda t: max(0.75, 0.95 - (t / 300) * 0.20)),
        ("Linear 0.90->0.70", lambda t: max(0.70, 0.90 - (t / 300) * 0.20)),
        ("Linear 0.85->0.65", lambda t: max(0.65, 0.85 - (t / 300) * 0.20)),
        ("Step: 0.95 first 2m, 0.80 after", lambda t: 0.95 if t < 120 else 0.80),
        ("Step: 0.90 first 2m, 0.75 after", lambda t: 0.90 if t < 120 else 0.75),
    ]
    for name, threshold_fn in decay_configs:
        total_with = 0
        total_without = 0
        exits = 0
        rescued = 0
        cut_short = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            fee = 0.015
            tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
            exited = False
            for t, b in r["bids"]:
                if t < 10:
                    continue
                thresh = threshold_fn(t)
                if b >= thresh:
                    total_with += tokens * b * (1 - fee) - r["amount"]
                    exits += 1
                    if r["outcome"] == "LOSS":
                        rescued += 1
                    elif r["outcome"] == "WIN":
                        cut_short += 1
                    exited = True
                    break
            if not exited:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        print(f"  {name:35s}: exits={exits:3d} ({rescued}L/{cut_short}W) | "
              f"PnL=${total_with:+.2f} vs hold=${total_without:+.2f} | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("9. COMBINED BEST: ROI-based + entry-price tiering + time decay")
    print("=" * 70)
    # The idea: for cheap entries, lower ROI target. For expensive, higher.
    # Plus: lower threshold as time runs out.
    combined_configs = [
        ("ROI 50% + time decay 0.90->0.70",
         lambda r, t: max(0.70, 0.90 - (t / 300) * 0.20),  # dynamic threshold
         0.50),  # min ROI
        ("Tiered ROI (30%/50%/75% by entry)",
         lambda r, t: 0.0,  # no threshold, pure ROI
         None),  # special handling
        ("ROI 30% after 120s, 75% before",
         lambda r, t: 0.0,
         None),
    ]
    # Tiered ROI by entry price
    print("  Tiered ROI (different ROI target by entry price):")
    for roi_cheap, roi_mid, roi_exp in [(0.30, 0.50, 0.75),
                                         (0.20, 0.40, 0.60),
                                         (0.50, 0.75, 1.00)]:
        total_with = 0
        total_without = 0
        exits = 0
        rescued = 0
        cut_short = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            ep = r["entry_price"]
            if ep < 0.35:
                roi = roi_cheap
            elif ep < 0.50:
                roi = roi_mid
            else:
                roi = roi_exp
            fee = 0.015
            tokens = (r["amount"] / ep) * (1 - fee)
            target_bid = (r["amount"] * (1 + roi)) / (tokens * (1 - fee))
            hit = any(b >= target_bid for _, b in r["bids"])
            if hit:
                exits += 1
                total_with += tokens * target_bid * (1 - fee) - r["amount"]
                if r["outcome"] == "LOSS":
                    rescued += 1
                elif r["outcome"] == "WIN":
                    cut_short += 1
            else:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        label = f"ROI {roi_cheap*100:.0f}%/{roi_mid*100:.0f}%/{roi_exp*100:.0f}% (cheap/mid/exp)"
        print(f"    {label:45s}: exits={exits:3d} ({rescued}L/{cut_short}W) | delta=${delta:+.2f}")

    # Time-aware ROI
    print("\n  Time-aware ROI (lower target as window progresses):")
    for early_roi, late_roi, split_sec in [(0.75, 0.30, 120),
                                            (0.75, 0.30, 180),
                                            (0.50, 0.20, 120),
                                            (1.00, 0.50, 180)]:
        total_with = 0
        total_without = 0
        exits = 0
        rescued = 0
        cut_short = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            fee = 0.015
            tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
            exited = False
            for t, b in r["bids"]:
                if t < 10:
                    continue
                roi = early_roi if t < split_sec else late_roi
                target_bid = (r["amount"] * (1 + roi)) / (tokens * (1 - fee))
                if b >= target_bid:
                    total_with += tokens * b * (1 - fee) - r["amount"]
                    exits += 1
                    if r["outcome"] == "LOSS":
                        rescued += 1
                    elif r["outcome"] == "WIN":
                        cut_short += 1
                    exited = True
                    break
            if not exited:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        label = f"ROI {early_roi*100:.0f}%<{split_sec}s, {late_roi*100:.0f}% after"
        print(f"    {label:35s}: exits={exits:3d} ({rescued}L/{cut_short}W) | "
              f"PnL=${total_with:+.2f} vs ${total_without:+.2f} | delta=${delta:+.2f}")


if __name__ == "__main__":
    main()
