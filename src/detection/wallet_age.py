"""
Wallet age detection via PolygonScan API
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional
import aiohttp
from loguru import logger

from src.database.models import Platform, WalletAgeCache
from src.database.db import Database


class WalletAgeChecker:
    """
    Checks wallet age via PolygonScan API.

    For Polymarket (on Polygon), we query the first transaction
    timestamp to determine if a wallet is "new".

    Results are cached to avoid rate limits.
    """

    def __init__(
        self,
        db: Database,
        api_key: Optional[str] = None,
        cache_duration: int = 86400  # 24 hours
    ):
        """
        Initialize wallet age checker.

        Args:
            db: Database instance for caching
            api_key: PolygonScan API key (optional but recommended)
            cache_duration: How long to cache wallet age (seconds)
        """
        self.db = db
        self.api_key = api_key
        self.cache_duration = timedelta(seconds=cache_duration)
        self.api_url = "https://api.polygonscan.com/api"

        self._session: Optional[aiohttp.ClientSession] = None
        self._request_semaphore = asyncio.Semaphore(5)  # Rate limit

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_wallet_age(
        self,
        wallet_address: str,
        platform: Platform
    ) -> Optional[timedelta]:
        """
        Get the age of a wallet.

        Args:
            wallet_address: The wallet address to check
            platform: The platform (only POLYMARKET uses on-chain data)

        Returns:
            timedelta of wallet age, or None if unknown
        """
        if platform != Platform.POLYMARKET:
            # For Kalshi, we can only track from when we first saw them
            return None

        # Check cache first
        cached = await self._get_cached_age(wallet_address)
        if cached is not None:
            return cached

        # Query PolygonScan
        first_tx_time = await self._query_first_transaction(wallet_address)

        if first_tx_time:
            age = datetime.utcnow() - first_tx_time
            await self._cache_wallet_age(wallet_address, first_tx_time)
            return age

        return None

    async def is_new_account(
        self,
        wallet_address: str,
        platform: Platform,
        threshold_days: int = 7
    ) -> bool:
        """
        Check if a wallet is considered "new".

        Args:
            wallet_address: The wallet address
            platform: The platform
            threshold_days: Accounts newer than this are "new"

        Returns:
            True if the account is new or unknown
        """
        age = await self.get_wallet_age(wallet_address, platform)

        if age is None:
            # Unknown age - treat as potentially new
            return True

        return age.days < threshold_days

    async def _get_cached_age(
        self,
        wallet_address: str
    ) -> Optional[timedelta]:
        """Check cache for wallet age"""
        with self.db.get_session() as session:
            cache = (
                session.query(WalletAgeCache)
                .filter_by(wallet_address=wallet_address)
                .first()
            )

            if not cache:
                return None

            # Check if cache is still valid
            if datetime.utcnow() - cache.lookup_timestamp > self.cache_duration:
                return None

            if cache.first_tx_timestamp:
                return datetime.utcnow() - cache.first_tx_timestamp

            # Cache exists but no first tx found
            return None

    async def _cache_wallet_age(
        self,
        wallet_address: str,
        first_tx_timestamp: Optional[datetime],
        error: Optional[str] = None
    ):
        """Store wallet age in cache"""
        with self.db.get_session() as session:
            cache = (
                session.query(WalletAgeCache)
                .filter_by(wallet_address=wallet_address)
                .first()
            )

            if cache:
                cache.first_tx_timestamp = first_tx_timestamp
                cache.lookup_timestamp = datetime.utcnow()
                cache.success = error is None
                cache.error_message = error
            else:
                cache = WalletAgeCache(
                    wallet_address=wallet_address,
                    first_tx_timestamp=first_tx_timestamp,
                    lookup_timestamp=datetime.utcnow(),
                    success=error is None,
                    error_message=error
                )
                session.add(cache)

    async def _query_first_transaction(
        self,
        wallet_address: str
    ) -> Optional[datetime]:
        """
        Query PolygonScan for wallet's first transaction.

        Uses the 'txlist' endpoint with sort=asc to get earliest tx.
        """
        async with self._request_semaphore:
            try:
                session = await self._get_session()

                params = {
                    'module': 'account',
                    'action': 'txlist',
                    'address': wallet_address,
                    'startblock': 0,
                    'endblock': 99999999,
                    'page': 1,
                    'offset': 1,  # We only need the first tx
                    'sort': 'asc'
                }

                if self.api_key:
                    params['apikey'] = self.api_key

                async with session.get(self.api_url, params=params) as response:
                    if response.status == 429:
                        logger.warning("PolygonScan rate limited")
                        await asyncio.sleep(5)
                        return None

                    data = await response.json()

                    if data.get('status') != '1':
                        # No transactions found or error
                        error = data.get('message', 'Unknown error')
                        logger.debug(f"PolygonScan query failed for {wallet_address[:10]}...: {error}")
                        await self._cache_wallet_age(wallet_address, None, error)
                        return None

                    result = data.get('result', [])
                    if not result:
                        return None

                    # Get timestamp of first transaction
                    first_tx = result[0]
                    timestamp = int(first_tx.get('timeStamp', 0))

                    if timestamp:
                        first_tx_time = datetime.utcfromtimestamp(timestamp)
                        logger.debug(
                            f"Wallet {wallet_address[:10]}... first tx: {first_tx_time}"
                        )
                        return first_tx_time

            except Exception as e:
                logger.error(f"Error querying PolygonScan: {e}")
                await self._cache_wallet_age(wallet_address, None, str(e))

            return None

    async def bulk_check_wallets(
        self,
        wallet_addresses: list[str],
        platform: Platform,
        threshold_days: int = 7
    ) -> dict[str, bool]:
        """
        Check multiple wallets for "new" status.

        Args:
            wallet_addresses: List of wallet addresses
            platform: Platform to check
            threshold_days: New account threshold

        Returns:
            Dict mapping address -> is_new_account
        """
        results = {}

        # Process in batches to avoid overwhelming the API
        batch_size = 5
        for i in range(0, len(wallet_addresses), batch_size):
            batch = wallet_addresses[i:i + batch_size]

            # Check concurrently within batch
            tasks = [
                self.is_new_account(addr, platform, threshold_days)
                for addr in batch
            ]
            batch_results = await asyncio.gather(*tasks)

            for addr, is_new in zip(batch, batch_results):
                results[addr] = is_new

            # Small delay between batches
            if i + batch_size < len(wallet_addresses):
                await asyncio.sleep(1)

        return results
