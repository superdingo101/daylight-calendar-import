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
    CONF_PARSER_PROVIDER,
    PARSER_PROVIDER_AI_TASK,
    PARSER_PROVIDER_DAYLIGHT_CLOUD,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    CONF_CALENDAR_ALIASES,
    CONF_CONFLICT_CALENDAR_ENTITIES,
    CONF_NOTIFICATION_PREFERENCES,
    CONF_EMAIL_ENABLED,
    CONF_EMAIL_HOST,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
    CONF_EMAIL_SENDER_ALLOWLIST,
    DOMAIN,
    MAX_CALENDAR_ALIASES,
)
from .direct_imap import (
    DirectImapAuthenticationError,
    DirectImapError,
    DirectImapMailboxError,
    DirectImapSource,
)
from .email_safety import normalize_sender_allowlist
from .notification_preferences import (
    NotificationPreferences,
    NotificationPreferencesError,
    normalize_notification_preferences,
    notification_preferences_snapshot,
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


def effective_parser_provider(entry: ConfigEntry) -> str:
    """Read the explicit parser choice, retaining BYO for every legacy entry."""
    provider = entry.options.get(
        CONF_PARSER_PROVIDER, entry.data.get(CONF_PARSER_PROVIDER, PARSER_PROVIDER_AI_TASK)
    )
    if provider not in (PARSER_PROVIDER_AI_TASK, PARSER_PROVIDER_DAYLIGHT_CLOUD):
        raise SettingsValidationError("invalid_parser_provider", "Choose a supported parser provider.")
    return provider


def effective_ai_task_entity(entry: ConfigEntry) -> str | None:
    """Return the saved BYO selection, even when hosted parsing is selected."""
    return entry.options.get(CONF_AI_TASK_ENTITY, entry.data.get(CONF_AI_TASK_ENTITY))


def effective_calendar_options(entry: ConfigEntry) -> tuple[str | None, list[str]]:
    """Return configured writable calendars; a new hosted entry may have none."""
    current = getattr(entry, "options", {})
    default_calendar = current.get(CONF_CALENDAR_ENTITY, entry.data.get(CONF_CALENDAR_ENTITY))
    allowed_calendars = list(current.get(
        CONF_CALENDAR_ENTITIES,
        entry.data.get(
            CONF_CALENDAR_ENTITIES,
            [entry.data[CONF_CALENDAR_ENTITY]] if entry.data.get(CONF_CALENDAR_ENTITY) else [],
        ),
    ))
    if default_calendar is not None:
        allowed_calendars.append(default_calendar)
    return default_calendar, list(dict.fromkeys(allowed_calendars))


def effective_core_options(entry: ConfigEntry) -> tuple[str | None, str | None, list[str]]:
    """Return saved AI and writable-calendar settings."""
    default_calendar, allowed_calendars = effective_calendar_options(entry)
    return effective_ai_task_entity(entry), default_calendar, allowed_calendars


def core_option_patch(
    baseline: tuple[str | None, str | None, list[str]],
    submitted: Mapping[str, Any],
    *, parser_provider: str = PARSER_PROVIDER_AI_TASK,
) -> dict[str, Any]:
    """Return only intentionally changed core options, normalized as atomic groups."""
    baseline_ai, baseline_default, baseline_allowed = baseline
    normalized = normalize_core_options(
        submitted.get(CONF_AI_TASK_ENTITY, baseline_ai),
        submitted.get(CONF_CALENDAR_ENTITY, baseline_default),
        list(submitted.get(CONF_CALENDAR_ENTITIES, baseline_allowed)),
        parser_provider=submitted.get(CONF_PARSER_PROVIDER, parser_provider),
    )
    patch: dict[str, Any] = {}
    if CONF_PARSER_PROVIDER in submitted and submitted[CONF_PARSER_PROVIDER] != parser_provider:
        patch[CONF_PARSER_PROVIDER] = submitted[CONF_PARSER_PROVIDER]
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
    ai_task_entity: str | None,
    default_calendar: str | None,
    allowed_calendars: list[str],
    *, parser_provider: str = PARSER_PROVIDER_AI_TASK,
) -> dict[str, Any]:
    """Normalize one core-settings submission."""
    if parser_provider not in (PARSER_PROVIDER_AI_TASK, PARSER_PROVIDER_DAYLIGHT_CLOUD):
        raise SettingsValidationError("invalid_parser_provider", "Choose a supported parser provider.")
    hosted = parser_provider == PARSER_PROVIDER_DAYLIGHT_CLOUD
    if not (hosted and ai_task_entity is None) and (
        not isinstance(ai_task_entity, str) or not ai_task_entity.startswith("ai_task.")
    ):
        raise SettingsValidationError(
            "invalid_ai_task", "The selected AI Task entity is invalid."
        )
    allowed = list(dict.fromkeys(allowed_calendars))
    if (
        (default_calendar is None and (not hosted or bool(allowed)))
        or (default_calendar is not None and (
            not isinstance(default_calendar, str) or not default_calendar.startswith("calendar.")
        ))
        or any(not isinstance(entity, str) or not entity.startswith("calendar.") for entity in allowed)
    ):
        raise SettingsValidationError(
            "invalid_calendar", "Writable calendars must be calendar entities."
        )
    if default_calendar is not None and default_calendar not in allowed:
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
    aliases: Mapping[str, str], allowed_calendars: list[str], *,
    enforce_count_limit: bool = True,
) -> dict[str, str]:
    """Validate aliases as exact, Unicode-normalized writable-calendar choices."""
    if not isinstance(aliases, Mapping):
        raise SettingsValidationError("invalid_aliases", "Calendar aliases must be a mapping.")
    normalized: dict[str, str] = {}
    if enforce_count_limit and len(aliases) > MAX_CALENDAR_ALIASES:
        raise SettingsValidationError(
            "too_many_aliases", "Configure at most 100 calendar aliases."
        )
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
        # Older versions accepted larger alias sets. Preserve their destinations
        # during setup and reads; enforce the cap only on replacement submissions.
        normalize_calendar_aliases(
            entry.options.get(CONF_CALENDAR_ALIASES, {}), allowed,
            enforce_count_limit=False,
        ),
        normalize_conflict_calendars(
            entry.options.get(CONF_CONFLICT_CALENDAR_ENTITIES, [default_calendar] if default_calendar else [])
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
        # Every check also reads its writable destination, which may not be
        # in the read-only conflict list. Keep that union within the bounded
        # 16-calendar observation budget for every possible destination.
        if len(conflicts) > 15:
            raise SettingsValidationError(
                "conflict_calendar_limit",
                "Select no more than 15 conflict calendars; the destination "
                "calendar is checked separately.",
            )
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
        "sender_allowlist": list(options.get(CONF_EMAIL_SENDER_ALLOWLIST, ())),
    }


