"""Channel-independent, privacy-minimal events derived from durable lifecycle records.

This module only prepares notification events. It does not send them or mutate
the lifecycle ledger; delivery and preferences are separate responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from collections.abc import Mapping


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
    """Project a validated committed transition, never live write-started state.

    Persisted activity may be legacy or damaged. Malformed records are ignored
    so one record cannot interrupt later delivery. Recovery of ambiguous writes
    is *not* inferred from this prunable history: a future durable notification
    outbox must observe persisted PendingEvent.write_attempt and status after
    PendingImportStore.async_load, with explicit recovery provenance.
    """
    if not isinstance(activity, Mapping) or not isinstance(transition, Mapping):
        return None
    raw_type = transition.get("type")
    if not isinstance(raw_type, str):
        return None
    template = _TEMPLATES.get(raw_type)
    if template is None:
        return None
    import_id = activity.get("id")
    occurred_at = transition.get("at")
    event_id = transition.get("event_id")
    if (
        not isinstance(import_id, str) or not import_id.strip()
        or not isinstance(occurred_at, str) or not occurred_at.strip()
        or (event_id is not None and (not isinstance(event_id, str) or not event_id.strip()))
    ):
        return None
    kind, title, message, severity = template
    # The stored identity is immutable. Do not use a transition's position in a
    # bounded history as an idempotency key.
    identity = "\x00".join((import_id, raw_type, occurred_at, str(event_id)))
    key = sha256(identity.encode("utf-8")).hexdigest()
    return NotificationEvent(
        type=kind,
        idempotency_key=key,
        occurred_at=occurred_at,
        import_id=import_id,
        event_id=event_id,
        title=title,
        message=message,
        severity=severity,
    )
