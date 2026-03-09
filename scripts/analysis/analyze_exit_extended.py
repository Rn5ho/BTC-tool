"""Extended early exit analysis — additional angles not covered in analyze_exit_deep.py.

Angles:
1. Stop-loss false positives: how many WINNERS temporarily dip below stop levels?
2. Combined stop-loss + profit-target simulation (both legs)
3. Actual EARLY_EXIT trade performance vs what holding would have returned
4. Maker fills vs taker fills — different exit behavior?
5. Entry price × bid trajectory — where do stops/targets help most?
6. Bid velocity as exit signal — rate of change matters
7. "Regret analysis" — for every exit, what WOULD have happened if we held?
8. Regime-aware exits (recent trades only)
9. Spread-aware exits — can we actually sell at the bid?
10. Exploration trades deep dive
"""
import sqlite3
import sys
import numpy as np
from collections import defaultdict

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
        asks = []
        spreads = []
        for snap in snapshots:
            snap = dict(snap)
            t = snap["timestamp"] - ts_sec
            if side == "UP":
                bid = snap.get("up_best_bid") or 0
                ask = snap.get("up_best_ask") or 0
                spread = snap.get("up_spread") or 0
            else:
                bid = snap.get("down_best_bid") or 0
                ask = snap.get("down_best_ask") or 0
                spread = snap.get("down_spread") or 0
            if bid:
                bids.append((t, bid))
            if ask:
                asks.append((t, ask))
            if spread is not None:
                spreads.append((t, spread))

        if len(bids) < 5:
            continue

        bid_values = [b for _, b in bids]
        max_bid = max(bid_values)
        min_bid = min(bid_values)
        max_bid_time = next(t for t, b in bids if b == max_bid)
        min_bid_time = next(t for t, b in bids if b == min_bid)

        # Bid velocity (avg change per snapshot)
        velocities = []
        for i in range(1, len(bid_values)):
            dt = bids[i][0] - bids[i-1][0]
            if dt > 0:
                velocities.append((bid_values[i] - bid_values[i-1]) / dt)

        enriched.append({
            "outcome": trade["outcome"],
            "entry_price": entry_price,
            "amount": trade["amount_usdc"],
            "actual_pnl": trade["pnl"],
            "side": side,
            "tag": trade.get("trade_tag") or "taker",
            "regime_state": trade.get("regime_state"),
            "regime_strength": trade.get("regime_strength"),
            "bids": bids,
            "asks": asks,
            "spreads": spreads,
            "max_bid": max_bid,
            "min_bid": min_bid,
            "max_bid_time": max_bid_time,
            "min_bid_time": min_bid_time,
            "bid_std": np.std(bid_values),
            "bid_velocities": velocities,
            "avg_velocity": np.mean(velocities) if velocities else 0,
            "max_velocity": max(velocities) if velocities else 0,
            "min_velocity": min(velocities) if velocities else 0,
        })

    return enriched


def compute_exit_pnl(entry_price, amount, sell_price, fee=0.015):
    """Compute PnL for selling at a given price."""
    tokens = (amount / entry_price) * (1 - fee)
    return tokens * sell_price * (1 - fee) - amount


