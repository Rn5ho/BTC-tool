"""Find 5-minute trend streaks and quantify damage from betting against them."""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    now = int(datetime.now(timezone.utc).timestamp())
    t24h_ms = (now - 86400) * 1000

    # Get all live trades ordered by time
    cursor = conn.execute(
        "SELECT id, timestamp, side, entry_price, outcome, pnl, "
        "regime_state, regime_strength, trade_tag, market_slug "
        "FROM live_trades "
        "WHERE timestamp >= ? AND success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp",
        (t24h_ms,),
    )
    trades = [dict(t) for t in cursor.fetchall()]

    # Each trade is on a 5-min window. The market_slug or timestamp tells us
    # which window. We can also determine what actually happened (UP or DOWN)
    # from the outcome + side:
    # - If side=UP and outcome=WIN -> market resolved UP
    # - If side=UP and outcome=LOSS -> market resolved DOWN
    # - If side=DOWN and outcome=WIN -> market resolved DOWN
    # - If side=DOWN and outcome=LOSS -> market resolved UP
    # - EARLY_EXIT -> use paper trade for resolution

    # Get paper trades to determine resolution for early-exited live trades
    paper_cursor = conn.execute(
        "SELECT market_slug, side, outcome "
        "FROM paper_trades "
        "WHERE timestamp >= ? AND outcome IS NOT NULL",
        (t24h_ms,),
    )
    paper_map = {}
    for p in paper_cursor:
        paper_map[p["market_slug"]] = dict(p)

    # Determine actual market resolution for each trade
    for t in trades:
        if t["outcome"] == "WIN":
            t["resolution"] = t["side"]  # market went our way
        elif t["outcome"] == "LOSS":
            t["resolution"] = "DOWN" if t["side"] == "UP" else "UP"
        elif t["outcome"] == "EARLY_EXIT":
            # Check paper trade
            paper = paper_map.get(t["market_slug"])
            if paper:
                if paper["outcome"] == "WIN":
                    t["resolution"] = paper["side"]
                elif paper["outcome"] == "LOSS":
                    t["resolution"] = "DOWN" if paper["side"] == "UP" else "UP"
                else:
                    t["resolution"] = "?"
            else:
                t["resolution"] = "?"

    # Now find consecutive resolution streaks (actual market direction)
    print("5-MINUTE MARKET RESOLUTION STREAKS (last 24h)")
    print("=" * 90)
    print()

    # Group trades by 5-min window (round timestamp to 5-min boundary)
    windows = []
    seen_slugs = set()
    for t in trades:
        slug = t["market_slug"]
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        windows.append(t)

    # Find consecutive resolution streaks
    streaks = []
    current_dir = None
    current_streak = []

    for w in windows:
        res = w["resolution"]
        if res == "?":
            # Unknown breaks the streak
            if len(current_streak) >= 3:
                streaks.append((current_dir, current_streak[:]))
            current_dir = None
            current_streak = []
            continue

        if res == current_dir:
            current_streak.append(w)
        else:
            if len(current_streak) >= 3:
                streaks.append((current_dir, current_streak[:]))
            current_dir = res
            current_streak = [w]

    if len(current_streak) >= 3:
        streaks.append((current_dir, current_streak[:]))

    # Sort by streak length
    streaks.sort(key=lambda x: len(x[1]), reverse=True)

    total_streak_damage = 0
    total_streak_trades = 0

    for direction, streak in streaks:
        # Find ALL trades during this streak (not just one per window)
        start_ts = streak[0]["timestamp"]
        end_ts = streak[-1]["timestamp"]
        all_in_streak = [t for t in trades if start_ts <= t["timestamp"] <= end_ts]

        start_str = datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc).strftime("%H:%M")
        end_str = datetime.fromtimestamp(end_ts / 1000, tz=timezone.utc).strftime("%H:%M")

        against = [t for t in all_in_streak if t["side"] != direction]
        with_trend = [t for t in all_in_streak if t["side"] == direction]

        against_pnl = sum(t["pnl"] or 0 for t in against)
        with_pnl = sum(t["pnl"] or 0 for t in with_trend)
        total_pnl = against_pnl + with_pnl

        print(f"  {direction:4s} streak: {len(streak)} windows | {start_str}-{end_str} UTC")
        print(f"    Total trades: {len(all_in_streak)} | P&L: ${total_pnl:+.2f}")

        if against:
            a_w = sum(1 for t in against if t["outcome"] == "WIN")
            a_l = sum(1 for t in against if t["outcome"] == "LOSS")
            a_e = sum(1 for t in against if t["outcome"] == "EARLY_EXIT")
            print(f"    AGAINST trend: {len(against)} trades ({a_w}W/{a_l}L/{a_e}E) "
                  f"P&L: ${against_pnl:+.2f}")
            total_streak_damage += against_pnl
            total_streak_trades += len(against)

        if with_trend:
            w_w = sum(1 for t in with_trend if t["outcome"] == "WIN")
            w_l = sum(1 for t in with_trend if t["outcome"] == "LOSS")
            w_e = sum(1 for t in with_trend if t["outcome"] == "EARLY_EXIT")
            print(f"    WITH trend:    {len(with_trend)} trades ({w_w}W/{w_l}L/{w_e}E) "
                  f"P&L: ${with_pnl:+.2f}")

        # Show individual trades
        for t in all_in_streak:
            ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M")
            marker = "<<<" if t["side"] != direction else ""
            print(f"      #{t['id']:3d} {ts} bet {t['side']:4s} (mkt={direction}) "
                  f"entry={t['entry_price']:.3f} -> {t['outcome']:10s} "
                  f"${t['pnl']:+.2f} {marker}")
        print()

    # Summary
    print("=" * 90)
    print("TREND STREAK SUMMARY")
    print("=" * 90)
    print(f"  Streaks found (>= 3 windows): {len(streaks)}")
    print(f"  Total against-trend trades in streaks: {total_streak_trades}")
    print(f"  Total against-trend P&L: ${total_streak_damage:+.2f}")
    print()

    # What if we had FLIPPED those against-trend trades?
    # Instead of betting against, bet WITH the trend
    # Against-trend LOSS -> would be WIN (payout = 1/entry - 1 after fees)
    # Against-trend WIN -> would be LOSS (lose full bet)
    # Against-trend EARLY_EXIT -> harder to estimate
    print("HYPOTHETICAL: FLIP AGAINST-TREND TRADES")
    print("=" * 90)

    actual_pnl = 0
    flipped_pnl = 0
    skip_pnl = 0

    all_against = []
    for direction, streak in streaks:
        start_ts = streak[0]["timestamp"]
        end_ts = streak[-1]["timestamp"]
        against = [t for t in trades
                    if start_ts <= t["timestamp"] <= end_ts and t["side"] != direction]
        all_against.extend(against)

    for t in all_against:
        actual_pnl += t["pnl"] or 0

        # If we had bet WITH the trend instead:
        # We'd be buying the trend side. The entry price for the trend side
        # = 1 - current_entry_price (approximately, since bid/ask differ)
        trend_entry = 1.0 - t["entry_price"]
        if t["outcome"] == "LOSS":
            # Original: bet against trend, lost (market went with trend)
            # Flipped: bet WITH trend, would WIN
            tokens = t["amount_usdc"] / trend_entry
            gross_profit = tokens * 1.0 - t["amount_usdc"]
            flipped_pnl += gross_profit * 0.98  # rough fee
        elif t["outcome"] == "WIN":
            # Original: bet against trend, won (market went against trend)
            # Flipped: bet WITH trend, would LOSE
            flipped_pnl += -t["amount_usdc"]
        elif t["outcome"] == "EARLY_EXIT":
            # Conservative: assume flipped would also early exit at similar profit
            flipped_pnl += t["pnl"] or 0

    print(f"  Against-trend trades in streaks: {len(all_against)}")
    print(f"  Actual P&L (bet against):    ${actual_pnl:+.2f}")
    print(f"  Hypothetical (bet WITH):     ${flipped_pnl:+.2f}")
    print(f"  Hypothetical (SKIP these):   $0.00 (save ${-actual_pnl:+.2f})")
    print()
    print(f"  Flip improvement:  ${flipped_pnl - actual_pnl:+.2f}")
    print(f"  Skip improvement:  ${-actual_pnl:+.2f}")

    # Overall system impact
    total_system_pnl = sum(t["pnl"] or 0 for t in trades)
    print()
    print(f"  System P&L as-is:        ${total_system_pnl:+.2f}")
    print(f"  System P&L with flips:   ${total_system_pnl - actual_pnl + flipped_pnl:+.2f}")
    print(f"  System P&L with skips:   ${total_system_pnl - actual_pnl:+.2f}")


if __name__ == "__main__":
    main()
