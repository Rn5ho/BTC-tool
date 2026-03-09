"""Backtest exit strategy using BOTH live and paper trades against snapshots.

Also builds a synthetic bid model from candle data for extended backtesting.
"""
import sqlite3
import sys
import numpy as np
from datetime import datetime, timezone

sys.path.insert(0, ".")


def load_all_trades_with_bids(conn):
    """Load both live and paper trades, match with bid snapshots."""
    conn.row_factory = sqlite3.Row

    # Live trades
    live = [dict(r) for r in conn.execute(
        "SELECT *, 'live' as source FROM live_trades "
        "WHERE success=1 AND outcome IS NOT NULL AND pnl IS NOT NULL ORDER BY timestamp"
    ).fetchall()]

    # Paper trades
    paper = [dict(r) for r in conn.execute(
        "SELECT *, 'paper' as source FROM paper_trades "
        "WHERE outcome IS NOT NULL AND pnl IS NOT NULL ORDER BY timestamp"
    ).fetchall()]

    print(f"Raw: {len(live)} live + {len(paper)} paper")

    all_trades = []
    for trade in live + paper:
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

        all_trades.append({
            "outcome": trade["outcome"],
            "entry_price": entry_price,
            "amount": trade.get("amount_usdc") or trade.get("amount", 0),
            "actual_pnl": trade["pnl"],
            "side": side,
            "source": trade["source"],
            "bids": bids,
            "tag": trade.get("trade_tag") or "taker",
        })

    return all_trades


def sim_tiered(trades, cheap_t, mid_t, high_t, boundary1=0.35, boundary2=0.50):
    """Simulate tiered first-touch exit."""
    total_with = 0
    total_without = 0
    exits = 0
    rescued = 0
    cut_short = 0
    for r in trades:
        total_without += r["actual_pnl"]
        ep = r["entry_price"]
        if ep < boundary1:
            thresh = cheap_t
        elif ep < boundary2:
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
    delta = total_with - total_without
    return {"delta": delta, "exits": exits, "rescued": rescued, "cut_short": cut_short,
            "pnl_with": total_with, "pnl_without": total_without}


def build_bid_model(conn):
    """Build a model mapping (time_elapsed, btc_pct_move) -> bid level.

    Uses the 3.2 days where we have both candle data and snapshot data.
    """
    conn.row_factory = sqlite3.Row
    # Get all unique market slugs with their snapshot data
    slugs = conn.execute(
        "SELECT DISTINCT slug FROM market_snapshots"
    ).fetchall()
    slugs = [s[0] for s in slugs]

    data_points = []  # (time_elapsed_frac, btc_move_pct, bid_level, side)

    for slug in slugs:
        # Each slug is a 5-min market. Parse the timestamps.
        snaps = [dict(r) for r in conn.execute(
            "SELECT * FROM market_snapshots WHERE slug = ? ORDER BY timestamp",
            (slug,)
        ).fetchall()]
        if len(snaps) < 10:
            continue

        # Get window start time from slug or first snapshot
        window_start = snaps[0]["timestamp"]
        window_end = window_start + 300  # 5 min

        # Get BTC candles covering this window
        candles = conn.execute(
            "SELECT timestamp, open, close FROM candles "
            "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (window_start * 1000 - 60000, window_end * 1000 + 60000)
        ).fetchall()
        if not candles:
            continue

        btc_open = candles[0][1]  # BTC price at window start

        for snap in snaps:
            t_elapsed = snap["timestamp"] - window_start
            if t_elapsed < 0 or t_elapsed > 300:
                continue

            # Find closest candle to get current BTC price
            snap_ms = snap["timestamp"] * 1000
            closest_candle = min(candles, key=lambda c: abs(c[0] - snap_ms))
            btc_current = closest_candle[2]  # close
            btc_move_pct = (btc_current - btc_open) / btc_open * 100

            up_bid = snap.get("up_best_bid") or 0
            down_bid = snap.get("down_best_bid") or 0

            if up_bid > 0:
                data_points.append({
                    "t_frac": t_elapsed / 300,
                    "btc_move": btc_move_pct,
                    "bid": up_bid,
                    "side": "UP",
                })
            if down_bid > 0:
                data_points.append({
                    "t_frac": t_elapsed / 300,
                    "btc_move": btc_move_pct,
                    "bid": down_bid,
                    "side": "DOWN",
                })

    return data_points


