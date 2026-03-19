"""
Comprehensive time-based pattern analysis for BTC 5-min Polymarket data.
Analyzes hour-of-day, day-of-week, volatility regimes, consecutive patterns,
BTC price levels, spread as predictor, and combined filter strategies.
"""
import sqlite3
import datetime
import statistics
from collections import defaultdict

DB_PATH = 'btc_edge.db'


def build_windows(conn):
    """Build window-level data by joining market_snapshots with candles for outcome."""
    c = conn.cursor()

    # Get all distinct slugs
    c.execute('SELECT DISTINCT slug FROM market_snapshots ORDER BY slug')
    slugs = [r[0] for r in c.fetchall()]

    # Pre-fetch all candles into a dict for fast lookup
    c.execute('SELECT timestamp, open, close FROM candles')
    candle_open = {}
    candle_close = {}
    for ts, o, cl in c.fetchall():
        candle_open[ts] = o
        candle_close[ts] = cl

    # Pre-fetch first snapshot per slug
    c.execute('''
        SELECT slug, up_price, down_price, btc_price,
               up_best_bid, up_best_ask, down_best_bid, down_best_ask,
               up_spread, down_spread
        FROM market_snapshots
        WHERE id IN (
            SELECT MIN(id) FROM market_snapshots GROUP BY slug
        )
    ''')
    first_snaps = {}
    for row in c.fetchall():
        first_snaps[row[0]] = row[1:]

    windows = []
    skipped = 0

    for slug in slugs:
        try:
            window_open_ts = int(slug.split('-')[-1])
        except Exception:
            continue

        window_close_ts = window_open_ts + 300  # 5 minutes

        # BTC open from first 1-min candle in window
        open_ts_ms = window_open_ts * 1000
        close_ts_ms = (window_close_ts - 60) * 1000

        btc_open_val = candle_open.get(open_ts_ms)
        btc_close_val = candle_close.get(close_ts_ms)

        if btc_open_val is None or btc_close_val is None:
            # Try nearby candles
            found_open = False
            found_close = False
            for offset in range(0, 300000, 60000):
                if not found_open and (open_ts_ms + offset) in candle_open:
                    btc_open_val = candle_open[open_ts_ms + offset]
                    found_open = True
                if not found_close and (close_ts_ms - offset) in candle_close:
                    btc_close_val = candle_close[close_ts_ms - offset]
                    found_close = True
                if found_open and found_close:
                    break
            if btc_open_val is None or btc_close_val is None:
                skipped += 1
                continue

        winner = 'UP' if btc_close_val > btc_open_val else 'DOWN'
        btc_return = (btc_close_val - btc_open_val) / btc_open_val

        snap = first_snaps.get(slug)
        if not snap:
            skipped += 1
            continue

        up_price = snap[0] or 0.5
        down_price = snap[1] or 0.5
        btc_price = snap[2] or btc_open_val
        up_spread = snap[7] or 0
        down_spread = snap[8] or 0
        avg_spread = (up_spread + down_spread) / 2 if up_spread and down_spread else 0

        dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=window_open_ts)

        favorite = 'UP' if up_price > down_price else 'DOWN'
        fav_price = max(up_price, down_price)
        dog_price = min(up_price, down_price)

        windows.append({
            'slug': slug,
            'ts': window_open_ts,
            'dt': dt,
            'hour': dt.hour,
            'weekday': dt.weekday(),
            'btc_open': btc_open_val,
            'btc_close': btc_close_val,
            'btc_return': btc_return,
            'abs_return': abs(btc_return),
            'winner': winner,
            'up_price': up_price,
            'down_price': down_price,
            'btc_price': btc_price,
            'favorite': favorite,
            'fav_price': fav_price,
            'dog_price': dog_price,
            'up_spread': up_spread,
            'down_spread': down_spread,
            'avg_spread': avg_spread,
        })

    print(f"  Skipped {skipped} windows (missing candle data)")
    return windows


