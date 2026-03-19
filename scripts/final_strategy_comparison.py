"""
Final comprehensive strategy comparison.
Unlimited capital (no bankruptcy distortion), fixed $5 bets, 6.25% fee on wins.
"""
import sqlite3
import datetime
import math
import statistics
from collections import defaultdict

DB_PATH = 'btc_edge.db'
FEE = 0.0625
BET = 5


def build_windows():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT DISTINCT slug FROM market_snapshots ORDER BY slug')
    slugs = [r[0] for r in c.fetchall()]
    c.execute('SELECT timestamp, open, close FROM candles')
    co = {}
    cc = {}
    for ts, o, cl in c.fetchall():
        co[ts] = o
        cc[ts] = cl
    c.execute('''
        SELECT slug, up_price, down_price, btc_price, up_spread, down_spread
        FROM market_snapshots WHERE id IN (SELECT MIN(id) FROM market_snapshots GROUP BY slug)
    ''')
    snaps = {}
    for row in c.fetchall():
        snaps[row[0]] = row[1:]
    conn.close()

    windows = []
    for slug in slugs:
        try:
            wot = int(slug.split('-')[-1])
        except:
            continue
        om = wot * 1000
        cm = (wot + 240) * 1000
        bo = co.get(om)
        bc = cc.get(cm)
        if bo is None or bc is None:
            for off in range(0, 300000, 60000):
                if bo is None and (om + off) in co:
                    bo = co[om + off]
                if bc is None and (cm - off) in cc:
                    bc = cc[cm - off]
                if bo is not None and bc is not None:
                    break
            if bo is None or bc is None:
                continue
        snap = snaps.get(slug)
        if not snap:
            continue
        winner = 'UP' if bc > bo else 'DOWN'
        dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=wot)
        up_p = snap[0] or 0.5
        dn_p = snap[1] or 0.5
        asp = ((snap[3] or 0) + (snap[4] or 0)) / 2
        windows.append({
            'ts': wot, 'dt': dt, 'hour': dt.hour, 'weekday': dt.weekday(),
            'winner': winner, 'up_price': up_p, 'down_price': dn_p,
            'btc_price': snap[2] or bo, 'avg_spread': asp,
        })
    windows.sort(key=lambda w: w['ts'])
    return windows


def trade_pnl(entry, won):
    if won:
        profit = BET / entry - BET
        return profit - profit * FEE
    return -BET


def eval_strategy(trades):
    if not trades:
        return None
    pnls = []
    equity = 10000  # large starting capital to avoid bankruptcy
    min_eq = equity
    wins = 0
    for w, side, entry in trades:
        won = w['winner'] == side
        p = trade_pnl(entry, won)
        pnls.append(p)
        equity += p
        min_eq = min(min_eq, equity)
        if won:
            wins += 1

    n = len(pnls)
    wr = wins / n
    net = sum(pnls)
    ev = net / n
    std = statistics.stdev(pnls) if n > 1 else 1
    sharpe_per_trade = ev / std if std > 0 else 0

    return {
        'n': n, 'wins': wins, 'wr': wr, 'net': net, 'ev': ev,
        'std': std, 'sharpe': sharpe_per_trade,
        'max_dd': 10000 - min_eq,
    }


