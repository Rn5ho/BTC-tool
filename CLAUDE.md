# CLAUDE.md

## Project Overview

BTC Polymarket 5-Minute Edge Finder — monitors Binance BTC price data, uses a trained ML model (RandomForestClassifier, 53.9% accuracy) to predict 5-minute BTC direction, and trades on Polymarket's binary UP/DOWN markets. Deployed on Hetzner VPS in Helsinki (65.21.178.90) running 24/7. Live trading enabled via py-clob-client with adaptive tiered early exit selling.

## Win Rate Metrics — How to Read Performance

Early Exit (EE) is an intentional profit-locking mechanism, not a rescue system. It secures wins that might flip in the last seconds before settlement. Most EE trades are **regrets** (would have won at settlement anyway) — that's by design. Three metrics matter:

| Metric | Formula | What it measures | All-time |
|--------|---------|-----------------|----------|
| **True Model WR** | `(W + EE_regrets) / (W + L + EE)` | Model's directional accuracy — did we call the right side? Uses Gamma resolution ground truth to check if EE trades would have won. | ~47% |
| **Settlement WR** | `W / (W + L)` | Trades that went to settlement only. **Misleading low** — excludes EE trades which are mostly correct calls exited early. Do NOT use this to judge model quality. | ~38% |
| **Effective WR** | `(W + EE) / (W + L + EE)` | Treats all EE as wins. **Misleading high** — a small fraction of EE were saves (would have lost). Best proxy when Gamma data is unavailable. | ~59% |

**Key insight**: The system profits by harvesting bid spikes on correct predictions (EE) rather than waiting for settlement. EE trades earn ~$4 avg vs ~$9 for full settlement wins, but avoid the ~$5 loss risk. This is a deliberate R:R tradeoff, not a deficiency.

**Gamma resolution** (`gamma_winner_matches` column) is the ground truth for EE classification:
- `gamma_winner_matches = 1` → REGRET (model was right, exited early for less profit)
- `gamma_winner_matches = 0` → SAVE (model was wrong, EE avoided a loss)
- Run `backfill_gamma_resolution.py --apply` to backfill recent trades

## Tech Stack

