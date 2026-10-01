"""Direct IMAP transport adapter for email ingestion."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
import re
import ssl
from typing import Protocol

from aioimaplib import AUTH, NONAUTH, AioImapException, IMAP4_SSL, Response

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


class _ImapClient(Protocol):
    """Subset of aioimaplib used by the adapter."""

    def get_state(self) -> str:
        """Return the current IMAP protocol state."""
        ...

    async def wait_hello_from_server(self) -> None:
        """Wait for the server greeting."""
        ...

    async def login(self, user: str, password: str) -> Response:
        """Authenticate."""
        ...

    async def select(self, mailbox: str = "INBOX") -> Response:
        """Select a mailbox."""
        ...

    async def uid_search(
        self, *criteria: str, charset: str | None = "utf-8"
    ) -> Response:
        """Search by UID."""
        ...

    async def uid(self, command: str, *criteria: str) -> Response:
        """Execute a UID command."""
        ...

    async def logout(self) -> Response:
        """End the IMAP session."""
        ...


type _ClientFactory = Callable[..., _ImapClient]
type _Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ssl_context(*, verify_ssl: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _require_ok(
    response: Response,
    message: str,
    error_type: type[DirectImapError] = DirectImapProtocolError,
) -> None:
    if response.result != "OK":
        raise error_type(message)


def _parse_uidvalidity(response: Response, mailbox: str) -> int:
    _require_ok(response, "IMAP mailbox selection failed", DirectImapMailboxError)
    for raw_line in response.lines:
        if match := _UIDVALIDITY_RE.search(bytes(raw_line)):
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
    response: Response,
    *,
    mailbox: str,
    uid_validity: int,
) -> tuple[int, ...]:
    _require_ok(response, "IMAP UID search failed")
    if not response.lines:
        return ()

    first_line = bytes(response.lines[0]).strip()
    if not first_line:
        return ()

    tokens = first_line.split()
    if not all(token.isdigit() for token in tokens):
        if len(response.lines) == 1:
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


def _extract_fetch_body(response: Response, *, expected_uid: int) -> bytes:
    _require_ok(response, "IMAP UID fetch failed")
    if len(response.lines) < 2:
        raise DirectImapProtocolError("IMAP UID fetch did not return a message body")

    header = bytes(response.lines[0])
    match = _FETCH_UID_RE.search(header)
    if match is None or int(match.group(1)) != expected_uid:
        raise DirectImapProtocolError("IMAP UID fetch returned the wrong message")

    body = response.lines[1]
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise DirectImapProtocolError("IMAP UID fetch returned an invalid message body")
    return bytes(body)


class DirectImapSource:
    """Collect raw RFC messages from one mailbox over direct IMAP."""

    def __init__(
        self,
        settings: DirectImapSettings,
        *,
        client_factory: _ClientFactory = IMAP4_SSL,
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
            except (TimeoutError, AioImapException, OSError) as exc:
                raise DirectImapConnectionError("IMAP UID search failed") from exc

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
                except (TimeoutError, AioImapException, OSError) as exc:
                    raise DirectImapConnectionError("IMAP UID fetch failed") from exc

                raw_message = _extract_fetch_body(
                    fetch_response,
                    expected_uid=uid,
                )
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
        except (TimeoutError, AioImapException, OSError) as exc:
            raise DirectImapConnectionError("Unable to create IMAP connection") from exc

        try:
            await client.wait_hello_from_server()
            state = client.get_state()
            if state == NONAUTH:
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
            if state != AUTH:
                raise DirectImapAuthenticationError(
                    "IMAP server did not enter authenticated state"
                )

            select_response = await client.select(mailbox)
            uid_validity = _parse_uidvalidity(select_response, mailbox)
            return client, uid_validity
        except DirectImapError:
            await self._async_logout(client)
            raise
        except (TimeoutError, AioImapException, OSError) as exc:
            await self._async_logout(client)
            raise DirectImapConnectionError("IMAP connection failed") from exc

    @staticmethod
    async def _async_logout(client: _ImapClient) -> None:
        try:
            await client.logout()
        except (TimeoutError, AioImapException, OSError):
            pass
