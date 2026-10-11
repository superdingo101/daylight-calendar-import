"""Reprocessing invariants that must survive parser/transaction mutations."""
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.daylight_calendar_import.storage import (
    PendingEvent, PendingEventEditError,
)
from test_storage import FakeStoreBackend, make_store, draft


async def existing(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = (await store.async_add(
        source_text="retained original", events=[draft()], source_id="reprocess-source",
    )).pending
    return store, backend, item


async def test_reprocessing_requires_existing_retained_source(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    parser = AsyncMock()
    with pytest.raises(PendingEventEditError) as failure:
        await store.async_reprocess("missing-source", parser)
    assert str(failure.value) == "Pending import not found"
    parser.assert_not_awaited()


async def test_reprocessing_rejects_nontext_source_with_stable_guidance(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    item = (await store.async_add(
        source_text="not original content", events=[draft()], source_kind="email"
    )).pending
    parser = AsyncMock()
    with pytest.raises(PendingEventEditError) as failure:
        await store.async_reprocess(item.id, parser)
    assert str(failure.value) == (
        "Original source material is unavailable; submit the source again"
    )
    parser.assert_not_awaited()


@pytest.mark.parametrize("activity", [
    {"created_count": 1, "rejected_count": 0},
    {"created_count": 0, "rejected_count": 1},
])
async def test_reprocessing_rejects_handled_activity_without_mutating(monkeypatch, activity):
    store, backend, item = await existing(monkeypatch)
    monkeypatch.setattr(store, "get_activity", lambda _key: activity)
    parser = AsyncMock()
    previous = backend.save_attempts
    with pytest.raises(PendingEventEditError) as failure:
        await store.async_reprocess(item.id, parser)
    assert str(failure.value) == "Handled or uncertain imports cannot be reprocessed"
    parser.assert_not_awaited()
    assert backend.save_attempts == previous


async def test_reprocessing_rejects_second_claim_of_same_import(monkeypatch):
    store, backend, item = await existing(monkeypatch)
    store._reprocessing.add(item.id)
    parser = AsyncMock()
    with pytest.raises(PendingEventEditError) as failure:
        await store.async_reprocess(item.id, parser)
    assert str(failure.value) == "Source reprocessing is already in progress"
    parser.assert_not_awaited()


async def test_reprocessing_rejects_stale_snapshot_before_committing(monkeypatch):
    store, backend, item = await existing(monkeypatch)
    stale = replace(item, source_text="stale snapshot")
    original_saves = backend.save_attempts
    with pytest.raises(PendingEventEditError) as failure:
        await store._async_commit_reprocessed(stale, (), ())
    assert str(failure.value) == (
        "Import changed during reprocessing; refresh before retrying"
    )
    assert backend.save_attempts == original_saves
    assert store.get(item.id) is item


async def test_reprocessing_persists_precise_duplicate_count_and_activity(monkeypatch):
    store, backend, original = await existing(monkeypatch)
    novel_draft = replace(draft(), title="Second retained event")
    candidates = (
        PendingEvent.create(draft(), "calendar.family"),
        PendingEvent.create(novel_draft, "calendar.family"),
        PendingEvent.create(novel_draft, "calendar.family"),
    )
    real_transition = store._transition
    transition = Mock(wraps=real_transition)
    monkeypatch.setattr(store, "_transition", transition)
    updated, added, duplicate_count = await store.async_reprocess(
        original.id, AsyncMock(return_value=(candidates, ("a warning",)))
    )
    assert (added, duplicate_count) == (1, 2)
    assert updated.duplicate_events == original.duplicate_events + 2
    assert updated.events == original.events + (candidates[1],)
    assert updated.warnings == ("a warning",)
    assert transition.call_args.args[0] is updated
    assert transition.call_args.args[1] == "source_reprocessed"
    assert transition.call_args.kwargs == {"remaining": updated.events}
    assert store.get_activity(original.id)["transitions"][-1]["type"] == "source_reprocessed"
    assert backend.saved[-1]["activity"] == list(store.list_activity())
