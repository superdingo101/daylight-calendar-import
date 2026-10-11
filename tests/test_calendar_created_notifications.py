"""Notifications reflect confirmed, atomically persisted calendar outcomes."""

from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import.notification_preferences import normalize_notification_preferences
from test_storage import FakeStoreBackend, make_store, draft
from tests.test_settings import FakeConnection, entry, hass_for, invoke
from custom_components.daylight_calendar_import import settings_api


def enable(store):
    store.notification_preferences = lambda: normalize_notification_preferences({
        "enabled": True, "classes": ["calendar_created"], "target": "notify.phone",
    })


async def test_calendar_creation_and_journal_commit_together(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    result = await store.async_add(source_text="private", events=[draft()])
    assert not store.list_notifications()
    event = result.pending.events[0]
    await store.async_approve_event(result.pending.id, event.id, AsyncMock())
    assert store.list() == ()
    record = store.list_notifications()[0]
    assert record.event.type == "calendar_created" and record.event.event_id == event.id
    assert record.target == "notify.phone"
    saved = backend.saved[-1]
    assert saved["items"] == []
    assert saved["activity"][-1]["transitions"][-1]["type"] == "calendar_created"
    assert saved["notifications"] == [record.as_dict()]
    backend.load_result = saved
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.list_notifications() == (record,)


async def test_unconfirmed_or_unpersisted_write_never_notifies_created(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    result = await store.async_add(source_text="private", events=[draft()])
    event = result.pending.events[0]
    with pytest.raises(RuntimeError):
        await store.async_approve_event(result.pending.id, event.id, AsyncMock(side_effect=RuntimeError("ambiguous")))
    assert not store.list_notifications()
    assert store.get_event(result.pending.id, event.id).status == "write_uncertain"

    async def created_but_outcome_cannot_save(_event):
        backend.save_error = OSError("disk unavailable")

    # Explicitly confirm non-creation to permit another write attempt.
    await store.async_resolve_uncertain(result.pending.id, event.id, "not_created")
    with pytest.raises(OSError):
        await store.async_approve_event(result.pending.id, event.id, created_but_outcome_cannot_save)
    assert not store.list_notifications()
    assert store.get_event(result.pending.id, event.id).status == "write_uncertain"


async def test_explicit_confirmed_created_resolution_notifies_after_commit(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    enable(store)
    result = await store.async_add(source_text="private", events=[draft()])
    event = result.pending.events[0]
    with pytest.raises(RuntimeError):
        await store.async_approve_event(result.pending.id, event.id, AsyncMock(side_effect=RuntimeError()))
    assert await store.async_resolve_uncertain(result.pending.id, event.id, "created")
    assert store.list_notifications()[0].event.type == "calendar_created"
    assert store.list() == ()


async def test_calendar_created_category_can_be_enabled_independently():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 390, "entry_id": "entry-1",
        "expected_notifications": {"enabled": False, "classes": [], "target": None},
        "notifications": {"enabled": True, "classes": ["calendar_created"], "target": "notify.phone"},
    })
    assert not connection.errors
    assert connection.results[-1][1]["notifications"]["classes"] == ["calendar_created"]
