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

# Unusual activity + arbitrage
from src.signals.arb_trader import ArbTrader

# LLM intelligence (Phase 3)
from src.intelligence.llm_client import LLMClient
from src.intelligence.market_tagger import MarketTagger
from src.intelligence.correlation_detector import CorrelationDetector
from src.intelligence.strategy_reviewer import StrategyReviewer

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)




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
    category: Optional[str] = None  # Market category (sports, politics, crypto, etc.)
    high_water_mark: Optional[float] = None  # B3: Trailing stop — highest price seen
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

        # Slack alerts — only for live mode (not dry_run/paper)
        if live_trading_enabled and not live_dry_run:
            self._slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
            if self._slack_webhook_url:
                logger.info("Slack alerts enabled (live mode)")
        else:
            self._slack_webhook_url = None

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

        # Cluster and hedge detection
        self._cluster_detector = ClusterDetector()

        # Arbitrage scanner (initialized in start(), owned by ArbTrader)
        self._arbitrage_scanner: Optional[IntraMarketArbitrage] = None

        # RTDS WebSocket feed
        self._rtds_feed = None
        self._whale_trade_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._activity_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._rtds_task: Optional[asyncio.Task] = None
        self._ws_process_task: Optional[asyncio.Task] = None
        self._ws_activity_task: Optional[asyncio.Task] = None
        self._ws_trades_processed = 0

        # Shadow mode: run WebSocket alongside REST, compare results
        self._shadow_mode = True
        self._rest_fallback_interval = 300  # 5 min for REST discovery in production mode
        self._last_rest_poll = datetime.min.replace(tzinfo=timezone.utc)

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
            "category_copy_pnl": whale_state.get("category_copy_pnl", {}),
            "whale_category_mix": whale_state.get("whale_category_mix", {}),
        })
        # Point proxy references to whale manager's state
        self._whale_copy_pnl = self._whale_manager._whale_copy_pnl
        self._pruned_whales = self._whale_manager._pruned_whales

        # Wire category cap check for A5 (whale deprioritization)
        from src.evaluation.trade_evaluator import MAX_CATEGORY_EXPOSURE_PCT, DEFAULT_CATEGORY_CAP

        def _is_category_at_cap(category: str) -> bool:
            cap = MAX_CATEGORY_EXPOSURE_PCT.get(category, DEFAULT_CATEGORY_CAP)
            total_exp = self._position_manager.total_exposure
            if total_exp <= 0:
                return False
            cat_exp = sum(
                (p.live_cost_usd or p.copy_amount_usd)
                for p in self._position_manager.positions.values()
                if p.status == "open" and getattr(p, "category", None) == category
            )
            return cat_exp / total_exp > cap

        self._whale_manager._is_category_at_cap = _is_category_at_cap

        # Initialize LLM intelligence (Phase 3 — graceful no-op if no API key)
        self._llm_client = LLMClient(self._session)
        self._market_tagger = MarketTagger(self._llm_client)

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
            market_tagger=self._market_tagger,
        )

        # Initialize ArbTrader
        arb_config = dict(self._module_config)
        arb_config.update({
            "unusual_trade_size": self.UNUSUAL_TRADE_SIZE,
            "unusual_ratio": self.UNUSUAL_RATIO,
            "unusual_min_trade": self.UNUSUAL_MIN_TRADE,
            "unusual_min_avg": self.UNUSUAL_MIN_AVG,
            "unusual_min_history_trades": 3,
            "_live_trader_ref": self._live_trader,
        })
        self._arb_trader = ArbTrader(
            session=self._session,
            position_manager=self._position_manager,
            cluster_detector=self._cluster_detector,
            market_data=self._market_data,
            reporter=self._reporter,
            arbitrage_scanner=self._arbitrage_scanner,
            get_active_whales=lambda: self.whales,
            execute_live_buy=self._execute_live_buy,
            create_position=self._create_position,
            save_state=self._save_state,
            save_trade=self._save_trade,
            has_conflicting_position=self._has_conflicting_position,
            config=arb_config,
        )
        self._correlation_detector = CorrelationDetector(
            llm_client=self._llm_client,
            get_open_positions=lambda: [
                p for p in self._position_manager.positions.values() if p.status == "open"
            ],
            get_total_exposure=lambda: self._position_manager.total_exposure,
            reporter=self._reporter,
        )
        self._strategy_reviewer = StrategyReviewer(
            llm_client=self._llm_client,
            get_report_data=lambda: self._get_report_data(),
            get_portfolio_stats=lambda: self._get_portfolio_stats(),
            reporter=self._reporter,
        )

        # Restore LLM tag cache from state and register for persistence
        if whale_state.get("market_tag_cache"):
            self._market_tagger.from_dict(whale_state["market_tag_cache"])
        self._position_manager._extra_state_providers["market_tag_cache"] = self._market_tagger

        self._running = True

        # Fetch whale list dynamically from leaderboard API
        await self._refresh_whale_list()

        # Compact startup banner
        if self.live_trading_enabled:
            mode_str = "dry_run shadow" if self.live_dry_run else "LIVE"
            max_trade = self.live_max_per_trade
            max_exp = self.live_max_exposure
        else:
            mode_str = "paper"
            max_trade = self.max_per_trade
            max_exp = self.max_total_exposure
        whale_list = sorted(self.whales.values(), key=lambda w: w.monthly_profit, reverse=True)
        top_names = ", ".join(
            f"{w.name} (${w.monthly_profit/1e6:.1f}M/mo)" if w.monthly_profit >= 1e6
            else f"{w.name} (${w.monthly_profit:,.0f}/mo)"
            for w in whale_list[:3]
        )
        ws_url = "wss://ws-live-data.polymarket.com"
        logger.info(
            f"WHALE COPY TRADER | mode={mode_str} | {len(self.whales)} whales | "
            f"max ${max_trade:.2f}/trade ${max_exp:.2f} exposure"
        )
        logger.info(f"  top: {top_names}")
        logger.info(
            f"  RTDS: {ws_url} | REST fallback: {self._rest_fallback_interval}s"
        )

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

        # Initialize RTDS WebSocket feed
        from src.ingestion.rtds_feed import RTDSFeed
        whale_address_set = set(self.whales.keys())
        # Include open-position wallets so we catch their sells
        for pos in self._position_manager.positions.values():
            if pos.status == "open":
                whale_address_set.add(pos.whale_address.lower())
        self._rtds_feed = RTDSFeed(
            whale_addresses=whale_address_set,
            whale_trade_queue=self._whale_trade_queue,
            activity_queue=self._activity_queue,
        )

        # Startup logged locally only (no Slack — keep alerts for buys/sells only)

    async def _refresh_whale_list(self):
        """Fetch the leaderboard and update the tracked whale list."""
        await self._whale_manager.refresh_from_leaderboard()
        self.whales = self._whale_manager.whales

        # Sync RTDS whale filter
        if self._rtds_feed:
            whale_addrs = set(self.whales.keys())
            # Include open-position wallets so we catch their sells
            for pos in self._position_manager.positions.values():
                if pos.status == "open":
                    whale_addrs.add(pos.whale_address.lower())
            self._rtds_feed.update_whale_addresses(whale_addrs)

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

    # ================================================================
    # WEBSOCKET-DRIVEN TRADE PROCESSING
    # ================================================================

    async def _process_ws_whale_trades(self):
        """Process whale trades received via RTDS WebSocket."""
        while self._running:
            try:
                try:
                    trade = await asyncio.wait_for(
                        self._whale_trade_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                proxy_wallet = trade.get("proxyWallet", "").lower()
                tx_hash = trade.get("transactionHash", "")

                # Defense-in-depth dedup (RTDSFeed already dedups)
                if tx_hash in self._position_manager._seen_tx_hashes:
                    continue

                # Look up whale
                whale = self.whales.get(proxy_wallet)
                if whale is None:
                    # Check all whales (might be open-position wallet not in active set)
                    if self._whale_manager:
                        whale = self._whale_manager._all_whales.get(proxy_wallet)
                    if whale is None:
                        continue

                # Check trade freshness
                trade_timestamp = trade.get("timestamp")
                age = 0
                if trade_timestamp:
                    try:
                        ts = float(trade_timestamp)
                        if ts > 1e12:
                            ts = ts / 1000
                        age = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        age = 0

                    if age > 300:
                        # Still check for sells on old trades
                        if trade.get("side") == "SELL":
                            sell_signal = self._check_for_whale_sells(trade)
                            if sell_signal:
                                self._position_manager._seen_tx_hashes.add(tx_hash)
                                logger.info(
                                    f"SIGNAL {whale.name} | SELL (late detect, {age:.0f}s) "
                                    f"| {sell_signal['position'].market_title[:40]} [WS]"
                                )
                                await self._execute_copy_sell(sell_signal)
                        continue

                # FRESH TRADE
                side = trade.get("side", "?")
                size = trade.get("size", 0)
                price = trade.get("price", 0)
                title = trade.get("title", "?")[:40]
                logger.info(
                    f"SIGNAL {whale.name} | {side} ${size*price:,.0f} "
                    f"| age={age:.0f}s | {title} [WS]"
                )

                # Record for progressive scaling
                self._whale_manager._fresh_trade_timestamps.append(
                    datetime.now(timezone.utc)
                )

                # Mark as seen
                self._position_manager._seen_tx_hashes.add(tx_hash)
                self._ws_trades_processed += 1

                # Check sell signal first
                sell_signal = self._check_for_whale_sells(trade)
                if sell_signal:
                    logger.info(
                        f"SIGNAL {whale.name} | SELL "
                        f"| {sell_signal['position'].market_title[:40]} [WS]"
                    )
                    await self._execute_copy_sell(sell_signal)
                    continue

                # Evaluate and potentially copy
                result = await self._evaluate_trade(whale, trade)

                # If skipped due to category cap, don't count as fresh
                # (ensures auto-scaling recognizes we're starving for actionable trades)
                if result == "category_capped":
                    self._whale_manager._fresh_trade_timestamps.pop()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[WS] Error processing whale trade: {e}")

    async def _process_ws_activity(self):
        """Process all trades from RTDS for unusual activity detection."""
        batch = []
        BATCH_SIZE = 50
        BATCH_TIMEOUT = 5.0

        while self._running:
            try:
                try:
                    trade = await asyncio.wait_for(
                        self._activity_queue.get(), timeout=BATCH_TIMEOUT
                    )
                    batch.append(trade)
                except asyncio.TimeoutError:
                    pass

                if len(batch) >= BATCH_SIZE or (batch and len(batch) > 0):
                    await self._arb_trader.process_activity_batch(
                        batch, PaperTrade, CopiedPosition
                    )
                    batch = []

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[WS] Error processing activity batch: {e}")
                batch = []

    async def _rest_discovery_poll(self):
        """Lightweight REST poll for discovery only (every 5 min in production).

        Catches whale trades on markets we might have missed during
        a WebSocket disconnect. Feeds missed trades into the WS queue.
        """
        for address, whale in self.whales.items():
            try:
                trades = await self._fetch_whale_trades(address, limit=5)
                for trade in trades:
                    tx_hash = trade.get("transactionHash", "")
                    if tx_hash and tx_hash not in self._position_manager._seen_tx_hashes:
                        trade["_source"] = "rest_fallback"
                        try:
                            self._whale_trade_queue.put_nowait(trade)
                        except asyncio.QueueFull:
                            pass
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.debug(f"REST discovery error for {whale.name}: {e}")

        # Also check sells from open-position wallets not in whale list
        try:
            await self._check_open_position_sells()
        except Exception as e:
            logger.warning(f"Error checking open position sells: {e}")

    # ================================================================
    # HEARTBEAT
    # ================================================================

    def _log_heartbeat(self):
        """Single-line system health summary, logged every ~1 min."""
        # WS status
        if self._rtds_feed:
            rs = self._rtds_feed.stats
            ws_ok = "ok" if rs.get("connected") else "DOWN"
            ws_rate = rs.get("trade_events", 0)
            whale_matches = rs.get("whale_matches", 0)
            ws_part = f"WS: {ws_ok} {ws_rate}evt {whale_matches} whale"
        else:
            ws_part = "WS: off"

        # REST status (from last poll)
        total_whales = len(self.whales)
        rest_part = f"REST: poll#{self._polls_completed}"

        # Position stats
        stats = self._get_portfolio_stats()
        open_pos = stats["open_positions"]
        mkt_val = stats["open_market_value"]
        pnl = stats["total_pnl"]
        pos_part = f"pos: {open_pos} open ${mkt_val:.2f} val"
        pnl_part = f"P&L: ${pnl:+.2f}"

        # Mode
        if self.live_trading_enabled:
            mode_str = "dry_run shadow" if self.live_dry_run else "LIVE"
        else:
            mode_str = "paper"

        logger.info(
            f"HEARTBEAT | {ws_part} | {rest_part} | {pos_part} | {pnl_part} | mode: {mode_str}"
        )

    # ================================================================
    # MAIN LOOP
    # ================================================================

    async def run(self):
        """Main loop — WebSocket-driven with optional REST fallback."""
        report_task = asyncio.create_task(self._periodic_report())
        arbitrage_task = asyncio.create_task(self._arbitrage_loop())

        # LLM intelligence periodic tasks (no-op if no OPENAI_API_KEY)
        if self._llm_client.available:
            logger.info("LLM intelligence enabled (C2 correlation check every 6h, C3 strategy review every 24h)")
            asyncio.create_task(self._correlation_detector.run_periodic(lambda: self._running))
            asyncio.create_task(self._strategy_reviewer.run_periodic(lambda: self._running))
        else:
            logger.info("LLM intelligence disabled (set OPENAI_API_KEY to enable)")

        # Start RTDS WebSocket feed + processing tasks
        if self._rtds_feed:
            self._rtds_task = asyncio.create_task(self._rtds_feed.connect())
            self._ws_process_task = asyncio.create_task(self._process_ws_whale_trades())
            self._ws_activity_task = asyncio.create_task(self._process_ws_activity())
            ws_mode = "shadow (REST+WS)" if self._shadow_mode else "production (WS-primary)"
            logger.info(f"RTDS WebSocket started in {ws_mode} mode")

        try:
            while self._running:
                # Periodically refresh the whale leaderboard
                if self._whale_manager.last_leaderboard_refresh:
                    hours_since = (datetime.now(timezone.utc) - self._whale_manager.last_leaderboard_refresh).total_seconds() / 3600
                    if hours_since >= LEADERBOARD_REFRESH_HOURS:
                        await self._refresh_whale_list()

                # REST polling strategy depends on mode
                now = datetime.now(timezone.utc)
                if self._shadow_mode:
                    # Shadow mode: full REST poll every cycle (existing behavior)
                    await self._poll_whale_trades()
                else:
                    # Production mode: lightweight REST discovery every 5 min
                    rest_elapsed = (now - self._last_rest_poll).total_seconds()
                    if rest_elapsed >= self._rest_fallback_interval:
                        await self._rest_discovery_poll()
                        self._last_rest_poll = now

                    # Still run cluster signals and market resolutions
                    try:
                        await self._check_cluster_signals()
                    except Exception as e:
                        logger.warning(f"Error checking clusters: {e}")

                    try:
                        await self._check_market_resolutions()
                    except Exception as e:
                        logger.warning(f"Error checking resolutions: {e}")

                    self._polls_completed += 1
                    self._check_scaling()

                    # Dedup cleanup
                    if len(self._position_manager._seen_tx_hashes) > 10000:
                        self._position_manager._seen_tx_hashes = set(
                            list(self._position_manager._seen_tx_hashes)[-5000:]
                        )

                # Consolidated heartbeat every ~1 min (every 4th poll)
                if self._polls_completed % 4 == 0 and self._polls_completed > 0:
                    self._log_heartbeat()

                await asyncio.sleep(self.POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Shutting down...")
        finally:
            # Cancel all tasks
            for task in [report_task, arbitrage_task,
                         self._rtds_task, self._ws_process_task,
                         self._ws_activity_task]:
                if task:
                    task.cancel()
            await self.stop()

    async def stop(self):
        """Clean shutdown"""
        self._running = False

        # Disconnect RTDS WebSocket
        if self._rtds_feed:
            await self._rtds_feed.disconnect()

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
                                            f"SIGNAL {whale.name} | SELL (late detect, {age:.0f}s old) "
                                            f"| {sell_signal['position'].market_title[:40]}"
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
                                    logger.debug(
                                        f"skip old: {whale.name} {age:.0f}s "
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
                                    f"SIGNAL {whale.name} | {side} ${size*price:,.0f} "
                                    f"| age={age:.0f}s | {title}"
                                )
                                trades_fresh += 1
                                # Record for progressive scaling
                                self._whale_manager._fresh_trade_timestamps.append(datetime.now(timezone.utc))
                    except Exception as e:
                        trades_skipped_parse_err += 1
                        logger.warning(f"{whale.name}: timestamp parse error: {trade_timestamp} ({e})")
                        continue

                    # Mark as seen NOW (trade is fresh enough to process)
                    self._position_manager._seen_tx_hashes.add(tx_hash)
                    new_trades_found += 1

                    # === EXIT LOGIC: Check if this is a SELL from a whale we copied ===
                    sell_signal = self._check_for_whale_sells(trade)
                    if sell_signal:
                        sell_signals_found += 1
                        logger.info(
                            f"SIGNAL {whale.name} | SELL "
                            f"| {sell_signal['position'].market_title[:40]}"
                        )
                        await self._execute_copy_sell(sell_signal)
                        continue  # Don't also try to copy this as a new trade

                    # Evaluate and potentially copy the trade
                    trades_evaluated += 1
                    result = await self._evaluate_trade(whale, trade)
                    if result == "category_capped":
                        self._whale_manager._fresh_trade_timestamps.pop()

                # Small delay between wallets to avoid rate limits
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.warning(f"Error polling {whale.name}: {e}")
                whale_fetch_failures += 1

        poll_duration = (datetime.now(timezone.utc) - poll_start_time).total_seconds()

        # Detailed poll stats at debug level (visible with --verbose)
        logger.debug(
            f"Poll #{self._polls_completed} ({poll_duration:.1f}s): "
            f"API={whale_fetch_successes}/{len(self.whales)} OK | "
            f"trades={total_trades_returned} returned, {trades_skipped_seen} seen, "
            f"{trades_skipped_old} old, {trades_skipped_no_ts} no-ts, "
            f"{trades_skipped_parse_err} parse-err, "
            f"{trades_fresh} fresh, {trades_evaluated} evaluated, {new_trades_found} new | "
            f"seen_cache={len(self._position_manager._seen_tx_hashes)}"
        )

        # Log at INFO only when something noteworthy happened
        if whale_fetch_failures > 0:
            logger.info(f"REST poll #{self._polls_completed}: {whale_fetch_failures}/{len(self.whales)} API failures")
        if self._polls_completed == 1:
            logger.info(
                f"First poll complete: {whale_fetch_successes}/{len(self.whales)} APIs OK, "
                f"{trades_fresh} fresh trades"
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

        # Progressive whale scaling check
        self._check_scaling()

        # C1: Flush any pending LLM market tags (batched)
        if self._market_tagger and self._market_tagger._pending:
            try:
                await self._market_tagger.process_pending()
            except Exception as e:
                logger.debug(f"LLM tag flush failed: {e}")

        # Cleanup old tx hashes (keep last 10000)
        if len(self._position_manager._seen_tx_hashes) > 10000:
            self._position_manager._seen_tx_hashes = set(list(self._position_manager._seen_tx_hashes)[-5000:])

    async def _scan_for_unusual_activity(self) -> int:
        """Scan for unusual activity — delegates to ArbTrader."""
        return await self._arb_trader.scan_for_unusual_activity(PaperTrade, CopiedPosition)

    async def _copy_unusual_trade(self, trade: dict, reason: str):
        """Copy an unusual activity trade — delegates to ArbTrader."""
        await self._arb_trader._copy_unusual_trade(trade, reason, PaperTrade, CopiedPosition)

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
        """Scan for arbitrage opportunities — delegates to ArbTrader."""
        await self._arb_trader.scan_for_arbitrage(PaperTrade)

    async def _paper_trade_arbitrage(self, opp):
        """Paper trade an arbitrage opportunity — delegates to ArbTrader."""
        await self._arb_trader._paper_trade_arbitrage(opp, PaperTrade)

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
                logger.warning(f"Whale trade fetch failed for {address[:10]}...: HTTP {resp.status} - {body[:200]}")
                return []
            return await resp.json()

    def _has_conflicting_position(self, market_id: str, outcome: str, whale_name: str) -> bool:
        """Check if we hold an opposing position — delegates to TradeEvaluator."""
        return self._trade_evaluator.has_conflicting_position(market_id, outcome, whale_name)

    async def _evaluate_trade(self, whale: WhaleWallet, trade: dict) -> Optional[str]:
        """Evaluate a whale trade and decide if we should copy it — delegates to TradeEvaluator."""
        return await self._trade_evaluator.evaluate_trade(whale, trade, PaperTrade, CopiedPosition)

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
        # B4: Compute which categories are currently at cap
        from src.evaluation.trade_evaluator import MAX_CATEGORY_EXPOSURE_PCT, DEFAULT_CATEGORY_CAP
        capped_categories = set()
        total_exp = self._position_manager.total_exposure
        if total_exp > 0:
            cat_exposures: Dict[str, float] = {}
            for p in self._position_manager.positions.values():
                if p.status == "open":
                    cat = getattr(p, "category", None) or "unknown"
                    cat_exposures[cat] = cat_exposures.get(cat, 0) + (p.live_cost_usd or p.copy_amount_usd)
            for cat, exp in cat_exposures.items():
                cap = MAX_CATEGORY_EXPOSURE_PCT.get(cat, DEFAULT_CATEGORY_CAP)
                if exp / total_exp > cap:
                    capped_categories.add(cat)

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
            "category_copy_pnl": self._whale_manager._category_copy_pnl if self._whale_manager else {},
            "capped_categories": capped_categories,
            "rtds_stats": self._rtds_feed.stats if self._rtds_feed else {},
            "ws_trades_processed": self._ws_trades_processed,
            "shadow_mode": self._shadow_mode,
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
Modes:
  --mode paper       Paper trading only (default)
  --mode dry_run     Connects to Polymarket API, doesn't execute orders
  --mode live        Real money trading (5s safety countdown)

Examples:
  python run_whale_copy_trader.py                    # paper mode
  python run_whale_copy_trader.py --mode dry_run     # dry run
  python run_whale_copy_trader.py --mode live         # real money
  TRADING_MODE=dry_run python run_whale_copy_trader.py  # env var override
        """
    )

    # Mode selection
    parser.add_argument("--mode", choices=["paper", "dry_run", "live"], default=None,
                        help="Trading mode (default: paper)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug-level logging")

    # Paper trading options
    parser.add_argument("--max-trade", type=float, default=1.0,
                        help="Max $ per paper trade (default: 1.0)")
    parser.add_argument("--max-total", type=float, default=10000.0,
                        help="Max total paper exposure (default: 10000.0)")

    # WebSocket options
    parser.add_argument("--no-shadow", action="store_true",
                        help="Disable shadow mode (WS-only, REST for discovery)")
    parser.add_argument("--rest-fallback-interval", type=int, default=300,
                        help="REST fallback poll interval in seconds (default: 300)")

    # Backward-compat aliases (hidden)
    parser.add_argument("--live", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--live-real", action="store_true",
                        help=argparse.SUPPRESS)

    # Live trading limits
    parser.add_argument("--live-max-trade", type=float, default=5.0,
                        help="Max $ per live trade (default: 5.0)")
    parser.add_argument("--live-max-exposure", type=float, default=50.0,
                        help="Max total live exposure (default: 50.0)")

    return parser.parse_args()


async def main():
    args = parse_args()

    # === Verbose logging ===
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Verbose logging enabled")

    # === Resolve trading mode ===
    # Priority: TRADING_MODE env var > --mode flag > --live/--live-real compat > paper
    trading_mode = os.getenv("TRADING_MODE", "").lower()
    if trading_mode:
        if trading_mode not in ("paper", "dry_run", "live"):
            logger.error(f"Invalid TRADING_MODE: '{trading_mode}'. Use: paper, dry_run, or live")
            return
        mode = trading_mode
    elif args.mode:
        mode = args.mode
    elif args.live and args.live_real:
        mode = "live"
    elif args.live:
        mode = "dry_run"
    else:
        mode = "paper"

    # Map mode to internal flags
    live_enabled = mode in ("dry_run", "live")
    live_dry_run = mode != "live"

    # Read live trading limits from env vars (with CLI fallbacks)
    args.live_max_trade = float(os.getenv("LIVE_MAX_TRADE", args.live_max_trade))
    args.live_max_exposure = float(os.getenv("LIVE_MAX_EXPOSURE", args.live_max_exposure))

    # Warn about real trading
    if mode == "live":
        logger.warning("LIVE TRADING MODE - Real orders will be submitted to Polymarket!")
        logger.warning(f"Max per trade: ${args.live_max_trade:.2f} | Max exposure: ${args.live_max_exposure:.2f}")
        logger.warning("Press Ctrl+C within 5 seconds to cancel...")
        await asyncio.sleep(5)
        logger.info("Proceeding with live trading...")

    trader = WhaleCopyTrader(
        max_per_trade=args.max_trade,
        max_total_exposure=args.max_total,
        live_trading_enabled=live_enabled,
        live_max_per_trade=args.live_max_trade,
        live_max_exposure=args.live_max_exposure,
        live_dry_run=live_dry_run,
    )

    # WebSocket mode
    trader._shadow_mode = not args.no_shadow
    trader._rest_fallback_interval = args.rest_fallback_interval

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
