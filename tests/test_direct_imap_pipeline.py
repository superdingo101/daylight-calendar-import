"""Deterministic cross-layer tests for the Direct IMAP ingestion pipeline."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Callable, NamedTuple
from unittest.mock import AsyncMock

import pytest

import custom_components.daylight_calendar_import as integration_module
import custom_components.daylight_calendar_import.storage as storage_module
from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
)
from custom_components.daylight_calendar_import.dedup import source_fingerprint
from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapSettings,
    DirectImapSource,
)
from custom_components.daylight_calendar_import.email_polling import (
    EmailPollResult,
    async_poll_email_source,
)
from custom_components.daylight_calendar_import.email_source import EmailDisposition
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import ParseOutcome
from custom_components.daylight_calendar_import.providers import SourceValidationError
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
        before_store: Callable[[], None] | None = None,
    ) -> None:
        self.state = "NONAUTH"
        self.search_uids = search_uids
        self.messages = messages or {}
        self.uid_validity = uid_validity
        self.store_result = store_result
        self.before_store = before_store
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
            if self.before_store is not None:
                self.before_store()
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
    """Restart-capable in-memory Home Assistant Store backend."""

    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    @property
    def latest(self) -> dict[str, Any] | None:
        return deepcopy(self.saved[-1]) if self.saved else None

    async def async_load(self):
        return self.latest

    async def async_save(self, data):
        self.saved.append(deepcopy(data))

    async def async_remove(self):
        return None


class FakeServices:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], object] = {}

    def async_register(self, domain, service, handler, **kwargs) -> None:
        self.handlers[(domain, service)] = (handler, kwargs)

    def async_remove(self, domain, service) -> None:
        self.handlers.pop((domain, service), None)


class FakeHass:
    def __init__(self) -> None:
        self.services = FakeServices()
        self.data: dict[str, Any] = {}


class PipelineEnvironment:
    """Install the integration and expose its real email processor closure."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        parser: AsyncMock,
    ) -> None:
        self.backend = FakeStoreBackend()
        self.parser = parser
        self.runtime_setups: list[
            tuple[object, object, PendingImportStore, object]
        ] = []
        self.store_factory_calls: list[tuple[object, int, str, bool]] = []

        def fake_store(received_hass, version, key, *, private=False):
            self.store_factory_calls.append(
                (received_hass, version, key, private)
            )
            return self.backend

        async def capture_runtime(hass, entry, store, processor):
            self.runtime_setups.append((hass, entry, store, processor))
            return SimpleNamespace(async_stop=AsyncMock())

        monkeypatch.setattr(storage_module, "_PendingStore", fake_store)
        monkeypatch.setattr(
            integration_module,
            "async_register_review_panel",
            AsyncMock(),
        )
        monkeypatch.setattr(
            integration_module,
            "parse_source_with_provider",
            parser,
        )
        monkeypatch.setattr(
            integration_module,
            "async_setup_email_runtime",
            capture_runtime,
        )

    async def async_setup(
        self,
    ) -> tuple[FakeHass, PendingImportStore, object]:
        hass = FakeHass()
        entry = SimpleNamespace(
            entry_id="pipeline-entry",
            data={
                CONF_AI_TASK_ENTITY: "ai_task.test",
                CONF_CALENDAR_ENTITY: "calendar.family",
            },
        )
        assert await integration_module.async_setup_entry(hass, entry) is True
        store = hass.data[DOMAIN][entry.entry_id]
        processor = self.runtime_setups[-1][3]
        return hass, store, processor


def _source(factory: ScriptedFactory) -> DirectImapSource:
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


def _snapshot_has_durable_source(
    snapshot: dict[str, Any] | None,
    source_id: str,
) -> bool:
    if snapshot is None:
        return False
    fingerprint = source_fingerprint(source_id)
    if fingerprint in snapshot.get("seen_source_fingerprints", ()):
        return True
    return any(
        item.get("source_fingerprint") == fingerprint
        for item in snapshot.get("items", ())
    )


