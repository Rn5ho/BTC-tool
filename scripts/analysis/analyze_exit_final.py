"""Final exploration angles before approach proposals.

1. Bid velocity / "held above" as exit signal
2. Maker vs taker exit strategies
3. Tiered bid variant sweep
"""
import sqlite3
import sys
import numpy as np
from itertools import product

sys.path.insert(0, ".")


def load_enriched(conn):
    """Load trades enriched with bid snapshot data."""
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
        enriched.append({
            "outcome": trade["outcome"],
            "entry_price": entry_price,
            "amount": trade["amount_usdc"],
            "actual_pnl": trade["pnl"],
            "side": side,
            "tag": trade.get("trade_tag") or "taker",
            "bids": bids,
            "max_bid": max(bid_values),
            "min_bid": min(bid_values),
        })

    return enriched


def sim_sequential(enriched, threshold_fn, min_hold_snapshots=0):
    """Simulate sequential monitoring with optional hold requirement.

    threshold_fn(entry_price, t) -> bid threshold to sell at
    min_hold_snapshots: bid must be >= threshold for this many consecutive
                        snapshots before we sell (0 = first touch)
    """
    total_with = 0
    total_without = 0
    exits = 0
    rescued = 0
    cut_short = 0
    holds = 0

    for r in enriched:
        total_without += r["actual_pnl"]
        fee = 0.015
        tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
        exited = False
        consecutive = 0

        for t, b in r["bids"]:
            if t < 10:
                continue
            thresh = threshold_fn(r["entry_price"], t)
            if b >= thresh:
                consecutive += 1
                if consecutive >= min_hold_snapshots + 1:
                    # Sell at current bid
                    total_with += tokens * b * (1 - fee) - r["amount"]
                    exits += 1
                    if r["outcome"] == "LOSS":
                        rescued += 1
                    elif r["outcome"] == "WIN":
                        cut_short += 1
                    exited = True
                    break
            else:
                consecutive = 0

        if not exited:
            total_with += r["actual_pnl"]
            holds += 1

    delta = total_with - total_without
    return {
        "exits": exits, "rescued": rescued, "cut_short": cut_short,
        "holds": holds, "pnl_with": total_with, "pnl_without": total_without,
        "delta": delta,
    }


