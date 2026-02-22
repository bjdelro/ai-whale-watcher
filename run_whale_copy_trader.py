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
import random
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set, Optional
from dataclasses import dataclass, asdict

import aiohttp
from dotenv import load_dotenv

# Arbitrage scanning
from src.arbitrage import IntraMarketArbitrage, ArbitrageOpportunity

# Live trading execution
from src.execution.live_trader import LiveTrader, LiveOrder
from src.execution.redeemer import PositionRedeemer

# Market data
from src.market_data import MarketDataClient

# Whale management
from src.whales import WhaleWallet, WhaleManager
from src.whales.whale_manager import (
    FALLBACK_WHALES, LEADERBOARD_REFRESH_HOURS,
    fetch_top_whales,
)

# Signal detection
from src.signals import ClusterDetector

# Reporting
from src.reporting import Reporter

# Position management
from src.positions import PositionManager

# Trade evaluation
from src.evaluation import TradeEvaluator

# Load environment variables
load_dotenv()

# ================================================================
# Patch py-clob-client to bypass Cloudflare on POST /order
# Cloudflare uses TLS fingerprinting to block httpx/python requests.
# curl_cffi impersonates Chrome's TLS fingerprint to get through.
#
# The client.py imports `post` directly from helpers, so we must patch
# both the helpers module AND the client module's local reference.
# ================================================================
from curl_cffi import requests as _curl_requests
from py_clob_client.http_helpers import helpers as _clob_helpers
from py_clob_client import client as _clob_client_module
from py_clob_client.exceptions import PolyApiException as _PolyApiException
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

_original_post = _clob_helpers.post


def _patched_post(endpoint, headers=None, data=None):
    """Route POST /order through curl_cffi, everything else through httpx."""
    if "/order" in endpoint:
        headers = _clob_helpers.overloadHeaders("POST", headers)
        if isinstance(data, str):
            resp = _curl_requests.post(
                endpoint, headers=headers,
                data=data.encode("utf-8"), impersonate="chrome",
            )
        else:
            resp = _curl_requests.post(
                endpoint, headers=headers,
                json=data, impersonate="chrome",
            )
        if resp.status_code != 200:
            raise _PolyApiException(resp)
        try:
            return resp.json()
        except ValueError:
            return resp.text
    return _original_post(endpoint, headers, data)


# Patch both the helpers module and the client module's local reference
_clob_helpers.post = _patched_post
_clob_client_module.post = _patched_post

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
    # Live order fields (populated only when a real order fills successfully)
    live_shares: Optional[float] = None
    live_cost_usd: Optional[float] = None
    live_order_id: Optional[str] = None


