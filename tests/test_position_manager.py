"""
Tests for position_manager.py — covers:
- B3: Trailing stop (HWM tracking, activation, exit)
- Exit condition checks (SL, TP, stale)
- Position creation and P&L recording
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from typing import Optional

from src.positions.position_manager import PositionManager


# ---------------------------------------------------------------------------
# Lightweight CopiedPosition for tests (avoids importing orchestrator)
# ---------------------------------------------------------------------------

@dataclass
class FakeCopiedPosition:
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
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    category: Optional[str] = None
    high_water_mark: Optional[float] = None
    live_shares: Optional[float] = None
    live_cost_usd: Optional[float] = None
    live_order_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pm():
    """Create a PositionManager with all mocked dependencies."""
    market_data = MagicMock()
    whale_manager = MagicMock()
    whale_manager.record_copy_pnl = MagicMock()
    whale_manager.record_category_pnl = MagicMock()
    whale_manager.active_whale_count = 8
    reporter = AsyncMock()
    session = MagicMock()

    config = {
        "live_trading_enabled": False,
        "stop_loss_pct": 0.15,
        "take_profit_pct": 0.20,
        "stale_position_hours": 48,
    }

    mgr = PositionManager(
        state_file="/tmp/test_state.json",
        market_data=market_data,
        live_trader=None,
        redeemer=None,
        whale_manager=whale_manager,
        reporter=reporter,
        session=session,
        config=config,
    )
    return mgr


def _make_position(entry_price=0.50, shares=4.0, cost=2.0, hwm=None, hours_ago=0, category=None):
    """Helper to create a FakeCopiedPosition."""
    entry_time = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return FakeCopiedPosition(
        entry_price=entry_price,
        shares=shares,
        copy_amount_usd=cost,
        high_water_mark=hwm,
        entry_time=entry_time,
        category=category,
    )


# ---------------------------------------------------------------------------
# B3: Trailing Stop
# ---------------------------------------------------------------------------

class TestTrailingStop:
    async def test_hwm_updated_on_price_increase(self, pm):
        """High-water mark should be updated when price increases."""
        pos = _make_position(entry_price=0.50, hwm=0.55)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.60  # New high

        await pm._check_exit_conditions([pos])

        assert pos.high_water_mark == 0.60

    async def test_trailing_stop_not_triggered_before_activation(self, pm):
        """Trailing stop should not trigger if gain hasn't reached 10%."""
        pos = _make_position(entry_price=0.50, hwm=0.54)  # 8% gain peak
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.51  # Dropped from HWM but <10% ever

        await pm._check_exit_conditions([pos])

        assert pos.status == "open"

    async def test_trailing_stop_triggered_after_activation(self, pm):
        """Trailing stop should trigger: 10%+ gain then 5%+ drop from HWM."""
        # Entry at 0.50, peaked at 0.60 (20% gain), now at 0.56 (6.7% drop from HWM)
        pos = _make_position(entry_price=0.50, hwm=0.60)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.56

        await pm._check_exit_conditions([pos])

        assert pos.status == "closed"
        assert pos.exit_reason == "trailing_stop"

    async def test_trailing_stop_not_triggered_small_drop(self, pm):
        """Should NOT trigger if drop from HWM is less than 5%."""
        # Entry at 0.50, peaked at 0.60, now at 0.58 (3.3% drop — not enough)
        pos = _make_position(entry_price=0.50, hwm=0.60)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.58

        await pm._check_exit_conditions([pos])

        assert pos.status == "open"

    async def test_hwm_initialized_from_entry_price(self, pm):
        """If no HWM set, should initialize from entry_price."""
        pos = _make_position(entry_price=0.50, hwm=None)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.55

        await pm._check_exit_conditions([pos])

        assert pos.high_water_mark == 0.55


# ---------------------------------------------------------------------------
# Stop Loss
# ---------------------------------------------------------------------------

class TestStopLoss:
    async def test_stop_loss_triggers(self, pm):
        """Position should close when down 15% from entry."""
        pos = _make_position(entry_price=0.50)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.42  # -16% from entry

        await pm._check_exit_conditions([pos])

        assert pos.status == "closed"
        assert pos.exit_reason == "stop_loss"

    async def test_stop_loss_not_triggered(self, pm):
        """Position should stay open when down less than 15%."""
        pos = _make_position(entry_price=0.50)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.45  # -10%

        await pm._check_exit_conditions([pos])

        assert pos.status == "open"


