# Design: 3-Layer Gamma Verification System

**Date:** 2026-03-10
**Problem:** DB claims +$208.69 total PnL. Actual Polymarket balance shows ~$127 on $152.77 deposited = **-$25.71 actual PnL**. Discrepancy: **$234.40**. Root cause: 47 trades (12.7% of WINs) where DB outcome=WIN but Gamma API says the other side won ("phantom WINs"). These cascade into corrupted streak guards, inflated win rates, and wrong bankroll calculations.

## Root Cause

Settlement code in `_settle_previous_window()` waits only 5 seconds then queries Gamma API. Polymarket markets take 2-6 minutes to resolve after window close. Gamma returns `closed=False` → all 3 retries fail → falls back to Chainlink/Binance price comparison. This price comparison disagrees with Polymarket's actual resolution ~7.8% of the time.

**Evidence:** VPS logs show `[Chainlink Buffer]` for every single settlement. Gamma API path has never succeeded in production. Tested live: market 90 seconds after close → `closed=False`. Market 390 seconds after close → `closed=True, outcomePrices=[1,0]`.

## Secondary: Missed WINs

18 trades where DB outcome=LOSS but Gamma says our side won. If tokens were auto-redeemed, this is +$49.14 in uncounted profit. If tokens are stuck, $105.21 in potentially claimable value.

## Design: 3 Layers

### Layer 1: Delayed Gamma Verification (5-minute callback)

After `_settle_previous_window()` settles a trade using Chainlink fallback, schedule an async verification task to run ~5 minutes later when Gamma resolution is guaranteed available.

```
_settle_previous_window():
    ... existing settlement (Chainlink fallback) ...
    if settlement_source != "Gamma API":
        asyncio.create_task(_verify_settlement_gamma(slug, settled_trades))
```

`_verify_settlement_gamma(slug, settled_trades)`:
1. `await asyncio.sleep(300)` — wait 5 minutes for Gamma resolution
2. Query Gamma API for actual resolution (reuse `_query_gamma_resolution`)
3. If Gamma disagrees with recorded outcome:
   - Flip WIN↔LOSS in DB
   - Recalculate PnL (`pnl = (shares - amount)` for WIN, `pnl = -amount` for LOSS)
   - Update `gamma_resolution` and `gamma_winner_matches` columns
   - Update in-memory `_side_outcomes` streak chain (remove old outcome, append corrected one)
   - Log correction at WARNING level
   - Send Telegram alert: "CORRECTION: {slug} {side} {old}→{new} (Gamma verified)"
4. If Gamma agrees: update `gamma_resolution` and `gamma_winner_matches=1` (confirms correct)
5. If Gamma still unavailable after 5 min: log warning, let Layer 2 catch it

**Trades affected per verification:** Only the 1-2 trades from the just-settled window. Fast, low-risk.

### Layer 2: Hourly Reconciliation (enhanced stale settlement)

Enhance existing `_settle_stale_live_trades()` (runs every 60 min) to also re-verify already-settled trades that lack Gamma confirmation.

Current behavior: only picks up `outcome IS NULL` trades.
New behavior: also picks up trades where `gamma_resolution IS NULL` AND `settled_at IS NOT NULL` AND `outcome != 'EARLY_EXIT'` AND `settled_at < now - 10 minutes`.

For each such trade:
1. Query Gamma API for resolution
2. If resolution available and disagrees with DB outcome → correct (same logic as Layer 1)
3. If resolution agrees → stamp `gamma_resolution` and `gamma_winner_matches=1`
4. If no resolution yet (market somehow still not closed) → skip, try next hour

This catches anything Layer 1 missed (e.g., if the 5-min task was cancelled during restart, or Gamma was slow).

### Layer 3: Balance Sanity Check (daily Telegram alert)

Add a periodic check (every 6 hours) that compares DB-calculated PnL to actual wallet state.

`_balance_sanity_check()`:
1. Query Polymarket USDC balance via `get_balance()` (existing method)
2. Calculate DB-expected balance: `sum(deposits) + sum(pnl)` or simpler: `live_trader.bankroll`
3. Compare: if `|db_balance - actual_balance| > $5.00` → send Telegram warning
4. Log the comparison regardless

**Note:** This won't be perfectly accurate (pending positions, in-flight orders, token dust), but a $5+ gap after accounting for open positions is a strong signal something is wrong. The current gap of $234 would have been caught immediately.

## Streak Guard Implications

When a correction happens (phantom WIN→LOSS), the streak chain must be updated:
- Remove the "WIN" from `_side_outcomes[side]`
- Insert "LOSS" in its place
- Re-evaluate whether streak guard should now be active

This is tricky because the in-memory list only keeps last 10 outcomes and we don't track which list position corresponds to which trade. Simpler approach: **rebuild the streak chain from DB** after any correction:

```python
async def _rebuild_streak_state(self, side: str):
    """Rebuild _side_outcomes from last 10 DB trades for this side."""
    recent = await self.db.get_recent_outcomes(side, limit=10)
    self._side_outcomes[side] = [r["outcome"] for r in recent]
```

## DB Changes

New columns on `live_trades` (if not already present from backfill script):
- `gamma_resolution TEXT` — "UP" or "DOWN" from Gamma API
- `gamma_winner_matches INTEGER` — 1=DB agrees with Gamma, 0=DB disagrees

New helper in `storage/db.py`:
- `get_unverified_trades()` — trades with `outcome IS NOT NULL AND gamma_resolution IS NULL AND outcome != 'EARLY_EXIT'`
- `correct_trade_outcome(id, new_outcome, new_pnl, gamma_resolution, gamma_winner_matches)` — atomic update
- `get_recent_outcomes(side, limit)` — for streak rebuild

## What This Does NOT Fix

- **Historical phantom WINs**: The 47 already-wrong trades stay wrong in DB unless we re-run `backfill_gamma_resolution.py --apply` and add a correction pass. This is a one-time cleanup, not part of the live system.
- **Bankroll/balance display**: `live_trader.bankroll` is an in-memory counter that drifts. Layer 3 alerts when it drifts too far. A full fix would reset bankroll from actual balance periodically.
- **Paper trade settlement**: Paper trades still use Chainlink. They don't affect real money, so lower priority.

## Testing Plan

1. Deploy with Layer 1 only, monitor logs for 24h
2. Check that corrections are happening (should see ~1-2 per day based on 7.8% disagreement rate and ~28 settlements/day)
3. After confirming Layer 1 works, enable Layer 2 as safety net
4. Layer 3 (balance check) can ship simultaneously — it's read-only

## Risk

- **Double correction**: If Layer 1 corrects a trade, Layer 2 must not re-correct it. Guard: check `gamma_winner_matches IS NOT NULL` before correcting (already-verified trades are skipped).
- **Correction during active position**: Should not happen — we only verify trades that were already settled (outcome != NULL).
- **PnL sign flip**: Must recalculate PnL from scratch, not just negate. WIN PnL = `shares - amount`, LOSS PnL = `-amount`.
