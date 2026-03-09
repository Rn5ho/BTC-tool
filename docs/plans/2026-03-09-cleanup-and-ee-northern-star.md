# Cleanup & EE Northern Star Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Clean up the codebase, fix critical bugs blocking data collection, and lay the foundation for an EE-first trading system that profits from bid-spike harvesting across all hours.

**Architecture:** Three phases — (1) Cleanup: fix bugs, move scripts, sync config/code/docs, (2) Deploy & Collect: get shadow tracking working on VPS so exit-probability data accumulates, (3) Exit ML Foundation: build the first exit-probability model once data is available. Phase 1+2 are this plan. Phase 3 is a follow-up plan once we have 500+ shadow windows.

**Tech Stack:** Python 3.11, SQLite, asyncio, scikit-learn (future), Polymarket CLOB API

**Northern Star:** A system that trades every 5-min window, uses a directional model to enter positions, and relies on an intelligent EE system (dynamic thresholds, exit-probability awareness) to harvest profit from bid spikes — making the average trade profitable across all hours.

---

## Phase 1: Cleanup & Bug Fixes

### Task 1: Fix Shadow Tracking Bug (Critical — Blocking Data Collection)

**Files:**
- Fix: `main.py:1163`

**Context:** Shadow tracking has been broken since deploy (2026-03-08). The `_save_shadow_data()` method references `fv.funding_zscore` but the FeatureVector field is `fv.funding_rate`. Every save attempt crashes with `AttributeError`, producing 0 rows in `shadow_windows`. This is the #1 blocker — we need this data for the exit-probability model.

**Step 1: Fix the field name**

In `main.py` line 1163, change:
```python
entry_funding_zscore=fv.funding_zscore if fv else None,
```
to:
```python
entry_funding_zscore=fv.funding_rate if fv else None,
```

**Step 2: Verify syntax**

Run: `python -m py_compile main.py`
Expected: No output (success)

**Step 3: Commit**

```bash
git add main.py
git commit -m "fix: shadow tracking AttributeError — fv.funding_zscore → fv.funding_rate

Shadow book loop has been crashing on every window save since deploy (2026-03-08).
FeatureVector field is 'funding_rate', not 'funding_zscore'. Zero shadow windows collected.
This fix unblocks exit-probability data collection.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Wire EE Thresholds to Config (Fix Config/Code Mismatch)

**Files:**
- Fix: `strategy/live_trader.py:617-620`
- Fix: `config.py:86-87` (update defaults to match current code truth)

**Context:** Two middle EE tiers are hardcoded in `get_exit_threshold()` instead of reading from config. The config defaults are also stale (0.45 and 0.65) while code uses 0.65 and 0.90. Fix both: update config defaults to current values AND wire them in.

**Step 1: Update config defaults to match current behavior**

In `config.py`, change lines 86-87:
```python
early_exit_threshold_low_mid: float = 0.45 # entry 0.35-0.40: brief spikes, grab profit fast
early_exit_threshold_mid: float = 0.65     # entry 0.40-0.50: decent exit rate at 0.65
```
to:
```python
early_exit_threshold_low_mid: float = 0.65 # entry 0.35-0.40: raised from 0.45 on 2026-03-05
early_exit_threshold_mid: float = 0.90     # entry 0.40-0.50: raised from 0.65 on 2026-03-05
```

**Step 2: Wire config into live_trader.py**

In `strategy/live_trader.py`, change lines 617-620:
```python
if entry_price < 0.40:
    return 0.65
if entry_price < 0.50:
    return 0.90
```
to:
```python
if entry_price < 0.40:
    return settings.early_exit_threshold_low_mid
if entry_price < 0.50:
    return settings.early_exit_threshold_mid
```

**Step 3: Verify syntax**

Run: `python -m py_compile strategy/live_trader.py config.py`
Expected: No output (success)

**Step 4: Commit**

```bash
git add strategy/live_trader.py config.py
git commit -m "fix: wire EE thresholds to config, update defaults to 0.65/0.90

