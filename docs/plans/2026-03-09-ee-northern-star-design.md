# EE Northern Star — Strategic Design Document

> **For future sessions:** This is the master strategic document for the BTC Polymarket system's direction. Read this FIRST before making any changes. It captures 9 days of live trading data analysis and the fundamental insight that drives all future work.

## The Core Insight

**All profitability comes from Early Exit (EE), not from the direction model.**

| Component | P&L | Trades | Avg/Trade |
|-----------|-----|--------|-----------|
| Settlement (WIN+LOSS) | **-$1,355** | 961 | -$1.41 |
| Early Exit | **+$1,541** | 478 | +$3.22 |
| **Net** | **+$186** | 1,439 | +$0.13 |

The direction model (RandomForestClassifier, trained 2026-02-28) has a **47.1% True Model WR** — worse than a coin flip. The system profits because EE harvests bid spikes within 5-minute windows, locking in $3.22 avg profit before settlement uncertainty can destroy the position.

**This means we've been optimizing the wrong thing.** Improving directional accuracy from 47% to 52% helps, but the real leverage is in the EE system — pushing EE trigger rate from 33% to 50% would 7x net profit.

## The Numbers That Matter

### By Trade Type (8.9 days, 1,439 settled trades on $116 deposit)

| Tag | Trades | PnL | Avg | Settle WR | Eff WR | Avg Entry |
|-----|--------|-----|-----|-----------|--------|-----------|
| normal | 681 | +$49 | +$0.07 | 30.8% | 57.7% | 0.522 |
| regime_flip | 374 | +$125 | +$0.33 | **10.9%** | 58.3% | 0.487 |
| maker_fill | 360 | -$11 | -$0.03 | 60.6% | 60.6% | 0.604 |
| post_streak | 16 | +$39 | +$2.43 | 50.0% | 93.8% | 0.533 |

**Key observations:**
- Regime flip has **10.9% settlement WR** (catastrophic) but 58.3% effective WR. It makes money ONLY through EE. The directional flip is essentially random — it just generates positions for EE to harvest.
- Normal trades: ~52% true model WR (backing out EE regrets). Slightly above random. The model provides a small directional edge that EE amplifies.
- Maker fills: 60.6% WR but -$11 net. FOK switch (2026-03-08) should eliminate future maker fills.

### By Entry Price Tier

| Tier | Trades | PnL | Eff WR | EE Count | Note |
|------|--------|-----|--------|----------|------|
| <0.35 | 22 | -$15 | 32% | 0 | Paper-only exploration |
| 0.35-0.40 | 54 | -$12 | 41% | 10 | Marginal |
| **0.40-0.50** | **498** | **+$93** | **53%** | **191** | Sweet spot — cheap entry, good EE |
| 0.50-0.55 | 348 | +$69 | 56% | 118 | Decent |
| >=0.55 | 517 | +$51 | 69% | 159 | High WR but lower EE value |

**Entry 0.40-0.50 is the sweet spot**: cheap enough for EE to trigger easily (threshold 0.90), enough margin for meaningful profit. This tier generates the most total PnL.

### By Hour (UTC) — Top 5 / Bottom 5

| Best | Trades | Avg/Trade | | Worst | Trades | Avg/Trade |
|------|--------|-----------|-|-------|--------|-----------|
| H18 | 64 | +$1.22 | | H02 | 6 | -$1.40 |
| H06 | 62 | +$0.98 | | H01 | 64 | -$0.92 |
| H17 | 73 | +$0.86 | | H14 | 48 | -$0.77 |
| H00 | 49 | +$0.69 | | H11 | 70 | -$0.54 |
| H10 | 63 | +$0.68 | | H08 | 76 | -$0.53 |

**User explicitly rejected hour blocking** — the goal is to make ALL hours profitable through better per-window intelligence, not by avoiding bad hours.

### Daily Trend

