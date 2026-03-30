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
from signals.regime import RegimeDetector
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
            side_selection=settings.side_selection,
        )
        self.regime_detector = RegimeDetector(
            trend_threshold=settings.regime_trend_threshold,
        )
        self.paper_trader = None  # initialized in start()
        self.live_trader = None   # initialized in start() when LIVE_TRADING=true
        self.alerter = None       # initialized in start()

        # State
        self._running = False
        self._paused = False  # when True, analysis loop skips trading
        self._current_regime = None  # RegimeState from last classification
        self._regime_trend_count = 0        # consecutive windows above flip threshold
        self._regime_trend_direction = None  # "UP" or "DOWN" — must be consistent
        self._regime_last_counted_slug = None  # only count once per window
        self._window_btc_start: float | None = None
        self._current_slug: str | None = None
        self._window_start_time: float = 0.0  # unix timestamp of window start (from slug)

        # Trade entry timing — only enter trades in the first N seconds of a
        # 5-minute window.  After this cutoff the market has already priced in
        # the move and any "edge" our model sees is likely stale.
        self._MAX_ENTRY_SECONDS: float = 60.0  # first 60s only — data shows 60-120s entries have 24.5% WR (-$53)

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

        # Track skip reason per window for Telegram notification
        self._window_skip_reason: str | None = None
        self._window_skip_notified: bool = False
        self._window_traded: bool = False
        # Cache data at skip time for enriched skipped_windows recording
        self._window_skip_data: dict | None = None

        # Loss streak guard — pause a side after consecutive same-side losses
        # Only tracks our taker trades (not maker_fills) for accuracy
        self._side_outcomes: dict[str, list[str]] = {"UP": [], "DOWN": []}  # last N outcomes per side
        self._side_paused: dict[str, float] = {}  # side -> pause expiry timestamp

        # Shadow tracking — monitor order books on every window (traded or not)
        # to collect bid spike data for exit probability model training.
        self._shadow_slug: str | None = None
        self._shadow_up_max_bid: float = 0.0
        self._shadow_down_max_bid: float = 0.0
        self._shadow_up_min_bid: float = 1.0
        self._shadow_down_min_bid: float = 1.0
        self._shadow_up_open_ask: float | None = None
        self._shadow_down_open_ask: float | None = None
        self._shadow_btc_start: float | None = None
        self._shadow_poll_count: int = 0
        self._shadow_features: object | None = None  # FeatureVector snapshot

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
    # Pause state persistence
    # ------------------------------------------------------------------
    _PAUSE_STATE_FILE = "pause_state.json"

    def _save_pause_state(self) -> None:
        """Persist pause state to file so it survives service restarts."""
        try:
            from datetime import datetime, timezone as _tz
            state = {
                "paused": self._paused,
                "paused_at": datetime.now(_tz.utc).isoformat() if self._paused else None,
            }
            with open(self._PAUSE_STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception:
            logger.exception("Failed to save pause state")

    def _load_pause_state(self) -> bool:
        """Load pause state from file. Returns True if paused."""
        try:
            with open(self._PAUSE_STATE_FILE, "r") as f:
                state = json.load(f)
            return bool(state.get("paused", False))
        except FileNotFoundError:
            return False
        except Exception:
            logger.exception("Failed to load pause state")
            return False

    def _build_flipped_signal(
        self, original: dict, market, new_side: str
    ) -> dict | None:
        """Build a flipped signal for paper trading during trends.

        Swaps the side to the trend direction, looks up the correct entry
        price from the market order book, and recalculates probabilities.
        Returns None if the flipped entry price is outside the tradeable range.
        """
        from data.polymarket import compute_fee_factor

        if new_side == "UP":
            entry_price = market.up_best_ask if market.up_best_ask else market.up_price
            midpoint = market.up_price
            spread = market.up_spread
            market_prob = market.up_price
        else:
            entry_price = market.down_best_ask if market.down_best_ask else market.down_price
            midpoint = market.down_price
            spread = market.down_spread
            market_prob = market.down_price

        # Entry price filter (same as edge detector)
        if entry_price > 0.65 or entry_price < 0.25:
            return None

        # For flipped trades, set our_prob slightly above market to reflect
        # trend conviction (we're betting on regime, not the ML model).
        our_prob = market_prob + 0.02

        fee_factor = compute_fee_factor(
            entry_price, settings.polymarket_fee_rate, settings.polymarket_fee_exponent
        )
        adj_market = market_prob / (1.0 - fee_factor) if fee_factor < 1.0 else market_prob
        edge = max(our_prob - adj_market, 0.001)

        return {
            "side": new_side,
            "our_prob": our_prob,
            "market_prob": market_prob,
            "edge": edge,
            "entry_price": entry_price,
            "midpoint_price": midpoint,
            "spread": spread,
            "fee_factor": fee_factor,
            "confidence": abs(self._current_regime.strength),
            "signals": original.get("signals", {}),
            "market_slug": original["market_slug"],
            "exploration": entry_price < settings.live_entry_min or entry_price >= settings.live_entry_max,
            "regime_state": original.get("regime_state"),
            "regime_strength": original.get("regime_strength"),
            "regime_flip": True,
        }

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

        # Restore pause state from file (survives service restarts)
        if self._load_pause_state():
            self._paused = True
            logger.warning("PAUSED state restored from file — trading is paused")

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
                    # Prefer real CLOB balance over simulated DB PnL
                    real_balance = await self.live_trader.get_balance()
                    if real_balance is not None and real_balance > 0:
                        self.live_trader.bankroll = real_balance
                        self.live_trader.initial_bankroll = settings.virtual_bankroll
                        self.live_trader._max_bankroll = real_balance
                        logger.info(
                            "Live bankroll from CLOB: $%.2f (deposited ~$%.2f)",
                            real_balance, settings.virtual_bankroll,
                        )
                    else:
                        # Fallback to DB-based restoration
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
            asyncio.create_task(self._early_exit_loop(), name="early_exit"),
            asyncio.create_task(self._stats_loop(), name="stats"),
            asyncio.create_task(
                self.alerter.run_command_listener(), name="telegram_cmds"
            ),
            asyncio.create_task(self._proxy_watchdog(), name="proxy_watchdog"),
            asyncio.create_task(self._shadow_book_loop(), name="shadow_book"),
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
        self.alerter.register_command("recent", self._cmd_recent)
        self.alerter.register_command("today", self._cmd_today)
        self.alerter.register_command("pause", self._cmd_pause)
        self.alerter.register_command("resume", self._cmd_resume)
        self.alerter.register_command("weights", self._cmd_weights)
        self.alerter.register_command("regime", self._cmd_regime)
        self.alerter.register_command("ee", self._cmd_ee)
        self.alerter.register_command("analyze", self._cmd_analyze)
        self.alerter.register_command("spread", self._cmd_spread)

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
                if self._use_ml and hasattr(self.model, "predict_from_candles"):
                    window_ts = int(self._window_start_time) if self._window_start_time else int(time.time())
                    p_up = self.model.predict_from_candles(
                        self.binance.get_candles(n=50), window_ts
                    )
                else:
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
        """Handle /stats — comprehensive trading stats (live primary)."""
        parts = []

        # Live stats (primary)
        if self.live_trader and self.live_trader.is_active:
            ls = await self.db.get_live_trading_stats_full()
            balance = await self.live_trader.get_balance()
            session = self.live_trader.get_session_summary()
            ee_str = f" + {ls.get('early_exits', 0)}ee" if ls.get("early_exits") else ""
            mk_str = f" + {ls.get('maker_fills', 0)}mk" if ls.get("maker_fills") else ""

            # Portfolio: real CLOB balance vs total deposited = ground truth PnL
            deposited = settings.total_deposited
            portfolio_lines = ""
            if balance is not None:
                real_pnl = balance - deposited
                portfolio_lines = (
                    f"Balance: <b>${balance:.2f}</b>\n"
                    f"Deposited: ${deposited:.2f}\n"
                    f"All-time PnL: <b>${real_pnl:+.2f}</b> ({real_pnl/deposited:+.1%})\n"
                )
            else:
                portfolio_lines = f"Balance: unavailable\nDeposited: ${deposited:.2f}\n"

            # Effective WR: (W + EE) / (W + L + EE) — treats EE as wins
            total_settled = ls.get('wins', 0) + ls.get('losses', 0) + ls.get('early_exits', 0)
            eff_wr = (ls.get('wins', 0) + ls.get('early_exits', 0)) / total_settled if total_settled > 0 else 0.0

            parts.append(
                f"\U0001f4b5 <b>PORTFOLIO</b>\n"
                f"{portfolio_lines}"
                f"\n<b>Live Trades</b>\n"
                f"Settled: {ls.get('settled', 0)} | "
                f"W/L: {ls.get('wins', 0)}/{ls.get('losses', 0)}{ee_str}{mk_str}\n"
                f"Effective WR: <b>{eff_wr:.1%}</b> | "
                f"Settlement WR: {ls.get('win_rate', 0):.1%}\n"
                f"DB PnL: ${ls.get('total_pnl', 0):+.2f} "
                f"(EE: ${ls.get('early_exit_pnl', 0):+.2f})\n"
                f"Volume: ${ls.get('total_amount', 0):,.2f} | "
                f"Max bet: ${settings.max_live_bet_usdc:.2f}\n"
                f"Session: {session.get('successful', 0)} filled / "
                f"{session.get('total', 0)} total"
            )

        # Paper stats (secondary — one compact line)
        if self.paper_trader:
            stats = await self.paper_trader.get_stats()
            parts.append(
                f"\n\U0001f4dd Paper: "
                f"{stats.get('wins', 0)}W/{stats.get('losses', 0)}L "
                f"({stats.get('win_rate', 0):.0%}) "
                f"${stats.get('total_pnl', 0):+.2f}"
            )

        if not parts:
            return "No trading data."
        return f"\U0001f4c8 <b>STATS</b>\n\n" + "\n\n".join(parts)

    async def _cmd_trades(self, args: str = "") -> str:
        """Handle /trades — list pending live positions and recent settlements."""
        # Pending live positions (in-memory, this session)
        pending = self._live_trade_tokens
        unsettled = await self.db.get_unsettled_live_trades()

        if not pending and not unsettled:
            db_stats = await self.db.get_live_trading_stats_full()
            return (
                f"\U0001f4dd <b>TRADES</b>\n\n"
                f"No pending positions.\n"
                f"Settled: {db_stats.get('settled', 0)} | "
                f"W/L: {db_stats.get('wins', 0)}/{db_stats.get('losses', 0)}"
            )

        lines = [f"\U0001f4dd <b>PENDING POSITIONS</b>\n"]
        from strategy.live_trader import LiveTrader
        for slug, info in pending.items():
            status = ""
            if info.get("exited"):
                status = " [EXITED]"
            entry = info.get('entry_price', 0)
            threshold = LiveTrader.get_exit_threshold(entry, trade_tag=info.get("trade_tag"))
            lines.append(
                f"  {info['side']} {slug}{status}\n"
                f"  Entry: {entry:.3f} | "
                f"EE: ${threshold:.2f} | "
                f"Size: ${info.get('amount', 0):.2f} | "
                f"Tokens: {info.get('tokens', 0):.1f}"
            )
        # Show DB unsettled trades not in memory (from previous sessions)
        memory_slugs = set(pending.keys())
        db_only = [r for r in unsettled if r["market_slug"] not in memory_slugs]
        if db_only:
            lines.append(f"\n<b>Unsettled (prior sessions):</b>")
            for r in db_only[:5]:
                lines.append(
                    f"  {r['side']} {r['market_slug']}\n"
                    f"  Entry: {r.get('entry_price', 0):.3f} | "
                    f"Size: ${r['amount_usdc']:.2f}"
                )
            if len(db_only) > 5:
                lines.append(f"  ... and {len(db_only) - 5} more")
        return "\n".join(lines)

    async def _cmd_recent(self, args: str = "") -> str:
        """Handle /recent [N] — show last N settled live trades."""
        n = 5
        if args.strip():
            try:
                n = min(max(int(args.strip()), 1), 10)
            except ValueError:
                return "Usage: /recent [1-10]"

        trades = await self.db.get_recent_live_trades(n)
        if not trades:
            return "\U0001f4dd No settled live trades yet."

        lines = [f"\U0001f4dd <b>LAST {len(trades)} TRADES</b>\n"]
        now_ms = int(time.time() * 1000)
        for t in trades:
            outcome = t.get("outcome", "?")
            icon = "\u2705" if outcome == "WIN" else ("\u274c" if outcome == "LOSS" else "\U0001f4b0")
            pnl = t.get("pnl", 0) or 0
            entry = t.get("entry_price", 0) or 0
            side = t.get("side", "?")
            ago_min = (now_ms - t["timestamp"]) / 60000
            if ago_min < 60:
                ago_str = f"{ago_min:.0f}m ago"
            elif ago_min < 1440:
                ago_str = f"{ago_min / 60:.1f}h ago"
            else:
                ago_str = f"{ago_min / 1440:.1f}d ago"
            lines.append(
                f"{icon} {side} @ {entry:.3f} | "
                f"${pnl:+.2f} | {ago_str}"
            )
        return "\n".join(lines)

    async def _cmd_today(self, args: str = "") -> str:
        """Handle /today — today's trading performance (UTC)."""
        trades = await self.db.get_today_live_trades()
        if not trades:
            return "\U0001f4c5 No live trades today (UTC)."

        wins = sum(1 for t in trades if t["outcome"] == "WIN")
        losses = sum(1 for t in trades if t["outcome"] == "LOSS")
        early_exits = sum(1 for t in trades if t["outcome"] == "EARLY_EXIT")
        total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)

        # Best and worst trade
        best = max(trades, key=lambda t: t.get("pnl", 0) or 0)
        worst = min(trades, key=lambda t: t.get("pnl", 0) or 0)

        wl_total = wins + losses
        wr = (wins / wl_total * 100) if wl_total > 0 else 0

        ee_str = f" + {early_exits}ee" if early_exits else ""
        lines = [
            f"\U0001f4c5 <b>TODAY (UTC)</b>\n",
            f"Trades: {len(trades)} | W/L: {wins}/{losses}{ee_str} ({wr:.0f}%)",
            f"P&amp;L: <b>${total_pnl:+.2f}</b>",
            f"Best: ${best.get('pnl', 0) or 0:+.2f} ({best['side']} @ {best.get('entry_price', 0) or 0:.3f})",
            f"Worst: ${worst.get('pnl', 0) or 0:+.2f} ({worst['side']} @ {worst.get('entry_price', 0) or 0:.3f})",
        ]
        return "\n".join(lines)

    async def _cmd_pause(self, args: str = "") -> str:
        """Handle /pause — stop placing new trades (data collection continues)."""
        if self._paused:
            return "\u23f8 Already paused. Use /resume to restart trading."
        self._paused = True
        self._save_pause_state()
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
        self._save_pause_state()
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
            from strategy.live_trader import LiveTrader
            ee_lines = (
                f"\n\n<b>Early Exit Thresholds:</b>\n"
                f"  &lt;$0.35  exit @ ${settings.early_exit_threshold_low:.2f} (paper-only)\n"
                f"  $0.35-0.40  exit @ ${settings.early_exit_threshold_low_mid:.2f} (paper-only)\n"
                f"  $0.40-0.50  exit @ ${settings.early_exit_threshold_mid:.2f}\n"
                f"  &gt;=$0.50  exit @ ${settings.early_exit_threshold_high:.2f}"
            )
            # Show active positions with their EE status
            if self._live_trade_tokens:
                ee_lines += f"\n\n<b>Active EE monitoring:</b>"
                for slug, info in self._live_trade_tokens.items():
                    if info.get("exited"):
                        continue
                    entry = info.get("entry_price", 0)
                    threshold = LiveTrader.get_exit_threshold(entry, trade_tag=info.get("trade_tag"))
                    ee_lines += (
                        f"\n  {info['side']} @ ${entry:.3f}"
                        f" -> exits @ ${threshold:.2f}"
                    )
            return (
                "\u2696 <b>ML MODEL</b>\n\n"
                f"Type: {type(self.model._model).__name__}\n"
                f"Features: 44\n"
                f"Mode: {'always-trade' if settings.always_trade else 'edge-threshold'}\n"
                f"Sizing: {settings.sizing_strategy}\n"
                f"Dampening: {settings.confidence_dampen}\n"
                f"Min confidence: {settings.min_confidence}"
                f"{ee_lines}"
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
        """Handle /regime — show full 5-component regime detection breakdown."""
        candles = self.binance.get_candles(n=50)
        if len(candles) < 30:
            return "\u26a0 Need at least 30 candles for regime calculation."

        from signals.indicators import TechnicalIndicators

        regime = self.regime_detector.classify(candles)

        # Visual bar: map strength [-1, 1] to position [0, 20]
        bar_pos = int((regime.strength + 1.0) * 10)
        bar_pos = max(0, min(20, bar_pos))
        bar = "\u2591" * bar_pos + "\u2588" + "\u2591" * (20 - bar_pos)

        labels = {
            "trending_up": "TRENDING UP",
            "trending_down": "TRENDING DOWN",
            "ranging": "RANGING",
        }
        label = labels.get(regime.regime, regime.regime.upper())

        # Direction consistency detail
        recent = candles[-20:]
        ups = sum(1 for c in recent if c.close > c.open)
        downs = len(recent) - ups
        dir_detail = f"{max(ups, downs)}/{len(recent)} {'UP' if ups > downs else 'DOWN'}"

        # Price vs EMA detail
        closes = [c.close for c in candles]
        ema21 = TechnicalIndicators.ema(closes, 21)
        recent10 = candles[-10:]
        above = sum(1 for c in recent10 if c.close > ema21)
        pve_detail = f"{above}/10 {'above' if above >= 5 else 'below'}"

        # Momentum detail
        mom10 = TechnicalIndicators.momentum(candles, lookback=10)
        mom20 = TechnicalIndicators.momentum(candles, lookback=20)
        mom_agree = "agree" if (mom10 > 0) == (mom20 > 0) else "disagree"

        btc = self.binance.get_latest_price()
        ema9 = TechnicalIndicators.ema(closes, 9)

        return (
            f"\U0001f30d <b>MARKET REGIME</b>\n\n"
            f"Regime: <b>{label}</b> (strength: {regime.strength:+.2f})\n"
            f"[{bar}]\n"
            f"  BEAR {'&lt;' * 10} {'&gt;' * 10} BULL\n\n"
            f"<b>Components:</b>\n"
            f"  Direction (20c):  {dir_detail} ({regime.direction_pct:+.2f})\n"
            f"  Momentum 10/20m:  {mom_agree} ({regime.momentum_score:+.2f})\n"
            f"  EMA-21 slope:     {regime.ema_slope:+.2f}\n"
            f"  Price vs EMA:     {pve_detail} ({regime.price_vs_ema:+.2f})\n"
            f"  EMA cross:        {regime.ema_cross:+.2f}\n\n"
            f"BTC: ${btc:,.2f} | EMA9: ${ema9:,.0f} | EMA21: ${ema21:,.0f}\n"
            f"Threshold: {self.regime_detector.trend_threshold:.2f} | "
            f"Flip: {settings.regime_flip_threshold:.2f}\n"
            f"Trend count: {self._regime_trend_count}/{settings.regime_flip_confirm_windows}"
            f"{' -- CONFIRMED' if self._regime_trend_count >= settings.regime_flip_confirm_windows else ' -- not yet'}"
        )

    async def _cmd_ee(self, args: str = "") -> str:
        """Handle /ee [1h|24h] — early exit tier performance."""
        import time as _time

        # Parse time filter
        since_ms = None
        label = "ALL TIME"
        arg = args.strip().lower()
        if arg in ("1h", "1hr"):
            since_ms = int((_time.time() - 3600) * 1000)
            label = "LAST 1H"
        elif arg in ("24h", "24hr", "1d"):
            since_ms = int((_time.time() - 86400) * 1000)
            label = "LAST 24H"
        elif arg:
            return "\u26a0 Usage: /ee [1h|24h]"

        rows = await self.db.get_early_exit_tier_stats(since_ms=since_ms)
        if not rows:
            return f"\u26a0 No early exit data ({label})."

        # Group by tier
        tier_defs = [
            ("<0.35", 0.0, 0.35),
            ("0.35-0.40", 0.35, 0.40),
            ("0.40-0.50", 0.40, 0.50),
            (">=0.50", 0.50, 999.0),
        ]
        tiers: dict[str, list[dict]] = {name: [] for name, _, _ in tier_defs}
        for r in rows:
            ep = r.get("entry_price") or 0
            for name, lo, hi in tier_defs:
                if lo <= ep < hi:
                    tiers[name].append(r)
                    break

        lines = [f"<b>EARLY EXIT TIERS ({label})</b>\n"]

        for name, _, _ in tier_defs:
            trades = tiers[name]
            if not trades:
                continue
            n = len(trades)
            rescues = sum(1 for t in trades if not t["would_have_won"])
            regrets = n - rescues
            ee_pnl = sum(t["pnl"] or 0 for t in trades)

            # Hypothetical settlement P&L
            settle_pnl = 0.0
            for t in trades:
                ep = t["entry_price"]
                amt = t["amount_usdc"]
                if not ep or ep <= 0:
                    continue
                if t["would_have_won"]:
                    settle_pnl += (amt / ep) - amt
                else:
                    settle_pnl -= amt

            advantage = ee_pnl - settle_pnl
            thresholds = sorted(set(
                t["exit_threshold_used"] for t in trades if t["exit_threshold_used"]
            ))
            th_str = "/".join(f"{t:.2f}" for t in thresholds)

            sign = "+" if advantage >= 0 else ""
            lines.append(
                f"<b>{name}</b> (th={th_str}): {n} trades\n"
                f"  Rescue: {rescues} ({rescues*100//n}%) | "
                f"Regret: {regrets} ({regrets*100//n}%)\n"
                f"  EE: ${ee_pnl:+.2f} | Settle: ${settle_pnl:+.2f} | "
                f"Adv: <b>${sign}{advantage:.2f}</b>"
            )

        return "\n".join(lines)

    async def _cmd_analyze(self, args: str = "") -> str:
        """Handle /analyze — run trade analysis on live trades (incl. maker fills)."""
        try:
            import sqlite3 as _sqlite3
            from collections import defaultdict as _defaultdict
            from datetime import datetime, timezone

            conn = _sqlite3.connect(self.db.db_path)
            conn.row_factory = _sqlite3.Row
            trades = [dict(r) for r in conn.execute(
                "SELECT * FROM live_trades "
                "WHERE success = 1 AND outcome IS NOT NULL "
                "ORDER BY timestamp"
            ).fetchall()]
            conn.close()

            if not trades:
                return "\u26a0 No settled live trades to analyze."

            # Split by type
            taker_trades = [t for t in trades if t.get("trade_tag") != "maker_fill"]
            maker_trades = [t for t in trades if t.get("trade_tag") == "maker_fill"]
            early_exits = [t for t in trades if t["outcome"] == "EARLY_EXIT"]
            settled = [t for t in trades if t["outcome"] in ("WIN", "LOSS")]

            total = len(settled)
            wins = sum(1 for t in settled if t["outcome"] == "WIN")
            losses = total - wins
            total_pnl = sum(t["pnl"] or 0 for t in trades)
            avg_pnl = total_pnl / len(trades) if trades else 0

            # Side analysis
            side_lines = []
            for side in ("UP", "DOWN"):
                subset = [t for t in settled if t["side"] == side]
                if not subset:
                    continue
                swins = sum(1 for t in subset if t["outcome"] == "WIN")
                swr = swins / len(subset) * 100
                spnl = sum(t["pnl"] or 0 for t in subset)
                side_lines.append(
                    f"  {side}: {swr:.0f}% ({swins}/{len(subset)}) ${spnl:+.2f}"
                )

            # Entry price bucket analysis
            price_buckets = [
                ("0.25-0.35", 0.25, 0.35), ("0.35-0.45", 0.35, 0.45),
                ("0.45-0.55", 0.45, 0.55), ("0.55-0.65", 0.55, 0.65),
            ]
            price_lines = []
            for label, lo, hi in price_buckets:
                subset = [t for t in settled if t.get("entry_price") and lo <= t["entry_price"] < hi]
                if not subset:
                    continue
                bwins = sum(1 for t in subset if t["outcome"] == "WIN")
                bwr = bwins / len(subset) * 100
                bpnl = sum(t["pnl"] or 0 for t in subset)
                price_lines.append(
                    f"  {label}: {bwr:.0f}% ({bwins}/{len(subset)}) ${bpnl:+.2f}"
                )

            # Hour analysis
            hour_stats: dict[int, list[bool]] = _defaultdict(list)
            for t in settled:
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

            # Taker vs maker breakdown
            taker_pnl = sum(t["pnl"] or 0 for t in taker_trades)
            maker_pnl = sum(t["pnl"] or 0 for t in maker_trades)
            early_pnl = sum(t["pnl"] or 0 for t in early_exits)

            text = (
                f"\U0001f4ca <b>LIVE TRADE ANALYSIS</b>\n\n"
                f"<b>Overall:</b>\n"
                f"  Trades: {total} W/L | W/L: {wins}/{losses}\n"
                f"  Win rate: {wins/total*100:.1f}%\n"
                f"  P&amp;L: ${total_pnl:+.2f}\n\n"
                f"<b>By source:</b>\n"
                f"  Taker (our orders): {len(taker_trades)} | ${taker_pnl:+.2f}\n"
                f"  Maker (others hit us): {len(maker_trades)} | ${maker_pnl:+.2f}\n"
                f"  Early exits: {len(early_exits)} | ${early_pnl:+.2f}\n\n"
                f"<b>By side:</b>\n"
                + "\n".join(side_lines)
            )
            if price_lines:
                text += "\n\n<b>By entry price:</b>\n" + "\n".join(price_lines)
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

        # Sync maker fills from CLOB on startup
        if self.live_trader and self.live_trader.is_active:
            await self._sync_maker_fills()

        while self._running:
            try:
                await self._run_one_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in analysis cycle")
            await asyncio.sleep(3)  # poll every 3 seconds

    async def _early_exit_loop(self) -> None:
        """Dedicated 1-second loop for early exit monitoring.

        Runs independently of the main 3-second analysis loop so we can
        react faster to fleeting bid spikes throughout the window.
        """
        while self._running:
            try:
                await self._monitor_early_exit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in early exit loop")
            await asyncio.sleep(1)

    async def _shadow_book_loop(self) -> None:
        """Background loop that polls order books every 10s on ALL windows.

        Tracks max/min bids for both UP and DOWN tokens regardless of whether
        we trade.  At window transition, saves one summary row to shadow_windows.
        This data feeds the future exit probability model.
        """
        logger.info("Shadow book tracking started")
        while self._running:
            try:
                market = self.polymarket._current_market
                if market is None:
                    await asyncio.sleep(10)
                    continue

                slug = market.slug
                up_token = market.up_token_id
                down_token = market.down_token_id

                # --- Window transition: save previous, reset state ---
                if slug != self._shadow_slug:
                    if self._shadow_slug and self._shadow_poll_count > 0:
                        await self._save_shadow_data()
                    # Reset for new window
                    self._shadow_slug = slug
                    self._shadow_up_max_bid = 0.0
                    self._shadow_down_max_bid = 0.0
                    self._shadow_up_min_bid = 1.0
                    self._shadow_down_min_bid = 1.0
                    self._shadow_poll_count = 0
                    self._shadow_btc_start = self.binance.get_latest_price()
                    self._shadow_features = None
                    # Capture open asks (entry prices at window start)
                    self._shadow_up_open_ask = market.up_price
                    self._shadow_down_open_ask = market.down_price
                    # Snapshot Binance features once at window open
                    candles = self.binance.get_candles(n=50)
                    if len(candles) >= 5:
                        self._shadow_features = self.features.compute_features(
                            candles=candles,
                            orderbook=self.binance.orderbook,
                            trades=list(self.binance.recent_trades),
                            funding=self.binance.funding,
                        )

                # --- Poll both order books ---
                up_book = await self.polymarket.get_orderbook(up_token)
                down_book = await self.polymarket.get_orderbook(down_token)

                if up_book and up_book.get("bids"):
                    best_bid = float(up_book["bids"][-1]["price"])
                    self._shadow_up_max_bid = max(self._shadow_up_max_bid, best_bid)
                    self._shadow_up_min_bid = min(self._shadow_up_min_bid, best_bid)

                if down_book and down_book.get("bids"):
                    best_bid = float(down_book["bids"][-1]["price"])
                    self._shadow_down_max_bid = max(self._shadow_down_max_bid, best_bid)
                    self._shadow_down_min_bid = min(self._shadow_down_min_bid, best_bid)

                self._shadow_poll_count += 1

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in shadow book loop")
            await asyncio.sleep(10)

    async def _save_shadow_data(self) -> None:
        """Persist accumulated shadow tracking data for the completed window."""
        btc_end = self.binance.get_latest_price()
        regime = self._current_regime

        # What did we trade this window (if anything)?
        traded_side = None
        traded_tag = None
        if self._shadow_slug in self._live_trade_tokens:
            info = self._live_trade_tokens[self._shadow_slug]
            traded_side = info.get("side")
            traded_tag = info.get("trade_tag")

        fv = self._shadow_features
        await self.db.save_shadow_window(
            market_slug=self._shadow_slug,
            up_open_ask=self._shadow_up_open_ask,
            down_open_ask=self._shadow_down_open_ask,
            up_max_bid=self._shadow_up_max_bid,
            down_max_bid=self._shadow_down_max_bid,
            up_min_bid=self._shadow_up_min_bid if self._shadow_up_min_bid < 1.0 else None,
            down_min_bid=self._shadow_down_min_bid if self._shadow_down_min_bid < 1.0 else None,
            btc_price_start=self._shadow_btc_start,
            btc_price_end=btc_end,
            regime_state=regime.regime if regime else None,
            regime_strength=regime.strength if regime else None,
            traded_side=traded_side,
            traded_tag=traded_tag,
            poll_count=self._shadow_poll_count,
            entry_obi=fv.obi if fv else None,
            entry_taker_ratio=fv.taker_ratio if fv else None,
            entry_momentum_1m=fv.momentum_1m if fv else None,
            entry_momentum_5m=fv.momentum_5m if fv else None,
            entry_rsi=fv.rsi if fv else None,
            entry_vwap_dev=fv.vwap_deviation if fv else None,
            entry_bb_position=fv.bb_position if fv else None,
            entry_ema_cross=fv.ema_cross if fv else None,
            entry_funding_zscore=fv.funding_rate if fv else None,
            entry_volume_zscore=fv.volume_zscore if fv else None,
            entry_atr=fv.atr if fv else None,
        )

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
            self._window_skip_reason = None
            self._window_skip_notified = False
            self._window_traded = False
            self._window_skip_data = None
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

        # 0b. Retry _window_btc_start if it was None at init (e.g. after restart)
        if self._window_btc_start is None:
            price = self.polymarket.get_chainlink_stream_price()
            if price is None:
                price = self.binance.get_latest_price()
            if price is not None:
                self._window_btc_start = price
                logger.info("Late BTC start price: $%.2f", price)

        # 1. Discover current Polymarket market
        market = await self.polymarket.discover_market()
        if market is None:
            return

        # 2. Refresh live Polymarket prices + order books
        up_book = down_book = None
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
            up_bid_size=up_book.bid_size if up_book else None,
            up_ask_size=up_book.ask_size if up_book else None,
            down_bid_size=down_book.bid_size if down_book else None,
            down_ask_size=down_book.ask_size if down_book else None,
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

        # Apply confidence dampening to shrink P(up) toward 0.5.
        # Counters known ML model overconfidence (~10-20%).
        if settings.confidence_dampen < 1.0:
            p_up = 0.5 + settings.confidence_dampen * (p_up - 0.5)

        # Save feature snapshot
        await self.db.save_feature_snapshot(
            features=feature_vec,
            prob_up=p_up,
            market_slug=market.slug,
        )

        # 4b. Compute market regime
        self._current_regime = self.regime_detector.classify(candles)

        # 4c. Track consecutive trending windows for flip confirmation.
        #   A real trend sustains 3+ windows (15 min).  Brief flickers
        #   spike for 1-2 windows then drop back — these should NOT flip.
        #   Only count once per 5-min window (not every 3s loop iteration).
        current_slug = self._current_slug
        if current_slug and current_slug != self._regime_last_counted_slug:
            self._regime_last_counted_slug = current_slug
            if (self._current_regime.regime != "ranging"
                    and abs(self._current_regime.strength) >= settings.regime_flip_threshold):
                new_dir = "UP" if self._current_regime.strength > 0 else "DOWN"
                if new_dir == self._regime_trend_direction:
                    self._regime_trend_count += 1
                else:
                    self._regime_trend_direction = new_dir
                    self._regime_trend_count = 1
                logger.info(
                    "REGIME trending %s (count=%d/%d, str=%.2f)%s",
                    new_dir, self._regime_trend_count,
                    settings.regime_flip_confirm_windows,
                    self._current_regime.strength,
                    "" if self._regime_trend_count >= settings.regime_flip_confirm_windows
                    else " -- waiting for confirmation",
                )
            else:
                if self._regime_trend_count > 0:
                    logger.info(
                        "REGIME trend broke (was %s x%d, now str=%.2f) -- reset",
                        self._regime_trend_direction, self._regime_trend_count,
                        self._current_regime.strength,
                    )
                self._regime_trend_count = 0
                self._regime_trend_direction = None

        # 5. Detect edge (pass p_up_override so edge detector uses ML prediction)
        self._cycle_count += 1
        signal = self.edge_detector.evaluate(
            feature_vec, market, p_up_override=p_up
        )

        if signal is None:
            # Capture skip reason from edge detector
            if not self._window_skip_reason and self.edge_detector.last_skip_reason:
                self._window_skip_reason = self.edge_detector.last_skip_reason
                # Cache data at skip time (pre-edge: no signal, but p_up + features available)
                if not self._window_skip_data:
                    _model_side = "UP" if p_up > 0.5 else "DOWN"
                    self._window_skip_data = {
                        "model_confidence": abs(p_up - 0.5),
                        "entry_price": market.up_best_ask if _model_side == "UP" else market.down_best_ask,
                        "model_side": _model_side,
                        "feature_vec": feature_vec,
                    }
            # Real-time skip notification — tell user immediately when window is skipped
            if self._window_skip_reason and not self._window_skip_notified and not self._window_traded and self.alerter:
                self._window_skip_notified = True
                btc_now_rt = self.binance.get_latest_price()
                btc_str = f" | BTC: ${btc_now_rt:,.2f}" if btc_now_rt else ""
                try:
                    await self.alerter._send(
                        f"SKIP {market.slug}\n"
                        f"Reason: {self._window_skip_reason}\n"
                        f"P(up): {p_up:.1%}{btc_str}",
                        parse_mode=None,
                    )
                except Exception:
                    logger.exception("Failed to send real-time skip notification")
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
                regime_str = ""
                if self._current_regime:
                    r = self._current_regime
                    regime_labels = {"trending_up": "TREND_UP", "trending_down": "TREND_DN", "ranging": "RANGE"}
                    regime_str = f" | regime={regime_labels.get(r.regime, r.regime)}({r.strength:+.2f})"
                logger.info(
                    "-- Status %s: BTC $%s | P(up)=%.1f%% | Mkt=%.0f/%.0f%s%s%s",
                    model_tag,
                    f"{btc_now:,.2f}" if btc_now else "N/A",
                    p_up * 100,
                    market.up_price * 100,
                    market.down_price * 100,
                    spread_str,
                    regime_str,
                    stats_str,
                )
            return

        # 6. Edge found — apply safety filters before trading.

        # Helper: cache signal data when skipping a window (post-edge)
        def _cache_skip_data() -> None:
            if self._window_skip_data:
                return
            self._window_skip_data = {
                "model_confidence": signal.get("confidence"),
                "entry_price": signal.get("entry_price"),
                "model_side": signal.get("side"),
                "feature_vec": feature_vec,
            }

        # 6a. Skip if trading is paused.
        if self._paused:
            self._window_skip_reason = "paused"
            _cache_skip_data()
            return

        # 6b. Skip if we already have a pending trade on this market.
        if (
            self.paper_trader
            and market.slug in self.paper_trader._pending_trades
        ):
            return
        if market.slug in self._live_trade_tokens:
            return

        btc_now = self.binance.get_latest_price()

        # 6c. Time gate — only enter trades in the first portion of the window.
        #     After the cutoff the market has already priced in the move.
        seconds_in_window = time.time() - self._window_start_time
        if seconds_in_window > self._MAX_ENTRY_SECONDS:
            if not self._window_skip_reason:
                self._window_skip_reason = "time gate (>60s)"
                _cache_skip_data()
            return

        # 6d. Hour blacklist — skip hours with historically poor performance.
        from datetime import datetime, timezone as _tz
        current_hour = datetime.now(_tz.utc).hour
        if current_hour in self._blacklist_hours:
            self._window_skip_reason = f"blacklisted hour ({current_hour:02d}:00 UTC)"
            _cache_skip_data()
            return

        # 6d2. Regime flip — when a strong trend is detected, flip the signal
        #   to bet WITH the trend instead of the model's counter-trend prediction.
        #   Applies to paper always; live when regime_flip_live=True.
        #   Disabled in "cheaper" side_selection mode — always buy the cheaper side.
        #   Must run BEFORE streak guard and trend filter so they check the
        #   post-flip side (the side we'll actually trade).
        flip_signal = None
        if (settings.side_selection != "cheaper"
                and self._current_regime
                and self._current_regime.regime != "ranging"
                and abs(self._current_regime.strength) >= settings.regime_flip_threshold
                and self._regime_trend_count >= settings.regime_flip_confirm_windows):
            trend_side = self.regime_detector.get_trend_side(self._current_regime)
            if trend_side and trend_side != signal["side"]:
                flipped = self._build_flipped_signal(signal, market, trend_side)
                if flipped:
                    flip_signal = flipped
                    logger.info(
                        "REGIME FLIP: %s -> %s (regime=%s, str=%.2f, live=%s)",
                        signal["side"], trend_side,
                        self._current_regime.regime,
                        self._current_regime.strength,
                        "yes" if settings.regime_flip_live else "paper-only",
                    )

        # 6d3. Trend-conflict filter — don't bet against a strong intra-window
        #     price move.  Uses the post-flip side so regime-flipped trades
        #     (which align with the trend) are not incorrectly blocked.
        effective_side = signal["side"]
        if flip_signal and settings.regime_flip_live:
            effective_side = flip_signal["side"]
        if self._window_btc_start and btc_now:
            window_move_pct = (btc_now - self._window_btc_start) / self._window_btc_start * 100
            btc_trending_up = window_move_pct > self._TREND_CONFLICT_PCT
            btc_trending_down = window_move_pct < -self._TREND_CONFLICT_PCT
            if (effective_side == "UP" and btc_trending_down) or \
               (effective_side == "DOWN" and btc_trending_up):
                self._window_skip_reason = (
                    f"trend conflict ({effective_side} vs BTC {window_move_pct:+.2f}%)"
                )
                logger.info("Skipping edge — %s", self._window_skip_reason)
                _cache_skip_data()
                return

        # 6d4. Loss streak guard — pause a side after consecutive losses.
        #      Uses post-flip side so the guard protects the actually-traded
        #      direction, not the model's original prediction.
        trade_side = effective_side
        if trade_side in self._side_paused:
            if time.time() < self._side_paused[trade_side]:
                self._window_skip_reason = (
                    f"streak guard ({settings.streak_pause_threshold}x {trade_side} LOSS)"
                )
                logger.info("Skipping edge — %s", self._window_skip_reason)
                _cache_skip_data()
                return
            else:
                # Pause expired — clear it, tag next trade
                del self._side_paused[trade_side]
                logger.info("Streak guard expired for %s — resuming", trade_side)
                signal["_post_streak"] = True

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

        # Attach regime info to signal dict for downstream use
        signal["regime_state"] = self._current_regime.regime if self._current_regime else None
        signal["regime_strength"] = self._current_regime.strength if self._current_regime else None

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
        flip_tag = " [FLIP]" if flip_signal else ""
        explore_tag = " [EXPLORE]" if signal.get("exploration") else ""
        model_tag = ("ML" if self._use_ml else "RB") + flip_tag + explore_tag
        spread_val = signal.get("spread")
        midpoint_val = signal.get("midpoint_price")
        spread_info = ""
        if spread_val is not None and midpoint_val is not None:
            spread_info = f" spread={spread_val:.4f} mid={midpoint_val:.3f}"
        regime_tag = ""
        if self._current_regime:
            r = self._current_regime
            regime_labels = {"trending_up": "TREND_UP", "trending_down": "TREND_DN", "ranging": "RANGE"}
            regime_tag = f" regime={regime_labels.get(r.regime, r.regime)}({r.strength:+.2f})"
        logger.info(
            ">>> %s %s %s | our=%.1f%% mkt=%.1f%% edge=%+.1f%% "
            "conf=%.1f%%%s fee=%.2f%%%s | BTC=$%s | [%s]",
            model_tag,
            signal["side"],
            signal["market_slug"],
            signal["our_prob"] * 100,
            signal["market_prob"] * 100,
            signal["edge"] * 100,
            conf_pct,
            regime_tag,
            fee_pct,
            spread_info,
            f"{btc_now:,.2f}" if btc_now else "N/A",
            top_signals,
        )

        self._window_traded = True

        # Paper trade (no Telegram — paper stats kept in DB/logs only)
        # Use flipped signal during strong trends, otherwise use model's signal
        if self.paper_trader:
            paper_signal = flip_signal if flip_signal else signal
            if settings.side_selection == "cheaper":
                paper_signal = {**paper_signal, "trade_tag_override": "cheap_side"}
            elif paper_signal.get("exploration"):
                paper_signal = {**paper_signal, "size_override": 1.00}
            elif paper_signal.get("regime_flip"):
                paper_signal = {**paper_signal, "trade_tag_override": "regime_flip"}
            await self.paper_trader.place_trade(paper_signal)

        # Live trade — place real order on Polymarket
        # Live range: LIVE_ENTRY_MIN to LIVE_ENTRY_MAX. Outside = paper-only exploration.
        live_signal = signal  # default: model's signal
        if flip_signal and settings.regime_flip_live:
            # Only allow flips within the live entry range.
            flip_entry = flip_signal.get("entry_price", 1.0)
            if settings.live_entry_min <= flip_entry < settings.live_entry_max:
                # Ensure exploration flag is cleared for flips in live range
                live_signal = {**flip_signal, "exploration": False}
            else:
                logger.info(
                    "FLIP ENTRY CAP: skipping live flip (entry=%.3f outside %.2f-%.2f)",
                    flip_entry, settings.live_entry_min, settings.live_entry_max,
                )

        # Spread filter — wide spreads predict poor EE outcomes
        live_spread = live_signal.get("spread") or 0.0
        if live_spread > 0.03:
            logger.info(
                "SPREAD FILTER: skipping live trade (spread=%.3f > 0.03) on %s %s",
                live_spread, live_signal["side"], live_signal["market_slug"],
            )
            live_signal = {**live_signal, "exploration": True}  # route to paper-only

        if self.live_trader and self.live_trader.is_active and not self.live_trader.is_paused and not live_signal.get("exploration"):
            # Determine token ID from market
            if market:
                token_id = (
                    market.up_token_id if live_signal["side"] == "UP"
                    else market.down_token_id
                )
                # Use live trader's own adaptive sizing (based on live bankroll)
                live_amount = self.live_trader.compute_bet_size(
                    confidence=live_signal.get("confidence", 0.0),
                )

                live_result = await self.live_trader.place_order(
                    token_id=token_id,
                    amount_usdc=live_amount,
                    side=live_signal["side"],
                    market_slug=live_signal["market_slug"],
                    entry_price=live_signal["entry_price"],
                )

                # FOK rejection — treat as skipped window for tracking
                if live_result.get("fok_rejected"):
                    self._window_skip_reason = (
                        f"FOK rejected (insufficient liquidity) "
                        f"${live_amount:.2f} @ ${live_signal['entry_price']:.2f}"
                    )
                    self._window_traded = False  # no trade actually placed
                    logger.info("FOK rejected — %s", self._window_skip_reason)

                # Safety: cancel any lingering orders (shouldn't exist with FOK,
                # but kept as a belt-and-suspenders check)
                if live_result["success"]:
                    await self.live_trader.cancel_all_orders()

                # Persist to DB first (to get row ID for early exit tracking)
                trade_tag = None
                if settings.side_selection == "cheaper":
                    trade_tag = "cheap_side"
                elif flip_signal and settings.regime_flip_live:
                    trade_tag = "regime_flip"
                elif live_signal.get("_post_streak"):
                    trade_tag = "post_streak"
                # Parse fill price from CLOB response if available
                _fill_price = None
                _resp = live_result.get("response")
                if _resp and isinstance(_resp, dict):
                    try:
                        # FOK response: takingAmount (tokens) / makingAmount (USDC)
                        taking = float(_resp.get("takingAmount", 0))
                        making = float(_resp.get("makingAmount", 0))
                        if taking > 0 and making > 0:
                            _fill_price = round(making / taking, 4)
                        else:
                            # GTC fallback: fill price at takerOrder.price
                            taker_order = _resp.get("takerOrder")
                            if taker_order and isinstance(taker_order, dict):
                                _fill_price = float(taker_order.get("price", 0))
                                if _fill_price <= 0:
                                    _fill_price = None
                    except (ValueError, TypeError):
                        _fill_price = None

                # Regime sub-components for data collection
                _regime = self._current_regime
                # Entry-time orderbook: pick the book matching our side
                _token_book = (
                    up_book if live_signal["side"] == "UP" else down_book
                ) if (up_book or down_book) else None
                live_trade_id = await self.db.save_live_trade(
                    timestamp=int(time.time() * 1000),
                    market_slug=live_signal["market_slug"],
                    side=live_signal["side"],
                    token_id=token_id,
                    amount_usdc=live_result["amount"],
                    order_id=live_result.get("order_id"),
                    status="filled" if live_result["success"] else "failed",
                    success=live_result["success"],
                    response_json=json.dumps(_resp) if _resp else None,
                    entry_price=live_signal["entry_price"],
                    trade_tag=trade_tag,
                    regime_state=live_signal.get("regime_state"),
                    regime_strength=live_signal.get("regime_strength"),
                    btc_price_at_open=self._window_btc_start,
                    fill_price=_fill_price,
                    regime_direction_pct=_regime.direction_pct if _regime else None,
                    regime_momentum_score=_regime.momentum_score if _regime else None,
                    regime_ema_slope=_regime.ema_slope if _regime else None,
                    regime_price_vs_ema=_regime.price_vs_ema if _regime else None,
                    regime_ema_cross=_regime.ema_cross if _regime else None,
                    model_confidence=live_signal.get("confidence"),
                    # Entry-time Binance features (for exit-probability ML)
                    entry_obi=feature_vec.obi if feature_vec else None,
                    entry_taker_ratio=feature_vec.taker_ratio if feature_vec else None,
                    entry_momentum_1m=feature_vec.momentum_1m if feature_vec else None,
                    entry_momentum_5m=feature_vec.momentum_5m if feature_vec else None,
                    entry_rsi=feature_vec.rsi if feature_vec else None,
                    entry_vwap_dev=feature_vec.vwap_deviation if feature_vec else None,
                    entry_bb_position=feature_vec.bb_position if feature_vec else None,
                    entry_ema_cross=feature_vec.ema_cross if feature_vec else None,
                    entry_funding_zscore=feature_vec.funding_rate if feature_vec else None,
                    entry_volume_zscore=feature_vec.volume_zscore if feature_vec else None,
                    entry_atr=feature_vec.atr if feature_vec else None,
                    # Entry-time Polymarket orderbook state
                    entry_up_spread=market.up_spread,
                    entry_down_spread=market.down_spread,
                    entry_token_bid_size=_token_book.bid_size if _token_book else None,
                    entry_token_ask_size=_token_book.ask_size if _token_book else None,
                )

                # Track token for settlement + early exit
                if live_result["success"]:
                    # Use actual token count from CLOB response (takingAmount)
                    # instead of formula estimate.  The CLOB already deducts fees.
                    resp_data = live_result.get("response") or {}
                    taking_str = resp_data.get("takingAmount", "")
                    if taking_str and str(taking_str).strip():
                        tokens = float(taking_str)
                        logger.info(
                            "Actual tokens from CLOB: %.4f (entry=%.3f, USDC=%.2f)",
                            tokens, live_signal["entry_price"], live_result["amount"],
                        )
                    else:
                        # Fallback to formula if response missing
                        from data.polymarket import compute_fee_factor
                        fee_factor = compute_fee_factor(
                            live_signal["entry_price"],
                            settings.polymarket_fee_rate,
                            settings.polymarket_fee_exponent,
                        )
                        tokens = (live_result["amount"] / live_signal["entry_price"]) * (1.0 - fee_factor)
                        logger.warning(
                            "No takingAmount in response — using formula estimate: %.4f tokens",
                            tokens,
                        )
                    self._live_trade_tokens[live_signal["market_slug"]] = {
                        "side": live_signal["side"],
                        "token_id": token_id,
                        "amount": live_result["amount"],
                        "entry_price": live_signal["entry_price"],
                        "tokens": tokens,
                        "db_id": live_trade_id,
                        "trade_tag": trade_tag,
                    }

                # Telegram alert — combined trade + signal info
                if self.alerter:
                    await self.alerter.send_trade_placed_alert(
                        side=live_signal["side"],
                        slug=live_signal["market_slug"],
                        amount=live_result["amount"],
                        entry_price=live_signal["entry_price"],
                        confidence=live_signal.get("confidence", 0.0),
                        edge=live_signal.get("edge", 0.0),
                        order_id=live_result.get("order_id"),
                        success=live_result["success"],
                        error_msg=live_result.get("error", ""),
                        is_regime_flip=bool(live_signal.get("regime_flip")),
                    )


    # ------------------------------------------------------------------
    # Early exit monitoring (data collection — no trading)
    # ------------------------------------------------------------------

    # Only log early exit data every N seconds to avoid flooding
    _EARLY_EXIT_LOG_INTERVAL: float = 10.0
    _last_early_exit_log: float = 0.0

    async def _monitor_early_exit(self) -> None:
        """Monitor bid prices for active positions and sell when threshold hit.

        Runs every 1s for the whole window (after a 10s grace period to avoid
        stale book data right after entry).  Tiered exit thresholds by entry
        price — cheap entries exit aggressively, expensive entries stay conservative.
        """
        if not self._current_slug or not self._window_start_time:
            return

        seconds_in = time.time() - self._window_start_time
        # Skip first 10 seconds — book data may be stale right after entry
        if seconds_in < 10:
            return

        now = time.time()

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

        # Parse bids (buyers willing to buy our token).
        # CLOB API returns bids sorted ascending — best bid is LAST.
        bids = book_data.get("bids", [])
        if not bids:
            return

        # Best bid = highest price (last element in ascending sort)
        best_bid = float(bids[-1].get("price", 0))
        best_bid_size = float(bids[-1].get("size", 0))

        # Track max/min bid during this window for data collection
        if live_pos:
            prev_max = live_pos.get("max_bid", 0)
            if best_bid > prev_max:
                live_pos["max_bid"] = best_bid
            prev_min = live_pos.get("min_bid", 999)
            if best_bid < prev_min:
                live_pos["min_bid"] = best_bid

        # Total bid depth above 0.85
        deep_bids = [(float(b["price"]), float(b["size"])) for b in bids if float(b["price"]) >= 0.85]
        total_deep_size = sum(s for _, s in deep_bids)

        seconds_left = 300 - seconds_in

        # Rate-limit logging only (exit trigger still runs every 1s)
        if now - self._last_early_exit_log >= self._EARLY_EXIT_LOG_INTERVAL:
            self._last_early_exit_log = now
            btc = self.binance.get_latest_price()
            btc_move = ""
            if btc and self._window_btc_start:
                delta = btc - self._window_btc_start
                btc_move = f" | BTC {'+' if delta >= 0 else ''}{delta:.2f}"
            profit_pct = ((best_bid / entry_price) - 1) * 100 if entry_price > 0 else 0
            logger.info(
                "[EARLY-EXIT] %s %s | %.0fs left | bid=%.3f x%.0f | depth>=0.85: %.0f tokens | "
                "entry=%.3f profit=%.1f%%%s",
                side, slug[-15:], seconds_left,
                best_bid, best_bid_size,
                total_deep_size,
                entry_price, profit_pct, btc_move,
            )

        # ---- EARLY EXIT TRIGGER ----
        # Tiered thresholds by entry price. Cheap entries (<0.50) have
        # low WR (13-25%) and benefit from aggressive exits. Expensive
        # entries keep the conservative 0.95 threshold.
        # Data: 640 trades, validated on live + paper + CLOB ground truth.
        trade_tag = live_pos.get("trade_tag") if live_pos else None
        exit_threshold = self.live_trader.get_exit_threshold(entry_price, trade_tag=trade_tag) if self.live_trader else 0.95
        # Use best_bid_size (depth at the actual best bid price) — not
        # total_deep_size (depth >= 0.85) which is meaningless when the
        # exit threshold is 0.45-0.65 and bids are nowhere near 0.85.
        available_depth = best_bid_size
        # Retry logic: up to 3 rapid attempts with 5s cooldown, then
        # reset every 30s so we keep trying as long as bid stays above threshold.
        # Previously hard-capped at 3 total — missed a $0.99 bid for 90+ seconds.
        exit_attempts = live_pos.get("exit_attempts", 0) if live_pos else 0
        last_attempt = live_pos.get("last_exit_attempt", 0) if live_pos else 0
        cooldown_ok = (now - last_attempt) >= 5
        # Reset attempt counter after 30s — gives CLOB API time to recover
        if exit_attempts >= 3 and (now - last_attempt) >= 30:
            exit_attempts = 0
            if live_pos:
                live_pos["exit_attempts"] = 0
        if (live_pos
                and not live_pos.get("exited")
                and exit_attempts < 3
                and cooldown_ok
                and self.live_trader
                and best_bid >= exit_threshold
                and available_depth >= 20):

            live_pos["exit_attempts"] = exit_attempts + 1
            live_pos["last_exit_attempt"] = now

            result = await self.live_trader.sell_early_exit(
                token_id=token_id,
                tokens=live_pos["tokens"],
                best_bid=best_bid,
                market_slug=slug,
            )

            # Cancel any remaining open orders after sell attempt
            await self.live_trader.cancel_all_orders()

            if not result["success"]:
                # Check if tokens were actually sold despite API error
                remaining = await self.live_trader.get_token_balance(token_id)
                if remaining is not None and remaining < 1.0:
                    # Tokens are gone — sell succeeded silently on-chain
                    logger.warning(
                        "[EARLY-EXIT] API reported failure but tokens are GONE (balance=%.2f) "
                        "— treating as successful sell for %s",
                        remaining, slug[-15:],
                    )
                    result["success"] = True
                    result["sell_amount"] = best_bid * live_pos["tokens"]
                else:
                    logger.warning(
                        "[EARLY-EXIT] Sell failed for %s (attempt %d/3, balance=%.1f) "
                        "— will retry after 5s cooldown (resets after 30s)",
                        slug[-15:], live_pos["exit_attempts"],
                        remaining if remaining is not None else -1,
                    )

            if result["success"]:
                buy_amount = live_pos["amount"]
                # Use actual USDC received from CLOB (takingAmount) for
                # accurate P&L.  Falls back to estimated sell_amount.
                resp_data = result.get("response") or {}
                taking_str = resp_data.get("takingAmount", "")
                if taking_str and str(taking_str).strip():
                    sell_amount = float(taking_str)
                    logger.info(
                        "EE actual USDC received: $%.2f (estimated: $%.2f)",
                        sell_amount, result["sell_amount"],
                    )
                else:
                    sell_amount = result["sell_amount"]
                    logger.warning(
                        "No takingAmount in EE sell response — using estimate $%.2f",
                        sell_amount,
                    )
                pnl = sell_amount - buy_amount

                await self.db.update_live_trade(
                    live_pos["db_id"], "EARLY_EXIT", pnl, int(time.time() * 1000),
                    max_bid_during_window=live_pos.get("max_bid"),
                    min_bid_during_window=live_pos.get("min_bid"),
                    exit_threshold_used=exit_threshold,
                )
                self.live_trader.record_settlement(pnl > 0, pnl)

                # Early exit breaks loss streak (it was a profitable rescue)
                side = live_pos["side"]
                if side in self._side_outcomes:
                    self._side_outcomes[side].append("EARLY_EXIT")
                    if len(self._side_outcomes[side]) > 10:
                        self._side_outcomes[side] = self._side_outcomes[side][-10:]

                live_pos["exited"] = True

                tier = "low" if entry_price < 0.35 else ("mid" if entry_price < 0.50 else "high")
                logger.info(
                    "[EARLY-EXIT SOLD] %s %s | bid=%.3f (threshold=%.2f, tier=%s) | "
                    "tokens=%.1f | sell=$%.2f buy=$%.2f | pnl=$%+.2f | %.0fs left",
                    side, slug[-15:], best_bid, exit_threshold, tier,
                    live_pos["tokens"],
                    sell_amount, buy_amount, pnl, seconds_left,
                )

                # Telegram alert
                if self.alerter:
                    try:
                        tier_label = "low" if entry_price < 0.35 else ("mid" if entry_price < 0.50 else "high")
                        await self.alerter._send(
                            f"<b>EARLY EXIT</b> ({tier_label} tier)\n\n"
                            f"Market: {slug}\n"
                            f"Side: {side} | Sold at ${best_bid:.3f} (threshold {exit_threshold:.2f})\n"
                            f"Tokens: {live_pos['tokens']:.1f} | Sell: ${sell_amount:.2f}\n"
                            f"PnL: <b>${pnl:+.2f}</b> | {seconds_left:.0f}s early"
                        )
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Window settlement
    # ------------------------------------------------------------------

    async def _query_gamma_resolution(self, slug: str, max_retries: int = 3) -> str | None:
        """Query Gamma API for the actual Polymarket resolution of a market.

        Returns "UP", "DOWN", or None if not yet resolved.
        Retries with delays to allow on-chain resolution to complete.
        """
        import aiohttp as _aiohttp

        for attempt in range(max_retries):
            if attempt > 0:
                await asyncio.sleep(3)  # wait between retries

            try:
                async with _aiohttp.ClientSession(
                    timeout=_aiohttp.ClientTimeout(total=8)
                ) as session:
                    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                events = data if isinstance(data, list) else [data]
                if not events:
                    continue
                market = events[0].get("markets", [{}])[0]
                if not market.get("closed"):
                    continue

                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                prices = market.get("outcomePrices", [])
                if isinstance(prices, str):
                    prices = json.loads(prices)

                up_idx = outcomes.index("Up") if "Up" in outcomes else None
                if up_idx is not None:
                    up_price = float(prices[up_idx])
                    if up_price >= 0.99:
                        return "UP"
                    elif up_price <= 0.01:
                        return "DOWN"
            except Exception:
                continue

        return None

    async def _settle_previous_window(self) -> None:
        """Settle trades from the previous 5-minute window.

        For live trades: queries Gamma API for actual Polymarket resolution.
        For paper trades: uses Chainlink/Binance price comparison (best effort).
        """
        # Get BTC end price — prefer boundary-accurate buffer lookup
        window_end_time = self._window_start_time + 300.0  # 5-min window
        btc_end, price_delta = self.polymarket.get_chainlink_price_at(window_end_time)
        if btc_end is not None:
            price_source = f"Chainlink Buffer (delta={price_delta:.1f}s)"
        else:
            btc_end = self.polymarket.get_chainlink_stream_price()
            price_source = "Chainlink Stream"
        if btc_end is None:
            btc_end = self.binance.get_latest_price()
            price_source = "Binance (fallback)"

        # Query Gamma API for the ACTUAL Polymarket resolution (source of truth)
        slug = self._current_slug
        gamma_resolution = None
        if slug:
            # Wait a few seconds for on-chain resolution to finalize
            await asyncio.sleep(5)
            gamma_resolution = await self._query_gamma_resolution(slug)

        # Price-based direction (for paper trades + logging)
        have_prices = self._window_btc_start is not None and btc_end is not None
        if have_prices:
            btc_went_up_price = btc_end > self._window_btc_start
            direction_price = "UP" if btc_went_up_price else "DOWN"
            delta = btc_end - self._window_btc_start
        else:
            btc_went_up_price = None
            direction_price = "UNKNOWN"
            delta = 0.0

        # Use Gamma resolution if available, fall back to price comparison
        if gamma_resolution:
            btc_went_up = gamma_resolution == "UP"
            direction = gamma_resolution
            settlement_source = "Gamma API"
        elif have_prices:
            btc_went_up = btc_went_up_price
            direction = direction_price
            settlement_source = price_source
        else:
            logger.warning("Cannot settle %s — no Gamma resolution and no price data", slug)
            # Still send end-of-window skip summary even when settlement fails
            if not self._window_traded and not self._window_skip_notified and self.alerter:
                reason = self._window_skip_reason or "no signal from model"
                try:
                    await self.alerter._send(
                        f"SKIPPED {self._current_slug}\n"
                        f"Reason: {reason}\n"
                        f"BTC: N/A (no settlement data)",
                        parse_mode=None,
                    )
                except Exception:
                    logger.exception("Failed to send skip notification")
            return

        # Log if Gamma disagrees with price data
        if gamma_resolution and have_prices and gamma_resolution != direction_price:
            logger.warning(
                "Gamma resolution (%s) DISAGREES with %s price (%s) for %s",
                gamma_resolution, price_source, direction_price, slug,
            )

        logger.info(
            "--- WINDOW SETTLED: %s | BTC $%.2f -> $%.2f (%+.2f = %s) [%s]",
            self._current_slug,
            self._window_btc_start or 0,
            btc_end or 0,
            delta,
            direction,
            settlement_source,
        )

        # Log skipped windows to DB + Telegram
        if not self._window_traded and slug:
            reason = self._window_skip_reason or "no signal from model"
            _r = self._current_regime
            _sd = self._window_skip_data or {}
            _fv = _sd.get("feature_vec")
            await self.db.save_skipped_window(
                timestamp=int(time.time() * 1000),
                market_slug=slug,
                skip_reason=reason,
                btc_price=btc_end,
                regime_state=_r.regime if _r else None,
                regime_strength=_r.strength if _r else None,
                model_confidence=_sd.get("model_confidence"),
                entry_price=_sd.get("entry_price"),
                model_side=_sd.get("model_side"),
                entry_obi=_fv.obi if _fv else None,
                entry_taker_ratio=_fv.taker_ratio if _fv else None,
                entry_momentum_1m=_fv.momentum_1m if _fv else None,
                entry_momentum_5m=_fv.momentum_5m if _fv else None,
                entry_rsi=_fv.rsi if _fv else None,
                entry_vwap_dev=_fv.vwap_deviation if _fv else None,
                entry_bb_position=_fv.bb_position if _fv else None,
                entry_ema_cross=_fv.ema_cross if _fv else None,
                entry_funding_zscore=_fv.funding_rate if _fv else None,
                entry_volume_zscore=_fv.volume_zscore if _fv else None,
                entry_atr=_fv.atr if _fv else None,
            )
            if not self._window_skip_notified and self.alerter:
                try:
                    btc_start_str = f"${self._window_btc_start:,.2f}" if self._window_btc_start else "N/A"
                    btc_end_str = f"${btc_end:,.2f}" if btc_end else "N/A"
                    await self.alerter._send(
                        f"SKIPPED {self._current_slug}\n"
                        f"Reason: {reason}\n"
                        f"BTC: {btc_start_str} to {btc_end_str} ({direction})",
                        parse_mode=None,
                    )
                except Exception:
                    logger.exception("Failed to send skip notification")

        if self.paper_trader and have_prices:
            await self.paper_trader.settle_all_pending(
                btc_start_price=self._window_btc_start,
                btc_end_price=btc_end,
            )

            stats = await self.paper_trader.get_stats()
            logger.info(
                "--- P&L: $%+.2f | Win rate: %.0f%% (%d/%d) | Bankroll: $%.2f",
                stats.get("total_pnl", 0),
                stats.get("win_rate", 0) * 100,
                stats.get("wins", 0),
                stats.get("settled_trades", 0),
                stats.get("bankroll", 0),
            )

        # Settle live trades using Gamma resolution (or price fallback)
        settled_ids: list[int] = []
        if self.live_trader and self.live_trader.is_active:
            settled_ids = await self._settle_live_trades_for_window(slug, btc_went_up, btc_end)

        # Backfill settlement data on any EARLY_EXIT trades for this window
        if slug:
            await self.db.backfill_early_exit_settlement(slug, btc_end, btc_went_up)

        # Pop in-memory position data before it goes stale
        live_info = None
        if slug and slug in self._live_trade_tokens:
            live_info = self._live_trade_tokens.pop(slug)

        # Layer 1: Schedule delayed Gamma verification if we used Chainlink fallback
        if settled_ids and settlement_source != "Gamma API":
            asyncio.create_task(
                self._verify_settlement_gamma(slug, settled_ids, live_info=live_info),
                name=f"gamma_verify_{slug}",
            )

        # Telegram alerts for live settlement
        if live_info and not live_info.get("exited") and self.alerter:
            if settlement_source == "Gamma API":
                # Gamma was available at settlement — notification is accurate
                trade_won = (live_info["side"] == "UP" and btc_went_up) or \
                            (live_info["side"] == "DOWN" and not btc_went_up)
                outcome = "WIN" if trade_won else "LOSS"
                await self.alerter.send_live_settlement_alert(
                    slug=slug,
                    side=live_info["side"],
                    outcome=outcome,
                    amount=live_info.get("amount", 0),
                    entry_price=live_info.get("entry_price", 0),
                )
                await self.alerter.send_stats_summary(
                    stats, live_info=await self._build_live_info()
                )
            elif settled_ids:
                # Chainlink fallback — defer notification until Gamma verifies (~5 min)
                logger.info(
                    "Deferring settlement notification for %s until Gamma verification (%ds)",
                    slug, settings.gamma_verify_delay_seconds,
                )
            else:
                # No settled IDs but live_info exists — send with unverified tag
                trade_won = (live_info["side"] == "UP" and btc_went_up) or \
                            (live_info["side"] == "DOWN" and not btc_went_up)
                outcome = "WIN [unverified]" if trade_won else "LOSS [unverified]"
                await self.alerter.send_live_settlement_alert(
                    slug=slug,
                    side=live_info["side"],
                    outcome=outcome,
                    amount=live_info.get("amount", 0),
                    entry_price=live_info.get("entry_price", 0),
                )
                await self.alerter.send_stats_summary(
                    stats, live_info=await self._build_live_info()
                )

    async def _settle_live_trades_for_window(
        self, slug: str, btc_went_up: bool, settlement_price: float | None = None
    ) -> list[int]:
        """Settle unsettled live trades matching the given slug.

        Returns list of settled trade IDs (for Gamma verification scheduling).
        """
        settled_ids: list[int] = []
        if not self.live_trader:
            return settled_ids
        unsettled = await self.db.get_unsettled_live_trades()
        for row in unsettled:
            if row["market_slug"] != slug:
                continue
            side = row["side"]
            amount = row["amount_usdc"]
            entry_price = row.get("entry_price") or 0.0

            won = (side == "UP" and btc_went_up) or (side == "DOWN" and not btc_went_up)
            outcome = "WIN" if won else "LOSS"

            # PnL calculation — use actual token count from buy response
            # when available, otherwise fall back to fee-adjusted formula.
            live_pos = self._live_trade_tokens.get(slug)
            if live_pos and live_pos.get("tokens"):
                shares = live_pos["tokens"]
                pnl = (shares - amount) if won else -amount
            elif entry_price > 0:
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

            # Update DB — include max bid, exit threshold, settlement price
            settled_at = int(time.time() * 1000)
            live_pos = self._live_trade_tokens.get(slug)
            max_bid = live_pos.get("max_bid") if live_pos else None
            min_bid = live_pos.get("min_bid") if live_pos else None
            exit_th = (
                self.live_trader.get_exit_threshold(entry_price, trade_tag=live_pos.get("trade_tag") if live_pos else None)
                if entry_price > 0 else None
            )
            await self.db.update_live_trade(
                row["id"], outcome, pnl, settled_at,
                max_bid_during_window=max_bid,
                min_bid_during_window=min_bid,
                exit_threshold_used=exit_th,
                settlement_price=settlement_price,
            )
            settled_ids.append(row["id"])

            # Update live trader internal state
            self.live_trader.record_settlement(won, pnl)

            # Update streak guard (skip maker_fills — only track our taker trades)
            trade_tag = row.get("trade_tag")
            if trade_tag not in ("maker_fill",):
                self._side_outcomes[side].append(outcome)
                # Keep only last 10
                if len(self._side_outcomes[side]) > 10:
                    self._side_outcomes[side] = self._side_outcomes[side][-10:]
                # Check for consecutive losses
                recent = self._side_outcomes[side]
                threshold = settings.streak_pause_threshold
                if (len(recent) >= threshold
                        and all(r == "LOSS" for r in recent[-threshold:])):
                    pause_seconds = settings.streak_pause_windows * 300
                    self._side_paused[side] = time.time() + pause_seconds
                    logger.warning(
                        "STREAK GUARD: %dx %s LOSS — pausing %s for %d windows (%.0fs)",
                        threshold, side, side, settings.streak_pause_windows, pause_seconds,
                    )
                    if self.alerter:
                        try:
                            await self.alerter._send(
                                f"STREAK GUARD: {threshold}x {side} LOSS\n"
                                f"Pausing {side} for {settings.streak_pause_windows} windows",
                                parse_mode=None,
                            )
                        except Exception:
                            pass

            logger.info(
                "Settled live trade: %s %s -> %s | pnl=$%+.2f | live bankroll=$%.2f",
                side, slug, outcome, pnl, self.live_trader.bankroll,
            )

        return settled_ids

    # ------------------------------------------------------------------
    # Gamma verification (3-layer system)
    # ------------------------------------------------------------------

    async def _db_fetch_trade(self, trade_id: int) -> dict | None:
        """Fetch a live trade by ID (thin wrapper for verification code)."""
        return await self.db.get_live_trade_by_id(trade_id)

    def _compute_pnl(
        self, won: bool, amount: float, entry_price: float,
        response_json: str | None = None,
    ) -> float:
        """Compute PnL for a trade given outcome, amount, and entry price.

        Tries actual token count from response_json first, then falls back
        to fee-adjusted formula.
        """
        # Try actual tokens from FOK response (matches _settle_stale_live_trades pattern)
        actual_tokens = None
        if response_json:
            try:
                resp_d = json.loads(response_json)
                ts = resp_d.get("takingAmount", "")
                if ts and str(ts).strip():
                    actual_tokens = float(ts)
            except Exception:
                pass

        if actual_tokens is not None:
            return (actual_tokens - amount) if won else -amount

        if entry_price > 0 and self.live_trader:
            fee_factor = 0.0
            if self.live_trader._fee_rate > 0:
                from data.polymarket import compute_fee_factor
                fee_factor = compute_fee_factor(
                    entry_price,
                    self.live_trader._fee_rate,
                    self.live_trader._fee_exponent,
                )
            shares = (amount / entry_price) * (1.0 - fee_factor)
            return (shares - amount) if won else -amount

        return -amount if not won else 0.0

    async def _rebuild_streak_state(self, side: str) -> None:
        """Rebuild _side_outcomes from last 10 DB trades for a given side.

        Called after Gamma corrections to ensure streak guard reflects
        corrected outcomes rather than stale in-memory state.
        """
        recent = await self.db.get_recent_outcomes(side, limit=10)
        self._side_outcomes[side] = [r["outcome"] for r in recent]
        logger.info(
            "Rebuilt streak state for %s: %s",
            side, self._side_outcomes[side][-3:] if self._side_outcomes[side] else "[]",
        )

    async def _verify_settlement_gamma(
        self, slug: str, settled_trade_ids: list[int],
        live_info: dict | None = None,
    ) -> None:
        """Layer 1: Delayed Gamma verification of Chainlink-settled trades.

        Waits gamma_verify_delay_seconds (default 5 min) for Gamma resolution
        to become available, then checks if our Chainlink-based settlement was
        correct. Corrects phantom WINs/missed WINs in DB.

        If live_info is provided, sends the Telegram settlement notification
        AFTER verification (deferred from _settle_previous_window).
        """
        delay = settings.gamma_verify_delay_seconds
        logger.info(
            "Gamma verify scheduled for %s (%d trades) in %ds",
            slug, len(settled_trade_ids), delay,
        )
        await asyncio.sleep(delay)

        # Query Gamma
        gamma_resolution = await self._query_gamma_resolution(slug, max_retries=3)
        if not gamma_resolution:
            logger.warning(
                "Gamma verify: no resolution for %s after %ds — Layer 2 will catch it",
                slug, delay,
            )
            # Still send deferred notification with Chainlink-based outcome (best we have)
            if live_info and not live_info.get("exited") and self.alerter:
                # Read outcome from DB (already settled by Chainlink)
                for trade_id in settled_trade_ids:
                    row = await self._db_fetch_trade(trade_id)
                    if row and row["outcome"] in ("WIN", "LOSS"):
                        await self.alerter.send_live_settlement_alert(
                            slug=slug,
                            side=row["side"],
                            outcome=f"{row['outcome']} [unverified]",
                            amount=row["amount_usdc"],
                            entry_price=row.get("entry_price", 0),
                        )
                        break
                await self.alerter.send_stats_summary(
                    await self.paper_trader.get_stats() if self.paper_trader else {},
                    live_info=await self._build_live_info(),
                )
            return

        # Check each settled trade against Gamma truth
        corrections = 0
        verified_outcome = None  # for deferred notification
        verified_side = None
        verified_amount = 0.0
        verified_entry_price = 0.0
        for trade_id in settled_trade_ids:
            try:
                row = await self._db_fetch_trade(trade_id)
                if not row:
                    continue
                # Skip trades that were already corrected or are EE/VOID
                if row.get("gamma_resolution") is not None:
                    continue
                if row["outcome"] not in ("WIN", "LOSS"):
                    continue

                side = row["side"]
                amount = row["amount_usdc"]
                entry_price = row.get("entry_price") or 0.0

                # What Gamma says should have happened
                side_would_win = (side == gamma_resolution)
                expected_outcome = "WIN" if side_would_win else "LOSS"
                matches = 1 if row["outcome"] == expected_outcome else 0

                # Track for deferred notification
                verified_outcome = expected_outcome
                verified_side = side
                verified_amount = amount
                verified_entry_price = entry_price

                if matches:
                    # Outcome is correct — just stamp verification
                    await self.db.stamp_gamma_verification(
                        trade_id, gamma_resolution, 1
                    )
                else:
                    # WRONG — correct it
                    corrections += 1
                    old_outcome = row["outcome"]

                    # Recalculate PnL from scratch
                    new_pnl = self._compute_pnl(
                        won=side_would_win,
                        amount=amount,
                        entry_price=entry_price,
                        response_json=row.get("response_json"),
                    )

                    await self.db.correct_trade_outcome(
                        trade_id, expected_outcome, new_pnl,
                        gamma_resolution, 0,
                    )

                    # Update live trader bankroll (reverse old PnL, apply new)
                    if self.live_trader:
                        old_pnl = row.get("pnl", 0.0)
                        pnl_delta = new_pnl - old_pnl
                        self.live_trader.bankroll += pnl_delta

                    logger.warning(
                        "GAMMA CORRECTION: trade %d %s %s: %s -> %s (pnl $%+.2f -> $%+.2f)",
                        trade_id, side, slug, old_outcome, expected_outcome,
                        row.get("pnl", 0), new_pnl,
                    )

            except Exception:
                logger.exception("Gamma verify failed for trade %d", trade_id)

        if corrections:
            # Rebuild streak state from corrected DB data
            for side in ("UP", "DOWN"):
                await self._rebuild_streak_state(side)
            logger.warning(
                "Gamma verify %s: %d correction(s) applied, streak state rebuilt",
                slug, corrections,
            )
        else:
            logger.info("Gamma verify %s: all %d trades confirmed correct", slug, len(settled_trade_ids))

        # Send deferred Telegram notification with Gamma-verified outcome
        if live_info and not live_info.get("exited") and self.alerter and verified_outcome:
            await self.alerter.send_live_settlement_alert(
                slug=slug,
                side=verified_side or live_info["side"],
                outcome=verified_outcome,
                amount=verified_amount or live_info.get("amount", 0),
                entry_price=verified_entry_price or live_info.get("entry_price", 0),
            )
            await self.alerter.send_stats_summary(
                await self.paper_trader.get_stats() if self.paper_trader else {},
                live_info=await self._build_live_info(),
            )

    async def _gamma_reconciliation(self) -> None:
        """Layer 2: Hourly reconciliation of trades missing Gamma verification.

        Catches anything Layer 1 missed (e.g., task cancelled on restart,
        Gamma was slow). Re-checks all settled trades without gamma_resolution.
        """
        unverified = await self.db.get_unverified_trades(min_age_seconds=600)
        if not unverified:
            return

        logger.info("Gamma reconciliation: %d unverified trades", len(unverified))

        # Group by slug for efficient API calls
        by_slug: dict[str, list[dict]] = {}
        for row in unverified:
            by_slug.setdefault(row["market_slug"], []).append(row)

        corrections = 0
        verified = 0
        for slug, trades in by_slug.items():
            gamma_resolution = await self._query_gamma_resolution(slug, max_retries=2)
            if not gamma_resolution:
                continue  # Still not resolved — try next hour

            for row in trades:
                side = row["side"]
                side_would_win = (side == gamma_resolution)
                expected_outcome = "WIN" if side_would_win else "LOSS"
                matches = 1 if row["outcome"] == expected_outcome else 0

                if matches:
                    await self.db.stamp_gamma_verification(
                        row["id"], gamma_resolution, 1
                    )
                    verified += 1
                else:
                    # Correction needed
                    amount = row["amount_usdc"]
                    entry_price = row.get("entry_price") or 0.0
                    new_pnl = self._compute_pnl(
                        won=side_would_win,
                        amount=amount,
                        entry_price=entry_price,
                        response_json=row.get("response_json"),
                    )
                    await self.db.correct_trade_outcome(
                        row["id"], expected_outcome, new_pnl,
                        gamma_resolution, 0,
                    )

                    # Adjust bankroll
                    if self.live_trader:
                        old_pnl = row.get("pnl", 0.0)
                        self.live_trader.bankroll += (new_pnl - old_pnl)

                    corrections += 1
                    logger.warning(
                        "RECONCILIATION: trade %d %s %s: %s -> %s",
                        row["id"], side, slug, row["outcome"], expected_outcome,
                    )

            await asyncio.sleep(0.3)  # Rate limit Gamma API

        if corrections:
            for side in ("UP", "DOWN"):
                await self._rebuild_streak_state(side)

            if self.alerter:
                try:
                    await self.alerter._send(
                        f"RECONCILIATION: {corrections} correction(s), {verified} confirmed\n"
                        f"Streak state rebuilt",
                        parse_mode=None,
                    )
                except Exception:
                    pass

        if verified or corrections:
            logger.info(
                "Gamma reconciliation done: %d verified, %d corrected",
                verified, corrections,
            )

    async def _balance_sanity_check(self) -> None:
        """Layer 3: Compare DB-calculated bankroll to actual Polymarket balance.

        Alerts if discrepancy exceeds $5. This catches systematic errors
        (phantom WINs, missed settlements, fee miscalculations) that
        individual trade verification might miss.
        """
        if not self.live_trader or not self.live_trader.is_active:
            return

        actual = await self.live_trader.get_balance()
        if actual is None:
            return

        # Count tokens in open positions (pending trades have value)
        unsettled = await self.db.get_unsettled_live_trades()
        open_value = sum(r.get("amount_usdc", 0) for r in unsettled)

        db_bankroll = self.live_trader.bankroll
        # Actual portfolio = USDC cash + value of open positions
        # DB bankroll tracks cash only (positions are subtracted at entry, added at settlement)
        expected_cash = db_bankroll
        discrepancy = actual - expected_cash

        logger.info(
            "Balance check: actual=$%.2f, DB bankroll=$%.2f, "
            "open positions=%d ($%.2f), discrepancy=$%+.2f",
            actual, db_bankroll, len(unsettled), open_value, discrepancy,
        )

        # Alert if discrepancy exceeds threshold (accounting for open positions)
        # Open positions are expected to cause ~$5-10 gap during active trading
        adjusted_discrepancy = abs(discrepancy) - open_value
        if adjusted_discrepancy > 5.0:
            logger.warning(
                "BALANCE DISCREPANCY: $%.2f (adjusted for %d open positions: $%.2f)",
                discrepancy, len(unsettled), adjusted_discrepancy,
            )
            if self.alerter:
                try:
                    await self.alerter._send(
                        f"BALANCE WARNING\n"
                        f"Actual: ${actual:.2f}\n"
                        f"DB bankroll: ${db_bankroll:.2f}\n"
                        f"Open positions: {len(unsettled)} (${open_value:.2f})\n"
                        f"Gap: ${discrepancy:+.2f}",
                        parse_mode=None,
                    )
                except Exception:
                    pass

    async def _settle_stale_live_trades(self) -> None:
        """Settle stale unsettled live trades from previous sessions.

        Uses Gamma API for actual resolution (source of truth).
        Falls back to candle price comparison if Gamma unavailable.
        """
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

        # Cache Gamma resolutions by slug
        gamma_cache: dict[str, str | None] = {}

        for row in stale:
            slug = row["market_slug"]
            btc_end = None  # init before branches to avoid UnboundLocalError

            # Try Gamma API first (source of truth)
            if slug not in gamma_cache:
                gamma_cache[slug] = await self._query_gamma_resolution(slug, max_retries=1)

            gamma_res = gamma_cache[slug]
            if gamma_res:
                btc_went_up = gamma_res == "UP"
            else:
                # Fall back to candle data
                try:
                    window_ts = int(slug.rsplit("-", 1)[-1])
                except (ValueError, IndexError):
                    logger.warning("Cannot parse window ts from slug %s — voiding", slug)
                    await self.db.update_live_trade(
                        row["id"], "VOID", 0.0, int(time.time() * 1000)
                    )
                    continue

                btc_start = await self.db.get_btc_price_at(window_ts * 1000)
                btc_end = await self.db.get_btc_price_at((window_ts + 300) * 1000)
                if btc_start is None or btc_end is None:
                    logger.warning(
                        "No data for live trade %s — voiding trade %d", slug, row["id"],
                    )
                    await self.db.update_live_trade(
                        row["id"], "VOID", 0.0, int(time.time() * 1000)
                    )
                    continue
                btc_went_up = btc_end > btc_start

            side = row["side"]
            amount = row["amount_usdc"]
            entry_price = row.get("entry_price") or 0.0
            won = (side == "UP" and btc_went_up) or (side == "DOWN" and not btc_went_up)
            outcome = "WIN" if won else "LOSS"

            # Try actual token count from buy response_json
            actual_tokens = None
            resp_json_str = row.get("response_json")
            if resp_json_str:
                try:
                    resp_d = json.loads(resp_json_str)
                    ts = resp_d.get("takingAmount", "")
                    if ts and str(ts).strip():
                        actual_tokens = float(ts)
                except Exception:
                    pass

            if actual_tokens is not None:
                shares = actual_tokens
                pnl = (shares - amount) if won else -amount
            elif entry_price > 0:
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
            # For catchup: btc_end is only available in candle fallback path
            _stale_settle_price = btc_end if not gamma_res else None
            await self.db.update_live_trade(
                row["id"], outcome, pnl, settled_at,
                settlement_price=_stale_settle_price,
            )
            self.live_trader.record_settlement(won, pnl)

            source = "Gamma" if gamma_res else "candle"
            logger.info(
                "Settled stale live trade: %s %s -> %s | pnl=$%+.2f [%s]",
                side, slug, outcome, pnl, source,
            )

            # Backfill settlement data on EARLY_EXIT trades for same slug
            await self.db.backfill_early_exit_settlement(
                slug, btc_end, btc_went_up,
            )

    async def _sync_maker_fills(self) -> None:
        """Sync maker fills from CLOB API into the DB.

        Our GTC buy orders can get filled by other traders hitting them (maker
        fills). These are real P&L that the DB doesn't track because we only
        record taker orders we initiate. This method fetches all CLOB trades,
        identifies maker fills, and inserts them into live_trades.
        """
        if not self.live_trader or not self.live_trader.is_active:
            return

        try:
            all_trades = await self.live_trader.fetch_clob_trades()
            if not all_trades:
                return

            maker_trades = [t for t in all_trades if t.get("trader_side") == "MAKER"]
            if not maker_trades:
                logger.info("Maker fill sync: 0 maker trades found")
                return

            # Get existing order_ids from DB to avoid duplicates.
            # Maker fills don't have order_ids we placed, but each CLOB trade
            # has a unique 'id' field. We store it as order_id in the DB.
            existing = await self.db._db.execute(
                "SELECT order_id FROM live_trades WHERE trade_tag = 'maker_fill'"
            )
            existing_ids = {row[0] for row in await existing.fetchall()}

            new_fills = []
            # Cache condition_id -> slug lookups
            slug_cache: dict[str, str | None] = {}

            # Our funder address (case-insensitive match against maker_orders)
            our_addr = self.live_trader._funder_address.lower()

            for trade in maker_trades:
                trade_id = trade.get("id", "")
                if trade_id in existing_ids:
                    continue

                # For MAKER trades, the top-level size/price is the TAKER's
                # order. Our actual fill is in maker_orders where maker_address
                # matches our funder. Extract our specific matched_amount.
                maker_orders = trade.get("maker_orders", [])
                our_fill = None
                for mo in maker_orders:
                    if mo.get("maker_address", "").lower() == our_addr:
                        our_fill = mo
                        break
                if our_fill is None:
                    continue

                # Our fill details from the maker_orders sub-object
                our_tokens = float(our_fill.get("matched_amount", 0))
                our_price = float(our_fill.get("price", 0))
                our_side = our_fill.get("outcome", "").upper()  # "Up" -> "UP"
                if our_side not in ("UP", "DOWN") or our_tokens <= 0 or our_price <= 0:
                    continue

                amount_usdc = round(our_tokens * our_price, 2)
                asset_id = trade.get("asset_id", "")

                # Resolve condition_id -> slug
                condition_id = trade.get("market", "")
                if condition_id not in slug_cache:
                    slug_cache[condition_id] = await self.live_trader.get_market_slug(condition_id)
                    await asyncio.sleep(0.2)  # rate limit
                slug = slug_cache.get(condition_id)
                if not slug:
                    continue

                match_time = trade.get("match_time")
                ts = int(match_time) * 1000 if match_time else int(time.time() * 1000)

                new_fills.append({
                    "timestamp": ts,
                    "market_slug": slug,
                    "side": our_side,
                    "token_id": asset_id,
                    "amount_usdc": amount_usdc,
                    "order_id": trade_id,
                    "entry_price": our_price,
                })

            if not new_fills:
                logger.info("Maker fill sync: %d maker trades, 0 new", len(maker_trades))
                return

            # Insert new maker fills into DB
            # Post-FOK switch: new maker fills should not appear.
            # If they do, FOK is not working as expected.
            logger.warning(
                "UNEXPECTED: %d new maker fills found after FOK switch! "
                "FOK should prevent all maker fills. Investigate.",
                len(new_fills),
            )
            for fill in new_fills:
                # Best-effort BTC price lookup from candles at fill time
                _btc_open = await self.db.get_btc_price_at(fill["timestamp"])
                await self.db.save_live_trade(
                    timestamp=fill["timestamp"],
                    market_slug=fill["market_slug"],
                    side=fill["side"],
                    token_id=fill["token_id"],
                    amount_usdc=fill["amount_usdc"],
                    order_id=fill["order_id"],
                    status="filled",
                    success=True,
                    entry_price=fill["entry_price"],
                    trade_tag="maker_fill",
                    fill_price=fill["entry_price"],  # maker fills at limit price
                    btc_price_at_open=_btc_open,
                )

            logger.info(
                "Maker fill sync: inserted %d new maker fills (of %d total maker trades)",
                len(new_fills), len(maker_trades),
            )

            # Now settle any unsettled maker fills using Gamma API resolutions
            await self._settle_maker_fills_from_gamma()

        except Exception:
            logger.exception("Error in maker fill sync")

    async def _settle_maker_fills_from_gamma(self) -> None:
        """Settle unsettled maker fills by querying Gamma API for resolutions."""
        import aiohttp as _aiohttp

        unsettled = await self.db.get_unsettled_live_trades()
        maker_unsettled = [r for r in unsettled if r.get("trade_tag") == "maker_fill"]
        if not maker_unsettled:
            return

        slugs_to_check = {r["market_slug"] for r in maker_unsettled}
        resolutions: dict[str, str] = {}

        async with _aiohttp.ClientSession(
            timeout=_aiohttp.ClientTimeout(total=10)
        ) as session:
            for slug in slugs_to_check:
                try:
                    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                    events = data if isinstance(data, list) else [data]
                    if not events:
                        continue
                    market = events[0].get("markets", [{}])[0]
                    if not market.get("closed"):
                        continue

                    outcomes = market.get("outcomes", [])
                    if isinstance(outcomes, str):
                        import json as _json
                        outcomes = _json.loads(outcomes)
                    prices = market.get("outcomePrices", [])
                    if isinstance(prices, str):
                        import json as _json
                        prices = _json.loads(prices)

                    up_idx = outcomes.index("Up") if "Up" in outcomes else None
                    if up_idx is not None:
                        up_price = float(prices[up_idx])
                        if up_price >= 0.99:
                            resolutions[slug] = "UP"
                        elif up_price <= 0.01:
                            resolutions[slug] = "DOWN"

                    await asyncio.sleep(0.2)
                except Exception:
                    continue

        settled_count = 0
        for row in maker_unsettled:
            slug = row["market_slug"]
            if slug not in resolutions:
                continue

            winner = resolutions[slug]
            side = row["side"]
            amount = row["amount_usdc"]
            entry_price = row.get("entry_price") or 0.0

            won = side == winner
            outcome = "WIN" if won else "LOSS"

            if entry_price > 0:
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

            await self.db.update_live_trade(
                row["id"], outcome, round(pnl, 4), int(time.time() * 1000)
            )
            settled_count += 1

        if settled_count:
            logger.info("Settled %d maker fills from Gamma API", settled_count)

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

            btc_went_up = btc_end > btc_start  # strict: flat = DOWN on Polymarket

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
        """Periodically log and send trading stats (every 60 minutes)."""
        while self._running:
            await asyncio.sleep(3600)  # 60 minutes
            try:
                if self.paper_trader:
                    stats = await self.paper_trader.get_stats()
                    report = self.paper_trader.format_stats_report(stats)
                    logger.info("\n%s", report)

                    # Layer 3: Balance sanity check (must run BEFORE bankroll sync)
                    if self.live_trader and self.live_trader.is_active:
                        await self._balance_sanity_check()

                    # Sync bankroll from real CLOB balance
                    if self.live_trader and self.live_trader.is_active:
                        real_bal = await self.live_trader.get_balance()
                        if real_bal is not None and real_bal > 0:
                            old_br = self.live_trader.bankroll
                            self.live_trader.bankroll = real_bal
                            if real_bal > self.live_trader._max_bankroll:
                                self.live_trader._max_bankroll = real_bal
                            logger.info(
                                "Bankroll synced from CLOB: $%.2f (was $%.2f)",
                                real_bal, old_br,
                            )

                    # Settle any stale unsettled live trades (Gamma API)
                    if self.live_trader and self.live_trader.is_active:
                        await self._settle_stale_live_trades()

                    # Layer 2: Gamma reconciliation for already-settled trades
                    if self.live_trader and self.live_trader.is_active:
                        await self._gamma_reconciliation()

                    # Sync maker fills from CLOB API
                    if self.live_trader and self.live_trader.is_active:
                        await self._sync_maker_fills()

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
