# Deep Analysis Report — BTC Polymarket 5-Minute Edge Finder

**Date:** 2026-03-10
**Scope:** 8 parallel research investigations covering 1,439+ settled live trades over 9.8 days ($116 deposited, ~$302 balance)
**Purpose:** Identify highest-impact improvements, validate or reject the current strategy, and chart the next phase of development

---

## 1. Executive Summary

This system is not a directional prediction engine — it is a short-term volatility harvester that uses Polymarket's 5-minute BTC binary markets as the vehicle. All $186 of net profit comes from Early Exit ($+1,541) offsetting catastrophic settlement losses ($-1,355). The direction model is worse than a coin flip at 47.1% True WR. The single highest-potential change identified is a **Dual Position strategy** (buying both UP and DOWN simultaneously), which eliminates the need for directional accuracy entirely and projects $116-198/day in conservative estimates — 5-9x the current $22/day. Quick wins available immediately include entry price filtering by trade type (+$127 recovered), raising minimum confidence from 1.5% to 2.0% (-88 losing trades), and volatility-based bet sizing (+$15-20/day). The exit-probability ML model previously planned as Phase 3 is unlikely to work — entry-time features have near-zero predictive power over bid spike height (R-squared = 0.004). The real opportunity lies in intra-window dynamic decisions using real-time bid trajectory data.

---

## 2. The Big Picture

### What This System Actually Is

This is a **volatility arbitrage strategy** that monetizes intra-window price swings in Polymarket's binary option books. It is not, in any meaningful sense, a directional BTC prediction system. The evidence:

- The direction model has 47.1% True WR (below random after accounting for the 6.2% structural DOWN advantage from flat-close rules)
- Regime flip trades have a 10.9% settlement WR — essentially random noise — yet produce +$125 in profit, entirely through EE
- 86% of Early Exit trades are "regrets" (model had the right direction), meaning EE is mostly clipping winners, not rescuing losers
- max(UP_bid) + max(DOWN_bid) averages 1.55 across all windows — both sides spike above their entry cost in most windows, regardless of settlement direction

### What Actually Drives Profit

Three things generate money in this system, in order of importance:

1. **Intra-window bid volatility** — 72% of windows have UP bid range >= $0.50. Bids swing wildly within 5 minutes even when the final settlement outcome is a coin flip. This is the raw material.

2. **Early Exit timing** — The system buys tokens at the ask (typically $0.40-0.55) and sells when the bid spikes above a threshold. Median EE exit at 221 seconds (3m41s). 28.6% exit in the last 30 seconds. The threshold system converts raw volatility into realized profit.

3. **Volume** — Trading every window matters. At +$0.13/trade average, the system needs 170+ trades/day to hit $22. Anything that reduces trade count (hour blocking, aggressive filtering) directly reduces revenue unless the per-trade improvement is large enough to compensate.

What does NOT meaningfully drive profit:
- Directional accuracy (47.1% WR, below random)
- Feature engineering for direction (volume z-score, the "strongest" training feature, has zero live predictive value for EE)
- Entry price optimization within the current 0.25-0.65 range (matters somewhat but is secondary)

---

## 3. Critical Findings

### Finding #1: Direction Prediction and Spike Prediction Are the Same Problem

This is the most important finding from the entire analysis and it constrains all future work.

When the model picks the correct side, max_bid averages **0.91**. When wrong, **0.62**. This is near-perfect correlation — the bid spikes high precisely when our directional call was right, and stays low when it was wrong.

**Implication:** There is no shortcut to better EE by predicting spikes independently of direction. A spike predictor IS a direction predictor. The Northern Star plan's assumption that "EE probability" and "directional accuracy" are independent optimization axes is partially wrong. They are the same axis viewed from two angles.

**Nuance:** This doesn't mean EE is useless — EE still provides massive value by converting winning predictions into early profit (avoiding settlement reversal risk) and occasionally rescuing bad calls. But it means we cannot dramatically improve EE rates without improving directional accuracy, or fundamentally changing the strategy (see Finding #2).

### Finding #2: Dual Position Strategy Has 5-9x Profit Potential

If we buy BOTH UP and DOWN tokens simultaneously, we don't need directional accuracy at all. The data shows:

- max(UP_bid) + max(DOWN_bid) averages **1.55** across all windows
- At a 0.70 exit threshold: 50% of windows see BOTH sides spike above threshold, 49% see one side spike, 1% neither
- Entry cost for both sides: approximately $1.00 total (UP ask + DOWN ask, often close to the binary complement)
- Conservative stress-test projection: $0.40/window = **$116/day**
- Central estimate: **$150-198/day**

This is the single largest opportunity identified in this analysis, by a wide margin. Details in Section 4.

### Finding #3: Entry Price Filtering by Trade Type Recovers $127

Normal trades and regime flip trades have opposite optimal entry ranges:

| Trade Type | Profitable Range | Losing Range | Losing Trades | Lost $ |
|-----------|-----------------|-------------|--------------|--------|
| Normal | Entry >= 0.50 | Entry < 0.50 | 214 | -$66 |
| Regime flip | Entry < 0.55 | Entry >= 0.55 | 64 | -$55 |

Applying "normal trades only at entry >= 0.50, regime flips only at entry < 0.55" would have eliminated 308 losing trades and recovered approximately **$127** over the 9.8-day period — roughly doubling per-trade PnL from $0.14 to $0.28.

**Caveat:** These are in-sample numbers. The true out-of-sample improvement will be smaller. The directional pattern is strong enough that even a 50% regression would be worth implementing.

### Finding #4: The Exit-Probability Model (as Planned) Won't Work

