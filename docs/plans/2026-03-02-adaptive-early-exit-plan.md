# Adaptive Early Exit — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace flat 0.95 early exit threshold with tiered thresholds by entry price (0.60/<0.35, 0.65/0.35-0.50, 0.95/>=0.50).

**Architecture:** Add three configurable threshold settings in config.py, a helper function in live_trader.py, and swap the hardcoded `0.95` in main.py's `_monitor_early_exit` with the tiered lookup. No DB, model, or structural changes.

**Tech Stack:** Python 3.11, pydantic-settings, asyncio. Deployed via SCP to Hetzner VPS (65.21.178.90).

---

### Task 1: Add threshold settings to config.py

**Files:**
- Modify: `config.py:59-63` (after `regime_flip_threshold`)

**Step 1: Add the three threshold settings**

After the `regime_flip_threshold` and `confidence_dampen` block (line 68), add:

```python
    # Adaptive early exit — tiered thresholds by entry price
    # Lower entry prices have lower WR and benefit from aggressive exits.
    # Data: 640 trades, validated on live + paper + CLOB ground truth.
    early_exit_threshold_low: float = 0.60    # entry < 0.35 (~15% WR)
    early_exit_threshold_mid: float = 0.65    # entry 0.35-0.50 (~25% WR)
    early_exit_threshold_high: float = 0.95   # entry >= 0.50 (~55% WR)
```

**Step 2: Syntax check**

Run: `python -m py_compile config.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add tiered early exit threshold settings"
```

---

### Task 2: Add `get_exit_threshold()` to live_trader.py

**Files:**
- Modify: `strategy/live_trader.py` (add static method near the early exit section, around line 510)

**Step 1: Add the helper method**

Add this method to the `LiveTrader` class, just before the existing `sell_early_exit` method (line ~513):

```python
    @staticmethod
    def get_exit_threshold(entry_price: float) -> float:
        """Return the early exit bid threshold based on entry price tier.

        Cheap entries have low WR and benefit from aggressive exits.
        Expensive entries have decent WR and should stay conservative.
        """
        from config import settings
        if entry_price < 0.35:
            return settings.early_exit_threshold_low
        if entry_price < 0.50:
            return settings.early_exit_threshold_mid
        return settings.early_exit_threshold_high
```

**Step 2: Syntax check**

Run: `python -m py_compile strategy/live_trader.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add strategy/live_trader.py
git commit -m "feat: add get_exit_threshold helper for tiered exits"
```

---

### Task 3: Replace hardcoded 0.95 in main.py

**Files:**
- Modify: `main.py:1474-1485` (the early exit trigger block)

**Step 1: Compute tiered threshold and use it**

In `_monitor_early_exit`, replace lines 1474-1485:

```python
        # ---- EARLY EXIT TRIGGER ----
        # Sell live tokens when bid is high enough to lock in profit.
        # No time restriction — if someone offers 95%+ value at any point,
        # take it rather than risk a reversal.  Backtest on 288 windows showed
        # 0.95 is optimal: +$9.16 vs baseline (0.90 was only +$3.77).
        available_depth = best_bid_size + total_deep_size
        if (live_pos
                and not live_pos.get("exited")
                and not live_pos.get("exit_failed")
                and self.live_trader
                and best_bid >= 0.95
                and available_depth >= 20):
```

With:

```python
        # ---- EARLY EXIT TRIGGER ----
        # Tiered thresholds by entry price. Cheap entries (<0.50) have
        # low WR (13-25%) and benefit from aggressive exits. Expensive
        # entries keep the conservative 0.95 threshold.
        # Data: 640 trades, validated on live + paper + CLOB ground truth.
        exit_threshold = self.live_trader.get_exit_threshold(entry_price) if self.live_trader else 0.95
        available_depth = best_bid_size + total_deep_size
        if (live_pos
                and not live_pos.get("exited")
                and not live_pos.get("exit_failed")
                and self.live_trader
                and best_bid >= exit_threshold
                and available_depth >= 20):
```

