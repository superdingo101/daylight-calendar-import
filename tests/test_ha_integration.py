"""Integration tests against a real Home Assistant test instance."""

from unittest.mock import ANY, AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.components.frontend import async_panel_exists
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import (
    ATTR_SOURCE_ID,
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITIES,
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
    SERVICE_SUBMIT_IMAGE,
    SERVICE_SUBMIT_PDF,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import ParseOutcome
from custom_components.daylight_calendar_import.storage import PendingImportStore
from custom_components.daylight_calendar_import.review_panel import PANEL_PATH


SERVICES = (
    SERVICE_PARSE_TEXT,
    SERVICE_IMPORT_TEXT,
    SERVICE_SUBMIT_TEXT,
    SERVICE_SUBMIT_IMAGE,
    SERVICE_SUBMIT_PDF,
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
    assert async_panel_exists(hass, PANEL_PATH)


async def test_legacy_entry_migrates_on_setup(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup_entry(hass, entry)
    assert entry.version == 2
    assert entry.data[CONF_CALENDAR_ENTITIES] == ["calendar.family"]


async def test_unsupported_entry_version_refuses_migration(hass: HomeAssistant) -> None:
    from custom_components.daylight_calendar_import import async_migrate_entry

    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, version=3)
    assert await async_migrate_entry(hass, entry) is False


def _assert_services(hass: HomeAssistant, *, registered: bool) -> None:
    for service in SERVICES:
        assert hass.services.has_service(DOMAIN, service) is registered


async def test_real_setup_registers_services_and_dispatches_submit(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Set up through HA and dispatch a registered action through its service registry."""
    parsed = [_draft()]
    parse = AsyncMock(return_value=ParseOutcome(parsed, []))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider", parse
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
        source=ANY,
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
    assert not async_panel_exists(hass, PANEL_PATH)
    _assert_services(hass, registered=False)
    assert entry.entry_id not in hass.data[DOMAIN]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert async_panel_exists(hass, PANEL_PATH)
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


async def test_pending_added_event_is_private_and_only_fires_after_durable_queue(hass, monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.daylight_calendar_import.parser import ParseOutcome

    parse = AsyncMock(return_value=ParseOutcome([_draft()], []))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider", parse,
    )
    entry = _entry()
    await _setup_entry(hass, entry)
    seen = []
    cancel = hass.bus.async_listen(
        f"{DOMAIN}_pending_added", lambda event: seen.append(event.data),
    )
    try:
        payload = {ATTR_TEXT: "Bring drinks to the practice", ATTR_SOURCE_ID: "message-private"}
        result = await hass.services.async_call(
            DOMAIN, SERVICE_SUBMIT_TEXT, payload, blocking=True, return_response=True,
        )
        await hass.async_block_till_done()
        assert len(seen) == 1
        assert seen[0] == {"pending_id": result["pending"]["id"], "event_count": 1}
        assert "drinks" not in str(seen)
        assert "sender" not in seen[0]
        duplicate = await hass.services.async_call(
            DOMAIN, SERVICE_SUBMIT_TEXT, payload, blocking=True, return_response=True,
        )
        await hass.async_block_till_done()
        assert duplicate["duplicate_source"] is True
        assert len(seen) == 1
    finally:
        cancel()


async def test_pending_added_bus_failure_does_not_rollback_durable_import(hass, monkeypatch, caplog):
    from unittest.mock import Mock
    from custom_components.daylight_calendar_import.parser import ParseOutcome

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider",
        AsyncMock(return_value=ParseOutcome([_draft()], [])),
    )
    entry = _entry()
    await _setup_entry(hass, entry)
    monkeypatch.setattr(
        type(hass.bus), "async_fire", Mock(side_effect=RuntimeError("subscriber disconnected")),
    )
    response = await hass.services.async_call(
        DOMAIN, SERVICE_SUBMIT_TEXT,
        {ATTR_TEXT: "Another practice", ATTR_SOURCE_ID: "source-for-bus-failure"},
        blocking=True, return_response=True,
    )
    assert response["pending"] is not None
    assert len(hass.data[DOMAIN][entry.entry_id].list()) == 1
    assert "notification could not be published" in caplog.text


async def test_opted_in_review_ready_dispatch_is_post_commit_and_not_replayed(hass, monkeypatch):
    """New durable pending imports notify, but duplicate source replay does not."""
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider",
        AsyncMock(return_value=ParseOutcome([_draft()], [])),
    )
    emitted = []

    async def capture(hass_instance, activity, preferences):
        store = hass_instance.data[DOMAIN][entry.entry_id]
        assert store.get_activity(activity["id"]) is not None
        assert store.get(activity["id"]) is not None
        assert preferences.permits("review_ready")
        emitted.append(activity["id"])

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_notify_review_ready",
        capture,
    )
    entry = MockConfigEntry(
        domain=DOMAIN, title="Daylight Calendar Import",
        data={CONF_AI_TASK_ENTITY: "ai_task.test",
              CONF_CALENDAR_ENTITY: "calendar.family"},
        options={CONF_NOTIFICATION_PREFERENCES: {
            "enabled": True, "target": "notify.phone", "classes": ["review_ready"],
        }},
    )
    await _setup_entry(hass, entry)
    source = {ATTR_TEXT: "Practice with water", ATTR_SOURCE_ID: "notification-source"}
    response = await hass.services.async_call(
        DOMAIN, SERVICE_SUBMIT_TEXT, source, blocking=True, return_response=True,
    )
    await hass.async_block_till_done()
    assert emitted == [response["pending"]["id"]]
    await hass.services.async_call(
        DOMAIN, SERVICE_SUBMIT_TEXT, source, blocking=True, return_response=True,
    )
    await hass.async_block_till_done()
    assert emitted == [response["pending"]["id"]]
