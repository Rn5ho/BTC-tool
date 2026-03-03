# Trading Logic Reference

## Data Flow

```
Binance WS (spot+futures) → Rolling State (candles, orderbook, trades, funding)
                              ↓
                        Feature Engineering → FeatureVector (12 fields)
                              ↓
                   ┌─── ML Model (44 features from candles) ──┐
                   │         OR                                │
                   └─── Rule-Based Ensemble (12 features) ─────┘
                              ↓
                         P(up) prediction
                              ↓                          ↓
                        Edge Detection          SQLite (log features)
                              ↓
Polymarket API → Implied P(up)  →  edge = our_P(side) - market_P(side)
    (4 parallel: 2 midpoints + 2 order books per cycle)
                              ↓
              ┌─── Always-Trade: trade every window ───┐
              │         OR                              │
              └─── Classic: only if edge > threshold ──┘
                              ↓
                   Safety Filters (time gate + hour blacklist + trend conflict + min confidence)
                              ↓
                   Adaptive Sizing (confidence × hour-of-day × streak × drawdown × rolling WR)
                              ↓
                        Paper Trader → simulate bet, log to SQLite (no Telegram)
                              ↓
                        Live Trader → real CLOB order → Telegram Alert
                              ↓
                  Early Exit Monitor → sell at bid >= $0.95 → Telegram Alert
```

## Edge Detection & Always-Trade Mode

The edge detector (`strategy/edge.py`) supports two modes:

### Always-Trade Mode (`ALWAYS_TRADE=true` — ACTIVE)
- ML model predicts P(up) every 5-minute window
- Direction chosen by model: P(up) > 0.5 → UP, else → DOWN
- Minimum confidence filter: skip windows where |P(up) - 0.5| < `MIN_CONFIDENCE` (default 0.015)
- Backtested: 0.015 threshold trades ~53% of windows at 56.9% WR ($59/day simulated)
- Edge vs market is computed for sizing (higher confidence = bigger bet)

### Classic Mode (`ALWAYS_TRADE=false`)
- Only trades when positive edge exceeds `MIN_EDGE_THRESHOLD` (default 5%)
- Picks side with largest positive edge
- Many windows are skipped (no trade)

