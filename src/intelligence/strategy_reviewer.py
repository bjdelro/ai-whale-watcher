"""
C3: Periodic strategy review via LLM.

Every 24 hours, feeds the LLM the full performance report — per-whale P&L,
per-category P&L, win rates, open positions, current config — and asks
for actionable recommendations. Posts results to Slack.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from .llm_client import LLMClient

if TYPE_CHECKING:
    from src.reporting.reporter import Reporter

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a quantitative trading advisor for a Polymarket whale copy-trading system.
You will receive a performance snapshot including per-whale and per-category P&L,
open positions, win rates, and system configuration.

Analyze the data and provide 3-5 specific, actionable recommendations. Focus on:
1. Which whales to stop/start copying based on recent performance
2. Category allocation issues (over-exposed to sports? under-exposed to crypto?)
3. Position concentration risks (too many bets on same thesis/timeframe?)
4. Parameter tuning (stop-loss too tight/loose? position sizing issues?)
5. Any patterns that suggest the system is making systematic errors

Be specific with numbers. Reference actual whale names and market titles.
Keep each recommendation to 2-3 sentences max.

Respond with ONLY valid JSON:
{
  "recommendations": [
    {
      "priority": "high",
      "category": "whale_selection",
      "action": "Stop copying WhaleX — 2W/8L on sports, dragging overall P&L",
      "reasoning": "WhaleX has -$4.20 copy P&L with 20% win rate. Only profitable in crypto (2/2 wins)."
    }
  ],
  "overall_assessment": "One sentence summary of portfolio health."
}"""

CHECK_INTERVAL_HOURS = 24


class StrategyReviewer:
    """Periodically reviews strategy performance via LLM and posts recommendations."""

    def __init__(
        self,
        llm_client: LLMClient,
        get_report_data: Callable[[], dict],
        get_portfolio_stats: Callable[[], dict],
        reporter: Optional["Reporter"] = None,
    ):
        self._llm = llm_client
        self._get_report_data = get_report_data
        self._get_portfolio_stats = get_portfolio_stats
        self._reporter = reporter
        self._last_check: Optional[datetime] = None
        self._last_result: Optional[dict] = None

    async def run_periodic(self, running_check: Callable[[], bool]):
        """Run strategy review every 24 hours. Call as an asyncio task."""
        # Wait 1 hour after startup before first review (need data to accumulate)
        await asyncio.sleep(3600)

        while running_check():
            if not self._llm.available:
                await asyncio.sleep(3600)
                continue

            now = datetime.now(timezone.utc)
            if self._last_check and (now - self._last_check).total_seconds() < CHECK_INTERVAL_HOURS * 3600:
                await asyncio.sleep(300)
                continue

            await self.run_review()
            self._last_check = now
            await asyncio.sleep(300)

    async def run_review(self) -> Optional[dict]:
        """Run a strategy review and post results."""
        if not self._llm.available:
            return None

        data = self._get_report_data()
        stats = self._get_portfolio_stats()

        prompt = self._build_prompt(data, stats)
        result = await self._llm.complete_json(prompt, system=SYSTEM_PROMPT, max_tokens=1500)

        if not result:
            logger.warning("Strategy review: LLM returned no result")
            return None

        self._last_result = result
        recommendations = result.get("recommendations", [])
        assessment = result.get("overall_assessment", "No assessment")

        # Log recommendations
        logger.info(f"\n{'='*60}")
        logger.info(f"STRATEGY REVIEW — {assessment}")
        logger.info(f"{'='*60}")
        for i, rec in enumerate(recommendations, 1):
            priority = rec.get("priority", "?").upper()
            action = rec.get("action", "?")
            logger.info(f"  [{priority}] {i}. {action}")
        logger.info(f"{'='*60}\n")

        # Post to Slack
        if self._reporter and recommendations:
            lines = [f"*Strategy Review*\n_{assessment}_\n"]
            for i, rec in enumerate(recommendations, 1):
                priority_emoji = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\U0001f7e2"}.get(
                    rec.get("priority", ""), "\u26aa"
                )
                lines.append(f"{priority_emoji} {rec.get('action', '?')}")
            await self._reporter.send_slack(text="\n".join(lines))

        return result

    def _build_prompt(self, data: dict, stats: dict) -> str:
        """Build the performance snapshot prompt for the LLM."""
        sections = []

        # Portfolio overview
        sections.append(
            f"PORTFOLIO OVERVIEW:\n"
            f"  Realized P&L: ${stats.get('realized_pnl', 0):+.2f}\n"
            f"  Unrealized P&L: ${stats.get('unrealized_pnl', 0):+.2f}\n"
            f"  Win Rate: {stats.get('win_rate', 0)*100:.0f}% "
            f"({stats.get('positions_won', 0)}W/{stats.get('positions_lost', 0)}L)\n"
            f"  Open Positions: {stats.get('open_positions', 0)}\n"
            f"  Exposure: ${stats.get('open_exposure', 0):.2f}"
        )

        # Per-whale P&L
        whale_pnl = data.get("whale_copy_pnl", {})
        if whale_pnl:
            whales = data.get("whales", {})
            all_whales = data.get("all_whales", {})
            lines = ["PER-WHALE COPY P&L:"]
            sorted_whales = sorted(whale_pnl.items(), key=lambda x: x[1]["realized_pnl"], reverse=True)
            for addr, pnl_data in sorted_whales[:15]:
                name = addr[:12]
                for w in list(whales.values()) + list(all_whales.values()):
                    if w.address.lower() == addr.lower():
                        name = w.name
                        break
                wr = pnl_data["wins"] / pnl_data["copies"] * 100 if pnl_data["copies"] > 0 else 0
                pruned = " [PRUNED]" if addr in data.get("pruned_whales", set()) else ""
                lines.append(
                    f"  {name}: ${pnl_data['realized_pnl']:+.2f} "
                    f"({pnl_data['copies']} copies, {wr:.0f}% win){pruned}"
                )
            sections.append("\n".join(lines))

        # Per-category P&L
        cat_pnl = data.get("category_copy_pnl", {})
        if cat_pnl:
            lines = ["PER-CATEGORY P&L:"]
            for cat, cat_data in sorted(cat_pnl.items(), key=lambda x: x[1]["realized_pnl"], reverse=True):
                wr = cat_data["wins"] / cat_data["copies"] * 100 if cat_data["copies"] > 0 else 0
                lines.append(
                    f"  {cat}: ${cat_data['realized_pnl']:+.2f} "
                    f"({cat_data['copies']} copies, {wr:.0f}% win)"
                )
            sections.append("\n".join(lines))

        # Open positions
        positions = data.get("copied_positions", {})
        current_prices = data.get("current_prices", {})
        open_pos = [p for p in positions.values() if p.status == "open"]
        if open_pos:
            lines = ["OPEN POSITIONS:"]
            for pos in open_pos[:20]:
                current = current_prices.get(pos.token_id, pos.entry_price)
                cost = getattr(pos, "live_cost_usd", None) or pos.copy_amount_usd
                eff_shares = getattr(pos, "live_shares", None) or pos.shares
                pnl = (current * eff_shares) - cost
                cat = getattr(pos, "category", "?") or "?"
                lines.append(
                    f"  [{cat}] {pos.outcome} @ {pos.entry_price:.0%}->{current:.0%} "
                    f"(${pnl:+.2f}) | {pos.whale_name} | {pos.market_title[:50]}"
                )
            sections.append("\n".join(lines))

        return "\n\n".join(sections)

    def get_last_result(self) -> Optional[dict]:
        return self._last_result
