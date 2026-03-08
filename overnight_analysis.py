"""Overnight bleeding analysis - 2026-03-05"""
import sqlite3
from datetime import datetime

conn = sqlite3.connect('btc_edge.db')
conn.row_factory = sqlite3.Row

# === SECTION 1: Overnight trades ===
print("=" * 120)
print("OVERNIGHT TRADES (Mar 4 18:00 UTC -> Mar 5 07:00 UTC)")
print("=" * 120)

trades = conn.execute('''
    SELECT created_at, side, entry_price, outcome, pnl, trade_tag,
           model_confidence, regime_strength, max_bid_during_window,
           exit_threshold_used, amount_usdc, order_id, market_slug
    FROM live_trades
    WHERE created_at >= '2026-03-04 18:00:00'
    ORDER BY created_at
''').fetchall()

total_pnl = 0
wins = losses = early_exits = pending = 0
loss_streaks = []
current_streak = 0
hourly = {}

for t in trades:
    pnl = t['pnl'] or 0
    total_pnl += pnl
    outcome = t['outcome'] or 'PENDING'
    if outcome == 'WIN':
        wins += 1
        current_streak = 0
    elif outcome == 'LOSS':
        losses += 1
        current_streak += 1
        loss_streaks.append(current_streak)
    elif outcome == 'EARLY_EXIT':
        early_exits += 1
        current_streak = 0
    else:
        pending += 1

    hour = t['created_at'][:13]  # YYYY-MM-DD HH
    if hour not in hourly:
        hourly[hour] = {'trades': 0, 'pnl': 0, 'wins': 0, 'losses': 0, 'ee': 0}
    hourly[hour]['trades'] += 1
    hourly[hour]['pnl'] += pnl
    if outcome == 'WIN': hourly[hour]['wins'] += 1
    elif outcome == 'LOSS': hourly[hour]['losses'] += 1
    elif outcome == 'EARLY_EXIT': hourly[hour]['ee'] += 1

    tag = t['trade_tag'] or ''
    regime = t['regime_strength'] or 0
    conf = t['model_confidence'] or 0
    max_bid = t['max_bid_during_window'] or 0
    ee_thresh = t['exit_threshold_used'] or 0
    bet = t['amount_usdc'] or 0

    print(f"{t['created_at']} | {t['side']:>4} | entry={t['entry_price']:.3f} | {outcome:>10} | pnl={pnl:+.2f} | bet=${bet:.1f} | tag={tag:15} | regime={regime:+.2f} | conf={conf:.3f} | maxbid={max_bid:.3f} | ee={ee_thresh:.2f}")

print(f"\nSUMMARY: {len(trades)} trades | W:{wins} L:{losses} EE:{early_exits} Pending:{pending} | Total P&L: ${total_pnl:+.2f}")
if wins + losses > 0:
    print(f"Win rate (settled): {wins/(wins+losses)*100:.1f}%")
print(f"Max loss streak: {max(loss_streaks) if loss_streaks else 0}")

# === SECTION 2: Hourly breakdown ===
print("\n" + "=" * 120)
print("HOURLY BREAKDOWN")
print("=" * 120)
for hour in sorted(hourly):
    h = hourly[hour]
    wr = h['wins'] / (h['wins'] + h['losses']) * 100 if (h['wins'] + h['losses']) > 0 else 0
    print(f"{hour} | {h['trades']:2} trades | W:{h['wins']} L:{h['losses']} EE:{h['ee']} | P&L: ${h['pnl']:+.2f} | WR: {wr:.0f}%")

# === SECTION 3: By trade tag ===
print("\n" + "=" * 120)
print("BY TRADE TAG")
print("=" * 120)
tag_stats = conn.execute('''
    SELECT trade_tag, COUNT(*) as cnt,
           SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
           SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as ee,
           SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl,
           AVG(entry_price) as avg_entry
    FROM live_trades
    WHERE created_at >= '2026-03-04 18:00:00' AND outcome IS NOT NULL
    GROUP BY trade_tag
''').fetchall()
for t in tag_stats:
    wr = t['wins'] / (t['wins'] + t['losses']) * 100 if (t['wins'] + t['losses']) > 0 else 0
    print(f"  {t['trade_tag'] or 'normal':20} | {t['cnt']:3} trades | W:{t['wins']} L:{t['losses']} EE:{t['ee']} | P&L: ${t['total_pnl'] or 0:+.2f} | avg: ${t['avg_pnl'] or 0:+.2f} | WR: {wr:.0f}% | avg_entry: {t['avg_entry']:.3f}")

# === SECTION 4: By entry price tier ===
print("\n" + "=" * 120)
print("BY ENTRY PRICE TIER (overnight)")
print("=" * 120)
tiers = [
    ('< 0.35', 0, 0.35),
    ('0.35-0.40', 0.35, 0.40),
    ('0.40-0.50', 0.40, 0.50),
    ('>= 0.50', 0.50, 1.0),
]
for label, lo, hi in tiers:
    rows = conn.execute('''
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as ee,
               SUM(pnl) as total_pnl, AVG(amount_usdc) as avg_bet
        FROM live_trades
        WHERE created_at >= '2026-03-04 18:00:00' AND outcome IS NOT NULL
          AND entry_price >= ? AND entry_price < ?
    ''', (lo, hi)).fetchone()
    if rows['cnt'] > 0:
        wr = rows['wins'] / (rows['wins'] + rows['losses']) * 100 if (rows['wins'] + rows['losses']) > 0 else 0
        print(f"  {label:12} | {rows['cnt']:3} trades | W:{rows['wins']} L:{rows['losses']} EE:{rows['ee']} | P&L: ${rows['total_pnl'] or 0:+.2f} | avg_bet: ${rows['avg_bet'] or 0:.1f} | WR: {wr:.0f}%")

