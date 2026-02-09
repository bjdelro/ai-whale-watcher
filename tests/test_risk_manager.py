"""Tests for RiskManager - all 12+ risk checks, position tracking, P&L, halt/resume."""

from datetime import datetime, timedelta
from src.execution.risk_manager import RiskManager, RiskLimits, TradeRecord, Position


class TestCanTrade:
    """Tests for RiskManager.can_trade() -- each check in isolation."""

    def _default_params(self):
        return {
            "copy_score": 80.0,
            "wallet_tier": "elite",
            "wallet_trades": 60,
            "market_volume_24h": 100000.0,
            "hours_to_close": 48.0,
            "market_id": "market_001",
            "trade_size_usd": 0.50,
            "copied_wallet": "0xwhale",
            "side": "BUY",
        }

    def test_all_checks_pass(self):
        rm = RiskManager()
        allowed, reason = rm.can_trade(**self._default_params())
        assert allowed is True
        assert reason == "OK"

    def test_halted_rejects(self):
        rm = RiskManager()
        rm._halted = True
        rm._halt_reason = "test halt"
        allowed, reason = rm.can_trade(**self._default_params())
        assert allowed is False
        assert "halted" in reason.lower()

    def test_score_below_threshold(self):
        rm = RiskManager()
        params = self._default_params()
        params["copy_score"] = 50.0
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "Score" in reason

    def test_wallet_tier_not_allowed(self):
        rm = RiskManager()
        params = self._default_params()
        params["wallet_tier"] = "neutral"
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "tier" in reason.lower()

    def test_wallet_trades_too_few(self):
        rm = RiskManager()
        params = self._default_params()
        params["wallet_trades"] = 5
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "trades" in reason.lower()

    def test_market_volume_too_low(self):
        rm = RiskManager()
        params = self._default_params()
        params["market_volume_24h"] = 500.0
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "volume" in reason.lower()

    def test_hours_to_close_too_few(self):
        rm = RiskManager()
        params = self._default_params()
        params["hours_to_close"] = 2.0
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "closes" in reason.lower()

    def test_hours_to_close_none_passes(self):
        rm = RiskManager()
        params = self._default_params()
        params["hours_to_close"] = None
        allowed, reason = rm.can_trade(**params)
        assert allowed is True

    def test_trade_size_exceeds_max(self):
        rm = RiskManager()
        params = self._default_params()
        params["trade_size_usd"] = 5.0
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "exceeds" in reason.lower()

    def test_total_exposure_exceeded(self):
        rm = RiskManager()
        rm.positions = {
            f"mkt_{i}": Position(
                market_id=f"mkt_{i}", market_name=f"Market {i}", outcome="YES",
                side="BUY", entry_price=0.5, size_usd=10.0, shares=20.0,
                entry_time=datetime.utcnow(), copy_score=80.0, copied_wallet="0x",
            )
            for i in range(10)
        }
        params = self._default_params()
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "exposure" in reason.lower()

    def test_max_positions_reached(self):
        rm = RiskManager(limits=RiskLimits(max_positions=2))
        rm.positions = {
            f"mkt_{i}": Position(
                market_id=f"mkt_{i}", market_name="M", outcome="YES",
                side="BUY", entry_price=0.5, size_usd=1.0, shares=2.0,
                entry_time=datetime.utcnow(), copy_score=80.0, copied_wallet="0x",
            )
            for i in range(2)
        }
        params = self._default_params()
        params["market_id"] = "new_market"
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "positions" in reason.lower()

    def test_per_market_limit(self):
        rm = RiskManager()
        rm.positions["market_001"] = Position(
            market_id="market_001", market_name="M", outcome="YES",
            side="BUY", entry_price=0.5, size_usd=4.90, shares=10.0,
            entry_time=datetime.utcnow(), copy_score=80.0, copied_wallet="0x",
        )
        params = self._default_params()
        params["trade_size_usd"] = 0.50
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "market" in reason.lower()

    def test_daily_loss_limit(self):
        rm = RiskManager()
        rm.daily_pnl = -50.0
        rm.daily_pnl_reset_date = datetime.utcnow().date()
        params = self._default_params()
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "loss" in reason.lower()


