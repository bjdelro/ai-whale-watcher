"""
Database models for Whale Watcher
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime,
    ForeignKey, Text, JSON, Enum as SQLEnum, Index
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Platform(str, Enum):
    """Supported prediction market platforms"""
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class TradeType(str, Enum):
    """Trade direction"""
    BUY = "buy"
    SELL = "sell"


class AlertType(str, Enum):
    """Types of whale alerts"""
    NEW_ACCOUNT_LARGE_TRADE = "new_account_large_trade"
    LARGE_TRADE = "large_trade"
    LAST_MINUTE_TRADE = "last_minute_trade"
    OBSCURE_MARKET = "obscure_market"
    COMPOSITE = "composite"  # Multiple signals combined


class AlertSeverity(str, Enum):
    """Alert severity levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Wallet(Base):
    """Tracked wallet/account"""
    __tablename__ = "wallets"

    address = Column(String(255), primary_key=True)
    platform = Column(SQLEnum(Platform), nullable=False)

    # When we first saw this wallet
    first_seen = Column(DateTime, default=datetime.utcnow)

    # When the wallet was created on-chain (for Polymarket)
    # None if we haven't looked it up yet
    blockchain_first_tx = Column(DateTime, nullable=True)

    # Cached computation - is this a "new" account?
    is_new_account = Column(Boolean, default=True)

    # When we last checked wallet age
    age_checked_at = Column(DateTime, nullable=True)

    # Total trade volume we've seen from this wallet
    total_volume_usd = Column(Float, default=0.0)

    # Number of trades we've seen
    trade_count = Column(Integer, default=0)

    # Optional extra data (username, profile info, etc.)
    extra_data = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    trades = relationship("Trade", back_populates="wallet")

    def __repr__(self):
        return f"<Wallet {self.address[:10]}... ({self.platform.value})>"


class Market(Base):
    """Prediction market being monitored"""
    __tablename__ = "markets"

    id = Column(String(255), primary_key=True)
    platform = Column(SQLEnum(Platform), nullable=False)

    # Market details
    name = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True)

    # Market metrics
    volume_24h = Column(Float, default=0.0)
    volume_7d = Column(Float, default=0.0)
    liquidity = Column(Float, default=0.0)

    # Market timing
    close_time = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    # Is this considered an "obscure" market?
    is_obscure = Column(Boolean, default=False)

    # Raw market data from API
    extra_data = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    trades = relationship("Trade", back_populates="market")

    def __repr__(self):
        return f"<Market {self.name[:30]}... ({self.platform.value})>"

    @property
    def seconds_until_close(self) -> Optional[int]:
        """Returns seconds until market closes, or None if no close time"""
        if not self.close_time:
            return None
        delta = self.close_time - datetime.utcnow()
        return max(0, int(delta.total_seconds()))


class Trade(Base):
    """Individual trade record"""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # External trade ID from the platform
    external_id = Column(String(255), nullable=True)

    platform = Column(SQLEnum(Platform), nullable=False)

    # Foreign keys
    wallet_address = Column(String(255), ForeignKey("wallets.address"), nullable=False)
    market_id = Column(String(255), ForeignKey("markets.id"), nullable=False)

    # Trade details
    trade_type = Column(SQLEnum(TradeType), nullable=False)
    amount_usd = Column(Float, nullable=False)
    price = Column(Float, nullable=True)  # 0-1 for prediction markets
    shares = Column(Float, nullable=True)

    # Outcome being traded (Yes/No or specific outcome)
    outcome = Column(String(100), nullable=True)

    # When the trade happened
    timestamp = Column(DateTime, nullable=False)

    # Blockchain info (for Polymarket)
    block_number = Column(Integer, nullable=True)
    tx_hash = Column(String(255), nullable=True)

    # Raw trade data from API
    raw_data = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    wallet = relationship("Wallet", back_populates="trades")
    market = relationship("Market", back_populates="trades")
    alerts = relationship("Alert", back_populates="trade")

    # Indexes for common queries
    __table_args__ = (
        Index('idx_trade_platform_timestamp', 'platform', 'timestamp'),
        Index('idx_trade_wallet_timestamp', 'wallet_address', 'timestamp'),
        Index('idx_trade_amount', 'amount_usd'),
    )

    def __repr__(self):
        return f"<Trade {self.trade_type.value} ${self.amount_usd:,.0f} on {self.market_id[:20]}...>"


