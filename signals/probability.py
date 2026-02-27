"""Probability model that combines features into P(up) estimate."""

import logging

from data.models import FeatureVector

logger = logging.getLogger(__name__)


class ProbabilityModel:
    """Combine normalised feature signals into a single P(up) probability."""

    DEFAULT_WEIGHTS: dict[str, float] = {
        "obi": 0.05,
        "taker": 0.25,
        "momentum": 0.05,
        "rsi": 0.10,
        "vwap": 0.10,
        "funding": 0.10,
        "volume_zscore": 0.15,
        "regime": 0.20,
    }

    # Signal categories for regime-adaptive weighting.
    # In trending markets, trend-following signals are boosted and
    # mean-reverting signals are reduced.  This prevents the model from
    # betting against strong directional moves (e.g., OBI dip-buying
    # pushing the model toward UP during a sell-off).
    TREND_FOLLOWING: set[str] = {"momentum", "regime", "taker", "rsi"}
    MEAN_REVERTING: set[str] = {"obi", "vwap", "funding"}
    # volume_zscore is intentionally neutral — it's directional via taker.

    # Regime strength beyond which adaptive weighting kicks in.
    _REGIME_ADAPT_THRESHOLD: float = 0.15

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
    def _normalize_volume_direction(volume_zscore: float, taker_ratio: float) -> float:
        """Volume-weighted direction: high volume amplifies taker pressure.

        Above-average volume with buying pressure → bullish signal.
        Above-average volume with selling pressure → bearish signal.
        Below-average volume → near-zero signal (no conviction).

        This replaces the old non-directional volume z-score which blindly
        pushed toward UP when volume was high, regardless of direction.
        """
        # Only above-average volume contributes; below-average → 0
        vol_mag = max(0.0, min(0.5, volume_zscore / 4.0 * 0.5))
        # Direction comes from taker flow
        direction = max(-1.0, min(1.0, taker_ratio))
        return vol_mag * direction

    @staticmethod
    def _normalize_regime(ema_cross: float, bb_position: float) -> float:
        """Trend regime from EMA cross and Bollinger Band position.

        Provides multi-window trend memory that per-window signals lack.
        EMA cross (EMA9 vs EMA21 gap) captures trend direction; BB position
        (where price sits within Bollinger Bands) captures range context.

        Returns [-0.5, 0.5]: negative = bearish regime, positive = bullish.
        """
        # EMA cross: (fast-slow)/slow * 100 — clip at +-1.0% separation
        ema_signal = max(-0.5, min(0.5, ema_cross / 1.0 * 0.5))
        # BB position: [0, 1] with 0.5 = middle band — shift to [-0.5, 0.5]
        bb_signal = max(-0.5, min(0.5, bb_position - 0.5))
        # 60% EMA cross (trend direction) + 40% BB position (range placement)
        return 0.6 * ema_signal + 0.4 * bb_signal

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
            "volume_zscore": self._normalize_volume_direction(
                features.volume_zscore, features.taker_ratio
            ),
            "regime": self._normalize_regime(features.ema_cross, features.bb_position),
        }

        # ------------------------------------------------------------------
        # Regime-adaptive weighting: in trending markets, boost trend-following
        # signals and reduce mean-reverting signals.  This prevents the model
        # from fighting strong directional moves (e.g., betting UP during a
        # sell-off because OBI shows dip-buying or VWAP says "oversold").
        #
        # In a strong bearish regime the model will now lean more into
        # taker/momentum/regime/RSI (which point DOWN) and less into
        # OBI/VWAP/funding (which often point UP as a contrarian trap).
        # Same number of trades, better directional accuracy.
        # ------------------------------------------------------------------
        regime_strength = abs(signals["regime"])

        if regime_strength > self._REGIME_ADAPT_THRESHOLD:
            # Linear scale: 0 at threshold → 1.0 at regime = 0.5
            adapt_range = 0.5 - self._REGIME_ADAPT_THRESHOLD
            adapt_factor = min(1.0, (regime_strength - self._REGIME_ADAPT_THRESHOLD) / adapt_range)

            # Trend signals get up to 1.5x, mean-revert down to 0.4x
            trend_mult = 1.0 + 0.5 * adapt_factor     # [1.0, 1.5]
            revert_mult = 1.0 - 0.6 * adapt_factor    # [1.0, 0.4]

            adjusted = {}
            for k, w in self.weights.items():
                if k in self.TREND_FOLLOWING:
                    adjusted[k] = w * trend_mult
                elif k in self.MEAN_REVERTING:
                    adjusted[k] = w * revert_mult
                else:
                    adjusted[k] = w  # neutral signals unchanged

            # Re-normalize so weights sum to 1.0
            total_w = sum(adjusted.values())
            if total_w > 0:
                adjusted = {k: v / total_w for k, v in adjusted.items()}

            weights_to_use = adjusted
        else:
            weights_to_use = self.weights

        weighted_sum = sum(weights_to_use[k] * signals[k] for k in signals)
        p_up_raw = 0.5 + weighted_sum

        # Dampen confidence: shrink toward 0.5
        p_up = 0.5 + self.confidence_dampen * (p_up_raw - 0.5)

        p_up = max(0.05, min(0.95, p_up))
        logger.debug(
            "predict: regime_str=%.3f adapt=%s signals=%s  weighted_sum=%.4f  p_up=%.4f",
            regime_strength,
            "yes" if regime_strength > self._REGIME_ADAPT_THRESHOLD else "no",
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
            "volume_zscore": self._normalize_volume_direction(
                features.volume_zscore, features.taker_ratio
            ),
            "regime": self._normalize_regime(features.ema_cross, features.bb_position),
        }
        logger.debug("signal_breakdown: %s", breakdown)
        return breakdown
