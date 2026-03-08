"""Quick PnL breakdown for live trades."""
import sqlite3

c = sqlite3.connect("btc_edge.db")

print("=== Live Trade PnL Breakdown ===\n")

rows = c.execute(
    "SELECT outcome, COUNT(*), COALESCE(SUM(amount_usdc),0), COALESCE(SUM(pnl),0) "
    "FROM live_trades WHERE success=1 GROUP BY outcome ORDER BY outcome"
).fetchall()

total_pnl = 0
for outcome, count, spent, pnl in rows:
    label = outcome or "UNSETTLED"
    print(f"  {label:12s}: {count:3d} trades, spent ${spent:7.2f}, pnl ${pnl:+8.2f}")
    total_pnl += pnl

print(f"\n  TOTAL DB PnL: ${total_pnl:+.2f}")
print(f"  Deposited:    $73.00")
print(f"  Expected:     ${73 + total_pnl:.2f}")
print(f"  Actual:       $111.00")
print(f"  Gap:          ${111 - 73 - total_pnl:.2f}")

# Check for maker fills (trades we didn't place but got filled on)
print("\n=== Possible Gaps ===")
print("  1. Maker fills (whales hitting our GTC orders) - untracked bonus P&L")
print("  2. Tokens won but not yet redeemed (sitting in wallet as positions)")
print("  3. Early exit PnL may differ from DB estimate")

# Show early exit detail
rows = c.execute(
    "SELECT id, market_slug, side, amount_usdc, pnl, entry_price "
    "FROM live_trades WHERE outcome='EARLY_EXIT'"
).fetchall()
if rows:
    print(f"\n=== Early Exit Details ({len(rows)} trades) ===")
    for r in rows:
        print(f"  #{r[0]:3d} {r[2]:4s} ${r[3]:.2f} -> pnl ${r[4]:+.2f} (entry {r[5]:.3f})")
    total_ee = sum(r[4] for r in rows)
    print(f"  Total early exit PnL: ${total_ee:+.2f}")
