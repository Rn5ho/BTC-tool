"""Async Binance WebSocket client for collecting real-time BTC market data.

Connects to multiple Binance streams (spot kline, depth, aggTrade and futures
mark-price / funding) and maintains rolling in-memory state that downstream
consumers can query at any time.
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Callable, Optional

import websockets

from data.models import AggTrade, Candle, FundingInfo, OrderBook, OrderBookLevel

logger = logging.getLogger(__name__)


class BinanceDataCollector:
    """Real-time BTC/USDT data collector using Binance WebSocket streams."""

    # Combined spot stream endpoint (kline, depth20, aggTrade)
    _SPOT_STREAM = (
        "wss://stream.binance.com:9443/stream"
        "?streams=btcusdt@kline_1m/btcusdt@depth20@100ms/btcusdt@aggTrade"
    )
    # Futures mark-price stream (includes funding rate, updated every 1 s)
    _FUTURES_STREAM = "wss://fstream.binance.com/ws/btcusdt@markPrice@1s"

    # Reconnection back-off parameters
    _BACKOFF_BASE: float = 1.0
    _BACKOFF_MAX: float = 8.0

    def __init__(self, ws_url: str = "wss://stream.binance.com:9443/ws") -> None:
        self.ws_url = ws_url

        # Rolling state ---------------------------------------------------
        self.candles: deque[Candle] = deque(maxlen=100)
        self.current_candle: Optional[Candle] = None
        self.orderbook: Optional[OrderBook] = None
        self.recent_trades: deque[AggTrade] = deque(maxlen=500)
        self.funding: Optional[FundingInfo] = None

        # Control ----------------------------------------------------------
        self._running: bool = False
        self._callbacks: dict[str, list[Callable]] = {}

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Binance spot + futures streams and process messages."""
        self._running = True
        logger.info("BinanceDataCollector starting ...")
        await asyncio.gather(
            self._handle_spot_stream(),
            self._handle_futures_stream(),
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("BinanceDataCollector stopping ...")
        self._running = False

    # ------------------------------------------------------------------
    # Stream handlers (with reconnection)
    # ------------------------------------------------------------------

    async def _handle_spot_stream(self) -> None:
        """Connect to the combined spot stream and dispatch messages."""
        backoff = self._BACKOFF_BASE
        while self._running:
            try:
                async with websockets.connect(self._SPOT_STREAM) as ws:
                    logger.info("Spot stream connected")
                    backoff = self._BACKOFF_BASE  # reset on success
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            # Send a pong to keep connection alive
                            try:
                                await ws.ping()
                            except Exception:
                                break
                            continue

                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("Spot stream: invalid JSON received")
                            continue

                        stream = msg.get("stream", "")
                        data = msg.get("data", {})

                        if "kline" in stream:
                            self._process_kline(data)
                        elif "depth" in stream:
                            self._process_depth(data)
                        elif "aggTrade" in stream:
                            self._process_agg_trade(data)
                        else:
                            logger.debug("Spot stream: unhandled stream %s", stream)

            except asyncio.CancelledError:
                logger.info("Spot stream task cancelled")
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.error(
                    "Spot stream error: %s — reconnecting in %.1fs", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_MAX)

    async def _handle_futures_stream(self) -> None:
        """Connect to the futures mark-price / funding stream."""
        backoff = self._BACKOFF_BASE
        while self._running:
            try:
                async with websockets.connect(self._FUTURES_STREAM) as ws:
                    logger.info("Futures stream connected")
                    backoff = self._BACKOFF_BASE
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            try:
                                await ws.ping()
                            except Exception:
                                break
                            continue

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("Futures stream: invalid JSON received")
                            continue

                        self._process_funding(data)

            except asyncio.CancelledError:
                logger.info("Futures stream task cancelled")
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.error(
                    "Futures stream error: %s — reconnecting in %.1fs", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_MAX)

    # ------------------------------------------------------------------
    # Message processors
    # ------------------------------------------------------------------

    def _process_kline(self, data: dict) -> None:
        """Parse a kline/candlestick event and update rolling candle state."""
        try:
            k = data["k"]
            candle = Candle(
                timestamp=k["t"],
                open=float(k["o"]),
                high=float(k["h"]),
                low=float(k["l"]),
                close=float(k["c"]),
                volume=float(k["v"]),
                taker_buy_volume=float(k["V"]),
                trades=k["n"],
                closed=k["x"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse kline: %s", exc)
            return

        if candle.closed:
            self.candles.append(candle)
            self.current_candle = None
            self._emit("candle_closed", candle)
            logger.debug(
                "Candle closed — ts=%d close=%.2f vol=%.4f",
                candle.timestamp,
                candle.close,
                candle.volume,
            )
        else:
            self.current_candle = candle

    def _process_depth(self, data: dict) -> None:
        """Parse a depth20 snapshot and update the order-book state."""
        try:
            self.orderbook = OrderBook(
                timestamp=int(time.time() * 1000),
                bids=[OrderBookLevel(float(p), float(q)) for p, q in data["bids"]],
                asks=[OrderBookLevel(float(p), float(q)) for p, q in data["asks"]],
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse depth: %s", exc)
            return

        self._emit("depth_update", self.orderbook)

    def _process_agg_trade(self, data: dict) -> None:
        """Parse an aggregate trade event and append to the rolling buffer."""
        try:
            trade = AggTrade(
                timestamp=data["T"],
                price=float(data["p"]),
                quantity=float(data["q"]),
                is_buyer_maker=data["m"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse aggTrade: %s", exc)
            return

        self.recent_trades.append(trade)
        self._emit("trade", trade)

    def _process_funding(self, data: dict) -> None:
        """Parse a mark-price / funding-rate event."""
        try:
            self.funding = FundingInfo(
                timestamp=data["E"],
                funding_rate=float(data["r"]),
                mark_price=float(data["p"]),
                next_funding_time=data["T"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse funding: %s", exc)
            return

        self._emit("funding_update", self.funding)

    # ------------------------------------------------------------------
    # Event system
    # ------------------------------------------------------------------

    def on(self, event: str, callback: Callable) -> None:
        """Register a *callback* for *event*.

        Supported events: ``candle_closed``, ``depth_update``, ``trade``,
        ``funding_update``.
        """
        self._callbacks.setdefault(event, []).append(callback)

    def _emit(self, event: str, data: Any) -> None:
        """Invoke all callbacks registered for *event*."""
        for cb in self._callbacks.get(event, []):
            try:
                cb(data)
            except Exception as exc:
                logger.error("Callback error for event '%s': %s", event, exc)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_latest_price(self) -> Optional[float]:
        """Return the most recent BTC/USDT price.

        Prefers the current (forming) candle close; falls back to the last
        aggregate trade price.
        """
        if self.current_candle is not None:
            return self.current_candle.close
        if self.recent_trades:
            return self.recent_trades[-1].price
        return None

    def get_candles(self, n: int = 50) -> list[Candle]:
        """Return the last *n* closed candles (oldest first)."""
        candles = list(self.candles)
        return candles[-n:]
