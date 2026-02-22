"""
Cluster and hedge detection for whale trades.

Detects coordinated trading (multiple wallets betting same direction)
and analyzes hedging behavior (wallets trading both sides of a market).
"""

import logging
import time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class ClusterDetector:
    """Detects cluster trading patterns and analyzes hedging behavior."""

    # Cluster detection settings
    CLUSTER_WINDOW_SECONDS = 300  # 5 minute window for cluster detection
    CLUSTER_MIN_WALLETS = 3  # Minimum wallets betting same direction to trigger cluster signal
    CLUSTER_MIN_VOLUME = 1000  # Minimum combined volume for cluster signal

    def __init__(self):
        # Track recent trades by market for cluster detection
        # Format: {condition_id: [(timestamp, wallet, side, value), ...]}
        self._recent_market_trades: Dict[str, List[tuple]] = {}
        self._cluster_signals = 0

        # Track recent trades by wallet+market to detect hedging (buying both sides)
        # Format: {(wallet, condition_id): [(timestamp, outcome, value, shares, price), ...]}
        self._wallet_market_trades: Dict[tuple, List[tuple]] = {}

        # Track NET positions per wallet+market for smart hedging
        # Format: {(wallet, condition_id): {outcome: {"shares": float, "cost": float}, ...}}
        self._wallet_net_positions: Dict[tuple, Dict[str, dict]] = {}

    @property
    def cluster_signals(self) -> int:
        return self._cluster_signals

    def record_wallet_market_trade(self, wallet: str, condition_id: str, outcome: str, value: float, price: float):
        """Record a wallet's trade on a market and update net position with proper payout math"""
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

    def get_net_position(self, wallet: str, condition_id: str) -> Dict[str, float]:
        """Get the net position for a wallet on a market"""
        key = (wallet.lower(), condition_id)
        return self._wallet_net_positions.get(key, {})

    def analyze_hedge(self, wallet: str, condition_id: str, current_outcome: str, current_value: float, current_price: float) -> tuple:
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

    def record_trade_for_cluster(self, condition_id: str, wallet: str, side: str, value: float):
        """Record a trade for cluster detection"""
        now = time.time()

        if condition_id not in self._recent_market_trades:
            self._recent_market_trades[condition_id] = []

        self._recent_market_trades[condition_id].append((now, wallet, side, value))

        # Cleanup old trades (older than cluster window)
        cutoff = now - self.CLUSTER_WINDOW_SECONDS
        self._recent_market_trades[condition_id] = [
            t for t in self._recent_market_trades[condition_id] if t[0] > cutoff
        ]

    async def check_cluster_signals(self):
        """
        Detect cluster signals: multiple wallets betting the same direction
        on the same market within a short time window.

        This often indicates shared information or coordinated trading.
        """
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
                        f"CLUSTER SIGNAL: {unique_wallets} wallets BUY on same market "
                        f"(${total_volume:,.0f} total) in last {self.CLUSTER_WINDOW_SECONDS}s"
                    )

            # Check for sell cluster
            if len(sells) >= self.CLUSTER_MIN_WALLETS:
                unique_wallets = len(set(t[1] for t in sells))
                total_volume = sum(t[3] for t in sells)

                if unique_wallets >= self.CLUSTER_MIN_WALLETS and total_volume >= self.CLUSTER_MIN_VOLUME:
                    self._cluster_signals += 1
                    logger.info(
                        f"CLUSTER SIGNAL: {unique_wallets} wallets SELL on same market "
                        f"(${total_volume:,.0f} total) in last {self.CLUSTER_WINDOW_SECONDS}s"
                    )

        # Cleanup old market entries
        if len(self._recent_market_trades) > 500:
            # Keep only markets with recent activity
            self._recent_market_trades = {
                k: v for k, v in self._recent_market_trades.items()
                if v and v[-1][0] > cutoff
            }
