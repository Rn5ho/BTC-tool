# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — a real-time tool that monitors Binance BTC price data (spot + futures), uses a trained ML model to predict 5-minute BTC direction, and paper trades on Polymarket's binary UP/DOWN markets every 5-minute window. Telegram bot for alerts and interactive commands.

**Status:** Deployed on Hetzner VPS (46.225.27.241) running 24/7 as a systemd service. ML model (RandomForestClassifier, 53.9% test accuracy on 34,100 samples) replaced the original rule-based model. Running in "always-trade" mode with $20 paper bankroll and hybrid adaptive bet sizing. **Live trading enabled and profitable** via py-clob-client with $5 max bet cap, routing CLOB API through SOCKS5 proxy (SSH tunnel → user's Mullvad VPN → Slovenia) to bypass Polymarket geoblock (Germany blocked). First 9 hours of live trading (2026-03-01): 43 real trades, +$5.24 profit (+26.2% ROI), $20 → $25.24.

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

strategy/       → Trading logic
  edge.py       → Edge detection (compare P(up) vs Polymarket implied odds; uses ask price for entry; supports always-trade mode)
  paper_trader.py → Paper trading engine (fixed/Kelly/adaptive sizing, PnL with Polymarket fees, settlement)
  live_trader.py  → Live trading engine (real Polymarket CLOB orders via py-clob-client, GTC market buys, SOCKS5 proxy, auto-sell winners)

alerts/
  telegram.py   → Telegram bot (edge alerts, trade notifications, settlements, interactive commands)

storage/
  db.py         → SQLite (candles, feature_snapshots, paper_trades, market_snapshots with spread columns, historical price lookup)

models/         → Trained ML model artifacts
  best_model.pkl  → Serialized RandomForestClassifier (RF_d3, trained 2026-02-28)
  scaler.pkl      → StandardScaler fitted on training data
  dataset.npz     → Training dataset (34,100 samples x 44 features)

deploy/         → Hetzner VPS deployment
  btc-edge.service → systemd service file (runs as btcedge user, auto-restart)
  setup.sh      → Automated server setup script (Ubuntu/Debian)

ml_pipeline.py       → ML training pipeline (downloads Binance history, builds dataset, trains models)
simulate_compounding.py → Monte Carlo simulation for bet sizing strategies
config.py            → Pydantic Settings loaded from .env
main.py              → Async orchestrator wiring all components, Telegram command handlers, console output
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
python -m py_compile main.py config.py data/models.py data/binance_ws.py data/polymarket.py signals/indicators.py signals/features.py signals/probability.py signals/ml_probability.py strategy/edge.py strategy/paper_trader.py alerts/telegram.py storage/db.py
```

### Hetzner VPS (46.225.27.241)

```bash
# SSH into server (key at ~/.ssh/id_ed25519)
ssh root@46.225.27.241

# Check service status
systemctl status btc-edge

# View logs (live)
tail -f /home/btcedge/BTC-tool/btc_edge.log

# Force restart (service has 90s stop timeout — use kill for fast restart)
systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge

# Deploy files from local machine (from BTC-tool directory)
scp config.py main.py root@46.225.27.241:/home/btcedge/BTC-tool/
scp data/models.py data/polymarket.py root@46.225.27.241:/home/btcedge/BTC-tool/data/
scp strategy/edge.py strategy/paper_trader.py root@46.225.27.241:/home/btcedge/BTC-tool/strategy/
scp signals/ml_probability.py root@46.225.27.241:/home/btcedge/BTC-tool/signals/
scp alerts/telegram.py root@46.225.27.241:/home/btcedge/BTC-tool/alerts/
scp storage/db.py root@46.225.27.241:/home/btcedge/BTC-tool/storage/
scp models/best_model.pkl models/scaler.pkl root@46.225.27.241:/home/btcedge/BTC-tool/models/
scp .env root@46.225.27.241:/home/btcedge/BTC-tool/

# Download DB for analysis
scp root@46.225.27.241:/home/btcedge/BTC-tool/btc_edge.db .

# Reset paper trading data
ssh root@46.225.27.241 "cd /home/btcedge/BTC-tool && source venv/bin/activate && python -c \"import sqlite3; c=sqlite3.connect('btc_edge.db'); c.execute('DELETE FROM paper_trades'); c.commit(); print('Cleared', c.total_changes)\""
```

## Telegram Bot Commands

The bot (`@BTC5mBot`) supports interactive commands:

| Command | Description |
|---------|-------------|
| `/status` | Current BTC price (Binance + Chainlink), model type (ML/RB), P(up), market odds, window info |
| `/stats` | Trading performance: total trades, win rate, P&L, bankroll, ROI |
| `/trades` | List pending (unsettled) paper trades |
| `/reset` | Clear all paper trades from DB and reset bankroll (requires double-tap within 30s) |
| `/budget` | Show current bankroll and bet size |
| `/budget 200` | Set bankroll to $200 |
| `/pause` | Stop placing new trades (data collection continues, pending trades still settle) |
| `/resume` | Resume placing trades |
| `/weights` | Show model info (ML type + settings, or rule-based weights) |
| `/regime` | Show market regime (EMA cross + BB position analysis) |
| `/analyze` | Run trade analysis: breakdown by edge bucket, side, and hour |
| `/spread` | Show live order book spreads (bid/ask/spread/sizes) for current market |
| `/balance` | Show USDC balance, session stats, pending claims (live trading only) |
| `/livetrades` | Show live trade stats (session + all-time from DB) |

Periodic stats (every 30 min) include live trading info: USDC balance, session fills, all-time totals. Live trade WIN/LOSS settlement alerts are sent via Telegram with estimated profit.

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
CLOB_PROXY=socks5://127.0.0.1:1080  # SOCKS5 proxy for geoblock bypass

# Polymarket fees
POLYMARKET_FEE_RATE=0.25
POLYMARKET_FEE_EXPONENT=2

# Hour blacklist (UTC hours to skip trading)
BLACKLIST_HOURS=2              # comma-separated, e.g. "2,3,4"
```

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
                        Paper Trader → simulate bet, log to SQLite
                              ↓
                        Telegram Alert → notify user
```

## ML Probability Model (v2 — Trained RandomForestClassifier)

**Active model** — controlled by `USE_ML_MODEL=true` in .env.

### Training (ml_pipeline.py)
- **Dataset**: 34,100 labeled 5-minute windows from 170,947 candles (119 days: Nov 2025 - Feb 2026)
- **Source**: Binance historical klines (monthly ZIP archives from data.binance.vision + recent daily klines)
- **Labels**: Binary (UP=49.9%, DOWN=50.1%) — balanced, no class weighting needed
- **Split**: Time-series 80/20 (no future leakage)
- **Cross-validation**: 5-fold time-series CV

### 44 Features
1. **Momentum** (5): 1m, 3m, 5m, 10m, 20m price returns
2. **RSI** (2): RSI-9, RSI-14
3. **Price vs averages** (6): VWAP deviation, BB position, BB width, EMA crosses (9/21, 5/13), price vs EMA9, price vs EMA21
4. **Volatility** (4): ATR-14, ATR%, recent 5-bar volatility, volatility ratio (5-bar/20-bar)
5. **Volume** (5): volume z-score, volume trend, taker ratio (1m, 3m, 5m)
6. **Candle patterns** (5): upper/lower wick ratio, body ratio, close vs open, avg body ratio (5-bar)
7. **Lag features** (6): previous direction (5m, 10m, 15m, 20m), streak length, mean reversion
8. **Time** (8): cyclical hour (sin/cos), minute, session flags (Asia/Europe/US), cyclical day-of-week (sin/cos)
9. **Microstructure** (2): high-low range %, close position in range

### Results — ALL MODELS STATISTICALLY SIGNIFICANT (p < 0.001)

| Model | CV (5-fold) | Test (6,820) | Simulated P&L ($5 flat) |
|-------|------------|-------------|------------------------|
| RF_d3 | 52.5% | **53.9%** | **+$2,066 ($87/day)** |
| RF_d5 | **52.6%** | 53.6% | +$1,889 ($80/day) |
| HistGBT | 52.3% | 53.5% | +$1,790 ($76/day) |
| GBT_v3 | 52.6% | 52.9% | +$1,446 ($61/day) |
| LogReg | 52.2% | 52.8% | +$1,357 ($57/day) |

**Deployed model**: RF_d3 (best test accuracy 53.9%, lowest overfitting gap)

### Top Features (by importance)
rsi_14 (6.9%), vwap_deviation (6.8%), bb_position (6.4%), ema_cross_9_21 (5.4%), rsi_9 (5.4%), momentum_20m (4.0%), taker_ratio_3m (3.6%)

### Best/Worst Hours (UTC)
- **Best**: 14:00 (62.5%), 19:00 (57.2%), 09:00 (56.9%), 08:00 (56.6%)
- **Worst**: 03:00 (47.9%), 04:00 (48.6%)

### Confidence Thresholding
- threshold=0.02: 43% of windows traded, **56.8% WR**, $73/day
- threshold=0.05: 13% of windows traded, **57.4% WR**, $24/day

### Live Integration (signals/ml_probability.py)
- `MLProbabilityModel` class — drop-in replacement for `ProbabilityModel`
- `predict(features)` — fallback using 12 FeatureVector fields (zero-pads missing 32 features)
- `predict_from_candles(candles, window_start_ts)` — **preferred**: computes all 44 features from candle history
- main.py uses `predict_from_candles()` when ML model is active
- Falls back to rule-based model if model files not found or scikit-learn not installed

## Rule-Based Probability Model (v1 — Legacy Fallback)

Available when `USE_ML_MODEL=false`. Original model from before ML training.

```
P_raw = 0.5 + w_obi*OBI + w_taker*taker + w_momentum*momentum + w_rsi*rsi + w_vwap*vwap + w_funding*funding
P(up) = 0.5 + CONFIDENCE_DAMPEN * (P_raw - 0.5)
```

Each signal normalized to [-0.5, 0.5]. Default weights: OBI=0.05, taker=0.25, momentum=0.05, RSI=0.10, VWAP=0.10, funding=0.10, volume_zscore=0.15, regime=0.20. Confidence dampening=0.6 (shrinks toward 50%). Performance: 49.7% WR over 2,736 trades — essentially a coin flip.

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
2. **Max edge cap** (`MAX_EDGE_THRESHOLD=0.18`): Edges above 18% are rejected as model error
3. **Entry price cap**: Reject entry prices above 0.90 or below 0.10 (thin book protection)
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

## Live Trading (Real Money via py-clob-client)

Live trading places real GTC market buy orders on Polymarket alongside paper trades.

### Architecture
- `strategy/live_trader.py`: `LiveTrader` class wrapping py-clob-client (synchronous lib)
- All CLOB API calls wrapped in `asyncio.to_thread()` to avoid blocking the event loop
- Uses `signature_type=2` (Polymarket proxy wallet from Rabby browser extension)
- Order type: GTC market buy via `MarketOrderArgs`+`create_market_order`
- CLOB 5-token minimum: amount auto-bumped to `5 × entry_price` (~$2.50 at typical prices)
- Retry with exponential backoff on 425 "Too Early" errors (matching engine restarts)
- Auto-sell winning tokens: **DISABLED** — Polymarket's claim/redeem system is unreliable. User claims manually on polymarket.com.
- Candle prefetch from Binance REST API eliminates 30-min buffering delay on restart

### py-clob-client Quirks & Patches
- **Rounding bug** (CRITICAL): `get_market_order_amounts` for BUY rounds taker_amount to `round_config.amount` (4-5 decimals) but CLOB requires max 2. The library has the rounding reversed for BUY market orders (maker and taker swapped). Fixed by monkey-patching `OrderBuilder.get_market_order_amounts` in `initialize()` to compute taker first (rounded to 2 dec), then derive maker (up to 4 dec). **Must use `round_down` for maker** — floating point (e.g. `4.36 * 0.63 = 2.7468000000000004`) causes `round_up` to overshoot by 0.0001, which CLOB rejects.
- **5-token minimum**: ALL CLOB markets have `minimum_order_size: 5` (tokens, not USDC). At $0.50 per token = $2.50 minimum per trade. The Polymarket website uses a different mechanism for small orders. `place_order()` auto-bumps amount to `5 × entry_price`. `MAX_LIVE_BET_USDC=5.0` to accommodate.
- **$1 minimum for marketable orders**: After the rounding patch, the effective maker amount can drop below $1. Explicit `$1.00` floor added in `place_order()`.
- **FOK fails on thin books**: FOK orders require immediate full fill. 5-min binary markets are thin. Use GTC instead.
- **Balance API**: `get_balance_allowance()` requires `BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)` — no default.
- **pydantic-settings vs os.environ**: pydantic-settings reads .env but does NOT set OS env vars. Use `settings.clob_proxy` not `os.environ.get("CLOB_PROXY")`.

### Geoblock Bypass
- Hetzner VPS is in Germany — Polymarket blocks Germany
- Solution: SSH reverse tunnel from user's local machine (Mullvad VPN → Slovenia)
  - User runs: `ssh -R 1080 root@46.225.27.241` (keeps tunnel alive)
  - VPS py-clob-client routes through `socks5://127.0.0.1:1080`
