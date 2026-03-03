# Live Trading Reference

Live trading places real GTC market buy orders on Polymarket alongside paper trades.

## Architecture

- `strategy/live_trader.py`: `LiveTrader` class wrapping py-clob-client (synchronous lib)
- All CLOB API calls wrapped in `asyncio.to_thread()` to avoid blocking the event loop
- Uses `signature_type=2` (Polymarket proxy wallet from Rabby browser extension)
- Order type: GTC market buy via `MarketOrderArgs`+`create_market_order`
- CLOB 5-token minimum: amount auto-bumped to `5 × entry_price` (~$2.50 at typical prices)
- Retry with exponential backoff on 425 "Too Early" errors (matching engine restarts)
- **Early exit selling**: `sell_early_exit()` sells tokens *before* resolution when bid >= $0.95. Replaces disabled post-resolution auto-sell. Unsold tokens claimed manually on polymarket.com.
- Candle prefetch from Binance REST API eliminates 30-min buffering delay on restart

## py-clob-client Quirks & Patches

- **Rounding bug** (CRITICAL): `get_market_order_amounts` for BUY rounds taker_amount to `round_config.amount` (4-5 decimals) but CLOB requires max 2. The library has the rounding reversed for BUY market orders (maker and taker swapped). Fixed by monkey-patching `OrderBuilder.get_market_order_amounts` in `initialize()` to compute taker first (rounded to 2 dec), then derive maker (up to 4 dec). **Must use `round_down` for maker** — floating point causes `round_up` to overshoot by 0.0001, which CLOB rejects.
- **Maker float precision** (CRITICAL): Even with `round_down`, float multiplication is imprecise (e.g. `5.93 * 0.59 = 3.4986999...` but CLOB expects `3.4987`). Fixed by using `Decimal` for maker amount calculation in the monkey-patched `_patched` function: `d_maker = Decimal(str(taker)) * Decimal(str(price))`.
- **5-token minimum**: ALL CLOB markets have `minimum_order_size: 5` (tokens, not USDC). At $0.50 per token = $2.50 minimum per trade. The Polymarket website uses a different mechanism for small orders. `place_order()` auto-bumps amount to `5 × entry_price`. `MAX_LIVE_BET_USDC=5.0` to accommodate.
- **$1 minimum for marketable orders**: After the rounding patch, the effective maker amount can drop below $1. Explicit `$1.00` floor added in `place_order()`.
- **FOK fails on thin books**: FOK orders require immediate full fill. 5-min binary markets are thin. Use GTC instead.
- **Balance API**: `get_balance_allowance()` requires `BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)` — no default.
- **pydantic-settings vs os.environ**: pydantic-settings reads .env but does NOT set OS env vars. Use `settings.clob_proxy` not `os.environ.get("CLOB_PROXY")`.
- **SELL amount semantics**: `MarketOrderArgs(amount, side=SELL)` expects **token count**, not USDC. For BUY, amount = USDC to spend. For SELL, amount = tokens to sell. Passing USDC value for SELL causes partial fills.

## Geoblock — Not Applicable

- VPS is in Helsinki, Finland — Polymarket CLOB API is accessible directly (no geoblock)
- Previously ran on Hetzner Germany with SSH tunnel proxy; migrated to Helsinki to eliminate proxy dependency
- Proxy monkey-patching code still exists in live_trader.py but is inactive (CLOB_PROXY is empty)

## Safety Controls

- Hard cap: `MAX_LIVE_BET_USDC=5.0` (accommodates 5-token minimum even at $0.70+ prices)
- Minimum: `max(5.5 × entry_price, $3.50)` per trade — $3.50 hard floor guarantees 5 tokens at any fill price up to $0.70 (CLOB fill price can shift from entry_price signal)
- Floor: `$1.00` minimum per order (CLOB rejects marketable orders below $1)
- Balance check before each order
- All paper trading safety filters apply (time gate, hour blacklist, trend conflict, confidence)
- `/pause` stops both paper and live trading

## DB Schema

