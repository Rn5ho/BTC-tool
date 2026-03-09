"""
Historical simulation: Replay entire live trading history with CURRENT rules.

Bankroll timeline (real):
- Feb 28: $73 starting balance ($30 initial + $43 early deposit)
- Mar 3: +$43 deposit (Berachain dust)

Current rules applied uniformly:
- Entry price filter: live only 0.40-0.65 (below 0.40 = paper-only)
- MIN_CONFIDENCE = 0.015
- CONFIDENCE_DAMPEN = 0.6
- Regime flip at |strength| >= 0.35
- Streak guard: 3 consecutive same-side losses -> pause 2 windows
- 4-tier early exit thresholds
- Adaptive sizing (8% of bankroll, hour multipliers, max $10)
- Polymarket fees (rate=0.25, exponent=2)
"""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

# --- Config (current settings) -------------------------------
INITIAL_BANKROLL = 73.0
DEPOSIT_DATE = "2026-03-03"
DEPOSIT_AMOUNT = 43.0
MAX_BET = 10.0
BANKROLL_PCT = 0.08
MIN_CONFIDENCE = 0.015
CONFIDENCE_DAMPEN = 0.6
ENTRY_PRICE_MIN_LIVE = 0.40
ENTRY_PRICE_MAX = 0.65
REGIME_FLIP_THRESHOLD = 0.35
STREAK_PAUSE_THRESHOLD = 3
STREAK_PAUSE_WINDOWS = 2
FEE_RATE = 0.25
FEE_EXPONENT = 2
BLACKLIST_HOURS = {2}

# Early exit thresholds
def get_exit_threshold(entry_price):
    if entry_price < 0.35:
        return 0.50
    elif entry_price < 0.40:
        return 0.45
    elif entry_price < 0.50:
        return 0.65
    else:
        return 0.95

# Hour multipliers
HOUR_MULTIPLIERS = {
    4: 0.7, 7: 0.7,
    6: 1.2, 8: 1.2, 10: 1.2, 12: 1.2,
    16: 1.2, 18: 1.2, 22: 1.2,
    9: 1.5, 14: 1.5, 20: 1.5,
}

def compute_fee_factor(entry_price, fee_rate=FEE_RATE, fee_exponent=FEE_EXPONENT):
    return (fee_rate / 10000) * (entry_price ** fee_exponent)

def compute_pnl_win(amount_usdc, entry_price):
    fee = compute_fee_factor(entry_price)
    shares = (amount_usdc / entry_price) * (1.0 - fee)
    return shares - amount_usdc

def compute_pnl_loss(amount_usdc):
    return -amount_usdc

def compute_bet_size(bankroll, hour, entry_price):
    base = bankroll * BANKROLL_PCT
    mult = HOUR_MULTIPLIERS.get(hour, 1.0)
    size = base * mult
    # Clamp
    size = max(1.0, min(size, MAX_BET))
    # CLOB minimum: 5 tokens minimum, so min USDC = max(5.5 * price, 3.50)
    min_usdc = max(5.5 * entry_price, 3.50)
    size = max(size, min_usdc)
    size = min(size, MAX_BET)
    return round(size, 2)


