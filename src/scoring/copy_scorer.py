"""
Copy scorer for computing final copy-edge scores.
Combines wallet quality, trade significance, and context.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .features import FeatureExtractor, TradeFeatures
from ..database.models import CanonicalTrade, WalletStats, WalletTier
from ..database.db import Database

logger = logging.getLogger(__name__)


@dataclass
class CopyScore:
    """Result of copy scoring"""
    score: float  # 0-100 scale
    confidence: str  # HIGH, MEDIUM, LOW
    recommendation: str  # STRONG_COPY, COPY, NEUTRAL, AVOID
    features: TradeFeatures
    breakdown: dict  # Score component breakdown


class CopyScorer:
    """
    Compute copy-edge scores for trades.

    The score represents expected profitability of copying a trade,
    accounting for:
    - Wallet historical performance (most important)
    - Trade size relative to book depth
    - Trade intent (opening vs closing)
    - Expected slippage/costs

    Score range: 0-100
    - 70+: Strong copy signal
    - 50-70: Moderate copy signal
    - 30-50: Neutral
    - <30: Avoid copying
    """

    # Scoring weights
    WEIGHTS = {
        "wallet_quality": 40,  # Most important
        "size_significance": 25,  # Boosted — size_vs_wallet_avg is highly predictive
        "intent": 15,
        "market_context": 10,
        "timing": 5,
        "cost_penalty": 10,  # Negative adjustment
    }

    # Confidence thresholds
    CONFIDENCE_THRESHOLDS = {
        "HIGH": 50,  # Wallet has 50+ trades
        "MEDIUM": 20,  # Wallet has 20+ trades
    }

    def __init__(
        self,
        db: Database,
        feature_extractor: Optional[FeatureExtractor] = None,
        min_score_for_alert: float = 50.0,
    ):
        """
        Initialize the copy scorer.

        Args:
            db: Database instance
            feature_extractor: Optional feature extractor (created if not provided)
            min_score_for_alert: Minimum score to generate an alert
        """
        self.db = db
        self.feature_extractor = feature_extractor or FeatureExtractor(db)
        self.min_score_for_alert = min_score_for_alert

        # Stats
        self._trades_scored = 0

    async def score_trade(
        self,
        trade: CanonicalTrade,
        wallet_stats: Optional[WalletStats] = None,
    ) -> CopyScore:
        """
        Compute copy score for a trade.

        Args:
            trade: The canonical trade to score
            wallet_stats: Pre-fetched wallet stats (optional)

        Returns:
            CopyScore with score, confidence, and breakdown
        """
        # Extract features
        features = await self.feature_extractor.extract_features(
            trade, wallet_stats=wallet_stats
        )

        # Compute component scores
        breakdown = {}

        # 1. Wallet Quality Score (0-40 points)
        wallet_score = self._score_wallet_quality(features)
        breakdown["wallet_quality"] = wallet_score

        # 2. Size Significance Score (0-20 points)
        size_score = self._score_size_significance(features)
        breakdown["size_significance"] = size_score

        # 3. Intent Score (0-15 points)
        intent_score = self._score_intent(features)
        breakdown["intent"] = intent_score

        # 4. Market Context Score (0-10 points)
        market_score = self._score_market_context(features)
        breakdown["market_context"] = market_score

        # 5. Timing Score (0-5 points)
        timing_score = self._score_timing(features)
        breakdown["timing"] = timing_score

        # 6. Cost Penalty (-10 to 0 points)
        cost_penalty = self._compute_cost_penalty(features)
        breakdown["cost_penalty"] = cost_penalty

        # Compute total score
        raw_score = (
            wallet_score +
            size_score +
            intent_score +
            market_score +
            timing_score +
            cost_penalty
        )

        # Clamp to 0-100
        final_score = max(0, min(100, raw_score))

        # Determine confidence
        confidence = self._determine_confidence(features)

        # Determine recommendation
        recommendation = self._determine_recommendation(final_score, features)

        self._trades_scored += 1

        return CopyScore(
            score=round(final_score, 1),
            confidence=confidence,
            recommendation=recommendation,
            features=features,
            breakdown=breakdown,
        )

    def _score_wallet_quality(self, features: TradeFeatures) -> float:
        """
        Score based on wallet historical performance.

        Max: 40 points
        """
        score = 0.0

        # Tier-based scoring
        tier_scores = {
            WalletTier.ELITE: 30,
            WalletTier.GOOD: 20,
            WalletTier.NEUTRAL: 5,
            WalletTier.BAD: -15,
            WalletTier.UNKNOWN: 0,
        }
        score += tier_scores.get(features.wallet_tier, 0)

        # Bonus for strong win rate
        if features.wallet_win_rate_1h:
            if features.wallet_win_rate_1h >= 0.65:
                score += 5
            elif features.wallet_win_rate_1h >= 0.55:
                score += 2

        # Bonus for strong Sharpe
        if features.wallet_sharpe_1h:
            if features.wallet_sharpe_1h >= 1.5:
                score += 5
            elif features.wallet_sharpe_1h >= 1.0:
                score += 2

        return min(40, score)

    def _score_size_significance(self, features: TradeFeatures) -> float:
        """
        Score based on trade size relative to book depth and wallet history.

        Max: 25 points (boosted — size_vs_wallet_avg is highly predictive)
        """
        score = 0.0

        # % of book consumed
        if features.pct_book_consumed:
            if features.pct_book_consumed >= 20:  # 20% of book
                score += 13
            elif features.pct_book_consumed >= 10:
                score += 8
            elif features.pct_book_consumed >= 5:
                score += 4
            elif features.pct_book_consumed >= 2:
                score += 2

        # Size vs wallet average — boosted from 5pt to 12pt max
        # Trades that are large relative to a whale's own history are one of the
        # most predictive signals in copy trading (signals unusual conviction)
        if features.size_vs_wallet_avg:
            if features.size_vs_wallet_avg >= 5:  # 5x normal = extreme conviction
                score += 12
            elif features.size_vs_wallet_avg >= 3:  # 3x normal
                score += 9
            elif features.size_vs_wallet_avg >= 2:  # 2x normal
                score += 6
            elif features.size_vs_wallet_avg >= 1.5:
                score += 3

        return min(25, score)

    def _score_intent(self, features: TradeFeatures) -> float:
        """
        Score based on trade intent.

        Opening positions = higher conviction
        Closing positions = lower interest

        Max: 15 points
        """
        intent_scores = {
            "open": 15,  # New position = strong conviction
            "add": 10,  # Adding to position
            "reduce": 0,  # Reducing = neutral
            "close": -5,  # Closing = less interesting
            "unknown": 5,
        }

        return intent_scores.get(features.trade_intent.lower(), 5)

    # Efficiency-aware category adjustments (A6).
    # Sports and high-volume markets are well-priced by professional odds-setters,
    # so insider edge is less likely. Niche/politics/crypto markets have more
    # information asymmetry.
    CATEGORY_EFFICIENCY = {
        "sports": -6,       # Very efficient: professional odds-setting
        "pop_culture": -3,  # Moderately efficient
        "politics": +4,     # Higher info asymmetry potential
        "crypto": +5,       # Insider knowledge common
        "science": +3,      # Niche, fewer traders
        "business": +2,     # Moderate asymmetry
        "unknown": 0,
    }

    def _score_market_context(self, features: TradeFeatures) -> float:
        """
        Score based on market conditions and efficiency.

        Range: -8 to +10 points.
        Efficient markets (sports, high volume) are penalized.
        Inefficient markets (niche, low volume, politics, crypto) are boosted.
        """
        score = 0.0

        # Category-based efficiency adjustment (A6)
        cat = getattr(features, "market_category", None) or "unknown"
        score += self.CATEGORY_EFFICIENCY.get(cat, 0)

        # Markets with more time = more opportunity
        if features.hours_to_close:
            if features.hours_to_close >= 168:  # 1+ week
                score += 5
            elif features.hours_to_close >= 24:  # 1+ day
                score += 3
            elif features.hours_to_close >= 6:
                score += 1
            elif features.hours_to_close < 1:
                score -= 3  # Too close to resolution

        # Price near 50/50 = more room for movement
        if features.price_vs_50 is not None:
            if features.price_vs_50 <= 0.1:  # Within 10% of 0.5
                score += 3
            elif features.price_vs_50 >= 0.4:  # Very extreme (near 0.1 or 0.9)
                score -= 2

        # Volume: high volume in efficient categories = more penalty;
        # low volume in any category = potential niche opportunity
        if features.market_volume_24h:
            if features.market_volume_24h >= 100000:
                score += 2 if cat not in ("sports", "pop_culture") else -2
            elif features.market_volume_24h < 10000:
                score += 3  # Low-volume = niche market, more opportunity

        return max(-8, min(10, score))

    def _score_timing(self, features: TradeFeatures) -> float:
        """
        Score based on timing factors.

        Max: 5 points
        """
        score = 3  # Base score

        # Penalty for after-hours trading
        if features.is_after_hours:
            score -= 2

        return max(0, min(5, score))

    def _compute_cost_penalty(self, features: TradeFeatures) -> float:
        """
        Compute penalty for expected copying costs.

        Returns: -10 to 0 (always negative or zero)
        """
        penalty = 0.0

        # Price impact penalty
        if features.price_impact_bps:
            if features.price_impact_bps >= 50:  # 0.5% impact
                penalty -= 5
            elif features.price_impact_bps >= 20:
                penalty -= 2
            elif features.price_impact_bps >= 10:
                penalty -= 1

        # Spread penalty
        if features.spread_at_entry:
            if features.spread_at_entry >= 100:  # 1% spread
                penalty -= 5
            elif features.spread_at_entry >= 50:
                penalty -= 3
            elif features.spread_at_entry >= 20:
                penalty -= 1

        return max(-10, penalty)

    def _determine_confidence(self, features: TradeFeatures) -> str:
        """Determine confidence level based on data quality"""
        trades = features.wallet_total_trades

        if trades >= self.CONFIDENCE_THRESHOLDS["HIGH"]:
            return "HIGH"
        elif trades >= self.CONFIDENCE_THRESHOLDS["MEDIUM"]:
            return "MEDIUM"
        else:
            return "LOW"

    def _determine_recommendation(
        self, score: float, features: TradeFeatures
    ) -> str:
        """Determine copy recommendation based on score and context"""
        # Strong signal
        if score >= 70 and features.wallet_tier in [WalletTier.ELITE, WalletTier.GOOD]:
            return "STRONG_COPY"

        # Moderate signal
        if score >= 50:
            return "COPY"

        # Neutral
        if score >= 30:
            return "NEUTRAL"

        # Avoid
        return "AVOID"

    def should_alert(self, score: CopyScore) -> bool:
        """Check if score warrants an alert"""
        return score.score >= self.min_score_for_alert

    def format_score_message(self, trade: CanonicalTrade, score: CopyScore) -> str:
        """Format a human-readable score message"""
        features = score.features

        return f"""
