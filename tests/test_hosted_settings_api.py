"""Native hosted commands use HA admin authorization and the shared entry owner."""
# ruff: noqa: F811 -- shared real-entry fixture

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.daylight_calendar_import import async_unload_entry
from custom_components.daylight_calendar_import import hosted_settings_api as api
from custom_components.daylight_calendar_import.const import DOMAIN
from custom_components.daylight_calendar_import.hosted_credentials import (
    _ImmediatePrivateStore,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedConnectionResult,
)
from custom_components.daylight_calendar_import.hosted_runtime import (
    CONF_HOSTED_SERVICE,
    HostedRuntimeError,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedTransportError,
)
from custom_components.daylight_calendar_import.settings import SettingsValidationError
from tests.test_hosted_lifecycle import _REAL_LOAD, _REAL_WRITE
from tests.test_settings import FakeConnection, entry, hass_for
from tests.test_settings_acceptance import call, settings_runtime, success  # noqa: F401

SERVICE = {"api_origin": "https://api.example.test", "account_origin": "https://account.example.test"}
TOKEN = "private-token-" + "x" * 43
INSTALLATION = "installation_1234567890"
ACCOUNT = "account_1234567890"


def fake_runtime(hass, cfg):
    runtime = SimpleNamespace(accepting=True, busy=False, pause=lambda: None, resume=lambda: None,
        snapshot=lambda: {"api_origin": SERVICE["api_origin"], "account_origin": SERVICE["account_origin"]},
        async_action=AsyncMock(return_value={"state": "connected"}))
    hass.data[DOMAIN][cfg.entry_id].hosted_runtime = runtime
    return runtime


async def invoke(handler, hass, msg):
    connection = FakeConnection()
    await inspect.unwrap(handler)(hass, connection, {"id": 1, **msg})
    return connection


async def test_real_socket_connect_candidate_recovery_disconnect_and_restart(hass, hass_ws_client, settings_runtime, monkeypatch, freezer, caplog):
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    cfg = settings_runtime.entry
    client = await hass_ws_client(hass)
    freezer.move_to("2026-10-24T00:00:00Z")
    initial = await success(client, api.WS_HOSTED_GET)
    assert initial["configured"] is False and initial["state"] == "not_configured"
    configured = await success(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE)
    assert configured["state"] == "disconnected"
    started = AsyncMock(return_value={"pairing_id": "pairing_1234567890", "user_code": "ABCD1234EFGH",
        "verification_uri": "https://account.example.test/connect", "expires_at": "2026-10-24T00:10:00Z", "poll_interval_seconds": 5})
    polled = AsyncMock(return_value={"pairing_id": "pairing_1234567890", "status": "approved", "expires_at": "2026-10-24T00:10:00Z"})
    exchanged = AsyncMock(return_value={"installation_id": INSTALLATION, "installation_token": TOKEN,
        "expires_at": "2026-12-01T00:00:00Z", "confirmation_expires_at": "2026-10-24T00:10:00Z"})
    status = {"account_id": ACCOUNT, "installation_id": INSTALLATION, "credential_status": "current",
        "credential_expires_at": "2026-12-01T00:00:00Z", "account_url": "https://account.example.test/account"}
    checked = AsyncMock(side_effect=[HostedTransportError("unauthenticated", status=401), status])
    confirmed = AsyncMock(return_value={"installation_id": INSTALLATION, "expires_at": "2026-12-01T00:00:00Z"})
    disconnected = AsyncMock(return_value={"installation_id": INSTALLATION, "status": "disconnected"})
    for name, mock in {"start_pairing": started, "pairing_status": polled, "exchange": exchanged,
                       "installation_status": checked, "confirm": confirmed, "disconnect": disconnected}.items():
        monkeypatch.setattr(f"custom_components.daylight_calendar_import.hosted_transport.HostedLifecycleTransport.{name}", mock)
    denied = await call(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="connect", installation_label="Home")
    assert denied["error"]["code"] == "consent_required" and not started.called
    result = await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="connect", processing_consent=True, installation_label="Home")
    assert result["pairing"]["user_code"] == "ABCD1234EFGH"
    assert "pairing" not in await success(client, api.WS_HOSTED_GET)
    assert (await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="reconcile"))["state"] == "pairing_pending"
    assert await hass.config_entries.async_reload(cfg.entry_id)
    assert (await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="reconcile"))["state"] == "pairing_pending"
    assert (await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="poll"))["state"] == "pairing_wait"
    assert not polled.called
    freezer.tick(5)
    assert (await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="poll"))["state"] == "candidate_saved"
    recovered = await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="reconcile")
    assert recovered["state"] == "connected" and recovered["account_url"] == status["account_url"]
    confirmed.assert_awaited_once_with(TOKEN)
    checked.side_effect = None
    checked.return_value = status
    refused = await call(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="disconnect")
    assert refused["error"]["code"] == "confirmation_required" and not disconnected.called
    result = await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="disconnect", confirm_disconnect=True)
    assert result["state"] == "disconnected" and result["account_url"] is None
    old_calls = checked.await_count
    assert await hass.config_entries.async_reload(cfg.entry_id)
    assert (await success(client, api.WS_HOSTED_GET))["state"] == "disconnected"
    assert checked.await_count == old_calls and started.await_count == 1
    assert TOKEN not in str(result) and TOKEN not in caplog.text


