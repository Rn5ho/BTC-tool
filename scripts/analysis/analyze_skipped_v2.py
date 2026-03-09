"""Realistic simulation of skipped windows using actual market snapshot bid data."""
import sqlite3
import re
from datetime import datetime, timedelta, timezone

conn = sqlite3.connect("btc_edge_live.db")

FEE_RATE = 0.25
FEE_EXP = 2
BET_SIZE = 5.0

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


skips = conn.execute("""
    SELECT created_at, btc_price, regime_state, regime_strength, skip_reason, entry_price
    FROM skipped_windows
    WHERE skip_reason LIKE 'low confidence%'
    ORDER BY created_at
""").fetchall()

print(f"Analyzing {len(skips)} skipped low-confidence windows with real market data")
print("=" * 100)

# For each skipped window, find:
# 1. BTC direction (from candles)
# 2. Entry price (from market snapshots - ask price at window start)
# 3. Max bid during window (from market snapshots - for EE check)
# 4. Simulate both with and without EE

detailed = []

for created_at, btc_price, regime, strength, reason, skip_ep in skips:
    dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    dt_utc = dt.replace(tzinfo=timezone.utc)
    wmin = (dt_utc.minute // 5) * 5
    ws = dt_utc.replace(minute=wmin, second=0, microsecond=0)
    we = ws + timedelta(minutes=5)
    sms = int(ws.timestamp() * 1000)
    ems = int(we.timestamp() * 1000)

    # BTC direction
    c2 = conn.cursor()
    c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
               (sms, sms + 60000))
    orow = c2.fetchone()
    c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
               (ems - 60000, ems + 60000))
    crow = c2.fetchone()

    if not orow or not crow:
        continue

    btc_went = "UP" if crow[0] > orow[0] else "DOWN"

    # Get market snapshots for this 5-min window
    # market_snapshots uses seconds, candles use milliseconds
    snaps = conn.execute("""
        SELECT up_price, down_price, up_best_bid, down_best_bid
        FROM market_snapshots
        WHERE timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp
    """, (sms // 1000, ems // 1000)).fetchall()

    if not snaps:
        continue

    # Entry price = ask price at window start (first snapshot)
    up_entry = snaps[0][0]   # up_price = UP ask
    down_entry = snaps[0][1]  # down_price = DOWN ask

    # Max bid during window (for EE simulation)
    up_max_bid = max(s[2] for s in snaps if s[2]) if any(s[2] for s in snaps) else 0
    down_max_bid = max(s[3] for s in snaps if s[3]) if any(s[3] for s in snaps) else 0

    # Parse dampened confidence
    m = re.search(r"(\d+\.\d+)%", reason)
    dampened_conf = float(m.group(1)) / 100 if m else 0
    raw_conf = dampened_conf / 0.6

    # Simulate for both sides
    for bet_side in ["UP", "DOWN"]:
        ep = up_entry if bet_side == "UP" else down_entry
        max_bid = up_max_bid if bet_side == "UP" else down_max_bid

        if not ep or ep < 0.25 or ep > 0.65:
            continue

        would_win = (bet_side == btc_went)
        thresh = get_exit_threshold(ep)
        ee_triggered = max_bid >= thresh if max_bid else False

        # P&L without EE
        naked = pnl_win(BET_SIZE, ep) if would_win else pnl_loss(BET_SIZE)

        # P&L with EE
        if would_win:
            with_ee = pnl_win(BET_SIZE, ep)
            ee_outcome = "WIN"
        elif ee_triggered:
            with_ee = ee_pnl(BET_SIZE, ep, thresh)
            ee_outcome = "EE_RESCUE"
        else:
            with_ee = pnl_loss(BET_SIZE)
            ee_outcome = "LOSS"

        detailed.append({
            "time": created_at,
            "side": bet_side,
            "ep": ep,
            "max_bid": max_bid,
            "thresh": thresh,
            "btc_went": btc_went,
            "win": would_win,
            "ee_triggered": ee_triggered,
            "naked_pnl": naked,
            "ee_pnl": with_ee,
            "ee_outcome": ee_outcome,
            "raw_conf": raw_conf,
        })

# Group by window (each window has UP and DOWN entries)
windows = {}
for d in detailed:
    key = d["time"]
    if key not in windows:
        windows[key] = {"raw_conf": d["raw_conf"]}
    windows[key][d["side"]] = d

print(f"\nWindows with full data: {len(windows)}")

# For each window, the model would pick ONE side
# We don't know which, so simulate at different accuracy rates
print()
print(f"{'Scenario':<45} {'P&L':>8} {'$/trade':>8} {'W':>4} {'L':>4} {'EE':>4}")
print("-" * 80)

for wr_label, wr in [("50% WR (coin flip)", 0.50), ("52% WR", 0.52), ("54% WR (model avg)", 0.54)]:
    # Expected P&L: for each window, blend correct/incorrect pick
    total_naked = 0
    total_ee = 0
    exp_w = exp_l = exp_ee = 0

    for key, w in windows.items():
        btc_went = None
        for side in ["UP", "DOWN"]:
            if side in w:
                btc_went = w[side]["btc_went"]
                break

        correct_side = btc_went
        wrong_side = "DOWN" if btc_went == "UP" else "UP"

        if correct_side in w and wrong_side in w:
            correct = w[correct_side]
            wrong = w[wrong_side]

            # Naked P&L
            total_naked += wr * correct["naked_pnl"] + (1 - wr) * wrong["naked_pnl"]

            # EE P&L
            correct_ee_pnl = correct["ee_pnl"]  # WIN
            wrong_ee_pnl = wrong["ee_pnl"]       # EE rescue or LOSS

            total_ee += wr * correct_ee_pnl + (1 - wr) * wrong_ee_pnl

            exp_w += wr
            if wrong["ee_outcome"] == "EE_RESCUE":
                exp_ee += (1 - wr)
            else:
                exp_l += (1 - wr)
        elif correct_side in w:
            total_naked += wr * w[correct_side]["naked_pnl"]
            total_ee += wr * w[correct_side]["ee_pnl"]
            exp_w += wr

    n = len(windows)
    print(f"  No EE, {wr_label:<30} {total_naked:>+7.2f}$ {total_naked/n:>+7.3f}$")
    print(f"  With EE, {wr_label:<28} {total_ee:>+7.2f}$ {total_ee/n:>+7.3f}$  {exp_w:>4.0f} {exp_l:>4.0f} {exp_ee:>4.0f}")
    print()

# Detailed: how many windows actually have EE available?
print(f"\n{'='*100}")
print("EE AVAILABILITY ON SKIPPED WINDOWS:")
ee_avail_up = sum(1 for k, w in windows.items() if "UP" in w and w["UP"]["ee_triggered"] and not w["UP"]["win"])
ee_avail_dn = sum(1 for k, w in windows.items() if "DOWN" in w and w["DOWN"]["ee_triggered"] and not w["DOWN"]["win"])
no_ee_up = sum(1 for k, w in windows.items() if "UP" in w and not w["UP"]["ee_triggered"] and not w["UP"]["win"])
no_ee_dn = sum(1 for k, w in windows.items() if "DOWN" in w and not w["DOWN"]["ee_triggered"] and not w["DOWN"]["win"])

print(f"  Wrong-side bets where EE would rescue (UP): {ee_avail_up}/{ee_avail_up + no_ee_up}")
print(f"  Wrong-side bets where EE would rescue (DN): {ee_avail_dn}/{ee_avail_dn + no_ee_dn}")
total_wrong = ee_avail_up + ee_avail_dn + no_ee_up + no_ee_dn
total_rescued = ee_avail_up + ee_avail_dn
if total_wrong:
    print(f"  Overall EE rescue rate on wrong bets: {total_rescued}/{total_wrong} ({total_rescued/total_wrong*100:.0f}%)")

# Show some example EE rescues
print(f"\nSAMPLE WRONG-SIDE BETS WITH EE DATA:")
print(f"  {'Time':<17} {'Side':>5} {'Entry':>6} {'MaxBid':>7} {'Thresh':>7} {'EE?':>4} {'Naked':>8} {'WithEE':>8}")
count = 0
for key in sorted(windows.keys()):
    w = windows[key]
    for side in ["UP", "DOWN"]:
        if side in w and not w[side]["win"]:
            d = w[side]
            print(f"  {key[5:19]:<17} {side:>5} {d['ep']:>6.3f} {d['max_bid']:>7.3f} {d['thresh']:>7.2f} "
                  f"{'Y' if d['ee_triggered'] else 'N':>4} {d['naked_pnl']:>+7.2f}$ {d['ee_pnl']:>+7.2f}$")
            count += 1
            if count >= 20:
                break
    if count >= 20:
        break

# Split by dampening bucket
print(f"\n{'='*100}")
print("SPLIT BY DAMPENING BUCKET (With EE, at 54% model WR):")
for label, min_r, max_r in [
    ("Dampening-killed (raw 1.5-2.5%)", 1.5, 2.5),
    ("Truly low conf (raw 0.75-1.5%)", 0.75, 1.5),
    ("Near-zero conf (raw < 0.75%)", 0.0, 0.75),
]:
    subset = {k: v for k, v in windows.items() if min_r <= v["raw_conf"] * 100 < max_r}
    if not subset:
        print(f"  {label}: 0 windows")
        continue

    total_ee = 0
    for key, w in subset.items():
        btc_went = None
        for side in ["UP", "DOWN"]:
            if side in w:
                btc_went = w[side]["btc_went"]
                break
        correct = btc_went
        wrong = "DOWN" if btc_went == "UP" else "UP"
        if correct in w and wrong in w:
            total_ee += 0.54 * w[correct]["ee_pnl"] + 0.46 * w[wrong]["ee_pnl"]

    print(f"  {label}: {len(subset)} windows -> {total_ee:>+.2f}$ ({total_ee/len(subset):>+.3f}$/trade)")

conn.close()
