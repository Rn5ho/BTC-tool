"""Market regime detection for trend/range classification.

Uses a 5-component weighted composite score to classify the current market
as trending_up, trending_down, or ranging. Used to suppress counter-trend
paper trades while tagging live trades for analysis.
"""

import logging

import numpy as np

from data.models import Candle, RegimeState
from signals.indicators import TechnicalIndicators

logger = logging.getLogger(__name__)

# Minimum candles needed for regime classification
_MIN_CANDLES = 30


class RegimeDetector:
    """Classifies market regime from candle data.

    Algorithm: 5-component weighted composite score in [-1, 1].
    - A: Directional consistency (25%) — % of last 20 candles moving same direction
    - B: Multi-timeframe momentum (25%) — 10m and 20m returns agreement
    - C: EMA-21 slope (20%) — trend acceleration
    - D: Price vs EMA-21 consistency (15%) — persistent one-sided positioning
    - E: EMA-9/21 cross (15%) — fast/slow cross direction
    """

    def __init__(self, trend_threshold: float = 0.30) -> None:
        self.trend_threshold = trend_threshold

    def classify(self, candles: list[Candle]) -> RegimeState:
        """Classify current market regime from candle data.

        Returns RegimeState with regime label and component scores.
        Falls back to 'ranging' with strength 0.0 if insufficient data.
        """
        if len(candles) < _MIN_CANDLES:
            return RegimeState(
                regime="ranging", strength=0.0,
                direction_pct=0.0, momentum_score=0.0,
                ema_slope=0.0, price_vs_ema=0.0, ema_cross=0.0,
            )

        try:
            a = self._direction_consistency(candles)
            b = self._momentum_agreement(candles)
            c = self._ema_slope(candles)
            d = self._price_vs_ema(candles)
            e = self._ema_cross(candles)
        except Exception:
            logger.exception("Error computing regime signals")
            return RegimeState(
                regime="ranging", strength=0.0,
                direction_pct=0.0, momentum_score=0.0,
                ema_slope=0.0, price_vs_ema=0.0, ema_cross=0.0,
            )

        # Weighted composite: each signal in [-1, 1]
        strength = 0.25 * a + 0.25 * b + 0.20 * c + 0.15 * d + 0.15 * e
        strength = max(-1.0, min(1.0, strength))

        if strength > self.trend_threshold:
            regime = "trending_up"
        elif strength < -self.trend_threshold:
            regime = "trending_down"
        else:
            regime = "ranging"

        return RegimeState(
            regime=regime,
            strength=round(strength, 4),
            direction_pct=round(a, 4),
            momentum_score=round(b, 4),
            ema_slope=round(c, 4),
            price_vs_ema=round(d, 4),
            ema_cross=round(e, 4),
        )

    @staticmethod
    def is_counter_trend(side: str, regime: RegimeState) -> bool:
        """Return True if side opposes a strong trend."""
        if regime.regime == "trending_up" and side == "DOWN":
            return True
        if regime.regime == "trending_down" and side == "UP":
            return True
        return False

    @staticmethod
    def get_trend_side(regime: RegimeState) -> str | None:
        """Return the trade side aligned with the trend, or None if ranging."""
        if regime.regime == "trending_up":
            return "UP"
        if regime.regime == "trending_down":
            return "DOWN"
        return None

    # ------------------------------------------------------------------
    # Component signals (each returns value in [-1, 1])
    # ------------------------------------------------------------------

    @staticmethod
    def _direction_consistency(candles: list[Candle]) -> float:
        """A: What fraction of the last 20 candles closed in the same direction?

        Returns [-1, 1]: +1 if all 20 were bullish, -1 if all bearish, 0 if split.
        """
        recent = candles[-20:]
        ups = sum(1 for c in recent if c.close > c.open)
        downs = len(recent) - ups
        # Normalize: 100% one-sided = 1.0, 50/50 = 0.0
        pct_up = ups / len(recent)
        # Map [0, 1] to [-1, 1]: 0.5 -> 0, 1.0 -> 1, 0.0 -> -1
        return (pct_up - 0.5) * 2.0

    @staticmethod
    def _momentum_agreement(candles: list[Candle]) -> float:
        """B: Do 10-candle and 20-candle momentum agree on direction?

        Returns [-1, 1]. Positive = both bullish, negative = both bearish.
        Mixed signals -> near zero.
        """
        mom10 = TechnicalIndicators.momentum(candles, lookback=10)
        mom20 = TechnicalIndicators.momentum(candles, lookback=20)

        if mom10 == 0.0 and mom20 == 0.0:
            return 0.0

        # Normalize each momentum by a typical BTC 1-hour range (~0.5%)
        # to get values roughly in [-1, 1]
        norm_factor = 0.005  # 0.5%
        m10 = max(-1.0, min(1.0, mom10 / norm_factor))
        m20 = max(-1.0, min(1.0, mom20 / norm_factor))

        # If they agree in direction, average them; if they disagree, dampen
        if (m10 > 0 and m20 > 0) or (m10 < 0 and m20 < 0):
            return (m10 + m20) / 2.0
        else:
            # Disagreement: return the smaller magnitude with its sign
            return (m10 + m20) / 4.0

    @staticmethod
    def _ema_slope(candles: list[Candle]) -> float:
        """C: Is EMA-21 accelerating upward or downward?

        Compares current EMA-21 to EMA-21 from 10 candles ago.
        Returns [-1, 1].
        """
        closes = [c.close for c in candles]
        if len(closes) < 31:
            return 0.0

        ema_now = TechnicalIndicators.ema(closes, 21)
        ema_past = TechnicalIndicators.ema(closes[:-10], 21)

        if ema_past == 0.0:
            return 0.0

        # Slope as percentage change
        slope_pct = (ema_now - ema_past) / ema_past
        # Normalize: 0.3% change over 10 candles = strong trend
        norm_factor = 0.003
        return max(-1.0, min(1.0, slope_pct / norm_factor))

    @staticmethod
    def _price_vs_ema(candles: list[Candle]) -> float:
        """D: How consistently is price above/below EMA-21?

        Checks last 10 candles. Returns [-1, 1].
        +1 = all 10 above, -1 = all 10 below, 0 = mixed.
        """
        closes = [c.close for c in candles]
        ema21 = TechnicalIndicators.ema(closes, 21)

        if ema21 == 0.0:
            return 0.0

        recent = candles[-10:]
        above = sum(1 for c in recent if c.close > ema21)
        below = len(recent) - above
        pct_above = above / len(recent)
        return (pct_above - 0.5) * 2.0

    @staticmethod
    def _ema_cross(candles: list[Candle]) -> float:
        """E: EMA-9/21 cross direction and magnitude.

        Uses the existing ema_cross_signal which returns
        (fast_ema - slow_ema) / slow_ema * 100.
        Normalized to [-1, 1].
        """
        signal = TechnicalIndicators.ema_cross_signal(candles, fast_period=9, slow_period=21)
        # ema_cross_signal returns percentage difference * 100
        # Typical range is [-0.1, 0.1] for BTC 1-min candles
        # Normalize so 0.05% difference = strong signal
        norm_factor = 0.05
        return max(-1.0, min(1.0, signal / norm_factor))
