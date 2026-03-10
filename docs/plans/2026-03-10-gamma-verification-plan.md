# Gamma Verification System Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate phantom WINs (12.7% error rate) by verifying every settlement against Gamma API 5 minutes after window close, with hourly reconciliation and balance sanity checks as safety nets.

**Architecture:** Three layers: (1) delayed async verification task fires 5 min after each Chainlink-fallback settlement, queries Gamma, corrects WIN/LOSS if wrong; (2) hourly reconciliation re-checks any trades still missing Gamma confirmation; (3) periodic balance sanity check compares DB-calculated state to actual Polymarket wallet.

**Tech Stack:** Python 3.11+, asyncio, aiohttp (Gamma API), aiosqlite (DB), py-clob-client (balance query)

**Spec:** `docs/plans/2026-03-10-gamma-verification-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `storage/db.py` | Modify | Add `gamma_resolution`/`gamma_winner_matches` migration columns, `get_unverified_trades()`, `correct_trade_outcome()`, `get_recent_outcomes()` |
| `main.py` | Modify | Add `_verify_settlement_gamma()`, `_gamma_reconciliation_loop()`, `_balance_sanity_check()`, modify `_settle_previous_window()` to schedule verification, modify `_stats_loop()` to call reconciliation + balance check, add `_rebuild_streak_state()` |
| `config.py` | Modify | Add `GAMMA_VERIFY_DELAY_SECONDS` setting (default 300) |

No new files. All changes go into existing modules following established patterns.

---

## Chunk 1: DB Layer

### Task 1: Add gamma columns to migration

**Files:**
- Modify: `storage/db.py:236` (after `would_have_won` line in `_migrate_spread_columns`)

- [ ] **Step 1: Add gamma migration columns**

In `storage/db.py`, add two entries to the `alter_statements` list in `_migrate_spread_columns()`, right after the `("live_trades", "would_have_won", "INTEGER")` line (line 237):

```python
            # Gamma verification (2026-03-10)
            ("live_trades", "gamma_resolution", "TEXT"),
            ("live_trades", "gamma_winner_matches", "INTEGER"),
```

This ensures the columns exist on startup (the backfill script adds them separately, but the main app should be self-sufficient).

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile storage/db.py`
Expected: No output (clean compile)

- [ ] **Step 3: Commit**

```bash
git add storage/db.py
git commit -m "db: add gamma_resolution and gamma_winner_matches migration columns"
```

### Task 2: Add `get_unverified_trades()` query

**Files:**
- Modify: `storage/db.py` (add method after `get_unsettled_live_trades` at line ~787)

- [ ] **Step 1: Add the query method**

Add this method to the `Database` class after `get_unsettled_live_trades()`:

```python
    async def get_unverified_trades(self, min_age_seconds: int = 600) -> list[dict]:
        """Return settled trades that lack Gamma verification.

        Only returns trades older than min_age_seconds (default 10 min)
        to avoid re-checking trades that Layer 1 is about to verify.
        Excludes EARLY_EXIT (settled by CLOB sell, not by resolution).
        """
        try:
            cutoff_ms = int((time.time() - min_age_seconds) * 1000)
            cursor = await self._db.execute(
                """
                SELECT * FROM live_trades
                WHERE success = 1
                  AND outcome IN ('WIN', 'LOSS')
                  AND gamma_resolution IS NULL
                  AND settled_at IS NOT NULL
                  AND settled_at < ?
                """,
                (cutoff_ms,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to fetch unverified trades")
            return []
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile storage/db.py`
Expected: No output (clean compile)

- [ ] **Step 3: Commit**

```bash
git add storage/db.py
git commit -m "db: add get_unverified_trades() for Gamma reconciliation"
```

### Task 3: Add `correct_trade_outcome()` method

**Files:**
- Modify: `storage/db.py` (add method after `get_unverified_trades`)

- [ ] **Step 1: Add the correction method**

Add this method to the `Database` class right after `get_unverified_trades()`:

