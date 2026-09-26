"""Integration tests against a real Home Assistant test instance."""

from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import (
    ATTR_SOURCE_ID,
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_APPROVE_PENDING,
    SERVICE_APPROVE_PENDING_EVENT,
    SERVICE_EDIT_PENDING_EVENT,
    SERVICE_GET_PENDING,
    SERVICE_GET_PENDING_EVENT,
    SERVICE_IMPORT_TEXT,
    SERVICE_LIST_PENDING,
    SERVICE_PARSE_TEXT,
    SERVICE_REJECT_PENDING,
    SERVICE_REJECT_PENDING_EVENT,
    SERVICE_RESOLVE_PENDING_EVENT,
    SERVICE_SUBMIT_TEXT,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.storage import PendingImportStore


SERVICES = (
    SERVICE_PARSE_TEXT,
    SERVICE_IMPORT_TEXT,
    SERVICE_SUBMIT_TEXT,
    SERVICE_APPROVE_PENDING,
    SERVICE_REJECT_PENDING,
    SERVICE_LIST_PENDING,
    SERVICE_GET_PENDING,
    SERVICE_GET_PENDING_EVENT,
    SERVICE_EDIT_PENDING_EVENT,
    SERVICE_REJECT_PENDING_EVENT,
    SERVICE_APPROVE_PENDING_EVENT,
    SERVICE_RESOLVE_PENDING_EVENT,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Daylight Calendar Import",
        data={
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
        },
    )


def _draft() -> EventDraft:
    return EventDraft(
        title="Soccer Practice",
        start="2026-10-08T17:30:00-07:00",
        end="2026-10-08T18:30:00-07:00",
        all_day=False,
        location="Park",
        description="Bring water",
        confidence=0.9,
    )


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def _assert_services(hass: HomeAssistant, *, registered: bool) -> None:
    for service in SERVICES:
        assert hass.services.has_service(DOMAIN, service) is registered


async def test_real_setup_registers_services_and_dispatches_submit(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Set up through HA and dispatch a registered action through its service registry."""
    parsed = [_draft()]
    parse = AsyncMock(return_value=parsed)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_parse_text", parse
    )
    entry = _entry()

    await _setup_entry(hass, entry)

    _assert_services(hass, registered=True)
    store = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(store, PendingImportStore)
    assert store.list() == ()

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SUBMIT_TEXT,
        {
            ATTR_TEXT: "Soccer practice Thursday at 5:30",
            ATTR_SOURCE_ID: "message-123",
        },
        blocking=True,
        return_response=True,
    )

    parse.assert_awaited_once_with(
        hass,
        text="Soccer practice Thursday at 5:30",
        ai_task_entity="ai_task.test",
    )
    assert response is not None
    assert response["duplicate"] is False
    assert response["duplicate_source"] is False
    assert response["duplicate_events"] == 0
    pending = response["pending"]
    assert pending["source_text"] == "Soccer practice Thursday at 5:30"
    assert pending["events"][0]["title"] == "Soccer Practice"
    assert len(store.list()) == 1
    assert store.list()[0].id == pending["id"]


async def test_real_unload_unregisters_services_but_keeps_persisted_storage(
    hass: HomeAssistant,
) -> None:
    """Unloading removes runtime wiring without deleting durable pending data."""
    entry = _entry()
    await _setup_entry(hass, entry)
    store = hass.data[DOMAIN][entry.entry_id]
    added = await store.async_add(
        source_text="Persist me",
        events=[_draft()],
        source_id="message-persist",
    )
    assert added.pending is not None
    pending_id = added.pending.id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    _assert_services(hass, registered=False)
    assert entry.entry_id not in hass.data[DOMAIN]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    _assert_services(hass, registered=True)
    reloaded = hass.data[DOMAIN][entry.entry_id]
    restored = reloaded.get(pending_id)
    assert restored is not None
    assert restored.source_text == "Persist me"
    assert restored.events[0].draft == _draft()
    assert reloaded.is_source_duplicate("message-persist") is True


async def test_real_remove_entry_deletes_storage_and_runtime_wiring(
    hass: HomeAssistant,
) -> None:
    """Removing the config entry unloads services and deletes its private Store data."""
    entry = _entry()
    await _setup_entry(hass, entry)
    store = hass.data[DOMAIN][entry.entry_id]
    added = await store.async_add(
        source_text="Delete me",
        events=[_draft()],
        source_id="message-delete",
    )
    assert added.pending is not None

    assert await hass.config_entries.async_remove(entry.entry_id) is not None
    await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    _assert_services(hass, registered=False)
    assert entry.entry_id not in hass.data[DOMAIN]

    fresh_store = PendingImportStore(hass)
    await fresh_store.async_load()
    assert fresh_store.list() == ()
    assert fresh_store.is_source_duplicate("message-delete") is False
