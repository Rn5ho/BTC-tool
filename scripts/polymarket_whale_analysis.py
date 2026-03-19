"""Polymarket Whale Strategy Analysis

Fetches trade history for top leaderboard accounts and classifies their strategies.
Outputs a comprehensive analysis of what works and what doesn't.
"""

import json
import time
import sys
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone

# Top crypto leaderboard accounts (monthly)
CRYPTO_ACCOUNTS = [
    ("0x8dxd", "0x63ce342161250d705dc0b16df89036c8e5f9ba9a", 453207),
    ("k9Q2mX4L8A7ZP3R", "0xd0d6053c3c37e727402d84c14069780d360993aa", 450312),
    ("BoneReader", "0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9", 445829),
    ("0xdE17f7...", "0xde17f7144fbd0eddb2679132c10ff5e74b120988", 408433),
    ("0x1f0ebc...", "0x1f0ebc543b2d411f66947041625c0aa1ce61cf86", 268945),
    ("vidarx", "0x2d8b401d2f0e6937afebf18e19e11ca568a5260a", 267270),
    ("stingo43", "0x0006af12cd4dacc450836a0e1ec6ce47365d8c63", 205778),
    ("guh123", "0xa45fe11dd1420fca906ceac2c067844379a42429", 175825),
    ("hgjghjh85", "0x3e9d296b8f8f670cd859350b3c0a00251dc71f47", 161184),
    ("vague-sourdough", "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d", 160097),
    ("BoshBashBish", "0x29bc82f761749e67fa00d62896bc6855097b683c", 107485),
    ("kingofcoinflips", "0xe9c6312464b52aa3eff13d822b003282075995c9", 100996),
    ("livebreathevolatility", "0x818f214c7f3e479cce1d964d53fe3db7297558cb", 99104),
    ("Sharky6999", "0x751a2b86cab503496efd325c8344e10159349ea1", 92608),
]

# Top overall leaderboard (different from crypto)
OVERALL_ACCOUNTS = [
    ("HorizonSplendidView", "0x02227b8f5a9636e895607edd3185ed6ee5598ff7", 4016108),
    ("reachingthesky", "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2", 3742635),
    ("beachboy4", "0xc2e7800b5af46e6093872b177b7a5e7f0563be51", 2951326),
    ("majorexploiter", "0x019782cab5d844f02bafb71f512758be78579f3c", 2416975),
    ("CemeterySun", "0x37c1874a60d348903594a96703e0507c518fc53a", 1827134),
    ("gatorr", "0x93abbc022ce98d6f45d4444b594791cc4b7a9723", 1158268),
    ("Countryside", "0xbddf61af533ff524d27154e589d2d7a81510c684", 1014236),
    ("swisstony", "0x204f72f35326db932158cba6adff0b9a1da95e14", 777632),
    ("JaJackson", "0xf195721ad850377c96cd634457c70cd9e8308057", 758226),
    ("sovereign2013", "0xee613b3fc183ee44f9da9c05f53e2da107e3debf", 686243),
    ("gmanas", "0xe90bec87d9ef430f27f9dcfe72c34b76967d5da2", 679034),
    ("WoofMaster", "0x916f7165c2c836aba22edb6453cdbb5f3ea253ba", 571305),
    ("kch123", "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee", 529612),
]


def fetch_trades(address: str, limit: int = 200) -> list:
    """Fetch recent trades for an address from the Polymarket data API."""
    all_trades = []
    offset = 0
    batch = 100

    while len(all_trades) < limit:
        url = f"https://data-api.polymarket.com/trades?user={address}&limit={batch}&offset={offset}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                if not data:
                    break
                all_trades.extend(data)
                offset += batch
                if len(data) < batch:
                    break
                time.sleep(0.3)  # rate limit
        except (HTTPError, URLError, Exception) as e:
            print(f"    Error fetching {address[:10]}...: {e}", file=sys.stderr)
            break

    return all_trades[:limit]


