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
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    # Only add a FileHandler when running interactively (TTY).  Under systemd,
    # stdout is already redirected to btc_edge.log via StandardOutput=append,
    # so a second FileHandler would duplicate every line.
    if sys.stdout.isatty():
        handlers.append(logging.FileHandler("btc_edge.log"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    # Quieten noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


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
            fee_rate=settings.polymarket_fee_rate,
            fee_exponent=settings.polymarket_fee_exponent,
        )
        self.paper_trader = None  # initialized in start()
        self.alerter = None       # initialized in start()

        # State
        self._running = False
        self._paused = False  # when True, analysis loop skips trading
        self._window_btc_start: float | None = None
        self._current_slug: str | None = None
        self._window_start_time: float = 0.0  # wall-clock time when window started

        # Trade entry timing — only enter trades in the first N seconds of a
        # 5-minute window.  After this cutoff the market has already priced in
        # the move and any "edge" our model sees is likely stale.
        self._MAX_ENTRY_SECONDS: float = 120.0  # first 2 minutes of the 5-min window

        # Trend-conflict filter — if BTC has already moved more than this
        # percentage within the current window and our signal is the opposite
        # direction, skip the trade.  Prevents betting against strong
        # intra-window momentum that the market has already priced in.
        self._TREND_CONFLICT_PCT: float = 0.15  # 0.15%

        # Heartbeat tracking — avoids flooding the console
        self._cycle_count: int = 0
        self._last_heartbeat: float = 0.0
        self._HEARTBEAT_INTERVAL: float = 30.0  # seconds between status lines

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
            fee_rate=settings.polymarket_fee_rate,
            fee_exponent=settings.polymarket_fee_exponent,
        )
        await self.paper_trader.restore_bankroll()

        # Telegram alerter
        from alerts.telegram import TelegramAlerter
        self.alerter = TelegramAlerter(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        await self.alerter.start()
        self._register_bot_commands()

        # Polymarket session
        await self.polymarket.start()

        # Register Binance callback for candle persistence
        self.binance.on("candle_closed", self._on_candle_closed)

        # Launch concurrent tasks
        tasks = [
            asyncio.create_task(self.binance.start(), name="binance"),
            asyncio.create_task(
                self.polymarket.run_chainlink_stream(), name="chainlink_stream"
            ),
            asyncio.create_task(self._analysis_loop(), name="analysis"),
            asyncio.create_task(self._stats_loop(), name="stats"),
            asyncio.create_task(
                self.alerter.run_command_listener(), name="telegram_cmds"
            ),
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
    # Telegram bot commands
    # ------------------------------------------------------------------

    def _register_bot_commands(self) -> None:
        """Register interactive Telegram commands."""
        if not self.alerter:
            return
        self.alerter.register_command("status", self._cmd_status)
        self.alerter.register_command("stats", self._cmd_stats)
        self.alerter.register_command("trades", self._cmd_trades)
        self.alerter.register_command("pause", self._cmd_pause)
        self.alerter.register_command("resume", self._cmd_resume)
        self.alerter.register_command("weights", self._cmd_weights)
        self.alerter.register_command("analyze", self._cmd_analyze)
        self.alerter.register_command("reset", self._cmd_reset)

    async def _cmd_status(self) -> str:
        """Handle /status — current BTC price, model output, market odds."""
        btc = self.binance.get_latest_price()
        chainlink = self.polymarket.get_chainlink_stream_price()
        candles = len(self.binance.candles)
        trades = len(self.binance.recent_trades)
        ob = "yes" if self.binance.orderbook else "no"

        # Current model P(up)
        p_up_str = "N/A"
        if candles >= 5:
            try:
                feature_vec = self.features.compute_features(
                    candles=self.binance.get_candles(n=50),
                    orderbook=self.binance.orderbook,
                    trades=list(self.binance.recent_trades),
                    funding=self.binance.funding,
                )
                p_up = self.model.predict(feature_vec)
                p_up_str = f"{p_up:.1%}"
            except Exception:
                pass

        # Market odds
        mkt_str = "N/A"
        market = self.polymarket._current_market
        if market:
            mkt_str = f"Up {market.up_price:.0%} / Down {market.down_price:.0%}"

        slug = self._current_slug or "none"
        state = "\u23f8 PAUSED" if self._paused else "\u25b6 ACTIVE"

        return (
            f"\U0001f4ca <b>STATUS</b>\n\n"
            f"State: <b>{state}</b>\n"
            f"BTC (Binance): <b>${btc:,.2f}</b>\n"
            f"BTC (Chainlink): {f'<b>${chainlink:,.2f}</b>' if chainlink else 'N/A'}\n"
            f"Window: {slug}\n"
            f"P(up): <b>{p_up_str}</b>\n"
            f"Market: {mkt_str}\n\n"
            f"Candles: {candles} | Trades: {trades} | OB: {ob}"
        )

    async def _cmd_stats(self) -> str:
        """Handle /stats — trading performance summary."""
        if not self.paper_trader:
            return "Paper trader not initialized."
        stats = await self.paper_trader.get_stats()
        return (
            f"\U0001f4c8 <b>TRADING STATS</b>\n\n"
            f"Total trades: {stats.get('total_trades', 0)}\n"
            f"Settled: {stats.get('settled_trades', 0)}\n"
            f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}\n"
            f"Win rate: <b>{stats.get('win_rate', 0):.1%}</b>\n"
            f"Total P&amp;L: <b>${stats.get('total_pnl', 0):+.2f}</b>\n"
            f"Bankroll: ${stats.get('bankroll', 0):,.2f}\n"
            f"ROI: {stats.get('roi', 0):+.1%}"
        )

    async def _cmd_trades(self) -> str:
        """Handle /trades — list recent/pending paper trades."""
        if not self.paper_trader:
            return "Paper trader not initialized."

        pending = self.paper_trader._pending_trades
        if not pending:
            stats = await self.paper_trader.get_stats()
            settled = stats.get('settled_trades', 0)
            return (
                f"\U0001f4dd <b>TRADES</b>\n\n"
                f"No pending trades.\n"
                f"Total settled: {settled}"
            )

        lines = [f"\U0001f4dd <b>PENDING TRADES</b>\n"]
        for slug, trade in pending.items():
            lines.append(
                f"  {trade.side} {slug}\n"
                f"  Entry: {trade.entry_price:.3f} | "
                f"Size: ${trade.size:.2f}"
            )
        return "\n".join(lines)

    async def _cmd_pause(self) -> str:
        """Handle /pause — stop placing new trades (data collection continues)."""
        if self._paused:
            return "\u23f8 Already paused. Use /resume to restart trading."
        self._paused = True
        logger.info("Trading PAUSED via Telegram command")
        return (
            "\u23f8 <b>Trading PAUSED</b>\n\n"
            "Data collection and analysis continue.\n"
            "No new trades will be placed.\n"
            "Pending trades will still settle.\n"
            "Use /resume to restart."
        )

    async def _cmd_resume(self) -> str:
        """Handle /resume — resume placing trades."""
        if not self._paused:
            return "\u25b6 Already running. Trading is active."
        self._paused = False
        logger.info("Trading RESUMED via Telegram command")
        return (
            "\u25b6 <b>Trading RESUMED</b>\n\n"
            "New trades will be placed when edge is detected."
        )

    async def _cmd_weights(self) -> str:
        """Handle /weights — show current probability model weights."""
        w = self.model.weights
        lines = ["\u2696 <b>MODEL WEIGHTS</b>\n"]
        for name, value in sorted(w.items()):
            bar_len = int(value * 40)
            bar = "\u2588" * bar_len
            lines.append(f"  {name:<10s} {value:.2f}  {bar}")
        total = sum(w.values())
        lines.append(f"\n  Total: {total:.2f}")
        return "\n".join(lines)

    async def _cmd_analyze(self) -> str:
        """Handle /analyze — run trade analysis on the server DB."""
        try:
            import sqlite3 as _sqlite3
            from collections import defaultdict as _defaultdict

            conn = _sqlite3.connect(self.db.db_path)
            conn.row_factory = _sqlite3.Row
            trades = [dict(r) for r in conn.execute(
                "SELECT * FROM paper_trades WHERE outcome IS NOT NULL ORDER BY timestamp"
            ).fetchall()]
            conn.close()

            if not trades:
                return "\u26a0 No settled trades to analyze."

            total = len(trades)
            wins = sum(1 for t in trades if t["outcome"] == "WIN")
            losses = total - wins
            total_pnl = sum(t["pnl"] or 0 for t in trades)
            avg_pnl = total_pnl / total if total else 0

            # Edge bucket analysis
            buckets = [
                ("5-8%", 0.05, 0.08), ("8-12%", 0.08, 0.12),
                ("12-16%", 0.12, 0.16), ("16-20%", 0.16, 0.20),
                ("20%+", 0.20, 1.00),
            ]
            edge_lines = []
            for label, lo, hi in buckets:
                subset = [t for t in trades if lo <= t["edge"] < hi]
                if not subset:
                    continue
                bwins = sum(1 for t in subset if t["outcome"] == "WIN")
                bwr = bwins / len(subset) * 100
                bpnl = sum(t["pnl"] or 0 for t in subset)
                edge_lines.append(
                    f"  {label:>6s}: {bwr:.0f}% ({bwins}/{len(subset)}) ${bpnl:+.2f}"
                )

            # Side analysis
            side_lines = []
            for side in ("UP", "DOWN"):
                subset = [t for t in trades if t["side"] == side]
                if not subset:
                    continue
                swins = sum(1 for t in subset if t["outcome"] == "WIN")
                swr = swins / len(subset) * 100
                spnl = sum(t["pnl"] or 0 for t in subset)
                side_lines.append(
                    f"  {side}: {swr:.0f}% ({swins}/{len(subset)}) ${spnl:+.2f}"
                )

            # Hour analysis (top 3 best, worst)
            from datetime import datetime, timezone
            hour_stats: dict[int, list[bool]] = _defaultdict(list)
            for t in trades:
                ts = t["timestamp"] / 1000
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                hour_stats[dt.hour].append(t["outcome"] == "WIN")

            hour_lines = []
            if hour_stats:
                ranked_hours = sorted(
                    hour_stats.items(),
                    key=lambda x: sum(x[1]) / len(x[1]) if x[1] else 0,
                    reverse=True,
                )
                best = ranked_hours[:3]
                worst = ranked_hours[-3:]
                hour_lines.append("  Best hours:")
                for h, ws in best:
                    wr = sum(ws) / len(ws) * 100
                    hour_lines.append(f"    {h:02d}:00 UTC  {wr:.0f}% ({len(ws)} trades)")
                hour_lines.append("  Worst hours:")
                for h, ws in worst:
                    wr = sum(ws) / len(ws) * 100
                    hour_lines.append(f"    {h:02d}:00 UTC  {wr:.0f}% ({len(ws)} trades)")

            text = (
                f"\U0001f4ca <b>TRADE ANALYSIS</b>\n\n"
                f"<b>Overall:</b>\n"
                f"  Trades: {total} | W/L: {wins}/{losses}\n"
                f"  Win rate: {wins/total*100:.1f}%\n"
                f"  P&amp;L: ${total_pnl:+.2f} (avg ${avg_pnl:+.2f})\n\n"
                f"<b>By edge size:</b>\n"
                + "\n".join(edge_lines) + "\n\n"
                f"<b>By side:</b>\n"
                + "\n".join(side_lines)
            )
            if hour_lines:
                text += "\n\n<b>By hour (UTC):</b>\n" + "\n".join(hour_lines)

            return text

        except Exception as e:
            logger.exception("Error running /analyze")
            return f"\u26a0 Analysis error: {e}"

    async def _cmd_reset(self) -> str:
        """Handle /reset — clear all trade data and reset bankroll.

        This is a destructive operation so requires confirmation via
        a second /reset within 30 seconds.
        """
        now = time.time()
        # Simple confirmation: first /reset sets a timestamp, second /reset
        # within 30s actually resets.
        if hasattr(self, "_reset_requested_at") and now - self._reset_requested_at < 30:
            try:
                # Clear trades from DB
                await self.db._db.execute("DELETE FROM paper_trades")
                await self.db._db.execute("DELETE FROM feature_snapshots")
                await self.db._db.execute("DELETE FROM market_snapshots")
                await self.db._db.commit()

                # Reset in-memory state
                self.paper_trader._pending_trades.clear()
                self.paper_trader._total_fees = 0.0
                self.paper_trader.bankroll = self.paper_trader.initial_bankroll

                self._reset_requested_at = 0
                logger.info("Database RESET via Telegram command")
                return (
                    "\U0001f5d1 <b>RESET COMPLETE</b>\n\n"
                    f"All trades cleared.\n"
                    f"Bankroll reset to ${self.paper_trader.initial_bankroll:.2f}"
                )
            except Exception as e:
                logger.exception("Error during reset")
                return f"\u26a0 Reset failed: {e}"
        else:
            self._reset_requested_at = now
            return (
                "\u26a0 <b>CONFIRM RESET</b>\n\n"
                "This will delete ALL trade data and reset the bankroll.\n"
                "Send /reset again within 30 seconds to confirm."
            )

    # ------------------------------------------------------------------
    # Main analysis loop
    # ------------------------------------------------------------------

    async def _analysis_loop(self) -> None:
        """Run the feature → probability → edge → trade pipeline every cycle."""

        # Wait for Binance to accumulate some data first
        logger.info(
            "Waiting for initial data from Binance (need 5 closed 1-min candles, ~5 min) ..."
        )
        last_count = 0
        while self._running and len(self.binance.candles) < 5:
            count = len(self.binance.candles)
            price = self.binance.get_latest_price()
            trades = len(self.binance.recent_trades)
            if count != last_count or last_count == 0:
                logger.info(
                    "Buffering: %d/5 candles | BTC: %s | trades: %d | orderbook: %s",
                    count,
                    f"${price:,.2f}" if price else "waiting...",
                    trades,
                    "yes" if self.binance.orderbook else "no",
                )
                last_count = count
            await asyncio.sleep(10)
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
            self._window_start_time = time.time()
            # Prefer Chainlink RTDS stream (Polymarket's resolution source)
            self._window_btc_start = self.polymarket.get_chainlink_stream_price()
            price_source = "Chainlink Stream"
            if self._window_btc_start is None:
                self._window_btc_start = self.binance.get_latest_price()
                price_source = "Binance (fallback)"
            logger.info(
                "New window: %s | BTC start: $%.2f [%s]",
                market.slug,
                self._window_btc_start or 0,
                price_source,
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
        self._cycle_count += 1
        signal = self.edge_detector.evaluate(feature_vec, market)

        if signal is None:
            # Quiet heartbeat — only log once every HEARTBEAT_INTERVAL
            now = time.time()
            if now - self._last_heartbeat >= self._HEARTBEAT_INTERVAL:
                self._last_heartbeat = now
                btc_now = self.binance.get_latest_price()
                stats_str = ""
                if self.paper_trader:
                    stats = await self.paper_trader.get_stats()
                    stats_str = (
                        f" | Trades: {stats.get('settled_trades', 0)} "
                        f"({stats.get('win_rate', 0):.0%} win) "
                        f"| P&L: ${stats.get('total_pnl', 0):+.2f}"
                    )
                logger.info(
                    "-- Status: BTC $%s | P(up)=%.1f%% | Mkt=%.0f/%.0f%s",
                    f"{btc_now:,.2f}" if btc_now else "N/A",
                    p_up * 100,
                    market.up_price * 100,
                    market.down_price * 100,
                    stats_str,
                )
            return

        # 6. Edge found — apply safety filters before trading.

        # 6a. Skip if trading is paused.
        if self._paused:
            return

        # 6b. Skip if we already have a pending trade on this market.
        if (
            self.paper_trader
            and market.slug in self.paper_trader._pending_trades
        ):
            return

        btc_now = self.binance.get_latest_price()

        # 6c. Time gate — only enter trades in the first portion of the window.
        #     After the cutoff the market has already priced in the move.
        seconds_in_window = time.time() - self._window_start_time
        if seconds_in_window > self._MAX_ENTRY_SECONDS:
            logger.debug(
                "Skipping edge — too late in window (%.0fs > %.0fs cutoff)",
                seconds_in_window,
                self._MAX_ENTRY_SECONDS,
            )
            return

        # 6d. Trend-conflict filter — don't bet against a strong intra-window
        #     price move.  If BTC has already moved more than TREND_CONFLICT_PCT
        #     in one direction this window and our signal is the opposite, the
        #     market odds already reflect reality and our "edge" is an artefact.
        if self._window_btc_start and btc_now:
            window_move_pct = (btc_now - self._window_btc_start) / self._window_btc_start * 100
            btc_trending_up = window_move_pct > self._TREND_CONFLICT_PCT
            btc_trending_down = window_move_pct < -self._TREND_CONFLICT_PCT
            if (signal["side"] == "UP" and btc_trending_down) or \
               (signal["side"] == "DOWN" and btc_trending_up):
                logger.info(
                    "Skipping edge — trend conflict: signal=%s but BTC moved %+.2f%% this window",
                    signal["side"],
                    window_move_pct,
                )
                return
        signals_brief = signal.get("signals", {})
        top_signals = ", ".join(
            f"{k}={v:+.3f}" for k, v in sorted(
                signals_brief.items(), key=lambda x: abs(x[1]), reverse=True
            )[:3]
        )
        fee_pct = signal.get("fee_factor", 0.0) * 100
        logger.info(
            ">>> EDGE: %s %s | our=%.1f%% mkt=%.1f%% edge=%+.1f%% "
            "fee=%.2f%% | BTC=$%s | [%s]",
            signal["side"],
            signal["market_slug"],
            signal["our_prob"] * 100,
            signal["market_prob"] * 100,
            signal["edge"] * 100,
            fee_pct,
            f"{btc_now:,.2f}" if btc_now else "N/A",
            top_signals,
        )

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

        # Telegram edge alert (only fires once per market — when trade is placed)
        if self.alerter:
            await self.alerter.send_edge_alert(
                signal=signal,
                features_breakdown=signal.get("signals", {}),
            )

    # ------------------------------------------------------------------
    # Window settlement
    # ------------------------------------------------------------------

    async def _settle_previous_window(self) -> None:
        """Settle paper trades from the previous 5-minute window.

        Uses Chainlink RTDS stream price (Polymarket's resolution source).
        Falls back to Binance spot if the stream is unavailable.
        """
        if self._window_btc_start is None:
            return

        # Prefer Chainlink RTDS stream (Polymarket's actual resolution source)
        btc_end = self.polymarket.get_chainlink_stream_price()
        price_source = "Chainlink Stream"
        if btc_end is None:
            btc_end = self.binance.get_latest_price()
            price_source = "Binance (fallback)"
        if btc_end is None:
            logger.warning("Cannot settle — no BTC end price available")
            return

        btc_went_up = btc_end >= self._window_btc_start
        direction = "UP" if btc_went_up else "DOWN"
        delta = btc_end - self._window_btc_start
        logger.info(
            "--- WINDOW SETTLED: %s | BTC $%.2f -> $%.2f (%+.2f = %s) [%s]",
            self._current_slug,
            self._window_btc_start,
            btc_end,
            delta,
            direction,
            price_source,
        )

        if self.paper_trader:
            await self.paper_trader.settle_all_pending(
                btc_start_price=self._window_btc_start,
                btc_end_price=btc_end,
            )

            # Log updated stats after settlement
            stats = await self.paper_trader.get_stats()
            logger.info(
                "--- P&L: $%+.2f | Win rate: %.0f%% (%d/%d) | Bankroll: $%.2f",
                stats.get("total_pnl", 0),
                stats.get("win_rate", 0) * 100,
                stats.get("wins", 0),
                stats.get("settled_trades", 0),
                stats.get("bankroll", 0),
            )

            if self.alerter:
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
