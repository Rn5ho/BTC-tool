"""Analyze the impact of early exit logic over the past 24 hours.

Compares actual results (with early exits) vs hypothetical results
(if those trades had gone to settlement).
"""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    now = int(datetime.now(timezone.utc).timestamp())
    t24h_ms = (now - 86400) * 1000

    print(f"Analysis window: {datetime.fromtimestamp(now - 86400, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC "
          f"-> {datetime.fromtimestamp(now, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print()

    # -- All live trades in last 24h --------------------------------------
    cursor = conn.execute(
        "SELECT id, timestamp, market_slug, side, amount_usdc, entry_price, "
        "outcome, pnl, settled_at, trade_tag "
        "FROM live_trades "
        "WHERE timestamp >= ? AND success = 1 "
        "ORDER BY timestamp",
        (t24h_ms,),
    )
    trades = [dict(t) for t in cursor.fetchall()]
    print(f"Total live trades in last 24h: {len(trades)}")
    print()

    # -- Group by outcome -------------------------------------------------
    by_outcome = defaultdict(list)
    for t in trades:
        by_outcome[t["outcome"] or "PENDING"].append(t)

    print("=" * 70)
    print("ACTUAL RESULTS (with early exit)")
    print("=" * 70)
    total_pnl_actual = 0
    for outcome in ["WIN", "LOSS", "EARLY_EXIT", "PENDING"]:
        tlist = by_outcome.get(outcome, [])
        if not tlist:
            continue
        pnl = sum(t["pnl"] or 0 for t in tlist)
        total_pnl_actual += pnl
        avg_entry = sum(t["entry_price"] or 0 for t in tlist) / len(tlist)
        vol = sum(t["amount_usdc"] for t in tlist)
        print(f"  {outcome:12s}: {len(tlist):3d} trades | P&L: ${pnl:+7.2f} | "
              f"Avg entry: {avg_entry:.3f} | Volume: ${vol:.2f}")

    settled_count = len(by_outcome.get("WIN", [])) + len(by_outcome.get("LOSS", [])) + len(by_outcome.get("EARLY_EXIT", []))
    print(f"\n  Total settled P&L: ${total_pnl_actual:+.2f}  ({settled_count} trades)")

    # -- For each early-exited trade, find what would have happened ------
    early_exits = by_outcome.get("EARLY_EXIT", [])
    if not early_exits:
        print("\nNo early exits in this period.")
        return

    print()
    print("=" * 70)
    print(f"EARLY EXIT ANALYSIS ({len(early_exits)} trades)")
    print("=" * 70)

    # Try to determine the actual outcome by matching with paper trades
    # (paper trader runs in parallel and settles normally)
    rescued = []      # early exit was profitable AND settlement would have been a loss
    regretted = []    # early exit but settlement would have been a win
    neutral = []      # same outcome either way
    unknown = []      # can't determine

    for t in early_exits:
        slug = t["market_slug"]

        # Find matching paper trade for the same market
        paper = conn.execute(
            "SELECT side, outcome, pnl, entry_price FROM paper_trades "
            "WHERE market_slug = ? AND outcome IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1",
            (slug,),
        ).fetchone()

        if not paper:
            unknown.append(t)
            continue

        # Paper trade outcome tells us how the market resolved
        market_resolved = paper["outcome"]  # WIN or LOSS from paper's perspective

        # But paper may have traded the same or different side
        # The market_slug resolution is the same regardless of side
        # If paper side == our side, paper outcome == our hypothetical outcome
        # If paper side != our side, our hypothetical outcome is the opposite

        if paper["side"] == t["side"]:
            hypothetical_outcome = market_resolved
        else:
            hypothetical_outcome = "WIN" if market_resolved == "LOSS" else "LOSS"

        # Calculate hypothetical P&L if we had held to settlement
        entry = t["entry_price"]
        size = t["amount_usdc"]
        tokens = size / entry

        if hypothetical_outcome == "WIN":
            # Payout = tokens * 1.0, minus what we paid, minus fees
            gross = tokens * 1.0 - size
            # Polymarket fee on profit (2% effective with exponent)
            fee_rate = 0.25
            fee = max(0, gross) * fee_rate * (entry ** (fee_rate - 1)) if gross > 0 else 0
            # Simplified: fee ≈ profit * adjusted_rate
            # Actually from paper_trader: fee = profit * FEE_RATE * entry_price^(FEE_EXPONENT-1)...
            # Let's just estimate: profit * 0.02 as rough Polymarket fee
            hyp_pnl = gross * 0.98  # rough fee estimate
        else:
            hyp_pnl = -size  # total loss

        t["hyp_outcome"] = hypothetical_outcome
        t["hyp_pnl"] = hyp_pnl
        t["paper_outcome"] = market_resolved
        t["paper_side"] = paper["side"]

        if hypothetical_outcome == "LOSS":
            rescued.append(t)  # We exited early and would have lost --GOOD
        else:
            regretted.append(t)  # We exited early but would have won --BAD

    # -- Print detailed results -------------------------------------------
    print(f"\n  Rescued (exited early, would have LOST):    {len(rescued)}")
    print(f"  Regretted (exited early, would have WON):   {len(regretted)}")
    print(f"  Unknown (no paper trade match):             {len(unknown)}")

    # -- Rescued trades ---------------------------------------------------
    if rescued:
        print(f"\n  {'-'*60}")
        print(f"  RESCUED -- Early exit saved us from a loss")
        print(f"  {'-'*60}")
        total_exit_pnl = 0
        total_hyp_pnl = 0
        for t in rescued:
            ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M")
            saved = t["pnl"] - t["hyp_pnl"]
            total_exit_pnl += t["pnl"]
            total_hyp_pnl += t["hyp_pnl"]
            print(f"    #{t['id']:3d} {ts} {t['side']:4s} entry={t['entry_price']:.3f} "
                  f"size=${t['amount_usdc']:.2f} | "
                  f"EXIT: ${t['pnl']:+.2f} | HOLD: ${t['hyp_pnl']:+.2f} | "
                  f"SAVED: ${saved:+.2f}")
        print(f"\n    Subtotal --EXIT: ${total_exit_pnl:+.2f} vs HOLD: ${total_hyp_pnl:+.2f} "
              f"-> Saved ${total_exit_pnl - total_hyp_pnl:+.2f}")

    # -- Regretted trades -------------------------------------------------
    if regretted:
        print(f"\n  {'-'*60}")
        print(f"  REGRETTED --Early exit missed a win")
        print(f"  {'-'*60}")
        total_exit_pnl = 0
        total_hyp_pnl = 0
        for t in regretted:
            ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M")
            missed = t["hyp_pnl"] - t["pnl"]
            total_exit_pnl += t["pnl"]
            total_hyp_pnl += t["hyp_pnl"]
            print(f"    #{t['id']:3d} {ts} {t['side']:4s} entry={t['entry_price']:.3f} "
                  f"size=${t['amount_usdc']:.2f} | "
                  f"EXIT: ${t['pnl']:+.2f} | HOLD: ${t['hyp_pnl']:+.2f} | "
                  f"MISSED: ${missed:+.2f}")
        print(f"\n    Subtotal --EXIT: ${total_exit_pnl:+.2f} vs HOLD: ${total_hyp_pnl:+.2f} "
              f"-> Missed ${total_hyp_pnl - total_exit_pnl:+.2f}")

    # -- Unknown trades ---------------------------------------------------
    if unknown:
        print(f"\n  {'-'*60}")
        print(f"  UNKNOWN --No paper trade to compare ({len(unknown)} trades)")
        print(f"  {'-'*60}")
        for t in unknown:
            ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M")
            print(f"    #{t['id']:3d} {ts} {t['side']:4s} entry={t['entry_price']:.3f} "
                  f"size=${t['amount_usdc']:.2f} exit_pnl=${t['pnl']:+.2f}")

    # -- Summary ----------------------------------------------------------
    print()
    print("=" * 70)
    print("SUMMARY: EARLY EXIT IMPACT")
    print("=" * 70)

    known_exits = rescued + regretted
    if not known_exits:
        print("  No matched early exit trades to analyze.")
        return

    actual_pnl_exits = sum(t["pnl"] for t in known_exits)
    hyp_pnl_exits = sum(t["hyp_pnl"] for t in known_exits)
    delta = actual_pnl_exits - hyp_pnl_exits

    print(f"\n  Matched early exits:   {len(known_exits)}")
    print(f"  Rescued (saved loss):  {len(rescued)} ({len(rescued)/len(known_exits)*100:.0f}%)")
    print(f"  Regretted (missed win):{len(regretted)} ({len(regretted)/len(known_exits)*100:.0f}%)")
    print()
    print(f"  Actual P&L (early exits):      ${actual_pnl_exits:+.2f}")
    print(f"  Hypothetical P&L (hold all):   ${hyp_pnl_exits:+.2f}")
    print(f"  Net benefit of early exit:     ${delta:+.2f}")
    print()

    # Now compute overall with vs without
    other_pnl = sum(t["pnl"] or 0 for t in trades if t["outcome"] in ("WIN", "LOSS"))

    print(f"  Overall actual P&L (all settled):         ${other_pnl + actual_pnl_exits:+.2f}")
    print(f"  Overall hypothetical P&L (no early exit): ${other_pnl + hyp_pnl_exits:+.2f}")
    print(f"  Early exit advantage:                     ${delta:+.2f}")

    # -- By entry price tier ----------------------------------------------
    print()
    print("=" * 70)
    print("EARLY EXIT IMPACT BY ENTRY PRICE TIER")
    print("=" * 70)

    tiers = {
        "< 0.35 (threshold: 0.60)": lambda e: e < 0.35,
        "0.35-0.50 (threshold: 0.65)": lambda e: 0.35 <= e < 0.50,
        ">= 0.50 (threshold: 0.95)": lambda e: e >= 0.50,
    }

    for tier_name, tier_filter in tiers.items():
        tier_trades = [t for t in known_exits if tier_filter(t["entry_price"])]
        if not tier_trades:
            print(f"\n  {tier_name}: no trades")
            continue

        tier_rescued = [t for t in tier_trades if t["hyp_outcome"] == "LOSS"]
        tier_regretted = [t for t in tier_trades if t["hyp_outcome"] == "WIN"]
        tier_actual = sum(t["pnl"] for t in tier_trades)
        tier_hyp = sum(t["hyp_pnl"] for t in tier_trades)

        print(f"\n  {tier_name}:")
        print(f"    Trades: {len(tier_trades)} | Rescued: {len(tier_rescued)} | Regretted: {len(tier_regretted)}")
        print(f"    Actual P&L: ${tier_actual:+.2f} | Hyp P&L: ${tier_hyp:+.2f} | Delta: ${tier_actual - tier_hyp:+.2f}")
        if tier_rescued:
            avg_saved = sum(t["pnl"] - t["hyp_pnl"] for t in tier_rescued) / len(tier_rescued)
            print(f"    Avg saved per rescue: ${avg_saved:+.2f}")
        if tier_regretted:
            avg_missed = sum(t["hyp_pnl"] - t["pnl"] for t in tier_regretted) / len(tier_regretted)
            print(f"    Avg missed per regret: ${avg_missed:+.2f}")


if __name__ == "__main__":
    main()