```sql
CREATE TABLE live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    market_slug TEXT NOT NULL,
    side TEXT NOT NULL,
    token_id TEXT NOT NULL,
    amount_usdc REAL NOT NULL,
    order_id TEXT,
    status TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    response_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    -- Settlement tracking (added for decoupled live trading)
    entry_price REAL,
    outcome TEXT,           -- 'WIN', 'LOSS', or 'EARLY_EXIT'
    pnl REAL,
    settled_at INTEGER
);
```

## Independent Live Trading

- Live trader has its own bankroll, adaptive sizing, and settlement tracking (decoupled from paper trader)
- On startup: bankroll synced from **real CLOB USDC balance** via `get_balance()` (source of truth)
- Periodic bankroll sync every 30 min in `_stats_loop()` to stay in sync with on-chain state
- `compute_bet_size(confidence)`: independent adaptive sizing (same algorithm as paper trader)
- `record_settlement(won, pnl)`: updates bankroll, streak, drawdown state
- Live trades settled in `_settle_live_trades_for_window()` alongside paper trades
- `/stats` shows live stats as primary, paper as secondary

## Live Trading Results

### Phase 1 (2026-03-01, first 9 hours — Germany VPS with proxy)
From actual Polymarket CSV export:
- **Deposited**: $20.00
- **43 buy trades**, $100.33 total (capital recycled ~5x)
- **Final balance**: $25.24 → **+$5.24 profit (+26.2% ROI)**
- **Fill rate**: 54% initially → ~95%+ after fixes

### Phase 2 (2026-03-01 onwards — Helsinki VPS, decoupled)
- **Capital**: ~$73 deposited total
- **CLOB-verified P&L** (14.2 hours): USDC balance $119.90 → **+$46.90 profit (+64% ROI)**
- **107 CLOB trades**: 88 taker (our bot), 21 maker (whales hitting our GTC orders — bonus P&L)
- **Paper trades**: 173 settled, 60.5% WR, +$20.15 P&L
- **7 early exits**: +$16.20 P&L (significant contributor)
- **~35 failed orders** (~32% fail rate, mostly 5-token minimum edge cases)
- **Early exit selling**: Active on dedicated 1s loop, bid >= $0.95, no time restriction
- **Live bankroll**: Synced from real CLOB USDC balance (startup + 30-min periodic)

## Early Exit Selling

Sells live tokens before settlement when the outcome is nearly certain, locking in ~90%+ of max profit and eliminating last-second reversal risk.

### How It Works
- **Dedicated 1s loop** (`_early_exit_loop()`) — separate asyncio task, faster than 3s analysis cycle
- `_monitor_early_exit()` checks best bid price and depth for the active position's token
- **Trigger**: `bid >= 0.95` AND `depth >= 20 tokens` — **no time restriction** (triggers as soon as conditions met)
- `sell_early_exit()` in LiveTrader places a GTC SELL market order via `MarketOrderArgs(token_id, round(tokens, 2), side=SELL)`
- Amount = full token count (SELL expects tokens, not USDC). Requires >= 5 tokens.
- `exit_failed` flag prevents retry spam (1s loop would retry every second without it)
- On success: DB updated with `outcome="EARLY_EXIT"`, PnL recorded, Telegram alert sent

### Token Tracking
- At buy time: `tokens = (amount_usdc / entry_price) × (1 - fee_factor)`
- Stored in `_live_trade_tokens[slug]` dict alongside `db_id` for settlement updates
- `exited=True` flag prevents double-settlement at window end

### Settlement Integration
- `get_unsettled_live_trades()` queries `WHERE outcome IS NULL` — EARLY_EXIT trades auto-skipped
- `_live_trade_tokens` entry with `exited=True` also prevents in-memory settlement

### Log Format
- Data: `[EARLY-EXIT] UP n-5m-... | 40s left | bid=0.760 x107 | depth>=0.85: 0 tokens | entry=0.500 profit=52.0%`
- Sell: `[EARLY-EXIT SOLD] UP n-5m-... | bid=0.900 | tokens=6.5 | sell=$5.85 buy=$3.50 | pnl=$+2.35 | 29s before settlement`

### CLOB API Notes
- Bids sorted ascending (lowest first). Best bid = `bids[-1]`.
- Bids fluctuate wildly in last 2 min. Winning positions reliably reach $0.90+ in last ~60s.
