"""
Lightweight LLM client for intelligence features.

Uses the Anthropic API with Haiku for cost efficiency (~$0.25/M input tokens).
All calls are async-compatible and include retry logic.
"""

import json
import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_RETRIES = 2


class LLMClient:
    """Thin async wrapper around the Anthropic Messages API."""

    def __init__(self, session: aiohttp.ClientSession, api_key: Optional[str] = None):
        self._session = session
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._calls_made = 0
        self._tokens_used = 0

    @property
    def available(self) -> bool:
        """Check if the LLM client has an API key configured."""
        return bool(self._api_key)

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 1024,
        model: str = DEFAULT_MODEL,
    ) -> Optional[str]:
        """Send a prompt to the Anthropic API and return the text response.

        Returns None on failure (missing key, API error, timeout).
        """
        if not self._api_key:
            logger.debug("LLM: no API key configured, skipping")
            return None

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with self._session.post(
                    ANTHROPIC_API_URL,
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._calls_made += 1
                        self._tokens_used += data.get("usage", {}).get("input_tokens", 0)
                        self._tokens_used += data.get("usage", {}).get("output_tokens", 0)
                        content = data.get("content", [])
                        if content and content[0].get("type") == "text":
                            return content[0]["text"]
                        return None
                    elif resp.status == 429:
                        # Rate limited — wait and retry
                        logger.debug(f"LLM: rate limited (attempt {attempt + 1})")
                        await __import__("asyncio").sleep(2 ** attempt)
                        continue
                    else:
                        body_text = await resp.text()
                        logger.warning(f"LLM: API returned {resp.status}: {body_text[:200]}")
                        return None
            except Exception as e:
                logger.warning(f"LLM: request failed (attempt {attempt + 1}): {e}")
                if attempt < MAX_RETRIES:
                    await __import__("asyncio").sleep(1)
                    continue
                return None

        return None

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 1024,
        model: str = DEFAULT_MODEL,
    ) -> Optional[dict]:
        """Send a prompt and parse the response as JSON.

        The prompt should instruct the model to respond with valid JSON.
        Extracts JSON from the response even if wrapped in markdown code fences.
        """
        text = await self.complete(prompt, system=system, max_tokens=max_tokens, model=model)
        if not text:
            return None

        # Strip markdown code fences if present
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (possibly with language tag)
            first_newline = cleaned.index("\n") if "\n" in cleaned else 3
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.debug(f"LLM: failed to parse JSON response: {e}\nRaw: {text[:300]}")
            return None

    def get_stats(self) -> dict:
        return {
            "calls_made": self._calls_made,
            "tokens_used": self._tokens_used,
            "available": self.available,
        }
