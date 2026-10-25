"""Committed conflict alerts are private, bounded per revision and race safe."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import.notification_journal import NotificationRecord, notification_record
from custom_components.daylight_calendar_import.notification_preferences import normalize_notification_preferences
from custom_components.daylight_calendar_import import settings_api
from tests.test_settings import FakeConnection, entry, hass_for, invoke
from test_storage import FakeStoreBackend, make_store, draft


def enable(store):
    store.notification_preferences = lambda: normalize_notification_preferences({
        "enabled": True, "classes": ["conflict_detected"], "target": "notify.phone",
    })


async def pending(store):
    result = await store.async_add(source_text="Private email", events=[draft()])
    return result.pending.id, result.pending.events[0]


async def test_conflict_request_commits_without_changing_lifecycle(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    identifier, event = await pending(store)
    activity = store.get_activity(identifier)
    assert await store.async_record_conflict(identifier, event, ["calendar.family"])
    request, = store.list_notifications()
    assert request.event.type == "conflict_detected"
    assert request.import_id == identifier and request.event_id == event.id
    assert request.target == "notify.phone" and len(request.conflict_revision) == 64
    assert store.get_event(identifier, event.id) == event
    assert store.get_activity(identifier) == activity
    assert "Practice" not in str(request.as_dict()) + request.event.message
    assert "calendar.family" not in str(request.as_dict())
    assert backend.saved[-1]["notifications"] == [request.as_dict()]
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    enable(restored)
    await restored.async_load()
    assert await restored.async_record_conflict(identifier, event, ["calendar.family"])
    assert restored.list_notifications() == (request,)


async def test_concurrent_checks_deduplicate_scope_order_and_terminal_requests(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    identifier, event = await pending(store)
    results = await asyncio.gather(*[
        store.async_record_conflict(identifier, event, scope)
        for scope in (["calendar.a", "calendar.b"], ["calendar.b", "calendar.a", "calendar.a"]) * 5
    ])
    assert all(results) and len(store.list_notifications()) == 1
    request = store.list_notifications()[0]
    await store.async_update_notification(request, replace(request, status="sent"))
    await store.async_record_conflict(identifier, event, ["calendar.a", "calendar.b"])
    assert len(store.list_notifications()) == 1


async def test_new_revisions_and_scopes_can_alert_without_replaying_old_revisions(monkeypatch, freezer):
    store = make_store(monkeypatch, FakeStoreBackend())
    enable(store)
    identifier, event = await pending(store)
    await store.async_record_conflict(identifier, event, ["calendar.a"])
    await store.async_record_conflict(identifier, event, ["calendar.b"])
    updated = await store.async_edit_event(identifier, event.id, replace(event.draft, title="New draft"))
    await store.async_record_conflict(identifier, updated, ["calendar.a"])
    restored = await store.async_edit_event(identifier, event.id, event.draft)
    await store.async_record_conflict(identifier, restored, ["calendar.a"])
    records = store.list_notifications()
    assert len(records) == 3
    assert len({record.event.idempotency_key for record in records}) == 3
    assert len({record.occurred_at for record in records}) == 1


async def test_disabled_and_obsolete_snapshots_do_not_enqueue(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    identifier, event = await pending(store)
    assert await store.async_record_conflict(identifier, event, ["calendar.a"])
    assert not store.list_notifications()
    enable(store)
    changed = await store.async_edit_event(identifier, event.id, replace(event.draft, title="Edited"))
    assert not await store.async_record_conflict(identifier, event, ["calendar.a"])
    await store.async_remove(identifier)
    assert not await store.async_record_conflict(identifier, changed, ["calendar.a"])
    assert not store.list_notifications()


async def test_snapshot_rechecked_after_waiting_for_store_lock(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    enable(store)
    identifier, event = await pending(store)
    async with store._lock:
        task = asyncio.create_task(store.async_record_conflict(identifier, event, ["calendar.a"]))
        await asyncio.sleep(0)
        # Simulate another lock owner's committed edit before this check enters.
        store._items[identifier] = replace(store.get(identifier), events=(replace(event, draft=replace(event.draft, title="Changed")),))
    assert not await task
    assert not store.list_notifications()


async def test_failed_conflict_save_leaves_journal_unchanged_and_retryable(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    identifier, event = await pending(store)
    backend.save_error = OSError("private disk error")
    with pytest.raises(OSError):
        await store.async_record_conflict(identifier, event, ["calendar.a"])
    assert not store.list_notifications()
    backend.save_error = None
    await store.async_record_conflict(identifier, event, ["calendar.a"])
    assert len(store.list_notifications()) == 1


async def test_cancelled_conflict_save_finishes_commit(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    identifier, event = await pending(store)
    entered, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save

    async def blocked(data):
        entered.set()
        await release.wait()
        await save(data)

    backend.async_save = blocked
    task = asyncio.create_task(store.async_record_conflict(identifier, event, ["calendar.a"]))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store.list_notifications()) == 1


@pytest.mark.parametrize("revision", ["", "f" * 63, "g" * 64, 42])
def test_invalid_conflict_revision_fails_closed(revision):
    record = NotificationRecord("i", "conflict_detected", "2026-10-11T00:00:00+00:00", "e")
    with pytest.raises(ValueError, match="Invalid notification identity"):
        NotificationRecord.from_dict({**record.as_dict(), "conflict_revision": revision})


def test_conflict_revision_wire_projection_and_legacy_identity():
    from custom_components.daylight_calendar_import.notifications import notification_from_transition
    transition = {"type": "conflict_detected", "at": "2026-10-11T00:00:00+00:00", "event_id": "e"}
    legacy = notification_record({"id": "i"}, transition)
    raw = legacy.as_dict()
    raw.pop("conflict_revision")
    assert NotificationRecord.from_dict(raw) == legacy
    revised = notification_record({"id": "i"}, {**transition, "conflict_revision": "a" * 64})
    assert revised.conflict_revision == "a" * 64
    assert revised.event.idempotency_key != legacy.event.idempotency_key
    assert NotificationRecord.from_dict(revised.as_dict()) == revised
    assert notification_from_transition({"id": "i"}, {**transition, "conflict_revision": "bad"}) is None


async def test_conflict_category_can_be_enabled_independently():
    connection = FakeConnection()
    await invoke(settings_api.websocket_update_notifications, hass_for(entry()), connection, {
        "id": 390, "entry_id": "entry-1",
        "expected_notifications": {"enabled": False, "classes": [], "target": None},
        "notifications": {"enabled": True, "classes": ["conflict_detected"], "target": "notify.phone"},
    })
    assert not connection.errors
    assert connection.results[-1][1]["notifications"]["classes"] == ["conflict_detected"]
