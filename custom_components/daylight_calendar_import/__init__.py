"""Daylight Calendar Import integration."""

from __future__ import annotations

import asyncio
import logging
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
    SERVICE_LIST_ACTIVITY,
    SERVICE_GET_ACTIVITY,
    SERVICE_PARSE_TEXT,
    SERVICE_REJECT_PENDING,
    SERVICE_REJECT_PENDING_EVENT,
    SERVICE_RESOLVE_PENDING_EVENT,
    SERVICE_SUBMIT_TEXT,
    SERVICE_SUBMIT_IMAGE,
    SERVICE_SUBMIT_PDF,
)
from .email_runtime import (
    async_setup_email_runtime,
    email_review_source_text,
)
from .models import DraftValidationError, EventDraft
from .parser import ParseOutcome, async_parse_source as parse_source_with_provider
from .pdfs import async_pdf_source
from .providers import SourceValidationError
from .review_panel import async_register_review_panel, async_remove_review_panel
from .settings import effective_ai_task_entity, effective_calendar_options
from .settings_api import async_register_settings_api
from .sources import SourceDocument, SourceKind, TextSourceAdapter
from .uploads import async_image_source
from .storage import (
    PendingEventEditError,
    PendingEventResolutionError,
    PendingEvent,
    PendingImportApprovalUncertainError,
    PendingImportStore,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

_ENTRY_SERVICES = (
    SERVICE_PARSE_TEXT,
    SERVICE_IMPORT_TEXT,
    SERVICE_SUBMIT_TEXT,
    SERVICE_SUBMIT_IMAGE,
    SERVICE_SUBMIT_PDF,
    SERVICE_APPROVE_PENDING,
    SERVICE_REJECT_PENDING,
    SERVICE_LIST_PENDING,
    SERVICE_LIST_ACTIVITY,
    SERVICE_GET_ACTIVITY,
    SERVICE_GET_PENDING,
    SERVICE_GET_PENDING_EVENT,
    SERVICE_EDIT_PENDING_EVENT,
    SERVICE_REJECT_PENDING_EVENT,
    SERVICE_APPROVE_PENDING_EVENT,
    SERVICE_RESOLVE_PENDING_EVENT,
)


def _unregister_entry_services(hass: HomeAssistant) -> None:
    """Remove each registered service; safe for partial entry setup."""
    for name in _ENTRY_SERVICES:
        hass.services.async_remove(DOMAIN, name)



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
SUBMIT_PDF_SCHEMA = SUBMIT_IMAGE_SCHEMA
PENDING_SCHEMA = vol.Schema(
    {vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1))}
)
PENDING_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(ATTR_EVENT_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("expected_event"): dict,
    }
)
EDIT_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(ATTR_EVENT_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("event"): dict,
        vol.Optional(CONF_CALENDAR_ENTITY): cv.entity_id,
        vol.Optional("expected_event"): dict,
    }
)
RESOLVE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PENDING_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(ATTR_EVENT_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("resolution"): vol.In(("created", "not_created", "discard")),
        vol.Optional("expected_event"): dict,
    }
)