Middle tiers (0.35-0.40 and 0.40-0.50) were hardcoded in get_exit_threshold()
while config had stale defaults (0.45/0.65). Now config is source of truth.
No behavior change — code already used 0.65/0.90, defaults now match.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Move Analysis Scripts to `scripts/` Directory

**Files:**
- Move: 27 `analyze_*.py` files → `scripts/analysis/`
- Move: 8 `simulate_*.py` files → `scripts/simulation/`
- Keep in root: `backfill_gamma_resolution.py`, `backfill_outcomes.py` (operational tools, run regularly)

**Context:** 37 scripts clutter the project root. They're one-off analysis tools, not part of the runtime system. Moving them cleans up the project structure. Backfill scripts stay in root because they're operational (run periodically to maintain data quality).

**Step 1: Create directories**

```bash
mkdir -p scripts/analysis scripts/simulation
```

**Step 2: Move analysis scripts**

```bash
git mv analyze_24h_regime.py scripts/analysis/
git mv analyze_bid_spikes.py scripts/analysis/
git mv analyze_clob.py scripts/analysis/
git mv analyze_clob_exits.py scripts/analysis/
git mv analyze_early_close.py scripts/analysis/
git mv analyze_early_exit_impact.py scripts/analysis/
git mv analyze_ee_trend.py scripts/analysis/
git mv analyze_entry_filter.py scripts/analysis/
git mv analyze_exit_combined.py scripts/analysis/
git mv analyze_exit_deep.py scripts/analysis/
git mv analyze_exit_extended.py scripts/analysis/
git mv analyze_exit_final.py scripts/analysis/
git mv analyze_exit_optimize.py scripts/analysis/
git mv analyze_exit_sweet_spot.py scripts/analysis/
git mv analyze_exit_tiers.py scripts/analysis/
git mv analyze_flip_hypothetical.py scripts/analysis/
git mv analyze_full_3day.py scripts/analysis/
git mv analyze_funnel.py scripts/analysis/
git mv analyze_leak.py scripts/analysis/
git mv analyze_overnight.py scripts/analysis/
git mv analyze_pnl_gap.py scripts/analysis/
git mv analyze_regime_deep.py scripts/analysis/
git mv analyze_skipped_pnl.py scripts/analysis/
git mv analyze_skipped_v2.py scripts/analysis/
git mv analyze_skips.py scripts/analysis/
git mv analyze_trades.py scripts/analysis/
git mv analyze_trend_streaks.py scripts/analysis/
```

**Step 3: Move simulation scripts**

```bash
git mv simulate_clean.py scripts/simulation/
git mv simulate_compare.py scripts/simulation/
git mv simulate_compounding.py scripts/simulation/
git mv simulate_corrected.py scripts/simulation/
git mv simulate_fixed.py scripts/simulation/
git mv simulate_historical.py scripts/simulation/
git mv simulate_phased.py scripts/simulation/
git mv simulate_portfolio.py scripts/simulation/
```

**Step 4: Commit**

```bash
git add -A
git commit -m "chore: move 35 analysis/simulation scripts to scripts/

27 analyze_*.py → scripts/analysis/
8 simulate_*.py → scripts/simulation/
Backfill scripts stay in root (operational tools run regularly).
No runtime impact — these are one-off analysis tools.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Update .env.example With All Current Settings

**Files:**
- Fix: `.env.example`

**Context:** .env.example is missing 14+ settings that exist in config.py. Anyone setting up the project (or reviewing config) would be lost. Update it to match reality.

**Step 1: Regenerate .env.example from config.py**

Create a complete .env.example that documents every setting in config.py with its current default value. Group by function. Include comments explaining each setting. Mark dangerous settings (private key, funder address) with placeholder values.

Key additions needed:
- All 4 `EARLY_EXIT_THRESHOLD_*` settings (with updated defaults)
- `REGIME_TREND_THRESHOLD`, `REGIME_FLIP_THRESHOLD`, `REGIME_FLIP_LIVE`, `REGIME_FLIP_CONFIRM_WINDOWS`
- `STREAK_PAUSE_THRESHOLD`, `STREAK_PAUSE_WINDOWS`
- `SIZING_STRATEGY`, `MIN_CONFIDENCE`, `CONFIDENCE_DAMPEN`
- Fix stale defaults: `CONFIDENCE_DAMPEN=1.0` (not 0.6)
- Remove dead fields if any exist in .env.example that have no config.py counterpart

**Step 2: Verify no syntax issues**

Manually check that every key in .env.example has a matching field in config.py.

**Step 3: Commit**

```bash
git add .env.example
git commit -m "docs: update .env.example with all 30+ current settings