async def test_direct_imap_pipeline_persists_and_acknowledges_durable_message(
    monkeypatch,
) -> None:
    parser = AsyncMock(
        return_value=ParseOutcome([_draft()], ["Review time"])
    )
    environment = PipelineEnvironment(monkeypatch, parser)
    _, store, processor = await environment.async_setup()

    raw = _message("soccer@example.test")
    collect = FakeImapClient(search_uids=(42,), messages={42: raw})

    def assert_durable_snapshot_exists() -> None:
        assert _snapshot_has_durable_source(
            environment.backend.latest,
            "<soccer@example.test>",
        )

    acknowledge = FakeImapClient(before_store=assert_durable_snapshot_exists)
    factory = ScriptedFactory(collect, acknowledge)
    source = _source(factory)

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
    pending = store.list()
    assert len(pending) == 1
    assert pending[0].source_text == "Practice Friday at 5"
    assert pending[0].source_title == "Soccer"
    assert pending[0].source_kind == "email"
    assert pending[0].warnings == ("Review time",)
    assert pending[0].events[0].draft == _draft()
    assert store.get_activity(pending[0].id)["status"] == "review_ready"
    assert store.is_source_durable("<soccer@example.test>") is True

    parser.assert_awaited_once()
    assert parser.await_args.args == (environment.runtime_setups[0][0],)
    assert parser.await_args.kwargs["ai_task_entity"] == "ai_task.test"
    parsed_source = parser.await_args.kwargs["source"]
    assert parsed_source.title == "Soccer"
    assert parsed_source.upstream_source_id == "<soccer@example.test>"

    assert collect.search_calls == [(("UnSeen UnDeleted",), "us-ascii")]
    assert collect.uid_calls == [
        ("fetch", ("42", "(UID INTERNALDATE BODY.PEEK[])")),
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
    parser = AsyncMock(return_value=ParseOutcome([], []))
    environment = PipelineEnvironment(monkeypatch, parser)
    _, store, processor = await environment.async_setup()

    raw = _message(
        "fyi@example.test",
        subject="FYI",
        body="No calendar item",
    )
    collect = FakeImapClient(search_uids=(7,), messages={7: raw})
    acknowledge = FakeImapClient()
    factory = ScriptedFactory(collect, acknowledge)
    source = _source(factory)

    result = await async_poll_email_source(source, store, processor)

    assert result.processed == 1
    assert result.acknowledged == 1
    assert result.acknowledgement_failures == 0
    assert store.list() == ()
    activity = store.list_activity()
    assert len(activity) == 1
    assert activity[0]["status"] == "no_events"
    assert store.is_source_durable("<fyi@example.test>") is True
    assert _snapshot_has_durable_source(
        environment.backend.latest,
        "<fyi@example.test>",
    )
    parser.assert_awaited_once()
    assert acknowledge.uid_calls == [
        ("store", ("7", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert factory.remaining == 0


async def test_direct_imap_pipeline_processing_failure_retries_same_message(
    monkeypatch,
) -> None:
    parser = AsyncMock(
        side_effect=[
            RuntimeError("AI unavailable"),
            ParseOutcome([_draft()], []),
        ]
    )
    environment = PipelineEnvironment(monkeypatch, parser)
    _, store, processor = await environment.async_setup()

    raw = _message("retry@example.test")
    first_collect = FakeImapClient(search_uids=(11,), messages={11: raw})
    second_collect = FakeImapClient(search_uids=(11,), messages={11: raw})
    acknowledge = FakeImapClient()
    factory = ScriptedFactory(first_collect, second_collect, acknowledge)
    source = _source(factory)

    first = await async_poll_email_source(source, store, processor)
    first_activity = store.list_activity()
    second = await async_poll_email_source(source, store, processor)

    assert first.processing_failures == 1
    assert first.processed == 0
    assert first.acknowledged == 0
    assert len(first_activity) == 1
    assert first_activity[0]["status"] == "failed"
    assert store.is_source_durable("<retry@example.test>") is True

    assert second.processing_failures == 0
    assert second.processed == 1
    assert second.acknowledged == 1
    assert parser.await_count == 2
    pending = store.list()
    assert len(pending) == 1
    assert pending[0].id != first_activity[0]["id"]
    assert store.get_activity(pending[0].id)["status"] == "review_ready"
    assert acknowledge.uid_calls == [
        ("store", ("11", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert factory.remaining == 0


async def test_direct_imap_pipeline_ack_failure_survives_restart_and_retries(
    monkeypatch,
) -> None:
    parser = AsyncMock(return_value=ParseOutcome([_draft()], []))
    environment = PipelineEnvironment(monkeypatch, parser)
    _, first_store, first_processor = await environment.async_setup()

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
    source = _source(factory)

    first = await async_poll_email_source(
        source,
        first_store,
        first_processor,
    )

    assert first.processed == 1
    assert first.acknowledged == 0
    assert first.acknowledgement_failures == 1
    first_pending = first_store.list()
    assert len(first_pending) == 1
    pending_id = first_pending[0].id
    assert _snapshot_has_durable_source(
        environment.backend.latest,
        "<ack-retry@example.test>",
    )

    _, restarted_store, restarted_processor = await environment.async_setup()
    restarted_pending = restarted_store.list()
    assert len(restarted_pending) == 1
    assert restarted_pending[0].id == pending_id
    assert restarted_pending[0].events[0].draft == _draft()
    assert restarted_store.is_source_durable(
        "<ack-retry@example.test>"
    ) is True

    second = await async_poll_email_source(
        source,
        restarted_store,
        restarted_processor,
    )

    assert second.claimed == 0
    assert second.processed == 0
    assert second.duplicates == 1
    assert second.acknowledged == 1
    assert second.acknowledgement_failures == 0
    assert parser.await_count == 1
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
    parser = AsyncMock(return_value=ParseOutcome([_draft()], []))
    environment = PipelineEnvironment(monkeypatch, parser)
    _, store, processor = await environment.async_setup()

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
    source = _source(factory)

    result = await async_poll_email_source(source, store, processor)

    assert result.discovered == 2
    assert result.normalization_failures == 1
    assert result.claimed == 1
    assert result.processed == 1
    assert result.acknowledged == 1
    parser.assert_awaited_once()
    assert parser.await_args.kwargs["source"].title == "Good"

    activities = store.list_activity()
    assert {item["status"] for item in activities} == {
        "failed",
        "review_ready",
    }
    assert acknowledge.uid_calls == [
        ("store", ("4", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert factory.remaining == 0



async def test_direct_imap_terminal_validation_failure_is_durable_across_restart(
    monkeypatch,
) -> None:
    parser = AsyncMock(
        side_effect=SourceValidationError(
            "unsupported_capability",
            "Parser does not support this attachment",
        )
    )
    environment = PipelineEnvironment(monkeypatch, parser)
    _, first_store, first_processor = await environment.async_setup()

    raw = _message("terminal@example.test", subject="Unsupported")
    first_collect = FakeImapClient(search_uids=(23,), messages={23: raw})
    failed_ack = FakeImapClient(store_result="NO")
    second_collect = FakeImapClient(search_uids=(23,), messages={23: raw})
    successful_ack = FakeImapClient()
    source = _source(
        ScriptedFactory(
            first_collect,
            failed_ack,
            second_collect,
            successful_ack,
        )
    )

    first = await async_poll_email_source(
        source,
        first_store,
        first_processor,
    )

    assert first.terminal_failures == 1
    assert first.processing_failures == 0
    assert first.processed == 0
    assert first.acknowledged == 0
    assert first.acknowledgement_failures == 1
    assert first_store.is_source_durable("<terminal@example.test>") is True
    first_activity = first_store.list_activity()
    assert len(first_activity) == 1
    assert first_activity[0]["status"] == "failed"
    assert "AI Task cannot process" in first_activity[0]["guidance"]
    parser.assert_awaited_once()

    _, restarted_store, restarted_processor = await environment.async_setup()
    assert restarted_store.is_source_durable("<terminal@example.test>") is True

    second = await async_poll_email_source(
        source,
        restarted_store,
        restarted_processor,
    )

    assert second.claimed == 0
    assert second.processed == 0
    assert second.duplicates == 1
    assert second.terminal_failures == 0
    assert second.acknowledged == 1
    assert second.acknowledgement_failures == 0
    assert parser.await_count == 1
    assert failed_ack.uid_calls == [
        ("store", ("23", "+FLAGS.SILENT", "(\\Seen)")),
    ]
    assert successful_ack.uid_calls == [
        ("store", ("23", "+FLAGS.SILENT", "(\\Seen)")),
    ]
