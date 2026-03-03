# Database Schema Analysis

## Summary

The BTC-tool database (`btc_edge.db`) contains **330 settled live trades** across 1.5 days of data collection with **74,703 market snapshots** covering 807 unique markets.

## Table: `live_trades`

### Schema (18 columns)

| Column | Type | Nullable | Purpose |
|--------|------|----------|---------|
| `id` | INTEGER | No | Primary key |
| `timestamp` | INTEGER | No | Trade entry time (milliseconds UTC) |
| `market_slug` | TEXT | No | Polymarket slug (e.g., `btc-updown-5m-1772364900`) |
| `side` | TEXT | No | UP or DOWN |
| `token_id` | TEXT | No | Binary token contract address (numeric string) |
| `amount_usdc` | REAL | No | Bet size in USDC |
| `order_id` | TEXT | Yes | CLOB order ID (hex, may be NULL for some orders) |
| `status` | TEXT | No | Order status (e.g., `filled`, `failed`) |
| `success` | INTEGER | No | 1 if order placed successfully, 0 otherwise |
| `response_json` | TEXT | Yes | Full CLOB API response (JSON string) |
| `created_at` | TEXT | Yes | ISO 8601 creation timestamp |
| `outcome` | TEXT | Yes | Settlement result: `WIN`, `LOSS`, `EARLY_EXIT`, or NULL (unsettled) |
| `pnl` | REAL | Yes | Profit/loss in USDC (after fees, includes early exit premiums) |
| `entry_price` | REAL | Yes | Entry price when order was placed |
| `settled_at` | INTEGER | Yes | Settlement time (milliseconds UTC) |
| `trade_tag` | TEXT | Yes | Classification: `exploration` (0.25-0.35), `maker_fill`, or NULL |
| `regime_state` | TEXT | Yes | Market regime: `trending_up`, `trending_down`, `ranging`, or NULL |
| `regime_strength` | REAL | Yes | Regime confidence (0-1), NULL for most trades |

### Data Availability

- **Total trades**: 331 (including 1 unsettled)
- **Settled trades**: 330
  - **WIN**: 144 (43.6%) | +$350.42 | avg $2.43
  - **LOSS**: 154 (46.7%) | -$462.39 | avg -$3.00
  - **EARLY_EXIT**: 32 (9.7%) | +$83.82 | avg $2.62
  - **Unsettled**: 1

### Regime Data Coverage

- **With regime_state**: 37 trades (11.2%)
- **With regime_strength**: 37 trades (11.2%)
- Most trades (88.8%) have NULL regime fields — added recently, not backfilled

### Trade Tags Distribution

| Tag | Count | Purpose |
|-----|-------|---------|
| NULL | 234 | Normal trades (70.8%) |
| `maker_fill` | 89 | Orders filled as maker (passive) (26.9%) |
| `exploration` | 8 | Low-confidence exploration (0.25-0.35 price range) (2.4%) |

### Entry Price Distribution

Entry prices cluster around **$0.50-0.65** (implied odds range). Notable patterns:

- **Low prices (0.25-0.35)**: exploration trades, mixed results (avg WR 33%)
- **Mid prices (0.40-0.55)**: 50% of all trades, 43% WR
- **High prices (0.56-0.65)**: favorable odds, 55% WR
- **Outliers**: 2 trades at $0.94-0.97 (both winners)

### Data Collection Period

- **First trade**: 1772322604390 ms (2026-03-01 ~06:30 UTC)
- **Last trade**: 1772450434090 ms (2026-03-02 ~13:47 UTC)
- **Duration**: 1.5 days

## Table: `market_snapshots`

### Schema (13 columns)

| Column | Type | Nullable | Purpose |
|--------|------|----------|---------|
| `id` | INTEGER | No | Primary key |
| `timestamp` | INTEGER | No | Snapshot time (seconds UTC) |
| `slug` | TEXT | No | Polymarket slug (e.g., `btc-updown-5m-1772196000`) |
| `up_price` | REAL | No | Implied probability of UP |
| `down_price` | REAL | No | Implied probability of DOWN |
| `btc_price` | REAL | Yes | BTC price at snapshot time (may be NULL) |
| `created_at` | TEXT | Yes | ISO 8601 creation timestamp |
| `up_best_bid` | REAL | Yes | Best bid for UP token |
| `up_best_ask` | REAL | Yes | Best ask for UP token |
| `up_spread` | REAL | Yes | Bid-ask spread for UP |
| `down_best_bid` | REAL | Yes | Best bid for DOWN token |
| `down_best_ask` | REAL | Yes | Best ask for DOWN token |
| `down_spread` | REAL | Yes | Bid-ask spread for DOWN |

### Data Availability

- **Total snapshots**: 74,703
- **Unique markets**: 807
- **Snapshots per market**: ~92 average
  - Example: Market `btc-updown-5m-1772196000` has 98 snapshots over 297 seconds (~3s frequency)
  - Suggests snapshots captured every 3-5 seconds during market lifetime

