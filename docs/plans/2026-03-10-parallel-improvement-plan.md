# Parallel Improvement Plan: Backtest + Quick Wins + Dual-Position Paper

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate dual-position strategy via historical backtest, deploy immediate quick-win filters to the live system, and prepare dual-position paper trading infrastructure for forward validation.

**Architecture:** Three independent tracks executed in parallel. Track A is a standalone backtest script that replays 285K market snapshots. Track B modifies the live system's entry filtering and config. Track C extends shadow tracking to simulate dual-position paper trades. Each track produces independently testable, deployable results.

**Tech Stack:** Python 3.11, sqlite3, asyncio, existing Polymarket fee utilities

**Spec Reference:** `docs/plans/2026-03-10-deep-analysis-report.md`

---

## File Structure

### Track A: Dual-Position Backtest
- **Create:** `scripts/simulation/simulate_dual_position.py` — standalone backtest script, no imports from live system except `data.polymarket.compute_fee_factor`. Reads `btc_edge.db` directly. ~250 lines.

### Track B: Quick Wins (Live System)
- **Modify:** `config.py` — revert `early_exit_threshold_mid` to 0.90, raise `min_confidence` to 0.020
- **Modify:** `main.py:~1580-1595` — add entry price filter for normal trades (>=0.50 for live), add spread filter
- **Modify:** `strategy/edge.py:~186-196` — widen exploration range to <0.50 (currently <0.40) so normal trades below 0.50 go to paper

### Track C: Dual-Position Paper Tracking
- **Modify:** `main.py:~1095-1140` — enhance shadow window tracking to simulate dual-position EE outcomes and record per-window P&L

---

## Chunk 1: Track A — Dual-Position Backtest

### Task 1: Create the backtest script skeleton

**Files:**
- Create: `scripts/simulation/simulate_dual_position.py`

- [ ] **Step 1: Write the backtest script with fee calculations and data loading**

