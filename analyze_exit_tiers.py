"""Test finer tier granularity for exit thresholds.

Splits the 0.35-0.50 mid bucket and tests 4-tier and 5-tier systems,
plus continuous (linear interpolation) approaches.
"""
import sqlite3
import sys
import numpy as np

sys.path.insert(0, ".")


def load_enriched(conn):
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
        enriched.append({
            "outcome": trade["outcome"],
            "entry_price": entry_price,
            "amount": trade["amount_usdc"],
            "actual_pnl": trade["pnl"],
            "bids": bids,
            "tag": trade.get("trade_tag") or "taker",
        })
    return enriched


def sim_with_fn(enriched, threshold_fn):
    """Simulate first-touch exit with arbitrary threshold function."""
    total_with = 0
    total_without = 0
    exits = 0
    rescued = 0
    cut_short = 0
    for r in enriched:
        total_without += r["actual_pnl"]
        thresh = threshold_fn(r["entry_price"])
        fee = 0.015
        tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
        exited = False
        for t, b in r["bids"]:
            if t < 10:
                continue
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
    return total_with - total_without, exits, rescued, cut_short


def main():
    conn = sqlite3.connect("btc_edge.db")
    enriched = load_enriched(conn)
    conn.close()

    # Show distribution of trades by entry price
    print(f"Total: {len(enriched)} trades")
    ep_counts = {}
    for lo in np.arange(0.25, 0.65, 0.05):
        hi = lo + 0.05
        subset = [r for r in enriched if lo <= r["entry_price"] < hi]
        w = sum(1 for r in subset if r["outcome"] == "WIN")
        l = sum(1 for r in subset if r["outcome"] == "LOSS")
        pnl = sum(r["actual_pnl"] for r in subset)
        if subset:
            print(f"  Entry {lo:.2f}-{hi:.2f}: {len(subset):3d} trades (W/L={w}/{l}) PnL=${pnl:+.2f}")
            ep_counts[(lo, hi)] = len(subset)

    # ================================================================
    print("\n" + "=" * 70)
    print("1. OPTIMAL FLAT THRESHOLD PER 0.05 ENTRY BUCKET")
    print("   What's the best exit threshold for each narrow bucket?")
    print("=" * 70)
    for lo in np.arange(0.25, 0.65, 0.05):
        hi = lo + 0.05
        subset = [r for r in enriched if lo <= r["entry_price"] < hi]
        if len(subset) < 5:
            continue
        best_delta = -999
        best_t = 0
        for t in np.arange(0.40, 1.00, 0.02):
            delta, exits, _, _ = sim_with_fn(subset, lambda ep, t=t: t)
            if delta > best_delta:
                best_delta = delta
                best_t = t
        # Also show nearby thresholds
        nearby = []
        for t in np.arange(max(0.40, best_t - 0.10), min(1.00, best_t + 0.12), 0.02):
            delta, exits, rescued, cut = sim_with_fn(subset, lambda ep, t=t: t)
            nearby.append((t, delta, exits, rescued, cut))
        print(f"\n  Entry {lo:.2f}-{hi:.2f} ({len(subset)} trades): best threshold = {best_t:.2f} (${best_delta:+.2f})")
        for t, delta, exits, rescued, cut in nearby:
            marker = " <<<" if t == best_t else ""
            print(f"    t={t:.2f}: ${delta:+.2f} ({exits} exits, {rescued}L/{cut}W){marker}")

    # ================================================================
    print("\n" + "=" * 70)
    print("2. 4-TIER SYSTEM: Split 0.35-0.50 into two sub-buckets")
    print("=" * 70)
    # Test all reasonable 4-tier configs
    # Tiers: <0.35 | 0.35-X | X-0.50 | 0.50+
    # Where X = 0.40, 0.42, 0.45
    results_4t = []
    for split in [0.40, 0.42, 0.45]:
        for t1 in np.arange(0.50, 0.72, 0.04):  # cheap
            for t2 in np.arange(0.56, 0.78, 0.04):  # low-mid
                for t3 in np.arange(0.60, 0.86, 0.04):  # high-mid
                    for t4 in np.arange(0.88, 0.98, 0.02):  # high
                        if t2 < t1 or t3 < t2 or t4 < t3:
                            continue
                        def make_fn(split=split, t1=t1, t2=t2, t3=t3, t4=t4):
                            def fn(ep):
                                if ep < 0.35:
                                    return t1
                                if ep < split:
                                    return t2
                                if ep < 0.50:
                                    return t3
                                return t4
                            return fn
                        delta, exits, rescued, cut = sim_with_fn(enriched, make_fn())
                        results_4t.append({
                            "split": split, "t1": t1, "t2": t2, "t3": t3, "t4": t4,
                            "delta": delta, "exits": exits, "rescued": rescued, "cut": cut,
                        })

    results_4t.sort(key=lambda x: x["delta"], reverse=True)
    print(f"  Tested {len(results_4t)} 4-tier configs. Top 15:")
    print(f"  {'Split':>5s} | {'Cheap':>5s} {'LoMid':>5s} {'HiMid':>5s} {'High':>5s} | {'Delta':>8s} {'Exits':>5s} {'Resc':>5s} {'Cut':>5s}")
    for r in results_4t[:15]:
        print(f"  {r['split']:.2f}  | {r['t1']:.2f}  {r['t2']:.2f}  {r['t3']:.2f}  {r['t4']:.2f}  | "
              f"${r['delta']:+7.2f} {r['exits']:5d} {r['rescued']:5d} {r['cut']:5d}")

    # Compare best 4-tier vs best 3-tier
    best_4 = results_4t[0]
    delta_3, _, _, _ = sim_with_fn(enriched, lambda ep: 0.60 if ep < 0.35 else (0.65 if ep < 0.50 else 0.94))
    print(f"\n  Best 3-tier (0.60/0.65/0.94): ${delta_3:+.2f}")
    print(f"  Best 4-tier: ${best_4['delta']:+.2f} (split at {best_4['split']:.2f})")
    print(f"  Improvement from 4th tier: ${best_4['delta'] - delta_3:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("3. CONTINUOUS (LINEAR) THRESHOLD: Threshold = f(entry_price)")
    print("   Instead of buckets, smooth interpolation")
    print("=" * 70)
    # Linear from low_t at entry=0.25 to high_t at entry=0.65
    results_lin = []
    for low_t in np.arange(0.45, 0.70, 0.03):
        for high_t in np.arange(0.85, 1.00, 0.02):
            if high_t <= low_t:
                continue
            def make_fn(low_t=low_t, high_t=high_t):
                def fn(ep):
                    ep_clamped = max(0.25, min(0.65, ep))
                    return low_t + (high_t - low_t) * (ep_clamped - 0.25) / 0.40
                return fn
            delta, exits, rescued, cut = sim_with_fn(enriched, make_fn())
            results_lin.append({
                "low_t": low_t, "high_t": high_t,
                "delta": delta, "exits": exits, "rescued": rescued, "cut": cut,
            })

    results_lin.sort(key=lambda x: x["delta"], reverse=True)
    print(f"  Tested {len(results_lin)} linear configs. Top 10:")
    print(f"  {'Low@0.25':>8s} {'High@0.65':>9s} | {'Delta':>8s} {'Exits':>5s} {'Resc':>5s} {'Cut':>5s}")
    for r in results_lin[:10]:
        print(f"  {r['low_t']:.2f}     {r['high_t']:.2f}      | "
              f"${r['delta']:+7.2f} {r['exits']:5d} {r['rescued']:5d} {r['cut']:5d}")

    # Best linear vs best 3-tier vs best 4-tier
    best_lin = results_lin[0]
    print(f"\n  COMPARISON:")
    print(f"    3-tier (0.60/0.65/0.94): ${delta_3:+.2f}")
    print(f"    4-tier best:             ${best_4['delta']:+.2f}")
    print(f"    Linear best:             ${best_lin['delta']:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("4. PRACTICAL 4-TIER CANDIDATES (rounded values)")
    print("=" * 70)
    candidates = [
        ("3-tier: 0.60/0.65/0.94", lambda ep: 0.60 if ep < 0.35 else (0.65 if ep < 0.50 else 0.94)),
        ("4-tier: 0.60/0.60/0.70/0.94 (split 0.42)",
         lambda ep: 0.60 if ep < 0.35 else (0.60 if ep < 0.42 else (0.70 if ep < 0.50 else 0.94))),
        ("4-tier: 0.55/0.60/0.68/0.94 (split 0.42)",
         lambda ep: 0.55 if ep < 0.35 else (0.60 if ep < 0.42 else (0.68 if ep < 0.50 else 0.94))),
        ("4-tier: 0.60/0.62/0.70/0.94 (split 0.42)",
         lambda ep: 0.60 if ep < 0.35 else (0.62 if ep < 0.42 else (0.70 if ep < 0.50 else 0.94))),
        ("4-tier: 0.60/0.65/0.75/0.94 (split 0.45)",
         lambda ep: 0.60 if ep < 0.35 else (0.65 if ep < 0.45 else (0.75 if ep < 0.50 else 0.94))),
        ("Linear 0.54->0.94", lambda ep: 0.54 + (0.94 - 0.54) * (max(0.25, min(0.65, ep)) - 0.25) / 0.40),
        ("Linear 0.57->0.94", lambda ep: 0.57 + (0.94 - 0.57) * (max(0.25, min(0.65, ep)) - 0.25) / 0.40),
    ]
    for name, fn in candidates:
        delta, exits, rescued, cut = sim_with_fn(enriched, fn)
        print(f"  {name:50s}: ${delta:+7.2f} ({exits} exits, {rescued}L/{cut}W)")

    # ================================================================
    print("\n" + "=" * 70)
    print("5. SANITY: Show what threshold each entry price gets in linear model")
    print("=" * 70)
    best_low = best_lin["low_t"]
    best_high = best_lin["high_t"]
    print(f"  Linear: {best_low:.2f} at entry=0.25 -> {best_high:.2f} at entry=0.65")
    for ep in np.arange(0.25, 0.66, 0.05):
        thresh = best_low + (best_high - best_low) * (ep - 0.25) / 0.40
        subset = [r for r in enriched if abs(r["entry_price"] - ep) < 0.025]
        print(f"    entry={ep:.2f}: threshold={thresh:.3f} ({len(subset)} trades nearby)")


if __name__ == "__main__":
    main()
