# Telegram Bot Cleanup & Improvements — Design

**Date:** 2026-03-02
**Status:** Approved

## Problem

The Telegram bot grew organically and has 13 commands with significant overlap. Three commands (`/stats`, `/livetrades`, `/balance`) show redundant info. Trade placement fires two back-to-back messages. Paper-only commands (`/reset`, `/budget`) are dead weight since paper was removed from Telegram. No way to see recent trade history or daily performance without running `/analyze` (full statistical breakdown).

## Design Decisions

Captured via brainstorming session — user preferences documented below.

### User Preferences

- **Skip notifications**: Keep as-is. User finds them useful despite high frequency (~280/day).
- **Command overlap**: Merge `/stats` + `/livetrades` + `/balance` into one `/stats`.
- **Trade alerts**: Combine edge alert + trade placed into one message. Only fire when live trade placed.
- **Paper-only signals**: No Telegram alert. Silent.
- **Settlement alerts**: Keep minimal (single trade result). No running totals per settlement.
- **Periodic stats**: Change from 30-min to 60-min hourly report.
- **Paper commands**: Remove `/reset` and `/budget`. Not used.
- **Missing features**: Add `/recent` (last N trades) and `/today` (daily P&L summary).

## Final Command Set (10 commands)

### Kept Unchanged (7)

| Command | Description |
|---------|-------------|
| `/status` | BTC price (Binance + Chainlink), model P(up), market odds, state |
| `/trades` | Pending/unsettled live positions |
| `/pause` | Stop placing new trades |
| `/resume` | Resume placing trades |
| `/weights` | Model info (ML type + settings) |
| `/regime` | Market regime breakdown (5-component) |
| `/spread` | Live order book spreads |
| `/analyze` | Full statistical breakdown (side, hour, price bucket, source) |

### Reworked (1)

**`/stats`** — single comprehensive view, absorbs `/livetrades` and `/balance`:
- USDC balance (from CLOB API)
- Live bankroll + ROI
- W/L record, win rate, total P&L (all-time)
- Early exits + maker fills counts
- Session info (filled/total orders this session, volume)
- Small paper trading line at the bottom (just W/L and P&L)

### New (2)

**`/recent [N]`** — last N settled live trades (default 5, max 10):
- Each trade shows: side, entry price, outcome, P&L, time ago
- Quick trade log without the full statistical breakdown of `/analyze`

**`/today`** — today's performance (UTC day):
- Trade count, W/L record, P&L for today
- Best and worst trade
- Comparison context (e.g. "5 trades today, 3W/2L, +$2.15")

### Removed (4)

| Command | Reason |
|---------|--------|
| `/reset` | Paper-only, unused from Telegram |
| `/budget` | Paper-only, unused from Telegram |
| `/livetrades` | Absorbed into `/stats` |
| `/balance` | Absorbed into `/stats` |

## Alert Flow Changes

### Trade Placed (reworked)

**Before:** Two messages — "EDGE DETECTED" (with signal breakdown) + "LIVE TRADE PLACED" (with order details).

**After:** One combined message when live trade is placed:
- Side, slug, amount, entry price
- Confidence, edge
- Order ID

When live trade fails: error message (same as today).

Paper-only / exploration signals: no Telegram alert (silent).

### Settlement (unchanged)

WIN/LOSS/EARLY_EXIT format stays the same:
- Slug, side, outcome, P&L for that trade
- No running totals — hourly report covers that

### Skip Notifications (unchanged)

Every skipped window: slug, reason, BTC start/end, direction.

### Periodic Stats (reworked)

**Before:** Every 30 minutes.

**After:** Every 60 minutes (on the hour). Same comprehensive content, aligned with the unified `/stats` output.

## Implementation Scope

### Files Changed (3)

**`alerts/telegram.py`:**
- Replace `send_edge_alert()` + `send_live_trade_alert()` with new `send_trade_placed_alert()` combining both
- Remove `send_trade_alert()` (paper trade notification — unused)
- Keep all other methods unchanged

**`main.py`:**
- Remove `_cmd_reset`, `_cmd_budget`, `_cmd_livetrades`, `_cmd_balance` and their `register_command` calls
- Rework `_cmd_stats` to include USDC balance, session info, volume (absorb livetrades + balance content)
- Add `_cmd_recent(args)` — queries DB for last N settled live trades
- Add `_cmd_today(args)` — queries DB for today's trades (UTC day)
- Merge edge alert + live trade alert call site (lines ~1364-1380) into single `send_trade_placed_alert()` call
- Guard: only send trade alert when live trade is placed (not paper-only)
- Change `_stats_loop` interval from 1800s to 3600s
- Update `register_command` block (remove 4, add 2)

**`storage/db.py`:**
- Add `get_recent_live_trades(n: int)` — returns last N settled trades ordered by timestamp desc
- Add `get_today_live_trades()` — returns today's trades filtered by UTC day boundary

### Files NOT Touched

config.py, live_trader.py, paper_trader.py, edge.py, data/polymarket.py, signals/*, models/*. Zero risk to trading logic.

## Iteration Notes

This is a first pass. After deploying, collect feedback on:
- Is the combined trade placed message clear enough? Does it have the right info?
- Is hourly reporting the right frequency? Too frequent? Too sparse?
- Are `/recent` and `/today` showing useful info or do they need more/less detail?
- Should skip notifications get a throttle option (e.g. only send when reason changes)?
