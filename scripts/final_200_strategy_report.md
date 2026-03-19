# The $200 Polymarket Playbook
**Date**: 2026-03-19
**Research**: 20+ agents, 27 trader profiles, 513K data points, 25+ strategies tested and adversarially attacked
**Goal**: Find profitable strategies for $200 capital. Even 1c/hour counts.

---

## STRATEGIES RANKED BY VIABILITY

### Tier 1: MOST PROMISING (act on within days)

#### 1. NBA Late-Game Locks (Zero Fees)
**The play**: When a team leads by 15+ with 3 minutes left, buy at 95-97c. Win $0.03-0.05 per $0.95-0.97 risked. Game is effectively decided.

**Why it works**:
- ZERO fees on NBA — breakeven is exactly at entry price
- Team up 15+ with 3 min: wins >99.5% of the time
- 5-15 NBA games per night, ~3-5 have blowout endings
- Capital locked only 5-10 minutes (until game ends)
- No prediction needed — outcome is already decided

**The math**: $100 bet at 97c → $3 profit in 5 minutes. 3 locks per night → $9/night → $270/month.

**Risks**: Bots compete for fills. Order book may be thin at 97c+ during live play. Need to OBSERVE before deploying.

**Infrastructure**: Manual initially (watch games, place bets). Automate later with live score feed.

**Next step**: Watch 3 NBA games tonight on Polymarket. Check if 97c+ asks are available during blowouts.

---

#### 2. Daily BTC Price Targets — Informed Directional Trading
**The play**: Trade "Bitcoin above $X" daily markets using our existing Binance data pipeline. Focus on the at-the-money strike 1-2 days before resolution.

**Why it could work**:
- Resolution uses **Binance noon candle** — WE ALREADY MONITOR THIS EXACT DATA
- No latency arms race (24-hour window vs 5-minute sprint)
- Fewer bots than 5-min markets (lower frequency = less automated competition)
- Our indicators (momentum, funding rate, order flow, regime detection) are MORE meaningful over 24 hours
- kingofcoinflips made $653K doing this at $118 avg bet

**Current opportunity**: BTC dropped 4.3% today (hawkish Fed). $70K strike priced at 45.5c. Market may be overreacting — mean reversion after 4%+ drops is historically common.

