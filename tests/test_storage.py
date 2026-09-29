"""Tests for persistent pending-import storage and deduplication."""

import asyncio
from dataclasses import replace
from datetime import datetime
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
        source_text="Extracted schedule", events=[draft()], source_kind="pdf",
        source_title="schedule.pdf", warnings=["Event 2 had no date"], duplicate_events=2,
    )
    assert PendingImport.from_dict(item.as_dict()) == item
    assert item.source_kind == "pdf"
    assert item.as_service_dict()["warnings"] == ["Event 2 had no date"]
    assert item.as_service_dict()["duplicate_events"] == 2
    assert item.as_dict()["source_title"] == "schedule.pdf"
    legacy = PendingImport.from_dict({
        "id": item.id, "created_at": item.created_at, "source_text": item.source_text,
        "events": [event.as_dict() for event in item.events],
    })
    assert legacy.source_kind == "manual_text"
    assert legacy.source_title is None
    assert legacy.warnings == ()
    assert legacy.duplicate_events == 0
    assert legacy.as_service_dict()["source_title"] is None
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
    assert [row["id"] for row in store.list_activity()] == [second.id]
    assert store.get_activity(first.id) is None
    monkeypatch.setattr(storage_module, "ACTIVITY_TRANSITIONS_PER_IMPORT", 1)
    assert len(store._transition(second, "event_rejected")[0]["transitions"]) == 1


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


async def test_store_marks_source_seen_when_no_new_events(monkeypatch):
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
    assert {key: value for key, value in backend.saved[-1].items() if key != "activity"} == {
        "items": [],
        "seen_source_fingerprints": [source_fingerprint("empty-source")],
    }
    assert store.is_source_duplicate("empty-source") is True

    save_count = len(backend.saved)
    no_source = await store.async_add(source_text="no events", events=[])
    assert no_source.pending is None
    assert no_source.duplicate_events == 0
    assert len(backend.saved) == save_count


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

    with pytest.raises(ValueError, match="source_text must be a non-empty string"):
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
    with pytest.raises(ValueError, match="invalid pending event status"):
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
