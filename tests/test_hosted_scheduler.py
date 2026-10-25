"""Opt-in maintenance shares entry task ownership, durable budgets and HA cleanup."""
# ruff: noqa: F811 -- shared real-disk policy fixture

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.daylight_calendar_import import hosted_runtime as module
from custom_components.daylight_calendar_import.const import DOMAIN
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedConnectionResult,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedTransportError,
)
from tests.test_hosted_automation import ACTIVE, NEAR, automatic  # noqa: F401
from tests.test_hosted_runtime import SERVICE, entry


@pytest.fixture
async def scheduled(hass, automatic, monkeypatch):
    _policy, store, controller = automatic
    store.async_load = AsyncMock(return_value=store.snapshot)
    monkeypatch.setattr(module, "HostedCredentialStore", lambda *_: store)
    monkeypatch.setattr(module, "HostedLifecycleController", lambda *_: controller)
    r = await module.async_setup_hosted_runtime(hass, entry({
        module.CONF_HOSTED_SERVICE: SERVICE, module.CONF_HOSTED_AUTOMATIC: True,
    }))
    assert r.snapshot()["automatic_lifecycle"] is True and not r.accepting
    assert not controller.async_reconcile.called and r._cancel_timer is None
    yield r, store, controller
    await r.async_stop()


async def tick(hass, freezer, seconds):
    freezer.tick(seconds)
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done()


async def test_optin_timer_status_once_then_verified_expiry_and_private_invalidation(scheduled, hass, freezer):
    r, store, controller = scheduled
    events = []
    unsub = async_dispatcher_connect(hass, module.hosted_signal("hosted-entry"), lambda: events.append(r.snapshot()))
    r.resume()
    r.resume()
    assert r._cancel_timer is not None and r._cancel_stop is not None
    await tick(hass, freezer, 1)
    controller.async_reconcile.assert_awaited_once()
    assert r.snapshot()["state"] == "connected"
    assert events[-1] == r.snapshot() and not r.busy
    assert store.snapshot.automation.status.attempts == 0
    await tick(hass, freezer, 120)
    assert controller.async_reconcile.await_count == 1
    assert not controller.async_rotate.called
    r.pause()
    assert r._cancel_timer is None
    await tick(hass, freezer, 100)
    assert controller.async_reconcile.await_count == 1
    r.resume()
    assert r._cancel_timer is not None
    unsub()


async def test_only_exact_boolean_optin_can_start_maintenance(hass, monkeypatch):
    factory = AsyncMock()
    monkeypatch.setattr(module, "HostedCredentialStore", factory)
    for value in [None, False, 1, "true", {}]:
        r = await module.async_setup_hosted_runtime(hass, entry({module.CONF_HOSTED_AUTOMATIC: value}))
        r.resume()
        assert r.snapshot()["automatic_lifecycle"] is False and r._cancel_timer is None
        await r.async_stop()
    factory.assert_not_called()


async def test_optin_without_saved_consent_has_no_network_or_timer(scheduled, hass, freezer):
    r, store, controller = scheduled
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, processing_consent=False, current=None))
    r.resume()
    assert r._cancel_timer is None
    await tick(hass, freezer, 100)
    controller.async_reconcile.assert_not_called()


async def test_retry_after_and_exhaustion_stop_with_repair_and_manual_recovery(scheduled, hass, freezer):
    r, store, controller = scheduled
    controller.async_reconcile.side_effect = HostedTransportError("rate_limited", status=429, retryable=True, retry_after=300)
    r.resume()
    await tick(hass, freezer, 1)
    assert r.snapshot()["state"] == "service_unavailable"
    await tick(hass, freezer, 300)
    assert controller.async_reconcile.await_count == 1
    await tick(hass, freezer, 1)
    assert controller.async_reconcile.await_count == 2
    for _ in range(4):
        await tick(hass, freezer, 301)
    assert controller.async_reconcile.await_count == 6
    assert r.snapshot()["state"] == "automatic_attention_required"
    assert r._cancel_timer is None
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry") is not None
    await tick(hass, freezer, 10000)
    assert controller.async_reconcile.await_count == 6
    controller.async_reconcile.side_effect = None
    controller.async_reconcile.return_value = ACTIVE
    result = await r.async_action("reconcile")
    assert result["state"] == "connected"
    assert store.snapshot.automation.status.attempts == 0
    assert registry.async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry") is None


