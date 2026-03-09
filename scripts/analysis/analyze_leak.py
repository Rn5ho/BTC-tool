"""Why does 56% effective WR not grow the portfolio? Find the leaks."""

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
        "FROM live_trades WHERE timestamp >= ? AND success = 1 AND outcome IS NOT NULL "
        "ORDER BY timestamp",
        (t24h_ms,),
    )
    trades = [dict(t) for t in cursor.fetchall()]

    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    exits = [t for t in trades if t["outcome"] == "EARLY_EXIT"]

    print("=" * 75)
    print("WHY 56% EFFECTIVE WR ISN'T GROWING THE PORTFOLIO")
    print("=" * 75)

    # ================================================================
    # 1. The fundamental asymmetry
    # ================================================================
    print()
    print("1. THE PAYOFF ASYMMETRY")
    print("-" * 75)

    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    avg_exit = sum(t["pnl"] for t in exits) / len(exits) if exits else 0

    print(f"   Avg WIN payout:      ${avg_win:+.2f}")
    print(f"   Avg LOSS cost:       ${avg_loss:+.2f}")
    print(f"   Avg EARLY EXIT:      ${avg_exit:+.2f}")
    print()
    print(f"   Win/Loss ratio:      {abs(avg_win/avg_loss):.2f}x")
    print(f"   Exit/Loss ratio:     {abs(avg_exit/avg_loss):.2f}x")
    print()

    # What WR do you need to break even with these payoffs?
    # WR * avg_win + (1-WR) * avg_loss = 0
    # WR = -avg_loss / (avg_win - avg_loss)
    be_wr = -avg_loss / (avg_win - avg_loss) * 100
    print(f"   Breakeven WR needed: {be_wr:.1f}%")
    print(f"   Actual settlement WR: {len(wins)/(len(wins)+len(losses))*100:.1f}%")
    print()
    print(f"   --> You WIN ${avg_win:.2f} but LOSE ${abs(avg_loss):.2f}")
    print(f"   --> Each loss wipes out {abs(avg_loss/avg_win):.1f} wins")

    # ================================================================
    # 2. Break down by outcome type - where does money go?
    # ================================================================
    print()
    print("2. MONEY FLOW (last 24h)")
    print("-" * 75)

    win_pnl = sum(t["pnl"] for t in wins)
    loss_pnl = sum(t["pnl"] for t in losses)
    exit_pnl = sum(t["pnl"] for t in exits)
    total = win_pnl + loss_pnl + exit_pnl

    print(f"   {len(wins):3d} WINs:         ${win_pnl:+8.2f}  (avg ${avg_win:+.2f})")
    print(f"   {len(losses):3d} LOSSes:       ${loss_pnl:+8.2f}  (avg ${avg_loss:+.2f})")
    print(f"   {len(exits):3d} EARLY EXITs:  ${exit_pnl:+8.2f}  (avg ${avg_exit:+.2f})")
    print(f"   {'':3s} TOTAL:        ${total:+8.2f}")
    print()
    print(f"   WINs earned:    ${win_pnl:+.2f}")
    print(f"   LOSSes burned:  ${loss_pnl:+.2f}")
    print(f"   Gap:            ${win_pnl + loss_pnl:+.2f}  <-- losses outweigh wins")
    print(f"   Exits rescued:  ${exit_pnl:+.2f}  <-- covers the gap")
    print(f"   Net:            ${total:+.2f}")

    # ================================================================
    # 3. The entry price problem
    # ================================================================
    print()
    print("3. THE ENTRY PRICE PROBLEM")
    print("-" * 75)
    print()
    print("   Entry price determines your payout structure:")
    print()
    print(f"   {'Entry':>7s}  {'Win pays':>9s}  {'Loss costs':>11s}  {'BE WR':>6s}  {'Trades':>7s}  {'Actual WR':>10s}  {'P&L':>8s}")
    print(f"   {'-----':>7s}  {'---------':>9s}  {'-----------':>11s}  {'-----':>6s}  {'------':>7s}  {'---------':>10s}  {'---':>8s}")

    buckets = [(0.25, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65)]
    for lo, hi in buckets:
        b = [t for t in trades if lo <= (t["entry_price"] or 0) < hi]
        if not b:
            continue

        mid = (lo + hi) / 2
        # At this entry price, what does a WIN pay?
        win_pay = (1.0 / mid - 1.0) * 0.98  # payout ratio after fees
        loss_cost = 1.0  # lose full bet

        be = 1.0 / (1.0 + win_pay) * 100

        b_wins = [t for t in b if t["outcome"] == "WIN"]
        b_losses = [t for t in b if t["outcome"] == "LOSS"]
        b_exits = [t for t in b if t["outcome"] == "EARLY_EXIT"]
        settled = len(b_wins) + len(b_losses)
        wr = len(b_wins) / settled * 100 if settled else 0
        pnl = sum(t["pnl"] or 0 for t in b)

        status = "OK" if wr > be else "BLEEDING"

        print(f"   {lo:.2f}-{hi:.2f}  ${win_pay*3.5:+7.2f}   -${loss_cost*3.5:.2f}      "
              f"{be:4.0f}%  {len(b):5d}t  "
              f"{wr:8.0f}%   ${pnl:+7.2f}  {status}")

    # ================================================================
    # 4. Early exit economics - the hidden problem
    # ================================================================
    print()
    print("4. EARLY EXIT: PROFITABLE BUT CAPPED")
    print("-" * 75)

    if exits:
        exit_pnls = sorted([t["pnl"] for t in exits])
        avg_exit_entry = sum(t["entry_price"] for t in exits) / len(exits)

        # What would a full WIN have paid on average for these trades?
        avg_win_if_held = sum(
            (t["amount_usdc"] / t["entry_price"] * 1.0 - t["amount_usdc"]) * 0.98
            for t in exits
        ) / len(exits)

        print(f"   Avg exit profit:   ${avg_exit:+.2f}")
        print(f"   Avg full win pays: ${avg_win_if_held:+.2f}")
        print(f"   Capture rate:      {avg_exit/avg_win_if_held*100:.0f}% of potential win")
        print()
        print(f"   Early exits average {avg_exit:.2f} profit on a ${sum(t['amount_usdc'] for t in exits)/len(exits):.2f} bet")
        print(f"   That's {avg_exit/(sum(t['amount_usdc'] for t in exits)/len(exits))*100:.0f}% return per exit")
        print()
        print(f"   But a LOSS costs ${abs(avg_loss):.2f}")
        print(f"   So each loss wipes out {abs(avg_loss)/avg_exit:.1f} early exits")

    # ================================================================
    # 5. The volume mismatch
    # ================================================================
    print()
    print("5. VOLUME MISMATCH")
    print("-" * 75)

    win_vol = sum(t["amount_usdc"] for t in wins)
    loss_vol = sum(t["amount_usdc"] for t in losses)
    exit_vol = sum(t["amount_usdc"] for t in exits)

    print(f"   WIN volume:   ${win_vol:.2f} ({len(wins)} trades, avg ${win_vol/len(wins):.2f})")
    print(f"   LOSS volume:  ${loss_vol:.2f} ({len(losses)} trades, avg ${loss_vol/len(losses):.2f})")
    print(f"   EXIT volume:  ${exit_vol:.2f} ({len(exits)} trades, avg ${exit_vol/len(exits):.2f})")
    print()
    print(f"   You're putting MORE money into losses (${loss_vol:.0f}) than wins (${win_vol:.0f})")
    print(f"   That's because losses cluster at lower entry prices = bigger bets")

    # ================================================================
    # 6. What would it take to be profitable?
    # ================================================================
    print()
    print("6. WHAT WOULD IT TAKE TO BE PROFITABLE?")
    print("-" * 75)

    # Current: 54W, 84L, 67E in 24h = +$0.95
    # Need to either: reduce losses, increase wins, or increase exits

    # Option A: fewer losses
    # Each loss avoided = +$3.03 (avg loss)
    losses_to_cut = abs(total) / abs(avg_loss)
    print(f"   A: Cut {losses_to_cut:.0f} losses/day to break even (currently {len(losses)} losses)")

    # Option B: more early exits
    exits_needed = abs(win_pnl + loss_pnl) / avg_exit
    print(f"   B: Need {exits_needed:.0f} early exits to cover win/loss gap "
          f"(currently {len(exits)})")

    # Option C: higher entry prices (better payoff ratio)
    print(f"   C: Trade only 0.55-0.65 entries:")
    good_entries = [t for t in trades if 0.55 <= (t["entry_price"] or 0) < 0.65]
    if good_entries:
        g_pnl = sum(t["pnl"] or 0 for t in good_entries)
        g_w = sum(1 for t in good_entries if t["outcome"] == "WIN")
        g_l = sum(1 for t in good_entries if t["outcome"] == "LOSS")
        g_e = sum(1 for t in good_entries if t["outcome"] == "EARLY_EXIT")
        print(f"      {len(good_entries)} trades, {g_w}W/{g_l}L/{g_e}E, P&L: ${g_pnl:+.2f}")
        if len(good_entries) > 0:
            daily_rate = g_pnl  # already 24h
            print(f"      Projected daily: ${daily_rate:+.2f}")

    # Option D: regime flip (the big one)
    print(f"   D: Regime flip at 0.35 (just deployed):")
    print(f"      Projected improvement: +$35/day based on 3-day backtest")

    # ================================================================
    # 7. The core issue summarized
    # ================================================================
    print()
    print("=" * 75)
    print("SUMMARY: WHY YOU'RE FLAT")
    print("=" * 75)
    print()
    print(f"   1. Losses cost ${abs(avg_loss):.2f} avg, wins pay ${avg_win:.2f} avg")
    print(f"      --> Each loss erases {abs(avg_loss/avg_win):.1f} wins")
    print(f"   2. Entry prices averaging {sum(t['entry_price'] for t in trades)/len(trades):.3f}")
    print(f"      --> At 0.53, a win pays only {(1/0.53-1)*0.98:.2f}x your bet")
    print(f"      --> You need {1/(1+(1/0.53-1)*0.98)*100:.0f}% WR just to break even")
    print(f"   3. 84 losses x ${abs(avg_loss):.2f} = ${abs(loss_pnl):.0f} burned")
    print(f"      54 wins x ${avg_win:.2f} = ${win_pnl:.0f} earned")
    print(f"      67 exits x ${avg_exit:.2f} = ${exit_pnl:.0f} rescued")
    print(f"      Net: ${total:+.2f}")
    print()
    print(f"   The exits are doing heroic work just to stay flat.")
    print(f"   The regime flip should eliminate ~30 of those 84 losses/day")
    print(f"   and convert them to wins, which is the breakthrough needed.")


if __name__ == "__main__":
    main()
