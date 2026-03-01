"""BTC Polymarket 5-Minute Edge Finder — main orchestrator.

Connects to Binance WebSocket streams for real-time BTC data, discovers
Polymarket 5-minute prediction markets, generates directional probability
estimates, detects edge vs. market-implied odds, paper-trades when
mispricing exceeds the configured threshold, and sends Telegram alerts.
"""

import asyncio
import json
import logging
import signal
import sys
import time
from typing import Optional

from config import settings
from data.binance_ws import BinanceDataCollector
from data.polymarket import PolymarketClient
from signals.features import FeatureEngine
from signals.probability import ProbabilityModel
from strategy.edge import EdgeDetector
from storage.db import Database

# ML model — loaded when use_ml_model=True
if settings.use_ml_model:
    try:
        from signals.ml_probability import MLProbabilityModel
        _ml_available = True
    except (ImportError, FileNotFoundError) as e:
        _ml_available = False
        logging.getLogger("btc_edge").warning("ML model unavailable: %s — falling back to rule-based", e)
else:
    _ml_available = False

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

        # Choose probability model: ML (trained) or rule-based
        self._use_ml = settings.use_ml_model and _ml_available
        if self._use_ml:
            try:
                self.model = MLProbabilityModel()
                logger.info("Using ML probability model (trained)")
            except FileNotFoundError as e:
                logger.warning("ML model not found: %s — falling back to rule-based", e)
                self._use_ml = False

        if not self._use_ml:
            self.model = ProbabilityModel(
                weights={
                    "obi": settings.w_obi,
                    "taker": settings.w_taker,
                    "momentum": settings.w_momentum,
                    "rsi": settings.w_rsi,
                    "vwap": settings.w_vwap,
                    "funding": settings.w_funding,
                    "volume_zscore": settings.w_volume_zscore,
                    "regime": settings.w_regime,
                },
                confidence_dampen=settings.confidence_dampen,
            )

        self.edge_detector = EdgeDetector(
            model=self.model,
            min_edge=settings.min_edge_threshold,
            max_edge=settings.max_edge_threshold,
            fee_rate=settings.polymarket_fee_rate,
            fee_exponent=settings.polymarket_fee_exponent,
            always_trade=settings.always_trade,
            min_confidence=settings.min_confidence,
        )
        self.paper_trader = None  # initialized in start()
        self.live_trader = None   # initialized in start() when LIVE_TRADING=true
        self.alerter = None       # initialized in start()

        # State
        self._running = False
        self._paused = False  # when True, analysis loop skips trading
        self._window_btc_start: float | None = None
        self._current_slug: str | None = None
        self._window_start_time: float = 0.0  # unix timestamp of window start (from slug)

        # Trade entry timing — only enter trades in the first N seconds of a
        # 5-minute window.  After this cutoff the market has already priced in
        # the move and any "edge" our model sees is likely stale.
        self._MAX_ENTRY_SECONDS: float = 120.0  # first 2 minutes of the 5-min window

        # Trend-conflict filter — if BTC has already moved more than this
        # percentage within the current window and our signal is the opposite
        # direction, skip the trade.  Prevents betting against strong
        # intra-window momentum that the market has already priced in.
        self._TREND_CONFLICT_PCT: float = 0.15  # 0.15%

        # Quality filters — keep only high-quality trades.
        # Edges above this cap are likely model error, not real mispricing.
        self._MAX_EDGE: float = settings.max_edge_threshold
        # Skip when any single signal is near the +-0.5 saturation limits.
        self._MAX_SIGNAL_VALUE: float = settings.max_signal_value

        # Live trade tracking — maps slug → {side, token_id} for auto-sell after settlement
        self._live_trade_tokens: dict[str, dict] = {}

        # Heartbeat tracking — avoids flooding the console
        self._cycle_count: int = 0
        self._last_heartbeat: float = 0.0
        self._HEARTBEAT_INTERVAL: float = 30.0  # seconds between status lines

        # Hour blacklist — UTC hours where the model underperforms.
        # Parsed once from config; empty set = no blacklist.
        self._blacklist_hours: set[int] = set()
        for h in settings.blacklist_hours.split(","):
            h = h.strip()
            if h.isdigit():
                self._blacklist_hours.add(int(h))

    @staticmethod
    def _slug_start_time(slug: str) -> float:
        """Extract the window start timestamp (seconds) from a market slug.

        Slug format: ``btc-updown-5m-{unix_ts}``.  Returns the unix_ts as a
        float so the time gate can compute how far into the window we are.
        Falls back to ``time.time()`` if the slug is malformed.
        """
        try:
            return float(slug.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return time.time()

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
            sizing_strategy=settings.sizing_strategy,
        )
        await self.paper_trader.restore_bankroll()

        # Live trader (real Polymarket orders via py-clob-client)
        if settings.live_trading:
            from strategy.live_trader import LiveTrader, CLOB_AVAILABLE
            if CLOB_AVAILABLE:
                self.live_trader = LiveTrader(
                    private_key=settings.polymarket_private_key,
                    funder_address=settings.polymarket_funder_address,
                    max_bet_usdc=settings.max_live_bet_usdc,
                    fee_rate=settings.polymarket_fee_rate,
                    fee_exponent=settings.polymarket_fee_exponent,
                    sizing_strategy=settings.sizing_strategy,
                )
                ok = await self.live_trader.initialize()
                if ok:
                    # Restore live bankroll from DB
                    live_pnl = await self.db.restore_live_bankroll()
                    self.live_trader.restore_bankroll(
                        settings.virtual_bankroll, live_pnl
                    )
                    logger.info(
                        "LIVE TRADING ENABLED — max bet $%.2f, bankroll $%.2f",
                        settings.max_live_bet_usdc,
                        self.live_trader.bankroll,
                    )
                else:
                    logger.error("Live trader failed to initialize — running paper-only")
                    self.live_trader = None
            else:
                logger.warning(
                    "LIVE_TRADING=true but py-clob-client not installed — paper-only"
                )

        # Telegram alerter
        from alerts.telegram import TelegramAlerter
        self.alerter = TelegramAlerter(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        await self.alerter.start()
        if self.live_trader and self.live_trader.is_active:
            await self.alerter._send(
                "\U0001f4b5 <b>LIVE TRADING ENABLED</b>\n\n"
                f"Max bet: ${settings.max_live_bet_usdc:.2f}\n"
                "Paper trading continues in parallel."
            )
        self._register_bot_commands()

        # Polymarket session
        await self.polymarket.start()

        # Prefetch recent candles from Binance REST API so the ML model
        # can run immediately without waiting 30+ min for WS candles.
        prefetched = await self.binance.prefetch_candles(count=35)
        if prefetched >= 30:
            logger.info("Prefetch complete — %d candles ready, skipping buffering wait", prefetched)

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
            asyncio.create_task(self._proxy_watchdog(), name="proxy_watchdog"),
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
        self.alerter.register_command("reset", self._cmd_reset)
        self.alerter.register_command("budget", self._cmd_budget)
        self.alerter.register_command("pause", self._cmd_pause)
        self.alerter.register_command("resume", self._cmd_resume)
        self.alerter.register_command("weights", self._cmd_weights)
        self.alerter.register_command("regime", self._cmd_regime)
        self.alerter.register_command("analyze", self._cmd_analyze)
        self.alerter.register_command("spread", self._cmd_spread)
        self.alerter.register_command("balance", self._cmd_balance)
        self.alerter.register_command("livetrades", self._cmd_livetrades)

    async def _cmd_status(self, args: str = "") -> str:
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
        model_type = "ML" if self._use_ml else "Rule-based"
        mode = "always-trade" if settings.always_trade else "edge-threshold"
        live_str = ""
        if self.live_trader and self.live_trader.is_active:
            live_status = "PAUSED" if self.live_trader.is_paused else "ACTIVE"
            live_str = f"\nLive trading: <b>{live_status}</b> (max ${settings.max_live_bet_usdc:.2f})"

        return (
            f"\U0001f4ca <b>STATUS</b>\n\n"
            f"State: <b>{state}</b>\n"
            f"Model: {model_type} ({mode}){live_str}\n"
            f"BTC (Binance): <b>${btc:,.2f}</b>\n"
            f"BTC (Chainlink): {f'<b>${chainlink:,.2f}</b>' if chainlink else 'N/A'}\n"
            f"Window: {slug}\n"
            f"P(up): <b>{p_up_str}</b>\n"
            f"Market: {mkt_str}\n\n"
            f"Candles: {candles} | Trades: {trades} | OB: {ob}"
        )

    async def _cmd_stats(self, args: str = "") -> str:
        """Handle /stats — trading performance summary (live primary, paper secondary)."""
        parts = []

        # Live stats (primary)
        if self.live_trader and self.live_trader.is_active:
            ls = await self.db.get_live_trading_stats_full()
            roi = 0.0
            if self.live_trader.initial_bankroll > 0:
                roi = (self.live_trader.bankroll - self.live_trader.initial_bankroll) / self.live_trader.initial_bankroll
            parts.append(
                f"\U0001f4b5 <b>LIVE TRADING</b>\n"
                f"Settled: {ls.get('settled', 0)} | "
                f"W/L: {ls.get('wins', 0)}/{ls.get('losses', 0)}\n"
                f"Win rate: <b>{ls.get('win_rate', 0):.1%}</b>\n"
                f"P&amp;L: <b>${ls.get('total_pnl', 0):+.2f}</b>\n"
                f"Bankroll: ${self.live_trader.bankroll:,.2f}\n"
                f"ROI: {roi:+.1%}"
            )

        # Paper stats (secondary)
        if self.paper_trader:
            stats = await self.paper_trader.get_stats()
            parts.append(
                f"\U0001f4dd <b>PAPER TRADING</b>\n"
                f"Settled: {stats.get('settled_trades', 0)} | "
                f"W/L: {stats.get('wins', 0)}/{stats.get('losses', 0)}\n"
                f"Win rate: {stats.get('win_rate', 0):.1%}\n"
                f"P&amp;L: ${stats.get('total_pnl', 0):+.2f}\n"
                f"Bankroll: ${stats.get('bankroll', 0):,.2f}"
            )

        if not parts:
            return "No trading data."
        return f"\U0001f4c8 <b>TRADING STATS</b>\n\n" + "\n\n".join(parts)

    async def _cmd_trades(self, args: str = "") -> str:
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
                f"  {trade['side']} {slug}\n"
                f"  Entry: {trade['entry_price']:.3f} | "
                f"Size: ${trade['size_usdc']:.2f}"
            )
        return "\n".join(lines)

    async def _cmd_reset(self, args: str = "") -> str:
        """Handle /reset — clear all trade data and reset bankroll.

        This is a destructive operation so requires confirmation via
        a second /reset within 30 seconds.
        """
        if not self.paper_trader:
            return "Paper trader not initialized."

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

    async def _cmd_budget(self, args: str = "") -> str:
        """Handle /budget [amount] — show or set the bankroll.

        /budget        — show current bankroll and bet size
        /budget 200    — set bankroll to $200
        """
        if not self.paper_trader:
            return "Paper trader not initialized."

        if not args.strip():
            live_line = ""
            if self.live_trader and self.live_trader.is_active:
                live_line = (
                    f"\n\n<b>Live:</b>\n"
                    f"Bankroll: <b>${self.live_trader.bankroll:.2f}</b>\n"
                    f"Initial: ${self.live_trader.initial_bankroll:.2f}\n"
                    f"Sizing: {self.live_trader._sizing_strategy}"
                )
            return (
                f"\U0001f4b0 <b>BUDGET</b>\n\n"
                f"<b>Paper:</b>\n"
                f"Bankroll: ${self.paper_trader.bankroll:.2f}\n"
                f"Initial: ${self.paper_trader.initial_bankroll:.2f}\n"
                f"Sizing: {self.paper_trader.sizing_strategy}"
                f"{live_line}"
            )

        try:
            amount = float(args.strip())
            if amount <= 0:
                return "Amount must be positive."
        except ValueError:
            return f"Invalid amount: {args.strip()}\nUsage: /budget 200"

        self.paper_trader.bankroll = amount
        self.paper_trader.initial_bankroll = amount

        # Also update live bankroll if active
        if self.live_trader and self.live_trader.is_active:
            self.live_trader.bankroll = amount
            self.live_trader.initial_bankroll = amount
            self.live_trader._max_bankroll = amount

        return (
            f"\U0001f4b0 <b>BUDGET UPDATED</b>\n\n"
            f"Bankroll set to <b>${amount:.2f}</b>"
        )

    async def _cmd_pause(self, args: str = "") -> str:
        """Handle /pause — stop placing new trades (data collection continues)."""
        if self._paused:
            return "\u23f8 Already paused. Use /resume to restart trading."
        self._paused = True
        if self.live_trader:
            self.live_trader.pause()
        logger.info("Trading PAUSED via Telegram command")
        return (
            "\u23f8 <b>Trading PAUSED</b>\n\n"
            "Data collection and analysis continue.\n"
            "No new trades will be placed.\n"
            "Pending trades will still settle.\n"
            "Use /resume to restart."
        )

    async def _cmd_resume(self, args: str = "") -> str:
        """Handle /resume — resume placing trades."""
        if not self._paused:
            return "\u25b6 Already running. Trading is active."
        self._paused = False
        if self.live_trader:
            self.live_trader.resume()
        logger.info("Trading RESUMED via Telegram command")
        return (
            "\u25b6 <b>Trading RESUMED</b>\n\n"
            "New trades will be placed when edge is detected."
        )

    async def _cmd_weights(self, args: str = "") -> str:
        """Handle /weights — show current probability model weights."""
        if self._use_ml:
            return (
                "\u2696 <b>ML MODEL</b>\n\n"
                f"Type: {type(self.model._model).__name__}\n"
                f"Features: 44\n"
                f"Mode: {'always-trade' if settings.always_trade else 'edge-threshold'}\n"
                f"Sizing: {settings.sizing_strategy}"
            )
        w = self.model.weights
        lines = ["\u2696 <b>MODEL WEIGHTS</b>\n"]
        for name, value in sorted(w.items()):
            bar_len = int(value * 40)
            bar = "\u2588" * bar_len
            lines.append(f"  {name:<10s} {value:.2f}  {bar}")
        total = sum(w.values())
        lines.append(f"\n  Total: {total:.2f}")
        return "\n".join(lines)

    async def _cmd_regime(self, args: str = "") -> str:
        """Handle /regime — show current market regime from EMA cross + BB position."""
        candles = self.binance.get_candles(n=50)
        if len(candles) < 21:
            return "\u26a0 Need at least 21 candles for regime calculation."

        from signals.indicators import TechnicalIndicators

        ema_cross = TechnicalIndicators.ema_cross_signal(candles)
        bb_pos = TechnicalIndicators.bb_position(candles)
        if hasattr(self.model, '_normalize_regime'):
            regime = self.model._normalize_regime(ema_cross, bb_pos)
        else:
            # ML model: compute regime as simple EMA cross signal
            regime = max(-0.5, min(0.5, ema_cross * 100))

        # Visual bar
        bar_pos = int((regime + 0.5) * 20)  # 0-20 scale
        bar_pos = max(0, min(20, bar_pos))
        bar = "\u2591" * bar_pos + "\u2588" + "\u2591" * (20 - bar_pos)

        if regime < -0.15:
            label = "BEARISH"
        elif regime > 0.15:
            label = "BULLISH"
        else:
            label = "NEUTRAL"

        btc = self.binance.get_latest_price()
        closes = [c.close for c in candles]
        ema9 = TechnicalIndicators.ema(closes, 9)
        ema21 = TechnicalIndicators.ema(closes, 21)
        lower, middle, upper = TechnicalIndicators.bollinger_bands(candles)

        return (
            f"\U0001f30d <b>MARKET REGIME</b>\n\n"
            f"Regime: <b>{label}</b> ({regime:+.3f})\n"
            f"[{bar}]\n"
            f"  BEAR {'<' * 10} {'>' * 10} BULL\n\n"
            f"<b>Components:</b>\n"
            f"  EMA cross: {ema_cross:+.4f} (EMA9=${ema9:,.0f} vs EMA21=${ema21:,.0f})\n"
            f"  BB position: {bb_pos:.3f} (0=lower, 1=upper)\n"
            f"  BB range: ${lower:,.0f} - ${upper:,.0f}\n\n"
            f"BTC: ${btc:,.2f}"
        )

    async def _cmd_analyze(self, args: str = "") -> str:
        """Handle /analyze — run trade analysis on the DB."""
        try:
            import sqlite3 as _sqlite3
            from collections import defaultdict as _defaultdict
            from datetime import datetime, timezone

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
            hour_stats: dict[int, list[bool]] = _defaultdict(list)
            for t in trades:
                ts = t["timestamp"] / 1000
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                hour_stats[dt.hour].append(t["outcome"] == "WIN")

            hour_lines: list[str] = []
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

    async def _cmd_spread(self, args: str = "") -> str:
        """Handle /spread — show live order book spreads for current market."""
        market = self.polymarket._current_market
        if market is None:
            return "\u26a0 No active market."

        result = await self.polymarket.get_live_prices_with_book(market)
        if result is None:
            return "\u26a0 Failed to fetch order book."

        up_price, down_price, up_book, down_book = result

        def _fmt_book(label: str, book, midpoint: float) -> str:
            if book is None:
                return f"  {label}: no book data (mid={midpoint:.3f})"
            return (
                f"  {label}: bid={book.best_bid:.3f} ask={book.best_ask:.3f} "
                f"spread={book.spread:.4f}\n"
                f"    mid={book.midpoint:.3f} | "
                f"bid_sz={book.bid_size:.0f} ask_sz={book.ask_size:.0f}"
            )

        return (
            f"\U0001f4d6 <b>ORDER BOOK</b>\n\n"
            f"Market: {market.slug}\n\n"
            f"<b>UP token:</b>\n{_fmt_book('UP', up_book, up_price)}\n\n"
            f"<b>DOWN token:</b>\n{_fmt_book('DOWN', down_book, down_price)}"
        )

    async def _cmd_balance(self, args: str = "") -> str:
        """Handle /balance — show USDC balance, live bankroll, and trading summary."""
        if not self.live_trader or not self.live_trader.is_active:
            return "\u26a0 Live trading is not enabled."
        balance = await self.live_trader.get_balance()
        if balance is None:
            return "\u26a0 Failed to fetch balance."
        db_stats = await self.db.get_live_trading_stats_full()
        session = self.live_trader.get_session_summary()
        pending = len(self._live_trade_tokens)
        roi = 0.0
        if self.live_trader.initial_bankroll > 0:
            roi = (self.live_trader.bankroll - self.live_trader.initial_bankroll) / self.live_trader.initial_bankroll
        return (
            f"\U0001f4b0 <b>POLYMARKET BALANCE</b>\n\n"
            f"USDC: <b>${balance:.2f}</b>\n"
            f"Live bankroll: <b>${self.live_trader.bankroll:.2f}</b>\n"
            f"Live P&amp;L: ${db_stats.get('total_pnl', 0):+.2f} (ROI: {roi:+.1%})\n"
            f"Max bet: ${settings.max_live_bet_usdc:.2f}\n"
            f"Pending claims: {pending}\n\n"
            f"<b>Results:</b> {db_stats.get('settled', 0)} settled | "
            f"W/L: {db_stats.get('wins', 0)}/{db_stats.get('losses', 0)} "
            f"({db_stats.get('win_rate', 0):.0%})\n"
            f"<b>Session:</b> {session['successful']} filled / "
            f"{session['total']} total (${session['total_amount']:.2f})"
        )

    async def _cmd_livetrades(self, args: str = "") -> str:
        """Handle /livetrades — show live trade stats with P&L."""
        db_stats = await self.db.get_live_trading_stats_full()
        if db_stats["total"] == 0:
            return "\u26a0 No live trades recorded."

        lines = [
            f"\U0001f4b5 <b>LIVE TRADES</b>\n",
            f"<b>All time:</b>",
            f"  Orders: {db_stats['total']} ({db_stats['successful']} filled, {db_stats['failed']} failed)",
            f"  Settled: {db_stats['settled']} | W/L: {db_stats['wins']}/{db_stats['losses']}",
            f"  Win rate: {db_stats['win_rate']:.1%}",
            f"  P&amp;L: ${db_stats['total_pnl']:+.2f}",
            f"  Volume: ${db_stats['total_amount']:.2f}",
        ]

        if self.live_trader and self.live_trader.is_active:
            lines.append(f"\n  Bankroll: ${self.live_trader.bankroll:.2f}")
            summary = self.live_trader.get_session_summary()
            lines.append(
                f"\n<b>This session:</b>\n"
                f"  Orders: {summary['total']} ({summary['successful']} filled)\n"
                f"  Amount: ${summary['total_amount']:.2f}"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Main analysis loop
    # ------------------------------------------------------------------

    async def _analysis_loop(self) -> None:
        """Run the feature → probability → edge → trade pipeline every cycle."""

        # Wait for Binance to accumulate some data first
        min_candles = 30 if self._use_ml else 5
        logger.info(
            "Waiting for initial data from Binance (need %d closed 1-min candles) ...",
            min_candles,
        )
        last_count = 0
        while self._running and len(self.binance.candles) < min_candles:
            count = len(self.binance.candles)
            price = self.binance.get_latest_price()
            trades = len(self.binance.recent_trades)
            if count != last_count or last_count == 0:
                logger.info(
                    "Buffering: %d/%d candles | BTC: %s | trades: %d | orderbook: %s",
                    count,
                    min_candles,
                    f"${price:,.2f}" if price else "waiting...",
                    trades,
                    "yes" if self.binance.orderbook else "no",
                )
                last_count = count
            await asyncio.sleep(10)
        logger.info(
            "Initial data ready — %d candles buffered", len(self.binance.candles)
        )

        # Initialize window tracking now that we have price data
        self._current_slug = self.polymarket.get_current_slug()
        self._window_start_time = self._slug_start_time(self._current_slug)
        self._window_btc_start = self.polymarket.get_chainlink_stream_price()
        price_source = "Chainlink Stream"
        if self._window_btc_start is None:
            self._window_btc_start = self.binance.get_latest_price()
            price_source = "Binance (fallback)"
        logger.info(
            "Initialized window: %s | BTC start: $%.2f [%s]",
            self._current_slug,
            self._window_btc_start or 0,
            price_source,
        )

        # Settle any stale unsettled trades from previous sessions
        await self._settle_stale_trades()
        await self._settle_stale_live_trades()

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

        # 0. Check for window transitions BEFORE market discovery.
        #    Settlement must not depend on the Gamma API succeeding —
        #    otherwise trades remain unsettled if discovery is slow/fails.
        current_slug = self.polymarket.get_current_slug()
        if self._current_slug and self._current_slug != current_slug:
            await self._settle_previous_window()
        if self._current_slug != current_slug:
            self._current_slug = current_slug
            self._window_start_time = self._slug_start_time(current_slug)
            # Prefer Chainlink RTDS stream (Polymarket's resolution source)
            self._window_btc_start = self.polymarket.get_chainlink_stream_price()
            price_source = "Chainlink Stream"
            if self._window_btc_start is None:
                self._window_btc_start = self.binance.get_latest_price()
                price_source = "Binance (fallback)"
            logger.info(
                "New window: %s | BTC start: $%.2f [%s]",
                current_slug,
                self._window_btc_start or 0,
                price_source,
            )

        # 0b. Early exit monitoring — log bid prices for active positions
        #     in the last 2 minutes of the window (data collection only).
        await self._monitor_early_exit()

        # 1. Discover current Polymarket market
        market = await self.polymarket.discover_market()
        if market is None:
            return

        # 2. Refresh live Polymarket prices + order books
        book_result = await self.polymarket.get_live_prices_with_book(market)
        if book_result:
            up_price, down_price, up_book, down_book = book_result
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
                up_best_bid=up_book.best_bid if up_book else None,
                up_best_ask=up_book.best_ask if up_book else None,
                up_spread=up_book.spread if up_book else None,
                down_best_bid=down_book.best_bid if down_book else None,
                down_best_ask=down_book.best_ask if down_book else None,
                down_spread=down_book.spread if down_book else None,
            )

        # Save market snapshot
        btc_price = self.binance.get_latest_price()
        await self.db.save_market_snapshot(
            slug=market.slug,
            up_price=market.up_price,
            down_price=market.down_price,
            btc_price=btc_price,
            up_best_bid=market.up_best_bid,
            up_best_ask=market.up_best_ask,
            up_spread=market.up_spread,
            down_best_bid=market.down_best_bid,
            down_best_ask=market.down_best_ask,
            down_spread=market.down_spread,
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
        if self._use_ml:
            # ML model: use full candle history for all 44 features
            window_ts = int(self._window_start_time)
            p_up = self.model.predict_from_candles(candles, window_ts)
        else:
            p_up = self.model.predict(feature_vec)

        # Save feature snapshot
        await self.db.save_feature_snapshot(
            features=feature_vec,
            prob_up=p_up,
            market_slug=market.slug,
        )

        # 5. Detect edge (pass p_up_override so edge detector uses ML prediction)
        self._cycle_count += 1
        signal = self.edge_detector.evaluate(
            feature_vec, market, p_up_override=p_up
        )

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
                model_tag = "[ML]" if self._use_ml else "[RB]"
                spread_str = ""
                if market.up_spread is not None:
                    spread_str = f" | spread={market.up_spread:.4f}"
                logger.info(
                    "-- Status %s: BTC $%s | P(up)=%.1f%% | Mkt=%.0f/%.0f%s%s",
                    model_tag,
                    f"{btc_now:,.2f}" if btc_now else "N/A",
                    p_up * 100,
                    market.up_price * 100,
                    market.down_price * 100,
                    spread_str,
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

        # 6d. Hour blacklist — skip hours with historically poor performance.
        from datetime import datetime, timezone as _tz
        current_hour = datetime.now(_tz.utc).hour
        if current_hour in self._blacklist_hours:
            logger.debug(
                "Skipping edge — hour %02d:00 UTC is blacklisted", current_hour
            )
            return

        # 6e. Trend-conflict filter — don't bet against a strong intra-window
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

        # 6f. Max edge cap — only for classic (non-always-trade) mode.
        #     In always-trade mode the edge is just model-vs-market gap, not
        #     a quality signal, and the entry price filter handles thin books.
        if not settings.always_trade and signal["edge"] > self._MAX_EDGE:
            logger.info(
                "Skipping edge — too large (%.1f%% > %.1f%% cap): likely noise",
                signal["edge"] * 100,
                self._MAX_EDGE * 100,
            )
            return

        # 6g. Signal saturation filter — only applies to rule-based model.
        #     ML model doesn't have the same +-0.5 signal structure.
        if not self._use_ml:
            signals_data = signal.get("signals", {})
            saturated = {
                k: v for k, v in signals_data.items()
                if abs(v) > self._MAX_SIGNAL_VALUE
            }
            if saturated:
                sat_str = ", ".join(f"{k}={v:+.3f}" for k, v in saturated.items())
                logger.info(
                    "Skipping edge — saturated signals [%s] (limit=+-%.2f)",
                    sat_str,
                    self._MAX_SIGNAL_VALUE,
                )
                return

        signals_brief = signal.get("signals", {})
        # Filter to numeric values only (ML breakdown includes string 'ml_side')
        numeric_signals = {k: v for k, v in signals_brief.items() if isinstance(v, (int, float))}
        top_signals = ", ".join(
            f"{k}={v:+.3f}" for k, v in sorted(
                numeric_signals.items(), key=lambda x: abs(x[1]), reverse=True
            )[:3]
        )
        fee_pct = signal.get("fee_factor", 0.0) * 100
        conf_pct = signal.get("confidence", 0.0) * 100
        explore_tag = " [EXPLORE]" if signal.get("exploration") else ""
        model_tag = ("ML" if self._use_ml else "RB") + explore_tag
        spread_val = signal.get("spread")
        midpoint_val = signal.get("midpoint_price")
        spread_info = ""
        if spread_val is not None and midpoint_val is not None:
            spread_info = f" spread={spread_val:.4f} mid={midpoint_val:.3f}"
        logger.info(
            ">>> %s %s %s | our=%.1f%% mkt=%.1f%% edge=%+.1f%% "
            "conf=%.1f%% fee=%.2f%%%s | BTC=$%s | [%s]",
            model_tag,
            signal["side"],
            signal["market_slug"],
            signal["our_prob"] * 100,
            signal["market_prob"] * 100,
            signal["edge"] * 100,
            conf_pct,
            fee_pct,
            spread_info,
            f"{btc_now:,.2f}" if btc_now else "N/A",
            top_signals,
        )

        # Paper trade (no Telegram — paper stats kept in DB/logs only)
        if self.paper_trader:
            paper_signal = signal
            if signal.get("exploration"):
                paper_signal = {**signal, "size_override": 1.00}
            await self.paper_trader.place_trade(paper_signal)

        # Live trade — place real order on Polymarket
        if self.live_trader and self.live_trader.is_active and not self.live_trader.is_paused:
            # Determine token ID from market
            if market:
                token_id = (
                    market.up_token_id if signal["side"] == "UP"
                    else market.down_token_id
                )
                # Use live trader's own adaptive sizing (based on live bankroll)
                # Exploration trades: minimum size ($3.50 floor) for data collection
                if signal.get("exploration"):
                    live_amount = 3.50
                else:
                    live_amount = self.live_trader.compute_bet_size(
                        confidence=signal.get("confidence", 0.0),
                    )

                live_result = await self.live_trader.place_order(
                    token_id=token_id,
                    amount_usdc=live_amount,
                    side=signal["side"],
                    market_slug=signal["market_slug"],
                    entry_price=signal["entry_price"],
                )

                # Persist to DB first (to get row ID for early exit tracking)
                trade_tag = "exploration" if signal.get("exploration") else None
                live_trade_id = await self.db.save_live_trade(
                    timestamp=int(time.time() * 1000),
                    market_slug=signal["market_slug"],
                    side=signal["side"],
                    token_id=token_id,
                    amount_usdc=live_result["amount"],
                    order_id=live_result.get("order_id"),
                    status="filled" if live_result["success"] else "failed",
                    success=live_result["success"],
                    response_json=json.dumps(live_result.get("response"))
                    if live_result.get("response") else None,
                    entry_price=signal["entry_price"],
                    trade_tag=trade_tag,
                )

                # Track token for settlement + early exit
                if live_result["success"]:
                    from data.polymarket import compute_fee_factor
                    fee_factor = compute_fee_factor(
                        signal["entry_price"],
                        settings.polymarket_fee_rate,
                        settings.polymarket_fee_exponent,
                    )
                    tokens = (live_result["amount"] / signal["entry_price"]) * (1.0 - fee_factor)
                    self._live_trade_tokens[signal["market_slug"]] = {
                        "side": signal["side"],
                        "token_id": token_id,
                        "amount": live_result["amount"],
                        "entry_price": signal["entry_price"],
                        "tokens": tokens,
                        "db_id": live_trade_id,
                    }

                # Telegram alert
                if self.alerter:
                    await self.alerter.send_live_trade_alert(
                        side=signal["side"],
                        slug=signal["market_slug"],
                        amount=live_result["amount"],
                        order_id=live_result.get("order_id"),
                        success=live_result["success"],
                        error_msg=live_result.get("error", ""),
                    )

        # Telegram edge alert (only fires once per market — when trade is placed)
        if self.alerter:
            await self.alerter.send_edge_alert(
                signal=signal,
                features_breakdown=signal.get("signals", {}),
            )

    # ------------------------------------------------------------------
    # Early exit monitoring (data collection — no trading)
    # ------------------------------------------------------------------

    # Only log early exit data every N seconds to avoid flooding
    _EARLY_EXIT_LOG_INTERVAL: float = 10.0
    _last_early_exit_log: float = 0.0

    async def _monitor_early_exit(self) -> None:
        """Log bid prices for active positions in the last 2 min of window.

        This is data collection only — no trades are placed.  The logs will
        show whether there is enough liquidity to sell winning tokens early
        (e.g. at $0.90+ bid) before the window settles.
        """
        if not self._current_slug or not self._window_start_time:
            return

        seconds_in = time.time() - self._window_start_time
        # Only monitor in the last 120 seconds of the 5-min (300s) window
        if seconds_in < 180:
            return

        # Rate-limit logging
        now = time.time()
        if now - self._last_early_exit_log < self._EARLY_EXIT_LOG_INTERVAL:
            return

        # Check if we have an active position (live or paper) on this window
        slug = self._current_slug
        live_pos = self._live_trade_tokens.get(slug)
        paper_pos = self.paper_trader._pending_trades.get(slug) if self.paper_trader else None

        if not live_pos and not paper_pos:
            return

        # Get the token_id for our position's side
        pos = live_pos or paper_pos
        side = pos["side"]
        market = self.polymarket._current_market
        if not market:
            return

        token_id = market.up_token_id if side == "UP" else market.down_token_id
        entry_price = pos.get("entry_price", 0)

        # Fetch the order book for our token
        book_data = await self.polymarket.get_orderbook(token_id)
        if not book_data:
            return

        self._last_early_exit_log = now

        # Parse bids (buyers willing to buy our token).
        # CLOB API returns bids sorted ascending — best bid is LAST.
        bids = book_data.get("bids", [])
        if not bids:
            logger.info(
                "[EARLY-EXIT] %s %s | %.0fs left | NO BIDS | entry=%.3f",
                side, slug[-15:], 300 - seconds_in, entry_price,
            )
            return

        # Best bid = highest price (last element in ascending sort)
        best_bid = float(bids[-1].get("price", 0))
        best_bid_size = float(bids[-1].get("size", 0))

        # Total bid depth above 0.85
        deep_bids = [(float(b["price"]), float(b["size"])) for b in bids if float(b["price"]) >= 0.85]
        total_deep_size = sum(s for _, s in deep_bids)

        # Current BTC price for context
        btc = self.binance.get_latest_price()
        btc_move = ""
        if btc and self._window_btc_start:
            delta = btc - self._window_btc_start
            btc_move = f" | BTC {'+' if delta >= 0 else ''}{delta:.2f}"

        # Would we profit by selling at best_bid?
        profit_pct = ((best_bid / entry_price) - 1) * 100 if entry_price > 0 else 0

        seconds_left = 300 - seconds_in

        logger.info(
            "[EARLY-EXIT] %s %s | %.0fs left | bid=%.3f x%.0f | depth>=0.85: %.0f tokens | "
            "entry=%.3f profit=%.1f%%%s",
            side, slug[-15:], seconds_left,
            best_bid, best_bid_size,
            total_deep_size,
            entry_price, profit_pct, btc_move,
        )

        # ---- EARLY EXIT TRIGGER ----
        # Sell live tokens when bid is high enough to lock in profit.
        # No time restriction — if someone offers 90%+ value at any point
        # in the last 2 minutes, take it rather than risk a reversal.
        available_depth = best_bid_size + total_deep_size
        if (live_pos
                and not live_pos.get("exited")
                and self.live_trader
                and best_bid >= 0.90
                and available_depth >= 20):

            result = await self.live_trader.sell_early_exit(
                token_id=token_id,
                tokens=live_pos["tokens"],
                best_bid=best_bid,
                market_slug=slug,
            )

            if result["success"]:
                sell_amount = result["sell_amount"]
                buy_amount = live_pos["amount"]
                pnl = sell_amount - buy_amount

                await self.db.update_live_trade(
                    live_pos["db_id"], "EARLY_EXIT", pnl, int(time.time()),
                )
                self.live_trader.record_settlement(pnl > 0, pnl)
                live_pos["exited"] = True

                logger.info(
                    "[EARLY-EXIT SOLD] %s %s | bid=%.3f | tokens=%.1f | "
                    "sell=$%.2f buy=$%.2f | pnl=$%+.2f | %.0fs before settlement",
                    side, slug[-15:], best_bid, live_pos["tokens"],
                    sell_amount, buy_amount, pnl, seconds_left,
                )

                # Telegram alert
                if self.alerter:
                    try:
                        await self.alerter._send(
                            f"<b>EARLY EXIT</b>\n\n"
                            f"Market: {slug}\n"
                            f"Side: {side} | Sold at ${best_bid:.3f}\n"
                            f"Tokens: {live_pos['tokens']:.1f} | Sell: ${sell_amount:.2f}\n"
                            f"PnL: <b>${pnl:+.2f}</b> | {seconds_left:.0f}s early"
                        )
                    except Exception:
                        pass

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

        # Settle live trades from DB for this window
        slug = self._current_slug
        if self.live_trader and self.live_trader.is_active:
            await self._settle_live_trades_for_window(slug, btc_went_up)

        # Also handle in-memory tracked tokens (for Telegram alerts on
        # trades placed this session but not yet in the settlement DB flow)
        if slug and slug in self._live_trade_tokens:
            live_info = self._live_trade_tokens.pop(slug)
            trade_won = (live_info["side"] == "UP" and btc_went_up) or \
                        (live_info["side"] == "DOWN" and not btc_went_up)

            if self.alerter:
                outcome = "WIN" if trade_won else "LOSS"
                amt = live_info.get("amount", 0)
                await self.alerter.send_live_settlement_alert(
                    slug=slug,
                    side=live_info["side"],
                    outcome=outcome,
                    amount=amt,
                    entry_price=live_info.get("entry_price", 0),
                )

            if self.alerter:
                await self.alerter.send_stats_summary(
                    stats, live_info=await self._build_live_info()
                )

    async def _settle_live_trades_for_window(
        self, slug: str, btc_went_up: bool
    ) -> None:
        """Settle unsettled live trades matching the given slug."""
        if not self.live_trader:
            return
        unsettled = await self.db.get_unsettled_live_trades()
        for row in unsettled:
            if row["market_slug"] != slug:
                continue
            side = row["side"]
            amount = row["amount_usdc"]
            entry_price = row.get("entry_price") or 0.0

            won = (side == "UP" and btc_went_up) or (side == "DOWN" and not btc_went_up)
            outcome = "WIN" if won else "LOSS"

            # PnL calculation (same as paper trader)
            if entry_price > 0:
                fee_factor = 0.0
                if self.live_trader._fee_rate > 0:
                    from data.polymarket import compute_fee_factor
                    fee_factor = compute_fee_factor(
                        entry_price,
                        self.live_trader._fee_rate,
                        self.live_trader._fee_exponent,
                    )
                shares = (amount / entry_price) * (1.0 - fee_factor)
                pnl = (shares - amount) if won else -amount
            else:
                pnl = -amount if not won else 0.0

            # Update DB
            settled_at = int(time.time() * 1000)
            await self.db.update_live_trade(row["id"], outcome, pnl, settled_at)

            # Update live trader internal state
            self.live_trader.record_settlement(won, pnl)

            logger.info(
                "Settled live trade: %s %s -> %s | pnl=$%+.2f | live bankroll=$%.2f",
                side, slug, outcome, pnl, self.live_trader.bankroll,
            )

    async def _settle_stale_live_trades(self) -> None:
        """Settle stale unsettled live trades from previous sessions."""
        if not self.live_trader:
            return
        unsettled = await self.db.get_unsettled_live_trades()
        current_slug = self._current_slug or self.polymarket.get_current_slug()

        stale = [r for r in unsettled if r["market_slug"] != current_slug]
        if not stale:
            return

        logger.info(
            "Found %d stale unsettled live trade(s) — settling", len(stale)
        )

        for row in stale:
            slug = row["market_slug"]
            try:
                window_ts = int(slug.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                logger.warning("Cannot parse window ts from live trade slug %s — voiding", slug)
                await self.db.update_live_trade(
                    row["id"], "VOID", 0.0, int(time.time() * 1000)
                )
                continue

            window_start_ms = window_ts * 1000
            window_end_ms = (window_ts + 300) * 1000

            btc_start = await self.db.get_btc_price_at(window_start_ms)
            btc_end = await self.db.get_btc_price_at(window_end_ms)

            if btc_start is None or btc_end is None:
                logger.warning(
                    "No candle data for live trade window %s — voiding trade %d",
                    slug, row["id"],
                )
                await self.db.update_live_trade(
                    row["id"], "VOID", 0.0, int(time.time() * 1000)
                )
                continue

            btc_went_up = btc_end >= btc_start
            side = row["side"]
            amount = row["amount_usdc"]
            entry_price = row.get("entry_price") or 0.0
            won = (side == "UP" and btc_went_up) or (side == "DOWN" and not btc_went_up)
            outcome = "WIN" if won else "LOSS"

            if entry_price > 0:
                fee_factor = 0.0
                if self.live_trader._fee_rate > 0:
                    from data.polymarket import compute_fee_factor
                    fee_factor = compute_fee_factor(
                        entry_price,
                        self.live_trader._fee_rate,
                        self.live_trader._fee_exponent,
                    )
                shares = (amount / entry_price) * (1.0 - fee_factor)
                pnl = (shares - amount) if won else -amount
            else:
                pnl = -amount if not won else 0.0

            settled_at = int(time.time() * 1000)
            await self.db.update_live_trade(row["id"], outcome, pnl, settled_at)
            self.live_trader.record_settlement(won, pnl)

            logger.info(
                "Settled stale live trade: %s %s | BTC $%.2f -> $%.2f (%s) | pnl=$%+.2f",
                side, slug, btc_start, btc_end,
                "UP" if btc_went_up else "DOWN", pnl,
            )

    async def _build_live_info(self) -> Optional[dict]:
        """Build live trading info dict for stats summaries."""
        if not self.live_trader or not self.live_trader.is_active:
            return None
        balance = await self.live_trader.get_balance()
        db_stats = await self.db.get_live_trading_stats_full()
        session = self.live_trader.get_session_summary()
        return {
            "balance": balance,
            "db_stats": db_stats,
            "session": session,
            "bankroll": self.live_trader.bankroll,
        }

    async def _delayed_auto_sell(self, slug: str, live_info: dict) -> None:
        """Background task: wait for matching engine, then auto-sell winning tokens.

        Waits 10 seconds before the first attempt to give the matching engine
        time to restart after market resolution. The sell_winning_tokens method
        itself has retry logic with exponential backoff.
        """
        await asyncio.sleep(10)
        logger.info(
            "Auto-selling winning tokens for %s (%s)",
            slug, live_info["side"],
        )
        sell_result = await self.live_trader.sell_winning_tokens(
            token_id=live_info["token_id"],
            market_slug=slug,
            buy_amount_usdc=live_info.get("amount", 0.0),
            buy_price=live_info.get("entry_price", 0.0),
        )
        if sell_result["success"]:
            logger.info("Auto-sell successful — USDC reclaimed for %s", slug)
        else:
            logger.warning(
                "Auto-sell failed for %s: %s — claim on polymarket.com",
                slug, sell_result["error"],
            )
            if self.alerter:
                await self.alerter.send_error_alert(
                    f"Auto-sell failed for {slug} — claim manually on polymarket.com"
                )

    async def _settle_stale_trades(self) -> None:
        """Settle any unsettled trades from previous sessions whose windows have ended.

        On restart, in-memory state is lost and window transitions that
        occurred while the service was down (or during the buffering phase)
        are never detected.  This method pulls unsettled trades from the DB,
        looks up BTC prices from stored candle data, and settles them.
        """
        if not self.paper_trader:
            return

        unsettled = await self.db.get_unsettled_trades()
        current_slug = self._current_slug or self.polymarket.get_current_slug()

        stale = [r for r in unsettled if r["market_slug"] != current_slug]
        if not stale:
            return

        logger.info(
            "Found %d stale unsettled trade(s) from previous session — settling",
            len(stale),
        )

        for row in stale:
            slug = row["market_slug"]

            # Extract window timestamp from slug: btc-updown-5m-{ts}
            try:
                window_ts = int(slug.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                logger.warning("Cannot parse window ts from slug %s — voiding trade", slug)
                await self.db.update_paper_trade(
                    row["id"], "VOID", 0.0, int(time.time() * 1000)
                )
                continue

            window_start_ms = window_ts * 1000
            window_end_ms = (window_ts + 300) * 1000

            btc_start = await self.db.get_btc_price_at(window_start_ms)
            btc_end = await self.db.get_btc_price_at(window_end_ms)

            if btc_start is None or btc_end is None:
                logger.warning(
                    "No candle data for window %s — voiding trade %d",
                    slug,
                    row["id"],
                )
                await self.db.update_paper_trade(
                    row["id"], "VOID", 0.0, int(time.time() * 1000)
                )
                continue

            btc_went_up = btc_end >= btc_start

            # Load into paper_trader and use its settlement logic
            self.paper_trader._pending_trades[slug] = {
                "trade_id": row["id"],
                "side": row["side"],
                "size_usdc": row["size_usdc"],
                "entry_price": row["entry_price"],
                "our_prob": row["our_prob"],
                "market_prob": row["market_prob"],
                "edge": row["edge"],
            }
            await self.paper_trader.settle_trade(slug, btc_went_up)

            logger.info(
                "Settled stale trade: %s %s | BTC $%.2f -> $%.2f (%s)",
                row["side"],
                slug,
                btc_start,
                btc_end,
                "UP" if btc_went_up else "DOWN",
            )

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

                    # Log live stats
                    if self.live_trader and self.live_trader.is_active:
                        ls = await self.db.get_live_trading_stats_full()
                        logger.info(
                            "Live stats: %d settled, %d/%d W/L, WR=%.1f%%, P&L=$%+.2f, bankroll=$%.2f",
                            ls.get("settled", 0), ls.get("wins", 0), ls.get("losses", 0),
                            ls.get("win_rate", 0) * 100, ls.get("total_pnl", 0),
                            self.live_trader.bankroll,
                        )

                    live_info = await self._build_live_info()

                    if self.alerter:
                        await self.alerter.send_stats_summary(
                            stats, live_info=live_info
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in stats loop")

    async def _proxy_watchdog(self) -> None:
        """Monitor SOCKS5 proxy health and auto-pause live trading if it goes down.

        Checks the proxy every 2 minutes by doing a lightweight TCP connect
        to the SOCKS5 port.  When the proxy drops, live_trader is paused and
        a Telegram alert is sent.  When the proxy comes back, live trading
        is resumed automatically.
        """
        import os
        proxy_url = os.environ.get("CLOB_PROXY", settings.clob_proxy)
        if not proxy_url or not self.live_trader:
            return  # No proxy configured or no live trading — nothing to watch

        # Parse host:port from socks5://host:port
        try:
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            proxy_host = parsed.hostname or "127.0.0.1"
            proxy_port = parsed.port or 1080
        except Exception:
            proxy_host, proxy_port = "127.0.0.1", 1080

        proxy_was_down = False
        CHECK_INTERVAL = 120  # 2 minutes

        # Wait for initial buffering to finish before starting checks
        await asyncio.sleep(30)

        while self._running:
            try:
                # Quick TCP connect to check if SOCKS5 port is open
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(proxy_host, proxy_port),
                        timeout=5.0,
                    )
                    writer.close()
                    await writer.wait_closed()
                    proxy_up = True
                except (OSError, asyncio.TimeoutError):
                    proxy_up = False

                if not proxy_up and not proxy_was_down:
                    # Proxy just went down — pause live trading
                    proxy_was_down = True
                    if self.live_trader and not self.live_trader.is_paused:
                        self.live_trader.pause()
                        logger.warning(
                            "PROXY DOWN — live trading auto-paused (SOCKS5 %s:%d unreachable)",
                            proxy_host, proxy_port,
                        )
                        if self.alerter:
                            await self.alerter._send(
                                "\u26a0 <b>PROXY DOWN</b>\n\n"
                                "SOCKS5 proxy unreachable.\n"
                                "Live trading auto-paused.\n"
                                "Paper trading continues.\n\n"
                                "Reconnect the SSH tunnel to resume."
                            )

                elif proxy_up and proxy_was_down:
                    # Proxy recovered — resume live trading
                    proxy_was_down = False
                    if self.live_trader and self.live_trader.is_paused and not self._paused:
                        self.live_trader.resume()
                        logger.info(
                            "PROXY RESTORED — live trading auto-resumed (SOCKS5 %s:%d)",
                            proxy_host, proxy_port,
                        )
                        if self.alerter:
                            await self.alerter._send(
                                "\u2705 <b>PROXY RESTORED</b>\n\n"
                                "SOCKS5 proxy is back.\n"
                                "Live trading auto-resumed."
                            )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in proxy watchdog")

            await asyncio.sleep(CHECK_INTERVAL)


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
