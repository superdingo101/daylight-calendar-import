"""Source alerts follow committed failures and never include private source data."""

import asyncio
from datetime import UTC, datetime

import pytest

from custom_components.daylight_calendar_import.notification_preferences import normalize_notification_preferences
from custom_components.daylight_calendar_import import settings_api
from tests.test_settings import FakeConnection, entry, hass_for, invoke
from test_storage import FakeStoreBackend, make_store


def enable(store):
    store.notification_preferences = lambda: normalize_notification_preferences({
        "enabled": True, "classes": ["parse_failed"], "target": "notify.phone",
    })


async def begin(store, source_id="private-source"):
    return await store.async_begin_source_submission(
        source_id=source_id, source_kind="email", source_title="Private medical appointment",
        received_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("failure", ["parse", "source", "terminal"])
async def test_source_failure_and_safe_request_commit_together(monkeypatch, failure):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    identifier = await begin(store)
    assert not store.list_notifications()
    if failure == "parse":
        await store.async_record_parse_failure(identifier)
    elif failure == "source":
        await store.async_record_source_failure(identifier, "Private provider error")
    else:
        await store.async_record_terminal_source_failure(identifier, source_id="private-source",
                                                        guidance="Private attachment failure")
    request, = store.list_notifications()
    assert request.event.type == "parse_failed" and request.transition_type == "failed"
    assert request.import_id == identifier and request.event_id is None
    assert request.target == "notify.phone"
    assert "Private" not in str(request.as_dict()) + request.event.message + request.event.title
    saved = backend.saved[-1]
    assert saved["activity"][-1]["status"] == "failed"
    assert saved["notifications"] == [request.as_dict()]
    assert not saved.get("source_claims")
    await store.async_record_source_failure(identifier, "another failure")
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    enable(restored)
    await restored.async_load()
    assert restored.list_notifications() == (request,)


async def test_failed_failure_save_does_not_publish_request(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    identifier = await begin(store)
    backend.save_error = OSError("private disk error")
    with pytest.raises(OSError):
        await store.async_record_parse_failure(identifier)
    assert not store.list_notifications()
    assert store.get_activity(identifier)["status"] == "processing"
    backend.save_error = None
    await store.async_record_parse_failure(identifier)
    assert len(store.list_notifications()) == 1


async def test_interrupted_sources_commit_alerts_once_without_historical_replay(monkeypatch):
    backend = FakeStoreBackend()
    original = make_store(monkeypatch, backend)
    completed = await begin(original, "old")
    await original.async_record_parse_failure(completed)
    identifiers = [await begin(original, str(index)) for index in range(2)]
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    enable(restored)
    await restored.async_load()
    records = restored.list_notifications()
    assert {request.import_id for request in records} == set(identifiers)
    assert all(request.event.type == "parse_failed" for request in records)
    assert all(restored.get_activity(identifier)["status"] == "failed" for identifier in identifiers)
    assert not backend.saved[-1].get("source_claims")
    backend.load_result = backend.saved[-1]
    await restored.async_load()
    assert restored.list_notifications() == records


async def test_interrupted_recovery_failed_save_keeps_loaded_truth(monkeypatch):
    backend = FakeStoreBackend()
    original = make_store(monkeypatch, backend)
    identifier = await begin(original)
    backend.load_result = backend.saved[-1]
    backend.save_error = OSError("disk failure")
    restored = make_store(monkeypatch, backend)
    enable(restored)
    with pytest.raises(OSError):
        await restored.async_load()
    assert restored.get_activity(identifier)["status"] == "processing"
    assert not restored.list_notifications()
    assert identifier in restored._source_claims
    backend.save_error = None
    await restored.async_load()
    assert len(restored.list_notifications()) == 1


async def test_interrupted_recovery_finishes_before_cancellation(monkeypatch):
    backend = FakeStoreBackend()
    original = make_store(monkeypatch, backend)
    identifier = await begin(original)
    backend.load_result = backend.saved[-1]
    entered, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save

    async def blocked(data):
        entered.set()
        await release.wait()
        await save(data)

    backend.async_save = blocked
    restored = make_store(monkeypatch, backend)
    enable(restored)
    task = asyncio.create_task(restored.async_load())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored.get_activity(identifier)["status"] == "failed"
    assert len(restored.list_notifications()) == 1
    assert not restored._source_claims


async def test_disabled_failures_never_replay_after_enable(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    identifier = await begin(store)
    await store.async_record_parse_failure(identifier)
    enable(store)
    await store.async_record_parse_failure(identifier)
    backend.load_result = backend.saved[-1]
    await store.async_load()
    assert not store.list_notifications()


async def test_source_failure_category_can_be_enabled_independently():
    connection = FakeConnection()
    await invoke(settings_api.websocket_update_notifications, hass_for(entry()), connection, {
        "id": 390, "entry_id": "entry-1",
        "expected_notifications": {"enabled": False, "classes": [], "target": None},
        "notifications": {"enabled": True, "classes": ["parse_failed"], "target": "notify.phone"},
    })
    assert not connection.errors
    assert connection.results[-1][1]["notifications"]["classes"] == ["parse_failed"]
