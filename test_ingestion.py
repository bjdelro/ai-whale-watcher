#!/usr/bin/env python3
"""
Test script for the ingestion pipeline.
Verifies that the infrastructure is correctly set up.

Note: Trade data fetching from Polymarket requires either:
1. WebSocket connection for live data
2. Blockchain indexing for historical data
3. Premium API access

This test validates the infrastructure is ready for data ingestion.
"""

import asyncio
import logging
import os
import sys

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from src.core.polymarket_client import PolymarketClient
from src.database.db import Database
from src.ingestion.trade_fetcher import TradeFetcher
from src.ingestion.fill_aggregator import FillAggregator
from src.ingestion.orderbook_collector import OrderbookCollector
from src.ingestion.market_registry import MarketRegistryService

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def test_polymarket_client():
    """Test basic Polymarket API connectivity"""
    print("\n" + "=" * 60)
    print("Testing Polymarket Client")
    print("=" * 60)

    client = PolymarketClient()

    try:
        # Test markets endpoint (Gamma API - public)
        print("\n1. Fetching markets from Gamma API...")
        markets = await client.get_markets(closed=False, limit=5)
        print(f"   Found {len(markets)} markets")

        if markets:
            market = markets[0]
            print(f"   Example: {market.question[:60]}...")
            print(f"   Volume 24h: ${market.volume_24h:,.0f}")

        # Test CLOB markets endpoint (public)
        print("\n2. Fetching CLOB markets...")
        clob_markets = await client.get_clob_markets(limit=10)
        print(f"   Found {len(clob_markets)} CLOB markets")

        # Test orderbook endpoint (public)
        print("\n3. Testing orderbook endpoint...")
        # Find a market with tokens
        for m in clob_markets:
            if m.get("tokens"):
                token_id = m["tokens"][0]["token_id"]
                book = await client.get_orderbook(token_id)
                if book:
                    print(f"   Token: {token_id[:30]}...")
                    print(f"   Mid price: {book.mid_price:.4f}")
                    print(f"   Bids: {len(book.bids)} levels")
                    print(f"   Asks: {len(book.asks)} levels")
                    break

        # Test authentication
        print("\n4. Testing API authentication...")
        client._init_sync_client()
        if client._auth_initialized:
            print("   Authentication successful")
        else:
            print("   Authentication not available (check PRIVATE_KEY)")

        print("\n Polymarket client tests passed!")
        return True

    except Exception as e:
        print(f"\n Polymarket client error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await client.close()


async def test_database_models():
    """Test database model creation"""
    print("\n" + "=" * 60)
    print("Testing Database Models")
    print("=" * 60)

    db = Database("test_ingestion.db")

    try:
        await db.initialize()
        print("\n1. Database initialized successfully")

        # Test creating tables
        async with db.session() as session:
            # Import models to verify they exist
            from src.database.models import (
                RawFill, CanonicalTrade, Markout, WalletStats,
                OrderbookSnapshot, MarketRegistry, WalletPosition
            )
            print("2. All new models imported successfully")

            # Test basic query
            from sqlalchemy import select, func
            stmt = select(func.count()).select_from(RawFill)
            result = await session.execute(stmt)
            count = result.scalar()
            print(f"3. RawFill table accessible (count: {count})")

        print("\n Database model tests passed!")
        return True

    except Exception as e:
        print(f"\n Database error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await db.close()


async def test_market_registry():
    """Test market registry sync"""
    print("\n" + "=" * 60)
    print("Testing Market Registry")
    print("=" * 60)

    db = Database("test_ingestion.db")
    client = PolymarketClient()

    try:
        await db.initialize()

        registry = MarketRegistryService(client, db, sync_interval=300, cache_ttl=60)

        print("\n1. Syncing markets from Gamma API...")
        count = await registry.sync_markets()
        print(f"   Synced {count} markets")

        print("\n2. Testing market lookup...")
        markets = await registry.get_active_markets(limit=5)
        if markets:
            market = markets[0]
            print(f"   Found: {market.question[:50]}...")

            # Test cache
            cached = await registry.get_market(market.condition_id)
            print(f"   Cache hit: {cached is not None}")
        else:
            print("   No active markets found")

        print("\n3. Getting markets closing soon...")
        closing = await registry.get_closing_soon(hours=168, limit=5)  # 1 week
        print(f"   Found {len(closing)} markets closing in 1 week")

        stats = registry.get_stats()
        print(f"\n4. Registry stats:")
        print(f"   Markets synced: {stats['markets_synced']}")
        print(f"   Cache size: {stats['cache_size']}")

        print("\n Market registry tests passed!")
        return True

    except Exception as e:
        print(f"\n Market registry error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await client.close()
        await db.close()


async def test_orderbook_collector():
    """Test orderbook collection"""
    print("\n" + "=" * 60)
    print("Testing Orderbook Collector")
    print("=" * 60)

    db = Database("test_ingestion.db")
    client = PolymarketClient()

    try:
        await db.initialize()

        collector = OrderbookCollector(client, db, snapshot_interval=30, max_markets=10)

        print("\n1. Getting token IDs from CLOB API...")
        clob_markets = await client.get_clob_markets(limit=10)

        # Add tokens from CLOB markets
        tokens_added = 0
        for m in clob_markets:
            for token in m.get("tokens", []):
                token_id = token.get("token_id")
                if token_id:
                    collector.add_token(token_id)
                    tokens_added += 1
                    if tokens_added >= 5:
                        break
            if tokens_added >= 5:
                break

        stats = collector.get_stats()
        print(f"   Tracking {stats['tracked_tokens']} tokens")

        if stats['tracked_tokens'] > 0:
            print("\n2. Running snapshot cycle...")
            await collector._snapshot_cycle()
            stats = collector.get_stats()
            print(f"   Snapshots taken: {stats['snapshots_taken']}")

            print("\n3. Testing snapshot lookup...")
            from src.database.models import OrderbookSnapshot
            from sqlalchemy import select

            async with db.session() as session:
                stmt = select(OrderbookSnapshot).limit(1)
                result = await session.execute(stmt)
                snapshot = result.scalar_one_or_none()

                if snapshot:
                    print(f"   Token: {snapshot.token_id[:30]}...")
                    print(f"   Mid price: {snapshot.mid_price:.4f}")
                    print(f"   Bid depth (5%): ${snapshot.bid_depth_5pct or 0:,.0f}")
                    print(f"   Ask depth (5%): ${snapshot.ask_depth_5pct or 0:,.0f}")
                else:
                    print("   No snapshots stored (markets may be inactive)")

        print("\n Orderbook collector tests passed!")
        return True

    except Exception as e:
        print(f"\n Orderbook collector error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await client.close()
        await db.close()


async def test_infrastructure():
    """Test that all infrastructure components are in place"""
    print("\n" + "=" * 60)
    print("Testing Infrastructure Components")
    print("=" * 60)

    print("\n1. Checking module imports...")
    try:
        from src.ingestion import TradeFetcher, FillAggregator, OrderbookCollector, MarketRegistryService
        from src.core import PolymarketClient
        from src.database.models import (
            RawFill, CanonicalTrade, Markout, WalletStats,
            OrderbookSnapshot, MarketRegistry, WalletPosition,
            TradeIntent, WalletTier
        )
        print("   All modules import successfully")
    except ImportError as e:
        print(f"   Import error: {e}")
        return False

    print("\n2. Checking py-clob-client SDK...")
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import TradeParams
        print("   py-clob-client installed and importable")
    except ImportError:
        print("   py-clob-client not installed")

    print("\n3. Checking environment variables...")
    private_key = os.getenv("PRIVATE_KEY")
    if private_key:
        print("   PRIVATE_KEY is set")
    else:
        print("   PRIVATE_KEY not set (authenticated endpoints unavailable)")

    print("\n4. Checking database drivers...")
    try:
        import aiosqlite
        import greenlet
        print("   aiosqlite and greenlet installed")
    except ImportError as e:
        print(f"   Missing driver: {e}")
        return False

    print("\n Infrastructure tests passed!")
    return True


async def run_all_tests():
    """Run all ingestion tests"""
    print("\n" + "#" * 60)
    print("#" + " " * 20 + "INGESTION PIPELINE TESTS" + " " * 14 + "#")
    print("#" * 60)

    results = {}

    # Run tests in sequence
    results["infrastructure"] = await test_infrastructure()
    results["polymarket_client"] = await test_polymarket_client()
    results["database_models"] = await test_database_models()
    results["market_registry"] = await test_market_registry()
    results["orderbook_collector"] = await test_orderbook_collector()

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, result in results.items():
        status = "PASS" if result else "FAIL"
        print(f"  {name}: {status}")

    print(f"\nTotal: {passed}/{total} tests passed")

    # Cleanup
    if os.path.exists("test_ingestion.db"):
        os.remove("test_ingestion.db")
        print("\nCleaned up test database")

    print("\n" + "=" * 60)
    print("NOTES")
    print("=" * 60)
    print("""
The ingestion pipeline infrastructure is ready. Trade data ingestion
requires one of these approaches:

1. WebSocket: Connect to Polymarket's WebSocket for live trade events
2. Blockchain: Index trades directly from Polygon blockchain
3. Gamma API: Use /activity endpoint for specific wallet history

The current implementation supports all these approaches once trade
data is available. The pipeline will:
- Store raw fills in RawFill table
- Aggregate into CanonicalTrade records
- Track wallet positions
- Compute markouts for performance analysis
""")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
