"""Complete ML pipeline: download historical data, train models, save results.

Downloads months of Binance 1-minute klines, builds a large labeled dataset,
trains multiple ML models, evaluates thoroughly, and saves the best one.

Designed to run unattended for ~30-60 minutes.

Usage:
    python ml_pipeline.py
"""

import csv
import io
import json
import logging
import os
import pickle
import sqlite3
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

import numpy as np

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
LOG_FILE = Path(__file__).parent / "ml_pipeline.log"
MODEL_DIR = Path(__file__).parent / "models"
DATA_DIR = Path(__file__).parent / "historical_data"
DB_PATH = Path(__file__).parent / "btc_edge.db"

MODEL_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# Log to both file and console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candle dataclass
# ---------------------------------------------------------------------------
@dataclass
class Candle:
    timestamp: int  # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float
    trades: int


# ---------------------------------------------------------------------------
# STEP 1: Download historical Binance klines
# ---------------------------------------------------------------------------
def download_binance_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    months: int = 3,
) -> list[Candle]:
    """Download monthly kline archives from Binance public data.

    Binance provides free historical kline data at:
    https://data.binance.vision/data/spot/monthly/klines/{symbol}/{interval}/
    """
    log.info("=" * 60)
    log.info("STEP 1: Downloading historical Binance klines")
    log.info("=" * 60)

    all_candles = []

    # Also load existing candles from DB
    if DB_PATH.exists():
        log.info(f"Loading existing candles from {DB_PATH}...")
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT timestamp, open, high, low, close, volume, taker_buy_volume, trades "
            "FROM candles ORDER BY timestamp ASC"
        )
        for r in cur.fetchall():
            all_candles.append(Candle(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]))
        conn.close()
        log.info(f"  Loaded {len(all_candles)} candles from DB")

    # Determine which months to download
    now = datetime.now(timezone.utc)
    months_to_fetch = []
    for i in range(months, 0, -1):
        dt = now - timedelta(days=30 * i)
        months_to_fetch.append((dt.year, dt.month))
    # Also try current month (may be partial/unavailable)
    months_to_fetch.append((now.year, now.month))

    base_url = f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/{interval}"

    for year, month in months_to_fetch:
        filename = f"{symbol}-{interval}-{year}-{month:02d}.zip"
        url = f"{base_url}/{filename}"
        cache_path = DATA_DIR / filename

        # Check cache
        if cache_path.exists():
            log.info(f"  Using cached {filename}")
            try:
                candles = _parse_kline_zip(cache_path)
                all_candles.extend(candles)
                log.info(f"    -> {len(candles)} candles")
                continue
            except Exception as e:
                log.warning(f"    Cache corrupt, re-downloading: {e}")

        log.info(f"  Downloading {filename}...")
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urlopen(req, timeout=60)
            data = resp.read()

            # Save to cache
            with open(cache_path, "wb") as f:
                f.write(data)

            candles = _parse_kline_zip(cache_path)
            all_candles.extend(candles)
            log.info(f"    -> {len(candles)} candles")

            time.sleep(1)  # Be polite
        except URLError as e:
            log.warning(f"    Failed: {e} (may not be available yet)")
        except Exception as e:
            log.warning(f"    Error: {e}")

    # Also try daily data for most recent days (monthly archives lag)
    log.info("\n  Fetching recent daily klines...")
    daily_base = f"https://data.binance.vision/data/spot/daily/klines/{symbol}/{interval}"

    for days_ago in range(30, 0, -1):
        dt = now - timedelta(days=days_ago)
        filename = f"{symbol}-{interval}-{dt.year}-{dt.month:02d}-{dt.day:02d}.zip"
        url = f"{daily_base}/{filename}"
        cache_path = DATA_DIR / filename

        if cache_path.exists():
            try:
                candles = _parse_kline_zip(cache_path)
                all_candles.extend(candles)
                continue
            except Exception:
                pass

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urlopen(req, timeout=30)
            data = resp.read()
            with open(cache_path, "wb") as f:
                f.write(data)
            candles = _parse_kline_zip(cache_path)
            all_candles.extend(candles)
        except Exception:
            continue  # Daily files may not exist yet

        time.sleep(0.3)

    # Deduplicate by timestamp, filter out bad timestamps
    # Valid range: 2020-01-01 to 2030-01-01 in ms
    MIN_TS = 1577836800000  # 2020-01-01
    MAX_TS = 1893456000000  # 2030-01-01

    seen = set()
    unique_candles = []
    bad_count = 0
    for c in all_candles:
        if c.timestamp < MIN_TS or c.timestamp > MAX_TS:
            bad_count += 1
            continue
        if c.timestamp not in seen:
            seen.add(c.timestamp)
            unique_candles.append(c)

    if bad_count:
        log.warning(f"  Filtered out {bad_count} candles with invalid timestamps")

    unique_candles.sort(key=lambda c: c.timestamp)

    if unique_candles:
        first_sec = unique_candles[0].timestamp / 1000
        last_sec = unique_candles[-1].timestamp / 1000
        days = (last_sec - first_sec) / 86400
        log.info(f"\n  Total: {len(unique_candles)} unique candles ({days:.1f} days)")
        t0_str = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=first_sec)
        t1_str = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=last_sec)
        log.info(f"  Range: {t0_str.strftime('%Y-%m-%d %H:%M')} to {t1_str.strftime('%Y-%m-%d %H:%M')}")
    else:
        log.error("No candles loaded!")

    return unique_candles


