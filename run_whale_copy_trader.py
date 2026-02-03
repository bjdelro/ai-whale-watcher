#!/usr/bin/env python3
"""
Whale Copy Trader for AI Whale Watcher

Monitors top profitable Polymarket wallets and paper trades their moves.
Uses the Data API to track whale trades in real-time.

Strategy:
1. Monitor top 20 most profitable wallets from Polymarket leaderboard
2. When a whale makes a trade, evaluate if we should copy it
3. Paper trade with risk limits ($1/trade, $100 cap)

Usage:
    python run_whale_copy_trader.py
"""

import asyncio
import argparse
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Dict, List, Set
from dataclasses import dataclass, asdict

import aiohttp
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


@dataclass
class WhaleWallet:
    """A whale wallet we're tracking"""
    address: str
    name: str
    monthly_profit: float
    last_seen_trade_id: str = ""
    trades_copied: int = 0


@dataclass
class PaperTrade:
    """A simulated trade"""
    timestamp: str
    whale_address: str
    whale_name: str
    side: str
    outcome: str
    price: float
    whale_size: float
    our_size: float
    our_shares: float
    market_title: str
    condition_id: str
    asset_id: str
    tx_hash: str


# Top 20 profitable whales from Polymarket leaderboard (Feb 2026)
TOP_WHALES = [
    WhaleWallet("0x492442eab586f242b53bda933fd5de859c8a3782", "Multicolored-Self", 939609),
    WhaleWallet("0xd0b4c4c020abdc88ad9a884f999f3d8cff8ffed6", "MrSparklySimpsons", 882152),
    WhaleWallet("0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "beachboy4", 815937),
    WhaleWallet("0x96489abcb9f583d6835c8ef95ffc923d05a86825", "anoin123", 771031),
    WhaleWallet("0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a", "bossoskil1", 653491),
    WhaleWallet("0x9976874011b081e1e408444c579f48aa5b5967da", "BWArmageddon", 520868),
    WhaleWallet("0xdc876e6873772d38716fda7f2452a78d426d7ab6", "432614799197", 443001),
    WhaleWallet("0xd25c72ac0928385610611c8148803dc717334d20", "FeatherLeather", 420638),
    WhaleWallet("0x03e8a544e97eeff5753bc1e90d46e5ef22af1697", "weflyhigh", 291172),
    WhaleWallet("0xf208326de73e12994c0cd2b641dddc74a319fa74", "BreezeScout", 267731),
    WhaleWallet("0x2537fa3357f0e42fa283b8d0338390dda0b6bff9", "herewego446", 259803),
    WhaleWallet("0xbddf61af533ff524d27154e589d2d7a81510c684", "Countryside", 248955),
    WhaleWallet("0xb8e6281d22dc80e08885ebc7d819da9bf8cdd504", "ball52759", 233604),
    WhaleWallet("0xaa075924e1dc7cff3b9fab67401126338c4d2125", "rustin", 210746),
    WhaleWallet("0xafbacaeeda63f31202759eff7f8126e49adfe61b", "SammySledge", 186988),
    WhaleWallet("0x3b5c629f114098b0dee345fb78b7a3a013c7126e", "SMCAOMCRL", 162499),
    WhaleWallet("0x58776759ee5c70a915138706a1308add8bc5d894", "Marktakh", 154969),
    WhaleWallet("0xee613b3fc183ee44f9da9c05f53e2da107e3debf", "sovereign2013", 150160),
    WhaleWallet("0x1455445e9a775cfa3fe9fc4b02bb4d2f682ae5cd", "c4c4", 132236),
    WhaleWallet("0x090a0d3fc9d68d3e16db70e3460e3e4b510801b4", "slight-", 131751),
]


class WhaleCopyTrader:
    """
    Monitors whale wallets and copies their trades.
    Also detects "unusual activity" - small wallets making large trades.

    Uses the Polymarket Data API to fetch recent trades for each whale,
    then simulates copying profitable-looking trades.
    """

    DATA_API_BASE = "https://data-api.polymarket.com"
    POLL_INTERVAL = 15  # seconds between polls
    MIN_WHALE_TRADE_SIZE = 100  # Only copy trades >= $100 from known whales

    # Unusual activity detection thresholds
    UNUSUAL_TRADE_SIZE = 500  # Flag trades >= $500 from unknown wallets
    UNUSUAL_RATIO = 5.0  # Flag if trade is 5x larger than wallet's average

    # Market timing filters
    AVOID_EXTREME_PRICES = True  # Skip markets at >95% or <5%
    EXTREME_PRICE_THRESHOLD = 0.05  # 5% threshold for extreme

    # Cluster detection settings
    CLUSTER_WINDOW_SECONDS = 300  # 5 minute window for cluster detection
    CLUSTER_MIN_WALLETS = 3  # Minimum wallets betting same direction to trigger cluster signal
    CLUSTER_MIN_VOLUME = 1000  # Minimum combined volume for cluster signal

    def __init__(
        self,
        max_per_trade: float = 1.0,
        max_total_exposure: float = 100.0,
    ):
        self.max_per_trade = max_per_trade
        self.max_total_exposure = max_total_exposure

        # Track whales
        self.whales = {w.address.lower(): w for w in TOP_WHALES}

        # State
        self._running = False
        self._session: aiohttp.ClientSession = None
        self._seen_tx_hashes: Set[str] = set()
        self._paper_trades: List[PaperTrade] = []
        self._start_time = datetime.utcnow()
        self._polls_completed = 0
        self._total_exposure = 0.0

        # Price tracking for P&L
        self._entry_prices: Dict[str, float] = {}  # asset_id -> entry_price
        self._current_prices: Dict[str, float] = {}  # asset_id -> current_price

        # Track wallet history for unusual activity detection
        self._wallet_history: Dict[str, List[float]] = {}  # wallet -> list of trade sizes
        self._unusual_activity_count = 0

        # Cluster detection: track recent trades by market
        # Format: {condition_id: [(timestamp, wallet, side, value), ...]}
        self._recent_market_trades: Dict[str, List[tuple]] = {}
        self._cluster_signals = 0

    async def start(self):
        """Initialize and start the copy trader"""
        logger.info("=" * 60)
        logger.info("🐋 WHALE COPY TRADER")
        logger.info("=" * 60)
        logger.info(f"Tracking {len(self.whales)} whale wallets")
        logger.info(f"Max per trade: ${self.max_per_trade:.2f}")
        logger.info(f"Max exposure: ${self.max_total_exposure:.2f}")
        logger.info(f"Min whale trade size: ${self.MIN_WHALE_TRADE_SIZE}")
        logger.info("=" * 60)

        # Log whale names
        logger.info("🎯 Whales being tracked:")
        for i, whale in enumerate(TOP_WHALES[:10], 1):
            logger.info(f"   {i}. {whale.name} (${whale.monthly_profit:,.0f}/mo)")
        logger.info(f"   ... and {len(TOP_WHALES) - 10} more")
        logger.info("=" * 60)

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._running = True

        logger.info("Starting whale monitoring...")

    async def run(self):
        """Main loop - poll for whale trades"""
        report_task = asyncio.create_task(self._periodic_report())

        try:
            while self._running:
                await self._poll_whale_trades()
                await asyncio.sleep(self.POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Shutting down...")
        finally:
            report_task.cancel()
            await self.stop()

    async def stop(self):
        """Clean shutdown"""
        self._running = False

        if self._session:
            await self._session.close()

        # Print final report
        self._print_final_report()

    async def _poll_whale_trades(self):
        """Poll recent trades for all tracked whales AND scan for unusual activity"""
        self._polls_completed += 1
        new_trades_found = 0
        unusual_found = 0

        # 1. Check known whale wallets
        for address, whale in self.whales.items():
            try:
                trades = await self._fetch_whale_trades(address, limit=10)

                for trade in trades:
                    tx_hash = trade.get("transactionHash", "")

                    # Skip if we've seen this trade
                    if tx_hash in self._seen_tx_hashes:
                        continue

                    self._seen_tx_hashes.add(tx_hash)
                    new_trades_found += 1

                    # Evaluate and potentially copy the trade
                    await self._evaluate_trade(whale, trade)

                # Small delay between wallets to avoid rate limits
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.warning(f"Error polling {whale.name}: {e}")

        # 2. Scan ALL recent trades for unusual activity (small wallets making big moves)
        try:
            unusual_found = await self._scan_for_unusual_activity()
        except Exception as e:
            logger.warning(f"Error scanning unusual activity: {e}")

        # 3. Check for cluster signals (multiple wallets betting same direction)
        try:
            await self._check_cluster_signals()
        except Exception as e:
            logger.warning(f"Error checking clusters: {e}")

        # Periodic status
        if self._polls_completed % 4 == 0:  # Every minute
            logger.info(
                f"Poll #{self._polls_completed}: "
                f"Whales: {new_trades_found} trades | "
                f"Unusual: {self._unusual_activity_count} detected | "
                f"Copies: {len(self._paper_trades)}"
            )

        # Cleanup old tx hashes (keep last 10000)
        if len(self._seen_tx_hashes) > 10000:
            self._seen_tx_hashes = set(list(self._seen_tx_hashes)[-5000:])

    async def _scan_for_unusual_activity(self) -> int:
        """
        Scan recent trades for unusual activity:
        - Small/unknown wallets making large trades ($500+)
        - Wallets making trades much larger than their average

        This catches potential "insider" activity before it becomes known.
        """
        unusual_count = 0

        try:
            # Fetch recent trades across ALL wallets
            url = f"{self.DATA_API_BASE}/trades"
            params = {"limit": 50}  # Get last 50 trades

            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return 0
                trades = await resp.json()

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")

                # Skip if we've seen this trade
                if tx_hash in self._seen_tx_hashes:
                    continue

                self._seen_tx_hashes.add(tx_hash)

                wallet = trade.get("proxyWallet", "").lower()
                size = trade.get("size", 0)
                price = trade.get("price", 0)
                trade_value = size * price

                # Skip if it's a known whale (we already track them)
                if wallet in self.whales:
                    continue

                # Track this wallet's trade history
                if wallet not in self._wallet_history:
                    self._wallet_history[wallet] = []

                history = self._wallet_history[wallet]

                # Calculate average trade size for this wallet
                avg_size = sum(history) / len(history) if history else 0

                # Check for unusual activity
                is_unusual = False
                reason = ""

                # Condition 1: Large trade from unknown wallet
                if trade_value >= self.UNUSUAL_TRADE_SIZE and len(history) < 5:
                    is_unusual = True
                    reason = f"Large trade (${trade_value:,.0f}) from new wallet (only {len(history)} prior trades)"

                # Condition 2: Trade is much larger than wallet's average
                elif avg_size > 0 and trade_value >= avg_size * self.UNUSUAL_RATIO:
                    is_unusual = True
                    reason = f"Trade ${trade_value:,.0f} is {trade_value/avg_size:.1f}x larger than avg (${avg_size:.0f})"

                # Update history
                history.append(trade_value)
                if len(history) > 20:  # Keep last 20 trades
                    history.pop(0)

                if is_unusual:
                    unusual_count += 1
                    self._unusual_activity_count += 1

                    title = trade.get("title", "Unknown")
                    side = trade.get("side", "")
                    outcome = trade.get("outcome", "")
                    name = trade.get("name", wallet[:12])

                    logger.info(
                        f"🚨 UNUSUAL ACTIVITY: {name} "
                        f"{side} ${trade_value:,.0f} of {outcome} "
                        f"- {title[:40]}..."
                    )
                    logger.info(f"   Reason: {reason}")

                    # Copy the unusual trade!
                    await self._copy_unusual_trade(trade, reason)

        except Exception as e:
            logger.warning(f"Error in unusual activity scan: {e}")

        # Cleanup old wallet history (keep last 1000 wallets)
        if len(self._wallet_history) > 1000:
            # Keep wallets with most recent activity
            sorted_wallets = sorted(
                self._wallet_history.items(),
                key=lambda x: len(x[1]),
                reverse=True
            )
            self._wallet_history = dict(sorted_wallets[:500])

        return unusual_count

    async def _copy_unusual_trade(self, trade: dict, reason: str):
        """Copy an unusual activity trade"""
        # Check exposure limits
        if self._total_exposure >= self.max_total_exposure:
            logger.info(f"   ⚠️ Max exposure reached, skipping")
            return

        side = trade.get("side", "")
        price = trade.get("price", 0)
        title = trade.get("title", "Unknown")
        outcome = trade.get("outcome", "")
        condition_id = trade.get("conditionId", "")
        asset_id = trade.get("asset", "")
        tx_hash = trade.get("transactionHash", "")
        whale_size = trade.get("size", 0)
        wallet = trade.get("proxyWallet", "")
        name = trade.get("name", wallet[:12])

        # Calculate our position size
        our_size_usd = min(self.max_per_trade, self.max_total_exposure - self._total_exposure)
        our_shares = our_size_usd / price if price > 0 else 0

        # Create paper trade record
        paper_trade = PaperTrade(
            timestamp=datetime.utcnow().isoformat(),
            whale_address=wallet,
            whale_name=f"UNUSUAL:{name}",
            side=side,
            outcome=outcome,
            price=price,
            whale_size=whale_size,
            our_size=our_size_usd,
            our_shares=our_shares,
            market_title=title,
            condition_id=condition_id,
            asset_id=asset_id,
            tx_hash=tx_hash,
        )

        self._paper_trades.append(paper_trade)
        self._total_exposure += our_size_usd
        self._entry_prices[asset_id] = price

        logger.info(
            f"   📝 COPIED UNUSUAL: {side} ${our_size_usd:.2f} of {outcome} @ {price:.1%}"
        )

        # Save to file
        self._save_trade(paper_trade)

        # Record for cluster detection
        self._record_trade_for_cluster(condition_id, wallet, side, whale_size * price)

    def _record_trade_for_cluster(self, condition_id: str, wallet: str, side: str, value: float):
        """Record a trade for cluster detection"""
        import time
        now = time.time()

        if condition_id not in self._recent_market_trades:
            self._recent_market_trades[condition_id] = []

        self._recent_market_trades[condition_id].append((now, wallet, side, value))

        # Cleanup old trades (older than cluster window)
        cutoff = now - self.CLUSTER_WINDOW_SECONDS
        self._recent_market_trades[condition_id] = [
            t for t in self._recent_market_trades[condition_id] if t[0] > cutoff
        ]

    async def _check_cluster_signals(self):
        """
        Detect cluster signals: multiple wallets betting the same direction
        on the same market within a short time window.

        This often indicates shared information or coordinated trading.
        """
        import time
        now = time.time()
        cutoff = now - self.CLUSTER_WINDOW_SECONDS

        for condition_id, trades in list(self._recent_market_trades.items()):
            # Filter to recent trades
            recent = [t for t in trades if t[0] > cutoff]
            if len(recent) < self.CLUSTER_MIN_WALLETS:
                continue

            # Group by side
            buys = [t for t in recent if t[2] == "BUY"]
            sells = [t for t in recent if t[2] == "SELL"]

            # Check for buy cluster
            if len(buys) >= self.CLUSTER_MIN_WALLETS:
                unique_wallets = len(set(t[1] for t in buys))
                total_volume = sum(t[3] for t in buys)

                if unique_wallets >= self.CLUSTER_MIN_WALLETS and total_volume >= self.CLUSTER_MIN_VOLUME:
                    self._cluster_signals += 1
                    logger.info(
                        f"🎯 CLUSTER SIGNAL: {unique_wallets} wallets BUY on same market "
                        f"(${total_volume:,.0f} total) in last {self.CLUSTER_WINDOW_SECONDS}s"
                    )
                    # We don't auto-copy clusters yet, just log them
                    # Could add await self._copy_cluster_signal(condition_id, "BUY", ...) here

            # Check for sell cluster
            if len(sells) >= self.CLUSTER_MIN_WALLETS:
                unique_wallets = len(set(t[1] for t in sells))
                total_volume = sum(t[3] for t in sells)

                if unique_wallets >= self.CLUSTER_MIN_WALLETS and total_volume >= self.CLUSTER_MIN_VOLUME:
                    self._cluster_signals += 1
                    logger.info(
                        f"🎯 CLUSTER SIGNAL: {unique_wallets} wallets SELL on same market "
                        f"(${total_volume:,.0f} total) in last {self.CLUSTER_WINDOW_SECONDS}s"
                    )

        # Cleanup old market entries
        if len(self._recent_market_trades) > 500:
            # Keep only markets with recent activity
            self._recent_market_trades = {
                k: v for k, v in self._recent_market_trades.items()
                if v and v[-1][0] > cutoff
            }

    async def _fetch_whale_trades(self, address: str, limit: int = 10) -> List[dict]:
        """Fetch recent trades for a whale wallet"""
        url = f"{self.DATA_API_BASE}/trades"
        params = {"user": address, "limit": limit}

        async with self._session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            return await resp.json()

    async def _evaluate_trade(self, whale: WhaleWallet, trade: dict):
        """Evaluate a whale trade and decide if we should copy it"""
        side = trade.get("side", "")
        size = trade.get("size", 0)
        price = trade.get("price", 0)
        title = trade.get("title", "Unknown")
        outcome = trade.get("outcome", "")
        condition_id = trade.get("conditionId", "")
        asset_id = trade.get("asset", "")
        tx_hash = trade.get("transactionHash", "")

        trade_value = size * price

        # Skip small trades
        if trade_value < self.MIN_WHALE_TRADE_SIZE:
            return

        # Log the whale trade
        logger.info(
            f"🐋 WHALE TRADE: {whale.name} "
            f"{side} ${trade_value:,.0f} of {outcome} @ {price:.1%} "
            f"- {title[:50]}..."
        )

        # FILTER 1: Skip extreme prices (already decided markets)
        if self.AVOID_EXTREME_PRICES:
            if price < self.EXTREME_PRICE_THRESHOLD or price > (1 - self.EXTREME_PRICE_THRESHOLD):
                logger.info(f"   ⏭️ Skipping: extreme price ({price:.1%}) - market likely decided")
                return

        # FILTER 2: Check if we have room for more exposure
        if self._total_exposure >= self.max_total_exposure:
            logger.info(f"   ⚠️ Max exposure reached (${self._total_exposure:.2f}), skipping")
            return

        # Record for cluster detection before copying
        self._record_trade_for_cluster(condition_id, whale.address, side, trade_value)

        # Copy the trade!
        await self._copy_trade(whale, trade)

    async def _copy_trade(self, whale: WhaleWallet, trade: dict):
        """Execute a paper copy of the whale's trade"""
        side = trade.get("side", "")
        price = trade.get("price", 0)
        title = trade.get("title", "Unknown")
        outcome = trade.get("outcome", "")
        condition_id = trade.get("conditionId", "")
        asset_id = trade.get("asset", "")
        tx_hash = trade.get("transactionHash", "")
        whale_size = trade.get("size", 0)

        # Calculate our position size
        our_size_usd = min(self.max_per_trade, self.max_total_exposure - self._total_exposure)
        our_shares = our_size_usd / price if price > 0 else 0

        # Create paper trade record
        paper_trade = PaperTrade(
            timestamp=datetime.utcnow().isoformat(),
            whale_address=whale.address,
            whale_name=whale.name,
            side=side,
            outcome=outcome,
            price=price,
            whale_size=whale_size,
            our_size=our_size_usd,
            our_shares=our_shares,
            market_title=title,
            condition_id=condition_id,
            asset_id=asset_id,
            tx_hash=tx_hash,
        )

        self._paper_trades.append(paper_trade)
        self._total_exposure += our_size_usd
        self._entry_prices[asset_id] = price
        whale.trades_copied += 1

        logger.info(
            f"   📝 COPIED: {side} ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
            f"({our_shares:.2f} shares)"
        )

        # Save to file
        self._save_trade(paper_trade)

    def _save_trade(self, trade: PaperTrade):
        """Save trade to JSONL file"""
        os.makedirs("paper_trades", exist_ok=True)
        filename = f"paper_trades/whale_copies_{datetime.utcnow().strftime('%Y%m%d')}.jsonl"

        with open(filename, "a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")

    async def _periodic_report(self):
        """Print status report every 5 minutes"""
        while self._running:
            await asyncio.sleep(300)

            runtime = (datetime.utcnow() - self._start_time).total_seconds() / 3600

            # Calculate P&L (simplified - just track entry prices)
            total_pnl = 0.0
            winners = 0
            losers = 0

            for trade in self._paper_trades:
                entry = trade.price
                current = self._current_prices.get(trade.asset_id, entry)

                if trade.side == "BUY":
                    pnl_pct = (current - entry) / entry if entry > 0 else 0
                else:
                    pnl_pct = (entry - current) / entry if entry > 0 else 0

                pnl_usd = trade.our_size * pnl_pct
                total_pnl += pnl_usd

                if pnl_usd > 0:
                    winners += 1
                elif pnl_usd < 0:
                    losers += 1

            win_rate = (winners / len(self._paper_trades) * 100) if self._paper_trades else 0
            return_pct = (total_pnl / self._total_exposure * 100) if self._total_exposure > 0 else 0

            # Projections
            hourly_return = total_pnl / runtime if runtime > 0 else 0
            daily_projected = hourly_return * 24

            # Count whale vs unusual copies
            whale_copies = len([t for t in self._paper_trades if not t.whale_name.startswith("UNUSUAL:")])
            unusual_copies = len([t for t in self._paper_trades if t.whale_name.startswith("UNUSUAL:")])

            logger.info(
                f"\n{'='*60}\n"
                f"🐋 WHALE COPY TRADING REPORT ({runtime:.1f}h runtime)\n"
                f"{'='*60}\n"
                f"Polls: {self._polls_completed} | Wallets tracked: {len(self._wallet_history)}\n"
                f"Whale copies: {whale_copies} | Unusual copies: {unusual_copies}\n"
                f"Cluster signals detected: {self._cluster_signals}\n"
                f"{'='*60}\n"
                f"💰 P&L SUMMARY\n"
                f"   Total Exposure: ${self._total_exposure:.2f}\n"
                f"   Total P&L: ${total_pnl:+.2f} ({return_pct:+.1f}%)\n"
                f"   Winners: {winners} | Losers: {losers} | Win Rate: {win_rate:.1f}%\n"
                f"{'='*60}\n"
                f"📈 PROJECTIONS\n"
                f"   Hourly:  ${hourly_return:+.2f}/hr\n"
                f"   Daily:   ${daily_projected:+.2f}/day\n"
                f"{'='*60}"
            )

            # Show which whales we've copied
            copied_whales = [(w.name, w.trades_copied) for w in self.whales.values() if w.trades_copied > 0]
            if copied_whales:
                copied_whales.sort(key=lambda x: x[1], reverse=True)
                logger.info("🎯 WHALES COPIED:")
                for name, count in copied_whales[:5]:
                    logger.info(f"   {name}: {count} trades")
                logger.info(f"{'='*60}\n")

    def _print_final_report(self):
        """Print final summary when shutting down"""
        runtime = (datetime.utcnow() - self._start_time).total_seconds() / 3600

        logger.info(
            f"\n{'='*60}\n"
            f"🐋 FINAL WHALE COPY TRADING REPORT\n"
            f"{'='*60}\n"
            f"Runtime: {runtime:.2f} hours\n"
            f"Total trades copied: {len(self._paper_trades)}\n"
            f"Total exposure: ${self._total_exposure:.2f}\n"
            f"{'='*60}"
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Whale Copy Trader")
    parser.add_argument("--max-trade", type=float, default=1.0, help="Max $ per trade")
    parser.add_argument("--max-total", type=float, default=100.0, help="Max total exposure")
    return parser.parse_args()


async def main():
    args = parse_args()

    trader = WhaleCopyTrader(
        max_per_trade=args.max_trade,
        max_total_exposure=args.max_total,
    )

    # Handle shutdown signals
    import signal
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("\nReceived shutdown signal...")
        trader._running = False

    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    try:
        await trader.start()
        await trader.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
