"""Comprehensive backtesting suite for the ML model.

Runs 5 analyses:
1. Realistic simulation (adaptive sizing + fees + spreads)
2. Walk-forward analysis (rolling train/test windows)
3. Hour-based sizing optimization
4. Confidence threshold sweep
5. Out-of-sample test (fresh Binance data)

Usage:
    python backtest_comprehensive.py
"""

import csv
import io
import logging
import os
import pickle
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

import numpy as np

# Reuse feature extraction from ml_pipeline
from ml_pipeline import (
    Candle,
    extract_features,
    FEATURE_NAMES,
    download_binance_klines,
    _parse_kline_zip,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent / "models"
DATA_DIR = Path(__file__).parent / "historical_data"


def load_model_and_scaler():
    """Load the trained model and scaler."""
    with open(MODEL_DIR / "best_model.pkl", "rb") as f:
        model = pickle.load(f)
    with open(MODEL_DIR / "scaler.pkl", "rb") as f:
        scaler = pickle.load(f)
    log.info(f"Loaded model: {type(model).__name__}")
    return model, scaler


def load_dataset():
    """Load the saved dataset."""
    data = np.load(MODEL_DIR / "dataset.npz", allow_pickle=True)
    X = data["X"]
    y = data["y"]
    # Reconstruct hour from time features: hour_sin (idx 34), hour_cos (idx 35)
    # hour = atan2(sin, cos) * 24 / (2*pi)
    hour_sin = X[:, 34]
    hour_cos = X[:, 35]
    hours = np.arctan2(hour_sin, hour_cos) * 24 / (2 * np.pi)
    hours = np.round(hours) % 24
    log.info(f"Loaded dataset: {X.shape[0]} samples, {X.shape[1]} features")
    return X, y, hours.astype(int)


def compute_polymarket_fee(price, fee_rate=0.25, fee_exponent=2):
    """Compute Polymarket taker fee factor."""
    return fee_rate * (price * (1 - price)) ** fee_exponent


# =========================================================================
# ANALYSIS 1: Realistic simulation with adaptive sizing + fees
# =========================================================================
def analysis_1_realistic_simulation(model, scaler, X, y, hours):
    log.info("\n" + "=" * 70)
    log.info("  ANALYSIS 1: Realistic Simulation (Adaptive Sizing + Fees)")
    log.info("=" * 70)

    split = int(len(y) * 0.80)
    X_test = X[split:]
    y_test = y[split:]
    hours_test = hours[split:]

    X_test_s = scaler.transform(X_test)
    probs = model.predict_proba(X_test_s)[:, 1]
    probs = np.clip(probs, 0.05, 0.95)

    # Simulate with adaptive sizing
    bankroll = 20.0
    initial_bankroll = 20.0
    peak_bankroll = 20.0
    trades = []
    recent_outcomes = []  # for rolling WR
    consecutive_losses = 0

    for i in range(len(y_test)):
        p_up = probs[i]
        confidence = abs(p_up - 0.5)

        # Skip if no signal (mirrors live fix)
        if confidence < 0.005:
            continue

        # Direction
        side_up = p_up > 0.5
        pred_correct = (side_up and y_test[i] == 1) or (not side_up and y_test[i] == 0)

        # Entry price: simulate realistic spread
        # In a 50/50 market, midpoint ~ 0.50, ask ~ 0.51-0.52
        entry_mid = 0.50
        spread = 0.02  # typical 2-cent spread
        entry_price = entry_mid + spread / 2  # buy at ask

        # Adaptive sizing
        base_pct = 0.02  # 2% of bankroll
        size = bankroll * base_pct

        # Confidence multiplier (0.5x-2.0x)
        conf_mult = 0.5 + (confidence / 0.10) * 1.5  # scale 0-10% conf to 0.5-2.0
        conf_mult = max(0.5, min(2.0, conf_mult))
        size *= conf_mult

        # Streak multiplier
        if consecutive_losses >= 5:
            size *= 0.5
        elif consecutive_losses >= 3:
            size *= 0.75

        # Drawdown multiplier
        drawdown = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
        if drawdown > 0.25:
            size *= 0.5
        elif drawdown > 0.15:
            size *= 0.75

        # Rolling WR multiplier
        if len(recent_outcomes) >= 20:
            rolling_wr = sum(recent_outcomes[-20:]) / 20
            if rolling_wr > 0.55:
                size *= 1.3
            elif rolling_wr < 0.45:
                size *= 0.7

        # Clamp to [0.5%, 8%] of bankroll
        size = max(bankroll * 0.005, min(bankroll * 0.08, size))

        # Don't bet more than we have
        if size > bankroll:
            size = bankroll
        if bankroll <= 0.01:
            break

        # Fee
        fee_factor = compute_polymarket_fee(entry_price)
        shares = (size / entry_price) * (1 - fee_factor)

        # Settlement
        if pred_correct:
            pnl = shares - size  # WIN: shares resolve to $1 each
            consecutive_losses = 0
        else:
            pnl = -size  # LOSE: shares worth $0
            consecutive_losses += 1

        bankroll += pnl
        peak_bankroll = max(peak_bankroll, bankroll)
        recent_outcomes.append(1 if pred_correct else 0)

        trades.append({
            "i": i,
            "p_up": p_up,
            "confidence": confidence,
            "size": size,
            "pnl": pnl,
            "bankroll": bankroll,
            "correct": pred_correct,
            "hour": hours_test[i],
        })

    # Results
    n_trades = len(trades)
    wins = sum(1 for t in trades if t["correct"])
    total_pnl = bankroll - initial_bankroll
    wr = wins / n_trades * 100 if n_trades else 0

    log.info(f"\n  Starting bankroll: ${initial_bankroll:.2f}")
    log.info(f"  Final bankroll:   ${bankroll:.2f}")
    log.info(f"  Net P&L:          ${total_pnl:+.2f}")
    log.info(f"  ROI:              {total_pnl/initial_bankroll*100:+.1f}%")
    log.info(f"  Total trades:     {n_trades}")
    log.info(f"  Win rate:         {wr:.1f}%")
    log.info(f"  Peak bankroll:    ${peak_bankroll:.2f}")

    min_bankroll = min(t["bankroll"] for t in trades) if trades else initial_bankroll
    max_drawdown_pct = (peak_bankroll - min_bankroll) / peak_bankroll * 100
    log.info(f"  Min bankroll:     ${min_bankroll:.2f}")
    log.info(f"  Max drawdown:     {max_drawdown_pct:.1f}%")

    avg_size = np.mean([t["size"] for t in trades]) if trades else 0
    log.info(f"  Avg bet size:     ${avg_size:.3f}")

    # Trajectory at milestones
    log.info(f"\n  Bankroll trajectory:")
    milestones = [0, n_trades//10, n_trades//4, n_trades//2,
                  3*n_trades//4, 9*n_trades//10, n_trades-1]
    for m in milestones:
        if 0 <= m < len(trades):
            t = trades[m]
            wr_so_far = sum(1 for tt in trades[:m+1] if tt["correct"]) / (m+1) * 100
            log.info(f"    Trade {m+1:5d}: ${t['bankroll']:.2f}  (WR={wr_so_far:.1f}%)")

    return trades


# =========================================================================
# ANALYSIS 2: Walk-forward analysis
# =========================================================================
def analysis_2_walk_forward(X, y, hours):
    from sklearn.preprocessing import StandardScaler
    from sklearn.ensemble import RandomForestClassifier

    log.info("\n" + "=" * 70)
    log.info("  ANALYSIS 2: Walk-Forward Analysis (Rolling Windows)")
    log.info("=" * 70)

    # No timestamps available — use sample indices as proxy.
    # Dataset is chronological. ~288 samples/day (24h * 12 windows/h).
    SAMPLES_PER_DAY = 288
    TRAIN_DAYS = 60
    TEST_DAYS = 10
    STEP_DAYS = 10
    TRAIN_N = TRAIN_DAYS * SAMPLES_PER_DAY
    TEST_N = TEST_DAYS * SAMPLES_PER_DAY
    STEP_N = STEP_DAYS * SAMPLES_PER_DAY

    n_total = len(y)
    results = []
    start = 0
    fold = 0

    while start + TRAIN_N + TEST_N <= n_total:
        train_end = start + TRAIN_N
        test_end = train_end + TEST_N

        X_tr, y_tr = X[start:train_end], y[start:train_end]
        X_te, y_te = X[train_end:test_end], y[train_end:test_end]

        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        model = RandomForestClassifier(
            n_estimators=300, max_depth=3, min_samples_leaf=50,
            random_state=42, n_jobs=-1,
        )
        model.fit(X_tr_s, y_tr)

        preds = model.predict(X_te_s)
        probs = model.predict_proba(X_te_s)[:, 1]
        acc = np.mean(preds == y_te)

        # Confidence-filtered accuracy
        conf = np.abs(probs - 0.5)
        conf_mask = conf >= 0.02
        conf_acc = np.mean(preds[conf_mask] == y_te[conf_mask]) if np.sum(conf_mask) > 0 else 0

        fold += 1
        train_day_start = start // SAMPLES_PER_DAY
        test_day_start = train_end // SAMPLES_PER_DAY

        results.append({
            "fold": fold,
            "train_day": f"d{train_day_start}",
            "test_day": f"d{test_day_start}",
            "train_n": len(y_tr),
            "test_n": len(y_te),
            "accuracy": acc,
            "conf_accuracy": conf_acc,
            "conf_pct": np.mean(conf_mask) * 100,
        })

        start += STEP_N

    # Print results
    log.info(f"\n  {'Fold':>4s}  {'Train':>8s}  {'Test':>8s}  {'Train N':>7s}  {'Test N':>6s}  "
             f"{'All WR':>6s}  {'Conf WR':>7s}  {'Conf%':>5s}")
    log.info(f"  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*5}")

    for r in results:
        log.info(f"  {r['fold']:>4d}  {r['train_day']:>8s}  {r['test_day']:>8s}  {r['train_n']:>7d}  "
                 f"{r['test_n']:>6d}  {r['accuracy']*100:>5.1f}%  "
                 f"{r['conf_accuracy']*100:>6.1f}%  {r['conf_pct']:>4.0f}%")

    accs = [r["accuracy"] for r in results]
    conf_accs = [r["conf_accuracy"] for r in results]
    log.info(f"\n  Overall: mean={np.mean(accs)*100:.1f}%, "
             f"std={np.std(accs)*100:.1f}%, "
             f"min={np.min(accs)*100:.1f}%, max={np.max(accs)*100:.1f}%")
    log.info(f"  Confidence-filtered: mean={np.mean(conf_accs)*100:.1f}%, "
             f"min={np.min(conf_accs)*100:.1f}%, max={np.max(conf_accs)*100:.1f}%")

    above_50 = sum(1 for a in accs if a > 0.5)
    log.info(f"  Windows above 50%: {above_50}/{len(accs)} ({above_50/len(accs)*100:.0f}%)")

    return results


# =========================================================================
# ANALYSIS 3: Hour-based sizing optimization
# =========================================================================
def analysis_3_hour_sizing(model, scaler, X, y, hours_all):
    log.info("\n" + "=" * 70)
    log.info("  ANALYSIS 3: Hour-Based Sizing Optimization")
    log.info("=" * 70)

    split = int(len(y) * 0.80)
    X_test = X[split:]
    y_test = y[split:]
    hours = hours_all[split:]

    X_test_s = scaler.transform(X_test)
    preds = model.predict(X_test_s)
    probs = model.predict_proba(X_test_s)[:, 1]

    log.info(f"\n  Hour-by-hour performance on test set:")
    log.info(f"  {'Hour':>4s}  {'Trades':>6s}  {'WR':>6s}  {'Simulated $5 PnL':>18s}")
    log.info(f"  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*18}")

    hour_wr = {}
    for h in range(24):
        mask = hours == h
        if np.sum(mask) < 10:
            continue
        acc = np.mean(preds[mask] == y_test[mask])
        n = np.sum(mask)
        pnl = sum(5 * (0.96 - 1) if preds[mask][i] != y_test[mask][i]
                  else 5 * (1/0.52 * 0.984 - 1)
                  for i in range(n))
        hour_wr[h] = acc
        bar = "#" * max(0, int((acc - 0.45) * 200))
        log.info(f"  {h:4d}  {n:6d}  {acc*100:5.1f}%  ${pnl:+.2f}  {bar}")

    # Simulate hour-weighted strategy
    log.info(f"\n  --- Hour-Weighted Sizing Strategies ---")

    strategies = {
        "Flat $5": lambda h: 5.0,
        "Hour-scaled (1x-2x)": lambda h: 5.0 * (1.0 + max(0, hour_wr.get(h, 0.5) - 0.50) * 10),
        "Skip worst hours": lambda h: 5.0 if hour_wr.get(h, 0.5) >= 0.50 else 0.0,
        "Skip bottom 6 hours": lambda h: 5.0 if h not in sorted(hour_wr, key=hour_wr.get)[:6] else 0.0,
        "Best 8 hours only": lambda h: 5.0 if h in sorted(hour_wr, key=hour_wr.get, reverse=True)[:8] else 0.0,
    }

    for strat_name, size_fn in strategies.items():
        total_pnl = 0
        n_trades = 0
        wins = 0
        for i in range(len(y_test)):
            h = hours[i]
            size = size_fn(h)
            if size <= 0:
                continue
            n_trades += 1
            correct = preds[i] == y_test[i]
            if correct:
                wins += 1
                fee_factor = compute_polymarket_fee(0.52)
                shares = (size / 0.52) * (1 - fee_factor)
                total_pnl += shares - size
            else:
                total_pnl -= size

        wr = wins / n_trades * 100 if n_trades else 0
        per_day = total_pnl / (len(y_test) / 288) if len(y_test) > 0 else 0  # 288 = 24*12 windows/day
        log.info(f"  {strat_name:25s}: {n_trades:5d} trades, {wr:5.1f}% WR, "
                 f"PnL ${total_pnl:+.0f} (${per_day:+.0f}/day)")


# =========================================================================
# ANALYSIS 4: Confidence threshold sweep
# =========================================================================
def analysis_4_confidence_sweep(model, scaler, X, y, hours_all):
    log.info("\n" + "=" * 70)
    log.info("  ANALYSIS 4: Confidence Threshold Sweep")
    log.info("=" * 70)

    split = int(len(y) * 0.80)
    X_test = X[split:]
    y_test = y[split:]

    X_test_s = scaler.transform(X_test)
    probs = model.predict_proba(X_test_s)[:, 1]
    probs = np.clip(probs, 0.05, 0.95)
    preds = model.predict(X_test_s)
    confidence = np.abs(probs - 0.5)

    thresholds = [0.00, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]

    log.info(f"\n  {'Threshold':>9s}  {'Trades':>6s}  {'%Traded':>7s}  {'WR':>6s}  "
             f"{'$5 PnL':>8s}  {'$/day':>7s}  {'Avg Conf':>8s}")
    log.info(f"  {'-'*9}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*8}")

    test_days = len(y_test) / 288.0

    best_threshold = 0
    best_daily_pnl = -9999

    for thresh in thresholds:
        mask = confidence >= thresh
        n = np.sum(mask)
        if n < 10:
            continue

        acc = np.mean(preds[mask] == y_test[mask])
        avg_conf = np.mean(confidence[mask])

        # Simulate PnL with $5 flat bets and fees
        total_pnl = 0
        for i in np.where(mask)[0]:
            correct = preds[i] == y_test[i]
            if correct:
                fee_factor = compute_polymarket_fee(0.52)
                shares = (5.0 / 0.52) * (1 - fee_factor)
                total_pnl += shares - 5.0
            else:
                total_pnl -= 5.0

        daily_pnl = total_pnl / test_days
        pct_traded = n / len(y_test) * 100

        if daily_pnl > best_daily_pnl:
            best_daily_pnl = daily_pnl
            best_threshold = thresh

        log.info(f"  {thresh:>8.3f}  {n:6d}  {pct_traded:6.1f}%  {acc*100:5.1f}%  "
                 f"${total_pnl:+7.0f}  ${daily_pnl:+6.0f}  {avg_conf*100:7.2f}%")

    log.info(f"\n  >>> Best threshold for $/day: {best_threshold:.3f} (${best_daily_pnl:+.0f}/day)")

    # Sharpe-like ratio analysis
    log.info(f"\n  --- Risk-adjusted returns ---")
    for thresh in [0.00, 0.01, 0.02, 0.03, 0.05]:
        mask = confidence >= thresh
        if np.sum(mask) < 50:
            continue

        pnl_per_trade = []
        for i in np.where(mask)[0]:
            correct = preds[i] == y_test[i]
            if correct:
                fee_factor = compute_polymarket_fee(0.52)
                shares = (5.0 / 0.52) * (1 - fee_factor)
                pnl_per_trade.append(shares - 5.0)
            else:
                pnl_per_trade.append(-5.0)

        pnl_arr = np.array(pnl_per_trade)
        mean_pnl = np.mean(pnl_arr)
        std_pnl = np.std(pnl_arr)
        sharpe = mean_pnl / std_pnl * np.sqrt(288) if std_pnl > 0 else 0  # annualized by windows/day

        log.info(f"  threshold={thresh:.3f}: mean=${mean_pnl:+.3f}/trade, "
                 f"std=${std_pnl:.3f}, pseudo-Sharpe={sharpe:.2f}")


# =========================================================================
# ANALYSIS 5: Out-of-sample test (fresh data)
# =========================================================================
def analysis_5_out_of_sample(model, scaler):
    log.info("\n" + "=" * 70)
    log.info("  ANALYSIS 5: Out-of-Sample Test (Fresh Binance Data)")
    log.info("=" * 70)

    # The model was trained on data up to ~Feb 28. Try to get fresh daily data.
    now = datetime.now(timezone.utc)
    symbol = "BTCUSDT"
    interval = "1m"
    daily_base = f"https://data.binance.vision/data/spot/daily/klines/{symbol}/{interval}"

    all_candles = []
    log.info("  Downloading recent daily klines for out-of-sample test...")

    # Try last 7 days (some may overlap with training, that's OK for now)
    for days_ago in range(7, 0, -1):
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
            log.info(f"    Downloaded {filename}: {len(candles)} candles")
        except Exception:
            continue
        time.sleep(0.3)

    if not all_candles:
        log.warning("  No fresh data available for out-of-sample test")
        return

    # Deduplicate and sort
    seen = set()
    unique = []
    for c in all_candles:
        ts = c.timestamp
        if ts > 1_000_000_000_000_000:
            ts = ts // 1000
            c = Candle(ts, c.open, c.high, c.low, c.close, c.volume, c.taker_buy_volume, c.trades)
        if ts not in seen:
            seen.add(ts)
            unique.append(c)
    unique.sort(key=lambda c: c.timestamp)
    log.info(f"  Fresh candles: {len(unique)}")

    if len(unique) < 100:
        log.warning("  Not enough fresh data")
        return

    # Build windows and predict
    candle_by_ts = {c.timestamp: c for c in unique}
    timestamps = sorted(candle_by_ts.keys())

    first_ts = timestamps[0] // 1000
    last_ts = timestamps[-1] // 1000
    first_window = first_ts - (first_ts % 300) + 300
    last_window = last_ts - (last_ts % 300)

    predictions = []
    window_ts = first_window + 300 * 60  # skip first 60 windows for history

    while window_ts <= last_window - 300:
        start_ms = window_ts * 1000
        end_ms = (window_ts + 300) * 1000

        hist_times = [t for t in timestamps if t < start_ms]
        if len(hist_times) < 30:
            window_ts += 300
            continue

        hist_candles = [candle_by_ts[t] for t in hist_times[-60:]]

        start_price = candle_by_ts[hist_times[-1]].close

        end_candidates = [t for t in timestamps if end_ms - 60000 <= t <= end_ms + 60000]
        if not end_candidates:
            window_ts += 300
            continue
        best_end = min(end_candidates, key=lambda t: abs(t - end_ms))
        end_price = candle_by_ts[best_end].close

        if start_price == end_price:
            window_ts += 300
            continue

        label = 1 if end_price > start_price else 0

        feat = extract_features(hist_candles, window_ts)
        if feat is None:
            window_ts += 300
            continue

        feat_scaled = scaler.transform(feat.reshape(1, -1))
        pred = model.predict(feat_scaled)[0]
        prob = model.predict_proba(feat_scaled)[0, 1]
        confidence = abs(prob - 0.5)

        predictions.append({
            "ts": window_ts,
            "pred": pred,
            "label": label,
            "prob": prob,
            "confidence": confidence,
            "correct": pred == label,
            "hour": datetime.fromtimestamp(window_ts, tz=timezone.utc).hour,
        })

        window_ts += 300

    if not predictions:
        log.warning("  No predictions generated")
        return

    n = len(predictions)
    wins = sum(1 for p in predictions if p["correct"])
    wr = wins / n * 100

    from scipy import stats as sp_stats
    p_val = sp_stats.binomtest(wins, n, 0.5, alternative='greater').pvalue

    log.info(f"\n  Out-of-sample results:")
    log.info(f"  Total windows:  {n}")
    log.info(f"  Correct:        {wins}")
    log.info(f"  Win rate:       {wr:.1f}%")
    log.info(f"  p-value:        {p_val:.4f} {'***' if p_val < 0.01 else '**' if p_val < 0.05 else '*' if p_val < 0.1 else 'n.s.'}")

    # Date range
    first_dt = datetime.fromtimestamp(predictions[0]["ts"], tz=timezone.utc)
    last_dt = datetime.fromtimestamp(predictions[-1]["ts"], tz=timezone.utc)
    log.info(f"  Date range:     {first_dt.strftime('%Y-%m-%d')} to {last_dt.strftime('%Y-%m-%d')}")

    # Confidence-filtered
    for thresh in [0.01, 0.02, 0.03, 0.05]:
        filtered = [p for p in predictions if p["confidence"] >= thresh]
        if len(filtered) < 10:
            continue
        f_wr = sum(1 for p in filtered if p["correct"]) / len(filtered) * 100
        log.info(f"  conf>={thresh:.2f}: {len(filtered)} trades ({len(filtered)/n*100:.0f}%), {f_wr:.1f}% WR")

    # By hour
    log.info(f"\n  By hour:")
    for h in range(24):
        hour_preds = [p for p in predictions if p["hour"] == h]
        if len(hour_preds) < 5:
            continue
        h_wr = sum(1 for p in hour_preds if p["correct"]) / len(hour_preds) * 100
        bar = "#" * max(0, int((h_wr - 45) * 4))
        log.info(f"    {h:02d}:00  {len(hour_preds):3d} trades  {h_wr:5.1f}%  {bar}")

    return predictions


# =========================================================================
# Main
# =========================================================================
def main():
    start_time = time.time()

    log.info("=" * 70)
    log.info("  BTC ML Model — Comprehensive Backtesting Suite")
    log.info("=" * 70)

    model, scaler = load_model_and_scaler()
    X, y, hours = load_dataset()

    # Run all analyses
    analysis_1_realistic_simulation(model, scaler, X, y, hours)
    analysis_2_walk_forward(X, y, hours)
    analysis_3_hour_sizing(model, scaler, X, y, hours)
    analysis_4_confidence_sweep(model, scaler, X, y, hours)
    analysis_5_out_of_sample(model, scaler)

    elapsed = time.time() - start_time
    log.info(f"\n{'=' * 70}")
    log.info(f"  All analyses complete in {elapsed:.0f}s")
    log.info(f"{'=' * 70}")


if __name__ == "__main__":
    main()
