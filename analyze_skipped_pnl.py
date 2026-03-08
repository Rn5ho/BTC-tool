"""Simulate what would have happened if we traded the skipped low-confidence windows."""
import sqlite3
import re
from datetime import datetime, timedelta, timezone

conn = sqlite3.connect("btc_edge_live.db")

FEE_RATE = 0.25
FEE_EXP = 2
BET_SIZE = 5.0  # approximate bet size

def fee(p):
    return (FEE_RATE / 10000) * (p ** FEE_EXP)

def pnl_win(amt, price):
    f = fee(price)
    return (amt / price) * (1 - f) - amt

def pnl_loss(amt):
    return -amt

def ee_pnl(amt, entry_price, exit_price):
    f_entry = fee(entry_price)
    tokens = (amt / entry_price) * (1 - f_entry)
    f_exit = fee(exit_price)
    return tokens * exit_price * (1 - f_exit) - amt

def get_exit_threshold(ep):
    if ep < 0.35: return 0.50
    elif ep < 0.40: return 0.45
    elif ep < 0.50: return 0.65
    else: return 0.95


# Get all low-confidence skips
skips = conn.execute("""
    SELECT created_at, btc_price, regime_state, regime_strength, skip_reason, entry_price
    FROM skipped_windows
    WHERE skip_reason LIKE 'low confidence%'
    ORDER BY created_at
""").fetchall()

print(f"Simulating {len(skips)} skipped low-confidence windows")
print(f"{'='*100}")
print()

results = []
for created_at, btc_price, regime, strength, reason, skip_entry_price in skips:
    dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    dt_utc = dt.replace(tzinfo=timezone.utc)
    wmin = (dt_utc.minute // 5) * 5
    ws = dt_utc.replace(minute=wmin, second=0, microsecond=0)
    we = ws + timedelta(minutes=5)
    sms = int(ws.timestamp() * 1000)
    ems = int(we.timestamp() * 1000)

    # Get BTC open/close for this window
    c2 = conn.cursor()
    c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
               (sms, sms + 60000))
    orow = c2.fetchone()
    c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
               (ems - 60000, ems + 60000))
    crow = c2.fetchone()

    if not orow or not crow:
        continue

    btc_open = orow[0]
    btc_close = crow[0]
    btc_went = "UP" if btc_close > btc_open else "DOWN"

    # Get market snapshots for this window to find max bid and entry price
    # Use market_snapshots to find the bid behavior during this window
    snapshots = conn.execute("""
        SELECT up_price, down_price, up_best_bid, down_best_bid
        FROM market_snapshots
        WHERE timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp
    """, (sms, ems)).fetchall()

    # Estimate entry price from snapshots (ask price at window start)
    # Since the model has low confidence, P(up) is near 50%
    # Entry price would be roughly the market ask price
    if snapshots:
        # Use first snapshot's prices
        up_ask = snapshots[0][0]  # up_price (ask for UP side)
        down_ask = snapshots[0][1]  # down_price (ask for DOWN side)

        # Find max bid during window (for EE simulation)
        # We need to check both sides since we don't know which side the model would pick
        # With low confidence, model is ~50/50, so let's check both scenarios

        # For UP bet: entry = up_ask, need max up_bid during window
        # For DOWN bet: entry = down_ask, need max down_bid during window
        # market_snapshots stores up_price and down_price (which are ask prices)
        # The bid would be 1 - opposite_ask (approximately)

        # Actually, let's use the stored entry_price from skipped_windows if available
        # Otherwise estimate from market prices
    else:
        up_ask = down_ask = None

    # Parse dampened confidence from skip reason
    m = re.search(r"(\d+\.\d+)%", reason)
    dampened_conf = float(m.group(1)) / 100 if m else 0

    # Raw confidence (before dampening)
    raw_conf = dampened_conf / 0.6

    # Model predicted side: if raw P(up) > 0.5 -> UP, else DOWN
    # But we don't know which side from the skip record
    # We CAN infer: dampened confidence = |dampened_p_up - 0.5|
    # But we don't know if it was UP or DOWN prediction

    # Let's simulate BOTH scenarios for each window and take the model's likely pick
    # Since we don't know the side, we'll use a simpler approach:
    # Check what the ACTUAL traded windows around this time were doing

    # For now, simulate as if model picks randomly (50/50) — worst case
    # And separately, simulate as if model picks correctly at its historical rate (~54%)

    # Actually, let's just check: if we traded this window, what's the P&L with/without EE?
    # We need to simulate both UP and DOWN bets

    for bet_side in ["UP", "DOWN"]:
        if bet_side == "UP":
            ep = up_ask if up_ask else 0.45  # default if no snapshot
        else:
            ep = down_ask if down_ask else 0.45

        if ep < 0.25 or ep > 0.65:
            continue  # would be filtered by entry price anyway

        would_win = (bet_side == btc_went)
        amt = BET_SIZE

        # Without EE
        if would_win:
            naked_pnl = pnl_win(amt, ep)
        else:
            naked_pnl = pnl_loss(amt)

        # With EE — check if max bid during window crossed threshold
        # We need per-side bid data which we don't have in market_snapshots easily
        # Use the actual traded windows' EE rate as proxy
        # For now, just calculate the theoretical P&L

        results.append({
            "time": created_at,
            "side": bet_side,
            "entry": ep,
            "btc_went": btc_went,
            "win": would_win,
            "naked_pnl": naked_pnl,
            "raw_conf": raw_conf,
        })