@pytest.mark.parametrize("command,fields", [(api.WS_HOSTED_GET, {}), (api.WS_HOSTED_SUBSCRIBE, {}), (api.WS_HOSTED_CONFIGURE, SERVICE),
                                          (api.WS_HOSTED_ACTION, {"action": "connect", "processing_consent": True, "installation_label": "Home"})])
async def test_nonadmin_cannot_read_configure_or_act(hass, hass_ws_client, settings_runtime, command, fields):
    group = await hass.auth.async_get_group(GROUP_ID_USER)
    user = MockUser(groups=[group]).add_to_hass(hass)
    token = await hass.auth.async_create_refresh_token(user, client_id="https://example.com")
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(token))
    response = await call(client, command, entry_id=settings_runtime.entry.entry_id, **fields)
    assert response["success"] is False and response["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("handler,msg", [(api.websocket_hosted_get, {}),
    (api.websocket_hosted_subscribe, {}), (api.websocket_hosted_configure, SERVICE), (api.websocket_hosted_action, {"action": "reconcile"})])
async def test_unknown_entry_is_uniform_failure(handler, msg):
    cfg = entry()
    hass = hass_for(cfg)
    result = await invoke(handler, hass, {"entry_id": "missing", **msg})
    assert result.errors[0][1] in ("entry_not_found", "invalid_hosted_configuration")


@pytest.mark.parametrize("kind", ["missing", "loading", "paused"])
async def test_unready_runtime_never_accepts_get_or_action(kind):
    cfg = entry()
    hass = hass_for(cfg)
    if kind != "missing":
        r = fake_runtime(hass, cfg)
        if kind == "loading":
            hass.data[DOMAIN][cfg.entry_id].accepting_services = False
        else:
            r.accepting = False
    for handler, msg in [(api.websocket_hosted_get, {}), (api.websocket_hosted_subscribe, {}), (api.websocket_hosted_action, {"action": "reconcile"})]:
        result = await invoke(handler, hass, {"entry_id": cfg.entry_id, **msg})
        assert result.errors[0][1] == "hosted_unloading"


@pytest.mark.parametrize("action,extra", [("poll", {"processing_consent": True}), ("rotate", {"installation_label": "Home"}),
                                        ("reconcile", {"confirm_disconnect": True})])
async def test_action_specific_fields_do_not_cross_operations(action, extra):
    cfg = entry()
    hass = hass_for(cfg)
    r = fake_runtime(hass, cfg)
    result = await invoke(api.websocket_hosted_action, hass, {"entry_id": cfg.entry_id, "action": action, **extra})
    assert result.errors[0][1] == "invalid_hosted_action" and not r.async_action.called


async def test_runtime_error_is_fixed_and_no_configuration_mutation_is_retried():
    cfg = entry()
    hass = hass_for(cfg)
    r = fake_runtime(hass, cfg)
    r.async_action.side_effect = HostedRuntimeError(TOKEN)
    result = await invoke(api.websocket_hosted_action, hass, {"entry_id": cfg.entry_id, "action": "reconcile"})
    assert result.errors[0][1] == "hosted_action_failed" and TOKEN not in str(result.errors)
    r.busy = True
    result = await invoke(api.websocket_hosted_configure, hass, {"entry_id": cfg.entry_id, **SERVICE})
    assert result.errors[0][1] == "hosted_busy" and not hass.config_entries.updates


