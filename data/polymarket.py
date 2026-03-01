"""Async Polymarket client for discovering and monitoring BTC 5-minute prediction markets.

Uses the Gamma API for market discovery and the CLOB API for live price feeds.
Markets follow a deterministic slug pattern: btc-updown-5m-{window_ts} where
window_ts is the current unix timestamp rounded down to the nearest 5-minute boundary.

Settlement prices come from the Chainlink BTC/USD data stream via Polymarket's
RTDS (Real-Time Data Streaming) WebSocket — the same source Polymarket uses to
resolve these markets.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import aiohttp

from data.models import PolymarketMarket, PolymarketOrderBook

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0

# Monitoring loop interval
POLL_INTERVAL_SECONDS = 3.0

# 5-minute window in seconds
WINDOW_SECONDS = 300

# Chainlink BTC/USD Price Feed on Ethereum Mainnet
# This is the resolution source for Polymarket BTC Up/Down markets.
# Contract: Aggregator Proxy — returns price with 8 decimals.
CHAINLINK_BTC_USD_ADDR = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
CHAINLINK_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData() selector
CHAINLINK_DECIMALS = 8

# Public Ethereum RPC endpoints (fallback order)
ETH_RPC_URLS = [
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://ethereum-rpc.publicnode.com",
]

# Polymarket RTDS WebSocket — streams Chainlink BTC/USD in real-time.
# This is the actual resolution source for Polymarket BTC Up/Down markets.
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
RTDS_PING_INTERVAL = 5.0  # seconds
CHAINLINK_STREAM_STALE_SECONDS = 60.0  # consider price stale after this


def compute_fee_factor(price: float, fee_rate: float = 0.25, fee_exponent: int = 2) -> float:
    """Compute the Polymarket taker fee factor for a given share price.

    Uses the fee curve from the Maker Rebates Program documentation:
        fee_factor = fee_rate * (price * (1 - price)) ^ fee_exponent

    The fee_factor represents the fraction of trade value taken as fee.
    Multiply by size_usdc to get the actual fee in USDC.

    At price=0.50 with default crypto params: fee_factor ~ 0.015625 (1.56%).
    Near extremes (price close to 0 or 1): fee_factor approaches 0.

    Args:
        price: Share price between 0 and 1.
        fee_rate: Base fee rate parameter (0.25 for crypto markets).
        fee_exponent: Curve shaping exponent (2 for crypto markets).

    Returns:
        Fee factor as a float (multiply by USDC amount for fee).
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return fee_rate * (price * (1.0 - price)) ** fee_exponent