**The math**: $5 on YES $68K at 83c → win 17c per 83c = 20% return if BTC stays above $68K (it's at $69.4K, needs to drop only 2% more to lose). Need >83% probability to profit. Arguably it IS >83%.

**Fees**: 1.56% max at 50c, negligible at extremes. Better than 5-min because trades are daily, not every 5 minutes.

**Risks**: A single crash day (like today) can wipe a position. BTC volatility is 2.2% daily — moves of 3-5% happen regularly. Need position sizing discipline.

**Infrastructure**: Our existing Binance WebSocket + indicators + Polymarket API. Need new market slug discovery for daily targets. ~1 day of development.

**Next step**: Paper trade 10-20 positions over the next week on daily targets using our Binance data for direction.

---

#### 3. Tennis Comeback Betting (Zero Fees)
**The play**: When a strong player loses set 1, buy at 15-30c. Comeback rate is ~30% (ATP hard court). If market prices at 20c, that's 50% edge.

**Why it works**:
- ZERO fees — breakeven at exactly the entry price
- $5 bet at 20c → $25 payout if correct. Only need 20% WR to break even.
- 20-40 matches per day during major tournaments
- Manual trading viable (matches last hours)
- Live momentum: can EE-sell at 40-60c on break-back without waiting for match end

**The math**: 10 trades/week at $5, 30% WR, 20c avg entry:
- 3 wins × $20 profit + 7 losses × $5 = +$60 - $35 = +$25/week → $100/month

**Risks**: Need reliable comeback probability estimates by player. Market may already price correctly. Sample size for validation will take weeks.

**Infrastructure**: Free live scores (FlashScore), manual betting. Already researched in Tennis-pm project.

**Next step**: Paper trade 50 set-1-loser situations at Miami Open this week.

---

### Tier 2: WORTH INVESTIGATING (need more research/observation)

#### 4. NBA Halftime Edge (Zero Fees)
Teams up 10+ at halftime win ~80%. If Polymarket prices below 80c, there's an edge. Zero fees mean even 1-2% mispricing is profitable. But the market may already price this correctly. Need to observe live to verify.

#### 5. Copy-Trading kingofcoinflips on Daily Markets
They're still active (traded today). On daily markets, 10-second detection latency doesn't matter. Poll their trades, follow on same strikes. Tools already exist (Polycule, Bravado). Risk: they might stop trading or use decoy wallets.

#### 6. Sports Market Making on 1H Markets
NBA first-half markets have 6-7c spreads (vs 1c on moneylines). Place bids and asks to earn spread with zero fees. Resolve at halftime (1.25 hours). Risk: adverse selection, thin volume ($47 traded on some).

---

### Tier 3: FREE OPTIONS (no capital needed)

#### 7. POLY Token Airdrop
Confirmed for 2026. Our 1,684-trade history likely qualifies. Keep account minimally active. Estimated value: $10-100.

#### 8. Referral Program
$10 per referred user who deposits $20+. No capital needed.

---

### DEAD (killed by adversarial testing)

| # | Strategy | How it died |
|---|----------|-------------|
| 1 | BTC 5-min ML prediction | 47% WR, worse than coin flip |
| 2 | Stop-losses on 5-min | Kills EE, the only profitable mechanism |
| 3 | Late-window bail | +$46 real improvement, not +$356 (phantom EE bug) |
| 4 | Contrarian/buy cheap | Look-ahead bias artifact |
| 5 | Favorite scalping (Sharky) | Worst among 27 top traders |
| 6 | Market making on crypto | Spread < fees at fair prices |
| 7 | Cheap lottery tickets | 10c asks don't exist early enough |
| 8 | Multi-asset at current WR | 4x settlement bleeding |
| 9 | Hour-of-day EE filter | Likely overfit (14 days, no structural basis confirmed) |
| 10 | Copy-trading on 5-min | 3-10s latency, adversarial countermeasures |
| 11 | Cross-market arb | 2.7s windows, bots capture 73% |
| 12 | Near-certain outcomes (99c) | Pennies in front of steamroller |
| 13 | Liquidity rewards | Need $10K+ minimum |
| 14 | Binary option spreads | Not risk-free (range bet, not arb) |
| 15 | Lower EE thresholds | Every lower threshold performs worse |

---

## THE PLAN

### Week 1: Observation Only ($0 at risk)
- Watch 3-5 NBA games on Polymarket — check late-game lock availability
- Paper trade 10 daily BTC price target positions using Binance data
- Paper trade 10 tennis comeback situations at Miami Open
- Note: spreads, fill availability, price movement speed

### Week 2: Paper Trading ($0 at risk)
- If NBA locks are available: paper trade 20 locks
- If daily BTC targets show edge: paper trade 20 more positions
- Track all results meticulously

### Week 3-4: Micro-Deploy ($50 at risk)
- Deploy $50 on the strategy with best paper results
- $5 per bet, max 10 concurrent positions
- If profitable over 50+ trades: scale to $100

### Month 2: Scale ($100-200 at risk)
- Deploy full $200 on proven strategy
- If multiple strategies work: split capital
- Target: break even or slight profit. Don't chase returns.

### The Mindset
We lost $152 by rushing into a system we didn't validate. This time:
- Paper trade first. Always.
- Small bets ($5) until 50+ trades prove the edge.
- No ML models. No complex systems. Simple rules, manual execution.
- Zero-fee markets first (NBA, tennis). Crypto daily only with macro conviction.
- Compound slowly. $1/day = $365/year = 182% ROI on $200.

---

## WHAT OUR EXPERIENCE TAUGHT US

1. **The market is efficient for 5-min BTC direction prediction** — no simple rule or ML model beats it
2. **Fees kill thin edges** — 1.56% per trade means you need 53%+ accuracy just to break even
3. **Zero-fee markets change the math fundamentally** — 50% breakeven instead of 53%
4. **EE (bid spikes) is the only proven profitable mechanism on crypto** — +$1,928 on 585 trades
5. **Every backtest lies** — look-ahead bias, snapshot timing, phantom EE inflation. Only live results count.
6. **The winning traders don't predict better — they trade smarter**: cheaper entries, zero fees, speed edges, or structural advantages
7. **$200 is viable** if you pick the right market. $200 on BTC 5-min is suicide. $200 on zero-fee NBA locks is a real business.
