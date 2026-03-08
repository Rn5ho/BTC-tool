"""What if we had flipped against-trend trades at various strength thresholds?"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    now = int(datetime.now(timezone.utc).timestamp())
    t24h_ms = (now - 86400) * 1000

    cursor = conn.execute(
        "SELECT id, timestamp, side, entry_price, outcome, pnl, "
        "regime_state, regime_strength, market_slug "
        "FROM live_trades "
        "WHERE timestamp >= ? AND success = 1 AND outcome IS NOT NULL "
        "AND regime_state IN ('trending_up', 'trending_down') "
        "ORDER BY timestamp",
        (t24h_ms,),
    )
    trending = [dict(t) for t in cursor.fetchall()]

    against = []
    for t in trending:
        if t["regime_state"] == "trending_up" and t["side"] == "DOWN":
            against.append(t)
        elif t["regime_state"] == "trending_down" and t["side"] == "UP":
            against.append(t)

    # Paper trades for market resolution
    paper_cursor = conn.execute(
        "SELECT market_slug, side, outcome FROM paper_trades "
        "WHERE timestamp >= ? AND outcome IS NOT NULL",
        (t24h_ms,),
    )
    paper_map = {}
    for p in paper_cursor:
        paper_map[p["market_slug"]] = dict(p)

    print("HYPOTHETICAL: FLIP AGAINST-TREND TRADES AT VARIOUS THRESHOLDS")
    print("=" * 80)
    print()

    for threshold in [0.30, 0.35, 0.40, 0.50]:
        subset = [t for t in against if abs(t["regime_strength"] or 0) >= threshold]
        if not subset:
            continue

        actual_pnl = 0.0
        flipped_pnl = 0.0
        wins = 0
        losses = 0
        unknown = 0

        for t in subset:
            actual_pnl += t["pnl"] or 0

            paper = paper_map.get(t["market_slug"])
            if not paper:
                unknown += 1
                continue

            if paper["outcome"] == "WIN":
                market_went = paper["side"]
            else:
                market_went = "DOWN" if paper["side"] == "UP" else "UP"

            trend_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
            trend_entry = 1.0 - t["entry_price"]

            if market_went == trend_side:
                bet = 3.50
                tokens = bet / trend_entry
                gross = tokens * 1.0 - bet
                flipped_pnl += gross * 0.98
                wins += 1
            else:
                flipped_pnl += -3.50
                losses += 1

        total = wins + losses
        wr = wins / total * 100 if total else 0

        print(f"  Strength >= {threshold:.2f}: {len(subset)} trades")
        print(f"    Actual (against trend):  ${actual_pnl:+.2f}")
        print(f"    Flipped (with trend):    ${flipped_pnl:+.2f} ({wins}W/{losses}L, {wr:.0f}% WR)")
        print(f"    Skip (do nothing):       $0.00")
        print(f"    Flip vs actual:          ${flipped_pnl - actual_pnl:+.2f}")
        print(f"    Skip vs actual:          ${-actual_pnl:+.2f}")
        if unknown:
            print(f"    (unknown resolution: {unknown})")
        print()

    # Full picture
    print("=" * 80)
    print("FULL 24h SYSTEM PICTURE")
    print("=" * 80)

    non_trending_cursor = conn.execute(
        "SELECT pnl FROM live_trades "
        "WHERE timestamp >= ? AND success = 1 AND outcome IS NOT NULL "
        "AND (regime_state NOT IN ('trending_up', 'trending_down') OR regime_state IS NULL)",
        (t24h_ms,),
    )
    non_trend_pnl = sum(r["pnl"] or 0 for r in non_trending_cursor)
    against_pnl = sum(t["pnl"] or 0 for t in against)

    print(f"  Non-trending P&L:        ${non_trend_pnl:+.2f} (128 trades)")
    print(f"  Against-trend P&L:       ${against_pnl:+.2f} ({len(against)} trades)")
    print(f"  Current total:           $+0.95")
    print()
    print(f"  If SKIP trending:        ${non_trend_pnl:+.2f}")
    print(f"  If FLIP at 0.35:         see threshold table above")
    print()

    # Combined scenario: early exit + flip
    # Best case: non-trending stays same, trending gets flipped
    # Use 0.35 threshold flip numbers
    subset_35 = [t for t in against if abs(t["regime_strength"] or 0) >= 0.35]
    below_35 = [t for t in against if abs(t["regime_strength"] or 0) < 0.35]

    flip_pnl_35 = 0.0
    for t in subset_35:
        paper = paper_map.get(t["market_slug"])
        if not paper:
            continue
        if paper["outcome"] == "WIN":
            market_went = paper["side"]
        else:
            market_went = "DOWN" if paper["side"] == "UP" else "UP"
        trend_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
        trend_entry = 1.0 - t["entry_price"]
        if market_went == trend_side:
            bet = 3.50
            tokens = bet / trend_entry
            gross = tokens * 1.0 - bet
            flip_pnl_35 += gross * 0.98
        else:
            flip_pnl_35 += -3.50

    below_35_pnl = sum(t["pnl"] or 0 for t in below_35)

    print("BEST COMBINED SCENARIO (flip >= 0.35, skip < 0.35 trending)")
    print(f"  Non-trending:   ${non_trend_pnl:+.2f}")
    print(f"  Flipped (>=.35): ${flip_pnl_35:+.2f}")
    print(f"  Skipped (<.35):  $0.00 (was ${below_35_pnl:+.2f})")
    print(f"  Combined:        ${non_trend_pnl + flip_pnl_35:+.2f}")
    print()

    print("BEST COMBINED SCENARIO (flip >= 0.35, keep < 0.35 as-is)")
    print(f"  Non-trending:   ${non_trend_pnl:+.2f}")
    print(f"  Flipped (>=.35): ${flip_pnl_35:+.2f}")
    print(f"  Kept (<.35):     ${below_35_pnl:+.2f}")
    combo = non_trend_pnl + flip_pnl_35 + below_35_pnl
    print(f"  Combined:        ${combo:+.2f}")


if __name__ == "__main__":
    main()
