# Design: Switch from GTC to FOK Orders

**Date:** 2026-03-08
**Problem:** GTC buy orders split into taker + maker portions. The maker portion rests on the book and gets filled when the market reverses (adverse selection). All-time: 358 maker fills, -$11.67 P&L, trending worse (week 2: -$40.21). These fills have no EE protection, no confidence/regime/streak checks.

## Root Cause

GTC = "Good Till Cancel". When our order can't fully fill as taker, the remainder sits on the book. Other traders hit it later — always when the market has reversed into our resting order, which is exactly when our directional bet is losing.

Data breakdown (339 maker fills matched to taker trades):
- Before/during taker execution: 196 fills, -$52.34 (inherent to GTC, can't be cancelled)
- After taker (1s-2min): 56 fills, +$36.83 (cancel_all_orders sometimes catches these)
- Stale (>2min): 87 fills, -$17.57 (cancel_all_orders failed or didn't run)

## Solution

Switch to FOK (Fill Or Kill) — "fill my entire order immediately, or cancel everything." Zero resting orders, zero maker fills.

### Order types by flow

| Flow | Current | New | Rationale |
|------|---------|-----|-----------|
| Buy orders | GTC | **FOK** | Prevents maker fills entirely |
| EE sell orders | GTC | **FOK** | No resting sell orders; 1s retry loop handles rejections |
| Auto-sell (post-resolution) | GTC | **GTC** (keep) | Thin post-resolution liquidity; resting sell at $0.99 has zero risk |

### FOK rejection handling

**Buy rejections** (not enough liquidity to fill entire order):
- Skip the window entirely — thin book = bad entry anyway
- Log to `skipped_windows` table with `skip_reason = "FOK rejected (insufficient liquidity)"` and full context (side, confidence, entry_price, model features)
- Do NOT retry with smaller amount or fall back to FAK

**EE sell rejections** (can't sell all tokens at once):
- Log the rejection at INFO level
- The 1-second EE monitoring loop naturally retries next iteration
- No special handling needed

### Maker fill sync

- **Keep running** as a canary — if new maker fills appear after deploy, something is wrong
- Add WARNING-level log if any new fills are discovered (they shouldn't exist)
- After 1 week with zero new maker fills, consider removing the sync entirely

### Cancel logic

- `cancel_all_orders()` call after fills stays as a safety net
- Should rarely fire since FOK orders don't leave remainders

### Tracking & verification

After deploy, monitor:
1. **FOK rejection rate**: `SELECT COUNT(*) FROM skipped_windows WHERE skip_reason LIKE 'FOK%'` — if >10% of windows are rejected, reconsider
2. **Maker fill canary**: any new maker fills = FOK isn't working as expected
3. **Fill rate comparison**: compare successful fills/window before vs after over 48h
4. **P&L impact**: daily P&L should improve by ~$5-10/day (maker fill drag removed)

### What does NOT change

- Order sizing logic (5-token minimum, adaptive sizing)
- MarketOrderArgs construction (same API)
- Phantom order detection (still useful — FOK can still return an order ID that gets cancelled)
- Entry price filters, confidence filters, regime flip, streak guard
- EE threshold tiers
- Auto-sell retry logic

### Risks

- **FOK rejects too often**: If Polymarket books are regularly too thin for $3.50-$10 orders, we'd skip many windows. Mitigation: tracked in skipped_windows, can switch to FAK if rejection rate is high.
- **py-clob-client FOK bug**: CLAUDE.md notes "FOK fails" historically. Need to test on VPS before full deploy. Mitigation: test with a single small order first.
- **EE misses more exits**: FOK might reject EE sells on thin books, missing narrow 5-8s bid spikes. Mitigation: 1s retry loop gives multiple attempts per spike.
