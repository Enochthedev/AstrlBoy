"""
Centralized AI client with automatic OpenRouter fallback.

All Claude API calls should go through create_message() instead of
directly using AsyncAnthropic. When Anthropic returns a 429 (rate limit)
or 529 (overloaded), the call is transparently retried via OpenRouter
using the same model. Callers don't need to know which provider served
the request — the response format is identical.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx
from anthropic import APIStatusError, AsyncAnthropic, RateLimitError

from core.config import settings
from core.logging import get_logger

logger = get_logger("core.ai")

_anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

# OpenRouter uses OpenAI-compatible format but can serve Anthropic models.
# Model names need the anthropic/ prefix on OpenRouter.
_OPENROUTER_MODEL_MAP: dict[str, str] = {
    "claude-haiku-4-5": "anthropic/claude-haiku-4-5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4-6",
    "claude-opus-4-6": "anthropic/claude-opus-4-6",
}

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class TextBlock:
    """Mirrors anthropic.types.TextBlock so callers can do response.content[0].text."""

    text: str
    type: str = "text"


@dataclass
class AIResponse:
    """Mirrors the shape of an Anthropic message response.

    Only includes the fields that astrlboy code actually reads:
    response.content[0].text, response.model, response.stop_reason.
    """

    content: list[TextBlock]
    model: str = ""
    stop_reason: str = "end_turn"
    provider: str = "anthropic"


async def create_message(
    *,
    model: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
    system: str | None = None,
    **kwargs: Any,
) -> Any:
    """Create a Claude message with automatic OpenRouter fallback.

    Tries Anthropic first. On rate limit (429) or overload (529),
    falls back to OpenRouter if an API key is configured. The response
    is compatible with the Anthropic SDK response format — callers
    can use response.content[0].text without changes.

    Args:
        model: Claude model name (e.g. "claude-haiku-4-5").
        max_tokens: Maximum tokens in the response.
        messages: List of message dicts with 'role' and 'content'.
        system: Optional system prompt.
        **kwargs: Additional params passed to Anthropic (ignored by OpenRouter).

    Returns:
        Anthropic Message object (from Anthropic) or AIResponse (from OpenRouter).
    """
    # Build the Anthropic call kwargs
    anthropic_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        anthropic_kwargs["system"] = system

    # Pass through any extra Anthropic-specific params
    for k, v in kwargs.items():
        anthropic_kwargs[k] = v

    try:
        return await _anthropic.messages.create(**anthropic_kwargs)
    except (RateLimitError, APIStatusError) as exc:
        # Only fall back on 429 (rate limit) or 529 (overloaded)
        status = getattr(exc, "status_code", 0)
        if status not in (429, 529):
            raise

        if not settings.openrouter_api_key:
            logger.warning(
                "anthropic_rate_limited_no_fallback",
                model=model,
                status=status,
            )
            raise

        logger.info(
            "anthropic_rate_limited_falling_back",
            model=model,
            status=status,
            fallback="openrouter",
        )
        return await _openrouter_fallback(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            system=system,
        )


async def _openrouter_fallback(
    *,
    model: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
    system: str | None = None,
) -> AIResponse:
    """Call OpenRouter as a fallback when Anthropic is rate-limited.

    Converts the Anthropic-style request to OpenAI-compatible format
    and wraps the response in an AIResponse that matches the Anthropic
    SDK's response shape.

    Args:
        model: Anthropic model name.
        max_tokens: Max response tokens.
        messages: Message list.
        system: Optional system prompt.

    Returns:
        AIResponse with the same .content[0].text interface.

    Raises:
        Exception: If OpenRouter also fails.
    """
    or_model = _OPENROUTER_MODEL_MAP.get(model, f"anthropic/{model}")

    # OpenRouter uses OpenAI format — system prompt goes as a system message
    or_messages: list[dict[str, str]] = []
    if system:
        or_messages.append({"role": "system", "content": system})
    or_messages.extend(messages)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            _OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": or_model,
                "max_tokens": max_tokens,
                "messages": or_messages,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    # Extract text from OpenAI-format response
    text = ""
    choices = data.get("choices", [])
    if choices:
        text = choices[0].get("message", {}).get("content", "")

    logger.info(
        "openrouter_fallback_success",
        model=or_model,
        response_length=len(text),
    )

    return AIResponse(
        content=[TextBlock(text=text)],
        model=or_model,
        provider="openrouter",
    )
