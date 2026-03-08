"""Quick verification of regime detector components against raw candle data."""
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect("btc_edge.db")
rows = conn.execute("SELECT timestamp, open, close FROM candles ORDER BY timestamp DESC LIMIT 25").fetchall()
rows.reverse()

print("Last 25 1-min candles:")
print(f"  {'Time (UTC)':<12} {'Open':>10} {'Close':>10} {'Dir':>5}")
for ts, o, c in rows:
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    d = "UP" if c > o else "DOWN"
    print(f"  {dt.strftime('%H:%M:%S'):<12} {o:>10,.2f} {c:>10,.2f} {d:>5}")

# Direction consistency (last 20)
last20 = rows[-20:]
ups = sum(1 for _, o, c in last20 if c > o)
downs = 20 - ups
dir_score = (ups / 20 - 0.5) * 2.0
print(f"\nDirection: {ups}/20 UP, {downs}/20 DOWN -> score {dir_score:+.2f}")
print(f"  /regime shows: 13/20 UP (+0.30) -- {'MATCH' if abs(dir_score - 0.30) < 0.15 else 'MISMATCH'}")

# Momentum
closes = [r[2] for r in rows]
if len(closes) >= 11:
    mom10 = (closes[-1] - closes[-11]) / closes[-11]
    print(f"\nMomentum 10m: {mom10 * 100:+.4f}%")
if len(closes) >= 21:
    mom20 = (closes[-1] - closes[-21]) / closes[-21]
    print(f"Momentum 20m: {mom20 * 100:+.4f}%")
    agree = (mom10 > 0) == (mom20 > 0)
    print(f"Agree: {'YES' if agree else 'NO'} -- /regime shows: agree (+0.51)")

# Price vs EMA-21
if len(closes) >= 21:
    # Simple EMA-21
    multiplier = 2 / (21 + 1)
    ema = sum(closes[:21]) / 21
    for p in closes[21:]:
        ema = (p - ema) * multiplier + ema
    print(f"\nEMA-21: {ema:,.2f}")
    print(f"BTC now: {closes[-1]:,.2f}")

    last10 = rows[-10:]
    above = sum(1 for _, o, c in last10 if c > ema)
    pve_score = (above / 10 - 0.5) * 2.0
    print(f"Price vs EMA: {above}/10 above -> score {pve_score:+.2f}")
    print(f"  /regime shows: 8/10 above (+0.60) -- {'MATCH' if abs(pve_score - 0.60) < 0.3 else 'MISMATCH'}")

conn.close()
