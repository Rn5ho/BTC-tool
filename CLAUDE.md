# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — a real-time tool that monitors Binance BTC price data (spot + futures), computes directional probability estimates for 5-minute price movements, compares them against Polymarket's implied odds, and paper trades when mispricing is detected. Telegram alerts for notifications.

**Status:** Fully functional. Paper trading works end-to-end. Tested on Windows (user runs from `C:\Users\Rn5ho\BTC-tool`). No real-money trading yet — user has expressed interest in adding live trading via Rabby wallet with small trial capital (~$20).

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (spot klines + depth + aggTrades, futures funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices, no auth needed)
- Polymarket RTDS WebSocket for Chainlink BTC/USD stream (settlement price — matches Polymarket's resolution source)
- SQLite via aiosqlite (persistence)
- python-telegram-bot v21+ (alerts, optional)
- pydantic-settings (config from .env)
- numpy (indicators), no pandas at runtime

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
  edge.py       → Edge detection (compare P(up) vs fee-adjusted Polymarket implied odds, threshold-based)
  paper_trader.py → Paper trading engine (Kelly/fixed sizing, fee-adjusted PnL, settlement)

alerts/
  telegram.py   → Telegram notifications (edge alerts, trades, settlements, stats)

storage/
  db.py         → SQLite (candles, feature_snapshots, paper_trades, market_snapshots)

config.py       → Pydantic Settings loaded from .env
main.py         → Async orchestrator wiring all components, console output formatting
```

## Key Commands

```bash
# Install dependencies
pip install -e .

# Run the tool
python main.py

# Syntax check all files
python -m py_compile main.py config.py data/models.py data/binance_ws.py data/polymarket.py signals/indicators.py signals/features.py signals/probability.py strategy/edge.py strategy/paper_trader.py alerts/telegram.py storage/db.py
```

## Configuration

Copy `.env.example` to `.env`. Key settings:
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional, alerts disabled if missing
- `MIN_EDGE_THRESHOLD` — minimum edge to trigger paper trade (default 0.05 = 5%)
- `MIN_EDGE_DOWN` — minimum edge for DOWN-side trades (default 0.08 = 8%, higher bar because DOWN trades have lower historical win rate)
- `MAX_EDGE` — maximum edge cap; edges above this are rejected as likely model error (default 0.20 = 20%)
- `MAX_SIGNAL_VALUE` — skip trades where any single signal exceeds this magnitude (default 0.45; catches saturated OBI/taker near +-0.5 limits)
- `BET_SIZE_USDC` — fixed bet size per trade (default 50)
- `VIRTUAL_BANKROLL` — starting paper bankroll (default 10000)
- `USE_KELLY` — use half-Kelly sizing instead of fixed (default false)
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_PASSPHRASE` — Builder Mode credentials for live trading (leave blank for paper-only)
- `POLYMARKET_FEE_RATE` — fee curve rate parameter (default 0.25 for crypto markets)
- `POLYMARKET_FEE_EXPONENT` — fee curve exponent (default 2 for crypto markets)
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
                              ↓ (if |edge| > threshold)
                        Paper Trader → simulate bet, log to SQLite
                              ↓
                        Telegram Alert → notify user
```

## Probability Model (v1 — Rule-Based Weighted Ensemble)

```
P(up) = 0.5 + w_obi*OBI + w_taker*taker + w_momentum*momentum + w_rsi*rsi + w_vwap*vwap + w_funding*funding
```

Each signal is normalized to [-0.5, 0.5]:
- **OBI** (order book imbalance): bid/ask volume ratio → [-0.5, 0.5]
- **Taker ratio**: net taker buy/sell ratio → [-0.5, 0.5]
- **Momentum**: 60% of 1m + 40% of 5m momentum, clipped at ±2% → [-0.5, 0.5]
- **RSI(9)**: (rsi - 50) / 100 → [-0.5, 0.5]
- **VWAP deviation**: price vs VWAP, clipped at ±1% → [-0.5, 0.5]
- **Funding rate**: z-score inverted (high funding = bearish) → [-0.5, 0.5]

Final P(up) clamped to [0.05, 0.95]. Default weights: OBI=0.25, taker=0.25, momentum=0.15, RSI=0.15, VWAP=0.10, funding=0.10.

## Edge Detection & Paper Trading

- Edge = our_P(side) - fee_adjusted_market_P(side), evaluated for both UP and DOWN sides
- **Taker fee model**: Polymarket crypto markets charge fees using a bell-curve formula: `fee_factor = fee_rate * (price * (1 - price))^exponent`. At price=0.50 with crypto defaults (rate=0.25, exp=2): ~1.56% fee. Near-zero at price extremes. The fee-adjusted break-even probability is `p / (1 - fee_factor)` — edges must clear this hurdle to be real after fees.
- Trades when |best_edge| > MIN_EDGE_THRESHOLD (default 5%) **after fee adjustment**
- **Time gate**: Only enters trades in the first 120 seconds (2 min) of a 5-minute window. After that, the market has already priced in the move and any remaining "edge" is likely stale.
- **Trend-conflict filter**: If BTC has already moved >0.15% in one direction within the current window and the model's signal is in the *opposite* direction, the trade is skipped. Prevents contrarian bets against strong intra-window momentum that the market has correctly priced.
- **Max edge cap** (default 20%): Edges above `MAX_EDGE` are rejected. If the model claims 20%+ edge over real-money Polymarket odds, the model is more likely wrong than the market. Historical data confirms 20%+ edges win at ~50% — no better than a coin flip.
- **DOWN-side higher threshold** (default 8%): DOWN trades require a larger edge (`MIN_EDGE_DOWN`) than UP trades. Historical data shows DOWN trades have significantly lower win rate (~33% vs ~64% for UP at 5% threshold).
- **Signal saturation filter** (default 0.45): When any individual signal exceeds +-`MAX_SIGNAL_VALUE`, the trade is skipped. Extreme readings (e.g. OBI at -0.495) indicate a single noisy input is dominating the model, producing overconfident but unreliable predictions.
- One pending trade per market slug (no duplicate bets on same 5-min window)
- Settlement uses Chainlink BTC/USD stream via Polymarket RTDS WebSocket (the actual resolution source); Binance spot as fallback
- PnL with fees: shares = (size / price) * (1 - fee_factor); WIN = shares - size, LOSS = -size
- Stats tracked: total trades, win rate, cumulative PnL, total fees paid, bankroll, ROI

## Console Output Format

The tool prints clean ASCII to the console (no emojis — Windows cp1252 safe):
- **Buffering phase**: `Buffering: 0/5 candles | BTC: $68,562 | trades: 158 | orderbook: yes`
- **Edge signals**: `>>> EDGE: DOWN btc-updown-5m-... | our=57.0% mkt=48.5% edge=+8.5% fee=1.56%`
- **Settlement**: `--- WINDOW SETTLED: btc-updown-5m-... | BTC $68544 -> $68544 (+0.00 = UP)`
- **P&L summary**: `--- P&L: $+818.04 | Win rate: 81% (22/27) | Bankroll: $9999.01`
- **Status heartbeat** (every 30s): `-- Status: BTC $68,679 | P(up)=55.1% | Mkt=50/50 | Trades: 27 (81% win)`

## Key Design Decisions

- Polymarket 5-min BTC market slugs are deterministic: `btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`
- All Polymarket market data endpoints are free (no auth needed)
- Settlement via Chainlink BTC/USD data stream from Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`) — matches Polymarket's resolution source. Binance spot as fallback.
- Probability model is a weighted ensemble of normalized signals (v1, no ML)
- Polymarket taker fees modelled with bell-curve formula from Maker Rebates Program docs — edge detection and PnL both account for fees so paper results closely approximate real trading
- Builder Mode credentials configurable for future live trading — paper trading works without them
- Paper trading only — no real money integration yet (order placement layer still needed)
- Settlement happens on 5-min window transitions
- Tool needs ~5 minutes of warmup to buffer 5 closed 1-minute candles before analysis starts
- Startup runs 5 concurrent asyncio tasks: binance WS, Chainlink RTDS stream, analysis loop (3s cycle), stats loop (30m), Telegram command listener