GradientBoosting regression on entry-time features predicting max_bid: **R-squared = 0.004** (no signal). Classification (max_bid >= 0.70): **65.8% accuracy vs 68% naive baseline** (worse than always predicting positive).

Entry-time features simply do not contain enough information to predict what will happen to bid prices over the next 5 minutes. This makes sense — a 5-minute BTC move is essentially a random walk, and the features that would predict it are not observable at time zero.

**What does work:** Intra-window real-time signals. Bid at 60 seconds is a strong classifier (78.4% WR when bid > 0.70 vs 35.7% when < 0.35). First-90-second bid slope > 10 cents predicts 80.4% WR. These are runtime signals, not entry-time predictions.

### Finding #5: The Current 4-Tier EE System Is Surprisingly Effective

Against all tested alternatives, the existing tiered system performs well:

| System | PnL (same dataset) |
|--------|-------------------|
| Current 4-tier | **$245** |
| Best static threshold (0.95) | $62 |
| Flat 0.90 threshold | $329 |

The flat 0.90 threshold beats the 4-tier system by $84. However, this is heavily driven by the lower tiers (< 0.40 entry) clipping winners for small profit. Since those tiers have few trades and the system already routes them to paper-only, the gap in practice is smaller.

**Important finding within this:** A flat 0.90 threshold outperforms the tiered approach. The tiers' theoretical benefit (catch cheap-entry losers early) is outweighed by the cost of clipping winners. This suggests the mid-tier threshold should be raised, not lowered.

### Finding #6: Trade Outcomes Cluster (67.8% WR After a Win)

Sequential trades are not independent. After a WIN, the next trade has 67.8% effective WR. After a LOSS, 45.0%. BTC direction itself shows only 49.7% persistence (essentially random), so this is NOT a BTC momentum signal. It is a **Polymarket microstructure signal** — book conditions (depth, spread, maker behavior) persist across consecutive 5-minute windows.

This is directly actionable: size up after wins, size down after losses. The current adaptive sizing partially captures this through streak tracking, but a more explicit win-momentum multiplier would help.

### Finding #7: Model Confidence Is Monotonically Useful

| Confidence Band | Avg PnL/Trade | Count |
|----------------|--------------|-------|
| 1.5-2.0% | -$0.16 | 88 |
| 2.0-3.0% | +$0.18 | ~150 |
| 3.0-5.0% | +$0.54 | ~200 |
| 5.0-8.0% | +$1.18 | ~120 |

Raising MIN_CONFIDENCE from 1.5% to 2.0% would cut 88 marginally losing trades with essentially no upside forgone. Simple, zero-risk improvement.

### Finding #8: A Single Market Maker Dominates

One entity controls nearly all liquidity with 0.994 cross-side correlation. Spreads stable at ~2.6 cents since March 1. No competition trend. This is both a risk (single point of failure) and an opportunity (predictable behavior).

FOK slippage is ~1 cent ($0.19/trade, $56 total), an acceptable cost for eliminating maker fill drag.

### Finding #9: Fee Drag Is Meaningful but Not the Bottleneck

$89.27 in fees over 9.8 days (30.4% of gross profit). Fee breakeven WR is 50.8% at entry 0.50. EE double-fees are negligible (2.4% of EE profit). FOK taker premium costs ~$2.35/day but eliminates worse maker fill losses.

The path to reducing fee drag as a percentage is increasing profit per trade, not reducing fee rates. If per-trade profit doubles (from $0.13 to $0.26), fee drag drops from 30% to 15% of gross profit.

### Finding #10: UP vs DOWN Are More Symmetric Than Expected

True WR: UP 54.4%, DOWN 54.2%. EE rates identical (~34%). Max bids identical (~0.78). The only structural asymmetry is the flat-close-equals-DOWN rule giving DOWN a 6.2% advantage (56.2% of settlements are DOWN).

There is a meaningful asymmetry in **regime flips**: flipping to UP earns +$136 vs +$11 for flipping to DOWN. This likely reflects the 9.8-day sample period rather than a persistent structural feature. Would need more data to confirm.

---

## 4. The Dual Position Opportunity

### Concept

Instead of using the direction model to pick one side, buy BOTH UP and DOWN tokens at the start of each 5-minute window. Monitor both positions for bid spikes. EE each independently when its threshold is hit.

### Why It Should Work

The core math: in any 5-minute window, BTC moves. If it goes up, the UP bid spikes. If it goes down, the DOWN bid spikes. In volatile windows, it may move both directions at different times during the 5 minutes, causing BOTH bids to spike.

