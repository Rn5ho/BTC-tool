# Exit-Probability ML Model Design

**Status**: Research complete -- design document
**Date**: 2026-03-10
**Author**: Claude (research session)

---

## Executive Summary

An exit-probability model that predicts `P(max_bid >= threshold)` would theoretically unlock up to $1,750 more PnL over the current 756-trade dataset (perfect oracle ceiling). However, extensive data analysis reveals that **entry-time Binance features have near-zero predictive power for bid spike height** (R2 = 0.004, individual feature |r| < 0.10). The strongest predictor is entry_price (r = 0.32), which the existing 4-tier threshold system already exploits. A practical ML model would likely yield $50-150 improvement at best.

The bigger opportunity is **selective trading** (skipping the worst 10% of trades) rather than dynamic thresholds, and this requires a fundamentally different feature set than what is currently collected.

---

## Data Investigation Findings

### Available Training Data

| Source | Rows | Both Sides | Features | Bias Issues |
|--------|------|-----------|----------|-------------|
| `live_trades` (with max_bid) | 756 | No (bought side only) | 17 entry-time features | Side selection, EE truncation, market impact |
| `shadow_windows` (usable) | 22 | Yes | 11 Binance features | Just started collecting 2026-03-10 16:35 |
| `market_snapshots` + `feature_snapshots` | **2,807** | **Yes** | 14 features | None -- best available dataset |

**Critical discovery**: The `market_snapshots` table (285K intrawindow polls) combined with `feature_snapshots` (285K entry-time snapshots) provides **2,807 complete windows** with both-side max_bid data and full features. This is already 6x the minimum sample requirement and eliminates the need to wait for shadow window collection.

### Target Variable Distribution (2,807 windows)

**Model-chosen side max_bid (what we actually trade):**
- >= 0.40: 98.7%
- >= 0.50: 92.4%
- >= 0.60: 80.0%
- >= 0.70: 68.0%
- >= 0.80: 57.7%
- >= 0.90: 48.9%

**Best side max_bid (direction-agnostic):**
- >= 0.70: 99.9%
- >= 0.90: 90.6%

**Key insight**: Almost every window (99.9%) has a spike above 0.70 on at least one side. The challenge is not whether a spike occurs, but which side it occurs on -- and that is the direction prediction problem.

### Feature Predictive Power

**Pearson correlations with max_bid_during_window (756 live trades):**

| Feature | r | Signal |
|---------|---|--------|
| entry_price | +0.325 | Strong -- but already exploited by tier system |
| ABS(BTC move %) | -0.264 | Moderate -- larger moves = LOWER max_bid (surprising) |
| model_confidence | -0.096 | Weak inverse -- high confidence = lower spikes |
| entry_atr | -0.077 | Weak -- lower vol = higher spikes |
| entry_token_ask_size | -0.081 | Weak -- larger ask depth = lower spikes |
| entry_taker_ratio | +0.066 | Weak |
| All other Binance features | |r| < 0.07 | Noise |

**Hour of day** shows meaningful variation (avg max_bid: H17=0.868 vs H01=0.721) but was not a strong feature in GradientBoosting (importance: 0.012-0.017).

**Temporal autocorrelation** is negligible (lag-1 r=0.07, lag-2 r=0.04). Spike clustering is modest: P(spike | prev spike)=66.5% vs P(spike | prev no-spike)=58.9%.

### ML Feasibility Test Results (2,807 windows from market_snapshots)

**Classification: P(model-side max_bid >= 0.70)**
- Baseline rate: 68% positive
- GradientBoosting accuracy: **65.8%** (BELOW baseline)
- RandomForest accuracy: equivalent
- Model cannot beat "predict all positive"

**Regression: predict model-side max_bid value**
- GradientBoosting R2: **0.004** (essentially zero -- no better than predicting the mean)
- MAE: 0.175 (prediction range compressed to 0.449-0.986 vs actual 0.250-0.998)

**Regression: predict UP-side max_bid specifically**
- R2: 0.062 (marginally better but still nearly useless)

### Direction Model Contribution

The current direction model (47% True Model WR) provides a small but real edge in spike selection:

- Model-chosen side avg max_bid: 0.805
- Wrong side avg max_bid: 0.778
- Model side has higher max_bid: 52.2% of windows (vs 50% random)
- Spike advantage at 0.70 threshold: +4.4% more windows spike on model's side

This is small but consistent across all threshold levels (+3.9% to +5.9%). The direction model helps but is not the primary driver of profitability.

### Economic Upper Bounds

| Strategy | PnL on 756 trades | Per trade |
|----------|-------------------|-----------|
| Perfect oracle (best action per trade) | $1,995 | $2.64 |
| Skip worst 10% of trades | $764 | $1.12 |
| Skip worst 20% of trades | $1,045 | $1.73 |
| **Current system (4-tier EE)** | **$245** | **$0.32** |
| Best static threshold (0.95) | $62 | $0.08 |
| No EE (all settlement) | ~-$150 | ~-$0.20 |

The current 4-tier system outperforms any static threshold. The gap between current ($245) and skip-worst-10% ($764) represents $519 of theoretically capturable improvement.