### Safety Filters (both modes)
1. **Min confidence** (`MIN_CONFIDENCE=0.015`): Skip when |P(up) - 0.5| below threshold (always-trade mode)
2. **Max edge cap** (`MAX_EDGE_THRESHOLD=0.18`): Edges above 18% are rejected as model error (classic mode only; disabled in always-trade mode — edge size doesn't predict WR)
3. **Entry price filter**: Hard reject outside 0.25-0.65. Core range 0.35-0.65 uses normal sizing. **Exploration range 0.25-0.35**: traded at minimum size ($1 paper / $2.00 live) to collect WR data, tagged `trade_tag="exploration"` in DB. Live exploration uses $2.00 floor (not $3.50) since at entry prices 0.25-0.35, $2.00 buys 5+ tokens.
4. **Time gate** (`_MAX_ENTRY_SECONDS=120`): Only enter in first 2 minutes of 5-min window
5. **Hour blacklist** (`BLACKLIST_HOURS`): Skip configured UTC hours (default: 02:00)
6. **Trend-conflict filter** (`_TREND_CONFLICT_PCT=0.15`): Skip if BTC moved >0.15% against our signal direction within current window
7. **Signal saturation** (rule-based only): Skip when any signal near +-0.5 limits
8. **One trade per window**: No duplicate bets on same market slug
9. **Pause**: `/pause` command stops new trades while data collection continues

## Bet Sizing Strategies

Controlled by `SIZING_STRATEGY` in .env:

### Fixed (`sizing_strategy=fixed`)
Flat `BET_SIZE_USDC` every trade.

### Kelly (`sizing_strategy=kelly`)
Half-Kelly criterion: `f* = (p*b - q) / b`, capped at 5% of bankroll.

### Hybrid Adaptive (`sizing_strategy=adaptive` — ACTIVE)
Dynamic sizing based on multiple factors:
- **Base**: 2% of bankroll
- **Confidence multiplier** (0.5x-2.0x): Scales with `|P(up) - 0.5|`
- **Hour-of-day multiplier**: Strong hours (09,14,20) get 1.5x, good hours (6,8,10,12,16,18,22) get 1.2x, weak hours (4,7) get 0.7x, rest 1.0x
- **Streak multiplier**: 0.75x after 3 consecutive losses, 0.5x after 5
- **Drawdown multiplier**: 0.75x if drawdown >15%, 0.5x if >25%
- **Rolling WR multiplier**: 1.3x if last 20 trades >55% WR, 0.7x if <45%
- **Final size**: Clamped to [0.5%, 8%] of bankroll

### Monte Carlo Validation (1000 runs, $20 start)
All strategies showed **0% bust rate** across 1000 simulated runs:
- **Quarter Kelly**: median min $18.99, worst-case min $11.96
- **Hybrid Adaptive**: median min $18.73, worst-case min $12.66
- **Fixed 2%**: median min $17.66, worst-case min $5.04

## Paper Trading & Fee Model

Paper trading engine (`strategy/paper_trader.py`) simulates Polymarket's taker fee structure:

- **Entry price**: Uses best ask (taker price) from order book, not midpoint. More realistic PnL that matches what real takers pay. Falls back to midpoint if book unavailable.
- **Spread tracking**: Every trade records `entry_spread` (bid-ask spread) and `midpoint_price` for post-hoc analysis of spread impact.
- **Fee formula**: `fee_factor = fee_rate * (price * (1 - price))^fee_exponent`
- **Default**: fee_rate=0.25, exponent=2 → ~1.56% effective fee at midprice
- **Shares**: `(size_usdc / price) * (1 - fee_factor)`
- **PnL**: WIN = `shares - size_usdc`, LOSS = `-size_usdc`
- **Bankroll persistence**: On restart, bankroll = initial + cumulative historical PnL from DB
- **Analysis query**: `SELECT AVG(entry_price - midpoint_price), AVG(entry_spread) FROM paper_trades` to measure spread impact

## Window Lifecycle & Settlement

1. **Window detection**: Slugs are deterministic (`btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`). Analysis loop detects transitions every 3-second cycle.
2. **Two-tier settlement** (live trades):
   - **Real-time** (at window transition): Waits 5s, queries Gamma API for actual Polymarket resolution. Gamma is typically too slow for freshly-resolved 5-min markets (~35s insufficient), so falls back to Chainlink/Binance price comparison.
   - **Catchup** (startup + every 30 min): `_settle_stale_live_trades()` uses Gamma API with single retry — works reliably for older trades. Catches any mismatches from real-time settlement.
   - Paper trades always use Chainlink/Binance price comparison (best effort).
3. **Gamma API**: `GET https://gamma-api.polymarket.com/events?slug={slug}` → `outcomePrices: ["1","0"]` = UP won, `["0","1"]` = DOWN won. Checks `market.closed == true` before trusting.
4. **Flat close = DOWN**: Polymarket resolves flat closes (close == open) as DOWN — price didn't go UP. Uses strict `>` comparison (not `>=`).
5. **Settlement decoupled from market discovery**: Window transitions detected via `get_current_slug()` BEFORE the Gamma API call.
6. **Startup recovery**: Stale unsettled trades settled via Gamma API first, candle data fallback. Also runs periodically (every 30 min).
7. **Late BTC start**: If `_window_btc_start` is None after restart (streams not connected yet), retries on next analysis cycle once prices are available.

## Startup Sequence

1. Load config, check for ML model availability
2. Initialize ML model (or fall back to rule-based)
3. Initialize EdgeDetector with always_trade setting
4. Initialize DB, PaperTrader (with sizing_strategy), TelegramAlerter
5. Start Polymarket aiohttp session
6. Initialize LiveTrader (if LIVE_TRADING=true): derive API creds, patch rounding config (with Decimal), sync bankroll from real CLOB balance
7. Prefetch 35 candles from Binance REST API (skip 30-min buffering wait)
8. Launch 6 concurrent asyncio tasks:
   - **Binance WS**: Streams kline_1m, depth20, aggTrade, futures funding
   - **Chainlink RTDS**: Streams BTC/USD from Polymarket's data service
   - **Analysis loop**: Initializes window tracking, settles stale trades (paper + live), runs 3-second poll cycle, sends skip notifications via Telegram
   - **Early exit loop**: Dedicated 1-second poll cycle monitoring bid prices for early exit selling (bid >= $0.95)
   - **Stats loop**: Periodic stats report + CLOB bankroll sync + stale trade settlement + maker fill sync every 30 minutes
   - **Telegram command listener**: Long-polls for incoming /commands
