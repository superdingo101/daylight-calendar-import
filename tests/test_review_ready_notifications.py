"""Post-commit review-ready notification behavior."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.daylight_calendar_import.notification_preferences import (
    normalize_notification_preferences,
)
from custom_components.daylight_calendar_import.review_ready_notifications import (
    async_notify_review_ready,
)
from custom_components.daylight_calendar_import.ha_notification_sink import (
    NotificationDeliveryError,
)


def preferences(enabled=True):
    return normalize_notification_preferences({
        "enabled": enabled, "target": "notify.phone", "classes": ["review_ready"],
    })


def activity(transition="review_ready"):
    return {"id": "private-import", "transitions": [
        {"type": transition, "at": "2026-10-10T16:00:00+00:00", "event_id": None},
    ]}


@pytest.mark.asyncio
async def test_post_commit_review_ready_dispatch_is_privacy_safe():
    sink = AsyncMock(return_value=True)
    with patch(
        "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
        sink,
    ):
        await async_notify_review_ready(Mock(), activity(), preferences())
    assert sink.await_count == 1
    event = sink.await_args.args[1]
    assert event.type == "review_ready"
    assert "private-import" not in event.title + event.message


@pytest.mark.asyncio
@pytest.mark.parametrize("record, policy", [
    ({"id": "private-import", "transitions": []}, preferences()),
    (activity("received"), preferences()),
    (activity(), preferences(enabled=False)),
])
async def test_non_committed_or_unselected_records_do_not_dispatch(record, policy):
    sink = AsyncMock()
    with patch(
        "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
        sink,
    ):
        await async_notify_review_ready(Mock(), record, policy)
    sink.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_failure_cannot_undo_review_ready_commit():
    with patch(
        "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
        AsyncMock(side_effect=NotificationDeliveryError("Unavailable")),
    ):
        await async_notify_review_ready(Mock(), activity(), preferences())
