"""3-month settlement analysis. Decisive, bug-proof: depends only on btc_price_start/end.

Joins skipped_windows (model_side, model_confidence, entry_price) to
shadow_windows (open asks, settlement BTC prices).
"""
import sqlite3
from collections import defaultdict

db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

rows = c.execute('''
  SELECT w.created_at, w.up_open_ask, w.down_open_ask,
         w.btc_price_start, w.btc_price_end, w.regime_state, w.regime_strength,
         s.model_side, s.model_confidence, s.entry_price
  FROM skipped_windows s
  JOIN shadow_windows w ON s.market_slug = w.market_slug
  WHERE s.model_side IS NOT NULL
    AND w.btc_price_start > 0 AND w.btc_price_end > 0
  ORDER BY w.created_at
''').fetchall()

print(f'windows analysed: {len(rows)}')
print(f'date range: {rows[0]["created_at"]} .. {rows[-1]["created_at"]}\n')

def up_won(r):
    return r['btc_price_end'] > r['btc_price_start']   # flat -> DOWN

# ---- basic settlement facts ----
ups = sum(1 for r in rows if up_won(r))
ties = sum(1 for r in rows if r['btc_price_end'] == r['btc_price_start'])
print(f'UP settled:   {ups} ({ups/len(rows)*100:.2f}%)')
print(f'DOWN settled: {len(rows)-ups} ({(len(rows)-ups)/len(rows)*100:.2f}%)')
print(f'exact ties (resolve DOWN): {ties}\n')

# ---- model directional WR ----
def model_won(r):
    return (r['model_side'] == 'UP') == up_won(r)

mw = sum(1 for r in rows if model_won(r))
print('=== MODEL DIRECTIONAL ACCURACY (settlement) ===')
print(f'  model correct: {mw}/{len(rows)} = {mw/len(rows)*100:.2f}%')
print(f'  fade (invert): {len(rows)-mw}/{len(rows)} = {(len(rows)-mw)/len(rows)*100:.2f}%')

def ev(wr, price):
    """EV per $1 staked betting at `price`, win prob `wr`."""
    return wr/price - 1

# ---- P&L simulation: profit per $1 staked at settlement ----
# strategy returns (n_trades, wins, total_profit_per_$1, avg_price)
def sim(pick):
    """pick(r) -> ('UP'/'DOWN', price) or None to skip."""
    n=0; wins=0; pnl=0.0; psum=0.0
    for r in rows:
        res = pick(r)
        if res is None: continue
        side, price = res
        if price is None or price <= 0 or price >= 1: continue
        n+=1; psum+=price
        won = (side=='UP')==up_won(r)
        if won:
            wins+=1; pnl += (1.0-price)/price   # shares = 1/price, payoff 1, cost 1
        else:
            pnl += -1.0
    return n, wins, pnl, (psum/n if n else 0)

strategies = {
  'Model side @ open_ask':   lambda r: (r['model_side'], r['up_open_ask'] if r['model_side']=='UP' else r['down_open_ask']),
  'Fade model @ open_ask':   lambda r: ('DOWN' if r['model_side']=='UP' else 'UP', r['down_open_ask'] if r['model_side']=='UP' else r['up_open_ask']),
  'Model side @ entry_price':lambda r: (r['model_side'], r['entry_price']),
  'Cheaper side @ open_ask': lambda r: (('UP',r['up_open_ask']) if r['up_open_ask']<=r['down_open_ask'] else ('DOWN',r['down_open_ask'])),
  'Favorite side @ open_ask':lambda r: (('UP',r['up_open_ask']) if r['up_open_ask']>r['down_open_ask'] else ('DOWN',r['down_open_ask'])),
  'Always UP @ open_ask':    lambda r: ('UP', r['up_open_ask']),
  'Always DOWN @ open_ask':  lambda r: ('DOWN', r['down_open_ask']),
}
print('\n=== SETTLEMENT P&L (per $1 staked, NO fees, NO early-exit) ===')
print(f'{"strategy":<28}{"n":>7}{"WR%":>8}{"avgPx":>8}{"totPnL$":>11}{"EV/trade":>11}')
for name, pick in strategies.items():
    n,w,pnl,ap = sim(pick)
    wr = w/n*100 if n else 0
    evt = pnl/n if n else 0
    print(f'{name:<28}{n:>7}{wr:>8.2f}{ap:>8.3f}{pnl:>11.1f}{evt:>11.4f}')

# ---- market calibration: does open_ask predict outcome? ----
print('\n=== MARKET CALIBRATION (UP) — does up_open_ask match actual UP rate? ===')
print(f'{"up_ask bucket":<16}{"n":>7}{"actualUP%":>12}{"impliedUP%":>12}')
buckets = defaultdict(list)
for r in rows:
    b = round(r['up_open_ask']*20)/20  # 0.05 buckets
    buckets[b].append(up_won(r))
for b in sorted(buckets):
    v = buckets[b]
    if len(v) < 20: continue
    print(f'{b:<16.2f}{len(v):>7}{sum(v)/len(v)*100:>12.2f}{b*100:>12.2f}')

# ---- model WR by confidence ----
print('\n=== MODEL WR BY CONFIDENCE BUCKET ===')
print(f'{"conf bucket":<16}{"n":>7}{"modelWR%":>11}')
cb = defaultdict(list)
for r in rows:
    conf = r['model_confidence'] or 0
    b = min(int(conf*100//1), 9)  # 0-1%,1-2%,...,9%+
    cb[b].append(model_won(r))
for b in sorted(cb):
    v = cb[b]
    lbl = f'{b}-{b+1}%' if b<9 else '9%+'
    print(f'{lbl:<16}{len(v):>7}{sum(v)/len(v)*100:>11.2f}')

# ---- model WR by regime ----
print('\n=== MODEL WR BY REGIME ===')
rg = defaultdict(list)
for r in rows:
    rg[r['regime_state']].append(model_won(r))
for k,v in sorted(rg.items(), key=lambda x:-len(x[1])):
    print(f'  {k:<16}{len(v):>7}  WR={sum(v)/len(v)*100:.2f}%')

# ---- model WR by month ----
print('\n=== MODEL WR BY MONTH (drift check) ===')
mo = defaultdict(list)
for r in rows:
    mo[r['created_at'][:7]].append(model_won(r))
for k in sorted(mo):
    v=mo[k]
    print(f'  {k}  n={len(v):>6}  modelWR={sum(v)/len(v)*100:.2f}%  fadeWR={100-sum(v)/len(v)*100:.2f}%')

# ---- model WR by hour UTC ----
print('\n=== MODEL WR BY HOUR (UTC) ===')
hr = defaultdict(list)
for r in rows:
    hr[r['created_at'][11:13]].append(model_won(r))
for k in sorted(hr):
    v=hr[k]
    bar = '#'*int(sum(v)/len(v)*50)
    print(f'  {k}:00  n={len(v):>5}  WR={sum(v)/len(v)*100:5.1f}%  {bar}')

db.close()
