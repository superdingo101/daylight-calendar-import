"""Permission checked single identity resets preserve the rest of review state."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized, ServiceValidationError

from custom_components.daylight_calendar_import import RESET_DEDUPLICATION_SCHEMA
from custom_components.daylight_calendar_import.const import DOMAIN, SERVICE_RESET_DEDUPLICATION
from custom_components.daylight_calendar_import.dedup import source_fingerprint, event_fingerprint
from custom_components.daylight_calendar_import.storage import PendingEventEditError
from test_storage import make_store, FakeStoreBackend, draft


async def handled(store, source_id, event):
    item = (await store.async_add(source_text='source', events=[event], source_id=source_id)).pending
    await store.async_remove(item.id)


async def test_source_reset_is_independent_single_and_durable(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await handled(store, 'first', draft())
    other = replace(draft(), title='Other')
    await handled(store, 'second', other)
    history = store.list_activity()
    assert await store.async_reset_deduplication(source_id=' first ')
    assert not store.is_source_duplicate('first')
    assert store.is_source_duplicate('second')
    assert event_fingerprint(draft()) in store._seen_event_fingerprints
    assert store.list_activity() == history
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert not restored.is_source_duplicate('first') and restored.is_source_duplicate('second')
    attempts = backend.save_attempts
    assert not await restored.async_reset_deduplication(source_id='first')
    assert not await restored.async_reset_deduplication(source_id='unknown')
    assert backend.save_attempts == attempts
    duplicate = await restored.async_add(source_text='source', events=[draft()], source_id='first')
    assert duplicate.pending is None and duplicate.duplicate_events == 1


async def test_event_reset_uses_normalized_fingerprint_and_preserves_source(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await handled(store, 'source', draft())
    other = replace(draft(), title='Other')
    await handled(store, 'other-source', other)
    normalized = replace(draft(), title='  PRACTICE  ', confidence=.1)
    assert await store.async_reset_deduplication(event=normalized)
    assert event_fingerprint(draft()) not in store._seen_event_fingerprints
    assert event_fingerprint(other) in store._seen_event_fingerprints
    assert store.is_source_duplicate('source')
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored._seen_event_fingerprints == store._seen_event_fingerprints
    duplicate = await restored.async_add(source_text='source', events=[draft()], source_id='source')
    assert duplicate.pending is None and duplicate.duplicate_source
    # Resetting an event never creates it. An explicit new submission may queue it.
    result = await restored.async_add(source_text='new', events=[draft()], source_id='new-source')
    assert result.pending is not None


async def test_pending_uncertain_and_claimed_targets_refused(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    item = (await store.async_add(source_text='source', events=[draft()], source_id='pending')).pending
    for target in ({'source_id':'pending'}, {'event':draft()}):
        with pytest.raises(PendingEventEditError, match='Pending or processing'):
            await store.async_reset_deduplication(**target)
    with pytest.raises(RuntimeError):
        await store.async_approve_event(item.id, item.events[0].id, AsyncMock(side_effect=RuntimeError()))
    with pytest.raises(PendingEventEditError):
        await store.async_reset_deduplication(event=draft())
    claim_id = await store.async_begin_source_submission(source_id='claimed', source_kind='email', source_title=None, received_at=datetime.now(UTC))
    with pytest.raises(PendingEventEditError):
        await store.async_reset_deduplication(source_id='claimed')
    assert claim_id in store._source_claims


async def test_reset_during_provider_write_refuses_without_waiting(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    item = (await store.async_add(source_text='source', events=[draft()], source_id='source')).pending
    started, proceed = asyncio.Event(), asyncio.Event()
    async def write(_event):
        started.set()
        await proceed.wait()
    approval = asyncio.create_task(store.async_approve_event(item.id, item.events[0].id, write))
    await started.wait()
    with pytest.raises(PendingEventEditError):
        await store.async_reset_deduplication(event=draft())
    proceed.set()
    await approval
    assert event_fingerprint(draft()) in store._seen_event_fingerprints


async def test_rechecks_target_after_waiting_for_transaction_lock(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    await handled(store, 'source', draft())
    await store._lock.acquire()
    task = asyncio.create_task(store.async_reset_deduplication(source_id='source'))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Simulate a new source claim arriving before the reset acquires the lock.
    store._source_claims['processing'] = source_fingerprint('source')
    store._lock.release()
    with pytest.raises(PendingEventEditError):
        await task
    assert source_fingerprint('source') in store._seen_source_fingerprints


@pytest.mark.parametrize('source', [True, False])
async def test_failed_save_preserves_target(monkeypatch, source):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await handled(store, 'source', draft())
    backend.save_error = OSError('disk failed')
    with pytest.raises(OSError):
        await store.async_reset_deduplication(**({'source_id':'source'} if source else {'event':draft()}))
    assert store.is_source_duplicate('source')
    assert event_fingerprint(draft()) in store._seen_event_fingerprints


@pytest.mark.parametrize('source', [True, False])
async def test_cancelled_save_commits_target_removal(monkeypatch, source):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await handled(store, 'source', draft())
    started, proceed = asyncio.Event(), asyncio.Event()
    save = backend.async_save
    async def blocked(data):
        started.set()
        await proceed.wait()
        await save(data)
    backend.async_save = blocked
    task = asyncio.create_task(store.async_reset_deduplication(**({'source_id':'source'} if source else {'event':draft()})))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.is_source_duplicate('source') != source
    assert (event_fingerprint(draft()) in store._seen_event_fingerprints) == source
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored._seen_event_fingerprints == store._seen_event_fingerprints
    assert restored._seen_source_fingerprints == store._seen_source_fingerprints


async def test_storage_requires_one_target(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    for data in ({}, {'source_id':'source','event':draft()}):
        with pytest.raises(ValueError, match='exactly one'):
            await store.async_reset_deduplication(**data)


@pytest.mark.parametrize('data', [{}, {'source_id':'source','event':{}}, {'source_id':' '}, {'event':[]}, {'fingerprint':'v1:source:guess'}])
def test_schema_rejects_ambiguous_missing_and_invalid_targets(data):
    with pytest.raises(vol.Invalid):
        RESET_DEDUPLICATION_SCHEMA(data)


async def test_service_authorizes_validates_and_returns_target_result(monkeypatch):
    from unittest.mock import Mock
    from test_init import FakeHass, FakePermissions, entry
    from custom_components.daylight_calendar_import import async_setup_entry
    store = make_store(monkeypatch, FakeStoreBackend())
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions(True)))
    monkeypatch.setattr('custom_components.daylight_calendar_import.PendingImportStore', lambda _hass: store)
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_register_review_panel', AsyncMock())
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_track_time_interval', Mock(return_value=Mock()))
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_setup_email_runtime', AsyncMock(return_value=None))
    await async_setup_entry(hass, entry())
    handler, kwargs = hass.services.handlers[(DOMAIN,SERVICE_RESET_DEDUPLICATION)]
    assert kwargs['schema'] is RESET_DEDUPLICATION_SCHEMA
    await handled(store, 'source', draft())
    async def invoke(data):
        return await handler(SimpleNamespace(data=data,context=Context(user_id='user')))
    assert await invoke({'source_id':'source'}) == {'reset':True,'kind':'source'}
    assert await invoke({'event':draft().as_dict()}) == {'reset':True,'kind':'event'}
    assert await invoke({'source_id':'source'}) == {'reset':False,'kind':'source'}
    with pytest.raises(ServiceValidationError, match='Invalid event'):
        await invoke({'event':{}})
    item = (await store.async_add(source_text='source',events=[draft()],source_id='source')).pending
    with pytest.raises(ServiceValidationError,match='Pending or processing'):
        await invoke({'source_id':'source'})
    assert store.get(item.id) == item
    for permissions,user in ((False,'user'),(True,None)):
        hass.auth.user.permissions.allowed=permissions
        with pytest.raises(Unauthorized):
            await handler(SimpleNamespace(data={'source_id':'source'},context=Context(user_id=user)))

@pytest.mark.parametrize('kind', ['text', 'image', 'pdf'])
async def test_overlapping_submission_source_stays_protected_until_last_parser_exits(monkeypatch, kind):
    from contextlib import asynccontextmanager
    from unittest.mock import Mock
    from test_init import FakeHass, FakePermissions, entry
    from custom_components.daylight_calendar_import import async_setup_entry
    from custom_components.daylight_calendar_import.parser import ParseOutcome
    from custom_components.daylight_calendar_import.sources import SourceDocument, SourceKind
    store = make_store(monkeypatch, FakeStoreBackend())
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions(True)))
    monkeypatch.setattr('custom_components.daylight_calendar_import.PendingImportStore', lambda _hass: store)
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_register_review_panel', AsyncMock())
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_track_time_interval', Mock(return_value=Mock()))
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_setup_email_runtime', AsyncMock(return_value=None))
    @asynccontextmanager
    async def upload(*_args):
        yield SourceDocument('upload', SourceKind(kind), datetime.now(UTC), text='retained')
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_image_source', upload)
    monkeypatch.setattr('custom_components.daylight_calendar_import.async_pdf_source', upload)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = 0
    async def parse(*_args):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await release[index].wait()
        return ParseOutcome([draft()], [])
    monkeypatch.setattr('custom_components.daylight_calendar_import._async_parse_source', parse)
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, 'submit_' + kind)][0]
    data = {'source_id':'shared-source', 'text':'retained'}
    if kind != 'text':
        data['file_id'] = 'a' * 32
    def call():
        return SimpleNamespace(data=data, context=Context(user_id='user'))
    first = asyncio.create_task(handler(call()))
    await entered[0].wait()
    second = asyncio.create_task(handler(call()))
    await entered[1].wait()
    assert list(store.processing_source_fingerprints.values()) == [source_fingerprint('shared-source')] * 2
    release[0].set()
    result = await first
    await store.async_remove(result['pending']['id'])
    assert len(store.processing_source_fingerprints) == 1
    with pytest.raises(PendingEventEditError, match='processing'):
        await store.async_reset_deduplication(source_id='shared-source')
    # Cancellation releases only this invocation's registry entry.
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert not store.processing_source_fingerprints
    assert await store.async_reset_deduplication(source_id='shared-source')
