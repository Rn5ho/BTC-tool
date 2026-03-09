"""Analyze early closure opportunities from market snapshot data.

For each losing trade: what was the highest bid observed during the window?
Could we have exited profitably at various thresholds?
Special focus on exploration trades (0.25-0.35 entry price).
"""
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")


def main():
    conn = sqlite3.connect("btc_edge.db")
    conn.row_factory = sqlite3.Row

    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM live_trades WHERE success=1 AND outcome IS NOT NULL AND pnl IS NOT NULL ORDER BY timestamp"
    ).fetchall()]

    print(f"Live trades: {len(trades)}")
    print(f"  WIN: {sum(1 for t in trades if t['outcome'] == 'WIN')}")
    print(f"  LOSS: {sum(1 for t in trades if t['outcome'] == 'LOSS')}")
    print(f"  EARLY_EXIT: {sum(1 for t in trades if t['outcome'] == 'EARLY_EXIT')}")

    # Collect all trades with bid data during their window
    all_results = []
    for trade in trades:
        if trade["outcome"] == "EARLY_EXIT":
            continue
        slug = trade["market_slug"]
        side = trade["side"]
        entry_price = trade.get("entry_price", 0)
        if entry_price <= 0:
            continue
        ts = trade["timestamp"]
        ts_sec_start = ts // 1000
        ts_sec_end = ts_sec_start + 300

        snapshots = conn.execute(
            "SELECT * FROM market_snapshots WHERE slug = ? AND timestamp BETWEEN ? AND ?",
            (slug, ts_sec_start - 30, ts_sec_end + 30)
        ).fetchall()

        if not snapshots:
            continue

        bids_over_time = []
        for snap in snapshots:
            snap = dict(snap)
            if side == "UP":
                bid = snap.get("up_best_bid") or 0
            else:
                bid = snap.get("down_best_bid") or 0
            if bid:
                bids_over_time.append((snap["timestamp"] - ts_sec_start, bid))

        if not bids_over_time:
            continue

        max_bid = max(b for _, b in bids_over_time)

        all_results.append({
            "outcome": trade["outcome"],
            "entry_price": entry_price,
            "amount": trade["amount_usdc"],
            "actual_pnl": trade["pnl"],
            "max_bid": max_bid,
            "bids": bids_over_time,
            "side": side,
            "tag": trade.get("trade_tag") or "taker",
        })

    print(f"Trades with bid snapshot data: {len(all_results)}")

    # ================================================================
    print("\n" + "=" * 70)
    print("MAX BID DISTRIBUTION (losing trades only)")
    print("=" * 70)
    losses = [r for r in all_results if r["outcome"] == "LOSS"]
    bid_buckets = [
        ("< 0.20", 0, 0.20), ("0.20-0.30", 0.20, 0.30),
        ("0.30-0.40", 0.30, 0.40), ("0.40-0.50", 0.40, 0.50),
        ("0.50-0.60", 0.50, 0.60), ("0.60-0.70", 0.60, 0.70),
        ("0.70-0.80", 0.70, 0.80), ("0.80-0.90", 0.80, 0.90),
        ("0.90-0.95", 0.90, 0.95), (">= 0.95", 0.95, 2.0),
    ]
    for label, lo, hi in bid_buckets:
        subset = [r for r in losses if lo <= r["max_bid"] < hi]
        if subset:
            print(f"  Max bid {label:10s}: {len(subset):3d} trades")

    # ================================================================
    print("\n" + "=" * 70)
    print("BY ENTRY PRICE BUCKET (losing trades — rescue opportunities)")
    print("=" * 70)
    for ep_label, ep_lo, ep_hi in [
        ("Exploration (0.25-0.35)", 0.25, 0.35),
        ("Normal (0.35-0.50)", 0.35, 0.50),
        ("Favored (0.50-0.65)", 0.50, 0.65),
    ]:
        subset = [r for r in losses if ep_lo <= r["entry_price"] < ep_hi]
        if not subset:
            print(f"\n  {ep_label}: 0 trades")
            continue

        print(f"\n  {ep_label}: {len(subset)} losing trades")
        for bid_label, blo, bhi in bid_buckets:
            bs = [r for r in subset if blo <= r["max_bid"] < bhi]
            if bs:
                print(f"    Max bid {bid_label:10s}: {len(bs):3d}")

        for threshold in [0.50, 0.60, 0.70, 0.80, 0.90, 0.95]:
            rescuable = [r for r in subset if r["max_bid"] >= threshold]
            if not rescuable:
                continue
            total_exit_pnl = 0
            total_actual_pnl = 0
            for r in rescuable:
                fee = 0.015
                tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
                sell_proceeds = tokens * threshold * (1 - fee)
                total_exit_pnl += sell_proceeds - r["amount"]
                total_actual_pnl += r["actual_pnl"]
            saved = total_exit_pnl - total_actual_pnl
            print(f"    Exit at bid>={threshold:.2f}: {len(rescuable):3d} trades | "
                  f"exit PnL=${total_exit_pnl:+.2f} vs hold PnL=${total_actual_pnl:+.2f} | "
                  f"saved=${saved:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("FULL IMPACT: Early exit at threshold X across ALL trades")
    print("  Includes: rescued losses + prematurely exited winners")
    print("=" * 70)

    for threshold in [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        total_pnl_with = 0
        total_pnl_without = 0
        exited = 0
        rescued = 0
        cut_short = 0

        for r in all_results:
            total_pnl_without += r["actual_pnl"]
            hit = any(b >= threshold for _, b in r["bids"])

            if hit:
                exited += 1
                fee = 0.015
                tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
                sell_proceeds = tokens * threshold * (1 - fee)
                total_pnl_with += sell_proceeds - r["amount"]
                if r["outcome"] == "LOSS":
                    rescued += 1
                elif r["outcome"] == "WIN":
                    cut_short += 1
            else:
                total_pnl_with += r["actual_pnl"]

        delta = total_pnl_with - total_pnl_without
        print(f"  t={threshold:.2f}: exits={exited:3d} ({rescued}L rescued, "
              f"{cut_short}W cut short) | PnL=${total_pnl_with:+.2f} vs "
              f"${total_pnl_without:+.2f} | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("EXPLORATION TRADES ONLY (entry 0.25-0.35)")
    print("=" * 70)
    explore = [r for r in all_results if 0.25 <= r["entry_price"] < 0.35]
    print(f"Exploration trades with snapshots: {len(explore)}")
    if explore:
        print(f"  WIN: {sum(1 for r in explore if r['outcome'] == 'WIN')}")
        print(f"  LOSS: {sum(1 for r in explore if r['outcome'] == 'LOSS')}")
        for threshold in [0.50, 0.60, 0.70, 0.80, 0.90]:
            total_with = 0
            total_without = 0
            exits = 0
            for r in explore:
                total_without += r["actual_pnl"]
                hit = any(b >= threshold for _, b in r["bids"])
                if hit:
                    exits += 1
                    fee = 0.015
                    tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
                    total_with += tokens * threshold * (1 - fee) - r["amount"]
                else:
                    total_with += r["actual_pnl"]
            delta = total_with - total_without
            print(f"  t={threshold:.2f}: exits={exits}/{len(explore)} | "
                  f"PnL=${total_with:+.2f} vs ${total_without:+.2f} | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("TIERED EXIT: Different threshold by entry price")
    print("=" * 70)
    configs = [
        ("Current (flat 0.95)", {(0.25, 0.35): 0.95, (0.35, 0.50): 0.95, (0.50, 0.65): 0.95}),
        ("Conservative", {(0.25, 0.35): 0.80, (0.35, 0.50): 0.90, (0.50, 0.65): 0.95}),
        ("Moderate", {(0.25, 0.35): 0.70, (0.35, 0.50): 0.85, (0.50, 0.65): 0.95}),
        ("Aggressive", {(0.25, 0.35): 0.60, (0.35, 0.50): 0.80, (0.50, 0.65): 0.90}),
        ("Sweet spot?", {(0.25, 0.35): 0.70, (0.35, 0.50): 0.90, (0.50, 0.65): 0.95}),
    ]
    for name, tiers in configs:
        total_with = 0
        total_without = 0
        total_exits = 0
        rescued = 0
        cut_short = 0
        for r in all_results:
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
            hit = any(b >= thresh for _, b in r["bids"])
            if hit:
                total_exits += 1
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
        print(f"  {name:25s}: exits={total_exits:3d} ({rescued}L/{cut_short}W) | "
              f"PnL=${total_with:+.2f} vs ${total_without:+.2f} | delta=${delta:+.2f}")

    # ================================================================
    # Timing: WHEN during the window do spikes happen?
    print("\n" + "=" * 70)
    print("SPIKE TIMING: When do losing trades hit their max bid?")
    print("=" * 70)
    for r in losses:
        if not r["bids"]:
            continue
        max_b = max(r["bids"], key=lambda x: x[1])
        r["peak_time"] = max_b[0]

    timed = [r for r in losses if "peak_time" in r]
    for label, lo, hi in [("0-60s", 0, 60), ("60-120s", 60, 120),
                           ("120-180s", 120, 180), ("180-240s", 180, 240),
                           ("240-300s", 240, 300)]:
        subset = [r for r in timed if lo <= r["peak_time"] < hi]
        high_peak = [r for r in subset if r["max_bid"] >= 0.70]
        print(f"  {label}: {len(subset):3d} peaks | {len(high_peak)} reached bid >= 0.70")

    conn.close()


if __name__ == "__main__":
    main()
