"""Bounded parser-provider failure categories and private-safe recovery advice."""

from __future__ import annotations

from aiohttp import ClientConnectionError, ClientResponseError
from httpx import HTTPStatusError, NetworkError, TimeoutException

PROVIDER_FAILURE_GUIDANCE = {
    "hosted_imap_paused": "Direct IMAP is paused while Daylight Cloud parsing is selected. Choose BYO AI Task to resume email ingestion.",
    "hosted_connection_changed": "Daylight Cloud connection changed or needs verification. Open Daylight Cloud settings before retrying.",
    "hosted_authentication": "Daylight Cloud rejected the connection credential. Repair the connection in Daylight Cloud settings before retrying.",
    "hosted_access": "Managed parsing is not enabled for this Daylight Cloud connection. Check its access before retrying.",
    "hosted_rate_limited": "Daylight Cloud rate limit reached. Wait before deliberately retrying; the request was not replayed.",
    "hosted_outcome_uncertain": "Daylight Cloud parse outcome is uncertain. Check recent activity and the review inbox before deliberately retrying; the request was not replayed.",
    "hosted_parse_failed": "Daylight Cloud could not parse the source. Check its settings and recent activity before deliberately retrying.",
    "provider_timeout": "The AI provider timed out. Wait before trying the source again.",
    "provider_rate_limited": "The AI provider rate limit was reached. Wait and check your provider quota before trying again.",
    "provider_unavailable": "The AI provider is unavailable. Check the configured AI Task and its connection before trying again.",
    "provider_authentication": "The AI provider rejected authentication. Check its credentials and account permissions before trying again.",
    "provider_invalid_response": "The AI provider returned an invalid result. Check the configured AI Task and try the source again.",
    "provider_error": "Parsing failed. Check the configured AI Task and submit the source again.",
}


def classify_provider_failure(error: Exception) -> str:
    """Use typed causes, never provider exception text or arbitrary attributes."""
    try:
        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen and len(seen) < 16:
            seen.add(id(current))
            if isinstance(current, (TimeoutError, TimeoutException)):
                return "provider_timeout"
            if isinstance(current, ClientResponseError):
                status = current.status
            elif isinstance(current, HTTPStatusError):
                status = current.response.status_code
            else:
                status = getattr(current, "status_code", None)
            if type(status) is int:
                if status == 429:
                    return "provider_rate_limited"
                if status in (401, 403):
                    return "provider_authentication"
                if 500 <= status <= 599:
                    return "provider_unavailable"
            if isinstance(current, (ConnectionError, ClientConnectionError, NetworkError)):
                return "provider_unavailable"
            # Only explicit exception causes are evidence. Descriptors and
            # malformed cause values are untrusted provider implementation data.
            cause = current.__cause__
            current = cause if isinstance(cause, BaseException) else None
    except Exception:
        pass
    return "provider_error"
