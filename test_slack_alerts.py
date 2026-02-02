#!/usr/bin/env python3
"""
Test script for Slack alerts with mock trade data.

Generates realistic whale trades and sends alerts to Slack
to verify the full pipeline works.
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from loguru import logger
from src.database.db import init_db
from src.platforms.mock_feed import MockTradeFeed
from src.detection.detectors import WhaleDetector, DetectionResult
from src.alerts.alert_manager import AlertManager
import os


async def main():
    print("🐋 WHALE WATCHER - SLACK ALERT TEST")
    print("=" * 50)
    print("This will generate mock whale trades and send")
    print("real alerts to your Slack channel.\n")

    # Check Slack config
    slack_webhook = os.getenv('SLACK_WEBHOOK_URL')
    if not slack_webhook:
        print("❌ SLACK_WEBHOOK_URL not set in .env")
        return

    print(f"✅ Slack webhook configured")
    print("=" * 50)

    # Initialize components
    db = init_db("/tmp/test_whale_watcher.db")

    # Detection config - lower thresholds for testing
    detection_config = {
        'large_trade_usd': 5000,  # $5k threshold
        'new_account_days': 7,
        'last_minute_secs': 300,
        'obscure_volume_threshold': 50000,
        'min_alert_score': 40,  # Lower for testing
        'min_trade_amount': 1000,
        'deduplication_window': 60,  # 1 minute
    }

    # Initialize detector
    detector = WhaleDetector(db, detection_config)

    # Initialize alert manager with Slack
    alert_config = {
        'max_alerts_per_minute': 5,
    }
    alert_manager = AlertManager(
        db=db,
        config=alert_config,
        slack_webhook_url=slack_webhook
    )

    # Initialize mock feed
    mock_config = {
        'trade_interval': 3,  # Trade every 3 seconds
        'whale_probability': 0.5,  # 50% chance of whale (for testing)
        'new_account_probability': 0.5,  # 50% new accounts
    }
    mock_feed = MockTradeFeed(mock_config)

    # Track alerts
    alerts_sent = 0

    async def on_whale_detected(result: DetectionResult):
        nonlocal alerts_sent
        logger.info(
            f"🚨 WHALE ALERT: ${result.trade.amount_usd:,.0f} "
            f"(score: {result.score.final_score:.0f})"
        )
        await alert_manager.send_alert(result)
        alerts_sent += 1

    # Register callbacks
    detector.on_detection(on_whale_detected)

    async def on_trade(trade):
        # Check if this mock wallet is "new"
        is_new = mock_feed.is_new_wallet(trade.wallet_address)
        if is_new:
            # Mark in raw_data for detector
            if trade.raw_data:
                trade.raw_data['is_new_account'] = True

        await detector.process_trade(trade, mock_feed)

    mock_feed.on_trade(on_trade)

    # Start components
    await alert_manager.start()

    print("\n🚀 Starting mock trade feed...")
    print("   Generating trades every 3 seconds")
    print("   Whale alerts will be sent to Slack")
    print("   Press Ctrl+C to stop\n")

    # Run for 60 seconds or until interrupted
    try:
        feed_task = asyncio.create_task(mock_feed.start_listening())

        # Run for 60 seconds
        await asyncio.sleep(60)

        print(f"\n⏱️ Test complete! Sent {alerts_sent} alerts to Slack")

    except KeyboardInterrupt:
        print(f"\n\n🛑 Stopped by user. Sent {alerts_sent} alerts to Slack")

    finally:
        await mock_feed.close()
        await alert_manager.stop()
        await detector.close()


if __name__ == "__main__":
    # Configure logging
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
    )

    asyncio.run(main())
