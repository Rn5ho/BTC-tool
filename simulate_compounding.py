"""Simulate different bet sizing strategies on ML model predictions.

Compares: fixed, fixed % of bankroll, Kelly, half-Kelly, anti-martingale,
adaptive (streak-aware), and drawdown-adjusted sizing.

Usage:
    python simulate_compounding.py
"""

import json
import logging
import pickle
import sys
from pathlib import Path

import numpy as np
from sklearn.preprocessing import StandardScaler

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent / "models"

ENTRY_PRICE = 0.50
FEE_FACTOR = 0.25 * (0.5 * 0.5) ** 2  # ~0.0156


def load_predictions():
    """Load dataset and best model, return predictions on test set."""
    data = np.load(str(MODEL_DIR / "dataset.npz"), allow_pickle=True)
    X, y = data["X"], data["y"]

    with open(MODEL_DIR / "scaler.pkl", "rb") as f:
        scaler = pickle.load(f)

    # Load all models for comparison
    models = {}
    for name in ["RF_d3", "RF_d5", "HistGBT", "GBT_v1"]:
        path = MODEL_DIR / f"{name}.pkl"
        if path.exists():
            with open(path, "rb") as f:
                models[name] = pickle.load(f)

    # 80/20 time split (same as training)
    split = int(len(y) * 0.80)
    X_test, y_test = X[split:], y[split:]
    X_test_s = scaler.transform(X_test)

    return models, X_test_s, y_test


def simulate(y_test, probs, strategy_fn, initial_bankroll=100.0):
    """Run a simulation with a given bet sizing strategy.

    strategy_fn(bankroll, confidence, history) -> bet_size
    history is a dict with: wins, losses, streak, max_bankroll, trades

    Returns dict of stats.
    """
    bankroll = initial_bankroll
    max_bankroll = initial_bankroll
    min_bankroll = initial_bankroll
    max_drawdown = 0.0
    max_drawdown_pct = 0.0

    wins = 0
    losses = 0
    streak = 0  # positive = win streak, negative = loss streak
    total_wagered = 0.0
    total_pnl = 0.0

    bankroll_history = [bankroll]
    trade_log = []

    history = {
        "wins": 0, "losses": 0, "streak": 0,
        "max_bankroll": initial_bankroll, "trades": 0,
        "recent_wr": 0.5,  # rolling win rate over last 50
        "recent_results": [],
    }

    for i in range(len(y_test)):
        if bankroll <= 0.01:  # busted
            bankroll_history.append(bankroll)
            break

        p_up = probs[i]
        confidence = abs(p_up - 0.5)

        bet = strategy_fn(bankroll, confidence, history)
        bet = max(0.01, min(bet, bankroll))  # can't bet more than bankroll
        total_wagered += bet

        side_up = p_up > 0.5
        went_up = y_test[i] == 1
        won = (side_up and went_up) or (not side_up and not went_up)

        shares = (bet / ENTRY_PRICE) * (1 - FEE_FACTOR)
        pnl = (shares - bet) if won else -bet

        bankroll += pnl
        total_pnl += pnl

        if won:
            wins += 1
            streak = max(0, streak) + 1
        else:
            losses += 1
            streak = min(0, streak) - 1

        max_bankroll = max(max_bankroll, bankroll)
        min_bankroll = min(min_bankroll, bankroll)
        drawdown = max_bankroll - bankroll
        drawdown_pct = drawdown / max_bankroll if max_bankroll > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

        # Update history
        history["wins"] = wins
        history["losses"] = losses
        history["streak"] = streak
        history["max_bankroll"] = max_bankroll
        history["trades"] = wins + losses
        history["recent_results"].append(1 if won else 0)
        if len(history["recent_results"]) > 50:
            history["recent_results"].pop(0)
        history["recent_wr"] = np.mean(history["recent_results"]) if history["recent_results"] else 0.5

        bankroll_history.append(bankroll)

    n_trades = wins + losses
    wr = wins / n_trades if n_trades > 0 else 0
    days = n_trades / 288  # 288 windows per day

    return {
        "trades": n_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "final_bankroll": bankroll,
        "total_pnl": total_pnl,
        "total_wagered": total_wagered,
        "roi": total_pnl / total_wagered if total_wagered > 0 else 0,
        "daily_pnl": total_pnl / days if days > 0 else 0,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "peak_bankroll": max_bankroll,
        "min_bankroll": min_bankroll,
        "busted": bankroll <= 0.01,
        "bankroll_history": bankroll_history,
        "days": days,
    }


