# Comprehensive Polymarket Strategy Analysis
**Date**: 2026-03-19
**Research streams**: 6 parallel agents, 27+ trader profiles, 513K snapshots, 45K candles
**Goal**: Find a viable strategy for $100-200 capital

---

## METHODOLOGY WARNINGS

### Critical: Look-Ahead Bias in All Prior Backtests
The adversarial agent discovered that `btc_price` in market_snapshots arrives 13-285 seconds into each window, NOT at true boundaries. This **systematically inflated cheap-side win rates by 4.6 percentage points**. Any backtest using snapshot prices for winner determination is unreliable. The only trustworthy source is **Gamma API resolution data** (1,655 verified trades).

### Snapshot Timing
Snapshots are **3 seconds apart** (not 1 second). "Snapshot 40" = 133 seconds into window, not 40 seconds.

---

## CONFIRMED DEAD STRATEGIES

| # | Strategy | Why It's Dead | Evidence |
|---|----------|---------------|----------|
| 1 | **Contrarian (buy cheap side)** | Look-ahead bias artifact. Real WR 38.9% vs 40.4% breakeven = -1.5pp | Gamma-verified: -$250 PnL |
| 2 | **Favorite scalping (Sharky)** | Worst among all 27 top traders ($93K vs $4M #1). R:R 1:30-1:75 | Every threshold bankrupt at $100-200 |
| 3 | **Market making / spread capture** | Median spread $0.01, fee breakeven $0.031 at 50c. Spread < fees | 451K snapshots analyzed |
| 4 | **ML direction prediction** | 53% backtest -> 47% live. Model doesn't generalize | 1,684 live trades, -$152 |
| 5 | **Hold to settlement at fair prices** | Zero profitable small traders do this | 10 mid-tier traders analyzed |
| 6 | **Hour-of-day bias** | 52.2% WR but degrades 8x in second half (overfit). Only 20 days data | p=0.001 but unstable |
| 7 | **Consecutive window patterns** | All negative EV after fees. 50.9% alternation rate = random | z-scores all < 1.5 |
| 8 | **Volatility regime trading** | Cheap side loses in ALL regimes | Low/med/high vol all tested |
| 9 | **Day-of-week patterns** | No day reaches significance | Largest effect: Sunday 52% DOWN |
| 10 | **BTC price level patterns** | No difference at $60K vs $80K | Favorite WR ~57% regardless |

---

## STRATEGIES STILL ALIVE

### 1. EE Scalping (Buy 40-55c, Sell on Bid Spike)
**Status**: VALIDATED by real profitable traders

**Evidence**:
- treyway3: $3.9K profit, 48% sell rate, buys at 0.49-0.59, sells at 0.94-0.99
- no-hesi: $3.3K profit, 45% sell rate, same pattern
- Yanou35: $3.4K profit, 12% sell rate (more selective exits)
- Our own system: EE generated **+$1,928** on 585 trades (avg +$3.30)

**Why it works**: Doesn't depend on settlement accuracy. Profits come from intra-window bid spikes, not from predicting final direction. The bid spike is a real market microstructure phenomenon.

**For $200 capital**: Our system already does this. The problem was settlement losses (-$2,806 on 755 losses) overwhelmed EE gains. Fix: **never hold to settlement** — sell before window ends regardless, or only trade when EE probability is high.

**Key question**: Can we build a pure-EE strategy that exits ALL trades before settlement?

---

### 2. Cheap Lottery Tickets ($0.08-$0.15 entries)
**Status**: VALIDATED but NEEDS re-testing with Gamma data

**Evidence**:
- ProfessionalBet: $5.8K profit, 20% PnL/Volume ratio
- Buys at $0.08-$0.15, holds to settlement
- Each trade costs $0.40-$0.80 — well within $200 capital
- Winners pay 5-10x, losers cost pennies

**WARNING**: The adversarial agent proved that cheap-side win rates are inflated by snapshot look-ahead bias. ProfessionalBet's profitability needs verification against Gamma resolution data. At $0.10 entry, breakeven is 10% WR. If actual WR is 8-9% (after bias correction), the strategy loses.

**For $200 capital**: Each trade costs ~$0.50. Could make 400 trades. But need verified win rate first.

---

### 3. Daily "Bitcoin above $X" Price Target Markets
**Status**: PROMISING — different market structure, unexplored

**Evidence**:
- kingofcoinflips: $101K profit at $118 avg bet, 5.4 trades/hr
- Buys YES on achievable targets and NO on stretch targets
- 11 strike levels per day, noon ET resolution
- $8K-$150K volume per strike

**Why it might work**:
- 24 hours of price action is fundamentally more predictable than 5 minutes
- Multiple strikes let you calibrate confidence
- Technical analysis (support/resistance, momentum) is more meaningful over 24h
- Existing Binance data infrastructure can support this

**For $200 capital**: $5 bets on daily markets. Fewer trades per day but higher conviction.

**Key risk**: Still need directional accuracy. If our signals can't beat 50% on 5-min, can they beat 50% on daily? Different question — daily patterns may have different dynamics.

---

### 4. Multi-Asset 5-min Expansion
**Status**: UNIVERSAL among crypto HF winners, but edge source unclear

**Evidence**:
- Every top crypto trader trades 2-7 assets (BTC + ETH + SOL + XRP + DOGE + BNB + HYPE)
- 7 coins x 5-min markets available on Polymarket
- Correlated moves = same directional call across assets

**For $200 capital**: Spread $200 across 4 assets, $5 per trade. 4x trade volume.

**Key risk**: If BTC 5-min has no edge, why would ETH/SOL/XRP be different? Unless the edge is in cross-asset latency (BTC moves first, others lag) — which requires speed infrastructure we don't have.

---

## THE HONEST ASSESSMENT

### What the data says about $200 on Polymarket:

1. **The BTC 5-min market is efficiently priced.** No rule-based strategy generates consistent positive EV after fees. Confirmed by 6 independent analyses across 513K data points.

2. **The only reliable profit source is EE (early exit bid spikes).** Our system made +$1,928 on EE trades while losing -$2,806 on settlements. The 3 profitable small traders all use EE aggressively.

3. **The minimum CLOB order ($3.50) forces 1.75-5% position sizing at $200 capital.** This is too large for thin edges — variance kills before compounding.

4. **Every "edge" found in backtesting was either overfit or an artifact.** The contrarian strategy, hour-of-day patterns, volatility regimes — all failed validation.

### Three genuinely viable paths forward:

**Path A: Pure EE (highest confidence)**
- Buy at 40-55c, monitor for bid spikes, sell EVERY trade before settlement
- Never hold to settlement — eliminate the -$2,806 loss pool entirely
- Even at 50% spike rate with +$3 avg EE profit and -$2 avg non-spike loss, this is +EV
- Needs: sell mechanism for non-spike trades (sell at market 30s before settlement)
- Capital: $200 is viable if you can manage the sell-side execution

**Path B: Daily Price Targets (most unexplored)**
- Trade "Bitcoin above $X" daily markets with strike selection based on technical levels
- Fewer trades, higher conviction, longer time horizon for signals to matter
- Capital: $200 viable at $5-10 per trade
- Needs: new market discovery code, daily resolution tracking

**Path C: Cheap Lottery + EE Hybrid**
- Buy extreme cheap side ($0.08-$0.15) with tiny bets ($0.50-$1.00)
- If bid spikes, EE sell. If not, hold to settlement (low cost, high payout if right)
- Capital: $200 gives 200-400 trades
- Needs: verified cheap-side win rate from Gamma data

---

## WHAT OUR DATA IS ACTUALLY WORTH

Despite all the dead strategies, we've built something valuable:
- **513K market snapshots** proving BTC 5-min market efficiency
- **1,655 Gamma-verified trades** — the ONLY reliable ground truth
- **EE profit data**: +$1,928 on 585 EE trades proving bid spikes are real and exploitable
- **Infrastructure**: Binance WS, Polymarket API, Telegram bot, SQLite persistence
- **Fee model**: Precise understanding of Polymarket's fee curve
- **Order book dynamics**: Spread patterns, depth analysis, adverse selection quantification
- **The adversarial finding**: look-ahead bias detection methodology applicable to ANY future backtest

This data eliminates false paths faster than any competitor could. The next strategy we try starts from a position of knowledge, not hope.
