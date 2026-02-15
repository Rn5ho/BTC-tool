"""Probability model that combines features into P(up) estimate."""

import logging

from data.models import FeatureVector

logger = logging.getLogger(__name__)


class ProbabilityModel:
    """Combine normalised feature signals into a single P(up) probability."""

    DEFAULT_WEIGHTS: dict[str, float] = {
        "obi": 0.25,
        "taker": 0.25,
        "momentum": 0.15,
        "rsi": 0.15,
        "vwap": 0.10,
        "funding": 0.10,
    }

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = weights if weights is not None else dict(self.DEFAULT_WEIGHTS)
        logger.info("ProbabilityModel initialised with weights: %s", self.weights)

    # ------------------------------------------------------------------
    # Normalisation helpers – each returns a value in [-0.5, 0.5]
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_obi(obi: float) -> float:
        """OBI is already [-1, 1]; scale to [-0.5, 0.5]."""
        return obi * 0.5

    @staticmethod
    def _normalize_taker(taker_ratio: float) -> float:
        """Taker ratio is already [-1, 1]; scale to [-0.5, 0.5]."""
        return taker_ratio * 0.5

    @staticmethod
    def _normalize_momentum(momentum_1m: float, momentum_5m: float) -> float:
        """Combine 1m and 5m momentum, clip extremes, scale to [-0.5, 0.5].

        2% moves are considered extreme at the 5-minute time-frame.
        """
        combined = 0.6 * momentum_1m + 0.4 * momentum_5m
        clipped = max(-0.02, min(0.02, combined))
        return clipped / 0.02 * 0.5

    @staticmethod
    def _normalize_rsi(rsi: float) -> float:
        """Map RSI (0-100) to a directional signal in [-0.5, 0.5].

        High RSI indicates momentum may continue at the 5-min scale.
        """
        signal = (rsi - 50.0) / 100.0
        return max(-0.5, min(0.5, signal))

    @staticmethod
    def _normalize_vwap(vwap_deviation: float) -> float:
        """Positive deviation = above VWAP (bullish).

        Clip to [-0.01, 0.01] and scale to [-0.5, 0.5].
        """
        clipped = max(-0.01, min(0.01, vwap_deviation))
        return clipped / 0.01 * 0.5

    @staticmethod
    def _normalize_funding(funding_zscore: float) -> float:
        """High positive funding = overleveraged longs = bearish (mean reversion).

        Invert the signal: scale by 4.0 standard-deviations, then negate.
        """
        return max(-0.5, min(0.5, -funding_zscore / 4.0 * 0.5))

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, features: FeatureVector) -> float:
        """Return P(up) in [0.05, 0.95] by combining weighted signals."""
        signals = {
            "obi": self._normalize_obi(features.obi),
            "taker": self._normalize_taker(features.taker_ratio),
            "momentum": self._normalize_momentum(
                features.momentum_1m, features.momentum_5m
            ),
            "rsi": self._normalize_rsi(features.rsi),
            "vwap": self._normalize_vwap(features.vwap_deviation),
            "funding": self._normalize_funding(features.funding_rate),
        }

        weighted_sum = sum(self.weights[k] * signals[k] for k in signals)
        p_up = 0.5 + weighted_sum

        p_up = max(0.05, min(0.95, p_up))
        logger.debug(
            "predict: signals=%s  weighted_sum=%.4f  p_up=%.4f",
            signals,
            weighted_sum,
            p_up,
        )
        return p_up

    def get_signal_breakdown(self, features: FeatureVector) -> dict[str, float]:
        """Return dict of each normalised signal for debugging/logging."""
        breakdown = {
            "obi": self._normalize_obi(features.obi),
            "taker": self._normalize_taker(features.taker_ratio),
            "momentum": self._normalize_momentum(
                features.momentum_1m, features.momentum_5m
            ),
            "rsi": self._normalize_rsi(features.rsi),
            "vwap": self._normalize_vwap(features.vwap_deviation),
            "funding": self._normalize_funding(features.funding_rate),
        }
        logger.debug("signal_breakdown: %s", breakdown)
        return breakdown