- Monkey-patches py-clob-client's internal `httpx.Client` to use proxy (NOT global env vars)
  - Only CLOB API calls go through proxy; Binance WS and Polymarket data streams are unaffected
  - Requires `socksio` package for httpx SOCKS5 support
- **Proxy watchdog**: Background task checks SOCKS5 port every 2 minutes
  - Auto-pauses live trading if proxy goes down
  - Auto-resumes when proxy comes back
  - Sends Telegram alerts on state changes

### Safety Controls
- Hard cap: `MAX_LIVE_BET_USDC=5.0` (accommodates 5-token minimum even at $0.70+ prices)
- Minimum: `5 × entry_price` per trade (CLOB 5-token minimum, ~$2.50 at $0.50)
- Floor: `$1.00` minimum per order (CLOB rejects marketable orders below $1)
- Balance check before each order
- All paper trading safety filters apply (time gate, hour blacklist, trend conflict, confidence)
- `/pause` stops both paper and live trading
- Proxy watchdog auto-pauses on tunnel loss

### DB Schema
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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### Deploy Commands (live trading specific)
```bash
# Deploy live trader
scp strategy/live_trader.py root@46.225.27.241:/home/btcedge/BTC-tool/strategy/

# Check live trading status in logs
ssh root@46.225.27.241 "tail -c 20000 /home/btcedge/BTC-tool/btc_edge.log | strings | grep -iE '(LIVE|proxy|FILLED|FAILED)'"

# Check proxy health
ssh root@46.225.27.241 "curl -s --max-time 5 --socks5-hostname 127.0.0.1:1080 https://ipinfo.io/json"
```

