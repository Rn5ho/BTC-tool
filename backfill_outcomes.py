"""Backfill correct WIN/LOSS outcomes for historical trades using Gamma API.

All historical settlements used Binance prices instead of Chainlink (Polymarket's
actual resolution source). This script queries the Gamma API for each market's
real outcome and corrects the DB.

Usage:
    # Dry run (shows what would change, no DB writes)
    python backfill_outcomes.py

    # Apply corrections
    python backfill_outcomes.py --apply

    # Also fix paper trades
    python backfill_outcomes.py --apply --paper
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
# Polymarket fee model (same as paper_trader.py)
FEE_RATE = 0.25
FEE_EXPONENT = 2


def compute_fee_factor(price: float) -> float:
    """Compute Polymarket taker fee factor."""
    return FEE_RATE * (price * (1.0 - price)) ** FEE_EXPONENT


async def get_market_resolution(
    session: aiohttp.ClientSession, slug: str
) -> str | None:
    """Query Gamma API for the actual resolution of a market.

    Returns "UP", "DOWN", or None if the market is unresolved/not found.
    """
    url = f"{GAMMA_URL}/events?slug={slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("Gamma API %d for %s", resp.status, slug)
                return None
            data = await resp.json()
    except Exception as e:
        logger.warning("Gamma API error for %s: %s", slug, e)
        return None

    try:
        events = data if isinstance(data, list) else [data]
        if not events:
            return None

        event = events[0]
        markets = event.get("markets", [])
        if not markets:
            return None

        market = markets[0]

        # Check if resolved
        closed = market.get("closed", False)
        if not closed:
            return None  # Still open

        # Parse outcomes and prices
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        prices = market.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)

        if not outcomes or not prices:
            return None

        # Find winning outcome: outcomePrices = ["1", "0"] means first outcome won
        up_idx = outcomes.index("Up") if "Up" in outcomes else None
        if up_idx is None:
            return None

        up_price = float(prices[up_idx])

        # Resolved markets have outcomePrices of exactly "1" or "0"
        if up_price >= 0.99:
            return "UP"
        elif up_price <= 0.01:
            return "DOWN"
        else:
            # Not fully resolved yet
            return None

    except (KeyError, IndexError, ValueError) as e:
        logger.warning("Parse error for %s: %s", slug, e)
        return None


def compute_pnl(side: str, winner: str, amount_usdc: float, entry_price: float) -> tuple[str, float]:
    """Compute outcome and PnL for a trade given the actual winner.

    Returns (outcome_str, pnl_float).
    """
    won = side == winner
    outcome = "WIN" if won else "LOSS"

    if won:
        fee_factor = compute_fee_factor(entry_price)
        shares = (amount_usdc / entry_price) * (1.0 - fee_factor)
        pnl = shares - amount_usdc  # shares pay $1 each on win
    else:
        pnl = -amount_usdc

    return outcome, round(pnl, 4)


async def main():
    parser = argparse.ArgumentParser(description="Backfill trade outcomes from Gamma API")
    parser.add_argument("--apply", action="store_true", help="Apply corrections to DB")
    parser.add_argument("--paper", action="store_true", help="Also fix paper trades")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite DB")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # ---- Gather trades to check ----
    live_trades = conn.execute(
        "SELECT id, market_slug, side, amount_usdc, entry_price, outcome, pnl "
        "FROM live_trades WHERE success = 1"
    ).fetchall()
    logger.info("Found %d successful live trades", len(live_trades))

    paper_trades = []
    if args.paper:
        paper_trades = conn.execute(
            "SELECT id, market_slug, side, size_usdc, entry_price, outcome, pnl "
            "FROM paper_trades WHERE outcome IS NOT NULL"
        ).fetchall()
        logger.info("Found %d settled paper trades", len(paper_trades))

    # Collect unique slugs
    slugs = set()
    for t in live_trades:
        slugs.add(t["market_slug"])
    for t in paper_trades:
        slugs.add(t["market_slug"])
    logger.info("Unique market slugs to check: %d", len(slugs))

    # ---- Query Gamma API for actual outcomes ----
    resolutions: dict[str, str] = {}  # slug -> "UP" or "DOWN"
    failed_slugs = []

    async with aiohttp.ClientSession() as session:
        for i, slug in enumerate(sorted(slugs)):
            result = await get_market_resolution(session, slug)
            if result:
                resolutions[slug] = result
            else:
                failed_slugs.append(slug)

            if (i + 1) % 20 == 0:
                logger.info("Progress: %d/%d slugs queried", i + 1, len(slugs))

            # Rate limit: ~5 requests/sec to be polite
            await asyncio.sleep(0.2)

    logger.info(
        "Resolutions fetched: %d resolved, %d failed/unresolved",
        len(resolutions), len(failed_slugs),
    )

    # ---- Compare and fix live trades ----
    live_corrections = []
    live_already_correct = 0
    live_no_resolution = 0
    live_missing_entry_price = 0

    for t in live_trades:
        slug = t["market_slug"]
        if slug not in resolutions:
            live_no_resolution += 1
            continue

        winner = resolutions[slug]
        side = t["side"]
        entry_price = t["entry_price"]
        amount = t["amount_usdc"]

        if entry_price is None or entry_price == 0:
            live_missing_entry_price += 1
            continue

        new_outcome, new_pnl = compute_pnl(side, winner, amount, entry_price)
        old_outcome = t["outcome"]
        old_pnl = t["pnl"]

        # Skip early exits — they were sold before resolution at a different PnL
        if old_outcome == "EARLY_EXIT":
            live_already_correct += 1
            continue

        if old_outcome == new_outcome:
            live_already_correct += 1
            continue

        live_corrections.append({
            "id": t["id"],
            "slug": slug,
            "side": side,
            "old_outcome": old_outcome,
            "new_outcome": new_outcome,
            "old_pnl": old_pnl,
            "new_pnl": new_pnl,
            "winner": winner,
        })

    # ---- Compare and fix paper trades ----
    paper_corrections = []
    paper_already_correct = 0

    for t in paper_trades:
        slug = t["market_slug"]
        if slug not in resolutions:
            continue

        winner = resolutions[slug]
        side = t["side"]
        entry_price = t["entry_price"]
        amount = t["size_usdc"]

        if entry_price is None or entry_price == 0:
            continue

        new_outcome, new_pnl = compute_pnl(side, winner, amount, entry_price)
        old_outcome = t["outcome"]

        if old_outcome == new_outcome:
            paper_already_correct += 1
            continue

        paper_corrections.append({
            "id": t["id"],
            "slug": slug,
            "side": side,
            "old_outcome": old_outcome,
            "new_outcome": new_outcome,
            "old_pnl": t["pnl"],
            "new_pnl": new_pnl,
            "winner": winner,
        })

    # ---- Report ----
    print("\n" + "=" * 70)
    print("BACKFILL RESULTS")
    print("=" * 70)

    print(f"\nLive Trades:")
    print(f"  Total checked:      {len(live_trades)}")
    print(f"  Already correct:    {live_already_correct}")
    print(f"  Need correction:    {len(live_corrections)}")
    print(f"  No resolution data: {live_no_resolution}")
    print(f"  Missing entry_price:{live_missing_entry_price}")

    if live_corrections:
        old_pnl_sum = sum(c["old_pnl"] or 0 for c in live_corrections)
        new_pnl_sum = sum(c["new_pnl"] for c in live_corrections)
        flipped_to_win = sum(1 for c in live_corrections if c["new_outcome"] == "WIN")
        flipped_to_loss = sum(1 for c in live_corrections if c["new_outcome"] == "LOSS")

        print(f"\n  Corrections detail:")
        print(f"    LOSS -> WIN: {flipped_to_win}")
        print(f"    WIN -> LOSS: {flipped_to_loss}")
        print(f"    Old PnL sum: ${old_pnl_sum:+.2f}")
        print(f"    New PnL sum: ${new_pnl_sum:+.2f}")
        print(f"    PnL delta:   ${new_pnl_sum - old_pnl_sum:+.2f}")

        print(f"\n  Individual corrections:")
        for c in live_corrections:
            print(
                f"    #{c['id']:3d} {c['side']:4s} {c['slug'][-10:]} "
                f"{c['old_outcome'] or 'NULL':>4s} -> {c['new_outcome']:4s} "
                f"(actual: {c['winner']}) "
                f"pnl: ${c['old_pnl'] or 0:+.2f} -> ${c['new_pnl']:+.2f}"
            )

    if args.paper and paper_corrections:
        print(f"\nPaper Trades:")
        print(f"  Total checked:   {len(paper_trades)}")
        print(f"  Already correct: {paper_already_correct}")
        print(f"  Need correction: {len(paper_corrections)}")

        old_pnl_sum = sum(c["old_pnl"] or 0 for c in paper_corrections)
        new_pnl_sum = sum(c["new_pnl"] for c in paper_corrections)
        print(f"  PnL delta:       ${new_pnl_sum - old_pnl_sum:+.2f}")

    # ---- Apply ----
    if args.apply and (live_corrections or paper_corrections):
        print(f"\nApplying corrections...")
        now_ms = int(time.time() * 1000)

        for c in live_corrections:
            conn.execute(
                "UPDATE live_trades SET outcome = ?, pnl = ?, settled_at = ? WHERE id = ?",
                (c["new_outcome"], c["new_pnl"], now_ms, c["id"]),
            )

        for c in paper_corrections:
            conn.execute(
                "UPDATE paper_trades SET outcome = ?, pnl = ? WHERE id = ?",
                (c["new_outcome"], c["new_pnl"], c["id"]),
            )

        conn.commit()
        total = len(live_corrections) + len(paper_corrections)
        print(f"  Updated {total} trades in DB.")
    elif not args.apply and (live_corrections or paper_corrections):
        total = len(live_corrections) + len(paper_corrections)
        print(f"\n  DRY RUN: {total} trades would be corrected. Use --apply to write.")

    # ---- Also settle any unsettled trades ----
    unsettled_live = conn.execute(
        "SELECT id, market_slug, side, amount_usdc, entry_price "
        "FROM live_trades WHERE success = 1 AND outcome IS NULL"
    ).fetchall()

    if unsettled_live:
        unsettled_fixed = 0
        for t in unsettled_live:
            slug = t["market_slug"]
            if slug not in resolutions:
                continue
            entry_price = t["entry_price"]
            if not entry_price:
                continue

            winner = resolutions[slug]
            outcome, pnl = compute_pnl(t["side"], winner, t["amount_usdc"], entry_price)

            if args.apply:
                conn.execute(
                    "UPDATE live_trades SET outcome = ?, pnl = ?, settled_at = ? WHERE id = ?",
                    (outcome, pnl, int(time.time() * 1000), t["id"]),
                )
            unsettled_fixed += 1
            print(
                f"    Settled #{t['id']:3d} {t['side']:4s} {slug[-10:]} -> "
                f"{outcome} (actual: {winner}) pnl: ${pnl:+.2f}"
            )

        if unsettled_fixed:
            if args.apply:
                conn.commit()
                print(f"\n  Settled {unsettled_fixed} previously unsettled trades.")
            else:
                print(f"\n  DRY RUN: {unsettled_fixed} unsettled trades would be settled.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
