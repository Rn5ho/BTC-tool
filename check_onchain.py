"""Check on-chain CLOB data"""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, TradeParams, OpenOrderParams
import os, json
from dotenv import load_dotenv
load_dotenv()

host = 'https://clob.polymarket.com'
key = os.environ['POLYMARKET_PRIVATE_KEY']
chain_id = 137
funder = os.environ['POLYMARKET_FUNDER_ADDRESS']
client = ClobClient(host, key=key, chain_id=chain_id, signature_type=2, funder=funder)
client.set_api_creds(client.create_or_derive_api_creds())

# Balance
bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"USDC Balance: ${float(bal['balance']) / 1e6:.2f}")

# Recent trades (last page)
trades = client.get_trades(TradeParams(maker_address=funder))
print(f"\nRecent CLOB trades (last {len(trades)}):")
buy_total = 0
sell_total = 0
for t in trades[:30]:
    side = t.get('side', '?')
    price = t.get('price', '?')
    size = t.get('size', '?')
    ts = t.get('match_time', '?')
    otype = t.get('type', '?')
    asset_id = t.get('asset_id', '?')[-8:]
    if side == 'BUY':
        buy_total += float(price) * float(size)
    else:
        sell_total += float(price) * float(size)
    print(f"  {ts} | {side:4} | px={price} | sz={size} | {otype} | ...{asset_id}")

print(f"\nRecent buy volume: ${buy_total:.2f}")
print(f"Recent sell volume: ${sell_total:.2f}")

# Open orders
try:
    orders = client.get_orders(OpenOrderParams())
    print(f"\nOpen orders: {len(orders)}")
    for o in orders[:10]:
        oid = o.get('id', 'N/A')
        oside = o.get('side', '?')
        oprice = o.get('price', '?')
        osize = o.get('original_size', '?')
        matched = o.get('size_matched', '?')
        print(f"  {oid[:16]}... | {oside} | px={oprice} | matched={matched}/{osize}")
except Exception as e:
    print(f"Error getting orders: {e}")
