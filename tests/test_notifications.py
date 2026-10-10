"""Tests for stable, private lifecycle notification projection."""

import pytest

from custom_components.daylight_calendar_import.notifications import (
    NotificationEvent,
    notification_from_recovered_write,
    notification_from_transition,
)


@pytest.mark.parametrize(
    ("stored_type", "expected_type", "title", "message", "severity"),
    [
        ("review_ready", "review_ready", "Review needed",
         "A Daylight import is ready for review.", "info"),
        ("calendar_created", "calendar_created", "Event added",
         "Daylight added a calendar event.", "success"),
        ("calendar_create_failed", "calendar_create_failed", "Calendar write failed",
         "Daylight could not add an event.", "error"),
        ("calendar_write_uncertain", "calendar_write_uncertain", "Check calendar write",
         "Daylight cannot confirm whether an event was added. Review before retrying.", "warning"),
        ("conflict_detected", "conflict_detected", "Calendar conflict",
         "Daylight found a possible scheduling conflict.", "warning"),
        ("parse_failed", "parse_failed", "Import processing failed",
         "Daylight could not process a source.", "error"),
        ("failed", "parse_failed", "Import processing failed",
         "Daylight could not process a source.", "error"),
    ],
)
def test_all_supported_transitions_are_stable_and_private(
    stored_type, expected_type, title, message, severity,
):
    activity = {"id": "import-private-123", "source_title": "private meeting passcode 87654"}
    transition = {"type": stored_type, "at": "2026-10-09T12:00:00+00:00",
                  "event_id": "event-private-456"}
    result = notification_from_transition(activity, transition)
    assert result == NotificationEvent(
        type=expected_type,
        idempotency_key=result.idempotency_key,
        occurred_at="2026-10-09T12:00:00+00:00",
        import_id="import-private-123",
        event_id="event-private-456",
        title=title,
        message=message,
        severity=severity,
    )
    assert len(result.idempotency_key) == 64
    assert result == notification_from_transition(activity, transition)
    assert "private" not in result.title + result.message
    assert "87654" not in result.title + result.message
    assert "import-private" not in result.idempotency_key
    assert "event-private" not in result.idempotency_key


def test_golden_idempotency_key_protects_encoding_across_upgrades():
    assert notification_from_transition(
        {"id": "import-private-123"},
        {"type": "review_ready", "at": "2026-10-09T12:00:00+00:00",
         "event_id": "event-private-456"},
    ).idempotency_key == "66bdcb5dacfa0159d38985ebdebb5177733a9d5e896d0fba140a2a066a18f046"


def test_only_supported_stored_transitions_emit_notifications():
    activity = {"id": "import"}
    assert notification_from_transition(activity, {"type": "received"}) is None
    assert notification_from_transition(activity, {"type": ["invalid"]}) is None
    assert notification_from_transition(
        activity, {"type": "calendar_write_started"},
    ) is None


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


def test_recovered_write_started_is_projected_only_from_uncertain_pending_state():
    checkpoint = {"type": "calendar_write_started", "at": "durable-checkpoint",
                  "event_id": "event-a"}
    activity = {"id": "import-a", "transitions": [
        {"type": "review_ready", "at": "before", "event_id": None}, checkpoint,
    ]}
    result = notification_from_recovered_write(
        activity, event_id="event-a", persisted_status="write_uncertain",
    )
    expected = notification_from_transition(
        activity, {**checkpoint, "type": "calendar_write_uncertain"},
    )
    assert result == expected
    assert result.type == "calendar_write_uncertain"
    assert result.occurred_at == "durable-checkpoint"
    assert result == notification_from_recovered_write(
        activity, event_id="event-a", persisted_status="write_uncertain",
    )
    # A live write in progress must never be misreported as already uncertain.
    assert notification_from_recovered_write(
        activity, event_id="event-a", persisted_status="pending",
    ) is None
    assert notification_from_recovered_write(
        activity, event_id="another-event", persisted_status="write_uncertain",
    ) is None


def test_recovery_projection_does_not_reemit_resolved_or_explicit_uncertain_transitions():
    checkpoint = {"type": "calendar_write_started", "at": "before", "event_id": "event-a"}
    for next_type in ("calendar_created", "calendar_write_uncertain", "review_ready"):
        activity = {"id": "import-a", "transitions": [
            checkpoint, {"type": next_type, "at": "after", "event_id": "event-a"},
        ]}
        assert notification_from_recovered_write(
            activity, event_id="event-a", persisted_status="write_uncertain",
        ) is None