# Now analyze: what if model picked at various accuracy rates?
print("SCENARIO ANALYSIS: What if we traded these skipped windows?")
print()

# Get actual model accuracy from traded windows in same period
traded = conn.execute("""
    SELECT side, outcome, entry_price, pnl, trade_tag, max_bid_during_window
    FROM live_trades
    WHERE success=1 AND outcome IS NOT NULL
    AND created_at >= '2026-03-03 22:30'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchall()

# Group skipped windows (deduplicate — each window has UP and DOWN entry)
windows = {}
for r in results:
    key = r["time"]
    if key not in windows:
        windows[key] = {"btc_went": r["btc_went"], "raw_conf": r["raw_conf"]}
        # Find entry prices for both sides
        windows[key]["entries"] = {}
    windows[key]["entries"][r["side"]] = r["entry"]

print(f"Unique skipped windows with candle data: {len(windows)}")
print()

# Simulate at different model accuracy rates
for model_wr_label, model_wr in [("50% (coin flip)", 0.50), ("52%", 0.52), ("54% (our model avg)", 0.54)]:
    total_pnl_no_ee = 0
    total_pnl_with_ee = 0
    win_count = 0
    loss_count = 0
    ee_count = 0

    for key, w in windows.items():
        btc_went = w["btc_went"]

        # Simulate: model picks right side with probability = model_wr
        # Expected P&L = model_wr * win_pnl + (1 - model_wr) * loss_pnl

        # Get entry price for the "correct" side and "wrong" side
        correct_side = btc_went
        wrong_side = "DOWN" if btc_went == "UP" else "UP"

        ep_correct = w["entries"].get(correct_side, 0.45)
        ep_wrong = w["entries"].get(wrong_side, 0.45)

        if ep_correct < 0.25 or ep_correct > 0.65:
            ep_correct = 0.45
        if ep_wrong < 0.25 or ep_wrong > 0.65:
            ep_wrong = 0.45

        win_pnl = pnl_win(BET_SIZE, ep_correct)
        loss_pnl_val = pnl_loss(BET_SIZE)

        # No EE: expected PnL
        expected_no_ee = model_wr * win_pnl + (1 - model_wr) * loss_pnl_val
        total_pnl_no_ee += expected_no_ee

        # With EE: losses get rescued at ~72% rate with avg +$3.13
        # From our data: 72% of losses become EE at +$3.13, 28% stay as -$5.51
        ee_rescue_rate = 0.72
        avg_ee_pnl = 3.13
        avg_real_loss = -5.51

        expected_with_ee = (model_wr * win_pnl +
                           (1 - model_wr) * ee_rescue_rate * avg_ee_pnl +
                           (1 - model_wr) * (1 - ee_rescue_rate) * avg_real_loss)
        total_pnl_with_ee += expected_with_ee

    print(f"  Model WR = {model_wr_label}:")
    print(f"    Without EE:  {total_pnl_no_ee:>+8.2f}$ ({total_pnl_no_ee/len(windows):>+.3f}$/trade)")
    print(f"    With EE:     {total_pnl_with_ee:>+8.2f}$ ({total_pnl_with_ee/len(windows):>+.3f}$/trade)")
    print()

# Now do the same but separately for windows that would/wouldn't have passed without dampening
print(f"\n{'='*100}")
print("SPLIT BY DAMPENING IMPACT:")
print()

for subset_label, min_raw, max_raw in [
    ("Would trade without dampening (raw 1.5-2.5%)", 1.5, 2.5),
    ("Truly low confidence (raw < 1.5%)", 0.0, 1.5),
]:
    subset = {k: v for k, v in windows.items() if min_raw <= v["raw_conf"] * 100 < max_raw}
    if not subset:
        print(f"  {subset_label}: 0 windows")
        continue

    print(f"  {subset_label}: {len(subset)} windows")

    for model_wr_label, model_wr in [("50%", 0.50), ("54%", 0.54)]:
        total_with_ee = 0
        for key, w in subset.items():
            btc_went = w["btc_went"]
            correct_side = btc_went
            ep_correct = w["entries"].get(correct_side, 0.45)
            if ep_correct < 0.25 or ep_correct > 0.65:
                ep_correct = 0.45

            win_pnl = pnl_win(BET_SIZE, ep_correct)
            expected_with_ee = (model_wr * win_pnl +
                               (1 - model_wr) * 0.72 * 3.13 +
                               (1 - model_wr) * 0.28 * (-5.51))
            total_with_ee += expected_with_ee

        print(f"    At {model_wr_label} WR with EE: {total_with_ee:>+8.2f}$ ({total_with_ee/len(subset):>+.3f}$/trade)")
    print()

conn.close()
