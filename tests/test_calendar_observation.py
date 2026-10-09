"""Read-only HA calendar observation and pure matching integration."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from zoneinfo import ZoneInfo

import pytest
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized

from custom_components.daylight_calendar_import.calendar_observation import (
    CalendarObservationError,
    _candidate,
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


READ_CONTEXT = Context(user_id="reviewer")


def hass(response):
    """Fake the read-only entity API, not the control-only HA service."""
    providers = {}
    for calendar_id, record in (response if isinstance(response, dict) else {}).items():
        if record is None:
            continue
        values = record.get("events") if isinstance(record, dict) else record
        if isinstance(values, list):
            values = [SimpleNamespace(**value) if isinstance(value, dict) else value
                      for value in values]
        providers[calendar_id] = SimpleNamespace(async_get_events=AsyncMock(
            return_value=values,
        ))
    component = SimpleNamespace(get_entity=Mock(side_effect=providers.get))
    user = SimpleNamespace(permissions=SimpleNamespace(check_entity=Mock(return_value=True)))
    auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    fake = SimpleNamespace(
        auth=auth, data={DATA_COMPONENT: component},
        services=SimpleNamespace(async_call=AsyncMock(
            side_effect=AssertionError("get_events service requires control permission"),
        )),
        providers=providers, user=user,
    )
    return fake


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
        fake, draft(), observed_calendars=[], local_zone=ZONE, context=READ_CONTEXT
    ) == ()
    fake.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("entities", [["sensor.fake"], [None], ["calendar.family", 7], [["calendar.work"]]])
async def test_invalid_observation_scope_rejected_before_provider_call(entities):
    fake = hass(None)
    with pytest.raises(CalendarObservationError, match="Invalid observation calendar"):
        await async_observe_candidates(fake, draft(), observed_calendars=entities, local_zone=ZONE, context=READ_CONTEXT)
    fake.services.async_call.assert_not_awaited()


async def test_read_only_query_uses_requested_scope_context_and_local_interval():
    context = READ_CONTEXT
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
    fake.services.async_call.assert_not_awaited()
    fake.auth.async_get_user.assert_awaited_once_with("reviewer")
    assert fake.user.permissions.check_entity.call_args_list == [
        call("calendar.work", POLICY_READ), call("calendar.other", POLICY_READ)
    ]
    start = datetime.fromisoformat("2026-10-08T17:30:00-07:00").astimezone(ZONE)
    end = datetime.fromisoformat("2026-10-08T18:30:00-07:00").astimezone(ZONE)
    fake.providers["calendar.work"].async_get_events.assert_awaited_once_with(
        fake, start, end
    )
    fake.providers["calendar.other"].async_get_events.assert_awaited_once_with(
        fake, start, end
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
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT
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
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT
        )


async def test_all_day_calendar_response_maps_to_all_day_candidate():
    fake = hass({"calendar.work": {"events": [
        {"summary": "Vacation", "start": "2026-10-31", "end": "2026-11-02"}
    ]}})
    result = await async_observe_candidates(
        fake, draft(all_day=True), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT
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
        fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT
    )
    assert [match.kind for match in matches] == ["exact_duplicate", "conflict"]
    assert all(match.calendar_entity == "calendar.work" for match in matches)


async def test_unavailable_calendar_failure_propagates():
    fake = hass(None)
    fake.data[DATA_COMPONENT].get_entity.side_effect = lambda _entity: SimpleNamespace(
        async_get_events=AsyncMock(side_effect=RuntimeError("provider timeout")),
    )
    with pytest.raises(RuntimeError, match="provider timeout"):
        await async_classify_conflicts(
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT,
        )


def test_observation_window_accepts_elapsed_time_across_fall_dst_fold():
    # 01:30 PDT precedes 01:15 PST by 45 minutes, despite reversed wall time.
    folded = EventDraft(
        "DST practice", "2026-11-01T01:30:00-07:00",
        "2026-11-01T01:15:00-08:00", False,
    )
    start, end = observation_window(folded, local_zone=ZONE)
    assert start.isoformat() == "2026-11-01T01:30:00-07:00"
    assert end.isoformat() == "2026-11-01T01:15:00-08:00"


def test_observation_window_rejects_naive_timed_drafts():
    naive = EventDraft(
        "Practice", "2026-10-08T17:30:00", "2026-10-08T18:30:00-07:00", False,
    )
    with pytest.raises(CalendarObservationError, match="require UTC offsets"):
        observation_window(naive, local_zone=ZONE)
    naive_end = EventDraft(
        "Practice", "2026-10-08T17:30:00-07:00", "2026-10-08T18:30:00", False,
    )
    with pytest.raises(CalendarObservationError, match="require UTC offsets"):
        observation_window(naive_end, local_zone=ZONE)


@pytest.mark.parametrize(("start", "end"), [
    ("nonsense-time", "2026-10-08T18:00:00-07:00"),
    ("2026-10-08T17:00:00-07:00", "bad-end"),
    ("2026-10-08T17:00:00", "2026-10-08T18:00:00-07:00"),
    ("2026-10-08T17:00:00-07:00", "2026-10-08T18:00:00"),
    ("2026-10-08T19:00:00-07:00", "2026-10-08T18:00:00-07:00"),
])
async def test_invalid_timed_provider_events_are_rejected_at_observation_boundary(
    start, end,
):
    fake = hass({"calendar.work": {"events": [existing(start=start, end=end)]}})
    with pytest.raises(CalendarObservationError, match="timed calendar interval|timezone offsets"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT,
        )


@pytest.mark.parametrize(("start", "end"), [
    ("2026-10-09", "2026-10-08"),
    ("2026-10-08", "2026-10-08"),
])
async def test_invalid_all_day_provider_duration_is_rejected(start, end):
    fake = hass({"calendar.work": {"events": [
        existing(start=start, end=end)
    ]}})
    with pytest.raises(CalendarObservationError, match="all-day calendar interval"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT,
        )


async def test_zero_duration_timed_provider_event_is_observable_but_not_a_conflict():
    # HA 2026.7.4 permits zero-duration provider events, e.g. from Google.
    point = existing(
        summary="Practice",
        start="2026-10-08T17:30:00-07:00",
        end="2026-10-09T00:30:00+00:00",
    )
    fake = hass({"calendar.work": {"events": [
        point,
        existing(summary="Real conflict"),
        existing(summary="Outside", start="2026-10-09T09:00:00-07:00",
                 end="2026-10-09T10:00:00-07:00"),
    ]}})
    observed = await async_observe_candidates(
        fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT,
    )
    assert len(observed) == 3
    assert observed[0].start == point["start"]
    assert observed[0].end == point["end"]
    matches = await async_classify_conflicts(
        fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT,
    )
    assert [(m.kind, m.existing_title) for m in matches] == [
        ("conflict", "Real conflict")
    ]


def test_invalid_timed_draft_is_rejected_as_observation_error():
    broken = EventDraft(
        "Broken", "not-a-datetime", "2026-10-08T18:30:00-07:00", False,
    )
    with pytest.raises(CalendarObservationError, match="Invalid timed observation"):
        observation_window(broken, local_zone=ZONE)
    broken_end = EventDraft(
        "Broken", "2026-10-08T17:30:00-07:00", "not-a-datetime", False,
    )
    with pytest.raises(CalendarObservationError, match="Invalid timed observation"):
        observation_window(broken_end, local_zone=ZONE)


async def test_all_day_provider_events_still_reach_classifier():
    fake = hass({"calendar.work": {"events": [
        {"summary": "Practice", "start": "2026-10-31", "end": "2026-11-02"},
        {"summary": "Next holiday", "start": "2026-11-02", "end": "2026-11-03"},
    ]}})
    matches = await async_classify_conflicts(
        fake, draft(all_day=True),
        observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT,
    )
    assert [(match.kind, match.existing_title) for match in matches] == [
        ("exact_duplicate", "Practice")
    ]


async def test_valid_timed_provider_interval_crosses_repeated_dst_hour():
    fake = hass({"calendar.work": {"events": [
        existing(start="2026-11-01T01:30:00-07:00",
                 end="2026-11-01T01:15:00-08:00"),
    ]}})
    observed = await async_observe_candidates(
        fake, draft(), observed_calendars=["calendar.work"], local_zone=ZONE, context=READ_CONTEXT,
    )
    assert len(observed) == 1
    assert observed[0].start == "2026-11-01T01:30:00-07:00"



async def test_read_only_user_can_observe_but_control_service_is_never_called():
    fake = hass({"calendar.school": {"events": [existing()]}})
    results = await async_observe_candidates(
        fake, draft(), observed_calendars=["calendar.school"], local_zone=ZONE,
        context=READ_CONTEXT,
    )
    assert len(results) == 1
    fake.user.permissions.check_entity.assert_called_once_with(
        "calendar.school", POLICY_READ
    )
    fake.services.async_call.assert_not_awaited()


async def test_user_without_calendar_read_permission_is_denied_before_provider_access():
    fake = hass({"calendar.school": {"events": [existing()]}})
    fake.user.permissions.check_entity.return_value = False
    with pytest.raises(Unauthorized):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school"], local_zone=ZONE,
            context=READ_CONTEXT,
        )
    fake.providers["calendar.school"].async_get_events.assert_not_awaited()


async def test_no_user_or_missing_user_is_denied_before_provider_access():
    fake = hass({"calendar.school": {"events": [existing()]}})
    with pytest.raises(Unauthorized):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school"], local_zone=ZONE,
        )
    with pytest.raises(Unauthorized):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school"], local_zone=ZONE,
            context=Context(),
        )
    fake.auth.async_get_user.return_value = None
    with pytest.raises(Unauthorized):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school"], local_zone=ZONE,
            context=READ_CONTEXT,
        )
    fake.providers["calendar.school"].async_get_events.assert_not_awaited()


async def test_read_permission_checks_all_calendars_before_fetching_any():
    fake = hass({
        "calendar.school": {"events": [existing()]},
        "calendar.private": {"events": [existing(summary="Secret")]},
    })
    fake.user.permissions.check_entity.side_effect = lambda entity, policy: (
        policy == POLICY_READ and entity == "calendar.school"
    )
    with pytest.raises(Unauthorized):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school", "calendar.private"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    fake.providers["calendar.school"].async_get_events.assert_not_awaited()
    fake.providers["calendar.private"].async_get_events.assert_not_awaited()


async def test_missing_calendar_component_and_entity_fail_closed():
    fake = hass({"calendar.school": {"events": [existing()]}})
    fake.data.pop(DATA_COMPONENT)
    with pytest.raises(CalendarObservationError, match="unavailable"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school"], local_zone=ZONE,
            context=READ_CONTEXT,
        )
    fake = hass({"calendar.school": {"events": [existing()]}})
    with pytest.raises(CalendarObservationError, match="incomplete"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school", "calendar.missing"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    fake.providers["calendar.school"].async_get_events.assert_not_awaited()


async def test_native_provider_date_datetime_values_and_missing_fields():
    raw = [
        SimpleNamespace(summary="Holiday", start=date(2026, 10, 8),
                        end=date(2026, 10, 9)),
        SimpleNamespace(summary="Meeting",
                        start=datetime.fromisoformat("2026-10-08T17:30:00-07:00"),
                        end=datetime.fromisoformat("2026-10-08T18:30:00-07:00")),
    ]
    fake = hass({"calendar.school": {"events": []}})
    fake.providers["calendar.school"].async_get_events.return_value = raw
    candidates = await async_observe_candidates(
        fake, draft(), observed_calendars=["calendar.school"], local_zone=ZONE,
        context=READ_CONTEXT,
    )
    assert [value.all_day for value in candidates] == [True, False]
    assert candidates[0].start == "2026-10-08"
    assert candidates[1].start == "2026-10-08T17:30:00-07:00"

    fake.providers["calendar.school"].async_get_events.return_value = [
        SimpleNamespace(start=date(2026, 10, 8), end=date(2026, 10, 9))
    ]
    with pytest.raises(CalendarObservationError, match="Missing calendar event"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.school"],
            local_zone=ZONE, context=READ_CONTEXT,
        )


def test_candidate_rejects_non_mapping_response():
    with pytest.raises(CalendarObservationError, match="Invalid calendar event response"):
        _candidate("calendar.work", None)


async def test_native_same_day_all_day_event_is_normalized_and_counted_as_conflict():
    """Native HA CalendarEvent expands a same-date all-day event to one day."""
    native = CalendarEvent(
        start=date(2026, 10, 31),
        end=date(2026, 10, 31),
        summary="Same-day holiday",
    )
    assert native.end == date(2026, 11, 1)
    fake = hass({"calendar.work": {"events": []}})
    fake.providers["calendar.work"].async_get_events.return_value = [
        native,
        CalendarEvent(
            start=date(2026, 10, 31),
            end=date(2026, 11, 2),
            summary="Practice",
        ),
    ]
    observed = await async_observe_candidates(
        fake, draft(all_day=True),
        observed_calendars=["calendar.work"], local_zone=ZONE,
        context=READ_CONTEXT,
    )
    assert [(event.start, event.end) for event in observed] == [
        ("2026-10-31", "2026-11-01"),
        ("2026-10-31", "2026-11-02"),
    ]
    matches = await async_classify_conflicts(
        fake, draft(all_day=True),
        observed_calendars=["calendar.work"], local_zone=ZONE,
        context=READ_CONTEXT,
    )
    assert [(match.kind, match.existing_title) for match in matches] == [
        ("conflict", "Same-day holiday"),
        ("exact_duplicate", "Practice"),
    ]


async def test_native_same_day_event_can_be_an_exact_duplicate():
    """Do not silently discard a normalized event with the same title."""
    native = CalendarEvent(
        start=date(2026, 10, 31),
        end=date(2026, 10, 31),
        summary="Practice",
    )
    fake = hass({"calendar.work": {"events": []}})
    fake.providers["calendar.work"].async_get_events.return_value = [native]
    one_day_draft = EventDraft(
        "Practice", "2026-10-31", "2026-11-01", True,
    )
    matches = await async_classify_conflicts(
        fake, one_day_draft,
        observed_calendars=["calendar.work"], local_zone=ZONE,
        context=READ_CONTEXT,
    )
    assert [(match.kind, match.existing_title) for match in matches] == [
        ("exact_duplicate", "Practice"),
    ]


async def test_native_timed_zero_duration_remains_a_non_overlapping_point():
    """Unlike all-day events, native timed events retain equal timestamps."""
    start = datetime.fromisoformat("2026-10-08T17:30:00-07:00")
    native = CalendarEvent(start=start, end=start, summary="Practice")
    assert native.end == native.start
    fake = hass({"calendar.work": {"events": []}})
    fake.providers["calendar.work"].async_get_events.return_value = [
        native,
        CalendarEvent(
            start=start,
            end=datetime.fromisoformat("2026-10-08T18:30:00-07:00"),
            summary="Other appointment",
        ),
    ]
    matches = await async_classify_conflicts(
        fake, draft(), observed_calendars=["calendar.work"],
        local_zone=ZONE, context=READ_CONTEXT,
    )
    assert [(match.kind, match.existing_title) for match in matches] == [
        ("conflict", "Other appointment"),
    ]


@pytest.mark.parametrize(("start", "end"), [
    ("not-a-date", "2026-10-09"),
    ("2026-10-08", "not-a-date"),
])
def test_invalid_all_day_draft_uses_observation_error(start, end):
    with pytest.raises(CalendarObservationError, match="Invalid all-day observation interval"):
        observation_window(EventDraft("Practice", start, end, True), local_zone=ZONE)


@pytest.mark.parametrize(("start", "end"), [
    ("0001-01-01T00:00:00+01:00", "0001-01-01T01:00:00+01:00"),
    ("9999-12-31T22:00:00-02:00", "9999-12-31T23:00:00-02:00"),
])
def test_draft_timezone_conversion_overflow_fails_at_observation_boundary(
    start, end,
):
    """Both ends of Python's supported datetime range can overflow UTC."""
    extreme = EventDraft("Extremely early or late", start, end, False)
    with pytest.raises(CalendarObservationError, match="Invalid timed observation"):
        observation_window(extreme, local_zone=ZONE)


