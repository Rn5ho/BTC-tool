"""Analyze paper trading data to find which signals are predictive.

Run from the same directory as btc_edge.db:
    python analyze_trades.py

Outputs:
  1. Overall stats
  2. Win rate by signal direction (which signals predict correctly?)
  3. Signal strength vs outcome (do strong signals win more?)
  4. Edge bucket analysis (are high-edge trades better?)
  5. ML model — logistic regression on raw features to find if any
     combination is predictive. Prints coefficients and cross-validated
     accuracy.
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path("btc_edge.db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(n: float) -> str:
    return f"{n * 100:.1f}%"


def _bar(value: float, width: int = 30) -> str:
    """Simple ASCII bar for visualisation."""
    filled = int(abs(value) * width)
    if value >= 0:
        return "[" + "#" * filled + "." * (width - filled) + "]"
    return "[" + "." * (width - filled) + "#" * filled + "]"


def print_header(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------

def load_data(db_path: Path) -> tuple[list[dict], list[dict]]:
    """Load trades and matching feature snapshots from SQLite."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # All settled trades
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_trades WHERE outcome IS NOT NULL ORDER BY timestamp"
    ).fetchall()]

    # All feature snapshots
    features = [dict(r) for r in conn.execute(
        "SELECT * FROM feature_snapshots ORDER BY timestamp"
    ).fetchall()]

    conn.close()
    return trades, features


def match_features_to_trades(
    trades: list[dict], features: list[dict]
) -> list[dict]:
    """Join each trade with the closest-in-time feature snapshot for the same slug."""

    # Build index: slug -> list of feature rows sorted by timestamp
    by_slug: dict[str, list[dict]] = defaultdict(list)
    for f in features:
        slug = f.get("market_slug", "")
        if slug:
            by_slug[slug].append(f)

    merged = []
    for t in trades:
        slug = t["market_slug"]
        candidates = by_slug.get(slug, [])
        if not candidates:
            continue
        # Find the feature snapshot closest to trade timestamp
        best = min(candidates, key=lambda f: abs(f["timestamp"] - t["timestamp"]))
        row = {**t}
        for col in ("obi", "taker_ratio", "momentum_1m", "momentum_5m",
                     "rsi", "vwap_deviation", "bb_position", "ema_cross",
                     "funding_rate", "volume_zscore", "atr", "prob_up"):
            row[f"feat_{col}"] = best.get(col, 0.0) or 0.0
        merged.append(row)
    return merged


# ---------------------------------------------------------------------------
# 2. Overall stats
# ---------------------------------------------------------------------------

def print_overall_stats(trades: list[dict]) -> None:
    print_header("OVERALL STATS")
    total = len(trades)
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = total - wins
    total_pnl = sum(t["pnl"] or 0 for t in trades)
    avg_pnl = total_pnl / total if total else 0

    print(f"  Settled trades : {total}")
    print(f"  Wins / Losses  : {wins} / {losses}")
    print(f"  Win rate       : {_pct(wins / total) if total else 'N/A'}")
    print(f"  Total P&L      : ${total_pnl:+.2f}")
    print(f"  Avg P&L/trade  : ${avg_pnl:+.4f}")
    print(f"  Break-even WR  : ~50.8% (at p=0.50 with 1.56% fee)")


# ---------------------------------------------------------------------------
# 3. Signal direction analysis
# ---------------------------------------------------------------------------

SIGNAL_COLS = [
    ("feat_obi",            "OBI",         "high=bullish"),
    ("feat_taker_ratio",    "Taker Ratio", "high=bullish"),
    ("feat_momentum_1m",    "Momentum 1m", "high=bullish"),
    ("feat_momentum_5m",    "Momentum 5m", "high=bullish"),
    ("feat_rsi",            "RSI",         ">50=bullish"),
    ("feat_vwap_deviation", "VWAP Dev",    "high=bullish"),
    ("feat_funding_rate",   "Funding",     "high=bearish"),
    ("feat_volume_zscore",  "Vol Z-Score", "high=active"),
]


