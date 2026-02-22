"""
C1: Rich market tagging via LLM.

When a new market is first seen, calls Haiku to produce structured tags:
subcategories, insider_likelihood, efficiency_estimate. Results are cached
permanently and fed into copy scoring as bonus/penalty.

Runs lazily (on first encounter) rather than on a timer. New markets are
batched to reduce API costs.
"""

import logging
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

from .llm_client import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a prediction market analyst. Given a market title and optional description,
produce a JSON analysis with these fields:

- subcategories: list of 1-3 relevant topic tags (e.g. ["crypto", "regulatory", "executive_action"])
- insider_likelihood: one of "none", "low", "medium", "high" — how likely is it that
  insiders have non-public information that would let them profit on this market?
- efficiency_estimate: one of "high", "medium", "low" — how well-priced is this market
  likely to be? Sports with professional odds = high. Niche politics = low.
- reasoning: one sentence explaining your insider_likelihood and efficiency assessment.

Respond with ONLY valid JSON, no other text."""

BATCH_PROMPT_TEMPLATE = """\
Analyze the following prediction markets. For each, produce the structured JSON analysis.
Return a JSON array with one object per market, in the same order.

Markets:
{markets_text}

Respond with ONLY a JSON array, no other text."""

# Score adjustments based on LLM tags
INSIDER_SCORE_ADJUSTMENT = {
    "none": -3,
    "low": 0,
    "medium": +3,
    "high": +5,
}
EFFICIENCY_SCORE_ADJUSTMENT = {
    "high": -5,
    "medium": 0,
    "low": +4,
}


@dataclass
class MarketTag:
    """LLM-generated tags for a market."""
    condition_id: str
    title: str
    subcategories: List[str] = field(default_factory=list)
    insider_likelihood: str = "low"  # none/low/medium/high
    efficiency_estimate: str = "medium"  # high/medium/low
    reasoning: str = ""

    @property
    def score_adjustment(self) -> float:
        """Net score adjustment for copy scoring (can be negative)."""
        return (
            INSIDER_SCORE_ADJUSTMENT.get(self.insider_likelihood, 0)
            + EFFICIENCY_SCORE_ADJUSTMENT.get(self.efficiency_estimate, 0)
        )


class MarketTagger:
    """Tags markets with LLM-generated intelligence. Cache is permanent."""

    def __init__(self, llm_client: LLMClient):
        self._llm = llm_client
        self._cache: Dict[str, MarketTag] = {}  # condition_id -> MarketTag
        self._pending: Dict[str, str] = {}  # condition_id -> title (awaiting batch)
        self._batch_size = 10

    async def get_tag(self, condition_id: str, title: str, description: str = "") -> Optional[MarketTag]:
        """Get or create a market tag. Returns cached result or None if LLM unavailable."""
        if condition_id in self._cache:
            return self._cache[condition_id]

        if not self._llm.available:
            return None

        # Queue for batching
        self._pending[condition_id] = title
        if description:
            self._pending[condition_id] = f"{title} — {description[:200]}"

        # Process batch if we have enough
        if len(self._pending) >= self._batch_size:
            await self.process_pending()

        return self._cache.get(condition_id)

    async def process_pending(self):
        """Process all pending markets in a single batched LLM call."""
        if not self._pending or not self._llm.available:
            return

        items = list(self._pending.items())
        self._pending.clear()

        if len(items) == 1:
            # Single market — simpler prompt
            cid, title = items[0]
            await self._tag_single(cid, title)
        else:
            # Batch
            await self._tag_batch(items)

    async def _tag_single(self, condition_id: str, title: str):
        """Tag a single market via LLM."""
        prompt = f"Market: {title}"
        result = await self._llm.complete_json(prompt, system=SYSTEM_PROMPT, max_tokens=256)

        if result:
            tag = MarketTag(
                condition_id=condition_id,
                title=title,
                subcategories=result.get("subcategories", []),
                insider_likelihood=result.get("insider_likelihood", "low"),
                efficiency_estimate=result.get("efficiency_estimate", "medium"),
                reasoning=result.get("reasoning", ""),
            )
            self._cache[condition_id] = tag
            logger.debug(
                f"LLM tagged: {title[:40]}... → insider={tag.insider_likelihood}, "
                f"efficiency={tag.efficiency_estimate}, adj={tag.score_adjustment:+d}"
            )
        else:
            # Cache a default so we don't re-query
            self._cache[condition_id] = MarketTag(condition_id=condition_id, title=title)

    async def _tag_batch(self, items: list):
        """Tag multiple markets in a single LLM call."""
        markets_text = "\n".join(
            f"{i+1}. {title}" for i, (_, title) in enumerate(items)
        )
        prompt = BATCH_PROMPT_TEMPLATE.format(markets_text=markets_text)
        result = await self._llm.complete_json(prompt, system=SYSTEM_PROMPT, max_tokens=256 * len(items))

        if result and isinstance(result, list):
            for i, (cid, title) in enumerate(items):
                if i < len(result) and isinstance(result[i], dict):
                    entry = result[i]
                    tag = MarketTag(
                        condition_id=cid,
                        title=title,
                        subcategories=entry.get("subcategories", []),
                        insider_likelihood=entry.get("insider_likelihood", "low"),
                        efficiency_estimate=entry.get("efficiency_estimate", "medium"),
                        reasoning=entry.get("reasoning", ""),
                    )
                    self._cache[cid] = tag
                else:
                    self._cache[cid] = MarketTag(condition_id=cid, title=title)

            logger.info(f"LLM batch-tagged {len(items)} markets")
        else:
            # Fallback: cache defaults
            for cid, title in items:
                self._cache[cid] = MarketTag(condition_id=cid, title=title)
            logger.warning(f"LLM batch tagging failed for {len(items)} markets — using defaults")

    def get_score_adjustment(self, condition_id: str) -> float:
        """Get the LLM-derived score adjustment for a market. 0 if not tagged."""
        tag = self._cache.get(condition_id)
        return tag.score_adjustment if tag else 0.0

    def to_dict(self) -> dict:
        """Serialize tag cache for persistence."""
        return {
            cid: asdict(tag) for cid, tag in self._cache.items()
        }

    def from_dict(self, data: dict):
        """Restore tag cache from persistence."""
        for cid, tag_dict in data.items():
            try:
                self._cache[cid] = MarketTag(**tag_dict)
            except (TypeError, KeyError):
                pass

    def get_stats(self) -> dict:
        return {
            "markets_tagged": len(self._cache),
            "pending": len(self._pending),
        }
