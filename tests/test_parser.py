"""Tests for the AI parsing boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    drafts = await parser.async_parse_text(
        object(), text="  Soccer Thursday at 5:30  ", ai_task_entity="ai_task.test"
    )
    assert drafts[0].title == "Soccer Practice"
    kwargs = generate.await_args.kwargs
    assert kwargs["entity_id"] == "ai_task.test"
    assert "Soccer Thursday at 5:30" in kwargs["instructions"]
    assert kwargs["structure"] is parser.EVENTS_STRUCTURE


async def test_async_parse_text_rejects_empty_input():
    with pytest.raises(ValueError, match="text must not be empty"):
        await parser.async_parse_text(object(), text="  ", ai_task_entity="ai_task.test")
