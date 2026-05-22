"""One-off: explore 3 months of collected data. Schemas + counts + date ranges."""
import sqlite3, sys

db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

tables = ['candles', 'live_trades', 'paper_trades', 'skipped_windows',
          'market_snapshots', 'feature_snapshots', 'shadow_windows']

for t in tables:
    try:
        n = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    except Exception as e:
        print(f'{t}: ERROR {e}')
        continue
    print(f'\n===== {t}: {n} rows =====')
    cols = [r[1] for r in c.execute(f'PRAGMA table_info({t})').fetchall()]
    print('cols:', ', '.join(cols))
    # find a timestamp-ish column
    tcol = None
    for cand in ('created_at', 'timestamp', 'ts', 'window_start', 'open_time', 'entry_time'):
        if cand in cols:
            tcol = cand
            break
    if tcol and n:
        mn, mx = c.execute(f'SELECT MIN({tcol}), MAX({tcol}) FROM {t}').fetchone()
        print(f'{tcol} range: {mn}  ->  {mx}')

db.close()