Was missing 14 settings (EE thresholds, regime, streak guard, sizing).
Fixed stale defaults (CONFIDENCE_DAMPEN was 0.6, now 1.0).
Grouped by function with comments.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Update CLAUDE.md Stale Sections

**Files:**
- Fix: `CLAUDE.md`

**Context:** Several sections have drifted from reality. Fix the most impactful ones:

**Step 1: Fix EE threshold table**

Update the "Adaptive Early Exit" section's threshold table to match current code:

| Entry Price | Exit Threshold | Rationale |
|-------------|---------------|-----------|
| < 0.35 | 0.50 | Lottery tickets — spike briefly, grab any profit |
| 0.35 - 0.40 | 0.65 | Raised from 0.45 (2026-03-05) — old threshold gave $0.50 margin |
| 0.40 - 0.50 | 0.90 | Raised from 0.65 (2026-03-05) — 76% would-win rate, stop clipping winners |
| >= 0.50 | 0.95 | High WR, conservative — let winners run |

**Step 2: Fix Telegram commands table**

Update count from 10 to 12, add `/ee` and `/spread` commands.

**Step 3: Add scripts/ to Architecture section**

Add `scripts/` directory to the architecture tree:
```
scripts/
  analysis/    → 27 one-off analysis scripts (bid spikes, exits, regimes, etc.)
  simulation/  → 8 Monte Carlo / historical replay scripts
```

**Step 4: Add changelog entry for this cleanup**

Add row to changelog table.

**Step 5: Commit (with code changes from Tasks 1-4)**

This commit should be combined with or follow the code changes. CLAUDE.md changelog must travel with the code per project conventions.

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md — EE thresholds, commands, architecture

Fix stale EE threshold table (0.65/0.90 not 0.45/0.65).
Add /ee and /spread to command list (12 commands, not 10).
Add scripts/ directory to architecture tree.
Add changelog entry for cleanup.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Phase 2: Deploy & Start Collecting Data

### Task 6: Deploy All Fixes to VPS

**Files:** All changed files from Tasks 1-5

**Step 1: Deploy changed files**

```bash
scp main.py config.py strategy/live_trader.py root@65.21.178.90:/home/btcedge/BTC-tool/
```

Note: .env.example and CLAUDE.md don't need deploying (not runtime files). Scripts moved to scripts/ don't need deploying either (analysis tools, not runtime).

**Step 2: Restart service**

```bash
ssh root@65.21.178.90 "systemctl kill -s SIGKILL btc-edge; systemctl reset-failed btc-edge; systemctl start btc-edge"
```

**Step 3: Verify shadow tracking works**

Wait ~60 seconds for at least one window transition, then:

```bash
ssh root@65.21.178.90 "sleep 60 && sqlite3 /home/btcedge/BTC-tool/btc_edge.db 'SELECT COUNT(*) FROM shadow_windows'"
```

Expected: >= 1 (growing by ~12/hour = one per 5-min window)

If still 0, check logs:
```bash
ssh root@65.21.178.90 "grep -i shadow /home/btcedge/BTC-tool/btc_edge.log | tail -5"
```

**Step 4: Verify trading still works**

```bash
ssh root@65.21.178.90 "tail -30 /home/btcedge/BTC-tool/btc_edge.log | grep -iE '(LIVE|FILLED|SKIP|shadow)'"
```

Expected: Normal trading messages + "Saved shadow window" log lines.

**Step 5: Tag the deploy**

