"""Crash recovery, retry limits, policy changes and notification isolation."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import import notification_delivery as delivery
from custom_components.daylight_calendar_import.notification_journal import NotificationRecord
from custom_components.daylight_calendar_import.notification_preferences import normalize_notification_preferences
from test_notification_journal import record
from test_storage import FakeStoreBackend, make_store, draft


def policy(enabled=True, target="notify.phone"):
    return normalize_notification_preferences({"enabled": enabled, "target": target,
                                             "classes": ["review_ready"]})


def runtime(monkeypatch, backend=None):
    store = make_store(monkeypatch, backend or FakeStoreBackend())
    store.accepting_services = True
    store.notification_preferences = lambda: policy()
    store.notification_tasks = set()
    return store


async def enqueue(store, **changes):
    request = replace(record(), target="notify.phone", **changes)
    await store.async_enqueue_notification(request)
    return request


async def run(store):
    await delivery.async_deliver_notifications(SimpleNamespace(), store, {"entry": store.notification_tasks})
    await asyncio.sleep(0)


async def test_committed_success_is_not_replayed_after_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    await enqueue(store)
    sink = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    state = store.list_notifications()[0]
    assert state.status == "sent" and state.attempts == 1 and state.failure is None
    backend.load_result = backend.saved[-1]
    restored = runtime(monkeypatch, backend)
    await restored.async_load()
    await run(restored)
    sink.assert_awaited_once()


async def test_failed_submission_is_private_bounded_and_recovers_after_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    await enqueue(store)
    sink = AsyncMock(side_effect=RuntimeError("private credentials"))
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    monkeypatch.setattr(delivery, "RETRY_SECONDS", 0)
    await run(store)
    assert store.list_notifications()[0].status == "pending"
    backend.load_result = backend.saved[-1]
    restored = runtime(monkeypatch, backend)
    await restored.async_load()
    await run(restored)
    await run(restored)
    await run(restored)
    assert sink.await_count == 3
    assert restored.list_notifications()[0].status == "failed"
    assert restored.list_notifications()[0].failure == "delivery_failed"
    assert "private" not in str(backend.saved)


@pytest.mark.parametrize("changed", [policy(False), policy(target="notify.other")])
async def test_changed_policy_discards_unsent_request(monkeypatch, changed):
    store = runtime(monkeypatch)
    await enqueue(store)
    store.notification_preferences = lambda: changed
    sink = AsyncMock()
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    sink.assert_not_awaited()
    assert store.list_notifications()[0].status == "cancelled"


async def test_crash_after_last_attempt_is_terminal_without_fourth_call(monkeypatch):
    store = runtime(monkeypatch)
    await enqueue(store, attempts=3)
    sink = AsyncMock()
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    sink.assert_not_awaited()
    assert store.list_notifications()[0].failure == "delivery_interrupted"


async def test_retry_deadline_and_closed_admission_defer_delivery(monkeypatch):
    store = runtime(monkeypatch)
    await enqueue(store, next_attempt_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    sink = AsyncMock()
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    store.accepting_services = False
    await run(store)
    sink.assert_not_awaited()
    assert store.list_notifications()[0].attempts == 0


async def test_attempt_save_failure_prevents_side_effect(monkeypatch):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    await enqueue(store)
    backend.save_error = OSError("no disk")
    sink = AsyncMock()
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    with pytest.raises(OSError):
        await run(store)
    sink.assert_not_awaited()
    assert store.list_notifications()[0].attempts == 0


async def test_policy_change_during_attempt_save_prevents_send(monkeypatch):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    await enqueue(store)
    save = backend.async_save

    async def change_policy(data):
        await save(data)
        store.notification_preferences = lambda: policy(False)

    backend.async_save = change_policy
    sink = AsyncMock()
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    sink.assert_not_awaited()
    assert store.list_notifications()[0].attempts == 1


async def test_optimistic_delivery_snapshot_and_identity_validation(monkeypatch):
    store = runtime(monkeypatch)
    original = await enqueue(store)
    newer = replace(original, attempts=1)
    assert await store.async_update_notification(original, newer)
    assert not await store.async_update_notification(original, replace(original, status="sent"))
    with pytest.raises(ValueError, match="identity cannot change"):
        await store.async_update_notification(newer, replace(newer, event_id="other"))


@pytest.mark.parametrize("changes", [
    {"status": "other"}, {"attempts": True}, {"attempts": 4}, {"failure": "private text"},
    {"next_attempt_at": "2026-10-11T01:00:00"}, {"target": "calendar.private"},
])
def test_invalid_delivery_state_fails_closed(changes):
    with pytest.raises(ValueError):
        NotificationRecord.from_dict({**record().as_dict(), **changes})


async def test_review_ready_enqueue_is_atomic_and_does_not_replay_old_activity(monkeypatch):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    result = await store.async_add(source_text="private", events=[draft()])
    persisted = backend.saved[-1]
    assert len(persisted["notifications"]) == 1
    assert persisted["activity"][-1]["status"] == "review_ready"
    request = store.list_notifications()[0]
    assert request.target == "notify.phone"
    await store._async_save(store._items, activity=store._activity)
    assert store.list_notifications() == (request,)
    assert result.pending.id == request.import_id


async def test_scheduler_failure_does_not_change_committed_import(monkeypatch, caplog):
    store = runtime(monkeypatch)
    store.on_notifications = lambda: (_ for _ in ()).throw(RuntimeError("private"))
    result = await store.async_add(source_text="private", events=[draft()])
    assert store.get(result.pending.id) is not None
    assert len(store.list_notifications()) == 1
    assert "durable request retained" in caplog.text
    assert "private" not in caplog.text


async def test_timeout_retains_stubborn_provider_and_prevents_overlapping_retry(monkeypatch):
    store = runtime(monkeypatch)
    request = await enqueue(store)
    release, started = asyncio.Event(), asyncio.Event()
    calls = 0

    async def stubborn(*args):
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("private late failure")

    monkeypatch.setattr(delivery, "async_send_ha_notification", stubborn)
    monkeypatch.setattr(delivery, "DELIVERY_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(delivery, "RETRY_SECONDS", 0)
    await run(store)
    assert started.is_set()
    assert len(store.notification_tasks) == 1
    await run(store)
    assert calls == 1 and store.list_notifications()[0].attempts == 1
    tasks = tuple(store.notification_tasks)
    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert not store.notification_tasks
    assert request.event.idempotency_key == store.list_notifications()[0].event.idempotency_key


async def test_cancelled_worker_resumes_checkpointed_attempt_on_reload(monkeypatch):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    await enqueue(store)
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(delivery, "async_send_ha_notification", blocked)
    monkeypatch.setattr(delivery, "RETRY_SECONDS", 0)
    task = asyncio.create_task(run(store))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert store.list_notifications()[0].attempts == 1
    backend.load_result = backend.saved[-1]
    restored = runtime(monkeypatch, backend)
    await restored.async_load()
    sink = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(restored)
    assert restored.list_notifications()[0].status == "sent"
    assert restored.list_notifications()[0].attempts == 2


async def test_global_provider_capacity_defers_without_consuming_attempt(monkeypatch):
    store = runtime(monkeypatch)
    await enqueue(store)
    tasks = {asyncio.create_task(asyncio.Event().wait()) for _ in range(delivery.MAX_ACTIVE_TASKS)}
    try:
        await delivery.async_deliver_notifications(None, store, {"retired": tasks})
        assert store.list_notifications()[0].attempts == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_compare_failure_does_not_send_stale_request(monkeypatch):
    store = runtime(monkeypatch)
    await enqueue(store)
    monkeypatch.setattr(store, "async_update_notification", AsyncMock(return_value=False))
    sink = AsyncMock()
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    sink.assert_not_awaited()


async def test_cancelled_journal_update_commits_before_propagating(monkeypatch):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    original = await enqueue(store)
    entered, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save

    async def blocked_save(data):
        entered.set()
        await release.wait()
        await save(data)

    backend.async_save = blocked_save
    task = asyncio.create_task(store.async_update_notification(original, replace(original, attempts=1)))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.list_notifications()[0].attempts == 1


async def test_cancel_entry_owned_notification_tasks():
    import asyncio
    from types import SimpleNamespace
    from custom_components.daylight_calendar_import import _async_cancel_notification_tasks

    async def blocked():
        await asyncio.Event().wait()
    task = asyncio.create_task(blocked())
    store = SimpleNamespace(notification_tasks={task})
    await _async_cancel_notification_tasks(store)
    assert task.cancelled()
    await _async_cancel_notification_tasks(SimpleNamespace(notification_tasks=set()))


async def test_unload_deadline_does_not_wait_for_stubborn_notification_task(
    monkeypatch, caplog,
):
    import asyncio
    from types import SimpleNamespace
    from custom_components.daylight_calendar_import import _async_cancel_notification_tasks

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._NOTIFICATION_CANCEL_GRACE_SECONDS",
        0.002,
    )
    release = asyncio.Event()
    started = asyncio.Event()
    async def ignores_cancel():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
    task = asyncio.create_task(ignores_cancel())
    await started.wait()
    store = SimpleNamespace(notification_tasks={task})
    await asyncio.wait_for(_async_cancel_notification_tasks(store), timeout=0.5)
    assert not task.done()
    assert "did not stop before unload deadline" in caplog.text
    release.set()
    await asyncio.wait_for(task, timeout=1)


async def test_history_without_transitions_and_already_journaled_identity_are_not_enqueued(monkeypatch):
    store = runtime(monkeypatch)
    original = await enqueue(store)
    activity = ({"id": "legacy", "transitions": []},
                {"id": original.import_id, "transitions": [
                    {"type": original.transition_type, "at": original.occurred_at, "event_id": None}]})
    await store._async_save({}, activity=activity)
    assert store.list_notifications() == (original,)


async def test_enabling_consent_does_not_replay_existing_review_ready_history(monkeypatch):
    store = runtime(monkeypatch)
    store.notification_preferences = lambda: policy(False)
    await store.async_add(source_text="private", events=[draft()])
    assert store.list_notifications() == ()
    store.notification_preferences = lambda: policy()
    await store._async_save(store._items, activity=store._activity)
    assert store.list_notifications() == ()
