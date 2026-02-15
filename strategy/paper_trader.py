"""Paper trading engine for simulating bets on Polymarket 5-min BTC markets."""

import logging
import time
from typing import Optional

from data.models import PaperTrade, PolymarketMarket
from storage.db import Database

logger = logging.getLogger(__name__)


class PaperTrader:
    """Simulates placing and settling binary bets on Polymarket 5-min BTC windows."""

    def __init__(
        self,
        db: Database,
        bankroll: float = 10000.0,
        bet_size: float = 50.0,
        use_kelly: bool = False,
    ) -> None:
        self.db = db
        self.bankroll = bankroll
        self.initial_bankroll = bankroll
        self.bet_size = bet_size
        self.use_kelly = use_kelly
        self._pending_trades: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def kelly_size(self, our_prob: float, market_odds: float) -> float:
        """Compute half-Kelly bet size.

        Kelly criterion: f* = (p * b - q) / b
        where p = our_prob, q = 1 - p, b = net odds = (1 / market_odds) - 1.

        We use half-Kelly for safety and clip to [0, 0.05] of bankroll.
        """
        if market_odds <= 0.0 or market_odds >= 1.0:
            return 0.0

        p = our_prob
        q = 1.0 - p
        b = (1.0 / market_odds) - 1.0  # net odds

        if b <= 0.0:
            return 0.0

        f_star = (p * b - q) / b

        if f_star <= 0.0:
            return 0.0

        actual_fraction = f_star / 2.0  # half-Kelly
        actual_fraction = max(0.0, min(actual_fraction, 0.05))

        return actual_fraction * self.bankroll

    def compute_bet_size(self, our_prob: float, market_prob: float) -> float:
        """Return the bet size in USDC, never exceeding the current bankroll."""
        if self.use_kelly:
            size = self.kelly_size(our_prob, market_prob)
        else:
            size = self.bet_size

        return min(size, self.bankroll)

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------

    async def place_trade(self, signal: dict) -> Optional[int]:
        """Open a new paper trade from a signal dict.

        signal keys: side, our_prob, market_prob, edge, entry_price, market_slug

        Returns the trade id on success, or None if the trade was skipped.
        """
        market_slug = signal["market_slug"]

        # Guard against duplicate pending trades on the same market
        if market_slug in self._pending_trades:
            logger.warning(
                "Already have a pending trade for %s -- skipping", market_slug
            )
            return None

        bet_size = self.compute_bet_size(signal["our_prob"], signal["market_prob"])

        if bet_size <= 0:
            logger.info(
                "Bet size is zero for %s (edge=%.4f) -- skipping",
                market_slug,
                signal["edge"],
            )
            return None

        trade = PaperTrade(
            timestamp=int(time.time() * 1000),
            market_slug=market_slug,
            side=signal["side"],
            our_prob=signal["our_prob"],
            market_prob=signal["market_prob"],
            edge=signal["edge"],
            size_usdc=bet_size,
            entry_price=signal["entry_price"],
        )

        await self.db.save_paper_trade(trade)

        # Retrieve the id that was just written so we can update later.
        # The DB returns unsettled trades; grab the one matching this slug/ts.
        unsettled = await self.db.get_unsettled_trades()
        trade_id: Optional[int] = None
        for row in unsettled:
            if row["market_slug"] == market_slug and row["timestamp"] == trade.timestamp:
                trade_id = row["id"]
                break

        self._pending_trades[market_slug] = {
            "trade_id": trade_id,
            "side": signal["side"],
            "size_usdc": bet_size,
            "entry_price": signal["entry_price"],
            "our_prob": signal["our_prob"],
            "market_prob": signal["market_prob"],
            "edge": signal["edge"],
        }

        logger.info(
            "Placed paper trade: %s %s | size=$%.2f entry=%.4f edge=%.4f",
            signal["side"],
            market_slug,
            bet_size,
            signal["entry_price"],
            signal["edge"],
        )

        return trade_id

    async def settle_trade(self, market_slug: str, btc_went_up: bool) -> None:
        """Settle a single pending trade given the BTC outcome."""
        if market_slug not in self._pending_trades:
            logger.warning("No pending trade found for %s -- nothing to settle", market_slug)
            return

        info = self._pending_trades[market_slug]
        side = info["side"]
        size_usdc = info["size_usdc"]
        entry_price = info["entry_price"]
        trade_id = info["trade_id"]

        # Determine outcome
        won = (side == "UP" and btc_went_up) or (side == "DOWN" and not btc_went_up)
        outcome = "WIN" if won else "LOSS"

        # Calculate PnL
        if won:
            # Bought a share at entry_price that is now worth 1.0
            pnl = size_usdc * ((1.0 - entry_price) / entry_price)
        else:
            # Share is now worth 0 -- entire stake is lost
            pnl = -size_usdc

        # Update bankroll
        self.bankroll += pnl

        # Persist settlement
        settled_at = int(time.time() * 1000)
        if trade_id is not None:
            await self.db.update_paper_trade(trade_id, outcome, pnl, settled_at)

        # Clean up
        del self._pending_trades[market_slug]

        logger.info(
            "Settled %s %s -> %s | pnl=$%.2f | bankroll=$%.2f",
            side,
            market_slug,
            outcome,
            pnl,
            self.bankroll,
        )

    async def settle_all_pending(
        self, btc_start_price: float, btc_end_price: float
    ) -> None:
        """Settle every unsettled trade using the BTC price movement."""
        btc_went_up = btc_end_price >= btc_start_price

        # Pull unsettled rows from DB so we also catch trades that may have
        # been recorded in a previous session but not yet settled.
        unsettled = await self.db.get_unsettled_trades()
        for row in unsettled:
            slug = row["market_slug"]
            # Make sure it is tracked locally so settle_trade can find it
            if slug not in self._pending_trades:
                self._pending_trades[slug] = {
                    "trade_id": row["id"],
                    "side": row["side"],
                    "size_usdc": row["size_usdc"],
                    "entry_price": row["entry_price"],
                    "our_prob": row["our_prob"],
                    "market_prob": row["market_prob"],
                    "edge": row["edge"],
                }
            await self.settle_trade(slug, btc_went_up)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        """Return combined trading statistics including current bankroll."""
        stats = await self.db.get_trading_stats()
        stats["bankroll"] = self.bankroll
        if self.initial_bankroll > 0:
            stats["roi"] = (self.bankroll - self.initial_bankroll) / self.initial_bankroll
        else:
            stats["roi"] = 0.0
        return stats

    @staticmethod
    def format_stats_report(stats: dict) -> str:
        """Return a human-readable summary of trading performance."""
        win_rate_pct = stats.get("win_rate", 0.0) * 100
        roi_pct = stats.get("roi", 0.0) * 100
        avg_edge_pct = stats.get("avg_edge", 0.0) * 100

        return (
            "Paper Trading Stats:\n"
            f"Total trades: {stats.get('total_trades', 0)} | "
            f"Settled: {stats.get('settled_trades', 0)}\n"
            f"Win rate: {win_rate_pct:.1f}%\n"
            f"Total P&L: ${stats.get('total_pnl', 0.0):.2f}\n"
            f"Bankroll: ${stats.get('bankroll', 0.0):.2f} (ROI: {roi_pct:.1f}%)\n"
            f"Avg edge: {avg_edge_pct:.1f}% | "
            f"Avg P&L/trade: ${stats.get('avg_pnl_per_trade', 0.0):.2f}"
        )
