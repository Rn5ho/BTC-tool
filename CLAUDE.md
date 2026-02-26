# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — a real-time tool that monitors Binance BTC price data (spot + futures), computes directional probability estimates for 5-minute price movements, compares them against Polymarket's implied odds, and paper trades when mispricing is detected. Telegram bot for alerts and interactive commands.

**Status:** Fully functional and deployed on Hetzner VPS (46.225.27.241) running 24/7 as a systemd service. Paper trading works end-to-end with Polymarket fee model. Live trading via py-clob-client is implemented and runs in parallel with paper trading (disabled by default, toggle via `LIVE_TRADING_ENABLED`). Interactive Telegram bot with commands for monitoring and management of both paper and live trading.

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (spot klines + depth + aggTrades, futures funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices, no auth needed)
- Polymarket RTDS WebSocket for Chainlink BTC/USD stream (settlement price — matches Polymarket's resolution source)
- SQLite via aiosqlite (persistence)
- python-telegram-bot v21+ (interactive bot with commands)
- pydantic-settings (config from .env)
- numpy (indicators), no pandas at runtime
- py-clob-client (Polymarket CLOB API for live trading — sync library, wrapped with asyncio.to_thread)

## Architecture

```
data/           → Data collection layer
  models.py     → Shared dataclasses (Candle, OrderBook, AggTrade, FundingInfo, FeatureVector, PaperTrade, PolymarketMarket)
  binance_ws.py → Binance WebSocket client (kline_1m, depth20@100ms, aggTrade streams, futures funding)
  polymarket.py → Polymarket Gamma/CLOB client (slug discovery, live prices, Chainlink RTDS stream, fee rate API, compute_fee_factor())

signals/        → Signal generation
  indicators.py → Technical indicators (RSI-9, VWAP, BB-20, EMA 9/21, ATR-14, momentum, vol z-score)
  features.py   → Feature engineering (OBI, taker ratio, funding z-score → FeatureVector)
  probability.py→ Weighted ensemble model → P(up) in [0.05, 0.95]

strategy/       → Trading logic
  edge.py       → Edge detection (compare P(up) vs fee-adjusted Polymarket implied odds, positive-edge only)
  paper_trader.py → Paper trading engine (Kelly/fixed sizing, fee-adjusted PnL, settlement, bankroll restoration)
  live_trader.py  → Live trading engine (py-clob-client FOK market orders, 2% bankroll sizing, parallel with paper)

alerts/
  telegram.py   → Telegram bot (edge alerts, trade notifications, settlements, interactive commands)

storage/
  db.py         → SQLite (candles, feature_snapshots, paper_trades, live_trades, market_snapshots, historical price lookup)

deploy/         → Hetzner VPS deployment
  btc-edge.service → systemd service file (runs as btcedge user, auto-restart)
  setup.sh      → Automated server setup script (Ubuntu/Debian)

config.py            → Pydantic Settings loaded from .env
main.py              → Async orchestrator wiring all components, Telegram command handlers, console output
analyze_trades.py    → Standalone trade analysis script (run on VPS: python analyze_trades.py)
```

## Key Commands

```bash
# Install dependencies
pip install -e .

# Run the tool
python main.py

# Syntax check all files
python -m py_compile main.py config.py data/models.py data/binance_ws.py data/polymarket.py signals/indicators.py signals/features.py signals/probability.py strategy/edge.py strategy/paper_trader.py strategy/live_trader.py alerts/telegram.py storage/db.py
```

### Hetzner VPS (46.225.27.241)

```bash
# SSH into server
ssh root@46.225.27.241

# Check service status
systemctl status btc-edge

# View logs
tail -f /home/btcedge/BTC-tool/btc_edge.log

# Deploy latest code
cd /home/btcedge/BTC-tool
sudo -u btcedge git pull origin claude/read-claude-docs-jBeBH
sudo systemctl restart btc-edge

# Reset paper trading data
sudo -u btcedge sqlite3 /home/btcedge/BTC-tool/btc_edge.db "DELETE FROM paper_trades;"
```

## Telegram Bot Commands

The bot (`@BTC5mBot`) supports interactive commands:

| Command | Description |
|---------|-------------|
| `/status` | Current BTC price (Binance + Chainlink), model P(up), market odds, window info, pause state |
| `/stats` | Trading performance: total trades, win rate, P&L, bankroll, ROI |
| `/trades` | List pending (unsettled) paper trades |
| `/pause` | Stop placing new trades (data collection and settlement continue) |
| `/resume` | Resume placing trades |
| `/weights` | Display current probability model weights with ASCII bar chart |
| `/analyze` | Full trade analysis: overall stats, edge buckets, side breakdown, hourly win rate |
| `/reset` | Two-step confirmation to clear all trade data and reset bankroll |
| `/budget` | Show current bankroll and bet size |
| `/budget 200` | Set bankroll to $200 (also resets initial_bankroll for ROI calculation) |
| `/live` | Live trading status: state, bankroll, on-chain USDC balance, bet size, P&L, win rate |
| `/live_pause` | Pause live trading only (paper trading continues) |
| `/live_resume` | Resume live trading |
| `/help` | List available commands |

Commands are dispatched via long-polling (`get_updates`) in a dedicated asyncio task. Handlers accept an optional `args: str = ""` for commands like `/budget 200`.

## Configuration

Copy `.env.example` to `.env`. Key settings:

### Telegram
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional, alerts and bot commands disabled if missing

### Strategy Thresholds
- `MIN_EDGE_THRESHOLD` — minimum positive edge to trigger paper trade (default 0.05 = 5%)
- `MIN_EDGE_DOWN` — minimum edge for DOWN-side trades (default 0.08 = 8%, higher bar because DOWN trades have lower historical win rate)
- `MAX_EDGE_THRESHOLD` — maximum edge cap; edges above this are rejected as model overconfidence (default 0.18 = 18%)
- `MAX_SIGNAL_VALUE` — skip trades where any single signal exceeds this magnitude (default 0.45; catches saturated OBI/taker near +-0.5 limits)
- `CONFIDENCE_DAMPEN` — shrink P(up) toward 50% to counter overconfidence (default 0.6; 1.0 = no dampening)
- `BLACKLIST_HOURS` — comma-separated UTC hours to skip trading (default "2" — 02:00 UTC has 37% WR)

### Sizing
- `BET_SIZE_USDC` — fixed bet size per trade (default 5)
- `VIRTUAL_BANKROLL` — starting paper bankroll (default 100)
- `USE_KELLY` — use half-Kelly sizing instead of fixed (default false)

### Polymarket
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_PASSPHRASE` — Builder Mode credentials for live trading (leave blank for paper-only)
- `POLYMARKET_FEE_RATE` — fee curve rate parameter (default 0.25 for crypto markets)
- `POLYMARKET_FEE_EXPONENT` — fee curve exponent (default 2 for crypto markets)

### Live Trading
- `LIVE_TRADING_ENABLED` — master switch for live trading (default false, must be explicitly enabled)
- `POLYGON_PRIVATE_KEY` — Polygon wallet private key (hex, 0x prefix) for signing CLOB orders
- `POLYGON_WALLET_ADDRESS` — wallet address that holds USDC.e on Polygon
- `LIVE_BET_PCT` — bet size as fraction of live bankroll (default 0.02 = 2%)
- `LIVE_BANKROLL` — starting live bankroll in USDC (default 100)

### Data URLs
- `BINANCE_WS_URL` — Binance WebSocket endpoint (default `wss://stream.binance.com:9443/ws`)
- `POLYMARKET_GAMMA_URL` — Gamma API for market discovery (default `https://gamma-api.polymarket.com`)
- `POLYMARKET_CLOB_URL` — CLOB API for live prices (default `https://clob.polymarket.com`)

### Model Weights
- `W_OBI`, `W_TAKER`, `W_MOMENTUM`, `W_RSI`, `W_VWAP`, `W_FUNDING` — probability model weights (must sum to 1.0)

## Data Flow

```
Binance WS (spot+futures) → Rolling State (candles, orderbook, trades, funding)
                              ↓
                        Feature Engineering → FeatureVector
                              ↓
                        Probability Model → P(up)
                              ↓                          ↓
                        Edge Detection          SQLite (log features)
                              ↓
Polymarket API → Implied P(up)  →  edge = our_P(side) - market_P(side)
                              ↓ (if edge > threshold, positive only)
                        Safety Filters (time gate + hour blacklist + trend conflict)
                              ↓
                    ┌─────────┴─────────┐
              Paper Trader         Live Trader (if enabled)
              (simulate bet)       (FOK market buy via CLOB)
              log to SQLite        log to SQLite + order_id
                    └─────────┬─────────┘
                              ↓
                        Telegram Alert → notify user
```

## Probability Model (v1 — Rule-Based Weighted Ensemble)

```
P_raw = 0.5 + w_obi*OBI + w_taker*taker + w_momentum*momentum + w_rsi*rsi + w_vwap*vwap + w_funding*funding
P(up) = 0.5 + CONFIDENCE_DAMPEN * (P_raw - 0.5)
```

Confidence dampening (default 0.6) shrinks predictions toward 50% to counter the model's systematic overconfidence (calibration analysis on 2,736 trades showed 10-20% overestimation at every probability bucket).

Each signal is normalized to [-0.5, 0.5]:
- **OBI** (order book imbalance): bid/ask volume ratio → [-0.5, 0.5]
- **Taker ratio**: net taker buy/sell ratio → [-0.5, 0.5]
- **Momentum**: 60% of 1m + 40% of 5m momentum, clipped at ±2% → [-0.5, 0.5]
- **RSI(9)**: (rsi - 50) / 100 → [-0.5, 0.5]
- **VWAP deviation**: price vs VWAP, clipped at ±1% → [-0.5, 0.5]
- **Funding rate**: z-score inverted (high funding = bearish) → [-0.5, 0.5]

Final P(up) clamped to [0.05, 0.95]. Default weights: OBI=0.25, taker=0.25, momentum=0.15, RSI=0.15, VWAP=0.10, funding=0.10.

## Edge Detection & Safety Filters

The edge detector (`strategy/edge.py`) evaluates both sides and only considers **positive** edges, with fee-adjusted probabilities:

- **Taker fee model**: Polymarket crypto markets charge fees using a bell-curve formula: `fee_factor = fee_rate * (price * (1 - price))^exponent`. At price=0.50 with crypto defaults (rate=0.25, exp=2): ~1.56% fee. Near-zero at price extremes. The fee-adjusted break-even probability is `p / (1 - fee_factor)` — edges must clear this hurdle to be real after fees.

1. **Edge calculation**: For each side, `edge = our_P(side) - fee_adjusted_market_P(side)`. Only sides where we think the market underprices **after fees** (positive edge) are candidates.
2. **Side selection**: Pick the side with the larger positive edge. If neither side has positive edge, no trade.
3. **Threshold**: Only trade if `edge > MIN_EDGE_THRESHOLD` (default 5%).

### Safety Filters (applied in order)

1. **Pause gate**: Trading can be paused via `/pause` Telegram command — data collection and settlement continue but no new trades are placed.
2. **Duplicate prevention**: One pending trade per market slug (no duplicate bets on same 5-min window).
3. **Time gate** (`_MAX_ENTRY_SECONDS = 120`): Only enter trades in the first 2 minutes of a 5-minute window. After that, the market has already priced in the move and any remaining "edge" is likely stale.
4. **Hour blacklist** (`BLACKLIST_HOURS`): Skip trading during configured UTC hours. Default: 02:00 UTC (37% win rate, -$114 P&L over 108 trades in analysis). Configurable via comma-separated env var.
5. **Trend-conflict filter** (`_TREND_CONFLICT_PCT = 0.15`): If BTC has already moved >0.15% in one direction within the current window and the model's signal is the opposite direction, the trade is skipped. Prevents contrarian bets against strong intra-window momentum.
6. **Max edge cap** (`MAX_EDGE_THRESHOLD = 0.18`): Edges above 18% are rejected. Analysis of 2,736 trades showed 18-20% edge trades are net negative, and 20%+ trades win only 31%. Lowered from 0.20 to 0.18 based on data showing $501 P&L at 18% cap vs $445 at 20%.
7. **DOWN-side higher threshold** (`MIN_EDGE_DOWN = 0.08`): DOWN trades require a larger edge than UP trades. Historical data shows DOWN trades have significantly lower win rate (~33% vs ~64% for UP at 5% threshold).
8. **Signal saturation filter** (`MAX_SIGNAL_VALUE = 0.45`): When any individual signal exceeds +-0.45, the trade is skipped. Extreme readings (e.g. OBI at -0.495) indicate a single noisy input is dominating the model.

## Paper Trading & Fee Model

Paper trading engine (`strategy/paper_trader.py`) simulates Polymarket's actual taker fee structure:

- **Fee model**: Bell-curve taker fee from Maker Rebates Program docs: `fee_factor = fee_rate * (price * (1 - price))^exponent`. Buying shares at price *p* with fee factor *f*: `shares = (size / p) * (1 - f)`.
- **PnL**: WIN = `shares - size` (each winning share pays $1.00), LOSS = `-size` (shares worth $0)
- **Sizing**: Fixed ($5/trade default) or half-Kelly criterion, never exceeding bankroll
- **Kelly formula**: `f* = (p*b - q) / b`, half-Kelly with 5% of bankroll cap
- **Default config**: $100 bankroll, $5 bet size
- **Bankroll persistence**: `restore_bankroll()` loads cumulative P&L from DB on startup so bankroll survives restarts
- **Stats**: Tracks total trades, settled, wins, losses, win rate, cumulative PnL, total fees paid, bankroll, ROI

## Live Trading (py-clob-client)

Live trading (`strategy/live_trader.py`) places real orders on Polymarket's CLOB in parallel with paper trading:

- **Library**: py-clob-client (synchronous — all calls wrapped with `asyncio.to_thread()`)
- **Order type**: FOK (Fill-Or-Kill) market buy orders — immediate full fill or cancel
- **Sizing**: `LIVE_BET_PCT` * current bankroll (default 2% = $2 on $100 start)
- **Minimum order**: $0.50 (below this, trade is skipped to avoid dust rejections)
- **Token selection**: Orchestrator injects the correct `token_id` (UP or DOWN) into the signal dict based on the edge detector's chosen side
- **Same safety filters**: Live trades pass through all 8 safety filters identically to paper trades
- **Parallel operation**: Paper trading always runs; live trading runs alongside when enabled
- **Independent pause**: `/live_pause` stops live orders without affecting paper trading
- **Bankroll persistence**: `restore_bankroll()` loads cumulative P&L from `live_trades` table on restart
- **Settlement**: Same window-boundary settlement as paper; both traders settle simultaneously using Chainlink RTDS prices
- **Stale trade recovery**: On restart, unsettled live trades are recovered from DB and settled using historical candle data (same as paper)
- **Graceful degradation**: If py-clob-client is not installed or CLOB API fails, live trades are silently skipped; paper trading and data collection continue unaffected

### Prerequisites for live trading
1. Polygon wallet with USDC.e balance
2. Small amount of POL for gas (token approvals)
3. One-time token allowance approval for Polymarket exchange contracts
4. Builder Mode API credentials (derive via `client.create_or_derive_api_creds()`)
5. Set `LIVE_TRADING_ENABLED=true` in `.env`

### Database
Live trades are stored in a separate `live_trades` table (same schema as `paper_trades` plus `order_id` column) for clean separation and independent statistics.

## Window Lifecycle & Settlement

1. **Window detection**: Slugs are deterministic (`btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`). The analysis loop detects transitions every 3-second cycle.
2. **Settlement source**: Chainlink BTC/USD via Polymarket RTDS WebSocket (the actual resolution source). Binance spot as fallback.
3. **Settlement is decoupled from market discovery**: Window transitions are detected via `get_current_slug()` BEFORE the Gamma API call. This ensures settlement fires even if the Gamma API is slow/fails.
4. **Startup recovery**: On restart, after the 5-minute buffering phase, window tracking is initialized and any stale unsettled trades from previous sessions are settled using historical candle data from the DB (`get_btc_price_at()`). Trades with no matching candle data are voided (0 PnL).

## Startup Sequence

1. Initialize DB (create tables if needed, including `live_trades`)
2. Create PaperTrader; create LiveTrader if credentials present and `LIVE_TRADING_ENABLED=true`
3. Restore bankrolls from DB (both paper and live)
4. Create TelegramAlerter, start Polymarket aiohttp session
4. Launch 5 concurrent asyncio tasks:
   - **Binance WS**: Streams kline_1m, depth20, aggTrade, futures funding
   - **Chainlink RTDS**: Streams BTC/USD from Polymarket's data service
   - **Analysis loop**: Buffers 5 candles (~5 min), initializes window tracking, settles stale trades, then runs 3-second poll cycle
   - **Stats loop**: Periodic stats report every 30 minutes
   - **Telegram command listener**: Long-polls for incoming /commands

## Console Output Format

The tool prints clean ASCII to the console (no emojis — Windows cp1252 safe):
- **Buffering phase**: `Buffering: 0/5 candles | BTC: $68,562 | trades: 158 | orderbook: yes`
- **Edge signals**: `>>> EDGE: DOWN btc-updown-5m-... | our=57.0% mkt=48.5% edge=+8.5% fee=1.56% | BTC=$68,562 | [obi=+0.123, taker=-0.045, momentum=+0.089]`
- **Settlement**: `--- WINDOW SETTLED: btc-updown-5m-... | BTC $68544 -> $68562 (+18.00 = UP) [Chainlink Stream]`
- **P&L summary**: `--- P&L: $+3.04 | Win rate: 60% (3/5) | Bankroll: $103.04`
- **Live P&L** (when live trading enabled): `--- LIVE P&L: $+1.22 | Win rate: 60% (3/5) | Bankroll: $101.22`
- **Live trade placed**: `LIVE TRADE PLACED: UP btc-updown-5m-... | size=$2.00 entry=0.4800 edge=0.0650 | order=abc123...`
- **Status heartbeat** (every 30s): `-- Status: BTC $68,679 | P(up)=55.1% | Mkt=50/50 | Trades: 5 (60% win) | P&L: $+3.04`

## Deployment (Hetzner VPS)

The bot runs 24/7 on a Hetzner VPS at `46.225.27.241`:

- **User**: `btcedge` (dedicated, non-root)
- **Directory**: `/home/btcedge/BTC-tool`
- **Service**: `btc-edge.service` via systemd (`Restart=always`, `RestartSec=10`)
- **Logs**: `/home/btcedge/BTC-tool/btc_edge.log` (also via `journalctl -u btc-edge`)
- **Hardening**: `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths=/home/btcedge/BTC-tool`
- **Setup**: `deploy/setup.sh` automates user creation, repo clone, venv setup, service install
- **Branch**: Currently tracking `claude/read-claude-docs-jBeBH`

## Trade Analysis Results (2,736 trades, 2026-02-16 to 2026-02-26)

Analysis script: `python analyze_trades.py` (run on VPS).

**Overall**: 2,736 trades over 10.5 days, 49.7% win rate, +$416 P&L ($40/day), 3% ROI on stakes. Model is profitable due to asymmetric payouts (avg entry 0.479 = better-than-even payout on wins).

**Key findings that drove code changes:**
- **Model overconfidence**: Every calibration bucket showed OVER by 7-20%. Model predicts 60-65% but actual WR is 49.8%. Fixed with `CONFIDENCE_DAMPEN=0.6`.
- **MAX_EDGE 18% > 20%**: P&L at 18% cap = $501 vs $445 at 20% cap. Trades in 18-20% range are net losers.
- **02:00 UTC terrible**: 37% WR, -$114 over 108 trades — by far the worst hour. Added to `BLACKLIST_HOURS`.
- **Best hours**: 05:00 (61% WR, +$144), 07:00-08:00 (55-56% WR, +$103 each).
- **Best edge buckets**: 5-6% (52% WR, +$0.44/trade), 8-12% (51% WR, +$245 combined).
- **Features**: taker_ratio and volume_zscore are the strongest win/loss differentiators. OBI and momentum show near-zero predictive delta.
- **Streaks**: Max 10 win and 10 loss. Avg streak length 2.0 for both. No serial correlation (after-win WR = after-loss WR).
- **Max drawdown**: $200 (43% of peak equity) over 657 trades (Feb 18-21).
- **Friday**: 38.1% WR (-$98) but only 1 Friday in sample — needs more data.

## Known Issues & Fixes Applied

1. **Edge detector betting wrong side** (CRITICAL — fixed): The edge detector picked the side with the largest *absolute* edge rather than the largest *positive* edge. If `up_edge = -17%` and `down_edge = +17%`, it chose UP because `abs(-17%) >= abs(+17%)`. This caused the bot to systematically bet the opposite direction of what the model predicted. Fixed in `strategy/edge.py` to only consider sides with positive edge (where our probability exceeds the market's).

2. **Trades never settling after restart** (fixed): On service restart, `_current_slug` starts as `None`. The first window transition check (`self._current_slug and ...`) evaluated to `False`, silently skipping settlement of all pre-restart trades. Fixed by: (a) initializing window tracking after the buffering phase, (b) adding `_settle_stale_trades()` that pulls unsettled trades from the DB and settles them using historical candle data on startup.

3. **Settlement gated behind market discovery** (fixed): Settlement only happened inside `_run_one_cycle` after `discover_market()` succeeded. If the Gamma API was slow/failed for a new window, trades would never settle. Fixed by computing the slug via `get_current_slug()` and checking for window transitions BEFORE the Gamma API call.

4. **Late-window entries** (fixed): Trades could enter at any point during a 5-min window (e.g., 3 minutes in). By then the market has priced in the move and the "edge" is stale. Fixed with `_MAX_ENTRY_SECONDS = 120` time gate.

5. **Contrarian bets against strong trends** (fixed): Mean-reverting signals (OBI from dip-buyers, VWAP "oversold") produced UP signals during BTC crashes while the market correctly priced DOWN high. Fixed with `_TREND_CONFLICT_PCT = 0.15` trend-conflict filter.

6. **Chainlink stale settlement prices** (fixed): The on-chain Chainlink aggregator has a ~1h heartbeat, returning identical prices for window start and end. This always resolved as UP, inflating win rates to ~82%. Fixed by streaming via Polymarket RTDS WebSocket — the actual resolution source.

7. **Windows cp1252 encoding** (fixed): Telegram emojis logged at INFO level caused `UnicodeEncodeError`. Fixed by logging at DEBUG level with ASCII-only stripping.

8. **Console spam** (fixed): Edge signals logged every 3-second cycle. Fixed with duplicate trade detection and 30-second heartbeat interval.

9. **Duplicate log lines on Hetzner** (fixed): `setup_logging()` added both a StreamHandler and FileHandler(`btc_edge.log`). Under systemd, stdout is already captured to the same file via `StandardOutput=append`, causing every line to appear twice. Fixed by only adding FileHandler when stdout is a TTY (interactive).

10. **httpx log spam** (fixed): Telegram's `getUpdates` polling logged every 10 seconds via httpx at INFO level. Fixed by setting httpx/httpcore loggers to WARNING.

11. **P&L/bankroll inconsistency across restarts** (fixed): Bankroll was reset to VIRTUAL_BANKROLL on each restart, ignoring accumulated P&L. Fixed with `restore_bankroll()` which loads historical PnL from DB on startup.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info — emojis only in Telegram HTML messages)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)
- Paper trades stored as dicts in `_pending_trades` (keyed by market slug), persisted to SQLite
- Live trades stored identically in LiveTrader's `_pending_trades`, persisted to separate `live_trades` table
- Synchronous third-party libraries (py-clob-client) wrapped with `asyncio.to_thread()` to avoid blocking

