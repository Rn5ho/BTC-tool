"""Backfill Gamma API resolution data for all live trades.

Adds gamma_resolution ('UP'/'DOWN') and gamma_winner columns to live_trades
WITHOUT modifying existing outcome/pnl/settlement_price fields. This gives us
ground-truth settlement direction from Polymarket's own resolution source.

Usage:
    # Dry run (shows stats, no DB writes)
    python backfill_gamma_resolution.py

    # Apply to DB
    python backfill_gamma_resolution.py --apply

    # Use a local DB file
    python backfill_gamma_resolution.py --apply --db btc_edge.db
"""

import argparse
import asyncio
import json
import logging
import sqlite3
import time

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
DB_PATH = "btc_edge.db"


async def get_market_resolution(
    session: aiohttp.ClientSession, slug: str
) -> str | None:
    """Query Gamma API for the actual resolution of a market.

    Returns "UP" or "DOWN", or None if unresolved/not found.
    """
    url = f"{GAMMA_URL}/events?slug={slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    try:
        events = data if isinstance(data, list) else [data]
        if not events:
            return None

        market = events[0].get("markets", [None])[0]
        if not market or not market.get("closed", False):
            return None

        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        prices = market.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)

        if not outcomes or not prices:
            return None

        up_idx = outcomes.index("Up") if "Up" in outcomes else None
        if up_idx is None:
            return None

        up_price = float(prices[up_idx])
        if up_price >= 0.99:
            return "UP"
        elif up_price <= 0.01:
            return "DOWN"
        return None

    except (KeyError, IndexError, ValueError):
        return None


async def main():
    parser = argparse.ArgumentParser(description="Backfill Gamma resolution data")
    parser.add_argument("--apply", action="store_true", help="Write to DB")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite DB")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Add columns if they don't exist
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(live_trades)").fetchall()
    }
    if "gamma_resolution" not in existing:
        conn.execute("ALTER TABLE live_trades ADD COLUMN gamma_resolution TEXT")
        logger.info("Added gamma_resolution column")
    if "gamma_winner_matches" not in existing:
        conn.execute("ALTER TABLE live_trades ADD COLUMN gamma_winner_matches INTEGER")
        logger.info("Added gamma_winner_matches column")
    conn.commit()

    # Get all trades that need backfill
    trades = conn.execute(
        "SELECT id, market_slug, side, outcome, entry_price "
        "FROM live_trades "
        "WHERE success = 1 AND gamma_resolution IS NULL"
    ).fetchall()
    logger.info("Trades needing gamma resolution: %d", len(trades))

    if not trades:
        print("Nothing to backfill.")
        conn.close()
        return

    # Collect unique slugs
    slugs = sorted(set(t["market_slug"] for t in trades))
    logger.info("Unique slugs to query: %d", len(slugs))

    # Query Gamma API
    resolutions: dict[str, str] = {}
    async with aiohttp.ClientSession() as session:
        for i, slug in enumerate(slugs):
            result = await get_market_resolution(session, slug)
            if result:
                resolutions[slug] = result

            if (i + 1) % 50 == 0:
                logger.info("Progress: %d/%d slugs", i + 1, len(slugs))

            await asyncio.sleep(0.2)  # rate limit

    logger.info("Resolved: %d / %d slugs", len(resolutions), len(slugs))

    # Compute results
    updates = []
    no_resolution = 0
    for t in trades:
        slug = t["market_slug"]
        if slug not in resolutions:
            no_resolution += 1
            continue

        gamma_dir = resolutions[slug]
        # Does the Gamma resolution match our trade's side winning?
        side_would_win = (t["side"] == gamma_dir)
        actual_outcome = t["outcome"]

        # Compare: does our recorded outcome match Gamma truth?
        if actual_outcome == "EARLY_EXIT":
            # EE trades: gamma tells us what WOULD have happened
            matches = 1 if side_would_win else 0
        elif actual_outcome in ("WIN", "LOSS"):
            expected = "WIN" if side_would_win else "LOSS"
            matches = 1 if actual_outcome == expected else 0
        else:
            matches = None  # unsettled

        updates.append({
            "id": t["id"],
            "gamma_resolution": gamma_dir,
            "gamma_winner_matches": matches,
        })

    # Report
    total_settled = [u for u in updates if u["gamma_winner_matches"] is not None]
    correct = sum(1 for u in total_settled if u["gamma_winner_matches"] == 1)
    wrong = sum(1 for u in total_settled if u["gamma_winner_matches"] == 0)

    # EE-specific stats
    ee_trades = [t for t in trades if t["outcome"] == "EARLY_EXIT"]
    ee_slugs = {t["market_slug"] for t in ee_trades}
    ee_would_win = sum(
        1 for u in updates
        for t in trades
        if t["id"] == u["id"] and t["outcome"] == "EARLY_EXIT"
        and u["gamma_winner_matches"] == 1
    )
    ee_would_lose = sum(
        1 for u in updates
        for t in trades
        if t["id"] == u["id"] and t["outcome"] == "EARLY_EXIT"
        and u["gamma_winner_matches"] == 0
    )

    print("\n" + "=" * 60)
    print("GAMMA RESOLUTION BACKFILL")
    print("=" * 60)
    print(f"\n  Trades to update:    {len(updates)}")
    print(f"  No resolution:       {no_resolution}")
    print(f"  Settled match:       {correct} correct, {wrong} wrong")
    if ee_would_win + ee_would_lose > 0:
        print(f"  EE would-win:        {ee_would_win}")
        print(f"  EE would-lose:       {ee_would_lose}")
        print(f"  EE would-win rate:   {100*ee_would_win/(ee_would_win+ee_would_lose):.1f}%")

    if args.apply and updates:
        for u in updates:
            conn.execute(
                "UPDATE live_trades SET gamma_resolution = ?, gamma_winner_matches = ? "
                "WHERE id = ?",
                (u["gamma_resolution"], u["gamma_winner_matches"], u["id"]),
            )
        conn.commit()
        print(f"\n  Written {len(updates)} rows to DB.")
    elif updates:
        print(f"\n  DRY RUN: {len(updates)} rows would be updated. Use --apply.")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