def test_all_day_draft_utc_range_overflow_has_explicit_observation_error():
    """All-day local midnight at year 1 can precede the minimum UTC instant."""
    extreme = EventDraft("Year one", "0001-01-01", "0001-01-02", True)
    with pytest.raises(CalendarObservationError, match="Invalid event interval"):
        observation_window(extreme, local_zone=timezone(timedelta(hours=1)))


@pytest.mark.parametrize(("start", "end"), [
    ("0001-01-01T00:00:00+01:00", "0001-01-01T01:00:00+01:00"),
    ("9999-12-31T22:00:00-02:00", "9999-12-31T23:00:00-02:00"),
])
async def test_provider_timezone_conversion_overflow_fails_before_classification(
    start, end,
):
    """Invalid UTC bounds must not be interpreted as an empty agenda."""
    fake = hass({"calendar.work": {"events": [
        existing(start=start, end=end),
    ]}})
    with pytest.raises(CalendarObservationError, match="Invalid timed calendar interval"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=ZONE, context=READ_CONTEXT,
        )


async def test_all_day_provider_utc_overflow_is_observation_error_not_raw_overflow():
    """The pure matcher also converts local all-day dates to UTC."""
    fake = hass({"calendar.work": {"events": [
        {"summary": "Year one", "start": "0001-01-01", "end": "0001-01-02"},
    ]}})
    fixed_zone = timezone(timedelta(hours=1))
    with pytest.raises(
        CalendarObservationError, match="Invalid calendar interval for classification"
    ):
        await async_classify_conflicts(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=fixed_zone, context=READ_CONTEXT,
        )


