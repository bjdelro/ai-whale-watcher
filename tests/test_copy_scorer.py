"""Tests for CopyScorer - all 6 scoring components, confidence, recommendation, clamping."""

from unittest.mock import AsyncMock, MagicMock
from src.scoring.copy_scorer import CopyScorer
from src.database.models import WalletTier


class TestScoreWalletQuality:
    """Tests for CopyScorer._score_wallet_quality (max 40 points)."""

    async def test_elite_tier_base_score(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.ELITE)
        score = scorer._score_wallet_quality(features)
        # ELITE=30 + win_rate 0.62>=0.55 -> +2 + sharpe 1.2>=1.0 -> +2 = 34
        assert score == 34.0

    async def test_elite_with_high_win_rate_and_sharpe(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            wallet_tier=WalletTier.ELITE,
            wallet_win_rate_1h=0.70,
            wallet_sharpe_1h=2.0,
        )
        score = scorer._score_wallet_quality(features)
        # 30 + 5 (>=0.65) + 5 (>=1.5) = 40
        assert score == 40.0

    async def test_good_tier(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.GOOD)
        score = scorer._score_wallet_quality(features)
        # 20 + 2 + 2 = 24
        assert score == 24.0

    async def test_bad_tier_negative(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            wallet_tier=WalletTier.BAD,
            wallet_win_rate_1h=0.40,
            wallet_sharpe_1h=0.3,
        )
        score = scorer._score_wallet_quality(features)
        assert score == -15.0

    async def test_unknown_tier(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            wallet_tier=WalletTier.UNKNOWN,
            wallet_win_rate_1h=None,
            wallet_sharpe_1h=None,
        )
        score = scorer._score_wallet_quality(features)
        assert score == 0.0

    async def test_clamped_at_40(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            wallet_tier=WalletTier.ELITE,
            wallet_win_rate_1h=0.80,
            wallet_sharpe_1h=3.0,
        )
        score = scorer._score_wallet_quality(features)
        assert score <= 40.0

    async def test_neutral_tier(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            wallet_tier=WalletTier.NEUTRAL,
            wallet_win_rate_1h=0.50,
            wallet_sharpe_1h=0.5,
        )
        score = scorer._score_wallet_quality(features)
        assert score == 5.0


