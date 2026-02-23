"""
Lightweight LLM client for intelligence features.

Uses the OpenAI API with GPT-4o-mini for cost efficiency.
All calls are async-compatible and include retry logic.
"""

import json
import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
MAX_RETRIES = 2


class LLMClient:
    """Thin async wrapper around the OpenAI Chat Completions API."""

    def __init__(self, session: aiohttp.ClientSession, api_key: Optional[str] = None):
        self._session = session
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
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
        """Send a prompt to the OpenAI API and return the text response.

        Returns None on failure (missing key, API error, timeout).
        """
        if not self._api_key:
            logger.debug("LLM: no API key configured, skipping")
            return None

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with self._session.post(
                    OPENAI_API_URL,
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._calls_made += 1
                        self._tokens_used += data.get("usage", {}).get("prompt_tokens", 0)
                        self._tokens_used += data.get("usage", {}).get("completion_tokens", 0)
                        choices = data.get("choices", [])
                        if choices and choices[0].get("message", {}).get("content"):
                            return choices[0]["message"]["content"]
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
