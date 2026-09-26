"""Tests for the config flow."""

from unittest.mock import AsyncMock, Mock, patch

from custom_components.daylight_calendar_import.config_flow import (
    DaylightCalendarImportConfigFlow,
)
from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
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
