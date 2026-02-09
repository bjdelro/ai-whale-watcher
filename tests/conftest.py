"""
Shared test fixtures for the AI Whale Watcher test suite.
All tests run offline with in-memory SQLite and mocked external services.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

from src.database.models import (
    Base, RawFill, CanonicalTrade, Markout, WalletStats,
    OrderbookSnapshot, MarketRegistry, WalletPosition,
    TradeIntent, WalletTier,
)
from src.database.db import Database
from src.scoring.features import TradeFeatures


@pytest.fixture
async def db():
    """
    Create an in-memory async SQLite database for testing.
    Each test gets a completely fresh database.
    """
    database = Database.__new__(Database)
    database.db_path = ":memory:"
    database.db_url = "sqlite:///:memory:"
    database._initialized = False
    database._async_initialized = False
    database.engine = None
    database.async_engine = None
    database.SessionLocal = None
    database.AsyncSessionLocal = None

    database.async_engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with database.async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    database.AsyncSessionLocal = async_sessionmaker(
        database.async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    database._async_initialized = True

    yield database

    await database.async_engine.dispose()


@pytest.fixture
def make_raw_fill():
    """Factory for creating RawFill instances with unique IDs."""
    _counter = [0]

    def _factory(**overrides):
        _counter[0] += 1
        defaults = {
            "id": f"fill_{_counter[0]}",
            "market_order_id": f"order_{_counter[0]}",
            "maker": "0xmaker" + "0" * 34,
            "taker": "0xtaker" + "0" * 34,
            "side": "BUY",
            "outcome": "YES",
            "price": 0.65,
            "size": 100.0,
            "token_id": "token_abc123",
            "condition_id": "condition_abc123",
            "timestamp": datetime(2025, 1, 15, 14, 30, 0),
            "processed": False,
        }
        defaults.update(overrides)
        return RawFill(**defaults)

    return _factory


@pytest.fixture
def make_canonical_trade():
    """Factory for creating CanonicalTrade instances."""
    _counter = [0]

    def _factory(**overrides):
        _counter[0] += 1
        now = datetime(2025, 1, 15, 14, 30, 0)
        defaults = {
            "market_order_id": f"order_{_counter[0]}",
            "wallet": "0xtaker" + "0" * 34,
            "condition_id": "condition_abc123",
            "token_id": "token_abc123",
            "side": "BUY",
            "outcome": "YES",
            "avg_price": 0.65,
            "total_size": 100.0,
            "total_usd": 65.0,
            "num_fills": 1,
            "first_fill_time": now,
            "last_fill_time": now,
            "duration_ms": 0,
            "intent": TradeIntent.OPEN,
            "position_before": 0.0,
            "position_after": 100.0,
        }
        defaults.update(overrides)
        return CanonicalTrade(**defaults)

    return _factory


@pytest.fixture
def make_wallet_stats():
    """Factory for creating WalletStats instances."""
    def _factory(**overrides):
        defaults = {
            "wallet": "0xtaker" + "0" * 34,
            "total_trades": 60,
            "total_volume_usd": 50000.0,
            "avg_trade_size": 833.0,
            "win_rate_1h": 0.62,
            "avg_markout_1h": 55.0,
            "sharpe_1h": 1.2,
            "tier": WalletTier.ELITE,
        }
        defaults.update(overrides)
        return WalletStats(**defaults)

    return _factory


@pytest.fixture
def make_orderbook_snapshot():
    """Factory for creating OrderbookSnapshot instances."""
    def _factory(**overrides):
        defaults = {
            "token_id": "token_abc123",
            "timestamp": datetime(2025, 1, 15, 14, 29, 0),
            "mid_price": 0.65,
            "best_bid": 0.64,
            "best_ask": 0.66,
            "spread_bps": 30.0,
            "bid_depth_5pct": 5000.0,
            "ask_depth_5pct": 5000.0,
        }
        defaults.update(overrides)
        return OrderbookSnapshot(**defaults)

    return _factory


@pytest.fixture
def make_market_registry():
    """Factory for creating MarketRegistry instances."""
    def _factory(**overrides):
        defaults = {
            "condition_id": "condition_abc123",
            "question": "Will X happen?",
            "end_date": datetime.utcnow() + timedelta(days=7),
            "volume_24h": 100000.0,
            "liquidity": 50000.0,
            "is_active": True,
        }
        defaults.update(overrides)
        return MarketRegistry(**defaults)

    return _factory


@pytest.fixture
def make_trade_features():
    """Factory for creating TradeFeatures dataclass instances directly."""
    def _factory(**overrides):
        defaults = {
            "usd_size": 65.0,
            "pct_book_consumed": 1.3,
            "size_vs_wallet_avg": 1.0,
            "price_impact_bps": 5.0,
            "spread_at_entry": 30.0,
            "wallet_tier": WalletTier.ELITE,
            "wallet_win_rate_1h": 0.62,
            "wallet_sharpe_1h": 1.2,
            "wallet_avg_markout_1h": 55.0,
            "wallet_total_trades": 60,
            "trade_intent": "open",
            "position_change_pct": 100.0,
            "hours_to_close": 168.0,
            "market_volume_24h": 100000.0,
            "current_price": 0.65,
            "price_vs_50": 0.15,
            "is_after_hours": False,
            "minutes_since_market_open": None,
        }
        defaults.update(overrides)
        return TradeFeatures(**defaults)

    return _factory


@pytest.fixture
def mock_polymarket_client():
    """Mock PolymarketClient that returns no data by default."""
    client = AsyncMock()
    client.get_price = AsyncMock(return_value=0.70)
    client.get_orderbook = AsyncMock(return_value=None)
    client.get_markets = AsyncMock(return_value=[])
    client.close = AsyncMock()
    return client