def _parse_kline_zip(zip_path: Path) -> list[Candle]:
    """Parse a Binance kline ZIP archive into Candle objects."""
    candles = []
    with zipfile.ZipFile(str(zip_path)) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            with zf.open(name) as f:
                reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
                for row in reader:
                    if len(row) < 11:
                        continue
                    try:
                        ts = int(row[0])
                        # Binance CSVs may use microseconds (16 digits) or
                        # milliseconds (13 digits). Normalize to milliseconds.
                        if ts > 1_000_000_000_000_000:  # microseconds
                            ts = ts // 1000
                        candles.append(Candle(
                            timestamp=ts,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            taker_buy_volume=float(row[9]),
                            trades=int(row[8]),
                        ))
                    except (ValueError, IndexError):
                        continue
    return candles


# ---------------------------------------------------------------------------
# STEP 2: Feature engineering
# ---------------------------------------------------------------------------
def compute_rsi(closes: np.ndarray, period: int = 9) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))


def compute_ema(prices: np.ndarray, period: int) -> float:
    if len(prices) < period:
        return 0.0
    mult = 2.0 / (period + 1)
    val = float(np.mean(prices[:period]))
    for i in range(period, len(prices)):
        val = (prices[i] - val) * mult + val
    return val


def compute_atr(highs, lows, closes, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )
    val = float(np.mean(tr[:period]))
    for i in range(period, len(tr)):
        val = (val * (period - 1) + tr[i]) / period
    return val


FEATURE_NAMES = [
    # Momentum (multi-lookback)
    "momentum_1m", "momentum_3m", "momentum_5m", "momentum_10m", "momentum_20m",
    # RSI
    "rsi_9", "rsi_14",
    # Price vs moving averages
    "vwap_deviation", "bb_position", "bb_width",
    "ema_cross_9_21", "ema_cross_5_13",
    "price_vs_ema9", "price_vs_ema21",
    # Volatility
    "atr_14", "atr_pct", "recent_volatility", "volatility_ratio",
    # Volume
    "volume_zscore", "volume_trend",
    "taker_ratio_1m", "taker_ratio_3m", "taker_ratio_5m",
    # Candle patterns
    "upper_wick_ratio", "lower_wick_ratio", "body_ratio", "close_vs_open",
    "avg_body_ratio_5",  # average body ratio over 5 candles
    # Lag features
    "prev_5m_dir", "prev_10m_dir", "prev_15m_dir", "prev_20m_dir",
    "streak",
    "mean_reversion_5m",  # how far from 5m rolling mean
    # Time features
    "hour_sin", "hour_cos",
    "minute_in_hour",
    "is_asia", "is_europe", "is_us",
    "dow_sin", "dow_cos",
    # Microstructure
    "high_low_range_pct",  # range as % of price
    "close_position_in_range",  # where close sits in high-low range
]