## Known Issues & Fixes Applied

- **Windows cp1252 encoding**: Telegram alert messages contain emojis for HTML formatting. When Telegram is disabled, these were previously logged at INFO level causing `UnicodeEncodeError` on Windows consoles. Fixed by logging at DEBUG level and stripping non-ASCII before debug output (`alerts/telegram.py:79-87`).
- **Console spam**: Edge signals were logging every 3-second cycle. Fixed with duplicate trade detection — only logs edge once per market slug when a new paper trade is placed.
- **Heartbeat flooding**: Added 30-second interval between status heartbeats instead of logging every cycle.
- **Chainlink stale settlement prices**: The on-chain Chainlink aggregator (`latestRoundData()`) has a ~1h heartbeat, returning identical prices for window start and end within 5-min windows. This caused start==end every time, always resolving as UP, inflating win rates to ~82%. Fixed by streaming Chainlink BTC/USD via Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`) — the same data source Polymarket uses for market resolution. Binance spot is the fallback if the stream is unavailable.
- **Late-window entries**: The analysis loop could place trades at any point during a 5-min window (e.g., 3 minutes in). By then the market has priced in the move and the "edge" is stale. Fixed with a time gate: trades only allowed in the first 120 seconds of each window.
- **Contrarian bets against strong trends**: The model's mean-reverting signals (OBI from dip-buyers, VWAP "oversold") would produce UP signals during BTC crashes, while the market correctly priced DOWN at 70-80%. The model would see a large "edge" and bet UP against the trend. Fixed with a trend-conflict filter: if BTC has moved >0.15% in one direction within the window and the signal is opposite, the trade is skipped.
- **Duplicate log lines on Hetzner**: `setup_logging()` added both a StreamHandler and FileHandler(`btc_edge.log`). Under systemd, stdout is already captured to the same file via `StandardOutput=append`, causing every line to appear twice. Fixed by only adding FileHandler when stdout is a TTY (interactive).
- **httpx log spam**: Telegram's `getUpdates` polling logged every 10 seconds via httpx at INFO level. Fixed by setting httpx/httpcore loggers to WARNING.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info — emojis only in Telegram HTML messages)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)

## Current Status & Performance

After ~115 settled trades on Hetzner, win rate is **48.7%** with **+$36.14 P&L** (+8.5% ROI, $5 bets). Profitable despite sub-50% win rate because winners pay more than losers cost at asymmetric prices. Fee model is verified correct (matches Polymarket docs). The P&L/bankroll inconsistency across restarts has been fixed (`restore_bankroll()`).

An analysis script (`analyze_trades.py`) is available to diagnose which signals help/hurt. Run `python analyze_trades.py` in the same directory as `btc_edge.db`. It includes a logistic regression ML model to test if any feature combination is learnable.

## Infrastructure

- **User runs on Windows** (`C:\Users\Rn5ho\BTC-tool`) — local development.
- **Hetzner VPS**: Server name **matic-tb**, IP `46.225.27.241`, CPX22 (3 vCPU, 4GB RAM), Nuremberg DC, Ubuntu 24.04. SSH: `ssh root@46.225.27.241`. Deployed as systemd service `btc-edge` under user `btcedge` at `/home/btcedge/BTC-tool`.
- Deployment via `deploy/setup.sh` (run as root) — clones repo, sets up venv, installs deps, configures systemd.
- To update after code changes: SSH in, `cd /home/btcedge/BTC-tool && sudo -u btcedge git pull origin master && systemctl restart btc-edge`.

## Potential Next Steps (Priority Order)

1. ~~**Run `analyze_trades.py`**~~ — use `/analyze` command in Telegram or run `python analyze_trades.py` on the server.
2. ~~**Deploy to Hetzner VPS**~~ — DONE. Running as `btc-edge` systemd service.
3. ~~**Enhance Telegram bot**~~ — DONE. Commands: `/status`, `/stats`, `/trades`, `/pause`, `/resume`, `/weights`, `/analyze`, `/reset`, `/help`.
4. **ML-based probability model** — if `/analyze` shows any signal has predictive power, replace the hand-tuned ensemble with a logistic regression or gradient-boosted model trained on collected data.
5. **Live trading** — only pursue if the model demonstrates consistent >52% win rate after fees. Builder Mode credentials and fee calculation are already configured — remaining work is the order placement layer.
