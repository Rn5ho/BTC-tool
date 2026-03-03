# Known Issues & Fixes Applied

1. **Edge detector betting wrong side** (CRITICAL — fixed): Picked largest *absolute* edge instead of largest *positive* edge, causing systematic wrong-direction bets.

2. **Trades never settling after restart** (fixed): `_current_slug` started as `None`, skipping settlement. Fixed with startup recovery + `_settle_stale_trades()`.

3. **Settlement gated behind market discovery** (fixed): Decoupled settlement from Gamma API by checking slug transitions first.

4. **Late-window entries** (fixed): Time gate `_MAX_ENTRY_SECONDS=120` prevents stale-edge entries.

5. **Contrarian bets against strong trends** (fixed): Trend-conflict filter `_TREND_CONFLICT_PCT=0.15`.

6. **Chainlink stale settlement prices** (fixed): Switched from on-chain aggregator (1h heartbeat) to Polymarket RTDS WebSocket stream.

7. **Windows cp1252 encoding** (fixed): Telegram emojis at DEBUG level only, ASCII-safe console.

8. **Binance historical data microsecond timestamps** (fixed in ml_pipeline.py): Binance CSV files use 16-digit microsecond timestamps. Fixed with `if ts > 1_000_000_000_000_000: ts = ts // 1000`.

9. **systemctl restart hangs** (known): The service has a 90s SIGTERM timeout. Use `systemctl kill -s SIGKILL btc-edge` for fast restarts.

10. **py-clob-client maker rounding overshoot** (fixed): Floating point `4.36 * 0.63 = 2.7468000000000004` caused `round_up` to produce `2.7469`, rejected by CLOB. Fixed by using `round_down` for maker amount.

11. **425 "Too Early" on order placement** (fixed): Matching engine restarts cause transient 425 errors. Fixed with exponential backoff retry (3 attempts, 3s/6s/12s delays).

12. **Auto-sell fails on resolved markets** (known — mitigated): CLOB order book closes when 5-min markets resolve. Selling via CLOB after resolution doesn't work. **Mitigated by early exit selling**: tokens are sold *before* resolution when bid >= $0.95. Remaining unsold tokens (losers, low-bid winners) are claimed manually on polymarket.com.

13. **SCP to wrong path shadows packages** (fixed): `scp alerts/telegram.py root@host:/home/btcedge/BTC-tool/` creates `telegram.py` in project root, shadowing the `telegram` package. Always SCP to the full subdirectory path.

14. **CLOB fill price shift causes <5 token failure** (fixed): Entry price signal was $0.385 but CLOB filled at ~$0.47, reducing token count below 5-token minimum. Fixed with `$3.50` hard floor: `min_usdc = max(round(5.5 * price, 2), 3.50)` guarantees 5 tokens at any fill price up to $0.70.

15. **SELL MarketOrderArgs amount = token count, not USDC** (fixed): `MarketOrderArgs(amount, side=SELL)` expects token count. Early exit was passing `tokens × bid` (USDC value), causing the CLOB to sell fewer tokens and leave a residual position behind. Fixed to pass `round(tokens, 2)` directly.

