"""
Whale discovery and lifecycle management.

Handles leaderboard fetching, progressive scaling of the active whale set,
per-whale copy P&L tracking, pruning, and conviction-weighted sizing.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class WhaleWallet:
    """A whale wallet we're tracking"""
    address: str
    name: str
    monthly_profit: float
    rank: int = 0  # 1-based leaderboard rank (1 = highest profit)
    last_seen_trade_id: str = ""
    trades_copied: int = 0


# Fallback whale list — only used if the leaderboard API is unreachable
FALLBACK_WHALES = [
    WhaleWallet("0x492442eab586f242b53bda933fd5de859c8a3782", "Multicolored-Self", 939609, rank=1),
    WhaleWallet("0xd0b4c4c020abdc88ad9a884f999f3d8cff8ffed6", "MrSparklySimpsons", 882152, rank=2),
    WhaleWallet("0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "beachboy4", 815937, rank=3),
    WhaleWallet("0x96489abcb9f583d6835c8ef95ffc923d05a86825", "anoin123", 771031, rank=4),
    WhaleWallet("0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a", "bossoskil1", 653491, rank=5),
    WhaleWallet("0x9976874011b081e1e408444c579f48aa5b5967da", "BWArmageddon", 520868, rank=6),
    WhaleWallet("0xdc876e6873772d38716fda7f2452a78d426d7ab6", "432614799197", 443001, rank=7),
    WhaleWallet("0xd25c72ac0928385610611c8148803dc717334d20", "FeatherLeather", 420638, rank=8),
    WhaleWallet("0x03e8a544e97eeff5753bc1e90d46e5ef22af1697", "weflyhigh", 291172, rank=9),
    WhaleWallet("0xf208326de73e12994c0cd2b641dddc74a319fa74", "BreezeScout", 267731, rank=10),
    WhaleWallet("0x2537fa3357f0e42fa283b8d0338390dda0b6bff9", "herewego446", 259803, rank=11),
    WhaleWallet("0xbddf61af533ff524d27154e589d2d7a81510c684", "Countryside", 248955, rank=12),
    WhaleWallet("0xb8e6281d22dc80e08885ebc7d819da9bf8cdd504", "ball52759", 233604, rank=13),
    WhaleWallet("0xaa075924e1dc7cff3b9fab67401126338c4d2125", "rustin", 210746, rank=14),
    WhaleWallet("0xafbacaeeda63f31202759eff7f8126e49adfe61b", "SammySledge", 186988, rank=15),
    WhaleWallet("0x3b5c629f114098b0dee345fb78b7a3a013c7126e", "SMCAOMCRL", 162499, rank=16),
    WhaleWallet("0x58776759ee5c70a915138706a1308add8bc5d894", "Marktakh", 154969, rank=17),
    WhaleWallet("0xee613b3fc183ee44f9da9c05f53e2da107e3debf", "sovereign2013", 150160, rank=18),
    WhaleWallet("0x1455445e9a775cfa3fe9fc4b02bb4d2f682ae5cd", "c4c4", 132236, rank=19),
    WhaleWallet("0x090a0d3fc9d68d3e16db70e3460e3e4b510801b4", "slight-", 131751, rank=20),
]

LEADERBOARD_API_URL = "https://data-api.polymarket.com/v1/leaderboard"
LEADERBOARD_REFRESH_HOURS = 6  # Re-fetch leaderboard every N hours
LEADERBOARD_FETCH_N = 50  # Always fetch this many from API (cache for scaling)

# Progressive whale scaling — start small, expand if activity is low
ACTIVE_WHALES_INITIAL = 8        # Start polling top 8
ACTIVE_WHALES_STEP = 4           # Add/remove 4 at a time
ACTIVE_WHALES_MAX = 20           # Never poll more than 20 (less noise, higher signal quality)
SCALING_WINDOW_SECONDS = 600     # 10-minute lookback for activity
SCALING_MIN_FRESH_TRADES = 5     # Scale up if fewer than this in window
SCALING_CHECK_INTERVAL = 60      # Check scaling every 60 seconds


async def fetch_top_whales(session: aiohttp.ClientSession = None) -> List[WhaleWallet]:
    """
    Fetch the top profitable wallets from the Polymarket leaderboard API.
    Falls back to FALLBACK_WHALES if the API is unreachable.
    """
    close_session = False
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        close_session = True

    try:
        params = {
            "timePeriod": "MONTH",
            "orderBy": "PNL",
            "limit": 50,
        }
        async with session.get(LEADERBOARD_API_URL, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    f"Leaderboard API returned HTTP {resp.status}: {body[:200]}. "
                    f"Falling back to hardcoded whale list."
                )
                return list(FALLBACK_WHALES)

            data = await resp.json()

        if not data or not isinstance(data, list):
            logger.warning("Leaderboard API returned empty/invalid data. Using fallback.")
            return list(FALLBACK_WHALES)

        # Filter to positive PNL and convert to WhaleWallet objects
        whales = []
        for entry in data:
            pnl = entry.get("pnl", 0)
            if pnl <= 0:
                continue
            address = entry.get("proxyWallet", "")
            name = entry.get("userName", "") or address[:12]
            if not address:
                continue
            whales.append(WhaleWallet(
                address=address,
                name=name,
                monthly_profit=int(pnl),
                rank=len(whales) + 1,  # 1-based rank by PNL order
            ))
            if len(whales) >= LEADERBOARD_FETCH_N:
                break

        if not whales:
            logger.warning("Leaderboard returned no profitable wallets. Using fallback.")
            return list(FALLBACK_WHALES)

        logger.info(f"Fetched top {len(whales)} whales from Polymarket leaderboard")
        for i, w in enumerate(whales[:5], 1):
            logger.debug(f"  {i}. {w.name} (${w.monthly_profit:,.0f}/mo)")
        if len(whales) > 5:
            logger.debug(f"  ... and {len(whales) - 5} more")

        return whales

    except Exception as e:
        logger.warning(f"Failed to fetch leaderboard: {e}. Using fallback.")
        return list(FALLBACK_WHALES)
    finally:
        if close_session:
            await session.close()


class WhaleManager:
    """Manages whale discovery, scaling, P&L tracking, and pruning."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        max_per_trade: float = 1.0,
        get_open_position_whales: Optional[Callable[[], Set[str]]] = None,
    ):
        self._session = session
        self._max_per_trade = max_per_trade

        # Callback to get whale addresses with open positions (for rebuild_active_set)
        self._get_open_position_whales = get_open_position_whales or (lambda: set())

        # Track whales (populated dynamically via leaderboard API)
        self.whales: Dict[str, WhaleWallet] = {w.address.lower(): w for w in FALLBACK_WHALES}
        self._all_whales: Dict[str, WhaleWallet] = {}  # Full fetched list (up to 50)
        self._last_leaderboard_refresh: Optional[datetime] = None

        # Progressive whale scaling
        self._active_whale_count: int = ACTIVE_WHALES_INITIAL
        self._fresh_trade_timestamps: List[datetime] = []
        self._last_scaling_check: datetime = datetime.min.replace(tzinfo=timezone.utc)

        # Per-whale copy P&L tracking
        self._whale_copy_pnl: Dict[str, dict] = {}
        self._pruned_whales: Set[str] = set()
        self.WHALE_PRUNE_MIN_COPIES = 5
        self.WHALE_PRUNE_PNL_THRESHOLD = -0.10

    async def refresh_from_leaderboard(self) -> None:
        """Fetch the leaderboard and update the tracked whale list.
        Stores full list for progressive scaling, then rebuilds active set.
        """
        new_whales = await fetch_top_whales(session=self._session)
        new_whales_dict = {w.address.lower(): w for w in new_whales}

        old_all_addrs = set(self._all_whales.keys())
        new_all_addrs = set(new_whales_dict.keys())

        if self._last_leaderboard_refresh is not None:
            added = new_all_addrs - old_all_addrs
            removed = old_all_addrs - new_all_addrs

            if added or removed:
                logger.info(
                    f"Leaderboard updated: +{len(added)} new, -{len(removed)} dropped"
                )
                for addr in list(added)[:3]:
                    w = new_whales_dict[addr]
                    logger.debug(f"  + {w.name} rank #{w.rank} (${w.monthly_profit:,.0f}/mo)")
            else:
                logger.debug("Leaderboard refreshed - no changes")

        # Store full list, then rebuild active set based on current scaling
        self._all_whales = new_whales_dict
        self.rebuild_active_set()
        self._last_leaderboard_refresh = datetime.now(timezone.utc)

    def rebuild_active_set(self) -> None:
        """Rebuild self.whales from _all_whales based on active count + open positions."""
        # Sort all whales by rank (1 = best)
        sorted_whales = sorted(self._all_whales.values(), key=lambda w: w.rank)

        # Take top N by active_whale_count
        active = {}
        for w in sorted_whales[:self._active_whale_count]:
            active[w.address.lower()] = w

        # Always include whales with open positions (even if beyond active count)
        open_position_whales = 0
        open_whale_addrs = self._get_open_position_whales()
        for addr in open_whale_addrs:
            if addr in self._all_whales and addr not in active:
                active[addr] = self._all_whales[addr]
                open_position_whales += 1

        self.whales = active
        extra_str = f" + {open_position_whales} with open positions" if open_position_whales else ""
        logger.debug(
            f"Active whales: {len(active)} "
            f"(top {min(self._active_whale_count, len(self._all_whales))} by rank{extra_str})"
        )

    def check_scaling(self) -> None:
        """Check if we should scale the active whale count up or down based on trade activity."""
        now = datetime.now(timezone.utc)

        # Only check every SCALING_CHECK_INTERVAL seconds
        if (now - self._last_scaling_check).total_seconds() < SCALING_CHECK_INTERVAL:
            return
        self._last_scaling_check = now

        # Prune timestamps outside the window
        cutoff = now - timedelta(seconds=SCALING_WINDOW_SECONDS)
        self._fresh_trade_timestamps = [
            t for t in self._fresh_trade_timestamps if t > cutoff
        ]

        fresh_count = len(self._fresh_trade_timestamps)
        old_count = self._active_whale_count

        if fresh_count < SCALING_MIN_FRESH_TRADES:
            # Low activity — scale UP to find more trades
            new_count = min(
                self._active_whale_count + ACTIVE_WHALES_STEP,
                ACTIVE_WHALES_MAX,
                len(self._all_whales),
            )
            if new_count > self._active_whale_count:
                self._active_whale_count = new_count
                self.rebuild_active_set()
                logger.info(
                    f"Scaling UP: {old_count} -> {new_count} whales "
                    f"({fresh_count} fresh trades in last {SCALING_WINDOW_SECONDS // 60}min, "
                    f"need {SCALING_MIN_FRESH_TRADES})"
                )
        elif fresh_count >= SCALING_MIN_FRESH_TRADES * 2:
            # High activity — scale DOWN to focus on top whales
            new_count = max(
                self._active_whale_count - ACTIVE_WHALES_STEP,
                ACTIVE_WHALES_INITIAL,
            )
            if new_count < self._active_whale_count:
                self._active_whale_count = new_count
                self.rebuild_active_set()
                logger.info(
                    f"Scaling DOWN: {old_count} -> {new_count} whales "
                    f"({fresh_count} fresh trades — plenty of activity)"
                )

    def record_copy_pnl(self, whale_address: str, pnl: float) -> None:
        """Record realized P&L for a copy from a specific whale."""
        addr = whale_address.lower()
        if addr not in self._whale_copy_pnl:
            self._whale_copy_pnl[addr] = {"copies": 0, "realized_pnl": 0.0, "wins": 0, "losses": 0}
        entry = self._whale_copy_pnl[addr]
        entry["copies"] += 1
        entry["realized_pnl"] += pnl
        if pnl > 0:
            entry["wins"] += 1
        elif pnl < 0:
            entry["losses"] += 1

        # Check if this whale should be pruned
        if (entry["copies"] >= self.WHALE_PRUNE_MIN_COPIES and
                entry["realized_pnl"] < self.WHALE_PRUNE_PNL_THRESHOLD):
            if addr not in self._pruned_whales:
                self._pruned_whales.add(addr)
                whale_name = "unknown"
                for w in self.whales.values():
                    if w.address.lower() == addr:
                        whale_name = w.name
                        break
                logger.warning(
                    f"PRUNED WHALE: {whale_name} — "
                    f"copy P&L ${entry['realized_pnl']:+.2f} after {entry['copies']} copies "
                    f"({entry['wins']}W/{entry['losses']}L). No longer copying."
                )

    def is_pruned(self, whale_address: str) -> bool:
        """Check if a whale has been pruned due to poor copy performance."""
        return whale_address.lower() in self._pruned_whales

    def conviction_size(self, whale: WhaleWallet, trade_value: float) -> float:
        """
        Calculate conviction-weighted position size based on whale rank and trade size.

        Returns a dollar amount based on:
        - Whale leaderboard rank (top 5 = bigger bets)
        - Trade size relative to typical trades (larger = more conviction)
        - Per-whale copy track record (if available)
        """
        multiplier = 1.0

        # Rank-based: top whales get bigger allocation
        if whale.rank <= 3:
            multiplier = 3.0
        elif whale.rank <= 5:
            multiplier = 2.5
        elif whale.rank <= 10:
            multiplier = 2.0
        elif whale.rank <= 15:
            multiplier = 1.5
        # rank > 15 stays at 1.0

        # Trade size conviction: large trades from whale = more conviction
        if trade_value >= 10000:
            multiplier *= 1.5
        elif trade_value >= 5000:
            multiplier *= 1.25

        # Per-whale track record adjustment
        addr = whale.address.lower()
        if addr in self._whale_copy_pnl:
            data = self._whale_copy_pnl[addr]
            if data["copies"] >= 3:
                if data["realized_pnl"] > 0:
                    multiplier *= 1.25  # Winning whale, bet more
                elif data["realized_pnl"] < -0.05:
                    multiplier *= 0.5  # Losing whale, bet less

        # Cap the multiplier
        multiplier = min(multiplier, 5.0)

        return self._max_per_trade * multiplier

    @property
    def last_leaderboard_refresh(self) -> Optional[datetime]:
        return self._last_leaderboard_refresh

    @property
    def active_whale_count(self) -> int:
        return self._active_whale_count

    @active_whale_count.setter
    def active_whale_count(self, value: int):
        self._active_whale_count = value

    def to_dict(self) -> dict:
        """Serialize state for persistence."""
        return {
            "active_whale_count": self._active_whale_count,
            "whale_copy_pnl": self._whale_copy_pnl,
            "pruned_whales": list(self._pruned_whales),
        }

    def from_dict(self, data: dict) -> None:
        """Restore state from persistence."""
        self._active_whale_count = data.get("active_whale_count", ACTIVE_WHALES_INITIAL)
        self._whale_copy_pnl = data.get("whale_copy_pnl", {})
        self._pruned_whales = set(data.get("pruned_whales", []))
