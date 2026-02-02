"""
Alert scoring system for whale detection
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.database.models import Platform, AlertSeverity
from src.platforms.base import TradeEvent, MarketInfo


@dataclass
class ScoreBreakdown:
    """Detailed breakdown of alert score components"""

    # Base score from trade size
    base_score: float = 0.0

    # Multipliers and components
    new_account_bonus: float = 0.0
    trade_size_factor: float = 0.0
    timing_factor: float = 0.0
    obscurity_factor: float = 0.0

    # Final computed score
    final_score: float = 0.0

    # Reasons this was flagged
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage"""
        return {
            'base_score': self.base_score,
            'new_account_bonus': self.new_account_bonus,
            'trade_size_factor': self.trade_size_factor,
            'timing_factor': self.timing_factor,
            'obscurity_factor': self.obscurity_factor,
            'final_score': self.final_score,
            'reasons': self.reasons
        }


class AlertScorer:
    """
    Computes composite alert scores for trades.

    Score factors:
    - Trade size relative to thresholds
    - New account detection
    - Time until market close
    - Market obscurity (low volume)
    """

    def __init__(self, config: dict):
        """
        Initialize scorer with configuration.

        Args:
            config: Detection configuration dict
        """
        self.config = config

        # Thresholds
        self.large_trade_threshold = config.get('large_trade_usd', 100000)
        self.new_account_days = config.get('new_account_days', 7)
        self.last_minute_secs = config.get('last_minute_secs', 300)
        self.obscure_volume_threshold = config.get('obscure_volume_threshold', 50000)
        self.min_alert_score = config.get('min_alert_score', 60)

        # Scoring weights
        scoring = config.get('scoring', {})
        self.new_account_multiplier = scoring.get('new_account_multiplier', 2.5)
        self.trade_size_weight = scoring.get('trade_size_weight', 1.0)
        self.timing_weight = scoring.get('timing_weight', 1.5)
        self.obscurity_weight = scoring.get('obscurity_weight', 1.2)

    def score_trade(
        self,
        trade: TradeEvent,
        market: Optional[MarketInfo] = None,
        is_new_account: bool = False,
        wallet_age_days: Optional[int] = None
    ) -> ScoreBreakdown:
        """
        Compute alert score for a trade.

        Args:
            trade: The trade event
            market: Market information (if available)
            is_new_account: Whether the wallet is a new account
            wallet_age_days: Wallet age in days (if known)

        Returns:
            ScoreBreakdown with all components
        """
        breakdown = ScoreBreakdown()

        # ========== Base Score from Trade Size ==========
        if trade.amount_usd >= self.large_trade_threshold:
            # Scale from 30-60 based on how much over threshold
            ratio = trade.amount_usd / self.large_trade_threshold
            breakdown.base_score = min(30 + (ratio - 1) * 10, 60)
            breakdown.reasons.append(
                f"Large trade: ${trade.amount_usd:,.0f} "
                f"(threshold: ${self.large_trade_threshold:,.0f})"
            )
        elif trade.amount_usd >= self.large_trade_threshold * 0.5:
            # Medium trade: 15-30 score
            ratio = trade.amount_usd / (self.large_trade_threshold * 0.5)
            breakdown.base_score = 15 + (ratio - 1) * 15
            breakdown.reasons.append(
                f"Medium trade: ${trade.amount_usd:,.0f}"
            )
        else:
            # Small trade: minimal score
            breakdown.base_score = 10

        # ========== New Account Bonus ==========
        if is_new_account:
            breakdown.new_account_bonus = 20 * self.new_account_multiplier

            if wallet_age_days is not None:
                if wallet_age_days < 1:
                    breakdown.new_account_bonus *= 1.5
                    breakdown.reasons.append(
                        f"VERY NEW account (<1 day old)"
                    )
                elif wallet_age_days < 3:
                    breakdown.new_account_bonus *= 1.2
                    breakdown.reasons.append(
                        f"New account ({wallet_age_days} days old)"
                    )
                else:
                    breakdown.reasons.append(
                        f"New account ({wallet_age_days} days old)"
                    )
            else:
                breakdown.reasons.append("New/unknown account")

        # ========== Timing Factor (Last Minute Trades) ==========
        if market and market.close_time:
            # Calculate seconds until close (works for both dataclass and ORM model)
            delta = market.close_time - datetime.utcnow()
            seconds_until_close = int(delta.total_seconds())

            # Only flag if market is closing SOON (positive seconds) and within threshold
            # Don't flag markets that have already closed (negative) or are far in future
            if 0 < seconds_until_close <= self.last_minute_secs:
                if seconds_until_close <= 60:  # Last minute
                    breakdown.timing_factor = 25 * self.timing_weight
                    breakdown.reasons.append(
                        f"LAST MINUTE trade ({seconds_until_close}s before close)"
                    )
                else:
                    # Scale based on how close to close
                    factor = 1 - (seconds_until_close / self.last_minute_secs)
                    breakdown.timing_factor = 15 * factor * self.timing_weight
                    minutes = seconds_until_close // 60
                    breakdown.reasons.append(
                        f"Late trade ({minutes} min before close)"
                    )

        # ========== Market Obscurity Factor ==========
        if market:
            if market.volume_24h < self.obscure_volume_threshold:
                # Obscure market bonus
                ratio = 1 - (market.volume_24h / self.obscure_volume_threshold)
                breakdown.obscurity_factor = 15 * ratio * self.obscurity_weight
                breakdown.reasons.append(
                    f"Obscure market (24h vol: ${market.volume_24h:,.0f})"
                )

            # Also check if trade is large relative to market volume
            if market.volume_24h > 0:
                volume_ratio = trade.amount_usd / market.volume_24h
                if volume_ratio > 0.1:  # More than 10% of daily volume
                    breakdown.trade_size_factor = 10 * volume_ratio * self.trade_size_weight
                    breakdown.reasons.append(
                        f"Large relative to market ({volume_ratio:.1%} of 24h vol)"
                    )

        # ========== Compute Final Score ==========
        breakdown.final_score = (
            breakdown.base_score +
            breakdown.new_account_bonus +
            breakdown.trade_size_factor +
            breakdown.timing_factor +
            breakdown.obscurity_factor
        )

        # Cap at 100
        breakdown.final_score = min(100, breakdown.final_score)

        return breakdown

    def should_alert(self, score: ScoreBreakdown) -> bool:
        """Check if score meets alert threshold"""
        return score.final_score >= self.min_alert_score

    def get_severity(self, score: ScoreBreakdown) -> AlertSeverity:
        """Determine alert severity based on score"""
        if score.final_score >= 90:
            return AlertSeverity.CRITICAL
        elif score.final_score >= 75:
            return AlertSeverity.HIGH
        elif score.final_score >= 60:
            return AlertSeverity.MEDIUM
        else:
            return AlertSeverity.LOW

    def format_alert_message(
        self,
        trade: TradeEvent,
        score: ScoreBreakdown,
        market: Optional[MarketInfo] = None
    ) -> str:
        """
        Format human-readable alert message.

        Args:
            trade: The trade event
            score: Score breakdown
            market: Market info (optional)

        Returns:
            Formatted alert message
        """
        severity = self.get_severity(score)
        severity_emoji = {
            AlertSeverity.LOW: "⚪",
            AlertSeverity.MEDIUM: "🟡",
            AlertSeverity.HIGH: "🟠",
            AlertSeverity.CRITICAL: "🔴"
        }

        # Header with severity
        lines = [
            f"{severity_emoji[severity]} **WHALE TRADE DETECTED**",
            ""
        ]

        # Trade details
        action = "BUY" if trade.trade_type.value == "buy" else "SELL"
        lines.extend([
            f"**Wallet:** `{trade.wallet_address[:10]}...{trade.wallet_address[-6:]}`",
            f"**Action:** {action} {trade.outcome or ''}",
            f"**Market:** {trade.market_name[:50]}",
            f"**Value:** ${trade.amount_usd:,.0f}",
        ])

        if trade.price:
            lines.append(f"**Price:** {trade.price:.1%}")

        # Score and reasons
        lines.extend([
            "",
            f"**Alert Score:** {score.final_score:.0f}/100",
            "",
            "**Signals:**"
        ])

        for reason in score.reasons:
            lines.append(f"• {reason}")

        # Platform and timestamp
        lines.extend([
            "",
            f"_Platform: {trade.platform.value.title()}_",
            f"_Time: {trade.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC_"
        ])

        # Explorer link for Polymarket
        if trade.platform == Platform.POLYMARKET and trade.wallet_address:
            lines.append(
                f"\n[View on PolygonScan](https://polygonscan.com/address/{trade.wallet_address})"
            )

        return "\n".join(lines)
