"""
Polymarket platform client - REST API + WebSocket
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional, Any
import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from src.database.models import Platform, TradeType
from .base import BasePlatform, TradeEvent, MarketInfo


class PolymarketClient(BasePlatform):
    """
    Polymarket client for monitoring whale trades.

    Uses:
    - CLOB API for trade data
    - Gamma API for market metadata
    - WebSocket for real-time trade stream
    """

    def __init__(self, config: dict):
        super().__init__(config)

        self.clob_url = config.get('clob_url', 'https://clob.polymarket.com')
        self.gamma_url = config.get('gamma_url', 'https://gamma-api.polymarket.com')
        self.ws_url = config.get('websocket_url', 'wss://ws-subscriptions-clob.polymarket.com/ws/market')
        self.polling_interval = config.get('polling_interval', 60)

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._should_run = False

        # Track last seen trade for polling
        self._last_trade_timestamp: Optional[datetime] = None

        # Market cache
        self._markets_cache: dict[str, MarketInfo] = {}
        self._markets_cache_time: Optional[datetime] = None
        self._cache_duration = timedelta(minutes=5)

    @property
    def platform(self) -> Platform:
        return Platform.POLYMARKET

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    'Accept': 'application/json',
                    'User-Agent': 'WhaleWatcher/1.0'
                }
            )
        return self._session

    async def close(self):
        """Close all connections"""
        await self.disconnect()
        if self._session and not self._session.closed:
            await self._session.close()

    # ==================== REST API Methods ====================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def _request(self, url: str, params: Optional[dict] = None) -> Any:
        """Make HTTP request with retry logic"""
        session = await self._get_session()
        async with session.get(url, params=params) as response:
            if response.status == 429:
                # Rate limited
                retry_after = int(response.headers.get('Retry-After', 60))
                logger.warning(f"Rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                raise Exception("Rate limited")

            response.raise_for_status()
            return await response.json()

    async def get_markets(self) -> list[MarketInfo]:
        """Fetch all active markets from Gamma API"""
        # Check cache
        if (self._markets_cache_time and
            datetime.utcnow() - self._markets_cache_time < self._cache_duration):
            return list(self._markets_cache.values())

        try:
            # Fetch events from Gamma API
            data = await self._request(f"{self.gamma_url}/events", params={
                'active': 'true',
                'closed': 'false'
            })

            markets = []
            for event in data:
                for market in event.get('markets', []):
                    market_info = self._parse_gamma_market(market, event)
                    if market_info:
                        markets.append(market_info)
                        self._markets_cache[market_info.id] = market_info

            self._markets_cache_time = datetime.utcnow()
            logger.info(f"Fetched {len(markets)} active Polymarket markets")
            return markets

        except Exception as e:
            logger.error(f"Failed to fetch Polymarket markets: {e}")
            return list(self._markets_cache.values())

    def _parse_gamma_market(self, market: dict, event: dict) -> Optional[MarketInfo]:
        """Parse market data from Gamma API response"""
        try:
            # Parse close time
            close_time = None
            end_date = market.get('endDate') or event.get('endDate')
            if end_date:
                try:
                    close_time = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                except:
                    pass

            return MarketInfo(
                id=market.get('conditionId') or market.get('id'),
                platform=Platform.POLYMARKET,
                name=market.get('question') or event.get('title', 'Unknown'),
                description=market.get('description'),
                category=event.get('category'),
                volume_24h=float(market.get('volume24hr', 0) or 0),
                liquidity=float(market.get('liquidity', 0) or 0),
                close_time=close_time,
                is_active=market.get('active', True),
                metadata={
                    'event_id': event.get('id'),
                    'event_slug': event.get('slug'),
                    'clob_token_ids': market.get('clobTokenIds', []),
                    'outcomes': market.get('outcomes', ['Yes', 'No'])
                }
            )
        except Exception as e:
            logger.debug(f"Failed to parse market: {e}")
            return None

    async def get_market(self, market_id: str) -> Optional[MarketInfo]:
        """Fetch specific market info"""
        if market_id in self._markets_cache:
            return self._markets_cache[market_id]

        # Refresh cache
        await self.get_markets()
        return self._markets_cache.get(market_id)

    async def get_recent_trades(
        self,
        market_id: Optional[str] = None,
        limit: int = 100
    ) -> list[TradeEvent]:
        """
        Fetch recent trades from CLOB API.

        Note: The public trades endpoint may have limitations.
        We'll use the activity feed approach.
        """
        trades = []

        try:
            # Get markets to iterate through
            if market_id:
                markets = [await self.get_market(market_id)]
                markets = [m for m in markets if m]
            else:
                markets = await self.get_markets()

            # For each market, try to get recent activity
            # The CLOB API's trade endpoints are limited
            # In production, you'd use the blockchain indexer or a subgraph

            for market in markets[:20]:  # Limit to avoid too many requests
                try:
                    # Try to get price history as a proxy for activity
                    token_ids = market.metadata.get('clob_token_ids', [])
                    if not token_ids:
                        continue

                    # Query price endpoint to detect activity
                    # Real implementation would use the GraphQL subgraph
                    # or blockchain event logs

                except Exception as e:
                    logger.debug(f"Error fetching trades for {market.id}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to fetch Polymarket trades: {e}")

        return trades

    # ==================== WebSocket Methods ====================

    async def connect(self):
        """Connect to Polymarket WebSocket"""
        if self._is_connected:
            return

        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10
            )
            self._is_connected = True
            logger.info("Connected to Polymarket WebSocket")

        except Exception as e:
            logger.error(f"Failed to connect to Polymarket WebSocket: {e}")
            raise

    async def disconnect(self):
        """Disconnect from WebSocket"""
        self._should_run = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._is_connected = False
        logger.info("Disconnected from Polymarket WebSocket")

    async def start_listening(self):
        """Start listening for trade events"""
        self._should_run = True

        while self._should_run:
            try:
                if not self._is_connected:
                    await self.connect()

                # Subscribe to market channel
                await self._subscribe_to_markets()

                # Listen for messages
                async for message in self._ws:
                    if not self._should_run:
                        break

                    await self._handle_ws_message(message)

            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                self._is_connected = False

                if self._should_run:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                self._is_connected = False

                if self._should_run:
                    await asyncio.sleep(5)

    async def _subscribe_to_markets(self):
        """Subscribe to market updates"""
        # Subscribe to the market channel for all markets
        # The exact subscription format depends on Polymarket's WS API
        subscribe_msg = {
            "type": "subscribe",
            "channel": "market",
            "markets": []  # Empty for all markets
        }

        await self._ws.send(json.dumps(subscribe_msg))
        logger.debug("Subscribed to Polymarket market channel")

    async def _handle_ws_message(self, message: str):
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)
            msg_type = data.get('type', data.get('event_type', ''))

            # Handle different message types
            if msg_type in ['trade', 'fill', 'order_filled']:
                trade = self._parse_ws_trade(data)
                if trade:
                    await self._emit_trade(trade)

            elif msg_type == 'price_change':
                # Could use this to detect activity
                pass

            elif msg_type in ['subscribed', 'pong', 'heartbeat']:
                # Acknowledgment messages
                pass

            else:
                logger.debug(f"Unknown WS message type: {msg_type}")

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from WebSocket: {message[:100]}")
        except Exception as e:
            logger.error(f"Error handling WS message: {e}")

    def _parse_ws_trade(self, data: dict) -> Optional[TradeEvent]:
        """Parse trade event from WebSocket message"""
        try:
            # Extract trade details
            # The exact format depends on Polymarket's WS API
            asset_id = data.get('asset_id') or data.get('market')
            maker = data.get('maker') or data.get('taker')
            side = data.get('side', 'buy').lower()

            # Get amount in USD
            price = float(data.get('price', 0))
            size = float(data.get('size', 0))
            amount_usd = price * size

            # Skip small trades
            if amount_usd < 1000:  # Minimum $1k to track
                return None

            # Get market name
            market_name = data.get('market_name', asset_id or 'Unknown Market')

            return TradeEvent(
                platform=Platform.POLYMARKET,
                wallet_address=maker or 'unknown',
                market_id=asset_id or 'unknown',
                market_name=market_name,
                trade_type=TradeType.BUY if side == 'buy' else TradeType.SELL,
                amount_usd=amount_usd,
                price=price,
                shares=size,
                outcome=data.get('outcome'),
                timestamp=datetime.utcnow(),
                external_id=data.get('id'),
                tx_hash=data.get('tx_hash'),
                raw_data=data
            )

        except Exception as e:
            logger.debug(f"Failed to parse trade: {e}")
            return None

    # ==================== Polling Fallback ====================

    async def poll_trades(self):
        """Poll for new trades as WebSocket fallback"""
        logger.info("Starting Polymarket trade polling")

        while self._should_run:
            try:
                trades = await self.get_recent_trades(limit=50)

                for trade in trades:
                    # Skip if we've seen this trade before
                    if (self._last_trade_timestamp and
                        trade.timestamp <= self._last_trade_timestamp):
                        continue

                    await self._emit_trade(trade)
                    self._last_trade_timestamp = trade.timestamp

            except Exception as e:
                logger.error(f"Polling error: {e}")

            await asyncio.sleep(self.polling_interval)
