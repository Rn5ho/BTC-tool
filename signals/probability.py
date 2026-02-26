"""Probability model that combines features into P(up) estimate."""

import logging

from data.models import FeatureVector

logger = logging.getLogger(__name__)


class ProbabilityModel:
    """Combine normalised feature signals into a single P(up) probability."""

    DEFAULT_WEIGHTS: dict[str, float] = {
        "obi": 0.15,
        "taker": 0.25,
        "momentum": 0.10,
        "rsi": 0.10,
        "vwap": 0.10,
        "funding": 0.10,
        "regime": 0.20,
    }

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        confidence_dampen: float = 1.0,
    ) -> None:
        self.weights = weights if weights is not None else dict(self.DEFAULT_WEIGHTS)
        # Shrink predictions toward 0.5 to counter overconfidence.
        # 1.0 = no dampening, 0.0 = always predict 50%.
        self.confidence_dampen = max(0.0, min(1.0, confidence_dampen))
        logger.info(
            "ProbabilityModel initialised with weights: %s, dampen=%.2f",
            self.weights,
            self.confidence_dampen,
        )

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

    @staticmethod
    def _normalize_regime(features: FeatureVector) -> float:
        """Market regime signal from trend indicators already in FeatureVector.

        Combines three trend measures into a single [-0.5, 0.5] signal:
        - EMA cross (EMA9 vs EMA21): direction and magnitude of short-term trend
        - BB position: where price sits in the Bollinger Band (momentum proxy)
        - 5-min momentum: multi-candle return direction

        Positive = bullish regime (pushes P(up) higher).
        Negative = bearish regime (pushes P(up) lower).
        """
        # EMA cross: (fast-slow)/slow * 100.  For BTC, +-0.3% is a clear trend.
        ema_signal = max(-1.0, min(1.0, features.ema_cross / 0.3))

        # BB position: 0-1 range (can exceed), center at 0.5.  Map to [-1, 1].
        bb_signal = max(-1.0, min(1.0, (features.bb_position - 0.5) * 2.0))

        # 5m momentum: return over 5 candles.  +-0.5% is significant for 5-min.
        mom_signal = max(-1.0, min(1.0, features.momentum_5m / 0.005))

        # Weighted combination: EMA cross is the strongest trend measure,
        # BB position captures momentum, 5m momentum captures recent direction.
        combined = 0.4 * ema_signal + 0.3 * bb_signal + 0.3 * mom_signal

        # Scale to [-0.5, 0.5] for consistency with other signals
        return max(-0.5, min(0.5, combined * 0.5))

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
            "regime": self._normalize_regime(features),
        }

        weighted_sum = sum(self.weights[k] * signals[k] for k in signals)
        p_up_raw = 0.5 + weighted_sum

        # Dampen confidence: shrink toward 0.5
        p_up = 0.5 + self.confidence_dampen * (p_up_raw - 0.5)

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
            "regime": self._normalize_regime(features),
        }
        logger.debug("signal_breakdown: %s", breakdown)
        return breakdown
