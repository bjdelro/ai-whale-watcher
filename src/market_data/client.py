"""
Market data client for Polymarket.

Fetches prices, market details, and detects market resolution.
Uses CLOB API as primary source with Gamma API fallback.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Polymarket API endpoints
DATA_API_BASE = "https://data-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class MarketDataClient:
    """Fetches and caches market data from Polymarket APIs."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._cache: Dict[str, dict] = {}

    async def fetch_price(self, token_id: str) -> Optional[float]:
        """Fetch current price for a token via recent trades.

        Only returns a price if the most recent trade is less than 30 minutes old.
        This prevents stale prices from triggering false market resolutions
        (e.g., an old $0.01 trade making us think the market resolved as a loss).
        """
        try:
            url = f"{DATA_API_BASE}/trades"
            params = {"asset": token_id, "limit": 1}

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    trades = await resp.json()
                    if trades:
                        # Check trade freshness — ignore stale prices
                        trade = trades[0]
                        trade_timestamp = trade.get("timestamp") or trade.get("matchTime") or trade.get("createdAt")
                        if trade_timestamp:
                            try:
                                ts = float(trade_timestamp)
                                if ts > 1e12:
                                    ts = ts / 1000
                                age = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
                                if age > 1800:  # 30 minutes — skip stale prices
                                    logger.debug(f"Skipping stale price for token {token_id[:20]}... (age={age:.0f}s)")
                                    return None
                            except (ValueError, TypeError):
                                pass  # Can't parse timestamp, allow the price through
                        return trade.get("price")
        except Exception:
            pass
        return None

    async def fetch_market(self, condition_id: str) -> Optional[dict]:
        """Fetch market details from CLOB API (with Gamma API fallback for resolution data)."""
        # Check cache first
        if condition_id in self._cache:
            cache_entry = self._cache[condition_id]
            # Cache for 60 seconds
            if (datetime.now(timezone.utc) - cache_entry.get("_cached_at", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < 60:
                return cache_entry

        market_data = None

        # Primary: CLOB API — has token prices and active/closed status
        try:
            url = f"{CLOB_API_BASE}/markets/{condition_id}"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    market_data = await resp.json()
        except Exception as e:
            logger.debug(f"CLOB market fetch failed for {condition_id[:20]}...: {e}")

        # Fallback: Gamma API — has resolution/closed fields
        if not market_data:
            try:
                url = f"{GAMMA_API_BASE}/markets"
                params = {"condition_id": condition_id, "limit": 1}
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        markets = await resp.json()
                        if markets:
                            market_data = markets[0] if isinstance(markets, list) else markets
            except Exception as e:
                logger.debug(f"Gamma market fetch failed for {condition_id[:20]}...: {e}")

        if market_data:
            market_data["_cached_at"] = datetime.now(timezone.utc)
            self._cache[condition_id] = market_data
            return market_data

        return None

    def is_resolved(self, market_data: dict) -> tuple:
        """
        Determine if a market has resolved and what the outcome is.
        Returns: (is_resolved: bool, winning_outcome: str | None)
        """
        # Check explicit resolution flags
        if market_data.get("resolved"):
            return (True, market_data.get("resolution", market_data.get("winning_outcome")))

        if market_data.get("closed"):
            return (True, market_data.get("resolution", market_data.get("winning_outcome")))

        # Check price convergence for binary markets
        # If Yes price is ~100% or ~0%, market is effectively resolved
        tokens = market_data.get("tokens", [])
        if tokens:
            for token in tokens:
                price = token.get("price", 0.5)
                outcome = token.get("outcome", "")
                if price >= 0.99:
                    return (True, outcome)
                if price <= 0.01:
                    # This outcome lost, other one won
                    other_outcome = "No" if outcome == "Yes" else "Yes"
                    return (True, other_outcome)

        # Check if past end date
        end_date_str = market_data.get("endDate") or market_data.get("end_date")
        if end_date_str:
            try:
                from dateutil.parser import parse as parse_date
                end_date = parse_date(end_date_str)
                if datetime.now(timezone.utc) > end_date:
                    # Past end date but no resolution yet - might need manual check
                    return (False, None)
            except Exception:
                pass

        return (False, None)

    def to_dict(self) -> dict:
        """Serialize cache state for persistence."""
        # Don't persist cache — it's short-lived (60s TTL)
        return {}

    def from_dict(self, data: dict) -> None:
        """Restore cache state from persistence."""
        pass
