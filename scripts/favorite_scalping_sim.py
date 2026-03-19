"""Heavy Favorite Scalping Strategy Simulation

Tests two variants using real market snapshot and candle data:
- Variant A: Buy favorite (>= threshold), hold to resolution
- Variant B: Buy favorite, exit on tick-up, fallback to resolution

Uses $100 starting capital with Polymarket fee model.

Column mapping from SELECT:
  0=slug, 1=timestamp, 2=up_price, 3=down_price, 4=btc_price,
  5=up_best_bid, 6=up_best_ask, 7=down_best_bid, 8=down_best_ask
"""

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DB_PATH = "btc_edge.db"

# Column indices from the SELECT query
SL, TS, UP, DN, BTC, UBB, UBA, DBB, DBA = range(9)


def fee_factor(price: float) -> float:
    """Polymarket taker fee: fee_rate * (p * (1-p))^2"""
    return 0.25 * (price * (1 - price)) ** 2


def shares_bought(usdc: float, price: float) -> float:
    return (usdc / price) * (1 - fee_factor(price))


def pnl_win(usdc: float, price: float) -> float:
    return shares_bought(usdc, price) * 1.0 - usdc


def pnl_loss(usdc: float, price: float) -> float:
    return -usdc


def pnl_early_exit(usdc: float, entry_price: float, exit_price: float) -> float:
    shares = shares_bought(usdc, entry_price)
    sell_ff = fee_factor(exit_price)
    proceeds = shares * exit_price * (1 - sell_ff)
    return proceeds - usdc


def determine_window_winner(slug: str, candles_by_window: dict) -> str | None:
    """UP wins if BTC close > open over the 5-min window. Flat = DOWN."""
    try:
        window_start_s = int(slug.split("-")[-1])
    except (ValueError, IndexError):
        return None

    window_start_ms = window_start_s * 1000
    candles = candles_by_window.get(window_start_ms)
    if not candles:
        return None

    open_price = candles[0][0]
    close_price = candles[-1][1]
    return "UP" if close_price > open_price else "DOWN"


def get_entry_price(snap, side: str) -> float:
    """Buy at ask price (realistic entry). Fallback to mid price."""
    if side == "UP":
        return snap[UBA] if snap[UBA] else (snap[UP] or 0)
    else:
        return snap[DBA] if snap[DBA] else (snap[DN] or 0)


def get_bid_price(snap, side: str) -> float:
    """Sell at bid price (realistic exit)."""
    if side == "UP":
        return snap[UBB] if snap[UBB] else (snap[UP] or 0)
    else:
        return snap[DBB] if snap[DBB] else (snap[DN] or 0)


def get_mid_price(snap, side: str) -> float:
    """Mid-market price for the given side."""
    if side == "UP":
        return snap[UP] or 0
    else:
        return snap[DN] or 0


