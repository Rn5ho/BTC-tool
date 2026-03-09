"""Analyze exit strategies using REAL CLOB trade data from Polymarket API.

Pulls actual fills, groups by market, matches with bid snapshots,
and simulates exit strategies against ground truth.
"""
import sqlite3
import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, TradeParams
from dotenv import dotenv_values
from datetime import datetime, timezone
from collections import defaultdict
import numpy as np

env = dotenv_values('.env')
client = ClobClient(
    'https://clob.polymarket.com',
    key=env['POLYMARKET_PRIVATE_KEY'],
    chain_id=137,
    signature_type=2,
    funder=env['POLYMARKET_FUNDER_ADDRESS'],
)
client.set_api_creds(client.create_or_derive_api_creds())

# Balance
bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
usdc = int(bal['balance']) / 1e6

# All trades from CLOB
all_trades = client.get_trades(TradeParams())
taker = [t for t in all_trades if t.get('trader_side') == 'TAKER']
maker = [t for t in all_trades if t.get('trader_side') == 'MAKER']

print("=" * 65)
print("POLYMARKET CLOB — GROUND TRUTH DATA")
print("=" * 65)
print(f"Current USDC balance: ${usdc:.2f}")
print(f"Total CLOB trades: {len(all_trades)} (taker: {len(taker)}, maker: {len(maker)})")

# Group taker trades by market (asset_id)
markets = defaultdict(lambda: {'buys': [], 'sells': []})
for t in taker:
    asset = t.get('asset_id') or t.get('market')
    markets[asset]['buys' if t['side'] == 'BUY' else 'sells'].append(t)

# Also track maker fills
maker_by_asset = defaultdict(list)
for t in maker:
    asset = t.get('asset_id') or t.get('market')
    maker_by_asset[asset].append(t)

# Build trade summary
trade_summaries = []
for asset_id, data in markets.items():
    if not data['buys']:
        continue
    buy_cost = sum(float(b['size']) * float(b['price']) for b in data['buys'])
    buy_tokens = sum(float(b['size']) for b in data['buys'])
    buy_price = buy_cost / buy_tokens if buy_tokens > 0 else 0
    buy_time = min(int(b['match_time']) for b in data['buys'])

    # Get market slug from the trade
    market_id = data['buys'][0].get('market', '')
    outcome_token = data['buys'][0].get('outcome', '')  # "Yes" or "No"

    if data['sells']:
        sell_rev = sum(float(s['size']) * float(s['price']) for s in data['sells'])
        sell_tokens = sum(float(s['size']) for s in data['sells'])
        sell_price = sell_rev / sell_tokens if sell_tokens > 0 else 0
        pnl = sell_rev - buy_cost
        exit_type = "EARLY_EXIT"
    else:
        sell_rev = 0
        sell_tokens = 0
        sell_price = 0
        # Settled on-chain: outcome depends on resolution
        # If token won, value = 1.0 per token (auto-redeemed)
        # If token lost, value = 0
        exit_type = "HOLD"
        pnl = None  # Will compute after resolution check

    trade_summaries.append({
        'asset_id': asset_id,
        'market_id': market_id,
        'outcome_token': outcome_token,
        'buy_cost': buy_cost,
        'buy_tokens': buy_tokens,
        'buy_price': buy_price,
        'buy_time': buy_time,
        'sell_rev': sell_rev,
        'sell_tokens': sell_tokens,
        'sell_price': sell_price,
        'exit_type': exit_type,
        'pnl': pnl,
    })

# Sort by time
trade_summaries.sort(key=lambda t: t['buy_time'])

print(f"\n--- Taker Trades by Market ---")
print(f"Total markets: {len(trade_summaries)}")
early_exits = [t for t in trade_summaries if t['exit_type'] == 'EARLY_EXIT']
holds = [t for t in trade_summaries if t['exit_type'] == 'HOLD']
print(f"Early exits: {len(early_exits)}")
print(f"Hold to settlement: {len(holds)}")

