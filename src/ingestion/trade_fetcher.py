"""
Trade fetcher for continuous polling of Polymarket trades.
Stores raw fills in the database for aggregation.
"""

import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import List, Optional, Set

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..core.polymarket_client import PolymarketClient, TradeData
from ..database.models import RawFill
from ..database.db import Database

logger = logging.getLogger(__name__)


class TradeFetcher:
    """
    Continuously fetch trades from Polymarket and store as RawFills.

    Supports:
    - Polling recent trades across active markets
    - Backfilling historical trades for specific wallets
    - Deduplication of already-seen trades
    """

    def __init__(
        self,
        client: PolymarketClient,
        db: Database,
        poll_interval: float = 5.0,
        batch_size: int = 100,
    ):
        """
        Initialize the trade fetcher.

        Args:
            client: Polymarket API client
            db: Database instance
            poll_interval: Seconds between poll cycles
            batch_size: Max trades per API request
        """
        self.client = client
        self.db = db
        self.poll_interval = poll_interval
        self.batch_size = batch_size

        # Track seen trade IDs to avoid duplicates within session
        self._seen_ids: Set[str] = set()
        self._max_seen_cache = 50000

        # Tracking
        self._last_poll_time: Optional[datetime] = None
        self._total_fetched = 0
        self._total_stored = 0

        # Running state
        self._running = False

    async def start(self):
        """Start the continuous polling loop"""
        self._running = True
        logger.info("Trade fetcher started")

        while self._running:
            try:
                await self._poll_cycle()
            except Exception as e:
                logger.error(f"Poll cycle error: {e}")

            await asyncio.sleep(self.poll_interval)

    def stop(self):
        """Stop the polling loop"""
        self._running = False
        logger.info("Trade fetcher stopped")

    async def _poll_cycle(self):
        """Execute a single poll cycle"""
        start_time = datetime.utcnow()

        # Get active markets to poll
        token_ids = await self.client.get_active_token_ids(limit=50)

        if not token_ids:
            logger.warning("No active markets found")
            return

        # Poll trades for each token (could batch this)
        all_trades: List[TradeData] = []

        # Fetch trades in parallel batches
        batch_size = 10  # Concurrent requests
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i : i + batch_size]
            tasks = [
                self.client.get_trades(token_id=tid, limit=self.batch_size)
                for tid in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    all_trades.extend(result)
                elif isinstance(result, Exception):
                    logger.warning(f"Trade fetch error: {result}")

        # Store new trades
        new_count = await self._store_trades(all_trades)

        self._last_poll_time = datetime.utcnow()
        elapsed = (self._last_poll_time - start_time).total_seconds()

        if new_count > 0:
            logger.info(
                f"Poll cycle: {len(all_trades)} trades, {new_count} new, {elapsed:.2f}s"
            )

    async def _store_trades(self, trades: List[TradeData]) -> int:
        """
        Store trades as RawFills, deduplicating.

        Args:
            trades: List of trade data

        Returns:
            Number of new trades stored
        """
        if not trades:
            return 0

        new_trades = []

        for trade in trades:
            # Skip if already seen in session
            if trade.id in self._seen_ids:
                continue

            # Add to session cache
            self._seen_ids.add(trade.id)

            # Limit cache size
            if len(self._seen_ids) > self._max_seen_cache:
                # Remove oldest ~20%
                to_remove = list(self._seen_ids)[: self._max_seen_cache // 5]
                for id_ in to_remove:
                    self._seen_ids.discard(id_)

            new_trades.append(trade)

        if not new_trades:
            return 0

        # Batch insert with conflict handling
        async with self.db.session() as session:
            for trade in new_trades:
                fill = RawFill(
                    id=trade.id,
                    market_order_id=trade.market_order_id,
                    bucket_index=trade.bucket_index,
                    maker=trade.maker,
                    taker=trade.taker,
                    side=trade.side,
                    outcome=trade.outcome,
                    price=trade.price,
                    size=trade.size,
                    token_id=trade.token_id,
                    condition_id=trade.condition_id,
                    timestamp=trade.timestamp,
                    tx_hash=trade.tx_hash,
                    block_number=trade.block_number,
                    raw_json=json.dumps(trade.raw_json),
                    processed=False,
                )

                # Use merge to handle duplicates gracefully
                await session.merge(fill)

            await session.commit()

        self._total_fetched += len(trades)
        self._total_stored += len(new_trades)

        return len(new_trades)

    async def fetch_wallet_history(
        self,
        wallet: str,
        max_trades: int = 1000,
    ) -> int:
        """
        Backfill historical trades for a specific wallet.

        Args:
            wallet: Wallet address to fetch history for
            max_trades: Maximum trades to fetch

        Returns:
            Number of new trades stored
        """
        logger.info(f"Backfilling history for wallet {wallet[:10]}...")

        all_trades: List[TradeData] = []
        offset = 0
        batch_size = 100

        while len(all_trades) < max_trades:
            trades = await self.client.get_activity(
                wallet=wallet,
                limit=batch_size,
                offset=offset,
            )

            if not trades:
                break

            all_trades.extend(trades)
            offset += batch_size

            # Rate limiting
            await asyncio.sleep(0.2)

        new_count = await self._store_trades(all_trades)
        logger.info(
            f"Backfilled {len(all_trades)} trades for {wallet[:10]}..., {new_count} new"
        )

        return new_count

    async def fetch_recent_trades(
        self,
        since: Optional[datetime] = None,
        token_ids: Optional[List[str]] = None,
    ) -> List[TradeData]:
        """
        Fetch recent trades since a timestamp.

        Args:
            since: Only return trades after this time
            token_ids: Optional list of token IDs to filter

        Returns:
            List of trade data
        """
        if token_ids is None:
            token_ids = await self.client.get_active_token_ids(limit=50)

        all_trades: List[TradeData] = []

        for token_id in token_ids:
            trades = await self.client.get_trades(
                token_id=token_id,
                limit=self.batch_size,
            )

            if since:
                trades = [t for t in trades if t.timestamp > since]

            all_trades.extend(trades)

            # Rate limiting
            await asyncio.sleep(0.1)

        # Sort by timestamp
        all_trades.sort(key=lambda t: t.timestamp, reverse=True)

        return all_trades

    def get_stats(self) -> dict:
        """Get fetcher statistics"""
        return {
            "running": self._running,
            "last_poll": self._last_poll_time.isoformat() if self._last_poll_time else None,
            "total_fetched": self._total_fetched,
            "total_stored": self._total_stored,
            "cache_size": len(self._seen_ids),
        }