def main():
    conn = sqlite3.connect("btc_edge.db")
    enriched = load_enriched(conn)
    conn.close()

    losses = [r for r in enriched if r["outcome"] == "LOSS"]
    wins = [r for r in enriched if r["outcome"] == "WIN"]
    taker = [r for r in enriched if r["tag"] not in ("maker_fill", "exploration")]
    maker = [r for r in enriched if r["tag"] == "maker_fill"]
    explore = [r for r in enriched if r["entry_price"] < 0.35]

    print(f"Total: {len(enriched)} ({len(wins)}W/{len(losses)}L)")
    print(f"Taker: {len(taker)} | Maker: {len(maker)} | Explore (<0.35): {len(explore)}")

    # ================================================================
    print("\n" + "=" * 70)
    print("1. BID VELOCITY: 'Held above X for N snapshots' as exit trigger")
    print("   Tests whether waiting for confirmation improves results")
    print("=" * 70)

    # Tiered threshold function
    def tiered_thresh(ep, t):
        if ep < 0.35:
            return 0.70
        if ep < 0.50:
            return 0.85
        return 0.95

    for hold_n in [0, 1, 2, 3, 4, 5]:
        r = sim_sequential(enriched, tiered_thresh, min_hold_snapshots=hold_n)
        label = f"First touch" if hold_n == 0 else f"Held {hold_n} snapshots (~{hold_n*3}s)"
        print(f"  {label:30s}: exits={r['exits']:3d} ({r['rescued']}L/{r['cut_short']}W) "
              f"hold={r['holds']:3d} | delta=${r['delta']:+.2f}")

    # Same but with flat 0.90 threshold
    print("\n  With flat 0.90 threshold:")
    for hold_n in [0, 1, 2, 3, 4, 5]:
        r = sim_sequential(enriched, lambda ep, t: 0.90, min_hold_snapshots=hold_n)
        label = f"First touch" if hold_n == 0 else f"Held {hold_n} (~{hold_n*3}s)"
        print(f"  {label:25s}: exits={r['exits']:3d} ({r['rescued']}L/{r['cut_short']}W) "
              f"hold={r['holds']:3d} | delta=${r['delta']:+.2f}")

    # Does confirmation help more for specific entry price ranges?
    print("\n  Confirmation by entry price bucket (tiered threshold):")
    for ep_label, ep_lo, ep_hi in [("Cheap 0.25-0.35", 0.25, 0.35),
                                     ("Mid 0.35-0.50", 0.35, 0.50),
                                     ("High 0.50-0.65", 0.50, 0.65)]:
        subset = [r for r in enriched if ep_lo <= r["entry_price"] < ep_hi]
        if len(subset) < 5:
            print(f"  {ep_label}: {len(subset)} trades (too few)")
            continue
        for hold_n in [0, 1, 2]:
            res = sim_sequential(subset, tiered_thresh, min_hold_snapshots=hold_n)
            hn = "1st" if hold_n == 0 else f"{hold_n}+"
            print(f"    {ep_label:20s} {hn:4s}: exits={res['exits']:3d} "
                  f"({res['rescued']}L/{res['cut_short']}W) | delta=${res['delta']:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("2. MAKER vs TAKER: Should they have different exit strategies?")
    print("=" * 70)

    for label, subset in [("TAKER", taker), ("MAKER", maker)]:
        if len(subset) < 10:
            print(f"  {label}: {len(subset)} trades (too few)")
            continue
        w = sum(1 for r in subset if r["outcome"] == "WIN")
        l = sum(1 for r in subset if r["outcome"] == "LOSS")
        pnl = sum(r["actual_pnl"] for r in subset)
        print(f"\n  === {label} ({len(subset)} trades, W/L={w}/{l}, PnL=${pnl:+.2f}) ===")

        # Test strategies on each population
        strategies = [
            ("No exit (baseline)", lambda ep, t: 99.0),
            ("Flat 0.95", lambda ep, t: 0.95),
            ("Flat 0.90", lambda ep, t: 0.90),
            ("Tiered (0.70/0.85/0.95)", tiered_thresh),
            ("Tiered + 1-snap hold", None),  # special
        ]
        for name, thresh_fn in strategies:
            if name == "Tiered + 1-snap hold":
                res = sim_sequential(subset, tiered_thresh, min_hold_snapshots=1)
            elif name == "No exit (baseline)":
                print(f"    {name:30s}: PnL=${pnl:+.2f}")
                continue
            else:
                res = sim_sequential(subset, thresh_fn)
            print(f"    {name:30s}: exits={res['exits']:3d} ({res['rescued']}L/{res['cut_short']}W) "
                  f"| PnL=${res['pnl_with']:+.2f} | delta=${res['delta']:+.2f}")

        # Best single threshold for this population?
        print(f"    Threshold sweep:")
        best_delta = -999
        best_t = 0
        for t in np.arange(0.60, 1.00, 0.05):
            res = sim_sequential(subset, lambda ep, _t, t=t: t)
            if res["delta"] > best_delta:
                best_delta = res["delta"]
                best_t = t
            print(f"      t={t:.2f}: exits={res['exits']:3d} | delta=${res['delta']:+.2f}")
        print(f"    Best flat threshold: {best_t:.2f} (delta=${best_delta:+.2f})")

    # ================================================================
    print("\n" + "=" * 70)
    print("3. TIERED BID VARIANT SWEEP")
    print("   Testing all reasonable tier combinations")
    print("=" * 70)

    # Sweep: cheap threshold (0.50-0.80), mid threshold (0.70-0.95), high threshold (0.85-0.98)
    cheap_range = np.arange(0.50, 0.85, 0.05)
    mid_range = np.arange(0.70, 1.00, 0.05)
    high_range = np.arange(0.85, 1.00, 0.05)

    results = []
    for c in cheap_range:
        for m in mid_range:
            for h in high_range:
                if m < c or h < m:
                    continue  # thresholds must be increasing
                def make_fn(c=c, m=m, h=h):
                    return lambda ep, t: c if ep < 0.35 else (m if ep < 0.50 else h)
                res = sim_sequential(enriched, make_fn())
                results.append({
                    "cheap": c, "mid": m, "high": h,
                    **res,
                })

    # Sort by delta and show top 20
    results.sort(key=lambda x: x["delta"], reverse=True)
    print(f"  Tested {len(results)} combinations. Top 20:")
    print(f"  {'Cheap':>6s} {'Mid':>6s} {'High':>6s} | {'Exits':>5s} {'Resc':>5s} {'Cut':>5s} | {'Delta':>8s} {'PnL':>8s}")
    for r in results[:20]:
        print(f"  {r['cheap']:.2f}  {r['mid']:.2f}  {r['high']:.2f}  | "
              f"{r['exits']:5d} {r['rescued']:5d} {r['cut_short']:5d} | "
              f"${r['delta']:+7.2f} ${r['pnl_with']:+7.2f}")

    # Bottom 5 (worst)
    print(f"\n  Bottom 5 (worst):")
    for r in results[-5:]:
        print(f"  {r['cheap']:.2f}  {r['mid']:.2f}  {r['high']:.2f}  | "
              f"{r['exits']:5d} {r['rescued']:5d} {r['cut_short']:5d} | "
              f"${r['delta']:+7.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("4. BEST COMBO: Tiered + hold confirmation sweep")
    print("=" * 70)
    # Take top 5 tier configs and test with 0/1/2 hold snapshots
    for r in results[:5]:
        c, m, h = r["cheap"], r["mid"], r["high"]
        for hold_n in [0, 1, 2]:
            def make_fn(c=c, m=m, h=h):
                return lambda ep, t: c if ep < 0.35 else (m if ep < 0.50 else h)
            res = sim_sequential(enriched, make_fn(), min_hold_snapshots=hold_n)
            hn = "1st" if hold_n == 0 else f"{hold_n}+"
            print(f"  {c:.2f}/{m:.2f}/{h:.2f} hold={hn:3s}: exits={res['exits']:3d} "
                  f"({res['rescued']}L/{res['cut_short']}W) | delta=${res['delta']:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("5. EXPLORATION-SPECIFIC: What threshold works for 0.25-0.35 entries?")
    print("=" * 70)
    if len(explore) >= 3:
        print(f"  Exploration trades: {len(explore)}")
        for t in np.arange(0.40, 0.85, 0.05):
            res = sim_sequential(explore, lambda ep, _t, t=t: t)
            print(f"    t={t:.2f}: exits={res['exits']:3d} ({res['rescued']}L/{res['cut_short']}W) "
                  f"| delta=${res['delta']:+.2f} | PnL=${res['pnl_with']:+.2f}")

        # With hold confirmation
        print(f"\n  Best exploration thresholds with 1-snap hold:")
        for t in np.arange(0.40, 0.85, 0.05):
            res = sim_sequential(explore, lambda ep, _t, t=t: t, min_hold_snapshots=1)
            print(f"    t={t:.2f} (hold 1): exits={res['exits']:3d} ({res['rescued']}L/{res['cut_short']}W) "
                  f"| delta=${res['delta']:+.2f}")


if __name__ == "__main__":
    main()