def effective_notification_preferences(entry: ConfigEntry) -> NotificationPreferences:
    """Read effective options with legacy fallback, failing closed on corrupt state."""
    raw = entry.options.get(
        CONF_NOTIFICATION_PREFERENCES,
        entry.data.get(CONF_NOTIFICATION_PREFERENCES, {}),
    )
    try:
        return normalize_notification_preferences(raw)
    except NotificationPreferencesError:
        return NotificationPreferences()


def notification_preferences_patch(
    entry: ConfigEntry, submitted: Mapping[str, object],
) -> dict[str, object]:
    """Validate the complete notification policy; never infer opt-in."""
    try:
        requested = normalize_notification_preferences(submitted)
    except NotificationPreferencesError as err:
        raise SettingsValidationError("invalid_notifications", str(err)) from err
    normalized = notification_preferences_snapshot(requested)
    if normalized == notification_preferences_snapshot(effective_notification_preferences(entry)):
        return {}
    return {CONF_NOTIFICATION_PREFERENCES: normalized}


def settings_snapshot(entry: ConfigEntry) -> dict[str, Any]:
    """Return the complete secret-safe settings view for one entry."""
    ai_task_entity, default_calendar, allowed_calendars = effective_core_options(entry)
    aliases, conflict_calendars = effective_calendar_intelligence(entry)
    return {
        "entry_id": entry.entry_id,
        "parser_provider": effective_parser_provider(entry),
        "calendar_aliases": aliases,
        "conflict_calendar_entities": conflict_calendars,
        "ai_task_entity": ai_task_entity,
        "calendar_entity": default_calendar,
        "calendar_entities": allowed_calendars,
        "email": email_settings_snapshot(entry.options),
        "notifications": notification_preferences_snapshot(effective_notification_preferences(entry)),
    }


