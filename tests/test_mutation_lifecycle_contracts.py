"""Exact lifecycle and failure-admission contracts for mutation testing."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import custom_components.daylight_calendar_import as integration
from custom_components.daylight_calendar_import.const import DOMAIN
from test_init import FakeHass, entry


@pytest.mark.parametrize("guard", [False, True, None])
@pytest.mark.parametrize("raises", [False, True])
async def test_unload_failure_restores_admission_only_when_safe(guard, raises):
    hass = FakeHass()
    config = entry()
    store = SimpleNamespace(
        accepting_services=True,
        notification_tasks=set(),
        notification_delivery_lock=None,
    )
    if guard is not None:
        store.calendar_settings_reload_guard = guard
    hass.data[DOMAIN] = {config.entry_id: store}
    if raises:
        hass.config_entries.async_unload_platforms = AsyncMock(
            side_effect=RuntimeError("platform unavailable")
        )
        with pytest.raises(RuntimeError, match="platform unavailable"):
            await integration.async_unload_entry(hass, config)
    else:
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
        assert await integration.async_unload_entry(hass, config) is False
    assert store.accepting_services is (not bool(guard))
    assert hass.data[DOMAIN][config.entry_id] is store
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        config, integration.PLATFORMS
    )


async def test_successful_unload_cleans_handlers_and_all_notification_hooks(monkeypatch):
    hass = FakeHass()
    config = entry()
    unsubscribe = Mock()
    store = SimpleNamespace(
        accepting_services=True, notification_tasks=set(),
        notification_delivery_lock=None,
        notification_unsubscribe=unsubscribe,
        on_notifications=Mock(), on_review_ready=Mock(),
        active_submissions=set(), active_service_handlers=set(),
        email_runtime=None,
    )
    hass.data[DOMAIN] = {config.entry_id: store}
    remove_panel = Mock()
    monkeypatch.setattr(integration, "async_remove_review_panel", remove_panel)
    assert await integration.async_unload_entry(hass, config) is True
    assert store.accepting_services is False
    assert store.on_notifications is None
    assert store.on_review_ready is None
    assert hass.data[DOMAIN] == {}
    unsubscribe.assert_called_once_with()
    remove_panel.assert_called_once_with(hass)
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        config, integration.PLATFORMS
    )


async def test_notification_cancellation_waits_for_durable_delivery_lock():
    lock = asyncio.Lock()
    await lock.acquire()
    store = SimpleNamespace(notification_tasks=set(), notification_delivery_lock=lock)
    task = asyncio.create_task(integration._async_cancel_notification_tasks(store))
    await asyncio.sleep(0)
    try:
        assert not task.done(), "Unload must wait for the durable delivery journal"
    finally:
        lock.release()
    await asyncio.wait_for(task, timeout=1)
