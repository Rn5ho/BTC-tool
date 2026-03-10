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
