"""AI-backed parsing for Daylight Calendar Import."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import ai_task
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .models import DraftValidationError, EventDraft

TASK_NAME = "Extract calendar event drafts"
PROMPT_TEMPLATE = """Extract every calendar event explicitly supported by the source text below.

Rules:
- Do not invent dates, times, locations, titles, or durations.
- If an event lacks enough information to produce both start and end, omit it.
- For timed events, return ISO 8601 datetimes with explicit UTC offsets.
- For all-day events, return ISO 8601 dates; end is exclusive, matching calendar semantics.
- Preserve useful source details in description when appropriate.
- confidence is from 0 to 1 and reflects confidence in the extracted event.
- Resolve relative dates and local clock times using the reference datetime and Home Assistant time zone below.
- Return no events when the source does not contain a calendar event.

Reference datetime: {reference_datetime}
Home Assistant time zone: {time_zone}

Source text:
{text}
"""

EVENTS_STRUCTURE = vol.Schema(
    {
        vol.Required(
            "events",
            description="Calendar events explicitly supported by the source text",
        ): selector.ObjectSelector(
            {
                "multiple": True,
                "fields": {
                    "title": {
                        "required": True,
                        "label": "Short event title",
                        "selector": {"text": {}},
                    },
                    "start": {
                        "required": True,
                        "label": "ISO date or timezone-aware ISO datetime",
                        "selector": {"text": {}},
                    },
                    "end": {
                        "required": True,
                        "label": "Exclusive ISO end date or timezone-aware ISO datetime",
                        "selector": {"text": {}},
                    },
                    "all_day": {
                        "required": True,
                        "label": "True only for an all-day event",
                        "selector": {"boolean": {}},
                    },
                    "location": {
                        "label": "Event location when explicitly present",
                        "selector": {"text": {}},
                    },
                    "description": {
                        "label": "Useful supporting details from the source",
                        "selector": {"text": {"multiline": True}},
                    },
                    "confidence": {
                        "required": True,
                        "label": "Confidence from 0 through 1",
                        "selector": {"number": {"min": 0, "max": 1, "step": 0.01}},
                    },
                },
            }
        ),
    }
)


class ParseResultError(ValueError):
    """Raised when an AI Task response cannot be converted to drafts."""


async def async_parse_text(
    hass: HomeAssistant,
    *,
    text: str,
    ai_task_entity: str,
) -> list[EventDraft]:
    """Parse free-form text into validated event drafts."""
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("text must not be empty")

    time_zone = hass.config.time_zone
    local_tz = dt_util.get_time_zone(time_zone)
    reference_datetime = dt_util.now(time_zone=local_tz).isoformat()

    result = await ai_task.async_generate_data(
        hass,
        task_name=TASK_NAME,
        entity_id=ai_task_entity,
        instructions=PROMPT_TEMPLATE.format(
            text=clean_text,
            reference_datetime=reference_datetime,
            time_zone=time_zone,
        ),
        structure=EVENTS_STRUCTURE,
    )
    return parse_ai_data(result.data)


def parse_ai_data(data: Any) -> list[EventDraft]:
    """Convert AI structured output into validated drafts."""
    if not isinstance(data, dict):
        raise ParseResultError("AI Task result must be an object")

    events = data.get("events")
    if not isinstance(events, list):
        raise ParseResultError("AI Task result must contain an events list")

    drafts: list[EventDraft] = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            raise ParseResultError(f"event {index} must be an object")
        try:
            drafts.append(EventDraft.from_mapping(raw))
        except DraftValidationError as err:
            raise ParseResultError(f"event {index} is invalid: {err}") from err
    return drafts
