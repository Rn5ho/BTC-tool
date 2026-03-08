"""Analyze last 24h performance with regime/trend breakdown."""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = "btc_edge_analysis.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    now = int(datetime.now(timezone.utc).timestamp())
    t24h_ms = (now - 86400) * 1000

    cursor = conn.execute(
        "SELECT id, timestamp, side, amount_usdc, entry_price, outcome, pnl, "
        "trade_tag, regime_state, regime_strength "
        "FROM live_trades "
        "WHERE timestamp >= ? AND success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp",
        (t24h_ms,),
    )
    trades = [dict(t) for t in cursor.fetchall()]

    print("LAST 24h PERFORMANCE (since early exit deployed)")
    print("=" * 70)

    wins = sum(1 for t in trades if t["outcome"] in ("WIN", "EARLY_EXIT"))
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    total_pnl = sum(t["pnl"] or 0 for t in trades)

    s_wins = sum(1 for t in trades if t["outcome"] == "WIN")
    s_losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    exits = sum(1 for t in trades if t["outcome"] == "EARLY_EXIT")

    print(f"  All settled: {len(trades)} trades ({s_wins}W / {s_losses}L / {exits}E)")
    print(f"  Effective WR (exit=win): {wins}/{wins + losses} = {wins / (wins + losses) * 100:.1f}%")
    print(f"  Settlement-only WR: {s_wins}/{s_wins + s_losses} = {s_wins / (s_wins + s_losses) * 100:.1f}%")
    print(f"  Total P&L: ${total_pnl:+.2f}")
    print()

    # ---- Regime breakdown ----
    print("REGIME BREAKDOWN (last 24h)")
    print("=" * 70)
    by_regime = defaultdict(list)
    for t in trades:
        regime = t["regime_state"] or "unknown"
        by_regime[regime].append(t)

    for regime in sorted(by_regime.keys()):
        tlist = by_regime[regime]
        w = sum(1 for t in tlist if t["outcome"] == "WIN")
        l = sum(1 for t in tlist if t["outcome"] == "LOSS")
        e = sum(1 for t in tlist if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in tlist)
        settled_r = w + l
        wr = w / settled_r * 100 if settled_r else 0
        eff_wr = (w + e) / (w + l + e) * 100 if (w + l + e) else 0
        print(
            f"  {regime:15s}: {len(tlist):3d} trades | "
            f"{w}W/{l}L/{e}E | Settle WR: {wr:.0f}% | "
            f"Eff WR: {eff_wr:.0f}% | P&L: ${pnl:+.2f}"
        )

    print()

    # ---- Trend alignment vs conflict ----
    print("TREND ALIGNMENT vs CONFLICT (last 24h)")
    print("=" * 70)
    for t in trades:
        regime = t["regime_state"] or "unknown"
        side = t["side"]
        if regime == "trending_up":
            t["alignment"] = "aligned" if side == "UP" else "conflict"
        elif regime == "trending_down":
            t["alignment"] = "aligned" if side == "DOWN" else "conflict"
        else:
            t["alignment"] = "ranging"

    for alignment in ["ranging", "aligned", "conflict"]:
        group = [t for t in trades if t.get("alignment") == alignment]
        if not group:
            continue
        w = sum(1 for t in group if t["outcome"] == "WIN")
        l = sum(1 for t in group if t["outcome"] == "LOSS")
        e = sum(1 for t in group if t["outcome"] == "EARLY_EXIT")
        pnl = sum(t["pnl"] or 0 for t in group)
        settled_g = w + l
        wr = w / settled_g * 100 if settled_g else 0
        eff_wr = (w + e) / (w + l + e) * 100 if (w + l + e) else 0
        print(
            f"  {alignment:10s}: {len(group):3d} trades | "
            f"{w}W/{l}L/{e}E | Settle WR: {wr:.0f}% | "
            f"Eff WR: {eff_wr:.0f}% | P&L: ${pnl:+.2f}"
        )

    print()

    # ---- Hypothetical: exclude conflict trades ----
    conflict = [t for t in trades if t.get("alignment") == "conflict"]
    non_conflict = [t for t in trades if t.get("alignment") != "conflict"]

    if conflict:
        print("HYPOTHETICAL: EXCLUDE TREND-CONFLICT TRADES")
        print("=" * 70)
        nc_w = sum(1 for t in non_conflict if t["outcome"] == "WIN")
        nc_l = sum(1 for t in non_conflict if t["outcome"] == "LOSS")
        nc_e = sum(1 for t in non_conflict if t["outcome"] == "EARLY_EXIT")
        nc_pnl = sum(t["pnl"] or 0 for t in non_conflict)
        nc_wr = nc_w / (nc_w + nc_l) * 100 if (nc_w + nc_l) else 0
        nc_eff = (nc_w + nc_e) / (nc_w + nc_l + nc_e) * 100 if (nc_w + nc_l + nc_e) else 0

        c_w = sum(1 for t in conflict if t["outcome"] == "WIN")
        c_l = sum(1 for t in conflict if t["outcome"] == "LOSS")
        c_e = sum(1 for t in conflict if t["outcome"] == "EARLY_EXIT")
        c_pnl = sum(t["pnl"] or 0 for t in conflict)
        c_wr = c_w / (c_w + c_l) * 100 if (c_w + c_l) else 0

        print(f"  Conflict trades (would skip):")
        print(f"    {len(conflict)} trades | {c_w}W/{c_l}L/{c_e}E | WR: {c_wr:.0f}% | P&L: ${c_pnl:+.2f}")
        print()
        print(f"  Without conflict:")
        print(f"    {len(non_conflict)} trades | {nc_w}W/{nc_l}L/{nc_e}E")
        print(f"    Settle WR: {nc_wr:.0f}% | Eff WR: {nc_eff:.0f}%")
        print(f"    P&L: ${nc_pnl:+.2f}")
        print(f"    Improvement: ${nc_pnl - total_pnl:+.2f} by skipping {len(conflict)} trades")
    else:
        print("  No trend-conflict trades found in last 24h")

    print()

    # ---- Also exclude all trending trades ----
    trending = [t for t in trades if t["regime_state"] in ("trending_up", "trending_down")]
    ranging_only = [t for t in trades if t["regime_state"] not in ("trending_up", "trending_down")]

    if trending:
        print("HYPOTHETICAL: ONLY TRADE IN RANGING REGIME")
        print("=" * 70)
        r_w = sum(1 for t in ranging_only if t["outcome"] == "WIN")
        r_l = sum(1 for t in ranging_only if t["outcome"] == "LOSS")
        r_e = sum(1 for t in ranging_only if t["outcome"] == "EARLY_EXIT")
        r_pnl = sum(t["pnl"] or 0 for t in ranging_only)
        r_wr = r_w / (r_w + r_l) * 100 if (r_w + r_l) else 0
        r_eff = (r_w + r_e) / (r_w + r_l + r_e) * 100 if (r_w + r_l + r_e) else 0

        t_w = sum(1 for t in trending if t["outcome"] == "WIN")
        t_l = sum(1 for t in trending if t["outcome"] == "LOSS")
        t_e = sum(1 for t in trending if t["outcome"] == "EARLY_EXIT")
        t_pnl = sum(t["pnl"] or 0 for t in trending)
        t_wr = t_w / (t_w + t_l) * 100 if (t_w + t_l) else 0

        print(f"  Trending trades (would skip):")
        print(f"    {len(trending)} trades | {t_w}W/{t_l}L/{t_e}E | WR: {t_wr:.0f}% | P&L: ${t_pnl:+.2f}")
        print()
        print(f"  Ranging only:")
        print(f"    {len(ranging_only)} trades | {r_w}W/{r_l}L/{r_e}E")
        print(f"    Settle WR: {r_wr:.0f}% | Eff WR: {r_eff:.0f}%")
        print(f"    P&L: ${r_pnl:+.2f}")
        print(f"    Improvement: ${r_pnl - total_pnl:+.2f} by skipping {len(trending)} trades")

    print()

    # ---- Regime flip trades specifically ----
    print("REGIME FLIP TRADES")
    print("=" * 70)
    flips = [t for t in trades if t["trade_tag"] == "regime_flip"]
    if flips:
        f_w = sum(1 for t in flips if t["outcome"] == "WIN")
        f_l = sum(1 for t in flips if t["outcome"] == "LOSS")
        f_e = sum(1 for t in flips if t["outcome"] == "EARLY_EXIT")
        f_pnl = sum(t["pnl"] or 0 for t in flips)
        f_wr = f_w / (f_w + f_l) * 100 if (f_w + f_l) else 0
        print(f"  {len(flips)} trades | {f_w}W/{f_l}L/{f_e}E | WR: {f_wr:.0f}% | P&L: ${f_pnl:+.2f}")
        for t in flips:
            ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M")
            print(
                f"    #{t['id']:3d} {ts} {t['side']:4s} entry={t['entry_price']:.3f} "
                f"regime={t['regime_state']} str={t['regime_strength']:.2f} "
                f"-> {t['outcome']} ${t['pnl']:+.2f}"
            )
    else:
        print("  No regime_flip trades in last 24h")


if __name__ == "__main__":
    main()
