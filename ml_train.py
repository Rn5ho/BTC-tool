"""ML model training for BTC 5-minute direction prediction.

New paradigm: every 5-minute window is a trade. The model predicts UP or DOWN.
No abstaining, no edge thresholds. Optimize for pure accuracy.

Usage:
    python ml_train.py
"""

import sqlite3
import pickle
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "btc_edge.db"
MODEL_DIR = Path(__file__).parent / "models"
MODEL_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Candle
# ---------------------------------------------------------------------------
@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float
    trades: int


# ---------------------------------------------------------------------------
# Indicator functions (same as signals/indicators.py)
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


def compute_vwap_deviation(highs, lows, closes, volumes) -> float:
    typical = (highs + lows + closes) / 3.0
    total_vol = np.sum(volumes)
    if total_vol == 0.0:
        return 0.0
    vwap = np.sum(typical * volumes) / total_vol
    if vwap == 0.0:
        return 0.0
    return float((closes[-1] - vwap) / vwap)


def compute_bb_position(closes: np.ndarray, period: int = 20) -> float:
    if len(closes) < period:
        return 0.5
    window = closes[-period:]
    middle = np.mean(window)
    std = np.std(window, ddof=0)
    if std == 0.0:
        return 0.5
    upper = middle + 2.0 * std
    lower = middle - 2.0 * std
    bw = upper - lower
    if bw == 0.0:
        return 0.5
    return float((closes[-1] - lower) / bw)


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


# ---------------------------------------------------------------------------
# Feature engineering — let ML learn everything
# ---------------------------------------------------------------------------
FEATURE_NAMES = [
    # Price action
    "momentum_1m", "momentum_3m", "momentum_5m", "momentum_10m",
    "rsi_9", "rsi_14",
    "vwap_deviation",
    "bb_position",
    "ema_cross_9_21",  # (ema9 - ema21) / ema21
    "ema_cross_5_13",  # faster cross
    "atr_14",
    "atr_pct",  # ATR as % of price (normalized volatility)
    # Volume
    "volume_zscore",
    "taker_ratio_1m",  # buy/sell from last candle
    "taker_ratio_5m",  # avg over last 5 candles
    "volume_trend",  # is volume increasing or decreasing
    # Price patterns
    "upper_wick_ratio",  # selling pressure
    "lower_wick_ratio",  # buying pressure
    "body_ratio",  # candle body size relative to range
    "close_vs_open",  # last candle direction
    # Multi-timeframe
    "price_vs_ema9",  # price position vs fast EMA
    "price_vs_ema21",  # price position vs slow EMA
    "bb_width",  # volatility squeeze indicator
    "recent_volatility",  # std of last 5 closes
    # Lag features (previous window outcomes)
    "prev_5m_direction",  # did last 5m go up?
    "prev_10m_direction",  # did last 10m go up?
    "prev_15m_direction",  # last 15m
    "consecutive_direction",  # streak of same direction
    # Time features
    "hour_sin", "hour_cos",  # cyclical encoding of UTC hour
    "minute_in_hour",  # minute within the hour (0-55)
    "is_asia_session",  # Tokyo/Sydney hours
    "is_europe_session",  # London hours
    "is_us_session",  # NY hours
    "day_of_week_sin", "day_of_week_cos",  # cyclical day encoding
]


