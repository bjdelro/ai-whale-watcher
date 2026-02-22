"""
Market data fetching and caching for Polymarket.
Handles price lookups, market details, and resolution detection.
"""

from .client import MarketDataClient

__all__ = [
    "MarketDataClient",
]
