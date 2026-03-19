"""Analyze: what happens to WINNERS when the stop-loss fires?
The stop doesn't know which trades are W or L. It fires on ALL."""

import sqlite3
from collections import defaultdict

conn = sqlite3.connect("btc_edge.db")
c = conn.cursor()

def ff(p): return 0.25 * (p * (1-p))**2

c.execute("""SELECT entry_price, outcome, pnl, amount_usdc, side, market_slug
             FROM live_trades WHERE outcome IN ('WIN','LOSS') AND entry_price IS NOT NULL ORDER BY id""")
all_wl = c.fetchall()

slugs = set(t[5] for t in all_wl if t[5])
snap_cache = {}
for slug in slugs:
    c.execute("SELECT timestamp, up_best_bid, down_best_bid, up_price, down_price FROM market_snapshots WHERE slug = ? ORDER BY timestamp", (slug,))
    snap_cache[slug] = c.fetchall()

print("=" * 75)
print("WHAT HAPPENS TO WINNERS WHEN THE STOP FIRES?")
print("=" * 75)

for model_name, threshold, is_trail in [
    ("Trail -5c", 0.05, True), ("Trail -10c", 0.10, True),
    ("Bid<50c", 0.50, False), ("Bid<40c", 0.40, False), ("Bid<30c", 0.30, False)
]:
    stopped_w = []
    stopped_l = []
    kept_w = 0
    kept_l = 0

    for entry, outcome, actual_pnl, amount, side, slug in all_wl:
        if not slug or not amount:
            if outcome == "WIN": kept_w += 1
            else: kept_l += 1
            continue
        snaps = snap_cache.get(slug, [])
        if len(snaps) < 5:
            if outcome == "WIN": kept_w += 1
            else: kept_l += 1
            continue

        t0 = snaps[0][0]
        sell_bid = None

        if is_trail:
            pk = entry
            for s in snaps:
                elapsed = s[0] - t0
                if elapsed < 30: continue
                bid = (s[1] if s[1] else (s[3] or 0)) if side == "UP" else (s[2] if s[2] else (s[4] or 0))
                if bid > pk: pk = bid
                if pk - bid >= threshold:
                    sell_bid = bid
                    break
        else:
            for s in snaps:
                elapsed = s[0] - t0
                if elapsed < 30: continue
                bid = (s[1] if s[1] else (s[3] or 0)) if side == "UP" else (s[2] if s[2] else (s[4] or 0))
                if bid < threshold:
                    sell_bid = bid
                    break

        if sell_bid is not None and sell_bid >= 0.005:
            new_pnl = (amount/entry)*(1-ff(entry))*sell_bid*(1-ff(sell_bid)) - amount
            item = {"actual": actual_pnl, "stop": new_pnl, "sell_bid": sell_bid, "entry": entry}
            if outcome == "WIN":
                stopped_w.append(item)
            else:
                stopped_l.append(item)
        else:
            if outcome == "WIN": kept_w += 1
            else: kept_l += 1

    total_stopped = len(stopped_w) + len(stopped_l)
    if total_stopped == 0: continue
    accuracy = len(stopped_l) / total_stopped * 100

    print(f"\n  {model_name}:")
    print(f"    Stop fired: {total_stopped} trades ({accuracy:.1f}% were actually losers)")
    print(f"    Not stopped: {kept_w}W + {kept_l}L")

    if stopped_w:
        sac_total = sum(w["actual"] - w["stop"] for w in stopped_w)
        avg_sell = sum(w["sell_bid"] for w in stopped_w) / len(stopped_w)
        print(f"    False alarms (winners stopped): {len(stopped_w)}")
        print(f"      Avg sell bid: {avg_sell:.3f}")
        print(f"      Would have won: ${sum(w['actual'] for w in stopped_w):+.2f}")
        print(f"      Got from stop:  ${sum(w['stop'] for w in stopped_w):+.2f}")
        print(f"      Sacrifice:      ${sac_total:.2f} (${sac_total/len(stopped_w):.2f}/trade)")

    if stopped_l:
        saved_total = sum(l["stop"] - l["actual"] for l in stopped_l)
        avg_sell_l = sum(l["sell_bid"] for l in stopped_l) / len(stopped_l)
        print(f"    Correct catches (losers stopped): {len(stopped_l)}")
        print(f"      Avg sell bid: {avg_sell_l:.3f}")
        print(f"      Would have lost: ${sum(l['actual'] for l in stopped_l):+.2f}")
        print(f"      Lost from stop:  ${sum(l['stop'] for l in stopped_l):+.2f}")
        print(f"      Saved:           ${saved_total:.2f} (${saved_total/len(stopped_l):.2f}/trade)")

    net = sum(l["stop"]-l["actual"] for l in stopped_l) - sum(w["actual"]-w["stop"] for w in stopped_w)
    print(f"    NET IMPACT: ${net:+.2f}")
    print(f"    Per stopped trade: ${net/total_stopped:+.2f}")

