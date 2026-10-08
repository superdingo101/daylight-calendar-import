"""Read-only HA calendar observation and pure matching integration."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.daylight_calendar_import.calendar_observation import (
    CalendarObservationError,
    async_classify_conflicts,
    async_observe_candidates,
    observation_window,
)
from custom_components.daylight_calendar_import.models import EventDraft


ZONE = ZoneInfo("America/Los_Angeles")


def draft(*, all_day=False):
    return EventDraft(
        title="Practice",
        start="2026-10-31" if all_day else "2026-10-08T17:30:00-07:00",
        end="2026-11-02" if all_day else "2026-10-08T18:30:00-07:00",
        all_day=all_day,
    )


def hass(response):
    services = SimpleNamespace(async_call=AsyncMock(return_value=response))
    return SimpleNamespace(services=services)


def existing(**changes):
    result = {"summary": "Practice", "start": "2026-10-08T17:30:00-07:00",
              "end": "2026-10-08T18:30:00-07:00"}
    result.update(changes)
    return result


def test_observation_window_retains_local_timezone_and_dst_boundaries():
    start, end = observation_window(draft(all_day=True), local_zone=ZONE)
    assert start.isoformat() == "2026-10-31T00:00:00-07:00"
    assert end.isoformat() == "2026-11-02T00:00:00-08:00"
    timed_start, timed_end = observation_window(draft(), local_zone=ZONE)
    assert timed_start.hour == 17
    assert timed_end.hour == 18


def test_window_rejects_reversed_dates():
    invalid = EventDraft("Practice", "2026-10-09", "2026-10-08", True)
    with pytest.raises(CalendarObservationError, match="Invalid event interval"):
        observation_window(invalid, local_zone=ZONE)


async def test_empty_observation_scope_never_queries_home_assistant():
    fake = hass(None)
    assert await async_observe_candidates(
        fake, draft(), observed_calendars=[], local_zone=ZONE
    ) == ()
    fake.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("entities", [["sensor.fake"], [None], ["calendar.family", 7]])
async def test_invalid_observation_scope_rejected_before_provider_call(entities):
    fake = hass(None)
    with pytest.raises(CalendarObservationError, match="Invalid observation calendar"):
        await async_observe_candidates(fake, draft(), observed_calendars=entities, local_zone=ZONE)
    fake.services.async_call.assert_not_awaited()


async def test_read_only_query_uses_requested_scope_context_and_local_interval():
    context = object()
    fake = hass({
        "calendar.work": {"events": [existing()]},
        "calendar.other": {"events": []},
    })
    found = await async_observe_candidates(
        fake, draft(),
        observed_calendars=["calendar.work", "calendar.work", "calendar.other"],
        local_zone=ZONE, context=context,
    )
    assert len(found) == 1
    assert found[0].calendar_entity == "calendar.work"
    assert found[0].title == "Practice"
    fake.services.async_call.assert_awaited_once_with(
        "calendar", "get_events",
        {"start_date_time": "2026-10-08T17:30:00-07:00",
         "end_date_time": "2026-10-08T18:30:00-07:00"},
        target={"entity_id": ["calendar.work", "calendar.other"]},
        blocking=True, return_response=True, context=context,
    )


@pytest.mark.parametrize("response", [
    None, [],
    {"calendar.work": None},
    {"calendar.work": {}},
    {"calendar.work": {"events": "not a list"}},
])
async def test_missing_or_invalid_provider_responses_are_not_empty_agendas(response):
    fake = hass(response)
    with pytest.raises(CalendarObservationError):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE
        )


@pytest.mark.parametrize("event", [
    None,
    {"start": "2026-10-08", "end": "2026-10-09"},
    {"summary": "Practice", "start": "2026-10-08", "end": 17},
    {"summary": "Practice", "start": "2026-10-08", "end": "2026-10-09T10:00:00+00:00"},
    {"summary": "Practice", "start": "bad-date-!", "end": "2026-10-09"},
])
async def test_malformed_calendar_items_raise_instead_of_disappearing(event):
    fake = hass({"calendar.work": {"events": [event]}})
    with pytest.raises(CalendarObservationError):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE
        )


async def test_all_day_calendar_response_maps_to_all_day_candidate():
    fake = hass({"calendar.work": {"events": [
        {"summary": "Vacation", "start": "2026-10-31", "end": "2026-11-02"}
    ]}})
    result = await async_observe_candidates(
        fake, draft(all_day=True), observed_calendars=["calendar.work"], local_zone=ZONE
    )
    assert result[0].all_day is True
    assert result[0].start == "2026-10-31"


async def test_match_classifies_duplicate_conflict_and_ignores_unrelated_events():
    fake = hass({"calendar.work": {"events": [
        existing(),
        existing(summary="Other appointment"),
        existing(summary="Later", start="2026-10-09T09:00:00-07:00",
                 end="2026-10-09T10:00:00-07:00"),
    ]}})
    matches = await async_classify_conflicts(
        fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE
    )
    assert [match.kind for match in matches] == ["exact_duplicate", "conflict"]
    assert all(match.calendar_entity == "calendar.work" for match in matches)


async def test_unavailable_calendar_failure_propagates():
    fake = hass(None)
    fake.services.async_call.side_effect = RuntimeError("provider timeout")
    with pytest.raises(RuntimeError, match="provider timeout"):
        await async_classify_conflicts(
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE,
        )