if early_exits:
    ee_pnl = sum(t['pnl'] for t in early_exits)
    print(f"Early exit P&L: ${ee_pnl:+.2f} (avg ${ee_pnl/len(early_exits):+.2f}/trade)")
    print(f"  Avg buy price: {np.mean([t['buy_price'] for t in early_exits]):.3f}")
    print(f"  Avg sell price: {np.mean([t['sell_price'] for t in early_exits]):.3f}")

# ================================================================
print("\n" + "=" * 65)
print("MAKER FILLS SUMMARY")
print("=" * 65)
maker_markets = 0
maker_cost = 0
for asset_id, fills in maker_by_asset.items():
    maker_markets += 1
    maker_cost += sum(float(f['size']) * float(f['price']) for f in fills)
print(f"Maker markets: {maker_markets}")
print(f"Maker total cost: ${maker_cost:.2f}")

# ================================================================
print("\n" + "=" * 65)
print("MATCH WITH DB — Verify CLOB data matches our records")
print("=" * 65)

conn = sqlite3.connect('btc_edge.db')
conn.row_factory = sqlite3.Row
db_live = [dict(r) for r in conn.execute(
    "SELECT * FROM live_trades WHERE success=1 ORDER BY timestamp"
).fetchall()]

# Match by entry_price proximity and timestamp
matched = 0
unmatched_clob = 0
for ts in trade_summaries:
    found = False
    for db in db_live:
        # Match by approximate time (within 30s) and price (within 5%)
        time_diff = abs(db['timestamp'] / 1000 - ts['buy_time'])
        if time_diff < 30 and db.get('entry_price') and abs(db['entry_price'] - ts['buy_price']) < 0.05:
            found = True
            break
    if found:
        matched += 1
    else:
        unmatched_clob += 1

print(f"CLOB trades matched to DB: {matched}/{len(trade_summaries)}")
print(f"CLOB trades NOT in DB: {unmatched_clob}")
print(f"DB live trades: {len(db_live)}")

# ================================================================
print("\n" + "=" * 65)
print("EXIT STRATEGY BACKTEST ON REAL CLOB DATA")
print("=" * 65)

# For each taker buy, match with market_snapshots to get bid trajectory
enriched = []
for ts in trade_summaries:
    buy_time = ts['buy_time']

    # Find matching slug in market_snapshots
    # The CLOB market_id is a condition_id, not a slug. We need to find the slug.
    # Try matching by timestamp and outcome token
    outcome = ts['outcome_token']

    # Find snapshots around buy time
    snapshots = conn.execute(
        "SELECT * FROM market_snapshots WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (buy_time - 30, buy_time + 330)
    ).fetchall()

    if not snapshots:
        continue

    # Try to figure out which side (UP/DOWN) from outcome token
    # In our system: "Yes" on UP token = UP side, "Yes" on DOWN token = DOWN side
    # The asset_id tells us which token we bought
    # Actually, let's just try both UP and DOWN bids and see which one
    # has the entry price matching our buy price

    best_match = None
    best_diff = 999
    for snap in snapshots[:5]:  # Check first few snapshots
        snap = dict(snap)
        for side_prefix in ['up', 'down']:
            ask = snap.get(f'{side_prefix}_best_ask') or 0
            if ask > 0 and abs(ask - ts['buy_price']) < 0.10:
                diff = abs(ask - ts['buy_price'])
                if diff < best_diff:
                    best_diff = diff
                    best_match = (snap['slug'], 'UP' if side_prefix == 'up' else 'DOWN')

    if best_match is None:
        continue

    slug, side = best_match

    # Now get full bid trajectory for this market
    market_snaps = conn.execute(
        "SELECT * FROM market_snapshots WHERE slug = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (slug, buy_time - 30, buy_time + 330)
    ).fetchall()

    if len(market_snaps) < 5:
        continue

    bids = []
    for snap in market_snaps:
        snap = dict(snap)
        t = snap['timestamp'] - buy_time
        if side == "UP":
            bid = snap.get('up_best_bid') or 0
        else:
            bid = snap.get('down_best_bid') or 0
        if bid:
            bids.append((t, bid))

    if len(bids) < 5:
        continue

    # Determine actual outcome
    # For EARLY_EXIT, we know the P&L
    # For HOLD, check DB
    if ts['exit_type'] == 'EARLY_EXIT':
        actual_pnl = ts['pnl']
        outcome = 'EARLY_EXIT'
    else:
        # Find in DB
        db_match = None
        for db in db_live:
            time_diff = abs(db['timestamp'] / 1000 - buy_time)
            if time_diff < 30 and db.get('entry_price') and abs(db['entry_price'] - ts['buy_price']) < 0.05:
                db_match = db
                break
        if db_match and db_match.get('outcome'):
            actual_pnl = db_match['pnl']
            outcome = db_match['outcome']
        else:
            continue  # Can't determine outcome

    if outcome == 'EARLY_EXIT':
        continue  # Skip early exits for exit strategy simulation

    enriched.append({
        'outcome': outcome,
        'entry_price': ts['buy_price'],
        'amount': ts['buy_cost'],
        'actual_pnl': actual_pnl,
        'side': side,
        'bids': bids,
        'exit_type': ts['exit_type'],
        'buy_tokens': ts['buy_tokens'],
    })