def analyze_hour_of_day(windows):
    print("=" * 100)
    print("ANALYSIS 1: HOUR-OF-DAY PATTERNS (UTC)")
    print("=" * 100)

    by_hour = defaultdict(list)
    for w in windows:
        by_hour[w['hour']].append(w)

    print(f"\n{'Hour':>4} | {'N':>5} | {'UP%':>6} | {'DN%':>6} | {'AvgSprd':>7} | {'FavWR':>6} | "
          f"{'AlwUP_EV':>9} | {'AlwDN_EV':>9} | {'Best':>5} | {'BestEV':>8} | {'CumPnL':>9}")
    print("-" * 105)

    hour_results = []
    for hour in range(24):
        ws = by_hour.get(hour, [])
        if not ws:
            continue
        n = len(ws)
        up_wins = sum(1 for w in ws if w['winner'] == 'UP')
        up_pct = up_wins / n
        dn_pct = 1 - up_pct
        avg_spread = statistics.mean(w['avg_spread'] for w in ws)
        fav_wins = sum(1 for w in ws if w['winner'] == w['favorite']) / n

        avg_up_p = statistics.mean(w['up_price'] for w in ws)
        avg_dn_p = statistics.mean(w['down_price'] for w in ws)

        ev_up = 5 * (up_pct * (1 - avg_up_p) - dn_pct * avg_up_p)
        ev_dn = 5 * (dn_pct * (1 - avg_dn_p) - up_pct * avg_dn_p)

        best_side = 'UP' if ev_up > ev_dn else 'DOWN'
        best_ev = max(ev_up, ev_dn)

        print(f"{hour:4d} | {n:5d} | {up_pct:5.1%} | {dn_pct:5.1%} | {avg_spread:6.4f} | {fav_wins:5.1%} | "
              f"${ev_up:+8.3f} | ${ev_dn:+8.3f} | {best_side:>5} | ${best_ev:+7.3f} | ${best_ev*n:+8.1f}")

        hour_results.append((hour, n, up_pct, best_side, best_ev, avg_spread, fav_wins))

    print("\n--- Top 5 Most Profitable Hours ---")
    sorted_hrs = sorted(hour_results, key=lambda x: x[4], reverse=True)
    for h, n, up_pct, side, ev, sprd, fav in sorted_hrs[:5]:
        wr = up_pct if side == 'UP' else (1 - up_pct)
        print(f"  Hour {h:02d}: Always bet {side}, WR={wr:.1%}, EV=${ev:+.3f}/trade, N={n}, "
              f"Cumulative=${ev*n:+.1f}")

    print("\n--- 5 Worst Hours ---")
    for h, n, up_pct, side, ev, sprd, fav in sorted_hrs[-5:]:
        wr = up_pct if side == 'UP' else (1 - up_pct)
        print(f"  Hour {h:02d}: Best is {side}, WR={wr:.1%}, EV=${ev:+.3f}/trade, N={n}")

    return hour_results


