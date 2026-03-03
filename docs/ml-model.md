# ML Probability Model Reference

## Active Model (v2 — Trained RandomForestClassifier)

Controlled by `USE_ML_MODEL=true` in .env.

### Training (ml_pipeline.py)
- **Dataset**: 34,100 labeled 5-minute windows from 170,947 candles (119 days: Nov 2025 - Feb 2026)
- **Source**: Binance historical klines (monthly ZIP archives from data.binance.vision + recent daily klines)
- **Labels**: Binary (UP=49.9%, DOWN=50.1%) — balanced, no class weighting needed
- **Split**: Time-series 80/20 (no future leakage)
- **Cross-validation**: 5-fold time-series CV

### 44 Features
1. **Momentum** (5): 1m, 3m, 5m, 10m, 20m price returns
2. **RSI** (2): RSI-9, RSI-14
3. **Price vs averages** (6): VWAP deviation, BB position, BB width, EMA crosses (9/21, 5/13), price vs EMA9, price vs EMA21
4. **Volatility** (4): ATR-14, ATR%, recent 5-bar volatility, volatility ratio (5-bar/20-bar)
5. **Volume** (5): volume z-score, volume trend, taker ratio (1m, 3m, 5m)
6. **Candle patterns** (5): upper/lower wick ratio, body ratio, close vs open, avg body ratio (5-bar)
7. **Lag features** (6): previous direction (5m, 10m, 15m, 20m), streak length, mean reversion
8. **Time** (8): cyclical hour (sin/cos), minute, session flags (Asia/Europe/US), cyclical day-of-week (sin/cos)
9. **Microstructure** (2): high-low range %, close position in range

### Results — ALL MODELS STATISTICALLY SIGNIFICANT (p < 0.001)

| Model | CV (5-fold) | Test (6,820) | Simulated P&L ($5 flat) |
|-------|------------|-------------|------------------------|
| RF_d3 | 52.5% | **53.9%** | **+$2,066 ($87/day)** |
| RF_d5 | **52.6%** | 53.6% | +$1,889 ($80/day) |
| HistGBT | 52.3% | 53.5% | +$1,790 ($76/day) |
| GBT_v3 | 52.6% | 52.9% | +$1,446 ($61/day) |
| LogReg | 52.2% | 52.8% | +$1,357 ($57/day) |

**Deployed model**: RF_d3 (best test accuracy 53.9%, lowest overfitting gap)

### Top Features (by importance)
rsi_14 (6.9%), vwap_deviation (6.8%), bb_position (6.4%), ema_cross_9_21 (5.4%), rsi_9 (5.4%), momentum_20m (4.0%), taker_ratio_3m (3.6%)

### Best/Worst Hours (UTC)
- **Best**: 14:00 (62.5%), 19:00 (57.2%), 09:00 (56.9%), 08:00 (56.6%)
- **Worst**: 03:00 (47.9%), 04:00 (48.6%)

### Confidence Thresholding
- threshold=0.02: 43% of windows traded, **56.8% WR**, $73/day
- threshold=0.05: 13% of windows traded, **57.4% WR**, $24/day

### Live Integration (signals/ml_probability.py)
- `MLProbabilityModel` class — drop-in replacement for `ProbabilityModel`
- `predict(features)` — fallback using 12 FeatureVector fields (zero-pads missing 32 features)
- `predict_from_candles(candles, window_start_ts)` — **preferred**: computes all 44 features from candle history
- main.py uses `predict_from_candles()` when ML model is active
- Falls back to rule-based model if model files not found or scikit-learn not installed

## Rule-Based Probability Model (v1 — Legacy Fallback)

Available when `USE_ML_MODEL=false`. Original model from before ML training.

```
P_raw = 0.5 + w_obi*OBI + w_taker*taker + w_momentum*momentum + w_rsi*rsi + w_vwap*vwap + w_funding*funding
P(up) = 0.5 + CONFIDENCE_DAMPEN * (P_raw - 0.5)
```

Each signal normalized to [-0.5, 0.5]. Default weights: OBI=0.05, taker=0.25, momentum=0.05, RSI=0.10, VWAP=0.10, funding=0.10, volume_zscore=0.15, regime=0.20. Confidence dampening=0.6 (shrinks toward 50%). Performance: 49.7% WR over 2,736 trades — essentially a coin flip.
