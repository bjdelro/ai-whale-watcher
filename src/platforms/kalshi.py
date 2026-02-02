"""
Kalshi platform client - REST API + WebSocket
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


class KalshiClient(BasePlatform):
    """
    Kalshi client for monitoring whale trades.

    Uses:
    - REST API for markets and trade history
    - WebSocket for real-time trade stream
    """

    def __init__(self, config: dict):
        super().__init__(config)

        self.rest_url = config.get('rest_url', 'https://trading-api.kalshi.com/trade-api/v2')
        self.ws_url = config.get('websocket_url', 'wss://api.elections.kalshi.com/trade-api/ws/v2')
        self.polling_interval = config.get('polling_interval', 120)

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._should_run = False

        # Track last seen trade for polling
        self._last_trade_id: Optional[str] = None
        self._last_cursor: Optional[str] = None

        # Market cache
        self._markets_cache: dict[str, MarketInfo] = {}
        self._markets_cache_time: Optional[datetime] = None
        self._cache_duration = timedelta(minutes=5)

        # Message ID for WebSocket
        self._msg_id = 0

    @property
    def platform(self) -> Platform:
        return Platform.KALSHI

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

    def _next_msg_id(self) -> int:
        """Get next WebSocket message ID"""
        self._msg_id += 1
        return self._msg_id

    # ==================== REST API Methods ====================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def _request(
        self,
        endpoint: str,
        params: Optional[dict] = None
    ) -> Any:
        """Make HTTP request with retry logic"""
        session = await self._get_session()
        url = f"{self.rest_url}{endpoint}"

        async with session.get(url, params=params) as response:
            if response.status == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                logger.warning(f"Kalshi rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                raise Exception("Rate limited")

            if response.status == 404:
                return None

            response.raise_for_status()
            return await response.json()

    async def get_markets(self) -> list[MarketInfo]:
        """Fetch all active markets"""
        # Check cache
        if (self._markets_cache_time and
            datetime.utcnow() - self._markets_cache_time < self._cache_duration):
            return list(self._markets_cache.values())

        try:
            markets = []
            cursor = None

            # Paginate through all markets
            while True:
                params = {
                    'limit': 200,
                    'status': 'open'
                }
                if cursor:
                    params['cursor'] = cursor

                data = await self._request('/markets', params)

                if not data or 'markets' not in data:
                    break

                for market in data['markets']:
                    market_info = self._parse_market(market)
                    if market_info:
                        markets.append(market_info)
                        self._markets_cache[market_info.id] = market_info

                cursor = data.get('cursor')
                if not cursor:
                    break

            self._markets_cache_time = datetime.utcnow()
            logger.info(f"Fetched {len(markets)} active Kalshi markets")
            return markets

        except Exception as e:
            logger.error(f"Failed to fetch Kalshi markets: {e}")
            return list(self._markets_cache.values())

    def _parse_market(self, market: dict) -> Optional[MarketInfo]:
        """Parse market data from API response"""
        try:
            # Parse close time
            close_time = None
            expiration = market.get('close_time') or market.get('expiration_time')
            if expiration:
                try:
                    close_time = datetime.fromisoformat(expiration.replace('Z', '+00:00'))
                except:
                    pass

            return MarketInfo(
                id=market.get('ticker'),
                platform=Platform.KALSHI,
                name=market.get('title') or market.get('ticker'),
                description=market.get('subtitle'),
                category=market.get('category'),
                volume_24h=float(market.get('volume_24h', 0) or 0),
                liquidity=float(market.get('open_interest', 0) or 0),
                close_time=close_time,
                is_active=market.get('status') == 'open',
                metadata={
                    'event_ticker': market.get('event_ticker'),
                    'yes_bid': market.get('yes_bid'),
                    'yes_ask': market.get('yes_ask'),
                    'last_price': market.get('last_price')
                }
            )
        except Exception as e:
            logger.debug(f"Failed to parse Kalshi market: {e}")
            return None

    async def get_market(self, market_id: str) -> Optional[MarketInfo]:
        """Fetch specific market info"""
        if market_id in self._markets_cache:
            return self._markets_cache[market_id]

        try:
            data = await self._request(f'/markets/{market_id}')
            if data and 'market' in data:
                market = self._parse_market(data['market'])
                if market:
                    self._markets_cache[market.id] = market
                    return market
        except Exception as e:
            logger.debug(f"Failed to fetch market {market_id}: {e}")

        return None

    async def get_recent_trades(
        self,
        market_id: Optional[str] = None,
        limit: int = 100
    ) -> list[TradeEvent]:
        """Fetch recent trades from Kalshi API"""
        trades = []

        try:
            if market_id:
                # Get trades for specific market
                data = await self._request(
                    f'/markets/{market_id}/trades',
                    params={'limit': limit}
                )

                if data and 'trades' in data:
                    for trade_data in data['trades']:
                        trade = self._parse_trade(trade_data, market_id)
                        if trade:
                            trades.append(trade)
            else:
                # Get trades across top markets
                markets = await self.get_markets()

                # Sort by volume and get top active markets
                active_markets = sorted(
                    [m for m in markets if m.is_active],
                    key=lambda x: x.volume_24h,
                    reverse=True
                )[:30]

                for market in active_markets:
                    try:
                        market_trades = await self.get_recent_trades(
                            market.id,
                            limit=20
                        )
                        trades.extend(market_trades)
                    except Exception as e:
                        logger.debug(f"Error fetching trades for {market.id}: {e}")

                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.1)

            logger.debug(f"Fetched {len(trades)} trades from Kalshi")

        except Exception as e:
            logger.error(f"Failed to fetch Kalshi trades: {e}")

        return trades

    def _parse_trade(
        self,
        trade_data: dict,
        market_id: str
    ) -> Optional[TradeEvent]:
        """Parse trade from API response"""
        try:
            # Get market info
            market = self._markets_cache.get(market_id)
            market_name = market.name if market else market_id

            # Kalshi trades are in cents, convert to USD
            count = int(trade_data.get('count', 0))
            price = float(trade_data.get('yes_price', 50)) / 100  # cents to decimal
            amount_usd = count * price  # Each contract is ~$1 max

            # Skip small trades
            if amount_usd < 100:
                return None

            # Determine trade type
            taker_side = trade_data.get('taker_side', 'yes').lower()
            trade_type = TradeType.BUY if taker_side == 'yes' else TradeType.SELL

            # Parse timestamp
            timestamp = datetime.utcnow()
            created_time = trade_data.get('created_time')
            if created_time:
                try:
                    timestamp = datetime.fromisoformat(created_time.replace('Z', '+00:00'))
                except:
                    pass

            return TradeEvent(
                platform=Platform.KALSHI,
                wallet_address=trade_data.get('taker_member_id', 'anonymous'),
                market_id=market_id,
                market_name=market_name,
                trade_type=trade_type,
                amount_usd=amount_usd,
                price=price,
                shares=float(count),
                outcome='Yes' if taker_side == 'yes' else 'No',
                timestamp=timestamp,
                external_id=trade_data.get('trade_id'),
                raw_data=trade_data
            )

        except Exception as e:
            logger.debug(f"Failed to parse Kalshi trade: {e}")
            return None

    # ==================== WebSocket Methods ====================

    async def connect(self):
        """Connect to Kalshi WebSocket"""
        if self._is_connected:
            return

        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10
            )
            self._is_connected = True
            logger.info("Connected to Kalshi WebSocket")

        except Exception as e:
            logger.error(f"Failed to connect to Kalshi WebSocket: {e}")
            raise

    async def disconnect(self):
        """Disconnect from WebSocket"""
        self._should_run = False

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._is_connected = False
        logger.info("Disconnected from Kalshi WebSocket")

    async def start_listening(self):
        """Start listening for trade events"""
        self._should_run = True

        while self._should_run:
            try:
                if not self._is_connected:
                    await self.connect()

                # Subscribe to trade channel
                await self._subscribe_to_trades()

                # Listen for messages
                async for message in self._ws:
                    if not self._should_run:
                        break

                    await self._handle_ws_message(message)

            except ConnectionClosed as e:
                logger.warning(f"Kalshi WebSocket closed: {e}")
                self._is_connected = False

                if self._should_run:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Kalshi WebSocket error: {e}")
                self._is_connected = False

                if self._should_run:
                    await asyncio.sleep(5)

    async def _subscribe_to_trades(self):
        """Subscribe to trade updates"""
        # Get top markets to subscribe to
        markets = await self.get_markets()
        top_markets = sorted(
            [m for m in markets if m.is_active],
            key=lambda x: x.volume_24h,
            reverse=True
        )[:50]

        # Subscribe to trade channel
        subscribe_msg = {
            "id": self._next_msg_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["trade"],
                "market_tickers": [m.id for m in top_markets]
            }
        }

        await self._ws.send(json.dumps(subscribe_msg))
        logger.debug(f"Subscribed to {len(top_markets)} Kalshi markets")

    async def _handle_ws_message(self, message: str):
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)

            # Handle different message types
            msg_type = data.get('type', '')

            if msg_type == 'trade':
                trade = self._parse_ws_trade(data)
                if trade:
                    await self._emit_trade(trade)

            elif msg_type == 'subscribed':
                logger.debug("Kalshi subscription confirmed")

            elif msg_type == 'error':
                logger.error(f"Kalshi WS error: {data.get('msg')}")

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from Kalshi: {message[:100]}")
        except Exception as e:
            logger.error(f"Error handling Kalshi message: {e}")

    def _parse_ws_trade(self, data: dict) -> Optional[TradeEvent]:
        """Parse trade from WebSocket message"""
        try:
            msg = data.get('msg', {})
            market_id = msg.get('market_ticker')

            # Get market info
            market = self._markets_cache.get(market_id)
            market_name = market.name if market else market_id

            # Parse trade details
            count = int(msg.get('count', 0))
            price = float(msg.get('yes_price', 50)) / 100
            amount_usd = count * price

            # Skip small trades
            if amount_usd < 100:
                return None

            taker_side = msg.get('taker_side', 'yes').lower()

            return TradeEvent(
                platform=Platform.KALSHI,
                wallet_address=msg.get('taker_member_id', 'anonymous'),
                market_id=market_id,
                market_name=market_name,
                trade_type=TradeType.BUY if taker_side == 'yes' else TradeType.SELL,
                amount_usd=amount_usd,
                price=price,
                shares=float(count),
                outcome='Yes' if taker_side == 'yes' else 'No',
                timestamp=datetime.utcnow(),
                external_id=msg.get('trade_id'),
                raw_data=data
            )

        except Exception as e:
            logger.debug(f"Failed to parse Kalshi WS trade: {e}")
            return None

    # ==================== Polling Fallback ====================

    async def poll_trades(self):
        """Poll for new trades as WebSocket fallback"""
        logger.info("Starting Kalshi trade polling")

        while self._should_run:
            try:
                trades = await self.get_recent_trades(limit=50)

                for trade in trades:
                    # Skip if we've seen this trade before
                    if (self._last_trade_id and
                        trade.external_id == self._last_trade_id):
                        continue

                    await self._emit_trade(trade)

                if trades:
                    self._last_trade_id = trades[0].external_id

            except Exception as e:
                logger.error(f"Kalshi polling error: {e}")

            await asyncio.sleep(self.polling_interval)