class WhaleCopyTrader:
    """
    Monitors whale wallets and copies their trades.
    Also detects "unusual activity" - small wallets making large trades.

    Uses the Polymarket Data API to fetch recent trades for each whale,
    then simulates copying profitable-looking trades.
    """

    DATA_API_BASE = "https://data-api.polymarket.com"
    POLL_INTERVAL = 15  # seconds between polls
    MIN_WHALE_TRADE_SIZE = 50  # Skip dust/accidental trades from whales — only copy deliberate trades

    # Unusual activity detection thresholds
    UNUSUAL_TRADE_SIZE = 1000  # Flag trades >= $1000 from unknown wallets
    UNUSUAL_RATIO = 5.0  # Flag if trade is 5x larger than wallet's average
    UNUSUAL_MIN_TRADE = 500  # Minimum trade size to even consider for unusual activity
    UNUSUAL_MIN_AVG = 100  # Wallet must have avg trade size >= $100 for ratio comparison

    # Market timing filters
    AVOID_EXTREME_PRICES = True  # Skip markets at >95% or <5%
    EXTREME_PRICE_THRESHOLD = 0.15  # 15% threshold — skip >85% or <15% (terrible risk/reward)

    # Active position management
    STOP_LOSS_PCT = 0.15         # Close position if down 15% from entry
    TAKE_PROFIT_PCT = 0.20       # Close position if up 20% from entry
    STALE_POSITION_HOURS = 48    # Close positions that haven't moved after 48h

    # Price-slippage gate
    MAX_SLIPPAGE_PCT = 0.03      # Skip if current price > whale entry + 3%

    def __init__(
        self,
        max_per_trade: float = 1.0,
        max_total_exposure: float = 100.0,
        # Live trading configuration
        live_trading_enabled: bool = False,
        live_max_per_trade: float = 5.0,
        live_max_exposure: float = 50.0,
        live_dry_run: bool = True,  # Safety: dry run by default even if enabled
    ):
        self.max_per_trade = max_per_trade
        self.max_total_exposure = max_total_exposure

        # Live trading settings
        self.live_trading_enabled = live_trading_enabled
        self.live_max_per_trade = live_max_per_trade
        self.live_max_exposure = live_max_exposure
        self.live_dry_run = live_dry_run
        self._live_trader: Optional[LiveTrader] = None

        # Slack alerts
        self._slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        if self._slack_webhook_url:
            logger.info("Slack alerts enabled")

        # Modules (initialized in start() when session is available)
        self._reporter: Optional[Reporter] = None
        self._whale_manager: Optional[WhaleManager] = None
        self._position_manager: Optional[PositionManager] = None
        self._market_data: Optional[MarketDataClient] = None

        # Backwards-compatible property — populated by whale manager
        self.whales: Dict[str, WhaleWallet] = {w.address.lower(): w for w in FALLBACK_WHALES}

        # State
        self._running = False
        self._session: aiohttp.ClientSession = None
        self._start_time = datetime.now(timezone.utc)
        self._polls_completed = 0

        # Track wallet history for unusual activity detection
        self._wallet_history: Dict[str, List[float]] = {}
        self._unusual_activity_count = 0

        # Cluster and hedge detection
        self._cluster_detector = ClusterDetector()

        # Arbitrage scanner
        self._arbitrage_scanner: Optional[IntraMarketArbitrage] = None
        self._arbitrage_opportunities: List[ArbitrageOpportunity] = []
        self._last_arbitrage_scan = datetime.min.replace(tzinfo=timezone.utc)
        self._arbitrage_scan_interval = 60
        self._arbitrage_found_count = 0

        # Persistent state file — separate per trading mode so paper/live never mix
        if live_trading_enabled and not live_dry_run:
            mode_suffix = "live"
        elif live_trading_enabled:
            mode_suffix = "dry_run"
        else:
            mode_suffix = "paper"
        self._state_file = os.getenv(
            "STATE_FILE",
            f"market_logs/positions_state_{mode_suffix}.json"
        )

        # Per-whale copy P&L — proxies to WhaleManager (set up in start())
        self._whale_copy_pnl: Dict[str, dict] = {}
        self._pruned_whales: Set[str] = set()

    async def start(self):
        """Initialize and start the copy trader"""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._market_data = MarketDataClient(self._session)

        # Config dict shared across modules
        self._module_config = {
            "live_trading_enabled": self.live_trading_enabled,
            "live_dry_run": self.live_dry_run,
            "max_per_trade": self.max_per_trade,
            "max_total_exposure": self.max_total_exposure,
            "live_max_per_trade": self.live_max_per_trade,
            "live_max_exposure": self.live_max_exposure,
            "data_api_base": self.DATA_API_BASE,
            "stop_loss_pct": self.STOP_LOSS_PCT,
            "take_profit_pct": self.TAKE_PROFIT_PCT,
            "stale_position_hours": self.STALE_POSITION_HOURS,
            "max_exposure": self.live_max_exposure if self.live_trading_enabled else self.max_total_exposure,
            "min_whale_trade_size": self.MIN_WHALE_TRADE_SIZE,
            "avoid_extreme_prices": self.AVOID_EXTREME_PRICES,
            "extreme_price_threshold": self.EXTREME_PRICE_THRESHOLD,
        }

        # Initialize whale manager (no deps on position_manager yet)
        def _get_open_position_whales():
            if self._position_manager:
                return {
                    pos.whale_address.lower()
                    for pos in self._position_manager.positions.values()
                    if pos.status == "open"
                }
            return set()
        self._whale_manager = WhaleManager(
            session=self._session,
            max_per_trade=self.max_per_trade,
            get_open_position_whales=_get_open_position_whales,
        )

        # Initialize reporter (needs _get_portfolio_stats which comes from position_manager)
        # We'll wire the callbacks after position_manager is created
        self._reporter = Reporter(
            session=self._session,
            slack_webhook_url=self._slack_webhook_url,
            get_portfolio_stats=lambda: self._get_portfolio_stats(),
            get_report_data=lambda: self._get_report_data(),
            config=self._module_config,
        )

        # Initialize position manager — will load state
        self._position_manager = PositionManager(
            state_file=self._state_file,
            market_data=self._market_data,
            live_trader=None,  # Set after live_trader is initialized
            redeemer=None,     # Set after redeemer is initialized
            whale_manager=self._whale_manager,
            reporter=self._reporter,
            session=self._session,
            config=self._module_config,
        )

        # Load state and restore whale manager
        whale_state = self._position_manager.load_state(CopiedPosition)
        self._whale_manager.from_dict({
            "active_whale_count": whale_state.get("active_whale_count", 8),
            "whale_copy_pnl": whale_state.get("whale_copy_pnl", {}),
            "pruned_whales": whale_state.get("pruned_whales", []),
        })
        # Point proxy references to whale manager's state
        self._whale_copy_pnl = self._whale_manager._whale_copy_pnl
        self._pruned_whales = self._whale_manager._pruned_whales

        # Initialize trade evaluator
        self._trade_evaluator = TradeEvaluator(
            position_manager=self._position_manager,
            cluster_detector=self._cluster_detector,
            market_data=self._market_data,
            whale_manager=self._whale_manager,
            fetch_whale_trades=self._fetch_whale_trades,
            get_active_whales=lambda: self.whales,
            get_polls_completed=lambda: self._polls_completed,
            config=self._module_config,
        )

        self._running = True

        # Fetch whale list dynamically from leaderboard API
        await self._refresh_whale_list()

        logger.info("=" * 60)
        logger.info("🐋 WHALE COPY TRADER")
        logger.info("=" * 60)
        logger.info(f"Tracking {len(self.whales)} whale wallets")
        logger.info(f"Max per trade: ${self.max_per_trade:.2f} (paper)")
        logger.info(f"Max exposure: ${self.max_total_exposure:.2f} (paper)")
        logger.info(f"Min whale trade size: ${self.MIN_WHALE_TRADE_SIZE}")

        # Live trading status
        if self.live_trading_enabled:
            mode = "DRY RUN" if self.live_dry_run else "LIVE"
            logger.info("=" * 60)
            logger.info(f"💰 LIVE TRADING: ENABLED ({mode})")
            logger.info(f"   Max per trade: ${self.live_max_per_trade:.2f}")
            logger.info(f"   Max exposure: ${self.live_max_exposure:.2f}")
            if not self.live_dry_run:
                logger.info("   ⚠️  REAL MONEY MODE - TRADES WILL EXECUTE")
        else:
            logger.info("💰 LIVE TRADING: DISABLED (paper only)")

        logger.info("=" * 60)

        # Log whale names
        whale_list = sorted(self.whales.values(), key=lambda w: w.monthly_profit, reverse=True)
        logger.info("🎯 Whales being tracked:")
        for i, whale in enumerate(whale_list[:10], 1):
            logger.info(f"   {i}. {whale.name} (${whale.monthly_profit:,.0f}/mo)")
        if len(whale_list) > 10:
            logger.info(f"   ... and {len(whale_list) - 10} more")
        logger.info("=" * 60)

        # Initialize arbitrage scanner with shared session
        self._arbitrage_scanner = IntraMarketArbitrage(session=self._session)

        # Initialize live trader if enabled
        if self.live_trading_enabled:
            self._live_trader = LiveTrader(
                max_order_usd=self.live_max_per_trade,
                max_total_exposure=self.live_max_exposure,
                dry_run=self.live_dry_run,
            )
            if not self._live_trader.initialize():
                logger.error("Failed to initialize LiveTrader - disabling live trading")
                self.live_trading_enabled = False
                self._live_trader = None

        # Initialize position redeemer for auto-claiming winning positions
        self._redeemer = None
        if self.live_trading_enabled:
            self._redeemer = PositionRedeemer()
            if not self._redeemer.initialize():
                logger.warning("PositionRedeemer failed to initialize - winning positions must be redeemed manually")
                self._redeemer = None
            else:
                logger.info("PositionRedeemer initialized - will auto-redeem winning positions")

        # Wire live trader and redeemer into position manager
        self._position_manager._live_trader = self._live_trader
        self._position_manager._redeemer = self._redeemer

        # Reconcile positions against current market state
        await self._position_manager.reconcile_positions(
            fetch_whale_trades=self._fetch_whale_trades,
            BalanceAllowanceParams=BalanceAllowanceParams,
            AssetType=AssetType,
        )

        logger.info("Starting whale monitoring + arbitrage scanning...")

        # Send Slack startup notification
        mode = "PAPER"
        if self.live_trading_enabled:
            mode = "DRY RUN" if self.live_dry_run else "LIVE"
        # Startup logged locally only (no Slack — keep alerts for buys/sells only)

    async def _refresh_whale_list(self):
        """Fetch the leaderboard and update the tracked whale list."""
        await self._whale_manager.refresh_from_leaderboard()
        self.whales = self._whale_manager.whales

    def _rebuild_active_whales(self):
        """Rebuild active whale set based on scaling + open positions."""
        self._whale_manager.rebuild_active_set()
        self.whales = self._whale_manager.whales

    def _check_scaling(self):
        """Check if we should scale the active whale count up or down."""
        self._whale_manager.check_scaling()
        self.whales = self._whale_manager.whales

    async def _arbitrage_loop(self):
        """Separate loop for arbitrage scanning so it doesn't block whale polling"""
        while self._running:
            try:
                await self._scan_for_arbitrage()
            except Exception as e:
                logger.warning(f"Error scanning arbitrage: {e}")
            await asyncio.sleep(60)  # Arbitrage scan every 60 seconds

    async def run(self):
        """Main loop - poll for whale trades"""
        report_task = asyncio.create_task(self._periodic_report())
        arbitrage_task = asyncio.create_task(self._arbitrage_loop())

        try:
            while self._running:
                # Periodically refresh the whale leaderboard
                if self._whale_manager.last_leaderboard_refresh:
                    hours_since = (datetime.now(timezone.utc) - self._whale_manager.last_leaderboard_refresh).total_seconds() / 3600
                    if hours_since >= LEADERBOARD_REFRESH_HOURS:
                        await self._refresh_whale_list()

                await self._poll_whale_trades()
                await asyncio.sleep(self.POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Shutting down...")
        finally:
            report_task.cancel()
            arbitrage_task.cancel()
            await self.stop()

    async def stop(self):
        """Clean shutdown"""
        self._running = False

        # Save state before shutting down
        self._position_manager.save_state()
        logger.info("State saved to disk before shutdown")

        if self._session:
            await self._session.close()

        # Shutdown live trader if active
        if self._live_trader:
            self._live_trader.shutdown()

        # Print final report
        self._print_final_report()

    async def _poll_whale_trades(self):
        """Poll recent trades for all tracked whales AND scan for unusual activity"""
        self._polls_completed += 1
        new_trades_found = 0
        unusual_found = 0
        sell_signals_found = 0

        # 1. Check known whale wallets
        whale_fetch_successes = 0
        whale_fetch_failures = 0
        total_trades_returned = 0
        trades_skipped_seen = 0
        trades_skipped_old = 0
        trades_skipped_no_ts = 0
        trades_skipped_parse_err = 0
        trades_fresh = 0
        trades_evaluated = 0
        poll_start_time = datetime.now(timezone.utc)

        for address, whale in self.whales.items():
            try:
                trades = await self._fetch_whale_trades(address, limit=10)
                total_trades_returned += len(trades)
                if trades:
                    whale_fetch_successes += 1
                else:
                    whale_fetch_failures += 1

                for trade in trades:
                    tx_hash = trade.get("transactionHash", "")

                    # Skip if we've already successfully processed this trade
                    if tx_hash in self._position_manager._seen_tx_hashes:
                        trades_skipped_seen += 1
                        continue

                    # Check trade age BEFORE marking as seen — if it's too old,
                    # don't add to seen set so we can retry on next poll when
                    # the API might return fresher trades
                    trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
                    if not trade_timestamp:
                        trades_skipped_no_ts += 1
                        continue

                    try:
                        if isinstance(trade_timestamp, (int, float)) or str(trade_timestamp).isdigit():
                            ts = float(trade_timestamp)
                            if ts > 1e12:
                                ts = ts / 1000
                        else:
                            ts = None
                        if ts is not None:
                            age = (poll_start_time - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
                            is_old = age > 300
                            if is_old:
                                # Even if the trade is old, still check if it's a SELL
                                # matching one of our open positions — whale exits must
                                # not be missed just because the poll was delayed or the
                                # trade is slightly older than 5 minutes.
                                if trade.get("side") == "SELL":
                                    sell_signal = self._check_for_whale_sells(trade)
                                    if sell_signal:
                                        self._position_manager._seen_tx_hashes.add(tx_hash)
                                        sell_signals_found += 1
                                        logger.info(
                                            f"🐋 WHALE SELLING (late detect, {age:.0f}s old): "
                                            f"{whale.name} exiting position in "
                                            f"{sell_signal['position'].market_title[:40]}..."
                                        )
                                        await self._execute_copy_sell(sell_signal)
                                        continue

                                trades_skipped_old += 1
                                # Log every old trade on first poll so we can see what's being skipped
                                if self._polls_completed == 1:
                                    side = trade.get("side", "?")
                                    size = trade.get("size", 0)
                                    price = trade.get("price", 0)
                                    title = trade.get("title", "?")[:35]
                                    logger.info(
                                        f"   ⏭️ {whale.name}: OLD ({age:.0f}s) "
                                        f"| {side} ${size*price:,.0f} | {title}"
                                    )
                                continue
                            else:
                                # FRESH TRADE — log it immediately
                                side = trade.get("side", "?")
                                size = trade.get("size", 0)
                                price = trade.get("price", 0)
                                title = trade.get("title", "?")[:40]
                                logger.info(
                                    f"🔥 FRESH TRADE FOUND: {whale.name} "
                                    f"| {side} ${size*price:,.0f} | age={age:.0f}s "
                                    f"| {title}"
                                )
                                trades_fresh += 1
                                # Record for progressive scaling
                                self._whale_manager._fresh_trade_timestamps.append(datetime.now(timezone.utc))
                    except Exception as e:
                        trades_skipped_parse_err += 1
                        logger.warning(f"   ⚠️ {whale.name}: timestamp parse error: {trade_timestamp} ({e})")
                        continue

                    # Mark as seen NOW (trade is fresh enough to process)
                    self._position_manager._seen_tx_hashes.add(tx_hash)
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
                    trades_evaluated += 1
                    await self._evaluate_trade(whale, trade)

                # Small delay between wallets to avoid rate limits
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.warning(f"Error polling {whale.name}: {e}")
                whale_fetch_failures += 1

        poll_duration = (datetime.now(timezone.utc) - poll_start_time).total_seconds()

        # Log summary EVERY poll so we can always see what's happening
        logger.info(
            f"📡 Poll #{self._polls_completed} ({poll_duration:.1f}s): "
            f"API={whale_fetch_successes}/{len(self.whales)} OK | "
            f"trades={total_trades_returned} returned, {trades_skipped_seen} seen, "
            f"{trades_skipped_old} old, {trades_skipped_no_ts} no-ts, "
            f"{trades_skipped_parse_err} parse-err, "
            f"{trades_fresh} fresh, {trades_evaluated} evaluated, {new_trades_found} new | "
            f"seen_cache={len(self._position_manager._seen_tx_hashes)}"
        )

        # 1b. Check for sells from wallets with open positions NOT in whale list
        #     (catches unusual-activity wallets and whales that dropped off leaderboard)
        try:
            await self._check_open_position_sells()
        except Exception as e:
            logger.warning(f"Error checking open position sells: {e}")

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

        # 4. Arbitrage scanning moved to separate _arbitrage_loop() to avoid blocking whale polls

        # 5. === EXIT LOGIC: Check for market resolutions ===
        try:
            await self._check_market_resolutions()
        except Exception as e:
            logger.warning(f"Error checking resolutions: {e}")

        # Position status every 4 polls (~1 min)
        if self._polls_completed % 4 == 0:
            stats = self._get_portfolio_stats()
            mode_tag = "[LIVE]" if self.live_trading_enabled else "[PAPER]"
            usdc_str = f"${stats['usdc_balance']:.2f}" if stats['usdc_balance'] is not None else "N/A"
            total_str = f"${stats['total_value']:.2f}" if stats['total_value'] is not None else "N/A"
            logger.info(
                f"💼 {mode_tag} "
                f"Cash: {usdc_str} | "
                f"Positions: {stats['open_positions']} open (${stats['open_market_value']:.2f} mkt val) | "
                f"Portfolio: {total_str} | "
                f"P&L: ${stats['total_pnl']:+.2f}"
            )

        # Progressive whale scaling check
        self._check_scaling()

        # Cleanup old tx hashes (keep last 10000)
        if len(self._position_manager._seen_tx_hashes) > 10000:
            self._position_manager._seen_tx_hashes = set(list(self._position_manager._seen_tx_hashes)[-5000:])

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
                    body = await resp.text()
                    logger.warning(f"⚠️ Unusual activity scan failed: HTTP {resp.status} - {body[:200]}")
                    return 0
                trades = await resp.json()

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")

                # Skip if we've already processed this trade
                if tx_hash in self._position_manager._seen_tx_hashes:
                    continue

                # Check age BEFORE marking as seen — don't burn old trades
                trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
                if not trade_timestamp:
                    continue  # No timestamp - skip to be safe
                try:
                    if isinstance(trade_timestamp, (int, float)) or str(trade_timestamp).isdigit():
                        ts = float(trade_timestamp)
                        if ts > 1e12:  # Milliseconds
                            ts = ts / 1000
                        trade_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                    else:
                        from dateutil.parser import parse as parse_date
                        trade_time = parse_date(str(trade_timestamp))
                        if trade_time.tzinfo:
                            import pytz
                            trade_time = trade_time.astimezone(pytz.UTC).replace(tzinfo=None)

                    age_seconds = (datetime.now(timezone.utc) - trade_time).total_seconds()
                    if age_seconds > 300:  # 5 minutes
                        continue  # Skip old trades silently
                except Exception:
                    continue  # Can't parse - skip to be safe

                # Trade is fresh — mark as seen now so we don't double-process
                self._position_manager._seen_tx_hashes.add(tx_hash)

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

        # DEDUP: Skip if we already have an open position for this wallet + market
        for pos in self._position_manager.positions.values():
            if (pos.status == "open" and
                pos.whale_address.lower() == wallet and
                pos.market_id == condition_id):
                logger.info(f"   ⏭️ Skipping: already have open position from this wallet on this market")
                return

        # CROSS-WHALE CONFLICT: Skip if we hold an opposing position from ANY whale
        wallet_name = trade.get("userName", "") or wallet[:12]
        if self._has_conflicting_position(condition_id, outcome, f"UNUSUAL:{wallet_name}"):
            return

        # Check extreme prices
        if self.AVOID_EXTREME_PRICES:
            if price < self.EXTREME_PRICE_THRESHOLD or price > (1 - self.EXTREME_PRICE_THRESHOLD):
                logger.info(f"   ⏭️ Skipping: extreme price ({price:.1%})")
                return

        # Check exposure limits (unified: always use _total_exposure from CopiedPositions)
        max_exposure = self.live_max_exposure if self.live_trading_enabled else self.max_total_exposure
        if self._position_manager.total_exposure >= max_exposure:
            logger.info(f"   ⚠️ Max exposure reached (${self._position_manager.total_exposure:.2f}/${max_exposure:.0f}), skipping")
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

        # Only process BUY trades
        if side != "BUY":
            logger.info(f"   📝 UNUSUAL {side} (not copying non-BUY trades)")
            return

        # PRICE-SLIPPAGE GATE: Check if price has moved too far from trade entry
        if asset_id:
            current_price = await self._fetch_current_price(asset_id)
            if current_price is not None:
                slippage = current_price - price
                slippage_pct = slippage / price if price > 0 else 0
                if slippage_pct > 0.03:  # Price moved >3% above entry
                    logger.info(
                        f"   ⏭️ Skipping unusual: price slippage too high "
                        f"(trade @ {price:.1%}, now @ {current_price:.1%}, "
                        f"+{slippage_pct:.1%}) — edge likely gone"
                    )
                    return

        self._position_manager._entry_prices[asset_id] = price
        self._position_manager._unusual_copies_count += 1

        # === UNIFIED FLOW: Create position, then submit live order if enabled ===
        if self.live_trading_enabled and self._live_trader:
            # LIVE MODE: Submit order first, only create position on success
            order = await self._execute_live_buy(
                token_id=asset_id,
                price=price,
                market_title=title,
            )

            if order and order.status in ("filled", "dry_run"):
                # Order succeeded — create CopiedPosition with live fields
                our_shares = order.size
                our_size_usd = order.cost_usd
                position = self._create_position(
                    whale_address=wallet,
                    whale_name=f"UNUSUAL:{name}",
                    trade=trade,
                    our_size=our_size_usd,
                    our_shares=our_shares,
                )
                # Populate live fields
                position.live_shares = order.size
                position.live_cost_usd = order.cost_usd
                position.live_order_id = order.order_id
                # Update exposure
                self._position_manager.total_exposure += our_size_usd
                self._save_state()

                mode = "DRY RUN" if self._live_trader.dry_run else "LIVE"
                logger.info(
                    f"   📝 {mode} UNUSUAL BUY: ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                    f"({our_shares:.2f} shares) [Position: {position.position_id}]"
                )

                # Send Slack alert
                paper_trade = PaperTrade(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    whale_address=wallet, whale_name=f"UNUSUAL:{name}",
                    side=side, outcome=outcome, price=price,
                    whale_size=whale_size, our_size=our_size_usd,
                    our_shares=our_shares, market_title=title,
                    condition_id=condition_id, asset_id=asset_id, tx_hash=tx_hash,
                )
                await self._slack_trade_alert(paper_trade, position)
            else:
                # Order failed — do NOT create position
                err = order.error_message if order else "no order returned"
                logger.warning(
                    f"   ❌ LIVE ORDER FAILED (not tracking): {outcome} @ {price:.1%} | "
                    f"{err} | {title[:30]}..."
                )
        else:
            # PAPER MODE: Create position with paper sizing
            our_size_usd = min(self.max_per_trade, self.max_total_exposure - self._position_manager.total_exposure)
            our_shares = our_size_usd / price if price > 0 else 0

            position = self._create_position(
                whale_address=wallet,
                whale_name=f"UNUSUAL:{name}",
                trade=trade,
                our_size=our_size_usd,
                our_shares=our_shares,
            )
            self._position_manager.total_exposure += our_size_usd

            paper_trade = PaperTrade(
                timestamp=datetime.now(timezone.utc).isoformat(),
                whale_address=wallet, whale_name=f"UNUSUAL:{name}",
                side=side, outcome=outcome, price=price,
                whale_size=whale_size, our_size=our_size_usd,
                our_shares=our_shares, market_title=title,
                condition_id=condition_id, asset_id=asset_id, tx_hash=tx_hash,
            )
            self._position_manager._paper_trades.append(paper_trade)
            self._save_trade(paper_trade)

            logger.info(
                f"   📝 PAPER UNUSUAL BUY: ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                f"({our_shares:.2f} shares) [Position: {position.position_id}]"
            )
            await self._slack_trade_alert(paper_trade, position)

        # Record for cluster detection
        self._record_trade_for_cluster(condition_id, wallet, side, whale_size * price)

    def _record_wallet_market_trade(self, wallet: str, condition_id: str, outcome: str, value: float, price: float):
        """Record a wallet's trade on a market and update net position."""
        self._cluster_detector.record_wallet_market_trade(wallet, condition_id, outcome, value, price)

    def _get_net_position(self, wallet: str, condition_id: str) -> Dict[str, float]:
        """Get the net position for a wallet on a market."""
        return self._cluster_detector.get_net_position(wallet, condition_id)

    def _analyze_hedge(self, wallet: str, condition_id: str, current_outcome: str, current_value: float, current_price: float) -> tuple:
        """Analyze if this trade is a hedge and what the net position is."""
        return self._cluster_detector.analyze_hedge(wallet, condition_id, current_outcome, current_value, current_price)

    def _record_trade_for_cluster(self, condition_id: str, wallet: str, side: str, value: float):
        """Record a trade for cluster detection."""
        self._cluster_detector.record_trade_for_cluster(condition_id, wallet, side, value)

    async def _scan_for_arbitrage(self):
        """
        Scan for risk-free intra-market arbitrage opportunities.

        This runs less frequently (every 60s) since arbitrage opportunities
        are slower-moving than whale trades.
        """
        now = datetime.now(timezone.utc)

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
        # Check exposure limits (unified)
        max_exposure = self.live_max_exposure if self.live_trading_enabled else self.max_total_exposure
        if self._position_manager.total_exposure >= max_exposure:
            logger.info(f"   Max exposure reached, skipping arbitrage")
            return

        # For arbitrage, we buy $1 worth of each outcome set
        # This guarantees $1 payout regardless of which wins
        our_size = min(self.max_per_trade, max_exposure - self._position_manager.total_exposure)

        # Scale the arbitrage to our position size
        scale = our_size / opp.total_cost if opp.total_cost > 0 else 0

        for outcome, price in opp.outcomes.items():
            shares = scale * (1 / price) if price > 0 else 0

            paper_trade = PaperTrade(
                timestamp=datetime.now(timezone.utc).isoformat(),
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
                tx_hash=f"arb_{datetime.now(timezone.utc).timestamp()}",
            )

            self._position_manager._paper_trades.append(paper_trade)
            self._save_trade(paper_trade)

        self._position_manager.total_exposure += our_size
        profit = our_size * opp.profit_pct

        logger.info(
            f"   PAPER TRADED: ${our_size:.2f} for ${our_size + profit:.2f} guaranteed ({opp.profit_pct:.2%})"
        )

    async def _check_cluster_signals(self):
        """Detect cluster signals: multiple wallets betting same direction."""
        await self._cluster_detector.check_cluster_signals()

    async def _fetch_whale_trades(self, address: str, limit: int = 10) -> List[dict]:
        """Fetch recent trades for a whale wallet"""
        url = f"{self.DATA_API_BASE}/trades"
        params = {"user": address, "limit": limit}

        async with self._session.get(url, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"⚠️ Whale trade fetch failed for {address[:10]}...: HTTP {resp.status} - {body[:200]}")
                return []
            return await resp.json()

    def _has_conflicting_position(self, market_id: str, outcome: str, whale_name: str) -> bool:
        """Check if we hold an opposing position — delegates to TradeEvaluator."""
        return self._trade_evaluator.has_conflicting_position(market_id, outcome, whale_name)

    async def _evaluate_trade(self, whale: WhaleWallet, trade: dict):
        """Evaluate a whale trade and decide if we should copy it — delegates to TradeEvaluator."""
        await self._trade_evaluator.evaluate_trade(whale, trade, PaperTrade, CopiedPosition)

    async def _copy_trade(self, whale: WhaleWallet, trade: dict):
        """Execute a copy of the whale's trade — delegates to PositionManager."""
        await self._position_manager.copy_trade(whale, trade, PaperTrade, CopiedPosition)

    async def _execute_live_buy(self, token_id: str, price: float,
                               market_title: str) -> Optional[LiveOrder]:
        """Execute a live BUY order — delegates to PositionManager."""
        return await self._position_manager._execute_live_buy(token_id, price, market_title)

    def _save_trade(self, trade: PaperTrade):
        """Save trade to JSONL file — delegates to PositionManager."""
        self._position_manager._save_trade(trade)

    # ================================================================
    # POSITION PERSISTENCE (delegated to PositionManager)
    # ================================================================

    def _save_state(self):
        """Save positions and P&L to disk — delegates to PositionManager."""
        self._position_manager.save_state()

    def _load_state(self):
        """Load positions and P&L from disk — delegates to PositionManager."""
        whale_state = self._position_manager.load_state(CopiedPosition)
        if whale_state:
            self._loaded_active_whale_count = whale_state.get("active_whale_count", 8)
            self._whale_copy_pnl = whale_state.get("whale_copy_pnl", {})
            self._pruned_whales = set(whale_state.get("pruned_whales", []))

    async def _reconcile_positions(self):
        """Reconcile saved positions against current market state — delegates to PositionManager."""
        await self._position_manager.reconcile_positions(
            fetch_whale_trades=self._fetch_whale_trades,
            BalanceAllowanceParams=BalanceAllowanceParams,
            AssetType=AssetType,
        )

    # ================================================================
    # SLACK ALERTS (delegated to Reporter)
    # ================================================================

    async def _send_slack(self, text: str = "", blocks: list = None):
        """Send a message to Slack via webhook — delegates to Reporter."""
        await self._reporter.send_slack(text=text, blocks=blocks)

    async def _slack_trade_alert(self, paper_trade: PaperTrade, position: CopiedPosition = None):
        """Send Slack alert for a new copied trade — delegates to Reporter."""
        await self._reporter.slack_trade_alert(paper_trade, position)

    async def _slack_exit_alert(self, position: CopiedPosition, pnl: float, reason: str):
        """Send Slack alert when a position is closed — delegates to Reporter."""
        await self._reporter.slack_exit_alert(position, pnl, reason)

    async def _slack_periodic_report(self):
        """Send periodic portfolio report to Slack — delegates to Reporter."""
        await self._reporter.slack_periodic_report()

    # ================================================================
    # EXIT LOGIC: Whale Sell Detection & Market Resolution
    # ================================================================

    def _create_position(self, whale_address: str, whale_name: str, trade: dict,
                         our_size: float, our_shares: float) -> CopiedPosition:
        """Create a tracked position — delegates to PositionManager."""
        return self._position_manager.create_position(
            CopiedPosition, whale_address, whale_name, trade, our_size, our_shares
        )

    def _check_for_whale_sells(self, trade: dict) -> Optional[dict]:
        """Check if this trade is a SELL from a copied whale — delegates to TradeEvaluator."""
        return self._trade_evaluator.check_for_whale_sells(trade)

    async def _check_open_position_sells(self):
        """Poll tracked wallets for sell signals — delegates to TradeEvaluator."""
        await self._trade_evaluator.check_open_position_sells(PaperTrade)

    async def _execute_copy_sell(self, signal: dict):
        """Sell our position when whale sells — delegates to PositionManager."""
        await self._position_manager.execute_copy_sell(signal, PaperTrade)

    async def _execute_live_sell(self, position, price: float, reason: str) -> bool:
        """Execute a live SELL order — delegates to PositionManager."""
        return await self._position_manager.execute_live_sell(position, price, reason)

    async def _check_market_resolutions(self):
        """Check if any markets with open positions have resolved — delegates to PositionManager."""
        await self._position_manager.check_market_resolutions()

    async def _fetch_current_price(self, token_id: str) -> Optional[float]:
        """Fetch current price for a token via recent trades."""
        return await self._market_data.fetch_price(token_id)

    async def _fetch_market_data(self, condition_id: str) -> Optional[dict]:
        """Fetch market details from CLOB API (with Gamma API fallback)."""
        return await self._market_data.fetch_market(condition_id)

    def _is_market_resolved(self, market_data: dict) -> tuple:
        """Determine if a market has resolved and what the outcome is."""
        return self._market_data.is_resolved(market_data)

    async def _close_position_at_resolution(self, pos_id: str, winning_outcome: str, market_data: dict):
        """Close a position when market resolves — delegates to PositionManager."""
        await self._position_manager._close_position_at_resolution(pos_id, winning_outcome, market_data)

    def _record_whale_copy_pnl(self, whale_address: str, pnl: float):
        """Record realized P&L for a copy from a specific whale."""
        self._whale_manager.record_copy_pnl(whale_address, pnl)

    def _is_whale_pruned(self, whale_address: str) -> bool:
        """Check if a whale has been pruned due to poor copy performance."""
        return self._whale_manager.is_pruned(whale_address)

    def _conviction_size(self, whale: WhaleWallet, trade_value: float) -> float:
        """Calculate conviction-weighted position size."""
        return self._whale_manager.conviction_size(whale, trade_value)

    def _calculate_unrealized_pnl(self) -> float:
        """Calculate unrealized P&L — delegates to PositionManager."""
        return self._position_manager.calculate_unrealized_pnl()

    def _get_portfolio_stats(self) -> dict:
        """Calculate portfolio statistics — delegates to PositionManager."""
        return self._position_manager.get_portfolio_stats()

    def _get_report_data(self) -> dict:
        """Provide report data for the Reporter module."""
        return {
            "copied_positions": self._position_manager.positions,
            "current_prices": self._position_manager._current_prices,
            "whale_copies_count": self._position_manager._whale_copies_count,
            "unusual_copies_count": self._position_manager._unusual_copies_count,
            "arb_copies_count": self._position_manager._arb_copies_count,
            "whale_copy_pnl": self._whale_copy_pnl,
            "pruned_whales": self._pruned_whales,
            "whales": self.whales,
            "all_whales": self._whale_manager._all_whales if self._whale_manager else {},
            "live_trader": self._live_trader,
            "start_time": self._start_time,
        }

    async def _periodic_report(self):
        """Print status report every 5 minutes — delegates to Reporter."""
        await self._reporter.periodic_report(lambda: self._running)

    def _print_final_report(self):
        """Print final summary — delegates to Reporter."""
        self._reporter.print_final_report()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Whale Copy Trader - Copy profitable Polymarket wallets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Live Trading Modes:
  --live              Enable live trading (dry run mode - logs but doesn't execute)
  --live --live-real  Enable live trading with REAL orders (uses real money!)

Examples:
  python run_whale_copy_trader.py                           # Paper trading only
  python run_whale_copy_trader.py --live                    # Paper + live dry run
  python run_whale_copy_trader.py --live --live-real        # Paper + REAL trading
  python run_whale_copy_trader.py --live --live-max-trade 5 # Live with $5 max per trade
        """
    )

    # Paper trading options
    parser.add_argument("--max-trade", type=float, default=1.0,
                        help="Max $ per paper trade (default: 1.0)")
    parser.add_argument("--max-total", type=float, default=10000.0,
                        help="Max total paper exposure (default: 10000.0)")

    # Live trading options
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading (dry run mode by default)")
    parser.add_argument("--live-real", action="store_true",
                        help="Execute REAL trades (requires --live, uses real money!)")
    parser.add_argument("--live-max-trade", type=float, default=5.0,
                        help="Max $ per live trade (default: 5.0)")
    parser.add_argument("--live-max-exposure", type=float, default=50.0,
                        help="Max total live exposure (default: 50.0)")

    return parser.parse_args()


async def main():
    args = parse_args()

    # === TRADING_MODE env var support (overrides CLI flags) ===
    trading_mode = os.getenv("TRADING_MODE", "").lower()
    if trading_mode:
        if trading_mode == "paper":
            args.live = False
            args.live_real = False
        elif trading_mode == "dry_run":
            args.live = True
            args.live_real = False
        elif trading_mode == "live":
            args.live = True
            args.live_real = True
        else:
            logger.error(f"Invalid TRADING_MODE: '{trading_mode}'. Use: paper, dry_run, or live")
            return
        logger.info(f"TRADING_MODE env var set to: {trading_mode}")

    # Read live trading limits from env vars (with CLI fallbacks)
    args.live_max_trade = float(os.getenv("LIVE_MAX_TRADE", args.live_max_trade))
    args.live_max_exposure = float(os.getenv("LIVE_MAX_EXPOSURE", args.live_max_exposure))

    # Validate live trading args
    if args.live_real and not args.live:
        logger.error("--live-real requires --live flag")
        return

    # Warn about real trading
    if args.live and args.live_real:
        logger.warning("=" * 60)
        logger.warning("⚠️  LIVE TRADING MODE WITH REAL MONEY")
        logger.warning("=" * 60)
        logger.warning(f"Max per trade: ${args.live_max_trade:.2f}")
        logger.warning(f"Max exposure: ${args.live_max_exposure:.2f}")
        logger.warning("Real orders will be submitted to Polymarket!")
        logger.warning("Press Ctrl+C within 5 seconds to cancel...")
        logger.warning("=" * 60)
        await asyncio.sleep(5)
        logger.info("Proceeding with live trading...")

    trader = WhaleCopyTrader(
        max_per_trade=args.max_trade,
        max_total_exposure=args.max_total,
        # Live trading config
        live_trading_enabled=args.live,
        live_max_per_trade=args.live_max_trade,
        live_max_exposure=args.live_max_exposure,
        live_dry_run=not args.live_real,  # Dry run unless --live-real is set
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
