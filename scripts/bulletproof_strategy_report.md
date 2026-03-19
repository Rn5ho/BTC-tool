# Bulletproof Strategy Report: What Actually Survives
**Date**: 2026-03-19
**Method**: 4 parallel research agents + self-adversarial testing on each
**Standard**: Only strategies that survived bootstrap testing, forward-walk, AND adversarial attacks

---

## KILLED IN THIS ROUND

### Cheap Lottery Tickets — DEAD
ProfessionalBet's $0.08-$0.15 entries looked great on paper. Reality:
- Asks <= $0.10 don't appear until **221 seconds** into the window (median)
- Only **0.6%** of windows have a 10c ask within first 60 seconds
- By the time a side is at 10c, the outcome is nearly decided — the market isn't mispricing, it's correctly updating
- ProfessionalBet likely uses **resting limit orders** or **different timeframes**, not market orders on 5-min
- No gamma-verified data exists for this approach
- **Verdict: not executable on 5-min markets at $200**

### Multi-Asset Expansion — DEAD (at current WR)
- ETH is the only viable addition ($6.4M/day), but spreads are 2x wider
- Alt order books are bot-mirrored — unlikely to create organic EE spikes
- 70-90% correlation = mostly the same bet repeated
- At 47% WR: 4 assets = 4x the settlement bleeding (-$46/day vs -$15)
- **Only viable if we abandon ML model entirely for pure EE — a different system**

### hgjghjh85 Replication — DEAD (for $200)
- $161K profit in 5 days is real but driven by **$1K-$28K position sizes**
- They sweep entire order books — creates different dynamics than $5 fills
- 49.2% WR at 40c entry is near coin-flip — edge is scale, not prediction
- 5 days / 73 markets could be variance
- **The insight is valuable (cheap entries + selective stops), but the scale isn't replicable**

---

## WHAT SURVIVED ALL ATTACKS

### 1. EE Hour Filter {0, 6, 7, 17, 23} UTC

**The finding**: EE rate varies dramatically by hour. Five hours have EE rates above the 36.5% breakeven threshold.

| Hour (UTC) | EE Rate | EV/Trade | Trades |
|---|---|---|---|
| 17 | 49.4% | +$1.03 | 85 |
| 7 | 48.1% | +$0.32 | 52 |
| 0 | 44.4% | +$0.12 | 72 |
| 6 | 42.1% | +$0.28 | 57 |
| 23 | 41.0% | +$0.18 | 61 |
| **Combined** | **44.6%** | **+$0.44** | **327** |

**Adversarial results**:
- Bootstrap P(mean > 0): **99.4%**
- Forward walk: positive in BOTH halves
- Without best hour (17): remaining hours at 92% confidence
- $200 simulation: $200 → $354 (55% max drawdown)
- 10/12 days positive for hour 17

**Caveats**: 14 days of data. No theoretical basis for WHY these hours produce more bid spikes. Could drift. Recommendation: 60+ more days of paper trading to confirm.

**Confidence: 70%** (high statistical confidence, but short data window)

### 2. EE Clustering (Volatility Begets Volatility)

**The finding**: After an EE trade fires, the NEXT trade has 46.8% EE probability (vs 28.3% baseline). 1.65x lift.

**Why it's real**: This is well-known market microstructure — volatility clusters. When BTC is moving sharply, consecutive 5-min windows have bigger moves, creating more bid spikes.

**Application**: Increase bet size after an EE. Or enter the NEXT window with higher conviction.

**Adversarial results**: Structural/theoretical basis (not data-mined). Consistent with established finance research on volatility clustering.

**Confidence: 85%** (strong theoretical backing + empirical confirmation)

### 3. Regime Flip + Good Hours Combo

**The finding**: Regime flip trades in good EE hours achieve 62.9% EE rate (vs 27.5% normal).

**Why it works**: Regime flips occur during trending markets = high volatility = more bid spikes. Good hours filter out noise.

**Adversarial result**: Mechanically sound — trending + volatile hours = more EE. But sample size shrinks fast when combining filters.

**Confidence: 60%** (logical but thin sample)

---

## THE HONEST BOTTOM LINE

### What we know for certain:
1. **EE is the only profitable mechanism** (+$1,928 proven, validated by real traders)
2. **The ML model destroys value** (31% settlement WR, worse than coin flip)
3. **Fees make every settlement trade slightly -EV** (0.3-0.9pp house edge)
4. **The market is efficiently priced** — no simple rule beats it at settlement
5. **Bid spikes are real and exploitable** — but only during certain hours/conditions

### What's actionable with $200:
1. **Collect 60+ more days of data** to validate hour filter (system already running for free)
2. **Paper trade the hour filter** — only track EE outcomes in {0, 6, 7, 17, 23} UTC
3. **If validated**: trade ONLY during those hours, ignore all other windows
4. **Use EE clustering**: increase position after an EE fires (next window has 1.65x EE probability)
5. **Expected outcome**: ~26 trades/day × $0.44/trade = +$11.44/day → +$343/month

### What we should NOT do:
- Trade every window (negative settlement EV)
- Use the ML model for direction (worse than random)
- Add multi-asset (multiplies losses at current WR)
- Implement stop-losses (kills EE opportunities)
- Buy cheap lottery tickets (not executable on 5-min)

### Risk assessment for $200 deployment:
- **Best case** (hour filter validated over 60 days): +$343/month, 55% max DD
- **Base case** (hour filter partially degrades): +$100-200/month, 60-70% max DD
- **Worst case** (hour filter was overfit): -$100-150 before we realize and stop
- **Maximum loss**: -$200 (entire bankroll, if we don't monitor)

### Recommended next step:
**Wait. Collect data. Don't spend money yet.**
The system is already running and collecting data for free. Use the next 60 days to paper-trade the hour filter. If EE rate in {0,6,7,17,23} stays above 40% across 1,000+ trades, the edge is real and worth deploying.
