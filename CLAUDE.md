# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — monitors Binance BTC price data, uses a trained ML model (RandomForestClassifier, 53.9% accuracy) to predict 5-minute BTC direction, and trades on Polymarket's binary UP/DOWN markets. Deployed on Hetzner VPS in Helsinki (65.21.178.90) running 24/7. Live trading enabled via py-clob-client with adaptive tiered early exit selling.

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (spot klines + depth + aggTrades, futures funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices, no auth needed)
- Polymarket RTDS WebSocket for Chainlink BTC/USD stream (settlement price — matches Polymarket's resolution source)
- py-clob-client (Polymarket CLOB trading — real orders via GTC market buys)
- scikit-learn (ML model inference — RandomForestClassifier loaded from pickle)
- SQLite via aiosqlite (persistence)
- python-telegram-bot v21+ (interactive bot with commands)
- pydantic-settings (config from .env)
- numpy (indicators + ML feature extraction), no pandas at runtime

## Architecture

```
data/           → Data collection layer
  models.py     → Shared dataclasses (Candle, OrderBook, AggTrade, FundingInfo, FeatureVector, PaperTrade, PolymarketMarket, PolymarketOrderBook)
  binance_ws.py → Binance WebSocket client (kline_1m, depth20@100ms, aggTrade streams, futures funding)
  polymarket.py → Polymarket Gamma/CLOB client (slug discovery, live prices + order books, Chainlink RTDS stream)

signals/        → Signal generation
  indicators.py → Technical indicators (RSI-9/14, VWAP, BB-20, EMA 5/9/13/21, ATR-14, momentum, vol z-score)
  features.py   → Feature engineering (OBI, taker ratio, funding z-score → FeatureVector with 12 fields)
  probability.py→ Rule-based weighted ensemble model → P(up) in [0.05, 0.95] (LEGACY — still available as fallback)
  ml_probability.py → ML model wrapper → P(up) in [0.05, 0.95] using trained RandomForestClassifier with 44 features (ACTIVE)
  regime.py     → Market regime detector (trending_up/trending_down/ranging via EMA cross + BB position + multi-window momentum)

strategy/       → Trading logic
  edge.py       → Edge detection (compare P(up) vs Polymarket implied odds; uses ask price for entry; supports always-trade mode)
  paper_trader.py → Paper trading engine (fixed/Kelly/adaptive sizing, PnL with Polymarket fees, settlement)
  live_trader.py  → Live trading engine (real Polymarket CLOB orders via py-clob-client, GTC market buys, independent bankroll/sizing/settlement, adaptive tiered early exit selling)

alerts/
  telegram.py   → Telegram bot (edge alerts, live trade notifications, live settlements, interactive commands — paper trades excluded)

storage/
  db.py         → SQLite (candles, feature_snapshots, paper_trades, live_trades with outcome/pnl tracking, market_snapshots with spread columns, historical price lookup)

models/         → Trained ML model artifacts
  best_model.pkl  → Serialized RandomForestClassifier (RF_d3, trained 2026-02-28)
  scaler.pkl      → StandardScaler fitted on training data
  dataset.npz     → Training dataset (34,100 samples x 44 features)

deploy/         → Hetzner VPS deployment
  btc-edge.service → systemd service file (runs as btcedge user, auto-restart)
  setup.sh      → Automated server setup script (Ubuntu/Debian)

ml_pipeline.py       → ML training pipeline (downloads Binance history, builds dataset, trains models)
simulate_compounding.py → Monte Carlo simulation for bet sizing strategies
analyze_clob.py      → CLOB API analysis utility (real trade data, P&L from wallet)
backfill_outcomes.py → One-off script to correct historical trade outcomes using Gamma API resolutions
config.py            → Pydantic Settings loaded from .env
main.py              → Async orchestrator wiring all components, Telegram command handlers, console output, maker fill sync, Gamma settlement

docs/plans/     → Design documents and implementation plans

```

## Key Commands

```bash
# Install dependencies
pip install -e .

# Run the tool
python main.py

# Retrain ML model (downloads ~120 days of Binance klines, takes ~25 min)
python ml_pipeline.py

# Run Monte Carlo simulations for bet sizing strategies
python simulate_compounding.py

# Syntax check all files
python -m py_compile main.py config.py data/models.py data/binance_ws.py data/polymarket.py signals/indicators.py signals/features.py signals/probability.py signals/ml_probability.py signals/regime.py strategy/edge.py strategy/paper_trader.py strategy/live_trader.py alerts/telegram.py storage/db.py
```

### Hetzner VPS — Helsinki, Finland (65.21.178.90)

```bash
# SSH into server (key at ~/.ssh/id_ed25519)
ssh root@65.21.178.90

# Check service status
systemctl status btc-edge

# View logs (live)
tail -f /home/btcedge/BTC-tool/btc_edge.log

# Force restart (service has 90s stop timeout — use kill for fast restart)
systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge

# Deploy files from local machine (from BTC-tool directory)
scp config.py main.py root@65.21.178.90:/home/btcedge/BTC-tool/
scp data/models.py data/polymarket.py root@65.21.178.90:/home/btcedge/BTC-tool/data/
scp strategy/edge.py strategy/paper_trader.py strategy/live_trader.py root@65.21.178.90:/home/btcedge/BTC-tool/strategy/
scp signals/ml_probability.py root@65.21.178.90:/home/btcedge/BTC-tool/signals/
scp alerts/telegram.py root@65.21.178.90:/home/btcedge/BTC-tool/alerts/
scp storage/db.py root@65.21.178.90:/home/btcedge/BTC-tool/storage/
scp models/best_model.pkl models/scaler.pkl root@65.21.178.90:/home/btcedge/BTC-tool/models/
scp .env root@65.21.178.90:/home/btcedge/BTC-tool/

# Download DB for analysis
scp root@65.21.178.90:/home/btcedge/BTC-tool/btc_edge.db .

# Check live trading status in logs
ssh root@65.21.178.90 "tail -c 20000 /home/btcedge/BTC-tool/btc_edge.log | strings | grep -iE '(LIVE|FILLED|FAILED)'"

# Reset paper trading data
ssh root@65.21.178.90 "cd /home/btcedge/BTC-tool && source venv/bin/activate && python -c \"import sqlite3; c=sqlite3.connect('btc_edge.db'); c.execute('DELETE FROM paper_trades'); c.commit(); print('Cleared', c.total_changes)\""
```

## Telegram Bot Commands

The bot (`@BTC5mBot`) supports interactive commands:

| Command | Description |
|---------|-------------|
| `/status` | Current BTC price (Binance + Chainlink), model type (ML/RB), P(up), market odds, window info |
| `/stats` | Comprehensive stats: USDC balance, bankroll, ROI, W/L, win rate, P&L, volume, session info, compact paper line |
| `/trades` | List pending (unsettled) live positions + DB unsettled trades |
| `/recent [N]` | Last N settled live trades (default 5, max 10) with side, entry price, P&L, time ago |
| `/today` | Today's performance (UTC): trade count, W/L, P&L, best/worst trade |
| `/pause` | Stop placing new trades (data collection continues, pending trades still settle) |
| `/resume` | Resume placing trades |
| `/weights` | Show model info (ML type + settings, or rule-based weights) |
| `/regime` | Show market regime (EMA cross + BB position analysis) |
| `/analyze` | Run live trade analysis: breakdown by side, hour, taker/maker/early-exit, and entry price buckets |
| `/spread` | Show live order book spreads (bid/ask/spread/sizes) for current market |

Periodic stats (every 60 min) include live trading info. Trade placement sends one combined alert (side, entry price, confidence, edge, order ID) — only for live trades, not paper/exploration. Live trade WIN/LOSS/EARLY_EXIT settlement alerts sent via Telegram. Skip notifications sent when a window is skipped (with reason). Paper trade notifications excluded from Telegram.

## Configuration

Copy `.env.example` to `.env`. Key settings:

```env
# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Strategy
MIN_EDGE_THRESHOLD=0.05        # min positive edge for classic mode (ignored in always-trade)
MAX_EDGE_THRESHOLD=0.18        # cap — edges above this are rejected as model error
BET_SIZE_USDC=5                # fixed bet size (used when SIZING_STRATEGY=fixed)
VIRTUAL_BANKROLL=20            # starting paper bankroll
USE_KELLY=false                # half-Kelly sizing (legacy, use SIZING_STRATEGY instead)

# ML Model
USE_ML_MODEL=true              # true=ML model, false=rule-based ensemble
ALWAYS_TRADE=true              # true=trade every window, false=only trade when edge > threshold
SIZING_STRATEGY=adaptive       # fixed | kelly | adaptive
MIN_CONFIDENCE=0.015           # min |P(up)-0.5| to trade (backtested sweet spot: 56.9% WR)

# Live trading (real money on Polymarket)
LIVE_TRADING=true
POLYMARKET_PRIVATE_KEY=...     # EOA private key (hex, no 0x prefix) from Rabby
POLYMARKET_FUNDER_ADDRESS=...  # Proxy wallet from polymarket.com deposit settings
MAX_LIVE_BET_USDC=5.0         # Hard safety cap (must accommodate 5-token CLOB minimum)
# CLOB_PROXY=socks5://127.0.0.1:1080  # Not needed from Finland (no geoblock)

# Polymarket fees
POLYMARKET_FEE_RATE=0.25
POLYMARKET_FEE_EXPONENT=2

# Regime detection
REGIME_TREND_THRESHOLD=0.30    # strength threshold for trending classification
REGIME_FLIP_THRESHOLD=0.35     # flip signal to trend-following when |strength| >= this
REGIME_FLIP_LIVE=true          # enable regime flip for live trades (not just paper)

# Loss streak guard
STREAK_PAUSE_THRESHOLD=3       # consecutive same-side losses to trigger pause
STREAK_PAUSE_WINDOWS=2         # number of 5-min windows to pause (~10 min)

# Confidence dampening — shrink P(up) toward 0.5 to counter model overconfidence
CONFIDENCE_DAMPEN=0.6          # 1.0 = no dampening, 0.0 = always 50%

# Adaptive early exit — tiered thresholds by entry price
EARLY_EXIT_THRESHOLD_LOW=0.60  # entry < 0.35 (~15% WR, aggressive exit)
EARLY_EXIT_THRESHOLD_MID=0.65  # entry 0.35-0.50 (~25% WR)
EARLY_EXIT_THRESHOLD_HIGH=0.95 # entry >= 0.50 (~55% WR, conservative)

# Hour blacklist (UTC hours to skip trading)
BLACKLIST_HOURS=2              # comma-separated, e.g. "2,3,4"
```

## Safety Filters

Applied in `strategy/edge.py` and `main.py` before every trade:

1. **Min confidence** (`MIN_CONFIDENCE=0.015`): Skip when |P(up) - 0.5| below threshold
2. **Entry price filter**: Hard reject outside 0.25-0.65. Exploration range 0.25-0.35 uses minimum size, tagged `trade_tag="exploration"`
3. **Time gate** (`_MAX_ENTRY_SECONDS=60`): Only enter in first 60 seconds of 5-min window (data: 60-120s entries had 24.5% WR, -$53 on 53 trades)
4. **Hour blacklist** (`BLACKLIST_HOURS`): Skip configured UTC hours (default: 02:00)
5. **Loss streak guard** (`STREAK_PAUSE_THRESHOLD=3`): After 3 consecutive LOSS on the same side, pause that side for 2 windows (~10 min). Early exits break the chain. Only tracks taker trades. Sends Telegram alert on trigger. First trade after pause tagged `post_streak`.
6. **Trend-conflict filter** (`_TREND_CONFLICT_PCT=0.15`): Skip if BTC moved >0.15% against signal direction
7. **Regime flip** (`REGIME_FLIP_THRESHOLD=0.35`): When regime strength >= 0.35 (trending), flip both paper and live trades to bet WITH the trend. Tagged `trade_tag='regime_flip'`. Data: 82-100% trend continuation at this threshold. Controlled by `REGIME_FLIP_LIVE`.
8. **One trade per window**: No duplicate bets on same market slug
9. **Pause**: `/pause` stops new trades while data collection continues
10. **Flat close = DOWN**: Polymarket resolves ties as DOWN. Uses strict `>` (not `>=`)

## Adaptive Early Exit

Dedicated 1-second monitoring loop (`_early_exit_loop()` → `_monitor_early_exit()`) sells live positions when the bid price reaches a threshold, locking in profit before settlement risk.

**Tiered thresholds by entry price** (deployed 2026-03-02):

| Entry Price | Exit Threshold | Win Rate | Rationale |
|-------------|---------------|----------|-----------|
| < 0.35 | 0.60 | ~15% | Lottery tickets — sell on any spike |
| 0.35 - 0.50 | 0.65 | ~25% | Low WR — exit aggressively |
| >= 0.50 | 0.95 | ~55% | Conservative, proven threshold |

- **First touch**: Sell immediately when `bid >= threshold` (no hold-confirmation — bid spikes on losers last only 5-8 seconds)
- **Monitoring window**: Entire 5-min window after a 10-second grace period (avoids stale book data right after entry)
- **Minimum depth**: `>= 20 tokens` on bid side
- **Token balance query**: Before selling, `get_token_balance()` queries actual CLOB conditional token balance (avoids "not enough balance" errors from computed vs actual token count mismatch)
- **Retry logic**: Up to 3 attempts with 5-second cooldown between each (replaces old permanent `exit_failed` flag that blocked all retries after one failure)
- **DB outcome**: `"EARLY_EXIT"` — skipped by unsettled trade queries
- **Asymmetric payoff**: Each rescue saves avg +$4.44, each regretted exit costs avg -$0.61 (7:1 ratio)
- **`get_exit_threshold(entry_price)`** in `LiveTrader` returns the appropriate threshold
- **Data**: Validated on 640 trades (live + paper) and 45 CLOB ground-truth trades

## Danger Zone: py-clob-client Landmines

These three bugs will silently break live trading if the workarounds are removed:

1. **Rounding bug** (monkey-patched in `LiveTrader.initialize()`): Library swaps maker/taker rounding for BUY orders. Our patch computes taker first (2 dec), derives maker via `Decimal` multiplication (4 dec). Uses `round_down` for maker — `round_up` overshoots by 0.0001.

2. **SELL amount = token count, NOT USDC**: `MarketOrderArgs(amount, side=SELL)` expects token count. For BUY, amount = USDC. Passing USDC value for SELL causes partial fills leaving tokens behind.

3. **5-token minimum**: ALL CLOB markets require `minimum_order_size: 5` (tokens, not USDC). `place_order()` auto-bumps to `max(5.5 × price, $3.50)`. Hard floor `$1.00` for any marketable order.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — ML model falls back to rule-based if unavailable; Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)
- Paper trades stored as dicts in `_pending_trades` (keyed by market slug), persisted to SQLite

## Detailed Reference Docs

See `docs/` for in-depth reference material (read on-demand when working in specific areas):

- **`docs/live-trading.md`** — CLOB architecture, all py-clob-client quirks (8 items), safety controls, DB schema, independent live trading, early exit selling (trigger/token tracking/settlement), live results
- **`docs/ml-model.md`** — 44 ML features, training pipeline, model comparison table, best/worst hours, confidence thresholds, rule-based fallback formula
- **`docs/trading-logic.md`** — Data flow diagram, edge detection modes, bet sizing strategies (fixed/kelly/adaptive with multiplier details), paper trading fee model, window lifecycle & settlement (Gamma API, two-tier), startup sequence
- **`docs/known-issues.md`** — All 32 historical bug fixes and known issues with descriptions and resolutions
- **`docs/plans/`** — Design documents and implementation plans (adaptive early exit design + plan)