async def test_near_expiry_renewal_and_uncertain_intent_never_reissued(scheduled, hass, freezer):
    r, store, controller = scheduled
    r._state = NEAR
    async def uncertain():
        old = store.snapshot
        await store.async_checkpoint(old, replace(old, rotation_not_before=(datetime.now(UTC)+timedelta(seconds=30)).isoformat()))
        raise HostedTransportError("transport_error", retryable=True)
    controller.async_rotate.side_effect = uncertain
    r.resume()
    await tick(hass, freezer, 1)
    controller.async_rotate.assert_awaited_once()
    # The renewal failure gates recovery too; then verify authority and stop.
    await tick(hass, freezer, 1)
    controller.async_reconcile.assert_not_called()
    await tick(hass, freezer, 15)
    assert r.snapshot()["state"] == "automatic_attention_required"
    assert r._cancel_timer is None
    await tick(hass, freezer, 5000)
    assert controller.async_rotate.await_count == 1
    result = await r.async_action("reconcile")
    assert result["state"] == "automatic_attention_required"


async def test_one_owned_operation_and_shutdown_cancels_automatic_request(scheduled, hass, freezer):
    r, _store, controller = scheduled
    entered = asyncio.Event()
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
    controller.async_reconcile.side_effect = blocked
    r.resume()
    freezer.tick(1)
    async_fire_time_changed(hass, datetime.now(UTC))
    await entered.wait()
    assert r.busy and r._task in r._tasks
    with pytest.raises(module.HostedRuntimeError, match="hosted_busy"):
        await r.async_action("rotate")
    r._automatic_tick(None)  # No queued second action when already busy.
    assert controller.async_reconcile.await_count == 1
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert r._stopped and not r.accepting and r._cancel_timer is None and r._cancel_stop is None
    assert not r.busy
    r._automatic_tick(None)
    assert controller.async_reconcile.await_count == 1


async def test_manual_action_cancels_timer_and_reschedules_only_after_completion(scheduled):
    r, _store, controller = scheduled
    r.resume()
    old = r._cancel_timer
    result = await r.async_action("reconcile")
    assert result["state"] == "connected"
    assert r._cancel_timer is not None and r._cancel_timer is not old
    assert controller.async_reconcile.await_count == 1


async def test_noop_step_has_no_network_and_stale_timer_is_harmless(scheduled):
    r, _store, controller = scheduled
    r.resume()
    r._state = ACTIVE
    # Simulate the timer consuming its scheduled handle before its callback.
    r._cancel_timer()
    r._cancel_timer = None
    r._automatic_tick(None)
    await asyncio.gather(*tuple(r._tasks))
    await asyncio.sleep(0)
    controller.async_reconcile.assert_not_called()
    controller.async_rotate.assert_not_called()
    assert r.snapshot()["state"] == "connected"
    r.pause()
    r._automatic_tick(None)
    assert not r.busy


async def test_attention_is_visible_in_manual_response_before_done_callback(scheduled):
    r, store, _controller = scheduled
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, rotation_not_before="2026-10-24T00:01:00Z"))
    r.resume()
    result = await r.async_action("reconcile")
    assert result["state"] == "automatic_attention_required"
    assert r._cancel_timer is None


@pytest.mark.parametrize("state", ["storage_repair", "repair_required", "remote_disconnect_unconfirmed"])
async def test_terminal_automatic_failure_surfaces_repair_without_replay(scheduled, hass, state):
    r, _store, controller = scheduled
    r._state = HostedConnectionResult(state)
    r.resume()
    assert r._cancel_timer is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry") is not None
    controller.async_reconcile.assert_not_called()


async def test_intentional_disconnect_and_optout_clear_entry_repair(scheduled, hass):
    r, _store, controller = scheduled
    r._state = HostedConnectionResult("repair_required")
    r.resume()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry") is not None
    controller.async_disconnect.return_value = HostedConnectionResult("disconnected")
    await r.async_action("disconnect")
    assert registry.async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry") is None
    r._attention_issue()
    r._automatic = False
    r.resume()
    assert registry.async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry") is None


@pytest.mark.parametrize("state", [
    "configuration_repair", "service_repair", "reconciliation_required",
    "fresh_pairing_required",
])
async def test_all_terminal_states_create_entry_scoped_warning(scheduled, hass, state):
    r, _store, controller = scheduled
    r._state = HostedConnectionResult(state)
    r.resume()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry")
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_key == "hosted_lifecycle"
    assert issue.is_persistent is False
    assert r._cancel_timer is None
    controller.async_reconcile.assert_not_awaited()
    controller.async_rotate.assert_not_awaited()


