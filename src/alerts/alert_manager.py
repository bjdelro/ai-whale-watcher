"""
Alert manager - coordinates alert routing and delivery
"""

import asyncio
from datetime import datetime
from typing import Optional
from loguru import logger

from src.database.db import Database
from src.detection.detectors import DetectionResult
from .telegram_bot import TelegramAlertBot
from .slack_bot import SlackAlertBot


class AlertManager:
    """
    Manages alert routing and delivery.

    Coordinates between detection engine and notification channels.
    Handles deduplication, rate limiting, and delivery confirmation.
    """

    def __init__(
        self,
        db: Database,
        config: dict,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        slack_webhook_url: Optional[str] = None
    ):
        """
        Initialize alert manager.

        Args:
            db: Database instance
            config: Alert configuration
            telegram_token: Telegram bot token
            telegram_chat_id: Telegram chat ID
            slack_webhook_url: Slack Incoming Webhook URL
        """
        self.db = db
        self.config = config

        # Initialize Telegram bot if configured
        self.telegram_bot: Optional[TelegramAlertBot] = None
        if telegram_token and telegram_chat_id:
            self.telegram_bot = TelegramAlertBot(
                token=telegram_token,
                chat_id=telegram_chat_id,
                db=db,
                config=config
            )

        # Initialize Slack bot if configured
        self.slack_bot: Optional[SlackAlertBot] = None
        if slack_webhook_url:
            self.slack_bot = SlackAlertBot(
                webhook_url=slack_webhook_url,
                db=db,
                config=config
            )

        # Alert queue
        self._alert_queue: asyncio.Queue = asyncio.Queue()
        self._is_running = False
        self._processor_task: Optional[asyncio.Task] = None

        # Stats
        self._alerts_processed = 0
        self._alerts_sent = 0
        self._alerts_failed = 0

    async def start(self):
        """Start the alert manager"""
        self._is_running = True

        # Start Telegram bot
        if self.telegram_bot:
            await self.telegram_bot.start()

        # Start Slack bot
        if self.slack_bot:
            await self.slack_bot.start()

        # Start alert processor
        self._processor_task = asyncio.create_task(self._process_alerts())

        logger.info("Alert manager started")

    async def stop(self):
        """Stop the alert manager"""
        self._is_running = False

        # Stop processor
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        # Stop Telegram bot
        if self.telegram_bot:
            await self.telegram_bot.stop()

        # Stop Slack bot
        if self.slack_bot:
            await self.slack_bot.stop()

        logger.info("Alert manager stopped")

    async def send_alert(self, result: DetectionResult):
        """
        Queue an alert for delivery.

        Args:
            result: Detection result to alert on
        """
        await self._alert_queue.put(result)

    async def _process_alerts(self):
        """Process alerts from the queue"""
        logger.info("Alert processor started")

        while self._is_running:
            try:
                # Wait for alert with timeout
                try:
                    result = await asyncio.wait_for(
                        self._alert_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                self._alerts_processed += 1

                # Send to Telegram
                if self.telegram_bot:
                    success = await self.telegram_bot.send_alert(result)
                    if success:
                        self._alerts_sent += 1
                    else:
                        self._alerts_failed += 1

                # Send to Slack
                if self.slack_bot:
                    success = await self.slack_bot.send_alert(result)
                    if success:
                        self._alerts_sent += 1
                    else:
                        self._alerts_failed += 1

                # Mark task done
                self._alert_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing alert: {e}")
                self._alerts_failed += 1

        logger.info("Alert processor stopped")

    def get_stats(self) -> dict:
        """Get alert manager statistics"""
        return {
            'processed': self._alerts_processed,
            'sent': self._alerts_sent,
            'failed': self._alerts_failed,
            'queue_size': self._alert_queue.qsize()
        }


async def send_test_alert(
    telegram_token: str,
    telegram_chat_id: str
) -> bool:
    """
    Send a test alert to verify Telegram configuration.

    Args:
        telegram_token: Bot token
        telegram_chat_id: Chat ID

    Returns:
        True if successful
    """
    from telegram import Bot

    try:
        bot = Bot(token=telegram_token)

        message = (
            "🐋 *Test Alert*\n\n"
            "Whale Watcher is configured correctly!\n\n"
            "You will receive alerts here when whale "
            "activity is detected on Polymarket and Kalshi."
        )

        await bot.send_message(
            chat_id=telegram_chat_id,
            text=message,
            parse_mode="Markdown"
        )

        logger.info("Test alert sent successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to send test alert: {e}")
        return False
