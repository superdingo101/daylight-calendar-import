"""Tests for the AI parsing boundary."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.daylight_calendar_import import parser, providers
from custom_components.daylight_calendar_import.sources import SourceDocument, SourceKind, TextSourceAdapter


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
    result = parser.parse_ai_data({"events": [VALID]})
    assert len(result.events) == 1
    assert result.events[0].title == "Soccer Practice"
    assert result.warnings == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "AI Task result must be an object"),
        ({}, "AI Task result must contain an events list"),
    ],
)
def test_parse_ai_data_rejects_bad_shapes(value, message):
    with pytest.raises(parser.ParseResultError, match=message):
        parser.parse_ai_data(value)


def test_parse_ai_data_keeps_valid_events_and_reports_invalid_indices():
    result = parser.parse_ai_data({"events": [
        "bad", VALID, {**VALID, "title": ""}, {**VALID, "title": "Another event"},
    ]})
    assert [draft.title for draft in result.events] == ["Soccer Practice", "Another event"]
    assert result.warnings == [
        "event 0 must be an object",
        "event 2 is invalid: title must be a non-empty string",
    ]


def test_parse_ai_data_all_invalid_produces_no_drafts_with_warnings():
    result = parser.parse_ai_data({"events": [False]})
    assert result.events == []
    assert result.warnings == ["event 0 must be an object"]


async def test_async_parse_text(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [VALID]}))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
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
    outcome = await parser.async_parse_text(
        hass, text="  Soccer Thursday at 5:30  ", ai_task_entity="ai_task.test"
    )
    assert outcome.events[0].title == "Soccer Practice"
    assert outcome.warnings == []
    kwargs = generate.await_args.kwargs
    assert kwargs["entity_id"] == "ai_task.test"
    assert "Soccer Thursday at 5:30" in kwargs["instructions"]
    assert "Reference datetime: 2026-09-25T14:30:00-07:00" in kwargs["instructions"]
    assert "Home Assistant time zone: America/Los_Angeles" in kwargs["instructions"]
    assert kwargs["structure"] is parser.EVENTS_STRUCTURE


async def test_async_parse_text_rejects_empty_input():
    with pytest.raises(ValueError, match="text must not be empty"):
        await parser.async_parse_text(object(), text="  ", ai_task_entity="ai_task.test")


async def test_provider_rejects_source_without_text():
    source = TextSourceAdapter().create("some text")
    empty = SourceDocument(id=source.id, kind=SourceKind.IMAGE, received_at=source.received_at)
    with pytest.raises(ValueError, match="source must contain text"):
        await providers.AITaskParserProvider(object(), "ai_task.test").async_parse(
            empty, reference_datetime="2026-09-25T14:30:00-07:00", time_zone="America/Los_Angeles"
        )


async def test_source_boundary_passes_normalized_document_to_provider(monkeypatch):
    provider_parse = AsyncMock(return_value=parser.ParseOutcome([], []))
    monkeypatch.setattr(providers.AITaskParserProvider, "async_parse", provider_parse)
    source = TextSourceAdapter().create("source text", source_id="upstream-1")
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="UTC"))

    outcome = await parser.async_parse_source(hass, source=source, ai_task_entity="ai_task.test")

    assert outcome == parser.ParseOutcome([], [])
    assert provider_parse.await_args.args == (source,)
    assert provider_parse.await_args.kwargs["time_zone"] == "UTC"
    assert provider_parse.await_args.kwargs["reference_datetime"]


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
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
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
