"""Pure calendar duplicate/conflict classification without HA side effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone, tzinfo

from .models import EventDraft


def _key(title: str) -> str:
    """Normalize innocuous whitespace and case, not fuzzy semantics."""
    return " ".join(title.split()).casefold()


def _range(
    start: str, end: str, all_day: bool, local_zone: tzinfo,
) -> tuple[datetime, datetime]:
    """Compare intervals as UTC instants, interpreting all-day dates locally."""
    if all_day:
        return (
            datetime.combine(date.fromisoformat(start), time.min, local_zone).astimezone(timezone.utc),
            datetime.combine(date.fromisoformat(end), time.min, local_zone).astimezone(timezone.utc),
        )
    return (
        datetime.fromisoformat(start).astimezone(timezone.utc),
        datetime.fromisoformat(end).astimezone(timezone.utc),
    )


@dataclass(frozen=True, slots=True)
class CalendarCandidate:
    """Minimum bounded information needed from an existing calendar event."""

    calendar_entity: str
    title: str
    start: str
    end: str
    all_day: bool


@dataclass(frozen=True, slots=True)
class CalendarMatch:
    """Stable match type and calendar scope for human review."""

    kind: str
    calendar_entity: str
    existing_title: str


def classify_calendar_event(
    draft: EventDraft, existing: CalendarCandidate, *, local_zone: tzinfo,
) -> CalendarMatch | None:
    """Classify exact duplicate, possible duplicate, or scheduling conflict."""
    candidate_start, candidate_end = _range(draft.start, draft.end, draft.all_day, local_zone)
    existing_start, existing_end = _range(
        existing.start, existing.end, existing.all_day, local_zone
    )
    same_title = _key(draft.title) == _key(existing.title)
    same_interval = (
        draft.all_day == existing.all_day
        and candidate_start == existing_start
        and candidate_end == existing_end
    )
    if same_title and same_interval:
        kind = "exact_duplicate"
    elif not (candidate_start < existing_end and existing_start < candidate_end):
        return None
    elif same_title:
        kind = "possible_duplicate"
    else:
        kind = "conflict"
    return CalendarMatch(kind, existing.calendar_entity, existing.title)
