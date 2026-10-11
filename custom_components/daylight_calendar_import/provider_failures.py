"""Bounded parser-provider failure categories and private-safe recovery advice."""

from __future__ import annotations

from aiohttp import ClientConnectionError, ClientResponseError

PROVIDER_FAILURE_GUIDANCE = {
    "provider_timeout": "The AI provider timed out. Wait before trying the source again.",
    "provider_rate_limited": "The AI provider rate limit was reached. Wait and check your provider quota before trying again.",
    "provider_unavailable": "The AI provider is unavailable. Check the configured AI Task and its connection before trying again.",
    "provider_authentication": "The AI provider rejected authentication. Check its credentials and account permissions before trying again.",
    "provider_invalid_response": "The AI provider returned an invalid result. Check the configured AI Task and try the source again.",
    "provider_error": "Parsing failed. Check the configured AI Task and submit the source again.",
}


def classify_provider_failure(error: Exception) -> str:
    """Use typed causes, never provider exception text or arbitrary attributes."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return "provider_timeout"
        status = (current.status if isinstance(current, ClientResponseError)
                  else getattr(current, "status_code", None))
        if type(status) is int:
            if status == 429:
                return "provider_rate_limited"
            if status in (401, 403):
                return "provider_authentication"
            if 500 <= status <= 599:
                return "provider_unavailable"
        if isinstance(current, (ConnectionError, ClientConnectionError)):
            return "provider_unavailable"
        # Only explicit causal chains are evidence. Implicit context may be an
        # unrelated exception handled by the upstream implementation.
        current = current.__cause__
    return "provider_error"
