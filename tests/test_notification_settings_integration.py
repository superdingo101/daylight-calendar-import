"""Integration tests for opt-in notification settings contracts."""

from __future__ import annotations

import pytest

from tests.test_settings import entry, hass_for, FakeConnection, invoke
from custom_components.daylight_calendar_import import settings_api


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
               "classes": ["review_ready"]}
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 210, "entry_id": "entry-1", "notifications": payload,
        "expected_notifications": {"enabled": False, "classes": [], "target": None},
    })
    assert not connection.errors
    assert hass.config_entries.reloads == ["entry-1"]
    assert config_entry.options[CONF_NOTIFICATION_PREFERENCES] == {
        "enabled": True, "target": "notify.phone",
        "classes": ["review_ready"],
    }
    assert connection.results[-1][1]["notifications"] == config_entry.options[CONF_NOTIFICATION_PREFERENCES]
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 211, "entry_id": "entry-1", "notifications": payload,
        "expected_notifications": config_entry.options[CONF_NOTIFICATION_PREFERENCES],
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
        {"enabled": True, "target": "notify.phone", "classes": ["parse_failed"]},
        {"enabled": True, "target": "notify.phone", "classes": ["review_ready", "parse_failed"]},
        {"enabled": True, "target": "notify.phone"},
    ]):
        await invoke(settings_api.websocket_update_notifications, hass, connection, {
            "id": 220 + index, "entry_id": "entry-1", "notifications": invalid,
            "expected_notifications": {"enabled": False, "classes": [], "target": None},
        })
    assert len(connection.errors) == 6
    assert all(code == "invalid_notifications" for _, code, _ in connection.errors)
    assert not hass.config_entries.updates
    assert not hass.config_entries.reloads


@pytest.mark.asyncio
async def test_notifications_concurrent_policy_rejects_stale_snapshot():
    """Another administrator cannot overwrite an unseen accepted preference change."""
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES
    config_entry = entry(options={CONF_NOTIFICATION_PREFERENCES: {
        "enabled": True, "target": "notify.phone", "classes": ["review_ready"],
    }})
    hass = hass_for(config_entry)
    connection = FakeConnection()
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 300, "entry_id": "entry-1",
        "expected_notifications": {"enabled": False, "classes": [], "target": None},
        "notifications": {
            "enabled": True, "target": "notify.other", "classes": ["parse_failed"],
        },
    })
    assert connection.errors[0][1] == "notifications_changed"
    assert config_entry.options[CONF_NOTIFICATION_PREFERENCES]["target"] == "notify.phone"
    assert not hass.config_entries.reloads


@pytest.mark.asyncio
async def test_notifications_reject_invalid_snapshot_and_empty_opt_in():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    empty = {"enabled": False, "classes": [], "target": None}
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 301, "entry_id": "entry-1",
        "expected_notifications": {"enabled": "bad"},
        "notifications": empty,
    })
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 302, "entry_id": "entry-1",
        "expected_notifications": empty,
        "notifications": {"enabled": True, "target": "notify.phone", "classes": []},
    })
    assert [error[1] for error in connection.errors] == [
        "invalid_notifications", "invalid_notifications",
    ]
    assert not hass.config_entries.reloads


@pytest.mark.asyncio
async def test_only_supported_enabled_notifications_are_accepted():
    """Future-domain classes remain opt-out until a delivery adapter exists."""
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    empty = {"enabled": False, "classes": [], "target": None}
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 310, "entry_id": "entry-1",
        "expected_notifications": empty,
        "notifications": {"enabled": True, "target": "notify.phone",
                          "classes": ["review_ready"]},
    })
    assert connection.errors == []
    assert connection.results[-1][1]["notifications"]["classes"] == ["review_ready"]


