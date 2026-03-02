# Telegram Bot Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Consolidate 13 Telegram commands to 10, merge overlapping commands, combine trade alerts, add `/recent` and `/today`, remove paper-only commands.

**Architecture:** Three files changed — `storage/db.py` (2 new query methods), `alerts/telegram.py` (replace 2 alert methods with 1 combined), `main.py` (rework commands, alert call sites, stats interval). Zero changes to trading logic.

**Tech Stack:** Python 3.11, aiosqlite, python-telegram-bot v21+

**Design doc:** `docs/plans/2026-03-02-telegram-cleanup-design.md`

---

### Task 1: Add DB query methods for `/recent` and `/today`

**Files:**
- Modify: `storage/db.py` (add 2 methods after line 698, end of `get_trading_stats`)

**Step 1: Add `get_recent_live_trades(n)` method**

Add after the `get_trading_stats` method (line 698):

```python
async def get_recent_live_trades(self, n: int = 5) -> list[dict]:
    """Return the last N settled live trades, most recent first."""
    try:
        cursor = await self._db.execute(
            "SELECT * FROM live_trades "
            "WHERE success = 1 AND outcome IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (n,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Failed to fetch recent live trades")
        return []
```

**Step 2: Add `get_today_live_trades()` method**

Add right after the previous method:

```python
async def get_today_live_trades(self) -> list[dict]:
    """Return all settled live trades from today (UTC)."""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(start_of_day.timestamp() * 1000)
        cursor = await self._db.execute(
            "SELECT * FROM live_trades "
            "WHERE success = 1 AND outcome IS NOT NULL "
            "AND timestamp >= ? "
            "ORDER BY timestamp",
            (start_ms,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Failed to fetch today's live trades")
        return []
```

**Step 3: Verify syntax**

Run: `python -m py_compile storage/db.py`
Expected: No output (clean compile)

**Step 4: Commit**

```bash
git add storage/db.py
git commit -m "Add get_recent_live_trades and get_today_live_trades DB queries"
```

---

### Task 2: Replace alert methods in telegram.py

**Files:**
- Modify: `alerts/telegram.py`

**Step 1: Replace `send_edge_alert` and `send_trade_alert` with `send_trade_placed_alert`**

Delete `send_edge_alert` (lines 114-134) and `send_trade_alert` (lines 136-162). Replace with:

```python
async def send_trade_placed_alert(
    self,
    side: str,
    slug: str,
    amount: float,
    entry_price: float,
    confidence: float,
    edge: float,
    order_id: str | None = None,
    success: bool = True,
    error_msg: str = "",
) -> None:
    """Send a combined trade placement notification (edge + order details)."""
    if success:
        side_emoji = "\U0001f7e2" if side == "UP" else "\U0001f534"
        text = (
            f"{side_emoji} <b>LIVE TRADE PLACED</b>\n\n"
            f"Market: {slug}\n"
            f"Side: <b>{side}</b> @ {entry_price:.3f}\n"
            f"Size: <b>${amount:.2f}</b>\n"
            f"Confidence: {confidence:.1%} | Edge: {edge:+.1%}\n"
            f"Order: {order_id or 'N/A'}"
        )
    else:
        text = (
            f"\u274c <b>LIVE TRADE FAILED</b>\n\n"
            f"Market: {slug}\n"
            f"Side: {side} @ {entry_price:.3f}\n"
            f"Amount: ${amount:.2f}\n"
            f"Error: {error_msg}"
        )
    await self._send(text)
```

**Step 2: Remove `send_live_trade_alert` (lines 227-253)**

Delete the entire `send_live_trade_alert` method — its functionality is now in `send_trade_placed_alert`.

**Step 3: Verify syntax**

Run: `python -m py_compile alerts/telegram.py`
Expected: No output (clean compile)

**Step 4: Commit**

```bash
git add alerts/telegram.py
git commit -m "Replace edge+trade alerts with combined send_trade_placed_alert"
```

---

### Task 3: Rework main.py commands — remove paper commands, merge stats

**Files:**
- Modify: `main.py`

This is the largest task. Do it in sub-steps, verifying syntax after each.

**Step 1: Remove command registrations (line 378-390)**

Remove these 4 lines from the `register_command` block:
```python
self.alerter.register_command("reset", self._cmd_reset)
self.alerter.register_command("budget", self._cmd_budget)
self.alerter.register_command("balance", self._cmd_balance)
self.alerter.register_command("livetrades", self._cmd_livetrades)
```

Add these 2 new registrations:
```python
self.alerter.register_command("recent", self._cmd_recent)
self.alerter.register_command("today", self._cmd_today)
```

**Step 2: Rework `_cmd_stats` (lines 448-485)**

Replace the entire method body. The new `/stats` absorbs `/livetrades` and `/balance`:

```python
async def _cmd_stats(self, args: str = "") -> str:
    """Handle /stats — comprehensive trading stats (live primary)."""
    parts = []

    # Live stats (primary)
    if self.live_trader and self.live_trader.is_active:
        ls = await self.db.get_live_trading_stats_full()
        balance = await self.live_trader.get_balance()
        session = self.live_trader.get_session_summary()
        roi = 0.0
        if self.live_trader.initial_bankroll > 0:
            roi = (self.live_trader.bankroll - self.live_trader.initial_bankroll) / self.live_trader.initial_bankroll
        ee_str = f" + {ls.get('early_exits', 0)}ee" if ls.get("early_exits") else ""
        mk_str = f" + {ls.get('maker_fills', 0)}mk" if ls.get("maker_fills") else ""

        balance_line = ""
        if balance is not None:
            balance_line = f"USDC: <b>${balance:.2f}</b>\n"

        parts.append(
            f"\U0001f4b5 <b>LIVE TRADING</b>\n"
            f"{balance_line}"
            f"Bankroll: <b>${self.live_trader.bankroll:,.2f}</b> | ROI: {roi:+.1%}\n"
            f"Settled: {ls.get('settled', 0)} | "
            f"W/L: {ls.get('wins', 0)}/{ls.get('losses', 0)}{ee_str}{mk_str}\n"
            f"Win rate: <b>{ls.get('win_rate', 0):.1%}</b>\n"
            f"P&amp;L: <b>${ls.get('total_pnl', 0):+.2f}</b>\n"
            f"Volume: ${ls.get('total_amount', 0):,.2f}\n"
            f"Max bet: ${settings.max_live_bet_usdc:.2f}\n"
            f"Session: {session.get('successful', 0)} filled / "
            f"{session.get('total', 0)} total"
        )

    # Paper stats (secondary — one compact line)
    if self.paper_trader:
        stats = await self.paper_trader.get_stats()
        parts.append(
            f"\n\U0001f4dd Paper: "
            f"{stats.get('wins', 0)}W/{stats.get('losses', 0)}L "
            f"({stats.get('win_rate', 0):.0%}) "
            f"${stats.get('total_pnl', 0):+.2f}"
        )

    if not parts:
        return "No trading data."
    return f"\U0001f4c8 <b>STATS</b>\n\n" + "\n\n".join(parts)
```

**Step 3: Add `_cmd_recent` method**

Add after `_cmd_stats`:

```python
async def _cmd_recent(self, args: str = "") -> str:
    """Handle /recent [N] — show last N settled live trades."""
    n = 5
    if args.strip():
        try:
            n = min(max(int(args.strip()), 1), 10)
        except ValueError:
            return "Usage: /recent [1-10]"

    trades = await self.db.get_recent_live_trades(n)
    if not trades:
        return "\U0001f4dd No settled live trades yet."

    lines = [f"\U0001f4dd <b>LAST {len(trades)} TRADES</b>\n"]
    now_ms = int(time.time() * 1000)
    for t in trades:
        outcome = t.get("outcome", "?")
        icon = "\u2705" if outcome == "WIN" else ("\u274c" if outcome == "LOSS" else "\U0001f4b0")
        pnl = t.get("pnl", 0) or 0
        entry = t.get("entry_price", 0) or 0
        side = t.get("side", "?")
        ago_min = (now_ms - t["timestamp"]) / 60000
        if ago_min < 60:
            ago_str = f"{ago_min:.0f}m ago"
        elif ago_min < 1440:
            ago_str = f"{ago_min / 60:.1f}h ago"
        else:
            ago_str = f"{ago_min / 1440:.1f}d ago"
        lines.append(
            f"{icon} {side} @ {entry:.3f} | "
            f"${pnl:+.2f} | {ago_str}"
        )
    return "\n".join(lines)
```

**Step 4: Add `_cmd_today` method**

Add after `_cmd_recent`:

```python
async def _cmd_today(self, args: str = "") -> str:
    """Handle /today — today's trading performance (UTC)."""
    trades = await self.db.get_today_live_trades()
    if not trades:
        return "\U0001f4c5 No live trades today (UTC)."

    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    early_exits = sum(1 for t in trades if t["outcome"] == "EARLY_EXIT")
    total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)

    # Best and worst trade
    best = max(trades, key=lambda t: t.get("pnl", 0) or 0)
    worst = min(trades, key=lambda t: t.get("pnl", 0) or 0)

    wl_total = wins + losses
    wr = (wins / wl_total * 100) if wl_total > 0 else 0

    ee_str = f" + {early_exits}ee" if early_exits else ""
    lines = [
        f"\U0001f4c5 <b>TODAY (UTC)</b>\n",
        f"Trades: {len(trades)} | W/L: {wins}/{losses}{ee_str} ({wr:.0f}%)",
        f"P&amp;L: <b>${total_pnl:+.2f}</b>",
        f"Best: ${best.get('pnl', 0) or 0:+.2f} ({best['side']} @ {best.get('entry_price', 0) or 0:.3f})",
        f"Worst: ${worst.get('pnl', 0) or 0:+.2f} ({worst['side']} @ {worst.get('entry_price', 0) or 0:.3f})",
    ]
    return "\n".join(lines)
```