Data:
- max(UP_bid) + max(DOWN_bid) averages **1.55** (substantially above the ~1.00 total entry cost)
- At 0.70 exit threshold: 50% of windows yield two EE triggers, 49% yield one, 1% yield none
- Even the worst-case windows (one side spikes, one doesn't) are approximately breakeven: the spiking side's EE profit covers the non-spiking side's loss

### Revenue Projection

**Conservative (stress test):** $0.40 net profit per window, $5 bet per side
- $0.40 x 288 windows/day = **$116/day**

**Central estimate:** $0.55 net profit per window
- $0.55 x 288 windows/day = **$158/day**

**Current system:** ~$22/day

### Risks and Unknowns

This is the most promising finding, but several serious risks need investigation before implementation:

1. **Execution risk:** Two simultaneous FOK orders per window doubles order volume. If one fills and the other is rejected (insufficient depth), you have a single directional position at a random entry price. Need to handle partial execution gracefully.

2. **Capital requirement:** $10/window ($5 per side) x potential multiple concurrent positions = need larger bankroll buffer. Current $302 balance may be tight.

3. **Market impact:** Doubling buy volume may move asks or attract market maker attention. At $5-10 per side on a market with 130-500 token depth, impact should be minimal, but needs monitoring.

4. **Fee doubling:** Two entries + two potential exits = up to 4 fee events per window. At 1.5% fee rate, this eats into the thinner per-side margins. Need to model fees carefully.

5. **The 1.55 max-sum may not be simultaneously achievable:** max(UP_bid) and max(DOWN_bid) typically occur at DIFFERENT times within the window. The UP bid might peak at t=90s while DOWN peaks at t=240s. The EE system handles this naturally (it monitors continuously), but the revenue projection assumes both can be captured, which requires that each spike lasts long enough for the 1-second polling loop to detect and execute.

6. **Sample size:** The 1.55 average comes from a relatively short history. Need to verify this holds across different market regimes.

7. **Correlation with existing strategy:** If we switch to dual-position, we lose the directional model entirely. If the dual strategy underperforms during certain regimes, we have no fallback.

### Recommended Approach

Do NOT switch entirely to dual-position immediately. Instead:

1. **Paper-trade dual positions** by adding shadow tracking for the opposite side of every live trade. The shadow data already captures `up_max_bid` and `down_max_bid` — use this to simulate dual-position P&L.

2. **Validate with 500+ windows** of simulated dual-position results. Confirm the $0.40-0.55/window net after fees.

3. **If validated**, implement as a new trading mode alongside the existing directional mode. Start with small bets ($3 per side) and scale up.

4. **Key metric to watch:** Per-window net after all fees and slippage. Must be consistently > $0.20 to be worth the added complexity and capital.

---

## 5. Quick Wins (Implementable Immediately)

These changes require minimal code modification and have clear data support.

### 5.1 Entry Price Filters by Trade Type

**Impact:** +$127 over 9.8 days (~$13/day)
**Effort:** ~30 minutes
**Risk:** Low (in-sample overfit possible, but directional signal is strong)

In `strategy/edge.py` or `main.py`, add:
- Normal trades: require entry_price >= 0.50 (route < 0.50 to paper)
- Regime flip trades: require entry_price < 0.55 (route >= 0.55 to paper)

Continue collecting data on filtered-out trades via paper to validate.

### 5.2 Raise MIN_CONFIDENCE to 0.020

**Impact:** Eliminate 88 losing trades (~+$14 over period, ~$1.40/day)
**Effort:** Config change only
**Risk:** Very low

The 1.5-2.0% confidence band averages -$0.16/trade. Cutting it costs virtually no upside.

### 5.3 Volatility-Based Bet Sizing

**Impact:** +$15-20/day estimated
**Effort:** ~2 hours
**Risk:** Low-medium (need to define "low volatility" threshold carefully)

Low-volume periods produce $1.02/trade vs high-volume $0.03/trade. Low BTC volatility windows: 73.7% WR vs 52.2% at high volatility.

Use ATR or volume z-score to scale bet size. When volume z-score < -0.5 (quiet market), bet 1.5-2x. When volume z-score > 1.0 (volatile), bet 0.5x. Thin books in quiet markets spike easier, which is the exact mechanism EE exploits.

### 5.4 Win-Momentum Sizing

**Impact:** Moderate (hard to estimate precisely)
**Effort:** ~1 hour
**Risk:** Low

After a WIN, size up 1.3-1.5x (67.8% WR on next trade). After a LOSS, size down 0.7x (45% WR on next trade). Piggyback on the microstructure persistence signal.

### 5.5 Polymarket Spread Filter

**Impact:** Avoid toxic windows (spread > 0.02 = 35% EE rate vs 56%)
**Effort:** ~30 minutes
**Risk:** Low (reduces volume slightly)

When the Polymarket spread exceeds 2 cents, either skip the window or route to paper-only. Wide spread indicates uncertain book conditions that suppress EE triggers.

---

## 6. Medium-Term Improvements (1-3 Weeks)

### 6.1 Intra-Window Dynamic Exit (Replaces Static Thresholds)

**Impact:** Potentially large — the only data-supported path to improving EE beyond the current system
**Effort:** 1-2 weeks
**Risk:** Medium (requires careful real-time logic)

Entry-time features cannot predict spikes (R-squared = 0.004), but real-time intra-window signals are highly predictive:

- Bid at 60 seconds: 78.4% WR when > 0.70
- First-90-second bid slope > 10 cents: 80.4% WR
- Low BTC volatility in first minute: 73.7% WR

Instead of a fixed threshold, implement a **dynamic exit threshold** that adjusts based on what is happening within the window:
- If bid rises to 0.60 within 60s, lower the exit threshold (momentum is strong, spike likely to continue)
- If bid stays below 0.40 after 120s, raise the exit threshold (only exit on a dramatic reversal)
- If BTC volatility is low and bid is climbing steadily, hold for a higher exit price

This is fundamentally different from the entry-time exit-probability model — it uses runtime information, not prediction.

### 6.2 Direction Model Retraining

**Impact:** Recovering 3-5% WR would reduce settlement drag by ~$200-400/month
**Effort:** Half a day (run ml_pipeline.py, evaluate, deploy)
**Risk:** Low (easy to A/B test against current model)

The current model was trained on Nov 2025 - Feb 2026 data. Live True WR has degraded from 53.9% to 47.1% — classic concept drift. Retraining on recent data should recover some accuracy.

Consider adding entry_price as a feature (it correlates with direction accuracy, r = 0.36 between entry price and WR) and Polymarket spread (liquidity signal).

### 6.3 Dual Position Paper Trading

**Impact:** Validates the 5-9x revenue projection before committing capital
**Effort:** 1 week to implement shadow dual-position tracking properly
**Risk:** None (paper only)

Use the existing shadow_windows infrastructure to simulate buying both sides. Track hypothetical P&L including fees, slippage, and execution timing. Run for 2+ weeks before going live.

### 6.4 Hour-Aware Threshold Lookup

**Impact:** +$5-10/day estimated
**Effort:** A few hours
**Risk:** Low

Average max_bid varies from 0.721 at H01 to 0.868 at H17. A static lookup table mapping hour to exit threshold captures this without needing an ML model. Higher thresholds during peak hours (let winners run higher), lower during quiet hours (take what you can get).

### 6.5 Selective Trade Skipping via Quality Classifier

**Impact:** Removing worst 10% of trades projects +$519 over the period
**Effort:** 1-2 weeks
**Risk:** Medium (model may overfit)

A binary classifier predicting P(profitable) trained on the 756 settled live trades plus 2,807 reconstructed windows from market_snapshots. Unlike the exit-probability model (which failed), this predicts the combined outcome (EE + settlement), which may have more signal since it incorporates both direction and spike probability.

**Important caveat:** The +$519 figure is the theoretical maximum from perfect hindsight. A realistic model will capture maybe 20-30% of that, yielding +$100-150 — still worthwhile but not transformative.

---

## 7. What Doesn't Work

### 7.1 Exit-Probability Model from Entry-Time Features

R-squared = 0.004. Classification accuracy below naive baseline. Entry-time features simply cannot predict 5-minute bid behavior. This was the core assumption of the Northern Star Phase 3 plan, and the data firmly rejects it.

**Do not build this model.** The time and effort would be wasted.

### 7.2 Auto-Sell Before Settlement

Average bids are flat at ~$0.48 throughout the window. There is no systematic pre-settlement bid pattern to exploit. Not viable.

### 7.3 Contrarian Strategy (Always Buy Cheap Side)

Identical $0.14/trade to the current system. No improvement. The market is well-calibrated — cheap tokens are cheap for a reason (low win probability).

### 7.4 Market Making

The 2.6-cent spread is barely above the 3-cent round-trip fee. Not viable without a fee advantage or significantly more sophisticated pricing.

### 7.5 Hour Blocking (for current system)

While certain hours lose money (H02: -$1.40/trade), the user explicitly does not want hour blocking. The data does suggest that hour-aware thresholds (Section 6.4) are a better approach — adjust behavior per hour rather than avoiding hours.

### 7.6 Scalping Intra-Window Oscillations

Theoretically $397/day, but requires sub-3-second execution and many rapid FOK orders per window. Execution risk is extreme. FOK rejection rates are already climbing (7 to 29/day). Not practical with current infrastructure.

### 7.7 Lowering Mid-Tier EE Threshold

The recent change lowering mid-tier (0.40-0.50 entry) from 0.90 to 0.70 was deployed on 2026-03-09. However, the analysis shows a flat 0.90 threshold outperforms the tiered system ($329 vs $226). Lower tiers clip winning trades for small profit. The mid-tier threshold should likely be raised back to 0.90 or even 0.95, not lowered.

**Specific data point:** The 4-tier system produces $245 while the best static threshold (0.95 flat) produces $62, suggesting the tiered system's advantage comes from the aggressive low-entry tiers, not from lowering mid-tier thresholds. But a flat 0.90 produces $329 — the tiers are net-negative compared to a uniform high threshold.

---

## 8. Data Quality Issues

### 8.1 Shadow Data Was Garbage Until Fix (RESOLVED)

238 of 260 shadow windows had `max_bid = 0.01` due to reading `bids[0]` (lowest bid) instead of `bids[-1]` (highest). Fixed 2026-03-09. Only 22 valid shadow windows exist as of the analysis date. All shadow-derived conclusions use live trade data instead.

### 8.2 Shadow `traded_side` Is Always NULL

The settlement code pops the trade from the pending dict before the shadow save runs. Shadow windows never record what side was traded. Not critical for dual-position simulation (which needs both sides), but blocks any analysis correlating shadow data with trade outcomes.

### 8.3 Shadow Polls Every 10 Seconds vs Live 1 Second

Shadow book monitoring runs on a 10-second polling interval. Live EE monitoring polls every 1 second. This means shadow `max_bid` values systematically underestimate the true maximum (they miss short-lived spikes between polls). The reported shadow spike rate (~50%) is a lower bound on the true rate.

### 8.4 FOK Rejections Are Climbing

7 rejections/day early on, rising to 29/day recently. This may indicate deteriorating book depth or increased competition for fills. If this trend continues, it will degrade execution quality for both the current and any future dual-position strategy.

### 8.5 Feature Importance Mismatch Between Training and Live

Volume z-score was the strongest feature during ML model training but has near-zero or negative relationship with EE outcomes in live data. This is a red flag for model validity — the feature the model weighs most heavily is not predictive in production. Likely contributor to the 53.9% -> 47.1% WR degradation.

### 8.6 Nine Days of Data

All findings are based on 1,439 trades over 9.8 days. Many sub-analyses (e.g., regime flip at entry >= 0.55: 64 trades) have small sample sizes. Results that depend on fewer than 100 trades should be treated as directional hypotheses, not confirmed findings.

### 8.7 Gamma Resolution Backfill Gaps

Not all EE trades have been classified as regret vs save. The 86% regret / 14% save split comes from backfilled data; recent trades may not have ground truth yet.

---

## 9. Open Questions

### 9.1 Is the 1.55 Max-Sum Stable?

The dual-position strategy depends on max(UP_bid) + max(DOWN_bid) consistently exceeding entry costs (~1.00). Is this stable across different BTC volatility regimes? During very low-volatility periods, both bids might hover near 0.50 with neither spiking. Need at least 4 weeks of data across different market conditions.

### 9.2 Why Do Trade Outcomes Cluster?

67.8% WR after a win is a strong and actionable signal, but we don't fully understand the mechanism. Is it market maker behavior? BTC microstructure? Polymarket-specific book dynamics? Understanding the cause would help predict when the pattern breaks down.

### 9.3 What Happens When the Market Maker Changes Behavior?

A single entity controls nearly all liquidity. If they tighten spreads, widen spreads, reduce depth, or change their quoting algorithm, the EE strategy could be significantly affected. We have no way to predict or hedge this risk.

### 9.4 Is the Direction Model Adding Value At All?

With 47.1% True WR and the finding that direction prediction and spike prediction are the same problem, it is unclear whether the model provides any value over random side selection. A controlled test (random side selection with same EE system) would answer this definitively. If random is equivalent, the path to dual-position becomes even more compelling.

### 9.5 Can Intra-Window Signals Be Captured Programmatically?

The bid-at-60-seconds signal (78.4% WR when > 0.70) is strong, but acting on it within the existing architecture is non-trivial. The system currently enters at window open and monitors for exit. A mid-window decision framework (enter late, adjust threshold, add to position) would be a different architecture.

### 9.6 What Is the Regime Flip Actually Doing?

Regime flips have 10.9% settlement WR but 58.3% effective WR. This means flips are almost always wrong on direction but frequently saved by EE. Is the flip mechanism just generating random positions that happen to have cheap entry prices (avg 0.487)? If so, any mechanism that generates positions at cheap entry prices would work equally well.

### 9.7 How Will Fee Structure Evolve?

At 30.4% of gross profit, fees are a significant drag. Polymarket could change fee parameters at any time. The current quadratic fee formula penalizes mid-price entries most — a shift toward extreme entry prices (very cheap or very expensive) would naturally reduce fee drag, but at the cost of other tradeoffs.

---

## 10. Recommended Roadmap

Prioritized by expected impact divided by effort and risk. Items within each tier can be parallelized.

### Tier 1: Immediate (This Week)

| # | Action | Expected Impact | Effort | Risk |
|---|--------|----------------|--------|------|
| 1 | Raise MIN_CONFIDENCE from 1.5% to 2.0% | +$1.40/day | Config change | Very low |
| 2 | Entry price filters by trade type (normal >= 0.50, flip < 0.55 for live; paper all) | +$13/day | 30 min code | Low |
| 3 | Polymarket spread filter (skip or paper-only when spread > 0.02) | +$3-5/day | 30 min code | Low |
| 4 | Revert mid-tier EE threshold from 0.70 back to 0.90 | Prevents winner clipping | Config change | Low |

**Combined Tier 1 impact estimate: +$15-20/day (from $22 to ~$40/day)**

### Tier 2: Next 1-2 Weeks

| # | Action | Expected Impact | Effort | Risk |
|---|--------|----------------|--------|------|
| 5 | Volatility-based bet sizing (quiet markets = bigger bets) | +$15-20/day | 2 hours | Low-medium |
| 6 | Win-momentum sizing (size up after wins) | +$5-10/day | 1 hour | Low |
| 7 | Begin dual-position paper simulation using shadow infrastructure | Data collection (no P&L yet) | 1 week | None |
| 8 | Retrain direction model on recent data | Recover 2-5% WR | Half day | Low |
| 9 | Hour-aware threshold lookup table | +$5-10/day | Few hours | Low |

**Combined Tier 2 impact estimate: +$25-40/day additional (from ~$40 to ~$70/day)**

### Tier 3: Weeks 3-4

| # | Action | Expected Impact | Effort | Risk |
|---|--------|----------------|--------|------|
| 10 | Intra-window dynamic exit thresholds (bid trajectory based) | +$10-30/day | 1-2 weeks | Medium |
| 11 | Evaluate dual-position paper results; go live if validated | 5-9x revenue if it works | 1 week integration | Medium |
| 12 | Trade quality classifier (skip worst 10% of windows) | +$5-15/day | 1-2 weeks | Medium |

### Tier 4: Month 2+

| # | Action | Expected Impact | Effort | Risk |
|---|--------|----------------|--------|------|
| 13 | Full dual-position strategy with independent EE per side | $100-200/day projected | Major rework | High |
| 14 | Scale bet sizes as bankroll grows and strategy stabilizes | Linear revenue scaling | Ongoing | Medium |
| 15 | Monitor for market maker behavior changes | Risk mitigation | Ongoing | N/A |

### What to Stop Doing

- **Do not build the entry-time exit-probability ML model.** The data conclusively shows R-squared = 0.004. This was the planned Phase 3 of the Northern Star — it should be replaced with intra-window dynamic exit (Tier 3, #10).
- **Do not lower EE thresholds for middle tiers.** The data shows flat 0.90 beats the tiered system. Lower thresholds clip winners.
- **Do not invest in hour blocking.** The user doesn't want it, and the data supports hour-aware thresholds as a better approach.
- **Do not wait for shadow data to accumulate before acting.** The 756 settled live trades plus 2,807 reconstructed windows from market_snapshots are sufficient for most analyses. Shadow data adds value only for dual-position validation.

---

## Appendix: Key Numbers at a Glance

| Metric | Value |
|--------|-------|
| Total trades (settled) | 1,439 |
| Days of data | 9.8 |
| Net PnL | +$186 |
| PnL from settlement | -$1,355 |
| PnL from EE | +$1,541 |
| Avg PnL/trade | +$0.13 |
| True Model WR | 47.1% |
| EE rate | ~33% |
| Avg EE profit | $3.22 |
| Daily PnL | ~$22 |
| Total fees | $89.27 (30.4% of gross) |
| max(UP_bid) + max(DOWN_bid) avg | 1.55 |
| Win-cluster WR (after win) | 67.8% |
| Loss-cluster WR (after loss) | 45.0% |
| FOK slippage per trade | ~$0.19 |
| Market maker spread | ~2.6 cents |
| DOWN structural advantage | 6.2% |

---

## 11. Critical Self-Review

This section catalogues the weaknesses, gaps, overconfidence, and missing risks in the analysis above. It should be read as a mandatory counterweight before acting on any recommendation.

### 11.1 Sample Size: Nearly Every Sub-Finding Is Underpowered

The report is built on 1,439 trades over 9.8 days. That is a dangerously small sample for the granularity of claims being made.

- **Entry price filtering by trade type** (Finding #3): The "normal trades lose at entry < 0.50" finding relies on 214 trades. The "regime flips lose at entry >= 0.55" finding relies on 64 trades. A standard binomial confidence interval at n=64 with p=0.50 is roughly +/-12%. The claimed directional pattern could easily be noise. The report acknowledges this in one sentence ("in-sample numbers") but then immediately says "even a 50% regression would be worth implementing." That is hand-waving away the core problem: with 64 observations, you cannot distinguish a real pattern from a random split with any confidence.

- **Win-momentum clustering** (Finding #6): 67.8% WR after a win sounds strong. But what is the confidence interval? At n=~500 (rough estimate of win-preceded trades), the 95% CI is approximately 63.5%-72.1%. The lower bound is still above 50%, so this is likely real. However, the mechanism is unexplained (Section 9.2 admits this), which means we have no basis for predicting when it will stop working. Acting on a pattern without understanding the cause is curve-fitting with extra steps.

- **Confidence band analysis** (Finding #7): The 1.5-2.0% band has only 88 trades averaging -$0.16/trade. At n=88 with high variance (individual trades range from -$5 to +$9), the standard error of the mean is likely >$0.30. The -$0.16 average is not statistically distinguishable from zero. Raising MIN_CONFIDENCE is presented as "zero-risk" but it is actually "zero-evidence-of-benefit."

- **Hour-aware thresholds** (Section 6.4): Average max_bid "varies from 0.721 at H01 to 0.868 at H17." With ~60 trades per hour (1,439/24), each hourly average has a standard error of roughly 0.02-0.03. The observed range of 0.15 is plausibly real but could easily shrink by 50-70% out of sample. A lookup table trained on this data is a textbook overfitting opportunity.

**Bottom line:** The report treats nearly all findings as confirmed and actionable. A more honest framing: Findings #1 (direction=spike), #4 (exit model fails), and #5 (tiered system is reasonable) rest on solid statistical ground. Everything else is a hypothesis that needs 3-5x more data to confirm.

### 11.2 The 1.55 Max-Sum: Multiple Layers of Unreliability

The Dual Position strategy (Finding #2, Section 4) is the report's centerpiece recommendation, projecting $116-198/day vs the current $22/day. This projection rests on the 1.55 average of max(UP_bid) + max(DOWN_bid). There are at least six problems the report underweights or ignores:

**A) Data provenance is questionable.** Section 8.1 admits that 238 of 260 shadow windows had garbage data (max_bid = 0.01). Only 22 valid shadow windows exist. So where does the 1.55 come from? It must be reconstructed from market_snapshots, which poll at varying intervals (the report's own Section 8.3 says shadow polls at 10s, and market_snapshots polling frequency "varies 62-97 snapshots per window"). The 1.55 is therefore a maximum computed from sparse, irregularly-sampled data — it is a *lower bound* on the true asynchronous max-sum, but also a figure whose reliability we cannot assess because we do not know how it was computed from which dataset.

**B) Asynchronous peaks are not capturable as described.** The report correctly notes (Risk #5) that UP and DOWN peaks occur at different times. But it then dismisses this concern by saying "the EE system handles this naturally (it monitors continuously)." In a dual-position setup, you need to successfully execute TWO FOK sell orders per window. With FOK rejection rates already climbing from 7/day to 29/day (Section 8.4), the probability of both sells executing successfully in a given window is significantly less than the probability of either one succeeding individually. If one side fails to sell, you hold it to settlement — and the settlement-only win rate is 38.2%. The projected revenue should be penalized by the FOK failure probability on each side, which the report does not model.

**C) The entry cost assumption of ~$1.00 total is wrong.** When you buy UP at the ask and DOWN at the ask simultaneously, the total entry cost is UP_ask + DOWN_ask. In a well-functioning binary market, the sum of asks should be slightly above $1.00 (the market maker's round-trip spread). At a 2.6-cent spread per side, the ask sum is approximately $1.00 + $0.026 + $0.026 = $1.052. Add fees on both entries: at price 0.50, fee_factor = 0.015625, so fees are approximately 0.0156 * $0.50 * 2 = $0.016. Total entry cost is ~$1.07, not "approximately $1.00." Over 288 windows/day, this $0.07 gap costs $20/day — nearly the entire current daily profit.

Additionally, buying both sides simultaneously will move the asks outward. Even at $5/side on a 130-500 token depth book, you are consuming 10-25+ tokens from each side. The market maker will widen the spread in response, increasing entry costs further. The report mentions this as a risk (Risk #3) but does not model it.

**D) Four fee events per window, not two.** The dual strategy generates up to 4 fee events: buy UP, buy DOWN, sell UP (EE), sell DOWN (EE). At 1.56% per event on ~$5 transactions, that is roughly $0.31 in fees per window. Over 288 windows, that is $89/day in fees alone. The report mentions fee doubling (Risk #4) but does not subtract it from the revenue projection. The "conservative" $0.40/window estimate may already be consumed by fees.

Let us reconstruct: Revenue = (max sum - entry cost), roughly $1.55 - $1.07 = $0.48 gross per window. Subtract 4x fees ($0.31) = $0.17 net. At 288 windows/day = $49/day. This is 2.2x the current $22/day — meaningful, but nowhere near the "5-9x" headline figure.

**E) Tail risk is unbounded.** In windows where neither side spikes (report says 1% of windows), you lose the full entry cost minus whatever you get at settlement for the winning side. In a dead-flat BTC window (zero movement), neither bid spikes, and you lose the spread + fees on both sides. Over 288 windows/day, even 1% dead windows = 2.88 windows = potential -$5-8/day drag. If the "1% dead windows" estimate is wrong by even 2-3x (which is plausible given the sample size), this drag becomes significant.

**F) The 1.55 figure may be inflated by the BTC volatility regime of the sample period.** 9.8 days in early March 2026 — what was BTC doing? If BTC was unusually volatile during this period, the max-sum would be inflated. The report does not benchmark the sample period's BTC realized volatility against longer-term averages. If BTC vol drops 30-40% (as it does routinely), the max-sum could drop well below 1.40, making dual-position unprofitable after fees.

### 11.3 Survivorship and Regime Bias

The entire $186 in profit was earned over 9.8 days. This is a single sample of a single market regime. Key concerns:

- **BTC volatility dependence**: The system profits from intra-window BTC price swings creating bid spikes. If BTC enters a low-volatility regime (which historically occurs for weeks at a time), EE trigger rates will collapse. The report never quantifies the minimum BTC volatility needed for the strategy to break even.

- **Market maker stability**: All liquidity comes from one entity (Finding #8). This entity could withdraw or change behavior at any time. The report correctly flags this as a risk but offers no mitigation or contingency plan. If the market maker widens spreads from 2.6 cents to 5 cents, or reduces depth from 130 to 50 tokens, the strategy may become unprofitable overnight.

- **Polymarket market structure**: The 5-minute BTC binary markets are a specific product that Polymarket can modify or discontinue. Fee parameters can change. Resolution rules can change. The analysis assumes a static market structure.

- **Selection bias in EE profit**: The $1,541 in EE profit counts every successful early exit. But EE is only possible when (a) the bid spikes above threshold AND (b) the FOK sell order executes. Trades where the bid briefly touched the threshold but the sell failed are counted as settlement outcomes, not EE failures. The true EE success-when-triggered rate is not reported.

### 11.4 Contradictions and Logical Gaps

**A) Finding #1 vs Finding #2 are in tension.** Finding #1 says "direction prediction and spike prediction are the same problem" — when the model picks the right side, max_bid averages 0.91; when wrong, 0.62. Finding #2 proposes buying both sides to avoid needing directional accuracy. But if spikes only happen on the correct side (0.91 vs 0.62), then the "wrong side" of a dual position will rarely spike high enough to EE. The max-sum of 1.55 is 0.91 + 0.62 — but you can only capture the 0.91 side via EE (above typical thresholds), while the 0.62 side stays below most exit thresholds. This means in practice, dual-position revenue comes almost entirely from the winning side, with the losing side as a dead cost. The dual strategy degenerates to: "buy both, exit the winner, lose on the loser" — which is just a different (and more expensive) version of "buy random, exit if it spikes."

**B) Finding #5 vs Section 7.7 contradict each other.** Finding #5 says the current 4-tier system produces $245, while a flat 0.90 threshold produces $329. Section 7.7 says lowering mid-tier was a mistake and recommends reverting to 0.90. But the 4-tier system was designed specifically for cheap entries (< 0.40) where the 0.90 threshold would almost never trigger — those tokens rarely spike to 0.90. The proper comparison is: what is the profit on trades with entry < 0.40 under each system? A flat 0.90 on a $0.30 entry means you never exit early, guaranteeing settlement outcomes (38% WR) on those trades. The aggregate comparison hides tier-specific effects.

**C) The "volume z-score is useless live" claim (Finding #8.5, Section 3) needs scrutiny.** The report says volume z-score was the strongest training feature but has "near-zero or negative relationship with EE outcomes in live data." But the model was trained to predict directional outcomes, not EE outcomes. These are different targets. A feature can be useful for direction but not for EE profitability if EE profitability is dominated by market microstructure rather than direction. This is not evidence the model is broken — it is evidence that direction and EE-profitability are different (which Finding #1 already established from another angle).

### 11.5 Missing Risk Analysis

**A) Correlation with crypto market drawdowns.** If BTC crashes 10%+, what happens? Bid-ask spreads typically blow out, depth evaporates, and FOK rejection rates spike. The strategy's worst-case scenario is not a slow bleed — it is a volatility event that simultaneously breaks the EE mechanism (no depth to sell into) while creating directional losses on all open positions. The report does not discuss this scenario.

**B) Regulatory and platform risk.** Polymarket has faced regulatory scrutiny. The 5-minute crypto markets specifically could be classified as unlicensed derivatives. A regulatory action could freeze funds, halt markets, or change rules with zero notice. The report is silent on this.

**C) Execution latency and timing.** The system runs on a Helsinki VPS. Polymarket's CLOB is likely hosted in US-East. Network latency of 100-200ms between Helsinki and US-East means that a bid spike lasting 2-3 seconds gives only 8-12 polling opportunities, not 2000-3000. If the spike is concurrent with high load (other participants also trying to sell), the actual fill probability per spike is lower than implied by 1-second polling.

**D) Adverse selection.** When the system sells at a high bid, who is buying? If the buyer is better-informed (they know the window will resolve in the seller's favor), the system is experiencing adverse selection: it is selling winners to smart money. The 86% regret rate on EE (model was right, exited early) is consistent with this — the system is systematically selling correct positions to buyers who know those positions are underpriced. This is not necessarily unprofitable (small certain gain vs large uncertain gain), but it means the EE margin will compress over time as the counterparty optimizes.

**E) Compounding errors in the roadmap.** The Tier 1-4 impact estimates are presented additively: Tier 1 = +$15-20/day, Tier 2 = +$25-40/day, totaling ~$70/day. But these estimates interact. Entry price filtering (Tier 1) reduces trade count, which reduces the base for volatility-based sizing (Tier 2). Win-momentum sizing (Tier 2) changes the outcome distribution, which changes the win-cluster effect. The estimates cannot simply be summed. The real combined impact is likely 50-70% of the sum of individual estimates.

