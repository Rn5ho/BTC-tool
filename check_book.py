"""Quick check of raw order book structure."""
import asyncio
from data.polymarket import PolymarketClient

async def check():
    pc = PolymarketClient(
        gamma_url="https://gamma-api.polymarket.com",
        clob_url="https://clob.polymarket.com",
    )
    await pc.start()
    m = await pc.discover_market()
    if m:
        book = await pc.get_orderbook(m.up_token_id)
        if book:
            bids = book.get("bids", [])[:8]
            asks = book.get("asks", [])[:8]
            print("UP token bids (top 8, should be highest price first):")
            for b in bids:
                print("  price=%s size=%s" % (b.get("price"), b.get("size")))
            print("UP token asks (top 8, should be lowest price first):")
            for a in asks:
                print("  price=%s size=%s" % (a.get("price"), a.get("size")))
            print()
            # Check sort order
            if bids:
                prices = [float(b["price"]) for b in bids]
                print("Bids sorted desc?", prices == sorted(prices, reverse=True))
                print("Bid prices:", prices)
    await pc.stop()

asyncio.run(check())
