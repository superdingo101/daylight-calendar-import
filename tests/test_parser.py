"""Tests for the AI parsing boundary."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.daylight_calendar_import import parser


VALID = {
    "title": "Soccer Practice",
    "start": "2026-10-08T17:30:00-07:00",
    "end": "2026-10-08T18:30:00-07:00",
    "all_day": False,
    "location": "Community Park",
    "description": "Bring water",
    "confidence": 0.95,
}


def test_parse_ai_data():
    drafts = parser.parse_ai_data({"events": [VALID]})
    assert len(drafts) == 1
    assert drafts[0].title == "Soccer Practice"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "AI Task result must be an object"),
        ({}, "AI Task result must contain an events list"),
        ({"events": ["bad"]}, "event 0 must be an object"),
        ({"events": [{**VALID, "title": ""}]}, "event 0 is invalid"),
    ],
)
def test_parse_ai_data_rejects_bad_shapes(value, message):
    with pytest.raises(parser.ParseResultError, match=message):
        parser.parse_ai_data(value)


async def test_async_parse_text(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [VALID]}))
    monkeypatch.setattr(parser.ai_task, "async_generate_data", generate)
    monkeypatch.setattr(
        parser.dt_util,
        "now",
        lambda *, time_zone: datetime(
            2026, 9, 25, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles")
        ),
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="America/Los_Angeles")
    )
    drafts = await parser.async_parse_text(
        hass, text="  Soccer Thursday at 5:30  ", ai_task_entity="ai_task.test"
    )
    assert drafts[0].title == "Soccer Practice"
    kwargs = generate.await_args.kwargs
    assert kwargs["entity_id"] == "ai_task.test"
    assert "Soccer Thursday at 5:30" in kwargs["instructions"]
    assert "Reference datetime: 2026-09-25T14:30:00-07:00" in kwargs["instructions"]
    assert "Home Assistant time zone: America/Los_Angeles" in kwargs["instructions"]
    assert kwargs["structure"] is parser.EVENTS_STRUCTURE


async def test_async_parse_text_rejects_empty_input():
    with pytest.raises(ValueError, match="text must not be empty"):
        await parser.async_parse_text(object(), text="  ", ai_task_entity="ai_task.test")


async def test_async_parse_text_preserves_ai_task_and_timezone_contract(monkeypatch):
    from unittest.mock import Mock

    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [VALID]}))
    local_tz = ZoneInfo("America/Los_Angeles")
    get_time_zone = Mock(return_value=local_tz)
    now = Mock(
        return_value=datetime(
            2026, 9, 25, 14, 30, tzinfo=local_tz
        )
    )
    monkeypatch.setattr(parser.ai_task, "async_generate_data", generate)
    monkeypatch.setattr(parser.dt_util, "get_time_zone", get_time_zone)
    monkeypatch.setattr(parser.dt_util, "now", now)

    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="America/Los_Angeles")
    )
    await parser.async_parse_text(
        hass, text="Soccer Thursday at 5:30", ai_task_entity="ai_task.test"
    )

    assert generate.await_args.args == (hass,)
    kwargs = generate.await_args.kwargs
    assert kwargs["task_name"] == parser.TASK_NAME
    assert kwargs["entity_id"] == "ai_task.test"
    get_time_zone.assert_called_once_with("America/Los_Angeles")
    now.assert_called_once_with(time_zone=local_tz)