# ---------------------------------------------------------------------------
# Take Profit
# ---------------------------------------------------------------------------

class TestTakeProfit:
    async def test_take_profit_triggers(self, pm):
        """Position should close when up 20% from entry."""
        pos = _make_position(entry_price=0.50)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.61  # +22% from entry

        await pm._check_exit_conditions([pos])

        assert pos.status == "closed"
        assert pos.exit_reason == "take_profit"


# ---------------------------------------------------------------------------
# Stale Position
# ---------------------------------------------------------------------------

class TestStalePosition:
    async def test_stale_position_closes(self, pm):
        """Position held >48h with <2% movement should close as stale."""
        pos = _make_position(entry_price=0.50, hours_ago=50)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.505  # +1% — within 2% threshold

        await pm._check_exit_conditions([pos])

        assert pos.status == "closed"
        assert pos.exit_reason == "stale_position"

    async def test_stale_position_not_closed_if_moved(self, pm):
        """Position held >48h but moved >2% should NOT close as stale."""
        pos = _make_position(entry_price=0.50, hours_ago=50)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.55  # +10% — too much movement

        await pm._check_exit_conditions([pos])

        # Should be closed by trailing stop or take_profit, not stale
        # But entry_price=0.50, hwm=None -> hwm=0.55, gain=10% exactly at activation threshold
        # drop from hwm = 0 -> no trailing stop
        # pnl_pct = 10% -> not take_profit (need 20%)
        assert pos.status == "open"


# ---------------------------------------------------------------------------
# P&L Recording
# ---------------------------------------------------------------------------

class TestPnlRecording:
    async def test_exit_records_pnl(self, pm):
        """Closing a position should record P&L and update counters."""
        pos = _make_position(entry_price=0.50)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.61  # Take profit

        await pm._check_exit_conditions([pos])

        assert pos.pnl is not None
        assert pm._realized_pnl != 0
        assert pm._positions_closed == 1

    async def test_exit_records_category_pnl(self, pm):
        """Closing should call record_category_pnl on whale_manager."""
        pos = _make_position(entry_price=0.50, category="crypto")
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.61

        await pm._check_exit_conditions([pos])

        pm._whale_manager.record_category_pnl.assert_called_once()
        call_args = pm._whale_manager.record_category_pnl.call_args
        assert call_args[0][0] == "crypto"


# ---------------------------------------------------------------------------
# Exit priority: trailing_stop vs stale
# ---------------------------------------------------------------------------

class TestExitPriority:
    async def test_trailing_stop_takes_priority_over_stale(self, pm):
        """Trailing stop should fire before stale check is considered."""
        # 50h old position, peaked at 0.60 (20% gain), now at 0.56 (6.7% drop)
        pos = _make_position(entry_price=0.50, hwm=0.60, hours_ago=50)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.56

        await pm._check_exit_conditions([pos])

        assert pos.exit_reason == "trailing_stop"

    async def test_stop_loss_takes_priority_over_trailing(self, pm):
        """Stop loss should fire even if trailing stop conditions also met."""
        # Entry at 0.50, peaked at 0.60, now at 0.40 (-20% from entry, also -33% from HWM)
        pos = _make_position(entry_price=0.50, hwm=0.60)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.40

        await pm._check_exit_conditions([pos])

        assert pos.exit_reason == "stop_loss"  # SL checked first


# ---------------------------------------------------------------------------
# Portfolio Stats
# ---------------------------------------------------------------------------

class TestPortfolioStats:
    def test_unrealized_pnl(self, pm):
        pos = _make_position(entry_price=0.50, shares=4.0, cost=2.0)
        pm.positions["p1"] = pos
        pm._current_prices["tok_abc"] = 0.60

        pnl = pm.calculate_unrealized_pnl()
        # 0.60 * 4.0 - 2.0 = 0.40
        assert pnl == pytest.approx(0.40)

    def test_portfolio_stats(self, pm):
        pos = _make_position(entry_price=0.50, shares=4.0, cost=2.0)
        pm.positions["p1"] = pos
        pm._total_exposure = 2.0
        pm._current_prices["tok_abc"] = 0.60

        stats = pm.get_portfolio_stats()

        assert stats["open_positions"] == 1
        assert stats["open_exposure"] == pytest.approx(2.0)
        assert stats["unrealized_pnl"] == pytest.approx(0.40)
