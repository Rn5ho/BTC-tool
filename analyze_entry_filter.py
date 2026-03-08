"""What if we only traded at higher entry prices? Full 3-day simulation."""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cursor = conn.execute(
        "SELECT id, timestamp, market_slug, side, amount_usdc, entry_price, "
        "outcome, pnl, trade_tag, regime_state, regime_strength "
        "FROM live_trades WHERE success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp"
    )
    trades = [dict(t) for t in cursor.fetchall()]

    paper_cursor = conn.execute(
        "SELECT market_slug, side, outcome FROM paper_trades WHERE outcome IS NOT NULL"
    )
    paper_map = {p["market_slug"]: dict(p) for p in paper_cursor}

    # Classify alignment
    for t in trades:
        regime = t["regime_state"] or "unknown"
        if regime == "trending_up":
            t["alignment"] = "aligned" if t["side"] == "UP" else "conflict"
        elif regime == "trending_down":
            t["alignment"] = "aligned" if t["side"] == "DOWN" else "conflict"
        else:
            t["alignment"] = "ranging"

    first_ts = trades[0]["timestamp"] / 1000
    last_ts = trades[-1]["timestamp"] / 1000
    span_days = (last_ts - first_ts) / 86400
    offset = 64.14 - 49.11  # maker fill correction

    print("=" * 90)
    print(f"ENTRY PRICE FILTER ANALYSIS ({span_days:.1f} days, {len(trades)} trades)")
    print("=" * 90)
    print()

    # ================================================================
    # Test various minimum entry price cutoffs
    # ================================================================
    cutoffs = [0.25, 0.35, 0.40, 0.45, 0.50, 0.55]

    print(f"  {'Min Entry':>10s}  {'Trades':>6s}  {'W':>4s}  {'L':>4s}  {'E':>4s}  "
          f"{'WR':>6s}  {'Eff':>6s}  {'P&L':>9s}  {'$/day':>7s}  "
          f"{'AvgWin':>7s}  {'AvgLoss':>8s}  {'Balance':>9s}")
    print(f"  {'-'*10}  {'-'*6}  {'-'*4}  {'-'*4}  {'-'*4}  "
          f"{'-'*6}  {'-'*6}  {'-'*9}  {'-'*7}  "
          f"{'-'*7}  {'-'*8}  {'-'*9}")

    for min_entry in cutoffs:
        filtered = [t for t in trades if (t["entry_price"] or 0) >= min_entry]
        if not filtered:
            continue

        w = sum(1 for t in filtered if t["outcome"] == "WIN")
        l = sum(1 for t in filtered if t["outcome"] == "LOSS")
        e = sum(1 for t in filtered if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in filtered)
        settled = w + l
        wr = w / settled * 100 if settled else 0
        eff = (w + e) / len(filtered) * 100
        daily = pnl / span_days

        wins_list = [t for t in filtered if t["outcome"] == "WIN"]
        losses_list = [t for t in filtered if t["outcome"] == "LOSS"]
        avg_w = sum(t["pnl"] for t in wins_list) / len(wins_list) if wins_list else 0
        avg_l = sum(t["pnl"] for t in losses_list) / len(losses_list) if losses_list else 0

        bal = 73 + pnl + offset

        print(f"  {'>= ' + str(min_entry):>10s}  {len(filtered):>6d}  {w:>4d}  {l:>4d}  {e:>4d}  "
              f"{wr:>5.1f}%  {eff:>5.1f}%  ${pnl:>+8.2f}  ${daily:>+6.2f}  "
              f"${avg_w:>+6.2f}  ${avg_l:>+7.2f}  ${bal:>8.2f}")

    # ================================================================
    # Same thing but COMBINED with regime flip
    # ================================================================
    print()
    print("=" * 90)
    print("ENTRY PRICE FILTER + REGIME FLIP AT 0.35")
    print("=" * 90)
    print()
    print(f"  {'Min Entry':>10s}  {'Trades':>6s}  {'W':>4s}  {'L':>4s}  {'E':>4s}  "
          f"{'WR':>6s}  {'Eff':>6s}  {'P&L':>9s}  {'$/day':>7s}  {'Balance':>9s}")
    print(f"  {'-'*10}  {'-'*6}  {'-'*4}  {'-'*4}  {'-'*4}  "
          f"{'-'*6}  {'-'*6}  {'-'*9}  {'-'*7}  {'-'*9}")

    for min_entry in cutoffs:
        filtered = [t for t in trades if (t["entry_price"] or 0) >= min_entry]
        if not filtered:
            continue

        w = 0
        l = 0
        e = 0
        pnl = 0.0
        count = 0

        for t in filtered:
            if t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) >= 0.35:
                # Flip this trade
                paper = paper_map.get(t["market_slug"])
                if not paper:
                    continue
                if paper["outcome"] == "WIN":
                    market_went = paper["side"]
                else:
                    market_went = "DOWN" if paper["side"] == "UP" else "UP"
                trend_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
                trend_entry = 1.0 - t["entry_price"]
                # Check if flipped entry meets our min_entry filter
                if trend_entry < min_entry:
                    continue  # skip - flipped entry too low
                if market_went == trend_side:
                    bet = t["amount_usdc"]
                    tokens = bet / trend_entry
                    gross = tokens * 1.0 - bet
                    pnl += gross * 0.98
                    w += 1
                else:
                    pnl += -t["amount_usdc"]
                    l += 1
                count += 1
            elif t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) < 0.35:
                continue  # skip weak trend
            else:
                if t["outcome"] == "WIN":
                    w += 1
                elif t["outcome"] == "LOSS":
                    l += 1
                elif t["outcome"] == "EARLY_EXIT":
                    e += 1
                pnl += t["pnl"] or 0
                count += 1

        if count == 0:
            continue

        settled = w + l
        wr = w / settled * 100 if settled else 0
        eff = (w + e) / count * 100
        daily = pnl / span_days
        bal = 73 + pnl + offset

        print(f"  {'>= ' + str(min_entry):>10s}  {count:>6d}  {w:>4d}  {l:>4d}  {e:>4d}  "
              f"{wr:>5.1f}%  {eff:>5.1f}%  ${pnl:>+8.2f}  ${daily:>+6.2f}  ${bal:>8.2f}")

    # ================================================================
    # Detailed breakdown: what you LOSE by raising the floor
    # ================================================================
    print()
    print("=" * 90)
    print("WHAT YOU LOSE/GAIN BY RAISING ENTRY FLOOR")
    print("=" * 90)

    for lo, hi, label in [(0.25, 0.35, "0.25-0.35 (exploration)"),
                           (0.35, 0.40, "0.35-0.40"),
                           (0.40, 0.45, "0.40-0.45"),
                           (0.45, 0.50, "0.45-0.50"),
                           (0.50, 0.55, "0.50-0.55")]:
        bucket = [t for t in trades if lo <= (t["entry_price"] or 0) < hi]
        if not bucket:
            print(f"\n  {label}: no trades")
            continue

        w = sum(1 for t in bucket if t["outcome"] == "WIN")
        l = sum(1 for t in bucket if t["outcome"] == "LOSS")
        e = sum(1 for t in bucket if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in bucket)
        settled = w + l
        wr = w / settled * 100 if settled else 0

        win_pnl = sum(t["pnl"] for t in bucket if t["outcome"] == "WIN")
        loss_pnl = sum(t["pnl"] for t in bucket if t["outcome"] == "LOSS")
        exit_pnl = sum(t["pnl"] for t in bucket if t["outcome"] == "EARLY_EXIT")

        # Breakeven WR at this price
        mid = (lo + hi) / 2
        win_pay = (1.0 / mid - 1.0) * 0.98
        be_wr = 1.0 / (1.0 + win_pay) * 100

        verdict = "CUT IT" if pnl < -5 else ("MARGINAL" if abs(pnl) < 5 else "KEEP")

        print(f"\n  {label}:  {len(bucket)} trades | {w}W/{l}L/{e}E | WR: {wr:.0f}% (need {be_wr:.0f}%)")
        print(f"    Wins: ${win_pnl:+.2f} | Losses: ${loss_pnl:+.2f} | Exits: ${exit_pnl:+.2f} | Net: ${pnl:+.2f}")
        print(f"    Per day: ${pnl/span_days:+.2f}/day  -->  {verdict}")

    # ================================================================
    # Optimal combo summary
    # ================================================================
    print()
    print("=" * 90)
    print("OPTIMAL COMBINATIONS (projected 3-day balance from $73)")
    print("=" * 90)
    print()

    combos = [
        ("Current system",                          "as-is, no changes"),
        ("Flip only",                               "regime flip at 0.35, keep all entries"),
        ("Flip + entry >= 0.40",                    "flip + cut worst entry bucket"),
        ("Flip + entry >= 0.45",                    "flip + only mid-to-high entries"),
        ("Flip + entry >= 0.50",                    "flip + only high entries"),
    ]

    for name, desc in combos:
        if name == "Current system":
            p = sum(t["pnl"] or 0 for t in trades)
        elif name == "Flip only":
            p = 0
            for t in trades:
                if t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) >= 0.35:
                    paper = paper_map.get(t["market_slug"])
                    if not paper:
                        continue
                    mw = paper["side"] if paper["outcome"] == "WIN" else ("DOWN" if paper["side"] == "UP" else "UP")
                    ts = "UP" if t["regime_state"] == "trending_up" else "DOWN"
                    te = 1.0 - t["entry_price"]
                    if mw == ts:
                        p += (t["amount_usdc"] / te * 1.0 - t["amount_usdc"]) * 0.98
                    else:
                        p += -t["amount_usdc"]
                elif t["alignment"] == "conflict":
                    continue
                else:
                    p += t["pnl"] or 0
        else:
            # Extract min entry from name
            min_e = float(name.split(">= ")[1])
            p = 0
            for t in trades:
                if (t["entry_price"] or 0) < min_e:
                    # Check if flipped version would qualify
                    if t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) >= 0.35:
                        flipped_entry = 1.0 - t["entry_price"]
                        if flipped_entry >= min_e:
                            paper = paper_map.get(t["market_slug"])
                            if paper:
                                mw = paper["side"] if paper["outcome"] == "WIN" else ("DOWN" if paper["side"] == "UP" else "UP")
                                ts = "UP" if t["regime_state"] == "trending_up" else "DOWN"
                                if mw == ts:
                                    p += (t["amount_usdc"] / flipped_entry * 1.0 - t["amount_usdc"]) * 0.98
                                else:
                                    p += -t["amount_usdc"]
                    continue
                if t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) >= 0.35:
                    paper = paper_map.get(t["market_slug"])
                    if not paper:
                        continue
                    mw = paper["side"] if paper["outcome"] == "WIN" else ("DOWN" if paper["side"] == "UP" else "UP")
                    ts = "UP" if t["regime_state"] == "trending_up" else "DOWN"
                    te = 1.0 - t["entry_price"]
                    if mw == ts:
                        p += (t["amount_usdc"] / te * 1.0 - t["amount_usdc"]) * 0.98
                    else:
                        p += -t["amount_usdc"]
                elif t["alignment"] == "conflict":
                    continue
                else:
                    p += t["pnl"] or 0

        bal = 73 + p + offset
        daily = p / span_days
        print(f"  {name:<30s}  P&L: ${p:+8.2f}  Balance: ${bal:>7.2f}  (${daily:+.2f}/day)")
        print(f"    {desc}")
        print()


if __name__ == "__main__":
    main()
