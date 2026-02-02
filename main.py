#!/usr/bin/env python3
"""
Whale Watcher - Prediction Market Monitoring System

Monitors Polymarket and Kalshi for large trades, especially from new accounts.
Sends alerts via Telegram when whale activity is detected.

Usage:
    python main.py                  # Run with default config
    python main.py --config my.yaml # Run with custom config
    python main.py --test-alert     # Send test Telegram alert
"""

import asyncio
import argparse
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from loguru import logger

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.database.db import Database, init_db
from src.platforms.polymarket import PolymarketClient
from src.platforms.kalshi import KalshiClient
from src.detection.detectors import WhaleDetector, DetectionResult
from src.alerts.alert_manager import AlertManager, send_test_alert
from src.alerts.slack_bot import send_slack_test_alert


class WhaleWatcher:
    """
    Main application class for Whale Watcher.

    Orchestrates:
    - Platform connections (Polymarket, Kalshi)
    - Detection engine
    - Alert delivery
    """

    def __init__(self, config_path: str = "config.yaml"):
        """
        Initialize Whale Watcher.

        Args:
            config_path: Path to configuration file
        """
        self.config_path = config_path
        self.config = self._load_config()

        # Initialize components
        self.db: Optional[Database] = None
        self.polymarket: Optional[PolymarketClient] = None
        self.kalshi: Optional[KalshiClient] = None
        self.detector: Optional[WhaleDetector] = None
        self.alert_manager: Optional[AlertManager] = None

        # Task tracking
        self._tasks: list[asyncio.Task] = []
        self._is_running = False

    def _load_config(self) -> dict:
        """Load configuration from YAML file"""
        # Load .env file
        load_dotenv()

        # Load YAML config
        config_file = Path(self.config_path)
        if not config_file.exists():
            logger.warning(f"Config file not found: {self.config_path}, using defaults")
            return self._default_config()

        with open(config_file) as f:
            config = yaml.safe_load(f)

        # Override with environment variables
        config = self._apply_env_overrides(config)

        return config

    def _default_config(self) -> dict:
        """Return default configuration"""
        return {
            'platforms': {
                'polymarket': {
                    'enabled': True,
                    'websocket_url': 'wss://ws-subscriptions-clob.polymarket.com/ws/market',
                    'clob_url': 'https://clob.polymarket.com',
                    'gamma_url': 'https://gamma-api.polymarket.com',
                    'polling_interval': 60
                },
                'kalshi': {
                    'enabled': True,
                    'websocket_url': 'wss://api.elections.kalshi.com/trade-api/ws/v2',
                    'rest_url': 'https://trading-api.kalshi.com/trade-api/v2',
                    'polling_interval': 120
                }
            },
            'detection': {
                'new_account_days': 7,
                'large_trade_usd': 100000,
                'last_minute_secs': 300,
                'obscure_volume_threshold': 50000,
                'min_alert_score': 60,
                'min_trade_amount': 1000
            },
            'alerts': {
                'telegram_enabled': True,
                'max_alerts_per_minute': 10,
                'deduplication_window': 300
            },
            'database': {
                'path': 'whale_watcher.db'
            },
            'logging': {
                'level': 'INFO'
            }
        }

    def _apply_env_overrides(self, config: dict) -> dict:
        """Apply environment variable overrides to config"""
        # Telegram settings
        config.setdefault('alerts', {})
        config['alerts']['telegram_token'] = os.getenv('TELEGRAM_BOT_TOKEN')
        config['alerts']['telegram_chat_id'] = os.getenv('TELEGRAM_CHAT_ID')

        # Slack settings
        config['alerts']['slack_webhook_url'] = os.getenv('SLACK_WEBHOOK_URL')

        # PolygonScan API key
        config.setdefault('polygonscan', {})
        config['polygonscan']['api_key'] = os.getenv('POLYGONSCAN_API_KEY')

        # Optional overrides
        if os.getenv('LARGE_TRADE_THRESHOLD'):
            config['detection']['large_trade_usd'] = int(os.getenv('LARGE_TRADE_THRESHOLD'))

        if os.getenv('NEW_ACCOUNT_DAYS'):
            config['detection']['new_account_days'] = int(os.getenv('NEW_ACCOUNT_DAYS'))

        return config

    async def initialize(self):
        """Initialize all components"""
        logger.info("Initializing Whale Watcher...")

        # Setup logging
        self._setup_logging()

        # Initialize database
        db_path = self.config.get('database', {}).get('path', 'whale_watcher.db')
        self.db = init_db(db_path)
        logger.info(f"Database initialized: {db_path}")

        # Initialize detection engine
        self.detector = WhaleDetector(
            db=self.db,
            config=self.config.get('detection', {}),
            polygonscan_api_key=self.config.get('polygonscan', {}).get('api_key')
        )

        # Initialize alert manager
        alerts_config = self.config.get('alerts', {})
        self.alert_manager = AlertManager(
            db=self.db,
            config=alerts_config,
            telegram_token=alerts_config.get('telegram_token'),
            telegram_chat_id=alerts_config.get('telegram_chat_id'),
            slack_webhook_url=alerts_config.get('slack_webhook_url')
        )

        # Register detection callback
        self.detector.on_detection(self._on_whale_detected)

        # Initialize platform clients
        platforms = self.config.get('platforms', {})

        if platforms.get('polymarket', {}).get('enabled', True):
            self.polymarket = PolymarketClient(platforms.get('polymarket', {}))
            self.polymarket.on_trade(self._on_polymarket_trade)
            logger.info("Polymarket client initialized")

        if platforms.get('kalshi', {}).get('enabled', True):
            self.kalshi = KalshiClient(platforms.get('kalshi', {}))
            self.kalshi.on_trade(self._on_kalshi_trade)
            logger.info("Kalshi client initialized")

        logger.info("Initialization complete")

    def _setup_logging(self):
        """Configure logging"""
        log_config = self.config.get('logging', {})
        level = log_config.get('level', 'INFO')

        # Remove default handler
        logger.remove()

        # Add console handler
        logger.add(
            sys.stderr,
            level=level,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        )

        # Add file handler if configured
        log_file = log_config.get('file')
        if log_file:
            logger.add(
                log_file,
                level=level,
                rotation="10 MB",
                retention="7 days"
            )

    async def _on_whale_detected(self, result: DetectionResult):
        """Handle whale detection"""
        logger.info(
            f"🐋 Whale detected: ${result.trade.amount_usd:,.0f} "
            f"(score: {result.score.final_score:.0f})"
        )

        # Send alert
        if self.alert_manager:
            await self.alert_manager.send_alert(result)

    async def _on_polymarket_trade(self, trade):
        """Handle Polymarket trade"""
        await self.detector.process_trade(trade, self.polymarket)

    async def _on_kalshi_trade(self, trade):
        """Handle Kalshi trade"""
        await self.detector.process_trade(trade, self.kalshi)

    async def start(self):
        """Start the whale watcher"""
        if self._is_running:
            return

        self._is_running = True
        logger.info("Starting Whale Watcher...")

        # Start alert manager
        if self.alert_manager:
            await self.alert_manager.start()

        # Start platform listeners
        if self.polymarket:
            task = asyncio.create_task(
                self._run_platform(self.polymarket, "Polymarket")
            )
            self._tasks.append(task)

        if self.kalshi:
            task = asyncio.create_task(
                self._run_platform(self.kalshi, "Kalshi")
            )
            self._tasks.append(task)

        logger.info("Whale Watcher started - monitoring for whale activity")

        # Wait for all tasks
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_platform(self, platform, name: str):
        """Run a platform listener with fallback polling"""
        while self._is_running:
            try:
                # Try WebSocket first
                logger.info(f"Starting {name} WebSocket listener...")
                await platform.start_listening()

            except Exception as e:
                logger.error(f"{name} WebSocket failed: {e}")

                if self._is_running:
                    # Fallback to polling
                    logger.info(f"Falling back to {name} polling...")
                    try:
                        await platform.poll_trades()
                    except Exception as poll_error:
                        logger.error(f"{name} polling failed: {poll_error}")
                        await asyncio.sleep(30)

    async def stop(self):
        """Stop the whale watcher"""
        logger.info("Stopping Whale Watcher...")
        self._is_running = False

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()

        # Wait for cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Stop components
        if self.alert_manager:
            await self.alert_manager.stop()

        if self.polymarket:
            await self.polymarket.close()

        if self.kalshi:
            await self.kalshi.close()

        if self.detector:
            await self.detector.close()

        logger.info("Whale Watcher stopped")


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Whale Watcher - Prediction Market Monitor"
    )
    parser.add_argument(
        '--config', '-c',
        default='config.yaml',
        help='Path to configuration file'
    )
    parser.add_argument(
        '--test-alert',
        action='store_true',
        help='Send a test alert to Telegram'
    )

    args = parser.parse_args()

    # Handle test alert
    if args.test_alert:
        load_dotenv()
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        slack_webhook = os.getenv('SLACK_WEBHOOK_URL')

        if not token and not chat_id and not slack_webhook:
            logger.error("At least one of TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID or SLACK_WEBHOOK_URL must be set in .env")
            sys.exit(1)

        success = True

        # Test Telegram if configured
        if token and chat_id:
            logger.info("Sending Telegram test alert...")
            telegram_success = await send_test_alert(token, chat_id)
            success = success and telegram_success

        # Test Slack if configured
        if slack_webhook:
            logger.info("Sending Slack test alert...")
            slack_success = await send_slack_test_alert(slack_webhook)
            success = success and slack_success

        sys.exit(0 if success else 1)

    # Create and run whale watcher
    watcher = WhaleWatcher(args.config)

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(watcher.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Initialize and start
    try:
        await watcher.initialize()
        await watcher.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await watcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
