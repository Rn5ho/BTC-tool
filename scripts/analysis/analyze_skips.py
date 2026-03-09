"""Analyze skipped windows — what would have happened if we traded them?"""
import sqlite3
from datetime import datetime, timedelta, timezone

conn = sqlite3.connect("btc_edge_live.db")

# For each skipped window with low confidence, check what BTC actually did
rows = conn.execute("""
    SELECT created_at, btc_price, regime_state, regime_strength, skip_reason
    FROM skipped_windows
    WHERE skip_reason LIKE 'low confidence%'
    ORDER BY created_at
""").fetchall()

print(f"LOW-CONFIDENCE SKIPS: {len(rows)} windows")
print(f"Checking what BTC did in each skipped 5-min window...")
print()

wins_up = wins_down = unknown = 0
for created_at, btc, regime, strength, reason in rows:
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

    if orow and crow:
        went = "UP" if crow[0] > orow[0] else "DOWN"
        if went == "UP":
            wins_up += 1
        else:
            wins_down += 1
    else:
        unknown += 1

total = wins_up + wins_down
if total:
    print(f"BTC went UP: {wins_up}/{total} ({wins_up / total * 100:.0f}%)")
    print(f"BTC went DOWN: {wins_down}/{total} ({wins_down / total * 100:.0f}%)")
    print(f"Unknown: {unknown}")
    print()
    print("Since market is ~50/50, a coin-flip model on these windows would break even.")
    print("But fees + spread cost ~1.5% per trade.")
    print(f"Skipping {total} x ~$5 trades saves ~${total * 5 * 0.02:.0f} in fees+spread")
else:
    print("No candle data available for skipped windows")

# WR comparison before/after dampening
print()
print("--- SETTLEMENT WR COMPARISON ---")

r = conn.execute("""
    SELECT
        SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as w,
        SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as l,
        SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as ee,
        ROUND(SUM(pnl), 2) as pnl,
        COUNT(*) as cnt
    FROM live_trades WHERE success=1 AND outcome IS NOT NULL
    AND created_at < '2026-03-03 22:30'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()
w, l, ee, pnl, cnt = r
wr = w / (w + l) * 100 if (w + l) else 0
print(f"Before dampening fix (Feb 28 - Mar 3 22:30): {cnt} trades")
print(f"  W:{w} L:{l} EE:{ee} Settlement WR:{wr:.0f}%  P&L:${pnl:+.2f}")

r = conn.execute("""
    SELECT
        SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as w,
        SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as l,
        SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as ee,
        ROUND(SUM(pnl), 2) as pnl,
        COUNT(*) as cnt
    FROM live_trades WHERE success=1 AND outcome IS NOT NULL
    AND created_at >= '2026-03-03 22:30'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()
w, l, ee, pnl, cnt = r
wr = w / (w + l) * 100 if (w + l) else 0
print(f"After dampening fix (Mar 3 22:30+): {cnt} trades")
print(f"  W:{w} L:{l} EE:{ee} Settlement WR:{wr:.0f}%  P&L:${pnl:+.2f}")
print(f"  (also includes regime flip, EE tier changes, etc.)")

# P&L per trade comparison
print()
print("--- P&L PER TRADE ---")
r1 = conn.execute("""
    SELECT ROUND(SUM(pnl), 2), COUNT(*) FROM live_trades
    WHERE success=1 AND outcome IS NOT NULL AND created_at < '2026-03-03 22:30'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()
r2 = conn.execute("""
    SELECT ROUND(SUM(pnl), 2), COUNT(*) FROM live_trades
    WHERE success=1 AND outcome IS NOT NULL AND created_at >= '2026-03-03 22:30'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()
if r1[1] and r2[1]:
    print(f"Before: ${r1[0] / r1[1]:+.3f}/trade ({r1[1]} trades)")
    print(f"After:  ${r2[0] / r2[1]:+.3f}/trade ({r2[1]} trades)")

# How many trades per hour before vs after?
print()
print("--- TRADE FREQUENCY ---")
# Count hours with activity
h1 = conn.execute("""
    SELECT COUNT(DISTINCT substr(created_at, 1, 13)) FROM live_trades
    WHERE success=1 AND outcome IS NOT NULL AND created_at < '2026-03-03 22:30'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()[0]
h2 = conn.execute("""
    SELECT COUNT(DISTINCT substr(created_at, 1, 13)) FROM live_trades
    WHERE success=1 AND outcome IS NOT NULL AND created_at >= '2026-03-03 22:30'
    AND trade_tag NOT IN ('exploration', 'maker_fill')
""").fetchone()[0]
if h1 and h2:
    print(f"Before: {r1[1] / h1:.1f} trades/hour ({r1[1]} trades over {h1} hours)")
    print(f"After:  {r2[1] / h2:.1f} trades/hour ({r2[1]} trades over {h2} hours)")
    print(f"  -> {(1 - r2[1] / h2 / (r1[1] / h1)) * 100:.0f}% fewer trades/hour")

conn.close()
