"""Technical indicators optimized for 5-minute BTC direction prediction.

All computation uses numpy for speed. No pandas or ta-lib dependencies.
Operates on lists of Candle dataclasses from data.models.
"""

import logging

import numpy as np

from data.models import Candle

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """Stateless technical indicator calculations.

    Every method is a @staticmethod that takes candle data (or raw price
    lists) and returns computed indicator values.  Designed for low-latency
    5-minute scalping on BTC.
    """

    # ------------------------------------------------------------------
    # RSI (Wilder's smoothing, period=9 for 5-min scalping)
    # ------------------------------------------------------------------
    @staticmethod
    def rsi(candles: list[Candle], period: int = 9) -> float:
        """Relative Strength Index using Wilder's smoothing.

        Returns a value in [0, 100].  Returns 50.0 when there is
        insufficient data (fewer than ``period + 1`` candles).
        """
        if len(candles) < period + 1:
            return 50.0

        closes = np.array([c.close for c in candles], dtype=np.float64)
        deltas = np.diff(closes)

        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Seed with SMA over the first `period` changes
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        # Wilder's smoothing for the remainder
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0.0:
            return 100.0

        rs = avg_gain / avg_loss
        return float(100.0 - 100.0 / (1.0 + rs))

    # ------------------------------------------------------------------
    # VWAP
    # ------------------------------------------------------------------
    @staticmethod
    def vwap(candles: list[Candle]) -> float:
        """Volume-Weighted Average Price over the provided candles.

        typical_price = (high + low + close) / 3
        VWAP = sum(typical_price * volume) / sum(volume)

        Returns 0.0 if the candle list is empty or total volume is zero.
        """
        if not candles:
            return 0.0

        typical = np.array(
            [(c.high + c.low + c.close) / 3.0 for c in candles],
            dtype=np.float64,
        )
        volumes = np.array([c.volume for c in candles], dtype=np.float64)

        total_volume = np.sum(volumes)
        if total_volume == 0.0:
            return 0.0

        return float(np.sum(typical * volumes) / total_volume)

    # ------------------------------------------------------------------
    # VWAP deviation
    # ------------------------------------------------------------------
    @staticmethod
    def vwap_deviation(candles: list[Candle]) -> float:
        """Relative deviation of the current price from VWAP.

        (current_close - vwap) / vwap
        Positive means price is above VWAP; negative means below.
        Returns 0.0 when VWAP cannot be computed.
        """
        if not candles:
            return 0.0

        vwap_val = TechnicalIndicators.vwap(candles)
        if vwap_val == 0.0:
            return 0.0

        current_price = candles[-1].close
        return float((current_price - vwap_val) / vwap_val)

    # ------------------------------------------------------------------
    # Bollinger Bands
    # ------------------------------------------------------------------
    @staticmethod
    def bollinger_bands(
        candles: list[Candle],
        period: int = 20,
        num_std: float = 2.0,
    ) -> tuple[float, float, float]:
        """Bollinger Bands: (lower, middle, upper).

        middle = SMA of closing prices over *period*.
        upper  = middle + num_std * std
        lower  = middle - num_std * std

        Returns (0.0, 0.0, 0.0) when there are fewer than *period* candles.
        """
        if len(candles) < period:
            return (0.0, 0.0, 0.0)

        closes = np.array(
            [c.close for c in candles[-period:]],
            dtype=np.float64,
        )

        middle = float(np.mean(closes))
        std = float(np.std(closes, ddof=0))  # population std
        upper = middle + num_std * std
        lower = middle - num_std * std

        return (lower, middle, upper)

    # ------------------------------------------------------------------
    # BB position (normalised 0-1)
    # ------------------------------------------------------------------
    @staticmethod
    def bb_position(
        candles: list[Candle],
        period: int = 20,
        num_std: float = 2.0,
    ) -> float:
        """Where the current close sits within the Bollinger Band range.

        (close - lower) / (upper - lower)

        Normalised to roughly 0-1.  Values < 0 mean below the lower band,
        values > 1 mean above the upper band.  Returns 0.5 when bands
        cannot be computed.
        """
        lower, middle, upper = TechnicalIndicators.bollinger_bands(
            candles, period, num_std
        )
        if lower == 0.0 and middle == 0.0 and upper == 0.0:
            return 0.5

        band_width = upper - lower
        if band_width == 0.0:
            return 0.5

        current_price = candles[-1].close
        return float((current_price - lower) / band_width)

    # ------------------------------------------------------------------
    # EMA (on raw price list)
    # ------------------------------------------------------------------
    @staticmethod
    def ema(prices: list[float], period: int) -> float:
        """Exponential Moving Average — returns the last EMA value.

        Seeded with the SMA of the first *period* values, then iteratively
        applies the EMA multiplier ``2 / (period + 1)``.

        Returns 0.0 when there are fewer than *period* prices.
        """
        if len(prices) < period:
            return 0.0

        arr = np.array(prices, dtype=np.float64)
        multiplier = 2.0 / (period + 1)

        # Seed: SMA of the first `period` values
        ema_val = float(np.mean(arr[:period]))

        # Walk forward from index `period` onward
        for i in range(period, len(arr)):
            ema_val = (arr[i] - ema_val) * multiplier + ema_val

        return ema_val

    # ------------------------------------------------------------------
    # EMA cross signal (continuous)
    # ------------------------------------------------------------------
    @staticmethod
    def ema_cross_signal(
        candles: list[Candle],
        fast_period: int = 9,
        slow_period: int = 21,
    ) -> float:
        """Continuous EMA cross signal.

        Returns ``(fast_ema - slow_ema) / slow_ema * 100`` to capture
        both direction and magnitude.  Positive means fast > slow (bullish),
        negative means fast < slow (bearish).

        Returns 0.0 when either EMA cannot be computed.
        """
        closes = [c.close for c in candles]

        fast_ema = TechnicalIndicators.ema(closes, fast_period)
        slow_ema = TechnicalIndicators.ema(closes, slow_period)

        if slow_ema == 0.0:
            return 0.0

        return float((fast_ema - slow_ema) / slow_ema * 100.0)

    # ------------------------------------------------------------------
    # ATR (Wilder's smoothing)
    # ------------------------------------------------------------------
    @staticmethod
    def atr(candles: list[Candle], period: int = 14) -> float:
        """Average True Range using Wilder's smoothing.

        TR = max(high - low, |high - prev_close|, |low - prev_close|)

        Returns 0.0 when there are fewer than ``period + 1`` candles.
        """
        if len(candles) < period + 1:
            return 0.0

        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        closes = np.array([c.close for c in candles], dtype=np.float64)

        # True Range for each candle starting at index 1
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )

        # Seed with SMA of the first `period` TR values
        atr_val = float(np.mean(tr[:period]))

        # Wilder's smoothing for the rest
        for i in range(period, len(tr)):
            atr_val = (atr_val * (period - 1) + tr[i]) / period

        return atr_val

    # ------------------------------------------------------------------
    # Momentum (simple return)
    # ------------------------------------------------------------------
    @staticmethod
    def momentum(candles: list[Candle], lookback: int = 1) -> float:
        """Price return over the last *lookback* candles.

        ``(current_close - lookback_close) / lookback_close``

        Returns 0.0 when there are fewer than ``lookback + 1`` candles
        or when the lookback close is zero.
        """
        if len(candles) < lookback + 1:
            return 0.0

        current_close = candles[-1].close
        lookback_close = candles[-(lookback + 1)].close

        if lookback_close == 0.0:
            return 0.0

        return float((current_close - lookback_close) / lookback_close)

    # ------------------------------------------------------------------
    # Volume z-score
    # ------------------------------------------------------------------
    @staticmethod
    def volume_zscore(candles: list[Candle], lookback: int = 20) -> float:
        """Z-score of the latest candle's volume vs. a rolling window.

        ``(latest_vol - mean_vol) / std_vol``

        Returns 0.0 when there are fewer than *lookback* candles or
        when the standard deviation is zero.
        """
        if len(candles) < lookback:
            return 0.0

        volumes = np.array(
            [c.volume for c in candles[-lookback:]],
            dtype=np.float64,
        )

        mean_vol = float(np.mean(volumes))
        std_vol = float(np.std(volumes, ddof=0))

        if std_vol == 0.0:
            return 0.0

        latest_vol = candles[-1].volume
        return float((latest_vol - mean_vol) / std_vol)
