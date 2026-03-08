# FOK Orders Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Switch buy and EE sell orders from GTC to FOK to eliminate maker fill drag (-$40/week and worsening).

**Architecture:** Change `OrderType.GTC` to `OrderType.FOK` in two places (buy + EE sell), handle FOK rejections by logging to `skipped_windows`, keep auto-sell on GTC. Add canary warning to maker fill sync.

**Tech Stack:** py-clob-client (`OrderType.FOK`), SQLite (skipped_windows), existing logging.

---

### Task 1: Switch buy orders from GTC to FOK

**Files:**
- Modify: `strategy/live_trader.py:470`
- Modify: `strategy/live_trader.py:378` (docstring)

**Step 1: Change OrderType on buy**

In `strategy/live_trader.py`, line 470, change:
```python
            resp = await self._post_order_with_retry(signed_order, OrderType.GTC)
```
to:
```python
            resp = await self._post_order_with_retry(signed_order, OrderType.FOK)
```

**Step 2: Update the docstring**

In `strategy/live_trader.py`, line 378, change:
```python
        """Place a GTC market buy order on Polymarket.
```
to:
```python
        """Place a FOK market buy order on Polymarket.
```

**Step 3: Verify syntax**

Run: `python -m py_compile strategy/live_trader.py`
Expected: no output (success)

---

### Task 2: Handle FOK rejection on buy orders

When FOK rejects (not enough liquidity), `place_order` currently treats non-matched orders as phantom/failed. But we need the rejection to propagate cleanly back to `main.py` so it can log to `skipped_windows`.

**Files:**
- Modify: `strategy/live_trader.py:470-489` (place_order FOK handling)
- Modify: `main.py:1475-1488` (caller: log FOK rejection to skipped_windows)

**Step 1: Add FOK-specific error message in place_order**

The existing phantom order detection (lines 491-533) already handles this: if `size_matched == 0` or `status == "CANCELED"`, it sets `result["success"] = False` with an error message. FOK rejections will hit this path naturally.

Add a distinct error tag so the caller can distinguish FOK rejection from other failures. In `strategy/live_trader.py`, after line 508 (inside the `matched == 0 or clob_status == "CANCELED"` block), add a flag:

```python
                            result["fok_rejected"] = True
```

Insert this line right after the `result["error"] = (...)` assignment at line 505-508, before the logger.warning. The full block becomes:

```python
                        if matched == 0 or clob_status == "CANCELED":
                            result["success"] = False
                            result["error"] = (
                                f"Order {clob_status} with 0 tokens matched "
                                f"(original_size={order_status.get('original_size', '?')})"
                            )
                            result["fok_rejected"] = True
```

Also remove the cancel logic at lines 514-528 — FOK orders don't leave remainders, so there's nothing to cancel:

```python
                            # FOK orders don't leave remainders — no cancel needed.
```

**Step 2: Log FOK rejection as skipped window in main.py**

In `main.py`, after the `place_order` call (around line 1481), handle FOK rejection. Change the block at lines 1475-1488 from:

```python
                live_result = await self.live_trader.place_order(
                    token_id=token_id,
                    amount_usdc=live_amount,
                    side=live_signal["side"],
                    market_slug=live_signal["market_slug"],
                    entry_price=live_signal["entry_price"],
                )

                # Cancel any remaining open orders immediately after fill.
                # GTC orders that don't fully fill leave a resting remainder
                # on the book that can get hit later as unmanaged maker fills
                # (no confidence/regime/streak checks).
                if live_result["success"]:
                    await self.live_trader.cancel_all_orders()
```

to:

```python
                live_result = await self.live_trader.place_order(
                    token_id=token_id,
                    amount_usdc=live_amount,
                    side=live_signal["side"],
                    market_slug=live_signal["market_slug"],
                    entry_price=live_signal["entry_price"],
                )

                # FOK rejection — log as skipped window for analysis
                if live_result.get("fok_rejected"):
                    self._window_skip_reason = (
                        f"FOK rejected (insufficient liquidity) "
                        f"${live_amount:.2f} @ ${live_signal['entry_price']:.2f}"
                    )
                    logger.info("FOK rejected — %s", self._window_skip_reason)

                # Safety: cancel any lingering orders (shouldn't exist with FOK,
                # but kept as a belt-and-suspenders check)
                if live_result["success"]:
                    await self.live_trader.cancel_all_orders()
```

**Step 3: Verify syntax**

Run: `python -m py_compile strategy/live_trader.py && python -m py_compile main.py`
Expected: no output (success)

