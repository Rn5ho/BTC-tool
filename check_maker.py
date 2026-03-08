"""Check maker fills in DB."""
import sqlite3

c = sqlite3.connect("btc_edge.db")
rows = c.execute(
    "SELECT id, market_slug, side, amount_usdc, entry_price, outcome, pnl "
    "FROM live_trades WHERE trade_tag = 'maker_fill'"
).fetchall()

print(f"Maker fills in DB: {len(rows)}")
for r in rows:
    slug_end = r[1][-10:] if r[1] else "?"
    outcome = r[5] or "N/A"
    pnl = r[6] or 0
    print(f"  #{r[0]:3d} {r[2]:4s} {slug_end} amt=${r[3]:.2f} price={r[4]:.3f} {outcome} pnl=${pnl:+.2f}")

total_maker_pnl = sum((r[6] or 0) for r in rows)
print(f"\nTotal maker fill PnL: ${total_maker_pnl:+.2f}")

# Updated totals
r = c.execute(
    "SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM live_trades WHERE success=1 AND outcome IS NOT NULL"
).fetchone()
print(f"\nAll live trades: {r[0]} settled, total PnL: ${r[1]:+.2f}")
print(f"Deposited: $73, Expected portfolio: ${73 + r[1]:.2f}")