def print_signal_direction_analysis(data: list[dict]) -> None:
    """For each signal, split trades by signal direction and compare win rates."""
    print_header("WIN RATE BY SIGNAL DIRECTION")
    print(f"  {'Signal':<14s} {'Bullish WR':>12s} {'Bearish WR':>12s} {'Delta':>8s}  Note")
    print("  " + "-" * 56)

    for col, name, note in SIGNAL_COLS:
        bullish_wins, bullish_total = 0, 0
        bearish_wins, bearish_total = 0, 0

        for row in data:
            val = row.get(col, 0.0)
            won = row["outcome"] == "WIN"
            side = row["side"]

            # Determine if the signal was bullish or bearish.
            # For RSI, bullish = > 50.  For funding, inverted.  For others, > 0.
            if col == "feat_rsi":
                is_bullish = val > 50
            elif col == "feat_funding_rate":
                is_bullish = val < 0  # negative funding = bullish
            else:
                is_bullish = val > 0

            if is_bullish:
                bullish_total += 1
                if won:
                    bullish_wins += 1
            else:
                bearish_total += 1
                if won:
                    bearish_wins += 1

        bwr = bullish_wins / bullish_total if bullish_total else 0
        brwr = bearish_wins / bearish_total if bearish_total else 0
        delta = bwr - brwr

        flag = " <-- useful" if abs(delta) > 0.05 else ""
        print(
            f"  {name:<14s} {_pct(bwr):>8s} ({bullish_total:>3d}) "
            f"{_pct(brwr):>8s} ({bearish_total:>3d}) {delta:>+7.1%}{flag}"
        )


# ---------------------------------------------------------------------------
# 4. Signal strength analysis
# ---------------------------------------------------------------------------

def print_signal_strength_analysis(data: list[dict]) -> None:
    """Split trades by signal strength (weak/medium/strong) and compare win rates."""
    print_header("SIGNAL STRENGTH VS WIN RATE")
    print("  (Absolute signal value: weak < 0.1, medium 0.1-0.3, strong > 0.3)")
    print()

    for col, name, _ in SIGNAL_COLS:
        if col in ("feat_rsi", "feat_volume_zscore"):
            continue  # different scale, skip for this analysis

        buckets = {"weak": [0, 0], "medium": [0, 0], "strong": [0, 0]}
        for row in data:
            val = abs(row.get(col, 0.0))
            won = 1 if row["outcome"] == "WIN" else 0
            if val < 0.1:
                b = "weak"
            elif val < 0.3:
                b = "medium"
            else:
                b = "strong"
            buckets[b][0] += 1
            buckets[b][1] += won

        parts = []
        for bname in ("weak", "medium", "strong"):
            total, wins = buckets[bname]
            wr = wins / total if total else 0
            parts.append(f"{bname}: {_pct(wr)} ({total})")
        print(f"  {name:<14s}  {' | '.join(parts)}")


# ---------------------------------------------------------------------------
# 5. Edge bucket analysis
# ---------------------------------------------------------------------------

def print_edge_analysis(trades: list[dict]) -> None:
    """Bucket trades by edge size and see if larger edges perform better."""
    print_header("EDGE SIZE VS ACTUAL WIN RATE")
    print("  (Does a bigger detected edge actually win more often?)")
    print()

    buckets = [
        ("5-8%",   0.05, 0.08),
        ("8-12%",  0.08, 0.12),
        ("12-16%", 0.12, 0.16),
        ("16-20%", 0.16, 0.20),
        ("20%+",   0.20, 1.00),
    ]

    for label, lo, hi in buckets:
        subset = [t for t in trades if lo <= t["edge"] < hi]
        if not subset:
            print(f"  Edge {label:>6s}: no trades")
            continue
        wins = sum(1 for t in subset if t["outcome"] == "WIN")
        wr = wins / len(subset)
        pnl = sum(t["pnl"] or 0 for t in subset)
        bar = _bar(wr - 0.5, 20)  # center at 50%
        print(
            f"  Edge {label:>6s}: {_pct(wr):>6s} win ({wins}/{len(subset)}) "
            f" P&L=${pnl:+.2f}  {bar}"
        )


# ---------------------------------------------------------------------------
# 6. Side analysis
# ---------------------------------------------------------------------------

