"""Public beta review decisions and durable uncertain-write recovery in HA."""

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.calendar.const import DATA_COMPONENT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY, CONF_CALENDAR_ALIASES, CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY, CONF_CONFLICT_CALENDAR_ENTITIES, DOMAIN,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import ParseOutcome

DRAFT = EventDraft("Practice", "2026-11-12T17:00:00-08:00", "2026-11-12T18:00:00-08:00", False)


async def call(client, service, **data):
    await client.send_json_auto_id({"type": "call_service", "domain": DOMAIN,
                                   "service": service, "service_data": data,
                                   "return_response": True})
    return await client.receive_json()


@pytest.fixture
async def workflow(hass, hass_ws_client, monkeypatch):
    parser = AsyncMock(return_value=ParseOutcome([DRAFT], []))
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parser)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_AI_TASK_ENTITY: "ai_task.test",
        CONF_CALENDAR_ENTITY: "calendar.family", CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.kids"]},
        options={CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.kids"], CONF_CALENDAR_ALIASES: {"kids": "calendar.kids"},
                 CONF_CONFLICT_CALENDAR_ENTITIES: ["calendar.work", "calendar.school"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    providers = {name: SimpleNamespace(available=True, async_get_events=AsyncMock(return_value=[]))
                 for name in ("calendar.family", "calendar.kids", "calendar.work", "calendar.school")}
    monkeypatch.setitem(hass.data, DATA_COMPONENT, SimpleNamespace(get_entity=providers.get))
    writes = AsyncMock()
    hass.services.async_register("calendar", "create_event", writes)
    return SimpleNamespace(entry=entry, client=await hass_ws_client(hass), parser=parser, providers=providers,
                           writes=writes, store=hass.data[DOMAIN][entry.entry_id])




async def submit(runtime, events):
    runtime.parser.return_value = ParseOutcome(events, [])
    result = await call(runtime.client, "submit_text", text="Schedule for review")
    assert result["success"], result
    return result["result"]["response"]["pending"]


def next_draft(title):
    return EventDraft(title, "2026-11-13T17:00:00-08:00", "2026-11-13T18:00:00-08:00", False)


async def reload_store(hass, runtime):
    assert await hass.config_entries.async_unload(runtime.entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(runtime.entry.entry_id)
    await hass.async_block_till_done()
    runtime.store = hass.data[DOMAIN][runtime.entry.entry_id]


async def test_reject_one_approve_one_and_bulk_remaining_survive_reload(hass, workflow):
    runtime = workflow
    pending = await submit(runtime, [DRAFT, next_draft("Game"), next_draft("Concert")])
    first, second, third = pending["events"]
    rejected = await call(runtime.client, "reject_pending_event", pending_id=pending["id"], event_id=second["id"])
    assert rejected["success"], rejected
    approved = await call(runtime.client, "approve_pending_event", pending_id=pending["id"], event_id=first["id"])
    assert approved["success"], approved
    await reload_store(hass, runtime)
    remaining = runtime.store.get(pending["id"])
    assert [event.id for event in remaining.events] == [third["id"]]
    final = await call(runtime.client, "approve_pending", pending_id=pending["id"])
    assert final["success"], final
    assert final["result"]["response"]["imported"] == 1
    assert [args.args[0].data["summary"] for args in runtime.writes.await_args_list] == ["Practice", "Concert"]
    await reload_store(hass, runtime)
    assert runtime.store.list() == ()
    assert runtime.writes.await_count == 2


async def test_concurrent_single_and_bulk_approval_write_each_event_once(hass, hass_ws_client, monkeypatch, workflow):
    runtime = workflow
    pending = await submit(runtime, [DRAFT, next_draft("Game")])
    entered, release, bulk_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    approval_lock = runtime.store._lock
    acquire = approval_lock.acquire
    process_events = runtime.store.async_process_events
    bulk_handler_task = None

    async def observe_bulk_handler(*args, **kwargs):
        nonlocal bulk_handler_task
        bulk_handler_task = asyncio.current_task()
        return await process_events(*args, **kwargs)

    async def observe_acquire():
        if asyncio.current_task() is bulk_handler_task:
            assert approval_lock.locked()
            bulk_waiting.set()
        # With the first writer blocked, this real acquire suspends before
        # the event waiter can resume; the bulk handler is now queued.
        return await acquire()

    monkeypatch.setattr(runtime.store, "async_process_events", observe_bulk_handler)
    monkeypatch.setattr(approval_lock, "acquire", observe_acquire)

    async def hold_first_write(_call):
        if not entered.is_set():
            entered.set()
            await release.wait()

    runtime.writes.side_effect = hold_first_write
    single = asyncio.create_task(call(runtime.client, "approve_pending_event",
        pending_id=pending["id"], event_id=pending["events"][0]["id"]))
    other = await hass_ws_client(hass)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        bulk = asyncio.create_task(call(other, "approve_pending", pending_id=pending["id"]))
        await asyncio.wait_for(bulk_waiting.wait(), 5)
        assert not bulk.done()
        assert not release.is_set()
        assert runtime.writes.await_count == 1
    finally:
        release.set()
    first_result, bulk_result = await asyncio.gather(single, bulk)
    assert first_result["success"], first_result
    assert bulk_result["success"], bulk_result
    assert [args.args[0].data["summary"] for args in runtime.writes.await_args_list] == ["Practice", "Game"]
    assert runtime.store.list() == ()


async def test_unavailable_destination_is_retryable_without_uncertain_checkpoint(workflow):
    runtime = workflow
    pending = await submit(runtime, [DRAFT, next_draft("Game")])
    runtime.providers["calendar.family"].available = False
    refused = await call(runtime.client, "approve_pending", pending_id=pending["id"])
    assert refused["success"] is False
    assert "before calendar write" in refused["error"]["message"]
    assert [event.status for event in runtime.store.get(pending["id"]).events] == ["pending", "pending"]
    runtime.writes.assert_not_awaited()
    runtime.providers["calendar.family"].available = True
    retried = await call(runtime.client, "approve_pending", pending_id=pending["id"])
    assert retried["success"], retried
    assert runtime.writes.await_count == 2


@pytest.mark.parametrize("resolution", ["created", "not_created", "discard"])
async def test_interrupted_bulk_write_restart_requires_explicit_resolution(hass, workflow, resolution):
    runtime = workflow
    pending = await submit(runtime, [DRAFT, next_draft("Game"), next_draft("Concert")])
    _, second, third = pending["events"]
    # A provider may create an event and lose its response. The integration
    # cannot infer whether this timed-out request committed externally.
    runtime.writes.side_effect = [None, TimeoutError("response lost")]
    failed = await call(runtime.client, "approve_pending", pending_id=pending["id"])
    assert failed["success"] is False
    assert runtime.writes.await_count == 2
    await reload_store(hass, runtime)
    remaining = runtime.store.get(pending["id"])
    assert [event.id for event in remaining.events] == [second["id"], third["id"]]
    assert [event.status for event in remaining.events] == ["write_uncertain", "pending"]
    runtime.writes.side_effect = None
    for service, extra in [("approve_pending", {}), ("approve_pending_event", {"event_id": second["id"]}),
                           ("reject_pending_event", {"event_id": second["id"]}), ("reject_pending", {})]:
        refused = await call(runtime.client, service, pending_id=pending["id"], **extra)
        assert refused["success"] is False, (service, refused)
    assert runtime.writes.await_count == 2
    resolved = await call(runtime.client, "resolve_pending_event", pending_id=pending["id"],
                          event_id=second["id"], resolution=resolution)
    assert resolved["success"], resolved
    await reload_store(hass, runtime)
    final = await call(runtime.client, "approve_pending", pending_id=pending["id"])
    assert final["success"], final
    assert runtime.store.list() == ()
    expected = ["Practice", "Game"] + (["Game"] if resolution == "not_created" else []) + ["Concert"]
    assert [args.args[0].data["summary"] for args in runtime.writes.await_args_list] == expected
