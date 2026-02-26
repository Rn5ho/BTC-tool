from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Strategy
    min_edge_threshold: float = 0.05
    max_edge_threshold: float = 0.18
    bet_size_usdc: float = 5.0
    virtual_bankroll: float = 100.0
    use_kelly: bool = False

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
