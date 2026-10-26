"""Native admin-only P02 configuration and explicit lifecycle actions."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN
from .hosted_runtime import (
    CONF_HOSTED_AUTOMATIC,
    CONF_HOSTED_SERVICE,
    HostedConnectionRuntime,
    HostedRuntimeError,
    hosted_signal,
)
from .hosted_transport import HostedServiceBinding
from .settings import SettingsValidationError, async_save_option_patch, settings_lock
from .settings_api import _entry_for_message

WS_HOSTED_GET = f"{DOMAIN}/hosted/get"
WS_HOSTED_CONFIGURE = f"{DOMAIN}/hosted/configure"
WS_HOSTED_SUBSCRIBE = f"{DOMAIN}/hosted/subscribe"
WS_HOSTED_ACTION = f"{DOMAIN}/hosted/action"
_ERRORS = {
    "hosted_busy", "hosted_unloading", "hosted_unavailable", "invalid_hosted_action",
    "hosted_action_failed", "consent_required", "confirmation_required", "reload_failed",
    "invalid_hosted_configuration", "entry_not_found",
}


def _runtime(hass: HomeAssistant, entry: Any) -> HostedConnectionRuntime:
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    runtime = getattr(store, "hosted_runtime", None)
    if (store is None or getattr(store, "accepting_services", False) is not True
            or runtime is None or not runtime.accepting):
        raise HostedRuntimeError("hosted_unloading")
    return runtime


def _error(connection: Any, msg: dict[str, Any], code: str) -> None:
    connection.send_error(
        msg["id"], code if code in _ERRORS else "hosted_action_failed",
        "Daylight connection action could not complete. Check connection settings.",
    )


@websocket_api.require_admin
@websocket_api.websocket_command({
    "type": WS_HOSTED_GET, vol.Optional("entry_id"): cv.string,
})
@websocket_api.async_response
async def websocket_hosted_get(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Return only the shared entry owner's safe connection state."""
    try:
        entry = _entry_for_message(hass, msg)
        result = _runtime(hass, entry).snapshot()
    except SettingsValidationError:
        _error(connection, msg, "entry_not_found")
    except HostedRuntimeError as error:
        _error(connection, msg, str(error))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({
    "type": WS_HOSTED_CONFIGURE,
    vol.Required("entry_id"): cv.string,
    vol.Required("api_origin"): vol.All(str, vol.Length(min=1, max=2048)),
    vol.Required("account_origin"): vol.All(str, vol.Length(min=1, max=2048)),
    vol.Optional("automatic_lifecycle"): bool,
})
@websocket_api.async_response
async def websocket_hosted_configure(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Save trusted destinations without granting consent or relabelling secrets."""
    try:
        entry = _entry_for_message(hass, msg)
        try:
            service = HostedServiceBinding(msg["api_origin"], msg["account_origin"])
        except ValueError:
            raise HostedRuntimeError("invalid_hosted_configuration") from None
        async with settings_lock(hass, entry.entry_id):
            runtime = _runtime(hass, entry)
            if runtime.busy:
                raise HostedRuntimeError("hosted_busy")
            canonical = asdict(service)
            automatic = msg.get("automatic_lifecycle")
            if automatic is None:
                # Missing fields on a first setup default to safe bounded
                # maintenance; older clients must not override an already
                # saved opt-out merely by updating the service origins.
                automatic = (True if entry.options.get(CONF_HOSTED_SERVICE) is None
                             else entry.options.get(CONF_HOSTED_AUTOMATIC) is True)
            if (entry.options.get(CONF_HOSTED_SERVICE) == canonical
                    and (entry.options.get(CONF_HOSTED_AUTOMATIC) is True) == automatic):
                result = runtime.snapshot()
            else:
                before = dict(entry.options)
                old_store = hass.data[DOMAIN][entry.entry_id]
                previous_guard = getattr(old_store, "hosted_settings_reload_guard", False)
                old_store.hosted_settings_reload_guard = True
                runtime.pause()
                try:
                    await async_save_option_patch(hass, entry, {
                        CONF_HOSTED_SERVICE: canonical, CONF_HOSTED_AUTOMATIC: automatic,
                    })
                finally:
                    # Only an unchanged configuration may reopen the old owner.
                    # Saved origins with failed reload must remain fail-closed.
                    if entry.options == before:
                        old_store.hosted_settings_reload_guard = previous_guard
                        if (hass.data.get(DOMAIN, {}).get(entry.entry_id) is old_store
                                and old_store.accepting_services and not previous_guard):
                            runtime.resume()
                    else:
                        runtime.pause()
                loaded = _runtime(hass, entry)
                result = loaded.snapshot()
                if ((result["api_origin"], result["account_origin"]) != (
                    service.api_origin, service.account_origin,
                ) or result.get("automatic_lifecycle", False) != automatic):
                    loaded.pause()
                    raise HostedRuntimeError("reload_failed")
    except SettingsValidationError as error:
        _error(connection, msg, "reload_failed" if error.code == "reload_failed" else "invalid_hosted_configuration")
    except HostedRuntimeError as error:
        _error(connection, msg, str(error))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({
    "type": WS_HOSTED_ACTION,
    vol.Required("entry_id"): cv.string,
    vol.Required("action"): vol.In(("connect", "reconnect", "poll", "reconcile", "rotate", "disconnect")),
    vol.Optional("processing_consent"): bool,
    vol.Optional("installation_label"): vol.All(str, vol.Length(min=1, max=128)),
    vol.Optional("confirm_disconnect"): bool,
    vol.Optional("confirm_reconnect"): bool,
})
@websocket_api.async_response
async def websocket_hosted_action(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Perform one explicit action; consent and disconnect confirmation are separate."""
    try:
        entry = _entry_for_message(hass, msg)
        action = msg["action"]
        connect_keys = {"processing_consent", "installation_label"} & msg.keys()
        if ((action not in ("connect", "reconnect") and connect_keys)
                or (action != "disconnect" and "confirm_disconnect" in msg)
                or (action != "reconnect" and "confirm_reconnect" in msg)):
            raise HostedRuntimeError("invalid_hosted_action")
        kwargs = {}
        if action in ("connect", "reconnect"):
            if msg.get("processing_consent") is not True or "installation_label" not in msg:
                raise HostedRuntimeError("consent_required")
            kwargs = {"processing_consent": True, "installation_label": msg["installation_label"]}
        if action == "reconnect" and msg.get("confirm_reconnect") is not True:
            raise HostedRuntimeError("confirmation_required")
        if action == "disconnect" and msg.get("confirm_disconnect") is not True:
            raise HostedRuntimeError("confirmation_required")
        result = await _runtime(hass, entry).async_action(action, **kwargs)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        _error(connection, msg, "hosted_unloading")
    except SettingsValidationError:
        _error(connection, msg, "entry_not_found")
    except HostedRuntimeError as error:
        _error(connection, msg, str(error))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({
    "type": WS_HOSTED_SUBSCRIBE, vol.Required("entry_id"): cv.string,
})
@websocket_api.async_response
async def websocket_hosted_subscribe(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Send invalidations only; administrators fetch the current owner's snapshot."""
    try:
        entry = _entry_for_message(hass, msg)
        _runtime(hass, entry)
    except SettingsValidationError:
        _error(connection, msg, "entry_not_found")
    except HostedRuntimeError as error:
        _error(connection, msg, str(error))
    else:
        @callback
        def changed() -> None:
            connection.send_event(msg["id"], {"entry_id": entry.entry_id})
        connection.subscriptions[msg["id"]] = async_dispatcher_connect(
            hass, hosted_signal(entry.entry_id), changed,
        )
        connection.send_result(msg["id"])
        changed()


@callback
def async_register_hosted_settings_api(hass: HomeAssistant) -> None:
    """Register admin-only native commands without making a network request."""
    for command in (websocket_hosted_get, websocket_hosted_configure, websocket_hosted_action, websocket_hosted_subscribe):
        websocket_api.async_register_command(hass, command)