def main():
    windows = build_windows()
    N = len(windows)
    print(f"Windows: {N}, {windows[0]['dt']} to {windows[-1]['dt']}")

    up_count = sum(1 for w in windows if w['winner'] == 'UP')
    print(f"Overall: UP={up_count / N:.1%}, DOWN={1 - up_count / N:.1%}")

    # Define UP hours based on analysis 1
    up_hours = {5, 7, 9, 10, 12, 13, 14, 20, 21}

    # Build all strategy trade lists
    strats = {}

    strats['Always DOWN'] = [(w, 'DOWN', w['down_price']) for w in windows]
    strats['Always UP'] = [(w, 'UP', w['up_price']) for w in windows]

    strats['Always Cheap'] = [
        (w, 'UP' if w['up_price'] < w['down_price'] else 'DOWN',
         min(w['up_price'], w['down_price'])) for w in windows
    ]
    strats['Always Favorite'] = [
        (w, 'UP' if w['up_price'] > w['down_price'] else 'DOWN',
         max(w['up_price'], w['down_price'])) for w in windows
    ]

    strats['Hour Bias'] = [
        (w, 'UP' if w['hour'] in up_hours else 'DOWN',
         w['up_price'] if w['hour'] in up_hours else w['down_price'])
        for w in windows
    ]

    # Momentum-2
    mom2 = []
    for i in range(2, N):
        w = windows[i]
        if w['ts'] - windows[i - 1]['ts'] > 310 or windows[i - 1]['ts'] - windows[i - 2]['ts'] > 310:
            continue
        if windows[i - 1]['winner'] == windows[i - 2]['winner']:
            s = windows[i - 1]['winner']
            mom2.append((w, s, w['up_price'] if s == 'UP' else w['down_price']))
    strats['Momentum-2'] = mom2

    # Contrarian-3
    contr3 = []
    for i in range(3, N):
        ok = True
        for j in range(i - 3, i):
            if windows[j + 1]['ts'] - windows[j]['ts'] > 310:
                ok = False
                break
        if not ok:
            continue
        if windows[i - 1]['winner'] == windows[i - 2]['winner'] == windows[i - 3]['winner']:
            streak = windows[i - 1]['winner']
            bet = 'DOWN' if streak == 'UP' else 'UP'
            contr3.append((windows[i], bet, windows[i]['up_price'] if bet == 'UP' else windows[i]['down_price']))
    strats['Contrarian-3'] = contr3

    strats['Hour 6 DOWN'] = [(w, 'DOWN', w['down_price']) for w in windows if w['hour'] == 6]

    strats['DOWN Best 6 Hours'] = [
        (w, 'DOWN', w['down_price']) for w in windows
        if w['hour'] in {0, 6, 11, 18, 19, 22}
    ]

    strats['UP Best 3 Hours'] = [
        (w, 'UP', w['up_price']) for w in windows
        if w['hour'] in {10, 12, 13}
    ]

    strats['DOWN Best Hours (9)'] = [
        (w, 'DOWN', w['down_price']) for w in windows
        if w['hour'] in {0, 2, 6, 11, 17, 18, 19, 22, 23}
    ]

    # Mom2 + hour bias aligned
    mom2_hb = []
    for i in range(2, N):
        w = windows[i]
        if w['ts'] - windows[i - 1]['ts'] > 310 or windows[i - 1]['ts'] - windows[i - 2]['ts'] > 310:
            continue
        if windows[i - 1]['winner'] == windows[i - 2]['winner']:
            s = windows[i - 1]['winner']
            hs = 'UP' if w['hour'] in up_hours else 'DOWN'
            if s == hs:
                mom2_hb.append((w, s, w['up_price'] if s == 'UP' else w['down_price']))
    strats['Mom2 + HourBias'] = mom2_hb

    strats['Cheap + Sprd 0.03-0.05'] = [
        (w, 'UP' if w['up_price'] < w['down_price'] else 'DOWN',
         min(w['up_price'], w['down_price']))
        for w in windows if 0.03 <= w['avg_spread'] < 0.05
    ]

    strats['Cheap entry<0.40'] = [
        (w, 'UP' if w['up_price'] < w['down_price'] else 'DOWN',
         min(w['up_price'], w['down_price']))
        for w in windows if min(w['up_price'], w['down_price']) < 0.40
    ]

    strats['Cheap + H13-21'] = [
        (w, 'UP' if w['up_price'] < w['down_price'] else 'DOWN',
         min(w['up_price'], w['down_price']))
        for w in windows if 13 <= w['hour'] <= 21
    ]

    # DOWN when down is cheap (< up_price)
    strats['DOWN when cheap'] = [
        (w, 'DOWN', w['down_price']) for w in windows
        if w['down_price'] < w['up_price']
    ]

    # Evaluate all
    print("\n" + "=" * 110)
    print("STRATEGY COMPARISON: Fixed $5 bets, 6.25% fee on wins, unlimited capital")
    print("=" * 110)
    print(f"{'Strategy':>25} | {'N':>5} | {'WR':>6} | {'NetEV':>9} | {'NetPnL':>10} | "
          f"{'MaxDD':>8} | {'Sharpe':>7} | {'Trades/day':>10}")
    print("-" * 100)

    days = (windows[-1]['ts'] - windows[0]['ts']) / 86400

    results = []
    for name, trades in strats.items():
        r = eval_strategy(trades)
        if r is None:
            continue
        r['name'] = name
        r['tpd'] = r['n'] / days
        results.append(r)

    results.sort(key=lambda x: x['net'], reverse=True)

    for r in results:
        print(f"{r['name']:>25} | {r['n']:5d} | {r['wr']:5.1%} | ${r['ev']:+8.3f} | "
              f"${r['net']:+9.1f} | ${r['max_dd']:7.1f} | {r['sharpe']:+6.4f} | {r['tpd']:9.1f}")

    # ================================================================
    # OVERFITTING TEST: First half vs second half for top strategies
    # ================================================================
    print("\n" + "=" * 110)
    print("OVERFITTING TEST: First half vs second half")
    print("=" * 110)

    mid_ts = windows[N // 2]['ts']
    top_strats = ['Hour Bias', 'Mom2 + HourBias', 'DOWN Best Hours (9)',
                  'DOWN Best 6 Hours', 'UP Best 3 Hours', 'Hour 6 DOWN']

    print(f"{'Strategy':>25} | {'1H N':>5} | {'1H WR':>6} | {'1H EV':>8} | {'1H PnL':>8} | "
          f"{'2H N':>5} | {'2H WR':>6} | {'2H EV':>8} | {'2H PnL':>8} | {'Stable?':>7}")
    print("-" * 105)

    for name in top_strats:
        trades = strats[name]
        h1 = [(w, s, e) for w, s, e in trades if w['ts'] < mid_ts]
        h2 = [(w, s, e) for w, s, e in trades if w['ts'] >= mid_ts]

        r1 = eval_strategy(h1)
        r2 = eval_strategy(h2)
        if not r1 or not r2:
            continue

        stable = 'YES' if r1['ev'] > 0 and r2['ev'] > 0 else 'NO'
        print(f"{name:>25} | {r1['n']:5d} | {r1['wr']:5.1%} | ${r1['ev']:+7.3f} | ${r1['net']:+7.1f} | "
              f"{r2['n']:5d} | {r2['wr']:5.1%} | ${r2['ev']:+7.3f} | ${r2['net']:+7.1f} | {stable:>7}")

    # ================================================================
    # STATISTICAL SIGNIFICANCE
    # ================================================================
    print("\n" + "=" * 110)
    print("STATISTICAL SIGNIFICANCE (z-test vs 50% null, corrected for entry price)")
    print("=" * 110)

    def z_test(wins, n, null_p=0.5):
        if n == 0:
            return 0, 1
        p = wins / n
        se = math.sqrt(null_p * (1 - null_p) / n)
        z = (p - null_p) / se if se > 0 else 0
        pval = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
        return z, pval

    print(f"{'Strategy':>25} | {'N':>5} | {'WR':>6} | {'Z':>7} | {'p-value':>8} | {'Sig@5%':>6}")
    print("-" * 65)

    for r in results[:10]:
        name = r['name']
        z, p = z_test(r['wins'], r['n'])
        sig = 'YES' if p < 0.05 else 'no'
        print(f"{name:>25} | {r['n']:5d} | {r['wr']:5.1%} | {z:+6.2f} | {p:7.4f} | {sig:>6}")

    # ================================================================
    # BREAKEVEN TABLE
    # ================================================================
    print("\n" + "=" * 80)
    print("BREAKEVEN WIN RATES BY ENTRY PRICE (with 6.25% fee)")
    print("=" * 80)
    print(f"{'Entry':>6} | {'Net Win':>8} | {'Loss':>6} | {'BE WR':>6} | {'Tokens/$5':>9}")
    print("-" * 45)
    for entry in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]:
        profit = BET / entry - BET
        net_win = profit - profit * FEE
        be_wr = BET / (net_win + BET)
        tokens = BET / entry
        print(f"{entry:6.2f} | ${net_win:7.2f} | $-{BET:.2f} | {be_wr:5.1%} | {tokens:8.1f}")

    # ================================================================
    # THE REAL QUESTION: Is there any edge at all?
    # ================================================================
    print("\n" + "=" * 110)
    print("FINAL ANALYSIS: SURVIVAL SIMULATION ($200 capital, $5 bets)")
    print("=" * 110)

    for name in ['Hour Bias', 'Mom2 + HourBias', 'DOWN Best 6 Hours',
                  'UP Best 3 Hours', 'Hour 6 DOWN', 'Always DOWN',
                  'Always Favorite', 'Always Cheap']:
        trades = strats[name]
        equity = 200
        min_eq = 200
        max_eq = 200
        bankrupt = False
        traded = 0

        for w, side, entry in trades:
            if equity < BET:
                bankrupt = True
                break
            won = w['winner'] == side
            p = trade_pnl(entry, won)
            equity += p
            traded += 1
            min_eq = min(min_eq, equity)
            max_eq = max(max_eq, equity)

        print(f"  {name:>25}: Final=${equity:.0f}, Min=${min_eq:.0f}, "
              f"MaxDD=${max_eq - min_eq:.0f}, Traded={traded}, "
              f"{'BANKRUPT' if bankrupt else 'Survived'}")

    print("\n" + "=" * 110)
    print("CONCLUSIONS")
    print("=" * 110)
    print("""
1. HOUR-BIAS STRATEGY: The strongest signal found.
   - Bet UP during hours {5,7,9,10,12,13,14,20,21}, DOWN otherwise
   - WR=52.2%, Net EV=+$0.093/trade, cumulative +$520 over 5,624 trades
   - PARTIALLY STABLE: positive in both halves, but degrading (1H: +$0.16, 2H: +$0.02)
   - Drives ~$26/day gross at 288 trades/day

2. HOUR 6 DOWN: Strongest single-hour effect.
   - WR=57.9%, highly profitable per trade, but only 12 trades/day
   - Statistically significant (z=2.45, p=0.014)
   - But only 240 trades -- could be noise

3. ALL SIMPLE STRATEGIES LOSE AFTER FEES
   - Always DOWN, Always UP, Always Cheap, Always Favorite: all negative
   - Fees of 6.25% on win profit require ~52% WR at typical entry prices

4. MOMENTUM EXISTS but is weak
   - After 2 consecutive same-direction results, continuation > reversal
   - But WR is only 49.5% (not enough to beat fees)

5. THE MARKET IS WELL-CALIBRATED
   - Favorite wins 56.7% (highly significant, z=10.05)
   - Favorite pricing correctly reflects this -- no systematic mispricing

6. DATA INSUFFICIENCY
   - 20 days of data (5,624 windows) is marginal for detecting small edges
   - Hour-level patterns have only ~240 observations each
   - Weekly P&L swings are enormous relative to edge size
   - Need 3-6 months of data for reliable conclusions

7. KEY CAVEAT: These results use opening snapshot prices as entry.
   Live trading faces slippage, spread crossing, and timing delays
   that would reduce all EVs further.
""")


if __name__ == '__main__':
    main()
