"""Beta audit of the durable queue's public automation/privacy boundaries."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import import diagnostics, sensor
from custom_components.daylight_calendar_import.const import DOMAIN
from custom_components.daylight_calendar_import.ha_notification_sink import async_send_ha_notification
from custom_components.daylight_calendar_import.notification_preferences import NOTIFICATION_CLASSES
from custom_components.daylight_calendar_import.notifications import notification_from_transition
from tests.test_ha_notification_sink import hass_with_notify, policy
from tests.test_notification_delivery import run, runtime
from tests.test_storage import FakeStoreBackend, draft

SENSITIVE = ("patient@example.com", "zoom.us/j/123", "secret-code")
PRIVATE = "patient@example.com https://zoom.us/j/123?pwd=secret-code"


@pytest.mark.parametrize("kind", sorted(NOTIFICATION_CLASSES))
async def test_every_notification_category_projects_only_static_safe_content(kind):
    """All shipped classes omit raw source, event, provider and meeting data."""
    activity = {"id": "opaque-import", "title": PRIVATE, "source_text": PRIVATE,
                "description": PRIVATE, "location": PRIVATE, "guidance": PRIVATE}
    transition = {"type": kind, "at": "2026-10-15T12:00:00+00:00",
                  "event_id": "opaque-event", "error": PRIVATE, "message": PRIVATE}
    event = notification_from_transition(activity, transition)
    assert event is not None
    hass = hass_with_notify()
    assert await async_send_ha_notification(hass, event, policy(classes=(kind,)))
    call = hass.services.async_call.await_args
    assert set(call.args[2]) == {"title", "message"}
    assert all(value not in str(call) for value in SENSITIVE)
    assert "opaque-import" not in str(call) and "opaque-event" not in str(call)
    disabled = hass_with_notify()
    assert not await async_send_ha_notification(disabled, event, policy(enabled=False, classes=(kind,)))
    disabled.services.async_call.assert_not_awaited()


async def test_queue_reload_keeps_counts_and_deduplicated_private_notifications(monkeypatch):
    """Durable replay cannot leak meeting data or replay a completed delivery."""
    from custom_components.daylight_calendar_import import notification_delivery
    from unittest.mock import AsyncMock

    backend = FakeStoreBackend()
    store = runtime(monkeypatch, backend)
    result = await store.async_add(source_text=PRIVATE, events=[
        replace(draft(), description=PRIVATE, location=PRIVATE),
        replace(draft(), title="Second appointment", description=PRIVATE),
    ])
    hass = hass_with_notify()
    async def send(_hass, event, preferences):
        return await async_send_ha_notification(hass, event, preferences)

    sink = AsyncMock(side_effect=send)
    monkeypatch.setattr(notification_delivery, "async_send_ha_notification", sink)
    await run(store)
    assert len(store.list_notifications()) == 1
    assert store.list_notifications()[0].status == "sent"
    assert all(value not in json.dumps([r.as_dict() for r in store.list_notifications()])
               for value in SENSITIVE)
    assert all(value not in json.dumps(store.list_activity()) for value in SENSITIVE)
    backend.load_result = backend.saved[-1]
    restored = runtime(monkeypatch, backend)
    await restored.async_load()
    await run(restored)
    sink.assert_awaited_once()
    entities = []
    entry = SimpleNamespace(entry_id="beta", data={}, options={})
    view_hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: restored}})
    await sensor.async_setup_entry(view_hass, entry, entities.extend)
    assert [entity.native_value for entity in entities] == [1, 2]
    exported = await diagnostics.async_get_config_entry_diagnostics(view_hass, entry)
    assert exported["queue"]["imports"] == 1
    assert exported["queue"]["events"] == 2
    assert exported["notifications"]["status"]["sent"] == 1
    assert all(value not in json.dumps(exported) for value in SENSITIVE)
    assert result.pending.id not in json.dumps(exported)
    assert await restored.async_remove(result.pending.id)
    assert [entity.native_value for entity in entities] == [0, 0]


async def test_real_queue_automation_event_has_only_id_and_count(hass, monkeypatch):
    """The installed runtime's event bus receives no source or meeting content."""
    from unittest.mock import AsyncMock
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.daylight_calendar_import.const import CONF_AI_TASK_ENTITY, CONF_CALENDAR_ENTITY

    monkeypatch.setattr("custom_components.daylight_calendar_import.async_classify_conflicts",
                        AsyncMock(return_value=[]))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_AI_TASK_ENTITY: "ai_task.test", CONF_CALENDAR_ENTITY: "calendar.family"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    events = []
    unsubscribe = hass.bus.async_listen(f"{DOMAIN}_pending_added", events.append)
    store = hass.data[DOMAIN][entry.entry_id]
    result = await store.async_add(source_text=PRIVATE, events=[replace(draft(), description=PRIVATE)])
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {"pending_id": result.pending.id, "event_count": 1}
    duplicate = await store.async_add(source_text=PRIVATE, events=[replace(draft(), description=PRIVATE)])
    assert duplicate.pending is None
    await hass.async_block_till_done()
    assert len(events) == 1
    unsubscribe()