```python
"""
Dual-Position Backtest: Simulate buying BOTH UP and DOWN tokens every window.

Uses market_snapshots (285K rows, ~3s intervals) to replay bid/ask trajectories.
For each 5-minute window:
  1. Buy UP at first available ask, buy DOWN at first available ask
  2. Monitor both bids independently at ~3s polling (snapshot frequency)
  3. EE each side when bid >= threshold (independent triggers)
  4. If no EE by window end, settle based on actual BTC direction

Accounts for:
  - Full Polymarket fees on every buy AND sell (4 fee events max per window)
  - Configurable slippage per fill
  - FOK rejection modeling (skip window if book depth < min_depth)
  - Separate thresholds per side based on entry price tier

CAVEAT: Settlement direction uses Binance BTC price from market_snapshots, not
Chainlink. There is a ~7.8% disagreement rate between Binance and Chainlink.
This means ~1 in 13 "no_ee" windows may have incorrect settlement direction.
The EE-triggered windows (which dominate the strategy) are unaffected.

Usage:
    python scripts/simulation/simulate_dual_position.py
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Add project root to path for fee import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from data.polymarket import compute_fee_factor


# --- Configuration -----------------------------------------------------------

DB_PATH = "btc_edge.db"
BET_PER_SIDE = 5.0          # USDC per side per window
SLIPPAGE = 0.01             # cents worse per fill (buy higher, sell lower)
MIN_DEPTH_TOKENS = 20       # minimum bid depth to execute EE sell
FEE_RATE = 0.25
FEE_EXPONENT = 2
GRACE_PERIOD_S = 10         # seconds after entry before monitoring EE

# EE thresholds to sweep
THRESHOLDS = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


# --- Fee & PnL helpers -------------------------------------------------------

def shares_purchased(amount_usdc: float, ask_price: float) -> float:
    """Shares received after buying at ask_price with fees."""
    ff = compute_fee_factor(ask_price, FEE_RATE, FEE_EXPONENT)
    return (amount_usdc / ask_price) * (1.0 - ff)


def sell_proceeds(shares: float, bid_price: float) -> float:
    """USDC received after selling shares at bid_price with fees."""
    gross = shares * bid_price
    ff = compute_fee_factor(bid_price, FEE_RATE, FEE_EXPONENT)
    return gross * (1.0 - ff)


def settlement_value(shares: float, won: bool) -> float:
    """USDC received at settlement (shares * $1 if won, $0 if lost)."""
    return shares if won else 0.0


# --- Data loading ------------------------------------------------------------

def load_windows(db_path: str) -> dict:
    """Load market_snapshots grouped by slug, sorted by timestamp.

    Returns dict: slug -> list of snapshot dicts, ordered by timestamp.
    Only includes windows with both UP and DOWN bid/ask data.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT slug, timestamp, up_best_bid, up_best_ask, down_best_bid, down_best_ask,
               up_bid_size, down_bid_size, btc_price
        FROM market_snapshots
        WHERE up_best_bid IS NOT NULL AND down_best_bid IS NOT NULL
          AND up_best_ask IS NOT NULL AND down_best_ask IS NOT NULL
        ORDER BY slug, timestamp
    """)
    windows = defaultdict(list)
    for row in cur.fetchall():
        windows[dict(row)["slug"]].append(dict(row))
    conn.close()
    return dict(windows)


def determine_winner(snapshots: list) -> str | None:
    """Determine which side won based on BTC price movement.

    Uses first and last BTC price in the window. Flat/down = DOWN wins.
    Returns 'UP' or 'DOWN', or None if no BTC price data.
    """
    first_btc = None
    last_btc = None
    for s in snapshots:
        if s["btc_price"]:
            if first_btc is None:
                first_btc = s["btc_price"]
            last_btc = s["btc_price"]
    if first_btc is None or last_btc is None:
        return None
    return "UP" if last_btc > first_btc else "DOWN"


# --- Simulation engine -------------------------------------------------------

def simulate_window(snapshots: list, threshold: float) -> dict:
    """Simulate dual-position trading for one 5-minute window.

    Returns dict with:
      - up_entry, down_entry: ask prices at entry
      - up_exit, down_exit: 'EE' | 'WIN' | 'LOSS'
      - up_pnl, down_pnl: USDC profit/loss per side
      - total_pnl: combined
      - fees: total fees paid
      - scenario: 'both_ee' | 'one_ee' | 'no_ee'
    """
    if len(snapshots) < 5:
        return None

    # Entry: use first snapshot's ask prices + slippage
    entry = snapshots[0]
    up_ask = entry["up_best_ask"] + SLIPPAGE
    down_ask = entry["down_best_ask"] + SLIPPAGE

    # Skip if combined entry cost is unreasonable (> $1.10 for $1 of exposure)
    if up_ask + down_ask > 1.10:
        return None

    # Skip if either side has no reasonable ask
    if up_ask <= 0.05 or down_ask <= 0.05 or up_ask >= 0.95 or down_ask >= 0.95:
        return None

    # Calculate shares purchased
    up_shares = shares_purchased(BET_PER_SIDE, up_ask)
    down_shares = shares_purchased(BET_PER_SIDE, down_ask)

    # Track fees
    up_entry_fee = BET_PER_SIDE * compute_fee_factor(up_ask, FEE_RATE, FEE_EXPONENT)
    down_entry_fee = BET_PER_SIDE * compute_fee_factor(down_ask, FEE_RATE, FEE_EXPONENT)
    total_fees = up_entry_fee + down_entry_fee

    # Entry timestamp for grace period
    entry_ts = entry["timestamp"]

    # Monitor for EE
    up_exited = False
    down_exited = False
    up_exit_pnl = 0.0
    down_exit_pnl = 0.0

    for snap in snapshots[1:]:
        elapsed = snap["timestamp"] - entry_ts
        if elapsed < GRACE_PERIOD_S:
            continue

        # Check UP side EE
        if not up_exited:
            up_bid = snap["up_best_bid"] - SLIPPAGE  # slippage on sell
            up_depth = snap.get("up_bid_size") or 999  # assume depth OK if missing
            if up_bid >= threshold and up_depth >= MIN_DEPTH_TOKENS:
                proceeds = sell_proceeds(up_shares, up_bid)
                up_exit_pnl = proceeds - BET_PER_SIDE
                # Fee on gross sale (shares * bid), not on net proceeds
                exit_fee = (up_shares * up_bid) * compute_fee_factor(up_bid, FEE_RATE, FEE_EXPONENT)
                total_fees += exit_fee
                up_exited = True

        # Check DOWN side EE
        if not down_exited:
            down_bid = snap["down_best_bid"] - SLIPPAGE
            down_depth = snap.get("down_bid_size") or 999
            if down_bid >= threshold and down_depth >= MIN_DEPTH_TOKENS:
                proceeds = sell_proceeds(down_shares, down_bid)
                down_exit_pnl = proceeds - BET_PER_SIDE
                exit_fee = (down_shares * down_bid) * compute_fee_factor(down_bid, FEE_RATE, FEE_EXPONENT)
                total_fees += exit_fee
                down_exited = True

        if up_exited and down_exited:
            break

    # Settlement for sides that didn't EE
    winner = determine_winner(snapshots)
    if winner is None:
        return None  # no BTC data, can't settle

    if not up_exited:
        up_won = (winner == "UP")
        up_exit_pnl = settlement_value(up_shares, up_won) - BET_PER_SIDE

    if not down_exited:
        down_won = (winner == "DOWN")
        down_exit_pnl = settlement_value(down_shares, down_won) - BET_PER_SIDE

    total_pnl = up_exit_pnl + down_exit_pnl

    # Classify scenario
    if up_exited and down_exited:
        scenario = "both_ee"
    elif up_exited or down_exited:
        scenario = "one_ee"
    else:
        scenario = "no_ee"

    return {
        "up_entry": up_ask,
        "down_entry": down_ask,
        "combined_entry": up_ask + down_ask,
        "up_exit": "EE" if up_exited else ("WIN" if winner == "UP" else "LOSS"),
        "down_exit": "EE" if down_exited else ("WIN" if winner == "DOWN" else "LOSS"),
        "up_pnl": up_exit_pnl,
        "down_pnl": down_exit_pnl,
        "total_pnl": total_pnl,
        "fees": total_fees,
        "scenario": scenario,
        "winner": winner,
    }


# --- Main --------------------------------------------------------------------

def main():
    print("Loading market snapshots...")
    windows = load_windows(DB_PATH)
    print(f"  {len(windows)} windows with valid bid/ask data\n")

    for threshold in THRESHOLDS:
        results = []
        for slug, snapshots in windows.items():
            result = simulate_window(snapshots, threshold)
            if result:
                results.append(result)

        if not results:
            print(f"Threshold {threshold:.2f}: No valid windows")
            continue

        # Aggregate
        n = len(results)
        total_pnl = sum(r["total_pnl"] for r in results)
        total_fees = sum(r["fees"] for r in results)
        avg_pnl = total_pnl / n
        both_ee = sum(1 for r in results if r["scenario"] == "both_ee")
        one_ee = sum(1 for r in results if r["scenario"] == "one_ee")
        no_ee = sum(1 for r in results if r["scenario"] == "no_ee")

        # Per-scenario breakdown
        both_ee_pnl = [r["total_pnl"] for r in results if r["scenario"] == "both_ee"]
        one_ee_pnl = [r["total_pnl"] for r in results if r["scenario"] == "one_ee"]
        no_ee_pnl = [r["total_pnl"] for r in results if r["scenario"] == "no_ee"]

        avg_both = sum(both_ee_pnl) / len(both_ee_pnl) if both_ee_pnl else 0
        avg_one = sum(one_ee_pnl) / len(one_ee_pnl) if one_ee_pnl else 0
        avg_no = sum(no_ee_pnl) / len(no_ee_pnl) if no_ee_pnl else 0

        avg_entry = sum(r["combined_entry"] for r in results) / n
        daily_est = avg_pnl * 288  # 288 windows/day

        # Win rate (positive PnL windows)
        win_rate = sum(1 for r in results if r["total_pnl"] > 0) / n

        # Drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for r in results:
            cumulative += r["total_pnl"]
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        print(f"{'='*70}")
        print(f"  THRESHOLD: {threshold:.2f}  |  {n} windows  |  ${BET_PER_SIDE:.0f}/side")
        print(f"{'='*70}")
        print(f"  Total PnL:     ${total_pnl:>8.2f}  ({total_pnl/n:.3f}/window)")
        print(f"  Total fees:    ${total_fees:>8.2f}  ({total_fees/n:.3f}/window)")
        print(f"  Avg entry sum:  {avg_entry:.3f}")
        print(f"  Win rate:       {win_rate:.1%} of windows profitable")
        print(f"  Max drawdown:  ${max_dd:>8.2f}")
        print(f"  Daily estimate: ${daily_est:>8.2f}/day (288 windows)")
        print(f"")
        print(f"  Scenarios:")
        print(f"    Both EE:  {both_ee:>4d} ({both_ee/n:.1%})  avg ${avg_both:>+.2f}/window")
        print(f"    One EE:   {one_ee:>4d} ({one_ee/n:.1%})  avg ${avg_one:>+.2f}/window")
        print(f"    No EE:    {no_ee:>4d} ({no_ee/n:.1%})  avg ${avg_no:>+.2f}/window")
        print()

    # Also run a detailed daily breakdown for the best threshold
    best_threshold = 0.70  # from prior analysis
    print(f"\n{'='*70}")
    print(f"  DAILY BREAKDOWN @ threshold={best_threshold}")
    print(f"{'='*70}")

    daily = defaultdict(lambda: {"windows": 0, "pnl": 0.0, "fees": 0.0})
    from datetime import datetime, timezone
    for slug, snapshots in windows.items():
        result = simulate_window(snapshots, best_threshold)
        if result:
            # Extract date from slug timestamp (btc-updown-5m-{ts})
            try:
                ts = int(slug.split("-")[-1])
                day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                continue  # skip malformed slugs
            daily[day]["windows"] += 1
            daily[day]["pnl"] += result["total_pnl"]
            daily[day]["fees"] += result["fees"]

    for day in sorted(daily.keys()):
        d = daily[day]
        avg = d["pnl"] / d["windows"] if d["windows"] else 0
        print(f"  {day}: {d['windows']:>3d} windows  PnL ${d['pnl']:>+8.2f}  "
              f"Fees ${d['fees']:>6.2f}  Avg ${avg:>+.3f}/window")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the backtest**

Run from project root:
```bash
cd /c/Users/Rn5ho/BTC-tool && python scripts/simulation/simulate_dual_position.py
```

Expected: Output showing PnL per threshold (0.60-0.95), scenario breakdown (both_ee / one_ee / no_ee), daily breakdown at 0.70 threshold. Script should complete in <30 seconds.

**What to look for:**
- Is total PnL positive at any threshold?
- What's the average entry cost sum? (Should be ~1.01-1.07)
- What percentage of windows have both sides EE?
- What's the max drawdown?
- Is the daily PnL distribution consistent or lumpy?

- [ ] **Step 3: Commit**

```bash
git add scripts/simulation/simulate_dual_position.py
git commit -m "feat: dual-position backtest script — sweep EE thresholds on 285K market snapshots"
```

### Task 2: Analyze backtest results and add comparison mode

**Files:**
- Modify: `scripts/simulation/simulate_dual_position.py`

- [ ] **Step 1: Add single-side comparison to the same script**

After the dual-position sweep, add a section that runs the same windows through a single-side simulation (current strategy equivalent) for direct comparison. Append this after the `main()` function's dual-position output:

```python
    # --- Comparison: single-side (current strategy equivalent) ----------------
    print(f"\n{'='*70}")
    print(f"  COMPARISON: SINGLE-SIDE (current strategy) @ threshold=0.90")
    print(f"{'='*70}")

    single_results = []
    for slug, snapshots in windows.items():
        if len(snapshots) < 5:
            continue
        entry = snapshots[0]
        if not entry["up_best_ask"] or not entry["down_best_ask"]:
            continue

        winner = determine_winner(snapshots)
        if winner is None:
            continue

        # Pick the winning side (best case for direction model)
        # This gives a ceiling on single-side performance
        side = winner  # oracle picks correct side
        ask_key = f"{side.lower()}_best_ask"
        bid_key = f"{side.lower()}_best_bid"
        depth_key = f"{side.lower()}_bid_size"

        ask_price = entry[ask_key] + SLIPPAGE
        if ask_price <= 0.05 or ask_price >= 0.95:
            continue

        shares = shares_purchased(BET_PER_SIDE, ask_price)
        entry_fee = BET_PER_SIDE * compute_fee_factor(ask_price, FEE_RATE, FEE_EXPONENT)
        fees = entry_fee

        # Monitor for EE at 0.90 threshold
        exited = False
        pnl = 0.0
        entry_ts = entry["timestamp"]
        for snap in snapshots[1:]:
            if snap["timestamp"] - entry_ts < GRACE_PERIOD_S:
                continue
            bid = snap[bid_key] - SLIPPAGE
            depth = snap.get(depth_key) or 999
            if bid >= 0.90 and depth >= MIN_DEPTH_TOKENS:
                proceeds = sell_proceeds(shares, bid)
                pnl = proceeds - BET_PER_SIDE
                fees += proceeds * compute_fee_factor(bid, FEE_RATE, FEE_EXPONENT)
                exited = True
                break

        if not exited:
            pnl = settlement_value(shares, True) - BET_PER_SIDE  # oracle always wins

        single_results.append({"pnl": pnl, "ee": exited, "fees": fees})

    n_s = len(single_results)
    total_s = sum(r["pnl"] for r in single_results)
    ee_s = sum(1 for r in single_results if r["ee"])
    print(f"  Oracle single-side (always picks winner):")
    print(f"    Windows: {n_s}  Total PnL: ${total_s:.2f}  Avg: ${total_s/n_s:.3f}/window")
    print(f"    EE rate: {ee_s/n_s:.1%}  Daily est: ${total_s/n_s*288:.2f}/day")
    print()

    # 50% accuracy single-side (realistic model)
    import random
    random.seed(42)
    model_results = []
    for slug, snapshots in windows.items():
        if len(snapshots) < 5:
            continue
        entry = snapshots[0]
        if not entry["up_best_ask"] or not entry["down_best_ask"]:
            continue
        winner = determine_winner(snapshots)
        if winner is None:
            continue

        # Random 50% pick
        side = random.choice(["UP", "DOWN"])
        correct = (side == winner)
        ask_key = f"{side.lower()}_best_ask"
        bid_key = f"{side.lower()}_best_bid"
        depth_key = f"{side.lower()}_bid_size"

        ask_price = entry[ask_key] + SLIPPAGE
        if ask_price <= 0.05 or ask_price >= 0.95:
            continue

        shares = shares_purchased(BET_PER_SIDE, ask_price)
        exited = False
        pnl = 0.0
        entry_ts = entry["timestamp"]
        for snap in snapshots[1:]:
            if snap["timestamp"] - entry_ts < GRACE_PERIOD_S:
                continue
            bid = snap[bid_key] - SLIPPAGE
            depth = snap.get(depth_key) or 999
            if bid >= 0.90 and depth >= MIN_DEPTH_TOKENS:
                proceeds = sell_proceeds(shares, bid)
                pnl = proceeds - BET_PER_SIDE
                exited = True
                break

        if not exited:
            pnl = settlement_value(shares, correct) - BET_PER_SIDE

        model_results.append({"pnl": pnl, "ee": exited, "fees": 0.0})

    n_m = len(model_results)
    total_m = sum(r["pnl"] for r in model_results)
    ee_m = sum(1 for r in model_results if r["ee"])
    print(f"  Random 50% single-side (coin flip model):")
    print(f"    Windows: {n_m}  Total PnL: ${total_m:.2f}  Avg: ${total_m/n_m:.3f}/window")
    print(f"    EE rate: {ee_m/n_m:.1%}  Daily est: ${total_m/n_m*288:.2f}/day")