@pytest.mark.asyncio
async def test_enable_review_ready_preserves_existing_dormant_classes():
    """Existing future selections cannot prevent opting into shipped delivery."""
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES

    old = {"enabled": False, "target": "notify.phone", "classes": ["parse_failed"]}
    config_entry = entry(options={CONF_NOTIFICATION_PREFERENCES: old})
    hass = hass_for(config_entry)
    connection = FakeConnection()
    requested = {"enabled": True, "target": "notify.phone",
                 "classes": ["parse_failed", "review_ready"]}
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 320, "entry_id": "entry-1",
        "expected_notifications": old,
        "notifications": requested,
    })
    assert connection.errors == []
    assert config_entry.options[CONF_NOTIFICATION_PREFERENCES] == requested
    assert hass.config_entries.reloads == ["entry-1"]


@pytest.mark.asyncio
async def test_enabled_policy_cannot_add_new_undelivered_classes():
    """Only dormant classes present in the optimistic baseline can survive."""
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES

    previous = {"enabled": False, "target": "notify.phone",
                "classes": ["parse_failed"]}
    config_entry = entry(options={CONF_NOTIFICATION_PREFERENCES: previous})
    hass = hass_for(config_entry)
    connection = FakeConnection()
    for index, classes in enumerate([
        ["parse_failed"],
        ["review_ready", "conflict_detected"],
        ["review_ready", "parse_failed", "conflict_detected"],
    ]):
        await invoke(settings_api.websocket_update_notifications, hass, connection, {
            "id": 330 + index, "entry_id": "entry-1",
            "expected_notifications": previous,
            "notifications": {"enabled": True, "target": "notify.phone",
                              "classes": classes},
        })
    assert len(connection.errors) == 3
    assert all(code == "invalid_notifications" for _, code, _ in connection.errors)
    assert config_entry.options[CONF_NOTIFICATION_PREFERENCES] == previous
    assert hass.config_entries.reloads == []


@pytest.mark.asyncio
async def test_disabling_notifications_keeps_saved_classes_without_delivery():
    """Disabling does not need a deliverable selection or erase dormant choices."""
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES

    previous = {"enabled": True, "target": "notify.phone",
                "classes": ["review_ready", "parse_failed"]}
    requested = {"enabled": False, "target": "notify.phone",
                 "classes": ["review_ready", "parse_failed"]}
    config_entry = entry(options={CONF_NOTIFICATION_PREFERENCES: previous})
    hass = hass_for(config_entry)
    connection = FakeConnection()
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 350, "entry_id": "entry-1",
        "expected_notifications": previous, "notifications": requested,
    })
    assert not connection.errors
    assert config_entry.options[CONF_NOTIFICATION_PREFERENCES] == {
        "enabled": False, "target": "notify.phone",
        "classes": ["parse_failed", "review_ready"],
    }
    assert hass.config_entries.reloads == ["entry-1"]


@pytest.mark.asyncio
async def test_disabled_policy_cannot_stage_unsupported_class_for_later_activation():
    """Reject the two-request bypass through a disabled policy."""
    from custom_components.daylight_calendar_import.const import CONF_NOTIFICATION_PREFERENCES

    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    previous = {"enabled": False, "target": None, "classes": []}
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 360, "entry_id": "entry-1",
        "expected_notifications": previous,
        "notifications": {"enabled": False, "target": "notify.phone",
                          "classes": ["parse_failed"]},
    })
    assert connection.errors[0][1] == "invalid_notifications"
    assert CONF_NOTIFICATION_PREFERENCES not in config_entry.options
    assert hass.config_entries.reloads == []

    # A disabled policy may still add the one class we can actually deliver.
    await invoke(settings_api.websocket_update_notifications, hass, connection, {
        "id": 361, "entry_id": "entry-1",
        "expected_notifications": previous,
        "notifications": {"enabled": False, "target": "notify.phone",
                          "classes": ["review_ready"]},
    })
    assert len(connection.errors) == 1
    assert config_entry.options[CONF_NOTIFICATION_PREFERENCES]["classes"] == ["review_ready"]