def main():
    conn = sqlite3.connect("btc_edge.db")

    # ================================================================
    # PART 1: Combined live + paper backtest
    # ================================================================
    all_trades = load_all_trades_with_bids(conn)
    live_trades = [t for t in all_trades if t["source"] == "live"]
    paper_trades = [t for t in all_trades if t["source"] == "paper"]

    print(f"\nEnriched: {len(all_trades)} total ({len(live_trades)} live + {len(paper_trades)} paper)")
    for source, subset in [("Live", live_trades), ("Paper", paper_trades), ("Combined", all_trades)]:
        w = sum(1 for r in subset if r["outcome"] == "WIN")
        l = sum(1 for r in subset if r["outcome"] == "LOSS")
        pnl = sum(r["actual_pnl"] for r in subset)
        print(f"  {source:8s}: {len(subset):3d} trades (W/L={w}/{l}) PnL=${pnl:+.2f}")

    # Entry price distribution (combined)
    print("\nEntry price distribution (combined):")
    for lo in np.arange(0.25, 0.65, 0.05):
        hi = lo + 0.05
        subset = [r for r in all_trades if lo <= r["entry_price"] < hi]
        if subset:
            w = sum(1 for r in subset if r["outcome"] == "WIN")
            l = sum(1 for r in subset if r["outcome"] == "LOSS")
            print(f"  {lo:.2f}-{hi:.2f}: {len(subset):3d} trades (W/L={w}/{l})")

    # ================================================================
    print("\n" + "=" * 70)
    print("1. KEY TIER CONFIGS ON COMBINED DATA")
    print("=" * 70)

    configs = [
        ("Current (flat 0.95)", 0.95, 0.95, 0.95),
        ("3-tier 0.60/0.65/0.94", 0.60, 0.65, 0.94),
        ("3-tier 0.60/0.70/0.95", 0.60, 0.70, 0.95),
        ("3-tier 0.70/0.85/0.95", 0.70, 0.85, 0.95),
        ("3-tier 0.55/0.65/0.94", 0.55, 0.65, 0.94),
        ("3-tier 0.65/0.68/0.94", 0.65, 0.68, 0.94),
    ]
    for name, c, m, h in configs:
        for label, subset in [("Live", live_trades), ("Paper", paper_trades), ("Combined", all_trades)]:
            r = sim_tiered(subset, c, m, h)
            if label == "Combined":
                per = r["delta"] / len(subset) if subset else 0
                print(f"  {name:35s} {label:8s}: delta=${r['delta']:+7.2f} "
                      f"({r['exits']:3d} exits, {r['rescued']}L/{r['cut_short']}W) "
                      f"[${per:+.3f}/trade]")
            else:
                print(f"  {' ':35s} {label:8s}: delta=${r['delta']:+7.2f} "
                      f"({r['exits']:3d} exits, {r['rescued']}L/{r['cut_short']}W)")

    # ================================================================
    print("\n" + "=" * 70)
    print("2. FINE SWEEP ON COMBINED DATA (confirm optimal)")
    print("=" * 70)
    results = []
    for c in np.arange(0.50, 0.72, 0.04):
        for m in np.arange(0.58, 0.76, 0.02):
            for h in np.arange(0.88, 0.98, 0.02):
                if m < c or h < m:
                    continue
                r = sim_tiered(all_trades, c, m, h)
                results.append((c, m, h, r["delta"], r["exits"], r["rescued"], r["cut_short"]))

    results.sort(key=lambda x: x[3], reverse=True)
    print(f"  Tested {len(results)} combos. Top 15:")
    print(f"  {'Cheap':>6s} {'Mid':>6s} {'High':>6s} | {'Delta':>8s} | {'Exits':>5s} {'Resc':>5s} {'Cut':>5s}")
    for c, m, h, delta, exits, rescued, cut in results[:15]:
        per = delta / len(all_trades)
        print(f"  {c:.2f}  {m:.2f}  {h:.2f}  | ${delta:+7.2f} | "
              f"{exits:5d} {rescued:5d} {cut:5d}  [${per:+.3f}/trade]")

    # ================================================================
    print("\n" + "=" * 70)
    print("3. PER-BUCKET OPTIMAL (combined data)")
    print("=" * 70)
    for lo in np.arange(0.25, 0.65, 0.05):
        hi = lo + 0.05
        subset = [r for r in all_trades if lo <= r["entry_price"] < hi]
        if len(subset) < 5:
            continue
        best_delta = -999
        best_t = 0
        for t in np.arange(0.40, 1.00, 0.02):
            res = sim_tiered(subset, t, t, t)
            if res["delta"] > best_delta:
                best_delta = res["delta"]
                best_t = t
        w = sum(1 for r in subset if r["outcome"] == "WIN")
        l = sum(1 for r in subset if r["outcome"] == "LOSS")
        print(f"  {lo:.2f}-{hi:.2f}: {len(subset):3d} trades (W/L={w}/{l}) | "
              f"best={best_t:.2f} (${best_delta:+.2f})")

    # ================================================================
    print("\n" + "=" * 70)
    print("4. SPLIT-HALF VALIDATION ON COMBINED DATA")
    print("=" * 70)
    n = len(all_trades)
    half1 = all_trades[:n//2]
    half2 = all_trades[n//2:]
    print(f"  Half 1: {len(half1)} trades | Half 2: {len(half2)} trades")

    # Test key configs on both halves
    for name, c, m, h in [("0.60/0.65/0.94", 0.60, 0.65, 0.94),
                            ("0.60/0.70/0.95", 0.60, 0.70, 0.95),
                            ("Flat 0.95", 0.95, 0.95, 0.95)]:
        r1 = sim_tiered(half1, c, m, h)
        r2 = sim_tiered(half2, c, m, h)
        print(f"  {name:20s}: H1=${r1['delta']:+.2f} H2=${r2['delta']:+.2f} | "
              f"total=${r1['delta']+r2['delta']:+.2f}")

    # ================================================================
    # PART 2: Synthetic bid model from candle data
    # ================================================================
    print("\n" + "=" * 70)
    print("5. SYNTHETIC BID MODEL: BTC move -> bid mapping")
    print("=" * 70)

    data_points = build_bid_model(conn)
    print(f"  Collected {len(data_points)} (time, btc_move, bid) data points")

    if data_points:
        # Analyze relationship: how does BTC % move map to bid?
        up_pts = [d for d in data_points if d["side"] == "UP"]
        dn_pts = [d for d in data_points if d["side"] == "DOWN"]

        print(f"  UP: {len(up_pts)} | DOWN: {len(dn_pts)}")

        # For UP token: bid should increase when BTC goes up
        for move_label, move_lo, move_hi in [
            ("BTC -0.3%+", -999, -0.3),
            ("BTC -0.1 to -0.3%", -0.3, -0.1),
            ("BTC -0.1 to 0%", -0.1, 0),
            ("BTC 0 to +0.1%", 0, 0.1),
            ("BTC +0.1 to +0.3%", 0.1, 0.3),
            ("BTC +0.3%+", 0.3, 999),
        ]:
            up_sub = [d for d in up_pts if move_lo <= d["btc_move"] < move_hi]
            if up_sub:
                avg_bid = np.mean([d["bid"] for d in up_sub])
                med_bid = np.median([d["bid"] for d in up_sub])
                print(f"    {move_label:25s}: {len(up_sub):5d} pts | "
                      f"UP bid avg={avg_bid:.3f} med={med_bid:.3f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("6. SYNTHETIC BACKTEST: Simulate exits on all 15 days of candles")
    print("=" * 70)

    # Build a simple model: for each 5-min window, simulate bid trajectory
    # based on minute-by-minute BTC prices
    candles = conn.execute(
        "SELECT timestamp, open, high, low, close FROM candles ORDER BY timestamp"
    ).fetchall()

    # Map BTC % move -> expected bid (from real data)
    if up_pts:
        # Simple bucketed model
        move_to_bid = {}
        for move_bucket in np.arange(-0.5, 0.5, 0.05):
            bucket_pts = [d for d in up_pts
                          if move_bucket <= d["btc_move"] < move_bucket + 0.05
                          and d["t_frac"] > 0.3]  # mid-to-late window
            if bucket_pts:
                move_to_bid[move_bucket] = np.median([d["bid"] for d in bucket_pts])

        if move_to_bid:
            print(f"  BTC move -> UP bid mapping ({len(move_to_bid)} buckets):")
            for k in sorted(move_to_bid):
                print(f"    BTC {k:+.2f}% to {k+0.05:+.2f}%: UP bid ~{move_to_bid[k]:.3f}")

    # Simulate on all 15 days
    # For each 5-min window: use candle data to compute minute-by-minute BTC % move
    # Then map to bid using our model
    if move_to_bid:
        synthetic_trades = []
        for i in range(0, len(candles) - 4):
            t_start = candles[i][0]
            t_end = candles[i + 4][0]
            if (t_end - t_start) > 360000:
                continue
            minute = (t_start // 60000) % 60
            if minute % 5 != 0:
                continue

            btc_open = candles[i][1]
            btc_close = candles[i + 4][4]
            went_up = btc_close > btc_open

            # Build minute-by-minute bid trajectory (for UP token)
            up_bids = []
            for j in range(5):
                if i + j >= len(candles):
                    break
                btc_now = candles[i + j][4]  # close
                move_pct = (btc_now - btc_open) / btc_open * 100
                # Find closest bucket
                bucket = round(move_pct / 0.05) * 0.05
                bucket = max(min(bucket, 0.45), -0.50)
                bid = move_to_bid.get(round(bucket, 2))
                if bid is None:
                    # Find nearest
                    dists = [(abs(k - bucket), v) for k, v in move_to_bid.items()]
                    bid = min(dists, key=lambda x: x[0])[1]
                up_bids.append((j * 60, bid))

            # DOWN bid is roughly 1 - UP bid (with spread)
            down_bids = [(t, max(0.01, 1.0 - b - 0.02)) for t, b in up_bids]

            # Simulate both sides as potential trades
            for side, bids in [("UP", up_bids), ("DOWN", down_bids)]:
                # Simulate entry at median price for this side
                entry = bids[0][1] if bids else 0.50
                if entry < 0.25 or entry > 0.65:
                    continue
                # Actual outcome
                if side == "UP":
                    won = went_up
                else:
                    won = not went_up

                amount = 3.50
                fee = 0.015
                tokens = (amount / entry) * (1 - fee)
                if won:
                    pnl = tokens * (1 - fee) - amount  # WIN pays $1/token
                else:
                    pnl = -amount  # LOSS pays $0/token

                synthetic_trades.append({
                    "outcome": "WIN" if won else "LOSS",
                    "entry_price": entry,
                    "amount": amount,
                    "actual_pnl": pnl,
                    "side": side,
                    "bids": bids,
                    "source": "synthetic",
                    "tag": "synthetic",
                })

        print(f"\n  Synthetic trades: {len(synthetic_trades)}")
        if synthetic_trades:
            w = sum(1 for t in synthetic_trades if t["outcome"] == "WIN")
            l = sum(1 for t in synthetic_trades if t["outcome"] == "LOSS")
            pnl = sum(t["actual_pnl"] for t in synthetic_trades)
            print(f"  W/L: {w}/{l} | PnL: ${pnl:.2f}")

            # Test exit strategies on synthetic data
            print(f"\n  Exit strategy results on synthetic data:")
            for name, c, m, h in [
                ("Current (flat 0.95)", 0.95, 0.95, 0.95),
                ("3-tier 0.60/0.65/0.94", 0.60, 0.65, 0.94),
                ("3-tier 0.60/0.70/0.95", 0.60, 0.70, 0.95),
                ("3-tier 0.70/0.85/0.95", 0.70, 0.85, 0.95),
            ]:
                r = sim_tiered(synthetic_trades, c, m, h)
                per = r["delta"] / len(synthetic_trades)
                print(f"    {name:35s}: delta=${r['delta']:+7.2f} "
                      f"({r['exits']} exits) [${per:+.4f}/trade]")

    conn.close()


if __name__ == "__main__":
    main()