## Window Lifecycle & Settlement

1. **Window detection**: Slugs are deterministic (`btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`). Analysis loop detects transitions every 3-second cycle.
2. **Settlement source**: Chainlink BTC/USD via Polymarket RTDS WebSocket (the actual resolution source). Binance spot as fallback.
3. **Settlement decoupled from market discovery**: Window transitions detected via `get_current_slug()` BEFORE the Gamma API call.
4. **Startup recovery**: After 5-minute buffering, stale unsettled trades from previous sessions are settled using historical candle data.

## Startup Sequence

1. Load config, check for ML model availability
2. Initialize ML model (or fall back to rule-based)
3. Initialize EdgeDetector with always_trade setting
4. Initialize DB, PaperTrader (with sizing_strategy), TelegramAlerter
5. Start Polymarket aiohttp session
6. Initialize LiveTrader (if LIVE_TRADING=true): derive API creds, apply SOCKS5 proxy monkey-patch, patch rounding config
7. Prefetch 35 candles from Binance REST API (skip 30-min buffering wait)
8. Launch 6 concurrent asyncio tasks:
   - **Binance WS**: Streams kline_1m, depth20, aggTrade, futures funding
   - **Chainlink RTDS**: Streams BTC/USD from Polymarket's data service
   - **Analysis loop**: Initializes window tracking, settles stale trades, then runs 3-second poll cycle
   - **Stats loop**: Periodic stats report every 30 minutes
   - **Telegram command listener**: Long-polls for incoming /commands
   - **Proxy watchdog**: Checks SOCKS5 tunnel every 2 min, auto-pauses/resumes live trading

