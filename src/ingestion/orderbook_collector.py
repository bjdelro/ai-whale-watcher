"""
Orderbook collector for periodic depth snapshots.
Used to compute % of book consumed by trades.
"""

import asyncio
import logging
import json
from datetime import datetime
from typing import Dict, List, Optional, Set

from sqlalchemy import select

from ..core.polymarket_client import PolymarketClient, OrderbookData
from ..database.models import OrderbookSnapshot, MarketRegistry
from ..database.db import Database

logger = logging.getLogger(__name__)


class OrderbookCollector:
    """
    Periodically snapshot orderbooks for depth analysis.

    Key responsibilities:
    - Track active markets to monitor
    - Snapshot orderbooks at configurable intervals
    - Compute depth at various price levels
    - Store for later use in trade analysis
    """

    def __init__(
        self,
        client: PolymarketClient,
        db: Database,
        snapshot_interval: float = 30.0,
        max_markets: int = 50,
    ):
        """
        Initialize the orderbook collector.

        Args:
            client: Polymarket API client
            db: Database instance
            snapshot_interval: Seconds between snapshot cycles
            max_markets: Maximum number of markets to track
        """
        self.client = client
        self.db = db
        self.snapshot_interval = snapshot_interval
        self.max_markets = max_markets

        # Markets to track
        self._tracked_tokens: Set[str] = set()

        # Stats
        self._snapshots_taken = 0
        self._last_snapshot_time: Optional[datetime] = None

        # Running state
        self._running = False

    async def start(self):
        """Start the periodic snapshot loop"""
        self._running = True
        logger.info("Orderbook collector started")

        while self._running:
            try:
                await self._snapshot_cycle()
            except Exception as e:
                logger.error(f"Snapshot cycle error: {e}")

            await asyncio.sleep(self.snapshot_interval)

    def stop(self):
        """Stop the snapshot loop"""
        self._running = False
        logger.info("Orderbook collector stopped")

    async def refresh_tracked_markets(self):
        """Refresh the list of markets to track"""
        # Get most active markets from registry
        async with self.db.session() as session:
            stmt = (
                select(MarketRegistry)
                .where(MarketRegistry.is_active == True)
                .order_by(MarketRegistry.volume_24h.desc())
                .limit(self.max_markets)
            )
            result = await session.execute(stmt)
            markets = result.scalars().all()

            self._tracked_tokens.clear()
            for market in markets:
                if market.token_id_yes:
                    self._tracked_tokens.add(market.token_id_yes)
                if market.token_id_no:
                    self._tracked_tokens.add(market.token_id_no)

        logger.info(f"Tracking {len(self._tracked_tokens)} tokens")

    async def _snapshot_cycle(self):
        """Execute a single snapshot cycle"""
        start_time = datetime.utcnow()

        # Refresh tracked markets periodically
        if not self._tracked_tokens:
            await self.refresh_tracked_markets()

            # If still empty, get token IDs from API directly
            if not self._tracked_tokens:
                token_ids = await self.client.get_active_token_ids(limit=self.max_markets)
                self._tracked_tokens = set(token_ids)

        if not self._tracked_tokens:
            logger.warning("No tokens to track")
            return

        # Snapshot orderbooks in parallel batches
        snapshots: List[OrderbookSnapshot] = []
        batch_size = 10

        token_list = list(self._tracked_tokens)
        for i in range(0, len(token_list), batch_size):
            batch = token_list[i : i + batch_size]
            tasks = [self._fetch_and_process_book(token_id) for token_id in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, OrderbookSnapshot):
                    snapshots.append(result)
                elif isinstance(result, Exception):
                    logger.warning(f"Orderbook fetch error: {result}")

        # Store snapshots
        if snapshots:
            await self._store_snapshots(snapshots)

        self._last_snapshot_time = datetime.utcnow()
        elapsed = (self._last_snapshot_time - start_time).total_seconds()

        logger.debug(
            f"Snapshot cycle: {len(snapshots)} books captured in {elapsed:.2f}s"
        )

    async def _fetch_and_process_book(
        self, token_id: str
    ) -> Optional[OrderbookSnapshot]:
        """Fetch and process a single orderbook"""
        book = await self.client.get_orderbook(token_id)

        if not book:
            return None

        return self._process_orderbook(book)

    def _process_orderbook(self, book: OrderbookData) -> OrderbookSnapshot:
        """Process orderbook data into a snapshot"""
        # Compute depth at various levels
        bid_depth_1pct = self._compute_depth(book.bids, book.mid_price, 0.01)
        ask_depth_1pct = self._compute_depth(book.asks, book.mid_price, 0.01)
        bid_depth_5pct = self._compute_depth(book.bids, book.mid_price, 0.05)
        ask_depth_5pct = self._compute_depth(book.asks, book.mid_price, 0.05)

        # Total depth
        total_bid_depth = sum(level["size"] * level["price"] for level in book.bids)
        total_ask_depth = sum(level["size"] * level["price"] for level in book.asks)

        # Compress raw book for storage
        raw_book = json.dumps({
            "bids": book.bids[:20],  # Top 20 levels
            "asks": book.asks[:20],
        })

        return OrderbookSnapshot(
            token_id=book.token_id,
            timestamp=book.timestamp,
            mid_price=book.mid_price,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            spread_bps=book.spread_bps,
            bid_depth_1pct=bid_depth_1pct,
            ask_depth_1pct=ask_depth_1pct,
            bid_depth_5pct=bid_depth_5pct,
            ask_depth_5pct=ask_depth_5pct,
            total_bid_depth=total_bid_depth,
            total_ask_depth=total_ask_depth,
            raw_book=raw_book,
        )

    def _compute_depth(
        self,
        levels: List[Dict[str, float]],
        mid_price: float,
        pct_from_mid: float,
    ) -> float:
        """
        Compute total depth within a percentage of mid price.

        Args:
            levels: List of price/size dicts
            mid_price: Current mid price
            pct_from_mid: Percentage from mid (0.01 = 1%)

        Returns:
            Total USD depth within range
        """
        if not levels or mid_price <= 0:
            return 0.0

        depth = 0.0
        for level in levels:
            price = level["price"]
            size = level["size"]

            # Check if within range
            price_diff = abs(price - mid_price) / mid_price
            if price_diff <= pct_from_mid:
                depth += size * price  # USD value

        return depth

    async def _store_snapshots(self, snapshots: List[OrderbookSnapshot]):
        """Store orderbook snapshots to database"""
        async with self.db.session() as session:
            for snapshot in snapshots:
                session.add(snapshot)
                self._snapshots_taken += 1

            await session.commit()

    async def get_latest_snapshot(
        self, token_id: str
    ) -> Optional[OrderbookSnapshot]:
        """
        Get the most recent snapshot for a token.

        Args:
            token_id: Token ID to lookup

        Returns:
            Latest OrderbookSnapshot or None
        """
        async with self.db.session() as session:
            stmt = (
                select(OrderbookSnapshot)
                .where(OrderbookSnapshot.token_id == token_id)
                .order_by(OrderbookSnapshot.timestamp.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_snapshot_at_time(
        self,
        token_id: str,
        at_time: datetime,
    ) -> Optional[OrderbookSnapshot]:
        """
        Get the closest snapshot before a given time.

        Args:
            token_id: Token ID to lookup
            at_time: Time to find snapshot before

        Returns:
            Closest OrderbookSnapshot or None
        """
        async with self.db.session() as session:
            stmt = (
                select(OrderbookSnapshot)
                .where(
                    OrderbookSnapshot.token_id == token_id,
                    OrderbookSnapshot.timestamp <= at_time,
                )
                .order_by(OrderbookSnapshot.timestamp.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    def add_token(self, token_id: str):
        """Add a token to track"""
        self._tracked_tokens.add(token_id)

    def remove_token(self, token_id: str):
        """Remove a token from tracking"""
        self._tracked_tokens.discard(token_id)

    def get_stats(self) -> dict:
        """Get collector statistics"""
        return {
            "running": self._running,
            "tracked_tokens": len(self._tracked_tokens),
            "snapshots_taken": self._snapshots_taken,
            "last_snapshot": (
                self._last_snapshot_time.isoformat()
                if self._last_snapshot_time
                else None
            ),
        }
