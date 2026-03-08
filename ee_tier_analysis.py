"""One-off: Analyze early exit rescues/regrets by entry price tier."""
import sqlite3

conn = sqlite3.connect("btc_edge.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT entry_price, exit_threshold_used, would_have_won, pnl, amount_usdc
    FROM live_trades WHERE outcome = 'EARLY_EXIT' AND would_have_won IS NOT NULL
""").fetchall()

tiers = {"<0.35": [], "0.35-0.40": [], "0.40-0.50": [], ">=0.50": []}
for r in rows:
    ep = r["entry_price"] or 0
    if ep < 0.35:
        tiers["<0.35"].append(r)
    elif ep < 0.40:
        tiers["0.35-0.40"].append(r)
    elif ep < 0.50:
        tiers["0.40-0.50"].append(r)
    else:
        tiers[">=0.50"].append(r)

print("=" * 70)
print("EARLY EXIT ANALYSIS BY TIER")
print("=" * 70)

for tier, trades in tiers.items():
    if not trades:
        print(f"\n{tier}: no trades")
        continue
    n = len(trades)
    rescues = sum(1 for t in trades if not t["would_have_won"])
    regrets = sum(1 for t in trades if t["would_have_won"])
    total_pnl = sum(t["pnl"] or 0 for t in trades)

    # What would have happened at settlement (no EE)
    hypothetical_pnl = 0.0
    for t in trades:
        ep = t["entry_price"]
        amt = t["amount_usdc"]
        if not ep or ep <= 0:
            continue
        if t["would_have_won"]:
            shares = amt / ep  # simplified, ignoring fees for comparison
            hypothetical_pnl += shares - amt
        else:
            hypothetical_pnl -= amt

    thresholds = sorted(set(t["exit_threshold_used"] for t in trades if t["exit_threshold_used"]))
    th_str = "/".join(f"{t:.2f}" for t in thresholds)

    print(f"\n{tier} (threshold={th_str}): {n} trades")
    print(f"  Rescues (would have LOST): {rescues} ({rescues/n*100:.0f}%)")
    print(f"  Regrets (would have WON):  {regrets} ({regrets/n*100:.0f}%)")
    print(f"  EE P&L:         ${total_pnl:+.2f}  (avg ${total_pnl/n:+.2f})")
    print(f"  Settlement P&L: ${hypothetical_pnl:+.2f}  (avg ${hypothetical_pnl/n:+.2f})")
    print(f"  EE advantage:   ${total_pnl - hypothetical_pnl:+.2f}")

    # Show rescue savings vs regret cost
    rescue_savings = sum(t["amount_usdc"] for t in trades if not t["would_have_won"])
    regret_cost = sum(
        ((t["amount_usdc"] / t["entry_price"]) - t["amount_usdc"]) - (t["pnl"] or 0)
        for t in trades
        if t["would_have_won"] and t["entry_price"] and t["entry_price"] > 0
    )
    print(f"  Rescue saved:   ${rescue_savings:+.2f} (avoided full losses)")
    print(f"  Regret cost:    ${regret_cost:+.2f} (missed settlement upside vs EE pnl)")

print("\n" + "=" * 70)
conn.close()