---

### Task 3: Switch EE sell orders from GTC to FOK

**Files:**
- Modify: `strategy/live_trader.py:727`

**Step 1: Change OrderType on EE sell**

In `strategy/live_trader.py`, line 727, change:
```python
            resp = await self._post_order_with_retry(signed_order, OrderType.GTC)
```
to:
```python
            resp = await self._post_order_with_retry(signed_order, OrderType.FOK)
```

**Step 2: Verify syntax**

Run: `python -m py_compile strategy/live_trader.py`
Expected: no output (success)

---

### Task 4: Add canary warning to maker fill sync

**Files:**
- Modify: `main.py:2274` (inside `_sync_maker_fills`)

**Step 1: Add warning when new maker fills are found**

In `main.py`, in the `_sync_maker_fills` method, find the block at line 2274 that inserts new fills:

```python
            # Insert new maker fills into DB
            for fill in new_fills:
```

Add a warning log before the insert loop:

```python
            # Insert new maker fills into DB
            # Post-FOK switch: new maker fills should not appear.
            # If they do, FOK is not working as expected.
            logger.warning(
                "UNEXPECTED: %d new maker fills found after FOK switch! "
                "FOK should prevent all maker fills. Investigate.",
                len(new_fills),
            )
            for fill in new_fills:
```

**Step 2: Verify syntax**

Run: `python -m py_compile main.py`
Expected: no output (success)

---

### Task 5: Test FOK on VPS before full deploy

**Step 1: Deploy changes to VPS**

```bash
scp strategy/live_trader.py root@65.21.178.90:/home/btcedge/BTC-tool/strategy/
scp main.py root@65.21.178.90:/home/btcedge/BTC-tool/
```

**Step 2: Quick syntax check on VPS**

```bash
ssh root@65.21.178.90 "cd /home/btcedge/BTC-tool && source venv/bin/activate && python -m py_compile strategy/live_trader.py && python -m py_compile main.py && echo OK"
```

Expected: `OK`

**Step 3: Restart service**

```bash
ssh root@65.21.178.90 "systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge"
```

**Step 4: Verify startup and first trade**

```bash
ssh root@65.21.178.90 "sleep 10 && tail -50 /home/btcedge/BTC-tool/btc_edge.log | strings"
```

Watch for:
- Clean startup (no import errors)
- First trade attempt uses FOK (look for "LIVE ORDER FILLED" or "FOK rejected")
- No crash on FOK rejection

**Step 5: Monitor for 30 min**

```bash
ssh root@65.21.178.90 "tail -c 10000 /home/btcedge/BTC-tool/btc_edge.log | strings | grep -iE '(FOK|FILLED|REJECTED|PHANTOM|maker fill)'"
```

Check:
- FOK orders are filling successfully
- No "UNEXPECTED: new maker fills" warnings
- FOK rejections (if any) are logged cleanly

---

### Task 6: Update CLAUDE.md and commit

**Files:**
- Modify: `CLAUDE.md` (changelog + convention updates)

**Step 1: Update changelog**

Add to changelog table:
```
| 2026-03-08 ~XX:XX | **FOK orders**: Switched buy + EE sell from GTC to FOK. Eliminates maker fill drag (-$40/wk). Auto-sell stays GTC. FOK rejections logged to skipped_windows. Maker fill sync kept as canary. | `docs/plans/2026-03-08-fok-orders-design.md` |
```

**Step 2: Update Danger Zone section**

In the "Danger Zone: py-clob-client Landmines" section, add:
```
4. **FOK for buys/EE, GTC for auto-sell only**: Buy and EE sell orders use FOK (Fill Or Kill) to prevent maker fill drag. Auto-sell (post-resolution) keeps GTC because post-resolution liquidity is thin and resting sells at $0.99 are harmless.
```

**Step 3: Commit**

```bash
git add strategy/live_trader.py main.py CLAUDE.md docs/plans/2026-03-08-fok-orders-design.md docs/plans/2026-03-08-fok-orders-plan.md
git commit -m "feat: switch buy + EE sell orders from GTC to FOK

Eliminates maker fill drag (358 all-time, -$11.67 and worsening at
-$40/week). FOK = fill entire order immediately or cancel, no resting
orders on the book.

- Buy orders: GTC -> FOK
- EE sell orders: GTC -> FOK
- Auto-sell (post-resolution): stays GTC (thin liquidity, no risk)
- FOK rejections logged to skipped_windows for analysis
- Maker fill sync kept as canary (warns if new fills appear)

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```
