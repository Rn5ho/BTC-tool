"""Recon market_snapshots: timestamp format, slug format, snaps-per-window."""
import sqlite3
db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

print('sample market_snapshots rows:')
for r in c.execute('SELECT timestamp, slug, up_price, down_price, up_best_bid, down_best_bid, up_best_ask, down_best_ask, created_at FROM market_snapshots ORDER BY id DESC LIMIT 5'):
    print(dict(r))

print('\ntimestamp column type/range:')
r = c.execute('SELECT MIN(timestamp), MAX(timestamp), typeof(timestamp) FROM market_snapshots').fetchone()
print(r[0], r[1], r[2])

print('\nsample slugs from shadow_windows:')
for r in c.execute('SELECT market_slug FROM shadow_windows ORDER BY id DESC LIMIT 5'):
    print(' ', r['market_slug'])

# snaps per window for a recent slug
slug = c.execute('SELECT market_slug FROM shadow_windows ORDER BY id DESC LIMIT 1 OFFSET 5').fetchone()[0]
n = c.execute('SELECT COUNT(*) FROM market_snapshots WHERE slug=?', (slug,)).fetchone()[0]
print(f'\nsnapshots for {slug}: {n}')
print('first/last 3 snaps (timestamp, up_best_bid, down_best_bid, btc_price):')
for r in c.execute('SELECT timestamp, up_best_bid, down_best_bid, btc_price, created_at FROM market_snapshots WHERE slug=? ORDER BY timestamp LIMIT 3', (slug,)):
    print('  ', dict(r))
for r in c.execute('SELECT timestamp, up_best_bid, down_best_bid, btc_price, created_at FROM market_snapshots WHERE slug=? ORDER BY timestamp DESC LIMIT 3', (slug,)):
    print('  ', dict(r))

# distribution of snaps/window
print('\nsnaps-per-window distribution (sample 500 recent windows):')
import statistics
counts=[]
for r in c.execute('SELECT market_slug FROM shadow_windows ORDER BY id DESC LIMIT 500'):
    cnt = c.execute('SELECT COUNT(*) FROM market_snapshots WHERE slug=?', (r['market_slug'],)).fetchone()[0]
    counts.append(cnt)
counts.sort()
print(f'  min={counts[0]} p10={counts[len(counts)//10]} median={counts[len(counts)//2]} p90={counts[len(counts)*9//10]} max={counts[-1]}')
print(f'  windows with 0 snaps: {sum(1 for x in counts if x==0)}/500')
db.close()
