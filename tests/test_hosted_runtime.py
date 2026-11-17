"""Entry-owned hosted actions retain uncertainty and keep local setup available."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from custom_components.daylight_calendar_import.hosted_credentials import (
    BoundCredential,
    CredentialCheckpoint,
    HostedCredentialConflict,
    HostedCredentialStorageError,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedConnectionResult,
    HostedPairingInstructions,
)
from custom_components.daylight_calendar_import.hosted_runtime import (
    CONF_HOSTED_SERVICE,
    HostedRuntimeError,
    async_setup_hosted_runtime,
    service_from_options,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedServiceBinding,
    HostedTransportError,
)

SERVICE = {"api_origin": "https://api.example.test", "account_origin": "https://account.example.test"}


def entry(options=None):
    return SimpleNamespace(entry_id="hosted-entry", options=options or {})


@pytest.fixture
async def runtime(hass, monkeypatch):
    store = AsyncMock()
    store.async_load.return_value = CredentialCheckpoint()
    store.snapshot = store.async_load.return_value
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore", lambda *_: store)
    r = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert not r.accepting
    r.resume()
    assert r.accepting and not r.busy
    return r, store


async def test_no_configuration_never_constructs_store_or_activates_network(hass, monkeypatch):
    constructor = AsyncMock()
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore", constructor)
    r = await async_setup_hosted_runtime(hass, entry())
    assert r.snapshot() == {
        "entry_id": "hosted-entry", "configured": False, "automatic_lifecycle": False, "reconnect_in_progress": False, "api_origin": None,
        "account_origin": None, "state": "not_configured", "account_url": None,
        "credential_status": None, "credential_expires_at": None,
    }
    constructor.assert_not_called()
    with pytest.raises(HostedRuntimeError, match="hosted_already_loaded"):
        await r.async_load()
    r.resume()
    with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
        await r.async_action("reconcile")
    await r.async_stop()
    await r.async_stop()
    r.resume()
    with pytest.raises(HostedRuntimeError, match="hosted_unloading"):
        await r.async_action("disconnect")


@pytest.mark.parametrize("value", [{}, [], "secret", {**SERVICE, "extra": True}, {**SERVICE, "api_origin": "http://bad.test"}])
async def test_bad_configuration_is_hosted_only_repair(hass, value):
    r = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: value}))
    assert r.snapshot()["state"] == "configuration_repair"
    assert r.snapshot()["api_origin"] is None
    with pytest.raises(ValueError):
        service_from_options({CONF_HOSTED_SERVICE: value})


@pytest.mark.parametrize("checkpoint, expected", [
    (CredentialCheckpoint(), "disconnected"),
    (CredentialCheckpoint(processing_consent=True), "recovery_required"),
    (CredentialCheckpoint(local_disconnect="unconfirmed"), "remote_disconnect_unconfirmed"),
    (CredentialCheckpoint(local_disconnect="confirmed"), "disconnected"),
    (CredentialCheckpoint(service_binding=HostedServiceBinding("https://other.test", SERVICE["account_origin"])), "service_repair"),
])
async def test_loaded_state_is_not_remote_authority(hass, monkeypatch, checkpoint, expected):
    store = AsyncMock()
    store.async_load.return_value = checkpoint
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore", lambda *_: store)
    r = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert r.snapshot()["state"] == expected
    assert r.snapshot()["credential_expires_at"] is None
    assert r.snapshot()["account_url"] is None


async def test_storage_failure_does_not_escape_into_entry_setup(hass, monkeypatch):
    store = AsyncMock()
    store.async_load.side_effect = HostedCredentialStorageError("private path and token")
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore", lambda *_: store)
    r = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert r.snapshot()["state"] == "storage_repair"
    assert "private" not in str(r.snapshot())


async def test_storage_load_cancellation_still_cancels_entry_setup(hass, monkeypatch):
    async def cancelled():
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise HostedCredentialStorageError("private") from None
    store = AsyncMock()
    store.async_load.side_effect = cancelled
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore", lambda *_: store)
    task = asyncio.create_task(async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE})))
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("action", ["connect", "poll", "reconcile", "rotate", "disconnect"])
async def test_explicit_actions_use_one_controller_and_safe_projection(runtime, action):
    r, _store = runtime
    method = {"connect": "async_start_pairing", "poll": "async_poll_pairing", "reconcile": "async_reconcile", "rotate": "async_rotate", "disconnect": "async_disconnect"}[action]
    controller = AsyncMock()
    getattr(controller, method).return_value = HostedConnectionResult("connected", "current", "2026-12-01T00:00:00Z", "https://account.example.test/account")
    r._controller = controller
    kwargs = {"processing_consent": True, "installation_label": "Home"} if action == "connect" else {}
    result = await r.async_action(action, **kwargs)
    assert result == r.snapshot()
    assert result["account_url"] == "https://account.example.test/account"
    getattr(controller, method).assert_awaited_once_with(**kwargs)


async def test_approval_instructions_are_only_in_connect_response(runtime):
    r, _store = runtime
    c = AsyncMock()
    c.async_start_pairing.return_value = HostedPairingInstructions("ABCD1234EFGH", "https://account.example.test/connect", "2026-10-24T00:10:00Z", 5)
    r._controller = c
    result = await r.async_action("connect", processing_consent=True, installation_label="Home")
    assert result["pairing"]["user_code"] == "ABCD1234EFGH"
    assert "pairing" not in r.snapshot() and "ABCD1234EFGH" not in str(r.snapshot())
    assert r.snapshot()["state"] == "pairing_pending"


@pytest.mark.parametrize("action,kwargs", [("unknown", {}), ("reconcile", {"secret": "value"})])
async def test_invalid_actions_do_not_enter_controller(runtime, action, kwargs):
    r, _store = runtime
    with pytest.raises(HostedRuntimeError, match="invalid_hosted_action"):
        await r.async_action(action, **kwargs)


@pytest.mark.parametrize("error,state", [
    (HostedCredentialStorageError("private"), "storage_repair"),
    (HostedCredentialConflict("private"), "recovery_required"),
    (HostedTransportError("service_reconciliation_required"), "service_repair"),
    (HostedTransportError("private", status=401), "repair_required"),
    (HostedTransportError("private", status=403), "repair_required"),
    (HostedTransportError("entitlement_required", status=403), "access_unavailable"),
    (HostedTransportError("entitlement_required", status=503), "service_unavailable"),
    (HostedTransportError("identity_mismatch"), "repair_required"),
    (HostedTransportError("private", status=503), "service_unavailable"),
])
async def test_failure_projection_excludes_raw_errors(runtime, error, state):
    r, _store = runtime
    c = AsyncMock()
    c.async_reconcile.side_effect = error
    r._controller = c
    result = await r.async_action("reconcile")
    assert result["state"] == state and "private" not in str(result)
    if isinstance(error, (HostedCredentialStorageError, HostedCredentialConflict)):
        with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
            await r.async_action("reconcile")


async def test_unexpected_failure_is_sanitized(runtime):
    r, _store = runtime
    c = AsyncMock()
    c.async_reconcile.side_effect = ValueError("secret in provider error")
    r._controller = c
    with pytest.raises(HostedRuntimeError, match="^hosted_action_failed$"):
        await r.async_action("reconcile")
    assert r.snapshot()["state"] == "service_unavailable"


async def test_one_action_no_queue_pause_resume_and_stop(runtime):
    r, _store = runtime
    c = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()
    async def action():
        entered.set()
        await release.wait()
        return HostedConnectionResult("connected")
    c.async_reconcile.side_effect = action
    r._controller = c
    running = asyncio.create_task(r.async_action("reconcile"))
    await entered.wait()
    assert r.busy
    with pytest.raises(HostedRuntimeError, match="hosted_busy"):
        await r.async_action("rotate")
    r.pause()
    with pytest.raises(HostedRuntimeError, match="hosted_unloading"):
        await r.async_action("disconnect")
    r.resume()
    release.set()
    await running
    assert not r._tasks
    await r.async_stop()


async def test_cancellation_ownership_survives_stop_and_reload(runtime, hass, monkeypatch):
    r, _store = runtime
    c = AsyncMock()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    async def action():
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return HostedConnectionResult("repair_required")
    c.async_reconcile.side_effect = action
    r._controller = c
    running = asyncio.create_task(r.async_action("reconcile"))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await cancelled.wait()
    # A stopped runtime cannot reopen admission while a cancelled request lives.
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime._STOP_SECONDS", 0)
    await r.async_stop()
    replacement = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert replacement.snapshot()["state"] == "operation_finishing"
    replacement.resume()
    with pytest.raises(HostedRuntimeError, match="hosted_busy"):
        await replacement.async_action("reconcile")
    release.set()
    await asyncio.gather(*tuple(r._tasks), return_exceptions=True)
    assert not r._tasks
    result = await replacement.async_action("reconcile")
    assert result["state"] == "disconnected"
    assert replacement._controller is not None
    assert _store.async_load.await_count == 2


async def test_stop_cancels_normal_action_and_is_bounded(runtime):
    r, _store = runtime
    c = AsyncMock()
    entered = asyncio.Event()
    async def action():
        entered.set()
        await asyncio.Event().wait()
    c.async_reconcile.side_effect = action
    r._controller = c
    running = asyncio.create_task(r.async_action("reconcile"))
    await entered.wait()
    await r.async_stop()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert r.snapshot()["state"] == "recovery_required"


async def test_explicit_deferred_recovery_still_blocks_private_storage_failure(runtime):
    r, store = runtime
    r._controller = None
    r._state = HostedConnectionResult("operation_finishing")
    store.async_load.side_effect = HostedCredentialStorageError("private")
    result = await r.async_action("reconcile")
    assert result["state"] == "storage_repair"
    assert r._controller is None and "private" not in str(result)


# Mutation guards for entry ownership, public error codes, and task identity.
# The assertions intentionally cover observable action semantics without changing
# the hosted implementation or the project-wide mutation score threshold.
def test_initial_hosted_runtime_state_and_entry_scoped_ownership(hass):
    from custom_components.daylight_calendar_import.const import DOMAIN
    from custom_components.daylight_calendar_import.hosted_runtime import (
        HostedConnectionRuntime,
    )

    r = HostedConnectionRuntime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert r._service is None
    assert r._accepting is False
    assert r._stopped is False
    assert r._loaded is False
    registry = hass.data[f"{DOMAIN}_hosted_tasks"]
    assert registry["hosted-entry"] is r._tasks
    assert None not in registry


def test_malformed_hosted_config_has_exact_safe_error():
    with pytest.raises(ValueError) as raised:
        service_from_options({CONF_HOSTED_SERVICE: {"api_origin": "https://api.example.test"}})
    assert str(raised.value) == "invalid hosted configuration"


async def test_loaded_runtime_wires_entry_store_session_and_trusted_origins(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_runtime as module

    store = AsyncMock()
    store.async_load.return_value = CredentialCheckpoint()
    session = object()
    seen = {}
    controller = SimpleNamespace(reconnect_in_progress=False)

    def store_factory(*args):
        seen["store_args"] = args
        return store

    def session_factory(instance):
        seen["session_hass"] = instance
        return session

    def transport_factory(*args, **kwargs):
        seen["transport_args"] = args
        seen["transport_kwargs"] = kwargs
        return "transport-sentinel"

    def controller_factory(*args):
        seen["controller_args"] = args
        return controller

    monkeypatch.setattr(module, "HostedCredentialStore", store_factory)
    monkeypatch.setattr(module, "async_get_clientsession", session_factory)
    monkeypatch.setattr(module, "HostedLifecycleTransport", transport_factory)
    monkeypatch.setattr(module, "HostedLifecycleController", controller_factory)

    runtime = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert seen["store_args"] == (hass, "hosted-entry")
    assert seen["session_hass"] is hass
    assert seen["transport_args"] == (session,)
    assert seen["transport_kwargs"] == SERVICE
    assert seen["controller_args"] == (store, "transport-sentinel")
    assert runtime._controller is controller
    assert runtime.snapshot()["api_origin"] == SERVICE["api_origin"]
    assert runtime.snapshot()["account_origin"] == SERVICE["account_origin"]
    assert runtime.snapshot()["configured"] is True


async def test_admission_errors_are_exact_fixed_codes(runtime):
    r, _ = runtime

    async def rejected(action, expected, **kwargs):
        with pytest.raises(HostedRuntimeError) as error:
            await r.async_action(action, **kwargs)
        assert str(error.value) == expected

    await rejected("unknown", "invalid_hosted_action")
    await rejected("reconcile", "invalid_hosted_action", extraneous=True)
    with pytest.raises(HostedRuntimeError) as loaded:
        await r.async_load()
    assert str(loaded.value) == "hosted_already_loaded"
    r.pause()
    await rejected("reconcile", "hosted_unloading")
    r.resume()
    controller = r._controller
    r._controller = None
    await rejected("reconcile", "hosted_unavailable")
    r._controller = controller
    marker = object()
    r._tasks.add(marker)
    try:
        await rejected("reconcile", "hosted_busy")
    finally:
        r._tasks.remove(marker)


async def test_explicit_action_has_named_entry_owned_task(runtime, hass):
    from custom_components.daylight_calendar_import.const import DOMAIN

    r, _ = runtime
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked():
        started.set()
        await release.wait()
        return HostedConnectionResult("connected")

    controller = AsyncMock()
    controller.async_reconcile.side_effect = blocked
    r._controller = controller
    waiter = asyncio.create_task(r.async_action("reconcile"))
    try:
        await started.wait()
        assert r._task is not None
        assert r._task.get_name() == "Daylight hosted lifecycle"
        assert r._task in hass.data[f"{DOMAIN}_hosted_tasks"]["hosted-entry"]
        assert r.busy
    finally:
        release.set()
        await waiter
    assert not r.busy


async def test_cancelled_deferred_load_can_be_explicitly_recovered_after_it_settles(runtime):
    r, store = runtime
    r._controller = None
    r._state = HostedConnectionResult("operation_finishing")
    entered = asyncio.Event()
    async def blocked_load():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise HostedCredentialStorageError("private cancelled read") from None
    store.async_load.side_effect = blocked_load
    waiter = asyncio.create_task(r.async_action("reconcile"))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.gather(*tuple(r._tasks), return_exceptions=True)
    await asyncio.sleep(0)  # Let the owned task's completion callback retire it.
    assert not r.busy and r._controller is None
    assert r.snapshot()["state"] == "operation_finishing"
    store.async_load.side_effect = None
    result = await r.async_action("reconcile")
    assert result["state"] == "disconnected" and r._controller is not None


@pytest.mark.parametrize("wrapped", [False, True])
async def test_hosted_load_budget_preserves_local_entry_availability(hass, monkeypatch, wrapped):
    store = AsyncMock()
    async def blocked():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if wrapped:
                raise HostedCredentialStorageError("private timed out read") from None
            raise
    store.async_load.side_effect = blocked
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore", lambda *_: store)
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_runtime._LOAD_SECONDS", 0.001)
    r = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert r.snapshot()["state"] == "storage_repair"
    assert r._controller is None and not r.busy
    assert "private" not in str(r.snapshot())


async def test_cancelled_controller_replaces_previous_authority_with_recovery(runtime):
    r, _store = runtime
    controller = AsyncMock()
    controller.async_reconcile.side_effect = asyncio.CancelledError()
    r._controller = controller
    r._state = HostedConnectionResult("connected", "current", "2026-12-01T00:00:00Z")
    with pytest.raises(asyncio.CancelledError):
        await r._perform("reconcile", {})
    assert r.snapshot()["state"] == "recovery_required"
    assert r.snapshot()["credential_status"] is None
    assert r.snapshot()["credential_expires_at"] is None


async def test_snapshot_reports_safe_saved_reconnect_progress(runtime):
    r, _store = runtime
    r._controller = SimpleNamespace(reconnect_in_progress=True)
    assert r.snapshot()["reconnect_in_progress"] is True
    r._controller.reconnect_in_progress = False
    assert r.snapshot()["reconnect_in_progress"] is False


async def test_startup_checks_saved_access_and_recovers_without_repair(hass, monkeypatch):
    binding = HostedServiceBinding(**SERVICE)
    credential = BoundCredential(
        "account_1234567890", "installation_1234567890", "x" * 60,
        "2099-12-01T00:00:00+00:00",
    )
    checkpoint = CredentialCheckpoint(
        processing_consent=True, current=credential, service_binding=binding,
    )
    store = AsyncMock()
    store.snapshot = checkpoint
    store.async_load.return_value = checkpoint
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore",
        lambda *_: store,
    )
    transport = AsyncMock(service_binding=binding)
    transport.installation_status.side_effect = HostedTransportError(
        "entitlement_required", status=403,
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.hosted_runtime.HostedLifecycleTransport",
        lambda *_, **__: transport,
    )

    runtime = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    assert runtime.snapshot()["state"] == "checking_access"
    runtime.resume()
    runtime.resume()
    await hass.async_block_till_done()
    assert runtime.snapshot()["state"] == "access_unavailable"
    assert runtime.snapshot()["credential_status"] is None
    assert runtime._verified_parse_credential is None
    assert transport.mock_calls == [call.installation_status(credential.token)]
    store.async_checkpoint.assert_not_called()

    transport.installation_status.side_effect = None
    transport.installation_status.return_value = {
        "account_id": credential.account_id,
        "installation_id": credential.installation_id,
        "credential_status": "current",
        "credential_expires_at": credential.expires_at,
    }
    assert (await runtime.async_action("reconcile"))["state"] == "connected"
    assert runtime._verified_parse_credential == credential
    store.async_checkpoint.assert_not_called()
    assert store.snapshot is checkpoint
    await runtime.async_stop()


@pytest.mark.parametrize("field", ["service_binding", "candidate", "pairing_verifier"])
async def test_startup_does_not_recover_incomplete_connections(hass, monkeypatch, field):
    from tests.test_hosted_lifecycle import CANDIDATE, CURRENT

    checkpoint = CredentialCheckpoint(
        processing_consent=True, current=CURRENT, service_binding=HostedServiceBinding(**SERVICE),
    )
    value = {"service_binding": None, "candidate": CANDIDATE, "pairing_verifier": "v" * 43}[field]
    store = AsyncMock()
    store.snapshot = store.async_load.return_value = replace(checkpoint, **{field: value})
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore",
        lambda *_: store,
    )
    runtime = await async_setup_hosted_runtime(hass, entry({CONF_HOSTED_SERVICE: SERVICE}))
    runtime.resume()
    assert runtime.snapshot()["state"] == "recovery_required"
    assert not runtime.busy
    await runtime.async_stop()


async def test_delivery_poll_is_opt_in_single_flight_and_backed_off(hass, monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from custom_components.daylight_calendar_import import hosted_runtime as module
    from custom_components.daylight_calendar_import.const import (
        CONF_PARSER_PROVIDER, PARSER_PROVIDER_DAYLIGHT_CLOUD,
    )

    cancel = Mock()
    timer = Mock(return_value=cancel)
    issue = Mock()
    monkeypatch.setattr(module, "async_track_point_in_utc_time", timer)
    monkeypatch.setattr(module.ir, "async_create_issue", issue)
    config = entry({
        module.CONF_HOSTED_DELIVERY_POLL: True,
        CONF_PARSER_PROVIDER: PARSER_PROVIDER_DAYLIGHT_CLOUD,
    })
    config.data = {}
    runtime = await async_setup_hosted_runtime(hass, config)
    assert runtime.snapshot()["delivery_polling"] is True
    runtime.resume()
    assert not timer.called
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=object())
    runtime._state = HostedConnectionResult("connected", credential_status="current")
    runtime._delivery_parser_selected = False
    runtime._schedule_delivery_poll()
    assert not timer.called
    runtime._delivery_parser_selected = True
    runtime._schedule_delivery_poll()
    assert timer.call_count == 1
    passed = AsyncMock(side_effect=HostedTransportError("rate_limited", status=429, retry_after=300))
    monkeypatch.setattr(runtime, "async_delivery_pass", passed)
    timer.call_args.args[1](datetime.now(UTC))
    runtime._delivery_poll_tick(datetime.now(UTC))
    await hass.async_block_till_done()
    passed.assert_awaited_once_with(limit=1)
    assert (timer.call_args.args[2] - datetime.now(UTC)).total_seconds() > 295
    assert not issue.called

    runtime._delivery_failures = 4
    passed.side_effect = RuntimeError("private provider detail")
    await runtime._async_delivery_poll()
    assert issue.call_args.kwargs["translation_key"] == "hosted_delivery_retry"
    assert (runtime._delivery_due_at - datetime.now(UTC)).total_seconds() > 895

    passed.side_effect = None
    await runtime._async_delivery_poll()
    assert runtime._delivery_failures == 0
    assert (runtime._delivery_due_at - datetime.now(UTC)).total_seconds() > 55
    await runtime.async_stop()
    cancel.assert_called()
    count = passed.await_count
    runtime._delivery_poll_tick(datetime.now(UTC))
    await hass.async_block_till_done()
    assert passed.await_count == count


async def test_delivery_poll_defers_foreground_and_requires_explicit_recovery(hass, monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from custom_components.daylight_calendar_import import hosted_runtime as module

    timer = Mock(return_value=Mock())
    create_issue = Mock()
    delete_issue = Mock()
    monkeypatch.setattr(module, "async_track_point_in_utc_time", timer)
    monkeypatch.setattr(module.ir, "async_create_issue", create_issue)
    monkeypatch.setattr(module.ir, "async_delete_issue", delete_issue)
    runtime = await async_setup_hosted_runtime(hass, entry())
    runtime.resume()
    with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
        await runtime.async_retry_delivery_parse()
    runtime._delivery_poll_enabled = runtime._delivery_parser_selected = True
    store = SimpleNamespace(hosted_parse_uncertain=True, active_submissions={object()})
    async def clear(value):
        assert value is None
        store.hosted_parse_uncertain = False
    store.async_change_hosted_parse_attempt = AsyncMock(side_effect=clear)
    runtime.configure_delivery(store=store, route_source=object(), parse_source=object())
    runtime._state = HostedConnectionResult("connected", credential_status="current")
    runtime._schedule_delivery_poll()
    assert not timer.called
    assert create_issue.call_args.kwargs == {
        "is_fixable": False, "is_persistent": True, "severity": module.ir.IssueSeverity.WARNING,
        "translation_key": "hosted_delivery_uncertain",
    }
    assert runtime.snapshot()["delivery_parse_uncertain"]
    runtime._delivery_poll_running = True
    with pytest.raises(HostedRuntimeError, match="hosted_busy"):
        await runtime.async_retry_delivery_parse()
    runtime._delivery_poll_running = False
    assert "delivery_parse_uncertain" not in await runtime.async_retry_delivery_parse()
    store.async_change_hosted_parse_attempt.assert_awaited_once_with(None)
    delete_issue.assert_called_with(hass, "daylight_calendar_import",
                                    "hosted_delivery_uncertain_hosted-entry")
    assert timer.call_count == 1
    run = AsyncMock()
    monkeypatch.setattr(runtime, "async_delivery_pass", run)
    runtime._delivery_poll_tick(datetime.now(UTC))
    run.assert_not_awaited()
    assert (runtime._delivery_due_at - datetime.now(UTC)).total_seconds() > 55
    await runtime._async_delivery_poll()
    run.assert_not_awaited()
    await runtime.async_stop()


async def test_delivery_runtime_starts_with_strictly_disabled_email_defaults(runtime):
    r, _ = runtime
    assert r._delivery_poll_enabled is False
    assert r._delivery_parser_selected is False
    assert r._delivery_failures == 0
    assert r._delivery_due_at is None
    assert r._delivery_poll_running is False
    assert r._delivery_owner is None
    assert r._verified_parse_credential is None
    await r.async_stop()


async def test_delivery_scheduler_independent_admission_guards_and_deadlines(runtime, hass, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from custom_components.daylight_calendar_import import hosted_runtime as module

    timer = Mock(return_value=Mock())
    monkeypatch.setattr(module, "async_track_point_in_utc_time", timer)
    r, _ = runtime
    r._delivery_poll_enabled = r._delivery_parser_selected = True
    store = SimpleNamespace(
        hosted_parse_uncertain=False, active_submissions=set(), active_service_handlers=set(),
    )
    r.configure_delivery(store=store, route_source=object(), parse_source=object())
    handlers = r._delivery_handlers
    r._state = HostedConnectionResult("connected", credential_status="current")

    r._delivery_handlers = None
    r._schedule_delivery_poll()
    timer.assert_not_called()
    r._delivery_handlers = handlers

    r._delivery_poll_running = True
    r._schedule_delivery_poll()
    timer.assert_not_called()
    r._delivery_poll_running = False

    owner = asyncio.current_task()
    r._tasks.add(owner)
    try:
        r._schedule_delivery_poll()
        timer.assert_not_called()
    finally:
        r._tasks.remove(owner)

    r._accepting = False
    r._schedule_delivery_poll()
    timer.assert_not_called()
    r._accepting = True

    r._delivery_poll_enabled = False
    r._schedule_delivery_poll()
    timer.assert_not_called()
    r._delivery_poll_enabled = True

    r._delivery_parser_selected = False
    r._schedule_delivery_poll()
    timer.assert_not_called()
    r._delivery_parser_selected = True

    r._state = HostedConnectionResult("connected", credential_status="expired")
    r._schedule_delivery_poll()
    timer.assert_not_called()

    r._state = HostedConnectionResult("connected", credential_status="overlap")
    start = datetime.now(UTC)
    r._schedule_delivery_poll()
    assert timer.call_count == 1
    assert timer.call_args.args[0] is hass
    assert timer.call_args.args[1] == r._delivery_poll_tick
    assert 0.8 <= (timer.call_args.args[2] - start).total_seconds() <= 1.5

    timer.reset_mock()
    r._delivery_due_at = datetime.now(UTC) - timedelta(minutes=5)
    start = datetime.now(UTC)
    r._schedule_delivery_poll()
    assert timer.call_count == 1
    assert 0.8 <= (timer.call_args.args[2] - start).total_seconds() <= 1.5

    future = datetime.now(UTC) + timedelta(seconds=150)
    r._delivery_due_at = future
    r._schedule_delivery_poll()
    assert timer.call_args.args[2] == future
    await r.async_stop()


async def test_delivery_repair_issue_records_correct_owner_and_persistence(runtime, hass, monkeypatch):
    from unittest.mock import Mock

    from custom_components.daylight_calendar_import import hosted_runtime as module

    create_issue = Mock()
    monkeypatch.setattr(module.ir, "async_create_issue", create_issue)
    r, _ = runtime

    r._delivery_attention_issue("hosted_delivery_expired")
    create_issue.assert_called_once_with(
        hass, module.DOMAIN, "hosted_delivery_expired_hosted-entry",
        is_fixable=False, severity=module.ir.IssueSeverity.WARNING,
        translation_key="hosted_delivery_expired", is_persistent=True,
    )
    create_issue.reset_mock()
    r._delivery_attention_issue("hosted_delivery_retry")
    create_issue.assert_called_once_with(
        hass, module.DOMAIN, "hosted_delivery_retry_hosted-entry",
        is_fixable=False, severity=module.ir.IssueSeverity.WARNING,
        translation_key="hosted_delivery_retry", is_persistent=False,
    )
    await r.async_stop()


async def test_delivery_backoff_progression_logs_and_clears_exact_repair(runtime, hass, monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from custom_components.daylight_calendar_import import hosted_runtime as module

    r, _ = runtime
    r._delivery_poll_enabled = r._delivery_parser_selected = True
    r.configure_delivery(
        store=SimpleNamespace(
            hosted_parse_uncertain=False, active_submissions=set(), active_service_handlers=set(),
        ),
        route_source=object(), parse_source=object(),
    )
    r._state = HostedConnectionResult("connected", credential_status="current")
    monkeypatch.setattr(module, "async_track_point_in_utc_time", Mock(return_value=Mock()))
    warning = Mock()
    delete = Mock()
    create = Mock()
    monkeypatch.setattr(module._LOGGER, "warning", warning)
    monkeypatch.setattr(module.ir, "async_delete_issue", delete)
    monkeypatch.setattr(module.ir, "async_create_issue", create)
    poll = AsyncMock(side_effect=HostedTransportError("rate_limited", status=429, retry_after=0))
    monkeypatch.setattr(r, "async_delivery_pass", poll)

    await r._async_delivery_poll()
    assert r._delivery_failures == 1
    assert 118 <= (r._delivery_due_at - datetime.now(UTC)).total_seconds() <= 122
    assert r._delivery_poll_running is False
    await r._async_delivery_poll()
    assert r._delivery_failures == 2
    assert 238 <= (r._delivery_due_at - datetime.now(UTC)).total_seconds() <= 242

    r._delivery_failures = 4
    poll.side_effect = RuntimeError("private provider message")
    await r._async_delivery_poll()
    assert r._delivery_failures == 5
    assert 895 <= (r._delivery_due_at - datetime.now(UTC)).total_seconds() <= 902
    warning.assert_called_once_with("Daylight hosted delivery polling is backing off")
    create.assert_called_once_with(
        hass, module.DOMAIN, "hosted_delivery_retry_hosted-entry",
        is_fixable=False, severity=module.ir.IssueSeverity.WARNING,
        translation_key="hosted_delivery_retry", is_persistent=False,
    )
    await r._async_delivery_poll()
    await r._async_delivery_poll()
    assert r._delivery_failures == 6
    assert 895 <= (r._delivery_due_at - datetime.now(UTC)).total_seconds() <= 902
    warning.assert_called_once()

    poll.side_effect = None
    await r._async_delivery_poll()
    assert r._delivery_failures == 0
    assert r._delivery_poll_running is False
    assert 58 <= (r._delivery_due_at - datetime.now(UTC)).total_seconds() <= 62
    delete.assert_called_once_with(hass, module.DOMAIN, "hosted_delivery_retry_hosted-entry")
    assert poll.await_args_list and all(x.kwargs == {"limit": 1} for x in poll.await_args_list)
    await r.async_stop()


async def test_explicit_delivery_retry_fences_permission_and_owns_transaction(runtime, hass, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from custom_components.daylight_calendar_import import hosted_runtime as module

    r, _ = runtime
    timer = Mock(return_value=Mock())
    delete = Mock()
    monkeypatch.setattr(module, "async_track_point_in_utc_time", timer)
    monkeypatch.setattr(module.ir, "async_delete_issue", delete)
    store = SimpleNamespace(hosted_parse_uncertain=True)
    async def clear_attempt(value):
        assert value is None
        assert r._delivery_poll_running is True
        store.hosted_parse_uncertain = False
    store.async_change_hosted_parse_attempt = AsyncMock(side_effect=clear_attempt)
    r.configure_delivery(store=store, route_source=object(), parse_source=object())
    r._state = HostedConnectionResult("connected", credential_status="current")

    with pytest.raises(HostedRuntimeError, match="^hosted_unavailable$"):
        await r.async_retry_delivery_parse()
    r._delivery_poll_enabled = r._delivery_parser_selected = True
    r.pause()
    with pytest.raises(HostedRuntimeError, match="^hosted_unavailable$"):
        await r.async_retry_delivery_parse()
    r.resume()
    r._delivery_poll_running = True
    with pytest.raises(HostedRuntimeError, match="^hosted_busy$"):
        await r.async_retry_delivery_parse()
    r._delivery_poll_running = False

    r._delivery_due_at = datetime.now(UTC) + timedelta(hours=2)
    result = await r.async_retry_delivery_parse()
    assert result["delivery_polling"] is True
    assert "delivery_parse_uncertain" not in result
    assert r._delivery_due_at is None
    assert r._delivery_poll_running is False
    store.async_change_hosted_parse_attempt.assert_awaited_once_with(None)
    delete.assert_any_call(hass, module.DOMAIN, "hosted_delivery_uncertain_hosted-entry")
    assert timer.called
    await r.async_stop()


async def test_foreground_delivery_conflict_checks_service_handlers_independently(runtime):
    r, _ = runtime
    store = SimpleNamespace(active_submissions=set(), active_service_handlers={object()})
    r.configure_delivery(store=store, route_source=object(), parse_source=object())
    assert r._foreground_delivery_conflict() is True
    store.active_service_handlers.clear()
    assert r._foreground_delivery_conflict() is False
    store.active_submissions.add(object())
    assert r._foreground_delivery_conflict() is True
    await r.async_stop()