async def test_configure_validates_and_same_service_is_noop():
    cfg = entry(options={CONF_HOSTED_SERVICE: SERVICE})
    hass = hass_for(cfg)
    fake_runtime(hass, cfg)
    result = await invoke(api.websocket_hosted_configure, hass, {"entry_id": cfg.entry_id, **SERVICE})
    assert result.results and not hass.config_entries.reloads
    result = await invoke(api.websocket_hosted_configure, hass, {"entry_id": cfg.entry_id, **SERVICE, "api_origin": "http://bad.test"})
    assert result.errors[0][1] == "invalid_hosted_configuration" and not hass.config_entries.updates


@pytest.mark.parametrize("failure", ["unchanged", "saved", "stale"])
async def test_failed_configuration_keeps_old_owner_closed_after_saved_origin_change(monkeypatch, failure):
    cfg = entry()
    hass = hass_for(cfg)
    r = fake_runtime(hass, cfg)
    def pause(): r.accepting = False
    def resume(): r.accepting = True
    r.pause, r.resume = pause, resume
    async def save(_hass, _entry, patch):
        if failure != "unchanged":
            cfg.options = patch
        if failure != "stale":
            raise SettingsValidationError("reload_failed", TOKEN)
        replacement = fake_runtime(hass, cfg)
        replacement.snapshot = lambda: {"api_origin": "https://old.test", "account_origin": SERVICE["account_origin"]}
    monkeypatch.setattr(api, "async_save_option_patch", save)
    result = await invoke(api.websocket_hosted_configure, hass, {"entry_id": cfg.entry_id, **SERVICE})
    assert result.errors and TOKEN not in str(result.errors)
    assert r.accepting is (failure == "unchanged")


@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_real_failed_reload_never_reopens_old_hosted_owner(hass, hass_ws_client, settings_runtime, monkeypatch, failure):
    cfg = settings_runtime.entry
    client = await hass_ws_client(hass)
    old = hass.data[DOMAIN][cfg.entry_id]
    if failure == "false":
        unload = AsyncMock(return_value=False)
    else:
        unload = AsyncMock(side_effect=RuntimeError("synthetic failed unload"))
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    response = await call(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE)
    assert response["success"] is False
    assert dict(cfg.options[CONF_HOSTED_SERVICE]) == SERVICE
    assert hass.data[DOMAIN][cfg.entry_id] is old
    assert old.hosted_settings_reload_guard and not old.hosted_runtime.accepting
    with pytest.raises(HostedRuntimeError, match="hosted_unloading"):
        await old.hosted_runtime.async_action("reconcile")
    unload.side_effect = None
    unload.return_value = True
    # HA marks failed unload non-recoverable until restart. Clean up this
    # fixture directly, without pretending a retry establishes live recovery.
    assert await async_unload_entry(hass, cfg)


async def test_configuration_failure_cannot_resume_discarded_owner(monkeypatch):
    cfg = entry()
    hass = hass_for(cfg)
    r = fake_runtime(hass, cfg)
    r.pause = lambda: setattr(r, "accepting", False)
    r.resume = lambda: setattr(r, "accepting", True)
    async def save(*_args):
        hass.data[DOMAIN].clear()
        raise SettingsValidationError("invalid_settings", TOKEN)
    monkeypatch.setattr(api, "async_save_option_patch", save)
    response = await invoke(api.websocket_hosted_configure, hass, {"entry_id": cfg.entry_id, **SERVICE})
    assert response.errors[0][1] == "invalid_hosted_configuration" and not r.accepting


async def test_real_owned_action_cancelled_by_reload_receives_fixed_ws_error(hass, hass_ws_client, settings_runtime):
    cfg = settings_runtime.entry
    client = await hass_ws_client(hass)
    await success(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE)
    runtime = hass.data[DOMAIN][cfg.entry_id].hosted_runtime
    entered = asyncio.Event()
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
    original = runtime._controller.async_reconcile
    runtime._controller.async_reconcile = AsyncMock(side_effect=blocked)
    await client.send_json_auto_id({"type": api.WS_HOSTED_ACTION, "entry_id": cfg.entry_id, "action": "reconcile"})
    await entered.wait()
    assert await hass.config_entries.async_reload(cfg.entry_id)
    result = await asyncio.wait_for(client.receive_json(), timeout=2)
    assert result["success"] is False and result["error"]["code"] == "hosted_unloading"
    runtime._controller.async_reconcile.assert_awaited_once()
    runtime._controller.async_reconcile = original


