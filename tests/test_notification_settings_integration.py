"""Integration tests for opt-in notification settings contracts."""

from __future__ import annotations

import pytest

from tests.test_settings import entry, hass_for, FakeConnection, invoke


def test_notification_settings_legacy_defaults_and_corrupt_state_fail_closed():
    from custom_components.daylight_calendar_import.settings import (
        effective_notification_preferences, notification_preferences_patch,
    )
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES
    config_entry = entry()
    assert not effective_notification_preferences(config_entry).enabled
    config_entry.data[CONF_NOTIFICATION_PREFERENCES] = {
        "enabled": True, "target": "notify.phone", "classes": ["review_ready"],
    }
    assert effective_notification_preferences(config_entry).permits("review_ready")
    config_entry.options[CONF_NOTIFICATION_PREFERENCES] = {"enabled": "yes"}
    assert not effective_notification_preferences(config_entry).enabled
    assert notification_preferences_patch(config_entry, {
        "enabled": True, "target": "notify.phone", "classes": ["parse_failed"],
    }) == {CONF_NOTIFICATION_PREFERENCES: {
        "enabled": True, "target": "notify.phone", "classes": ["parse_failed"],
    }}


@pytest.mark.asyncio
async def test_notifications_ws_opt_in_persists_and_noop_skips_reload():
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    payload = {"enabled": True, "target": "notify.phone",
               "classes": ["review_ready", "parse_failed"]}
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 210, "entry_id": "entry-1", "notifications": payload,
    })
    assert not connection.errors
    assert hass.config_entries.reloads == ["entry-1"]
    assert config_entry.options[CONF_NOTIFICATION_PREFERENCES] == {
        "enabled": True, "target": "notify.phone",
        "classes": ["parse_failed", "review_ready"],
    }
    assert connection.results[-1][1]["notifications"] == config_entry.options[CONF_NOTIFICATION_PREFERENCES]
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 211, "entry_id": "entry-1", "notifications": payload,
    })
    assert hass.config_entries.reloads == ["entry-1"]
    assert len(connection.results) == 2


@pytest.mark.asyncio
async def test_notifications_ws_validation_rejects_bad_policy_without_writes():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    for index, invalid in enumerate([
        {"enabled": True}, {"classes": ["unknown"]},
        {"target": "notify.missing", "enabled": "yes"},
    ]):
        await invoke(settings_api.websocket_update_notifications, hass, connection, {
            "id": 220 + index, "entry_id": "entry-1", "notifications": invalid,
        })
    assert len(connection.errors) == 3
    assert all(code == "invalid_notifications" for _, code, _ in connection.errors)
    assert not hass.config_entries.updates
    assert not hass.config_entries.reloads
