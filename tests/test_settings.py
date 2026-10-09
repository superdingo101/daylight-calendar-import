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
    core_option_patch,
    effective_core_options,
    email_settings_snapshot,
    normalize_core_options,
    settings_snapshot,
    settings_lock,
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
    runtime_store = SimpleNamespace(
        _lock=asyncio.Lock(), list=lambda: (),
        active_submissions=set(), active_service_handlers=set(),
        accepting_services=True, email_runtime=None,
    )
    return SimpleNamespace(
        config_entries=FakeConfigEntries(config_entry),
        data={DOMAIN: {config_entry.entry_id: runtime_store}},
    )


async def invoke(handler, hass, connection, msg):
    await inspect.unwrap(handler)(hass, connection, msg)


def test_settings_lock_is_namespaced_and_keyed_per_entry():
    hass = SimpleNamespace(data={})

    first = settings_lock(hass, "entry-1")
    same = settings_lock(hass, "entry-1")
    second = settings_lock(hass, "entry-2")

    assert first is same
    assert first is not second
    assert list(hass.data) == [f"{DOMAIN}_settings_locks"]
    assert hass.data[f"{DOMAIN}_settings_locks"] == {
        "entry-1": first,
        "entry-2": second,
    }


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


def test_core_option_patch_returns_only_changed_atomic_groups():
    baseline = (
        "ai_task.old",
        "calendar.family",
        ["calendar.family", "calendar.work"],
    )

    assert core_option_patch(
        baseline,
        {CONF_AI_TASK_ENTITY: "ai_task.new"},
    ) == {CONF_AI_TASK_ENTITY: "ai_task.new"}
    assert core_option_patch(
        baseline,
        {
            CONF_CALENDAR_ENTITY: "calendar.work",
            CONF_CALENDAR_ENTITIES: ["calendar.work"],
        },
    ) == {
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
    }
    assert core_option_patch(
        baseline,
        {
            CONF_AI_TASK_ENTITY: "ai_task.old",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        },
    ) == {}
    assert core_option_patch(
        baseline,
        {CONF_CALENDAR_ENTITY: "calendar.work"},
    ) == {
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
    }
    assert core_option_patch(
        baseline,
        {CONF_CALENDAR_ENTITIES: ["calendar.family"]},
    ) == {
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family"],
    }


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
    assert str(err.value) == "The selected AI Task entity is invalid."

    with pytest.raises(SettingsValidationError, match="calendar entities") as err:
        normalize_core_options(
            "ai_task.updated",
            "sensor.not_calendar",
            ["sensor.not_calendar"],
        )
    assert err.value.code == "invalid_calendar"
    assert str(err.value) == "Writable calendars must be calendar entities."

    with pytest.raises(SettingsValidationError) as err:
        normalize_core_options(
            "ai_task.updated",
            "sensor.not_calendar",
            ["calendar.family"],
        )
    assert err.value.code == "invalid_calendar"
    assert str(err.value) == "Writable calendars must be calendar entities."

    with pytest.raises(SettingsValidationError, match="included") as err:
        normalize_core_options(
            "ai_task.updated",
            "calendar.family",
            ["calendar.work"],
        )
    assert err.value.code == "default_not_allowed"
    assert str(err.value) == "The default calendar must be included in the allowed calendars."


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
    assert default_email == {
        "enabled": False,
        "host": "",
        "port": 993,
        "username": "",
        "password_configured": False,
        "mailbox": "INBOX",
        "verify_ssl": True,
    }

    snapshot = settings_snapshot(entry(options=options))
    assert snapshot == {
        "entry_id": "entry-1",
        "ai_task_entity": "ai_task.initial",
        "calendar_entity": "calendar.family",
        "calendar_entities": ["calendar.family", "calendar.work"],
        "calendar_aliases": {},
        "conflict_calendar_entities": ["calendar.family"],
        "email": email,
    }
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
    ("failure", "code", "message"),
    [
        (
            DirectImapAuthenticationError(),
            "invalid_auth",
            "The IMAP server rejected the supplied credentials.",
        ),
        (
            DirectImapMailboxError(),
            "invalid_mailbox",
            "The configured IMAP mailbox could not be selected.",
        ),
        (
            DirectImapConnectionError(),
            "cannot_connect",
            "Could not connect to the Direct IMAP mailbox.",
        ),
    ],
)
async def test_email_validation_maps_transport_errors(failure, code, message):
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
    assert str(err.value) == message


