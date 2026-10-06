"""Tests for the native Daylight settings model and WebSocket API."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from homeassistant.exceptions import Unauthorized

from custom_components.daylight_calendar_import import async_setup
from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    CONF_EMAIL_ENABLED,
    CONF_EMAIL_HOST,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
    DOMAIN,
)
from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapAuthenticationError,
    DirectImapConnectionError,
    DirectImapMailboxError,
)
from custom_components.daylight_calendar_import.settings import (
    SettingsValidationError,
    async_validate_email_options,
    effective_core_options,
    email_settings_snapshot,
    normalize_core_options,
    settings_snapshot,
)
from custom_components.daylight_calendar_import import settings_api


def entry(*, options=None, domain=DOMAIN):
    return SimpleNamespace(
        entry_id="entry-1",
        domain=domain,
        data={
            CONF_AI_TASK_ENTITY: "ai_task.initial",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        },
        options=options or {},
    )


class FakeConfigEntries:
    def __init__(self, config_entry):
        self.entry = config_entry
        self.reloads = []
        self.updates = []
        self.reload_result = True

    def async_get_entry(self, entry_id):
        return (
            self.entry
            if self.entry is not None and entry_id == self.entry.entry_id
            else None
        )

    def async_entries(self, domain):
        return (
            [self.entry]
            if self.entry is not None and self.entry.domain == domain
            else []
        )

    def async_update_entry(self, config_entry, *, options):
        assert config_entry is self.entry
        self.entry.options = options
        self.updates.append(options)

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)
        return self.reload_result


class FakeConnection:
    def __init__(self, *, admin=True):
        self.user = SimpleNamespace(is_admin=admin)
        self.results = []
        self.errors = []

    def send_result(self, message_id, result=None):
        self.results.append((message_id, result))

    def send_error(self, message_id, code, message):
        self.errors.append((message_id, code, message))


def hass_for(config_entry):
    return SimpleNamespace(
        config_entries=FakeConfigEntries(config_entry),
        data={},
    )


async def invoke(handler, hass, connection, msg):
    await inspect.unwrap(handler)(hass, connection, msg)


def test_effective_core_options_prefers_options_and_deduplicates():
    config_entry = entry(options={
        CONF_AI_TASK_ENTITY: "ai_task.updated",
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: [
            "calendar.work",
            "calendar.family",
            "calendar.work",
        ],
    })

    assert effective_core_options(config_entry) == (
        "ai_task.updated",
        "calendar.work",
        ["calendar.work", "calendar.family"],
    )
    assert effective_core_options(entry()) == (
        "ai_task.initial",
        "calendar.family",
        ["calendar.family", "calendar.work"],
    )


def test_normalize_core_options_validates_and_deduplicates():
    assert normalize_core_options(
        "ai_task.updated",
        "calendar.family",
        ["calendar.family", "calendar.work", "calendar.family"],
    ) == {
        CONF_AI_TASK_ENTITY: "ai_task.updated",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
    }

    with pytest.raises(SettingsValidationError, match="AI Task") as err:
        normalize_core_options(
            "sensor.not_ai",
            "calendar.family",
            ["calendar.family"],
        )
    assert err.value.code == "invalid_ai_task"

    with pytest.raises(SettingsValidationError, match="calendar entities") as err:
        normalize_core_options(
            "ai_task.updated",
            "sensor.not_calendar",
            ["sensor.not_calendar"],
        )
    assert err.value.code == "invalid_calendar"

    with pytest.raises(SettingsValidationError, match="included") as err:
        normalize_core_options(
            "ai_task.updated",
            "calendar.family",
            ["calendar.work"],
        )
    assert err.value.code == "default_not_allowed"


def test_email_and_complete_settings_snapshots_hide_secret():
    options = {
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 1993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "super-secret",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: False,
    }
    email = email_settings_snapshot(options)
    assert email == {
        "enabled": True,
        "host": "imap.example.test",
        "port": 1993,
        "username": "calendar@example.test",
        "password_configured": True,
        "mailbox": "Calendar",
        "verify_ssl": False,
    }
    assert "super-secret" not in str(email)

    default_email = email_settings_snapshot({})
    assert default_email["enabled"] is False
    assert default_email["port"] == 993
    assert default_email["mailbox"] == "INBOX"
    assert default_email["verify_ssl"] is True
    assert default_email["password_configured"] is False

    snapshot = settings_snapshot(entry(options=options))
    assert snapshot["entry_id"] == "entry-1"
    assert snapshot["email"] == email
    assert CONF_EMAIL_PASSWORD not in snapshot


@pytest.mark.asyncio
async def test_email_validation_normalizes_port_and_preserves_saved_password():
    current = {
        CONF_AI_TASK_ENTITY: "ai_task.current",
        CONF_EMAIL_PASSWORD: "saved",
        CONF_EMAIL_HOST: "old.example.test",
    }
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993.0,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: True,
    }
    validate = AsyncMock()
    with patch(
        "custom_components.daylight_calendar_import.settings.DirectImapSource",
        return_value=SimpleNamespace(async_validate=validate),
    ):
        options = await async_validate_email_options(
            "entry-1", current, user_input
        )

    assert options[CONF_EMAIL_PORT] == 993
    assert options[CONF_EMAIL_PASSWORD] == "saved"
    assert options[CONF_EMAIL_ENABLED] is True
    assert CONF_AI_TASK_ENTITY not in options
    validate.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (DirectImapAuthenticationError(), "invalid_auth"),
        (DirectImapMailboxError(), "invalid_mailbox"),
        (DirectImapConnectionError(), "cannot_connect"),
    ],
)
async def test_email_validation_maps_transport_errors(failure, code):
    validate = AsyncMock(side_effect=failure)
    with patch(
        "custom_components.daylight_calendar_import.settings.DirectImapSource",
        return_value=SimpleNamespace(async_validate=validate),
    ):
        with pytest.raises(SettingsValidationError) as err:
            await async_validate_email_options(
                "entry-1",
                {},
                {
                    CONF_EMAIL_HOST: "imap.example.test",
                    CONF_EMAIL_PORT: 993,
                    CONF_EMAIL_USERNAME: "user",
                    CONF_EMAIL_PASSWORD: "password",
                    CONF_EMAIL_MAILBOX: "INBOX",
                    CONF_EMAIL_VERIFY_SSL: True,
                },
            )
    assert err.value.code == code


@pytest.mark.asyncio
async def test_email_validation_maps_incomplete_and_invalid_settings():
    with pytest.raises(SettingsValidationError) as err:
        await async_validate_email_options("entry-1", {}, {})
    assert err.value.code == "invalid_email_config"

    with pytest.raises(SettingsValidationError) as err:
        await async_validate_email_options(
            "entry-1",
            {},
            {
                CONF_EMAIL_HOST: "imap.example.test",
                CONF_EMAIL_PORT: 993.5,
                CONF_EMAIL_USERNAME: "user",
                CONF_EMAIL_PASSWORD: "password",
                CONF_EMAIL_MAILBOX: "INBOX",
                CONF_EMAIL_VERIFY_SSL: True,
            },
        )
    assert err.value.code == "invalid_email_config"


@pytest.mark.asyncio
async def test_settings_get_is_secret_safe_and_reports_missing_entry():
    config_entry = entry(options={CONF_EMAIL_PASSWORD: "hidden"})
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_get_settings,
        hass,
        connection,
        {"id": 1, "type": settings_api.WS_GET_SETTINGS},
    )
    assert connection.errors == []
    assert connection.results[0][0] == 1
    assert connection.results[0][1]["email"]["password_configured"] is True
    assert "hidden" not in str(connection.results)

    hass.config_entries.entry = None
    await invoke(
        settings_api.websocket_get_settings,
        hass,
        connection,
        {"id": 2, "type": settings_api.WS_GET_SETTINGS},
    )
    assert connection.errors[-1][0:2] == (2, "entry_not_found")

    hass.config_entries.entry = entry(domain="other_domain")
    await invoke(
        settings_api.websocket_get_settings,
        hass,
        connection,
        {"id": 3, "type": settings_api.WS_GET_SETTINGS, "entry_id": "entry-1"},
    )
    assert connection.errors[-1][0:2] == (3, "entry_not_found")


@pytest.mark.asyncio
async def test_core_update_saves_reloads_and_returns_settings():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_core_settings,
        hass,
        connection,
        {
            "id": 4,
            "type": settings_api.WS_UPDATE_CORE_SETTINGS,
            "entry_id": "entry-1",
            CONF_AI_TASK_ENTITY: "ai_task.updated",
            CONF_CALENDAR_ENTITY: "calendar.work",
            CONF_CALENDAR_ENTITIES: ["calendar.work"],
        },
    )

    assert connection.errors == []
    assert hass.config_entries.reloads == ["entry-1"]
    assert config_entry.options == {
        CONF_AI_TASK_ENTITY: "ai_task.updated",
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
    }
    assert connection.results[-1][1]["calendar_entities"] == ["calendar.work"]


@pytest.mark.asyncio
async def test_core_update_can_patch_ai_without_reverting_calendars():
    config_entry = entry(options={
        CONF_AI_TASK_ENTITY: "ai_task.old",
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
    })
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_core_settings,
        hass,
        connection,
        {
            "id": 41,
            "type": settings_api.WS_UPDATE_CORE_SETTINGS,
            "entry_id": "entry-1",
            CONF_AI_TASK_ENTITY: "ai_task.updated",
        },
    )

    assert connection.errors == []
    assert config_entry.options == {
        CONF_AI_TASK_ENTITY: "ai_task.updated",
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
    }


@pytest.mark.asyncio
async def test_core_update_can_patch_calendars_without_reverting_ai():
    config_entry = entry(options={
        CONF_AI_TASK_ENTITY: "ai_task.current",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
    })
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_core_settings,
        hass,
        connection,
        {
            "id": 42,
            "type": settings_api.WS_UPDATE_CORE_SETTINGS,
            "entry_id": "entry-1",
            CONF_CALENDAR_ENTITY: "calendar.work",
            CONF_CALENDAR_ENTITIES: ["calendar.work"],
        },
    )

    assert connection.errors == []
    assert config_entry.options == {
        CONF_AI_TASK_ENTITY: "ai_task.current",
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
    }


@pytest.mark.asyncio
async def test_core_update_requires_at_least_one_setting():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_core_settings,
        hass,
        connection,
        {
            "id": 43,
            "type": settings_api.WS_UPDATE_CORE_SETTINGS,
            "entry_id": "entry-1",
        },
    )

    assert connection.errors == [
        (43, "invalid_settings", "At least one core setting must be provided.")
    ]
    assert hass.config_entries.updates == []
    assert hass.config_entries.reloads == []


@pytest.mark.asyncio
async def test_core_update_returns_validation_error_without_saving():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_core_settings,
        hass,
        connection,
        {
            "id": 5,
            "type": settings_api.WS_UPDATE_CORE_SETTINGS,
            "entry_id": "entry-1",
            CONF_AI_TASK_ENTITY: "ai_task.updated",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.work"],
        },
    )

    assert connection.errors[-1][0:2] == (5, "default_not_allowed")
    assert hass.config_entries.updates == []
    assert hass.config_entries.reloads == []


@pytest.mark.asyncio
async def test_email_update_can_disable_without_touching_saved_connection():
    config_entry = entry(options={
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PASSWORD: "saved",
    })
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_email_settings,
        hass,
        connection,
        {
            "id": 6,
            "type": settings_api.WS_UPDATE_EMAIL_SETTINGS,
            "entry_id": "entry-1",
            "enabled": False,
        },
    )

    assert config_entry.options[CONF_EMAIL_ENABLED] is False
    assert config_entry.options[CONF_EMAIL_HOST] == "imap.example.test"
    assert config_entry.options[CONF_EMAIL_PASSWORD] == "saved"
    assert hass.config_entries.reloads == ["entry-1"]
    assert connection.results[-1][1]["email"]["enabled"] is False


@pytest.mark.asyncio
async def test_email_update_validates_enabled_settings_and_hides_password():
    config_entry = entry(options={CONF_EMAIL_PASSWORD: "saved"})
    hass = hass_for(config_entry)
    connection = FakeConnection()
    validate = AsyncMock()
    with patch(
        "custom_components.daylight_calendar_import.settings.DirectImapSource",
        return_value=SimpleNamespace(async_validate=validate),
    ):
        await invoke(
            settings_api.websocket_update_email_settings,
            hass,
            connection,
            {
                "id": 7,
                "type": settings_api.WS_UPDATE_EMAIL_SETTINGS,
                "entry_id": "entry-1",
                "enabled": True,
                "host": "imap.example.test",
                "port": 993.0,
                "username": "calendar@example.test",
                "password": "",
                "mailbox": "Calendar",
                "verify_ssl": True,
            },
        )

    assert connection.errors == []
    assert config_entry.options[CONF_EMAIL_PASSWORD] == "saved"
    assert config_entry.options[CONF_EMAIL_HOST] == "imap.example.test"
    assert connection.results[-1][1]["email"]["password_configured"] is True
    assert "saved" not in str(connection.results)
    validate.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_email_update_returns_validation_error_without_reload():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    with patch(
        "custom_components.daylight_calendar_import.settings_api.async_validate_email_options",
        AsyncMock(
            side_effect=SettingsValidationError(
                "cannot_connect", "Could not connect."
            )
        ),
    ):
        await invoke(
            settings_api.websocket_update_email_settings,
            hass,
            connection,
            {
                "id": 8,
                "type": settings_api.WS_UPDATE_EMAIL_SETTINGS,
                "entry_id": "entry-1",
                "enabled": True,
            },
        )

    assert connection.errors == [(8, "cannot_connect", "Could not connect.")]
    assert hass.config_entries.updates == []
    assert hass.config_entries.reloads == []


@pytest.mark.asyncio
async def test_slow_email_validation_serializes_concurrent_core_update():
    config_entry = entry(options={
        CONF_AI_TASK_ENTITY: "ai_task.old",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family"],
        CONF_EMAIL_ENABLED: False,
        CONF_EMAIL_PASSWORD: "saved",
    })
    hass = hass_for(config_entry)
    email_connection = FakeConnection()
    core_connection = FakeConnection()
    validation_started = asyncio.Event()
    validation_continue = asyncio.Event()

    async def validate_email(_entry_id, current, user_input):
        assert current[CONF_AI_TASK_ENTITY] == "ai_task.old"
        validation_started.set()
        await validation_continue.wait()
        return {
            CONF_EMAIL_ENABLED: True,
            **user_input,
            CONF_EMAIL_PASSWORD: current[CONF_EMAIL_PASSWORD],
        }

    with patch(
        "custom_components.daylight_calendar_import.settings_api.async_validate_email_options",
        side_effect=validate_email,
    ):
        email_task = asyncio.create_task(
            invoke(
                settings_api.websocket_update_email_settings,
                hass,
                email_connection,
                {
                    "id": 81,
                    "type": settings_api.WS_UPDATE_EMAIL_SETTINGS,
                    "entry_id": "entry-1",
                    "enabled": True,
                    "host": "imap.example.test",
                    "port": 993.0,
                    "username": "calendar@example.test",
                    "password": "",
                    "mailbox": "INBOX",
                    "verify_ssl": True,
                },
            )
        )
        await validation_started.wait()

        core_task = asyncio.create_task(
            invoke(
                settings_api.websocket_update_core_settings,
                hass,
                core_connection,
                {
                    "id": 82,
                    "type": settings_api.WS_UPDATE_CORE_SETTINGS,
                    "entry_id": "entry-1",
                    CONF_AI_TASK_ENTITY: "ai_task.new",
                },
            )
        )
        await asyncio.sleep(0)
        assert not core_task.done()
        assert config_entry.options[CONF_AI_TASK_ENTITY] == "ai_task.old"

        validation_continue.set()
        await asyncio.gather(email_task, core_task)

    assert core_connection.errors == []
    assert email_connection.errors == []
    assert config_entry.options[CONF_AI_TASK_ENTITY] == "ai_task.new"
    assert config_entry.options[CONF_EMAIL_ENABLED] is True
    assert config_entry.options[CONF_EMAIL_HOST] == "imap.example.test"
    assert config_entry.options[CONF_EMAIL_PASSWORD] == "saved"
    assert hass.config_entries.reloads == ["entry-1", "entry-1"]


@pytest.mark.asyncio
async def test_concurrent_email_updates_reuse_latest_committed_password():
    config_entry = entry(options={
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "imap.old.test",
        CONF_EMAIL_PASSWORD: "old-secret",
    })
    hass = hass_for(config_entry)
    first_connection = FakeConnection()
    second_connection = FakeConnection()
    validation_started = asyncio.Event()
    validation_continue = asyncio.Event()
    validation_currents = []

    async def validate_email(_entry_id, current, user_input):
        validation_currents.append(dict(current))
        if len(validation_currents) == 1:
            validation_started.set()
            await validation_continue.wait()
        return {
            CONF_EMAIL_ENABLED: True,
            **user_input,
            CONF_EMAIL_PASSWORD: (
                user_input.get(CONF_EMAIL_PASSWORD)
                or current[CONF_EMAIL_PASSWORD]
            ),
        }

    with patch(
        "custom_components.daylight_calendar_import.settings_api.async_validate_email_options",
        side_effect=validate_email,
    ):
        first_task = asyncio.create_task(
            invoke(
                settings_api.websocket_update_email_settings,
                hass,
                first_connection,
                {
                    "id": 84,
                    "type": settings_api.WS_UPDATE_EMAIL_SETTINGS,
                    "entry_id": "entry-1",
                    "enabled": True,
                    "host": "imap.first.test",
                    "password": "new-secret",
                },
            )
        )
        await validation_started.wait()

        second_task = asyncio.create_task(
            invoke(
                settings_api.websocket_update_email_settings,
                hass,
                second_connection,
                {
                    "id": 85,
                    "type": settings_api.WS_UPDATE_EMAIL_SETTINGS,
                    "entry_id": "entry-1",
                    "enabled": True,
                    "host": "imap.second.test",
                    "password": "",
                },
            )
        )
        await asyncio.sleep(0)
        assert len(validation_currents) == 1
        assert not second_task.done()

        validation_continue.set()
        await asyncio.gather(first_task, second_task)

    assert first_connection.errors == []
    assert second_connection.errors == []
    assert validation_currents[1][CONF_EMAIL_PASSWORD] == "new-secret"
    assert config_entry.options[CONF_EMAIL_HOST] == "imap.second.test"
    assert config_entry.options[CONF_EMAIL_PASSWORD] == "new-secret"
    assert hass.config_entries.reloads == ["entry-1", "entry-1"]


@pytest.mark.asyncio
async def test_reload_failure_is_reported_after_options_are_saved():
    config_entry = entry()
    hass = hass_for(config_entry)
    hass.config_entries.reload_result = False
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_core_settings,
        hass,
        connection,
        {
            "id": 83,
            "type": settings_api.WS_UPDATE_CORE_SETTINGS,
            "entry_id": "entry-1",
            CONF_AI_TASK_ENTITY: "ai_task.updated",
        },
    )

    assert connection.results == []
    assert connection.errors == [
        (
            83,
            "reload_failed",
            "Settings were saved, but Daylight could not reload. "
            "Restart Home Assistant before relying on the new settings.",
        )
    ]
    assert config_entry.options[CONF_AI_TASK_ENTITY] == "ai_task.updated"
    assert hass.config_entries.reloads == ["entry-1"]


def test_settings_commands_require_admin():
    connection = FakeConnection(admin=False)
    with pytest.raises(Unauthorized):
        settings_api.websocket_get_settings(
            hass_for(entry()),
            connection,
            {"id": 9, "type": settings_api.WS_GET_SETTINGS},
        )


def test_register_settings_api_registers_all_commands(monkeypatch):
    register = Mock()
    monkeypatch.setattr(settings_api.websocket_api, "async_register_command", register)
    hass = SimpleNamespace()

    settings_api.async_register_settings_api(hass)

    assert [call.args for call in register.call_args_list] == [
        (hass, settings_api.websocket_get_settings),
        (hass, settings_api.websocket_update_core_settings),
        (hass, settings_api.websocket_update_email_settings),
    ]


@pytest.mark.asyncio
async def test_integration_setup_registers_settings_api(monkeypatch):
    register = Mock()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_register_settings_api",
        register,
    )
    hass = SimpleNamespace()

    assert await async_setup(hass, {}) is True
    register.assert_called_once_with(hass)