**Step 5: Delete the removed command methods**

Delete these entire methods:
- `_cmd_reset` (lines 528-567)
- `_cmd_budget` (lines 569-610)
- `_cmd_balance` (lines 864-889)
- `_cmd_livetrades` (lines 891-918)

**Step 6: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (clean compile)

**Step 7: Commit**

```bash
git add main.py
git commit -m "Rework /stats, add /recent and /today, remove paper commands"
```

---

### Task 4: Merge alert call sites in main.py

**Files:**
- Modify: `main.py` (lines 1364-1380)

**Step 1: Replace the two alert calls with one combined call**

Current code (lines 1364-1380):
```python
            # Telegram alert
            if self.alerter:
                await self.alerter.send_live_trade_alert(
                    side=signal["side"],
                    slug=signal["market_slug"],
                    amount=live_result["amount"],
                    order_id=live_result.get("order_id"),
                    success=live_result["success"],
                    error_msg=live_result.get("error", ""),
                )

    # Telegram edge alert (only fires once per market — when trade is placed)
    if self.alerter:
        await self.alerter.send_edge_alert(
            signal=signal,
            features_breakdown=signal.get("signals", {}),
        )
```

Replace with:
```python
            # Telegram alert — combined trade + signal info
            if self.alerter:
                await self.alerter.send_trade_placed_alert(
                    side=signal["side"],
                    slug=signal["market_slug"],
                    amount=live_result["amount"],
                    entry_price=signal["entry_price"],
                    confidence=signal.get("confidence", 0.0),
                    edge=signal.get("edge", 0.0),
                    order_id=live_result.get("order_id"),
                    success=live_result["success"],
                    error_msg=live_result.get("error", ""),
                )
```

Note: The standalone edge alert block at the outer indentation level (lines 1375-1380) is **deleted entirely**. This means paper-only and exploration signals no longer trigger Telegram alerts — exactly as designed.

**Step 2: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (clean compile)

**Step 3: Commit**

```bash
git add main.py
git commit -m "Merge edge+trade alert into single send_trade_placed_alert call"
```

---

### Task 5: Change stats loop from 30 min to 60 min

**Files:**
- Modify: `main.py` (line 2172)

**Step 1: Change interval**

Change line 2172 from:
```python
            await asyncio.sleep(1800)  # 30 minutes
```
To:
```python
            await asyncio.sleep(3600)  # 60 minutes
```

**Step 2: Update docstring**

Change line 2170 from:
```python
    """Periodically log and send trading stats (every 30 minutes)."""
```
To:
```python
    """Periodically log and send trading stats (every 60 minutes)."""
```

**Step 3: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (clean compile)

**Step 4: Commit**

```bash
git add main.py
git commit -m "Change periodic stats from 30-min to 60-min interval"
```

---

### Task 6: Full syntax check and final commit

**Step 1: Syntax check all modified files**

Run:
```bash
python -m py_compile storage/db.py alerts/telegram.py main.py
```
Expected: No output (all clean)

**Step 2: Verify no broken references**

Search for any remaining references to the deleted methods:
```bash
grep -rn "send_edge_alert\|send_trade_alert\|send_live_trade_alert\|_cmd_reset\|_cmd_budget\|_cmd_balance\|_cmd_livetrades" main.py alerts/telegram.py
```
Expected: No matches (all references cleaned up)

**Step 3: Review the full diff**

```bash
git diff --stat HEAD~5
```

Verify: Only 3 files changed (`storage/db.py`, `alerts/telegram.py`, `main.py`). No trading logic files.

---

### Task 7: Deploy to VPS and verify

**Step 1: Deploy changed files**

```bash
scp storage/db.py root@65.21.178.90:/home/btcedge/BTC-tool/storage/
scp alerts/telegram.py root@65.21.178.90:/home/btcedge/BTC-tool/alerts/
scp main.py root@65.21.178.90:/home/btcedge/BTC-tool/
```

**Step 2: Restart service**

```bash
ssh root@65.21.178.90 "systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge"
```

**Step 3: Verify startup**

```bash
ssh root@65.21.178.90 "sleep 5 && tail -50 /home/btcedge/BTC-tool/btc_edge.log | strings"
```

Look for: "BTC Polymarket Edge Finder started" in logs, no import errors.

**Step 4: Test commands in Telegram**

Send to the bot:
- `/help` — should show 10 commands (not 13)
- `/stats` — should show combined view with USDC balance, bankroll, W/L, session info
- `/recent` — should show last 5 settled trades
- `/today` — should show today's performance
- `/reset` — should say "Unknown command"
- `/budget` — should say "Unknown command"

**Step 5: Wait for next trade cycle**

Monitor that:
- Live trade placed → one combined message (not two)
- Paper-only/exploration signal → no Telegram alert
- Skip notification → still fires as before
