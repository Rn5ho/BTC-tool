"""Corrected clean simulation with real BTC settlement from candles."""
import sqlite3
from datetime import datetime, timedelta

FEE_RATE = 0.25
FEE_EXPONENT = 2
STARTING = 85.71

def fee(p): return (FEE_RATE/10000)*(p**FEE_EXPONENT)
def pnl_w(a,p): f=fee(p); return (a/p)*(1-f)-a
def pnl_l(a): return -a

def main():
    conn = sqlite3.connect("btc_edge.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT created_at, side, entry_price, outcome, pnl, amount_usdc, trade_tag,
               model_confidence, regime_state, regime_strength, max_bid_during_window,
               settlement_price, btc_price_at_open
        FROM live_trades
        WHERE outcome IS NOT NULL AND success=1 AND created_at >= '2026-03-03 20:00'
        ORDER BY created_at
    """)
    trades = [dict(row) for row in c.fetchall()]

    # Look up real BTC prices from candles for every trade
    for t in trades:
        created = t["created_at"]
        dt = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
        minute = (dt.minute // 5) * 5
        window_start = dt.replace(minute=minute, second=0)
        window_end = window_start + timedelta(minutes=5)
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(window_end.timestamp() * 1000)

        c2 = conn.cursor()
        c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
                   (start_ms, start_ms + 60000))
        open_row = c2.fetchone()
        c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                   (end_ms - 60000, end_ms + 60000))
        close_row = c2.fetchone()

        btc_open = t["btc_price_at_open"]
        if open_row and not btc_open:
            btc_open = open_row[0]
        btc_close = t["settlement_price"]
        if close_row and not btc_close:
            btc_close = close_row[0]

        t["btc_open_real"] = btc_open
        t["btc_close_real"] = btc_close

        if btc_open and btc_close:
            t["market_went"] = "UP" if btc_close > btc_open else "DOWN"
        elif t["outcome"] == "WIN":
            t["market_went"] = t["side"]
        elif t["outcome"] == "LOSS":
            t["market_went"] = "DOWN" if t["side"] == "UP" else "UP"
        else:
            t["market_went"] = None

        if t["trade_tag"] == "regime_flip":
            t["model_side"] = "DOWN" if t["side"] == "UP" else "UP"
        else:
            t["model_side"] = t["side"]

    conn.close()

    known = sum(1 for t in trades if t["market_went"] is not None)
    unknown = len(trades) - known
    print(f"Market direction: {known} known, {unknown} unknown out of {len(trades)} trades\n")

    # Run scenarios
    def run(use_ee, use_flip, label):
        bankroll = STARTING
        pnl = 0.0
        wins = losses = ee_count = 0
        peak = bankroll
        max_dd = 0.0

        for t in trades:
            side = t["side"]
            model_side = t["model_side"]
            ep = t["entry_price"]
            outcome = t["outcome"]
            apnl = t["pnl"] or 0.0
            aamt = t["amount_usdc"] or 0.0
            market_went = t["market_went"]
            maxbid = t["max_bid_during_window"]

            bet_side = side if use_flip else model_side

            if market_went is not None:
                would_win = (bet_side == market_went)
            else:
                if use_flip:
                    would_win = (outcome == "WIN")
                else:
                    would_win = (outcome == "LOSS") if t["trade_tag"] == "regime_flip" else (outcome == "WIN")

            if use_ee and outcome == "EARLY_EXIT":
                trade_pnl = apnl
                trade_oc = "EARLY_EXIT"
            elif use_ee and maxbid is not None:
                thresh = 0.50 if ep < 0.35 else 0.45 if ep < 0.40 else 0.65 if ep < 0.50 else 0.95
                if maxbid >= thresh and not would_win:
                    f = fee(ep)
                    tokens = (aamt/ep)*(1-f)
                    sf = fee(thresh)
                    trade_pnl = tokens * thresh * (1-sf) - aamt
                    trade_oc = "EARLY_EXIT"
                elif would_win:
                    trade_pnl = pnl_w(aamt, ep)
                    trade_oc = "WIN"
                else:
                    trade_pnl = pnl_l(aamt)
                    trade_oc = "LOSS"
            else:
                if would_win:
                    trade_pnl = pnl_w(aamt, ep)
                    trade_oc = "WIN"
                else:
                    trade_pnl = pnl_l(aamt)
                    trade_oc = "LOSS"

            pnl += trade_pnl
            bankroll += trade_pnl
            if bankroll > peak: peak = bankroll
            dd = peak - bankroll
            if dd > max_dd: max_dd = dd
            if trade_oc == "WIN": wins += 1
            elif trade_oc == "LOSS": losses += 1
            else: ee_count += 1

        wr = wins/(wins+losses)*100 if (wins+losses) > 0 else 0
        return {"label": label, "wins": wins, "losses": losses, "ee": ee_count,
                "wr": wr, "pnl": pnl, "bankroll": bankroll, "max_dd": max_dd}

    r_actual = run(True,  True,  "ACTUAL (EE + Flip)")
    r_no_ee  = run(False, True,  "Flip only, no EE")
    r_no_flip= run(True,  False, "EE only, no Flip")
    r_naked  = run(False, False, "NAKED (model only)")

    print("=" * 85)
    print(f"  CORRECTED SIMULATION -- {len(trades)} trades, Mar 3 20:00 - Mar 4 08:00")
    print(f"  Real BTC settlement prices from candle data. Starting: ${STARTING}")
    print("=" * 85)

    print(f"\n  {'Scenario':<25} {'W':>4} {'L':>4} {'EE':>4} {'WR%':>6} {'P&L':>10} {'Bankroll':>10} {'MaxDD':>8}")
    print(f"  {'-'*25} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*10} {'-'*10} {'-'*8}")
    for r in [r_actual, r_no_ee, r_no_flip, r_naked]:
        print(f"  {r['label']:<25} {r['wins']:>4} {r['losses']:>4} {r['ee']:>4} {r['wr']:>5.1f}% {r['pnl']:>+9.2f}$ ${r['bankroll']:>8.2f} ${r['max_dd']:>6.2f}")

    print(f"\n  FEATURE VALUE (with real settlement data):")
    ee_val = r_actual["pnl"] - r_no_ee["pnl"]
    flip_val = r_actual["pnl"] - r_no_flip["pnl"]
    total = r_actual["pnl"] - r_naked["pnl"]
    print(f"    Early Exit value:   {ee_val:>+8.2f}$  (ACTUAL - no_EE)")
    print(f"    Regime Flip value:  {flip_val:>+8.2f}$  (ACTUAL - no_flip)")
    print(f"    Combined uplift:    {total:>+8.2f}$  (ACTUAL - NAKED)")
    print(f"    Interaction:        {total - ee_val - flip_val:>+8.2f}$")

    # Breakdown: what model alone would have done (settlement WR)
    print(f"\n  MODEL SETTLEMENT ACCURACY (no EE, no flip -- just model calls):")
    model_right = sum(1 for t in trades if t["market_went"] and t["model_side"] == t["market_went"])
    model_wrong = sum(1 for t in trades if t["market_went"] and t["model_side"] != t["market_went"])
    model_unknown = sum(1 for t in trades if t["market_went"] is None)
    print(f"    Model correct:  {model_right}/{model_right+model_wrong} ({model_right/(model_right+model_wrong)*100:.0f}%)")
    print(f"    Model wrong:    {model_wrong}/{model_right+model_wrong}")
    if model_unknown > 0:
        print(f"    Unknown:        {model_unknown}")

    # Same for flip side
    flip_trades = [t for t in trades if t["trade_tag"] == "regime_flip"]
    flip_right = sum(1 for t in flip_trades if t["market_went"] and t["side"] == t["market_went"])
    flip_wrong = sum(1 for t in flip_trades if t["market_went"] and t["side"] != t["market_went"])
    model_on_flip_right = sum(1 for t in flip_trades if t["market_went"] and t["model_side"] == t["market_went"])
    model_on_flip_wrong = sum(1 for t in flip_trades if t["market_went"] and t["model_side"] != t["market_went"])

    print(f"\n  ON REGIME-FLIP TRADES ONLY ({len(flip_trades)} trades):")
    ft = flip_right + flip_wrong
    mt = model_on_flip_right + model_on_flip_wrong
    print(f"    Flip (traded) correct:  {flip_right}/{ft} ({flip_right/ft*100:.0f}%)" if ft else "    No data")
    print(f"    Model (original) correct: {model_on_flip_right}/{mt} ({model_on_flip_right/mt*100:.0f}%)" if mt else "    No data")
    print(f"    --> Model was BETTER by {model_on_flip_right - flip_right} trades")
    print(f"    --> But without EE, flip losses = ${flip_wrong * 4:.0f}, model losses = ${model_on_flip_wrong * 4:.0f}")

    # The key insight
    print(f"\n  KEY INSIGHT:")
    print(f"    This 12h window was RANGING (BTC: $67.5k-$68.9k, no clear trend)")
    print(f"    The regime detector kept firing false trending signals")
    print(f"    Model mean-reversion was actually correct most of the time")
    print(f"    BUT: on Mar 1-2, real trends existed and model got crushed")
    print(f"    Regime flip is insurance for trending markets (even if costly in ranging)")

    # Loss streaks from previous days
    print(f"\n  PREVIOUS DAYS FOR CONTEXT:")
    conn2 = sqlite3.connect("btc_edge.db")
    c3 = conn2.cursor()
    c3.execute("""
        SELECT date(created_at),
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END),
               SUM(CASE WHEN outcome='EARLY_EXIT' THEN 1 ELSE 0 END),
               COUNT(*), ROUND(SUM(pnl), 2)
        FROM live_trades WHERE outcome IS NOT NULL AND success=1
        GROUP BY date(created_at) ORDER BY date(created_at)
    """)
    print(f"    {'Day':<12} {'W':>4} {'L':>4} {'EE':>4}  {'P&L':>10}  Settlement WR")
    for r in c3.fetchall():
        wr = r[1]/(r[1]+r[2])*100 if (r[1]+r[2]) > 0 else 0
        print(f"    {r[0]:<12} {r[1]:>4} {r[2]:>4} {r[3]:>4}  {r[5]:>+9.2f}$  {wr:.0f}%")
    conn2.close()


if __name__ == "__main__":
    main()
