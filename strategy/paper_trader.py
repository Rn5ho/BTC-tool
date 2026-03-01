"""Paper trading engine for simulating bets on Polymarket 5-min BTC markets.

Accounts for Polymarket taker fees when computing PnL.  Buying shares at
price *p* with fee factor *f* means you receive fewer shares:
    shares = (size_usdc / p) * (1 - f)
On a win the payout is shares * $1, so:
    win_pnl  = shares - size_usdc
    loss_pnl = -size_usdc
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from data.models import PaperTrade
from data.polymarket import compute_fee_factor
from storage.db import Database

logger = logging.getLogger(__name__)


class PaperTrader:
    """Simulates placing and settling binary bets on Polymarket 5-min BTC windows.

    Fee model mirrors Polymarket's actual taker fee structure using the
    bell-curve formula: fee_factor = fee_rate * (price * (1 - price))^exponent.
    """

    def __init__(
        self,
        db: Database,
        bankroll: float = 100.0,
        bet_size: float = 5.0,
        use_kelly: bool = False,
        fee_rate: float = 0.0,
        fee_exponent: int = 2,
        sizing_strategy: str = "fixed",
    ) -> None:
        self.db = db
        self.bankroll = bankroll
        self.initial_bankroll = bankroll
        self.bet_size = bet_size
        self.use_kelly = use_kelly
        self.fee_rate = fee_rate
        self.fee_exponent = fee_exponent
        self.sizing_strategy = sizing_strategy
        self._pending_trades: dict[str, dict] = {}
        self._total_fees: float = 0.0  # cumulative fees paid

        # Adaptive sizing state
        self._recent_outcomes: list[bool] = []  # rolling window of win/loss
        self._consecutive_losses: int = 0
        self._max_bankroll: float = bankroll  # for drawdown tracking

    async def restore_bankroll(self) -> None:
        """Restore bankroll from historical trades in the database.

        On restart the in-memory bankroll resets to initial_bankroll,
        but the DB still holds P&L from all previous sessions.  This
        method adds the cumulative historical P&L so the bankroll and
        the displayed total_pnl stay consistent.
        """
        stats = await self.db.get_trading_stats()
        historical_pnl = stats.get("total_pnl", 0.0)
        if historical_pnl != 0.0:
            self.bankroll = self.initial_bankroll + historical_pnl
            logger.info(
                "Restored bankroll from DB: initial=$%.2f + historical_pnl=$%+.2f = $%.2f",
                self.initial_bankroll,
                historical_pnl,
                self.bankroll,
            )

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

    # Hour-based sizing multipliers (UTC).
    # Derived from backtesting: training test set + out-of-sample validation.
    # Consistently strong hours get 1.5x, good hours 1.2x, weak hours 0.7x.
    #   Strong (both >57% WR): 9, 14, 20
    #   Good   (both >53% WR): 6, 8, 10, 12, 16, 18, 22
    #   Weak   (both <50% WR): 4, 7
    #   Neutral (rest): 1.0x
    HOUR_MULTIPLIERS = {
        4: 0.7, 7: 0.7,                                # weak
        6: 1.2, 8: 1.2, 10: 1.2, 12: 1.2,             # good
        16: 1.2, 18: 1.2, 22: 1.2,                     # good
        9: 1.5, 14: 1.5, 20: 1.5,                      # strong
    }

    def adaptive_size(self, confidence: float) -> float:
        """Hybrid adaptive sizing: adjusts bet based on multiple factors.

        Base: 2% of bankroll
        Multipliers:
        - Confidence (|P-0.5|): 0.5x at low confidence, up to 2.0x at high
        - Hour-of-day: 0.7x-1.5x based on backtested hourly WR
        - Streak: halve after 5+ consecutive losses
        - Drawdown: halve if drawdown > 25%
        - Rolling WR: 1.3x if last 20 trades >55% WR, 0.7x if <45%

        Final size clamped to [0.5%, 8%] of bankroll.
        """
        base_pct = 0.02  # 2% of bankroll

        # Confidence multiplier: scale 0.5x-2.0x based on |P(up) - 0.5|
        # confidence ranges 0.0 (pure coin flip) to 0.45 (max model output)
        conf_mult = 0.5 + (confidence / 0.45) * 1.5
        conf_mult = max(0.5, min(conf_mult, 2.0))

        # Hour-of-day multiplier
        utc_hour = datetime.now(timezone.utc).hour
        hour_mult = self.HOUR_MULTIPLIERS.get(utc_hour, 1.0)

        # Streak multiplier: reduce after consecutive losses
        streak_mult = 1.0
        if self._consecutive_losses >= 5:
            streak_mult = 0.5
        elif self._consecutive_losses >= 3:
            streak_mult = 0.75

        # Drawdown multiplier
        dd_mult = 1.0
        if self._max_bankroll > 0:
            drawdown = (self._max_bankroll - self.bankroll) / self._max_bankroll
            if drawdown > 0.25:
                dd_mult = 0.5
            elif drawdown > 0.15:
                dd_mult = 0.75

        # Rolling WR multiplier (last 20 trades)
        wr_mult = 1.0
        if len(self._recent_outcomes) >= 10:
            recent_wr = sum(self._recent_outcomes[-20:]) / len(self._recent_outcomes[-20:])
            if recent_wr > 0.55:
                wr_mult = 1.3
            elif recent_wr < 0.45:
                wr_mult = 0.7

        final_pct = base_pct * conf_mult * hour_mult * streak_mult * dd_mult * wr_mult
        final_pct = max(0.005, min(final_pct, 0.08))  # clamp 0.5%-8%

        size = final_pct * self.bankroll

        logger.debug(
            "Adaptive size: base=2%% x conf=%.2f x hour=%.2f x streak=%.2f x dd=%.2f x wr=%.2f "
            "= %.1f%% -> $%.2f",
            conf_mult, hour_mult, streak_mult, dd_mult, wr_mult,
            final_pct * 100, size,
        )

        return size

    def compute_bet_size(
        self, our_prob: float, market_prob: float, confidence: float = 0.0
    ) -> float:
        """Return the bet size in USDC, never exceeding the current bankroll."""
        if self.sizing_strategy == "kelly" or self.use_kelly:
            size = self.kelly_size(our_prob, market_prob)
        elif self.sizing_strategy == "adaptive":
            size = self.adaptive_size(confidence)
        else:
            size = self.bet_size

        # Enforce $1 minimum (Polymarket minimum stake)
        size = max(1.00, size)
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

        bet_size = self.compute_bet_size(
            signal["our_prob"],
            signal["market_prob"],
            confidence=signal.get("confidence", abs(signal["our_prob"] - 0.5)),
        )

        # Exploration trades: override sizing to minimum for data collection
        if signal.get("size_override"):
            bet_size = signal["size_override"]

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
            entry_spread=signal.get("spread"),
            midpoint_price=signal.get("midpoint_price"),
            trade_tag="exploration" if signal.get("exploration") else None,
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
            "fee_factor": signal.get("fee_factor", 0.0),
            "spread": signal.get("spread"),
            "midpoint_price": signal.get("midpoint_price"),
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

        # Calculate PnL accounting for taker fees.
        # Fee factor comes from the signal if available, otherwise compute it.
        fee_factor = info.get("fee_factor", 0.0)
        if fee_factor == 0.0 and self.fee_rate > 0.0:
            fee_factor = compute_fee_factor(
                entry_price, self.fee_rate, self.fee_exponent
            )

        # Shares received = (size_usdc / price) * (1 - fee_factor)
        shares = (size_usdc / entry_price) * (1.0 - fee_factor)
        fee_usdc = size_usdc * fee_factor  # approximate fee in USDC
        self._total_fees += fee_usdc

        if won:
            # Each winning share pays $1.00
            pnl = shares - size_usdc
        else:
            # Shares worth $0 -- entire stake lost
            pnl = -size_usdc

        # Update bankroll
        self.bankroll += pnl

        # Track adaptive sizing state
        self._recent_outcomes.append(won)
        if len(self._recent_outcomes) > 50:
            self._recent_outcomes = self._recent_outcomes[-50:]
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
        if self.bankroll > self._max_bankroll:
            self._max_bankroll = self.bankroll

        # Persist settlement
        settled_at = int(time.time() * 1000)
        if trade_id is not None:
            await self.db.update_paper_trade(trade_id, outcome, pnl, settled_at)

        # Clean up
        del self._pending_trades[market_slug]

        logger.info(
            "Settled %s %s -> %s | pnl=$%.2f (fee=$%.2f) | bankroll=$%.2f",
            side,
            market_slug,
            outcome,
            pnl,
            fee_usdc,
            self.bankroll,
        )

    async def settle_all_pending(
        self, btc_start_price: float, btc_end_price: float
    ) -> None:
        """Settle every unsettled trade using the BTC price movement."""
        btc_went_up = btc_end_price > btc_start_price  # strict: flat = DOWN on Polymarket

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
        stats["total_fees"] = self._total_fees
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
            f"Total P&L: ${stats.get('total_pnl', 0.0):.2f} | "
            f"Fees paid: ${stats.get('total_fees', 0.0):.2f}\n"
            f"Bankroll: ${stats.get('bankroll', 0.0):.2f} (ROI: {roi_pct:.1f}%)\n"
            f"Avg edge: {avg_edge_pct:.1f}% | "
            f"Avg P&L/trade: ${stats.get('avg_pnl_per_trade', 0.0):.2f}"
        )
