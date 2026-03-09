"""Analyze CLOB trade data from Polymarket API — filtered to our bot's trades only."""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, TradeParams
from dotenv import dotenv_values
from datetime import datetime, timezone
from collections import defaultdict

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

# All trades
all_trades = client.get_trades(TradeParams())
taker = [t for t in all_trades if t.get('trader_side') == 'TAKER']
maker = [t for t in all_trades if t.get('trader_side') == 'MAKER']

print("=" * 60)
print("POLYMARKET CLOB ANALYSIS (source of truth)")
print("=" * 60)
print(f"Current USDC balance: ${usdc:.2f}")
print(f"Total CLOB trades: {len(all_trades)} (taker: {len(taker)}, maker: {len(maker)})")

# Our bot's trades only (taker)
buys = [t for t in taker if t['side'] == 'BUY']
sells = [t for t in taker if t['side'] == 'SELL']
buy_usdc = sum(float(t['size']) * float(t['price']) for t in buys)
buy_tokens = sum(float(t['size']) for t in buys)
sell_usdc = sum(float(t['size']) * float(t['price']) for t in sells)
sell_tokens = sum(float(t['size']) for t in sells)

print(f"\n--- Our Bot's Trades (taker only) ---")
print(f"Buys:  {len(buys)} orders, ${buy_usdc:.2f} USDC spent, {buy_tokens:.1f} tokens")
print(f"Sells: {len(sells)} orders, ${sell_usdc:.2f} USDC received, {sell_tokens:.1f} tokens")

# Maker trades (counterparty — not initiated by us)
if maker:
    print(f"\n--- Maker Trades (counterparty, NOT our bot) ---")
    for t in maker:
        ts = int(t['match_time'])
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m-%d %H:%M')
        cost = float(t['size']) * float(t['price'])
        print(f"  {dt} {t['side']:4s} {t['outcome']:4s} sz={t['size']} @ {t['price']} = ${cost:.2f}")

# Group taker trades by market
markets = defaultdict(lambda: {'buys': [], 'sells': []})
for t in taker:
    m = t['market']
    markets[m]['buys' if t['side'] == 'BUY' else 'sells'].append(t)

ee_count = 0
ee_pnl = 0
buy_only = 0
buy_only_cost = 0

for mkt, data in markets.items():
    cost = sum(float(b['size']) * float(b['price']) for b in data['buys'])
    if data['sells']:
        rev = sum(float(s['size']) * float(s['price']) for s in data['sells'])
        ee_count += 1
        ee_pnl += rev - cost
    else:
        buy_only += 1
        buy_only_cost += cost

print(f"\n--- Market-Level Summary ---")
print(f"Early exits: {ee_count} markets, P&L: ${ee_pnl:+.2f}")
print(f"Buy-only (settled on-chain): {buy_only} markets, cost: ${buy_only_cost:.2f}")

# The real math
deposited = 73.0
print(f"\n{'='*60}")
print(f"REAL P&L")
print(f"{'='*60}")
print(f"Deposited:        ~${deposited:.2f}")
print(f"Current USDC:      ${usdc:.2f}")
print(f"Profit (USDC):     ${usdc - deposited:+.2f}")
print(f"ROI:               {(usdc - deposited)/deposited*100:+.1f}%")
print(f"Hours running:     ~14.2")
print(f"Profit/hour:       ${(usdc - deposited)/14.2:.2f}")
print(f"\nNote: Winning tokens are auto-redeemed by Polymarket")
print(f"and added back to USDC balance. This is the true P&L.")