def main():
    conn = sqlite3.connect("btc_edge.db")
    enriched = load_enriched(conn)
    losses = [r for r in enriched if r["outcome"] == "LOSS"]
    wins = [r for r in enriched if r["outcome"] == "WIN"]
    print(f"Enriched trades: {len(enriched)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Taker: {sum(1 for r in enriched if r['tag'] in (None, 'taker', 'normal'))}")
    print(f"  Maker: {sum(1 for r in enriched if r['tag'] == 'maker_fill')}")
    print(f"  Exploration: {sum(1 for r in enriched if r['tag'] == 'exploration')}")
    print(f"  With regime data: {sum(1 for r in enriched if r['regime_state'])}")

    # ================================================================
    print("\n" + "=" * 70)
    print("1. STOP-LOSS FALSE POSITIVES ON WINNERS")
    print("   How many winning trades temporarily dip below stop levels?")
    print("=" * 70)
    for stop_pct in [0.05, 0.10, 0.15, 0.20, 0.25]:
        stop_price_fn = lambda ep: ep * (1 - stop_pct)
        triggered_wins = 0
        triggered_losses = 0
        win_pnl_lost = 0  # PnL we give up by stopping out winners
        loss_pnl_saved = 0  # PnL we save by stopping out losers
        for r in enriched:
            stop_price = stop_price_fn(r["entry_price"])
            hit = any(b <= stop_price for t, b in r["bids"] if t > 10)
            if hit:
                stop_exit_pnl = compute_exit_pnl(r["entry_price"], r["amount"], stop_price)
                if r["outcome"] == "WIN":
                    triggered_wins += 1
                    win_pnl_lost += r["actual_pnl"] - stop_exit_pnl  # positive = we lost this
                elif r["outcome"] == "LOSS":
                    triggered_losses += 1
                    loss_pnl_saved += stop_exit_pnl - r["actual_pnl"]  # positive = we saved this

        net = loss_pnl_saved - win_pnl_lost
        total_w = len(wins)
        total_l = len(losses)
        print(f"  Stop entry-{stop_pct*100:.0f}%: "
              f"triggers {triggered_wins}/{total_w} wins (lose ${win_pnl_lost:.2f}) + "
              f"{triggered_losses}/{total_l} losses (save ${loss_pnl_saved:.2f}) | "
              f"NET ${net:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("2. COMBINED STOP-LOSS + PROFIT-TARGET (both legs)")
    print("   Simulate monitoring bid each snapshot: stop if low, take profit if high")
    print("=" * 70)

    combos = [
        # (name, stop_pct, profit_threshold_fn)
        ("Stop 10% + flat 0.90", 0.10, lambda ep, t: 0.90),
        ("Stop 10% + flat 0.95", 0.10, lambda ep, t: 0.95),
        ("Stop 10% + ROI 50%", 0.10, lambda ep, t: ep * 1.50 / 0.97),  # approx
        ("Stop 10% + tiered bid", 0.10,
         lambda ep, t: 0.70 if ep < 0.35 else (0.85 if ep < 0.50 else 0.95)),
        ("Stop 15% + tiered bid", 0.15,
         lambda ep, t: 0.70 if ep < 0.35 else (0.85 if ep < 0.50 else 0.95)),
        ("Stop 10% + time-decay 0.95->0.75", 0.10,
         lambda ep, t: max(0.75, 0.95 - (t / 300) * 0.20)),
        ("Stop 10% + ROI 100%<180s, 50% after", 0.10, None),  # special
        ("Stop 15% + ROI 75%<180s, 30% after", 0.15, None),  # special
    ]

    for name, stop_pct, profit_fn in combos:
        total_with = 0
        total_without = 0
        stops = 0
        profits = 0
        holds = 0
        rescued = 0
        cut_short = 0

        for r in enriched:
            total_without += r["actual_pnl"]
            stop_price = r["entry_price"] * (1 - stop_pct)
            fee = 0.015
            tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
            exited = False

            for t, b in r["bids"]:
                if t < 10:
                    continue

                # Check stop-loss first
                if b <= stop_price:
                    total_with += tokens * stop_price * (1 - fee) - r["amount"]
                    stops += 1
                    if r["outcome"] == "LOSS":
                        rescued += 1
                    elif r["outcome"] == "WIN":
                        cut_short += 1
                    exited = True
                    break

                # Check profit target
                if profit_fn is not None:
                    target = profit_fn(r["entry_price"], t)
                    if b >= target:
                        total_with += tokens * b * (1 - fee) - r["amount"]
                        profits += 1
                        if r["outcome"] == "LOSS":
                            rescued += 1
                        elif r["outcome"] == "WIN":
                            cut_short += 1
                        exited = True
                        break
                else:
                    # Special: time-aware ROI
                    if "100%<180s, 50% after" in name:
                        roi = 1.00 if t < 180 else 0.50
                    elif "75%<180s, 30% after" in name:
                        roi = 0.75 if t < 180 else 0.30
                    else:
                        roi = 0.50
                    target_bid = (r["amount"] * (1 + roi)) / (tokens * (1 - fee))
                    if b >= target_bid:
                        total_with += tokens * b * (1 - fee) - r["amount"]
                        profits += 1
                        if r["outcome"] == "LOSS":
                            rescued += 1
                        elif r["outcome"] == "WIN":
                            cut_short += 1
                        exited = True
                        break

            if not exited:
                total_with += r["actual_pnl"]
                holds += 1

        delta = total_with - total_without
        print(f"  {name:42s}: stops={stops:3d} profits={profits:3d} hold={holds:3d} | "
              f"rescued={rescued}L cut={cut_short}W | "
              f"PnL=${total_with:+.2f} vs ${total_without:+.2f} | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("3. ACTUAL EARLY_EXIT TRADES: How do they compare to holding?")
    print("=" * 70)
    conn.row_factory = sqlite3.Row
    early_exits = [dict(r) for r in conn.execute(
        "SELECT * FROM live_trades WHERE success=1 AND outcome='EARLY_EXIT' ORDER BY timestamp"
    ).fetchall()]
    print(f"  Total early exits: {len(early_exits)}")
    if early_exits:
        total_ee_pnl = sum(t["pnl"] for t in early_exits)
        avg_ee_pnl = total_ee_pnl / len(early_exits)
        print(f"  Total PnL from early exits: ${total_ee_pnl:.2f}")
        print(f"  Avg PnL per early exit: ${avg_ee_pnl:.2f}")
        for ee in early_exits[:10]:
            entry = ee.get("entry_price", 0)
            side = ee["side"]
            pnl = ee["pnl"]
            amt = ee["amount_usdc"]
            # What would holding have returned?
            # We can check what the actual resolution was by looking for
            # the same market slug in non-EARLY_EXIT trades
            print(f"    {side:4s} entry={entry:.3f} amt=${amt:.2f} pnl=${pnl:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("4. MAKER FILLS vs TAKER FILLS — different exit behavior?")
    print("=" * 70)
    for tag_label, tag_filter in [
        ("Taker (our orders)", lambda r: r["tag"] not in ("maker_fill", "exploration")),
        ("Maker fills", lambda r: r["tag"] == "maker_fill"),
        ("Exploration", lambda r: r["tag"] == "exploration"),
    ]:
        subset = [r for r in enriched if tag_filter(r)]
        if not subset:
            print(f"  {tag_label}: 0 trades")
            continue
        w = sum(1 for r in subset if r["outcome"] == "WIN")
        l = sum(1 for r in subset if r["outcome"] == "LOSS")
        pnl = sum(r["actual_pnl"] for r in subset)
        avg_max = np.mean([r["max_bid"] for r in subset])
        avg_entry = np.mean([r["entry_price"] for r in subset])
        # How many could be rescued at 0.70?
        rescuable = sum(1 for r in subset if r["outcome"] == "LOSS" and r["max_bid"] >= 0.70)
        print(f"  {tag_label:25s}: {len(subset):3d} trades | W/L={w}/{l} | "
              f"PnL=${pnl:+.2f} | avg_entry={avg_entry:.3f} | "
              f"avg_max_bid={avg_max:.3f} | {rescuable}L rescuable>=0.70")

    # ================================================================
    print("\n" + "=" * 70)
    print("5. ENTRY PRICE × OUTCOME × MAX BID HEATMAP")
    print("   Where do stops and profit targets help most?")
    print("=" * 70)
    ep_buckets = [
        ("0.25-0.35", 0.25, 0.35),
        ("0.35-0.45", 0.35, 0.45),
        ("0.45-0.55", 0.45, 0.55),
        ("0.55-0.65", 0.55, 0.65),
    ]
    for ep_label, ep_lo, ep_hi in ep_buckets:
        subset = [r for r in enriched if ep_lo <= r["entry_price"] < ep_hi]
        if not subset:
            continue
        w = sum(1 for r in subset if r["outcome"] == "WIN")
        l = sum(1 for r in subset if r["outcome"] == "LOSS")
        pnl = sum(r["actual_pnl"] for r in subset)

        # Min bid stats (for stop-loss)
        loss_sub = [r for r in subset if r["outcome"] == "LOSS"]
        win_sub = [r for r in subset if r["outcome"] == "WIN"]
        loss_min_bids = [r["min_bid"] for r in loss_sub] if loss_sub else [0]
        win_min_bids = [r["min_bid"] for r in win_sub] if win_sub else [0]

        # What fraction of losers hit stop-loss at entry-10%?
        sl_losses = sum(1 for r in loss_sub
                        if any(b <= r["entry_price"]*0.90 for t, b in r["bids"] if t > 10))
        sl_wins = sum(1 for r in win_sub
                      if any(b <= r["entry_price"]*0.90 for t, b in r["bids"] if t > 10))

        print(f"  {ep_label}: {len(subset)} trades (W/L={w}/{l}) PnL=${pnl:+.2f}")
        print(f"    Loss min bids: avg={np.mean(loss_min_bids):.3f} | "
              f"Stop-10% triggers: {sl_losses}/{len(loss_sub)}L, {sl_wins}/{len(win_sub)}W")
        # Max bid for losses (rescue potential)
        if loss_sub:
            loss_max_bids = [r["max_bid"] for r in loss_sub]
            pcts = [25, 50, 75, 90]
            percs = np.percentile(loss_max_bids, pcts)
            print(f"    Loss max bids: p25={percs[0]:.3f} p50={percs[1]:.3f} "
                  f"p75={percs[2]:.3f} p90={percs[3]:.3f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("6. BID VELOCITY AS EXIT SIGNAL")
    print("   Can we detect when a spike is fading and sell before it drops?")
    print("=" * 70)
    # For trades that hit max_bid >= 0.70, look at velocity BEFORE peak
    peaked = [r for r in enriched if r["max_bid"] >= 0.60]
    print(f"  Trades peaking above 0.60: {len(peaked)}")

    # How quickly does the bid drop after hitting peak?
    for r in peaked:
        peak_idx = None
        for i, (t, b) in enumerate(r["bids"]):
            if b == r["max_bid"]:
                peak_idx = i
                break
        if peak_idx is None:
            r["post_peak_speed"] = 0
            r["hold_time_at_peak"] = 0
            continue
        # How long does the bid stay within 5% of peak?
        hold_count = 0
        for i in range(peak_idx + 1, len(r["bids"])):
            if r["bids"][i][1] >= r["max_bid"] * 0.95:
                hold_count += 1
            else:
                break
        r["hold_time_at_peak"] = hold_count

        # Drop speed: how much does bid fall in 2 snapshots after peak?
        if peak_idx < len(r["bids"]) - 2:
            r["post_peak_speed"] = r["max_bid"] - r["bids"][peak_idx + 2][1]
        else:
            r["post_peak_speed"] = 0

    hold_times = [r["hold_time_at_peak"] for r in peaked]
    drop_speeds = [r["post_peak_speed"] for r in peaked]
    print(f"  Snapshots bid stays within 5% of peak: "
          f"avg={np.mean(hold_times):.1f} median={np.median(hold_times):.0f} "
          f"max={max(hold_times)}")
    print(f"  Drop after peak (2 snapshots): "
          f"avg={np.mean(drop_speeds):.3f} median={np.median(drop_speeds):.3f}")

    # For losses specifically
    peaked_losses = [r for r in losses if r["max_bid"] >= 0.60]
    peaked_wins = [r for r in wins if r["max_bid"] >= 0.60]
    if peaked_losses:
        hold_l = [r["hold_time_at_peak"] for r in peaked_losses if "hold_time_at_peak" in r]
        drop_l = [r["post_peak_speed"] for r in peaked_losses if "post_peak_speed" in r]
        print(f"\n  LOSSES peaking >=0.60 ({len(peaked_losses)}):")
        print(f"    Stay at peak: avg={np.mean(hold_l):.1f} snapshots")
        print(f"    Drop after: avg={np.mean(drop_l):.3f}")
    if peaked_wins:
        hold_w = [r["hold_time_at_peak"] for r in peaked_wins if "hold_time_at_peak" in r]
        drop_w = [r["post_peak_speed"] for r in peaked_wins if "post_peak_speed" in r]
        print(f"  WINS peaking >=0.60 ({len(peaked_wins)}):")
        print(f"    Stay at peak: avg={np.mean(hold_w):.1f} snapshots")
        print(f"    Drop after: avg={np.mean(drop_w):.3f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("7. REGRET ANALYSIS: For every simulated exit, what would holding return?")
    print("   Tests whether exits are actually value-add or just feel good")
    print("=" * 70)
    # Simulate "moderate tiered bid" exits and for each, compare exit PnL vs hold PnL
    tiers = {(0.25, 0.35): 0.70, (0.35, 0.50): 0.85, (0.50, 0.65): 0.95}
    good_exits = 0  # exit PnL > hold PnL
    bad_exits = 0   # exit PnL < hold PnL (wish we held)
    good_delta = 0
    bad_delta = 0
    for r in enriched:
        ep = r["entry_price"]
        thresh = None
        for (lo, hi), t in tiers.items():
            if lo <= ep < hi:
                thresh = t
                break
        if thresh is None:
            continue
        hit = any(b >= thresh for t, b in r["bids"] if t > 10)
        if hit:
            exit_pnl = compute_exit_pnl(ep, r["amount"], thresh)
            hold_pnl = r["actual_pnl"]
            if exit_pnl > hold_pnl:
                good_exits += 1
                good_delta += exit_pnl - hold_pnl
            else:
                bad_exits += 1
                bad_delta += hold_pnl - exit_pnl

    print(f"  Moderate tiered exits that triggered:")
    print(f"    {good_exits:3d} exits BETTER than holding (rescued ${good_delta:.2f})")
    print(f"    {bad_exits:3d} exits WORSE than holding (gave up ${bad_delta:.2f})")
    print(f"    Net: ${good_delta - bad_delta:+.2f}")
    if good_exits + bad_exits > 0:
        print(f"    Hit rate: {good_exits / (good_exits + bad_exits) * 100:.1f}% of exits are beneficial")

    # ================================================================
    print("\n" + "=" * 70)
    print("8. SPREAD-AWARE EXITS: Can we sell at the bid or do we lose to spread?")
    print("=" * 70)
    # Look at spread when bid is at peak
    spreads_at_peak = []
    for r in enriched:
        if not r["spreads"] or not r["bids"]:
            continue
        # Find spread at same time as max bid
        peak_t = r["max_bid_time"]
        closest_spread = None
        min_dt = 999
        for t, s in r["spreads"]:
            if abs(t - peak_t) < min_dt:
                min_dt = abs(t - peak_t)
                closest_spread = s
        if closest_spread is not None:
            spreads_at_peak.append({
                "spread": closest_spread,
                "max_bid": r["max_bid"],
                "outcome": r["outcome"],
                "entry_price": r["entry_price"],
            })

    if spreads_at_peak:
        sp = [s["spread"] for s in spreads_at_peak if s["spread"] > 0]
        if sp:
            print(f"  Spread at peak bid: avg={np.mean(sp):.4f} "
                  f"median={np.median(sp):.4f} p90={np.percentile(sp, 90):.4f}")
            # Spread for high-bid peaks (when we'd actually want to sell)
            high = [s["spread"] for s in spreads_at_peak if s["max_bid"] >= 0.70 and s["spread"] > 0]
            if high:
                print(f"  Spread when bid>=0.70: avg={np.mean(high):.4f} "
                      f"median={np.median(high):.4f} p90={np.percentile(high, 90):.4f}")
            low = [s["spread"] for s in spreads_at_peak if s["max_bid"] < 0.50 and s["spread"] > 0]
            if low:
                print(f"  Spread when bid<0.50: avg={np.mean(low):.4f} "
                      f"median={np.median(low):.4f} p90={np.percentile(low, 90):.4f}")
        print(f"  Impact: if selling INTO bid, we likely get bid or bid-spread/2")
        print(f"  At avg spread {np.mean(sp):.4f}, on $5 trade that's ~${5 * np.mean(sp):.3f} slippage")

    # ================================================================
    print("\n" + "=" * 70)
    print("9. EXPLORATION TRADES DEEP DIVE (entry 0.25-0.35)")
    print("=" * 70)
    explore = [r for r in enriched if r["entry_price"] < 0.35]
    print(f"  Total: {len(explore)}")
    if explore:
        w = sum(1 for r in explore if r["outcome"] == "WIN")
        l = sum(1 for r in explore if r["outcome"] == "LOSS")
        pnl = sum(r["actual_pnl"] for r in explore)
        print(f"  W/L: {w}/{l} | PnL: ${pnl:.2f}")
        # These have the biggest "lottery ticket" upside
        # What's the bid trajectory for winning vs losing exploration trades?
        if [r for r in explore if r["outcome"] == "LOSS"]:
            loss_explore = [r for r in explore if r["outcome"] == "LOSS"]
            print(f"\n  Losing exploration ({len(loss_explore)}):")
            for r in sorted(loss_explore, key=lambda x: x["max_bid"], reverse=True)[:10]:
                max_b = r["max_bid"]
                roi_at_peak = compute_exit_pnl(r["entry_price"], r["amount"], max_b) / r["amount"] * 100
                print(f"    entry={r['entry_price']:.3f} max_bid={max_b:.3f} "
                      f"peak_time={r['max_bid_time']}s | "
                      f"ROI at peak: {roi_at_peak:+.1f}% | hold PnL=${r['actual_pnl']:+.2f}")

        # What threshold captures most value for exploration?
        print(f"\n  Exploration exit simulations:")
        for thresh in [0.50, 0.60, 0.70, 0.80, 0.90]:
            total_with = 0
            total_without = 0
            exits = 0
            for r in explore:
                total_without += r["actual_pnl"]
                hit = any(b >= thresh for t, b in r["bids"] if t > 10)
                if hit:
                    exits += 1
                    total_with += compute_exit_pnl(r["entry_price"], r["amount"], thresh)
                else:
                    total_with += r["actual_pnl"]
            delta = total_with - total_without
            print(f"    t={thresh:.2f}: {exits}/{len(explore)} exits | delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("10. TIME-SERIES SIMULATION: 1-second-like decision loop")
    print("    Simulates sequential monitoring and sells at FIRST trigger")
    print("=" * 70)
    # This is the most realistic simulation — we check bids in order
    # and take the FIRST opportunity that meets criteria

    strategies = [
        ("Current (flat 0.95)", {
            "stop": None,
            "profit": lambda ep, t, b: b >= 0.95,
        }),
        ("Tiered bid only (0.70/0.85/0.95)", {
            "stop": None,
            "profit": lambda ep, t, b: b >= (0.70 if ep < 0.35 else (0.85 if ep < 0.50 else 0.95)),
        }),
        ("Stop-10% + tiered bid", {
            "stop": lambda ep, b: b <= ep * 0.90,
            "profit": lambda ep, t, b: b >= (0.70 if ep < 0.35 else (0.85 if ep < 0.50 else 0.95)),
        }),
        ("Stop-10% + ROI 100%<3m/50% after", {
            "stop": lambda ep, b: b <= ep * 0.90,
            "profit": lambda ep, t, b: b >= (ep * 2.06 if t < 180 else ep * 1.55),  # ~ROI 100%/50% accounting for fees
        }),
        ("Stop-15% + ROI 75%<3m/30% after", {
            "stop": lambda ep, b: b <= ep * 0.85,
            "profit": lambda ep, t, b: b >= (ep * 1.80 if t < 180 else ep * 1.34),  # ~ROI 75%/30%
        }),
        ("Stop-10% + time-decay 0.95->0.70", {
            "stop": lambda ep, b: b <= ep * 0.90,
            "profit": lambda ep, t, b: b >= max(0.70, 0.95 - (t / 300) * 0.25),
        }),
        ("Stop-10% + decay + exploration 0.60", {
            "stop": lambda ep, b: b <= ep * 0.90,
            "profit": lambda ep, t, b: (
                b >= 0.60 if ep < 0.35
                else b >= max(0.70, 0.95 - (t / 300) * 0.25)
            ),
        }),
    ]

    for name, strategy in strategies:
        total_with = 0
        total_without = 0
        stop_exits = 0
        profit_exits = 0
        holds = 0
        rescued_l = 0
        cut_w = 0

        for r in enriched:
            total_without += r["actual_pnl"]
            fee = 0.015
            tokens = (r["amount"] / r["entry_price"]) * (1 - fee)
            exited = False

            for t, b in r["bids"]:
                if t < 10:
                    continue

                # Check stop-loss
                if strategy["stop"] and strategy["stop"](r["entry_price"], b):
                    total_with += tokens * b * (1 - fee) - r["amount"]
                    stop_exits += 1
                    if r["outcome"] == "LOSS":
                        rescued_l += 1
                    elif r["outcome"] == "WIN":
                        cut_w += 1
                    exited = True
                    break

                # Check profit target
                if strategy["profit"](r["entry_price"], t, b):
                    total_with += tokens * b * (1 - fee) - r["amount"]
                    profit_exits += 1
                    if r["outcome"] == "LOSS":
                        rescued_l += 1
                    elif r["outcome"] == "WIN":
                        cut_w += 1
                    exited = True
                    break

            if not exited:
                total_with += r["actual_pnl"]
                holds += 1

        delta = total_with - total_without
        total_exits = stop_exits + profit_exits
        print(f"  {name:42s}: exits={total_exits:3d} (S{stop_exits}/P{profit_exits}) "
              f"hold={holds:3d} | {rescued_l}L/{cut_w}W | "
              f"delta=${delta:+.2f}")

    # ================================================================
    print("\n" + "=" * 70)
    print("11. SENSITIVITY: How robust are results to fee assumptions?")
    print("=" * 70)
    # Our fee model assumes 1.5%. What if actual execution is worse?
    best_strategy = {
        "stop": lambda ep, b: b <= ep * 0.90,
        "profit": lambda ep, t, b: b >= (0.70 if ep < 0.35 else (0.85 if ep < 0.50 else 0.95)),
    }
    for fee_pct in [0.010, 0.015, 0.020, 0.025, 0.030]:
        total_with = 0
        total_without = 0
        exits = 0
        for r in enriched:
            total_without += r["actual_pnl"]
            tokens = (r["amount"] / r["entry_price"]) * (1 - fee_pct)
            exited = False
            for t, b in r["bids"]:
                if t < 10:
                    continue
                if best_strategy["stop"](r["entry_price"], b):
                    total_with += tokens * b * (1 - fee_pct) - r["amount"]
                    exits += 1
                    exited = True
                    break
                if best_strategy["profit"](r["entry_price"], t, b):
                    total_with += tokens * b * (1 - fee_pct) - r["amount"]
                    exits += 1
                    exited = True
                    break
            if not exited:
                total_with += r["actual_pnl"]
        delta = total_with - total_without
        print(f"  Fee={fee_pct*100:.1f}%: delta=${delta:+.2f} ({exits} exits)")

    conn.close()


if __name__ == "__main__":
    main()
