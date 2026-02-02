#!/usr/bin/env python3
"""
Paper Trading Runner for AI Whale Watcher

Runs the copy-edge scoring system in paper trading mode.
Monitors Polymarket for high-quality copy signals and simulates trades.

Usage:
    python run_paper_trading.py [--max-trade 1.0] [--max-total 100.0] [--min-score 70]

Press Ctrl+C to stop and see the final report.
"""

import asyncio
import argparse
import logging
import os
import signal
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Reduce noise from other loggers
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

from src.database.db import Database
from src.core.polymarket_client import PolymarketClient
from src.ingestion.market_registry import MarketRegistryService
from src.ingestion.websocket_feed import WebSocketFeed
from src.ingestion.fill_aggregator import FillAggregator
from src.scoring.wallet_profiler import WalletProfiler
from src.execution.paper_trader import PaperTrader
from src.execution.risk_manager import RiskLimits


class PaperTradingRunner:
    """
    Main runner for paper trading mode.

    Orchestrates:
    1. WebSocket connection for real-time trades
    2. Trade aggregation
    3. Copy scoring
    4. Paper trade simulation
    5. Periodic reporting
    """

    def __init__(self, risk_limits: RiskLimits):
        self.risk_limits = risk_limits

        # Components (initialized in start())
        self.db: Database = None
        self.client: PolymarketClient = None
        self.market_registry: MarketRegistryService = None
        self.websocket: WebSocketFeed = None
        self.feed_manager: TradeFeedManager = None
        self.aggregator: FillAggregator = None
        self.wallet_profiler: WalletProfiler = None
        self.paper_trader: PaperTrader = None

        # State
        self._running = False
        self._trades_processed = 0
        self._last_report_time = datetime.utcnow()

    async def start(self):
        """Initialize all components and start processing"""
        logger.info("=" * 60)
        logger.info("AI WHALE WATCHER - PAPER TRADING MODE")
        logger.info("=" * 60)
        logger.info(f"Max per trade: ${self.risk_limits.max_per_trade_usd:.2f}")
        logger.info(f"Max total exposure: ${self.risk_limits.max_total_exposure_usd:.2f}")
        logger.info(f"Min copy score: {self.risk_limits.min_copy_score:.0f}")
        logger.info(f"Allowed wallet tiers: {self.risk_limits.allowed_wallet_tiers}")
        logger.info("=" * 60)

        # Initialize database
        logger.info("Initializing database...")
        self.db = Database("whale_watcher.db")
        await self.db.initialize()

        # Initialize Polymarket client
        logger.info("Initializing Polymarket client...")
        private_key = os.getenv("PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY")
        if not private_key:
            logger.warning("PRIVATE_KEY not set - WebSocket authentication may fail")

        self.client = PolymarketClient(private_key=private_key)

        # Initialize market registry
        logger.info("Syncing market registry...")
        self.market_registry = MarketRegistryService(self.client, self.db)
        await self.market_registry.sync_markets()  # One-time sync
        logger.info(f"Synced {self.market_registry._markets_synced} markets to registry")

        # Initialize paper trader
        logger.info("Initializing paper trader...")
        self.paper_trader = PaperTrader(
            db=self.db,
            polymarket_client=self.client,
            risk_limits=self.risk_limits,
            log_dir="paper_trades",
        )

        # Initialize fill aggregator
        self.aggregator = FillAggregator(self.db)

        # Initialize wallet profiler
        self.wallet_profiler = WalletProfiler(self.db)

        # Get top markets by volume for monitoring
        # Start with fewer markets to avoid overwhelming WebSocket
        top_markets = await self._get_top_markets(limit=20)
        logger.info(f"Monitoring {len(top_markets)} top token IDs")

        # Initialize WebSocket with our trade handler
        self.websocket = WebSocketFeed(
            on_trade=self._on_trade,
        )

        # Subscribe to top markets
        for token_id in top_markets:
            await self.websocket.subscribe_token(token_id)

        self._running = True
        logger.info("Paper trading started! Waiting for signals...")
        logger.info("Press Ctrl+C to stop and see report\n")

    async def run(self):
        """Main run loop"""
        # Start WebSocket in background
        ws_task = asyncio.create_task(self.websocket.connect())

        # Periodic tasks
        report_task = asyncio.create_task(self._periodic_report())
        position_task = asyncio.create_task(self._periodic_position_update())

        try:
            # Wait for WebSocket (runs forever until cancelled)
            await ws_task
        except asyncio.CancelledError:
            logger.info("Shutting down...")
        finally:
            report_task.cancel()
            position_task.cancel()
            await self.stop()

    async def stop(self):
        """Clean shutdown"""
        self._running = False

        if self.websocket:
            await self.websocket.disconnect()

        if self.db:
            await self.db.close()

        # Print final report
        if self.paper_trader:
            self.paper_trader.print_report()

    def _on_trade(self, trade_data):
        """
        Called when a new trade arrives via WebSocket.
        This is a sync callback that schedules async processing.

        Args:
            trade_data: TradeData object from WebSocket
        """
        # Schedule async processing
        asyncio.create_task(self._process_trade(trade_data))

    async def _process_trade(self, trade_data):
        """Process a trade asynchronously"""
        try:
            self._trades_processed += 1

            # Get wallet address (taker is the one we want to potentially copy)
            wallet = trade_data.taker
            if not wallet:
                return

            # Check if this is a "large enough" trade to care about
            usd_value = trade_data.size * trade_data.price

            # Skip tiny trades
            if usd_value < 100:  # Minimum $100 to even consider
                return

            # Build a minimal CanonicalTrade object for scoring
            from src.database.models import CanonicalTrade, TradeIntent

            # We need to fetch wallet stats for scoring
            wallet_stats = await self._get_or_create_wallet_stats(wallet)

            # Create a trade object for scoring
            trade = CanonicalTrade(
                market_order_id=trade_data.id or "",
                wallet=wallet,
                condition_id=trade_data.condition_id or trade_data.token_id,
                token_id=trade_data.token_id,
                side=trade_data.side,
                outcome=trade_data.outcome,
                avg_price=trade_data.price,
                total_size=trade_data.size,
                total_usd=usd_value,
                num_fills=1,
                first_fill_time=trade_data.timestamp or datetime.utcnow(),
                last_fill_time=trade_data.timestamp or datetime.utcnow(),
                intent=TradeIntent.UNKNOWN,
            )

            # Get market info
            market = self.market_registry.get_by_condition(trade.condition_id)

            # Evaluate for copy potential
            score = await self.paper_trader.evaluate_trade(
                trade=trade,
                wallet_stats=wallet_stats,
                market=market,
            )

            if score:
                # This trade passed the scoring threshold
                logger.info(
                    f"\n{'='*50}\n"
                    f"COPY SIGNAL: {score.recommendation}\n"
                    f"Score: {score.score:.0f}/100 ({score.confidence})\n"
                    f"Wallet: {wallet[:12]}... ({score.features.wallet_tier.value})\n"
                    f"Trade: {trade.side} {trade.outcome or 'YES'} @ {trade_data.price:.2%} (${usd_value:,.0f})\n"
                    f"Market: {market.question[:60] if market else 'Unknown'}...\n"
                    f"{'='*50}"
                )

                # Simulate the copy trade
                await self.paper_trader.simulate_copy(trade, score, market)

        except Exception as e:
            logger.error(f"Error processing trade: {e}")

    async def _get_or_create_wallet_stats(self, wallet: str):
        """Get wallet stats, creating minimal entry if not exists"""
        from sqlalchemy import select
        from src.database.models import WalletStats, WalletTier

        async with self.db.session() as session:
            stmt = select(WalletStats).where(WalletStats.wallet == wallet)
            result = await session.execute(stmt)
            stats = result.scalar_one_or_none()

            if not stats:
                # Create minimal stats for unknown wallet
                stats = WalletStats(
                    wallet=wallet,
                    total_trades=0,
                    tier=WalletTier.UNKNOWN,
                )
                session.add(stats)

            return stats

    async def _get_top_markets(self, limit: int = 50) -> list:
        """Get token IDs for top markets by volume"""
        from sqlalchemy import select
        from src.database.models import MarketRegistry

        token_ids = []

        async with self.db.session() as session:
            stmt = (
                select(MarketRegistry)
                .where(MarketRegistry.is_active == True)
                .order_by(MarketRegistry.volume_24h.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            markets = result.scalars().all()

            for market in markets:
                if market.token_id_yes:
                    token_ids.append(market.token_id_yes)
                if market.token_id_no:
                    token_ids.append(market.token_id_no)

        return token_ids[:limit * 2]  # YES and NO tokens

    def _on_ws_error(self, error: Exception):
        """Handle WebSocket errors"""
        logger.error(f"WebSocket error: {error}")

    async def _periodic_report(self):
        """Print status report every 5 minutes"""
        while self._running:
            await asyncio.sleep(300)  # 5 minutes

            stats = self.paper_trader.get_stats()
            logger.info(
                f"\n--- STATUS ---\n"
                f"Runtime: {stats['runtime_hours']:.1f}h | "
                f"Signals: {stats['signals_seen']} | "
                f"Passed: {stats['signals_passed']} | "
                f"Trades: {stats['trades_simulated']} | "
                f"Positions: {stats['positions']} | "
                f"P&L: ${stats['realized_pnl'] + stats['unrealized_pnl']:+.2f}\n"
            )

    async def _periodic_position_update(self):
        """Update position prices every minute"""
        while self._running:
            await asyncio.sleep(60)

            try:
                await self.paper_trader.update_positions()
                await self.paper_trader.check_for_exits()
            except Exception as e:
                logger.debug(f"Error updating positions: {e}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run AI Whale Watcher in paper trading mode"
    )
    parser.add_argument(
        "--max-trade",
        type=float,
        default=1.0,
        help="Maximum $ per trade (default: 1.00)"
    )
    parser.add_argument(
        "--max-total",
        type=float,
        default=100.0,
        help="Maximum total exposure (default: 100.00)"
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=70.0,
        help="Minimum copy score to trade (default: 70)"
    )
    parser.add_argument(
        "--max-daily-loss",
        type=float,
        default=20.0,
        help="Maximum daily loss before halt (default: 20.00)"
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    # Configure risk limits from arguments
    risk_limits = RiskLimits(
        max_per_trade_usd=args.max_trade,
        max_total_exposure_usd=args.max_total,
        min_copy_score=args.min_score,
        max_daily_loss_usd=args.max_daily_loss,
    )

    runner = PaperTradingRunner(risk_limits)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("\nReceived shutdown signal...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    try:
        await runner.start()
        await runner.run()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
