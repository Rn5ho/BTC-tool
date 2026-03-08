"""W/L breakdown for all scenarios across full 3-day period."""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    paper_cursor = conn.execute(
        "SELECT market_slug, side, outcome FROM paper_trades WHERE outcome IS NOT NULL"
    )
    paper_map = {}
    for p in paper_cursor:
        paper_map[p["market_slug"]] = dict(p)

    cursor = conn.execute(
        "SELECT id, timestamp, market_slug, side, amount_usdc, entry_price, "
        "outcome, pnl, trade_tag, regime_state, regime_strength "
        "FROM live_trades WHERE success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp"
    )
    trades = [dict(t) for t in cursor.fetchall()]

    # Classify alignment
    for t in trades:
        regime = t["regime_state"] or "unknown"
        if regime == "trending_up":
            t["alignment"] = "aligned" if t["side"] == "UP" else "conflict"
        elif regime == "trending_down":
            t["alignment"] = "aligned" if t["side"] == "DOWN" else "conflict"
        else:
            t["alignment"] = "ranging"

    # Determine hypothetical outcome for early exits
    for t in trades:
        if t["outcome"] == "EARLY_EXIT":
            paper = paper_map.get(t["market_slug"])
            if paper:
                if paper["side"] == t["side"]:
                    t["hyp_outcome"] = paper["outcome"]
                else:
                    t["hyp_outcome"] = "WIN" if paper["outcome"] == "LOSS" else "LOSS"
            else:
                t["hyp_outcome"] = None
        else:
            t["hyp_outcome"] = t["outcome"]

    # Determine flip outcome for conflict trades
    for t in trades:
        if t["alignment"] == "conflict":
            paper = paper_map.get(t["market_slug"])
            if paper:
                if paper["outcome"] == "WIN":
                    market_went = paper["side"]
                else:
                    market_went = "DOWN" if paper["side"] == "UP" else "UP"
                trend_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
                t["flip_outcome"] = "WIN" if market_went == trend_side else "LOSS"
            else:
                t["flip_outcome"] = None
        else:
            t["flip_outcome"] = None

    first_ts = trades[0]["timestamp"] / 1000
    last_ts = trades[-1]["timestamp"] / 1000
    span_days = (last_ts - first_ts) / 86400

    print("=" * 85)
    print(f"W/L BREAKDOWN - ALL SCENARIOS ({span_days:.1f} days, {len(trades)} trades)")
    print("=" * 85)
    print()

    # ================================================================
    # Scenario A: ACTUAL
    # ================================================================
    a_w = sum(1 for t in trades if t["outcome"] == "WIN")
    a_l = sum(1 for t in trades if t["outcome"] == "LOSS")
    a_e = sum(1 for t in trades if t["outcome"] == "EARLY_EXIT")
    a_pnl = sum(t["pnl"] or 0 for t in trades)
    a_settled = a_w + a_l
    a_wr = a_w / a_settled * 100 if a_settled else 0
    a_eff = (a_w + a_e) / len(trades) * 100

    print("  A: ACTUAL (what really happened)")
    print("  " + "-" * 50)
    print(f"    {a_w}W / {a_l}L / {a_e}E  ({len(trades)} total)")
    print(f"    Settlement WR:  {a_w}/{a_settled} = {a_wr:.1f}%")
    print(f"    Effective WR:   {a_w + a_e}/{len(trades)} = {a_eff:.1f}%")
    print(f"    P&L: ${a_pnl:+.2f}")
    print()

    # ================================================================
    # Scenario B: FLIP >= 0.35
    # ================================================================
    b_w = 0
    b_l = 0
    b_e = 0
    b_pnl = 0.0
    b_total = 0

    for t in trades:
        if t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) >= 0.35:
            # This trade gets flipped
            if t["flip_outcome"] == "WIN":
                b_w += 1
                trend_entry = 1.0 - t["entry_price"]
                bet = t["amount_usdc"]
                tokens = bet / trend_entry
                gross = tokens * 1.0 - bet
                b_pnl += gross * 0.98
            elif t["flip_outcome"] == "LOSS":
                b_l += 1
                b_pnl += -t["amount_usdc"]
            else:
                continue  # unknown, skip
            b_total += 1
        elif t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) < 0.35:
            # Weak trend, skip entirely
            continue
        else:
            # Keep as-is
            if t["outcome"] == "WIN":
                b_w += 1
            elif t["outcome"] == "LOSS":
                b_l += 1
            elif t["outcome"] == "EARLY_EXIT":
                b_e += 1
            b_pnl += t["pnl"] or 0
            b_total += 1

    b_settled = b_w + b_l
    b_wr = b_w / b_settled * 100 if b_settled else 0
    b_eff = (b_w + b_e) / b_total * 100 if b_total else 0

    print("  B: FLIP >= 0.35 (flip trend-conflict trades)")
    print("  " + "-" * 50)
    print(f"    {b_w}W / {b_l}L / {b_e}E  ({b_total} total, {len(trades) - b_total} skipped)")
    print(f"    Settlement WR:  {b_w}/{b_settled} = {b_wr:.1f}%")
    print(f"    Effective WR:   {b_w + b_e}/{b_total} = {b_eff:.1f}%")
    print(f"    P&L: ${b_pnl:+.2f}")
    print()

    # ================================================================
    # Scenario C: SKIP trending
    # ================================================================
    non_conflict = [t for t in trades if t["alignment"] != "conflict"]
    c_w = sum(1 for t in non_conflict if t["outcome"] == "WIN")
    c_l = sum(1 for t in non_conflict if t["outcome"] == "LOSS")
    c_e = sum(1 for t in non_conflict if t["outcome"] == "EARLY_EXIT")
    c_pnl = sum(t["pnl"] or 0 for t in non_conflict)
    c_settled = c_w + c_l
    c_wr = c_w / c_settled * 100 if c_settled else 0
    c_eff = (c_w + c_e) / len(non_conflict) * 100 if non_conflict else 0
    skipped = len(trades) - len(non_conflict)

    print("  C: SKIP trending (only trade in ranging)")
    print("  " + "-" * 50)
    print(f"    {c_w}W / {c_l}L / {c_e}E  ({len(non_conflict)} total, {skipped} skipped)")
    print(f"    Settlement WR:  {c_w}/{c_settled} = {c_wr:.1f}%")
    print(f"    Effective WR:   {c_w + c_e}/{len(non_conflict)} = {c_eff:.1f}%")
    print(f"    P&L: ${c_pnl:+.2f}")
    print()

    # ================================================================
    # Scenario D: NO early exit
    # ================================================================
    d_w = 0
    d_l = 0
    d_pnl = 0.0

    for t in trades:
        if t["outcome"] == "EARLY_EXIT":
            if t["hyp_outcome"] == "WIN":
                d_w += 1
                tokens = t["amount_usdc"] / t["entry_price"]
                gross = tokens * 1.0 - t["amount_usdc"]
                d_pnl += gross * 0.98
            elif t["hyp_outcome"] == "LOSS":
                d_l += 1
                d_pnl += -t["amount_usdc"]
            else:
                # Unknown -- keep actual
                d_pnl += t["pnl"] or 0
                if (t["pnl"] or 0) > 0:
                    d_w += 1
                else:
                    d_l += 1
        else:
            if t["outcome"] == "WIN":
                d_w += 1
            else:
                d_l += 1
            d_pnl += t["pnl"] or 0

    d_wr = d_w / (d_w + d_l) * 100 if (d_w + d_l) else 0

    print("  D: NO early exit (all go to settlement)")
    print("  " + "-" * 50)
    print(f"    {d_w}W / {d_l}L / 0E  ({d_w + d_l} total)")
    print(f"    Settlement WR:  {d_w}/{d_w + d_l} = {d_wr:.1f}%")
    print(f"    P&L: ${d_pnl:+.2f}")
    print()

    # ================================================================
    # Comparison table
    # ================================================================
    offset = 64.14 - 49.11

    print("=" * 85)
    print("COMPARISON TABLE")
    print("=" * 85)
    print()
    print(f"  {'Scenario':<30s} {'Trades':>6s} {'W':>4s} {'L':>4s} {'E':>4s} "
          f"{'WR':>6s} {'Eff WR':>7s} {'P&L':>9s} {'Balance':>9s}")
    print(f"  {'-'*30} {'-'*6} {'-'*4} {'-'*4} {'-'*4} "
          f"{'-'*6} {'-'*7} {'-'*9} {'-'*9}")

    rows = [
        ("D: No early exit",    d_w+d_l, d_w, d_l, 0,
         f"{d_wr:.1f}%", f"{d_wr:.1f}%",
         d_pnl, 73 + d_pnl + offset),
        ("A: Actual",           len(trades), a_w, a_l, a_e,
         f"{a_wr:.1f}%", f"{a_eff:.1f}%",
         a_pnl, 73 + a_pnl + offset),
        ("C: Skip trending",    len(non_conflict), c_w, c_l, c_e,
         f"{c_wr:.1f}%", f"{c_eff:.1f}%",
         c_pnl, 73 + c_pnl + offset),
        ("B: Flip >= 0.35",     b_total, b_w, b_l, b_e,
         f"{b_wr:.1f}%", f"{b_eff:.1f}%",
         b_pnl, 73 + b_pnl + offset),
    ]

    for name, n, w, l, e, wr, eff, pnl, bal in rows:
        print(f"  {name:<30s} {n:>6d} {w:>4d} {l:>4d} {e:>4d} "
              f"{wr:>6s} {eff:>7s} ${pnl:>+8.2f} ${bal:>8.2f}")

    # ================================================================
    # Day-by-day W/L
    # ================================================================
    print()
    print("=" * 85)
    print("DAY-BY-DAY W/L (Actual)")
    print("=" * 85)

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
        eff = (w + e) / len(tlist) * 100

        # Conflict trades this day
        conf = [t for t in tlist if t["alignment"] == "conflict"]
        conf_w = sum(1 for t in conf if t["outcome"] == "WIN")
        conf_l = sum(1 for t in conf if t["outcome"] == "LOSS")
        conf_e = sum(1 for t in conf if t["outcome"] == "EARLY_EXIT")
        conf_pnl = sum(t["pnl"] or 0 for t in conf)

        print(f"\n  {day}:  {w}W / {l}L / {e}E = {len(tlist)} trades")
        print(f"    Settlement WR: {wr:.0f}% | Effective WR: {eff:.0f}% | P&L: ${pnl:+.2f}")
        if conf:
            print(f"    Trend-conflict: {conf_w}W/{conf_l}L/{conf_e}E = {len(conf)} trades, "
                  f"P&L: ${conf_pnl:+.2f}")


if __name__ == "__main__":
    main()
