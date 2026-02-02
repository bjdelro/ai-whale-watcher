"""
Wallet profiler for computing performance metrics.
Builds profiles based on historical markout performance.
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, func, and_

from ..database.models import (
    CanonicalTrade, Markout, WalletStats, WalletTier
)
from ..database.db import Database

logger = logging.getLogger(__name__)


class WalletProfiler:
    """
    Build and maintain wallet performance profiles.

    Profiles include:
    - Win rates at various horizons
    - Average markouts (forward returns)
    - Sharpe ratios
    - Trading patterns
    - Tier classification
    """

    # Tier thresholds
    TIER_THRESHOLDS = {
        WalletTier.ELITE: {
            "min_trades": 50,
            "min_win_rate_1h": 0.60,
            "min_avg_markout_1h": 50,  # 50 bps
        },
        WalletTier.GOOD: {
            "min_trades": 20,
            "min_win_rate_1h": 0.55,
            "min_avg_markout_1h": 20,  # 20 bps
        },
        WalletTier.BAD: {
            "max_win_rate_1h": 0.45,
            "max_avg_markout_1h": -20,  # -20 bps
        },
    }

    def __init__(
        self,
        db: Database,
        update_interval: float = 300.0,  # 5 minutes
        min_trades_for_stats: int = 5,
    ):
        """
        Initialize the wallet profiler.

        Args:
            db: Database instance
            update_interval: Seconds between profile updates
            min_trades_for_stats: Minimum trades needed for meaningful stats
        """
        self.db = db
        self.update_interval = update_interval
        self.min_trades_for_stats = min_trades_for_stats

        # Cache for quick lookups
        self._stats_cache: Dict[str, WalletStats] = {}
        self._cache_ttl = 60.0  # seconds
        self._cache_timestamps: Dict[str, datetime] = {}

        # Stats
        self._profiles_updated = 0
        self._running = False

    async def start(self):
        """Start the profile update loop"""
        self._running = True
        logger.info("Wallet profiler started")

        while self._running:
            try:
                await self._update_all_profiles()
            except Exception as e:
                logger.error(f"Profile update error: {e}")

            await asyncio.sleep(self.update_interval)

    def stop(self):
        """Stop the profile update loop"""
        self._running = False
        logger.info("Wallet profiler stopped")

    async def update_wallet_stats(self, wallet: str) -> Optional[WalletStats]:
        """
        Recompute stats for a specific wallet.

        Args:
            wallet: Wallet address

        Returns:
            Updated WalletStats or None if insufficient data
        """
        async with self.db.session() as session:
            # Get trade count and volume
            trade_stmt = select(
                func.count(CanonicalTrade.id).label("count"),
                func.sum(CanonicalTrade.total_usd).label("volume"),
                func.avg(CanonicalTrade.total_usd).label("avg_size"),
                func.min(CanonicalTrade.first_fill_time).label("first_trade"),
                func.max(CanonicalTrade.first_fill_time).label("last_trade"),
            ).where(CanonicalTrade.wallet == wallet)

            result = await session.execute(trade_stmt)
            trade_row = result.one()

            total_trades = trade_row.count or 0
            total_volume = trade_row.volume or 0.0
            avg_trade_size = trade_row.avg_size
            first_trade_at = trade_row.first_trade
            last_trade_at = trade_row.last_trade

            if total_trades < self.min_trades_for_stats:
                # Not enough data
                tier = WalletTier.UNKNOWN
                win_rates = {}
                avg_markouts = {}
                sharpes = {}
            else:
                # Compute win rates and average markouts for each horizon
                win_rates = {}
                avg_markouts = {}
                sharpes = {}

                for horizon in ["5m", "1h", "6h", "1d"]:
                    returns = await self._get_wallet_returns(session, wallet, horizon)

                    if len(returns) >= self.min_trades_for_stats:
                        win_rates[horizon] = sum(1 for r in returns if r > 0) / len(returns)
                        avg_markouts[horizon] = sum(returns) / len(returns)
                        sharpes[horizon] = self._compute_sharpe(returns)
                    else:
                        win_rates[horizon] = None
                        avg_markouts[horizon] = None
                        sharpes[horizon] = None

                # Determine tier
                tier = self._classify_tier(
                    total_trades, win_rates.get("1h"), avg_markouts.get("1h")
                )

            # Get preferred markets
            market_stmt = (
                select(
                    CanonicalTrade.condition_id,
                    func.count(CanonicalTrade.id).label("count"),
                )
                .where(CanonicalTrade.wallet == wallet)
                .group_by(CanonicalTrade.condition_id)
                .order_by(func.count(CanonicalTrade.id).desc())
                .limit(5)
            )
            result = await session.execute(market_stmt)
            preferred = [row.condition_id for row in result]

            # Update or create WalletStats
            stats_stmt = select(WalletStats).where(WalletStats.wallet == wallet)
            result = await session.execute(stats_stmt)
            stats = result.scalar_one_or_none()

            if stats:
                # Update existing
                stats.total_trades = total_trades
                stats.total_volume_usd = total_volume
                stats.avg_trade_size = avg_trade_size
                stats.win_rate_5m = win_rates.get("5m")
                stats.win_rate_1h = win_rates.get("1h")
                stats.win_rate_6h = win_rates.get("6h")
                stats.win_rate_1d = win_rates.get("1d")
                stats.avg_markout_5m = avg_markouts.get("5m")
                stats.avg_markout_1h = avg_markouts.get("1h")
                stats.avg_markout_6h = avg_markouts.get("6h")
                stats.avg_markout_1d = avg_markouts.get("1d")
                stats.sharpe_1h = sharpes.get("1h")
                stats.sharpe_1d = sharpes.get("1d")
                stats.tier = tier
                stats.preferred_markets = json.dumps(preferred)
                stats.first_trade_at = first_trade_at
                stats.last_trade_at = last_trade_at
                stats.updated_at = datetime.utcnow()
            else:
                # Create new
                stats = WalletStats(
                    wallet=wallet,
                    total_trades=total_trades,
                    total_volume_usd=total_volume,
                    avg_trade_size=avg_trade_size,
                    win_rate_5m=win_rates.get("5m"),
                    win_rate_1h=win_rates.get("1h"),
                    win_rate_6h=win_rates.get("6h"),
                    win_rate_1d=win_rates.get("1d"),
                    avg_markout_5m=avg_markouts.get("5m"),
                    avg_markout_1h=avg_markouts.get("1h"),
                    avg_markout_6h=avg_markouts.get("6h"),
                    avg_markout_1d=avg_markouts.get("1d"),
                    sharpe_1h=sharpes.get("1h"),
                    sharpe_1d=sharpes.get("1d"),
                    tier=tier,
                    preferred_markets=json.dumps(preferred),
                    first_trade_at=first_trade_at,
                    last_trade_at=last_trade_at,
                )
                session.add(stats)

            await session.commit()

            # Update cache
            self._stats_cache[wallet] = stats
            self._cache_timestamps[wallet] = datetime.utcnow()
            self._profiles_updated += 1

            return stats

    async def _get_wallet_returns(
        self, session, wallet: str, horizon: str
    ) -> List[float]:
        """Get markout returns for a wallet at a specific horizon"""
        stmt = (
            select(Markout.return_bps)
            .join(CanonicalTrade, Markout.canonical_trade_id == CanonicalTrade.id)
            .where(
                and_(
                    CanonicalTrade.wallet == wallet,
                    Markout.horizon == horizon,
                    Markout.is_final == True,
                )
            )
        )
        result = await session.execute(stmt)
        return [r for (r,) in result if r is not None]

    def _compute_sharpe(self, returns: List[float]) -> Optional[float]:
        """
        Compute Sharpe ratio from returns.

        Assumes returns are in basis points.
        Uses standard deviation as risk measure.
        """
        if len(returns) < 5:
            return None

        mean_return = sum(returns) / len(returns)

        if len(returns) < 2:
            return None

        variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = math.sqrt(variance)

        if std_dev == 0:
            return None

        # Sharpe = mean / std (no risk-free rate for simplicity)
        return round(mean_return / std_dev, 2)

    def _classify_tier(
        self,
        total_trades: int,
        win_rate_1h: Optional[float],
        avg_markout_1h: Optional[float],
    ) -> WalletTier:
        """Classify wallet into a tier based on performance"""
        if total_trades < 10:
            return WalletTier.UNKNOWN

        if win_rate_1h is None or avg_markout_1h is None:
            return WalletTier.NEUTRAL

        # Check ELITE
        elite = self.TIER_THRESHOLDS[WalletTier.ELITE]
        if (
            total_trades >= elite["min_trades"]
            and win_rate_1h >= elite["min_win_rate_1h"]
            and avg_markout_1h >= elite["min_avg_markout_1h"]
        ):
            return WalletTier.ELITE

        # Check GOOD
        good = self.TIER_THRESHOLDS[WalletTier.GOOD]
        if (
            total_trades >= good["min_trades"]
            and win_rate_1h >= good["min_win_rate_1h"]
            and avg_markout_1h >= good["min_avg_markout_1h"]
        ):
            return WalletTier.GOOD

        # Check BAD
        bad = self.TIER_THRESHOLDS[WalletTier.BAD]
        if win_rate_1h <= bad["max_win_rate_1h"] or avg_markout_1h <= bad["max_avg_markout_1h"]:
            return WalletTier.BAD

        return WalletTier.NEUTRAL

    async def get_wallet_stats(self, wallet: str) -> Optional[WalletStats]:
        """
        Get stats for a wallet, with caching.

        Args:
            wallet: Wallet address

        Returns:
            WalletStats or None
        """
        # Check cache
        if wallet in self._stats_cache:
            cached_at = self._cache_timestamps.get(wallet)
            if cached_at and (datetime.utcnow() - cached_at).total_seconds() < self._cache_ttl:
                return self._stats_cache[wallet]

        # Fetch from database
        async with self.db.session() as session:
            stmt = select(WalletStats).where(WalletStats.wallet == wallet)
            result = await session.execute(stmt)
            stats = result.scalar_one_or_none()

            if stats:
                self._stats_cache[wallet] = stats
                self._cache_timestamps[wallet] = datetime.utcnow()

            return stats

    async def get_wallet_tier(self, wallet: str) -> WalletTier:
        """Get the tier classification for a wallet"""
        stats = await self.get_wallet_stats(wallet)
        return stats.tier if stats else WalletTier.UNKNOWN

    async def _update_all_profiles(self) -> int:
        """
        Update profiles for all active wallets.

        Returns:
            Number of profiles updated
        """
        # Get wallets with recent activity
        cutoff = datetime.utcnow() - timedelta(days=7)

        async with self.db.session() as session:
            stmt = (
                select(CanonicalTrade.wallet)
                .where(CanonicalTrade.first_fill_time >= cutoff)
                .distinct()
            )
            result = await session.execute(stmt)
            wallets = [w for (w,) in result]

        updated = 0
        for wallet in wallets:
            try:
                await self.update_wallet_stats(wallet)
                updated += 1
            except Exception as e:
                logger.warning(f"Failed to update profile for {wallet[:10]}...: {e}")

            # Rate limiting
            await asyncio.sleep(0.1)

        if updated > 0:
            logger.info(f"Updated {updated} wallet profiles")

        return updated

    async def get_top_wallets(
        self,
        tier: Optional[WalletTier] = None,
        min_trades: int = 20,
        limit: int = 50,
    ) -> List[WalletStats]:
        """
        Get top performing wallets.

        Args:
            tier: Filter by tier (None for all)
            min_trades: Minimum trades required
            limit: Max wallets to return

        Returns:
            List of WalletStats sorted by performance
        """
        async with self.db.session() as session:
            stmt = (
                select(WalletStats)
                .where(WalletStats.total_trades >= min_trades)
            )

            if tier:
                stmt = stmt.where(WalletStats.tier == tier)

            stmt = stmt.order_by(WalletStats.avg_markout_1h.desc()).limit(limit)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    def get_stats(self) -> dict:
        """Get profiler statistics"""
        return {
            "running": self._running,
            "profiles_updated": self._profiles_updated,
            "cache_size": len(self._stats_cache),
        }