def classify_market(title: str, slug: str) -> dict:
    """Classify a market by type and timeframe."""
    title_lower = (title or "").lower()
    slug_lower = (slug or "").lower()

    info = {"asset": "other", "timeframe": "event", "type": "other"}

    # Crypto assets
    for asset, keywords in [
        ("BTC", ["bitcoin", "btc"]),
        ("ETH", ["ethereum", "eth"]),
        ("SOL", ["solana", "sol"]),
        ("XRP", ["xrp"]),
        ("DOGE", ["dogecoin", "doge"]),
        ("BNB", ["bnb"]),
        ("HYPE", ["hyperliquid", "hype"]),
        ("ADA", ["cardano", "ada"]),
        ("AVAX", ["avalanche", "avax"]),
        ("LINK", ["chainlink", "link"]),
        ("SUI", ["sui"]),
        ("PEPE", ["pepe"]),
    ]:
        if any(k in title_lower or k in slug_lower for k in keywords):
            info["asset"] = asset
            break

    # Non-crypto
    if info["asset"] == "other":
        for cat, keywords in [
            ("politics", ["trump", "election", "president", "democrat", "republican", "congress", "senate", "governor"]),
            ("sports", ["nba", "nfl", "mlb", "nhl", "ufc", "tennis", "soccer", "football", "march madness", "ncaa"]),
            ("stocks", ["stock", "msft", "aapl", "googl", "nvda", "tsla", "amzn", "s&p", "nasdaq", "spy"]),
            ("fed", ["fed", "fomc", "interest rate", "inflation", "cpi", "gdp"]),
            ("culture", ["oscar", "grammy", "celebrity", "youtube"]),
        ]:
            if any(k in title_lower for k in keywords):
                info["asset"] = cat
                break

    # Timeframe
    if "5m" in slug_lower or "5-min" in title_lower:
        info["timeframe"] = "5m"
    elif "15m" in slug_lower or "15-min" in title_lower:
        info["timeframe"] = "15m"
    elif "1h" in slug_lower or "hourly" in title_lower:
        info["timeframe"] = "1h"
    elif "daily" in title_lower or "close above" in title_lower or "close below" in title_lower:
        info["timeframe"] = "daily"

    # Up/down vs price target vs event
    if "updown" in slug_lower or "up or down" in title_lower:
        info["type"] = "updown"
    elif "above" in title_lower or "below" in title_lower or "dip" in title_lower:
        info["type"] = "price_target"
    else:
        info["type"] = "event"

    return info