# ---------------------------------------------------------------------------
# Bet sizing strategies
# ---------------------------------------------------------------------------

def fixed_5(bankroll, confidence, history):
    """Always bet $5."""
    return 5.0


def fixed_pct_2(bankroll, confidence, history):
    """Bet 2% of bankroll."""
    return bankroll * 0.02


def fixed_pct_3(bankroll, confidence, history):
    """Bet 3% of bankroll."""
    return bankroll * 0.03


def fixed_pct_5(bankroll, confidence, history):
    """Bet 5% of bankroll."""
    return bankroll * 0.05


def kelly(bankroll, confidence, history):
    """Full Kelly criterion.
    f* = (p*b - q) / b where p = win prob, b = payout odds, q = 1-p
    """
    p = 0.5 + confidence  # our estimated win probability
    q = 1 - p
    # Payout: bet at 0.50, win pays (1/0.50 - 1) * (1-fee) = 0.9844
    b = (1.0 / ENTRY_PRICE - 1.0) * (1 - FEE_FACTOR)
    f = (p * b - q) / b
    f = max(0.0, f)
    return bankroll * f


def half_kelly(bankroll, confidence, history):
    """Half Kelly — standard risk-adjusted approach."""
    p = 0.5 + confidence
    q = 1 - p
    b = (1.0 / ENTRY_PRICE - 1.0) * (1 - FEE_FACTOR)
    f = (p * b - q) / b
    f = max(0.0, f) * 0.5
    return bankroll * f


def quarter_kelly(bankroll, confidence, history):
    """Quarter Kelly — conservative."""
    p = 0.5 + confidence
    q = 1 - p
    b = (1.0 / ENTRY_PRICE - 1.0) * (1 - FEE_FACTOR)
    f = (p * b - q) / b
    f = max(0.0, f) * 0.25
    return bankroll * f


def confidence_scaled(bankroll, confidence, history):
    """Scale bet by confidence: 1-5% of bankroll."""
    # Low confidence (0.00) = 1%, high confidence (0.20+) = 5%
    pct = 0.01 + confidence * 0.20  # 1% to ~5%
    pct = min(0.05, pct)
    return bankroll * pct


def streak_adaptive(bankroll, confidence, history):
    """Reduce bet during losing streaks, increase during winning streaks.
    Base: 3% of bankroll. Adjusted by streak.
    """
    base_pct = 0.03
    streak = history["streak"]

    if streak <= -6:
        # Deep losing streak: cut to 1%
        mult = 0.33
    elif streak <= -3:
        # Moderate losing streak: reduce to 2%
        mult = 0.66
    elif streak >= 6:
        # Hot streak: increase to 4%
        mult = 1.33
    elif streak >= 3:
        # Winning streak: slight increase
        mult = 1.15
    else:
        mult = 1.0

    return bankroll * base_pct * mult


def drawdown_adaptive(bankroll, confidence, history):
    """Reduce bets during drawdowns, increase when near peak.
    Base: 3% of bankroll. Scale down when in drawdown.
    """
    peak = history["max_bankroll"]
    if peak <= 0:
        return bankroll * 0.03

    drawdown_pct = (peak - bankroll) / peak

    if drawdown_pct > 0.30:
        # Deep drawdown: cut to 1%
        mult = 0.33
    elif drawdown_pct > 0.15:
        # Moderate drawdown: reduce to 2%
        mult = 0.66
    elif drawdown_pct < 0.02:
        # Near peak: normal or slight boost
        mult = 1.1
    else:
        mult = 1.0

    return bankroll * 0.03 * mult


def momentum_wr(bankroll, confidence, history):
    """Adjust bet based on rolling win rate (last 50 trades).
    If recent WR > 55%: bet more. If < 50%: bet less.
    """
    wr = history["recent_wr"]
    base_pct = 0.03

    if history["trades"] < 20:
        # Not enough data yet, use base
        return bankroll * base_pct

    if wr > 0.56:
        mult = 1.5
    elif wr > 0.53:
        mult = 1.2
    elif wr < 0.48:
        mult = 0.5
    elif wr < 0.50:
        mult = 0.7
    else:
        mult = 1.0

    return bankroll * base_pct * mult


