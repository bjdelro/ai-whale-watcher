"""
Unusual activity detection and intra-market arbitrage scanner.

Detects large trades from unknown wallets (potential insider activity)
and scans for risk-free arbitrage opportunities across binary and
multi-outcome markets.
"""

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp
    from src.positions.position_manager import PositionManager
    from src.signals.cluster_detector import ClusterDetector
    from src.market_data.client import MarketDataClient
    from src.reporting.reporter import Reporter

logger = logging.getLogger(__name__)


class ArbTrader:
    """Unusual activity detection and arbitrage scanning."""

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        position_manager: "PositionManager",
        cluster_detector: "ClusterDetector",
        market_data: "MarketDataClient",
        reporter: "Reporter",
        arbitrage_scanner,
        get_active_whales: Callable,
        execute_live_buy: Callable,
        create_position: Callable,
        save_state: Callable,
        save_trade: Callable,
        has_conflicting_position: Callable,
        config: dict,
    ):
        """
        Args:
            session: Shared aiohttp session.
            position_manager: For position lookups and exposure.
            cluster_detector: For hedge analysis and trade recording.
            market_data: For price lookups.
            reporter: For Slack alerts.
            arbitrage_scanner: IntraMarketArbitrage scanner instance.
            get_active_whales: callable() -> dict of active whale addresses.
            execute_live_buy: async callable(token_id, price, market_title) -> LiveOrder.
            create_position: callable(whale_addr, whale_name, trade, our_size, our_shares) -> CopiedPosition.
            save_state: callable() to persist state.
            save_trade: callable(trade) to save paper trade.
            has_conflicting_position: callable(market_id, outcome, whale_name) -> bool.
            config: Dict with keys for thresholds and limits.
        """
        self._session = session
        self._position_manager = position_manager
        self._cluster_detector = cluster_detector
        self._market_data = market_data
        self._reporter = reporter
        self._arbitrage_scanner = arbitrage_scanner
        self._get_active_whales = get_active_whales
        self._execute_live_buy = execute_live_buy
        self._create_position = create_position
        self._save_state = save_state
        self._save_trade = save_trade
        self._has_conflicting_position = has_conflicting_position
        self._config = config

        # Unusual activity state
        self._wallet_history: Dict[str, List[float]] = {}
        self._unusual_activity_count = 0

        # Arbitrage state
        self._arbitrage_opportunities: list = []
        self._last_arbitrage_scan = datetime.min.replace(tzinfo=timezone.utc)
        self._arbitrage_scan_interval = 60
        self._arbitrage_found_count = 0

    # ------------------------------------------------------------------
    # Properties for orchestrator access
    # ------------------------------------------------------------------

    @property
    def unusual_activity_count(self) -> int:
        return self._unusual_activity_count

    @property
    def arbitrage_found_count(self) -> int:
        return self._arbitrage_found_count

    @property
    def arbitrage_opportunities(self) -> list:
        return self._arbitrage_opportunities

    @property
    def wallet_history(self) -> Dict[str, List[float]]:
        return self._wallet_history

    # ------------------------------------------------------------------
    # Unusual activity scanning
    # ------------------------------------------------------------------

    async def scan_for_unusual_activity(self, PaperTrade, CopiedPosition) -> int:
        """
        Scan recent trades for unusual activity:
        - Small/unknown wallets making large trades ($500+)
        - Wallets making trades much larger than their average
        """
        unusual_count = 0
        data_api_base = self._config.get("data_api_base", "https://data-api.polymarket.com")
        unusual_trade_size = self._config.get("unusual_trade_size", 1000)
        unusual_ratio = self._config.get("unusual_ratio", 5.0)
        unusual_min_trade = self._config.get("unusual_min_trade", 500)
        unusual_min_avg = self._config.get("unusual_min_avg", 100)

        try:
            url = f"{data_api_base}/trades"
            params = {"limit": 50}

            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Unusual activity scan failed: HTTP {resp.status} - {body[:200]}")
                    return 0
                trades = await resp.json()

            active_whales = self._get_active_whales()

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")

                if tx_hash in self._position_manager._seen_tx_hashes:
                    continue

                # Check age BEFORE marking as seen
                trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
                if not trade_timestamp:
                    continue
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
                    if age_seconds > 300:
                        continue
                except Exception:
                    continue

                self._position_manager._seen_tx_hashes.add(tx_hash)

                wallet = trade.get("proxyWallet", "").lower()
                size = trade.get("size", 0)
                price = trade.get("price", 0)
                trade_value = size * price

                # Skip known whales
                if wallet in active_whales:
                    continue

                if wallet not in self._wallet_history:
                    self._wallet_history[wallet] = []

                history = self._wallet_history[wallet]
                avg_size = sum(history) / len(history) if history else 0

                is_unusual = False
                reason = ""

                if trade_value < unusual_min_trade:
                    history.append(trade_value)
                    if len(history) > 20:
                        history.pop(0)
                    continue

                if trade_value >= unusual_trade_size and len(history) < 5:
                    is_unusual = True
                    reason = f"Large trade (${trade_value:,.0f}) from new wallet (only {len(history)} prior trades)"

                elif avg_size >= unusual_min_avg and trade_value >= avg_size * unusual_ratio:
                    is_unusual = True
                    reason = f"Trade ${trade_value:,.0f} is {trade_value/avg_size:.1f}x larger than avg (${avg_size:.0f})"

                history.append(trade_value)
                if len(history) > 20:
                    history.pop(0)

                if is_unusual:
                    unusual_count += 1
                    self._unusual_activity_count += 1

                    title = trade.get("title", "Unknown")
                    side = trade.get("side", "")
                    outcome = trade.get("outcome", "")
                    name = trade.get("name", wallet[:12])

                    logger.debug(
                        f"unusual: {name} {side} ${trade_value:,.0f} of {outcome} "
                        f"- {title[:40]} | {reason}"
                    )

                    await self._copy_unusual_trade(trade, reason, PaperTrade, CopiedPosition)

        except Exception as e:
            logger.warning(f"Error in unusual activity scan: {e}")

        # Cleanup old wallet history
        if len(self._wallet_history) > 1000:
            sorted_wallets = sorted(
                self._wallet_history.items(),
                key=lambda x: len(x[1]),
                reverse=True
            )
            self._wallet_history = dict(sorted_wallets[:500])

        return unusual_count

    async def process_activity_batch(self, trades: list, PaperTrade, CopiedPosition) -> int:
        """
        Process a batch of trades from the RTDS activity queue.

        WebSocket-driven replacement for scan_for_unusual_activity().
        Instead of REST polling GET /trades?limit=50, we receive trades
        continuously from the RTDS firehose and apply the same detection logic.
        """
        unusual_count = 0
        unusual_trade_size = self._config.get("unusual_trade_size", 1000)
        unusual_ratio = self._config.get("unusual_ratio", 5.0)
        unusual_min_trade = self._config.get("unusual_min_trade", 500)
        unusual_min_avg = self._config.get("unusual_min_avg", 100)
        active_whales = self._get_active_whales()

        for trade in trades:
            tx_hash = trade.get("transactionHash", "")

            if tx_hash in self._position_manager._seen_tx_hashes:
                continue

            # Check freshness
            trade_timestamp = trade.get("timestamp")
            if not trade_timestamp:
                continue
            try:
                ts = float(trade_timestamp)
                if ts > 1e12:
                    ts = ts / 1000
                age = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
                if age > 300:
                    continue
            except (ValueError, TypeError):
                continue

            self._position_manager._seen_tx_hashes.add(tx_hash)

            wallet = trade.get("proxyWallet", "").lower()
            size = trade.get("size", 0)
            price = trade.get("price", 0)
            trade_value = size * price

            # Skip known whales (handled by whale queue)
            if wallet in active_whales:
                continue

            if wallet not in self._wallet_history:
                self._wallet_history[wallet] = []

            history = self._wallet_history[wallet]
            avg_size = sum(history) / len(history) if history else 0

            is_unusual = False
            reason = ""

            if trade_value < unusual_min_trade:
                history.append(trade_value)
                if len(history) > 20:
                    history.pop(0)
                continue

            if trade_value >= unusual_trade_size and len(history) < 5:
                is_unusual = True
                reason = f"Large trade (${trade_value:,.0f}) from new wallet (only {len(history)} prior trades)"

            elif avg_size >= unusual_min_avg and trade_value >= avg_size * unusual_ratio:
                is_unusual = True
                reason = f"Trade ${trade_value:,.0f} is {trade_value/avg_size:.1f}x larger than avg (${avg_size:.0f})"

            history.append(trade_value)
            if len(history) > 20:
                history.pop(0)

            if is_unusual:
                unusual_count += 1
                self._unusual_activity_count += 1

                title = trade.get("title", "Unknown")
                side = trade.get("side", "")
                outcome = trade.get("outcome", "")
                name = trade.get("name", wallet[:12])

                logger.debug(
                    f"unusual [WS]: {name} {side} ${trade_value:,.0f} of {outcome} "
                    f"- {title[:40]} | {reason}"
                )

                await self._copy_unusual_trade(trade, reason, PaperTrade, CopiedPosition)

        # Cleanup old wallet history
        if len(self._wallet_history) > 1000:
            sorted_wallets = sorted(
                self._wallet_history.items(),
                key=lambda x: len(x[1]),
                reverse=True
            )
            self._wallet_history = dict(sorted_wallets[:500])

        return unusual_count

    async def _copy_unusual_trade(self, trade: dict, reason: str, PaperTrade, CopiedPosition):
        """Copy an unusual activity trade."""
        wallet = trade.get("proxyWallet", "").lower()
        condition_id = trade.get("conditionId", "")
        outcome = trade.get("outcome", "")
        price = trade.get("price", 0)
        size = trade.get("size", 0)
        trade_value = size * price

        # Require minimum wallet history before copying
        min_history = self._config.get("unusual_min_history_trades", 3)
        wallet_history = self._wallet_history.get(wallet, [])
        if len(wallet_history) < min_history:
            logger.debug(f"skip history: unusual wallet has only {len(wallet_history)}/{min_history} prior trades")
            return

        # DEDUP: Skip if already have open position
        for pos in self._position_manager.positions.values():
            if (pos.status == "open" and
                pos.whale_address.lower() == wallet and
                pos.market_id == condition_id):
                logger.debug(f"skip dedup: unusual wallet already has open position on this market")
                return

        # CROSS-WHALE CONFLICT
        wallet_name = trade.get("userName", "") or wallet[:12]
        if self._has_conflicting_position(condition_id, outcome, f"UNUSUAL:{wallet_name}"):
            return

        # Check extreme prices
        avoid_extreme = self._config.get("avoid_extreme_prices", True)
        extreme_threshold = self._config.get("extreme_price_threshold", 0.05)
        if avoid_extreme:
            if price < extreme_threshold or price > (1 - extreme_threshold):
                logger.debug(f"skip extreme: unusual price {price:.1%}")
                return

        # Check exposure limits
        live_enabled = self._config.get("live_trading_enabled", False)
        max_exposure = (
            self._config.get("live_max_exposure", 50.0) if live_enabled
            else self._config.get("max_total_exposure", 100.0)
        )
        if self._position_manager.total_exposure >= max_exposure:
            logger.debug(f"skip exposure: ${self._position_manager.total_exposure:.2f}/${max_exposure:.0f}")
            return

        # Smart hedge analysis
        is_hedge, net_direction, net_profit, recommendation = self._cluster_detector.analyze_hedge(
            wallet, condition_id, outcome, trade_value, price
        )

        if recommendation in ('skip_small_hedge', 'skip_no_direction', 'skip_arbitrage'):
            logger.debug(f"skip: unusual {recommendation.replace('_', ' ')}")
            self._cluster_detector.record_wallet_market_trade(wallet, condition_id, outcome, trade_value, price)
            return

        # Record for position tracking
        self._cluster_detector.record_wallet_market_trade(wallet, condition_id, outcome, trade_value, price)

        side = trade.get("side", "")
        title = trade.get("title", "Unknown")
        outcome = trade.get("outcome", "")
        condition_id = trade.get("conditionId", "")
        asset_id = trade.get("asset", "")
        tx_hash = trade.get("transactionHash", "")
        whale_size = trade.get("size", 0)
        wallet = trade.get("proxyWallet", "")
        name = trade.get("name", wallet[:12])

        if side != "BUY":
            logger.debug(f"skip: unusual {side} (not copying non-BUY trades)")
            return

        # PRICE-SLIPPAGE GATE
        if asset_id:
            current_price = await self._market_data.fetch_price(asset_id)
            if current_price is not None:
                slippage = current_price - price
                slippage_pct = slippage / price if price > 0 else 0
                if slippage_pct > 0.03:
                    logger.debug(
                        f"skip slippage: unusual trade@{price:.1%} now@{current_price:.1%} +{slippage_pct:.1%}"
                    )
                    return

        self._position_manager._entry_prices[asset_id] = price
        self._position_manager._unusual_copies_count += 1

        live_enabled = self._config.get("live_trading_enabled", False)
        live_trader = self._config.get("_live_trader_ref")  # For dry_run check

        if live_enabled and live_trader:
            order = await self._execute_live_buy(
                token_id=asset_id,
                price=price,
                market_title=title,
            )

            if order and order.status in ("filled", "dry_run"):
                our_shares = order.size
                our_size_usd = order.cost_usd
                position = self._create_position(
                    whale_address=wallet,
                    whale_name=f"UNUSUAL:{name}",
                    trade=trade,
                    our_size=our_size_usd,
                    our_shares=our_shares,
                )
                position.live_shares = order.size
                position.live_cost_usd = order.cost_usd
                position.live_order_id = order.order_id
                self._position_manager.total_exposure += our_size_usd
                self._save_state()

                mode = "DRY RUN" if live_trader.dry_run else "LIVE"
                logger.info(
                    f"SIGNAL unusual:{name} | {mode} BUY ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                    f"({our_shares:.2f} shares) | {title[:35]}"
                )

                paper_trade = PaperTrade(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    whale_address=wallet, whale_name=f"UNUSUAL:{name}",
                    side=side, outcome=outcome, price=price,
                    whale_size=whale_size, our_size=our_size_usd,
                    our_shares=our_shares, market_title=title,
                    condition_id=condition_id, asset_id=asset_id, tx_hash=tx_hash,
                )
                await self._reporter.slack_trade_alert(paper_trade, position)
            else:
                err = order.error_message if order else "no order returned"
                logger.warning(
                    f"LIVE ORDER FAILED (unusual): {outcome} @ {price:.1%} | {err} | {title[:30]}"
                )
        else:
            max_per_trade = self._config.get("max_per_trade", 1.0)
            our_size_usd = min(max_per_trade, max_exposure - self._position_manager.total_exposure)
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
                f"SIGNAL unusual:{name} | PAPER BUY ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                f"({our_shares:.2f} shares) | {title[:35]}"
            )
            await self._reporter.slack_trade_alert(paper_trade, position)

        # Record for cluster detection
        self._cluster_detector.record_trade_for_cluster(condition_id, wallet, side, whale_size * price)

    # ------------------------------------------------------------------
    # Arbitrage scanning
    # ------------------------------------------------------------------

    async def scan_for_arbitrage(self, PaperTrade):
        """Scan for risk-free intra-market arbitrage opportunities."""
        now = datetime.now(timezone.utc)

        if (now - self._last_arbitrage_scan).total_seconds() < self._arbitrage_scan_interval:
            return

        self._last_arbitrage_scan = now

        if not self._arbitrage_scanner:
            return

        # Scan binary markets
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

            await self._paper_trade_arbitrage(opp, PaperTrade)

        # Scan multi-outcome markets
        multi_opps = await self._arbitrage_scanner.scan_all_markets()

        for opp in multi_opps:
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

            await self._paper_trade_arbitrage(opp, PaperTrade)

        # Keep only recent opportunities
        if len(self._arbitrage_opportunities) > 100:
            self._arbitrage_opportunities = self._arbitrage_opportunities[-50:]

    async def _paper_trade_arbitrage(self, opp, PaperTrade):
        """Paper trade an arbitrage opportunity."""
        live_enabled = self._config.get("live_trading_enabled", False)
        max_exposure = (
            self._config.get("live_max_exposure", 50.0) if live_enabled
            else self._config.get("max_total_exposure", 100.0)
        )
        if self._position_manager.total_exposure >= max_exposure:
            logger.info(f"   Max exposure reached, skipping arbitrage")
            return

        max_per_trade = self._config.get("max_per_trade", 1.0)
        our_size = min(max_per_trade, max_exposure - self._position_manager.total_exposure)

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
                whale_size=0,
                our_size=our_size * (price / opp.total_cost),
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
