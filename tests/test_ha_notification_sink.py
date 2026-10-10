"""HA notify-service adapter tests, including actual registered entities."""

import traceback
from unittest.mock import AsyncMock, Mock

from homeassistant.components import notify
from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
import pytest

from custom_components.daylight_calendar_import.ha_notification_sink import (
    NotificationDeliveryError, async_send_ha_notification,
)
from custom_components.daylight_calendar_import.notification_preferences import (
    normalize_notification_preferences,
)
from custom_components.daylight_calendar_import.notifications import (
    notification_from_transition,
)


def event():
    return notification_from_transition(
        {"id": "sensitive-source"},
        {"type": "review_ready", "at": "2026-10-09T10:00:00+00:00",
         "event_id": "secret-event"},
    )


def policy(*, enabled=True, classes=("review_ready",), target="notify.phone"):
    return normalize_notification_preferences({
        "enabled": enabled, "classes": list(classes), "target": target,
    })


def hass_with_notify(*, registered=True, available=True, has_service=True, error=None):
    entity = Mock(entity_id="notify.phone", available=available)
    component = Mock(entities=[entity] if registered else [])
    return Mock(
        data={notify.DATA_COMPONENT: component},
        services=Mock(
            has_service=Mock(return_value=has_service),
            async_call=AsyncMock(side_effect=error),
        ),
    )


@pytest.mark.asyncio
async def test_selected_registered_entity_receives_privacy_safe_ha_service_request():
    hass = hass_with_notify()
    assert await async_send_ha_notification(hass, event(), policy()) is True
    hass.services.has_service.assert_called_once_with("notify", "send_message")
    hass.services.async_call.assert_awaited_once_with(
        "notify", "send_message",
        {"title": "Review needed", "message": "A Daylight import is ready for review."},
        target={"entity_id": "notify.phone"}, blocking=True,
    )
    assert "sensitive" not in str(hass.services.async_call.await_args)
    assert "secret" not in str(hass.services.async_call.await_args)


@pytest.mark.asyncio
async def test_disabled_or_unselected_classes_never_access_ha():
    hass = hass_with_notify()
    assert await async_send_ha_notification(hass, event(), policy(enabled=False)) is False
    assert await async_send_ha_notification(hass, event(), policy(classes=())) is False
    hass.services.has_service.assert_not_called()
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_target_skips_delivery():
    hass = hass_with_notify()
    assert await async_send_ha_notification(
        hass, event(), normalize_notification_preferences({}),
    ) is False


@pytest.mark.asyncio
async def test_missing_notify_service_fails_closed():
    hass = hass_with_notify(has_service=False)
    with pytest.raises(NotificationDeliveryError, match="service unavailable"):
        await async_send_ha_notification(hass, event(), policy())
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_notify_component_fails_closed():
    hass = hass_with_notify()
    hass.data.clear()
    with pytest.raises(NotificationDeliveryError, match="entity unavailable"):
        await async_send_ha_notification(hass, event(), policy())
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_state_without_registered_notify_entity_cannot_be_dispatched():
    hass = hass_with_notify(registered=False)
    with pytest.raises(NotificationDeliveryError, match="entity unavailable"):
        await async_send_ha_notification(hass, event(), policy())
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_but_unavailable_notify_entity_fails_closed():
    hass = hass_with_notify(available=False)
    with pytest.raises(NotificationDeliveryError, match="entity unavailable"):
        await async_send_ha_notification(hass, event(), policy())
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_registered_notify_entity_fails_closed():
    hass = hass_with_notify()
    hass.data[notify.DATA_COMPONENT].entities = [Mock(
        entity_id="notify.different", available=True,
    )]
    with pytest.raises(NotificationDeliveryError, match="entity unavailable"):
        await async_send_ha_notification(hass, event(), policy())
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_availability_exception_is_sanitized():
    class LeakingEntity:
        entity_id = "notify.phone"

        @property
        def available(self):
            raise RuntimeError("private-provider-token")

    hass = hass_with_notify()
    hass.data[notify.DATA_COMPONENT].entities = [LeakingEntity()]
    with pytest.raises(NotificationDeliveryError, match="entity unavailable") as caught:
        await async_send_ha_notification(hass, event(), policy())
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-provider-token" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_provider_service_failure_cannot_leak_exception_details():
    hass = hass_with_notify(error=RuntimeError("private-endpoint-and-token"))
    with pytest.raises(NotificationDeliveryError, match="service call failed") as caught:
        await async_send_ha_notification(hass, event(), policy())
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-endpoint-and-token" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_real_notify_entity_invoked_even_when_initial_state_is_unknown(hass):
    """Run Home Assistant's actual notify entity service path."""
    delivered = []

    class RecordingNotify(NotifyEntity):
        _attr_name = "Daylight Test Notification"
        _attr_unique_id = "daylight_notification_test"
        _attr_supported_features = NotifyEntityFeature.TITLE

        async def async_send_message(self, message, title=None):
            delivered.append((title, message))

    await notify.async_setup(hass, {})
    entity = RecordingNotify()
    await hass.data[notify.DATA_COMPONENT].async_add_entities([entity])
    assert entity.entity_id is not None
    prefs = policy(target=entity.entity_id)
    assert await async_send_ha_notification(hass, event(), prefs) is True
    assert delivered == [("Review needed", "A Daylight import is ready for review.")]


@pytest.mark.asyncio
async def test_real_orphan_state_does_not_count_as_registered_notify_entity(hass):
    await notify.async_setup(hass, {})
    hass.states.async_set("notify.orphan", "prior")
    prefs = policy(target="notify.orphan")
    with pytest.raises(NotificationDeliveryError, match="entity unavailable"):
        await async_send_ha_notification(hass, event(), prefs)