# === SECTION 5: Early exit performance ===
print("\n" + "=" * 120)
print("EARLY EXIT ANALYSIS (overnight)")
print("=" * 120)
ee_trades = conn.execute('''
    SELECT created_at, side, entry_price, pnl, max_bid_during_window, exit_threshold_used, amount_usdc, trade_tag
    FROM live_trades
    WHERE created_at >= '2026-03-04 18:00:00' AND outcome = 'EARLY_EXIT'
    ORDER BY created_at
''').fetchall()
for t in ee_trades:
    print(f"  {t['created_at']} | {t['side']:>4} | entry={t['entry_price']:.3f} | pnl={t['pnl']:+.2f} | maxbid={t['max_bid_during_window'] or 0:.3f} | ee_thresh={t['exit_threshold_used'] or 0:.2f} | bet=${t['amount_usdc'] or 0:.1f} | tag={t['trade_tag'] or ''}")
print(f"  Total EE trades: {len(ee_trades)}, EE P&L: ${sum(t['pnl'] or 0 for t in ee_trades):+.2f}")

# === SECTION 6: Regime state timeline ===
print("\n" + "=" * 120)
print("REGIME TIMELINE (overnight)")
print("=" * 120)
regime_trades = conn.execute('''
    SELECT created_at, side, regime_strength, trade_tag, outcome, pnl
    FROM live_trades
    WHERE created_at >= '2026-03-04 18:00:00'
    ORDER BY created_at
''').fetchall()
for t in regime_trades:
    rs = t['regime_strength'] or 0
    regime_label = 'TREND_UP' if rs > 0.3 else ('TREND_DN' if rs < -0.3 else 'RANGING')
    flip = ' ** FLIP **' if t['trade_tag'] and 'regime_flip' in (t['trade_tag'] or '') else ''
    print(f"  {t['created_at']} | {t['side']:>4} | regime={rs:+.3f} ({regime_label:10}){flip} | {t['outcome'] or 'PENDING':>10} | pnl={t['pnl'] or 0:+.2f}")

# === SECTION 7: Full day comparison ===
print("\n" + "=" * 120)
print("FULL DAY COMPARISON")
print("=" * 120)
for period_name, start, end in [
    ("Mar 4 daytime (00:00-18:00)", "2026-03-04 00:00:00", "2026-03-04 18:00:00"),
    ("Mar 4 evening (18:00-00:00)", "2026-03-04 18:00:00", "2026-03-05 00:00:00"),
    ("Mar 5 overnight (00:00-07:00)", "2026-03-05 00:00:00", "2026-03-05 07:00:00"),
]:
    row = conn.execute('''
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as ee,
               SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl,
               SUM(amount_usdc) as total_bet
        FROM live_trades
        WHERE created_at >= ? AND created_at < ? AND outcome IS NOT NULL
    ''', (start, end)).fetchone()
    if row['cnt'] > 0:
        wr = row['wins'] / (row['wins'] + row['losses']) * 100 if (row['wins'] + row['losses']) > 0 else 0
        print(f"  {period_name:35} | {row['cnt']:3} trades | W:{row['wins']} L:{row['losses']} EE:{row['ee']} | P&L: ${row['total_pnl'] or 0:+.2f} | avg: ${row['avg_pnl'] or 0:+.2f} | wagered: ${row['total_bet'] or 0:.0f} | WR: {wr:.0f}%")

# === SECTION 8: Skipped windows overnight ===
print("\n" + "=" * 120)
print("SKIPPED WINDOWS (overnight)")
print("=" * 120)
try:
    skips = conn.execute('''
        SELECT created_at, reason, btc_price, model_side, model_confidence, entry_price, regime_state
        FROM skipped_windows
        WHERE created_at >= '2026-03-04 18:00:00'
        ORDER BY created_at
    ''').fetchall()
    skip_reasons = {}
    for s in skips:
        r = s['reason']
        skip_reasons[r] = skip_reasons.get(r, 0) + 1
    print(f"  Total skipped: {len(skips)}")
    for r, cnt in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r}: {cnt}")
except Exception as e:
    print(f"  Error: {e}")

# === SECTION 9: All-time bankroll trajectory ===
print("\n" + "=" * 120)
print("BANKROLL TRAJECTORY (cumulative P&L, last 48h)")
print("=" * 120)
all_trades = conn.execute('''
    SELECT created_at, pnl, outcome, amount_usdc
    FROM live_trades
    WHERE created_at >= '2026-03-03 18:00:00' AND outcome IS NOT NULL
    ORDER BY created_at
''').fetchall()

# Get starting bankroll reference
cum_pnl = 0
for t in all_trades:
    cum_pnl += (t['pnl'] or 0)
    # Print every 10th trade or significant events
print(f"  Cumulative P&L over last 48h: ${cum_pnl:+.2f}")

# Print running total every hour
hourly_cum = {}
running = 0
for t in all_trades:
    running += (t['pnl'] or 0)
    hour = t['created_at'][:13]
    hourly_cum[hour] = running

for hour in sorted(hourly_cum):
    print(f"  {hour}: cumP&L = ${hourly_cum[hour]:+.2f}")

conn.close()
