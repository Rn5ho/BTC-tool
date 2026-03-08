"""Deep overnight analysis - maker fills, hour 2, regime flip, trend filter"""
import sqlite3

conn = sqlite3.connect('btc_edge.db')
conn.row_factory = sqlite3.Row

# =====================================================================
# 1. MAKER FILL ANALYSIS - How long do they sit? What fills them?
# =====================================================================
print("=" * 100)
print("1. MAKER FILL LIFECYCLE - How long do orders sit before filling?")
print("=" * 100)

# Maker fills have created_at = settlement time (when sync discovers them)
# But we can look at the market_slug to figure out the window they were placed in
makers = conn.execute('''
    SELECT id, created_at, market_slug, side, entry_price, outcome, pnl, amount_usdc
    FROM live_trades
    WHERE trade_tag = 'maker_fill'
    ORDER BY created_at DESC
    LIMIT 50
''').fetchall()

print(f"Last 50 maker fills:")
maker_pnl_total = 0
maker_w = maker_l = 0
for m in makers:
    pnl = m['pnl'] or 0
    maker_pnl_total += pnl
    if m['outcome'] == 'WIN': maker_w += 1
    elif m['outcome'] == 'LOSS': maker_l += 1
    # The slug contains the window time info
    slug = m['market_slug'] or ''
    print(f"  {m['created_at']} | {m['side']:>4} | entry={m['entry_price']:.3f} | {m['outcome']:>5} | pnl={pnl:+.2f} | ${m['amount_usdc'] or 0:.1f} | slug={slug[-30:]}")

print(f"\nMaker fill ALL-TIME stats:")
all_makers = conn.execute('''
    SELECT COUNT(*) as cnt,
           SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
           SUM(pnl) as total_pnl,
           AVG(pnl) as avg_pnl,
           AVG(entry_price) as avg_entry,
           SUM(amount_usdc) as total_wagered
    FROM live_trades
    WHERE trade_tag = 'maker_fill' AND outcome IS NOT NULL
''').fetchone()
wr = all_makers['wins'] / (all_makers['wins'] + all_makers['losses']) * 100 if (all_makers['wins'] + all_makers['losses']) > 0 else 0
print(f"  Total: {all_makers['cnt']} trades | W:{all_makers['wins']} L:{all_makers['losses']} | P&L: ${all_makers['total_pnl'] or 0:+.2f} | avg: ${all_makers['avg_pnl'] or 0:+.2f} | WR: {wr:.0f}%")
print(f"  Avg entry: {all_makers['avg_entry']:.3f} | Total wagered: ${all_makers['total_wagered'] or 0:.0f}")

# Maker fills by entry price
print(f"\nMaker fills by entry price tier (all-time):")
for label, lo, hi in [('<0.40', 0, 0.40), ('0.40-0.50', 0.40, 0.50), ('0.50-0.60', 0.50, 0.60), ('0.60-0.80', 0.60, 0.80), ('>=0.80', 0.80, 1.0)]:
    r = conn.execute('''
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
               SUM(pnl) as total_pnl, AVG(entry_price) as avg_ep
        FROM live_trades
        WHERE trade_tag = 'maker_fill' AND outcome IS NOT NULL
          AND entry_price >= ? AND entry_price < ?
    ''', (lo, hi)).fetchone()
    if r['cnt'] > 0:
        wr2 = r['wins'] / (r['wins'] + r['losses']) * 100 if (r['wins'] + r['losses']) > 0 else 0
        print(f"  {label:12} | {r['cnt']:3} trades | W:{r['wins']} L:{r['losses']} | P&L: ${r['total_pnl'] or 0:+.2f} | WR: {wr2:.0f}% | avg_entry: {r['avg_ep']:.3f}")

