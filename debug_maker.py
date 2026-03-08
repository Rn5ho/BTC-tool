"""Debug: inspect raw CLOB maker trade data to find correct amount field."""
import asyncio
import json
from config import settings
from strategy.live_trader import LiveTrader

async def main():
    lt = LiveTrader(
        private_key=settings.polymarket_private_key,
        funder_address=settings.polymarket_funder_address,
        max_bet_usdc=settings.max_live_bet_usdc,
    )
    await lt.initialize()

    trades = await lt.fetch_clob_trades()
    maker_trades = [t for t in trades if t.get("trader_side") == "MAKER"]

    print(f"Total trades: {len(trades)}, Maker: {len(maker_trades)}")
    print()

    # Show first 5 maker trades in detail
    for i, t in enumerate(maker_trades[:5]):
        print(f"=== Maker trade #{i+1} ===")
        print(f"  id: {t.get('id', '')[:20]}")
        print(f"  outcome: {t.get('outcome')}")
        print(f"  side: {t.get('side')}")
        print(f"  size (top-level): {t.get('size')}")
        print(f"  price: {t.get('price')}")
        print(f"  asset_id: {t.get('asset_id', '')[:20]}...")
        print(f"  market: {t.get('market', '')[:20]}...")
        print(f"  maker_address: {t.get('maker_address', '')[:20]}...")

        maker_orders = t.get("maker_orders", [])
        print(f"  maker_orders count: {len(maker_orders)}")
        for j, mo in enumerate(maker_orders):
            print(f"    maker_order[{j}]:")
            print(f"      maker_address: {mo.get('maker_address', '')[:20]}...")
            print(f"      matched_amount: {mo.get('matched_amount')}")
            print(f"      price: {mo.get('price')}")
            print(f"      side: {mo.get('side')}")
            print(f"      outcome: {mo.get('outcome')}")
        print()

    # Show our funder address for comparison
    print(f"Our funder: {settings.polymarket_funder_address}")

asyncio.run(main())