# Mutation gates must check the public error contract, not only exception type.
# The review UI depends on distinguishing malformed inputs from incomplete HA
# calendar reads and read authorization failures.
@pytest.mark.parametrize(("item", "expected"), [
    (None, "Invalid calendar event response"),
    ({}, "Missing calendar event fields"),
    ({"summary": "Practice", "start": "bad-date-!", "end": "2026-10-09"},
     "Invalid all-day calendar interval"),
    ({"summary": "Practice", "start": "2026-10-09", "end": "2026-10-08"},
     "Invalid all-day calendar interval"),
    ({"summary": "Practice", "start": "2026-10-08", "end": "2026-10-08"},
     "Invalid all-day calendar interval"),
    ({"summary": "Practice", "start": "2026-10-08", "end":
      "2026-10-08T18:00:00-07:00"}, "Mixed calendar event date formats"),
    ({"summary": "Practice", "start": "2026-10-08T17:00:00-07:00",
      "end": "2026-10-08"}, "Mixed calendar event date formats"),
    ({"summary": "Practice", "start": "nonsense", "end":
      "2026-10-08T18:00:00-07:00"}, "Invalid timed calendar interval"),
    ({"summary": "Practice", "start": "2026-10-08T17:00:00-07:00",
      "end": "nonsense"}, "Invalid timed calendar interval"),
    ({"summary": "Practice", "start": "2026-10-08T17:00:00",
      "end": "2026-10-08T18:00:00-07:00"},
     "Timed calendar events require timezone offsets"),
    ({"summary": "Practice", "start": "2026-10-08T17:00:00-07:00",
      "end": "2026-10-08T18:00:00"},
     "Timed calendar events require timezone offsets"),
    ({"summary": "Practice", "start": "2026-10-08T18:00:00-07:00",
      "end": "2026-10-08T17:00:00-07:00"}, "Invalid timed calendar interval"),
    ({"summary": "Practice", "start": "0001-01-01T00:00:00+01:00",
      "end": "0001-01-01T01:00:00+01:00"}, "Invalid timed calendar interval"),
    ({"summary": "Practice", "start": "9999-12-31T22:00:00-02:00",
      "end": "9999-12-31T23:00:00-02:00"}, "Invalid timed calendar interval"),
])
def test_calendar_candidate_exact_validation_errors(item, expected):
    with pytest.raises(CalendarObservationError) as exc:
        _candidate("calendar.family", item)
    assert str(exc.value) == expected


