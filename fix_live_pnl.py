"""One-time fix: backfill entry_price and recalculate PnL for old live trades."""
import sqlite3

DB_PATH = "btc_edge.db"
FEE_RATE = 0.25
FEE_EXPONENT = 2

c = sqlite3.connect(DB_PATH)

rows = c.execute("""
    SELECT lt.id, lt.side, lt.amount_usdc, lt.outcome, pt.entry_price
    FROM live_trades lt
    JOIN paper_trades pt ON lt.market_slug = pt.market_slug AND lt.side = pt.side
    WHERE lt.success = 1 AND (lt.entry_price IS NULL OR lt.entry_price = 0)
    AND lt.outcome IS NOT NULL
""").fetchall()

fixed = 0
for tid, side, amount, outcome, ep in rows:
    fee_factor = FEE_RATE * (ep * (1 - ep)) ** FEE_EXPONENT
    shares = (amount / ep) * (1.0 - fee_factor)
    pnl = (shares - amount) if outcome == "WIN" else -amount
    c.execute("UPDATE live_trades SET entry_price = ?, pnl = ? WHERE id = ?", (ep, pnl, tid))
    fixed += 1

c.commit()

# Report
r = c.execute("SELECT COALESCE(SUM(pnl), 0) FROM live_trades WHERE pnl IS NOT NULL").fetchone()
total_pnl = r[0]
wins = c.execute("SELECT COUNT(*) FROM live_trades WHERE outcome = 'WIN'").fetchone()[0]
losses = c.execute("SELECT COUNT(*) FROM live_trades WHERE outcome = 'LOSS'").fetchone()[0]
settled = wins + losses
wr = wins / settled * 100 if settled > 0 else 0

print(f"Fixed {fixed} trades")
print(f"Settled: {settled} | Wins: {wins} | Losses: {losses} | WR: {wr:.1f}%")
print(f"Total live P&L: ${total_pnl:+.2f}")
c.close()
