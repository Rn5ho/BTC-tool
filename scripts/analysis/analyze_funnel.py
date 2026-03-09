"""Trade funnel analysis: where do 12 windows/hour go?"""
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect("btc_edge_live.db")

# Since dampening fix (Mar 3 22:30)
START = "2026-03-03 22:30"

# Total hours of operation
first = conn.execute(f"""
    SELECT MIN(created_at) FROM (
        SELECT created_at FROM live_trades WHERE created_at >= '{START}' AND success=1
        UNION ALL
        SELECT created_at FROM skipped_windows WHERE created_at >= '{START}'
    )
""").fetchone()[0]
last = conn.execute(f"""
    SELECT MAX(created_at) FROM (
        SELECT created_at FROM live_trades WHERE created_at >= '{START}' AND success=1
        UNION ALL
        SELECT created_at FROM skipped_windows WHERE created_at >= '{START}'
    )
""").fetchone()[0]

dt_first = datetime.strptime(first[:19], "%Y-%m-%d %H:%M:%S")
dt_last = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
hours = (dt_last - dt_first).total_seconds() / 3600
print(f"Period: {first[:16]} to {last[:16]} UTC ({hours:.1f} hours)")
print(f"Possible windows: {hours * 12:.0f} (12 per hour)")
print()

# Trades (by tag)
trades = conn.execute(f"""
    SELECT trade_tag, COUNT(*) FROM live_trades
    WHERE success=1 AND outcome IS NOT NULL AND created_at >= '{START}'
    GROUP BY trade_tag
""").fetchall()
total_trades = sum(c for _, c in trades)
print(f"TRADED: {total_trades}")
for tag, cnt in trades:
    print(f"  {tag or 'normal':<20} {cnt:>4}")

# Skips by reason category
skips = conn.execute(f"""
    SELECT skip_reason, COUNT(*) FROM skipped_windows
    WHERE created_at >= '{START}'
    GROUP BY skip_reason ORDER BY COUNT(*) DESC
""").fetchall()
total_skips = sum(c for _, c in skips)

# Group by category
skip_cats = {}
for reason, cnt in skips:
    if "low confidence" in reason:
        cat = "low confidence"
    elif "blacklist" in reason:
        cat = "blacklisted hour"
    elif "entry price" in reason:
        cat = "entry price filter"
    elif "trend conflict" in reason:
        cat = "trend conflict"
    elif "time gate" in reason:
        cat = "time gate"
    elif "streak" in reason:
        cat = "streak guard"
    else:
        cat = reason
    skip_cats[cat] = skip_cats.get(cat, 0) + cnt

print(f"\nSKIPPED: {total_skips}")
for cat in sorted(skip_cats, key=lambda x: -skip_cats[x]):
    print(f"  {cat:<25} {skip_cats[cat]:>4}")

accounted = total_trades + total_skips
possible = int(hours * 12)
unaccounted = possible - accounted
print(f"\nUNACCOUNTED: {unaccounted} (no trade + no skip recorded)")
print(f"  (likely: no market available, startup delays, time gate)")

# Funnel
print(f"\n--- FUNNEL (per hour) ---")
print(f"  Available windows:    12.0")
print(f"  Traded:               {total_trades / hours:>5.1f}  ({total_trades / possible * 100:.0f}%)")
print(f"  Skipped (recorded):   {total_skips / hours:>5.1f}  ({total_skips / possible * 100:.0f}%)")
print(f"  Unaccounted:          {unaccounted / hours:>5.1f}  ({unaccounted / possible * 100:.0f}%)")

# What if we removed dampening but kept min_confidence at 1.5%?
# The dampening raises effective threshold from 1.5% to 2.5%
# How many of the "low confidence" skips had confidence between 1.5% and 2.5%?
print(f"\n--- DAMPENING IMPACT ---")
# Parse confidence from skip reason
import re
conf_skips = []
for reason, cnt in skips:
    m = re.search(r"(\d+\.\d+)%", reason)
    if m and "low confidence" in reason:
        conf_val = float(m.group(1))
        conf_skips.append((conf_val, cnt))

# These are DAMPENED confidence values. The raw value was conf/0.6
# A skip at dampened 0.9% means raw was 0.9/0.6 = 1.5% (would have passed without dampening)
# A skip at dampened 1.4% means raw was 1.4/0.6 = 2.33% (would have passed without dampening)
total_would_trade = 0
total_low_conf = 0
for conf_val, cnt in conf_skips:
    raw_conf = conf_val / 0.6
    total_low_conf += cnt
    if raw_conf >= 1.5:
        total_would_trade += cnt

print(f"  Low-confidence skips: {total_low_conf}")
print(f"  Would have traded without dampening: {total_would_trade}")
print(f"  Truly low confidence (even raw < 1.5%): {total_low_conf - total_would_trade}")
print(f"  Extra trades/hour without dampening: +{total_would_trade / hours:.1f}")

# Check the EE rescue rate on traded windows — how good is the safety net?
print(f"\n--- EE SAFETY NET (since {START[:10]}) ---")
r = conn.execute(f"""
    SELECT outcome, COUNT(*), ROUND(SUM(pnl), 2)
    FROM live_trades WHERE success=1 AND outcome IS NOT NULL
    AND created_at >= '{START}'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
    GROUP BY outcome
""").fetchall()
for outcome, cnt, pnl in r:
    print(f"  {outcome:<12} {cnt:>4} trades  P&L: ${pnl:+.2f}")

# EE on losses specifically
ee_data = conn.execute(f"""
    SELECT COUNT(*), ROUND(SUM(pnl), 2)
    FROM live_trades WHERE success=1 AND outcome='EARLY_EXIT'
    AND created_at >= '{START}'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()
loss_data = conn.execute(f"""
    SELECT COUNT(*), ROUND(SUM(pnl), 2)
    FROM live_trades WHERE success=1 AND outcome='LOSS'
    AND created_at >= '{START}'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()
if ee_data[0] and loss_data[0]:
    total_neg = ee_data[0] + loss_data[0]
    rescued_pct = ee_data[0] / total_neg * 100
    print(f"\n  Would-be losses: {total_neg}")
    print(f"  Rescued by EE: {ee_data[0]} ({rescued_pct:.0f}%)")
    print(f"  Actually lost: {loss_data[0]} ({100 - rescued_pct:.0f}%)")
    avg_ee_pnl = ee_data[1] / ee_data[0]
    avg_loss_pnl = loss_data[1] / loss_data[0]
    print(f"  Avg EE P&L: ${avg_ee_pnl:+.2f}  |  Avg LOSS P&L: ${avg_loss_pnl:+.2f}")

conn.close()