## Console Output Format

Clean ASCII output (Windows cp1252 safe):
- **Buffering**: `Buffering: 0/5 candles | BTC: $63,999 | trades: 238 | orderbook: yes`
- **ML trade signals**: `>>> ML UP btc-updown-5m-... | our=53.5% mkt=50.0% edge=+2.7% conf=3.5% fee=1.56% spread=0.0200 mid=0.500`
- **Rule-based signals**: `>>> RB DOWN btc-updown-5m-... | our=57.0% mkt=48.5% edge=+8.5%`
- **Settlement**: `--- WINDOW SETTLED: btc-updown-5m-... | BTC $63982 -> $64001 (+19.00 = UP) [Chainlink Stream]`
- **P&L**: `--- P&L: $+3.04 | Win rate: 60% (3/5) | Bankroll: $23.04`
- **Heartbeat** (30s): `-- Status [ML]: BTC $63,999 | P(up)=53.5% | Mkt=50/50 | spread=0.0200 | Trades: 5 (60% win)`

## Deployment (Hetzner VPS)

Runs 24/7 on Hetzner VPS at `46.225.27.241`:

- **User**: `btcedge` (dedicated, non-root)
- **Directory**: `/home/btcedge/BTC-tool`
- **Service**: `btc-edge.service` via systemd (`Restart=always`, `RestartSec=10`)
- **Logs**: `/home/btcedge/BTC-tool/btc_edge.log` (also via `journalctl -u btc-edge`)
- **SSH key**: `~/.ssh/id_ed25519` (ed25519, comment "btc-tool-deploy")
- **Branch**: `claude/add-polymarket-btc-markets-kZRWJ`
- **Python venv**: `/home/btcedge/BTC-tool/venv` (includes scikit-learn, numpy, aiohttp, etc.)
- **Hardening**: `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths=/home/btcedge/BTC-tool`

