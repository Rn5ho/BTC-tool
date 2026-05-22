"""Confirm live (real-money) results — the ground truth that shadow sims must match."""
import sqlite3
db = sqlite3.connect('/home/btcedge/BTC-tool/btc_edge.db')
db.row_factory = sqlite3.Row
c = db.cursor()

print('=== LIVE TRADES by outcome (real money) ===')
for r in c.execute('''SELECT outcome, COUNT(*) n, ROUND(SUM(pnl),2) pnl, ROUND(AVG(pnl),3) avg
                      FROM live_trades WHERE outcome IS NOT NULL GROUP BY outcome ORDER BY n DESC'''):
    print(f'  {str(r["outcome"]):<14} n={r["n"]:>5}  total=${r["pnl"]:>9}  avg=${r["avg"]}')

r = c.execute('SELECT COUNT(*) n, ROUND(SUM(pnl),2) p FROM live_trades WHERE outcome IS NOT NULL').fetchone()
print(f'  ALL settled: n={r["n"]}  NET=${r["p"]}')

# cheaper-side subset
print('\n=== cheap_side tagged trades (the 2026-03-30 live experiment) ===')
for r in c.execute('''SELECT outcome, COUNT(*) n, ROUND(SUM(pnl),2) pnl
                      FROM live_trades WHERE trade_tag='cheap_side' AND outcome IS NOT NULL
                      GROUP BY outcome'''):
    print(f'  {str(r["outcome"]):<14} n={r["n"]:>4}  total=${r["pnl"]}')

# gamma ground-truth WR where available
r = c.execute('''SELECT COUNT(*) n, SUM(gamma_winner_matches) m
                 FROM live_trades WHERE gamma_winner_matches IS NOT NULL''').fetchone()
if r['n']:
    print(f'\ngamma-verified directional WR: {r["m"]}/{r["n"]} = {r["m"]/r["n"]*100:.2f}%')
db.close()
