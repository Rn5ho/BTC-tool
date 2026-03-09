"""Fixed simulation with correct UTC timezone handling."""
import sqlite3
from datetime import datetime, timedelta, timezone

FEE_RATE = 0.25; FEE_EXP = 2
def fee(p): return (FEE_RATE/10000)*(p**FEE_EXP)
def pw(a,p): f=fee(p); return (a/p)*(1-f)-a
def pl(a): return -a

def utc_ts_ms(dt_str):
    """Convert 'YYYY-MM-DD HH:MM:SS' (UTC) to milliseconds correctly."""
    dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def main():
    conn = sqlite3.connect("btc_edge_live.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT created_at, side, entry_price, outcome, pnl, amount_usdc, trade_tag,
               regime_state, regime_strength, max_bid_during_window,
               settlement_price, btc_price_at_open
        FROM live_trades
        WHERE trade_tag='regime_flip' AND outcome IS NOT NULL AND success=1
        ORDER BY created_at
    """)
    trades = [dict(row) for row in c.fetchall()]

    print(f"ALL {len(trades)} REGIME FLIP TRADES -- FIXED UTC lookup")
    print("=" * 130)
    print(f"  {'UTC Time':<17} {'Traded':>6} {'Model':>6} {'BTC Open':>10} {'BTC Close':>10} {'5m Dir':>6} {'Mdl OK':>6} {'Flp OK':>6} {'Outcome':>10} {'P&L':>7}")

    model_right = 0; model_wrong = 0
    flip_right = 0; flip_wrong = 0
    model_pnl = 0.0; flip_pnl = 0.0

    for t in trades:
        created = t["created_at"]
        traded_side = t["side"]
        model_side = "DOWN" if traded_side == "UP" else "UP"
        ep = t["entry_price"]
        outcome = t["outcome"]
        pnl = t["pnl"] or 0
        amt = t["amount_usdc"] or 0

        # Correct UTC timestamp conversion
        dt = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
        dt_utc = dt.replace(tzinfo=timezone.utc)
        wmin = (dt_utc.minute // 5) * 5
        ws = dt_utc.replace(minute=wmin, second=0, microsecond=0)
        we = ws + timedelta(minutes=5)
        sms = int(ws.timestamp() * 1000)
        ems = int(we.timestamp() * 1000)

        c2 = conn.cursor()
        # Get open: first candle close in window
        c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
                   (sms, sms + 60000))
        orow = c2.fetchone()
        # Get close: last candle close in window
        c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                   (ems - 60000, ems + 60000))
        crow = c2.fetchone()

        # Prefer trade's own btc_price_at_open if available
        bo = t["btc_price_at_open"] if t["btc_price_at_open"] else (orow[0] if orow else None)
        bc = t["settlement_price"] if t["settlement_price"] else (crow[0] if crow else None)

        if bo and bc:
            went = "UP" if bc > bo else "DOWN"
            m_ok = "Y" if model_side == went else "N"
            f_ok = "Y" if traded_side == went else "N"
            if model_side == went: model_right += 1
            else: model_wrong += 1
            if traded_side == went: flip_right += 1
            else: flip_wrong += 1

            if model_side == went:
                model_pnl += pw(amt, ep)
            else:
                model_pnl += pl(amt)
        else:
            went = "??"; m_ok = "??"; f_ok = "??"

        flip_pnl += pnl

        print(f"  {created[5:19]:<17} {traded_side:>6} {model_side:>6} {bo or 0:>10,.0f} {bc or 0:>10,.0f} {went:>6} {m_ok:>6} {f_ok:>6} {outcome:>10} {pnl:>+7.1f}")

    ft = flip_right + flip_wrong
    mt = model_right + model_wrong
    print(f"\n{'='*130}")
    print(f"  CORRECTED RESULTS (UTC-safe):")
    print(f"    Flip correct:   {flip_right}/{ft} ({flip_right/ft*100:.1f}%)" if ft else "")
    print(f"    Model correct:  {model_right}/{mt} ({model_right/mt*100:.1f}%)" if mt else "")
    print(f"    Flip P&L (actual, with EE):  {flip_pnl:>+.2f}$")
    print(f"    Model P&L (would-be, no EE): {model_pnl:>+.2f}$")
    print(f"    Difference:                  {flip_pnl - model_pnl:>+.2f}$")

    # Split by period
    periods = [
        ("Mar 3 17:00-21:00", "2026-03-03 17:00", "2026-03-03 21:00"),
        ("Mar 3 21:00-Mar 4 05:00", "2026-03-03 21:00", "2026-03-04 05:00"),
        ("Mar 4 05:00-10:00 (trend)", "2026-03-04 05:00", "2026-03-04 10:00"),
        ("Mar 4 10:00-15:00", "2026-03-04 10:00", "2026-03-04 15:00"),
    ]

    print(f"\n  SPLIT BY PERIOD:")
    for label, start, end in periods:
        subset = [t for t in trades if t["created_at"] >= start and t["created_at"] < end]
        if not subset:
            continue

        s_fr = s_fw = s_mr = s_mw = 0
        s_fpnl = 0.0; s_mpnl = 0.0

        for t in subset:
            created = t["created_at"]
            traded_side = t["side"]
            model_side = "DOWN" if traded_side == "UP" else "UP"
            ep = t["entry_price"]
            pnl_val = t["pnl"] or 0
            amt = t["amount_usdc"] or 0

            dt = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
            dt_utc = dt.replace(tzinfo=timezone.utc)
            wmin = (dt_utc.minute // 5) * 5
            ws = dt_utc.replace(minute=wmin, second=0, microsecond=0)
            we = ws + timedelta(minutes=5)
            sms = int(ws.timestamp() * 1000)
            ems = int(we.timestamp() * 1000)

            c2 = conn.cursor()
            c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
                       (sms, sms + 60000))
            orow = c2.fetchone()
            c2.execute("SELECT close FROM candles WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                       (ems - 60000, ems + 60000))
            crow = c2.fetchone()

            bo = t["btc_price_at_open"] if t["btc_price_at_open"] else (orow[0] if orow else None)
            bc = t["settlement_price"] if t["settlement_price"] else (crow[0] if crow else None)

            if bo and bc:
                went = "UP" if bc > bo else "DOWN"
                if model_side == went: s_mr += 1
                else: s_mw += 1
                if traded_side == went: s_fr += 1
                else: s_fw += 1
                if model_side == went: s_mpnl += pw(amt, ep)
                else: s_mpnl += pl(amt)

            s_fpnl += pnl_val

        sft = s_fr + s_fw; smt = s_mr + s_mw
        print(f"\n    {label}: {len(subset)} flips")
        if sft:
            print(f"      Flip: {s_fr}/{sft} = {s_fr/sft*100:.0f}%    Model: {s_mr}/{smt} = {s_mr/smt*100:.0f}%")
        print(f"      Flip P&L: {s_fpnl:>+.2f}$    Model P&L: {s_mpnl:>+.2f}$    Delta: {s_fpnl-s_mpnl:>+.2f}$")

    conn.close()

if __name__ == "__main__":
    main()
