"""Check for unclaimed winning tokens in the wallet."""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from config import Settings
import sqlite3

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

# Check all unique token IDs from WIN trades
cur.execute("""
    SELECT DISTINCT token_id, side,
           SUM(amount_usdc) as total_cost,
           MAX(created_at) as last_trade,
           COUNT(*) as trade_count
    FROM live_trades WHERE outcome='WIN' AND entry_price > 0
    GROUP BY token_id
    ORDER BY last_trade DESC
""")
rows = cur.fetchall()

total_unclaimed = 0.0
unclaimed_count = 0
claimed_count = 0

for row in rows:
    token_id, side, cost, last, cnt = row
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id
        )
        result = client.get_balance_allowance(params)
        token_bal = float(result.get("balance", 0)) / 1e6
        if token_bal > 0.5:
            total_unclaimed += token_bal
            unclaimed_count += 1
            print(
                f"UNCLAIMED {side} {last}: {token_bal:.2f} tokens "
                f"(${token_bal:.2f}) from {cnt} trade(s)"
            )
        else:
            claimed_count += 1
    except Exception as e:
        print(f"Error checking {token_id[:16]}: {e}")

print(f"\n=== SUMMARY ===")
print(f"Token IDs checked: {len(rows)}")
print(f"Claimed (redeemed): {claimed_count}")
print(f"Unclaimed: {unclaimed_count}")
print(f"Total unclaimed value: ${total_unclaimed:.2f}")

# USDC balance
bal = client.get_balance_allowance(
    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
)
usdc = float(bal["balance"]) / 1e6
print(f"USDC balance: ${usdc:.2f}")
print(f"Total portfolio: ${usdc + total_unclaimed:.2f}")
print(f"vs deposited $116 = PnL ${usdc + total_unclaimed - 116:.2f}")

conn.close()
