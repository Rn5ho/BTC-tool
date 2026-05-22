"""Recon: can we join skipped_windows (model_side) to shadow_windows (settlement)?"""
import sqlite3
db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

print('skip_reason distribution (all time):')
for r in c.execute('SELECT skip_reason, COUNT(*) n, MIN(created_at) mn, MAX(created_at) mx FROM skipped_windows GROUP BY skip_reason ORDER BY n DESC'):
    print(f'  {r["n"]:6d}  {r["skip_reason"]}   [{r["mn"]} .. {r["mx"]}]')

print('\nmodel_side distribution in skipped_windows:')
for r in c.execute('SELECT model_side, COUNT(*) n FROM skipped_windows GROUP BY model_side'):
    print(f'  {r["model_side"]}: {r["n"]}')

# join test
n_join = c.execute('''SELECT COUNT(*) FROM skipped_windows s
                      JOIN shadow_windows w ON s.market_slug = w.market_slug''').fetchone()[0]
n_skip = c.execute('SELECT COUNT(*) FROM skipped_windows').fetchone()[0]
print(f'\nskipped_windows rows: {n_skip}')
print(f'joined to shadow_windows on slug: {n_join}')

# joined with model_side not null
n_good = c.execute('''SELECT COUNT(*) FROM skipped_windows s
                      JOIN shadow_windows w ON s.market_slug = w.market_slug
                      WHERE s.model_side IS NOT NULL''').fetchone()[0]
print(f'joined AND model_side not null: {n_good}')

# date range of the joinable set
r = c.execute('''SELECT MIN(w.created_at) mn, MAX(w.created_at) mx FROM skipped_windows s
                 JOIN shadow_windows w ON s.market_slug = w.market_slug
                 WHERE s.model_side IS NOT NULL''').fetchone()
print(f'joinable date range: {r["mn"]} .. {r["mx"]}')

# duplicates? one slug -> many skipped rows?
r = c.execute('SELECT COUNT(*) total, COUNT(DISTINCT market_slug) uniq FROM skipped_windows').fetchone()
print(f'\nskipped_windows: {r["total"]} rows, {r["uniq"]} unique slugs')
r = c.execute('SELECT COUNT(*) total, COUNT(DISTINCT market_slug) uniq FROM shadow_windows').fetchone()
print(f'shadow_windows:  {r["total"]} rows, {r["uniq"]} unique slugs')

print('\nsample joined rows:')
for r in c.execute('''SELECT w.created_at, s.model_side, s.model_confidence, s.entry_price,
                      w.up_open_ask, w.down_open_ask, w.btc_price_start, w.btc_price_end
                      FROM skipped_windows s JOIN shadow_windows w ON s.market_slug=w.market_slug
                      WHERE s.model_side IS NOT NULL ORDER BY w.id DESC LIMIT 6'''):
    print(f'  {r["created_at"]} model={r["model_side"]} conf={r["model_confidence"]} entry={r["entry_price"]} btc {r["btc_price_start"]}->{r["btc_price_end"]}')
db.close()
