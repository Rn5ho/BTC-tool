"""Check recent live trade performance and UP/DOWN bias."""
import sqlite3

c = sqlite3.connect("btc_edge.db")

# Last 30 live trades
rows = c.execute("""
    SELECT id, side, outcome, pnl, entry_price, market_slug, timestamp
    FROM live_trades WHERE success=1
    ORDER BY id DESC LIMIT 30
""").fetchall()

print("=== Last 30 live trades (newest first) ===")
wins = losses = 0
total_pnl = 0
up_calls = down_calls = 0
for r in rows:
    tid, side, outcome, pnl, price, slug, ts = r
    pnl = pnl or 0
    outcome = outcome or "PENDING"
    total_pnl += pnl
    if outcome == "WIN":
        wins += 1
    elif outcome == "LOSS":
        losses += 1
    if side == "UP":
        up_calls += 1
    else:
        down_calls += 1
    print(f"  #{tid:3d} {side:4s} {outcome:10s} pnl={pnl:+6.2f} entry={price:.3f} {slug[-15:]}")

settled = wins + losses
wr = wins / settled * 100 if settled else 0
print(f"\nLast 30: UP calls={up_calls}, DOWN calls={down_calls}")
print(f"Settled: {settled} | W={wins} L={losses} | WR={wr:.0f}% | PnL=${total_pnl:+.2f}")

# Overall side distribution
rows2 = c.execute("""
    SELECT side, COUNT(*),
           SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),
           SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END),
           COALESCE(SUM(pnl), 0)
    FROM live_trades WHERE success=1 AND outcome IS NOT NULL
    GROUP BY side
""").fetchall()

print("\n=== All-time by side ===")
for side, cnt, w, l, pnl in rows2:
    wr2 = w / (w + l) * 100 if (w + l) else 0
    print(f"  {side:4s}: {cnt} trades, W={w} L={l}, WR={wr2:.1f}%, PnL=${pnl:+.2f}")

# Check paper trades too for the UP bias
rows3 = c.execute("""
    SELECT side, COUNT(*)
    FROM paper_trades
    WHERE id > (SELECT MAX(id) - 30 FROM paper_trades)
    GROUP BY side
""").fetchall()
print(f"\nLast ~30 paper trades by side: {dict(rows3)}")
