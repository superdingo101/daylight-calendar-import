"""Consumed routing subjects never become provider recurrence evidence."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import import providers
from custom_components.daylight_calendar_import.models import UNSUPPORTED_RECURRENCE_WARNING
from custom_components.daylight_calendar_import.parser import PROMPT_TEMPLATE
from custom_components.daylight_calendar_import.source_routing import plan_source_routing
from custom_components.daylight_calendar_import.sources import SourceKind, TextSourceAdapter

REFERENCE = "2026-10-01T12:00:00+00:00"
BODY = "Join us on October 8, 2026, from 17:30 to 18:30 UTC."
EVENT = {
    "title": "Staff meeting",
    "start": "2026-10-08T17:30:00+00:00",
    "end": "2026-10-08T18:30:00+00:00",
    "all_day": False,
    "confidence": 0.95,
    "assumptions": [],
}


def route(source, aliases):
    return plan_source_routing(
        source,
        default_calendar="calendar.family",
        allowed_calendars={"calendar.family", "calendar.work"},
        aliases=aliases,
    )


@pytest.fixture
def provider(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(data={
        "unsupported_recurrence": False, "events": [EVENT],
    }))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    return providers.AITaskParserProvider(SimpleNamespace(data={}), "ai_task.test"), generate


@pytest.mark.parametrize(
    ("title", "body", "aliases", "destination", "requires_confirmation"),
    [
        ("Calendar: Weekly Meeting", BODY, {"weekly meeting": "calendar.work"}, "calendar.work", False),
        (" Calendar: WEEKLY   Meeting ", BODY, {"weekly meeting": "calendar.work"}, "calendar.work", False),
        ("Calendar: Weekly Meeting", BODY, {}, "calendar.family", True),
        ("Calendar: Weekly Meeting", "Calendar: family\n" + BODY, {"weekly meeting": "calendar.work"}, "calendar.family", True),
        ("Calendar:", BODY, {}, "calendar.family", True),
        ("Calendar: " + "Weekly Meeting " * 6, BODY, {}, "calendar.family", True),
    ],
)
async def test_consumed_subject_does_not_flag_one_off_body(
    provider, title, body, aliases, destination, requires_confirmation,
):
    adapter, generate = provider
    source = replace(TextSourceAdapter().create(body), kind=SourceKind.EMAIL, title=title)

    plan = route(source, aliases)
    outcome = await adapter.async_parse(
        plan.parser_source, reference_datetime=REFERENCE, time_zone="UTC",
    )

    assert plan.calendar_entity == destination
    assert plan.requires_confirmation is requires_confirmation
    assert plan.parser_source.title is None
    assert plan.parser_source.text == BODY
    assert source.title == title
    assert len(outcome.events) == 1
    assert outcome.warnings == []
    assert generate.await_args.kwargs["instructions"] == PROMPT_TEMPLATE.format(
        text=json.dumps(BODY, ensure_ascii=False), reference_datetime=REFERENCE, time_zone="UTC",
    )


async def test_consumed_subject_keeps_recurring_body_evidence(provider):
    adapter, generate = provider
    body = "Practice every Thursday at 17:30."
    source = replace(
        TextSourceAdapter().create(body), kind=SourceKind.EMAIL,
        title="Calendar: Weekly Meeting",
    )
    plan = route(source, {"weekly meeting": "calendar.work"})

    outcome = await adapter.async_parse(
        plan.parser_source, reference_datetime=REFERENCE, time_zone="UTC",
    )

    assert outcome.events == []
    assert outcome.warnings == [UNSUPPORTED_RECURRENCE_WARNING]
    assert body in generate.await_args.kwargs["instructions"]


@pytest.mark.parametrize("title", ["Weekly staff meeting", "Calendar: work\nWeekly meeting"])
@pytest.mark.parametrize("body", [BODY, "Calendar: work\n" + BODY])
async def test_ordinary_subject_keeps_title_evidence(provider, title, body):
    adapter, generate = provider
    source = replace(TextSourceAdapter().create(body), kind=SourceKind.EMAIL, title=title)
    plan = route(source, {"work": "calendar.work"})

    outcome = await adapter.async_parse(
        plan.parser_source, reference_datetime=REFERENCE, time_zone="UTC",
    )

    assert plan.parser_source.title == title
    assert plan.parser_source.text == BODY
    assert outcome.events == []
    assert outcome.warnings == [UNSUPPORTED_RECURRENCE_WARNING]
    assert generate.await_args.kwargs["instructions"] == PROMPT_TEMPLATE.format(
        text=json.dumps(f"Source title: {title}\n\nSource body:\n{BODY}", ensure_ascii=False),
        reference_datetime=REFERENCE, time_zone="UTC",
    )


def test_non_email_title_is_not_consumed_as_routing_control():
    source = replace(TextSourceAdapter().create(BODY), title="Calendar: Weekly Meeting")

    plan = route(source, {"weekly meeting": "calendar.work"})

    assert plan.parser_source == source
    assert plan.calendar_entity == "calendar.family"
