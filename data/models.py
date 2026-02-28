from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Candle:
    timestamp: int  # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float
    trades: int
    closed: bool = False


@dataclass
class OrderBookLevel:
    price: float
    quantity: float


@dataclass
class OrderBook:
    timestamp: int
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)


@dataclass
class AggTrade:
    timestamp: int
    price: float
    quantity: float
    is_buyer_maker: bool  # True = seller-initiated (taker sell)


@dataclass
class FundingInfo:
    timestamp: int
    funding_rate: float
    mark_price: float
    next_funding_time: int


@dataclass
class PolymarketOrderBook:
    """Parsed top-of-book from the CLOB /book endpoint."""
    best_bid: float
    best_ask: float
    spread: float
    bid_size: float
    ask_size: float
    midpoint: float


@dataclass
class PolymarketMarket:
    slug: str
    question: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    up_price: float
    down_price: float
    window_start: int  # unix timestamp of 5-min window start
    window_end: int
    # Spread tracking (optional — populated when order book is available)
    up_best_bid: Optional[float] = None
    up_best_ask: Optional[float] = None
    up_spread: Optional[float] = None
    down_best_bid: Optional[float] = None
    down_best_ask: Optional[float] = None
    down_spread: Optional[float] = None


@dataclass
class FeatureVector:
    timestamp: int
    obi: float = 0.0  # order book imbalance
    taker_ratio: float = 0.0  # net taker buy ratio
    momentum_1m: float = 0.0
    momentum_5m: float = 0.0
    rsi: float = 50.0
    vwap_deviation: float = 0.0
    bb_position: float = 0.0  # -1 to 1, position within Bollinger Bands
    ema_cross: float = 0.0  # 1 if fast > slow, -1 otherwise
    funding_rate: float = 0.0
    volume_zscore: float = 0.0
    atr: float = 0.0


@dataclass
class PaperTrade:
    timestamp: int
    market_slug: str
    side: str  # "UP" or "DOWN"
    our_prob: float
    market_prob: float
    edge: float
    size_usdc: float
    entry_price: float
    outcome: Optional[str] = None  # "WIN" or "LOSS"
    pnl: Optional[float] = None
    settled_at: Optional[int] = None
    entry_spread: Optional[float] = None  # bid-ask spread at entry
    midpoint_price: Optional[float] = None  # midpoint at entry (for comparison)
