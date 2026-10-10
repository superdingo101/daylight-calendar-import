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


@pytest.mark.asyncio
async def test_notification_delivery_failures_are_sanitized(caplog):
    import asyncio

    for cause in [NotificationDeliveryError("private provider token"),
                  RuntimeError("private source details")]:
        with patch(
            "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
            AsyncMock(side_effect=cause),
        ):
            await async_notify_review_ready(Mock(), activity(), preferences())
    assert caplog.text.count("Daylight notification delivery failed") == 2
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_notification_delivery_timeout_is_bounded_and_private(caplog, monkeypatch):
    import asyncio
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.review_ready_notifications._DELIVERY_TIMEOUT_SECONDS",
        0.001,
    )
    async def blocked(*args):
        await asyncio.sleep(60)
    with patch(
        "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
        blocked,
    ):
        await async_notify_review_ready(Mock(), activity(), preferences())
    assert "Daylight notification delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_cancel_entry_owned_notification_tasks():
    import asyncio
    from types import SimpleNamespace
    from custom_components.daylight_calendar_import import _async_cancel_notification_tasks

    async def blocked():
        await asyncio.Event().wait()
    task = asyncio.create_task(blocked())
    store = SimpleNamespace(notification_tasks={task})
    await _async_cancel_notification_tasks(store)
    assert task.cancelled()
    await _async_cancel_notification_tasks(SimpleNamespace(notification_tasks=set()))