## Potential Next Steps

- **Live trading validation**: Live trading is implemented — next step is to fund a Polygon wallet with ~$100 USDC.e, derive API credentials, approve token allowances, and enable with `LIVE_TRADING_ENABLED=true`. Run for 1-2 weeks alongside paper trading to compare results and validate that real fills match paper expectations.
- **Edge threshold tuning for live**: Currently live uses the same thresholds as paper (5% UP, 8% DOWN). Collect more forward data with current filters before differentiating — high-conviction trades haven't necessarily been the most successful, so the meaning of "edge" may need revisiting.
- **Weight rebalancing**: Data shows taker_ratio and volume_zscore are the strongest features; OBI and momentum have near-zero predictive delta. Consider increasing W_TAKER, adding volume_zscore as a weighted signal, and reducing W_OBI/W_MOMENTUM.
- **Hour scheduling**: Consider expanding blacklist to other weak hours (11:00=42.5% WR, 19:00-21:00=44-46% WR) once more data confirms the pattern.
- **Friday filter**: Only 1 Friday in sample (38.1% WR, -$98) — collect more data before adding a day-of-week filter.
- **Model improvements**: ML-based probability model, more features (liquidation data, funding rate momentum, cross-exchange flows)
- **Backtesting**: Replay historical data to validate signal weights and dampening factor
