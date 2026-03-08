"""
Phased historical simulation: What did we LOSE by not having current rules from day 1?

Three scenarios compared side-by-side:
  A) ACTUAL    -- what really happened (from DB)
  B) PHASED    -- rules applied only from their real deployment dates
  C) FULL      -- all current rules applied from day 1 (counterfactual)

Deployment timeline (from CLAUDE.md changelog):
  Feb 28 23:50  -- Live trading launched ($1 bets, no EE, no filters)
  Mar 1 ~11:30  -- Basic early exit (last 2 min, single threshold ~0.65)
  Mar 2 ~14:00  -- Entry time gate tightened 120s -> 60s
  Mar 2 ~18:00  -- Adaptive early exit (full window, 3-tier thresholds)
  Mar 3 ~20:30  -- 4-tier early exit (current thresholds)
  Mar 3 ~21:30  -- Regime flip + streak guard deployed
  Mar 3 ~22:30  -- Confidence dampening fix, early exit 10s->1s
  Mar 4 ~07:45  -- Exploration threshold raised 0.35 -> 0.40
  Mar 4 ~10:12  -- Max bet raised $5 -> $10

Bankroll: $73 start + $43 deposit on Mar 3
"""

import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

# Current rules
INITIAL_BANKROLL = 73.0
DEPOSIT_DATE = "2026-03-03"
DEPOSIT_AMOUNT = 43.0
FEE_RATE = 0.25
FEE_EXPONENT = 2

HOUR_MULTIPLIERS = {
    4: 0.7, 7: 0.7,
    6: 1.2, 8: 1.2, 10: 1.2, 12: 1.2,
    16: 1.2, 18: 1.2, 22: 1.2,
    9: 1.5, 14: 1.5, 20: 1.5,
}

def get_exit_threshold(entry_price):
    if entry_price < 0.35: return 0.50
    elif entry_price < 0.40: return 0.45
    elif entry_price < 0.50: return 0.65
    else: return 0.95

def compute_fee_factor(p):
    return (FEE_RATE / 10000) * (p ** FEE_EXPONENT)

def pnl_win(amt, ep):
    fee = compute_fee_factor(ep)
    shares = (amt / ep) * (1.0 - fee)
    return shares - amt

def pnl_loss(amt):
    return -amt

def compute_bet(bankroll, hour, entry_price, max_bet):
    base = bankroll * 0.08
    mult = HOUR_MULTIPLIERS.get(hour, 1.0)
    size = base * mult
    size = max(1.0, min(size, max_bet))
    min_usdc = max(5.5 * entry_price, 3.50)
    size = max(size, min_usdc)
    size = min(size, max_bet)
    return round(size, 2)


class Scenario:
    def __init__(self, name):
        self.name = name
        self.bankroll = INITIAL_BANKROLL
        self.deposit_added = False
        self.pnl = 0.0
        self.peak = INITIAL_BANKROLL
        self.max_dd = 0.0
        self.wins = 0
        self.losses = 0
        self.ee = 0
        self.skipped = 0
        self.volume = 0.0
        self.daily = defaultdict(lambda: {"t":0, "w":0, "l":0, "ee":0, "pnl":0.0, "skip":0, "vol":0.0})
        self.curve = []
        # streak guard
        self.streak_up = 0
        self.streak_down = 0
        self.pause_up = None
        self.pause_down = None

    def add_deposit(self, day):
        if not self.deposit_added and day >= DEPOSIT_DATE:
            self.bankroll += DEPOSIT_AMOUNT
            self.deposit_added = True

    def record(self, day, outcome, trade_pnl, bet_size):
        self.bankroll += trade_pnl
        self.pnl += trade_pnl
        self.volume += bet_size
        d = self.daily[day]
        d["t"] += 1
        d["pnl"] += trade_pnl
        d["vol"] += bet_size
        if outcome == "WIN": self.wins += 1; d["w"] += 1
        elif outcome == "LOSS": self.losses += 1; d["l"] += 1
        elif outcome == "EARLY_EXIT": self.ee += 1; d["ee"] += 1
        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = self.peak - self.bankroll
        if dd > self.max_dd:
            self.max_dd = dd

    def skip(self, day):
        self.skipped += 1
        self.daily[day]["skip"] += 1

    def snapshot(self, ts):
        self.curve.append((ts, self.bankroll, self.pnl))

    def wr(self):
        d = self.wins + self.losses
        return self.wins / d * 100 if d > 0 else 0


