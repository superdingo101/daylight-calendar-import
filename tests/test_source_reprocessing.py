"""Explicit retained-source reprocessing preserves review work and dedupe."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized, ServiceValidationError

from custom_components.daylight_calendar_import import async_setup_entry
from custom_components.daylight_calendar_import.const import DOMAIN, SERVICE_REPROCESS_PENDING
from custom_components.daylight_calendar_import.parser import ParseOutcome
from custom_components.daylight_calendar_import.storage import PendingEvent, PendingEventEditError
from test_storage import make_store, FakeStoreBackend, draft
from test_init import FakeHass, FakePermissions, entry


def candidates(*drafts):
    return tuple(PendingEvent.create(item, 'calendar.family') for item in drafts), ('Parser warning',)


async def test_reparse_preserves_originals_and_deduplicates_everywhere(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    original = (await store.async_add(source_text='retained', events=[draft()], source_id='source')).pending
    handled = replace(draft(), title='Handled')
    other = (await store.async_add(source_text='other', events=[handled])).pending
    await store.async_remove(other.id)
    novel = replace(draft(), title='New event')
    updated, added, duplicates = await store.async_reprocess(original.id, AsyncMock(return_value=candidates(draft(), handled, novel, novel)))
    assert added == 1 and duplicates == 3
    assert updated.events[:1] == original.events
    assert updated.source_fingerprint == original.source_fingerprint
    assert updated.source_text == 'retained'
    assert store.is_source_duplicate('source')
    assert updated.warnings == ('Parser warning',)
    assert store.get_activity(original.id)['transitions'][-1]['type'] == 'source_reprocessed'
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.get(original.id) == updated
    assert restored.is_source_duplicate('source')
    _, added, duplicates = await restored.async_reprocess(original.id, AsyncMock(return_value=candidates(novel)))
    assert added == 0 and duplicates == 1


@pytest.mark.parametrize('kind', ['email', 'image', 'pdf'])
async def test_unavailable_material_refused_without_parser(monkeypatch, kind):
    store = make_store(monkeypatch, FakeStoreBackend())
    item = (await store.async_add(source_text='summary', events=[draft()], source_kind=kind)).pending
    parser = AsyncMock()
    with pytest.raises(PendingEventEditError, match='unavailable'):
        await store.async_reprocess(item.id, parser)
    parser.assert_not_awaited()


async def test_missing_handled_and_uncertain_refused(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    parser = AsyncMock()
    with pytest.raises(PendingEventEditError, match='not found'):
        await store.async_reprocess('missing', parser)
    item = (await store.async_add(source_text='text', events=[draft(), replace(draft(), title='Second')])).pending
    await store.async_reject_event(item.id, item.events[0].id)
    with pytest.raises(PendingEventEditError, match='Handled'):
        await store.async_reprocess(item.id, parser)
    other = (await store.async_add(source_text='text', events=[replace(draft(), title='Third')])).pending
    with pytest.raises(RuntimeError):
        await store.async_approve_event(other.id, other.events[0].id, AsyncMock(side_effect=RuntimeError()))
    with pytest.raises(PendingEventEditError, match='uncertain'):
        await store.async_reprocess(other.id, parser)
    parser.assert_not_awaited()


async def test_concurrent_reparse_refused_and_changed_review_retained(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    item = (await store.async_add(source_text='text', events=[draft()])).pending
    started, proceed = asyncio.Event(), asyncio.Event()
    async def parse(_item):
        started.set()
        await proceed.wait()
        return candidates(replace(draft(), title='New'))
    task = asyncio.create_task(store.async_reprocess(item.id, parse))
    await started.wait()
    with pytest.raises(PendingEventEditError, match='already in progress'):
        await store.async_reprocess(item.id, AsyncMock())
    edited = await store.async_edit_event(item.id, item.events[0].id, replace(draft(), title='User edit'))
    proceed.set()
    with pytest.raises(PendingEventEditError, match='changed during'):
        await task
    assert store.get(item.id).events == (edited,)
    assert not store._reprocessing


async def test_review_changed_then_restored_still_rejects_stale_parse(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    item = (await store.async_add(source_text='text', events=[draft()])).pending
    started, proceed = asyncio.Event(), asyncio.Event()
    async def parse(_item):
        started.set()
        await proceed.wait()
        return candidates(replace(draft(), title='Novel'))
    task = asyncio.create_task(store.async_reprocess(item.id, parse))
    await started.wait()
    await store.async_edit_event(item.id, item.events[0].id, replace(draft(), title='Edited'))
    await store.async_edit_event(item.id, item.events[0].id, draft())
    assert store.get(item.id) == item
    unrelated = (await store.async_add(source_text='other', events=[replace(draft(), title='Unrelated')])).pending
    assert unrelated is not None
    proceed.set()
    with pytest.raises(PendingEventEditError, match='changed during'):
        await task
    assert store.get(item.id) == item


async def test_parse_error_cancel_and_storage_failure_leave_original(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = (await store.async_add(source_text='text', events=[draft()])).pending
    for error in (RuntimeError('provider failed'), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            await store.async_reprocess(item.id, AsyncMock(side_effect=error))
        assert store.get(item.id) == item
        assert not store._reprocessing
    backend.save_error = OSError('disk failed')
    with pytest.raises(OSError):
        await store.async_reprocess(item.id, AsyncMock(return_value=candidates()))
    assert store.get(item.id) == item
    assert not store._reprocessing


async def test_cancel_during_commit_finishes_atomic_save(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = (await store.async_add(source_text='text', events=[draft()])).pending
    started, proceed = asyncio.Event(), asyncio.Event()
    save = backend.async_save
    async def blocked(data):
        started.set()
        await proceed.wait()
        await save(data)
    backend.async_save = blocked
    task = asyncio.create_task(store.async_reprocess(item.id, AsyncMock(return_value=candidates(replace(draft(), title='Novel')))))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store.get(item.id).events) == 2
    assert backend.saved[-1]['items'][0] == store.get(item.id).as_dict()
    assert not store._reprocessing


async def setup_service(monkeypatch, permissions=True):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions(permissions)))
    monkeypatch.setattr('custom_components.daylight_calendar_import.PendingImportStore', lambda _hass: store)
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_register_review_panel', AsyncMock())
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_track_time_interval', Mock(return_value=Mock()))
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_setup_email_runtime', AsyncMock(return_value=None))
    await async_setup_entry(hass, entry())
    return store, hass.services.handlers[(DOMAIN, SERVICE_REPROCESS_PENDING)][0]


async def test_service_permissions_response_and_source_reference(monkeypatch):
    store, handler = await setup_service(monkeypatch)
    item = (await store.async_add(source_text='retained text', events=[draft()])).pending
    provider = AsyncMock(return_value=ParseOutcome([draft(), replace(draft(), title='Novel')], [], [(), ('Local time',)]))
    monkeypatch.setattr('custom_components.daylight_calendar_import._async_parse_source', provider)
    result = await handler(SimpleNamespace(data={'pending_id':item.id}, context=Context(user_id='user')))
    assert result['added_events'] == result['duplicate_events'] == 1
    source = provider.call_args.args[1]
    assert source.text == item.source_text
    assert source.received_at.isoformat() == item.created_at
    assert store.get(item.id).events[-1].date_time_assumptions == ('Local time',)
    assert not store.active_submissions
    with pytest.raises(ServiceValidationError, match='not found'):
        await handler(SimpleNamespace(data={'pending_id':'missing'}, context=Context(user_id='user')))
    assert not store.active_submissions


@pytest.mark.parametrize('user_id,permissions', [(None, True), ('user', False)])
async def test_service_denies_before_read_or_provider(monkeypatch, user_id, permissions):
    store, handler = await setup_service(monkeypatch, permissions)
    provider = AsyncMock()
    monkeypatch.setattr('custom_components.daylight_calendar_import._async_parse_source', provider)
    with pytest.raises(Unauthorized):
        await handler(SimpleNamespace(data={'pending_id':'private'}, context=Context(user_id=user_id)))
    provider.assert_not_awaited()
    assert not store.active_submissions


async def test_service_no_events_preserves_existing_review(monkeypatch):
    store, handler = await setup_service(monkeypatch)
    item = (await store.async_add(source_text='text', events=[draft()])).pending
    monkeypatch.setattr('custom_components.daylight_calendar_import._async_parse_source', AsyncMock(return_value=ParseOutcome([], [])))
    result = await handler(SimpleNamespace(data={'pending_id':item.id}, context=Context(user_id='user')))
    assert result['added_events'] == result['duplicate_events'] == 0
    assert store.get(item.id).events == item.events
