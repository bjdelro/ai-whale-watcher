"""
RTDS WebSocket feed for real-time whale trade detection.

Connects to Polymarket's RTDS (Real-Time Data Service) at
wss://ws-live-data.polymarket.com and receives ALL platform trades.
Filters client-side by whale proxyWallet address and routes matched
trades to an asyncio.Queue for the orchestrator to process.

Key differences from the CLOB WebSocket (websocket_feed.py):
- URL: wss://ws-live-data.polymarket.com (not clob subscriptions)
- No authentication required
- Global firehose subscription (activity topic, type=*)
- Literal "PING" keepalive every 5 seconds
- Trade data nested in event["payload"]
- Both sides of a trade fire — dedup by (txHash, proxyWallet)
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL = 5       # seconds between PING messages
DEDUP_WINDOW = 300      # seconds to keep dedup entries
DEDUP_MAX_SIZE = 50_000  # max entries before cleanup


class RTDSFeed:
    """
    Real-time trade feed via Polymarket RTDS WebSocket.

    Receives all platform trades, filters by whale address set,
    and routes whale trades and all trades to separate queues.
    """

    def __init__(
        self,
        whale_addresses: Set[str],
        whale_trade_queue: asyncio.Queue,
        activity_queue: Optional[asyncio.Queue] = None,
        reconnect_delay: float = 3.0,
        max_reconnect_attempts: int = 1000,
    ):
        """
        Args:
            whale_addresses: Set of lowercased proxyWallet addresses to match.
            whale_trade_queue: Queue for whale-matched trades.
            activity_queue: Optional queue for ALL trades (unusual activity).
                           If None, non-whale trades are discarded.
            reconnect_delay: Base delay between reconnection attempts.
            max_reconnect_attempts: Max attempts before giving up.
        """
        self._whale_addresses = whale_addresses
        self._whale_trade_queue = whale_trade_queue
        self._activity_queue = activity_queue
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_attempts = max_reconnect_attempts

        # Connection state
        self._ws = None
        self._running = False
        self._connected = False
        self._reconnect_count = 0

        # Dedup: (txHash, proxyWallet) -> timestamp
        self._seen_events: Dict[tuple, float] = {}

        # Stats
        self._total_messages = 0
        self._trade_events = 0
        self._whale_matches = 0
        self._dedup_hits = 0
        self._last_message_time: Optional[float] = None
        self._connect_time: Optional[float] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict:
        return {
            "connected": self._connected,
            "running": self._running,
            "total_messages": self._total_messages,
            "trade_events": self._trade_events,
            "whale_matches": self._whale_matches,
            "dedup_hits": self._dedup_hits,
            "reconnect_count": self._reconnect_count,
            "last_message_age": (
                time.time() - self._last_message_time
                if self._last_message_time else None
            ),
            "uptime_seconds": (
                time.time() - self._connect_time
                if self._connect_time else None
            ),
            "whale_count": len(self._whale_addresses),
        }

    async def connect(self):
        """Connect to RTDS and process messages. Reconnects on failure."""
        self._running = True

        while self._running and self._reconnect_count < self._max_reconnect_attempts:
            try:
                logger.info(f"RTDS: Connecting to {RTDS_URL}")

                async with websockets.connect(
                    RTDS_URL,
                    ping_interval=None,  # We handle keepalive ourselves
                    close_timeout=5,
                    max_size=2**20,  # 1MB max message size
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._connect_time = time.time()
                    self._reconnect_count = 0
                    logger.info("RTDS: Connected. Subscribing to activity firehose...")

                    subscribe_msg = {
                        "action": "subscribe",
                        "subscriptions": [
                            {"topic": "activity", "type": "*"}
                        ],
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("RTDS: Subscription sent. Listening for trades...")

                    # Start PING keepalive
                    ping_stop = asyncio.Event()
                    ping_task = asyncio.create_task(self._ping_loop(ping_stop))

                    try:
                        await self._message_loop()
                    finally:
                        ping_stop.set()
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except ConnectionClosed as e:
                logger.warning(f"RTDS: Connection closed: {e}")
            except asyncio.CancelledError:
                logger.info("RTDS: Task cancelled")
                break
            except Exception as e:
                logger.error(f"RTDS: Error: {e}")

            self._connected = False
            self._ws = None

            if self._running:
                self._reconnect_count += 1
                wait = min(self._reconnect_delay * self._reconnect_count, 30)
                logger.info(
                    f"RTDS: Reconnecting in {wait:.0f}s "
                    f"(attempt {self._reconnect_count})"
                )
                await asyncio.sleep(wait)

        if self._reconnect_count >= self._max_reconnect_attempts:
            logger.error("RTDS: Max reconnection attempts reached")

    async def disconnect(self):
        """Disconnect from RTDS."""
        self._running = False
        if self._ws:
            await self._ws.close()
        self._connected = False
        logger.info("RTDS: Disconnected")

    def update_whale_addresses(self, new_addresses: Set[str]):
        """Update the whale address filter set."""
        added = new_addresses - self._whale_addresses
        removed = self._whale_addresses - new_addresses
        self._whale_addresses.clear()
        self._whale_addresses.update(new_addresses)
        if added or removed:
            logger.info(
                f"RTDS: Whale filter updated: {len(new_addresses)} addresses "
                f"(+{len(added)}, -{len(removed)})"
            )

    async def _ping_loop(self, stop_event: asyncio.Event):
        """Send literal PING every 5 seconds as required by RTDS."""
        while not stop_event.is_set():
            try:
                if self._ws:
                    await self._ws.send("PING")
            except Exception:
                break
            await asyncio.sleep(PING_INTERVAL)

    async def _message_loop(self):
        """Process incoming RTDS messages."""
        async for raw_msg in self._ws:
            if not self._running:
                break

            # Skip PONG responses
            if raw_msg == "PONG":
                continue

            self._total_messages += 1
            self._last_message_time = time.time()

            try:
                data = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            # Handle both single events and arrays
            events = data if isinstance(data, list) else [data]

            for event in events:
                if isinstance(event, dict):
                    self._process_event(event)

    def _process_event(self, event: dict):
        """Process a single RTDS event — filter and route."""
        payload = event.get("payload")
        if not payload or not isinstance(payload, dict):
            return

        proxy_wallet = (payload.get("proxyWallet") or "").lower()
        if not proxy_wallet:
            return

        self._trade_events += 1
        tx_hash = payload.get("transactionHash", "")

        # Dedup: same txHash+proxyWallet = duplicate event
        dedup_key = (tx_hash, proxy_wallet)
        now = time.time()
        if dedup_key in self._seen_events:
            self._dedup_hits += 1
            return
        self._seen_events[dedup_key] = now

        # Periodic dedup cleanup
        if len(self._seen_events) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW
            self._seen_events = {
                k: v for k, v in self._seen_events.items()
                if v > cutoff
            }

        # Build normalized trade dict compatible with existing pipeline
        trade_dict = {
            "proxyWallet": payload.get("proxyWallet", ""),
            "side": payload.get("side", ""),
            "size": payload.get("size", 0),
            "price": payload.get("price", 0),
            "title": payload.get("title", ""),
            "outcome": payload.get("outcome", ""),
            "conditionId": payload.get("conditionId", ""),
            "asset": payload.get("asset", ""),
            "transactionHash": tx_hash,
            "timestamp": payload.get("timestamp", ""),
            "outcomeIndex": payload.get("outcomeIndex"),
            "eventSlug": payload.get("eventSlug", ""),
            "slug": payload.get("slug", ""),
            "name": payload.get("name", ""),
            "pseudonym": payload.get("pseudonym", ""),
            "_source": "rtds",
        }

        # Route to whale queue if tracked whale
        if proxy_wallet in self._whale_addresses:
            self._whale_matches += 1
            try:
                self._whale_trade_queue.put_nowait(trade_dict)
            except asyncio.QueueFull:
                logger.warning("RTDS: Whale trade queue full, dropping event")

        # Route ALL trades to activity queue (unusual activity detection)
        if self._activity_queue is not None:
            try:
                self._activity_queue.put_nowait(trade_dict)
            except asyncio.QueueFull:
                pass  # Expected under high volume