```

- [ ] **Step 2: Run the updated script**

```bash
cd /c/Users/Rn5ho/BTC-tool && python scripts/simulation/simulate_dual_position.py
```

Expected: All three strategies compared side by side — dual-position at various thresholds, oracle single-side, and random single-side. This gives us the definitive answer on whether dual-position beats single-side.

- [ ] **Step 3: Commit**

```bash
git add scripts/simulation/simulate_dual_position.py
git commit -m "feat: add single-side oracle + random comparison to dual-position backtest"
```

---

## Chunk 2: Track B — Quick Wins

### Task 3: Config changes (zero-risk)

**Files:**
- Modify: `config.py:27` — raise min_confidence from 0.015 to 0.020
- Modify: `config.py:87` — revert early_exit_threshold_mid from 0.70 to 0.90

- [ ] **Step 1: Update config.py**

In `config.py` line 27, change:
```python
    min_confidence: float = 0.015
```
to:
```python
    min_confidence: float = 0.020
```

In `config.py` line 87, change:
```python
    early_exit_threshold_mid: float = 0.90     # entry 0.40-0.50: 0.70 tested but VPS had 0.90 hardcoded; data shows 0.90 earns more
```
to:
```python
    early_exit_threshold_mid: float = 0.90     # entry 0.40-0.50: 0.70 tested 2026-03-09, reverted — flat 0.90 outperforms tiered