print(f"\nCLOB trades with bid data: {len(enriched)}")
if enriched:
    w = sum(1 for r in enriched if r['outcome'] == 'WIN')
    l = sum(1 for r in enriched if r['outcome'] == 'LOSS')
    pnl = sum(r['actual_pnl'] for r in enriched)
    print(f"W/L: {w}/{l} | PnL: ${pnl:.2f}")

    # Entry price distribution
    print(f"\nEntry price distribution:")
    for lo in np.arange(0.25, 0.65, 0.05):
        hi = lo + 0.05
        subset = [r for r in enriched if lo <= r['entry_price'] < hi]
        if subset:
            sw = sum(1 for r in subset if r['outcome'] == 'WIN')
            sl = sum(1 for r in subset if r['outcome'] == 'LOSS')
            print(f"  {lo:.2f}-{hi:.2f}: {len(subset):3d} trades (W/L={sw}/{sl})")

    # Run exit strategies
    def sim_tiered(trades, cheap_t, mid_t, high_t):
        total_with = 0
        total_without = 0
        exits = 0
        rescued = 0
        cut_short = 0
        for r in trades:
            total_without += r['actual_pnl']
            ep = r['entry_price']
            if ep < 0.35:
                thresh = cheap_t
            elif ep < 0.50:
                thresh = mid_t
            else:
                thresh = high_t
            fee = 0.015
            tokens = (r['amount'] / ep) * (1 - fee)
            exited = False
            for t, b in r['bids']:
                if t < 10:
                    continue
                if b >= thresh:
                    total_with += tokens * b * (1 - fee) - r['amount']
                    exits += 1
                    if r['outcome'] == 'LOSS':
                        rescued += 1
                    elif r['outcome'] == 'WIN':
                        cut_short += 1
                    exited = True
                    break
            if not exited:
                total_with += r['actual_pnl']
        delta = total_with - total_without
        return {'delta': delta, 'exits': exits, 'rescued': rescued,
                'cut_short': cut_short, 'pnl_with': total_with,
                'pnl_without': total_without}

    print(f"\n--- Exit Strategy Results (CLOB ground truth) ---")
    configs = [
        ("Current flat 0.95", 0.95, 0.95, 0.95),
        ("3-tier 0.60/0.65/0.94", 0.60, 0.65, 0.94),
        ("3-tier 0.60/0.70/0.95", 0.60, 0.70, 0.95),
        ("3-tier 0.70/0.85/0.95", 0.70, 0.85, 0.95),
        ("<0.50 only: 0.65/0.65/0.95", 0.65, 0.65, 0.95),
        ("<0.50 aggressive: 0.55/0.60/0.95", 0.55, 0.60, 0.95),
    ]
    for name, c, m, h in configs:
        r = sim_tiered(enriched, c, m, h)
        per = r['delta'] / len(enriched) if enriched else 0
        print(f"  {name:40s}: delta=${r['delta']:+7.2f} "
              f"({r['exits']} exits, {r['rescued']}L/{r['cut_short']}W) "
              f"[${per:+.3f}/trade]")

    # Per-bucket optimal
    print(f"\n--- Per-Bucket Optimal (CLOB ground truth) ---")
    for lo in np.arange(0.25, 0.65, 0.05):
        hi = lo + 0.05
        subset = [r for r in enriched if lo <= r['entry_price'] < hi]
        if len(subset) < 3:
            continue
        best_delta = -999
        best_t = 0
        for t in np.arange(0.40, 1.00, 0.02):
            res = sim_tiered(subset, t, t, t)
            if res['delta'] > best_delta:
                best_delta = res['delta']
                best_t = t
        sw = sum(1 for r in subset if r['outcome'] == 'WIN')
        sl = sum(1 for r in subset if r['outcome'] == 'LOSS')
        print(f"  {lo:.2f}-{hi:.2f}: {len(subset):3d} trades (W/L={sw}/{sl}) "
              f"best={best_t:.2f} (${best_delta:+.2f})")

    # Split: <0.50 vs >=0.50
    cheap = [r for r in enriched if r['entry_price'] < 0.50]
    expensive = [r for r in enriched if r['entry_price'] >= 0.50]
    print(f"\n--- Split: <0.50 vs >=0.50 ---")
    if cheap:
        cw = sum(1 for r in cheap if r['outcome'] == 'WIN')
        cl = sum(1 for r in cheap if r['outcome'] == 'LOSS')
        cp = sum(r['actual_pnl'] for r in cheap)
        print(f"  <0.50: {len(cheap)} trades (W/L={cw}/{cl}) PnL=${cp:.2f}")
        for name, c, m, h in [("Flat 0.95", 0.95, 0.95, 0.95),
                                ("Flat 0.65", 0.65, 0.65, 0.65),
                                ("Flat 0.68", 0.68, 0.68, 0.68)]:
            r = sim_tiered(cheap, c, m, h)
            print(f"    {name:20s}: delta=${r['delta']:+.2f} ({r['rescued']}L/{r['cut_short']}W)")
    if expensive:
        ew = sum(1 for r in expensive if r['outcome'] == 'WIN')
        el = sum(1 for r in expensive if r['outcome'] == 'LOSS')
        ep = sum(r['actual_pnl'] for r in expensive)
        print(f"  >=0.50: {len(expensive)} trades (W/L={ew}/{el}) PnL=${ep:.2f}")
        for name, c, m, h in [("Flat 0.95", 0.95, 0.95, 0.95),
                                ("Flat 0.94", 0.94, 0.94, 0.94),
                                ("Flat 0.90", 0.90, 0.90, 0.90)]:
            r = sim_tiered(expensive, c, m, h)
            print(f"    {name:20s}: delta=${r['delta']:+.2f} ({r['rescued']}L/{r['cut_short']}W)")

conn.close()

# ================================================================
print("\n" + "=" * 65)
print("REAL P&L SUMMARY")
print("=" * 65)
total_buy_cost = sum(t['buy_cost'] for t in trade_summaries)
total_sell_rev = sum(t['sell_rev'] for t in trade_summaries if t['sell_rev'] > 0)
total_ee_pnl = sum(t['pnl'] for t in early_exits) if early_exits else 0
print(f"Total bought: ${total_buy_cost:.2f}")
print(f"Total sold (early exits): ${total_sell_rev:.2f}")
print(f"Early exit net P&L: ${total_ee_pnl:+.2f}")
print(f"Maker fill cost: ${maker_cost:.2f}")
print(f"Current USDC: ${usdc:.2f}")
