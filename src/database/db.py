"""
Database initialization and session management.
Supports both synchronous and asynchronous operations.
"""

import os
from datetime import datetime
from typing import Optional
from contextlib import contextmanager, asynccontextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from loguru import logger

from .models import Base, Wallet, Trade, Market, Alert, Platform


class Database:
    """Database connection and session management (supports sync and async)"""

    def __init__(self, db_path: str = "whale_watcher.db"):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file, or full connection URL
        """
        # Handle both file paths and connection URLs
        if db_path.startswith("sqlite"):
            self.db_url = db_path
            # Extract file path for sync engine
            if "+aiosqlite" in db_path:
                self.db_path = db_path.replace("sqlite+aiosqlite:///", "")
            else:
                self.db_path = db_path.replace("sqlite:///", "")
        else:
            self.db_path = db_path
            self.db_url = f"sqlite:///{db_path}"

        self.engine = None
        self.async_engine = None
        self.SessionLocal = None
        self.AsyncSessionLocal = None
        self._initialized = False
        self._async_initialized = False

    async def initialize(self):
        """Initialize async database connection and create tables"""
        if self._async_initialized:
            return

        async_url = f"sqlite+aiosqlite:///{self.db_path}"

        self.async_engine = create_async_engine(
            async_url,
            echo=False,
        )

        # Create tables using sync engine first
        sync_url = f"sqlite:///{self.db_path}"
        sync_engine = create_engine(sync_url, echo=False)
        Base.metadata.create_all(bind=sync_engine)
        sync_engine.dispose()

        # Create async session factory
        self.AsyncSessionLocal = async_sessionmaker(
            self.async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        self._async_initialized = True
        logger.info(f"Async database initialized at {self.db_path}")

    async def close(self):
        """Close async database connection"""
        if self.async_engine:
            await self.async_engine.dispose()
            self._async_initialized = False

    @asynccontextmanager
    async def session(self):
        """Get an async database session with automatic cleanup"""
        if not self._async_initialized:
            await self.initialize()

        async with self.AsyncSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error(f"Database error: {e}")
                raise

    def init(self):
        """Initialize database connection and create tables"""
        if self._initialized:
            return

        # Create SQLite database with WAL mode for better concurrency
        db_url = f"sqlite:///{self.db_path}"

        self.engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False  # Set to True for SQL debugging
        )

        # Enable WAL mode for better concurrent access
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-64000")  # 64MB cache
            cursor.close()

        # Create all tables
        Base.metadata.create_all(bind=self.engine)

        # Create session factory
        self.SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine
        )

        self._initialized = True
        logger.info(f"Database initialized at {self.db_path}")

    @contextmanager
    def get_session(self):
        """Get a database session with automatic cleanup"""
        if not self._initialized:
            self.init()

        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            session.close()

    def get_or_create_wallet(
        self,
        session: Session,
        address: str,
        platform: Platform,
        metadata: Optional[dict] = None
    ) -> Wallet:
        """Get existing wallet or create new one"""
        wallet = session.query(Wallet).filter_by(address=address).first()

        if not wallet:
            wallet = Wallet(
                address=address,
                platform=platform,
                metadata=metadata
            )
            session.add(wallet)
            session.flush()
            logger.debug(f"Created new wallet: {address[:10]}...")

        return wallet

    def get_or_create_market(
        self,
        session: Session,
        market_id: str,
        platform: Platform,
        name: str,
        **kwargs
    ) -> Market:
        """Get existing market or create new one"""
        market = session.query(Market).filter_by(id=market_id).first()

        if not market:
            market = Market(
                id=market_id,
                platform=platform,
                name=name,
                **kwargs
            )
            session.add(market)
            session.flush()
            logger.debug(f"Created new market: {name[:30]}...")
        else:
            # Update market info
            for key, value in kwargs.items():
                if hasattr(market, key) and value is not None:
                    setattr(market, key, value)
            market.updated_at = datetime.utcnow()

        return market

    def record_trade(
        self,
        session: Session,
        wallet: Wallet,
        market: Market,
        **trade_data
    ) -> Trade:
        """Record a new trade"""
        trade = Trade(
            wallet_address=wallet.address,
            market_id=market.id,
            platform=wallet.platform,
            **trade_data
        )
        session.add(trade)

        # Update wallet stats
        wallet.trade_count += 1
        wallet.total_volume_usd += trade_data.get('amount_usd', 0)
        wallet.updated_at = datetime.utcnow()

        session.flush()
        return trade

    def record_alert(
        self,
        session: Session,
        trade: Trade,
        alert_type,
        severity,
        score: float,
        message: str,
        details: Optional[dict] = None
    ) -> Alert:
        """Record a new alert"""
        alert = Alert(
            trade_id=trade.id,
            alert_type=alert_type,
            severity=severity,
            score=score,
            message=message,
            details=details
        )
        session.add(alert)
        session.flush()
        return alert

    def get_recent_alerts(
        self,
        session: Session,
        limit: int = 50,
        unsent_only: bool = False
    ) -> list[Alert]:
        """Get recent alerts"""
        query = session.query(Alert).order_by(Alert.created_at.desc())

        if unsent_only:
            query = query.filter(Alert.sent_to_telegram == False)

        return query.limit(limit).all()

    def get_wallet_trades(
        self,
        session: Session,
        wallet_address: str,
        limit: int = 100
    ) -> list[Trade]:
        """Get trades for a specific wallet"""
        return (
            session.query(Trade)
            .filter_by(wallet_address=wallet_address)
            .order_by(Trade.timestamp.desc())
            .limit(limit)
            .all()
        )

    def get_large_trades(
        self,
        session: Session,
        min_amount: float,
        hours: int = 24,
        limit: int = 100
    ) -> list[Trade]:
        """Get large trades from recent hours"""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        return (
            session.query(Trade)
            .filter(Trade.amount_usd >= min_amount)
            .filter(Trade.timestamp >= cutoff)
            .order_by(Trade.amount_usd.desc())
            .limit(limit)
            .all()
        )


# Global database instance
_db: Optional[Database] = None


def get_db() -> Database:
    """Get the global database instance"""
    global _db
    if _db is None:
        _db = Database()
        _db.init()
    return _db


def init_db(db_path: str = "whale_watcher.db") -> Database:
    """Initialize the global database with custom path"""
    global _db
    _db = Database(db_path)
    _db.init()
    return _db
