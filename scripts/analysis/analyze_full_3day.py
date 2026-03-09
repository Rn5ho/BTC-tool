"""Full 3-day analysis: early exit impact, trend damage, flip hypothetical."""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ================================================================
    # ALL live trades ever (should be ~3 days)
    # ================================================================
    cursor = conn.execute(
        "SELECT id, timestamp, market_slug, side, amount_usdc, entry_price, "
        "outcome, pnl, settled_at, trade_tag, regime_state, regime_strength "
        "FROM live_trades WHERE success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp"
    )
    trades = [dict(t) for t in cursor.fetchall()]

    first_ts = trades[0]["timestamp"] / 1000
    last_ts = trades[-1]["timestamp"] / 1000
    span_hours = (last_ts - first_ts) / 3600

    print("=" * 80)
    print("FULL 3-DAY LIVE TRADING ANALYSIS")
    print("=" * 80)
    print(f"Period: {datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} "
          f"-> {datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Span: {span_hours:.0f} hours ({span_hours/24:.1f} days)")
    print(f"Total trades: {len(trades)}")
    print()

    # ================================================================
    # SECTION 1: Overall performance
    # ================================================================
    print("=" * 80)
    print("1. OVERALL PERFORMANCE")
    print("=" * 80)

    by_outcome = defaultdict(list)
    for t in trades:
        by_outcome[t["outcome"]].append(t)

    for o in ["WIN", "LOSS", "EARLY_EXIT"]:
        tlist = by_outcome.get(o, [])
        if not tlist:
            continue
        pnl = sum(t["pnl"] or 0 for t in tlist)
        avg_entry = sum(t["entry_price"] or 0 for t in tlist) / len(tlist)
        vol = sum(t["amount_usdc"] for t in tlist)
        print(f"  {o:12s}: {len(tlist):4d} trades | P&L: ${pnl:+8.2f} | "
              f"Avg entry: {avg_entry:.3f} | Volume: ${vol:.2f}")

    total_pnl = sum(t["pnl"] or 0 for t in trades)
    total_vol = sum(t["amount_usdc"] for t in trades)
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    exits = sum(1 for t in trades if t["outcome"] == "EARLY_EXIT")

    print(f"\n  Total P&L: ${total_pnl:+.2f} | Volume: ${total_vol:.2f} | "
          f"ROI: {total_pnl/total_vol*100:.1f}%")
    print(f"  Settlement WR: {wins}/{wins+losses} = {wins/(wins+losses)*100:.1f}%")
    print(f"  Effective WR (exit=win): {wins+exits}/{len(trades)} = "
          f"{(wins+exits)/len(trades)*100:.1f}%")

    # ================================================================
    # SECTION 2: Early exit impact
    # ================================================================
    print()
    print("=" * 80)
    print("2. EARLY EXIT IMPACT")
    print("=" * 80)

    # Paper trades for resolution lookup
    paper_cursor = conn.execute(
        "SELECT market_slug, side, outcome FROM paper_trades WHERE outcome IS NOT NULL"
    )
    paper_map = {}
    for p in paper_cursor:
        paper_map[p["market_slug"]] = dict(p)

    early_exits = by_outcome.get("EARLY_EXIT", [])
    rescued = []
    regretted = []
    unknown = []

    for t in early_exits:
        paper = paper_map.get(t["market_slug"])
        if not paper:
            unknown.append(t)
            continue

        if paper["side"] == t["side"]:
            hyp_outcome = paper["outcome"]
        else:
            hyp_outcome = "WIN" if paper["outcome"] == "LOSS" else "LOSS"

        entry = t["entry_price"]
        size = t["amount_usdc"]
        if hyp_outcome == "WIN":
            tokens = size / entry
            gross = tokens * 1.0 - size
            hyp_pnl = gross * 0.98
        else:
            hyp_pnl = -size

        t["hyp_outcome"] = hyp_outcome
        t["hyp_pnl"] = hyp_pnl

        if hyp_outcome == "LOSS":
            rescued.append(t)
        else:
            regretted.append(t)

    known = rescued + regretted
    actual_exit_pnl = sum(t["pnl"] for t in known)
    hyp_exit_pnl = sum(t["hyp_pnl"] for t in known)
    delta = actual_exit_pnl - hyp_exit_pnl

    print(f"\n  Early exits: {len(early_exits)} total ({len(known)} matched, {len(unknown)} unknown)")
    print(f"  Rescued (would have lost):  {len(rescued)} ({len(rescued)/len(known)*100:.0f}%)")
    print(f"  Regretted (would have won): {len(regretted)} ({len(regretted)/len(known)*100:.0f}%)")
    print()
    print(f"  Actual early exit P&L:    ${actual_exit_pnl:+.2f}")
    print(f"  Hypothetical (hold all):  ${hyp_exit_pnl:+.2f}")
    print(f"  Net benefit of early exit: ${delta:+.2f}")
    print()

    if rescued:
        avg_saved = sum(t["pnl"] - t["hyp_pnl"] for t in rescued) / len(rescued)
        print(f"  Avg saved per rescue:  ${avg_saved:+.2f}")
    if regretted:
        avg_missed = sum(t["hyp_pnl"] - t["pnl"] for t in regretted) / len(regretted)
        print(f"  Avg missed per regret: ${avg_missed:+.2f}")
    if rescued and regretted:
        print(f"  Asymmetry ratio: {avg_saved/avg_missed:.1f}:1")

    # P&L without early exit at all
    other_pnl = sum(t["pnl"] or 0 for t in trades if t["outcome"] in ("WIN", "LOSS"))
    print(f"\n  System P&L with early exit:    ${total_pnl:+.2f}")
    print(f"  System P&L without early exit: ${other_pnl + hyp_exit_pnl:+.2f}")
    print(f"  Early exit total value:        ${delta:+.2f}")
    print(f"  Per day:                       ${delta / (span_hours/24):+.2f}/day")

    # By entry tier
    print(f"\n  By entry price tier:")
    tiers = [
        ("< 0.35 (exit @ 0.60)", lambda e: e < 0.35),
        ("0.35-0.50 (exit @ 0.65)", lambda e: 0.35 <= e < 0.50),
        (">= 0.50 (exit @ 0.95)", lambda e: e >= 0.50),
    ]
    for name, filt in tiers:
        tier = [t for t in known if filt(t["entry_price"])]
        if not tier:
            print(f"    {name}: no trades")
            continue
        r = sum(1 for t in tier if t["hyp_outcome"] == "LOSS")
        g = sum(1 for t in tier if t["hyp_outcome"] == "WIN")
        a_pnl = sum(t["pnl"] for t in tier)
        h_pnl = sum(t["hyp_pnl"] for t in tier)
        print(f"    {name}: {len(tier)} exits | {r} rescued / {g} regretted | "
              f"delta: ${a_pnl - h_pnl:+.2f}")

    # ================================================================
    # SECTION 3: Regime / trend analysis
    # ================================================================
    print()
    print("=" * 80)
    print("3. REGIME / TREND ANALYSIS")
    print("=" * 80)

    by_regime = defaultdict(list)
    for t in trades:
        by_regime[t["regime_state"] or "unknown"].append(t)

    for regime in sorted(by_regime.keys()):
        tlist = by_regime[regime]
        w = sum(1 for t in tlist if t["outcome"] == "WIN")
        l = sum(1 for t in tlist if t["outcome"] == "LOSS")
        e = sum(1 for t in tlist if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in tlist)
        settled = w + l
        wr = w / settled * 100 if settled else 0
        eff = (w + e) / (w + l + e) * 100 if (w + l + e) else 0
        print(f"  {regime:15s}: {len(tlist):3d} trades | {w}W/{l}L/{e}E | "
              f"Settle WR: {wr:.0f}% | Eff WR: {eff:.0f}% | P&L: ${pnl:+.2f}")

    # Alignment breakdown
    print()
    for t in trades:
        regime = t["regime_state"] or "unknown"
        if regime == "trending_up":
            t["alignment"] = "aligned" if t["side"] == "UP" else "conflict"
        elif regime == "trending_down":
            t["alignment"] = "aligned" if t["side"] == "DOWN" else "conflict"
        else:
            t["alignment"] = "ranging"

    print("  Trend alignment:")
    for alignment in ["ranging", "aligned", "conflict"]:
        group = [t for t in trades if t.get("alignment") == alignment]
        if not group:
            continue
        w = sum(1 for t in group if t["outcome"] == "WIN")
        l = sum(1 for t in group if t["outcome"] == "LOSS")
        e = sum(1 for t in group if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in group)
        settled = w + l
        wr = w / settled * 100 if settled else 0
        print(f"    {alignment:10s}: {len(group):3d} trades | {w}W/{l}L/{e}E | "
              f"WR: {wr:.0f}% | P&L: ${pnl:+.2f}")

    # ================================================================
    # SECTION 4: Flip hypothetical at various thresholds
    # ================================================================
    print()
    print("=" * 80)
    print("4. FLIP HYPOTHETICAL (all 3 days)")
    print("=" * 80)

    against = [t for t in trades if t.get("alignment") == "conflict"]

    for threshold in [0.30, 0.35, 0.40, 0.50]:
        subset = [t for t in against if abs(t["regime_strength"] or 0) >= threshold]
        if not subset:
            continue

        actual_pnl_t = 0.0
        flipped_pnl = 0.0
        f_wins = 0
        f_losses = 0
        f_unknown = 0

        for t in subset:
            actual_pnl_t += t["pnl"] or 0
            paper = paper_map.get(t["market_slug"])
            if not paper:
                f_unknown += 1
                continue

            if paper["outcome"] == "WIN":
                market_went = paper["side"]
            else:
                market_went = "DOWN" if paper["side"] == "UP" else "UP"

            trend_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
            trend_entry = 1.0 - t["entry_price"]

            if market_went == trend_side:
                bet = t["amount_usdc"]
                tokens = bet / trend_entry
                gross = tokens * 1.0 - bet
                flipped_pnl += gross * 0.98
                f_wins += 1
            else:
                flipped_pnl += -t["amount_usdc"]
                f_losses += 1

        total_f = f_wins + f_losses
        wr = f_wins / total_f * 100 if total_f else 0

        print(f"\n  Strength >= {threshold:.2f}: {len(subset)} trades")
        print(f"    Actual (against trend):  ${actual_pnl_t:+.2f}")
        print(f"    Flipped (with trend):    ${flipped_pnl:+.2f} ({f_wins}W/{f_losses}L, {wr:.0f}% WR)")
        print(f"    Skip:                    $0.00")
        print(f"    Flip improvement:        ${flipped_pnl - actual_pnl_t:+.2f}")
        if f_unknown:
            print(f"    (unknown resolution: {f_unknown})")

    # ================================================================
    # SECTION 5: Combined scenario projection
    # ================================================================
    print()
    print("=" * 80)
    print("5. COMBINED SCENARIO (early exit + regime flip)")
    print("=" * 80)

    non_conflict = [t for t in trades if t.get("alignment") != "conflict"]
    non_conflict_pnl = sum(t["pnl"] or 0 for t in non_conflict)

    # Flip at 0.35
    flip_35 = [t for t in against if abs(t["regime_strength"] or 0) >= 0.35]
    below_35 = [t for t in against if abs(t["regime_strength"] or 0) < 0.35]

    flip_pnl = 0.0
    f_w = 0
    f_l = 0
    for t in flip_35:
        paper = paper_map.get(t["market_slug"])
        if not paper:
            continue
        if paper["outcome"] == "WIN":
            market_went = paper["side"]
        else:
            market_went = "DOWN" if paper["side"] == "UP" else "UP"
        trend_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
        trend_entry = 1.0 - t["entry_price"]
        if market_went == trend_side:
            bet = t["amount_usdc"]
            tokens = bet / trend_entry
            gross = tokens * 1.0 - bet
            flip_pnl += gross * 0.98
            f_w += 1
        else:
            flip_pnl += -t["amount_usdc"]
            f_l += 1

    below_pnl = sum(t["pnl"] or 0 for t in below_35)

    print(f"\n  Non-conflict trades:     ${non_conflict_pnl:+.2f} ({len(non_conflict)} trades)")
    print(f"  Flipped (>= 0.35):       ${flip_pnl:+.2f} ({len(flip_35)} trades, {f_w}W/{f_l}L)")
    print(f"  Weak trend kept (<0.35): ${below_pnl:+.2f} ({len(below_35)} trades)")
    combo = non_conflict_pnl + flip_pnl + below_pnl
    print(f"\n  ACTUAL total:            ${total_pnl:+.2f}")
    print(f"  PROJECTED (flip >= .35): ${combo:+.2f}")
    print(f"  Improvement:             ${combo - total_pnl:+.2f}")
    print(f"  Per day:                 ${(combo - total_pnl) / (span_hours/24):+.2f}/day improvement")
    print(f"  Projected daily P&L:     ${combo / (span_hours/24):+.2f}/day")

    # ================================================================
    # SECTION 6: Day-by-day breakdown
    # ================================================================
    print()
    print("=" * 80)
    print("6. DAY-BY-DAY BREAKDOWN")
    print("=" * 80)

    by_day = defaultdict(list)
    for t in trades:
        day = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append(t)

    for day in sorted(by_day.keys()):
        tlist = by_day[day]
        w = sum(1 for t in tlist if t["outcome"] == "WIN")
        l = sum(1 for t in tlist if t["outcome"] == "LOSS")
        e = sum(1 for t in tlist if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in tlist)
        settled = w + l
        wr = w / settled * 100 if settled else 0
        eff = (w + e) / (w + l + e) * 100 if (w + l + e) else 0

        # Against-trend damage this day
        conflict_day = [t for t in tlist if t.get("alignment") == "conflict"]
        conflict_pnl = sum(t["pnl"] or 0 for t in conflict_day)

        # Early exit value this day
        exits_day = [t for t in tlist if t["outcome"] == "EARLY_EXIT"]
        exit_pnl = sum(t["pnl"] or 0 for t in exits_day)

        print(f"\n  {day}:")
        print(f"    Trades: {len(tlist)} ({w}W/{l}L/{e}E) | WR: {wr:.0f}% | Eff: {eff:.0f}%")
        print(f"    P&L: ${pnl:+.2f} | Exit value: ${exit_pnl:+.2f} | "
              f"Trend damage: ${conflict_pnl:+.2f} ({len(conflict_day)} trades)")

    # ================================================================
    # SECTION 7: Hourly heatmap
    # ================================================================
    print()
    print("=" * 80)
    print("7. HOURLY P&L HEATMAP (all 3 days)")
    print("=" * 80)

    by_hour = defaultdict(lambda: {"w": 0, "l": 0, "e": 0, "pnl": 0.0, "n": 0,
                                    "conflict_pnl": 0.0, "conflict_n": 0})
    for t in trades:
        h = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).hour
        by_hour[h]["n"] += 1
        by_hour[h]["pnl"] += t["pnl"] or 0
        if t["outcome"] == "WIN":
            by_hour[h]["w"] += 1
        elif t["outcome"] == "LOSS":
            by_hour[h]["l"] += 1
        elif t["outcome"] == "EARLY_EXIT":
            by_hour[h]["e"] += 1
        if t.get("alignment") == "conflict":
            by_hour[h]["conflict_pnl"] += t["pnl"] or 0
            by_hour[h]["conflict_n"] += 1

    for h in range(24):
        d = by_hour[h]
        if d["n"] == 0:
            continue
        settled = d["w"] + d["l"]
        wr = d["w"] / settled * 100 if settled else 0
        bar_len = max(0, int(d["pnl"] / 2))
        bar_neg = max(0, int(-d["pnl"] / 2))
        bar = "-" * bar_neg + "|" + "+" * bar_len
        print(f"  {h:02d}:00 {d['n']:3d}t {d['w']:2d}W/{d['l']:2d}L/{d['e']:2d}E "
              f"WR:{wr:3.0f}% ${d['pnl']:+7.2f} "
              f"conflict:${d['conflict_pnl']:+6.2f}({d['conflict_n']:2d}t) {bar}")

    # ================================================================
    # SECTION 8: Win rate by entry price with early exit
    # ================================================================
    print()
    print("=" * 80)
    print("8. EFFECTIVE WIN RATE BY ENTRY PRICE")
    print("=" * 80)

    buckets = [(0.25, 0.35), (0.35, 0.40), (0.40, 0.45), (0.45, 0.50),
               (0.50, 0.55), (0.55, 0.60), (0.60, 0.65)]
    for lo, hi in buckets:
        b = [t for t in trades if lo <= (t["entry_price"] or 0) < hi]
        if not b:
            continue
        w = sum(1 for t in b if t["outcome"] == "WIN")
        l = sum(1 for t in b if t["outcome"] == "LOSS")
        e = sum(1 for t in b if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in b)
        settled = w + l
        wr = w / settled * 100 if settled else 0
        eff = (w + e) / len(b) * 100
        # Breakeven WR for this entry range
        mid_entry = (lo + hi) / 2
        win_payout = (1.0 / mid_entry - 1.0) * 0.98
        be_wr = 1.0 / (1.0 + win_payout) * 100
        print(f"  {lo:.2f}-{hi:.2f}: {len(b):3d}t | {w}W/{l}L/{e}E | "
              f"WR:{wr:3.0f}% Eff:{eff:3.0f}% | P&L:${pnl:+7.2f} | BE:{be_wr:.0f}%")


if __name__ == "__main__":
    main()