@pytest.mark.parametrize(("item", "expected"), [
    (EventDraft("Practice", "bad-date", "2026-10-09", True),
     "Invalid all-day observation interval"),
    (EventDraft("Practice", "bad-datetime", "2026-10-08T18:00:00-07:00", False),
     "Invalid timed observation interval"),
    (EventDraft("Practice", "2026-10-08T17:00:00-07:00", "bad-datetime", False),
     "Invalid timed observation interval"),
    (EventDraft("Practice", "2026-10-08T17:00:00", "2026-10-08T18:00:00-07:00", False),
     "Timed observations require UTC offsets"),
    (EventDraft("Practice", "2026-10-08T17:00:00-07:00", "2026-10-08T18:00:00", False),
     "Timed observations require UTC offsets"),
    (EventDraft("Practice", "2026-10-08", "2026-10-08", True),
     "Invalid event interval"),
    (EventDraft("Practice", "2026-10-09", "2026-10-08", True),
     "Invalid event interval"),
    (EventDraft("Practice", "2026-10-08T18:00:00-07:00",
      "2026-10-08T18:00:00-07:00", False), "Invalid event interval"),
    (EventDraft("Practice", "2026-10-08T19:00:00-07:00",
      "2026-10-08T18:00:00-07:00", False), "Invalid event interval"),
    (EventDraft("Practice", "0001-01-01T00:00:00+01:00",
      "0001-01-01T01:00:00+01:00", False), "Invalid timed observation interval"),
    (EventDraft("Practice", "9999-12-31T22:00:00-02:00",
      "9999-12-31T23:00:00-02:00", False), "Invalid timed observation interval"),
])
def test_observation_window_exact_validation_errors(item, expected):
    with pytest.raises(CalendarObservationError) as exc:
        observation_window(item, local_zone=ZONE)
    assert str(exc.value) == expected