```

Note: The value was already 0.90 but the comment was stale from the 0.70 experiment. If the VPS .env has `EARLY_EXIT_THRESHOLD_MID=0.70`, it needs to be removed or set to 0.90. The config.py default is the fallback.

- [ ] **Step 2: Verify syntax**

```bash
python -m py_compile config.py
```

Expected: No output (success).

- [ ] **Step 3: Do NOT commit yet** — all Track B changes go in one commit with CLAUDE.md (Task 6).

### Task 4: Entry price filter by trade type

**Files:**
- Modify: `main.py:~1580-1595` — add normal-trade entry price filter for live trades
- Modify: `strategy/edge.py:~196` — widen exploration range from <0.40 to <0.50

**Context:** The analysis found:
- Normal trades below entry 0.50 lose money (-$66 on 214 trades)
- Regime flip trades above entry 0.55 lose money (-$55 on 64 trades)
- The regime flip cap at <0.50 is already deployed (main.py:1587)
- The exploration range (<0.40 = paper-only) needs to widen to <0.50 for normal trades

The approach requires two changes: (1) widen the exploration boundary to <0.50, and (2) raise the regime flip entry cap to <0.55 with an explicit exploration override so flips at entry 0.40-0.55 still go live.

**CRITICAL INTERACTION:** Widening exploration to <0.50 sets `exploration=True` for all entries below $0.50 in `edge.py`. This would block regime flip trades at entry 0.40-0.50 from going live (main.py line 1595 checks `not live_signal.get("exploration")`). The fix: when assigning a flip signal for live, explicitly clear the exploration flag.

- [ ] **Step 1: Widen exploration range in edge.py**

In `strategy/edge.py` line 196, change:
```python
        exploration = entry_price < 0.40
