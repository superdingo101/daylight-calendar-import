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


def test_unicode_equivalent_titles_are_exact_duplicates():
    assert classify(draft("Café"), existing("Cafe\u0301")).kind == "exact_duplicate"
    assert classify(draft("Ａｃｔｉｖｉｔｙ"), existing("Activity")).kind == "exact_duplicate"


def test_naive_timed_provider_values_fail_closed():
    import pytest

    for start, end in (
        ("2026-10-08T17:00:00", "2026-10-08T18:00:00+00:00"),
        ("2026-10-09T00:00:00+00:00", "2026-10-09T01:00:00"),
    ):
        with pytest.raises(ValueError, match=r"^Timed calendar events must include timezone offsets$"):
            classify(draft(), existing(start=start, end=end))


def test_invalid_provider_ranges_fail_closed():
    import pytest

    invalid_ranges = (
        existing(start="2026-10-09T00:00:00+00:00", end="2026-10-09T00:00:00+00:00"),
        existing(start="2026-10-09T01:00:00+00:00", end="2026-10-09T00:00:00+00:00"),
        existing(start="2026-10-10", end="2026-10-09", all_day=True),
    )
    for candidate in invalid_ranges:
        with pytest.raises(ValueError, match=r"^Calendar event end must be after start$"):
            classify(draft(), candidate)


def test_end_to_start_adjacency_is_not_a_conflict():
    """Back-to-back events are distinct in either direction."""
    assert classify(
        draft("Practice"),
        existing(
            "Another appointment",
            start="2026-10-08T23:00:00+00:00",
            end="2026-10-09T00:00:00+00:00",
        ),
    ) is None


def test_match_interval_context_is_clipped_and_normalized_to_local_zone():
    """Only the allowed interval fields leave calendar observation."""
    from dataclasses import asdict

    result = classify(draft(), existing("Dinner", "2026-10-08T23:30:00Z",
                                       "2026-10-09T00:30:00Z"))
    assert asdict(result) == {
        "kind": "conflict", "calendar_entity": "calendar.observed",
        "existing_title": "Dinner", "existing_start": "2026-10-08T16:30:00-07:00",
        "existing_end": "2026-10-08T17:30:00-07:00", "existing_all_day": False,
        "overlap_start": "2026-10-08T17:00:00-07:00",
        "overlap_end": "2026-10-08T17:30:00-07:00",
    }
    contained = classify(draft(), existing("Dinner", "2026-10-09T00:15:00Z",
                                          "2026-10-09T00:45:00Z"))
    assert contained.overlap_start == "2026-10-08T17:15:00-07:00"
    assert contained.overlap_end == "2026-10-08T17:45:00-07:00"


def test_all_day_context_keeps_exclusive_dates_and_dst_overlap():
    result = classify(draft("Fair", "2026-11-01", "2026-11-03", True),
                      existing("Holiday", "2026-11-01", "2026-11-02", True))
    assert result.existing_start == "2026-11-01"
    assert result.existing_end == "2026-11-02"
    assert result.existing_all_day is True
    assert result.overlap_start == "2026-11-01T00:00:00-07:00"
    assert result.overlap_end == "2026-11-02T00:00:00-08:00"


def test_extreme_utc_context_falls_back_when_local_offset_overflows():
    from datetime import timedelta, timezone

    event = draft("Future", "9999-12-31T09:00:00Z", "9999-12-31T11:00:00Z")
    result = classify_calendar_event(event, existing("Future", event.start, event.end),
                                     local_zone=timezone(timedelta(hours=14)))
    assert result.kind == "exact_duplicate"
    assert result.existing_start == "9999-12-31T09:00:00+00:00"
    assert result.existing_end == "9999-12-31T11:00:00+00:00"
    assert result.overlap_start == result.existing_start
    assert result.overlap_end == result.existing_end


def test_early_utc_range_falls_back_as_a_pair_when_local_start_underflows():
    from datetime import timedelta, timezone

    event = draft("Early", "0001-01-01T01:00:00Z", "0001-01-01T15:00:00Z")
    result = classify_calendar_event(event, existing("Early", event.start, event.end),
                                     local_zone=timezone(timedelta(hours=-12)))
    assert result.existing_start == "0001-01-01T01:00:00+00:00"
    assert result.existing_end == "0001-01-01T15:00:00+00:00"
    assert result.overlap_start == result.existing_start
    assert result.overlap_end == result.existing_end
