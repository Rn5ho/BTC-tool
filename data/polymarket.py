"""Async Polymarket client for discovering and monitoring BTC 5-minute prediction markets.

Uses the Gamma API for market discovery and the CLOB API for live price feeds.
Markets follow a deterministic slug pattern: btc-updown-5m-{window_ts} where
window_ts is the current unix timestamp rounded down to the nearest 5-minute boundary.
"""

import asyncio
import logging
import time
from typing import Callable, Optional

import aiohttp

from data.models import PolymarketMarket

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0

# Monitoring loop interval
POLL_INTERVAL_SECONDS = 3.0

# 5-minute window in seconds
WINDOW_SECONDS = 300


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
                import json

                token_ids = json.loads(token_ids)

            outcomes = market_data["outcomes"]
            if isinstance(outcomes, str):
                import json

                outcomes = json.loads(outcomes)

            prices = market_data["outcomePrices"]
            if isinstance(prices, str):
                import json

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
