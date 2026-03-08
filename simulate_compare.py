"""Compare current system (EE + confirmed regime flip) vs naked model (no EE, no flip).

Uses real BTC settlement from candles with correct UTC handling.
Simulates the full trade history with real bankroll timeline ($73 start, +$43 on Mar 3).
"""
import sqlite3
from datetime import datetime, timedelta, timezone

DB = "btc_edge_live.db"

# Polymarket fee model
FEE_RATE = 0.25
FEE_EXP = 2

def fee(p):
    return (FEE_RATE / 10000) * (p ** FEE_EXP)

def pnl_win(amt, price):
    f = fee(price)
    return (amt / price) * (1 - f) - amt

def pnl_loss(amt):
    return -amt

def ee_pnl(amt, entry_price, exit_price):
    """P&L from early exit sell at exit_price."""
    f_entry = fee(entry_price)
    tokens = (amt / entry_price) * (1 - f_entry)
    f_exit = fee(exit_price)
    return tokens * exit_price * (1 - f_exit) - amt

def get_btc_direction(conn, created_at, btc_open_stored, settlement_stored):
    """Look up real BTC 5-min direction from candles (UTC-safe)."""
    dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    dt_utc = dt.replace(tzinfo=timezone.utc)
    wmin = (dt_utc.minute // 5) * 5
    ws = dt_utc.replace(minute=wmin, second=0, microsecond=0)
    we = ws + timedelta(minutes=5)
    sms = int(ws.timestamp() * 1000)
    ems = int(we.timestamp() * 1000)

    c2 = conn.cursor()
    c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
               (sms, sms + 60000))
    orow = c2.fetchone()
    c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
               (ems - 60000, ems + 60000))
    crow = c2.fetchone()

    bo = btc_open_stored if btc_open_stored else (orow[0] if orow else None)
    bc = settlement_stored if settlement_stored else (crow[0] if crow else None)

    if bo and bc:
        return "UP" if bc > bo else "DOWN", bo, bc
    return None, bo, bc


