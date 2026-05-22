"""Recon shadow_windows: semantics, null rates, sample rows."""
import sqlite3
db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

n = c.execute('SELECT COUNT(*) FROM shadow_windows').fetchone()[0]
print(f'total shadow_windows: {n}')

# null / zero rates for key cols
for col in ('up_open_ask','down_open_ask','up_max_bid','down_max_bid',
            'btc_price_start','btc_price_end','traded_side','traded_tag','regime_state'):
    nn = c.execute(f'SELECT COUNT(*) FROM shadow_windows WHERE {col} IS NULL').fetchone()[0]
    nz = c.execute(f'SELECT COUNT(*) FROM shadow_windows WHERE {col}=0 OR {col}=""').fetchone()[0]
    print(f'  {col:18s} null={nn:6d}  zero/empty={nz:6d}')

print('\ntraded_side distribution:')
for r in c.execute('SELECT traded_side, COUNT(*) n FROM shadow_windows GROUP BY traded_side'):
    print(f'  {r["traded_side"]}: {r["n"]}')

print('\ntraded_tag distribution:')
for r in c.execute('SELECT traded_tag, COUNT(*) n FROM shadow_windows GROUP BY traded_tag ORDER BY n DESC'):
    print(f'  {r["traded_tag"]}: {r["n"]}')

print('\nsample rows (recent):')
for r in c.execute('SELECT created_at,up_open_ask,down_open_ask,up_max_bid,down_max_bid,btc_price_start,btc_price_end,traded_side,traded_tag,regime_state FROM shadow_windows ORDER BY id DESC LIMIT 8'):
    print(f'  {r["created_at"]} up_ask={r["up_open_ask"]} dn_ask={r["down_open_ask"]} up_maxb={r["up_max_bid"]} dn_maxb={r["down_max_bid"]} btc {r["btc_price_start"]}->{r["btc_price_end"]} side={r["traded_side"]} tag={r["traded_tag"]} regime={r["regime_state"]}')

print('\nsample rows (oldest):')
for r in c.execute('SELECT created_at,up_open_ask,down_open_ask,up_max_bid,down_max_bid,btc_price_start,btc_price_end,traded_side,traded_tag FROM shadow_windows ORDER BY id ASC LIMIT 4'):
    print(f'  {r["created_at"]} up_ask={r["up_open_ask"]} dn_ask={r["down_open_ask"]} up_maxb={r["up_max_bid"]} dn_maxb={r["down_max_bid"]} btc {r["btc_price_start"]}->{r["btc_price_end"]} side={r["traded_side"]} tag={r["traded_tag"]}')
db.close()
