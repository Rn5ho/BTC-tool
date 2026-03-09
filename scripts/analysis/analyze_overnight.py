import sqlite3

conn = sqlite3.connect('btc_edge.db')
c = conn.cursor()

print('=== TAKER-ONLY DUPLICATES (excluding maker_fill) ===')
c.execute("""SELECT market_slug, COUNT(*) as n, GROUP_CONCAT(ROUND(amount_usdc,2))
FROM live_trades WHERE success=1 AND COALESCE(trade_tag, '') <> 'maker_fill'
GROUP BY market_slug HAVING n > 1 ORDER BY n DESC LIMIT 15""")
for row in c.fetchall():
    print('%s: %d taker trades | amounts: %s' % (row[0][-15:], row[1], row[2]))

print()
print('=== ALL TRADES ON WORST SLUG (6 trades on 1772397000) ===')
c.execute("""SELECT id, side, ROUND(amount_usdc,2), outcome, trade_tag,
    SUBSTR(order_id, 1, 30), ROUND(entry_price,3)
FROM live_trades WHERE success=1 AND market_slug LIKE '%1772397000' ORDER BY id""")
for row in c.fetchall():
    tag = row[4] or 'taker'
    oid = row[5] or ''
    print('id:%3d | %5s | $%5s | %10s | %10s | entry:%s | %s' % (row[0], row[1], row[2], row[3], tag, row[6], oid))

print()
print('=== SUMMARY: dupes in duplicate slugs ===')
c.execute("""SELECT
  SUM(CASE WHEN trade_tag='maker_fill' THEN 1 ELSE 0 END) as maker_fills,
  SUM(CASE WHEN COALESCE(trade_tag, '') <> 'maker_fill' THEN 1 ELSE 0 END) as taker_trades,
  COUNT(*) as total
FROM live_trades WHERE success=1 AND market_slug IN (
    SELECT market_slug FROM live_trades WHERE success=1 GROUP BY market_slug HAVING COUNT(*) > 1
)""")
row = c.fetchone()
print('In duplicate slugs: %d maker fills, %d taker trades, %d total' % row)

print()
c.execute("""SELECT COUNT(*) FROM (
  SELECT market_slug FROM live_trades
  WHERE success=1 AND COALESCE(trade_tag, '') <> 'maker_fill'
  GROUP BY market_slug HAVING COUNT(*) > 1
)""")
print('Slugs with multiple TAKER orders: %d' % c.fetchone()[0])

print()
print('=== PnL FROM EXTRA TAKER TRADES (duplicate cost) ===')
c.execute("""SELECT ROUND(SUM(pnl), 2) FROM live_trades
WHERE success=1 AND pnl IS NOT NULL
AND COALESCE(trade_tag, '') <> 'maker_fill'
AND id NOT IN (
    SELECT MIN(id) FROM live_trades
    WHERE success=1 AND COALESCE(trade_tag, '') <> 'maker_fill'
    GROUP BY market_slug
)""")
print('PnL from duplicate TAKER trades: $%s' % c.fetchone()[0])

c.execute("""SELECT COUNT(*) FROM live_trades
WHERE success=1 AND COALESCE(trade_tag, '') <> 'maker_fill'
AND id NOT IN (
    SELECT MIN(id) FROM live_trades
    WHERE success=1 AND COALESCE(trade_tag, '') <> 'maker_fill'
    GROUP BY market_slug
)""")
print('Number of duplicate TAKER trades: %d' % c.fetchone()[0])

print()
print('=== EXPLORATION TRADES DETAIL ===')
c.execute("""SELECT id, side, ROUND(amount_usdc,2), outcome, ROUND(pnl,2), ROUND(entry_price,3), market_slug
FROM live_trades WHERE success=1 AND trade_tag='exploration' ORDER BY id""")
for row in c.fetchall():
    print('id:%3d | %5s | $%5s | %8s | pnl:$%6s | entry:%s' % (row[0], row[1], row[2], row[3], row[4], row[5]))

conn.close()