def run_simulation():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("=" * 80)
    print("HEAVY FAVORITE SCALPING STRATEGY SIMULATION")
    print("Starting capital: $100  |  Bet size: $5  |  Polymarket fees included")
    print("=" * 80)

    # ---- Load candle data for winner determination ----
    print("\nLoading data...")
    c.execute("SELECT timestamp, open, close FROM candles ORDER BY timestamp")
    all_candles = c.fetchall()

    candles_by_window = defaultdict(list)
    for ts, o, cl in all_candles:
        window_start = (ts // 300_000) * 300_000
        candles_by_window[window_start].append((o, cl))

    # ---- Load market snapshots grouped by window ----
    c.execute("""
        SELECT slug, timestamp, up_price, down_price, btc_price,
               up_best_bid, up_best_ask, down_best_bid, down_best_ask
        FROM market_snapshots
        ORDER BY slug, timestamp
    """)
    snapshots = c.fetchall()

    windows = defaultdict(list)
    for row in snapshots:
        windows[row[SL]].append(row)

    # Count how many have bid/ask data
    has_book = sum(1 for row in snapshots if row[UBB] is not None)

    print(f"  {len(all_candles):,} candles | {len(snapshots):,} snapshots | {len(windows):,} windows")
    print(f"  Snapshots with bid/ask data: {has_book:,} ({has_book/len(snapshots)*100:.0f}%)")

    # Date range
    c.execute("SELECT MIN(created_at), MAX(created_at) FROM market_snapshots")
    mn, mx = c.fetchone()
    print(f"  Date range: {mn} to {mx}")

    # =====================================================
    # CALIBRATION CHECK: Is pricing efficient?
    # =====================================================
    print("\n" + "=" * 80)
    print("CALIBRATION CHECK: Does price X% resolve correctly X% of the time?")
    print("=" * 80)

    buckets = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
               (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90),
               (0.90, 0.95), (0.95, 1.00)]

    print(f"\n  {'Price Bucket':>14}  {'Win/Total':>10}  {'Actual WR':>9}  {'Expected':>8}  {'Edge':>7}  {'$5 EV':>7}")
    print(f"  {'-'*14}  {'-'*10}  {'-'*9}  {'-'*8}  {'-'*7}  {'-'*7}")

    for lo, hi in buckets:
        total = 0
        correct = 0

        for slug, snaps in windows.items():
            if not snaps:
                continue
            # Use mid-window snapshot for better representation
            mid_idx = len(snaps) // 4  # ~1 min into 5-min window
            mid = snaps[min(mid_idx, len(snaps) - 1)]
            up_p = mid[UP] or 0
            down_p = mid[DN] or 0

            winner = determine_window_winner(slug, candles_by_window)
            if winner is None:
                continue

            if lo <= up_p < hi:
                total += 1
                if winner == "UP":
                    correct += 1
            if lo <= down_p < hi:
                total += 1
                if winner == "DOWN":
                    correct += 1

        if total > 0:
            actual_wr = correct / total * 100
            expected = (lo + hi) / 2 * 100
            edge = actual_wr - expected
            # EV per $5 bet at midpoint price
            mid_price = (lo + hi) / 2
            ev = 5 * (actual_wr / 100 * (1 - mid_price) / mid_price - (1 - actual_wr / 100))
            print(f"  {lo:.0%}-{hi:.0%}:  {correct:>4}/{total:<5}  {actual_wr:>7.1f}%  {expected:>6.0f}%  {edge:>+5.1f}pp  ${ev:>+5.2f}")

    # =====================================================
    # VARIANT A: Hold to Resolution
    # =====================================================
    print("\n" + "=" * 80)
    print("VARIANT A: BUY FAVORITE, HOLD TO RESOLUTION")
    print("=" * 80)

    thresholds = [0.80, 0.85, 0.90, 0.95]
    variant_a_results = {}

    for threshold in thresholds:
        bankroll = 100.0
        bet_size = 5.0
        trades = 0
        wins = 0
        losses = 0
        skipped = 0
        total_pnl = 0.0
        pnl_list = []
        daily_pnl = defaultdict(float)
        max_drawdown = 0.0
        peak_bankroll = 100.0

        for slug, snaps in sorted(windows.items()):
            entry_snap = None
            favorite_side = None
            entry_price = None

            for snap in snaps:
                up_p = snap[UP] or 0
                down_p = snap[DN] or 0

                if up_p >= threshold:
                    entry_snap = snap
                    favorite_side = "UP"
                    entry_price = get_entry_price(snap, "UP")
                    break
                elif down_p >= threshold:
                    entry_snap = snap
                    favorite_side = "DOWN"
                    entry_price = get_entry_price(snap, "DOWN")
                    break

            if entry_snap is None or entry_price <= 0:
                continue

            actual_bet = min(bet_size, bankroll)
            if actual_bet < 3.50:
                continue

            winner = determine_window_winner(slug, candles_by_window)
            if winner is None:
                skipped += 1
                continue

            trades += 1
            won = (favorite_side == winner)

            if won:
                wins += 1
                trade_pnl = pnl_win(actual_bet, entry_price)
            else:
                losses += 1
                trade_pnl = pnl_loss(actual_bet, entry_price)

            total_pnl += trade_pnl
            bankroll += trade_pnl
            pnl_list.append(trade_pnl)

            if bankroll > peak_bankroll:
                peak_bankroll = bankroll
            dd = (peak_bankroll - bankroll) / peak_bankroll * 100
            if dd > max_drawdown:
                max_drawdown = dd

            try:
                dt = datetime.fromtimestamp(int(slug.split("-")[-1]), tz=timezone.utc)
                daily_pnl[dt.strftime("%Y-%m-%d")] += trade_pnl
            except:
                pass

            if bankroll <= 0:
                break

        wr = wins / trades * 100 if trades else 0
        avg_win = sum(p for p in pnl_list if p > 0) / max(wins, 1)
        avg_loss = sum(p for p in pnl_list if p < 0) / max(losses, 1)
        breakeven_wr = threshold * 100

        variant_a_results[threshold] = {
            'trades': trades, 'wins': wins, 'losses': losses,
            'wr': wr, 'pnl': total_pnl, 'bankroll': bankroll,
            'edge': wr - breakeven_wr, 'avg_win': avg_win, 'avg_loss': avg_loss,
        }

        print(f"\n  Threshold: {threshold:.0%} (need {breakeven_wr:.0f}% WR to break even)")
        print(f"  Trades: {trades:,}  |  W: {wins}  L: {losses}  |  WR: {wr:.1f}%")
        print(f"  Edge vs break-even: {wr - breakeven_wr:+.1f}pp")
        print(f"  Avg WIN: ${avg_win:+.3f}  |  Avg LOSS: ${avg_loss:+.2f}  |  R:R = 1:{abs(avg_loss/avg_win):.1f}" if avg_win else "")
        print(f"  Total PnL: ${total_pnl:+.2f}  |  ROI: {(bankroll-100)/100*100:+.1f}%")
        print(f"  Final bankroll: ${bankroll:.2f}  |  Max drawdown: {max_drawdown:.1f}%")
        if bankroll <= 0:
            print(f"  *** BANKRUPT ***")
        if daily_pnl:
            pos = sum(1 for v in daily_pnl.values() if v > 0)
            neg = sum(1 for v in daily_pnl.values() if v < 0)
            print(f"  Days: {pos} green, {neg} red / {len(daily_pnl)} total")

    # =====================================================
    # VARIANT B: Tick Scalping
    # =====================================================
    print("\n" + "=" * 80)
    print("VARIANT B: TICK SCALPING (EARLY EXIT)")
    print("=" * 80)
    print("  Buy favorite >= threshold, sell on +Y tick, fallback to resolution")

    # Test multiple threshold x tick combinations
    test_combos = [
        (0.85, 0.01), (0.85, 0.02), (0.85, 0.03), (0.85, 0.05),
        (0.90, 0.01), (0.90, 0.02), (0.90, 0.03), (0.90, 0.05),
        (0.95, 0.01), (0.95, 0.02), (0.95, 0.03),
    ]

    print(f"\n  {'Threshold':>9}  {'Tick':>5}  {'Trades':>6}  {'EE%':>5}  "
          f"{'WR':>5}  {'PnL':>8}  {'Final$':>7}  {'ROI':>6}  {'MaxDD':>5}")
    print(f"  {'-'*9}  {'-'*5}  {'-'*6}  {'-'*5}  {'-'*5}  {'-'*8}  {'-'*7}  {'-'*6}  {'-'*5}")

    for entry_threshold, tick_target in test_combos:
        bankroll = 100.0
        bet_size = 5.0
        trades = 0
        wins_exit = 0
        wins_resolution = 0
        losses = 0
        total_pnl = 0.0
        peak = 100.0
        max_dd = 0.0

        for slug, snaps in sorted(windows.items()):
            entry_idx = None
            favorite_side = None
            entry_price = None

            for i, snap in enumerate(snaps):
                up_p = snap[UP] or 0
                down_p = snap[DN] or 0

                if up_p >= entry_threshold:
                    entry_idx = i
                    favorite_side = "UP"
                    entry_price = get_entry_price(snap, "UP")
                    break
                elif down_p >= entry_threshold:
                    entry_idx = i
                    favorite_side = "DOWN"
                    entry_price = get_entry_price(snap, "DOWN")
                    break

            if entry_idx is None or entry_price <= 0:
                continue

            actual_bet = min(bet_size, bankroll)
            if actual_bet < 3.50:
                continue

            # Look for tick-up exit in remaining snapshots
            exited_early = False
            exit_price = None

            for snap in snaps[entry_idx + 1:]:
                current_bid = get_bid_price(snap, favorite_side)
                if current_bid >= entry_price + tick_target:
                    exited_early = True
                    exit_price = current_bid
                    break

            trades += 1

            if exited_early:
                trade_pnl = pnl_early_exit(actual_bet, entry_price, exit_price)
                if trade_pnl > 0:
                    wins_exit += 1
                else:
                    losses += 1  # fee ate the profit
            else:
                winner = determine_window_winner(slug, candles_by_window)
                if winner is None:
                    trades -= 1
                    continue
                if favorite_side == winner:
                    wins_resolution += 1
                    trade_pnl = pnl_win(actual_bet, entry_price)
                else:
                    losses += 1
                    trade_pnl = pnl_loss(actual_bet, entry_price)

            total_pnl += trade_pnl
            bankroll += trade_pnl
            if bankroll > peak:
                peak = bankroll
            dd = (peak - bankroll) / peak * 100
            if dd > max_dd:
                max_dd = dd
            if bankroll <= 0:
                break

        total_wins = wins_exit + wins_resolution
        wr = total_wins / trades * 100 if trades else 0
        ee_pct = wins_exit / trades * 100 if trades else 0
        roi = (bankroll - 100) / 100 * 100

        print(f"  {entry_threshold:>8.0%}  {tick_target:>4.0f}c  {trades:>6,}  "
              f"{ee_pct:>4.0f}%  {wr:>4.1f}%  ${total_pnl:>+7.2f}  "
              f"${bankroll:>6.2f}  {roi:>+5.1f}%  {max_dd:>4.1f}%")

    # =====================================================
    # INTRA-WINDOW PRICE MOVEMENT ANALYSIS
    # =====================================================
    print("\n" + "=" * 80)
    print("PRICE MOVEMENT AFTER BUYING FAVORITE (mid-price tracking)")
    print("=" * 80)

    for entry_thresh in [0.85, 0.90, 0.95]:
        fav_moves = []
        adv_moves = []
        total_an = 0

        for slug, snaps in windows.items():
            entry_idx = None
            favorite_side = None
            entry_price = None

            for i, snap in enumerate(snaps):
                up_p = snap[UP] or 0
                down_p = snap[DN] or 0
                if up_p >= entry_thresh:
                    entry_idx = i
                    favorite_side = "UP"
                    entry_price = up_p
                    break
                elif down_p >= entry_thresh:
                    entry_idx = i
                    favorite_side = "DOWN"
                    entry_price = down_p
                    break

            if entry_idx is None:
                continue

            best = entry_price
            worst = entry_price
            for snap in snaps[entry_idx + 1:]:
                p = get_mid_price(snap, favorite_side)
                if p > best:
                    best = p
                if p < worst:
                    worst = p

            fav_moves.append(best - entry_price)
            adv_moves.append(entry_price - worst)
            total_an += 1

        if total_an == 0:
            continue

        print(f"\n  Entry >= {entry_thresh:.0%}  ({total_an:,} windows)")

        print(f"  Favorable (price rises after entry):")
        for b in [0.01, 0.02, 0.03, 0.05, 0.10]:
            ct = sum(1 for m in fav_moves if m >= b)
            print(f"    >= +{b:.2f}: {ct:>5,} ({ct/total_an*100:>5.1f}%)")

        print(f"  Adverse (price drops after entry):")
        for b in [0.01, 0.02, 0.05, 0.10, 0.20, 0.50]:
            ct = sum(1 for m in adv_moves if m >= b)
            print(f"    >= -{b:.2f}: {ct:>5,} ({ct/total_an*100:>5.1f}%)")

        avg_fav = sum(fav_moves) / len(fav_moves)
        avg_adv = sum(adv_moves) / len(adv_moves)
        print(f"  Average: +{avg_fav:.4f} favorable, -{avg_adv:.4f} adverse")

    # =====================================================
    # BOTH-SIDES ARBITRAGE CHECK
    # =====================================================
    print("\n" + "=" * 80)
    print("BOTH-SIDES ARBITRAGE CHECK")
    print("=" * 80)

    # Simultaneous (same snapshot)
    simul_arb = 0
    simul_best = 1.0
    arb_total = 0

    for slug, snaps in windows.items():
        for snap in snaps:
            up_ask = snap[UBA] if snap[UBA] else (snap[UP] or 1.0)
            down_ask = snap[DBA] if snap[DBA] else (snap[DN] or 1.0)
            total = up_ask + down_ask
            arb_total += 1
            if total < 1.0:
                simul_arb += 1
                if total < simul_best:
                    simul_best = total

    print(f"  Checked {arb_total:,} snapshots across {len(windows):,} windows")
    print(f"  Simultaneous arbs (UP ask + DOWN ask < $1.00): {simul_arb:,}")
    if simul_arb:
        profit_per = 1.0 - simul_best
        print(f"  Best arb: ${simul_best:.4f} cost -> ${profit_per:.4f} guaranteed profit per share")

    # Cross-window (min UP ask + min DOWN ask within same window)
    cross_arb = 0
    cross_best = 1.0
    for slug, snaps in windows.items():
        min_up = min((snap[UBA] if snap[UBA] else (snap[UP] or 1.0)) for snap in snaps)
        min_dn = min((snap[DBA] if snap[DBA] else (snap[DN] or 1.0)) for snap in snaps)
        total = min_up + min_dn
        if total < 1.0:
            cross_arb += 1
            if total < cross_best:
                cross_best = total

    print(f"\n  Cross-time arbs (min asks within same window, NOT simultaneous): {cross_arb}")
    if cross_arb:
        print(f"  Best: ${cross_best:.4f}")

    # =====================================================
    # EXISTING BOT TRADES: FAVORITE vs UNDERDOG
    # =====================================================
    print("\n" + "=" * 80)
    print("YOUR BOT'S TRADES: FAVORITE vs UNDERDOG ANALYSIS")
    print("=" * 80)

    c.execute("""
        SELECT entry_price, outcome, pnl, side, trade_tag
        FROM live_trades
        WHERE outcome IS NOT NULL AND entry_price IS NOT NULL
    """)
    bot_trades = c.fetchall()

    # Bucket by entry price
    fav_trades = [t for t in bot_trades if t[0] and t[0] >= 0.50]  # bought favorite
    dog_trades = [t for t in bot_trades if t[0] and t[0] < 0.50]   # bought underdog

    for label, trades_list in [("Favorite (entry >= 50c)", fav_trades), ("Underdog (entry < 50c)", dog_trades)]:
        if not trades_list:
            continue
        n = len(trades_list)
        w = sum(1 for t in trades_list if t[1] == "WIN")
        l = sum(1 for t in trades_list if t[1] == "LOSS")
        ee = sum(1 for t in trades_list if t[1] == "EARLY_EXIT")
        pnl_tot = sum(t[2] for t in trades_list if t[2])
        print(f"\n  {label}: {n} trades")
        print(f"    W: {w}  L: {l}  EE: {ee}  |  Settlement WR: {w/(w+l)*100:.1f}%" if w+l else f"    W: {w}  L: {l}  EE: {ee}")
        print(f"    Total PnL: ${pnl_tot:+.2f}")

    # High-favorite brackets
    print(f"\n  Entry price breakdown:")
    brackets = [(0.25, 0.35), (0.35, 0.40), (0.40, 0.45), (0.45, 0.50),
                (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.80),
                (0.80, 0.90), (0.90, 1.00)]

    print(f"  {'Entry':>11}  {'N':>5}  {'W':>4}  {'L':>4}  {'EE':>4}  {'WR%':>5}  {'PnL':>8}")
    print(f"  {'-'*11}  {'-'*5}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*5}  {'-'*8}")

    for lo, hi in brackets:
        bt = [t for t in bot_trades if t[0] and lo <= t[0] < hi]
        if not bt:
            continue
        n = len(bt)
        w = sum(1 for t in bt if t[1] == "WIN")
        l = sum(1 for t in bt if t[1] == "LOSS")
        ee = sum(1 for t in bt if t[1] == "EARLY_EXIT")
        pnl_tot = sum(t[2] for t in bt if t[2])
        wr = w / (w + l) * 100 if (w + l) else 0
        print(f"  {lo:.2f}-{hi:.2f}  {n:>5}  {w:>4}  {l:>4}  {ee:>4}  {wr:>4.0f}%  ${pnl_tot:>+7.2f}")

    # =====================================================
    # SUMMARY
    # =====================================================
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    print("\n  Variant A (hold to resolution):")
    for t in thresholds:
        r = variant_a_results.get(t)
        if r:
            verdict = "PROFITABLE" if r['pnl'] > 0 else "UNPROFITABLE"
            print(f"    {t:.0%}: {r['wr']:.1f}% WR, edge {r['edge']:+.1f}pp, "
                  f"${r['pnl']:+.2f} PnL -> {verdict}")

    print(f"\n  Key insight: At entry price P, you need P% win rate to break even.")
    print(f"  If actual WR > P%, there's an edge. If WR < P%, market is over-priced.")
    print(f"  Variant B adds early exit to capture micro-movements without resolution risk.")

    conn.close()


if __name__ == "__main__":
    run_simulation()
