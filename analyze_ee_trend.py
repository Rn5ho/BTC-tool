"""Analyze: Should EE be relaxed for regime_flip (trend-following) trades?"""
import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), 'btc_edge.db')
conn = sqlite3.connect(db_path)
c = conn.cursor()

def get_window_outcome(slug, side):
    """Reconstruct whether trade would have won using candle data."""
    parts = slug.split('-')
    window_start_ms = int(parts[-1]) * 1000
    window_end_ms = window_start_ms + 300_000

    c.execute('SELECT open FROM candles WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1', (window_start_ms,))
    start = c.fetchone()
    c.execute('SELECT close FROM candles WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1', (window_end_ms,))
    end = c.fetchone()

    if start and end:
        btc_up = end[0] > start[0]
        return btc_up if side == "UP" else not btc_up, start[0], end[0]
    return None, None, None


# ── Get all regime_flip trades ──
c.execute('''
    SELECT id, market_slug, side, outcome, pnl, entry_price, fill_price,
           max_bid_during_window, exit_threshold_used, created_at, regime_strength
    FROM live_trades WHERE trade_tag='regime_flip' ORDER BY created_at
''')
all_flips = c.fetchall()

# ── Get all normal taker trades (non-flip, non-maker, non-exploration) ──
c.execute('''
    SELECT id, market_slug, side, outcome, pnl, entry_price, fill_price,
           max_bid_during_window, exit_threshold_used, created_at, regime_strength
    FROM live_trades WHERE (trade_tag IS NULL) AND outcome IS NOT NULL ORDER BY created_at
''')
all_normal = c.fetchall()


def analyze_ee_group(trades, label):
    """Analyze early exits for a group of trades."""
    ee_trades = [t for t in trades if t[3] == 'EARLY_EXIT']
    settled = [t for t in trades if t[3] in ('WIN', 'LOSS')]

    if not ee_trades:
        print(f"\n=== {label}: No early exits ===")
        return

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"\nTotal trades: {len(trades)} ({len(ee_trades)} EE, {len(settled)} settled)\n")

    # EE detail table
    would_won = 0
    would_lost = 0
    ee_pnl = 0
    missed_profit = 0
    saved_loss = 0

    print(f"{'ID':>4} {'Side':>4} {'Entry':>6} {'PnL':>7} {'MaxBid':>7} {'Thr':>5} {'BTC Move':>10} {'Won?':>4} {'Opp.Cost':>8}")
    print("-" * 70)

    for t in ee_trades:
        tid, slug, side, outcome, pnl, entry, fill, max_bid, threshold, created, strength = t
        fill = fill or entry
        would_win, btc_o, btc_c = get_window_outcome(slug, side)
        ee_pnl += pnl

        if btc_o and btc_c:
            move_pct = (btc_c - btc_o) / btc_o * 100
            move_str = f"{move_pct:+.3f}%"
        else:
            move_str = "???"

        if would_win is True:
            would_won += 1
            tokens = round(5.0 / fill, 2)
            settle_pnl = round(tokens * 1.0 - 5.0, 2)
            opp_cost = round(settle_pnl - pnl, 2)
            missed_profit += opp_cost
            win_str = "YES"
        elif would_win is False:
            would_lost += 1
            save = round(pnl + 5.0, 2)
            saved_loss += save
            opp_cost = 0
            win_str = "NO"
        else:
            opp_cost = 0
            win_str = "???"

        print(f"{tid:>4} {side:>4} {entry:>6.3f} {pnl:>7.2f} {max_bid or 0:>7.3f} {threshold or 0:>5.2f} {move_str:>10} {win_str:>4} {opp_cost:>+8.2f}")

    known = would_won + would_lost
    wr = would_won / known * 100 if known else 0

    print(f"\nEE Summary:")
    print(f"  Would-have-won at settlement: {would_won}/{known} ({wr:.0f}%)")
    print(f"  Actual EE P&L collected:      ${ee_pnl:>8.2f}")
    print(f"  Missed profit (regret):       ${missed_profit:>8.2f}")
    print(f"  Saved from loss (rescue):     ${saved_loss:>8.2f}")

    # ── Hypothetical: no EE ──
    hypo_pnl = 0
    for t in ee_trades:
        tid, slug, side, outcome, pnl, entry, fill, max_bid, threshold, created, strength = t
        fill = fill or entry
        would_win, _, _ = get_window_outcome(slug, side)
        if would_win is True:
            tokens = round(5.0 / fill, 2)
            hypo_pnl += round(tokens * 1.0 - 5.0, 2)
        elif would_win is False:
            hypo_pnl += -5.0

    settled_pnl = sum(t[4] for t in settled if t[4])
    total_actual = ee_pnl + settled_pnl
    total_hypo = hypo_pnl + settled_pnl

    print(f"\n  Actual total P&L (EE + settled):    ${total_actual:>8.2f}")
    print(f"  Hypo total P&L (no EE, all settle): ${total_hypo:>8.2f}")
    print(f"  EE net value (+ = EE helps):        ${total_actual - total_hypo:>+8.2f}")

    # ── By entry price tier ──
    print(f"\n  By entry price tier:")
    tiers = [("< 0.35", 0, 0.35), ("0.35-0.40", 0.35, 0.40), ("0.40-0.50", 0.40, 0.50), (">= 0.50", 0.50, 1.0)]
    for label_t, lo, hi in tiers:
        tier = [t for t in ee_trades if lo <= (t[6] or t[5]) < hi]
        if not tier:
            continue
        t_won = t_lost = t_ee = t_hypo = 0
        for t in tier:
            tid, slug, side, outcome, pnl, entry, fill, max_bid, threshold, created, strength = t
            fill = fill or entry
            would_win, _, _ = get_window_outcome(slug, side)
            t_ee += pnl
            if would_win is True:
                t_won += 1
                tokens = round(5.0 / fill, 2)
                t_hypo += round(tokens * 1.0 - 5.0, 2)
            elif would_win is False:
                t_lost += 1
                t_hypo += -5.0
        t_wr = t_won / (t_won + t_lost) * 100 if (t_won + t_lost) else 0
        print(f"    {label_t}: {len(tier)} trades, WR={t_wr:.0f}% ({t_won}W/{t_lost}L), EE=${t_ee:.2f}, Hypo=${t_hypo:.2f}, Impact=${t_ee - t_hypo:+.2f}")


