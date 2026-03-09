"""Deep P&L reconciliation: compare DB records vs actual CLOB fills."""
import sqlite3
import json

c = sqlite3.connect("btc_edge.db")

# --- BUY side: are we getting fewer tokens than DB thinks? ---
# For BUY: takingAmount = tokens received, makingAmount = USDC paid
rows = c.execute(
    "SELECT amount_usdc, entry_price, response_json, outcome, pnl, created_at "
    "FROM live_trades WHERE success = 1 AND response_json IS NOT NULL "
    "ORDER BY created_at DESC"
).fetchall()

total_token_diff = 0
total_pnl_impact = 0
win_count = 0
loss_count = 0
ee_count = 0

for bet, entry, rjson, outcome, pnl, dt in rows:
    if not rjson or not bet or not entry:
        continue
    try:
        d = json.loads(rjson)
    except Exception:
        continue

    taking_str = d.get("takingAmount", "")
    making_str = d.get("makingAmount", "")
    if not taking_str or not making_str:
        continue

    # Check if this is a BUY or SELL response
    # BUY: taking=tokens, making=USDC
    # SELL (EE): taking=USDC, making=tokens
    taking = float(taking_str)
    making = float(making_str)

    # For BUY trades: taking = actual tokens received
    # DB expects: tokens = bet / entry_price (before fee adjustment)
    # The taking already includes fee deduction by CLOB
    if outcome in ("WIN", "LOSS"):
        actual_tokens = taking
        db_tokens_no_fee = bet / entry if entry > 0 else 0
        token_diff = db_tokens_no_fee - actual_tokens
        # For WIN: lost pnl = token_diff * $1 (each missing token = $1 less payout)
        if outcome == "WIN":
            pnl_impact = token_diff * 1.0  # each token redeems at $1
            win_count += 1
        else:
            pnl_impact = 0  # LOSS = lose everything, tokens don't matter
            loss_count += 1
        total_token_diff += token_diff
        total_pnl_impact += pnl_impact

print(f"=== BUY TOKEN ANALYSIS ===")
print(f"WIN trades: {win_count}")
print(f"LOSS trades: {loss_count}")
print(f"Total token deficit (DB vs actual): {total_token_diff:.2f}")
print(f"WIN P&L impact (missing tokens x $1): ${total_pnl_impact:.2f}")
print()

# Now check: does the DB WIN pnl already account for fees?
# If yes, the gap is elsewhere. Let's verify.
win_rows = c.execute(
    "SELECT amount_usdc, entry_price, pnl, response_json "
    "FROM live_trades WHERE outcome = 'WIN' AND response_json IS NOT NULL "
    "ORDER BY created_at DESC LIMIT 10"
).fetchall()

print("=== WIN P&L: DB vs actual tokens ===")
print("bet$   entry  db_pnl  actual_tokens  pnl_from_actual  diff")
for bet, entry, pnl, rjson in win_rows:
    if not rjson:
        continue
    d = json.loads(rjson)
    ts = d.get("takingAmount", "")
    if not ts or not str(ts).strip():
        continue
    actual_tokens = float(ts)
    actual_pnl = actual_tokens * 1.0 - bet  # redeem at $1
    diff = (pnl or 0) - actual_pnl
    print(f"${bet:5.2f}  {entry:.3f}  ${pnl or 0:+6.2f}  {actual_tokens:8.2f}  ${actual_pnl:+6.2f}     ${diff:+.2f}")

print()

# --- Total reconciliation ---
# DB total P&L
db_total = c.execute("SELECT SUM(pnl) FROM live_trades WHERE outcome IS NOT NULL").fetchone()[0] or 0

# Actual WIN P&L from tokens
actual_win_pnl = 0
win_db_pnl = 0
for bet, entry, pnl, rjson in c.execute(
    "SELECT amount_usdc, entry_price, pnl, response_json "
    "FROM live_trades WHERE outcome = 'WIN' AND response_json IS NOT NULL"
).fetchall():
    if not rjson:
        continue
    d = json.loads(rjson)
    ts = d.get("takingAmount", "")
    if ts and ts.strip():
        actual_tokens = float(ts)
        actual_win_pnl += (actual_tokens - bet)
    win_db_pnl += (pnl or 0)

# Actual EE P&L from takingAmount
actual_ee_pnl = 0
ee_db_pnl = 0
for bet, pnl, rjson in c.execute(
    "SELECT amount_usdc, pnl, response_json "
    "FROM live_trades WHERE outcome = 'EARLY_EXIT' AND response_json IS NOT NULL"
).fetchall():
    if not rjson or not pnl:
        continue
    d = json.loads(rjson)
    ts = d.get("takingAmount", "")
    if ts and ts.strip():
        actual_ee_pnl += (float(ts) - bet)
    ee_db_pnl += pnl

# LOSS P&L is always -bet (same in DB and reality)
loss_pnl = c.execute("SELECT SUM(pnl) FROM live_trades WHERE outcome = 'LOSS'").fetchone()[0] or 0

print(f"=== FULL RECONCILIATION ===")
print(f"WIN P&L  - DB: ${win_db_pnl:+.2f}  Actual: ${actual_win_pnl:+.2f}  Gap: ${win_db_pnl - actual_win_pnl:+.2f}")
print(f"EE P&L   - DB: ${ee_db_pnl:+.2f}  Actual: ${actual_ee_pnl:+.2f}  Gap: ${ee_db_pnl - actual_ee_pnl:+.2f}")
print(f"LOSS P&L - DB: ${loss_pnl:+.2f}  (same in reality)")
print()
print(f"DB total:     ${db_total:+.2f}")
print(f"Actual total: ${actual_win_pnl + actual_ee_pnl + loss_pnl:+.2f}")
print(f"Gap:          ${db_total - (actual_win_pnl + actual_ee_pnl + loss_pnl):+.2f}")
print(f"User reports: ~$+4.00")
