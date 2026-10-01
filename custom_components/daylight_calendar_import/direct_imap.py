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
    MAX_IMAP_IDENTIFIER,
    DirectImapReference,
    EmailDisposition,
    EmailEnvelope,
    EmailProvenance,
    EmailSourceConfig,
    EmailSourceType,
)

DEFAULT_IMAP_PORT = 993
DEFAULT_IMAP_SEARCH = "UnSeen UnDeleted"
DEFAULT_IMAP_CHARSET = "us-ascii"
DEFAULT_IMAP_TIMEOUT = 10.0

_STATE_AUTH = "AUTH"
_STATE_NONAUTH = "NONAUTH"

_UIDVALIDITY_RE = re.compile(rb"\[UIDVALIDITY\s+([0-9]+)\]", re.IGNORECASE)
_FETCH_START_RE = re.compile(rb"^[0-9]+\s+FETCH\s+\(", re.IGNORECASE)
_FETCH_UID_RE = re.compile(rb"\bUID\s+([0-9]+)\b", re.IGNORECASE)
_LITERAL_SUFFIX_RE = re.compile(rb"\{([0-9]+)\}\s*$")
_BODY_LITERAL_RE = re.compile(
    rb"\bBODY(?:\.PEEK)?\[\]\s+\{([0-9]+)\}\s*$",
    re.IGNORECASE,
)

_ERR_PORT = "port must be an integer between 1 and 65535"  # pragma: no mutate
_ERR_VERIFY_SSL = "verify_ssl must be a boolean"  # pragma: no mutate
_ERR_TIMEOUT = "timeout must be a finite positive number"  # pragma: no mutate
_ERR_CREATE_CONNECTION = "Unable to create IMAP connection"  # pragma: no mutate
_ERR_AUTH = "IMAP authentication failed"  # pragma: no mutate
_ERR_AUTH_STATE = "IMAP server did not enter authenticated state"  # pragma: no mutate
_ERR_MAILBOX = "IMAP mailbox selection failed"  # pragma: no mutate
_ERR_MAILBOX_DATA = "IMAP mailbox selection returned malformed response data"  # pragma: no mutate
_ERR_UIDVALIDITY_INVALID = "IMAP server returned an invalid UIDVALIDITY"  # pragma: no mutate
_ERR_UIDVALIDITY_MISSING = "IMAP server did not provide UIDVALIDITY"  # pragma: no mutate
_ERR_SEARCH = "IMAP UID search failed"  # pragma: no mutate
_ERR_SEARCH_DATA = "IMAP UID search returned malformed response data"  # pragma: no mutate
_ERR_SEARCH_IDS = "IMAP UID search returned malformed identifiers"  # pragma: no mutate
_ERR_SEARCH_ID = "IMAP UID search returned an invalid identifier"  # pragma: no mutate
_ERR_FETCH = "IMAP UID fetch failed"  # pragma: no mutate
_ERR_FETCH_DATA = "IMAP UID fetch returned malformed response data"  # pragma: no mutate
_ERR_FETCH_LITERAL = "IMAP UID fetch did not return literal data"  # pragma: no mutate
_ERR_FETCH_BODY_COUNT = "IMAP UID fetch did not return exactly one BODY literal"  # pragma: no mutate
_ERR_FETCH_NO_UID = "IMAP UID fetch did not identify a message"  # pragma: no mutate
_ERR_FETCH_UID = "IMAP UID fetch returned an invalid identifier"  # pragma: no mutate
_ERR_FETCH_LENGTH = "IMAP UID fetch returned a BODY literal with the wrong length"  # pragma: no mutate
_ERR_FETCH_LITERAL_SIZE = "IMAP UID fetch returned an invalid BODY literal length"  # pragma: no mutate
_ERR_FETCH_UNTERMINATED = "IMAP UID fetch returned an unterminated FETCH response"  # pragma: no mutate
_ERR_SEARCH_TRANSPORT = "IMAP UID search failed"  # pragma: no mutate
_ERR_FETCH_TRANSPORT = "IMAP UID fetch failed"  # pragma: no mutate
_ERR_DISPOSITION = "Upstream IMAP disposition is not implemented yet"  # pragma: no mutate
_ERR_PROVENANCE_SOURCE = "Email provenance belongs to another source"  # pragma: no mutate
_ERR_PROVENANCE_TYPE = "Email provenance has the wrong source type"  # pragma: no mutate
_ERR_PROVENANCE_REFERENCE = "Email provenance has the wrong transport reference"  # pragma: no mutate
_ERR_CONNECTION = "IMAP connection failed"  # pragma: no mutate


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
            raise ValueError(_ERR_PORT)
        if type(self.verify_ssl) is not bool:
            raise ValueError(_ERR_VERIFY_SSL)
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError(_ERR_TIMEOUT)


