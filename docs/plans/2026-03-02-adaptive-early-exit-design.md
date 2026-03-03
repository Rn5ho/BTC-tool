# Adaptive Early Exit Strategy — Design Document

**Date**: 2026-03-02
**Status**: Approved

## Problem

The current early exit system uses a flat `bid >= 0.95` threshold for all positions. Analysis of 640+ trades across live, paper, and CLOB ground truth data shows this is suboptimal — cheap entries (< 0.50) have 13-25% win rate and would benefit from more aggressive exits, while expensive entries (>= 0.50) have ~55% WR and should stay conservative.

## Evidence

### Data Sources

| Source | Trades | Period |
|--------|--------|--------|
| Live trades (DB + snapshots) | 309 | 1.7 days |
| Paper trades (DB + snapshots) | 331 | 2.1 days |
| CLOB API (ground truth) | 45 matched | 1.7 days |
| Candle data (regime analysis) | 4,192 windows | 14.9 days |

### Key Findings

1. **Win rate varies dramatically by entry price**:
   - Entry < 0.35: ~15% WR
   - Entry 0.35-0.50: ~25% WR
   - Entry >= 0.50: ~55% WR

2. **Tiered exits outperform flat 0.95 across all datasets**:
   - Live: +$130 vs +$58 (flat 0.95)
   - Combined: +$37 vs -$44 (flat 0.95 is negative on combined)
   - CLOB: +$56 vs +$25

3. **First-touch exit beats hold-confirmation**:
   - First touch: +$136
   - 1-snapshot hold: +$90 (34% worse)
   - Reason: losing trades' bid spikes last only ~5-8 seconds

4. **Stop-losses are a trap**: Look great in idealized form (+$114) but terrible in realistic sequential simulation (+$54) because bids gap below stop levels.

5. **Value is concentrated in cheap entries**: Entries >= 0.50 show essentially zero benefit from exits (delta = -$0.39 to -$0.86 on combined data).

6. **Existing early exits already saving ~$120**: 46 early exits at 0.95 threshold have generated +$119.80 in real P&L.

### Threshold Optimization

Fine-grained sweep of 612 combinations on live data, validated on combined data:

| Config | Live Δ | Combined Δ | CLOB Δ |
|--------|--------|------------|--------|
| Flat 0.95 | +$58 | -$44 | +$25 |
| **0.60/0.65/0.95** | **+$130** | **+$37** | **+$56** |
| 0.70/0.85/0.95 | +$96 | -$11 | +$47 |

The 0.65 mid threshold sits in the per-bucket optimal plateau (0.64-0.68 range). The high threshold stays at 0.95 where it's proven.

## Design

### Thresholds

| Entry Price | Exit Threshold | Rationale |
|-------------|---------------|-----------|
| < 0.35 | 0.60 | Lottery tickets, ~15% WR. Sell on any spike. |
| 0.35 - 0.50 | 0.65 | Low WR (~25%). Most are losers — exit aggressively. |
| >= 0.50 | 0.95 | Current threshold. 55% WR, conservative exit. |

Boundaries at 0.35 and 0.50. The 0.50 boundary aligns with the natural WR cliff (24% -> 52%).

### Trigger Rules

- **First touch**: Sell immediately when bid >= threshold
- **Minimum depth**: >= 20 tokens on bid side
- **Time gate**: Skip first 10 seconds after entry
- **Scope**: All live positions

### Files to Modify

| File | Change |
|------|--------|
| `config.py` | Add `early_exit_threshold_low`, `_mid`, `_high` settings |
| `main.py` | Replace hardcoded `0.95` with `get_exit_threshold(entry_price)` in `_monitor_early_exit` |
| `strategy/live_trader.py` | Add `get_exit_threshold()` helper method |

### Config Additions

```python
early_exit_threshold_low: float = 0.60    # entry < 0.35
early_exit_threshold_mid: float = 0.65    # entry 0.35-0.50
early_exit_threshold_high: float = 0.95   # entry >= 0.50
```

### What Doesn't Change

- 1-second monitoring loop
- Token tracking at buy time
- Sell execution and retry logic
- DB recording (outcome="EARLY_EXIT")
- Telegram notifications
- Depth requirement (>= 20 tokens)

### Logging Enhancement

Tag exits with threshold info:
```
EARLY EXIT: slug @ bid=0.67 (threshold=0.65, tier=mid, entry=0.42)
```

### Expected Impact

- Conservative: +$0.07/trade (+$14/day at 200 trades/day)
- Optimistic: +$0.44/trade (+$88/day)
- Each rescue: avg +$4.44, each regretted exit: avg -$0.61 (7:1 asymmetry)

### Risks

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Thresholds overfit to small sample | Medium | Low (only affects min-size <0.50 trades) | Configurable via .env |
| Market maker behavior changes | Low | Medium | Monitor and adjust thresholds |
| Selling at low bids gets poor fills | Low | Low | 1 cent avg spread at bid >= 0.60 |
| Cuts short rare <0.50 winners | High (by design) | Low | 13-25% WR means math strongly favors exiting |

### Validation Plan

After deployment, query:
```sql
SELECT
  CASE
    WHEN entry_price < 0.35 THEN 'low'
    WHEN entry_price < 0.50 THEN 'mid'
    ELSE 'high'
  END as tier,
  COUNT(*) as exits,
  ROUND(AVG(pnl), 2) as avg_pnl,
  ROUND(SUM(pnl), 2) as total_pnl
FROM live_trades
WHERE outcome = 'EARLY_EXIT'
GROUP BY tier;
```