```bash
git tag -a deploy-$(date -u +%Y-%m-%d-%H%M) -m "deployed: cleanup + shadow tracking fix + EE config wiring"
```

---

### Task 7: Run Gamma Resolution Backfill

**Context:** 259 EE trades have NULL gamma_winner_matches. Backfilling gives us ground truth for all historical trades.

**Step 1: Run backfill**

```bash
python backfill_gamma_resolution.py --apply
```

Expected: Updates 200+ trades with gamma_resolution and gamma_winner_matches.

**Step 2: Verify**

```bash
python -c "
import sqlite3
c = sqlite3.connect('btc_edge.db').cursor()
c.execute('SELECT COUNT(*) FROM live_trades WHERE outcome=\"EARLY_EXIT\" AND gamma_winner_matches IS NULL')
print(f'EE trades still missing gamma: {c.fetchone()[0]}')
"
```

Expected: Close to 0 (only very recent unsettled trades).

---

## Phase 3: Exit ML Foundation (Future — Requires Data from Phase 2)

> **Prerequisite:** Shadow tracking must be collecting data for 2-3 days (~500-800 windows) before starting this phase. Check progress with:
> ```bash
> ssh root@65.21.178.90 "sqlite3 /home/btcedge/BTC-tool/btc_edge.db 'SELECT COUNT(*) FROM shadow_windows'"
> ```

### Task 8: Design the Exit Probability Model (Planning Only)

This task is a design session, not implementation. When 500+ shadow windows are collected, create a new plan document for the exit-probability model:

**Key design questions to answer:**
1. **Target variable**: `max_bid >= exit_threshold` (binary) or `max_bid` (regression)?
2. **Features**: Which of the 11 entry-time Binance features + entry price + spread + regime predict exit probability?
3. **Integration**: Does the model set dynamic EE thresholds? Or does it gate trading (skip windows with low exit probability)? Or both?
4. **Training**: How to split shadow_windows data (time-series, not random)?
5. **Deployment**: New pkl artifact? Inference in the EE monitoring loop or at entry time?

**The hypothesis to validate:**
With the right exit-probability model, we can:
- Skip windows where EE is unlikely (reducing settlement losses)
- Lower thresholds when EE is very likely (capturing more profit)
- Size up on high-exit-probability windows
- Make every hour profitable (instead of hour-blocking)

This replaces hourly blacklisting with per-window intelligence — addressing the user's goal of 24/7 participation.

### Task 9: Retrain Direction Model (Parallel Track)

**Context:** Current model was trained on Nov 2025 - Feb 2026 data (119 days). It's been live for 9 days. True model WR has degraded from 53.9% backtest to 47.1% live (concept drift). Regular retraining could recover 2-3% WR.

**When to do this:**
- After Phase 2 is stable (shadow tracking collecting, VPS running clean)
- Run `python ml_pipeline.py` to retrain with updated data (downloads 120 days from Binance, ~25 min)
- Compare new model accuracy vs current
- Deploy if improved

**Consider adding new features:**
- `entry_price` (the Polymarket entry cost — captures market's implied probability)
- `spread` (bid-ask spread — captures liquidity/uncertainty)
- `regime_strength` (continuous, not just binary trending/ranging)
- `hour_bucket` (learned hourly patterns directly)

---

## Summary: What This Plan Achieves

| Phase | Tasks | Impact | Timeline |
|-------|-------|--------|----------|
| **1: Cleanup** | Tasks 1-5 | Fix shadow tracking, wire config, clean project | 1-2 hours |
| **2: Deploy** | Tasks 6-7 | Start collecting exit data, backfill gamma truth | 30 min + wait |
| **3: Exit ML** | Tasks 8-9 | Design + build the EE-first model (future plan) | After 2-3 days of data |

**Immediate value:** Shadow tracking starts collecting ~288 windows/day. After 3 days we have ~800 windows — enough to train the first exit-probability model and take the next step toward the northern star.

**What stays the same:** Trading continues 24/7 with current model. No behavior changes in Phase 1-2 (config defaults match existing behavior). Phase 3 is where the strategy evolves.