class _Response(Protocol):
    """Response shape returned by aioimaplib."""

    result: str
    lines: Sequence[object]


class _ImapClient(Protocol):  # pragma: no mutate block
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

    async def select(self, mailbox: str) -> _Response:
        """Select a mailbox."""
        ...

    async def uid_search(
        self, *criteria: str, charset: str | None
    ) -> _Response:
        """Search by UID."""
        ...

    async def uid(self, command: str, *criteria: str) -> _Response:
        """Execute a UID command."""
        ...

    async def logout(self) -> _Response:
        """End the IMAP session."""
        ...

    def abort(self) -> None:
        """Close the transport without issuing an IMAP command."""
        ...


class _AioImapClientAdapter:
    """Expose cancellation-safe transport abort for aioimaplib."""

    def __init__(self, client: object) -> None:
        self._client = client

    def get_state(self) -> str:
        return self._client.get_state()  # type: ignore[attr-defined,no-any-return]

    async def wait_hello_from_server(self) -> None:
        await self._client.wait_hello_from_server()  # type: ignore[attr-defined]

    async def login(self, user: str, password: str) -> _Response:
        return await self._client.login(user, password)  # type: ignore[attr-defined,no-any-return]

    async def select(self, mailbox: str) -> _Response:
        return await self._client.select(mailbox)  # type: ignore[attr-defined,no-any-return]

    async def uid_search(
        self, *criteria: str, charset: str | None
    ) -> _Response:
        return await self._client.uid_search(  # type: ignore[attr-defined,no-any-return]
            *criteria, charset=charset
        )

    async def uid(self, command: str, *criteria: str) -> _Response:
        return await self._client.uid(  # type: ignore[attr-defined,no-any-return]
            command, *criteria
        )

    async def logout(self) -> _Response:
        return await self._client.logout()  # type: ignore[attr-defined,no-any-return]

    def abort(self) -> None:
        """Cancel an in-flight connect and close any established socket."""
        client_task = getattr(self._client, "_client_task", None)
        if client_task is not None and not client_task.done():
            client_task.cancel()
        protocol = getattr(self._client, "protocol", None)
        transport = getattr(protocol, "transport", None)
        if transport is not None:
            transport.close()


type _ClientFactory = Callable[..., _ImapClient]
type _Clock = Callable[[], datetime]