class TestDedupWindow:
    def test_within_dedup_window(self):
        rm = RiskManager()
        rm._recent_trades["0xwhale:market_001:BUY"] = datetime.utcnow()
        params = {
            "copy_score": 80.0, "wallet_tier": "elite", "wallet_trades": 60,
            "market_volume_24h": 100000.0, "hours_to_close": 48.0,
            "market_id": "market_001", "trade_size_usd": 0.50,
            "copied_wallet": "0xwhale", "side": "BUY",
        }
        allowed, reason = rm.can_trade(**params)
        assert allowed is False
        assert "Duplicate" in reason

    def test_after_dedup_window(self):
        rm = RiskManager()
        old_time = datetime.utcnow() - timedelta(seconds=120)
        rm._recent_trades["0xwhale:market_001:BUY"] = old_time
        params = {
            "copy_score": 80.0, "wallet_tier": "elite", "wallet_trades": 60,
            "market_volume_24h": 100000.0, "hours_to_close": 48.0,
            "market_id": "market_001", "trade_size_usd": 0.50,
            "copied_wallet": "0xwhale", "side": "BUY",
        }
        allowed, reason = rm.can_trade(**params)
        assert allowed is True

    def test_different_side_no_dedup(self):
        rm = RiskManager()
        rm._recent_trades["0xwhale:market_001:BUY"] = datetime.utcnow()
        params = {
            "copy_score": 80.0, "wallet_tier": "elite", "wallet_trades": 60,
            "market_volume_24h": 100000.0, "hours_to_close": 48.0,
            "market_id": "market_001", "trade_size_usd": 0.50,
            "copied_wallet": "0xwhale", "side": "SELL",
        }
        allowed, reason = rm.can_trade(**params)
        assert allowed is True


class TestPositionTracking:
    def _make_trade_record(self, **overrides):
        defaults = {
            "timestamp": datetime.utcnow(),
            "market_id": "mkt_1",
            "market_name": "Test Market",
            "outcome": "YES",
            "side": "BUY",
            "price": 0.60,
            "size_usd": 0.60,
            "shares": 1.0,
            "copy_score": 80.0,
            "copied_wallet": "0xwhale",
        }
        defaults.update(overrides)
        return TradeRecord(**defaults)

    def test_record_creates_position(self):
        rm = RiskManager()
        trade = self._make_trade_record()
        rm.record_trade(trade)
        assert "mkt_1" in rm.positions
        assert rm.positions["mkt_1"].shares == 1.0

    def test_record_averages_into_existing(self):
        rm = RiskManager()
        trade1 = self._make_trade_record(price=0.60, shares=1.0, size_usd=0.60)
        trade2 = self._make_trade_record(price=0.70, shares=1.0, size_usd=0.70)
        rm.record_trade(trade1)
        rm.record_trade(trade2)
        pos = rm.positions["mkt_1"]
        assert pos.shares == 2.0
        assert abs(pos.entry_price - 0.65) < 0.001

    def test_close_position_buy_profit(self):
        rm = RiskManager()
        trade = self._make_trade_record(price=0.60, shares=10.0, size_usd=6.00)
        rm.record_trade(trade)
        rm.close_position("mkt_1", exit_price=0.80)
        assert abs(rm.realized_pnl - 2.0) < 0.001
        assert "mkt_1" not in rm.positions

    def test_close_position_buy_loss(self):
        rm = RiskManager()
        trade = self._make_trade_record(price=0.60, shares=10.0, size_usd=6.00)
        rm.record_trade(trade)
        rm.close_position("mkt_1", exit_price=0.40)
        assert abs(rm.realized_pnl - (-2.0)) < 0.001

    def test_close_position_sell_profit(self):
        rm = RiskManager()
        trade = self._make_trade_record(side="SELL", price=0.70, shares=10.0, size_usd=7.00)
        rm.record_trade(trade)
        rm.close_position("mkt_1", exit_price=0.50)
        assert abs(rm.realized_pnl - 2.0) < 0.001

    def test_close_nonexistent_position(self):
        rm = RiskManager()
        rm.close_position("nonexistent", exit_price=0.50)
        assert rm.realized_pnl == 0.0


