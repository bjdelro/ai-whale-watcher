"""
Telegram bot for whale alerts
"""

import asyncio
from datetime import datetime
from typing import Optional

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)
from loguru import logger

from src.database.db import Database
from src.database.models import AlertSeverity
from src.detection.detectors import DetectionResult


class TelegramAlertBot:
    """
    Telegram bot for sending whale alerts and handling commands.

    Commands:
    - /start - Welcome message
    - /status - System status
    - /alerts - Recent alerts
    - /top - Top whales today
    - /help - Help message
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        db: Database,
        config: Optional[dict] = None
    ):
        """
        Initialize Telegram bot.

        Args:
            token: Telegram bot token
            chat_id: Chat ID to send alerts to
            db: Database instance
            config: Optional configuration
        """
        self.token = token
        self.chat_id = chat_id
        self.db = db
        self.config = config or {}

        self._bot: Optional[Bot] = None
        self._app: Optional[Application] = None
        self._is_running = False

        # Rate limiting
        self._last_alert_time: Optional[datetime] = None
        self._alerts_this_minute = 0
        self._max_alerts_per_minute = config.get('max_alerts_per_minute', 10) if config else 10

        # Stats
        self._alerts_sent = 0
        self._start_time = datetime.utcnow()

    async def initialize(self):
        """Initialize the bot application"""
        self._app = (
            Application.builder()
            .token(self.token)
            .build()
        )

        # Register command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("alerts", self._cmd_alerts))
        self._app.add_handler(CommandHandler("top", self._cmd_top))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

        self._bot = self._app.bot
        logger.info("Telegram bot initialized")

    async def start(self):
        """Start the bot (for receiving commands)"""
        if not self._app:
            await self.initialize()

        self._is_running = True

        # Start polling in background
        await self._app.initialize()
        await self._app.start()

        # Start polling without blocking
        await self._app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot started")

    async def stop(self):
        """Stop the bot"""
        self._is_running = False

        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

        logger.info("Telegram bot stopped")

    async def send_alert(self, result: DetectionResult) -> bool:
        """
        Send whale alert to Telegram.

        Args:
            result: Detection result to send

        Returns:
            True if sent successfully
        """
        # Check rate limit
        if not self._check_rate_limit():
            logger.warning("Rate limit exceeded, skipping alert")
            return False

        try:
            if not self._bot:
                await self.initialize()

            # Format message with Markdown
            message = self._format_telegram_message(result)

            # Send message
            await self._bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )

            self._alerts_sent += 1
            self._record_alert_sent()

            # Update database
            self._mark_alert_sent(result)

            logger.debug(f"Alert sent to Telegram: {result.score.final_score:.0f}")
            return True

        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")
            return False

    def _format_telegram_message(self, result: DetectionResult) -> str:
        """Format detection result for Telegram"""
        trade = result.trade
        score = result.score

        # Severity emoji
        severity_emoji = {
            AlertSeverity.LOW: "⚪",
            AlertSeverity.MEDIUM: "🟡",
            AlertSeverity.HIGH: "🟠",
            AlertSeverity.CRITICAL: "🔴"
        }

        emoji = severity_emoji.get(result.severity, "⚪")

        # Build message
        lines = [
            f"{emoji} *WHALE TRADE DETECTED*",
            "",
            f"*Wallet:* `{trade.wallet_address[:8]}...{trade.wallet_address[-6:]}`",
            f"*Action:* {trade.trade_type.value.upper()} {trade.outcome or ''}",
            f"*Market:* {self._escape_markdown(trade.market_name[:45])}",
            f"*Value:* ${trade.amount_usd:,.0f}",
        ]

        if trade.price:
            lines.append(f"*Price:* {trade.price:.1%}")

        lines.extend([
            "",
            f"*Score:* {score.final_score:.0f}/100",
            "",
            "*Signals:*"
        ])

        for reason in score.reasons[:5]:  # Limit to 5 reasons
            lines.append(f"• {self._escape_markdown(reason)}")

        lines.extend([
            "",
            f"_{trade.platform.value.title()} | {trade.timestamp.strftime('%H:%M:%S')} UTC_"
        ])

        # Add explorer link for Polymarket
        if trade.platform.value == "polymarket" and trade.wallet_address:
            lines.append(
                f"\n[View Wallet](https://polygonscan.com/address/{trade.wallet_address})"
            )

        return "\n".join(lines)

    def _escape_markdown(self, text: str) -> str:
        """Escape Markdown special characters"""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits"""
        now = datetime.utcnow()

        # Reset counter every minute
        if self._last_alert_time:
            elapsed = (now - self._last_alert_time).total_seconds()
            if elapsed >= 60:
                self._alerts_this_minute = 0

        # Check limit
        if self._alerts_this_minute >= self._max_alerts_per_minute:
            return False

        return True

    def _record_alert_sent(self):
        """Record that an alert was sent"""
        self._last_alert_time = datetime.utcnow()
        self._alerts_this_minute += 1

    def _mark_alert_sent(self, result: DetectionResult):
        """Mark alert as sent in database"""
        try:
            with self.db.get_session() as session:
                # Find the most recent alert for this trade
                from src.database.models import Alert
                alert = (
                    session.query(Alert)
                    .filter(Alert.message == result.message)
                    .order_by(Alert.created_at.desc())
                    .first()
                )
                if alert:
                    alert.sent_to_telegram = True
                    alert.sent_at = datetime.utcnow()
        except Exception as e:
            logger.debug(f"Failed to mark alert sent: {e}")

    # ==================== Command Handlers ====================

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        message = (
            "🐋 *Whale Watcher Bot*\n\n"
            "I monitor Polymarket and Kalshi for large trades, "
            "especially from new accounts.\n\n"
            "*Commands:*\n"
            "/status - System status\n"
            "/alerts - Recent alerts\n"
            "/top - Top whales today\n"
            "/help - Help message"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        uptime = datetime.utcnow() - self._start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)

        # Get stats from database
        with self.db.get_session() as session:
            from src.database.models import Trade, Alert
            from datetime import timedelta

            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

            trades_today = session.query(Trade).filter(Trade.timestamp >= today).count()
            alerts_today = session.query(Alert).filter(Alert.created_at >= today).count()

        message = (
            "📊 *System Status*\n\n"
            f"*Uptime:* {hours}h {minutes}m\n"
            f"*Alerts Sent:* {self._alerts_sent}\n"
            f"*Trades Today:* {trades_today}\n"
            f"*Alerts Today:* {alerts_today}\n"
            f"\n_Running since {self._start_time.strftime('%Y-%m-%d %H:%M')} UTC_"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /alerts command"""
        with self.db.get_session() as session:
            alerts = self.db.get_recent_alerts(session, limit=5)

            if not alerts:
                await update.message.reply_text("No recent alerts.")
                return

            lines = ["🔔 *Recent Alerts*\n"]

            for alert in alerts:
                trade = alert.trade
                emoji = "🟡" if alert.severity.value == "medium" else "🟠" if alert.severity.value == "high" else "🔴" if alert.severity.value == "critical" else "⚪"

                lines.append(
                    f"{emoji} ${trade.amount_usd:,.0f} on {trade.market.name[:30]}...\n"
                    f"   Score: {alert.score:.0f} | {alert.created_at.strftime('%H:%M')}"
                )

            await update.message.reply_text(
                "\n".join(lines),
                parse_mode=ParseMode.MARKDOWN
            )

    async def _cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /top command"""
        with self.db.get_session() as session:
            trades = self.db.get_large_trades(session, min_amount=50000, hours=24, limit=5)

            if not trades:
                await update.message.reply_text("No large trades in the last 24h.")
                return

            lines = ["🐋 *Top Whales (24h)*\n"]

            for i, trade in enumerate(trades, 1):
                lines.append(
                    f"{i}. ${trade.amount_usd:,.0f} - {trade.wallet_address[:8]}...\n"
                    f"   {trade.market.name[:35]}..."
                )

            await update.message.reply_text(
                "\n".join(lines),
                parse_mode=ParseMode.MARKDOWN
            )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        message = (
            "🐋 *Whale Watcher Help*\n\n"
            "*What I Monitor:*\n"
            "• Large trades ($100k+)\n"
            "• New account activity\n"
            "• Last-minute trades before market close\n"
            "• Activity on obscure markets\n\n"
            "*Alert Levels:*\n"
            "⚪ Low - Minor signal\n"
            "🟡 Medium - Moderate signal\n"
            "🟠 High - Strong signal\n"
            "🔴 Critical - Multiple strong signals\n\n"
            "*Commands:*\n"
            "/status - System status\n"
            "/alerts - Recent alerts\n"
            "/top - Top whales today"
        )
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
