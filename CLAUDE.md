# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — a tool that generates directional probability estimates for 5-minute BTC price movements, compares them to Polymarket's implied odds, and paper trades when mispricing is detected. Telegram alerts for notifications.

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (price, orderbook, trades, funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices)
- SQLite via aiosqlite (persistence)
- python-telegram-bot v21+ (alerts)
- pydantic-settings (config from .env)
- numpy (indicators), no pandas at runtime

## Architecture

```
data/           → Data collection layer
  models.py     → Shared dataclasses (Candle, OrderBook, AggTrade, FeatureVector, PaperTrade, PolymarketMarket)
  binance_ws.py → Binance WebSocket client (klines, depth20@100ms, aggTrades, funding)
  polymarket.py → Polymarket Gamma/CLOB client (deterministic slug discovery, live prices)

signals/        → Signal generation
  indicators.py → Technical indicators (RSI-9, VWAP, BB-20, EMA 9/21, ATR-14, momentum, vol z-score)
  features.py   → Feature engineering (OBI, taker ratio, funding z-score → FeatureVector)
  probability.py→ Weighted ensemble model → P(up) in [0.05, 0.95]

strategy/       → Trading logic
  edge.py       → Edge detection (compare P(up) vs Polymarket implied odds)
  paper_trader.py → Paper trading engine (Kelly/fixed sizing, PnL, settlement)

alerts/
  telegram.py   → Telegram notifications (edge alerts, trades, settlements, stats)

storage/
  db.py         → SQLite (candles, feature_snapshots, paper_trades, market_snapshots)

config.py       → Pydantic Settings loaded from .env
main.py         → Async orchestrator wiring all components
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
- `W_OBI`, `W_TAKER`, `W_MOMENTUM`, `W_RSI`, `W_VWAP`, `W_FUNDING` — probability model weights

## Data Flow

Binance WS → Rolling State → Feature Engineering → P(up) Model → Edge Detection → Paper Trader → SQLite + Telegram

## Key Design Decisions

- Polymarket 5-min BTC market slugs are deterministic: `btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`
- All Polymarket market data endpoints are free (no auth needed)
- Probability model is a weighted ensemble of normalized signals (v1, no ML)
- Paper trading only — no real money integration yet
- Settlement happens on 5-min window transitions by comparing BTC start/end prices

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — Telegram disabled silently if unconfigured
