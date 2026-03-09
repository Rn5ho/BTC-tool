"""Simulate portfolio growth from $73 with early exit + regime flip from day 1."""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Paper trades for resolution lookup
    paper_cursor = conn.execute(
        "SELECT market_slug, side, outcome FROM paper_trades WHERE outcome IS NOT NULL"
    )
    paper_map = {}
    for p in paper_cursor:
        paper_map[p["market_slug"]] = dict(p)

    # All live trades ordered chronologically
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

    START = 73.0

    # ================================================================
    # Scenario A: ACTUAL (what really happened)
    # ================================================================
    balance_actual = START
    actual_curve = [(trades[0]["timestamp"], balance_actual)]
    for t in trades:
        balance_actual += t["pnl"] or 0
        actual_curve.append((t["timestamp"], balance_actual))

    # ================================================================
    # Scenario B: With regime flip at 0.35 from day 1
    # (non-conflict trades: keep actual P&L
    #  conflict trades with strength >= 0.35: simulate flipped P&L
    #  conflict trades with strength < 0.35: skip them entirely)
    # ================================================================
    balance_flip = START
    flip_curve = [(trades[0]["timestamp"], balance_flip)]
    flip_trades = 0
    flip_wins = 0
    flip_losses = 0

    for t in trades:
        if t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) >= 0.35:
            # Simulate flipped trade
            paper = paper_map.get(t["market_slug"])
            if not paper:
                # Unknown resolution -- skip conservatively
                flip_curve.append((t["timestamp"], balance_flip))
                continue

            if paper["outcome"] == "WIN":
                market_went = paper["side"]
            else:
                market_went = "DOWN" if paper["side"] == "UP" else "UP"

            trend_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
            trend_entry = 1.0 - t["entry_price"]

            bet = t["amount_usdc"]
            if market_went == trend_side:
                tokens = bet / trend_entry
                gross = tokens * 1.0 - bet
                pnl = gross * 0.98
                flip_wins += 1
            else:
                pnl = -bet
                flip_losses += 1

            balance_flip += pnl
            flip_trades += 1

        elif t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) < 0.35:
            # Weak trend -- skip this trade entirely
            flip_curve.append((t["timestamp"], balance_flip))
            continue

        else:
            # Non-conflict: keep actual P&L
            balance_flip += t["pnl"] or 0
            flip_trades += 1

        flip_curve.append((t["timestamp"], balance_flip))

    # ================================================================
    # Scenario C: Skip ALL trending trades (conservative)
    # ================================================================
    balance_skip = START
    skip_curve = [(trades[0]["timestamp"], balance_skip)]
    skip_count = 0

    for t in trades:
        if t["alignment"] == "conflict":
            skip_curve.append((t["timestamp"], balance_skip))
            skip_count += 1
            continue
        balance_skip += t["pnl"] or 0
        skip_curve.append((t["timestamp"], balance_skip))

    # ================================================================
    # Scenario D: No early exit (all trades go to settlement)
    # ================================================================
    balance_no_exit = START
    no_exit_curve = [(trades[0]["timestamp"], balance_no_exit)]

    for t in trades:
        if t["outcome"] == "EARLY_EXIT":
            # What would have happened at settlement?
            paper = paper_map.get(t["market_slug"])
            if not paper:
                balance_no_exit += t["pnl"] or 0  # keep actual if unknown
                no_exit_curve.append((t["timestamp"], balance_no_exit))
                continue

            if paper["side"] == t["side"]:
                hyp_outcome = paper["outcome"]
            else:
                hyp_outcome = "WIN" if paper["outcome"] == "LOSS" else "LOSS"

            if hyp_outcome == "WIN":
                tokens = t["amount_usdc"] / t["entry_price"]
                gross = tokens * 1.0 - t["amount_usdc"]
                pnl = gross * 0.98
            else:
                pnl = -t["amount_usdc"]

            balance_no_exit += pnl
        else:
            balance_no_exit += t["pnl"] or 0

        no_exit_curve.append((t["timestamp"], balance_no_exit))

    # ================================================================
    # Print results
    # ================================================================
    print("=" * 80)
    print("PORTFOLIO SIMULATION: $73 START")
    print("=" * 80)
    print()
    print(f"  Scenario A (ACTUAL):          ${balance_actual:.2f}  ({balance_actual - START:+.2f})")
    print(f"  Scenario B (FLIP >= 0.35):    ${balance_flip:.2f}  ({balance_flip - START:+.2f})")
    print(f"  Scenario C (SKIP trending):   ${balance_skip:.2f}  ({balance_skip - START:+.2f})")
    print(f"  Scenario D (NO early exit):   ${balance_no_exit:.2f}  ({balance_no_exit - START:+.2f})")
    print()
    print(f"  Flip details: {flip_wins}W/{flip_losses}L on flipped trades")
    print(f"  Skipped in scenario C: {skip_count} trades")
    print()

    # ================================================================
    # Chronological portfolio curve with milestones
    # ================================================================
    print("=" * 80)
    print("PORTFOLIO CURVE (hourly snapshots)")
    print("=" * 80)
    print()
    print(f"  {'Time':>16s}   {'Actual':>8s}  {'Flip':>8s}  {'Skip':>8s}  {'NoExit':>8s}")
    print(f"  {'-'*16}   {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")

    # Sample at hourly intervals
    first_ts = trades[0]["timestamp"]
    last_ts = trades[-1]["timestamp"]

    # Build lookup: for each scenario, find balance at given timestamp
    def balance_at(curve, ts):
        bal = START
        for ct, cb in curve:
            if ct > ts:
                break
            bal = cb
        return bal

    ts = first_ts
    while ts <= last_ts:
        time_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        a = balance_at(actual_curve, ts)
        b = balance_at(flip_curve, ts)
        c = balance_at(skip_curve, ts)
        d = balance_at(no_exit_curve, ts)
        print(f"  {time_str:>16s}   ${a:7.2f}  ${b:7.2f}  ${c:7.2f}  ${d:7.2f}")
        ts += 3600 * 1000  # 1 hour in ms

    # Final
    time_str = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
    print(f"  {time_str:>16s}   ${balance_actual:7.2f}  ${balance_flip:7.2f}  "
          f"${balance_skip:7.2f}  ${balance_no_exit:7.2f}  <-- FINAL")

    # ================================================================
    # Peak / trough analysis
    # ================================================================
    print()
    print("=" * 80)
    print("PEAK / TROUGH ANALYSIS")
    print("=" * 80)

    for name, curve in [("Actual", actual_curve), ("Flip", flip_curve),
                         ("Skip", skip_curve), ("NoExit", no_exit_curve)]:
        peak = max(curve, key=lambda x: x[1])
        trough = min(curve, key=lambda x: x[1])
        peak_time = datetime.fromtimestamp(peak[0] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        trough_time = datetime.fromtimestamp(trough[0] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        max_dd = 0
        running_peak = START
        for _, bal in curve:
            running_peak = max(running_peak, bal)
            dd = running_peak - bal
            max_dd = max(max_dd, dd)
        print(f"  {name:8s}: Peak ${peak[1]:.2f} ({peak_time}) | "
              f"Trough ${trough[1]:.2f} ({trough_time}) | Max DD: ${max_dd:.2f}")

    # ================================================================
    # Day-by-day comparison
    # ================================================================
    print()
    print("=" * 80)
    print("DAY-BY-DAY P&L COMPARISON")
    print("=" * 80)

    by_day = defaultdict(lambda: {"actual": 0, "flip": 0, "skip": 0, "no_exit": 0, "n": 0})

    for t in trades:
        day = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day]["actual"] += t["pnl"] or 0
        by_day[day]["n"] += 1

        if t["alignment"] == "conflict" and abs(t["regime_strength"] or 0) >= 0.35:
            paper = paper_map.get(t["market_slug"])
            if paper:
                if paper["outcome"] == "WIN":
                    mw = paper["side"]
                else:
                    mw = "DOWN" if paper["side"] == "UP" else "UP"
                ts_side = "UP" if t["regime_state"] == "trending_up" else "DOWN"
                te = 1.0 - t["entry_price"]
                if mw == ts_side:
                    fp = (t["amount_usdc"] / te * 1.0 - t["amount_usdc"]) * 0.98
                else:
                    fp = -t["amount_usdc"]
                by_day[day]["flip"] += fp
            else:
                by_day[day]["flip"] += 0
        elif t["alignment"] == "conflict":
            by_day[day]["flip"] += 0  # skipped
            by_day[day]["skip"] += 0
        else:
            by_day[day]["flip"] += t["pnl"] or 0
            by_day[day]["skip"] += t["pnl"] or 0

        # No early exit
        if t["outcome"] == "EARLY_EXIT":
            paper = paper_map.get(t["market_slug"])
            if paper:
                if paper["side"] == t["side"]:
                    ho = paper["outcome"]
                else:
                    ho = "WIN" if paper["outcome"] == "LOSS" else "LOSS"
                if ho == "WIN":
                    tokens = t["amount_usdc"] / t["entry_price"]
                    hp = (tokens * 1.0 - t["amount_usdc"]) * 0.98
                else:
                    hp = -t["amount_usdc"]
                by_day[day]["no_exit"] += hp
            else:
                by_day[day]["no_exit"] += t["pnl"] or 0
        else:
            by_day[day]["no_exit"] += t["pnl"] or 0

        if t["alignment"] != "conflict":
            by_day[day]["skip"] += 0  # already added above

    print(f"\n  {'Day':>12s}  {'Trades':>6s}  {'Actual':>9s}  {'Flip':>9s}  {'Skip':>9s}  {'NoExit':>9s}")
    print(f"  {'-'*12}  {'-'*6}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*9}")
    for day in sorted(by_day.keys()):
        d = by_day[day]
        print(f"  {day:>12s}  {d['n']:6d}  ${d['actual']:+8.2f}  ${d['flip']:+8.2f}  "
              f"${d['skip']:+8.2f}  ${d['no_exit']:+8.2f}")


if __name__ == "__main__":
    main()
