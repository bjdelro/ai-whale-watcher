"""
Position lifecycle management — open, close, reconcile, persist.

Single source of truth for all position state: CopiedPositions, exposure,
P&L counters, entry/current prices. Handles live order execution, market
resolution, stop-loss/take-profit, and state persistence.
"""

import asyncio
import json
import logging
import os
import random
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Set, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp
    from src.execution.live_trader import LiveTrader, LiveOrder
    from src.execution.redeemer import PositionRedeemer
    from src.market_data.client import MarketDataClient
    from src.whales.whale_manager import WhaleManager
    from src.reporting.reporter import Reporter

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages position lifecycle: open, close, reconcile, persist."""

    def __init__(
        self,
        state_file: str,
        market_data: "MarketDataClient",
        live_trader: Optional["LiveTrader"],
        redeemer: Optional["PositionRedeemer"],
        whale_manager: "WhaleManager",
        reporter: "Reporter",
        session: "aiohttp.ClientSession",
        config: dict,
    ):
        """
        Args:
            state_file: Path to JSON state file for persistence.
            market_data: MarketDataClient for price/market lookups.
            live_trader: LiveTrader for order execution (None if paper-only).
            redeemer: PositionRedeemer for auto-claiming wins (None if unavailable).
            whale_manager: WhaleManager for conviction sizing and P&L recording.
            reporter: Reporter for Slack alerts.
            session: Shared aiohttp session for API calls.
            config: Dict with keys:
                - live_trading_enabled, live_dry_run: bool
                - max_per_trade, max_total_exposure: float (paper)
                - live_max_per_trade, live_max_exposure: float (live)
                - data_api_base: str
                - stop_loss_pct, take_profit_pct, stale_position_hours: float
        """
        self._state_file = state_file
        self._market_data = market_data
        self._live_trader = live_trader
        self._redeemer = redeemer
        self._whale_manager = whale_manager
        self._reporter = reporter
        self._session = session
        self._config = config

        # Position tracking (single source of truth)
        self.positions: Dict[str, object] = {}  # str -> CopiedPosition
        self._position_counter = 0
        self._total_exposure = 0.0

        # P&L tracking
        self._realized_pnl = 0.0
        self._positions_closed = 0
        self._positions_won = 0
        self._positions_lost = 0

        # Price tracking
        self._entry_prices: Dict[str, float] = {}
        self._current_prices: Dict[str, float] = {}

        # Trade counters
        self._whale_copies_count = 0
        self._unusual_copies_count = 0
        self._arb_copies_count = 0

        # Dedup
        self._seen_tx_hashes: Set[str] = set()

        # Paper trades list
        self._paper_trades: List[object] = []

        # Market resolution tracking
        self._last_resolution_check = datetime.min.replace(tzinfo=timezone.utc)
        self._resolution_check_interval = 15  # Check every 15s, 1/4 of markets per cycle
        self._resolution_check_cohort = 0  # Cycles 0-3 for staggered checks
        self._market_cache: Dict[str, dict] = {}

        # Extra state from orchestrator (e.g. LLM caches) — merged into save_state
        self._extra_state_providers: Dict[str, object] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def total_exposure(self) -> float:
        return self._total_exposure

    @total_exposure.setter
    def total_exposure(self, value: float):
        self._total_exposure = value

    # ------------------------------------------------------------------
    # Position creation
    # ------------------------------------------------------------------

    def create_position(self, CopiedPosition, whale_address: str, whale_name: str,
                        trade: dict, our_size: float, our_shares: float):
        """Create a tracked position when we copy a whale BUY."""
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
            category=trade.get("_category"),
        )

        self.positions[position_id] = position

        # Track whale's category mix for category-aware selection (A5)
        if position.category:
            self._whale_manager.record_whale_category(whale_address, position.category)

        self.save_state()
        return position

    # ------------------------------------------------------------------
    # Trade copying (unified paper + live flow)
    # ------------------------------------------------------------------

    async def copy_trade(self, whale, trade: dict, PaperTrade, CopiedPosition):
        """Execute a copy of the whale's trade — unified flow for paper and live."""
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
            logger.debug(f"skip: {side} trade (not copying non-BUY)")
            return

        self._entry_prices[asset_id] = price
        whale.trades_copied += 1
        self._whale_copies_count += 1

        live_enabled = self._config.get("live_trading_enabled", False)

        # === UNIFIED FLOW: Submit order (if live), then track position ===
        if live_enabled and self._live_trader:
            # LIVE MODE: Submit order first, only create position on success
            order = await self._execute_live_buy(
                token_id=asset_id,
                price=price,
                market_title=title,
            )

            if order and order.status in ("filled", "dry_run"):
                our_shares = order.size
                our_size_usd = order.cost_usd
                position = self.create_position(
                    CopiedPosition,
                    whale_address=whale.address,
                    whale_name=whale.name,
                    trade=trade,
                    our_size=our_size_usd,
                    our_shares=our_shares,
                )
                position.live_shares = order.size
                position.live_cost_usd = order.cost_usd
                position.live_order_id = order.order_id
                self._total_exposure += our_size_usd
                self.save_state()

                mode = "DRY RUN" if self._live_trader.dry_run else "LIVE"
                logger.info(
                    f"SIGNAL {whale.name} | {mode} BUY ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                    f"({our_shares:.2f} shares) | {title[:35]}"
                )

                paper_trade = PaperTrade(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    whale_address=whale.address, whale_name=whale.name,
                    side=side, outcome=outcome, price=price,
                    whale_size=whale_size, our_size=our_size_usd,
                    our_shares=our_shares, market_title=title,
                    condition_id=condition_id, asset_id=asset_id, tx_hash=tx_hash,
                )
                await self._reporter.slack_trade_alert(paper_trade, position)
            else:
                err = order.error_message if order else "no order returned"
                logger.warning(
                    f"LIVE ORDER FAILED: {outcome} @ {price:.1%} | {err} | {title[:30]}"
                )
        else:
            # PAPER MODE: Create position with conviction-weighted sizing
            conviction_size = self._whale_manager.conviction_size(whale, whale_size * price)
            # A6: Apply category efficiency multiplier
            cat_conviction = trade.get("_category_conviction", 1.0)
            conviction_size *= cat_conviction
            # C1: Apply LLM market tag score adjustment (±9 pts mapped to ±15% sizing)
            llm_adj = trade.get("_llm_score_adj", 0.0)
            if llm_adj != 0:
                conviction_size *= 1.0 + (llm_adj / 60.0)  # ±9 → ±15%
            max_exposure = self._config.get("max_total_exposure", 100.0)
            remaining = max_exposure - self._total_exposure
            our_size_usd = min(conviction_size, remaining)
            our_shares = our_size_usd / price if price > 0 else 0

            position = self.create_position(
                CopiedPosition,
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
                f"SIGNAL {whale.name} | PAPER BUY ${our_size_usd:.2f} of {outcome} @ {price:.1%} "
                f"({our_shares:.2f} shares) | {title[:35]}"
            )
            await self._reporter.slack_trade_alert(paper_trade, position)

    # ------------------------------------------------------------------
    # Live order execution
    # ------------------------------------------------------------------

    async def _execute_live_buy(self, token_id: str, price: float,
                                market_title: str):
        """Execute a live BUY order on Polymarket. Returns LiveOrder or None."""
        if not self._live_trader:
            return None

        live_max_exposure = self._config.get("live_max_exposure", 50.0)
        remaining_exposure = live_max_exposure - self._total_exposure
        if remaining_exposure <= 0:
            logger.debug(
                f"skip exposure: ${self._total_exposure:.2f}/${live_max_exposure:.0f}"
            )
            return None

        try:
            randomized_size = round(random.uniform(1.20, 1.60), 2)
            live_size_usd = min(randomized_size, remaining_exposure)

            if live_size_usd <= 0:
                actual_balance = self._live_trader.get_collateral_balance()
                if actual_balance is not None and actual_balance > 0:
                    logger.debug(
                        f"exposure tracker reset: tracked ${self._total_exposure:.2f}/${live_max_exposure:.2f}, "
                        f"actual USDC ${actual_balance:.2f}"
                    )
                    self._total_exposure = max(0.0, live_max_exposure - actual_balance)
                    remaining_exposure = live_max_exposure - self._total_exposure
                    live_size_usd = min(randomized_size, remaining_exposure)
                if live_size_usd <= 0:
                    logger.debug(f"skip exposure: max exposure reached after reset")
                    return None

            self._live_trader._total_exposure = self._total_exposure

            order = await self._live_trader.submit_buy_order(
                token_id=token_id,
                price=price,
                size_usd=live_size_usd,
                market_title=market_title,
            )

            if order and order.status == "failed":
                logger.warning(
                    f"LIVE ORDER FAILED: @ {price:.1%} | "
                    f"{order.error_message or 'unknown error'} | {market_title[:30]}"
                )
            elif not order:
                logger.warning(f"LIVE: No order returned for {market_title[:30]}")

            return order

        except Exception as e:
            logger.error(f"LIVE: Error executing buy: {e}")
            return None

    async def execute_live_sell(self, position, price: float, reason: str) -> bool:
        """Execute a live SELL order. Returns True if succeeded."""
        if not self._live_trader:
            return False

        shares_to_sell = position.live_shares or position.shares
        if not shares_to_sell or shares_to_sell <= 0:
            logger.warning(f"LIVE: No shares to sell for {position.market_title[:30]}")
            return False

        try:
            self._live_trader._total_exposure = self._total_exposure

            order = await self._live_trader.submit_sell_order(
                token_id=position.token_id,
                price=price,
                shares=shares_to_sell,
                market_title=position.market_title,
            )

            if order and order.status in ("filled", "dry_run"):
                mode = "DRY RUN" if self._live_trader.dry_run else "LIVE"
                logger.info(
                    f"{mode} SELL: {shares_to_sell:.2f} shares @ {price:.1%} ({reason})"
                )
                return True
            elif order and order.status == "failed":
                logger.warning(
                    f"LIVE SELL FAILED: {order.error_message or 'unknown'} | {position.market_title[:30]}"
                )
                return False
            else:
                logger.warning(f"LIVE: No sell order returned for {position.market_title[:30]}")
                return False

        except Exception as e:
            logger.error(f"LIVE: Error executing sell: {e}")
            return False

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    async def execute_copy_sell(self, signal: dict, PaperTrade):
        """Sell our position when whale sells — unified for paper and live."""
        position = signal["position"]
        sell_price = signal["whale_sell_price"]

        effective_shares = position.live_shares or position.shares
        cost = position.live_cost_usd or position.copy_amount_usd
        proceeds = sell_price * effective_shares
        pnl = proceeds - cost

        live_enabled = self._config.get("live_trading_enabled", False)

        # === LIVE MODE: Submit sell order first ===
        if live_enabled and self._live_trader:
            sell_succeeded = await self.execute_live_sell(position, sell_price, "whale_sold")
            if not sell_succeeded:
                logger.warning(
                    f"Live sell failed for {position.market_title[:30]} - closing anyway (whale exited)"
                )

        # Update position
        position.status = "closed"
        position.exit_price = sell_price
        position.exit_time = datetime.now(timezone.utc).isoformat()
        position.exit_reason = "whale_sold"
        position.pnl = pnl

        # Update totals
        self._realized_pnl += pnl
        self._whale_manager.record_copy_pnl(position.whale_address, pnl)
        self._whale_manager.record_category_pnl(getattr(position, "category", None) or "unknown", pnl)
        cost = position.live_cost_usd or position.copy_amount_usd
        self._total_exposure -= cost
        if self._total_exposure < 0:
            self._total_exposure = 0
        self._positions_closed += 1
        if pnl > 0:
            self._positions_won += 1
        elif pnl < 0:
            self._positions_lost += 1

        self.save_state()

        # Log
        mode_label = "LIVE" if live_enabled else "PAPER"
        logger.info(
            f"SIGNAL {position.whale_name} | {mode_label} SELL (whale exited) {position.outcome} "
            f"@ {sell_price:.1%} | entry {position.entry_price:.1%} | P&L ${pnl:+.2f}"
        )

        # Save exit trade record in paper mode
        if not live_enabled:
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

        await self._reporter.slack_exit_alert(position, pnl, "Whale Sold")

    # ------------------------------------------------------------------
    # Market resolutions
    # ------------------------------------------------------------------

    async def check_market_resolutions(self):
        """Check if any markets with open positions have resolved.

        Uses cohort-based staggering: checks 1/4 of markets per cycle (every 15s),
        so each market is checked every ~60s but load is spread across 4 cycles.
        Fetches market data (cached 60s) instead of per-token prices — one API call
        per market returns both prices and resolution status.
        """
        now = datetime.now(timezone.utc)
        if (now - self._last_resolution_check).total_seconds() < self._resolution_check_interval:
            return

        self._last_resolution_check = now
        current_cohort = self._resolution_check_cohort
        self._resolution_check_cohort = (self._resolution_check_cohort + 1) % 4

        open_positions = [p for p in self.positions.values() if p.status == "open"]
        if not open_positions:
            return

        # Group positions by market_id (condition_id)
        markets: Dict[str, list] = {}
        for pos in open_positions:
            markets.setdefault(pos.market_id, []).append(pos)

        # Only check markets in this cohort
        cohort_markets = {
            mid: positions for mid, positions in markets.items()
            if hash(mid) % 4 == current_cohort
        }

        if not cohort_markets:
            return

        logger.debug(
            f"Resolution check cohort {current_cohort}/3: "
            f"{len(cohort_markets)}/{len(markets)} markets"
        )

        for condition_id, market_positions in cohort_markets.items():
            try:
                market_data = await self._market_data.fetch_market(condition_id, max_age=60)
                if not market_data:
                    continue

                # Extract all token prices from this market's data
                token_prices = self._market_data.extract_token_prices(market_data)
                self._current_prices.update(token_prices)

                # Check resolution
                is_resolved, winning_outcome = self._market_data.is_resolved(market_data)
                if is_resolved and winning_outcome:
                    for pos in market_positions:
                        if pos.status == "open":
                            await self._close_position_at_resolution(
                                pos.position_id, winning_outcome, market_data
                            )
                elif is_resolved:
                    logger.debug(
                        f"Market {condition_id[:20]}... resolved but no winning outcome"
                    )

            except Exception as e:
                logger.warning(f"Error checking resolution for market {condition_id[:20]}...: {e}")

        # Check SL/TP/stale exits for positions in this cohort
        cohort_positions = [
            pos for pos in open_positions
            if hash(pos.market_id) % 4 == current_cohort and pos.status == "open"
        ]
        if cohort_positions:
            await self._check_exit_conditions(cohort_positions)

    # B3: Trailing stop constants
    TRAILING_STOP_ACTIVATION_PCT = 0.10  # Activate after 10% gain
    TRAILING_STOP_TRAIL_PCT = 0.05       # 5% below high-water mark

    async def _check_exit_conditions(self, positions_to_check: list):
        """Check SL/TP/trailing stop/stale exits for given positions using cached prices."""
        now = datetime.now(timezone.utc)
        stop_loss_pct = self._config.get("stop_loss_pct", 0.15)
        take_profit_pct = self._config.get("take_profit_pct", 0.20)
        stale_hours = self._config.get("stale_position_hours", 48)
        live_enabled = self._config.get("live_trading_enabled", False)

        for pos in positions_to_check:
            if pos.status != "open":
                continue

            current_price = self._current_prices.get(pos.token_id)
            if current_price is None:
                continue

            effective_shares = pos.live_shares or pos.shares
            cost = pos.live_cost_usd or pos.copy_amount_usd
            pnl = (current_price * effective_shares) - cost
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0

            # B3: Update high-water mark
            hwm = getattr(pos, "high_water_mark", None) or pos.entry_price
            if current_price > hwm:
                pos.high_water_mark = current_price
                hwm = current_price

            exit_reason = None

            # Binary event markets (sports) swing 20-40% mid-event;
            # stop-loss locks in premature losses — let them resolve.
            category = getattr(pos, "category", None) or "unknown"
            NO_STOP_LOSS_CATEGORIES = {"sports"}

            if pnl_pct <= -stop_loss_pct and category not in NO_STOP_LOSS_CATEGORIES:
                exit_reason = "stop_loss"
            elif pnl_pct >= take_profit_pct:
                exit_reason = "take_profit"
            else:
                # B3: Trailing stop — after 10% gain, close if 5% below HWM
                hwm_gain_pct = (hwm - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
                if hwm_gain_pct >= self.TRAILING_STOP_ACTIVATION_PCT:
                    drop_from_hwm = (hwm - current_price) / hwm if hwm > 0 else 0
                    if drop_from_hwm >= self.TRAILING_STOP_TRAIL_PCT:
                        exit_reason = "trailing_stop"

            if not exit_reason:
                try:
                    entry_time = datetime.fromisoformat(pos.entry_time)
                    if entry_time.tzinfo is None:
                        entry_time = entry_time.replace(tzinfo=timezone.utc)
                    hours_held = (now - entry_time).total_seconds() / 3600
                    if hours_held >= stale_hours and abs(pnl_pct) < 0.02:
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
                self._whale_manager.record_copy_pnl(pos.whale_address, pnl)
                self._whale_manager.record_category_pnl(getattr(pos, "category", None) or "unknown", pnl)
                self._total_exposure -= cost
                if self._total_exposure < 0:
                    self._total_exposure = 0
                self._positions_closed += 1
                if pnl > 0:
                    self._positions_won += 1
                elif pnl < 0:
                    self._positions_lost += 1

                logger.info(
                    f"SIGNAL {pos.whale_name} | {exit_reason.upper()}: "
                    f"{pos.outcome} @ {pos.entry_price:.1%}->{current_price:.1%} "
                    f"| P&L ${pnl:+.2f} ({pnl_pct:+.1%}) "
                    f"| {pos.market_title[:35]}"
                )

                if live_enabled and self._live_trader:
                    await self.execute_live_sell(pos, current_price, exit_reason)

                await self._reporter.slack_exit_alert(pos, pnl, exit_reason.replace("_", " ").title())

                self.save_state()

    async def _close_position_at_resolution(self, pos_id: str, winning_outcome: str, market_data: dict):
        """Close a position when market resolves — unified for paper and live."""
        position = self.positions.get(pos_id)
        if not position or position.status != "open":
            return

        if winning_outcome:
            did_win = position.outcome.lower() == winning_outcome.lower()
            exit_price = 1.0 if did_win else 0.0
        else:
            exit_price = position.entry_price

        effective_shares = position.live_shares or position.shares
        cost = position.live_cost_usd or position.copy_amount_usd
        proceeds = exit_price * effective_shares
        pnl = proceeds - cost

        position.status = "closed"
        position.exit_price = exit_price
        position.exit_time = datetime.now(timezone.utc).isoformat()
        position.exit_reason = "resolved"
        position.pnl = pnl

        self._realized_pnl += pnl
        self._whale_manager.record_copy_pnl(position.whale_address, pnl)
        self._whale_manager.record_category_pnl(getattr(position, "category", None) or "unknown", pnl)
        self._total_exposure -= cost
        if self._total_exposure < 0:
            self._total_exposure = 0
        self._positions_closed += 1
        if pnl > 0:
            self._positions_won += 1
        elif pnl < 0:
            self._positions_lost += 1

        self.save_state()

        result = "WON" if exit_price == 1.0 else "LOST" if exit_price == 0.0 else "UNKNOWN"
        live_enabled = self._config.get("live_trading_enabled", False)
        mode_label = "LIVE" if live_enabled else "PAPER"
        logger.info(
            f"SIGNAL {position.whale_name} | {mode_label} RESOLVED {result} "
            f"| {position.outcome} | P&L ${pnl:+.2f} | {position.market_title[:35]}"
        )

        if not live_enabled:
            from run_whale_copy_trader import PaperTrade
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

        # Auto-redeem winning positions
        if live_enabled and self._redeemer and exit_price == 1.0:
            try:
                is_neg_risk = False
                if self._live_trader and self._live_trader._client:
                    try:
                        is_neg_risk = self._live_trader._client.get_neg_risk(position.token_id)
                    except Exception:
                        pass

                redeemed = self._redeemer.redeem(
                    condition_id=position.market_id,
                    is_neg_risk=is_neg_risk,
                )
                if redeemed:
                    logger.info(f"REDEEMED: {position.market_title[:40]} -> USDC returned")
                else:
                    logger.warning(f"Redemption failed for {position.market_title[:30]} - redeem manually")
            except Exception as e:
                logger.warning(f"Redemption error: {e} - redeem manually")

        result_str = "WON" if exit_price == 1.0 else "LOST" if exit_price == 0.0 else "RESOLVED"
        await self._reporter.slack_exit_alert(position, pnl, f"Market {result_str}")

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def reconcile_positions(self, fetch_whale_trades, BalanceAllowanceParams, AssetType):
        """
        Reconcile saved positions against current market state on startup.

        Args:
            fetch_whale_trades: async callable(address, limit) -> List[dict]
            BalanceAllowanceParams: class for balance queries
            AssetType: enum for asset types
        """
        live_enabled = self._config.get("live_trading_enabled", False)

        # --- Step 0: Recover falsely-closed positions (live mode only) ---
        recovered_count = 0
        if live_enabled and self._live_trader and self._live_trader._client:
            closed_positions = {
                pid: pos for pid, pos in self.positions.items()
                if pos.status == "closed" and pos.token_id
            }

            if closed_positions:
                logger.debug(
                    f"Checking {len(closed_positions)} closed positions for on-chain balances"
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
                            old_reason = pos.exit_reason
                            old_pnl = pos.pnl or 0

                            self._realized_pnl -= old_pnl
                            self._positions_closed -= 1
                            if old_pnl > 0:
                                self._positions_won -= 1
                            elif old_pnl < 0:
                                self._positions_lost -= 1

                            pos.status = "open"
                            pos.exit_price = None
                            pos.exit_time = None
                            pos.exit_reason = None
                            pos.pnl = None
                            pos.live_shares = balance_shares

                            recovered_count += 1
                            cost = pos.live_cost_usd or pos.copy_amount_usd
                            logger.info(
                                f"RECOVERED: {pos.market_title[:40]} "
                                f"| {balance_shares:.2f} shares on-chain | was '{old_reason}'"
                            )

                    except Exception as e:
                        logger.debug(
                            f"   Could not check balance for closed pos {pos.market_title[:30]}...: {e}"
                        )

                    await asyncio.sleep(0.2)

                if recovered_count > 0:
                    logger.info(f"Recovered {recovered_count} falsely-closed positions")

        open_positions = {
            pid: pos for pid, pos in self.positions.items()
            if pos.status == "open"
        }

        if not open_positions:
            logger.info("Reconciliation: no open positions to reconcile")
            return

        logger.info(f"Reconciling {len(open_positions)} open positions...")

        resolved_count = 0
        price_updated = 0
        live_warnings = 0

        # --- Step 1: Check if markets resolved while we were down ---
        for pos_id, pos in list(open_positions.items()):
            try:
                market_data = await self._market_data.fetch_market(pos.market_id, max_age=60)
                if not market_data:
                    logger.warning(
                        f"Could not fetch market data for {pos.market_title[:40]}"
                    )
                    await asyncio.sleep(0.2)
                    continue

                is_resolved, winning_outcome = self._market_data.is_resolved(market_data)
                if is_resolved:
                    logger.info(
                        f"Market resolved while down: {pos.market_title[:40]} -> {winning_outcome or 'unknown'}"
                    )
                    await self._close_position_at_resolution(
                        pos_id, winning_outcome, market_data
                    )
                    resolved_count += 1
                    del open_positions[pos_id]

            except Exception as e:
                logger.warning(f"Error checking resolution for {pos.market_title[:30]}: {e}")

            await asyncio.sleep(0.2)

        # --- Step 2: Check if whales sold positions while we were down ---
        whale_sold_count = 0
        if open_positions:
            whale_positions: Dict[str, List[str]] = {}
            for pos_id, pos in open_positions.items():
                whale_positions.setdefault(pos.whale_address, []).append(pos_id)

            logger.debug(f"Checking {len(whale_positions)} whale wallets for sells while down")

            for whale_addr, pos_ids in whale_positions.items():
                try:
                    trades = await fetch_whale_trades(whale_addr, limit=50)
                    sell_trades = [t for t in trades if t.get("side") == "SELL"]

                    for sell_trade in sell_trades:
                        sell_token = sell_trade.get("asset", "")
                        sell_price = sell_trade.get("price", 0)
                        sell_size = sell_trade.get("size", 0)

                        for pos_id in list(pos_ids):
                            pos = self.positions.get(pos_id)
                            if not pos or pos.status != "open":
                                continue
                            if pos.token_id == sell_token:
                                logger.info(
                                    f"Whale sold while down: {pos.whale_name} exited "
                                    f"{pos.market_title[:40]} @ {sell_price:.1%}"
                                )
                                effective_shares = pos.live_shares or pos.shares
                                pnl = (sell_price - pos.entry_price) * effective_shares

                                pos.status = "closed"
                                pos.exit_price = sell_price
                                pos.exit_time = datetime.now(timezone.utc).isoformat()
                                pos.exit_reason = "whale_sold"
                                pos.pnl = pnl

                                self._realized_pnl += pnl
                                self._whale_manager.record_copy_pnl(pos.whale_address, pnl)
                                self._whale_manager.record_category_pnl(getattr(pos, "category", None) or "unknown", pnl)
                                self._positions_closed += 1
                                if pnl > 0:
                                    self._positions_won += 1
                                elif pnl < 0:
                                    self._positions_lost += 1

                                logger.info(
                                    f"Closing (whale exited while down): {pos.outcome} "
                                    f"@ {sell_price:.1%} | entry {pos.entry_price:.1%} "
                                    f"| P&L ${pnl:+.2f}"
                                )

                                if live_enabled and self._live_trader:
                                    sell_ok = await self.execute_live_sell(pos, sell_price, "whale_sold")
                                    if not sell_ok:
                                        logger.warning(
                                            f"Live sell failed on reconciliation for {pos.market_title[:30]}"
                                        )

                                await self._reporter.slack_exit_alert(pos, pnl, "Whale Sold (while down)")

                                whale_sold_count += 1
                                del open_positions[pos_id]
                                pos_ids.remove(pos_id)
                                break

                except Exception as e:
                    logger.warning(f"Error checking whale sells for {whale_addr[:12]}: {e}")

                await asyncio.sleep(0.3)

        # --- Step 3: Fetch current prices via fetch_market (batched by market) ---
        market_ids = {pos.market_id for pos in open_positions.values()}
        for condition_id in market_ids:
            try:
                market_data = await self._market_data.fetch_market(condition_id)
                if market_data:
                    token_prices = self._market_data.extract_token_prices(market_data)
                    self._current_prices.update(token_prices)
                    price_updated += len(token_prices)
            except Exception as e:
                logger.debug(f"   Could not fetch market {condition_id[:20]}...: {e}")

            await asyncio.sleep(0.2)

        # --- Step 3b: Stop-loss / Take-profit / Stale position cleanup ---
        sl_tp_count = 0
        await self._check_exit_conditions(list(open_positions.values()))
        # Count how many were closed by exit conditions
        for pos_id in list(open_positions.keys()):
            pos = self.positions.get(pos_id)
            if pos and pos.status != "open":
                sl_tp_count += 1
                del open_positions[pos_id]

        # --- Step 4: Live mode — verify on-chain balances ---
        settled_count = 0
        if live_enabled and self._live_trader and self._live_trader._client:
            logger.debug("Verifying on-chain balances via CLOB API")
            for pos_id, pos in list(open_positions.items()):
                try:
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=pos.token_id,
                        signature_type=2,
                    )
                    balance_info = self._live_trader._client.get_balance_allowance(params)
                    balance = float(balance_info.get("balance", 0)) if balance_info else 0
                    balance_shares = balance / 1e6 if balance > 100 else balance

                    if balance_shares <= 0.001:
                        market_data_cached = self._market_cache.get(pos.market_id)
                        exit_price = pos.entry_price
                        result_note = "settled (zero balance)"

                        if market_data_cached:
                            tokens = market_data_cached.get("tokens", [])
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
                        self._whale_manager.record_copy_pnl(pos.whale_address, pnl)
                        self._whale_manager.record_category_pnl(getattr(pos, "category", None) or "unknown", pnl)
                        self._total_exposure -= cost
                        if self._total_exposure < 0:
                            self._total_exposure = 0
                        self._positions_closed += 1
                        if pnl > 0:
                            self._positions_won += 1
                        elif pnl < 0:
                            self._positions_lost += 1

                        logger.info(
                            f"{result_note}: {pos.market_title[:40]} | P&L ${pnl:+.2f}"
                        )
                        settled_count += 1
                        del open_positions[pos_id]
                    else:
                        if pos.live_shares and abs(balance_shares - pos.live_shares) > 0.01:
                            logger.debug(
                                f"Balance update: {pos.market_title[:35]} "
                                f"tracked={pos.live_shares:.2f} -> on-chain={balance_shares:.2f}"
                            )
                            pos.live_shares = balance_shares
                except Exception as e:
                    logger.debug(f"   Could not verify balance for {pos.market_title[:30]}...: {e}")

                await asyncio.sleep(0.2)

        # --- Step 5: Recalculate exposure from actual open positions ---
        remaining_open = [p for p in self.positions.values() if p.status == "open"]
        self._total_exposure = sum(
            (p.live_cost_usd or p.copy_amount_usd) for p in remaining_open
        )

        # --- Step 6: Log reconciliation report ---
        unrealized = self.calculate_unrealized_pnl()
        logger.info(
            f"Reconciliation complete: "
            f"{resolved_count} resolved, {whale_sold_count} whale-sold, "
            f"{sl_tp_count} SL/TP/stale, {settled_count} settled, "
            f"{len(remaining_open)} open | "
            f"exposure ${self._total_exposure:.2f} | P&L ${unrealized:+.2f}"
        )

        self.save_state()

    # ------------------------------------------------------------------
    # Portfolio stats
    # ------------------------------------------------------------------

    def calculate_unrealized_pnl(self) -> float:
        """Calculate unrealized P&L for open positions."""
        unrealized = 0.0
        for position in self.positions.values():
            if position.status == "open":
                current_price = self._current_prices.get(position.token_id, position.entry_price)
                effective_shares = position.live_shares or position.shares
                cost = position.live_cost_usd or position.copy_amount_usd
                current_value = current_price * effective_shares
                pnl = current_value - cost
                unrealized += pnl
        return unrealized

    def get_portfolio_stats(self) -> dict:
        """Calculate comprehensive portfolio statistics."""
        open_positions = [p for p in self.positions.values() if p.status == "open"]
        closed_positions = [p for p in self.positions.values() if p.status == "closed"]

        unrealized_pnl = self.calculate_unrealized_pnl()
        total_pnl = self._realized_pnl + unrealized_pnl

        open_cost = sum(
            (p.live_cost_usd or p.copy_amount_usd) for p in open_positions
        )

        open_market_value = 0.0
        for p in open_positions:
            current_price = self._current_prices.get(p.token_id, p.entry_price)
            effective_shares = p.live_shares or p.shares
            open_market_value += current_price * effective_shares

        usdc_balance = None
        if self._config.get("live_trading_enabled") and self._live_trader:
            usdc_balance = self._live_trader.get_collateral_balance()

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
            "open_exposure": open_cost,
        }

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def save_state(self):
        """Save positions and P&L to disk so they survive restarts."""
        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "positions": {pid: asdict(pos) for pid, pos in self.positions.items()},
            "position_counter": self._position_counter,
            "realized_pnl": self._realized_pnl,
            "positions_closed": self._positions_closed,
            "positions_won": self._positions_won,
            "positions_lost": self._positions_lost,
            "total_exposure": self._total_exposure,
            "active_whale_count": self._whale_manager.active_whale_count if self._whale_manager else 8,
            "seen_tx_hashes": list(self._seen_tx_hashes)[-2000:],
            "entry_prices": self._entry_prices,
            "whale_copy_pnl": self._whale_manager._whale_copy_pnl if self._whale_manager else {},
            "pruned_whales": list(self._whale_manager._pruned_whales if self._whale_manager else set()),
            "category_copy_pnl": self._whale_manager._category_copy_pnl if self._whale_manager else {},
            "whale_category_mix": self._whale_manager._whale_category_mix if self._whale_manager else {},
            "category_cache": self._market_data.category_cache if self._market_data else {},
        }

        # Merge extra state from orchestrator (LLM caches, etc.)
        for key, provider in self._extra_state_providers.items():
            if hasattr(provider, "to_dict"):
                state[key] = provider.to_dict()

        try:
            os.makedirs(os.path.dirname(self._state_file) or ".", exist_ok=True)
            tmp_file = self._state_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_file, self._state_file)
            logger.debug(f"State saved: {len(state['positions'])} positions")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def load_state(self, CopiedPosition):
        """Load positions and P&L from disk on startup."""
        if not os.path.exists(self._state_file):
            logger.info("No saved state found - starting fresh")
            return {}

        try:
            with open(self._state_file, "r") as f:
                state = json.load(f)

            for pid, pos_dict in state.get("positions", {}).items():
                self.positions[pid] = CopiedPosition(**pos_dict)

            self._position_counter = state.get("position_counter", 0)
            self._realized_pnl = state.get("realized_pnl", 0.0)
            self._positions_closed = state.get("positions_closed", 0)
            self._positions_won = state.get("positions_won", 0)
            self._positions_lost = state.get("positions_lost", 0)
            self._total_exposure = state.get("total_exposure", 0.0)
            self._seen_tx_hashes = set(state.get("seen_tx_hashes", []))
            self._entry_prices = state.get("entry_prices", {})

            # Restore category cache
            if self._market_data:
                self._market_data.from_dict({"category_cache": state.get("category_cache", {})})

            open_count = len([p for p in self.positions.values() if p.status == "open"])
            closed_count = len([p for p in self.positions.values() if p.status == "closed"])
            saved_at = state.get("saved_at", "unknown")

            # Recalculate realized P&L from closed positions
            recalculated_pnl = 0.0
            for p in self.positions.values():
                if p.status == "closed" and p.exit_price is not None:
                    eff_shares = p.live_shares or p.shares
                    cost = p.live_cost_usd or p.copy_amount_usd
                    recalculated_pnl += (p.exit_price * eff_shares) - cost
            if abs(recalculated_pnl - self._realized_pnl) > 0.01:
                logger.info(
                    f"P&L recalculated: ${self._realized_pnl:+.2f} -> ${recalculated_pnl:+.2f}"
                )
                self._realized_pnl = recalculated_pnl

            live_enabled = self._config.get("live_trading_enabled", False)
            mode_label = "LIVE" if live_enabled else "PAPER"
            logger.info(
                f"State restored from {saved_at} [{mode_label}]: "
                f"{open_count} open positions, {closed_count} closed, "
                f"P&L: ${self._realized_pnl:+.2f}, "
                f"exposure: ${self._total_exposure:.2f}"
            )

            # Return whale-manager state for the orchestrator to pass through
            return {
                "active_whale_count": state.get("active_whale_count", 8),
                "whale_copy_pnl": state.get("whale_copy_pnl", {}),
                "pruned_whales": state.get("pruned_whales", []),
                "category_copy_pnl": state.get("category_copy_pnl", {}),
                "whale_category_mix": state.get("whale_category_mix", {}),
                "market_tag_cache": state.get("market_tag_cache", {}),
            }

        except Exception as e:
            logger.error(f"Failed to load state: {e} - starting fresh")
            return {}

    # ------------------------------------------------------------------
    # Trade logging
    # ------------------------------------------------------------------

    def _save_trade(self, trade):
        """Save trade to JSONL file."""
        os.makedirs("paper_trades", exist_ok=True)
        filename = f"paper_trades/whale_copies_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"

        with open(filename, "a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
