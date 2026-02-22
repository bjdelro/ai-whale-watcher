"""
Tests for trade_evaluator.py — covers:
- Category exposure limits (A3)
- Category conviction multipliers (A6)
- Conflicting position detection
- Extreme price filtering
- Slippage gate
- Dedup logic
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass
from typing import Optional

from src.evaluation.trade_evaluator import (
    TradeEvaluator,
    MAX_CATEGORY_EXPOSURE_PCT,
    DEFAULT_CATEGORY_CAP,
    CATEGORY_CONVICTION,
)


# ---------------------------------------------------------------------------
# Lightweight position dataclass
# ---------------------------------------------------------------------------

@dataclass
class FakePosition:
    position_id: str = "pos_1"
    market_id: str = "cond_123"
    token_id: str = "tok_abc"
    outcome: str = "Yes"
    whale_address: str = "0xwhale"
    whale_name: str = "TestWhale"
    entry_price: float = 0.50
    shares: float = 4.0
    entry_time: str = ""
    copy_amount_usd: float = 2.0
    market_title: str = "Test Market"
    status: str = "open"
    category: Optional[str] = None
    live_cost_usd: Optional[float] = None
    live_shares: Optional[float] = None


@dataclass
class FakePaperTrade:
    timestamp: str = ""
    whale_address: str = ""
    whale_name: str = ""
    side: str = ""
    outcome: str = ""
    price: float = 0
    whale_size: float = 0
    our_size: float = 0
    our_shares: float = 0
    market_title: str = ""
    condition_id: str = ""
    asset_id: str = ""
    tx_hash: str = ""


@dataclass
class FakeCopiedPosition:
    position_id: str = ""
    market_id: str = ""
    token_id: str = ""
    outcome: str = ""
    whale_address: str = ""
    whale_name: str = ""
    entry_price: float = 0
    shares: float = 0
    entry_time: str = ""
    copy_amount_usd: float = 0
    market_title: str = ""
    status: str = "open"
    category: Optional[str] = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def evaluator():
    """Create a TradeEvaluator with mocked dependencies."""
    position_manager = MagicMock()
    position_manager.positions = {}
    position_manager.total_exposure = 0.0
    position_manager.copy_trade = AsyncMock()

    cluster_detector = MagicMock()
    cluster_detector.analyze_hedge.return_value = (False, None, 0, "copy")
    cluster_detector.record_wallet_market_trade = MagicMock()
    cluster_detector.record_trade_for_cluster = MagicMock()

    market_data = MagicMock()
    market_data.get_category = AsyncMock(return_value="politics")
    market_data.fetch_price = AsyncMock(return_value=None)

    whale_manager = MagicMock()
    whale_manager.is_pruned.return_value = False
    whale_manager.conviction_size.return_value = 1.0

    config = {
        "min_whale_trade_size": 50,
        "avoid_extreme_prices": True,
        "extreme_price_threshold": 0.15,
        "max_total_exposure": 100.0,
        "live_max_exposure": 50.0,
        "live_trading_enabled": False,
    }

    evaluator = TradeEvaluator(
        position_manager=position_manager,
        cluster_detector=cluster_detector,
        market_data=market_data,
        whale_manager=whale_manager,
        fetch_whale_trades=AsyncMock(return_value=[]),
        get_active_whales=lambda: {},
        get_polls_completed=lambda: 10,
        config=config,
    )
    return evaluator


def _make_whale():
    whale = MagicMock()
    whale.address = "0xwhale"
    whale.name = "TestWhale"
    return whale


def _make_trade(side="BUY", size=100, price=0.50, outcome="Yes",
                condition_id="cond_123", asset_id="tok_abc",
                tx_hash="tx_001", title="Test Market", age_seconds=30):
    """Create a fresh trade dict."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "side": side,
        "size": size,
        "price": price,
        "outcome": outcome,
        "conditionId": condition_id,
        "asset": asset_id,
        "transactionHash": tx_hash,
        "title": title,
        "timestamp": str(int(ts.timestamp())),
    }


# ---------------------------------------------------------------------------
# Category Exposure Limits (A3)
# ---------------------------------------------------------------------------

class TestCategoryExposureLimits:
    def test_max_category_exposure_pct_values(self):
        """Verify the configured caps."""
        assert MAX_CATEGORY_EXPOSURE_PCT["sports"] == 0.15
        assert MAX_CATEGORY_EXPOSURE_PCT["politics"] == 0.35
        assert MAX_CATEGORY_EXPOSURE_PCT["crypto"] == 0.30

    async def test_category_capped_returns_sentinel(self, evaluator):
        """When category is over cap, should return 'category_capped'."""
        # Set up: already 20% exposure in sports, total exposure $10
        # Use a DIFFERENT market_id to avoid dedup check hitting first
        sports_pos = FakePosition(
            position_id="p_sports", category="sports",
            copy_amount_usd=2.0, status="open",
            market_id="cond_OTHER", whale_address="0xother_whale",
        )
        evaluator._position_manager.positions = {"p_sports": sports_pos}
        evaluator._position_manager.total_exposure = 10.0

        evaluator._market_data.get_category = AsyncMock(return_value="sports")

        whale = _make_whale()
        trade = _make_trade()
        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result == "category_capped"

    async def test_category_not_capped_proceeds(self, evaluator):
        """When category has room, trade should proceed."""
        evaluator._position_manager.total_exposure = 10.0
        evaluator._market_data.get_category = AsyncMock(return_value="politics")

        # No positions in politics yet
        evaluator._position_manager.positions = {}

        whale = _make_whale()
        trade = _make_trade()
        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        # Should have called copy_trade (not capped)
        assert result is None  # None means successfully processed


