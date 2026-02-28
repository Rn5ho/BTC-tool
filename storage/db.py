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
                     entry_spread, midpoint_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ) -> None:
        """Insert a market price snapshot."""
        try:
            ts = int(time.time())
            await self._db.execute(
                """
                INSERT INTO market_snapshots
                    (timestamp, slug, up_price, down_price, btc_price,
                     up_best_bid, up_best_ask, up_spread,
                     down_best_bid, down_best_ask, down_spread)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, slug, up_price, down_price, btc_price,
                 up_best_bid, up_best_ask, up_spread,
                 down_best_bid, down_best_ask, down_spread),
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
        """Compute aggregate trading statistics across all paper trades."""
        stats = {
            "total_trades": 0,
            "settled_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_edge": 0.0,
            "avg_pnl_per_trade": 0.0,
        }
        try:
            # Total trades
            cursor = await self._db.execute("SELECT COUNT(*) FROM paper_trades")
            row = await cursor.fetchone()
            stats["total_trades"] = row[0]

            # Settled trades
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE outcome IS NOT NULL"
            )
            row = await cursor.fetchone()
            stats["settled_trades"] = row[0]

            # Wins
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE outcome = 'WIN'"
            )
            row = await cursor.fetchone()
            stats["wins"] = row[0]

            # Losses
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE outcome = 'LOSS'"
            )
            row = await cursor.fetchone()
            stats["losses"] = row[0]

            # Win rate
            if stats["settled_trades"] > 0:
                stats["win_rate"] = stats["wins"] / stats["settled_trades"]

            # Total PnL
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) FROM paper_trades WHERE pnl IS NOT NULL"
            )
            row = await cursor.fetchone()
            stats["total_pnl"] = row[0]

            # Average edge
            cursor = await self._db.execute(
                "SELECT COALESCE(AVG(edge), 0.0) FROM paper_trades"
            )
            row = await cursor.fetchone()
            stats["avg_edge"] = row[0]

            # Average PnL per settled trade
            if stats["settled_trades"] > 0:
                stats["avg_pnl_per_trade"] = stats["total_pnl"] / stats["settled_trades"]

            logger.info("Trading stats: %s", stats)
        except Exception:
            logger.exception("Failed to compute trading stats")

        return stats
