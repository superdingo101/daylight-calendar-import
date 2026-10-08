"""Native Home Assistant pending queue sensor and commit subscription tests."""

from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.daylight_calendar_import import sensor
from custom_components.daylight_calendar_import.storage import PendingImportStore
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.const import DOMAIN


def _draft():
    return EventDraft(
        title="Practice", start="2026-10-08T17:30:00-07:00",
        end="2026-10-08T18:30:00-07:00", all_day=False,
    )


async def test_two_entities_are_registered_and_read_live_counts(hass):
    store = PendingImportStore(hass)
    await store.async_load()
    entry = SimpleNamespace(entry_id="unit-entry")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = store
    captured = []
    await sensor.async_setup_entry(hass, entry, captured.extend)
    assert len(captured) == 2
    imports, events = captured
    assert imports.unique_id == "unit-entry_pending_imports"
    assert events.unique_id == "unit-entry_pending_events"
    assert imports.native_value == 0
    assert events.native_value == 0

    changed = Mock()
    unsubscribe = store.async_subscribe(changed)
    result = await store.async_add(source_text="Practice", events=[_draft()])
    assert result.pending is not None
    assert imports.native_value == 1
    assert events.native_value == 1
    assert changed.call_count == 1

    assert await store.async_remove(result.pending.id)
    assert imports.native_value == 0
    assert events.native_value == 0
    assert changed.call_count == 2

    unsubscribe()
    await store.async_add(source_text="Another practice", events=[_draft()])
    assert changed.call_count == 2


async def test_store_subscribers_cannot_break_committed_writes(hass, caplog):
    store = PendingImportStore(hass)
    await store.async_load()
    healthy = Mock()
    def failing():
        raise RuntimeError("listener bug")
    store.async_subscribe(failing)
    store.async_subscribe(healthy)
    result = await store.async_add(source_text="One event", events=[_draft()])
    assert result.pending is not None
    healthy.assert_called_once()
    assert "subscriber failed" in caplog.text


async def test_sensor_subscription_lifecycle(hass):
    store = PendingImportStore(hass)
    await store.async_load()
    entry = SimpleNamespace(entry_id="unit-entry")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = store
    captured = []
    await sensor.async_setup_entry(hass, entry, captured.extend)
    entity = captured[0]
    callback = Mock()
    entity.async_write_ha_state = callback
    await entity.async_added_to_hass()
    await store.async_add(source_text="Added", events=[_draft()])
    callback.assert_called_once()
    await entity.async_will_remove_from_hass()
    await store.async_add(source_text="Another", events=[_draft()])
    callback.assert_called_once()
