"""Tests for LiveTrader - execution safety checks in dry_run mode."""

from unittest.mock import MagicMock
from src.execution.live_trader import LiveTrader, OrderSide


class TestOrderSizeCapping:
    async def test_order_reduced_to_max(self):
        trader = LiveTrader(
            private_key="0xfake",
            max_order_usd=5.0,
            dry_run=True,
        )
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader._submit_order(
            token_id="tok123",
            side=OrderSide.BUY,
            price=0.50,
            size_usd=10.0,
        )
        assert order is not None
        assert order.cost_usd <= 5.0

    async def test_order_within_max_unchanged(self):
        trader = LiveTrader(
            private_key="0xfake",
            max_order_usd=10.0,
            dry_run=True,
        )
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader._submit_order(
            token_id="tok123",
            side=OrderSide.BUY,
            price=0.50,
            size_usd=5.0,
        )
        assert order is not None


class TestPriceValidation:
    async def test_price_zero_rejected(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader._submit_order(
            token_id="tok123", side=OrderSide.BUY, price=0.0, size_usd=1.0,
        )
        assert order is None

    async def test_price_one_rejected(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader._submit_order(
            token_id="tok123", side=OrderSide.BUY, price=1.0, size_usd=1.0,
        )
        assert order is None

    async def test_negative_price_rejected(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader._submit_order(
            token_id="tok123", side=OrderSide.BUY, price=-0.5, size_usd=1.0,
        )
        assert order is None

    async def test_valid_price_accepted(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader._submit_order(
            token_id="tok123", side=OrderSide.BUY, price=0.65, size_usd=1.0,
        )
        assert order is not None


class TestMinimumShares:
    async def test_buy_minimum_5_shares(self):
        trader = LiveTrader(private_key="0xfake", max_order_usd=50.0, dry_run=True)
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader._submit_order(
            token_id="tok123", side=OrderSide.BUY, price=0.80, size_usd=1.0,
        )
        assert order is not None
        assert order.size >= 5.0


class TestDustSellRejection:
    async def test_sell_under_5_shares_rejected(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        trader._initialized = True

        order = await trader.submit_sell_order(
            token_id="tok123", price=0.50, shares=3.0,
        )
        assert order is None

    async def test_sell_5_shares_accepted(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader.submit_sell_order(
            token_id="tok123", price=0.50, shares=5.0,
        )
        assert order is not None


class TestDryRunMode:
    async def test_dry_run_returns_order_without_execution(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        trader._initialized = True
        trader._client = MagicMock()

        order = await trader.submit_buy_order(
            token_id="tok123", price=0.50, size_usd=2.50,
        )
        assert order is not None
        assert order.status == "dry_run"
        trader._client.create_order.assert_not_called()


class TestExposureLimit:
    async def test_buy_rejected_when_exposure_at_limit(self):
        trader = LiveTrader(
            private_key="0xfake",
            max_total_exposure=10.0,
            dry_run=True,
        )
        trader._initialized = True
        trader._client = MagicMock()
        trader._total_exposure = 10.0
        trader.get_collateral_balance = MagicMock(return_value=None)

        order = await trader._submit_order(
            token_id="tok123", side=OrderSide.BUY, price=0.50, size_usd=1.0,
        )
        assert order is None

    async def test_exposure_recalibration_from_balance(self):
        trader = LiveTrader(
            private_key="0xfake",
            max_total_exposure=10.0,
            dry_run=True,
        )
        trader._initialized = True
        trader._client = MagicMock()
        trader._total_exposure = 10.0
        trader.get_collateral_balance = MagicMock(return_value=5.0)

        order = await trader._submit_order(
            token_id="tok123", side=OrderSide.BUY, price=0.50, size_usd=2.0,
        )
        assert order is not None


class TestGetStats:
    def test_stats_structure(self):
        trader = LiveTrader(private_key="0xfake", dry_run=True)
        stats = trader.get_stats()
        assert stats["mode"] == "DRY RUN"
        assert "orders_submitted" in stats
        assert "orders_filled" in stats
        assert "orders_failed" in stats
