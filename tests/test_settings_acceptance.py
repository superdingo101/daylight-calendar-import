"""Beta acceptance for native settings over HA's authenticated WebSocket API.

Use real entry setup, settings transactions, unload/reload and storage. Only the
external mailbox boundary is simulated; no API decorators are unwrapped.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY, CONF_CALENDAR_ENTITIES, CONF_CALENDAR_ENTITY,
    CONF_EMAIL_PASSWORD, DOMAIN,
)
from custom_components.daylight_calendar_import.settings_api import (
    WS_GET_SETTINGS, WS_UPDATE_CALENDAR_INTELLIGENCE, WS_UPDATE_CORE_SETTINGS,
    WS_UPDATE_EMAIL_SETTINGS, WS_UPDATE_NOTIFICATIONS,
)

SECRET = "mailbox-password-never-returned"
POLICY = {"enabled": True, "target": "notify.phone", "classes": ["review_ready"]}


async def call(client, command, **fields):
    await client.send_json_auto_id({"type": command, **fields})
    return await client.receive_json()


async def success(client, command, **fields):
    response = await call(client, command, **fields)
    assert response["success"], response
    assert SECRET not in json.dumps(response)
    assert CONF_EMAIL_PASSWORD not in json.dumps(response)
    return response["result"]


@pytest.fixture
async def settings_runtime(hass, monkeypatch):
    validate = AsyncMock()

    async def collect(_self):
        # Exercise the actual email runtime without contacting a mailbox.
        for envelope in ():
            yield envelope

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.direct_imap.DirectImapSource.async_validate",
        validate,
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.direct_imap.DirectImapSource.async_collect",
        collect,
    )
    entry = MockConfigEntry(domain=DOMAIN, data={
        CONF_AI_TASK_ENTITY: "ai_task.initial", CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return SimpleNamespace(entry=entry, validate=validate)


async def test_all_settings_tabs_survive_real_entry_reload(hass, hass_ws_client, settings_runtime):
    entry = settings_runtime.entry
    client = await hass_ws_client(hass)
    initial = await success(client, WS_GET_SETTINGS)
    assert initial["email"]["enabled"] is False
    assert initial["notifications"]["enabled"] is False
    old_store = hass.data[DOMAIN][entry.entry_id]

    await success(client, WS_UPDATE_CORE_SETTINGS, entry_id=entry.entry_id,
                  ai_task_entity="ai_task.updated")
    await success(client, WS_UPDATE_CORE_SETTINGS, entry_id=entry.entry_id,
                  calendar_entity="calendar.work", calendar_entities=["calendar.work", "calendar.family"])
    await success(client, WS_UPDATE_CALENDAR_INTELLIGENCE, entry_id=entry.entry_id,
                  calendar_aliases={" School ": "calendar.family"},
                  conflict_calendar_entities=["calendar.observation"])
    await success(client, WS_UPDATE_EMAIL_SETTINGS, entry_id=entry.entry_id, enabled=True,
                  host="imap.example.com", username="imports@example.com", password=SECRET,
                  sender_allowlist=["school@example.com"])
    final = await success(client, WS_UPDATE_NOTIFICATIONS, entry_id=entry.entry_id,
                          expected_notifications=initial["notifications"], notifications=POLICY)
    assert final == {
        "entry_id": entry.entry_id, "ai_task_entity": "ai_task.updated",
        "parser_provider": "ai_task",
        "calendar_entity": "calendar.work", "calendar_entities": ["calendar.work", "calendar.family"],
        "calendar_aliases": {"school": "calendar.family"},
        "conflict_calendar_entities": ["calendar.observation"],
        "email": {"enabled": True, "host": "imap.example.com", "port": 993,
                  "username": "imports@example.com", "password_configured": True,
                  "mailbox": "INBOX", "verify_ssl": True, "sender_allowlist": ["school@example.com"]},
        "notifications": POLICY,
    }
    assert entry.options[CONF_EMAIL_PASSWORD] == SECRET
    settings_runtime.validate.assert_awaited_once()
    assert hass.data[DOMAIN][entry.entry_id] is not old_store
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await success(client, WS_GET_SETTINGS) == final
    assert hass.data[DOMAIN][entry.entry_id].accepting_services is True


@pytest.mark.parametrize("command,fields", [
    (WS_GET_SETTINGS, {}),
    (WS_UPDATE_CORE_SETTINGS, {"ai_task_entity": "ai_task.other"}),
    (WS_UPDATE_EMAIL_SETTINGS, {"enabled": False}),
    (WS_UPDATE_CALENDAR_INTELLIGENCE, {"calendar_aliases": {}}),
    (WS_UPDATE_NOTIFICATIONS, {"expected_notifications": {}, "notifications": POLICY}),
])
async def test_every_settings_endpoint_requires_admin(
    hass, hass_ws_client, settings_runtime, command, fields,
):
    group = await hass.auth.async_get_group(GROUP_ID_USER)
    user = MockUser(groups=[group]).add_to_hass(hass)
    assert not user.is_admin
    token = await hass.auth.async_create_refresh_token(user, "https://example.com")
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(token))
    entry = settings_runtime.entry
    before = deepcopy(dict(entry.options))
    store = hass.data[DOMAIN][entry.entry_id]
    response = await call(client, command, **({} if command == WS_GET_SETTINGS else {"entry_id": entry.entry_id}), **fields)
    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"
    assert SECRET not in json.dumps(response)
    assert dict(entry.options) == before
    assert hass.data[DOMAIN][entry.entry_id] is store
    settings_runtime.validate.assert_not_awaited()


async def test_concurrent_tabs_merge_disjoint_changes_and_reject_stale_policy(
    hass, hass_ws_client, settings_runtime,
):
    entry = settings_runtime.entry
    email_client = await hass_ws_client(hass)
    general_client = await hass_ws_client(hass)
    initial = await success(general_client, WS_GET_SETTINGS)
    started, finish = asyncio.Event(), asyncio.Event()

    async def validate():
        started.set()
        await finish.wait()

    settings_runtime.validate.side_effect = validate
    email = asyncio.create_task(success(email_client, WS_UPDATE_EMAIL_SETTINGS,
        entry_id=entry.entry_id, enabled=True, host="imap.example.com",
        username="imports@example.com", password=SECRET))
    await asyncio.wait_for(started.wait(), timeout=5)
    general = asyncio.create_task(success(general_client, WS_UPDATE_CORE_SETTINGS,
        entry_id=entry.entry_id, ai_task_entity="ai_task.concurrent"))
    try:
        # The other settings transaction must wait while mailbox validation
        # owns the settings lock, rather than overwrite options from a snapshot.
        await asyncio.sleep(0)
        assert not general.done()
    finally:
        finish.set()
        await asyncio.gather(email, general)
    final = await success(general_client, WS_GET_SETTINGS)
    assert final["ai_task_entity"] == "ai_task.concurrent"
    assert final["email"]["enabled"] is True
    assert entry.options[CONF_EMAIL_PASSWORD] == SECRET
    await success(general_client, WS_UPDATE_NOTIFICATIONS, entry_id=entry.entry_id,
                  expected_notifications=initial["notifications"], notifications=POLICY)
    rejected = await call(email_client, WS_UPDATE_NOTIFICATIONS, entry_id=entry.entry_id,
                          expected_notifications=initial["notifications"],
                          notifications={"enabled": False, "target": None, "classes": []})
    assert rejected["success"] is False
    assert rejected["error"]["code"] == "notifications_changed"
    assert (await success(email_client, WS_GET_SETTINGS))["notifications"] == POLICY


@pytest.mark.parametrize("command,fields,code", [
    (WS_UPDATE_CORE_SETTINGS, {"ai_task_entity": "sensor.invalid"}, "invalid_ai_task"),
    (WS_UPDATE_CORE_SETTINGS, {"calendar_entity": "calendar.other"}, "default_not_allowed"),
    (WS_UPDATE_CALENDAR_INTELLIGENCE, {"calendar_aliases": {"School": "calendar.other"}}, "alias_target_not_allowed"),
    (WS_UPDATE_EMAIL_SETTINGS, {"enabled": True, "host": "imap.example.com"}, "invalid_email_config"),
    (WS_UPDATE_NOTIFICATIONS, {"expected_notifications": {}, "notifications": {"enabled": True}}, "invalid_notifications"),
])
async def test_invalid_tab_save_leaves_all_options_and_runtime_unchanged(
    hass, hass_ws_client, settings_runtime, command, fields, code,
):
    entry = settings_runtime.entry
    client = await hass_ws_client(hass)
    baseline = await success(client, WS_GET_SETTINGS)
    store = hass.data[DOMAIN][entry.entry_id]
    before = deepcopy(dict(entry.options))
    response = await call(client, command, entry_id=entry.entry_id, **fields)
    assert response["success"] is False
    assert response["error"]["code"] == code
    assert dict(entry.options) == before
    assert hass.data[DOMAIN][entry.entry_id] is store
    assert await success(client, WS_GET_SETTINGS) == baseline
    settings_runtime.validate.assert_not_awaited()


async def test_failed_mailbox_validation_redacts_remote_details_and_keeps_every_tab(
    hass, hass_ws_client, settings_runtime,
):
    from custom_components.daylight_calendar_import.direct_imap import DirectImapAuthenticationError

    entry = settings_runtime.entry
    client = await hass_ws_client(hass)
    await success(client, WS_UPDATE_CALENDAR_INTELLIGENCE, entry_id=entry.entry_id,
                  calendar_aliases={"School": "calendar.family"})
    baseline = await success(client, WS_GET_SETTINGS)
    before = deepcopy(dict(entry.options))
    store = hass.data[DOMAIN][entry.entry_id]
    settings_runtime.validate.side_effect = DirectImapAuthenticationError(f"Remote server echoed {SECRET}")
    refused = await call(client, WS_UPDATE_EMAIL_SETTINGS, entry_id=entry.entry_id,
                         enabled=True, host="imap.example.com", username="imports@example.com", password=SECRET)
    assert refused["success"] is False
    assert refused["error"]["code"] == "invalid_auth"
    assert SECRET not in json.dumps(refused)
    assert dict(entry.options) == before
    assert hass.data[DOMAIN][entry.entry_id] is store
    assert await success(client, WS_GET_SETTINGS) == baseline

    # Removing an alias destination from Calendars is also rejected atomically,
    # preserving the separate Routing tab's saved references.
    refused = await call(client, WS_UPDATE_CORE_SETTINGS, entry_id=entry.entry_id,
                         calendar_entity="calendar.work", calendar_entities=["calendar.work"])
    assert refused["success"] is False
    assert refused["error"]["code"] == "alias_target_not_allowed"
    assert dict(entry.options) == before
    assert await success(client, WS_GET_SETTINGS) == baseline


async def test_fresh_cloud_entry_loads_without_ai_task_or_calendar_and_preserves_byo_after_reload(hass, hass_ws_client):
    """Actual HA setup/settings reload must not assume local services exist."""
    from custom_components.daylight_calendar_import.hosted_settings_api import WS_HOSTED_GET

    configured = MockConfigEntry(domain=DOMAIN, version=2, data={
        "parser_provider": "daylight_cloud", CONF_AI_TASK_ENTITY: None,
        CONF_CALENDAR_ENTITY: None, CONF_CALENDAR_ENTITIES: [],
    })
    configured.add_to_hass(hass)
    assert await hass.config_entries.async_setup(configured.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    initial = await success(client, WS_GET_SETTINGS)
    assert initial["parser_provider"] == "daylight_cloud"
    assert initial[CONF_AI_TASK_ENTITY] is None
    assert initial[CONF_CALENDAR_ENTITY] is None
    assert initial[CONF_CALENDAR_ENTITIES] == []
    connection = await success(client, WS_HOSTED_GET, entry_id=configured.entry_id)
    assert connection["state"] == "not_configured"
    assert configured.options == {}

    denied = await call(client, WS_UPDATE_CORE_SETTINGS, entry_id=configured.entry_id,
                        parser_provider="ai_task")
    assert denied["success"] is False
    assert denied["error"]["code"] == "invalid_ai_task"
    assert configured.options == {}

    await success(client, WS_UPDATE_CORE_SETTINGS, entry_id=configured.entry_id,
                  calendar_entity="calendar.family", calendar_entities=["calendar.family"])
    await success(client, WS_UPDATE_CORE_SETTINGS, entry_id=configured.entry_id,
                  parser_provider="ai_task", ai_task_entity="ai_task.chosen")
    # Explicitly select Cloud again while retaining the independent local settings.
    final = await success(client, WS_UPDATE_CORE_SETTINGS, entry_id=configured.entry_id,
                          parser_provider="daylight_cloud")
    assert final[CONF_AI_TASK_ENTITY] == "ai_task.chosen"
    assert final[CONF_CALENDAR_ENTITY] == "calendar.family"
    assert await hass.config_entries.async_unload(configured.entry_id)
    assert await hass.config_entries.async_setup(configured.entry_id)
    await hass.async_block_till_done()
    assert await success(client, WS_GET_SETTINGS) == final
    assert hass.data[DOMAIN][configured.entry_id].accepting_services is True
