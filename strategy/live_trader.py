"""Live trading engine for placing real orders on Polymarket via py-clob-client.

All CLOB API calls are synchronous (py-clob-client is not async), so every
blocking call is wrapped in ``asyncio.to_thread()`` to avoid stalling the
event loop.

Safety features:
- Hard per-trade cap (MAX_LIVE_BET_USDC from config)
- Minimum $0.10 floor
- Balance check before each order
- Pause support (shared with paper trader via orchestrator)
- Graceful degradation if py-clob-client is not installed
"""

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Graceful import — if py-clob-client is not installed, the module still
# loads but ``CLOB_AVAILABLE`` is False and LiveTrader.initialize() will
# refuse to activate.
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    CLOB_AVAILABLE = True
except ImportError:
    ClobClient = None  # type: ignore[assignment,misc]
    MarketOrderArgs = None  # type: ignore[assignment,misc]
    OrderType = None  # type: ignore[assignment,misc]
    BUY = None  # type: ignore[assignment]
    SELL = None  # type: ignore[assignment]
    CLOB_AVAILABLE = False
    logger.warning(
        "py-clob-client not installed — live trading disabled. "
        "Install with: pip install py-clob-client"
    )

# Polymarket CLOB host and Polygon chain ID
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


class LiveTrader:
    """Places real GTC market buy orders on Polymarket via py-clob-client.

    Parameters
    ----------
    private_key : str
        Hex-encoded EOA private key (no 0x prefix).
    funder_address : str
        Proxy wallet address (from polymarket.com account settings).
    max_bet_usdc : float
        Hard safety cap per trade in USDC.
    """

    # Hour-based sizing multipliers (UTC) — same as PaperTrader.
    HOUR_MULTIPLIERS = {
        4: 0.7, 7: 0.7,
        6: 1.2, 8: 1.2, 10: 1.2, 12: 1.2,
        16: 1.2, 18: 1.2, 22: 1.2,
        9: 1.5, 14: 1.5, 20: 1.5,
    }

    def __init__(
        self,
        private_key: str,
        funder_address: str,
        max_bet_usdc: float = 2.0,
        fee_rate: float = 0.0,
        fee_exponent: int = 2,
        sizing_strategy: str = "adaptive",
    ) -> None:
        self._private_key = private_key
        self._funder_address = funder_address
        self._max_bet = max_bet_usdc
        self._fee_rate = fee_rate
        self._fee_exponent = fee_exponent
        self._sizing_strategy = sizing_strategy
        self._client: Optional[object] = None
        self._active = False
        self._paused = False
        # In-memory trade log for session analysis
        self._session_trades: list[dict] = []

        # Bankroll tracking (restored from DB on startup)
        self.bankroll: float = 0.0
        self.initial_bankroll: float = 0.0
        self._max_bankroll: float = 0.0
        self._consecutive_losses: int = 0
        self._recent_outcomes: list[bool] = []

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self) -> bool:
        """Derive API credentials and verify connectivity.

        Returns True if the client is ready for trading, False otherwise.
        """
        if not CLOB_AVAILABLE:
            logger.error("Cannot initialize LiveTrader — py-clob-client not installed")
            return False

        if not self._private_key or not self._funder_address:
            logger.error("Cannot initialize LiveTrader — missing private key or funder address")
            return False

        try:
            # Route CLOB API calls through SOCKS5 proxy if available
            # (used to bypass geoblock from datacenter IPs)
            # Monkey-patches py-clob-client's internal httpx client rather than
            # setting global HTTP_PROXY (which would break Binance/Polymarket data streams).
            from config import settings as _settings
            proxy_url = _settings.clob_proxy
            if proxy_url:
                try:
                    import httpx
                    import py_clob_client.http_helpers.helpers as clob_helpers
                    clob_helpers._http_client = httpx.Client(
                        proxy=proxy_url, http2=True
                    )
                    logger.info("CLOB API proxy configured: %s", proxy_url)
                except Exception as exc:
                    logger.warning("Failed to configure CLOB proxy: %s", exc)

            # Fix py-clob-client rounding bug in get_market_order_amounts:
            # For BUY+FOK, the CLOB server requires:
            #   maker (USDC) = max 2 decimals
            #   taker (tokens) = max 4 decimals
            # The library produces too many decimals on both.
            # Fix: round maker (USDC) to 2 dec, derive taker via Decimal, cap at 4 dec.
            try:
                from py_clob_client.order_builder.builder import OrderBuilder
                from py_clob_client.order_builder.helpers import (
                    round_down, round_normal, round_up, decimal_places,
                    to_token_decimals,
                )
                from py_clob_client.order_builder.constants import BUY as _BUY
                from py_order_utils.model import BUY as UtilsBuy, SELL as UtilsSell

                _orig = OrderBuilder.get_market_order_amounts

                from decimal import Decimal, ROUND_DOWN as _RD

                def _patched(self, side, amount, price, round_config):
                    if side != _BUY:
                        return _orig(self, side, amount, price, round_config)
                    # BUY+FOK: maker=USDC (max 2 dec), taker=tokens (max 4 dec)
                    raw_price = round_normal(price, round_config.price)
                    raw_maker = round_down(amount, 2)  # USDC, 2 dec
                    d_maker = Decimal(str(raw_maker))
                    d_price = Decimal(str(raw_price))
                    d_taker = d_maker / d_price
                    if d_taker.as_tuple().exponent < -4:
                        d_taker = d_taker.quantize(Decimal("0.0001"), rounding=_RD)
                    raw_taker = float(d_taker)
                    return (
                        UtilsBuy,
                        to_token_decimals(raw_maker),
                        to_token_decimals(raw_taker),
                    )

                OrderBuilder.get_market_order_amounts = _patched
                logger.info("Patched CLOB OrderBuilder.get_market_order_amounts for BUY")
            except Exception as exc:
                logger.warning("Failed to patch OrderBuilder: %s", exc)

            client = ClobClient(
                CLOB_HOST,
                key=self._private_key,
                chain_id=CHAIN_ID,
                signature_type=2,  # Browser wallet proxy (Rabby)
                funder=self._funder_address,
            )
            # Derive API credentials (blocking call)
            creds = await asyncio.to_thread(client.create_or_derive_api_creds)
            client.set_api_creds(creds)

            self._client = client
            self._active = True
            logger.info(
                "LiveTrader initialized — max bet $%.2f, funder %s...%s",
                self._max_bet,
                self._funder_address[:8],
                self._funder_address[-6:],
            )
            return True
        except Exception:
            logger.exception("Failed to initialize LiveTrader")
            return False

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> Optional[float]:
        """Fetch USDC balance from Polymarket.

        Returns balance in USDC, or None on failure.
        """
        if not self._active or self._client is None:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = await asyncio.to_thread(
                self._client.get_balance_allowance, params
            )
            if isinstance(result, dict):
                bal = result.get("balance", "0")
                return float(bal) / 1e6  # USDC.e has 6 decimals
            return float(result) / 1e6
        except Exception:
            logger.exception("Failed to fetch balance")
            return None

    # ------------------------------------------------------------------
    # Bankroll & sizing
    # ------------------------------------------------------------------

    def restore_bankroll(self, initial: float, historical_pnl: float) -> None:
        """Set bankroll from initial capital + cumulative live P&L."""
        self.initial_bankroll = initial
        self.bankroll = initial + historical_pnl
        self._max_bankroll = self.bankroll
        logger.info(
            "Live bankroll restored: initial=$%.2f + pnl=$%+.2f = $%.2f",
            initial, historical_pnl, self.bankroll,
        )

    def adaptive_size(self, confidence: float) -> float:
        """Hybrid adaptive sizing based on live bankroll and live outcomes."""
        base_pct = 0.02

        conf_mult = 0.5 + (confidence / 0.45) * 1.5
        conf_mult = max(0.5, min(conf_mult, 2.0))

        utc_hour = datetime.now(timezone.utc).hour
        hour_mult = self.HOUR_MULTIPLIERS.get(utc_hour, 1.0)

        streak_mult = 1.0
        if self._consecutive_losses >= 5:
            streak_mult = 0.5
        elif self._consecutive_losses >= 3:
            streak_mult = 0.75

        dd_mult = 1.0
        if self._max_bankroll > 0:
            drawdown = (self._max_bankroll - self.bankroll) / self._max_bankroll
            if drawdown > 0.25:
                dd_mult = 0.5
            elif drawdown > 0.15:
                dd_mult = 0.75

        wr_mult = 1.0
        if len(self._recent_outcomes) >= 10:
            recent_wr = sum(self._recent_outcomes[-20:]) / len(self._recent_outcomes[-20:])
            if recent_wr > 0.55:
                wr_mult = 1.3
            elif recent_wr < 0.45:
                wr_mult = 0.7

        final_pct = base_pct * conf_mult * hour_mult * streak_mult * dd_mult * wr_mult
        final_pct = max(0.005, min(final_pct, 0.08))

        size = final_pct * self.bankroll

        logger.info(
            "Live adaptive size: base=2%% x conf=%.2f x hour=%.2f x streak=%.2f x dd=%.2f x wr=%.2f "
            "= %.1f%% of $%.0f -> $%.2f",
            conf_mult, hour_mult, streak_mult, dd_mult, wr_mult,
            final_pct * 100, self.bankroll, size,
        )
        return size

    def compute_bet_size(self, confidence: float = 0.0) -> float:
        """Return live bet size in USDC, capped by bankroll percentage and absolute max.

        The effective cap scales with bankroll: 8% of bankroll or the
        configured ``max_bet_usdc`` (from .env), whichever is smaller.
        This ensures bets grow proportionally as the bankroll grows while
        still respecting the hard safety ceiling.
        """
        if self.bankroll <= 0:
            return 0.0
        if self._sizing_strategy == "adaptive":
            size = self.adaptive_size(confidence)
        elif self._sizing_strategy == "fixed":
            size = settings.bet_size_usdc
        else:
            size = 0.02 * self.bankroll  # fallback: flat 2%
        size = max(1.00, size)
        # Cap: 8% of bankroll or absolute max, whichever is smaller
        effective_cap = min(0.08 * self.bankroll, self._max_bet)
        pre_cap = size
        size = min(size, self.bankroll, effective_cap)
        logger.info(
            "Live bet size: $%.2f (pre-cap $%.2f, cap $%.2f = min(8%%x$%.0f, $%.0f), bankroll $%.0f)",
            size, pre_cap, effective_cap, self.bankroll, self._max_bet, self.bankroll,
        )
        return size

    def record_settlement(self, won: bool, pnl: float) -> None:
        """Update internal bankroll/streak state after a live trade settles."""
        self.bankroll += pnl
        self._recent_outcomes.append(won)
        if len(self._recent_outcomes) > 50:
            self._recent_outcomes = self._recent_outcomes[-50:]
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
        if self.bankroll > self._max_bankroll:
            self._max_bankroll = self.bankroll

    # ------------------------------------------------------------------
    # Retry helper for 425 "Too Early" (matching engine restarting)
    # ------------------------------------------------------------------

    async def _post_order_with_retry(
        self, signed_order, order_type, max_retries: int = 3, initial_delay: float = 3.0
    ):
        """Post order with exponential backoff retry on 425 errors."""
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                return await asyncio.to_thread(
                    self._client.post_order, signed_order, order_type
                )
            except Exception as exc:
                last_exc = exc
                if "425" in str(exc) and attempt < max_retries:
                    delay = initial_delay * (2 ** attempt)
                    logger.info(
                        "425 Too Early — retrying in %.0fs (attempt %d/%d)",
                        delay, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        amount_usdc: float,
        side: str,
        market_slug: str = "",
        entry_price: float = 0.0,
        exploration: bool = False,
    ) -> dict:
        """Place a FOK market buy order on Polymarket.

        Parameters
        ----------
        token_id : str
            CLOB token ID for the outcome (UP or DOWN token).
        amount_usdc : float
            Trade size in USDC (will be capped by max_bet, floored at $1).
        side : str
            "UP" or "DOWN" — for logging only; the token_id determines
            which outcome we buy.
        market_slug : str
            Market slug for logging and DB records.
        entry_price : float
            Token price from orderbook. Used to compute minimum USDC
            needed to meet the CLOB 5-token minimum order size.
        exploration : bool
            If True, use a lower USDC floor ($2.00 instead of $3.50) since
            exploration trades are at low entry prices (0.25-0.35) where
            even $2 buys 5+ tokens.

        Returns
        -------
        dict with keys: success (bool), order_id (str|None), error (str),
        amount (float), response (dict|None).
        """
        result = {
            "success": False,
            "order_id": None,
            "error": "",
            "amount": 0.0,
            "response": None,
        }

        if not self._active or self._client is None:
            result["error"] = "LiveTrader not active"
            return result

        if self._paused:
            result["error"] = "Trading is paused"
            return result

        # CLOB requires minimum 5 tokens per order.
        # MarketOrderArgs fills at the current best ask, which can shift
        # significantly from our entry_price signal in volatile 5-min markets.
        # Normal trades: $3.50 floor guarantees 5 tokens up to $0.70 fill.
        # Exploration trades (entry 0.25-0.35): $2.00 floor is sufficient
        # since 5 × 0.40 = $2.00, giving margin even if fill price shifts.
        price = entry_price if entry_price > 0 else 0.50
        usdc_floor = 2.00 if exploration else 3.50
        min_usdc = max(round(5.5 * price, 2), usdc_floor)
        amount_usdc = round(max(amount_usdc, min_usdc), 2)

        # Ensure amount is at least $1.00 after rounding (CLOB minimum for
        # marketable orders). The rounding patch can reduce the effective
        # maker amount below $1 if we're right at the boundary.
        if amount_usdc < 1.00:
            amount_usdc = 1.00

        # Safety: enforce hard cap — skip trade if minimum exceeds cap
        if amount_usdc > self._max_bet:
            result["error"] = (
                f"${amount_usdc:.2f} needed (5-token min at ${price:.2f}) "
                f"exceeds cap ${self._max_bet:.2f}"
            )
            logger.info("Skipping live trade — %s", result["error"])
            return result
        result["amount"] = amount_usdc

        # Balance check
        balance = await self.get_balance()
        if balance is not None and balance < amount_usdc:
            result["error"] = f"Insufficient balance: ${balance:.2f} < ${amount_usdc:.2f}"
            logger.warning("Skipping live trade — %s", result["error"])
            return result

        logger.info(
            "Placing LIVE %s market order: %s $%.2f on %s",
            side, token_id[:16], amount_usdc, market_slug,
        )

        try:
            # Use MarketOrderArgs — auto-fills against best available asks.
            # Unlike OrderArgs/create_order, this has no 5-token minimum.
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usdc,
                side=BUY,
            )
            signed_order = await asyncio.to_thread(
                self._client.create_market_order, order_args
            )
            resp = await self._post_order_with_retry(signed_order, OrderType.FOK)

            # Parse response
            if isinstance(resp, dict):
                result["response"] = resp
                order_id = resp.get("orderID", resp.get("id", ""))
                result["order_id"] = order_id
                # Check for success indicators
                status = resp.get("status", resp.get("success", ""))
                if status in ("matched", "live", True, "MATCHED"):
                    result["success"] = True
                elif order_id:
                    # Has an order ID — likely succeeded
                    result["success"] = True
                else:
                    result["error"] = f"Unexpected response status: {status}"
            else:
                # Non-dict response — treat as success if no exception
                result["response"] = {"raw": str(resp)}
                result["success"] = True

            # Verify actual fill — CLOB can accept an order (return orderID)
            # but then immediately cancel it with 0 tokens matched.
            # Without this check we record phantom trades with fake P&L.
            if result["success"] and result["order_id"]:
                try:
                    await asyncio.sleep(1.5)  # brief delay for CLOB to settle
                    order_status = await asyncio.to_thread(
                        self._client.get_order, result["order_id"]
                    )
                    if isinstance(order_status, dict):
                        matched = float(order_status.get("size_matched", 0) or 0)
                        clob_status = order_status.get("status", "")
                        if matched == 0 or clob_status == "CANCELED":
                            result["success"] = False
                            result["error"] = (
                                f"Order {clob_status} with 0 tokens matched "
                                f"(original_size={order_status.get('original_size', '?')})"
                            )
                            result["fok_rejected"] = True
                            logger.warning(
                                "FOK REJECTED: %s %s $%.2f order=%s — %s",
                                side, market_slug, amount_usdc,
                                result["order_id"], result["error"],
                            )
                            # FOK orders don't leave remainders — no cancel needed.
                        else:
                            logger.info(
                                "Order verified: %s matched=%.2f status=%s",
                                result["order_id"][:20], matched, clob_status,
                            )
                except Exception as exc:
                    # Verification failed — keep the trade but log a warning.
                    # Better to have a potential phantom than to discard a real fill.
                    logger.warning(
                        "Could not verify order %s: %s — keeping as filled",
                        result["order_id"][:20] if result["order_id"] else "?", exc,
                    )

            if result["success"]:
                logger.info(
                    "LIVE ORDER FILLED: %s %s $%.2f order=%s",
                    side, market_slug, amount_usdc, result["order_id"],
                )
            else:
                logger.warning(
                    "LIVE ORDER ISSUE: %s %s $%.2f — %s",
                    side, market_slug, amount_usdc, result["error"],
                )

        except Exception as exc:
            result["error"] = str(exc)
            # Detect FOK rejection (book too thin to fill entire order)
            if "fully filled" in str(exc) or "FOK" in str(exc):
                result["fok_rejected"] = True
                logger.warning(
                    "FOK REJECTED: %s %s $%.2f — insufficient liquidity",
                    side, market_slug, amount_usdc,
                )
            else:
                logger.exception(
                    "LIVE ORDER FAILED: %s %s $%.2f — %s",
                    side, market_slug, amount_usdc, exc,
                )

        # Record in session log
        self._session_trades.append({
            "timestamp": int(time.time()),
            "side": side,
            "slug": market_slug,
            "token_id": token_id,
            "amount": amount_usdc,
            "success": result["success"],
            "order_id": result["order_id"],
            "error": result["error"],
        })

        return result

    # ------------------------------------------------------------------
    # Cancel open orders — prevent stale GTC orders from becoming maker fills
    # ------------------------------------------------------------------

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders on the CLOB.

        Called after each trade to prevent unfilled GTC order remainders
        from sitting on the book and getting filled later as unmanaged
        maker positions (no confidence check, no regime filter, no streak
        guard).

        Returns the number of orders cancelled, or -1 on error.
        """
        if not self._active or self._client is None:
            return -1
        try:
            resp = await asyncio.to_thread(self._client.cancel_all)
            # Response is typically {"canceled_orders": [...]}
            cancelled = []
            if isinstance(resp, dict):
                cancelled = resp.get("canceled_orders", [])
            count = len(cancelled) if isinstance(cancelled, list) else 0
            if count > 0:
                logger.info("Cancelled %d open order(s) to prevent stale maker fills", count)
            return count
        except Exception:
            logger.warning("Failed to cancel open orders", exc_info=True)
            return -1

    # ------------------------------------------------------------------
    # Early exit — sell tokens before window settles
    # ------------------------------------------------------------------

    @staticmethod
    def get_exit_threshold(entry_price: float, trade_tag: str | None = None) -> float:
        """Return the early exit bid threshold based on entry price tier.

        Cheap entries have low WR and benefit from aggressive exits.
        Expensive entries have decent WR and should stay conservative.
        Thresholds validated on 548 trades with 102K market snapshots.

        Regime-flip trades in the 0.40-0.50 tier use a relaxed threshold
        (0.90) because the model is correctly riding trends — EE at 0.65
        caps winners at ~$2 while settlement pays ~$5.
        """
        from config import settings
        if entry_price < 0.35:
            return settings.early_exit_threshold_low
        if entry_price < 0.40:
            return settings.early_exit_threshold_low_mid
        if entry_price < 0.50:
            return settings.early_exit_threshold_mid
        return settings.early_exit_threshold_high

    async def get_token_balance(self, token_id: str) -> float | None:
        """Query actual conditional token balance from CLOB.

        Returns the number of tokens we hold, or None on failure.
        """
        if not self._active or self._client is None:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id,
            )
            result = await asyncio.to_thread(
                self._client.get_balance_allowance, params
            )
            if isinstance(result, dict):
                bal = result.get("balance", "0")
                return float(bal) / 1e6  # conditional tokens use 6 decimals
            return float(result) / 1e6
        except Exception:
            logger.exception("Failed to fetch token balance for %s", token_id[:16])
            return None

    async def sell_early_exit(
        self,
        token_id: str,
        tokens: float,
        best_bid: float,
        market_slug: str = "",
    ) -> dict:
        """Sell tokens early when bid is high enough to lock in profit.

        Unlike ``sell_winning_tokens`` (post-resolution), this sells while the
        market is still live.  The CLOB order book is active, so standard
        retry timing is used.

        Parameters
        ----------
        tokens : float
            Token count from the buy (computed at buy time — may overestimate
            if CLOB fill price differed from signal entry price).
        best_bid : float
            Current best bid price on the order book.
        """
        result: dict = {"success": False, "error": "", "order_id": None, "sell_amount": 0.0}

        if not self._active or self._client is None:
            result["error"] = "LiveTrader not active"
            return result

        # Query actual token balance — our computed token count may be wrong
        # if the CLOB filled at a different price than the signal entry price.
        actual_balance = await self.get_token_balance(token_id)
        if actual_balance is not None:
            if actual_balance < 5:
                result["error"] = f"Actual token balance {actual_balance:.2f} below CLOB 5-token minimum"
                logger.warning("Skipping early exit — %s", result["error"])
                return result
            if actual_balance < tokens:
                logger.info(
                    "Token balance correction: computed=%.2f actual=%.2f (diff=%.2f)",
                    tokens, actual_balance, tokens - actual_balance,
                )
            # Use actual balance (floor to 2 decimal places for safety)
            sell_tokens = math.floor(actual_balance * 100) / 100
        else:
            # Fallback: use computed tokens with a safety margin
            sell_tokens = round(tokens * 0.95, 2)
            logger.warning(
                "Could not query token balance — using 95%% of computed: %.2f",
                sell_tokens,
            )

        if sell_tokens < 5:
            result["error"] = f"Token count {sell_tokens:.2f} below CLOB 5-token minimum"
            logger.warning("Skipping early exit — %s", result["error"])
            return result

        # For SELL MarketOrderArgs, amount = TOKEN COUNT (not USDC).
        # Pass our full token count so the CLOB sells the entire position.
        # Expected USDC proceeds = tokens × best_bid (for PnL tracking).
        expected_usdc = round(sell_tokens * best_bid, 2)
        result["sell_amount"] = expected_usdc

        logger.info(
            "Placing EARLY EXIT SELL: %s %.1f tokens (actual) @ bid $%.3f = ~$%.2f on %s",
            token_id[:16], sell_tokens, best_bid, expected_usdc, market_slug,
        )

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(sell_tokens, 2),  # token count, not USDC
                side=SELL,
            )
            signed_order = await asyncio.to_thread(
                self._client.create_market_order, order_args
            )
            resp = await self._post_order_with_retry(signed_order, OrderType.FOK)

            if isinstance(resp, dict):
                result["order_id"] = resp.get("orderID", resp.get("id", ""))
                result["success"] = True
                result["response"] = resp
            else:
                result["success"] = True

            # Verify the sell actually matched
            if result["success"] and result["order_id"]:
                try:
                    await asyncio.sleep(1.5)
                    order_status = await asyncio.to_thread(
                        self._client.get_order, result["order_id"]
                    )
                    if isinstance(order_status, dict):
                        matched = float(order_status.get("size_matched", 0) or 0)
                        clob_status = order_status.get("status", "")
                        if matched == 0 or clob_status == "CANCELED":
                            result["success"] = False
                            result["error"] = (
                                f"Sell {clob_status} with 0 matched — tokens still held"
                            )
                            logger.warning(
                                "PHANTOM EARLY EXIT: %s order=%s — %s",
                                market_slug, result["order_id"][:20], result["error"],
                            )
                except Exception as exc:
                    logger.warning(
                        "Could not verify early exit sell %s: %s — keeping as filled",
                        result["order_id"][:20] if result["order_id"] else "?", exc,
                    )

            if result["success"]:
                logger.info(
                    "EARLY EXIT FILLED: %s $%.2f order=%s on %s",
                    token_id[:16], expected_usdc, result["order_id"], market_slug,
                )
            else:
                logger.info(
                    "EARLY EXIT NOT FILLED: %s order=%s — will settle normally",
                    market_slug, result["order_id"],
                )

        except Exception as exc:
            result["error"] = str(exc)
            logger.warning(
                "EARLY EXIT FAILED for %s: %s — will settle normally",
                market_slug, exc,
            )

        return result

    # ------------------------------------------------------------------
    # Auto-sell winning tokens (reclaim USDC after market resolution)
    # ------------------------------------------------------------------

    async def sell_winning_tokens(
        self,
        token_id: str,
        market_slug: str = "",
        buy_amount_usdc: float = 0.0,
        buy_price: float = 0.0,
    ) -> dict:
        """Sell winning tokens to reclaim USDC after market resolution.

        Attempts to sell via CLOB with retries (matching engine may need time
        to come back after resolution). If all retries fail, the user needs
        to claim manually on the Polymarket website.

        Parameters
        ----------
        buy_amount_usdc : float
            Original USDC amount spent on the buy (used to estimate token count).
        buy_price : float
            Entry price per token (used to estimate token count).
        """
        result = {"success": False, "error": "", "order_id": None}

        if not self._active or self._client is None:
            result["error"] = "LiveTrader not active"
            return result

        # Estimate token count from original buy: tokens ≈ amount / price
        # Sell amount in USDC ≈ tokens × $0.99 (winning token price)
        if buy_amount_usdc > 0 and buy_price > 0:
            est_tokens = buy_amount_usdc / buy_price
            sell_amount = round(est_tokens * 0.99, 2)
        else:
            sell_amount = 5.0  # conservative default (5 tokens × $0.99)

        sell_amount = max(sell_amount, 1.00)  # CLOB minimum

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=sell_amount,
                side=SELL,
            )
            signed_order = await asyncio.to_thread(
                self._client.create_market_order, order_args
            )
            # Use retry with longer delays for post-resolution sells
            resp = await self._post_order_with_retry(
                signed_order, OrderType.GTC,
                max_retries=4, initial_delay=5.0,
            )

            if isinstance(resp, dict):
                result["order_id"] = resp.get("orderID", resp.get("id", ""))
                result["success"] = True
            else:
                result["success"] = True

            logger.info(
                "AUTO-SELL placed: %s $%.2f order=%s (reclaiming USDC from %s)",
                token_id[:16], sell_amount, result["order_id"], market_slug,
            )

        except Exception as exc:
            result["error"] = str(exc)
            logger.warning(
                "AUTO-SELL failed for %s: %s — claim manually on polymarket.com",
                market_slug, exc,
            )

        return result

    # ------------------------------------------------------------------
    # Session summary
    # ------------------------------------------------------------------

    def get_session_summary(self) -> dict:
        """Return a summary of trades placed this session."""
        total = len(self._session_trades)
        successful = sum(1 for t in self._session_trades if t["success"])
        total_amount = sum(t["amount"] for t in self._session_trades if t["success"])
        return {
            "total": total,
            "successful": successful,
            "failed": total - successful,
            "total_amount": total_amount,
        }

    def format_session_report(self) -> str:
        """Return a human-readable session report."""
        s = self.get_session_summary()
        return (
            f"Live Trading Session:\n"
            f"  Orders: {s['total']} ({s['successful']} filled, {s['failed']} failed)\n"
            f"  Total amount: ${s['total_amount']:.2f}"
        )

    # ------------------------------------------------------------------
    # CLOB trade sync — discover maker fills not tracked in DB
    # ------------------------------------------------------------------

    async def fetch_clob_trades(self) -> list[dict]:
        """Fetch all trades from the CLOB API for this wallet.

        Returns a list of trade dicts with fields: id, market, asset_id,
        side, size, price, trader_side, match_time, outcome, etc.
        """
        if not self._active or self._client is None:
            return []
        try:
            from py_clob_client.clob_types import TradeParams
            trades = await asyncio.to_thread(
                self._client.get_trades, TradeParams()
            )
            return trades if isinstance(trades, list) else []
        except Exception:
            logger.exception("Failed to fetch CLOB trades")
            return []

    async def get_market_slug(self, condition_id: str) -> str | None:
        """Resolve a CLOB condition_id to a market slug."""
        if not self._active or self._client is None:
            return None
        try:
            market = await asyncio.to_thread(
                self._client.get_market, condition_id
            )
            if isinstance(market, dict):
                return market.get("market_slug")
            return None
        except Exception:
            logger.debug("Failed to resolve condition_id %s", condition_id[:16])
            return None
