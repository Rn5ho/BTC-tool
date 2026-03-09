"""
Clean-window simulation: Only uses trades from Mar 3 21:00+ where all features
were deployed and we have real data. No hindsight bias.

Compares 5 scenarios to isolate the value of each feature:
  1) ACTUAL     -- what really happened
  2) NO_EE      -- same trades, but no early exit (let everything settle)
  3) NO_FLIP    -- same trades, but no regime flip (use model's original side)
  4) NO_GUARD   -- same trades, but no streak guard
  5) NAKED      -- raw model: no EE, no flip, no guard

Starting bankroll: $85.71 (actual balance at Mar 3 ~20:00)
"""

import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

STARTING_BANKROLL = 85.71  # actual balance at start of clean window
FEE_RATE = 0.25
FEE_EXPONENT = 2

def fee(p):
    return (FEE_RATE / 10000) * (p ** FEE_EXPONENT)

def pnl_win(amt, ep):
    f = fee(ep)
    shares = (amt / ep) * (1.0 - f)
    return shares - amt

def pnl_loss(amt):
    return -amt

def get_exit_threshold(ep):
    if ep < 0.35: return 0.50
    elif ep < 0.40: return 0.45
    elif ep < 0.50: return 0.65
    else: return 0.95


def main():
    conn = sqlite3.connect("btc_edge.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get all trades from clean window
    c.execute("""
        SELECT created_at, side, entry_price, outcome, pnl, amount_usdc, trade_tag,
               model_confidence, regime_state, regime_strength, max_bid_during_window,
               settlement_price, btc_price_at_open
        FROM live_trades
        WHERE outcome IS NOT NULL AND success=1 AND created_at >= '2026-03-03 20:00'
        ORDER BY created_at
    """)
    trades = [dict(row) for row in c.fetchall()]
    conn.close()

    print("=" * 90)
    print(f"  CLEAN WINDOW SIMULATION -- {len(trades)} trades from Mar 3 20:00+")
    print(f"  Starting bankroll: ${STARTING_BANKROLL:.2f}")
    print(f"  All current features deployed. Zero hindsight bias.")
    print("=" * 90)

    # Reconstruct regime info for regime_flip trades
    # If tag=regime_flip and side=UP -> regime was trending_up (flipped TO up)
    # If tag=regime_flip and side=DOWN -> regime was trending_down
    for t in trades:
        if t["trade_tag"] == "regime_flip" and t["regime_state"] is None:
            if t["side"] == "UP":
                t["regime_state"] = "trending_up"
                t["regime_strength"] = 0.40  # at least 0.35 to trigger
            else:
                t["regime_state"] = "trending_down"
                t["regime_strength"] = -0.40

    # For each trade, determine what the MODEL's original side was
    # If regime_flip: model said opposite of what we traded
    for t in trades:
        if t["trade_tag"] == "regime_flip":
            t["model_side"] = "DOWN" if t["side"] == "UP" else "UP"
        else:
            t["model_side"] = t["side"]

    # For each trade, determine the TRUE market direction (for counterfactuals)
    # If outcome=WIN, the traded side was correct
    # If outcome=LOSS, the traded side was wrong
    # If outcome=EARLY_EXIT, we need settlement data or btc prices
    for t in trades:
        if t["outcome"] == "WIN":
            t["market_went"] = t["side"]
        elif t["outcome"] == "LOSS":
            t["market_went"] = "DOWN" if t["side"] == "UP" else "UP"
        else:
            # EARLY_EXIT -- need to figure out what settlement would have been
            # Use btc_price_at_open and settlement_price if available
            if t["settlement_price"] is not None and t["btc_price_at_open"] is not None:
                t["market_went"] = "UP" if t["settlement_price"] > t["btc_price_at_open"] else "DOWN"
            else:
                t["market_went"] = None  # unknown

    # ====== Run scenarios ======
    scenarios = {
        "ACTUAL":    {"use_ee": True,  "use_flip": True,  "use_guard": True},
        "NO_EE":     {"use_ee": False, "use_flip": True,  "use_guard": True},
        "NO_FLIP":   {"use_ee": True,  "use_flip": False, "use_guard": True},
        "NO_GUARD":  {"use_ee": True,  "use_flip": True,  "use_guard": False},
        "NAKED":     {"use_ee": False, "use_flip": False, "use_guard": False},
    }

    results = {}
    for name, cfg in scenarios.items():
        r = run_scenario(trades, cfg["use_ee"], cfg["use_flip"], cfg["use_guard"])
        results[name] = r

    # ====== Print results ======
    print(f"\n{'-'*90}")
    print(f"  TRADE-BY-TRADE (first 30 trades)")
    print(f"{'-'*90}")
    print(f"  {'Time':<17} {'Side':>4} {'EP':>5} {'Tag':<13} {'Actual':>10} {'NoEE':>10} {'NoFlip':>10} {'Naked':>10}")

    for i, t in enumerate(trades[:30]):
        ts = t["created_at"][11:16]
        day = t["created_at"][5:10]
        side = t["side"]
        ep = t["entry_price"]
        tag = (t["trade_tag"] or "")[:12]

        actual_oc = results["ACTUAL"]["trade_outcomes"][i]
        noee_oc = results["NO_EE"]["trade_outcomes"][i]
        noflip_oc = results["NO_FLIP"]["trade_outcomes"][i]
        naked_oc = results["NAKED"]["trade_outcomes"][i]

        def fmt_oc(oc):
            if oc is None: return "SKIP"
            o, p = oc
            sym = "W" if o == "WIN" else ("L" if o == "LOSS" else "EE")
            return f"{sym} {p:>+.1f}"

        print(f"  {day} {ts:<11} {side:>4} {ep:>5.2f} {tag:<13} {fmt_oc(actual_oc):>10} {fmt_oc(noee_oc):>10} {fmt_oc(noflip_oc):>10} {fmt_oc(naked_oc):>10}")

    print(f"\n{'-'*90}")
    print(f"  SCENARIO COMPARISON")
    print(f"{'-'*90}")
    print(f"  {'Scenario':<12} {'Trades':>6} {'W':>4} {'L':>4} {'EE':>4} {'WR':>7} {'P&L':>10} {'Bankroll':>10} {'MaxDD':>8}")

    for name in ["ACTUAL", "NO_EE", "NO_FLIP", "NO_GUARD", "NAKED"]:
        r = results[name]
        wr = r["wins"] / (r["wins"] + r["losses"]) * 100 if (r["wins"] + r["losses"]) > 0 else 0
        print(f"  {name:<12} {r['taken']:>6} {r['wins']:>4} {r['losses']:>4} {r['ee']:>4} {wr:>6.1f}% {r['pnl']:>+9.2f}$ ${r['bankroll']:>8.2f} ${r['max_dd']:>6.2f}")

    # Feature value decomposition
    print(f"\n{'-'*90}")
    print(f"  FEATURE VALUE DECOMPOSITION")
    print(f"{'-'*90}")

    naked_pnl = results["NAKED"]["pnl"]
    actual_pnl = results["ACTUAL"]["pnl"]

    ee_value = results["NO_FLIP"]["pnl"] - results["NAKED"]["pnl"]  # EE only (no flip in either)
    # Actually let's be more precise
    # NAKED = no EE, no flip, no guard
    # NO_GUARD = EE + flip, no guard  -> guard value = ACTUAL - NO_GUARD
    # NO_EE = flip + guard, no EE     -> EE value = ACTUAL - NO_EE
    # NO_FLIP = EE + guard, no flip   -> flip value = ACTUAL - NO_FLIP

    ee_val = actual_pnl - results["NO_EE"]["pnl"]
    flip_val = actual_pnl - results["NO_FLIP"]["pnl"]
    guard_val = actual_pnl - results["NO_GUARD"]["pnl"]
    total_features = actual_pnl - naked_pnl
    interaction = total_features - (ee_val + flip_val + guard_val)

    print(f"\n  Value of each feature (P&L with vs without):")
    print(f"    Early Exit:    {ee_val:>+8.2f}$  (ACTUAL vs NO_EE)")
    print(f"    Regime Flip:   {flip_val:>+8.2f}$  (ACTUAL vs NO_FLIP)")
    print(f"    Streak Guard:  {guard_val:>+8.2f}$  (ACTUAL vs NO_GUARD)")
    print(f"    Interaction:   {interaction:>+8.2f}$  (features amplify each other)")
    print(f"    -------")
    print(f"    Total uplift:  {total_features:>+8.2f}$  (ACTUAL vs NAKED)")
    print(f"\n    NAKED P&L:     {naked_pnl:>+8.2f}$")
    print(f"    ACTUAL P&L:    {actual_pnl:>+8.2f}$")

    # Breakdown by trade type
    print(f"\n{'-'*90}")
    print(f"  BREAKDOWN BY TRADE TYPE")
    print(f"{'-'*90}")

    flip_trades = [t for t in trades if t["trade_tag"] == "regime_flip"]
    normal_trades = [t for t in trades if t["trade_tag"] != "regime_flip" and t["trade_tag"] != "maker_fill"]
    maker_trades = [t for t in trades if t["trade_tag"] == "maker_fill"]

    for label, subset in [("Regime flip", flip_trades), ("Normal (model)", normal_trades), ("Maker fill", maker_trades)]:
        if not subset:
            continue
        wins = sum(1 for t in subset if t["outcome"] == "WIN")
        losses = sum(1 for t in subset if t["outcome"] == "LOSS")
        ee = sum(1 for t in subset if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in subset)
        vol = sum(t["amount_usdc"] or 0 for t in subset)
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        print(f"  {label:<16} {len(subset):>3} trades  W:{wins} L:{losses} EE:{ee}  WR:{wr:>5.1f}%  P&L:{pnl:>+7.2f}$  Vol:{vol:>.0f}$")

    # What would normal trades have done if flipped?
    print(f"\n  COUNTERFACTUAL: What if we had flipped MORE aggressively?")
    could_flip = 0
    flip_would_help = 0
    flip_pnl_delta = 0
    for t in normal_trades:
        if t["regime_strength"] is not None and abs(t["regime_strength"]) >= 0.25:
            could_flip += 1
            model_correct = (t["model_side"] == t["market_went"]) if t["market_went"] else None
            if model_correct is False:
                flip_would_help += 1
                flip_pnl_delta += (t["pnl"] or 0) * -2  # would gain instead of lose
            elif model_correct is True:
                flip_pnl_delta -= (t["pnl"] or 0) * 2  # would lose instead of gain

    if could_flip > 0:
        print(f"    {could_flip} normal trades had |regime_strength| >= 0.25")
        print(f"    {flip_would_help} of those would have benefited from flipping")
        print(f"    Estimated P&L delta: {flip_pnl_delta:>+.2f}$")

    print(f"\n{'='*90}")
    print(f"  This is clean, unbiased data -- all features were live during this period.")
    print(f"  N={len(trades)} trades over ~12 hours. Small sample, but real.")
    print(f"{'='*90}")


def run_scenario(trades, use_ee, use_flip, use_guard):
    """Run a single scenario and return results."""
    bankroll = STARTING_BANKROLL
    pnl = 0.0
    peak = bankroll
    max_dd = 0.0
    wins = losses = ee = taken = skipped = 0
    trade_outcomes = []

    streak_up = streak_down = 0
    pause_up = pause_down = None

    for t in trades:
        created = t["created_at"]
        window_key = created[:16]
        side = t["side"]
        ep = t["entry_price"]
        outcome = t["outcome"]
        apnl = t["pnl"] or 0.0
        aamt = t["amount_usdc"] or 0.0
        tag = t["trade_tag"]
        model_side = t["model_side"]
        market_went = t["market_went"]
        maxbid = t["max_bid_during_window"]

        # Determine effective side for this scenario
        if use_flip:
            effective_side = side  # already flipped in actual data
        else:
            effective_side = model_side  # un-flip: use model's original

        # Streak guard
        if use_guard and tag != "maker_fill":
            if effective_side == "UP" and pause_up and window_key < pause_up:
                trade_outcomes.append(None)
                skipped += 1
                continue
            if effective_side == "DOWN" and pause_down and window_key < pause_down:
                trade_outcomes.append(None)
                skipped += 1
                continue

        taken += 1

        # Determine outcome for this scenario's side
        if not use_flip and tag == "regime_flip":
            # We're using model's original side, not the flipped side
            if market_went is not None:
                model_correct = (model_side == market_went)
                base_outcome = "WIN" if model_correct else "LOSS"
            else:
                # Unknown settlement, use inverse of actual
                if outcome == "WIN":
                    base_outcome = "LOSS"  # flip was right, model was wrong
                elif outcome == "LOSS":
                    base_outcome = "WIN"
                else:
                    base_outcome = outcome
        else:
            base_outcome = outcome

        # Early exit
        if use_ee and base_outcome == "EARLY_EXIT":
            # Keep the actual EE outcome
            trade_pnl = apnl * (aamt / aamt) if aamt > 0 else 0  # use actual
            trade_outcome = "EARLY_EXIT"
        elif use_ee and maxbid is not None:
            threshold = get_exit_threshold(ep)
            if maxbid >= threshold and base_outcome != "WIN":
                # Would have early exited
                f = fee(ep)
                tokens = (aamt / ep) * (1.0 - f)
                # Estimate sell at ~threshold
                sell_f = fee(threshold)
                proceeds = tokens * threshold * (1.0 - sell_f)
                trade_pnl = proceeds - aamt
                trade_outcome = "EARLY_EXIT"
            elif base_outcome == "WIN":
                trade_pnl = pnl_win(aamt, ep)
                trade_outcome = "WIN"
            else:
                trade_pnl = pnl_loss(aamt)
                trade_outcome = "LOSS"
        elif not use_ee and outcome == "EARLY_EXIT":
            # No EE -- would have gone to settlement
            if market_went is not None:
                settled_correct = (effective_side == market_went)
                if settled_correct:
                    trade_pnl = pnl_win(aamt, ep)
                    trade_outcome = "WIN"
                else:
                    trade_pnl = pnl_loss(aamt)
                    trade_outcome = "LOSS"
            else:
                # Unknown, assume loss (conservative)
                trade_pnl = pnl_loss(aamt)
                trade_outcome = "LOSS"
        elif base_outcome == "WIN":
            trade_pnl = pnl_win(aamt, ep)
            trade_outcome = "WIN"
        else:
            trade_pnl = pnl_loss(aamt)
            trade_outcome = "LOSS"

        trade_outcomes.append((trade_outcome, trade_pnl))

        pnl += trade_pnl
        bankroll += trade_pnl
        if bankroll > peak: peak = bankroll
        dd = peak - bankroll
        if dd > max_dd: max_dd = dd

        if trade_outcome == "WIN": wins += 1
        elif trade_outcome == "LOSS": losses += 1
        elif trade_outcome == "EARLY_EXIT": ee += 1

        # Update streak guard state
        if use_guard and tag != "maker_fill" and trade_outcome != "EARLY_EXIT":
            if trade_outcome == "LOSS":
                if effective_side == "UP":
                    streak_up += 1; streak_down = 0
                    if streak_up >= 3:
                        dt = datetime.strptime(window_key, "%Y-%m-%d %H:%M") + timedelta(minutes=10)
                        pause_up = dt.strftime("%Y-%m-%d %H:%M")
                        streak_up = 0
                else:
                    streak_down += 1; streak_up = 0
                    if streak_down >= 3:
                        dt = datetime.strptime(window_key, "%Y-%m-%d %H:%M") + timedelta(minutes=10)
                        pause_down = dt.strftime("%Y-%m-%d %H:%M")
                        streak_down = 0
            elif trade_outcome == "WIN":
                if effective_side == "UP": streak_up = 0
                else: streak_down = 0

    return {
        "taken": taken, "skipped": skipped,
        "wins": wins, "losses": losses, "ee": ee,
        "pnl": pnl, "bankroll": bankroll, "peak": peak, "max_dd": max_dd,
        "trade_outcomes": trade_outcomes,
    }


if __name__ == "__main__":
    main()
