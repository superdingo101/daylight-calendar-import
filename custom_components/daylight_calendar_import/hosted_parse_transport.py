"""Bounded multipart parser transport, independent of lifecycle response limits.

A request is sent once. A connection failure can leave the provider's outcome
unknown; neither a request ID nor a retryable error grants replay authority.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, DecimalException
from typing import Any
from uuid import uuid4

import aiohttp
import voluptuous as vol

from .hosted_transport import (
    HostedLifecycleTransport,
    HostedServiceBinding,
    HostedTransportError,
    _pairs,
    _reject_constant,
    _strict_strings,
)
from .providers import ProviderError

# The complete v1 response can contain 256 full descriptions and per-event
# warnings. The small lifecycle envelope is intentionally not reused here.
MAX_PARSE_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_PARSE_REQUEST_JSON_BYTES = 5 * 1024 * 1024
PARSE_TIMEOUT_SECONDS = 120


class HostedParseError(ProviderError):
    """Fixed/safe failure information; never retains raw source/server text."""

    def __init__(
        self, code: str, *, status: int | None = None, retryable: bool = False,
        retry_after: int | None = None, uncertain: bool = False,
    ) -> None:
        super().__init__(code, "Daylight Cloud could not parse this source")
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.uncertain = uncertain


def _number(raw: str) -> Decimal | int:
    """Keep fractional confidence exact; never round a schema integer."""
    try:
        number = Decimal(raw)
        if not number.is_finite():
            raise ValueError
        if -10485760 <= number <= 10485760 and number == number.to_integral_value():
            return int(number)
        return number
    except DecimalException:
        raise ValueError from None


class HostedParseTransport:
    """One POST to a locally configured HTTPS origin using HA's session."""

    def __init__(self, session: aiohttp.ClientSession, service: HostedServiceBinding) -> None:
        if type(service) is not HostedServiceBinding:
            raise ValueError("invalid trusted service")
        self._session = session
        self._service = service

    async def async_parse(
        self, request: dict[str, Any], attachments: tuple[tuple[str, str, bytes], ...],
        *, token: str,
    ) -> dict[str, Any]:
        """Submit bounded source bytes once and strictly decode the response."""
        try:
            payload = json.dumps(
                request, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            ).encode("utf-8")
        except (ValueError, UnicodeError, TypeError, RecursionError):
            raise HostedParseError("invalid_request") from None
        if len(payload) > MAX_PARSE_REQUEST_JSON_BYTES:
            raise HostedParseError("source_too_large")
        body = aiohttp.FormData()
        body.add_field("request", payload.decode("utf-8"), content_type="application/json")
        for identifier, media_type, content in attachments:
            body.add_field(
                "attachment." + identifier, content, content_type=media_type,
                filename=identifier,
            )
        status = None
        try:
            async with asyncio.timeout(PARSE_TIMEOUT_SECONDS):
                async with self._session.request(
                    "POST", self._service.api_origin + "/v1/parse",
                    headers={
                        "Accept": "application/json", "Accept-Encoding": "identity",
                        "X-Request-ID": str(uuid4()), "Authorization": "Bearer " + token,
                    },
                    data=body, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=PARSE_TIMEOUT_SECONDS),
                    max_line_size=4096, max_field_size=4096, max_headers=32,
                    auto_decompress=False,
                ) as response:
                    status = response.status
                    if (300 <= status < 400 or "Content-Encoding" in response.headers
                            or response.content_type != "application/json"):
                        raise HostedParseError("provider_invalid_response", status=status)
                    # Parse v1 predates delivery's mandatory no-store contract.
                    # Never follow redirects or persist a response in any cache.
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        raw.extend(chunk)
                        if len(raw) > MAX_PARSE_RESPONSE_BYTES:
                            raise HostedParseError("provider_invalid_response", status=status)
                    value = json.loads(
                        raw.decode("utf-8"), object_pairs_hook=_pairs,
                        parse_constant=_reject_constant, parse_float=_number,
                    )
                    _strict_strings(value)
                    if status != 200:
                        self._raise_api_error(value, status, response.headers.get("Retry-After"))
                    if type(value) is not dict:
                        raise ValueError
                    return value
        except TimeoutError:
            raise HostedParseError(
                "provider_timeout", status=status, retryable=True, uncertain=True,
            ) from None
        except aiohttp.ClientError:
            raise HostedParseError(
                "provider_unavailable", status=status, retryable=True, uncertain=True,
            ) from None
        except (ValueError, UnicodeError, RecursionError, vol.Invalid):
            raise HostedParseError("provider_invalid_response", status=status) from None

    @staticmethod
    def _raise_api_error(value: Any, status: int, retry_header: str | None) -> None:
        """Preserve canonical codes/flags, excluding all remote message text."""
        # Lifecycle's canonical error validation is shared, but its 429 policy
        # is narrower: parser Retry-After is optional and handled separately.
        missing_retry = retry_header is None
        if status == 429 and missing_retry:
            retry_header = "1"
        if status == 429 and retry_header is not None and (
            re.fullmatch(r"[0-9]{1,3}", retry_header) is None
            or not 1 <= int(retry_header) <= 300
        ):
            raise HostedParseError("provider_invalid_response", status=status)
        try:
            HostedLifecycleTransport._raise_api_error(value, status, retry_header)
        except HostedTransportError as error:
            if error.code == "invalid_response":
                raise HostedParseError("provider_invalid_response", status=status) from None
            raise HostedParseError(
                error.code, status=status, retryable=error.retryable,
                retry_after=None if missing_retry else error.retry_after,
            ) from None
