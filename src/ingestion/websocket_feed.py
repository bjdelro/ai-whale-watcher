"""
WebSocket feed for real-time trade data from Polymarket.
Connects to the CLOB WebSocket API for live trade events.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import AsyncIterator, Callable, Dict, List, Optional, Set
import websockets
from websockets.exceptions import ConnectionClosed

from ..core.polymarket_client import TradeData

logger = logging.getLogger(__name__)


def _generate_ws_auth():
    """Generate WebSocket authentication using py-clob-client"""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        private_key = os.getenv("PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY")
        if not private_key:
            return None

        # Create client and get API credentials
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        # Generate auth for WebSocket
        timestamp = int(time.time())
        return {
            "apiKey": creds.api_key,
            "secret": creds.api_secret,
            "passphrase": creds.api_passphrase,
            "timestamp": timestamp,
        }
    except Exception as e:
        logger.warning(f"Failed to generate WS auth: {e}")
        return None


class WebSocketFeed:
    """
    Real-time trade feed via Polymarket WebSocket.

    WebSocket URL: wss://ws-subscriptions-clob.polymarket.com/ws/market

    Message types:
    - trade: Individual fill events
    - price_change: Price updates
    """

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        on_trade: Optional[Callable[[TradeData], None]] = None,
        reconnect_delay: float = 5.0,
        max_reconnect_attempts: int = 100,  # High for long-running
    ):
        """
        Initialize the WebSocket feed.

        Args:
            on_trade: Callback for trade events
            reconnect_delay: Seconds to wait between reconnection attempts
            max_reconnect_attempts: Max reconnection attempts before giving up
        """
        self.on_trade = on_trade
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts

        # Authentication
        self._auth = _generate_ws_auth()
        if self._auth:
            logger.info("WebSocket authentication configured")
        else:
            logger.warning("WebSocket running without authentication")

        # Connection state
        self._ws = None
        self._running = False
        self._connected = False
        self._reconnect_count = 0

        # Subscriptions
        self._subscribed_tokens: Set[str] = set()
        self._subscribed_markets: Set[str] = set()

        # Stats
        self._messages_received = 0
        self._trades_received = 0
        self._last_message_time: Optional[datetime] = None

    async def connect(self):
        """Connect to the WebSocket"""
        self._running = True

        while self._running and self._reconnect_count < self.max_reconnect_attempts:
            try:
                logger.info(f"Connecting to WebSocket: {self.WS_URL}")

                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_count = 0
                    logger.info("WebSocket connected successfully")

                    # Resubscribe to markets
                    await self._resubscribe()

                    # Process messages
                    await self._message_loop()

            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            self._connected = False
            self._ws = None

            if self._running:
                self._reconnect_count += 1
                wait_time = self.reconnect_delay * self._reconnect_count
                logger.info(f"Reconnecting in {wait_time}s (attempt {self._reconnect_count})")
                await asyncio.sleep(wait_time)

        if self._reconnect_count >= self.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached")

    async def disconnect(self):
        """Disconnect from the WebSocket"""
        self._running = False
        if self._ws:
            await self._ws.close()
        self._connected = False
        logger.info("WebSocket disconnected")

    async def subscribe_token(self, token_id: str):
        """Subscribe to trades for a specific token"""
        self._subscribed_tokens.add(token_id)

        if self._connected and self._ws:
            await self._send_subscription(token_id)

    async def subscribe_market(self, condition_id: str):
        """Subscribe to trades for a market (both YES and NO tokens)"""
        self._subscribed_markets.add(condition_id)

        # Note: The actual subscription happens when we have token IDs
        # This is a placeholder for market-level subscriptions

    async def unsubscribe_token(self, token_id: str):
        """Unsubscribe from a token"""
        self._subscribed_tokens.discard(token_id)

        if self._connected and self._ws:
            await self._send_unsubscription(token_id)

    async def _send_subscription(self, token_id: str):
        """Send subscription message with authentication"""
        if not self._ws:
            return

        msg = {
            "type": "MARKET",
            "assets_ids": [token_id],
        }

        # Add auth if available
        if self._auth:
            msg["auth"] = self._auth

        try:
            await self._ws.send(json.dumps(msg))
            logger.debug(f"Subscribed to token: {token_id[:30]}...")
        except Exception as e:
            logger.error(f"Failed to subscribe: {e}")

    async def _send_unsubscription(self, token_id: str):
        """Send unsubscription message"""
        if not self._ws:
            return

        msg = {
            "type": "unsubscribe",
            "channel": "market",
            "assets_ids": [token_id],
        }

        try:
            await self._ws.send(json.dumps(msg))
            logger.debug(f"Unsubscribed from token: {token_id[:30]}...")
        except Exception as e:
            logger.error(f"Failed to unsubscribe: {e}")

    async def _resubscribe(self):
        """Resubscribe to all tokens after reconnection"""
        for token_id in self._subscribed_tokens:
            await self._send_subscription(token_id)
            await asyncio.sleep(0.1)  # Rate limiting

    async def _message_loop(self):
        """Process incoming WebSocket messages"""
        async for message in self._ws:
            self._messages_received += 1
            self._last_message_time = datetime.utcnow()

            try:
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON message: {message[:100]}")
            except Exception as e:
                logger.error(f"Error handling message: {e}")

    async def _handle_message(self, data):
        """Handle a parsed WebSocket message"""
        # Handle list responses (empty subscription confirmations)
        if isinstance(data, list):
            if data:
                # Process each item in the list
                for item in data:
                    if isinstance(item, dict):
                        await self._handle_message(item)
            return

        if not isinstance(data, dict):
            logger.debug(f"Unexpected message format: {type(data)}")
            return

        msg_type = data.get("type") or data.get("event_type")

        if msg_type == "trade" or msg_type == "last_trade_price":
            await self._handle_trade(data)
        elif msg_type == "price_change":
            # Price updates - could be useful for markouts
            pass
        elif msg_type == "book":
            # Orderbook updates
            pass
        elif msg_type == "subscribed":
            logger.debug(f"Subscription confirmed: {data}")
        elif msg_type == "error":
            logger.error(f"WebSocket error message: {data}")
        else:
            logger.debug(f"Unknown message type: {msg_type}")

    async def _handle_trade(self, data: dict):
        """Handle a trade message"""
        self._trades_received += 1

        try:
            trade = self._parse_trade_message(data)

            if self.on_trade:
                self.on_trade(trade)

        except Exception as e:
            logger.warning(f"Failed to parse trade message: {e}")

    def _parse_trade_message(self, data: dict) -> TradeData:
        """Parse a WebSocket trade message into TradeData"""
        # Handle various message formats
        trade_data = data.get("data", data)

        # Parse timestamp
        timestamp = trade_data.get("timestamp") or trade_data.get("match_time")
        if isinstance(timestamp, str):
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except:
                ts = datetime.utcnow()
        elif isinstance(timestamp, (int, float)):
            if timestamp > 1e12:
                timestamp = timestamp / 1000
            ts = datetime.fromtimestamp(timestamp)
        else:
            ts = datetime.utcnow()

        return TradeData(
            id=str(trade_data.get("id") or trade_data.get("match_id") or ""),
            market_order_id=trade_data.get("market_order_id") or trade_data.get("order_id"),
            bucket_index=trade_data.get("bucket_index"),
            maker=trade_data.get("maker") or trade_data.get("maker_address") or "",
            taker=trade_data.get("taker") or trade_data.get("taker_address") or "",
            side=trade_data.get("side", "").upper() or ("BUY" if trade_data.get("is_buy") else "SELL"),
            outcome=trade_data.get("outcome"),
            price=float(trade_data.get("price", 0)),
            size=float(trade_data.get("size") or trade_data.get("amount") or 0),
            token_id=trade_data.get("token_id") or trade_data.get("asset_id") or "",
            condition_id=trade_data.get("condition_id"),
            timestamp=ts,
            tx_hash=trade_data.get("transaction_hash") or trade_data.get("tx_hash"),
            block_number=trade_data.get("block_number"),
            raw_json=data,
        )

    async def stream_trades(self) -> AsyncIterator[TradeData]:
        """
        Stream trades as an async iterator.

        Usage:
            async for trade in feed.stream_trades():
                process(trade)
        """
        queue: asyncio.Queue[TradeData] = asyncio.Queue()

        def on_trade(trade: TradeData):
            try:
                queue.put_nowait(trade)
            except asyncio.QueueFull:
                logger.warning("Trade queue full, dropping trade")

        old_callback = self.on_trade
        self.on_trade = on_trade

        try:
            while self._running:
                try:
                    trade = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield trade
                except asyncio.TimeoutError:
                    continue
        finally:
            self.on_trade = old_callback

    def get_stats(self) -> dict:
        """Get feed statistics"""
        return {
            "connected": self._connected,
            "running": self._running,
            "reconnect_count": self._reconnect_count,
            "subscribed_tokens": len(self._subscribed_tokens),
            "messages_received": self._messages_received,
            "trades_received": self._trades_received,
            "last_message": (
                self._last_message_time.isoformat() if self._last_message_time else None
            ),
        }


class TradeFeedManager:
    """
    Manages the WebSocket feed and stores trades to the database.
    """

    def __init__(
        self,
        db,
        feed: Optional[WebSocketFeed] = None,
    ):
        """
        Initialize the feed manager.

        Args:
            db: Database instance
            feed: Optional WebSocket feed (created if not provided)
        """
        self.db = db
        self.feed = feed or WebSocketFeed(on_trade=self._on_trade)

        # Trade buffer for batch inserts
        self._trade_buffer: List[TradeData] = []
        self._buffer_size = 100
        self._last_flush = datetime.utcnow()
        self._flush_interval = 5.0  # seconds

        # Stats
        self._trades_stored = 0

    def _on_trade(self, trade: TradeData):
        """Callback for incoming trades"""
        self._trade_buffer.append(trade)

        # Flush if buffer is full
        if len(self._trade_buffer) >= self._buffer_size:
            asyncio.create_task(self._flush_buffer())

    async def _flush_buffer(self):
        """Flush trade buffer to database"""
        if not self._trade_buffer:
            return

        trades = self._trade_buffer.copy()
        self._trade_buffer.clear()
        self._last_flush = datetime.utcnow()

        try:
            from ..database.models import RawFill

            async with self.db.session() as session:
                for trade in trades:
                    fill = RawFill(
                        id=trade.id,
                        market_order_id=trade.market_order_id,
                        bucket_index=trade.bucket_index,
                        maker=trade.maker,
                        taker=trade.taker,
                        side=trade.side,
                        outcome=trade.outcome,
                        price=trade.price,
                        size=trade.size,
                        token_id=trade.token_id,
                        condition_id=trade.condition_id,
                        timestamp=trade.timestamp,
                        tx_hash=trade.tx_hash,
                        block_number=trade.block_number,
                        raw_json=json.dumps(trade.raw_json),
                        processed=False,
                    )
                    await session.merge(fill)

                await session.commit()

            self._trades_stored += len(trades)
            logger.debug(f"Flushed {len(trades)} trades to database")

        except Exception as e:
            logger.error(f"Failed to flush trades: {e}")
            # Put trades back in buffer
            self._trade_buffer.extend(trades)

    async def start(self, token_ids: Optional[List[str]] = None):
        """Start the feed manager"""
        # Subscribe to tokens
        if token_ids:
            for token_id in token_ids:
                await self.feed.subscribe_token(token_id)

        # Start periodic flush task
        asyncio.create_task(self._periodic_flush())

        # Connect to WebSocket
        await self.feed.connect()

    async def stop(self):
        """Stop the feed manager"""
        await self.feed.disconnect()
        await self._flush_buffer()

    async def _periodic_flush(self):
        """Periodically flush the trade buffer"""
        while self.feed._running:
            await asyncio.sleep(self._flush_interval)

            elapsed = (datetime.utcnow() - self._last_flush).total_seconds()
            if elapsed >= self._flush_interval and self._trade_buffer:
                await self._flush_buffer()

    def get_stats(self) -> dict:
        """Get manager statistics"""
        return {
            "feed": self.feed.get_stats(),
            "buffer_size": len(self._trade_buffer),
            "trades_stored": self._trades_stored,
        }
