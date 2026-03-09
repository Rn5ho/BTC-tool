#!/usr/bin/env python3
"""Comprehensive trade analysis for BTC Polymarket Edge Finder.

Run on the VPS:
    cd /home/btcedge/BTC-tool
    source venv/bin/activate
    python analyze_trades.py

Or specify a custom DB path:
    python analyze_trades.py /path/to/btc_edge.db
"""

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Polymarket fee: 2% on net profit for wins
PROFIT_FEE_RATE = 0.02


def load_trades(db_path: str) -> list[dict]:
    """Load all settled trades from the database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE outcome IS NOT NULL ORDER BY timestamp"
    ).fetchall()
    trades = [dict(r) for r in rows]
    conn.close()
    return trades


def load_feature_snapshots(db_path: str) -> dict[str, dict]:
    """Load feature snapshots keyed by market_slug for joining with trades."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM feature_snapshots ORDER BY timestamp"
    ).fetchall()
    conn.close()
    # Keep the latest snapshot per slug
    by_slug: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        slug = d.get("market_slug")
        if slug:
            by_slug[slug] = d
    return by_slug


def pnl_for_trade(trade: dict) -> float:
    """Recompute PnL from trade data (sanity check)."""
    if trade["outcome"] == "WIN":
        gross = trade["size_usdc"] * ((1.0 - trade["entry_price"]) / trade["entry_price"])
        return gross * (1.0 - PROFIT_FEE_RATE)
    else:
        return -trade["size_usdc"]


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def fmt_usd(x: float) -> str:
    return f"${x:+,.2f}"


def section(title: str) -> str:
    return f"\n{'=' * 60}\n  {title}\n{'=' * 60}"