# ── Run analysis for both groups ──
analyze_ee_group(all_flips, "REGIME FLIP (trend-following) trades")
analyze_ee_group(all_normal, "NORMAL (mean-reversion) trades")

# ── Cross-comparison ──
print(f"\n{'='*70}")
print(f"  CROSS-COMPARISON: Should EE be different for flip trades?")
print(f"{'='*70}\n")

for label, trades in [("Regime Flip", all_flips), ("Normal", all_normal)]:
    ee = [t for t in trades if t[3] == 'EARLY_EXIT']
    won_count = lost_count = 0
    for t in ee:
        would_win, _, _ = get_window_outcome(t[1], t[2])
        if would_win is True:
            won_count += 1
        elif would_win is False:
            lost_count += 1
    total_k = won_count + lost_count
    wr = won_count / total_k * 100 if total_k else 0

    settled = [t for t in trades if t[3] in ('WIN', 'LOSS')]
    s_wins = len([t for t in settled if t[3] == 'WIN'])
    s_total = len(settled)
    s_wr = s_wins / s_total * 100 if s_total else 0

    print(f"  {label}:")
    print(f"    Settlement WR (trades that settled): {s_wins}/{s_total} ({s_wr:.0f}%)")
    print(f"    EE trades that WOULD have won:       {won_count}/{total_k} ({wr:.0f}%)")
    print(f"    => EE is cutting {won_count} winners (regret) and saving {lost_count} losers (rescue)")
    print()

# ── Key question: by max_bid level ──
print(f"\n{'='*70}")
print(f"  MAX BID REACHED: How high do flip-EE trades go?")
print(f"{'='*70}\n")

for label, trades in [("Regime Flip", all_flips), ("Normal", all_normal)]:
    ee = [t for t in trades if t[3] == 'EARLY_EXIT' and t[7] and t[7] > 0]
    if not ee:
        print(f"  {label}: no max_bid data")
        continue

    print(f"  {label} ({len(ee)} EE trades with max_bid data):")
    for t in sorted(ee, key=lambda x: x[7], reverse=True):
        tid, slug, side, outcome, pnl, entry, fill, max_bid, threshold, created, strength = t
        fill = fill or entry
        would_win, _, _ = get_window_outcome(slug, side)
        wo = "WON" if would_win else "LOST" if would_win is False else "???"
        tokens = round(5.0 / fill, 2)
        settle_pnl = round(tokens * 1.0 - 5.0, 2) if would_win else -5.0
        print(f"    ID={tid} entry={entry:.3f} maxBid={max_bid:.3f} thresh={threshold:.2f} eePnL=${pnl:.2f} settlePnL=${settle_pnl:.2f} {wo}")
    print()

conn.close()