Copy Signal Detected!

Trade Details:
- Side: {trade.side} @ ${trade.avg_price:.4f}
- Size: ${trade.total_usd:,.0f} ({features.pct_book_consumed or 0:.1f}% of book depth)
- Intent: {features.trade_intent.upper()}

Wallet Profile:
- Tier: {features.wallet_tier.value.upper()}
- Win Rate (1h): {features.wallet_win_rate_1h * 100:.1f}% (based on {features.wallet_total_trades} trades)
- Avg Markout: {features.wallet_avg_markout_1h or 0:.1f} bps
- Sharpe (1h): {features.wallet_sharpe_1h or 0:.2f}

Copy Score: {score.score:.0f}/100
- Recommendation: {score.recommendation}
- Confidence: {score.confidence}

Score Breakdown:
- Wallet Quality: {score.breakdown['wallet_quality']:.0f}/40
- Size Significance: {score.breakdown['size_significance']:.0f}/25
- Intent: {score.breakdown['intent']:.0f}/15
- Market Context: {score.breakdown['market_context']:.0f}/10
- Timing: {score.breakdown['timing']:.0f}/5
- Cost Penalty: {score.breakdown['cost_penalty']:.0f}
""".strip()

    def get_stats(self) -> dict:
        """Get scorer statistics"""
        return {
            "trades_scored": self._trades_scored,
            "min_score_for_alert": self.min_score_for_alert,
        }
