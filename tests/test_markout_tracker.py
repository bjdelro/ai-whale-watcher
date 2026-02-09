"""Tests for MarkoutTracker - return computation and markout scheduling."""

from datetime import datetime
from sqlalchemy import select, func
from src.scoring.markout_tracker import MarkoutTracker
from src.database.models import CanonicalTrade, Markout, TradeIntent


class TestComputeReturnBps:
    """Tests for MarkoutTracker._compute_return_bps."""

    def _tracker(self, db, mock_polymarket_client):
        return MarkoutTracker(db=db, client=mock_polymarket_client)

    async def test_buy_price_up(self, db, mock_polymarket_client):
        t = self._tracker(db, mock_polymarket_client)
        result = t._compute_return_bps(entry_price=0.50, exit_price=0.55, side="BUY")
        assert result == 1000.0

    async def test_buy_price_down(self, db, mock_polymarket_client):
        t = self._tracker(db, mock_polymarket_client)
        result = t._compute_return_bps(entry_price=0.50, exit_price=0.45, side="BUY")
        assert result == -1000.0

    async def test_buy_price_flat(self, db, mock_polymarket_client):
        t = self._tracker(db, mock_polymarket_client)
        result = t._compute_return_bps(entry_price=0.50, exit_price=0.50, side="BUY")
        assert result == 0.0

    async def test_sell_price_down_profit(self, db, mock_polymarket_client):
        t = self._tracker(db, mock_polymarket_client)
        result = t._compute_return_bps(entry_price=0.50, exit_price=0.45, side="SELL")
        assert result == 1000.0

    async def test_sell_price_up_loss(self, db, mock_polymarket_client):
        t = self._tracker(db, mock_polymarket_client)
        result = t._compute_return_bps(entry_price=0.50, exit_price=0.55, side="SELL")
        assert result == -1000.0

    async def test_zero_entry_price(self, db, mock_polymarket_client):
        t = self._tracker(db, mock_polymarket_client)
        result = t._compute_return_bps(entry_price=0.0, exit_price=0.50, side="BUY")
        assert result == 0.0


class TestScheduleMarkouts:
    """Tests for MarkoutTracker.schedule_markouts."""

    async def test_creates_all_horizons(self, db, mock_polymarket_client, make_canonical_trade):
        t = MarkoutTracker(db=db, client=mock_polymarket_client)
        trade = make_canonical_trade(avg_price=0.65)

        async with db.session() as session:
            session.add(trade)
            await session.flush()
            trade_id = trade.id

        await t.schedule_markouts(trade)

        async with db.session() as session:
            result = await session.execute(
                select(Markout).where(Markout.canonical_trade_id == trade_id)
            )
            markouts = result.scalars().all()
            horizons = {m.horizon for m in markouts}
            assert horizons == {"5m", "1h", "6h", "1d"}

            for m in markouts:
                assert m.entry_price == 0.65
                assert m.exit_price is None
                assert m.is_final is False

    async def test_does_not_duplicate_markouts(self, db, mock_polymarket_client, make_canonical_trade):
        t = MarkoutTracker(db=db, client=mock_polymarket_client)
        trade = make_canonical_trade(avg_price=0.65)

        async with db.session() as session:
            session.add(trade)
            await session.flush()
            trade_id = trade.id

        await t.schedule_markouts(trade)
        await t.schedule_markouts(trade)

        async with db.session() as session:
            result = await session.execute(
                select(func.count()).select_from(Markout).where(
                    Markout.canonical_trade_id == trade_id
                )
            )
            count = result.scalar()
            assert count == 4