async def test_genuine_native_handler_cancellation_propagates():
    cfg = entry()
    hass = hass_for(cfg)
    r = fake_runtime(hass, cfg)
    entered = asyncio.Event()
    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()
    r.async_action.side_effect = blocked
    task = asyncio.create_task(invoke(api.websocket_hosted_action, hass, {"entry_id": cfg.entry_id, "action": "reconcile"}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("extra", [{"installation_token": "client-supplied-placeholder"}, {"account_id": ACCOUNT}, {"action": "reconnect"}])
async def test_live_schema_rejects_private_fields_and_unsupported_reconnect(hass, hass_ws_client, settings_runtime, extra):
    client = await hass_ws_client(hass)
    response = await call(client, api.WS_HOSTED_ACTION, entry_id=settings_runtime.entry.entry_id, action="reconcile", **{k: v for k, v in extra.items() if k != "action"}) if "action" not in extra else await call(client, api.WS_HOSTED_ACTION, entry_id=settings_runtime.entry.entry_id, **extra)
    assert response["success"] is False and response["error"]["code"] == "invalid_format"
    assert TOKEN not in str(response)


async def test_unchanged_reload_cannot_reopen_closed_store(monkeypatch):
    cfg = entry()
    hass = hass_for(cfg)
    r = fake_runtime(hass, cfg)
    r.pause = lambda: setattr(r, "accepting", False)
    r.resume = lambda: setattr(r, "accepting", True)
    async def save(*_args):
        hass.data[DOMAIN][cfg.entry_id].accepting_services = False
    monkeypatch.setattr(api, "async_save_option_patch", save)
    response = await invoke(api.websocket_hosted_configure, hass, {"entry_id": cfg.entry_id, **SERVICE})
    assert response.errors[0][1] == "hosted_unloading" and not r.accepting


async def test_native_explicit_recovery_initializes_settled_replacement_without_http(hass, hass_ws_client, settings_runtime, monkeypatch):
    cfg = settings_runtime.entry
    client = await hass_ws_client(hass)
    await success(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE)
    runtime = hass.data[DOMAIN][cfg.entry_id].hosted_runtime
    runtime._controller = None
    runtime._state = HostedConnectionResult("operation_finishing")
    request = AsyncMock(side_effect=AssertionError("no automatic request"))
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_transport.HostedLifecycleTransport._request", request)
    assert (await success(client, api.WS_HOSTED_GET))["state"] == "operation_finishing"
    result = await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="reconcile")
    assert result["state"] == "disconnected" and runtime._controller is not None
    request.assert_not_called()


async def test_real_admin_subscription_only_invalidates_safe_entry_and_unsubscribes(hass, hass_ws_client, settings_runtime):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from custom_components.daylight_calendar_import.hosted_runtime import hosted_signal

    cfg = settings_runtime.entry
    client = await hass_ws_client(hass)
    subscribed = await call(client, api.WS_HOSTED_SUBSCRIBE, entry_id=cfg.entry_id)
    assert subscribed["success"] is True
    initial = await client.receive_json()
    assert initial == {"id": subscribed["id"], "type": "event", "event": {"entry_id": cfg.entry_id}}
    async_dispatcher_send(hass, hosted_signal(cfg.entry_id))
    assert await client.receive_json() == initial
    result = await call(client, "unsubscribe_events", subscription=subscribed["id"])
    assert result["success"] is True
    async_dispatcher_send(hass, hosted_signal(cfg.entry_id))
    # With no queued invalidation, the next message belongs to this get.
    assert (await success(client, api.WS_HOSTED_GET))["entry_id"] == cfg.entry_id


async def test_real_optin_saves_boolean_without_acquiring_consent(hass, hass_ws_client, settings_runtime, monkeypatch):
    from custom_components.daylight_calendar_import.hosted_runtime import (
        CONF_HOSTED_AUTOMATIC,
    )
    cfg = settings_runtime.entry
    client = await hass_ws_client(hass)
    request = AsyncMock(side_effect=AssertionError("No consent means no network"))
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_transport.HostedLifecycleTransport._request", request)
    result = await success(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE, automatic_lifecycle=True)
    assert result["automatic_lifecycle"] is True and result["state"] == "disconnected"
    assert cfg.options[CONF_HOSTED_AUTOMATIC] is True
    r = hass.data[DOMAIN][cfg.entry_id].hosted_runtime
    assert r._cancel_timer is None
    same = await success(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE, automatic_lifecycle=True)
    assert same == result and hass.data[DOMAIN][cfg.entry_id].hosted_runtime is r
    # Older clients cannot silently keep a previously enabled opt-in.
    disabled = await success(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE)
    assert disabled["automatic_lifecycle"] is False
    request.assert_not_called()


@pytest.mark.parametrize("lost_exchange", [False, True])
async def test_real_automatic_pairing_confirmation_and_restart_throttle(hass, hass_ws_client, settings_runtime, monkeypatch, freezer, lost_exchange):
    from datetime import UTC, datetime

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.daylight_calendar_import.hosted_runtime import (
        CONF_HOSTED_AUTOMATIC,
    )

    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_automation.secrets.randbelow", lambda _: 0)
    cfg = settings_runtime.entry
    client = await hass_ws_client(hass)
    freezer.move_to("2026-10-24T00:00:00Z")
    await success(client, api.WS_HOSTED_CONFIGURE, entry_id=cfg.entry_id, **SERVICE, automatic_lifecycle=True)
    assert cfg.options[CONF_HOSTED_AUTOMATIC] is True
    responses = {
        "start_pairing": {"pairing_id": "pairing_1234567890", "user_code": "ABCD1234EFGH",
            "verification_uri": "https://account.example.test/connect", "expires_at": "2026-10-24T00:10:00Z", "poll_interval_seconds": 5},
        "pairing_status": {"pairing_id": "pairing_1234567890", "status": "approved", "expires_at": "2026-10-24T00:10:00Z"},
        "exchange": {"installation_id": INSTALLATION, "installation_token": TOKEN,
            "expires_at": "2026-12-01T00:00:00Z", "confirmation_expires_at": "2026-10-24T00:10:00Z"},
        "confirm": {"installation_id": INSTALLATION, "expires_at": "2026-12-01T00:00:00Z"},
    }
    mocks = {}
    for name, response in responses.items():
        mocks[name] = AsyncMock(return_value=response)
        monkeypatch.setattr(f"custom_components.daylight_calendar_import.hosted_transport.HostedLifecycleTransport.{name}", mocks[name])
    if lost_exchange:
        mocks["exchange"].side_effect = HostedTransportError("service_unavailable", retryable=True)
    status = {"account_id": ACCOUNT, "installation_id": INSTALLATION, "credential_status": "current",
        "credential_expires_at": "2026-12-01T00:00:00Z", "account_url": "https://account.example.test/account"}
    checked = AsyncMock(side_effect=[HostedTransportError("unauthenticated", status=401), status])
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_transport.HostedLifecycleTransport.installation_status", checked)
    connected = await success(client, api.WS_HOSTED_ACTION, entry_id=cfg.entry_id, action="connect", processing_consent=True, installation_label="Home")
    assert connected["pairing"]["user_code"] == "ABCD1234EFGH"
    freezer.tick(4)
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done()
    mocks["pairing_status"].assert_not_called()
    freezer.tick(1)
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done()
    if lost_exchange:
        from homeassistant.helpers import issue_registry as ir
        assert (await success(client, api.WS_HOSTED_GET))["state"] == "automatic_attention_required"
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"hosted_lifecycle_{cfg.entry_id}") is not None
        assert await hass.config_entries.async_reload(cfg.entry_id)
        assert (await success(client, api.WS_HOSTED_GET))["state"] == "automatic_attention_required"
        mocks["exchange"].assert_awaited_once()
        mocks["confirm"].assert_not_called()
        checked.assert_not_called()
        return
    assert (await success(client, api.WS_HOSTED_GET))["state"] == "candidate_saved"
    freezer.tick(1)
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done()
    final = await success(client, api.WS_HOSTED_GET)
    assert final["state"] == "connected" and "pairing" not in final and TOKEN not in str(final)
    mocks["confirm"].assert_awaited_once_with(TOKEN)
    assert checked.await_count == 2
    assert await hass.config_entries.async_reload(cfg.entry_id)
    freezer.tick(100)
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done()
    assert checked.await_count == 2
    assert (await success(client, api.WS_HOSTED_GET))["state"] == "recovery_required"
    mocks["start_pairing"].assert_awaited_once()
    mocks["exchange"].assert_awaited_once()
