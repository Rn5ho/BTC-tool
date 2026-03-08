"""Fine-grained optimization of tiered exit thresholds.

Sweeps 0.02 increments around the 0.60/0.70/0.95 region
to confirm these are truly optimal (not just local peak).
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


def sim(enriched, cheap_t, mid_t, high_t):
    """Simulate tiered first-touch exit."""
    total_with = 0
    total_without = 0
    exits = 0
    rescued = 0
    cut_short = 0

    for r in enriched:
        total_without += r["actual_pnl"]
        ep = r["entry_price"]
        if ep < 0.35:
            thresh = cheap_t
        elif ep < 0.50:
            thresh = mid_t
        else:
            thresh = high_t

        fee = 0.015
        tokens = (r["amount"] / ep) * (1 - fee)
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

    print(f"Trades: {len(enriched)}")

    # ================================================================
    print("\n" + "=" * 70)
    print("1. FINE-GRAINED SWEEP: 0.02 increments around optimal region")
    print("=" * 70)

    cheap_range = np.arange(0.50, 0.76, 0.02)
    mid_range = np.arange(0.62, 0.82, 0.02)
    high_range = np.arange(0.88, 1.00, 0.02)

    results = []
    for c in cheap_range:
        for m in mid_range:
            for h in high_range:
                if m < c or h < m:
                    continue
                delta, exits, rescued, cut = sim(enriched, c, m, h)
                results.append((c, m, h, delta, exits, rescued, cut))

    results.sort(key=lambda x: x[3], reverse=True)

    print(f"Tested {len(results)} combinations. Top 30:")
    print(f"  {'Cheap':>6s} {'Mid':>6s} {'High':>6s} | {'Delta':>8s} | {'Exits':>5s} {'Resc':>5s} {'Cut':>5s}")
    for c, m, h, delta, exits, rescued, cut in results[:30]:
        marker = " <<<" if (c, m, h) == (0.60, 0.70, 0.95) else ""
        print(f"  {c:.2f}  {m:.2f}  {h:.2f}  | ${delta:+7.2f} | "
              f"{exits:5d} {rescued:5d} {cut:5d}{marker}")

    # ================================================================
    print("\n" + "=" * 70)
    print("2. SENSITIVITY: How much does delta change if we're off by 0.02-0.04?")
    print("=" * 70)
    # Show neighborhood around the best config
    best = results[0]
    bc, bm, bh = best[0], best[1], best[2]
    print(f"  Best: {bc:.2f}/{bm:.2f}/{bh:.2f} = ${best[3]:+.2f}")

    # Vary each parameter independently
    for label, vary_idx in [("Vary cheap", 0), ("Vary mid", 1), ("Vary high", 2)]:
        print(f"\n  {label} (others fixed at {bc:.2f}/{bm:.2f}/{bh:.2f}):")
        vals = np.arange(0.40, 1.00, 0.02)
        for v in vals:
            params = [bc, bm, bh]
            params[vary_idx] = v
            if params[1] < params[0] or params[2] < params[1]:
                continue
            delta, exits, rescued, cut = sim(enriched, *params)
            marker = " <<<" if abs(v - params[vary_idx]) < 0.001 and v == best[vary_idx] else ""
            if abs(v - best[vary_idx]) <= 0.10 or v == best[vary_idx]:
                print(f"    {v:.2f}: ${delta:+7.2f} ({exits} exits, {rescued}L/{cut}W){marker}")

    # ================================================================
    print("\n" + "=" * 70)
    print("3. ROBUSTNESS: Split-half validation")
    print("   Train on first half of trades, test on second half")
    print("=" * 70)
    n = len(enriched)
    half1 = enriched[:n//2]
    half2 = enriched[n//2:]
    print(f"  Half 1: {len(half1)} trades | Half 2: {len(half2)} trades")

    # Find best config on each half
    for label, subset in [("Half 1 (train)", half1), ("Half 2 (test)", half2)]:
        best_delta = -999
        best_config = None
        for c in np.arange(0.50, 0.76, 0.05):
            for m in np.arange(0.65, 0.85, 0.05):
                for h in np.arange(0.85, 1.00, 0.05):
                    if m < c or h < m:
                        continue
                    delta, _, _, _ = sim(subset, c, m, h)
                    if delta > best_delta:
                        best_delta = delta
                        best_config = (c, m, h)
        print(f"  {label}: best={best_config[0]:.2f}/{best_config[1]:.2f}/{best_config[2]:.2f} "
              f"delta=${best_delta:+.2f}")

    # Test the overall best on each half
    print(f"\n  Overall best ({bc:.2f}/{bm:.2f}/{bh:.2f}) applied to each half:")
    for label, subset in [("Half 1", half1), ("Half 2", half2)]:
        delta, exits, rescued, cut = sim(subset, bc, bm, bh)
        print(f"    {label}: delta=${delta:+.2f} ({exits} exits, {rescued}L/{cut}W)")

    # ================================================================
    print("\n" + "=" * 70)
    print("4. PER-TRADE DELTA DISTRIBUTION")
    print("   Is the improvement spread across many trades or concentrated?")
    print("=" * 70)
    per_trade_deltas = []
    for r in enriched:
        ep = r["entry_price"]
        if ep < 0.35:
            thresh = bc
        elif ep < 0.50:
            thresh = bm
        else:
            thresh = bh

        fee = 0.015
        tokens = (r["amount"] / ep) * (1 - fee)
        exit_pnl = None
        for t, b in r["bids"]:
            if t < 10:
                continue
            if b >= thresh:
                exit_pnl = tokens * b * (1 - fee) - r["amount"]
                break

        if exit_pnl is not None:
            delta = exit_pnl - r["actual_pnl"]
            per_trade_deltas.append({"delta": delta, "outcome": r["outcome"],
                                      "entry": ep, "amount": r["amount"]})

    if per_trade_deltas:
        deltas = [d["delta"] for d in per_trade_deltas]
        pos = [d for d in per_trade_deltas if d["delta"] > 0]
        neg = [d for d in per_trade_deltas if d["delta"] < 0]
        print(f"  Trades that triggered exit: {len(per_trade_deltas)}")
        print(f"  Beneficial exits: {len(pos)} (avg ${np.mean([d['delta'] for d in pos]):+.2f})")
        print(f"  Harmful exits: {len(neg)} (avg ${np.mean([d['delta'] for d in neg]):+.2f})")
        print(f"  Percentiles: p10=${np.percentile(deltas, 10):+.2f} "
              f"p25=${np.percentile(deltas, 25):+.2f} "
              f"p50=${np.percentile(deltas, 50):+.2f} "
              f"p75=${np.percentile(deltas, 75):+.2f} "
              f"p90=${np.percentile(deltas, 90):+.2f}")

        # By outcome
        loss_d = [d["delta"] for d in per_trade_deltas if d["outcome"] == "LOSS"]
        win_d = [d["delta"] for d in per_trade_deltas if d["outcome"] == "WIN"]
        if loss_d:
            print(f"\n  Exited LOSSES ({len(loss_d)}): avg delta=${np.mean(loss_d):+.2f} "
                  f"(${np.sum(loss_d):+.2f} total)")
        if win_d:
            print(f"  Exited WINS ({len(win_d)}): avg delta=${np.mean(win_d):+.2f} "
                  f"(${np.sum(win_d):+.2f} total)")

    # ================================================================
    print("\n" + "=" * 70)
    print("5. EDGE BREAKPOINTS: Entry price boundaries")
    print("   Are 0.35 and 0.50 the right bucket boundaries?")
    print("=" * 70)
    # Test alternative bucket boundaries
    boundaries = [
        ("0.35/0.50 (current)", 0.35, 0.50),
        ("0.33/0.48", 0.33, 0.48),
        ("0.37/0.52", 0.37, 0.52),
        ("0.35/0.45", 0.35, 0.45),
        ("0.35/0.55", 0.35, 0.55),
        ("0.30/0.50", 0.30, 0.50),
        ("0.40/0.55", 0.40, 0.55),
    ]
    for label, b1, b2 in boundaries:
        def make_fn(b1=b1, b2=b2):
            return lambda ep, t: bc if ep < b1 else (bm if ep < b2 else bh)
        delta, exits, rescued, cut = sim(enriched, bc, bm, bh)  # base
        # Actually need custom sim for different boundaries
        total_with = 0
        total_without = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            ep = r["entry_price"]
            if ep < b1:
                thresh = bc
            elif ep < b2:
                thresh = bm
            else:
                thresh = bh
            fee = 0.015
            tokens = (r["amount"] / ep) * (1 - fee)
            exited = False
            for t, b in r["bids"]:
                if t < 10:
                    continue
                if b >= thresh:
                    total_with += tokens * b * (1 - fee) - r["amount"]
                    exited = True
                    break
            if not exited:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        print(f"  {label:20s}: delta=${delta:+.2f}")


if __name__ == "__main__":
    main()
