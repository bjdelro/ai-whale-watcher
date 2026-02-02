"""
Risk management for copy trading.
Enforces position limits, loss limits, and trade frequency controls.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    """Risk control parameters"""
    # Per-trade limits
    max_per_trade_usd: float = 1.00  # Maximum $ per trade
    min_copy_score: float = 70.0  # Minimum score to copy

    # Portfolio limits
    max_total_exposure_usd: float = 100.00  # Maximum total $ at risk
    max_positions: int = 50  # Maximum number of open positions
    max_per_market_usd: float = 5.00  # Maximum $ per market

    # Loss limits
    max_daily_loss_usd: float = 20.00  # Stop trading if down this much today
    max_total_loss_usd: float = 50.00  # Stop trading if down this much total

    # Timing controls
    cooldown_seconds: int = 300  # Minimum seconds between trades
    max_trades_per_hour: int = 10  # Rate limit

    # Wallet filters
    allowed_wallet_tiers: List[str] = field(default_factory=lambda: ["elite", "good"])
    min_wallet_trades: int = 20  # Minimum historical trades for wallet

    # Market filters
    min_market_volume_24h: float = 10000.0  # Minimum market volume
    min_hours_to_close: float = 6.0  # Don't trade markets closing soon


@dataclass
class Position:
    """Represents an open position"""
    market_id: str
    market_name: str
    outcome: str  # YES or NO
    side: str  # BUY
    entry_price: float
    size_usd: float
    shares: float
    entry_time: datetime
    copy_score: float
    copied_wallet: str

    # For P&L tracking
    current_price: Optional[float] = None
    unrealized_pnl: float = 0.0


@dataclass
class TradeRecord:
    """Record of an executed trade"""
    timestamp: datetime
    market_id: str
    market_name: str
    outcome: str
    side: str
    price: float
    size_usd: float
    shares: float
    copy_score: float
    copied_wallet: str
    is_paper: bool = True

    # Set when position is closed
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    realized_pnl: Optional[float] = None


class RiskManager:
    """
    Manages risk controls and position tracking for copy trading.

    Enforces limits on:
    - Trade size
    - Total exposure
    - Daily/total losses
    - Trade frequency
    - Wallet quality
    - Market quality
    """

    def __init__(self, limits: Optional[RiskLimits] = None):
        """
        Initialize risk manager.

        Args:
            limits: Risk control parameters (uses defaults if not provided)
        """
        self.limits = limits or RiskLimits()

        # Position tracking
        self.positions: Dict[str, Position] = {}  # market_id -> Position

        # Trade history
        self.trades: List[TradeRecord] = []

        # P&L tracking
        self.realized_pnl: float = 0.0
        self.daily_pnl: float = 0.0
        self.daily_pnl_reset_date: Optional[datetime] = None

        # Rate limiting
        self._last_trade_time: Optional[datetime] = None
        self._trades_this_hour: List[datetime] = []

        # State
        self._halted = False
        self._halt_reason: Optional[str] = None

    def can_trade(self,
                  copy_score: float,
                  wallet_tier: str,
                  wallet_trades: int,
                  market_volume_24h: float,
                  hours_to_close: Optional[float],
                  market_id: str,
                  trade_size_usd: float) -> tuple[bool, str]:
        """
        Check if a trade is allowed under current risk limits.

        Returns:
            (allowed, reason) tuple
        """
        # Check if halted
        if self._halted:
            return False, f"Trading halted: {self._halt_reason}"

        # Reset daily P&L if new day
        self._maybe_reset_daily_pnl()

        # Check copy score threshold
        if copy_score < self.limits.min_copy_score:
            return False, f"Score {copy_score:.0f} below minimum {self.limits.min_copy_score:.0f}"

        # Check wallet quality
        if wallet_tier.lower() not in [t.lower() for t in self.limits.allowed_wallet_tiers]:
            return False, f"Wallet tier '{wallet_tier}' not in allowed tiers"

        if wallet_trades < self.limits.min_wallet_trades:
            return False, f"Wallet has {wallet_trades} trades, need {self.limits.min_wallet_trades}+"

        # Check market quality
        if market_volume_24h < self.limits.min_market_volume_24h:
            return False, f"Market volume ${market_volume_24h:,.0f} below minimum"

        if hours_to_close is not None and hours_to_close < self.limits.min_hours_to_close:
            return False, f"Market closes in {hours_to_close:.1f}h, need {self.limits.min_hours_to_close:.1f}h+"

        # Check trade size
        if trade_size_usd > self.limits.max_per_trade_usd:
            return False, f"Trade ${trade_size_usd:.2f} exceeds max ${self.limits.max_per_trade_usd:.2f}"

        # Check portfolio limits
        total_exposure = sum(p.size_usd for p in self.positions.values())
        if total_exposure + trade_size_usd > self.limits.max_total_exposure_usd:
            return False, f"Would exceed max exposure ${self.limits.max_total_exposure_usd:.2f}"

        if len(self.positions) >= self.limits.max_positions:
            return False, f"At max positions ({self.limits.max_positions})"

        # Check per-market limit
        if market_id in self.positions:
            existing = self.positions[market_id].size_usd
            if existing + trade_size_usd > self.limits.max_per_market_usd:
                return False, f"Would exceed max ${self.limits.max_per_market_usd:.2f} for this market"

        # Check loss limits
        if self.daily_pnl <= -self.limits.max_daily_loss_usd:
            self._halt("Daily loss limit reached")
            return False, f"Daily loss ${abs(self.daily_pnl):.2f} exceeds limit"

        if self.realized_pnl <= -self.limits.max_total_loss_usd:
            self._halt("Total loss limit reached")
            return False, f"Total loss ${abs(self.realized_pnl):.2f} exceeds limit"

        # Check rate limits
        now = datetime.utcnow()

        if self._last_trade_time:
            seconds_since_last = (now - self._last_trade_time).total_seconds()
            if seconds_since_last < self.limits.cooldown_seconds:
                wait_time = self.limits.cooldown_seconds - seconds_since_last
                return False, f"Cooldown: wait {wait_time:.0f}s"

        # Clean up old trades from hourly counter
        hour_ago = now - timedelta(hours=1)
        self._trades_this_hour = [t for t in self._trades_this_hour if t > hour_ago]

        if len(self._trades_this_hour) >= self.limits.max_trades_per_hour:
            return False, f"Rate limit: {self.limits.max_trades_per_hour} trades/hour"

        return True, "OK"

    def record_trade(self, trade: TradeRecord):
        """Record a new trade and update tracking"""
        self.trades.append(trade)
        self._last_trade_time = trade.timestamp
        self._trades_this_hour.append(trade.timestamp)

        # Create position
        position = Position(
            market_id=trade.market_id,
            market_name=trade.market_name,
            outcome=trade.outcome,
            side=trade.side,
            entry_price=trade.price,
            size_usd=trade.size_usd,
            shares=trade.shares,
            entry_time=trade.timestamp,
            copy_score=trade.copy_score,
            copied_wallet=trade.copied_wallet,
        )

        # Add to or update existing position
        if trade.market_id in self.positions:
            existing = self.positions[trade.market_id]
            # Average in (simplified - assumes same direction)
            total_size = existing.size_usd + trade.size_usd
            total_shares = existing.shares + trade.shares
            avg_price = (existing.entry_price * existing.shares + trade.price * trade.shares) / total_shares
            existing.entry_price = avg_price
            existing.size_usd = total_size
            existing.shares = total_shares
        else:
            self.positions[trade.market_id] = position

        logger.info(
            f"Recorded trade: {trade.side} {trade.outcome} on {trade.market_name[:30]}... "
            f"@ {trade.price:.2%} for ${trade.size_usd:.2f}"
        )

    def close_position(self, market_id: str, exit_price: float, reason: str = "manual"):
        """Close a position and record P&L"""
        if market_id not in self.positions:
            logger.warning(f"No position to close for market {market_id}")
            return

        position = self.positions[market_id]

        # Calculate P&L
        # For prediction markets: if we bought YES at 0.60 and it's now 0.70, we made 10 cents per share
        if position.side == "BUY":
            pnl_per_share = exit_price - position.entry_price
        else:
            pnl_per_share = position.entry_price - exit_price

        realized_pnl = pnl_per_share * position.shares

        # Update tracking
        self.realized_pnl += realized_pnl
        self.daily_pnl += realized_pnl

        # Find and update the trade record
        for trade in reversed(self.trades):
            if trade.market_id == market_id and trade.exit_price is None:
                trade.exit_price = exit_price
                trade.exit_time = datetime.utcnow()
                trade.realized_pnl = realized_pnl
                break

        # Remove position
        del self.positions[market_id]

        logger.info(
            f"Closed position: {position.market_name[:30]}... "
            f"Entry: {position.entry_price:.2%} -> Exit: {exit_price:.2%} "
            f"P&L: ${realized_pnl:+.2f} ({reason})"
        )

    def update_position_price(self, market_id: str, current_price: float):
        """Update current price for unrealized P&L calculation"""
        if market_id in self.positions:
            position = self.positions[market_id]
            position.current_price = current_price

            if position.side == "BUY":
                pnl_per_share = current_price - position.entry_price
            else:
                pnl_per_share = position.entry_price - current_price

            position.unrealized_pnl = pnl_per_share * position.shares

    def get_total_unrealized_pnl(self) -> float:
        """Get total unrealized P&L across all positions"""
        return sum(p.unrealized_pnl for p in self.positions.values())

    def get_total_exposure(self) -> float:
        """Get total $ at risk"""
        return sum(p.size_usd for p in self.positions.values())

    def get_stats(self) -> dict:
        """Get current risk manager statistics"""
        return {
            "positions": len(self.positions),
            "total_exposure": self.get_total_exposure(),
            "max_exposure": self.limits.max_total_exposure_usd,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.get_total_unrealized_pnl(),
            "daily_pnl": self.daily_pnl,
            "total_trades": len(self.trades),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
        }

    def _halt(self, reason: str):
        """Halt trading"""
        self._halted = True
        self._halt_reason = reason
        logger.warning(f"Trading HALTED: {reason}")

    def resume(self):
        """Resume trading after halt"""
        self._halted = False
        self._halt_reason = None
        logger.info("Trading resumed")

    def _maybe_reset_daily_pnl(self):
        """Reset daily P&L counter at midnight UTC"""
        today = datetime.utcnow().date()
        if self.daily_pnl_reset_date != today:
            if self.daily_pnl_reset_date is not None:
                logger.info(f"Daily P&L reset. Yesterday: ${self.daily_pnl:+.2f}")
            self.daily_pnl = 0.0
            self.daily_pnl_reset_date = today