## Known Issues & Fixes Applied

1. **Edge detector betting wrong side** (CRITICAL — fixed): Picked largest *absolute* edge instead of largest *positive* edge, causing systematic wrong-direction bets.

2. **Trades never settling after restart** (fixed): `_current_slug` started as `None`, skipping settlement. Fixed with startup recovery + `_settle_stale_trades()`.

3. **Settlement gated behind market discovery** (fixed): Decoupled settlement from Gamma API by checking slug transitions first.

4. **Late-window entries** (fixed): Time gate `_MAX_ENTRY_SECONDS=120` prevents stale-edge entries.

5. **Contrarian bets against strong trends** (fixed): Trend-conflict filter `_TREND_CONFLICT_PCT=0.15`.

6. **Chainlink stale settlement prices** (fixed): Switched from on-chain aggregator (1h heartbeat) to Polymarket RTDS WebSocket stream.

7. **Windows cp1252 encoding** (fixed): Telegram emojis at DEBUG level only, ASCII-safe console.

8. **Binance historical data microsecond timestamps** (fixed in ml_pipeline.py): Binance CSV files use 16-digit microsecond timestamps. Fixed with `if ts > 1_000_000_000_000_000: ts = ts // 1000`.

9. **systemctl restart hangs** (known): The service has a 90s SIGTERM timeout. Use `systemctl kill -s SIGKILL btc-edge` for fast restarts.

10. **py-clob-client maker rounding overshoot** (fixed): Floating point `4.36 * 0.63 = 2.7468000000000004` caused `round_up` to produce `2.7469`, rejected by CLOB. Fixed by using `round_down` for maker amount.

11. **425 "Too Early" on order placement** (fixed): Matching engine restarts cause transient 425 errors. Fixed with exponential backoff retry (3 attempts, 3s/6s/12s delays).

12. **Auto-sell fails on resolved markets** (known — disabled): CLOB order book closes when 5-min markets resolve. Selling via CLOB after resolution doesn't work. Polymarket's own claim/redeem system is also unreliable. Auto-sell disabled; user claims manually on polymarket.com.

13. **SCP to wrong path shadows packages** (fixed): `scp alerts/telegram.py root@host:/home/btcedge/BTC-tool/` creates `telegram.py` in project root, shadowing the `telegram` package. Always SCP to the full subdirectory path.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — ML model falls back to rule-based if unavailable; Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)
- Paper trades stored as dicts in `_pending_trades` (keyed by market slug), persisted to SQLite

## Live Trading Results (2026-03-01, first 9 hours)

From actual Polymarket CSV export:
- **Deposited**: $20.00
- **43 buy trades**, $100.33 total (capital recycled ~5x)
- **11 sells** (auto-sell of winners): $45.87 recovered
- **47 redeems** (manual claims): $59.70 recovered
- **Final balance**: $25.24 → **+$5.24 profit (+26.2% ROI)**
- **Fill rate**: 54% (38/70 attempts) — improved to ~95%+ after fixes
- **Failure breakdown**: 18/32 fixed (rounding, proxy, GTC, precision), 14/32 fixed by raising cap to $5

## Potential Next Steps

- **On-chain token redemption**: Implement `redeemPositions()` via web3.py on the CTF contract to auto-claim winning tokens without relying on Polymarket's unreliable UI. Needs conditionId from market data, MATIC for gas.
- **Early exits**: Sell tokens before window ends when price hits ~$0.95+ (market outcome nearly certain). Could capture profit without waiting for settlement.
- **Model retraining**: Retrain periodically as market dynamics shift. Pipeline is ready (`ml_pipeline.py`), takes ~25 min.
- **Feature expansion**: Liquidation data, funding rate momentum, cross-exchange flows, order book depth imbalance at multiple levels.
- **Scale up bet sizing**: With proven profitability, increase capital and bet sizes. Current adaptive sizing uses 2% of bankroll as base.
- **Polymarket API docs**: docs.polymarket.com has detailed API reference for fee formulas and CLOB integration.
