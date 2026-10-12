"""Completed history retention preserves durable work and replay suppression."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import import storage as storage_module
from custom_components.daylight_calendar_import.dedup import source_fingerprint, event_fingerprint
from custom_components.daylight_calendar_import.notification_journal import NotificationRecord
from custom_components.daylight_calendar_import.notification_preferences import normalize_notification_preferences
from test_storage import FakeStoreBackend, make_store, draft


def ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def activity(identifier, at, status="failed"):
    return {"id": identifier, "status": status, "source_title": "private metadata",
            "transitions": [{"type": "failed", "at": at, "event_id": None}]}


def request(identifier, status="sent", days=31, **kwargs):
    return NotificationRecord(identifier, "review_ready", ago(100), None, status=status,
                              terminal_at=None if status == "pending" else ago(days), **kwargs)


async def test_expire_completed_history_but_keep_every_kind_of_active_identity(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = (await store.async_add(source_text="private", events=[draft()])).pending
    identifiers = [item.id, "processing", "discovered", "claimed", "releasing", "delivery"]
    records = [activity(identifier, ago(60)) for identifier in identifiers]
    records[1]["status"] = "processing"
    records[2]["status"] = "discovered"
    store._source_claim_releases.add("releasing")
    requests = tuple(request(identifier) for identifier in identifiers) + (
        request("delivery", "pending"), request("expired"), request("recent", days=1),
    )
    await store._async_save(store._items,
        activity=tuple(records) + (activity("expired", ago(31)), activity("recent", ago(1))),
        source_claims={"claimed": source_fingerprint("claimed")}, notifications=requests)
    assert {record["id"] for record in store.list_activity()} == set(identifiers) | {"recent"}
    assert {record.import_id for record in store.list_notifications()} == set(identifiers) | {"recent"}
    assert backend.saved[-1]["source_claims"] == {"claimed": source_fingerprint("claimed")}
    assert backend.saved[-1]["items"] == [item.as_dict()]


async def test_count_caps_only_completed_records_and_use_completion_order(monkeypatch):
    monkeypatch.setattr(storage_module, "ACTIVITY_LIMIT", 1)
    monkeypatch.setattr(storage_module, "TERMINAL_NOTIFICATION_LIMIT", 1)
    store = make_store(monkeypatch, FakeStoreBackend())
    pending = request("active", "pending")
    # Old request, completed recently, is newer than one completed yesterday.
    recent_completion = request("newer", days=0)
    await store._async_save({}, activity=(activity("old", ago(1)), activity("new", ago(0)),
        activity("active", ago(100))), notifications=(recent_completion, request("older", days=1), pending))
    assert [item["id"] for item in store._activity] == ["new", "active"]
    assert store.list_notifications() == (recent_completion, pending)


async def test_legacy_terminal_requests_get_full_window_and_identity_unchanged(monkeypatch):
    backend = FakeStoreBackend({"items": [], "notifications": [
        replace(request("legacy"), terminal_at=None).as_dict()]})
    store = make_store(monkeypatch, backend)
    before = datetime.now(UTC)
    await store.async_load()
    retained = store.list_notifications()[0]
    assert before <= datetime.fromisoformat(retained.terminal_at) <= datetime.now(UTC)
    original = NotificationRecord.from_dict(backend.load_result["notifications"][0])
    assert retained.event.idempotency_key == original.event.idempotency_key
    assert len(backend.saved) == 1
    await store.async_apply_retention()
    assert len(backend.saved) == 1


async def test_long_pending_request_gets_completion_timestamp_when_sent(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    pending = request("old-pending", "pending")
    await store.async_enqueue_notification(pending)
    before = datetime.now(UTC)
    await store.async_update_notification(pending, replace(pending, status="sent"))
    sent = store.list_notifications()[0]
    assert sent.status == "sent"
    assert before <= datetime.fromisoformat(sent.terminal_at) <= datetime.now(UTC)
    assert sent.event.idempotency_key == pending.event.idempotency_key
    # Further terminal bookkeeping cannot reset the retention window.
    assert await store.async_update_notification(sent, replace(sent, terminal_at=ago(1)))
    assert store.list_notifications()[0].terminal_at == sent.terminal_at


@pytest.mark.parametrize("timestamp", [None, "broken", "2020-01-01T00:00:00"])
async def test_malformed_legacy_activity_time_does_not_authorize_age_deletion(monkeypatch, timestamp):
    store = make_store(monkeypatch, FakeStoreBackend())
    record = activity("legacy", timestamp)
    if timestamp is None:
        record["transitions"] = []
    await store._async_save({}, activity=(record,))
    assert store._activity == (record,)


async def test_failed_prune_keeps_memory_and_durable_snapshot(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    history, journal = (activity("old", ago(31)),), (request("old"),)
    store._activity, store._notifications = history, journal
    backend.save_error = OSError("disk unavailable")
    with pytest.raises(OSError):
        await store.async_apply_retention()
    assert store._activity == history and store._notifications == journal
    assert backend.saved == []


async def test_cancelled_retention_finishes_atomic_commit_and_serializes_enqueue(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    store._activity, store._notifications = (activity("old", ago(31)),), (request("old"),)
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = backend.async_save

    async def blocked(data):
        entered.set()
        await release.wait()
        await original_save(data)

    backend.async_save = blocked
    pruning = asyncio.create_task(store.async_apply_retention())
    await entered.wait()
    pruning.cancel()
    delivery = asyncio.create_task(store.async_enqueue_notification(request("new", "pending")))
    await asyncio.sleep(0)
    assert not pruning.done() and not delivery.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pruning
    assert await delivery
    assert store._activity == ()
    assert [record.import_id for record in store.list_notifications()] == ["new"]
    assert backend.saved[0] == {"items": []}
    assert backend.saved[-1]["notifications"] == [store.list_notifications()[0].as_dict()]


async def test_pending_uncertain_and_conflict_dedupe_survive_expiry_and_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    policy = normalize_notification_preferences({"enabled": True, "target": "notify.phone",
        "classes": ["calendar_write_uncertain", "conflict_detected"]})
    store.notification_preferences = lambda: policy
    item = (await store.async_add(source_text="private", events=[draft()])).pending
    event = item.events[0]
    await store.async_record_conflict(item.id, event, ["calendar.home"])
    with pytest.raises(RuntimeError):
        await store.async_approve_event(item.id, event.id, AsyncMock(side_effect=RuntimeError()))
    store._notifications = tuple(replace(record, status="sent", terminal_at=ago(60))
                                 for record in store.list_notifications())
    await store.async_apply_retention()
    assert len(store.list_notifications()) == 2
    backend.load_result = backend.saved[-1]
    # Retention made no changes, so persist simulated historical completion first.
    await store._async_save(store._items)
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    restored.notification_preferences = lambda: policy
    await restored.async_load()
    assert len(restored.list_notifications()) == 2
    assert restored.get_event(item.id, event.id).status == "write_uncertain"
    # Same revision's identity is retained even while the current checkpoint differs.
    assert any(record.conflict_revision for record in restored.list_notifications())
    # Resolve the uncertainty and reject the event: only now may requests expire.
    await restored.async_resolve_uncertain(item.id, event.id, "not_created")
    await restored.async_reject_event(item.id, event.id)
    assert restored.list_notifications() == ()


@pytest.mark.parametrize("timestamp", ["broken", "2026-01-01T00:00:00"])
def test_terminal_timestamp_rejects_invalid_wire_state(timestamp):
    with pytest.raises(ValueError):
        NotificationRecord.from_dict(replace(request("invalid"), terminal_at=timestamp).as_dict())


async def test_pending_conflict_identity_stays_silent_after_reload_with_zero_terminal_cap(monkeypatch):
    monkeypatch.setattr(storage_module, "TERMINAL_NOTIFICATION_LIMIT", 0)
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    policy = normalize_notification_preferences({"enabled": True, "target": "notify.phone",
        "classes": ["conflict_detected"]})
    store.notification_preferences = lambda: policy
    item = (await store.async_add(source_text="private", events=[draft()])).pending
    event = item.events[0]
    await store.async_record_conflict(item.id, event, ["calendar.home"])
    store._notifications = (replace(store.list_notifications()[0], status="sent", terminal_at=ago(60)),)
    await store._async_save(store._items)
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    restored.notification_preferences = lambda: policy
    await restored.async_load()
    assert await restored.async_record_conflict(item.id, event, ["calendar.home"])
    assert len(restored.list_notifications()) == 1
    assert restored.list_notifications()[0].status == "sent"


async def test_retention_never_expires_dedup_fingerprints(monkeypatch):
    handled_source = source_fingerprint("source-identity")
    handled_event = event_fingerprint(draft())
    backend = FakeStoreBackend({"items": [], "activity": [activity("old", ago(60))],
        "seen_source_fingerprints": [handled_source],
        "seen_event_fingerprints": [handled_event]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    assert store._activity == ()
    assert backend.saved[-1] == {"items": [], "seen_source_fingerprints": [handled_source],
                               "seen_event_fingerprints": [handled_event]}


async def test_reprocessing_does_not_restore_pruned_history(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = (await store.async_add(source_text="retained source", events=[draft()])).pending
    store._activity += (activity("expired", ago(60)),)
    await store.async_reprocess(item.id, AsyncMock(return_value=((), ())))
    assert store.get_activity("expired") is None
    assert all(record["id"] != "expired" for record in backend.saved[-1]["activity"])


async def test_terminal_request_with_live_provider_protected_until_task_finishes(monkeypatch):
    monkeypatch.setattr(storage_module, "TERMINAL_NOTIFICATION_LIMIT", 0)
    store = make_store(monkeypatch, FakeStoreBackend())
    terminal = request("running", "failed")
    release = asyncio.Event()
    provider = asyncio.create_task(release.wait(), name=f"Daylight notify {terminal.event.idempotency_key}")
    completed = asyncio.create_task(asyncio.sleep(0), name="unrelated-completed")
    await completed
    store.notification_tasks = {provider, completed}
    try:
        await store._async_save({}, notifications=(terminal,),
                                activity=(activity("running", ago(60)),))
        assert store.list_notifications() == (terminal,)
        assert store._activity[0]["id"] == "running"
    finally:
        release.set()
        await provider
    await store.async_apply_retention()
    assert store.list_notifications() == () and store._activity == ()
