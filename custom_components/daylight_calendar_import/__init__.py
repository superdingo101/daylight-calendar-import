"""Daylight Calendar Import integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DESCRIPTION
from homeassistant.core import Context, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_IMPORT_TEXT,
    SERVICE_PARSE_TEXT,
)
from .models import EventDraft
from .parser import async_parse_text

PARSE_SCHEMA = vol.Schema({vol.Required(ATTR_TEXT): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Daylight Calendar Import from a config entry."""

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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_PARSE_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_IMPORT_TEXT)
    return True


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