| Day | Trades | PnL | Note |
|-----|--------|-----|------|
| Mar 1 | 225 | +$22 | First full day, mostly maker fills |
| Mar 2 | 174 | -$39 | Time gate tightened 120→60s |
| Mar 3 | 196 | +$10 | Audit fixes, 4-tier EE deployed |
| Mar 4 | 162 | +$75 | Best day, dampening removed |
| Mar 5 | 139 | -$35 | Overnight bleed, regime fix |
| Mar 6 | 138 | +$95 | Best day ever |
| Mar 7 | 167 | +$25 | Steady |
| Mar 8 | 156 | +$13 | FOK switch deployed |
| Mar 9 | 81 | +$20 | Partial day |

System oscillates between +$75 and -$39 daily. EE smooths variance but can't eliminate it entirely.

## The Northern Star Vision

> A system that trades every 5-min window, uses a directional model to enter positions, and relies on an intelligent EE system (dynamic thresholds, exit-probability awareness) to harvest profit from bid spikes — making the average trade profitable across all hours.

### What This Means Concretely

1. **Trade every window** (no hour blocking, no reducing volume)
2. **Direction model's job**: Get us into positions. Doesn't need to be >50% WR — just needs to generate positions where bids spike. Even a 47% model works if EE is good enough.
3. **EE system's job**: The profit engine. Detect bid spikes, exit immediately, lock in profit. Currently triggers on 33% of trades — target 50%+.
4. **Exit-probability model's job** (TO BUILD): Predict WHICH windows will have bid spikes. Use this to:
   - Set dynamic EE thresholds per window (lower when spike is likely, higher when unlikely)
   - Size up on high-EE-probability windows
   - Eventually: skip only the windows where EE probability is very low AND settlement WR is bad

### Why This Works

The Polymarket 5-min BTC market has a structural property: **bid prices spike intra-window even when the final settlement direction is unclear**. This happens because:
- Market makers adjust prices as BTC moves during the 5-min window
- A BTC move in our direction causes the bid for our token to spike (even temporarily)
- BTC can move in our direction early in the window, then reverse before settlement
- EE captures the early spike; settlement would have been a loss

This is **volatility harvesting**, not directional prediction. The model just picks a side; EE monetizes the intra-window volatility on that side.

## The Exit-Probability Model — Design

### Target Variable

**Binary**: Did `max_bid_during_window >= exit_threshold` for the traded token?

This is the event that determines EE profit. We want to predict it at entry time.

Alternative: Regression on `max_bid_during_window` — but binary is simpler and directly actionable.

### Training Data

**Source**: `shadow_windows` table (collecting since 2026-03-09 20:40 UTC)

Each row contains:
- `up_max_bid`, `down_max_bid` — the max bid price for each token during the window
- `up_open_ask`, `down_open_ask` — the entry cost (ask price at window open)
- 11 Binance features at entry time (OBI, taker_ratio, momentum, RSI, VWAP, BB, EMA, funding, volume, ATR)
- `regime_state`, `regime_strength`
- `btc_price_start`, `btc_price_end`
- `traded_side`, `traded_tag` (if we traded this window)
- `poll_count` (data quality — how many 10s polls we got)

**Label construction**: For each shadow window, compute:
```python
# For the UP token:
up_entry_price = up_open_ask
up_exit_threshold = get_exit_threshold(up_entry_price)
up_would_ee = 1 if up_max_bid >= up_exit_threshold else 0

# Same for DOWN token
```

This gives us TWO labels per window (one for each side). We can train on both, effectively doubling the dataset.

**Sample size**: ~288 windows/day × 2 sides = ~576 samples/day. Need 500+ windows = ~2 days minimum, ideally 1000+ = ~4 days.

### Features (Candidate List)

**Entry-time Binance features** (11):
- `entry_obi` — order book imbalance
- `entry_taker_ratio` — net buy pressure
- `entry_momentum_1m`, `entry_momentum_5m` — recent price momentum
- `entry_rsi` — RSI-9
- `entry_vwap_dev` — price vs VWAP
- `entry_bb_position` — position in Bollinger Bands
- `entry_ema_cross` — EMA-9 vs EMA-21
- `entry_funding_zscore` — futures funding rate z-score
- `entry_volume_zscore` — volume z-score
- `entry_atr` — ATR-14 (volatility measure)

