"""
Data ingestion pipeline for the whale watcher system.
Handles fetching trades, aggregating fills, and collecting orderbook data.
"""

from .trade_fetcher import TradeFetcher
from .fill_aggregator import FillAggregator
from .orderbook_collector import OrderbookCollector
from .market_registry import MarketRegistryService
from .websocket_feed import WebSocketFeed, TradeFeedManager

__all__ = [
    "TradeFetcher",
    "FillAggregator",
    "OrderbookCollector",
    "MarketRegistryService",
    "WebSocketFeed",
    "TradeFeedManager",
]
