"""Tests for WalletProfiler - tier classification and Sharpe ratio computation."""

from src.scoring.wallet_profiler import WalletProfiler
from src.database.models import WalletTier


class TestClassifyTier:
    def _profiler(self, db):
        return WalletProfiler(db=db)

    async def test_elite(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=60, win_rate_1h=0.65, avg_markout_1h=80.0)
        assert tier == WalletTier.ELITE

    async def test_good(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=30, win_rate_1h=0.58, avg_markout_1h=30.0)
        assert tier == WalletTier.GOOD

    async def test_bad_by_win_rate(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=30, win_rate_1h=0.40, avg_markout_1h=30.0)
        assert tier == WalletTier.BAD

    async def test_bad_by_markout(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=30, win_rate_1h=0.55, avg_markout_1h=-25.0)
        assert tier == WalletTier.BAD

    async def test_neutral(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=30, win_rate_1h=0.52, avg_markout_1h=10.0)
        assert tier == WalletTier.NEUTRAL

    async def test_unknown_few_trades(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=5, win_rate_1h=0.80, avg_markout_1h=100.0)
        assert tier == WalletTier.UNKNOWN

    async def test_neutral_when_none_rates(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=15, win_rate_1h=None, avg_markout_1h=None)
        assert tier == WalletTier.NEUTRAL

    async def test_elite_boundary_exact(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=50, win_rate_1h=0.60, avg_markout_1h=50.0)
        assert tier == WalletTier.ELITE

    async def test_good_boundary_exact(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=20, win_rate_1h=0.55, avg_markout_1h=20.0)
        assert tier == WalletTier.GOOD

    async def test_bad_boundary_win_rate(self, db):
        """win_rate exactly 0.45 -> BAD."""
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=30, win_rate_1h=0.45, avg_markout_1h=10.0)
        assert tier == WalletTier.BAD

    async def test_bad_boundary_markout(self, db):
        """markout exactly -20 -> BAD."""
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=30, win_rate_1h=0.50, avg_markout_1h=-20.0)
        assert tier == WalletTier.BAD

    async def test_unknown_boundary_9_trades(self, db):
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=9, win_rate_1h=0.70, avg_markout_1h=60.0)
        assert tier == WalletTier.UNKNOWN

    async def test_unknown_boundary_10_trades(self, db):
        """10 trades is enough to classify (not UNKNOWN)."""
        p = self._profiler(db)
        tier = p._classify_tier(total_trades=10, win_rate_1h=0.52, avg_markout_1h=10.0)
        assert tier == WalletTier.NEUTRAL


class TestComputeSharpe:
    async def test_positive_returns(self, db):
        p = WalletProfiler(db=db)
        returns = [10.0, 20.0, 30.0, 40.0, 50.0]
        sharpe = p._compute_sharpe(returns)
        assert sharpe is not None
        assert sharpe > 0

    async def test_negative_returns(self, db):
        p = WalletProfiler(db=db)
        returns = [-10.0, -20.0, -30.0, -40.0, -50.0]
        sharpe = p._compute_sharpe(returns)
        assert sharpe is not None
        assert sharpe < 0

    async def test_fewer_than_5_returns_none(self, db):
        p = WalletProfiler(db=db)
        assert p._compute_sharpe([10.0, 20.0, 30.0, 40.0]) is None

    async def test_zero_std_returns_none(self, db):
        p = WalletProfiler(db=db)
        returns = [10.0, 10.0, 10.0, 10.0, 10.0]
        assert p._compute_sharpe(returns) is None

    async def test_empty_returns(self, db):
        p = WalletProfiler(db=db)
        assert p._compute_sharpe([]) is None

    async def test_mixed_returns(self, db):
        p = WalletProfiler(db=db)
        returns = [100.0, -50.0, 75.0, -25.0, 60.0]
        sharpe = p._compute_sharpe(returns)
        assert sharpe is not None
        assert sharpe > 0

    async def test_single_return(self, db):
        p = WalletProfiler(db=db)
        assert p._compute_sharpe([50.0]) is None
