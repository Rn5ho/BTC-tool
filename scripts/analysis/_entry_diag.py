"""Diagnose the entry-price assumption that drives the shadow +EV.

If the shadow sim shows +EV only because it enters at a too-cheap, too-early
ask, this exposes it. Checks:
  1. model-side WR bucketed by entry ask  -> is the market calibrated?
  2. avg model-side ask at different snapshot offsets into the window
  3. seconds-into-window of the 'first snapshot'
"""
import sqlite3
from collections import defaultdict

db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

windows = {}
for r in c.execute('''
  SELECT w.market_slug slug, s.model_side, w.btc_price_start bs, w.btc_price_end be
  FROM skipped_windows s JOIN shadow_windows w ON s.market_slug=w.market_slug
  WHERE s.model_side IS NOT NULL AND w.btc_price_start>0 AND w.btc_price_end>0'''):
    windows[r['slug']] = (r['model_side'], r['bs'], r['be'])

snaps = defaultdict(list)
for r in c.execute('''SELECT slug, timestamp, up_best_ask, down_best_ask
                      FROM market_snapshots ORDER BY slug, timestamp'''):
    snaps[r['slug']].append(r)
db.close()

def wstart(slug): return int(slug.rsplit('-',1)[1])
def up_won(bs,be): return be>bs

# 1. model-side WR bucketed by FIRST-snapshot entry ask
bucket = defaultdict(lambda:[0,0])  # ask_bucket -> [n, wins]
first_secs = []
for slug,(mside,bs,be) in windows.items():
    sn = snaps.get(slug)
    if not sn: continue
    askk = 'up_best_ask' if mside=='UP' else 'down_best_ask'
    entry=None; esec=None
    for s in sn:
        a=s[askk]
        if a and 0<a<1:
            entry=a; esec=s['timestamp']-wstart(slug); break
    if entry is None: continue
    first_secs.append(esec)
    won=(mside=='UP')==up_won(bs,be)
    b=round(entry*20)/20
    bucket[b][0]+=1
    bucket[b][1]+= 1 if won else 0

print('=== model-side WR by FIRST-snapshot entry ask ===')
print('if market is calibrated, WR% ~= ask*100; +EV needs WR% > ask*100')
print(f'{"ask":>6}{"n":>7}{"WR%":>8}{"breakeven%":>12}{"edge(WR-px)":>13}')
for b in sorted(bucket):
    n,w=bucket[b]
    if n<30: continue
    wr=w/n*100
    print(f'{b:>6.2f}{n:>7}{wr:>8.2f}{b*100:>12.1f}{wr-b*100:>13.2f}')

first_secs.sort()
print(f'\nfirst-snapshot seconds-into-window: min={first_secs[0]} '
      f'median={first_secs[len(first_secs)//2]} max={first_secs[-1]}')

# 2. avg model-side ask at snapshot nearest a target second
print('\n=== avg model-side ask vs entry timing ===')
for target in (12, 30, 45, 60, 90, 120):
    asks=[]
    for slug,(mside,bs,be) in windows.items():
        sn=snaps.get(slug)
        if not sn: continue
        askk='up_best_ask' if mside=='UP' else 'down_best_ask'
        best=None;bd=1e9
        for s in sn:
            sec=s['timestamp']-wstart(slug)
            a=s[askk]
            if a and 0<a<1 and abs(sec-target)<bd:
                bd=abs(sec-target);best=a
        if best is not None: asks.append(best)
    if asks:
        print(f'  entry @ ~{target:>3}s : n={len(asks):>6}  avg model-side ask={sum(asks)/len(asks):.4f}')
