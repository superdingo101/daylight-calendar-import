"""Shared settings model and validation for Daylight Calendar Import."""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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
    DOMAIN,
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

_SETTINGS_LOCKS = f"{DOMAIN}_settings_locks"


EMAIL_OPTION_KEYS = (
    CONF_EMAIL_HOST,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_VERIFY_SSL,
)


def settings_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Return the per-entry lock serializing settings transactions."""
    locks = hass.data.setdefault(_SETTINGS_LOCKS, {})
    return locks.setdefault(entry_id, asyncio.Lock())


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


def core_option_patch(
    baseline: tuple[str, str, list[str]],
    submitted: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only intentionally changed core options, normalized as atomic groups."""
    baseline_ai, baseline_default, baseline_allowed = baseline
    normalized = normalize_core_options(
        submitted.get(CONF_AI_TASK_ENTITY, baseline_ai),
        submitted.get(CONF_CALENDAR_ENTITY, baseline_default),
        list(submitted.get(CONF_CALENDAR_ENTITIES, baseline_allowed)),
    )
    patch: dict[str, Any] = {}
    if (
        CONF_AI_TASK_ENTITY in submitted
        and normalized[CONF_AI_TASK_ENTITY] != baseline_ai
    ):
        patch[CONF_AI_TASK_ENTITY] = normalized[CONF_AI_TASK_ENTITY]
    if (
        CONF_CALENDAR_ENTITY in submitted
        or CONF_CALENDAR_ENTITIES in submitted
    ) and (
        normalized[CONF_CALENDAR_ENTITY] != baseline_default
        or normalized[CONF_CALENDAR_ENTITIES] != baseline_allowed
    ):
        patch[CONF_CALENDAR_ENTITY] = normalized[CONF_CALENDAR_ENTITY]
        patch[CONF_CALENDAR_ENTITIES] = normalized[CONF_CALENDAR_ENTITIES]
    return patch


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



def normalize_calendar_aliases(
    aliases: Mapping[str, str], allowed_calendars: list[str],
) -> dict[str, str]:
    """Validate aliases as exact, Unicode-normalized writable-calendar choices."""
    if not isinstance(aliases, Mapping):
        raise SettingsValidationError("invalid_aliases", "Calendar aliases must be a mapping.")
    normalized: dict[str, str] = {}
    for alias, target in aliases.items():
        if not isinstance(alias, str) or "\n" in alias or "\r" in alias:
            raise SettingsValidationError("invalid_alias", "Calendar alias is invalid.")
        key = " ".join(unicodedata.normalize("NFKC", alias).split()).casefold()
        if not key or len(key) > 64:
            raise SettingsValidationError("invalid_alias", "Calendar alias is invalid.")
        if key in normalized:
            raise SettingsValidationError("duplicate_alias", "Calendar aliases must be unique.")
        if not isinstance(target, str) or target not in allowed_calendars:
            raise SettingsValidationError(
                "alias_target_not_allowed", "Calendar alias targets must be writable calendars."
            )
        normalized[key] = target
    return normalized


def normalize_conflict_calendars(calendars: list[str]) -> list[str]:
    """Preserve an explicitly selected read-only observation scope."""
    if not isinstance(calendars, list) or any(
        not isinstance(value, str) or not value.startswith("calendar.")
        for value in calendars
    ):
        raise SettingsValidationError(
            "invalid_conflict_calendars", "Conflict calendars must be calendar entities."
        )
    return list(dict.fromkeys(calendars))


def effective_calendar_intelligence(entry: ConfigEntry) -> tuple[dict[str, str], list[str]]:
    """Read v0.6 settings with safe legacy defaults and no implied write permission."""
    default_calendar, allowed = effective_calendar_options(entry)
    return (
        normalize_calendar_aliases(entry.options.get(CONF_CALENDAR_ALIASES, {}), allowed),
        normalize_conflict_calendars(
            entry.options.get(CONF_CONFLICT_CALENDAR_ENTITIES, [default_calendar])
        ),
    )