@pytest.mark.asyncio
async def test_email_validation_maps_incomplete_and_invalid_settings():
    with pytest.raises(SettingsValidationError) as err:
        await async_validate_email_options("entry-1", {}, {})
    assert err.value.code == "invalid_email_config"
    assert str(err.value) == "The Direct IMAP settings are incomplete or invalid."

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
    assert str(err.value) == "The Direct IMAP settings are incomplete or invalid."


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
    assert connection.errors[-1] == (
        2,
        "entry_not_found",
        "Daylight Calendar Import configuration was not found.",
    )

    hass.config_entries.entry = entry(domain="other_domain")
    await invoke(
        settings_api.websocket_get_settings,
        hass,
        connection,
        {"id": 3, "type": settings_api.WS_GET_SETTINGS, "entry_id": "entry-1"},
    )
    assert connection.errors[-1] == (
        3,
        "entry_not_found",
        "Daylight Calendar Import configuration was not found.",
    )


def test_settings_entry_lookup_honors_explicit_entry_id():
    first = entry()
    first.entry_id = "entry-1"
    second = entry()
    second.entry_id = "entry-2"

    class MultipleConfigEntries:
        def async_entries(self, domain):
            assert domain == DOMAIN
            return [first, second]

        def async_get_entry(self, entry_id):
            return {"entry-1": first, "entry-2": second}.get(entry_id)

    hass = SimpleNamespace(config_entries=MultipleConfigEntries())

    assert settings_api._entry_for_message(
        hass, {"entry_id": "entry-2"}
    ) is second


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
async def test_core_update_noop_returns_snapshot_without_reload():
    config_entry = entry(options={CONF_AI_TASK_ENTITY: "ai_task.current"})
    hass = hass_for(config_entry)
    connection = FakeConnection()

    await invoke(
        settings_api.websocket_update_core_settings,
        hass,
        connection,
        {
            "id": 40,
            "type": settings_api.WS_UPDATE_CORE_SETTINGS,
            "entry_id": "entry-1",
            CONF_AI_TASK_ENTITY: "ai_task.current",
        },
    )

    assert connection.errors == []
    assert connection.results[-1][1]["ai_task_entity"] == "ai_task.current"
    assert hass.config_entries.updates == []
    assert hass.config_entries.reloads == []


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
        (hass, settings_api.websocket_update_calendar_intelligence),
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


def test_calendar_intelligence_normalization_and_legacy_defaults():
    from custom_components.daylight_calendar_import.settings import (
        normalize_calendar_aliases, normalize_conflict_calendars, effective_calendar_intelligence,
    )
    assert effective_calendar_intelligence(entry()) == ({}, ["calendar.family"])
    assert normalize_calendar_aliases(
        {"  KIDS  ": "calendar.work", "Family": "calendar.family"},
        ["calendar.family", "calendar.work"],
    ) == {"kids": "calendar.work", "family": "calendar.family"}
    assert normalize_conflict_calendars(["calendar.other", "calendar.other"]) == ["calendar.other"]
    for aliases in (
        {"": "calendar.family"},
        {"Work": "calendar.work", " work ": "calendar.family"},
        {"Bad": "calendar.other"},
        {"Bad\nAlias": "calendar.family"},
    ):
        with pytest.raises(SettingsValidationError):
            normalize_calendar_aliases(aliases, ["calendar.family", "calendar.work"])
    with pytest.raises(SettingsValidationError):
        normalize_conflict_calendars(["sensor.not_calendar"])


