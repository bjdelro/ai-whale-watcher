#!/usr/bin/env python3
"""
Demo script - Simulates whale trades to demonstrate the detection system
"""

import asyncio
import random
from datetime import datetime, timedelta

from src.database.db import init_db
from src.database.models import Platform, TradeType
from src.platforms.base import TradeEvent, MarketInfo
from src.detection.detectors import WhaleDetector, DetectionResult
from src.detection.scoring import AlertScorer

# Sample markets
SAMPLE_MARKETS = [
    MarketInfo(
        id="will-trump-win-2024",
        platform=Platform.POLYMARKET,
        name="Will Trump win the 2024 Presidential Election?",
        volume_24h=5_000_000,
        close_time=datetime(2024, 11, 5, 23, 59)
    ),
    MarketInfo(
        id="btc-above-100k-jan",
        platform=Platform.POLYMARKET,
        name="Will Bitcoin be above $100k on January 1, 2025?",
        volume_24h=1_200_000,
        close_time=datetime(2025, 1, 1)
    ),
    MarketInfo(
        id="obscure-market-123",
        platform=Platform.POLYMARKET,
        name="Will obscure event X happen?",
        volume_24h=15_000,  # Low volume = obscure
        close_time=datetime.utcnow() + timedelta(hours=1)
    ),
    MarketInfo(
        id="last-minute-market",
        platform=Platform.KALSHI,
        name="Market closing very soon",
        volume_24h=500_000,
        close_time=datetime.utcnow() + timedelta(minutes=3)  # Closes in 3 min
    ),
]

# Generate random wallet addresses
def random_wallet():
    return "0x" + "".join(random.choices("0123456789abcdef", k=40))


async def simulate_trade(detector: WhaleDetector, trade: TradeEvent, market: MarketInfo, is_new: bool):
    """Simulate processing a trade"""
    print(f"\n{'='*60}")
    print(f"📊 INCOMING TRADE")
    print(f"{'='*60}")
    print(f"  Wallet: {trade.wallet_address[:12]}...")
    print(f"  Market: {trade.market_name[:40]}...")
    print(f"  Amount: ${trade.amount_usd:,.0f}")
    print(f"  Type:   {trade.trade_type.value.upper()}")
    print(f"  New Account: {'Yes' if is_new else 'No'}")

    # Analyze the trade
    result = await detector.analyze_trade(trade, platform=None)

    # Override new account status for demo
    result.is_new_account = is_new
    result.score = detector.scorer.score_trade(
        trade,
        market=market,
        is_new_account=is_new,
        wallet_age_days=2 if is_new else 100
    )
    result.should_alert = detector.scorer.should_alert(result.score)

    print(f"\n📈 ANALYSIS RESULT")
    print(f"  Score: {result.score.final_score:.0f}/100")
    print(f"  Severity: {result.severity.value.upper()}")
    print(f"  Should Alert: {'✅ YES' if result.should_alert else '❌ No'}")

    if result.score.reasons:
        print(f"\n  Signals detected:")
        for reason in result.score.reasons:
            print(f"    • {reason}")

    if result.should_alert:
        print(f"\n🚨 ALERT WOULD BE SENT:")
        print("-" * 50)
        # Simplified alert message
        emoji = "🔴" if result.score.final_score >= 90 else "🟠" if result.score.final_score >= 75 else "🟡"
        print(f"{emoji} WHALE TRADE DETECTED")
        print(f"Wallet: {trade.wallet_address[:10]}...{trade.wallet_address[-6:]}")
        print(f"Action: {trade.trade_type.value.upper()} on {trade.market_name[:35]}...")
        print(f"Value: ${trade.amount_usd:,.0f}")
        print(f"Score: {result.score.final_score:.0f}/100")


async def main():
    print("🐋 WHALE WATCHER DEMO")
    print("=" * 60)
    print("This demo simulates various whale trade scenarios\n")

    # Initialize
    db = init_db("/tmp/demo_whale.db")
    config = {
        'large_trade_usd': 100000,
        'new_account_days': 7,
        'last_minute_secs': 300,
        'obscure_volume_threshold': 50000,
        'min_alert_score': 60,
        'min_trade_amount': 1000,
        'deduplication_window': 0  # Disable for demo
    }
    detector = WhaleDetector(db, config)

    # Scenario 1: Large trade from established account
    print("\n" + "🔵 SCENARIO 1: Large trade from ESTABLISHED account")
    trade1 = TradeEvent(
        platform=Platform.POLYMARKET,
        wallet_address=random_wallet(),
        market_id="will-trump-win-2024",
        market_name="Will Trump win the 2024 Presidential Election?",
        trade_type=TradeType.BUY,
        amount_usd=150_000,
        price=0.55,
        timestamp=datetime.utcnow()
    )
    await simulate_trade(detector, trade1, SAMPLE_MARKETS[0], is_new=False)
    await asyncio.sleep(1)

    # Scenario 2: Large trade from NEW account (high alert!)
    print("\n" + "🔵 SCENARIO 2: Large trade from NEW account (2 days old)")
    trade2 = TradeEvent(
        platform=Platform.POLYMARKET,
        wallet_address=random_wallet(),
        market_id="will-trump-win-2024",
        market_name="Will Trump win the 2024 Presidential Election?",
        trade_type=TradeType.BUY,
        amount_usd=200_000,
        price=0.55,
        timestamp=datetime.utcnow()
    )
    await simulate_trade(detector, trade2, SAMPLE_MARKETS[0], is_new=True)
    await asyncio.sleep(1)

    # Scenario 3: Trade on obscure market
    print("\n" + "🔵 SCENARIO 3: Trade on OBSCURE market (low volume)")
    trade3 = TradeEvent(
        platform=Platform.POLYMARKET,
        wallet_address=random_wallet(),
        market_id="obscure-market-123",
        market_name="Will obscure event X happen?",
        trade_type=TradeType.BUY,
        amount_usd=75_000,
        price=0.30,
        timestamp=datetime.utcnow()
    )
    await simulate_trade(detector, trade3, SAMPLE_MARKETS[2], is_new=True)
    await asyncio.sleep(1)

    # Scenario 4: Last-minute trade before market close
    print("\n" + "🔵 SCENARIO 4: LAST-MINUTE trade (market closes in 3 min)")
    trade4 = TradeEvent(
        platform=Platform.KALSHI,
        wallet_address=random_wallet(),
        market_id="last-minute-market",
        market_name="Market closing very soon",
        trade_type=TradeType.BUY,
        amount_usd=120_000,
        price=0.80,
        timestamp=datetime.utcnow()
    )
    await simulate_trade(detector, trade4, SAMPLE_MARKETS[3], is_new=True)
    await asyncio.sleep(1)

    # Scenario 5: Small trade (should NOT alert)
    print("\n" + "🔵 SCENARIO 5: Small trade (should NOT trigger alert)")
    trade5 = TradeEvent(
        platform=Platform.POLYMARKET,
        wallet_address=random_wallet(),
        market_id="btc-above-100k-jan",
        market_name="Will Bitcoin be above $100k on January 1, 2025?",
        trade_type=TradeType.SELL,
        amount_usd=25_000,
        price=0.45,
        timestamp=datetime.utcnow()
    )
    await simulate_trade(detector, trade5, SAMPLE_MARKETS[1], is_new=False)

    print("\n" + "=" * 60)
    print("🏁 DEMO COMPLETE")
    print("=" * 60)
    print("\nTo run for real:")
    print("1. Add your Telegram bot token to .env")
    print("2. Run: python main.py")
    print("3. Watch for whale alerts in Telegram!")


if __name__ == "__main__":
    asyncio.run(main())
