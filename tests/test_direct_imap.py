"""Tests for the Direct IMAP email source adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import ssl
from typing import Any, NamedTuple

import pytest

from custom_components.daylight_calendar_import import direct_imap as direct_imap_module
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
        login_error: BaseException | None = None,
        select_error: BaseException | None = None,
        search_error: BaseException | None = None,
        logout_error: BaseException | None = None,
        abort_error: BaseException | None = None,
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
        self.login_error = login_error
        self.select_error = select_error
        self.search_error = search_error
        self.logout_error = logout_error
        self.abort_error = abort_error
        self.login_calls: list[tuple[str, str]] = []
        self.select_calls: list[str] = []
        self.search_calls: list[tuple[tuple[str, ...], str | None]] = []
        self.uid_calls: list[tuple[str, tuple[str, ...]]] = []
        self.logout_calls = 0
        self.abort_calls = 0
        self.hello_completed = False

    def get_state(self) -> str:
        return self.state

    async def wait_hello_from_server(self) -> None:
        if self.hello_error:
            raise self.hello_error
        self.hello_completed = True

    async def login(self, user: str, password: str) -> FakeResponse:
        self.login_calls.append((user, password))
        if self.login_error:
            raise self.login_error
        if self.login_response.result == "OK":
            self.state = "AUTH"
        return self.login_response

    async def select(self, mailbox: str = "INBOX") -> FakeResponse:
        self.select_calls.append(mailbox)
        if self.select_error:
            raise self.select_error
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

    def abort(self) -> None:
        self.abort_calls += 1
        if self.abort_error:
            raise self.abort_error


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
        f"1 FETCH (UID {uid} BODY[] {{{len(body)}}}".encode(),
        bytearray(body),
        b")",
        b"Fetch completed",
    )


def _fetch_body_before_uid(uid: int, body: bytes) -> FakeResponse:
    return _ok(
        f"1 FETCH (BODY[] {{{len(body)}}}".encode(),
        bytearray(body),
        f" UID {uid})".encode(),
        b"Fetch completed",
    )


def test_direct_imap_settings_and_source_config_hide_secret() -> None:
    settings = _settings()
    source, _ = _source(FakeImapClient(), settings=settings)

    assert settings.port == 993
    assert settings.mailbox == "INBOX"
    assert settings.search == "UnSeen UnDeleted"
    assert settings.charset == "us-ascii"
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


@pytest.mark.parametrize("port", (1, 65535))
def test_direct_imap_settings_accept_port_boundaries(port: int) -> None:
    assert _settings(port=port).port == port


def test_direct_imap_settings_accept_fractional_positive_timeout() -> None:
    assert _settings(timeout=0.5).timeout == 0.5


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


async def test_default_factory_uses_pinned_runtime_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import version

    import aioimaplib

    assert version("aioimaplib") == "2.0.1"

    client = FakeImapClient()
    calls: list[dict[str, Any]] = []

    def constructor(**kwargs: Any) -> FakeImapClient:
        calls.append(kwargs)
        return client

    monkeypatch.setattr(aioimaplib, "IMAP4_SSL", constructor)

    source = DirectImapSource(
        _settings(),
        clock=lambda: datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
    )
    await source.async_validate()

    assert calls[0]["host"] == "imap.example.test"
    assert client.login_calls == [("calendar@example.test", "app-secret")]
    assert client.select_calls == ["INBOX"]
    assert client.logout_calls == 1


async def test_default_factory_collects_through_runtime_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aioimaplib

    body = b"Subject: Adapter\r\n\r\nEvent"
    client = FakeImapClient(
        search_response=_ok(b"7", b"Search completed"),
        fetch_responses={"7": _fetch(7, body)},
    )
    monkeypatch.setattr(aioimaplib, "IMAP4_SSL", lambda **kwargs: client)

    source = DirectImapSource(
        _settings(),
        clock=lambda: datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
    )
    [envelope] = [item async for item in source.async_collect()]

    assert envelope.raw_message == body
    assert client.search_calls == [(("UnSeen UnDeleted",), "us-ascii")]
    assert client.uid_calls == [("fetch", ("7", "(UID BODY.PEEK[])"))]
    assert client.logout_calls == 1


@pytest.mark.parametrize(("task_done", "has_transport"), ((False, True), (True, False)))
async def test_default_factory_abort_closes_connect_resources(
    monkeypatch: pytest.MonkeyPatch,
    task_done: bool,
    has_transport: bool,
) -> None:
    import aioimaplib

    class ConnectTask:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def done(self) -> bool:
            return task_done

        def cancel(self) -> None:
            self.cancel_calls += 1

    class Transport:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    client = FakeImapClient(hello_error=asyncio.CancelledError())
    connect_task = ConnectTask()
    transport = Transport() if has_transport else None
    client._client_task = connect_task  # type: ignore[attr-defined]
    client.protocol = type("Protocol", (), {"transport": transport})()  # type: ignore[attr-defined]
    monkeypatch.setattr(aioimaplib, "IMAP4_SSL", lambda **kwargs: client)

    source = DirectImapSource(_settings())
    with pytest.raises(asyncio.CancelledError):
        await source.async_validate()

    assert connect_task.cancel_calls == (0 if task_done else 1)
    if transport is not None:
        assert transport.close_calls == 1
    assert client.logout_calls == 0


def test_runtime_adapter_abort_tolerates_missing_connect_internals() -> None:
    adapter = direct_imap_module._AioImapClientAdapter(object())
    adapter.abort()


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


async def test_collect_does_not_parse_literal_bytes_as_fetch_metadata() -> None:
    body = b"2 FETCH (UID 99 BODY[] {4}"
    client = FakeImapClient(
        search_response=_ok(b"7", b"Search completed"),
        fetch_responses={"7": _fetch(7, body)},
    )
    source, _ = _source(client)

    [envelope] = [item async for item in source.async_collect()]

    assert envelope.raw_message == body


async def test_collect_accepts_body_literal_before_uid_metadata() -> None:
    body = b"Subject: Reordered\\r\\n\\r\\nEvent"
    client = FakeImapClient(
        search_response=_ok(b"7", b"Search completed"),
        fetch_responses={"7": _fetch_body_before_uid(7, body)},
    )
    source, _ = _source(client)

    [envelope] = [item async for item in source.async_collect()]

    assert envelope.raw_message == body
    assert envelope.provenance.transport_reference == DirectImapReference(
        mailbox="INBOX",
        uid_validity=1234,
        uid=7,
    )


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
    assert all(item.provenance.source_id == "mailbox-1" for item in envelopes)
    assert all(
        item.provenance.source_type is EmailSourceType.DIRECT_IMAP
        for item in envelopes
    )
    assert client.search_calls == [(("UnSeen UnDeleted",), "us-ascii")]
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


async def test_collect_skips_vanished_uid_with_unsolicited_fetch_frame() -> None:
    client = FakeImapClient(
        search_response=_ok(b"1 2 3", b"Search completed"),
        fetch_responses={
            "1": _fetch(1, b"one"),
            "2": _fetch(99, b"unsolicited"),
            "3": _fetch(3, b"three"),
        },
    )
    source, _ = _source(client)

    envelopes = [item async for item in source.async_collect()]

    assert [item.raw_message for item in envelopes] == [b"one", b"three"]
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
    ("client_kwargs", "error_type", "message"),
    (
        (
            {"login_response": _no(b"Invalid credentials")},
            DirectImapAuthenticationError,
            "IMAP authentication failed",
        ),
        (
            {"select_response": _no(b"No such mailbox")},
            DirectImapMailboxError,
            "IMAP mailbox selection failed",
        ),
        (
            {"select_response": _ok(b"FLAGS (\\Seen)", b"Select completed")},
            DirectImapProtocolError,
            "IMAP server did not provide UIDVALIDITY",
        ),
        (
            {
                "select_response": _ok(
                    b"OK [UIDVALIDITY 4294967296] UIDs valid",
                    b"Select completed",
                )
            },
            DirectImapProtocolError,
            "IMAP server returned an invalid UIDVALIDITY",
        ),
        (
            {"select_response": _ok(object(), b"Select completed")},
            DirectImapProtocolError,
            "IMAP mailbox selection returned malformed response data",
        ),
    ),
)
async def test_validate_rejects_auth_mailbox_and_uidvalidity_failures(
    client_kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    client = FakeImapClient(**client_kwargs)
    source, _ = _source(client)

    with pytest.raises(error_type, match=f"^{message}$"):
        await source.async_validate()

    assert client.logout_calls == 1
    assert client.abort_calls == 0


async def test_validate_cancellation_aborts_pre_greeting_transport() -> None:
    client = FakeImapClient(hello_error=asyncio.CancelledError())
    source, _ = _source(client)

    with pytest.raises(asyncio.CancelledError):
        await source.async_validate()

    assert client.abort_calls == 1
    assert client.logout_calls == 0


async def test_validate_cancellation_after_greeting_logs_out() -> None:
    client = FakeImapClient(login_error=asyncio.CancelledError())
    source, _ = _source(client)

    with pytest.raises(asyncio.CancelledError):
        await source.async_validate()

    assert client.logout_calls == 1
    assert client.abort_calls == 0


async def test_validate_unexpected_error_after_greeting_logs_out() -> None:
    client = FakeImapClient(select_error=RuntimeError("select exploded"))
    source, _ = _source(client)

    with pytest.raises(DirectImapConnectionError, match="^IMAP connection failed$"):
        await source.async_validate()

    assert client.logout_calls == 1
    assert client.abort_calls == 0


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

    expected_message = (
        "Unable to create IMAP connection"
        if factory_error
        else "IMAP connection failed"
    )
    with pytest.raises(
        DirectImapConnectionError,
        match=f"^{expected_message}$",
    ):
        await source.async_validate()

    assert client.logout_calls == 0
    assert client.abort_calls == (0 if factory_error else 1)


@pytest.mark.parametrize(
    ("search_response", "message"),
    (
        (_no(b"Search rejected"), "IMAP UID search failed"),
        (
            _ok(b"1 nope", b"Search completed"),
            "IMAP UID search returned malformed identifiers",
        ),
        (_ok(b"1 nope"), "IMAP UID search returned malformed identifiers"),
        (
            _ok(b"4294967296", b"Search completed"),
            "IMAP UID search returned an invalid identifier",
        ),
        (
            _ok(object(), b"Search completed"),
            "IMAP UID search returned malformed response data",
        ),
    ),
)
async def test_collect_rejects_unsuccessful_or_malformed_search(
    search_response: FakeResponse,
    message: str,
) -> None:
    client = FakeImapClient(search_response=search_response)
    source, _ = _source(client)

    with pytest.raises(DirectImapProtocolError, match=f"^{message}$"):
        _ = [item async for item in source.async_collect()]

    assert client.logout_calls == 1


async def test_collect_wraps_search_transport_failure() -> None:
    client = FakeImapClient(search_error=OSError("connection lost"))
    source, _ = _source(client)

    with pytest.raises(DirectImapConnectionError, match="UID search failed"):
        _ = [item async for item in source.async_collect()]

    assert client.logout_calls == 1


async def test_collect_literal_without_closing_metadata_is_unterminated() -> None:
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={
            "1": _ok(
                b"1 FETCH (UID 1 BODY[] {3}",
                b"abc",
            )
        },
    )
    source, _ = _source(client)

    with pytest.raises(
        DirectImapProtocolError,
        match="^IMAP UID fetch returned an unterminated FETCH response$",
    ):
        _ = [item async for item in source.async_collect()]


async def test_collect_ignores_non_literal_metadata_before_fetch_frame() -> None:
    body = b"abc"
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={
            "1": _ok(
                b"* 1 EXISTS",
                b"1 FETCH (UID 1 BODY[] {3}",
                body,
                b")",
                b"Fetch completed",
            )
        },
    )
    source, _ = _source(client)

    [envelope] = [item async for item in source.async_collect()]
    assert envelope.raw_message == body


async def test_collect_rejects_unterminated_outer_fetch_with_nested_flags() -> None:
    body = b"abc"
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={
            "1": _ok(
                b"1 FETCH (BODY[] {3}",
                body,
                b" UID 1 FLAGS (Seen)",
                b"Fetch completed",
            )
        },
    )
    source, _ = _source(client)

    with pytest.raises(
        DirectImapProtocolError,
        match="^IMAP UID fetch returned an unterminated FETCH response$",
    ):
        _ = [item async for item in source.async_collect()]

    assert client.logout_calls == 1


async def test_collect_ignores_parentheses_after_outer_fetch_close() -> None:
    body = b"target"
    client = FakeImapClient(
        search_response=_ok(b"7", b"Search completed"),
        fetch_responses={
            "7": _ok(
                f"1 FETCH (UID 7 BODY[] {{{len(body)}}}".encode(),
                body,
                b")",
                b"Fetch completed (ok)",
            )
        },
    )
    source, _ = _source(client)

    [envelope] = [item async for item in source.async_collect()]

    assert envelope.raw_message == body


async def test_collect_handles_unrelated_frame_before_requested_frame() -> None:
    body = b"target"
    client = FakeImapClient(
        search_response=_ok(b"7", b"Search completed"),
        fetch_responses={
            "7": _ok(
                b"2 FETCH (UID 99 FLAGS (Seen))",
                f"1 FETCH (UID 7 BODY[] {{{len(body)}}}".encode(),
                body,
                b")",
                b"Fetch completed",
            )
        },
    )
    source, _ = _source(client)

    [envelope] = [item async for item in source.async_collect()]

    assert envelope.raw_message == body


async def test_collect_rejects_multiple_body_literals_for_requested_uid() -> None:
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={
            "1": _ok(
                b"1 FETCH (UID 1 BODY[] {1}",
                b"a",
                b" BODY.PEEK[] {1}",
                b"b",
                b")",
                b"Fetch completed",
            )
        },
    )
    source, _ = _source(client)

    with pytest.raises(
        DirectImapProtocolError,
        match="^IMAP UID fetch did not return exactly one BODY literal$",
    ):
        _ = [item async for item in source.async_collect()]


@pytest.mark.parametrize(
    ("fetch_response", "message"),
    (
        (_no(b"Fetch rejected"), "IMAP UID fetch failed"),
        (
            _ok(b"1 FETCH (BODY.PEEK[] {3}", b"abc", b")", b"Fetch completed"),
            "IMAP UID fetch did not identify a message",
        ),
        (
            _ok(b"1 FETCH (UID 1 BODY[] {3}"),
            "IMAP UID fetch did not return literal data",
        ),
        (
            _ok(b"1 FETCH (UID 1 FLAGS (\\Seen))", b"Fetch completed"),
            "IMAP UID fetch did not return exactly one BODY literal",
        ),
        (
            _ok(b"1 FETCH (UID 1 BODY[] {4}", b"abc", b")", b"Fetch completed"),
            "IMAP UID fetch returned a BODY literal with the wrong length",
        ),
        (
            _ok(b"1 FETCH (UID 0 BODY[] {3}", b"abc", b")", b"Fetch completed"),
            "IMAP UID fetch returned an invalid identifier",
        ),
        (
            _ok(
                f"1 FETCH (UID {IMAP_MAX + 1} BODY[] {{3}}".encode(),
                b"abc",
                b")",
                b"Fetch completed",
            ),
            "IMAP UID fetch returned an invalid identifier",
        ),
        (
            _ok(
                b"1 FETCH (UID " + b"9" * 5000 + b" BODY[] {3}",
                b"abc",
                b")",
                b"Fetch completed",
            ),
            "IMAP UID fetch returned an invalid identifier",
        ),
        (
            _ok(
                b"1 FETCH (UID 1 BODY[] {" + b"9" * 5000 + b"}",
                b"abc",
                b")",
                b"Fetch completed",
            ),
            "IMAP UID fetch returned an invalid BODY literal length",
        ),
        (
            _ok(b"1 FETCH (UID 1 BODY[] {3}", b"abc", b"Fetch completed"),
            "IMAP UID fetch returned an unterminated FETCH response",
        ),
        (
            _ok(object(), b"abc", b")", b"Fetch completed"),
            "IMAP UID fetch returned malformed response data",
        ),
        (
            _ok(b"1 FETCH (UID 1 BODY[] {3}", "not-bytes", b")"),
            "IMAP UID fetch returned malformed response data",
        ),
    ),
)
async def test_collect_rejects_invalid_fetch_responses(
    fetch_response: FakeResponse,
    message: str,
) -> None:
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={"1": fetch_response},
    )
    source, _ = _source(client)

    with pytest.raises(DirectImapProtocolError, match=f"^{message}$"):
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
    assert client.abort_calls == 1


async def test_logout_and_abort_failures_do_not_mask_success() -> None:
    client = FakeImapClient(
        logout_error=OSError("socket already closed"),
        abort_error=RuntimeError("transport close failed"),
    )
    source, _ = _source(client)

    await source.async_validate()

    assert client.logout_calls == 1
    assert client.abort_calls == 1


async def test_abort_failure_does_not_mask_pre_greeting_cancellation() -> None:
    client = FakeImapClient(
        hello_error=asyncio.CancelledError(),
        abort_error=RuntimeError("transport close failed"),
    )
    source, _ = _source(client)

    with pytest.raises(asyncio.CancelledError):
        await source.async_validate()

    assert client.logout_calls == 0
    assert client.abort_calls == 1


async def test_terminal_logout_finishes_before_cancellation_propagates() -> None:
    logout_started = asyncio.Event()
    logout_release = asyncio.Event()

    class BlockingLogoutClient(FakeImapClient):
        async def logout(self) -> FakeResponse:
            self.logout_calls += 1
            logout_started.set()
            await logout_release.wait()
            return _ok(b"Logout completed")

    client = BlockingLogoutClient()
    source, _ = _source(client)
    task = asyncio.create_task(source.async_validate())

    await logout_started.wait()
    task.cancel()
    logout_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
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

    with pytest.raises(
        DirectImapUnsupportedDispositionError,
        match="^Upstream IMAP disposition is not implemented yet$",
    ):
        await source.async_acknowledge(_provenance(), disposition=disposition)


@pytest.mark.parametrize(
    ("provenance", "message"),
    (
        (_provenance(source_id="other"), "Email provenance belongs to another source"),
        (
            _provenance(source_type="other"),  # type: ignore[arg-type]
            "Email provenance has the wrong source type",
        ),
        (
            _provenance(reference="uid:1"),
            "Email provenance has the wrong transport reference",
        ),
    ),
)
async def test_acknowledge_rejects_foreign_or_malformed_provenance(
    provenance: EmailProvenance,
    message: str,
) -> None:
    source, _ = _source(FakeImapClient())

    with pytest.raises(DirectImapProtocolError, match=f"^{message}$"):
        await source.async_acknowledge(provenance, disposition=EmailDisposition())


def test_require_ok_preserves_supplied_error_message() -> None:
    with pytest.raises(DirectImapProtocolError, match="^sentinel$"):
        direct_imap_module._require_ok(_no(b"no"), "sentinel")


def test_line_bytes_preserves_supplied_error_message() -> None:
    with pytest.raises(DirectImapProtocolError, match="^sentinel$"):
        direct_imap_module._line_bytes(object(), "sentinel")


@pytest.mark.parametrize("value", (1, IMAP_MAX))
def test_parse_uidvalidity_accepts_identifier_boundaries(value: int) -> None:
    response = _ok(f"OK [UIDVALIDITY {value}] UIDs valid".encode())
    assert direct_imap_module._parse_uidvalidity(response) == value


@pytest.mark.parametrize("value", (0, IMAP_MAX + 1))
def test_parse_uidvalidity_rejects_out_of_range_values(value: int) -> None:
    response = _ok(f"OK [UIDVALIDITY {value}] UIDs valid".encode())
    with pytest.raises(
        DirectImapProtocolError,
        match="^IMAP server returned an invalid UIDVALIDITY$",
    ):
        direct_imap_module._parse_uidvalidity(response)


def test_parse_uidvalidity_rejects_oversized_decimal_identifier() -> None:
    response = _ok(b"OK [UIDVALIDITY " + b"9" * 5000 + b"] UIDs valid")
    with pytest.raises(
        DirectImapProtocolError,
        match="^IMAP server returned an invalid UIDVALIDITY$",
    ):
        direct_imap_module._parse_uidvalidity(response)


def test_parse_search_uids_accepts_identifier_boundaries() -> None:
    response = _ok(f"1 {IMAP_MAX}".encode(), b"Search completed")
    assert direct_imap_module._parse_search_uids(response) == (1, IMAP_MAX)


@pytest.mark.parametrize("value", (0, IMAP_MAX + 1))
def test_parse_search_uids_rejects_out_of_range_values(value: int) -> None:
    response = _ok(str(value).encode(), b"Search completed")
    with pytest.raises(
        DirectImapProtocolError,
        match="^IMAP UID search returned an invalid identifier$",
    ):
        direct_imap_module._parse_search_uids(response)


def test_parse_search_uids_rejects_oversized_decimal_identifier() -> None:
    response = _ok(b"9" * 5000, b"Search completed")
    with pytest.raises(
        DirectImapProtocolError,
        match="^IMAP UID search returned an invalid identifier$",
    ):
        direct_imap_module._parse_search_uids(response)


async def test_collect_default_clock_is_timezone_aware() -> None:
    client = FakeImapClient(
        search_response=_ok(b"1", b"Search completed"),
        fetch_responses={"1": _fetch(1, b"Subject: Clock\r\n\r\nTest")},
    )
    source = DirectImapSource(_settings(), client_factory=FakeFactory(client))

    [envelope] = [item async for item in source.async_collect()]

    assert envelope.received_at.tzinfo is UTC
