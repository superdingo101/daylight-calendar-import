"""Tests for the Direct IMAP email source adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import ssl
import sys
from typing import Any, NamedTuple
from types import ModuleType

import pytest

from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapAuthenticationError,
    DirectImapConnectionError,
    DirectImapMailboxError,
    DirectImapProtocolError,
    DirectImapSettings,
    DirectImapSource,
    DirectImapUnsupportedDispositionError,
)
from custom_components.daylight_calendar_import.email_source import (
    DirectImapReference,
    EmailDisposition,
    EmailProvenance,
    EmailSource,
    EmailSourceType,
)

IMAP_MAX = 4_294_967_295


class FakeResponse(NamedTuple):
    result: str
    lines: list[object]


def _ok(*lines: object) -> FakeResponse:
    return FakeResponse("OK", list(lines))


def _no(*lines: object) -> FakeResponse:
    return FakeResponse("NO", list(lines))


class FakeImapClient:
    """Deterministic aioimaplib stand-in."""

    def __init__(
        self,
        *,
        state: str = "NONAUTH",
        login_response: FakeResponse | None = None,
        select_response: FakeResponse | None = None,
        search_response: FakeResponse | None = None,
        fetch_responses: dict[str, FakeResponse | BaseException] | None = None,
        hello_error: BaseException | None = None,
        search_error: BaseException | None = None,
        logout_error: BaseException | None = None,
    ) -> None:
        self.state = state
        self.login_response = login_response or _ok(b"Logged in")
        self.select_response = select_response or _ok(
            b"FLAGS (\\Seen)",
            b"OK [UIDVALIDITY 1234] UIDs valid",
            b"Select completed",
        )
        self.search_response = search_response or _ok(b"", b"Search completed")
        self.fetch_responses = fetch_responses or {}
        self.hello_error = hello_error
        self.search_error = search_error
        self.logout_error = logout_error
        self.login_calls: list[tuple[str, str]] = []
        self.select_calls: list[str] = []
        self.search_calls: list[tuple[tuple[str, ...], str | None]] = []
        self.uid_calls: list[tuple[str, tuple[str, ...]]] = []
        self.logout_calls = 0

    def get_state(self) -> str:
        return self.state

    async def wait_hello_from_server(self) -> None:
        if self.hello_error:
            raise self.hello_error

    async def login(self, user: str, password: str) -> FakeResponse:
        self.login_calls.append((user, password))
        if self.login_response.result == "OK":
            self.state = "AUTH"
        return self.login_response

    async def select(self, mailbox: str = "INBOX") -> FakeResponse:
        self.select_calls.append(mailbox)
        return self.select_response

    async def uid_search(
        self, *criteria: str, charset: str | None = "utf-8"
    ) -> FakeResponse:
        self.search_calls.append((criteria, charset))
        if self.search_error:
            raise self.search_error
        return self.search_response

    async def uid(self, command: str, *criteria: str) -> FakeResponse:
        self.uid_calls.append((command, criteria))
        uid = criteria[0]
        response = self.fetch_responses[uid]
        if isinstance(response, BaseException):
            raise response
        return response

    async def logout(self) -> FakeResponse:
        self.logout_calls += 1
        if self.logout_error:
            raise self.logout_error
        return _ok(b"Logout completed")


class FakeFactory:
    def __init__(self, client: FakeImapClient, *, error: BaseException | None = None) -> None:
        self.client = client
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeImapClient:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.client


def _settings(**overrides: Any) -> DirectImapSettings:
    values: dict[str, Any] = {
        "source_id": "mailbox-1",
        "host": "imap.example.test",
        "username": "calendar@example.test",
        "password": "app-secret",
    }
    values.update(overrides)
    return DirectImapSettings(**values)


def _source(
    client: FakeImapClient,
    *,
    settings: DirectImapSettings | None = None,
    factory: FakeFactory | None = None,
) -> tuple[DirectImapSource, FakeFactory]:
    actual_factory = factory or FakeFactory(client)
    source = DirectImapSource(
        settings or _settings(),
        client_factory=actual_factory,
        clock=lambda: datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
    )
    return source, actual_factory


def _fetch(uid: int, body: bytes) -> FakeResponse:
    return _ok(
        f"1 FETCH (UID {uid} BODY.PEEK[] {{{len(body)}}}".encode(),
        bytearray(body),
        b")",
        b"Fetch completed",
    )


def test_direct_imap_settings_and_source_config_hide_secret() -> None:
    settings = _settings()
    source, _ = _source(FakeImapClient(), settings=settings)

    assert settings.port == 993
    assert settings.mailbox == "INBOX"
    assert settings.search == "UnSeen UnDeleted"
    assert settings.charset == "utf-8"
    assert settings.verify_ssl is True
    assert settings.timeout == 10.0
    assert "app-secret" not in repr(settings)
    assert isinstance(source, EmailSource)
    assert source.config.source_id == "mailbox-1"
    assert source.config.source_type is EmailSourceType.DIRECT_IMAP
    assert source.config.disposition == EmailDisposition()


@pytest.mark.parametrize(
    "field",
    ("source_id", "host", "username", "password", "mailbox", "search", "charset"),
)
@pytest.mark.parametrize("value", ("", "   ", None, 42))
def test_direct_imap_settings_reject_invalid_strings(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        _settings(**{field: value})


@pytest.mark.parametrize("port", (0, 65536, -1, True, 993.5))
def test_direct_imap_settings_reject_invalid_port(port: object) -> None:
    with pytest.raises(ValueError, match="port must be an integer between 1 and 65535"):
        _settings(port=port)


@pytest.mark.parametrize("verify_ssl", (0, 1, "true", None))
def test_direct_imap_settings_reject_invalid_verify_ssl(verify_ssl: object) -> None:
    with pytest.raises(ValueError, match="verify_ssl must be a boolean"):
        _settings(verify_ssl=verify_ssl)


@pytest.mark.parametrize(
    "timeout",
    (0, -1, True, "10", None, float("inf"), float("-inf"), float("nan")),
)
def test_direct_imap_settings_reject_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout must be a finite positive number"):
        _settings(timeout=timeout)


async def test_validate_connects_authenticates_selects_and_logs_out() -> None:
    client = FakeImapClient()
    source, factory = _source(client)

    await source.async_validate()

    assert client.login_calls == [("calendar@example.test", "app-secret")]
    assert client.select_calls == ["INBOX"]
    assert client.logout_calls == 1
    assert factory.calls[0]["host"] == "imap.example.test"
    assert factory.calls[0]["port"] == 993
    assert factory.calls[0]["timeout"] == 10.0
    context = factory.calls[0]["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


async def test_default_factory_loads_runtime_client_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeImapClient()
    calls: list[dict[str, Any]] = []

    def constructor(**kwargs: Any) -> FakeImapClient:
        calls.append(kwargs)
        return client

    module = ModuleType("aioimaplib")
    module.IMAP4_SSL = constructor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aioimaplib", module)

    source = DirectImapSource(
        _settings(),
        clock=lambda: datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
    )
    await source.async_validate()

    assert calls[0]["host"] == "imap.example.test"
    assert client.logout_calls == 1


async def test_validate_supports_explicit_unverified_tls() -> None:
    client = FakeImapClient()
    source, factory = _source(client, settings=_settings(verify_ssl=False))

    await source.async_validate()

    context = factory.calls[0]["ssl_context"]
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


async def test_validate_skips_login_for_preauthenticated_session() -> None:
    client = FakeImapClient(state="AUTH")
    source, _ = _source(client)

    await source.async_validate()

    assert client.login_calls == []
    assert client.select_calls == ["INBOX"]


async def test_collect_enumerates_every_matching_uid_with_uid_fetch() -> None:
    client = FakeImapClient(
        search_response=_ok(b"7 9 12", b"Search completed"),
        fetch_responses={
            "7": _fetch(7, b"Subject: One\r\n\r\nFirst"),
            "9": _fetch(9, b"Subject: Two\r\n\r\nSecond"),
            "12": _fetch(12, b"Subject: Three\r\n\r\nThird"),
        },
    )
    source, _ = _source(client)

    envelopes = [item async for item in source.async_collect()]

    assert [item.raw_message for item in envelopes] == [
        b"Subject: One\r\n\r\nFirst",
        b"Subject: Two\r\n\r\nSecond",
        b"Subject: Three\r\n\r\nThird",
    ]
    assert [item.provenance.transport_reference for item in envelopes] == [
        DirectImapReference(mailbox="INBOX", uid_validity=1234, uid=7),
        DirectImapReference(mailbox="INBOX", uid_validity=1234, uid=9),
        DirectImapReference(mailbox="INBOX", uid_validity=1234, uid=12),
    ]
    assert all(
        item.received_at == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        for item in envelopes
    )
    assert all(item.upstream_source_id is None for item in envelopes)
    assert client.search_calls == [(("UnSeen UnDeleted",), "utf-8")]
    assert client.uid_calls == [
        ("fetch", ("7", "(UID BODY.PEEK[])")),
        ("fetch", ("9", "(UID BODY.PEEK[])")),
        ("fetch", ("12", "(UID BODY.PEEK[])")),
    ]
    assert client.logout_calls == 1


@pytest.mark.parametrize(
    "search_response",
    (
        _ok(b"", b"Search completed"),
        _ok(b"Search completed"),
        FakeResponse("OK", []),
    ),
)
async def test_collect_handles_empty_search_variants(
    search_response: FakeResponse,
) -> None:
    client = FakeImapClient(search_response=search_response)
    source, _ = _source(client)

    assert [item async for item in source.async_collect()] == []
    assert client.uid_calls == []
    assert client.logout_calls == 1


@pytest.mark.parametrize(
    "vanished_response",
    (
        FakeResponse("OK", []),
        _ok(b"Fetch completed"),
    ),
)
async def test_collect_skips_uid_deleted_before_fetch(
    vanished_response: FakeResponse,
) -> None:
    client = FakeImapClient(
        search_response=_ok(b"1 2 3", b"Search completed"),
        fetch_responses={
            "1": _fetch(1, b"one"),
            "2": vanished_response,
            "3": _fetch(3, b"three"),
        },
    )
    source, _ = _source(client)

    envelopes = [item async for item in source.async_collect()]

    assert [item.raw_message for item in envelopes] == [b"one", b"three"]
    assert client.uid_calls == [
        ("fetch", ("1", "(UID BODY.PEEK[])")),
        ("fetch", ("2", "(UID BODY.PEEK[])")),
        ("fetch", ("3", "(UID BODY.PEEK[])")),
    ]
    assert client.logout_calls == 1


async def test_collect_closes_connection_when_generator_is_closed() -> None:
    client = FakeImapClient(
        search_response=_ok(b"1 2", b"Search completed"),
        fetch_responses={
            "1": _fetch(1, b"one"),
            "2": _fetch(2, b"two"),
        },
    )
    source, _ = _source(client)
    iterator = source.async_collect()

    first = await anext(iterator)
    assert first.raw_message == b"one"
    assert client.logout_calls == 0

    await iterator.aclose()
    assert client.logout_calls == 1


@pytest.mark.parametrize(
    ("client", "error_type"),
    (
        (
            FakeImapClient(login_response=_no(b"Invalid credentials")),
            DirectImapAuthenticationError,
        ),
        (
            FakeImapClient(select_response=_no(b"No such mailbox")),
            DirectImapMailboxError,
        ),
        (
            FakeImapClient(
                select_response=_ok(b"FLAGS (\\Seen)", b"Select completed")
            ),
            DirectImapProtocolError,
        ),
        (
            FakeImapClient(
                select_response=_ok(
                    b"OK [UIDVALIDITY 4294967296] UIDs valid",
                    b"Select completed",
                )
            ),
            DirectImapProtocolError,
        ),
        (
            FakeImapClient(select_response=_ok(object(), b"Select completed")),
            DirectImapProtocolError,
        ),
    ),
)
async def test_validate_rejects_auth_mailbox_and_uidvalidity_failures(
    client: FakeImapClient,
    error_type: type[Exception],
) -> None:
    source, _ = _source(client)

    with pytest.raises(error_type):
        await source.async_validate()

    assert client.logout_calls == 1


async def test_validate_cancellation_cleans_up_and_propagates() -> None:
    client = FakeImapClient(hello_error=asyncio.CancelledError())
    source, _ = _source(client)

    with pytest.raises(asyncio.CancelledError):
        await source.async_validate()

    assert client.logout_calls == 1


async def test_validate_rejects_unexpected_post_greeting_state() -> None:
    client = FakeImapClient(state="LOGOUT")
    source, _ = _source(client)

    with pytest.raises(
        DirectImapAuthenticationError,
        match="did not enter authenticated state",
    ):
        await source.async_validate()
    assert client.logout_calls == 1


@pytest.mark.parametrize(
    ("factory_error", "hello_error"),
    (
        (OSError("factory failed"), None),
        (None, TimeoutError("hello failed")),
    ),
)
async def test_validate_wraps_connection_failures(
    factory_error: BaseException | None,
    hello_error: BaseException | None,
) -> None:
    client = FakeImapClient(hello_error=hello_error)
    factory = FakeFactory(client, error=factory_error)
    source, _ = _source(client, factory=factory)

    with pytest.raises(DirectImapConnectionError):
        await source.async_validate()

    assert client.logout_calls == (0 if factory_error else 1)


@pytest.mark.parametrize(
    "search_response",
    (
        _no(b"Search rejected"),
        _ok(b"1 nope", b"Search completed"),
        _ok(b"1 nope"),
        _ok(b"4294967296", b"Search completed"),
        _ok(object(), b"Search completed"),
    ),
)
async def test_collect_rejects_unsuccessful_or_malformed_search(
    search_response: FakeResponse,
) -> None:
    client = FakeImapClient(search_response=search_response)
    source, _ = _source(client)

    with pytest.raises(DirectImapProtocolError):
        _ = [item async for item in source.async_collect()]

    assert client.logout_calls == 1


async def test_collect_wraps_search_transport_failure() -> None:
    client = FakeImapClient(search_error=OSError("connection lost"))
    source, _ = _source(client)

    with pytest.raises(DirectImapConnectionError, match="UID search failed"):
        _ = [item async for item in source.async_collect()]

    assert client.logout_calls == 1


@pytest.mark.parametrize(
    "fetch_response",
    (
        _no(b"Fetch rejected"),
        _ok(b"1 FETCH (BODY.PEEK[] {3}", b"abc", b")", b"Fetch completed"),
        _ok(b"1 FETCH (UID 2 BODY.PEEK[] {3}", b"abc", b")", b"Fetch completed"),
        _ok(b"1 FETCH (UID 1 BODY.PEEK[] {3}"),
        _ok(object(), b"abc", b")", b"Fetch completed"),
        _ok(b"1 FETCH (UID 1 BODY.PEEK[] {3}", "not-bytes", b")"),
    ),
)
async def test_collect_rejects_invalid_fetch_responses(
    fetch_response: FakeResponse,
) -> None:
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={"1": fetch_response},
    )
    source, _ = _source(client)

    with pytest.raises(DirectImapProtocolError):
        _ = [item async for item in source.async_collect()]

    assert client.logout_calls == 1


async def test_collect_wraps_fetch_transport_failure() -> None:
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={"1": OSError("connection lost")},
    )
    source, _ = _source(client)

    with pytest.raises(DirectImapConnectionError, match="UID fetch failed"):
        _ = [item async for item in source.async_collect()]

    assert client.logout_calls == 1


async def test_logout_failure_does_not_mask_success() -> None:
    client = FakeImapClient(logout_error=OSError("socket already closed"))
    source, _ = _source(client)

    await source.async_validate()
    assert client.logout_calls == 1


def _provenance(
    *,
    source_id: str = "mailbox-1",
    source_type: EmailSourceType = EmailSourceType.DIRECT_IMAP,
    reference: object | None = None,
) -> EmailProvenance:
    return EmailProvenance(
        source_id=source_id,
        source_type=source_type,
        transport_reference=(
            reference
            if reference is not None
            else DirectImapReference(mailbox="INBOX", uid_validity=1234, uid=1)
        ),
    )


async def test_acknowledge_default_is_non_destructive_noop() -> None:
    source, factory = _source(FakeImapClient())

    await source.async_acknowledge(_provenance(), disposition=EmailDisposition())

    assert factory.calls == []


@pytest.mark.parametrize(
    "disposition",
    (
        EmailDisposition(mark_seen=True),
        EmailDisposition(move_to_folder="Processed"),
        EmailDisposition(add_flag="daylight-processed"),
    ),
)
async def test_acknowledge_rejects_upstream_mutation_until_later_pr(
    disposition: EmailDisposition,
) -> None:
    source, _ = _source(FakeImapClient())

    with pytest.raises(DirectImapUnsupportedDispositionError):
        await source.async_acknowledge(_provenance(), disposition=disposition)


@pytest.mark.parametrize(
    "provenance",
    (
        _provenance(source_id="other"),
        _provenance(source_type="other"),  # type: ignore[arg-type]
        _provenance(reference="uid:1"),
    ),
)
async def test_acknowledge_rejects_foreign_or_malformed_provenance(
    provenance: EmailProvenance,
) -> None:
    source, _ = _source(FakeImapClient())

    with pytest.raises(DirectImapProtocolError):
        await source.async_acknowledge(provenance, disposition=EmailDisposition())


async def test_collect_default_clock_is_timezone_aware() -> None:
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={"1": _fetch(1, b"Subject: Clock\r\n\r\nTest")},
    )
    source = DirectImapSource(_settings(), client_factory=FakeFactory(client))

    [envelope] = [item async for item in source.async_collect()]

    assert envelope.received_at.tzinfo is UTC
