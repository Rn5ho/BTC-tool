"""For every trade, find the max bid during its 5-min window.
This tells us: what exit threshold would have caught each trade?"""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # All trades
    cursor = conn.execute(
        "SELECT id, timestamp, market_slug, side, amount_usdc, entry_price, "
        "outcome, pnl "
        "FROM live_trades WHERE success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp"
    )
    trades = [dict(t) for t in cursor.fetchall()]

    # For each trade, find max bid on OUR side during the window
    # The slug tells us which market, side tells us up or down
    print("=" * 90)
    print("MAX BID SPIKE ANALYSIS: What was available during each window?")
    print("=" * 90)
    print()

    results = []

    for t in trades:
        slug = t["market_slug"]
        side = t["side"]

        # Which bid column to look at?
        if side == "UP":
            bid_col = "up_best_bid"
        else:
            bid_col = "down_best_bid"

        # Get max bid during this market's window (after 10s grace)
        # Slug format: btc-updown-5m-{epoch} where epoch is window start
        try:
            window_start = int(slug.split("-")[-1])
        except (ValueError, IndexError):
            continue

        grace = 10  # seconds
        window_end = window_start + 300  # 5 minutes

        snap_cursor = conn.execute(
            f"SELECT MAX({bid_col}) as max_bid, MIN({bid_col}) as min_bid, "
            f"COUNT(*) as snap_count "
            f"FROM market_snapshots "
            f"WHERE slug = ? AND timestamp >= ? AND timestamp <= ?",
            (slug, window_start + grace, window_end),
        )
        snap = snap_cursor.fetchone()

        if not snap or snap["snap_count"] == 0 or snap["max_bid"] is None:
            continue

        t["max_bid"] = snap["max_bid"]
        t["min_bid"] = snap["min_bid"]
        t["snap_count"] = snap["snap_count"]

        # What return could we have gotten at max bid?
        if t["entry_price"] and t["entry_price"] > 0:
            t["max_return"] = (snap["max_bid"] - t["entry_price"]) / t["entry_price"]
            t["max_exit_pnl"] = t["amount_usdc"] * t["max_return"]
        else:
            t["max_return"] = 0
            t["max_exit_pnl"] = 0

        results.append(t)

    print(f"Matched {len(results)} trades with market snapshot data")
    print()

    # ================================================================
    # 1. For LOSSES: what was the max bid available?
    # ================================================================
    print("=" * 90)
    print("1. LOSING TRADES: MAX BID SPIKE DURING WINDOW")
    print("=" * 90)
    print()

    losses = [t for t in results if t["outcome"] == "LOSS"]

    # Group by entry tier
    tiers = [(0.25, 0.35), (0.35, 0.40), (0.40, 0.45),
             (0.45, 0.50), (0.50, 0.55), (0.55, 0.65)]

    for lo, hi in tiers:
        tier_losses = [t for t in losses if lo <= (t["entry_price"] or 0) < hi]
        if not tier_losses:
            continue

        print(f"  Entry {lo:.2f}-{hi:.2f}: {len(tier_losses)} losses")

        # How many had a bid spike above entry (could have exited in profit)?
        above_entry = [t for t in tier_losses if t["max_bid"] > t["entry_price"]]
        # How many had a bid spike giving >10%, >20%, >30% return?
        above_10 = [t for t in tier_losses if t["max_return"] >= 0.10]
        above_20 = [t for t in tier_losses if t["max_return"] >= 0.20]
        above_30 = [t for t in tier_losses if t["max_return"] >= 0.30]
        above_50 = [t for t in tier_losses if t["max_return"] >= 0.50]

        print(f"    Bid > entry (any profit):   {len(above_entry)}/{len(tier_losses)} "
              f"({len(above_entry)/len(tier_losses)*100:.0f}%)")
        print(f"    Bid > entry +10%:           {len(above_10)}/{len(tier_losses)} "
              f"({len(above_10)/len(tier_losses)*100:.0f}%)")
        print(f"    Bid > entry +20%:           {len(above_20)}/{len(tier_losses)} "
              f"({len(above_20)/len(tier_losses)*100:.0f}%)")
        print(f"    Bid > entry +30%:           {len(above_30)}/{len(tier_losses)} "
              f"({len(above_30)/len(tier_losses)*100:.0f}%)")
        print(f"    Bid > entry +50%:           {len(above_50)}/{len(tier_losses)} "
              f"({len(above_50)/len(tier_losses)*100:.0f}%)")

        # Max bid distribution
        max_bids = [t["max_bid"] for t in tier_losses]
        max_returns = [t["max_return"] for t in tier_losses]
        avg_max_bid = sum(max_bids) / len(max_bids)
        avg_max_ret = sum(max_returns) / len(max_returns)
        print(f"    Avg max bid: {avg_max_bid:.3f} (avg max return: {avg_max_ret*100:+.0f}%)")

        # Money left on table: if we exited at max bid
        potential_pnl = sum(t["max_exit_pnl"] for t in tier_losses if t["max_exit_pnl"] > 0)
        actual_pnl = sum(t["pnl"] for t in tier_losses)
        print(f"    Actual P&L: ${actual_pnl:+.2f}")
        print(f"    If exited at max bid (when profitable): ${potential_pnl:+.2f}")
        print(f"    Money left on table: ${potential_pnl - actual_pnl:+.2f}")
        print()

    # ================================================================
    # 2. Simulate various exit thresholds
    # ================================================================
    print("=" * 90)
    print("2. SIMULATED EXIT THRESHOLDS (all trades with snapshot data)")
    print("=" * 90)
    print()

    # For each threshold: if max_bid >= threshold, trade exits
    # at that threshold price. Otherwise goes to settlement.

    entry_tiers = [(0.35, 0.50, "0.35-0.50"), (0.50, 0.55, "0.50-0.55"),
                   (0.55, 0.65, "0.55-0.65")]

    for elo, ehi, elabel in entry_tiers:
        tier_trades = [t for t in results if elo <= (t["entry_price"] or 0) < ehi]
        if not tier_trades:
            continue

        actual_pnl = sum(t["pnl"] or 0 for t in tier_trades)

        print(f"  Entry tier {elabel} ({len(tier_trades)} trades, actual P&L: ${actual_pnl:+.2f})")
        print()
        print(f"    {'Threshold':>10s}  {'Exits':>5s}  {'Settled':>7s}  "
              f"{'Exit P&L':>9s}  {'Settle P&L':>11s}  {'Total P&L':>10s}  {'vs Actual':>10s}")
        print(f"    {'-'*10}  {'-'*5}  {'-'*7}  {'-'*9}  {'-'*11}  {'-'*10}  {'-'*10}")

        for threshold in [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
            if threshold <= elo:
                continue

            exit_pnl = 0.0
            settle_pnl = 0.0
            exits = 0
            settled = 0

            for t in tier_trades:
                if t["max_bid"] >= threshold:
                    # Would have exited at threshold
                    # Profit = (threshold - entry) / entry * bet
                    profit = (threshold - t["entry_price"]) / t["entry_price"] * t["amount_usdc"]
                    # Subtract a small fee estimate
                    profit *= 0.98
                    exit_pnl += profit
                    exits += 1
                else:
                    # Goes to settlement - use actual outcome
                    settle_pnl += t["pnl"] or 0
                    settled += 1

            total = exit_pnl + settle_pnl
            vs_actual = total - actual_pnl
            marker = " <-- BEST" if vs_actual == max(
                [(exit_pnl2 + settle_pnl2 - actual_pnl)
                 for th2 in [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
                 if th2 > elo
                 for exit_pnl2, settle_pnl2 in [
                     (sum((th2 - t2["entry_price"]) / t2["entry_price"] * t2["amount_usdc"] * 0.98
                          for t2 in tier_trades if t2["max_bid"] >= th2),
                      sum(t2["pnl"] or 0 for t2 in tier_trades if t2["max_bid"] < th2))
                 ]]
            ) else ""

            print(f"    {threshold:>10.2f}  {exits:>5d}  {settled:>7d}  "
                  f"${exit_pnl:>+8.2f}  ${settle_pnl:>+10.2f}  ${total:>+9.2f}  "
                  f"${vs_actual:>+9.2f}{marker}")

        print()

    # ================================================================
    # 3. Optimal per-tier thresholds
    # ================================================================
    print("=" * 90)
    print("3. OPTIMAL EXIT THRESHOLD PER ENTRY TIER")
    print("=" * 90)
    print()

    fine_tiers = [(0.25, 0.35), (0.35, 0.40), (0.40, 0.45),
                  (0.45, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.65)]

    for elo, ehi in fine_tiers:
        tier_trades = [t for t in results if elo <= (t["entry_price"] or 0) < ehi]
        if len(tier_trades) < 5:
            continue

        actual_pnl = sum(t["pnl"] or 0 for t in tier_trades)
        best_threshold = None
        best_pnl = actual_pnl
        best_detail = ""

        for th_pct in range(5, 100, 5):
            # Threshold = entry + th_pct% of entry
            # But actually, fixed threshold is simpler
            pass

        # Try absolute thresholds
        for threshold in [x / 100 for x in range(40, 100, 2)]:
            if threshold <= elo:
                continue

            exit_pnl = 0.0
            settle_pnl = 0.0
            exits = 0

            for t in tier_trades:
                if t["max_bid"] >= threshold:
                    profit = (threshold - t["entry_price"]) / t["entry_price"] * t["amount_usdc"]
                    profit *= 0.98
                    exit_pnl += profit
                    exits += 1
                else:
                    settle_pnl += t["pnl"] or 0

            total = exit_pnl + settle_pnl
            if total > best_pnl:
                best_pnl = total
                best_threshold = threshold
                best_detail = f"{exits} exits"

        if best_threshold:
            improvement = best_pnl - actual_pnl
            print(f"  {elo:.2f}-{ehi:.2f}: optimal threshold = {best_threshold:.2f} "
                  f"({best_detail})")
            print(f"    Current P&L: ${actual_pnl:+.2f} -> Optimal: ${best_pnl:+.2f} "
                  f"(+${improvement:.2f})")
        else:
            print(f"  {elo:.2f}-{ehi:.2f}: current threshold already optimal "
                  f"(P&L: ${actual_pnl:+.2f})")
        print()

    # ================================================================
    # 4. Combined: new thresholds applied to entire dataset
    # ================================================================
    print("=" * 90)
    print("4. COMBINED: OPTIMAL THRESHOLDS vs CURRENT vs SKIP")
    print("=" * 90)
    print()

    # Find best threshold per tier
    optimal_thresholds = {}
    for elo, ehi in fine_tiers:
        tier_trades = [t for t in results if elo <= (t["entry_price"] or 0) < ehi]
        if len(tier_trades) < 5:
            continue

        best_th = None
        best_total = sum(t["pnl"] or 0 for t in tier_trades)

        for threshold in [x / 100 for x in range(40, 100, 2)]:
            if threshold <= elo:
                continue
            ep = sum(
                (threshold - t["entry_price"]) / t["entry_price"] * t["amount_usdc"] * 0.98
                for t in tier_trades if t["max_bid"] >= threshold
            )
            sp = sum(t["pnl"] or 0 for t in tier_trades if t["max_bid"] < threshold)
            if ep + sp > best_total:
                best_total = ep + sp
                best_th = threshold

        optimal_thresholds[(elo, ehi)] = best_th

    print("  Optimal thresholds found:")
    for (elo, ehi), th in sorted(optimal_thresholds.items()):
        current = "0.60" if ehi <= 0.35 else ("0.65" if ehi <= 0.50 else "0.95")
        print(f"    Entry {elo:.2f}-{ehi:.2f}: {th} (current: {current})")

    print()

    # Apply optimal thresholds to all trades
    opt_pnl = 0.0
    cur_pnl = 0.0
    opt_exits = 0
    opt_settled_w = 0
    opt_settled_l = 0

    for t in results:
        cur_pnl += t["pnl"] or 0

        # Find which tier
        th = None
        for (elo, ehi), threshold in optimal_thresholds.items():
            if elo <= (t["entry_price"] or 0) < ehi and threshold:
                th = threshold
                break

        if th and t["max_bid"] >= th:
            profit = (th - t["entry_price"]) / t["entry_price"] * t["amount_usdc"] * 0.98
            opt_pnl += profit
            opt_exits += 1
        else:
            opt_pnl += t["pnl"] or 0
            if t["outcome"] == "WIN":
                opt_settled_w += 1
            elif t["outcome"] == "LOSS":
                opt_settled_l += 1

    span_days = (results[-1]["timestamp"] - results[0]["timestamp"]) / 1000 / 86400
    offset = 15.03

    print(f"  {'Strategy':<35s}  {'P&L':>9s}  {'$/day':>8s}  {'Balance':>9s}")
    print(f"  {'-'*35}  {'-'*9}  {'-'*8}  {'-'*9}")
    print(f"  {'Current thresholds':<35s}  ${cur_pnl:>+8.2f}  ${cur_pnl/span_days:>+7.2f}  "
          f"${73+cur_pnl+offset:>8.2f}")
    print(f"  {'Optimal thresholds':<35s}  ${opt_pnl:>+8.2f}  ${opt_pnl/span_days:>+7.2f}  "
          f"${73+opt_pnl+offset:>8.2f}")
    print(f"  {'Skip < 0.55 entries':<35s}  ", end="")
    skip_pnl = sum(t["pnl"] or 0 for t in results if (t["entry_price"] or 0) >= 0.55)
    print(f"${skip_pnl:>+8.2f}  ${skip_pnl/span_days:>+7.2f}  ${73+skip_pnl+offset:>8.2f}")

    improvement = opt_pnl - cur_pnl
    print(f"\n  Optimal thresholds improvement: ${improvement:+.2f} "
          f"(${improvement/span_days:+.2f}/day)")


if __name__ == "__main__":
    main()
