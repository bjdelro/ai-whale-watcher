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

        # Whale manager (initialized in start() when session is available)
        self._whale_manager: Optional[WhaleManager] = None
        # Backwards-compatible property — populated by whale manager
        self.whales: Dict[str, WhaleWallet] = {w.address.lower(): w for w in FALLBACK_WHALES}

        # State
        self._running = False
        self._session: aiohttp.ClientSession = None
        self._seen_tx_hashes: Set[str] = set()
        self._paper_trades: List[PaperTrade] = []
        self._start_time = datetime.now(timezone.utc)
        self._polls_completed = 0
        self._total_exposure = 0.0

        # Price tracking for P&L
        self._entry_prices: Dict[str, float] = {}  # asset_id -> entry_price
        self._current_prices: Dict[str, float] = {}  # asset_id -> current_price

        # Track wallet history for unusual activity detection
        self._wallet_history: Dict[str, List[float]] = {}  # wallet -> list of trade sizes
        self._unusual_activity_count = 0

        # Cluster and hedge detection
        self._cluster_detector = ClusterDetector()

        # Arbitrage scanner
        self._arbitrage_scanner: Optional[IntraMarketArbitrage] = None
        self._arbitrage_opportunities: List[ArbitrageOpportunity] = []
        self._last_arbitrage_scan = datetime.min.replace(tzinfo=timezone.utc)
        self._arbitrage_scan_interval = 60  # Scan for arbitrage every 60 seconds
        self._arbitrage_found_count = 0

        # === EXIT LOGIC: Position tracking ===
        # Track copied positions with whale association for exit logic
        self._copied_positions: Dict[str, CopiedPosition] = {}
        self._position_counter = 0  # For generating unique position IDs

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

        # Market data client (initialized in start() when session is available)
        self._market_data: Optional[MarketDataClient] = None

        # Market resolution tracking
        self._last_resolution_check = datetime.min.replace(tzinfo=timezone.utc)
        self._resolution_check_interval = 60  # Check for resolutions every 60 seconds
        self._markets_checked: Set[str] = set()  # Track which markets we've fetched details for
        self._market_cache: Dict[str, dict] = {}  # Cache market details (legacy, used by _market_data)

        # P&L tracking (paper mode)
        self._realized_pnl = 0.0
        self._positions_closed = 0
        self._positions_won = 0
        self._positions_lost = 0

        # Trade counters (used in both paper and live mode for reporting)
        self._whale_copies_count = 0
        self._unusual_copies_count = 0
        self._arb_copies_count = 0

        # Per-whale copy P&L — proxies to WhaleManager (set up in start())
        # These references are updated after WhaleManager is initialized
        self._whale_copy_pnl: Dict[str, dict] = {}
        self._pruned_whales: Set[str] = set()

    async def start(self):
        """Initialize and start the copy trader"""
        # Restore saved state from disk (positions, P&L, seen trades)
        self._load_state()

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._market_data = MarketDataClient(self._session)

        # Initialize whale manager with callback for open position whales
        def _get_open_position_whales():
            return {
                pos.whale_address.lower()
                for pos in self._copied_positions.values()
                if pos.status == "open"
            }
        self._whale_manager = WhaleManager(
            session=self._session,
            max_per_trade=self.max_per_trade,
            get_open_position_whales=_get_open_position_whales,
        )
        # Restore whale manager state from loaded state file
        self._whale_manager.from_dict({
            "active_whale_count": getattr(self, "_loaded_active_whale_count", 8),
            "whale_copy_pnl": self._whale_copy_pnl,
            "pruned_whales": list(self._pruned_whales),
        })
        # Point proxy references to whale manager's state
        self._whale_copy_pnl = self._whale_manager._whale_copy_pnl
        self._pruned_whales = self._whale_manager._pruned_whales
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

        # Reconcile positions against current market state
        await self._reconcile_positions()

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
        self._save_state()
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
                    if tx_hash in self._seen_tx_hashes:
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
                                        self._seen_tx_hashes.add(tx_hash)
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
            f"seen_cache={len(self._seen_tx_hashes)}"
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
                    body = await resp.text()
                    logger.warning(f"⚠️ Unusual activity scan failed: HTTP {resp.status} - {body[:200]}")
                    return 0
                trades = await resp.json()

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")

                # Skip if we've already processed this trade
                if tx_hash in self._seen_tx_hashes:
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

        # DEDUP: Skip if we already have an open position for this wallet + market
        for pos in self._copied_positions.values():
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
        if self._total_exposure >= max_exposure:
            logger.info(f"   ⚠️ Max exposure reached (${self._total_exposure:.2f}/${max_exposure:.0f}), skipping")
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

        self._entry_prices[asset_id] = price
        self._unusual_copies_count += 1

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
                self._total_exposure += our_size_usd
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
            our_size_usd = min(self.max_per_trade, self.max_total_exposure - self._total_exposure)
            our_shares = our_size_usd / price if price > 0 else 0

            position = self._create_position(
                whale_address=wallet,
                whale_name=f"UNUSUAL:{name}",
                trade=trade,
                our_size=our_size_usd,
                our_shares=our_shares,
            )
            self._total_exposure += our_size_usd

            paper_trade = PaperTrade(
                timestamp=datetime.now(timezone.utc).isoformat(),
                whale_address=wallet, whale_name=f"UNUSUAL:{name}",
                side=side, outcome=outcome, price=price,
                whale_size=whale_size, our_size=our_size_usd,
                our_shares=our_shares, market_title=title,
                condition_id=condition_id, asset_id=asset_id, tx_hash=tx_hash,
            )
            self._paper_trades.append(paper_trade)
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
        if self._total_exposure >= max_exposure:
            logger.info(f"   Max exposure reached, skipping arbitrage")
            return

        # For arbitrage, we buy $1 worth of each outcome set
        # This guarantees $1 payout regardless of which wins
        our_size = min(self.max_per_trade, max_exposure - self._total_exposure)

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

            self._paper_trades.append(paper_trade)
            self._save_trade(paper_trade)

        self._total_exposure += our_size
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
        """
        Check if we already hold an open position on this market with a DIFFERENT outcome.
        Prevents the bot from betting both sides of the same market from different whales.

        Returns True if a conflicting position exists (caller should skip the trade).
        """
        for pos in self._copied_positions.values():
            if (pos.status == "open" and
                    pos.market_id == market_id and
                    pos.outcome.lower() != outcome.lower()):
                logger.info(
                    f"   ⚔️ Cross-whale conflict: {whale_name} wants {outcome}, "
                    f"but already holding {pos.outcome} from {pos.whale_name} "
                    f"on '{pos.market_title[:40]}...' — skipping"
                )
                return True
        return False

    async def _evaluate_trade(self, whale: WhaleWallet, trade: dict):
        """Evaluate a whale trade and decide if we should copy it"""
        # PRUNING: Skip whales with poor copy track record
        if self._is_whale_pruned(whale.address):
            logger.debug(f"   ⏭️ {whale.name}: pruned (poor copy P&L), skipping")
            return

        side = trade.get("side", "")
        size = trade.get("size", 0)
        price = trade.get("price", 0)
        title = trade.get("title", "Unknown")
        outcome = trade.get("outcome", "")
        condition_id = trade.get("conditionId", "")
        asset_id = trade.get("asset", "")
        tx_hash = trade.get("transactionHash", "")

        trade_value = size * price

        # FILTER 0: Skip stale trades — tighter 120s window for copy freshness
        # (the main poll loop already filters at 300s; this is stricter for copy decisions)
        MAX_COPY_AGE_SECONDS = 120  # Only copy trades < 2 minutes old
        trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
        if not trade_timestamp:
            # No timestamp - skip to be safe (can't verify it's recent)
            if self._polls_completed <= 2:
                logger.debug(f"   ⏭️ {whale.name}: skipped (no timestamp) tx={tx_hash[:12]}...")
            return
        try:
            # Handle Unix timestamp (seconds or milliseconds)
            if isinstance(trade_timestamp, (int, float)) or str(trade_timestamp).isdigit():
                ts = float(trade_timestamp)
                if ts > 1e12:  # Milliseconds
                    ts = ts / 1000
                trade_time = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                # Parse ISO format string
                from dateutil.parser import parse as parse_date
                trade_time = parse_date(str(trade_timestamp))
                # Convert to UTC if timezone-aware
                if trade_time.tzinfo:
                    import pytz
                    trade_time = trade_time.astimezone(pytz.UTC).replace(tzinfo=None)

            age_seconds = (datetime.now(timezone.utc) - trade_time).total_seconds()
            if age_seconds > MAX_COPY_AGE_SECONDS:
                if self._polls_completed <= 2:
                    logger.info(
                        f"   ⏭️ {whale.name}: skipped STALE trade ({age_seconds:.0f}s ago, >{MAX_COPY_AGE_SECONDS}s) "
                        f"| {side} ${trade_value:,.0f} {outcome} @ {price:.0%} | {title[:35]}"
                    )
                return
        except Exception as e:
            # Can't parse timestamp - skip to be safe
            logger.debug(f"Skipping trade with unparseable timestamp: {trade_timestamp} ({e})")
            return

        # Skip small trades
        if trade_value < self.MIN_WHALE_TRADE_SIZE:
            if self._polls_completed <= 2:
                logger.info(f"   ⏭️ {whale.name}: skipped SMALL trade (${trade_value:,.0f} < ${self.MIN_WHALE_TRADE_SIZE})")
            return

        # Log the whale trade
        logger.info(
            f"🐋 WHALE TRADE: {whale.name} "
            f"{side} ${trade_value:,.0f} of {outcome} @ {price:.1%} "
            f"- {title[:50]}..."
        )

        # DEDUP: Skip if we already have an open position for this whale + market
        for pos in self._copied_positions.values():
            if (pos.status == "open" and
                pos.whale_address.lower() == whale.address.lower() and
                pos.market_id == condition_id):
                logger.info(f"   ⏭️ Skipping: already have open position from {whale.name} on this market")
                return

        # CROSS-WHALE CONFLICT: Skip if we hold an opposing position from ANY whale
        if self._has_conflicting_position(condition_id, outcome, whale.name):
            return

        # FILTER 1: Skip extreme prices (already decided markets)
        if self.AVOID_EXTREME_PRICES:
            if price < self.EXTREME_PRICE_THRESHOLD or price > (1 - self.EXTREME_PRICE_THRESHOLD):
                logger.info(f"   ⏭️ Skipping: extreme price ({price:.1%}) - market likely decided")
                return

        # FILTER 2: Check if we have room for more exposure (unified: always use _total_exposure)
        max_exposure = self.live_max_exposure if self.live_trading_enabled else self.max_total_exposure
        if self._total_exposure >= max_exposure:
            logger.info(f"   ⚠️ Max exposure reached (${self._total_exposure:.2f}/${max_exposure:.0f}), skipping")
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

        # PRICE-SLIPPAGE GATE: Check if price has moved too far from whale's entry
        asset_id = trade.get("asset", "")
        if asset_id and side == "BUY":
            current_price = await self._fetch_current_price(asset_id)
            if current_price is not None:
                slippage = current_price - price
                slippage_pct = slippage / price if price > 0 else 0
                if slippage_pct > 0.03:  # Price moved >3% above whale's entry
                    logger.info(
                        f"   ⏭️ Skipping: price slippage too high "
                        f"(whale @ {price:.1%}, now @ {current_price:.1%}, "
                        f"+{slippage_pct:.1%} slippage) — edge likely gone"
                    )
                    return

        # Copy the trade!
        await self._copy_trade(whale, trade)

    async def _copy_trade(self, whale: WhaleWallet, trade: dict):
        """Execute a copy of the whale's trade — unified flow for paper and live"""
        side = trade.get("side", "")
        price = trade.get("price", 0)
        title = trade.get("title", "Unknown")
        outcome = trade.get("outcome", "")
        condition_id = trade.get("conditionId", "")
        asset_id = trade.get("asset", "")
        tx_hash = trade.get("transactionHash", "")
        whale_size = trade.get("size", 0)

        # Only copy BUY trades (sells are handled by exit logic)
        if side != "BUY":
            logger.info(f"   📝 {side} trade (not copying non-BUY trades)")
            return

        self._entry_prices[asset_id] = price
        whale.trades_copied += 1
        self._whale_copies_count += 1

        # === UNIFIED FLOW: Submit order (if live), then track position ===
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
                    whale_address=whale.address,
                    whale_name=whale.name,
                    trade=trade,
                    our_size=our_size_usd,
                    our_shares=our_shares,
                )
                # Populate live fields
                position.live_shares = order.size
                position.live_cost_usd = order.cost_usd
                position.live_order_id = order.order_id
                # Update exposure
                self._total_exposure += our_size_usd
                self._save_state()

                mode = "DRY RUN" if self._live_trader.dry_run else "LIVE"
                logger.info(
                    f"   📝 {mode} BUY: ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                    f"({our_shares:.2f} shares) [Position: {position.position_id}]"
                )

                # Send Slack alert
                paper_trade = PaperTrade(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    whale_address=whale.address, whale_name=whale.name,
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
            # PAPER MODE: Create position with conviction-weighted sizing
            conviction_size = self._conviction_size(whale, whale_size * price)
            remaining = self.max_total_exposure - self._total_exposure
            our_size_usd = min(conviction_size, remaining)
            our_shares = our_size_usd / price if price > 0 else 0

            position = self._create_position(
                whale_address=whale.address,
                whale_name=whale.name,
                trade=trade,
                our_size=our_size_usd,
                our_shares=our_shares,
            )
            self._total_exposure += our_size_usd

            paper_trade = PaperTrade(
                timestamp=datetime.now(timezone.utc).isoformat(),
                whale_address=whale.address, whale_name=whale.name,
                side=side, outcome=outcome, price=price,
                whale_size=whale_size, our_size=our_size_usd,
                our_shares=our_shares, market_title=title,
                condition_id=condition_id, asset_id=asset_id, tx_hash=tx_hash,
            )
            self._paper_trades.append(paper_trade)
            self._save_trade(paper_trade)

            logger.info(
                f"   📝 PAPER BUY: ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                f"({our_shares:.2f} shares) [Position: {position.position_id}]"
            )
            await self._slack_trade_alert(paper_trade, position)

    async def _execute_live_buy(
        self,
        token_id: str,
        price: float,
        market_title: str,
    ) -> Optional[LiveOrder]:
        """
        Execute a live BUY order on Polymarket.
        Returns the LiveOrder on success, or None on failure.
        Does NOT track positions — caller handles that.

        Checks exposure BEFORE making any API calls to avoid unnecessary requests.
        """
        if not self._live_trader:
            return None

        # Check exposure FIRST — before any API calls to Polymarket
        remaining_exposure = self.live_max_exposure - self._total_exposure
        if remaining_exposure <= 0:
            logger.info(
                f"   💰 LIVE: Skipping - max exposure reached "
                f"(${self._total_exposure:.2f}/${self.live_max_exposure:.0f})"
            )
            return None

        try:
            # Randomize order size between 1.20 and 1.60 to avoid bot detection
            randomized_size = round(random.uniform(1.20, 1.60), 2)
            live_size_usd = min(randomized_size, remaining_exposure)

            if live_size_usd <= 0:
                # Before giving up, check actual USDC balance on Polymarket
                # Internal tracking can drift if positions were closed externally
                actual_balance = self._live_trader.get_collateral_balance()
                if actual_balance is not None and actual_balance > 0:
                    logger.info(
                        f"   💰 LIVE: Internal tracking says exposure maxed "
                        f"(${self._total_exposure:.2f}/${self.live_max_exposure:.2f}), "
                        f"but actual USDC balance is ${actual_balance:.2f}. Resetting tracker."
                    )
                    # Recalibrate exposure from actual balance
                    self._total_exposure = max(
                        0.0, self.live_max_exposure - actual_balance
                    )
                    remaining_exposure = self.live_max_exposure - self._total_exposure
                    live_size_usd = min(randomized_size, remaining_exposure)
                if live_size_usd <= 0:
                    logger.info(f"   💰 LIVE: Skipping - max exposure reached")
                    return None

            # Sync LiveTrader's exposure tracking with main bot before submitting
            self._live_trader._total_exposure = self._total_exposure

            # Submit the order
            order = await self._live_trader.submit_buy_order(
                token_id=token_id,
                price=price,
                size_usd=live_size_usd,
                market_title=market_title,
            )

            if order and order.status == "failed":
                logger.warning(
                    f"   ❌ LIVE ORDER FAILED: @ {price:.1%} | "
                    f"{order.error_message or 'unknown error'} | {market_title[:30]}..."
                )
            elif not order:
                logger.warning(f"   ❌ LIVE: No order returned for {market_title[:30]}...")

            return order

        except Exception as e:
            logger.error(f"   💰 LIVE: Error executing buy: {e}")
            return None

    def _save_trade(self, trade: PaperTrade):
        """Save trade to JSONL file"""
        os.makedirs("paper_trades", exist_ok=True)
        filename = f"paper_trades/whale_copies_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"

        with open(filename, "a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")

    # ================================================================
    # POSITION PERSISTENCE (survives restarts/redeploys)
    # ================================================================

    def _save_state(self):
        """Save positions and P&L to disk so they survive restarts"""
        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "positions": {pid: asdict(pos) for pid, pos in self._copied_positions.items()},
            "position_counter": self._position_counter,
            "realized_pnl": self._realized_pnl,
            "positions_closed": self._positions_closed,
            "positions_won": self._positions_won,
            "positions_lost": self._positions_lost,
            "total_exposure": self._total_exposure,
            "active_whale_count": self._whale_manager.active_whale_count if self._whale_manager else 8,
            "seen_tx_hashes": list(self._seen_tx_hashes)[-2000:],  # Keep last 2000
            "entry_prices": self._entry_prices,
            "whale_copy_pnl": self._whale_manager._whale_copy_pnl if self._whale_manager else self._whale_copy_pnl,
            "pruned_whales": list(self._whale_manager._pruned_whales if self._whale_manager else self._pruned_whales),
        }

        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            # Write to temp file then rename for atomic write (no corruption)
            tmp_file = self._state_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_file, self._state_file)
            logger.debug(f"State saved: {len(state['positions'])} positions")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_state(self):
        """Load positions and P&L from disk on startup"""
        if not os.path.exists(self._state_file):
            logger.info("No saved state found — starting fresh")
            return

        try:
            with open(self._state_file, "r") as f:
                state = json.load(f)

            # Restore positions
            for pid, pos_dict in state.get("positions", {}).items():
                self._copied_positions[pid] = CopiedPosition(**pos_dict)

            # Restore counters
            self._position_counter = state.get("position_counter", 0)
            self._realized_pnl = state.get("realized_pnl", 0.0)
            self._positions_closed = state.get("positions_closed", 0)
            self._positions_won = state.get("positions_won", 0)
            self._positions_lost = state.get("positions_lost", 0)
            self._total_exposure = state.get("total_exposure", 0.0)
            self._loaded_active_whale_count = state.get("active_whale_count", 8)
            self._seen_tx_hashes = set(state.get("seen_tx_hashes", []))
            self._entry_prices = state.get("entry_prices", {})
            self._whale_copy_pnl = state.get("whale_copy_pnl", {})
            self._pruned_whales = set(state.get("pruned_whales", []))

            open_count = len([p for p in self._copied_positions.values() if p.status == "open"])
            closed_count = len([p for p in self._copied_positions.values() if p.status == "closed"])
            saved_at = state.get("saved_at", "unknown")

            # Recalculate realized P&L from closed positions using cost-basis accounting
            # This corrects any accumulated error from the old formula
            recalculated_pnl = 0.0
            for p in self._copied_positions.values():
                if p.status == "closed" and p.exit_price is not None:
                    eff_shares = p.live_shares or p.shares
                    cost = p.live_cost_usd or p.copy_amount_usd
                    recalculated_pnl += (p.exit_price * eff_shares) - cost
            if abs(recalculated_pnl - self._realized_pnl) > 0.01:
                logger.info(
                    f"P&L recalculated from closed positions: "
                    f"${self._realized_pnl:+.2f} → ${recalculated_pnl:+.2f}"
                )
                self._realized_pnl = recalculated_pnl

            mode_label = "LIVE" if self.live_trading_enabled else "PAPER"
            logger.info(
                f"State restored from {saved_at} [{mode_label}]: "
                f"{open_count} open positions, {closed_count} closed, "
                f"P&L: ${self._realized_pnl:+.2f}, "
                f"exposure: ${self._total_exposure:.2f}"
            )

        except Exception as e:
            logger.error(f"Failed to load state: {e} — starting fresh")

    async def _reconcile_positions(self):
        """
        Reconcile saved positions against current market state on startup.

        Steps:
        0. (Live) Recover falsely-closed positions by checking on-chain balances
        1. Check if any markets resolved while bot was down
        2. Check if whales sold while bot was down
        3. Fetch current prices for unrealized P&L
        4. (Live) Verify on-chain balances / close zero-balance positions
        5. Recalculate exposure from actual open positions
        6. Log reconciliation report
        7. Persist state
        """
        # --- Step 0: Recover falsely-closed positions (live mode only) ---
        # If a position is marked "closed" but still has on-chain balance,
        # it was falsely closed (e.g., by the midnight resolution bug).
        # Re-open it so we can properly track and exit it.
        recovered_count = 0
        if self.live_trading_enabled and self._live_trader and self._live_trader._client:
            closed_positions = {
                pid: pos for pid, pos in self._copied_positions.items()
                if pos.status == "closed" and pos.token_id
            }

            if closed_positions:
                logger.info(
                    f"🔍 Checking {len(closed_positions)} closed positions for on-chain balances..."
                )

                for pos_id, pos in closed_positions.items():
                    try:
                        params = BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=pos.token_id,
                            signature_type=2,
                        )
                        balance_info = self._live_trader._client.get_balance_allowance(params)
                        balance = float(balance_info.get("balance", 0)) if balance_info else 0
                        balance_shares = balance / 1e6 if balance > 100 else balance

                        if balance_shares > 0.001:
                            # Still has on-chain balance! This was falsely closed.
                            old_reason = pos.exit_reason
                            old_pnl = pos.pnl or 0

                            # Reverse the P&L and counters from the false close
                            self._realized_pnl -= old_pnl
                            self._positions_closed -= 1
                            if old_pnl > 0:
                                self._positions_won -= 1
                            elif old_pnl < 0:
                                self._positions_lost -= 1

                            # Re-open the position
                            pos.status = "open"
                            pos.exit_price = None
                            pos.exit_time = None
                            pos.exit_reason = None
                            pos.pnl = None
                            pos.live_shares = balance_shares

                            recovered_count += 1
                            cost = pos.live_cost_usd or pos.copy_amount_usd
                            logger.info(
                                f"   ♻️ RECOVERED: {pos.market_title[:45]}... "
                                f"| {balance_shares:.2f} shares on-chain "
                                f"| Was falsely closed as '{old_reason}' "
                                f"| Cost: ${cost:.2f}"
                            )

                    except Exception as e:
                        logger.debug(
                            f"   Could not check balance for closed pos {pos.market_title[:30]}...: {e}"
                        )

                    await asyncio.sleep(0.2)  # Rate limit

                if recovered_count > 0:
                    logger.info(
                        f"   ♻️ Recovered {recovered_count} falsely-closed positions"
                    )

        open_positions = {
            pid: pos for pid, pos in self._copied_positions.items()
            if pos.status == "open"
        }

        if not open_positions:
            logger.info("Reconciliation: no open positions to reconcile")
            return

        logger.info(f"🔄 Reconciling {len(open_positions)} open positions...")

        resolved_count = 0
        price_updated = 0
        live_warnings = 0

        # --- Step 1: Check if markets resolved while we were down ---
        for pos_id, pos in list(open_positions.items()):
            try:
                market_data = await self._fetch_market_data(pos.market_id)
                if not market_data:
                    logger.warning(
                        f"   ⚠️ Could not fetch market data for {pos.market_title[:40]}..."
                    )
                    await asyncio.sleep(0.2)
                    continue

                is_resolved, winning_outcome = self._is_market_resolved(market_data)
                if is_resolved:
                    logger.info(
                        f"   🏁 Market resolved while down: {pos.market_title[:40]}... "
                        f"→ Winner: {winning_outcome or 'unknown'}"
                    )
                    await self._close_position_at_resolution(
                        pos_id, winning_outcome, market_data
                    )
                    resolved_count += 1
                    # Remove from our working set since it's now closed
                    del open_positions[pos_id]

            except Exception as e:
                logger.warning(f"   ⚠️ Error checking resolution for {pos.market_title[:30]}...: {e}")

            await asyncio.sleep(0.2)  # Rate limit API calls

        # --- Step 2: Check if whales sold positions while we were down ---
        whale_sold_count = 0
        if open_positions:
            # Group open positions by whale address
            whale_positions: Dict[str, List[str]] = {}
            for pos_id, pos in open_positions.items():
                whale_positions.setdefault(pos.whale_address, []).append(pos_id)

            logger.info(f"   🐋 Checking {len(whale_positions)} whale wallets for sells while down...")

            for whale_addr, pos_ids in whale_positions.items():
                try:
                    # Fetch more trades than usual to catch sells from while bot was down
                    trades = await self._fetch_whale_trades(whale_addr, limit=50)
                    sell_trades = [t for t in trades if t.get("side") == "SELL"]

                    for sell_trade in sell_trades:
                        sell_token = sell_trade.get("asset", "")
                        sell_price = sell_trade.get("price", 0)
                        sell_size = sell_trade.get("size", 0)

                        # Check if this sell matches any of our open positions
                        for pos_id in list(pos_ids):
                            pos = self._copied_positions.get(pos_id)
                            if not pos or pos.status != "open":
                                continue
                            if pos.token_id == sell_token:
                                logger.info(
                                    f"   🐋 Whale sold while down: {pos.whale_name} exited "
                                    f"{pos.market_title[:40]}... @ {sell_price:.1%}"
                                )
                                # Close our position at the whale's sell price
                                effective_shares = pos.live_shares or pos.shares
                                pnl = (sell_price - pos.entry_price) * effective_shares

                                pos.status = "closed"
                                pos.exit_price = sell_price
                                pos.exit_time = datetime.now(timezone.utc).isoformat()
                                pos.exit_reason = "whale_sold"
                                pos.pnl = pnl

                                self._realized_pnl += pnl
                                self._record_whale_copy_pnl(pos.whale_address, pnl)
                                self._positions_closed += 1
                                if pnl > 0:
                                    self._positions_won += 1
                                elif pnl < 0:
                                    self._positions_lost += 1

                                pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                                logger.info(
                                    f"   📤 Closing (whale exited while down): {pos.outcome} "
                                    f"@ {sell_price:.1%} | Entry: {pos.entry_price:.1%} "
                                    f"| P&L: {pnl_emoji} ${pnl:+.2f}"
                                )

                                # Execute live sell if in live mode
                                if self.live_trading_enabled and self._live_trader:
                                    sell_ok = await self._execute_live_sell(pos, sell_price, "whale_sold")
                                    if not sell_ok:
                                        logger.warning(
                                            f"   ⚠️ Live sell failed on reconciliation for "
                                            f"{pos.market_title[:30]}..."
                                        )

                                await self._slack_exit_alert(pos, pnl, "Whale Sold (while down)")

                                whale_sold_count += 1
                                del open_positions[pos_id]
                                pos_ids.remove(pos_id)
                                break  # One sell per token match

                except Exception as e:
                    logger.warning(f"   ⚠️ Error checking whale sells for {whale_addr[:12]}...: {e}")

                await asyncio.sleep(0.3)  # Rate limit

        # --- Step 3: Fetch current prices for remaining open positions ---
        token_ids = {pos.token_id for pos in open_positions.values()}
        for token_id in token_ids:
            try:
                current_price = await self._fetch_current_price(token_id)
                if current_price is not None:
                    self._current_prices[token_id] = current_price
                    price_updated += 1
            except Exception as e:
                logger.debug(f"   Could not fetch price for token {token_id[:20]}...: {e}")

            await asyncio.sleep(0.2)  # Rate limit

        # --- Step 3b: Stop-loss / Take-profit / Stale position cleanup ---
        sl_tp_count = 0
        now = datetime.now(timezone.utc)
        for pos_id in list(open_positions.keys()):
            pos = self._copied_positions.get(pos_id)
            if not pos or pos.status != "open":
                continue

            current_price = self._current_prices.get(pos.token_id)
            if current_price is None:
                continue

            effective_shares = pos.live_shares or pos.shares
            cost = pos.live_cost_usd or pos.copy_amount_usd
            pnl = (current_price * effective_shares) - cost
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0

            exit_reason = None

            # Stop-loss: close if down more than threshold from entry
            if pnl_pct <= -self.STOP_LOSS_PCT:
                exit_reason = "stop_loss"
            # Take-profit: close if up more than threshold from entry
            elif pnl_pct >= self.TAKE_PROFIT_PCT:
                exit_reason = "take_profit"
            # Stale position: close if old and hasn't moved meaningfully
            else:
                try:
                    entry_time = datetime.fromisoformat(pos.entry_time)
                    if entry_time.tzinfo is None:
                        entry_time = entry_time.replace(tzinfo=timezone.utc)
                    hours_held = (now - entry_time).total_seconds() / 3600
                    if hours_held >= self.STALE_POSITION_HOURS and abs(pnl_pct) < 0.02:
                        exit_reason = "stale_position"
                except (ValueError, TypeError):
                    pass

            if exit_reason:
                pos.status = "closed"
                pos.exit_price = current_price
                pos.exit_time = now.isoformat()
                pos.exit_reason = exit_reason
                pos.pnl = pnl

                self._realized_pnl += pnl
                self._record_whale_copy_pnl(pos.whale_address, pnl)
                self._total_exposure -= cost
                if self._total_exposure < 0:
                    self._total_exposure = 0
                self._positions_closed += 1
                if pnl > 0:
                    self._positions_won += 1
                elif pnl < 0:
                    self._positions_lost += 1

                reason_emoji = {"stop_loss": "🛑", "take_profit": "🎯", "stale_position": "⏰"}
                pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                logger.info(
                    f"   {reason_emoji.get(exit_reason, '📤')} {exit_reason.upper()}: "
                    f"{pos.outcome} @ {pos.entry_price:.1%}→{current_price:.1%} "
                    f"| P&L: {pnl_emoji} ${pnl:+.2f} ({pnl_pct:+.1%}) "
                    f"| {pos.market_title[:35]}..."
                )

                # Execute live sell if applicable
                if self.live_trading_enabled and self._live_trader:
                    await self._execute_live_sell(pos, current_price, exit_reason)

                await self._slack_exit_alert(pos, pnl, exit_reason.replace("_", " ").title())

                sl_tp_count += 1
                del open_positions[pos_id]

        # --- Step 4: Live mode — verify on-chain balances and close zero-balance positions ---
        settled_count = 0
        if self.live_trading_enabled and self._live_trader and self._live_trader._client:
            logger.info("   🔗 Verifying on-chain balances via CLOB API...")
            for pos_id, pos in list(open_positions.items()):
                try:
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=pos.token_id,
                        signature_type=2,
                    )
                    balance_info = self._live_trader._client.get_balance_allowance(params)
                    balance = float(balance_info.get("balance", 0)) if balance_info else 0
                    # Polymarket returns conditional token balance in raw units (6 decimals)
                    balance_shares = balance / 1e6 if balance > 100 else balance

                    if balance_shares <= 0.001:
                        # Zero balance — market settled or position was closed externally
                        # Try to determine outcome from current market data
                        market_data = self._market_cache.get(pos.market_id)
                        exit_price = pos.entry_price  # Default: assume break-even
                        result_note = "settled (zero balance)"

                        if market_data:
                            tokens = market_data.get("tokens", [])
                            for t in tokens:
                                if t.get("winner"):
                                    did_win = pos.outcome.lower() == t["outcome"].lower()
                                    exit_price = 1.0 if did_win else 0.0
                                    result_note = f"settled ({'won' if did_win else 'lost'})"
                                    break

                        effective_shares = pos.live_shares or pos.shares
                        pnl = (exit_price - pos.entry_price) * effective_shares

                        pos.status = "closed"
                        pos.exit_price = exit_price
                        pos.exit_time = datetime.now(timezone.utc).isoformat()
                        pos.exit_reason = "settled_zero_balance"
                        pos.pnl = pnl

                        cost = pos.live_cost_usd or pos.copy_amount_usd
                        self._realized_pnl += pnl
                        self._record_whale_copy_pnl(pos.whale_address, pnl)
                        self._total_exposure -= cost
                        if self._total_exposure < 0:
                            self._total_exposure = 0
                        self._positions_closed += 1
                        if pnl > 0:
                            self._positions_won += 1
                        elif pnl < 0:
                            self._positions_lost += 1

                        pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                        logger.info(
                            f"   🏁 {result_note}: {pos.market_title[:40]}... "
                            f"| P&L: {pnl_emoji} ${pnl:+.2f}"
                        )
                        settled_count += 1
                        del open_positions[pos_id]
                    else:
                        # Has balance — update live_shares to match on-chain reality
                        if pos.live_shares and abs(balance_shares - pos.live_shares) > 0.01:
                            logger.info(
                                f"   📊 Balance update: {pos.market_title[:35]}... "
                                f"tracked={pos.live_shares:.2f} → on-chain={balance_shares:.2f}"
                            )
                            pos.live_shares = balance_shares
                except Exception as e:
                    logger.debug(f"   Could not verify balance for {pos.market_title[:30]}...: {e}")

                await asyncio.sleep(0.2)

        # --- Step 5: Recalculate exposure from actual open positions (unified) ---
        remaining_open = [p for p in self._copied_positions.values() if p.status == "open"]
        self._total_exposure = sum(
            (p.live_cost_usd or p.copy_amount_usd) for p in remaining_open
        )

        # --- Step 6: Log reconciliation report ---
        exposure = self._total_exposure
        unrealized = self._calculate_unrealized_pnl()
        logger.info(
            f"✅ Reconciliation complete: "
            f"{resolved_count} resolved, {whale_sold_count} whale-sold, "
            f"{sl_tp_count} SL/TP/stale, "
            f"{settled_count} settled (zero balance), "
            f"{len(remaining_open)} remaining open | "
            f"Exposure: ${exposure:.2f} | "
            f"Unrealized P&L: ${unrealized:+.2f}"
        )

        # Log each remaining open position for visibility
        for pos in remaining_open:
            current_price = self._current_prices.get(pos.token_id, pos.entry_price)
            effective_shares = pos.live_shares or pos.shares
            cost = pos.live_cost_usd or pos.copy_amount_usd
            pos_pnl = (current_price * effective_shares) - cost
            pnl_emoji = "🟢" if pos_pnl > 0 else "🔴" if pos_pnl < 0 else "⚪"
            logger.info(
                f"   📊 OPEN: {pos.market_title[:45]}... "
                f"| {pos.outcome} @ {pos.entry_price:.1%} → {current_price:.1%} "
                f"| Cost: ${cost:.2f} | P&L: {pnl_emoji} ${pos_pnl:+.2f} "
                f"| Whale: {pos.whale_name}"
            )

        # --- Step 7: Persist any changes ---
        if resolved_count > 0 or whale_sold_count > 0 or sl_tp_count > 0 or settled_count > 0:
            self._save_state()

    # ================================================================
    # SLACK ALERTS
    # ================================================================

    async def _send_slack(self, text: str = "", blocks: list = None):
        """Send a message to Slack via webhook"""
        if not self._slack_webhook_url or not self._session:
            return

        payload = {}
        if blocks:
            payload["blocks"] = blocks
        if text:
            payload["text"] = text

        try:
            async with self._session.post(
                self._slack_webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Slack webhook returned {resp.status}")
        except Exception as e:
            logger.debug(f"Slack alert failed: {e}")

    async def _slack_trade_alert(self, paper_trade: PaperTrade, position: CopiedPosition = None):
        """Send Slack alert for a new copied trade"""
        mode = "PAPER"
        if self.live_trading_enabled:
            mode = "DRY RUN" if self.live_dry_run else "LIVE"

        emoji = "🟢" if paper_trade.side == "BUY" else "🔴"
        stats = self._get_portfolio_stats()

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {mode} {paper_trade.side} — Copied {paper_trade.whale_name}", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Market:*\n{paper_trade.market_title[:60]}"},
                    {"type": "mrkdwn", "text": f"*Outcome:*\n{paper_trade.outcome} @ {paper_trade.price:.1%}"},
                    {"type": "mrkdwn", "text": f"*Whale Size:*\n${paper_trade.whale_size:,.0f}"},
                    {"type": "mrkdwn", "text": f"*Our Size:*\n${paper_trade.our_size:.2f}"},
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": (
                    f"Exposure: ${stats['open_exposure']:.2f}/${self.live_max_exposure if self.live_trading_enabled else self.max_total_exposure:.0f} | "
                    f"Open: {stats['open_positions']} | "
                    f"P&L: ${stats['total_pnl']:+.2f} | "
                    f"W/L: {stats['positions_won']}/{stats['positions_lost']}"
                )}]
            },
        ]
        await self._send_slack(blocks=blocks)

    async def _slack_exit_alert(self, position: CopiedPosition, pnl: float, reason: str):
        """Send Slack alert when a position is closed"""
        mode = "PAPER"
        if self.live_trading_enabled:
            mode = "DRY RUN" if self.live_dry_run else "LIVE"

        emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        stats = self._get_portfolio_stats()

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {mode} EXIT — {reason}", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Market:*\n{position.market_title[:60]}"},
                    {"type": "mrkdwn", "text": f"*P&L:*\n${pnl:+.2f}"},
                    {"type": "mrkdwn", "text": f"*Entry:*\n{position.entry_price:.1%}"},
                    {"type": "mrkdwn", "text": f"*Exit:*\n{position.exit_price:.1%}"},
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": (
                    f"Whale: {position.whale_name} | "
                    f"Total P&L: ${stats['total_pnl']:+.2f} | "
                    f"W/L: {stats['positions_won']}/{stats['positions_lost']} | "
                    f"Win Rate: {stats['win_rate']*100:.0f}%"
                )}]
            },
        ]
        await self._send_slack(blocks=blocks)

    async def _slack_periodic_report(self):
        """Send periodic portfolio report to Slack"""
        stats = self._get_portfolio_stats()
        runtime = (datetime.now(timezone.utc) - self._start_time).total_seconds() / 3600
        hourly_return = stats["realized_pnl"] / runtime if runtime > 0 else 0

        mode = "PAPER"
        if self.live_trading_enabled:
            mode = "DRY RUN" if self.live_dry_run else "LIVE"

        usdc_str = f"${stats['usdc_balance']:.2f}" if stats['usdc_balance'] is not None else "N/A"
        total_str = f"${stats['total_value']:.2f}" if stats['total_value'] is not None else "N/A"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🐋 {mode} Portfolio Report ({runtime:.1f}h)", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*USDC Cash:*\n{usdc_str}"},
                    {"type": "mrkdwn", "text": f"*Open Positions:*\n{stats['open_positions']} (${stats['open_market_value']:.2f} mkt val)"},
                    {"type": "mrkdwn", "text": f"*Total Portfolio:*\n{total_str}"},
                    {"type": "mrkdwn", "text": f"*Total P&L:*\n${stats['total_pnl']:+.2f}"},
                ]
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Realized:*\n${stats['realized_pnl']:+.2f}"},
                    {"type": "mrkdwn", "text": f"*Unrealized:*\n${stats['unrealized_pnl']:+.2f}"},
                    {"type": "mrkdwn", "text": f"*Win Rate:*\n{stats['win_rate']*100:.0f}% ({stats['positions_won']}W/{stats['positions_lost']}L)"},
                    {"type": "mrkdwn", "text": f"*Session Rate:*\n${hourly_return:+.2f}/hr"},
                ]
            },
        ]

        # Add live order stats if enabled
        if self._live_trader:
            live_stats = self._live_trader.get_stats()
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": (
                    f"*💰 Live Trading ({live_stats['mode']}):*\n"
                    f"Orders: {live_stats['orders_submitted']} submitted, {live_stats['orders_filled']} filled"
                )}
            })

        await self._send_slack(blocks=blocks)

    # ================================================================
    # EXIT LOGIC: Whale Sell Detection & Market Resolution
    # ================================================================

    def _create_position(self, whale_address: str, whale_name: str, trade: dict,
                         our_size: float, our_shares: float) -> CopiedPosition:
        """Create a tracked position when we copy a whale BUY"""
        self._position_counter += 1
        position_id = f"pos_{self._position_counter}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        position = CopiedPosition(
            position_id=position_id,
            market_id=trade.get("conditionId", ""),
            token_id=trade.get("asset", ""),
            outcome=trade.get("outcome", ""),
            whale_address=whale_address.lower(),
            whale_name=whale_name,
            entry_price=trade.get("price", 0),
            shares=our_shares,
            entry_time=datetime.now(timezone.utc).isoformat(),
            copy_amount_usd=our_size,
            market_title=trade.get("title", "Unknown"),
            status="open",
        )

        self._copied_positions[position_id] = position
        self._save_state()  # Persist new position to disk
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

    async def _check_open_position_sells(self):
        """
        Poll wallets that have open positions but are NOT in the current whale list.
        This catches sells from:
        - Unusual-activity wallets we copied
        - Whales that dropped off the leaderboard since we copied them
        """
        # Collect unique wallet addresses from open positions that aren't tracked whales
        open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
        if not open_positions:
            return

        extra_wallets = set()
        for pos in open_positions:
            if pos.whale_address not in self.whales:
                extra_wallets.add(pos.whale_address)

        if not extra_wallets:
            return

        for wallet_address in extra_wallets:
            try:
                trades = await self._fetch_whale_trades(wallet_address, limit=5)
                for trade in trades:
                    tx_hash = trade.get("transactionHash", "")
                    if tx_hash in self._seen_tx_hashes:
                        continue

                    # Only care about SELLs for exit detection
                    side = trade.get("side", "")
                    if side != "SELL":
                        continue

                    # Check freshness — be more generous here (10 min window)
                    # since these wallets are polled less frequently
                    trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
                    if trade_timestamp:
                        try:
                            ts = float(trade_timestamp)
                            if ts > 1e12:
                                ts = ts / 1000
                            age = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
                            if age > 600:  # 10 minute window
                                continue
                        except (ValueError, TypeError):
                            continue

                    self._seen_tx_hashes.add(tx_hash)

                    # Check if this sell matches any of our open positions
                    sell_signal = self._check_for_whale_sells(trade)
                    if sell_signal:
                        # Find the whale name from the position
                        pos_name = sell_signal["position"].whale_name
                        logger.info(
                            f"🐋 TRACKED WALLET SELLING: {pos_name} exiting position in "
                            f"{sell_signal['position'].market_title[:40]}..."
                        )
                        await self._execute_copy_sell(sell_signal)

                await asyncio.sleep(0.1)  # Rate limit

            except Exception as e:
                logger.debug(f"Error polling tracked wallet {wallet_address[:12]}...: {e}")

    async def _execute_copy_sell(self, signal: dict):
        """Sell our position when whale sells theirs — unified for paper and live"""
        position: CopiedPosition = signal["position"]
        sell_price = signal["whale_sell_price"]

        # Use live_shares if available (from real order), otherwise fall back to paper shares
        effective_shares = position.live_shares or position.shares
        cost = position.live_cost_usd or position.copy_amount_usd

        # Calculate P&L using cost-basis: proceeds - cost
        proceeds = sell_price * effective_shares
        pnl = proceeds - cost

        # === LIVE MODE: Submit sell order first ===
        if self.live_trading_enabled and self._live_trader:
            sell_succeeded = await self._execute_live_sell(position, sell_price, "whale_sold")
            if not sell_succeeded:
                logger.warning(
                    f"   ⚠️ Live sell failed for {position.market_title[:30]}... "
                    f"— marking position closed anyway (whale exited)"
                )

        # Update position (always — needed to prevent re-matching)
        position.status = "closed"
        position.exit_price = sell_price
        position.exit_time = datetime.now(timezone.utc).isoformat()
        position.exit_reason = "whale_sold"
        position.pnl = pnl

        # Update totals (unified — always update from CopiedPosition)
        self._realized_pnl += pnl
        self._record_whale_copy_pnl(position.whale_address, pnl)
        cost = position.live_cost_usd or position.copy_amount_usd
        self._total_exposure -= cost
        if self._total_exposure < 0:
            self._total_exposure = 0  # Safety clamp
        self._positions_closed += 1
        if pnl > 0:
            self._positions_won += 1
        elif pnl < 0:
            self._positions_lost += 1

        self._save_state()

        # Log
        pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        mode_label = "LIVE" if self.live_trading_enabled else "PAPER"
        logger.info(
            f"   📤 {mode_label} SELL (whale exited): {position.outcome} @ {sell_price:.1%} "
            f"| Entry: {position.entry_price:.1%} | P&L: {pnl_emoji} ${pnl:+.2f}"
        )

        # Save exit trade record in paper mode
        if not self.live_trading_enabled:
            exit_trade = PaperTrade(
                timestamp=datetime.now(timezone.utc).isoformat(),
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
                tx_hash=f"exit_whale_{datetime.now(timezone.utc).timestamp()}",
            )
            self._save_trade(exit_trade)

        # Send Slack alert for exit
        await self._slack_exit_alert(position, pnl, "Whale Sold")

    async def _execute_live_sell(
        self,
        position: CopiedPosition,
        price: float,
        reason: str,
    ) -> bool:
        """
        Execute a live SELL order on Polymarket using CopiedPosition data.
        Returns True if sell succeeded, False otherwise.
        """
        if not self._live_trader:
            return False

        # Use live_shares if available, fall back to paper shares
        shares_to_sell = position.live_shares or position.shares
        if not shares_to_sell or shares_to_sell <= 0:
            logger.warning(f"   💰 LIVE: No shares to sell for {position.market_title[:30]}...")
            return False

        try:
            # Sync LiveTrader's exposure tracking with main bot before submitting
            self._live_trader._total_exposure = self._total_exposure

            order = await self._live_trader.submit_sell_order(
                token_id=position.token_id,
                price=price,
                shares=shares_to_sell,
                market_title=position.market_title,
            )

            if order and order.status in ("filled", "dry_run"):
                mode = "DRY RUN" if self._live_trader.dry_run else "LIVE"
                status_emoji = "🔵" if self._live_trader.dry_run else "💰"
                logger.info(
                    f"   {status_emoji} {mode} SELL: {shares_to_sell:.2f} shares @ {price:.1%} "
                    f"({reason})"
                )
                return True
            elif order and order.status == "failed":
                logger.warning(
                    f"   ❌ LIVE SELL FAILED: {order.error_message or 'unknown'} | "
                    f"{position.market_title[:30]}..."
                )
                return False
            else:
                logger.warning(f"   ❌ LIVE: No sell order returned for {position.market_title[:30]}...")
                return False

        except Exception as e:
            logger.error(f"   💰 LIVE: Error executing sell: {e}")
            return False

    async def _check_market_resolutions(self):
        """
        Check if any markets with open positions have resolved.
        Markets resolve when price hits 0% or 100%, or when explicitly closed.

        Uses the trades API to get current prices for our positions.
        """
        now = datetime.now(timezone.utc)

        # Only check periodically
        if (now - self._last_resolution_check).total_seconds() < self._resolution_check_interval:
            return

        self._last_resolution_check = now

        # Get open positions
        open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
        if not open_positions:
            return

        # Check prices via trades API for each unique token
        token_ids = {p.token_id for p in open_positions}

        for token_id in token_ids:
            try:
                current_price = await self._fetch_current_price(token_id)
                if current_price is None:
                    continue

                # Update price cache for P&L calculations
                self._current_prices[token_id] = current_price

                # Check if resolved (price at 0% or 100%)
                # IMPORTANT: Verify with the market API before closing to avoid
                # false resolutions from stale/outlier trade prices
                if current_price >= 0.99 or current_price <= 0.01:
                    # Find a position with this token to get the condition_id
                    condition_id = None
                    for pos in self._copied_positions.values():
                        if pos.token_id == token_id and pos.status == "open":
                            condition_id = pos.market_id
                            break

                    if condition_id:
                        # Double-check with market API before declaring resolved
                        market_data = await self._fetch_market_data(condition_id)
                        if market_data:
                            is_resolved, winning_outcome = self._is_market_resolved(market_data)
                            if is_resolved and winning_outcome:
                                for pos_id, position in list(self._copied_positions.items()):
                                    if position.token_id == token_id and position.status == "open":
                                        await self._close_position_at_resolution(
                                            pos_id, winning_outcome, market_data
                                        )
                            elif is_resolved:
                                logger.info(
                                    f"⚠️ Market {condition_id[:20]}... resolved but no winning outcome — skipping auto-close"
                                )
                            else:
                                logger.debug(
                                    f"Price extreme ({current_price:.2f}) but market not resolved yet for {condition_id[:20]}..."
                                )

            except Exception as e:
                logger.warning(f"Error checking resolution for token {token_id[:20]}...: {e}")

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
        """Close a position when market resolves — unified for paper and live.

        Resolved markets auto-settle on Polymarket, so no sell order is needed.
        We just update our tracking.
        """
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
            exit_price = position.entry_price  # Conservative: assume no change

        # Use live_shares if available, otherwise paper shares
        effective_shares = position.live_shares or position.shares
        cost = position.live_cost_usd or position.copy_amount_usd

        # Calculate P&L using cost-basis: proceeds - cost
        proceeds = exit_price * effective_shares
        pnl = proceeds - cost

        # Update position
        position.status = "closed"
        position.exit_price = exit_price
        position.exit_time = datetime.now(timezone.utc).isoformat()
        position.exit_reason = "resolved"
        position.pnl = pnl

        # Update totals (unified — always update from CopiedPosition)
        self._realized_pnl += pnl
        self._record_whale_copy_pnl(position.whale_address, pnl)
        self._total_exposure -= cost
        if self._total_exposure < 0:
            self._total_exposure = 0  # Safety clamp
        self._positions_closed += 1
        if pnl > 0:
            self._positions_won += 1
        elif pnl < 0:
            self._positions_lost += 1

        self._save_state()

        # Log
        result = "WON" if exit_price == 1.0 else "LOST" if exit_price == 0.0 else "UNKNOWN"
        pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        mode_label = "LIVE" if self.live_trading_enabled else "PAPER"
        logger.info(
            f"🏁 {mode_label} RESOLVED: {position.market_title[:40]}... "
            f"| Our bet: {position.outcome} → {result} "
            f"| P&L: {pnl_emoji} ${pnl:+.2f}"
        )

        # Save resolution trade record in paper mode
        if not self.live_trading_enabled:
            exit_trade = PaperTrade(
                timestamp=datetime.now(timezone.utc).isoformat(),
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
                tx_hash=f"resolved_{datetime.now(timezone.utc).timestamp()}",
            )
            self._save_trade(exit_trade)

        # Auto-redeem winning positions to convert conditional tokens back to USDC
        if self.live_trading_enabled and self._redeemer and exit_price == 1.0:
            try:
                # Check if this is a neg-risk market via the CLOB client
                is_neg_risk = False
                if self._live_trader and self._live_trader._client:
                    try:
                        is_neg_risk = self._live_trader._client.get_neg_risk(position.token_id)
                    except Exception:
                        pass  # Default to standard market

                redeemed = self._redeemer.redeem(
                    condition_id=position.market_id,
                    is_neg_risk=is_neg_risk,
                )
                if redeemed:
                    logger.info(f"   💵 REDEEMED: {position.market_title[:40]}... → USDC returned to wallet")
                else:
                    logger.warning(f"   ⚠️ Redemption failed for {position.market_title[:30]}... — redeem manually")
            except Exception as e:
                logger.warning(f"   ⚠️ Redemption error: {e} — redeem manually")

        # Send Slack alert for resolution
        result_str = "WON" if exit_price == 1.0 else "LOST" if exit_price == 0.0 else "RESOLVED"
        await self._slack_exit_alert(position, pnl, f"Market {result_str}")

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
        """Calculate unrealized P&L for open positions (unified for paper and live).

        Uses cost-basis accounting: P&L = current_market_value - actual_cost_paid
        """
        unrealized = 0.0
        for position in self._copied_positions.values():
            if position.status == "open":
                current_price = self._current_prices.get(position.token_id, position.entry_price)
                effective_shares = position.live_shares or position.shares
                cost = position.live_cost_usd or position.copy_amount_usd
                current_value = current_price * effective_shares
                pnl = current_value - cost
                unrealized += pnl
        return unrealized

    def _get_portfolio_stats(self) -> dict:
        """Calculate comprehensive portfolio statistics — always from CopiedPosition (single source of truth)"""
        open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
        closed_positions = [p for p in self._copied_positions.values() if p.status == "closed"]

        unrealized_pnl = self._calculate_unrealized_pnl()
        total_pnl = self._realized_pnl + unrealized_pnl

        # Cost basis of open positions (what we paid)
        open_cost = sum(
            (p.live_cost_usd or p.copy_amount_usd) for p in open_positions
        )

        # Current market value of open positions
        open_market_value = 0.0
        for p in open_positions:
            current_price = self._current_prices.get(p.token_id, p.entry_price)
            effective_shares = p.live_shares or p.shares
            open_market_value += current_price * effective_shares

        # Get actual USDC balance from Polymarket
        usdc_balance = None
        if self.live_trading_enabled and self._live_trader:
            usdc_balance = self._live_trader.get_collateral_balance()

        # Total portfolio value = USDC cash + market value of open positions
        total_value = None
        if usdc_balance is not None:
            total_value = usdc_balance + open_market_value

        return {
            "realized_pnl": self._realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "positions_won": self._positions_won,
            "positions_lost": self._positions_lost,
            "win_rate": self._positions_won / len(closed_positions) if closed_positions else 0,
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "open_cost": open_cost,
            "open_market_value": open_market_value,
            "usdc_balance": usdc_balance,
            "total_value": total_value,
            # Keep old key for compatibility
            "open_exposure": open_cost,
        }

    async def _periodic_report(self):
        """Print status report every 5 minutes"""
        while self._running:
            await asyncio.sleep(300)

            runtime = (datetime.now(timezone.utc) - self._start_time).total_seconds() / 3600

            # Get comprehensive portfolio stats
            stats = self._get_portfolio_stats()

            # Calculate return percentage
            total_invested = stats["open_exposure"] + sum(
                (p.live_cost_usd or p.copy_amount_usd) for p in self._copied_positions.values() if p.status == "closed"
            )
            return_pct = (stats["total_pnl"] / total_invested * 100) if total_invested > 0 else 0

            # Projections based on realized P&L
            hourly_return = stats["realized_pnl"] / runtime if runtime > 0 else 0
            daily_projected = hourly_return * 24

            # Use counters for trade counts (works in both paper and live mode)
            whale_copies = self._whale_copies_count
            unusual_copies = self._unusual_copies_count
            arb_copies = self._arb_copies_count

            mode_label = "LIVE" if self.live_trading_enabled else "PAPER"

            # Portfolio value summary
            usdc_str = f"${stats['usdc_balance']:.2f}" if stats['usdc_balance'] is not None else "N/A"
            total_str = f"${stats['total_value']:.2f}" if stats['total_value'] is not None else "N/A"

            logger.info(
                f"\n{'='*60}\n"
                f"🐋 WHALE COPY TRADING REPORT [{mode_label}] ({runtime:.1f}h runtime)\n"
                f"{'='*60}\n"
                f"💰 PORTFOLIO\n"
                f"   USDC Cash:       {usdc_str}\n"
                f"   Open Positions:  {stats['open_positions']} (cost: ${stats['open_cost']:.2f} → mkt value: ${stats['open_market_value']:.2f})\n"
                f"   Total Value:     {total_str}\n"
                f"{'='*60}\n"
                f"📊 P&L\n"
                f"   Realized (closed):   ${stats['realized_pnl']:+.2f}  ({stats['positions_won']}W / {stats['positions_lost']}L, {stats['win_rate']*100:.0f}% win rate)\n"
                f"   Unrealized (open):   ${stats['unrealized_pnl']:+.2f}\n"
                f"   Total P&L:           ${stats['total_pnl']:+.2f}\n"
                f"   Session rate:        ${hourly_return:+.2f}/hr\n"
                f"{'='*60}"
            )

            # Show open positions sorted by P&L (biggest winners/losers)
            open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
            if open_positions:
                # Calculate P&L for each and sort
                pos_with_pnl = []
                for pos in open_positions:
                    current = self._current_prices.get(pos.token_id, pos.entry_price)
                    effective_shares = pos.live_shares or pos.shares
                    cost = pos.live_cost_usd or pos.copy_amount_usd
                    mkt_val = current * effective_shares
                    pnl = mkt_val - cost
                    pos_with_pnl.append((pos, current, cost, mkt_val, pnl))

                pos_with_pnl.sort(key=lambda x: x[4], reverse=True)

                logger.info(f"📈 OPEN POSITIONS ({len(open_positions)}):")
                for pos, current, cost, mkt_val, pnl in pos_with_pnl[:8]:
                    emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                    logger.info(
                        f"   {emoji} {pos.outcome} @ {pos.entry_price:.0%}→{current:.0%} "
                        f"| cost ${cost:.2f} → ${mkt_val:.2f} ({pnl:+.2f}) "
                        f"| {pos.market_title[:35]}..."
                    )
                if len(open_positions) > 8:
                    logger.info(f"   ... and {len(open_positions) - 8} more")

            # Per-whale copy P&L leaderboard
            if self._whale_copy_pnl:
                sorted_whales = sorted(
                    self._whale_copy_pnl.items(),
                    key=lambda x: x[1]["realized_pnl"],
                    reverse=True,
                )
                logger.info(f"🐋 PER-WHALE COPY P&L ({len(sorted_whales)} whales):")
                for addr, data in sorted_whales[:10]:
                    # Find whale name
                    name = addr[:12]
                    for w in list(self.whales.values()) + list(self._whale_manager._all_whales.values() if self._whale_manager else []):
                        if w.address.lower() == addr.lower():
                            name = w.name
                            break
                    wr = data["wins"] / data["copies"] * 100 if data["copies"] > 0 else 0
                    pruned_tag = " [PRUNED]" if addr in self._pruned_whales else ""
                    emoji = "🟢" if data["realized_pnl"] > 0 else "🔴" if data["realized_pnl"] < 0 else "⚪"
                    logger.info(
                        f"   {emoji} {name}: ${data['realized_pnl']:+.2f} "
                        f"({data['copies']} copies, {wr:.0f}% win){pruned_tag}"
                    )
                if self._pruned_whales:
                    logger.info(f"   🚫 {len(self._pruned_whales)} whale(s) pruned for poor performance")

            logger.info(f"{'='*60}\n")

            # Periodic report logged locally only (no Slack — keep alerts for buys/sells only)

    def _print_final_report(self):
        """Print final summary when shutting down"""
        runtime = (datetime.now(timezone.utc) - self._start_time).total_seconds() / 3600
        stats = self._get_portfolio_stats()
        total_copies = self._whale_copies_count + self._unusual_copies_count + self._arb_copies_count

        mode_label = "LIVE" if self.live_trading_enabled else "PAPER"
        logger.info(
            f"\n{'='*60}\n"
            f"🐋 FINAL WHALE COPY TRADING REPORT [{mode_label}]\n"
            f"{'='*60}\n"
            f"Runtime: {runtime:.2f} hours\n"
            f"Total trades copied: {total_copies}\n"
            f"{'='*60}\n"
            f"📊 POSITIONS\n"
            f"   Opened: {stats['open_positions'] + stats['closed_positions']}\n"
            f"   Closed: {stats['closed_positions']}\n"
            f"   Still Open: {stats['open_positions']} (${stats['open_exposure']:.2f})\n"
            f"{'='*60}\n"
            f"💰 FINAL P&L ({mode_label})\n"
            f"   Realized:   ${stats['realized_pnl']:+.2f}\n"
            f"   Unrealized: ${stats['unrealized_pnl']:+.2f}\n"
            f"   Total:      ${stats['total_pnl']:+.2f}\n"
            f"   Win Rate:   {stats['win_rate']*100:.1f}% ({stats['positions_won']}W / {stats['positions_lost']}L)\n"
            f"{'='*60}"
        )

        # List any remaining open positions (unified — always from CopiedPosition)
        open_positions = [p for p in self._copied_positions.values() if p.status == "open"]
        if open_positions:
            logger.info(f"📈 REMAINING {mode_label} POSITIONS:")
            for pos in open_positions:
                current = self._current_prices.get(pos.token_id, pos.entry_price)
                effective_shares = pos.live_shares or pos.shares
                cost = pos.live_cost_usd or pos.copy_amount_usd
                unrealized = (current * effective_shares) - cost
                logger.info(
                    f"   {pos.outcome} @ {pos.entry_price:.1%} | ${unrealized:+.2f} | "
                    f"{pos.market_title[:40]}... | Whale: {pos.whale_name}"
                )
            logger.info(f"{'='*60}")

        # Show order stats if in live mode
        if self.live_trading_enabled and self._live_trader:
            live_stats = self._live_trader.get_stats()
            logger.info(
                f"📦 ORDER STATS: {live_stats['orders_submitted']} submitted, "
                f"{live_stats['orders_filled']} filled, {live_stats['orders_failed']} failed"
            )


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
