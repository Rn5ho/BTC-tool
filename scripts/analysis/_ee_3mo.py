"""Realistic early-exit simulation over 3 months of shadow data.

Entry  = first snapshot's REAL best_ask (includes vig) for the chosen side.
EE     = scan best_bid in [start+15s, start+240s] (10s grace, 60s tail excluded
         to remove settlement-convergence artifacts). Trigger on bid >= thresh
         AND bid_size >= 20 tokens. Exit at the triggering bid.
Settle = btc_price_end vs btc_price_start from shadow_windows.
PnL    = per $1 staked, gross (Polymarket charges ~0 fee on these markets).
"""
import sqlite3
from collections import defaultdict

db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

# windows: slug -> (model_side, btc_start, btc_end, regime)
windows = {}
for r in c.execute('''
  SELECT w.market_slug slug, s.model_side, w.btc_price_start bs, w.btc_price_end be,
         w.regime_state regime
  FROM skipped_windows s JOIN shadow_windows w ON s.market_slug=w.market_slug
  WHERE s.model_side IS NOT NULL AND w.btc_price_start>0 AND w.btc_price_end>0'''):
    windows[r['slug']] = (r['model_side'], r['bs'], r['be'], r['regime'])
print(f'windows: {len(windows)}')

# load all snapshots grouped by slug
snaps = defaultdict(list)
for r in c.execute('''SELECT slug, timestamp, up_best_bid, down_best_bid,
                      up_best_ask, down_best_ask, up_bid_size, down_bid_size
                      FROM market_snapshots ORDER BY slug, timestamp'''):
    snaps[r['slug']].append(r)
print(f'slugs with snapshots: {len(snaps)}')
db.close()

def win_start(slug):
    return int(slug.rsplit('-',1)[1])

def up_won(bs, be):
    return be > bs

TIERS = [(0.35,0.50),(0.40,0.65),(0.50,0.93),(1.01,0.95)]
def tiered(entry):
    for hi,th in TIERS:
        if entry < hi: return th
    return 0.95

def simulate(side_pick, ee_thresh, label, tail_excl=60, grace=15):
    """side_pick(model_side)->'UP'/'DOWN'. ee_thresh: float or 'tiered' or None."""
    n=0; settled=0; ee=0
    wins=0; losses=0; ee_regret=0; ee_save=0
    pnl=0.0
    for slug,(mside,bs,be,regime) in windows.items():
        sn = snaps.get(slug)
        if not sn or len(sn)<5: continue
        side = side_pick(mside)
        ws = win_start(slug)
        bidk = 'up_best_bid' if side=='UP' else 'down_best_bid'
        askk = 'up_best_ask' if side=='UP' else 'down_best_ask'
        sizek = 'up_bid_size' if side=='UP' else 'down_bid_size'
        # entry = first snap with a real ask
        entry=None
        for s in sn:
            a=s[askk]
            if a and 0<a<1:
                entry=a; break
        if entry is None: continue
        n+=1
        won = (side=='UP')==up_won(bs,be)
        th = tiered(entry) if ee_thresh=='tiered' else ee_thresh
        # EE scan
        trig=None
        if th is not None:
            for s in sn:
                sec=s['timestamp']-ws
                if sec<grace or sec>300-tail_excl: continue
                b=s[bidk]; sz=s[sizek] or 0
                if b and b>=th and sz>=20:
                    trig=b; break
        if trig is not None:
            ee+=1
            pnl += trig/entry - 1.0
            if won: ee_regret+=1     # would have won anyway
            else:   ee_save+=1       # EE rescued a loser
        else:
            settled+=1
            if won: wins+=1;  pnl += 1.0/entry - 1.0
            else:   losses+=1; pnl += -1.0
    wr_dir = (wins+ee_regret)/n*100 if n else 0
    return dict(label=label, n=n, ee=ee, settled=settled, wins=wins, losses=losses,
                ee_regret=ee_regret, ee_save=ee_save, pnl=pnl,
                ev=pnl/n if n else 0, ee_rate=ee/n*100 if n else 0, true_wr=wr_dir)

def show(rows):
    print(f'{"strategy":<34}{"n":>6}{"EE%":>7}{"trueWR%":>9}{"totPnL$":>10}{"EV/trd":>9}')
    for r in rows:
        print(f'{r["label"]:<34}{r["n"]:>6}{r["ee_rate"]:>7.1f}{r["true_wr"]:>9.2f}{r["pnl"]:>10.1f}{r["ev"]:>9.4f}')

model = lambda m: m
fade  = lambda m: 'DOWN' if m=='UP' else 'UP'

print('\n=== MODEL SIDE — EE threshold sweep (entry=real ask, 60s tail excluded) ===')
show([
  simulate(model, None,     'model, NO early-exit'),
  simulate(model, 0.85,     'model, EE @ 0.85'),
  simulate(model, 0.90,     'model, EE @ 0.90'),
  simulate(model, 0.93,     'model, EE @ 0.93'),
  simulate(model, 0.95,     'model, EE @ 0.95'),
  simulate(model, 0.97,     'model, EE @ 0.97'),
  simulate(model, 'tiered', 'model, EE tiered (config)'),
])

print('\n=== FADE SIDE — EE threshold sweep ===')
show([
  simulate(fade, None,     'fade, NO early-exit'),
  simulate(fade, 0.90,     'fade, EE @ 0.90'),
  simulate(fade, 0.93,     'fade, EE @ 0.93'),
  simulate(fade, 0.95,     'fade, EE @ 0.95'),
  simulate(fade, 'tiered', 'fade, EE tiered (config)'),
])

print('\n=== TAIL-EXCLUSION SENSITIVITY (model, EE @ 0.93) ===')
for tail in (0, 30, 60, 90):
    r = simulate(model, 0.93, f'tail_excl={tail}s', tail_excl=tail)
    print(f'  tail={tail:>3}s  EE%={r["ee_rate"]:5.1f}  trueWR={r["true_wr"]:.2f}  totPnL=${r["pnl"]:.1f}  EV={r["ev"]:.4f}')

print('\n=== detail: model EE tiered ===')
r = simulate(model,'tiered','model tiered')
print(f'  n={r["n"]} settled={r["settled"]} (W={r["wins"]} L={r["losses"]}) '
      f'EE={r["ee"]} (regret={r["ee_regret"]} save={r["ee_save"]})')
print(f'  total PnL ${r["pnl"]:.2f}  EV/trade ${r["ev"]:.4f}')
