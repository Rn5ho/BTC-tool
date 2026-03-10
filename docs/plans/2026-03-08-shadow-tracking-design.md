# Design: Shadow Order Book Tracking

**Date:** 2026-03-08
**Problem:** We only collect bid behavior (max_bid) on windows we trade. To train an exit probability model ("will EE trigger?"), we need bid data on ALL windows — traded and skipped. Currently 756 skipped windows have zero bid data.

## Solution

A background loop polls the order book for both UP and DOWN tokens every 10 seconds on every window, regardless of trading. At window end, writes one summary row to a `shadow_windows` table.

### Data collected per window

- `market_slug` — window identifier
- `up_open_ask`, `down_open_ask` — entry cost at window open
- `up_max_bid`, `down_max_bid` — highest bid seen during window (EE proxy)
- `up_min_bid`, `down_min_bid` — lowest bid seen
- `btc_price_start`, `btc_price_end` — BTC movement
- `regime_state`, `regime_strength` — market regime
- `traded_side` — what we traded (NULL if skipped)
- `traded_tag` — trade_tag if traded
- `poll_count` — data quality check
- 11 Binance features: OBI, taker_ratio, momentum_1m/5m, RSI, vwap_dev, bb_position, ema_cross, funding_zscore, volume_zscore, ATR
- `created_at` — timestamp

### Architecture

- New async task `_shadow_book_loop()` launched alongside existing tasks in `_run()`
- Polls every 10 seconds (vs 1s for EE monitoring — no need for speed here)
- Independent of EE loop — doesn't interfere with trading
- At window transition (detected via slug change), saves the accumulated data
- Resets tracking state for new window

### What does NOT change

- Trading logic, EE monitoring, order placement — untouched
- Existing `market_snapshots` table — shadow is separate
- API connections — reuses existing polymarket client

### Load

- ~8K additional API calls/day (within limits, already doing ~15K for EE)
- ~288 rows/day in DB, ~56 KB/day
