"""Channel-independent, privacy-minimal events derived from durable lifecycle records.

This module only prepares notification events. It does not send them or mutate
the lifecycle ledger; delivery and preferences are separate responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping


# The types here are stable user-facing preference categories. Stored lifecycle
# transitions are deliberately mapped here rather than becoming a notify API.
_TEMPLATES = {
    "review_ready": ("review_ready", "Review needed", "A Daylight import is ready for review.", "info"),
    "calendar_created": ("calendar_created", "Event added", "Daylight added a calendar event.", "success"),
    "calendar_create_failed": ("calendar_create_failed", "Calendar write failed", "Daylight could not add an event.", "error"),
    "calendar_write_uncertain": ("calendar_write_uncertain", "Check calendar write", "Daylight cannot confirm whether an event was added. Review before retrying.", "warning"),
    "conflict_detected": ("conflict_detected", "Calendar conflict", "Daylight found a possible scheduling conflict.", "warning"),
    "parse_failed": ("parse_failed", "Import processing failed", "Daylight could not process a source.", "error"),
    # Existing source activity records use 'failed' for terminal processing
    # failures, including parser failure. The public category is parse_failed.
    "failed": ("parse_failed", "Import processing failed", "Daylight could not process a source.", "error"),
}


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """One immutable notification request, independent of delivery channel."""

    type: str
    idempotency_key: str
    occurred_at: str
    import_id: str
    event_id: str | None
    title: str
    message: str
    severity: str


def notification_from_transition(
    activity: Mapping[str, object], transition: Mapping[str, object],
) -> NotificationEvent | None:
    """Project a persisted lifecycle transition without copying source details.

    The key uses stable stored transition fields, never a list offset (history
    is bounded/pruned), and cannot reveal the import/event identifiers to an
    external notification destination.
    """
    raw_type = transition.get("type")
    if not isinstance(raw_type, str):
        return None
    template = _TEMPLATES.get(raw_type)
    if template is None:
        return None
    kind, title, message, severity = template
    import_id = activity["id"]
    occurred_at = transition["at"]
    event_id = transition.get("event_id")
    # Activity/transition records come from the validated durable local store.
    identity = "\x00".join((str(import_id), raw_type, str(occurred_at), str(event_id)))
    key = sha256(identity.encode("utf-8")).hexdigest()
    return NotificationEvent(
        type=kind,
        idempotency_key=key,
        occurred_at=str(occurred_at),
        import_id=str(import_id),
        event_id=str(event_id) if event_id is not None else None,
        title=title,
        message=message,
        severity=severity,
    )
