"""Read-only Home Assistant calendar observation for review-time matching.

This boundary never grants write access or implies a calendar can be created
upon. A missing/failed provider response is not equivalent to an empty agenda.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone, tzinfo
from collections.abc import Sequence
from typing import Any

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

from .calendar_match import CalendarCandidate, CalendarMatch, classify_calendar_event
from .models import EventDraft


class CalendarObservationError(ValueError):
    """Calendar responses cannot be trusted to prove no overlap."""


def observation_window(
    draft: EventDraft, *, local_zone: tzinfo,
) -> tuple[datetime, datetime]:
    """Use the draft's real local interval, including variable DST day lengths."""
    if draft.all_day:
        start = datetime.combine(date.fromisoformat(draft.start), time.min, local_zone)
        end = datetime.combine(date.fromisoformat(draft.end), time.min, local_zone)
    else:
        try:
            start_value = datetime.fromisoformat(draft.start)
            end_value = datetime.fromisoformat(draft.end)
        except ValueError as exc:
            raise CalendarObservationError("Invalid timed observation interval") from exc
        if start_value.utcoffset() is None or end_value.utcoffset() is None:
            raise CalendarObservationError("Timed observations require UTC offsets")
        start = start_value.astimezone(local_zone)
        end = end_value.astimezone(local_zone)
    # Python compares datetimes sharing a tzinfo by wall clock, which is
    # incorrect across the repeated hour of the autumn DST transition.
    if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
        raise CalendarObservationError("Invalid event interval")
    return start, end


def _candidate(calendar_entity: str, raw: Any) -> CalendarCandidate:
    """Validate the bounded provider fields used by the pure classifier."""
    if not isinstance(raw, dict):
        raise CalendarObservationError("Invalid calendar event response")
    title = raw.get("summary")
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(title, str) or not isinstance(start, str) or not isinstance(end, str):
        raise CalendarObservationError("Missing calendar event fields")
    all_day = len(start) == 10 and len(end) == 10
    if all_day:
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError as exc:
            raise CalendarObservationError("Invalid all-day calendar interval") from exc
        if date.fromisoformat(end) <= date.fromisoformat(start):
            raise CalendarObservationError("Invalid all-day calendar interval")
    elif len(start) == 10 or len(end) == 10:
        raise CalendarObservationError("Mixed calendar event date formats")
    else:
        try:
            start_time = datetime.fromisoformat(start)
            end_time = datetime.fromisoformat(end)
        except ValueError as exc:
            raise CalendarObservationError("Invalid timed calendar interval") from exc
        if start_time.utcoffset() is None or end_time.utcoffset() is None:
            raise CalendarObservationError("Timed calendar events require timezone offsets")
        # Home Assistant permits zero-duration timed provider events. Reject
        # reversed intervals, but preserve zero-duration points for observation.
        if end_time.astimezone(timezone.utc) < start_time.astimezone(timezone.utc):
            raise CalendarObservationError("Invalid timed calendar interval")
    return CalendarCandidate(calendar_entity, title, start, end, all_day)


async def async_observe_candidates(
    hass: HomeAssistant,
    draft: EventDraft,
    *,
    observed_calendars: Sequence[str],
    local_zone: tzinfo,
    context: Context | None = None,
) -> tuple[CalendarCandidate, ...]:
    """Read selected calendars under POLICY_READ; never create/edit events."""
    identifiers = list(dict.fromkeys(observed_calendars))
    if not identifiers:
        return ()
    if any(not isinstance(identifier, str) or not identifier.startswith("calendar.")
           for identifier in identifiers):
        raise CalendarObservationError("Invalid observation calendar")
    start, end = observation_window(draft, local_zone=local_zone)
    # Home Assistant's calendar.get_events *service* requires POLICY_CONTROL
    # because generic entity services are control-scoped. Read-only reviewers
    # must instead use the same POLICY_READ + entity API as HA's calendar view.
    if context is None or context.user_id is None:
        raise Unauthorized(context=context, permission=POLICY_READ)
    user = await hass.auth.async_get_user(context.user_id)
    if user is None:
        raise Unauthorized(
            context=context, permission=POLICY_READ, user_id=context.user_id,
        )
    for entity in identifiers:
        if not user.permissions.check_entity(entity, POLICY_READ):
            raise Unauthorized(
                context=context, entity_id=entity, permission=POLICY_READ,
                user_id=context.user_id,
            )
    component = hass.data.get(DATA_COMPONENT)
    if component is None:
        raise CalendarObservationError("Calendar observation is unavailable")
    providers = []
    for entity_id in identifiers:
        provider = component.get_entity(entity_id)
        if provider is None:
            raise CalendarObservationError("Calendar observation is incomplete")
        providers.append((entity_id, provider))

    candidates: list[CalendarCandidate] = []
    for entity_id, provider in providers:
        response = await provider.async_get_events(hass, start, end)
        if not isinstance(response, list):
            raise CalendarObservationError("Calendar observation is incomplete")
        for event in response:
            # CalendarEvent exposes native date/datetime fields. Do not retain
            # descriptions, attendees or other private provider metadata.
            summary = getattr(event, "summary", None)
            event_start = getattr(event, "start", None)
            event_end = getattr(event, "end", None)
            candidates.append(_candidate(entity_id, {
                "summary": summary,
                "start": (
                    event_start.isoformat()
                    if isinstance(event_start, (date, datetime)) else event_start
                ),
                "end": (
                    event_end.isoformat()
                    if isinstance(event_end, (date, datetime)) else event_end
                ),
            }))
    return tuple(candidates)


async def async_classify_conflicts(
    hass: HomeAssistant, draft: EventDraft, *,
    observed_calendars: Sequence[str], local_zone: tzinfo,
    context: Context | None = None,
) -> tuple[CalendarMatch, ...]:
    """Classify observed event intervals without hiding calendar lookup errors."""
    candidates = await async_observe_candidates(
        hass, draft, observed_calendars=observed_calendars,
        local_zone=local_zone, context=context,
    )
    matches: list[CalendarMatch] = []
    for existing in candidates:
        # A provider's zero-duration point occupies no scheduling interval,
        # so the positive-duration classifier must not attempt to match it.
        if not existing.all_day and (
            datetime.fromisoformat(existing.start).astimezone(timezone.utc)
            == datetime.fromisoformat(existing.end).astimezone(timezone.utc)
        ):
            continue
        match = classify_calendar_event(draft, existing, local_zone=local_zone)
        if match is not None:
            matches.append(match)
    return tuple(matches)