# ---------------------------------------------------------------------------
# Category Conviction (A6)
# ---------------------------------------------------------------------------

class TestCategoryConviction:
    def test_conviction_values(self):
        assert CATEGORY_CONVICTION["sports"] == 0.6
        assert CATEGORY_CONVICTION["crypto"] == 1.4
        assert CATEGORY_CONVICTION["politics"] == 1.3
        assert CATEGORY_CONVICTION["unknown"] == 1.0


# ---------------------------------------------------------------------------
# Conflicting Position Detection
# ---------------------------------------------------------------------------

class TestConflictingPosition:
    def test_no_conflict_when_same_outcome(self, evaluator):
        pos = FakePosition(market_id="m1", outcome="Yes", status="open")
        evaluator._position_manager.positions = {"p1": pos}

        assert not evaluator.has_conflicting_position("m1", "Yes", "whale")

    def test_conflict_when_opposing_outcome(self, evaluator):
        pos = FakePosition(market_id="m1", outcome="Yes", status="open")
        evaluator._position_manager.positions = {"p1": pos}

        assert evaluator.has_conflicting_position("m1", "No", "whale")

    def test_no_conflict_when_closed(self, evaluator):
        pos = FakePosition(market_id="m1", outcome="Yes", status="closed")
        evaluator._position_manager.positions = {"p1": pos}

        assert not evaluator.has_conflicting_position("m1", "No", "whale")


# ---------------------------------------------------------------------------
# Sell Detection
# ---------------------------------------------------------------------------

class TestSellDetection:
    def test_detects_sell_matching_position(self, evaluator):
        pos = FakePosition(
            position_id="p1",
            whale_address="0xwhale",
            token_id="tok_abc",
            status="open",
        )
        evaluator._position_manager.positions = {"p1": pos}

        trade = {
            "side": "SELL",
            "proxyWallet": "0xwhale",
            "asset": "tok_abc",
            "price": 0.60,
            "size": 10,
        }

        signal = evaluator.check_for_whale_sells(trade)
        assert signal is not None
        assert signal["action"] == "SELL"
        assert signal["position_id"] == "p1"

    def test_ignores_buy_trade(self, evaluator):
        pos = FakePosition(status="open", whale_address="0xwhale", token_id="tok_abc")
        evaluator._position_manager.positions = {"p1": pos}

        trade = {"side": "BUY", "proxyWallet": "0xwhale", "asset": "tok_abc", "price": 0.5, "size": 10}
        assert evaluator.check_for_whale_sells(trade) is None

    def test_ignores_different_token(self, evaluator):
        pos = FakePosition(status="open", whale_address="0xwhale", token_id="tok_abc")
        evaluator._position_manager.positions = {"p1": pos}

        trade = {"side": "SELL", "proxyWallet": "0xwhale", "asset": "tok_OTHER", "price": 0.5, "size": 10}
        assert evaluator.check_for_whale_sells(trade) is None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestFilters:
    async def test_skip_small_trades(self, evaluator):
        """Trades below min_whale_trade_size should be skipped."""
        whale = _make_whale()
        trade = _make_trade(size=1, price=0.5)  # $0.50 < $50 min

        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result is None  # Skipped quietly
        evaluator._position_manager.copy_trade.assert_not_called()

    async def test_skip_extreme_price_high(self, evaluator):
        """Trades at extreme prices (>85%) should be skipped."""
        whale = _make_whale()
        trade = _make_trade(price=0.90, size=200)

        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result is None
        evaluator._position_manager.copy_trade.assert_not_called()

    async def test_skip_extreme_price_low(self, evaluator):
        """Trades at extreme prices (<15%) should be skipped."""
        whale = _make_whale()
        trade = _make_trade(price=0.10, size=200)

        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result is None
        evaluator._position_manager.copy_trade.assert_not_called()

    async def test_skip_pruned_whale(self, evaluator):
        """Pruned whales should be skipped."""
        evaluator._whale_manager.is_pruned.return_value = True

        whale = _make_whale()
        trade = _make_trade()

        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result is None
        evaluator._position_manager.copy_trade.assert_not_called()

    async def test_skip_stale_trade(self, evaluator):
        """Trades older than 120s should be skipped."""
        whale = _make_whale()
        trade = _make_trade(age_seconds=300)

        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result is None
        evaluator._position_manager.copy_trade.assert_not_called()

    async def test_skip_over_exposure(self, evaluator):
        """Should skip when total exposure is at max."""
        evaluator._position_manager.total_exposure = 200.0  # Over 100.0 limit

        whale = _make_whale()
        trade = _make_trade()

        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result is None
        evaluator._position_manager.copy_trade.assert_not_called()

    async def test_skip_dedup(self, evaluator):
        """Should skip if we already have an open position for this whale+market."""
        pos = FakePosition(
            whale_address="0xwhale", market_id="cond_123", status="open",
        )
        evaluator._position_manager.positions = {"p1": pos}

        whale = _make_whale()
        trade = _make_trade()

        result = await evaluator.evaluate_trade(whale, trade, FakePaperTrade, FakeCopiedPosition)

        assert result is None
        evaluator._position_manager.copy_trade.assert_not_called()
