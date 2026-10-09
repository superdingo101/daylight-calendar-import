"""Tests for persistent pending-import storage and deduplication."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from homeassistant.helpers.storage import Store

import custom_components.daylight_calendar_import.storage as storage_module
from custom_components.daylight_calendar_import.dedup import (
    event_fingerprint,
    source_fingerprint,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.storage import (
    STORAGE_KEY,
    STORAGE_VERSION,
    PendingImport,
    PendingImportApprovalUncertainError,
    PendingEvent,
    PendingEventEditError,
    PendingEventResolutionError,
    PendingImportStore,
    _migrate_v1,
    _PendingStore,
    _remember_fingerprints,
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


async def test_recovery_snapshot_cannot_resolve_a_new_write_attempt(monkeypatch):
    item = PendingImport.create(source_text="school", events=[draft()])
    raw = item.as_dict()
    raw["events"][0]["status"] = "write_uncertain"
    backend = FakeStoreBackend(load_result={"items": [raw]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    original = store.get_event(item.id, item.events[0].id)
    assert original is not None
    assert await store.async_resolve_uncertain(item.id, original.id, "not_created",
                                               expected_event=original)
    edited = await store.async_edit_event(item.id, original.id,
                                          replace(draft(), title="Changed meeting"))
    assert edited is not None
    async def failed(_event):
        raise RuntimeError("calendar response lost")
    with pytest.raises(RuntimeError, match="calendar response lost"):
        await store.async_approve_event(item.id, original.id, failed)
    with pytest.raises(PendingEventResolutionError, match="changed since it was loaded"):
        await store.async_resolve_uncertain(item.id, original.id, "created",
                                           expected_event=original)
    assert store.get_event(item.id, original.id).status == "write_uncertain"
    assert store.get_event(item.id, original.id).draft.title == "Changed meeting"

    same = PendingImport.create(source_text="same draft", events=[draft()])
    same_raw = same.as_dict()
    same_raw["events"][0]["status"] = "write_uncertain"
    same_backend = FakeStoreBackend(load_result={"items": [same_raw]})
    retry = make_store(monkeypatch, same_backend)
    await retry.async_load()
    previous_attempt = retry.get_event(same.id, same.events[0].id)
    assert previous_attempt is not None
    await retry.async_resolve_uncertain(same.id, previous_attempt.id, "not_created",
                                        expected_event=previous_attempt)
    with pytest.raises(RuntimeError, match="calendar response lost"):
        await retry.async_approve_event(same.id, previous_attempt.id, failed)
    next_attempt = retry.get_event(same.id, previous_attempt.id)
    assert next_attempt is not None
    assert next_attempt.draft == previous_attempt.draft
    assert next_attempt.write_attempt is not None
    assert next_attempt.as_service_dict()["write_attempt"] == next_attempt.write_attempt
    same_backend.load_result = same_backend.saved[-1]
    restarted = make_store(monkeypatch, same_backend)
    await restarted.async_load()
    assert restarted.get_event(same.id, previous_attempt.id) == next_attempt
    with pytest.raises(PendingEventResolutionError, match="changed since it was loaded"):
        await restarted.async_resolve_uncertain(same.id, previous_attempt.id, "created",
                                               expected_event=previous_attempt)
    assert await restarted.async_resolve_uncertain(same.id, previous_attempt.id, "created",
                                                  expected_event=next_attempt)


def second_draft():
    return EventDraft(
        title="Picture Day",
        start="2026-10-09",
        end="2026-10-10",
        all_day=True,
        confidence=1,
    )


def make_store(monkeypatch, backend):
    calls = []
    hass = SimpleNamespace()

    def fake_store(received_hass, version, key, *, private=False):
        calls.append((received_hass, version, key, private))
        return backend

    monkeypatch.setattr(storage_module, "_PendingStore", fake_store)
    store = PendingImportStore(hass)
    assert calls == [(hass, STORAGE_VERSION, STORAGE_KEY, True)]
    return store


def test_pending_import_create_and_round_trip():
    source_fp = source_fingerprint("message-1")
    pending = PendingImport.create(
        source_text="  Soccer practice email  ",
        events=[draft()],
        source_fingerprint=source_fp,
    )

    UUID(pending.id)
    created_at = datetime.fromisoformat(pending.created_at)
    assert created_at.utcoffset().total_seconds() == 0
    assert pending.source_text == "Soccer practice email"
    assert tuple(event.draft for event in pending.events) == (draft(),)
    UUID(pending.events[0].id)
    assert pending.source_fingerprint == source_fp
    assert pending.as_dict()["source_fingerprint"] == source_fp
    assert pending.as_service_dict()["events"][0] == {
        **draft().as_dict(), "id": pending.events[0].id, "status": "pending", "calendar_entity": None,
    }
    assert pending.as_service_dict()["approval_in_flight"] is False

    restored = PendingImport.from_dict(pending.as_dict())
    assert restored == pending

    without_source = PendingImport.create(source_text="text", events=[draft()])
    assert "source_fingerprint" not in without_source.as_dict()


def test_review_metadata_round_trip_and_legacy_defaults():
    item = PendingImport.create(
        source_text="Extracted schedule", events=[draft()], source_kind="email",
        source_title="School schedule", source_sender="Teacher <teacher@example.test>",
        warnings=["Event 2 had no date"], duplicate_events=2,
    )
    assert PendingImport.from_dict(item.as_dict()) == item
    assert item.source_kind == "email"
    assert item.as_service_dict()["warnings"] == ["Event 2 had no date"]
    assert item.as_service_dict()["duplicate_events"] == 2
    assert item.as_dict()["source_title"] == "School schedule"
    assert item.as_dict()["source_sender"] == "Teacher <teacher@example.test>"
    assert item.as_service_dict()["source_sender"] == "Teacher <teacher@example.test>"
    legacy = PendingImport.from_dict({
        "id": item.id, "created_at": item.created_at, "source_text": item.source_text,
        "events": [event.as_dict() for event in item.events],
    })
    assert legacy.source_kind == "manual_text"
    assert legacy.source_title is None
    assert legacy.source_sender is None
    assert legacy.warnings == ()
    assert legacy.duplicate_events == 0
    assert legacy.as_service_dict()["source_title"] is None
    assert "source_sender" not in legacy.as_service_dict()
    assert legacy.as_service_dict()["duplicate_events"] == 0


async def test_review_metadata_survives_edit_reject_checkpoint_and_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(
        source_text="Newsletter", events=[draft(), second_draft()],
        source_kind="pdf", source_title="school.pdf", warnings=["Review the year"],
    )).pending
    assert item is not None
    first, second = item.events
    metadata = (item.source_kind, item.source_title, item.warnings, item.duplicate_events)

    await store.async_edit_event(item.id, first.id, replace(draft(), title="Updated"))
    assert store.get_activity(item.id)["title"] == "Updated"
    assert backend.saved[-1]["activity"][-1]["title"] == "Updated"
    assert [row["type"] for row in store.get_activity(item.id)["transitions"]] == ["review_ready"]
    assert (store.get(item.id).source_kind, store.get(item.id).source_title,
            store.get(item.id).warnings, store.get(item.id).duplicate_events) == metadata
    async def failed(_event):
        raise RuntimeError("calendar unavailable")
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        await store.async_approve_event(item.id, first.id, failed)
    assert store.get(item.id).source_title == "school.pdf"
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get(item.id).warnings == ("Review the year",)
    assert await restarted.async_resolve_uncertain(item.id, first.id, "not_created")
    assert await restarted.async_reject_event(item.id, second.id)
    assert (restarted.get(item.id).source_kind, restarted.get(item.id).source_title,
            restarted.get(item.id).warnings) == metadata[:3]


async def test_activity_survives_completion_failure_and_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="private source", events=[draft()],
                                  source_kind="pdf", source_title="schedule.pdf")).pending
    assert item is not None
    assert store.list_activity()[0] == {
        "id": item.id, "created_at": item.created_at, "source_kind": "pdf",
        "source_title": "schedule.pdf", "title": "Practice", "status": "review_ready",
        "created_count": 0, "rejected_count": 0,
        "transitions": [{"type": "review_ready", "at": store.list_activity()[0]["transitions"][0]["at"],
                         "event_id": None}],
    }
    assert datetime.fromisoformat(store.list_activity()[0]["transitions"][0]["at"]).utcoffset().total_seconds() == 0
    assert backend.saved[-1]["activity"] == list(store.list_activity())
    store.get_activity(item.id)["transitions"].clear()
    store.list_activity()[0]["status"] = "tampered"
    assert store.get_activity(item.id)["status"] == "review_ready"
    assert len(store.get_activity(item.id)["transitions"]) == 1
    assert "private source" not in str(store.list_activity())

    async def failed(_event):
        raise RuntimeError("unconfirmed")

    with pytest.raises(RuntimeError, match="unconfirmed"):
        await store.async_approve_event(item.id, item.events[0].id, failed)
    assert [row["type"] for row in store.get_activity(item.id)["transitions"]] == [
        "review_ready", "calendar_write_started", "calendar_write_uncertain",
    ]
    assert backend.saved[-2]["activity"][0]["status"] == "calendar_write_uncertain"
    assert backend.saved[-2]["activity"][0]["transitions"][-1]["event_id"] == item.events[0].id
    assert backend.saved[-1]["activity"] == list(reversed(store.list_activity()))
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_activity(item.id)["status"] == "calendar_write_uncertain"
    assert await restarted.async_resolve_uncertain(item.id, item.events[0].id, "created")
    assert restarted.get(item.id) is None
    assert restarted.get_activity(item.id)["status"] == "calendar_created"
    assert backend.saved[-1]["activity"] == list(reversed(restarted.list_activity()))


async def test_failed_uncertainty_logging_preserves_calendar_exception(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="source", events=[draft()])).pending
    assert item is not None
    async def processor(_event):
        backend.save_error = RuntimeError("activity disk failed")
        raise ConnectionError("calendar response lost")
    with pytest.raises(ConnectionError, match="calendar response lost"):
        await store.async_approve_event(item.id, item.events[0].id, processor)
    assert store.get_event(item.id, item.events[0].id).status == "write_uncertain"
    assert store.get_activity(item.id)["status"] == "calendar_write_uncertain"
    assert store.get_activity(item.id)["transitions"][-1]["type"] == "calendar_write_started"


async def test_activity_is_bounded_and_storage_failure_keeps_previous_state(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    first = (await store.async_add(source_text="first", events=[draft()])).pending
    assert first is not None
    backend.save_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        await store.async_reject_event(first.id, first.events[0].id)
    assert store.get_activity(first.id)["status"] == "review_ready"
    backend.save_error = None
    assert await store.async_reject_event(first.id, first.events[0].id)
    assert store.get_activity(first.id)["status"] == "event_rejected"
    assert backend.saved[-1]["activity"] == list(reversed(store.list_activity()))
    assert store.list() == ()
    assert len(store._transition(first, "review_ready")) == 1
    monkeypatch.setattr(storage_module, "ACTIVITY_LIMIT", 1)
    second = (await store.async_add(source_text="second", events=[second_draft()])).pending
    assert second is not None
    assert [row["id"] for row in store.list_activity()] == [second.id, first.id]
    assert await store.async_reject_event(second.id, second.events[0].id)
    assert [row["id"] for row in store.list_activity()] == [second.id]
    assert store.get_activity(first.id) is None
    monkeypatch.setattr(storage_module, "ACTIVITY_TRANSITIONS_PER_IMPORT", 1)
    assert len(store._transition(second, "event_rejected")[0]["transitions"]) == 1
    third = (await store.async_add(source_text="third", events=[
        replace(draft(), title="New A"), replace(second_draft(), title="New B")])).pending
    assert third is not None
    assert len(store.list_activity()) == 2  # One completed plus one active.
    assert await store.async_remove(third.id)
    assert [row["id"] for row in store.list_activity()] == [third.id]
    assert store.get_activity(third.id)["rejected_count"] == 2
    assert backend.saved[-1]["activity"] == [store.get_activity(third.id)]


async def test_parse_failure_history_is_private_bounded_and_atomic(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    received = datetime.now(storage_module.UTC)
    backend.save_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        await store.async_begin_submission(source_kind="pdf", source_title="Schedule", received_at=received)
    assert store.list_activity() == ()
    backend.save_error = None
    failed_id = await store.async_begin_submission(source_kind="pdf", source_title="Schedule", received_at=received)
    processing = store.get_activity(failed_id)
    assert processing["status"] == "processing"
    assert [step["type"] for step in processing["transitions"]] == ["received", "processing"]
    assert processing["created_at"] == received.isoformat()
    assert datetime.fromisoformat(processing["transitions"][1]["at"]).utcoffset().total_seconds() == 0
    backend.save_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        await store.async_record_parse_failure(failed_id)
    assert store.get_activity(failed_id) == processing
    assert store._finish_submission("missing", "failed") == store._activity
    backend.save_error = None
    await store.async_record_parse_failure(failed_id)
    record = store.get_activity(failed_id)
    assert set(record) == {"id", "created_at", "source_kind", "source_title", "title",
                           "created_count", "rejected_count", "status", "guidance", "transitions"}
    assert record["created_at"] == record["transitions"][0]["at"]
    assert datetime.fromisoformat(record["created_at"]).utcoffset().total_seconds() == 0
    assert record["source_kind"] == "pdf" and record["source_title"] == "Schedule"
    assert record["created_count"] == record["rejected_count"] == 0
    assert record["status"] == "failed" and record["title"] == "Schedule"
    assert record["guidance"] == "Parsing failed. Check the configured AI Task and submit the source again."
    assert [step["type"] for step in record["transitions"]] == ["received", "processing", "failed"]
    assert datetime.fromisoformat(record["transitions"][-1]["at"]).utcoffset().total_seconds() == 0
    assert all(set(step) == {"type", "at", "event_id"} and step["event_id"] is None
               for step in record["transitions"])
    assert "source_text" not in str(record)
    assert backend.saved[-1]["activity"] == [record]
    monkeypatch.setattr(storage_module, "ACTIVITY_LIMIT", 0)
    processing_id = await store.async_begin_submission(source_kind="pdf", source_title=None,
                                                       received_at=datetime.now(storage_module.UTC))
    assert store.get_activity(failed_id) is None
    assert store.get_activity(processing_id)["status"] == "processing"
    await store.async_record_parse_failure(processing_id)
    assert store.get_activity(processing_id) is None
    monkeypatch.setattr(storage_module, "ACTIVITY_LIMIT", 1)
    next_id = await store.async_begin_submission(source_kind="manual_text", source_title=None,
                                                 received_at=datetime.now(storage_module.UTC))
    await store.async_record_parse_failure(next_id)
    assert store.get_activity(next_id)["title"] == "Submission"
    active_received = datetime.now(storage_module.UTC)
    active_id = await store.async_begin_submission(source_kind="manual_text", source_title=None,
                                                   received_at=active_received)
    active = (await store.async_add(source_text="source", events=[draft()], activity_id=active_id)).pending
    assert active.id == active_id
    assert active.created_at == active_received.isoformat()
    assert active.created_at == store.get_activity(active.id)["created_at"]
    assert [step["type"] for step in store.get_activity(active.id)["transitions"]] == [
        "received", "processing", "review_ready",
    ]
    assert datetime.fromisoformat(store.get_activity(active.id)["transitions"][-1]["at"]).utcoffset().total_seconds() == 0
    assert {row["id"] for row in store.list_activity()} == {next_id, active.id}


async def test_interrupted_processing_is_failed_on_restart_and_empty_results_finish(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    received = datetime.now(storage_module.UTC)
    interrupted = await store.async_begin_submission(source_kind="image", source_title="flyer", received_at=received)
    assert backend.saved[-1]["activity"][-1]["status"] == "processing"
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_activity(interrupted)["status"] == "failed"
    assert "interrupted" in restarted.get_activity(interrupted)["guidance"]
    assert backend.saved[-1]["activity"][-1]["status"] == "failed"
    empty = await restarted.async_begin_submission(source_kind="manual_text", source_title=None, received_at=received)
    result = await restarted.async_add(source_text="no events", events=[], activity_id=empty)
    assert result.pending is None
    assert restarted.get_activity(empty)["status"] == "failed"
    assert "No reviewable events" in restarted.get_activity(empty)["guidance"]
    assert restarted.get_activity(empty)["guidance"] == "No reviewable events were found. Check the source and submit it again."
    assert backend.saved[-1]["activity"][-1] == restarted.get_activity(empty)
    existing = (await restarted.async_add(source_text="first", events=[draft()])).pending
    duplicate = await restarted.async_begin_submission(source_kind="manual_text", source_title=None, received_at=received)
    result = await restarted.async_add(source_text="same", events=[draft()], activity_id=duplicate)
    assert result.duplicate_events == 1
    assert restarted.get_activity(duplicate)["status"] == "duplicate"
    assert "guidance" not in restarted.get_activity(duplicate)
    from_source = await restarted.async_begin_submission(source_kind="manual_text", source_title=None, received_at=received)
    result = await restarted.async_add(source_text="same source", events=[second_draft()], source_id="seen", activity_id=from_source)
    assert result.pending is not None
    raced = await restarted.async_begin_submission(source_kind="manual_text", source_title=None, received_at=received)
    result = await restarted.async_add(source_text="same source", events=[second_draft()], source_id="seen", activity_id=raced)
    assert result.duplicate_source
    assert restarted.get_activity(raced)["status"] == "duplicate"
    assert backend.saved[-1]["activity"][-1] == restarted.get_activity(raced)
    assert restarted.get_activity(existing.id)["status"] == "review_ready"


async def test_history_limit_never_prunes_another_live_parser(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    monkeypatch.setattr(storage_module, "ACTIVITY_LIMIT", 0)
    received = datetime.now(storage_module.UTC)
    first = await store.async_begin_submission(source_kind="manual_text", source_title=None, received_at=received)
    second = await store.async_begin_submission(source_kind="manual_text", source_title=None, received_at=received)
    assert {item["id"] for item in store.list_activity()} == {first, second}
    pending = (await store.async_add(source_text="first", events=[draft()], activity_id=first)).pending
    assert pending.id == first
    assert store.get_activity(second)["status"] == "processing"
    await store.async_record_parse_failure(second)
    assert store.get_activity(second) is None
    assert store.get_activity(first)["status"] == "review_ready"


async def test_editing_activity_title_is_atomic_and_only_follows_leading_event(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="school", events=[draft(), second_draft()])).pending
    assert item is not None
    first, second = item.events
    await store.async_edit_event(item.id, second.id, replace(second_draft(), title="Other photo"))
    assert store.get_activity(item.id)["title"] == first.draft.title
    backend.save_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        await store.async_edit_event(item.id, first.id, replace(draft(), title="Changed"))
    assert store.get_activity(item.id)["title"] == first.draft.title
    backend.save_error = None
    await store.async_edit_event(item.id, first.id, replace(draft(), title="Changed"))
    assert store.get_activity(item.id)["title"] == "Changed"
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_activity(item.id)["title"] == "Changed"


async def test_active_activity_survives_completed_history_pruning(monkeypatch):
    monkeypatch.setattr(storage_module, "ACTIVITY_LIMIT", 1)
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    active = (await store.async_add(source_text="active", events=[draft(), second_draft()])).pending
    assert active is not None
    first, second = active.events
    async def created(_event):
        pass
    await store.async_approve_event(active.id, first.id, created)
    assert store.get_activity(active.id)["title"] == second.draft.title
    assert store.get_activity(active.id)["created_count"] == 1
    # Keep an active record even when all slots are occupied by active imports.
    another = (await store.async_add(source_text="other", events=[
        replace(draft(), title="Different")])).pending
    assert another is not None
    assert {row["id"] for row in store.list_activity()} == {active.id, another.id}
    await store.async_reject_event(another.id, another.events[0].id)
    assert store.get_activity(active.id) is not None
    await store.async_reject_event(active.id, second.id)
    assert store.get_activity(active.id)["status"] == "mixed"
    assert (store.get_activity(active.id)["created_count"],
            store.get_activity(active.id)["rejected_count"]) == (1, 1)
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_activity(active.id)["status"] == "mixed"


async def test_legacy_activity_counters_and_uncertain_sibling_status(monkeypatch):
    item = PendingImport.create(source_text="school", events=[draft(), second_draft()])
    raw = item.as_dict()
    raw["events"][0]["status"] = "write_uncertain"
    old_activity = {"id": item.id, "created_at": item.created_at,
                    "source_kind": "manual_text", "source_title": None,
                    "title": "Practice", "status": "review_ready", "transitions": []}
    backend = FakeStoreBackend(load_result={"items": [raw], "activity": [old_activity]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    assert await store.async_reject_event(item.id, item.events[1].id)
    record = store.get_activity(item.id)
    assert record["status"] == "calendar_write_uncertain"
    assert record["created_count"] == 0
    assert record["rejected_count"] == 1
    assert record["transitions"][-1]["event_id"] == item.events[1].id
    assert backend.saved[-1]["activity"][-1] == record
    assert await store.async_resolve_uncertain(item.id, item.events[0].id, "created")
    assert store.get_activity(item.id)["status"] == "mixed"
    assert store.get_activity(item.id)["created_count"] == 1
    first_only = PendingImport.create(source_text="single", events=[draft()])
    first_raw = first_only.as_dict()
    first_raw["events"][0]["status"] = "write_uncertain"
    legacy = {**old_activity, "id": first_only.id, "created_at": first_only.created_at}
    restored = make_store(monkeypatch, FakeStoreBackend(load_result={
        "items": [first_raw], "activity": [legacy],
    }))
    await restored.async_load()
    assert await restored.async_resolve_uncertain(first_only.id, first_only.events[0].id, "created")
    assert (restored.get_activity(first_only.id)["created_count"],
            restored.get_activity(first_only.id)["rejected_count"]) == (1, 0)


async def test_mixed_event_activity_retains_review_status_and_atomic_checkpoints(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="school", events=[draft(), second_draft()])).pending
    assert item is not None
    first, second = item.events

    async def created(_event):
        assert backend.saved[-1]["activity"][-1]["transitions"][-1]["type"] == "calendar_write_started"
        assert backend.saved[-1]["activity"][-1]["status"] == "calendar_write_uncertain"

    assert await store.async_approve_event(item.id, first.id, created) == first
    record = store.get_activity(item.id)
    assert record["status"] == "review_ready"
    assert record["transitions"][-1] == {
        "type": "calendar_created", "at": record["transitions"][-1]["at"], "event_id": first.id,
    }
    assert backend.saved[-1]["activity"][-1] == record
    assert await store.async_reject_event(item.id, second.id)
    assert store.get_activity(item.id)["status"] == "mixed"
    assert (store.get_activity(item.id)["created_count"],
            store.get_activity(item.id)["rejected_count"]) == (1, 1)
    assert store.get_activity(item.id)["transitions"][-1]["event_id"] == second.id
    assert backend.saved[-1]["activity"][-1] == store.get_activity(item.id)

    other = (await store.async_add(source_text="new", events=[draft(), second_draft()])).pending
    # Previously handled fingerprints prevent the same drafts from being requeued.
    assert other is None

    reverse = make_store(monkeypatch, FakeStoreBackend())
    await reverse.async_load()
    item = (await reverse.async_add(source_text="reverse", events=[draft(), second_draft()])).pending
    assert item is not None
    assert await reverse.async_reject_event(item.id, item.events[0].id)
    assert reverse.get_activity(item.id)["status"] == "review_ready"
    async def noop(_event):
        pass
    assert await reverse.async_approve_event(item.id, item.events[1].id, noop) == item.events[1]
    assert reverse.get_activity(item.id)["status"] == "mixed"
    assert (reverse.get_activity(item.id)["created_count"],
            reverse.get_activity(item.id)["rejected_count"]) == (1, 1)


async def test_duplicate_count_is_persisted_with_review_metadata(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    await store.async_add(source_text="First", events=[draft()])
    result = await store.async_add(
        source_text="Second", events=[draft(), second_draft()], source_kind="image",
        source_title="schedule.png", warnings=["One item needs checking"],
    )
    assert result.pending is not None
    assert result.duplicate_events == 1
    assert result.pending.duplicate_events == 1
    assert result.pending.as_service_dict()["source_title"] == "schedule.png"


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
    assert store.is_source_duplicate("new-source") is False


async def test_store_restores_items_and_deduplication_history(monkeypatch):
    active_source = source_fingerprint("active-source")
    seen_source = source_fingerprint("seen-source")
    seen_event = event_fingerprint(draft())
    pending = PendingImport.create(
        source_text="text",
        events=[second_draft()],
        source_fingerprint=active_source,
    )
    backend = FakeStoreBackend(
        load_result={
            "items": [pending.as_dict()],
            "seen_source_fingerprints": [seen_source],
            "seen_event_fingerprints": [seen_event],
        }
    )
    store = make_store(monkeypatch, backend)

    await store.async_load()

    assert store.list() == (pending,)
    assert store.get(pending.id) == pending
    assert store.is_source_duplicate("seen-source") is True
    assert store.is_source_duplicate("active-source") is True
    assert store.is_source_duplicate("new-source") is False

    duplicate = await store.async_add(
        source_text="duplicate event",
        events=[draft()],
    )
    assert duplicate.pending is None
    assert duplicate.duplicate_source is False
    assert duplicate.duplicate_events == 1
    assert backend.saved == []


async def test_get_event_uses_stable_id_across_restart_and_checkpoints(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(source_text="newsletter", events=[draft(), second_draft()])).pending
    assert pending is not None
    first, second = pending.events
    assert store.get_event(pending.id, second.id) == second
    assert store.get_event(pending.id, "missing") is None
    assert store.get_event("missing", first.id) is None

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_event(pending.id, second.id) == second

    async def processor(event):
        if event.draft == second_draft():
            raise RuntimeError("calendar unavailable")

    with pytest.raises(RuntimeError, match="calendar unavailable"):
        await restarted.async_process_events(pending.id, processor)
    assert restarted.get_event(pending.id, first.id) is None
    uncertain = restarted.get_event(pending.id, second.id)
    assert uncertain is not None
    assert uncertain.status == "write_uncertain"


async def test_edit_event_updates_matching_and_survives_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    first = (await store.async_add(source_text="one", events=[draft()])).pending
    assert first is not None
    original = first.events[0]
    replacement = second_draft()
    edited = await store.async_edit_event(first.id, original.id, replacement)
    assert edited is not None
    assert edited.id == original.id
    assert edited.draft == replacement
    assert store.get_event(first.id, original.id) == edited
    assert await store.async_edit_event(first.id, original.id, replacement) == edited
    with pytest.raises(PendingEventEditError) as stale:
        await store.async_edit_event(first.id, original.id, draft(), expected_event=original)
    assert str(stale.value) == "Event changed since it was loaded; refresh before editing"
    assert await store.async_edit_event(
        first.id, original.id, replacement, expected_event=edited
    ) == edited
    assert len(backend.saved) == 2  # unchanged draft does not rewrite storage

    # The old fingerprint is free; the new one is actively reserved.
    old = await store.async_add(source_text="old", events=[draft()])
    assert old.pending is not None
    duplicate = await store.async_add(source_text="duplicate", events=[replacement])
    assert duplicate.pending is None
    assert duplicate.duplicate_events == 1
    backend.load_result = backend.saved[-1]
    reloaded = make_store(monkeypatch, backend)
    await reloaded.async_load()
    assert reloaded.get_event(first.id, original.id) == edited


async def test_edit_event_rejects_missing_uncertain_or_duplicate(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    first = (await store.async_add(source_text="first", events=[draft()])).pending
    second = (await store.async_add(source_text="second", events=[second_draft()])).pending
    assert first is not None and second is not None
    assert await store.async_edit_event("missing", "event", draft()) is None
    assert await store.async_edit_event(first.id, "missing", draft()) is None
    with pytest.raises(PendingEventEditError, match="duplicates"):
        await store.async_edit_event(first.id, first.events[0].id, second_draft())
    assert store.get_event(first.id, first.events[0].id) == first.events[0]

    assert await store.async_remove(second.id)
    with pytest.raises(PendingEventEditError, match="duplicates"):
        await store.async_edit_event(first.id, first.events[0].id, second_draft())

    backend.load_result = {"items": [{
        **first.as_dict(),
        "events": [{**first.events[0].as_dict(), "status": "write_uncertain"}],
    }]}
    uncertain_store = make_store(monkeypatch, backend)
    await uncertain_store.async_load()
    with pytest.raises(PendingEventEditError, match="Uncertain"):
        await uncertain_store.async_edit_event(first.id, first.events[0].id, second_draft())


async def test_edit_save_failure_keeps_original_draft(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    first = (await store.async_add(source_text="one", events=[draft()])).pending
    assert first is not None
    backend.save_error = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await store.async_edit_event(first.id, first.events[0].id, second_draft())
    assert store.get_event(first.id, first.events[0].id) == first.events[0]


async def test_reject_one_event_preserves_siblings_and_dedupes_rejection(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(
        source_text="newsletter", events=[draft(), second_draft()], source_id="mail-1"
    )).pending
    assert item is not None
    first, second = item.events

    assert await store.async_reject_event(item.id, "missing") is False
    assert await store.async_reject_event("missing", first.id) is False
    assert await store.async_reject_event(item.id, first.id) is True
    assert store.get_event(item.id, first.id) is None
    assert store.get_event(item.id, second.id) == second
    assert store.is_source_duplicate("mail-1")  # still actively pending
    assert backend.saved[-1]["seen_event_fingerprints"] == [event_fingerprint(draft())]
    assert "seen_source_fingerprints" not in backend.saved[-1]

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_event(item.id, second.id) == second
    repeated = await restarted.async_add(source_text="other", events=[draft()])
    assert repeated.pending is None and repeated.duplicate_events == 1

    assert await restarted.async_reject_event(item.id, second.id) is True
    assert restarted.get(item.id) is None
    assert backend.saved[-1]["seen_source_fingerprints"] == [source_fingerprint("mail-1")]
    assert backend.saved[-1]["seen_event_fingerprints"] == [
        event_fingerprint(draft()), event_fingerprint(second_draft()),
    ]
    assert await restarted.async_reject_event(item.id, second.id) is False


async def test_decisions_reject_stale_event_without_side_effects(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="notice", events=[draft()])).pending
    assert item is not None
    original = item.events[0]
    await store.async_edit_event(item.id, original.id, second_draft())
    saved_count = len(backend.saved)

    async def processor(_event):
        pytest.fail("stale approval must not write to the calendar")

    with pytest.raises(PendingEventEditError) as approval_error:
        await store.async_approve_event(
            item.id, original.id, processor, expected_event=original,
        )
    assert str(approval_error.value) == "Event changed since it was loaded; refresh before deciding"
    with pytest.raises(PendingEventEditError) as rejection_error:
        await store.async_reject_event(
            item.id, original.id, expected_event=original,
        )
    assert str(rejection_error.value) == "Event changed since it was loaded; refresh before deciding"
    assert len(backend.saved) == saved_count
    edited = store.get_event(item.id, original.id)
    assert edited is not None
    assert await store.async_reject_event(
        item.id, original.id, expected_event=edited,
    )
    next_item = (await store.async_add(
        source_text="other", events=[replace(second_draft(), title="Another event")],
    )).pending
    assert next_item is not None
    next_event = next_item.events[0]
    processed = []

    async def write(event):
        processed.append(event)

    assert await store.async_approve_event(
        next_item.id, next_event.id, write, expected_event=next_event,
    ) == next_event
    assert processed == [next_event]


async def test_reject_event_save_failure_and_uncertain_guard(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="mail", events=[draft()])).pending
    assert item is not None
    backend.save_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        await store.async_reject_event(item.id, item.events[0].id)
    assert store.get(item.id) == item

    backend.load_result = {"items": [{
        **item.as_dict(),
        "events": [{**item.events[0].as_dict(), "status": "write_uncertain"}],
    }]}
    uncertain = make_store(monkeypatch, backend)
    await uncertain.async_load()
    with pytest.raises(PendingImportApprovalUncertainError):
        await uncertain.async_reject_event(item.id, item.events[0].id)


async def test_reject_last_event_without_source_fingerprint(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="manual", events=[draft()])).pending
    assert item is not None
    assert await store.async_reject_event(item.id, item.events[0].id)
    assert {key: value for key, value in backend.saved[-1].items() if key != "activity"} == {
        "items": [], "seen_event_fingerprints": [event_fingerprint(draft())],
    }


async def test_approve_selected_event_preserves_other_draft_and_source(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(
        source_text="newsletter", events=[draft(), second_draft()], source_id="mail-2"
    )).pending
    assert pending is not None
    first, second = pending.events
    processed = []

    async def processor(event):
        processed.append(event)

    assert await store.async_approve_event("missing", second.id, processor) is None
    assert await store.async_approve_event(pending.id, "missing", processor) is None
    assert await store.async_approve_event(pending.id, second.id, processor) == second
    assert processed == [second]
    assert store.get_event(pending.id, first.id) == first
    assert store.get_event(pending.id, second.id) is None
    assert backend.saved[-2]["items"][0]["events"][1]["status"] == "write_uncertain"
    assert backend.saved[-2]["items"][0]["events"][0]["status"] == "pending"
    assert backend.saved[-1]["seen_event_fingerprints"] == [event_fingerprint(second_draft())]
    assert "seen_source_fingerprints" not in backend.saved[-1]

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_event(pending.id, first.id) == first
    assert await restarted.async_approve_event(pending.id, first.id, processor) == first
    assert restarted.get(pending.id) is None
    assert processed == [second, first]
    assert backend.saved[-1]["seen_source_fingerprints"] == [source_fingerprint("mail-2")]


async def test_selected_approval_failure_blocks_retry_without_sibling_loss(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(source_text="two", events=[draft(), second_draft()])).pending
    assert pending is not None
    first, second = pending.events

    async def failed_processor(_event):
        raise RuntimeError("calendar timed out")

    with pytest.raises(RuntimeError, match="calendar timed out"):
        await store.async_approve_event(pending.id, second.id, failed_processor)
    uncertain = store.get_event(pending.id, second.id)
    assert uncertain is not None and uncertain.status == "write_uncertain"
    assert store.get_event(pending.id, first.id) == first
    assert store.get(pending.id).approval_in_flight is True
    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_approve_event(pending.id, second.id, failed_processor)
    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_process_events(pending.id, failed_processor)


async def test_selected_approval_checkpoint_failures_and_no_source_id(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(source_text="one", events=[draft()])).pending
    assert pending is not None
    event = pending.events[0]
    calls = []

    async def processor(draft):
        calls.append(draft)

    backend.save_error = RuntimeError("save failed")
    backend.fail_on_save_attempt = backend.save_attempts + 1
    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_approve_event(pending.id, event.id, processor)
    assert calls == []
    assert store.get(pending.id) == pending

    backend.fail_on_save_attempt = backend.save_attempts + 2
    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_approve_event(pending.id, event.id, processor)
    assert calls == [event]
    assert store.get_event(pending.id, event.id).status == "write_uncertain"
    assert "seen_source_fingerprints" not in backend.saved[-1]


async def test_selected_approval_serializes_against_rejection(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(source_text="one", events=[draft()])).pending
    assert pending is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def processor(_draft):
        started.set()
        await release.wait()

    approval = asyncio.create_task(
        store.async_approve_event(pending.id, pending.events[0].id, processor)
    )
    await started.wait()
    rejection = asyncio.create_task(store.async_reject_event(pending.id, pending.events[0].id))
    await asyncio.sleep(0)
    assert not rejection.done()
    release.set()
    assert await approval == pending.events[0]
    assert await rejection is False


async def test_resolve_uncertain_as_created_preserves_siblings(monkeypatch):
    item = PendingImport.create(
        source_text="school", events=[draft(), second_draft()],
        source_fingerprint=source_fingerprint("school-mail"),
    )
    first, second = item.events
    raw = item.as_dict()
    raw["events"][0]["status"] = "write_uncertain"
    backend = FakeStoreBackend(load_result={"items": [raw]})
    store = make_store(monkeypatch, backend)
    await store.async_load()

    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_remove(item.id)
    assert await store.async_resolve_uncertain(item.id, first.id, "created")
    assert store.get_event(item.id, first.id) is None
    assert store.get_event(item.id, second.id) == second
    assert store.get(item.id).approval_in_flight is False
    assert backend.saved[-1]["seen_event_fingerprints"] == [event_fingerprint(draft())]
    assert "seen_source_fingerprints" not in backend.saved[-1]

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_event(item.id, second.id) == second

    async def uncertain_write(_draft):
        raise RuntimeError("timeout")

    with pytest.raises(RuntimeError, match="timeout"):
        await restarted.async_approve_event(item.id, second.id, uncertain_write)
    assert await restarted.async_resolve_uncertain(item.id, second.id, "created")
    assert restarted.get(item.id) is None
    assert backend.saved[-1]["seen_source_fingerprints"] == [source_fingerprint("school-mail")]


async def test_resolve_not_created_allows_retry_with_same_id(monkeypatch):
    item = PendingImport.create(source_text="manual", events=[draft()])
    raw = item.as_dict()
    raw["events"][0]["status"] = "write_uncertain"
    backend = FakeStoreBackend(load_result={"items": [raw]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    event = item.events[0]

    assert await store.async_resolve_uncertain(item.id, event.id, "not_created")
    assert store.get_event(item.id, event.id) == event
    assert "seen_event_fingerprints" not in backend.saved[-1]
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_event(item.id, event.id) == event

    processed = []

    async def processor(draft):
        processed.append(draft)

    assert await restarted.async_approve_event(item.id, event.id, processor) == event
    assert processed == [event]
    assert restarted.get(item.id) is None


async def test_resolve_discard_is_handled_and_reject_all_requires_resolution(monkeypatch):
    item = PendingImport.create(source_text="manual", events=[draft()])
    raw = item.as_dict()
    raw["events"][0]["status"] = "write_uncertain"
    backend = FakeStoreBackend(load_result={"items": [raw]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    event = item.events[0]

    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_remove(item.id)
    assert await store.async_resolve_uncertain(item.id, event.id, "discard")
    assert store.get(item.id) is None
    assert {key: value for key, value in backend.saved[-1].items() if key != "activity"} == {
        "items": [], "seen_event_fingerprints": [event_fingerprint(draft())],
    }
    duplicate = await store.async_add(source_text="same", events=[draft()])
    assert duplicate.pending is None and duplicate.duplicate_events == 1


async def test_resolve_rejects_invalid_state_missing_ids_and_failed_save(monkeypatch):
    item = PendingImport.create(source_text="manual", events=[draft()])
    raw = item.as_dict()
    raw["events"][0]["status"] = "write_uncertain"
    backend = FakeStoreBackend(load_result={"items": [raw]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    event = item.events[0]

    with pytest.raises(ValueError, match="invalid uncertain-write resolution"):
        await store.async_resolve_uncertain(item.id, event.id, "unknown")
    assert await store.async_resolve_uncertain("missing", event.id, "created") is False
    assert await store.async_resolve_uncertain(item.id, "missing", "created") is False

    backend.save_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        await store.async_resolve_uncertain(item.id, event.id, "not_created")
    assert store.get_event(item.id, event.id).status == "write_uncertain"

    ready = make_store(monkeypatch, FakeStoreBackend(load_result={"items": [item.as_dict()]}))
    await ready.async_load()
    with pytest.raises(PendingEventResolutionError, match="no uncertain"):
        await ready.async_resolve_uncertain(item.id, event.id, "created")


async def test_store_adds_rejects_and_remembers_source_and_event(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    result = await store.async_add(
        source_text=" text ",
        events=[draft()],
        source_id="message-1",
    )
    pending = result.pending
    assert pending is not None
    assert result.duplicate_source is False
    assert result.duplicate_events == 0
    assert pending.source_fingerprint == source_fingerprint("message-1")
    assert store.get(pending.id) == pending
    assert store.is_source_duplicate("message-1") is True
    assert [{key: value for key, value in row.items() if key != "activity"} for row in backend.saved] == [{"items": [pending.as_dict()]}]

    save_count = len(backend.saved)
    assert await store.async_remove("missing") is False
    assert len(backend.saved) == save_count

    assert await store.async_remove(pending.id) is True
    assert store.get(pending.id) is None
    assert store.list() == ()
    assert {key: value for key, value in backend.saved[-1].items() if key != "activity"} == {
        "items": [],
        "seen_source_fingerprints": [source_fingerprint("message-1")],
        "seen_event_fingerprints": [event_fingerprint(draft())],
    }
    assert store.is_source_duplicate("message-1") is True

    source_duplicate = await store.async_add(
        source_text="different event from same source",
        events=[second_draft()],
        source_id="message-1",
    )
    assert source_duplicate.pending is None
    assert source_duplicate.duplicate_source is True
    assert source_duplicate.duplicate_events == 0


async def test_store_filters_active_and_within_submission_event_duplicates(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    first = await store.async_add(source_text="first", events=[draft()])
    assert first.pending is not None

    duplicate = await store.async_add(source_text="same", events=[draft()])
    assert duplicate.pending is None
    assert duplicate.duplicate_events == 1

    partial = await store.async_add(
        source_text="partial",
        events=[draft(), second_draft(), second_draft()],
    )
    assert partial.pending is not None
    assert tuple(event.draft for event in partial.pending.events) == (second_draft(),)
    assert partial.duplicate_source is False
    assert partial.duplicate_events == 2
    assert len(store.list()) == 2


async def test_store_allows_retry_when_no_events_were_extracted(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    empty = await store.async_add(
        source_text="no events",
        events=[],
        source_id="empty-source",
    )
    assert empty.pending is None
    assert empty.duplicate_source is False
    assert empty.duplicate_events == 0
    assert backend.saved == []
    assert store.is_source_duplicate("empty-source") is False

    save_count = len(backend.saved)
    no_source = await store.async_add(source_text="no events", events=[])
    assert no_source.pending is None
    assert no_source.duplicate_events == 0
    assert len(backend.saved) == save_count
    retry = await store.async_add(source_text="corrected", events=[draft()], source_id="empty-source")
    assert retry.pending is not None


async def test_store_all_duplicate_events_marks_new_source_seen(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    first = await store.async_add(source_text="first", events=[draft()])
    assert first.pending is not None

    result = await store.async_add(
        source_text="same event, new source",
        events=[draft()],
        source_id="message-2",
    )

    assert result.pending is None
    assert result.duplicate_source is False
    assert result.duplicate_events == 1
    assert store.is_source_duplicate("message-2") is True
    assert backend.saved[-1]["seen_source_fingerprints"] == [
        source_fingerprint("message-2")
    ]


async def test_store_async_add_rejects_blank_source_text(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    with pytest.raises(ValueError, match=r"^source_text must be a non-empty string$"):
        await store.async_add(source_text="   ", events=[draft()])


async def test_store_keeps_memory_unchanged_when_save_fails(monkeypatch):
    existing = PendingImport.create(source_text="existing", events=[draft()])
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    backend.save_error = RuntimeError("save failed")

    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_add(source_text="new", events=[second_draft()])
    assert store.list() == (existing,)

    with pytest.raises(RuntimeError, match="save failed"):
        await store.async_remove(existing.id)
    assert store.list() == (existing,)


async def test_store_can_remove_backing_storage(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)

    await store.async_remove_storage()

    assert backend.removed is True


async def test_store_processes_events_and_remembers_deduplication(monkeypatch):
    source_fp = source_fingerprint("message-approve")
    existing = PendingImport.create(
        source_text="existing",
        events=[draft(), second_draft()],
        source_fingerprint=source_fp,
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
        events=(PendingEvent(existing.events[0].id, draft(), "write_uncertain"), existing.events[1]),
        source_fingerprint=source_fp,
    )
    remaining = PendingImport(
        id=existing.id,
        created_at=existing.created_at,
        source_text=existing.source_text,
        events=(existing.events[1],),
        source_fingerprint=source_fp,
    )
    second_in_flight = PendingImport(
        id=existing.id,
        created_at=existing.created_at,
        source_text=existing.source_text,
        events=(PendingEvent(existing.events[1].id, second_draft(), "write_uncertain"),),
        source_fingerprint=source_fp,
    )
    first_fp = event_fingerprint(draft())
    second_fp = event_fingerprint(second_draft())

    assert result == existing
    assert processed == list(existing.events)
    assert store.list() == ()
    first_attempt = backend.saved[0]["items"][0]["events"][0]["write_attempt"]
    second_attempt = backend.saved[2]["items"][0]["events"][0]["write_attempt"]
    UUID(first_attempt)
    UUID(second_attempt)
    assert first_attempt != second_attempt
    first_in_flight = replace(first_in_flight, events=(
        replace(first_in_flight.events[0], write_attempt=first_attempt),
        *first_in_flight.events[1:],
    ))
    second_in_flight = replace(second_in_flight, events=(
        replace(second_in_flight.events[0], write_attempt=second_attempt),
    ))
    assert [{key: value for key, value in row.items() if key != "activity"} for row in backend.saved] == [
        {"items": [first_in_flight.as_dict()]},
        {
            "items": [remaining.as_dict()],
            "seen_event_fingerprints": [first_fp],
        },
        {
            "items": [second_in_flight.as_dict()],
            "seen_event_fingerprints": [first_fp],
        },
        {
            "items": [],
            "seen_source_fingerprints": [source_fp],
            "seen_event_fingerprints": [first_fp, second_fp],
        },
    ]

    assert store.is_source_duplicate("message-approve") is True
    duplicate = await store.async_add(
        source_text="same approved event",
        events=[draft()],
    )
    assert duplicate.pending is None
    assert duplicate.duplicate_events == 1

    assert await store.async_process_events("missing", processor) is None


async def test_store_partial_failure_marks_remaining_approval_uncertain(monkeypatch):
    second = second_draft()
    existing = PendingImport.create(
        source_text="existing",
        events=[draft(), second],
    )
    backend = FakeStoreBackend(load_result={"items": [existing.as_dict()]})
    store = make_store(monkeypatch, backend)
    await store.async_load()

    async def processor(event):
        if event.draft == second:
            raise RuntimeError("processor failed")

    with pytest.raises(RuntimeError, match="processor failed"):
        await store.async_process_events(existing.id, processor)

    remaining = store.get(existing.id)
    assert remaining is not None
    assert tuple(event.draft for event in remaining.events) == (second,)
    assert remaining.approval_in_flight is True
    assert backend.saved[-1]["seen_event_fingerprints"] == [
        event_fingerprint(draft())
    ]

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
    assert processed == [existing.events[0]]

    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_process_events(existing.id, processor)
    assert processed == [existing.events[0]]


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
    raw = {"items": [{"id": "old", "created_at": "2026-01-01", "source_text": "legacy", "events": [draft().as_dict()]}]}
    migrated = _migrate_v1(raw)
    restored = PendingImport.from_dict(migrated["items"][0])
    assert restored.approval_in_flight is False
    assert restored.source_fingerprint is None
    UUID(restored.events[0].id)


async def test_v1_store_migration_preserves_uncertainty_and_history(hass):
    legacy = {
        "items": [{
            "id": "legacy-import", "created_at": "2026-09-26T03:00:00+00:00",
            "source_text": "newsletter", "events": [draft().as_dict(), second_draft().as_dict()],
            "approval_in_flight": True, "source_fingerprint": source_fingerprint("mail-1"),
        }],
        "seen_source_fingerprints": [source_fingerprint("mail-0")],
        "seen_event_fingerprints": [event_fingerprint(second_draft())],
    }
    await Store(hass, 1, STORAGE_KEY, private=True).async_save(legacy)
    store = PendingImportStore(hass)
    await store.async_load()

    pending = store.get("legacy-import")
    assert pending is not None
    assert pending.approval_in_flight
    assert [event.status for event in pending.events] == ["write_uncertain", "pending"]
    assert len({event.id for event in pending.events}) == 2
    assert store.is_source_duplicate("mail-0")
    assert store.is_source_duplicate("mail-1")
    migrated = await _PendingStore(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_load()
    assert migrated == {**legacy, "items": [pending.as_dict()]}
    # A second load reads v2 and retains the migrated IDs and blocked retry.
    again = PendingImportStore(hass)
    await again.async_load()
    assert again.get("legacy-import") == pending

    async def processor(_event):
        pytest.fail("uncertain event must not be retried")

    with pytest.raises(PendingImportApprovalUncertainError):
        await again.async_process_events("legacy-import", processor)


async def test_v1_migration_failure_does_not_replace_storage(hass):
    broken = {"items": [{"id": "broken", "events": [{"title": "invalid"}]}]}
    await Store(hass, 1, STORAGE_KEY, private=True).async_save(broken)
    with pytest.raises(Exception):
        await PendingImportStore(hass).async_load()
    assert await Store(hass, 1, STORAGE_KEY, private=True).async_load() == broken


def test_pending_event_rejects_unknown_status():
    with pytest.raises(ValueError, match=r"^invalid pending event status$"):
        PendingEvent.from_dict({"id": "x", "draft": draft().as_dict(), "status": "unknown"})


async def test_migration_rejects_unsupported_major_version():
    with pytest.raises(ValueError, match="Unsupported pending storage version"):
        await _PendingStore._async_migrate_func(None, 0, 1, {"items": []})


def test_remember_fingerprints_deduplicates_refreshes_and_bounds(monkeypatch):
    existing = ("a", "b")

    assert _remember_fingerprints(existing, ()) == existing

    monkeypatch.setattr(storage_module, "DEDUP_HISTORY_LIMIT", 2)
    assert _remember_fingerprints(existing, ("b", "c", "c")) == ("b", "c")

async def test_edit_event_preserves_import_metadata_and_siblings(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (
        await store.async_add(
            source_text="newsletter",
            events=[draft(), second_draft()],
            source_id="edit-source",
        )
    ).pending
    assert item is not None
    first, second = item.events
    replacement = EventDraft(
        title="Dentist",
        start="2026-10-11T09:00:00-07:00",
        end="2026-10-11T10:00:00-07:00",
        all_day=False,
        location="Clinic",
        confidence=0.8,
    )

    edited = await store.async_edit_event(item.id, first.id, replacement)
    assert edited is not None
    updated = store.get(item.id)
    assert updated is not None
    assert updated.id == item.id
    assert updated.created_at == item.created_at
    assert updated.source_text == item.source_text
    assert updated.source_fingerprint == item.source_fingerprint
    assert updated.events == (edited, second)

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get(item.id) == updated


async def test_edit_event_rejects_duplicate_sibling(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (
        await store.async_add(
            source_text="two events",
            events=[draft(), second_draft()],
        )
    ).pending
    assert item is not None

    with pytest.raises(PendingEventEditError, match="duplicates"):
        await store.async_edit_event(
            item.id,
            item.events[0].id,
            item.events[1].draft,
        )


async def test_reject_event_preserves_metadata_and_immediate_dedupe(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (
        await store.async_add(
            source_text="newsletter",
            events=[draft(), second_draft()],
            source_id="reject-source",
        )
    ).pending
    assert item is not None
    first, second = item.events

    assert await store.async_reject_event(item.id, first.id)
    updated = store.get(item.id)
    assert updated is not None
    assert updated.id == item.id
    assert updated.created_at == item.created_at
    assert updated.source_text == item.source_text
    assert updated.source_fingerprint == item.source_fingerprint
    assert updated.events == (second,)

    repeated = await store.async_add(
        source_text="repeat rejected event",
        events=[draft()],
    )
    assert repeated.pending is None
    assert repeated.duplicate_events == 1


async def test_resolve_not_created_preserves_other_uncertain_event(monkeypatch):
    source_fp = source_fingerprint("uncertain-source")
    item = PendingImport.create(
        source_text="newsletter",
        events=[draft(), second_draft()],
        source_fingerprint=source_fp,
    )
    raw = item.as_dict()
    raw["events"] = [
        {**event, "status": "write_uncertain"}
        for event in raw["events"]
    ]
    backend = FakeStoreBackend(load_result={"items": [raw]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    loaded = store.get(item.id)
    assert loaded is not None
    first, second = loaded.events

    assert await store.async_resolve_uncertain(
        item.id, first.id, "not_created"
    )
    updated = store.get(item.id)
    assert updated is not None
    assert updated.created_at == item.created_at
    assert updated.source_text == item.source_text
    assert updated.source_fingerprint == source_fp
    assert updated.events[0].id == first.id
    assert updated.events[0].status == "pending"
    assert updated.events[1].id == second.id
    assert updated.events[1].status == "write_uncertain"


async def test_resolve_last_event_keeps_source_dedupe_live_in_memory(monkeypatch):
    source_fp = source_fingerprint("resolved-source")
    item = PendingImport.create(
        source_text="newsletter",
        events=[draft()],
        source_fingerprint=source_fp,
    )
    raw = item.as_dict()
    raw["events"] = [
        {**raw["events"][0], "status": "write_uncertain"}
    ]
    backend = FakeStoreBackend(load_result={"items": [raw]})
    store = make_store(monkeypatch, backend)
    await store.async_load()

    assert await store.async_resolve_uncertain(
        item.id, item.events[0].id, "created"
    )
    assert store.get(item.id) is None
    assert store.is_source_duplicate("resolved-source") is True


async def test_save_without_history_override_preserves_seen_sources(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    first = (
        await store.async_add(
            source_text="first",
            events=[draft()],
            source_id="seen-source",
        )
    ).pending
    assert first is not None
    assert await store.async_remove(first.id)

    second = await store.async_add(
        source_text="second",
        events=[second_draft()],
    )
    assert second.pending is not None
    assert backend.saved[-1]["seen_source_fingerprints"] == [
        source_fingerprint("seen-source")
    ]


async def test_calendar_destination_survives_edit_checkpoint_and_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(
        source_text="one", events=[draft()], calendar_entity="calendar.family"
    )).pending
    assert pending is not None
    event = pending.events[0]
    assert event.calendar_entity == "calendar.family"
    changed = await store.async_edit_event(
        pending.id, event.id, draft(), calendar_entity="calendar.work"
    )
    assert changed is not None
    assert changed.calendar_entity == "calendar.work"
    assert await store.async_edit_event(pending.id, event.id, draft()) == changed
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_event(pending.id, event.id) == changed


    async def failed_write(selected):
        assert selected.calendar_entity == "calendar.work"
        raise RuntimeError("calendar write failed")

    with pytest.raises(RuntimeError, match="calendar write failed"):
        await restarted.async_approve_event(pending.id, event.id, failed_write)
    backend.load_result = backend.saved[-1]
    await restarted.async_load()
    uncertain = restarted.get_event(pending.id, event.id)
    assert uncertain is not None
    assert uncertain.calendar_entity == "calendar.work"
    assert uncertain.status == "write_uncertain"
    assert await restarted.async_resolve_uncertain(pending.id, event.id, "not_created")
    assert restarted.get_event(pending.id, event.id) == changed


def test_old_pending_event_without_destination_defaults_to_unselected():
    event = PendingEvent.create(draft())
    raw = event.as_dict()
    raw.pop("calendar_entity")
    assert PendingEvent.from_dict(raw) == event

async def test_source_submission_claim_blocks_duplicate_before_parser(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    received_at = datetime.fromisoformat("2026-10-01T12:00:00+00:00")

    activity_id = await store.async_begin_source_submission(
        source_id="<mail-1@example.test>", source_kind="email",
        source_title="School notice", received_at=received_at,
    )

    assert activity_id is not None
    assert store.is_source_duplicate("<mail-1@example.test>") is True
    assert await store.async_begin_source_submission(
        source_id="<mail-1@example.test>", source_kind="email",
        source_title="School notice", received_at=received_at,
    ) is None
    assert backend.saved[-1]["source_claims"] == {
        activity_id: source_fingerprint("<mail-1@example.test>")
    }
    assert "<mail-1@example.test>" not in str(backend.saved[-1])


async def test_source_submission_claim_is_atomic_under_concurrency(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    received_at = datetime.fromisoformat("2026-10-01T12:00:00+00:00")

    results = await asyncio.gather(*(
        store.async_begin_source_submission(
            source_id="<same@example.test>", source_kind="email",
            source_title=None, received_at=received_at,
        )
        for _ in range(2)
    ))

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert len(backend.saved) == 1


async def test_source_claim_save_failure_leaves_source_retryable(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    backend.save_error = RuntimeError("storage unavailable")

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await store.async_begin_source_submission(
            source_id="<mail-1@example.test>", source_kind="email",
            source_title=None,
            received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
        )

    assert store.is_source_duplicate("<mail-1@example.test>") is False
    assert backend.saved == []


async def test_parse_failure_releases_source_claim_for_retry(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    received_at = datetime.fromisoformat("2026-10-01T12:00:00+00:00")
    activity_id = await store.async_begin_source_submission(
        source_id="<mail-1@example.test>", source_kind="email",
        source_title=None, received_at=received_at,
    )
    assert activity_id is not None

    await store.async_record_parse_failure(activity_id)

    assert store.is_source_duplicate("<mail-1@example.test>") is False
    assert "source_claims" not in backend.saved[-1]
    retry_id = await store.async_begin_source_submission(
        source_id="<mail-1@example.test>", source_kind="email",
        source_title=None, received_at=received_at,
    )
    assert retry_id is not None and retry_id != activity_id


async def test_claim_handoff_to_pending_is_one_durable_transaction(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<mail-1@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id, source_kind="email", source_title="Party",
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None

    result = await store.async_add(
        source_text="Friday at 5", events=[draft()], source_id=source_id,
        source_kind="email", source_title="Party", activity_id=activity_id,
    )

    assert result.pending is not None
    assert result.pending.id == activity_id
    assert result.pending.source_fingerprint == source_fingerprint(source_id)
    assert "source_claims" not in backend.saved[-1]
    assert store.is_source_duplicate(source_id) is True

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.is_source_duplicate(source_id) is True
    assert restarted.get(activity_id) is not None


async def test_claim_cannot_be_finalized_with_different_source(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    activity_id = await store.async_begin_source_submission(
        source_id="<mail-1@example.test>", source_kind="email", source_title=None,
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None
    saved_count = len(backend.saved)

    with pytest.raises(ValueError, match="does not match claimed source"):
        await store.async_add(
            source_text="Friday at 5", events=[draft()],
            source_id="<different@example.test>", activity_id=activity_id,
        )

    assert len(backend.saved) == saved_count
    assert store.is_source_duplicate("<mail-1@example.test>") is True
    assert store.is_source_duplicate("<different@example.test>") is False


async def test_restart_releases_interrupted_claim_for_retry(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<mail-1@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id, source_kind="email", source_title=None,
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None
    backend.load_result = backend.saved[-1]

    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()

    assert restarted.is_source_duplicate(source_id) is False
    assert restarted.get_activity(activity_id)["status"] == "failed"
    assert "source_claims" not in backend.saved[-1]


async def test_zero_event_claim_is_durably_handled(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<no-events@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id, source_kind="email", source_title="FYI",
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None

    result = await store.async_add(
        source_text="No calendar item here", events=[], source_id=source_id,
        source_kind="email", source_title="FYI", activity_id=activity_id,
    )

    assert result.pending is None
    assert result.duplicate_source is False
    assert result.duplicate_events == 0
    assert store.is_source_duplicate(source_id) is True
    assert store.is_source_durable(source_id) is True
    assert store.get_activity(activity_id)["status"] == "no_events"
    assert "guidance" not in store.get_activity(activity_id)
    assert backend.saved[-1]["seen_source_fingerprints"] == [
        source_fingerprint(source_id)
    ]
    assert "source_claims" not in backend.saved[-1]



async def test_failed_claim_release_is_retried_before_next_source_claim(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<retry-release@example.test>"
    received_at = datetime.fromisoformat("2026-10-01T12:00:00+00:00")
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=received_at,
    )
    assert activity_id is not None

    backend.save_error = RuntimeError("storage unavailable")
    backend.fail_on_save_attempt = backend.save_attempts + 1
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await store.async_record_parse_failure(activity_id)

    assert store.is_source_duplicate(source_id) is True

    backend.save_error = None
    retry_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=received_at,
    )

    assert retry_id is not None
    assert retry_id != activity_id
    assert store.get_activity(activity_id)["status"] == "failed"
    assert store.is_source_duplicate(source_id) is True
    assert backend.saved[-1]["source_claims"] == {
        retry_id: source_fingerprint(source_id)
    }


async def test_failed_claim_release_retry_aborts_if_storage_is_still_unavailable(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<retry-release@example.test>"
    received_at = datetime.fromisoformat("2026-10-01T12:00:00+00:00")
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=received_at,
    )
    assert activity_id is not None

    backend.save_error = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await store.async_record_parse_failure(activity_id)

    saved_count = len(backend.saved)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await store.async_begin_source_submission(
            source_id=source_id,
            source_kind="email",
            source_title=None,
            received_at=received_at,
        )

    assert len(backend.saved) == saved_count
    assert store.is_source_duplicate(source_id) is True


class BlockingSaveBackend(FakeStoreBackend):
    """Persist once, then allow cancellation before the save call returns."""

    def __init__(self, load_result=None):
        super().__init__(load_result)
        self.block_on_attempt = None
        self.saved_before_block = asyncio.Event()
        self.release_block = asyncio.Event()

    async def async_save(self, data):
        self.save_attempts += 1
        if self.save_error is not None and (
            self.fail_on_save_attempt is None
            or self.save_attempts == self.fail_on_save_attempt
        ):
            raise self.save_error
        self.saved.append(data)
        if self.save_attempts == self.block_on_attempt:
            self.saved_before_block.set()
            await self.release_block.wait()


async def test_claim_acquisition_cancellation_reconciles_durable_claim(monkeypatch):
    backend = BlockingSaveBackend()
    backend.block_on_attempt = 1
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<claim-cancel@example.test>"

    task = asyncio.create_task(store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title="Cancelled claim",
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    ))
    await backend.saved_before_block.wait()
    task.cancel()
    backend.release_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.is_source_duplicate(source_id) is False
    assert "source_claims" not in backend.saved[-1]
    activity = store.list_activity()[0]
    assert activity["status"] == "failed"


async def test_claim_handoff_cancellation_keeps_durable_pending_import(monkeypatch):
    backend = BlockingSaveBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<handoff-cancel@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title="Handoff",
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None
    backend.block_on_attempt = backend.save_attempts + 1
    from unittest.mock import Mock
    ready = Mock()
    store.on_review_ready = ready

    task = asyncio.create_task(store.async_add(
        source_text="Friday at 5",
        events=[draft()],
        source_id=source_id,
        source_kind="email",
        source_title="Handoff",
        activity_id=activity_id,
    ))
    await backend.saved_before_block.wait()
    task.cancel()
    backend.release_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    pending = store.get(activity_id)
    assert pending is not None
    assert pending.source_fingerprint == source_fingerprint(source_id)
    assert store.get_activity(activity_id)["status"] == "review_ready"
    assert "source_claims" not in backend.saved[-1]
    ready.assert_called_once_with(pending)

    await store.async_record_parse_failure(activity_id)
    assert store.get(activity_id) == pending
    assert store.get_activity(activity_id)["status"] == "review_ready"


async def test_claim_acquisition_cancellation_preserves_cancel_if_cleanup_save_fails(monkeypatch):
    backend = BlockingSaveBackend()
    backend.block_on_attempt = 1
    backend.save_error = RuntimeError("storage unavailable")
    backend.fail_on_save_attempt = 2
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<claim-cleanup-fails@example.test>"

    task = asyncio.create_task(store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title="Cancelled claim",
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    ))
    await backend.saved_before_block.wait()
    task.cancel()
    backend.release_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.is_source_duplicate(source_id) is True


async def test_parse_failure_cancellation_finishes_release_then_raises(monkeypatch):
    backend = BlockingSaveBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<release-cancel@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None
    backend.block_on_attempt = backend.save_attempts + 1

    task = asyncio.create_task(store.async_record_parse_failure(activity_id))
    await backend.saved_before_block.wait()
    task.cancel()
    backend.release_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.is_source_duplicate(source_id) is False
    assert store.get_activity(activity_id)["status"] == "failed"
    assert "source_claims" not in backend.saved[-1]


async def test_pending_release_retry_cancellation_finishes_release_then_raises(monkeypatch):
    backend = BlockingSaveBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<retry-cancel@example.test>"
    received_at = datetime.fromisoformat("2026-10-01T12:00:00+00:00")
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=received_at,
    )
    assert activity_id is not None

    backend.save_error = RuntimeError("storage unavailable")
    backend.fail_on_save_attempt = backend.save_attempts + 1
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await store.async_record_parse_failure(activity_id)

    backend.save_error = None
    backend.fail_on_save_attempt = None
    backend.block_on_attempt = backend.save_attempts + 1
    task = asyncio.create_task(store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=received_at,
    ))
    await backend.saved_before_block.wait()
    task.cancel()
    backend.release_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.is_source_duplicate(source_id) is False
    assert store.get_activity(activity_id)["status"] == "failed"


class BeforePersistBlockingBackend(FakeStoreBackend):
    """Block a save before recording it as durable."""

    def __init__(self, load_result=None):
        super().__init__(load_result)
        self.block_on_attempt = None
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()

    async def async_save(self, data):
        self.save_attempts += 1
        if self.save_attempts == self.block_on_attempt:
            self.save_started.set()
            await self.release_save.wait()
        if self.save_error is not None and (
            self.fail_on_save_attempt is None
            or self.save_attempts == self.fail_on_save_attempt
        ):
            raise self.save_error
        self.saved.append(data)


async def test_claim_cleanup_cancellation_while_waiting_for_lock_still_releases(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<lock-cancel@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None

    await store._lock.acquire()
    task = asyncio.create_task(store.async_record_parse_failure(activity_id))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    store._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.is_source_duplicate(source_id) is False
    assert store.get_activity(activity_id)["status"] == "failed"
    assert "source_claims" not in backend.saved[-1]


async def test_pending_handoff_cancellation_before_persistence_finishes_transaction(monkeypatch):
    backend = BeforePersistBlockingBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<before-persist@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title="Handoff",
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None
    backend.block_on_attempt = backend.save_attempts + 1

    task = asyncio.create_task(store.async_add(
        source_text="Friday at 5",
        events=[draft()],
        source_id=source_id,
        source_kind="email",
        source_title="Handoff",
        activity_id=activity_id,
    ))
    await backend.save_started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert len(backend.saved) == 1
    backend.release_save.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    pending = store.get(activity_id)
    assert pending is not None
    assert store.get_activity(activity_id)["status"] == "review_ready"
    assert "source_claims" not in backend.saved[-1]


async def test_transaction_helper_propagates_inner_cancellation(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    async def cancelled_operation():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await store._async_complete_transaction(cancelled_operation())


async def test_transaction_helper_preserves_cancellation_over_later_error(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    started = asyncio.Event()
    release = asyncio.Event()

    async def operation():
        started.set()
        await release.wait()
        raise RuntimeError("transaction failed")

    task = asyncio.create_task(store._async_complete_transaction(operation()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelled_duplicate_claim_returns_cancellation_without_cleanup(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<already-claimed@example.test>"
    received_at = datetime.fromisoformat("2026-10-01T12:00:00+00:00")
    first = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=received_at,
    )
    assert first is not None

    await store._lock.acquire()
    task = asyncio.create_task(store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title=None,
        received_at=received_at,
    ))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    store._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.is_source_duplicate(source_id) is True
    assert store.get_activity(first)["status"] == "processing"



async def test_source_durable_excludes_live_claim_and_includes_pending(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<durable@example.test>"
    activity_id = await store.async_begin_source_submission(
        source_id=source_id,
        source_kind="email",
        source_title="Durable",
        received_at=datetime.fromisoformat("2026-10-01T12:00:00+00:00"),
    )
    assert activity_id is not None

    assert store.is_source_duplicate(source_id) is True
    assert store.is_source_durable(source_id) is False

    result = await store.async_add(
        source_text="Friday at 5",
        events=[draft()],
        source_id=source_id,
        source_kind="email",
        source_title="Durable",
        activity_id=activity_id,
    )

    assert result.pending is not None
    assert store.is_source_durable(source_id) is True



async def test_source_durable_includes_handled_history(monkeypatch) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<handled@example.test>"
    store._seen_source_fingerprints = (source_fingerprint(source_id),)

    assert store.is_source_durable(source_id) is True



async def test_source_discovery_is_private_durable_and_promotes_same_activity(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    received = datetime.fromisoformat("2026-10-03T12:00:00+00:00")

    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=received,
    )

    discovered = store.get_activity(activity_id)
    assert discovered is not None
    assert discovered["status"] == "discovered"
    assert discovered["source_kind"] == "email"
    assert discovered["source_title"] == "Email"
    assert [step["type"] for step in discovered["transitions"]] == [
        "received",
        "discovered",
    ]
    assert discovered["created_at"] == received.isoformat()
    assert "source_text" not in str(discovered)
    assert "source_claims" not in backend.saved[-1]

    claimed = await store.async_claim_source_discovery(
        activity_id,
        source_id="<lifecycle@example.test>",
        source_kind="email",
        source_title="School concert",
    )

    assert claimed is True
    processing = store.get_activity(activity_id)
    assert processing is not None
    assert processing["status"] == "processing"
    assert processing["source_kind"] == "email"
    assert processing["source_title"] == "School concert"
    assert processing["title"] == "School concert"
    assert [step["type"] for step in processing["transitions"]] == [
        "received",
        "discovered",
        "processing",
    ]
    processing_step = processing["transitions"][-1]
    assert set(processing_step) == {"type", "at", "event_id"}
    assert processing_step["event_id"] is None
    assert (
        datetime.fromisoformat(processing_step["at"]).utcoffset().total_seconds()
        == 0
    )
    assert backend.saved[-1]["source_claims"] == {
        activity_id: source_fingerprint("<lifecycle@example.test>")
    }


async def test_source_discovery_duplicate_finishes_same_activity(monkeypatch) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    existing = await store.async_add(
        source_text="existing",
        events=[draft()],
        source_id="<duplicate@example.test>",
    )
    assert existing.pending is not None

    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    )
    claimed = await store.async_claim_source_discovery(
        activity_id,
        source_id="<duplicate@example.test>",
        source_kind="email",
        source_title=None,
    )

    assert claimed is False
    duplicate = store.get_activity(activity_id)
    assert duplicate is not None
    assert duplicate["status"] == "duplicate"
    assert duplicate["source_title"] is None
    assert duplicate["title"] == "Email"
    assert [step["type"] for step in duplicate["transitions"]] == [
        "received",
        "discovered",
        "duplicate",
    ]
    duplicate_step = duplicate["transitions"][-1]
    assert set(duplicate_step) == {"type", "at", "event_id"}
    assert duplicate_step["event_id"] is None
    assert (
        datetime.fromisoformat(duplicate_step["at"]).utcoffset().total_seconds()
        == 0
    )
    assert existing.pending is not None
    assert store.get_activity(existing.pending.id)["status"] == "review_ready"
    assert [item["id"] for item in store.list_activity()].count(activity_id) == 1

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    persisted = restarted.get_activity(activity_id)
    assert persisted is not None
    assert persisted["status"] == "duplicate"
    assert [step["type"] for step in persisted["transitions"]] == [
        "received",
        "discovered",
        "duplicate",
    ]


async def test_discovery_failure_is_visible_and_does_not_create_source_claim(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    )

    await store.async_record_source_failure(
        activity_id,
        "Email could not be normalized and was left untouched.",
    )

    failed = store.get_activity(activity_id)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["guidance"] == (
        "Email could not be normalized and was left untouched."
    )
    assert [step["type"] for step in failed["transitions"]] == [
        "received",
        "discovered",
        "failed",
    ]
    assert "source_claims" not in backend.saved[-1]

    save_count = len(backend.saved)
    await store.async_record_source_failure(activity_id, "ignored")
    assert len(backend.saved) == save_count
    assert store.get_activity(activity_id) == failed


async def test_claim_source_discovery_rejects_unknown_or_non_discovered_activity(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    with pytest.raises(
        ValueError,
        match="^activity must reference a discovered source$",
    ):
        await store.async_claim_source_discovery(
            "missing",
            source_id="<missing@example.test>",
            source_kind="email",
            source_title="Missing",
        )

    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    )
    assert await store.async_claim_source_discovery(
        activity_id,
        source_id="<claimed@example.test>",
        source_kind="email",
        source_title="Claimed",
    )
    with pytest.raises(
        ValueError,
        match="^activity must reference a discovered source$",
    ):
        await store.async_claim_source_discovery(
            activity_id,
            source_id="<claimed-again@example.test>",
            source_kind="email",
            source_title="Claimed again",
        )


async def test_discovery_save_failure_is_atomic(monkeypatch) -> None:
    backend = FakeStoreBackend()
    backend.save_error = RuntimeError("disk full")
    store = make_store(monkeypatch, backend)
    await store.async_load()

    with pytest.raises(RuntimeError, match="disk full"):
        await store.async_begin_source_discovery(
            source_kind="email",
            source_title="Email",
            received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
        )

    assert store.list_activity() == ()


async def test_restart_marks_interrupted_discovery_failed(monkeypatch) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    )
    backend.load_result = backend.saved[-1]

    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()

    failed = restarted.get_activity(activity_id)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["transitions"][-1]["type"] == "failed"
    assert failed["guidance"] == (
        "Processing was interrupted. Check the source and submit it again."
    )
    assert backend.saved[-1]["activity"][-1] == failed


async def test_discovery_cancellation_finishes_failure_checkpoint(monkeypatch) -> None:
    backend = BlockingSaveBackend()
    backend.block_on_attempt = 1
    store = make_store(monkeypatch, backend)
    await store.async_load()

    task = asyncio.create_task(store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    ))
    await backend.saved_before_block.wait()
    task.cancel()
    backend.release_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    activity = store.list_activity()[0]
    assert activity["status"] == "failed"
    assert activity["guidance"] == (
        "Source discovery was interrupted. Check the source and try again."
    )
    assert [step["type"] for step in activity["transitions"]] == [
        "received",
        "discovered",
        "failed",
    ]


async def test_claim_discovery_cancellation_releases_claim(monkeypatch) -> None:
    backend = BlockingSaveBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    )
    backend.block_on_attempt = backend.save_attempts + 1

    task = asyncio.create_task(store.async_claim_source_discovery(
        activity_id,
        source_id="<cancelled-lifecycle@example.test>",
        source_kind="email",
        source_title="Cancelled",
    ))
    await backend.saved_before_block.wait()
    task.cancel()
    backend.release_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.is_source_duplicate("<cancelled-lifecycle@example.test>") is False
    failed = store.get_activity(activity_id)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["guidance"] == (
        "Source processing was interrupted. Check the source and try again."
    )
    assert [step["type"] for step in failed["transitions"]] == [
        "received",
        "discovered",
        "processing",
        "failed",
    ]
    assert "source_claims" not in backend.saved[-1]



async def test_discovery_cancellation_preserves_cancel_if_failure_cleanup_fails(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    async def cancelled_transaction(operation):
        operation.close()
        return "activity-cancelled", True

    async def failed_cleanup(_activity_id, _guidance):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(store, "_async_complete_transaction", cancelled_transaction)
    monkeypatch.setattr(store, "async_record_source_failure", failed_cleanup)

    with pytest.raises(asyncio.CancelledError):
        await store.async_begin_source_discovery(
            source_kind="email",
            source_title="Email",
            received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
        )


async def test_claim_discovery_cancellation_preserves_cancel_if_cleanup_fails(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    async def cancelled_transaction(operation):
        operation.close()
        return True, True

    async def failed_cleanup(_activity_id, _guidance):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(store, "_async_complete_transaction", cancelled_transaction)
    monkeypatch.setattr(store, "async_record_source_failure", failed_cleanup)

    with pytest.raises(asyncio.CancelledError):
        await store.async_claim_source_discovery(
            "activity-cancelled",
            source_id="<cancelled@example.test>",
            source_kind="email",
            source_title="Cancelled",
        )



async def test_discovered_activity_is_protected_from_history_eviction(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    monkeypatch.setattr(storage_module, "ACTIVITY_LIMIT", 0)
    received = datetime.fromisoformat("2026-10-03T12:00:00+00:00")

    first = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="First",
        received_at=received,
    )
    second = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Second",
        received_at=received,
    )

    assert {item["id"] for item in store.list_activity()} == {first, second}
    assert all(item["status"] == "discovered" for item in store.list_activity())


async def test_processing_transition_survives_restart_before_recovery(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()

    sibling = (
        await store.async_add(
            source_text="existing review",
            events=[draft()],
            source_kind="manual_text",
        )
    ).pending
    assert sibling is not None
    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    )
    assert await store.async_claim_source_discovery(
        activity_id,
        source_id="<restart-processing@example.test>",
        source_kind="email",
        source_title="Restart processing",
    )
    assert store.get_activity(sibling.id)["status"] == "review_ready"
    assert [item["id"] for item in store.list_activity()].count(activity_id) == 1

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()

    recovered = restarted.get_activity(activity_id)
    assert recovered is not None
    assert recovered["status"] == "failed"
    assert recovered["source_kind"] == "email"
    assert recovered["source_title"] == "Restart processing"
    assert [step["type"] for step in recovered["transitions"]] == [
        "received",
        "discovered",
        "processing",
        "failed",
    ]
    assert restarted.get_activity(sibling.id)["status"] == "review_ready"


async def test_load_clears_orphan_source_claim_and_remains_usable(monkeypatch) -> None:
    orphan_source = "<orphan@example.test>"
    backend = FakeStoreBackend(load_result={
        "items": [],
        "source_claims": {
            "orphan-activity": source_fingerprint(orphan_source),
        },
    })
    store = make_store(monkeypatch, backend)

    await store.async_load()

    assert backend.saved[-1] == {"items": []}
    assert store.is_source_duplicate(orphan_source) is False

    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Email",
        received_at=datetime.fromisoformat("2026-10-03T12:00:00+00:00"),
    )
    assert await store.async_claim_source_discovery(
        activity_id,
        source_id="<usable-after-load@example.test>",
        source_kind="email",
        source_title="Usable",
    )


async def test_terminal_source_failure_is_durable_and_survives_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Bad attachment",
        received_at=datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
    )
    assert await store.async_claim_source_discovery(
        activity_id,
        source_id="<terminal@example.test>",
        source_kind="email",
        source_title="Bad attachment",
    )

    await store.async_record_terminal_source_failure(
        activity_id,
        source_id="<terminal@example.test>",
        guidance="Correct the attachment and resend it as a new message.",
    )

    record = store.get_activity(activity_id)
    assert record is not None
    assert record["status"] == "failed"
    assert record["guidance"] == (
        "Correct the attachment and resend it as a new message."
    )
    assert record["transitions"][-1]["type"] == "failed"
    assert store.is_source_durable("<terminal@example.test>") is True
    assert store.is_source_duplicate("<terminal@example.test>") is True
    assert backend.saved[-1].get("source_claims", {}) == {}
    assert backend.saved[-1]["seen_source_fingerprints"] == [
        source_fingerprint("<terminal@example.test>")
    ]

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.is_source_durable("<terminal@example.test>") is True
    assert restarted.get_activity(activity_id)["status"] == "failed"

    # Re-finalizing an already terminal activity is a no-op.
    saved_count = len(backend.saved)
    await restarted.async_record_terminal_source_failure(
        activity_id,
        source_id="<terminal@example.test>",
        guidance="Different guidance",
    )
    assert len(backend.saved) == saved_count


async def test_failed_terminal_persistence_releases_claim_before_email_retry(
    monkeypatch,
) -> None:
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    source_id = "<terminal-save-retry@example.test>"
    received_at = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Terminal",
        received_at=received_at,
    )
    assert await store.async_claim_source_discovery(
        activity_id,
        source_id=source_id,
        source_kind="email",
        source_title="Terminal",
    )

    backend.save_error = RuntimeError("storage unavailable")
    backend.fail_on_save_attempt = backend.save_attempts + 1
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await store.async_record_terminal_source_failure(
            activity_id,
            source_id=source_id,
            guidance="Correct and resend.",
        )

    assert store.is_source_durable(source_id) is False
    assert store.is_source_duplicate(source_id) is True
    assert store.get_activity(activity_id)["status"] == "processing"

    backend.save_error = None
    backend.fail_on_save_attempt = None
    retry_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Retry",
        received_at=received_at,
    )
    assert await store.async_claim_source_discovery(
        retry_id,
        source_id=source_id,
        source_kind="email",
        source_title="Retry",
    )

    assert retry_id != activity_id
    assert store.get_activity(activity_id)["status"] == "failed"
    assert store.get_activity(retry_id)["status"] == "processing"
    assert store.is_source_durable(source_id) is False
    assert backend.saved[-1]["source_claims"] == {
        retry_id: source_fingerprint(source_id)
    }


async def test_terminal_source_failure_rejects_mismatched_claim(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    activity_id = await store.async_begin_source_discovery(
        source_kind="email",
        source_title="Message",
        received_at=datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
    )
    assert await store.async_claim_source_discovery(
        activity_id,
        source_id="<claimed@example.test>",
        source_kind="email",
        source_title="Message",
    )

    with pytest.raises(ValueError, match="source_id does not match claimed source"):
        await store.async_record_terminal_source_failure(
            activity_id,
            source_id="<other@example.test>",
            guidance="Terminal",
        )

    assert store.is_source_durable("<claimed@example.test>") is False
    assert store.get_activity(activity_id)["status"] == "processing"


async def test_terminal_source_failure_propagates_deferred_cancellation(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    await store.async_load()

    async def cancelled_transaction(operation):
        operation.close()
        return None, True

    monkeypatch.setattr(
        store,
        "_async_complete_transaction",
        cancelled_transaction,
    )

    with pytest.raises(asyncio.CancelledError):
        await store.async_record_terminal_source_failure(
            "activity",
            source_id="<terminal@example.test>",
            guidance="Terminal",
        )


async def test_routing_confirmation_is_durable_and_blocks_single_and_bulk_writes(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(
        source_text="Calendar: unknown\nPractice", events=[draft(), second_draft()],
        calendar_entity="calendar.family", routing_unresolved=True,
    )).pending
    assert item is not None
    first, second = item.events
    writes = []

    async def write(event):
        writes.append(event.id)

    with pytest.raises(PendingEventEditError, match="Confirm the destination"):
        await store.async_approve_event(item.id, first.id, write)
    with pytest.raises(PendingEventEditError, match="Confirm all event destinations"):
        await store.async_process_events(item.id, write)
    assert writes == []
    assert await store.async_edit_event(item.id, first.id, draft()) == first
    confirmed = await store.async_edit_event(
        item.id, first.id, draft(), calendar_entity="calendar.family",
        expected_event=first,
    )
    assert confirmed is not None and not confirmed.routing_unresolved
    assert store.get_event(item.id, second.id).routing_unresolved
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert not restored.get_event(item.id, first.id).routing_unresolved
    assert restored.get_event(item.id, second.id).routing_unresolved
    await restored.async_approve_event(item.id, first.id, write)
    assert writes == [first.id]
    with pytest.raises(PendingEventEditError, match="Confirm the destination"):
        await restored.async_approve_event(item.id, second.id, write)


async def test_bulk_route_preflight_refuses_mixed_events_before_first_side_effect(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(
        source_text="Calendar: unresolved\nTwo events",
        events=[draft(), second_draft()],
        calendar_entity="calendar.family", routing_unresolved=True,
    )).pending
    first, second = item.events
    await store.async_edit_event(item.id, first.id, first.draft,
                                 calendar_entity="calendar.family")
    writes = []

    async def write(event):
        writes.append(event.id)

    with pytest.raises(PendingEventEditError, match="Confirm all event destinations"):
        await store.async_process_events(item.id, write)
    assert writes == []
    assert store.get_event(item.id, second.id).routing_unresolved
    assert store.get_event(item.id, first.id).status == "pending"


@pytest.mark.parametrize("warning", [
    "Calendar routing hint is not configured or allowed. "
    "Check the destination calendar during review.",
    "Conflicting calendar routing hints. "
    "Check the destination calendar during review.",
])
def test_legacy_unresolved_route_is_migrated_without_reflagging_confirmation(warning):
    original = PendingImport.create(
        source_text="Practice",
        events=[draft(), second_draft()],
        calendar_entity="calendar.family",
        warnings=[warning],
    )
    old_payload = original.as_dict()
    for event in old_payload["events"]:
        del event["routing_unresolved"]  # Old release has no per-event field.
    loaded = PendingImport.from_dict(old_payload)
    assert all(event.routing_unresolved for event in loaded.events)
    # An intentional destination save clears the warning only on that event,
    # even while the parent import's historical routing warning is retained.
    old_payload["events"][0]["routing_unresolved"] = False
    restarted = PendingImport.from_dict(old_payload)
    assert restarted.events[0].routing_unresolved is False
    assert restarted.events[1].routing_unresolved is True
    assert restarted.as_dict()["events"][0]["routing_unresolved"] is False
    assert restarted.as_dict()["events"][1]["routing_unresolved"] is True
    assert PendingImport.from_dict(restarted.as_dict()) == restarted


def test_legacy_regular_pending_import_keeps_approved_routing_compatibility():
    original = PendingImport.create(
        source_text="Practice", events=[draft()], calendar_entity="calendar.family",
        warnings=["Review extracted times"],
    )
    raw = original.as_dict()
    del raw["events"][0]["routing_unresolved"]
    assert PendingImport.from_dict(raw).events[0].routing_unresolved is False

async def test_migrated_uncertain_route_stays_blocked_after_not_created(monkeypatch):
    """Resolving a legacy uncertain write never silently accepts its fallback."""
    warning = (
        "Calendar routing hint is not configured or allowed. "
        "Check the destination calendar during review."
    )
    original = PendingImport.create(
        source_text="Practice", events=[draft()],
        calendar_entity="calendar.family", warnings=[warning],
    )
    legacy = original.as_dict()
    event_id = original.events[0].id
    legacy["events"][0]["status"] = "write_uncertain"
    legacy["events"][0]["write_attempt"] = "attempt-before-upgrade"
    del legacy["events"][0]["routing_unresolved"]
    backend = FakeStoreBackend(load_result={"items": [legacy]})
    store = make_store(monkeypatch, backend)
    await store.async_load()
    migrated = store.get_event(original.id, event_id)
    assert migrated is not None
    assert migrated.routing_unresolved is True
    assert migrated.status == "write_uncertain"

    assert await store.async_resolve_uncertain(
        original.id, event_id, "not_created", expected_event=migrated,
    )
    recovered = store.get_event(original.id, event_id)
    assert recovered is not None
    assert recovered.status == "pending"
    assert recovered.write_attempt is None
    assert recovered.routing_unresolved is True
    assert backend.saved[-1]["items"][0]["events"][0]["routing_unresolved"] is True

    writes = []

    async def write(event):
        writes.append(event)

    with pytest.raises(PendingEventEditError, match="Confirm"):
        await store.async_approve_event(original.id, event_id, write)
    assert writes == []

    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_event(original.id, event_id).routing_unresolved is True
    with pytest.raises(PendingEventEditError, match="Confirm"):
        await restarted.async_process_events(original.id, write)
    assert writes == []


def test_pending_storage_initializes_independent_containers_and_unsubscribes(monkeypatch):
    """Fresh durable stores must have usable isolated state and removable listeners."""
    first = make_store(monkeypatch, FakeStoreBackend())
    second = make_store(monkeypatch, FakeStoreBackend())
    for store in (first, second):
        assert store._items == {}
        assert store._activity == ()
        assert store._seen_source_fingerprints == ()
        assert store._seen_event_fingerprints == ()
        assert store._source_claims == {}
        assert store._source_claim_releases == set()
    assert first._items is not second._items
    assert first._source_claims is not second._source_claims
    assert first._source_claim_releases is not second._source_claim_releases

    received = []
    cancel_first = first.async_subscribe(lambda: received.append("kept"))
    removed = lambda: received.append("removed")
    cancel_removed = first.async_subscribe(removed)
    first._commit_items({})
    assert sorted(received) == ["kept", "removed"]
    cancel_removed()
    first._commit_items({})
    assert sorted(received) == ["kept", "kept", "removed"]
    cancel_first()
    first._commit_items({})
    assert sorted(received) == ["kept", "kept", "removed"]


def test_pending_import_constructor_defaults_and_rejections_are_exact():
    with pytest.raises(ValueError) as error:
        PendingImport.create(source_text="   ", events=[draft()])
    assert str(error.value) == "source_text must be a non-empty string"
    with pytest.raises(ValueError) as error:
        PendingImport.create(source_text="Valid text", events=[])
    assert str(error.value) == "pending import must contain at least one event"

    pending = PendingImport.create(source_text="Valid text", events=[draft()])
    assert pending.source_kind == "manual_text"
    assert pending.source_sender is None
    assert pending.events[0].routing_unresolved is False
    assert pending.events[0].as_dict()["routing_unresolved"] is False
    assert pending.warnings == ()
    flagged = PendingEvent.create(draft(), routing_unresolved=True)
    assert flagged.routing_unresolved is True
    assert flagged.as_dict()["routing_unresolved"] is True
    assert flagged.as_service_dict()["routing_unresolved"] is True
    explicit = PendingEvent.create(draft(), "calendar.family")
    assert explicit.calendar_entity == "calendar.family"
    assert explicit.routing_unresolved is False


async def test_store_add_persists_sender_and_source_kind_across_restart(monkeypatch):
    """Source metadata is intentionally durable, not omitted during transaction handoff."""
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    outcome = await store.async_add(
        source_text="Upcoming class",
        events=[draft()],
        source_kind="email",
        source_title="School digest",
        source_sender="Teacher <teacher@example.test>",
        routing_unresolved=True,
    )
    item = outcome.pending
    assert item is not None
    assert item.source_kind == "email"
    assert item.source_title == "School digest"
    assert item.source_sender == "Teacher <teacher@example.test>"
    assert item.events[0].routing_unresolved is True
    assert backend.saved[-1]["items"][0]["source_sender"] == "Teacher <teacher@example.test>"

    backend.load_result = backend.saved[-1]
    reloaded = make_store(monkeypatch, backend)
    await reloaded.async_load()
    restored = reloaded.get(item.id)
    assert restored is not None
    assert restored.source_kind == "email"
    assert restored.source_title == "School digest"
    assert restored.source_sender == "Teacher <teacher@example.test>"
    assert restored.events[0].routing_unresolved is True


def test_pending_assumption_metadata_roundtrip_and_legacy_compatibility():
    item = PendingImport.create(
        source_text="Flyer", events=[draft()],
        event_assumptions=[("Friday resolved using calendar context",)],
    )
    event = item.events[0]
    assert event.as_service_dict()["date_time_assumptions"] == [
        "Friday resolved using calendar context"
    ]
    assert PendingImport.from_dict(item.as_dict()) == item
    old = event.as_dict()
    old.pop("date_time_assumptions")
    assert PendingEvent.from_dict(old).date_time_assumptions == ()
    with pytest.raises(ValueError, match="event assumptions must match"):
        PendingImport.create(source_text="Flyer", events=[draft()], event_assumptions=[])


async def test_duplicate_filter_keeps_only_accepted_event_assumptions(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    await store.async_add(source_text="Previous", events=[draft()])
    outcome = await store.async_add(
        source_text="Second", events=[draft(), second_draft()],
        event_assumptions=[
            ("Old event assumptions should not appear",),
            ("Picture Day derived from relative date",),
        ],
    )
    assert outcome.pending is not None
    assert outcome.duplicate_events == 1
    assert len(outcome.pending.events) == 1
    assert outcome.pending.events[0].draft == second_draft()
    assert outcome.pending.events[0].date_time_assumptions == (
        "Picture Day derived from relative date",
    )
    assert outcome.pending.warnings == ()
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    restored = restarted.get(outcome.pending.id)
    assert restored == outcome.pending
    assert restored.as_service_dict()["events"][0]["date_time_assumptions"] == [
        "Picture Day derived from relative date"
    ]


async def test_temporal_edits_clear_stale_assumptions_but_title_edits_keep_them(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    await store.async_load()
    pending_item = (await store.async_add(
        source_text="Schedule", events=[draft()],
        event_assumptions=[("Clock time normalized",)],
    )).pending
    assert pending_item is not None
    original = pending_item.events[0]
    renamed = await store.async_edit_event(
        pending_item.id, original.id, replace(original.draft, title="Revised"),
        expected_event=original,
    )
    assert renamed.date_time_assumptions == ("Clock time normalized",)
    updated = await store.async_edit_event(
        pending_item.id, original.id,
        replace(renamed.draft, start="2026-10-08T16:30:00-07:00"),
        expected_event=renamed,
    )
    assert updated.date_time_assumptions == ()
    assert "date_time_assumptions" not in updated.as_service_dict()


async def test_wrong_event_assumption_count_is_rejected_without_any_write(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_load()
    with pytest.raises(ValueError, match="event assumptions must match"):
        await store.async_add(
            source_text="Schedule", events=[draft()],
            event_assumptions=[(), ()],
        )
    assert backend.saved == []
