"""
Mock trade feed for testing the detection and alert system.

Generates realistic-looking trades to test the full pipeline:
  Trade -> Detection -> Scoring -> Alert -> Slack
"""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable
from loguru import logger

from src.database.models import Platform, TradeType
from .base import BasePlatform, TradeEvent, MarketInfo


# Static mock markets - all with far-future close times to avoid stale "last minute" alerts
MOCK_MARKETS = [
    {
        "id": "bitcoin-150k-2026",
        "name": "Will Bitcoin reach $150,000 in 2026?",
        "volume_24h": 3_200_000,
        "close_time": datetime(2026, 12, 31, 23, 59),
    },
    {
        "id": "fed-rate-decision-march-2026",
        "name": "Will the Fed cut rates in March 2026?",
        "volume_24h": 1_500_000,
        "close_time": datetime(2026, 3, 18, 18, 0),
    },
    {
        "id": "super-bowl-2027-winner",
        "name": "Will the NFC team win Super Bowl 2027?",
        "volume_24h": 800_000,
        "close_time": datetime(2027, 2, 14, 23, 59),
    },
    {
        "id": "oscars-2027-best-picture",
        "name": "Will a sci-fi film win Best Picture at Oscars 2027?",
        "volume_24h": 650_000,
        "close_time": datetime(2027, 3, 7, 23, 59),
    },
    {
        "id": "obscure-local-event",
        "name": "Will candidate X win the city council race?",
        "volume_24h": 12_000,  # Low volume = obscure market signal
        "close_time": datetime(2026, 11, 3, 23, 59),
    },
]


def random_wallet() -> str:
    """Generate a random Ethereum-style wallet address"""
    return "0x" + "".join(random.choices("0123456789abcdef", k=40))


class MockTradeFeed(BasePlatform):
    """
    Mock trade feed that generates realistic whale trades for testing.

    Generates trades at configurable intervals with varying:
    - Trade sizes (some whales, some regular)
    - Wallet ages (some new, some established)
    - Markets (popular, obscure, closing soon)
    """

    def __init__(self, config: dict):
        super().__init__(config)

        self.trade_interval = config.get('trade_interval', 5)  # seconds between trades
        self.whale_probability = config.get('whale_probability', 0.3)  # 30% chance of whale
        self.new_account_probability = config.get('new_account_probability', 0.4)

        self._should_run = False
        self._trade_callback: Optional[Callable[[TradeEvent], Awaitable[None]]] = None

        # Track generated wallets
        self._new_wallets: set[str] = set()
        self._established_wallets: list[str] = [random_wallet() for _ in range(20)]

    @property
    def platform(self) -> Platform:
        return Platform.POLYMARKET

    def on_trade(self, callback: Callable[[TradeEvent], Awaitable[None]]):
        """Register callback for trade events"""
        self._trade_callback = callback

    async def start_listening(self):
        """Start generating mock trades"""
        self._should_run = True
        logger.info("Mock trade feed started - generating test trades")

        while self._should_run:
            try:
                # Generate a trade
                trade = self._generate_trade()

                if trade and self._trade_callback:
                    logger.info(f"Mock trade: ${trade.amount_usd:,.0f} on {trade.market_name[:30]}...")
                    await self._trade_callback(trade)

                # Wait before next trade
                await asyncio.sleep(self.trade_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Mock feed error: {e}")
                await asyncio.sleep(1)

    async def close(self):
        """Stop the mock feed"""
        self._should_run = False
        logger.info("Mock trade feed stopped")

    def _generate_trade(self) -> Optional[TradeEvent]:
        """Generate a mock trade with realistic characteristics"""

        # Pick a market
        market = random.choice(MOCK_MARKETS)

        # Determine if this is a whale trade
        is_whale = random.random() < self.whale_probability

        # Generate trade size
        if is_whale:
            # Whale trades: $10k - $500k
            amount = random.uniform(10_000, 500_000)
        else:
            # Regular trades: $100 - $5k
            amount = random.uniform(100, 5_000)

        # Determine wallet
        is_new_account = random.random() < self.new_account_probability

        if is_new_account:
            wallet = random_wallet()
            self._new_wallets.add(wallet)
        else:
            wallet = random.choice(self._established_wallets)

        # Generate other trade details
        trade_type = random.choice([TradeType.BUY, TradeType.SELL])
        price = random.uniform(0.1, 0.9)
        outcome = random.choice(["Yes", "No"])

        return TradeEvent(
            platform=Platform.POLYMARKET,
            wallet_address=wallet,
            market_id=market["id"],
            market_name=market["name"],
            trade_type=trade_type,
            amount_usd=amount,
            price=price,
            shares=amount / price if price > 0 else 0,
            outcome=outcome,
            timestamp=datetime.utcnow(),
            block_number=random.randint(80_000_000, 82_000_000),
            tx_hash="0x" + "".join(random.choices("0123456789abcdef", k=64)),
            raw_data={"mock": True, "is_new_account": is_new_account}
        )

    async def get_markets(self) -> list[MarketInfo]:
        """Return mock markets"""
        return [
            MarketInfo(
                id=m["id"],
                platform=Platform.POLYMARKET,
                name=m["name"],
                volume_24h=m["volume_24h"],
                close_time=m["close_time"],
            )
            for m in MOCK_MARKETS
        ]

    async def get_market(self, market_id: str) -> Optional[MarketInfo]:
        """Get a specific mock market"""
        for m in MOCK_MARKETS:
            if m["id"] == market_id:
                return MarketInfo(
                    id=m["id"],
                    platform=Platform.POLYMARKET,
                    name=m["name"],
                    volume_24h=m["volume_24h"],
                    close_time=m["close_time"],
                )
        return None

    def is_new_wallet(self, wallet: str) -> bool:
        """Check if wallet was generated as 'new' in this session"""
        return wallet in self._new_wallets

    async def connect(self):
        """Mock connect - no-op"""
        self._is_connected = True

    async def disconnect(self):
        """Mock disconnect - no-op"""
        self._is_connected = False

    async def get_recent_trades(self, market_id: Optional[str] = None, limit: int = 100) -> list[TradeEvent]:
        """Return empty list - mock doesn't store trades"""
        return []

    async def poll_trades(self):
        """Poll is same as start_listening for mock"""
        await self.start_listening()
