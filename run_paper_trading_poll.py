#!/usr/bin/env python3
"""
Paper Trading Runner (Polling Mode) for AI Whale Watcher

Uses polling instead of WebSocket to fetch recent trades.
More reliable but slightly delayed (~10-30 seconds).

Usage:
    python run_paper_trading_poll.py [--max-trade 1.0] [--max-total 100.0] [--min-score 70]

Press Ctrl+C to stop and see the final report.
"""

import asyncio
import argparse
import logging
import os
import signal
import sys
from datetime import datetime, timedelta
from typing import Set

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
logging.getLogger("httpx").setLevel(logging.WARNING)

from src.database.db import Database
from src.core.polymarket_client import PolymarketClient
from src.database.models import CanonicalTrade, TradeIntent, WalletStats, WalletTier
from src.ingestion.market_registry import MarketRegistryService
from src.scoring.copy_scorer import CopyScorer
from src.scoring.features import FeatureExtractor
from src.execution.paper_trader import PaperTrader
from src.execution.risk_manager import RiskLimits


class PaperTradingPollingRunner:
    """
    Paper trading using API polling instead of WebSocket.

    More reliable for long-running sessions:
    - Polls recent trades every 10 seconds
    - Tracks seen trade IDs to avoid duplicates
    - Automatically recovers from API errors
    """

    POLL_INTERVAL = 10  # seconds
    TRADES_LOOKBACK = 100  # number of recent trades to fetch

    def __init__(self, risk_limits: RiskLimits):
        self.risk_limits = risk_limits

        # Components
        self.db: Database = None
        self.client: PolymarketClient = None
        self.market_registry: MarketRegistryService = None
        self.paper_trader: PaperTrader = None

        # State
        self._running = False
        self._seen_trades: Set[str] = set()
        self._trades_processed = 0
        self._last_report_time = datetime.utcnow()
        self._start_time = datetime.utcnow()

    async def start(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("AI WHALE WATCHER - PAPER TRADING (POLLING MODE)")
        logger.info("=" * 60)
        logger.info(f"Max per trade: ${self.risk_limits.max_per_trade_usd:.2f}")
        logger.info(f"Max total exposure: ${self.risk_limits.max_total_exposure_usd:.2f}")
        logger.info(f"Min copy score: {self.risk_limits.min_copy_score:.0f}")
        logger.info(f"Poll interval: {self.POLL_INTERVAL}s")
        logger.info("=" * 60)

        # Initialize database
        logger.info("Initializing database...")
        self.db = Database("whale_watcher.db")
        await self.db.initialize()

        # Initialize Polymarket client
        logger.info("Initializing Polymarket client...")
        private_key = os.getenv("PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY")
        if not private_key:
            logger.error("PRIVATE_KEY not set - cannot fetch trades")
            raise ValueError("PRIVATE_KEY required")

        self.client = PolymarketClient(private_key=private_key)

        # Initialize market registry
        logger.info("Syncing market registry...")
        self.market_registry = MarketRegistryService(self.client, self.db)
        await self.market_registry.sync_markets()
        logger.info(f"Synced {self.market_registry._markets_synced} markets")

        # Initialize paper trader
        logger.info("Initializing paper trader...")
        self.paper_trader = PaperTrader(
            db=self.db,
            polymarket_client=self.client,
            risk_limits=self.risk_limits,
            log_dir="paper_trades",
        )

        self._running = True
        logger.info("Paper trading started! Polling for trades...")
        logger.info("Press Ctrl+C to stop and see report\n")

    async def run(self):
        """Main polling loop"""
        report_task = asyncio.create_task(self._periodic_report())

        try:
            while self._running:
                try:
                    await self._poll_trades()
                except Exception as e:
                    logger.error(f"Error polling trades: {e}")

                await asyncio.sleep(self.POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Shutting down...")
        finally:
            report_task.cancel()
            await self.stop()

    async def stop(self):
        """Clean shutdown"""
        self._running = False

        if self.db:
            await self.db.close()

        if self.paper_trader:
            self.paper_trader.print_report()

    async def _poll_trades(self):
        """Poll for recent trades across active markets"""
        try:
            # Get recent trades from the API
            trades = await self.client.get_trades(
                limit=self.TRADES_LOOKBACK
            )

            if not trades:
                return

            new_count = 0
            for trade in trades:
                # Skip if we've seen this trade
                trade_id = trade.id or f"{trade.taker}_{trade.timestamp}"
                if trade_id in self._seen_trades:
                    continue

                self._seen_trades.add(trade_id)
                new_count += 1

                # Process the trade
                await self._process_trade(trade)

            if new_count > 0:
                logger.debug(f"Processed {new_count} new trades")

            # Cleanup old seen trades (keep last 10000)
            if len(self._seen_trades) > 10000:
                # Convert to list, sort, keep recent
                self._seen_trades = set(list(self._seen_trades)[-5000:])

        except Exception as e:
            logger.warning(f"Failed to poll trades: {e}")

    async def _process_trade(self, trade_data):
        """Process a single trade"""
        try:
            self._trades_processed += 1

            # Get wallet address
            wallet = trade_data.taker
            if not wallet:
                return

            # Check if trade is large enough
            usd_value = trade_data.size * trade_data.price

            # Skip tiny trades
            if usd_value < 100:
                return

            # Log large trades
            if usd_value >= 1000:
                logger.info(
                    f"Large trade: ${usd_value:,.0f} by {wallet[:10]}... "
                    f"({trade_data.side} @ {trade_data.price:.2%})"
                )

            # Get wallet stats
            wallet_stats = await self._get_or_create_wallet_stats(wallet)

            # Create trade object for scoring
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

        async with self.db.session() as session:
            stmt = select(WalletStats).where(WalletStats.wallet == wallet)
            result = await session.execute(stmt)
            stats = result.scalar_one_or_none()

            if not stats:
                stats = WalletStats(
                    wallet=wallet,
                    total_trades=0,
                    tier=WalletTier.UNKNOWN,
                )
                session.add(stats)

            return stats

    async def _periodic_report(self):
        """Print status report every 5 minutes"""
        while self._running:
            await asyncio.sleep(300)

            runtime = (datetime.utcnow() - self._start_time).total_seconds() / 3600
            stats = self.paper_trader.get_stats()

            logger.info(
                f"\n--- STATUS ({runtime:.1f}h) ---\n"
                f"Trades processed: {self._trades_processed} | "
                f"Signals seen: {stats['signals_seen']} | "
                f"Passed: {stats['signals_passed']} | "
                f"Paper trades: {stats['trades_simulated']} | "
                f"P&L: ${stats['realized_pnl'] + stats['unrealized_pnl']:+.2f}\n"
            )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run AI Whale Watcher in paper trading mode (polling)"
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

    risk_limits = RiskLimits(
        max_per_trade_usd=args.max_trade,
        max_total_exposure_usd=args.max_total,
        min_copy_score=args.min_score,
        max_daily_loss_usd=args.max_daily_loss,
    )

    runner = PaperTradingPollingRunner(risk_limits)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("\nReceived shutdown signal...")
        runner._running = False

    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    try:
        await runner.start()
        await runner.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
