"""Recalibrate portfolio simulation with actual CLOB balance."""

offset = 64.14 - 49.11  # maker fill profit not tracked in DB

scenarios = {
    "A: ACTUAL":         49.11,
    "B: FLIP >= 0.35":  135.48,
    "C: SKIP trending":  84.33,
    "D: NO early exit": -31.85,
}

print("CALIBRATED PORTFOLIO SIMULATION (adjusted for maker fill offset)")
print("=" * 65)
print(f"  Actual CLOB balance: $64.14")
print(f"  Simulated balance:   $49.11")
print(f"  Maker fill offset:   +${offset:.2f}")
print()
print(f"  {'Scenario':<25s}  {'Balance':>8s}  {'Return':>8s}  {'ROI':>6s}")
print(f"  {'-'*25}  {'-'*8}  {'-'*8}  {'-'*6}")
for name, raw in scenarios.items():
    adjusted = raw + offset
    ret = adjusted - 73
    pct = ret / 73 * 100
    print(f"  {name:<25s}  ${adjusted:7.2f}  ${ret:+7.2f}  {pct:+5.0f}%")

print()
print("PEAK / TROUGH (calibrated)")
print("=" * 65)
data = [
    ("Actual",  131.29 + offset, "03-01 13:05", 25.70 + offset, "03-03 09:30"),
    ("Flip",    155.88 + offset, "03-03 15:35", 45.96 + offset, "03-02 04:05"),
    ("Skip",    131.29 + offset, "03-01 13:05", 45.96 + offset, "03-02 04:05"),
    ("NoExit",  127.96 + offset, "03-01 12:10", -48.40 + offset, "03-03 09:30"),
]
for name, peak, pt, trough, tt in data:
    dd = peak - trough
    print(f"  {name:8s}: Peak ${peak:.0f} ({pt}) | "
          f"Trough ${trough:.0f} ({tt}) | Max DD: ${dd:.0f}")

print()
print("DAILY P&L (calibrated, offset spread evenly across 2.7 days)")
print("=" * 65)
daily_offset = offset / 2.7
days = [
    ("2026-03-01", 21.55, 21.55,  21.55,   8.74),
    ("2026-03-02",-39.37, -5.30, -20.63, -69.04),
    ("2026-03-03", -7.04, 45.26,   9.44, -45.53),
]
print(f"  {'Day':>12s}  {'Actual':>9s}  {'Flip':>9s}  {'Skip':>9s}  {'NoExit':>9s}")
print(f"  {'-'*12}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*9}")
for day, a, f, s, n in days:
    # Add offset proportionally
    a2 = a + daily_offset
    f2 = f + daily_offset
    s2 = s + daily_offset
    n2 = n + daily_offset
    print(f"  {day:>12s}  ${a2:+8.2f}  ${f2:+8.2f}  ${s2:+8.2f}  ${n2:+8.2f}")

print()
print("THE STORY:")
print("=" * 65)
actual_bal = 49.11 + offset
flip_bal = 135.48 + offset
print(f"  Started:  $73")
print(f"  Now:      ${actual_bal:.0f} (actual)")
print(f"  Could be: ${flip_bal:.0f} (with flip + early exit from day 1)")
print(f"  Missed:   ${flip_bal - actual_bal:.0f} in 2.7 days")
print(f"  Daily projected with both engines: ~${(flip_bal - 73) / 2.7:.0f}/day")
