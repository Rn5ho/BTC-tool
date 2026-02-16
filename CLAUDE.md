# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — a real-time tool that monitors Binance BTC price data (spot + futures), computes directional probability estimates for 5-minute price movements, compares them against Polymarket's implied odds, and paper trades when mispricing is detected. Telegram bot for alerts and interactive control.

**Status:** Fully functional and deployed to production. Paper trading runs 24/7 on a Hetzner VPS with Telegram bot for monitoring and control. All data collection, edge detection, trading, and settlement are automated. Live trading infrastructure (Builder Mode credentials, fee calculation) is pre-configured — only the order placement layer remains to be built.

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (spot klines + depth + aggTrades, futures funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices, no auth needed)
- Polymarket RTDS WebSocket for Chainlink BTC/USD stream (settlement price — matches Polymarket's resolution source)
- SQLite via aiosqlite (persistence)
- python-telegram-bot v21+ (interactive bot + alerts)
- pydantic-settings (config from .env)
- numpy (indicators), no pandas at runtime

## Architecture

```
data/               → Data collection layer
  models.py         → Shared dataclasses (Candle, OrderBook, AggTrade, FundingInfo, FeatureVector, PaperTrade, PolymarketMarket)
  binance_ws.py     → Binance WebSocket client (kline_1m, depth20@100ms, aggTrade streams, futures funding)
  polymarket.py     → Polymarket Gamma/CLOB client (slug discovery, live prices, Chainlink RTDS stream, fee rate API, compute_fee_factor())

signals/            → Signal generation
  indicators.py     → Technical indicators (RSI-9, VWAP, BB-20, EMA 9/21, ATR-14, momentum, vol z-score)
  features.py       → Feature engineering (OBI, taker ratio, funding z-score → FeatureVector)
  probability.py    → Weighted ensemble model → P(up) in [0.05, 0.95]

strategy/           → Trading logic
  edge.py           → Edge detection (compare P(up) vs fee-adjusted Polymarket implied odds, threshold-based)
  paper_trader.py   → Paper trading engine (Kelly/fixed sizing, fee-adjusted PnL, settlement, bankroll restoration)

alerts/
  telegram.py       → Telegram bot (interactive commands + edge/trade/settlement/stats alerts)

storage/
  db.py             → SQLite (candles, feature_snapshots, paper_trades, market_snapshots)

deploy/             → Production deployment
  setup.sh          → Automated Hetzner VPS setup (system packages, user, repo, venv, systemd)
  btc-edge.service  → systemd unit file (auto-restart, security hardening)

analyze_trades.py   → Offline trade analysis (signal breakdown, edge buckets, hourly, ML logistic regression)
config.py           → Pydantic Settings loaded from .env
main.py             → Async orchestrator wiring all components, Telegram command handlers, console output
```

## Key Commands

```bash
# Install dependencies
pip install -e .

# Run the tool
python main.py

# Run trade analysis (requires btc_edge.db in working directory)
python analyze_trades.py

# Syntax check all files
python -m py_compile main.py config.py data/models.py data/binance_ws.py data/polymarket.py signals/indicators.py signals/features.py signals/probability.py strategy/edge.py strategy/paper_trader.py alerts/telegram.py storage/db.py

# Hetzner deployment (run as root on fresh Ubuntu 22.04+)
bash deploy/setup.sh

# Update deployed code on Hetzner
ssh root@46.225.27.241
cd /home/btcedge/BTC-tool && sudo -u btcedge git pull origin master && systemctl restart btc-edge

# Service management on Hetzner
systemctl status btc-edge        # Check service status
journalctl -u btc-edge -f        # Follow systemd logs
tail -f /home/btcedge/BTC-tool/btc_edge.log  # Follow app logs
systemctl restart btc-edge       # Restart the service
```

## Configuration

Copy `.env.example` to `.env`. Key settings:

### Telegram
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional, alerts and bot commands disabled if missing

### Strategy Thresholds
- `MIN_EDGE_THRESHOLD` — minimum edge to trigger paper trade (default 0.05 = 5%)
- `MIN_EDGE_DOWN` — minimum edge for DOWN-side trades (default 0.08 = 8%, higher bar because DOWN trades have lower historical win rate)
- `MAX_EDGE` — maximum edge cap; edges above this are rejected as likely model error (default 0.20 = 20%)
- `MAX_SIGNAL_VALUE` — skip trades where any single signal exceeds this magnitude (default 0.45; catches saturated OBI/taker near +-0.5 limits)

### Sizing
- `BET_SIZE_USDC` — fixed bet size per trade (default 50)
- `VIRTUAL_BANKROLL` — starting paper bankroll (default 10000)
- `USE_KELLY` — use half-Kelly sizing instead of fixed (default false)

### Polymarket
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_PASSPHRASE` — Builder Mode credentials for live trading (leave blank for paper-only)
- `POLYMARKET_FEE_RATE` — fee curve rate parameter (default 0.25 for crypto markets)
- `POLYMARKET_FEE_EXPONENT` — fee curve exponent (default 2 for crypto markets)

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
Polymarket API → Implied P(up)  →  edge = P(up) - implied_P(up)
                              ↓ (if |edge| > threshold + safety filters)
                        Paper Trader → simulate bet, log to SQLite
                              ↓
                        Telegram Bot → alert user + interactive commands
```

## Probability Model (v1 — Rule-Based Weighted Ensemble)

```
P(up) = 0.5 + w_obi*OBI + w_taker*taker + w_momentum*momentum + w_rsi*rsi + w_vwap*vwap + w_funding*funding
```

Each signal is normalized to [-0.5, 0.5]:
- **OBI** (order book imbalance): bid/ask volume ratio → [-0.5, 0.5]
- **Taker ratio**: net taker buy/sell ratio → [-0.5, 0.5]
- **Momentum**: 60% of 1m + 40% of 5m momentum, clipped at +-2% → [-0.5, 0.5]
- **RSI(9)**: (rsi - 50) / 100 → [-0.5, 0.5]
- **VWAP deviation**: price vs VWAP, clipped at +-1% → [-0.5, 0.5]
- **Funding rate**: z-score inverted (high funding = bearish) → [-0.5, 0.5]

Final P(up) clamped to [0.05, 0.95]. Default weights: OBI=0.25, taker=0.25, momentum=0.15, RSI=0.15, VWAP=0.10, funding=0.10.

`get_signal_breakdown(features)` returns the normalized value of each signal for logging and debugging.

## Edge Detection & Paper Trading

- Edge = our_P(side) - fee_adjusted_market_P(side), evaluated for both UP and DOWN sides
- **Taker fee model**: Polymarket crypto markets charge fees using a bell-curve formula: `fee_factor = fee_rate * (price * (1 - price))^exponent`. At price=0.50 with crypto defaults (rate=0.25, exp=2): ~1.56% fee. Near-zero at price extremes. The fee-adjusted break-even probability is `p / (1 - fee_factor)` — edges must clear this hurdle to be real after fees.
- Trades when |best_edge| > MIN_EDGE_THRESHOLD (default 5%) **after fee adjustment**

### Safety Filters (applied in order)

1. **Pause gate**: Trading can be paused via `/pause` Telegram command — data collection and settlement continue but no new trades are placed.
2. **Duplicate prevention**: One pending trade per market slug (no duplicate bets on same 5-min window).
3. **Time gate** (120s): Only enters trades in the first 2 minutes of a 5-minute window. After that, the market has already priced in the move and any remaining "edge" is likely stale.
4. **Trend-conflict filter** (0.15%): If BTC has already moved >0.15% in one direction within the current window and the model's signal is in the *opposite* direction, the trade is skipped. Prevents contrarian bets against strong intra-window momentum that the market has correctly priced.
5. **Max edge cap** (default 20%): Edges above `MAX_EDGE` are rejected. If the model claims 20%+ edge over real-money Polymarket odds, the model is more likely wrong than the market. Historical data confirms 20%+ edges win at ~50% — no better than a coin flip.
6. **DOWN-side higher threshold** (default 8%): DOWN trades require a larger edge (`MIN_EDGE_DOWN`) than UP trades. Historical data shows DOWN trades have significantly lower win rate (~33% vs ~64% for UP at 5% threshold).
7. **Signal saturation filter** (default 0.45): When any individual signal exceeds +-`MAX_SIGNAL_VALUE`, the trade is skipped. Extreme readings (e.g. OBI at -0.495) indicate a single noisy input is dominating the model, producing overconfident but unreliable predictions.

### Settlement & PnL

- Settlement uses Chainlink BTC/USD stream via Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`) — the actual resolution source Polymarket uses; Binance spot as fallback if stream is unavailable or stale (>60s old)
- PnL with fees: shares = (size / price) * (1 - fee_factor); WIN = shares - size, LOSS = -size
- Stats tracked: total trades, settled trades, wins, losses, win rate, cumulative PnL, total fees paid, bankroll, ROI
- `restore_bankroll()` handles cross-restart persistence — loads historical PnL from DB and adjusts bankroll on startup

## Telegram Bot

### Commands
- `/status` — BTC price (Binance + Chainlink stream), P(up), market odds, current window, pause state
- `/stats` — Trading performance: total trades, settled, W/L, win rate, P&L, bankroll, ROI
- `/trades` — List pending trades with entry price and size
- `/pause` — Stop placing new trades (data collection and settlement continue)
- `/resume` — Resume placing trades
- `/weights` — Display current probability model weights with ASCII bar chart
- `/analyze` — Full trade analysis: overall stats, edge buckets (5-8%, 8-12%, 12-16%, 16-20%, 20%+), side breakdown (UP vs DOWN), hourly win rate (best/worst UTC hours)
- `/reset` — Two-step confirmation to clear all trade data and reset bankroll
- `/help` — Auto-generated list of available commands

### Alert Types
- **Edge alert**: When edge is detected and trade is placed — includes signal breakdown
- **Trade alert**: Paper trade placement confirmation with side, slug, size, entry price
- **Settlement alert**: Win/loss result with P&L and updated bankroll
- **Stats summary**: Periodic stats (every 30 minutes) and after each window settlement
- **Error alert**: Error notifications

### Implementation Details
- Command listener polls `getUpdates` with 10s long-polling timeout
- Only processes messages from the configured `chat_id`
- Strips `@botname` suffix from commands (e.g. `/status@Matic5m_bot`)
- Graceful degradation: disabled silently if token/chat_id missing or library not installed
- Messages logged at DEBUG level when disabled (prevents Windows cp1252 encoding errors)
- Messages truncated to 4096 chars (Telegram limit)

## Console Output Format

The tool prints clean ASCII to the console (no emojis — Windows cp1252 safe):
- **Buffering phase**: `Buffering: 0/5 candles | BTC: $68,562 | trades: 158 | orderbook: yes`
- **Edge signals**: `>>> EDGE: DOWN btc-updown-5m-... | our=57.0% mkt=48.5% edge=+8.5% fee=1.56% | BTC=$68,562 | [obi=+0.123, taker=-0.045, momentum=+0.089]`
- **Settlement**: `--- WINDOW SETTLED: btc-updown-5m-... | BTC $68544 -> $68544 (+0.00 = UP) [Chainlink Stream]`
- **P&L summary**: `--- P&L: $+818.04 | Win rate: 81% (22/27) | Bankroll: $9999.01`
- **Status heartbeat** (every 30s): `-- Status: BTC $68,679 | P(up)=55.1% | Mkt=50/50 | Trades: 27 (81% win) | P&L: $+36.14`

## Trade Analysis Script (`analyze_trades.py`)

Offline analysis tool that reads from `btc_edge.db`. Also available in-bot via `/analyze` (subset). Run `python analyze_trades.py` for the full report.

**Analyses (8 sections):**
1. **Overall stats** — Total trades, W/L, win rate, total/avg P&L
2. **Signal direction analysis** — Win rate when each signal is bullish vs bearish
3. **Signal strength analysis** — Win rate by strength: weak/medium/strong
4. **Edge bucket analysis** — Win rate by edge size (5-8%, 8-12%, 12-16%, 16-20%, 20%+)
5. **Side analysis** — Win rate UP vs DOWN
6. **Signal agreement analysis** — How many signals agree with trade direction
7. **Time-of-day analysis** — Win rate by UTC hour
8. **ML analysis** — Logistic regression on all 11 features (cross-validated accuracy, ranked feature coefficients, suggested weights if model finds signal)

## Key Design Decisions

- Polymarket 5-min BTC market slugs are deterministic: `btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`
- All Polymarket market data endpoints are free (no auth needed)
- Settlement via Chainlink BTC/USD data stream from Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`) — matches Polymarket's resolution source. Binance spot as fallback. On-chain Chainlink aggregator (`latestRoundData()`) available as secondary fallback but has ~1h heartbeat (unsuitable for 5-min windows).
- Probability model is a weighted ensemble of normalized signals (v1, no ML)
- Polymarket taker fees modelled with bell-curve formula from Maker Rebates Program docs — edge detection and PnL both account for fees so paper results closely approximate real trading
- Builder Mode credentials configurable for future live trading — paper trading works without them
- Settlement happens on 5-min window transitions (detected by slug change in the analysis loop)
- Tool needs ~5 minutes of warmup to buffer 5 closed 1-minute candles before analysis starts
- Startup runs 5 concurrent asyncio tasks: Binance WS, Chainlink RTDS stream, analysis loop (3s cycle), stats loop (30m), Telegram command listener