# PART 2: Winner vs Loser trajectories
print("\n" + "=" * 75)
print("WINNER vs LOSER BID TRAJECTORIES (normalized to entry)")
print("=" * 75)

win_delta = defaultdict(list)
loss_delta = defaultdict(list)

for entry, outcome, actual_pnl, amount, side, slug in all_wl[:600]:
    if not slug: continue
    snaps = snap_cache.get(slug, [])
    if len(snaps) < 5: continue
    t0 = snaps[0][0]
    for s in snaps:
        elapsed = s[0] - t0
        bid = (s[1] if s[1] else (s[3] or 0)) if side == "UP" else (s[2] if s[2] else (s[4] or 0))
        bucket = (elapsed // 30) * 30
        if bucket <= 300:
            delta = bid - entry  # how far from entry
            if outcome == "WIN": win_delta[bucket].append(delta)
            else: loss_delta[bucket].append(delta)

print(f"  {'Time':>5}  {'W: bid-entry':>12}  {'L: bid-entry':>12}  {'Gap':>6}  {'Can distinguish?':>17}")
for t in sorted(set(win_delta.keys()) & set(loss_delta.keys())):
    wd = sum(win_delta[t])/len(win_delta[t])
    ld = sum(loss_delta[t])/len(loss_delta[t])
    gap = wd - ld
    # What % of losers are below -5c from entry at this point?
    l_below_5c = sum(1 for d in loss_delta[t] if d < -0.05) / len(loss_delta[t]) * 100
    w_below_5c = sum(1 for d in win_delta[t] if d < -0.05) / len(win_delta[t]) * 100
    print(f"  {t:>4}s  {wd:>+11.3f}  {ld:>+11.3f}  {gap:>+5.3f}  W<-5c:{w_below_5c:.0f}% L<-5c:{l_below_5c:.0f}%")

# PART 3: The key question - when bid drops 5c from entry, what % are losers?
print("\n" + "=" * 75)
print("WHEN BID DROPS X FROM ENTRY, WHAT % ARE ACTUALLY LOSERS?")
print("This is the stop-loss 'accuracy' as a loser detector")
print("=" * 75)

for drop in [0.03, 0.05, 0.07, 0.10, 0.15, 0.20]:
    triggered_w = 0
    triggered_l = 0
    not_triggered_w = 0
    not_triggered_l = 0

    for entry, outcome, actual_pnl, amount, side, slug in all_wl:
        if not slug: continue
        snaps = snap_cache.get(slug, [])
        if len(snaps) < 5: continue
        t0 = snaps[0][0]
        triggered = False
        for s in snaps:
            elapsed = s[0] - t0
            if elapsed < 30: continue
            bid = (s[1] if s[1] else (s[3] or 0)) if side == "UP" else (s[2] if s[2] else (s[4] or 0))
            if bid <= entry - drop:
                triggered = True
                break

        if triggered:
            if outcome == "WIN": triggered_w += 1
            else: triggered_l += 1
        else:
            if outcome == "WIN": not_triggered_w += 1
            else: not_triggered_l += 1

    total_trig = triggered_w + triggered_l
    total_not = not_triggered_w + not_triggered_l
    if total_trig > 0 and total_not > 0:
        precision = triggered_l / total_trig * 100  # of those stopped, how many were losers
        recall = triggered_l / (triggered_l + not_triggered_l) * 100  # of all losers, how many caught
        false_pos = triggered_w / total_trig * 100
        wr_if_not_stopped = not_triggered_w / total_not * 100
        print(f"  Drop >= {drop:.2f}:")
        print(f"    Fires on: {total_trig} trades ({triggered_l}L + {triggered_w}W)")
        print(f"    Precision (% are losers): {precision:.1f}%")
        print(f"    False positive (% are winners): {false_pos:.1f}%")
        print(f"    Recall (% of all losers caught): {recall:.1f}%")
        print(f"    If NOT stopped: {total_not} trades, WR = {wr_if_not_stopped:.1f}%")
        print()

conn.close()
