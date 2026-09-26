"""Daylight Calendar Import integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DESCRIPTION
from homeassistant.core import Context, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_PENDING_ID,
    ATTR_SOURCE_ID,
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_APPROVE_PENDING,
    SERVICE_IMPORT_TEXT,
    SERVICE_PARSE_TEXT,
    SERVICE_REJECT_PENDING,
    SERVICE_SUBMIT_TEXT,
)
from .models import EventDraft
from .parser import async_parse_text
from .storage import PendingImportApprovalUncertainError, PendingImportStore

PARSE_SCHEMA = vol.Schema({vol.Required(ATTR_TEXT): cv.string})
SUBMIT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TEXT): cv.string,
        vol.Optional(ATTR_SOURCE_ID): vol.All(
            cv.string,
            lambda value: value.strip(),
            vol.Length(min=1, max=2048),
        ),
    }
)
PENDING_SCHEMA = vol.Schema(
    {vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1))}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Daylight Calendar Import from a config entry."""
    pending_store = PendingImportStore(hass)
    await pending_store.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = pending_store

    async def handle_parse_text(call: ServiceCall) -> ServiceResponse:
        drafts = await _parse_for_entry(
            hass, entry, call.data[ATTR_TEXT], context=call.context
        )
        return {"events": [draft.as_dict() for draft in drafts]}

    async def handle_import_text(call: ServiceCall) -> ServiceResponse:
        drafts = await _parse_for_entry(
            hass, entry, call.data[ATTR_TEXT], context=call.context
        )
        for draft in drafts:
            await _async_create_calendar_event(
                hass,
                entry.data[CONF_CALENDAR_ENTITY],
                draft,
                context=call.context,
            )
        return {
            "events": [draft.as_dict() for draft in drafts],
            "imported": len(drafts),
        }

    async def handle_submit_text(call: ServiceCall) -> ServiceResponse:
        source_text = call.data[ATTR_TEXT]
        source_id = call.data.get(ATTR_SOURCE_ID)

        if (
            source_id is not None
            and pending_store.is_source_duplicate(source_id)
        ):
            return {
                "pending": None,
                "duplicate": True,
                "duplicate_source": True,
                "duplicate_events": 0,
            }

        drafts = await _parse_for_entry(
            hass, entry, source_text, context=call.context
        )
        result = await pending_store.async_add(
            source_text=source_text,
            events=drafts,
            source_id=source_id,
        )
        return {
            "pending": (
                result.pending.as_dict()
                if result.pending is not None
                else None
            ),
            "duplicate": (
                result.duplicate_source or result.duplicate_events > 0
            ),
            "duplicate_source": result.duplicate_source,
            "duplicate_events": result.duplicate_events,
        }

    async def handle_approve_pending(call: ServiceCall) -> ServiceResponse:
        calendar_entity = entry.data[CONF_CALENDAR_ENTITY]
        await _async_check_entity_control_permission(
            hass, calendar_entity, call.context
        )

        async def create_event(draft: EventDraft) -> None:
            await _async_create_calendar_event(
                hass,
                calendar_entity,
                draft,
                context=call.context,
            )

        pending_id = call.data[ATTR_PENDING_ID]
        try:
            pending = await pending_store.async_process_events(
                pending_id, create_event
            )
        except PendingImportApprovalUncertainError as err:
            raise ServiceValidationError(
                "Pending import has an unfinished approval attempt; "
                "automatic retry is blocked to avoid duplicates, so inspect "
                "the calendar before explicitly rejecting it"
            ) from err
        if pending is None:
            raise ServiceValidationError(
                f"Pending import not found: {pending_id}"
            )

        return {
            "pending_id": pending.id,
            "approved": True,
            "imported": len(pending.events),
            "events": [draft.as_dict() for draft in pending.events],
        }

    async def handle_reject_pending(call: ServiceCall) -> ServiceResponse:
        await _async_check_entity_control_permission(
            hass, entry.data[CONF_CALENDAR_ENTITY], call.context
        )
        pending_id = call.data[ATTR_PENDING_ID]
        if not await pending_store.async_remove(pending_id):
            raise ServiceValidationError(
                f"Pending import not found: {pending_id}"
            )
        return {"pending_id": pending_id, "rejected": True}

    hass.services.async_register(
        DOMAIN,
        SERVICE_PARSE_TEXT,
        handle_parse_text,
        schema=PARSE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_TEXT,
        handle_import_text,
        schema=PARSE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SUBMIT_TEXT,
        handle_submit_text,
        schema=SUBMIT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPROVE_PENDING,
        handle_approve_pending,
        schema=PENDING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REJECT_PENDING,
        handle_reject_pending,
        schema=PENDING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_PARSE_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_IMPORT_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_SUBMIT_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_APPROVE_PENDING)
    hass.services.async_remove(DOMAIN, SERVICE_REJECT_PENDING)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove persisted data when the config entry is deleted."""
    await PendingImportStore(hass).async_remove_storage()


async def _parse_for_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    text: str,
    *,
    context: Context | None = None,
) -> list[EventDraft]:
    ai_task_entity = entry.data[CONF_AI_TASK_ENTITY]
    await _async_check_entity_control_permission(hass, ai_task_entity, context)
    return await async_parse_text(
        hass,
        text=text,
        ai_task_entity=ai_task_entity,
    )


async def _async_check_entity_control_permission(
    hass: HomeAssistant,
    entity_id: str,
    context: Context | None,
) -> None:
    """Enforce Home Assistant entity-control permissions for a user call."""
    if context is None or context.user_id is None:
        return

    user = await hass.auth.async_get_user(context.user_id)
    if user is None:
        raise UnknownUser(
            context=context,
            permission=POLICY_CONTROL,
            user_id=context.user_id,
        )
    if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
        raise Unauthorized(
            context=context,
            permission=POLICY_CONTROL,
            user_id=context.user_id,
            perm_category=CAT_ENTITIES,
        )


async def _async_create_calendar_event(
    hass: HomeAssistant,
    calendar_entity: str,
    draft: EventDraft,
    *,
    context: Context | None = None,
) -> None:
    data: dict[str, Any] = {
        "entity_id": calendar_entity,
        "summary": draft.title,
        CONF_DESCRIPTION: draft.description or "",
    }
    if draft.location:
        data["location"] = draft.location

    if draft.all_day:
        data["start_date"] = draft.start
        data["end_date"] = draft.end
    else:
        data["start_date_time"] = draft.start
        data["end_date_time"] = draft.end

    await hass.services.async_call(
        "calendar", "create_event", data, blocking=True, context=context
    )
