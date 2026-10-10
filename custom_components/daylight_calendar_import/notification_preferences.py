"""Validated, opt-in notification policy independent of delivery transport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.core import valid_entity_id


NOTIFICATION_CLASSES = frozenset({
    "review_ready", "calendar_created", "calendar_create_failed",
    "calendar_write_uncertain", "conflict_detected", "parse_failed",
})


class NotificationPreferencesError(ValueError):
    """Raised when user-supplied notification preferences are unsafe."""


@dataclass(frozen=True, slots=True)
class NotificationPreferences:
    """Conservative local notification defaults and bounded delivery target."""

    enabled: bool = False
    classes: frozenset[str] = frozenset()
    target: str | None = None

    def permits(self, notification_type: str) -> bool:
        """Whether an explicitly selected event class may be delivered."""
        return self.enabled and self.target is not None and notification_type in self.classes


def normalize_notification_preferences(raw: Mapping[str, object]) -> NotificationPreferences:
    """Validate a settings payload without inferring consent from defaults."""
    if not isinstance(raw, Mapping):
        raise NotificationPreferencesError("Notification preferences must be an object")
    if set(raw) - {"enabled", "classes", "target"}:
        raise NotificationPreferencesError("Unknown notification setting")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise NotificationPreferencesError("Notification enabled must be a boolean")
    classes = raw.get("classes", ())
    if not isinstance(classes, (list, tuple, frozenset)) or any(
        not isinstance(kind, str) or kind not in NOTIFICATION_CLASSES for kind in classes
    ):
        raise NotificationPreferencesError("Unknown notification class")
    target = raw.get("target")
    if target is not None and (
        not isinstance(target, str) or len(target) > 128
        or not target.isprintable()
        or not target.startswith("notify.") or not valid_entity_id(target)
    ):
        raise NotificationPreferencesError("Notification target must be a notify entity")
    if enabled and target is None:
        raise NotificationPreferencesError("Select a notification target before enabling")
    return NotificationPreferences(enabled=enabled, classes=frozenset(classes), target=target)


def notification_preferences_snapshot(preferences: NotificationPreferences) -> dict[str, object]:
    """Expose only bounded non-secret configuration."""
    return {
        "enabled": preferences.enabled,
        "classes": sorted(preferences.classes),
        "target": preferences.target,
    }
