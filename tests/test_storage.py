"""Tests for persistent pending-import storage and deduplication."""

import asyncio
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
        **draft().as_dict(), "id": pending.events[0].id, "status": "pending",
    }
    assert pending.as_service_dict()["approval_in_flight"] is False

    restored = PendingImport.from_dict(pending.as_dict())
    assert restored == pending

    without_source = PendingImport.create(source_text="text", events=[draft()])
    assert "source_fingerprint" not in without_source.as_dict()


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
        if event == second_draft():
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
    assert backend.saved[-1] == {
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
    assert processed == [second_draft()]
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
    assert processed == [second_draft(), draft()]
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
    assert calls == [draft()]
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
    assert processed == [draft()]
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
    assert backend.saved[-1] == {
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
    assert backend.saved == [{"items": [pending.as_dict()]}]

    save_count = len(backend.saved)
    assert await store.async_remove("missing") is False
    assert len(backend.saved) == save_count

    assert await store.async_remove(pending.id) is True
    assert store.get(pending.id) is None
    assert store.list() == ()
    assert backend.saved[-1] == {
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
    assert backend.saved[-1] == {
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
    assert processed == [draft(), second_draft()]
    assert store.list() == ()
    assert backend.saved == [
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
        if event == second:
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
    assert processed == [draft()]

    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_process_events(existing.id, processor)
    assert processed == [draft()]


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
