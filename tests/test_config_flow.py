"""Tests for the config flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

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
    CONF_EMAIL_MARK_SEEN,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_SENDER_ALLOWLIST,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
)
from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapAuthenticationError,
)


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



def _options_entry(options=None):
    return SimpleNamespace(
        entry_id="test-entry",
        options=options or {},
    )


def test_config_flow_exposes_options_flow():
    flow = DaylightCalendarImportConfigFlow.async_get_options_flow(
        _options_entry()
    )
    assert isinstance(flow, DaylightCalendarImportOptionsFlow)


async def test_options_flow_can_disable_email_ingestion():
    flow = DaylightCalendarImportOptionsFlow()
    expected = {"type": "create_entry"}
    entry = _options_entry({CONF_EMAIL_ENABLED: True})

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
            {CONF_EMAIL_ENABLED: False}
        )

    assert result is expected
    create_entry.assert_called_once_with(
        data={CONF_EMAIL_ENABLED: False}
    )


async def test_options_flow_routes_enabled_email_to_connection_step():
    flow = DaylightCalendarImportOptionsFlow()
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
            {CONF_EMAIL_ENABLED: True}
        )

    assert result is expected
    email_step.assert_awaited_once_with()


async def test_options_flow_validates_and_saves_direct_imap():
    flow = DaylightCalendarImportOptionsFlow()
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
        CONF_EMAIL_SENDER_ALLOWLIST: (
            "trusted@example.test\nother@example.test"
        ),
        CONF_EMAIL_MARK_SEEN: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.config_flow.DirectImapSource",
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
    assert settings.sender_allowlist == (
        "trusted@example.test",
        "other@example.test",
    )
    create_entry.assert_called_once_with(
        data={CONF_EMAIL_ENABLED: True, **user_input}
    )


async def test_options_flow_reports_invalid_imap_credentials():
    flow = DaylightCalendarImportOptionsFlow()
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
        CONF_EMAIL_SENDER_ALLOWLIST: "",
        CONF_EMAIL_MARK_SEEN: True,
    }

    with (
        patch.object(
            DaylightCalendarImportOptionsFlow,
            "config_entry",
            new_callable=PropertyMock,
            return_value=entry,
        ),
        patch(
            "custom_components.daylight_calendar_import.config_flow.DirectImapSource",
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
