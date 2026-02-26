from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Strategy
    min_edge_threshold: float = 0.05
    min_edge_down: float = 0.08          # higher bar for DOWN trades (data shows worse win rate)
    max_edge_threshold: float = 0.18     # cap — edges above this are likely model error, not mispricing
    max_signal_value: float = 0.45       # skip when any single signal is saturated (near +-0.5 limits)
    bet_size_usdc: float = 5.0
    virtual_bankroll: float = 100.0
    use_kelly: bool = False

    # Polymarket Builder Mode (for live trading — leave blank for paper-only)
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""

    # Live trading
    polygon_private_key: str = ""       # Polygon wallet private key (hex, with or without 0x prefix)
    polygon_wallet_address: str = ""    # Polygon wallet address that holds USDC.e
    live_trading_enabled: bool = False  # Must be explicitly enabled
    live_bet_pct: float = 0.02          # Bet size as fraction of live bankroll (2%)
    live_bankroll: float = 100.0        # Starting live bankroll in USDC

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

    # Feature weights
    w_obi: float = 0.25
    w_taker: float = 0.25
    w_momentum: float = 0.15
    w_rsi: float = 0.15
    w_vwap: float = 0.10
    w_funding: float = 0.10

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