**Market structure features** (derived from shadow data):
- `entry_price` — the ask price we'd pay to enter (critical — cheaper = easier EE)
- `spread` — bid-ask spread at entry (liquidity indicator)
- `regime_state` — trending_up/trending_down/ranging
- `regime_strength` — continuous [-1, 1]

**Time features**:
- `hour_sin`, `hour_cos` — cyclical hour encoding
- `minute_in_hour` — position within the hour

**Volatility features** (most important category for EE prediction):
- `entry_atr` — already captured
- `recent_range` — high-low range of last few candles (could derive from momentum features)
- `volume_zscore` — high volume = more movement = more bid spikes

### Hypothesis: What Drives EE Probability?

Based on the data analysis:

1. **Entry price** is #1 — cheaper entries have lower EE thresholds, easier to trigger
2. **Volatility (ATR, volume)** is #2 — more volatile windows = more bid movement
3. **Regime** is #3 — ranging markets have more mean-reversion spikes; trending markets have one-directional spikes (good if we're on the right side)
4. **Time of day** is #4 — London/NY overlap (H06-H18) has more volatility
5. **Direction features** (RSI, momentum) are lower priority — EE probability is mostly about volatility, not direction

### Integration Options

**Option A: Dynamic EE thresholds** (simplest)
- Model predicts EE probability at current threshold
- If probability > 70%: lower threshold slightly (capture more profit)
- If probability < 30%: raise threshold (only exit on strong spikes)
- No change to trading logic, just EE threshold becomes dynamic

**Option B: Trade gating** (more aggressive)
- Only trade windows where EE probability > X%
- Skip low-EE-probability windows entirely (saves capital for better opportunities)
- Risk: reduces volume, might miss some winners
- **User may not want this** — conflicts with "trade every window" vision

**Option C: Sizing based on EE probability** (recommended alongside A)
- High EE probability → size up (more capital on high-confidence EE opportunities)
- Low EE probability → size down (reduce exposure on likely-settlement trades)
- Works with existing adaptive sizing framework
- **Aligns with northern star**: still trades every window, just bets bigger when EE is likely

**Recommended: A + C** — dynamic thresholds + EE-based sizing. Trade every window, but allocate capital intelligently.

### Training Approach

1. **Time-series split** (not random) — train on first 70% of windows, test on last 30%
2. **Model**: Start with LogisticRegression (interpretable, fast) and RandomForest (robust)
3. **Calibration**: Ensure predicted probabilities are well-calibrated (Platt scaling if needed)
4. **Evaluation metric**: AUC-ROC + calibration plot. We need accurate probabilities, not just good classification.
5. **Feature importance**: Use permutation importance to identify what actually predicts EE

### What Success Looks Like

| Metric | Current | Target | Impact |
|--------|---------|--------|--------|
| EE trigger rate | 33% | 45-50% | More trades exit profitably |
| Avg PnL/trade | $0.13 | $0.30-0.50 | 2-4x profit per trade |
| Daily PnL | ~$21 | $50-80 | Sustainable profitability |
| Worst hour avg | -$1.40 | > -$0.50 | No catastrophic hours |

## Parallel Track: Direction Model Retraining

The current model was trained on Nov 2025 - Feb 2026 data (119 days). Live for 9 days, True Model WR degraded from 53.9% backtest to 47.1%. This is likely concept drift.

**When**: After shadow tracking is stable and exit-probability model is designed
**How**: Run `python ml_pipeline.py` (downloads 120 days from Binance, ~25 min)
**Consider adding features**:
- `entry_price` (Polymarket implied probability)
- `spread` (liquidity/uncertainty)
- `regime_strength` (continuous, not just binary)

