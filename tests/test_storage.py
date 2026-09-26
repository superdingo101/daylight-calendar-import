"""Tests for persistent pending-import storage."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

import custom_components.daylight_calendar_import.storage as storage_module
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.storage import (
    STORAGE_KEY,
    STORAGE_VERSION,
    PendingImport,
    PendingImportApprovalUncertainError,
    PendingImportStore,
)


class FakeStoreBackend:
    """In-memory stand-in for Home Assistant's Store."""

    def __init__(self, load_result=None):
        self.load_result = load_result
        self.saved = []
        self.save_error = None
        self.fail_on_save_attempt = None
        self.save_attempts = 0
        self.removed = False

    async def async_load(self):
        return self.load_result

    async def async_save(self, data):
        self.save_attempts += 1
        if self.save_error is not None and (
            self.fail_on_save_attempt is None
            or self.save_attempts == self.fail_on_save_attempt
        ):
            raise self.save_error
        self.saved.append(data)

    async def async_remove(self):
        self.removed = True


def draft():
    return EventDraft(
        title="Practice",
        start="2026-10-08T17:30:00-07:00",
        end="2026-10-08T18:30:00-07:00",
        all_day=False,
        location="Park",
        description="Bring water",
        confidence=0.9,
    )


def make_store(monkeypatch, backend):
    calls = []
    hass = SimpleNamespace()

    def fake_store(received_hass, version, key, *, private=False):
        calls.append((received_hass, version, key, private))
        return backend

    monkeypatch.setattr(storage_module, "Store", fake_store)
    store = PendingImportStore(hass)
    assert calls == [(hass, STORAGE_VERSION, STORAGE_KEY, True)]
    return store


def test_pending_import_create_and_round_trip():
    pending = PendingImport.create(
        source_text="  Soccer practice email  ",
        events=[draft()],
    )

    UUID(pending.id)
    created_at = datetime.fromisoformat(pending.created_at)
    assert created_at.utcoffset().total_seconds() == 0
    assert pending.source_text == "Soccer practice email"
    assert pending.events == (draft(),)

    restored = PendingImport.from_dict(pending.as_dict())
    assert restored == pending


@pytest.mark.parametrize(
    ("source_text", "events", "message"),
    [
        ("   ", [draft()], "source_text must be a non-empty string"),
        ("text", [], "pending import must contain at least one event"),
    ],
)
def test_pending_import_rejects_invalid_input(source_text, events, message):
    with pytest.raises(ValueError, match=message):
        PendingImport.create(source_text=source_text, events=events)


async def test_store_loads_empty_state(monkeypatch):
    backend = FakeStoreBackend(load_result=None)
    store = make_store(monkeypatch, backend)

    await store.async_load()

    assert store.list() == ()
    assert store.get("missing") is None


async def test_store_restores_persisted_items(monkeypatch):
    pending = PendingImport.create(source_text="text", events=[draft()])
    backend = FakeStoreBackend(load_result={"items": [pending.as_dict()]})
    store = make_store(monkeypatch, backend)

    await store.async_load()

    assert store.list() == (pending,)
    assert store.get(pending.id) == pending


async def test_store_adds_and_removes_persistently(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)

    pending = await store.async_add(source_text=" text ", events=[draft()])

    assert store.get(pending.id) == pending
    assert store.list() == (pending,)
    assert backend.saved == [{"items": [pending.as_dict()]}]

    save_count = len(backend.saved)
    assert await store.async_remove("missing") is False
    assert len(backend.saved) == save_count

    assert await store.async_remove(pending.id) is True
    assert store.get(pending.id) is None
    assert store.list() == ()
    assert backend.saved[-1] == {"items": []}


async def test_store_keeps_memory_unchanged_when_save_fails(monkeypatch):
    existing = PendingImport.create(source_text="existing", events=[draft()])
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    backend.save_error = RuntimeError("save failed")

    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_add(source_text="new", events=[draft()])
    assert store.list() == (existing,)

    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_remove(existing.id)
    assert store.list() == (existing,)


async def test_store_can_remove_backing_storage(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)

    await store.async_remove_storage()

    assert backend.removed is True