@pytest.mark.parametrize(("calendar_ids", "expected"), [
    (["sensor.illegal"], "Invalid observation calendar"),
    ([["calendar.unhashable"]], "Invalid observation calendar"),
])
async def test_rejected_calendar_scope_error_is_precise(calendar_ids, expected):
    fake = hass(None)
    with pytest.raises(CalendarObservationError) as exc:
        await async_observe_candidates(
            fake, draft(), observed_calendars=calendar_ids,
            local_zone=ZONE, context=READ_CONTEXT,
        )
    assert str(exc.value) == expected
    fake.auth.async_get_user.assert_not_awaited()


@pytest.mark.parametrize(("fake_response", "expected"), [
    (None, "Calendar observation is incomplete"),
    ({"calendar.work": {"events": "bad-provider-data"}},
     "Calendar observation is incomplete"),
])
async def test_calendar_provider_incomplete_response_message(fake_response, expected):
    fake = hass(fake_response)
    with pytest.raises(CalendarObservationError) as exc:
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    assert str(exc.value) == expected


async def test_missing_calendar_component_and_entity_exact_error_contract():
    fake = hass({"calendar.work": {"events": []}})
    fake.data.pop(DATA_COMPONENT)
    with pytest.raises(CalendarObservationError) as exc:
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    assert str(exc.value) == "Calendar observation is unavailable"

    fake = hass({"calendar.work": {"events": []}})
    with pytest.raises(CalendarObservationError) as exc:
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work", "calendar.missing"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    assert str(exc.value) == "Calendar observation is incomplete"
    fake.providers["calendar.work"].async_get_events.assert_not_awaited()


