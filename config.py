from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Strategy
    min_edge_threshold: float = 0.05
    bet_size_usdc: float = 50.0
    virtual_bankroll: float = 10000.0
    use_kelly: bool = False

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