### 11.6 Quick Win Estimates: Optimism Audit

| Quick Win | Claimed Impact | Reality Check |
|-----------|---------------|---------------|
| Entry price filters | +$13/day | In-sample on 278 trades (214+64). Out-of-sample regression of 50%+ is standard. More realistic: +$4-7/day. Also reduces trade count, lowering volume-dependent revenue. |
| MIN_CONFIDENCE 1.5% to 2.0% | +$1.40/day | Based on 88 trades with mean PnL not statistically different from zero. Could be +$1.40 or -$1.40. True impact: approximately $0 with wide error bars. |
| Volatility-based sizing | +$15-20/day | No backtest presented. "Low volume = $1.02/trade vs high volume $0.03" is a correlation, not a sizing simulation. Sizing up on quiet markets assumes the pattern is stable. Actual impact after sizing logic, caps, and interaction effects: unknowable without simulation. |
| Win-momentum sizing | +$5-10/day | Based on unexplained 67.8% WR clustering. If the mechanism is market-maker behavior, it could change when the MM adjusts. No simulation of actual sizing-up outcomes presented. |
| Spread filter | +$3-5/day | Most defensible estimate — wide spreads indicating poor conditions is well-grounded. But reduces volume, and volume matters. Net impact may be lower. |

**Total claimed: ~$37-48/day additional.** More realistic estimate after accounting for out-of-sample regression, interaction effects, and statistical noise: **$10-20/day additional**. Still meaningful, but roughly half the headline figure.

