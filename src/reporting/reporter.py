"""
Reporting module — Slack alerts and periodic status reports.

Decoupled from trading logic: receives all data via callbacks and
read-only references. No back-references to the orchestrator.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, Set, List, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from src.execution.live_trader import LiveTrader

logger = logging.getLogger(__name__)


class Reporter:
    """Handles Slack alerts, periodic console reports, and final summaries."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        slack_webhook_url: Optional[str],
        get_portfolio_stats: Callable[[], dict],
        get_report_data: Callable[[], dict],
        config: dict,
    ):
        """
        Args:
            session: Shared aiohttp session for Slack webhooks.
            slack_webhook_url: Slack webhook URL (None to disable).
            get_portfolio_stats: Callback returning portfolio stats dict.
            get_report_data: Callback returning full report data dict with keys:
                - copied_positions: Dict[str, CopiedPosition]
                - current_prices: Dict[str, float]
                - whale_copies_count, unusual_copies_count, arb_copies_count: int
                - whale_copy_pnl: Dict[str, dict]
                - pruned_whales: Set[str]
                - whales: Dict[str, WhaleWallet]
                - all_whales: Dict[str, WhaleWallet]
                - live_trader: Optional[LiveTrader]
                - start_time: datetime
            config: Dict with keys:
                - live_trading_enabled: bool
                - live_dry_run: bool
                - max_exposure: float
        """
        self._session = session
        self._slack_webhook_url = slack_webhook_url
        self._get_portfolio_stats = get_portfolio_stats
        self._get_report_data = get_report_data
        self._config = config

    # ------------------------------------------------------------------
    # Slack primitives
    # ------------------------------------------------------------------

    async def send_slack(self, text: str = "", blocks: list = None):
        """Send a message to Slack via webhook."""
        if not self._slack_webhook_url or not self._session:
            return

        payload = {}
        if blocks:
            payload["blocks"] = blocks
        if text:
            payload["text"] = text

        try:
            async with self._session.post(
                self._slack_webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Slack webhook returned {resp.status}")
        except Exception as e:
            logger.debug(f"Slack alert failed: {e}")

    # ------------------------------------------------------------------
    # Trade alerts
    # ------------------------------------------------------------------

    async def slack_trade_alert(self, paper_trade, position=None):
        """Send Slack alert for a new copied trade."""
        mode = self._mode_label()
        emoji = "\U0001f7e2" if paper_trade.side == "BUY" else "\U0001f534"
        stats = self._get_portfolio_stats()

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {mode} {paper_trade.side} \u2014 Copied {paper_trade.whale_name}", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Market:*\n{paper_trade.market_title[:60]}"},
                    {"type": "mrkdwn", "text": f"*Outcome:*\n{paper_trade.outcome} @ {paper_trade.price:.1%}"},
                    {"type": "mrkdwn", "text": f"*Whale Size:*\n${paper_trade.whale_size:,.0f}"},
                    {"type": "mrkdwn", "text": f"*Our Size:*\n${paper_trade.our_size:.2f}"},
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": (
                    f"Exposure: ${stats['open_exposure']:.2f}/${self._config['max_exposure']:.0f} | "
                    f"Open: {stats['open_positions']} | "
                    f"P&L: ${stats['total_pnl']:+.2f} | "
                    f"W/L: {stats['positions_won']}/{stats['positions_lost']}"
                )}]
            },
        ]
        await self.send_slack(blocks=blocks)

    async def slack_exit_alert(self, position, pnl: float, reason: str):
        """Send Slack alert when a position is closed."""
        mode = self._mode_label()
        emoji = "\U0001f7e2" if pnl > 0 else "\U0001f534" if pnl < 0 else "\u26aa"
        stats = self._get_portfolio_stats()

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {mode} EXIT \u2014 {reason}", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Market:*\n{position.market_title[:60]}"},
                    {"type": "mrkdwn", "text": f"*P&L:*\n${pnl:+.2f}"},
                    {"type": "mrkdwn", "text": f"*Entry:*\n{position.entry_price:.1%}"},
                    {"type": "mrkdwn", "text": f"*Exit:*\n{position.exit_price:.1%}"},
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": (
                    f"Whale: {position.whale_name} | "
                    f"Total P&L: ${stats['total_pnl']:+.2f} | "
                    f"W/L: {stats['positions_won']}/{stats['positions_lost']} | "
                    f"Win Rate: {stats['win_rate']*100:.0f}%"
                )}]
            },
        ]
        await self.send_slack(blocks=blocks)

    async def slack_periodic_report(self):
        """Send periodic portfolio report to Slack."""
        stats = self._get_portfolio_stats()
        data = self._get_report_data()
        start_time = data["start_time"]
        runtime = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600
        hourly_return = stats["realized_pnl"] / runtime if runtime > 0 else 0

        mode = self._mode_label()

        usdc_str = f"${stats['usdc_balance']:.2f}" if stats['usdc_balance'] is not None else "N/A"
        total_str = f"${stats['total_value']:.2f}" if stats['total_value'] is not None else "N/A"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"\U0001f40b {mode} Portfolio Report ({runtime:.1f}h)", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*USDC Cash:*\n{usdc_str}"},
                    {"type": "mrkdwn", "text": f"*Open Positions:*\n{stats['open_positions']} (${stats['open_market_value']:.2f} mkt val)"},
                    {"type": "mrkdwn", "text": f"*Total Portfolio:*\n{total_str}"},
                    {"type": "mrkdwn", "text": f"*Total P&L:*\n${stats['total_pnl']:+.2f}"},
                ]
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Realized:*\n${stats['realized_pnl']:+.2f}"},
                    {"type": "mrkdwn", "text": f"*Unrealized:*\n${stats['unrealized_pnl']:+.2f}"},
                    {"type": "mrkdwn", "text": f"*Win Rate:*\n{stats['win_rate']*100:.0f}% ({stats['positions_won']}W/{stats['positions_lost']}L)"},
                    {"type": "mrkdwn", "text": f"*Session Rate:*\n${hourly_return:+.2f}/hr"},
                ]
            },
        ]

        # Add live order stats if enabled
        live_trader = data.get("live_trader")
        if live_trader:
            live_stats = live_trader.get_stats()
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": (
                    f"*\U0001f4b0 Live Trading ({live_stats['mode']}):*\n"
                    f"Orders: {live_stats['orders_submitted']} submitted, {live_stats['orders_filled']} filled"
                )}
            })

        await self.send_slack(blocks=blocks)

    # ------------------------------------------------------------------
    # Console reports
    # ------------------------------------------------------------------

    async def periodic_report(self, running_check: Callable[[], bool]):
        """Print status report every 5 minutes. Runs as an asyncio task."""
        while running_check():
            await __import__("asyncio").sleep(300)

            data = self._get_report_data()
            stats = self._get_portfolio_stats()
            start_time = data["start_time"]
            runtime = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600

            # Calculate return percentage
            copied_positions = data["copied_positions"]
            total_invested = stats["open_exposure"] + sum(
                (p.live_cost_usd or p.copy_amount_usd) for p in copied_positions.values() if p.status == "closed"
            )
            return_pct = (stats["total_pnl"] / total_invested * 100) if total_invested > 0 else 0

            # Projections based on realized P&L
            hourly_return = stats["realized_pnl"] / runtime if runtime > 0 else 0
            daily_projected = hourly_return * 24

            whale_copies = data["whale_copies_count"]
            unusual_copies = data["unusual_copies_count"]
            arb_copies = data["arb_copies_count"]

            mode_label = self._mode_label()

            usdc_str = f"${stats['usdc_balance']:.2f}" if stats['usdc_balance'] is not None else "N/A"
            total_str = f"${stats['total_value']:.2f}" if stats['total_value'] is not None else "N/A"

            logger.info(
                f"\n{'='*60}\n"
                f"\U0001f40b WHALE COPY TRADING REPORT [{mode_label}] ({runtime:.1f}h runtime)\n"
                f"{'='*60}\n"
                f"\U0001f4b0 PORTFOLIO\n"
                f"   USDC Cash:       {usdc_str}\n"
                f"   Open Positions:  {stats['open_positions']} (cost: ${stats['open_cost']:.2f} \u2192 mkt value: ${stats['open_market_value']:.2f})\n"
                f"   Total Value:     {total_str}\n"
                f"{'='*60}\n"
                f"\U0001f4ca P&L\n"
                f"   Realized (closed):   ${stats['realized_pnl']:+.2f}  ({stats['positions_won']}W / {stats['positions_lost']}L, {stats['win_rate']*100:.0f}% win rate)\n"
                f"   Unrealized (open):   ${stats['unrealized_pnl']:+.2f}\n"
                f"   Total P&L:           ${stats['total_pnl']:+.2f}\n"
                f"   Session rate:        ${hourly_return:+.2f}/hr\n"
                f"{'='*60}"
            )

            # Show open positions sorted by P&L
            current_prices = data["current_prices"]
            open_positions = [p for p in copied_positions.values() if p.status == "open"]
            if open_positions:
                pos_with_pnl = []
                for pos in open_positions:
                    current = current_prices.get(pos.token_id, pos.entry_price)
                    effective_shares = pos.live_shares or pos.shares
                    cost = pos.live_cost_usd or pos.copy_amount_usd
                    mkt_val = current * effective_shares
                    pnl = mkt_val - cost
                    pos_with_pnl.append((pos, current, cost, mkt_val, pnl))

                pos_with_pnl.sort(key=lambda x: x[4], reverse=True)

                logger.info(f"\U0001f4c8 OPEN POSITIONS ({len(open_positions)}):")
                for pos, current, cost, mkt_val, pnl in pos_with_pnl[:8]:
                    emoji = "\U0001f7e2" if pnl > 0 else "\U0001f534" if pnl < 0 else "\u26aa"
                    logger.info(
                        f"   {emoji} {pos.outcome} @ {pos.entry_price:.0%}\u2192{current:.0%} "
                        f"| cost ${cost:.2f} \u2192 ${mkt_val:.2f} ({pnl:+.2f}) "
                        f"| {pos.market_title[:35]}..."
                    )
                if len(open_positions) > 8:
                    logger.info(f"   ... and {len(open_positions) - 8} more")

            # Per-whale copy P&L leaderboard
            whale_copy_pnl = data["whale_copy_pnl"]
            if whale_copy_pnl:
                sorted_whales = sorted(
                    whale_copy_pnl.items(),
                    key=lambda x: x[1]["realized_pnl"],
                    reverse=True,
                )
                logger.info(f"\U0001f40b PER-WHALE COPY P&L ({len(sorted_whales)} whales):")
                whales = data["whales"]
                all_whales = data.get("all_whales", {})
                pruned_whales = data["pruned_whales"]
                for addr, pnl_data in sorted_whales[:10]:
                    name = addr[:12]
                    for w in list(whales.values()) + list(all_whales.values()):
                        if w.address.lower() == addr.lower():
                            name = w.name
                            break
                    wr = pnl_data["wins"] / pnl_data["copies"] * 100 if pnl_data["copies"] > 0 else 0
                    pruned_tag = " [PRUNED]" if addr in pruned_whales else ""
                    emoji = "\U0001f7e2" if pnl_data["realized_pnl"] > 0 else "\U0001f534" if pnl_data["realized_pnl"] < 0 else "\u26aa"
                    logger.info(
                        f"   {emoji} {name}: ${pnl_data['realized_pnl']:+.2f} "
                        f"({pnl_data['copies']} copies, {wr:.0f}% win){pruned_tag}"
                    )
                if pruned_whales:
                    logger.info(f"   \U0001f6ab {len(pruned_whales)} whale(s) pruned for poor performance")

            logger.info(f"{'='*60}\n")

    def print_final_report(self, running_check: Callable[[], bool] = None):
        """Print final summary when shutting down."""
        data = self._get_report_data()
        stats = self._get_portfolio_stats()
        start_time = data["start_time"]
        runtime = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600
        total_copies = data["whale_copies_count"] + data["unusual_copies_count"] + data["arb_copies_count"]

        mode_label = self._mode_label()
        logger.info(
            f"\n{'='*60}\n"
            f"\U0001f40b FINAL WHALE COPY TRADING REPORT [{mode_label}]\n"
            f"{'='*60}\n"
            f"Runtime: {runtime:.2f} hours\n"
            f"Total trades copied: {total_copies}\n"
            f"{'='*60}\n"
            f"\U0001f4ca POSITIONS\n"
            f"   Opened: {stats['open_positions'] + stats['closed_positions']}\n"
            f"   Closed: {stats['closed_positions']}\n"
            f"   Still Open: {stats['open_positions']} (${stats['open_exposure']:.2f})\n"
            f"{'='*60}\n"
            f"\U0001f4b0 FINAL P&L ({mode_label})\n"
            f"   Realized:   ${stats['realized_pnl']:+.2f}\n"
            f"   Unrealized: ${stats['unrealized_pnl']:+.2f}\n"
            f"   Total:      ${stats['total_pnl']:+.2f}\n"
            f"   Win Rate:   {stats['win_rate']*100:.1f}% ({stats['positions_won']}W / {stats['positions_lost']}L)\n"
            f"{'='*60}"
        )

        # List remaining open positions
        copied_positions = data["copied_positions"]
        current_prices = data["current_prices"]
        open_positions = [p for p in copied_positions.values() if p.status == "open"]
        if open_positions:
            logger.info(f"\U0001f4c8 REMAINING {mode_label} POSITIONS:")
            for pos in open_positions:
                current = current_prices.get(pos.token_id, pos.entry_price)
                effective_shares = pos.live_shares or pos.shares
                cost = pos.live_cost_usd or pos.copy_amount_usd
                unrealized = (current * effective_shares) - cost
                logger.info(
                    f"   {pos.outcome} @ {pos.entry_price:.1%} | ${unrealized:+.2f} | "
                    f"{pos.market_title[:40]}... | Whale: {pos.whale_name}"
                )
            logger.info(f"{'='*60}")

        # Show order stats if in live mode
        live_trader = data.get("live_trader")
        if self._config.get("live_trading_enabled") and live_trader:
            live_stats = live_trader.get_stats()
            logger.info(
                f"\U0001f4e6 ORDER STATS: {live_stats['orders_submitted']} submitted, "
                f"{live_stats['orders_filled']} filled, {live_stats['orders_failed']} failed"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mode_label(self) -> str:
        if self._config.get("live_trading_enabled"):
            return "DRY RUN" if self._config.get("live_dry_run") else "LIVE"
        return "PAPER"
