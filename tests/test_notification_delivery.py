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


async def test_failed_submission_is_private_bounded_and_recovers_after_restart(monkeypatch, caplog):
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
    assert caplog.text.count("retry budget exhausted") == 1
    assert "private" not in caplog.text


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


async def test_crash_after_last_attempt_is_terminal_without_fourth_call(monkeypatch, caplog):
    store = runtime(monkeypatch)
    await enqueue(store, attempts=3)
    sink = AsyncMock()
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    sink.assert_not_awaited()
    assert store.list_notifications()[0].failure == "delivery_interrupted"
    assert "retry budget exhausted" in caplog.text


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


@pytest.mark.parametrize("attempts", [2, 3])
async def test_terminal_warning_requires_committed_terminal_state(monkeypatch, caplog, attempts):
    store = runtime(monkeypatch)
    await enqueue(store, attempts=attempts)
    if attempts == 3:
        monkeypatch.setattr(store, "async_update_notification", AsyncMock(return_value=False))
    else:
        async def concurrent_change(*args):
            current = store.list_notifications()[0]
            await store.async_update_notification(current, replace(current, status="cancelled"))
            return False
        monkeypatch.setattr(delivery, "async_send_ha_notification", concurrent_change)
    await run(store)
    assert "retry budget exhausted" not in caplog.text


@pytest.mark.parametrize("failure", ["delivery_failed", "delivery_interrupted"])
async def test_terminal_commit_logs_even_when_caller_cancelled(monkeypatch, caplog, failure):
    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    original = await enqueue(store, attempts=3)
    entered, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save

    async def blocked_save(data):
        entered.set()
        await release.wait()
        await save(data)

    backend.async_save = blocked_save
    terminal = replace(original, status="failed", failure=failure)
    task = asyncio.create_task(store.async_update_notification(original, terminal))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = store.list_notifications()[0]
    assert saved == replace(terminal, terminal_at=saved.terminal_at)
    assert saved.terminal_at is not None
    terminal = saved
    assert caplog.text.count("retry budget exhausted") == 1
    assert "import-1" not in caplog.text and "notify.phone" not in caplog.text
    assert await store.async_update_notification(terminal, terminal)
    assert caplog.text.count("retry budget exhausted") == 1


@pytest.mark.parametrize("first_state", ["sent", "policy", "exhausted", "deferred"])
async def test_delivery_continues_after_independent_unactionable_requests(monkeypatch, first_state):
    """A terminal, revoked or deferred notification cannot block its successor."""
    store = runtime(monkeypatch)
    first = replace(record(), import_id="import-first", target="notify.phone")
    if first_state == "sent":
        first = replace(first, status="sent")
    elif first_state == "policy":
        first = replace(first, target="notify.other")
    elif first_state == "exhausted":
        first = replace(first, attempts=delivery.MAX_ATTEMPTS)
    else:
        first = replace(first, next_attempt_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    second = replace(record(), import_id="import-second", target="notify.phone")
    assert await store.async_enqueue_notification(first)
    assert await store.async_enqueue_notification(second)
    sink = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    current_first, current_second = store.list_notifications()
    assert current_second.status == "sent" and current_second.attempts == 1
    sink.assert_awaited_once()
    if first_state == "policy":
        assert current_first.status == "cancelled"
        assert current_first.failure == "policy_changed"
    elif first_state == "exhausted":
        assert current_first.status == "failed"
        assert current_first.failure == "delivery_interrupted"
    elif first_state == "deferred":
        assert current_first.attempts == 0


async def test_failed_comparison_on_one_notification_does_not_block_next(monkeypatch):
    store = runtime(monkeypatch)
    first = replace(record(), import_id="import-first", target="notify.phone")
    second = replace(record(), import_id="import-second", target="notify.phone")
    await store.async_enqueue_notification(first)
    await store.async_enqueue_notification(second)
    original_update = store.async_update_notification

    async def update(expected, updated):
        if expected.import_id == "import-first":
            return False
        return await original_update(expected, updated)

    monkeypatch.setattr(store, "async_update_notification", update)
    sink = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    assert store.list_notifications()[0].attempts == 0
    assert store.list_notifications()[1].status == "sent"
    sink.assert_awaited_once()


async def test_delivery_checkpoint_records_a_future_retry_deadline(monkeypatch):
    store = runtime(monkeypatch)
    await enqueue(store)
    sink = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    before = datetime.now(UTC)
    await run(store)
    request = store.list_notifications()[0]
    deadline = datetime.fromisoformat(request.next_attempt_at)
    assert timedelta(seconds=delivery.RETRY_SECONDS - 5) < deadline - before
    assert deadline - before < timedelta(seconds=delivery.RETRY_SECONDS + 5)


async def test_delivery_is_due_at_exact_retry_deadline(monkeypatch):
    deadline = datetime(2026, 10, 11, 1, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline

    store = runtime(monkeypatch)
    await enqueue(store, next_attempt_at=deadline.isoformat())
    sink = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "datetime", FrozenDateTime)
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    await run(store)
    sink.assert_awaited_once()
    assert store.list_notifications()[0].status == "sent"


async def test_active_provider_for_one_identity_does_not_block_another(monkeypatch):
    store = runtime(monkeypatch)
    first = replace(record(), import_id="import-first", target="notify.phone")
    second = replace(record(), import_id="import-second", target="notify.phone")
    await store.async_enqueue_notification(first)
    await store.async_enqueue_notification(second)
    blocker = asyncio.create_task(asyncio.Event().wait(), name=f"Daylight notify {first.event.idempotency_key}")
    store.notification_tasks.add(blocker)
    sink = AsyncMock(return_value=True)
    monkeypatch.setattr(delivery, "async_send_ha_notification", sink)
    try:
        await run(store)
        assert store.list_notifications()[0].attempts == 0
        assert store.list_notifications()[1].status == "sent"
        sink.assert_awaited_once()
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)


def test_journal_filters_ineligible_history_without_losing_later_review_requests(monkeypatch):
    """Malformed, unrelated and event-level transitions must not stop the scan."""
    store = runtime(monkeypatch)
    old = record()
    # Preserve already-committed notifications when more history is projected.
    store._notifications = (replace(old, target="notify.phone"),)
    at = "2026-10-12T12:00:00+00:00"

    def activity(import_id, kind=None, *, event_id=None):
        transitions = [] if kind is None else [
            {"type": kind, "at": at, "event_id": event_id}
        ]
        return {"id": import_id, "transitions": transitions}

    projected = store._journal_for_activity((
        activity("empty"),
        activity("event-only", "review_ready", event_id="event-1"),
        activity("unrelated", "calendar_created", event_id="event-2"),
        activity("import-a", "review_ready"),
        activity("import-a", "review_ready"),  # Same transition in one batch
        activity("import-b", "review_ready"),
    ))
    assert [request.import_id for request in projected] == [
        old.import_id, "import-a", "import-b",
    ]
    assert all(request.target == "notify.phone" for request in projected)
    assert all(request.event.type == "review_ready" for request in projected)
