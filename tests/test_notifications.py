"""Tests for stable, private lifecycle notification projection."""

import pytest

from custom_components.daylight_calendar_import.notifications import (
    NotificationEvent,
    notification_from_transition,
)


@pytest.mark.parametrize(
    ("stored_type", "expected_type", "severity"),
    [
        ("review_ready", "review_ready", "info"),
        ("calendar_created", "calendar_created", "success"),
        ("calendar_create_failed", "calendar_create_failed", "error"),
        ("calendar_write_uncertain", "calendar_write_uncertain", "warning"),
        ("conflict_detected", "conflict_detected", "warning"),
        ("parse_failed", "parse_failed", "error"),
        ("failed", "parse_failed", "error"),
    ],
)
def test_all_supported_transitions_are_stable_and_private(stored_type, expected_type, severity):
    activity = {"id": "import-private-123", "source_title": "private meeting passcode 87654"}
    transition = {"type": stored_type, "at": "2026-10-09T12:00:00+00:00",
                  "event_id": "event-private-456"}
    result = notification_from_transition(activity, transition)
    assert isinstance(result, NotificationEvent)
    assert result.type == expected_type
    assert result.severity == severity
    assert result.event_id == "event-private-456"
    assert result.import_id == "import-private-123"
    assert len(result.idempotency_key) == 64
    assert result == notification_from_transition(activity, transition)
    assert "private" not in result.title + result.message
    assert "87654" not in result.title + result.message
    assert "import-private" not in result.idempotency_key
    assert "event-private" not in result.idempotency_key


def test_only_supported_stored_transitions_emit_notifications():
    activity = {"id": "import"}
    assert notification_from_transition(activity, {"type": "received"}) is None
    assert notification_from_transition(activity, {"type": ["invalid"]}) is None


def test_transition_identity_changes_with_event_and_time_and_handles_source_level():
    activity = {"id": "import"}
    transition = {"type": "review_ready", "at": "first"}
    first = notification_from_transition(activity, transition)
    assert first.event_id is None
    assert first.idempotency_key != notification_from_transition(
        activity, {**transition, "at": "second"}
    ).idempotency_key
    assert first.idempotency_key != notification_from_transition(
        activity, {**transition, "event_id": "event"}
    ).idempotency_key
    assert first.idempotency_key != notification_from_transition(
        {"id": "another"}, transition
    ).idempotency_key