@pytest.mark.asyncio
async def test_calendar_intelligence_admin_update_and_writable_scope():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    await invoke(
        settings_api.websocket_update_calendar_intelligence, hass, connection,
        {"id": 60, "entry_id": config_entry.entry_id,
         "type": settings_api.WS_UPDATE_CALENDAR_INTELLIGENCE,
         "calendar_aliases": {" Kids ": "calendar.work"},
         "conflict_calendar_entities": ["calendar.observation"]},
    )
    assert not connection.errors
    assert config_entry.options["calendar_aliases"] == {"kids": "calendar.work"}
    assert connection.results[-1][1]["conflict_calendar_entities"] == ["calendar.observation"]
    await invoke(
        settings_api.websocket_update_calendar_intelligence, hass, connection,
        {"id": 61, "entry_id": config_entry.entry_id,
         "type": settings_api.WS_UPDATE_CALENDAR_INTELLIGENCE,
         "calendar_aliases": {"bad": "calendar.observation"}},
    )
    assert connection.errors[-1][1] == "alias_target_not_allowed"


@pytest.mark.asyncio
async def test_calendar_alias_cannot_be_stranded_by_core_update():
    config_entry = entry(options={"calendar_aliases": {"work": "calendar.work"}})
    hass = hass_for(config_entry)
    connection = FakeConnection()
    await invoke(
        settings_api.websocket_update_core_settings, hass, connection,
        {"id": 62, "entry_id": config_entry.entry_id,
         "type": settings_api.WS_UPDATE_CORE_SETTINGS,
         CONF_CALENDAR_ENTITIES: ["calendar.family"]},
    )
    assert connection.errors[-1][1] == "alias_target_not_allowed"
    assert not hass.config_entries.updates


def test_calendar_intelligence_validation_and_noop_branches():
    from custom_components.daylight_calendar_import.settings import (
        normalize_calendar_aliases, normalize_conflict_calendars,
        calendar_intelligence_patch,
    )
    with pytest.raises(SettingsValidationError) as err:
        normalize_calendar_aliases([], ["calendar.family"])
    assert err.value.code == "invalid_aliases"
    with pytest.raises(SettingsValidationError):
        normalize_calendar_aliases({42: "calendar.family"}, ["calendar.family"])
    with pytest.raises(SettingsValidationError):
        normalize_calendar_aliases({"x" * 65: "calendar.family"}, ["calendar.family"])
    with pytest.raises(SettingsValidationError):
        normalize_calendar_aliases({"ok": 42}, ["calendar.family"])
    with pytest.raises(SettingsValidationError):
        normalize_conflict_calendars("calendar.family")
    with pytest.raises(SettingsValidationError):
        normalize_conflict_calendars([None])
    config_entry = entry()
    assert calendar_intelligence_patch(config_entry, {}) == {}
    assert calendar_intelligence_patch(
        config_entry, {"calendar_aliases": {}, "conflict_calendar_entities": ["calendar.family"]}
    ) == {}
    assert calendar_intelligence_patch(
        config_entry, {"calendar_aliases": {"home": "calendar.family"}}
    ) == {"calendar_aliases": {"home": "calendar.family"}}
    assert calendar_intelligence_patch(
        config_entry, {"conflict_calendar_entities": []}
    ) == {"conflict_calendar_entities": []}


@pytest.mark.asyncio
async def test_calendar_intelligence_rejects_empty_update_and_noops():
    config_entry = entry()
    hass = hass_for(config_entry)
    connection = FakeConnection()
    await invoke(
        settings_api.websocket_update_calendar_intelligence, hass, connection,
        {"id": 81, "type": settings_api.WS_UPDATE_CALENDAR_INTELLIGENCE,
         "entry_id": config_entry.entry_id},
    )
    assert connection.errors[-1][1] == "invalid_settings"
    await invoke(
        settings_api.websocket_update_calendar_intelligence, hass, connection,
        {"id": 82, "type": settings_api.WS_UPDATE_CALENDAR_INTELLIGENCE,
         "entry_id": config_entry.entry_id, "calendar_aliases": {}},
    )
    assert connection.results[-1][0] == 82
    assert hass.config_entries.reloads == []