def should_skip(entry_price, hour, confidence, regime_strength, regime_state,
                side, tag, window_key, scenario,
                use_entry_filter, use_confidence, use_streak, use_regime_flip,
                entry_min=0.40):
    """Returns (skip_reason, effective_side) or (None, effective_side)."""
    if tag == "maker_fill":
        return None, side

    effective_side = side

    # Regime flip (changes effective_side before other checks)
    if use_regime_flip and regime_strength is not None and abs(regime_strength) >= 0.35:
        if regime_state == "trending_up": effective_side = "UP"
        elif regime_state == "trending_down": effective_side = "DOWN"

    if hour == 2:
        return "blacklist", effective_side

    if use_entry_filter and entry_price is not None:
        if entry_price < entry_min:
            return "entry_price", effective_side
        if entry_price > 0.65:
            return "entry_high", effective_side

    if use_confidence and confidence is not None:
        if confidence * 0.6 < 0.015:
            return "low_conf", effective_side

    if use_streak:
        if effective_side == "UP" and scenario.pause_up and window_key < scenario.pause_up:
            return "streak_UP", effective_side
        if effective_side == "DOWN" and scenario.pause_down and window_key < scenario.pause_down:
            return "streak_DN", effective_side

    return None, effective_side


def update_streak(scenario, effective_side, outcome, window_key, tag):
    if tag == "maker_fill" or outcome == "EARLY_EXIT":
        return
    if outcome == "LOSS":
        if effective_side == "UP":
            scenario.streak_up += 1
            scenario.streak_down = 0
            if scenario.streak_up >= 3:
                dt = datetime.strptime(window_key, "%Y-%m-%d %H:%M") + timedelta(minutes=10)
                scenario.pause_up = dt.strftime("%Y-%m-%d %H:%M")
                scenario.streak_up = 0
        else:
            scenario.streak_down += 1
            scenario.streak_up = 0
            if scenario.streak_down >= 3:
                dt = datetime.strptime(window_key, "%Y-%m-%d %H:%M") + timedelta(minutes=10)
                scenario.pause_down = dt.strftime("%Y-%m-%d %H:%M")
                scenario.streak_down = 0
    elif outcome == "WIN":
        if effective_side == "UP": scenario.streak_up = 0
        else: scenario.streak_down = 0


def sim_outcome(side, effective_side, original_outcome, entry_price, bet_size,
                actual_pnl, actual_amt, max_bid, use_ee):
    """Determine simulated outcome and P&L."""
    flipped = (effective_side != side)

    if flipped:
        if original_outcome == "WIN": base_outcome = "LOSS"
        elif original_outcome == "LOSS": base_outcome = "WIN"
        else: base_outcome = original_outcome  # EE stays
    else:
        base_outcome = original_outcome

    # Try early exit simulation
    if use_ee and max_bid is not None and base_outcome != "EARLY_EXIT":
        threshold = get_exit_threshold(entry_price)
        if max_bid >= threshold:
            fee = compute_fee_factor(entry_price)
            tokens = (bet_size / entry_price) * (1.0 - fee)
            sell_fee = compute_fee_factor(threshold)
            proceeds = tokens * threshold * (1.0 - sell_fee)
            return "EARLY_EXIT", proceeds - bet_size

    if base_outcome == "EARLY_EXIT":
        if actual_amt > 0 and actual_pnl != 0:
            return "EARLY_EXIT", (bet_size / actual_amt) * actual_pnl
        else:
            threshold = get_exit_threshold(entry_price)
            fee = compute_fee_factor(entry_price)
            tokens = (bet_size / entry_price) * (1.0 - fee)
            return "EARLY_EXIT", tokens * threshold - bet_size
    elif base_outcome == "WIN":
        return "WIN", pnl_win(bet_size, entry_price)
    else:
        return "LOSS", pnl_loss(bet_size)


