"""Shared Groq LLM client and retry/backoff helper.

Used by both the triage pass (triage.py) and the drafting pass (drafter.py).
Factored out so the rate-limit handling required by PRD §9 lives in one place.

PRD references: §5.2/§5.4 (models), §9 (exponential backoff, 3 retries).
"""

from __future__ import annotations

import logging
import os
import time

import groq
from groq import Groq

logger = logging.getLogger(__name__)

# Errors worth retrying: rate limits and transient server/connection issues.
# Kept as a module-level tuple so tests can substitute their own.
RETRYABLE_ERRORS = (
    groq.RateLimitError,
    groq.APIConnectionError,
    groq.InternalServerError,
)

_client: Groq | None = None


class LLMError(RuntimeError):
    """Raised when a Groq call cannot be completed (missing key or retries
    exhausted). The orchestrator catches this to skip the thread without
    applying ``Agent-Processed`` so it is retried next run (PRD §9)."""


def get_client() -> Groq:
    """Return a cached Groq client built from the GROQ_API_KEY secret."""
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise LLMError("Missing required environment variable 'GROQ_API_KEY'.")
        _client = Groq(api_key=api_key)
    return _client


def complete_with_retry(
    messages: list[dict],
    model: str,
    *,
    max_tokens: int,
    temperature: float = 0.0,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    client=None,
) -> str:
    """Call Groq chat completions with exponential backoff on retryable errors.

    Retries up to ``max_attempts`` times with delays base_delay, 2x, 4x, ...
    Raises ``LLMError`` if all attempts fail (PRD §9).
    """
    client = client or get_client()
    delay = base_delay
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""
        except RETRYABLE_ERRORS as exc:
            last_error = exc
            logger.warning(
                "Groq call failed (attempt %d/%d): %s",
                attempt,
                max_attempts,
                type(exc).__name__,
            )
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= 2

    raise LLMError(
        f"Groq call to {model} failed after {max_attempts} attempts: {last_error}"
    ) from last_error