16. **Flat close settlement wrong direction** (fixed): `btc_went_up = btc_end >= btc_start` treated ties (flat closes) as UP. Polymarket resolves flat as DOWN (price didn't go UP). Fixed to strict `>` in 4 locations (main.py x3, paper_trader.py x1).

17. **Maker amount Decimal precision** (fixed): Float `5.93 * 0.59 = 3.4986999...` but CLOB expects `3.4987`. Even `round_down` couldn't fix float imprecision. Fixed by using `Decimal(str(taker)) * Decimal(str(price))` for exact arithmetic.

18. **Telegram HTML parse error on skip notifications** (fixed): `$67,093.37 -> $67,019.64` — the `->` was interpreted as HTML tag closer. Fixed with `parse_mode=None` (plain text) for skip notifications.

19. **Signal breakdown showing wrong model output** (fixed): `get_signal_breakdown()` re-ran ML model with degraded 12-feature fallback, showing different (sometimes opposite) direction from actual trade. Fixed: edge detector `evaluate()` now embeds actual `p_up`, `confidence`, `best_side` directly in the signals dict.

20. **Early exit retry spam on 1s loop** (fixed): After moving early exit to dedicated 1s loop, failed sell attempts retried every second. Fixed with `exit_failed` flag on the position dict.

21. **DB PnL understates real profits** (known — mitigated): DB tracks simulated settlements while CLOB has real fills, maker trades (counterparty hitting our GTC orders), and manual redemptions. Gap was ~$30 after 14 hours. **Mitigated by syncing bankroll from real CLOB balance** on startup and every 30 min.

22. **`sell_early_exit` NameError on success log** (fixed): `sell_amount` variable was undefined at line 569 of `live_trader.py`. Every successful early exit sell crashed the log line, caught by `except`, logged "EARLY EXIT FAILED" even though the CLOB order went through. Fixed: replaced with `expected_usdc`.

23. **Duplicate Telegram alerts for early-exited trades** (fixed): `_settle_previous_window()` Path B sent WIN/LOSS settlement alerts for trades that were already early-exited. Fixed: added `if not live_info.get("exited")` guard.

24. **No HTTP timeout on Polymarket API** (fixed): `aiohttp.ClientSession()` had no timeout — a hung API call would freeze the entire bot. Fixed: added `aiohttp.ClientTimeout(total=15)`.

25. **`/status` showed wrong P(up)** (fixed): Used degraded 12-feature `predict()` fallback instead of `predict_from_candles()` with full 44 features. Same class of bug as #19. Fixed: `/status` now uses `predict_from_candles()` when ML model is active.

26. **NaN in ML features could crash prediction** (fixed): No NaN check before `scaler.transform()`. A malformed Binance candle would propagate NaN through the entire feature vector. Fixed: added `np.isnan` guard in both `predict()` and `predict_from_candles()`.

27. **`settled_at` timestamp inconsistency** (fixed): Early exit stored `settled_at` in seconds (`int(time.time())`), normal settlement in milliseconds (`int(time.time() * 1000)`). Fixed: early exit now uses milliseconds consistently.

28. **No live trade dedup guard** (fixed): Paper trader checked `if slug in _pending_trades`, but live trader had no equivalent guard. Could theoretically double-order on same window. Fixed: added `if market.slug in self._live_trade_tokens: return`.

29. **Missing DB index on live_trades settlement queries** (fixed): `WHERE success = 1 AND outcome IS NULL` ran as full table scan every settlement cycle. Added composite index `idx_live_trades_outcome ON live_trades(success, outcome)`.

30. **Gamma API too slow for real-time settlement** (known — mitigated): 5-minute markets aren't marked as `closed` in Gamma API within 35s of resolution. Real-time settlement falls back to Chainlink price comparison. Mitigated by periodic `_settle_stale_live_trades()` every 30 min which successfully uses Gamma for older trades, catching any price-based mismatches.

31. **`_window_btc_start` None after restart** (fixed): On restart, Chainlink and Binance streams aren't connected yet, so `_window_btc_start` was set to None. With Gamma also too slow, settlement was silently skipped. Fixed: (1) removed early return on None start price — Gamma can settle without prices, (2) added late price retry in `_run_one_cycle` once streams connect.

32. **Mean-reversion model bias in trending markets** (known — monitoring): ML model's top features (RSI, VWAP deviation, BB position) are all mean-reversion indicators. In sustained trends, the model keeps calling for reversals that don't come. Evidence: 15+ consecutive UP calls during a downtrend with ~22% WR. All-time UP trades: 51.4% WR vs DOWN: 59.0% WR. Future fix: regime detector to identify trending vs ranging markets.