@pytest.mark.asyncio
async def test_saving_invalid_default_does_not_change_options():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    with pytest.raises(SettingsValidationError, match="default calendar"):
        await async_save_option_patch(
            hass, config_entry, {CONF_CALENDAR_ENTITIES: ["calendar.work"]}
        )
    assert hass.config_entries.updates == []


@pytest.mark.parametrize(
    ("aliases", "expected_code", "expected_message"),
    [
        ([], "invalid_aliases", "Calendar aliases must be a mapping."),
        ({42: "calendar.family"}, "invalid_alias", "Calendar alias is invalid."),
        ({"Bad\nAlias": "calendar.family"}, "invalid_alias", "Calendar alias is invalid."),
        ({"Bad\rAlias": "calendar.family"}, "invalid_alias", "Calendar alias is invalid."),
        ({"   ": "calendar.family"}, "invalid_alias", "Calendar alias is invalid."),
        ({"A" * 65: "calendar.family"}, "invalid_alias", "Calendar alias is invalid."),
        (
            {"HOME": "calendar.family", " home ": "calendar.work"},
            "duplicate_alias",
            "Calendar aliases must be unique.",
        ),
        (
            {"home": "calendar.unlisted"},
            "alias_target_not_allowed",
            "Calendar alias targets must be writable calendars.",
        ),
        (
            {"home": 42},
            "alias_target_not_allowed",
            "Calendar alias targets must be writable calendars.",
        ),
    ],
)
def test_alias_validation_errors_are_stable_public_api_contracts(
    aliases, expected_code, expected_message,
):
    from custom_components.daylight_calendar_import.settings import normalize_calendar_aliases

    with pytest.raises(SettingsValidationError) as error:
        normalize_calendar_aliases(aliases, ["calendar.family", "calendar.work"])
    assert error.value.code == expected_code
    assert str(error.value) == expected_message


@pytest.mark.parametrize(
    "calendars",
    ["calendar.family", [None], [42], ["sensor.not_calendar"]],
)
def test_conflict_validation_returns_stable_error_contract(calendars):
    from custom_components.daylight_calendar_import.settings import normalize_conflict_calendars

    with pytest.raises(SettingsValidationError) as error:
        normalize_conflict_calendars(calendars)
    assert error.value.code == "invalid_conflict_calendars"
    assert str(error.value) == "Conflict calendars must be calendar entities."


def test_calendar_aliases_preserve_precise_unicode_and_length_contract():
    from custom_components.daylight_calendar_import.settings import normalize_calendar_aliases

    normalize = lambda aliases: normalize_calendar_aliases(aliases, ["calendar.family"])
    assert normalize({"  My   Kids ": "calendar.family"}) == {
        "my kids": "calendar.family",
    }
    assert normalize({"A" * 64: "calendar.family"}) == {
        "a" * 64: "calendar.family",
    }
    assert normalize({"ＫＩＤＳ": "calendar.family"}) == {"kids": "calendar.family"}
    assert normalize({"Cafe\u0301": "calendar.family"}) == {"café": "calendar.family"}
    with pytest.raises(SettingsValidationError) as err:
        normalize({"ＫＩＤＳ": "calendar.family", "kids": "calendar.family"})
    assert err.value.code == "duplicate_alias"
    assert str(err.value) == "Calendar aliases must be unique."


