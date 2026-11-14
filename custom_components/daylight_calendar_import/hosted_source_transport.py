"""One-shot, unactivated H3 source claim transport.

A failed claim may have acquired a remote lease. Callers must never retry the
same request transparently, and must not acknowledge a source on transport alone.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import voluptuous as vol

from .hosted_parse_transport import _number
from .hosted_transport import (
    HostedLifecycleTransport, HostedServiceBinding, HostedTransportError,
    _pairs, _reject_constant, _strict_strings, _timestamp,
)

# JSON-escaped non-BMP scalars can require 12 bytes each. Account for text,
# title, sender and all four filenames, plus bounded identifiers and envelope.
MAX_DELIVERY_BYTES = 12 * (1_000_000 + 6 * 512) + 16_384
MAX_CLAIM_BYTES = 50 * MAX_DELIVERY_BYTES
_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_LEASE = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_ATTACHMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_MEDIA = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


def _fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError
    return value


def _string(value: Any, length: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= length:
        raise ValueError
    return value


def _pattern(value: Any, regex: re.Pattern) -> str:
    if type(value) is not str or not regex.fullmatch(value):
        raise ValueError
    return value


def _date(value: Any) -> datetime:
    try:
        _timestamp(value)
        return datetime.fromisoformat(value.upper()).astimezone(timezone.utc)
    except (ValueError, vol.Invalid, OverflowError):
        raise ValueError from None


def _source(value: Any) -> None:
    source = _fields(value, {"kind", "received_at", "attachments", "metadata"}, {"title", "text"})
    if source["kind"] != "email":
        raise ValueError
    _date(source["received_at"])
    for key, length in (("title", 512), ("text", 1_000_000)):
        if key in source and source[key] is not None:
            _string(source[key], length)
    metadata = _fields(source["metadata"], set(), {"sender"})
    if "sender" in metadata:
        _string(metadata["sender"], 512)
    attachments = source["attachments"]
    if type(attachments) is not list or len(attachments) > 4 or not (source.get("text") or attachments):
        raise ValueError
    ids, total = set(), 0
    for value in attachments:
        item = _fields(value, {"id", "media_type", "size_bytes", "sha256"}, {"filename"})
        ident = _pattern(item["id"], _ATTACHMENT)
        _pattern(item["sha256"], _DIGEST)
        if ident in ids or type(item["media_type"]) is not str or item["media_type"] not in _MEDIA:
            raise ValueError
        ids.add(ident)
        if "filename" in item and item["filename"] is not None:
            _string(item["filename"], 512)
        size = item["size_bytes"]
        if type(size) is not int or not 1 <= size <= 10_485_760:
            raise ValueError
        total += size
    if total > 10_485_760:
        raise ValueError


def _page(value: Any, limit: int, now: datetime) -> dict[str, Any]:
    page = _fields(value, {"schema_version", "deliveries"})
    if type(page["schema_version"]) is not int or page["schema_version"] != 1:
        raise ValueError
    items = page["deliveries"]
    if type(items) is not list or len(items) > limit:
        raise ValueError
    ids, source_ids = set(), set()
    for item in items:
        item = _fields(item, {"delivery_id", "source_id", "lease_token", "lease_expires_at", "source_expires_at", "source"})
        ident = _pattern(item["delivery_id"], _ID)
        source_id = _pattern(item["source_id"], _ID)
        _pattern(item["lease_token"], _LEASE)
        if ident in ids or source_id in source_ids:
            raise ValueError
        ids.add(ident)
        source_ids.add(source_id)
        expiry = _date(item["source_expires_at"])
        lease = _date(item["lease_expires_at"])
        if not now < expiry <= now + timedelta(days=7, minutes=5) or lease > expiry:
            raise ValueError
        _source(item["source"])
    return page


class HostedSourceTransport:
    """Claim from a preconfigured trusted origin with no implicit retries."""

    def __init__(self, session: aiohttp.ClientSession, service: HostedServiceBinding) -> None:
        if type(service) is not HostedServiceBinding:
            raise ValueError("invalid trusted service")
        self._session, self._service = session, service

    @property
    def service_binding(self) -> HostedServiceBinding:
        """The trusted service to which this client sends credentials."""
        return self._service

    async def async_claim(self, *, token: str, limit: int = 20) -> dict[str, Any]:
        """Perform one claim; a lost response leaves an unknown, expiring lease."""
        if type(limit) is not int or not 1 <= limit <= 50 or type(token) is not str or re.fullmatch(r"[A-Za-z0-9._~-]{43,256}", token) is None:
            raise ValueError("invalid claim request")
        status = None
        try:
            async with asyncio.timeout(15):
                async with self._session.request(
                    "POST", self._service.api_origin + "/v1/sources/claim",
                    headers={"Accept": "application/json", "Accept-Encoding": "identity", "Authorization": "Bearer " + token},
                    json={"schema_version": 1, "limit": limit}, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15), max_line_size=4096,
                    max_field_size=4096, max_headers=32, auto_decompress=False,
                ) as response:
                    status = response.status
                    directives = response.headers.get("Cache-Control", "").lower().split(",")
                    if (300 <= status < 400 or "Content-Encoding" in response.headers
                            or response.content_type != "application/json"
                            or "no-store" not in (part.strip() for part in directives)):
                        raise ValueError
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        raw.extend(chunk)
                        if len(raw) > min(MAX_CLAIM_BYTES, limit * MAX_DELIVERY_BYTES):
                            raise ValueError
                    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                                       parse_constant=_reject_constant, parse_float=_number)
                    _strict_strings(value)
                    if status != 200:
                        HostedLifecycleTransport._raise_api_error(value, status, response.headers.get("Retry-After"))
                    return _page(value, limit, datetime.now(timezone.utc))
        except (aiohttp.ClientError, TimeoutError):
            raise HostedTransportError("service_unavailable", status=status, retryable=True) from None
        except (ValueError, UnicodeError, RecursionError, vol.Invalid, OverflowError):
            raise HostedTransportError("invalid_response", status=status) from None