def extract_features(candles: list[Candle], window_start_ts: int) -> np.ndarray | None:
    if len(candles) < 30:
        return None

    closes = np.array([c.close for c in candles])
    highs = np.array([c.high for c in candles])
    lows = np.array([c.low for c in candles])
    opens = np.array([c.open for c in candles])
    volumes = np.array([c.volume for c in candles])
    taker_buy = np.array([c.taker_buy_volume for c in candles])
    price = closes[-1]

    # Momentum
    def mom(n):
        if len(closes) < n + 1 or closes[-(n+1)] == 0:
            return 0.0
        return float((closes[-1] - closes[-(n+1)]) / closes[-(n+1)])

    momentum_1m = mom(1)
    momentum_3m = mom(3)
    momentum_5m = mom(5)
    momentum_10m = mom(10)
    momentum_20m = mom(20) if len(closes) >= 21 else 0.0

    # RSI
    rsi_9 = compute_rsi(closes, 9)
    rsi_14 = compute_rsi(closes, 14)

    # VWAP
    typical = (highs + lows + closes) / 3.0
    total_vol = np.sum(volumes)
    vwap = np.sum(typical * volumes) / total_vol if total_vol > 0 else price
    vwap_dev = (price - vwap) / vwap if vwap > 0 else 0.0

    # Bollinger Bands
    if len(closes) >= 20:
        bb_slice = closes[-20:]
        bb_mid = np.mean(bb_slice)
        bb_std = np.std(bb_slice, ddof=0)
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_bw = bb_upper - bb_lower
        bb_pos = (price - bb_lower) / bb_bw if bb_bw > 0 else 0.5
        bb_width = bb_bw / bb_mid if bb_mid > 0 else 0.0
    else:
        bb_pos = 0.5
        bb_width = 0.0

    # EMA crosses
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    ema5 = compute_ema(closes, 5)
    ema13 = compute_ema(closes, 13)
    ema_cross_9_21 = (ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0.0
    ema_cross_5_13 = (ema5 - ema13) / ema13 * 100 if ema13 > 0 else 0.0
    price_vs_ema9 = (price - ema9) / ema9 * 100 if ema9 > 0 else 0.0
    price_vs_ema21 = (price - ema21) / ema21 * 100 if ema21 > 0 else 0.0

    # ATR / volatility
    atr_14 = compute_atr(highs, lows, closes, 14)
    atr_pct = atr_14 / price if price > 0 else 0.0
    recent_volatility = float(np.std(closes[-5:])) if len(closes) >= 5 else 0.0

    # Volatility ratio: recent vs longer-term
    if len(closes) >= 20:
        vol_short = np.std(closes[-5:])
        vol_long = np.std(closes[-20:])
        volatility_ratio = vol_short / vol_long if vol_long > 0 else 1.0
    else:
        volatility_ratio = 1.0

    # Volume
    if len(volumes) >= 20:
        v_mean = np.mean(volumes[-20:])
        v_std = np.std(volumes[-20:], ddof=0)
        volume_zscore = (volumes[-1] - v_mean) / v_std if v_std > 0 else 0.0
    else:
        volume_zscore = 0.0

    if len(volumes) >= 10:
        vol_recent = np.mean(volumes[-5:])
        vol_older = np.mean(volumes[-10:-5])
        volume_trend = (vol_recent - vol_older) / vol_older if vol_older > 0 else 0.0
    else:
        volume_trend = 0.0

    # Taker ratios
    def taker_ratio_n(n):
        slc = candles[-n:] if len(candles) >= n else candles
        buy = sum(c.taker_buy_volume for c in slc)
        total = sum(c.volume for c in slc)
        if total == 0:
            return 0.0
        return (buy - (total - buy)) / total

    taker_ratio_1m = taker_ratio_n(1)
    taker_ratio_3m = taker_ratio_n(3)
    taker_ratio_5m = taker_ratio_n(5)

    # Candle patterns (last candle)
    last = candles[-1]
    cr = last.high - last.low
    if cr > 0:
        upper_wick = (last.high - max(last.open, last.close)) / cr
        lower_wick = (min(last.open, last.close) - last.low) / cr
        body_ratio = abs(last.close - last.open) / cr
    else:
        upper_wick = lower_wick = body_ratio = 0.0
    close_vs_open = 1.0 if last.close > last.open else (-1.0 if last.close < last.open else 0.0)

    # Average body ratio over last 5 candles
    if len(candles) >= 5:
        brs = []
        for c in candles[-5:]:
            r = c.high - c.low
            brs.append(abs(c.close - c.open) / r if r > 0 else 0.0)
        avg_body_ratio_5 = np.mean(brs)
    else:
        avg_body_ratio_5 = 0.0

    # Lag features
    def prev_dir(n):
        if len(closes) < n + 1:
            return 0.0
        return 1.0 if closes[-1] > closes[-(n+1)] else -1.0

    prev_5m = prev_dir(5)
    prev_10m = prev_dir(10)
    prev_15m = prev_dir(15)
    prev_20m = prev_dir(20) if len(closes) >= 21 else 0.0

    streak = 0
    if len(closes) >= 6:
        last_dir = 1 if closes[-1] > closes[-2] else -1
        for i in range(2, min(len(closes), 20)):
            d = 1 if closes[-i] > closes[-(i+1)] else -1
            if d == last_dir:
                streak += 1
            else:
                break
        streak *= last_dir

    # Mean reversion signal
    if len(closes) >= 5:
        mean_5 = np.mean(closes[-5:])
        mean_reversion = (price - mean_5) / mean_5 * 100 if mean_5 > 0 else 0.0
    else:
        mean_reversion = 0.0

    # Time
    dt = datetime.fromtimestamp(window_start_ts, tz=timezone.utc)
    hour = dt.hour + dt.minute / 60.0
    hour_sin = np.sin(2 * np.pi * hour / 24.0)
    hour_cos = np.cos(2 * np.pi * hour / 24.0)
    minute_in_hour = dt.minute
    dow = dt.weekday()
    dow_sin = np.sin(2 * np.pi * dow / 7.0)
    dow_cos = np.cos(2 * np.pi * dow / 7.0)
    h = dt.hour
    is_asia = 1.0 if 0 <= h < 8 else 0.0
    is_europe = 1.0 if 7 <= h < 16 else 0.0
    is_us = 1.0 if 13 <= h < 22 else 0.0

    # Microstructure
    high_low_pct = cr / price * 100 if price > 0 else 0.0
    close_in_range = (price - last.low) / cr if cr > 0 else 0.5

    return np.array([
        momentum_1m, momentum_3m, momentum_5m, momentum_10m, momentum_20m,
        rsi_9, rsi_14,
        vwap_dev, bb_pos, bb_width,
        ema_cross_9_21, ema_cross_5_13,
        price_vs_ema9, price_vs_ema21,
        atr_14, atr_pct, recent_volatility, volatility_ratio,
        volume_zscore, volume_trend,
        taker_ratio_1m, taker_ratio_3m, taker_ratio_5m,
        upper_wick, lower_wick, body_ratio, close_vs_open,
        avg_body_ratio_5,
        prev_5m, prev_10m, prev_15m, prev_20m,
        float(streak), mean_reversion,
        hour_sin, hour_cos, float(minute_in_hour),
        is_asia, is_europe, is_us,
        dow_sin, dow_cos,
        high_low_pct, close_in_range,
    ])


# ---------------------------------------------------------------------------
# STEP 3: Build dataset
# ---------------------------------------------------------------------------
def build_dataset(candles: list[Candle]):
    log.info("\n" + "=" * 60)
    log.info("STEP 2: Building labeled dataset")
    log.info("=" * 60)

    candle_by_ts = {c.timestamp: c for c in candles}
    timestamps = sorted(candle_by_ts.keys())

    first_ts = timestamps[0] // 1000
    last_ts = timestamps[-1] // 1000
    first_window = first_ts - (first_ts % 300) + 300
    last_window = last_ts - (last_ts % 300)

    features_list = []
    labels = []
    meta_list = []

    total_windows = (last_window - first_window) // 300
    processed = 0
    skipped_no_history = 0
    skipped_no_end = 0
    skipped_tie = 0

    window_ts = first_window
    while window_ts <= last_window - 300:
        start_ms = window_ts * 1000
        end_ms = (window_ts + 300) * 1000

        # History before this window
        hist_times = [t for t in timestamps if t < start_ms]
        if len(hist_times) < 30:
            skipped_no_history += 1
            window_ts += 300
            continue

        # Candle history (last 60 minutes of 1-min candles)
        hist_candles = [candle_by_ts[t] for t in hist_times[-60:]]

        # Start price
        start_price = candle_by_ts[hist_times[-1]].close

        # End price: candle at or near window end
        end_candidates = [t for t in timestamps if end_ms - 60000 <= t <= end_ms + 60000]
        if not end_candidates:
            skipped_no_end += 1
            window_ts += 300
            continue
        best_end = min(end_candidates, key=lambda t: abs(t - end_ms))
        end_price = candle_by_ts[best_end].close

        if start_price == end_price:
            skipped_tie += 1
            window_ts += 300
            continue

        label = 1 if end_price > start_price else 0

        feat = extract_features(hist_candles, window_ts)
        if feat is None:
            window_ts += 300
            continue

        features_list.append(feat)
        labels.append(label)
        meta_list.append({
            "window_start": window_ts,
            "start_price": start_price,
            "end_price": end_price,
        })

        processed += 1
        if processed % 5000 == 0:
            log.info(f"  Processed {processed} windows...")

        window_ts += 300

    X = np.array(features_list)
    y = np.array(labels)

    log.info(f"\n  Total possible windows: {total_windows}")
    log.info(f"  Skipped (no history): {skipped_no_history}")
    log.info(f"  Skipped (no end candle): {skipped_no_end}")
    log.info(f"  Skipped (tie): {skipped_tie}")
    log.info(f"  Final dataset: {X.shape[0]} samples, {X.shape[1]} features")
    log.info(f"  Labels: UP={np.sum(y)} ({np.mean(y)*100:.1f}%), DOWN={np.sum(y==0)} ({np.mean(y==0)*100:.1f}%)")

    return X, y, meta_list


# ---------------------------------------------------------------------------
# STEP 4: Train and evaluate
# ---------------------------------------------------------------------------
def train_and_evaluate(X, y, meta):
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        RandomForestClassifier,
        HistGradientBoostingClassifier,
    )
    from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
    from sklearn.model_selection import TimeSeriesSplit
    from scipy import stats

    log.info("\n" + "=" * 60)
    log.info("STEP 3: Training ML models")
    log.info("=" * 60)

    n = len(y)

    # ================================================================
    # Time-series cross-validation (5 folds)
    # ================================================================
    log.info("\n--- Time-Series Cross-Validation (5 folds) ---")
    tscv = TimeSeriesSplit(n_splits=5)

    model_configs = {
        "LogReg_C01": lambda: LogisticRegression(C=0.1, max_iter=2000, random_state=42),
        "LogReg_C001": lambda: LogisticRegression(C=0.01, max_iter=2000, random_state=42),
        "RF_d5": lambda: RandomForestClassifier(n_estimators=300, max_depth=5, min_samples_leaf=30, random_state=42, n_jobs=-1),
        "RF_d3": lambda: RandomForestClassifier(n_estimators=300, max_depth=3, min_samples_leaf=50, random_state=42, n_jobs=-1),
        "GBT_v1": lambda: GradientBoostingClassifier(
            n_estimators=200, max_depth=3, learning_rate=0.03,
            min_samples_leaf=30, subsample=0.7, max_features=0.7, random_state=42,
        ),
        "GBT_v2": lambda: GradientBoostingClassifier(
            n_estimators=300, max_depth=2, learning_rate=0.05,
            min_samples_leaf=50, subsample=0.8, max_features=0.5, random_state=42,
        ),
        "GBT_v3": lambda: GradientBoostingClassifier(
            n_estimators=500, max_depth=2, learning_rate=0.01,
            min_samples_leaf=60, subsample=0.7, max_features=0.6, random_state=42,
        ),
        "HistGBT": lambda: HistGradientBoostingClassifier(
            max_iter=300, max_depth=3, learning_rate=0.05,
            min_samples_leaf=50, max_features=0.7,
            l2_regularization=1.0, random_state=42,
        ),
    }

    cv_results = {name: [] for name in model_configs}

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        for name, model_fn in model_configs.items():
            model = model_fn()
            model.fit(X_tr_s, y_tr)
            acc = accuracy_score(y_te, model.predict(X_te_s))
            cv_results[name].append(acc)

        log.info(f"  Fold {fold+1}/5 done (train={len(train_idx)}, test={len(test_idx)})")

    log.info(f"\n  {'Model':<15s} {'Mean':>7s} {'Std':>6s} {'Min':>6s} {'Max':>6s}  Folds")
    log.info(f"  {'-'*15} {'-'*7} {'-'*6} {'-'*6} {'-'*6}  {'-'*30}")
    for name, accs in sorted(cv_results.items(), key=lambda x: np.mean(x[1]), reverse=True):
        log.info(f"  {name:<15s} {np.mean(accs)*100:>6.1f}% {np.std(accs)*100:>5.1f}% "
                 f"{np.min(accs)*100:>5.1f}% {np.max(accs)*100:>5.1f}%  "
                 f"{[f'{a*100:.1f}' for a in accs]}")

    # ================================================================
    # Final train/test (80/20)
    # ================================================================
    log.info(f"\n{'='*60}")
    log.info("STEP 4: Final evaluation (80/20 time split)")
    log.info(f"{'='*60}")

    split = int(n * 0.80)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    meta_test = meta[split:]

    log.info(f"  Train: {len(y_train)} ({np.mean(y_train)*100:.1f}% UP)")
    log.info(f"  Test:  {len(y_test)} ({np.mean(y_test)*100:.1f}% UP)")

    baseline = max(np.mean(y_test), 1 - np.mean(y_test))

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    # Train all models on full train set
    trained_models = {}
    for name, model_fn in model_configs.items():
        log.info(f"  Training {name}...")
        model = model_fn()
        model.fit(X_train_s, y_train)
        trained_models[name] = model

    # Evaluate
    log.info(f"\n  {'Model':<15s} {'Train':>7s} {'Test':>7s} {'Brier':>7s} {'LogLoss':>8s} {'p-val':>8s}")
    log.info(f"  {'-'*15} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*8}")
    log.info(f"  {'Baseline':<15s} {'':>7s} {baseline*100:>6.1f}% {'0.2500':>7s} {'0.6931':>8s} {'':>8s}")

    best_name = None
    best_cv_mean = 0.0

    for name in sorted(cv_results.keys(), key=lambda k: np.mean(cv_results[k]), reverse=True):
        model = trained_models[name]
        train_acc = accuracy_score(y_train, model.predict(X_train_s))
        test_acc = accuracy_score(y_test, model.predict(X_test_s))
        probs = model.predict_proba(X_test_s)[:, 1]
        brier = brier_score_loss(y_test, probs)
        ll = log_loss(y_test, probs)

        n_correct = int(test_acc * len(y_test))
        p_val = stats.binomtest(n_correct, len(y_test), 0.5, alternative='greater').pvalue
        sig = "***" if p_val < 0.01 else "**" if p_val < 0.05 else "*" if p_val < 0.1 else ""

        log.info(f"  {name:<15s} {train_acc*100:>6.1f}% {test_acc*100:>6.1f}% "
                 f"{brier:>7.4f} {ll:>8.4f} {p_val:>7.4f} {sig}")

        # Select best by CV mean (more robust than single test split)
        cv_mean = np.mean(cv_results[name])
        if cv_mean > best_cv_mean:
            best_cv_mean = cv_mean
            best_name = name

    best_model = trained_models[best_name]
    best_test_acc = accuracy_score(y_test, best_model.predict(X_test_s))
    log.info(f"\n  >>> Best model (by CV): {best_name} (CV={best_cv_mean*100:.1f}%, Test={best_test_acc*100:.1f}%)")

    # ================================================================
    # Feature importance
    # ================================================================
    log.info(f"\n{'='*60}")
    log.info(f"FEATURE IMPORTANCE ({best_name})")
    log.info(f"{'='*60}")

    if hasattr(best_model, 'feature_importances_'):
        importances = best_model.feature_importances_
    elif hasattr(best_model, 'coef_'):
        importances = np.abs(best_model.coef_[0])
    else:
        importances = np.zeros(len(FEATURE_NAMES))

    ranked = sorted(zip(FEATURE_NAMES, importances), key=lambda x: x[1], reverse=True)
    max_imp = max(importances) if max(importances) > 0 else 1
    for name_f, imp in ranked[:20]:
        bar = "#" * int(imp / max_imp * 30)
        log.info(f"  {name_f:28s} {imp:.4f} {bar}")

    # Bottom features (candidates for removal)
    log.info(f"\n  Bottom 10 (lowest importance):")
    for name_f, imp in ranked[-10:]:
        log.info(f"  {name_f:28s} {imp:.4f}")

    # ================================================================
    # Calibration
    # ================================================================
    log.info(f"\n{'='*60}")
    log.info(f"CALIBRATION ({best_name})")
    log.info(f"{'='*60}")

    probs = best_model.predict_proba(X_test_s)[:, 1]
    buckets = [(0.0, 0.35), (0.35, 0.40), (0.40, 0.45), (0.45, 0.50),
               (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 1.0)]
    for lo, hi in buckets:
        mask = (probs >= lo) & (probs < hi)
        cnt = np.sum(mask)
        if cnt > 5:
            actual = np.mean(y_test[mask])
            predicted = np.mean(probs[mask])
            log.info(f"  P(up) [{lo:.2f}-{hi:.2f}): n={cnt:4d}, pred={predicted:.3f}, "
                     f"actual={actual:.3f}, gap={predicted-actual:+.3f}")

    # ================================================================
    # Simulated P&L
    # ================================================================
    log.info(f"\n{'='*60}")
    log.info("STEP 5: Simulated P&L")
    log.info(f"{'='*60}")

    ENTRY_PRICE = 0.50
    FEE_FACTOR = 0.25 * (0.5 * 0.5) ** 2  # ~0.0156

    # --- A) Flat bet, trade every window ---
    log.info("\n  A) Flat $5 bet, every window:")
    for name, model in trained_models.items():
        preds = model.predict(X_test_s)
        wins = sum(1 for i in range(len(y_test))
                   if (preds[i] == 1 and y_test[i] == 1) or (preds[i] == 0 and y_test[i] == 0))
        n_t = len(y_test)
        shares = (5.0 / ENTRY_PRICE) * (1 - FEE_FACTOR)
        pnl = wins * (shares - 5.0) + (n_t - wins) * (-5.0)
        daily = pnl / (n_t / 288)
        wr = wins / n_t * 100
        log.info(f"    {name:<15s}: {wr:.1f}% WR, PnL=${pnl:+.2f} (${daily:+.1f}/day)")

    # --- B) Confidence-scaled ---
    log.info("\n  B) Confidence-scaled bets ($2-$10):")
    for name, model in trained_models.items():
        probs = model.predict_proba(X_test_s)[:, 1]
        total_pnl = 0.0
        wins = 0
        n_t = len(y_test)
        total_wagered = 0.0

        for i in range(n_t):
            conf = abs(probs[i] - 0.5)
            bet = min(10.0, 2.0 + conf * 16.0)
            total_wagered += bet

            side_up = probs[i] > 0.5
            went_up = y_test[i] == 1
            won = (side_up and went_up) or (not side_up and not went_up)

            shares = (bet / ENTRY_PRICE) * (1 - FEE_FACTOR)
            pnl = (shares - bet) if won else -bet
            total_pnl += pnl
            if won:
                wins += 1

        wr = wins / n_t * 100
        daily = total_pnl / (n_t / 288)
        roi = total_pnl / total_wagered * 100 if total_wagered > 0 else 0
        log.info(f"    {name:<15s}: {wr:.1f}% WR, PnL=${total_pnl:+.2f} (${daily:+.1f}/day), "
                 f"ROI={roi:+.2f}%")

    # --- C) Only trade when confidence > threshold ---
    log.info("\n  C) Confidence threshold (only trade when |P-0.5| > threshold):")
    for threshold in [0.02, 0.05, 0.08, 0.10, 0.15]:
        probs = best_model.predict_proba(X_test_s)[:, 1]
        total_pnl = 0.0
        wins = 0
        n_traded = 0

        for i in range(len(y_test)):
            conf = abs(probs[i] - 0.5)
            if conf < threshold:
                continue

            n_traded += 1
            bet = 5.0

            side_up = probs[i] > 0.5
            went_up = y_test[i] == 1
            won = (side_up and went_up) or (not side_up and not went_up)

            shares = (bet / ENTRY_PRICE) * (1 - FEE_FACTOR)
            pnl = (shares - bet) if won else -bet
            total_pnl += pnl
            if won:
                wins += 1

        if n_traded > 0:
            wr = wins / n_traded * 100
            daily = total_pnl / (len(y_test) / 288)
            log.info(f"    threshold={threshold:.2f}: {n_traded:4d} trades ({n_traded/len(y_test)*100:.0f}%), "
                     f"{wr:.1f}% WR, PnL=${total_pnl:+.2f} (${daily:+.1f}/day)")

    # ================================================================
    # Hourly breakdown
    # ================================================================
    log.info(f"\n{'='*60}")
    log.info(f"HOURLY ACCURACY ({best_name})")
    log.info(f"{'='*60}")

    preds = best_model.predict(X_test_s)
    hours_data = {}
    for i, m in enumerate(meta_test):
        dt = datetime.fromtimestamp(m["window_start"], tz=timezone.utc)
        h = dt.hour
        if h not in hours_data:
            hours_data[h] = {"correct": 0, "total": 0}
        hours_data[h]["total"] += 1
        correct = (preds[i] == 1 and y_test[i] == 1) or (preds[i] == 0 and y_test[i] == 0)
        if correct:
            hours_data[h]["correct"] += 1

    for h in sorted(hours_data.keys()):
        d = hours_data[h]
        acc = d["correct"] / d["total"] * 100
        bar = "#" * int(acc - 45)  # visual bar starting from 45%
        marker = " <<<" if acc >= 55 else " !!!" if acc < 48 else ""
        log.info(f"  {h:02d}:00 UTC: {acc:5.1f}% ({d['correct']:3d}/{d['total']:3d}) {bar}{marker}")

    # ================================================================
    # Save
    # ================================================================
    log.info(f"\n{'='*60}")
    log.info("STEP 6: Saving models")
    log.info(f"{'='*60}")

    with open(MODEL_DIR / "best_model.pkl", "wb") as f:
        pickle.dump(best_model, f)
    with open(MODEL_DIR / "scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)

    model_meta = {
        "model_type": best_name,
        "feature_names": FEATURE_NAMES,
        "n_features": len(FEATURE_NAMES),
        "train_samples": len(y_train),
        "test_samples": len(y_test),
        "test_accuracy": best_test_acc,
        "cv_accuracy_mean": best_cv_mean,
        "cv_accuracy_std": float(np.std(cv_results[best_name])),
        "baseline_accuracy": baseline,
        "paradigm": "always_trade",
        "total_candles": len(meta) + split,
    }
    with open(MODEL_DIR / "model_meta.json", "w") as f:
        json.dump(model_meta, f, indent=2)

    for mname, model in trained_models.items():
        with open(MODEL_DIR / f"{mname}.pkl", "wb") as f:
            pickle.dump(model, f)

    log.info(f"  Best: {best_name} (CV={best_cv_mean*100:.1f}%)")
    log.info(f"  Saved to {MODEL_DIR}/")

    # Save dataset for future use
    np.savez_compressed(
        str(MODEL_DIR / "dataset.npz"),
        X=X, y=y,
        feature_names=FEATURE_NAMES,
    )
    log.info(f"  Saved dataset to {MODEL_DIR}/dataset.npz")

    log.info(f"\n{'='*60}")
    log.info("DONE")
    log.info(f"{'='*60}")
    log.info(f"\nResults written to: {LOG_FILE}")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("BTC 5-Min ML Pipeline (Always Trade)")
    log.info(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    candles = download_binance_klines(months=3)

    if len(candles) < 1000:
        log.error(f"Only {len(candles)} candles — not enough data")
        sys.exit(1)

    X, y, meta = build_dataset(candles)

    if len(y) < 500:
        log.error(f"Only {len(y)} samples — not enough")
        sys.exit(1)

    train_and_evaluate(X, y, meta)
