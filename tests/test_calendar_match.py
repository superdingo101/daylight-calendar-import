"""Calendar classification tests independent of calendar provider behavior."""

from custom_components.daylight_calendar_import.calendar_match import (
    CalendarCandidate, classify_calendar_event,
)
from custom_components.daylight_calendar_import.models import EventDraft


def draft(title="Practice", start="2026-10-08T17:00:00-07:00",
          end="2026-10-08T18:00:00-07:00", all_day=False):
    return EventDraft(title, start, end, all_day)


def existing(title="Practice", start="2026-10-09T00:00:00+00:00",
             end="2026-10-09T01:00:00+00:00", all_day=False):
    return CalendarCandidate("calendar.observed", title, start, end, all_day)


def test_exact_duplicate_ignores_descriptions_and_matches_timezones():
    result = classify_calendar_event(draft(), existing("  PRACTICE  "))
    assert result.kind == "exact_duplicate"
    assert result.calendar_entity == "calendar.observed"


def test_possible_duplicate_same_title_different_overlap():
    assert classify_calendar_event(
        draft(), existing(end="2026-10-09T00:30:00+00:00")
    ).kind == "possible_duplicate"


def test_distinct_titles_overlap_but_adjacent_does_not():
    assert classify_calendar_event(draft(), existing("Dinner")).kind == "conflict"
    assert classify_calendar_event(
        draft(), existing("Dinner", start="2026-10-09T01:00:00+00:00",
                          end="2026-10-09T02:00:00+00:00")
    ) is None


def test_all_day_event_ranges_are_exclusive_end():
    candidate = draft("Holiday", "2026-10-08", "2026-10-10", True)
    assert classify_calendar_event(
        candidate, existing("Holiday", "2026-10-08", "2026-10-10", True)
    ).kind == "exact_duplicate"
    assert classify_calendar_event(
        candidate, existing("Holiday", "2026-10-10", "2026-10-11", True)
    ) is None
    assert classify_calendar_event(
        candidate, existing("Vacation", "2026-10-09", "2026-10-11", True)
    ).kind == "conflict"


def test_contained_range_is_conflict_and_same_title_is_possible_duplicate():
    assert classify_calendar_event(
        draft(), existing("Meeting", "2026-10-08T23:00:00+00:00",
                          "2026-10-09T03:00:00+00:00")
    ).kind == "conflict"
    assert classify_calendar_event(
        draft(), existing("Practice", "2026-10-08T23:00:00+00:00",
                          "2026-10-09T03:00:00+00:00")
    ).kind == "possible_duplicate"