def _default_client_factory(**kwargs: object) -> _ImapClient:
    """Create the runtime client without importing optional requirements in tests."""
    from aioimaplib import IMAP4_SSL

    return _AioImapClientAdapter(IMAP4_SSL(**kwargs))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ssl_context(*, verify_ssl: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False  # pragma: no mutate
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


def _parse_uidvalidity(response: _Response) -> int:
    _require_ok(response, _ERR_MAILBOX, DirectImapMailboxError)
    for raw_line in response.lines:
        line = _line_bytes(
            raw_line,
            _ERR_MAILBOX_DATA,
        )
        if match := _UIDVALIDITY_RE.search(line):
            try:
                value = int(match.group(1))
            except ValueError as exc:
                raise DirectImapProtocolError(_ERR_UIDVALIDITY_INVALID) from exc
            if not 1 <= value <= MAX_IMAP_IDENTIFIER:
                raise DirectImapProtocolError(_ERR_UIDVALIDITY_INVALID)
            return value
    raise DirectImapProtocolError(_ERR_UIDVALIDITY_MISSING)


def _parse_search_uids(response: _Response) -> tuple[int, ...]:
    _require_ok(response, _ERR_SEARCH)
    if not response.lines:
        return ()

    first_line = _line_bytes(
        response.lines[0],
        _ERR_SEARCH_DATA,
    ).strip()
    if not first_line:
        return ()

    tokens = first_line.split()
    if not all(token.isdigit() for token in tokens):
        if len(response.lines) == 1 and not any(token.isdigit() for token in tokens):
            return ()
        raise DirectImapProtocolError(_ERR_SEARCH_IDS)

    uids: list[int] = []
    for token in tokens:
        try:
            uid = int(token)
        except ValueError as exc:
            raise DirectImapProtocolError(_ERR_SEARCH_ID) from exc
        if not 1 <= uid <= MAX_IMAP_IDENTIFIER:
            raise DirectImapProtocolError(_ERR_SEARCH_ID)
        uids.append(uid)
    return tuple(uids)


def _extract_fetch_body(
    response: _Response,
    *,
    expected_uid: int,
) -> bytes | None:
    """Return the requested BODY literal without assuming FETCH item order."""
    _require_ok(response, _ERR_FETCH)
    lines = tuple(
        _line_bytes(line, _ERR_FETCH_DATA)
        for line in response.lines
    )
    metadata_indexes: list[int] = []
    literal_indexes: set[int] = set()
    index = 0
    while index < len(lines):
        metadata_indexes.append(index)
        literal_match = _LITERAL_SUFFIX_RE.search(lines[index])
        if literal_match is None:
            index += 1
            continue
        if index + 1 >= len(lines):
            raise DirectImapProtocolError(
                _ERR_FETCH_LITERAL
            )
        literal_indexes.add(index + 1)
        index += 2

    fetch_starts = [
        index
        for index in metadata_indexes
        if _FETCH_START_RE.match(lines[index])
    ]
    if not fetch_starts:
        return None

    for frame_number, frame_start in enumerate(fetch_starts):
        frame_end = (
            fetch_starts[frame_number + 1]
            if frame_number + 1 < len(fetch_starts)
            else len(lines)
        )
        frame_metadata = [
            index
            for index in metadata_indexes
            if frame_start <= index < frame_end
        ]
        body_markers = [
            (index, match)
            for index in frame_metadata
            if (match := _BODY_LITERAL_RE.search(lines[index])) is not None
        ]
        uid_values: list[int] = []
        for index in frame_metadata:
            for match in _FETCH_UID_RE.finditer(lines[index]):
                try:
                    uid_value = int(match.group(1))
                except ValueError as exc:
                    raise DirectImapProtocolError(_ERR_FETCH_UID) from exc
                if not 1 <= uid_value <= MAX_IMAP_IDENTIFIER:
                    raise DirectImapProtocolError(_ERR_FETCH_UID)
                uid_values.append(uid_value)
        if expected_uid not in uid_values:
            if body_markers and not uid_values:
                raise DirectImapProtocolError(_ERR_FETCH_NO_UID)
            continue

        if len(body_markers) != 1:
            raise DirectImapProtocolError(
                _ERR_FETCH_BODY_COUNT
            )
        marker_index, marker = body_markers[0]
        literal_index = marker_index + 1
        literal = lines[literal_index]
        try:
            declared_size = int(marker.group(1))
        except ValueError as exc:
            raise DirectImapProtocolError(_ERR_FETCH_LITERAL_SIZE) from exc
        if len(literal) != declared_size:
            raise DirectImapProtocolError(
                _ERR_FETCH_LENGTH
            )

        depth = 0
        frame_closed = False
        for index in frame_metadata:
            depth += lines[index].count(b"(") - lines[index].count(b")")
            if depth == 0:
                frame_closed = True
                break
        if not frame_closed:
            raise DirectImapProtocolError(_ERR_FETCH_UNTERMINATED)
        return literal

    return None


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
        self._config = EmailSourceConfig(source_id=settings.source_id)

    @property
    def config(self) -> EmailSourceConfig:
        """Return the transport-neutral source configuration."""
        return self._config

    async def async_validate(self) -> None:
        """Validate connection, authentication, mailbox, and UID identity support."""
        client, _ = await self._async_open(self._settings.mailbox)
        await self._async_logout_cancellation_safe(client)

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
                raise DirectImapConnectionError(_ERR_SEARCH_TRANSPORT) from None

            uids = _parse_search_uids(search_response)
            for uid in uids:
                try:
                    fetch_response = await client.uid(
                        "fetch",
                        str(uid),
                        "(UID BODY.PEEK[])",
                    )
                except Exception:
                    raise DirectImapConnectionError(_ERR_FETCH_TRANSPORT) from None

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
            await self._async_logout_cancellation_safe(client)

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
                _ERR_DISPOSITION
            )

    def _validate_provenance(self, provenance: EmailProvenance) -> None:
        if provenance.source_id != self._settings.source_id:
            raise DirectImapProtocolError(_ERR_PROVENANCE_SOURCE)
        if provenance.source_type is not EmailSourceType.DIRECT_IMAP:
            raise DirectImapProtocolError(_ERR_PROVENANCE_TYPE)
        if not isinstance(provenance.transport_reference, DirectImapReference):
            raise DirectImapProtocolError(
                _ERR_PROVENANCE_REFERENCE
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
            raise DirectImapConnectionError(_ERR_CREATE_CONNECTION) from exc

        greeted = False  # pragma: no mutate
        try:
            await client.wait_hello_from_server()
            greeted = True
            state = client.get_state()
            if state == _STATE_NONAUTH:
                login_response = await client.login(
                    self._settings.username,
                    self._settings.password,
                )
                _require_ok(
                    login_response,
                    _ERR_AUTH,
                    DirectImapAuthenticationError,
                )
                state = client.get_state()
            if state != _STATE_AUTH:
                raise DirectImapAuthenticationError(
                    _ERR_AUTH_STATE
                )

            select_response = await client.select(mailbox)
            uid_validity = _parse_uidvalidity(select_response)
            return client, uid_validity
        except asyncio.CancelledError:
            await self._async_cleanup_failed_open(client, greeted=greeted)
            raise
        except DirectImapError:
            await self._async_cleanup_failed_open(client, greeted=greeted)
            raise
        except Exception:
            await self._async_cleanup_failed_open(client, greeted=greeted)
            raise DirectImapConnectionError(_ERR_CONNECTION) from None

    async def _async_cleanup_failed_open(
        self, client: _ImapClient, *, greeted: bool
    ) -> None:
        """Close a failed connection using only commands valid for its state."""
        if greeted:
            await self._async_logout_cancellation_safe(client)
        else:
            self._abort_quietly(client)

    @staticmethod
    def _abort_quietly(client: _ImapClient) -> None:
        """Best-effort transport abort that never masks the original outcome."""
        try:
            client.abort()
        except Exception:
            pass

    @classmethod
    async def _async_logout(cls, client: _ImapClient) -> None:
        try:
            await client.logout()
        except Exception:
            cls._abort_quietly(client)

    @classmethod
    async def _async_logout_cancellation_safe(cls, client: _ImapClient) -> None:
        """Finish LOGOUT even if the caller is cancelled, then propagate cancellation."""
        logout_task = asyncio.create_task(cls._async_logout(client))
        try:
            await asyncio.shield(logout_task)
        except asyncio.CancelledError:
            await logout_task
            raise
