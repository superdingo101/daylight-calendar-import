"""Direct IMAP transport adapter for email ingestion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
import re
import ssl
from typing import Protocol

from .email_source import (
    DirectImapReference,
    EmailDisposition,
    EmailEnvelope,
    EmailProvenance,
    EmailSourceConfig,
    EmailSourceType,
)

DEFAULT_IMAP_PORT = 993
DEFAULT_IMAP_SEARCH = "UnSeen UnDeleted"
DEFAULT_IMAP_CHARSET = "utf-8"
DEFAULT_IMAP_TIMEOUT = 10.0

_STATE_AUTH = "AUTH"
_STATE_NONAUTH = "NONAUTH"

_UIDVALIDITY_RE = re.compile(rb"\[UIDVALIDITY\s+([0-9]+)\]", re.IGNORECASE)
_FETCH_UID_RE = re.compile(rb"\bUID\s+([0-9]+)\b", re.IGNORECASE)


class DirectImapError(Exception):
    """Base error raised by the Direct IMAP adapter."""


class DirectImapConnectionError(DirectImapError):
    """The IMAP server could not be reached or the connection was lost."""


class DirectImapAuthenticationError(DirectImapError):
    """The IMAP server rejected authentication."""


class DirectImapMailboxError(DirectImapError):
    """The configured mailbox could not be selected."""


class DirectImapProtocolError(DirectImapError):
    """The IMAP server returned an invalid or unsuccessful response."""


class DirectImapUnsupportedDispositionError(DirectImapError):
    """The requested upstream disposition is not implemented yet."""


@dataclass(frozen=True, slots=True)
class DirectImapSettings:
    """Connection settings for one Direct IMAP email source."""

    source_id: str
    host: str
    username: str
    password: str = field(repr=False)
    mailbox: str = "INBOX"
    port: int = DEFAULT_IMAP_PORT
    search: str = DEFAULT_IMAP_SEARCH
    charset: str = DEFAULT_IMAP_CHARSET
    verify_ssl: bool = True
    timeout: float = DEFAULT_IMAP_TIMEOUT

    def __post_init__(self) -> None:
        """Reject malformed settings before opening a network connection."""
        for name in (
            "source_id",
            "host",
            "username",
            "password",
            "mailbox",
            "search",
            "charset",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if type(self.verify_ssl) is not bool:
            raise ValueError("verify_ssl must be a boolean")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number")


class _Response(Protocol):
    """Response shape returned by aioimaplib."""

    result: str
    lines: Sequence[object]


class _ImapClient(Protocol):
    """Subset of aioimaplib used by the adapter."""

    def get_state(self) -> str:
        """Return the current IMAP protocol state."""
        ...

    async def wait_hello_from_server(self) -> None:
        """Wait for the server greeting."""
        ...

    async def login(self, user: str, password: str) -> _Response:
        """Authenticate."""
        ...

    async def select(self, mailbox: str = "INBOX") -> _Response:
        """Select a mailbox."""
        ...

    async def uid_search(
        self, *criteria: str, charset: str | None = "utf-8"
    ) -> _Response:
        """Search by UID."""
        ...

    async def uid(self, command: str, *criteria: str) -> _Response:
        """Execute a UID command."""
        ...

    async def logout(self) -> _Response:
        """End the IMAP session."""
        ...


type _ClientFactory = Callable[..., _ImapClient]
type _Clock = Callable[[], datetime]


def _default_client_factory(**kwargs: object) -> _ImapClient:
    """Create the runtime client without importing optional requirements in tests."""
    from aioimaplib import IMAP4_SSL

    return IMAP4_SSL(**kwargs)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ssl_context(*, verify_ssl: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _require_ok(
    response: _Response,
    message: str,
    error_type: type[DirectImapError] = DirectImapProtocolError,
) -> None:
    if response.result != "OK":
        raise error_type(message)


def _line_bytes(value: object, message: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise DirectImapProtocolError(message)
    return bytes(value)


def _parse_uidvalidity(response: _Response, mailbox: str) -> int:
    _require_ok(response, "IMAP mailbox selection failed", DirectImapMailboxError)
    for raw_line in response.lines:
        line = _line_bytes(
            raw_line,
            "IMAP mailbox selection returned malformed response data",
        )
        if match := _UIDVALIDITY_RE.search(line):
            try:
                value = int(match.group(1))
                return DirectImapReference(
                    mailbox=mailbox,
                    uid_validity=value,
                    uid=1,
                ).uid_validity
            except ValueError as exc:
                raise DirectImapProtocolError(
                    "IMAP server returned an invalid UIDVALIDITY"
                ) from exc
    raise DirectImapProtocolError("IMAP server did not provide UIDVALIDITY")


def _parse_search_uids(
    response: _Response,
    *,
    mailbox: str,
    uid_validity: int,
) -> tuple[int, ...]:
    _require_ok(response, "IMAP UID search failed")
    if not response.lines:
        return ()

    first_line = _line_bytes(
        response.lines[0],
        "IMAP UID search returned malformed response data",
    ).strip()
    if not first_line:
        return ()

    tokens = first_line.split()
    if not all(token.isdigit() for token in tokens):
        if len(response.lines) == 1 and not any(token.isdigit() for token in tokens):
            return ()
        raise DirectImapProtocolError("IMAP UID search returned malformed identifiers")

    uids: list[int] = []
    for token in tokens:
        try:
            reference = DirectImapReference(
                mailbox=mailbox,
                uid_validity=uid_validity,
                uid=int(token),
            )
        except ValueError as exc:
            raise DirectImapProtocolError(
                "IMAP UID search returned an invalid identifier"
            ) from exc
        uids.append(reference.uid)
    return tuple(uids)


def _extract_fetch_body(
    response: _Response,
    *,
    expected_uid: int,
) -> bytes | None:
    """Return the requested BODY literal without assuming FETCH item order."""
    _require_ok(response, "IMAP UID fetch failed")
    lines = tuple(
        _line_bytes(line, "IMAP UID fetch returned malformed response data")
        for line in response.lines
    )
    fetch_starts = [
        index for index, line in enumerate(lines) if _FETCH_START_RE.match(line)
    ]
    if not fetch_starts:
        return None

    saw_uid = False
    for frame_number, frame_start in enumerate(fetch_starts):
        frame_end = (
            fetch_starts[frame_number + 1]
            if frame_number + 1 < len(fetch_starts)
            else len(lines)
        )
        frame = lines[frame_start:frame_end]
        body_markers = [
            (index, match)
            for index, line in enumerate(frame)
            if (match := _BODY_LITERAL_RE.search(line)) is not None
        ]
        literal_indexes = {
            index + 1 for index, _ in body_markers if index + 1 < len(frame)
        }
        uid_values = [
            int(match.group(1))
            for index, line in enumerate(frame)
            if index not in literal_indexes
            for match in _FETCH_UID_RE.finditer(line)
        ]
        saw_uid = saw_uid or bool(uid_values)
        if expected_uid not in uid_values:
            continue

        if len(body_markers) != 1:
            raise DirectImapProtocolError(
                "IMAP UID fetch did not return exactly one BODY literal"
            )
        marker_index, marker = body_markers[0]
        literal_index = marker_index + 1
        if literal_index >= len(frame):
            raise DirectImapProtocolError(
                "IMAP UID fetch did not return a message body"
            )

        literal = frame[literal_index]
        declared_size = int(marker.group(1))
        if len(literal) != declared_size:
            raise DirectImapProtocolError(
                "IMAP UID fetch returned a BODY literal with the wrong length"
            )
        if not any(b")" in line for line in frame[literal_index + 1 :]):
            raise DirectImapProtocolError(
                "IMAP UID fetch returned an unterminated FETCH response"
            )
        return literal

    if saw_uid:
        raise DirectImapProtocolError("IMAP UID fetch returned the wrong message")
    raise DirectImapProtocolError("IMAP UID fetch did not identify a message")


class DirectImapSource:
    """Collect raw RFC messages from one mailbox over direct IMAP."""

    def __init__(
        self,
        settings: DirectImapSettings,
        *,
        client_factory: _ClientFactory = _default_client_factory,
        clock: _Clock = _utcnow,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._clock = clock
        self._config = EmailSourceConfig(
            source_id=settings.source_id,
            source_type=EmailSourceType.DIRECT_IMAP,
        )

    @property
    def config(self) -> EmailSourceConfig:
        """Return the transport-neutral source configuration."""
        return self._config

    async def async_validate(self) -> None:
        """Validate connection, authentication, mailbox, and UID identity support."""
        client, _ = await self._async_open(self._settings.mailbox)
        await self._async_logout(client)

    async def async_collect(self) -> AsyncIterator[EmailEnvelope]:
        """Yield every message UID matching the configured IMAP search."""
        client, uid_validity = await self._async_open(self._settings.mailbox)
        try:
            try:
                search_response = await client.uid_search(
                    self._settings.search,
                    charset=self._settings.charset,
                )
            except Exception:
                raise DirectImapConnectionError("IMAP UID search failed") from None

            uids = _parse_search_uids(
                search_response,
                mailbox=self._settings.mailbox,
                uid_validity=uid_validity,
            )
            for uid in uids:
                try:
                    fetch_response = await client.uid(
                        "fetch",
                        str(uid),
                        "(UID BODY.PEEK[])",
                    )
                except Exception:
                    raise DirectImapConnectionError("IMAP UID fetch failed") from None

                raw_message = _extract_fetch_body(
                    fetch_response,
                    expected_uid=uid,
                )
                if raw_message is None:
                    continue
                yield EmailEnvelope(
                    received_at=self._clock(),
                    raw_message=raw_message,
                    provenance=EmailProvenance(
                        source_id=self._settings.source_id,
                        source_type=EmailSourceType.DIRECT_IMAP,
                        transport_reference=DirectImapReference(
                            mailbox=self._settings.mailbox,
                            uid_validity=uid_validity,
                            uid=uid,
                        ),
                    ),
                )
        finally:
            await self._async_logout(client)

    async def async_acknowledge(
        self,
        provenance: EmailProvenance,
        *,
        disposition: EmailDisposition,
    ) -> None:
        """Apply the v0.5 default: durable local handling without mailbox mutation."""
        self._validate_provenance(provenance)
        if disposition != EmailDisposition():
            raise DirectImapUnsupportedDispositionError(
                "Upstream IMAP disposition is not implemented yet"
            )

    def _validate_provenance(self, provenance: EmailProvenance) -> None:
        if provenance.source_id != self._settings.source_id:
            raise DirectImapProtocolError("Email provenance belongs to another source")
        if provenance.source_type is not EmailSourceType.DIRECT_IMAP:
            raise DirectImapProtocolError("Email provenance has the wrong source type")
        if not isinstance(provenance.transport_reference, DirectImapReference):
            raise DirectImapProtocolError(
                "Email provenance has the wrong transport reference"
            )

    async def _async_open(self, mailbox: str) -> tuple[_ImapClient, int]:
        try:
            client = self._client_factory(
                host=self._settings.host,
                port=self._settings.port,
                timeout=float(self._settings.timeout),
                ssl_context=_ssl_context(verify_ssl=self._settings.verify_ssl),
            )
        except (TimeoutError, OSError) as exc:
            raise DirectImapConnectionError("Unable to create IMAP connection") from exc

        try:
            await client.wait_hello_from_server()
            state = client.get_state()
            if state == _STATE_NONAUTH:
                login_response = await client.login(
                    self._settings.username,
                    self._settings.password,
                )
                _require_ok(
                    login_response,
                    "IMAP authentication failed",
                    DirectImapAuthenticationError,
                )
                state = client.get_state()
            if state != _STATE_AUTH:
                raise DirectImapAuthenticationError(
                    "IMAP server did not enter authenticated state"
                )

            select_response = await client.select(mailbox)
            uid_validity = _parse_uidvalidity(select_response, mailbox)
            return client, uid_validity
        except asyncio.CancelledError:
            await asyncio.shield(self._async_logout(client))
            raise
        except DirectImapError:
            await self._async_logout(client)
            raise
        except Exception:
            await self._async_logout(client)
            raise DirectImapConnectionError("IMAP connection failed") from None

    @staticmethod
    async def _async_logout(client: _ImapClient) -> None:
        try:
            await client.logout()
        except Exception:
            pass
