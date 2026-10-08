"""Calendar classification tests independent of calendar provider behavior."""

from zoneinfo import ZoneInfo

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


LOCAL_ZONE = ZoneInfo("America/Los_Angeles")


def classify(draft_event, existing_event):
    return classify_calendar_event(draft_event, existing_event, local_zone=LOCAL_ZONE)


def test_exact_duplicate_ignores_descriptions_and_matches_timezones():
    result = classify(draft(), existing("  PRACTICE  "))
    assert result.kind == "exact_duplicate"
    assert result.calendar_entity == "calendar.observed"


def test_possible_duplicate_same_title_different_overlap():
    assert classify(
        draft(), existing(end="2026-10-09T00:30:00+00:00")
    ).kind == "possible_duplicate"


def test_distinct_titles_overlap_but_adjacent_does_not():
    assert classify(draft(), existing("Dinner")).kind == "conflict"
    assert classify(
        draft(), existing("Dinner", start="2026-10-09T01:00:00+00:00",
                          end="2026-10-09T02:00:00+00:00")
    ) is None


def test_all_day_event_ranges_are_exclusive_end():
    candidate = draft("Holiday", "2026-10-08", "2026-10-10", True)
    assert classify(
        candidate, existing("Holiday", "2026-10-08", "2026-10-10", True)
    ).kind == "exact_duplicate"
    assert classify(
        candidate, existing("Holiday", "2026-10-10", "2026-10-11", True)
    ) is None
    assert classify(
        candidate, existing("Vacation", "2026-10-09", "2026-10-11", True)
    ).kind == "conflict"


def test_contained_range_is_conflict_and_same_title_is_possible_duplicate():
    assert classify(
        draft(), existing("Meeting", "2026-10-08T23:00:00+00:00",
                          "2026-10-09T03:00:00+00:00")
    ).kind == "conflict"
    assert classify(
        draft(), existing("Practice", "2026-10-08T23:00:00+00:00",
                          "2026-10-09T03:00:00+00:00")
    ).kind == "possible_duplicate"


def test_all_day_dst_boundaries_are_local_midnights():
    from datetime import datetime, timezone
    from custom_components.daylight_calendar_import.calendar_match import _range

    start, end = _range("2026-11-01", "2026-11-02", True, LOCAL_ZONE)
    assert start == datetime(2026, 11, 1, 7, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 25 * 3600


def test_mixed_all_day_and_timed_uses_local_day_boundary():
    all_day = draft("Fair", "2026-10-08", "2026-10-09", True)
    timed = existing("Meeting", "2026-10-08T05:00:00+00:00", "2026-10-08T06:00:00+00:00")
    assert classify(all_day, timed) is None
    overlap = existing("Meeting", "2026-10-08T08:00:00+00:00", "2026-10-08T09:00:00+00:00")
    assert classify(all_day, overlap).kind == "conflict"