### Snapshot Frequency

Markets are live for ~5 minutes each, with snapshots captured at **~3-5 second intervals** throughout the market lifetime.

## Key Insights for Early Exit Analysis

### 1. Rich Market Microstructure Data

**Available**: Bid/ask prices, spreads, and depth data over time
- Can track liquidity depth progression during 5-min window
- Can identify when bids reach 0.95+ threshold with sufficient depth
- Early exit strategy can correlate trigger hits with actual trade outcomes

### 2. Complete Trade History with Settlement

**Available**: Entry price, outcome, P&L, timestamp, settlement time
- Full P&L attribution (before and after fees)
- Early exit captures premium above outcome value
- 32 EARLY_EXIT trades provide direct measurement of early exit profitability

### 3. Regime Data (Sparse but Growing)

**Limited**: Only 37 trades (11.2%) have regime state/strength
- Not backfilled to historical trades
- Can analyze correlation between regime and early exit profitability
- Future trades will accumulate more regime data for trend analysis

### 4. Maker Fills (26.9% of trades)

**Important**: 89 trades filled as market maker (passive orders)
- Different risk profile from taker entries (worse entry price avg)
- Should analyze early exit profitability separately for maker vs taker
- Maker orders don't exit early (no active selling for passive fills)

### 5. Trade Tags for Segmentation

**Useful**: Can segment analysis by trade_tag
- `exploration` trades (n=8): different model confidence, can compare early exit rates
- `maker_fill` trades (n=89): passive entries, exclude from early exit analysis
- Normal trades (n=234): full early exit opportunity

## Queries for Extended Early Exit Analysis

### Early Exit Performance by Entry Price Bucket

```sql
SELECT
    ROUND(entry_price, 0.05) as price_bucket,
    COUNT(*) as total_trades,
    SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as early_exits,
    100.0 * SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) / COUNT(*) as early_exit_rate,
    ROUND(AVG(CASE WHEN outcome='EARLY_EXIT' THEN pnl ELSE NULL END), 2) as avg_early_exit_pnl,
    ROUND(AVG(CASE WHEN outcome IN ('WIN','LOSS') THEN pnl ELSE NULL END), 2) as avg_full_hold_pnl
FROM live_trades
WHERE success=1 AND outcome IS NOT NULL AND trade_tag != 'maker_fill'
GROUP BY ROUND(entry_price, 0.05)
ORDER BY price_bucket;
```

### Bid Depth When Early Exit Triggered

```sql
SELECT
    lt.market_slug,
    lt.entry_price,
    ms.up_best_bid,
    ms.up_spread,
    lt.outcome,
    lt.pnl
FROM live_trades lt
JOIN market_snapshots ms ON ms.slug = lt.market_slug
WHERE lt.outcome='EARLY_EXIT'
    AND ms.timestamp > lt.timestamp / 1000
    AND ms.timestamp < (lt.timestamp / 1000) + 300  -- within 5-min window
    AND ms.up_best_bid >= 0.95
ORDER BY lt.timestamp DESC
LIMIT 20;
```

### Early Exit vs Full Hold Comparison

```sql
SELECT
    'EARLY_EXIT' as strategy,
    COUNT(*) as trades,
    ROUND(SUM(pnl), 2) as total_pnl,
    ROUND(AVG(pnl), 3) as avg_pnl,
    ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 1) as win_rate
FROM live_trades
WHERE success=1 AND outcome='EARLY_EXIT' AND trade_tag != 'maker_fill'
UNION ALL
SELECT
    'FULL_HOLD' as strategy,
    COUNT(*) as trades,
    ROUND(SUM(pnl), 2) as total_pnl,
    ROUND(AVG(pnl), 3) as avg_pnl,
    ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 1) as win_rate
FROM live_trades
WHERE success=1 AND outcome IN ('WIN', 'LOSS') AND trade_tag != 'maker_fill';
```

### Regime Impact on Early Exit Frequency

```sql
SELECT
    COALESCE(regime_state, 'unknown') as regime,
    COUNT(*) as total_trades,
    SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as early_exits,
    ROUND(100.0 * SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) / COUNT(*), 1) as early_exit_rate,
    ROUND(AVG(CASE WHEN outcome='EARLY_EXIT' THEN pnl ELSE NULL END), 2) as avg_early_exit_pnl
FROM live_trades
WHERE success=1 AND outcome IS NOT NULL AND trade_tag != 'maker_fill'
GROUP BY regime_state
ORDER BY total_trades DESC;
```

## File Locations

- **Database**: `/C/Users/Rn5ho/BTC-tool/btc_edge.db`
- **Schema creation**: `/C/Users/Rn5ho/BTC-tool/storage/db.py`
- **Trade recording**: `/C/Users/Rn5ho/BTC-tool/strategy/live_trader.py`
- **Market snapshots**: `/C/Users/Rn5ho/BTC-tool/data/polymarket.py`

