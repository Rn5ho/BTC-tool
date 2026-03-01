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
    max_live_bet_usdc: float = 2.0        # Hard safety cap per live trade
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

    # Confidence dampening — shrink P(up) toward 0.5 to counter model
    # overconfidence.  1.0 = no dampening, 0.0 = always 50%.
    # Calibration data shows ~10-20% overconfidence; 0.6 is a good fit.
    confidence_dampen: float = 0.6

    # Hour blacklist (UTC hours where model underperforms; skip trading)
    blacklist_hours: str = "2"  # comma-separated UTC hours, e.g. "2,11,19"

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
