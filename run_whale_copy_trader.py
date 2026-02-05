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
from typing import Dict, List, Set, Optional
from dataclasses import dataclass, asdict

import aiohttp
from dotenv import load_dotenv

# Arbitrage scanning
from src.arbitrage import IntraMarketArbitrage, ArbitrageOpportunity

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


@dataclass
class CopiedPosition:
    """
    A position we're tracking (linked to the whale we copied).
    This enables exit logic - when the whale sells, we sell too.
    """
    position_id: str
    market_id: str  # condition_id
    token_id: str   # asset_id
    outcome: str    # "Yes" or "No"
    whale_address: str  # Which whale we copied
    whale_name: str
    entry_price: float
    shares: float
    entry_time: str
    copy_amount_usd: float
    market_title: str
    status: str = "open"  # "open" | "closed"
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None  # "whale_sold" | "resolved" | "manual"
    pnl: Optional[float] = None


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
    UNUSUAL_TRADE_SIZE = 1000  # Flag trades >= $1000 from unknown wallets
    UNUSUAL_RATIO = 5.0  # Flag if trade is 5x larger than wallet's average
    UNUSUAL_MIN_TRADE = 500  # Minimum trade size to even consider for unusual activity
    UNUSUAL_MIN_AVG = 100  # Wallet must have avg trade size >= $100 for ratio comparison

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

        # Track recent trades by wallet+market to detect hedging (buying both sides)
        # Format: {(wallet, condition_id): [(timestamp, outcome, value), ...]}
        self._wallet_market_trades: Dict[tuple, List[tuple]] = {}

        # Track NET positions per wallet+market for smart hedging
        # Format: {(wallet, condition_id): {outcome: net_value, ...}}
        self._wallet_net_positions: Dict[tuple, Dict[str, float]] = {}

        # Arbitrage scanner
        self._arbitrage_scanner: Optional[IntraMarketArbitrage] = None
        self._arbitrage_opportunities: List[ArbitrageOpportunity] = []
        self._last_arbitrage_scan = datetime.min
        self._arbitrage_scan_interval = 60  # Scan for arbitrage every 60 seconds
        self._arbitrage_found_count = 0

        # === EXIT LOGIC: Position tracking ===
        # Track copied positions with whale association for exit logic
        self._copied_positions: Dict[str, CopiedPosition] = {}
        self._position_counter = 0  # For generating unique position IDs

        # Market resolution tracking
        self._last_resolution_check = datetime.min
        self._resolution_check_interval = 60  # Check for resolutions every 60 seconds
        self._markets_checked: Set[str] = set()  # Track which markets we've fetched details for
        self._market_cache: Dict[str, dict] = {}  # Cache market details

        # P&L tracking
        self._realized_pnl = 0.0
        self._positions_closed = 0
        self._positions_won = 0
        self._positions_lost = 0

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

        # Initialize arbitrage scanner with shared session
        self._arbitrage_scanner = IntraMarketArbitrage(session=self._session)

        logger.info("Starting whale monitoring + arbitrage scanning...")

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
        sell_signals_found = 0

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

                    # === EXIT LOGIC: Check if this is a SELL from a whale we copied ===
                    sell_signal = self._check_for_whale_sells(trade)
                    if sell_signal:
                        sell_signals_found += 1
                        logger.info(
                            f"🐋 WHALE SELLING: {whale.name} exiting position in "
                            f"{sell_signal['position'].market_title[:40]}..."
                        )
                        await self._execute_copy_sell(sell_signal)
                        continue  # Don't also try to copy this as a new trade

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

        # 4. Scan for intra-market arbitrage (risk-free profit!)
        try:
            await self._scan_for_arbitrage()
        except Exception as e:
            logger.warning(f"Error scanning arbitrage: {e}")

        # 5. === EXIT LOGIC: Check for market resolutions ===
        try:
            await self._check_market_resolutions()
        except Exception as e:
            logger.warning(f"Error checking resolutions: {e}")

        # Periodic status
        if self._polls_completed % 4 == 0:  # Every minute
            open_positions = len([p for p in self._copied_positions.values() if p.status == "open"])
            closed_positions = len([p for p in self._copied_positions.values() if p.status == "closed"])
            logger.info(
                f"Poll #{self._polls_completed}: "
                f"Whales: {new_trades_found} trades | "
                f"Unusual: {self._unusual_activity_count} | "
                f"Positions: {open_positions} open, {closed_positions} closed | "
                f"P&L: ${self._realized_pnl:+.2f}"
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

                # Skip tiny trades entirely - not worth tracking
                if trade_value < self.UNUSUAL_MIN_TRADE:
                    # Still update history but don't flag
                    history.append(trade_value)
                    if len(history) > 20:
                        history.pop(0)
                    continue

                # Condition 1: Large trade from unknown/new wallet
                if trade_value >= self.UNUSUAL_TRADE_SIZE and len(history) < 5:
                    is_unusual = True
                    reason = f"Large trade (${trade_value:,.0f}) from new wallet (only {len(history)} prior trades)"

                # Condition 2: Trade is much larger than wallet's average
                # Only trigger if avg is meaningful (>= $100) and trade is >= $500
                elif avg_size >= self.UNUSUAL_MIN_AVG and trade_value >= avg_size * self.UNUSUAL_RATIO:
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
        wallet = trade.get("proxyWallet", "").lower()
        condition_id = trade.get("conditionId", "")
        outcome = trade.get("outcome", "")
        price = trade.get("price", 0)
        size = trade.get("size", 0)
        trade_value = size * price

        # Check extreme prices
        if self.AVOID_EXTREME_PRICES:
            if price < self.EXTREME_PRICE_THRESHOLD or price > (1 - self.EXTREME_PRICE_THRESHOLD):
                logger.info(f"   ⏭️ Skipping: extreme price ({price:.1%})")
                return

        # Check exposure limits
        if self._total_exposure >= self.max_total_exposure:
            logger.info(f"   ⚠️ Max exposure reached, skipping")
            return

        # Smart hedge analysis
        is_hedge, net_direction, net_profit, recommendation = self._analyze_hedge(
            wallet, condition_id, outcome, trade_value, price
        )

        if recommendation in ('skip_small_hedge', 'skip_no_direction', 'skip_arbitrage'):
            logger.info(f"   ⏭️ Skipping: {recommendation.replace('_', ' ')}")
            self._record_wallet_market_trade(wallet, condition_id, outcome, trade_value, price)
            return

        # Record for position tracking
        self._record_wallet_market_trade(wallet, condition_id, outcome, trade_value, price)

        side = trade.get("side", "")
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

        # === EXIT LOGIC: Create tracked position for BUY trades ===
        if side == "BUY":
            position = self._create_position(
                whale_address=wallet,
                whale_name=f"UNUSUAL:{name}",
                trade=trade,
                our_size=our_size_usd,
                our_shares=our_shares,
            )
            logger.info(
                f"   📝 COPIED UNUSUAL BUY: ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                f"[Position: {position.position_id}]"
            )
        else:
            logger.info(
                f"   📝 COPIED UNUSUAL: {side} ${our_size_usd:.2f} of {outcome} @ {price:.1%}"
            )

        # Save to file
        self._save_trade(paper_trade)

        # Record for cluster detection
        self._record_trade_for_cluster(condition_id, wallet, side, whale_size * price)

    def _record_wallet_market_trade(self, wallet: str, condition_id: str, outcome: str, value: float, price: float):
        """Record a wallet's trade on a market and update net position with proper payout math"""
        import time
        now = time.time()
        key = (wallet.lower(), condition_id)

        # Calculate shares purchased (value / price = shares, each share pays $1 if wins)
        shares = value / price if price > 0 else 0

        # Record trade history with shares, not just dollars
        if key not in self._wallet_market_trades:
            self._wallet_market_trades[key] = []
        self._wallet_market_trades[key].append((now, outcome, value, shares, price))

        # Keep only trades from last 30 minutes (longer window for net position tracking)
        cutoff = now - 1800
        self._wallet_market_trades[key] = [
            t for t in self._wallet_market_trades[key] if t[0] > cutoff
        ]

        # Update net position (track SHARES not dollars - shares = potential payout)
        if key not in self._wallet_net_positions:
            self._wallet_net_positions[key] = {}

        if outcome not in self._wallet_net_positions[key]:
            self._wallet_net_positions[key][outcome] = {"shares": 0.0, "cost": 0.0}

        self._wallet_net_positions[key][outcome]["shares"] += shares
        self._wallet_net_positions[key][outcome]["cost"] += value

    def _get_net_position(self, wallet: str, condition_id: str) -> Dict[str, float]:
        """Get the net position for a wallet on a market"""
        key = (wallet.lower(), condition_id)
        return self._wallet_net_positions.get(key, {})

    def _analyze_hedge(self, wallet: str, condition_id: str, current_outcome: str, current_value: float, current_price: float) -> tuple:
        """
        Analyze if this trade is a hedge and what the net position is.

        Uses proper payout math:
        - Shares = dollars / price
        - If outcome wins, each share pays $1
        - Net profit = shares won - total cost

        Returns: (is_hedge, net_direction, net_payout, recommendation)
        - is_hedge: True if they've traded both sides
        - net_direction: The outcome where they profit most if it wins
        - net_payout: The potential profit if that outcome wins
        - recommendation: 'copy', 'skip_small_hedge', 'consider_hedge', 'skip_no_direction'
        """
        import time
        now = time.time()
        key = (wallet.lower(), condition_id)

        # Calculate shares for current trade
        current_shares = current_value / current_price if current_price > 0 else 0

        if key not in self._wallet_market_trades:
            return (False, current_outcome, current_value, 'copy')

        # Get all recent trades on this market
        cutoff = now - 1800  # 30 minute window
        recent_trades = [t for t in self._wallet_market_trades[key] if t[0] > cutoff]

        if not recent_trades:
            return (False, current_outcome, current_value, 'copy')

        # Calculate position per outcome: {outcome: {"shares": X, "cost": Y}}
        positions = {}
        total_cost = 0
        for _, outcome, value, shares, price in recent_trades:
            if outcome not in positions:
                positions[outcome] = {"shares": 0.0, "cost": 0.0}
            positions[outcome]["shares"] += shares
            positions[outcome]["cost"] += value
            total_cost += value

        # Add the current trade
        if current_outcome not in positions:
            positions[current_outcome] = {"shares": 0.0, "cost": 0.0}
        positions[current_outcome]["shares"] += current_shares
        positions[current_outcome]["cost"] += current_value
        total_cost += current_value

        # Check if they've traded multiple outcomes (hedging)
        outcomes_traded = [o for o, pos in positions.items() if pos["shares"] > 0]
        is_hedge = len(outcomes_traded) > 1

        if not is_hedge:
            # Pure directional bet
            return (False, current_outcome, current_value, 'copy')

        # Calculate profit/loss for each possible outcome
        # If outcome X wins: profit = shares_X * $1 - total_cost
        profits_by_outcome = {}
        for outcome, pos in positions.items():
            payout_if_wins = pos["shares"]  # Each share pays $1
            profit_if_wins = payout_if_wins - total_cost
            profits_by_outcome[outcome] = profit_if_wins

        # Find which outcome they profit most from (or lose least)
        net_direction = max(profits_by_outcome, key=profits_by_outcome.get)
        net_profit = profits_by_outcome[net_direction]

        # Calculate if they're guaranteed profit (arbitrage) or have directional exposure
        min_profit = min(profits_by_outcome.values())
        max_profit = max(profits_by_outcome.values())

        # If min_profit > 0, they've locked in guaranteed profit (arbitrage)
        if min_profit > 0:
            return (True, net_direction, net_profit, 'skip_arbitrage')

        # If profits are similar across outcomes, they're hedged with no clear direction
        profit_spread = max_profit - min_profit
        if profit_spread < total_cost * 0.2:  # Less than 20% spread
            return (True, net_direction, net_profit, 'skip_no_direction')

        # Determine if current trade is adding to their favored direction or hedging
        if current_outcome == net_direction:
            # They're adding conviction to their favored outcome - copy it
            return (True, net_direction, net_profit, 'copy')
        else:
            # They're hedging - check how much
            hedge_size = positions[current_outcome]["cost"]
            main_size = sum(p["cost"] for o, p in positions.items() if o != current_outcome)

            hedge_ratio = hedge_size / main_size if main_size > 0 else 1.0

            if hedge_ratio < 0.3:
                # Small hedge - skip it, main bet is the signal
                return (True, net_direction, net_profit, 'skip_small_hedge')
            elif hedge_ratio < 0.7:
                # Medium hedge - worth noting
                return (True, net_direction, net_profit, 'consider_hedge')
            else:
                # Large hedge
                return (True, net_direction, net_profit, 'skip_no_direction')

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

    async def _scan_for_arbitrage(self):
        """
        Scan for risk-free intra-market arbitrage opportunities.

        This runs less frequently (every 60s) since arbitrage opportunities
        are slower-moving than whale trades.
        """
        now = datetime.utcnow()

        # Only scan periodically (not every poll)
        if (now - self._last_arbitrage_scan).total_seconds() < self._arbitrage_scan_interval:
            return

        self._last_arbitrage_scan = now

        if not self._arbitrage_scanner:
            return

        # Scan binary markets (YES + NO < $1)
        binary_opps = await self._arbitrage_scanner.scan_binary_markets()

        for opp in binary_opps:
            self._arbitrage_found_count += 1
            self._arbitrage_opportunities.append(opp)

            logger.info(
                f"\n{'='*50}\n"
                f"ARBITRAGE OPPORTUNITY! (Binary)\n"
                f"{'='*50}\n"
                f"Market: {opp.market_title}\n"
                f"Outcomes: Yes=${opp.outcomes.get('Yes', 0):.4f} + No=${opp.outcomes.get('No', 0):.4f} = ${opp.total_cost:.4f}\n"
                f"Profit: ${opp.profit:.4f} ({opp.profit_pct:.2%})\n"
                f"24h Volume: ${opp.volume_24h:,.0f}\n"
                f"Action: Buy 1 share of Yes AND 1 share of No\n"
                f"{'='*50}"
            )

            # Paper trade the arbitrage
            await self._paper_trade_arbitrage(opp)

        # Scan multi-outcome markets (sum of all YES < $1)
        multi_opps = await self._arbitrage_scanner.scan_all_markets()

        for opp in multi_opps:
            # Skip if we already logged this one (from binary scan)
            if any(o.condition_id == opp.condition_id for o in binary_opps):
                continue

            self._arbitrage_found_count += 1
            self._arbitrage_opportunities.append(opp)

            outcomes_str = " + ".join([f"{k}=${v:.2f}" for k, v in list(opp.outcomes.items())[:5]])
            if len(opp.outcomes) > 5:
                outcomes_str += f" + {len(opp.outcomes)-5} more"

            logger.info(
                f"\n{'='*50}\n"
                f"ARBITRAGE OPPORTUNITY! (Multi-Outcome)\n"
                f"{'='*50}\n"
                f"Market: {opp.market_title}\n"
                f"Outcomes ({len(opp.outcomes)}): {outcomes_str}\n"
                f"Total Cost: ${opp.total_cost:.4f}\n"
                f"Guaranteed Payout: $1.00\n"
                f"Profit: ${opp.profit:.4f} ({opp.profit_pct:.2%})\n"
                f"24h Volume: ${opp.volume_24h:,.0f}\n"
                f"Action: Buy 1 share of EACH outcome\n"
                f"{'='*50}"
            )

            await self._paper_trade_arbitrage(opp)

        # Keep only recent opportunities (last 100)
        if len(self._arbitrage_opportunities) > 100:
            self._arbitrage_opportunities = self._arbitrage_opportunities[-50:]

    async def _paper_trade_arbitrage(self, opp: ArbitrageOpportunity):
        """Paper trade an arbitrage opportunity"""
        # Check exposure limits
        if self._total_exposure >= self.max_total_exposure:
            logger.info(f"   Max exposure reached, skipping arbitrage")
            return

        # For arbitrage, we buy $1 worth of each outcome set
        # This guarantees $1 payout regardless of which wins
        our_size = min(self.max_per_trade, self.max_total_exposure - self._total_exposure)

        # Scale the arbitrage to our position size
        scale = our_size / opp.total_cost if opp.total_cost > 0 else 0

        for outcome, price in opp.outcomes.items():
            shares = scale * (1 / price) if price > 0 else 0

            paper_trade = PaperTrade(
                timestamp=datetime.utcnow().isoformat(),
                whale_address="ARBITRAGE",
                whale_name="ARBITRAGE:risk-free",
                side="BUY",
                outcome=outcome,
                price=price,
                whale_size=0,  # N/A for arbitrage
                our_size=our_size * (price / opp.total_cost),  # Proportional
                our_shares=shares,
                market_title=opp.market_title,
                condition_id=opp.condition_id,
                asset_id=f"{opp.condition_id}_{outcome}",
                tx_hash=f"arb_{datetime.utcnow().timestamp()}",
            )

            self._paper_trades.append(paper_trade)
            self._save_trade(paper_trade)

        self._total_exposure += our_size
        profit = our_size * opp.profit_pct

        logger.info(
            f"   PAPER TRADED: ${our_size:.2f} for ${our_size + profit:.2f} guaranteed ({opp.profit_pct:.2%})"
        )

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

        # SMART HEDGE ANALYSIS: Understand the whale's net position with proper payout math
        is_hedge, net_direction, net_profit, recommendation = self._analyze_hedge(
            whale.address, condition_id, outcome, trade_value, price
        )

        if is_hedge:
            logger.info(f"   🔄 Hedge detected: profits most if {net_direction} wins (${net_profit:+,.0f})")

        if recommendation == 'skip_small_hedge':
            logger.info(f"   ⏭️ Skipping: small hedge, main bet is {net_direction}")
            # Still record for tracking
            self._record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)
            return
        elif recommendation == 'skip_no_direction':
            logger.info(f"   ⏭️ Skipping: heavily hedged, no clear direction")
            self._record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)
            return
        elif recommendation == 'skip_arbitrage':
            logger.info(f"   ⏭️ Skipping: arbitrage (guaranteed profit regardless of outcome)")
            self._record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)
            return
        elif recommendation == 'consider_hedge':
            logger.info(f"   🤔 Medium hedge - whale reducing exposure, still favors {net_direction}")
            # Still copy, but log that it's a hedge

        # Record this trade for position tracking
        self._record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)

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

        # === EXIT LOGIC: Create tracked position for BUY trades ===
        if side == "BUY":
            position = self._create_position(
                whale_address=whale.address,
                whale_name=whale.name,
                trade=trade,
                our_size=our_size_usd,
                our_shares=our_shares,
            )
            logger.info(
                f"   📝 COPIED BUY: ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                f"({our_shares:.2f} shares) [Position: {position.position_id}]"
            )
        else:
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

    # ================================================================
    # EXIT LOGIC: Whale Sell Detection & Market Resolution
    # ================================================================

    def _create_position(self, whale_address: str, whale_name: str, trade: dict,
                         our_size: float, our_shares: float) -> CopiedPosition:
        """Create a tracked position when we copy a whale BUY"""
        self._position_counter += 1
        position_id = f"pos_{self._position_counter}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        position = CopiedPosition(
            position_id=position_id,
            market_id=trade.get("conditionId", ""),
            token_id=trade.get("asset", ""),
            outcome=trade.get("outcome", ""),
            whale_address=whale_address.lower(),
            whale_name=whale_name,
            entry_price=trade.get("price", 0),
            shares=our_shares,
            entry_time=datetime.utcnow().isoformat(),
            copy_amount_usd=our_size,
            market_title=trade.get("title", "Unknown"),
            status="open",
        )

        self._copied_positions[position_id] = position
        return position

    def _check_for_whale_sells(self, trade: dict) -> Optional[dict]:
        """
        Check if this trade is a SELL from a whale we copied.
        Returns sell signal if we should exit our position.
        """
        side = trade.get("side", "")
        if side != "SELL":
            return None

        wallet = trade.get("proxyWallet", "").lower()
        token_id = trade.get("asset", "")
        sell_price = trade.get("price", 0)
        sell_size = trade.get("size", 0)

        # Find any open positions where:
        # 1. Same whale address
        # 2. Same token (they're selling what they bought)
        for pos_id, position in self._copied_positions.items():
            if (position.status == "open" and
                position.whale_address == wallet and
                position.token_id == token_id):

                return {
                    "action": "SELL",
                    "position_id": pos_id,
                    "position": position,
                    "whale_sell_price": sell_price,
                    "whale_sell_size": sell_size,
                    "whale_sell_value": sell_size * sell_price,
                }

        return None

    async def _execute_copy_sell(self, signal: dict):
        """Sell our position when whale sells theirs (paper trade)"""
        position: CopiedPosition = signal["position"]
        sell_price = signal["whale_sell_price"]

        # Calculate P&L
        # P&L = (exit_price - entry_price) * shares
        pnl = (sell_price - position.entry_price) * position.shares

        # Update position
        position.status = "closed"
        position.exit_price = sell_price
        position.exit_time = datetime.utcnow().isoformat()
        position.exit_reason = "whale_sold"
        position.pnl = pnl

        # Update totals
        self._realized_pnl += pnl
        self._total_exposure -= position.copy_amount_usd
        self._positions_closed += 1
        if pnl > 0:
            self._positions_won += 1
        elif pnl < 0:
            self._positions_lost += 1

        # Log
        pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        logger.info(
            f"   📤 COPIED SELL (whale exited): {position.outcome} @ {sell_price:.1%} "
            f"| Entry: {position.entry_price:.1%} | P&L: {pnl_emoji} ${pnl:+.2f}"
        )

        # Save exit trade
        exit_trade = PaperTrade(
            timestamp=datetime.utcnow().isoformat(),
            whale_address=position.whale_address,
            whale_name=position.whale_name,
            side="SELL",
            outcome=position.outcome,
            price=sell_price,
            whale_size=signal["whale_sell_size"],
            our_size=position.copy_amount_usd,
            our_shares=position.shares,
            market_title=position.market_title,
            condition_id=position.market_id,
            asset_id=position.token_id,
            tx_hash=f"exit_whale_{datetime.utcnow().timestamp()}",
        )
        self._save_trade(exit_trade)

    async def _check_market_resolutions(self):
        """
        Check if any markets with open positions have resolved.
        Markets resolve when price hits 0% or 100%, or when explicitly closed.
        """
        now = datetime.utcnow()

        # Only check periodically
        if (now - self._last_resolution_check).total_seconds() < self._resolution_check_interval:
            return

        self._last_resolution_check = now

        # Get unique market IDs from open positions
        open_market_ids = {
            p.market_id for p in self._copied_positions.values()
            if p.status == "open"
        }

        if not open_market_ids:
            return

        for market_id in open_market_ids:
            try:
                market_data = await self._fetch_market_data(market_id)
                if not market_data:
                    continue

                is_resolved, resolution_outcome = self._is_market_resolved(market_data)

                if is_resolved:
                    # Close all positions in this market
                    for pos_id, position in list(self._copied_positions.items()):
                        if position.market_id == market_id and position.status == "open":
                            await self._close_position_at_resolution(
                                pos_id, resolution_outcome, market_data
                            )

            except Exception as e:
                logger.warning(f"Error checking resolution for {market_id}: {e}")

    async def _fetch_market_data(self, condition_id: str) -> Optional[dict]:
        """Fetch market details from API"""
        # Check cache first
        if condition_id in self._market_cache:
            cache_entry = self._market_cache[condition_id]
            # Cache for 60 seconds
            if (datetime.utcnow() - cache_entry.get("_cached_at", datetime.min)).total_seconds() < 60:
                return cache_entry

        try:
            url = f"{self.DATA_API_BASE}/markets/{condition_id}"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    # Try alternative endpoint
                    url = f"{self.DATA_API_BASE}/markets"
                    params = {"condition_id": condition_id}
                    async with self._session.get(url, params=params) as resp2:
                        if resp2.status != 200:
                            return None
                        markets = await resp2.json()
                        if markets:
                            market_data = markets[0] if isinstance(markets, list) else markets
                        else:
                            return None
                else:
                    market_data = await resp.json()

                market_data["_cached_at"] = datetime.utcnow()
                self._market_cache[condition_id] = market_data
                return market_data

        except Exception as e:
            logger.warning(f"Error fetching market {condition_id}: {e}")
            return None

    def _is_market_resolved(self, market_data: dict) -> tuple:
        """
        Determine if a market has resolved and what the outcome is.
        Returns: (is_resolved: bool, winning_outcome: str | None)
        """
        # Check explicit resolution flags
        if market_data.get("resolved"):
            return (True, market_data.get("resolution", market_data.get("winning_outcome")))

        if market_data.get("closed"):
            return (True, market_data.get("resolution", market_data.get("winning_outcome")))

        # Check price convergence for binary markets
        # If Yes price is ~100% or ~0%, market is effectively resolved
        tokens = market_data.get("tokens", [])
        if tokens:
            for token in tokens:
                price = token.get("price", 0.5)
                outcome = token.get("outcome", "")
                if price >= 0.99:
                    return (True, outcome)
                if price <= 0.01:
                    # This outcome lost, other one won
                    other_outcome = "No" if outcome == "Yes" else "Yes"
                    return (True, other_outcome)

        # Check if past end date
        end_date_str = market_data.get("endDate") or market_data.get("end_date")
        if end_date_str:
            try:
                from dateutil.parser import parse as parse_date
                end_date = parse_date(end_date_str)
                if datetime.utcnow() > end_date:
                    # Past end date but no resolution yet - might need manual check
                    return (False, None)
            except:
                pass

        return (False, None)

    async def _close_position_at_resolution(self, pos_id: str, winning_outcome: str, market_data: dict):
        """Close a position when market resolves"""
        position = self._copied_positions.get(pos_id)
        if not position or position.status != "open":
            return

        # Determine exit price based on resolution
        # If we held the winning outcome: exit_price = 1.0 (each share pays $1)
        # If we held the losing outcome: exit_price = 0.0
        if winning_outcome:
            did_win = position.outcome.lower() == winning_outcome.lower()
            exit_price = 1.0 if did_win else 0.0
        else:
            # Unknown resolution - use current price if available
            exit_price = position.entry_price  # Conservative: assume no change

        # Calculate P&L
        pnl = (exit_price - position.entry_price) * position.shares

        # Update position
        position.status = "closed"
        position.exit_price = exit_price
        position.exit_time = datetime.utcnow().isoformat()
        position.exit_reason = "resolved"
        position.pnl = pnl

        # Update totals
        self._realized_pnl += pnl
        self._total_exposure -= position.copy_amount_usd
        self._positions_closed += 1
        if pnl > 0:
            self._positions_won += 1
        elif pnl < 0:
            self._positions_lost += 1

        # Log
        result = "WON" if exit_price == 1.0 else "LOST" if exit_price == 0.0 else "UNKNOWN"
        pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        logger.info(
            f"🏁 MARKET RESOLVED: {position.market_title[:40]}... "
            f"| Our bet: {position.outcome} → {result} "
            f"| P&L: {pnl_emoji} ${pnl:+.2f}"
        )

        # Save resolution trade
        exit_trade = PaperTrade(
            timestamp=datetime.utcnow().isoformat(),
            whale_address=position.whale_address,
            whale_name=position.whale_name,
            side="RESOLVED",
            outcome=position.outcome,
            price=exit_price,
            whale_size=0,
            our_size=position.copy_amount_usd,
            our_shares=position.shares,
            market_title=position.market_title,
            condition_id=position.market_id,
            asset_id=position.token_id,
            tx_hash=f"resolved_{datetime.utcnow().timestamp()}",
        )
        self._save_trade(exit_trade)

    def _calculate_unrealized_pnl(self) -> float:
        """Calculate unrealized P&L for open positions"""
        unrealized = 0.0
        for position in self._copied_positions.values():
            if position.status == "open":
                current_price = self._current_prices.get(position.token_id, position.entry_price)
                pnl = (current_price - position.entry_price) * position.shares
                unrealized += pnl
        return unrealized

    def _get_portfolio_stats(self) -> dict:
        """Calculate comprehensive portfolio statistics"""
        open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
        closed_positions = [p for p in self._copied_positions.values() if p.status == "closed"]

        unrealized_pnl = self._calculate_unrealized_pnl()
        total_pnl = self._realized_pnl + unrealized_pnl

        return {
            "realized_pnl": self._realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "positions_won": self._positions_won,
            "positions_lost": self._positions_lost,
            "win_rate": self._positions_won / len(closed_positions) if closed_positions else 0,
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "open_exposure": sum(p.copy_amount_usd for p in open_positions),
        }

    async def _periodic_report(self):
        """Print status report every 5 minutes"""
        while self._running:
            await asyncio.sleep(300)

            runtime = (datetime.utcnow() - self._start_time).total_seconds() / 3600

            # Get comprehensive portfolio stats
            stats = self._get_portfolio_stats()

            # Calculate return percentage
            total_invested = stats["open_exposure"] + sum(
                p.copy_amount_usd for p in self._copied_positions.values() if p.status == "closed"
            )
            return_pct = (stats["total_pnl"] / total_invested * 100) if total_invested > 0 else 0

            # Projections based on realized P&L
            hourly_return = stats["realized_pnl"] / runtime if runtime > 0 else 0
            daily_projected = hourly_return * 24

            # Count whale vs unusual vs arbitrage copies
            whale_copies = len([t for t in self._paper_trades if not t.whale_name.startswith("UNUSUAL:") and not t.whale_name.startswith("ARBITRAGE:")])
            unusual_copies = len([t for t in self._paper_trades if t.whale_name.startswith("UNUSUAL:")])
            arb_copies = len([t for t in self._paper_trades if t.whale_name.startswith("ARBITRAGE:")])

            logger.info(
                f"\n{'='*60}\n"
                f"🐋 WHALE COPY TRADING REPORT ({runtime:.1f}h runtime)\n"
                f"{'='*60}\n"
                f"Polls: {self._polls_completed} | Wallets tracked: {len(self._wallet_history)}\n"
                f"Whale copies: {whale_copies} | Unusual copies: {unusual_copies}\n"
                f"Arbitrage opportunities: {self._arbitrage_found_count} | Arb trades: {arb_copies}\n"
                f"Cluster signals detected: {self._cluster_signals}\n"
                f"{'='*60}\n"
                f"📊 POSITION STATUS\n"
                f"   Open Positions: {stats['open_positions']} (${stats['open_exposure']:.2f} exposure)\n"
                f"   Closed Positions: {stats['closed_positions']}\n"
                f"{'='*60}\n"
                f"💰 P&L SUMMARY\n"
                f"   Realized P&L:   ${stats['realized_pnl']:+.2f}\n"
                f"   Unrealized P&L: ${stats['unrealized_pnl']:+.2f}\n"
                f"   Total P&L:      ${stats['total_pnl']:+.2f} ({return_pct:+.1f}%)\n"
                f"   Winners: {stats['positions_won']} | Losers: {stats['positions_lost']} | "
                f"Win Rate: {stats['win_rate']*100:.1f}%\n"
                f"{'='*60}\n"
                f"📈 PROJECTIONS (based on realized)\n"
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

            # Show open positions
            open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
            if open_positions:
                logger.info("📈 OPEN POSITIONS:")
                for pos in sorted(open_positions, key=lambda p: p.entry_time, reverse=True)[:5]:
                    current = self._current_prices.get(pos.token_id, pos.entry_price)
                    unrealized = (current - pos.entry_price) * pos.shares
                    emoji = "🟢" if unrealized > 0 else "🔴" if unrealized < 0 else "⚪"
                    logger.info(
                        f"   {emoji} {pos.outcome} @ {pos.entry_price:.1%} → {current:.1%} "
                        f"| ${unrealized:+.2f} | {pos.market_title[:30]}..."
                    )
                if len(open_positions) > 5:
                    logger.info(f"   ... and {len(open_positions) - 5} more")

            logger.info(f"{'='*60}\n")

    def _print_final_report(self):
        """Print final summary when shutting down"""
        runtime = (datetime.utcnow() - self._start_time).total_seconds() / 3600
        stats = self._get_portfolio_stats()

        logger.info(
            f"\n{'='*60}\n"
            f"🐋 FINAL WHALE COPY TRADING REPORT\n"
            f"{'='*60}\n"
            f"Runtime: {runtime:.2f} hours\n"
            f"Total trades copied: {len(self._paper_trades)}\n"
            f"{'='*60}\n"
            f"📊 POSITIONS\n"
            f"   Opened: {stats['open_positions'] + stats['closed_positions']}\n"
            f"   Closed: {stats['closed_positions']}\n"
            f"   Still Open: {stats['open_positions']} (${stats['open_exposure']:.2f})\n"
            f"{'='*60}\n"
            f"💰 FINAL P&L\n"
            f"   Realized:   ${stats['realized_pnl']:+.2f}\n"
            f"   Unrealized: ${stats['unrealized_pnl']:+.2f}\n"
            f"   Total:      ${stats['total_pnl']:+.2f}\n"
            f"   Win Rate:   {stats['win_rate']*100:.1f}% ({stats['positions_won']}W / {stats['positions_lost']}L)\n"
            f"{'='*60}"
        )

        # List any remaining open positions
        open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
        if open_positions:
            logger.info("📈 REMAINING OPEN POSITIONS:")
            for pos in open_positions:
                current = self._current_prices.get(pos.token_id, pos.entry_price)
                unrealized = (current - pos.entry_price) * pos.shares
                logger.info(
                    f"   {pos.outcome} @ {pos.entry_price:.1%} | ${unrealized:+.2f} | "
                    f"{pos.market_title[:40]}... | Whale: {pos.whale_name}"
                )
            logger.info(f"{'='*60}")


def parse_args():
    parser = argparse.ArgumentParser(description="Whale Copy Trader")
    parser.add_argument("--max-trade", type=float, default=1.0, help="Max $ per trade")
    parser.add_argument("--max-total", type=float, default=10000.0, help="Max total exposure")
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