---

## Recommended Model Design

### What NOT to Build

Do **not** build a model that predicts max_bid height from entry-time Binance features. The data conclusively shows:
1. R2 = 0.004 -- there is no signal
2. The dominant predictor (entry_price) is already used
3. All individual Binance features correlate at |r| < 0.10 with max_bid

### What to Build Instead: Trade Quality Classifier

The highest-value model is one that identifies **which specific trades will lose money** -- enabling selective skipping or reduced bet sizing. The target is not "will the bid spike?" but rather **"will this trade be profitable?"** (combining direction correctness, spike timing, and exit execution).

### Target Variable

**Primary**: Binary classification
`y = 1 if actual_pnl > 0 else 0`

This directly targets the economic outcome. A trade is profitable if either:
- It goes to settlement and wins (correct direction)
- It gets an early exit above entry price (spike + sell execution)
- Either outcome: `y = 1` when `outcome in ('WIN', 'EARLY_EXIT')`

From live data: ~60% of trades are profitable (414 EE + 36 WIN = 450 out of 756). This is a balanced enough target.

**Secondary (regression, for bet sizing)**:
`y = actual_pnl` (continuous)

### Feature Set (22 features)

**Entry-time Binance indicators (11)** -- already collected on live_trades and shadow_windows:
1. `entry_obi` -- order book imbalance
2. `entry_taker_ratio` -- taker buy/sell ratio
3. `entry_momentum_1m` -- 1-min price momentum
4. `entry_momentum_5m` -- 5-min price momentum
5. `entry_rsi` -- RSI-14
6. `entry_vwap_dev` -- VWAP deviation
7. `entry_bb_position` -- Bollinger Band position
8. `entry_ema_cross` -- EMA cross strength
9. `entry_funding_zscore` -- funding rate z-score
10. `entry_volume_zscore` -- volume z-score
11. `entry_atr` -- ATR-14

**Polymarket state (4)** -- already collected:
12. `entry_price` -- entry ask price (strongest single predictor, r=0.32)
13. `entry_up_spread` -- UP token spread
14. `entry_down_spread` -- DOWN token spread
15. `market_imbalance` -- `|up_ask - down_ask|` (derived; balanced markets spike more)

**Temporal features (4)** -- need to derive:
16. `hour_sin` -- sin(2pi * hour/24) cyclical encoding
17. `hour_cos` -- cos(2pi * hour/24) cyclical encoding
18. `prev_window_outcome` -- last window result (0=loss, 1=EE/win)
19. `recent_spike_rate` -- fraction of last 5 windows with max_bid >= 0.70

**Regime features (3)** -- partially collected:
20. `regime_strength` -- signed regime strength (-1 to +1)
21. `model_confidence` -- P(up) from direction model
22. `model_vs_market` -- `model_confidence - entry_price` (model disagrees with market)

### Model Type

**Recommended: GradientBoosting (XGBoost or LightGBM) classifier**

Rationale:
- Handles mixed feature types (continuous indicators + derived categoricals)
- Handles non-linear interactions (entry_price x ATR, hour x regime)
- Tree-based models degrade gracefully with weak features
- Calibrated probabilities via `predict_proba()` enable threshold tuning
- Fast inference (<1ms) for real-time use

**NOT recommended**:
- Neural networks: insufficient data (756-2807 samples), overfitting risk
- Linear models: interactions matter more than main effects here
- Random Forest: GBM consistently outperforms on structured data this size

### Training Approach

**Phase 1: Train on live_trades (now)**

Data: 756 trades with complete features and known outcomes
- Split: temporal 70/15/15 (train/val/test) -- roughly 529/113/114 trades
- Target: `outcome in ('WIN', 'EARLY_EXIT')` as positive class
- Validation: walk-forward cross-validation (5 temporal folds)
- Hyperparameters: max_depth=3-5, n_estimators=100-300, learning_rate=0.05-0.1
- Regularization: min_samples_leaf=20 (prevents overfitting small data)

**Bias handling**:
- EE truncation: for EE trades, use `gamma_winner_matches` as ground truth (available for 525/525 EE trades)
- Side selection: model only sees trades we chose; shadow data will fix this later but 756 trades is sufficient to start
- Market impact: small ($5-10 bets on $200+ token pools) -- likely negligible

**Phase 2: Retrain on market_snapshots + feature_snapshots (now, using existing data)**

Data: 2,807 windows with both-side max_bid and entry-time features
- Target reformulation needed: no `pnl` outcome, only max_bid values
- Use proxy target: `max_bid_for_model_side >= entry_price + 0.15` (profitable EE)
- This eliminates side bias entirely: we can evaluate BOTH sides per window

**Phase 3: Retrain with shadow_windows (2+ days from now)**

