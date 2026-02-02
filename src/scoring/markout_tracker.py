"""
Markout tracker for computing forward returns on trades.
Tracks price movement at various horizons after trade execution.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, update, and_

from ..database.models import CanonicalTrade, Markout
from ..database.db import Database
from ..core.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


class MarkoutTracker:
    """
    Compute and track forward returns for trades.

    Markouts are computed at standard horizons:
    - 5m: Very short-term (noise vs signal)
    - 1h: Short-term (main performance metric)
    - 6h: Medium-term
    - 1d: Longer-term (subject to more noise)
    """

    HORIZONS = {
        "5m": timedelta(minutes=5),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "1d": timedelta(days=1),
    }

    def __init__(
        self,
        db: Database,
        client: PolymarketClient,
        check_interval: float = 60.0,
    ):
        """
        Initialize the markout tracker.

        Args:
            db: Database instance
            client: Polymarket client for price lookups
            check_interval: Seconds between checking for pending markouts
        """
        self.db = db
        self.client = client
        self.check_interval = check_interval

        # Price cache to reduce API calls
        self._price_cache: Dict[str, Tuple[float, datetime]] = {}
        self._cache_ttl = 30.0  # seconds

        # Stats
        self._markouts_computed = 0
        self._running = False

    async def start(self):
        """Start the markout computation loop"""
        self._running = True
        logger.info("Markout tracker started")

        while self._running:
            try:
                await self._process_pending_markouts()
            except Exception as e:
                logger.error(f"Markout processing error: {e}")

            await asyncio.sleep(self.check_interval)

    def stop(self):
        """Stop the markout computation loop"""
        self._running = False
        logger.info("Markout tracker stopped")

    async def schedule_markouts(self, trade: CanonicalTrade):
        """
        Schedule markouts for a new trade.

        Args:
            trade: The trade to track
        """
        async with self.db.session() as session:
            for horizon, delta in self.HORIZONS.items():
                # Check if markout already exists
                stmt = select(Markout).where(
                    and_(
                        Markout.canonical_trade_id == trade.id,
                        Markout.horizon == horizon,
                    )
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    continue

                # Create new markout
                scheduled_for = trade.first_fill_time + delta

                markout = Markout(
                    canonical_trade_id=trade.id,
                    horizon=horizon,
                    entry_price=trade.avg_price,
                    exit_price=None,
                    return_bps=None,
                    is_final=False,
                    scheduled_for=scheduled_for,
                )
                session.add(markout)

            await session.commit()

    async def _process_pending_markouts(self) -> int:
        """
        Process all markouts that are due.

        Returns:
            Number of markouts computed
        """
        now = datetime.utcnow()
        computed = 0

        async with self.db.session() as session:
            # Get pending markouts that are due
            stmt = (
                select(Markout)
                .where(
                    and_(
                        Markout.is_final == False,
                        Markout.scheduled_for <= now,
                    )
                )
                .limit(100)
            )
            result = await session.execute(stmt)
            markouts = result.scalars().all()

            if not markouts:
                return 0

            # Get associated trades
            trade_ids = [m.canonical_trade_id for m in markouts]
            stmt = select(CanonicalTrade).where(CanonicalTrade.id.in_(trade_ids))
            result = await session.execute(stmt)
            trades = {t.id: t for t in result.scalars().all()}

            # Process each markout
            for markout in markouts:
                trade = trades.get(markout.canonical_trade_id)
                if not trade:
                    continue

                try:
                    exit_price = await self._get_price(trade.token_id)
                    if exit_price is None:
                        continue

                    # Compute return in basis points
                    return_bps = self._compute_return_bps(
                        entry_price=markout.entry_price,
                        exit_price=exit_price,
                        side=trade.side,
                    )

                    # Update markout
                    markout.exit_price = exit_price
                    markout.return_bps = return_bps
                    markout.is_final = True
                    markout.computed_at = now

                    computed += 1
                    self._markouts_computed += 1

                except Exception as e:
                    logger.warning(f"Failed to compute markout {markout.id}: {e}")

            await session.commit()

        if computed > 0:
            logger.info(f"Computed {computed} markouts")

        return computed

    def _compute_return_bps(
        self,
        entry_price: float,
        exit_price: float,
        side: str,
    ) -> float:
        """
        Compute return in basis points, adjusted for trade direction.

        For BUY trades: positive if price went up
        For SELL trades: positive if price went down
        """
        if entry_price <= 0:
            return 0.0

        raw_return = (exit_price - entry_price) / entry_price * 10000

        # Flip sign for SELL trades (profit when price goes down)
        if side.upper() == "SELL":
            raw_return = -raw_return

        return round(raw_return, 2)

    async def _get_price(self, token_id: str) -> Optional[float]:
        """
        Get current price for a token, with caching.
        """
        # Check cache
        if token_id in self._price_cache:
            price, cached_at = self._price_cache[token_id]
            if (datetime.utcnow() - cached_at).total_seconds() < self._cache_ttl:
                return price

        # Fetch from API
        price = await self.client.get_price(token_id)

        if price is not None:
            self._price_cache[token_id] = (price, datetime.utcnow())

        return price

    async def get_trade_markouts(
        self, trade_id: int
    ) -> Dict[str, Optional[float]]:
        """
        Get all markouts for a trade.

        Returns:
            Dict mapping horizon to return_bps (or None if not yet computed)
        """
        async with self.db.session() as session:
            stmt = select(Markout).where(Markout.canonical_trade_id == trade_id)
            result = await session.execute(stmt)
            markouts = result.scalars().all()

            return {m.horizon: m.return_bps for m in markouts}

    async def get_wallet_markouts(
        self,
        wallet: str,
        horizon: str = "1h",
        limit: int = 100,
    ) -> List[float]:
        """
        Get markout returns for a wallet at a specific horizon.

        Returns:
            List of return values in basis points
        """
        async with self.db.session() as session:
            stmt = (
                select(Markout)
                .join(CanonicalTrade, Markout.canonical_trade_id == CanonicalTrade.id)
                .where(
                    and_(
                        CanonicalTrade.wallet == wallet,
                        Markout.horizon == horizon,
                        Markout.is_final == True,
                    )
                )
                .order_by(CanonicalTrade.first_fill_time.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            markouts = result.scalars().all()

            return [m.return_bps for m in markouts if m.return_bps is not None]

    def get_stats(self) -> dict:
        """Get tracker statistics"""
        return {
            "running": self._running,
            "markouts_computed": self._markouts_computed,
            "cache_size": len(self._price_cache),
        }
