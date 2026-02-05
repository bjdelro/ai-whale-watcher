"""
Intra-Market Arbitrage Scanner for Polymarket

Detects risk-free arbitrage opportunities in multi-outcome markets.

HOW IT WORKS:
In a multi-outcome market (e.g., "Who wins Best Picture?"), if:
- Film A (Yes): $0.35
- Film B (Yes): $0.32
- Film C (Yes): $0.30
- Total: $0.97

Buy 1 share of each = $0.97 cost
One MUST win = $1.00 payout
Guaranteed profit: $0.03 (3.1%)

This is RISK-FREE because exactly one outcome will pay $1.00.
"""

import logging
from dataclasses import dataclass
from typing import List, Dict, Optional
import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class ArbitrageOpportunity:
    """Represents a risk-free arbitrage opportunity"""
    market_slug: str
    market_title: str
    condition_id: str
    outcomes: Dict[str, float]  # {outcome_name: price}
    total_cost: float  # Cost to buy all outcomes
    guaranteed_payout: float  # Always $1.00
    profit: float  # guaranteed_payout - total_cost
    profit_pct: float  # profit / total_cost
    volume_24h: float  # Market liquidity indicator


class IntraMarketArbitrage:
    """
    Scans Polymarket for intra-market arbitrage opportunities.

    An opportunity exists when the sum of all YES prices in a
    multi-outcome market is less than $1.00.
    """

    # API endpoints
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"

    # Thresholds
    MIN_PROFIT_PCT = 0.01  # Minimum 1% profit to flag (account for fees/slippage)
    MIN_OUTCOMES = 2  # Must have at least 2 outcomes
    MIN_VOLUME_24H = 1000  # Minimum $1000 24h volume (liquidity filter)

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self):
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def scan_all_markets(self) -> List[ArbitrageOpportunity]:
        """
        Scan all active multi-outcome markets for arbitrage opportunities.

        Returns list of opportunities sorted by profit percentage (highest first).
        """
        opportunities = []
        session = await self._get_session()

        try:
            # Fetch all active markets from Gamma API
            markets = await self._fetch_active_markets(session)
            logger.info(f"Scanning {len(markets)} markets for arbitrage...")

            # Group markets by parent event (multi-outcome markets share same group)
            events = self._group_by_event(markets)

            for event_slug, event_markets in events.items():
                if len(event_markets) < self.MIN_OUTCOMES:
                    continue

                # Check for arbitrage opportunity
                opp = self._check_arbitrage(event_slug, event_markets)
                if opp:
                    opportunities.append(opp)

        except Exception as e:
            logger.error(f"Error scanning for arbitrage: {e}")

        # Sort by profit percentage (highest first)
        opportunities.sort(key=lambda x: x.profit_pct, reverse=True)

        return opportunities

    async def _fetch_active_markets(self, session: aiohttp.ClientSession) -> List[dict]:
        """Fetch all active markets from Gamma API"""
        all_markets = []
        offset = 0
        limit = 100

        while True:
            url = f"{self.GAMMA_API}/markets"
            params = {
                "limit": limit,
                "offset": offset,
                "active": "true",
                "closed": "false"
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Failed to fetch markets: {resp.status}")
                    break

                markets = await resp.json()

                if not markets:
                    break

                all_markets.extend(markets)

                if len(markets) < limit:
                    break

                offset += limit

                # Safety limit
                if offset > 2000:
                    break

        return all_markets

    def _group_by_event(self, markets: List[dict]) -> Dict[str, List[dict]]:
        """
        Group markets by their parent event.

        Multi-outcome markets share the same event/group identifier.
        We look for markets with the same groupItemTitle or similar slugs.
        """
        events = {}

        for market in markets:
            # Try to find event grouping
            # Polymarket uses different patterns for grouping:
            # 1. groupItemTitle - explicit group
            # 2. Slug pattern matching (e.g., "best-picture-2026-*")

            group_key = market.get("groupItemTitle") or market.get("conditionId", "")

            # For binary markets (Yes/No), use condition_id as the group
            # They have 2 outcomes inherently (Yes and No)

            if group_key not in events:
                events[group_key] = []
            events[group_key].append(market)

        return events

    def _check_arbitrage(self, event_slug: str, markets: List[dict]) -> Optional[ArbitrageOpportunity]:
        """
        Check if a multi-outcome event has an arbitrage opportunity.

        For multi-outcome markets: sum of all YES prices should = $1.00
        If sum < $1.00, there's arbitrage.
        """
        outcomes = {}
        total_volume = 0
        market_title = ""
        condition_id = ""

        for market in markets:
            # Get the YES price (or best bid)
            outcome_name = market.get("groupItemTitle") or market.get("question", "Unknown")

            # outcomePrices is typically [yes_price, no_price]
            outcome_prices = market.get("outcomePrices")

            # Handle string representation of list
            if isinstance(outcome_prices, str):
                try:
                    import json
                    outcome_prices = json.loads(outcome_prices.replace("'", '"'))
                except:
                    outcome_prices = None

            if outcome_prices and len(outcome_prices) >= 1:
                # First element is YES price
                try:
                    yes_price = float(outcome_prices[0]) if outcome_prices[0] else 0
                except (ValueError, TypeError):
                    yes_price = 0
            else:
                # Try other price fields
                yes_price = float(market.get("bestBid", 0) or market.get("lastTradePrice", 0) or 0)

            if yes_price <= 0 or yes_price >= 1:
                continue

            outcomes[outcome_name] = yes_price
            total_volume += float(market.get("volume24hr", 0) or 0)

            if not market_title:
                market_title = market.get("question", event_slug)
            if not condition_id:
                condition_id = market.get("conditionId", "")

        # Need at least 2 outcomes for multi-outcome arbitrage
        if len(outcomes) < self.MIN_OUTCOMES:
            return None

        # Check liquidity
        if total_volume < self.MIN_VOLUME_24H:
            return None

        # Calculate arbitrage
        total_cost = sum(outcomes.values())
        guaranteed_payout = 1.0  # One outcome will pay $1
        profit = guaranteed_payout - total_cost
        profit_pct = profit / total_cost if total_cost > 0 else 0

        # Check if profitable after fees (Polymarket charges ~1% on winnings)
        if profit_pct < self.MIN_PROFIT_PCT:
            return None

        return ArbitrageOpportunity(
            market_slug=event_slug,
            market_title=market_title[:100],  # Truncate long titles
            condition_id=condition_id,
            outcomes=outcomes,
            total_cost=total_cost,
            guaranteed_payout=guaranteed_payout,
            profit=profit,
            profit_pct=profit_pct,
            volume_24h=total_volume
        )

    async def scan_binary_markets(self) -> List[ArbitrageOpportunity]:
        """
        Scan binary (Yes/No) markets for arbitrage.

        In a binary market, if YES + NO < $1.00, there's arbitrage.
        Buy both YES and NO = guaranteed $1.00 payout.
        """
        opportunities = []
        session = await self._get_session()

        try:
            markets = await self._fetch_active_markets(session)

            for market in markets:
                opp = self._check_binary_arbitrage(market)
                if opp:
                    opportunities.append(opp)

        except Exception as e:
            logger.error(f"Error scanning binary markets: {e}")

        opportunities.sort(key=lambda x: x.profit_pct, reverse=True)
        return opportunities

    def _check_binary_arbitrage(self, market: dict) -> Optional[ArbitrageOpportunity]:
        """
        Check if a binary market has YES + NO arbitrage.

        If bestBid(YES) + bestBid(NO) < $1.00 (minus fees), there's arbitrage.
        """
        # Get prices
        outcome_prices = market.get("outcomePrices")
        if not outcome_prices:
            return None

        # Handle string representation of list (API sometimes returns this)
        if isinstance(outcome_prices, str):
            try:
                import json
                outcome_prices = json.loads(outcome_prices.replace("'", '"'))
            except:
                return None

        if len(outcome_prices) < 2:
            return None

        try:
            yes_price = float(outcome_prices[0]) if outcome_prices[0] else 0
            no_price = float(outcome_prices[1]) if outcome_prices[1] else 0
        except (ValueError, TypeError):
            return None

        if yes_price <= 0 or no_price <= 0:
            return None

        # Check volume
        volume = float(market.get("volume24hr", 0) or 0)
        if volume < self.MIN_VOLUME_24H:
            return None

        # Calculate arbitrage
        total_cost = yes_price + no_price
        profit = 1.0 - total_cost
        profit_pct = profit / total_cost if total_cost > 0 else 0

        # Need sufficient profit margin (fees + slippage)
        if profit_pct < self.MIN_PROFIT_PCT:
            return None

        return ArbitrageOpportunity(
            market_slug=market.get("slug", ""),
            market_title=market.get("question", "Unknown")[:100],
            condition_id=market.get("conditionId", ""),
            outcomes={"Yes": yes_price, "No": no_price},
            total_cost=total_cost,
            guaranteed_payout=1.0,
            profit=profit,
            profit_pct=profit_pct,
            volume_24h=volume
        )


# Quick test
if __name__ == "__main__":
    import asyncio

    async def test():
        scanner = IntraMarketArbitrage()

        print("Scanning for arbitrage opportunities...")

        # Scan binary markets
        binary_opps = await scanner.scan_binary_markets()
        print(f"\nFound {len(binary_opps)} binary market opportunities:")
        for opp in binary_opps[:5]:
            print(f"  {opp.market_title[:50]}")
            print(f"    Cost: ${opp.total_cost:.4f} -> Profit: ${opp.profit:.4f} ({opp.profit_pct:.2%})")

        # Scan multi-outcome markets
        multi_opps = await scanner.scan_all_markets()
        print(f"\nFound {len(multi_opps)} multi-outcome opportunities:")
        for opp in multi_opps[:5]:
            print(f"  {opp.market_title[:50]}")
            print(f"    Outcomes: {len(opp.outcomes)}")
            print(f"    Cost: ${opp.total_cost:.4f} -> Profit: ${opp.profit:.4f} ({opp.profit_pct:.2%})")

        await scanner.close()

    asyncio.run(test())
