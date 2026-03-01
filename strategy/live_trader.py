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
            # For BUY, the library rounds taker to `amount` (4-5) decimals but
            # the CLOB server requires max 2 on taker and max 4 on maker.
            # Also, maker must equal taker × price (consistency check).
            # The fix: compute taker first (rounded to 2), then derive maker.
            try:
                from py_clob_client.order_builder.builder import OrderBuilder
                from py_clob_client.order_builder.helpers import (
                    round_down, round_normal, round_up, decimal_places,
                    to_token_decimals,
                )
                from py_clob_client.order_builder.constants import BUY as _BUY
                from py_order_utils.model import BUY as UtilsBuy, SELL as UtilsSell

                _orig = OrderBuilder.get_market_order_amounts

                def _patched(self, side, amount, price, round_config):
                    if side != _BUY:
                        return _orig(self, side, amount, price, round_config)
                    # BUY: taker=tokens (max 2 dec), maker=USDC (max 4 dec)
                    raw_price = round_normal(price, round_config.price)
                    raw_taker = round_down(amount / raw_price, 2)
                    raw_maker = raw_taker * raw_price
                    if decimal_places(raw_maker) > 4:
                        raw_maker = round_down(raw_maker, 4)
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

        logger.debug(
            "Live adaptive size: base=2%% x conf=%.2f x hour=%.2f x streak=%.2f x dd=%.2f x wr=%.2f "
            "= %.1f%% -> $%.2f",
            conf_mult, hour_mult, streak_mult, dd_mult, wr_mult,
            final_pct * 100, size,
        )
        return size

    def compute_bet_size(self, confidence: float = 0.0) -> float:
        """Return live bet size in USDC, capped by bankroll and max bet."""
        if self.bankroll <= 0:
            return 0.0
        if self._sizing_strategy == "adaptive":
            size = self.adaptive_size(confidence)
        else:
            size = 0.02 * self.bankroll  # fallback: flat 2%
        size = max(1.00, size)
        size = min(size, self.bankroll, self._max_bet)
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
    ) -> dict:
        """Place a GTC market buy order on Polymarket.

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
        # Worst case: entry price filter allows up to 0.65, so we need at
        # least 5 × 0.65 = $3.25.  Use $3.50 as a hard floor to guarantee
        # 5 tokens with margin at any price in our 0.35-0.65 range.
        price = entry_price if entry_price > 0 else 0.50
        min_usdc = max(round(5.5 * price, 2), 3.50)
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
            resp = await self._post_order_with_retry(signed_order, OrderType.GTC)

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
    # Early exit — sell tokens before window settles
    # ------------------------------------------------------------------

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
            Exact token count from the buy (computed at buy time with fees).
        best_bid : float
            Current best bid price on the order book.
        """
        result: dict = {"success": False, "error": "", "order_id": None, "sell_amount": 0.0}

        if not self._active or self._client is None:
            result["error"] = "LiveTrader not active"
            return result

        # CLOB 5-token minimum applies to sells too
        if tokens < 5:
            result["error"] = f"Token count {tokens:.2f} below CLOB 5-token minimum"
            logger.warning("Skipping early exit — %s", result["error"])
            return result

        # For SELL MarketOrderArgs, amount = TOKEN COUNT (not USDC).
        # Pass our full token count so the CLOB sells the entire position.
        # Expected USDC proceeds = tokens × best_bid (for PnL tracking).
        expected_usdc = round(tokens * best_bid, 2)
        result["sell_amount"] = expected_usdc

        logger.info(
            "Placing EARLY EXIT SELL: %s %.1f tokens @ bid $%.3f = ~$%.2f on %s",
            token_id[:16], tokens, best_bid, expected_usdc, market_slug,
        )

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(tokens, 2),  # token count, not USDC
                side=SELL,
            )
            signed_order = await asyncio.to_thread(
                self._client.create_market_order, order_args
            )
            resp = await self._post_order_with_retry(signed_order, OrderType.GTC)

            if isinstance(resp, dict):
                result["order_id"] = resp.get("orderID", resp.get("id", ""))
                result["success"] = True
            else:
                result["success"] = True

            logger.info(
                "EARLY EXIT FILLED: %s $%.2f order=%s on %s",
                token_id[:16], sell_amount, result["order_id"], market_slug,
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
