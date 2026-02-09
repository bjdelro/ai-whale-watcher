"""Tests for FeatureExtractor - pure computation helper methods."""

from datetime import datetime
from unittest.mock import MagicMock
from src.scoring.features import FeatureExtractor


class TestIsAfterHours:
    def _extractor(self, db):
        return FeatureExtractor(db)

    async def test_weekday_normal_hours(self, db):
        fe = self._extractor(db)
        ts = datetime(2025, 1, 15, 14, 0, 0)  # Wednesday 2pm UTC
        assert fe._is_after_hours(ts) is False

    async def test_saturday(self, db):
        fe = self._extractor(db)
        ts = datetime(2025, 1, 18, 14, 0, 0)  # Saturday
        assert fe._is_after_hours(ts) is True

    async def test_sunday(self, db):
        fe = self._extractor(db)
        ts = datetime(2025, 1, 19, 10, 0, 0)  # Sunday
        assert fe._is_after_hours(ts) is True

    async def test_weekday_early_morning_utc(self, db):
        fe = self._extractor(db)
        ts = datetime(2025, 1, 13, 5, 0, 0)  # Monday 5am UTC
        assert fe._is_after_hours(ts) is True

    async def test_weekday_boundary_4am_utc(self, db):
        fe = self._extractor(db)
        ts = datetime(2025, 1, 13, 4, 0, 0)  # 4am UTC is within [4, 11)
        assert fe._is_after_hours(ts) is True

    async def test_weekday_boundary_11am_utc(self, db):
        fe = self._extractor(db)
        ts = datetime(2025, 1, 13, 11, 0, 0)  # 11am UTC is NOT after hours
        assert fe._is_after_hours(ts) is False

    async def test_weekday_boundary_3am_utc(self, db):
        fe = self._extractor(db)
        ts = datetime(2025, 1, 13, 3, 0, 0)  # 3am UTC is before cutoff
        assert fe._is_after_hours(ts) is False


class TestComputePriceVs50:
    async def test_at_50(self, db):
        fe = FeatureExtractor(db)
        assert fe._compute_price_vs_50(0.5) == 0.0

    async def test_at_10(self, db):
        fe = FeatureExtractor(db)
        result = fe._compute_price_vs_50(0.1)
        assert abs(result - 0.4) < 0.001

    async def test_at_90(self, db):
        fe = FeatureExtractor(db)
        result = fe._compute_price_vs_50(0.9)
        assert abs(result - 0.4) < 0.001

    async def test_none_price(self, db):
        fe = FeatureExtractor(db)
        assert fe._compute_price_vs_50(None) is None

    async def test_at_0(self, db):
        fe = FeatureExtractor(db)
        assert abs(fe._compute_price_vs_50(0.0) - 0.5) < 0.001

    async def test_at_1(self, db):
        fe = FeatureExtractor(db)
        assert abs(fe._compute_price_vs_50(1.0) - 0.5) < 0.001


class TestComputePctBookConsumed:
    async def test_buy_uses_ask_depth(self, db, make_orderbook_snapshot):
        fe = FeatureExtractor(db)
        trade = MagicMock(side="BUY", total_usd=500.0, pct_book_consumed=None)
        orderbook = make_orderbook_snapshot(ask_depth_5pct=5000.0)
        result = fe._compute_pct_book_consumed(trade, orderbook)
        assert abs(result - 10.0) < 0.01

    async def test_sell_uses_bid_depth(self, db, make_orderbook_snapshot):
        fe = FeatureExtractor(db)
        trade = MagicMock(side="SELL", total_usd=250.0, pct_book_consumed=None)
        orderbook = make_orderbook_snapshot(bid_depth_5pct=5000.0)
        result = fe._compute_pct_book_consumed(trade, orderbook)
        assert abs(result - 5.0) < 0.01

    async def test_zero_depth_returns_none(self, db, make_orderbook_snapshot):
        fe = FeatureExtractor(db)
        trade = MagicMock(side="BUY", total_usd=500.0, pct_book_consumed=None)
        orderbook = make_orderbook_snapshot(ask_depth_5pct=0.0)
        result = fe._compute_pct_book_consumed(trade, orderbook)
        assert result is None

    async def test_no_orderbook_fallback(self, db):
        fe = FeatureExtractor(db)
        trade = MagicMock(side="BUY", total_usd=500.0, pct_book_consumed=12.5)
        result = fe._compute_pct_book_consumed(trade, None)
        assert result == 12.5

    async def test_negative_depth_returns_none(self, db, make_orderbook_snapshot):
        fe = FeatureExtractor(db)
        trade = MagicMock(side="BUY", total_usd=500.0, pct_book_consumed=None)
        orderbook = make_orderbook_snapshot(ask_depth_5pct=-100.0)
        result = fe._compute_pct_book_consumed(trade, orderbook)
        assert result is None


class TestComputeSizeVsAvg:
    async def test_normal_ratio(self, db, make_wallet_stats):
        fe = FeatureExtractor(db)
        trade = MagicMock(total_usd=1666.0)
        stats = make_wallet_stats(avg_trade_size=833.0)
        result = fe._compute_size_vs_avg(trade, stats)
        assert abs(result - 2.0) < 0.01

    async def test_no_stats(self, db):
        fe = FeatureExtractor(db)
        trade = MagicMock(total_usd=500.0)
        assert fe._compute_size_vs_avg(trade, None) is None

    async def test_zero_avg_trade_size(self, db, make_wallet_stats):
        fe = FeatureExtractor(db)
        trade = MagicMock(total_usd=500.0)
        stats = make_wallet_stats(avg_trade_size=0.0)
        assert fe._compute_size_vs_avg(trade, stats) is None

    async def test_none_avg_trade_size(self, db, make_wallet_stats):
        fe = FeatureExtractor(db)
        trade = MagicMock(total_usd=500.0)
        stats = make_wallet_stats(avg_trade_size=None)
        assert fe._compute_size_vs_avg(trade, stats) is None


class TestComputePositionChangePct:
    async def test_new_position(self, db):
        fe = FeatureExtractor(db)
        trade = MagicMock(position_before=0, position_after=100.0)
        assert fe._compute_position_change_pct(trade) == 100.0

    async def test_double_position(self, db):
        fe = FeatureExtractor(db)
        trade = MagicMock(position_before=100.0, position_after=200.0)
        result = fe._compute_position_change_pct(trade)
        assert abs(result - 100.0) < 0.01

    async def test_partial_close(self, db):
        fe = FeatureExtractor(db)
        trade = MagicMock(position_before=100.0, position_after=50.0)
        result = fe._compute_position_change_pct(trade)
        assert abs(result - 50.0) < 0.01

    async def test_none_before(self, db):
        fe = FeatureExtractor(db)
        trade = MagicMock(position_before=None, position_after=100.0)
        assert fe._compute_position_change_pct(trade) is None

    async def test_none_after(self, db):
        fe = FeatureExtractor(db)
        trade = MagicMock(position_before=100.0, position_after=None)
        assert fe._compute_position_change_pct(trade) is None