def main():
    conn = sqlite3.connect("btc_edge.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get all settled live trades
    c.execute("""
        SELECT created_at, side, entry_price, outcome, pnl, amount_usdc, trade_tag,
               model_confidence, btc_price_at_open, settlement_price, fill_price,
               max_bid_during_window, exit_threshold_used,
               regime_state, regime_strength, market_slug
        FROM live_trades
        WHERE outcome IS NOT NULL AND success=1
        ORDER BY created_at
    """)
    trades = [dict(row) for row in c.fetchall()]
    conn.close()

    print("=" * 80)
    print(f"  HISTORICAL SIMULATION -- Current Rules on {len(trades)} Trades")
    print(f"  Bankroll: ${INITIAL_BANKROLL} start + ${DEPOSIT_AMOUNT} deposit on {DEPOSIT_DATE}")
    print("=" * 80 + "\n")

    # --- State ------------------------------------------------
    bankroll = INITIAL_BANKROLL
    deposit_added = False
    total_pnl = 0.0
    peak_bankroll = bankroll
    max_drawdown = 0.0
    max_drawdown_pct = 0.0

    # Actual tracking
    actual_bankroll = INITIAL_BANKROLL
    actual_total_pnl = 0.0

    # Streak guard state
    streak_up = 0  # consecutive UP losses
    streak_down = 0  # consecutive DOWN losses
    pause_up_until = None  # skip UP until this window
    pause_down_until = None

    # Counters
    sim_taken = 0
    sim_skipped = 0
    sim_wins = 0
    sim_losses = 0
    sim_ee = 0
    sim_pnl = 0.0
    sim_volume = 0.0
    skip_reasons = defaultdict(int)

    actual_wins = 0
    actual_losses = 0
    actual_ee = 0
    actual_pnl_total = 0.0
    actual_volume = 0.0

    # Daily tracking
    daily_sim = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "ee": 0, "volume": 0.0, "skipped": 0})
    daily_actual = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "ee": 0, "volume": 0.0})

    # Bankroll curve
    bankroll_curve = []
    actual_curve = []

    prev_window = None

    for t in trades:
        created = t["created_at"]
        day = created[:10]
        hour = int(created[11:13])
        side = t["side"]
        entry_price = t["entry_price"]
        outcome = t["outcome"]
        actual_pnl_val = t["pnl"] or 0.0
        actual_amt = t["amount_usdc"] or 0.0
        tag = t["trade_tag"]
        confidence = t["model_confidence"]
        regime_state = t["regime_state"]
        regime_strength = t["regime_strength"]
        max_bid = t["max_bid_during_window"]
        slug = t["market_slug"]

        # Determine 5-min window from slug or timestamp
        window_key = created[:16]  # YYYY-MM-DD HH:MM

        # --- Deposit check --------------------------------
        if not deposit_added and day >= DEPOSIT_DATE:
            bankroll += DEPOSIT_AMOUNT
            actual_bankroll += DEPOSIT_AMOUNT
            deposit_added = True
            print(f"  +++ ${DEPOSIT_AMOUNT} deposit on {DEPOSIT_DATE} -> bankroll ${bankroll:.2f}")

        # --- Track actual ---------------------------------
        actual_bankroll += actual_pnl_val
        actual_total_pnl += actual_pnl_val
        actual_volume += actual_amt
        d_a = daily_actual[day]
        d_a["trades"] += 1
        d_a["pnl"] += actual_pnl_val
        d_a["volume"] += actual_amt
        if outcome == "WIN":
            actual_wins += 1
            d_a["wins"] += 1
        elif outcome == "LOSS":
            actual_losses += 1
            d_a["losses"] += 1
        elif outcome == "EARLY_EXIT":
            actual_ee += 1
            d_a["ee"] += 1
        actual_curve.append((created, actual_bankroll, actual_total_pnl))

        # --- Apply current filters -----------------------
        skip_reason = None

        # 1. Hour blacklist
        if hour in BLACKLIST_HOURS:
            skip_reason = "blacklist_hour"

        # 2. Entry price filter (paper-only below 0.40)
        if not skip_reason and entry_price is not None:
            if entry_price < ENTRY_PRICE_MIN_LIVE:
                skip_reason = "entry_price_paper_only"
            elif entry_price > ENTRY_PRICE_MAX:
                skip_reason = "entry_price_too_high"

        # 3. Confidence filter (only if we have data)
        if not skip_reason and confidence is not None:
            dampened_conf = confidence * CONFIDENCE_DAMPEN
            if dampened_conf < MIN_CONFIDENCE:
                skip_reason = "low_confidence"

        # 4. Streak guard
        if not skip_reason:
            effective_side = side
            # Apply regime flip if applicable
            if regime_strength is not None and abs(regime_strength) >= REGIME_FLIP_THRESHOLD:
                if regime_state == "trending_up":
                    effective_side = "UP"
                elif regime_state == "trending_down":
                    effective_side = "DOWN"

            if effective_side == "UP" and pause_up_until and window_key < pause_up_until:
                skip_reason = "streak_guard_UP"
            elif effective_side == "DOWN" and pause_down_until and window_key < pause_down_until:
                skip_reason = "streak_guard_DOWN"

        # 5. Maker fills -- always include (discovered, not placed)
        if tag == "maker_fill":
            skip_reason = None  # maker fills bypass filters

        if skip_reason:
            sim_skipped += 1
            skip_reasons[skip_reason] += 1
            daily_sim[day]["skipped"] += 1

            # Still update streak tracking based on actual outcome
            # (the market still moved, we just didn't bet)
            bankroll_curve.append((created, bankroll, sim_pnl))
            continue

        # --- TRADE TAKEN ----------------------------------
        sim_taken += 1

        # Determine effective side (with regime flip)
        effective_side = side
        flipped = False
        if regime_strength is not None and abs(regime_strength) >= REGIME_FLIP_THRESHOLD:
            if regime_state == "trending_up" and side != "UP":
                effective_side = "UP"
                flipped = True
            elif regime_state == "trending_down" and side != "DOWN":
                effective_side = "DOWN"
                flipped = True

        # Compute bet size with current adaptive sizing
        bet_size = compute_bet_size(bankroll, hour, entry_price)
        sim_volume += bet_size

        # Determine outcome
        # For flipped trades, we need to figure out the outcome based on settlement
        # Original outcome was based on original side. If we flipped, outcome changes.
        if flipped:
            # If original was WIN, flipping means we'd LOSE (and vice versa)
            # But early exits are based on bid price, not direction
            if outcome == "WIN":
                sim_outcome = "LOSS"
            elif outcome == "LOSS":
                sim_outcome = "WIN"
            else:
                sim_outcome = outcome  # EARLY_EXIT stays the same
        else:
            sim_outcome = outcome

        # Re-simulate early exit if we have max_bid data
        if max_bid is not None and sim_outcome != "EARLY_EXIT":
            threshold = get_exit_threshold(entry_price)
            if max_bid >= threshold:
                # Would have triggered early exit
                # Estimate EE pnl: sell at threshold price
                fee = compute_fee_factor(entry_price)
                tokens = (bet_size / entry_price) * (1.0 - fee)
                sell_proceeds = tokens * threshold
                # Rough sell fee
                sell_fee = compute_fee_factor(threshold)
                sell_proceeds *= (1.0 - sell_fee)
                ee_pnl = sell_proceeds - bet_size
                sim_outcome = "EARLY_EXIT"
                trade_pnl = ee_pnl
            else:
                # No early exit, use settlement outcome
                if sim_outcome == "WIN":
                    trade_pnl = compute_pnl_win(bet_size, entry_price)
                else:
                    trade_pnl = compute_pnl_loss(bet_size)
        elif sim_outcome == "EARLY_EXIT":
            # Use actual EE outcome but scale to our bet size
            if actual_amt > 0 and actual_pnl_val != 0:
                # Scale proportionally
                trade_pnl = (bet_size / actual_amt) * actual_pnl_val
            else:
                # Estimate
                threshold = get_exit_threshold(entry_price)
                fee = compute_fee_factor(entry_price)
                tokens = (bet_size / entry_price) * (1.0 - fee)
                trade_pnl = tokens * threshold - bet_size
        elif sim_outcome == "WIN":
            trade_pnl = compute_pnl_win(bet_size, entry_price)
        else:  # LOSS
            trade_pnl = compute_pnl_loss(bet_size)

        # Update bankroll
        bankroll += trade_pnl
        sim_pnl += trade_pnl
        total_pnl += trade_pnl

        # Track peak/drawdown
        if bankroll > peak_bankroll:
            peak_bankroll = bankroll
        dd = peak_bankroll - bankroll
        if dd > max_drawdown:
            max_drawdown = dd
            max_drawdown_pct = dd / peak_bankroll * 100

        # Update counters
        d_s = daily_sim[day]
        d_s["trades"] += 1
        d_s["pnl"] += trade_pnl
        d_s["volume"] += bet_size
        if sim_outcome == "WIN":
            sim_wins += 1
            d_s["wins"] += 1
        elif sim_outcome == "LOSS":
            sim_losses += 1
            d_s["losses"] += 1
        elif sim_outcome == "EARLY_EXIT":
            sim_ee += 1
            d_s["ee"] += 1

        # Update streak guard (only taker, not maker)
        if tag != "maker_fill" and sim_outcome != "EARLY_EXIT":
            if sim_outcome == "LOSS":
                if effective_side == "UP":
                    streak_up += 1
                    streak_down = 0
                    if streak_up >= STREAK_PAUSE_THRESHOLD:
                        # Parse window time, add 2 windows (10 min)
                        pause_up_until = _add_minutes(window_key, STREAK_PAUSE_WINDOWS * 5)
                        streak_up = 0
                else:
                    streak_down += 1
                    streak_up = 0
                    if streak_down >= STREAK_PAUSE_THRESHOLD:
                        pause_down_until = _add_minutes(window_key, STREAK_PAUSE_WINDOWS * 5)
                        streak_down = 0
            elif sim_outcome == "WIN":
                if effective_side == "UP":
                    streak_up = 0
                else:
                    streak_down = 0

        bankroll_curve.append((created, bankroll, sim_pnl))

    # --- RESULTS ------------------------------------------
    print(f"\n{'-'*80}")
    print(f"  DAILY BREAKDOWN")
    print(f"{'-'*80}")
    print(f"  {'Date':<12} {'Actual':>42} | {'Simulated':>42}")
    print(f"  {'':12} {'Trades':>6} {'W':>4} {'L':>4} {'EE':>4} {'P&L':>9} {'Vol':>10} | {'Trades':>6} {'Skip':>5} {'W':>4} {'L':>4} {'EE':>4} {'P&L':>9} {'Vol':>10}")
    print(f"  {'-'*12} {'-'*42} | {'-'*42}")

    all_days = sorted(set(list(daily_actual.keys()) + list(daily_sim.keys())))
    for day in all_days:
        da = daily_actual.get(day, {"trades":0,"pnl":0,"wins":0,"losses":0,"ee":0,"volume":0})
        ds = daily_sim.get(day, {"trades":0,"pnl":0,"wins":0,"losses":0,"ee":0,"volume":0,"skipped":0})
        print(f"  {day:<12} {da['trades']:>6} {da['wins']:>4} {da['losses']:>4} {da['ee']:>4} {da['pnl']:>+9.2f} {da['volume']:>10.2f} | {ds['trades']:>6} {ds.get('skipped',0):>5} {ds['wins']:>4} {ds['losses']:>4} {ds['ee']:>4} {ds['pnl']:>+9.2f} {ds['volume']:>10.2f}")

    print(f"\n{'-'*80}")
    print(f"  SUMMARY COMPARISON")
    print(f"{'-'*80}")

    actual_settled = actual_wins + actual_losses + actual_ee
    sim_settled = sim_wins + sim_losses + sim_ee
    actual_wr = actual_wins / (actual_wins + actual_losses) * 100 if (actual_wins + actual_losses) > 0 else 0
    sim_wr = sim_wins / (sim_wins + sim_losses) * 100 if (sim_wins + sim_losses) > 0 else 0

    print(f"  {'Metric':<30} {'Actual':>15} {'Simulated':>15} {'Delta':>15}")
    print(f"  {'-'*30} {'-'*15} {'-'*15} {'-'*15}")
    print(f"  {'Trades taken':<30} {actual_settled:>15} {sim_taken:>15} {sim_taken - actual_settled:>+15}")
    print(f"  {'Trades skipped':<30} {'--':>15} {sim_skipped:>15} {'':>15}")
    print(f"  {'Wins':<30} {actual_wins:>15} {sim_wins:>15} {sim_wins - actual_wins:>+15}")
    print(f"  {'Losses':<30} {actual_losses:>15} {sim_losses:>15} {sim_losses - actual_losses:>+15}")
    print(f"  {'Early exits':<30} {actual_ee:>15} {sim_ee:>15} {sim_ee - actual_ee:>+15}")
    print(f"  {'Win rate (W/W+L)':<30} {actual_wr:>14.1f}% {sim_wr:>14.1f}% {sim_wr - actual_wr:>+14.1f}%")
    print(f"  {'Total P&L':<30} {actual_total_pnl:>+14.2f}$ {sim_pnl:>+14.2f}$ {sim_pnl - actual_total_pnl:>+14.2f}$")
    print(f"  {'Volume':<30} {actual_volume:>14.2f}$ {sim_volume:>14.2f}$ {sim_volume - actual_volume:>+14.2f}$")
    print(f"  {'ROI (P&L / Volume)':<30} {actual_total_pnl/actual_volume*100 if actual_volume else 0:>14.1f}% {sim_pnl/sim_volume*100 if sim_volume else 0:>14.1f}%")

    print(f"\n  {'BANKROLL':>30}")
    print(f"  {'-'*30} {'-'*15} {'-'*15}")
    deposits = INITIAL_BANKROLL + (DEPOSIT_AMOUNT if deposit_added else 0)
    print(f"  {'Total deposited':<30} {deposits:>14.2f}$ {deposits:>14.2f}$")
    print(f"  {'Final bankroll':<30} {actual_bankroll:>14.2f}$ {bankroll:>14.2f}$ {bankroll - actual_bankroll:>+14.2f}$")
    print(f"  {'Net profit (bankroll-dep)':<30} {actual_bankroll - deposits:>+14.2f}$ {bankroll - deposits:>+14.2f}$")
    print(f"  {'Peak bankroll':<30} {'--':>15} {peak_bankroll:>14.2f}$")
    print(f"  {'Max drawdown':<30} {'--':>15} {max_drawdown:>14.2f}$ ({max_drawdown_pct:.1f}%)")

    print(f"\n{'-'*80}")
    print(f"  SKIP REASONS (current rules would have skipped {sim_skipped} trades)")
    print(f"{'-'*80}")
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason:<30} {count:>6}")

    # Show bankroll curve at key moments
    print(f"\n{'-'*80}")
    print(f"  BANKROLL CURVE (hourly snapshots)")
    print(f"{'-'*80}")
    print(f"  {'Time':<20} {'Sim Bankroll':>14} {'Sim P&L':>12} {'Act Bankroll':>14} {'Act P&L':>12}")
    print(f"  {'-'*20} {'-'*14} {'-'*12} {'-'*14} {'-'*12}")

    # Sample every ~30 trades for curve
    step = max(1, len(bankroll_curve) // 40)
    actual_idx = 0
    for i in range(0, len(bankroll_curve), step):
        ts, bk, pnl = bankroll_curve[i]
        # Find matching actual
        while actual_idx < len(actual_curve) - 1 and actual_curve[actual_idx][0] < ts:
            actual_idx += 1
        act_ts, act_bk, act_pnl = actual_curve[min(actual_idx, len(actual_curve)-1)]
        print(f"  {ts[:16]:<20} ${bk:>12.2f} {pnl:>+11.2f}$ ${act_bk:>12.2f} {act_pnl:>+11.2f}$")

    # Final
    if bankroll_curve:
        ts, bk, pnl = bankroll_curve[-1]
        act_ts, act_bk, act_pnl = actual_curve[-1]
        print(f"  {'-'*20} {'-'*14} {'-'*12} {'-'*14} {'-'*12}")
        print(f"  {'FINAL':<20} ${bk:>12.2f} {pnl:>+11.2f}$ ${act_bk:>12.2f} {act_pnl:>+11.2f}$")

    # --- WHAT-IF: Impact of each rule ---------------------
    print(f"\n{'-'*80}")
    print(f"  RULE IMPACT ANALYSIS")
    print(f"{'-'*80}")
    print(f"  Shows how many actual trades would be filtered by each current rule:\n")

    # Re-scan trades for individual rule impact
    rule_impact = defaultdict(lambda: {"filtered": 0, "pnl_saved": 0.0})
    for t in trades:
        entry_price = t["entry_price"]
        hour = int(t["created_at"][11:13])
        conf = t["model_confidence"]
        actual_pnl_val = t["pnl"] or 0.0
        tag = t["trade_tag"]

        if tag == "maker_fill":
            continue

        if entry_price is not None and entry_price < ENTRY_PRICE_MIN_LIVE:
            rule_impact["entry_price < 0.40 (paper-only)"]["filtered"] += 1
            rule_impact["entry_price < 0.40 (paper-only)"]["pnl_saved"] -= actual_pnl_val

        if hour in BLACKLIST_HOURS:
            rule_impact["blacklist hours"]["filtered"] += 1
            rule_impact["blacklist hours"]["pnl_saved"] -= actual_pnl_val

        if conf is not None:
            dampened = conf * CONFIDENCE_DAMPEN
            if dampened < MIN_CONFIDENCE:
                rule_impact["low confidence (dampened)"]["filtered"] += 1
                rule_impact["low confidence (dampened)"]["pnl_saved"] -= actual_pnl_val

    for rule, data in sorted(rule_impact.items(), key=lambda x: -x[1]["filtered"]):
        print(f"  {rule:<40} {data['filtered']:>4} trades filtered, P&L avoided: {data['pnl_saved']:>+.2f}$")

    print(f"\n{'='*80}")
    print(f"  NOTE: Early trades (Feb 28 - Mar 2) lack regime/confidence data.")
    print(f"  Regime flip & confidence filter only apply where data exists.")
    print(f"  Early exit re-simulation only for trades with max_bid data ({sum(1 for t in trades if t['max_bid_during_window'] is not None)} trades).")
    print(f"{'='*80}")


def _add_minutes(window_key, minutes):
    """Add minutes to a 'YYYY-MM-DD HH:MM' string."""
    dt = datetime.strptime(window_key, "%Y-%m-%d %H:%M")
    from datetime import timedelta
    dt += timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    main()