def print_side_analysis(trades: list[dict]) -> None:
    """Compare win rate for UP vs DOWN bets."""
    print_header("WIN RATE BY SIDE")

    for side in ("UP", "DOWN"):
        subset = [t for t in trades if t["side"] == side]
        if not subset:
            print(f"  {side}: no trades")
            continue
        wins = sum(1 for t in subset if t["outcome"] == "WIN")
        wr = wins / len(subset)
        pnl = sum(t["pnl"] or 0 for t in subset)
        print(f"  {side:>5s}: {_pct(wr):>6s} win ({wins}/{len(subset)})  P&L=${pnl:+.2f}")


# ---------------------------------------------------------------------------
# 7. Signal agreement analysis
# ---------------------------------------------------------------------------

def print_signal_agreement(data: list[dict]) -> None:
    """Check if trades where more signals agree perform better."""
    print_header("SIGNAL AGREEMENT (how many signals point in the trade direction?)")

    agreement_buckets: dict[int, list[bool]] = defaultdict(list)

    for row in data:
        side = row["side"]
        agreeing = 0
        # Count how many signals agree with the trade side
        for col, _, _ in SIGNAL_COLS:
            val = row.get(col, 0.0)
            if col == "feat_rsi":
                signal_bullish = val > 50
            elif col == "feat_funding_rate":
                signal_bullish = val < 0
            elif col == "feat_volume_zscore":
                continue  # non-directional
            else:
                signal_bullish = val > 0

            if (side == "UP" and signal_bullish) or (side == "DOWN" and not signal_bullish):
                agreeing += 1

        agreement_buckets[agreeing].append(row["outcome"] == "WIN")

    print(f"  {'Signals agreeing':>18s}  {'Win Rate':>10s}  {'Trades':>8s}")
    print("  " + "-" * 40)
    for n in sorted(agreement_buckets.keys()):
        wins_list = agreement_buckets[n]
        wr = sum(wins_list) / len(wins_list)
        print(f"  {n:>18d}  {_pct(wr):>10s}  {len(wins_list):>8d}")


# ---------------------------------------------------------------------------
# 8. ML analysis (logistic regression)
# ---------------------------------------------------------------------------