def analyze_day_of_week(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 2: DAY-OF-WEEK PATTERNS")
    print("=" * 100)

    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    by_day = defaultdict(list)
    for w in windows:
        by_day[w['weekday']].append(w)

    print(f"\n{'Day':>4} | {'N':>5} | {'UP%':>6} | {'AvgSprd':>7} | {'FavWR':>6} | "
          f"{'AlwUP_EV':>9} | {'AlwDN_EV':>9} | {'Best':>5} | {'BestEV':>8}")
    print("-" * 80)

    for day in range(7):
        ws = by_day.get(day, [])
        if not ws:
            continue
        n = len(ws)
        up_pct = sum(1 for w in ws if w['winner'] == 'UP') / n
        dn_pct = 1 - up_pct
        avg_spread = statistics.mean(w['avg_spread'] for w in ws)
        fav_wr = sum(1 for w in ws if w['winner'] == w['favorite']) / n

        avg_up_p = statistics.mean(w['up_price'] for w in ws)
        avg_dn_p = statistics.mean(w['down_price'] for w in ws)
        ev_up = 5 * (up_pct * (1 - avg_up_p) - dn_pct * avg_up_p)
        ev_dn = 5 * (dn_pct * (1 - avg_dn_p) - up_pct * avg_dn_p)
        best = 'UP' if ev_up > ev_dn else 'DOWN'
        best_ev = max(ev_up, ev_dn)

        print(f"{day_names[day]:>4} | {n:5d} | {up_pct:5.1%} | {avg_spread:6.4f} | {fav_wr:5.1%} | "
              f"${ev_up:+8.3f} | ${ev_dn:+8.3f} | {best:>5} | ${best_ev:+7.3f}")


def analyze_volatility_regimes(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 3: VOLATILITY REGIME ANALYSIS")
    print("=" * 100)

    abs_returns = sorted(w['abs_return'] for w in windows)
    n = len(abs_returns)
    low_thresh = abs_returns[n // 3]
    high_thresh = abs_returns[2 * n // 3]

    print(f"\nTercile thresholds: Low <= {low_thresh:.5%}, Med <= {high_thresh:.5%}, High > {high_thresh:.5%}")

    regimes = {'LOW': [], 'MEDIUM': [], 'HIGH': []}
    for w in windows:
        if w['abs_return'] <= low_thresh:
            regimes['LOW'].append(w)
        elif w['abs_return'] <= high_thresh:
            regimes['MEDIUM'].append(w)
        else:
            regimes['HIGH'].append(w)

    print(f"\n{'Regime':>8} | {'N':>5} | {'UP%':>6} | {'FavWR':>6} | {'DogWR':>6} | "
          f"{'CheapWR':>7} | {'Avg|Ret|':>9} | {'DogEV/5':>8} | {'FavEV/5':>8}")
    print("-" * 90)

    for regime in ['LOW', 'MEDIUM', 'HIGH']:
        ws = regimes[regime]
        rn = len(ws)
        up_pct = sum(1 for w in ws if w['winner'] == 'UP') / rn
        fav_wr = sum(1 for w in ws if w['winner'] == w['favorite']) / rn
        dog_wr = 1 - fav_wr

        cheap_wr = sum(1 for w in ws if
            (w['winner'] == 'UP' and w['up_price'] < w['down_price']) or
            (w['winner'] == 'DOWN' and w['down_price'] < w['up_price'])
        ) / rn

        avg_ret = statistics.mean(w['abs_return'] for w in ws)

        avg_dog_p = statistics.mean(w['dog_price'] for w in ws)
        avg_fav_p = statistics.mean(w['fav_price'] for w in ws)
        ev_dog = 5 * (dog_wr * (1 - avg_dog_p) - fav_wr * avg_dog_p)
        ev_fav = 5 * (fav_wr * (1 - avg_fav_p) - dog_wr * avg_fav_p)

        print(f"{regime:>8} | {rn:5d} | {up_pct:5.1%} | {fav_wr:5.1%} | {dog_wr:5.1%} | "
              f"{cheap_wr:6.1%} | {avg_ret:8.4%} | ${ev_dog:+7.3f} | ${ev_fav:+7.3f}")

    # DOWN bias in low-vol?
    print("\n--- Low-vol insight ---")
    ws_low = regimes['LOW']
    down_pct = sum(1 for w in ws_low if w['winner'] == 'DOWN') / len(ws_low)
    print(f"  Low-vol DOWN rate: {down_pct:.1%} (flat close = DOWN by Polymarket rules)")
    print(f"  This is the 'tie goes to DOWN' effect in low-volatility windows")


def analyze_consecutive_patterns(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 4: CONSECUTIVE WINDOW PATTERNS")
    print("=" * 100)

    sorted_w = sorted(windows, key=lambda w: w['ts'])

    for streak_len in [2, 3, 4, 5]:
        up_after_up = 0
        up_after_up_total = 0
        dn_after_dn = 0
        dn_after_dn_total = 0

        for i in range(streak_len, len(sorted_w)):
            consecutive = True
            for j in range(i - streak_len, i):
                if sorted_w[j+1]['ts'] - sorted_w[j]['ts'] > 310:
                    consecutive = False
                    break
            if not consecutive:
                continue

            prev = [sorted_w[j]['winner'] for j in range(i - streak_len, i)]

            if all(w == 'UP' for w in prev):
                up_after_up_total += 1
                if sorted_w[i]['winner'] == 'UP':
                    up_after_up += 1

            if all(w == 'DOWN' for w in prev):
                dn_after_dn_total += 1
                if sorted_w[i]['winner'] == 'DOWN':
                    dn_after_dn += 1

        print(f"\nAfter {streak_len} consecutive UPs (N={up_after_up_total}):")
        if up_after_up_total > 0:
            p = up_after_up / up_after_up_total
            label = 'MEAN_REV' if p < 0.47 else 'MOMENTUM' if p > 0.53 else 'NEUTRAL'
            print(f"  P(UP)={p:.1%}, P(DOWN)={1-p:.1%} [{label}]")
        print(f"After {streak_len} consecutive DOWNs (N={dn_after_dn_total}):")
        if dn_after_dn_total > 0:
            p = dn_after_dn / dn_after_dn_total
            label = 'MEAN_REV' if p < 0.47 else 'MOMENTUM' if p > 0.53 else 'NEUTRAL'
            print(f"  P(DOWN)={p:.1%}, P(UP)={1-p:.1%} [{label}]")

    # Alternation rate
    print(f"\n--- Alternation Rate ---")
    alt = 0
    cont = 0
    for i in range(1, len(sorted_w)):
        if sorted_w[i]['ts'] - sorted_w[i-1]['ts'] > 310:
            continue
        if sorted_w[i]['winner'] != sorted_w[i-1]['winner']:
            alt += 1
        else:
            cont += 1
    total = alt + cont
    if total > 0:
        print(f"  Alternation: {alt/total:.1%} ({alt}/{total})")
        print(f"  Continuation: {cont/total:.1%} ({cont}/{total})")

    # EV of contrarian strategies
    print(f"\n--- Contrarian After Streaks: EV ---")
    for streak_len in [2, 3, 4]:
        for streak_side in ['UP', 'DOWN']:
            bet_side = 'DOWN' if streak_side == 'UP' else 'UP'
            wins = 0
            total_trades = 0
            pnl = 0

            for i in range(streak_len, len(sorted_w)):
                consecutive = True
                for j in range(i - streak_len, i):
                    if sorted_w[j+1]['ts'] - sorted_w[j]['ts'] > 310:
                        consecutive = False
                        break
                if not consecutive:
                    continue

                prev = [sorted_w[j]['winner'] for j in range(i - streak_len, i)]
                if not all(w == streak_side for w in prev):
                    continue

                w = sorted_w[i]
                entry = w['up_price'] if bet_side == 'UP' else w['down_price']
                won = w['winner'] == bet_side
                trade_pnl = 5 * ((1 - entry) if won else -entry)
                pnl += trade_pnl
                total_trades += 1
                if won:
                    wins += 1

            if total_trades > 0:
                wr = wins / total_trades
                ev = pnl / total_trades
                print(f"  After {streak_len}x{streak_side} bet {bet_side}: "
                      f"WR={wr:.1%}, N={total_trades}, EV=${ev:+.3f}, Cum=${pnl:+.1f}")


def analyze_btc_price_levels(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 5: BTC PRICE LEVEL ANALYSIS")
    print("=" * 100)

    buckets = defaultdict(list)
    for w in windows:
        bucket = int(w['btc_price'] / 5000) * 5000
        buckets[bucket].append(w)

    print(f"\n{'BTC Range':>15} | {'N':>5} | {'UP%':>6} | {'FavWR':>6} | {'AvgSprd':>7} | "
          f"{'CheapWR':>7} | {'DogEV/5':>8} | {'FavEV/5':>8}")
    print("-" * 85)

    for bucket in sorted(buckets.keys()):
        ws = buckets[bucket]
        n = len(ws)
        if n < 20:
            continue
        up_pct = sum(1 for w in ws if w['winner'] == 'UP') / n
        fav_wr = sum(1 for w in ws if w['winner'] == w['favorite']) / n
        avg_spread = statistics.mean(w['avg_spread'] for w in ws)

        cheap_wr = sum(1 for w in ws if
            (w['winner'] == 'UP' and w['up_price'] < w['down_price']) or
            (w['winner'] == 'DOWN' and w['down_price'] < w['up_price'])
        ) / n

        avg_dog_p = statistics.mean(w['dog_price'] for w in ws)
        avg_fav_p = statistics.mean(w['fav_price'] for w in ws)
        dog_wr = 1 - fav_wr
        ev_dog = 5 * (dog_wr * (1 - avg_dog_p) - fav_wr * avg_dog_p)
        ev_fav = 5 * (fav_wr * (1 - avg_fav_p) - dog_wr * avg_fav_p)

        label = f"${bucket//1000}K-{(bucket+5000)//1000}K"
        print(f"{label:>15} | {n:5d} | {up_pct:5.1%} | {fav_wr:5.1%} | {avg_spread:6.4f} | "
              f"{cheap_wr:6.1%} | ${ev_dog:+7.3f} | ${ev_fav:+7.3f}")


def analyze_spread_as_predictor(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 6: SPREAD AS PREDICTOR")
    print("=" * 100)

    ws_with_spread = [w for w in windows if w['avg_spread'] > 0]

    spread_buckets = {
        '< 0.01': [w for w in ws_with_spread if w['avg_spread'] < 0.01],
        '0.01-0.02': [w for w in ws_with_spread if 0.01 <= w['avg_spread'] < 0.02],
        '0.02-0.03': [w for w in ws_with_spread if 0.02 <= w['avg_spread'] < 0.03],
        '0.03-0.05': [w for w in ws_with_spread if 0.03 <= w['avg_spread'] < 0.05],
        '0.05-0.10': [w for w in ws_with_spread if 0.05 <= w['avg_spread'] < 0.10],
        '>= 0.10': [w for w in ws_with_spread if w['avg_spread'] >= 0.10],
    }

    print(f"\n{'Spread':>12} | {'N':>5} | {'UP%':>6} | {'FavWR':>6} | {'DogWR':>6} | "
          f"{'CheapWR':>7} | {'DogEV/5':>8} | {'FavEV/5':>8}")
    print("-" * 85)

    for label, ws in spread_buckets.items():
        if not ws:
            continue
        n = len(ws)
        up_pct = sum(1 for w in ws if w['winner'] == 'UP') / n
        fav_wr = sum(1 for w in ws if w['winner'] == w['favorite']) / n
        dog_wr = 1 - fav_wr

        cheap_wr = sum(1 for w in ws if
            (w['winner'] == 'UP' and w['up_price'] < w['down_price']) or
            (w['winner'] == 'DOWN' and w['down_price'] < w['up_price'])
        ) / n

        avg_dog_p = statistics.mean(w['dog_price'] for w in ws)
        avg_fav_p = statistics.mean(w['fav_price'] for w in ws)
        ev_dog = 5 * (dog_wr * (1 - avg_dog_p) - fav_wr * avg_dog_p)
        ev_fav = 5 * (fav_wr * (1 - avg_fav_p) - dog_wr * avg_fav_p)

        print(f"{label:>12} | {n:5d} | {up_pct:5.1%} | {fav_wr:5.1%} | {dog_wr:5.1%} | "
              f"{cheap_wr:6.1%} | ${ev_dog:+7.3f} | ${ev_fav:+7.3f}")

    # Contrarian by spread
    print("\n--- Always Bet Cheap Side, by Spread ---")
    for label, ws in spread_buckets.items():
        if not ws or len(ws) < 30:
            continue
        n = len(ws)
        pnl = 0
        wins = 0
        for w in ws:
            if w['up_price'] < w['down_price']:
                bet_side, entry = 'UP', w['up_price']
            else:
                bet_side, entry = 'DOWN', w['down_price']
            won = w['winner'] == bet_side
            pnl += 5 * ((1 - entry) if won else -entry)
            if won:
                wins += 1
        wr = wins / n
        ev = pnl / n
        print(f"  {label:>12}: WR={wr:.1%}, EV=${ev:+.3f}/trade, N={n}, Cum=${pnl:+.1f}")


def analyze_entry_price_buckets(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 6b: CONTRARIAN BY ENTRY PRICE BUCKET")
    print("=" * 100)

    price_buckets = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0, 'entries': []})

    for w in windows:
        if w['up_price'] < w['down_price']:
            bet_side, entry = 'UP', w['up_price']
        else:
            bet_side, entry = 'DOWN', w['down_price']

        if entry < 0.30:
            bucket = '<0.30'
        elif entry < 0.35:
            bucket = '0.30-0.35'
        elif entry < 0.40:
            bucket = '0.35-0.40'
        elif entry < 0.45:
            bucket = '0.40-0.45'
        elif entry < 0.50:
            bucket = '0.45-0.50'
        else:
            bucket = '>=0.50'

        won = w['winner'] == bet_side
        trade_pnl = 5 * ((1 - entry) if won else -entry)

        pb = price_buckets[bucket]
        pb['total'] += 1
        if won:
            pb['wins'] += 1
        pb['pnl'] += trade_pnl
        pb['entries'].append(entry)

    print(f"\n{'Entry':>12} | {'N':>5} | {'WR':>6} | {'AvgEntry':>8} | {'EV/trade':>9} | {'CumPnL':>9}")
    print("-" * 65)

    for bucket in ['<0.30', '0.30-0.35', '0.35-0.40', '0.40-0.45', '0.45-0.50', '>=0.50']:
        pb = price_buckets[bucket]
        if pb['total'] == 0:
            continue
        wr = pb['wins'] / pb['total']
        avg_entry = statistics.mean(pb['entries'])
        ev = pb['pnl'] / pb['total']
        print(f"{bucket:>12} | {pb['total']:5d} | {wr:5.1%} | {avg_entry:7.3f} | ${ev:+8.3f} | ${pb['pnl']:+8.1f}")


def analyze_combined_filters(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 7: COMBINED FILTER STRATEGIES")
    print("=" * 100)

    sorted_w = sorted(windows, key=lambda w: w['ts'])

    # Build previous winner lookup
    prev_winners = {}
    for i, w in enumerate(sorted_w):
        prevs = []
        for j in range(1, 6):
            if i - j >= 0 and w['ts'] - sorted_w[i-j]['ts'] <= 310 * j + 30:
                prevs.append(sorted_w[i-j]['winner'])
            else:
                break
        prev_winners[w['ts']] = prevs

    def cheap_side(w):
        if w['up_price'] < w['down_price']:
            return 'UP', w['up_price']
        return 'DOWN', w['down_price']

    strategies = []

    # 1: Baseline
    strategies.append(("Always Cheap (baseline)", lambda w, p: cheap_side(w), lambda w: True))

    # 2-3: Hour filters
    strategies.append(("Cheap + Hours 6-14", lambda w, p: cheap_side(w),
                       lambda w: 6 <= w['hour'] <= 14))
    strategies.append(("Cheap + Hours 13-21", lambda w, p: cheap_side(w),
                       lambda w: 13 <= w['hour'] <= 21))

    # 4-5: Spread filters
    strategies.append(("Cheap + Spread<0.02", lambda w, p: cheap_side(w),
                       lambda w: 0 < w['avg_spread'] < 0.02))
    strategies.append(("Cheap + Spread>0.05", lambda w, p: cheap_side(w),
                       lambda w: w['avg_spread'] > 0.05))

    # 6-7: Entry price filters
    def cheap_entry_filter(w, lo, hi):
        s, e = cheap_side(w)
        if lo <= e < hi:
            return s, e
        return None, None
    strategies.append(("Cheap + Entry<0.40",
                       lambda w, p: cheap_entry_filter(w, 0, 0.40),
                       lambda w: True))
    strategies.append(("Cheap + Entry 0.40-0.50",
                       lambda w, p: cheap_entry_filter(w, 0.40, 0.50),
                       lambda w: True))
    strategies.append(("Cheap + Entry 0.35-0.45",
                       lambda w, p: cheap_entry_filter(w, 0.35, 0.45),
                       lambda w: True))

    # 8-10: Streak-based
    def contrarian_after_n(w, prevs, n_streak):
        if len(prevs) < n_streak:
            return None, None
        if len(set(prevs[:n_streak])) == 1:
            streak_side = prevs[0]
            bet = 'DOWN' if streak_side == 'UP' else 'UP'
            entry = w['up_price'] if bet == 'UP' else w['down_price']
            return bet, entry
        return None, None

    strategies.append(("Contrarian after 2-streak",
                       lambda w, p: contrarian_after_n(w, p, 2),
                       lambda w: True))
    strategies.append(("Contrarian after 3-streak",
                       lambda w, p: contrarian_after_n(w, p, 3),
                       lambda w: True))

    def momentum_after_n(w, prevs, n_streak):
        if len(prevs) < n_streak:
            return None, None
        if len(set(prevs[:n_streak])) == 1:
            bet = prevs[0]
            entry = w['up_price'] if bet == 'UP' else w['down_price']
            return bet, entry
        return None, None

    strategies.append(("Momentum after 2-streak",
                       lambda w, p: momentum_after_n(w, p, 2),
                       lambda w: True))
    strategies.append(("Momentum after 3-streak",
                       lambda w, p: momentum_after_n(w, p, 3),
                       lambda w: True))

    # 11: Cheap + after 2-streak (only bet cheap side after streaks)
    def cheap_after_streak(w, prevs, n_streak):
        if len(prevs) < n_streak:
            return None, None
        if len(set(prevs[:n_streak])) == 1:
            return cheap_side(w)
        return None, None
    strategies.append(("Cheap + after 2-streak",
                       lambda w, p: cheap_after_streak(w, p, 2),
                       lambda w: True))

    # 12: Combined hour + spread
    strategies.append(("Cheap+H6-14+Sprd<0.03", lambda w, p: cheap_side(w),
                       lambda w: 6 <= w['hour'] <= 14 and 0 < w['avg_spread'] < 0.03))

    # 13-14: Direction biases
    strategies.append(("Always DOWN", lambda w, p: ('DOWN', w['down_price']), lambda w: True))
    strategies.append(("Always UP", lambda w, p: ('UP', w['up_price']), lambda w: True))

    # 15: Contrarian + cheap + hour filter
    strategies.append(("Contr3+Cheap+H6-14",
                       lambda w, p: contrarian_after_n(w, p, 3) if 6 <= w['hour'] <= 14 else (None, None),
                       lambda w: True))

    # 16: Contrarian + cheap entry 0.40-0.50
    def contr3_cheap_40_50(w, p):
        side, entry = contrarian_after_n(w, p, 3)
        if side is None:
            return None, None
        # Check if contrarian bet side has cheap entry
        if 0.40 <= entry < 0.50:
            return side, entry
        return None, None
    strategies.append(("Contr3+Entry0.40-0.50", contr3_cheap_40_50, lambda w: True))

    # 17: Contrarian after 2 fav wins + spread < 0.03
    def contr2_tight(w, prevs):
        if len(prevs) < 2:
            return None, None
        if prevs[0] == prevs[1]:
            bet = 'DOWN' if prevs[0] == 'UP' else 'UP'
            entry = w['up_price'] if bet == 'UP' else w['down_price']
            if w['avg_spread'] < 0.03 and w['avg_spread'] > 0:
                return bet, entry
        return None, None
    strategies.append(("Contr2+Spread<0.03", contr2_tight, lambda w: True))

    # 18: Always bet DOWN when entry < 0.45 (testing DOWN bias at cheap levels)
    def down_cheap(w, p):
        if w['down_price'] < 0.45:
            return 'DOWN', w['down_price']
        return None, None
    strategies.append(("DOWN when dn_price<0.45", down_cheap, lambda w: True))

    print(f"\n{'Strategy':>30} | {'N':>5} | {'WR':>6} | {'EV/trd':>8} | {'CumPnL':>9} | "
          f"{'$200 Surv':>9} | {'MinEquity':>9}")
    print("-" * 100)

    for name, bet_fn, filter_fn in strategies:
        wins = 0
        total = 0
        pnl_total = 0
        equity = 200
        min_equity = 200

        for w in sorted_w:
            if not filter_fn(w):
                continue
            prevs = prev_winners.get(w['ts'], [])
            side, entry = bet_fn(w, prevs)
            if side is None:
                continue

            won = w['winner'] == side
            trade_pnl = 5 * ((1 - entry) if won else -entry)
            total += 1
            if won:
                wins += 1
            pnl_total += trade_pnl
            equity += trade_pnl
            min_equity = min(min_equity, equity)

        if total == 0:
            continue

        wr = wins / total
        ev = pnl_total / total
        survives = min_equity > 0

        print(f"{name:>30} | {total:5d} | {wr:5.1%} | ${ev:+7.3f} | ${pnl_total:+8.1f} | "
              f"{'YES' if survives else 'NO':>9} | ${min_equity:+8.1f}")

    return sorted_w, prev_winners


def analyze_favorite_mispricing(windows):
    print("\n" + "=" * 100)
    print("ANALYSIS 8: MARKET MISPRICING BY FAVORITE PRICE")
    print("=" * 100)

    fav_buckets = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl_fav': 0, 'pnl_dog': 0})

    for w in windows:
        fp = w['fav_price']
        if fp < 0.52:
            bucket = '0.50-0.52'
        elif fp < 0.55:
            bucket = '0.52-0.55'
        elif fp < 0.58:
            bucket = '0.55-0.58'
        elif fp < 0.60:
            bucket = '0.58-0.60'
        elif fp < 0.65:
            bucket = '0.60-0.65'
        else:
            bucket = '>=0.65'

        fav_won = w['winner'] == w['favorite']
        fb = fav_buckets[bucket]
        fb['total'] += 1
        if fav_won:
            fb['wins'] += 1

        fb['pnl_fav'] += 5 * ((1 - fp) if fav_won else -fp)
        dp = w['dog_price']
        fb['pnl_dog'] += 5 * ((1 - dp) if not fav_won else -dp)

    print(f"\n{'FavPrice':>12} | {'N':>5} | {'FavWR':>6} | {'DogWR':>6} | "
          f"{'FavEV/5':>8} | {'DogEV/5':>8} | {'Better':>7} | {'Mispriced?':>10}")
    print("-" * 85)

    for bucket in ['0.50-0.52', '0.52-0.55', '0.55-0.58', '0.58-0.60', '0.60-0.65', '>=0.65']:
        fb = fav_buckets[bucket]
        if fb['total'] < 20:
            continue
        wr = fb['wins'] / fb['total']
        dog_wr = 1 - wr
        ev_fav = fb['pnl_fav'] / fb['total']
        ev_dog = fb['pnl_dog'] / fb['total']
        better = 'FAV' if ev_fav > ev_dog else 'DOG'

        # Is the favorite overpriced? Compare actual WR vs implied prob
        if bucket == '>=0.65':
            imp = 0.68
        else:
            parts = bucket.split('-')
            lo = float(parts[0])
            hi = float(parts[1])
            imp = (lo + hi) / 2

        mispriced = 'OVER' if wr < imp - 0.02 else 'UNDER' if wr > imp + 0.02 else 'FAIR'

        print(f"{bucket:>12} | {fb['total']:5d} | {wr:5.1%} | {dog_wr:5.1%} | "
              f"${ev_fav:+7.3f} | ${ev_dog:+7.3f} | {better:>7} | {mispriced:>10}")


def analyze_time_of_window_in_hour(windows):
    """Extra: does position within the hour matter (first window vs last)?"""
    print("\n" + "=" * 100)
    print("ANALYSIS 9: POSITION WITHIN HOUR (minute 0, 5, 10, ... 55)")
    print("=" * 100)

    by_minute = defaultdict(list)
    for w in windows:
        minute = w['dt'].minute
        # Round to nearest 5
        minute_bucket = (minute // 5) * 5
        by_minute[minute_bucket].append(w)

    print(f"\n{'Minute':>6} | {'N':>5} | {'UP%':>6} | {'FavWR':>6} | {'CheapWR':>7} | {'CheapEV/5':>9}")
    print("-" * 55)

    for minute in range(0, 60, 5):
        ws = by_minute.get(minute, [])
        if not ws:
            continue
        n = len(ws)
        up_pct = sum(1 for w in ws if w['winner'] == 'UP') / n
        fav_wr = sum(1 for w in ws if w['winner'] == w['favorite']) / n

        pnl = 0
        wins = 0
        for w in ws:
            if w['up_price'] < w['down_price']:
                bet, entry = 'UP', w['up_price']
            else:
                bet, entry = 'DOWN', w['down_price']
            won = w['winner'] == bet
            pnl += 5 * ((1 - entry) if won else -entry)
            if won:
                wins += 1

        cheap_wr = wins / n
        ev = pnl / n

        print(f"{minute:6d} | {n:5d} | {up_pct:5.1%} | {fav_wr:5.1%} | {cheap_wr:6.1%} | ${ev:+8.3f}")


def analyze_time_stability(windows):
    """Check if patterns are stable over time or just artifacts of one period."""
    print("\n" + "=" * 100)
    print("ANALYSIS 10: TEMPORAL STABILITY (weekly breakdown)")
    print("=" * 100)

    # Group by week
    by_week = defaultdict(list)
    for w in windows:
        week_start = w['dt'] - datetime.timedelta(days=w['dt'].weekday())
        week_key = week_start.strftime('%m-%d')
        by_week[week_key].append(w)

    print(f"\n{'Week':>8} | {'N':>5} | {'UP%':>6} | {'FavWR':>6} | {'CheapWR':>7} | "
          f"{'CheapEV/5':>9} | {'CumCheap':>9}")
    print("-" * 75)

    cum_pnl = 0
    for week in sorted(by_week.keys()):
        ws = by_week[week]
        n = len(ws)
        up_pct = sum(1 for w in ws if w['winner'] == 'UP') / n
        fav_wr = sum(1 for w in ws if w['winner'] == w['favorite']) / n

        pnl = 0
        wins = 0
        for w in ws:
            if w['up_price'] < w['down_price']:
                bet, entry = 'UP', w['up_price']
            else:
                bet, entry = 'DOWN', w['down_price']
            won = w['winner'] == bet
            pnl += 5 * ((1 - entry) if won else -entry)
            if won:
                wins += 1

        cheap_wr = wins / n
        ev = pnl / n
        cum_pnl += pnl

        print(f"{week:>8} | {n:5d} | {up_pct:5.1%} | {fav_wr:5.1%} | {cheap_wr:6.1%} | "
              f"${ev:+8.3f} | ${cum_pnl:+8.1f}")


def main():
    conn = sqlite3.connect(DB_PATH)

    print("Building window-level dataset from market_snapshots + candles...")
    windows = build_windows(conn)
    print(f"Built {len(windows)} windows with outcome data")
    if not windows:
        print("ERROR: No windows built!")
        return

    windows.sort(key=lambda w: w['ts'])
    print(f"Date range: {windows[0]['dt']} to {windows[-1]['dt']}")

    up_count = sum(1 for w in windows if w['winner'] == 'UP')
    print(f"Overall: UP={up_count/len(windows):.1%}, DOWN={1-up_count/len(windows):.1%} (N={len(windows)})")

    analyze_hour_of_day(windows)
    analyze_day_of_week(windows)
    analyze_volatility_regimes(windows)
    analyze_consecutive_patterns(windows)
    analyze_btc_price_levels(windows)
    analyze_spread_as_predictor(windows)
    analyze_entry_price_buckets(windows)
    analyze_combined_filters(windows)
    analyze_favorite_mispricing(windows)
    analyze_time_of_window_in_hour(windows)
    analyze_time_stability(windows)

    # Final summary
    print("\n" + "=" * 100)
    print("SUMMARY OF KEY FINDINGS")
    print("=" * 100)
    print("""
Key questions answered:
1. Are there exploitable hour-of-day or day-of-week biases?
2. Does volatility regime affect favorite/underdog edge?
3. Is there mean reversion or momentum in consecutive windows?
4. Does BTC price level matter?
5. Does spread width predict anything?
6. Do any combined filters produce survivable positive EV?

Look at the numbers above and check:
- Any EV/trade above +$0.10 with N > 100 is interesting
- Any win rate above 53% for cheap side (or below 47% for favorite) is notable
- Survival from $200 means the drawdown is manageable
""")

    conn.close()


if __name__ == '__main__':
    main()
