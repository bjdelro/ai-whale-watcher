"""
C2: Correlated position detection via LLM.

Every 6 hours, feeds all open position market titles to Haiku and asks it
to identify clusters of correlated bets. Flags over-concentrated exposure
in Slack.
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
You are a prediction market portfolio analyst. Given a list of open positions,
identify clusters of correlated bets — positions that depend on the same
underlying event or thesis.

For each cluster found, explain:
- Which positions are correlated and why
- The combined exposure at risk
- A recommendation: reduce, hold, or acceptable

Respond with ONLY valid JSON in this format:
{
  "clusters": [
    {
      "positions": [1, 3],
      "thesis": "Both depend on Trump/GOP performance",
      "risk": "high",
      "recommendation": "Reduce combined exposure — close the weaker conviction position"
    }
  ],
  "summary": "2 of 5 positions are correlated. Consider diversifying."
}

If no correlations are found, return {"clusters": [], "summary": "No significant correlations detected."}"""

CHECK_INTERVAL_HOURS = 6
CORRELATION_EXPOSURE_THRESHOLD = 0.30  # Flag if correlated cluster > 30% of total


class CorrelationDetector:
    """Periodically detects correlated positions via LLM analysis."""

    def __init__(
        self,
        llm_client: LLMClient,
        get_open_positions: Callable[[], list],
        get_total_exposure: Callable[[], float],
        reporter: Optional["Reporter"] = None,
    ):
        self._llm = llm_client
        self._get_open_positions = get_open_positions
        self._get_total_exposure = get_total_exposure
        self._reporter = reporter
        self._last_check: Optional[datetime] = None
        self._last_result: Optional[dict] = None

    async def run_periodic(self, running_check: Callable[[], bool]):
        """Run correlation checks every 6 hours. Call as an asyncio task."""
        while running_check():
            await asyncio.sleep(60)  # Check every minute if it's time

            if not self._llm.available:
                await asyncio.sleep(3600)
                continue

            now = datetime.now(timezone.utc)
            if self._last_check and (now - self._last_check).total_seconds() < CHECK_INTERVAL_HOURS * 3600:
                continue

            await self.check_correlations()
            self._last_check = now

    async def check_correlations(self) -> Optional[dict]:
        """Run a correlation check on current open positions."""
        positions = self._get_open_positions()
        if len(positions) < 2:
            return None

        # Build position list for the LLM
        pos_lines = []
        pos_map = {}  # index -> position object
        for i, pos in enumerate(positions, 1):
            cost = getattr(pos, "live_cost_usd", None) or pos.copy_amount_usd
            line = (
                f"{i}. \"{pos.market_title}\" — {pos.outcome} "
                f"@ {pos.entry_price:.0%} (${cost:.2f})"
            )
            pos_lines.append(line)
            pos_map[i] = pos

        prompt = f"Open positions:\n" + "\n".join(pos_lines)
        result = await self._llm.complete_json(prompt, system=SYSTEM_PROMPT, max_tokens=1024)

        if not result:
            return None

        self._last_result = result
        clusters = result.get("clusters", [])

        if not clusters:
            logger.debug("Correlation check: no clusters found")
            return result

        # Check if any cluster exceeds the exposure threshold
        total_exp = self._get_total_exposure()
        for cluster in clusters:
            pos_indices = cluster.get("positions", [])
            cluster_exposure = 0.0
            for idx in pos_indices:
                pos = pos_map.get(idx)
                if pos:
                    cluster_exposure += getattr(pos, "live_cost_usd", None) or pos.copy_amount_usd

            cluster["exposure_usd"] = cluster_exposure
            cluster_pct = cluster_exposure / total_exp if total_exp > 0 else 0
            cluster["exposure_pct"] = cluster_pct

            if cluster_pct > CORRELATION_EXPOSURE_THRESHOLD:
                cluster["flagged"] = True
                logger.warning(
                    f"Correlated positions: {cluster.get('thesis', '?')} "
                    f"| ${cluster_exposure:.2f} ({cluster_pct:.0%} of portfolio) "
                    f"| {cluster.get('recommendation', '')}"
                )

                # Alert via Slack
                if self._reporter:
                    summary = result.get("summary", "Correlated positions detected")
                    msg = (
                        f"*Correlation Alert*\n"
                        f"_{cluster.get('thesis', 'Correlated positions')}_\n"
                        f"Exposure: ${cluster_exposure:.2f} ({cluster_pct:.0%})\n"
                        f"Recommendation: {cluster.get('recommendation', 'Review manually')}"
                    )
                    await self._reporter.send_slack(text=msg)

        logger.info(
            f"Correlation check: {len(clusters)} cluster(s) found across "
            f"{len(positions)} positions"
        )
        return result

    def get_last_result(self) -> Optional[dict]:
        return self._last_result