## Infrastructure

### Hetzner VPS (Production)
- **Server**: matic-tb, IP `46.225.27.241`, CPX22 (3 vCPU, 4GB RAM), Nuremberg DC, Ubuntu 24.04
- **SSH**: `ssh root@46.225.27.241`
- **Service**: systemd `btc-edge` under user `btcedge` at `/home/btcedge/BTC-tool`
- **Deployment**: `deploy/setup.sh` (run as root) — installs packages, creates user, clones repo, sets up venv, installs systemd service
- **Update**: `cd /home/btcedge/BTC-tool && sudo -u btcedge git pull origin master && systemctl restart btc-edge`
- **Logs**: `/home/btcedge/BTC-tool/btc_edge.log` (via systemd `StandardOutput=append`)
- **Hardening**: `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths` limited to app directory

### Local Development
- **Windows**: `C:\Users\Rn5ho\BTC-tool`

## Known Issues & Fixes Applied

- **Windows cp1252 encoding**: Telegram alert messages contain emojis for HTML formatting. When Telegram is disabled, these were previously logged at INFO level causing `UnicodeEncodeError` on Windows consoles. Fixed by logging at DEBUG level and stripping non-ASCII before debug output (`alerts/telegram.py:79-95`).
- **Console spam**: Edge signals were logging every 3-second cycle. Fixed with duplicate trade detection — only logs edge once per market slug when a new paper trade is placed.
- **Heartbeat flooding**: Added 30-second interval between status heartbeats instead of logging every cycle.
- **Chainlink stale settlement prices**: The on-chain Chainlink aggregator (`latestRoundData()`) has a ~1h heartbeat, returning identical prices for window start and end within 5-min windows. This caused start==end every time, always resolving as UP, inflating win rates to ~82%. Fixed by streaming Chainlink BTC/USD via Polymarket RTDS WebSocket — the same data source Polymarket uses for market resolution. Binance spot is the fallback if the stream is unavailable.
- **Late-window entries**: The analysis loop could place trades at any point during a 5-min window (e.g., 3 minutes in). By then the market has priced in the move and the "edge" is stale. Fixed with a time gate: trades only allowed in the first 120 seconds of each window.
- **Contrarian bets against strong trends**: The model's mean-reverting signals (OBI from dip-buyers, VWAP "oversold") would produce UP signals during BTC crashes, while the market correctly priced DOWN at 70-80%. The model would see a large "edge" and bet UP against the trend. Fixed with a trend-conflict filter: if BTC has moved >0.15% in one direction within the window and the signal is opposite, the trade is skipped.
- **Duplicate log lines on Hetzner**: `setup_logging()` added both a StreamHandler and FileHandler(`btc_edge.log`). Under systemd, stdout is already captured to the same file via `StandardOutput=append`, causing every line to appear twice. Fixed by only adding FileHandler when stdout is a TTY (interactive).
- **httpx log spam**: Telegram's `getUpdates` polling logged every 10 seconds via httpx at INFO level. Fixed by setting httpx/httpcore loggers to WARNING.
- **P&L/bankroll inconsistency across restarts**: Bankroll was reset to VIRTUAL_BANKROLL on each restart, ignoring accumulated P&L. Fixed with `restore_bankroll()` which loads historical PnL from DB on startup.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info — emojis only in Telegram HTML messages)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)

## Current Status & Performance

Running 24/7 on Hetzner VPS as systemd service. Profitable with asymmetric pricing — winners pay more than losers cost even when win rate is below 50%. Fee model verified correct (matches Polymarket docs).

Use `/stats` in Telegram or `/analyze` for current performance numbers. Run `python analyze_trades.py` on the server for the full 8-section analysis with ML feature importance.

## Potential Next Steps

1. **ML-based probability model** — if `/analyze` shows any signal has predictive power, replace the hand-tuned ensemble with a logistic regression or gradient-boosted model trained on collected data.
2. **Live trading** — only pursue if the model demonstrates consistent >52% win rate after fees. Builder Mode credentials and fee calculation are already configured — remaining work is the order placement layer via Polymarket CLOB API.