async def test_due_work_is_scheduled_one_second_in_future(scheduled, monkeypatch):
    r, _store, _controller = scheduled
    from unittest.mock import Mock

    schedule = Mock(return_value=lambda: None)
    monkeypatch.setattr(module, "async_track_point_in_utc_time", schedule)
    now = datetime.now(UTC)
    r.resume()
    schedule.assert_called_once_with(r._hass, r._automatic_tick, now + timedelta(seconds=1))


@pytest.mark.parametrize("enabled,accepting", [(False, True), (True, False), (True, True)])
async def test_completion_invalidates_only_active_automatic_entry(scheduled, monkeypatch, enabled, accepting):
    from unittest.mock import Mock

    r, _store, _controller = scheduled
    r._automatic = enabled
    r._accepting = accepting
    send = Mock()
    monkeypatch.setattr(module, "async_dispatcher_send", send)
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    r._tasks.add(task)
    r._finished(task)
    assert task not in r._tasks
    if enabled and accepting:
        send.assert_called_once_with(r._hass, module.hosted_signal("hosted-entry"))
    else:
        send.assert_not_called()


@pytest.mark.parametrize("error", [
    module.HostedCredentialStorageError("private"),
    module.HostedCredentialConflict("private"),
])
async def test_failed_storage_discards_automatic_policy(scheduled, error):
    r, _store, controller = scheduled
    controller.async_reconcile.side_effect = error
    await r._perform("reconcile", {})
    assert r._controller is None
    assert r._automation is None


@pytest.mark.parametrize("enabled,status,cleared", [
    (True, "overlap", True), (True, None, False), (False, "current", False),
])
async def test_repair_clear_requires_optin_and_verified_authority(scheduled, hass, enabled, status, cleared):
    r, _store, controller = scheduled
    r._automatic = enabled
    r._attention_issue()
    controller.async_reconcile.return_value = HostedConnectionResult(
        "connected", status, ACTIVE.credential_expires_at,
    )
    await r._perform("reconcile", {})
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry")
    assert (issue is None) is cleared


async def test_manual_rate_limit_never_triggers_early_automatic_status(scheduled, hass, freezer):
    r, store, controller = scheduled
    r.resume()
    controller.async_reconcile.side_effect = HostedTransportError("rate_limited", status=429, retryable=True, retry_after=300)
    result = await r.async_action("reconcile")
    assert result["state"] == "service_unavailable"
    assert store.snapshot.automation.status.attempts == 0
    await tick(hass, freezer, 300)
    assert controller.async_reconcile.await_count == 1
    await tick(hass, freezer, 1)
    assert controller.async_reconcile.await_count == 2


async def test_automatic_renewal_rate_limit_gates_subsequent_status(scheduled, hass, freezer):
    r, _store, controller = scheduled
    r._state = NEAR
    controller.async_rotate.side_effect = HostedTransportError("rate_limited", status=429, retryable=True, retry_after=300)
    r.resume()
    await tick(hass, freezer, 1)
    assert controller.async_rotate.await_count == 1
    await tick(hass, freezer, 300)
    controller.async_reconcile.assert_not_called()
    await tick(hass, freezer, 1)
    controller.async_reconcile.assert_awaited_once()


@pytest.mark.parametrize("consumed", [False, True])
async def test_expired_or_consumed_pairing_surfaces_repairs_and_never_replays(scheduled, hass, freezer, consumed):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        PairingAttempt,
    )
    r, store, controller = scheduled
    old = store.snapshot
    attempt = PairingAttempt("pairing_1234567890", "2026-10-24T00:00:05Z", 5, "2026-10-24T00:00:00Z", consumed)
    await store.async_checkpoint(old, replace(old, current=None, pairing_verifier="v" * 43, pairing=attempt))
    freezer.tick(5)
    r.resume()
    assert r.snapshot()["state"] == "automatic_attention_required"
    assert r._cancel_timer is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "hosted_lifecycle_hosted-entry") is not None
    assert not controller.mock_calls


async def test_transient_live_pairing_failure_reports_retry_without_losing_attempt(scheduled):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        PairingAttempt,
    )
    r, store, controller = scheduled
    old = store.snapshot
    attempt = PairingAttempt("pairing_1234567890", "2026-10-24T00:10:00Z", 5, "2026-10-24T00:00:00Z")
    await store.async_checkpoint(old, replace(old, current=None, pairing_verifier="v" * 43, pairing=attempt))
    controller.async_poll_pairing.side_effect = HostedTransportError("service_unavailable", retryable=True)
    r.resume()
    result = await r.async_action("poll")
    assert result["state"] == "pairing_retry" and "pairing" not in result
    assert store.snapshot.pairing == attempt