**Expected impact**: Recovering 2-3% WR (from 47% to 50%) would reduce settlement drag by ~$200/month. Not the main lever, but worth doing.

## Implementation Sequence

### Phase 1: Cleanup (DONE — 2026-03-09)
- [x] Fix shadow tracking bug (fv.funding_zscore → fv.funding_rate)
- [x] Wire EE thresholds to config
- [x] Move 35 scripts to scripts/
- [x] Update .env.example and CLAUDE.md
- [x] Deploy to VPS, verify shadow collecting

### Phase 2: Collect Data (IN PROGRESS — 2-4 days)
- [ ] Shadow tracking accumulates 500-1000 windows
- [ ] Monitor shadow data quality (check poll_count, null rates)
- [ ] Run gamma backfill periodically for new trades

### Phase 3: Build Exit-Probability Model (~3-5 days of work)
- [ ] Download shadow_windows data from VPS
- [ ] Construct labels (did max_bid >= threshold?)
- [ ] Exploratory analysis: what features correlate with EE?
- [ ] Train LogisticRegression + RandomForest
- [ ] Evaluate on time-series test split
- [ ] If good: integrate into live_trader.py (dynamic thresholds + sizing)
- [ ] Deploy and monitor

### Phase 4: Optimize & Scale (ongoing)
- [ ] Retrain direction model with fresh data
- [ ] Tune EE thresholds based on exit-probability model feedback
- [ ] Scale bet size as bankroll grows
- [ ] Consider multiple positions per window (if data supports it)

## Key Decisions Made

1. **No hour blocking** — user wants 24/7 participation, per-window intelligence instead
2. **EE is the profit engine** — all future work optimizes around EE, not directional accuracy
3. **Trade every window** — volume matters, EE converts even mediocre directional calls into profit
4. **Config is source of truth** — EE thresholds wired to config, not hardcoded
5. **Shadow tracking is foundation** — can't build exit ML without per-window bid data on ALL windows

## Files That Matter

| File | Role |
|------|------|
| `main.py:1059-1167` | Shadow book loop + save |
| `main.py:1752-1950` | Early exit monitoring loop |
| `strategy/live_trader.py:602-621` | `get_exit_threshold()` — the 4-tier logic |
| `strategy/live_trader.py:623-660` | `get_token_balance()` — CLOB balance query |
| `storage/db.py:133-162` | `shadow_windows` table schema |
| `storage/db.py:461-520` | `save_shadow_window()` method |
| `signals/ml_probability.py` | Current direction model |
| `ml_pipeline.py` | Direction model training pipeline |
| `config.py:82-88` | EE threshold config |
| `docs/plans/2026-03-09-cleanup-and-ee-northern-star.md` | Implementation plan (tasks) |

## How to Check Progress

```bash
# Shadow windows collected so far
ssh root@65.21.178.90 "sqlite3 /home/btcedge/BTC-tool/btc_edge.db 'SELECT COUNT(*) FROM shadow_windows'"

# Shadow data quality (should see polls >= 25 per window = good coverage)
ssh root@65.21.178.90 "sqlite3 /home/btcedge/BTC-tool/btc_edge.db 'SELECT AVG(poll_count), MIN(poll_count), MAX(poll_count) FROM shadow_windows'"

# Recent shadow windows (verify data looks sane)
ssh root@65.21.178.90 "sqlite3 /home/btcedge/BTC-tool/btc_edge.db 'SELECT market_slug, up_max_bid, down_max_bid, poll_count FROM shadow_windows ORDER BY id DESC LIMIT 5'"

# Current live performance
ssh root@65.21.178.90 "sqlite3 /home/btcedge/BTC-tool/btc_edge.db \"SELECT COUNT(*), ROUND(SUM(pnl),2) FROM live_trades WHERE outcome IS NOT NULL\""

# Download DB for analysis
scp root@65.21.178.90:/home/btcedge/BTC-tool/btc_edge.db .
```

When shadow_windows count reaches 500+, it's time for Phase 3.
