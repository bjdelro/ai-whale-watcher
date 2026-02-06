"""
Paper trading (dry run) mode for copy trading.
Simulates trades without executing, tracks hypothetical P&L.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .risk_manager import RiskManager, RiskLimits, TradeRecord
from ..database.db import Database
from ..database.models import CanonicalTrade, MarketRegistry, WalletStats
from ..scoring.copy_scorer import CopyScorer, CopyScore
from ..scoring.features import FeatureExtractor
from ..core.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


class PaperTrader:
    """
    Paper trading system for testing copy strategies without real money.

    Features:
    - Monitors for high-scoring copy signals
    - Simulates order execution
    - Tracks hypothetical P&L
    - Logs all signals and decisions
    - Generates reports
    """

    def __init__(
        self,
        db: Database,
        polymarket_client: PolymarketClient,
        risk_limits: Optional[RiskLimits] = None,
        log_dir: str = "paper_trades",
    ):
        """
        Initialize paper trader.

        Args:
            db: Database instance
            polymarket_client: Polymarket API client
            risk_limits: Risk control parameters
            log_dir: Directory for trade logs
        """
        self.db = db
        self.client = polymarket_client
        self.risk_manager = RiskManager(risk_limits)

        # Scoring
        self.feature_extractor = FeatureExtractor(db)
        self.copy_scorer = CopyScorer(db, self.feature_extractor)

        # Logging
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

        # Signal log file
        self._signal_log = self.log_dir / f"signals_{datetime.utcnow().strftime('%Y%m%d')}.jsonl"

        # Stats
        self._signals_seen = 0
        self._signals_passed = 0
        self._trades_simulated = 0
        self._start_time = datetime.utcnow()

    async def evaluate_trade(
        self,
        trade: CanonicalTrade,
        wallet_stats: Optional[WalletStats] = None,
        market: Optional[MarketRegistry] = None,
    ) -> Optional[CopyScore]:
        """
        Evaluate a trade for copy potential.

        Args:
            trade: The canonical trade to evaluate
            wallet_stats: Pre-fetched wallet stats
            market: Pre-fetched market info

        Returns:
            CopyScore if trade should be copied, None otherwise
        """
        self._signals_seen += 1

        # Score the trade
        score = await self.copy_scorer.score_trade(trade, wallet_stats)

        # Log the signal regardless of outcome
        self._log_signal(trade, score, market)

        # Check if it passes scoring threshold
        if not self.copy_scorer.should_alert(score):
            return None

        self._signals_passed += 1
        return score

    async def simulate_copy(
        self,
        trade: CanonicalTrade,
        score: CopyScore,
        market: Optional[MarketRegistry] = None,
    ) -> bool:
        """
        Simulate copying a trade (paper trade).

        Args:
            trade: The canonical trade to copy
            score: Copy score for this trade
            market: Market info

        Returns:
            True if paper trade was executed
        """
        features = score.features

        # Determine trade size (always use max_per_trade in paper mode)
        trade_size = self.risk_manager.limits.max_per_trade_usd

        # Get market name
        market_name = market.question if market else trade.condition_id[:30]

        # Check risk limits
        can_trade, reason = self.risk_manager.can_trade(
            copy_score=score.score,
            wallet_tier=features.wallet_tier.value,
            wallet_trades=features.wallet_total_trades,
            market_volume_24h=features.market_volume_24h or 0,
            hours_to_close=features.hours_to_close,
            market_id=trade.condition_id,
            trade_size_usd=trade_size,
            copied_wallet=trade.wallet_address,
            side=trade.side,
        )

        if not can_trade:
            logger.info(f"Trade blocked by risk manager: {reason}")
            self._log_decision(trade, score, "BLOCKED", reason)
            return False

        # Calculate shares (price is 0-1, so shares = usd / price)
        entry_price = trade.avg_price
        shares = trade_size / entry_price if entry_price > 0 else 0

        # Create trade record
        trade_record = TradeRecord(
            timestamp=datetime.utcnow(),
            market_id=trade.condition_id,
            market_name=market_name,
            outcome=trade.outcome or "YES",
            side="BUY",  # We always buy to copy
            price=entry_price,
            size_usd=trade_size,
            shares=shares,
            copy_score=score.score,
            copied_wallet=trade.wallet,
            is_paper=True,
        )

        # Record with risk manager
        self.risk_manager.record_trade(trade_record)
        self._trades_simulated += 1

        # Log the decision
        self._log_decision(trade, score, "EXECUTED", "Paper trade")

        logger.info(
            f"PAPER TRADE: {score.recommendation} - "
            f"BUY {trade.outcome or 'YES'} @ {entry_price:.2%} for ${trade_size:.2f} "
            f"(score={score.score:.0f}, wallet={trade.wallet[:8]}...)"
        )

        return True

    async def update_positions(self):
        """Update current prices for all open positions"""
        for market_id, position in list(self.risk_manager.positions.items()):
            try:
                # Get current orderbook
                orderbook = await self.client.get_orderbook(
                    # We need token_id, but we stored condition_id
                    # This is a simplification - in production we'd track token_id
                    token_id=market_id
                )
                if orderbook and orderbook.mid_price:
                    self.risk_manager.update_position_price(market_id, orderbook.mid_price)
            except Exception as e:
                logger.debug(f"Could not update price for {market_id}: {e}")

    async def check_for_exits(self):
        """
        Check if any positions should be closed.

        Exit conditions:
        - Market resolved
        - Take profit (e.g., 20% gain)
        - Stop loss (e.g., 30% loss)
        - Time-based (e.g., held for 24h)
        """
        now = datetime.utcnow()

        for market_id, position in list(self.risk_manager.positions.items()):
            should_exit = False
            reason = ""

            # Check unrealized P&L thresholds
            if position.current_price is not None:
                pnl_pct = (position.current_price - position.entry_price) / position.entry_price

                # Take profit at 20%
                if pnl_pct >= 0.20:
                    should_exit = True
                    reason = f"Take profit ({pnl_pct:.1%})"

                # Stop loss at 30%
                elif pnl_pct <= -0.30:
                    should_exit = True
                    reason = f"Stop loss ({pnl_pct:.1%})"

            # Time-based exit (24h)
            hours_held = (now - position.entry_time).total_seconds() / 3600
            if hours_held >= 24:
                should_exit = True
                reason = f"Time exit ({hours_held:.1f}h held)"

            if should_exit:
                exit_price = position.current_price or position.entry_price
                self.risk_manager.close_position(market_id, exit_price, reason)

    def _log_signal(
        self,
        trade: CanonicalTrade,
        score: CopyScore,
        market: Optional[MarketRegistry],
    ):
        """Log a signal to the signal log file"""
        features = score.features

        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "signal",
            "trade": {
                "wallet": trade.wallet,
                "condition_id": trade.condition_id,
                "side": trade.side,
                "outcome": trade.outcome,
                "price": trade.avg_price,
                "size_usd": trade.total_usd,
                "intent": features.trade_intent,
            },
            "score": {
                "value": score.score,
                "confidence": score.confidence,
                "recommendation": score.recommendation,
                "breakdown": score.breakdown,
            },
            "wallet_profile": {
                "tier": features.wallet_tier.value,
                "win_rate_1h": features.wallet_win_rate_1h,
                "sharpe_1h": features.wallet_sharpe_1h,
                "total_trades": features.wallet_total_trades,
            },
            "market": {
                "name": market.question[:100] if market else None,
                "volume_24h": features.market_volume_24h,
                "hours_to_close": features.hours_to_close,
            },
        }

        with open(self._signal_log, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def _log_decision(
        self,
        trade: CanonicalTrade,
        score: CopyScore,
        decision: str,
        reason: str,
    ):
        """Log a trading decision"""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "decision",
            "decision": decision,
            "reason": reason,
            "trade_wallet": trade.wallet[:12],
            "score": score.score,
            "recommendation": score.recommendation,
        }

        with open(self._signal_log, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def get_stats(self) -> dict:
        """Get paper trading statistics"""
        risk_stats = self.risk_manager.get_stats()

        runtime = (datetime.utcnow() - self._start_time).total_seconds() / 3600

        return {
            "runtime_hours": round(runtime, 2),
            "signals_seen": self._signals_seen,
            "signals_passed": self._signals_passed,
            "trades_simulated": self._trades_simulated,
            "pass_rate": self._signals_passed / max(1, self._signals_seen),
            **risk_stats,
        }

    def print_report(self):
        """Print a summary report"""
        stats = self.get_stats()

        print("\n" + "=" * 60)
        print("PAPER TRADING REPORT")
        print("=" * 60)
        print(f"Runtime: {stats['runtime_hours']:.1f} hours")
        print(f"\nSignals:")
        print(f"  Seen: {stats['signals_seen']}")
        print(f"  Passed filter: {stats['signals_passed']} ({stats['pass_rate']:.1%})")
        print(f"  Trades executed: {stats['trades_simulated']}")
        print(f"\nPositions:")
        print(f"  Open: {stats['positions']}")
        print(f"  Exposure: ${stats['total_exposure']:.2f} / ${stats['max_exposure']:.2f}")
        print(f"\nP&L:")
        print(f"  Realized: ${stats['realized_pnl']:+.2f}")
        print(f"  Unrealized: ${stats['unrealized_pnl']:+.2f}")
        print(f"  Daily: ${stats['daily_pnl']:+.2f}")
        print(f"  Total: ${stats['realized_pnl'] + stats['unrealized_pnl']:+.2f}")

        if stats['halted']:
            print(f"\n** TRADING HALTED: {stats['halt_reason']} **")

        print("=" * 60)

        # Print open positions
        if self.risk_manager.positions:
            print("\nOPEN POSITIONS:")
            for pos in self.risk_manager.positions.values():
                pnl_str = f"${pos.unrealized_pnl:+.2f}" if pos.unrealized_pnl else "N/A"
                print(f"  {pos.market_name[:40]}...")
                print(f"    {pos.outcome} @ {pos.entry_price:.2%} -> {pos.current_price or 0:.2%} | P&L: {pnl_str}")

        print()
