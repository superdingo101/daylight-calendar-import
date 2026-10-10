"""Best-effort review-ready dispatch from an authoritative committed store.

This is a deliberately non-durable adapter: it never blocks or changes pending
state. A later durable notification outbox handles retries and restarts.
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)
_DELIVERY_TIMEOUT_SECONDS = 15

from homeassistant.core import HomeAssistant

from .ha_notification_sink import async_send_ha_notification
from .notification_preferences import NotificationPreferences
from .notifications import notification_from_transition


async def async_notify_review_ready(
    hass: HomeAssistant, activity: dict, preferences: NotificationPreferences,
) -> None:
    """Emit an opted-in, source-private notification for the committed transition.

    Calling code must invoke this *after* the pending store has persisted and
    exposed its committed lifecycle record. Never synthesize a transition or
    notify for replayed pre-existing review items after a reload.
    """
    transitions = activity.get("transitions", ())
    if not transitions:
        return
    latest = transitions[-1]
    if latest.get("type") != "review_ready":
        return
    event = notification_from_transition(activity, latest)
    if event is None or not preferences.permits(event.type):
        return
    try:
        await asyncio.wait_for(
            async_send_ha_notification(hass, event, preferences),
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
    except Exception:
        # Never log source content or provider exceptions; notification failure
        # is not a failure of the durable accepted import.
        _LOGGER.warning("Daylight notification delivery failed; verify the configured notify entity")

