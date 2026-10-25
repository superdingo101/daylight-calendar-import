"""Home Assistant notify-entity transport.

The result describes successful submission to Home Assistant, not receipt by
the destination device. Durable notification outcomes/retries are separate from
the calendar-import lifecycle and belong to a later outbox integration.
"""

from __future__ import annotations

from homeassistant.components.notify import DATA_COMPONENT
from homeassistant.core import HomeAssistant

from .notification_preferences import NotificationPreferences
from .notifications import NotificationEvent


class NotificationDeliveryError(RuntimeError):
    """A configured HA notify entity is unavailable or service delivery fails."""


async def async_send_ha_notification(
    hass: HomeAssistant,
    event: NotificationEvent,
    preferences: NotificationPreferences,
) -> bool:
    """Submit an explicitly opted-in event to a registered HA notify entity.

    True means that Home Assistant completed the blocking service request,
    *not* that a remote phone/device received the message. HA 2026.7 does not
    expose an atomic dispatch receipt via notify.send_message. Preflight checks
    the actual registered NotifyEntity (not just its potentially stale State);
    a removal during the service call remains an unavoidable race. A future
    durable outbox must not promise exactly-once downstream delivery.
    """
    if not preferences.permits(event.type):
        return False
    target = preferences.target

    if not hass.services.has_service("notify", "send_message"):
        raise NotificationDeliveryError("Home Assistant notification service unavailable")

    # Home Assistant's generic service resolver silently succeeds when no
    # registered entity matches. Check the same EntityComponent registry used
    # by the resolver, not hass.states, which may contain orphan/unknown states.
    component = hass.data.get(DATA_COMPONENT)
    registered = False
    if component is not None:
        try:
            registered = any(
                entity.entity_id == target and entity.available
                for entity in component.entities
            )
        except Exception:
            # Provider availability code may raise sensitive details. Do not
            # let those exceptions or tracebacks escape this privacy boundary.
            registered = False
    if not registered:
        raise NotificationDeliveryError("Selected Home Assistant notification entity unavailable")

    # Do not chain provider exceptions: a traceback could disclose credentials
    # or private response data. Raise the generic error outside the except.
    failed = False
    try:
        await hass.services.async_call(
            "notify",
            "send_message",
            {"title": event.title, "message": event.message},
            target={"entity_id": target},
            blocking=True,
        )
    except Exception:
        failed = True
    if failed:
        raise NotificationDeliveryError("Home Assistant notification service call failed")
    return True