def calendar_intelligence_patch(
    entry: ConfigEntry, submitted: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate only the requested v0.6 fields using the current writable scope."""
    _, allowed = effective_calendar_options(entry)
    patch: dict[str, Any] = {}
    if CONF_CALENDAR_ALIASES in submitted:
        aliases = normalize_calendar_aliases(submitted[CONF_CALENDAR_ALIASES], allowed)
        if aliases != effective_calendar_intelligence(entry)[0]:
            patch[CONF_CALENDAR_ALIASES] = aliases
    if CONF_CONFLICT_CALENDAR_ENTITIES in submitted:
        conflicts = normalize_conflict_calendars(submitted[CONF_CONFLICT_CALENDAR_ENTITIES])
        if conflicts != effective_calendar_intelligence(entry)[1]:
            patch[CONF_CONFLICT_CALENDAR_ENTITIES] = conflicts
    return patch


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
    aliases, conflict_calendars = effective_calendar_intelligence(entry)
    return {
        "entry_id": entry.entry_id,
        "calendar_aliases": aliases,
        "conflict_calendar_entities": conflict_calendars,
        "ai_task_entity": ai_task_entity,
        "calendar_entity": default_calendar,
        "calendar_entities": allowed_calendars,
        "email": email_settings_snapshot(entry.options),
    }


async def async_save_option_patch(
    hass: HomeAssistant,
    entry: ConfigEntry,
    patch: Mapping[str, Any],
) -> None:
    """Merge an option patch into the latest state and reload the entry."""
    merged = {**entry.options, **patch}
    # A writable calendar may not be removed while an existing alias uses it.
    default, allowed = (
        merged.get(CONF_CALENDAR_ENTITY, entry.data[CONF_CALENDAR_ENTITY]),
        merged.get(
            CONF_CALENDAR_ENTITIES,
            entry.data.get(CONF_CALENDAR_ENTITIES, [entry.data[CONF_CALENDAR_ENTITY]]),
        ),
    )
    normalize_calendar_aliases(merged.get(CONF_CALENDAR_ALIASES, {}), allowed)
    if default not in allowed:
        raise SettingsValidationError(
            "default_not_allowed", "The default calendar must be included in the allowed calendars."
        )
    # Pending event destinations are durable; do not silently strand them.
    if CONF_CALENDAR_ENTITIES in patch:
        store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if store is not None and any(
            event.calendar_entity is not None and event.calendar_entity not in allowed
            for item in store.list()
            for event in item.events
        ):
            raise SettingsValidationError(
                "pending_calendar_in_use",
                "A pending event still uses a removed calendar. Reassign it before saving.",
            )
    hass.config_entries.async_update_entry(entry, options=merged)
    if not await hass.config_entries.async_reload(entry.entry_id):
        raise SettingsValidationError(
            "reload_failed",
            "Settings were saved, but Daylight could not reload. "
            "Restart Home Assistant before relying on the new settings.",
        )


async def async_validate_email_options(
    entry_id: str,
    current: Mapping[str, Any],
    user_input: Mapping[str, Any],
) -> dict[str, Any]:
    """Build and validate an enabled Direct IMAP option patch."""
    normalized = dict(user_input)
    port = normalized.get(CONF_EMAIL_PORT)
    if isinstance(port, float) and port.is_integer():
        normalized[CONF_EMAIL_PORT] = int(port)

    password = (
        normalized.get(CONF_EMAIL_PASSWORD)
        or current.get(CONF_EMAIL_PASSWORD, "")
    )
    email_patch = {
        CONF_EMAIL_ENABLED: True,
        **normalized,
        CONF_EMAIL_PASSWORD: password,
    }
    try:
        settings = direct_imap_settings_from_options(
            entry_id,
            {**current, **email_patch},
        )
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
    return email_patch