# Maker fills - both sides in same window?
print(f"\nMaker fills: windows with BOTH UP and DOWN fills:")
# Group by created_at (settlement batch time)
batches = conn.execute('''
    SELECT created_at,
           GROUP_CONCAT(side) as sides,
           GROUP_CONCAT(outcome) as outcomes,
           GROUP_CONCAT(printf("%.2f", pnl)) as pnls,
           SUM(pnl) as batch_pnl,
           COUNT(*) as cnt
    FROM live_trades
    WHERE trade_tag = 'maker_fill' AND outcome IS NOT NULL
    GROUP BY created_at
    HAVING COUNT(DISTINCT side) > 1
    ORDER BY created_at DESC
    LIMIT 20
''').fetchall()
for b in batches:
    print(f"  {b['created_at']} | {b['cnt']} fills | sides: {b['sides']} | outcomes: {b['outcomes']} | pnls: {b['pnls']} | batch: ${b['batch_pnl']:+.2f}")

# =====================================================================
# 2. HOUR 2 (02:00 UTC) IMPACT - Was unblocking it a mistake?
# =====================================================================
print("\n" + "=" * 100)
print("2. HOUR-BY-HOUR ANALYSIS (ALL-TIME vs OVERNIGHT)")
print("=" * 100)

# All-time by hour
print("\nAll-time performance by UTC hour:")
for h in range(24):
    r = conn.execute('''
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as ee,
               SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl
        FROM live_trades
        WHERE CAST(strftime('%H', created_at) AS INTEGER) = ? AND outcome IS NOT NULL
    ''', (h,)).fetchone()
    if r['cnt'] > 0:
        wr = r['wins'] / (r['wins'] + r['losses']) * 100 if (r['wins'] + r['losses']) > 0 else 0
        marker = " *** WORST ***" if r['total_pnl'] and r['total_pnl'] < -10 else ""
        print(f"  {h:02d}:00 | {r['cnt']:3} trades | W:{r['wins']:2} L:{r['losses']:2} EE:{r['ee']:2} | P&L: ${r['total_pnl'] or 0:+7.2f} | avg: ${r['avg_pnl'] or 0:+.2f} | WR: {wr:.0f}%{marker}")

# Hour 2 specifically - what happened last night
print("\nHour 02:00 UTC breakdown (last night only):")
h2 = conn.execute('''
    SELECT created_at, side, entry_price, outcome, pnl, trade_tag, regime_strength, amount_usdc
    FROM live_trades
    WHERE created_at >= '2026-03-05 02:00:00' AND created_at < '2026-03-05 03:00:00'
    ORDER BY created_at
''').fetchall()
for t in h2:
    print(f"  {t['created_at']} | {t['side']:>4} | entry={t['entry_price']:.3f} | {t['outcome'] or 'PEND':>10} | pnl={t['pnl'] or 0:+.2f} | ${t['amount_usdc'] or 0:.1f} | tag={t['trade_tag'] or ''} | regime={t['regime_strength'] or 0:+.2f}")

# =====================================================================
# 3. REGIME FLIP: 1-window vs 3-window confirmation impact
# =====================================================================
print("\n" + "=" * 100)
print("3. REGIME FLIP ANALYSIS - Would 1-window have helped?")
print("=" * 100)

# Look at trades where regime was trending but NO flip happened (because 3-window conf not met)
# These are trades where regime_strength > 0.35 but trade_tag is NOT regime_flip
print("\nTrades in TRENDING regime that were NOT flipped (overnight):")
non_flipped = conn.execute('''
    SELECT created_at, side, entry_price, outcome, pnl, regime_strength, trade_tag, amount_usdc
    FROM live_trades
    WHERE created_at >= '2026-03-04 18:00:00'
      AND ABS(regime_strength) >= 0.30
      AND (trade_tag IS NULL OR trade_tag = '' OR trade_tag NOT LIKE '%regime_flip%')
      AND trade_tag != 'maker_fill'
      AND outcome IS NOT NULL
    ORDER BY created_at
''').fetchall()

would_flip_pnl = 0
for t in non_flipped:
    rs = t['regime_strength'] or 0
    side = t['side']
    # Would flip have changed the outcome?
    # If regime is negative (trend down) and we bet UP -> flip would bet DOWN
    # If regime is positive (trend up) and we bet DOWN -> already aligned?
    trending_dir = 'UP' if rs > 0 else 'DOWN'
    aligned = (side == trending_dir)
    flip_side = trending_dir if not aligned else side
    conflict = "AGAINST TREND" if not aligned else "WITH TREND"
    pnl = t['pnl'] or 0

    print(f"  {t['created_at']} | {side:>4} | regime={rs:+.3f} | trend={trending_dir} | {conflict:15} | {t['outcome']:>10} | pnl={pnl:+.2f}")
    if not aligned:
        would_flip_pnl += pnl

