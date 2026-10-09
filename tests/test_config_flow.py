"""Tests for the config flow."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest

from custom_components.daylight_calendar_import.config_flow import (
    DaylightCalendarImportConfigFlow,
    DaylightCalendarImportOptionsFlow,
)
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
)
from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapAuthenticationError,
    DirectImapConnectionError,
    DirectImapMailboxError,
)
from custom_components.daylight_calendar_import.email_runtime import (
    DEFAULT_EMAIL_MAILBOX,
    DEFAULT_EMAIL_PORT,
)
from custom_components.daylight_calendar_import.settings import settings_lock


def test_config_flow_version():
    assert DaylightCalendarImportConfigFlow.VERSION == 2


async def test_config_flow_shows_form():
    flow = DaylightCalendarImportConfigFlow()
    expected = {"type": "form"}

    with patch.object(flow, "async_show_form", return_value=expected) as show_form:
        result = await flow.async_step_user()

    assert result is expected
    kwargs = show_form.call_args.kwargs
    assert kwargs["step_id"] == "user"
    schema = kwargs["data_schema"]
    assert len(schema.schema) == 3

    selectors = list(schema.schema.values())
    ai_task_selector = selectors[0]
    assert ai_task_selector.config["filter"] == [
        {
            "domain": ["ai_task"],
            "supported_features": [1],
        }
    ]

    calendar_selector = selectors[1]
    assert calendar_selector.config["filter"] == [
        {
            "domain": ["calendar"],
            "supported_features": [1],
        }
    ]
    assert selectors[2].config["multiple"] is True
    assert selectors[2].config["filter"] == calendar_selector.config["filter"]


async def test_config_flow_creates_entry():
    flow = DaylightCalendarImportConfigFlow()
    user_input = {
        CONF_AI_TASK_ENTITY: "ai_task.test",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
    }
    expected = {"type": "create_entry"}

    with (
        patch.object(flow, "async_set_unique_id", AsyncMock()) as set_unique_id,
        patch.object(flow, "_abort_if_unique_id_configured", Mock()) as abort_if_configured,
        patch.object(flow, "async_create_entry", Mock(return_value=expected)) as create_entry,
    ):
        result = await flow.async_step_user(user_input)

    assert result is expected
    set_unique_id.assert_awaited_once_with("daylight_calendar_import")
    abort_if_configured.assert_called_once_with()
    create_entry.assert_called_once_with(
        title="Daylight Calendar Import",
        data=user_input,
    )


async def test_config_flow_schema_uses_expected_required_keys():
    flow = DaylightCalendarImportConfigFlow()
    expected = {"type": "form"}

    with patch.object(flow, "async_show_form", return_value=expected) as show_form:
        await flow.async_step_user()

    schema = show_form.call_args.kwargs["data_schema"]
    assert [marker.schema for marker in schema.schema] == [
        CONF_AI_TASK_ENTITY,
        CONF_CALENDAR_ENTITY,
        CONF_CALENDAR_ENTITIES,
    ]


async def test_config_flow_rejects_default_outside_allowed_calendars():
    flow = DaylightCalendarImportConfigFlow()
    expected = {"type": "form"}
    with patch.object(flow, "async_show_form", return_value=expected) as show_form:
        result = await flow.async_step_user({
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.work"],
        })
    assert result is expected
    assert show_form.call_args.kwargs["errors"] == {
        CONF_CALENDAR_ENTITY: "default_not_allowed"
    }



def _options_entry(options=None, data=None):
    return SimpleNamespace(
        entry_id="test-entry",
        options=options or {},
        data=data or {
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        },
    )


class _OptionsConfigEntries:
    def __init__(self):
        self.updates = []
        self.reloads = []
        self.reload_result = True

    def async_update_entry(self, entry, *, options):
        entry.options = dict(options)
        self.updates.append(dict(options))
        return True

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)
        return self.reload_result


def _options_hass():
    return SimpleNamespace(
        data={"daylight_calendar_import": {"test-entry": SimpleNamespace(
            _lock=asyncio.Lock(), list=lambda: (), accepting_services=True,
            active_submissions=set(), active_service_handlers=set(),
        )}},
        config_entries=_OptionsConfigEntries(),
    )


def test_config_flow_exposes_options_flow():
    flow = DaylightCalendarImportConfigFlow.async_get_options_flow(
        _options_entry()
    )
    assert isinstance(flow, DaylightCalendarImportOptionsFlow)


async def test_options_flow_can_disable_email_ingestion():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "create_entry"}
    entry = _options_entry({
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "app-secret",
        CONF_EMAIL_MAILBOX: "Calendar",
    })

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_create_entry",
            Mock(return_value=expected),
        ) as create_entry,
    ):
        result = await flow.async_step_init(
            {
                CONF_AI_TASK_ENTITY: "ai_task.new",
                CONF_CALENDAR_ENTITY: "calendar.new",
                CONF_CALENDAR_ENTITIES: ["calendar.new"],
                CONF_EMAIL_ENABLED: False,
            }
        )

    assert result is expected
    create_entry.assert_called_once_with(data=None)
    assert entry.options == {
        CONF_EMAIL_ENABLED: False,
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "app-secret",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_AI_TASK_ENTITY: "ai_task.new",
        CONF_CALENDAR_ENTITY: "calendar.new",
        CONF_CALENDAR_ENTITIES: ["calendar.new"],
    }
    assert flow.hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_routes_enabled_email_to_connection_step():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_step_email",
            AsyncMock(return_value=expected),
        ) as email_step,
    ):
        result = await flow.async_step_init(
            {
                CONF_AI_TASK_ENTITY: "ai_task.updated",
                CONF_CALENDAR_ENTITY: "calendar.family",
                CONF_CALENDAR_ENTITIES: ["calendar.family"],
                CONF_EMAIL_ENABLED: True,
            }
        )

    assert result is expected
    email_step.assert_awaited_once_with()
    assert flow._pending_core_patch == {
        CONF_AI_TASK_ENTITY: "ai_task.updated",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family"],
    }


async def test_options_flow_validates_and_saves_direct_imap():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    flow._pending_core_patch = {
        CONF_AI_TASK_ENTITY: "ai_task.new",
        CONF_CALENDAR_ENTITY: "calendar.new",
        CONF_CALENDAR_ENTITIES: ["calendar.new", "calendar.work"],
    }
    expected = {"type": "create_entry"}
    entry = _options_entry()
    validate = AsyncMock()
    source = SimpleNamespace(async_validate=validate)
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "app-secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(return_value=source),
        ) as source_factory,
        patch.object(
            flow,
            "async_create_entry",
            Mock(return_value=expected),
        ) as create_entry,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    validate.assert_awaited_once_with()
    settings = source_factory.call_args.args[0]
    assert settings.source_id == "test-entry:direct-imap"
    assert settings.sender_allowlist == ()
    create_entry.assert_called_once_with(data=None)
    assert entry.options == {
        CONF_AI_TASK_ENTITY: "ai_task.new",
        CONF_CALENDAR_ENTITY: "calendar.new",
        CONF_CALENDAR_ENTITIES: ["calendar.new", "calendar.work"],
        CONF_EMAIL_ENABLED: True,
        **user_input,
    }
    assert flow.hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_normalizes_number_selector_port_to_int():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "create_entry"}
    entry = _options_entry()
    validate = AsyncMock()
    source = SimpleNamespace(async_validate=validate)
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993.0,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "app-secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(return_value=source),
        ) as source_factory,
        patch.object(
            flow,
            "async_create_entry",
            Mock(return_value=expected),
        ) as create_entry,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    validate.assert_awaited_once_with()
    settings = source_factory.call_args.args[0]
    assert settings.port == 993
    assert type(settings.port) is int
    create_entry.assert_called_once_with(data=None)
    assert entry.options == {
        CONF_EMAIL_ENABLED: True,
        **user_input,
        CONF_EMAIL_PORT: 993,
    }
    assert flow.hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_rejects_fractional_number_selector_port():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993.5,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "app-secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(),
        ) as source_factory,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    source_factory.assert_not_called()
    assert show_form.call_args.kwargs["errors"] == {
        "base": "invalid_email_config"
    }


async def test_options_flow_reports_invalid_imap_credentials():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()
    source = SimpleNamespace(
        async_validate=AsyncMock(
            side_effect=DirectImapAuthenticationError("bad credentials")
        )
    )
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "wrong-secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(return_value=source),
        ),
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    assert show_form.call_args.kwargs["step_id"] == "email"
    assert show_form.call_args.kwargs["errors"] == {
        "base": "invalid_auth"
    }



async def test_options_flow_shows_default_calendar_and_disabled_email_suggestions():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "add_suggested_values_to_schema",
            Mock(return_value=object()),
        ) as add_suggested,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_init()

    assert result is expected
    assert show_form.call_args.kwargs["step_id"] == "init"
    schema = add_suggested.call_args.args[0]
    assert [marker.schema for marker in schema.schema] == [
        CONF_AI_TASK_ENTITY,
        CONF_CALENDAR_ENTITY,
        CONF_CALENDAR_ENTITIES,
        CONF_EMAIL_ENABLED,
    ]
    selectors = list(schema.schema.values())
    assert selectors[0].config["filter"] == [
        {"domain": ["ai_task"], "supported_features": [1]}
    ]
    assert selectors[2].config["multiple"] is True
    assert add_suggested.call_args.args[1] == {
        CONF_AI_TASK_ENTITY: "ai_task.test",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        CONF_EMAIL_ENABLED: False,
    }


async def test_options_flow_prefers_current_default_calendar_option():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry({
        CONF_AI_TASK_ENTITY: "ai_task.option",
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
        CONF_EMAIL_ENABLED: True,
    })

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "add_suggested_values_to_schema",
            Mock(return_value=object()),
        ) as add_suggested,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ),
    ):
        result = await flow.async_step_init()

    assert result is expected
    assert add_suggested.call_args.args[1] == {
        CONF_AI_TASK_ENTITY: "ai_task.option",
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
        CONF_EMAIL_ENABLED: True,
    }


async def test_options_flow_rejects_default_outside_allowed_calendars():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "add_suggested_values_to_schema",
            Mock(return_value=object()),
        ) as add_suggested,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        attempted = {
            CONF_AI_TASK_ENTITY: "ai_task.updated",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.work"],
            CONF_EMAIL_ENABLED: False,
        }
        result = await flow.async_step_init(attempted)

    assert result is expected
    assert show_form.call_args.kwargs["errors"] == {
        CONF_CALENDAR_ENTITY: "default_not_allowed"
    }
    assert add_suggested.call_args.args[1] == attempted


async def test_options_flow_can_remove_allowed_calendars_and_change_ai():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "create_entry"}
    entry = _options_entry({
        CONF_AI_TASK_ENTITY: "ai_task.old",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: [
            "calendar.family",
            "calendar.work",
            "calendar.stale",
        ],
        CONF_EMAIL_ENABLED: False,
    })

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_create_entry",
            Mock(return_value=expected),
        ) as create_entry,
    ):
        result = await flow.async_step_init({
            CONF_AI_TASK_ENTITY: "ai_task.new",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
            CONF_EMAIL_ENABLED: False,
        })

    assert result is expected
    create_entry.assert_called_once_with(data=None)
    assert entry.options == {
        CONF_AI_TASK_ENTITY: "ai_task.new",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        CONF_EMAIL_ENABLED: False,
    }
    assert flow.hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_shows_email_form_with_all_fields():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email()

    assert result is expected
    assert show_form.call_args.kwargs["step_id"] == "email"
    assert show_form.call_args.kwargs["errors"] == {}
    assert show_form.call_args.kwargs["description_placeholders"] == {
        "privacy_url": (
            "https://github.com/superdingo101/daylight-calendar-import/"
            "blob/main/docs/direct-imap.md#mailbox-privacy-and-access"
        )
    }
    schema = show_form.call_args.kwargs["data_schema"]
    fields = {
        marker.schema: (marker, field_selector)
        for marker, field_selector in schema.schema.items()
    }
    assert list(fields) == [
        CONF_EMAIL_HOST,
        CONF_EMAIL_PORT,
        CONF_EMAIL_USERNAME,
        CONF_EMAIL_PASSWORD,
        CONF_EMAIL_MAILBOX,
        CONF_EMAIL_VERIFY_SSL,
    ]

    port_marker, port_selector = fields[CONF_EMAIL_PORT]
    assert port_marker.default() == DEFAULT_EMAIL_PORT
    assert port_selector.config["min"] == 1
    assert port_selector.config["max"] == 65535
    assert port_selector.config["step"] == 1
    assert port_selector.config["mode"] == "box"

    password_marker, password_selector = fields[CONF_EMAIL_PASSWORD]
    assert password_marker.default() == ""
    assert password_selector.config["type"] == "password"

    mailbox_marker, _ = fields[CONF_EMAIL_MAILBOX]
    assert mailbox_marker.default() == DEFAULT_EMAIL_MAILBOX

    verify_ssl_marker, _ = fields[CONF_EMAIL_VERIFY_SSL]
    assert verify_ssl_marker.default() is True


@pytest.mark.parametrize(
    ("error", "expected_code"),
    (
        (
            DirectImapMailboxError("mailbox unavailable"),
            "invalid_mailbox",
        ),
        (
            DirectImapConnectionError("offline"),
            "cannot_connect",
        ),
    ),
)
async def test_options_flow_reports_direct_imap_validation_errors(
    error,
    expected_code,
):
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()
    source = SimpleNamespace(
        async_validate=AsyncMock(side_effect=error)
    )
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(return_value=source),
        ),
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    assert show_form.call_args.kwargs["errors"] == {
        "base": expected_code
    }


async def test_options_flow_reports_invalid_email_configuration():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 0,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    assert show_form.call_args.kwargs["errors"] == {
        "base": "invalid_email_config"
    }


@pytest.mark.parametrize(
    ("error", "expected_code"),
    (
        (DirectImapAuthenticationError("bad credentials"), "invalid_auth"),
        (DirectImapMailboxError("mailbox unavailable"), "invalid_mailbox"),
        (DirectImapConnectionError("offline"), "cannot_connect"),
    ),
)
async def test_options_flow_preserves_attempted_values_after_validation_error(
    error,
    expected_code,
):
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry({
        CONF_EMAIL_HOST: "old.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "old@example.test",
        CONF_EMAIL_PASSWORD: "old-secret",
        CONF_EMAIL_MAILBOX: "Old",
        CONF_EMAIL_VERIFY_SSL: True,
    })
    source = SimpleNamespace(async_validate=AsyncMock(side_effect=error))
    user_input = {
        CONF_EMAIL_HOST: "new.example.test",
        CONF_EMAIL_PORT: 1993,
        CONF_EMAIL_USERNAME: "new@example.test",
        CONF_EMAIL_PASSWORD: "new-secret",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: False,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(return_value=source),
        ),
        patch.object(
            flow,
            "add_suggested_values_to_schema",
            Mock(return_value=object()),
        ) as add_suggested,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    assert show_form.call_args.kwargs["errors"] == {"base": expected_code}
    assert add_suggested.call_args.args[1] == user_input


async def test_options_flow_preserves_attempted_values_after_invalid_configuration():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry({
        CONF_EMAIL_HOST: "old.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "old@example.test",
        CONF_EMAIL_PASSWORD: "old-secret",
        CONF_EMAIL_MAILBOX: "Old",
        CONF_EMAIL_VERIFY_SSL: True,
    })
    user_input = {
        CONF_EMAIL_HOST: "new.example.test",
        CONF_EMAIL_PORT: 0,
        CONF_EMAIL_USERNAME: "new@example.test",
        CONF_EMAIL_PASSWORD: "new-secret",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: False,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "add_suggested_values_to_schema",
            Mock(return_value=object()),
        ) as add_suggested,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    assert show_form.call_args.kwargs["errors"] == {
        "base": "invalid_email_config"
    }
    assert add_suggested.call_args.args[1] == user_input


async def test_options_flow_initial_email_form_uses_persisted_suggestions():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    current = {
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 1993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "saved-secret",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: False,
    }
    entry = _options_entry(current)

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "add_suggested_values_to_schema",
            Mock(return_value=object()),
        ) as add_suggested,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ),
    ):
        result = await flow.async_step_email()

    assert result is expected
    suggested = add_suggested.call_args.args[1]
    assert suggested == {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 1993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: False,
    }
    assert CONF_EMAIL_PASSWORD not in suggested



async def test_options_flow_rejects_blank_password_on_first_enable():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "form"}
    entry = _options_entry()
    user_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(),
        ) as source_factory,
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    source_factory.assert_not_called()
    assert show_form.call_args.kwargs["errors"] == {
        "base": "invalid_email_config"
    }


async def test_options_flow_reuses_saved_password_when_edit_form_is_blank():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "create_entry"}
    entry = _options_entry({
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "old.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "old@example.test",
        CONF_EMAIL_PASSWORD: "saved-secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    })
    validate = AsyncMock()
    source = SimpleNamespace(async_validate=validate)
    user_input = {
        CONF_EMAIL_HOST: "new.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "new@example.test",
        CONF_EMAIL_PASSWORD: "",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(return_value=source),
        ) as source_factory,
        patch.object(
            flow,
            "async_create_entry",
            Mock(return_value=expected),
        ) as create_entry,
    ):
        result = await flow.async_step_email(user_input)

    assert result is expected
    validate.assert_awaited_once_with()
    assert source_factory.call_args.args[0].password == "saved-secret"
    create_entry.assert_called_once_with(data=None)
    assert entry.options == {
        CONF_EMAIL_ENABLED: True,
        **user_input,
        CONF_EMAIL_PASSWORD: "saved-secret",
    }
    assert flow.hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_email_serializes_with_native_settings_updates():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    expected = {"type": "create_entry"}
    entry = _options_entry({
        CONF_AI_TASK_ENTITY: "ai_task.old",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family"],
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "imap.old.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "old@example.test",
        CONF_EMAIL_PASSWORD: "old-secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    })
    hass = _options_hass()
    flow.hass = hass
    source = SimpleNamespace(async_validate=AsyncMock())
    user_input = {
        CONF_EMAIL_HOST: "imap.new.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "new@example.test",
        CONF_EMAIL_PASSWORD: "",
        CONF_EMAIL_MAILBOX: "Calendar",
        CONF_EMAIL_VERIFY_SSL: True,
    }

    lock = settings_lock(hass, entry.entry_id)
    await lock.acquire()
    try:
        with (
            patch.object(
                DaylightCalendarImportOptionsFlow,
                "config_entry",
                new_callable=PropertyMock,
                return_value=entry,
            ),
            patch(
                "custom_components.daylight_calendar_import.settings.DirectImapSource",
                Mock(return_value=source),
            ) as source_factory,
            patch.object(
                flow,
                "async_create_entry",
                Mock(return_value=expected),
            ) as create_entry,
        ):
            save_task = asyncio.create_task(flow.async_step_email(user_input))
            await asyncio.sleep(0)
            source_factory.assert_not_called()

            entry.options = {
                **entry.options,
                CONF_AI_TASK_ENTITY: "ai_task.new",
                CONF_EMAIL_PASSWORD: "new-secret",
            }
            lock.release()

            result = await save_task
    finally:
        if lock.locked():
            lock.release()

    assert result is expected
    assert source_factory.call_args.args[0].password == "new-secret"
    create_entry.assert_called_once_with(data=None)
    assert entry.options[CONF_AI_TASK_ENTITY] == "ai_task.new"
    assert entry.options[CONF_EMAIL_PASSWORD] == "new-secret"
    assert hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_preserves_concurrent_core_change_after_init_step():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    entry = _options_entry({
        CONF_AI_TASK_ENTITY: "ai_task.old",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        CONF_EMAIL_ENABLED: False,
    })
    form = {"type": "form"}
    email_input = {
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }
    source = SimpleNamespace(async_validate=AsyncMock())

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_step_email",
            AsyncMock(return_value=form),
        ),
    ):
        result = await flow.async_step_init({
            CONF_AI_TASK_ENTITY: "ai_task.old",
            CONF_CALENDAR_ENTITY: "calendar.work",
            CONF_CALENDAR_ENTITIES: ["calendar.work"],
            CONF_EMAIL_ENABLED: True,
        })

    assert result is form
    assert flow._pending_core_patch == {
        CONF_CALENDAR_ENTITY: "calendar.work",
        CONF_CALENDAR_ENTITIES: ["calendar.work"],
    }

    entry.options = {
        **entry.options,
        CONF_AI_TASK_ENTITY: "ai_task.concurrent",
    }
    expected = {"type": "create_entry"}
    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.settings.DirectImapSource",
            Mock(return_value=source),
        ),
        patch.object(
            flow,
            "async_create_entry",
            Mock(return_value=expected),
        ) as create_entry,
    ):
        result = await flow.async_step_email(email_input)

    assert result is expected
    create_entry.assert_called_once_with(data=None)
    assert entry.options[CONF_AI_TASK_ENTITY] == "ai_task.concurrent"
    assert entry.options[CONF_CALENDAR_ENTITY] == "calendar.work"
    assert entry.options[CONF_CALENDAR_ENTITIES] == ["calendar.work"]
    assert flow.hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_uses_form_baseline_to_ignore_stale_unchanged_fields():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    entry = _options_entry({
        CONF_AI_TASK_ENTITY: "ai_task.old",
        CONF_CALENDAR_ENTITY: "calendar.family",
        CONF_CALENDAR_ENTITIES: ["calendar.family"],
        CONF_EMAIL_ENABLED: False,
    })
    form = {"type": "form"}

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=form),
        ),
    ):
        assert await flow.async_step_init() is form

    entry.options = {
        **entry.options,
        CONF_AI_TASK_ENTITY: "ai_task.concurrent",
    }
    expected = {"type": "create_entry"}
    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_create_entry",
            Mock(return_value=expected),
        ) as create_entry,
    ):
        result = await flow.async_step_init({
            CONF_AI_TASK_ENTITY: "ai_task.old",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family"],
            CONF_EMAIL_ENABLED: False,
        })

    assert result is expected
    create_entry.assert_called_once_with(data=None)
    assert entry.options[CONF_AI_TASK_ENTITY] == "ai_task.concurrent"
    assert flow.hass.config_entries.reloads == ["test-entry"]


async def test_options_flow_reports_reload_failure_after_disabled_save():
    flow = DaylightCalendarImportOptionsFlow()
    flow.hass = _options_hass()
    flow.hass.config_entries.reload_result = False
    entry = _options_entry({CONF_EMAIL_ENABLED: True})
    expected = {"type": "form"}

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch.object(
            flow,
            "async_show_form",
            Mock(return_value=expected),
        ) as show_form,
    ):
        result = await flow.async_step_init({
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
            CONF_EMAIL_ENABLED: False,
        })

    assert result is expected
    assert entry.options[CONF_EMAIL_ENABLED] is False
    assert flow.hass.config_entries.reloads == ["test-entry"]
    assert show_form.call_args.kwargs["errors"] == {"base": "reload_failed"}
