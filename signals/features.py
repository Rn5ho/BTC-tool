"""Feature engineering: combines raw market data into a FeatureVector."""

import logging
import statistics
import time
from collections import deque
from typing import Optional

from data.models import AggTrade, Candle, FeatureVector, FundingInfo, OrderBook
from signals.indicators import TechnicalIndicators

logger = logging.getLogger(__name__)


class FeatureEngine:
    """Combines order-book, trade, funding, and candle data into features."""

    def __init__(self) -> None:
        self._funding_history: deque[float] = deque(maxlen=1000)
        self._obi_history: deque[float] = deque(maxlen=200)

    # ------------------------------------------------------------------
    # Order-book imbalance
    # ------------------------------------------------------------------
    def compute_order_book_imbalance(
        self, orderbook: OrderBook, levels: int = 10
    ) -> float:
        """Compute OBI from the top *levels* of the book.

        OBI = (bid_qty - ask_qty) / (bid_qty + ask_qty)
        Range: [-1, 1].  Positive means buying pressure.
        Returns 0.0 when the book is ``None`` or empty.
        """
        if orderbook is None:
            return 0.0

        top_bids = orderbook.bids[:levels]
        top_asks = orderbook.asks[:levels]

        if not top_bids and not top_asks:
            return 0.0

        bid_qty = sum(level.quantity for level in top_bids)
        ask_qty = sum(level.quantity for level in top_asks)
        total = bid_qty + ask_qty

        if total == 0.0:
            return 0.0

        obi = (bid_qty - ask_qty) / total
        self._obi_history.append(obi)
        return obi

    # ------------------------------------------------------------------
    # Taker buy / sell ratio
    # ------------------------------------------------------------------
    def compute_taker_ratio(
        self, trades: list[AggTrade], window_ms: int = 60_000
    ) -> float:
        """Net taker buy ratio over a rolling window.

        Filters trades within the last *window_ms* milliseconds.
        ratio = (buy_vol - sell_vol) / (buy_vol + sell_vol)
        Range: [-1, 1].  Positive means net buying.
        Returns 0.0 when there are no trades in the window.
        """
        if not trades:
            return 0.0

        cutoff = trades[-1].timestamp - window_ms
        recent = [t for t in trades if t.timestamp >= cutoff]

        if not recent:
            return 0.0

        taker_buy_vol = sum(t.quantity for t in recent if not t.is_buyer_maker)
        taker_sell_vol = sum(t.quantity for t in recent if t.is_buyer_maker)
        total = taker_buy_vol + taker_sell_vol

        if total == 0.0:
            return 0.0

        return (taker_buy_vol - taker_sell_vol) / total

    # ------------------------------------------------------------------
    # Funding-rate z-score
    # ------------------------------------------------------------------
    def compute_funding_zscore(self, funding: Optional[FundingInfo]) -> float:
        """Z-score of the current funding rate relative to recent history.

        Returns 0.0 when *funding* is ``None`` or fewer than 10 samples
        have been collected.
        """
        if funding is None:
            return 0.0

        self._funding_history.append(funding.funding_rate)

        if len(self._funding_history) < 10:
            return 0.0

        mean = statistics.mean(self._funding_history)
        std = statistics.pstdev(self._funding_history)

        if std == 0.0:
            return 0.0

        return (funding.funding_rate - mean) / std

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def compute_features(
        self,
        candles: list[Candle],
        orderbook: Optional[OrderBook],
        trades: list[AggTrade],
        funding: Optional[FundingInfo],
    ) -> FeatureVector:
        """Produce a complete :class:`FeatureVector` from raw market data."""
        obi = self.compute_order_book_imbalance(orderbook) if orderbook else 0.0
        taker_ratio = self.compute_taker_ratio(trades)

        momentum_1m = TechnicalIndicators.momentum(candles, lookback=1)
        momentum_5m = TechnicalIndicators.momentum(candles, lookback=5)
        rsi = TechnicalIndicators.rsi(candles, period=9)
        vwap_dev = TechnicalIndicators.vwap_deviation(candles)
        bb_pos = TechnicalIndicators.bb_position(candles)
        ema_cross = TechnicalIndicators.ema_cross_signal(candles)
        funding_zscore = self.compute_funding_zscore(funding)
        vol_zscore = TechnicalIndicators.volume_zscore(candles)
        atr = TechnicalIndicators.atr(candles)

        return FeatureVector(
            timestamp=int(time.time() * 1000),
            obi=obi,
            taker_ratio=taker_ratio,
            momentum_1m=momentum_1m,
            momentum_5m=momentum_5m,
            rsi=rsi,
            vwap_deviation=vwap_dev,
            bb_position=bb_pos,
            ema_cross=ema_cross,
            funding_rate=funding_zscore,
            volume_zscore=vol_zscore,
            atr=atr,
        )
