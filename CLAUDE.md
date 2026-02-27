# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — a real-time tool that monitors Binance BTC price data (spot + futures), computes directional probability estimates for 5-minute price movements, compares them against Polymarket's implied odds, and paper trades when mispricing is detected. Telegram bot for alerts and interactive commands.

**Status:** Fully functional and deployed on Hetzner VPS (46.225.27.241) running 24/7 as a systemd service. Paper trading works end-to-end with Polymarket fee model. Interactive Telegram bot with commands for monitoring and management. No real-money trading yet — user has expressed interest in adding live trading via py-clob-client with small trial capital (~$20).

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (spot klines + depth + aggTrades, futures funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices, no auth needed)
- Polymarket RTDS WebSocket for Chainlink BTC/USD stream (settlement price — matches Polymarket's resolution source)
- SQLite via aiosqlite (persistence)
- python-telegram-bot v21+ (interactive bot with commands)
- pydantic-settings (config from .env)
- numpy (indicators), no pandas at runtime

## Architecture

```
data/           → Data collection layer
  models.py     → Shared dataclasses (Candle, OrderBook, AggTrade, FundingInfo, FeatureVector, PaperTrade, PolymarketMarket)
  binance_ws.py → Binance WebSocket client (kline_1m, depth20@100ms, aggTrade streams, futures funding)
  polymarket.py → Polymarket Gamma/CLOB client (slug discovery, live prices, Chainlink RTDS stream)

signals/        → Signal generation
  indicators.py → Technical indicators (RSI-9, VWAP, BB-20, EMA 9/21, ATR-14, momentum, vol z-score)
  features.py   → Feature engineering (OBI, taker ratio, funding z-score → FeatureVector)
  probability.py→ Weighted ensemble model → P(up) in [0.05, 0.95]

strategy/       → Trading logic
  edge.py       → Edge detection (compare P(up) vs Polymarket implied odds, positive-edge only)
  paper_trader.py → Paper trading engine (Kelly/fixed sizing, PnL with Polymarket fees, settlement)

alerts/
  telegram.py   → Telegram bot (edge alerts, trade notifications, settlements, interactive commands)

storage/
  db.py         → SQLite (candles, feature_snapshots, paper_trades, market_snapshots, historical price lookup)

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
python -m py_compile main.py config.py data/models.py data/binance_ws.py data/polymarket.py signals/indicators.py signals/features.py signals/probability.py strategy/edge.py strategy/paper_trader.py alerts/telegram.py storage/db.py
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
sudo -u btcedge git pull origin claude/continue-5m-btc-tool-nx4a9
sudo systemctl restart btc-edge

# Reset paper trading data
sudo -u btcedge sqlite3 /home/btcedge/BTC-tool/btc_edge.db "DELETE FROM paper_trades;"
```

## Telegram Bot Commands

The bot (`@BTC5mBot`) supports interactive commands:

| Command | Description |
|---------|-------------|
| `/status` | Current BTC price (Binance + Chainlink), model P(up), market odds, window info |
| `/stats` | Trading performance: total trades, win rate, P&L, bankroll, ROI |
| `/trades` | List pending (unsettled) paper trades |
| `/reset` | Clear all paper trades from DB and reset bankroll to initial value |
| `/budget` | Show current bankroll and bet size |
| `/budget 200` | Set bankroll to $200 (also resets initial_bankroll for ROI calculation) |
| `/help` | List available commands |

Commands are dispatched via long-polling (`get_updates`) in a dedicated asyncio task. Handlers accept an optional args string for commands like `/budget 200`.

## Configuration

Copy `.env.example` to `.env`. Key settings:
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — optional, alerts disabled if missing
- `MIN_EDGE_THRESHOLD` — minimum positive edge to trigger paper trade (default 0.05 = 5%)
- `MAX_EDGE_THRESHOLD` — maximum edge cap; edges above this are rejected as model overconfidence (default 0.18 = 18%)
- `BET_SIZE_USDC` — fixed bet size per trade (default 5)
- `VIRTUAL_BANKROLL` — starting paper bankroll (default 100)
- `USE_KELLY` — use half-Kelly sizing instead of fixed (default false)
- `CONFIDENCE_DAMPEN` — shrink P(up) toward 50% to counter overconfidence (default 0.6; 1.0 = no dampening)
- `BLACKLIST_HOURS` — comma-separated UTC hours to skip trading (default "2" — 02:00 UTC has 37% WR)
- `MAX_SIGNAL_VALUE` — skip trades when any single signal exceeds this (default 0.45 = near ±0.5 saturation)
- `W_OBI`, `W_TAKER`, `W_MOMENTUM`, `W_RSI`, `W_VWAP`, `W_FUNDING`, `W_VOLUME_ZSCORE`, `W_REGIME` — probability model weights (must sum to 1.0)

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
                        Safety Filters (time gate + hour blacklist + trend conflict + saturation)
                              ↓
                        Paper Trader → simulate bet, log to SQLite
                              ↓
                        Telegram Alert → notify user
```

## Probability Model (v3 — Regime-Adaptive Weighted Ensemble)

```
signals = normalize(OBI, taker, momentum, RSI, VWAP, funding, volume_direction, regime)
weights = regime_adaptive_reweight(base_weights, regime_strength)
P_raw = 0.5 + sum(weights[i] * signals[i])
P(up) = 0.5 + CONFIDENCE_DAMPEN * (P_raw - 0.5)
```

### Regime-Adaptive Weighting

Signals are categorized as **trend-following** (taker, momentum, regime, RSI) or **mean-reverting** (OBI, VWAP, funding). When the regime signal indicates a clear trend (|regime| > 0.15), the model dynamically adjusts weights:

- **Trend-following signals**: boosted up to **1.5x** their base weight
- **Mean-reverting signals**: reduced down to **0.4x** their base weight
- **Neutral signals** (volume_direction): unchanged

The scaling is linear with regime strength (0.15→0.5 maps to 0→1.0 adaptation factor). Weights are re-normalized to sum to 1.0 after adjustment.

**Rationale**: In trending markets, mean-reverting signals produce contrarian traps — e.g., OBI shows dip-buying during a sell-off, VWAP says "oversold." These push the model toward UP when DOWN is correct. Instead of filtering out these bad trades (trade less), the adaptive weights fix the model's direction (trade smarter). Every wrong UP bet becomes a correct DOWN bet.

### Signal Normalization

Each signal is normalized to [-0.5, 0.5]:
- **OBI** (order book imbalance): bid/ask volume ratio → [-0.5, 0.5]
- **Taker ratio**: net taker buy/sell ratio → [-0.5, 0.5]
- **Momentum**: 60% of 1m + 40% of 5m momentum, clipped at ±2% → [-0.5, 0.5]
- **RSI(9)**: (rsi - 50) / 100 → [-0.5, 0.5] (trend-following: high RSI = bullish at 5-min scale)
- **VWAP deviation**: price vs VWAP, clipped at ±1% → [-0.5, 0.5]
- **Funding rate**: z-score inverted (high funding = bearish) → [-0.5, 0.5]
- **Volume direction**: `vol_magnitude * taker_direction` — high volume amplifies taker pressure direction. Below-average volume → near-zero signal. Replaces old non-directional volume z-score which blindly pushed toward UP when volume was high.
- **Regime**: 60% EMA cross (EMA9 vs EMA21) + 40% BB position — multi-window trend memory

Confidence dampening (default 0.6) shrinks predictions toward 50% to counter the model's systematic overconfidence (calibration analysis on 2,736 trades showed 10-20% overestimation at every probability bucket).

Final P(up) clamped to [0.05, 0.95]. Default weights: OBI=0.05, taker=0.25, momentum=0.05, RSI=0.10, VWAP=0.10, funding=0.10, volume_direction=0.15, regime=0.20.

## Edge Detection & Safety Filters

The edge detector (`strategy/edge.py`) evaluates both sides and only considers **positive** edges:

1. **Edge calculation**: For each side, `edge = our_P(side) - market_P(side)`. Only sides where we think the market underprices (positive edge) are candidates.
2. **Side selection**: Pick the side with the larger positive edge. If neither side has positive edge, no trade.
3. **Threshold**: Only trade if `edge > MIN_EDGE_THRESHOLD` (default 5%).
3b. **Max edge cap** (`MAX_EDGE_THRESHOLD = 0.18`): Edges above 18% are rejected. Analysis of 2,736 trades showed 18-20% edge trades are net negative, and 20%+ trades win only 31%. Lowered from 0.20 to 0.18 based on data showing $501 P&L at 18% cap vs $445 at 20%.
4. **Time gate** (`_MAX_ENTRY_SECONDS = 120`): Only enter trades in the first 2 minutes of a 5-minute window. After that, the market has already priced in the move and any remaining "edge" is likely stale.
5. **Hour blacklist** (`BLACKLIST_HOURS`): Skip trading during configured UTC hours. Default: 02:00 UTC (37% win rate, -$114 P&L over 108 trades in analysis). Configurable via comma-separated env var.
6. **Trend-conflict filter** (`_TREND_CONFLICT_PCT = 0.15`): If BTC has already moved >0.15% in one direction within the current window and the model's signal is the opposite direction, the trade is skipped. Prevents contrarian bets against strong intra-window momentum.
7. **Signal saturation filter** (`MAX_SIGNAL_VALUE = 0.45`): If any single signal is near the ±0.5 limits, the trade is skipped. The model is likely overreacting to a single noisy input rather than seeing a real multi-signal pattern.
8. **One trade per window**: Only one pending trade per market slug (no duplicate bets on same 5-min window).

## Paper Trading & Fee Model

Paper trading engine (`strategy/paper_trader.py`) simulates Polymarket's actual fee structure:

- **Polymarket fee**: 2% on net profit for winning trades (`PROFIT_FEE_RATE = 0.02`)
- **No fee on losses**: Full stake is lost on losing trades
- **PnL**: WIN = `size * (1 - entry_price) / entry_price - fee`, LOSS = `-size`
- **Sizing**: Fixed ($5/trade default) or half-Kelly criterion, never exceeding bankroll
- **Kelly formula**: `f* = (p*b - q) / b`, half-Kelly with 5% of bankroll cap
- **Default config**: $100 bankroll, $5 bet size

## Window Lifecycle & Settlement

1. **Window detection**: Slugs are deterministic (`btc-updown-5m-{unix_ts}` where `unix_ts = now - (now % 300)`). The analysis loop detects transitions every 3-second cycle.
2. **Settlement source**: Chainlink BTC/USD via Polymarket RTDS WebSocket (the actual resolution source). Binance spot as fallback.
3. **Settlement is decoupled from market discovery**: Window transitions are detected via `get_current_slug()` BEFORE the Gamma API call. This ensures settlement fires even if the Gamma API is slow/fails.
4. **Startup recovery**: On restart, after the 5-minute buffering phase, window tracking is initialized and any stale unsettled trades from previous sessions are settled using historical candle data from the DB (`get_btc_price_at()`). Trades with no matching candle data are voided (0 PnL).

## Startup Sequence

1. Initialize DB (create tables if needed)
2. Create PaperTrader, TelegramAlerter
3. Start Polymarket aiohttp session
4. Launch 5 concurrent asyncio tasks:
   - **Binance WS**: Streams kline_1m, depth20, aggTrade, futures funding
   - **Chainlink RTDS**: Streams BTC/USD from Polymarket's data service
   - **Analysis loop**: Buffers 5 candles (~5 min), initializes window tracking, settles stale trades, then runs 3-second poll cycle
   - **Stats loop**: Periodic stats report every 30 minutes
   - **Telegram command listener**: Long-polls for incoming /commands

## Console Output Format

The tool prints clean ASCII to the console (no emojis — Windows cp1252 safe):
- **Buffering phase**: `Buffering: 0/5 candles | BTC: $68,562 | trades: 158 | orderbook: yes`
- **Edge signals**: `>>> EDGE: DOWN btc-updown-5m-... | our=57.0% mkt=48.5% edge=+8.5%`
- **Settlement**: `--- WINDOW SETTLED: btc-updown-5m-... | BTC $68544 -> $68562 (+18.00 = UP) [Chainlink Stream]`
- **P&L summary**: `--- P&L: $+3.04 | Win rate: 60% (3/5) | Bankroll: $103.04`
- **Status heartbeat** (every 30s): `-- Status: BTC $68,679 | P(up)=55.1% | Mkt=50/50 | Trades: 5 (60% win)`

## Deployment (Hetzner VPS)

The bot runs 24/7 on a Hetzner VPS at `46.225.27.241`:

- **User**: `btcedge` (dedicated, non-root)
- **Directory**: `/home/btcedge/BTC-tool`
- **Service**: `btc-edge.service` via systemd (`Restart=always`, `RestartSec=10`)
- **Logs**: `/home/btcedge/BTC-tool/btc_edge.log` (also via `journalctl -u btc-edge`)
- **Hardening**: `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths=/home/btcedge/BTC-tool`
- **Setup**: `deploy/setup.sh` automates user creation, repo clone, venv setup, service install
- **Branch**: Currently tracking `claude/continue-5m-btc-tool-nx4a9`

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

9. **Model fighting trends — wrong-direction bets** (fixed): In trending markets, mean-reverting signals (OBI dip-buying, VWAP "oversold") pushed the model toward UP during sell-offs. The model would bet UP and lose; the correct DOWN bet was missed. Fixed with regime-adaptive weighting: in strong trends, trend-following signals (taker, momentum, regime, RSI) are boosted up to 1.5x while mean-reverting signals (OBI, VWAP, funding) are reduced to 0.4x. Same trade volume, better directional accuracy.

10. **Non-directional volume signal** (fixed): `volume_zscore` was 15% of the model weight but had no directional information — high volume blindly pushed toward UP regardless of whether buying or selling dominated. Replaced with `volume_direction = vol_magnitude * taker_direction`: high volume + sellers = bearish, high volume + buyers = bullish, low volume = near-zero.

11. **DOWN side penalty removed** (fixed): `MIN_EDGE_DOWN = 0.08` required DOWN trades to have 8% edge (vs 5% for UP), blocking potentially profitable DOWN bets. Removed — with better directional accuracy from regime-adaptive weights, both sides are treated equally.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info — emojis only in Telegram HTML messages)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)
- Paper trades stored as dicts in `_pending_trades` (keyed by market slug), persisted to SQLite

## Potential Next Steps

- **Live trading**: User wants to integrate real Polymarket trading via py-clob-client with ~$20 trial capital. Would require CLOB API integration with wallet signing.
- **Validate regime-adaptive weights**: Monitor win rate by side and regime strength after deploying the adaptive model. Compare UP/DOWN performance in trending vs ranging periods.
- **Hour scheduling**: Consider expanding blacklist to other weak hours (11:00=42.5% WR, 19:00-21:00=44-46% WR) once more data confirms the pattern.
- **Friday filter**: Only 1 Friday in sample (38.1% WR, -$98) — collect more data before adding a day-of-week filter.
- **Model improvements**: ML-based probability model, more features (liquidation data, funding rate momentum, cross-exchange flows)
- **Backtesting**: Replay historical data to validate signal weights, dampening factor, and regime-adaptive multipliers
- **Bankroll persistence**: On restart, restore bankroll from DB (initial + cumulative PnL) rather than resetting to config value
