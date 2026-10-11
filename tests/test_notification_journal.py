"""Durable identity, restart, atomicity, and privacy tests for notifications."""

import asyncio

import pytest

from custom_components.daylight_calendar_import.notification_journal import (
    NotificationRecord, notification_record,
)
from test_storage import FakeStoreBackend, make_store, draft


def record(kind="review_ready", event_id=None):
    return notification_record(
        {"id": "import-1", "source_text": "private source", "title": "private title"},
        {"type": kind, "at": "2026-10-11T01:00:00+00:00", "event_id": event_id},
    )


def test_identity_round_trip_and_private_content():
    original = record("failed")
    restored = NotificationRecord.from_dict(original.as_dict())
    assert restored == original
    assert restored.event.type == "parse_failed"
    assert restored.event.idempotency_key == original.event.idempotency_key
    assert "private" not in str(original.as_dict())
    assert notification_record({"id": "i"}, {"type": "processing"}) is None
    with pytest.raises(ValueError, match="Invalid notification identity"):
        NotificationRecord.from_dict({**original.as_dict(), "transition_type": "unknown"})


async def test_journal_legacy_restart_and_unrelated_writes(monkeypatch):
    backend = FakeStoreBackend({"items": []})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    assert store.list_notifications() == ()
    first, second = record(), record("calendar_created", "event-1")
    results = await asyncio.gather(*(store.async_enqueue_notification(first) for _ in range(3)))
    assert results == [True, False, False]
    assert await store.async_enqueue_notification(second)
    pending = await store.async_add(source_text="private", events=[draft()])
    await store.async_remove(pending.pending.id)
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.list_notifications() == (first, second)
    assert not await restored.async_enqueue_notification(first)
    assert restored.list() == ()
    assert len(backend.saved[-1]["notifications"]) == 2


async def test_failed_save_preserves_committed_journal(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    await store.async_enqueue_notification(record())
    backend.save_error = OSError("disk unavailable")
    with pytest.raises(OSError):
        await store.async_enqueue_notification(record("calendar_created", "event-1"))
    assert store.list_notifications() == (record(),)
    assert len(backend.saved) == 1


async def test_journal_and_lifecycle_share_one_atomic_envelope(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    activity = ({"id": "import-1", "status": "review_ready"},)
    await store._async_save({}, activity=activity, notifications=(record(),))
    assert backend.saved == [{"items": [], "activity": list(activity),
                              "notifications": [record().as_dict()]}]
    assert store.list_notifications() == (record(),)
    backend.save_error = OSError("disk unavailable")
    with pytest.raises(OSError):
        await store._async_save({}, activity=(), notifications=())
    assert store.list_notifications() == (record(),)


async def test_cancelled_enqueue_finishes_commit_before_propagating(monkeypatch):
    backend = FakeStoreBackend()
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = backend.async_save

    async def blocked_save(data):
        entered.set()
        await release.wait()
        await original_save(data)

    backend.async_save = blocked_save
    store = make_store(monkeypatch, backend)
    task = asyncio.create_task(store.async_enqueue_notification(record()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.list_notifications() == (record(),)
    assert backend.saved[-1]["notifications"] == [record().as_dict()]