def _expected_event(raw: dict | None, event_id: str) -> PendingEvent | None:
    """Decode an optional review snapshot for atomic decision checks."""
    if raw is None:
        return None
    if raw.get("id") != event_id or raw.get("status") != "pending":
        raise PendingEventEditError("Event changed since it was loaded; refresh before deciding")
    return PendingEvent(
        event_id, EventDraft.from_mapping(raw), raw["status"],
        raw.get(CONF_CALENDAR_ENTITY),
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


def _calendar_configuration(entry: ConfigEntry) -> tuple[str, tuple[str, ...]]:
    """Return effective calendar settings."""
    default_calendar, allowed_calendars = effective_calendar_options(entry)
    return default_calendar, tuple(allowed_calendars)


def _ai_task_configuration(entry: ConfigEntry) -> str:
    """Return the effective AI Task entity."""
    return effective_ai_task_entity(entry)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-wide APIs that survive config-entry reloads."""
    del config
    async_register_settings_api(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Daylight Calendar Import from a config entry."""
    # A failed sensor rollback may have left entities pointing at an old
    # store. Never silently replace it with a freshly loaded snapshot.
    previous_store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if previous_store is not None:
        if not getattr(previous_store, "rollback_pending", False):
            raise RuntimeError("Daylight entry is already initialized")
        runtime = getattr(previous_store, "email_runtime", None)
        if runtime is not None:
            await runtime.async_stop()
        if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
            raise RuntimeError("Previous Daylight sensor rollback is incomplete")
        hass.data[DOMAIN].pop(entry.entry_id, None)
    pending_store = PendingImportStore(hass)
    await pending_store.async_load()
    pending_store.active_submissions = set()
    # Track entry service calls *before* their first await so unload cannot
    # miss a request waiting for authorization or a source claim.
    pending_store.active_service_handlers = set()
    pending_store.accepting_services = True
    default_calendar, allowed_calendars = _calendar_configuration(entry)
    ai_task_entity = _ai_task_configuration(entry)
    await async_register_review_panel(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = pending_store

    def tracked(handler):
        async def invoke(call):
            if not pending_store.accepting_services:
                raise ServiceValidationError("Daylight is unloading; retry after reload")
            task = asyncio.current_task()
            pending_store.active_service_handlers.add(task)
            try:
                return await handler(call)
            finally:
                pending_store.active_service_handlers.discard(task)

        return invoke

    async def parse_submission(source: SourceDocument, activity_id: str) -> Any:
        """Finish a stopped parser without obscuring its original error."""
        try:
            return await _async_parse_source(hass, source, ai_task_entity)
        except (asyncio.CancelledError, Exception):
            try:
                await pending_store.async_record_parse_failure(activity_id)
            except Exception:
                pass
            raise

    async def process_email_document(
        source: SourceDocument,
        activity_id: str,
    ) -> None:
        """Send one normalized email through the existing review pipeline."""
        # Email polling owns retry/terminal classification for parser failures.
        outcome = await _async_parse_source(hass, source, ai_task_entity)
        await pending_store.async_add(
            source_text=email_review_source_text(source),
            events=outcome.events,
            source_id=source.upstream_source_id,
            calendar_entity=default_calendar,
            source_kind=source.kind.value,
            source_title=source.title,
            source_sender=source.metadata.get("sender"),
            warnings=outcome.warnings,
            activity_id=activity_id,
        )

    def event_calendar(event: PendingEvent) -> str:
        calendar_entity = event.calendar_entity or default_calendar
        if calendar_entity not in allowed_calendars:
            raise ServiceValidationError("Event calendar is not in the allowed calendars")
        return calendar_entity

    async def handle_parse_text(call: ServiceCall) -> ServiceResponse:
        outcome = await _parse_text_with_ai_task(
            hass, ai_task_entity, call.data[ATTR_TEXT], context=call.context
        )
        return {"events": [draft.as_dict() for draft in outcome.events],
                "warnings": outcome.warnings}

    async def handle_import_text(call: ServiceCall) -> ServiceResponse:
        outcome = await _parse_text_with_ai_task(
            hass, ai_task_entity, call.data[ATTR_TEXT], context=call.context
        )
        for draft in outcome.events:
            await _async_create_calendar_event(
                hass,
                default_calendar,
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
        activity_id = await pending_store.async_begin_submission(
            source_kind=source.kind.value, source_title=source.title, received_at=source.received_at
        )
        task = asyncio.current_task()
        pending_store.active_submissions.add(task)
        try:
            outcome = await parse_submission(source, activity_id)
            result = await pending_store.async_add(
                source_text=source.text,
                events=outcome.events,
                source_id=source.upstream_source_id,
                calendar_entity=default_calendar,
                warnings=outcome.warnings,
                activity_id=activity_id,
            )
        finally:
            pending_store.active_submissions.discard(task)
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

    async def submit_attachment_source(source: SourceDocument) -> ServiceResponse:
        """Send a normalized upload through parsing and pending review."""
        if source.upstream_source_id is not None and pending_store.is_source_duplicate(source.upstream_source_id):
            return {"pending": None, "duplicate": True, "duplicate_source": True,
                    "duplicate_events": 0, "warnings": []}
        activity_id = await pending_store.async_begin_submission(
            source_kind=source.kind.value, source_title=source.title, received_at=source.received_at
        )
        task = asyncio.current_task()
        pending_store.active_submissions.add(task)
        try:
            outcome = await parse_submission(source, activity_id)
            label = "PDF" if source.kind is SourceKind.PDF else "Image"
            attachment_note = f"{label} attachment (SHA-256: {source.attachments[0].sha256})" if source.attachments else ""
            result = await pending_store.async_add(
                source_text="\n\n".join(part for part in (source.text, attachment_note) if part),
                events=outcome.events,
                source_id=source.upstream_source_id,
                calendar_entity=default_calendar,
                source_kind=source.kind.value,
                source_title=source.title,
                warnings=outcome.warnings,
                activity_id=activity_id,
            )
        finally:
            pending_store.active_submissions.discard(task)
        return {"pending": result.pending.as_service_dict() if result.pending else None,
                "duplicate": result.duplicate_source or result.duplicate_events > 0,
                "duplicate_source": result.duplicate_source,
                "duplicate_events": result.duplicate_events,
                "warnings": outcome.warnings}

    async def handle_submit_image(call: ServiceCall) -> ServiceResponse:
        """Queue event drafts from an uploaded image and optional source text."""
        await _async_check_entity_control_permission(
            hass, ai_task_entity, call.context
        )
        async with async_image_source(hass, call.data[ATTR_FILE_ID]) as image:
            source = replace(image, text=call.data.get(ATTR_TEXT, "").strip() or None,
                             upstream_source_id=call.data.get(ATTR_SOURCE_ID))
            return await submit_attachment_source(source)

    async def handle_submit_pdf(call: ServiceCall) -> ServiceResponse:
        """Queue event drafts from a text PDF or attachment-capable parser."""
        await _async_check_entity_control_permission(
            hass, ai_task_entity, call.context
        )
        async with async_pdf_source(hass, call.data[ATTR_FILE_ID], call.data.get(ATTR_TEXT, "")) as pdf:
            return await submit_attachment_source(
                replace(pdf, upstream_source_id=call.data.get(ATTR_SOURCE_ID))
            )

    async def handle_approve_pending(call: ServiceCall) -> ServiceResponse:
        pending_id = call.data[ATTR_PENDING_ID]
        pending_to_approve = pending_store.get(pending_id)
        if pending_to_approve is None:
            await _async_check_entity_control_permission(
                hass, default_calendar, call.context
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
            hass, ai_task_entity, call.context
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
                "source_kind": item.source_kind,
                "source_title": item.source_title,
                "warnings": list(item.warnings),
                "duplicate_events": item.duplicate_events,
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
        result["allowed_calendars"] = list(allowed_calendars)
        result["default_calendar"] = default_calendar
        return {"pending": result}

    async def handle_list_activity(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        return {"activity": [
            {key: value for key, value in record.items() if key != "transitions"}
            for record in pending_store.list_activity()
        ]}

    async def handle_get_activity(call: ServiceCall) -> ServiceResponse:
        await check_read_permission(call)
        pending_id = call.data[ATTR_PENDING_ID]
        record = pending_store.get_activity(pending_id)
        if record is None:
            raise ServiceValidationError(f"Activity not found: {pending_id}")
        return {"activity": record}

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
            expected = call.data.get("expected_event")
            expected_event = None
            if expected is not None:
                if expected.get("id") != event_id or expected.get("status") not in (
                    "pending", "write_uncertain"
                ):
                    raise PendingEventEditError("Event changed since it was loaded; refresh before editing")
                expected_event = PendingEvent(
                    event_id, EventDraft.from_mapping(expected), expected["status"],
                    expected.get(CONF_CALENDAR_ENTITY),
                )
            calendar_entity = call.data.get(CONF_CALENDAR_ENTITY)
            if calendar_entity is not None and calendar_entity not in allowed_calendars:
                raise ServiceValidationError("Calendar is not in the allowed calendars")
            edited = await pending_store.async_edit_event(
                pending_id, event_id, draft, calendar_entity=calendar_entity,
                expected_event=expected_event,
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
            expected_event = _expected_event(call.data.get("expected_event"), event_id)
            if expected_event is None:
                rejected = await pending_store.async_reject_event(pending_id, event_id)
            else:
                rejected = await pending_store.async_reject_event(
                    pending_id, event_id, expected_event=expected_event,
                )
        except PendingImportApprovalUncertainError as err:
            raise ServiceValidationError(
                "This event has an uncertain calendar write; resolve it before rejecting"
            ) from err
        except (DraftValidationError, PendingEventEditError) as err:
            raise ServiceValidationError(str(err)) from err
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
            expected_event = _expected_event(call.data.get("expected_event"), event_id)
            if expected_event is None:
                event = await pending_store.async_approve_event(
                    pending_id, event_id, create_event,
                )
            else:
                event = await pending_store.async_approve_event(
                    pending_id, event_id, create_event, expected_event=expected_event,
                )
        except PendingImportApprovalUncertainError as err:
            raise ServiceValidationError(
                "This event has an uncertain calendar write; verify it before retrying"
            ) from err
        except (DraftValidationError, PendingEventEditError) as err:
            raise ServiceValidationError(str(err)) from err
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
            snapshot = call.data.get("expected_event")
            expected = None
            if snapshot is not None:
                if snapshot.get("id") != event_id or snapshot.get("status") != "write_uncertain":
                    raise PendingEventResolutionError("Event changed since it was loaded; refresh before resolving")
                expected = PendingEvent(event_id, EventDraft.from_mapping(snapshot),
                                        snapshot["status"], snapshot.get(CONF_CALENDAR_ENTITY),
                                        snapshot.get("write_attempt"))
            if expected is None:
                resolved = await pending_store.async_resolve_uncertain(pending_id, event_id, resolution)
            else:
                resolved = await pending_store.async_resolve_uncertain(
                    pending_id, event_id, resolution, expected_event=expected,
                )
        except (DraftValidationError, PendingEventResolutionError) as err:
            raise ServiceValidationError(str(err)) from err
        if not resolved:
            raise ServiceValidationError(
                f"Pending event not found: {pending_id}/{event_id}"
            )
        return {"pending_id": pending_id, "event_id": event_id,
                "resolution": resolution}

    try:
        hass.services.async_register(
            DOMAIN,
            SERVICE_PARSE_TEXT,
            tracked(handle_parse_text),
            schema=PARSE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_TEXT,
            tracked(handle_import_text),
            schema=PARSE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SUBMIT_TEXT,
            tracked(handle_submit_text),
            schema=SUBMIT_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SUBMIT_IMAGE, tracked(handle_submit_image),
            schema=SUBMIT_IMAGE_SCHEMA, supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SUBMIT_PDF, tracked(handle_submit_pdf),
            schema=SUBMIT_PDF_SCHEMA, supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_APPROVE_PENDING,
            tracked(handle_approve_pending),
            schema=PENDING_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REJECT_PENDING,
            tracked(handle_reject_pending),
            schema=PENDING_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_LIST_PENDING, tracked(handle_list_pending),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_LIST_ACTIVITY, tracked(handle_list_activity),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_GET_ACTIVITY, tracked(handle_get_activity),
            schema=PENDING_SCHEMA, supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_GET_PENDING, tracked(handle_get_pending),
            schema=PENDING_SCHEMA, supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_GET_PENDING_EVENT, tracked(handle_get_pending_event),
            schema=PENDING_EVENT_SCHEMA, supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_EDIT_PENDING_EVENT, tracked(handle_edit_pending_event),
            schema=EDIT_EVENT_SCHEMA, supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_REJECT_PENDING_EVENT, tracked(handle_reject_pending_event),
            schema=PENDING_EVENT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_APPROVE_PENDING_EVENT, tracked(handle_approve_pending_event),
            schema=PENDING_EVENT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_RESOLVE_PENDING_EVENT, tracked(handle_resolve_pending_event),
            schema=RESOLVE_EVENT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )
        pending_store.email_runtime = await async_setup_email_runtime(
            hass,
            entry,
            pending_store,
            process_email_document,
        )
        # Forward last: a failing email configuration cannot strand sensor entities.
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    except (Exception, asyncio.CancelledError):
        # Even a failed or cancelled partial forward must not leave its sensor
        # entities pointing to a discarded store.
        pending_store.accepting_services = False
        _unregister_entry_services(hass)
        cleanup_ok = True
        runtime = getattr(pending_store, "email_runtime", None)
        if runtime is not None:
            try:
                await runtime.async_stop()
            except Exception:
                cleanup_ok = False
                _LOGGER.exception("Failed to stop email runtime during setup rollback")
        try:
            if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
                cleanup_ok = False
                _LOGGER.error("Failed to roll back Daylight sensor platform")
        except (Exception, asyncio.CancelledError):
            cleanup_ok = False
            _LOGGER.exception("Failed to roll back sensor setup")
        async_remove_review_panel(hass)
        if cleanup_ok:
            hass.data[DOMAIN].pop(entry.entry_id, None)
        else:
            # Keep the store reachable so the next setup can retry cleanup
            # before reading a second, potentially stale storage snapshot.
            pending_store.rollback_pending = True
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Finish unload atomically from the caller's perspective, even on cancellation."""
    store = hass.data[DOMAIN][entry.entry_id]
    store.accepting_services = False

    async def finish_unload() -> bool:
        try:
            platforms_unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except BaseException:
            store.accepting_services = True
            raise
        if not platforms_unloaded:
            store.accepting_services = True
            return False

        _unregister_entry_services(hass)
        async_remove_review_panel(hass)
        email_runtime = getattr(store, "email_runtime", None)
        if email_runtime is not None:
            await email_runtime.async_stop()
        accepted = set(store.active_submissions) | set(getattr(store, "active_service_handlers", ()))
        if accepted:
            await asyncio.gather(*accepted, return_exceptions=True)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return True

    # Shield the entire sequence, not just gathering handlers: cancellation
    # after platform unload must not strand a half-torn-down entry.
    completion = asyncio.create_task(finish_unload())
    try:
        return await asyncio.shield(completion)
    except asyncio.CancelledError:
        await completion
        raise

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove persisted data when the config entry is deleted."""
    await PendingImportStore(hass).async_remove_storage()


async def _parse_text_with_ai_task(
    hass: HomeAssistant,
    ai_task_entity: str,
    text: str,
    *,
    context: Context | None = None,
) -> ParseOutcome:
    """Authorize and parse text with one immutable AI Task selection."""
    await _async_check_entity_control_permission(hass, ai_task_entity, context)
    source = TextSourceAdapter().create(text)
    return await _async_parse_source(hass, source, ai_task_entity)


async def _async_parse_source(
    hass: HomeAssistant,
    source: SourceDocument,
    ai_task_entity: str,
) -> ParseOutcome:
    """Feed a normalized source into the current text parser boundary."""
    if source.text is None and not source.attachments:
        raise SourceValidationError(
            "empty_source",
            "This source has no text or attachments for the configured parser",
        )
    return await parse_source_with_provider(
        hass,
        source=source,
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