### 11.7 What the Report Should Have Included But Did Not

1. **Confidence intervals on all key metrics.** The report presents point estimates throughout. At n=1,439, the 95% CI on average PnL/trade ($0.13) is roughly $0.13 +/- $0.08 (assuming ~$1.50 std dev per trade). The true daily PnL could plausibly be anywhere from $7 to $37/day. This uncertainty should be stated.

2. **A proper backtest of the dual-position strategy.** The report has the market_snapshots data to simulate dual-position outcomes window by window, including realistic fees, slippage, and FOK failure rates. This was not done. Instead, the projection uses an average max-sum minus a vague entry cost, which is not a backtest.

3. **BTC realized volatility during the sample period vs historical norms.** Without this, we cannot assess whether the strategy's performance is regime-dependent.

4. **Breakdown of EE trigger-to-fill success rate.** How often does the bid reach the threshold but the FOK sell fails? This directly affects all EE-dependent projections.

5. **The distribution of per-trade PnL, not just the mean.** A $0.13 average with a tight distribution around zero is very different from a $0.13 average with a bimodal distribution at +$4 and -$5. The risk profile matters for sizing decisions.

6. **Correlation analysis between consecutive trade outcomes.** The win-clustering finding is presented as a binary (67.8% after win, 45% after loss). What about the autocorrelation at lag 2, 3, 5? If it decays rapidly, the sizing benefit is smaller than implied.