```python
    async def correct_trade_outcome(
        self,
        trade_id: int,
        new_outcome: str,
        new_pnl: float,
        gamma_resolution: str,
        gamma_winner_matches: int,
    ) -> None:
        """Correct a trade's outcome after Gamma verification.

        Updates outcome, pnl, and stamps gamma verification fields.
        Used when Gamma API disagrees with the original Chainlink-based settlement.
        """
        try:
            await self._db.execute(
                """
                UPDATE live_trades
                SET outcome = ?, pnl = ?,
                    gamma_resolution = ?, gamma_winner_matches = ?
                WHERE id = ?
                """,
                (new_outcome, new_pnl, gamma_resolution, gamma_winner_matches, trade_id),
            )
            await self._db.commit()
            logger.warning(
                "CORRECTED live trade %d: outcome=%s pnl=%.4f gamma=%s",
                trade_id, new_outcome, new_pnl, gamma_resolution,
            )
        except Exception:
            logger.exception("Failed to correct trade %d", trade_id)
```

- [ ] **Step 2: Add `stamp_gamma_verification()` for trades that are already correct**

Add this method right after `correct_trade_outcome()`:

```python
    async def stamp_gamma_verification(
        self,
        trade_id: int,
        gamma_resolution: str,
        gamma_winner_matches: int,
    ) -> None:
        """Stamp Gamma verification on a trade without changing outcome/pnl.

        Used when Gamma confirms the existing outcome is correct.
        """
        try:
            await self._db.execute(
                """
                UPDATE live_trades
                SET gamma_resolution = ?, gamma_winner_matches = ?
                WHERE id = ?
                """,
                (gamma_resolution, gamma_winner_matches, trade_id),
            )
            await self._db.commit()
        except Exception:
            logger.exception("Failed to stamp gamma on trade %d", trade_id)
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile storage/db.py`
Expected: No output (clean compile)

- [ ] **Step 4: Commit**

```bash
git add storage/db.py
git commit -m "db: add correct_trade_outcome() and stamp_gamma_verification()"
```

### Task 4: Add `get_recent_outcomes()` for streak rebuild

**Files:**
- Modify: `storage/db.py` (add method after `stamp_gamma_verification`)

- [ ] **Step 1: Add the query method**

```python
    async def get_recent_outcomes(self, side: str, limit: int = 10) -> list[dict]:
        """Return the last N settled live trades for a given side.

        Used to rebuild streak guard state after a correction.
        Excludes maker_fill trades (same as streak guard logic in main.py).
        Orders by settled_at DESC so most recent is first.
        """
        try:
            cursor = await self._db.execute(
                """
                SELECT outcome FROM live_trades
                WHERE success = 1
                  AND side = ?
                  AND outcome IN ('WIN', 'LOSS', 'EARLY_EXIT')
                  AND (trade_tag IS NULL OR trade_tag != 'maker_fill')
                ORDER BY settled_at DESC
                LIMIT ?
                """,
                (side, limit),
            )
            rows = await cursor.fetchall()
            # Reverse so index 0 = oldest, matching _side_outcomes order
            return [dict(row) for row in reversed(rows)]
        except Exception:
            logger.exception("Failed to fetch recent outcomes for %s", side)
            return []
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile storage/db.py`
Expected: No output (clean compile)

- [ ] **Step 3: Commit**

```bash
git add storage/db.py
git commit -m "db: add get_recent_outcomes() for streak state rebuild"
```

---

## Chunk 2: Config + Core Verification Logic in main.py

### Task 5: Add config setting

**Files:**
- Modify: `config.py:88` (after early_exit_threshold_high)

**Note:** `early_exit_threshold_mid` was already reverted from 0.70 to 0.90 before this plan was written (in config.py). That change will be committed together with the final commit.

- [ ] **Step 1: Add the setting**

Add after the `early_exit_threshold_high` line in `config.py`:

```python
    # Gamma verification — delay before re-querying Gamma after Chainlink settlement
    gamma_verify_delay_seconds: int = 300  # 5 minutes — markets resolve in 2-6 min
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile config.py`
Expected: No output (clean compile)

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add GAMMA_VERIFY_DELAY_SECONDS setting (default 300)"
```

### Task 6: Add helper methods `_db_fetch_trade`, `_compute_pnl`, `_rebuild_streak_state`

**Note:** These are added BEFORE the verification method that uses them, to keep the repo in a working state between commits.

**Files:**
- Modify: `main.py` (add methods near the settlement code)
- Modify: `storage/db.py` (add `get_live_trade_by_id`)

- [ ] **Step 1: Add `get_live_trade_by_id()` to db.py**

Add to `Database` class after `get_unsettled_live_trades()`:

```python
    async def get_live_trade_by_id(self, trade_id: int) -> dict | None:
        """Fetch a single live trade by ID."""
        try:
            cursor = await self._db.execute(
                "SELECT * FROM live_trades WHERE id = ?", (trade_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        except Exception:
            logger.exception("Failed to fetch live trade %d", trade_id)
            return None
```

- [ ] **Step 2: Add `_db_fetch_trade()` wrapper in main.py**

Add to `BTCEdgeFinder` class near the other settlement methods:

```python
    async def _db_fetch_trade(self, trade_id: int) -> dict | None:
        """Fetch a live trade by ID (thin wrapper for verification code)."""
        return await self.db.get_live_trade_by_id(trade_id)
```

- [ ] **Step 3: Add `_compute_pnl()` in main.py**

This extracts the PnL calculation already duplicated in `_settle_live_trades_for_window` and `_settle_stale_live_trades` into a reusable method:

```python
    def _compute_pnl(
        self, won: bool, amount: float, entry_price: float,
        response_json: str | None = None,
    ) -> float:
        """Compute PnL for a trade given outcome, amount, and entry price.

        Tries actual token count from response_json first, then falls back
        to fee-adjusted formula.
        """
        # Try actual tokens from FOK response (matches _settle_stale_live_trades pattern)
        actual_tokens = None
        if response_json:
            try:
                resp_d = json.loads(response_json)
                ts = resp_d.get("takingAmount", "")
                if ts and str(ts).strip():
                    actual_tokens = float(ts)
            except Exception:
                pass

        if actual_tokens is not None:
            return (actual_tokens - amount) if won else -amount

        if entry_price > 0 and self.live_trader:
            fee_factor = 0.0
            if self.live_trader._fee_rate > 0:
                from data.polymarket import compute_fee_factor
                fee_factor = compute_fee_factor(
                    entry_price,
                    self.live_trader._fee_rate,
                    self.live_trader._fee_exponent,
                )
            shares = (amount / entry_price) * (1.0 - fee_factor)
            return (shares - amount) if won else -amount

        return -amount if not won else 0.0
```

- [ ] **Step 4: Add `_rebuild_streak_state()` in main.py**

```python
    async def _rebuild_streak_state(self, side: str) -> None:
        """Rebuild _side_outcomes from last 10 DB trades for a given side.

        Called after Gamma corrections to ensure streak guard reflects
        corrected outcomes rather than stale in-memory state.
        """
        recent = await self.db.get_recent_outcomes(side, limit=10)
        self._side_outcomes[side] = [r["outcome"] for r in recent]
        logger.info(
            "Rebuilt streak state for %s: %s",
            side, self._side_outcomes[side][-3:] if self._side_outcomes[side] else "[]",
        )
```

- [ ] **Step 5: Verify syntax**

Run: `python -m py_compile storage/db.py && python -m py_compile main.py`
Expected: No output (clean compile)

- [ ] **Step 6: Commit**

```bash
git add storage/db.py main.py
git commit -m "feat: add helper methods for Gamma verification (PnL calc, streak rebuild, trade fetch)"
```

### Task 7: Add `_verify_settlement_gamma()` — Layer 1

**Files:**
- Modify: `main.py` (add method after `_settle_live_trades_for_window`, around line 2269)

This is the core verification function. It waits 5 minutes, queries Gamma, and corrects any wrong outcomes.

- [ ] **Step 1: Add the verification method**

Add this method to the `BTCEdgeFinder` class after `_settle_live_trades_for_window()`:

```python
    async def _verify_settlement_gamma(
        self, slug: str, settled_trade_ids: list[int]
    ) -> None:
        """Layer 1: Delayed Gamma verification of Chainlink-settled trades.

        Waits gamma_verify_delay_seconds (default 5 min) for Gamma resolution
        to become available, then checks if our Chainlink-based settlement was
        correct. Corrects phantom WINs/missed WINs in DB.
        """
        delay = settings.gamma_verify_delay_seconds
        logger.info(
            "Gamma verify scheduled for %s (%d trades) in %ds",
            slug, len(settled_trade_ids), delay,
        )
        await asyncio.sleep(delay)

        # Query Gamma
        gamma_resolution = await self._query_gamma_resolution(slug, max_retries=3)
        if not gamma_resolution:
            logger.warning(
                "Gamma verify: no resolution for %s after %ds — Layer 2 will catch it",
                slug, delay,
            )
            return

        # Check each settled trade against Gamma truth
        corrections = 0
        for trade_id in settled_trade_ids:
            try:
                row = await self._db_fetch_trade(trade_id)
                if not row:
                    continue
                # Skip trades that were already corrected or are EE/VOID
                if row.get("gamma_resolution") is not None:
                    continue
                if row["outcome"] not in ("WIN", "LOSS"):
                    continue

                side = row["side"]
                amount = row["amount_usdc"]
                entry_price = row.get("entry_price") or 0.0

                # What Gamma says should have happened
                side_would_win = (side == gamma_resolution)
                expected_outcome = "WIN" if side_would_win else "LOSS"
                matches = 1 if row["outcome"] == expected_outcome else 0

                if matches:
                    # Outcome is correct — just stamp verification
                    await self.db.stamp_gamma_verification(
                        trade_id, gamma_resolution, 1
                    )
                else:
                    # WRONG — correct it
                    corrections += 1
                    old_outcome = row["outcome"]

                    # Recalculate PnL from scratch
                    new_pnl = self._compute_pnl(
                        won=side_would_win,
                        amount=amount,
                        entry_price=entry_price,
                        response_json=row.get("response_json"),
                    )

                    await self.db.correct_trade_outcome(
                        trade_id, expected_outcome, new_pnl,
                        gamma_resolution, 0,
                    )

                    # Update live trader bankroll (reverse old PnL, apply new)
                    if self.live_trader:
                        old_pnl = row.get("pnl", 0.0)
                        pnl_delta = new_pnl - old_pnl
                        self.live_trader.bankroll += pnl_delta

                    logger.warning(
                        "GAMMA CORRECTION: trade %d %s %s: %s -> %s (pnl $%+.2f -> $%+.2f)",
                        trade_id, side, slug, old_outcome, expected_outcome,
                        row.get("pnl", 0), new_pnl,
                    )

                    # Telegram alert
                    if self.alerter:
                        try:
                            await self.alerter._send(
                                f"CORRECTION: {slug}\n"
                                f"{side} {old_outcome} -> {expected_outcome}\n"
                                f"PnL: ${row.get('pnl', 0):+.2f} -> ${new_pnl:+.2f}\n"
                                f"(Gamma verified)",
                                parse_mode=None,
                            )
                        except Exception:
                            pass

            except Exception:
                logger.exception("Gamma verify failed for trade %d", trade_id)

        if corrections:
            # Rebuild streak state from corrected DB data
            for side in ("UP", "DOWN"):
                await self._rebuild_streak_state(side)
            logger.warning(
                "Gamma verify %s: %d correction(s) applied, streak state rebuilt",
                slug, corrections,
            )
        else:
            logger.info("Gamma verify %s: all %d trades confirmed correct", slug, len(settled_trade_ids))
```

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (clean compile)

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add _verify_settlement_gamma() — Layer 1 delayed Gamma verification"
```

### Task 8: Wire Layer 1 into `_settle_previous_window()`

**Files:**
- Modify: `main.py:2148-2154` (the live trade settlement section in `_settle_previous_window`)

- [ ] **Step 1: Collect settled trade IDs and schedule verification**

Find the section in `_settle_previous_window()` around line 2148-2154:

```python
        # Settle live trades using Gamma resolution (or price fallback)
        if self.live_trader and self.live_trader.is_active:
            await self._settle_live_trades_for_window(slug, btc_went_up, btc_end)

        # Backfill settlement data on any EARLY_EXIT trades for this window
        if slug:
            await self.db.backfill_early_exit_settlement(slug, btc_end, btc_went_up)
```

Replace with:

```python
        # Settle live trades using Gamma resolution (or price fallback)
        settled_ids: list[int] = []
        if self.live_trader and self.live_trader.is_active:
            settled_ids = await self._settle_live_trades_for_window(slug, btc_went_up, btc_end)

        # Backfill settlement data on any EARLY_EXIT trades for this window
        if slug:
            await self.db.backfill_early_exit_settlement(slug, btc_end, btc_went_up)

        # Layer 1: Schedule delayed Gamma verification if we used Chainlink fallback
        if settled_ids and settlement_source != "Gamma API":
            asyncio.create_task(
                self._verify_settlement_gamma(slug, settled_ids),
                name=f"gamma_verify_{slug}",
            )
```

- [ ] **Step 2: Make `_settle_live_trades_for_window` return trade IDs**

Modify the method signature at line 2180 from:

```python
    async def _settle_live_trades_for_window(
        self, slug: str, btc_went_up: bool, settlement_price: float | None = None
    ) -> None:
```

To:

```python
    async def _settle_live_trades_for_window(
        self, slug: str, btc_went_up: bool, settlement_price: float | None = None
    ) -> list[int]:
```

Add `settled_ids: list[int] = []` at the start of the method body (after the `if not self.live_trader: return` guard — change `return` to `return []`).

After the `await self.db.update_live_trade(...)` call, add:
```python
            settled_ids.append(row["id"])
```

At the end of the method, add:
```python
        return settled_ids
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (clean compile)

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: wire Layer 1 — schedule Gamma verification after Chainlink settlement"
```

---

## Chunk 3: Layer 2 (Hourly Reconciliation) + Layer 3 (Balance Check)

### Task 9: Add Layer 2 — Gamma reconciliation in `_stats_loop`

**Files:**
- Modify: `main.py:2709-2711` (in `_stats_loop`, after stale trade settlement)

- [ ] **Step 1: Add `_gamma_reconciliation()` method**

Add this method to `BTCEdgeFinder` class:

```python
    async def _gamma_reconciliation(self) -> None:
        """Layer 2: Hourly reconciliation of trades missing Gamma verification.

        Catches anything Layer 1 missed (e.g., task cancelled on restart,
        Gamma was slow). Re-checks all settled trades without gamma_resolution.
        """
        unverified = await self.db.get_unverified_trades(min_age_seconds=600)
        if not unverified:
            return

        logger.info("Gamma reconciliation: %d unverified trades", len(unverified))

        # Group by slug for efficient API calls
        by_slug: dict[str, list[dict]] = {}
        for row in unverified:
            by_slug.setdefault(row["market_slug"], []).append(row)

        corrections = 0
        verified = 0
        for slug, trades in by_slug.items():
            gamma_resolution = await self._query_gamma_resolution(slug, max_retries=2)
            if not gamma_resolution:
                continue  # Still not resolved — try next hour

            for row in trades:
                side = row["side"]
                side_would_win = (side == gamma_resolution)
                expected_outcome = "WIN" if side_would_win else "LOSS"
                matches = 1 if row["outcome"] == expected_outcome else 0

                if matches:
                    await self.db.stamp_gamma_verification(
                        row["id"], gamma_resolution, 1
                    )
                    verified += 1
                else:
                    # Correction needed
                    amount = row["amount_usdc"]
                    entry_price = row.get("entry_price") or 0.0
                    new_pnl = self._compute_pnl(
                        won=side_would_win,
                        amount=amount,
                        entry_price=entry_price,
                        response_json=row.get("response_json"),
                    )
                    await self.db.correct_trade_outcome(
                        row["id"], expected_outcome, new_pnl,
                        gamma_resolution, 0,
                    )

                    # Adjust bankroll
                    if self.live_trader:
                        old_pnl = row.get("pnl", 0.0)
                        self.live_trader.bankroll += (new_pnl - old_pnl)

                    corrections += 1
                    logger.warning(
                        "RECONCILIATION: trade %d %s %s: %s -> %s",
                        row["id"], side, slug, row["outcome"], expected_outcome,
                    )

            await asyncio.sleep(0.3)  # Rate limit Gamma API

        if corrections:
            for side in ("UP", "DOWN"):
                await self._rebuild_streak_state(side)

            if self.alerter:
                try:
                    await self.alerter._send(
                        f"RECONCILIATION: {corrections} correction(s), {verified} confirmed\n"
                        f"Streak state rebuilt",
                        parse_mode=None,
                    )
                except Exception:
                    pass

        if verified or corrections:
            logger.info(
                "Gamma reconciliation done: %d verified, %d corrected",
                verified, corrections,
            )
```

- [ ] **Step 2: Wire into `_stats_loop`**

In `_stats_loop()`, after the `await self._settle_stale_live_trades()` call (around line 2711), add:

```python
                        # Layer 2: Gamma reconciliation for already-settled trades
                        await self._gamma_reconciliation()
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (clean compile)

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: add Layer 2 — hourly Gamma reconciliation for unverified trades"
```

### Task 10: Add Layer 3 — Balance sanity check

**Files:**
- Modify: `main.py` (add method + wire into `_stats_loop`)

- [ ] **Step 1: Add `_balance_sanity_check()` method**

```python
    async def _balance_sanity_check(self) -> None:
        """Layer 3: Compare DB-calculated bankroll to actual Polymarket balance.

        Alerts if discrepancy exceeds $5. This catches systematic errors
        (phantom WINs, missed settlements, fee miscalculations) that
        individual trade verification might miss.
        """
        if not self.live_trader or not self.live_trader.is_active:
            return

        actual = await self.live_trader.get_balance()
        if actual is None:
            return

        # Count tokens in open positions (pending trades have value)
        unsettled = await self.db.get_unsettled_live_trades()
        open_value = sum(r.get("amount_usdc", 0) for r in unsettled)

        db_bankroll = self.live_trader.bankroll
        # Actual portfolio = USDC cash + value of open positions
        # DB bankroll tracks cash only (positions are subtracted at entry, added at settlement)
        expected_cash = db_bankroll
        discrepancy = actual - expected_cash

        logger.info(
            "Balance check: actual=$%.2f, DB bankroll=$%.2f, "
            "open positions=%d ($%.2f), discrepancy=$%+.2f",
            actual, db_bankroll, len(unsettled), open_value, discrepancy,
        )

        # Alert if discrepancy exceeds threshold (accounting for open positions)
        # Open positions are expected to cause ~$5-10 gap during active trading
        adjusted_discrepancy = abs(discrepancy) - open_value
        if adjusted_discrepancy > 5.0:
            logger.warning(
                "BALANCE DISCREPANCY: $%.2f (adjusted for %d open positions: $%.2f)",
                discrepancy, len(unsettled), adjusted_discrepancy,
            )
            if self.alerter:
                try:
                    await self.alerter._send(
                        f"BALANCE WARNING\n"
                        f"Actual: ${actual:.2f}\n"
                        f"DB bankroll: ${db_bankroll:.2f}\n"
                        f"Open positions: {len(unsettled)} (${open_value:.2f})\n"
                        f"Gap: ${discrepancy:+.2f}",
                        parse_mode=None,
                    )
                except Exception:
                    pass
```

- [ ] **Step 2: Wire into `_stats_loop` — BEFORE bankroll sync**

In `_stats_loop()`, the balance sanity check MUST run BEFORE the bankroll sync at line 2697-2707 (which sets `self.live_trader.bankroll = real_bal`). If it runs after, the discrepancy will always be ~$0.

Find the section (around line 2696):
```python
                    # Sync bankroll from real CLOB balance
                    if self.live_trader and self.live_trader.is_active:
                        real_bal = await self.live_trader.get_balance()
```

Add BEFORE that block:
```python
                    # Layer 3: Balance sanity check (must run BEFORE bankroll sync)
                    if self.live_trader and self.live_trader.is_active:
                        await self._balance_sanity_check()
```

Then, after `await self._settle_stale_live_trades()`, add:
```python
                        # Layer 2: Gamma reconciliation for already-settled trades
                        await self._gamma_reconciliation()
```

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (clean compile)

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: add Layer 3 — hourly balance sanity check with Telegram alert"
```

---

## Chunk 4: Config Revert + CLAUDE.md + Full Verification

### Task 11: Verify full syntax + update CLAUDE.md changelog

**Files:**
- Modify: `CLAUDE.md` (add changelog entry)

- [ ] **Step 1: Full syntax check of all modified files**

Run:
```bash
python -m py_compile config.py && python -m py_compile storage/db.py && python -m py_compile main.py
```
Expected: No output (all clean)

- [ ] **Step 2: Add CLAUDE.md changelog entry**

Add to top of changelog table:

```markdown
| 2026-03-10 ~XX:XX | **Gamma verification system**: 3-layer fix for phantom WINs (12.7% error rate, $234 discrepancy). Layer 1: delayed 5-min Gamma verification after every Chainlink settlement. Layer 2: hourly reconciliation catches anything Layer 1 missed. Layer 3: balance sanity check alerts on $5+ DB/actual divergence. Corrections update outcome, PnL, bankroll, and streak guard state. Config: reverted `early_exit_threshold_mid` 0.70 -> 0.90 (VPS had 0.90 hardcoded anyway, data confirms 0.90 earns more). | `docs/plans/2026-03-10-gamma-verification-design.md` |
```

- [ ] **Step 3: Commit all together**

```bash
git add config.py storage/db.py main.py CLAUDE.md docs/plans/2026-03-10-gamma-verification-design.md docs/plans/2026-03-10-gamma-verification-plan.md
git commit -m "feat: 3-layer Gamma verification system — fix phantom WINs

Layer 1: delayed Gamma verification 5 min after Chainlink settlement
Layer 2: hourly reconciliation for unverified trades
Layer 3: balance sanity check ($5+ discrepancy alert)
Also reverts early_exit_threshold_mid 0.70 to 0.90"
```

### Task 12: Deploy to VPS

- [ ] **Step 1: Push to GitHub**

```bash
git push origin
```

- [ ] **Step 2: Deploy changed files to VPS**

```bash
scp config.py main.py root@65.21.178.90:/home/btcedge/BTC-tool/
scp storage/db.py root@65.21.178.90:/home/btcedge/BTC-tool/storage/
```

- [ ] **Step 3: Restart service**

```bash
ssh root@65.21.178.90 "systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge"
```

- [ ] **Step 4: Verify running**

```bash
ssh root@65.21.178.90 "sleep 3 && systemctl is-active btc-edge && tail -20 /home/btcedge/BTC-tool/btc_edge.log"
```

Check for: "Connected to SQLite database", no import errors, service active.

- [ ] **Step 5: Tag deploy**

```bash
git tag -a deploy-$(date -u +%Y-%m-%d-%H%M) -m "deployed: 3-layer Gamma verification system"
```

### Task 13: Run backfill on VPS DB to fix historical phantom WINs

- [ ] **Step 1: Download current DB**

```bash
scp root@65.21.178.90:/home/btcedge/BTC-tool/btc_edge.db .
```

- [ ] **Step 2: Run backfill for any trades missing gamma data**

```bash
python backfill_gamma_resolution.py --apply --db btc_edge.db
```

Review output — expect to see corrections for some already-settled trades.

- [ ] **Step 3: Upload corrected DB back to VPS**

```bash
scp btc_edge.db root@65.21.178.90:/home/btcedge/BTC-tool/
```

**Note:** Only do this when the service is stopped. Otherwise the DB will be overwritten while the service is writing to it. Alternative: run the backfill directly on the VPS.

Better approach — run directly on VPS:

```bash
scp backfill_gamma_resolution.py root@65.21.178.90:/home/btcedge/BTC-tool/
ssh root@65.21.178.90 "cd /home/btcedge/BTC-tool && source venv/bin/activate && python backfill_gamma_resolution.py --apply"
```

### Task 14: Monitor for 24 hours

- [ ] **Step 1: After ~1 hour, check logs for Layer 1 activity**

```bash
ssh root@65.21.178.90 "grep -i 'gamma verify\|CORRECTION\|RECONCILIATION' /home/btcedge/BTC-tool/btc_edge.log | tail -30"
```

Expected: ~12 "Gamma verify scheduled" per hour (one per window), most "confirmed correct", occasional corrections.

- [ ] **Step 2: After ~1 hour, check for Layer 2 activity**

```bash
ssh root@65.21.178.90 "grep -i 'reconciliation' /home/btcedge/BTC-tool/btc_edge.log | tail -10"
```

Expected: "Gamma reconciliation: 0 unverified trades" (Layer 1 should catch them first).

- [ ] **Step 3: Check balance sanity**

```bash
ssh root@65.21.178.90 "grep -i 'balance check' /home/btcedge/BTC-tool/btc_edge.log | tail -5"
```

Expected: Small discrepancy ($0-5) from normal operations.
