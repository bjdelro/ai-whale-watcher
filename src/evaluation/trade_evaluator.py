"""
Trade evaluation and exit detection logic.

Decides whether to copy a whale trade, detects whale sells for exit,
and polls tracked wallets for sell signals.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.positions.position_manager import PositionManager
    from src.signals.cluster_detector import ClusterDetector
    from src.market_data.client import MarketDataClient
    from src.whales.whale_manager import WhaleManager

logger = logging.getLogger(__name__)


class TradeEvaluator:
    """Evaluates whale trades for copying and detects exit signals."""

    def __init__(
        self,
        position_manager: "PositionManager",
        cluster_detector: "ClusterDetector",
        market_data: "MarketDataClient",
        whale_manager: "WhaleManager",
        fetch_whale_trades: Callable,
        get_active_whales: Callable,
        get_polls_completed: Callable,
        config: dict,
    ):
        """
        Args:
            position_manager: For position lookups, copy_trade, execute_copy_sell.
            cluster_detector: For hedge analysis and cluster recording.
            market_data: For fetching current prices.
            whale_manager: For pruning checks and conviction sizing.
            fetch_whale_trades: async callable(address, limit) -> List[dict]
            get_active_whales: callable() -> dict of active whale addresses
            get_polls_completed: callable() -> int (poll counter for log gating)
            config: Dict with keys:
                - min_whale_trade_size: float
                - avoid_extreme_prices: bool
                - extreme_price_threshold: float
                - max_total_exposure: float
                - live_max_exposure: float
                - live_trading_enabled: bool
        """
        self._position_manager = position_manager
        self._cluster_detector = cluster_detector
        self._market_data = market_data
        self._whale_manager = whale_manager
        self._fetch_whale_trades = fetch_whale_trades
        self._get_active_whales = get_active_whales
        self._get_polls_completed = get_polls_completed
        self._config = config

    # ------------------------------------------------------------------
    # Trade evaluation
    # ------------------------------------------------------------------

    def has_conflicting_position(self, market_id: str, outcome: str, whale_name: str) -> bool:
        """Check if we already hold an opposing position on this market."""
        for pos in self._position_manager.positions.values():
            if (pos.status == "open" and
                    pos.market_id == market_id and
                    pos.outcome.lower() != outcome.lower()):
                logger.info(
                    f"   \u2694\ufe0f Cross-whale conflict: {whale_name} wants {outcome}, "
                    f"but already holding {pos.outcome} from {pos.whale_name} "
                    f"on '{pos.market_title[:40]}...' \u2014 skipping"
                )
                return True
        return False

    async def evaluate_trade(self, whale, trade: dict, PaperTrade, CopiedPosition):
        """Evaluate a whale trade and decide if we should copy it."""
        # PRUNING: Skip whales with poor copy track record
        if self._whale_manager.is_pruned(whale.address):
            logger.debug(f"   \u23ed\ufe0f {whale.name}: pruned (poor copy P&L), skipping")
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
        polls_completed = self._get_polls_completed()

        # FILTER 0: Skip stale trades — tighter 120s window for copy freshness
        MAX_COPY_AGE_SECONDS = 120
        trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
        if not trade_timestamp:
            if polls_completed <= 2:
                logger.debug(f"   \u23ed\ufe0f {whale.name}: skipped (no timestamp) tx={tx_hash[:12]}...")
            return
        try:
            if isinstance(trade_timestamp, (int, float)) or str(trade_timestamp).isdigit():
                ts = float(trade_timestamp)
                if ts > 1e12:
                    ts = ts / 1000
                trade_time = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                from dateutil.parser import parse as parse_date
                trade_time = parse_date(str(trade_timestamp))
                if trade_time.tzinfo:
                    import pytz
                    trade_time = trade_time.astimezone(pytz.UTC).replace(tzinfo=None)

            age_seconds = (datetime.now(timezone.utc) - trade_time).total_seconds()
            if age_seconds > MAX_COPY_AGE_SECONDS:
                if polls_completed <= 2:
                    logger.info(
                        f"   \u23ed\ufe0f {whale.name}: skipped STALE trade ({age_seconds:.0f}s ago, >{MAX_COPY_AGE_SECONDS}s) "
                        f"| {side} ${trade_value:,.0f} {outcome} @ {price:.0%} | {title[:35]}"
                    )
                return
        except Exception as e:
            logger.debug(f"Skipping trade with unparseable timestamp: {trade_timestamp} ({e})")
            return

        # Skip small trades
        min_trade_size = self._config.get("min_whale_trade_size", 50)
        if trade_value < min_trade_size:
            if polls_completed <= 2:
                logger.info(f"   \u23ed\ufe0f {whale.name}: skipped SMALL trade (${trade_value:,.0f} < ${min_trade_size})")
            return

        # Log the whale trade
        logger.info(
            f"\U0001f40b WHALE TRADE: {whale.name} "
            f"{side} ${trade_value:,.0f} of {outcome} @ {price:.1%} "
            f"- {title[:50]}..."
        )

        # DEDUP: Skip if we already have an open position for this whale + market
        for pos in self._position_manager.positions.values():
            if (pos.status == "open" and
                pos.whale_address.lower() == whale.address.lower() and
                pos.market_id == condition_id):
                logger.info(f"   \u23ed\ufe0f Skipping: already have open position from {whale.name} on this market")
                return

        # CROSS-WHALE CONFLICT
        if self.has_conflicting_position(condition_id, outcome, whale.name):
            return

        # FILTER 1: Skip extreme prices
        avoid_extreme = self._config.get("avoid_extreme_prices", True)
        extreme_threshold = self._config.get("extreme_price_threshold", 0.05)
        if avoid_extreme:
            if price < extreme_threshold or price > (1 - extreme_threshold):
                logger.info(f"   \u23ed\ufe0f Skipping: extreme price ({price:.1%}) - market likely decided")
                return

        # FILTER 2: Check exposure limits
        live_enabled = self._config.get("live_trading_enabled", False)
        max_exposure = (
            self._config.get("live_max_exposure", 50.0) if live_enabled
            else self._config.get("max_total_exposure", 100.0)
        )
        if self._position_manager.total_exposure >= max_exposure:
            logger.info(f"   \u26a0\ufe0f Max exposure reached (${self._position_manager.total_exposure:.2f}/${max_exposure:.0f}), skipping")
            return

        # SMART HEDGE ANALYSIS
        is_hedge, net_direction, net_profit, recommendation = self._cluster_detector.analyze_hedge(
            whale.address, condition_id, outcome, trade_value, price
        )

        if is_hedge:
            logger.info(f"   \U0001f504 Hedge detected: profits most if {net_direction} wins (${net_profit:+,.0f})")

        if recommendation == 'skip_small_hedge':
            logger.info(f"   \u23ed\ufe0f Skipping: small hedge, main bet is {net_direction}")
            self._cluster_detector.record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)
            return
        elif recommendation == 'skip_no_direction':
            logger.info(f"   \u23ed\ufe0f Skipping: heavily hedged, no clear direction")
            self._cluster_detector.record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)
            return
        elif recommendation == 'skip_arbitrage':
            logger.info(f"   \u23ed\ufe0f Skipping: arbitrage (guaranteed profit regardless of outcome)")
            self._cluster_detector.record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)
            return
        elif recommendation == 'consider_hedge':
            logger.info(f"   \U0001f914 Medium hedge - whale reducing exposure, still favors {net_direction}")

        # Record for position tracking
        self._cluster_detector.record_wallet_market_trade(whale.address, condition_id, outcome, trade_value, price)

        # Record for cluster detection before copying
        self._cluster_detector.record_trade_for_cluster(condition_id, whale.address, side, trade_value)

        # PRICE-SLIPPAGE GATE
        asset_id = trade.get("asset", "")
        if asset_id and side == "BUY":
            current_price = await self._market_data.fetch_price(asset_id)
            if current_price is not None:
                slippage = current_price - price
                slippage_pct = slippage / price if price > 0 else 0
                if slippage_pct > 0.03:
                    logger.info(
                        f"   \u23ed\ufe0f Skipping: price slippage too high "
                        f"(whale @ {price:.1%}, now @ {current_price:.1%}, "
                        f"+{slippage_pct:.1%} slippage) \u2014 edge likely gone"
                    )
                    return

        # Copy the trade!
        await self._position_manager.copy_trade(whale, trade, PaperTrade, CopiedPosition)

    # ------------------------------------------------------------------
    # Sell detection
    # ------------------------------------------------------------------

    def check_for_whale_sells(self, trade: dict) -> Optional[dict]:
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

        for pos_id, position in self._position_manager.positions.items():
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

    async def check_open_position_sells(self, PaperTrade):
        """
        Poll wallets that have open positions but are NOT in the current whale list.
        Catches sells from unusual-activity wallets and dropped-off whales.
        """
        open_positions = [p for p in self._position_manager.positions.values() if p.status == "open"]
        if not open_positions:
            return

        active_whales = self._get_active_whales()
        extra_wallets = set()
        for pos in open_positions:
            if pos.whale_address not in active_whales:
                extra_wallets.add(pos.whale_address)

        if not extra_wallets:
            return

        for wallet_address in extra_wallets:
            try:
                trades = await self._fetch_whale_trades(wallet_address, limit=5)
                for trade in trades:
                    tx_hash = trade.get("transactionHash", "")
                    if tx_hash in self._position_manager._seen_tx_hashes:
                        continue

                    side = trade.get("side", "")
                    if side != "SELL":
                        continue

                    # Check freshness — 10 min window
                    trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
                    if trade_timestamp:
                        try:
                            ts = float(trade_timestamp)
                            if ts > 1e12:
                                ts = ts / 1000
                            age = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
                            if age > 600:
                                continue
                        except (ValueError, TypeError):
                            continue

                    self._position_manager._seen_tx_hashes.add(tx_hash)

                    sell_signal = self.check_for_whale_sells(trade)
                    if sell_signal:
                        pos_name = sell_signal["position"].whale_name
                        logger.info(
                            f"\U0001f40b TRACKED WALLET SELLING: {pos_name} exiting position in "
                            f"{sell_signal['position'].market_title[:40]}..."
                        )
                        await self._position_manager.execute_copy_sell(sell_signal, PaperTrade)

                await asyncio.sleep(0.1)

            except Exception as e:
                logger.debug(f"Error polling tracked wallet {wallet_address[:12]}...: {e}")
