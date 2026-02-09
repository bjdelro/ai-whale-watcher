"""Tests for database model creation, relationships, and session management."""

from datetime import datetime
from sqlalchemy import select, func
from src.database.models import (
    RawFill, CanonicalTrade, WalletStats, WalletPosition,
    Markout, OrderbookSnapshot, MarketRegistry,
    WalletTier, TradeIntent,
)


class TestModelCreation:
    async def test_create_raw_fill(self, db, make_raw_fill):
        fill = make_raw_fill()
        async with db.session() as session:
            session.add(fill)

        async with db.session() as session:
            result = await session.execute(select(RawFill))
            stored = result.scalar_one()
            assert stored.id == fill.id
            assert stored.price == 0.65
            assert stored.processed is False

    async def test_create_canonical_trade(self, db, make_canonical_trade):
        trade = make_canonical_trade()
        async with db.session() as session:
            session.add(trade)

        async with db.session() as session:
            result = await session.execute(select(CanonicalTrade))
            stored = result.scalar_one()
            assert stored.side == "BUY"
            assert stored.intent == TradeIntent.OPEN
            assert stored.total_usd == 65.0

    async def test_create_wallet_stats(self, db, make_wallet_stats):
        stats = make_wallet_stats(tier=WalletTier.ELITE)
        async with db.session() as session:
            session.add(stats)

        async with db.session() as session:
            result = await session.execute(select(WalletStats))
            stored = result.scalar_one()
            assert stored.tier == WalletTier.ELITE
            assert stored.total_trades == 60

    async def test_create_markout_with_relationship(self, db, make_canonical_trade):
        trade = make_canonical_trade()
        async with db.session() as session:
            session.add(trade)
            await session.flush()

            markout = Markout(
                canonical_trade_id=trade.id,
                horizon="1h",
                entry_price=0.65,
                is_final=False,
                scheduled_for=datetime.utcnow(),
            )
            session.add(markout)

        async with db.session() as session:
            result = await session.execute(select(Markout))
            stored = result.scalar_one()
            assert stored.horizon == "1h"
            assert stored.entry_price == 0.65

    async def test_create_orderbook_snapshot(self, db, make_orderbook_snapshot):
        snap = make_orderbook_snapshot()
        async with db.session() as session:
            session.add(snap)

        async with db.session() as session:
            result = await session.execute(select(OrderbookSnapshot))
            stored = result.scalar_one()
            assert abs(stored.mid_price - 0.65) < 0.001

    async def test_create_market_registry(self, db, make_market_registry):
        market = make_market_registry()
        async with db.session() as session:
            session.add(market)

        async with db.session() as session:
            result = await session.execute(select(MarketRegistry))
            stored = result.scalar_one()
            assert stored.question == "Will X happen?"
            assert stored.is_active is True

    async def test_create_wallet_position(self, db):
        async with db.session() as session:
            pos = WalletPosition(
                wallet="0xabc", condition_id="cond1", outcome="YES",
                size=100.0, avg_entry_price=0.60,
            )
            session.add(pos)

        async with db.session() as session:
            result = await session.execute(select(WalletPosition))
            stored = result.scalar_one()
            assert stored.size == 100.0
            assert stored.outcome == "YES"


class TestSessionManagement:
    async def test_session_auto_commits(self, db, make_raw_fill):
        fill = make_raw_fill()
        async with db.session() as session:
            session.add(fill)

        async with db.session() as session:
            result = await session.execute(select(func.count()).select_from(RawFill))
            assert result.scalar() == 1

    async def test_session_rollback_on_error(self, db, make_raw_fill):
        fill = make_raw_fill()
        try:
            async with db.session() as session:
                session.add(fill)
                raise ValueError("test error")
        except ValueError:
            pass

        async with db.session() as session:
            result = await session.execute(select(func.count()).select_from(RawFill))
            assert result.scalar() == 0


class TestEnums:
    def test_wallet_tier_values(self):
        assert WalletTier.ELITE.value == "elite"
        assert WalletTier.GOOD.value == "good"
        assert WalletTier.NEUTRAL.value == "neutral"
        assert WalletTier.BAD.value == "bad"
        assert WalletTier.UNKNOWN.value == "unknown"

    def test_trade_intent_values(self):
        assert TradeIntent.OPEN.value == "open"
        assert TradeIntent.CLOSE.value == "close"
        assert TradeIntent.ADD.value == "add"
        assert TradeIntent.REDUCE.value == "reduce"
        assert TradeIntent.UNKNOWN.value == "unknown"


class TestMarketRegistryProperties:
    async def test_hours_to_close_with_future_date(self, db, make_market_registry):
        from datetime import timedelta
        market = make_market_registry(end_date=datetime.utcnow() + timedelta(hours=48))
        assert market.hours_to_close is not None
        assert market.hours_to_close > 47.0
        assert market.hours_to_close <= 48.0

    async def test_hours_to_close_with_none(self, db, make_market_registry):
        market = make_market_registry(end_date=None)
        assert market.hours_to_close is None

    async def test_hours_to_close_past_date(self, db, make_market_registry):
        from datetime import timedelta
        market = make_market_registry(end_date=datetime.utcnow() - timedelta(hours=1))
        assert market.hours_to_close == 0
