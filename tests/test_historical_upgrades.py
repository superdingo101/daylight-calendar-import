"""Historical wire fixtures survive the public migration/load/save interfaces."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import import async_migrate_entry
from custom_components.daylight_calendar_import.const import DOMAIN
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.settings import effective_calendar_options
from custom_components.daylight_calendar_import.storage import (
    PendingImportApprovalUncertainError, PendingImportStore, STORAGE_KEY, _PendingStore,
)

FIXTURES = Path(__file__).parent / "fixtures" / "historical_upgrades"
CONFIGS = json.loads((FIXTURES / "config_entries.json").read_text())


@pytest.mark.parametrize("raw", CONFIGS, ids=["single-calendar-v1", "multi-calendar-v2"])
async def test_historical_config_migration_preserves_options_and_is_idempotent(hass, raw):
    original = deepcopy(raw)
    entry = MockConfigEntry(domain=DOMAIN, **raw)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert dict(entry.options) == original["options"]
    assert dict(entry.data) == {
        **original["data"],
        "calendar_entities": original["data"].get("calendar_entities", ["calendar.family"]),
    }
    assert effective_calendar_options(entry) == ("calendar.school", ["calendar.family", "calendar.school"])
    snapshot = (dict(entry.data), dict(entry.options), entry.version)
    assert await async_migrate_entry(hass, entry)
    assert (dict(entry.data), dict(entry.options), entry.version) == snapshot


@pytest.mark.parametrize("name", ["storage-v1", "v0.5.3", "v0.7.1"])
async def test_historical_storage_disk_round_trip_preserves_decisions(hass, tmp_path, monkeypatch, freezer, name):
    freezer.move_to("2026-10-15T17:00:00+00:00")
    hass.config.config_dir = str(tmp_path)
    original = json.loads((FIXTURES / f"{name}.json").read_text())
    path = tmp_path / ".storage" / STORAGE_KEY
    await hass.async_add_executor_job(path.parent.mkdir, 0o700, True, True)
    await hass.async_add_executor_job(path.write_text, json.dumps(original))
    # Bypass only the test framework's in-memory storage shim; retain HA's
    # real envelope migration, private-file serialization, and atomic writes.
    monkeypatch.setattr(_PendingStore, "_async_load", _PendingStore._async_load_data)
    async def write_disk(store, envelope):
        await hass.async_add_executor_job(store._write_data, envelope)
    monkeypatch.setattr(_PendingStore, "_async_write_data", write_disk)

    store = PendingImportStore(hass)
    await store.async_load()
    pending = store.list()[0]
    event = pending.events[0]
    assert pending.id == "historical-import"
    assert pending.source_text == "Family calendar email"
    assert pending.source_fingerprint == original["data"]["items"][0]["source_fingerprint"]
    assert event.draft == EventDraft.from_mapping(
        original["data"]["items"][0]["events"][0] if original["version"] == 1
        else original["data"]["items"][0]["events"][0]["draft"]
    )
    assert event.status == "write_uncertain"
    if original["version"] == 2:
        assert event.id == "historical-event"
        assert event.calendar_entity == "calendar.family"
        assert event.write_attempt == "attempt-before-upgrade"
    if name == "v0.7.1":
        assert event.routing_unresolved is False
    processor = AsyncMock()
    with pytest.raises(PendingImportApprovalUncertainError):
        await store.async_approve_event(pending.id, event.id, processor)
    processor.assert_not_awaited()
    assert store.is_source_duplicate("handled-message")
    assert store._seen_event_fingerprints == tuple(original["data"]["seen_event_fingerprints"])
    notifications = store.list_notifications()
    if name == "v0.7.1":
        assert [record.as_dict() for record in notifications] == original["data"]["notifications"]
    else:
        assert notifications == ()

    # Migration must be durable before any unrelated user action forces a save.
    migrated = json.loads(await hass.async_add_executor_job(path.read_text))
    assert migrated["version"] == 2
    assert migrated["data"]["items"][0]["events"][0]["id"] == event.id
    immediate_restart = PendingImportStore(hass)
    await immediate_restart.async_load()
    assert immediate_restart.get_event(pending.id, event.id) == event
    assert immediate_restart.list_notifications() == notifications

    # An unrelated public mutation must serialize all historical state.
    await store.async_add(source_text="New submission", events=[replace(event.draft, title="New appointment")])
    disk = json.loads(await hass.async_add_executor_job(path.read_text))
    assert disk["version"] == 2
    assert disk["data"]["seen_source_fingerprints"] == original["data"]["seen_source_fingerprints"]
    assert disk["data"]["seen_event_fingerprints"] == original["data"]["seen_event_fingerprints"]
    restarted = PendingImportStore(hass)
    await restarted.async_load()
    assert restarted.get_event(pending.id, event.id) == event
    assert restarted.get_event(pending.id, event.id).write_attempt == event.write_attempt
    assert restarted.is_source_duplicate("handled-message")
    assert restarted.list_notifications() == notifications
    with pytest.raises(PendingImportApprovalUncertainError):
        await restarted.async_approve_event(pending.id, event.id, processor)
    processor.assert_not_awaited()
