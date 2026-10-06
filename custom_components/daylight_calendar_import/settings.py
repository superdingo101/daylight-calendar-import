"""Shared settings model and validation for Daylight Calendar Import."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry

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
)
from .direct_imap import (
    DirectImapAuthenticationError,
    DirectImapError,
    DirectImapMailboxError,
    DirectImapSource,
)
from .email_runtime import (
    DEFAULT_EMAIL_MAILBOX,
    DEFAULT_EMAIL_PORT,
    direct_imap_settings_from_options,
)

EMAIL_OPTION_KEYS = (
    CONF_EMAIL_HOST,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_VERIFY_SSL,
)


class SettingsValidationError(ValueError):
    """A user-facing settings validation failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def effective_ai_task_entity(entry: ConfigEntry) -> str:
    """Return the effective AI Task entity."""
    current = getattr(entry, "options", {})
    return current.get(
        CONF_AI_TASK_ENTITY,
        entry.data[CONF_AI_TASK_ENTITY],
    )


def effective_calendar_options(entry: ConfigEntry) -> tuple[str, list[str]]:
    """Return the effective default and writable calendars."""
    current = getattr(entry, "options", {})
    default_calendar = current.get(
        CONF_CALENDAR_ENTITY,
        entry.data[CONF_CALENDAR_ENTITY],
    )
    allowed_calendars = list(
        current.get(
            CONF_CALENDAR_ENTITIES,
            entry.data.get(
                CONF_CALENDAR_ENTITIES,
                [entry.data[CONF_CALENDAR_ENTITY]],
            ),
        )
    )
    return (
        default_calendar,
        list(dict.fromkeys((*allowed_calendars, default_calendar))),
    )


def effective_core_options(entry: ConfigEntry) -> tuple[str, str, list[str]]:
    """Return effective AI and writable-calendar settings."""
    default_calendar, allowed_calendars = effective_calendar_options(entry)
    return (
        effective_ai_task_entity(entry),
        default_calendar,
        allowed_calendars,
    )


def normalize_core_options(
    ai_task_entity: str,
    default_calendar: str,
    allowed_calendars: list[str],
) -> dict[str, Any]:
    """Normalize one core-settings submission."""
    if not ai_task_entity.startswith("ai_task."):
        raise SettingsValidationError(
            "invalid_ai_task", "The selected AI Task entity is invalid."
        )
    allowed = list(dict.fromkeys(allowed_calendars))
    if (
        not default_calendar.startswith("calendar.")
        or any(not entity.startswith("calendar.") for entity in allowed)
    ):
        raise SettingsValidationError(
            "invalid_calendar", "Writable calendars must be calendar entities."
        )
    if default_calendar not in allowed:
        raise SettingsValidationError(
            "default_not_allowed",
            "The default calendar must be included in the allowed calendars.",
        )
    return {
        CONF_AI_TASK_ENTITY: ai_task_entity,
        CONF_CALENDAR_ENTITY: default_calendar,
        CONF_CALENDAR_ENTITIES: allowed,
    }


def email_settings_snapshot(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return Direct IMAP settings safe to expose to the frontend."""
    return {
        "enabled": bool(options.get(CONF_EMAIL_ENABLED, False)),
        "host": options.get(CONF_EMAIL_HOST, ""),
        "port": options.get(CONF_EMAIL_PORT, DEFAULT_EMAIL_PORT),
        "username": options.get(CONF_EMAIL_USERNAME, ""),
        "password_configured": bool(options.get(CONF_EMAIL_PASSWORD)),
        "mailbox": options.get(CONF_EMAIL_MAILBOX, DEFAULT_EMAIL_MAILBOX),
        "verify_ssl": options.get(CONF_EMAIL_VERIFY_SSL, True),
    }


def settings_snapshot(entry: ConfigEntry) -> dict[str, Any]:
    """Return the complete secret-safe settings view for one entry."""
    ai_task_entity, default_calendar, allowed_calendars = effective_core_options(entry)
    return {
        "entry_id": entry.entry_id,
        "ai_task_entity": ai_task_entity,
        "calendar_entity": default_calendar,
        "calendar_entities": allowed_calendars,
        "email": email_settings_snapshot(entry.options),
    }


async def async_validate_email_options(
    entry_id: str,
    current: Mapping[str, Any],
    user_input: Mapping[str, Any],
) -> dict[str, Any]:
    """Build and validate enabled Direct IMAP options."""
    normalized = dict(user_input)
    port = normalized.get(CONF_EMAIL_PORT)
    if isinstance(port, float) and port.is_integer():
        normalized[CONF_EMAIL_PORT] = int(port)

    password = (
        normalized.get(CONF_EMAIL_PASSWORD)
        or current.get(CONF_EMAIL_PASSWORD, "")
    )
    options = {
        **current,
        CONF_EMAIL_ENABLED: True,
        **normalized,
        CONF_EMAIL_PASSWORD: password,
    }
    try:
        settings = direct_imap_settings_from_options(entry_id, options)
        await DirectImapSource(settings).async_validate()
    except DirectImapAuthenticationError as err:
        raise SettingsValidationError(
            "invalid_auth", "The IMAP server rejected the supplied credentials."
        ) from err
    except DirectImapMailboxError as err:
        raise SettingsValidationError(
            "invalid_mailbox", "The configured IMAP mailbox could not be selected."
        ) from err
    except (KeyError, ValueError) as err:
        raise SettingsValidationError(
            "invalid_email_config", "The Direct IMAP settings are incomplete or invalid."
        ) from err
    except DirectImapError as err:
        raise SettingsValidationError(
            "cannot_connect", "Could not connect to the Direct IMAP mailbox."
        ) from err
    return options
