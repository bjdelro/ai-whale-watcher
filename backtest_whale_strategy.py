#!/usr/bin/env python3
"""
Backtest Whale Copy Strategy

Fetches historical trades from top whales and calculates P&L
based on current prices or market resolutions.
"""

import asyncio
import aiohttp
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional
from collections import defaultdict

# Same whale list as the copy trader
TOP_WHALES = [
    ("0x492442eab586f242b53bda933fd5de859c8a3782", "Multicolored-Self"),
    ("0xd0b4c4c020abdc88ad9a884f999f3d8cff8ffed6", "MrSparklySimpsons"),
    ("0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "beachboy4"),
    ("0x96489abcb9f583d6835c8ef95ffc923d05a86825", "anoin123"),
    ("0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a", "bossoskil1"),
    ("0x9976874011b081e1e408444c579f48aa5b5967da", "BWArmageddon"),
    ("0xdc876e6873772d38716fda7f2452a78d426d7ab6", "432614799197"),
    ("0xd25c72ac0928385610611c8148803dc717334d20", "FeatherLeather"),
    ("0x03e8a544e97eeff5753bc1e90d46e5ef22af1697", "weflyhigh"),
    ("0xf208326de73e12994c0cd2b641dddc74a319fa74", "BreezeScout"),
]

DATA_API_BASE = "https://data-api.polymarket.com"

@dataclass
class Position:
    whale: str
    market_title: str
    outcome: str
    entry_price: float
    entry_time: str
    shares: float  # Based on $1 position size
    asset_id: str
    condition_id: str
    current_price: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None


async def fetch_whale_trades(session: aiohttp.ClientSession, address: str, limit: int = 100) -> List[dict]:
    """Fetch recent trades for a whale"""
    url = f"{DATA_API_BASE}/trades"
    params = {"user": address, "limit": limit}

    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception as e:
        print(f"Error fetching trades: {e}")
        return []


async def fetch_current_price(session: aiohttp.ClientSession, asset_id: str) -> Optional[float]:
    """Fetch current price for an asset"""
    try:
        # Try to get price from recent trades
        url = f"{DATA_API_BASE}/trades"
        params = {"asset": asset_id, "limit": 1}

        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                trades = await resp.json()
                if trades:
                    return trades[0].get("price")
    except:
        pass
    return None


async def fetch_market_info(session: aiohttp.ClientSession, condition_id: str) -> Optional[dict]:
    """Fetch market info to check resolution status"""
    try:
        url = f"{DATA_API_BASE}/markets/{condition_id}"
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
    except:
        pass
    return None


async def run_backtest():
    print("=" * 70)
    print("WHALE COPY STRATEGY BACKTEST")
    print("=" * 70)
    print(f"Analyzing top {len(TOP_WHALES)} whales")
    print("Position size: $1 per trade (for easy scaling)")
    print("Filter: BUY trades >= $100, price between 5%-95%")
    print("=" * 70)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        all_positions: List[Position] = []
        whale_stats = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0, "losses": 0})

        # Fetch trades from each whale
        for address, name in TOP_WHALES:
            print(f"\nFetching trades for {name}...")
            trades = await fetch_whale_trades(session, address, limit=50)

            buys = []
            sells_by_asset = defaultdict(list)

            for trade in trades:
                side = trade.get("side", "")
                asset_id = trade.get("asset", "")

                if side == "BUY":
                    buys.append(trade)
                elif side == "SELL":
                    sells_by_asset[asset_id].append(trade)

            print(f"  Found {len(buys)} BUY trades, {sum(len(v) for v in sells_by_asset.values())} SELL trades")

            # Process BUY trades
            for trade in buys:
                price = trade.get("price", 0)
                size = trade.get("size", 0)
                trade_value = size * price

                # Apply same filters as copy trader
                if trade_value < 100:  # Min $100 trade
                    continue
                if price < 0.05 or price > 0.95:  # Skip extreme prices
                    continue

                asset_id = trade.get("asset", "")
                condition_id = trade.get("conditionId", "")

                # $1 position = 1/price shares
                our_shares = 1.0 / price

                position = Position(
                    whale=name,
                    market_title=trade.get("title", "Unknown")[:50],
                    outcome=trade.get("outcome", ""),
                    entry_price=price,
                    entry_time=trade.get("timestamp", ""),
                    shares=our_shares,
                    asset_id=asset_id,
                    condition_id=condition_id,
                )

                # Check if whale sold this position later
                if asset_id in sells_by_asset:
                    for sell in sells_by_asset[asset_id]:
                        sell_time = sell.get("timestamp", "")
                        if sell_time > trade.get("timestamp", ""):
                            position.exit_price = sell.get("price", 0)
                            position.exit_reason = "whale_sold"
                            break

                all_positions.append(position)

            await asyncio.sleep(0.2)  # Rate limit

        print(f"\n{'=' * 70}")
        print(f"Found {len(all_positions)} copyable positions")
        print("Fetching current prices...")
        print("=" * 70)

        # Fetch current prices for open positions
        price_cache = {}
        market_cache = {}

        for i, pos in enumerate(all_positions):
            if pos.exit_price is None:
                # Check if market resolved
                if pos.condition_id not in market_cache:
                    market_info = await fetch_market_info(session, pos.condition_id)
                    market_cache[pos.condition_id] = market_info
                    await asyncio.sleep(0.1)

                market_info = market_cache.get(pos.condition_id)
                if market_info:
                    # Check for resolution
                    if market_info.get("resolved") or market_info.get("closed"):
                        winning = market_info.get("resolution") or market_info.get("winning_outcome")
                        if winning:
                            did_win = pos.outcome.lower() == winning.lower()
                            pos.exit_price = 1.0 if did_win else 0.0
                            pos.exit_reason = "resolved"

                    # Check tokens for price
                    if pos.exit_price is None:
                        tokens = market_info.get("tokens", [])
                        for token in tokens:
                            if token.get("outcome", "").lower() == pos.outcome.lower():
                                pos.current_price = token.get("price", pos.entry_price)
                                # Check if effectively resolved
                                if pos.current_price >= 0.99:
                                    pos.exit_price = 1.0
                                    pos.exit_reason = "resolved_won"
                                elif pos.current_price <= 0.01:
                                    pos.exit_price = 0.0
                                    pos.exit_reason = "resolved_lost"
                                break

                # Fallback: fetch from recent trades
                if pos.exit_price is None and pos.current_price is None:
                    if pos.asset_id not in price_cache:
                        price = await fetch_current_price(session, pos.asset_id)
                        price_cache[pos.asset_id] = price
                        await asyncio.sleep(0.05)
                    pos.current_price = price_cache.get(pos.asset_id)

            if (i + 1) % 20 == 0:
                print(f"  Processed {i + 1}/{len(all_positions)} positions...")

        # Calculate P&L
        print(f"\n{'=' * 70}")
        print("RESULTS")
        print("=" * 70)

        total_invested = 0
        total_pnl = 0
        realized_pnl = 0
        unrealized_pnl = 0
        winners = 0
        losers = 0
        open_count = 0

        for pos in all_positions:
            total_invested += 1.0  # $1 per position

            if pos.exit_price is not None:
                # Closed position
                pnl = (pos.exit_price - pos.entry_price) * pos.shares
                pos.pnl = pnl
                realized_pnl += pnl

                if pnl > 0:
                    winners += 1
                elif pnl < 0:
                    losers += 1

                whale_stats[pos.whale]["pnl"] += pnl
                whale_stats[pos.whale]["trades"] += 1
                if pnl > 0:
                    whale_stats[pos.whale]["wins"] += 1
                else:
                    whale_stats[pos.whale]["losses"] += 1
            else:
                # Open position - use current price or entry price
                current = pos.current_price if pos.current_price else pos.entry_price
                pnl = (current - pos.entry_price) * pos.shares
                pos.pnl = pnl
                unrealized_pnl += pnl
                open_count += 1

                whale_stats[pos.whale]["trades"] += 1

        total_pnl = realized_pnl + unrealized_pnl

        # Summary
        print(f"\nPOSITION SUMMARY:")
        print(f"  Total positions: {len(all_positions)}")
        print(f"  Closed: {len(all_positions) - open_count}")
        print(f"  Still open: {open_count}")
        print(f"  Winners: {winners} | Losers: {losers}")
        if winners + losers > 0:
            print(f"  Win rate: {winners / (winners + losers) * 100:.1f}%")

        print(f"\nP&L SUMMARY (based on $1 per trade):")
        print(f"  Total invested: ${total_invested:.2f}")
        print(f"  Realized P&L:   ${realized_pnl:+.2f}")
        print(f"  Unrealized P&L: ${unrealized_pnl:+.2f}")
        print(f"  TOTAL P&L:      ${total_pnl:+.2f}")
        print(f"  Return:         {total_pnl / total_invested * 100:+.1f}%")

        # Per-whale breakdown
        print(f"\n{'=' * 70}")
        print("P&L BY WHALE:")
        print("=" * 70)

        sorted_whales = sorted(whale_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
        for whale, stats in sorted_whales:
            if stats["trades"] > 0:
                win_rate = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
                emoji = "🟢" if stats["pnl"] > 0 else "🔴" if stats["pnl"] < 0 else "⚪"
                print(f"  {emoji} {whale:20} | {stats['trades']:3} trades | "
                      f"${stats['pnl']:+7.2f} | {win_rate:5.1f}% wins")

        # Show some example positions
        print(f"\n{'=' * 70}")
        print("SAMPLE CLOSED POSITIONS:")
        print("=" * 70)

        closed = [p for p in all_positions if p.exit_price is not None]
        closed.sort(key=lambda x: x.pnl or 0, reverse=True)

        print("\nBest trades:")
        for pos in closed[:5]:
            emoji = "🟢" if pos.pnl > 0 else "🔴"
            print(f"  {emoji} {pos.whale:15} | {pos.outcome:3} @ {pos.entry_price:.1%} → {pos.exit_price:.1%} "
                  f"| ${pos.pnl:+.2f} | {pos.market_title[:30]}...")

        print("\nWorst trades:")
        for pos in closed[-5:]:
            emoji = "🟢" if pos.pnl > 0 else "🔴"
            print(f"  {emoji} {pos.whale:15} | {pos.outcome:3} @ {pos.entry_price:.1%} → {pos.exit_price:.1%} "
                  f"| ${pos.pnl:+.2f} | {pos.market_title[:30]}...")

        # Scaling projections
        print(f"\n{'=' * 70}")
        print("SCALING PROJECTIONS:")
        print("=" * 70)
        roi = total_pnl / total_invested if total_invested > 0 else 0
        print(f"  If you invested $100:  P&L = ${100 * roi:+.2f}")
        print(f"  If you invested $1000: P&L = ${1000 * roi:+.2f}")
        print(f"  If you invested $10000: P&L = ${10000 * roi:+.2f}")

        print(f"\n{'=' * 70}")
        if total_pnl > 0:
            print("VERDICT: Strategy appears PROFITABLE based on recent data")
        else:
            print("VERDICT: Strategy appears UNPROFITABLE based on recent data")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_backtest())
