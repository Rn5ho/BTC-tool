"""Async SQLite storage layer for candles, features, paper trades, and market snapshots."""

import logging
import time
from typing import Optional

import aiosqlite

from data.models import Candle, FeatureVector, PaperTrade

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database for persisting trading data."""

    def __init__(self, db_path: str = "btc_edge.db") -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the connection and create tables + indexes."""
        try:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            logger.info("Connected to SQLite database at %s", self.db_path)

            await self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    taker_buy_volume REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS feature_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    obi REAL,
                    taker_ratio REAL,
                    momentum_1m REAL,
                    momentum_5m REAL,
                    rsi REAL,
                    vwap_deviation REAL,
                    bb_position REAL,
                    ema_cross REAL,
                    funding_rate REAL,
                    volume_zscore REAL,
                    atr REAL,
                    prob_up REAL,
                    market_slug TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    market_slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    our_prob REAL NOT NULL,
                    market_prob REAL NOT NULL,
                    edge REAL NOT NULL,
                    size_usdc REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    outcome TEXT,
                    pnl REAL,
                    settled_at INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    up_price REAL NOT NULL,
                    down_price REAL NOT NULL,
                    btc_price REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(timestamp);
                CREATE INDEX IF NOT EXISTS idx_features_ts ON feature_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON paper_trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_slug ON paper_trades(market_slug);
                CREATE INDEX IF NOT EXISTS idx_snapshots_slug ON market_snapshots(slug);

                CREATE TABLE IF NOT EXISTS live_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    market_slug TEXT NOT NULL,
                    side TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    amount_usdc REAL NOT NULL,
                    order_id TEXT,
                    status TEXT NOT NULL,
                    success INTEGER NOT NULL DEFAULT 0,
                    response_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_live_trades_ts ON live_trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_live_trades_slug ON live_trades(market_slug);
                CREATE INDEX IF NOT EXISTS idx_live_trades_outcome ON live_trades(success, outcome);

                CREATE TABLE IF NOT EXISTS skipped_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    market_slug TEXT NOT NULL,
                    skip_reason TEXT NOT NULL,
                    btc_price REAL,
                    regime_state TEXT,
                    regime_strength REAL,
                    model_confidence REAL,
                    entry_price REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_skipped_ts ON skipped_windows(timestamp);
                """
            )
            await self._db.commit()
            logger.info("Database tables and indexes created successfully")

            # Run migrations for spread tracking columns
            await self._migrate_spread_columns()
        except Exception:
            logger.exception("Failed to initialize database")
            raise

    async def _migrate_spread_columns(self) -> None:
        """Add spread tracking columns to existing tables (safe to run repeatedly)."""
        alter_statements = [
            # market_snapshots spread columns
            ("market_snapshots", "up_best_bid", "REAL"),
            ("market_snapshots", "up_best_ask", "REAL"),
            ("market_snapshots", "up_spread", "REAL"),
            ("market_snapshots", "down_best_bid", "REAL"),
            ("market_snapshots", "down_best_ask", "REAL"),
            ("market_snapshots", "down_spread", "REAL"),
            # paper_trades spread columns
            ("paper_trades", "entry_spread", "REAL"),
            ("paper_trades", "midpoint_price", "REAL"),
            # live_trades outcome tracking columns
            ("live_trades", "outcome", "TEXT"),
            ("live_trades", "pnl", "REAL"),
            ("live_trades", "entry_price", "REAL"),
            ("live_trades", "settled_at", "INTEGER"),
            # trade_tag for exploration vs normal trades
            ("paper_trades", "trade_tag", "TEXT"),
            ("live_trades", "trade_tag", "TEXT"),
            # regime detection columns
            ("paper_trades", "regime_state", "TEXT"),
            ("paper_trades", "regime_strength", "REAL"),
            ("live_trades", "regime_state", "TEXT"),
            ("live_trades", "regime_strength", "REAL"),
            # early exit data collection (2026-03-03)
            ("live_trades", "max_bid_during_window", "REAL"),
            ("live_trades", "exit_threshold_used", "REAL"),
            # order book depth sizes (2026-03-03)
            ("market_snapshots", "up_bid_size", "REAL"),
            ("market_snapshots", "up_ask_size", "REAL"),
            ("market_snapshots", "down_bid_size", "REAL"),
            ("market_snapshots", "down_ask_size", "REAL"),
            # extended data collection (2026-03-03)
            ("live_trades", "btc_price_at_open", "REAL"),
            ("live_trades", "settlement_price", "REAL"),
            ("live_trades", "fill_price", "REAL"),
            ("live_trades", "regime_direction_pct", "REAL"),
            ("live_trades", "regime_momentum_score", "REAL"),
            ("live_trades", "regime_ema_slope", "REAL"),
            ("live_trades", "regime_price_vs_ema", "REAL"),
            ("live_trades", "regime_ema_cross", "REAL"),
            ("live_trades", "model_confidence", "REAL"),
        ]
        for table, column, col_type in alter_statements:
            try:
                await self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
            except Exception:
                pass  # Column already exists
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            try:
                await self._db.close()
                logger.info("Database connection closed")
            except Exception:
                logger.exception("Error closing database connection")
            finally:
                self._db = None

    # ------------------------------------------------------------------
    # Inserts
    # ------------------------------------------------------------------

    async def save_candle(self, candle: Candle) -> None:
        """Insert a single candle row."""
        try:
            await self._db.execute(
                """
                INSERT INTO candles (timestamp, open, high, low, close, volume, taker_buy_volume, trades)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candle.timestamp,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.taker_buy_volume,
                    candle.trades,
                ),
            )
            await self._db.commit()
            logger.debug("Saved candle at timestamp %d", candle.timestamp)
        except Exception:
            logger.exception("Failed to save candle at timestamp %s", candle.timestamp)
            raise

    async def save_feature_snapshot(
        self, features: FeatureVector, prob_up: float, market_slug: str
    ) -> None:
        """Insert a feature-vector snapshot."""
        try:
            await self._db.execute(
                """
                INSERT INTO feature_snapshots
                    (timestamp, obi, taker_ratio, momentum_1m, momentum_5m,
                     rsi, vwap_deviation, bb_position, ema_cross,
                     funding_rate, volume_zscore, atr, prob_up, market_slug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    features.timestamp,
                    features.obi,
                    features.taker_ratio,
                    features.momentum_1m,
                    features.momentum_5m,
                    features.rsi,
                    features.vwap_deviation,
                    features.bb_position,
                    features.ema_cross,
                    features.funding_rate,
                    features.volume_zscore,
                    features.atr,
                    prob_up,
                    market_slug,
                ),
            )
            await self._db.commit()
            logger.debug(
                "Saved feature snapshot at timestamp %d (prob_up=%.4f, slug=%s)",
                features.timestamp,
                prob_up,
                market_slug,
            )
        except Exception:
            logger.exception("Failed to save feature snapshot")
            raise

    async def save_paper_trade(self, trade: PaperTrade) -> None:
        """Insert a new paper trade."""
        try:
            await self._db.execute(
                """
                INSERT INTO paper_trades
                    (timestamp, market_slug, side, our_prob, market_prob,
                     edge, size_usdc, entry_price, outcome, pnl, settled_at,
                     entry_spread, midpoint_price, trade_tag,
                     regime_state, regime_strength)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.timestamp,
                    trade.market_slug,
                    trade.side,
                    trade.our_prob,
                    trade.market_prob,
                    trade.edge,
                    trade.size_usdc,
                    trade.entry_price,
                    trade.outcome,
                    trade.pnl,
                    trade.settled_at,
                    trade.entry_spread,
                    trade.midpoint_price,
                    trade.trade_tag,
                    trade.regime_state,
                    trade.regime_strength,
                ),
            )
            await self._db.commit()
            logger.info(
                "Saved paper trade: %s %s edge=%.4f size=%.2f spread=%s",
                trade.side,
                trade.market_slug,
                trade.edge,
                trade.size_usdc,
                f"{trade.entry_spread:.4f}" if trade.entry_spread else "N/A",
            )
        except Exception:
            logger.exception("Failed to save paper trade")
            raise

    async def update_paper_trade(
        self, trade_id: int, outcome: str, pnl: float, settled_at: int
    ) -> None:
        """Update an existing paper trade with settlement data."""
        try:
            await self._db.execute(
                """
                UPDATE paper_trades
                SET outcome = ?, pnl = ?, settled_at = ?
                WHERE id = ?
                """,
                (outcome, pnl, settled_at, trade_id),
            )
            await self._db.commit()
            logger.info(
                "Updated paper trade %d: outcome=%s pnl=%.4f",
                trade_id,
                outcome,
                pnl,
            )
        except Exception:
            logger.exception("Failed to update paper trade %d", trade_id)
            raise

    async def save_skipped_window(
        self,
        timestamp: int,
        market_slug: str,
        skip_reason: str,
        btc_price: Optional[float] = None,
        regime_state: Optional[str] = None,
        regime_strength: Optional[float] = None,
        model_confidence: Optional[float] = None,
        entry_price: Optional[float] = None,
    ) -> None:
        """Record a skipped trading window with reason."""
        try:
            await self._db.execute(
                """
                INSERT INTO skipped_windows
                    (timestamp, market_slug, skip_reason, btc_price,
                     regime_state, regime_strength, model_confidence, entry_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp, market_slug, skip_reason, btc_price,
                 regime_state, regime_strength, model_confidence, entry_price),
            )
            await self._db.commit()
        except Exception:
            logger.exception("Failed to save skipped window")

    async def save_live_trade(
        self,
        timestamp: int,
        market_slug: str,
        side: str,
        token_id: str,
        amount_usdc: float,
        order_id: Optional[str],
        status: str,
        success: bool,
        response_json: Optional[str] = None,
        entry_price: float = 0.0,
        trade_tag: Optional[str] = None,
        regime_state: Optional[str] = None,
        regime_strength: Optional[float] = None,
        btc_price_at_open: Optional[float] = None,
        fill_price: Optional[float] = None,
        regime_direction_pct: Optional[float] = None,
        regime_momentum_score: Optional[float] = None,
        regime_ema_slope: Optional[float] = None,
        regime_price_vs_ema: Optional[float] = None,
        regime_ema_cross: Optional[float] = None,
        model_confidence: Optional[float] = None,
    ) -> Optional[int]:
        """Insert a live trade record. Returns the row id on success."""
        try:
            cursor = await self._db.execute(
                """
                INSERT INTO live_trades
                    (timestamp, market_slug, side, token_id, amount_usdc,
                     order_id, status, success, response_json, entry_price,
                     trade_tag, regime_state, regime_strength,
                     btc_price_at_open, fill_price,
                     regime_direction_pct, regime_momentum_score,
                     regime_ema_slope, regime_price_vs_ema, regime_ema_cross,
                     model_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    market_slug,
                    side,
                    token_id,
                    amount_usdc,
                    order_id,
                    status,
                    1 if success else 0,
                    response_json,
                    entry_price,
                    trade_tag,
                    regime_state,
                    regime_strength,
                    btc_price_at_open,
                    fill_price,
                    regime_direction_pct,
                    regime_momentum_score,
                    regime_ema_slope,
                    regime_price_vs_ema,
                    regime_ema_cross,
                    model_confidence,
                ),
            )
            await self._db.commit()
            logger.info(
                "Saved live trade: %s %s $%.2f order=%s success=%s",
                side, market_slug, amount_usdc, order_id, success,
            )
            return cursor.lastrowid
        except Exception:
            logger.exception("Failed to save live trade")
            return None

    async def get_live_trade_stats(self) -> dict:
        """Compute aggregate stats for live trades."""
        stats = {"total": 0, "successful": 0, "failed": 0, "total_amount": 0.0}
        try:
            cursor = await self._db.execute("SELECT COUNT(*) FROM live_trades")
            row = await cursor.fetchone()
            stats["total"] = row[0]

            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM live_trades WHERE success = 1"
            )
            row = await cursor.fetchone()
            stats["successful"] = row[0]

            stats["failed"] = stats["total"] - stats["successful"]

            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(amount_usdc), 0.0) FROM live_trades WHERE success = 1"
            )
            row = await cursor.fetchone()
            stats["total_amount"] = row[0]
        except Exception:
            logger.exception("Failed to compute live trade stats")
        return stats

    async def update_live_trade(
        self,
        trade_id: int,
        outcome: str,
        pnl: float,
        settled_at: int,
        max_bid_during_window: float | None = None,
        exit_threshold_used: float | None = None,
        settlement_price: float | None = None,
    ) -> None:
        """Update a live trade with settlement data."""
        try:
            await self._db.execute(
                """
                UPDATE live_trades
                SET outcome = ?, pnl = ?, settled_at = ?,
                    max_bid_during_window = ?, exit_threshold_used = ?,
                    settlement_price = ?
                WHERE id = ?
                """,
                (outcome, pnl, settled_at,
                 max_bid_during_window, exit_threshold_used,
                 settlement_price, trade_id),
            )
            await self._db.commit()
            logger.info(
                "Updated live trade %d: outcome=%s pnl=%.4f",
                trade_id, outcome, pnl,
            )
        except Exception:
            logger.exception("Failed to update live trade %d", trade_id)

    async def get_unsettled_live_trades(self) -> list[dict]:
        """Return all successful live trades that have not yet been settled."""
        try:
            cursor = await self._db.execute(
                "SELECT * FROM live_trades WHERE success = 1 AND outcome IS NULL"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch unsettled live trades")
            return []

    async def get_live_trading_stats_full(self) -> dict:
        """Compute full live trading stats including win/loss/P&L.

        Includes ALL outcomes: WIN, LOSS, EARLY_EXIT, maker fills.
        Win rate is calculated from WIN/(WIN+LOSS) only — early exits excluded
        from WR denominator since they are neither a full win nor a full loss.
        Total P&L always includes every settled trade (taker + maker + early exit).
        """
        stats = {
            "total": 0, "successful": 0, "failed": 0, "total_amount": 0.0,
            "settled": 0, "wins": 0, "losses": 0, "early_exits": 0,
            "maker_fills": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "early_exit_pnl": 0.0, "maker_pnl": 0.0,
        }
        try:
            # Order counts
            cursor = await self._db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful,
                    COALESCE(SUM(CASE WHEN success = 1 THEN amount_usdc ELSE 0 END), 0.0) as total_amount
                FROM live_trades
                """
            )
            row = await cursor.fetchone()
            stats["total"] = row[0]
            stats["successful"] = row[1]
            stats["failed"] = stats["total"] - stats["successful"]
            stats["total_amount"] = row[2]

            # Settlement stats — all in one query
            cursor = await self._db.execute(
                """
                SELECT
                    SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) as settled,
                    SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome = 'EARLY_EXIT' THEN 1 ELSE 0 END) as early_exits,
                    SUM(CASE WHEN trade_tag = 'maker_fill' AND outcome IS NOT NULL THEN 1 ELSE 0 END) as maker_fills,
                    COALESCE(SUM(pnl), 0.0) as total_pnl,
                    COALESCE(SUM(CASE WHEN outcome = 'EARLY_EXIT' THEN pnl ELSE 0 END), 0.0) as early_exit_pnl,
                    COALESCE(SUM(CASE WHEN trade_tag = 'maker_fill' THEN pnl ELSE 0 END), 0.0) as maker_pnl
                FROM live_trades
                WHERE success = 1
                """
            )
            row = await cursor.fetchone()
            stats["settled"] = row[0] or 0
            stats["wins"] = row[1] or 0
            stats["losses"] = row[2] or 0
            stats["early_exits"] = row[3] or 0
            stats["maker_fills"] = row[4] or 0
            stats["total_pnl"] = row[5]
            stats["early_exit_pnl"] = row[6]
            stats["maker_pnl"] = row[7]

            # Win rate from W/L only (early exits excluded from denominator)
            wl_total = stats["wins"] + stats["losses"]
            if wl_total > 0:
                stats["win_rate"] = stats["wins"] / wl_total
        except Exception:
            logger.exception("Failed to compute full live trade stats")
        return stats

    async def restore_live_bankroll(self) -> float:
        """Return cumulative live P&L from DB (for bankroll restoration)."""
        try:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) FROM live_trades WHERE pnl IS NOT NULL"
            )
            row = await cursor.fetchone()
            return row[0]
        except Exception:
            logger.exception("Failed to restore live bankroll")
            return 0.0

    async def get_all_settled_live_trades(self) -> list[dict]:
        """Return ALL settled live trades: WIN, LOSS, EARLY_EXIT, maker fills.

        This is the canonical query for analysis/backtesting. Use this instead
        of writing raw SQL — it guarantees no outcomes are missed.
        """
        try:
            cursor = await self._db.execute(
                "SELECT * FROM live_trades "
                "WHERE success = 1 AND outcome IS NOT NULL AND pnl IS NOT NULL "
                "ORDER BY timestamp"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch all settled live trades")
            return []

    async def get_all_settled_paper_trades(self) -> list[dict]:
        """Return ALL settled paper trades: WIN, LOSS, EARLY_EXIT, regime_flip, etc.

        This is the canonical query for analysis/backtesting. Use this instead
        of writing raw SQL — it guarantees no outcomes are missed.
        """
        try:
            cursor = await self._db.execute(
                "SELECT * FROM paper_trades "
                "WHERE outcome IS NOT NULL AND pnl IS NOT NULL "
                "ORDER BY timestamp"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch all settled paper trades")
            return []

    async def save_market_snapshot(
        self,
        slug: str,
        up_price: float,
        down_price: float,
        btc_price: Optional[float] = None,
        up_best_bid: Optional[float] = None,
        up_best_ask: Optional[float] = None,
        up_spread: Optional[float] = None,
        down_best_bid: Optional[float] = None,
        down_best_ask: Optional[float] = None,
        down_spread: Optional[float] = None,
        up_bid_size: Optional[float] = None,
        up_ask_size: Optional[float] = None,
        down_bid_size: Optional[float] = None,
        down_ask_size: Optional[float] = None,
    ) -> None:
        """Insert a market price snapshot."""
        try:
            ts = int(time.time())
            await self._db.execute(
                """
                INSERT INTO market_snapshots
                    (timestamp, slug, up_price, down_price, btc_price,
                     up_best_bid, up_best_ask, up_spread,
                     down_best_bid, down_best_ask, down_spread,
                     up_bid_size, up_ask_size, down_bid_size, down_ask_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, slug, up_price, down_price, btc_price,
                 up_best_bid, up_best_ask, up_spread,
                 down_best_bid, down_best_ask, down_spread,
                 up_bid_size, up_ask_size, down_bid_size, down_ask_size),
            )
            await self._db.commit()
            logger.debug(
                "Saved market snapshot for %s: up=%.4f down=%.4f btc=%s spread=%s",
                slug,
                up_price,
                down_price,
                btc_price,
                f"{up_spread:.4f}/{down_spread:.4f}" if up_spread else "N/A",
            )
        except Exception:
            logger.exception("Failed to save market snapshot for %s", slug)
            raise

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def clear_paper_trades(self) -> int:
        """Delete all paper trades and return the number of rows removed."""
        try:
            cursor = await self._db.execute("DELETE FROM paper_trades")
            await self._db.commit()
            count = cursor.rowcount
            logger.info("Cleared %d paper trade(s) from database", count)
            return count
        except Exception:
            logger.exception("Failed to clear paper trades")
            return 0

    async def get_btc_price_at(self, timestamp_ms: int, tolerance_ms: int = 120000) -> Optional[float]:
        """Get BTC close price from the candle nearest to the given timestamp.

        Args:
            timestamp_ms: Target timestamp in milliseconds.
            tolerance_ms: Search window around the target (default 2 minutes).

        Returns:
            The close price of the nearest candle, or None if no candle found.
        """
        try:
            cursor = await self._db.execute(
                """SELECT close FROM candles
                   WHERE timestamp BETWEEN ? AND ?
                   ORDER BY ABS(timestamp - ?) LIMIT 1""",
                (timestamp_ms - tolerance_ms, timestamp_ms + tolerance_ms, timestamp_ms),
            )
            row = await cursor.fetchone()
            return row[0] if row else None
        except Exception:
            logger.exception("Failed to query BTC price at %d", timestamp_ms)
            return None

    async def get_unsettled_trades(self) -> list[dict]:
        """Return all paper trades that have not yet been settled."""
        try:
            cursor = await self._db.execute(
                "SELECT * FROM paper_trades WHERE outcome IS NULL"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch unsettled trades")
            return []

    async def get_trades_by_slug(self, slug: str) -> list[dict]:
        """Return all paper trades for a specific market slug."""
        try:
            cursor = await self._db.execute(
                "SELECT * FROM paper_trades WHERE market_slug = ?", (slug,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch trades for slug %s", slug)
            return []

    async def get_trading_stats(self) -> dict:
        """Compute aggregate trading statistics across all paper trades.

        Includes ALL outcomes: WIN, LOSS, EARLY_EXIT, regime_flip, exploration.
        Win rate is calculated from WIN/(WIN+LOSS) only — early exits excluded
        from WR denominator since they are neither a full win nor a full loss.
        Total P&L always includes every settled trade.
        """
        stats = {
            "total_trades": 0,
            "settled_trades": 0,
            "wins": 0,
            "losses": 0,
            "early_exits": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "early_exit_pnl": 0.0,
            "avg_edge": 0.0,
            "avg_pnl_per_trade": 0.0,
        }
        try:
            # All counts in one query
            cursor = await self._db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) as settled,
                    SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome = 'EARLY_EXIT' THEN 1 ELSE 0 END) as early_exits,
                    COALESCE(SUM(pnl), 0.0) as total_pnl,
                    COALESCE(SUM(CASE WHEN outcome = 'EARLY_EXIT' THEN pnl ELSE 0 END), 0.0) as early_exit_pnl,
                    COALESCE(AVG(edge), 0.0) as avg_edge
                FROM paper_trades
                """
            )
            row = await cursor.fetchone()

            stats["total_trades"] = row[0]
            stats["settled_trades"] = row[1]
            stats["wins"] = row[2]
            stats["losses"] = row[3]
            stats["early_exits"] = row[4]
            stats["total_pnl"] = row[5]
            stats["early_exit_pnl"] = row[6]
            stats["avg_edge"] = row[7]

            # Win rate from W/L only (early exits excluded from denominator)
            wl_total = stats["wins"] + stats["losses"]
            if wl_total > 0:
                stats["win_rate"] = stats["wins"] / wl_total

            # Average PnL per settled trade (all outcomes)
            if stats["settled_trades"] > 0:
                stats["avg_pnl_per_trade"] = stats["total_pnl"] / stats["settled_trades"]

            logger.info("Trading stats: %s", stats)
        except Exception:
            logger.exception("Failed to compute trading stats")

        return stats

    async def get_recent_live_trades(self, n: int = 5) -> list[dict]:
        """Return the last N settled live trades, most recent first."""
        try:
            cursor = await self._db.execute(
                "SELECT * FROM live_trades "
                "WHERE success = 1 AND outcome IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT ?",
                (n,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch recent live trades")
            return []

    async def get_today_live_trades(self) -> list[dict]:
        """Return all settled live trades from today (UTC)."""
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ms = int(start_of_day.timestamp() * 1000)
            cursor = await self._db.execute(
                "SELECT * FROM live_trades "
                "WHERE success = 1 AND outcome IS NOT NULL "
                "AND timestamp >= ? "
                "ORDER BY timestamp",
                (start_ms,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch today's live trades")
            return []