def analyze_trader(name: str, address: str, profit: float, trades: list) -> dict:
    """Analyze a single trader's strategy from their trade history."""
    if not trades:
        return None

    result = {
        "name": name,
        "address": address[:10] + "...",
        "reported_profit": profit,
        "trades_fetched": len(trades),
    }

    # Parse trades
    prices = []
    sizes = []
    buy_count = 0
    sell_count = 0
    assets = defaultdict(int)
    timeframes = defaultdict(int)
    market_types = defaultdict(int)
    market_slugs = set()
    timestamps = []

    for t in trades:
        price = float(t.get("price", 0))
        size = float(t.get("size", 0))
        side = t.get("side", "").upper()
        title = t.get("title", t.get("market", ""))
        slug = t.get("market_slug", t.get("slug", ""))

        if price > 0:
            prices.append(price)
        if size > 0:
            sizes.append(size * price)  # dollar value

        if side == "BUY" or side == "1":
            buy_count += 1
        elif side == "SELL" or side == "0":
            sell_count += 1

        info = classify_market(title, slug)
        assets[info["asset"]] += 1
        timeframes[info["timeframe"]] += 1
        market_types[info["type"]] += 1
        market_slugs.add(slug)

        ts = t.get("timestamp", t.get("created_at", 0))
        if isinstance(ts, (int, float)) and ts > 1000000000:
            timestamps.append(ts)

    result["buy_pct"] = buy_count / len(trades) * 100 if trades else 0
    result["sell_pct"] = sell_count / len(trades) * 100 if trades else 0
    result["unique_markets"] = len(market_slugs)

    # Price analysis
    if prices:
        result["avg_price"] = sum(prices) / len(prices)
        result["median_price"] = sorted(prices)[len(prices) // 2]
        result["min_price"] = min(prices)
        result["max_price"] = max(prices)

        # Price buckets
        result["pct_under_20"] = sum(1 for p in prices if p < 0.20) / len(prices) * 100
        result["pct_20_40"] = sum(1 for p in prices if 0.20 <= p < 0.40) / len(prices) * 100
        result["pct_40_60"] = sum(1 for p in prices if 0.40 <= p < 0.60) / len(prices) * 100
        result["pct_60_80"] = sum(1 for p in prices if 0.60 <= p < 0.80) / len(prices) * 100
        result["pct_80_plus"] = sum(1 for p in prices if p >= 0.80) / len(prices) * 100
        result["pct_90_plus"] = sum(1 for p in prices if p >= 0.90) / len(prices) * 100
        result["pct_95_plus"] = sum(1 for p in prices if p >= 0.95) / len(prices) * 100

    # Size analysis
    if sizes:
        result["avg_size_usd"] = sum(sizes) / len(sizes)
        result["median_size_usd"] = sorted(sizes)[len(sizes) // 2]
        result["total_volume"] = sum(sizes)

    # Asset distribution
    result["assets"] = dict(sorted(assets.items(), key=lambda x: -x[1]))
    result["timeframes"] = dict(sorted(timeframes.items(), key=lambda x: -x[1]))
    result["market_types"] = dict(sorted(market_types.items(), key=lambda x: -x[1]))

    # Trading frequency
    if len(timestamps) >= 2:
        ts_sorted = sorted(timestamps)
        span_hours = (ts_sorted[-1] - ts_sorted[0]) / 3600
        if span_hours > 0:
            result["trades_per_hour"] = len(timestamps) / span_hours

    # Strategy classification
    result["strategy"] = classify_strategy(result)

    return result


def classify_strategy(r: dict) -> str:
    """Classify the trader's primary strategy."""
    strategies = []

    avg_p = r.get("avg_price", 0.5)
    pct_90 = r.get("pct_90_plus", 0)
    pct_80 = r.get("pct_80_plus", 0)
    pct_under_20 = r.get("pct_under_20", 0)
    buy_pct = r.get("buy_pct", 50)
    assets = r.get("assets", {})
    tfs = r.get("timeframes", {})
    types = r.get("market_types", {})

    # Favorite scalper
    if pct_90 > 60:
        strategies.append("FAVORITE_SCALPER")
    elif pct_80 > 60:
        strategies.append("FAVORITE_BUYER")

    # Underdog buyer
    if pct_under_20 > 40:
        strategies.append("UNDERDOG_BUYER")

    # Market maker (lots of buys AND sells)
    if 35 < buy_pct < 65:
        strategies.append("MARKET_MAKER")

    # Crypto specialist
    crypto_assets = sum(v for k, v in assets.items() if k in
                        ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ADA", "AVAX", "LINK", "SUI", "PEPE"])
    total_assets = sum(assets.values())
    if total_assets > 0 and crypto_assets / total_assets > 0.7:
        strategies.append("CRYPTO_FOCUSED")

    # Short-timeframe trader
    short_tf = sum(v for k, v in tfs.items() if k in ["5m", "15m"])
    if total_assets > 0 and short_tf / total_assets > 0.5:
        strategies.append("SHORT_TIMEFRAME")

    # Event trader
    if types.get("event", 0) > total_assets * 0.5:
        strategies.append("EVENT_TRADER")

    # High frequency
    tph = r.get("trades_per_hour", 0)
    if tph > 5:
        strategies.append("HIGH_FREQUENCY")

    # Large position
    avg_size = r.get("avg_size_usd", 0)
    if avg_size > 1000:
        strategies.append("WHALE")
    elif avg_size > 100:
        strategies.append("MID_SIZE")

    return " + ".join(strategies) if strategies else "UNCLASSIFIED"


def print_trader_summary(r: dict):
    """Print a concise summary of one trader."""
    if not r:
        return

    print(f"\n  {r['name']} ({r['address']})")
    print(f"    Profit: ${r['reported_profit']:,.0f} | Trades fetched: {r['trades_fetched']}")
    print(f"    Strategy: {r['strategy']}")

    if "avg_price" in r:
        print(f"    Avg price: {r['avg_price']:.3f} | Median: {r['median_price']:.3f}")
        print(f"    Price dist: <20c:{r['pct_under_20']:.0f}% | 20-40c:{r['pct_20_40']:.0f}% | "
              f"40-60c:{r['pct_40_60']:.0f}% | 60-80c:{r['pct_60_80']:.0f}% | 80c+:{r['pct_80_plus']:.0f}% | "
              f"90c+:{r['pct_90_plus']:.0f}% | 95c+:{r['pct_95_plus']:.0f}%")

    if "avg_size_usd" in r:
        print(f"    Avg size: ${r['avg_size_usd']:,.0f} | Median: ${r['median_size_usd']:,.0f}")

    print(f"    Buy/Sell: {r['buy_pct']:.0f}% / {r['sell_pct']:.0f}%")
    print(f"    Assets: {r['assets']}")
    print(f"    Timeframes: {r['timeframes']}")
    if "trades_per_hour" in r:
        print(f"    Frequency: {r['trades_per_hour']:.1f} trades/hr")


def main():
    print("=" * 80)
    print("POLYMARKET WHALE STRATEGY ANALYSIS")
    print("Analyzing top traders from Overall + Crypto leaderboards")
    print("=" * 80)

    # Deduplicate accounts
    all_accounts = {}
    for name, addr, profit in CRYPTO_ACCOUNTS + OVERALL_ACCOUNTS:
        addr_lower = addr.lower()
        if addr_lower not in all_accounts or profit > all_accounts[addr_lower][2]:
            all_accounts[addr_lower] = (name, addr, profit)

    accounts = list(all_accounts.values())
    accounts.sort(key=lambda x: -x[2])  # sort by profit

    print(f"\nTotal unique accounts to analyze: {len(accounts)}")

    # Fetch and analyze each
    all_results = []
    for i, (name, addr, profit) in enumerate(accounts):
        print(f"\n[{i+1}/{len(accounts)}] Fetching trades for {name}...", end="", flush=True)
        trades = fetch_trades(addr, limit=200)
        print(f" got {len(trades)} trades")

        if trades:
            result = analyze_trader(name, addr, profit, trades)
            if result:
                all_results.append(result)
                print_trader_summary(result)

        time.sleep(0.5)  # be nice to the API

    # =====================================================
    # AGGREGATE ANALYSIS
    # =====================================================
    print("\n\n" + "=" * 80)
    print("AGGREGATE STRATEGY ANALYSIS")
    print("=" * 80)

    # Strategy distribution
    strategy_counts = defaultdict(list)
    for r in all_results:
        for s in r["strategy"].split(" + "):
            strategy_counts[s].append(r["name"])

    print("\n  Strategy Distribution:")
    for strat, traders in sorted(strategy_counts.items(), key=lambda x: -len(x[1])):
        print(f"    {strat}: {len(traders)} traders - {', '.join(traders[:5])}")

    # Price preference by profitability
    print("\n  Price Preference (avg entry price by profit tier):")
    sorted_results = sorted(all_results, key=lambda x: -x["reported_profit"])
    for i, r in enumerate(sorted_results):
        tier = "TOP 5" if i < 5 else "TOP 10" if i < 10 else "REST"
        avg_p = r.get("avg_price", 0)
        p90 = r.get("pct_90_plus", 0)
        print(f"    [{tier}] {r['name']}: avg {avg_p:.3f}, 90c+: {p90:.0f}%, "
              f"profit: ${r['reported_profit']:,.0f}, strategy: {r['strategy']}")

    # Common patterns among top earners
    print("\n  Patterns Among Top Earners:")
    top_5 = sorted_results[:5]

    avg_prices = [r.get("avg_price", 0.5) for r in top_5]
    avg_p90 = [r.get("pct_90_plus", 0) for r in top_5]
    avg_buy = [r.get("buy_pct", 50) for r in top_5]

    print(f"    Avg entry price: {sum(avg_prices)/len(avg_prices):.3f}")
    print(f"    Avg % at 90c+: {sum(avg_p90)/len(avg_p90):.0f}%")
    print(f"    Avg buy %: {sum(avg_buy)/len(avg_buy):.0f}%")

    # Asset diversification
    print("\n  Asset Diversification:")
    for r in sorted_results[:10]:
        n_assets = len(r.get("assets", {}))
        top_asset = max(r.get("assets", {"?": 1}).items(), key=lambda x: x[1])
        total = sum(r.get("assets", {}).values())
        concentration = top_asset[1] / total * 100 if total else 0
        print(f"    {r['name']}: {n_assets} assets, top={top_asset[0]} ({concentration:.0f}%)")

    # =====================================================
    # STRATEGY EFFECTIVENESS MATRIX
    # =====================================================
    print("\n" + "=" * 80)
    print("STRATEGY EFFECTIVENESS MATRIX")
    print("=" * 80)

    # Group by strategy, calculate avg profit
    strat_profits = defaultdict(list)
    for r in all_results:
        for s in r["strategy"].split(" + "):
            strat_profits[s].append(r["reported_profit"])

    print(f"\n  {'Strategy':<25} {'Count':>5} {'Avg Profit':>12} {'Total Profit':>14} {'Min':>10} {'Max':>12}")
    print(f"  {'-'*25} {'-'*5} {'-'*12} {'-'*14} {'-'*10} {'-'*12}")
    for strat, profits in sorted(strat_profits.items(), key=lambda x: -sum(x[1])/len(x[1])):
        avg = sum(profits) / len(profits)
        print(f"  {strat:<25} {len(profits):>5} ${avg:>11,.0f} ${sum(profits):>13,.0f} "
              f"${min(profits):>9,.0f} ${max(profits):>11,.0f}")

    # =====================================================
    # ACTIONABLE INSIGHTS
    # =====================================================
    print("\n" + "=" * 80)
    print("ACTIONABLE INSIGHTS FOR OUR SYSTEM")
    print("=" * 80)

    # Determine which strategies could work with small capital
    print("""
  Based on analysis of top Polymarket traders:

  WHAT WORKS (among winners):
  - [data populated after analysis]

  WHAT DOESN'T WORK:
  - [data populated after analysis]

  POTENTIAL EDGES FOR $100 CAPITAL:
  - [data populated after analysis]
    """)

    # Save raw results
    output = {
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "accounts_analyzed": len(all_results),
        "results": []
    }
    for r in all_results:
        # Make JSON-serializable
        clean = {k: v for k, v in r.items()}
        output["results"].append(clean)

    with open("scripts/whale_analysis_results.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("\n  Raw results saved to scripts/whale_analysis_results.json")


if __name__ == "__main__":
    main()
