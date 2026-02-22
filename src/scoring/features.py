"""
Feature extraction for copy scoring.
Extracts relevant features from trades for scoring.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from ..database.models import (
    CanonicalTrade, WalletStats, OrderbookSnapshot, MarketRegistry, WalletTier
)
from ..database.db import Database

logger = logging.getLogger(__name__)


@dataclass
class TradeFeatures:
    """Extracted features for a trade"""
    # Size context
    usd_size: float
    pct_book_consumed: Optional[float]
    size_vs_wallet_avg: Optional[float]

    # Price impact
    price_impact_bps: Optional[float]
    spread_at_entry: Optional[float]

    # Wallet quality
    wallet_tier: WalletTier
    wallet_win_rate_1h: Optional[float]
    wallet_sharpe_1h: Optional[float]
    wallet_avg_markout_1h: Optional[float]
    wallet_total_trades: int

    # Intent signals
    trade_intent: str  # OPEN, CLOSE, ADD, REDUCE, UNKNOWN
    position_change_pct: Optional[float]

    # Market context
    hours_to_close: Optional[float]
    market_volume_24h: Optional[float]
    current_price: Optional[float]
    price_vs_50: Optional[float]  # Distance from 0.5

    # Timing
    is_after_hours: bool  # Weekend/off hours
    minutes_since_market_open: Optional[int]

    # Category context (A6: efficiency scoring)
    market_category: Optional[str] = None  # sports, politics, crypto, etc.


class FeatureExtractor:
    """
    Extract features from trades for copy scoring.

    Features are designed to capture:
    1. Size significance (relative to book depth, not volume)
    2. Trader quality (historical markout performance)
    3. Trade intent (opening vs closing position)
    4. Market context (timing, liquidity)
    """

    def __init__(self, db: Database):
        """
        Initialize the feature extractor.

        Args:
            db: Database instance
        """
        self.db = db

    async def extract_features(
        self,
        trade: CanonicalTrade,
        wallet_stats: Optional[WalletStats] = None,
        orderbook: Optional[OrderbookSnapshot] = None,
        market: Optional[MarketRegistry] = None,
    ) -> TradeFeatures:
        """
        Extract all features for a trade.

        Args:
            trade: The canonical trade
            wallet_stats: Pre-fetched wallet stats (optional)
            orderbook: Pre-fetched orderbook snapshot (optional)
            market: Pre-fetched market info (optional)

        Returns:
            TradeFeatures dataclass
        """
        # Fetch missing data
        if wallet_stats is None:
            wallet_stats = await self._get_wallet_stats(trade.wallet)

        if orderbook is None:
            orderbook = await self._get_orderbook(trade.token_id)

        if market is None:
            market = await self._get_market(trade.condition_id)

        # Compute features
        return TradeFeatures(
            # Size context
            usd_size=trade.total_usd,
            pct_book_consumed=self._compute_pct_book_consumed(trade, orderbook),
            size_vs_wallet_avg=self._compute_size_vs_avg(trade, wallet_stats),

            # Price impact
            price_impact_bps=trade.price_impact_bps,
            spread_at_entry=orderbook.spread_bps if orderbook else None,

            # Wallet quality
            wallet_tier=wallet_stats.tier if wallet_stats else WalletTier.UNKNOWN,
            wallet_win_rate_1h=wallet_stats.win_rate_1h if wallet_stats else None,
            wallet_sharpe_1h=wallet_stats.sharpe_1h if wallet_stats else None,
            wallet_avg_markout_1h=wallet_stats.avg_markout_1h if wallet_stats else None,
            wallet_total_trades=wallet_stats.total_trades if wallet_stats else 0,

            # Intent signals
            trade_intent=trade.intent.value if trade.intent else "unknown",
            position_change_pct=self._compute_position_change_pct(trade),

            # Market context
            hours_to_close=market.hours_to_close if market else None,
            market_volume_24h=market.volume_24h if market else None,
            current_price=orderbook.mid_price if orderbook else trade.avg_price,
            price_vs_50=self._compute_price_vs_50(
                orderbook.mid_price if orderbook else trade.avg_price
            ),

            # Timing
            is_after_hours=self._is_after_hours(trade.first_fill_time),
            minutes_since_market_open=None,  # Would need market open time
        )

    def _compute_pct_book_consumed(
        self,
        trade: CanonicalTrade,
        orderbook: Optional[OrderbookSnapshot],
    ) -> Optional[float]:
        """
        Compute what percentage of the orderbook was consumed.

        Uses 5% depth as reference (more meaningful than total depth).
        """
        if not orderbook:
            return trade.pct_book_consumed

        # Get relevant depth based on trade side
        if trade.side.upper() == "BUY":
            depth = orderbook.ask_depth_5pct
        else:
            depth = orderbook.bid_depth_5pct

        if not depth or depth <= 0:
            return None

        # Trade size as percentage of available depth
        return (trade.total_usd / depth) * 100

    def _compute_size_vs_avg(
        self,
        trade: CanonicalTrade,
        wallet_stats: Optional[WalletStats],
    ) -> Optional[float]:
        """Compute trade size relative to wallet's average"""
        if not wallet_stats or not wallet_stats.avg_trade_size:
            return None

        if wallet_stats.avg_trade_size <= 0:
            return None

        return trade.total_usd / wallet_stats.avg_trade_size

    def _compute_position_change_pct(
        self,
        trade: CanonicalTrade,
    ) -> Optional[float]:
        """Compute position change as percentage"""
        if trade.position_before is None or trade.position_after is None:
            return None

        if trade.position_before == 0:
            # New position - infinite % change, return special value
            return 100.0

        return abs(trade.position_after - trade.position_before) / trade.position_before * 100

    def _compute_price_vs_50(self, price: Optional[float]) -> Optional[float]:
        """Compute distance from 0.5 (50/50 odds)"""
        if price is None:
            return None

        return abs(price - 0.5)

    def _is_after_hours(self, timestamp: datetime) -> bool:
        """
        Check if trade occurred during off-hours.

        Off hours defined as:
        - Weekends (Saturday, Sunday)
        - Late night (11pm - 6am EST)
        """
        # Weekend check
        if timestamp.weekday() >= 5:  # Saturday = 5, Sunday = 6
            return True

        # Late night check (UTC time, adjust for EST)
        hour = timestamp.hour
        # Roughly 4am-11am UTC is 11pm-6am EST
        if hour >= 4 and hour < 11:
            return True

        return False

    async def _get_wallet_stats(self, wallet: str) -> Optional[WalletStats]:
        """Fetch wallet stats from database"""
        from sqlalchemy import select

        async with self.db.session() as session:
            stmt = select(WalletStats).where(WalletStats.wallet == wallet)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _get_orderbook(self, token_id: str) -> Optional[OrderbookSnapshot]:
        """Fetch latest orderbook snapshot"""
        from sqlalchemy import select

        async with self.db.session() as session:
            stmt = (
                select(OrderbookSnapshot)
                .where(OrderbookSnapshot.token_id == token_id)
                .order_by(OrderbookSnapshot.timestamp.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _get_market(self, condition_id: str) -> Optional[MarketRegistry]:
        """Fetch market info from registry"""
        from sqlalchemy import select

        async with self.db.session() as session:
            stmt = select(MarketRegistry).where(
                MarketRegistry.condition_id == condition_id
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    def features_to_dict(self, features: TradeFeatures) -> Dict:
        """Convert features to dictionary for logging/debugging"""
        return {
            "usd_size": features.usd_size,
            "pct_book_consumed": features.pct_book_consumed,
            "size_vs_wallet_avg": features.size_vs_wallet_avg,
            "price_impact_bps": features.price_impact_bps,
            "spread_at_entry": features.spread_at_entry,
            "wallet_tier": features.wallet_tier.value,
            "wallet_win_rate_1h": features.wallet_win_rate_1h,
            "wallet_sharpe_1h": features.wallet_sharpe_1h,
            "wallet_avg_markout_1h": features.wallet_avg_markout_1h,
            "wallet_total_trades": features.wallet_total_trades,
            "trade_intent": features.trade_intent,
            "position_change_pct": features.position_change_pct,
            "hours_to_close": features.hours_to_close,
            "market_volume_24h": features.market_volume_24h,
            "current_price": features.current_price,
            "price_vs_50": features.price_vs_50,
            "is_after_hours": features.is_after_hours,
        }
