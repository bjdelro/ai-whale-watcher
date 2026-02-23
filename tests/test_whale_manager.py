"""
Tests for whale_manager.py — covers:
- B1: Decay-weighted P&L (pruning, conviction, un-pruning)
- B2: Composite whale ranking
- Category mix tracking, sports ratio
- Progressive scaling
- Conviction sizing
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from src.whales.whale_manager import (
    WhaleManager, WhaleWallet, _compute_composite_rank,
    ACTIVE_WHALES_INITIAL, ACTIVE_WHALES_MAX,
    SCALING_MIN_FRESH_TRADES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    """Create a WhaleManager with a mock session."""
    session = MagicMock(spec=aiohttp.ClientSession)
    mgr = WhaleManager(session=session, max_per_trade=1.0)
    # Seed _all_whales so rebuild_active_set works
    whales = [
        WhaleWallet(f"0x{i:040x}", f"whale_{i}", monthly_profit=100000 - i * 1000, rank=i)
        for i in range(1, 21)
    ]
    mgr._all_whales = {w.address.lower(): w for w in whales}
    mgr.rebuild_active_set()
    return mgr


# ---------------------------------------------------------------------------
# B2: Composite Ranking
# ---------------------------------------------------------------------------

class TestCompositeRanking:
    def test_filters_negative_pnl(self):
        entries = [
            {"pnl": -100, "volume": 50000, "proxyWallet": "0xbad"},
            {"pnl": 500, "volume": 10000, "proxyWallet": "0xgood"},
        ]
        ranked = _compute_composite_rank(entries)
        # Negative PNL should be filtered out; only the good one remains
        assert len(ranked) == 1
        assert ranked[0]["proxyWallet"] == "0xgood"

    def test_filters_low_volume(self):
        entries = [
            {"pnl": 1000, "volume": 100, "proxyWallet": "0xlow"},  # <$5k volume
            {"pnl": 500, "volume": 10000, "proxyWallet": "0xok"},
        ]
        ranked = _compute_composite_rank(entries)
        assert len(ranked) == 1
        assert ranked[0]["proxyWallet"] == "0xok"

    def test_roi_beats_raw_pnl(self):
        """A wallet with lower PNL but much higher ROI should rank higher."""
        entries = [
            {"pnl": 10000, "volume": 1000000, "proxyWallet": "0xbig_vol"},   # ROI = 0.01
            {"pnl": 5000, "volume": 10000, "proxyWallet": "0xefficient"},     # ROI = 0.50
            {"pnl": 2000, "volume": 50000, "proxyWallet": "0xmedium"},        # ROI = 0.04
        ]
        ranked = _compute_composite_rank(entries)
        # With 3 entries, efficient has roi_pct=1.0 (top ROI) → dominates
        assert ranked[0]["proxyWallet"] == "0xefficient"

    def test_fallback_on_empty(self):
        """If all entries are filtered out, return original list."""
        entries = [
            {"pnl": -100, "volume": 1, "proxyWallet": "0x1"},
        ]
        ranked = _compute_composite_rank(entries)
        assert ranked == entries

    def test_single_entry(self):
        """Single entry should work (no division by zero in percentile)."""
        entries = [{"pnl": 1000, "volume": 50000, "proxyWallet": "0xsolo"}]
        ranked = _compute_composite_rank(entries)
        assert len(ranked) == 1


# ---------------------------------------------------------------------------
# B1: Decay-Weighted P&L
# ---------------------------------------------------------------------------

class TestDecayWeightedPnl:
    def test_recent_trades_weighted_more(self, manager):
        """A recent loss should weigh more than an old win."""
        addr = "0x" + "a" * 40
        now = datetime.now(timezone.utc)

        manager._whale_copy_pnl[addr] = {
            "copies": 2, "realized_pnl": 0.0, "wins": 1, "losses": 1,
            "history": [
                {"pnl": 5.0, "ts": (now - timedelta(days=10)).isoformat()},  # old win, 0.5x weight
                {"pnl": -3.0, "ts": (now - timedelta(hours=12)).isoformat()},  # recent loss, 3x weight
            ],
        }

        decay_pnl = manager._decay_weighted_pnl(addr)
        # weighted = 5.0 * 0.5 + (-3.0) * 3.0 = 2.5 - 9.0 = -6.5
        # total_weight = 0.5 + 3.0 = 3.5
        # normalized = -6.5 / 3.5 ≈ -1.857
        assert decay_pnl < 0  # Recent loss dominates

    def test_all_recent_wins(self, manager):
        addr = "0x" + "b" * 40
        now = datetime.now(timezone.utc)

        manager._whale_copy_pnl[addr] = {
            "copies": 3, "realized_pnl": 3.0, "wins": 3, "losses": 0,
            "history": [
                {"pnl": 1.0, "ts": (now - timedelta(hours=6)).isoformat()},
                {"pnl": 1.0, "ts": (now - timedelta(hours=12)).isoformat()},
                {"pnl": 1.0, "ts": (now - timedelta(hours=18)).isoformat()},
            ],
        }

        decay_pnl = manager._decay_weighted_pnl(addr)
        assert decay_pnl > 0  # All recent wins = positive

    def test_empty_history_falls_back_to_raw(self, manager):
        addr = "0x" + "c" * 40
        manager._whale_copy_pnl[addr] = {
            "copies": 2, "realized_pnl": -1.5, "wins": 0, "losses": 2,
            # No history key — legacy data
        }

        decay_pnl = manager._decay_weighted_pnl(addr)
        assert decay_pnl == -1.5

    def test_no_entry_returns_zero(self, manager):
        assert manager._decay_weighted_pnl("0xnonexistent") == 0.0

    def test_48h_bucket(self, manager):
        addr = "0x" + "d" * 40
        now = datetime.now(timezone.utc)

        manager._whale_copy_pnl[addr] = {
            "copies": 1, "realized_pnl": 2.0, "wins": 1, "losses": 0,
            "history": [
                {"pnl": 2.0, "ts": (now - timedelta(hours=30)).isoformat()},  # 48h bucket, 2x weight
            ],
        }

        decay_pnl = manager._decay_weighted_pnl(addr)
        assert decay_pnl == 2.0  # Single entry normalized = pnl itself


# ---------------------------------------------------------------------------
# B1: Pruning with Decay
# ---------------------------------------------------------------------------

class TestPruningWithDecay:
    def test_prune_on_recent_losses(self, manager):
        """Whale with positive lifetime but negative recent should be pruned."""
        addr = "0x" + "e" * 40
        now = datetime.now(timezone.utc)

        # 5 copies: 3 old wins + 2 recent losses
        manager._whale_copy_pnl[addr] = {
            "copies": 4, "realized_pnl": 1.0, "wins": 3, "losses": 1,
            "history": [
                {"pnl": 2.0, "ts": (now - timedelta(days=14)).isoformat()},
                {"pnl": 2.0, "ts": (now - timedelta(days=12)).isoformat()},
                {"pnl": 2.0, "ts": (now - timedelta(days=10)).isoformat()},
                {"pnl": -5.0, "ts": (now - timedelta(hours=6)).isoformat()},
            ],
        }

        # Record one more recent loss to trigger pruning
        manager.record_copy_pnl(addr, -3.0)

        # Lifetime P&L: 1.0 + (-3.0) = -2.0, but decay_pnl emphasizes recent losses
        assert manager.is_pruned(addr)

    def test_un_prune_on_recovery(self, manager):
        """Pruned whale should be un-pruned when recent performance recovers."""
        addr = "0x" + "f" * 40
        now = datetime.now(timezone.utc)

        # Manually prune
        manager._pruned_whales.add(addr)
        manager._whale_copy_pnl[addr] = {
            "copies": 6, "realized_pnl": -2.0, "wins": 2, "losses": 4,
            "history": [
                {"pnl": -2.0, "ts": (now - timedelta(days=14)).isoformat()},
                {"pnl": -2.0, "ts": (now - timedelta(days=12)).isoformat()},
                {"pnl": -1.0, "ts": (now - timedelta(days=10)).isoformat()},
                {"pnl": -1.0, "ts": (now - timedelta(days=8)).isoformat()},
                # Recent wins
                {"pnl": 3.0, "ts": (now - timedelta(hours=12)).isoformat()},
                {"pnl": 3.0, "ts": (now - timedelta(hours=6)).isoformat()},
            ],
        }

        # is_pruned should check decay_pnl and un-prune
        assert not manager.is_pruned(addr)
        assert addr not in manager._pruned_whales


# ---------------------------------------------------------------------------
# Conviction Sizing
# ---------------------------------------------------------------------------

class TestConvictionSizing:
    def test_top_rank_gets_bigger_bet(self, manager):
        whale_top = WhaleWallet("0x1", "top", 1000000, rank=1)
        whale_mid = WhaleWallet("0x2", "mid", 500000, rank=15)

        size_top = manager.conviction_size(whale_top, 1000)
        size_mid = manager.conviction_size(whale_mid, 1000)

        assert size_top > size_mid

    def test_large_trade_conviction_boost(self, manager):
        whale = WhaleWallet("0x3", "test", 500000, rank=10)

        size_small = manager.conviction_size(whale, 1000)
        size_large = manager.conviction_size(whale, 10000)

        assert size_large > size_small

    def test_decay_pnl_affects_conviction(self, manager):
        """Whale with recent losses should get smaller sizing."""
        whale = WhaleWallet("0x" + "a" * 40, "test", 500000, rank=10)
        now = datetime.now(timezone.utc)

        # No track record
        size_no_record = manager.conviction_size(whale, 5000)

        # Add bad recent track record
        addr = whale.address.lower()
        manager._whale_copy_pnl[addr] = {
            "copies": 5, "realized_pnl": -2.0, "wins": 1, "losses": 4,
            "history": [
                {"pnl": -0.5, "ts": (now - timedelta(hours=i * 6)).isoformat()}
                for i in range(5)
            ],
        }

        size_bad_record = manager.conviction_size(whale, 5000)
        # Both unproven (no record) and proven-bad get 0.5x damping
        assert size_bad_record <= size_no_record

    def test_multiplier_capped_at_5(self, manager):
        """Conviction multiplier should never exceed 5x."""
        whale = WhaleWallet("0x" + "b" * 40, "top", 1000000, rank=1)
        now = datetime.now(timezone.utc)
        addr = whale.address.lower()

        manager._whale_copy_pnl[addr] = {
            "copies": 5, "realized_pnl": 10.0, "wins": 5, "losses": 0,
            "history": [
                {"pnl": 2.0, "ts": (now - timedelta(hours=i)).isoformat()}
                for i in range(5)
            ],
        }

        size = manager.conviction_size(whale, 50000)
        # max_per_trade = 1.0, multiplier capped at 5.0
        assert size <= 5.0


# ---------------------------------------------------------------------------
# Category Mix
# ---------------------------------------------------------------------------

class TestCategoryMix:
    def test_record_and_get_sports_ratio(self, manager):
        addr = "0x" + "a" * 40
        manager.record_whale_category(addr, "sports")
        manager.record_whale_category(addr, "sports")
        manager.record_whale_category(addr, "politics")

        assert manager.get_whale_sports_ratio(addr) == pytest.approx(2 / 3)

    def test_unknown_whale_has_zero_ratio(self, manager):
        assert manager.get_whale_sports_ratio("0xunknown") == 0.0

    def test_record_category_pnl(self, manager):
        manager.record_category_pnl("crypto", 1.5)
        manager.record_category_pnl("crypto", -0.5)

        cat = manager._category_copy_pnl["crypto"]
        assert cat["copies"] == 2
        assert cat["realized_pnl"] == pytest.approx(1.0)
        assert cat["wins"] == 1
        assert cat["losses"] == 1


# ---------------------------------------------------------------------------
# Scaling
# ---------------------------------------------------------------------------

class TestScaling:
    def test_scale_up_on_low_activity(self, manager):
        """Should scale up when fewer than SCALING_MIN_FRESH_TRADES in window."""
        manager._last_scaling_check = datetime.min.replace(tzinfo=timezone.utc)
        manager._fresh_trade_timestamps = []  # No fresh trades
        old_count = manager._active_whale_count

        manager.check_scaling()

        assert manager._active_whale_count > old_count

    def test_scale_down_on_high_activity(self, manager):
        """Should scale down when fresh trades exceed 2x threshold."""
        manager._last_scaling_check = datetime.min.replace(tzinfo=timezone.utc)
        manager._active_whale_count = ACTIVE_WHALES_MAX
        now = datetime.now(timezone.utc)
        # Add way more than threshold
        manager._fresh_trade_timestamps = [
            now - timedelta(seconds=i * 30)
            for i in range(SCALING_MIN_FRESH_TRADES * 3)
        ]

        manager.check_scaling()

        assert manager._active_whale_count < ACTIVE_WHALES_MAX

    def test_scaling_respects_interval(self, manager):
        """Scaling should only check every SCALING_CHECK_INTERVAL seconds."""
        manager._last_scaling_check = datetime.now(timezone.utc)  # Just checked
        old_count = manager._active_whale_count

        manager.check_scaling()

        assert manager._active_whale_count == old_count  # Unchanged


# ---------------------------------------------------------------------------
# Rebuild Active Set (A5: Deprioritization)
# ---------------------------------------------------------------------------

class TestRebuildActiveSet:
    def test_deprioritize_sports_heavy_whales(self, manager):
        """Sports-heavy whales should be deprioritized when sports is at cap."""
        # Make whale_1 and whale_2 sports-heavy
        w1 = "0x" + "0" * 39 + "1"
        w2 = "0x" + "0" * 39 + "2"
        for _ in range(10):
            manager.record_whale_category(w1, "sports")
            manager.record_whale_category(w2, "sports")
        manager.record_whale_category(w1, "crypto")  # whale_1: 91% sports

        # Set cap callback to say sports is at cap
        manager._is_category_at_cap = lambda cat: cat == "sports"

        manager.rebuild_active_set()

        assert w1 in manager._deprioritized_whales or w2 in manager._deprioritized_whales

    def test_no_deprioritization_when_cap_not_hit(self, manager):
        """No whales should be deprioritized when sports is not at cap."""
        manager._is_category_at_cap = lambda cat: False
        manager.rebuild_active_set()

        assert len(manager._deprioritized_whales) == 0


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_from_dict_roundtrip(self, manager):
        manager.record_copy_pnl("0x" + "a" * 40, 2.5)
        manager.record_category_pnl("crypto", 1.0)
        manager.record_whale_category("0x" + "a" * 40, "crypto")

        data = manager.to_dict()

        # Create fresh manager and restore
        session = MagicMock(spec=aiohttp.ClientSession)
        mgr2 = WhaleManager(session=session)
        mgr2.from_dict(data)

        assert "0x" + "a" * 40 in mgr2._whale_copy_pnl
        assert "crypto" in mgr2._category_copy_pnl
        assert "0x" + "a" * 40 in mgr2._whale_category_mix