print(f"\n  Against-trend trades (would have been flipped with 1-window): {sum(1 for t in non_flipped if (t['side'] == 'UP' and (t['regime_strength'] or 0) < -0.30) or (t['side'] == 'DOWN' and (t['regime_strength'] or 0) > 0.30))}")
print(f"  Their P&L: ${would_flip_pnl:+.2f}")
print(f"  (If flipped, we'd expect opposite outcomes — losses become potential wins)")

# Also look at regime_flip trades that LOST - were they real trends or flickering?
print("\nRegime flip trades that LOST (overnight) - regime strength at entry:")
flip_losses = conn.execute('''
    SELECT created_at, side, entry_price, outcome, pnl, model_confidence, regime_strength
    FROM live_trades
    WHERE created_at >= '2026-03-04 18:00:00'
      AND trade_tag = 'regime_flip' AND outcome = 'LOSS'
    ORDER BY created_at
''').fetchall()
for f in flip_losses:
    print(f"  {f['created_at']} | {f['side']:>4} | entry={f['entry_price']:.3f} | pnl={f['pnl'] or 0:+.2f} | conf={f['model_confidence'] or 0:.3f} | regime={f['regime_strength'] or 0:+.3f}")

# =====================================================================
# 4. TREND FILTER ANALYSIS - Normal trades during strong trends
# =====================================================================
print("\n" + "=" * 100)
print("4. TREND FILTER: Normal trades betting AGAINST strong trends")
print("=" * 100)

against_trend = conn.execute('''
    SELECT created_at, side, entry_price, outcome, pnl, regime_strength, trade_tag, amount_usdc
    FROM live_trades
    WHERE created_at >= '2026-03-04 18:00:00'
      AND outcome IS NOT NULL
      AND trade_tag NOT LIKE '%maker_fill%'
      AND (
        (side = 'UP' AND regime_strength < -0.30)
        OR (side = 'DOWN' AND regime_strength > 0.30)
      )
    ORDER BY created_at
''').fetchall()

print(f"\nNormal trades betting AGAINST trend (|regime| > 0.30):")
at_pnl = 0
for t in against_trend:
    pnl = t['pnl'] or 0
    at_pnl += pnl
    tag = t['trade_tag'] or 'normal'
    print(f"  {t['created_at']} | {t['side']:>4} | regime={t['regime_strength'] or 0:+.3f} | {t['outcome']:>10} | pnl={pnl:+.2f} | tag={tag}")
print(f"\n  Total against-trend: {len(against_trend)} trades | P&L: ${at_pnl:+.2f}")

# Compare: all taker trades WITH trend vs AGAINST trend (all-time)
print("\n\nALL-TIME: Taker trades WITH trend vs AGAINST trend (|regime| > 0.30):")
for label, condition in [
    ("WITH trend", "(side='UP' AND regime_strength > 0.30) OR (side='DOWN' AND regime_strength < -0.30)"),
    ("AGAINST trend", "(side='UP' AND regime_strength < -0.30) OR (side='DOWN' AND regime_strength > 0.30)"),
    ("RANGING", "ABS(regime_strength) < 0.30"),
]:
    r = conn.execute(f'''
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END) as ee,
               SUM(pnl) as total_pnl
        FROM live_trades
        WHERE outcome IS NOT NULL AND trade_tag != 'maker_fill'
          AND ({condition})
    ''').fetchone()
    if r['cnt'] > 0:
        wr = r['wins'] / (r['wins'] + r['losses']) * 100 if (r['wins'] + r['losses']) > 0 else 0
        print(f"  {label:15} | {r['cnt']:3} trades | W:{r['wins']} L:{r['losses']} EE:{r['ee']} | P&L: ${r['total_pnl'] or 0:+.2f} | WR: {wr:.0f}%")

conn.close()
