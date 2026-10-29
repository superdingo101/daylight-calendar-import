"""Dormant public hosted parser adapter; callers own runtime activation.

Credential callbacks must refer to the entry-owned M2 runtime. A saved candidate
or a credential for a different service never establishes parsing authority.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .hosted_credentials import BoundCredential, CredentialCheckpoint
from .hosted_parse_transport import HostedParseError, HostedParseTransport
from .hosted_transport import HostedServiceBinding, _strict_strings, _timestamp
from .models import EventDraft
from .parser import ParseOutcome, parse_ai_data
from .providers import ParserCapabilities, SourceValidationError
from .sources import SourceAttachment, SourceDocument, SourceKind

_CAPABILITIES = ParserCapabilities(True, True, True, 4, 10 * 1024 * 1024)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ZONE = re.compile(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*")
_STAGED = re.compile(r"daylight-(?:email-)?[A-Za-z0-9_-]+\.(?:png|jpg|webp|pdf)")


def _rfc3339(value: Any) -> None:
    try:
        _timestamp(value)
    except vol.Invalid:
        raise ValueError from None


def _text(value: Any, maximum: int, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ValueError
    value.encode("utf-8", "strict")


def _object(value: Any, keys: set[str]) -> None:
    if type(value) is not dict or set(value) != keys:
        raise ValueError


def _warnings(value: Any, maximum: int) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > maximum:
        raise ValueError
    for warning in value:
        _object(warning, {"code", "message", "field"})
        if type(warning["code"]) is not str or _CODE.fullmatch(warning["code"]) is None:
            raise ValueError
        _text(warning["message"], 2048)
        _text(warning["field"], 128, nullable=True)
    return value


def _draft(value: Any) -> dict[str, Any]:
    _object(value, {"title", "start", "end", "all_day", "location", "description", "confidence"})
    _text(value["title"], 512)
    _text(value["location"], 2048, nullable=True)
    _text(value["description"], 65536, nullable=True)
    if type(value["all_day"]) is not bool:
        raise ValueError
    for key in ("start", "end"):
        if value["all_day"]:
            if type(value[key]) is not str or _DATE.fullmatch(value[key]) is None:
                raise ValueError
        else:
            _rfc3339(value[key])
    confidence = value["confidence"]
    if type(confidence) not in (int, float, Decimal) or not 0 <= confidence <= 1:
        raise ValueError
    # The canonical RFC 3339 decoder accepts lowercase z, whereas the local
    # domain parser only accepts the standard uppercase UTC designator.
    # Normalize both timestamps before either local validation or parsing.
    mapped = {
        **value,
        "confidence": float(confidence),
        **({key: value[key][:-1] + "Z" if value[key].endswith("z") else value[key]
            for key in ("start", "end")} if not value["all_day"] else {}),
    }
    # Semantic ordering, actual dates, nonempty titles and offset-awareness are
    # independently enforced by the same local domain validator as BYO.
    EventDraft.from_mapping(mapped)
    return mapped


def parse_hosted_result(value: Any, source: SourceDocument) -> ParseOutcome:
    """Validate the complete public ParseResult before admitting any drafts."""
    _object(value, {"schema_version", "events", "rejected", "warnings", "provider_metadata"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError
    for key in ("events", "rejected"):
        if type(value[key]) is not list or len(value[key]) > 256:
            raise ValueError
    metadata = value["provider_metadata"]
    if metadata is not None and (
        type(metadata) is not dict or len(metadata) > 32
        or any(type(key) is not str or len(key) > 128 for key in metadata)
    ):
        raise ValueError
    _strict_strings(value)
    warnings = _warnings(value["warnings"], 128)
    messages = [f'{item["code"]}: {item["message"]}' for item in warnings]
    recurrence = any(item["code"] == "unsupported_recurrence" for item in warnings)
    drafts = []
    for index, event in enumerate(value["events"]):
        _object(event, {"draft", "warnings"})
        drafts.append(_draft(event["draft"]))
        event_warnings = _warnings(event["warnings"], 64)
        recurrence |= any(item["code"] == "unsupported_recurrence" for item in event_warnings)
        messages.extend(f'event {index}: {item["code"]}: {item["message"]}' for item in event_warnings)
    for rejected in value["rejected"]:
        _object(rejected, {"index", "reason_code", "message", "raw_summary"})
        index = rejected["index"]
        if index is not None and (
            type(index) not in (int, Decimal) or index < 0
            or (type(index) is Decimal and (not index.is_finite() or index != index.to_integral_value()))
        ):
            raise ValueError
        if type(rejected["reason_code"]) is not str or _CODE.fullmatch(rejected["reason_code"]) is None:
            raise ValueError
        _text(rejected["message"], 2048)
        _text(rejected["raw_summary"], 4096, nullable=True)
        recurrence |= rejected["reason_code"] == "unsupported_recurrence"
        # raw_summary may repeat source text; it is deliberately not retained.
        prefix = "candidate" if rejected["index"] is None else f'candidate {rejected["index"]}'
        messages.append(f'{prefix}: {rejected["reason_code"]}: {rejected["message"]}')
    evidence = "\n\n".join(part for part in (source.title, source.text) if part)
    result = parse_ai_data({"events": drafts, "unsupported_recurrence": recurrence}, source_text=evidence)
    return ParseOutcome(result.events, [*result.warnings, *messages], result.event_assumptions)


def _attachment_bytes(media_dirs: dict[str, str], attachment: SourceAttachment) -> bytes:
    """Read only a normal integration-staged file below a configured media root."""
    ref = urlsplit(attachment.content_ref)
    parts = ref.path.split("/")
    if (ref.scheme != "media-source" or ref.netloc != "media_source" or ref.query or ref.fragment
            or len(parts) != 3 or parts[0] or parts[1] not in media_dirs
            or _STAGED.fullmatch(parts[2]) is None):
        raise ValueError
    root = Path(media_dirs[parts[1]])
    # dir_fd + O_NOFOLLOW prevents a last-moment file symlink from turning a
    # reviewed local media reference into arbitrary-file disclosure.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = os.open(parts[2], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != attachment.size_bytes:
                raise ValueError
            content = handle.read(attachment.size_bytes + 1)
    finally:
        os.close(directory)
    if len(content) != attachment.size_bytes:
        raise ValueError
    observed = (
        "image/png" if content.startswith(b"\x89PNG\r\n\x1a\n") else
        "image/jpeg" if content.startswith(b"\xff\xd8\xff") else
        "image/webp" if content.startswith(b"RIFF") and content[8:12] == b"WEBP" else
        "application/pdf" if content.startswith(b"%PDF-") else None
    )
    if observed != attachment.media_type or (
        attachment.sha256 is not None
        and sha256(content).hexdigest() != attachment.sha256.lower()
    ):
        raise ValueError
    return content


class DaylightHostedParserProvider:
    """Parse through one trusted service with entry-owned current authority."""

    def __init__(
        self, hass: HomeAssistant, *, service_binding: HostedServiceBinding,
        checkpoint: Callable[[], CredentialCheckpoint | None], active: Callable[[], bool],
    ) -> None:
        self._hass = hass
        self._service = service_binding
        self._checkpoint = checkpoint
        self._active = active
        self._transport = HostedParseTransport(async_get_clientsession(hass), service_binding)

    @property
    def capabilities(self) -> ParserCapabilities:
        """Public v1 supports text and bounded image/PDF attachments."""
        return _CAPABILITIES

    def _authority(self) -> BoundCredential:
        try:
            checkpoint = self._checkpoint()
            if self._active() is not True or type(checkpoint) is not CredentialCheckpoint:
                raise HostedParseError("unauthenticated", status=401)
            if checkpoint.service_binding != self._service:
                raise HostedParseError("service_reconciliation_required")
            if (not checkpoint.processing_consent or checkpoint.local_disconnect != "none"
                    or checkpoint.reconnect is not None or checkpoint.current is None):
                raise HostedParseError("unauthenticated", status=401)
            credential = checkpoint.current
            if datetime.fromisoformat(credential.expires_at).astimezone(UTC) <= datetime.now(UTC):
                raise HostedParseError("unauthenticated", status=401)
            return credential
        except HostedParseError:
            raise
        except Exception:
            raise HostedParseError("unauthenticated", status=401) from None

    async def async_parse(
        self, source: SourceDocument, *, reference_datetime: str, time_zone: str,
    ) -> ParseOutcome:
        """Validate local source/authority, send once, then reject stale results."""
        credential = self._authority()
        self.capabilities.validate(source)
        try:
            _rfc3339(reference_datetime)
            if type(time_zone) is not str or len(time_zone) > 128 or _ZONE.fullmatch(time_zone) is None:
                raise ValueError
            ZoneInfo(time_zone)
            if type(source.kind) is not SourceKind or source.received_at.utcoffset() is None:
                raise ValueError
            _rfc3339(source.received_at.isoformat())
            _text(source.id, 128)
            _text(source.title, 512, nullable=True)
            _text(source.text, 1000000, nullable=True)
            identifiers = set()
            descriptors = []
            binaries = []
            for attachment in source.attachments:
                if (_IDENTIFIER.fullmatch(attachment.id) is None or attachment.id in identifiers
                        or type(attachment.size_bytes) is not int):
                    raise ValueError
                _text(attachment.filename, 512, nullable=True)
                if attachment.sha256 is not None and re.fullmatch(r"[A-Fa-f0-9]{64}", attachment.sha256) is None:
                    raise ValueError
                identifiers.add(attachment.id)
                content = await self._hass.async_add_executor_job(
                    _attachment_bytes, self._hass.config.media_dirs, attachment,
                )
                descriptors.append({
                    "id": attachment.id, "filename": attachment.filename,
                    "media_type": attachment.media_type, "size_bytes": attachment.size_bytes,
                    "sha256": attachment.sha256,
                })
                binaries.append((attachment.id, attachment.media_type, content))
            request = {
                "schema_version": 1, "reference_datetime": reference_datetime, "time_zone": time_zone,
                "source": {
                    "id": source.id, "kind": source.kind.value,
                    "received_at": source.received_at.isoformat(), "title": source.title,
                    "text": source.text, "attachments": descriptors,
                    # Local routing/upstream metadata has no role in model parsing.
                    "metadata": {},
                },
            }
        except (ValueError, TypeError, OSError, ZoneInfoNotFoundError):
            raise SourceValidationError("invalid_source", "Source could not be prepared for Daylight Cloud") from None
        if self._authority() != credential:
            raise HostedParseError("unauthenticated", status=401)
        response = await self._transport.async_parse(request, tuple(binaries), token=credential.token)
        if self._authority() != credential:
            raise HostedParseError("unauthenticated", status=401)
        try:
            return parse_hosted_result(response, source)
        except Exception:
            raise HostedParseError("provider_invalid_response", status=200) from None