```
to:
```python
        exploration = entry_price < 0.50
```

Also update the log message at line 198-199:
```python
        if exploration:
            logger.info(
                "Exploration signal — entry price %.3f in 0.25-0.50 range on %s %s",
                entry_price, best_side, market.slug,
            )
```

And update the comment at line 184:
```python
        # Core range 0.50-0.65: live trades with full sizing.
        # Exploration range 0.25-0.50: paper-only (normal trades below 0.50 lose money).
        # Below 0.25: too thin / too contrarian to be useful.
```

- [ ] **Step 2: Raise the regime flip entry cap from 0.50 to 0.55 AND clear exploration flag**

In `main.py`, find the regime flip entry cap section (~line 1584-1593). Change:
```python
            flip_entry = flip_signal.get("entry_price", 1.0)
            if flip_entry < 0.50:
                live_signal = flip_signal
            else:
                logger.info(
                    "FLIP ENTRY CAP: skipping live flip (entry=%.3f >= 0.50)",
                    flip_entry,
                )
```
to:
```python
            flip_entry = flip_signal.get("entry_price", 1.0)
            if flip_entry < 0.55:
                # Clear exploration flag — flips at 0.40-0.55 should go live
                # (exploration=True is set in edge.py for entry <0.50, but flips
                # at cheap entries are profitable and should not be paper-only)
                live_signal = {**flip_signal, "exploration": False}
            else:
                logger.info(
                    "FLIP ENTRY CAP: skipping live flip (entry=%.3f >= 0.55)",
                    flip_entry,
                )
```

**Why 0.55 not 0.50:** The analysis showed regime flips are profitable at entry <0.55, losing at >=0.55. The current cap at <0.50 is too tight — it excludes profitable 0.50-0.55 flip trades. The `exploration: False` override ensures cheap flips still route to live despite the widened exploration boundary.

- [ ] **Step 3: Verify syntax**

```bash
python -m py_compile strategy/edge.py && python -m py_compile main.py
```

Expected: No output (success).

- [ ] **Step 4: Do NOT commit yet** — all Track B changes go in one commit with CLAUDE.md (Task 6).

### Task 5: Spread filter

**Files:**
- Modify: `main.py:~1490-1510` — add spread check before live trade placement

**Context:** Polymarket spread > $0.02 predicts -$1.15/trade (35% EE rate vs 56% at tight spread). We should skip live trades when the spread is wide.

- [ ] **Step 1: Find the spread check location**

Read `main.py` around line 1490-1550 to find where the signal is evaluated and before the live trade block. The spread is available in `signal["spread"]` (set by edge.py line 180).

- [ ] **Step 2: Add spread filter before live trade placement**

In `main.py`, right before the live trade block (before the line `if self.live_trader and self.live_trader.is_active and not self.live_trader.is_paused and not live_signal.get("exploration"):`), add:

```python
        # Spread filter — wide spreads predict poor EE outcomes
        live_spread = live_signal.get("spread") or 0.0
        if live_spread > 0.03:
            logger.info(
                "SPREAD FILTER: skipping live trade (spread=%.3f > 0.03) on %s %s",
                live_spread, live_signal["side"], live_signal["market_slug"],
            )
            live_signal = {**live_signal, "exploration": True}  # route to paper-only