async def test_store_processes_events_with_checkpoints(monkeypatch):
    second = EventDraft(
        title="Picture Day",
        start="2026-10-09",
        end="2026-10-10",
        all_day=True,
        confidence=1,
    )
    existing = PendingImport.create(
        source_text="existing",
        events=[draft(), second],
    )
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    processed = []

    async def processor(event):
        processed.append(event)

    result = await store.async_process_events(existing.id, processor)

    first_in_flight = PendingImport(
        id=existing.id,
        created_at=existing.created_at,
        source_text=existing.source_text,
        events=(draft(), second),
        approval_in_flight=True,
    )
    remaining = PendingImport(
        id=existing.id,
        created_at=existing.created_at,
        source_text=existing.source_text,
        events=(second,),
    )
    second_in_flight = PendingImport(
        id=existing.id,
        created_at=existing.created_at,
        source_text=existing.source_text,
        events=(second,),
        approval_in_flight=True,
    )
    assert result == existing
    assert processed == [draft(), second]
    assert store.list() == ()
    assert backend.saved == [
        {"items": [first_in_flight.as_dict()]},
        {"items": [remaining.as_dict()]},
        {"items": [second_in_flight.as_dict()]},
        {"items": []},
    ]

    assert await store.async_process_events("missing", processor) is None
    assert processed == [draft(), second]


async def test_store_partial_failure_marks_remaining_approval_uncertain(monkeypatch):
    second = EventDraft(
        title="Picture Day",
        start="2026-10-09",
        end="2026-10-10",
        all_day=True,
        confidence=1,
    )
    existing = PendingImport.create(
        source_text="existing",
        events=[draft(), second],
    )
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    store = make_store(monkeypatch, backend)
    await store.async_load()

    async def processor(event):
        if event == second:
            raise RuntimeError("processor failed")

    with pytest.raises(RuntimeError, match="processor failed"):
        await store.async_process_events(existing.id, processor)

    remaining = store.get(existing.id)
    assert remaining is not None
    assert remaining.events == (second,)
    assert remaining.approval_in_flight is True

    called = False

    async def should_not_retry(_event):
        nonlocal called
        called = True

    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_process_events(existing.id, should_not_retry)
    assert called is False


async def test_store_checkpoint_failure_blocks_automatic_retry(monkeypatch):
    existing = PendingImport.create(source_text="existing", events=[draft()])
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    backend.save_error = RuntimeError("save failed")
    backend.fail_on_save_attempt = 2
    store = make_store(monkeypatch, backend)
    await store.async_load()
    processed = []

    async def processor(event):
        processed.append(event)

    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_process_events(existing.id, processor)

    uncertain = store.get(existing.id)
    assert uncertain is not None
    assert uncertain.approval_in_flight is True
    assert processed == [draft()]

    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_process_events(existing.id, processor)
    assert processed == [draft()]


async def test_store_preflight_failure_has_no_external_side_effect(monkeypatch):
    existing = PendingImport.create(source_text="existing", events=[draft()])
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    backend.save_error = RuntimeError("save failed")
    backend.fail_on_save_attempt = 1
    store = make_store(monkeypatch, backend)
    await store.async_load()
    processed = []

    async def processor(event):
        processed.append(event)

    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_process_events(existing.id, processor)

    assert store.list() == (existing,)
    assert processed == []


async def test_process_serializes_against_reject(monkeypatch):
    existing = PendingImport.create(source_text="existing", events=[draft()])
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    started = asyncio.Event()
    release = asyncio.Event()

    async def processor(_event):
        started.set()
        await release.wait()

    process_task = asyncio.create_task(
        store.async_process_events(existing.id, processor)
    )
    await started.wait()
    reject_task = asyncio.create_task(store.async_remove(existing.id))
    await asyncio.sleep(0)

    assert reject_task.done() is False

    release.set()
    assert await process_task == existing
    assert await reject_task is False
    assert store.list() == ()


def test_pending_import_restores_legacy_ready_state():
    raw = PendingImport.create(source_text="legacy", events=[draft()]).as_dict()
    raw.pop("approval_in_flight")

    restored = PendingImport.from_dict(raw)

    assert restored.approval_in_flight is False
