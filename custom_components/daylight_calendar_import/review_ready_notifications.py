"""Best-effort review-ready dispatch from an authoritative committed store.

This is a deliberately non-durable adapter: it never blocks or changes pending
state. A later durable notification outbox handles retries and restarts.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_DELIVERY_TIMEOUT_SECONDS = 15

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
    # wait_for() waits for a child to *acknowledge cancellation*. A broken HA
    # notify handler can suppress CancelledError, leaving entry unload blocked
    # indefinitely. Use a deadline that does not await child cancellation.
    delivery = asyncio.create_task(async_send_ha_notification(hass, event, preferences))
    def consume_result(task: asyncio.Task) -> None:
        # A cancellation-resistant provider may finish after the dispatcher
        # returns. Retrieve any exception without logging private details.
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                pass

    delivery.add_done_callback(consume_result)
    try:
        done, _ = await asyncio.wait({delivery}, timeout=_DELIVERY_TIMEOUT_SECONDS)
        if not done:
            raise TimeoutError("Notification delivery exceeded its deadline")
        delivery.result()
    except Exception:
        # Never log source content or provider exceptions; notification failure
        # is not a failure of the durable accepted import.
        _LOGGER.warning("Daylight notification delivery failed; verify the configured notify entity")
    finally:
        if not delivery.done():
            # Never block this task or config-entry teardown waiting for a
            # cancellation-resistant integration to finish.
            delivery.cancel()

