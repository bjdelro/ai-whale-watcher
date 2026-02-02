"""
Base platform interface for prediction markets
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Awaitable, Any
from enum import Enum

from src.database.models import Platform, TradeType


@dataclass
class TradeEvent:
    """Standardized trade event from any platform"""
    platform: Platform
    wallet_address: str
    market_id: str
    market_name: str
    trade_type: TradeType
    amount_usd: float
    price: Optional[float] = None
    shares: Optional[float] = None
    outcome: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Platform-specific IDs
    external_id: Optional[str] = None
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None

    # Raw data for debugging
    raw_data: Optional[dict] = None

    def __repr__(self):
        return (
            f"<TradeEvent {self.trade_type.value} ${self.amount_usd:,.0f} "
            f"by {self.wallet_address[:10]}... on {self.market_name[:30]}>"
        )


@dataclass
class MarketInfo:
    """Standardized market information"""
    id: str
    platform: Platform
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    volume_24h: float = 0.0
    volume_7d: float = 0.0
    liquidity: float = 0.0
    close_time: Optional[datetime] = None
    is_active: bool = True
    metadata: Optional[dict] = None


# Type alias for trade callbacks
TradeCallback = Callable[[TradeEvent], Awaitable[None]]


class BasePlatform(ABC):
    """Abstract base class for prediction market platforms"""

    def __init__(self, config: dict):
        """
        Initialize platform client.

        Args:
            config: Platform-specific configuration
        """
        self.config = config
        self._trade_callbacks: list[TradeCallback] = []
        self._is_connected = False

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Return the platform enum value"""
        pass

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self._is_connected

    def on_trade(self, callback: TradeCallback):
        """
        Register a callback for trade events.

        Args:
            callback: Async function to call when trade is detected
        """
        self._trade_callbacks.append(callback)

    async def _emit_trade(self, trade: TradeEvent):
        """Emit trade event to all registered callbacks"""
        for callback in self._trade_callbacks:
            try:
                await callback(trade)
            except Exception as e:
                from loguru import logger
                logger.error(f"Error in trade callback: {e}")

    @abstractmethod
    async def connect(self):
        """Connect to the platform's WebSocket feed"""
        pass

    @abstractmethod
    async def disconnect(self):
        """Disconnect from WebSocket feed"""
        pass

    @abstractmethod
    async def start_listening(self):
        """Start listening for trade events"""
        pass

    @abstractmethod
    async def get_markets(self) -> list[MarketInfo]:
        """Fetch all active markets"""
        pass

    @abstractmethod
    async def get_market(self, market_id: str) -> Optional[MarketInfo]:
        """Fetch specific market info"""
        pass

    @abstractmethod
    async def get_recent_trades(
        self,
        market_id: Optional[str] = None,
        limit: int = 100
    ) -> list[TradeEvent]:
        """
        Fetch recent trades via REST API (polling fallback).

        Args:
            market_id: Optional market to filter by
            limit: Maximum trades to return
        """
        pass

    @abstractmethod
    async def poll_trades(self):
        """Poll for new trades (fallback when WebSocket fails)"""
        pass
