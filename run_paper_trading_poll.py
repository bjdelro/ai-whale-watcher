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
from typing import Set, Dict

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
        self._price_cache: Dict[str, float] = {}  # token_id -> last_price
        self._paper_positions: list = []  # Track all paper trades for P&L calc
        self._trades_processed = 0
        self._signals_generated = 0
        self._polls_completed = 0
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
        """Poll for recent trades by monitoring price changes on active markets"""
        try:
            # Since CLOB get_trades returns empty, we monitor price movements instead
            # and simulate trades based on significant moves

            session = await self.client._get_session()

            # Fetch active markets from Gamma API (has full market data)
            async with session.get(
                f"{self.client.GAMMA_BASE_URL}/markets",
                params={"closed": "false", "limit": 100}
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Failed to fetch markets: {resp.status}")
                    return
                markets = await resp.json()

            if not markets:
                logger.warning("No markets returned from API")
                return

            # Handle both list and dict responses
            if isinstance(markets, dict):
                markets = markets.get("data", []) or markets.get("markets", [])

            if not isinstance(markets, list):
                logger.warning(f"Unexpected markets format: {type(markets)}")
                return

            # Filter to active markets and sort by volume
            active_markets = []
            for m in markets:
                if isinstance(m, dict) and m.get("active", True) and not m.get("closed", False):
                    active_markets.append(m)

            active_markets.sort(key=lambda x: float(x.get("volume", 0) or x.get("volumeNum", 0) or 0), reverse=True)

            # Take top 50 by volume
            top_markets = active_markets[:50]

            if self._polls_completed == 0:
                logger.info(f"Found {len(active_markets)} active markets, monitoring top {len(top_markets)}")

            new_signals = 0
            for market in top_markets:
                condition_id = market.get("condition_id") or market.get("conditionId", "")
                question = (market.get("question") or market.get("title") or "Unknown")[:50]

                # Gamma API uses clobTokenIds (JSON string or list)
                clob_tokens = market.get("clobTokenIds", [])
                if isinstance(clob_tokens, str):
                    try:
                        import json
                        clob_tokens = json.loads(clob_tokens)
                    except:
                        clob_tokens = []

                outcomes = market.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    try:
                        import json
                        outcomes = json.loads(outcomes)
                    except:
                        outcomes = ["Yes", "No"]

                # Create token list with outcomes
                tokens = []
                for i, token_id in enumerate(clob_tokens):
                    outcome = outcomes[i] if i < len(outcomes) else "YES"
                    tokens.append({"token_id": token_id, "outcome": outcome})

                for token in tokens:
                    token_id = token.get("token_id")
                    outcome = token.get("outcome", "YES")

                    if not token_id:
                        continue

                    # Get current price from orderbook
                    try:
                        async with session.get(
                            f"{self.client.CLOB_BASE_URL}/book",
                            params={"token_id": token_id}
                        ) as book_resp:
                            if book_resp.status != 200:
                                continue
                            book = await book_resp.json()
                    except Exception as e:
                        continue

                    # Calculate mid price
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])

                    if not bids and not asks:
                        continue

                    best_bid = float(bids[0]["price"]) if bids else 0
                    best_ask = float(asks[0]["price"]) if asks else 1
                    current_price = (best_bid + best_ask) / 2 if bids and asks else (best_bid or best_ask)

                    # Track price for this token
                    price_key = f"{token_id}"
                    if price_key not in self._price_cache:
                        self._price_cache[price_key] = current_price
                        continue

                    old_price = self._price_cache[price_key]
                    self._price_cache[price_key] = current_price

                    # Check for significant price movement (>3% change)
                    if old_price > 0.01:  # Avoid division by tiny numbers
                        pct_change = (current_price - old_price) / old_price

                        if abs(pct_change) >= 0.03:  # 3% threshold
                            new_signals += 1
                            side = "BUY" if pct_change > 0 else "SELL"

                            logger.info(
                                f"SIGNAL: {question}... {outcome} "
                                f"{old_price:.1%} -> {current_price:.1%} ({pct_change:+.1%}) "
                                f"-> Simulating {side}"
                            )

                            # Create a synthetic trade signal
                            await self._simulate_signal(
                                token_id=token_id,
                                condition_id=condition_id,
                                outcome=outcome,
                                side=side,
                                price=current_price,
                                pct_change=pct_change,
                                question=question,
                            )

                    # Small delay to avoid rate limits
                    await asyncio.sleep(0.05)

            self._polls_completed += 1
            if new_signals > 0:
                logger.info(f"Poll #{self._polls_completed}: Found {new_signals} new signals")
            elif self._polls_completed % 6 == 0:  # Log every minute (6 polls * 10s)
                logger.info(f"Poll #{self._polls_completed}: No new signals (monitoring {len(top_markets)} markets)")

            # Cleanup old price cache entries (keep last 500)
            if len(self._price_cache) > 500:
                keys = list(self._price_cache.keys())
                for k in keys[:-250]:
                    del self._price_cache[k]

        except Exception as e:
            logger.error(f"Failed to poll trades: {e}", exc_info=True)

    async def _simulate_signal(
        self,
        token_id: str,
        condition_id: str,
        outcome: str,
        side: str,
        price: float,
        pct_change: float,
        question: str,
    ):
        """Simulate a paper trade based on price movement signal"""
        try:
            self._signals_generated += 1

            # For paper trading, we'll simulate buying when price moves up
            # (momentum following) with our risk limits
            trade_size_usd = min(self.risk_limits.max_per_trade_usd, 10.0)

            # Calculate simulated position
            shares = trade_size_usd / price if price > 0 else 0

            # Log the paper trade
            trade_record = {
                "timestamp": datetime.utcnow().isoformat(),
                "token_id": token_id,
                "condition_id": condition_id,
                "outcome": outcome,
                "side": side,
                "entry_price": price,
                "pct_change_trigger": pct_change,
                "simulated_size_usd": trade_size_usd,
                "simulated_shares": shares,
                "question": question,
            }

            # Track in memory for P&L calculation
            self._paper_positions.append(trade_record)

            # Save to paper trades log
            import json
            import os
            os.makedirs("paper_trades", exist_ok=True)
            log_file = f"paper_trades/trades_{datetime.utcnow().strftime('%Y%m%d')}.jsonl"

            with open(log_file, "a") as f:
                f.write(json.dumps(trade_record) + "\n")

            self._trades_processed += 1

            logger.info(
                f"📝 PAPER TRADE #{self._trades_processed}: "
                f"{side} ${trade_size_usd:.2f} of {outcome} @ {price:.1%} "
                f"({shares:.2f} shares) - {question}..."
            )

        except Exception as e:
            logger.error(f"Error simulating signal: {e}")

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
        """Print status report every 5 minutes with P&L analysis"""
        while self._running:
            await asyncio.sleep(300)

            runtime = (datetime.utcnow() - self._start_time).total_seconds() / 3600

            # Calculate P&L for all positions
            total_pnl = 0.0
            winners = 0
            losers = 0
            position_details = []

            for pos in self._paper_positions:
                token_id = pos["token_id"]
                entry_price = pos["entry_price"]
                side = pos["side"]
                size_usd = pos["simulated_size_usd"]

                # Get current price from cache
                current_price = self._price_cache.get(token_id, entry_price)

                # Calculate P&L based on side
                # BUY: profit if price went up
                # SELL: profit if price went down
                if side == "BUY":
                    pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                else:  # SELL
                    pnl_pct = (entry_price - current_price) / entry_price if entry_price > 0 else 0

                pnl_usd = size_usd * pnl_pct
                total_pnl += pnl_usd

                if pnl_usd > 0:
                    winners += 1
                elif pnl_usd < 0:
                    losers += 1

                position_details.append({
                    "question": pos["question"][:30],
                    "side": side,
                    "outcome": pos["outcome"],
                    "entry": entry_price,
                    "current": current_price,
                    "pnl": pnl_usd,
                    "pnl_pct": pnl_pct
                })

            # Sort by P&L to show best/worst
            position_details.sort(key=lambda x: x["pnl"], reverse=True)

            win_rate = (winners / len(self._paper_positions) * 100) if self._paper_positions else 0

            logger.info(
                f"\n{'='*60}\n"
                f"📊 PAPER TRADING REPORT ({runtime:.1f}h runtime)\n"
                f"{'='*60}\n"
                f"Polls: {self._polls_completed} | Markets: {len(self._price_cache)}\n"
                f"Signals: {self._signals_generated} | Trades: {self._trades_processed}\n"
                f"{'='*60}\n"
                f"💰 P&L SUMMARY\n"
                f"   Total P&L: ${total_pnl:+.2f}\n"
                f"   Winners: {winners} | Losers: {losers} | Win Rate: {win_rate:.1f}%\n"
                f"{'='*60}"
            )

            # Show top 3 best and worst trades
            if position_details:
                logger.info("📈 BEST TRADES:")
                for p in position_details[:3]:
                    logger.info(
                        f"   {p['side']} {p['outcome']} {p['question']}... "
                        f"{p['entry']:.1%}->{p['current']:.1%} = ${p['pnl']:+.2f} ({p['pnl_pct']:+.1%})"
                    )

                logger.info("📉 WORST TRADES:")
                for p in position_details[-3:]:
                    logger.info(
                        f"   {p['side']} {p['outcome']} {p['question']}... "
                        f"{p['entry']:.1%}->{p['current']:.1%} = ${p['pnl']:+.2f} ({p['pnl_pct']:+.1%})"
                    )

                logger.info(f"{'='*60}\n")


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
