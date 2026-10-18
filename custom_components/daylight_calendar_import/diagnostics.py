"""Allowlisted operational aggregates for Home Assistant diagnostics downloads."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_AI_TASK_ENTITY, DOMAIN
from .provider_failures import PROVIDER_FAILURE_GUIDANCE
from .providers import AITaskParserProvider, ProviderError

_ACTIVITY_STATUSES = (
    "discovered", "processing", "failed", "duplicate", "no_events", "review_ready",
    "calendar_write_uncertain", "calendar_created", "event_rejected", "mixed",
)
_TRANSITION_TYPES = (
    "received", "discovered", "processing", "failed", "duplicate", "no_events",
    "review_ready", "calendar_write_started", "calendar_write_uncertain",
    "calendar_create_failed", "calendar_created", "event_rejected", "source_reprocessed",
    "notification_delivery_failed",
)
_EVENT_STATUSES = ("pending", "write_uncertain")
_NOTIFICATION_STATUSES = ("pending", "sent", "failed", "cancelled")


def _counts(values: Iterable[object], keys: Iterable[str]) -> dict[str, int]:
    """Never turn a stored value into a diagnostic key."""
    counts = dict.fromkeys(keys, 0)
    counts["unknown"] = 0
    for value in values:
        key = value if isinstance(value, str) and value in counts else "unknown"
        counts[key] += 1
    return counts


def _transitions(activity: Iterable[object]) -> Iterable[object]:
    """Count malformed stored history under a fixed unknown bucket."""
    for record in activity:
        steps = record.get("transitions", ()) if isinstance(record, Mapping) else None
        if not isinstance(steps, (tuple, list)):
            yield None
            continue
        for step in steps:
            yield step.get("type") if isinstance(step, Mapping) else None


def _parser_summary(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Inspect declared capabilities, with no parse call or provider request."""
    entity_id = entry.options.get(CONF_AI_TASK_ENTITY, entry.data.get(CONF_AI_TASK_ENTITY))
    configured = isinstance(entity_id, str) and bool(entity_id.strip())
    result: dict[str, Any] = {
        "configured": configured, "capabilities": None, "capability_error": None,
    }
    if not configured:
        return result
    try:
        capabilities = AITaskParserProvider(hass, entity_id).capabilities
    except ProviderError as error:
        result["capability_error"] = (
            error.code if isinstance(error.code, str) and error.code in PROVIDER_FAILURE_GUIDANCE
            else "provider_error"
        )
        return result
    result["capabilities"] = {
        "text": capabilities.text, "images": capabilities.images, "pdfs": capabilities.pdfs,
        "max_attachments": capabilities.max_attachments,
        "max_total_bytes": capabilities.max_total_bytes,
    }
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry,
) -> dict[str, Any]:
    """Return fixed-key counts and capabilities without copying private records."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    pending = store.list() if store is not None else ()
    activity = store.list_activity() if store is not None else ()
    notifications = store.list_notifications() if store is not None else ()
    events = tuple(event for item in pending for event in item.events)
    return {
        "runtime": {
            "loaded": store is not None,
            "accepting_services": getattr(store, "accepting_services", False) is True,
        },
        "parser": _parser_summary(hass, entry),
        "queue": {
            "imports": len(pending), "events": len(events),
            "event_status": _counts((event.status for event in events), _EVENT_STATUSES),
        },
        "activity": {
            "records": len(activity),
            "status": _counts(
                (record.get("status") if isinstance(record, Mapping) else None for record in activity),
                _ACTIVITY_STATUSES,
            ),
            "transitions": _counts(_transitions(activity), _TRANSITION_TYPES),
        },
        "provider_failures": _counts(
            (record["failure_code"] for record in activity
             if isinstance(record, Mapping) and record.get("failure_code") is not None),
            PROVIDER_FAILURE_GUIDANCE,
        ),
        "notifications": {
            "records": len(notifications),
            "status": _counts((record.status for record in notifications), _NOTIFICATION_STATUSES),
        },
    }
