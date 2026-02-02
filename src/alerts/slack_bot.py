"""
Slack bot for copy-edge alerts using Incoming Webhooks.

Updated for the new copy-edge scoring system with wallet profiling,
trade intent detection, and markout-based performance tracking.
"""

import asyncio
from datetime import datetime
from typing import Optional, Union

import aiohttp
from loguru import logger

from src.database.db import Database
from src.database.models import CanonicalTrade, WalletTier
from src.scoring.copy_scorer import CopyScore


class SlackAlertBot:
    """
    Slack bot for sending copy-edge alerts via Incoming Webhooks.

    Sends notifications when high-scoring copy opportunities are detected,
    including wallet performance stats, trade intent, and score breakdown.
    """

    def __init__(
        self,
        webhook_url: str,
        db: Database,
        config: Optional[dict] = None
    ):
        """
        Initialize Slack bot.

        Args:
            webhook_url: Slack Incoming Webhook URL
            db: Database instance
            config: Optional configuration
        """
        self.webhook_url = webhook_url
        self.db = db
        self.config = config or {}

        self._session: Optional[aiohttp.ClientSession] = None

        # Rate limiting
        self._last_alert_time: Optional[datetime] = None
        self._alerts_this_minute = 0
        self._max_alerts_per_minute = config.get('max_alerts_per_minute', 10) if config else 10

        # Stats
        self._alerts_sent = 0
        self._start_time = datetime.utcnow()

    async def initialize(self) -> bool:
        """
        Initialize the bot and verify webhook works.

        Returns:
            True if webhook is valid
        """
        self._session = aiohttp.ClientSession()

        # Test the webhook with a simple message
        try:
            async with self._session.post(
                self.webhook_url,
                json={"text": "Whale Watcher connected successfully!"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    logger.info("Slack webhook verified successfully")
                    return True
                else:
                    logger.error(f"Slack webhook verification failed: {response.status}")
                    return False
        except Exception as e:
            logger.error(f"Failed to verify Slack webhook: {e}")
            return False

    async def start(self):
        """Start the bot (initialize session if needed)"""
        if not self._session:
            await self.initialize()
        logger.info("Slack bot started")

    async def stop(self):
        """Stop the bot and cleanup"""
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("Slack bot stopped")

    async def send_alert(
        self,
        trade: CanonicalTrade,
        score: CopyScore,
        market_name: Optional[str] = None,
    ) -> bool:
        """
        Send copy-edge alert to Slack.

        Args:
            trade: The canonical trade that triggered the alert
            score: Copy score with breakdown and recommendation
            market_name: Optional market name for display

        Returns:
            True if sent successfully
        """
        # Check rate limit
        if not self._check_rate_limit():
            logger.warning("Slack rate limit exceeded, skipping alert")
            return False

        try:
            if not self._session:
                self._session = aiohttp.ClientSession()

            # Format message with Block Kit
            payload = self._format_copy_alert(trade, score, market_name)

            # Send to webhook
            async with self._session.post(
                self.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    self._alerts_sent += 1
                    self._record_alert_sent()
                    logger.info(
                        f"Copy alert sent: {score.recommendation} "
                        f"(score={score.score:.0f}, wallet={trade.wallet[:8]}...)"
                    )
                    return True
                else:
                    text = await response.text()
                    logger.error(f"Slack webhook returned {response.status}: {text}")
                    return False

        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")
            return False

    def _format_copy_alert(
        self,
        trade: CanonicalTrade,
        score: CopyScore,
        market_name: Optional[str] = None,
    ) -> dict:
        """Format copy-edge alert for Slack Block Kit"""
        features = score.features

        # Recommendation emoji and color
        rec_config = {
            "STRONG_COPY": (":rocket:", "Strong Copy Signal"),
            "COPY": (":chart_with_upwards_trend:", "Copy Signal"),
            "NEUTRAL": (":grey_question:", "Neutral"),
            "AVOID": (":warning:", "Avoid"),
        }
        emoji, header_text = rec_config.get(score.recommendation, (":grey_question:", "Signal"))

        # Wallet tier indicator
        tier_emoji = {
            WalletTier.ELITE: ":trophy:",
            WalletTier.GOOD: ":star:",
            WalletTier.NEUTRAL: ":white_circle:",
            WalletTier.BAD: ":x:",
            WalletTier.UNKNOWN: ":question:",
        }

        wallet_emoji = tier_emoji.get(features.wallet_tier, ":question:")

        # Market name fallback
        display_market = market_name or trade.condition_id[:20] + "..."

        # Build Block Kit message
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {header_text}",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Score:*\n`{score.score:.0f}/100` ({score.confidence})"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Wallet:*\n{wallet_emoji} `{trade.wallet[:8]}...{trade.wallet[-4:]}`"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Size:*\n${trade.total_usd:,.0f}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Intent:*\n{features.trade_intent.upper()}"
                    }
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Market:*\n{display_market[:80]}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Side:*\n{trade.side.upper()} @ {trade.avg_price:.2%}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Book Depth:*\n{features.pct_book_consumed or 0:.1f}% consumed"
                    }
                ]
            }
        ]

        # Wallet profile section
        wallet_stats = []
        if features.wallet_win_rate_1h is not None:
            wallet_stats.append(f"Win Rate: {features.wallet_win_rate_1h * 100:.0f}%")
        if features.wallet_sharpe_1h is not None:
            wallet_stats.append(f"Sharpe: {features.wallet_sharpe_1h:.2f}")
        if features.wallet_avg_markout_1h is not None:
            wallet_stats.append(f"Avg Markout: {features.wallet_avg_markout_1h:.0f}bps")
        wallet_stats.append(f"Trades: {features.wallet_total_trades}")

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Wallet Profile ({features.wallet_tier.value.title()}):*\n{' | '.join(wallet_stats)}"
            }
        })

        # Score breakdown section
        breakdown = score.breakdown
        breakdown_text = (
            f"Wallet: {breakdown.get('wallet_quality', 0):.0f}/40 | "
            f"Size: {breakdown.get('size_significance', 0):.0f}/20 | "
            f"Intent: {breakdown.get('intent', 0):.0f}/15 | "
            f"Market: {breakdown.get('market_context', 0):.0f}/10 | "
            f"Timing: {breakdown.get('timing', 0):.0f}/5"
        )
        if breakdown.get('cost_penalty', 0) < 0:
            breakdown_text += f" | Cost: {breakdown.get('cost_penalty', 0):.0f}"

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": breakdown_text
                }
            ]
        })

        # Timestamp context
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Polymarket | {trade.first_fill_time.strftime('%H:%M:%S')} UTC | {trade.num_fills} fills"
                }
            ]
        })

        # Action buttons
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "View Wallet"
                    },
                    "url": f"https://polygonscan.com/address/{trade.wallet}"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "View Market"
                    },
                    "url": f"https://polymarket.com/event/{trade.condition_id}"
                }
            ]
        })

        return {"blocks": blocks}

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

    def _mark_alert_sent(self, trade: CanonicalTrade, score: CopyScore):
        """Mark trade as alerted in database"""
        try:
            with self.db.get_session() as session:
                from src.database.models import CanonicalTrade as TradeModel
                db_trade = (
                    session.query(TradeModel)
                    .filter(TradeModel.id == trade.id)
                    .first()
                )
                if db_trade:
                    db_trade.alerted = True
                    db_trade.alert_score = score.score
                    session.commit()
        except Exception as e:
            logger.debug(f"Failed to mark trade as alerted: {e}")


async def send_slack_test_alert(webhook_url: str) -> bool:
    """
    Send a test alert to verify Slack webhook configuration.

    Args:
        webhook_url: Slack webhook URL

    Returns:
        True if successful
    """
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": ":rocket: Copy-Edge Scoring System Connected",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "*AI Whale Watcher is configured correctly!*\n\n"
                                "You will receive alerts here when high-scoring copy opportunities "
                                "are detected on Polymarket.\n\n"
                                "_Alerts include:_\n"
                                "• Wallet tier and historical performance\n"
                                "• Trade intent (OPEN/CLOSE/ADD/REDUCE)\n"
                                "• % of orderbook depth consumed\n"
                                "• Copy score breakdown (0-100)"
                            )
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Scoring: Wallet (40) + Size (20) + Intent (15) + Market (10) + Timing (5) - Costs"
                            }
                        ]
                    }
                ]
            }

            async with session.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    logger.info("Slack test alert sent successfully")
                    return True
                else:
                    text = await response.text()
                    logger.error(f"Failed to send Slack test alert: {response.status} - {text}")
                    return False

    except Exception as e:
        logger.error(f"Failed to send Slack test alert: {e}")
        return False
