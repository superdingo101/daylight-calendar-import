"""Tests for domain models."""

import pytest

from custom_components.daylight_calendar_import.models import DraftValidationError, EventDraft


def timed(**overrides):
    data = {
        "title": "Soccer Practice",
        "start": "2026-10-08T17:30:00-07:00",
        "end": "2026-10-08T18:30:00-07:00",
        "all_day": False,
        "location": "Community Park",
        "description": "Bring water",
        "confidence": 0.95,
    }
    data.update(overrides)
    return data


def test_valid_timed_event():
    draft = EventDraft.from_mapping(timed())
    assert draft.title == "Soccer Practice"
    assert draft.as_dict()["confidence"] == 0.95


def test_valid_all_day_event_and_optional_cleanup():
    draft = EventDraft.from_mapping(
        timed(
            title="  Picture Day  ",
            start="2026-10-12",
            end="2026-10-13",
            all_day=True,
            location=" ",
            description="",
            confidence=1,
        )
    )
    assert draft.title == "Picture Day"
    assert draft.location is None
    assert draft.description is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"title": ""}, "title must be a non-empty string"),
        ({"location": 123}, "location must be a string or null"),
        ({"all_day": "false"}, "all_day must be a boolean"),
        ({"confidence": True}, "confidence must be a number"),
        ({"confidence": "high"}, "confidence must be a number"),
        ({"confidence": 1.1}, "confidence must be between 0 and 1"),
        ({"start": "bad"}, "start/end must be valid ISO values"),
        (
            {"start": "2026-10-08T17:30:00", "end": "2026-10-08T18:30:00"},
            "timed events must include timezone offsets",
        ),
        ({"end": "2026-10-08T16:30:00-07:00"}, "end must be after start"),
    ],
)
def test_invalid_event(changes, message):
    with pytest.raises(DraftValidationError, match=message):
        EventDraft.from_mapping(timed(**changes))


def test_missing_and_zero_confidence_use_safe_boundary_values():
    without_confidence = timed()
    without_confidence.pop("confidence")
    assert EventDraft.from_mapping(without_confidence).confidence == 0.0
    assert EventDraft.from_mapping(timed(confidence=0)).confidence == 0.0


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-10-08T17:30:00", "2026-10-08T18:30:00-07:00"),
        ("2026-10-08T17:30:00-07:00", "2026-10-08T18:30:00"),
    ],
)
def test_timed_event_rejects_either_missing_timezone_offset(start, end):
    with pytest.raises(DraftValidationError, match="timezone offsets"):
        EventDraft.from_mapping(timed(start=start, end=end))


def test_event_rejects_zero_duration():
    with pytest.raises(DraftValidationError, match="end must be after start"):
        EventDraft.from_mapping(
            timed(end="2026-10-08T17:30:00-07:00")
        )