```

**Note:** Using 0.03 not 0.02 as the threshold. The analysis used snapshot spreads (bid-ask); the `signal["spread"]` may be computed slightly differently. 0.03 is more conservative and avoids over-filtering. The signal is tagged as exploration so paper still tracks it for data.

- [ ] **Step 3: Verify syntax**

```bash
python -m py_compile main.py
```

Expected: No output (success).

- [ ] **Step 4: Do NOT commit yet** — all Track B changes go in one commit with CLAUDE.md (Task 6).

### Task 6: Commit all Track B changes, update CLAUDE.md, and deploy

**Files:**
- Modify: `CLAUDE.md` — add changelog entries and update safety filters section

- [ ] **Step 1: Add changelog entries to CLAUDE.md**

Add these rows to the changelog table at the top (newest first):

```markdown
| 2026-03-10 ~XX:XX | **Quick wins from deep analysis**: (1) MIN_CONFIDENCE raised 0.015→0.020 (1.5-2% band averaged -$0.16/trade). (2) Normal trades below entry $0.50 now paper-only (lost -$66 on 214 trades). (3) Regime flip cap raised 0.50→0.55 (flips at 0.50-0.55 are profitable). (4) EE mid-tier reverted to 0.90 (flat 0.90 outperforms tiered, $329 vs $226). (5) Spread filter: skip live when spread > $0.03 (-$1.15/trade at wide spreads). | `docs/plans/2026-03-10-deep-analysis-report.md` |
```

Update the Safety Filters section (#2 entry price filter) to reflect the new 0.50 boundary:

Change:
```
2. **Entry price filter**: Hard reject outside 0.25-0.65. Exploration range 0.25-0.40 is paper-only
```
to:
```
2. **Entry price filter**: Hard reject outside 0.25-0.65. Exploration range 0.25-0.50 is paper-only (normal trades below 0.50 lose money per deep analysis)
```

- [ ] **Step 2: Commit ALL Track B changes in one commit (code + CLAUDE.md together)**

```bash
git add config.py strategy/edge.py main.py CLAUDE.md
git commit -m "feat: quick wins — raise confidence, widen exploration, flip cap 0.55, spread filter, EE mid revert"
```

- [ ] **Step 3: Push to GitHub**

```bash
git push origin HEAD
```

- [ ] **Step 4: Deploy to VPS**

```bash
# Deploy changed files (note: edge.py goes to strategy/ subdirectory)
scp config.py main.py CLAUDE.md root@65.21.178.90:/home/btcedge/BTC-tool/
scp strategy/edge.py root@65.21.178.90:/home/btcedge/BTC-tool/strategy/

# Remove mid-tier override from .env if present
ssh root@65.21.178.90 "grep -q EARLY_EXIT_THRESHOLD_MID /home/btcedge/BTC-tool/.env && sed -i '/EARLY_EXIT_THRESHOLD_MID/d' /home/btcedge/BTC-tool/.env; echo done"

# Set MIN_CONFIDENCE in .env
ssh root@65.21.178.90 "grep -q MIN_CONFIDENCE /home/btcedge/BTC-tool/.env && sed -i 's/MIN_CONFIDENCE=.*/MIN_CONFIDENCE=0.020/' /home/btcedge/BTC-tool/.env || echo 'MIN_CONFIDENCE=0.020' >> /home/btcedge/BTC-tool/.env"

# Restart service
ssh root@65.21.178.90 "systemctl stop btc-edge; sleep 2; systemctl start btc-edge"

# Verify running
ssh root@65.21.178.90 "sleep 5 && systemctl is-active btc-edge && tail -30 /home/btcedge/BTC-tool/btc_edge.log"
```

Expected: Service active, logs showing startup with new config values. Look for `min_confidence=0.020` in EdgeDetector init log.

- [ ] **Step 5: Tag deploy and push tag**

```bash
git tag -a deploy-$(date -u +%Y-%m-%d-%H%M) -m "deployed: quick wins (confidence, entry filter, spread, EE revert)"
git push --tags
```

---

## Chunk 3: Track C — Dual-Position Paper Tracking

### Task 7: Enhance shadow tracking for dual-position simulation

**Files:**
- Modify: `main.py:~1095-1140` — enhance `_save_shadow_window` to compute and store dual-position simulated P&L
- Modify: `storage/db.py` — add dual-position columns to shadow_windows table

**Context:** The shadow_windows table already collects both-side max_bid data. We need to add simulated dual-position P&L computation so we can track forward performance without risking real money.

- [ ] **Step 1: Add dual-position columns to shadow_windows migration**

In `storage/db.py`, find `_migrate_spread_columns()` method (line 174). At the END of the `alter_statements` list (after the last `("skipped_windows", ...)` entry, around line 253), add:

```python
            # Dual-position paper simulation (2026-03-10)
            ("shadow_windows", "dual_up_entry", "REAL"),
            ("shadow_windows", "dual_down_entry", "REAL"),
            ("shadow_windows", "dual_up_exit", "TEXT"),
            ("shadow_windows", "dual_down_exit", "TEXT"),
            ("shadow_windows", "dual_up_pnl", "REAL"),
            ("shadow_windows", "dual_down_pnl", "REAL"),
            ("shadow_windows", "dual_total_pnl", "REAL"),
            ("shadow_windows", "dual_scenario", "TEXT"),