7. **Monte Carlo simulation of the recommended roadmap.** What is the probability of ruin (going to $0) under the proposed changes? What is the distribution of outcomes at 30/60/90 days? The report presents expected values without variance.

8. **Analysis of how the market maker's quoting algorithm responds to the system's trading patterns.** After 1,439 trades from the same wallet, the MM may have adapted. Are recent spreads, depths, or fill rates different from early ones?

### 11.8 Overall Assessment

The report's strongest contributions are: (1) the clear framing of the system as a volatility harvester rather than a directional predictor, (2) the conclusive rejection of the entry-time exit-probability model, and (3) the identification of win-clustering as a potentially exploitable signal.

Its weakest aspect is the overconfidence in the dual-position projections, which suffer from data quality issues, unmodeled fees and execution costs, and potential regime dependence. The "5-9x revenue" headline should be treated as an upper bound under favorable conditions, not a central estimate.

The quick wins are mostly reasonable in direction but overstated in magnitude. Implementing them incrementally with proper out-of-sample tracking (paper-only first, then small live bets) is the right approach. The roadmap's Tier 1 items are low-risk. Tier 2+ items should wait for more data and proper backtesting.

**Recommended confidence calibration:** Where the report says "will work," read "might work." Where it says "+$X/day," mentally halve the number. Where it says "conclusively shows," check if n > 200 for that specific sub-analysis.
