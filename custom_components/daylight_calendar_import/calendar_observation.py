"""Read-only Home Assistant calendar observation for review-time matching.

This boundary never grants write access or implies a calendar can be created
upon. A missing/failed provider response is not equivalent to an empty agenda.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from dataclasses import replace
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


MAX_OBSERVATION_WINDOW = timedelta(days=90)
MAX_OBSERVATION_EVENTS = 500
MAX_OBSERVATION_CALENDARS = 16
MAX_OBSERVATION_TITLE_CHARS = 512


def _as_utc(value: datetime, *, message: str) -> datetime:
    """Normalize time bounds, including overflows at ISO years 1 and 9999."""
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise CalendarObservationError(message) from exc


def observation_window(
    draft: EventDraft, *, local_zone: tzinfo,
) -> tuple[datetime, datetime]:
    """Use the draft's real local interval, including variable DST day lengths."""
    if draft.all_day:
        try:
            start = datetime.combine(date.fromisoformat(draft.start), time.min, local_zone)
            end = datetime.combine(date.fromisoformat(draft.end), time.min, local_zone)
        except ValueError as exc:
            raise CalendarObservationError("Invalid all-day observation interval") from exc
    else:
        try:
            start_value = datetime.fromisoformat(draft.start)
            end_value = datetime.fromisoformat(draft.end)
        except ValueError as exc:
            raise CalendarObservationError("Invalid timed observation interval") from exc
        if start_value.utcoffset() is None or end_value.utcoffset() is None:
            raise CalendarObservationError("Timed observations require UTC offsets")
        try:
            start = start_value.astimezone(local_zone)
            end = end_value.astimezone(local_zone)
        except (OverflowError, ValueError) as exc:
            raise CalendarObservationError("Invalid timed observation interval") from exc
    # Python compares datetimes sharing a tzinfo by wall clock, which is
    # incorrect across the repeated hour of the autumn DST transition.
    start_utc = _as_utc(start, message="Invalid event interval")
    end_utc = _as_utc(end, message="Invalid event interval")
    if end_utc <= start_utc:
        raise CalendarObservationError("Invalid event interval")
    if end_utc - start_utc > MAX_OBSERVATION_WINDOW:
        raise CalendarObservationError("Calendar observation interval exceeds 90 days")
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
    if len(title) > MAX_OBSERVATION_TITLE_CHARS:
        raise CalendarObservationError("Calendar observation title exceeds 512 characters")
    all_day = len(start) == 10 and len(end) == 10
    if all_day:
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError as exc:
            raise CalendarObservationError("Invalid all-day calendar interval") from exc
        # Native CalendarEvent normalizes same-day all-day dates to one
        # calendar day in __post_init__. An equal-date response has bypassed
        # that contract; never invent its intended interval here.
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
        if _as_utc(end_time, message="Invalid timed calendar interval") < _as_utc(
            start_time, message="Invalid timed calendar interval"
        ):
            raise CalendarObservationError("Invalid timed calendar interval")
    return CalendarCandidate(calendar_entity, title, start, end, all_day)