def main():
    conn = sqlite3.connect("btc_edge.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT created_at, side, entry_price, outcome, pnl, amount_usdc, trade_tag,
                        model_confidence, regime_state, regime_strength, max_bid_during_window,
                        market_slug
                 FROM live_trades WHERE outcome IS NOT NULL AND success=1
                 ORDER BY created_at""")
    trades = [dict(row) for row in c.fetchall()]
    conn.close()

    actual = Scenario("ACTUAL")
    phased = Scenario("PHASED")
    full   = Scenario("FULL")

    # Deployment timestamps (from CLAUDE.md)
    EE_BASIC    = "2026-03-01 11:30"   # basic early exit
    EE_ADAPTIVE = "2026-03-02 18:00"   # full window, 3-tier
    EE_4TIER    = "2026-03-03 20:30"   # current 4-tier
    REGIME_FLIP = "2026-03-03 21:30"   # regime flip + streak guard
    DAMPEN_FIX  = "2026-03-03 22:30"   # confidence dampening
    ENTRY_040   = "2026-03-04 07:45"   # exploration -> 0.40
    MAX_BET_10  = "2026-03-04 10:12"   # max bet $5 -> $10

    for t in trades:
        created = t["created_at"]
        day = created[:10]
        hour = int(created[11:13])
        window_key = created[:16]
        side = t["side"]
        ep = t["entry_price"]
        outcome = t["outcome"]
        apnl = t["pnl"] or 0.0
        aamt = t["amount_usdc"] or 0.0
        tag = t["trade_tag"]
        conf = t["model_confidence"]
        rs = t["regime_state"]
        rstr = t["regime_strength"]
        maxbid = t["max_bid_during_window"]

        for sc in [actual, phased, full]:
            sc.add_deposit(day)

        # ============ SCENARIO A: ACTUAL ============
        actual.record(day, outcome, apnl, aamt)
        actual.snapshot(created)

        # ============ SCENARIO B: PHASED ============
        # Features enabled based on when they were actually deployed
        ts = created[:16]

        # Phased max bet
        ph_max_bet = 10.0 if ts >= MAX_BET_10 else 5.0

        # Phased entry filter: <0.35 before Mar 4, <0.40 after
        ph_entry_min = 0.40 if ts >= ENTRY_040 else 0.35
        ph_use_entry = True  # always had basic entry filter (0.25-0.65)

        # Phased EE: none before Mar 1 11:30, basic after, current after Mar 3 20:30
        ph_use_ee = ts >= EE_BASIC

        # Phased regime flip + streak guard
        ph_use_regime = ts >= REGIME_FLIP
        ph_use_streak = ts >= REGIME_FLIP

        # Phased confidence dampening
        ph_use_conf = ts >= DAMPEN_FIX

        skip_r, eff_side = should_skip(
            ep, hour, conf, rstr, rs, side, tag, window_key, phased,
            use_entry_filter=ph_use_entry, use_confidence=ph_use_conf,
            use_streak=ph_use_streak, use_regime_flip=ph_use_regime,
            entry_min=ph_entry_min)

        if skip_r:
            phased.skip(day)
            phased.snapshot(created)
        else:
            bet = compute_bet(phased.bankroll, hour, ep, ph_max_bet)
            oc, pl = sim_outcome(side, eff_side, outcome, ep, bet, apnl, aamt,
                                 maxbid if ph_use_ee else None, ph_use_ee)
            phased.record(day, oc, pl, bet)
            update_streak(phased, eff_side, oc, window_key, tag)
            phased.snapshot(created)

        # ============ SCENARIO C: FULL (all current rules from day 1) ============
        skip_r, eff_side = should_skip(
            ep, hour, conf, rstr, rs, side, tag, window_key, full,
            use_entry_filter=True, use_confidence=True,
            use_streak=True, use_regime_flip=True,
            entry_min=0.40)

        if skip_r:
            full.skip(day)
            full.snapshot(created)
        else:
            bet = compute_bet(full.bankroll, hour, ep, 10.0)
            oc, pl = sim_outcome(side, eff_side, outcome, ep, bet, apnl, aamt,
                                 maxbid, True)
            full.record(day, oc, pl, bet)
            update_streak(full, eff_side, oc, window_key, tag)
            full.snapshot(created)

    # ====== PRINT RESULTS ======
    print("=" * 95)
    print("  PHASED HISTORICAL SIMULATION -- Rules applied from actual deployment dates")
    print("  Bankroll: $73 start + $43 deposit on Mar 3 = $116 total deposited")
    print("=" * 95)

    print(f"\n  DEPLOYMENT TIMELINE:")
    print(f"  Feb 28 23:50  Live trading launched ($1 bets, no EE, no filters)")
    print(f"  Mar 01 11:30  Basic early exit (last 2 min, ~0.65 threshold)")
    print(f"  Mar 02 18:00  Adaptive early exit (full window, 3-tier)")
    print(f"  Mar 03 20:30  4-tier early exit (current thresholds)")
    print(f"  Mar 03 21:30  Regime flip + streak guard")
    print(f"  Mar 03 22:30  Confidence dampening fix")
    print(f"  Mar 04 07:45  Entry filter raised 0.35 -> 0.40")
    print(f"  Mar 04 10:12  Max bet raised $5 -> $10")

    print(f"\n{'-'*95}")
    print(f"  DAILY BREAKDOWN")
    print(f"{'-'*95}")
    print(f"  {'Date':<12} {'--- ACTUAL ---':>28}  {'--- PHASED ---':>28}  {'--- FULL ---':>28}")
    print(f"  {'':12} {'T':>4} {'W':>3} {'L':>3} {'EE':>3} {'P&L':>8} {'SK':>3}  {'T':>4} {'W':>3} {'L':>3} {'EE':>3} {'P&L':>8} {'SK':>3}  {'T':>4} {'W':>3} {'L':>3} {'EE':>3} {'P&L':>8} {'SK':>3}")

    all_days = sorted(set(list(actual.daily.keys()) + list(phased.daily.keys()) + list(full.daily.keys())))
    for day in all_days:
        a = actual.daily.get(day, {"t":0,"w":0,"l":0,"ee":0,"pnl":0,"skip":0})
        p = phased.daily.get(day, {"t":0,"w":0,"l":0,"ee":0,"pnl":0,"skip":0})
        f = full.daily.get(day, {"t":0,"w":0,"l":0,"ee":0,"pnl":0,"skip":0})
        print(f"  {day:<12} {a['t']:>4} {a['w']:>3} {a['l']:>3} {a['ee']:>3} {a['pnl']:>+8.1f} {a['skip']:>3}  {p['t']:>4} {p['w']:>3} {p['l']:>3} {p['ee']:>3} {p['pnl']:>+8.1f} {p['skip']:>3}  {f['t']:>4} {f['w']:>3} {f['l']:>3} {f['ee']:>3} {f['pnl']:>+8.1f} {f['skip']:>3}")

    print(f"\n{'-'*95}")
    print(f"  SUMMARY")
    print(f"{'-'*95}")
    deposits = INITIAL_BANKROLL + DEPOSIT_AMOUNT
    for sc in [actual, phased, full]:
        taken = sc.wins + sc.losses + sc.ee
        print(f"\n  {sc.name}:")
        print(f"    Trades: {taken} taken, {sc.skipped} skipped")
        print(f"    W/L/EE: {sc.wins}/{sc.losses}/{sc.ee}  (WR: {sc.wr():.1f}%)")
        print(f"    P&L: {sc.pnl:>+.2f}$  |  Volume: {sc.volume:>.0f}$  |  ROI: {sc.pnl/sc.volume*100 if sc.volume else 0:.1f}%")
        print(f"    Bankroll: ${sc.bankroll:.2f}  (deposited ${deposits:.0f}, net {sc.bankroll - deposits:>+.2f}$)")
        print(f"    Peak: ${sc.peak:.2f}  |  Max DD: ${sc.max_dd:.2f}")

    # Cost analysis
    print(f"\n{'-'*95}")
    print(f"  COST OF MISSING FEATURES")
    print(f"{'-'*95}")
    print(f"  (Comparing PHASED vs FULL -- what we lost by deploying features late)\n")

    cost = full.pnl - phased.pnl
    print(f"  Total opportunity cost: {cost:>+.2f}$")
    print(f"  PHASED final bankroll:  ${phased.bankroll:.2f}")
    print(f"  FULL final bankroll:    ${full.bankroll:.2f}")
    print(f"  Gap:                    ${full.bankroll - phased.bankroll:.2f}\n")

    # Per-day delta
    print(f"  Per-day opportunity cost (FULL - PHASED P&L):")
    for day in all_days:
        p = phased.daily.get(day, {"pnl":0})
        f = full.daily.get(day, {"pnl":0})
        delta = f["pnl"] - p["pnl"]
        bar = "+" * int(abs(delta) / 5) if delta > 0 else "-" * int(abs(delta) / 5)
        print(f"    {day}:  {delta:>+8.1f}$  {bar}")

    # Bankroll curve comparison (sampled)
    print(f"\n{'-'*95}")
    print(f"  BANKROLL CURVE")
    print(f"{'-'*95}")
    print(f"  {'Time':<17} {'Actual':>10} {'Phased':>10} {'Full':>10}  {'Ph-Act':>8} {'Fl-Act':>8}")

    step = max(1, len(actual.curve) // 30)
    for i in range(0, len(actual.curve), step):
        at, ab, _ = actual.curve[i]
        # Find closest phased/full
        pi = min(i, len(phased.curve)-1)
        fi = min(i, len(full.curve)-1)
        _, pb, _ = phased.curve[pi]
        _, fb, _ = full.curve[fi]
        print(f"  {at[:16]:<17} ${ab:>8.0f} ${pb:>8.0f} ${fb:>8.0f}  {pb-ab:>+7.0f}$ {fb-ab:>+7.0f}$")

    # Final
    _, ab, _ = actual.curve[-1]
    _, pb, _ = phased.curve[-1]
    _, fb, _ = full.curve[-1]
    print(f"  {'-'*17} {'-'*10} {'-'*10} {'-'*10}  {'-'*8} {'-'*8}")
    print(f"  {'FINAL':<17} ${ab:>8.0f} ${pb:>8.0f} ${fb:>8.0f}  {pb-ab:>+7.0f}$ {fb-ab:>+7.0f}$")

    print(f"\n{'='*95}")
    print(f"  CAVEATS:")
    print(f"  - Regime/confidence data only exists from Mar 2-3 onward")
    print(f"  - Max bid data only from Mar 3+ (41 trades). Earlier EE uses actual outcomes")
    print(f"  - FULL scenario has hindsight bias -- filters wouldn't have had data early on")
    print(f"  - Compounding amplifies differences: a $5 win early -> bigger bets -> bigger gaps")
    print(f"  - PHASED is the most realistic 'what-if' scenario")
    print(f"{'='*95}")


if __name__ == "__main__":
    main()