class PolymarketClient:
    """Async client for Polymarket BTC 5-minute prediction markets.

    Provides market discovery via the Gamma API and live price tracking
    via the CLOB API. Caches discovered markets by slug to minimize
    redundant network calls.
    """

    def __init__(self, gamma_url: str, clob_url: str) -> None:
        """Initialize the Polymarket client.

        Args:
            gamma_url: Base URL for the Gamma API (market discovery).
            clob_url: Base URL for the CLOB API (live prices and orderbook).
        """
        self._gamma_url = gamma_url.rstrip("/")
        self._clob_url = clob_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._current_market: Optional[PolymarketMarket] = None
        self._market_cache: dict[str, PolymarketMarket] = {}
        # Fee rate cache: token_id -> fee_rate_bps
        self._fee_rate_cache: dict[str, int] = {}
        # Chainlink RTDS stream state
        self._chainlink_stream_price: Optional[float] = None
        self._chainlink_stream_ts: float = 0.0

    async def start(self) -> None:
        """Create the aiohttp client session.

        Must be called before making any API requests. Typically called
        once during application startup.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            logger.info("Polymarket client session created")

    async def stop(self) -> None:
        """Close the aiohttp client session and release resources.

        Safe to call multiple times. Should be called during application
        shutdown.
        """
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("Polymarket client session closed")
        self._session = None

    # ------------------------------------------------------------------
    # Time / slug helpers
    # ------------------------------------------------------------------

    def get_current_slug(self) -> str:
        """Compute the deterministic slug for the current 5-minute window.

        The window timestamp is the current unix time floored to the nearest
        300-second boundary.

        Returns:
            Slug string of the form ``btc-updown-5m-{window_ts}``.
        """
        now = int(time.time())
        window_ts = now - (now % WINDOW_SECONDS)
        return f"btc-updown-5m-{window_ts}"

    def get_next_window_start(self) -> int:
        """Return the unix timestamp when the next 5-minute window begins.

        Returns:
            Integer unix timestamp of the next window boundary.
        """
        now = int(time.time())
        current_window_ts = now - (now % WINDOW_SECONDS)
        return current_window_ts + WINDOW_SECONDS

    def seconds_until_next_window(self) -> float:
        """Return the number of seconds remaining in the current 5-minute window.

        Returns:
            Seconds (as a float) until the current window ends and the next
            window begins.
        """
        return float(self.get_next_window_start() - time.time())

    # ------------------------------------------------------------------
    # Internal HTTP helper
    # ------------------------------------------------------------------

    async def _get_json(self, url: str) -> Optional[dict | list]:
        """Perform a GET request with retry logic.

        Retries up to ``MAX_RETRIES`` times with a linear backoff of
        ``RETRY_BACKOFF_SECONDS`` between attempts for transient errors
        (connection errors and HTTP 5xx responses).

        Args:
            url: Fully-qualified URL to fetch.

        Returns:
            Parsed JSON response, or None if all retries are exhausted.
        """
        if self._session is None or self._session.closed:
            logger.error("Session not started. Call start() before making requests.")
            return None

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status >= 500:
                        body = await resp.text()
                        logger.warning(
                            "Server error %s from %s (attempt %d/%d): %s",
                            resp.status,
                            url,
                            attempt,
                            MAX_RETRIES,
                            body[:200],
                        )
                        last_error = aiohttp.ClientResponseError(
                            resp.request_info,
                            resp.history,
                            status=resp.status,
                            message=body[:200],
                        )
                    else:
                        # Non-retryable client error (4xx)
                        body = await resp.text()
                        logger.error(
                            "Client error %s from %s: %s",
                            resp.status,
                            url,
                            body[:200],
                        )
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Request to %s failed (attempt %d/%d): %s",
                    url,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                last_error = exc

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        logger.error(
            "All %d retries exhausted for %s. Last error: %s",
            MAX_RETRIES,
            url,
            last_error,
        )
        return None

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def discover_market(
        self, slug: Optional[str] = None
    ) -> Optional[PolymarketMarket]:
        """Discover and return a PolymarketMarket for a given slug.

        If *slug* is None the current 5-minute window slug is used.
        Results are cached by slug so repeated calls for the same window
        do not trigger additional network requests.

        Args:
            slug: Market slug to look up. Defaults to the current window slug.

        Returns:
            A ``PolymarketMarket`` dataclass, or None if the market cannot
            be found or the API is unreachable.
        """
        if slug is None:
            slug = self.get_current_slug()

        # Check cache first
        if slug in self._market_cache:
            logger.debug("Cache hit for slug %s", slug)
            return self._market_cache[slug]

        url = f"{self._gamma_url}/events?slug={slug}"
        logger.info("Discovering market for slug %s", slug)

        data = await self._get_json(url)
        if data is None:
            logger.error("Failed to fetch event data for slug %s", slug)
            return None

        try:
            # Gamma API returns a list of events (or a single event depending
            # on the endpoint variant). Normalize to a list.
            events = data if isinstance(data, list) else [data]
            if not events:
                logger.warning("No events found for slug %s", slug)
                return None

            event = events[0]
            markets = event.get("markets", [])
            if not markets:
                logger.warning("Event for slug %s has no markets", slug)
                return None

            market_data = markets[0]

            # Parse token IDs, outcomes, and prices from the raw market data.
            # The clobTokenIds field may arrive as either a JSON list or a
            # JSON-encoded string representation of a list.
            token_ids = market_data["clobTokenIds"]
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)

            outcomes = market_data["outcomes"]
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            prices = market_data["outcomePrices"]
            if isinstance(prices, str):
                prices = json.loads(prices)

            # Determine which index is Up and which is Down
            up_idx = outcomes.index("Up")
            down_idx = outcomes.index("Down")

            # Extract the numeric window timestamp from the slug
            # Slug format: btc-updown-5m-{window_ts}
            slug_parts = slug.rsplit("-", 1)
            window_ts = int(slug_parts[-1])

            market = PolymarketMarket(
                slug=slug,
                question=market_data.get("question", ""),
                condition_id=market_data["conditionId"],
                up_token_id=token_ids[up_idx],
                down_token_id=token_ids[down_idx],
                up_price=float(prices[up_idx]),
                down_price=float(prices[down_idx]),
                window_start=window_ts,
                window_end=window_ts + WINDOW_SECONDS,
            )

            self._market_cache[slug] = market
            self._current_market = market

            logger.info(
                "Discovered market: %s | Up=%.4f Down=%.4f | window %d-%d",
                slug,
                market.up_price,
                market.down_price,
                market.window_start,
                market.window_end,
            )
            return market

        except (KeyError, IndexError, ValueError, TypeError) as exc:
            logger.error(
                "Failed to parse market data for slug %s: %s",
                slug,
                exc,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Live prices
    # ------------------------------------------------------------------

    async def get_live_prices(
        self, market: Optional[PolymarketMarket] = None
    ) -> Optional[tuple[float, float]]:
        """Fetch live midpoint prices for Up and Down tokens.

        Args:
            market: The market to query. Falls back to ``_current_market``
                if None.

        Returns:
            A ``(up_price, down_price)`` tuple, or None if prices cannot be
            retrieved.
        """
        if market is None:
            market = self._current_market
        if market is None:
            logger.error("No market available for live price fetch")
            return None

        up_url = f"{self._clob_url}/midpoint?token_id={market.up_token_id}"
        down_url = f"{self._clob_url}/midpoint?token_id={market.down_token_id}"

        up_data, down_data = await asyncio.gather(
            self._get_json(up_url),
            self._get_json(down_url),
        )

        if up_data is None or down_data is None:
            logger.warning("Failed to fetch midpoint prices for %s", market.slug)
            return None

        try:
            up_price = float(up_data.get("mid", up_data.get("price", 0)))
            down_price = float(down_data.get("mid", down_data.get("price", 0)))
            return (up_price, down_price)
        except (ValueError, TypeError, AttributeError) as exc:
            logger.error("Failed to parse midpoint prices: %s", exc)
            return None

    @staticmethod
    def _parse_orderbook(data: Optional[dict]) -> Optional[PolymarketOrderBook]:
        """Parse a CLOB /book response into a PolymarketOrderBook.

        Returns None if the book is empty or unparseable.
        """
        if data is None:
            return None
        try:
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids or not asks:
                return None
            # CLOB returns bids ascending (lowest first) and asks
            # descending (highest first).
            # Best bid = highest = bids[-1]. Best ask = lowest = asks[-1].
            best_bid = float(bids[-1]["price"])
            best_ask = float(asks[-1]["price"])
            bid_size = float(bids[-1]["size"])
            ask_size = float(asks[-1]["size"])
            spread = best_ask - best_bid
            midpoint = (best_bid + best_ask) / 2.0
            return PolymarketOrderBook(
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                bid_size=bid_size,
                ask_size=ask_size,
                midpoint=midpoint,
            )
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            logger.debug("Failed to parse orderbook: %s", exc)
            return None

    async def get_live_prices_with_book(
        self, market: Optional[PolymarketMarket] = None
    ) -> Optional[tuple[float, float, Optional[PolymarketOrderBook], Optional[PolymarketOrderBook]]]:
        """Fetch live midpoint prices AND order books for Up and Down tokens.

        Fires 4 parallel requests (2 midpoints + 2 books). If book requests
        fail, midpoints are still returned with None for the books.

        Returns:
            (up_price, down_price, up_book, down_book) or None on total failure.
        """
        if market is None:
            market = self._current_market
        if market is None:
            logger.error("No market available for live price fetch")
            return None

        up_mid_url = f"{self._clob_url}/midpoint?token_id={market.up_token_id}"
        down_mid_url = f"{self._clob_url}/midpoint?token_id={market.down_token_id}"
        up_book_url = f"{self._clob_url}/book?token_id={market.up_token_id}"
        down_book_url = f"{self._clob_url}/book?token_id={market.down_token_id}"

        up_mid_data, down_mid_data, up_book_data, down_book_data = await asyncio.gather(
            self._get_json(up_mid_url),
            self._get_json(down_mid_url),
            self._get_json(up_book_url),
            self._get_json(down_book_url),
        )

        if up_mid_data is None or down_mid_data is None:
            logger.warning("Failed to fetch midpoint prices for %s", market.slug)
            return None

        try:
            up_price = float(up_mid_data.get("mid", up_mid_data.get("price", 0)))
            down_price = float(down_mid_data.get("mid", down_mid_data.get("price", 0)))
        except (ValueError, TypeError, AttributeError) as exc:
            logger.error("Failed to parse midpoint prices: %s", exc)
            return None

        up_book = self._parse_orderbook(up_book_data)
        down_book = self._parse_orderbook(down_book_data)

        return (up_price, down_price, up_book, down_book)

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch the full orderbook for a given token.

        Args:
            token_id: The CLOB token ID to query.

        Returns:
            Parsed JSON dict containing bids and asks, or None on failure.
        """
        url = f"{self._clob_url}/book?token_id={token_id}"
        data = await self._get_json(url)
        if data is None:
            logger.warning("Failed to fetch orderbook for token %s", token_id)
        return data

    # ------------------------------------------------------------------
    # Fee rate
    # ------------------------------------------------------------------

    async def get_fee_rate_bps(self, token_id: str) -> int:
        """Fetch the taker fee rate in basis points for a token.

        Calls ``GET /fee-rate?token_id={token_id}`` on the CLOB API.
        Results are cached per token_id to avoid redundant requests.

        Args:
            token_id: The CLOB token ID to query.

        Returns:
            Fee rate in basis points (e.g. 0 for fee-free, 1000 for
            fee-enabled crypto markets). Returns 0 on failure.
        """
        if token_id in self._fee_rate_cache:
            return self._fee_rate_cache[token_id]

        url = f"{self._clob_url}/fee-rate?token_id={token_id}"
        data = await self._get_json(url)

        fee_bps = 0
        if data is not None:
            fee_bps = data.get("fee_rate_bps", 0) or 0

        self._fee_rate_cache[token_id] = fee_bps
        logger.info("Fee rate for token %s...: %d bps", token_id[:16], fee_bps)
        return fee_bps

    # ------------------------------------------------------------------
    # Chainlink RTDS stream (primary settlement source)
    # ------------------------------------------------------------------

    def get_chainlink_stream_price(self) -> Optional[float]:
        """Return the latest Chainlink BTC/USD price from the RTDS stream.

        Returns None if no price has been received yet or the price is
        stale (older than CHAINLINK_STREAM_STALE_SECONDS).
        """
        if self._chainlink_stream_price is None:
            return None
        age = time.time() - self._chainlink_stream_ts
        if age > CHAINLINK_STREAM_STALE_SECONDS:
            logger.warning("Chainlink stream price is stale (%.0fs old)", age)
            return None
        return self._chainlink_stream_price

    async def run_chainlink_stream(self) -> None:
        """Stream Chainlink BTC/USD prices from Polymarket RTDS WebSocket.

        Connects to Polymarket's real-time data service and subscribes to
        the ``crypto_prices_chainlink`` topic for ``btc/usd``. This is the
        actual data source Polymarket uses to resolve BTC Up/Down markets.

        Runs indefinitely with automatic reconnection on failure.
        Should be launched as an ``asyncio.Task``.
        """
        if self._session is None or self._session.closed:
            logger.error("Session not started. Call start() before streaming.")
            return

        subscribe_msg = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": json.dumps({"symbol": "btc/usd"}),
            }],
        }

        while True:
            try:
                async with self._session.ws_connect(
                    RTDS_WS_URL, heartbeat=RTDS_PING_INTERVAL,
                ) as ws:
                    await ws.send_json(subscribe_msg)
                    logger.info(
                        "Connected to Polymarket RTDS — streaming Chainlink BTC/USD"
                    )

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                if data.get("topic") == "crypto_prices_chainlink":
                                    payload = data.get("payload", {})
                                    price = payload.get("value") or payload.get("price")
                                    if price is not None:
                                        self._chainlink_stream_price = float(price)
                                        self._chainlink_stream_ts = time.time()
                                        logger.debug(
                                            "Chainlink stream: BTC/USD $%.2f",
                                            self._chainlink_stream_price,
                                        )
                            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                                logger.debug("Failed to parse RTDS message: %s", exc)
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            logger.warning("RTDS WebSocket closed: %s", msg.data)
                            break

            except asyncio.CancelledError:
                logger.info("Chainlink RTDS stream cancelled")
                raise
            except Exception as exc:
                logger.warning(
                    "Chainlink RTDS stream error: %s — reconnecting in 5s", exc
                )
            await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Chainlink on-chain price feed (fallback)
    # ------------------------------------------------------------------

    async def get_chainlink_btc_price(self) -> Optional[float]:
        """Fetch BTC/USD from Chainlink's on-chain aggregator (fallback).

        NOTE: The on-chain feed has a ~1h heartbeat and is unsuitable as
        the primary source for 5-minute settlement.  Prefer
        ``get_chainlink_stream_price()`` which uses Polymarket's RTDS.

        Returns:
            BTC price in USD, or None if the price cannot be fetched.
        """
        if self._session is None or self._session.closed:
            logger.error("Session not started. Call start() before fetching Chainlink price.")
            return None

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {"to": CHAINLINK_BTC_USD_ADDR, "data": CHAINLINK_LATEST_ROUND_DATA},
                "latest",
            ],
            "id": 1,
        }

        for rpc_url in ETH_RPC_URLS:
            try:
                async with self._session.post(
                    rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        continue
                    result = await resp.json()
                    hex_data = result.get("result", "")
                    if not hex_data or hex_data == "0x" or len(hex_data) < 130:
                        continue
                    # latestRoundData() returns (roundId, answer, startedAt, updatedAt, answeredInRound)
                    # answer is the second 32-byte word (chars 66..130 of the hex string)
                    answer_hex = hex_data[66:130]
                    price_raw = int(answer_hex, 16)
                    price = price_raw / (10 ** CHAINLINK_DECIMALS)
                    logger.debug("Chainlink BTC/USD: $%.2f (via %s)", price, rpc_url)
                    return price
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.debug("Chainlink RPC %s failed: %s", rpc_url, exc)
                continue

        logger.warning("Failed to fetch Chainlink BTC/USD price from all RPC endpoints")
        return None

    # ------------------------------------------------------------------
    # Continuous monitoring
    # ------------------------------------------------------------------

    async def monitor_market_cycle(
        self, callback: Callable[[PolymarketMarket], None]
    ) -> None:
        """Continuously monitor BTC 5-minute prediction markets.

        Discovers the current market, polls live prices, and invokes
        *callback* with an updated ``PolymarketMarket`` each cycle. When
        the 5-minute window expires the next market is automatically
        discovered.

        This method runs indefinitely and should be launched as an
        ``asyncio.Task``. Cancel the task to stop monitoring.

        Args:
            callback: A callable (sync or async) that receives the updated
                ``PolymarketMarket`` after each price refresh.
        """
        logger.info("Starting market monitoring cycle")
        current_slug: Optional[str] = None

        while True:
            try:
                new_slug = self.get_current_slug()

                # Detect window transition
                if new_slug != current_slug:
                    if current_slug is not None:
                        logger.info(
                            "Window transition: %s -> %s", current_slug, new_slug
                        )
                    current_slug = new_slug
                    market = await self.discover_market(current_slug)
                    if market is None:
                        logger.warning(
                            "Could not discover market for %s, retrying next cycle",
                            current_slug,
                        )
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                        # Reset slug so we retry discovery on next iteration
                        current_slug = None
                        continue
                else:
                    market = self._current_market

                # Fetch live prices and update the market object
                if market is not None:
                    prices = await self.get_live_prices(market)
                    if prices is not None:
                        up_price, down_price = prices
                        market = PolymarketMarket(
                            slug=market.slug,
                            question=market.question,
                            condition_id=market.condition_id,
                            up_token_id=market.up_token_id,
                            down_token_id=market.down_token_id,
                            up_price=up_price,
                            down_price=down_price,
                            window_start=market.window_start,
                            window_end=market.window_end,
                        )
                        self._current_market = market
                        # Update cache with latest prices
                        self._market_cache[market.slug] = market

                    # Invoke callback (support both sync and async callables)
                    try:
                        result = callback(market)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("Error in monitor callback")

                # Sleep until next poll, but not past the window boundary
                remaining = self.seconds_until_next_window()
                sleep_time = min(POLL_INTERVAL_SECONDS, max(0.1, remaining))
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                logger.info("Market monitoring cycle cancelled")
                raise
            except Exception:
                logger.exception("Unexpected error in monitoring loop, retrying")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
