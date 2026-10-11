"""Confirmed pre-write failure and durable ambiguous-write notification paths."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import.notification_journal import NotificationRecord
from custom_components.daylight_calendar_import.notification_preferences import normalize_notification_preferences
from test_storage import FakeStoreBackend, make_store, draft


def enable(store):
    store.notification_preferences = lambda: normalize_notification_preferences({
        "enabled": True, "classes": ["calendar_create_failed", "calendar_write_uncertain"],
        "target": "notify.phone",
    })


async def pending(store):
    result = await store.async_add(source_text="private", events=[draft()])
    return result.pending, result.pending.events[0]


async def test_preflight_failure_never_attempts_calendar_write_and_notifies_atomically(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    item, event = await pending(store)
    processor = AsyncMock()
    processor.async_preflight = AsyncMock(side_effect=RuntimeError("private provider detail"))
    with pytest.raises(RuntimeError):
        await store.async_approve_event(item.id, event.id, processor)
    processor.assert_not_awaited()
    assert store.get_event(item.id, event.id).status == "pending"
    assert store.list_notifications()[0].event.type == "calendar_create_failed"
    assert backend.saved[-1]["activity"][-1]["transitions"][-1]["type"] == "calendar_create_failed"
    assert "private provider" not in str(backend.saved)


async def test_failed_preflight_record_save_preserves_original_error_and_queue(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    item, event = await pending(store)
    backend.save_error = OSError("disk unavailable")
    processor = AsyncMock()
    processor.async_preflight = AsyncMock(side_effect=RuntimeError("preflight failed"))
    with pytest.raises(RuntimeError, match="preflight failed"):
        await store.async_approve_event(item.id, event.id, processor)
    processor.assert_not_awaited()
    assert store.get_event(item.id, event.id).status == "pending"
    assert not store.list_notifications()


async def test_provider_exception_is_uncertain_and_reload_does_not_repeat_attempt_alert(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    item, event = await pending(store)
    with pytest.raises(RuntimeError):
        await store.async_approve_event(item.id, event.id, AsyncMock(side_effect=RuntimeError()))
    request = store.list_notifications()[0]
    assert request.event.type == "calendar_write_uncertain" and not request.recovered
    assert request.write_attempt == store.get_event(item.id, event.id).write_attempt
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    enable(restored)
    await restored.async_load()
    assert restored.list_notifications() == (request,)
    assert restored.get_event(item.id, event.id).status == "write_uncertain"


async def test_restart_recovers_checkpoint_without_activity_history(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item, event = await pending(store)
    with pytest.raises(asyncio.CancelledError):
        await store.async_approve_event(item.id, event.id, AsyncMock(side_effect=asyncio.CancelledError()))
    raw = backend.saved[-1]
    raw.pop("activity")
    backend.load_result = raw
    restored = make_store(monkeypatch, backend)
    enable(restored)
    await restored.async_load()
    request = restored.list_notifications()[0]
    assert request.recovered and request.write_attempt
    assert request.event.type == "calendar_write_uncertain"
    assert restored.get_event(item.id, event.id).status == "write_uncertain"
    backend.load_result = backend.saved[-1]
    again = make_store(monkeypatch, backend)
    enable(again)
    await again.async_load()
    assert again.list_notifications() == (request,)


@pytest.mark.parametrize("fail_recovery", [False, True])
async def test_unsaved_success_uses_uncertain_checkpoint_and_never_claims_failure(monkeypatch, fail_recovery):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    item, event = await pending(store)

    async def successful_write(_event):
        backend.save_error = OSError("disk unavailable")
        if not fail_recovery:
            backend.fail_on_save_attempt = backend.save_attempts + 1

    with pytest.raises(OSError):
        await store.async_approve_event(item.id, event.id, successful_write)
    assert store.get_event(item.id, event.id).status == "write_uncertain"
    requests = store.list_notifications()
    assert not requests if fail_recovery else requests[0].event.type == "calendar_write_uncertain"
    assert all(r.event.type != "calendar_create_failed" for r in requests)


async def test_recovery_ignores_pending_siblings_and_supports_legacy_attempt(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item, event = await pending(store)
    raw = item.as_dict()
    raw["events"][0]["status"] = "write_uncertain"
    raw["events"][0].pop("write_attempt", None)
    other = {**event.as_dict(), "id": "other-event"}
    raw["events"].append(other)
    backend.load_result = {"items": [raw]}
    restored = make_store(monkeypatch, backend)
    enable(restored)
    await restored.async_load()
    assert len(restored.list_notifications()) == 1
    assert restored.list_notifications()[0].write_attempt is None
    assert restored.list_notifications()[0].event_id == event.id


@pytest.mark.parametrize("changes", [{"write_attempt": ""}, {"write_attempt": 4}, {"recovered": "yes"}])
def test_malformed_recovery_provenance_fails_closed(changes):
    from test_notification_journal import record
    with pytest.raises(ValueError, match="recovery provenance"):
        NotificationRecord.from_dict({**record().as_dict(), **changes})


async def test_history_alone_cannot_manufacture_uncertain_request(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    enable(store)
    activity = ({"id": "missing", "transitions": [{"type": "calendar_write_uncertain",
                 "at": "2026-10-11T02:00:00+00:00", "event_id": "missing"}]},)
    await store._async_save({}, activity=activity)
    assert not store.list_notifications()
    item, event = await pending(store)
    activity = store._transition(item, "calendar_write_uncertain", event_id=event.id)
    await store._async_save(store._items, activity=activity)
    assert not store.list_notifications()


async def test_distinct_uncertain_attempts_remain_distinct_even_with_same_timestamp(monkeypatch, freezer):
    store = make_store(monkeypatch, FakeStoreBackend())
    enable(store)
    item, event = await pending(store)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await store.async_approve_event(item.id, event.id, AsyncMock(side_effect=RuntimeError()))
        await store.async_resolve_uncertain(item.id, event.id, "not_created")
    requests = store.list_notifications()
    assert len(requests) == 2
    assert requests[0].occurred_at == requests[1].occurred_at
    assert requests[0].write_attempt != requests[1].write_attempt
    assert requests[0].event.idempotency_key != requests[1].event.idempotency_key


@pytest.mark.parametrize("attempt", ["", 42])
def test_invalid_attempt_identity_does_not_project(attempt):
    from custom_components.daylight_calendar_import.notifications import notification_from_transition
    assert notification_from_transition({"id": "i"}, {"type": "calendar_write_uncertain",
        "at": "2026-10-11T02:00:00+00:00", "event_id": "e", "write_attempt": attempt}) is None


async def test_invalid_provenance_is_rejected_before_enqueue(monkeypatch):
    from dataclasses import replace
    from test_notification_journal import record
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    with pytest.raises(ValueError):
        await store.async_enqueue_notification(replace(record(), write_attempt=42))
    assert not backend.saved and not store.list_notifications()


async def test_missing_checkpoint_has_no_uncertainty_transition_identity(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    item, event = await pending(store)
    transition = store._transition(item, "calendar_write_uncertain", event_id="absent")[-1]["transitions"][-1]
    assert transition["write_attempt"] is None


async def test_live_cancellation_notifies_uncertainty_without_waiting_for_restart(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    enable(store)
    item, event = await pending(store)
    started = asyncio.Event()

    async def blocked(_event):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(store.async_approve_event(item.id, event.id, blocked))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    request = store.list_notifications()[0]
    assert request.event.type == "calendar_write_uncertain" and not request.recovered
    assert request.write_attempt == store.get_event(item.id, event.id).write_attempt


async def test_cancellation_during_created_save_preserves_authoritative_outcome(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    store.notification_preferences = lambda: normalize_notification_preferences({
        "enabled": True, "classes": ["calendar_created", "calendar_write_uncertain"], "target": "notify.phone",
    })
    item, event = await pending(store)
    entered, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save

    async def block_created(data):
        if data["activity"][-1]["transitions"][-1]["type"] == "calendar_created":
            entered.set()
            await release.wait()
        await save(data)

    backend.async_save = block_created
    task = asyncio.create_task(store.async_approve_event(item.id, event.id, AsyncMock()))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.list() == ()
    assert store.list_notifications()[0].event.type == "calendar_created"
    # A later journal delivery save must retain the accepted calendar outcome.
    record = store.list_notifications()[0]
    await store.async_update_notification(record, record)
    assert backend.saved[-1]["items"] == []


async def test_second_cancellation_during_uncertainty_save_finishes_commit(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    item, event = await pending(store)
    entered, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save

    async def block_uncertain(data):
        if data["activity"][-1]["transitions"][-1]["type"] == "calendar_write_uncertain":
            entered.set()
            await release.wait()
        await save(data)

    backend.async_save = block_uncertain
    task = asyncio.create_task(store.async_approve_event(item.id, event.id, AsyncMock(side_effect=asyncio.CancelledError())))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_activity(item.id)["transitions"][-1]["type"] == "calendar_write_uncertain"
    assert store.list_notifications()[0].event.type == "calendar_write_uncertain"


@pytest.mark.parametrize("recovery_fails", [False, True])
async def test_cancelled_failed_created_save_recovers_uncertainty(monkeypatch, recovery_fails):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    item, event = await pending(store)
    entered, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save

    async def fail_created(data):
        if data["activity"][-1]["transitions"][-1]["type"] == "calendar_created":
            entered.set()
            await release.wait()
            raise OSError("outcome save failed")
        if recovery_fails and data.get("notifications"):
            raise OSError("recovery save failed")
        await save(data)

    backend.async_save = fail_created
    task = asyncio.create_task(store.async_approve_event(item.id, event.id, AsyncMock()))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    checkpoint = store.get_event(item.id, event.id)
    assert checkpoint.status == "write_uncertain"
    if recovery_fails:
        assert store.list_notifications() == ()
    else:
        record = store.list_notifications()[0]
        assert record.event.type == "calendar_write_uncertain" and record.recovered
        assert record.write_attempt == checkpoint.write_attempt
        assert backend.saved[-1]["notifications"] == [record.as_dict()]


async def test_uncertain_history_without_checkpoint_does_not_block_later_failure(monkeypatch):
    """Historical uncertainty is not enough without a matching durable checkpoint."""
    store = make_store(monkeypatch, FakeStoreBackend())
    enable(store)
    item, event = await pending(store)
    at = "2026-10-12T12:00:00+00:00"
    journal = store._journal_for_activity((
        {"id": "empty", "transitions": []},
        {"id": item.id, "transitions": [{
            "type": "calendar_write_uncertain", "at": at, "event_id": event.id,
        }]},
        {"id": item.id, "transitions": [{
            "type": "calendar_create_failed", "at": at, "event_id": event.id,
        }]},
    ))
    assert len(journal) == 1
    assert journal[0].event.type == "calendar_create_failed"
    assert journal[0].event_id == event.id
    assert journal[0].target == "notify.phone"


def test_legacy_uncertain_notification_defaults_to_non_recovered():
    """Old journal records must not invent restart-recovery provenance."""
    request = NotificationRecord(
        "import-1", "calendar_write_uncertain", "2026-10-12T12:00:00+00:00",
        "event-1", target="notify.phone",
    )
    payload = request.as_dict()
    payload.pop("recovered")
    restored = NotificationRecord.from_dict(payload)
    assert restored.recovered is False
    assert restored.write_attempt is None
