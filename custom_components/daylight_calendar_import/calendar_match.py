"""Pure calendar duplicate/conflict classification without HA side effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone, tzinfo
import unicodedata

from .models import EventDraft


def _key(title: str) -> str:
    """Normalize innocuous whitespace and case, not fuzzy semantics."""
    return " ".join(unicodedata.normalize("NFKC", title).split()).casefold()


def _range(
    start: str, end: str, all_day: bool, local_zone: tzinfo,
) -> tuple[datetime, datetime]:
    """Convert to UTC, rejecting ambiguous or invalid provider intervals."""
    if all_day:
        start_utc = datetime.combine(
            date.fromisoformat(start), time.min, local_zone
        ).astimezone(timezone.utc)
        end_utc = datetime.combine(
            date.fromisoformat(end), time.min, local_zone
        ).astimezone(timezone.utc)
    else:
        start_time = datetime.fromisoformat(start)
        end_time = datetime.fromisoformat(end)
        if start_time.utcoffset() is None or end_time.utcoffset() is None:
            raise ValueError("Timed calendar events must include timezone offsets")
        start_utc = start_time.astimezone(timezone.utc)
        end_utc = end_time.astimezone(timezone.utc)
    if end_utc <= start_utc:
        raise ValueError("Calendar event end must be after start")
    return start_utc, end_utc

def _display_range(start: datetime, end: datetime, local_zone: tzinfo) -> tuple[str, str]:
    """Keep an interval in one zone even at representable UTC year boundaries."""
    try:
        return start.astimezone(local_zone).isoformat(), end.astimezone(local_zone).isoformat()
    except OverflowError:
        return start.isoformat(), end.isoformat()


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
    existing_start: str
    existing_end: str
    existing_all_day: bool
    overlap_start: str
    overlap_end: str


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
    displayed_start, displayed_end = (
        (existing.start, existing.end) if existing.all_day
        else _display_range(existing_start, existing_end, local_zone)
    )
    overlap_start, overlap_end = _display_range(
        max(candidate_start, existing_start), min(candidate_end, existing_end), local_zone
    )
    return CalendarMatch(
        kind, existing.calendar_entity, existing.title,
        displayed_start, displayed_end, existing.all_day, overlap_start, overlap_end,
    )
