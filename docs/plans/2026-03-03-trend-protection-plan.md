# Trend Protection & Settlement Accuracy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix settlement timing bug, then add regime-aware live trade flipping and loss streak protection to stop bleeding money in trending markets.

**Architecture:** Three changes layered in order: (1) price buffer in Chainlink stream so settlement uses the correct boundary price, (2) extend existing paper-only regime flip to live trades, (3) new loss streak detector that pauses a side after consecutive losses. All thresholds configurable via .env. All actions tagged in DB for later analysis.

**Tech Stack:** Python 3.11+, asyncio, collections.deque, pydantic-settings config

**Design doc:** `docs/plans/2026-03-03-trend-protection-design.md`

---

### Task 1: Add Chainlink Price Buffer

**Files:**
- Modify: `data/polymarket.py:95-115` (add buffer to `__init__`)
- Modify: `data/polymarket.py:525-537` (add `get_chainlink_price_at` method)
- Modify: `data/polymarket.py:595-612` (append to buffer on each update)

**Step 1: Add the buffer and lookup method to PolymarketClient**

In `data/polymarket.py`, add a `deque` import at the top and initialize the buffer in `__init__`:

```python
from collections import deque
```

In `__init__` (after line 104), add:

```python
        # Rolling buffer of (unix_timestamp, price) for boundary-accurate settlement
        self._chainlink_price_buffer: deque[tuple[float, float]] = deque(maxlen=120)
```

**Step 2: Append to buffer on each Chainlink update**

In `run_chainlink_stream()`, after line 607 (`self._chainlink_stream_ts = time.time()`), add:

```python
                                        self._chainlink_price_buffer.append(
                                            (self._chainlink_stream_ts, self._chainlink_stream_price)
                                        )
```

**Step 3: Add the boundary-lookup method**

After the `get_chainlink_stream_price` method (after line 537), add:

```python
    def get_chainlink_price_at(self, target_time: float) -> tuple[float | None, float]:
        """Return the Chainlink price closest to target_time.

        Returns (price, delta_seconds) where delta_seconds is how far the
        sample was from target_time.  Returns (None, 0) if buffer is empty.
        """
        if not self._chainlink_price_buffer:
            return None, 0.0
        best_ts, best_price = min(
            self._chainlink_price_buffer, key=lambda tp: abs(tp[0] - target_time)
        )
        return best_price, abs(best_ts - target_time)
```

**Step 4: Commit**

```
feat: add Chainlink price buffer for boundary-accurate settlement
```

---

### Task 2: Use Price Buffer in Settlement

**Files:**
- Modify: `main.py:1536-1576` (`_settle_previous_window`)

**Step 1: Compute window end time and use buffer lookup**

In `_settle_previous_window()`, replace lines 1542-1547:

```python
        # Get BTC end price for paper trading + logging
        btc_end = self.polymarket.get_chainlink_stream_price()
        price_source = "Chainlink Stream"
        if btc_end is None:
            btc_end = self.binance.get_latest_price()
            price_source = "Binance (fallback)"
```

With:

```python
        # Get BTC end price — prefer boundary-accurate buffer lookup
        window_end_time = self._window_start_time + 300.0  # 5-min window
        btc_end, price_delta = self.polymarket.get_chainlink_price_at(window_end_time)
        if btc_end is not None:
            price_source = f"Chainlink Buffer (delta={price_delta:.1f}s)"
        else:
            btc_end = self.polymarket.get_chainlink_stream_price()
            price_source = "Chainlink Stream"
        if btc_end is None:
            btc_end = self.binance.get_latest_price()
            price_source = "Binance (fallback)"
```

**Step 2: Verify syntax**

Run: `python -m py_compile main.py && python -m py_compile data/polymarket.py`

**Step 3: Commit**

```
feat: use Chainlink price buffer for boundary-accurate settlement
```

---

### Task 3: Add Config for Regime Flip and Streak Guard

**Files:**
- Modify: `config.py:60-63` (add new settings)

**Step 1: Add new config fields**

After the existing `regime_flip_threshold` line (line 63), add:

```python
    # Regime flip — applies to live trades (not just paper) when trending
    regime_flip_live: bool = True         # enable regime flip for live trades

    # Loss streak guard — pause a side after consecutive losses
    streak_pause_threshold: int = 3       # consecutive same-side losses to trigger
    streak_pause_windows: int = 2         # number of 5-min windows to pause (~10 min)
```

Also change the `regime_flip_threshold` default from `0.40` to `0.35` (line 63):

```python
    regime_flip_threshold: float = 0.35
```

**Step 2: Verify syntax**

Run: `python -m py_compile config.py`

**Step 3: Commit**

```
feat: add config for live regime flip and streak guard
```

---

### Task 4: Enable Regime Flip for Live Trades

**Files:**
- Modify: `main.py:1146-1163` (extend flip logic scope)
- Modify: `main.py:1249-1268` (use flipped signal for live trades)

**Step 1: Rename paper_flip_signal to flip_signal (it now applies to both)**

In the regime flip block (lines 1146-1163), change `paper_flip_signal` references:

Replace line 1149:
```python
        paper_flip_signal = None  # None = use model's signal; dict = use flipped signal
```
With:
```python
        flip_signal = None  # None = use model's signal; dict = use flipped signal
```

Replace line 1157:
```python
                    paper_flip_signal = flipped
```
With:
```python
                    flip_signal = flipped
```

Replace line 1158-1163:
```python
                    logger.info(
                        "Paper FLIP: %s -> %s (regime=%s, str=%.2f)",
                        signal["side"], trend_side,
                        self._current_regime.regime,
                        self._current_regime.strength,
                    )
```
With:
```python
                    logger.info(
                        "REGIME FLIP: %s -> %s (regime=%s, str=%.2f, live=%s)",
                        signal["side"], trend_side,
                        self._current_regime.regime,
                        self._current_regime.strength,
                        "yes" if settings.regime_flip_live else "paper-only",
                    )
```

**Step 2: Use flip_signal for paper trades (update variable name)**

Replace line 1242:
```python
            paper_signal = paper_flip_signal if paper_flip_signal else signal
```
With:
```python
            paper_signal = flip_signal if flip_signal else signal
```

**Step 3: Use flip_signal for live trades when enabled**

Replace line 1251 (the live trade block):
```python
        if self.live_trader and self.live_trader.is_active and not self.live_trader.is_paused and not signal.get("exploration"):
```
With:
```python
        live_signal = signal  # default: model's signal
        if flip_signal and settings.regime_flip_live:
            live_signal = flip_signal
        if self.live_trader and self.live_trader.is_active and not self.live_trader.is_paused and not live_signal.get("exploration"):
```

Then in the live trade block, replace all references to `signal` with `live_signal`.
The key lines to change (each occurrence of `signal[` in the block from ~1252-1319):

- Line 1255: `if market:` stays
- Line 1257: `market.up_token_id if signal["side"] == "UP"` → `live_signal["side"]`
- Line 1258: `else market.down_token_id` stays
- Line 1261: `confidence=signal.get("confidence", 0.0),` → `live_signal.get(...)`
- Line 1264: `side=signal["side"],` → `live_signal["side"]`
- Line 1267: `market_slug=signal["market_slug"],` → `live_signal["market_slug"]`
- Line 1268: `entry_price=signal["entry_price"],` → `live_signal["entry_price"]`
- Line 1272: Set trade_tag for flipped trades:

```python
                trade_tag = "regime_flip" if (flip_signal and settings.regime_flip_live) else None
```

- Lines 1276-1287: Replace `signal` references with `live_signal`
- Lines 1310-1319 (Telegram alert): Replace `signal` references with `live_signal`

**Step 4: Verify syntax**

Run: `python -m py_compile main.py`

**Step 5: Commit**

```
feat: enable regime flip for live trades (strength >= 0.35)
```

---

### Task 5: Add Loss Streak Guard

**Files:**
- Modify: `main.py:120-155` (add streak state to `__init__`)
- Modify: `main.py:1097-1130` (add streak check in safety filters)
- Modify: `main.py:1700-1710` (update streak state on settlement)
- Modify: `main.py:1450-1460` (update streak state on early exit)

**Step 1: Add streak tracking state to BTCEdge.__init__**

After `self._window_skip_reason` (around line 154), add:

```python
        # Loss streak guard — pause a side after consecutive same-side losses
        # Only tracks our taker trades (not maker_fills) for accuracy
        self._side_outcomes: dict[str, list[str]] = {"UP": [], "DOWN": []}  # last N outcomes per side
        self._side_paused: dict[str, float] = {}  # side -> pause expiry timestamp
```

**Step 2: Add streak check in the safety filter chain**

After the hour blacklist check (line 1128 `return`), add a new filter block:

```python
        # 6d2. Loss streak guard — pause a side after consecutive losses.
        #      Acts as early warning for potential trend onset before regime
        #      detector has enough data (~20 min lag).
        trade_side = signal["side"]
        if flip_signal and settings.regime_flip_live:
            trade_side = flip_signal["side"]
        if trade_side in self._side_paused:
            if time.time() < self._side_paused[trade_side]:
                self._window_skip_reason = (
                    f"streak guard ({settings.streak_pause_threshold}x {trade_side} LOSS)"
                )
                logger.info("Skipping edge — %s", self._window_skip_reason)
                return
            else:
                # Pause expired — clear it
                del self._side_paused[trade_side]
                logger.info("Streak guard expired for %s — resuming", trade_side)
```

Note: this must go BEFORE the regime flip logic (6e2) so that a paused side stays
paused even if regime would flip it. The regime flip handles the *next* trade after
the pause expires naturally.

**Step 3: Update streak state on live trade settlement**

In `_settle_live_trades_for_window()` (around line 1706, after `self.live_trader.record_settlement(won, pnl)`), add:

```python
            # Update streak guard (skip maker_fills — only track our taker trades)
            trade_tag = row.get("trade_tag")
            if trade_tag not in ("maker_fill",):
                self._side_outcomes[side].append(outcome)
                # Keep only last 10
                if len(self._side_outcomes[side]) > 10:
                    self._side_outcomes[side] = self._side_outcomes[side][-10:]
                # Check for consecutive losses
                recent = self._side_outcomes[side]
                threshold = settings.streak_pause_threshold
                if (len(recent) >= threshold
                        and all(r == "LOSS" for r in recent[-threshold:])):
                    pause_seconds = settings.streak_pause_windows * 300
                    self._side_paused[side] = time.time() + pause_seconds
                    logger.warning(
                        "STREAK GUARD: %dx %s LOSS — pausing %s for %d windows (%.0fs)",
                        threshold, side, side, settings.streak_pause_windows, pause_seconds,
                    )
                    if self.alerter:
                        try:
                            await self.alerter._send(
                                f"STREAK GUARD: {threshold}x {side} LOSS\n"
                                f"Pausing {side} for {settings.streak_pause_windows} windows",
                                parse_mode=None,
                            )
                        except Exception:
                            pass
```

**Step 4: Reset streak on early exit settlement**

In the early exit success block (around line 1458, after `self.live_trader.record_settlement(pnl > 0, pnl)`), add:

```python
                # Early exit breaks loss streak (it was a profitable rescue)
                side = live_pos["side"]
                if side in self._side_outcomes:
                    self._side_outcomes[side].append("EARLY_EXIT")
                    if len(self._side_outcomes[side]) > 10:
                        self._side_outcomes[side] = self._side_outcomes[side][-10:]
```

**Step 5: Add `post_streak` tag for first trade after pause**

Back in the streak check filter (Step 2), when the pause expires, set a flag:

```python
            else:
                # Pause expired — clear it, tag next trade
                del self._side_paused[trade_side]
                logger.info("Streak guard expired for %s — resuming", trade_side)
                signal["_post_streak"] = True
```

Then in the live trade block (around line 1272 where trade_tag is set):

```python
                trade_tag = None
                if flip_signal and settings.regime_flip_live:
                    trade_tag = "regime_flip"
                elif live_signal.get("_post_streak"):
                    trade_tag = "post_streak"
```

**Step 6: Verify syntax**

Run: `python -m py_compile main.py`

**Step 7: Commit**

```
feat: add loss streak guard — pause side after 3 consecutive losses
```

---

### Task 6: Update .env and CLAUDE.md

**Files:**
- Modify: `.env` on VPS (add new config)
- Modify: `CLAUDE.md` (document new features)

**Step 1: Add new config to .env on VPS**

```env
# Regime flip for live trades (not just paper)
REGIME_FLIP_LIVE=true
REGIME_FLIP_THRESHOLD=0.35

# Loss streak guard
STREAK_PAUSE_THRESHOLD=3
STREAK_PAUSE_WINDOWS=2
```

**Step 2: Update CLAUDE.md**

Add to the Safety Filters section:
- Streak guard description
- Regime flip for live trades

Add to Configuration section:
- New env vars

**Step 3: Commit**

```
docs: document regime flip and streak guard in CLAUDE.md
```

---

### Task 7: Syntax Check, Deploy, Verify

**Step 1: Full syntax check**

```bash
python -m py_compile main.py config.py data/models.py data/binance_ws.py data/polymarket.py signals/indicators.py signals/features.py signals/probability.py signals/ml_probability.py signals/regime.py strategy/edge.py strategy/paper_trader.py strategy/live_trader.py alerts/telegram.py storage/db.py
```

**Step 2: Deploy to VPS**

```bash
scp config.py main.py root@65.21.178.90:/home/btcedge/BTC-tool/
scp data/polymarket.py root@65.21.178.90:/home/btcedge/BTC-tool/data/
```

**Step 3: Update .env on VPS**

```bash
ssh root@65.21.178.90 "cat >> /home/btcedge/BTC-tool/.env << 'EOF'

# Regime flip for live trades
REGIME_FLIP_LIVE=true
REGIME_FLIP_THRESHOLD=0.35

# Loss streak guard
STREAK_PAUSE_THRESHOLD=3
STREAK_PAUSE_WINDOWS=2
EOF"
```

**Step 4: Restart service**

```bash
ssh root@65.21.178.90 "systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge"
```

**Step 5: Verify startup**

```bash
ssh root@65.21.178.90 "sleep 10 && tail -50 /home/btcedge/BTC-tool/btc_edge.log"
```

Check for:
- No crash / traceback
- "Chainlink stream: BTC/USD $..." (stream connected)
- "New window: ..." (pipeline running)
- Price buffer entries being logged

**Step 6: Monitor first few windows**

```bash
ssh root@65.21.178.90 "tail -f /home/btcedge/BTC-tool/btc_edge.log | grep -E 'SETTLED|FLIP|STREAK|Buffer'"
```

Look for:
- Settlement lines with `Chainlink Buffer (delta=Xs)` — delta should be < 30s
- Any `REGIME FLIP` lines if market is trending
- No `STREAK GUARD` lines unless there are actual losing streaks

**Step 7: Commit all remaining changes**

```
chore: deploy trend protection to VPS
```