**Step 2: Update the log line to show threshold and tier**

Replace the existing `[EARLY-EXIT SOLD]` log line (line ~1512-1517):

```python
                logger.info(
                    "[EARLY-EXIT SOLD] %s %s | bid=%.3f | tokens=%.1f | "
                    "sell=$%.2f buy=$%.2f | pnl=$%+.2f | %.0fs before settlement",
                    side, slug[-15:], best_bid, live_pos["tokens"],
                    sell_amount, buy_amount, pnl, seconds_left,
                )
```

With:

```python
                tier = "low" if entry_price < 0.35 else ("mid" if entry_price < 0.50 else "high")
                logger.info(
                    "[EARLY-EXIT SOLD] %s %s | bid=%.3f (threshold=%.2f, tier=%s) | "
                    "tokens=%.1f | sell=$%.2f buy=$%.2f | pnl=$%+.2f | %.0fs left",
                    side, slug[-15:], best_bid, exit_threshold, tier,
                    live_pos["tokens"],
                    sell_amount, buy_amount, pnl, seconds_left,
                )
```

**Step 3: Update Telegram alert to show threshold**

Replace the Telegram alert block (line ~1522-1528):

```python
                    await self.alerter._send(
                        f"<b>EARLY EXIT</b>\n\n"
                        f"Market: {slug}\n"
                        f"Side: {side} | Sold at ${best_bid:.3f}\n"
                        f"Tokens: {live_pos['tokens']:.1f} | Sell: ${sell_amount:.2f}\n"
                        f"PnL: <b>${pnl:+.2f}</b> | {seconds_left:.0f}s early"
                    )
```

With:

```python
                    tier_label = "low" if entry_price < 0.35 else ("mid" if entry_price < 0.50 else "high")
                    await self.alerter._send(
                        f"<b>EARLY EXIT</b> ({tier_label} tier)\n\n"
                        f"Market: {slug}\n"
                        f"Side: {side} | Sold at ${best_bid:.3f} (threshold {exit_threshold:.2f})\n"
                        f"Tokens: {live_pos['tokens']:.1f} | Sell: ${sell_amount:.2f}\n"
                        f"PnL: <b>${pnl:+.2f}</b> | {seconds_left:.0f}s early"
                    )
```

**Step 4: Syntax check**

Run: `python -m py_compile main.py`
Expected: No output (success)

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: use tiered early exit thresholds by entry price"
```

---

### Task 4: Deploy to VPS and verify

**Step 1: SCP modified files**

```bash
scp config.py main.py root@65.21.178.90:/home/btcedge/BTC-tool/
scp strategy/live_trader.py root@65.21.178.90:/home/btcedge/BTC-tool/strategy/
```

**Step 2: Restart service**

```bash
ssh root@65.21.178.90 "systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge"
```

**Step 3: Verify startup**

```bash
ssh root@65.21.178.90 "sleep 5 && tail -50 /home/btcedge/BTC-tool/btc_edge.log | strings"
```

Expected: Service starts without import errors. Look for normal startup log lines.

**Step 4: Verify tiered exit is active**

Wait for an early exit to trigger and check the log for the new format:

```bash
ssh root@65.21.178.90 "tail -c 50000 /home/btcedge/BTC-tool/btc_edge.log | strings | grep -i 'EARLY-EXIT'"
```

Expected: Log lines show `threshold=X.XX, tier=low/mid/high` format.

**Step 5: Quick sanity check — thresholds are what we expect**

```bash
ssh root@65.21.178.90 "cd /home/btcedge/BTC-tool && source venv/bin/activate && python -c \"
from config import settings
print(f'Low (<0.35):  {settings.early_exit_threshold_low}')
print(f'Mid (0.35-0.50): {settings.early_exit_threshold_mid}')
print(f'High (>=0.50): {settings.early_exit_threshold_high}')
\""
```

Expected:
```
Low (<0.35):  0.6
Mid (0.35-0.50): 0.65
High (>=0.50): 0.95
```
