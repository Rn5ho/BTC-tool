"""For < 0.55 entries: what's the optimal early exit strategy?
Analyze actual exit prices, hold-vs-exit outcomes, and find sweet spots."""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    paper_cursor = conn.execute(
        "SELECT market_slug, side, outcome FROM paper_trades WHERE outcome IS NOT NULL"
    )
    paper_map = {p["market_slug"]: dict(p) for p in paper_cursor}

    # All trades
    cursor = conn.execute(
        "SELECT id, timestamp, market_slug, side, amount_usdc, entry_price, "
        "outcome, pnl, trade_tag, regime_state, regime_strength "
        "FROM live_trades WHERE success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp"
    )
    trades = [dict(t) for t in cursor.fetchall()]

    span_days = (trades[-1]["timestamp"] - trades[0]["timestamp"]) / 1000 / 86400

    # ================================================================
    # 1. Current early exit by entry tier - what's actually happening
    # ================================================================
    print("=" * 90)
    print("EARLY EXIT PERFORMANCE BY ENTRY PRICE TIER (3-day data)")
    print("=" * 90)
    print()

    tiers = [
        (0.25, 0.35, "0.25-0.35", "0.60"),
        (0.35, 0.40, "0.35-0.40", "0.65"),
        (0.40, 0.45, "0.40-0.45", "0.65"),
        (0.45, 0.50, "0.45-0.50", "0.65"),
        (0.50, 0.55, "0.50-0.55", "0.95"),
        (0.55, 0.60, "0.55-0.60", "0.95"),
        (0.60, 0.65, "0.60-0.65", "0.95"),
    ]

    for lo, hi, label, threshold in tiers:
        bucket = [t for t in trades if lo <= (t["entry_price"] or 0) < hi]
        if not bucket:
            continue

        wins = [t for t in bucket if t["outcome"] == "WIN"]
        losses = [t for t in bucket if t["outcome"] == "LOSS"]
        exits = [t for t in bucket if t["outcome"] == "EARLY_EXIT"]
        pnl = sum(t["pnl"] or 0 for t in bucket)

        settled = len(wins) + len(losses)
        wr = len(wins) / settled * 100 if settled else 0

        # For exits: what was the actual return?
        exit_returns = []
        for t in exits:
            ret = t["pnl"] / t["amount_usdc"] * 100
            exit_returns.append(ret)

        # For this tier, what % of trades get early-exited?
        exit_rate = len(exits) / len(bucket) * 100

        # Avg payout per outcome
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        avg_exit = sum(t["pnl"] for t in exits) / len(exits) if exits else 0

        print(f"  {label} (exit threshold: {threshold})")
        print(f"    {len(bucket)} trades: {len(wins)}W/{len(losses)}L/{len(exits)}E | "
              f"WR: {wr:.0f}% | Exit rate: {exit_rate:.0f}%")
        print(f"    Avg WIN: ${avg_win:+.2f} | Avg LOSS: ${avg_loss:+.2f} | "
              f"Avg EXIT: ${avg_exit:+.2f}")
        print(f"    P&L: ${pnl:+.2f} (${pnl/span_days:+.2f}/day)")
        if exit_returns:
            print(f"    Exit returns: min {min(exit_returns):.0f}%, "
                  f"avg {sum(exit_returns)/len(exit_returns):.0f}%, "
                  f"max {max(exit_returns):.0f}%")
        print()

    # ================================================================
    # 2. For < 0.55 entries: analyze what WOULD happen with more
    #    aggressive exit thresholds
    # ================================================================
    print("=" * 90)
    print("DEEP DIVE: < 0.55 ENTRIES - WHERE'S THE MONEY?")
    print("=" * 90)
    print()

    low_entries = [t for t in trades if (t["entry_price"] or 0) < 0.55]
    print(f"Total < 0.55 entries: {len(low_entries)} trades")
    print()

    # Current results
    low_w = sum(1 for t in low_entries if t["outcome"] == "WIN")
    low_l = sum(1 for t in low_entries if t["outcome"] == "LOSS")
    low_e = sum(1 for t in low_entries if t["outcome"] == "EARLY_EXIT")
    low_pnl = sum(t["pnl"] or 0 for t in low_entries)

    print(f"  Current: {low_w}W/{low_l}L/{low_e}E | P&L: ${low_pnl:+.2f}")
    print(f"  Wins earn:  ${sum(t['pnl'] for t in low_entries if t['outcome'] == 'WIN'):+.2f}")
    print(f"  Losses burn: ${sum(t['pnl'] for t in low_entries if t['outcome'] == 'LOSS'):+.2f}")
    print(f"  Exits save:  ${sum(t['pnl'] for t in low_entries if t['outcome'] == 'EARLY_EXIT'):+.2f}")
    print()

    # The key question: for LOSSES in < 0.55 range, did the bid ever
    # spike during the window? We can check via early exit data.
    # Trades that LOST but were NOT early-exited means the bid never
    # hit the exit threshold. What if the threshold was lower?

    # Let's analyze: for each entry price, what exit threshold makes
    # the tier profitable?

    print("=" * 90)
    print("SIMULATED EXIT THRESHOLDS FOR < 0.55 ENTRIES")
    print("=" * 90)
    print()
    print("  For each entry tier, we simulate: what if the exit threshold")
    print("  was lower? We check how many current LOSSes would have been")
    print("  rescued, and at what profit.")
    print()

    # We can't know exactly what bid prices were during losing trades,
    # but we can reason about it:
    # - Entry at 0.40 means we paid $0.40/token
    # - If bid reaches 0.50 during the window, we can sell at 0.50
    # - Profit = (0.50 - 0.40) / 0.40 * bet = 25% of bet
    # - The question: how often does bid reach various levels?

    # We DO know that for early-exited trades, the bid reached the
    # threshold. Let's analyze the exit P&L to infer bid levels.

    print("  EARLY EXIT DETAILS (< 0.55 entries):")
    print(f"  {'ID':>5s}  {'Entry':>6s}  {'Side':>5s}  {'Bet':>5s}  {'P&L':>7s}  {'Ret%':>5s}  {'Est Bid':>8s}")
    print(f"  {'-----':>5s}  {'-----':>6s}  {'----':>5s}  {'---':>5s}  {'-----':>7s}  {'----':>5s}  {'-------':>8s}")

    for t in sorted(low_entries, key=lambda x: x["entry_price"]):
        if t["outcome"] != "EARLY_EXIT":
            continue
        ret = t["pnl"] / t["amount_usdc"]
        # Estimate what bid we sold at:
        # tokens = bet / entry_price
        # sell_revenue = tokens * bid
        # pnl = sell_revenue - bet
        # pnl = (bet / entry) * bid - bet
        # pnl/bet = bid/entry - 1
        # bid = entry * (1 + pnl/bet)
        est_bid = t["entry_price"] * (1 + ret)
        print(f"  #{t['id']:4d}  {t['entry_price']:.3f}  {t['side']:>5s}  "
              f"${t['amount_usdc']:.2f}  ${t['pnl']:+5.2f}  {ret*100:+4.0f}%  ~{est_bid:.3f}")

    print()

    # ================================================================
    # 3. Estimate: what if we exited even earlier?
    # ================================================================
    print("=" * 90)
    print("WHAT IF WE EXIT EARLIER? (theoretical analysis)")
    print("=" * 90)
    print()

    # For trades that went to settlement (WIN or LOSS), we know the
    # final outcome but not intermediate bid prices. However, we can
    # use the early exit data to estimate how often bids spike.

    # Current exit rates by tier tell us something:
    for lo, hi, label in [(0.35, 0.45, "0.35-0.45"),
                           (0.45, 0.50, "0.45-0.50"),
                           (0.50, 0.55, "0.50-0.55")]:
        bucket = [t for t in trades if lo <= (t["entry_price"] or 0) < hi]
        if not bucket:
            continue

        w = sum(1 for t in bucket if t["outcome"] == "WIN")
        l = sum(1 for t in bucket if t["outcome"] == "LOSS")
        e = sum(1 for t in bucket if t["outcome"] == "EARLY_EXIT")
        exit_rate = e / len(bucket) * 100

        # For losses: each loss = -$3.50 (full bet)
        # For exits: avg profit
        exit_pnl = sum(t["pnl"] for t in bucket if t["outcome"] == "EARLY_EXIT")
        avg_exit_pnl = exit_pnl / e if e else 0
        loss_total = sum(t["pnl"] for t in bucket if t["outcome"] == "LOSS")
        win_total = sum(t["pnl"] for t in bucket if t["outcome"] == "WIN")
        total = win_total + loss_total + exit_pnl

        # Hypothetical: if we could exit even MORE trades early
        # What exit rate would make this tier break even?
        # Let remaining = settled trades (non-exit)
        # settled_pnl = wins - losses
        # Need: settled_pnl + exit_pnl >= 0
        # If we exit more losses, each loss converted to exit:
        # saves $3.50 (avoid loss) + adds ~$avg_exit (exit profit)
        # net gain per conversion: $3.50 + $avg_exit

        if l > 0 and avg_exit_pnl > 0:
            per_conversion = 3.50 + avg_exit_pnl  # saving a loss + gaining exit profit
            # But we also lose some wins that would have settled
            # For simplicity: how many losses need converting?
            deficit = abs(total)
            conversions_needed = deficit / per_conversion if total < 0 else 0

            print(f"  {label}: {len(bucket)} trades ({w}W/{l}L/{e}E) | "
                  f"P&L: ${total:+.2f}")
            print(f"    Current exit rate: {exit_rate:.0f}%")
            print(f"    Each loss->exit conversion saves: ${per_conversion:.2f}")
            if total < 0:
                print(f"    Need {conversions_needed:.0f} more exits to break even "
                      f"({conversions_needed/l*100:.0f}% of remaining losses)")
            else:
                print(f"    Already profitable!")
            print()

    # ================================================================
    # 4. The real question: can we get market data on bid spikes?
    # ================================================================
    print("=" * 90)
    print("MARKET SNAPSHOT ANALYSIS: HOW OFTEN DO BIDS SPIKE?")
    print("=" * 90)
    print()

    # Check if we have market_snapshots data
    try:
        snap_cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM market_snapshots"
        )
        snap_count = snap_cursor.fetchone()["cnt"]
        print(f"  Market snapshots in DB: {snap_count}")

        if snap_count > 0:
            # Get schema
            cols = conn.execute("PRAGMA table_info(market_snapshots)").fetchall()
            col_names = [c[1] for c in cols]
            print(f"  Columns: {', '.join(col_names)}")

            # Sample
            sample = conn.execute(
                "SELECT * FROM market_snapshots ORDER BY ROWID DESC LIMIT 3"
            ).fetchall()
            for s in sample:
                print(f"  Sample: {dict(s)}")
    except Exception as ex:
        print(f"  No market_snapshots table: {ex}")

    # ================================================================
    # 5. Summary: optimal strategy for cheap entries
    # ================================================================
    print()
    print("=" * 90)
    print("SUMMARY: WHAT TO DO WITH < 0.55 ENTRIES")
    print("=" * 90)
    print()

    # Compare strategies for the < 0.55 bucket
    low = [t for t in trades if (t["entry_price"] or 0) < 0.55]
    low_pnl = sum(t["pnl"] or 0 for t in low)

    # Strategy 1: keep as-is
    print(f"  1. KEEP AS-IS:        ${low_pnl:+.2f} ({low_pnl/span_days:+.2f}/day)")

    # Strategy 2: skip all < 0.55
    print(f"  2. SKIP ALL < 0.55:   $0.00 ($0.00/day) -- save ${abs(low_pnl):.2f}")

    # Strategy 3: only keep the early exits (somehow exit everything)
    exit_pnl_low = sum(t["pnl"] for t in low if t["outcome"] == "EARLY_EXIT")
    print(f"  3. EXIT-ONLY VALUE:   ${exit_pnl_low:+.2f} ({exit_pnl_low/span_days:+.2f}/day)")
    print(f"     (if we could exit every trade before settlement)")

    # Strategy 4: lower threshold so more trades exit
    # Estimate: if we lower threshold from 0.65 to 0.55 for 0.35-0.50
    # and from 0.95 to 0.65 for 0.50-0.55, how many more exits?
    for lo, hi, old_th, new_th in [(0.35, 0.50, 0.65, 0.55),
                                     (0.50, 0.55, 0.95, 0.65)]:
        bucket = [t for t in trades if lo <= (t["entry_price"] or 0) < hi]
        current_exits = sum(1 for t in bucket if t["outcome"] == "EARLY_EXIT")
        current_settled = sum(1 for t in bucket if t["outcome"] in ("WIN", "LOSS"))
        print(f"\n  Lower threshold {lo}-{hi}: {old_th} -> {new_th}")
        print(f"    Current: {current_exits} exits / {len(bucket)} trades ({current_exits/len(bucket)*100:.0f}%)")
        # We can estimate that a lower threshold catches more spikes
        # Rough: doubling exit rate is reasonable for halving threshold distance
        est_new_rate = min(0.80, current_exits / len(bucket) * (1 + (float(old_th) - float(new_th)) * 3))
        est_new_exits = int(est_new_rate * len(bucket))
        # Each new exit: profit = (new_th - entry) / entry * bet
        mid_entry = (lo + hi) / 2
        est_exit_profit = (float(new_th) - mid_entry) / mid_entry * 3.50
        est_saved = (est_new_exits - current_exits) * (3.50 + est_exit_profit)
        print(f"    Estimated new exits: ~{est_new_exits} ({est_new_rate*100:.0f}%)")
        print(f"    Est profit per new exit: ${est_exit_profit:.2f}")

    # Current entry price tier economics for all < 0.55
    print()
    print("  KEY INSIGHT:")
    print("  " + "-" * 60)

    # What fraction of < 0.55 entries get early-exited?
    low_exits = sum(1 for t in low if t["outcome"] == "EARLY_EXIT")
    low_settled_l = sum(1 for t in low if t["outcome"] == "LOSS")
    low_settled_w = sum(1 for t in low if t["outcome"] == "WIN")

    print(f"  Of {len(low)} trades under 0.55:")
    print(f"    {low_exits} early exited ({low_exits/len(low)*100:.0f}%) -> "
          f"${exit_pnl_low:+.2f}")
    print(f"    {low_settled_w} won at settlement ({low_settled_w/len(low)*100:.0f}%) -> "
          f"${sum(t['pnl'] for t in low if t['outcome']=='WIN'):+.2f}")
    print(f"    {low_settled_l} lost at settlement ({low_settled_l/len(low)*100:.0f}%) -> "
          f"${sum(t['pnl'] for t in low if t['outcome']=='LOSS'):+.2f}")
    print()
    print(f"  The {low_settled_l} settlement losses cost "
          f"${abs(sum(t['pnl'] for t in low if t['outcome']=='LOSS')):.2f}")
    print(f"  That's ${abs(sum(t['pnl'] for t in low if t['outcome']=='LOSS'))/span_days:.2f}/day in bleeding")
    print(f"  Early exits only rescue {low_exits} of them")
    print()
    print(f"  If we could exit 50% of losses early instead:")
    potential_saves = low_settled_l * 0.5
    avg_low_exit = exit_pnl_low / low_exits if low_exits else 2.0
    potential_value = potential_saves * (3.50 + avg_low_exit)
    print(f"    ~{potential_saves:.0f} losses converted to exits")
    print(f"    Saving ~${potential_value:.0f} ({potential_saves:.0f} x "
          f"${3.50 + avg_low_exit:.2f})")


if __name__ == "__main__":
    main()
