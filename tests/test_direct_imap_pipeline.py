"""Deterministic cross-layer tests for the Direct IMAP ingestion pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

import custom_components.daylight_calendar_import.storage as storage_module
from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapSettings,
    DirectImapSource,
)
from custom_components.daylight_calendar_import.email_polling import (
    EmailPollResult,
    async_poll_email_source,
)
from custom_components.daylight_calendar_import.email_runtime import (
    email_review_source_text,
)
from custom_components.daylight_calendar_import.email_source import EmailDisposition
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.storage import (
    STORAGE_KEY,
    STORAGE_VERSION,
    PendingImportStore,
)


class FakeResponse(NamedTuple):
    result: str
    lines: list[object]


def _ok(*lines: object) -> FakeResponse:
    return FakeResponse("OK", list(lines))


class FakeImapClient:
    """Small deterministic IMAP session used by cross-layer tests."""

    def __init__(
        self,
        *,
        search_uids: tuple[int, ...] = (),
        messages: dict[int, bytes] | None = None,
        uid_validity: int = 1234,
        store_result: str = "OK",
    ) -> None:
        self.state = "NONAUTH"
        self.search_uids = search_uids
        self.messages = messages or {}
        self.uid_validity = uid_validity
        self.store_result = store_result
        self.login_calls: list[tuple[str, str]] = []
        self.select_calls: list[str] = []
        self.search_calls: list[tuple[tuple[str, ...], str | None]] = []
        self.uid_calls: list[tuple[str, tuple[str, ...]]] = []
        self.logout_calls = 0
        self.abort_calls = 0

    def get_state(self) -> str:
        return self.state

    async def wait_hello_from_server(self) -> None:
        return None

    async def login(self, user: str, password: str) -> FakeResponse:
        self.login_calls.append((user, password))
        self.state = "AUTH"
        return _ok(b"Logged in")

    async def select(self, mailbox: str = "INBOX") -> FakeResponse:
        self.select_calls.append(mailbox)
        return _ok(
            b"FLAGS (\\Seen)",
            f"OK [UIDVALIDITY {self.uid_validity}] UIDs valid".encode(),
            b"Select completed",
        )

    async def uid_search(
        self,
        *criteria: str,
        charset: str | None = "utf-8",
    ) -> FakeResponse:
        self.search_calls.append((criteria, charset))
        return _ok(
            " ".join(str(uid) for uid in self.search_uids).encode(),
            b"Search completed",
        )

    async def uid(self, command: str, *criteria: str) -> FakeResponse:
        self.uid_calls.append((command, criteria))
        command = command.casefold()
        if command == "fetch":
            uid = int(criteria[0])
            body = self.messages[uid]
            return _ok(
                f"1 FETCH (UID {uid} BODY[] {{{len(body)}}}".encode(),
                bytearray(body),
                b")",
                b"Fetch completed",
            )
        if command == "store":
            return FakeResponse(self.store_result, [b"Store completed"])
        raise AssertionError((command, criteria))

    async def logout(self) -> FakeResponse:
        self.logout_calls += 1
        return _ok(b"Logout completed")

    def abort(self) -> None:
        self.abort_calls += 1


class ScriptedFactory:
    """Return one pre-scripted IMAP session for each connection open."""

    def __init__(self, *clients: FakeImapClient) -> None:
        self._clients = list(clients)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeImapClient:
        self.calls.append(kwargs)
        if not self._clients:
            raise AssertionError("unexpected IMAP connection")
        return self._clients.pop(0)

    @property
    def remaining(self) -> int:
        return len(self._clients)


class FakeStoreBackend:
    """In-memory Home Assistant Store backend."""

    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    async def async_load(self):
        return None

    async def async_save(self, data):
        self.saved.append(data)

    async def async_remove(self):
        return None


def _make_store(monkeypatch: pytest.MonkeyPatch) -> PendingImportStore:
    backend = FakeStoreBackend()
    hass = SimpleNamespace()
    calls: list[tuple[object, int, str, bool]] = []

    def fake_store(received_hass, version, key, *, private=False):
        calls.append((received_hass, version, key, private))
        return backend

    monkeypatch.setattr(storage_module, "_PendingStore", fake_store)
    store = PendingImportStore(hass)
    assert calls == [(hass, STORAGE_VERSION, STORAGE_KEY, True)]
    return store


def _settings(factory: ScriptedFactory) -> DirectImapSource:
    return DirectImapSource(
        DirectImapSettings(
            source_id="mailbox-1",
            host="imap.example.test",
            username="calendar@example.test",
            password="app-secret",
            disposition=EmailDisposition(mark_seen=True),
        ),
        client_factory=factory,
        clock=lambda: datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
    )


def _message(
    message_id: str,
    *,
    subject: str = "Soccer",
    body: str = "Practice Friday at 5",
) -> bytes:
    return (
        f"Subject: {subject}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}"
    ).encode()


def _draft() -> EventDraft:
    return EventDraft(
        title="Soccer practice",
        start="2026-10-09T17:00:00-07:00",
        end="2026-10-09T18:00:00-07:00",
        all_day=False,
        location="Field",
        description="Bring water",
        confidence=0.95,
    )


async def _persist(
    store: PendingImportStore,
    document,
    activity_id: str,
    *,
    events: tuple[EventDraft, ...],
):
    return await store.async_add(
        source_text=email_review_source_text(document),
        events=events,
        source_id=document.upstream_source_id,
        calendar_entity="calendar.family",
        source_kind=document.kind.value,
        source_title=document.title,
        warnings=(),
        activity_id=activity_id,
    )


async def test_direct_imap_pipeline_persists_and_acknowledges_durable_message(
    monkeypatch,
) -> None:
    raw = _message("soccer@example.test")
    collect = FakeImapClient(search_uids=(42,), messages={42: raw})
    acknowledge = FakeImapClient()
    factory = ScriptedFactory(collect, acknowledge)
    source = _settings(factory)
    store = _make_store(monkeypatch)
    await store.async_load()
    pending = []

    async def processor(document, activity_id):
        pending.append(
            (await _persist(store, document, activity_id, events=(_draft(),))).pending
        )

    result = await async_poll_email_source(source, store, processor)

    assert result == EmailPollResult(
        discovered=1,
        claimed=1,
        processed=1,
        duplicates=0,
        normalization_failures=0,
        processing_failures=0,
        acknowledged=1,
    )
    assert pending[0] is not None
    assert pending[0].source_text == "Practice Friday at 5"
    assert pending[0].source_title == "Soccer"
    assert pending[0].source_kind == "email"
    assert pending[0].events[0].draft == _draft()
    assert store.get_activity(pending[0].id)["status"] == "review_ready"
    assert store.is_source_durable("<soccer@example.test>") is True

    assert collect.search_calls == [(("UnSeen UnDeleted",), "us-ascii")]
    assert collect.uid_calls == [
        ("fetch", ("42", "(UID BODY.PEEK[])")),
    ]
    assert acknowledge.uid_calls == [
        ("store", ("42", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert collect.logout_calls == 1
    assert acknowledge.logout_calls == 1
    assert factory.remaining == 0


async def test_direct_imap_pipeline_zero_event_result_is_durable_and_acknowledged(
    monkeypatch,
) -> None:
    raw = _message("fyi@example.test", subject="FYI", body="No calendar item")
    collect = FakeImapClient(search_uids=(7,), messages={7: raw})
    acknowledge = FakeImapClient()
    factory = ScriptedFactory(collect, acknowledge)
    source = _settings(factory)
    store = _make_store(monkeypatch)
    await store.async_load()
    activity_ids: list[str] = []

    async def processor(document, activity_id):
        activity_ids.append(activity_id)
        result = await _persist(store, document, activity_id, events=())
        assert result.pending is None

    result = await async_poll_email_source(source, store, processor)

    assert result.processed == 1
    assert result.acknowledged == 1
    assert result.acknowledgement_failures == 0
    assert store.get_activity(activity_ids[0])["status"] == "no_events"
    assert store.is_source_durable("<fyi@example.test>") is True
    assert acknowledge.uid_calls == [
        ("store", ("7", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert factory.remaining == 0


async def test_direct_imap_pipeline_processing_failure_retries_same_message(
    monkeypatch,
) -> None:
    raw = _message("retry@example.test")
    first_collect = FakeImapClient(search_uids=(11,), messages={11: raw})
    second_collect = FakeImapClient(search_uids=(11,), messages={11: raw})
    acknowledge = FakeImapClient()
    factory = ScriptedFactory(first_collect, second_collect, acknowledge)
    source = _settings(factory)
    store = _make_store(monkeypatch)
    await store.async_load()
    attempts: list[str] = []

    async def processor(document, activity_id):
        attempts.append(activity_id)
        if len(attempts) == 1:
            raise RuntimeError("AI unavailable")
        await _persist(store, document, activity_id, events=(_draft(),))

    first = await async_poll_email_source(source, store, processor)
    second = await async_poll_email_source(source, store, processor)

    assert first.processing_failures == 1
    assert first.processed == 0
    assert first.acknowledged == 0
    assert store.get_activity(attempts[0])["status"] == "failed"

    assert second.processing_failures == 0
    assert second.processed == 1
    assert second.acknowledged == 1
    assert len(attempts) == 2
    assert attempts[0] != attempts[1]
    assert store.get_activity(attempts[1])["status"] == "review_ready"
    assert acknowledge.uid_calls == [
        ("store", ("11", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert factory.remaining == 0


async def test_direct_imap_pipeline_ack_failure_retries_without_reprocessing(
    monkeypatch,
) -> None:
    raw = _message("ack-retry@example.test")
    first_collect = FakeImapClient(search_uids=(19,), messages={19: raw})
    failed_ack = FakeImapClient(store_result="NO")
    second_collect = FakeImapClient(search_uids=(19,), messages={19: raw})
    successful_ack = FakeImapClient()
    factory = ScriptedFactory(
        first_collect,
        failed_ack,
        second_collect,
        successful_ack,
    )
    source = _settings(factory)
    store = _make_store(monkeypatch)
    await store.async_load()
    processed = 0

    async def processor(document, activity_id):
        nonlocal processed
        processed += 1
        await _persist(store, document, activity_id, events=(_draft(),))

    first = await async_poll_email_source(source, store, processor)
    second = await async_poll_email_source(source, store, processor)

    assert first.processed == 1
    assert first.acknowledged == 0
    assert first.acknowledgement_failures == 1
    assert second.claimed == 0
    assert second.processed == 0
    assert second.duplicates == 1
    assert second.acknowledged == 1
    assert second.acknowledgement_failures == 0
    assert processed == 1

    assert failed_ack.uid_calls == [
        ("store", ("19", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert successful_ack.uid_calls == [
        ("store", ("19", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert factory.remaining == 0


async def test_direct_imap_pipeline_normalization_failure_does_not_block_later_mail(
    monkeypatch,
) -> None:
    broken = (
        b"Subject: Broken\r\n"
        b"Message-ID: <broken@example.test>\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Disposition: attachment; filename*\r\n\r\n"
        b"Body"
    )
    good = _message("good@example.test", subject="Good")
    collect = FakeImapClient(
        search_uids=(3, 4),
        messages={3: broken, 4: good},
    )
    acknowledge = FakeImapClient()
    factory = ScriptedFactory(collect, acknowledge)
    source = _settings(factory)
    store = _make_store(monkeypatch)
    await store.async_load()
    processed_titles: list[str | None] = []

    async def processor(document, activity_id):
        processed_titles.append(document.title)
        await _persist(store, document, activity_id, events=(_draft(),))

    result = await async_poll_email_source(source, store, processor)

    assert result.discovered == 2
    assert result.normalization_failures == 1
    assert result.claimed == 1
    assert result.processed == 1
    assert result.acknowledged == 1
    assert processed_titles == ["Good"]
    assert acknowledge.uid_calls == [
        ("store", ("4", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert factory.remaining == 0
