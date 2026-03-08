from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Strategy
    min_edge_threshold: float = 0.05
    max_edge_threshold: float = 0.18     # cap — edges above this are likely model error, not mispricing
    max_signal_value: float = 0.45       # skip when any single signal is saturated (near +-0.5 limits)
    bet_size_usdc: float = 5.0
    virtual_bankroll: float = 100.0
    use_kelly: bool = False

    # ML model — use trained ML model instead of rule-based probability model
    use_ml_model: bool = True

    # Always-trade mode — enter a position every 5-min window
    # Instead of waiting for edge > min_edge, always trade the ML model's
    # predicted direction.  Edge is still computed for bet sizing.
    always_trade: bool = True

    # Minimum ML confidence (|P(up) - 0.5|) required to place a trade.
    # Backtested sweet spot: 0.015 gives 56.9% WR on 53% of windows ($59/day).
    min_confidence: float = 0.015

    # Sizing strategy: "fixed", "kelly", "adaptive"
    # - fixed: flat bet_size_usdc every trade
    # - kelly: half-Kelly based on edge
    # - adaptive: hybrid adaptive (confidence + streak + drawdown + rolling WR)
    sizing_strategy: str = "adaptive"

    # Live trading on Polymarket (real money via py-clob-client)
    live_trading: bool = False
    polymarket_private_key: str = ""       # EOA private key (hex, no 0x prefix)
    polymarket_funder_address: str = ""    # Proxy wallet from polymarket.com settings
    max_live_bet_usdc: float = 10.0       # Hard safety cap per live trade
    clob_proxy: str = ""                   # SOCKS5 proxy for CLOB API (e.g. socks5://127.0.0.1:1080)

    # Polymarket Builder Mode (legacy — not used by live_trader)
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""

    # Polymarket fee parameters (crypto 5-min / 15-min markets)
    # These are the curve parameters from the Maker Rebates Program docs.
    # fee_per_share = price * fee_rate * (price * (1 - price))^fee_exponent
    # At price=0.50 with defaults: effective fee ~ 1.56% of trade value.
    polymarket_fee_rate: float = 0.25
    polymarket_fee_exponent: int = 2

    # Data URLs
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"

    # Regime detection — strength threshold for trending classification
    regime_trend_threshold: float = 0.30
    # Regime flip — flip paper signal to trend-following when |strength| >= this
    # Higher than trend_threshold: 0.30-0.40 is classified trending but not flipped
    regime_flip_threshold: float = 0.35

    # Regime flip — applies to live trades (not just paper) when trending
    regime_flip_live: bool = True         # enable regime flip for live trades
    # Confirmation window — require N consecutive trending windows before flipping.
    # Prevents flickering: brief 1-2 window spikes past threshold won't trigger flips.
    # 3 windows = 15 min of sustained trend.  Tunable via .env.
    regime_flip_confirm_windows: int = 1

    # Loss streak guard — pause a side after consecutive losses
    streak_pause_threshold: int = 3       # consecutive same-side losses to trigger
    streak_pause_windows: int = 2         # number of 5-min windows to pause (~10 min)

    # Confidence dampening — shrink P(up) toward 0.5 to counter model
    # overconfidence.  1.0 = no dampening, 0.0 = always 50%.
    # Was 0.6 but EE safety net makes low-confidence trades profitable
    # (~+$1.55/trade even at 50% WR). Dampening just kills volume.
    confidence_dampen: float = 1.0

    # Adaptive early exit — tiered thresholds by entry price
    # Lower entry prices have lower WR and benefit from aggressive exits.
    # Data: 548 trades with 102K market snapshots (bid spike analysis).
    early_exit_threshold_low: float = 0.50     # entry < 0.35: lottery tickets, exit on any spike
    early_exit_threshold_low_mid: float = 0.45 # entry 0.35-0.40: brief spikes, grab profit fast
    early_exit_threshold_mid: float = 0.65     # entry 0.40-0.50: decent exit rate at 0.65
    early_exit_threshold_high: float = 0.95    # entry >= 0.50 (~55% WR, conservative)

    # Hour blacklist (UTC hours where model underperforms; skip trading)
    blacklist_hours: str = ""  # comma-separated UTC hours, e.g. "2,11,19"

    # Feature weights (v2 — rebalanced from 2,736-trade analysis)
    # Reduced OBI/momentum (near-zero predictive delta), added volume_zscore
    # (validated strongest feature) and regime (multi-window trend memory).
    w_obi: float = 0.05
    w_taker: float = 0.25
    w_momentum: float = 0.05
    w_rsi: float = 0.10
    w_vwap: float = 0.10
    w_funding: float = 0.10
    w_volume_zscore: float = 0.15
    w_regime: float = 0.20

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
