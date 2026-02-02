"""
Market registry service for keeping market metadata fresh.
Syncs with Gamma API and provides fast local lookups.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select, update

from ..core.polymarket_client import PolymarketClient, MarketData
from ..database.models import MarketRegistry
from ..database.db import Database

logger = logging.getLogger(__name__)


class MarketRegistryService:
    """
    Service for managing market metadata.

    Key responsibilities:
    - Sync markets from Gamma API periodically
    - Provide fast local lookups by condition_id
    - Track market resolution status
    - Cache token ID mappings
    """

    def __init__(
        self,
        client: PolymarketClient,
        db: Database,
        sync_interval: float = 300.0,  # 5 minutes
        cache_ttl: float = 60.0,  # 1 minute
    ):
        """
        Initialize the market registry service.

        Args:
            client: Polymarket API client
            db: Database instance
            sync_interval: Seconds between full syncs
            cache_ttl: Seconds before cache entries expire
        """
        self.client = client
        self.db = db
        self.sync_interval = sync_interval
        self.cache_ttl = cache_ttl

        # In-memory cache for fast lookups
        self._cache: Dict[str, MarketRegistry] = {}
        self._cache_timestamps: Dict[str, datetime] = {}

        # Token ID to condition ID mapping
        self._token_to_condition: Dict[str, str] = {}

        # Stats
        self._markets_synced = 0
        self._last_sync_time: Optional[datetime] = None

        # Running state
        self._running = False

    async def start(self):
        """Start the periodic sync loop"""
        self._running = True
        logger.info("Market registry service started")

        # Initial sync
        await self.sync_markets()

        while self._running:
            await asyncio.sleep(self.sync_interval)
            try:
                await self.sync_markets()
            except Exception as e:
                logger.error(f"Market sync error: {e}")

    def stop(self):
        """Stop the sync loop"""
        self._running = False
        logger.info("Market registry service stopped")

    async def sync_markets(self, include_closed: bool = False) -> int:
        """
        Sync markets from Gamma API to local database.

        Args:
            include_closed: Whether to include closed markets

        Returns:
            Number of markets synced
        """
        logger.info("Syncing markets from Gamma API...")

        # Fetch all active markets
        markets: List[MarketData] = []
        offset = 0
        batch_size = 100

        while True:
            batch = await self.client.get_markets(
                closed=include_closed,
                limit=batch_size,
                offset=offset,
            )

            if not batch:
                break

            markets.extend(batch)
            offset += batch_size

            # Safety limit
            if offset > 5000:
                break

            # Rate limiting
            await asyncio.sleep(0.2)

        # Store to database
        synced = await self._store_markets(markets)

        self._last_sync_time = datetime.utcnow()
        self._markets_synced = synced

        logger.info(f"Synced {synced} markets")
        return synced

    async def _store_markets(self, markets: List[MarketData]) -> int:
        """Store markets to database and update cache"""
        stored = 0

        async with self.db.session() as session:
            for market in markets:
                # Check if exists
                stmt = select(MarketRegistry).where(
                    MarketRegistry.condition_id == market.condition_id
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    # Update existing
                    existing.question = market.question
                    existing.description = market.description
                    existing.category = market.category
                    existing.slug = market.slug
                    existing.token_id_yes = market.token_id_yes
                    existing.token_id_no = market.token_id_no
                    existing.end_date = market.end_date
                    existing.total_volume = market.total_volume
                    existing.volume_24h = market.volume_24h
                    existing.liquidity = market.liquidity
                    existing.is_active = market.is_active
                    existing.resolution = market.resolution
                    existing.updated_at = datetime.utcnow()

                    # Update cache
                    self._cache[market.condition_id] = existing
                else:
                    # Create new
                    registry = MarketRegistry(
                        condition_id=market.condition_id,
                        question=market.question,
                        description=market.description,
                        category=market.category,
                        slug=market.slug,
                        token_id_yes=market.token_id_yes,
                        token_id_no=market.token_id_no,
                        end_date=market.end_date,
                        created_date=market.created_date,
                        total_volume=market.total_volume,
                        volume_24h=market.volume_24h,
                        liquidity=market.liquidity,
                        is_active=market.is_active,
                        resolution=market.resolution,
                    )
                    session.add(registry)

                    # Update cache
                    self._cache[market.condition_id] = registry

                # Update token mapping
                if market.token_id_yes:
                    self._token_to_condition[market.token_id_yes] = market.condition_id
                if market.token_id_no:
                    self._token_to_condition[market.token_id_no] = market.condition_id

                self._cache_timestamps[market.condition_id] = datetime.utcnow()
                stored += 1

            await session.commit()

        return stored

    async def get_market(self, condition_id: str) -> Optional[MarketRegistry]:
        """
        Get market by condition ID with caching.

        Args:
            condition_id: Market condition ID

        Returns:
            MarketRegistry or None
        """
        # Check cache first
        if condition_id in self._cache:
            cache_time = self._cache_timestamps.get(condition_id)
            if cache_time and (datetime.utcnow() - cache_time).total_seconds() < self.cache_ttl:
                return self._cache[condition_id]

        # Fetch from database
        async with self.db.session() as session:
            stmt = select(MarketRegistry).where(
                MarketRegistry.condition_id == condition_id
            )
            result = await session.execute(stmt)
            market = result.scalar_one_or_none()

            if market:
                self._cache[condition_id] = market
                self._cache_timestamps[condition_id] = datetime.utcnow()

            return market

    async def get_market_by_token(self, token_id: str) -> Optional[MarketRegistry]:
        """
        Get market by token ID.

        Args:
            token_id: Token ID (YES or NO)

        Returns:
            MarketRegistry or None
        """
        # Check token mapping first
        condition_id = self._token_to_condition.get(token_id)
        if condition_id:
            return await self.get_market(condition_id)

        # Search database
        async with self.db.session() as session:
            stmt = select(MarketRegistry).where(
                (MarketRegistry.token_id_yes == token_id) |
                (MarketRegistry.token_id_no == token_id)
            )
            result = await session.execute(stmt)
            market = result.scalar_one_or_none()

            if market:
                # Update mappings
                if market.token_id_yes:
                    self._token_to_condition[market.token_id_yes] = market.condition_id
                if market.token_id_no:
                    self._token_to_condition[market.token_id_no] = market.condition_id

                self._cache[market.condition_id] = market
                self._cache_timestamps[market.condition_id] = datetime.utcnow()

            return market

    async def get_active_markets(
        self,
        limit: int = 100,
        category: Optional[str] = None,
        min_volume: Optional[float] = None,
    ) -> List[MarketRegistry]:
        """
        Get active markets with optional filters.

        Args:
            limit: Maximum markets to return
            category: Filter by category
            min_volume: Minimum 24h volume

        Returns:
            List of MarketRegistry objects
        """
        async with self.db.session() as session:
            stmt = select(MarketRegistry).where(MarketRegistry.is_active == True)

            if category:
                stmt = stmt.where(MarketRegistry.category == category)

            if min_volume:
                stmt = stmt.where(MarketRegistry.volume_24h >= min_volume)

            stmt = stmt.order_by(MarketRegistry.volume_24h.desc()).limit(limit)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_closing_soon(
        self,
        hours: float = 24.0,
        limit: int = 50,
    ) -> List[MarketRegistry]:
        """
        Get markets closing within specified hours.

        Args:
            hours: Time window in hours
            limit: Maximum markets to return

        Returns:
            List of MarketRegistry objects
        """
        cutoff = datetime.utcnow() + timedelta(hours=hours)

        async with self.db.session() as session:
            stmt = (
                select(MarketRegistry)
                .where(
                    MarketRegistry.is_active == True,
                    MarketRegistry.end_date != None,
                    MarketRegistry.end_date <= cutoff,
                    MarketRegistry.end_date > datetime.utcnow(),
                )
                .order_by(MarketRegistry.end_date)
                .limit(limit)
            )

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def mark_resolved(
        self,
        condition_id: str,
        resolution: str,
    ):
        """
        Mark a market as resolved.

        Args:
            condition_id: Market condition ID
            resolution: Resolution outcome (YES/NO)
        """
        async with self.db.session() as session:
            stmt = (
                update(MarketRegistry)
                .where(MarketRegistry.condition_id == condition_id)
                .values(
                    resolution=resolution,
                    resolved_at=datetime.utcnow(),
                    is_active=False,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.execute(stmt)
            await session.commit()

        # Update cache
        if condition_id in self._cache:
            self._cache[condition_id].resolution = resolution
            self._cache[condition_id].is_active = False

    def clear_cache(self):
        """Clear the in-memory cache"""
        self._cache.clear()
        self._cache_timestamps.clear()
        self._token_to_condition.clear()

    def get_stats(self) -> dict:
        """Get service statistics"""
        return {
            "running": self._running,
            "markets_synced": self._markets_synced,
            "cache_size": len(self._cache),
            "token_mappings": len(self._token_to_condition),
            "last_sync": (
                self._last_sync_time.isoformat() if self._last_sync_time else None
            ),
        }