@pytest.mark.parametrize("missing", ["context", "user", "calendar_permission"])
async def test_calendar_authorization_denials_include_expected_metadata(missing):
    fake = hass({"calendar.work": {"events": [existing()]}})
    context = Context(user_id="reviewer")
    if missing == "context":
        context = Context()
    elif missing == "user":
        fake.auth.async_get_user.return_value = None
    elif missing == "calendar_permission":
        fake.user.permissions.check_entity.return_value = False
    with pytest.raises(Unauthorized) as exc:
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=ZONE, context=context,
        )
    denied = exc.value
    assert denied.context is context
    assert denied.permission == POLICY_READ
    assert denied.user_id == context.user_id
    assert denied.entity_id == (
        "calendar.work" if missing == "calendar_permission" else None
    )
    fake.providers["calendar.work"].async_get_events.assert_not_awaited()


def test_all_day_window_utc_underflow_message_is_exact():
    fixed_zone = timezone(timedelta(hours=1))
    with pytest.raises(CalendarObservationError) as exc:
        observation_window(
            EventDraft("Year one", "0001-01-01", "0001-01-02", True),
            local_zone=fixed_zone,
        )
    assert str(exc.value) == "Invalid event interval"


async def test_classifier_overflow_preserves_exact_boundary_message():
    fake = hass({"calendar.work": {"events": [
        {"summary": "Year one", "start": "0001-01-01", "end": "0001-01-02"},
    ]}})
    with pytest.raises(CalendarObservationError) as exc:
        await async_classify_conflicts(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=timezone(timedelta(hours=1)), context=READ_CONTEXT,
        )
    assert str(exc.value) == "Invalid calendar interval for classification"


