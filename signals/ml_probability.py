"""ML-based probability model for BTC 5-minute direction prediction.

Drop-in replacement for ProbabilityModel. Uses a trained scikit-learn model
loaded from disk. Same interface: predict(features) -> P(up).

The model was trained on 34,100 labeled 5-minute windows from 119 days of
Binance 1-minute candle data (Nov 2025 - Feb 2026). It uses 44 features
derived from price action, volume, momentum, and time-of-day patterns.
"""

import logging
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from data.models import Candle, FeatureVector

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"


class MLProbabilityModel:
    """Predict P(up) using a trained ML model.

    Maintains the same interface as ProbabilityModel so it can be used
    as a drop-in replacement in EdgeDetector and main.py.
    """

    def __init__(self, model_dir: Path | None = None) -> None:
        model_dir = model_dir or MODEL_DIR
        model_path = model_dir / "best_model.pkl"
        scaler_path = model_dir / "scaler.pkl"

        if not model_path.exists():
            raise FileNotFoundError(
                f"No trained model found at {model_path}. Run ml_pipeline.py first."
            )

        with open(model_path, "rb") as f:
            self._model = pickle.load(f)
        with open(scaler_path, "rb") as f:
            self._scaler = pickle.load(f)

        logger.info(
            "MLProbabilityModel loaded from %s (%s)",
            model_path,
            type(self._model).__name__,
        )

    def predict(self, features: FeatureVector) -> float:
        """Return P(up) in [0.05, 0.95] using the trained ML model."""
        feat_vec = self._extract_features_from_vector(features)
        if feat_vec is None:
            return 0.5

        if np.any(np.isnan(feat_vec)):
            logger.warning("NaN detected in feature vector (predict), returning 0.5")
            return 0.5

        feat_scaled = self._scaler.transform(feat_vec.reshape(1, -1))
        p_up = float(self._model.predict_proba(feat_scaled)[0, 1])
        p_up = max(0.05, min(0.95, p_up))

        logger.debug("ML predict: p_up=%.4f", p_up)
        return p_up

    def predict_from_candles(
        self, candles: list[Candle], window_start_ts: int
    ) -> float:
        """Predict P(up) directly from candle history.

        This is the preferred method when full candle history is available,
        as it computes all 44 features the model was trained on.
        """
        feat_vec = self._extract_features_from_candles(candles, window_start_ts)
        if feat_vec is None:
            return 0.5

        if np.any(np.isnan(feat_vec)):
            logger.warning("NaN detected in feature vector, returning 0.5")
            return 0.5

        feat_scaled = self._scaler.transform(feat_vec.reshape(1, -1))
        p_up = float(self._model.predict_proba(feat_scaled)[0, 1])
        p_up = max(0.05, min(0.95, p_up))

        logger.debug("ML predict (from candles): p_up=%.4f", p_up)
        return p_up

    def get_confidence(self, features: FeatureVector) -> float:
        """Return model confidence = |P(up) - 0.5|."""
        p_up = self.predict(features)
        return abs(p_up - 0.5)

    def get_signal_breakdown(self, features: FeatureVector) -> dict[str, float]:
        """Return a simplified signal breakdown for logging compatibility."""
        p_up = self.predict(features)
        return {
            "ml_p_up": p_up,
            "ml_confidence": abs(p_up - 0.5),
            "ml_side": "UP" if p_up > 0.5 else "DOWN",
        }

    # ------------------------------------------------------------------
    # Feature extraction
    # ------------------------------------------------------------------

    def _extract_features_from_vector(self, fv: FeatureVector) -> np.ndarray | None:
        """Build the 44-feature vector from a FeatureVector dataclass.

        Some features (multi-timeframe, candle patterns, time) require
        candle history. When only a FeatureVector is available, we fill
        what we can and zero-pad the rest. This is a fallback — prefer
        predict_from_candles() for full accuracy.
        """
        now = datetime.fromtimestamp(fv.timestamp / 1000, tz=timezone.utc)
        hour = now.hour + now.minute / 60.0
        dow = now.weekday()
        h = now.hour

        return np.array([
            # Momentum (we only have 1m and 5m from FeatureVector)
            fv.momentum_1m,       # momentum_1m
            0.0,                  # momentum_3m (not available)
            fv.momentum_5m,       # momentum_5m
            0.0,                  # momentum_10m
            0.0,                  # momentum_20m
            # RSI
            fv.rsi,               # rsi_9
            fv.rsi,               # rsi_14 (approximate)
            # Price vs averages
            fv.vwap_deviation,
            fv.bb_position,
            0.0,                  # bb_width
            fv.ema_cross,         # ema_cross_9_21
            0.0,                  # ema_cross_5_13
            0.0,                  # price_vs_ema9
            0.0,                  # price_vs_ema21
            # Volatility
            fv.atr,               # atr_14
            0.0,                  # atr_pct
            0.0,                  # recent_volatility
            0.0,                  # volatility_ratio
            # Volume
            fv.volume_zscore,
            0.0,                  # volume_trend
            fv.taker_ratio,       # taker_ratio_1m
            0.0,                  # taker_ratio_3m
            0.0,                  # taker_ratio_5m
            # Candle patterns
            0.0,                  # upper_wick_ratio
            0.0,                  # lower_wick_ratio
            0.0,                  # body_ratio
            0.0,                  # close_vs_open
            0.0,                  # avg_body_ratio_5
            # Lag features
            0.0, 0.0, 0.0, 0.0,  # prev directions
            0.0,                  # streak
            0.0,                  # mean_reversion_5m
            # Time features
            np.sin(2 * np.pi * hour / 24.0),
            np.cos(2 * np.pi * hour / 24.0),
            float(now.minute),
            1.0 if 0 <= h < 8 else 0.0,    # is_asia
            1.0 if 7 <= h < 16 else 0.0,   # is_europe
            1.0 if 13 <= h < 22 else 0.0,  # is_us
            np.sin(2 * np.pi * dow / 7.0),
            np.cos(2 * np.pi * dow / 7.0),
            # Microstructure
            0.0,                  # high_low_range_pct
            0.0,                  # close_position_in_range
        ])

    def _extract_features_from_candles(
        self, candles: list[Candle], window_start_ts: int
    ) -> np.ndarray | None:
        """Build the full 44-feature vector from candle history.

        Mirrors ml_pipeline.py:extract_features() exactly.
        """
        if len(candles) < 30:
            return None

        closes = np.array([c.close for c in candles])
        highs = np.array([c.high for c in candles])
        lows = np.array([c.low for c in candles])
        opens = np.array([c.open for c in candles])
        volumes = np.array([c.volume for c in candles])
        price = closes[-1]

        # Momentum
        def mom(n):
            if len(closes) < n + 1 or closes[-(n+1)] == 0:
                return 0.0
            return float((closes[-1] - closes[-(n+1)]) / closes[-(n+1)])

        # RSI
        def _rsi(period):
            if len(closes) < period + 1:
                return 50.0
            deltas = np.diff(closes)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)
            ag = np.mean(gains[:period])
            al = np.mean(losses[:period])
            for i in range(period, len(gains)):
                ag = (ag * (period - 1) + gains[i]) / period
                al = (al * (period - 1) + losses[i]) / period
            if al == 0:
                return 100.0
            return float(100.0 - 100.0 / (1.0 + ag / al))

        # EMA
        def _ema(arr, period):
            if len(arr) < period:
                return 0.0
            m = 2.0 / (period + 1)
            v = float(np.mean(arr[:period]))
            for i in range(period, len(arr)):
                v = (arr[i] - v) * m + v
            return v

        # VWAP
        typical = (highs + lows + closes) / 3.0
        tv = np.sum(volumes)
        vwap = np.sum(typical * volumes) / tv if tv > 0 else price
        vwap_dev = (price - vwap) / vwap if vwap > 0 else 0.0

        # Bollinger
        if len(closes) >= 20:
            bb_s = closes[-20:]
            bb_mid = np.mean(bb_s)
            bb_std = np.std(bb_s, ddof=0)
            bb_u = bb_mid + 2 * bb_std
            bb_l = bb_mid - 2 * bb_std
            bw = bb_u - bb_l
            bb_pos = (price - bb_l) / bw if bw > 0 else 0.5
            bb_width = bw / bb_mid if bb_mid > 0 else 0.0
        else:
            bb_pos, bb_width = 0.5, 0.0

        # EMAs
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        ema5 = _ema(closes, 5)
        ema13 = _ema(closes, 13)
        ec_9_21 = (ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0.0
        ec_5_13 = (ema5 - ema13) / ema13 * 100 if ema13 > 0 else 0.0
        pve9 = (price - ema9) / ema9 * 100 if ema9 > 0 else 0.0
        pve21 = (price - ema21) / ema21 * 100 if ema21 > 0 else 0.0

        # ATR
        def _atr(period=14):
            if len(closes) < period + 1:
                return 0.0
            tr = np.maximum(
                highs[1:] - lows[1:],
                np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
            )
            v = float(np.mean(tr[:period]))
            for i in range(period, len(tr)):
                v = (v * (period - 1) + tr[i]) / period
            return v

        atr_14 = _atr(14)
        atr_pct = atr_14 / price if price > 0 else 0.0
        recent_vol = float(np.std(closes[-5:])) if len(closes) >= 5 else 0.0

        if len(closes) >= 20:
            vr = np.std(closes[-5:]) / np.std(closes[-20:]) if np.std(closes[-20:]) > 0 else 1.0
        else:
            vr = 1.0

        # Volume
        if len(volumes) >= 20:
            vm = np.mean(volumes[-20:])
            vs = np.std(volumes[-20:], ddof=0)
            vol_z = (volumes[-1] - vm) / vs if vs > 0 else 0.0
        else:
            vol_z = 0.0

        if len(volumes) >= 10:
            vr_recent = np.mean(volumes[-5:])
            vr_older = np.mean(volumes[-10:-5])
            vol_trend = (vr_recent - vr_older) / vr_older if vr_older > 0 else 0.0
        else:
            vol_trend = 0.0

        def _taker_n(n):
            slc = candles[-n:] if len(candles) >= n else candles
            buy = sum(c.taker_buy_volume for c in slc)
            total = sum(c.volume for c in slc)
            if total == 0:
                return 0.0
            return (buy - (total - buy)) / total

        # Candle patterns
        last = candles[-1]
        cr = last.high - last.low
        if cr > 0:
            uw = (last.high - max(last.open, last.close)) / cr
            lw = (min(last.open, last.close) - last.low) / cr
            br = abs(last.close - last.open) / cr
        else:
            uw = lw = br = 0.0
        cvo = 1.0 if last.close > last.open else (-1.0 if last.close < last.open else 0.0)

        if len(candles) >= 5:
            brs = []
            for c in candles[-5:]:
                r = c.high - c.low
                brs.append(abs(c.close - c.open) / r if r > 0 else 0.0)
            abr5 = np.mean(brs)
        else:
            abr5 = 0.0

        # Lag features
        def _pd(n):
            if len(closes) < n + 1:
                return 0.0
            return 1.0 if closes[-1] > closes[-(n+1)] else -1.0

        streak = 0
        if len(closes) >= 6:
            ld = 1 if closes[-1] > closes[-2] else -1
            for i in range(2, min(len(closes), 20)):
                d = 1 if closes[-i] > closes[-(i+1)] else -1
                if d == ld:
                    streak += 1
                else:
                    break
            streak *= ld

        mr = 0.0
        if len(closes) >= 5:
            m5 = np.mean(closes[-5:])
            mr = (price - m5) / m5 * 100 if m5 > 0 else 0.0

        # Time
        dt = datetime.fromtimestamp(window_start_ts, tz=timezone.utc)
        hour = dt.hour + dt.minute / 60.0
        h = dt.hour
        dow = dt.weekday()

        hlp = cr / price * 100 if price > 0 else 0.0
        cir = (price - last.low) / cr if cr > 0 else 0.5

        return np.array([
            mom(1), mom(3), mom(5), mom(10),
            mom(20) if len(closes) >= 21 else 0.0,
            _rsi(9), _rsi(14),
            vwap_dev, bb_pos, bb_width,
            ec_9_21, ec_5_13, pve9, pve21,
            atr_14, atr_pct, recent_vol, float(vr),
            vol_z, vol_trend,
            _taker_n(1), _taker_n(3), _taker_n(5),
            uw, lw, br, cvo, abr5,
            _pd(5), _pd(10), _pd(15),
            _pd(20) if len(closes) >= 21 else 0.0,
            float(streak), mr,
            np.sin(2 * np.pi * hour / 24.0),
            np.cos(2 * np.pi * hour / 24.0),
            float(dt.minute),
            1.0 if 0 <= h < 8 else 0.0,
            1.0 if 7 <= h < 16 else 0.0,
            1.0 if 13 <= h < 22 else 0.0,
            np.sin(2 * np.pi * dow / 7.0),
            np.cos(2 * np.pi * dow / 7.0),
            hlp, cir,
        ])
