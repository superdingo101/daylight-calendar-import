"""Daylight Calendar Import integration."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DESCRIPTION
from homeassistant.core import Context, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_EVENT_ID,
    ATTR_FILE_ID,
    ATTR_PENDING_ID,
    ATTR_SOURCE_ID,
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_APPROVE_PENDING,
    SERVICE_APPROVE_PENDING_EVENT,
    SERVICE_EDIT_PENDING_EVENT,
    SERVICE_GET_PENDING,
    SERVICE_GET_PENDING_EVENT,
    SERVICE_IMPORT_TEXT,
    SERVICE_LIST_PENDING,
    SERVICE_PARSE_TEXT,
    SERVICE_REJECT_PENDING,
    SERVICE_REJECT_PENDING_EVENT,
    SERVICE_RESOLVE_PENDING_EVENT,
    SERVICE_SUBMIT_TEXT,
    SERVICE_SUBMIT_IMAGE,
)
from .models import DraftValidationError, EventDraft
from .parser import ParseOutcome, async_parse_source as parse_source_with_provider
from .sources import SourceDocument, TextSourceAdapter
from .uploads import async_image_source
from .storage import (
    PendingEventEditError,
    PendingEventResolutionError,
    PendingEvent,
    PendingImportApprovalUncertainError,
    PendingImportStore,
)

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
SUBMIT_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_FILE_ID): vol.All(cv.string, vol.Match(r"^[0-9a-f]{32}$")),
        vol.Optional(ATTR_TEXT, default=""): cv.string,
        vol.Optional(ATTR_SOURCE_ID): vol.All(
            cv.string, lambda value: value.strip(), vol.Length(min=1, max=2048)
        ),
    }
)
PENDING_SCHEMA = vol.Schema(
    {vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1))}
)
PENDING_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(ATTR_EVENT_ID): vol.All(cv.string, vol.Length(min=1)),
    }
)
EDIT_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(ATTR_EVENT_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("event"): dict,
        vol.Optional(CONF_CALENDAR_ENTITY): cv.entity_id,
    }
)
RESOLVE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(ATTR_EVENT_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("resolution"): vol.In(("created", "not_created", "discard")),
    }
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Give existing single-calendar entries an explicit allowed calendar list."""
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_CALENDAR_ENTITIES: [entry.data[CONF_CALENDAR_ENTITY]]},
            version=2,
        )
    return entry.version == 2


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Daylight Calendar Import from a config entry."""
    pending_store = PendingImportStore(hass)
    await pending_store.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = pending_store
    allowed_calendars = entry.data.get(CONF_CALENDAR_ENTITIES, [entry.data[CONF_CALENDAR_ENTITY]])

    def event_calendar(event: PendingEvent) -> str:
        calendar_entity = event.calendar_entity or entry.data[CONF_CALENDAR_ENTITY]
        if calendar_entity not in allowed_calendars:
            raise ServiceValidationError("Event calendar is not in the allowed calendars")
        return calendar_entity

    async def handle_parse_text(call: ServiceCall) -> ServiceResponse:
        outcome = await _parse_for_entry(
            hass, entry, call.data[ATTR_TEXT], context=call.context
        )
        return {"events": [draft.as_dict() for draft in outcome.events],
                "warnings": outcome.warnings}

    async def handle_import_text(call: ServiceCall) -> ServiceResponse:
        outcome = await _parse_for_entry(
            hass, entry, call.data[ATTR_TEXT], context=call.context
        )
        for draft in outcome.events:
            await _async_create_calendar_event(
                hass,
                entry.data[CONF_CALENDAR_ENTITY],
                draft,
                context=call.context,
            )
        return {
            "events": [draft.as_dict() for draft in outcome.events],
            "imported": len(outcome.events),
            "warnings": outcome.warnings,
        }

    async def handle_submit_text(call: ServiceCall) -> ServiceResponse:
        source_id = call.data.get(ATTR_SOURCE_ID)
        ai_task_entity = entry.data[CONF_AI_TASK_ENTITY]
        await _async_check_entity_control_permission(
            hass, ai_task_entity, call.context
        )

        if (
            source_id is not None
            and pending_store.is_source_duplicate(source_id)
        ):
            return {
                "pending": None,
                "duplicate": True,
                "duplicate_source": True,
                "duplicate_events": 0,
                "warnings": [],
            }

        source = TextSourceAdapter().create(call.data[ATTR_TEXT], source_id=source_id)
        outcome = await _async_parse_source(hass, entry, source)
        result = await pending_store.async_add(
            source_text=source.text,
            events=outcome.events,
            source_id=source.upstream_source_id,
            calendar_entity=entry.data[CONF_CALENDAR_ENTITY],
        )
        return {
            "pending": (
                result.pending.as_service_dict()
                if result.pending is not None
                else None
            ),
            "duplicate": (
                result.duplicate_source or result.duplicate_events > 0
            ),
            "duplicate_source": result.duplicate_source,
            "duplicate_events": result.duplicate_events,
            "warnings": outcome.warnings,
        }

    async def handle_submit_image(call: ServiceCall) -> ServiceResponse:
        """Queue event drafts from an uploaded image and optional source text."""
        await _async_check_entity_control_permission(
            hass, entry.data[CONF_AI_TASK_ENTITY], call.context
        )
        source_id = call.data.get(ATTR_SOURCE_ID)
        async with async_image_source(hass, call.data[ATTR_FILE_ID]) as image:
            if source_id is not None and pending_store.is_source_duplicate(source_id):
                return {"pending": None, "duplicate": True, "duplicate_source": True,
                        "duplicate_events": 0, "warnings": []}
            source = replace(image, text=call.data.get(ATTR_TEXT, "").strip() or None,
                             upstream_source_id=source_id)
            outcome = await _async_parse_source(hass, entry, source)
            result = await pending_store.async_add(
                source_text=source.text or f"Image attachment (SHA-256: {source.attachments[0].sha256})",
                events=outcome.events,
                source_id=source.upstream_source_id,
                calendar_entity=entry.data[CONF_CALENDAR_ENTITY],
            )
        return {"pending": result.pending.as_service_dict() if result.pending else None,
                "duplicate": result.duplicate_source or result.duplicate_events > 0,
                "duplicate_source": result.duplicate_source,
                "duplicate_events": result.duplicate_events,
                "warnings": outcome.warnings}

    async def handle_approve_pending(call: ServiceCall) -> ServiceResponse:
        pending_id = call.data[ATTR_PENDING_ID]
        pending_to_approve = pending_store.get(pending_id)
        if pending_to_approve is None:
            await _async_check_entity_control_permission(
                hass, entry.data[CONF_CALENDAR_ENTITY], call.context
            )
        else:
            for calendar_entity in {event_calendar(event) for event in pending_to_approve.events}:
                await _async_check_entity_control_permission(hass, calendar_entity, call.context)

        async def create_event(event: PendingEvent) -> None:
            calendar_entity = event_calendar(event)
            await _async_check_entity_control_permission(
                hass, calendar_entity, call.context
            )
            await _async_create_calendar_event(
                hass,
                calendar_entity,
                event.draft,
                context=call.context,
            )

        try:
            pending = await pending_store.async_process_events(
                pending_id, create_event
            )
        except PendingImportApprovalUncertainError as err:
            raise ServiceValidationError(
                "Pending import has an unfinished approval attempt; "
                "automatic retry is blocked to avoid duplicates. Check the "
                "calendar and use resolve_pending_event for the uncertain event"
            ) from err
        if pending is None:
            raise ServiceValidationError(
                f"Pending import not found: {pending_id}"
            )

        return {
            "pending_id": pending.id,
            "approved": True,
            "imported": len(pending.events),
            "events": [event.draft.as_dict() for event in pending.events],
        }

    async def handle_reject_pending(call: ServiceCall) -> ServiceResponse:
        for calendar_entity in allowed_calendars:
            await _async_check_entity_control_permission(hass, calendar_entity, call.context)
        pending_id = call.data[ATTR_PENDING_ID]
        try:
            removed = await pending_store.async_remove(pending_id)
        except PendingImportApprovalUncertainError as err:
            raise ServiceValidationError(
                "Pending import has an uncertain calendar write; resolve it before rejecting"
            ) from err
        if not removed:
            raise ServiceValidationError(
                f"Pending import not found: {pending_id}"
            )
        return {"pending_id": pending_id, "rejected": True}

    async def check_read_permission(call: ServiceCall) -> None:
        """Protect stored source text and drafts from anonymous or limited users."""
        if call.context is None or call.context.user_id is None:
            raise Unauthorized(context=call.context, permission=POLICY_CONTROL)
        await _async_check_entity_control_permission(
            hass, entry.data[CONF_AI_TASK_ENTITY], call.context
        )
        for calendar_entity in allowed_calendars:
            await _async_check_entity_control_permission(hass, calendar_entity, call.context)

    async def handle_list_pending(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        return {"imports": [
            {
                "id": item.id,
                "created_at": item.created_at,
                "event_count": len(item.events),
                "title": item.events[0].draft.title,
                "approval_in_flight": item.approval_in_flight,
            }
            for item in pending_store.list()
        ]}

    async def handle_get_pending(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        pending_id = call.data[ATTR_PENDING_ID]
        pending = pending_store.get(pending_id)
        if pending is None:
            raise ServiceValidationError(f"Pending import not found: {pending_id}")
        result = pending.as_service_dict()
        result.pop("source_fingerprint", None)
        return {"pending": result}

    async def handle_get_pending_event(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        pending_id = call.data[ATTR_PENDING_ID]
        event_id = call.data[ATTR_EVENT_ID]
        event = pending_store.get_event(pending_id, event_id)
        if event is None:
            raise ServiceValidationError(
                f"Pending event not found: {pending_id}/{event_id}"
            )
        return {"pending_id": pending_id, "event": event.as_service_dict()}

    async def handle_edit_pending_event(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        pending_id = call.data[ATTR_PENDING_ID]
        event_id = call.data[ATTR_EVENT_ID]
        try:
            draft = EventDraft.from_mapping(call.data["event"])
            calendar_entity = call.data.get(CONF_CALENDAR_ENTITY)
            if calendar_entity is not None and calendar_entity not in allowed_calendars:
                raise ServiceValidationError("Calendar is not in the allowed calendars")
            edited = await pending_store.async_edit_event(
                pending_id, event_id, draft, calendar_entity=calendar_entity
            )
        except (DraftValidationError, PendingEventEditError) as err:
            raise ServiceValidationError(str(err)) from err
        if edited is None:
            raise ServiceValidationError(
                f"Pending event not found: {pending_id}/{event_id}"
            )
        return {"pending_id": pending_id, "event": edited.as_service_dict()}

    async def handle_reject_pending_event(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        pending_id = call.data[ATTR_PENDING_ID]
        event_id = call.data[ATTR_EVENT_ID]
        try:
            rejected = await pending_store.async_reject_event(pending_id, event_id)
        except PendingImportApprovalUncertainError as err:
            raise ServiceValidationError(
                "This event has an uncertain calendar write; resolve it before rejecting"
            ) from err
        if not rejected:
            raise ServiceValidationError(
                f"Pending event not found: {pending_id}/{event_id}"
            )
        return {"pending_id": pending_id, "event_id": event_id, "rejected": True}

    async def handle_approve_pending_event(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        pending_id = call.data[ATTR_PENDING_ID]
        event_id = call.data[ATTR_EVENT_ID]

        event_to_approve = pending_store.get_event(pending_id, event_id)
        if event_to_approve is not None:
            await _async_check_entity_control_permission(
                hass, event_calendar(event_to_approve), call.context
            )

        async def create_event(event: PendingEvent) -> None:
            calendar_entity = event_calendar(event)
            await _async_check_entity_control_permission(
                hass, calendar_entity, call.context
            )
            await _async_create_calendar_event(
                hass, calendar_entity, event.draft, context=call.context
            )

        try:
            event = await pending_store.async_approve_event(
                pending_id, event_id, create_event
            )
        except PendingImportApprovalUncertainError as err:
            raise ServiceValidationError(
                "This event has an uncertain calendar write; verify it before retrying"
            ) from err
        if event is None:
            raise ServiceValidationError(
                f"Pending event not found: {pending_id}/{event_id}"
            )
        return {
            "pending_id": pending_id,
            "event_id": event_id,
            "approved": True,
            "event": {**event.as_service_dict(), "status": "approved"},
        }

    async def handle_resolve_pending_event(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        pending_id = call.data[ATTR_PENDING_ID]
        event_id = call.data[ATTR_EVENT_ID]
        resolution = call.data["resolution"]
        try:
            resolved = await pending_store.async_resolve_uncertain(
                pending_id, event_id, resolution
            )
        except PendingEventResolutionError as err:
            raise ServiceValidationError(str(err)) from err
        if not resolved:
            raise ServiceValidationError(
                f"Pending event not found: {pending_id}/{event_id}"
            )
        return {"pending_id": pending_id, "event_id": event_id,
                "resolution": resolution}

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
        DOMAIN, SERVICE_SUBMIT_IMAGE, handle_submit_image,
        schema=SUBMIT_IMAGE_SCHEMA, supports_response=SupportsResponse.ONLY,
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
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_PENDING, handle_list_pending,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_PENDING, handle_get_pending,
        schema=PENDING_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_PENDING_EVENT, handle_get_pending_event,
        schema=PENDING_EVENT_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EDIT_PENDING_EVENT, handle_edit_pending_event,
        schema=EDIT_EVENT_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REJECT_PENDING_EVENT, handle_reject_pending_event,
        schema=PENDING_EVENT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_APPROVE_PENDING_EVENT, handle_approve_pending_event,
        schema=PENDING_EVENT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESOLVE_PENDING_EVENT, handle_resolve_pending_event,
        schema=RESOLVE_EVENT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_PARSE_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_IMPORT_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_SUBMIT_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_SUBMIT_IMAGE)
    hass.services.async_remove(DOMAIN, SERVICE_APPROVE_PENDING)
    hass.services.async_remove(DOMAIN, SERVICE_REJECT_PENDING)
    hass.services.async_remove(DOMAIN, SERVICE_LIST_PENDING)
    hass.services.async_remove(DOMAIN, SERVICE_GET_PENDING)
    hass.services.async_remove(DOMAIN, SERVICE_GET_PENDING_EVENT)
    hass.services.async_remove(DOMAIN, SERVICE_EDIT_PENDING_EVENT)
    hass.services.async_remove(DOMAIN, SERVICE_REJECT_PENDING_EVENT)
    hass.services.async_remove(DOMAIN, SERVICE_APPROVE_PENDING_EVENT)
    hass.services.async_remove(DOMAIN, SERVICE_RESOLVE_PENDING_EVENT)
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
) -> ParseOutcome:
    ai_task_entity = entry.data[CONF_AI_TASK_ENTITY]
    await _async_check_entity_control_permission(hass, ai_task_entity, context)
    source = TextSourceAdapter().create(text)
    return await _async_parse_source(hass, entry, source)


async def _async_parse_source(
    hass: HomeAssistant, entry: ConfigEntry, source: SourceDocument
) -> ParseOutcome:
    """Feed a normalized source into the current text parser boundary."""
    if source.text is None and not source.attachments:
        raise ServiceValidationError("This source has no text for the configured parser")
    return await parse_source_with_provider(
        hass,
        source=source,
        ai_task_entity=entry.data[CONF_AI_TASK_ENTITY],
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
