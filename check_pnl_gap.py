"""Find the gap between simulated P&L and actual CLOB balance."""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Trades by tag
    cursor = conn.execute(
        "SELECT trade_tag, COUNT(*) as cnt, SUM(pnl) as total_pnl, "
        "SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins, "
        "SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses, "
        "SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as exits "
        "FROM live_trades WHERE success = 1 AND outcome IS NOT NULL "
        "GROUP BY trade_tag"
    )
    print("TRADES BY TAG:")
    total_pnl_all = 0
    for r in cursor:
        tag = r["trade_tag"] or "none"
        pnl = r["total_pnl"] or 0
        total_pnl_all += pnl
        print(f"  {tag:15s}: {r['cnt']:3d} trades | "
              f"{r['wins']}W/{r['losses']}L/{r['exits']}E | P&L: ${pnl:+.2f}")

    print(f"\n  Total tracked P&L: ${total_pnl_all:+.2f}")
    print(f"  Starting balance: $73.00")
    print(f"  Simulated balance: ${73 + total_pnl_all:.2f}")
    print(f"  Actual balance: ~$65-70")
    print(f"  Gap: ~${65 - (73 + total_pnl_all):.2f} to ${70 - (73 + total_pnl_all):.2f}")
    print()

    # Unsettled trades
    cursor2 = conn.execute(
        "SELECT id, timestamp, side, amount_usdc, entry_price, market_slug "
        "FROM live_trades WHERE success = 1 AND outcome IS NULL "
        "ORDER BY timestamp"
    )
    unsettled = list(cursor2)
    print(f"UNSETTLED TRADES: {len(unsettled)}")
    locked = 0
    for t in unsettled:
        ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        print(f"  #{t['id']} {ts} {t['side']} ${t['amount_usdc']:.2f} "
              f"entry={t['entry_price']:.3f} slug=...{t['market_slug'][-25:]}")
        locked += t["amount_usdc"]
    print(f"  Total locked in unsettled: ${locked:.2f}")
    print()

    # Maker fills
    cursor3 = conn.execute(
        "SELECT id, timestamp, side, amount_usdc, entry_price, outcome, pnl "
        "FROM live_trades WHERE trade_tag = 'maker_fill' AND success = 1 "
        "ORDER BY timestamp"
    )
    makers = list(cursor3)
    print(f"MAKER FILLS: {len(makers)}")
    maker_pnl = 0
    for m in makers:
        ts = datetime.fromtimestamp(m["timestamp"] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        pnl = m["pnl"] or 0
        maker_pnl += pnl
        print(f"  #{m['id']} {ts} {m['side']} ${m['amount_usdc']:.2f} "
              f"entry={m['entry_price']:.3f} -> {m['outcome']} ${pnl:+.2f}")
    print(f"  Maker fill P&L: ${maker_pnl:+.2f}")
    print()

    # Check for duplicate slugs (multiple trades on same market)
    cursor4 = conn.execute(
        "SELECT market_slug, COUNT(*) as cnt, SUM(amount_usdc) as vol, SUM(pnl) as pnl "
        "FROM live_trades WHERE success = 1 AND outcome IS NOT NULL "
        "GROUP BY market_slug HAVING cnt > 1 "
        "ORDER BY cnt DESC LIMIT 20"
    )
    dupes = list(cursor4)
    print(f"MARKETS WITH MULTIPLE TRADES: {len(dupes)}")
    dupe_extra_pnl = 0
    for d in dupes:
        slug_short = d["market_slug"][-30:]
        dupe_extra_pnl += d["pnl"] or 0
        print(f"  ...{slug_short}: {d['cnt']} trades, ${d['vol']:.2f} vol, ${d['pnl'] or 0:+.2f}")

    print()

    # Check success=0 trades (failed orders that might have consumed gas)
    cursor5 = conn.execute(
        "SELECT COUNT(*) as cnt FROM live_trades WHERE success = 0"
    )
    failed = cursor5.fetchone()["cnt"]
    print(f"FAILED ORDERS: {failed}")

    # P&L sanity: sum from taker trades only (no maker_fill)
    cursor6 = conn.execute(
        "SELECT SUM(pnl) as total FROM live_trades "
        "WHERE success = 1 AND outcome IS NOT NULL AND "
        "(trade_tag IS NULL OR trade_tag != 'maker_fill')"
    )
    taker_pnl = cursor6.fetchone()["total"] or 0
    print(f"\nTaker-only P&L: ${taker_pnl:+.2f}")
    print(f"Maker P&L: ${maker_pnl:+.2f}")
    print(f"Combined: ${taker_pnl + maker_pnl:+.2f}")
    print(f"Simulated from $73: ${73 + taker_pnl + maker_pnl:.2f}")


if __name__ == "__main__":
    main()