async def test_classifier_rejects_bad_timed_point_with_exact_error(monkeypatch):
    """Exercise the classifier's defensive UTC conversion after observation."""
    from custom_components.daylight_calendar_import import calendar_observation
    from custom_components.daylight_calendar_import.calendar_match import CalendarCandidate

    unsafe = CalendarCandidate(
        "calendar.work", "Practice",
        "0001-01-01T00:00:00+01:00", "0001-01-01T00:00:00+01:00", False,
    )
    monkeypatch.setattr(
        calendar_observation, "async_observe_candidates",
        AsyncMock(return_value=(unsafe,)),
    )
    with pytest.raises(CalendarObservationError) as exc:
        await async_classify_conflicts(
            hass(None), draft(), observed_calendars=["calendar.work"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    assert str(exc.value) == "Invalid timed calendar interval"


@pytest.mark.parametrize("all_day", [True, False])
def test_observation_range_limit_rejects_91_days_before_provider_work(all_day):
    from custom_components.daylight_calendar_import.calendar_observation import MAX_OBSERVATION_WINDOW
    assert MAX_OBSERVATION_WINDOW == timedelta(days=90)
    if all_day:
        too_long = EventDraft("Long", "2026-01-01", "2026-04-02", True)
    else:
        too_long = EventDraft("Long", "2026-01-01T12:00:00+00:00",
                              "2026-04-02T12:00:00+00:00", False)
    with pytest.raises(CalendarObservationError, match="exceeds 90 days"):
        observation_window(too_long, local_zone=ZONE)


async def test_observation_range_limit_allows_90_days():
    event = EventDraft("Long", "2026-01-01", "2026-04-01", True)
    fake = hass({"calendar.work": {"events": []}})
    assert await async_observe_candidates(
        fake, event, observed_calendars=["calendar.work"],
        local_zone=ZONE, context=READ_CONTEXT
    ) == ()


async def test_observation_scope_limit_rejects_more_than_16_calendars_without_reads():
    ids = [f"calendar.room_{i}" for i in range(17)]
    fake = hass({calendar: {"events": []} for calendar in ids})
    with pytest.raises(CalendarObservationError, match="exceeds 16 calendars"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=ids,
            local_zone=ZONE, context=READ_CONTEXT,
        )
    for provider in fake.providers.values():
        provider.async_get_events.assert_not_awaited()


async def test_provider_response_over_500_events_fails_explicitly_without_truncation():
    raw_events = [existing() for _ in range(501)]
    fake = hass({"calendar.work": {"events": raw_events}})
    with pytest.raises(CalendarObservationError, match="exceeds 500 events"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=ZONE, context=READ_CONTEXT,
        )


async def test_cumulative_event_limit_checks_across_selected_calendars():
    fake = hass({
        "calendar.one": {"events": [existing() for _ in range(260)]},
        "calendar.two": {"events": [existing() for _ in range(241)]},
    })
    with pytest.raises(CalendarObservationError, match="exceeds 500 events"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.one", "calendar.two"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    fake.providers["calendar.one"].async_get_events.assert_awaited_once()
    fake.providers["calendar.two"].async_get_events.assert_awaited_once()


async def test_exactly_500_observed_events_are_returned_without_truncation():
    fake = hass({"calendar.work": {"events": [existing() for _ in range(500)]}})
    matches = await async_classify_conflicts(
        fake, draft(), observed_calendars=["calendar.work"],
        local_zone=ZONE, context=READ_CONTEXT,
    )
    assert len(matches) == 500
    assert all(match.kind == "exact_duplicate" for match in matches)


async def test_unbounded_provider_title_is_an_explicit_incomplete_observation():
    fake = hass({"calendar.work": {"events": [
        existing(summary="x" * 513)
    ]}})
    with pytest.raises(CalendarObservationError, match="title exceeds 512 characters"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.work"],
            local_zone=ZONE, context=READ_CONTEXT,
        )


@pytest.mark.parametrize("position", ["first", "second"])
async def test_unavailable_calendar_entity_fails_before_any_provider_read(position):
    """An HA entity marked unavailable must not act like an empty calendar."""
    fake = hass({
        "calendar.family": {"events": []},
        "calendar.work": {"events": [existing()]},
    })
    target = "calendar.family" if position == "first" else "calendar.work"
    fake.providers[target].available = False
    with pytest.raises(CalendarObservationError, match="incomplete"):
        await async_observe_candidates(
            fake, draft(), observed_calendars=["calendar.family", "calendar.work"],
            local_zone=ZONE, context=READ_CONTEXT,
        )
    fake.providers["calendar.family"].async_get_events.assert_not_awaited()
    fake.providers["calendar.work"].async_get_events.assert_not_awaited()


async def test_available_calendar_entity_remains_observable():
    fake = hass({"calendar.family": {"events": []}})
    fake.providers["calendar.family"].available = True
    result = await async_observe_candidates(
        fake, draft(), observed_calendars=["calendar.family"],
        local_zone=ZONE, context=READ_CONTEXT,
    )
    assert result == ()
    fake.providers["calendar.family"].async_get_events.assert_awaited_once()
