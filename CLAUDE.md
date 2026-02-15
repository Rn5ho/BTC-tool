# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — a real-time tool that monitors Binance BTC price data (spot + futures), computes directional probability estimates for 5-minute price movements, compares them against Polymarket's implied odds, and paper trades when mispricing is detected. Telegram alerts for notifications.

**Status:** Fully functional. Paper trading works end-to-end. Tested on Windows (user runs from `C:\Users\Rn5ho\BTC-tool`). No real-money trading yet — user has expressed interest in adding live trading via Rabby wallet with small trial capital (~$20).

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (spot klines + depth + aggTrades, futures funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices, no auth needed)
- Chainlink BTC/USD oracle on Ethereum mainnet (settlement price source, via public RPC)
- SQLite via aiosqlite (persistence)
- python-telegram-bot v21+ (alerts, optional)
- pydantic-settings (config from .env)
- numpy (indicators), no pandas at runtime

## Architecture

```
data/           → Data collection layer
  models.py     → Shared dataclasses (Candle, OrderBook, AggTrade, FundingInfo, FeatureVector, PaperTrade, PolymarketMarket)
  binance_ws.py → Binance WebSocket client (kline_1m, depth20@100ms, aggTrade streams, futures funding)
  polymarket.py → Polymarket Gamma/CLOB client (deterministic slug discovery, live prices, Chainlink oracle)

signals/        → Signal generation
  indicators.py → Technical indicators (RSI-9, VWAP, BB-20, EMA 9/21, ATR-14, momentum, vol z-score)
  features.py   → Feature engineering (OBI, taker ratio, funding z-score → FeatureVector)
  probability.py→ Weighted ensemble model → P(up) in [0.05, 0.95]

strategy/       → Trading logic
  edge.py       → Edge detection (compare P(up) vs Polymarket implied odds, threshold-based)
  paper_trader.py → Paper trading engine (Kelly/fixed sizing, PnL, settlement)

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
- `BET_SIZE_USDC` — fixed bet size per trade (default 50)
- `VIRTUAL_BANKROLL` — starting paper bankroll (default 10000)
- `USE_KELLY` — use half-Kelly sizing instead of fixed (default false)
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

- Edge = our_P(side) - market_P(side), evaluated for both UP and DOWN sides
- Trades when |best_edge| > MIN_EDGE_THRESHOLD (default 5%)
- One pending trade per market slug (no duplicate bets on same 5-min window)
- Settlement uses Chainlink BTC/USD price (the actual Polymarket resolution source), with Binance spot as fallback
- PnL: WIN = size * (1 - entry_price) / entry_price, LOSS = -size
- Stats tracked: total trades, win rate, cumulative PnL, bankroll, ROI

## Console Output Format

The tool prints clean ASCII to the console (no emojis — Windows cp1252 safe):
- **Buffering phase**: `Buffering: 0/5 candles | BTC: $68,562 | trades: 158 | orderbook: yes`
- **Edge signals**: `>>> EDGE: DOWN btc-updown-5m-... | our=57.0% mkt=48.5% edge=+8.5%`
- **Settlement**: `--- WINDOW SETTLED: btc-updown-5m-... | BTC $68544 -> $68544 (+0.00 = UP)`
- **P&L summary**: `--- P&L: $+818.04 | Win rate: 81% (22/27) | Bankroll: $9999.01`
- **Status heartbeat** (every 30s): `-- Status: BTC $68,679 | P(up)=55.1% | Mkt=50/50 | Trades: 27 (81% win)`

## Key Design Decisions

- Polymarket 5-min BTC market slugs are deterministic: `btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`
- All Polymarket market data endpoints are free (no auth needed)
- Chainlink BTC/USD price feed (contract `0xF403...E88c`) for settlement — matches Polymarket's resolution oracle
- Probability model is a weighted ensemble of normalized signals (v1, no ML)
- Paper trading only — no real money integration yet
- Settlement happens on 5-min window transitions
- Tool needs ~5 minutes of warmup to buffer 5 closed 1-minute candles before analysis starts
- Startup runs 3 concurrent asyncio tasks: binance WS, analysis loop (3s cycle), stats loop (30m)

## Known Issues & Fixes Applied

- **Windows cp1252 encoding**: Telegram alert messages contain emojis for HTML formatting. When Telegram is disabled, these were previously logged at INFO level causing `UnicodeEncodeError` on Windows consoles. Fixed by logging at DEBUG level and stripping non-ASCII before debug output (`alerts/telegram.py:79-87`).
- **Console spam**: Edge signals were logging every 3-second cycle. Fixed with duplicate trade detection — only logs edge once per market slug when a new paper trade is placed.
- **Heartbeat flooding**: Added 30-second interval between status heartbeats instead of logging every cycle.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info — emojis only in Telegram HTML messages)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)

## Potential Next Steps

- **Live trading**: User wants to integrate real Polymarket trading via Rabby wallet with ~$20 trial capital. Would require py-clob-client or direct CLOB API integration with wallet signing.
- **Model improvements**: ML-based probability model, more features (liquidation data, funding rate momentum, cross-exchange flows)
- **Backtesting**: Replay historical data to validate signal weights
- **Signal weight optimization**: Use collected SQLite data to tune weights based on actual win rates per signal