Data: growing ~288 windows/day, with both-side bids + traded/skipped info
- Adds `traded_side` and `traded_tag` for counterfactual analysis
- Higher-quality bid data (10s polling vs market_snapshots' variable polling)
- Eventually overtakes market_snapshots as primary training source

### Minimum Data Requirements

- 22 features x 15 samples/feature = **330 minimum samples**
- Current: 756 live trades, 2,807 reconstructed windows -- both sufficient
- For robust validation: want 1,000+ samples (achievable from market_snapshots)
- For reliable feature importance: want 2,000+ (already available)

### Evaluation Metrics

1. **AUC-ROC**: Primary metric. Must exceed 0.55 to have any signal (random = 0.50)
2. **Precision at 30% recall**: Can we identify and SKIP the worst trades?
3. **Economic backtest**: Simulate PnL with model-guided skip/sizing decisions on held-out test set
4. **Calibration**: Predicted probabilities should match actual win rates in decile buckets

### Integration Strategy

**Recommended: Stacked architecture (direction model picks side, exit model decides sizing)**

The exit-probability model does NOT replace the direction model. It operates downstream:

```
Direction model: P(BTC goes UP) = 0.58
  -> Pick side: UP
  -> Entry price: 0.52

Exit model: P(profitable trade | features, side=UP, entry=0.52) = 0.72
  -> Decision: TRADE (confidence > 0.40 threshold)
  -> Sizing: scale bet by predicted probability (e.g., $7 instead of $5)
  -> EE threshold: dynamic based on predicted max_bid distribution
```

**Specific integration points:**

1. **Skip low-probability trades** (highest value):
   - If exit model says P(profitable) < 0.35, skip the window
   - Skip worst 10% of trades: estimated +$50-100/week improvement
   - Fallback: paper-trade skipped windows for continued data collection

2. **Adaptive bet sizing** (medium value):
   - Scale bet size by P(profitable): high confidence = bigger bet
   - Conservative: `bet = base_bet * max(0.5, min(2.0, P/0.6))`
   - Requires P(profitable) to be well-calibrated

3. **Dynamic EE thresholds** (lower value, higher risk):
   - If model predicts high spike probability, raise threshold (let it run)
   - If model predicts low spike probability, lower threshold (grab what you can)
   - Risk: threshold changes interact with execution in complex ways

### Expected Improvement

**Realistic estimate**: $50-150 additional PnL per week over current 4-tier system.

Rationale:
- Skip-worst-10% theoretical ceiling: ~$70/day ($490/week) from backtest
- ML model will capture 10-20% of theoretical ceiling
- Net: ~$50-100/week from selective skipping
- Additional ~$0-50/week from adaptive sizing (uncertain)

**Why not more?**
- Entry-time features have weak predictive power (R2 ~ 0)
- The strongest predictor (entry_price) is already used by tier system
- Most prediction power requires features we don't have:
  - Intra-window bid trajectory (how fast is bid moving?)
  - Cross-market liquidity state
  - BTC price relative to key technical levels
  - Time since last large BTC move

---

## Alternative Approaches (ranked by expected value)

### 1. Intra-Window Dynamic Exit (highest expected value)

Instead of predicting at entry time, make exit decisions dynamically DURING the window based on bid trajectory:

- If bid is rising fast at t=60s, hold (it will likely reach threshold)
- If bid is falling or stagnant at t=120s, sell early at whatever is available
- This uses REAL-TIME information rather than entry-time predictions
- Requires: bid trajectory tracking (already have 1s polling in EE loop)

### 2. Hour-Aware Tier Thresholds (medium value, easy to implement)

The data shows avg max_bid varies from 0.721 (H01) to 0.868 (H17). Rather than ML, use hour-specific threshold adjustments:

- Peak hours (H17, H00, H16, H18): raise thresholds (more headroom)
- Trough hours (H01, H02, H08, H11): lower thresholds (grab profits early)
- Implementation: simple lookup table, no ML needed

### 3. Retrained Direction Model (medium value, addresses concept drift)

Current model was trained 2026-02-28 on older data. True Model WR degraded from ~54% backtest to ~47% live. Retraining on recent data might recover 2-4% of direction accuracy, which the EE system amplifies:

- At 50% True Model WR (vs 47%): model side spike advantage goes from +4.4% to +8%
- This translates to more EE triggers per window
- Straightforward to implement: just re-run `ml_pipeline.py` with recent data

---

## Open Questions

1. **Shadow tracking data quality**: The shadow bid bug (fixed 2026-03-10 16:35) means only 22 usable shadow rows exist. Market_snapshots is the viable alternative, but its polling frequency varies (62-97 snapshots per window). Is this sufficient to capture true max_bid? Validation shows mean absolute diff of 0.092 vs live tracking.

2. **EE truncation censoring**: For the 414 EE trades in live data, max_bid is right-censored (we sold and stopped tracking). The excess over threshold (avg 0.028, max 0.340) suggests truncation is modest for most trades but severe for some. Survival analysis methods could better model this.

3. **Market impact on bid**: Our $5-10 buy orders push the bid up slightly. Shadow windows (where we don't trade) provide unbiased bid data. Worth comparing: do shadow window bids differ systematically from live trade bids?

4. **Feature stationarity**: All features are measured at the Binance/crypto level, which has regime changes (trending vs ranging). A model trained on recent ranging-market data may fail during trends. Consider regime-aware models or adaptive retraining.