def test_persisted_calendar_intelligence_uses_correct_keys_and_read_only_scope():
    from custom_components.daylight_calendar_import.settings import (
        effective_calendar_intelligence,
        calendar_intelligence_patch,
    )
    configured = entry(options={
        "calendar_aliases": {" Kids ": "calendar.work"},
        "conflict_calendar_entities": [
            "calendar.external",
            "calendar.work",
            "calendar.external",
        ],
    })
    assert effective_calendar_intelligence(configured) == (
        {"kids": "calendar.work"},
        ["calendar.external", "calendar.work"],
    )
    assert settings_snapshot(configured)["calendar_aliases"] == {"kids": "calendar.work"}
    assert settings_snapshot(configured)["conflict_calendar_entities"] == [
        "calendar.external", "calendar.work",
    ]
    assert calendar_intelligence_patch(configured, {
        "calendar_aliases": {"KIDS": "calendar.work"},
        "conflict_calendar_entities": ["calendar.external", "calendar.work"],
    }) == {}
    assert calendar_intelligence_patch(configured, {
        "conflict_calendar_entities": ["calendar.work"],
    }) == {"conflict_calendar_entities": ["calendar.work"]}


@pytest.mark.asyncio
async def test_invalid_default_calendar_save_has_stable_code_and_message():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch

    config_entry = entry()
    hass = hass_for(config_entry)
    with pytest.raises(SettingsValidationError) as error:
        await async_save_option_patch(
            hass, config_entry,
            {CONF_CALENDAR_ENTITIES: ["calendar.work"]},
        )
    assert error.value.code == "default_not_allowed"
    assert str(error.value) == (
        "The default calendar must be included in the allowed calendars."
    )
    assert hass.config_entries.updates == []
    assert hass.config_entries.reloads == []


@pytest.mark.asyncio
async def test_calendar_change_cannot_strand_pending_explicit_destination():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    pending = SimpleNamespace(events=[SimpleNamespace(calendar_entity="calendar.work")])
    hass.data[DOMAIN] = {config_entry.entry_id: SimpleNamespace(
        _lock=asyncio.Lock(), list=lambda: (pending,),
    )}
    with pytest.raises(SettingsValidationError) as err:
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert err.value.code == "pending_destination_not_allowed"
    assert not hass.config_entries.updates
    assert not hass.config_entries.reloads


@pytest.mark.asyncio
async def test_calendar_change_rejects_implicit_default_retargeting():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    pending = SimpleNamespace(events=[SimpleNamespace(calendar_entity=None)])
    hass.data[DOMAIN] = {config_entry.entry_id: SimpleNamespace(
        _lock=asyncio.Lock(), list=lambda: (pending,),
    )}
    with pytest.raises(SettingsValidationError) as err:
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITY: "calendar.work",
        })
    assert err.value.code == "pending_default_would_change"
    assert not hass.config_entries.updates


@pytest.mark.asyncio
async def test_safe_calendar_scope_update_retains_pending_destinations():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    pending = SimpleNamespace(events=[SimpleNamespace(calendar_entity="calendar.family")])
    hass.data[DOMAIN] = {config_entry.entry_id: SimpleNamespace(
        _lock=asyncio.Lock(), list=lambda: (pending,),
    )}
    await async_save_option_patch(hass, config_entry, {
        CONF_CALENDAR_ENTITIES: ["calendar.family"],
    })
    assert config_entry.options[CONF_CALENDAR_ENTITIES] == ["calendar.family"]
    assert hass.config_entries.reloads == [config_entry.entry_id]


@pytest.mark.asyncio
async def test_calendar_changes_fail_closed_if_runtime_queue_cannot_be_checked():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    hass.data[DOMAIN].clear()  # Unloaded integration, durable state may remain.
    with pytest.raises(SettingsValidationError) as err:
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert err.value.code == "pending_store_unavailable"
    assert hass.config_entries.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("active_field", ["active_submissions", "active_service_handlers"])
async def test_calendar_scope_change_rejects_live_processing(active_field):
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    runtime_store = hass.data[DOMAIN][config_entry.entry_id]
    getattr(runtime_store, active_field).add(object())
    with pytest.raises(SettingsValidationError) as err:
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert err.value.code == "calendar_change_busy"
    assert runtime_store.accepting_services is True
    assert not hass.config_entries.updates


