"""
Fill aggregator for grouping raw fills into canonical trades.
Groups by market_order_id to create a single trade record per market order.
"""

import asyncio
import logging
import json
from collections import defaultdict
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional, Tuple

from sqlalchemy import select, update, and_
from sqlalchemy.orm import selectinload

from ..database.models import (
    RawFill,
    CanonicalTrade,
    WalletPosition,
    TradeIntent,
    OrderbookSnapshot,
)
from ..database.db import Database
from ..core.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


class FillAggregator:
    """
    Aggregates RawFills into CanonicalTrades.

    Key responsibilities:
    - Group fills by market_order_id
    - Compute weighted average price
    - Determine trade intent (OPEN, CLOSE, ADD, REDUCE)
    - Update wallet positions
    - Compute price impact from orderbook snapshots
    """

    def __init__(
        self,
        db: Database,
        client: Optional[PolymarketClient] = None,
        processing_batch_size: int = 100,
    ):
        """
        Initialize the fill aggregator.

        Args:
            db: Database instance
            client: Optional Polymarket client for orderbook lookups
            processing_batch_size: Number of fills to process per batch
        """
        self.db = db
        self.client = client
        self.processing_batch_size = processing_batch_size

        # Stats
        self._trades_created = 0
        self._fills_processed = 0

    async def process_pending_fills(self) -> int:
        """
        Process all unprocessed fills into canonical trades.

        Returns:
            Number of new canonical trades created
        """
        trades_created = 0

        async with self.db.session() as session:
            # Get unprocessed fills grouped by market_order_id
            stmt = (
                select(RawFill)
                .where(RawFill.processed == False)
                .order_by(RawFill.timestamp)
                .limit(self.processing_batch_size * 10)
            )
            result = await session.execute(stmt)
            fills = result.scalars().all()

            if not fills:
                return 0

            # Group fills by market_order_id
            fills_by_order: Dict[str, List[RawFill]] = defaultdict(list)
            for fill in fills:
                key = fill.market_order_id or fill.id  # Fallback to fill ID if no order ID
                fills_by_order[key].append(fill)

            # Process each group
            for order_id, order_fills in fills_by_order.items():
                try:
                    trade = await self._aggregate_fills(session, order_id, order_fills)
                    if trade:
                        trades_created += 1
                        self._trades_created += 1

                    # Mark fills as processed
                    for fill in order_fills:
                        fill.processed = True
                        self._fills_processed += 1

                except Exception as e:
                    logger.error(f"Error aggregating order {order_id}: {e}")
                    continue

            await session.commit()

        logger.info(f"Aggregated {trades_created} canonical trades from {len(fills)} fills")
        return trades_created

    async def _aggregate_fills(
        self,
        session,
        market_order_id: str,
        fills: List[RawFill],
    ) -> Optional[CanonicalTrade]:
        """
        Aggregate a group of fills into a single canonical trade.

        Args:
            session: Database session
            market_order_id: The market order ID
            fills: List of fills for this order

        Returns:
            Created CanonicalTrade or None
        """
        if not fills:
            return None

        # Check if trade already exists
        stmt = select(CanonicalTrade).where(
            CanonicalTrade.market_order_id == market_order_id
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing trade
            return await self._update_trade(session, existing, fills)

        # Create new trade
        return await self._create_trade(session, market_order_id, fills)

    async def _create_trade(
        self,
        session,
        market_order_id: str,
        fills: List[RawFill],
    ) -> CanonicalTrade:
        """Create a new canonical trade from fills"""
        # Compute aggregates
        total_size = sum(f.size for f in fills)
        total_value = sum(f.price * f.size for f in fills)
        avg_price = total_value / total_size if total_size > 0 else 0

        # Use taker as wallet (they're taking liquidity)
        wallet = fills[0].taker
        side = fills[0].side
        outcome = fills[0].outcome
        token_id = fills[0].token_id
        condition_id = fills[0].condition_id

        # Timing
        timestamps = [f.timestamp for f in fills]
        first_fill_time = min(timestamps)
        last_fill_time = max(timestamps)
        duration_ms = int((last_fill_time - first_fill_time).total_seconds() * 1000)

        # Get position context
        position_before, position_after, intent = await self._compute_position_context(
            session, wallet, condition_id, outcome, side, total_size
        )

        # Get orderbook context
        pre_mid, post_mid, price_impact_bps, pct_book_consumed = (
            await self._compute_orderbook_context(session, token_id, first_fill_time, total_size, side)
        )

        # Calculate USD value (price * size for prediction markets)
        total_usd = total_size * avg_price

        trade = CanonicalTrade(
            market_order_id=market_order_id,
            wallet=wallet,
            condition_id=condition_id or "",
            token_id=token_id,
            side=side,
            outcome=outcome,
            avg_price=avg_price,
            total_size=total_size,
            total_usd=total_usd,
            num_fills=len(fills),
            first_fill_time=first_fill_time,
            last_fill_time=last_fill_time,
            duration_ms=duration_ms,
            pre_mid=pre_mid,
            post_mid=post_mid,
            price_impact_bps=price_impact_bps,
            pct_book_consumed=pct_book_consumed,
            intent=intent,
            position_before=position_before,
            position_after=position_after,
        )

        session.add(trade)

        # Update wallet position
        await self._update_position(
            session, wallet, condition_id or "", outcome or "", side, total_size, avg_price
        )

        return trade

    async def _update_trade(
        self,
        session,
        trade: CanonicalTrade,
        new_fills: List[RawFill],
    ) -> CanonicalTrade:
        """Update an existing trade with additional fills"""
        # Recompute aggregates including new fills
        new_size = sum(f.size for f in new_fills)
        new_value = sum(f.price * f.size for f in new_fills)

        old_value = trade.avg_price * trade.total_size
        total_size = trade.total_size + new_size
        avg_price = (old_value + new_value) / total_size if total_size > 0 else 0

        # Update timing
        timestamps = [f.timestamp for f in new_fills]
        new_last_time = max(timestamps)
        if new_last_time > trade.last_fill_time:
            trade.last_fill_time = new_last_time
            trade.duration_ms = int(
                (trade.last_fill_time - trade.first_fill_time).total_seconds() * 1000
            )

        trade.avg_price = avg_price
        trade.total_size = total_size
        trade.total_usd = total_size * avg_price
        trade.num_fills += len(new_fills)

        return trade

    async def _compute_position_context(
        self,
        session,
        wallet: str,
        condition_id: Optional[str],
        outcome: Optional[str],
        side: str,
        trade_size: float,
    ) -> Tuple[Optional[float], Optional[float], TradeIntent]:
        """
        Compute position before/after and trade intent.

        Returns:
            Tuple of (position_before, position_after, intent)
        """
        if not condition_id or not outcome:
            return None, None, TradeIntent.UNKNOWN

        # Get current position
        stmt = select(WalletPosition).where(
            and_(
                WalletPosition.wallet == wallet,
                WalletPosition.condition_id == condition_id,
                WalletPosition.outcome == outcome,
            )
        )
        result = await session.execute(stmt)
        position = result.scalar_one_or_none()

        position_before = position.size if position else 0.0

        # Calculate position after
        if side.upper() == "BUY":
            position_after = position_before + trade_size
        else:  # SELL
            position_after = position_before - trade_size

        # Determine intent
        if position_before == 0 and position_after > 0:
            intent = TradeIntent.OPEN
        elif position_before > 0 and position_after == 0:
            intent = TradeIntent.CLOSE
        elif position_before > 0 and position_after > position_before:
            intent = TradeIntent.ADD
        elif position_before > 0 and position_after < position_before:
            intent = TradeIntent.REDUCE
        else:
            intent = TradeIntent.UNKNOWN

        return position_before, position_after, intent

    async def _compute_orderbook_context(
        self,
        session,
        token_id: str,
        trade_time: datetime,
        trade_size: float,
        side: str,
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """
        Compute orderbook context from snapshots.

        Returns:
            Tuple of (pre_mid, post_mid, price_impact_bps, pct_book_consumed)
        """
        # Find closest orderbook snapshot before trade
        stmt = (
            select(OrderbookSnapshot)
            .where(
                and_(
                    OrderbookSnapshot.token_id == token_id,
                    OrderbookSnapshot.timestamp <= trade_time,
                )
            )
            .order_by(OrderbookSnapshot.timestamp.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        snapshot = result.scalar_one_or_none()

        if not snapshot:
            return None, None, None, None

        pre_mid = snapshot.mid_price

        # Estimate book consumed
        if side.upper() == "BUY":
            depth = snapshot.ask_depth_5pct or 0
        else:
            depth = snapshot.bid_depth_5pct or 0

        pct_book_consumed = (trade_size / depth * 100) if depth > 0 else None

        # We don't have post_mid without a subsequent snapshot
        # This could be enhanced with real-time orderbook tracking
        post_mid = None
        price_impact_bps = None

        return pre_mid, post_mid, price_impact_bps, pct_book_consumed

    async def _update_position(
        self,
        session,
        wallet: str,
        condition_id: str,
        outcome: str,
        side: str,
        size: float,
        price: float,
    ):
        """Update wallet position after trade"""
        if not condition_id or not outcome:
            return

        stmt = select(WalletPosition).where(
            and_(
                WalletPosition.wallet == wallet,
                WalletPosition.condition_id == condition_id,
                WalletPosition.outcome == outcome,
            )
        )
        result = await session.execute(stmt)
        position = result.scalar_one_or_none()

        if position:
            # Update existing position
            if side.upper() == "BUY":
                # Update average entry price
                old_value = position.size * (position.avg_entry_price or 0)
                new_value = size * price
                position.size += size
                if position.size > 0:
                    position.avg_entry_price = (old_value + new_value) / position.size
            else:  # SELL
                position.size -= size
                if position.size <= 0:
                    position.size = 0
                    position.avg_entry_price = None

            position.last_update_at = datetime.utcnow()
        else:
            # Create new position
            if side.upper() == "BUY":
                position = WalletPosition(
                    wallet=wallet,
                    condition_id=condition_id,
                    outcome=outcome,
                    size=size,
                    avg_entry_price=price,
                    first_entry_at=datetime.utcnow(),
                    last_update_at=datetime.utcnow(),
                )
                session.add(position)

    async def stream_canonical_trades(
        self,
        since: Optional[datetime] = None,
    ) -> AsyncIterator[CanonicalTrade]:
        """
        Stream new canonical trades as they're created.

        Args:
            since: Only yield trades created after this time

        Yields:
            CanonicalTrade objects as they're created
        """
        last_id = 0

        while True:
            async with self.db.session() as session:
                stmt = select(CanonicalTrade).where(CanonicalTrade.id > last_id)

                if since:
                    stmt = stmt.where(CanonicalTrade.created_at > since)

                stmt = stmt.order_by(CanonicalTrade.id).limit(100)

                result = await session.execute(stmt)
                trades = result.scalars().all()

                for trade in trades:
                    last_id = trade.id
                    yield trade

            # Wait before checking for new trades
            await asyncio.sleep(1.0)

    def get_stats(self) -> dict:
        """Get aggregator statistics"""
        return {
            "trades_created": self._trades_created,
            "fills_processed": self._fills_processed,
        }