class TestScoreSizeSignificance:
    """Tests for CopyScorer._score_size_significance (max 25 points).

    Scoring:
    - Book consumed: >=20% → 13, >=10% → 8, >=5% → 4, >=2% → 2
    - Size vs avg:   >=5x → 12, >=3x → 9, >=2x → 6, >=1.5x → 3
    """

    async def test_large_book_consumption(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(pct_book_consumed=25.0, size_vs_wallet_avg=4.0)
        score = scorer._score_size_significance(features)
        # 13 (>=20% book) + 9 (>=3x avg) = 22, capped at 25
        assert score == 22.0

    async def test_moderate_book_consumption(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(pct_book_consumed=12.0, size_vs_wallet_avg=2.5)
        score = scorer._score_size_significance(features)
        # 8 (>=10% book) + 6 (>=2x avg) = 14
        assert score == 14.0

    async def test_small_trade(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(pct_book_consumed=1.0, size_vs_wallet_avg=0.5)
        score = scorer._score_size_significance(features)
        assert score == 0.0

    async def test_none_values(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(pct_book_consumed=None, size_vs_wallet_avg=None)
        score = scorer._score_size_significance(features)
        assert score == 0.0

    async def test_5_pct_book(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(pct_book_consumed=5.0, size_vs_wallet_avg=None)
        score = scorer._score_size_significance(features)
        # 4 (>=5% book)
        assert score == 4.0

    async def test_2_pct_book(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(pct_book_consumed=2.0, size_vs_wallet_avg=None)
        score = scorer._score_size_significance(features)
        assert score == 2.0


class TestScoreIntent:
    """Tests for CopyScorer._score_intent (max 15 points)."""

    async def test_open_intent(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(trade_intent="open")
        assert scorer._score_intent(features) == 15

    async def test_add_intent(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(trade_intent="add")
        assert scorer._score_intent(features) == 10

    async def test_reduce_intent(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(trade_intent="reduce")
        assert scorer._score_intent(features) == 0

    async def test_close_intent_negative(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(trade_intent="close")
        assert scorer._score_intent(features) == -5

    async def test_unknown_intent(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(trade_intent="unknown")
        assert scorer._score_intent(features) == 5


class TestScoreMarketContext:
    """Tests for CopyScorer._score_market_context (max 10 points)."""

    async def test_ideal_market(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            hours_to_close=200.0,
            price_vs_50=0.05,
            market_volume_24h=150000.0,
        )
        score = scorer._score_market_context(features)
        assert score == 10.0

    async def test_market_closing_soon(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            hours_to_close=0.5,
            price_vs_50=0.05,
            market_volume_24h=150000.0,
        )
        score = scorer._score_market_context(features)
        # -3 + 3 + 2 = 2
        assert score == 2.0

    async def test_extreme_price(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            price_vs_50=0.45, hours_to_close=None, market_volume_24h=None,
        )
        score = scorer._score_market_context(features)
        assert score == -2.0

    async def test_low_volume(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            market_volume_24h=5000.0, hours_to_close=None, price_vs_50=None,
        )
        score = scorer._score_market_context(features)
        # A6: Low volume (<10k) now scores +3 (niche market opportunity)
        assert score == 3.0

    async def test_floor_at_negative_eight(self, db, make_trade_features):
        """A6: Floor changed from -5 to -8 with category efficiency scoring."""
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            hours_to_close=0.3,
            price_vs_50=0.45,
            market_volume_24h=5000.0,
        )
        score = scorer._score_market_context(features)
        # -3 (closing soon) + -2 (extreme price) + 3 (low volume niche) = -2
        assert score == -2.0


class TestScoreTiming:
    """Tests for CopyScorer._score_timing (max 5 points)."""

    async def test_normal_hours(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(is_after_hours=False)
        assert scorer._score_timing(features) == 3

    async def test_after_hours(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(is_after_hours=True)
        assert scorer._score_timing(features) == 1


class TestCostPenalty:
    """Tests for CopyScorer._compute_cost_penalty (-10 to 0 points)."""

    async def test_no_impact_no_spread(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(price_impact_bps=None, spread_at_entry=None)
        assert scorer._compute_cost_penalty(features) == 0.0

    async def test_high_impact_high_spread(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(price_impact_bps=60.0, spread_at_entry=120.0)
        penalty = scorer._compute_cost_penalty(features)
        assert penalty == -10.0

    async def test_moderate_costs(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(price_impact_bps=25.0, spread_at_entry=55.0)
        penalty = scorer._compute_cost_penalty(features)
        # -2 + -3 = -5
        assert penalty == -5.0

    async def test_clamped_at_negative_ten(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(price_impact_bps=100.0, spread_at_entry=200.0)
        penalty = scorer._compute_cost_penalty(features)
        assert penalty >= -10.0


class TestConfidence:
    """Tests for CopyScorer._determine_confidence."""

    async def test_high_confidence(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_total_trades=60)
        assert scorer._determine_confidence(features) == "HIGH"

    async def test_medium_confidence(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_total_trades=30)
        assert scorer._determine_confidence(features) == "MEDIUM"

    async def test_low_confidence(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_total_trades=10)
        assert scorer._determine_confidence(features) == "LOW"

    async def test_boundary_high(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_total_trades=50)
        assert scorer._determine_confidence(features) == "HIGH"

    async def test_boundary_medium(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_total_trades=20)
        assert scorer._determine_confidence(features) == "MEDIUM"

    async def test_boundary_low(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_total_trades=19)
        assert scorer._determine_confidence(features) == "LOW"


class TestRecommendation:
    """Tests for CopyScorer._determine_recommendation."""

    async def test_strong_copy_elite(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.ELITE)
        assert scorer._determine_recommendation(75.0, features) == "STRONG_COPY"

    async def test_strong_copy_good(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.GOOD)
        assert scorer._determine_recommendation(72.0, features) == "STRONG_COPY"

    async def test_high_score_but_neutral_tier_not_strong(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.NEUTRAL)
        assert scorer._determine_recommendation(80.0, features) == "COPY"

    async def test_copy_range(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.NEUTRAL)
        assert scorer._determine_recommendation(55.0, features) == "COPY"

    async def test_neutral_range(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.NEUTRAL)
        assert scorer._determine_recommendation(40.0, features) == "NEUTRAL"

    async def test_avoid_range(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.BAD)
        assert scorer._determine_recommendation(20.0, features) == "AVOID"

    async def test_boundary_70_with_elite(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.ELITE)
        assert scorer._determine_recommendation(70.0, features) == "STRONG_COPY"

    async def test_boundary_50(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.NEUTRAL)
        assert scorer._determine_recommendation(50.0, features) == "COPY"

    async def test_boundary_30(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(wallet_tier=WalletTier.BAD)
        assert scorer._determine_recommendation(30.0, features) == "NEUTRAL"


class TestScoreClamping:
    """Test overall score is clamped to 0-100."""

    async def test_score_cannot_exceed_100(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            wallet_tier=WalletTier.ELITE,
            wallet_win_rate_1h=0.80,
            wallet_sharpe_1h=3.0,
            pct_book_consumed=30.0,
            size_vs_wallet_avg=5.0,
            trade_intent="open",
            hours_to_close=200.0,
            price_vs_50=0.05,
            market_volume_24h=200000.0,
            is_after_hours=False,
            price_impact_bps=None,
            spread_at_entry=None,
        )
        scorer.feature_extractor = AsyncMock()
        scorer.feature_extractor.extract_features = AsyncMock(return_value=features)
        trade = MagicMock()
        result = await scorer.score_trade(trade)
        assert result.score <= 100.0

    async def test_score_cannot_go_below_0(self, db, make_trade_features):
        scorer = CopyScorer(db=db)
        features = make_trade_features(
            wallet_tier=WalletTier.BAD,
            wallet_win_rate_1h=0.30,
            wallet_sharpe_1h=0.2,
            pct_book_consumed=0.5,
            size_vs_wallet_avg=0.3,
            trade_intent="close",
            hours_to_close=0.3,
            price_vs_50=0.45,
            market_volume_24h=3000.0,
            is_after_hours=True,
            price_impact_bps=100.0,
            spread_at_entry=200.0,
        )
        scorer.feature_extractor = AsyncMock()
        scorer.feature_extractor.extract_features = AsyncMock(return_value=features)
        trade = MagicMock()
        result = await scorer.score_trade(trade)
        assert result.score >= 0.0


class TestShouldAlert:
    async def test_above_threshold(self, db):
        scorer = CopyScorer(db=db, min_score_for_alert=50.0)
        score = MagicMock()
        score.score = 55.0
        assert scorer.should_alert(score) is True

    async def test_below_threshold(self, db):
        scorer = CopyScorer(db=db, min_score_for_alert=50.0)
        score = MagicMock()
        score.score = 45.0
        assert scorer.should_alert(score) is False

    async def test_at_threshold(self, db):
        scorer = CopyScorer(db=db, min_score_for_alert=50.0)
        score = MagicMock()
        score.score = 50.0
        assert scorer.should_alert(score) is True