async def async_save_option_patch(
    hass: HomeAssistant,
    entry: ConfigEntry,
    patch: Mapping[str, Any],
) -> None:
    """Merge an option patch into the latest state and reload the entry."""
    merged = {**entry.options, **patch}
    # A writable calendar may not be removed while an existing alias uses it.
    provider = merged.get(
        CONF_PARSER_PROVIDER, entry.data.get(CONF_PARSER_PROVIDER, PARSER_PROVIDER_AI_TASK)
    )
    default = merged.get(CONF_CALENDAR_ENTITY, entry.data.get(CONF_CALENDAR_ENTITY))
    allowed = merged.get(
        CONF_CALENDAR_ENTITIES,
        entry.data.get(
            CONF_CALENDAR_ENTITIES,
            [entry.data[CONF_CALENDAR_ENTITY]] if entry.data.get(CONF_CALENDAR_ENTITY) else [],
        ),
    )
    normalize_core_options(
        merged.get(CONF_AI_TASK_ENTITY, entry.data.get(CONF_AI_TASK_ENTITY)),
        default, list(allowed), parser_provider=provider,
    )
    normalize_calendar_aliases(
        merged.get(CONF_CALENDAR_ALIASES, {}), allowed,
        enforce_count_limit=CONF_CALENDAR_ALIASES in patch,
    )
    # A calendar scope change is rejected when its authoritative queue or
    # admission state is unavailable; no speculative second storage instance.
    old_default, old_allowed = effective_calendar_options(entry)
    calendar_scope_changed = (
        default != old_default or set(allowed) != set(old_allowed)
    )
    parser_changed = provider != effective_parser_provider(entry)
    runtime_scope_changed = calendar_scope_changed or parser_changed
    pending_store = getattr(hass, "data", {}).get(DOMAIN, {}).get(entry.entry_id)
    if runtime_scope_changed and pending_store is None:
        raise SettingsValidationError(
            "pending_store_unavailable",
            "Load Daylight Calendar Import before changing writable calendars. "
            "The stored pending queue could not be checked.",
        )

    def verify_pending_and_admission() -> None:
        if not getattr(pending_store, "accepting_services", True):
            raise SettingsValidationError(
                "pending_store_unavailable",
                "Daylight is unloading or reloading. Retry after it is loaded.",
            )
        runtime = getattr(pending_store, "email_runtime", None)
        poll = getattr(runtime, "_task", None)
        if (
            getattr(pending_store, "active_submissions", ())
            or getattr(pending_store, "active_service_handlers", ())
            or (poll is not None and not poll.done())
        ):
            raise SettingsValidationError(
                "calendar_change_busy",
                "Daylight is processing a submission. Retry the calendar "
                "change after ingestion finishes.",
            )
        for pending in pending_store.list() if calendar_scope_changed else ():
            for event in pending.events:
                destination = event.calendar_entity or old_default
                # A newly hosted entry can already contain drafts with no
                # destination. Adding its first calendar must not trap those
                # drafts or silently resolve them to a new default.
                if (old_default is None and event.calendar_entity is None
                        and event.routing_unresolved):
                    continue
                if destination not in allowed:
                    raise SettingsValidationError(
                        "pending_destination_not_allowed",
                        "A pending event targets a calendar being removed. "
                        "Reassign or reject the event before saving settings.",
                    )
                if event.calendar_entity is None and default != old_default:
                    raise SettingsValidationError(
                        "pending_default_would_change",
                        "A pending event uses the current default calendar. "
                        "Assign it an explicit destination before changing the default.",
                    )

    was_accepting = True
    if runtime_scope_changed:
        # The store lock serializes this validation against durable pending edits.
        # Closing service/poll admission until reload prevents new work from
        # committing an old destination in the update-to-unload interval.
        async with pending_store._lock:
            verify_pending_and_admission()
            was_accepting = getattr(pending_store, "accepting_services", True)
            pending_store.accepting_services = False
            # HA's unload path normally reopens admission after failed platform
            # unload. Suppress that rollback while persisted options may differ
            # from the old entry's captured calendar routing.
            pending_store.calendar_settings_reload_guard = True
            try:
                hass.config_entries.async_update_entry(entry, options=merged)
            except BaseException:
                # Persistence failed before the new options were committed.
                # Restore service admission only while the old runtime is valid.
                pending_store.calendar_settings_reload_guard = False
                pending_store.accepting_services = was_accepting
                raise
    else:
        hass.config_entries.async_update_entry(entry, options=merged)
    reloaded = False
    try:
        reloaded = await hass.config_entries.async_reload(entry.entry_id)
    finally:
        if (
            runtime_scope_changed
            and hass.data.get(DOMAIN, {}).get(entry.entry_id) is pending_store
        ):
            # Restore only after a successful reload. A failed unload must
            # never reopen the old closures under newly persisted options.
            pending_store.accepting_services = was_accepting if reloaded else False
            if reloaded:
                pending_store.calendar_settings_reload_guard = False
    if not reloaded:
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
    if CONF_EMAIL_SENDER_ALLOWLIST in normalized:
        try:
            normalized[CONF_EMAIL_SENDER_ALLOWLIST] = list(normalize_sender_allowlist(
                normalized[CONF_EMAIL_SENDER_ALLOWLIST]
            ))
        except (TypeError, ValueError) as err:
            raise SettingsValidationError(
                "invalid_sender_allowlist", "Enter valid, complete sender email addresses."
            ) from err
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