class Alert(Base):
    """Generated whale alert"""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # What triggered this alert
    alert_type = Column(SQLEnum(AlertType), nullable=False)
    severity = Column(SQLEnum(AlertSeverity), default=AlertSeverity.MEDIUM)

    # Composite score (0-100)
    score = Column(Float, nullable=False)

    # The trade that triggered this alert
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=False)

    # Human-readable alert message
    message = Column(Text, nullable=False)

    # Detailed breakdown of why this was flagged
    details = Column(JSON, nullable=True)

    # Notification status
    sent_to_telegram = Column(Boolean, default=False)
    telegram_message_id = Column(Integer, nullable=True)
    sent_to_slack = Column(Boolean, default=False)
    slack_message_ts = Column(String(50), nullable=True)
    sent_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    trade = relationship("Trade", back_populates="alerts")

    # Indexes
    __table_args__ = (
        Index('idx_alert_created', 'created_at'),
        Index('idx_alert_severity', 'severity'),
        Index('idx_alert_score', 'score'),
    )

    def __repr__(self):
        return f"<Alert {self.alert_type.value} score={self.score:.1f}>"


class WalletAgeCache(Base):
    """Cache for wallet age lookups from PolygonScan"""
    __tablename__ = "wallet_age_cache"

    wallet_address = Column(String(255), primary_key=True)
    first_tx_timestamp = Column(DateTime, nullable=True)
    first_tx_block = Column(Integer, nullable=True)
    lookup_timestamp = Column(DateTime, default=datetime.utcnow)

    # Did the lookup succeed?
    success = Column(Boolean, default=True)
    error_message = Column(String(500), nullable=True)

    def __repr__(self):
        return f"<WalletAgeCache {self.wallet_address[:10]}...>"


# =============================================================================
# NEW MODELS FOR COPY-EDGE SCORING SYSTEM
# =============================================================================

class TradeIntent(str, Enum):
    """Intent behind a trade based on position changes"""
    OPEN = "open"       # New position
    CLOSE = "close"     # Exiting position
    ADD = "add"         # Adding to existing position
    REDUCE = "reduce"   # Reducing existing position
    UNKNOWN = "unknown" # Cannot determine


class WalletTier(str, Enum):
    """Wallet quality tiers based on historical performance"""
    ELITE = "elite"     # Top performers
    GOOD = "good"       # Above average
    NEUTRAL = "neutral" # Average or insufficient data
    BAD = "bad"         # Below average
    UNKNOWN = "unknown" # Not enough trades


