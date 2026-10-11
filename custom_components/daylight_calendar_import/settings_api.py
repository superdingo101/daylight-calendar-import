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
    CONF_CALENDAR_ALIASES,
    CONF_CONFLICT_CALENDAR_ENTITIES,
    CONF_EMAIL_ENABLED,
    CONF_EMAIL_HOST,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
    CONF_EMAIL_SENDER_ALLOWLIST,
    DOMAIN,
)
from .notification_preferences import (
    NotificationPreferencesError,
    normalize_notification_preferences,
    notification_preferences_snapshot,
)
from .settings import (
    SettingsValidationError,
    async_save_option_patch,
    async_validate_email_options,
    core_option_patch,
    calendar_intelligence_patch,
    notification_preferences_patch,
    effective_notification_preferences,
    effective_core_options,
    settings_lock,
    settings_snapshot,
)

WS_GET_SETTINGS = f"{DOMAIN}/settings/get"
WS_UPDATE_CORE_SETTINGS = f"{DOMAIN}/settings/core/update"
WS_UPDATE_EMAIL_SETTINGS = f"{DOMAIN}/settings/email/update"
WS_UPDATE_CALENDAR_INTELLIGENCE = f"{DOMAIN}/settings/calendar_intelligence/update"
WS_UPDATE_NOTIFICATIONS = f"{DOMAIN}/settings/notifications/update"


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

            patch = core_option_patch(
                effective_core_options(entry),
                msg,
            )
            if patch:
                await async_save_option_patch(hass, entry, patch)
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
        vol.Optional("sender_allowlist"): [cv.string],
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
                    "sender_allowlist": CONF_EMAIL_SENDER_ALLOWLIST,
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
            await async_save_option_patch(hass, entry, options)
    except SettingsValidationError as err:
        _send_validation_error(connection, msg, err)
        return

    connection.send_result(msg["id"], settings_snapshot(entry))



@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        "type": WS_UPDATE_CALENDAR_INTELLIGENCE,
        vol.Required("entry_id"): cv.string,
        vol.Optional(CONF_CALENDAR_ALIASES): dict,
        vol.Optional(CONF_CONFLICT_CALENDAR_ENTITIES): [cv.entity_id],
    }
)
@websocket_api.async_response
async def websocket_update_calendar_intelligence(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update only deterministic aliases and read-only conflict calendars."""
    try:
        entry = _entry_for_message(hass, msg)
        async with settings_lock(hass, entry.entry_id):
            if not any(
                field in msg
                for field in (CONF_CALENDAR_ALIASES, CONF_CONFLICT_CALENDAR_ENTITIES)
            ):
                raise SettingsValidationError(
                    "invalid_settings", "Provide calendar aliases or conflict calendars."
                )
            patch = calendar_intelligence_patch(entry, msg)
            if patch:
                await async_save_option_patch(hass, entry, patch)
    except SettingsValidationError as err:
        _send_validation_error(connection, msg, err)
        return
    connection.send_result(msg["id"], settings_snapshot(entry))


@websocket_api.require_admin
@websocket_api.websocket_command({
    "type": WS_UPDATE_NOTIFICATIONS,
    vol.Required("entry_id"): cv.string,
    vol.Required("notifications"): dict,
    vol.Required("expected_notifications"): dict,
})
@websocket_api.async_response
async def websocket_update_notifications(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update the full opt-in notification policy under the settings lock."""
    try:
        entry = _entry_for_message(hass, msg)
        async with settings_lock(hass, entry.entry_id):
            try:
                expected = notification_preferences_snapshot(
                    normalize_notification_preferences(msg["expected_notifications"])
                )
            except NotificationPreferencesError as err:
                raise SettingsValidationError("invalid_notifications", str(err)) from err
            current = notification_preferences_snapshot(effective_notification_preferences(entry))
            if expected != current:
                raise SettingsValidationError(
                    "notifications_changed",
                    "Notification settings changed elsewhere. Refresh and review before saving.",
                )
            patch = notification_preferences_patch(entry, msg["notifications"])
            if msg["notifications"].get("enabled") is True:
                selected = set(msg["notifications"].get("classes", ()))
                # Enabled policies must explicitly select the one class with
                # a delivery path. Previously stored hidden classes are kept
                # dormant for future implementations, but callers may not
                # introduce additional unsupported classes while opting in.
                if "review_ready" not in selected or not (
                    selected - {"review_ready"} <= set(current["classes"])
                ):
                    raise SettingsValidationError(
                        "invalid_notifications",
                        "Only review-ready notifications can be newly enabled.",
                    )
            if patch:
                await async_save_option_patch(hass, entry, patch)
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
    websocket_api.async_register_command(hass, websocket_update_calendar_intelligence)
    websocket_api.async_register_command(hass, websocket_update_notifications)
