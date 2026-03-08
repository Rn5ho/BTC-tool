"""Check unclaimed tokens - detailed view with market resolution status."""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from config import Settings
import sqlite3
import requests

s = Settings()
client = ClobClient(
    "https://clob.polymarket.com",
    key=s.polymarket_private_key,
    chain_id=137,
    signature_type=2,
    funder=s.polymarket_funder_address,
)
client.set_api_creds(client.create_or_derive_api_creds())

conn = sqlite3.connect("btc_edge.db")
cur = conn.cursor()

# Get the 14 unclaimed token IDs
cur.execute("""
    SELECT DISTINCT token_id, side, market_slug,
           SUM(amount_usdc) as total_cost,
           MAX(created_at) as last_trade
    FROM live_trades WHERE outcome='WIN' AND entry_price > 0
    GROUP BY token_id
    ORDER BY last_trade DESC
""")
rows = cur.fetchall()

print("Checking all WIN token IDs...\n")

unclaimed_tokens = []
for row in rows:
    token_id, side, slug, cost, last = row
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id
        )
        result = client.get_balance_allowance(params)
        token_bal = float(result.get("balance", 0)) / 1e6
        if token_bal > 0.5:
            unclaimed_tokens.append((token_id, side, slug, token_bal, last))
    except Exception:
        pass

print(f"Found {len(unclaimed_tokens)} unclaimed positions\n")

# For each unclaimed, check market resolution via Gamma API
for token_id, side, slug, balance, last_trade in unclaimed_tokens:
    print(f"--- {slug} ({side}, {balance:.2f} tokens) ---")
    print(f"  Last trade: {last_trade}")

    # Check Gamma API for resolution
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": slug.rsplit("-", 1)[0] if slug else ""},
            timeout=10,
        )
        if resp.status_code == 200:
            events = resp.json()
            if events:
                for mkt in events[0].get("markets", []):
                    if mkt.get("clobTokenIds") and token_id in str(mkt.get("clobTokenIds", [])):
                        print(f"  Gamma resolved: {mkt.get('resolved', 'unknown')}")
                        print(f"  Winner: {mkt.get('winner', 'unknown')}")
                        print(f"  Active: {mkt.get('active', 'unknown')}")
                        print(f"  Closed: {mkt.get('closed', 'unknown')}")
                        break
    except Exception as e:
        print(f"  Gamma check failed: {e}")

    # Also check CLOB market info
    try:
        # Try to get the market from CLOB
        mkt_info = client.get_market(token_id)
        if isinstance(mkt_info, dict):
            print(f"  CLOB active: {mkt_info.get('active', '?')}")
            print(f"  CLOB closed: {mkt_info.get('closed', '?')}")
            print(f"  CLOB accepting_orders: {mkt_info.get('accepting_orders', '?')}")
    except Exception as e:
        print(f"  CLOB market check: {type(e).__name__}")

    print()

# Also check: are there tokens from LOSS trades still in wallet?
# (phantom trades that were recorded as losses but never actually bought)
print("\n=== CHECKING LOSS TRADE PHANTOMS (recent) ===")
cur.execute("""
    SELECT DISTINCT token_id, side, market_slug, amount_usdc, created_at
    FROM live_trades WHERE outcome='LOSS'
    ORDER BY created_at DESC LIMIT 20
""")
loss_rows = cur.fetchall()

for row in loss_rows:
    token_id, side, slug, cost, created = row
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id
        )
        result = client.get_balance_allowance(params)
        token_bal = float(result.get("balance", 0)) / 1e6
        if token_bal > 0.5:
            print(f"  UNEXPECTED tokens in LOSS trade: {slug} {side} {created}: {token_bal:.2f} tokens")
    except Exception:
        pass

print("\nDone.")
conn.close()