class TestHaltResume:
    def test_halt_sets_state(self):
        rm = RiskManager()
        rm._halt("test reason")
        assert rm._halted is True
        assert rm._halt_reason == "test reason"

    def test_resume_clears_state(self):
        rm = RiskManager()
        rm._halt("test")
        rm.resume()
        assert rm._halted is False
        assert rm._halt_reason is None


class TestDailyPnlReset:
    def test_reset_on_new_day(self):
        rm = RiskManager()
        rm.daily_pnl = -20.0
        rm.daily_pnl_reset_date = (datetime.utcnow() - timedelta(days=1)).date()
        rm._maybe_reset_daily_pnl()
        assert rm.daily_pnl == 0.0
        assert rm.daily_pnl_reset_date == datetime.utcnow().date()

    def test_no_reset_same_day(self):
        rm = RiskManager()
        rm.daily_pnl = -20.0
        rm.daily_pnl_reset_date = datetime.utcnow().date()
        rm._maybe_reset_daily_pnl()
        assert rm.daily_pnl == -20.0


class TestUpdatePositionPrice:
    def test_updates_unrealized_pnl_buy(self):
        rm = RiskManager()
        rm.positions["mkt_1"] = Position(
            market_id="mkt_1", market_name="M", outcome="YES",
            side="BUY", entry_price=0.50, size_usd=5.0, shares=10.0,
            entry_time=datetime.utcnow(), copy_score=80.0, copied_wallet="0x",
        )
        rm.update_position_price("mkt_1", 0.60)
        assert abs(rm.positions["mkt_1"].unrealized_pnl - 1.0) < 0.001

    def test_updates_unrealized_pnl_sell(self):
        rm = RiskManager()
        rm.positions["mkt_1"] = Position(
            market_id="mkt_1", market_name="M", outcome="YES",
            side="SELL", entry_price=0.60, size_usd=6.0, shares=10.0,
            entry_time=datetime.utcnow(), copy_score=80.0, copied_wallet="0x",
        )
        rm.update_position_price("mkt_1", 0.50)
        assert abs(rm.positions["mkt_1"].unrealized_pnl - 1.0) < 0.001

    def test_total_unrealized_pnl(self):
        rm = RiskManager()
        rm.positions["mkt_1"] = Position(
            market_id="mkt_1", market_name="M", outcome="YES",
            side="BUY", entry_price=0.50, size_usd=5.0, shares=10.0,
            entry_time=datetime.utcnow(), copy_score=80.0, copied_wallet="0x",
            unrealized_pnl=1.0,
        )
        rm.positions["mkt_2"] = Position(
            market_id="mkt_2", market_name="M2", outcome="YES",
            side="BUY", entry_price=0.50, size_usd=5.0, shares=10.0,
            entry_time=datetime.utcnow(), copy_score=80.0, copied_wallet="0x",
            unrealized_pnl=-0.5,
        )
        assert abs(rm.get_total_unrealized_pnl() - 0.5) < 0.001


class TestGetStats:
    def test_stats_structure(self):
        rm = RiskManager()
        stats = rm.get_stats()
        assert "positions" in stats
        assert "total_exposure" in stats
        assert "realized_pnl" in stats
        assert "unrealized_pnl" in stats
        assert "daily_pnl" in stats
        assert "halted" in stats
