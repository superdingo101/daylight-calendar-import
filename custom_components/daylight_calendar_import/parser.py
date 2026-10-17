"""AI-backed parsing for Daylight Calendar Import."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .models import (
    DraftValidationError, EventDraft, UNSUPPORTED_RECURRENCE_WARNING,
    normalize_date_time_assumptions,
)
from .sources import SourceDocument, TextSourceAdapter

TASK_NAME = "Extract calendar event drafts"
PROMPT_TEMPLATE = """Extract every calendar event explicitly supported by the source text and any attached images or PDFs.

Rules:
- Do not invent dates, times, locations, titles, or durations.
- If an event lacks enough information to produce both start and end, omit it.
- For timed events, return ISO 8601 datetimes with explicit UTC offsets.
- For all-day events, return ISO 8601 dates; end is exclusive, matching calendar semantics.
- Preserve useful source details in description when appropriate.
- Preserve complete meeting join details in description: Zoom, Google Meet and Microsoft Teams URLs (including query parameters), meeting IDs, passcodes and dial-in numbers/instructions. Copy these details exactly when present; do not invent missing credentials or links. Keep an explicitly supplied location as well.
- Preserve complete event instructions, agendas, preparation requirements and other substantive description details even when lengthy. Do not summarize or silently truncate them; preserve paragraph breaks, lists and Unicode text.
- confidence is from 0 to 1 and reflects confidence in the extracted event.
- Resolve explicitly relative dates and local clock times using the reference datetime and Home Assistant time zone below.
- Do not infer missing event durations, end times, dates or years that are not deterministically supported by the source and reference date.
- For each returned event, include an assumptions list explaining every contextual date/time resolution (for example a relative day or a local time zone). Use an empty list for fully explicit timestamps.
- Recurring schedules are unsupported. Set unsupported_recurrence to true if source text or attachments describe any repeating schedule (including RRULE). Return no events for that source; never expand recurrence or silently reduce it to a single occurrence. An explicit list of independent dates is not recurrence.
- Always return unsupported_recurrence as a boolean, false for sources without a repeating schedule.
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
                        "label": "Supporting details including complete meeting links, IDs, passcodes and dial-in instructions",
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
        vol.Required("unsupported_recurrence"): selector.BooleanSelector(),
    }
)

# Bounded, schedule-shaped English backstop for provider false negatives.
# The required AI flag remains responsible for other wording and attachments.
_RECURRENCE_PATTERN = re.compile(
    r"\b(?:rrule\s*:|(?:every|each)\s+(?:(?:other|\d+)\s+)?"
    r"(?:weekday|week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b"
    r"|(?:repeat(?:s|ing)?|recurs?|recurr(?:ing|ence))\s+"
    r"(?:every|daily|weekly|monthly|yearly|annually)\b"
    r"|meets?\s+(?:daily|weekly|monthly|yearly|annually)\b"
    r"|(?:every|each)\s+(?:other|\d+)\s+days?\b"
    r"|(?:every|each)\s+day\s+(?:at|until|through|from)\b"
    r"|(?:daily|weekly|monthly|yearly|annual)\s+"
    r"(?:standup|meeting|class|practice|training|session|appointment)\b"
    r"|(?:meets?|practice|class|meeting|training|session|runs?|occurs?|repeats?)"
    r"\s+(?:every|each)\s+day\b)", re.IGNORECASE,
)
_NEGATED_RECURRENCE_PATTERN = re.compile(
    r"\b(?:does\s+not|do\s+not|doesn't|don't|will\s+not|won't|never|not)\s+"
    r"(?:repeat(?:s|ing)?|recurs?|meet(?:s)?|occur(?:s)?)\s+"
    r"(?:(?:every|each)\s+(?:(?:other|\d+)\s+)?\w+|daily|weekly|monthly|yearly|annually)\b",
    re.IGNORECASE,
)


def _has_recurring_schedule(source_text: str) -> bool:
    """Recognize common explicit schedules without treating prose as recurrence."""
    return _RECURRENCE_PATTERN.search(
        _NEGATED_RECURRENCE_PATTERN.sub("", source_text)
    ) is not None


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
    hass: HomeAssistant, *, source: SourceDocument, ai_task_entity: str,
    reference_datetime: datetime | None = None,
) -> ParseOutcome:
    """Route a normalized source through the configured parser provider."""
    from .providers import AITaskParserProvider

    time_zone = hass.config.time_zone
    local_tz = dt_util.get_time_zone(time_zone)
    if reference_datetime is None:
        reference = dt_util.now(time_zone=local_tz)
    else:
        if reference_datetime.utcoffset() is None:
            raise ValueError("reference datetime must include a time zone")
        reference = reference_datetime.astimezone(local_tz)
    provider = AITaskParserProvider(hass, ai_task_entity)
    return await provider.async_parse(
        source,
        reference_datetime=reference.isoformat(),
        time_zone=time_zone,
    )


def parse_ai_data(data: Any, *, source_text: str = "") -> ParseOutcome:
    """Retain valid drafts when individual AI output items are malformed."""
    if not isinstance(data, dict):
        raise ParseResultError("AI Task result must be an object")

    events = data.get("events")
    if not isinstance(events, list):
        raise ParseResultError("AI Task result must contain an events list")

    recurrence = data.get("unsupported_recurrence", False)
    if not isinstance(recurrence, bool):
        raise ParseResultError("unsupported_recurrence must be a boolean")
    if recurrence or _has_recurring_schedule(source_text):
        return ParseOutcome([], [UNSUPPORTED_RECURRENCE_WARNING])

    drafts: list[EventDraft] = []
    event_assumptions: list[tuple[str, ...]] = []
    warnings: list[str] = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            warnings.append(f"event {index} must be an object")
            continue
        try:
            draft = EventDraft.from_mapping(raw)
            try:
                assumptions = normalize_date_time_assumptions(raw.get("assumptions", []))
            except ValueError as err:
                raise DraftValidationError(str(err)) from err
            drafts.append(draft)
            event_assumptions.append(assumptions)
        except DraftValidationError as err:
            warnings.append(f"event {index} is invalid: {err}")
    return ParseOutcome(drafts, warnings, event_assumptions)
