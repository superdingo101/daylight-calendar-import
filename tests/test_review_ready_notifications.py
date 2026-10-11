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
async def test_post_commit_review_ready_dispatch_is_privacy_safe(caplog):
    sink = AsyncMock(return_value=True)
    hass = Mock()
    prefs = preferences()
    with patch(
        "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
        sink,
    ):
        await async_notify_review_ready(hass, activity(), prefs)
    sink.assert_awaited_once()
    actual_hass, event, actual_prefs = sink.await_args.args
    assert actual_hass is hass
    assert actual_prefs is prefs
    assert "Daylight notification delivery failed" not in caplog.text
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
    assert caplog.text.count("Daylight notification delivery failed; verify the configured notify entity") == 2
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
    assert "Daylight notification delivery failed; verify the configured notify entity" in caplog.text


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


@pytest.mark.asyncio
async def test_delivery_deadline_does_not_wait_for_cancellation_resistant_provider(
    monkeypatch, caplog,
):
    import asyncio
    import time

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.review_ready_notifications._DELIVERY_TIMEOUT_SECONDS",
        0.002,
    )
    child_done = asyncio.Event()
    allow_finish = asyncio.Event()

    async def ignores_cancel(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await allow_finish.wait()
            # The child may raise only after the parent has timed out; its
            # exception must be consumed and never reveal provider secrets.
            raise RuntimeError("secret provider error")
        finally:
            child_done.set()

    started = time.monotonic()
    with patch(
        "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
        ignores_cancel,
    ):
        await async_notify_review_ready(Mock(), activity(), preferences())
        assert time.monotonic() - started < 0.5
        allow_finish.set()
        await asyncio.wait_for(child_done.wait(), timeout=1)
        await asyncio.sleep(0)
    assert "secret" not in caplog.text
    assert "Daylight notification delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_unload_deadline_does_not_wait_for_stubborn_notification_task(
    monkeypatch, caplog,
):
    import asyncio
    from types import SimpleNamespace
    from custom_components.daylight_calendar_import import _async_cancel_notification_tasks

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._NOTIFICATION_CANCEL_GRACE_SECONDS",
        0.002,
    )
    release = asyncio.Event()
    started = asyncio.Event()
    async def ignores_cancel():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
    task = asyncio.create_task(ignores_cancel())
    await started.wait()
    store = SimpleNamespace(notification_tasks={task})
    await asyncio.wait_for(_async_cancel_notification_tasks(store), timeout=0.5)
    assert not task.done()
    assert "did not stop before unload deadline" in caplog.text
    release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_cancellation_resistant_provider_stays_tracked_until_it_finishes(
    monkeypatch,
):
    import asyncio

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.review_ready_notifications._DELIVERY_TIMEOUT_SECONDS",
        0.002,
    )
    registry = set()
    child_ready = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_provider(*args):
        child_ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    with patch(
        "custom_components.daylight_calendar_import.review_ready_notifications.async_send_ha_notification",
        stubborn_provider,
    ):
        await async_notify_review_ready(Mock(), activity(), preferences(), registry)
        assert child_ready.is_set()
        assert len(registry) == 1
        task = next(iter(registry))
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, timeout=1)
        await asyncio.sleep(0)
    assert registry == set()