def run_ml_analysis(data: list[dict]) -> None:
    """Train a logistic regression on feature snapshots to predict trade outcome."""
    print_header("ML ANALYSIS — Logistic Regression")

    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        print("  scikit-learn not installed. Run: pip install scikit-learn numpy")
        return

    feature_cols = [
        "feat_obi", "feat_taker_ratio", "feat_momentum_1m", "feat_momentum_5m",
        "feat_rsi", "feat_vwap_deviation", "feat_bb_position", "feat_ema_cross",
        "feat_funding_rate", "feat_volume_zscore", "feat_atr",
    ]

    X = []
    y = []
    for row in data:
        features = [row.get(c, 0.0) for c in feature_cols]
        if any(v is None for v in features):
            continue
        # Target: 1 if the trade won, 0 if lost
        X.append(features)
        y.append(1 if row["outcome"] == "WIN" else 0)

    X = np.array(X, dtype=float)
    y = np.array(y, dtype=int)

    if len(X) < 20:
        print(f"  Only {len(X)} samples — need at least 20 for meaningful analysis.")
        return

    print(f"  Samples: {len(X)} trades with matched features")
    print(f"  Features: {len(feature_cols)}")
    print(f"  Base win rate: {_pct(y.mean())} (this is what random guessing gets)")
    print()

    # Standardize features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Cross-validated accuracy
    model = LogisticRegression(max_iter=1000, random_state=42)
    n_folds = min(5, len(X) // 10) or 2
    scores = cross_val_score(model, X_scaled, y, cv=n_folds, scoring="accuracy")
    print(f"  {n_folds}-fold CV accuracy: {_pct(scores.mean())} (+/- {_pct(scores.std())})")
    print(f"  (If this is close to base win rate, features aren't predictive)")
    print()

    # Fit on all data to inspect coefficients
    model.fit(X_scaled, y)
    coefs = model.coef_[0]

    print("  Feature importance (logistic regression coefficients):")
    print(f"  {'Feature':<18s} {'Coeff':>8s}  {'Direction':<20s}  Strength")
    print("  " + "-" * 65)

    ranked = sorted(
        zip(feature_cols, coefs),
        key=lambda x: abs(x[1]),
        reverse=True,
    )

    for col, coef in ranked:
        name = col.replace("feat_", "")
        direction = "helps WIN" if coef > 0 else "helps LOSE"
        strength = _bar(coef / (max(abs(c) for c in coefs) + 1e-9), 15)
        print(f"  {name:<18s} {coef:>+8.4f}  {direction:<20s}  {strength}")

    # Predict on training data to see if there's any separation
    y_pred = model.predict(X_scaled)
    train_acc = (y_pred == y).mean()
    print()
    print(f"  Training accuracy: {_pct(train_acc)}")

    # Check if the model learns anything beyond the base rate
    improvement = train_acc - y.mean()
    if improvement > 0.05:
        print(f"  Model improves over base rate by {_pct(improvement)} -- signals have some info!")
        print()
        print("  Suggested action: Upweight the top positive coefficients,")
        print("  downweight or remove the negative ones.")
    elif improvement > 0.02:
        print(f"  Model improves over base rate by {_pct(improvement)} -- marginal signal.")
        print("  Might be noise. Need more data.")
    else:
        print(f"  Model does NOT improve over base rate ({_pct(improvement)}).")
        print("  The features are not predictive of trade outcomes.")
        print("  The Polymarket 5-min BTC market may be too efficient for these signals.")

    # Optimal weights suggestion
    print()
    print("  SUGGESTED WEIGHTS (based on positive coefficients only):")
    positive_coefs = [(col.replace("feat_", ""), c) for col, c in ranked if c > 0]
    if positive_coefs:
        total_pos = sum(c for _, c in positive_coefs)
        for name, c in positive_coefs:
            weight = c / total_pos
            print(f"    {name:<18s}: {weight:.3f}")
    else:
        print("    No features with positive coefficients — model finds nothing useful.")


# ---------------------------------------------------------------------------
# 9. Time-of-day analysis
# ---------------------------------------------------------------------------

def print_time_analysis(trades: list[dict]) -> None:
    """Check if certain hours perform better (BTC volatility varies by hour)."""
    print_header("WIN RATE BY HOUR (UTC)")

    from datetime import datetime, timezone

    hour_stats: dict[int, list[bool]] = defaultdict(list)
    for t in trades:
        ts = t["timestamp"] / 1000  # ms -> s
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour_stats[dt.hour].append(t["outcome"] == "WIN")

    if not hour_stats:
        print("  No data")
        return

    print(f"  {'Hour':>6s}  {'Win Rate':>10s}  {'Trades':>8s}  Bar")
    print("  " + "-" * 45)
    for h in sorted(hour_stats.keys()):
        wins = hour_stats[h]
        wr = sum(wins) / len(wins)
        bar = _bar(wr - 0.5, 15)
        marker = " <--" if wr > 0.55 else (" !!!" if wr < 0.40 else "")
        print(f"  {h:>4d}:00  {_pct(wr):>10s}  {len(wins):>8d}  {bar}{marker}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH.resolve()}")
        print("Run this script from the same directory as btc_edge.db")
        sys.exit(1)

    trades, features = load_data(DB_PATH)

    if not trades:
        print("No settled trades found in the database.")
        sys.exit(1)

    print(f"\nLoaded {len(trades)} settled trades, {len(features)} feature snapshots")

    # Join features to trades
    data = match_features_to_trades(trades, features)
    print(f"Matched features to {len(data)} trades")

    # Run all analyses
    print_overall_stats(trades)
    print_side_analysis(trades)
    print_edge_analysis(trades)

    if data:
        print_signal_direction_analysis(data)
        print_signal_strength_analysis(data)
        print_signal_agreement(data)
        print_time_analysis(trades)
        run_ml_analysis(data)
    else:
        print("\nCould not match features to trades — no feature_snapshots data?")
        print("The signal and ML analyses require feature data.")

    print()
    print("=" * 60)
    print("  DONE — review the results above to decide next steps.")
    print("=" * 60)


if __name__ == "__main__":
    main()