def hybrid_adaptive(bankroll, confidence, history):
    """Combines confidence, streak, drawdown, and rolling WR.
    The kitchen sink approach.
    """
    # Base: 2% of bankroll
    base_pct = 0.02

    # 1) Confidence multiplier: 0.5x to 2x
    conf_mult = 0.5 + confidence * 7.5  # 0.5 at 0, ~2.0 at 0.20
    conf_mult = min(2.0, conf_mult)

    # 2) Streak multiplier
    streak = history["streak"]
    if streak <= -5:
        streak_mult = 0.5
    elif streak <= -3:
        streak_mult = 0.7
    elif streak >= 5:
        streak_mult = 1.3
    else:
        streak_mult = 1.0

    # 3) Drawdown multiplier
    peak = history["max_bankroll"]
    dd_pct = (peak - bankroll) / peak if peak > 0 else 0
    if dd_pct > 0.25:
        dd_mult = 0.5
    elif dd_pct > 0.15:
        dd_mult = 0.7
    else:
        dd_mult = 1.0

    # 4) Rolling WR multiplier
    wr = history["recent_wr"]
    if history["trades"] < 20:
        wr_mult = 1.0
    elif wr > 0.55:
        wr_mult = 1.3
    elif wr < 0.48:
        wr_mult = 0.6
    else:
        wr_mult = 1.0

    final_pct = base_pct * conf_mult * streak_mult * dd_mult * wr_mult
    final_pct = max(0.005, min(0.08, final_pct))  # clamp 0.5% to 8%

    return bankroll * final_pct


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 70)
    log.info("BTC 5-Min — Compounding & Bet Sizing Strategy Comparison")
    log.info("=" * 70)

    models, X_test_s, y_test = load_predictions()
    n_test = len(y_test)
    days = n_test / 288
    log.info(f"Test set: {n_test} windows ({days:.1f} days)")

    # Use best model (RF_d3 or RF_d5)
    model_name = "RF_d3" if "RF_d3" in models else list(models.keys())[0]
    model = models[model_name]
    probs = model.predict_proba(X_test_s)[:, 1]
    preds = model.predict(X_test_s)

    base_wr = sum(1 for i in range(n_test)
                  if (preds[i] == 1 and y_test[i] == 1) or (preds[i] == 0 and y_test[i] == 0)) / n_test
    log.info(f"Model: {model_name} ({base_wr*100:.1f}% accuracy on test)")

    strategies = {
        "Fixed $5": fixed_5,
        "Fixed 2%": fixed_pct_2,
        "Fixed 3%": fixed_pct_3,
        "Fixed 5%": fixed_pct_5,
        "Full Kelly": kelly,
        "Half Kelly": half_kelly,
        "Quarter Kelly": quarter_kelly,
        "Confidence-scaled": confidence_scaled,
        "Streak-adaptive": streak_adaptive,
        "Drawdown-adaptive": drawdown_adaptive,
        "Momentum WR": momentum_wr,
        "Hybrid adaptive": hybrid_adaptive,
    }

    # Test with different starting bankrolls
    for start_bank in [100.0, 20.0]:
        log.info(f"\n{'='*70}")
        log.info(f"STARTING BANKROLL: ${start_bank}")
        log.info(f"{'='*70}")
        log.info(f"\n  {'Strategy':<22s} {'Final$':>9s} {'PnL':>10s} {'$/day':>8s} {'ROI':>7s} "
                 f"{'MaxDD%':>7s} {'MinBR$':>8s} {'PeakBR$':>9s} {'Bust':>5s}")
        log.info(f"  {'-'*22} {'-'*9} {'-'*10} {'-'*8} {'-'*7} {'-'*7} {'-'*8} {'-'*9} {'-'*5}")

        results = {}
        for name, fn in strategies.items():
            r = simulate(y_test, probs, fn, initial_bankroll=start_bank)
            results[name] = r

            log.info(f"  {name:<22s} ${r['final_bankroll']:>8.2f} ${r['total_pnl']:>+9.2f} "
                     f"${r['daily_pnl']:>+7.1f} {r['roi']*100:>+6.1f}% "
                     f"{r['max_drawdown_pct']*100:>6.1f}% ${r['min_bankroll']:>7.2f} "
                     f"${r['peak_bankroll']:>8.2f} {'YES' if r['busted'] else 'no':>5s}")

        # Risk-adjusted returns (Sharpe-like: PnL / max_drawdown)
        log.info(f"\n  Risk-adjusted ranking (PnL / MaxDrawdown):")
        ranked = []
        for name, r in results.items():
            if r["max_drawdown"] > 0 and not r["busted"]:
                ratio = r["total_pnl"] / r["max_drawdown"]
                ranked.append((name, ratio, r))
        ranked.sort(key=lambda x: x[1], reverse=True)
        for i, (name, ratio, r) in enumerate(ranked):
            marker = " <<<" if i < 3 else ""
            log.info(f"  {i+1:2d}. {name:<22s} ratio={ratio:>6.2f} "
                     f"(PnL=${r['total_pnl']:>+.0f}, DD=${r['max_drawdown']:>.0f}){marker}")

    # ---- Detailed analysis of top strategies ----
    log.info(f"\n{'='*70}")
    log.info("DETAILED ANALYSIS — Top Strategies ($100 start)")
    log.info(f"{'='*70}")

    top_strategies = ["Half Kelly", "Quarter Kelly", "Confidence-scaled",
                      "Hybrid adaptive", "Drawdown-adaptive", "Fixed 3%"]

    for name in top_strategies:
        fn = strategies[name]
        r = simulate(y_test, probs, fn, initial_bankroll=100.0)

        log.info(f"\n  --- {name} ---")
        log.info(f"  Final: ${r['final_bankroll']:.2f} | PnL: ${r['total_pnl']:+.2f} | "
                 f"Daily: ${r['daily_pnl']:+.1f}")
        log.info(f"  Peak: ${r['peak_bankroll']:.2f} | Min: ${r['min_bankroll']:.2f} | "
                 f"MaxDD: {r['max_drawdown_pct']*100:.1f}%")
        log.info(f"  Wagered: ${r['total_wagered']:.0f} | ROI: {r['roi']*100:+.2f}%")

        # Bankroll at key points
        bh = r["bankroll_history"]
        n = len(bh)
        checkpoints = [0.25, 0.50, 0.75, 1.0]
        cp_str = " | ".join(f"{int(p*100)}%: ${bh[min(int(p*(n-1)), n-1)]:.0f}" for p in checkpoints)
        log.info(f"  Timeline: {cp_str}")

    # ---- Monte Carlo: what happens with different luck? ----
    log.info(f"\n{'='*70}")
    log.info("MONTE CARLO — 1000 simulations (shuffled outcomes, same WR)")
    log.info(f"{'='*70}")

    mc_strategies = ["Fixed $5", "Half Kelly", "Quarter Kelly",
                     "Confidence-scaled", "Hybrid adaptive"]

    for strat_name in mc_strategies:
        fn = strategies[strat_name]
        mc_finals = []
        mc_busts = 0
        mc_max_dds = []

        rng = np.random.default_rng(42)

        for trial in range(1000):
            # Shuffle the outcome order (preserves overall WR but changes sequence)
            shuffled_idx = rng.permutation(len(y_test))
            y_shuffled = y_test[shuffled_idx]
            probs_shuffled = probs[shuffled_idx]

            r = simulate(y_shuffled, probs_shuffled, fn, initial_bankroll=100.0)
            mc_finals.append(r["final_bankroll"])
            mc_max_dds.append(r["max_drawdown_pct"])
            if r["busted"]:
                mc_busts += 1

        mc_finals = np.array(mc_finals)
        mc_max_dds = np.array(mc_max_dds)

        log.info(f"\n  {strat_name}:")
        log.info(f"    Median final:  ${np.median(mc_finals):.2f}")
        log.info(f"    Mean final:    ${np.mean(mc_finals):.2f}")
        log.info(f"    5th pct:       ${np.percentile(mc_finals, 5):.2f}")
        log.info(f"    25th pct:      ${np.percentile(mc_finals, 25):.2f}")
        log.info(f"    75th pct:      ${np.percentile(mc_finals, 75):.2f}")
        log.info(f"    95th pct:      ${np.percentile(mc_finals, 95):.2f}")
        log.info(f"    Bust rate:     {mc_busts/10:.1f}%")
        log.info(f"    Median MaxDD:  {np.median(mc_max_dds)*100:.1f}%")
        log.info(f"    95th pct DD:   {np.percentile(mc_max_dds, 95)*100:.1f}%")
        profitable = np.sum(mc_finals > 100) / len(mc_finals) * 100
        log.info(f"    Profitable:    {profitable:.1f}% of simulations")

    log.info(f"\n{'='*70}")
    log.info("DONE")
    log.info(f"{'='*70}")


if __name__ == "__main__":
    main()