def analyze(db_path: str) -> None:
    trades = load_trades(db_path)
    if not trades:
        print("No settled trades found.")
        return

    features_by_slug = load_feature_snapshots(db_path)

    total = len(trades)
    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    voids = [t for t in trades if t["outcome"] == "VOID"]
    n_wins = len(wins)
    n_losses = len(losses)
    n_voids = len(voids)
    # Exclude voids from win rate
    settled = n_wins + n_losses
    win_rate = n_wins / settled if settled > 0 else 0

    total_pnl = sum(t["pnl"] or 0 for t in trades)
    avg_pnl = total_pnl / settled if settled > 0 else 0
    total_staked = sum(t["size_usdc"] for t in trades if t["outcome"] in ("WIN", "LOSS"))
    roi_on_stakes = total_pnl / total_staked if total_staked > 0 else 0

    avg_edge = sum(t["edge"] for t in trades) / total
    avg_win_edge = sum(t["edge"] for t in wins) / n_wins if n_wins else 0
    avg_loss_edge = sum(t["edge"] for t in losses) / n_losses if n_losses else 0

    avg_entry = sum(t["entry_price"] for t in trades) / total
    avg_win_entry = sum(t["entry_price"] for t in wins) / n_wins if n_wins else 0
    avg_loss_entry = sum(t["entry_price"] for t in losses) / n_losses if n_losses else 0

    # Date range
    first_ts = trades[0]["timestamp"] / 1000
    last_ts = trades[-1]["timestamp"] / 1000
    first_dt = datetime.fromtimestamp(first_ts, tz=timezone.utc)
    last_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc)
    days_active = max((last_ts - first_ts) / 86400, 1)

    # ===================================================================
    # 1. OVERALL SUMMARY
    # ===================================================================
    print(section("OVERALL SUMMARY"))
    print(f"  Period:         {first_dt:%Y-%m-%d %H:%M} to {last_dt:%Y-%m-%d %H:%M} UTC")
    print(f"  Days active:    {days_active:.1f}")
    print(f"  Total trades:   {total} (settled: {settled}, void: {n_voids})")
    print(f"  Wins / Losses:  {n_wins} / {n_losses}")
    print(f"  Win rate:       {fmt_pct(win_rate)}")
    print(f"  Total P&L:      {fmt_usd(total_pnl)}")
    print(f"  Avg P&L/trade:  {fmt_usd(avg_pnl)}")
    print(f"  Total staked:   ${total_staked:,.2f}")
    print(f"  ROI on stakes:  {fmt_pct(roi_on_stakes)}")
    print(f"  Trades/day:     {settled / days_active:.1f}")
    print(f"  P&L/day:        {fmt_usd(total_pnl / days_active)}")
    print()
    print(f"  Avg edge:       {fmt_pct(avg_edge)}")
    print(f"  Avg edge (W):   {fmt_pct(avg_win_edge)}")
    print(f"  Avg edge (L):   {fmt_pct(avg_loss_edge)}")
    print(f"  Avg entry:      {avg_entry:.4f}")
    print(f"  Avg entry (W):  {avg_win_entry:.4f}")
    print(f"  Avg entry (L):  {avg_loss_entry:.4f}")

    # ===================================================================
    # 2. EDGE BUCKET ANALYSIS
    # ===================================================================
    print(section("EDGE BUCKET ANALYSIS"))
    buckets = [
        ("5.0-6.0%",  0.050, 0.060),
        ("6.0-7.0%",  0.060, 0.070),
        ("7.0-8.0%",  0.070, 0.080),
        ("8.0-10.0%", 0.080, 0.100),
        ("10.0-12.0%",0.100, 0.120),
        ("12.0-15.0%",0.120, 0.150),
        ("15.0-20.0%",0.150, 0.200),
        ("20.0%+",    0.200, 1.000),
    ]
    print(f"  {'Bucket':>12s}  {'Trades':>7s}  {'WinRate':>8s}  {'P&L':>10s}  {'AvgPnL':>8s}  {'AvgEdge':>8s}  {'EV/trade':>9s}")
    print(f"  {'-'*12}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*9}")
    for label, lo, hi in buckets:
        subset = [t for t in trades if lo <= t["edge"] < hi and t["outcome"] in ("WIN", "LOSS")]
        if not subset:
            continue
        bwins = sum(1 for t in subset if t["outcome"] == "WIN")
        bwr = bwins / len(subset)
        bpnl = sum(t["pnl"] or 0 for t in subset)
        bavg = bpnl / len(subset)
        bedge = sum(t["edge"] for t in subset) / len(subset)
        # EV per trade: expected_pnl based on win rate and avg win/loss amounts
        avg_win_pnl = sum(t["pnl"] for t in subset if t["outcome"] == "WIN") / max(bwins, 1)
        avg_loss_pnl = sum(t["pnl"] for t in subset if t["outcome"] == "LOSS") / max(len(subset) - bwins, 1)
        ev = bwr * avg_win_pnl + (1 - bwr) * avg_loss_pnl
        print(f"  {label:>12s}  {len(subset):>7d}  {bwr:>7.1%}  {bpnl:>+10.2f}  {bavg:>+8.2f}  {bedge:>7.1%}  {ev:>+9.2f}")

    # ===================================================================
    # 3. SIDE ANALYSIS (UP vs DOWN)
    # ===================================================================
    print(section("SIDE ANALYSIS"))
    for side in ("UP", "DOWN"):
        subset = [t for t in trades if t["side"] == side and t["outcome"] in ("WIN", "LOSS")]
        if not subset:
            continue
        sw = sum(1 for t in subset if t["outcome"] == "WIN")
        swr = sw / len(subset)
        spnl = sum(t["pnl"] or 0 for t in subset)
        savg = spnl / len(subset)
        se = sum(t["edge"] for t in subset) / len(subset)
        print(f"  {side}:")
        print(f"    Trades:   {len(subset)}")
        print(f"    Win rate: {fmt_pct(swr)}")
        print(f"    P&L:      {fmt_usd(spnl)}")
        print(f"    Avg P&L:  {fmt_usd(savg)}")
        print(f"    Avg edge: {fmt_pct(se)}")
        print()

    # ===================================================================
    # 4. HOUR OF DAY ANALYSIS
    # ===================================================================
    print(section("HOUR OF DAY ANALYSIS (UTC)"))
    hour_data: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        if t["outcome"] not in ("WIN", "LOSS"):
            continue
        dt = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
        hour_data[dt.hour].append(t)

    print(f"  {'Hour':>6s}  {'Trades':>7s}  {'WinRate':>8s}  {'P&L':>10s}  {'AvgPnL':>8s}")
    print(f"  {'-'*6}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*8}")
    for hour in range(24):
        subset = hour_data.get(hour, [])
        if not subset:
            continue
        hw = sum(1 for t in subset if t["outcome"] == "WIN")
        hwr = hw / len(subset)
        hpnl = sum(t["pnl"] or 0 for t in subset)
        havg = hpnl / len(subset)
        bar = "#" * int(hwr * 20)
        print(f"  {hour:02d}:00  {len(subset):>7d}  {hwr:>7.1%}  {hpnl:>+10.2f}  {havg:>+8.2f}  {bar}")

    # Best/worst hours
    ranked = sorted(
        [(h, ts) for h, ts in hour_data.items() if len(ts) >= 10],
        key=lambda x: sum(t["pnl"] or 0 for t in x[1]),
        reverse=True,
    )
    if ranked:
        print(f"\n  Best hours (by P&L, min 10 trades):")
        for h, ts in ranked[:3]:
            hw = sum(1 for t in ts if t["outcome"] == "WIN")
            hpnl = sum(t["pnl"] or 0 for t in ts)
            print(f"    {h:02d}:00 UTC  {hw}/{len(ts)} wins ({hw/len(ts):.0%})  P&L: {fmt_usd(hpnl)}")
        print(f"  Worst hours:")
        for h, ts in ranked[-3:]:
            hw = sum(1 for t in ts if t["outcome"] == "WIN")
            hpnl = sum(t["pnl"] or 0 for t in ts)
            print(f"    {h:02d}:00 UTC  {hw}/{len(ts)} wins ({hw/len(ts):.0%})  P&L: {fmt_usd(hpnl)}")

    # ===================================================================
    # 5. DAY OF WEEK ANALYSIS
    # ===================================================================
    print(section("DAY OF WEEK ANALYSIS"))
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_data: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        if t["outcome"] not in ("WIN", "LOSS"):
            continue
        dt = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
        dow_data[dt.weekday()].append(t)

    print(f"  {'Day':>5s}  {'Trades':>7s}  {'WinRate':>8s}  {'P&L':>10s}  {'AvgPnL':>8s}")
    print(f"  {'-'*5}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*8}")
    for dow in range(7):
        subset = dow_data.get(dow, [])
        if not subset:
            continue
        dw = sum(1 for t in subset if t["outcome"] == "WIN")
        dwr = dw / len(subset)
        dpnl = sum(t["pnl"] or 0 for t in subset)
        davg = dpnl / len(subset)
        print(f"  {dow_names[dow]:>5s}  {len(subset):>7d}  {dwr:>7.1%}  {dpnl:>+10.2f}  {davg:>+8.2f}")

    # ===================================================================
    # 6. MODEL CALIBRATION
    # ===================================================================
    print(section("MODEL CALIBRATION (predicted P vs actual win rate)"))
    prob_buckets = [
        ("50-55%", 0.50, 0.55),
        ("55-60%", 0.55, 0.60),
        ("60-65%", 0.60, 0.65),
        ("65-70%", 0.65, 0.70),
        ("70-75%", 0.70, 0.75),
        ("75-80%", 0.75, 0.80),
        ("80%+",   0.80, 1.00),
    ]
    print(f"  {'Predicted':>10s}  {'Trades':>7s}  {'Actual WR':>10s}  {'Delta':>8s}  {'P&L':>10s}")
    print(f"  {'-'*10}  {'-'*7}  {'-'*10}  {'-'*8}  {'-'*10}")
    for label, lo, hi in prob_buckets:
        subset = [t for t in trades if lo <= t["our_prob"] < hi and t["outcome"] in ("WIN", "LOSS")]
        if not subset:
            continue
        cw = sum(1 for t in subset if t["outcome"] == "WIN")
        cwr = cw / len(subset)
        mid = (lo + hi) / 2
        delta = cwr - mid
        cpnl = sum(t["pnl"] or 0 for t in subset)
        cal = "GOOD" if abs(delta) < 0.05 else ("OVER" if delta < 0 else "UNDER")
        print(f"  {label:>10s}  {len(subset):>7d}  {cwr:>9.1%}  {delta:>+7.1%}  {cpnl:>+10.2f}  {cal}")

    # ===================================================================
    # 7. ENTRY PRICE ANALYSIS
    # ===================================================================
    print(section("ENTRY PRICE ANALYSIS (market-implied probability at entry)"))
    entry_buckets = [
        ("40-45c", 0.40, 0.45),
        ("45-48c", 0.45, 0.48),
        ("48-50c", 0.48, 0.50),
        ("50-52c", 0.50, 0.52),
        ("52-55c", 0.52, 0.55),
        ("55-60c", 0.55, 0.60),
        ("60c+",   0.60, 1.00),
    ]
    print(f"  {'Entry':>8s}  {'Trades':>7s}  {'WinRate':>8s}  {'P&L':>10s}  {'Payout':>8s}")
    print(f"  {'-'*8}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*8}")
    for label, lo, hi in entry_buckets:
        subset = [t for t in trades if lo <= t["entry_price"] < hi and t["outcome"] in ("WIN", "LOSS")]
        if not subset:
            continue
        ew = sum(1 for t in subset if t["outcome"] == "WIN")
        ewr = ew / len(subset)
        epnl = sum(t["pnl"] or 0 for t in subset)
        # avg payout ratio for wins
        avg_payout = sum(
            (1.0 - t["entry_price"]) / t["entry_price"]
            for t in subset if t["outcome"] == "WIN"
        ) / max(ew, 1)
        print(f"  {label:>8s}  {len(subset):>7d}  {ewr:>7.1%}  {epnl:>+10.2f}  {avg_payout:>7.2f}x")

    # ===================================================================
    # 8. STREAK ANALYSIS
    # ===================================================================
    print(section("STREAK ANALYSIS"))
    settled_trades = [t for t in trades if t["outcome"] in ("WIN", "LOSS")]
    max_win_streak = 0
    max_loss_streak = 0
    current_streak = 0
    current_type = None
    streaks_win = []
    streaks_loss = []

    for t in settled_trades:
        if t["outcome"] == current_type:
            current_streak += 1
        else:
            if current_type == "WIN" and current_streak > 0:
                streaks_win.append(current_streak)
            elif current_type == "LOSS" and current_streak > 0:
                streaks_loss.append(current_streak)
            current_type = t["outcome"]
            current_streak = 1

    # Don't forget the last streak
    if current_type == "WIN":
        streaks_win.append(current_streak)
    elif current_type == "LOSS":
        streaks_loss.append(current_streak)

    max_win_streak = max(streaks_win) if streaks_win else 0
    max_loss_streak = max(streaks_loss) if streaks_loss else 0
    avg_win_streak = sum(streaks_win) / len(streaks_win) if streaks_win else 0
    avg_loss_streak = sum(streaks_loss) / len(streaks_loss) if streaks_loss else 0

    print(f"  Max win streak:   {max_win_streak}")
    print(f"  Avg win streak:   {avg_win_streak:.1f}")
    print(f"  Max loss streak:  {max_loss_streak}")
    print(f"  Avg loss streak:  {avg_loss_streak:.1f}")

    # Streak distribution
    print(f"\n  Win streak distribution:")
    streak_dist_w = defaultdict(int)
    for s in streaks_win:
        streak_dist_w[s] += 1
    for length in sorted(streak_dist_w.keys()):
        print(f"    {length:>3d}x: {streak_dist_w[length]:>4d} times")

    print(f"  Loss streak distribution:")
    streak_dist_l = defaultdict(int)
    for s in streaks_loss:
        streak_dist_l[s] += 1
    for length in sorted(streak_dist_l.keys()):
        print(f"    {length:>3d}x: {streak_dist_l[length]:>4d} times")

    # ===================================================================
    # 9. DRAWDOWN ANALYSIS
    # ===================================================================
    print(section("DRAWDOWN ANALYSIS"))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_start = 0
    max_dd_end = 0
    dd_start = 0
    in_drawdown = False

    equity_curve = []
    for i, t in enumerate(settled_trades):
        equity += t["pnl"] or 0
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
            in_drawdown = False
        dd = peak - equity
        if dd > 0 and not in_drawdown:
            dd_start = i
            in_drawdown = True
        if dd > max_dd:
            max_dd = dd
            max_dd_start = dd_start
            max_dd_end = i

    print(f"  Peak equity:      {fmt_usd(peak)}")
    print(f"  Final equity:     {fmt_usd(equity)}")
    print(f"  Max drawdown:     {fmt_usd(-max_dd)}")
    if peak > 0:
        print(f"  Max DD %:         {max_dd / peak:.1%} of peak")

    if max_dd > 0 and max_dd_start < len(settled_trades) and max_dd_end < len(settled_trades):
        dd_start_dt = datetime.fromtimestamp(
            settled_trades[max_dd_start]["timestamp"] / 1000, tz=timezone.utc
        )
        dd_end_dt = datetime.fromtimestamp(
            settled_trades[max_dd_end]["timestamp"] / 1000, tz=timezone.utc
        )
        dd_trades = max_dd_end - max_dd_start
        print(f"  DD period:        {dd_start_dt:%m-%d %H:%M} to {dd_end_dt:%m-%d %H:%M} ({dd_trades} trades)")

    # ===================================================================
    # 10. ROLLING PERFORMANCE (daily buckets)
    # ===================================================================
    print(section("ROLLING PERFORMANCE (by day)"))
    daily: dict[str, list[dict]] = defaultdict(list)
    for t in settled_trades:
        dt = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
        daily[dt.strftime("%m-%d")].append(t)

    print(f"  {'Date':>6s}  {'Trades':>7s}  {'WinRate':>8s}  {'P&L':>10s}  {'Cumul':>10s}")
    print(f"  {'-'*6}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*10}")
    cumulative = 0.0
    for day_key in sorted(daily.keys()):
        day_trades = daily[day_key]
        dw = sum(1 for t in day_trades if t["outcome"] == "WIN")
        dwr = dw / len(day_trades)
        dpnl = sum(t["pnl"] or 0 for t in day_trades)
        cumulative += dpnl
        bar = "+" * max(0, int(dpnl / 2)) if dpnl >= 0 else "-" * max(0, int(-dpnl / 2))
        print(f"  {day_key:>6s}  {len(day_trades):>7d}  {dwr:>7.1%}  {dpnl:>+10.2f}  {cumulative:>+10.2f}  {bar}")

    # ===================================================================
    # 11. OPTIMAL EDGE THRESHOLD
    # ===================================================================
    print(section("OPTIMAL EDGE THRESHOLD"))
    print("  Simulates cumulative P&L at different MIN_EDGE thresholds:\n")
    thresholds = [0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12, 0.14, 0.16, 0.18, 0.20]
    print(f"  {'MinEdge':>8s}  {'Trades':>7s}  {'WinRate':>8s}  {'P&L':>10s}  {'PnL/trade':>10s}  {'PnL/day':>9s}")
    print(f"  {'-'*8}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*10}  {'-'*9}")
    best_threshold = 0.05
    best_pnl = -float("inf")
    for threshold in thresholds:
        subset = [t for t in settled_trades if t["edge"] >= threshold]
        if not subset:
            continue
        tw = sum(1 for t in subset if t["outcome"] == "WIN")
        twr = tw / len(subset)
        tpnl = sum(t["pnl"] or 0 for t in subset)
        tavg = tpnl / len(subset)
        tpd = tpnl / days_active
        marker = " <-- current" if abs(threshold - 0.05) < 0.001 else ""
        if tpnl > best_pnl:
            best_pnl = tpnl
            best_threshold = threshold
        print(f"  {threshold:>7.0%}  {len(subset):>7d}  {twr:>7.1%}  {tpnl:>+10.2f}  {tavg:>+10.2f}  {tpd:>+9.2f}{marker}")
    print(f"\n  Optimal threshold: {best_threshold:.0%} (max cumulative P&L: {fmt_usd(best_pnl)})")

    # Also check max_edge cap
    print(f"\n  Effect of MAX_EDGE cap (removing trades above cap):")
    max_caps = [0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 1.00]
    print(f"  {'MaxEdge':>8s}  {'Trades':>7s}  {'WinRate':>8s}  {'P&L':>10s}")
    print(f"  {'-'*8}  {'-'*7}  {'-'*8}  {'-'*10}")
    for cap in max_caps:
        subset = [t for t in settled_trades if t["edge"] < cap]
        if not subset:
            continue
        cw = sum(1 for t in subset if t["outcome"] == "WIN")
        cwr = cw / len(subset)
        cpnl = sum(t["pnl"] or 0 for t in subset)
        marker = " <-- current" if abs(cap - 0.20) < 0.001 else ""
        label = "No cap" if cap >= 1.0 else f"{cap:.0%}"
        print(f"  {label:>8s}  {len(subset):>7d}  {cwr:>7.1%}  {cpnl:>+10.2f}{marker}")

    # ===================================================================
    # 12. MARKET PROBABILITY AT ENTRY (how often does market = 50/50?)
    # ===================================================================
    print(section("MARKET PRICE DISTRIBUTION AT ENTRY"))
    mkt_dist = defaultdict(int)
    for t in settled_trades:
        bucket = round(t["market_prob"] * 20) / 20  # 5% buckets
        mkt_dist[bucket] += 1
    print(f"  {'MktProb':>8s}  {'Count':>6s}  {'%':>6s}")
    print(f"  {'-'*8}  {'-'*6}  {'-'*6}")
    for prob in sorted(mkt_dist.keys()):
        count = mkt_dist[prob]
        pct = count / len(settled_trades) * 100
        bar = "#" * int(pct)
        print(f"  {prob:>7.0%}  {count:>6d}  {pct:>5.1f}%  {bar}")

    # ===================================================================
    # 13. FEATURE ANALYSIS (if snapshots available)
    # ===================================================================
    if features_by_slug:
        print(section("FEATURE ANALYSIS (from feature_snapshots)"))

        feature_names = ["obi", "taker_ratio", "momentum_1m", "momentum_5m",
                         "rsi", "vwap_deviation", "funding_rate", "volume_zscore"]

        # Collect feature values for wins vs losses
        win_features: dict[str, list[float]] = defaultdict(list)
        loss_features: dict[str, list[float]] = defaultdict(list)

        matched = 0
        for t in settled_trades:
            slug = t["market_slug"]
            if slug not in features_by_slug:
                continue
            matched += 1
            feat = features_by_slug[slug]
            target = win_features if t["outcome"] == "WIN" else loss_features
            for fn in feature_names:
                val = feat.get(fn)
                if val is not None:
                    target[fn].append(val)

        print(f"  Matched {matched}/{len(settled_trades)} trades to feature snapshots\n")

        if matched > 0:
            print(f"  {'Feature':>16s}  {'Win Avg':>10s}  {'Loss Avg':>10s}  {'Delta':>10s}  {'Signal':>8s}")
            print(f"  {'-'*16}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*8}")
            for fn in feature_names:
                wvals = win_features.get(fn, [])
                lvals = loss_features.get(fn, [])
                wavg = sum(wvals) / len(wvals) if wvals else 0
                lavg = sum(lvals) / len(lvals) if lvals else 0
                delta = wavg - lavg
                # Positive delta = feature is higher in wins
                sig = "BULLISH" if delta > 0 else "BEARISH" if delta < 0 else "-"
                print(f"  {fn:>16s}  {wavg:>+10.4f}  {lavg:>+10.4f}  {delta:>+10.4f}  {sig:>8s}")

    # ===================================================================
    # 14. CONSECUTIVE WINDOW ANALYSIS
    # ===================================================================
    print(section("POST-OUTCOME ANALYSIS"))
    print("  Win rate of trade after previous outcome:\n")
    prev_outcome = None
    after_win_results = []
    after_loss_results = []
    for t in settled_trades:
        if prev_outcome == "WIN":
            after_win_results.append(t["outcome"] == "WIN")
        elif prev_outcome == "LOSS":
            after_loss_results.append(t["outcome"] == "WIN")
        prev_outcome = t["outcome"]

    if after_win_results:
        aw_wr = sum(after_win_results) / len(after_win_results)
        print(f"  After WIN:   {fmt_pct(aw_wr)} win rate ({len(after_win_results)} trades)")
    if after_loss_results:
        al_wr = sum(after_loss_results) / len(after_loss_results)
        print(f"  After LOSS:  {fmt_pct(al_wr)} win rate ({len(after_loss_results)} trades)")

    # ===================================================================
    # 15. SUMMARY & RECOMMENDATIONS
    # ===================================================================
    print(section("RECOMMENDATIONS"))

    recs = []

    # Check if win rate is above break-even for the avg entry price
    # Break-even WR = entry_price / (1 - fee_rate * (1 - entry_price))
    if avg_entry > 0:
        be_wr = avg_entry  # Simplified: need to win at least entry_price fraction
        if win_rate > be_wr + 0.02:
            recs.append(f"  [+] Model has positive edge: WR {fmt_pct(win_rate)} > break-even ~{fmt_pct(be_wr)}")
        elif win_rate > be_wr:
            recs.append(f"  [~] Model is marginal: WR {fmt_pct(win_rate)} near break-even ~{fmt_pct(be_wr)}")
        else:
            recs.append(f"  [-] Model is underwater: WR {fmt_pct(win_rate)} < break-even ~{fmt_pct(be_wr)}")

    # Optimal threshold recommendation
    if best_threshold != 0.05:
        recs.append(f"  [!] Consider changing MIN_EDGE_THRESHOLD to {best_threshold:.0%} (currently 5%)")

    # Hour filtering
    if ranked:
        worst_h, worst_ts = ranked[-1]
        worst_wr = sum(1 for t in worst_ts if t["outcome"] == "WIN") / len(worst_ts)
        worst_pnl = sum(t["pnl"] or 0 for t in worst_ts)
        if worst_wr < 0.45 and len(worst_ts) >= 20:
            recs.append(f"  [!] Consider avoiding {worst_h:02d}:00 UTC: {fmt_pct(worst_wr)} WR, {fmt_usd(worst_pnl)} P&L over {len(worst_ts)} trades")

    # Side imbalance
    up_trades = [t for t in settled_trades if t["side"] == "UP"]
    down_trades = [t for t in settled_trades if t["side"] == "DOWN"]
    if up_trades and down_trades:
        up_wr = sum(1 for t in up_trades if t["outcome"] == "WIN") / len(up_trades)
        down_wr = sum(1 for t in down_trades if t["outcome"] == "WIN") / len(down_trades)
        if abs(up_wr - down_wr) > 0.05:
            better = "UP" if up_wr > down_wr else "DOWN"
            worse = "DOWN" if better == "UP" else "UP"
            recs.append(
                f"  [!] {better} trades ({fmt_pct(up_wr if better == 'UP' else down_wr)}) "
                f"outperform {worse} ({fmt_pct(down_wr if better == 'UP' else up_wr)}) "
                f"— consider weighting"
            )

    # Large loss streaks
    if max_loss_streak >= 8:
        recs.append(f"  [!] Max loss streak of {max_loss_streak} — consider reducing bet size or adding circuit breaker")

    if not recs:
        recs.append("  No specific recommendations — model appears well-calibrated.")

    for r in recs:
        print(r)

    print(f"\n{'=' * 60}")
    print(f"  Analysis complete: {total} trades analyzed")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "btc_edge.db"
    analyze(db_path)