def get_exit_threshold(entry_price):
    """4-tier early exit thresholds."""
    if entry_price < 0.35:
        return 0.50
    elif entry_price < 0.40:
        return 0.45
    elif entry_price < 0.50:
        return 0.65
    else:
        return 0.95


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT created_at, side, entry_price, outcome, pnl, amount_usdc, trade_tag,
               regime_state, regime_strength, max_bid_during_window,
               settlement_price, btc_price_at_open, fill_price
        FROM live_trades
        WHERE outcome IS NOT NULL AND success=1
        ORDER BY created_at
    """)
    trades = [dict(row) for row in c.fetchall()]

    # Enrich with BTC direction
    for t in trades:
        direction, bo, bc = get_btc_direction(
            conn, t["created_at"], t["btc_price_at_open"], t["settlement_price"]
        )
        t["market_went"] = direction
        t["btc_open"] = bo
        t["btc_close"] = bc

        # For regime_flip trades, model_side is opposite of traded side
        if t["trade_tag"] == "regime_flip":
            t["model_side"] = "DOWN" if t["side"] == "UP" else "UP"
        else:
            t["model_side"] = t["side"]

    conn.close()

    # --- Simulation scenarios ---
    def simulate(trades, use_ee, use_flip, confirm_windows, label):
        """Simulate trading with optional EE and regime flip.

        use_flip: whether to allow regime flips
        confirm_windows: how many consecutive trending windows needed (0 = instant like old behavior)
        """
        bankroll = 73.0  # starting balance
        deposit_added = False
        pnl_total = 0.0
        wins = losses = ee_count = 0
        peak = bankroll
        max_dd = 0.0

        # Regime flip confirmation tracking
        trend_count = 0
        trend_direction = None
        last_window = None

        for t in trades:
            # Add $43 deposit on Mar 3
            if not deposit_added and t["created_at"] >= "2026-03-03":
                bankroll += 43.0
                peak = max(peak, bankroll)
                deposit_added = True

            side = t["side"]
            model_side = t["model_side"]
            ep = t["entry_price"]
            outcome = t["outcome"]
            actual_pnl = t["pnl"] or 0.0
            amt = t["amount_usdc"] or 0.0
            market_went = t["market_went"]
            max_bid = t["max_bid_during_window"]
            tag = t["trade_tag"]

            # Determine which window this trade belongs to
            dt = datetime.strptime(t["created_at"][:19], "%Y-%m-%d %H:%M:%S")
            window_key = f"{dt.strftime('%Y-%m-%d %H')}:{(dt.minute // 5) * 5:02d}"

            # Update regime flip confirmation
            if window_key != last_window:
                last_window = window_key
                rs = t.get("regime_strength")
                rr = t.get("regime_state")
                # For flip trades, we know regime was trending (that's why it flipped)
                if tag == "regime_flip":
                    # Infer direction from the traded side (flip bets WITH trend)
                    new_dir = side
                    if new_dir == trend_direction:
                        trend_count += 1
                    else:
                        trend_direction = new_dir
                        trend_count = 1
                elif rs is not None and rr and rr != "ranging":
                    new_dir = "UP" if rs > 0 else "DOWN"
                    if new_dir == trend_direction:
                        trend_count += 1
                    else:
                        trend_direction = new_dir
                        trend_count = 1
                else:
                    trend_count = 0
                    trend_direction = None

            # Decide bet side
            if use_flip and tag == "regime_flip" and (confirm_windows == 0 or trend_count >= confirm_windows):
                bet_side = side  # keep the flip
            elif use_flip and tag == "regime_flip" and trend_count < confirm_windows:
                bet_side = model_side  # flip not confirmed, use model
            else:
                bet_side = side  # non-flip trade, use as-is

            # Determine if bet would win
            if market_went:
                would_win = (bet_side == market_went)
            else:
                # Fallback: use actual outcome (adjusted for flipped trades)
                if tag == "regime_flip" and bet_side == model_side:
                    # We're using model side, so invert the actual outcome
                    would_win = (outcome == "LOSS")
                else:
                    would_win = (outcome == "WIN")

            # Calculate P&L
            if use_ee and outcome == "EARLY_EXIT":
                trade_pnl = actual_pnl
                trade_outcome = "EE"
            elif use_ee and max_bid is not None:
                thresh = get_exit_threshold(ep)
                if max_bid >= thresh and not would_win:
                    trade_pnl = ee_pnl(amt, ep, thresh)
                    trade_outcome = "EE"
                elif would_win:
                    trade_pnl = pnl_win(amt, ep)
                    trade_outcome = "W"
                else:
                    trade_pnl = pnl_loss(amt)
                    trade_outcome = "L"
            else:
                if would_win:
                    trade_pnl = pnl_win(amt, ep)
                    trade_outcome = "W"
                else:
                    trade_pnl = pnl_loss(amt)
                    trade_outcome = "L"

            pnl_total += trade_pnl
            bankroll += trade_pnl
            if bankroll > peak:
                peak = bankroll
            dd = peak - bankroll
            if dd > max_dd:
                max_dd = dd

            if trade_outcome == "W":
                wins += 1
            elif trade_outcome == "L":
                losses += 1
            else:
                ee_count += 1

        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        return {
            "label": label, "wins": wins, "losses": losses, "ee": ee_count,
            "wr": wr, "pnl": pnl_total, "bankroll": bankroll, "max_dd": max_dd,
        }

    # Run scenarios
    # Exclude exploration and maker_fill — only taker trades
    taker = [t for t in trades if t["trade_tag"] not in ("exploration", "maker_fill")]

    r_actual    = simulate(taker, True,  True,  0, "ACTUAL (EE + flip, no confirm)")
    r_confirmed = simulate(taker, True,  True,  3, "EE + flip (3-window confirm)")
    r_ee_only   = simulate(taker, True,  False, 0, "EE only, no flip")
    r_flip_only = simulate(taker, False, True,  0, "Flip only, no EE")
    r_naked     = simulate(taker, False, False, 0, "NAKED (model only)")

    print("=" * 95)
    print(f"  FULL HISTORY SIMULATION -- {len(taker)} taker trades")
    print(f"  {taker[0]['created_at'][:16]} to {taker[-1]['created_at'][:16]} UTC")
    print(f"  Starting: $73 + $43 deposit on Mar 3 = $116 total deposited")
    print(f"  Real BTC settlement from candle data (UTC-safe)")
    print("=" * 95)

    print(f"\n  {'Scenario':<35} {'W':>4} {'L':>4} {'EE':>4} {'WR%':>6} {'P&L':>10} {'Bankroll':>10} {'MaxDD':>8}")
    print(f"  {'-'*35} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*10} {'-'*10} {'-'*8}")
    for r in [r_actual, r_confirmed, r_ee_only, r_flip_only, r_naked]:
        print(f"  {r['label']:<35} {r['wins']:>4} {r['losses']:>4} {r['ee']:>4} "
              f"{r['wr']:>5.1f}% {r['pnl']:>+9.2f}$ ${r['bankroll']:>8.2f} ${r['max_dd']:>6.2f}")

    print(f"\n  FEATURE VALUE:")
    ee_val = r_actual["pnl"] - r_flip_only["pnl"]
    flip_val = r_actual["pnl"] - r_ee_only["pnl"]
    confirm_val = r_confirmed["pnl"] - r_actual["pnl"]
    total = r_actual["pnl"] - r_naked["pnl"]
    print(f"    Early Exit:          {ee_val:>+8.2f}$ (actual - flip_only)")
    print(f"    Regime Flip:         {flip_val:>+8.2f}$ (actual - ee_only)")
    print(f"    Confirmation fix:    {confirm_val:>+8.2f}$ (confirmed - actual)")
    print(f"    Combined uplift:     {total:>+8.2f}$ (actual - naked)")

    # Daily breakdown
    print(f"\n  DAILY P&L (ACTUAL vs NAKED):")
    from collections import defaultdict
    daily_actual = defaultdict(float)
    daily_naked = defaultdict(float)

    for t in taker:
        day = t["created_at"][:10]
        actual_pnl = t["pnl"] or 0.0
        market_went = t["market_went"]
        model_side = t["model_side"]
        amt = t["amount_usdc"] or 0.0
        ep = t["entry_price"]

        daily_actual[day] += actual_pnl

        if market_went:
            naked_win = (model_side == market_went)
        else:
            tag = t["trade_tag"]
            outcome = t["outcome"]
            if tag == "regime_flip":
                naked_win = (outcome == "LOSS")  # model was opposite of traded
            else:
                naked_win = (outcome == "WIN")

        if naked_win:
            daily_naked[day] += pnl_win(amt, ep)
        else:
            daily_naked[day] += pnl_loss(amt)

    all_days = sorted(set(list(daily_actual.keys()) + list(daily_naked.keys())))
    print(f"    {'Day':<12} {'Actual':>10} {'Naked':>10} {'Delta':>10}")
    for day in all_days:
        a = daily_actual[day]
        n = daily_naked[day]
        print(f"    {day:<12} {a:>+9.2f}$ {n:>+9.2f}$ {a-n:>+9.2f}$")

    # Regime flip trades breakdown
    flip_trades = [t for t in taker if t["trade_tag"] == "regime_flip"]
    if flip_trades:
        print(f"\n  REGIME FLIP TRADES ({len(flip_trades)}):")
        flip_right = sum(1 for t in flip_trades if t["market_went"] and t["side"] == t["market_went"])
        flip_wrong = sum(1 for t in flip_trades if t["market_went"] and t["side"] != t["market_went"])
        model_right = sum(1 for t in flip_trades if t["market_went"] and t["model_side"] == t["market_went"])
        model_wrong = sum(1 for t in flip_trades if t["market_went"] and t["model_side"] != t["market_went"])
        ft = flip_right + flip_wrong
        mt = model_right + model_wrong
        if ft:
            print(f"    Flip (traded) correct:  {flip_right}/{ft} ({flip_right/ft*100:.0f}%)")
        if mt:
            print(f"    Model (original) correct: {model_right}/{mt} ({model_right/mt*100:.0f}%)")
        flip_pnl = sum(t["pnl"] or 0 for t in flip_trades)
        print(f"    Flip actual P&L: {flip_pnl:>+.2f}$")


if __name__ == "__main__":
    main()
