"""Live trading engine for placing real orders on Polymarket via py-clob-client.

Mirrors the PaperTrader interface so the orchestrator can drive both traders
with the same signal dict.  All py-clob-client calls are synchronous; we wrap
them with ``asyncio.to_thread`` to keep the event loop responsive.

Safety features:
- Bet size = live_bet_pct * current bankroll (default 2%)
- Bankroll check before every order (never bet more than available)
- FOK (Fill-Or-Kill) orders — no partial fills hanging around
- All trades logged to ``live_trades`` table for auditability
- Graceful degradation: if CLOB API fails, trade is skipped (paper continues)
"""

import asyncio
import logging
import time
from typing import Optional

from data.polymarket import compute_fee_factor
from storage.db import Database

logger = logging.getLogger(__name__)

# Lazy import — py-clob-client may not be installed
_CLOB_AVAILABLE = False
ClobClient = None
ApiCreds = None
MarketOrderArgs = None
OrderType = None
BalanceAllowanceParams = None
AssetType = None
BUY = "BUY"

try:
    from py_clob_client.client import ClobClient as _ClobClient
    from py_clob_client.clob_types import (
        ApiCreds as _ApiCreds,
        MarketOrderArgs as _MarketOrderArgs,
        OrderType as _OrderType,
        BalanceAllowanceParams as _BalanceAllowanceParams,
        AssetType as _AssetType,
    )
    from py_clob_client.order_builder.constants import BUY as _BUY

    ClobClient = _ClobClient
    ApiCreds = _ApiCreds
    MarketOrderArgs = _MarketOrderArgs
    OrderType = _OrderType
    BalanceAllowanceParams = _BalanceAllowanceParams
    AssetType = _AssetType
    BUY = _BUY
    _CLOB_AVAILABLE = True
except ImportError:
    logger.warning(
        "py-clob-client not installed — live trading unavailable. "
        "Install with: pip install py-clob-client"
    )