```

This uses the existing ALTER TABLE migration loop (lines 255-261) that safely skips columns that already exist.

- [ ] **Step 2: Verify the schema migration runs**

```bash
python -c "import asyncio; from storage.db import Database; db = Database('btc_edge.db'); asyncio.run(db.initialize()); print('OK')"
```

Expected: "OK" with no errors. New columns added to shadow_windows.

- [ ] **Step 3: Enhance shadow window save logic in main.py**

Find the `_save_shadow_window` method in `main.py` (around line 1095-1140). After computing `up_max_bid` and `down_max_bid`, add dual-position simulation:

```python
        # --- Dual-position paper simulation ---
        # Simulate buying both sides at window open and EE'ing each independently
        dual_threshold = 0.70  # from backtest analysis
        dual_bet = 5.0
        from data.polymarket import compute_fee_factor as _cff

        dual_up_entry = up_entry_ask if up_entry_ask else None
        dual_down_entry = down_entry_ask if down_entry_ask else None
        dual_up_exit = None
        dual_down_exit = None
        dual_up_pnl = None
        dual_down_pnl = None
        dual_total_pnl = None
        dual_scenario = None

        if dual_up_entry and dual_down_entry and dual_up_entry > 0.05 and dual_down_entry > 0.05:
            # Shares purchased (with fees)
            up_ff = _cff(dual_up_entry)
            down_ff = _cff(dual_down_entry)
            up_shares = (dual_bet / dual_up_entry) * (1.0 - up_ff)
            down_shares = (dual_bet / dual_down_entry) * (1.0 - down_ff)

            # Check if each side hit threshold
            up_ee = up_max_bid and up_max_bid >= dual_threshold
            down_ee = down_max_bid and down_max_bid >= dual_threshold

            # Determine settlement winner
            btc_went_up = btc_end > btc_start if btc_start and btc_end else None

            if up_ee:
                # Sell at threshold (conservative — real would sell at actual bid)
                sell_ff = _cff(dual_threshold)
                proceeds = up_shares * dual_threshold * (1.0 - sell_ff)
                dual_up_pnl = proceeds - dual_bet
                dual_up_exit = "EE"
            elif btc_went_up is not None:
                dual_up_pnl = (up_shares - dual_bet) if btc_went_up else -dual_bet
                dual_up_exit = "SETTLE_WIN" if btc_went_up else "SETTLE_LOSS"

            if down_ee:
                sell_ff = _cff(dual_threshold)
                proceeds = down_shares * dual_threshold * (1.0 - sell_ff)
                dual_down_pnl = proceeds - dual_bet
                dual_down_exit = "EE"
            elif btc_went_up is not None:
                down_won = not btc_went_up
                dual_down_pnl = (down_shares - dual_bet) if down_won else -dual_bet
                dual_down_exit = "SETTLE_WIN" if down_won else "SETTLE_LOSS"

            if dual_up_pnl is not None and dual_down_pnl is not None:
                dual_total_pnl = dual_up_pnl + dual_down_pnl
                if up_ee and down_ee:
                    dual_scenario = "both_ee"
                elif up_ee or down_ee:
                    dual_scenario = "one_ee"
                else:
                    dual_scenario = "no_ee"
```

Then update `save_shadow_window()` in `storage/db.py` (line 464-523):

**Add 8 new parameters** after `entry_atr` (line 490):
```python
        entry_atr: Optional[float] = None,
        # Dual-position paper simulation
        dual_up_entry: Optional[float] = None,
        dual_down_entry: Optional[float] = None,
        dual_up_exit: Optional[str] = None,
        dual_down_exit: Optional[str] = None,
        dual_up_pnl: Optional[float] = None,
        dual_down_pnl: Optional[float] = None,
        dual_total_pnl: Optional[float] = None,
        dual_scenario: Optional[str] = None,
    ) -> None:
```

**Add to INSERT column list** (after `entry_atr` on line 506):
```sql
                     entry_ema_cross, entry_funding_zscore,
                     entry_volume_zscore, entry_atr,
                     dual_up_entry, dual_down_entry, dual_up_exit, dual_down_exit,
                     dual_up_pnl, dual_down_pnl, dual_total_pnl, dual_scenario)
```

**Update VALUES placeholders** to have 33 `?` (was 25):
```sql
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

**Add to the values tuple** (after `entry_atr` on line 518):
```python
                 entry_ema_cross, entry_funding_zscore,
                 entry_volume_zscore, entry_atr,
                 dual_up_entry, dual_down_entry, dual_up_exit, dual_down_exit,
                 dual_up_pnl, dual_down_pnl, dual_total_pnl, dual_scenario),
```

Then in `main.py`, update the `_save_shadow_data()` call (line 1140) to pass the dual columns:
```python
        await self.db.save_shadow_window(
            # ... existing params ...
            entry_atr=fv.atr if fv else None,
            # Dual-position paper simulation
            dual_up_entry=dual_up_entry,
            dual_down_entry=dual_down_entry,
            dual_up_exit=dual_up_exit,
            dual_down_exit=dual_down_exit,
            dual_up_pnl=dual_up_pnl,
            dual_down_pnl=dual_down_pnl,
            dual_total_pnl=dual_total_pnl,
            dual_scenario=dual_scenario,
        )
```

**NOTE:** Shadow data uses 10-second polling (vs 1-second live monitoring). Dual P&L is therefore conservative — it will underestimate EE trigger rates. Add this comment near the dual simulation code:
```python
        # NOTE: max_bid from 10s polling underestimates true max.
        # Dual P&L is conservative — forward validation will show higher trigger rates.
```

- [ ] **Step 4: Verify syntax**

```bash
python -m py_compile main.py && python -m py_compile storage/db.py
```

Expected: No output (success).