- Python 3.11+, asyncio throughout
- Binance WebSocket (spot klines + depth + aggTrades, futures funding rate)
- Polymarket Gamma/CLOB API (market discovery, live prices, no auth needed)
- Polymarket RTDS WebSocket for Chainlink BTC/USD stream (settlement price — matches Polymarket's resolution source)
- py-clob-client (Polymarket CLOB trading — real orders via FOK market buys)
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

scripts/
  analysis/   → 27 one-off analysis scripts (bid spikes, exits, regimes, P&L gaps, etc.)
  simulation/ → 8 Monte Carlo / historical replay scripts

ml_pipeline.py       → ML training pipeline (downloads Binance history, builds dataset, trains models)
backfill_outcomes.py → Correct historical trade outcomes using Gamma API resolutions
backfill_gamma_resolution.py → Backfill gamma_resolution + gamma_winner_matches for EE ground truth
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

### Deploy Workflow

```bash
# Full deploy sequence (run from BTC-tool directory):

# 1. Ensure working tree is clean — never deploy uncommitted changes
git status

# 2. Commit (CLAUDE.md changelog must be in the same commit as code changes)
git add <changed-files> CLAUDE.md
git commit -m "feat/fix: short description of what and why"

# 3. Deploy to VPS (adjust scp lines to only the files that changed)
scp <changed-files> root@65.21.178.90:/home/btcedge/BTC-tool/

# 4. Restart service
ssh root@65.21.178.90 "systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge"

# 5. Verify it's running
ssh root@65.21.178.90 "sleep 3 && systemctl is-active btc-edge && tail -20 /home/btcedge/BTC-tool/btc_edge.log"

# 6. Tag the deploy
git tag -a deploy-$(date -u +%Y-%m-%d-%H%M) -m "deployed: short description"
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
| `/ee` | Early exit tier stats (thresholds, trigger rates, P&L by entry price bucket) |

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
MAX_LIVE_BET_USDC=10.0        # Hard safety cap (must accommodate 5-token CLOB minimum)
# CLOB_PROXY=socks5://127.0.0.1:1080  # Not needed from Finland (no geoblock)

# Polymarket fees
POLYMARKET_FEE_RATE=0.25
POLYMARKET_FEE_EXPONENT=2

# Regime detection
REGIME_TREND_THRESHOLD=0.30    # strength threshold for trending classification
REGIME_FLIP_THRESHOLD=0.35     # flip signal to trend-following when |strength| >= this
REGIME_FLIP_LIVE=true          # enable regime flip for live trades (not just paper)
REGIME_FLIP_CONFIRM_WINDOWS=1  # consecutive trending windows before flip fires (was 3, reverted 2026-03-05)

# Loss streak guard
STREAK_PAUSE_THRESHOLD=3       # consecutive same-side losses to trigger pause
STREAK_PAUSE_WINDOWS=2         # number of 5-min windows to pause (~10 min)

# Confidence dampening — shrink P(up) toward 0.5 to counter model overconfidence
CONFIDENCE_DAMPEN=1.0          # 1.0 = no dampening (current), 0.6 = original. Disabled 2026-03-04.

# Adaptive early exit — 4-tier thresholds by entry price
EARLY_EXIT_THRESHOLD_LOW=0.50      # entry < 0.35: lottery tickets, exit on any spike
EARLY_EXIT_THRESHOLD_LOW_MID=0.65  # entry 0.35-0.40: raised from 0.45 on 2026-03-05
EARLY_EXIT_THRESHOLD_MID=0.90      # entry 0.40-0.50: raised from 0.65 on 2026-03-05
EARLY_EXIT_THRESHOLD_HIGH=0.95     # entry >= 0.50 (~55% WR, conservative)

# Hour blacklist (UTC hours to skip trading)
BLACKLIST_HOURS=2              # comma-separated, e.g. "2,3,4"
```

## Safety Filters

Applied in `strategy/edge.py` and `main.py` before every trade:

1. **Min confidence** (`MIN_CONFIDENCE=0.020`): Skip when |P(up) - 0.5| below threshold
2. **Entry price filter**: Hard reject outside 0.25-0.65. Exploration range 0.25-0.50 is paper-only (normal trades below 0.50 lose money per deep analysis), tagged `trade_tag="exploration"`
3. **Time gate** (`_MAX_ENTRY_SECONDS=60`): Only enter in first 60 seconds of 5-min window *(deployed 2026-03-02)*
4. **Hour blacklist** (`BLACKLIST_HOURS`): Skip configured UTC hours (default: 02:00)
5. **Loss streak guard** (`STREAK_PAUSE_THRESHOLD=3`): After 3 consecutive LOSS on the same side, pause that side for 2 windows (~10 min). Early exits break the chain. Only tracks taker trades. Sends Telegram alert on trigger. First trade after pause tagged `post_streak`. *(deployed 2026-03-03 ~21:30 UTC)*
6. **Trend-conflict filter** (`_TREND_CONFLICT_PCT=0.15`): Skip if BTC moved >0.15% against signal direction
7. **Regime flip** (`REGIME_FLIP_THRESHOLD=0.35`): When regime strength >= 0.35 (trending), flip both paper and live trades to bet WITH the trend. Tagged `trade_tag='regime_flip'`. 1-window confirmation (`REGIME_FLIP_CONFIRM_WINDOWS=1`). Was briefly 3-window (2026-03-04 ~15:20 to 2026-03-05 ~03:00) but reverted — 3-window let 10 against-trend trades through for -$24 overnight. Controlled by `REGIME_FLIP_LIVE`. *(deployed 2026-03-03 ~21:30 UTC)*
8. **One trade per window**: No duplicate bets on same market slug
9. **Pause**: `/pause` stops new trades while data collection continues
10. **Flat close = DOWN**: Polymarket resolves ties as DOWN. Uses strict `>` (not `>=`)
11. **Spread filter** (`> 0.03`): Skip live trade when bid-ask spread exceeds $0.03 (routes to paper-only). Wide spreads predict -$1.15/trade EE outcomes

## Adaptive Early Exit

Dedicated 1-second monitoring loop (`_early_exit_loop()` → `_monitor_early_exit()`) sells live positions when the bid price reaches a threshold, locking in profit before settlement. This is a deliberate profit-locking strategy — ~86% of EE trades are regrets (model called correct side), not rescues. See "Win Rate Metrics" section for correct interpretation.

**4-tier thresholds by entry price** *(updated 2026-03-03 from 548-trade bid spike analysis)*:

| Entry Price | Exit Threshold | Losses Caught | Rationale |
|-------------|---------------|--------------|-----------|
| < 0.35 | 0.50 | 87% (13/15) | Lottery tickets — spike briefly, grab any profit |
| 0.35 - 0.40 | 0.65 | 88% (22/25) | Raised from 0.45 (2026-03-05) — old threshold gave ~$0.50 margin |
| 0.40 - 0.50 | 0.90 | ~70% | Raised from 0.65 (2026-03-05) — 76% would-win rate, stop clipping winners |
| >= 0.50 | 0.95 | 12% (16/131) | High WR, conservative — let winners run |

- **First touch**: Sell immediately when `bid >= threshold` (no hold-confirmation — bid spikes on losers last only 5-8 seconds)
- **Monitoring window**: Entire 5-min window after a 10-second grace period (avoids stale book data right after entry)
- **Minimum depth**: `>= 20 tokens` on bid side
- **Token balance query**: Before selling, `get_token_balance()` queries actual CLOB conditional token balance (avoids "not enough balance" errors from computed vs actual token count mismatch)
- **Retry logic**: Up to 3 attempts with 5-second cooldown between each (replaces old permanent `exit_failed` flag that blocked all retries after one failure)
- **DB outcome**: `"EARLY_EXIT"` — skipped by unsettled trade queries
- **Asymmetric payoff**: EE earns avg ~$4/trade. Regrets (86% of EE) forgo ~$5 extra settlement profit. Saves (14%) avoid ~$5 loss. Net positive tradeoff.
- **`get_exit_threshold(entry_price)`** in `LiveTrader` returns the appropriate threshold
- **Data collection**: Each settled trade stores `max_bid_during_window` and `exit_threshold_used` for future ML modeling of exit probability
- **Data**: Validated on 548 trades with 102K market snapshots (bid spike analysis)

## Danger Zone: py-clob-client Landmines

These three bugs will silently break live trading if the workarounds are removed:

1. **Rounding bug** (monkey-patched in `LiveTrader.initialize()`): Library swaps maker/taker rounding for BUY orders. Our patch computes taker first (2 dec), derives maker via `Decimal` multiplication (4 dec). Uses `round_down` for maker — `round_up` overshoots by 0.0001.

2. **SELL amount = token count, NOT USDC**: `MarketOrderArgs(amount, side=SELL)` expects token count. For BUY, amount = USDC. Passing USDC value for SELL causes partial fills leaving tokens behind.

3. **5-token minimum**: ALL CLOB markets require `minimum_order_size: 5` (tokens, not USDC). `place_order()` auto-bumps to `max(5.5 × price, $3.50)`. Hard floor `$1.00` for any marketable order.

## Version Control

- **Every code change MUST be committed before deploying to VPS** — never scp uncommitted changes, even for hotfixes. If it's urgent, commit with a messy message; you can always clean up later.
- **Commit messages must say what changed and why** — e.g. `fix: EE depth check using best_bid_size instead of total_deep_size` not `update stuff`.
- **CLAUDE.md changelog and code go in the same commit** — the changelog entry documents the deploy, so it must travel with the code. Never commit code now and update the changelog later.
- **Tag every deploy**: After scp + restart + verify, run `git tag -a deploy-YYYY-MM-DD-HHMM -m "deployed: short description"`. This creates a permanent record of exactly what code is running on the VPS.
- **Always push to GitHub**: Every commit must be pushed to `origin` before the session ends. The user accesses the repo remotely — unpushed commits are invisible and effectively lost. Push after every commit or batch of commits, never leave commits local-only.
- **Deploy workflow**: code change → update CLAUDE.md changelog → commit → push to GitHub → scp to VPS → restart → verify logs → tag.
- **No orphan deploys**: If you find untagged deploys (code on VPS that doesn't match any tag), create a retroactive tag at the best-guess commit.

## Conventions

- All async — use `async def` and `await` consistently
- Logging via `logging.getLogger(__name__)` in every module
- Dataclasses for data transfer between components (not dicts)
- Config via pydantic-settings, never hardcoded values
- Graceful degradation — ML model falls back to rule-based if unavailable; Telegram disabled silently if unconfigured
- Console output must be ASCII-safe (no emojis in logger.info)
- Windows compatibility — no signal handlers (add_signal_handler wrapped in try/except NotImplementedError)
- Paper trades stored as dicts in `_pending_trades` (keyed by market slug), persisted to SQLite
- **Changelog required**: When deploying any feature or behavior change to VPS, add a row to the Changelog table at the bottom of this file with the date (UTC), a short description, and a link to the design doc if one exists. This is critical for distinguishing which data was collected under which version.

## Detailed Reference Docs

See `docs/` for in-depth reference material (read on-demand when working in specific areas):

- **`docs/live-trading.md`** — CLOB architecture, all py-clob-client quirks (8 items), safety controls, DB schema, independent live trading, early exit selling (trigger/token tracking/settlement), live results
- **`docs/ml-model.md`** — 44 ML features, training pipeline, model comparison table, best/worst hours, confidence thresholds, rule-based fallback formula
- **`docs/trading-logic.md`** — Data flow diagram, edge detection modes, bet sizing strategies (fixed/kelly/adaptive with multiplier details), paper trading fee model, window lifecycle & settlement (Gamma API, two-tier), startup sequence
- **`docs/known-issues.md`** — All 32 historical bug fixes and known issues with descriptions and resolutions
- **`docs/plans/`** — Design documents and implementation plans (adaptive early exit design + plan)

## Changelog

Reverse-chronological log of deployed changes. Check timestamps to know what data was collected under which version.

| Date (UTC) | Change | Design Doc |
|------------|--------|------------|
| 2026-03-10 ~17:00 | **Quick wins from deep analysis**: (1) MIN_CONFIDENCE raised 0.015->0.020 (1.5-2% band averaged -$0.16/trade). (2) Normal trades below entry $0.50 now paper-only (lost -$66 on 214 trades). (3) Regime flip cap raised 0.50->0.55 (flips at 0.50-0.55 are profitable). (4) EE mid-tier comment updated to reflect 0.90 revert (flat 0.90 outperforms tiered). (5) Spread filter: skip live when spread > $0.03 (-$1.15/trade at wide spreads). | `docs/plans/2026-03-10-deep-analysis-report.md` |
| 2026-03-10 ~16:35 | **Shadow bid fix**: `bids[0]` → `bids[-1]` in shadow tracking. CLOB returns bids ascending; shadow was reading floor bid ($0.01) instead of best bid. All 237 shadow windows from 2026-03-09/10 have garbage bid data. Data collection effectively restarted. | — |
| 2026-03-10 | **Gamma verification system**: 3-layer fix for phantom WINs (12.7% error rate, $234 discrepancy). Layer 1: delayed 5-min Gamma verification after every Chainlink settlement. Layer 2: hourly reconciliation catches anything Layer 1 missed. Layer 3: balance sanity check alerts on $5+ DB/actual divergence. Corrections update outcome, PnL, bankroll, and streak guard state. Config: reverted `early_exit_threshold_mid` 0.70 -> 0.90 (VPS had 0.90 hardcoded anyway, data confirms 0.90 earns more). | `docs/plans/2026-03-10-gamma-verification-design.md` |
| 2026-03-09 ~21:00 | **Quick wins**: (1) EE mid-tier threshold lowered 0.90→0.70 for entry 0.40-0.50. Data shows 51% EE rate at 0.70 vs 37% at 0.90, est +$58/9days. Median max_bid for this tier is 0.70 — old threshold missed most spikes. (2) Regime flip entry cap at <0.50 for live trades. Flip entries >=0.55 lost -$0.85/trade (64 trades, -$55). Paper still flips all prices (data collection). | `docs/plans/2026-03-09-ee-northern-star-design.md` |
| 2026-03-09 ~20:40 | **Cleanup & EE northern star**: (1) Fixed shadow tracking bug — `fv.funding_zscore` → `fv.funding_rate` (was crashing every window since 2026-03-08, 0 rows collected). (2) Wired EE thresholds to config (mid tiers were hardcoded, config defaults stale). (3) Moved 35 analysis/simulation scripts to `scripts/`. (4) Updated .env.example with all 30+ settings. (5) Fixed stale CLAUDE.md (EE table, commands, architecture). | `docs/plans/2026-03-09-cleanup-and-ee-northern-star.md` |
| 2026-03-08 ~18:50 | **FOK orders**: Switched buy + EE sell from GTC to FOK. Eliminates maker fill drag (358 all-time, -$12 and worsening at -$40/wk). FOK = fill entire order immediately or cancel, no resting orders on book. Auto-sell stays GTC. FOK rejections logged to skipped_windows. Maker fill sync kept as canary. Rounding patch updated: maker (USDC) at 2 dec, taker (tokens) at 4 dec (FOK requires opposite of GTC). | `docs/plans/2026-03-08-fok-orders-design.md` |
| 2026-03-05 ~07:20 | **Overnight audit fixes**: (1) Regime flip confirmation reverted from 3 windows back to 1 — 3-window let 10 against-trend trades through for -$24 overnight while BTC trended down. (2) Hour 02 UTC re-blacklisted. Overnight bled -$47 (98 trades, 23% WR). Maker fills contributed -$23 (no EE protection, bet both sides). UP trades: 14% WR, -$53. EE saved +$85. | — |
| 2026-03-05 ~04:00 | **EE depth check fix**: `available_depth` now uses `best_bid_size` (tokens at best bid) instead of `total_deep_size` (tokens at bids >= $0.85). Old check blocked all cheap-entry exits — when threshold is $0.45 and bid hits $0.54, there's obviously no depth at $0.85. Low/mid tier exits were only triggering by luck when bid happened to reach 0.85+ | — |
| 2026-03-04 ~17:44 | **Data collection gaps fixed**: (1) `fill_price` now extracted from `takerOrder.price` (was looking at nonexistent `avg_price`), (2) `skipped_windows` enriched with `model_side`, `model_confidence`, `entry_price`, and 11 Binance features, (3) Maker fills now include `fill_price` and `btc_price_at_open` | — |
| 2026-03-04 ~16:02 | **Dampening removed**: `CONFIDENCE_DAMPEN` 0.6 -> 1.0. Was killing ~57% of windows as "low confidence" but analysis shows EE makes even coin-flip trades profitable (+$1.55/trade). 127 skipped windows would have made +$235. Now trades every window with raw confidence >= 1.5% | — |
| 2026-03-04 ~15:20 | **Regime flip confirmation window**: Require 3 consecutive trending windows (15 min) before triggering a flip. Fixes flickering — detector was briefly spiking past 0.35 threshold during ranging markets then dropping back, causing false flip trades. New config: `REGIME_FLIP_CONFIRM_WINDOWS=3`. `/regime` command shows trend count. | — |
| 2026-03-04 ~10:12 | **Max bet raised**: $5 → $10. Bankroll grew to ~$123 (deposits + P&L). At $128, $10 = 7.8% of bankroll — more conservative than $5 was at $53 (9.4%). All guardrails stay (adaptive sizing, streak guard, early exit, regime flip) | — |
| 2026-03-04 ~07:45 | **Exploration threshold raised**: 0.35 → 0.40. Entries <$0.40 now paper-only. Bad R:R in that tier (EE profit ~$1 vs $5 risk, overnight data: -$6.34 net on 7 trades) | — |
| 2026-03-03 ~22:30 | **Audit bug fixes (Tier 1)**: (1) Early exit monitor now checks bids every 1s (was throttled to 10s by misplaced rate limiter), (2) ML confidence dampening now applied (was no-op for ML path), (3) Regime flip runs before streak guard + trend filter (were checking wrong side). Also fixes trend filter blocking regime-flip opportunities. | `docs/audit-2026-03-03.md` |
| 2026-03-03 ~22:00 | **Entry-time feature persistence**: 15 new columns on live_trades — 11 Binance features at entry (OBI, taker ratio, momentum, RSI, VWAP dev, BB position, EMA cross, funding z-score, volume z-score, ATR) + 4 Polymarket orderbook state (up/down spread, token bid/ask sizes). Enables future exit-probability ML model training | — |
| 2026-03-03 ~21:00 | **Comprehensive data collection**: 9 new columns on live_trades (max_bid, exit_threshold, btc_at_open, settlement_price, fill_price, 5 regime sub-components, model_confidence), 4 new on market_snapshots (bid/ask sizes), new `skipped_windows` table. Enables ML exit prediction, slippage analysis, skip opportunity analysis | — |
| 2026-03-03 ~20:30 | **4-tier early exit**: Split 3-tier into 4-tier thresholds based on 548-trade bid spike analysis. <0.35: 0.60->0.50, 0.35-0.40: 0.65->0.45, 0.40-0.50: 0.65 (unchanged), >=0.50: 0.95 (unchanged). Catches 87% of cheap losses vs 20% before | — |
| 2026-03-03 ~21:30 | **Trend protection**: Chainlink price buffer for boundary-accurate settlement, live regime flip (strength >= 0.35), loss streak guard (3x LOSS pauses side for 2 windows) | `docs/plans/2026-03-03-trend-protection-design.md` |
| 2026-03-02 ~18:00 | **Adaptive early exit**: Tiered thresholds by entry price (<0.35/0.35-0.50/>=0.50), full-window monitoring, retry logic, token balance query | `docs/plans/2026-03-02-adaptive-early-exit-design.md` |
| 2026-03-02 ~16:00 | **Telegram cleanup**: 13 -> 10 commands, combined trade alert, /recent + /today, 60-min stats interval | `docs/plans/2026-03-02-telegram-cleanup-design.md` |
| 2026-03-02 ~14:00 | **Entry time gate**: Tightened from 120s to 60s (60-120s entries had 24.5% WR) | — |
| 2026-03-01 | **Confidence filter**: MIN_CONFIDENCE=0.015, skip low-signal windows | — |
| 2026-02-28 | **ML model trained**: RF_d3 (53.9% accuracy, 34,100 samples, 44 features) | — |
| 2026-02-28 | **Live trading launched**: py-clob-client, GTC orders, Helsinki VPS | — |
