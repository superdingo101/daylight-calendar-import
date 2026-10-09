"""AI-backed parsing for Daylight Calendar Import."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .models import DraftValidationError, EventDraft
from .sources import SourceDocument, TextSourceAdapter

TASK_NAME = "Extract calendar event drafts"
PROMPT_TEMPLATE = """Extract every calendar event explicitly supported by the source text and any attached images or PDFs.

Rules:
- Do not invent dates, times, locations, titles, or durations.
- If an event lacks enough information to produce both start and end, omit it.
- For timed events, return ISO 8601 datetimes with explicit UTC offsets.
- For all-day events, return ISO 8601 dates; end is exclusive, matching calendar semantics.
- Preserve useful source details in description when appropriate.
- confidence is from 0 to 1 and reflects confidence in the extracted event.
- Resolve explicitly relative dates and local clock times using the reference datetime and Home Assistant time zone below.
- Do not infer missing event durations, end times, dates or years that are not deterministically supported by the source and reference date.
- For each returned event, include an assumptions list explaining every contextual date/time resolution (for example a relative day or a local time zone). Use an empty list for fully explicit timestamps.
- Return no events when the source does not contain a calendar event.

Reference datetime: {reference_datetime}
Home Assistant time zone: {time_zone}

Source text (may be empty when the event is in an attachment):
{text}
"""

EVENTS_STRUCTURE = vol.Schema(
    {
        vol.Required(
            "events",
            description="Calendar events explicitly supported by the source text or attached images or PDFs",
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
                    "assumptions": {
                        "required": True,
                        "label": "Short disclosures of context used to resolve dates or times",
                        "selector": {"text": {"multiple": True}},
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


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """Validated drafts and indexed problems with individual AI output items."""

    events: list[EventDraft]
    warnings: list[str]
    event_assumptions: list[tuple[str, ...]] = field(default_factory=list)


async def async_parse_text(
    hass: HomeAssistant,
    *,
    text: str,
    ai_task_entity: str,
) -> ParseOutcome:
    """Parse free-form text into validated event drafts."""
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("text must not be empty")

    return await async_parse_source(
        hass, source=TextSourceAdapter().create(clean_text), ai_task_entity=ai_task_entity
    )


async def async_parse_source(
    hass: HomeAssistant, *, source: SourceDocument, ai_task_entity: str
) -> ParseOutcome:
    """Route a normalized source through the configured parser provider."""
    from .providers import AITaskParserProvider

    time_zone = hass.config.time_zone
    local_tz = dt_util.get_time_zone(time_zone)
    reference_datetime = dt_util.now(time_zone=local_tz).isoformat()
    provider = AITaskParserProvider(hass, ai_task_entity)
    return await provider.async_parse(
        source,
        reference_datetime=reference_datetime,
        time_zone=time_zone,
    )


def parse_ai_data(data: Any) -> ParseOutcome:
    """Retain valid drafts when individual AI output items are malformed."""
    if not isinstance(data, dict):
        raise ParseResultError("AI Task result must be an object")

    events = data.get("events")
    if not isinstance(events, list):
        raise ParseResultError("AI Task result must contain an events list")

    drafts: list[EventDraft] = []
    event_assumptions: list[tuple[str, ...]] = []
    warnings: list[str] = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            warnings.append(f"event {index} must be an object")
            continue
        try:
            draft = EventDraft.from_mapping(raw)
            assumptions = raw.get("assumptions", [])
            if not isinstance(assumptions, list) or len(assumptions) > 8 or any(
                not isinstance(value, str) or not value.strip() or len(value) > 160
                for value in assumptions
            ):
                raise DraftValidationError("assumptions must be up to eight short strings")
            drafts.append(draft)
            event_assumptions.append(tuple(value.strip() for value in assumptions))
        except DraftValidationError as err:
            warnings.append(f"event {index} is invalid: {err}")
    return ParseOutcome(drafts, warnings, event_assumptions)