@pytest.mark.asyncio
async def test_calendar_scope_change_rejects_running_imap_poll():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    runtime_store = hass.data[DOMAIN][config_entry.entry_id]
    runtime_store.email_runtime = SimpleNamespace(_task=SimpleNamespace(done=lambda: False))
    with pytest.raises(SettingsValidationError) as err:
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert err.value.code == "calendar_change_busy"
    runtime_store.email_runtime._task = SimpleNamespace(done=lambda: True)
    await async_save_option_patch(hass, config_entry, {
        CONF_CALENDAR_ENTITIES: ["calendar.family"],
    })
    assert runtime_store.accepting_services is True


@pytest.mark.asyncio
async def test_calendar_admission_is_paused_during_reload_and_restored_on_failure():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    runtime_store = hass.data[DOMAIN][config_entry.entry_id]
    observed = []

    async def reload_while_blocked(_entry_id):
        observed.append(runtime_store.accepting_services)
        return False

    hass.config_entries.async_reload = reload_while_blocked
    with pytest.raises(SettingsValidationError) as err:
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert err.value.code == "reload_failed"
    assert observed == [False]
    assert runtime_store.accepting_services is False


@pytest.mark.asyncio
async def test_ai_only_setting_does_not_need_running_queue():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    hass.data[DOMAIN].clear()
    await async_save_option_patch(hass, config_entry, {
        CONF_AI_TASK_ENTITY: "ai_task.new",
    })
    assert config_entry.options[CONF_AI_TASK_ENTITY] == "ai_task.new"


@pytest.mark.asyncio
async def test_calendar_scope_changes_refuse_unloading_runtime():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    store = hass.data[DOMAIN][config_entry.entry_id]
    store.accepting_services = False
    with pytest.raises(SettingsValidationError) as err:
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert err.value.code == "pending_store_unavailable"
    assert not hass.config_entries.updates


@pytest.mark.asyncio
async def test_failed_calendar_reload_exception_leaves_old_runtime_closed():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    store = hass.data[DOMAIN][config_entry.entry_id]

    async def broken_reload(_entry_id):
        assert store.accepting_services is False
        raise RuntimeError("reload exploded")

    hass.config_entries.async_reload = broken_reload
    with pytest.raises(RuntimeError, match="reload exploded"):
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert store.accepting_services is False
    assert config_entry.options[CONF_CALENDAR_ENTITIES] == ["calendar.family"]

@pytest.mark.asyncio
async def test_calendar_option_save_failure_restores_original_ingress():
    from custom_components.daylight_calendar_import.settings import async_save_option_patch
    config_entry = entry()
    hass = hass_for(config_entry)
    store = hass.data[DOMAIN][config_entry.entry_id]

    def fail_before_persist(_entry, *, options):
        assert store.accepting_services is False
        raise RuntimeError("cannot persist options")

    hass.config_entries.async_update_entry = fail_before_persist
    with pytest.raises(RuntimeError, match="cannot persist"):
        await async_save_option_patch(hass, config_entry, {
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
        })
    assert store.accepting_services is True
    assert config_entry.options == {}
    assert hass.config_entries.reloads == []


def test_conflict_calendar_limit_leaves_room_for_any_writable_destination():
    from custom_components.daylight_calendar_import.settings import (
        calendar_intelligence_patch, effective_calendar_intelligence,
    )
    config_entry = entry()
    selected = [f"calendar.observed_{i}" for i in range(16)]
    with pytest.raises(SettingsValidationError) as err:
        calendar_intelligence_patch(config_entry, {
            "conflict_calendar_entities": selected,
        })
    assert err.value.code == "conflict_calendar_limit"
    assert calendar_intelligence_patch(config_entry, {
        "conflict_calendar_entities": selected[:15],
    }) == {"conflict_calendar_entities": selected[:15]}
    # Existing settings remain readable so admins can reduce a previously
    # saved, overlong scope instead of breaking setup during an upgrade.
    legacy = entry(options={"conflict_calendar_entities": selected})
    assert effective_calendar_intelligence(legacy)[1] == selected
    assert calendar_intelligence_patch(legacy, {
        "conflict_calendar_entities": selected[:15],
    }) == {"conflict_calendar_entities": selected[:15]}
