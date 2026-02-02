"""
Main whale detection engine
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable, Awaitable

from loguru import logger

from src.database.models import Platform, AlertType, AlertSeverity
from src.database.db import Database
from src.platforms.base import TradeEvent, MarketInfo, BasePlatform
from .wallet_age import WalletAgeChecker
from .scoring import AlertScorer, ScoreBreakdown


@dataclass
class DetectionResult:
    """Result of whale detection analysis"""
    trade: TradeEvent
    should_alert: bool
    score: ScoreBreakdown
    alert_type: AlertType
    severity: AlertSeverity
    message: str
    market: Optional[MarketInfo] = None
    is_new_account: bool = False
    wallet_age_days: Optional[int] = None


# Callback type for detection results
DetectionCallback = Callable[[DetectionResult], Awaitable[None]]


class WhaleDetector:
    """
    Main detection engine for whale trades.

    Combines multiple signals:
    - Trade size thresholds
    - New account detection
    - Last-minute trade timing
    - Obscure market detection

    Emits detection results to registered callbacks.
    """

    def __init__(
        self,
        db: Database,
        config: dict,
        polygonscan_api_key: Optional[str] = None
    ):
        """
        Initialize whale detector.

        Args:
            db: Database instance
            config: Detection configuration
            polygonscan_api_key: API key for wallet age lookups
        """
        self.db = db
        self.config = config

        # Initialize components
        self.scorer = AlertScorer(config)
        self.wallet_checker = WalletAgeChecker(
            db,
            api_key=polygonscan_api_key,
            cache_duration=config.get('wallet_cache_duration', 86400)
        )

        # Callbacks for detection results
        self._callbacks: list[DetectionCallback] = []

        # Track recently alerted to avoid duplicates
        self._recent_alerts: dict[str, datetime] = {}
        self._dedup_window = config.get('deduplication_window', 300)  # 5 min

        # Market cache
        self._market_cache: dict[str, MarketInfo] = {}

    def on_detection(self, callback: DetectionCallback):
        """Register callback for detection results"""
        self._callbacks.append(callback)

    async def _emit_detection(self, result: DetectionResult):
        """Emit detection result to all callbacks"""
        for callback in self._callbacks:
            try:
                await callback(result)
            except Exception as e:
                logger.error(f"Error in detection callback: {e}")

    async def analyze_trade(
        self,
        trade: TradeEvent,
        platform: Optional[BasePlatform] = None
    ) -> DetectionResult:
        """
        Analyze a trade for whale activity.

        Args:
            trade: The trade event to analyze
            platform: Platform client (for market lookups)

        Returns:
            DetectionResult with analysis
        """
        # Get market info
        market = None
        if platform:
            try:
                market = await platform.get_market(trade.market_id)
                if market:
                    self._market_cache[trade.market_id] = market
            except Exception as e:
                logger.debug(f"Failed to get market info: {e}")
                market = self._market_cache.get(trade.market_id)

        # Check wallet age
        is_new_account = False
        wallet_age_days = None

        try:
            is_new_account = await self.wallet_checker.is_new_account(
                trade.wallet_address,
                trade.platform,
                self.config.get('new_account_days', 7)
            )

            age = await self.wallet_checker.get_wallet_age(
                trade.wallet_address,
                trade.platform
            )
            if age:
                wallet_age_days = age.days

        except Exception as e:
            logger.debug(f"Failed to check wallet age: {e}")

        # Score the trade
        score = self.scorer.score_trade(
            trade,
            market=market,
            is_new_account=is_new_account,
            wallet_age_days=wallet_age_days
        )

        # Determine alert type
        alert_type = self._determine_alert_type(score, is_new_account, market)

        # Check if we should alert
        should_alert = self.scorer.should_alert(score)

        # Check deduplication
        if should_alert:
            dedup_key = f"{trade.wallet_address}:{trade.market_id}"
            if self._is_duplicate(dedup_key):
                logger.debug(f"Skipping duplicate alert for {dedup_key}")
                should_alert = False
            else:
                self._record_alert(dedup_key)

        # Get severity and message
        severity = self.scorer.get_severity(score)
        message = self.scorer.format_alert_message(trade, score, market)

        return DetectionResult(
            trade=trade,
            should_alert=should_alert,
            score=score,
            alert_type=alert_type,
            severity=severity,
            message=message,
            market=market,
            is_new_account=is_new_account,
            wallet_age_days=wallet_age_days
        )

    def _determine_alert_type(
        self,
        score: ScoreBreakdown,
        is_new_account: bool,
        market: Optional[MarketInfo]
    ) -> AlertType:
        """Determine the primary alert type"""
        # Check each signal in priority order
        if is_new_account and score.new_account_bonus > 0:
            return AlertType.NEW_ACCOUNT_LARGE_TRADE

        if score.timing_factor > 10:
            return AlertType.LAST_MINUTE_TRADE

        if market and score.obscurity_factor > 10:
            return AlertType.OBSCURE_MARKET

        if score.base_score >= 30:
            return AlertType.LARGE_TRADE

        return AlertType.COMPOSITE

    def _is_duplicate(self, key: str) -> bool:
        """Check if this alert was recently sent"""
        if key not in self._recent_alerts:
            return False

        last_alert = self._recent_alerts[key]
        elapsed = (datetime.utcnow() - last_alert).total_seconds()
        return elapsed < self._dedup_window

    def _record_alert(self, key: str):
        """Record that an alert was sent"""
        self._recent_alerts[key] = datetime.utcnow()

        # Clean up old entries
        cutoff = datetime.utcnow()
        to_remove = [
            k for k, v in self._recent_alerts.items()
            if (cutoff - v).total_seconds() > self._dedup_window * 2
        ]
        for k in to_remove:
            del self._recent_alerts[k]

    async def process_trade(
        self,
        trade: TradeEvent,
        platform: Optional[BasePlatform] = None
    ):
        """
        Process a trade and emit detection if warranted.

        This is the main entry point called by platform listeners.

        Args:
            trade: The trade event
            platform: Platform client for lookups
        """
        try:
            # Quick filter: skip very small trades
            min_amount = self.config.get('min_trade_amount', 1000)
            if trade.amount_usd < min_amount:
                return

            # Analyze the trade
            result = await self.analyze_trade(trade, platform)

            # Store in database
            await self._store_trade(trade, result)

            # Emit if should alert
            if result.should_alert:
                logger.info(
                    f"Whale detected: ${trade.amount_usd:,.0f} by "
                    f"{trade.wallet_address[:10]}... (score: {result.score.final_score:.0f})"
                )
                await self._emit_detection(result)

        except Exception as e:
            logger.error(f"Error processing trade: {e}")

    async def _store_trade(self, trade: TradeEvent, result: DetectionResult):
        """Store trade and alert in database"""
        try:
            with self.db.get_session() as session:
                # Get or create wallet
                wallet = self.db.get_or_create_wallet(
                    session,
                    trade.wallet_address,
                    trade.platform,
                    metadata={'first_seen_via': 'trade'}
                )

                # Update wallet age info
                wallet.is_new_account = result.is_new_account

                # Get or create market
                market_kwargs = {}
                if result.market:
                    market_kwargs = {
                        'volume_24h': result.market.volume_24h,
                        'close_time': result.market.close_time,
                        'is_active': result.market.is_active
                    }

                market = self.db.get_or_create_market(
                    session,
                    trade.market_id,
                    trade.platform,
                    trade.market_name,
                    **market_kwargs
                )

                # Record trade
                db_trade = self.db.record_trade(
                    session,
                    wallet,
                    market,
                    trade_type=trade.trade_type,
                    amount_usd=trade.amount_usd,
                    price=trade.price,
                    shares=trade.shares,
                    outcome=trade.outcome,
                    timestamp=trade.timestamp,
                    external_id=trade.external_id,
                    tx_hash=trade.tx_hash,
                    block_number=trade.block_number,
                    raw_data=trade.raw_data
                )

                # Record alert if warranted
                if result.should_alert:
                    self.db.record_alert(
                        session,
                        db_trade,
                        result.alert_type,
                        result.severity,
                        result.score.final_score,
                        result.message,
                        details=result.score.to_dict()
                    )

        except Exception as e:
            logger.error(f"Error storing trade: {e}")

    async def close(self):
        """Clean up resources"""
        await self.wallet_checker.close()
