"""Private notification identities, independent of prunable activity history.

Only transition coordinates are persisted. Messages are regenerated from the
safe notification contract; source text and provider errors never enter this
journal. Delivery state and retries are added separately from lifecycle truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .notifications import NotificationEvent, notification_from_transition


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """One durable, immutable request, keyed by the transition identity."""

    import_id: str
    transition_type: str
    occurred_at: str
    event_id: str | None

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
        """Serialize identity only, without mutable activity or private content."""
        return {"import_id": self.import_id, "transition_type": self.transition_type,
                "occurred_at": self.occurred_at, "event_id": self.event_id}

    @classmethod
    def from_dict(cls, raw: Mapping) -> NotificationRecord:
        """Fail closed on invalid persisted identity rather than silently dropping it."""
        record = cls(raw["import_id"], raw["transition_type"], raw["occurred_at"], raw["event_id"])
        record.event
        return record


def notification_record(activity: Mapping, transition: Mapping) -> NotificationRecord | None:
    """Prepare a journal record only for a validated notification transition."""
    event = notification_from_transition(activity, transition)
    if event is None:
        return None
    return NotificationRecord(event.import_id, transition["type"], event.occurred_at, event.event_id)
