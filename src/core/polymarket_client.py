"""
Unified Polymarket API client for the whale watcher system.
Handles all interactions with Polymarket CLOB and Gamma APIs.

Uses py-clob-client SDK for authenticated endpoints (trades).
Uses aiohttp for public endpoints (markets, orderbooks).
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
import aiohttp
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

# Import py-clob-client for authenticated endpoints
try:
    from py_clob_client.client import ClobClient as SyncClobClient
    from py_clob_client.clob_types import TradeParams
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    SyncClobClient = None
    TradeParams = None

logger = logging.getLogger(__name__)


@dataclass
class TradeData:
    """Standardized trade data from Polymarket API"""
    id: str
    market_order_id: Optional[str]
    bucket_index: Optional[int]
    maker: str
    taker: str
    side: str  # BUY or SELL
    outcome: Optional[str]  # YES or NO
    price: float
    size: float
    token_id: str
    condition_id: Optional[str]
    timestamp: datetime
    tx_hash: Optional[str]
    block_number: Optional[int]
    raw_json: dict


@dataclass
class OrderbookData:
    """Standardized orderbook data from Polymarket API"""
    token_id: str
    timestamp: datetime
    bids: List[Dict[str, float]]  # [{price, size}, ...]
    asks: List[Dict[str, float]]  # [{price, size}, ...]
    mid_price: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread_bps: Optional[float]


@dataclass
class MarketData:
    """Standardized market data from Gamma API"""
    condition_id: str
    question: str
    description: Optional[str]
    category: Optional[str]
    slug: Optional[str]
    token_id_yes: Optional[str]
    token_id_no: Optional[str]
    end_date: Optional[datetime]
    created_date: Optional[datetime]
    total_volume: float
    volume_24h: float
    liquidity: float
    is_active: bool
    resolution: Optional[str]


class PolymarketClient:
    """
    Unified client for Polymarket APIs.

    Endpoints:
    - CLOB API: https://clob.polymarket.com
    - Gamma API: https://gamma-api.polymarket.com
    """

    CLOB_BASE_URL = "https://clob.polymarket.com"
    GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

    # Rate limiting
    MAX_REQUESTS_PER_SECOND = 10

    def __init__(
        self,
        private_key: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        """
        Initialize the Polymarket client.

        Args:
            private_key: Optional private key for authenticated endpoints
            session: Optional aiohttp session (created if not provided)
        """
        self.private_key = private_key or os.getenv("PRIVATE_KEY")
        self._session = session
        self._owns_session = session is None
        self._request_semaphore = asyncio.Semaphore(self.MAX_REQUESTS_PER_SECOND)
        self._last_request_time = 0.0

        # Sync CLOB client for authenticated endpoints
        self._sync_clob_client: Optional[SyncClobClient] = None
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._auth_initialized = False

    def _init_sync_client(self):
        """Initialize the sync CLOB client with authentication"""
        if not HAS_CLOB_CLIENT:
            logger.warning("py-clob-client not installed, authenticated endpoints unavailable")
            return

        if not self.private_key:
            logger.warning("No private key provided, authenticated endpoints unavailable")
            return

        if self._sync_clob_client is not None:
            return

        try:
            self._sync_clob_client = SyncClobClient(
                host=self.CLOB_BASE_URL,
                key=self.private_key,
                chain_id=137,  # Polygon mainnet
            )
            # Derive API credentials
            self._sync_clob_client.set_api_creds(
                self._sync_clob_client.create_or_derive_api_creds()
            )
            self._auth_initialized = True
            logger.info("CLOB client authenticated successfully")
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            self._sync_clob_client = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Accept": "application/json"}
            )
            self._owns_session = True
        return self._session

    async def close(self):
        """Close the session if we own it"""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        retries: int = 3,
    ) -> Optional[Dict]:
        """
        Make a rate-limited request with retries.
        """
        session = await self._get_session()

        for attempt in range(retries):
            try:
                async with self._request_semaphore:
                    # Rate limiting
                    now = asyncio.get_event_loop().time()
                    time_since_last = now - self._last_request_time
                    if time_since_last < 0.1:  # Min 100ms between requests
                        await asyncio.sleep(0.1 - time_since_last)

                    self._last_request_time = asyncio.get_event_loop().time()

                    async with session.request(
                        method, url, params=params, json=json_data
                    ) as response:
                        if response.status == 429:
                            # Rate limited - exponential backoff
                            wait_time = 2 ** attempt
                            logger.warning(f"Rate limited, waiting {wait_time}s")
                            await asyncio.sleep(wait_time)
                            continue

                        if response.status >= 400:
                            text = await response.text()
                            logger.error(f"API error {response.status}: {text[:200]}")
                            if attempt < retries - 1:
                                await asyncio.sleep(1)
                                continue
                            return None

                        return await response.json()

            except asyncio.TimeoutError:
                logger.warning(f"Request timeout (attempt {attempt + 1}/{retries})")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Request error: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)

        return None

    # =========================================================================
    # CLOB API - Trades (Authenticated)
    # =========================================================================

    async def get_trades(
        self,
        token_id: Optional[str] = None,
        maker: Optional[str] = None,
        limit: int = 100,
        before: Optional[str] = None,
    ) -> List[TradeData]:
        """
        Fetch recent trades from the CLOB API using authenticated SDK.

        Args:
            token_id: Filter by token (market outcome)
            maker: Filter by maker address
            limit: Max number of trades to return
            before: Cursor for pagination (not used with SDK)

        Returns:
            List of TradeData objects
        """
        # Initialize auth client if needed
        if not self._auth_initialized:
            self._init_sync_client()

        if not self._sync_clob_client:
            logger.warning("Cannot fetch trades: CLOB client not initialized")
            return []

        # Use ThreadPoolExecutor to run sync SDK method
        loop = asyncio.get_event_loop()

        def fetch_trades_sync():
            try:
                params = TradeParams(
                    asset_id=token_id,
                    maker_address=maker,
                )
                return self._sync_clob_client.get_trades(params=params)
            except Exception as e:
                logger.error(f"Error fetching trades: {e}")
                return []

        try:
            data = await loop.run_in_executor(self._executor, fetch_trades_sync)
        except Exception as e:
            logger.error(f"Executor error: {e}")
            return []

        if not data:
            return []

        trades = []
        items = data if isinstance(data, list) else data.get("data", [])
        for item in items[:limit]:
            try:
                trades.append(self._parse_trade(item))
            except Exception as e:
                logger.warning(f"Failed to parse trade: {e}")

        return trades

    async def get_activity(
        self,
        wallet: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TradeData]:
        """
        Fetch wallet activity history.

        Args:
            wallet: Wallet address
            limit: Max number of activities
            offset: Pagination offset

        Returns:
            List of TradeData objects
        """
        params = {"user": wallet, "limit": limit, "offset": offset}

        data = await self._request(
            "GET", f"{self.CLOB_BASE_URL}/activity", params=params
        )

        if not data:
            return []

        trades = []
        items = data if isinstance(data, list) else data.get("data", [])
        for item in items:
            try:
                # Activity endpoint may have different format
                if "trades" in item:
                    for trade in item["trades"]:
                        trades.append(self._parse_trade(trade))
                else:
                    trades.append(self._parse_trade(item))
            except Exception as e:
                logger.warning(f"Failed to parse activity: {e}")

        return trades

    def _parse_trade(self, data: dict) -> TradeData:
        """Parse raw trade data into TradeData object"""
        # Handle various timestamp formats
        timestamp = data.get("timestamp") or data.get("match_time") or data.get("created_at")
        if isinstance(timestamp, str):
            # Try ISO format first
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except:
                # Try Unix timestamp
                ts = datetime.fromtimestamp(float(timestamp))
        elif isinstance(timestamp, (int, float)):
            # Unix timestamp (might be in ms)
            if timestamp > 1e12:
                timestamp = timestamp / 1000
            ts = datetime.fromtimestamp(timestamp)
        else:
            ts = datetime.utcnow()

        return TradeData(
            id=str(data.get("id") or data.get("match_id") or ""),
            market_order_id=data.get("market_order_id") or data.get("order_id"),
            bucket_index=data.get("bucket_index"),
            maker=data.get("maker") or data.get("maker_address") or "",
            taker=data.get("taker") or data.get("taker_address") or "",
            side=data.get("side", "").upper() or ("BUY" if data.get("is_buy") else "SELL"),
            outcome=data.get("outcome"),
            price=float(data.get("price", 0)),
            size=float(data.get("size") or data.get("amount") or 0),
            token_id=data.get("token_id") or data.get("asset_id") or "",
            condition_id=data.get("condition_id"),
            timestamp=ts,
            tx_hash=data.get("transaction_hash") or data.get("tx_hash"),
            block_number=data.get("block_number"),
            raw_json=data,
        )

    # =========================================================================
    # CLOB API - Orderbook
    # =========================================================================

    async def get_orderbook(self, token_id: str) -> Optional[OrderbookData]:
        """
        Fetch the current orderbook for a token.

        Args:
            token_id: The token ID to get orderbook for

        Returns:
            OrderbookData object or None
        """
        params = {"token_id": token_id}

        data = await self._request("GET", f"{self.CLOB_BASE_URL}/book", params=params)

        if not data:
            return None

        return self._parse_orderbook(token_id, data)

    def _parse_orderbook(self, token_id: str, data: dict) -> OrderbookData:
        """Parse raw orderbook data"""
        bids = []
        asks = []

        # Parse bids
        for bid in data.get("bids", []):
            bids.append({
                "price": float(bid.get("price", 0)),
                "size": float(bid.get("size", 0)),
            })

        # Parse asks
        for ask in data.get("asks", []):
            asks.append({
                "price": float(ask.get("price", 0)),
                "size": float(ask.get("size", 0)),
            })

        # Sort by price
        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])

        # Calculate derived values
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None

        if best_bid and best_ask:
            mid_price = (best_bid + best_ask) / 2
            spread_bps = ((best_ask - best_bid) / mid_price) * 10000
        else:
            mid_price = best_bid or best_ask or 0.5
            spread_bps = None

        return OrderbookData(
            token_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
            mid_price=mid_price,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
        )

    # =========================================================================
    # CLOB API - Prices
    # =========================================================================

    async def get_price(self, token_id: str) -> Optional[float]:
        """
        Get the current mid price for a token.

        Args:
            token_id: The token ID

        Returns:
            Mid price or None
        """
        book = await self.get_orderbook(token_id)
        return book.mid_price if book else None

    async def get_prices_history(
        self,
        token_id: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        interval: str = "1h",
    ) -> List[Dict]:
        """
        Get historical prices for a token.

        Args:
            token_id: The token ID
            start_ts: Start timestamp (Unix)
            end_ts: End timestamp (Unix)
            interval: Price interval (1m, 5m, 1h, 1d)

        Returns:
            List of price candles
        """
        params = {"token_id": token_id, "interval": interval}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts

        data = await self._request(
            "GET", f"{self.CLOB_BASE_URL}/prices-history", params=params
        )

        return data.get("history", []) if data else []

    # =========================================================================
    # Gamma API - Markets
    # =========================================================================

    async def get_markets(
        self,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
        category: Optional[str] = None,
    ) -> List[MarketData]:
        """
        Fetch markets from Gamma API.

        Args:
            closed: Include closed markets
            limit: Max markets to return
            offset: Pagination offset
            category: Filter by category

        Returns:
            List of MarketData objects
        """
        params = {
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        if category:
            params["category"] = category

        data = await self._request("GET", f"{self.GAMMA_BASE_URL}/markets", params=params)

        if not data:
            return []

        markets = []
        items = data if isinstance(data, list) else data.get("data", [])
        for item in items:
            try:
                markets.append(self._parse_market(item))
            except Exception as e:
                logger.warning(f"Failed to parse market: {e}")

        return markets

    async def get_market(self, condition_id: str) -> Optional[MarketData]:
        """
        Fetch a single market by condition ID.

        Args:
            condition_id: The market condition ID

        Returns:
            MarketData or None
        """
        data = await self._request(
            "GET", f"{self.GAMMA_BASE_URL}/markets/{condition_id}"
        )

        if not data:
            return None

        return self._parse_market(data)

    def _parse_market(self, data: dict) -> MarketData:
        """Parse raw market data into MarketData object"""
        # Parse dates
        end_date = None
        if data.get("end_date") or data.get("end_date_iso"):
            try:
                date_str = data.get("end_date_iso") or data.get("end_date")
                end_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except:
                pass

        created_date = None
        if data.get("created_at"):
            try:
                created_date = datetime.fromisoformat(
                    data["created_at"].replace("Z", "+00:00")
                )
            except:
                pass

        # Get token IDs from outcomes
        token_id_yes = None
        token_id_no = None

        # Try "tokens" format first (from some endpoints)
        if "tokens" in data:
            for token in data["tokens"]:
                if token.get("outcome") == "Yes":
                    token_id_yes = token.get("token_id")
                elif token.get("outcome") == "No":
                    token_id_no = token.get("token_id")

        # Try "clobTokenIds" format (from Gamma API /markets endpoint)
        if not token_id_yes and "clobTokenIds" in data:
            clob_tokens = data["clobTokenIds"]
            # clobTokenIds can be a JSON string or list
            if isinstance(clob_tokens, str):
                try:
                    import json
                    clob_tokens = json.loads(clob_tokens)
                except:
                    clob_tokens = []

            outcomes = data.get("outcomes", ["Yes", "No"])
            if isinstance(outcomes, str):
                try:
                    import json
                    outcomes = json.loads(outcomes)
                except:
                    outcomes = ["Yes", "No"]

            if isinstance(clob_tokens, list) and len(clob_tokens) >= 2:
                # Map by outcome order (usually Yes first, No second)
                for i, outcome in enumerate(outcomes):
                    if i < len(clob_tokens):
                        if outcome.lower() == "yes":
                            token_id_yes = clob_tokens[i]
                        elif outcome.lower() == "no":
                            token_id_no = clob_tokens[i]

        return MarketData(
            condition_id=data.get("condition_id") or data.get("conditionId") or data.get("id") or "",
            question=data.get("question") or data.get("title") or "",
            description=data.get("description"),
            category=data.get("category") or data.get("tags", [None])[0] if data.get("tags") else None,
            slug=data.get("slug"),
            token_id_yes=token_id_yes or data.get("token_id_yes"),
            token_id_no=token_id_no or data.get("token_id_no"),
            end_date=end_date,
            created_date=created_date,
            total_volume=float(data.get("volume") or data.get("volumeNum") or data.get("total_volume") or 0),
            volume_24h=float(data.get("volume_24h") or data.get("volume24hr") or 0),
            liquidity=float(data.get("liquidity") or data.get("liquidityNum") or 0),
            is_active=data.get("active", True) and not data.get("closed", False) and not data.get("resolved", False),
            resolution=data.get("resolution") or data.get("winning_outcome"),
        )

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def get_active_token_ids(self, limit: int = 100) -> List[str]:
        """
        Get token IDs for all active markets from CLOB API.

        Args:
            limit: Max markets to fetch

        Returns:
            List of token IDs
        """
        # Fetch from CLOB API which includes token IDs directly
        data = await self._request("GET", f"{self.CLOB_BASE_URL}/markets")

        if not data:
            return []

        token_ids = []
        items = data if isinstance(data, list) else data.get("data", [])

        for market in items[:limit]:
            if not market.get("active", False):
                continue

            tokens = market.get("tokens", [])
            for token in tokens:
                token_id = token.get("token_id")
                if token_id:
                    token_ids.append(token_id)

        return token_ids

    async def get_clob_markets(self, active_only: bool = True, limit: int = 100) -> List[dict]:
        """
        Get markets from CLOB API with full token information.

        Args:
            active_only: Only return active markets
            limit: Max markets to return

        Returns:
            List of market dicts with token IDs
        """
        data = await self._request("GET", f"{self.CLOB_BASE_URL}/markets")

        if not data:
            return []

        items = data if isinstance(data, list) else data.get("data", [])

        if active_only:
            items = [m for m in items if m.get("active", False)]

        return items[:limit]
