"""Admin-only WebSocket settings API for the Daylight panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    CONF_EMAIL_ENABLED,
    CONF_EMAIL_HOST,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
    DOMAIN,
)
from .settings import (
    SettingsValidationError,
    async_validate_email_options,
    effective_core_options,
    normalize_core_options,
    settings_lock,
    settings_snapshot,
)

WS_GET_SETTINGS = f"{DOMAIN}/settings/get"
WS_UPDATE_CORE_SETTINGS = f"{DOMAIN}/settings/core/update"
WS_UPDATE_EMAIL_SETTINGS = f"{DOMAIN}/settings/email/update"


def _entry_for_message(hass: HomeAssistant, msg: dict[str, Any]) -> ConfigEntry:
    """Return the addressed entry, or the sole Daylight entry for panel reads."""
    entry_id = msg.get("entry_id")
    if entry_id is None:
        entries = hass.config_entries.async_entries(DOMAIN)
        if len(entries) != 1:
            raise SettingsValidationError(
                "entry_not_found", "Daylight Calendar Import configuration was not found."
            )
        return entries[0]

    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise SettingsValidationError(
            "entry_not_found", "Daylight Calendar Import configuration was not found."
        )
    return entry


async def _async_save_option_patch(
    hass: HomeAssistant,
    entry: ConfigEntry,
    patch: dict[str, Any],
) -> None:
    """Merge an option patch into the latest state and reload the entry."""
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, **patch},
    )
    if not await hass.config_entries.async_reload(entry.entry_id):
        raise SettingsValidationError(
            "reload_failed",
            "Settings were saved, but Daylight could not reload. "
            "Restart Home Assistant before relying on the new settings.",
        )


def _send_validation_error(
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    err: SettingsValidationError,
) -> None:
    """Return one stable settings validation failure."""
    connection.send_error(msg["id"], err.code, str(err))


@websocket_api.require_admin
@websocket_api.websocket_command({"type": WS_GET_SETTINGS})
@websocket_api.async_response
async def websocket_get_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return secret-safe Daylight settings."""
    try:
        entry = _entry_for_message(hass, msg)
    except SettingsValidationError as err:
        _send_validation_error(connection, msg, err)
        return
    connection.send_result(msg["id"], settings_snapshot(entry))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        "type": WS_UPDATE_CORE_SETTINGS,
        vol.Required("entry_id"): cv.string,
        vol.Optional(CONF_AI_TASK_ENTITY): cv.entity_id,
        vol.Optional(CONF_CALENDAR_ENTITY): cv.entity_id,
        vol.Optional(CONF_CALENDAR_ENTITIES): vol.All(
            [cv.entity_id],
            vol.Length(min=1),
        ),
    }
)
@websocket_api.async_response
async def websocket_update_core_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update AI and writable-calendar settings."""
    try:
        entry = _entry_for_message(hass, msg)
        async with settings_lock(hass, entry.entry_id):
            if not any(
                key in msg
                for key in (
                    CONF_AI_TASK_ENTITY,
                    CONF_CALENDAR_ENTITY,
                    CONF_CALENDAR_ENTITIES,
                )
            ):
                raise SettingsValidationError(
                    "invalid_settings", "At least one core setting must be provided."
                )

            current_ai, current_default, current_allowed = effective_core_options(entry)
            normalized = normalize_core_options(
                msg.get(CONF_AI_TASK_ENTITY, current_ai),
                msg.get(CONF_CALENDAR_ENTITY, current_default),
                msg.get(CONF_CALENDAR_ENTITIES, current_allowed),
            )
            patch: dict[str, Any] = {}
            if CONF_AI_TASK_ENTITY in msg:
                patch[CONF_AI_TASK_ENTITY] = normalized[CONF_AI_TASK_ENTITY]
            if (
                CONF_CALENDAR_ENTITY in msg
                or CONF_CALENDAR_ENTITIES in msg
            ):
                patch[CONF_CALENDAR_ENTITY] = normalized[CONF_CALENDAR_ENTITY]
                patch[CONF_CALENDAR_ENTITIES] = normalized[CONF_CALENDAR_ENTITIES]
            await _async_save_option_patch(hass, entry, patch)
    except SettingsValidationError as err:
        _send_validation_error(connection, msg, err)
        return

    connection.send_result(msg["id"], settings_snapshot(entry))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        "type": WS_UPDATE_EMAIL_SETTINGS,
        vol.Required("entry_id"): cv.string,
        vol.Required("enabled"): bool,
        vol.Optional("host"): cv.string,
        vol.Optional("port"): vol.Coerce(float),
        vol.Optional("username"): cv.string,
        vol.Optional("password"): cv.string,
        vol.Optional("mailbox"): cv.string,
        vol.Optional("verify_ssl"): bool,
    }
)
@websocket_api.async_response
async def websocket_update_email_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update Direct IMAP settings without exposing the stored password."""
    try:
        entry = _entry_for_message(hass, msg)
        async with settings_lock(hass, entry.entry_id):
            if not msg["enabled"]:
                options = {CONF_EMAIL_ENABLED: False}
            else:
                field_map = {
                    "host": CONF_EMAIL_HOST,
                    "port": CONF_EMAIL_PORT,
                    "username": CONF_EMAIL_USERNAME,
                    "password": CONF_EMAIL_PASSWORD,
                    "mailbox": CONF_EMAIL_MAILBOX,
                    "verify_ssl": CONF_EMAIL_VERIFY_SSL,
                }
                email_input = {
                    option_key: msg[api_key]
                    for api_key, option_key in field_map.items()
                    if api_key in msg
                }
                options = await async_validate_email_options(
                    entry.entry_id,
                    entry.options,
                    email_input,
                )
            await _async_save_option_patch(hass, entry, options)
    except SettingsValidationError as err:
        _send_validation_error(connection, msg, err)
        return

    connection.send_result(msg["id"], settings_snapshot(entry))


@callback
def async_register_settings_api(hass: HomeAssistant) -> None:
    """Register the Daylight settings WebSocket commands once."""
    websocket_api.async_register_command(hass, websocket_get_settings)
    websocket_api.async_register_command(hass, websocket_update_core_settings)
    websocket_api.async_register_command(hass, websocket_update_email_settings)
