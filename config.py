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

    # Polymarket Builder Mode (for live trading — leave blank for paper-only)
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
