"""BTC Polymarket 5-Minute Edge Finder — main orchestrator.

Connects to Binance WebSocket streams for real-time BTC data, discovers
Polymarket 5-minute prediction markets, generates directional probability
estimates, detects edge vs. market-implied odds, paper-trades when
mispricing exceeds the configured threshold, and sends Telegram alerts.
"""

import asyncio
import logging
import signal
import sys
import time

from config import settings
from data.binance_ws import BinanceDataCollector
from data.polymarket import PolymarketClient
from signals.features import FeatureEngine
from signals.probability import ProbabilityModel
from strategy.edge import EdgeDetector
from storage.db import Database

# Lazy imports for optional components (may not have their files yet at
# import time, but will exist at runtime).

logger = logging.getLogger("btc_edge")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("btc_edge.log"),
        ],
    )
    # Quieten noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

class Orchestrator:
    """Wires every component together and runs the main analysis loop."""

    def __init__(self) -> None:
        # Components
        self.binance = BinanceDataCollector(ws_url=settings.binance_ws_url)
        self.polymarket = PolymarketClient(
            gamma_url=settings.polymarket_gamma_url,
            clob_url=settings.polymarket_clob_url,
        )
        self.db = Database()
        self.features = FeatureEngine()
        self.model = ProbabilityModel(
            weights={
                "obi": settings.w_obi,
                "taker": settings.w_taker,
                "momentum": settings.w_momentum,
                "rsi": settings.w_rsi,
                "vwap": settings.w_vwap,
                "funding": settings.w_funding,
            }
        )
        self.edge_detector = EdgeDetector(
            model=self.model,
            min_edge=settings.min_edge_threshold,
        )
        self.paper_trader = None  # initialized in start()
        self.alerter = None       # initialized in start()

        # State
        self._running = False
        self._window_btc_start: float | None = None
        self._current_slug: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize all components and launch background tasks."""
        self._running = True
        logger.info("=" * 60)
        logger.info("  BTC Polymarket Edge Finder starting")
        logger.info("=" * 60)

        # Database
        await self.db.initialize()

        # Paper trader (import here to avoid circular / missing file issues)
        from strategy.paper_trader import PaperTrader
        self.paper_trader = PaperTrader(
            db=self.db,
            bankroll=settings.virtual_bankroll,
            bet_size=settings.bet_size_usdc,
            use_kelly=settings.use_kelly,
        )

        # Telegram alerter
        from alerts.telegram import TelegramAlerter
        self.alerter = TelegramAlerter(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        await self.alerter.start()

        # Polymarket session
        await self.polymarket.start()

        # Register Binance callback for candle persistence
        self.binance.on("candle_closed", self._on_candle_closed)

        # Launch concurrent tasks
        tasks = [
            asyncio.create_task(self.binance.start(), name="binance"),
            asyncio.create_task(self._analysis_loop(), name="analysis"),
            asyncio.create_task(self._stats_loop(), name="stats"),
        ]

        logger.info("All tasks launched — entering main loop")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled — shutting down")

    async def stop(self) -> None:
        """Graceful shutdown of all components."""
        logger.info("Shutting down ...")
        self._running = False
        await self.binance.stop()
        await self.polymarket.stop()
        if self.alerter:
            await self.alerter.stop()
        await self.db.close()
        logger.info("Shutdown complete")

    # ------------------------------------------------------------------
    # Binance candle callback
    # ------------------------------------------------------------------

    def _on_candle_closed(self, candle) -> None:
        """Persist closed candles to the database (fire-and-forget)."""
        asyncio.create_task(self.db.save_candle(candle))

    # ------------------------------------------------------------------
    # Main analysis loop
    # ------------------------------------------------------------------

    async def _analysis_loop(self) -> None:
        """Run the feature → probability → edge → trade pipeline every cycle."""

        # Wait for Binance to accumulate some data first
        logger.info("Waiting for initial data from Binance ...")
        while self._running and len(self.binance.candles) < 5:
            await asyncio.sleep(2)
        logger.info(
            "Initial data ready — %d candles buffered", len(self.binance.candles)
        )

        while self._running:
            try:
                await self._run_one_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in analysis cycle")
            await asyncio.sleep(3)  # poll every 3 seconds

    async def _run_one_cycle(self) -> None:
        """Single iteration of the analysis pipeline."""

        # 1. Discover current Polymarket market
        market = await self.polymarket.discover_market()
        if market is None:
            return

        # Detect window transitions for settlement
        if self._current_slug and self._current_slug != market.slug:
            await self._settle_previous_window()
        if self._current_slug != market.slug:
            self._current_slug = market.slug
            self._window_btc_start = self.binance.get_latest_price()
            logger.info(
                "New window: %s | BTC start: %s",
                market.slug,
                self._window_btc_start,
            )

        # 2. Refresh live Polymarket prices
        prices = await self.polymarket.get_live_prices(market)
        if prices:
            up_price, down_price = prices
            market = type(market)(
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

        # Save market snapshot
        btc_price = self.binance.get_latest_price()
        await self.db.save_market_snapshot(
            slug=market.slug,
            up_price=market.up_price,
            down_price=market.down_price,
            btc_price=btc_price,
        )

        # 3. Compute features
        candles = self.binance.get_candles(n=50)
        if len(candles) < 5:
            return

        feature_vec = self.features.compute_features(
            candles=candles,
            orderbook=self.binance.orderbook,
            trades=list(self.binance.recent_trades),
            funding=self.binance.funding,
        )

        # 4. Generate probability estimate
        p_up = self.model.predict(feature_vec)

        # Save feature snapshot
        await self.db.save_feature_snapshot(
            features=feature_vec,
            prob_up=p_up,
            market_slug=market.slug,
        )

        # 5. Detect edge
        signal = self.edge_detector.evaluate(feature_vec, market)
        if signal is None:
            logger.debug(
                "No edge | P(up)=%.3f | Mkt=%.3f/%.3f | slug=%s",
                p_up,
                market.up_price,
                market.down_price,
                market.slug,
            )
            return

        # 6. Edge found — log, trade, alert
        report = self.edge_detector.format_signal_report(signal, feature_vec)
        logger.info("\n%s", report)

        # Paper trade
        if self.paper_trader:
            trade_id = await self.paper_trader.place_trade(signal)
            if trade_id is not None and self.alerter:
                bet_size = self.paper_trader.compute_bet_size(
                    signal["our_prob"], signal["market_prob"]
                )
                await self.alerter.send_trade_alert(
                    side=signal["side"],
                    slug=signal["market_slug"],
                    size=bet_size,
                    entry_price=signal["entry_price"],
                    our_prob=signal["our_prob"],
                    edge=signal["edge"],
                )

        # Telegram edge alert
        if self.alerter:
            await self.alerter.send_edge_alert(
                signal=signal,
                features_breakdown=signal.get("signals", {}),
            )

    # ------------------------------------------------------------------
    # Window settlement
    # ------------------------------------------------------------------

    async def _settle_previous_window(self) -> None:
        """Settle paper trades from the previous 5-minute window."""
        if self._window_btc_start is None:
            return

        btc_end = self.binance.get_latest_price()
        if btc_end is None:
            logger.warning("Cannot settle — no BTC end price available")
            return

        btc_went_up = btc_end >= self._window_btc_start
        logger.info(
            "Settling window %s: start=%.2f end=%.2f went_%s",
            self._current_slug,
            self._window_btc_start,
            btc_end,
            "UP" if btc_went_up else "DOWN",
        )

        if self.paper_trader:
            await self.paper_trader.settle_all_pending(
                btc_start_price=self._window_btc_start,
                btc_end_price=btc_end,
            )

            # Send settlement alerts
            if self.alerter:
                stats = await self.paper_trader.get_stats()
                await self.alerter.send_stats_summary(stats)

    # ------------------------------------------------------------------
    # Periodic stats reporting
    # ------------------------------------------------------------------

    async def _stats_loop(self) -> None:
        """Periodically log and send trading stats (every 30 minutes)."""
        while self._running:
            await asyncio.sleep(1800)  # 30 minutes
            try:
                if self.paper_trader:
                    stats = await self.paper_trader.get_stats()
                    report = self.paper_trader.format_stats_report(stats)
                    logger.info("\n%s", report)
                    if self.alerter:
                        await self.alerter.send_stats_summary(stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in stats loop")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    setup_logging()
    orchestrator = Orchestrator()

    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("Received shutdown signal")
        asyncio.create_task(orchestrator.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        pass
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
