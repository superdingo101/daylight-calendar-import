"""Private notification identities, independent of prunable activity history.

Transition coordinates, the notify entity, and bounded delivery state are
persisted until a retention policy is introduced. Messages are regenerated from
the safe contract; source text and provider errors never enter this journal.
Delivery state remains separate from calendar lifecycle truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .notification_preferences import normalize_notification_preferences

from .notifications import NotificationEvent, notification_from_transition


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """One durable, immutable request, keyed by the transition identity."""

    import_id: str
    transition_type: str
    occurred_at: str
    event_id: str | None
    target: str | None = None
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: str | None = None
    failure: str | None = None

    @property
    def event(self) -> NotificationEvent:
        """Regenerate the public message from validated transition coordinates."""
        event = notification_from_transition(
            {"id": self.import_id},
            {"type": self.transition_type, "at": self.occurred_at, "event_id": self.event_id},
        )
        if event is None:
            raise ValueError("Invalid notification identity")
        return event

    def as_dict(self) -> dict:
        """Serialize identity and bounded delivery state without source content."""
        return {"import_id": self.import_id, "transition_type": self.transition_type,
                "occurred_at": self.occurred_at, "event_id": self.event_id,
                "target": self.target, "status": self.status, "attempts": self.attempts,
                "next_attempt_at": self.next_attempt_at, "failure": self.failure}

    @classmethod
    def from_dict(cls, raw: Mapping) -> NotificationRecord:
        """Fail closed on invalid persisted identity rather than silently dropping it."""
        record = cls(raw["import_id"], raw["transition_type"], raw["occurred_at"], raw["event_id"],
                     raw.get("target"), raw.get("status", "pending"), raw.get("attempts", 0),
                     raw.get("next_attempt_at"), raw.get("failure"))
        normalize_notification_preferences({"target": record.target})
        if (record.status not in {"pending", "sent", "failed", "cancelled"}
                or type(record.attempts) is not int or not 0 <= record.attempts <= 3
                or record.failure not in {None, "delivery_failed", "delivery_interrupted", "policy_changed"}):
            raise ValueError("Invalid notification delivery state")
        if record.next_attempt_at is not None:
            if datetime.fromisoformat(record.next_attempt_at).tzinfo is None:
                raise ValueError("Notification retry time must include a timezone")
        record.event
        return record


def notification_record(activity: Mapping, transition: Mapping) -> NotificationRecord | None:
    """Prepare a journal record only for a validated notification transition."""
    event = notification_from_transition(activity, transition)
    if event is None:
        return None
    return NotificationRecord(event.import_id, transition["type"], event.occurred_at, event.event_id)
