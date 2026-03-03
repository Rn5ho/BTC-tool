# Trend Protection & Settlement Accuracy Design

**Date:** 2026-03-03
**Status:** Draft — pending approval

## Problem

Two related issues are hurting live trading performance:

1. **Trending markets destroy us.** The ML model is mean-reverting — it bets against
   trends. When BTC trends, it has a ~15% win rate (105 trades, -$37). If we had
   flipped to bet WITH the trend, we'd have had 83% WR and +$142 instead of -$145.

2. **Settlement accuracy on close calls.** All 567 real-time settlements used
   Chainlink/Binance price comparison — zero used Gamma API (Polymarket's actual
   resolution hasn't happened yet 5s after window close). The price we read is the
   *current* price at settlement time, not the price at the exact window boundary.
   When BTC flips direction in the last few seconds, we call WIN/LOSS wrong.

Settlement accuracy is a prerequisite for streak detection: if outcomes are wrong,
streak logic inherits those errors.

## Data (537 settled live trades, 2026-02-28 through 2026-03-03)

### Regime performance
| Regime | Trades | WR (W/L) | P&L |
|--------|--------|----------|-----|
| ranging | 66 | 40.5% | +$33.14 |
| trending_up | 49 | 14.3% | -$27.46 |
| trending_down | 56 | 16.7% | -$9.91 |

### Trend continuation rate by strength
| Strength | Trend continued |
|----------|----------------|
| 0.30-0.35 | 56% (noisy) |
| 0.35-0.40 | 100% |
| 0.40-0.50 | 82% |
| 0.50-0.60 | 90% |
| 0.60+ | 85% |

At strength >= 0.35, trend continues through the 5-min window 82-100% of the time.
The regime detector's 10-20 minute lag does NOT make it too late — BTC trends persist.

### Settlement sources (from log analysis)
- Chainlink Stream: 499 windows
- Binance fallback: 68 windows
- Gamma API: 0 windows (always fails — resolution not ready 5s after close)

## Design — Three Phases

### Phase 0: Fix Settlement Accuracy

**Problem:** `_settle_previous_window()` calls `get_chainlink_stream_price()` which
returns the current price, not the price at the window boundary. On close calls
(small BTC moves in last few seconds), this can flip the WIN/LOSS determination.

**Fix:** Add a rolling price buffer to the Chainlink stream handler.

1. In `PolymarketClient`, add a `deque` of `(timestamp, price)` tuples (max 120
   entries ≈ 2 minutes of updates).
2. Each Chainlink stream update appends to the buffer (existing price storage
   continues unchanged for other uses).
3. Add method `get_chainlink_price_at(target_time: float) -> float | None` that
   returns the price with the timestamp closest to `target_time`.
4. In `_settle_previous_window()`, compute `window_end_time` from the slug
   timestamp + 300 seconds. Use `get_chainlink_price_at(window_end_time)` instead
   of `get_chainlink_stream_price()`.
5. Log the time delta between the window boundary and the price sample used, so we
   can monitor accuracy.

**Fallback chain:** buffer lookup → current Chainlink → Binance → skip settlement
(same as today minus the timing error).

### Phase 1: Loss Streak Guard + Live Regime Flip

Two complementary triggers for the same action:

#### 1a. Live Regime Flip

Currently regime flip only applies to paper trades (line 1146-1163 in main.py).
Extend it to live trades.

- When `regime_state != "ranging"` and `|strength| >= 0.35`:
  - Use `_build_flipped_signal()` (already exists) to construct a WITH-trend signal
  - Pass flipped signal to live trader instead of model's signal
  - Tag as `trade_tag='regime_flip'` in DB for tracking
  - Log: `"LIVE FLIP: {old_side} -> {new_side} (regime={state}, str={strength})"`
- Config: `REGIME_FLIP_THRESHOLD` (existing, change default from 0.40 to 0.35 based
  on data showing 0.35+ has 82-100% continuation)

#### 1b. Loss Streak Guard

A fast early-warning system that detects potential trend onset *before* the regime
detector (which needs ~20 minutes of data).

**State:**
- `_side_outcomes`: `dict[str, list[str]]` — rolling window of last 10 outcomes per
  side ("UP" and "DOWN"), updated on each live trade settlement.
- Only tracks our deliberate taker trades (not maker_fills) to avoid the known
  maker_fill settlement bugs.
- `_side_paused`: `dict[str, float]` — maps paused side to TTL expiry timestamp.

**Trigger:**
- After each live trade settlement, check the last N outcomes for that side.
- If N consecutive LOSS (only hard losses — early exits break the chain):
  - Set `_side_paused[side] = time.time() + STREAK_PAUSE_SECONDS`
  - Log: `"Streak guard: {N}x {side} LOSS — pausing {side} for {windows} windows"`
  - Send Telegram notification

**During pause:**
- If model wants to trade the paused side → skip with reason `"streak guard"`
- Trades on the OTHER side still go through
- The regime detector continues running in the background

**Pause expires:**
- Check regime state at expiry
- If regime says trending (and strength >= threshold) → regime flip handles it
  (already active from 1a above)
- If regime says ranging → resume normal

**Config (all in .env, with sensible defaults):**
- `STREAK_PAUSE_THRESHOLD=3` — consecutive losses to trigger pause
- `STREAK_PAUSE_WINDOWS=2` — number of 5-min windows to pause (~10 min)

**Tagging for data analysis:**
- Skipped trades: logged with reason `"streak guard (3xDOWN)"`
- All regime-flipped trades: `trade_tag='regime_flip'`
- Post-pause trades (first trade after pause expires): `trade_tag='post_streak'`
- This lets us later query: "were streak pauses actually correct? were flipped
  trades profitable? were post-streak trades any good?"

### Phase 2: Adaptive Confidence Dampening (Future)

Replace the fixed `CONFIDENCE_DAMPEN=0.6` with a regime-aware function:

| Regime | Strength | Dampen | Effect |
|--------|----------|--------|--------|
| ranging | < 0.30 | 0.7 | Trust model more than today |
| weak trend | 0.30-0.40 | 0.2 | Heavily doubt model |
| strong trend | >= 0.40 | -0.5 | Invert model prediction |

When dampen goes negative, P(up) shifts past 0.50 naturally, causing the edge
detector to pick the opposite side. No separate flip logic needed — the signal
inverts organically.

This is smoother than binary flip and removes the hard threshold. But it requires
careful calibration and more data. Implement after Phase 1 has gathered enough
regime-flip trade data.

### Future: Regime as ML Feature

Add `regime_strength` to the 44-feature vector and retrain. The model itself learns
when to mean-revert vs trend-follow, removing the need for hand-tuned thresholds.
Requires retraining pipeline changes and historical regime data. Not for this cycle.

## Key Principle

All thresholds and parameters in this design are starting hypotheses based on 537
trades of data. Everything is configurable and tagged for analysis. After gathering
~200+ regime-flip trades and ~50+ streak-triggered pauses, we revisit the data and
adjust.

## Implementation Order

1. Phase 0 (settlement fix) — prerequisite, do first
2. Phase 1a (live regime flip) — small change, existing infrastructure
3. Phase 1b (streak guard) — new but simple state tracking
4. Deploy, gather data, analyze
5. Phase 2 — only after data validates/invalidates Phase 1 approach
