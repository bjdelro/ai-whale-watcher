#!/usr/bin/env python3
"""
Market Monitor for AI Whale Watcher

Monitors Polymarket for significant price movements and trade activity.
Uses price polling since the trades API has limitations.

This logs market activity while the full trading system is being developed.

Usage:
    python run_market_monitor.py

Press Ctrl+C to stop.
"""

import asyncio
import argparse
import json
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)


class MarketMonitor:
    """
    Monitors Polymarket prices and detects significant activity.

    Polls top markets every 15 seconds and logs:
    - Price changes > 2%
    - Spread changes
    - Volume spikes
    """

    POLL_INTERVAL = 15
    PRICE_CHANGE_THRESHOLD = 0.02  # 2% price change
    NUM_MARKETS = 50

    def __init__(self):
        self._running = False
        self._client = None
        self._markets: Dict[str, dict] = {}  # token_id -> last state
        self._log_dir = Path("market_logs")
        self._log_dir.mkdir(exist_ok=True)
        self._log_file = self._log_dir / f"activity_{datetime.utcnow().strftime('%Y%m%d')}.jsonl"
        self._events_logged = 0

    async def start(self):
        """Initialize and start monitoring"""
        from py_clob_client.client import ClobClient

        logger.info("=" * 60)
        logger.info("POLYMARKET MARKET MONITOR")
        logger.info("=" * 60)

        private_key = os.getenv("PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY")
        if not private_key:
            raise ValueError("PRIVATE_KEY required")

        self._client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
        )
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)

        logger.info("Connected to Polymarket CLOB")
        logger.info(f"Monitoring {self.NUM_MARKETS} markets")
        logger.info(f"Logging to: {self._log_file}")
        logger.info("Press Ctrl+C to stop\n")

        self._running = True

    async def run(self):
        """Main monitoring loop"""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    await self._poll_markets(session)
                except Exception as e:
                    logger.error(f"Poll error: {e}")

                await asyncio.sleep(self.POLL_INTERVAL)

    async def _poll_markets(self, session):
        """Poll top markets for activity"""
        import json as json_lib

        # Get top markets
        url = "https://gamma-api.polymarket.com/markets?limit=100&closed=false"
        async with session.get(url) as resp:
            if resp.status != 200:
                return
            markets = await resp.json()

        # Sort by 24h volume
        markets_sorted = sorted(
            markets,
            key=lambda m: float(m.get("volume24hr") or 0),
            reverse=True
        )

        for m in markets_sorted[:self.NUM_MARKETS]:
            clob_tokens = m.get("clobTokenIds", "[]")
            if isinstance(clob_tokens, str):
                clob_tokens = json_lib.loads(clob_tokens)

            if not clob_tokens:
                continue

            token_id = clob_tokens[0]  # YES token
            market_name = m.get("question", "")[:60]

            try:
                # Get current price
                price_data = self._client.get_last_trade_price(token_id=token_id)
                current_price = float(price_data.get("price", 0))
                side = price_data.get("side", "")

                # Check for changes
                if token_id in self._markets:
                    last = self._markets[token_id]
                    last_price = last.get("price", current_price)

                    # Calculate price change
                    if last_price > 0:
                        change = (current_price - last_price) / last_price

                        if abs(change) >= self.PRICE_CHANGE_THRESHOLD:
                            event = {
                                "timestamp": datetime.utcnow().isoformat(),
                                "type": "price_change",
                                "market": market_name,
                                "token_id": token_id,
                                "old_price": last_price,
                                "new_price": current_price,
                                "change_pct": change * 100,
                                "side": side,
                                "volume_24h": m.get("volume24hr", 0),
                            }

                            self._log_event(event)

                            direction = "UP" if change > 0 else "DOWN"
                            logger.info(
                                f"PRICE {direction}: {market_name}... "
                                f"{last_price:.2%} -> {current_price:.2%} ({change:+.1%})"
                            )

                # Update state
                self._markets[token_id] = {
                    "price": current_price,
                    "side": side,
                    "name": market_name,
                    "updated": datetime.utcnow(),
                }

            except Exception as e:
                logger.debug(f"Error checking {market_name[:30]}: {e}")

    def _log_event(self, event: dict):
        """Log an event to file"""
        with open(self._log_file, "a") as f:
            f.write(json.dumps(event) + "\n")
        self._events_logged += 1

    def stop(self):
        """Stop monitoring"""
        self._running = False
        logger.info(f"\nLogged {self._events_logged} events to {self._log_file}")


async def main():
    monitor = MarketMonitor()

    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("\nShutting down...")
        monitor.stop()

    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    try:
        await monitor.start()
        await monitor.run()
    except Exception as e:
        logger.error(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