- [ ] **Step 5: Commit**

```bash
git add main.py storage/db.py
git commit -m "feat: dual-position paper simulation in shadow window tracking"
```

### Task 8: Add /dual Telegram command for monitoring

**Files:**
- Modify: `main.py` — add `/dual` command handler that queries shadow_windows dual-position results

- [ ] **Step 1: Find existing Telegram command registration**

Read `main.py` and find where Telegram commands are registered (look for `application.add_handler` calls). Add a new `/dual` command.

- [ ] **Step 2: Add the /dual command handler**

```python
    async def _cmd_dual(self, update, context):
        """Show dual-position paper trading results from shadow windows."""
        try:
            cursor = await self.db._db.execute("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN dual_total_pnl IS NOT NULL THEN 1 ELSE 0 END) as valid,
                       SUM(dual_total_pnl) as total_pnl,
                       AVG(dual_total_pnl) as avg_pnl,
                       SUM(CASE WHEN dual_scenario='both_ee' THEN 1 ELSE 0 END) as both_ee,
                       SUM(CASE WHEN dual_scenario='one_ee' THEN 1 ELSE 0 END) as one_ee,
                       SUM(CASE WHEN dual_scenario='no_ee' THEN 1 ELSE 0 END) as no_ee,
                       SUM(CASE WHEN dual_total_pnl > 0 THEN 1 ELSE 0 END) as wins
                FROM shadow_windows
                WHERE dual_total_pnl IS NOT NULL
            """)
            row = await cursor.fetchone()
            if not row or not row[1]:
                await update.message.reply_text("No dual-position data yet. Collecting...")
                return

            n, valid, total, avg, both, one, none_, wins = row
            wr = wins / valid if valid else 0
            daily_est = avg * 288 if avg else 0

            msg = (
                f"DUAL-POSITION PAPER (shadow)\n"
                f"Windows: {valid} (of {n} total)\n"
                f"PnL: ${total:+.2f} (${avg:+.3f}/window)\n"
                f"Win rate: {wr:.1%}\n"
                f"Both EE: {both} | One EE: {one} | No EE: {none_}\n"
                f"Daily est: ${daily_est:+.2f}/day"
            )
            await update.message.reply_text(msg)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
```

Register the handler alongside existing commands (in `main.py`, find line ~414 where other commands are registered):
```python
        self.alerter.register_command("dual", self._cmd_dual)
```

**Important:** This project uses `self.alerter.register_command()`, NOT `application.add_handler(CommandHandler(...))`. Follow the existing pattern at lines 403-414.

- [ ] **Step 3: Verify syntax**

```bash
python -m py_compile main.py
```

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: /dual Telegram command for dual-position paper results"
```

### Task 9: Deploy Track C to VPS

**Files:**
- Modify: `CLAUDE.md` — add changelog entry

- [ ] **Step 1: Add changelog entry**

```markdown
| 2026-03-10 ~XX:XX | **Dual-position paper tracking**: Shadow windows now simulate dual-position EE at 0.70 threshold and store per-window P&L. New `/dual` Telegram command shows results. Forward validation for dual-position strategy. | `docs/plans/2026-03-10-parallel-improvement-plan.md` |
```

- [ ] **Step 2: Commit (code + CLAUDE.md together)**

```bash
git add main.py storage/db.py CLAUDE.md
git commit -m "feat: dual-position paper simulation in shadow tracking + /dual command"
```

- [ ] **Step 3: Push to GitHub**

```bash
git push origin HEAD
```

- [ ] **Step 4: Deploy to VPS**

**PREREQUISITE:** Task 6 (Track B deploy) must have completed successfully first. This deploy includes Track C files only — Track B files (config.py, strategy/edge.py) should already be on the VPS.

```bash
# Deploy Track C files
scp main.py CLAUDE.md root@65.21.178.90:/home/btcedge/BTC-tool/
scp storage/db.py root@65.21.178.90:/home/btcedge/BTC-tool/storage/

# Restart
ssh root@65.21.178.90 "systemctl stop btc-edge; sleep 2; systemctl start btc-edge"

# Verify
ssh root@65.21.178.90 "sleep 5 && systemctl is-active btc-edge && tail -30 /home/btcedge/BTC-tool/btc_edge.log"
```

- [ ] **Step 5: Tag deploy and push**

```bash
git tag -a deploy-$(date -u +%Y-%m-%d-%H%M) -m "deployed: dual-position paper tracking + /dual command"
git push --tags
```

---

## Execution Notes

### Track Dependencies
- **Track A** (backtest) is fully independent — can run anytime
- **Track B** (quick wins) is fully independent — deploy immediately
- **Track C** (dual paper) is independent but should deploy AFTER Track B so the restart includes both

**Recommended execution order:**
1. Track A first (answers the biggest question)
2. Track B next (quick wins, ship to VPS)
3. Track C last (deploy with Track B in same restart if possible)

### After Execution
- Review backtest results — if dual-position is clearly unprofitable, skip Track C
- If backtest is promising, Track C collects forward validation data
- Use `/dual` command after 24-48 hours to check forward results
- Compare forward dual-position paper P&L to actual single-side live P&L

### What NOT to Do
- Do not build an exit-probability ML model (R-squared = 0.004, dead end)
- Do not lower EE thresholds for middle tiers (data shows flat 0.90 is better)
- Do not go live with dual-position without 2+ weeks of paper validation