def extract_features(candles: list[Candle], window_start_ts: int) -> np.ndarray | None:
    """Extract feature vector from candle history at a given point in time.

    candles: list of 1-min candles BEFORE the window start (history only).
    window_start_ts: unix timestamp of the 5-min window start.
    """
    if len(candles) < 30:
        return None

    closes = np.array([c.close for c in candles])
    highs = np.array([c.high for c in candles])
    lows = np.array([c.low for c in candles])
    opens = np.array([c.open for c in candles])
    volumes = np.array([c.volume for c in candles])
    taker_buy = np.array([c.taker_buy_volume for c in candles])

    price = closes[-1]

    # --- Momentum at different lookbacks ---
    def mom(lookback):
        if len(closes) < lookback + 1 or closes[-(lookback + 1)] == 0:
            return 0.0
        return float((closes[-1] - closes[-(lookback + 1)]) / closes[-(lookback + 1)])

    momentum_1m = mom(1)
    momentum_3m = mom(3)
    momentum_5m = mom(5)
    momentum_10m = mom(10)

    # --- RSI ---
    rsi_9 = compute_rsi(closes, 9)
    rsi_14 = compute_rsi(closes, 14)

    # --- VWAP ---
    vwap_dev = compute_vwap_deviation(highs, lows, closes, volumes)

    # --- Bollinger ---
    bb_pos = compute_bb_position(closes, 20)
    if len(closes) >= 20:
        bb_std = np.std(closes[-20:], ddof=0)
        bb_mid = np.mean(closes[-20:])
        bb_width = (4.0 * bb_std / bb_mid) if bb_mid > 0 else 0.0
    else:
        bb_width = 0.0

    # --- EMA crosses ---
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    ema5 = compute_ema(closes, 5)
    ema13 = compute_ema(closes, 13)

    ema_cross_9_21 = (ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0.0
    ema_cross_5_13 = (ema5 - ema13) / ema13 * 100 if ema13 > 0 else 0.0

    # --- ATR ---
    atr_14 = compute_atr(highs, lows, closes, 14)
    atr_pct = atr_14 / price if price > 0 else 0.0

    # --- Volume ---
    if len(volumes) >= 20:
        v_mean = np.mean(volumes[-20:])
        v_std = np.std(volumes[-20:], ddof=0)
        volume_zscore = (volumes[-1] - v_mean) / v_std if v_std > 0 else 0.0
    else:
        volume_zscore = 0.0

    # Taker ratio from candle data
    def taker_ratio_n(n):
        if len(candles) < n:
            return 0.0
        buy = sum(c.taker_buy_volume for c in candles[-n:])
        total = sum(c.volume for c in candles[-n:])
        if total == 0:
            return 0.0
        sell = total - buy
        return (buy - sell) / total

    taker_ratio_1m = taker_ratio_n(1)
    taker_ratio_5m = taker_ratio_n(5)

    # Volume trend: is volume increasing?
    if len(volumes) >= 10:
        vol_recent = np.mean(volumes[-5:])
        vol_older = np.mean(volumes[-10:-5])
        volume_trend = (vol_recent - vol_older) / vol_older if vol_older > 0 else 0.0
    else:
        volume_trend = 0.0

    # --- Candle patterns (last candle) ---
    last = candles[-1]
    candle_range = last.high - last.low
    if candle_range > 0:
        upper_wick = (last.high - max(last.open, last.close)) / candle_range
        lower_wick = (min(last.open, last.close) - last.low) / candle_range
        body_ratio = abs(last.close - last.open) / candle_range
    else:
        upper_wick = 0.0
        lower_wick = 0.0
        body_ratio = 0.0
    close_vs_open = 1.0 if last.close > last.open else (-1.0 if last.close < last.open else 0.0)

    # --- Multi-timeframe ---
    price_vs_ema9 = (price - ema9) / ema9 * 100 if ema9 > 0 else 0.0
    price_vs_ema21 = (price - ema21) / ema21 * 100 if ema21 > 0 else 0.0

    recent_volatility = float(np.std(closes[-5:])) if len(closes) >= 5 else 0.0

    # --- Lag features (direction of previous windows) ---
    def prev_direction(lookback_min):
        if len(closes) < lookback_min + 1:
            return 0.0
        return 1.0 if closes[-1] > closes[-(lookback_min + 1)] else -1.0

    prev_5m = prev_direction(5)
    prev_10m = prev_direction(10)
    prev_15m = prev_direction(15)

    # Consecutive same-direction streak
    streak = 0
    if len(closes) >= 6:
        last_dir = 1 if closes[-1] > closes[-2] else -1
        for i in range(2, min(len(closes), 20)):
            d = 1 if closes[-i] > closes[-(i + 1)] else -1
            if d == last_dir:
                streak += 1
            else:
                break
        streak *= last_dir  # positive = up streak, negative = down streak

    # --- Time features ---
    dt = datetime.fromtimestamp(window_start_ts, tz=timezone.utc)
    hour = dt.hour + dt.minute / 60.0
    hour_sin = np.sin(2 * np.pi * hour / 24.0)
    hour_cos = np.cos(2 * np.pi * hour / 24.0)
    minute_in_hour = dt.minute
    dow = dt.weekday()
    dow_sin = np.sin(2 * np.pi * dow / 7.0)
    dow_cos = np.cos(2 * np.pi * dow / 7.0)

    # Session flags
    h = dt.hour
    is_asia = 1.0 if (0 <= h < 8) else 0.0
    is_europe = 1.0 if (7 <= h < 16) else 0.0
    is_us = 1.0 if (13 <= h < 22) else 0.0

    return np.array([
        momentum_1m, momentum_3m, momentum_5m, momentum_10m,
        rsi_9, rsi_14,
        vwap_dev,
        bb_pos,
        ema_cross_9_21, ema_cross_5_13,
        atr_14, atr_pct,
        volume_zscore,
        taker_ratio_1m, taker_ratio_5m,
        volume_trend,
        upper_wick, lower_wick, body_ratio, close_vs_open,
        price_vs_ema9, price_vs_ema21,
        bb_width, recent_volatility,
        prev_5m, prev_10m, prev_15m, float(streak),
        hour_sin, hour_cos, float(minute_in_hour),
        is_asia, is_europe, is_us,
        dow_sin, dow_cos,
    ])


# ---------------------------------------------------------------------------
# Build dataset
# ---------------------------------------------------------------------------
def load_candles() -> list[Candle]:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute(
        "SELECT timestamp, open, high, low, close, volume, taker_buy_volume, trades "
        "FROM candles ORDER BY timestamp ASC"
    )
    candles = [
        Candle(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
        for r in cur.fetchall()
    ]
    conn.close()
    log.info(f"Loaded {len(candles)} candles")
    return candles


def build_dataset(candles: list[Candle]):
    candle_by_ts = {c.timestamp: c for c in candles}
    timestamps = sorted(candle_by_ts.keys())

    first_ts = timestamps[0] // 1000
    last_ts = timestamps[-1] // 1000
    first_window = first_ts - (first_ts % 300) + 300  # start from second window
    last_window = last_ts - (last_ts % 300)

    log.info(f"Candle range: {(last_ts - first_ts) / 86400:.1f} days")

    features_list = []
    labels = []
    meta = []

    window_ts = first_window
    while window_ts <= last_window - 300:
        start_ms = window_ts * 1000
        end_ms = (window_ts + 300) * 1000

        # History: all candles strictly before this window
        hist_times = [t for t in timestamps if t < start_ms]
        if len(hist_times) < 30:
            window_ts += 300
            continue

        # Build candle list (last 60 for indicator lookback)
        hist_candles = [candle_by_ts[t] for t in hist_times[-60:]]

        # Outcome: BTC price at start vs end of window
        start_price = candle_by_ts[hist_times[-1]].close

        # Find end price: closest candle to window end
        end_candidates = [t for t in timestamps if end_ms - 60000 <= t <= end_ms + 60000]
        if not end_candidates:
            window_ts += 300
            continue
        best_end = min(end_candidates, key=lambda t: abs(t - end_ms))
        end_price = candle_by_ts[best_end].close

        # Skip exact ties (rare but uninformative)
        if start_price == end_price:
            window_ts += 300
            continue

        label = 1 if end_price > start_price else 0

        # Extract features
        feat = extract_features(hist_candles, window_ts)
        if feat is None:
            window_ts += 300
            continue

        features_list.append(feat)
        labels.append(label)
        meta.append({
            "window_start": window_ts,
            "start_price": start_price,
            "end_price": end_price,
            "change_pct": (end_price - start_price) / start_price * 100,
        })

        window_ts += 300

    X = np.array(features_list)
    y = np.array(labels)
    log.info(f"Dataset: {X.shape[0]} samples, {X.shape[1]} features")
    log.info(f"Labels: UP={np.sum(y)} ({np.mean(y)*100:.1f}%), DOWN={np.sum(y==0)} ({np.mean(y==0)*100:.1f}%)")
    return X, y, meta


# ---------------------------------------------------------------------------
# Train
# ---------------------------------------------------------------------------
def train(X, y, meta):
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.metrics import accuracy_score, brier_score_loss
    from sklearn.model_selection import TimeSeriesSplit
    from scipy import stats

    n = len(y)

    # ---- Time-series cross-validation first (more robust than single split) ----
    log.info(f"\n{'='*60}")
    log.info("TIME-SERIES CROSS-VALIDATION (5 folds)")
    log.info(f"{'='*60}")

    tscv = TimeSeriesSplit(n_splits=5)
    cv_results = {
        "logreg": [], "rf": [], "gbt": [], "gbt_tuned": [],
    }

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_tr, y_te = y[train_idx], y[test_idx]

        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        # Logistic regression
        lr = LogisticRegression(C=0.1, max_iter=1000, random_state=42)
        lr.fit(X_tr_s, y_tr)
        cv_results["logreg"].append(accuracy_score(y_te, lr.predict(X_te_s)))

        # Random Forest
        rf = RandomForestClassifier(n_estimators=200, max_depth=5, min_samples_leaf=30, random_state=42, n_jobs=-1)
        rf.fit(X_tr_s, y_tr)
        cv_results["rf"].append(accuracy_score(y_te, rf.predict(X_te_s)))

        # GBT (regularized to prevent overfit)
        gbt = GradientBoostingClassifier(
            n_estimators=150, max_depth=3, learning_rate=0.03,
            min_samples_leaf=30, subsample=0.7, max_features=0.7,
            random_state=42,
        )
        gbt.fit(X_tr_s, y_tr)
        cv_results["gbt"].append(accuracy_score(y_te, gbt.predict(X_te_s)))

        # GBT tuned (slightly different params)
        gbt2 = GradientBoostingClassifier(
            n_estimators=200, max_depth=2, learning_rate=0.05,
            min_samples_leaf=50, subsample=0.8, max_features=0.5,
            random_state=42,
        )
        gbt2.fit(X_tr_s, y_tr)
        cv_results["gbt_tuned"].append(accuracy_score(y_te, gbt2.predict(X_te_s)))

    for name, accs in cv_results.items():
        mean_acc = np.mean(accs)
        std_acc = np.std(accs)
        log.info(f"  {name:15s}: {mean_acc*100:.1f}% +/- {std_acc*100:.1f}%  folds={[f'{a*100:.1f}' for a in accs]}")

    # ---- Final train/test split for detailed evaluation ----
    log.info(f"\n{'='*60}")
    log.info("FINAL EVALUATION (80/20 time split)")
    log.info(f"{'='*60}")

    split = int(n * 0.80)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    meta_test = meta[split:]

    log.info(f"  Train: {len(y_train)} ({np.mean(y_train)*100:.1f}% UP)")
    log.info(f"  Test:  {len(y_test)} ({np.mean(y_test)*100:.1f}% UP)")

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    baseline = max(np.mean(y_test), 1 - np.mean(y_test))
    log.info(f"  Baseline (majority): {baseline*100:.1f}%")

    models = {}

    # -- Logistic Regression --
    lr = LogisticRegression(C=0.1, max_iter=1000, random_state=42)
    lr.fit(X_train_s, y_train)
    models["LogReg"] = lr

    # -- Random Forest --
    rf = RandomForestClassifier(n_estimators=300, max_depth=5, min_samples_leaf=30, random_state=42, n_jobs=-1)
    rf.fit(X_train_s, y_train)
    models["RF"] = rf

    # -- GBT regularized --
    gbt = GradientBoostingClassifier(
        n_estimators=200, max_depth=3, learning_rate=0.03,
        min_samples_leaf=30, subsample=0.7, max_features=0.7,
        random_state=42,
    )
    gbt.fit(X_train_s, y_train)
    models["GBT"] = gbt

    # -- GBT v2 --
    gbt2 = GradientBoostingClassifier(
        n_estimators=250, max_depth=2, learning_rate=0.05,
        min_samples_leaf=50, subsample=0.8, max_features=0.5,
        random_state=42,
    )
    gbt2.fit(X_train_s, y_train)
    models["GBT_v2"] = gbt2

    log.info(f"\n  {'Model':<12s} {'Train':>7s} {'Test':>7s} {'Brier':>7s} {'p-value':>9s}")
    log.info(f"  {'-'*12} {'-'*7} {'-'*7} {'-'*7} {'-'*9}")

    best_model_name = None
    best_test_acc = 0.0

    for name, model in models.items():
        train_acc = accuracy_score(y_train, model.predict(X_train_s))
        test_acc = accuracy_score(y_test, model.predict(X_test_s))
        probs = model.predict_proba(X_test_s)[:, 1]
        brier = brier_score_loss(y_test, probs)

        n_correct = int(test_acc * len(y_test))
        p_val = stats.binomtest(n_correct, len(y_test), 0.5, alternative='greater').pvalue
        sig = "***" if p_val < 0.01 else "**" if p_val < 0.05 else "*" if p_val < 0.1 else ""

        log.info(f"  {name:<12s} {train_acc*100:>6.1f}% {test_acc*100:>6.1f}% {brier:>7.4f} {p_val:>8.4f} {sig}")

        if test_acc > best_test_acc:
            best_test_acc = test_acc
            best_model_name = name

    best_model = models[best_model_name]
    log.info(f"\n  Best model: {best_model_name} ({best_test_acc*100:.1f}%)")

    # ---- Feature importance (best model) ----
    log.info(f"\n{'='*60}")
    log.info(f"FEATURE IMPORTANCE ({best_model_name})")
    log.info(f"{'='*60}")

    if hasattr(best_model, 'feature_importances_'):
        importances = best_model.feature_importances_
    elif hasattr(best_model, 'coef_'):
        importances = np.abs(best_model.coef_[0])
    else:
        importances = np.zeros(len(FEATURE_NAMES))

    ranked = sorted(zip(FEATURE_NAMES, importances), key=lambda x: x[1], reverse=True)
    for name, imp in ranked[:15]:
        bar = "#" * int(imp / max(importances) * 30)
        log.info(f"  {name:25s} {imp:.4f} {bar}")

    # ---- Calibration ----
    log.info(f"\n{'='*60}")
    log.info(f"CALIBRATION ({best_model_name})")
    log.info(f"{'='*60}")

    probs = best_model.predict_proba(X_test_s)[:, 1]
    buckets = [(0.0, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 1.0)]
    for lo, hi in buckets:
        mask = (probs >= lo) & (probs < hi)
        cnt = np.sum(mask)
        if cnt > 0:
            actual = np.mean(y_test[mask])
            predicted = np.mean(probs[mask])
            log.info(f"  P(up) [{lo:.2f}-{hi:.2f}): n={cnt:4d}, pred={predicted:.3f}, actual={actual:.3f}, gap={predicted-actual:+.3f}")

    # ---- Simulated P&L: trade EVERY window ----
    log.info(f"\n{'='*60}")
    log.info("SIMULATED P&L (trade every window)")
    log.info(f"{'='*60}")

    BET_SIZE = 5.0
    ENTRY_PRICE = 0.50  # assume 50/50 market
    FEE_FACTOR = 0.25 * (0.5 * 0.5) ** 2  # ~0.0156

    for name, model in models.items():
        preds = model.predict(X_test_s)
        probs = model.predict_proba(X_test_s)[:, 1]

        bankroll = 100.0
        total_pnl = 0.0
        wins = 0
        n_trades = len(y_test)

        for i in range(n_trades):
            side_up = preds[i] == 1
            went_up = y_test[i] == 1
            won = (side_up and went_up) or (not side_up and not went_up)

            shares = (BET_SIZE / ENTRY_PRICE) * (1 - FEE_FACTOR)
            pnl = (shares - BET_SIZE) if won else -BET_SIZE
            total_pnl += pnl
            bankroll += pnl
            if won:
                wins += 1

        wr = wins / n_trades * 100
        daily_pnl = total_pnl / (n_trades / 288)  # 288 windows/day
        log.info(f"  {name:<12s}: {n_trades} trades, {wr:.1f}% WR, "
                 f"PnL=${total_pnl:+.2f} (${daily_pnl:+.1f}/day), bankroll=${bankroll:.2f}")

    # ---- Confidence-scaled betting ----
    log.info(f"\n{'='*60}")
    log.info("SIMULATED P&L (confidence-scaled bets)")
    log.info(f"{'='*60}")

    for name, model in models.items():
        probs = model.predict_proba(X_test_s)[:, 1]

        bankroll = 100.0
        total_pnl = 0.0
        wins = 0
        n_trades = len(y_test)

        for i in range(n_trades):
            p_up = probs[i]
            # Confidence = distance from 0.5
            confidence = abs(p_up - 0.5)
            # Scale bet: $2 minimum, up to $10 at max confidence
            bet = 2.0 + confidence * 16.0  # 2 + 16*conf
            bet = min(bet, 10.0)

            side_up = p_up > 0.5
            went_up = y_test[i] == 1
            won = (side_up and went_up) or (not side_up and not went_up)

            shares = (bet / ENTRY_PRICE) * (1 - FEE_FACTOR)
            pnl = (shares - bet) if won else -bet
            total_pnl += pnl
            bankroll += pnl
            if won:
                wins += 1

        wr = wins / n_trades * 100
        daily_pnl = total_pnl / (n_trades / 288)
        log.info(f"  {name:<12s}: {n_trades} trades, {wr:.1f}% WR, "
                 f"PnL=${total_pnl:+.2f} (${daily_pnl:+.1f}/day), bankroll=${bankroll:.2f}")

    # ---- Save best model ----
    log.info(f"\n{'='*60}")
    log.info("SAVING MODELS")
    log.info(f"{'='*60}")

    with open(MODEL_DIR / "best_model.pkl", "wb") as f:
        pickle.dump(best_model, f)
    with open(MODEL_DIR / "scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)

    model_meta = {
        "model_type": best_model_name,
        "feature_names": FEATURE_NAMES,
        "n_features": len(FEATURE_NAMES),
        "train_samples": len(y_train),
        "test_samples": len(y_test),
        "test_accuracy": best_test_acc,
        "baseline_accuracy": baseline,
        "paradigm": "always_trade",
    }
    with open(MODEL_DIR / "model_meta.json", "w") as f:
        json.dump(model_meta, f, indent=2)

    # Save all models
    for mname, model in models.items():
        with open(MODEL_DIR / f"{mname}.pkl", "wb") as f:
            pickle.dump(model, f)

    log.info(f"  Saved {best_model_name} -> models/best_model.pkl")
    log.info(f"  Saved scaler -> models/scaler.pkl")
    log.info(f"  Saved metadata -> models/model_meta.json")
    log.info(f"\nDone.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("BTC 5-Min Direction — ML Training (Always Trade)")
    log.info("=" * 60)

    candles = load_candles()
    X, y, meta = build_dataset(candles)

    if len(y) < 200:
        log.error(f"Only {len(y)} samples — need more data")
        sys.exit(1)

    train(X, y, meta)