class RawFill(Base):
    """
    Raw fills from Polymarket API - immutable append-only.
    Each fill represents a single execution against the orderbook.
    Multiple fills can belong to the same market order.
    """
    __tablename__ = "raw_fills"

    id = Column(String(255), primary_key=True)  # fill_id from API
    market_order_id = Column(String(255), index=True)
    bucket_index = Column(Integer, nullable=True)  # For multi-fill orders

    # Participants
    maker = Column(String(255), index=True)
    taker = Column(String(255), index=True)

    # Trade details
    side = Column(String(10), nullable=False)  # BUY/SELL
    outcome = Column(String(10), nullable=True)  # YES/NO
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)

    # Market identifiers
    token_id = Column(String(255), index=True)
    condition_id = Column(String(255), index=True)

    # Timing
    timestamp = Column(DateTime, index=True, nullable=False)

    # Blockchain
    tx_hash = Column(String(255), nullable=True)
    block_number = Column(Integer, nullable=True)

    # Store full API response for replay/debugging
    raw_json = Column(Text, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    processed = Column(Boolean, default=False)  # Has this been aggregated?

    __table_args__ = (
        Index('idx_raw_fill_market_order', 'market_order_id'),
        Index('idx_raw_fill_taker_time', 'taker', 'timestamp'),
        Index('idx_raw_fill_token_time', 'token_id', 'timestamp'),
        Index('idx_raw_fill_processed', 'processed'),
    )

    def __repr__(self):
        return f"<RawFill {self.id[:20]}... {self.side} {self.size}@{self.price}>"


class CanonicalTrade(Base):
    """
    Aggregated trades - one row per market_order_id.
    Groups multiple fills into a single logical trade for analysis.
    """
    __tablename__ = "canonical_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_order_id = Column(String(255), unique=True, index=True)

    # Who made the trade (taker)
    wallet = Column(String(255), index=True, nullable=False)

    # Market identifiers
    condition_id = Column(String(255), index=True, nullable=False)
    token_id = Column(String(255), index=True, nullable=False)

    # Aggregated trade details
    side = Column(String(10), nullable=False)  # BUY/SELL
    outcome = Column(String(10), nullable=True)  # YES/NO
    avg_price = Column(Float, nullable=False)  # Volume-weighted average
    total_size = Column(Float, nullable=False)  # Total shares
    total_usd = Column(Float, nullable=False)  # Total USD value

    # Fill aggregation stats
    num_fills = Column(Integer, default=1)
    first_fill_time = Column(DateTime, nullable=False)
    last_fill_time = Column(DateTime, nullable=False)
    duration_ms = Column(Integer, default=0)  # Time to fill

    # Orderbook context at time of trade
    pre_mid = Column(Float, nullable=True)  # Mid price before trade
    post_mid = Column(Float, nullable=True)  # Mid price after trade
    price_impact_bps = Column(Float, nullable=True)  # Basis points impact
    pct_book_consumed = Column(Float, nullable=True)  # % of orderbook depth taken

    # Position context
    intent = Column(SQLEnum(TradeIntent), default=TradeIntent.UNKNOWN)
    position_before = Column(Float, nullable=True)  # Position size before
    position_after = Column(Float, nullable=True)  # Position size after

    # Alert status
    alerted = Column(Boolean, default=False)
    alert_score = Column(Float, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    markouts = relationship("Markout", back_populates="trade")

    __table_args__ = (
        Index('idx_canonical_wallet_time', 'wallet', 'first_fill_time'),
        Index('idx_canonical_condition', 'condition_id'),
        Index('idx_canonical_time', 'first_fill_time'),
        Index('idx_canonical_not_alerted', 'alerted'),
    )

    def __repr__(self):
        return f"<CanonicalTrade {self.side} {self.total_size:.2f}@{self.avg_price:.3f} ${self.total_usd:.0f}>"


class Markout(Base):
    """
    Forward returns for each canonical trade.
    Tracks price movement at various horizons after trade execution.
    """
    __tablename__ = "markouts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    canonical_trade_id = Column(Integer, ForeignKey("canonical_trades.id"), nullable=False)

    # Which time horizon
    horizon = Column(String(10), nullable=False)  # 5m, 1h, 6h, 1d

    # Prices
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)  # NULL if not yet computed

    # Return in basis points (adjusted for trade direction)
    return_bps = Column(Float, nullable=True)

    # Status
    is_final = Column(Boolean, default=False)  # Has exit price been recorded?
    scheduled_for = Column(DateTime, nullable=True)  # When to compute exit
    computed_at = Column(DateTime, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    trade = relationship("CanonicalTrade", back_populates="markouts")

    __table_args__ = (
        Index('idx_markout_trade_horizon', 'canonical_trade_id', 'horizon'),
        Index('idx_markout_pending', 'is_final', 'scheduled_for'),
    )

    def __repr__(self):
        status = "final" if self.is_final else "pending"
        return f"<Markout {self.horizon} {self.return_bps:.1f}bps ({status})>"


class WalletStats(Base):
    """
    Rolling wallet performance metrics.
    Updated periodically as new markouts are computed.
    """
    __tablename__ = "wallet_stats"

    wallet = Column(String(255), primary_key=True)

    # Volume metrics
    total_trades = Column(Integer, default=0)
    total_volume_usd = Column(Float, default=0.0)
    avg_trade_size = Column(Float, nullable=True)

    # Win rates by horizon
    win_rate_5m = Column(Float, nullable=True)
    win_rate_1h = Column(Float, nullable=True)
    win_rate_6h = Column(Float, nullable=True)
    win_rate_1d = Column(Float, nullable=True)

    # Average markouts (basis points)
    avg_markout_5m = Column(Float, nullable=True)
    avg_markout_1h = Column(Float, nullable=True)
    avg_markout_6h = Column(Float, nullable=True)
    avg_markout_1d = Column(Float, nullable=True)

    # Risk-adjusted metrics
    sharpe_1h = Column(Float, nullable=True)
    sharpe_1d = Column(Float, nullable=True)

    # Classification
    tier = Column(SQLEnum(WalletTier), default=WalletTier.UNKNOWN)

    # Trading patterns
    preferred_markets = Column(Text, nullable=True)  # JSON list of condition_ids
    avg_position_hold_time = Column(Float, nullable=True)  # Hours

    # Activity
    first_trade_at = Column(DateTime, nullable=True)
    last_trade_at = Column(DateTime, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_wallet_stats_tier', 'tier'),
        Index('idx_wallet_stats_volume', 'total_volume_usd'),
    )

    def __repr__(self):
        return f"<WalletStats {self.wallet[:10]}... tier={self.tier.value} trades={self.total_trades}>"


class OrderbookSnapshot(Base):
    """
    Periodic orderbook snapshots for depth analysis.
    Used to compute % of book consumed by trades.
    """
    __tablename__ = "orderbook_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_id = Column(String(255), index=True, nullable=False)
    timestamp = Column(DateTime, index=True, nullable=False)

    # Price info
    mid_price = Column(Float, nullable=False)
    best_bid = Column(Float, nullable=True)
    best_ask = Column(Float, nullable=True)
    spread_bps = Column(Float, nullable=True)

    # Depth within 1% of mid (USD)
    bid_depth_1pct = Column(Float, nullable=True)
    ask_depth_1pct = Column(Float, nullable=True)

    # Depth within 5% of mid (USD)
    bid_depth_5pct = Column(Float, nullable=True)
    ask_depth_5pct = Column(Float, nullable=True)

    # Total visible depth (USD)
    total_bid_depth = Column(Float, nullable=True)
    total_ask_depth = Column(Float, nullable=True)

    # Raw orderbook for detailed analysis (JSON compressed)
    raw_book = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_orderbook_token_time', 'token_id', 'timestamp'),
    )

    def __repr__(self):
        return f"<OrderbookSnapshot {self.token_id[:20]}... mid={self.mid_price:.3f}>"


class MarketRegistry(Base):
    """
    Market metadata cache.
    Stores market info from Gamma API for quick lookups.
    """
    __tablename__ = "market_registry"

    condition_id = Column(String(255), primary_key=True)

    # Token IDs for YES/NO outcomes
    token_id_yes = Column(String(255), nullable=True)
    token_id_no = Column(String(255), nullable=True)

    # Market info
    question = Column(String(1000), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True)
    slug = Column(String(500), nullable=True)

    # Timing
    end_date = Column(DateTime, nullable=True)
    created_date = Column(DateTime, nullable=True)

    # Resolution
    resolution = Column(String(10), nullable=True)  # NULL, YES, NO
    resolved_at = Column(DateTime, nullable=True)

    # Volume metrics
    total_volume = Column(Float, default=0.0)
    volume_24h = Column(Float, default=0.0)
    liquidity = Column(Float, default=0.0)

    # Status
    is_active = Column(Boolean, default=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_market_active', 'is_active'),
        Index('idx_market_category', 'category'),
        Index('idx_market_end_date', 'end_date'),
    )

    def __repr__(self):
        return f"<MarketRegistry {self.question[:40]}...>"

    @property
    def hours_to_close(self) -> Optional[float]:
        """Returns hours until market closes, or None if no end date"""
        if not self.end_date:
            return None
        delta = self.end_date - datetime.utcnow()
        return max(0, delta.total_seconds() / 3600)


class WalletPosition(Base):
    """
    Current wallet positions per market.
    Used to determine trade intent (open vs close).
    """
    __tablename__ = "wallet_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet = Column(String(255), index=True, nullable=False)
    condition_id = Column(String(255), index=True, nullable=False)
    outcome = Column(String(10), nullable=False)  # YES/NO

    # Position details
    size = Column(Float, default=0.0)
    avg_entry_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, default=0.0)

    # Timing
    first_entry_at = Column(DateTime, nullable=True)
    last_update_at = Column(DateTime, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_position_wallet_market', 'wallet', 'condition_id', 'outcome', unique=True),
    )

    def __repr__(self):
        return f"<WalletPosition {self.wallet[:10]}... {self.outcome}={self.size:.2f}>"
