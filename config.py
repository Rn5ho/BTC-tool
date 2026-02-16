from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Strategy
    min_edge_threshold: float = 0.05
    min_edge_down: float = 0.08          # higher bar for DOWN trades (data shows worse win rate)
    max_edge: float = 0.20               # cap — edges above this are likely model error, not mispricing
    max_signal_value: float = 0.45       # skip when any single signal is saturated (near +-0.5 limits)
    bet_size_usdc: float = 50.0
    virtual_bankroll: float = 10000.0
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