async def async_observe_candidates(
    hass: HomeAssistant,
    draft: EventDraft,
    *,
    observed_calendars: Sequence[str],
    local_zone: tzinfo,
    context: Context | None = None,
    trusted_internal: bool = False,
) -> tuple[CalendarCandidate, ...]:
    """Read selected calendars; only trusted HA internal approval may omit a user."""
    if not observed_calendars:
        return ()
    # Validate before using identifiers as dict keys, so malformed/unhashable
    # input fails with the same explicit observation error.
    if any(not isinstance(identifier, str) or not identifier.startswith("calendar.")
           for identifier in observed_calendars):
        raise CalendarObservationError("Invalid observation calendar")
    identifiers = list(dict.fromkeys(observed_calendars))
    if len(identifiers) > MAX_OBSERVATION_CALENDARS:
        raise CalendarObservationError("Calendar observation exceeds 16 calendars")
    start, end = observation_window(draft, local_zone=local_zone)
    # Home Assistant's calendar.get_events *service* requires POLICY_CONTROL
    # because generic entity services are control-scoped. Read-only reviewers
    # must instead use the same POLICY_READ + entity API as HA's calendar view.
    if context is None or context.user_id is None:
        # HA automations and trusted in-process service calls use a userless
        # context. The caller must explicitly opt into this internal-only path;
        # public review reads still require an authenticated user.
        if not trusted_internal:
            raise Unauthorized(context=context, permission=POLICY_READ)
    else:
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
        if provider is None or getattr(provider, "available", True) is False:
            # HA can retain unavailable entities and return cached/empty events.
            raise CalendarObservationError("Calendar observation is incomplete")
        providers.append((entity_id, provider))

    def ensure_available(entity_id: str, provider: Any) -> None:
        """Require the same registered, available provider throughout this read."""
        if (
            hass.data.get(DATA_COMPONENT) is not component
            or component.get_entity(entity_id) is not provider
            or getattr(provider, "available", True) is False
        ):
            raise CalendarObservationError("Calendar observation is incomplete")

    candidates: list[CalendarCandidate] = []
    for entity_id, provider in providers:
        # A later provider might have become unavailable while earlier reads
        # awaited; a provider can also become unavailable during its own read.
        ensure_available(entity_id, provider)
        try:
            response = await provider.async_get_events(hass, start, end)
        except Exception as exc:
            # Provider errors are not proof of an empty schedule. Do not
            # surface provider-specific messages in the user-facing response.
            raise CalendarObservationError("Calendar observation is incomplete") from exc
        ensure_available(entity_id, provider)
        if not isinstance(response, list):
            raise CalendarObservationError("Calendar observation is incomplete")
        if len(response) > MAX_OBSERVATION_EVENTS - len(candidates):
            raise CalendarObservationError("Calendar observation exceeds 500 events")
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
    # An earlier provider may have gone unavailable while a later one awaited.
    # Never report the combined observation as complete in that case.
    for entity_id, provider in providers:
        ensure_available(entity_id, provider)
    return tuple(candidates)


def _approval_start_window(draft: EventDraft) -> EventDraft:
    """Read only the start of a candidate, then compare full original intervals.

    An exact duplicate necessarily shares the draft's start. Calendar providers
    report events intersecting the query interval, so a one-day (all-day) or
    one-minute (timed) start window is enough for the exact-duplicate guard.
    Keep the full draft untouched for classification against those candidates.
    """
    if draft.all_day:
        start = date.fromisoformat(draft.start)
        end = date.fromisoformat(draft.end)
        window = timedelta(days=1)
    else:
        # Different UTC offsets can make a valid instant later than the
        # start even when its wall clock is earlier. Build the short probe
        # in UTC: adding one minute to a year-9999 wall time may overflow,
        # although both event instants and their UTC probe are representable.
        start = _as_utc(
            datetime.fromisoformat(draft.start),
            message="Invalid timed observation interval",
        )
        end = _as_utc(
            datetime.fromisoformat(draft.end),
            message="Invalid timed observation interval",
        )
        window = timedelta(minutes=1)
    if end - start <= window:
        return draft
    # end is a representable instant at least one window after start, so
    # start + window cannot overflow the ISO year bounds.
    return replace(draft, end=(start + window).isoformat())


async def async_classify_conflicts(
    hass: HomeAssistant, draft: EventDraft, *,
    observed_calendars: Sequence[str], local_zone: tzinfo,
    context: Context | None = None,
    trusted_internal: bool = False,
    approval_start_only: bool = False,
) -> tuple[CalendarMatch, ...]:
    """Classify observed event intervals without hiding calendar lookup errors."""
    observation_draft = _approval_start_window(draft) if approval_start_only else draft
    candidates = await async_observe_candidates(
        hass, observation_draft, observed_calendars=observed_calendars,
        local_zone=local_zone, context=context,
        trusted_internal=trusted_internal,
    )
    matches: list[CalendarMatch] = []
    for existing in candidates:
        # Native CalendarEvent already expands a same-day all-day event into
        # a one-day interval. Only timed zero-duration points are non-overlapping.
        if not existing.all_day and (
            _as_utc(
                datetime.fromisoformat(existing.start),
                message="Invalid timed calendar interval",
            ) == _as_utc(
                datetime.fromisoformat(existing.end),
                message="Invalid timed calendar interval",
            )
        ):
            continue
        try:
            match = classify_calendar_event(draft, existing, local_zone=local_zone)
        except (OverflowError, ValueError) as exc:
            # The pure matcher operates on UTC intervals. For extreme valid
            # local dates, conversion to UTC may overflow datetime's range.
            raise CalendarObservationError(
                "Invalid calendar interval for classification"
            ) from exc
        if match is not None:
            matches.append(match)
    return tuple(matches)
