"""Tests for FillAggregator - VWAP, intent determination, position updates."""

from datetime import datetime
from sqlalchemy import select, func
from src.ingestion.fill_aggregator import FillAggregator
from src.database.models import RawFill, CanonicalTrade, WalletPosition, TradeIntent


class TestVWAPComputation:
    async def test_single_fill(self, db, make_raw_fill):
        agg = FillAggregator(db=db)
        async with db.session() as session:
            fill = make_raw_fill(price=0.65, size=100.0)
            session.add(fill)

        count = await agg.process_pending_fills()
        assert count == 1

    async def test_multi_fill_vwap(self, db, make_raw_fill):
        """VWAP = sum(price*size) / sum(size)."""
        agg = FillAggregator(db=db)
        order_id = "shared_order"
        async with db.session() as session:
            f1 = make_raw_fill(market_order_id=order_id, price=0.60, size=100.0)
            f2 = make_raw_fill(market_order_id=order_id, price=0.70, size=100.0)
            session.add_all([f1, f2])

        await agg.process_pending_fills()

        async with db.session() as session:
            stmt = select(CanonicalTrade).where(
                CanonicalTrade.market_order_id == order_id
            )
            result = await session.execute(stmt)
            trade = result.scalar_one()
            assert abs(trade.avg_price - 0.65) < 0.001
            assert trade.total_size == 200.0
            assert trade.num_fills == 2

    async def test_weighted_vwap(self, db, make_raw_fill):
        """VWAP with unequal sizes."""
        agg = FillAggregator(db=db)
        order_id = "weighted_order"
        async with db.session() as session:
            f1 = make_raw_fill(market_order_id=order_id, price=0.60, size=300.0)
            f2 = make_raw_fill(market_order_id=order_id, price=0.80, size=100.0)
            session.add_all([f1, f2])

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(
                select(CanonicalTrade).where(CanonicalTrade.market_order_id == order_id)
            )
            trade = result.scalar_one()
            # VWAP = (0.60*300 + 0.80*100) / 400 = 260/400 = 0.65
            assert abs(trade.avg_price - 0.65) < 0.001
            assert trade.total_size == 400.0


class TestIntentDetermination:
    async def test_open_intent(self, db, make_raw_fill):
        """No prior position + BUY = OPEN."""
        agg = FillAggregator(db=db)
        async with db.session() as session:
            fill = make_raw_fill(side="BUY", size=100.0)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(CanonicalTrade))
            trade = result.scalar_one()
            assert trade.intent == TradeIntent.OPEN
            assert trade.position_before == 0.0
            assert trade.position_after == 100.0

    async def test_close_intent(self, db, make_raw_fill):
        """Existing position fully sold = CLOSE."""
        agg = FillAggregator(db=db)
        async with db.session() as session:
            pos = WalletPosition(
                wallet="0xtaker" + "0" * 34,
                condition_id="condition_abc123",
                outcome="YES",
                size=100.0,
                avg_entry_price=0.60,
            )
            session.add(pos)

        async with db.session() as session:
            fill = make_raw_fill(side="SELL", size=100.0)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(CanonicalTrade))
            trade = result.scalar_one()
            assert trade.intent == TradeIntent.CLOSE

    async def test_add_intent(self, db, make_raw_fill):
        """Existing position + BUY more = ADD."""
        agg = FillAggregator(db=db)
        async with db.session() as session:
            pos = WalletPosition(
                wallet="0xtaker" + "0" * 34,
                condition_id="condition_abc123",
                outcome="YES",
                size=100.0,
                avg_entry_price=0.60,
            )
            session.add(pos)

        async with db.session() as session:
            fill = make_raw_fill(side="BUY", size=50.0)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(CanonicalTrade))
            trade = result.scalar_one()
            assert trade.intent == TradeIntent.ADD

    async def test_reduce_intent(self, db, make_raw_fill):
        """Existing position + partial SELL = REDUCE."""
        agg = FillAggregator(db=db)
        async with db.session() as session:
            pos = WalletPosition(
                wallet="0xtaker" + "0" * 34,
                condition_id="condition_abc123",
                outcome="YES",
                size=100.0,
                avg_entry_price=0.60,
            )
            session.add(pos)

        async with db.session() as session:
            fill = make_raw_fill(side="SELL", size=30.0)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(CanonicalTrade))
            trade = result.scalar_one()
            assert trade.intent == TradeIntent.REDUCE

    async def test_unknown_intent_no_condition(self, db, make_raw_fill):
        agg = FillAggregator(db=db)
        async with db.session() as session:
            fill = make_raw_fill(condition_id=None, outcome=None)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(CanonicalTrade))
            trade = result.scalar_one()
            assert trade.intent == TradeIntent.UNKNOWN


class TestPositionUpdate:
    async def test_buy_creates_position(self, db, make_raw_fill):
        agg = FillAggregator(db=db)
        async with db.session() as session:
            fill = make_raw_fill(side="BUY", size=100.0, price=0.65)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(WalletPosition))
            pos = result.scalar_one()
            assert pos.size == 100.0
            assert abs(pos.avg_entry_price - 0.65) < 0.001

    async def test_buy_adds_to_existing(self, db, make_raw_fill):
        agg = FillAggregator(db=db)
        async with db.session() as session:
            pos = WalletPosition(
                wallet="0xtaker" + "0" * 34,
                condition_id="condition_abc123",
                outcome="YES",
                size=100.0,
                avg_entry_price=0.60,
            )
            session.add(pos)

        async with db.session() as session:
            fill = make_raw_fill(side="BUY", size=100.0, price=0.70)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(WalletPosition))
            pos = result.scalar_one()
            assert pos.size == 200.0
            assert abs(pos.avg_entry_price - 0.65) < 0.001

    async def test_sell_reduces_position(self, db, make_raw_fill):
        agg = FillAggregator(db=db)
        async with db.session() as session:
            pos = WalletPosition(
                wallet="0xtaker" + "0" * 34,
                condition_id="condition_abc123",
                outcome="YES",
                size=100.0,
                avg_entry_price=0.60,
            )
            session.add(pos)

        async with db.session() as session:
            fill = make_raw_fill(side="SELL", size=60.0, price=0.70)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(WalletPosition))
            pos = result.scalar_one()
            assert pos.size == 40.0

    async def test_sell_full_zeros_position(self, db, make_raw_fill):
        agg = FillAggregator(db=db)
        async with db.session() as session:
            pos = WalletPosition(
                wallet="0xtaker" + "0" * 34,
                condition_id="condition_abc123",
                outcome="YES",
                size=100.0,
                avg_entry_price=0.60,
            )
            session.add(pos)

        async with db.session() as session:
            fill = make_raw_fill(side="SELL", size=100.0, price=0.70)
            session.add(fill)

        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(select(WalletPosition))
            pos = result.scalar_one()
            assert pos.size == 0.0
            assert pos.avg_entry_price is None


class TestDeduplication:
    async def test_same_order_id_not_duplicated(self, db, make_raw_fill):
        agg = FillAggregator(db=db)
        order_id = "dedup_order"

        async with db.session() as session:
            fill = make_raw_fill(market_order_id=order_id, price=0.65, size=100.0)
            session.add(fill)
        await agg.process_pending_fills()

        async with db.session() as session:
            fill2 = make_raw_fill(market_order_id=order_id, price=0.66, size=50.0)
            session.add(fill2)
        await agg.process_pending_fills()

        async with db.session() as session:
            result = await session.execute(
                select(func.count()).select_from(CanonicalTrade)
            )
            count = result.scalar()
            assert count == 1