class LiveTrader:
    """Places real orders on Polymarket CLOB and tracks live P&L.

    Uses the same signal interface as PaperTrader:
        signal = {side, our_prob, market_prob, edge, entry_price, market_slug, fee_factor}

    Bet sizing: live_bet_pct * current bankroll (default 2%).
    """

    def __init__(
        self,
        db: Database,
        private_key: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
        funder: str,
        bankroll: float = 100.0,
        bet_pct: float = 0.02,
        fee_rate: float = 0.25,
        fee_exponent: int = 2,
    ) -> None:
        self.db = db
        self.bankroll = bankroll
        self.initial_bankroll = bankroll
        self.bet_pct = bet_pct
        self.fee_rate = fee_rate
        self.fee_exponent = fee_exponent
        self._pending_trades: dict[str, dict] = {}
        self._total_fees: float = 0.0
        self._paused: bool = False
        self._client = None

        if not _CLOB_AVAILABLE:
            logger.error("py-clob-client not available — LiveTrader will not place orders")
            return

        try:
            from py_clob_client.constants import POLYGON

            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=passphrase,
            )
            self._client = ClobClient(
                "https://clob.polymarket.com",
                key=private_key,
                chain_id=POLYGON,
                creds=creds,
                signature_type=0,
                funder=funder,
            )
            logger.info(
                "LiveTrader initialized: bankroll=$%.2f, bet_pct=%.1f%%, funder=%s...%s",
                bankroll,
                bet_pct * 100,
                funder[:6],
                funder[-4:],
            )
        except Exception:
            logger.exception("Failed to initialize ClobClient — live trading disabled")
            self._client = None

    @property
    def is_ready(self) -> bool:
        """True if the CLOB client was initialized successfully."""
        return self._client is not None

    # ------------------------------------------------------------------
    # Bankroll persistence
    # ------------------------------------------------------------------

    async def restore_bankroll(self) -> None:
        """Restore live bankroll from historical trades in the database."""
        stats = await self.db.get_live_trading_stats()
        historical_pnl = stats.get("total_pnl", 0.0)
        if historical_pnl != 0.0:
            self.bankroll = self.initial_bankroll + historical_pnl
            logger.info(
                "Live bankroll restored: initial=$%.2f + pnl=$%+.2f = $%.2f",
                self.initial_bankroll,
                historical_pnl,
                self.bankroll,
            )

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def compute_bet_size(self, our_prob: float = 0.0, market_prob: float = 0.0) -> float:
        """Return bet size = bet_pct * bankroll, capped at bankroll."""
        size = self.bet_pct * self.bankroll
        # Minimum $0.50 to avoid dust orders that Polymarket may reject
        if size < 0.50:
            return 0.0
        return min(size, self.bankroll)

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------

    async def place_trade(self, signal: dict) -> Optional[str]:
        """Place a real market buy order on Polymarket CLOB.

        signal keys: side, our_prob, market_prob, edge, entry_price,
                     market_slug, fee_factor, token_id

        Returns the order_id on success, or None if skipped/failed.
        """
        if not self.is_ready:
            return None

        if self._paused:
            return None

        market_slug = signal["market_slug"]

        # Guard against duplicate pending trades on the same market
        if market_slug in self._pending_trades:
            logger.debug("Live: already have pending trade for %s", market_slug)
            return None

        # Need token_id to place the order
        token_id = signal.get("token_id")
        if not token_id:
            logger.warning("Live: no token_id in signal for %s — skipping", market_slug)
            return None

        bet_size = self.compute_bet_size(signal["our_prob"], signal["market_prob"])
        if bet_size <= 0:
            logger.info("Live: bet size too small ($%.2f) — skipping", bet_size)
            return None

        if bet_size > self.bankroll:
            logger.warning(
                "Live: bet $%.2f exceeds bankroll $%.2f — skipping",
                bet_size,
                self.bankroll,
            )
            return None

        # Place the order via CLOB API (sync → thread)
        order_id = await self._place_market_buy(token_id, bet_size)
        if order_id is None:
            return None

        # Record in DB
        trade_id = await self.db.save_live_trade(
            timestamp=int(time.time() * 1000),
            market_slug=market_slug,
            side=signal["side"],
            our_prob=signal["our_prob"],
            market_prob=signal["market_prob"],
            edge=signal["edge"],
            size_usdc=bet_size,
            entry_price=signal["entry_price"],
            order_id=order_id,
        )

        self._pending_trades[market_slug] = {
            "trade_id": trade_id,
            "order_id": order_id,
            "side": signal["side"],
            "size_usdc": bet_size,
            "entry_price": signal["entry_price"],
            "our_prob": signal["our_prob"],
            "market_prob": signal["market_prob"],
            "edge": signal["edge"],
            "fee_factor": signal.get("fee_factor", 0.0),
        }

        logger.info(
            "LIVE TRADE PLACED: %s %s | size=$%.2f entry=%.4f edge=%.4f | order=%s",
            signal["side"],
            market_slug,
            bet_size,
            signal["entry_price"],
            signal["edge"],
            order_id,
        )
        return order_id

    async def _place_market_buy(self, token_id: str, amount_usdc: float) -> Optional[str]:
        """Place a FOK market buy order via py-clob-client.

        Returns the order_id on success, None on failure.
        """
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usdc,
                side=BUY,
            )
            signed_order = await asyncio.to_thread(
                self._client.create_market_order, order_args
            )
            resp = await asyncio.to_thread(
                self._client.post_order, signed_order, OrderType.FOK
            )

            if not resp:
                logger.error("Live: empty response from post_order")
                return None

            success = resp.get("success", False)
            order_id = resp.get("orderID") or resp.get("order_id", "")

            if success and order_id:
                logger.info("Live: order placed successfully: %s", order_id)
                return order_id
            else:
                error_msg = resp.get("errorMsg") or resp.get("error", str(resp))
                logger.error("Live: order rejected: %s", error_msg)
                return None

        except Exception:
            logger.exception("Live: failed to place market buy order")
            return None

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def settle_trade(self, market_slug: str, btc_went_up: bool) -> None:
        """Settle a single pending live trade given the BTC outcome."""
        if market_slug not in self._pending_trades:
            return

        info = self._pending_trades[market_slug]
        side = info["side"]
        size_usdc = info["size_usdc"]
        entry_price = info["entry_price"]
        trade_id = info["trade_id"]

        won = (side == "UP" and btc_went_up) or (side == "DOWN" and not btc_went_up)
        outcome = "WIN" if won else "LOSS"

        # PnL with fee model (same as paper trader)
        fee_factor = info.get("fee_factor", 0.0)
        if fee_factor == 0.0 and self.fee_rate > 0.0:
            fee_factor = compute_fee_factor(entry_price, self.fee_rate, self.fee_exponent)

        shares = (size_usdc / entry_price) * (1.0 - fee_factor)
        fee_usdc = size_usdc * fee_factor
        self._total_fees += fee_usdc

        if won:
            pnl = shares - size_usdc
        else:
            pnl = -size_usdc

        self.bankroll += pnl

        # Persist settlement
        settled_at = int(time.time() * 1000)
        if trade_id is not None:
            await self.db.update_live_trade(trade_id, outcome, pnl, settled_at)

        del self._pending_trades[market_slug]

        logger.info(
            "LIVE SETTLED: %s %s -> %s | pnl=$%+.2f (fee=$%.2f) | bankroll=$%.2f",
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
        """Settle all pending live trades using the BTC price movement."""
        btc_went_up = btc_end_price >= btc_start_price

        # Also recover any unsettled trades from DB (e.g. after restart)
        unsettled = await self.db.get_unsettled_live_trades()
        for row in unsettled:
            slug = row["market_slug"]
            if slug not in self._pending_trades:
                self._pending_trades[slug] = {
                    "trade_id": row["id"],
                    "order_id": row.get("order_id", ""),
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
        """Return live trading statistics including current bankroll."""
        stats = await self.db.get_live_trading_stats()
        stats["bankroll"] = self.bankroll
        stats["total_fees"] = self._total_fees
        if self.initial_bankroll > 0:
            stats["roi"] = (self.bankroll - self.initial_bankroll) / self.initial_bankroll
        else:
            stats["roi"] = 0.0
        return stats

    async def get_usdc_balance(self) -> Optional[float]:
        """Fetch actual on-chain USDC.e balance from Polymarket."""
        if not self.is_ready:
            return None
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = await asyncio.to_thread(
                self._client.get_balance_allowance, params
            )
            balance = float(result.get("balance", 0))
            return balance
        except Exception:
            logger.exception("Failed to fetch USDC balance")
            return None
